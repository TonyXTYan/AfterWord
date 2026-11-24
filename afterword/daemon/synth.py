"""Speech synthesis backends.

Each backend renders text to a WAV file. Synthesis is blocking and is run in
a worker thread by the server. The Qwen3-TTS (mlx-audio) backend is the next
build step; until then the macOS `say` backend renders the audio, so the
whole pipeline (queue, timings, history, playback control) is real.
"""

from __future__ import annotations

import os
import struct
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import Profile

SAY_BASE_WPM = 175  # `say`'s default rate; pace 1.0 maps to this


@dataclass
class SynthResult:
    path: Path
    duration_s: float
    synth_s: float
    sample_rate: int

    @property
    def rtf(self) -> float | None:
        """Real-time factor: synthesis time / audio duration (<1 is faster
        than real time)."""
        return self.synth_s / self.duration_s if self.duration_s > 0 else None


class Synth(Protocol):
    name: str

    def synth(self, text: str, profile: Profile, attrs: dict[str, Any], out: Path) -> SynthResult: ...


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
        return SynthResult(out, duration, synth_s, rate)


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
        return SynthResult(out, frames / rate, time.perf_counter() - t, rate)


def make_synth(name: str) -> Synth:
    name = (os.environ.get("AFTERWORD_SYNTH") or name or "say").lower()
    if name == "say":
        return SaySynth()
    if name == "null":
        return NullSynth()
    raise ValueError(f"unknown synth backend {name!r} (available: say, null)")
