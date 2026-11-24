"""Qwen3-TTS through mlx-audio: one engine thread, models loaded on first use
and kept warm.

All MLX work (loading, rendering) runs on a single dedicated thread, so the
asyncio loop never blocks and MLX state is only touched from one thread. The
daemon's playback queue and the voice lab share that thread; work runs one
item at a time in submission order.

The thread is a daemon thread: a render in progress never keeps the process
alive after shutdown. Queued work is cancelled at shutdown, and a running
render stops at its next audio chunk.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import logging
import queue
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import voices

log = logging.getLogger("afterword.qwen")

MODEL_KINDS = ("custom_voice", "base", "voice_design")
DEFAULT_REPOS = {
    "custom_voice": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
    "base": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
    "voice_design": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
}
PULL_COMMAND = "uv run afterword models pull"
MAX_PENDING = 4
WARMUP_TEXT = "Ready."

# Qwen3-TTS emits 12.5 codec tokens per second of audio. Cap each render at
# a generous multiple of how long the text could plausibly take to say, so a
# degenerate render cannot run for minutes (the model default is 2048-8192).
TOKENS_PER_SECOND = 12.5
SECONDS_BASE = 8.0
SECONDS_PER_CHAR = 0.25

# Files the mlx-audio loader needs, relative to the snapshot root.
REQUIRED_FILES = (
    "config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "speech_tokenizer/config.json",
    "speech_tokenizer/model.safetensors",
)


class EngineUnavailable(RuntimeError):
    """mlx-audio is not installed."""


class ModelNotDownloaded(RuntimeError):
    pass


class EngineBusy(RuntimeError):
    pass


class EngineStopped(RuntimeError):
    pass


def mlx_audio_available() -> tuple[bool, str | None]:
    if importlib.util.find_spec("mlx_audio") is None:
        return False, "mlx-audio is not installed; run `uv sync --extra qwen`"
    return True, None


def snapshot_complete(path: Path) -> bool:
    """True if every file the loader needs is present (an interrupted
    download leaves a snapshot folder that is not)."""
    if not all((path / rel).is_file() for rel in REQUIRED_FILES):
        return False
    index = path / "model.safetensors.index.json"
    if index.is_file():
        try:
            shards = set(json.loads(index.read_text()).get("weight_map", {}).values())
        except (OSError, ValueError):
            return False
        return bool(shards) and all((path / s).is_file() for s in shards)
    return any(path.glob("*.safetensors"))


def local_snapshot(repo: str) -> Path | None:
    """Path of a complete cached model snapshot, or None. Never downloads."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return None
    try:
        path = Path(snapshot_download(repo, local_files_only=True))
    except Exception:
        return None
    return path if snapshot_complete(path) else None


def max_tokens_for(text: str) -> int:
    return int(TOKENS_PER_SECOND * (SECONDS_BASE + SECONDS_PER_CHAR * len(text)))


@dataclass
class RenderRequest:
    text: str
    voice: voices.VoiceSpec
    language: str = "auto"
    instruct: str | None = None
    ref_audio: Path | None = None
    ref_text: str | None = None
    seed: int | None = None
    temperature: float | None = None


@dataclass
class RenderResult:
    audio: Any  # numpy float32 array
    sample_rate: int
    synth_s: float
    load_s: float
    seed: int
    model: str


@dataclass
class _Loaded:
    model: Any
    repo: str
    load_ms: float
    loaded_at: float = field(default_factory=time.time)


@dataclass
class _Job:
    fn: Callable
    args: tuple
    future: concurrent.futures.Future


class QwenEngine:
    def __init__(self, repos: dict[str, str] | None = None, keep_loaded: int = 2):
        self.repos = dict(DEFAULT_REPOS)
        if repos:
            self.repos.update({k: v for k, v in repos.items() if k in MODEL_KINDS and v})
        self.keep_loaded = max(1, keep_loaded)
        self._models: OrderedDict[str, _Loaded] = OrderedDict()
        self._lock = threading.Lock()
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._pending = 0
        self._busy = False
        self._stopping = False
        self._last_load_ms: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._thread = threading.Thread(target=self._run, name="qwen", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ #
    # Introspection and config (any thread)                              #
    # ------------------------------------------------------------------ #

    def configure(self, repos: dict[str, str], keep_loaded: int) -> None:
        """Apply [qwen] config. Models from an old repo, and models beyond
        the new keep_loaded, are unloaded on the engine thread."""
        changed = False
        with self._lock:
            for kind in MODEL_KINDS:
                new = repos.get(kind) or DEFAULT_REPOS[kind]
                if new != self.repos[kind]:
                    self.repos[kind] = new
                    self._errors.pop(kind, None)
                    changed = True
            new_keep = max(1, keep_loaded)
            if new_keep != self.keep_loaded:
                self.keep_loaded = new_keep
                changed = True
        if changed:
            try:
                self._submit(self._trim, limit=False)
            except EngineStopped:
                pass

    def snapshot_path(self, kind: str) -> Path | None:
        return self._snapshot(self.repos[kind])

    def _available(self) -> tuple[bool, str | None]:
        return mlx_audio_available()

    def _snapshot(self, repo: str) -> Path | None:
        """Seam: where a repo's complete snapshot is (tests replace it)."""
        return local_snapshot(repo)

    def status(self) -> dict[str, Any]:
        available, error = mlx_audio_available()
        models = {}
        loaded_now = dict(self._models)
        for kind in MODEL_KINDS:
            loaded = loaded_now.get(kind)
            models[kind] = {
                "repo": self.repos[kind],
                "downloaded": self.snapshot_path(kind) is not None,
                "loaded": loaded is not None,
                "load_ms": loaded.load_ms if loaded else self._last_load_ms.get(kind),
                "error": self._errors.get(kind),
            }
        return {
            "available": available,
            "error": error,
            "busy": self._busy,
            "pending": self._pending,
            "memory": self._memory() if loaded_now else None,
            "models": models,
            "pull_command": PULL_COMMAND,
        }

    @staticmethod
    def _memory() -> dict[str, float] | None:
        try:
            import mlx.core as mx

            return {
                "active_gb": round(mx.get_active_memory() / 1e9, 2),
                "peak_gb": round(mx.get_peak_memory() / 1e9, 2),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Submitting work                                                    #
    # ------------------------------------------------------------------ #

    def _submit(self, fn, *args, limit: bool = True) -> concurrent.futures.Future:
        with self._lock:
            if self._stopping:
                raise EngineStopped("the engine is shutting down")
            if limit and self._pending >= MAX_PENDING:
                raise EngineBusy(f"{self._pending} renders already waiting")
            self._pending += 1
            future: concurrent.futures.Future = concurrent.futures.Future()
            self._jobs.put(_Job(fn, args, future))
        return future

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            with self._lock:
                self._pending -= 1
            if not job.future.set_running_or_notify_cancel():
                continue  # cancelled while queued
            if self._stopping:
                job.future.set_exception(EngineStopped("the engine is shutting down"))
                continue
            self._busy = True
            try:
                result = job.fn(*job.args)
            except BaseException as exc:
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)
            finally:
                self._busy = False

    def render(self, req: RenderRequest, limit: bool = True) -> RenderResult:
        """Blocking render (call from a worker thread, not the event loop)."""
        return self._submit(self._render, req, limit=limit).result()

    async def render_async(self, req: RenderRequest) -> RenderResult:
        import asyncio

        return await asyncio.wrap_future(self._submit(self._render, req))

    async def warm_async(self, kind: str) -> float:
        import asyncio

        return await asyncio.wrap_future(self._submit(self._ensure_timed, kind, limit=False))

    def shutdown(self, wait: float = 0.0) -> None:
        """Cancel queued work and stop the thread. A render in progress stops
        at its next chunk; the daemon thread never blocks process exit."""
        with self._lock:
            self._stopping = True
            drained = []
            while True:
                try:
                    drained.append(self._jobs.get_nowait())
                except queue.Empty:
                    break
            self._pending = 0
        for job in drained:
            if job is not None:
                job.future.cancel()
        self._jobs.put(None)
        if wait:
            self._thread.join(wait)

    # ------------------------------------------------------------------ #
    # Engine thread only                                                 #
    # ------------------------------------------------------------------ #

    def _ensure_timed(self, kind: str) -> float:
        _, load_s = self._ensure(kind)
        return load_s

    def _trim(self) -> None:
        for kind in list(self._models):
            if self._models[kind].repo != self.repos[kind]:
                self._unload(kind)
        while len(self._models) > self.keep_loaded:
            self._unload(next(iter(self._models)))

    def _ensure(self, kind: str) -> tuple[Any, float]:
        repo = self.repos[kind]
        loaded = self._models.get(kind)
        if loaded is not None and loaded.repo == repo:
            self._models.move_to_end(kind)
            return loaded.model, 0.0
        if loaded is not None:
            self._unload(kind)  # config now points at another repo
        available, error = self._available()
        if not available:
            raise EngineUnavailable(error)
        path = self._snapshot(repo)
        if path is None:
            raise ModelNotDownloaded(
                f"Qwen3-TTS model {repo} is not downloaded (or the download is incomplete); "
                f"run `{PULL_COMMAND} {kind}`"
            )
        while len(self._models) >= self.keep_loaded:
            self._unload(next(iter(self._models)))
        t = time.perf_counter()
        try:
            model = self._load_model(kind, path)
        except Exception as exc:
            self._errors[kind] = f"{type(exc).__name__}: {exc}"
            raise
        load_s = time.perf_counter() - t
        self._errors.pop(kind, None)
        self._models[kind] = _Loaded(model, repo, round(load_s * 1000, 1))
        self._last_load_ms[kind] = round(load_s * 1000, 1)
        log.info("loaded %s (%s) in %.1f s", kind, repo, load_s)
        return model, load_s

    def _load_model(self, kind: str, path: Path) -> Any:
        """Load and warm one model. The seam tests replace."""
        from mlx_audio.tts.utils import load_model

        model = load_model(path)
        self._warmup(kind, model)
        return model

    @staticmethod
    def _warmup(kind: str, model: Any) -> None:
        """One tiny render so the first real one does not pay for kernel
        compilation (about 5 s on first use). A failure here means the model
        cannot render (e.g. missing speech tokenizer), so it fails the load."""
        if kind == "base":
            return  # needs a reference clip; the first clone render warms it
        if kind == "custom_voice":
            speaker = (model.get_supported_speakers() or ["ryan"])[0]
            gen = model.generate_custom_voice(text=WARMUP_TEXT, speaker=speaker, language="english", max_tokens=64)
        else:
            gen = model.generate_voice_design(
                text=WARMUP_TEXT, instruct="A calm, neutral voice.", language="english", max_tokens=64
            )
        for _ in gen:
            pass

    def _unload(self, kind: str) -> None:
        loaded = self._models.pop(kind, None)
        if loaded is None:
            return
        del loaded
        try:
            import gc

            import mlx.core as mx

            gc.collect()
            mx.clear_cache()
        except Exception:
            pass
        log.info("unloaded %s", kind)

    def _render(self, req: RenderRequest) -> RenderResult:
        import numpy as np

        kind = req.voice.model
        if kind is None:
            raise ValueError(f"{req.voice} is not a Qwen voice")
        model, load_s = self._ensure(kind)

        import mlx.core as mx

        seed = req.seed if req.seed is not None else random.randrange(0, 2**31 - 1)
        mx.random.seed(seed)
        t = time.perf_counter()
        max_tokens = max_tokens_for(req.text)
        common = dict(text=req.text, language=req.language, temperature=req.temperature, max_tokens=max_tokens)
        if req.voice.kind == "builtin":
            speaker = req.voice.value
            supported = {s.lower(): s for s in model.get_supported_speakers()}
            if speaker.lower() not in supported:
                raise voices.VoiceError(
                    f"unknown speaker {speaker!r}; available: "
                    + ", ".join(voices.display_speaker(s) for s in supported)
                )
            gen = model.generate_custom_voice(speaker=speaker.lower(), instruct=req.instruct, **common)
        elif req.voice.kind == "design":
            gen = model.generate_voice_design(instruct=req.instruct or req.voice.value, **common)
        else:  # clone
            gen = model.generate(
                text=req.text,
                lang_code=req.language,
                ref_audio=str(req.ref_audio),
                ref_text=req.ref_text or None,
                temperature=req.temperature,
                max_tokens=max_tokens,
                split_pattern="",
            )
        chunks = []
        for r in gen:
            if self._stopping:
                raise EngineStopped("the engine is shutting down")
            chunks.append(np.asarray(r.audio, dtype=np.float32).reshape(-1))
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        synth_s = time.perf_counter() - t
        if audio.size == 0:
            raise RuntimeError("Qwen3-TTS returned no audio")
        return RenderResult(audio, int(model.sample_rate), synth_s, load_s, seed, kind)


def build_request(
    text: str,
    voice: voices.VoiceSpec,
    clips: voices.ClipStore,
    *,
    lang: str | None,
    style: str | None,
    mood: str | None,
    ref_text: str | None,
    seed: int | None,
    temperature: float | None,
) -> RenderRequest:
    """Turn profile-level settings into a render request. Style and mood
    only apply to builtin and design voices (the Base model has no
    instruction input)."""
    language = voices.language_name(lang)
    ref_audio = None
    instruct = None
    if voice.kind == "builtin":
        instruct = voices.compose_instruct(style, mood)
    elif voice.kind == "design":
        instruct = voices.compose_instruct(voice.value, style, mood)
    elif voice.kind == "clone":
        ref_audio, saved = clips.resolve(voice.value)
        ref_text = ref_text or saved or None
    return RenderRequest(
        text=text,
        voice=voice,
        language=language,
        instruct=instruct,
        ref_audio=ref_audio,
        ref_text=ref_text,
        seed=seed,
        temperature=temperature,
    )


def uses_style(voice: voices.VoiceSpec) -> bool:
    return voice.kind in ("builtin", "design")
