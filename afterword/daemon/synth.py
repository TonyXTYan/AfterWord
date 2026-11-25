"""Speech synthesis backends.

Each backend can render text to a WAV file (`synth`, the v1 path played with
afplay) or stream it (`stream`): 24 kHz mono float32 chunks handed to a sink
as they are made, for the streaming player. Both are blocking and run in a
worker thread by the server.

- qwen: the local models through mlx-audio (see qwen.py): Qwen3-TTS,
  Kokoro and Chatterbox, picked by each voice's prefix; `apple:` voices go
  to apple.py and `say:` voices to `say`. Pace is applied at playback
  (`playback_rate` for afplay, the WSOLA stretch when streaming), the same
  for every model (Qwen3-TTS has no speed control).
- say:  macOS `say`. Pace is applied while rendering for afplay; when
  streaming it renders at 1.0 (the stream's stretch applies pace), and the
  WAV is decoded with miniaudio and resampled to 24 kHz.
- null: silence, for tests.
"""

from __future__ import annotations

import concurrent.futures
import os
import struct
import subprocess
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import Profile

SAY_BASE_WPM = 175  # `say`'s default rate; pace 1.0 maps to this


@dataclass
class SynthResult:
    path: Path
    duration_s: float
    synth_s: float
    sample_rate: int
    playback_rate: float = 1.0  # the player applies this (pace for qwen)
    load_s: float = 0.0  # model load time paid by this render
    seed: int | None = None
    model: str | None = None
    backend: str = ""

    @property
    def rtf(self) -> float | None:
        """Real-time factor: synthesis time / audio duration (<1 is faster
        than real time)."""
        return self.synth_s / self.duration_s if self.duration_s > 0 else None


@dataclass
class StreamResult:
    """One streamed segment. Audio went to the sink at STREAM_RATE."""

    synth_s: float
    audio_s: float
    first_chunk_s: float | None = None  # render start to the first chunk
    load_s: float = 0.0
    seed: int | None = None
    model: str | None = None
    backend: str = ""


Sink = Callable[[Any], None]  # takes one float32 chunk at 24 kHz; raises to stop
STREAM_RATE = 24000
SAY_CHUNK_S = 0.5


class Synth(Protocol):
    name: str

    def synth(self, text: str, profile: Profile, attrs: dict[str, Any], out: Path) -> SynthResult: ...

    def stream(
        self, text: str, profile: Profile, attrs: dict[str, Any], sink: Sink, cancel: threading.Event
    ) -> StreamResult: ...


def partial_result(exc: BaseException) -> StreamResult | None:
    """What a stream render did before it was cancelled or failed (its
    timings), when the backend recorded it."""
    part = getattr(exc, "partial", None)
    return part if isinstance(part, StreamResult) else None


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        from .qwen import RenderCancelled

        raise RenderCancelled("cancelled")


def _feed(audio, sink: Sink, cancel: threading.Event | None, chunk_s: float) -> None:
    step = max(1, int(STREAM_RATE * chunk_s))
    for i in range(0, len(audio), step):
        _check_cancel(cancel)
        sink(audio[i:i + step])


def wav_duration(path: Path) -> tuple[float, int]:
    with wave.open(str(path), "rb") as w:
        rate = w.getframerate()
        return (w.getnframes() / float(rate) if rate else 0.0), rate


class SaySynth:
    """macOS `say`, rendered to a 16-bit WAV."""

    name = "say"

    def __init__(self, binary: str = "/usr/bin/say", timeout_s: float = 60.0):
        self.binary = binary
        self.timeout_s = timeout_s

    def synth(self, text: str, profile: Profile, attrs: dict[str, Any], out: Path) -> SynthResult:
        pace = float(attrs.get("pace") or profile.pace or 1.0)
        argv = [
            self.binary,
            "-o", str(out),
            "--file-format=WAVE",
            "--data-format=LEI16@22050",
            "-r", str(int(round(SAY_BASE_WPM * pace))),
        ]
        if profile.say_voice:
            argv += ["-v", profile.say_voice]
        t = time.perf_counter()
        # Text on stdin: never parsed as options.
        from afterword.hooks.tags import strip_speech_commands

        proc = subprocess.run(
            argv, input=strip_speech_commands(text).encode("utf-8"), capture_output=True, timeout=self.timeout_s
        )
        synth_s = time.perf_counter() - t
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            if profile.say_voice and "voice" in err.lower():
                # Unknown macOS voice: retry with the system voice.
                return self.synth(text, _without_say_voice(profile), attrs, out)
            raise RuntimeError(f"say failed ({proc.returncode}): {err[:300]}")
        duration, rate = wav_duration(out)
        return SynthResult(out, duration, synth_s, rate, backend="say")

    def stream(self, text, profile, attrs, sink, cancel=None) -> StreamResult:
        """Render at pace 1.0 to a temporary WAV, decode it to 24 kHz and
        feed it to the sink (the stream's time-stretch applies pace)."""
        from .stream import decode_to_stream_rate

        _check_cancel(cancel)
        fd, tmp = tempfile.mkstemp(prefix="afterword-say-", suffix=".wav")
        os.close(fd)
        result = None
        t_feed = None
        sent = [0]

        def counted(chunk):
            sink(chunk)
            sent[0] += len(chunk)

        try:
            try:
                result = self.synth(text, profile, {**attrs, "pace": 1.0}, Path(tmp))
                _check_cancel(cancel)
                audio = decode_to_stream_rate(Path(tmp))
            finally:
                Path(tmp).unlink(missing_ok=True)
            t_feed = time.perf_counter()
            _feed(audio, counted, cancel, SAY_CHUNK_S)
        except Exception as exc:
            if result is not None:  # rendered, then cancelled or cut off
                from .qwen import attach_partial

                fed_s = time.perf_counter() - t_feed if t_feed is not None else 0.0
                attach_partial(exc, StreamResult(
                    synth_s=result.synth_s + fed_s, audio_s=sent[0] / STREAM_RATE,
                    first_chunk_s=result.synth_s, backend="say",
                ))
            raise
        return StreamResult(
            synth_s=result.synth_s + (time.perf_counter() - t_feed), audio_s=len(audio) / STREAM_RATE,
            first_chunk_s=result.synth_s, backend="say",
        )


def _without_say_voice(profile: Profile) -> Profile:
    from dataclasses import replace

    return replace(profile, say_voice="")


class NullSynth:
    """Writes a short silent WAV. For tests and for running without audio."""

    name = "null"

    def __init__(self, seconds: float = 0.05):
        self.seconds = seconds

    def synth(self, text: str, profile: Profile, attrs: dict[str, Any], out: Path) -> SynthResult:
        t = time.perf_counter()
        rate = 16000
        frames = int(rate * self.seconds)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack("<h", 0) * frames)
        pace = float(attrs.get("pace") or profile.pace or 1.0)
        return SynthResult(out, frames / rate, time.perf_counter() - t, rate, playback_rate=pace, backend="null")

    def stream(self, text, profile, attrs, sink, cancel=None) -> StreamResult:
        import numpy as np

        t = time.perf_counter()
        audio = np.zeros(int(STREAM_RATE * self.seconds), np.float32)
        _feed(audio, sink, cancel, 1.0)
        return StreamResult(
            synth_s=time.perf_counter() - t, audio_s=len(audio) / STREAM_RATE, first_chunk_s=0.0, backend="null"
        )


class QwenSynth:
    """The local models via the shared QwenEngine (Qwen3-TTS, Kokoro,
    Chatterbox); `apple:` and `say:` voices are handed to their backends."""

    name = "qwen"

    def __init__(self, engine, clips):
        self.engine = engine
        self.clips = clips

    def _request(self, text: str, profile: Profile, attrs: dict[str, Any], spec):
        from . import qwen

        return qwen.build_request(
            text,
            spec,
            self.clips,
            lang=attrs.get("lang") or profile.lang,
            style=profile.style,
            mood=attrs.get("mood"),
            ref_text=profile.ref_text,
            seed=profile.seed,
            temperature=profile.temperature,
        )

    def synth(self, text: str, profile: Profile, attrs: dict[str, Any], out: Path) -> SynthResult:
        from . import qwen, voices

        spec = voices.parse_voice(profile.voice)
        if spec.kind == "say":
            from dataclasses import replace

            return SaySynth().synth(text, replace(profile, say_voice=spec.value), attrs, out)
        if spec.kind == "apple":
            from .apple import AppleSynth

            return AppleSynth().synth(text, profile, attrs, out)
        req = self._request(text, profile, attrs, spec)
        # The playback queue is never refused, and runs before lab work.
        result = self.engine.render(req, limit=False, priority=qwen.LIVE)
        duration = voices.write_wav(out, result.audio, result.sample_rate)
        pace = float(attrs.get("pace") or profile.pace or 1.0)
        return SynthResult(
            out, duration, result.synth_s, result.sample_rate,
            playback_rate=pace, load_s=result.load_s, seed=result.seed, model=result.model, backend=spec.backend,
        )

    def stream(self, text, profile, attrs, sink, cancel=None) -> StreamResult:
        """Stream a live render: chunks reach the sink on the engine thread
        as the model yields them. Skip/stop sets `cancel`: a queued job is
        dropped, a running one stops at its next chunk."""
        from . import qwen, voices
        from .stream import StreamResampler

        spec = voices.parse_voice(profile.voice)
        if spec.kind == "say":
            from dataclasses import replace

            return SaySynth().stream(text, replace(profile, say_voice=spec.value), attrs, sink, cancel)
        if spec.kind == "apple":
            from .apple import AppleSynth

            return AppleSynth().stream(text, profile, attrs, sink, cancel)
        req = self._request(text, profile, attrs, spec)
        resamplers: dict[int, StreamResampler] = {}

        def on_chunk(audio, rate):
            if rate == STREAM_RATE:
                sink(audio)
                return
            out = resamplers.setdefault(rate, StreamResampler(rate)).feed(audio)
            if out.size:
                sink(out)

        future = self.engine.submit_render(
            req, limit=False, priority=qwen.LIVE, cancel=cancel, on_chunk=on_chunk, stream=True
        )
        try:
            result = wait_future(future, cancel)
        except Exception as exc:
            part = getattr(exc, "partial", None)
            if isinstance(part, qwen.RenderResult):  # cancelled or failed part-way
                qwen.attach_partial(exc, self._stream_result(part, spec.backend))
            raise
        return self._stream_result(result, spec.backend)

    @staticmethod
    def _stream_result(result, backend: str = "qwen") -> StreamResult:
        return StreamResult(
            synth_s=result.synth_s, audio_s=result.audio.size / result.sample_rate,
            first_chunk_s=result.first_chunk_s, load_s=result.load_s, seed=result.seed,
            model=result.model, backend=backend,
        )


def wait_future(future: concurrent.futures.Future, cancel: threading.Event | None, poll_s: float = 0.05):
    """future.result(), but a set `cancel` drops a job that has not started
    (a running one notices the event itself)."""
    from .qwen import RenderCancelled

    while True:
        try:
            return future.result(timeout=poll_s)
        except concurrent.futures.CancelledError:
            raise RenderCancelled("cancelled") from None
        except concurrent.futures.TimeoutError:
            if cancel is not None and cancel.is_set() and future.cancel():
                raise RenderCancelled("cancelled") from None


def make_synth(name: str, engine=None, clips=None) -> Synth:
    name = (os.environ.get("AFTERWORD_SYNTH") or name or "say").lower()
    if name == "qwen":
        if engine is None or clips is None:
            raise ValueError("the qwen synth needs an engine")
        return QwenSynth(engine, clips)
    if name == "say":
        return SaySynth()
    if name == "null":
        return NullSynth()
    raise ValueError(f"unknown synth backend {name!r} (available: qwen, say, null)")
