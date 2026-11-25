"""Voice specs, languages, speakers, and the reference-clip store.

A profile's `voice` string picks where the voice comes from:

    builtin:<Speaker>      Qwen3-TTS CustomVoice preset speaker
    clone:<clip-name>      Qwen3-TTS Base, cloned from ~/.afterword/voices/<clip-name>.wav
    clone:<path>           Qwen3-TTS Base, cloned from that file (absolute or ~)
    design:<description>   Qwen3-TTS VoiceDesign, voice described in words
    kokoro:<voice>         Kokoro-82M preset voice (af_heart, zf_xiaobei, jf_alpha, ...;
                           "a,b" averages two voices)
    chatterbox:<clip>      Chatterbox Multilingual, cloned from a clip (name or path, like clone:)
    apple:<voice>          Apple's speech synthesizer (AVSpeechSynthesizer): a voice name
                           ("Ava", "Ava (Premium)") or identifier; `apple:` alone picks the
                           best installed voice for the line's language
    say:<MacVoice>         macOS `say` (no model)

Kinds with a model run on the shared engine thread (qwen.py); `apple` and
`say` run as subprocesses.
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

KINDS = ("builtin", "clone", "design", "kokoro", "chatterbox", "apple", "say")
MODEL_FOR_KIND = {
    "builtin": "custom_voice",
    "clone": "base",
    "design": "voice_design",
    "kokoro": "kokoro",
    "chatterbox": "chatterbox",
}
# The engine family of each kind: the `backend` recorded on events and previews.
BACKEND_FOR_KIND = {
    "builtin": "qwen", "clone": "qwen", "design": "qwen",
    "kokoro": "kokoro", "chatterbox": "chatterbox", "apple": "apple", "say": "say",
}
# Kinds whose voice comes from a reference clip (ClipStore.resolve).
CLIP_KINDS = ("clone", "chatterbox")
# Kinds where a fixed seed picks one take (sampling models); the others are
# deterministic or have no seed, so the seed explorer does not apply.
SEEDED_KINDS = ("builtin", "clone", "design", "chatterbox")

# ISO 639-1 (or en-gb) -> Kokoro pipeline code. es/fr/hi/it/pt go through
# espeak-ng (bundled with misaki); ja and zh need misaki[ja] / misaki[zh].
KOKORO_LANGS = {
    "en": "a", "en-us": "a", "en-gb": "b", "es": "e", "fr": "f", "hi": "h",
    "it": "i", "pt": "p", "ja": "j", "zh": "z",
}
KOKORO_LANG_NAMES = {
    "a": "American English", "b": "British English", "e": "Spanish", "f": "French", "h": "Hindi",
    "i": "Italian", "p": "Brazilian Portuguese", "j": "Japanese", "z": "Mandarin Chinese",
}
KOKORO_VOICE_RE = re.compile(r"[abefhijpz][fm]_[a-z0-9]+(,[abefhijpz][fm]_[a-z0-9]+)*")
# The languages of Chatterbox Multilingual (v2/v3), ISO 639-1.
CHATTERBOX_LANGS = (
    "ar", "da", "de", "el", "en", "es", "fi", "fr", "he", "hi", "it", "ja", "ko", "ms",
    "nl", "no", "pl", "pt", "ru", "sv", "sw", "tr", "zh",
)

# Every language some engine speaks (ISO 639-1 -> English name): what
# profiles, [profiles.x.langs.*] and the lab accept. Keep in step with the
# hook's _LANG_NAMES (afterword_hook.py; tests check).
LANGUAGE_NAMES = {
    "en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "hi": "Hindi", "ar": "Arabic", "da": "Danish", "el": "Greek", "fi": "Finnish",
    "he": "Hebrew", "ms": "Malay", "nl": "Dutch", "no": "Norwegian", "pl": "Polish",
    "sv": "Swedish", "sw": "Swahili", "tr": "Turkish",
}

# ISO 639-1 -> Qwen3-TTS language name (Qwen-only; other languages are "auto").
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
    kind: str  # one of KINDS
    value: str  # speaker, clip name/path, description, Kokoro or Apple voice, or mac voice

    @property
    def model(self) -> str | None:
        return MODEL_FOR_KIND.get(self.kind)

    @property
    def backend(self) -> str:
        return BACKEND_FOR_KIND.get(self.kind, self.kind)

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def parse_voice(spec: str | None) -> VoiceSpec:
    if not spec or not isinstance(spec, str) or ":" not in spec:
        raise VoiceError(
            f"voice {spec!r} is not a voice spec; use builtin:<Speaker>, clone:<clip>, "
            "design:<description>, kokoro:<voice>, chatterbox:<clip>, apple:<voice> or say:<MacVoice>"
        )
    kind, _, value = spec.partition(":")
    kind = kind.strip().lower()
    value = value.strip()
    if kind not in KINDS:
        raise VoiceError(f"unknown voice kind {kind!r} in {spec!r} (use {', '.join(KINDS)})")
    if kind in ("builtin", "clone", "design", "kokoro", "chatterbox") and not value:
        raise VoiceError(f"voice {spec!r} needs a value after '{kind}:'")
    if kind == "kokoro":
        value = value.lower()
        if not KOKORO_VOICE_RE.fullmatch(value):
            raise VoiceError(
                f"{spec!r} is not a Kokoro voice (like af_heart, zf_xiaobei or jf_alpha; "
                "'af_heart,af_bella' averages two)"
            )
    if kind == "apple" and len(value) > 200:
        raise VoiceError("Apple voice name is too long")
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


def lang_iso(lang: str | None) -> str | None:
    """An ISO 639-1 code (or en-gb) for a lang value, or None for auto/unknown."""
    if not lang:
        return None
    value = lang.strip().lower().replace("_", "-")
    if value in ("", "auto"):
        return None
    if value == "en-gb":
        return value
    for iso, name in ISO_TO_QWEN.items():
        if value == name:
            return iso
    base = value.split("-", 1)[0]
    return base if re.fullmatch(r"[a-z]{2,3}", base) else None


def lang_tag(lang: str | None) -> str | None:
    """A BCP-47 tag with its region kept ("zh-TW", "en-AU", "zh-Hant-TW"),
    for Apple voices; a language name becomes its ISO code. None for auto."""
    if not lang:
        return None
    value = lang.strip().replace("_", "-")
    if value.lower() in ("", "auto"):
        return None
    for iso, name in LANGUAGE_NAMES.items():
        if value.lower() == name.lower():
            return iso
    parts = [p for p in value.split("-") if p]
    if not parts or not re.fullmatch(r"[A-Za-z]{2,3}", parts[0]):
        return None
    out = [parts[0].lower()]
    for part in parts[1:]:
        if re.fullmatch(r"[A-Za-z]{4}", part):
            out.append(part.title())
        elif re.fullmatch(r"[A-Za-z]{2}|[0-9]{3}", part):
            out.append(part.upper())
        else:
            out.append(part.lower())
    return "-".join(out)


def kokoro_lang(voice: str, lang: str | None) -> str:
    """The Kokoro pipeline code for a line: the line's language when Kokoro
    has it, else the language of the (first) voice. A language Kokoro cannot
    speak is a VoiceError (the line then falls back like any failed render)."""
    iso = lang_iso(lang)
    if iso is None:
        return voice[0]
    code = KOKORO_LANGS.get(iso) or KOKORO_LANGS.get(iso.split("-", 1)[0])
    if code is None:
        raise VoiceError(
            f"Kokoro has no {iso} voices (it speaks "
            + ", ".join(sorted({k.split("-")[0] for k in KOKORO_LANGS})) + ")"
        )
    if code == "a" and voice[0] == "b":
        return "b"  # a British voice reading English keeps its accent
    return code


def chatterbox_lang(lang: str | None) -> str:
    iso = lang_iso(lang)
    if iso is None:
        return "en"
    base = iso.split("-", 1)[0]
    if base not in CHATTERBOX_LANGS:
        raise VoiceError(f"Chatterbox has no {iso} (it speaks {', '.join(CHATTERBOX_LANGS)})")
    return base


def kokoro_voices(snapshot: Path | None) -> list[dict[str, Any]]:
    """The preset voices in a Kokoro snapshot (voices/<name>.safetensors)."""
    if snapshot is None or not (Path(snapshot) / "voices").is_dir():
        return []
    out = []
    for p in sorted((Path(snapshot) / "voices").glob("*.safetensors")):
        name = p.stem
        if not KOKORO_VOICE_RE.fullmatch(name):
            continue
        out.append({
            "name": name,
            "lang": KOKORO_LANG_NAMES.get(name[0], name[0]),
            "lang_code": next((iso for iso, c in KOKORO_LANGS.items() if c == name[0] and "-" not in iso),
                              "en-gb" if name[0] == "b" else None),
            "gender": "female" if name[1] == "f" else "male",
        })
    return out


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
