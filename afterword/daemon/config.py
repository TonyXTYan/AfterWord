"""config.toml: defaults, validation, and mtime-based hot reload."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from afterword.hooks import common

log = logging.getLogger("afterword.config")

SYNTHS = ("say", "null")  # "qwen" joins in build step 4
PLAYERS = ("afplay", "null")

DEFAULT_CONFIG_TOML = """\
# AfterWord config. Edits are picked up automatically (hot reload).

[daemon]
port = 47800
fallback = "say"          # what the hook does when the daemon is down: "say" or "none"
announce_session = false  # speak the session label (cwd folder name) before each message
synth = "say"             # "say" (macOS, used until the Qwen3-TTS backend lands) or "null"
player = "afplay"         # "afplay" or "null"
max_queue = 16            # queued items beyond this are skipped as "daemon busy"
history = 50              # clips kept on disk for replay

[profiles.nova]
voice = "builtin:Chelsie"   # Qwen3-TTS voice, or "clone:~/voices/me.wav"
ref_text = ""               # transcript of the clone clip, if the model needs one
lang = "en"
pace = 1.0
style = "calm, slightly wry"
max_chars = 400
say_voice = "Samantha"      # macOS voice for the "say" synth backend

[profiles.ember]
voice = ""                  # Qwen3-TTS voice, chosen in build step 4
lang = "en"
pace = 1.05
style = "warm, brisk"
max_chars = 400
say_voice = "Daniel"
"""


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
    known: bool = True  # False when the name was not in config.toml


@dataclass(frozen=True)
class DaemonSettings:
    port: int = common.DEFAULT_PORT
    fallback: str = "say"
    announce_session: bool = False
    synth: str = "say"
    player: str = "afplay"
    max_queue: int = 16
    history: int = 50


@dataclass(frozen=True)
class Config:
    daemon: DaemonSettings = field(default_factory=DaemonSettings)
    profiles: dict[str, Profile] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def profile(self, name: str | None) -> Profile:
        if name and name in self.profiles:
            return self.profiles[name]
        return Profile(name=name or "default", known=False)


def _num(value: Any, kind: type, default: Any, lo: float, hi: float, where: str, errors: list[str]) -> Any:
    if value is None:
        return default
    try:
        if isinstance(value, bool):
            raise ValueError
        n = kind(value)
    except (TypeError, ValueError):
        errors.append(f"{where}: expected a number, got {value!r}")
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
    daemon = DaemonSettings(
        port=_num(d.get("port"), int, defaults.port, 1, 65535, "daemon.port", errors),
        fallback=fallback,
        announce_session=announce,
        synth=synth,
        player=player,
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
        )
    return Config(daemon=daemon, profiles=profiles, raw=raw), errors


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

    def reload(self, force: bool = False) -> bool:
        """Reload if changed (or forced). Returns True if a reload happened."""
        import time

        mtime = self._current_mtime()
        if not force and mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            raw = common.read_config_raw(self.path)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("config.toml not loaded, keeping previous config: %s", self.error)
            return True
        self.config, self.warnings = build_config(raw)
        self.error = None
        self.loaded_at = time.time()
        for w in self.warnings:
            log.warning("config: %s", w)
        log.info("config loaded from %s (%d profiles)", self.path, len(self.config.profiles))
        return True

    def get(self) -> Config:
        self.reload()
        return self.config
