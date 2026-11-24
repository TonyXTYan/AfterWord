"""Paths, config and per-session state shared by the hook and the daemon.

Stdlib-only and Python 3.9+ compatible (see tags.py for why). Every function
here is defensive: a broken file never raises out, it falls back to defaults.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import stat
import time
from pathlib import Path
from typing import Any, Dict, Optional

# tomllib (3.11+) and tempfile are imported where they are used: the hooks'
# fast paths (voice off) never need them, and they cost a few ms at startup.

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
    try:  # Python 3.11+
        import tomllib as _toml  # type: ignore[import-not-found]
    except ImportError:  # Python 3.9/3.10: see _mini_toml below
        _toml = None  # type: ignore[assignment]
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
# what the hook reads: [tables], [dotted.tables], bare, "quoted", 'literal'
# and dotted.keys, key = string / integer / float / boolean, arrays of those
# (also over several lines), and one-line { inline = "tables" }. A value it
# cannot read (nested arrays, multi-line strings, dates) leaves the key out, as if it were not set, so an unusual config
# degrades to defaults rather than failing the hook or reading TOML syntax
# as a string.
_MINI_PART = r'(?:"(?:[^"\\]|\\.)*"|\'[^\']*\'|[A-Za-z0-9_-]+)'
_MINI_KEY_RE = re.compile(r'^\s*(' + _MINI_PART + r'(?:\s*\.\s*' + _MINI_PART + r')*)\s*=\s*(.*)$')
_MINI_PART_RE = re.compile(_MINI_PART)
_MINI_SKIP = object()  # a value the subset cannot read: the key is left out


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


_MINI_ESC = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def _mini_basic(body: str) -> Any:
    """The text of a TOML basic string body (between the quotes)."""
    out = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in _MINI_ESC:
            out.append(_MINI_ESC[nxt])
            i += 2
        elif nxt in ("u", "U"):
            width = 4 if nxt == "u" else 8
            digits = body[i + 2:i + 2 + width]
            if len(digits) != width or not all(c in "0123456789abcdefABCDEF" for c in digits):
                return _MINI_SKIP
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                return _MINI_SKIP
            out.append(chr(code))
            i += 2 + width
        else:
            return _MINI_SKIP
    return "".join(out)


def _mini_value(raw: str) -> Any:
    raw = raw.strip()
    for q in ('"""', "'''"):
        if raw.startswith(q):
            # One-line multi-line string; one that spans lines is skipped.
            if len(raw) >= 6 and raw.endswith(q):
                inner = raw[3:-3]
                return _mini_basic(inner) if q == '"""' else inner
            return _MINI_SKIP
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        inner = raw[1:-1]
        if re.search(r'(?<!\\)(?:\\\\)*"', inner):
            return _MINI_SKIP  # an unescaped quote: not one string
        return _mini_basic(inner)
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        inner = raw[1:-1]
        return _MINI_SKIP if "'" in inner else inner
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw.replace("_", ""))
    except ValueError:
        pass
    try:
        return float(raw.replace("_", ""))
    except ValueError:
        return _MINI_SKIP


_MINI_WS = " \t"
_MINI_INLINE_KEY_RE = re.compile(_MINI_PART + r"(?:[ \t]*\.[ \t]*" + _MINI_PART + r")*")


def _mini_inline(raw: str) -> Any:
    """A one-line inline table, `{ a = 1, b.c = "x", d = { e = true } }`, as
    a dict. A value inside it that the subset cannot read leaves that key
    out; anything malformed gives _MINI_SKIP (the whole key is left out)."""
    pos = 0
    n = len(raw)

    def ws():
        nonlocal pos
        while pos < n and raw[pos] in _MINI_WS:
            pos += 1

    def string_end(q: str) -> int:
        i = pos + 1
        while i < n:
            if raw[i] == "\\" and q == '"':
                i += 2
                continue
            if raw[i] == q:
                return i + 1
            i += 1
        raise ValueError("unterminated string")

    def skip_array():
        nonlocal pos
        depth = 0
        while pos < n:
            ch = raw[pos]
            if ch in "\"'":
                pos = string_end(ch)
                continue
            if ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    pos += 1
                    return
            pos += 1
        raise ValueError("unterminated array")

    def table(depth: int) -> dict:
        nonlocal pos
        if depth > 8:
            raise ValueError("too deep")
        out: Dict[str, Any] = {}
        pos += 1  # "{"
        ws()
        if pos < n and raw[pos] == "}":
            pos += 1
            return out
        while True:
            ws()
            m = _MINI_INLINE_KEY_RE.match(raw, pos)
            if not m:
                raise ValueError("bad key")
            parts = [_mini_key_part(x) for x in _MINI_PART_RE.findall(m.group(0))]
            pos = m.end()
            ws()
            if pos >= n or raw[pos] != "=":
                raise ValueError("no =")
            pos += 1
            ws()
            if pos >= n:
                raise ValueError("no value")
            ch = raw[pos]
            if ch == "{":
                value: Any = table(depth + 1)
            elif ch == "[":
                skip_array()
                value = _MINI_SKIP
            else:
                start = pos
                if ch in "\"'":
                    pos = string_end(ch)
                else:
                    while pos < n and raw[pos] not in ",}":
                        pos += 1
                value = _mini_value(raw[start:pos])
            if value is not _MINI_SKIP:
                target = out
                for part in parts[:-1]:
                    nxt = target.get(part)
                    if not isinstance(nxt, dict):
                        nxt = {}
                        target[part] = nxt
                    target = nxt
                target[parts[-1]] = value
            ws()
            if pos < n and raw[pos] == ",":
                pos += 1
                continue
            if pos < n and raw[pos] == "}":
                pos += 1
                return out
            raise ValueError("expected , or }")

    try:
        raw = raw.strip()
        n = len(raw)
        if not raw.startswith("{"):
            return _MINI_SKIP
        result = table(0)
        ws()
        return result if pos == n else _MINI_SKIP
    except (ValueError, IndexError, RecursionError):
        return _MINI_SKIP


_MINI_OPEN = object()  # an array that goes on on the next line


def _mini_array(raw: str) -> Any:
    """An array of strings, numbers and booleans, `["a", 'b', 3,]`, as a list.
    _MINI_OPEN when it is not closed yet (a multi-line array: the caller
    appends the next line); _MINI_SKIP for anything else (nested arrays,
    inline tables, a value the subset cannot read)."""
    raw = raw.strip()
    if not raw.startswith("["):
        return _MINI_SKIP
    out = []
    pos, n = 1, len(raw)
    expect_value = True
    while True:
        while pos < n and raw[pos] in " \t\r\n":
            pos += 1
        if pos >= n:
            return _MINI_OPEN
        ch = raw[pos]
        if ch == "]":
            return out if pos == n - 1 else _MINI_SKIP
        if ch == ",":
            if expect_value:
                return _MINI_SKIP
            expect_value = True
            pos += 1
            continue
        if not expect_value or ch in "[{":
            return _MINI_SKIP
        start = pos
        if ch in "\"'":
            pos += 1
            while pos < n and raw[pos] != ch:
                pos += 2 if (raw[pos] == "\\" and ch == '"') else 1
            if pos >= n:
                return _MINI_SKIP  # a string does not go on to the next line
            pos += 1
        else:
            while pos < n and raw[pos] not in ",] \t\r\n":
                pos += 1
        value = _mini_value(raw[start:pos])
        if value is _MINI_SKIP:
            return _MINI_SKIP
        out.append(value)
        expect_value = False


def _mini_key_part(part: str) -> str:
    if part.startswith('"'):
        text = _mini_basic(part[1:-1])
        return text if isinstance(text, str) else part[1:-1]
    if part.startswith("'"):
        return part[1:-1]  # literal string: no escapes
    return part


def _mini_split_key(header: str) -> list:
    """Split a dotted table header, keeping quoted parts whole:
    profiles."nova.v2" -> ["profiles", "nova.v2"]."""
    parts = []
    buf = ""
    quote = None
    for ch in header:
        if quote:
            if ch == quote:
                quote = None
            else:
                buf += ch
        elif ch in "\"'":
            quote = ch
        elif ch == ".":
            parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    parts.append(buf.strip())
    return parts


def muted_all() -> bool:
    """Mute-all as set from the GUI (persisted by the daemon)."""
    try:
        data = json.loads(read_regular_file(home() / "daemon.json", 65536).decode("utf-8"))
    except Exception:
        return False
    return isinstance(data, dict) and data.get("muted_all") is True


def _mini_toml(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    cur = root
    open_quote = None  # inside a multi-line string: skip to its closing quotes
    pending = None  # a multi-line array: (target table, key parts, text so far)
    for line in text.splitlines():
        if open_quote:
            if open_quote in line:
                open_quote = None
            continue
        line = _mini_strip_comment(line).strip()
        if pending is not None:
            target, parts, buf = pending
            buf += "\n" + line
            value = _mini_array(buf)
            if value is _MINI_OPEN and len(buf) < 65536:
                pending = (target, parts, buf)
                continue
            pending = None
            if isinstance(value, list):
                _mini_set(target, parts, value)
            continue
        if not line:
            continue
        if line.startswith("["):
            if line.startswith("[[") or not line.endswith("]"):
                cur = {}  # arrays of tables are unsupported: park their keys
                continue
            cur = root
            for key in _mini_split_key(line[1:-1]):
                nxt = cur.get(key)
                if not isinstance(nxt, dict):
                    nxt = {}
                    cur[key] = nxt
                cur = nxt
            continue
        m = _MINI_KEY_RE.match(line)
        if m:
            raw_value = m.group(2).strip()
            if raw_value.startswith("["):
                value = _mini_array(raw_value)
                if value is _MINI_OPEN:
                    pending = (cur, [_mini_key_part(p) for p in _MINI_PART_RE.findall(m.group(1))], raw_value)
                    continue
            elif raw_value.startswith("{"):
                value = _mini_inline(raw_value)
            else:
                value = _mini_value(raw_value)
            if value is _MINI_SKIP:
                raw = m.group(2).strip()
                for q in ('"""', "'''"):
                    if raw.startswith(q) and not (len(raw) >= 6 and raw.endswith(q)):
                        open_quote = q
                continue
            _mini_set(cur, [_mini_key_part(p) for p in _MINI_PART_RE.findall(m.group(1))], value)
    return root


def _mini_set(target: Dict[str, Any], parts: list, value: Any) -> None:
    for part in parts[:-1]:  # dotted key: a.b = 1 is a = {b = 1}
        nxt = target.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            target[part] = nxt
        target = nxt
    target[parts[-1]] = value


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


def early_say(cfg: Dict[str, Any]) -> bool:
    """[daemon] early_say (default true): speak <say> blocks while Claude is
    still writing (MessageDisplay). Anything but `false` keeps it on."""
    return _get(cfg, "daemon", "early_say", default=True) is not False


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
    import tempfile

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


STATE_LOCK_WAIT_S = 1.0


@contextlib.contextmanager
def state_lock(wait: float = STATE_LOCK_WAIT_S):
    """An exclusive flock on state/.lock for a read-modify-write of a state
    file (the hooks and the daemon's set_session both take it). Waits at
    most `wait` seconds, then goes ahead without it: a lost update is better
    than a hook that hangs. Yields whether the lock is held."""
    fd = -1
    held = False
    try:
        import fcntl

        d = state_dir()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(d / ".lock"), os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if stat.S_ISREG(os.fstat(fd).st_mode):
            deadline = time.monotonic() + max(0.0, wait)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.005)
    except Exception:
        held = False
    try:
        yield held
    finally:
        if fd >= 0:
            try:
                os.close(fd)  # also releases the flock
            except OSError:
                pass


def update_state(session_id: object, change) -> Dict[str, Any]:
    """Read the session state, let `change(state)` edit it in place, and save
    it unless `change` returns False, all under state_lock(), so writers do
    not undo each other's changes. Returns the state. Save errors raise."""
    with state_lock():
        st = load_state(session_id)
        if change(st) is not False:
            save_state(session_id, st)
        return st


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
