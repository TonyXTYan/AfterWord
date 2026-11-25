"""Streaming playback: PCM chunks go to the audio device while the line is
still being rendered.

- `Playback` is one queue item's stream. The daemon writes raw 24 kHz mono
  float32 chunks into it (from any thread); a player thread owned by the
  playback time-stretches them for pace (`TimeStretch`, WSOLA) and fills a
  buffer that the device callback drains. Playback starts once about
  `PREBUFFER_S` of audio is buffered (or the input has ended), and
  `abort()` stops it at once.
- `StreamPlayer` opens a sounddevice (PortAudio) `OutputStream` per item.
- `NullStreamPlayer` has a fake device that consumes audio in real time and
  records every chunk it was fed (tests, AFTERWORD_PLAYER=null).
- `make_stream_player()` returns None (and why) when sounddevice or numpy
  cannot be imported; the daemon then uses the v1 path (whole WAV, afplay).

numpy is imported lazily: it comes with the optional qwen extra.
"""

from __future__ import annotations

import collections
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("afterword.stream")

SAMPLE_RATE = 24000
PREBUFFER_S = 0.3
BLOCK = 480  # 20 ms device callback blocks
OPEN_TIMEOUT_S = 5.0
STALL_S = 3.0  # no device callback for this long while playing: the device is gone
PACE_MIN, PACE_MAX = 0.5, 2.0


class PlayerUnavailable(RuntimeError):
    """The audio device could not be opened (or sounddevice is missing)."""


def clamp_pace(rate: float) -> float:
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return 1.0
    if rate != rate:  # NaN
        return 1.0
    return min(PACE_MAX, max(PACE_MIN, rate))


# --------------------------------------------------------------------------- #
# Time-stretch                                                                  #
# --------------------------------------------------------------------------- #


class TimeStretch:
    """Streaming WSOLA (waveform-similarity overlap-add) time-stretch.

    `rate` > 1 is faster: about len(input) / rate samples come out, at the
    same pitch. Frames of 40 ms with 50 % overlap (periodic Hann, so the
    windows sum to one); each frame's start is moved by up to +-10 ms to the
    position whose waveform best continues the previous frame. Feed chunks of
    any size with `process()`; `flush()` returns the tail.
    """

    def __init__(self, rate: float, sample_rate: int = SAMPLE_RATE):
        import numpy as np

        self._np = np
        self.rate = clamp_pace(rate)
        self.n = max(64, int(sample_rate * 0.040) // 2 * 2)
        self.hs = self.n // 2
        self.tol = max(8, int(sample_rate * 0.010))
        k = np.arange(self.n)
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * k / self.n)).astype(np.float32)
        self._in = np.zeros(0, np.float32)
        self._in_off = 0  # absolute input index of self._in[0]
        self._acc = np.zeros(0, np.float32)
        self._ws = np.zeros(0, np.float32)
        self._out_off = 0  # absolute output index of self._acc[0]
        self._k = 0  # next frame
        self._prev: int | None = None  # input start of the previous frame
        self._total_in = 0
        self._done = False

    def _pos(self, k: int) -> int:
        return int(round(k * self.hs * self.rate))

    def _get(self, start: int, n: int):
        """Input samples [start, start + n), zeros outside what we have."""
        np = self._np
        out = np.zeros(n, np.float32)
        lo = max(start, self._in_off)
        hi = min(start + n, self._in_off + len(self._in))
        if hi > lo:
            out[lo - start:hi - start] = self._in[lo - self._in_off:hi - self._in_off]
        return out

    def _target(self) -> int:
        return int(round(self._total_in / self.rate))

    def process(self, x) -> Any:
        np = self._np
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self._done:
            raise RuntimeError("TimeStretch already flushed")
        if x.size:
            self._in = np.concatenate([self._in, x])
            self._total_in += x.size
        self._frames(final=False)
        return self._emit(final=False)

    def flush(self) -> Any:
        if self._done:
            return self._np.zeros(0, self._np.float32)
        self._frames(final=True)
        self._done = True
        return self._emit(final=True)

    def _frames(self, final: bool) -> None:
        np = self._np
        n, hs, tol = self.n, self.hs, self.tol
        avail_end = self._in_off + len(self._in)
        while True:
            k = self._k
            out_start = k * hs
            if final and out_start >= self._target():
                break
            p = self._pos(k)
            need_end = p + tol + n
            if self._prev is not None:
                need_end = max(need_end, self._prev + hs + n)
            if not final and need_end > avail_end:
                break
            s = p
            if self._prev is not None:
                tmpl = self._get(self._prev + hs, n)
                if float(np.dot(tmpl, tmpl)) > 1e-9:
                    lo = max(0, p - tol)
                    region = self._get(lo, p + tol + n - lo)
                    c = np.correlate(region, tmpl, mode="valid")
                    if c.size:
                        s = lo + int(np.argmax(c))
            frame = self._get(s, n) * self.win
            rel = out_start - self._out_off
            grow = rel + n - len(self._acc)
            if grow > 0:
                self._acc = np.concatenate([self._acc, np.zeros(grow, np.float32)])
                self._ws = np.concatenate([self._ws, np.zeros(grow, np.float32)])
            self._acc[rel:rel + n] += frame
            self._ws[rel:rel + n] += self.win
            self._prev = s
            self._k += 1
            # Input before both the next frame's search window and the
            # current frame's continuation is never read again.
            keep_from = min(self._pos(self._k) - tol, s + hs)
            drop = keep_from - self._in_off
            if drop > 0:
                self._in = self._in[drop:]
                self._in_off += drop

    def _emit(self, final: bool):
        np = self._np
        if final:
            end = self._target()
        else:
            end = self._k * self.hs  # the next frame starts here: all before is final
        count = max(0, end - self._out_off)
        acc, ws = self._acc[:count], self._ws[:count]
        if len(acc) < count:  # only on flush with very short input
            acc = np.concatenate([acc, np.zeros(count - len(acc), np.float32)])
            ws = np.concatenate([ws, np.zeros(count - len(ws), np.float32)])
        out = np.where(ws > 1e-3, acc / np.maximum(ws, 1e-3), 0.0).astype(np.float32)
        self._acc = self._acc[count:]
        self._ws = self._ws[count:]
        self._out_off += count
        return out


# --------------------------------------------------------------------------- #
# Decoding (say segments)                                                       #
# --------------------------------------------------------------------------- #


def decode_to_stream_rate(path: Path):
    """Decode an audio file to mono float32 at SAMPLE_RATE: miniaudio when
    installed, else the wave module and linear interpolation."""
    import numpy as np

    try:
        import miniaudio
    except ImportError:
        miniaudio = None
    if miniaudio is not None:
        decoded = miniaudio.decode_file(
            str(path), output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=SAMPLE_RATE
        )
        return np.asarray(decoded.samples, dtype=np.float32).reshape(-1)
    import wave

    with wave.open(str(path), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"unsupported WAV sample width {width}")
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        arr = arr.reshape(-1, channels).mean(axis=1)
    return resample(arr, rate)


def resample(audio, rate: int):
    """Linear-interpolation resample to SAMPLE_RATE (good enough for `say`)."""
    import numpy as np

    audio = np.asarray(audio, dtype=np.float32).reshape(-1)
    if rate == SAMPLE_RATE or audio.size == 0:
        return audio
    n_out = int(round(audio.size * SAMPLE_RATE / rate))
    x_out = np.arange(n_out, dtype=np.float64) * (rate / SAMPLE_RATE)
    return np.interp(x_out, np.arange(audio.size), audio).astype(np.float32)


class StreamResampler:
    """`resample` for a stream of chunks: linear interpolation that carries
    its position across chunks, so chunk boundaries leave no seams."""

    def __init__(self, rate: int):
        self.step = float(rate) / SAMPLE_RATE
        self.pos = 0.0  # next output position, in input samples from `prev`
        self.prev = None  # last input sample of the previous chunk

    def feed(self, audio):
        import numpy as np

        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        if self.step == 1.0 or x.size == 0:
            return x
        buf = x if self.prev is None else np.concatenate([np.array([self.prev], np.float32), x])
        last = buf.size - 1
        if self.pos > last:
            self.pos -= last
            self.prev = float(buf[last])
            return np.zeros(0, np.float32)
        k = int((last - self.pos) // self.step) + 1
        xs = self.pos + np.arange(k, dtype=np.float64) * self.step
        out = np.interp(xs, np.arange(buf.size), buf).astype(np.float32)
        self.pos = xs[-1] + self.step - last
        self.prev = float(buf[last])
        return out


# --------------------------------------------------------------------------- #
# One item's stream                                                             #
# --------------------------------------------------------------------------- #


class Playback:
    """One queue item's audio stream (see the module docstring).

    `opener(callback)` returns a started-on-demand output stream with
    `start()`, `stop()`, `abort()`, `close()` and `latency`. Callbacks
    `on_first_audio()` and `on_done()` run on the player thread.
    """

    def __init__(
        self,
        opener: Callable[[Callable], Any],
        rate: float = 1.0,
        *,
        prebuffer_s: float = PREBUFFER_S,
        on_first_audio: Callable[[], None] | None = None,
        on_done: Callable[[], None] | None = None,
        record: bool = False,
        name: str = "stream",
    ):
        self._opener = opener
        self._in: queue.Queue = queue.Queue()
        self._buf: collections.deque = collections.deque()
        self._head = 0  # samples of self._buf[0] already played
        self._buffered = 0
        self._lock = threading.Lock()
        self._ended = False
        self._drained = threading.Event()
        self._aborted = threading.Event()
        self._opened = threading.Event()
        self.done = threading.Event()
        self._prebuffer = int(prebuffer_s * SAMPLE_RATE)
        self._on_first_audio = on_first_audio
        self._on_done = on_done
        self._notified_first = False
        self._stretch: TimeStretch | None = None
        self.rate = clamp_pace(rate)
        self.record = record
        self.fed: list = []  # raw chunks in order (record=True only)
        self.played: list = []  # what the device was given (record=True only)
        self.error: str | None = None
        self.first_audio_at: float | None = None  # wall time of the first sample out
        self.started_at: float | None = None
        self.ended_at: float | None = None
        self.played_samples = 0
        self.underruns = 0
        self.latency = 0.0
        self._last_cb = 0.0
        self._started = False
        self._in.put(("rate", self.rate))
        self._thread = threading.Thread(target=self._run, name=f"player-{name}", daemon=True)
        self._thread.start()

    # -- producer side (any thread) ------------------------------------------

    def wait_open(self, timeout: float | None = None) -> None:
        if not self._opened.wait(OPEN_TIMEOUT_S if timeout is None else timeout):
            self.abort()
            raise PlayerUnavailable("timed out opening the audio device")
        if self.error is not None:
            raise PlayerUnavailable(self.error)

    def begin_segment(self, rate: float) -> None:
        """Later chunks are stretched at this rate."""
        self._in.put(("rate", clamp_pace(rate)))

    def write(self, chunk) -> None:
        if self._aborted.is_set() or self.done.is_set():
            return
        self._in.put(("audio", chunk))

    def finish(self) -> None:
        """No more chunks: play out what is buffered, then stop."""
        self._in.put(("end", None))

    def abort(self) -> None:
        self._aborted.set()
        self._in.put(("abort", None))

    @property
    def aborted(self) -> bool:
        return self._aborted.is_set()

    @property
    def completed(self) -> bool:
        """Played to the end (not aborted, no device error)."""
        return self.done.is_set() and not self._aborted.is_set() and self.error is None

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)

    @property
    def played_s(self) -> float:
        return self.played_samples / SAMPLE_RATE

    # -- player thread --------------------------------------------------------

    def _push(self, audio) -> None:
        if audio is None or len(audio) == 0:
            return
        if self.record:
            self.played.append(audio)
        with self._lock:
            self._buf.append(audio)
            self._buffered += len(audio)

    def _set_rate(self, rate: float) -> None:
        if self._stretch is not None:
            self._push(self._stretch.flush())
        self.rate = rate
        self._stretch = TimeStretch(rate) if abs(rate - 1.0) > 0.005 else None

    def _maybe_notify_first(self) -> None:
        if not self._notified_first and self.first_audio_at is not None:
            self._notified_first = True
            if self._on_first_audio is not None:
                try:
                    self._on_first_audio()
                except Exception:
                    log.debug("on_first_audio failed", exc_info=True)

    def _stalled(self) -> bool:
        if self._started and not self._drained.is_set() and time.monotonic() - self._last_cb > STALL_S:
            self.error = "the audio device stopped responding"
            return True
        return False

    def _run(self) -> None:
        stream = None
        try:
            try:
                stream = self._opener(self._callback)
                lat = getattr(stream, "latency", 0.0)
                self.latency = float(lat[1] if isinstance(lat, (tuple, list)) else lat or 0.0)
            except Exception as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                return
            finally:
                self._opened.set()
            self._feed_loop(stream)
            while self._ended and not self._aborted.is_set() and not self._drained.is_set():
                self._drained.wait(0.05)
                self._maybe_notify_first()
                if self._stalled():
                    break
            self._maybe_notify_first()
            if self._started:
                if self._aborted.is_set() or self.error:
                    stream.abort()
                else:
                    # Let the device play its last buffers before stopping.
                    time.sleep(min(max(self.latency, 0.0), 0.5))
                    stream.stop()
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}: {exc}"
            log.warning("audio stream failed: %s", self.error)
            with _suppress():
                if stream is not None:
                    stream.abort()
        finally:
            if stream is not None:
                with _suppress():
                    stream.close()
            self.ended_at = time.time()
            self.done.set()
            if self._on_done is not None:
                try:
                    self._on_done()
                except Exception:
                    log.debug("on_done failed", exc_info=True)

    def _feed_loop(self, stream) -> None:
        while not self._aborted.is_set():
            try:
                kind, value = self._in.get(timeout=0.05)
            except queue.Empty:
                kind, value = None, None
            if kind == "abort":
                return
            if kind == "audio":
                if self.record:
                    self.fed.append(value)
                self._push(self._stretch.process(value) if self._stretch is not None else value)
            elif kind == "rate":
                self._set_rate(value)
            elif kind == "end":
                if self._stretch is not None:
                    self._push(self._stretch.flush())
                    self._stretch = None
                with self._lock:
                    self._ended = True
                    if self._buffered == 0:
                        self._drained.set()
            if not self._started and (self._buffered >= self._prebuffer or self._ended):
                if self._ended and self._buffered == 0:
                    return  # nothing to play
                self._last_cb = time.monotonic()
                self.started_at = time.time()
                stream.start()
                self._started = True
            self._maybe_notify_first()
            if self._ended or self._stalled():
                return

    def _callback(self, outdata, frames, time_info, status) -> None:
        """Device thread: copy buffered samples out; zeros when empty."""
        self._last_cb = time.monotonic()
        out = outdata[:, 0] if getattr(outdata, "ndim", 1) == 2 else outdata
        n = 0
        with self._lock:
            while n < frames and self._buf:
                head = self._buf[0]
                take = min(frames - n, len(head) - self._head)
                out[n:n + take] = head[self._head:self._head + take]
                n += take
                self._head += take
                if self._head >= len(head):
                    self._buf.popleft()
                    self._head = 0
            self._buffered -= n
            ended = self._ended
            empty = self._buffered <= 0
        if n < frames:
            out[n:] = 0
            if not ended and self.played_samples > 0:
                self.underruns += 1
        if n and self.first_audio_at is None:
            self.first_audio_at = time.time() + self.latency
        self.played_samples += n
        if ended and empty:
            self._drained.set()


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


# --------------------------------------------------------------------------- #
# Players                                                                       #
# --------------------------------------------------------------------------- #


class StreamPlayer:
    """sounddevice / PortAudio output, one OutputStream per queue item."""

    name = "stream"

    def __init__(self, sd):
        self._sd = sd
        self._pa_lock = threading.Lock()  # PortAudio calls are not thread-safe
        self._playbacks: list[Playback] = []  # every Playback whose thread may still use PortAudio
        self.last: Playback | None = None

    def _opener(self, callback, refresh: bool):
        with self._pa_lock:
            if refresh and not any(not pb.done.is_set() for pb in self._others()):
                # PortAudio fixes its device list (and the default output)
                # when it initialises; re-initialising (~2 ms) follows a
                # default device change, e.g. headphones plugged in while
                # the daemon runs. Never while another of our streams (one
                # still open, or an open that timed out but is still inside
                # PortAudio) could be using it.
                terminate, initialize = getattr(self._sd, "_terminate", None), getattr(self._sd, "_initialize", None)
                if terminate is not None and initialize is not None:
                    try:
                        terminate()
                        initialize()
                    except Exception:
                        log.debug("PortAudio re-initialise failed", exc_info=True)
            return self._sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                latency="low", callback=callback,
            )

    def _others(self) -> list[Playback]:
        me = threading.current_thread()
        return [pb for pb in self._playbacks if pb._thread is not me]

    def open(self, rate: float = 1.0, **kw) -> Playback:
        """Open the device (blocking; call from a worker thread). Raises
        PlayerUnavailable when it cannot be opened."""
        self._playbacks = [pb for pb in self._playbacks if not pb.done.is_set()]
        # Refresh the device list only when no stream of ours is in flight
        # (checked again under the lock when the open runs).
        refresh = not self._playbacks
        pb = Playback(lambda cb: self._opener(cb, refresh), rate, name=self.name, **kw)
        self._playbacks.append(pb)
        pb.wait_open()  # on a timeout pb stays tracked until its thread ends
        self.last = pb
        return pb


class _NullOutput:
    """A fake output device: calls the callback in real time (scaled by
    `speed`), from its own thread, and throws the audio away."""

    def __init__(self, callback, speed: float = 1.0):
        self._callback = callback
        self._speed = speed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.latency = 0.0

    def start(self):
        import numpy as np

        def run():
            block = np.zeros((BLOCK, 1), np.float32)
            period = BLOCK / SAMPLE_RATE / self._speed
            deadline = time.monotonic()
            while not self._stop.is_set():
                self._callback(block, BLOCK, None, None)
                deadline += period
                delay = deadline - time.monotonic()
                if delay > 0:
                    self._stop.wait(delay)

        self._thread = threading.Thread(target=run, name="null-audio", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)

    abort = stop

    def close(self):
        self.stop()


class NullStreamPlayer:
    """No audio device: consumes audio in real time and records what it was
    fed (`last.fed`: raw chunks; `last.played`: after the time-stretch)."""

    name = "null"

    def __init__(self, speed: float = 1.0, fail_open: str | None = None):
        self.speed = speed
        self.fail_open = fail_open
        self.last: Playback | None = None
        self.opened = 0

    def _opener(self, callback):
        if self.fail_open:
            raise OSError(self.fail_open)
        return _NullOutput(callback, self.speed)

    def open(self, rate: float = 1.0, **kw) -> Playback:
        self.opened += 1
        pb = Playback(self._opener, rate, record=True, name=self.name, **kw)
        pb.wait_open()
        self.last = pb
        return pb


def make_stream_player(name: str) -> tuple[Any | None, str | None]:
    """(player, None), or (None, why streaming is not available)."""
    if name == "null":
        try:
            import numpy  # noqa: F401
        except ImportError as exc:
            return None, f"numpy is not installed ({exc})"
        return NullStreamPlayer(), None
    try:
        import numpy  # noqa: F401
        import sounddevice
    except Exception as exc:  # ImportError, or OSError when PortAudio is missing
        return None, f"sounddevice is not available ({type(exc).__name__}: {exc})"
    return StreamPlayer(sounddevice), None
