"""config.toml: defaults, validation, and mtime-based hot reload."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from afterword.hooks import common
from afterword.hooks import pronounce as _pron
from afterword.hooks import scriptsplit as _split

log = logging.getLogger("afterword.config")

SYNTHS = ("qwen", "say", "null")
PLAYERS = ("afplay", "null")
QWEN_MODEL_KINDS = ("custom_voice", "base", "voice_design")
OTHER_MODEL_KINDS = ("kokoro", "chatterbox")  # [models]
MODEL_FALLBACKS = ("apple", "say", "none")

DEFAULT_CONFIG_TOML = """\
# AfterWord config. Edits are picked up automatically (hot reload).
# Profiles can also be built and saved from the voice lab:
# http://127.0.0.1:47800/#lab

[daemon]
port = 47800
fallback = "say"          # what the hook does when the daemon is down: "say" or "none"
announce_session = false  # speak the session label (cwd folder name) before each message
synth = "qwen"            # "qwen" (the local models: Qwen3-TTS, Kokoro, Chatterbox, plus
                          # apple: and say: voices), "say" (macOS say only) or "null"
model_fallback = "apple"  # when a model voice fails: "apple" (the best Apple voice for the
                          # line's language, then say), "say", or "none" (the line fails)
player = "afplay"         # "afplay" (the speakers) or "null"
stream = true             # start speaking while the line is still rendering (PortAudio;
                          # if the audio device cannot be opened, afplay plays the whole line)
early_say = true          # speak a <say> as soon as Claude has written it (MessageDisplay hook),
                          # not only when the reply is finished
max_queue = 16            # queued items beyond this are skipped as "daemon busy"
history = 50              # clips kept on disk for replay

[alerts]
# Spoken alerts: a short line when Claude needs you (a permission prompt, a
# question), is idle, or finished a long turn. They speak even when the
# session's voice is off, but not when it (or everything) is muted. Phrases
# live in alerts.json next to this file (Config page: edit, generate, hear).
enabled = false
profile = ""              # "" = the session's profile if its voice is on, else the first profile
events = ["permission", "input", "done"]   # also "idle"
done_after_s = 60         # "done": a turn this long finished with nothing spoken
permission_when = "away"  # "away" = after Claude Code's ~6 s prompt timer; "now" = immediately

[qwen]
# Hugging Face repos for the three Qwen3-TTS variants. Download them with
#   uv run afterword models pull
custom_voice = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"   # builtin:<Speaker>
base = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"                  # clone:<clip>
voice_design = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit"   # design:<description>
keep_loaded = 2           # models kept in memory at once (Qwen3-TTS about 3 GB each,
                          # Chatterbox about 3 GB; Kokoro is small and not counted)
warm_on_start = true      # load the models your profiles use when the daemon starts

[models]
# Hugging Face repos for the other local models (keep_loaded and
# warm_on_start above apply to them too). Download with
#   uv sync --extra kokoro --extra chatterbox
#   uv run afterword models pull kokoro chatterbox
kokoro = "mlx-community/Kokoro-82M-bf16"                  # kokoro:<voice>, e.g. kokoro:zf_xiaobei
chatterbox = "mlx-community/chatterbox-multilingual-v3"   # chatterbox:<clip>

# voice: builtin:<Speaker> | clone:<clip name or path> | design:<description> | say:<MacVoice>
# Speakers: Serena, Vivian, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee.
# A profile's voice speaks every language (the accent stays the same), unless
# the profile gives a language its own voice:
#   [profiles.nova.langs.zh]   # any of: voice, ref_text, style, pace, say_voice, seed
#   voice = "builtin:Vivian"
#   say_voice = "Tingting"
# Then mixed lines are split by script (Han, kana, Hangul, the rest) and each
# part is spoken with its language's voice.

[profiles.nova]
voice = "builtin:Serena"
ref_text = ""               # transcript of the clone clip (clone voices only)
lang = "en"
pace = 1.0
style = "calm, slightly wry"
max_chars = 400
say_voice = "Samantha"      # macOS voice used if Qwen3-TTS is unavailable

[profiles.ember]
voice = "builtin:Aiden"
lang = "en"
pace = 1.05
style = "warm, brisk"
max_chars = 400
say_voice = "Daniel"

[pronounce]
# Pronunciations: words rewritten before they are spoken (also editable on the
# GUI's Config page). Longest match first; a key that begins or ends with a
# letter or digit only matches whole words ("uv" leaves "uvicorn" alone); an
# all-lowercase key ignores case, a key with capitals matches exactly.
# Per-profile entries go in [profiles.<name>.pronounce] and override these.
# "SQLite" = "sequel lite"
# "kubectl" = "cube control"
# "uv" = "you vee"
# "__init__" = "dunder init"
"""


LANG_FIELDS = ("voice", "ref_text", "style", "pace", "say_voice", "seed")


@dataclass(frozen=True)
class LangVoice:
    """[profiles.x.langs.<lang>]: settings for one language. None inherits
    the profile's value."""

    voice: str | None = None
    ref_text: str | None = None
    style: str | None = None
    pace: float | None = None
    say_voice: str | None = None
    seed: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in LANG_FIELDS if getattr(self, k) is not None}


def _no_langs() -> Mapping[str, LangVoice]:
    return MappingProxyType({})


def lang_code(value: Any) -> str | None:
    """An ISO 639-1 code ("zh") for an ISO code, a regional code ("zh-CN") or
    a Qwen language name ("chinese"); None for anything else."""
    from .voices import ISO_TO_QWEN

    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-")
    if v in ISO_TO_QWEN:
        return v
    for code, name in ISO_TO_QWEN.items():
        if v == name:
            return code
    base = v.split("-", 1)[0]
    return base if base in ISO_TO_QWEN and base != v else None


@dataclass(frozen=True)
class Profile:
    name: str
    voice: str = ""
    ref_text: str = ""
    lang: str = "en"
    pace: float = 1.0
    style: str = ""
    max_chars: int = common.DEFAULT_MAX_CHARS
    say_voice: str = ""
    seed: int | None = None  # fixed sampling seed: the same line always sounds the same
    temperature: float | None = None  # None: the model's default
    persona: str = ""  # free text, only for `afterword alerts generate`
    known: bool = True  # False when the name was not in config.toml
    pronounce: _pron.Pairs = ()  # [profiles.x.pronounce]: (word, spoken as) pairs
    # [profiles.x.langs.<iso>]: a voice per language (read-only mapping)
    langs: Mapping[str, LangVoice] = field(default_factory=_no_langs, hash=False)


@dataclass(frozen=True)
class DaemonSettings:
    port: int = common.DEFAULT_PORT
    fallback: str = "say"
    announce_session: bool = False
    synth: str = "qwen"
    player: str = "afplay"
    stream: bool = True  # stream to the device as the engine renders (v1: whole WAV, then afplay)
    early_say: bool = True  # MessageDisplay: speak <say> blocks while Claude is still writing
    model_fallback: str = "apple"  # a failed model voice: "apple" (then say), "say" or "none"
    max_queue: int = 16
    history: int = 50


@dataclass(frozen=True)
class QwenSettings:
    custom_voice: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit"
    base: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit"
    voice_design: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit"
    keep_loaded: int = 2
    warm_on_start: bool = True
    # [models]: the other local models (the engine hosts them with Qwen3-TTS)
    kokoro: str = "mlx-community/Kokoro-82M-bf16"
    chatterbox: str = "mlx-community/chatterbox-multilingual-v3"

    def repos(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in QWEN_MODEL_KINDS + OTHER_MODEL_KINDS}


ALERT_EVENTS = ("permission", "input", "idle", "done")
PERMISSION_WHEN = ("away", "now")


@dataclass(frozen=True)
class AlertSettings:
    """[alerts]: spoken alerts (docs/design-v2.md, C)."""

    enabled: bool = False
    profile: str = ""  # "" = the session's profile if its voice is on, else the first profile
    events: tuple[str, ...] = ("permission", "input", "done")
    done_after_s: float = 60.0
    permission_when: str = "away"

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "profile": self.profile, "events": list(self.events),
                "done_after_s": self.done_after_s, "permission_when": self.permission_when}


DEFAULT_VOICE = "builtin:Ryan"


@dataclass(frozen=True)
class Config:
    daemon: DaemonSettings = field(default_factory=DaemonSettings)
    qwen: QwenSettings = field(default_factory=QwenSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    profiles: dict[str, Profile] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    pronounce: _pron.Pairs = ()  # global [pronounce] table

    def pronunciations(self, profile: Profile | str | None) -> _pron.Pairs:
        """The [pronounce] table for one profile: its own entries over the
        global ones (an unknown name borrows the first profile's, like its voice)."""
        p = profile if isinstance(profile, Profile) else self.profile(profile)
        return _pron.merge(self.pronounce, p.pronounce)

    def speakable(self, text: str, profile: Profile | str | None) -> str:
        """`text` as it should be spoken, after the pronunciation list."""
        return _pron.apply(text, self.pronunciations(profile))

    def voice_for(self, profile: Profile | str | None, lang: str | None) -> Profile:
        """The effective settings for one segment in `lang`: the profile with
        that language's [profiles.x.langs.<lang>] fields over its own, and
        `lang` set. A language without its own entry (or "auto"/None) gets
        the profile unchanged, except for `lang` and `say_voice`. A language
        voice that is given its own `voice` does not inherit the profile's
        `ref_text` (the transcript of a different clip). `say_voice` follows
        `scriptsplit.say_voice_for`: a zh/ja/ko segment of a profile with
        another primary language does not inherit the profile's say voice
        (which would read it as silence) but gets a built-in voice."""
        p = profile if isinstance(profile, Profile) else self.profile(profile)
        code = lang_code(lang)
        lv = p.langs.get(code) if code else None
        if lv is None:
            if not code or code == lang_code(p.lang):
                return p
            say = _split.say_voice_for(code, lang_code(p.lang), profile_say_voice=p.say_voice) or ""
            return replace(p, lang=code, say_voice=say)
        ref_text = lv.ref_text if lv.ref_text is not None else ("" if lv.voice else p.ref_text)
        say = _split.say_voice_for(code, lang_code(p.lang), lv.say_voice, lv.voice, p.say_voice) or ""
        return replace(
            p,
            lang=code,
            voice=lv.voice if lv.voice is not None else p.voice,
            ref_text=ref_text,
            style=lv.style if lv.style is not None else p.style,
            pace=lv.pace if lv.pace is not None else p.pace,
            say_voice=say,
            seed=lv.seed if lv.seed is not None else p.seed,
        )

    def profile(self, name: str | None) -> Profile:
        """The named profile. An unknown name borrows the first profile's
        settings (or a built-in default) so the line is still spoken."""
        if name and name in self.profiles:
            return self.profiles[name]
        if self.profiles:
            return replace(next(iter(self.profiles.values())), name=name or "default", known=False)
        return Profile(name=name or "default", voice=DEFAULT_VOICE, known=False)


def _num(value: Any, kind: type, default: Any, lo: float, hi: float, where: str, errors: list[str]) -> Any:
    import math

    if value is None:
        return default
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError
        if kind is int and isinstance(value, float) and not value.is_integer():
            raise ValueError
        n = kind(value)
    except (TypeError, ValueError, OverflowError):
        errors.append(f"{where}: expected a finite {'whole ' if kind is int else ''}number, got {value!r}")
        return default
    if not lo <= n <= hi:
        errors.append(f"{where}: {n} out of range [{lo}, {hi}]")
        return default
    return n


def _str(value: Any, default: str, where: str, errors: list[str]) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        errors.append(f"{where}: expected a string, got {value!r}")
        return default
    return value


def build_config(raw: dict[str, Any]) -> tuple[Config, list[str]]:
    """Validate a parsed TOML dict. Bad values fall back to defaults and are
    reported as warnings, never raised."""
    errors: list[str] = []
    d = raw.get("daemon") if isinstance(raw.get("daemon"), dict) else {}
    defaults = DaemonSettings()
    fallback = _str(d.get("fallback"), defaults.fallback, "daemon.fallback", errors)
    if fallback not in ("say", "none"):
        errors.append(f"daemon.fallback: {fallback!r} is not 'say' or 'none'")
        fallback = defaults.fallback
    synth = _str(d.get("synth"), defaults.synth, "daemon.synth", errors)
    if synth not in SYNTHS:
        errors.append(f"daemon.synth: unknown backend {synth!r} (available: {', '.join(SYNTHS)}); using {defaults.synth!r}")
        synth = defaults.synth
    player = _str(d.get("player"), defaults.player, "daemon.player", errors)
    if player not in PLAYERS:
        errors.append(f"daemon.player: unknown player {player!r} (available: {', '.join(PLAYERS)}); using {defaults.player!r}")
        player = defaults.player
    announce = d.get("announce_session", defaults.announce_session)
    if not isinstance(announce, bool):
        errors.append("daemon.announce_session: expected true/false")
        announce = defaults.announce_session
    stream = d.get("stream", defaults.stream)
    if not isinstance(stream, bool):
        errors.append("daemon.stream: expected true/false")
        stream = defaults.stream
    early_say = d.get("early_say", defaults.early_say)
    if not isinstance(early_say, bool):
        errors.append("daemon.early_say: expected true/false")
        early_say = defaults.early_say
    model_fallback = _str(d.get("model_fallback"), defaults.model_fallback, "daemon.model_fallback", errors)
    if model_fallback not in MODEL_FALLBACKS:
        errors.append(f"daemon.model_fallback: {model_fallback!r} is not one of {', '.join(MODEL_FALLBACKS)}")
        model_fallback = defaults.model_fallback
    daemon = DaemonSettings(
        port=_num(d.get("port"), int, defaults.port, 1, 65535, "daemon.port", errors),
        fallback=fallback,
        announce_session=announce,
        synth=synth,
        player=player,
        stream=stream,
        early_say=early_say,
        model_fallback=model_fallback,
        max_queue=_num(d.get("max_queue"), int, defaults.max_queue, 1, 1000, "daemon.max_queue", errors),
        history=_num(d.get("history"), int, defaults.history, 0, 10000, "daemon.history", errors),
    )

    profiles: dict[str, Profile] = {}
    raw_profiles = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else {}
    for name, p in raw_profiles.items():
        where = f"profiles.{name}"
        if not isinstance(p, dict):
            errors.append(f"{where}: expected a table")
            continue
        if not common_profile_name_ok(name):
            errors.append(f"{where}: invalid profile name")
            continue
        base = Profile(name=name)
        profiles[name] = Profile(
            name=name,
            voice=_str(p.get("voice"), base.voice, f"{where}.voice", errors),
            ref_text=_str(p.get("ref_text"), base.ref_text, f"{where}.ref_text", errors),
            lang=_str(p.get("lang"), base.lang, f"{where}.lang", errors),
            pace=_num(p.get("pace"), float, base.pace, 0.5, 2.0, f"{where}.pace", errors),
            style=_str(p.get("style"), base.style, f"{where}.style", errors),
            max_chars=_num(p.get("max_chars"), int, base.max_chars, 20, 5000, f"{where}.max_chars", errors),
            say_voice=_str(p.get("say_voice"), base.say_voice, f"{where}.say_voice", errors),
            seed=_num(p.get("seed"), int, None, 0, 2**31 - 1, f"{where}.seed", errors),
            temperature=_num(p.get("temperature"), float, None, 0.05, 2.0, f"{where}.temperature", errors),
            persona=_str(p.get("persona"), base.persona, f"{where}.persona", errors)[:2000],
            pronounce=_pron.clean_table(p.get("pronounce"), errors, f"{where}.pronounce"),
            langs=_langs(p.get("langs"), f"{where}.langs", errors),
        )
        _check_voice(profiles[name].voice, f"{where}.voice", errors)

    q = raw.get("qwen") if isinstance(raw.get("qwen"), dict) else {}
    m = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    qd = QwenSettings()
    warm = q.get("warm_on_start", qd.warm_on_start)
    if not isinstance(warm, bool):
        errors.append("qwen.warm_on_start: expected true/false")
        warm = qd.warm_on_start
    qwen = QwenSettings(
        custom_voice=_str(q.get("custom_voice"), qd.custom_voice, "qwen.custom_voice", errors),
        base=_str(q.get("base"), qd.base, "qwen.base", errors),
        voice_design=_str(q.get("voice_design"), qd.voice_design, "qwen.voice_design", errors),
        keep_loaded=_num(q.get("keep_loaded"), int, qd.keep_loaded, 1, 3, "qwen.keep_loaded", errors),
        warm_on_start=warm,
        kokoro=_str(m.get("kokoro"), qd.kokoro, "models.kokoro", errors),
        chatterbox=_str(m.get("chatterbox"), qd.chatterbox, "models.chatterbox", errors),
    )
    glob = _pron.clean_table(raw.get("pronounce"), errors, "pronounce")
    alerts = _alerts(raw.get("alerts"), profiles, errors)
    return Config(daemon=daemon, qwen=qwen, alerts=alerts, profiles=profiles, raw=raw, pronounce=glob), errors


def _alerts(raw: Any, profiles: dict[str, Profile], errors: list[str]) -> AlertSettings:
    """Validate [alerts]. A bad value is the default, with a warning."""
    d = AlertSettings()
    if raw is None:
        return d
    if not isinstance(raw, dict):
        errors.append("alerts: expected a table")
        return d
    for extra in sorted(set(raw) - {"enabled", "profile", "events", "done_after_s", "permission_when"}):
        errors.append(f"alerts.{extra}: unknown field ignored")
    enabled = raw.get("enabled", d.enabled)
    if not isinstance(enabled, bool):
        errors.append("alerts.enabled: expected true/false")
        enabled = d.enabled
    profile = _str(raw.get("profile"), d.profile, "alerts.profile", errors).strip()
    if profile and profiles and profile not in profiles:
        errors.append(f"alerts.profile: no profile named {profile!r}; using the session's (or the first) profile")
    events = raw.get("events", list(d.events))
    if not isinstance(events, list) or not all(isinstance(e, str) for e in events):
        errors.append(f"alerts.events: expected a list of event names ({', '.join(ALERT_EVENTS)})")
        events = list(d.events)
    clean: list[str] = []
    for e in events:
        if e not in ALERT_EVENTS:
            errors.append(f"alerts.events: unknown event {e!r} (known: {', '.join(ALERT_EVENTS)})")
        elif e not in clean:
            clean.append(e)
    when = _str(raw.get("permission_when"), d.permission_when, "alerts.permission_when", errors)
    if when not in PERMISSION_WHEN:
        errors.append(f"alerts.permission_when: {when!r} is not 'away' or 'now'")
        when = d.permission_when
    return AlertSettings(
        enabled=enabled,
        profile=profile,
        events=tuple(clean),
        done_after_s=float(_num(raw.get("done_after_s"), float, d.done_after_s, 0, 86400, "alerts.done_after_s", errors)),
        permission_when=when,
    )


def _check_voice(voice: str | None, where: str, errors: list[str]) -> None:
    if voice:
        from .voices import VoiceError, parse_voice

        try:
            parse_voice(voice)
        except VoiceError as exc:
            errors.append(f"{where}: {exc}")


def _langs(raw: Any, where: str, errors: list[str]) -> Mapping[str, LangVoice]:
    """Validate [profiles.x.langs.*]. Keys are normalised to ISO codes; an
    unknown language, a duplicate, or a non-table is skipped with a warning,
    a bad field falls back to inheriting."""
    if raw is None:
        return _no_langs()
    if not isinstance(raw, dict):
        errors.append(f"{where}: expected a table of languages ([{where}.<lang>])")
        return _no_langs()
    out: dict[str, LangVoice] = {}
    for key, table in raw.items():
        at = f"{where}.{key}"
        code = lang_code(key)
        if code is None:
            errors.append(f"{at}: unknown language {key!r} (use an ISO code such as zh, or a name such as chinese)")
            continue
        if code in out:
            errors.append(f"{at}: {code!r} is already set; ignored")
            continue
        if not isinstance(table, dict):
            errors.append(f"{at}: expected a table")
            continue
        for extra in sorted(set(table) - set(LANG_FIELDS)):
            errors.append(f"{at}.{extra}: unknown field ignored (allowed: {', '.join(LANG_FIELDS)})")
        voice = _str(table.get("voice"), None, f"{at}.voice", errors) or None
        _check_voice(voice, f"{at}.voice", errors)
        out[code] = LangVoice(
            voice=voice,
            ref_text=_str(table.get("ref_text"), None, f"{at}.ref_text", errors),
            style=_str(table.get("style"), None, f"{at}.style", errors),
            pace=_num(table.get("pace"), float, None, 0.5, 2.0, f"{at}.pace", errors),
            say_voice=_str(table.get("say_voice"), None, f"{at}.say_voice", errors),
            seed=_num(table.get("seed"), int, None, 0, 2**31 - 1, f"{at}.seed", errors),
        )
    return MappingProxyType(out)


def common_profile_name_ok(name: str) -> bool:
    from afterword.hooks.tags import PROFILE_NAME_RE

    return bool(PROFILE_NAME_RE.match(name))


class ConfigStore:
    """Holds the current Config; reloads when the file's mtime changes.
    A broken file keeps the last good config and records the error."""

    def __init__(self, path: Path | None = None):
        self.path = path or common.config_path()
        self._mtime: float | None = None
        self.config, self.warnings = build_config({})
        self.error: str | None = None
        self.loaded_at: float | None = None
        self._callbacks: list = []
        self.reload(force=True)

    def ensure_default_file(self) -> bool:
        if self.path.exists():
            return False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_CONFIG_TOML)
        self.reload(force=True)
        return True

    def _current_mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    def on_reload(self, callback) -> None:
        """Call `callback(config)` after every successful (re)load."""
        self._callbacks.append(callback)

    def reload(self, force: bool = False) -> bool:
        """Reload if changed (or forced). Returns True if a reload happened.
        Any failure keeps the last good config and records the error."""
        import time

        mtime = self._current_mtime()
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            raw = common.read_config_raw(self.path)
            config, warnings = build_config(raw)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("config.toml not loaded, keeping previous config: %s", self.error)
            return True
        self.config, self.warnings = config, warnings
        self.error = None
        self.loaded_at = time.time()
        for w in self.warnings:
            log.warning("config: %s", w)
        log.info("config loaded from %s (%d profiles)", self.path, len(self.config.profiles))
        for callback in list(self._callbacks):
            try:
                callback(self.config)
            except Exception:
                log.exception("config reload callback failed")
        return True

    def get(self) -> Config:
        self.reload()
        return self.config
