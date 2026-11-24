"""Pitch (F0) of a rendered take, for the voice lab's seed explorer.

A plain autocorrelation estimate on audio decimated to <= 16 kHz: 40 ms
frames every 10 ms, the lag of the strongest periodicity between 70 and
400 Hz, voiced frames only (loud enough and clearly periodic). Good enough to sort takes into "lower" and "higher"
voices and to see drift between renders; not a phonetics tool.

numpy comes with mlx-audio, not with AfterWord itself: without it every
value is None.
"""

from __future__ import annotations

from typing import Any

F0_MIN = 70.0
F0_MAX = 400.0
FRAME_S = 0.040
HOP_S = 0.010
VOICED_CORR = 0.5  # normalised autocorrelation peak a voiced frame needs
LOUD_DB = -35.0  # frames quieter than this, relative to the loudest, are skipped
MIN_VOICED = 5  # fewer voiced frames than this: no estimate
MAX_SECONDS = 120.0  # only the first two minutes are analysed
ANALYSIS_MAX_SR = 16000  # audio is decimated to at most this rate first

EMPTY: dict[str, Any] = {"f0_hz": None, "f0_spread_hz": None, "voiced_s": None}


def f0_stats(audio: Any, sample_rate: int) -> dict[str, Any]:
    """{"f0_hz": median F0, "f0_spread_hz": 90th minus 10th percentile,
    "voiced_s": seconds of voiced frames}; None values when there is no
    estimate (numpy missing, silence, noise, too short)."""
    try:
        import numpy as np
    except ImportError:
        return dict(EMPTY)
    try:
        return _f0_stats(np, audio, int(sample_rate))
    except Exception:  # never let a metric break a render
        return dict(EMPTY)


def analysis_step(sr: int) -> int:
    """Decimation factor that brings `sr` to at most ANALYSIS_MAX_SR (24 kHz
    Qwen audio is analysed at 12 kHz, 44.1/48 kHz at 14.7/16 kHz): plenty for
    70-400 Hz and cheaper. Each step averages `step` samples (a crude
    low-pass; the voice's harmonics that matter sit far below the new
    Nyquist)."""
    return max(1, -(-int(sr) // ANALYSIS_MAX_SR))


def _f0_stats(np, audio: Any, sr: int) -> dict[str, Any]:
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    if sr <= 0 or x.size == 0:
        return dict(EMPTY)
    x = x[: int(MAX_SECONDS * sr)]
    step = analysis_step(sr)
    if step > 1:
        x = x[: x.size - x.size % step].reshape(-1, step).mean(axis=1)
        sr = sr // step
    n = int(FRAME_S * sr)
    hop = int(HOP_S * sr)
    if x.size < n or n < 8:
        return dict(EMPTY)
    count = 1 + (x.size - n) // hop
    idx = np.arange(n)[None, :] + hop * np.arange(count)[:, None]
    frames = x[idx]
    frames = frames - frames.mean(axis=1, keepdims=True)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    peak = float(rms.max())
    if peak <= 1e-6:
        return dict(EMPTY)
    loud = rms >= peak * 10 ** (LOUD_DB / 20)
    frames = frames[loud]
    if frames.shape[0] < MIN_VOICED:
        return dict(EMPTY)
    frames = frames * np.hanning(n)[None, :]
    size = 1 << int(np.ceil(np.log2(2 * n)))
    spec = np.fft.rfft(frames, size, axis=1)
    ac = np.fft.irfft(spec * np.conj(spec), size, axis=1)[:, :n]
    # Unbiased (per overlapping sample), normalised to lag 0.
    ac = ac / (n - np.arange(n))[None, :]
    ac0 = ac[:, :1]
    ac = np.where(ac0 > 0, ac / np.where(ac0 > 0, ac0, 1.0), 0.0)
    lo = max(2, int(sr / F0_MAX))
    hi = min(n - 2, int(np.ceil(sr / F0_MIN)))
    if hi <= lo + 2:
        return dict(EMPTY)
    f0s = []
    for row in ac:
        seg = row[lo:hi + 1]
        best = float(seg.max())
        if best < VOICED_CORR:
            continue
        # The smallest lag that is a local peak close to the best one: a
        # period's multiples score as high, so this avoids octave-down errors.
        k = None
        for j in range(1, seg.size - 1):
            if seg[j] >= 0.85 * best and seg[j] >= seg[j - 1] and seg[j] >= seg[j + 1]:
                k = j
                break
        if k is None:
            k = int(seg.argmax())
            if k == 0 or k == seg.size - 1:
                continue
        a, b, c = seg[k - 1], seg[k], seg[k + 1]
        denom = a - 2 * b + c
        shift = 0.5 * (a - c) / denom if denom < 0 else 0.0
        lag = lo + k + max(-0.5, min(0.5, shift))
        f0 = sr / lag
        if F0_MIN <= f0 <= F0_MAX:
            f0s.append(f0)
    if len(f0s) < MIN_VOICED:
        return dict(EMPTY)
    arr = np.asarray(f0s)
    p10, p50, p90 = np.percentile(arr, [10, 50, 90])
    return {
        "f0_hz": round(float(p50), 1),
        "f0_spread_hz": round(float(p90 - p10), 1),
        "voiced_s": round(len(f0s) * HOP_S, 2),
    }


def f0_of_wav(path: Any) -> dict[str, Any]:
    """f0_stats of a 16-bit PCM WAV file (the first channel); None values when
    it cannot be read."""
    try:
        import wave

        import numpy as np

        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2:
                return dict(EMPTY)
            channels = w.getnchannels()
            rate = w.getframerate()
            frames = w.readframes(min(w.getnframes(), int(MAX_SECONDS * rate)))
        audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        if channels > 1:
            audio = audio[: audio.size - audio.size % channels].reshape(-1, channels)[:, 0]
        return f0_stats(audio, rate)
    except Exception:
        return dict(EMPTY)
