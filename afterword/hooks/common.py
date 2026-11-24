"""Paths, config and per-session state shared by the hook and the daemon.

Stdlib-only and Python 3.9+ compatible (see tags.py for why). Every function
here is defensive: a broken file never raises out, it falls back to defaults.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

try:  # Python 3.11+
    import tomllib as _toml  # type: ignore[import-not-found]
except ImportError:  # Python 3.9/3.10: see _mini_toml below
    _toml = None  # type: ignore[assignment]

SOCKET_NAME = "afterword.sock"
DEFAULT_PORT = 47800
DEFAULT_MAX_CHARS = 400
CONFIG_MAX_BYTES = 1024 * 1024
STATE_MAX_BYTES = 256 * 1024
SPOOL_MAX_BYTES = 10 * 1024 * 1024
HOOK_LOG_MAX_BYTES = 5 * 1024 * 1024

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# --------------------------------------------------------------------------- #
# Paths                                                                       #
# --------------------------------------------------------------------------- #


def home() -> Path:
    """Root directory, ~/.afterword unless AFTERWORD_HOME is set."""
    override = os.environ.get("AFTERWORD_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".afterword"


def ensure_home() -> Path:
    root = home()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def socket_path() -> Path:
    return home() / SOCKET_NAME


def state_dir() -> Path:
    return home() / "state"


def config_path() -> Path:
    return home() / "config.toml"


def spool_path() -> Path:
    """Hook events recorded while the daemon was unreachable."""
    return home() / "spool.jsonl"


def hook_log_path() -> Path:
    return home() / "hook.log"


def safe_session_id(session_id: object) -> str:
    """A filename-safe form of a session id. UUIDs pass through unchanged."""
    sid = session_id if isinstance(session_id, str) else ""
    if _SAFE_ID_RE.match(sid):
        return sid
    if not sid:
        return "unknown"
    return "h-" + hashlib.sha256(sid.encode("utf-8", "replace")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Config                                                                      #
# --------------------------------------------------------------------------- #


def load_config_raw(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read config.toml as a plain dict. Missing or broken file -> {}.

    Raises nothing. Callers that need to report parse errors should use
    `read_config_raw` instead.
    """
    try:
        return read_config_raw(path)
    except Exception:
        return {}


def read_config_raw(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read config.toml. Missing file -> {}. Parse errors propagate."""
    p = path or config_path()
    try:
        data = read_regular_file(p, CONFIG_MAX_BYTES)
    except FileNotFoundError:
        return {}
    text = data.decode("utf-8")
    parsed = _toml.loads(text) if _toml is not None else _mini_toml(text)
    return parsed if isinstance(parsed, dict) else {}


def read_regular_file(path: Path, max_bytes: int) -> bytes:
    """Read at most `max_bytes` of a regular file without ever blocking:
    FIFOs, devices and directories raise OSError instead of hanging."""
    fd = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file: %s" % path)
        chunks = []
        total = 0
        while total < max_bytes:
            chunk = os.read(fd, min(65536, max_bytes - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


# A small TOML subset for interpreters without tomllib (< 3.11). It covers
# what the hook reads: [tables], [dotted.tables], and key = string / integer /
# float / boolean. Anything else on a line is kept as a raw string or skipped,
# so an unusual config degrades to defaults rather than failing the hook.
_MINI_KEY_RE = re.compile(r'^\s*("(?:[^"\\]|\\.)*"|[A-Za-z0-9_-]+)\s*=\s*(.*)$')


def _mini_strip_comment(line: str) -> str:
    quote = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
        i += 1
    return line


def _mini_value(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(raw.replace("_", ""))
    except ValueError:
        return raw


def _mini_toml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    cur = root
    for line in text.splitlines():
        line = _mini_strip_comment(line).strip()
        if not line:
            continue
        if line.startswith("["):
            if line.startswith("[[") or not line.endswith("]"):
                cur = {}  # arrays of tables are unsupported: park their keys
                continue
            cur = root
            for part in line[1:-1].split("."):
                key = part.strip().strip('"')
                nxt = cur.get(key)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[key] = nxt
                cur = nxt
            continue
        m = _MINI_KEY_RE.match(line)
        if m:
            key = m.group(1)
            if key.startswith('"'):
                try:
                    key = json.loads(key)
                except ValueError:
                    key = key[1:-1]
            cur[key] = _mini_value(m.group(2))
    return root


def say_argv() -> list:
    """argv for macOS `say`. AFTERWORD_SAY_CMD overrides it (tests use a stub)."""
    cmd = os.environ.get("AFTERWORD_SAY_CMD")
    return shlex.split(cmd) if cmd else ["/usr/bin/say"]


def fallback_mode(cfg: Dict[str, Any]) -> str:
    """'say' or 'none'. AFTERWORD_FALLBACK overrides the config file."""
    env = os.environ.get("AFTERWORD_FALLBACK")
    value = env if env else _get(cfg, "daemon", "fallback", default="say")
    value = str(value).strip().lower()
    return value if value in ("say", "none") else "say"


def profile_max_chars(cfg: Dict[str, Any], profile: Optional[str]) -> int:
    value = _get(cfg, "profiles", profile or "", "max_chars", default=DEFAULT_MAX_CHARS)
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return n if 20 <= n <= 5000 else DEFAULT_MAX_CHARS


def _get(cfg: Any, *keys: str, default: Any = None) -> Any:
    cur = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


# --------------------------------------------------------------------------- #
# Session state                                                               #
# --------------------------------------------------------------------------- #


def state_file(session_id: object) -> Path:
    return state_dir() / (safe_session_id(session_id) + ".json")


def load_state(session_id: object) -> Dict[str, Any]:
    """Session state. Missing or corrupt -> a fresh 'off' state."""
    base: Dict[str, Any] = {"session_id": session_id, "profile": None}
    try:
        data = json.loads(read_regular_file(state_file(session_id), STATE_MAX_BYTES).decode("utf-8"))
    except Exception:
        return base
    if not isinstance(data, dict):
        return base
    profile = data.get("profile")
    if profile is not None and not isinstance(profile, str):
        data["profile"] = None
    base.update(data)
    return base


def save_state(session_id: object, state: Dict[str, Any]) -> None:
    """Atomically write session state (write temp file, then rename)."""
    d = state_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = dict(state)
    state["session_id"] = session_id
    state["updated"] = time.time()
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, state_file(session_id))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# Append-only JSONL helpers                                                   #
# --------------------------------------------------------------------------- #


def append_jsonl(path: Path, record: Dict[str, Any], max_bytes: int, rotate: bool) -> bool:
    """Append one JSON line. If the file is over `max_bytes`, rotate it to
    `<name>.1` (rotate=True) or drop the record (rotate=False).
    Returns True if written."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        size = path.lstat().st_size
    except FileNotFoundError:
        size = 0
    if size > max_bytes:
        if not rotate:
            return False
        try:
            os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
    line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
    # O_NONBLOCK + O_NOFOLLOW + the S_ISREG check: a FIFO, device or symlink
    # planted at the log path can neither block nor redirect the write.
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        os.write(fd, line.encode("utf-8", "replace"))
    finally:
        os.close(fd)
    return True


def cap_strings(obj: Any, limit: int = 64 * 1024, _depth: int = 0) -> Any:
    """Copy of `obj` with long strings truncated, for logging raw payloads."""
    if _depth > 20:
        return "<too deep>"
    if isinstance(obj, str):
        if len(obj) > limit:
            return obj[:limit] + "…[truncated %d chars]" % (len(obj) - limit)
        return obj
    if isinstance(obj, dict):
        return {str(k)[:200]: cap_strings(v, limit, _depth + 1) for k, v in list(obj.items())[:200]}
    if isinstance(obj, (list, tuple)):
        return [cap_strings(v, limit, _depth + 1) for v in list(obj)[:200]]
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return repr(obj)[:200]
