"""Early speaking: the per-message stream file and the keys shared by the
MessageDisplay hook and the Stop hook.

MessageDisplay hands the hook each batch of newly completed lines of an
assistant message (`message_id`, `index`, `final`, `delta`). The hook keeps
the batches of a message in ``<home>/stream/<session>.json``, parses the text
seen so far with the normal parser (code blocks masked) and sends each
newly completed ``<say>`` block to the daemon at once. Stop then sends only
the blocks that were not spoken early.

File layout::

    {"v": 1, "messages": {
        "<message_id>": {
            "turn": "<prompt_id>", "t": first seen, "updated": ts,
            "parts": {"0": delta, "1": delta, ...},   # by batch index
            "final": <index of the final batch> | absent,
            "sent": {"<early_key>": {"i": n, "digest": "...", "t": ts}},
            "done": "<why nothing more is sent>" | absent}}}

Batches can arrive out of order (the hook runs async, one process each), so
only the run of batches from index 0 without a gap is parsed. A file lock
serialises the processes of one session. Records older than PRUNE_AFTER_S
are dropped; stale files are removed.

Stdlib-only and Python 3.9+ (see tags.py).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

try:  # the hook runs with this directory on sys.path
    import common
except ImportError:  # imported as afterword.hooks.earlysay (tests, the daemon)
    from . import common  # type: ignore[no-redef]

PRUNE_AFTER_S = 600.0  # 10 minutes
FILE_MAX_BYTES = 2 * 1024 * 1024
MESSAGE_MAX_CHARS = 200 * 1024  # text kept per message; past this the message is given up
MAX_MESSAGES = 64  # records per session file (oldest dropped first)
MAX_INDEX = 100000
LOCK_WAIT_S = 1.0
_ID_MAX = 200

_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def stream_dir() -> Path:
    return common.home() / "stream"


def stream_file(session_id: object) -> Path:
    return stream_dir() / (common.safe_session_id(session_id) + ".json")


def _lock_file(session_id: object) -> Path:
    return stream_dir() / (common.safe_session_id(session_id) + ".lock")


# --------------------------------------------------------------------------- #
# Keys                                                                        #
# --------------------------------------------------------------------------- #


def early_key(message_id: str, ordinal: int, text: str) -> str:
    """sha1(message id + index of the <say> in that message + its text)."""
    raw = "%s\0%d\0%s" % (message_id, ordinal, text)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def digest(text: str) -> str:
    """Identifies a block's text within a turn (Stop cannot always know the
    MessageDisplay message id; see find_message)."""
    return hashlib.sha1(("say\0" + text).encode("utf-8", "replace")).hexdigest()[:20]


def valid_id(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= _ID_MAX


# --------------------------------------------------------------------------- #
# The file                                                                    #
# --------------------------------------------------------------------------- #


@contextmanager
def locked(session_id: object, wait_s: float = LOCK_WAIT_S) -> Iterator[bool]:
    """Hold the session's stream lock. Yields False when it could not be
    taken in time (the caller then does nothing)."""
    import fcntl

    d = stream_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(_lock_file(session_id)), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            yield False
            return
        end = time.time() + wait_s
        got = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    break
            if time.time() >= end:
                break
            time.sleep(0.01)
        try:
            os.utime(fd)  # keeps the lock file from being pruned while in use
        except (OSError, NotImplementedError, TypeError):
            pass
        yield got
    finally:
        os.close(fd)  # also releases the lock


def load(session_id: object) -> Dict[str, Any]:
    """The session's stream file; missing, too big or corrupt -> empty."""
    empty: Dict[str, Any] = {"v": 1, "messages": {}}
    try:
        data = json.loads(common.read_regular_file(stream_file(session_id), FILE_MAX_BYTES + 1).decode("utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
        return empty
    msgs = {}
    for mid, rec in data["messages"].items():
        if valid_id(mid) and isinstance(rec, dict):
            if not isinstance(rec.get("parts"), dict):
                rec["parts"] = {}
            if not isinstance(rec.get("sent"), dict):
                rec["sent"] = {}
            msgs[mid] = rec
    data["messages"] = msgs
    return data


def save(session_id: object, data: Dict[str, Any]) -> None:
    """Atomic write (temp file, rename). Too big: oldest messages first go."""
    d = stream_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    # The cap is in bytes (load reads at most FILE_MAX_BYTES + 1 of them).
    blob = json.dumps(data, ensure_ascii=False).encode("utf-8", "replace")
    while len(blob) > FILE_MAX_BYTES and data["messages"]:
        oldest = min(data["messages"], key=lambda m: _num(data["messages"][m].get("updated")))
        del data["messages"][oldest]
        blob = json.dumps(data, ensure_ascii=False).encode("utf-8", "replace")
    fd, tmp = tempfile.mkstemp(prefix=".stream-", suffix=".tmp", dir=str(d))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, str(stream_file(session_id)))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _num(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def prune(data: Dict[str, Any], now: float) -> None:
    """Drop records not touched for PRUNE_AFTER_S, and the oldest past
    MAX_MESSAGES."""
    msgs = data["messages"]
    for mid in [m for m, r in msgs.items() if now - _num(r.get("updated")) > PRUNE_AFTER_S]:
        del msgs[mid]
    if len(msgs) > MAX_MESSAGES:
        for mid in sorted(msgs, key=lambda m: _num(msgs[m].get("updated")))[: len(msgs) - MAX_MESSAGES]:
            del msgs[mid]


def prune_files(now: float, keep: object = None) -> None:
    """Remove other sessions' stream and lock files untouched for
    PRUNE_AFTER_S (best effort)."""
    keep_names = set()
    if keep is not None:
        sid = common.safe_session_id(keep)
        keep_names = {sid + ".json", sid + ".lock"}
    try:
        with os.scandir(str(stream_dir())) as it:
            for entry in it:
                if entry.name in keep_names:
                    continue
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime > PRUNE_AFTER_S:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Messages                                                                    #
# --------------------------------------------------------------------------- #


def add_batch(rec: Dict[str, Any], index: int, delta: str, final: bool) -> bool:
    """Store one batch. False when the message got too long (it is then
    given up: `done` is set and its text dropped)."""
    parts = rec["parts"]
    if rec.get("done"):
        return False
    parts[str(index)] = delta
    if final:
        rec["final"] = index
    if sum(len(p) for p in parts.values() if isinstance(p, str)) > MESSAGE_MAX_CHARS:
        rec["done"] = "message too long"
        rec["parts"] = {}
        return False
    return True


def joined(rec: Dict[str, Any]) -> Tuple[str, bool]:
    """(text of the batches 0..k without a gap, whether that is the whole
    message: the final batch arrived and nothing before it is missing)."""
    parts = rec.get("parts") or {}
    out = []
    i = 0
    while i <= MAX_INDEX:
        p = parts.get(str(i))
        if not isinstance(p, str):
            break
        out.append(p)
        i += 1
    final = rec.get("final")
    complete = isinstance(final, int) and not isinstance(final, bool) and i == final + 1
    return "".join(out), complete


def stable_count(text: str, parsed: Any, complete: bool) -> int:
    """How many of `parsed.says` (in order) are settled: no later batch can
    mask them, nest them in another block or change their index.

    - An unpaired `<say>` opener before a block could be closed by a later
      `</say>`: the block would then be nested in a new outer block (whose
      text includes it), so it is not settled.
    - A `<!--` before a block with no `-->` after it ends with its paragraph
      for now, but a later `-->` makes it mask everything up to there.
    - An inline code span opened before the block (or inside it) in the same
      paragraph could still close in a later batch. So the block also needs
      its paragraph to have ended (a blank line follows), or that paragraph
      up to the block's end to have no backtick and no `<!--`.

    Fenced blocks only grow forward (an unclosed fence already masks the
    rest). A complete message is settled. The count stops at the first block
    that is not settled, so the index of each block never changes."""
    says = parsed.says
    if complete:
        return len(says)
    first_open = min(list(parsed.open_says) + list(parsed.open_comments), default=len(text))
    n = 0
    for tag in says:
        if tag.start > first_open:
            break
        end = tag.start + len(tag.raw)
        if _BLANK_LINE_RE.search(text, end):
            n += 1
            continue
        para_start = 0
        for m in _BLANK_LINE_RE.finditer(text, 0, tag.start):
            para_start = m.end()
        head = text[para_start:end]
        if "`" in head or "<!--" in head:
            break
        n += 1
    return n


def text_sha(text: str) -> str:
    return hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()


def finish(rec: Dict[str, Any], text: str) -> None:
    """A complete message: keep only its hash (for find_message) and what
    was sent, not its text."""
    rec["text_sha"] = text_sha(text)
    rec["parts"] = {}
    rec["done"] = "complete"


def find_message(data: Dict[str, Any], turn: Optional[str], text: str) -> Optional[str]:
    """The message id whose text (as displayed) is `text`: Stop's
    `last_assistant_message`. Same turn first when the turn is known."""
    if not isinstance(text, str) or not text.strip():
        return None
    want = text_sha(text)
    best = None
    for mid, rec in data["messages"].items():
        got = rec.get("text_sha")
        if not isinstance(got, str):
            got = text_sha(joined(rec)[0])
        if got != want:
            continue
        if turn is not None and rec.get("turn") == turn:
            return mid
        if best is None or _num(rec.get("updated")) > _num(data["messages"][best].get("updated")):
            best = mid
    return best if turn is None else (best if best and not data["messages"][best].get("turn") else None)


def turn_digests(data: Dict[str, Any], turn: Optional[str]) -> set:
    """Digests of every block sent early in `turn`."""
    out = set()
    if turn is None:
        return out
    for rec in data["messages"].values():
        if rec.get("turn") != turn:
            continue
        for info in rec["sent"].values():
            if isinstance(info, dict) and isinstance(info.get("digest"), str):
                out.add(info["digest"])
    return out


def segments_with_keys(says: list, segments: List[dict], message_id: Optional[str]) -> List[Tuple[int, dict]]:
    """[(index of the <say>, segment)] with `early_key` (when the message id
    is known) and `digest` added to each spoken segment. `segments` come from
    tags.limit_says(..., keep_index=True); their `i` is removed."""
    out = []
    for seg in segments:
        i = seg.pop("i")
        text = says[i].text
        if message_id is not None:
            seg["early_key"] = early_key(message_id, i, text)
        seg["digest"] = digest(text)
        out.append((i, seg))
    return out
