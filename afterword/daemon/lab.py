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
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from afterword.hooks import common, pronounce
from afterword.hooks.tags import PROFILE_NAME_RE, clean_text

from . import apple, pitch, qwen, voices
from .config import LANG_FIELDS, ConfigStore, Profile, build_config, lang_code, parse_color
from .explore import Explorer, StarStore, semitones, stability_plan
from .synth import SaySynth

PREVIEW_KEEP = 100
TEXT_MAX = 1000
PREVIEW_ID_PREFIX = "p"

DESCRIPTION_MAX = 300
PROFILE_FIELDS = ("voice", "ref_text", "lang", "pace", "style", "max_chars", "say_voice", "seed", "temperature")
MAX_LANGS = 12  # [profiles.x.langs.*] tables per profile accepted from the lab


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
    out: dict[str, Any] = {k: getattr(p, k) for k in PROFILE_FIELDS}
    out["langs"] = {code: lv.as_dict() for code, lv in p.langs.items()}
    out["description"] = p.description
    out["color"] = p.color or None  # None: the GUI picks one automatically
    return out


def voice_item(p: Profile, usage: dict[str, dict[str, Any]], now: float) -> dict[str, Any]:
    """One entry of GET /api/voices: the profile plus when and by whom it was
    last used (voices.json) and the sessions using it now (within VOICE_IN_USE_S)."""
    u = usage.get(p.name) or {}
    in_use = sorted(
        ({"session_id": sid, "cwd": s.get("cwd"), "ts": s["ts"]}
         for sid, s in (u.get("sessions") or {}).items() if now - s["ts"] < common.VOICE_IN_USE_S),
        key=lambda r: r["ts"], reverse=True)
    out = {"name": p.name, **profile_dict(p)}
    out["persona"] = p.persona
    out["last_used"] = u.get("last_used")
    out["last_session"] = u.get("last_session")
    last = u.get("last_session")
    out["last_cwd"] = (u.get("sessions") or {}).get(last, {}).get("cwd") if last else None
    out["in_use"] = in_use
    return out


def _all_languages(qwen_langs: list[str]) -> list[str]:
    """Qwen's languages (from its model), then the other engines' ones."""
    seen = {lang.lower() for lang in qwen_langs}
    extra = [name.lower() for name in voices.LANGUAGE_NAMES.values() if name.lower() not in seen]
    return list(qwen_langs) + extra


def _langs_body(raw: Any) -> dict[str, dict[str, Any]] | None:
    """Validate the `langs` of a profile save: {lang: {voice?, ref_text?,
    style?, pace?, say_voice?, seed?}}. None when absent (keep the file's)."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LabError(400, "langs must be an object of {language: {voice, say_voice, ...}}")
    if len(raw) > MAX_LANGS:
        raise LabError(400, f"langs: at most {MAX_LANGS} languages")
    out: dict[str, dict[str, Any]] = {}
    for key, entry in raw.items():
        code = lang_code(key)
        if code is None:
            raise LabError(400, f"langs: unknown language {str(key)[:32]!r} (use an ISO code such as zh)")
        if code in out:
            raise LabError(400, f"langs: {code!r} is given twice")
        if not isinstance(entry, dict):
            raise LabError(400, f"langs.{code} must be an object")
        extra = sorted(set(entry) - set(LANG_FIELDS))
        if extra:
            raise LabError(400, f"langs.{code}: unknown field {extra[0]!r} (allowed: {', '.join(LANG_FIELDS)})")
        try:
            fields = {
                "voice": _opt_str(entry, "voice", 1100),
                "ref_text": _opt_str(entry, "ref_text", 2000),
                "style": _opt_str(entry, "style", 300),
                "pace": _opt_num(entry, "pace", float, 0.5, 2.0),
                "say_voice": _opt_str(entry, "say_voice", 100),
                "seed": _opt_num(entry, "seed", int, 0, 2**31 - 1),
            }
        except LabError as exc:
            raise LabError(exc.status, f"langs.{code}: {exc.message}") from None
        if fields["voice"]:
            try:
                voices.parse_voice(fields["voice"])
            except voices.VoiceError as exc:
                raise LabError(400, f"langs.{code}.voice: {exc}") from None
        if isinstance(fields["pace"], float):
            fields["pace"] = round(fields["pace"], 3)
        out[code] = {k: v for k, v in fields.items() if v is not None and v != ""}
    return out


class Lab:
    def __init__(self, home: Path, store: ConfigStore, engine: qwen.QwenEngine, clips: voices.ClipStore,
                 active_synth=None, publish=None, live_busy=None):
        self.home = home
        self.store = store
        self.engine = engine
        self.clips = clips
        self.active_synth = active_synth or (lambda: store.get().daemon.synth)
        self.publish = publish or (lambda kind, data: None)  # SSE (EventLog.publish)
        # True while the daemon has live speech to say (the seed explorer holds back).
        self.live_busy = live_busy or (lambda: False)
        self.previews_dir = home / "lab"
        self.previews: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._say_lock = asyncio.Lock()
        self._stability_lock = asyncio.Lock()
        # Previews last until the daemon restarts (see docs/lab-api.md);
        # stars live in lab/starred/ and are kept.
        for old in self.previews_dir.glob(f"{PREVIEW_ID_PREFIX}*.wav") if self.previews_dir.is_dir() else ():
            old.unlink(missing_ok=True)
        self.explorer = Explorer(self)
        self.stars = StarStore(self.previews_dir)

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
            "languages": ["auto"] + _all_languages(
                voices.languages_from_config(any_model / "config.json" if any_model else None)),
            # ISO -> lower-case language name, for every engine's languages
            # (the Qwen names are the same words).
            "lang_codes": {**{k: v.lower() for k, v in voices.LANGUAGE_NAMES.items()}, **voices.ISO_TO_QWEN},
            "say_voices": voices.say_voices(),
            "kokoro_voices": voices.kokoro_voices(self.engine.snapshot_path("kokoro")),
            "kokoro_langs": sorted({k.split("-")[0] for k in voices.KOKORO_LANGS}),
            "chatterbox_langs": list(voices.CHATTERBOX_LANGS),
            "apple": self.apple_status(),
            "apple_voices": [v for v in apple.list_voices() if not v.get("personal")]
            if apple.available()[0] else [],
            "clips": self.clips.list(),
            "profiles": {name: profile_dict(p) for name, p in cfg.profiles.items()},
            "daemon_synth": self.active_synth(),
            "configured_synth": cfg.daemon.synth,
            "samples": dict(voices.SAMPLES),
        }

    @staticmethod
    def apple_status() -> dict[str, Any]:
        ok, error = apple.available()
        return {"available": ok, "error": error}

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
            # A profile with [langs]: the voice for the requested language.
            lang = _opt_str(body, "lang", 32)
            profile = cfg.profiles[name]
            if lang and lang.lower() != "auto":
                profile = cfg.voice_for(profile, lang)
            base = {k: getattr(profile, k) for k in PROFILE_FIELDS}
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

    def _prepare(self, body: Any) -> dict[str, Any]:
        """Validate a preview body: the text, the text to speak (after the
        pronunciation list), the settings and the voice spec."""
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
        skip = body.get("skip_pronounce", False)
        if not isinstance(skip, bool):
            raise LabError(400, "skip_pronounce must be true or false")
        # The pronunciation list: global entries, plus one profile's own --
        # `pronounce_profile` when given (the builder sends the profile it
        # was loaded from, without making it the base of the settings),
        # else the base `profile`.
        cfg = self.store.get()
        pron_name = _opt_str(body, "pronounce_profile", 64)
        if pron_name and pron_name not in cfg.profiles:
            raise LabError(404, f"no profile named {pron_name!r}")
        pron_name = pron_name or _opt_str(body, "profile", 64) or None
        spoken = text if skip else pronounce.apply(text, cfg.pronunciations(pron_name) if pron_name else cfg.pronounce)
        if not qwen.uses_style(spec):
            settings["style"] = None
            settings["mood"] = None
        return {"text": text, "spoken": spoken, "settings": settings, "spec": spec,
                "skip": skip, "pron_name": pron_name}

    def check_voice(self, prep: dict[str, Any]) -> None:
        """Fail now, as a render would, when the voice cannot render: a
        missing reference clip (404), a model not downloaded or mlx-audio
        missing (503). Other problems still surface at the first render."""
        spec, settings = prep["spec"], prep["settings"]
        if spec.kind == "say":
            return
        if spec.kind == "apple":
            ok, error = apple.available()
            if not ok:
                raise LabError(503, error or "Apple voices are not available")
            return
        try:
            qwen.build_request(prep["spoken"], spec, self.clips, lang=settings.get("lang"), style=None, mood=None,
                               ref_text=settings.get("ref_text"), seed=None, temperature=None)
        except voices.VoiceError as exc:
            missing = "no reference clip" in str(exc) or "not found" in str(exc)
            raise LabError(404 if missing else 400, str(exc)) from None
        if spec.model is None:
            raise LabError(400, f"{spec} is not a local model voice")
        try:
            self.engine.check_ready(spec.model)
        except (qwen.EngineUnavailable, qwen.ModelNotDownloaded) as exc:
            raise LabError(503, str(exc)) from None

    def new_preview_path(self) -> Path:
        self.previews_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self.previews_dir / f"{PREVIEW_ID_PREFIX}{secrets.token_hex(5)}.wav"

    @staticmethod
    def _response(pid: str, prep: dict[str, Any], meta: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        text, spoken, skip = prep["text"], prep["spoken"], prep["skip"]
        audio_s = meta["audio_s"]
        return {
            "id": pid,
            "url": f"/api/lab/previews/{pid}.wav",
            "text": text,
            "spoken_text": spoken if spoken != text else None,
            "skip_pronounce": skip,
            "pronounce_profile": None if skip else prep["pron_name"],
            **meta,
            "rtf": round(meta["synth_ms"] / 1000 / audio_s, 3) if audio_s else None,
            "pace": settings["pace"],
            "settings": {k: settings.get(k) for k in ("voice", "ref_text", "lang", "pace", "style", "mood", "seed", "temperature")},
        }

    async def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        prep = self._prepare(body)
        settings = prep["settings"]
        out = self.new_preview_path()
        pid = out.stem
        try:
            meta = await self._render(prep["spec"], prep["spoken"], settings, out)
        except LabError:
            out.unlink(missing_ok=True)
            raise
        except Exception as exc:
            out.unlink(missing_ok=True)
            raise LabError(500, f"render failed: {type(exc).__name__}: {exc}") from None
        response = self._response(pid, prep, meta, settings)
        # Freezing a preview uses its words as the clip transcript: the spoken ones.
        self.previews[pid] = {"path": out, "text": prep["spoken"], "response": response}
        self._prune_previews()
        return response

    async def _render(self, spec: voices.VoiceSpec, text: str, settings: dict[str, Any], out: Path,
                      cancel: threading.Event | None = None, preemptible: bool = False) -> dict[str, Any]:
        """Render to `out`; the metadata of a preview, with its pitch.
        `preemptible` (the seed explorer): streamed, so live speech queued
        meanwhile stops it at its next chunk (qwen.RenderPreempted)."""
        if spec.kind in ("say", "apple"):
            async with self._say_lock:
                if spec.kind == "say":
                    profile = Profile(name="lab", say_voice=spec.value, pace=1.0)
                    run = SaySynth().synth
                else:
                    profile = Profile(name="lab", voice=str(spec), lang=settings.get("lang") or "", pace=1.0)
                    run = apple.AppleSynth().synth
                try:
                    result = await asyncio.to_thread(run, text, profile, {}, out)
                except voices.VoiceError as exc:
                    raise LabError(400, str(exc)) from None
                except apple.AppleUnavailable as exc:
                    raise LabError(503, str(exc)) from None
            f0 = await asyncio.to_thread(pitch.f0_of_wav, out)
            settings["seed"] = None  # no seed: never show or save one for this voice
            return {"backend": spec.backend, "model": None, "synth_ms": round(result.synth_s * 1000, 1),
                    "load_ms": 0.0, "audio_s": round(result.duration_s, 3), "seed": None,
                    "f0_hz": f0["f0_hz"], "f0_spread_hz": f0["f0_spread_hz"]}
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
            if preemptible or cancel is not None:
                # One job at a time from its owner, so not counted against MAX_PENDING.
                future = self.engine.submit_render(req, limit=False, priority=qwen.LAB, cancel=cancel,
                                                   stream=preemptible, preemptible=preemptible)
                result = await asyncio.wrap_future(future)
            else:
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
            if spec.kind in voices.CLIP_KINDS and isinstance(exc, (RuntimeError, ValueError, OSError)) and "audio" in str(exc).lower():
                raise LabError(400, f"could not read the reference clip: {exc}") from None
            raise
        duration = await asyncio.to_thread(voices.write_wav, out, result.audio, result.sample_rate)
        f0 = await asyncio.to_thread(pitch.f0_stats, result.audio, result.sample_rate)
        settings["seed"] = result.seed  # None for voices without a seed (Kokoro)
        return {"backend": spec.backend, "model": result.model, "synth_ms": round(result.synth_s * 1000, 1),
                "load_ms": round(result.load_s * 1000, 1), "audio_s": round(duration, 3), "seed": result.seed,
                "f0_hz": f0["f0_hz"], "f0_spread_hz": f0["f0_spread_hz"]}

    def _prune_previews(self) -> None:
        while len(self.previews) > PREVIEW_KEEP:
            _, old = self.previews.popitem(last=False)
            Path(old["path"]).unlink(missing_ok=True)

    def _entry(self, pid: Any) -> dict[str, Any] | None:
        """A preview or a seed-explorer take: {path, text (spoken), response}."""
        if not isinstance(pid, str):
            return None
        return self.previews.get(pid) or self.explorer.entry(pid)

    def preview_path(self, pid: str) -> Path | None:
        entry = self._entry(pid)
        return Path(entry["path"]) if entry else None

    # ------------------------------------------------------------------ #
    # Seed explorer, stars, stability check (explore.py)                 #
    # ------------------------------------------------------------------ #

    async def explore_start(self, body: Any) -> dict[str, Any]:
        return await self.explorer.start(body)

    def explore_cursor(self, body: Any) -> dict[str, Any]:
        return self.explorer.move(body)

    def explore_stop(self) -> dict[str, Any]:
        return self.explorer.stop()

    def explore_view(self) -> dict[str, Any]:
        return self.explorer.view()

    def stars_view(self) -> dict[str, Any]:
        return {"stars": self.stars.list(), "error": self.stars.error}

    def star(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        pid = body.get("preview_id")
        note = _opt_str(body, "note", 300)
        entry = self._entry(pid)
        if entry is None or not Path(entry["path"]).is_file():
            raise LabError(404, "preview not found (previews and takes are kept until the daemon restarts)")
        res = entry["response"]
        f0 = {"f0_hz": res.get("f0_hz"), "f0_spread_hz": res.get("f0_spread_hz")}
        if "f0_hz" not in res:
            f0 = pitch.f0_of_wav(entry["path"])
        try:
            star, created = self.stars.add(pid, entry, note, f0)
        except OverflowError as exc:
            raise LabError(409, str(exc)) from None
        except FileNotFoundError:  # discarded (a new exploration) or pruned meanwhile
            raise LabError(404, "preview not found (it was just discarded)") from None
        except OSError as exc:
            raise LabError(500, f"could not save the star: {exc}") from None
        return {"star": star, "created": created}

    def unstar(self, sid: str) -> dict[str, Any]:
        if not self.stars.remove(sid):
            raise LabError(404, f"no star {sid!r}")
        return {"ok": True}

    def star_path(self, sid: str) -> Path:
        path = self.stars.path(sid)
        if path is None or not path.is_file():
            raise LabError(404, "star not found")
        return path

    async def stability(self, body: Any) -> dict[str, Any]:
        """Render one setting and seed on three other sentences (one in
        another language) and report each one's pitch against the take's."""
        if not isinstance(body, dict):
            raise LabError(400, "expected a JSON object")
        star = None
        if body.get("star_id") is not None:
            star = self.stars.get(body.get("star_id"))
            if star is None:
                raise LabError(404, "star not found")
            settings = dict(star.get("settings") or {})
            seed = star.get("seed") if star.get("seed") is not None else settings.get("seed")
            base_text = star.get("text") or ""
            extra = {"skip_pronounce": bool(star.get("skip_pronounce"))}
            if star.get("pronounce_profile") and star["pronounce_profile"] in self.store.get().profiles:
                extra["pronounce_profile"] = star["pronounce_profile"]
            base = {"text": base_text, "f0_hz": star.get("f0_hz"), "f0_spread_hz": star.get("f0_spread_hz")}
        else:
            raw = body.get("settings")
            if not isinstance(raw, dict):
                raise LabError(400, "send {\"star_id\": ...} or {\"settings\": {...}, \"seed\": N}")
            seed = _opt_num(body, "seed", int, 0, 2**31 - 1)
            if seed is None:
                seed = _opt_num(raw, "seed", int, 0, 2**31 - 1)
            settings = self._settings(raw)
            base_text = raw.get("text") if isinstance(raw.get("text"), str) else ""
            extra = {k: raw[k] for k in ("skip_pronounce", "pronounce_profile", "profile") if raw.get(k) is not None}
            base = None
        if seed is None:
            raise LabError(400, "the take has no seed (macOS say voices cannot be checked)")
        if str(settings.get("voice") or "").lower().startswith("say:"):
            raise LabError(400, "macOS say voices have no seed to check")
        if self._stability_lock.locked():
            raise LabError(409, "a stability check is already running")
        async with self._stability_lock:
            lang = lang_code(settings.get("lang")) if settings.get("lang") else None
            takes: list[dict[str, Any]] = []
            errors: list[str] = []
            for code, sentence in stability_plan(lang, base_text):
                req = {k: settings.get(k) for k in ("voice", "ref_text", "pace", "style", "mood", "temperature")}
                req = {k: v for k, v in req.items() if v is not None}
                req.update(extra, text=sentence, seed=seed, lang=code)
                try:
                    res = await self.preview(req)
                except LabError as exc:
                    if not takes and exc.status in (400, 404, 503):
                        raise
                    errors.append(f"{code}: {exc.message}")
                    continue
                res = dict(res, check_lang=code,
                           f0_delta_st=semitones(res.get("f0_hz"), (base or {}).get("f0_hz")))
                takes.append(res)
        deltas = [abs(t["f0_delta_st"]) for t in takes if t.get("f0_delta_st") is not None]
        f0s = [t["f0_hz"] for t in takes if isinstance(t.get("f0_hz"), (int, float))]
        if base and isinstance(base.get("f0_hz"), (int, float)):
            f0s.append(base["f0_hz"])
        return {
            "star_id": star["id"] if star else None,
            "seed": seed,
            "voice": settings.get("voice"),
            "base": base,
            "takes": takes,
            "errors": errors,
            "max_delta_st": max(deltas) if deltas else None,
            "f0_range_hz": round(max(f0s) - min(f0s), 1) if len(f0s) > 1 else None,
        }

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
        if "description" in body:  # absent: an existing description is kept
            fields["description"] = _opt_str(body, "description", DESCRIPTION_MAX) or ""
        langs = _langs_body(body.get("langs"))
        if not fields["voice"]:
            raise LabError(400, "voice is required")
        try:
            voices.parse_voice(fields["voice"])
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        if isinstance(fields["pace"], float):
            fields["pace"] = round(fields["pace"], 3)
        try:
            write_profile(self.store.path, name, fields, overwrite=bool(body.get("overwrite")), langs=langs)
        except FileExistsError:
            raise LabError(409, f"profile {name!r} already exists") from None
        except ValueError as exc:
            raise LabError(400, str(exc)) from None
        self.store.reload(force=True)
        return {"ok": True, "profile": profile_dict(self.store.get().profiles[name])}

    def voices(self) -> dict[str, Any]:
        """GET /api/voices: every profile (config order) with its usage."""
        usage = common.load_voice_usage()
        now = time.time()
        return {"profiles": [voice_item(p, usage, now) for p in self.store.get().profiles.values()]}

    def set_description(self, name: str, body: Any) -> dict[str, Any]:
        """PATCH /api/lab/profiles/{name}: {"description": "text"} and/or
        {"color": "teal" | "#RRGGBB" | ""}; "" removes the key."""
        if not isinstance(body, dict) or not ({"description", "color"} & set(body)):
            raise LabError(400, "send {\"description\": \"text\"} and/or {\"color\": \"teal\" | \"#RRGGBB\" | \"\"}")
        keys: dict[str, str] = {}
        if "description" in body:
            text = _opt_str(body, "description", DESCRIPTION_MAX)
            if text is None:
                raise LabError(400, "description must be a string")
            keys["description"] = text
        if "color" in body:
            try:
                keys["color"] = parse_color(body["color"])
            except ValueError as exc:
                raise LabError(400, f"color: {exc}") from None
        if name not in self.store.get().profiles:
            raise LabError(404, f"no profile named {name!r}")
        try:
            found = write_profile_keys(self.store.path, name, keys)
        except ValueError as exc:
            raise LabError(400, f"config.toml could not be updated: {exc}") from None
        if not found:
            raise LabError(404, f"no profile named {name!r}")
        self.store.reload(force=True)
        profile = self.store.get().profiles.get(name)
        if profile is None:
            raise LabError(404, f"no profile named {name!r}")
        return {"ok": True, "profile": voice_item(profile, common.load_voice_usage(), time.time())}

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
        sid = body.get("star_id")
        if sid is None and isinstance(pid, str) and pid.startswith("s"):
            sid = pid  # a star id given as preview_id
        if sid is not None:
            star = self.stars.get(sid)
            path = self.stars.path(sid) if star else None
            if star is None or path is None or not path.is_file():
                raise LabError(404, "star not found")
            entry = {"path": path, "text": star.get("spoken") or star.get("text") or ""}
        else:
            entry = self._entry(pid)
        if entry is None or not Path(entry["path"]).is_file():
            raise LabError(404, "preview not found (previews are kept until the daemon restarts)")
        if not isinstance(name, str) or not voices.CLIP_NAME_RE.match(name):
            raise LabError(400, "clip name must be 1-64 letters, digits, '.', '_' or '-'")
        if self.clips.exists(name) and not body.get("overwrite"):
            raise LabError(409, f"a clip named {name!r} already exists")
        try:
            data = Path(entry["path"]).read_bytes()
        except FileNotFoundError:  # deleted (unstarred, discarded, pruned) since the check
            raise LabError(404, "star not found" if sid is not None else "preview not found") from None
        try:
            info = self.clips.add(name, data, entry["text"], overwrite=True)
        except voices.VoiceError as exc:
            raise LabError(400, str(exc)) from None
        return {"clip": info}

    async def warm(self, body: dict[str, Any]) -> dict[str, Any]:
        kind = body.get("model") if isinstance(body, dict) else None
        if kind not in qwen.MODEL_KINDS:
            raise LabError(400, f"model must be one of {', '.join(qwen.MODEL_KINDS)}")
        try:
            await self.engine.warm_async(kind, priority=qwen.LAB)  # live speech first
        except (qwen.EngineUnavailable, qwen.ModelNotDownloaded, qwen.EngineStopped) as exc:
            raise LabError(503, str(exc)) from None
        except Exception as exc:
            raise LabError(500, f"loading {kind} failed: {type(exc).__name__}: {exc}") from None
        return self.engine.status()


# --------------------------------------------------------------------------- #
# config.toml editing (keeps comments and layout)                             #
# --------------------------------------------------------------------------- #


# Every read-modify-write of config.toml holds this lock: the pronunciation
# save runs in a worker thread, profile saves on the event loop, and without
# it one write could silently undo the other.
CONFIG_WRITE_LOCK = threading.RLock()


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
    common.atomic_write(path, text, mode=mode, durable=True)


def write_profile(path: Path, name: str, fields: dict[str, Any], overwrite: bool,
                  langs: dict[str, dict[str, Any]] | None = None) -> None:
    """Write [profiles.<name>]. `langs` (when not None) replaces its
    [profiles.<name>.langs.*] tables: {iso: {field: value}}; None keeps them."""
    with CONFIG_WRITE_LOCK:
        _write_profile(path, name, fields, overwrite, langs)


def _write_langs(table, langs: dict[str, dict[str, Any]]) -> None:
    """Make table["langs"] hold exactly `langs`, in place: kept languages keep
    their position and comments (matched by ISO code, so [langs.chinese]
    stays [langs.chinese]); the rest go; new ones are added at the end."""
    import tomlkit
    from tomlkit.items import InlineTable

    existing = table.get("langs")
    if not langs:
        if existing is not None:
            del table["langs"]
        return
    if existing is None or not hasattr(existing, "keys") or isinstance(existing, InlineTable):
        if existing is not None:
            del table["langs"]
        table["langs"] = tomlkit.table(is_super_table=True)
        existing = table["langs"]
    keys = {}
    for key in list(existing.keys()):
        code = lang_code(key)
        if code not in langs or code in keys:
            del existing[key]
        else:
            keys[code] = key
    for code, values in langs.items():
        key = keys.get(code, code)
        sub = existing.get(key)
        new = sub is None or not hasattr(sub, "keys") or isinstance(sub, InlineTable)
        if new:
            if sub is not None:
                del existing[key]
            existing[key] = tomlkit.table()
            sub = existing[key]
        for f in LANG_FIELDS:
            value = values.get(f)
            if value is None or value == "":
                if f in sub:
                    del sub[f]
            elif f not in sub or sub[f] != value:
                sub[f] = value
        if new:
            sub.add(tomlkit.nl())  # a blank line before whatever follows


def _write_profile(path: Path, name: str, fields: dict[str, Any], overwrite: bool,
                   langs: dict[str, dict[str, Any]] | None = None) -> None:
    import tomllib

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
    if "description" in fields:  # left out of `fields`: keep what the file has
        if fields["description"]:
            table["description"] = fields["description"]
        elif "description" in table:
            del table["description"]
    if langs is not None:
        _write_langs(table, langs)
    # Read back: every field (and language) must land in this profile's table.
    got = tomllib.loads(tomlkit.dumps(doc)).get("profiles", {}).get(name, {})
    for key in PROFILE_FIELDS:
        value = fields.get(key)
        if not (value is None or (value == "" and key != "voice")) and got.get(key) != value:
            raise ValueError(f"profiles.{name}.{key} did not read back the same")
    if fields.get("description") and got.get("description") != fields["description"]:
        raise ValueError(f"profiles.{name}.description did not read back the same")
    if langs is not None:
        # Only the fields we write count: a key outside LANG_FIELDS (a typo
        # that build_config warns about) is left in place, not compared.
        back = {
            lang_code(k): {f: v[f] for f in LANG_FIELDS if f in v}
            for k, v in (got.get("langs") or {}).items() if isinstance(v, dict)
        }
        want = {code: {f: v for f, v in values.items() if v is not None and v != ""}
                for code, values in langs.items()}
        if back != want:
            raise ValueError(f"profiles.{name}.langs did not read back the same")
    _write_doc(path, doc)


def write_description(path: Path, name: str, text: str) -> bool:
    """Set (or, for "", remove) only `description` in [profiles.<name>],
    keeping comments and the rest of the table. False if there is no such profile."""
    return write_profile_keys(path, name, {"description": text})


def write_profile_keys(path: Path, name: str, keys: dict[str, str]) -> bool:
    """Set (or, for "", remove) only the given string keys in [profiles.<name>]
    in one write, keeping comments and the rest of the table. False if there
    is no such profile."""
    with CONFIG_WRITE_LOCK:
        doc = _load_doc(path)
        profiles = doc.get("profiles")
        if profiles is None or name not in profiles:
            return False
        table = profiles[name]
        for key, text in keys.items():
            if text:
                table[key] = text
            elif key in table:
                del table[key]
        _write_doc(path, doc)
        return True


def delete_profile(path: Path, name: str) -> bool:
    with CONFIG_WRITE_LOCK:
        return _delete_profile(path, name)


def _delete_profile(path: Path, name: str) -> bool:
    doc = _load_doc(path)
    profiles = doc.get("profiles")
    if profiles is None or name not in profiles:
        return False
    del profiles[name]
    _write_doc(path, doc)
    return True



# --------------------------------------------------------------------------- #
# Pronunciation list editing (GET/POST /api/pronounce, docs/gui-api.md)        #
# --------------------------------------------------------------------------- #


def pronounce_view(store: ConfigStore) -> dict[str, Any]:
    cfg = store.get()
    return {
        "path": str(store.path),
        "global": dict(cfg.pronounce),
        "profiles": {name: dict(p.pronounce) for name, p in cfg.profiles.items()},
        "default_profile": next(iter(cfg.profiles), None),
        "error": store.error,
        "warnings": [w for w in store.warnings if w.startswith("pronounce") or ".pronounce" in w],
        "limits": {"entries": pronounce.MAX_ENTRIES, "key": pronounce.MAX_KEY, "value": pronounce.MAX_VALUE},
    }


def save_pronounce(store: ConfigStore, body: Any) -> dict[str, Any]:
    """Replace one pronunciation table: {"global": {...}} or
    {"profile": name|null, "entries": {...}} (null = the global table)."""
    if not isinstance(body, dict):
        raise LabError(400, "expected a JSON object")
    if "global" in body:
        profile, entries = None, body.get("global")
    elif "entries" in body:
        profile, entries = body.get("profile"), body.get("entries")
    else:
        raise LabError(400, 'send {"global": {...}} or {"profile": name|null, "entries": {...}}')
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise LabError(400, "profile must be a profile name or null (null = the global table)")
    if not isinstance(entries, dict):
        raise LabError(400, "entries must be an object of \"word\": \"spoken as\" pairs")
    where = f"profiles.{profile}.pronounce" if profile is not None else "pronounce"
    errors: list[str] = []
    pairs = pronounce.clean_table(entries, errors, where)
    if len(entries) > pronounce.MAX_ENTRIES and not any("more than" in e for e in errors):
        errors.append(f"{where}: more than {pronounce.MAX_ENTRIES} entries")
    if errors:
        raise LabError(400, "; ".join(errors[:5]))
    if profile is not None and profile not in store.get().profiles:
        raise LabError(404, f"no profile named {profile!r}")
    try:
        write_pronounce(store.path, profile, dict(pairs))
    except LookupError:
        raise LabError(404, f"no profile named {profile!r} in config.toml") from None
    except Exception as exc:
        raise LabError(400, f"config.toml could not be updated: {exc}") from None
    store.reload(force=True)
    return pronounce_view(store)


def write_pronounce(path: Path, profile: str | None, entries: dict[str, str]) -> None:
    """Make one [pronounce] table hold exactly `entries`, in place: kept keys
    keep their position and comments, removed keys go, new keys are added at
    the end. The table itself (and comments around it) always stays.

    Entries the GUI never saw (rejected by clean_table: not a string, too
    long, past the entry limit) are left as they are, so a save cannot
    silently delete a hand-written line that only needs fixing."""
    with CONFIG_WRITE_LOCK:
        _write_pronounce(path, profile, entries)


def _table_of(parsed: dict[str, Any], profile: str | None) -> Any:
    if profile is None:
        return parsed.get("pronounce")
    profiles = parsed.get("profiles")
    table = profiles.get(profile) if isinstance(profiles, dict) else None
    return table.get("pronounce") if isinstance(table, dict) else None


def _write_pronounce(path: Path, profile: str | None, entries: dict[str, str]) -> None:
    import tomllib

    import tomlkit
    from tomlkit.items import InlineTable

    doc = _load_doc(path)
    if profile is not None:
        profiles = doc.get("profiles")
        parent = profiles.get(profile) if profiles is not None else None
        if parent is None:
            raise LookupError(profile)
    else:
        parent = doc
    before = _table_of(tomllib.loads(tomlkit.dumps(doc)), profile)
    before = before if isinstance(before, dict) else {}
    shown = {k for k, _ in pronounce.clean_table(before)}
    hidden = {k: v for k, v in before.items() if k not in shown and k not in entries}
    table = parent.get("pronounce")
    if table is None or not hasattr(table, "keys"):
        if table is not None:
            del parent["pronounce"]
        table = tomlkit.inline_table() if isinstance(parent, InlineTable) else tomlkit.table()
        parent["pronounce"] = table
        table = parent["pronounce"]
    for key in list(table.keys()):
        if key not in entries and key not in hidden:
            del table[key]
    for key, value in entries.items():
        if key not in table or table[key] != value:
            table[key] = value
    got = _table_of(tomllib.loads(tomlkit.dumps(doc)), profile)
    if not isinstance(got, dict) or got != {**hidden, **entries}:
        raise ValueError("the written table did not read back the same")
    _write_doc(path, doc)
