"""Apple's speech synthesizer (AVSpeechSynthesizer) through a small Swift
helper, for `apple:<voice>` voices and as the fallback when a model voice
fails.

The helper (apple/avspeech.swift) is compiled with swiftc on first use into
~/.afterword/bin/avspeech-<hash of the source> and run once per line: text
on stdin, mono float32 PCM on stdout as the synthesizer makes it (header
"AWPCM1" + uint32 sample rate). Unlike `say`, the audio comes back through
AfterWord's own player (streaming, pace, skip, clip history), and Premium or
Enhanced voices downloaded in System Settings → Accessibility → Spoken
Content are used when installed.

AFTERWORD_APPLE_HELPER overrides the helper: a command line (tests use a
Python stub) or "none" to turn Apple voices off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import shutil
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from afterword.hooks import common

from . import voices
from .config import Profile
from .synth import STREAM_RATE, Sink, StreamResult, SynthResult, _check_cancel

log = logging.getLogger("afterword.apple")

SOURCE = Path(__file__).parent / "apple" / "avspeech.swift"
MAGIC = b"AWPCM1"
HEADER = len(MAGIC) + 4
READ_BYTES = 4 * 2205  # ~0.1 s at 22.05 kHz
BUILD_TIMEOUT_S = 180.0
RENDER_TIMEOUT_S = 120.0
LIST_TTL_S = 600.0
RETRY_BUILD_S = 600.0

_lock = threading.Lock()
_build_error: tuple[float, str] | None = None
_voice_cache: tuple[float, list[dict[str, Any]]] | None = None


class AppleUnavailable(RuntimeError):
    """The helper cannot be built or is turned off."""


def _source_hash() -> str:
    return hashlib.sha256(SOURCE.read_bytes()).hexdigest()[:12]


def binary_path() -> Path:
    return common.home() / "bin" / f"avspeech-{_source_hash()}"


def _override() -> list[str] | None:
    raw = os.environ.get("AFTERWORD_APPLE_HELPER")
    if raw is None or not raw.strip():
        return None
    return shlex.split(raw)


def disabled() -> bool:
    cmd = _override()
    return bool(cmd) and cmd[0].lower() in ("none", "off", "0")


def helper_command(build: bool = True) -> list[str]:
    """argv for the helper, compiling it first if needed (and `build`).
    Raises AppleUnavailable with a user-facing reason."""
    global _build_error
    cmd = _override()
    if cmd:
        if disabled():
            raise AppleUnavailable("Apple voices are turned off (AFTERWORD_APPLE_HELPER=none)")
        return cmd
    path = binary_path()
    if path.is_file() and os.access(path, os.X_OK):
        return [str(path)]
    if not build:
        raise AppleUnavailable("the Apple voice helper is not built yet")
    with _lock:
        if path.is_file():
            return [str(path)]
        if _build_error and time.monotonic() - _build_error[0] < RETRY_BUILD_S:
            raise AppleUnavailable(_build_error[1])
        swiftc = shutil.which("swiftc") or ("/usr/bin/swiftc" if Path("/usr/bin/swiftc").exists() else None)
        if swiftc is None:
            _build_error = (time.monotonic(), "swiftc not found; install the Xcode command line tools "
                                              "(xcode-select --install) to use Apple voices")
            raise AppleUnavailable(_build_error[1])
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        t = time.perf_counter()
        try:
            proc = subprocess.run(
                [swiftc, "-O", "-framework", "AVFoundation", str(SOURCE), "-o", str(tmp)],
                capture_output=True, text=True, timeout=BUILD_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _build_error = (time.monotonic(), f"could not build the Apple voice helper: {exc}")
            tmp.unlink(missing_ok=True)
            raise AppleUnavailable(_build_error[1]) from None
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            _build_error = (time.monotonic(), "could not build the Apple voice helper: "
                            + (proc.stderr or proc.stdout).strip()[-400:])
            raise AppleUnavailable(_build_error[1])
        os.replace(tmp, path)
        _build_error = None
        for old in path.parent.glob("avspeech-*"):  # helpers built from older sources
            if old != path and not old.name.endswith(".tmp"):
                old.unlink(missing_ok=True)
        log.info("built the Apple voice helper in %.1f s", time.perf_counter() - t)
        return [str(path)]


def available() -> tuple[bool, str | None]:
    """Whether Apple voices can be used, without building the helper (it is
    built on first use)."""
    if disabled():
        return False, "Apple voices are turned off (AFTERWORD_APPLE_HELPER=none)"
    if _override() or binary_path().is_file():
        return True, None
    if _build_error and time.monotonic() - _build_error[0] < RETRY_BUILD_S:
        return False, _build_error[1]
    if not (shutil.which("swiftc") or Path("/usr/bin/swiftc").exists()):
        return False, "swiftc not found; install the Xcode command line tools (xcode-select --install)"
    return True, None


def list_voices(refresh: bool = False) -> list[dict[str, Any]]:
    """Installed voices: id, name, lang, quality, novelty, personal. Personal
    Voice is listed but cannot be used (the system does not hand its audio
    to other apps). Empty when the helper is unavailable."""
    global _voice_cache
    if not refresh and _voice_cache and time.monotonic() - _voice_cache[0] < LIST_TTL_S:
        return list(_voice_cache[1])
    try:
        proc = subprocess.run(helper_command() + ["--list"], capture_output=True, timeout=30)
        items = json.loads(proc.stdout or b"[]") if proc.returncode == 0 else []
    except (AppleUnavailable, OSError, subprocess.SubprocessError, ValueError) as exc:
        log.info("no Apple voice list: %s", exc)
        items = []
    items = [v for v in items if isinstance(v, dict) and isinstance(v.get("name"), str)]
    order = {"premium": 0, "enhanced": 1, "default": 2}
    items.sort(key=lambda v: (str(v.get("lang")), order.get(v.get("quality"), 3), v["name"]))
    _voice_cache = (time.monotonic(), items)
    return list(items)


def _lang_arg(profile: Profile, attrs: dict[str, Any]) -> str | None:
    """The line's language with its region ("zh-TW" picks a Taiwan voice)."""
    return voices.lang_tag(attrs.get("lang") or profile.lang)


def _voice_value(profile: Profile) -> str:
    try:
        spec = voices.parse_voice(profile.voice)
    except voices.VoiceError:
        return ""
    return spec.value if spec.kind == "apple" else ""


class AppleSynth:
    """`apple:<voice>` (or, with `auto_voice`, the best installed voice for
    the line's language: the fallback for a failed model voice)."""

    name = "apple"

    def __init__(self, auto_voice: bool = False):
        self.auto_voice = auto_voice

    def _argv(self, profile: Profile, attrs: dict[str, Any]) -> list[str]:
        argv = helper_command()
        voice = "" if self.auto_voice else _voice_value(profile)
        if voice:
            argv += ["--voice", voice]
        lang = _lang_arg(profile, attrs)
        if lang:
            argv += ["--lang", lang]
        return argv

    def _run(self, text, profile, attrs, on_audio, cancel) -> tuple[float, float | None, int, int]:
        """Run the helper; `on_audio(float32 chunk, rate)` per read. Returns
        (synth_s, first_chunk_s, samples, rate)."""
        import numpy as np

        _check_cancel(cancel)
        argv = self._argv(profile, attrs)
        t = time.perf_counter()
        # stderr goes to a file, not a pipe: a helper that writes a lot of
        # diagnostics can never block on it while we read its audio.
        errfile = tempfile.TemporaryFile()
        proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile)
        done = threading.Event()

        def watchdog():
            # Kill the helper on skip/stop or a hung render.
            while not done.wait(0.05):
                if (cancel is not None and cancel.is_set()) or time.perf_counter() - t > RENDER_TIMEOUT_S:
                    proc.kill()
                    return

        threading.Thread(target=watchdog, name="apple-watchdog", daemon=True).start()
        first = None
        samples = 0
        rate = 0
        try:
            try:
                proc.stdin.write(text.encode("utf-8"))
                proc.stdin.close()
            except BrokenPipeError:
                pass
            header = proc.stdout.read(HEADER)
            if len(header) == HEADER and header[:len(MAGIC)] == MAGIC:
                rate = struct.unpack("<I", header[len(MAGIC):])[0]
                pending = b""
                while True:
                    block = proc.stdout.read1(READ_BYTES) if hasattr(proc.stdout, "read1") else proc.stdout.read(READ_BYTES)
                    if not block:
                        break
                    pending += block
                    usable = len(pending) - len(pending) % 4
                    if not usable:
                        continue
                    chunk = np.frombuffer(pending[:usable], dtype="<f4").astype(np.float32)
                    pending = pending[usable:]
                    _check_cancel(cancel)
                    if first is None:
                        first = time.perf_counter() - t
                    samples += chunk.size
                    on_audio(chunk, rate)
            proc.wait(timeout=10)
            _check_cancel(cancel)
            if proc.returncode != 0 or not samples:
                errfile.seek(0)
                err = errfile.read()[-4000:].decode("utf-8", "replace").strip()
                if proc.returncode == 2:
                    raise voices.VoiceError(err or "no such Apple voice")
                if proc.returncode == 4 or proc.returncode == -9:
                    raise RuntimeError("the Apple voice helper timed out")
                raise RuntimeError(f"Apple speech failed ({proc.returncode}): {err[:300] or 'no audio'}")
        finally:
            done.set()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for f in (proc.stdout, errfile):
                try:
                    f.close()
                except Exception:
                    pass
        return time.perf_counter() - t, first, samples, rate

    def synth(self, text: str, profile: Profile, attrs: dict[str, Any], out: Path) -> SynthResult:
        import numpy as np

        chunks: list[Any] = []
        rates: list[int] = []

        def keep(chunk, rate):
            chunks.append(chunk)
            rates.append(rate)

        synth_s, _, samples, rate = self._run(text, profile, attrs, keep, None)
        audio = np.concatenate(chunks) if chunks else np.zeros(0, np.float32)
        duration = voices.write_wav(out, audio, rate or 22050)
        pace = float(attrs.get("pace") or profile.pace or 1.0)
        return SynthResult(out, duration, synth_s, rate, playback_rate=pace, backend="apple")

    def stream(self, text, profile, attrs, sink: Sink, cancel=None) -> StreamResult:
        """Stream at pace 1.0 (the stream's time-stretch applies pace)."""
        from .qwen import attach_partial
        from .stream import StreamResampler

        resampler: list[Any] = []
        sent = [0]
        t = time.perf_counter()
        first: list[float | None] = [None]

        def feed(chunk, rate):
            if not resampler:
                resampler.append(StreamResampler(rate))
            out = resampler[0].feed(chunk)
            if out.size:
                if first[0] is None:
                    first[0] = time.perf_counter() - t
                sink(out)
                sent[0] += out.size

        try:
            synth_s, _, _, _ = self._run(text, profile, attrs, feed, cancel)
        except Exception as exc:
            if sent[0]:
                attach_partial(exc, StreamResult(
                    synth_s=time.perf_counter() - t, audio_s=sent[0] / STREAM_RATE,
                    first_chunk_s=first[0], backend="apple",
                ))
            raise
        return StreamResult(synth_s=synth_s, audio_s=sent[0] / STREAM_RATE, first_chunk_s=first[0], backend="apple")
