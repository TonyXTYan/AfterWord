"""Spoken alerts, the daemon side (docs/design-v2.md, C): phrase pools in
~/.afterword/alerts.json with built-in English and Chinese defaults,
placeholders, the random pick that avoids the last 3, the "LLM random"
generator (`claude -p --model haiku`), and the [alerts] writer for the GUI.

The decision whether an alert speaks is in server.Daemon (ingest_alert).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import random
import re
import signal
import subprocess
import tempfile
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .config import ALERT_EVENTS, PERMISSION_WHEN, Config, Profile, lang_code

log = logging.getLogger("afterword.alerts")

FILE_NAME = "alerts.json"
MAX_WAIT_S = 30.0  # an alert that waited longer in the queue is dropped
RECENT = 3  # a pick avoids the last 3 phrases used for that event
PHRASE_MAX = 120
POOL_MAX = 60  # phrases kept per language and event
LANGS_MAX = 12
FILE_MAX_BYTES = 512 * 1024
COUNT_MAX = 30  # phrases asked for per event
GENERATE_TIMEOUT_S = 180.0

# Placeholders each event's phrases may use.
PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "permission": ("project", "action", "tool"),
    "input": ("project",),
    "idle": ("project",),
    "done": ("project", "minutes"),
}
KNOWN = ("project", "action", "tool", "minutes")
_TOKEN_RE = re.compile(r"\{([^{}]*)\}")

BUILTIN: dict[str, dict[str, list[str]]] = {
    "en": {
        "permission": [
            "{project} needs your OK to {action}.",
            "{project} is waiting for your approval to {action}.",
            "Permission needed in {project}: {action}.",
            "{project} wants to use {tool}. Your call.",
            "Quick approval needed in {project}.",
        ],
        "input": [
            "{project} needs your input.",
            "{project} has a question for you.",
            "Claude is waiting for your answer in {project}.",
            "Input needed in {project}.",
        ],
        "idle": [
            "{project} is waiting for you.",
            "{project} is ready when you are.",
            "Still there? {project} is waiting.",
            "{project} is idle, waiting for your next prompt.",
        ],
        "done": [
            "{project} is done.",
            "{project} finished after {minutes} minutes.",
            "Done in {project}. Your turn.",
            "All done in {project}.",
            "{project} has finished its work.",
        ],
    },
    "zh": {
        "permission": [
            "{project} 需要你批准：{action}。",
            "{project} 在等你确认：{action}。",
            "{project} 想用 {tool}，等你点头。",
            "{project} 需要你点一下同意。",
            "{project} 请求权限，等你处理。",
        ],
        "input": [
            "{project} 需要你回答一个问题。",
            "{project} 在等你的输入。",
            "{project} 有问题要问你。",
            "{project} 需要你来决定一下。",
        ],
        "idle": [
            "{project} 在等你。",
            "{project} 已经准备好了，等你下一步。",
            "还在吗？{project} 在等你。",
            "{project} 闲着呢，等你的新指令。",
        ],
        "done": [
            "{project} 完成了。",
            "{project} 忙了 {minutes} 分钟，做完了。",
            "{project} 搞定了，轮到你了。",
            "{project} 的任务完成了。",
            "{project} 这边好了，来看看吧。",
        ],
    },
}

# What "hear one" fills in.
SAMPLE_VALUES = {
    "en": {"project": "my-project", "action": "run the test suite", "tool": "Bash", "minutes": "3"},
    "zh": {"project": "my-project", "action": "运行测试", "tool": "Bash", "minutes": "3"},
}


class AlertError(ValueError):
    pass


class GenerateFailed(AlertError):
    """claude could not be run, or its reply was unusable."""


def placeholders_of(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def check_phrase(text: Any, event: str) -> str | None:
    """Why `text` is not a usable phrase for `event`, or None."""
    if not isinstance(text, str):
        return "not a string"
    t = text.strip()
    if not t:
        return "empty"
    if len(t) > PHRASE_MAX:
        return f"longer than {PHRASE_MAX} characters"
    if "\n" in t or "\r" in t:
        return "more than one line"
    allowed = PLACEHOLDERS.get(event, ())
    for name in placeholders_of(t):
        if name not in KNOWN:
            return f"unknown placeholder {{{name}}}"
        if name not in allowed:
            return f"{{{name}}} is not available for {event} (use {', '.join('{%s}' % a for a in allowed)})"
    if "{" in _TOKEN_RE.sub("", t) or "}" in _TOKEN_RE.sub("", t):
        return "unbalanced { }"
    return None


def clean_pools(raw: Any, where: str, warnings: list[str]) -> dict[str, dict[str, list[str]]]:
    """{lang: {event: [phrase, ...]}} with every bad entry dropped (and a
    warning). Languages are ISO codes; events the known four."""
    out: dict[str, dict[str, list[str]]] = {}
    if not isinstance(raw, dict):
        if raw is not None:
            warnings.append(f"{where}: expected an object of languages")
        return out
    for key, events in raw.items():
        code = lang_code(key)
        at = f"{where}.{key}"
        if code is None:
            warnings.append(f"{at}: unknown language {str(key)[:32]!r}")
            continue
        if code in out:
            warnings.append(f"{at}: {code!r} is given twice; ignored")
            continue
        if len(out) >= LANGS_MAX:
            warnings.append(f"{at}: more than {LANGS_MAX} languages; ignored")
            continue
        if not isinstance(events, dict):
            warnings.append(f"{at}: expected an object of events")
            continue
        pools: dict[str, list[str]] = {}
        for event, phrases in events.items():
            if event not in ALERT_EVENTS:
                warnings.append(f"{at}.{str(event)[:32]}: unknown event (known: {', '.join(ALERT_EVENTS)})")
                continue
            if not isinstance(phrases, list):
                warnings.append(f"{at}.{event}: expected a list of phrases")
                continue
            kept: list[str] = []
            for i, p in enumerate(phrases):
                why = check_phrase(p, event)
                if why:
                    warnings.append(f"{at}.{event}[{i}]: {why}")
                elif p.strip() not in kept:
                    kept.append(p.strip())
            if len(kept) > POOL_MAX:
                warnings.append(f"{at}.{event}: more than {POOL_MAX} phrases; the first {POOL_MAX} are used")
                kept = kept[:POOL_MAX]
            pools[event] = kept
        out[code] = pools
    return out


def fill(template: str, values: dict[str, str], lang: str | None = None) -> str:
    """The phrase with its placeholders filled in. English: an action that
    starts with a capitalised word reads "to remove …", not "to Remove …"."""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        value = str(values.get(name) or "")
        if name == "action" and lang == "en" and len(value) > 1 and value[0].isupper() and value[1].islower():
            value = value[0].lower() + value[1:]
        return value

    text = _TOKEN_RE.sub(sub, template)
    if lang == "en":
        text = re.sub(r"\b1 minutes\b", "1 minute", text)
    return re.sub(r"\s+", " ", text).strip()


class AlertPhrases:
    """alerts.json, re-read when it changes; the random pick."""

    def __init__(self, path: Path, rng: random.Random | None = None):
        self.path = path
        self.rng = rng or random.Random()
        self._mtime: float | None = -1.0
        self.file_pools: dict[str, dict[str, list[str]]] = {}
        self.warnings: list[str] = []
        self.error: str | None = None
        self._recent: dict[tuple[str, str], deque] = {}
        self._lock = threading.RLock()
        self.reload()

    # -- the file -------------------------------------------------------

    def reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if not force and mtime == self._mtime:
            return
        self._mtime = mtime
        if mtime is None:
            self.file_pools, self.warnings, self.error = {}, [], None
            return
        warnings: list[str] = []
        try:
            from afterword.hooks import common

            raw = json.loads(common.read_regular_file(self.path, FILE_MAX_BYTES).decode("utf-8"))
        except (OSError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            log.error("%s not loaded, keeping the previous phrases: %s", self.path, self.error)
            return
        self.file_pools = clean_pools(raw, FILE_NAME, warnings)
        self.warnings, self.error = warnings, None
        for w in warnings:
            log.warning("alerts: %s", w)

    def pools(self) -> dict[str, dict[str, list[str]]]:
        """The pools in use: the file's, and the built-in ones for every
        language and event the file leaves empty."""
        self.reload()
        out: dict[str, dict[str, list[str]]] = {}
        for lang in list(BUILTIN) + [k for k in self.file_pools if k not in BUILTIN]:
            own = self.file_pools.get(lang, {})
            out[lang] = {e: list(own.get(e) or BUILTIN.get(lang, {}).get(e, [])) for e in ALERT_EVENTS}
        return out

    def pool(self, lang: str, event: str) -> list[str]:
        return self.pools().get(lang, {}).get(event, [])

    def pick(self, event: str, lang: str | None, values: dict[str, str]) -> tuple[str, str, str]:
        """(text, template, language) for `event`: a random phrase of the
        language's pool (English when it has none) whose placeholders all
        have values, never one of the last RECENT used for that event while
        the pool has others."""
        with self._lock:
            have = {k for k, v in values.items() if v}
            code, pool = "en", []
            for c in dict.fromkeys(([lang] if lang else []) + ["en"]):
                pool = [p for p in self.pool(c, event) if set(placeholders_of(p)) <= have]
                if not pool:  # e.g. every phrase needs {action} and there is none
                    pool = [p for p in BUILTIN.get(c, {}).get(event, []) if set(placeholders_of(p)) <= have]
                if pool:
                    code = c
                    break
            if not pool:
                code, pool = "en", ["{project} needs you."]
            recent = self._recent.setdefault((code, event), deque(maxlen=RECENT))
            # Never one of the last RECENT; a pool of RECENT or fewer avoids
            # as many as it can (all but one).
            avoid = list(recent)[-min(RECENT, len(pool) - 1):] if len(pool) > 1 else []
            fresh = [p for p in pool if p not in avoid] or pool
            template = self.rng.choice(fresh)
            recent.append(template)
            return fill(template, values, code), template, code

    # -- writes ---------------------------------------------------------

    @contextlib.contextmanager
    def _file_lock(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._lock, open(self.path.with_name(FILE_NAME + ".lock"), "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read_file(self) -> dict[str, Any]:
        from afterword.hooks import common

        try:
            raw = json.loads(common.read_regular_file(self.path, FILE_MAX_BYTES).decode("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise AlertError(f"{self.path} is not readable JSON ({exc}); fix or remove it first") from None
        return raw if isinstance(raw, dict) else {}

    def _write_file(self, data: dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=1) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".alerts-", suffix=".json", dir=str(self.path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self.reload(force=True)

    def save_lang(self, lang: str, pools: dict[str, list[str]] | None) -> dict[str, list[str]]:
        """Replace one language's pools in the file (None or {}: remove the
        language, so the built-in pools apply again). Every phrase must be
        valid; an event left out (or empty) uses the built-in pool."""
        code = lang_code(lang)
        if code is None:
            raise AlertError(f"unknown language {str(lang)[:32]!r}")
        clean: dict[str, list[str]] = {}
        for event, phrases in (pools or {}).items():
            if event not in ALERT_EVENTS:
                raise AlertError(f"unknown event {str(event)[:32]!r} (known: {', '.join(ALERT_EVENTS)})")
            if not isinstance(phrases, list):
                raise AlertError(f"{event}: expected a list of phrases")
            kept: list[str] = []
            for i, p in enumerate(phrases):
                why = check_phrase(p, event)
                if why:
                    raise AlertError(f"{event} phrase {i + 1}: {why}")
                if p.strip() not in kept:
                    kept.append(p.strip())
            if len(kept) > POOL_MAX:
                raise AlertError(f"{event}: at most {POOL_MAX} phrases")
            if kept:
                clean[event] = kept
        with self._file_lock():
            data = self._read_file()
            for key in [k for k in data if lang_code(k) == code]:
                del data[key]
            if clean:
                data[code] = clean
            self._write_file(data)
        return clean

    def merge(self, lang: str, new: dict[str, list[str]]) -> dict[str, list[str]]:
        """Add phrases (already valid) to one language's pools in the file,
        starting from the pools in use (built-in ones included). Returns the
        phrases that were new."""
        code = lang_code(lang) or "en"
        added: dict[str, list[str]] = {}
        with self._file_lock():
            data = self._read_file()
            existing = {}
            for key in [k for k in data if lang_code(k) == code]:
                if isinstance(data[key], dict):
                    existing = data[key]
                del data[key]
            current = self.pools().get(code, {})
            merged: dict[str, list[str]] = {}
            for event in ALERT_EVENTS:
                base = existing.get(event) if isinstance(existing.get(event), list) else None
                pool = [p for p in (base if base else current.get(event, [])) if check_phrase(p, event) is None]
                fresh = [p for p in new.get(event, []) if p not in pool]
                added[event] = fresh
                merged[event] = (pool + fresh)[-POOL_MAX:]
            data[code] = merged
            self._write_file(data)
        return added


# --------------------------------------------------------------------------- #
# "LLM random": claude -p --model haiku                                       #
# --------------------------------------------------------------------------- #

Runner = Callable[[list, float], "subprocess.CompletedProcess"]


def language_name(code: str) -> str:
    from .voices import LANGUAGE_NAMES

    return LANGUAGE_NAMES.get(code, "English")


def build_prompt(profile: Profile, lang: str, count: int, persona: str | None = None) -> str:
    persona = (profile.persona if persona is None else persona).strip()
    lines = [
        "You write short spoken alerts for AfterWord, a text-to-speech companion for the Claude Code CLI.",
        f"A voice speaks them aloud when the user is away from the terminal. Voice profile: {profile.name!r}.",
        f"Language: {language_name(lang)}. Write every phrase in {language_name(lang)}.",
    ]
    if profile.style:
        lines.append(f"Speaking style: {profile.style}.")
    if persona:
        lines.append(f"Persona (stay in character): {persona}")
    lines += [
        "",
        f"Write {count} different phrases for each of these events:",
        "- permission: Claude needs the user to approve a tool call. Placeholders: {project} (the project "
        "folder name), {action} (what the tool wants to do, e.g. \"run the test suite\"), {tool} (the tool name).",
        "- input: Claude asks the user a question or needs input. Placeholder: {project}.",
        "- idle: Claude finished a while ago and is waiting for the next prompt. Placeholder: {project}.",
        "- done: a long task just finished. Placeholders: {project}, {minutes} (a whole number of minutes).",
        "",
        "Rules: each phrase is one short spoken sentence or two, at most 100 characters; plain text, no "
        "markdown, no emoji, no quotes around it; use only the placeholders listed for that event, written "
        "exactly like {project}; most phrases should mention {project}; vary the wording.",
        "Reply with only a JSON object, no prose and no code fence: "
        '{"permission": [...], "input": [...], "idle": [...], "done": [...]}',
    ]
    return "\n".join(lines)


def claude_argv(prompt: str) -> list[str]:
    exe = os.environ.get("AFTERWORD_CLAUDE_CMD") or "claude"
    return [exe, "-p", "--model", "haiku", "--output-format", "json", "--tools", "",
            "--no-session-persistence", prompt]


_RUNNING: dict = {}  # claude processes started by default_runner -> cancelled
_RUNNING_LOCK = threading.Lock()


def default_runner(argv: list, timeout: float) -> subprocess.CompletedProcess:
    """Run claude in its own process group, so cancel_running() (daemon
    shutdown) can end it and whatever it started."""
    env = dict(os.environ)
    env["AFTERWORD_DISABLE"] = "1"  # our own hooks stay out of this call
    with _RUNNING_LOCK:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, env=env, cwd=tempfile.gettempdir(), start_new_session=True)
        _RUNNING[proc] = False
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill(proc)
            proc.communicate()
            raise
        with _RUNNING_LOCK:
            if _RUNNING.get(proc):
                raise GenerateFailed("cancelled: the daemon is shutting down")
        return subprocess.CompletedProcess(argv, proc.returncode, out, err)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.pop(proc, None)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(OSError):
            proc.kill()


def cancel_running() -> int:
    """Kill every claude run of default_runner (daemon shutdown: the
    executor thread waiting on it can then end, and its generate() fails
    instead of merging into alerts.json behind the next daemon's back)."""
    with _RUNNING_LOCK:
        procs = list(_RUNNING)
        for proc in procs:
            _RUNNING[proc] = True
    for proc in procs:
        _kill(proc)
    return len(procs)


def parse_reply(stdout: str) -> dict[str, Any]:
    """The pools object from `claude -p --output-format json` output (the
    envelope's `result` text, which may wrap the JSON in a code fence)."""
    try:
        envelope = json.loads(stdout)
    except ValueError:
        raise GenerateFailed(f"claude did not return JSON: {stdout.strip()[:200]!r}") from None
    if not isinstance(envelope, dict):
        raise GenerateFailed("claude returned an unexpected JSON value")
    if envelope.get("is_error"):
        raise GenerateFailed(f"claude reported an error: {str(envelope.get('result') or envelope.get('subtype'))[:300]}")
    result = envelope.get("result")
    if isinstance(result, dict):
        data = result
    else:
        if not isinstance(result, str):
            raise GenerateFailed("claude's reply has no result text")
        start, end = result.find("{"), result.rfind("}")
        if start < 0 or end <= start:
            raise GenerateFailed(f"no JSON object in claude's reply: {result.strip()[:200]!r}")
        try:
            data = json.loads(result[start:end + 1])
        except ValueError as exc:
            raise GenerateFailed(f"claude's reply is not valid JSON ({exc})") from None
    if isinstance(data, dict) and isinstance(data.get("pools"), dict):
        data = data["pools"]
    if isinstance(data, dict) and len(data) == 1:  # {"zh": {...}}
        (only,) = data.values()
        if isinstance(only, dict) and set(only) & set(ALERT_EVENTS):
            data = only
    if not isinstance(data, dict) or not set(data) & set(ALERT_EVENTS):
        raise GenerateFailed("claude's reply has none of the events permission, input, idle, done")
    return data


def validate_generated(data: dict[str, Any], count: int) -> tuple[dict[str, list[str]], list[str]]:
    """(kept phrases per event, rejected "event: phrase (why)" notes)."""
    kept: dict[str, list[str]] = {}
    rejected: list[str] = []
    for event in ALERT_EVENTS:
        phrases = data.get(event)
        if not isinstance(phrases, list):
            continue
        out: list[str] = []
        for p in phrases[: count * 2]:
            text = p.strip().strip('"“”') if isinstance(p, str) else p
            why = check_phrase(text, event)
            if why:
                rejected.append(f"{event}: {str(p)[:80]!r} ({why})")
            elif text not in out:
                out.append(text)
        kept[event] = out[:count]
    return kept, rejected


def generate(cfg: Config, phrases: AlertPhrases, profile_name: str | None = None, count: int = 12,
             persona: str | None = None, runner: Runner | None = None,
             timeout: float = GENERATE_TIMEOUT_S) -> dict[str, Any]:
    """Ask claude (haiku) for phrase pools in the profile's language, check
    them, and merge them into alerts.json. Raises AlertError."""
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= COUNT_MAX:
        raise AlertError(f"count must be a whole number from 1 to {COUNT_MAX}")
    if persona is not None and (not isinstance(persona, str) or len(persona) > 2000):
        raise AlertError("persona must be text of at most 2000 characters")
    if profile_name is not None and not isinstance(profile_name, str):
        raise AlertError("profile must be a profile name")
    name = profile_name or alert_profile_name(cfg, None)
    if not name or name not in cfg.profiles:
        raise AlertError(f"no profile named {name!r}" if name else "no profiles configured")
    profile = cfg.profiles[name]
    lang = pool_lang(profile)
    prompt = build_prompt(profile, lang, count, persona)
    argv = claude_argv(prompt)
    run = runner or default_runner
    try:
        proc = run(argv, timeout)
    except FileNotFoundError:
        raise GenerateFailed("the claude command was not found (is Claude Code installed and on PATH?)") from None
    except subprocess.TimeoutExpired:
        raise GenerateFailed(f"claude did not answer within {int(timeout)} s") from None
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        raise GenerateFailed(f"claude failed (exit {proc.returncode}): {(proc.stderr or '').strip()[:300]}")
    data = parse_reply(proc.stdout or "")
    kept, rejected = validate_generated(data, count)
    if not any(kept.values()):
        raise GenerateFailed("claude's phrases were all rejected: " + "; ".join(rejected[:5]))
    added = phrases.merge(lang, kept)
    return {
        "profile": name, "lang": lang, "count": count, "persona": (profile.persona if persona is None else persona),
        "added": added, "rejected": rejected, "pools": phrases.pools().get(lang, {}),
    }


# --------------------------------------------------------------------------- #
# Which profile and language                                                  #
# --------------------------------------------------------------------------- #


def alert_profile_name(cfg: Config, session_profile: str | None) -> str | None:
    """[alerts] profile, else the session's profile (voice on), else the
    first profile."""
    a = cfg.alerts.profile
    if a and a in cfg.profiles:
        return a
    if session_profile and session_profile in cfg.profiles:
        return session_profile
    return next(iter(cfg.profiles), None)


def pool_lang(profile: Profile) -> str:
    """The language whose pool a profile speaks: its primary language
    (English for "auto" or an unknown one)."""
    return lang_code(profile.lang) or "en"


# --------------------------------------------------------------------------- #
# [alerts] writes (Config page)                                               #
# --------------------------------------------------------------------------- #


def settings_body(body: Any) -> dict[str, Any]:
    """Check a POST /api/alerts body: any of enabled, events, profile,
    done_after_s, permission_when."""
    if not isinstance(body, dict) or not body:
        raise AlertError("send any of enabled, events, profile, done_after_s, permission_when")
    extra = sorted(set(body) - {"enabled", "events", "profile", "done_after_s", "permission_when"})
    if extra:
        raise AlertError(f"unknown field {extra[0]!r}")
    out: dict[str, Any] = {}
    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise AlertError("enabled must be true or false")
        out["enabled"] = body["enabled"]
    if "events" in body:
        ev = body["events"]
        if not isinstance(ev, list) or not all(isinstance(e, str) and e in ALERT_EVENTS for e in ev):
            raise AlertError(f"events must be a list of {', '.join(ALERT_EVENTS)}")
        out["events"] = [e for e in ALERT_EVENTS if e in ev]
    if "profile" in body:
        if not isinstance(body["profile"], str) or len(body["profile"]) > 64:
            raise AlertError("profile must be a profile name or \"\"")
        out["profile"] = body["profile"].strip()
    if "done_after_s" in body:
        v = body["done_after_s"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 86400:
            raise AlertError("done_after_s must be a number of seconds from 0 to 86400")
        out["done_after_s"] = int(v) if float(v).is_integer() else float(v)
    if "permission_when" in body:
        if body["permission_when"] not in PERMISSION_WHEN:
            raise AlertError("permission_when must be \"away\" or \"now\"")
        out["permission_when"] = body["permission_when"]
    return out


def write_settings(path: Path, fields: dict[str, Any], known_profiles: list[str]) -> None:
    """Set keys of [alerts] in config.toml (comments kept; the table is added
    when missing). Holds lab.CONFIG_WRITE_LOCK; refuses a result that does
    not read back the same."""
    import tomllib

    import tomlkit

    from .lab import CONFIG_WRITE_LOCK, _load_doc, _write_doc

    if fields.get("profile") and fields["profile"] not in known_profiles:
        raise AlertError(f"no profile named {fields['profile']!r}")
    with CONFIG_WRITE_LOCK:
        doc = _load_doc(path)
        table = doc.get("alerts")
        if table is None or not hasattr(table, "keys"):
            if table is not None:
                del doc["alerts"]
            doc["alerts"] = tomlkit.table()
            table = doc["alerts"]
        for key, value in fields.items():
            table[key] = value
        got = tomllib.loads(tomlkit.dumps(doc)).get("alerts", {})
        for key, value in fields.items():
            if got.get(key) != value:
                raise AlertError(f"alerts.{key} did not read back the same; config.toml not changed")
        _write_doc(path, doc)
