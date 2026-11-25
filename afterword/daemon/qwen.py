"""Local TTS models through mlx-audio: one engine thread, models loaded on
first use and kept warm.

Model kinds: the three Qwen3-TTS variants (custom_voice, base, voice_design),
Kokoro-82M (kokoro) and Chatterbox Multilingual (chatterbox). The module
keeps its v1 name; QwenEngine hosts every kind.

All MLX work (loading, rendering) runs on a single dedicated thread, so the
asyncio loop never blocks and MLX state is only touched from one thread. The
daemon's playback queue and the voice lab share that thread; work runs one
item at a time, live speech (priority LIVE) before lab work (LAB), then in
submission order. A job can carry a cancel event, checked at every chunk.
Live speech renders stream (`stream=True`): each chunk is handed to an
`on_chunk` callback as soon as the model yields it. A lab job submitted as
`preemptible` (the seed explorer's) is stopped at its next chunk when live
speech is queued, and fails with RenderPreempted so its owner can retry.

The thread is a daemon thread: a render in progress never keeps the process
alive after shutdown. Queued work is cancelled at shutdown, and a running
render stops at its next audio chunk.
"""

from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import logging
import math
import queue
import random
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import voices

log = logging.getLogger("afterword.qwen")

QWEN_KINDS = ("custom_voice", "base", "voice_design")
MODEL_KINDS = QWEN_KINDS + ("kokoro", "chatterbox")
DEFAULT_REPOS = {
    "custom_voice": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
    "base": "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit",
    "voice_design": "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
    "kokoro": "mlx-community/Kokoro-82M-bf16",
    "chatterbox": "mlx-community/chatterbox-multilingual-v3",
}
LABELS = {
    "custom_voice": "Qwen3-TTS CustomVoice",
    "base": "Qwen3-TTS Base",
    "voice_design": "Qwen3-TTS VoiceDesign",
    "kokoro": "Kokoro",
    "chatterbox": "Chatterbox Multilingual",
}
# Other repos a kind loads from at load time (pulled with it).
EXTRA_REPOS = {"chatterbox": ("mlx-community/S3TokenizerV2",)}
# Small models that do not count against keep_loaded (Kokoro is ~0.4 GB).
LIGHT_KINDS = ("kokoro",)
# mlx-audio model_type when the snapshot's config.json does not name it.
MODEL_TYPES = {"kokoro": "kokoro"}
PULL_COMMAND = "uv run afterword models pull"
MAX_PENDING = 4
WARMUP_TEXT = "Ready."
STREAMING_INTERVAL = 1.0  # seconds of audio per streamed chunk (first audio ~0.24 s warm)

# Priority classes: lower runs first; arrival order within a class.
LIVE = 0  # the playback queue (and engine housekeeping)
LAB = 1  # voice lab previews and warm-ups

# Qwen3-TTS emits 12.5 codec tokens per second of audio. Cap each render at
# a generous multiple of how long the text could plausibly take to say, so a
# degenerate render cannot run for minutes (the model default is 2048-8192).
TOKENS_PER_SECOND = 12.5
SECONDS_BASE = 8.0
SECONDS_PER_CHAR = 0.25

# Chatterbox emits 25 speech tokens per second of audio, at most 1000 per
# call (about 40 s), and does not say when it stopped at the cap. Longer
# text is rendered in pieces of at most CHATTERBOX_PIECE_S estimated seconds
# (split at sentences, then clauses), so no piece comes near the cap.
CHATTERBOX_TOKENS_PER_SECOND = 25.0
CHATTERBOX_MAX_TOKENS = 1000
CHATTERBOX_PIECE_S = 20.0

# Files the mlx-audio loader needs, relative to the snapshot root.
REQUIRED_FILES = (
    "config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "speech_tokenizer/config.json",
    "speech_tokenizer/model.safetensors",
)
REQUIRED_FILES_FOR = {
    "kokoro": ("config.json", "voices/af_heart.safetensors"),
    "chatterbox": ("config.json", "tokenizer.json", "model.safetensors"),
    "mlx-community/S3TokenizerV2": ("config.json", "model.safetensors"),
}
# Sentence ends: Kokoro renders (and so streams) one sentence at a time.
SENTENCE_SPLIT = r"(?<=[.!?。！？])\s+|(?<=[。！？])"


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?。！？])\s*")
_CLAUSE_END_RE = re.compile(r"(?<=[,;:，、；：])\s*")


def speech_seconds(text: str) -> float:
    """A rough spoken length: about 0.25 s per CJK character, 0.07 s per
    other character (generous for both)."""
    cjk = len(_CJK_RE.findall(text))
    return 0.25 * cjk + 0.07 * (len(text) - cjk)


def split_by_duration(text: str, max_s: float) -> list[str]:
    """`text` in pieces of at most `max_s` estimated seconds: whole
    sentences where they fit, else clauses, else words (or characters)."""
    text = text.strip()
    if not text or speech_seconds(text) <= max_s:
        return [text] if text else []

    def units(part: str, level: int) -> list[str]:
        if speech_seconds(part) <= max_s:
            return [part]
        if level == 0:
            pieces = [x for x in _SENTENCE_END_RE.split(part) if x.strip()]
        elif level == 1:
            pieces = [x for x in _CLAUSE_END_RE.split(part) if x.strip()]
        elif level == 2:
            pieces = part.split(" ") if " " in part.strip() else list(part)
        else:
            per = max(1, int(max_s / 0.25))
            return [part[i:i + per] for i in range(0, len(part), per)]
        if len(pieces) <= 1:
            return units(part, level + 1)
        return [u for x in pieces for u in units(x, level + 1)]

    out: list[str] = []
    for unit in units(text, 0):
        sep = "" if (not out or _CJK_RE.match(unit[:1]) or out[-1].endswith(" ")) else " "
        if out and speech_seconds(out[-1] + sep + unit) <= max_s:
            out[-1] = out[-1] + sep + unit
        else:
            out.append(unit)
    return [o.strip() for o in out if o.strip()]


def required_files(kind_or_repo: str | None) -> tuple[str, ...]:
    return REQUIRED_FILES_FOR.get(kind_or_repo or "", REQUIRED_FILES)


def label(kind: str) -> str:
    return LABELS.get(kind, kind)


class EngineUnavailable(RuntimeError):
    """mlx-audio is not installed."""


class ModelNotDownloaded(RuntimeError):
    pass


class EngineBusy(RuntimeError):
    pass


class EngineStopped(RuntimeError):
    pass


class RenderCancelled(RuntimeError):
    """The job's cancel event was set (skip/stop)."""


class RenderPreempted(RenderCancelled):
    """A preemptible lab render was stopped because live speech was queued."""


class _Either:
    """Set when any of its events is set (a job's own cancel or its preempt)."""

    def __init__(self, *events: threading.Event):
        self.events = events

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.events)


def attach_partial(exc: BaseException, partial: Any) -> None:
    """Record on an exception what a render did before it stopped (its
    timings; see `partial` on RenderCancelled and render failures)."""
    try:
        exc.partial = partial  # type: ignore[attr-defined]
    except Exception:
        pass


def mlx_audio_available() -> tuple[bool, str | None]:
    if importlib.util.find_spec("mlx_audio") is None:
        return False, "mlx-audio is not installed; run `uv sync --extra qwen`"
    return True, None


def snapshot_complete(path: Path, required: tuple[str, ...] = REQUIRED_FILES) -> bool:
    """True if every file the loader needs is present (an interrupted
    download leaves a snapshot folder that is not)."""
    if not all((path / rel).is_file() for rel in required):
        return False
    index = path / "model.safetensors.index.json"
    if index.is_file():
        try:
            shards = set(json.loads(index.read_text()).get("weight_map", {}).values())
        except (OSError, ValueError):
            return False
        return bool(shards) and all((path / s).is_file() for s in shards)
    return any(path.glob("*.safetensors"))


def local_snapshot(repo: str, required: tuple[str, ...] = REQUIRED_FILES) -> Path | None:
    """Path of a complete cached model snapshot, or None. Never downloads."""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        return None
    try:
        path = Path(snapshot_download(repo, local_files_only=True))
    except Exception:
        # A repo fetched with allow_patterns (mlx-audio does this for
        # companion repos) is a partial snapshot: look for the files we need.
        try:
            path = Path(snapshot_download(repo, local_files_only=True, allow_patterns=list(required)))
        except Exception:
            return None
    return path if snapshot_complete(path, required) else None


def max_tokens_for(text: str, per_second: float = TOKENS_PER_SECOND) -> int:
    return int(per_second * (SECONDS_BASE + SECONDS_PER_CHAR * len(text)))


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
    lang: str | None = None  # ISO code of the line (Kokoro, Chatterbox), None = auto


@dataclass
class RenderResult:
    audio: Any  # numpy float32 array
    sample_rate: int
    synth_s: float
    load_s: float
    seed: int | None
    model: str
    first_chunk_s: float | None = None  # generation start to the first chunk


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
    priority: int = LAB
    seq: int = 0
    cancel: Any = None  # threading.Event or _Either
    kwargs: dict = field(default_factory=dict)
    preempt: threading.Event | None = None  # set by a LIVE render submitted while this runs
    live: bool = False  # a LIVE render (live speech)


class QwenEngine:
    def __init__(self, repos: dict[str, str] | None = None, keep_loaded: int = 2):
        self.repos = dict(DEFAULT_REPOS)
        if repos:
            self.repos.update({k: v for k, v in repos.items() if k in MODEL_KINDS and v})
        self.keep_loaded = max(1, keep_loaded)
        self._models: OrderedDict[str, _Loaded] = OrderedDict()
        self._lock = threading.Lock()
        self._jobs: queue.PriorityQueue[tuple[int, int, _Job | None]] = queue.PriorityQueue()
        self._seq = 0
        self._pending = 0
        self._busy = False
        self._current: _Job | None = None  # the running job (written under _lock)
        self._live = 0  # LIVE renders queued or running
        self._live_ended = 0.0  # time.monotonic() when the last LIVE render ended
        self.preemptions = 0
        self._stopping = False
        self._last_load_ms: dict[str, float] = {}
        self._errors: dict[str, str] = {}
        self._conds: OrderedDict[tuple, Any] = OrderedDict()  # Chatterbox voice conditionals, per clip
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
                self._submit(self._trim, limit=False, priority=LIVE)
            except EngineStopped:
                pass

    def snapshot_path(self, kind: str) -> Path | None:
        return self._snapshot(self.repos[kind])

    def _available(self) -> tuple[bool, str | None]:
        return mlx_audio_available()

    def _snapshot(self, repo: str) -> Path | None:
        """Seam: where a repo's complete snapshot is (tests replace it)."""
        kind = next((k for k, r in self.repos.items() if r == repo), None)
        return local_snapshot(repo, required_files(kind))

    def _extra_snapshot(self, repo: str) -> Path | None:
        """Seam: a companion repo's snapshot (EXTRA_REPOS)."""
        return local_snapshot(repo, required_files(repo))

    def _missing_extras(self, kind: str) -> list[str]:
        return [r for r in EXTRA_REPOS.get(kind, ()) if self._extra_snapshot(r) is None]

    def status(self) -> dict[str, Any]:
        available, error = mlx_audio_available()
        models = {}
        loaded_now = dict(self._models)
        for kind in MODEL_KINDS:
            loaded = loaded_now.get(kind)
            models[kind] = {
                "label": label(kind),
                "repo": self.repos[kind],
                "downloaded": self.snapshot_path(kind) is not None and not self._missing_extras(kind),
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

    def _submit(
        self, fn, *args, limit: bool = True, priority: int = LAB,
        cancel: Any = None, preempt: threading.Event | None = None, **kwargs,
    ) -> concurrent.futures.Future:
        """Queue `fn(*args, **kwargs)` for the engine thread. `limit` applies
        MAX_PENDING (lab work); `priority` LIVE jobs run before LAB ones. A
        LIVE render stops a running preemptible job (one with `preempt`)."""
        with self._lock:
            if self._stopping:
                raise EngineStopped("the engine is shutting down")
            if limit and self._pending >= MAX_PENDING:
                raise EngineBusy(f"{self._pending} renders already waiting")
            self._pending += 1
            self._seq += 1
            future: concurrent.futures.Future = concurrent.futures.Future()
            live = fn == self._render and priority <= LIVE
            job = _Job(fn, args, future, priority, self._seq, cancel, kwargs, preempt, live)
            self._jobs.put((priority, self._seq, job))
            if live:
                self._live += 1
                current = self._current
                if current is not None and current.preempt is not None and not current.preempt.is_set():
                    current.preempt.set()
                    self.preemptions += 1
                    log.info("live speech pre-empts the running lab render")
        return future

    def live_quiet_s(self) -> float:
        """Seconds since the last LIVE render ended; 0.0 while one is queued
        or running, inf when there has been none."""
        with self._lock:
            if self._live:
                return 0.0
            if not self._live_ended:
                return math.inf
            return max(0.0, time.monotonic() - self._live_ended)

    def check_ready(self, kind: str) -> None:
        """Raise EngineUnavailable / ModelNotDownloaded now, without loading,
        when a render of `kind` would fail that way."""
        available, error = self._available()
        if not available:
            raise EngineUnavailable(error)
        repo = self.repos[kind]
        missing = ([repo] if self._snapshot(repo) is None else []) + self._missing_extras(kind)
        if missing:
            raise ModelNotDownloaded(
                f"{label(kind)} model {', '.join(missing)} is not downloaded (or the download is incomplete); "
                f"run `{PULL_COMMAND} {kind}`"
            )

    def _run(self) -> None:
        while True:
            _, _, job = self._jobs.get()
            if job is None:
                return
            try:
                self._run_job(job)
            finally:
                if job.live:
                    with self._lock:
                        self._live -= 1
                        self._live_ended = time.monotonic()

    def _run_job(self, job: _Job) -> None:
        with self._lock:
            self._pending -= 1
            # A preemptible job taken off the queue just as live speech was
            # queued (a LIVE render waiting means one was submitted after
            # this job was dequeued): pre-empt it before it starts.
            if job.preempt is not None and self._live and not job.preempt.is_set():
                job.preempt.set()
                self.preemptions += 1
            self._current = job
        try:
            if not job.future.set_running_or_notify_cancel():
                return  # cancelled while queued
            if self._stopping:
                job.future.set_exception(EngineStopped("the engine is shutting down"))
                return
            if job.preempt is not None and job.preempt.is_set():
                job.future.set_exception(RenderPreempted("pre-empted by live speech before it started"))
                return
            if job.cancel is not None and job.cancel.is_set():
                job.future.set_exception(RenderCancelled("cancelled before it started"))
                return
            self._busy = True
            try:
                result = job.fn(*job.args, **job.kwargs)
            except BaseException as exc:
                if (isinstance(exc, RenderCancelled) and job.preempt is not None and job.preempt.is_set()
                        and not isinstance(exc, RenderPreempted)):
                    new = RenderPreempted("pre-empted by live speech")
                    attach_partial(new, getattr(exc, "partial", None))
                    exc = new
                job.future.set_exception(exc)
            else:
                job.future.set_result(result)
        finally:
            with self._lock:
                self._current = None
            self._busy = False

    def submit_render(
        self,
        req: RenderRequest,
        *,
        limit: bool = True,
        priority: int = LAB,
        cancel: threading.Event | None = None,
        on_chunk: Callable[[Any, int], None] | None = None,
        stream: bool = False,
        preemptible: bool = False,
    ) -> concurrent.futures.Future:
        """Queue a render. With `stream`, the model streams and every chunk
        goes to `on_chunk(audio, sample_rate)` on the engine thread as it
        arrives (an exception from it stops the render). `cancel` is checked
        before the job starts and at every chunk (RenderCancelled).
        `preemptible` (lab work only): a LIVE render submitted while this one
        runs stops it at its next chunk (RenderPreempted); stream it, or the
        model only yields once, at the end."""
        extra: dict[str, Any] = {}
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if stream:
            extra["stream"] = True
        preempt = threading.Event() if preemptible and priority > LIVE else None
        check: Any = cancel
        if preempt is not None:
            check = _Either(cancel, preempt) if cancel is not None else preempt
        if check is not None:
            extra["cancel_event"] = check
        return self._submit(self._render, req, limit=limit, priority=priority, cancel=check, preempt=preempt, **extra)

    def render(self, req: RenderRequest, limit: bool = True, priority: int = LAB, **kw) -> RenderResult:
        """Blocking render (call from a worker thread, not the event loop)."""
        return self.submit_render(req, limit=limit, priority=priority, **kw).result()

    async def render_async(self, req: RenderRequest, priority: int = LAB) -> RenderResult:
        import asyncio

        return await asyncio.wrap_future(self.submit_render(req, priority=priority))

    async def warm_async(self, kind: str, priority: int = LAB) -> float:
        """Load `kind` (not counted against MAX_PENDING). The daemon's
        start-up warm-up passes LIVE; the lab's uses the LAB default."""
        import asyncio

        return await asyncio.wrap_future(self._submit(self._ensure_timed, kind, limit=False, priority=priority))

    def shutdown(self, wait: float = 0.0) -> None:
        """Cancel queued work and stop the thread. A render in progress stops
        at its next chunk; the daemon thread never blocks process exit."""
        with self._lock:
            self._stopping = True
            drained = []
            while True:
                try:
                    drained.append(self._jobs.get_nowait()[2])
                except queue.Empty:
                    break
            self._pending = 0
        for job in drained:
            if job is not None:
                job.future.cancel()
        self._jobs.put((-1, 0, None))
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
        while self._heavy_count() > self.keep_loaded:
            self._unload(self._oldest_heavy())

    def _heavy_count(self, extra: str | None = None) -> int:
        """Loaded models counted against keep_loaded (LIGHT_KINDS are not)."""
        kinds = list(self._models) + ([extra] if extra else [])
        return sum(1 for k in kinds if k not in LIGHT_KINDS)

    def _oldest_heavy(self) -> str:
        return next(k for k in self._models if k not in LIGHT_KINDS)

    def _ensure(self, kind: str) -> tuple[Any, float]:
        repo = self.repos[kind]
        loaded = self._models.get(kind)
        if loaded is not None and loaded.repo == repo:
            self._models.move_to_end(kind)
            return loaded.model, 0.0
        if loaded is not None:
            self._unload(kind)  # config now points at another repo
        self.check_ready(kind)
        path = self._snapshot(repo)
        while kind not in LIGHT_KINDS and self._heavy_count(kind) > self.keep_loaded:
            self._unload(self._oldest_heavy())
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

        model_type = MODEL_TYPES.get(kind)
        model = load_model(Path(path), **({"model_type": model_type} if model_type else {}))
        self._warmup(kind, model)
        return model

    @staticmethod
    def _warmup(kind: str, model: Any) -> None:
        """One tiny render so the first real one does not pay for kernel
        compilation (about 5 s on first use). A failure here means the model
        cannot render (e.g. missing speech tokenizer), so it fails the load."""
        if kind in ("base", "chatterbox"):
            return  # needs a reference clip; the first clone render warms it
        if kind == "kokoro":
            # Builds the English pipeline (spaCy and the lexicon load once
            # per process); other languages' pipelines are built on first use.
            gen = model.generate(text=WARMUP_TEXT, voice="af_heart", lang_code="a", verbose=False)
        elif kind == "custom_voice":
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
        if kind == "chatterbox":
            self._conds.clear()
        try:
            import gc

            import mlx.core as mx

            gc.collect()
            mx.clear_cache()
        except Exception:
            pass
        log.info("unloaded %s", kind)

    def _render(
        self,
        req: RenderRequest,
        on_chunk: Callable[[Any, int], None] | None = None,
        stream: bool = False,
        cancel_event: Any = None,
    ) -> RenderResult:
        kind = req.voice.model
        if kind is None:
            raise ValueError(f"{req.voice} is not a local model voice")
        model, load_s = self._ensure(kind)

        import mlx.core as mx

        seed = req.seed if req.seed is not None else random.randrange(0, 2**31 - 1)
        mx.random.seed(seed)
        t = time.perf_counter()
        if kind == "kokoro":
            gen = self._kokoro_generate(model, req)
        elif kind == "chatterbox":
            gen = self._chatterbox_generate(model, req, stream)
        else:
            gen = self._qwen_generate(model, req, stream)
        if req.voice.kind not in voices.SEEDED_KINDS:
            seed = None  # deterministic: no seed to record or reuse
        return self._collect(gen, model, kind, t, load_s, seed, on_chunk, cancel_event)

    def _kokoro_generate(self, model, req: RenderRequest):
        voice = req.voice.value
        return model.generate(
            text=req.text, voice=voice, speed=1.0, lang_code=voices.kokoro_lang(voice, req.lang),
            split_pattern=SENTENCE_SPLIT, verbose=False,
        )

    def _chatterbox_conds(self, model, ref_audio: Path):
        """The clip's conditioning, computed once per clip file (it is the
        slow part of a short line)."""
        st = Path(ref_audio).stat()
        key = (str(ref_audio), st.st_mtime_ns, st.st_size)
        conds = self._conds.get(key)
        if conds is None:
            conds = model.prepare_conditionals(str(ref_audio), int(model.sample_rate))
            self._conds[key] = conds
            while len(self._conds) > 8:
                self._conds.popitem(last=False)
        else:
            self._conds.move_to_end(key)
        return conds

    def _chatterbox_generate(self, model, req: RenderRequest, stream: bool):
        """One generator over the text's pieces (split_by_duration), each a
        Chatterbox call with a token budget well inside its cap."""
        if req.ref_audio is None:
            raise voices.VoiceError(f"{req.voice} needs a reference clip")
        lang = voices.chatterbox_lang(req.lang)
        conds = self._chatterbox_conds(model, req.ref_audio)
        pieces = split_by_duration(req.text, CHATTERBOX_PIECE_S) or [req.text]

        def gen():
            for piece in pieces:
                budget = CHATTERBOX_TOKENS_PER_SECOND * (SECONDS_BASE + 2.0 * speech_seconds(piece))
                kw: dict[str, Any] = dict(
                    text=piece, lang_code=lang, conds=conds,
                    max_new_tokens=int(min(CHATTERBOX_MAX_TOKENS, budget)), verbose=False,
                )
                if req.temperature is not None:
                    kw["temperature"] = req.temperature
                if stream:
                    kw.update(stream=True, streaming_interval=STREAMING_INTERVAL)
                yield from model.generate(**kw)

        return gen()

    def _qwen_generate(self, model, req: RenderRequest, stream: bool):
        max_tokens = max_tokens_for(req.text)
        common = dict(text=req.text, language=req.language, temperature=req.temperature, max_tokens=max_tokens)
        if stream:
            common.update(stream=True, streaming_interval=STREAMING_INTERVAL)
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
                **({"stream": True, "streaming_interval": STREAMING_INTERVAL} if stream else {}),
            )
        return gen

    def _collect(self, gen, model, kind: str, t: float, load_s: float, seed: int,
                 on_chunk: Callable[[Any, int], None] | None, cancel_event: Any) -> RenderResult:
        """Run a model's generator: chunks to `on_chunk` as they come, the
        cancel event checked at each, timings kept on failure."""
        import numpy as np

        sample_rate = int(model.sample_rate)
        chunks = []
        first_chunk_s = None
        try:
            for r in gen:
                if self._stopping:
                    raise EngineStopped("the engine is shutting down")
                if cancel_event is not None and cancel_event.is_set():
                    raise RenderCancelled("cancelled")
                chunk = np.asarray(r.audio, dtype=np.float32).reshape(-1)
                if first_chunk_s is None:
                    first_chunk_s = time.perf_counter() - t
                chunks.append(chunk)
                if on_chunk is not None and chunk.size:
                    on_chunk(chunk, sample_rate)
        except Exception as exc:
            # A cancelled or failed render still took time: keep its timings
            # (and the audio made so far) for the event log.
            audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
            attach_partial(exc, RenderResult(
                audio, sample_rate, time.perf_counter() - t, load_s, seed, kind, first_chunk_s))
            raise
        finally:
            close = getattr(gen, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        synth_s = time.perf_counter() - t
        if audio.size == 0:
            raise RuntimeError(f"{label(kind)} returned no audio")
        return RenderResult(audio, sample_rate, synth_s, load_s, seed, kind, first_chunk_s)


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
    only apply to builtin and design voices (the Base model, Kokoro and
    Chatterbox have no instruction input)."""
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
    elif voice.kind == "chatterbox":
        ref_audio, _ = clips.resolve(voice.value)
        ref_text = None  # Chatterbox needs no transcript
    elif voice.kind == "kokoro":
        voices.kokoro_lang(voice.value, lang)  # an unsupported language fails now
        ref_text = None
    return RenderRequest(
        text=text,
        voice=voice,
        language=language,
        instruct=instruct,
        ref_audio=ref_audio,
        ref_text=ref_text,
        seed=seed,
        temperature=temperature,
        lang=voices.lang_iso(lang),
    )


def uses_style(voice: voices.VoiceSpec) -> bool:
    return voice.kind in ("builtin", "design")
