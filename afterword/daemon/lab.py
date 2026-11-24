"""Voice lab: render previews, build and save profiles, manage clone clips.

HTTP contract: docs/lab-api.md. Previews never go through the playback
queue; the browser plays them. They share the Qwen engine thread with the
queue, one render at a time.
"""

from __future__ import annotations

import asyncio
import math
import os
import secrets
import tempfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

from afterword.hooks.tags import PROFILE_NAME_RE, clean_text

from . import qwen, voices
from .config import ConfigStore, Profile, build_config
from .synth import SaySynth

PREVIEW_KEEP = 100
TEXT_MAX = 1000
PREVIEW_ID_PREFIX = "p"

PROFILE_FIELDS = ("voice", "ref_text", "lang", "pace", "style", "max_chars", "say_voice", "seed", "temperature")


class LabError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _opt_str(body: dict, key: str, limit: int) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise LabError(400, f"{key} must be a string")
    if len(value) > limit:
        raise LabError(400, f"{key} is too long (max {limit} characters)")
    return value.strip()


def _opt_num(body: dict, key: str, kind: type, lo: float, hi: float) -> Any:
    value = body.get(key)
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise LabError(400, f"{key} must be a number")
    try:
        n = float(value) if kind is float else value
        if kind is int:
            f = float(value)
            if not math.isfinite(f) or not f.is_integer():
                raise ValueError
            n = int(f)
    except (TypeError, ValueError, OverflowError):
        raise LabError(400, f"{key} must be a {'whole ' if kind is int else ''}number") from None
    if isinstance(n, float) and not math.isfinite(n):
        raise LabError(400, f"{key} must be a finite number")
    if not lo <= n <= hi:
        raise LabError(400, f"{key} must be between {lo} and {hi}")
    return n


def profile_dict(p: Profile) -> dict[str, Any]:
    return {k: getattr(p, k) for k in PROFILE_FIELDS}


class Lab:
    def __init__(self, home: Path, store: ConfigStore, engine: qwen.QwenEngine, clips: voices.ClipStore,
                 active_synth=None):
        self.home = home
        self.store = store
        self.engine = engine
        self.clips = clips
        self.active_synth = active_synth or (lambda: store.get().daemon.synth)
        self.previews_dir = home / "lab"
        self.previews: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._say_lock = asyncio.Lock()
        # Previews last until the daemon restarts (see docs/lab-api.md).
        for old in self.previews_dir.glob(f"{PREVIEW_ID_PREFIX}*.wav") if self.previews_dir.is_dir() else ():
            old.unlink(missing_ok=True)

    # ------------------------------------------------------------------ #
    # Catalog                                                            #
    # ------------------------------------------------------------------ #

    def catalog(self) -> dict[str, Any]:
        cv = self.engine.snapshot_path("custom_voice")
        any_model = cv or self.engine.snapshot_path("base") or self.engine.snapshot_path("voice_design")
        cfg = self.store.get()
        return {
            "engine": self.engine.status(),
            "speakers": voices.speakers_from_config(cv / "config.json" if cv else None),
            "languages": ["auto"] + voices.languages_from_config(any_model / "config.json" if any_model else None),
            "lang_codes": dict(voices.ISO_TO_QWEN),
            "say_voices": voices.say_voices(),
            "clips": self.clips.list(),
            "profiles": {name: profile_dict(p) for name, p in cfg.profiles.items()},
            "daemon_synth": self.active_synth(),
            "configured_synth": cfg.daemon.synth,
            "samples": dict(voices.SAMPLES),
        }

    # ------------------------------------------------------------------ #
    # Preview                                                            #
    # ------------------------------------------------------------------ #

    def _settings(self, body: dict[str, Any]) -> dict[str, Any]:
        """Merge the request over its base profile and validate."""
        name = _opt_str(body, "profile", 64)
        base: dict[str, Any] = {k: None for k in PROFILE_FIELDS}
        if name:
            cfg = self.store.get()
            if name not in cfg.profiles:
                raise LabError(404, f"no profile named {name!r}")
            base = profile_dict(cfg.profiles[name])
        overrides = {
            "voice": _opt_str(body, "voice", 1100),
            "ref_text": _opt_str(body, "ref_text", 2000),
            "lang": _opt_str(body, "lang", 32),
            "pace": _opt_num(body, "pace", float, 0.5, 2.0),
            "style": _opt_str(body, "style", 300),
            "seed": _opt_num(body, "seed", int, 0, 2**31 - 1),
            "temperature": _opt_num(body, "temperature", float, 0.05, 2.0),
        }
        settings = dict(base)
        settings.update({k: v for k, v in overrides.items() if v is not None})
        settings["mood"] = _opt_str(body, "mood", 64)
        if not settings.get("voice"):
            raise LabError(400, "choose a voice (voice is empty)")
        settings["pace"] = settings.get("pace") or 1.0
        return settings

    async def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        raw_text = body.get("text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise LabError(400, "text is empty")
        if len(raw_text) > TEXT_MAX:
            raise LabError(400, f"text is too long (max {TEXT_MAX} characters)")
        text = clean_text(raw_text)
        if not text:
            raise LabError(400, "text is empty after cleaning")
        settings = self._settings(body)
        try:
            spec = voices.parse_voice(settings["voice"])
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None

        self.previews_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        pid = PREVIEW_ID_PREFIX + secrets.token_hex(5)
        out = self.previews_dir / f"{pid}.wav"

        try:
            meta = await self._render(spec, text, settings, out)
        except LabError:
            out.unlink(missing_ok=True)
            raise
        except Exception as exc:
            out.unlink(missing_ok=True)
            raise LabError(500, f"render failed: {type(exc).__name__}: {exc}") from None
        if not qwen.uses_style(spec):
            settings["style"] = None
            settings["mood"] = None

        audio_s = meta["audio_s"]
        response = {
            "id": pid,
            "url": f"/api/lab/previews/{pid}.wav",
            "text": text,
            **meta,
            "rtf": round(meta["synth_ms"] / 1000 / audio_s, 3) if audio_s else None,
            "pace": settings["pace"],
            "settings": {k: settings.get(k) for k in ("voice", "ref_text", "lang", "pace", "style", "mood", "seed", "temperature")},
        }
        self.previews[pid] = {"path": out, "text": text, "response": response}
        self._prune_previews()
        return response

    async def _render(self, spec: voices.VoiceSpec, text: str, settings: dict[str, Any], out: Path) -> dict[str, Any]:
        if spec.kind == "say":
            async with self._say_lock:
                profile = Profile(name="lab", say_voice=spec.value, pace=1.0)
                result = await asyncio.to_thread(SaySynth().synth, text, profile, {}, out)
            return {"backend": "say", "model": None, "synth_ms": round(result.synth_s * 1000, 1),
                    "load_ms": 0.0, "audio_s": round(result.duration_s, 3), "seed": None}
        try:
            req = qwen.build_request(
                text, spec, self.clips,
                lang=settings.get("lang"), style=settings.get("style"), mood=settings.get("mood"),
                ref_text=settings.get("ref_text"), seed=settings.get("seed"),
                temperature=settings.get("temperature"),
            )
        except voices.VoiceError as exc:
            missing = "no reference clip" in str(exc) or "not found" in str(exc)
            raise LabError(404 if missing else 400, str(exc)) from None
        try:
            result = await self.engine.render_async(req)
        except qwen.EngineBusy as exc:
            raise LabError(429, f"busy: {exc}; try again in a moment") from None
        except (qwen.EngineUnavailable, qwen.ModelNotDownloaded) as exc:
            raise LabError(503, str(exc)) from None
        except qwen.EngineStopped as exc:
            raise LabError(503, str(exc)) from None
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        except Exception as exc:
            if spec.kind == "clone" and isinstance(exc, (RuntimeError, ValueError, OSError)) and "audio" in str(exc).lower():
                raise LabError(400, f"could not read the reference clip: {exc}") from None
            raise
        duration = await asyncio.to_thread(voices.write_wav, out, result.audio, result.sample_rate)
        settings["seed"] = result.seed
        return {"backend": "qwen", "model": result.model, "synth_ms": round(result.synth_s * 1000, 1),
                "load_ms": round(result.load_s * 1000, 1), "audio_s": round(duration, 3), "seed": result.seed}

    def _prune_previews(self) -> None:
        while len(self.previews) > PREVIEW_KEEP:
            _, old = self.previews.popitem(last=False)
            Path(old["path"]).unlink(missing_ok=True)

    def preview_path(self, pid: str) -> Path | None:
        entry = self.previews.get(pid)
        return Path(entry["path"]) if entry else None

    # ------------------------------------------------------------------ #
    # Profiles                                                           #
    # ------------------------------------------------------------------ #

    def save_profile(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        name = body.get("name")
        if not isinstance(name, str) or not PROFILE_NAME_RE.match(name):
            raise LabError(400, "profile name must be 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit")
        fields: dict[str, Any] = {
            "voice": _opt_str(body, "voice", 1100) or "",
            "ref_text": _opt_str(body, "ref_text", 2000),
            "lang": _opt_str(body, "lang", 32),
            "pace": _opt_num(body, "pace", float, 0.5, 2.0),
            "style": _opt_str(body, "style", 300),
            "max_chars": _opt_num(body, "max_chars", int, 20, 5000),
            "say_voice": _opt_str(body, "say_voice", 100),
            "seed": _opt_num(body, "seed", int, 0, 2**31 - 1),
            "temperature": _opt_num(body, "temperature", float, 0.05, 2.0),
        }
        if not fields["voice"]:
            raise LabError(400, "voice is required")
        try:
            voices.parse_voice(fields["voice"])
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        if isinstance(fields["pace"], float):
            fields["pace"] = round(fields["pace"], 3)
        try:
            write_profile(self.store.path, name, fields, overwrite=bool(body.get("overwrite")))
        except FileExistsError:
            raise LabError(409, f"profile {name!r} already exists") from None
        except ValueError as exc:
            raise LabError(400, str(exc)) from None
        self.store.reload(force=True)
        return {"ok": True, "profile": profile_dict(self.store.get().profiles[name])}

    def delete_profile(self, name: str) -> dict[str, Any]:
        try:
            removed = delete_profile(self.store.path, name)
        except ValueError as exc:
            raise LabError(400, f"config.toml could not be updated: {exc}") from None
        if not removed:
            raise LabError(404, f"no profile named {name!r}")
        self.store.reload(force=True)
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # Clips                                                              #
    # ------------------------------------------------------------------ #

    async def add_clip(self, name: str, data: bytes, consent: bool, ref_text: str, overwrite: bool) -> dict[str, Any]:
        if not consent:
            raise LabError(400, "confirm the voice is yours or used with the speaker's consent (consent=1)")
        try:
            info = await asyncio.to_thread(self.clips.add, name, data, ref_text, overwrite)
        except FileExistsError:
            raise LabError(409, f"a clip named {name!r} already exists") from None
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        return {"clip": info}

    def delete_clip(self, name: str) -> dict[str, Any]:
        try:
            removed = self.clips.delete(name)
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        if not removed:
            raise LabError(404, f"no clip named {name!r}")
        return {"ok": True}

    def clip_path(self, name: str) -> Path:
        try:
            path = self.clips.wav_path(name)
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        if not path.is_file():
            raise LabError(404, f"no clip named {name!r}")
        return path

    def freeze(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        pid = body.get("preview_id")
        name = body.get("name")
        entry = self.previews.get(pid) if isinstance(pid, str) else None
        if entry is None or not Path(entry["path"]).is_file():
            raise LabError(404, "preview not found (previews are kept until the daemon restarts)")
        if not isinstance(name, str) or not voices.CLIP_NAME_RE.match(name):
            raise LabError(400, "clip name must be 1-64 letters, digits, '.', '_' or '-'")
        if self.clips.exists(name) and not body.get("overwrite"):
            raise LabError(409, f"a clip named {name!r} already exists")
        try:
            info = self.clips.add(name, Path(entry["path"]).read_bytes(), entry["text"], overwrite=True)
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        return {"clip": info}

    async def warm(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("model") if isinstance(body, dict) else None
        if kind not in qwen.MODEL_KINDS:
            raise LabError(400, f"model must be one of {', '.join(qwen.MODEL_KINDS)}")
        try:
            await self.engine.warm_async(kind)
        except (qwen.EngineUnavailable, qwen.ModelNotDownloaded, qwen.EngineStopped) as exc:
            raise LabError(503, str(exc)) from None
        except Exception as exc:
            raise LabError(500, f"loading {kind} failed: {type(exc).__name__}: {exc}") from None
        return self.engine.status()


# --------------------------------------------------------------------------- #
# config.toml editing (keeps comments and layout)                             #
# --------------------------------------------------------------------------- #


def _load_doc(path: Path):
    import tomlkit

    path = path.resolve() if path.is_symlink() else path
    if path.exists():
        return tomlkit.parse(path.read_text(encoding="utf-8"))
    return tomlkit.document()


def _write_doc(path: Path, doc) -> None:
    import tomlkit

    text = tomlkit.dumps(doc)
    # Validate before writing: the result must still load as a config.
    import tomllib

    build_config(tomllib.loads(text))  # raises if tomlkit produced invalid TOML
    path = path.resolve() if path.is_symlink() else path  # keep a dotfiles symlink intact
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_profile(path: Path, name: str, fields: dict[str, Any], overwrite: bool) -> None:
    import tomlkit

    doc = _load_doc(path)
    profiles = doc.get("profiles")
    if profiles is None:
        profiles = tomlkit.table(is_super_table=True)
        doc["profiles"] = profiles
    if name in profiles and not overwrite:
        raise FileExistsError(name)
    table = profiles.get(name)
    if table is None:
        table = tomlkit.table()
        profiles[name] = table
    for key in PROFILE_FIELDS:
        value = fields.get(key)
        if value is None or (value == "" and key != "voice"):
            if key in table:
                del table[key]
        else:
            table[key] = value
    _write_doc(path, doc)


def delete_profile(path: Path, name: str) -> bool:
    doc = _load_doc(path)
    profiles = doc.get("profiles")
    if profiles is None or name not in profiles:
        return False
    del profiles[name]
    _write_doc(path, doc)
    return True

