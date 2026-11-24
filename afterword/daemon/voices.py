"""Voice specs, languages, speakers, and the reference-clip store.

A profile's `voice` string picks where the voice comes from:

    builtin:<Speaker>      Qwen3-TTS CustomVoice preset speaker
    clone:<clip-name>      Qwen3-TTS Base, cloned from ~/.afterword/voices/<clip-name>.wav
    clone:<path>           Qwen3-TTS Base, cloned from that file (absolute or ~)
    design:<description>   Qwen3-TTS VoiceDesign, voice described in words
    say:<MacVoice>         macOS `say` (no model)
"""

from __future__ import annotations

import io
import json
import os
import re
import struct
import subprocess
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afterword.hooks.tags import PROFILE_NAME_RE

KINDS = ("builtin", "clone", "design", "say")
MODEL_FOR_KIND = {"builtin": "custom_voice", "clone": "base", "design": "voice_design"}

# ISO 639-1 -> Qwen3-TTS language name.
ISO_TO_QWEN = {
    "en": "english",
    "zh": "chinese",
    "ja": "japanese",
    "ko": "korean",
    "de": "german",
    "fr": "french",
    "es": "spanish",
    "it": "italian",
    "pt": "portuguese",
    "ru": "russian",
}
QWEN_LANGUAGES = tuple(ISO_TO_QWEN.values())

# Speaker metadata the model config does not carry (from the Qwen3-TTS docs).
SPEAKER_NATIVE = {
    "serena": "chinese",
    "vivian": "chinese",
    "uncle_fu": "chinese",
    "dylan": "chinese",
    "eric": "chinese",
    "ryan": "english",
    "aiden": "english",
    "ono_anna": "japanese",
    "sohee": "korean",
}

# The same sentence in several languages, for checking that a voice keeps
# its accent across languages.
SAMPLES = {
    "en": "I've finished the refactor, and all the tests pass now.",
    "ja": "リファクタリングが終わって、テストもすべて通りました。",
    "zh": "重构已经完成了，所有测试现在都通过了。",
    "es": "Terminé la refactorización y ahora pasan todas las pruebas.",
    "fr": "J'ai fini la refactorisation, et tous les tests passent maintenant.",
    "de": "Ich bin mit dem Refactoring fertig, und alle Tests laufen jetzt durch.",
    "ko": "리팩터링을 마쳤고, 이제 모든 테스트가 통과합니다.",
}

CLIP_NAME_RE = PROFILE_NAME_RE
CLIP_MAX_BYTES = 20 * 1024 * 1024
CLIP_MAX_SECONDS = 60.0
CLIP_MIN_SECONDS = 1.0
CLIP_SAMPLE_RATE = 24000


class VoiceError(ValueError):
    """A voice spec or clip that cannot be used. The message is user-facing."""


@dataclass(frozen=True)
class VoiceSpec:
    kind: str  # builtin | clone | design | say
    value: str  # speaker, clip name/path, description, or mac voice

    @property
    def model(self) -> str | None:
        return MODEL_FOR_KIND.get(self.kind)

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def parse_voice(spec: str | None) -> VoiceSpec:
    if not spec or not isinstance(spec, str) or ":" not in spec:
        raise VoiceError(
            f"voice {spec!r} is not a voice spec; use builtin:<Speaker>, clone:<clip>, "
            "design:<description> or say:<MacVoice>"
        )
    kind, _, value = spec.partition(":")
    kind = kind.strip().lower()
    value = value.strip()
    if kind not in KINDS:
        raise VoiceError(f"unknown voice kind {kind!r} in {spec!r} (use {', '.join(KINDS)})")
    if kind in ("builtin", "clone", "design") and not value:
        raise VoiceError(f"voice {spec!r} needs a value after '{kind}:'")
    if kind == "design" and len(value) > 1000:
        raise VoiceError("voice description is too long (max 1000 characters)")
    return VoiceSpec(kind, value)


def language_name(lang: str | None) -> str:
    """ISO code, Qwen language name, or 'auto' -> Qwen language name/'auto'."""
    if not lang:
        return "auto"
    value = lang.strip().lower().replace("_", "-")
    if value in ("auto", ""):
        return "auto"
    if value in QWEN_LANGUAGES:
        return value
    base = value.split("-", 1)[0]
    return ISO_TO_QWEN.get(base, "auto")


def compose_instruct(*parts: str | None) -> str | None:
    """Join style/mood/description fragments into one instruction."""
    cleaned = [" ".join(p.split()).rstrip(".") for p in parts if p and p.strip()]
    return ". ".join(cleaned) + "." if cleaned else None


# --------------------------------------------------------------------------- #
# macOS say voices                                                            #
# --------------------------------------------------------------------------- #

_SAY_LINE_RE = re.compile(r"^(?P<name>.+?)\s+(?P<locale>[a-z]{2,3}_[A-Za-z0-9]+)\s+#")
_say_cache: list[dict[str, str]] | None = None


def say_voices() -> list[dict[str, str]]:
    global _say_cache
    if _say_cache is None:
        try:
            out = subprocess.run(["/usr/bin/say", "-v", "?"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        voices = []
        for line in out.splitlines():
            m = _SAY_LINE_RE.match(line)
            if m:
                voices.append({"name": m.group("name").strip(), "locale": m.group("locale")})
        _say_cache = voices
    return list(_say_cache)


# --------------------------------------------------------------------------- #
# Reference clips                                                             #
# --------------------------------------------------------------------------- #


class ClipStore:
    """~/.afterword/voices/<name>.wav plus <name>.txt (the transcript)."""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.Lock()  # one writer at a time: check-then-write stays atomic

    def _check_name(self, name: str) -> str:
        if not isinstance(name, str) or not CLIP_NAME_RE.fullmatch(name):
            raise VoiceError("clip name must be 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
        return name

    def wav_path(self, name: str) -> Path:
        return self.root / f"{self._check_name(name)}.wav"

    def exists(self, name: str) -> bool:
        return self.wav_path(name).is_file()

    def transcript(self, name: str) -> str:
        try:
            return (self.root / f"{self._check_name(name)}.txt").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def info(self, name: str) -> dict[str, Any]:
        path = self.wav_path(name)
        st = path.stat()
        try:
            with wave.open(str(path), "rb") as w:
                duration = w.getnframes() / float(w.getframerate() or 1)
        except (wave.Error, EOFError, OSError):
            duration = None
        return {
            "name": name,
            "url": f"/api/lab/clips/{name}",
            "duration_s": round(duration, 2) if duration is not None else None,
            "ref_text": self.transcript(name),
            "created": st.st_mtime,
        }

    def list(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.glob("*.wav")):
            if CLIP_NAME_RE.fullmatch(p.stem):
                try:
                    out.append(self.info(p.stem))
                except OSError:
                    continue
        return out

    def resolve(self, value: str) -> tuple[Path, str]:
        """A clone spec value -> (audio path, saved transcript)."""
        if "/" in value or value.startswith("~"):
            path = Path(value).expanduser()
            if not path.is_file():
                raise VoiceError(f"clone clip {value!r} not found")
            sidecar = path.with_suffix(".txt")
            ref = sidecar.read_text(encoding="utf-8").strip() if sidecar.is_file() else ""
            return path, ref
        if not self.exists(value):
            raise VoiceError(f"no reference clip named {value!r} (add one in the voice lab)")
        return self.wav_path(value), self.transcript(value)

    def add(self, name: str, data: bytes, ref_text: str = "", overwrite: bool = False) -> dict[str, Any]:
        self._check_name(name)
        if self.exists(name) and not overwrite:
            raise FileExistsError(name)
        if not data:
            raise VoiceError("empty upload")
        if len(data) > CLIP_MAX_BYTES:
            raise VoiceError(f"clip is larger than {CLIP_MAX_BYTES // (1024 * 1024)} MB")
        pcm = decode_to_pcm16(data)
        seconds = len(pcm) / 2 / CLIP_SAMPLE_RATE
        if seconds < CLIP_MIN_SECONDS:
            raise VoiceError(f"clip is too short ({seconds:.1f} s; need at least {CLIP_MIN_SECONDS:.0f} s)")
        if seconds > CLIP_MAX_SECONDS:
            raise VoiceError(f"clip is too long ({seconds:.0f} s; max {CLIP_MAX_SECONDS:.0f} s)")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock:
            if self.exists(name) and not overwrite:
                raise FileExistsError(name)
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".wav", dir=str(self.root))
            try:
                with os.fdopen(fd, "wb") as fh, wave.open(fh, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(CLIP_SAMPLE_RATE)
                    w.writeframes(pcm)
                os.replace(tmp, self.wav_path(name))
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            self.set_transcript(name, ref_text)
            return self.info(name)

    def set_transcript(self, name: str, ref_text: str) -> None:
        path = self.root / f"{self._check_name(name)}.txt"
        ref_text = (ref_text or "").strip()[:2000]
        if ref_text:
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".txt", dir=str(self.root))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(ref_text + "\n")
            os.replace(tmp, path)
        elif path.exists():
            path.unlink()

    def delete(self, name: str) -> bool:
        path = self.wav_path(name)
        with self._lock:
            if not path.is_file():
                return False
            path.unlink()
            (self.root / f"{name}.txt").unlink(missing_ok=True)
            return True


def decode_to_pcm16(data: bytes) -> bytes:
    """Decode WAV/MP3/FLAC/Vorbis bytes to mono 16-bit PCM at 24 kHz.

    Uses miniaudio (installed with the `qwen` extra). Without it only WAV is
    accepted, and it must already be mono 16-bit 24 kHz.
    """
    try:
        import miniaudio
    except ImportError:
        miniaudio = None
    if miniaudio is not None:
        try:
            decoded = miniaudio.decode(
                data,
                output_format=miniaudio.SampleFormat.SIGNED16,
                nchannels=1,
                sample_rate=CLIP_SAMPLE_RATE,
            )
        except miniaudio.DecodeError as exc:
            raise VoiceError(f"could not decode audio (use WAV, MP3, FLAC or OGG): {exc}") from exc
        return decoded.samples.tobytes()
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, CLIP_SAMPLE_RATE):
                raise VoiceError("without the qwen extra, clips must be mono 16-bit 24 kHz WAV")
            return w.readframes(w.getnframes())
    except wave.Error as exc:
        raise VoiceError(f"not a WAV file: {exc}") from exc


def write_wav(path: Path, samples, sample_rate: int) -> float:
    """Write float samples in [-1, 1] as 16-bit mono WAV. Returns seconds."""
    import numpy as np

    arr = np.asarray(samples, dtype=np.float32).reshape(-1)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return len(arr) / float(sample_rate)


def silence_wav(seconds: float, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(struct.pack("<h", 0) * int(seconds * sample_rate))
    return buf.getvalue()


def speakers_from_config(config_path: Path | None) -> list[dict[str, Any]]:
    """Speaker list from a CustomVoice model's config.json (no model load)."""
    if config_path is None:
        return []
    try:
        cfg = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return []
    talker = cfg.get("talker_config") or {}
    dialects = talker.get("spk_is_dialect") or {}
    out = []
    for key in talker.get("spk_id") or {}:
        dialect = dialects.get(key) or None
        out.append({
            "name": display_speaker(key),
            "native": SPEAKER_NATIVE.get(key),
            "dialect": dialect.removesuffix("_dialect").replace("_", " ") if isinstance(dialect, str) else None,
        })
    return out


def languages_from_config(config_path: Path | None) -> list[str]:
    if config_path is None:
        return list(QWEN_LANGUAGES)
    try:
        cfg = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return list(QWEN_LANGUAGES)
    langs = list((cfg.get("talker_config") or {}).get("codec_language_id") or {})
    return [lang for lang in langs if not lang.endswith("_dialect")] or list(QWEN_LANGUAGES)


def display_speaker(key: str) -> str:
    return "_".join(part.capitalize() for part in key.split("_"))
