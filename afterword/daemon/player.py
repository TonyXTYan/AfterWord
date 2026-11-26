"""Audio playback. Interruptible: stop() ends the current clip at once."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

# A clip that has not finished this long after its own length (at its rate)
# never will: the audio device or the player is stuck.
PLAY_SLACK_S = 10.0
PLAY_UNKNOWN_S = 60.0  # the bound when the clip's length cannot be read


class PlaybackTimeout(RuntimeError):
    """Playback did not finish within play_timeout_s and was stopped."""


def play_timeout_s(audio_s: float | None, rate: float = 1.0) -> float:
    """How long playing `audio_s` seconds at `rate` may take before it
    counts as stuck."""
    if audio_s is None or audio_s != audio_s or audio_s < 0:
        return PLAY_UNKNOWN_S
    return audio_s / max(float(rate or 1.0), 0.1) + PLAY_SLACK_S


def clip_seconds(path: Path) -> float | None:
    from .synth import wav_duration

    try:
        return wav_duration(path)[0]
    except Exception:
        return None


class AfplayPlayer:
    name = "afplay"

    def __init__(self, binary: str = "/usr/bin/afplay"):
        self.binary = binary
        self._proc: asyncio.subprocess.Process | None = None
        self._stopped = False

    async def play(self, path: Path, rate: float = 1.0) -> bool:
        """Play to the end. Returns False if interrupted by stop(). Raises
        PlaybackTimeout (after killing afplay) when it runs well past the
        clip's length.

        `rate` changes speed; afplay's rate-scaled playback keeps the pitch
        (Core Audio time-pitch), and -q 1 selects its higher quality mode.
        """
        self._stopped = False
        timeout = play_timeout_s(clip_seconds(path), rate)
        args = [str(path)]
        if abs(rate - 1.0) > 0.005:
            args = ["-r", f"{rate:.3f}", "-q", "1", str(path)]
        self._proc = await asyncio.create_subprocess_exec(
            self.binary, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        if self._stopped:  # stop() arrived while the process was spawning
            self._kill()
        proc = self._proc
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            self._kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 2.0)
            raise PlaybackTimeout(f"afplay did not finish within {timeout:.1f} s") from None
        except asyncio.CancelledError:
            self._kill()
            raise
        finally:
            self._proc = None
        if self._stopped:
            return False
        if proc.returncode != 0:
            raise RuntimeError(f"afplay exited {proc.returncode}: {err.decode('utf-8', 'replace')[:300]}")
        return True

    def stop(self) -> None:
        self._stopped = True
        self._kill()

    def _kill(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass


class NullPlayer:
    """Pretends to play for the clip's duration."""

    name = "null"

    def __init__(self):
        self._event = asyncio.Event()

    async def play(self, path: Path, rate: float = 1.0) -> bool:
        from .synth import wav_duration

        self._event = asyncio.Event()
        self.last_rate = rate
        try:
            duration, _ = wav_duration(path)
            duration /= max(rate, 0.1)
        except Exception:
            duration = 0.0
        try:
            await asyncio.wait_for(self._event.wait(), timeout=duration)
            return False
        except asyncio.TimeoutError:
            return True

    def stop(self) -> None:
        self._event.set()


def make_player(name: str):
    name = (os.environ.get("AFTERWORD_PLAYER") or name or "afplay").lower()
    if name == "afplay":
        return AfplayPlayer()
    if name == "null":
        return NullPlayer()
    raise ValueError(f"unknown player {name!r} (available: afplay, null)")
