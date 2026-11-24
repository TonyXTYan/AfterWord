"""Event log: SQLite (~/.afterword/events.db) plus a rotating text log, and a
fan-out of changes to live subscribers (the GUI's SSE stream)."""

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from afterword.hooks import common

log = logging.getLogger("afterword.log")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    kind        TEXT NOT NULL,              -- hook | test | system
    hook_event  TEXT,                       -- Stop, ...
    session_id  TEXT,
    cwd         TEXT,
    profile     TEXT,
    decision    TEXT,                       -- speak | skip | error
    status      TEXT NOT NULL,              -- skipped | queued | synthesizing | playing | spoken | error | info
    reason      TEXT,                       -- why skipped / failed
    text        TEXT,
    attrs       TEXT,                       -- JSON
    parsed      TEXT,                       -- JSON
    payload     TEXT,                       -- JSON (raw hook payload, strings capped)
    timings     TEXT,                       -- JSON
    delivery    TEXT,                       -- socket | spool
    clip        TEXT,                       -- path of the rendered audio
    error       TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
"""

JSON_FIELDS = ("attrs", "parsed", "payload", "timings")
COLUMNS = (
    "ts", "kind", "hook_event", "session_id", "cwd", "profile", "decision", "status",
    "reason", "text", "attrs", "parsed", "payload", "timings", "delivery", "clip", "error",
)
SUMMARY_COLUMNS = tuple(c for c in ("id",) + COLUMNS if c != "payload")


def setup_text_logging(home: Path, level: int = logging.INFO, stderr: bool = True) -> None:
    root = logging.getLogger("afterword")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        home / "afterwordd.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _dump(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in JSON_FIELDS:
        if key in d and d[key] is not None:
            try:
                d[key] = json.loads(d[key])
            except ValueError:
                pass
    return d


class EventLog:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- writes -------------------------------------------------------------

    def insert(self, **fields: Any) -> dict[str, Any]:
        fields.setdefault("ts", time.time())
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        cols = [c for c in COLUMNS if c in fields]
        values = [_dump(fields[c]) if c in JSON_FIELDS else fields[c] for c in cols]
        sql = f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        with self._lock:
            cur = self._db.execute(sql, values)
            event_id = cur.lastrowid
        event = self.get(event_id)
        self._publish("event", event)
        return event

    def update(self, event_id: int, *, merge_timings: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any] | None:
        unknown = set(fields) - set(COLUMNS)
        if unknown:
            raise ValueError(f"unknown event fields: {sorted(unknown)}")
        with self._lock:
            if merge_timings:
                row = self._db.execute("SELECT timings FROM events WHERE id=?", (event_id,)).fetchone()
                current = json.loads(row["timings"]) if row and row["timings"] else {}
                current.update(merge_timings)
                fields["timings"] = current
            if fields:
                cols = list(fields)
                values = [_dump(fields[c]) if c in JSON_FIELDS else fields[c] for c in cols]
                sql = f"UPDATE events SET {', '.join(f'{c}=?' for c in cols)} WHERE id=?"
                self._db.execute(sql, values + [event_id])
        event = self.get(event_id)
        if event:
            self._publish("event", event)
        return event

    # -- reads --------------------------------------------------------------

    def get(self, event_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def recent(self, limit: int = 200, before_id: int | None = None, full: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        cols = "*" if full else ",".join(SUMMARY_COLUMNS)
        sql = f"SELECT {cols} FROM events"
        args: list[Any] = []
        if before_id is not None:
            sql += " WHERE id < ?"
            args.append(int(before_id))
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [_row_to_dict(r) for r in rows]

    def recover_unfinished(self, reason: str) -> int:
        """Mark rows a previous run left in flight (queued / synthesizing /
        playing) as skipped. Returns how many."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE events SET status='skipped', reason=? "
                "WHERE status IN ('queued','synthesizing','playing')",
                (reason,),
            )
            return cur.rowcount

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    # -- live subscribers ---------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, kind: str, data: Any) -> None:
        self._publish(kind, data)

    def _publish(self, kind: str, data: Any) -> None:
        if not self._subscribers:
            return
        message = {"type": kind, "data": _summary(data) if kind == "event" else data}
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is not None and running is not loop:
            loop.call_soon_threadsafe(self._fanout, message)
        else:
            self._fanout(message)

    def _fanout(self, message: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass  # slow client: it will resync from /api/events


def _summary(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    return {k: v for k, v in event.items() if k != "payload"}


def _s(value: Any, limit: int = 4096) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def ingest_spool(events: EventLog, spool: Path | None = None) -> int:
    """Import events the hook wrote while the daemon was down. Returns count.

    The spool is claimed by renaming it, so hooks that run meanwhile start a
    new file. Claimed files a crash left behind are imported too. A bad
    record is logged and skipped; it never stops the rest.
    """
    spool = spool or common.spool_path()
    pending = sorted(spool.parent.glob(f"{spool.name}.*.ingest"))
    if spool.exists():
        claimed = spool.with_name(f"{spool.name}.{time.time_ns()}.ingest")
        try:
            spool.rename(claimed)
            pending.append(claimed)
            time.sleep(0.05)  # let a hook that opened the old file finish its append
        except OSError as exc:
            log.warning("could not claim spool %s: %s", spool, exc)
    n = 0
    for path in pending:
        try:
            data = common.read_regular_file(path, 2 * common.SPOOL_MAX_BYTES)
        except OSError as exc:
            log.warning("could not read spool %s: %s", path, exc)
            continue
        for line in data.decode("utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
                if isinstance(rec, dict) and _import_record(events, rec):
                    n += 1
            except Exception:
                log.warning("skipping bad spool record: %.200s", line, exc_info=True)
        try:
            path.unlink()
        except OSError:
            pass
    if n:
        log.info("imported %d spooled hook event(s)", n)
    return n


def _import_record(events: EventLog, rec: dict[str, Any]) -> bool:
    decision = rec.get("decision") if rec.get("decision") in ("speak", "skip") else "error"
    fallback = rec.get("fallback")
    if decision == "speak":
        status = "spoken" if fallback == "say" else ("error" if fallback == "error" else "skipped")
        reason = {
            "say": "daemon down: spoken by say fallback",
            "error": "daemon down: say fallback failed",
        }.get(fallback, "daemon down: fallback disabled")
    elif decision == "skip":
        status, reason = "skipped", _s(rec.get("reason"), 200)
    else:
        status, reason = "error", "error: malformed spool record"
    t_hook = rec.get("t_hook")
    ts = float(t_hook) if isinstance(t_hook, (int, float)) and not isinstance(t_hook, bool) else time.time()
    events.insert(
        ts=ts,
        kind="hook",
        hook_event=_s(rec.get("event"), 64) or "",
        session_id=_s(rec.get("session_id"), 256),
        cwd=_s(rec.get("cwd")),
        profile=_s(rec.get("profile"), 64),
        decision=decision,
        status=status,
        reason=reason,
        text=_s(rec.get("text"), 5000),
        attrs=rec.get("attrs") if isinstance(rec.get("attrs"), dict) else None,
        parsed=rec.get("parsed") if isinstance(rec.get("parsed"), dict) else None,
        payload=common.cap_strings(rec.get("payload")),
        delivery="spool",
        error=_s(rec.get("fallback_error")),
    )
    return True
