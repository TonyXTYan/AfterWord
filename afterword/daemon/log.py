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
    kind        TEXT NOT NULL,              -- hook | test | system | alert
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
    payload     TEXT,                       -- JSON (hook payload through common.project_payload)
    timings     TEXT,                       -- JSON
    delivery    TEXT,                       -- socket | spool
    clip        TEXT,                       -- path of the rendered audio
    error       TEXT
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS events_clip ON events(id) WHERE clip IS NOT NULL;
"""

JSON_FIELDS = ("attrs", "parsed", "payload", "timings")
COLUMNS = (
    "ts", "kind", "hook_event", "session_id", "cwd", "profile", "decision", "status",
    "reason", "text", "attrs", "parsed", "payload", "timings", "delivery", "clip", "error",
)
SUMMARY_COLUMNS = tuple(c for c in ("id",) + COLUMNS if c != "payload")
STAT_KEYS = ("hook_ms", "hook_to_daemon_ms", "synth_ms", "rtf", "first_chunk_ms", "first_audio_ms")


def _summary_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    v = sorted(values)

    def pct(p: float) -> float:
        i = min(len(v) - 1, max(0, round(p * (len(v) - 1))))
        return round(v[i], 3)

    return {"n": len(v), "p50": pct(0.5), "p95": pct(0.95), "max": round(v[-1], 3)}


LOG_FILE = "afterwordd.log"  # the daemon's own log in AfterWord's home (rotated at 2 MB)


def setup_text_logging(home: Path, level: int = logging.INFO, stderr: bool = True,
                       stderr_level: int = logging.NOTSET) -> None:
    root = logging.getLogger("afterword")
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(
        home / LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if stderr:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(stderr_level)
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


BUSY_TIMEOUT_MS = 2000  # wait this long for another process's lock, then fail
JOURNAL_SIZE_LIMIT = 8 * 1024 * 1024  # the WAL is cut back to this after a checkpoint
QUICK_CHECK_BUDGET_S = 2.0  # a quick_check that runs longer is given up (the file is kept)
# PRAGMA user_version once stored hook payloads went through
# common.project_payload (0.3.5): older rows held prompts and replies raw.
SCRUBBED_VERSION = 1


class EventLogError(RuntimeError):
    """events.db could not be opened, and was left where it is (locked by
    another process, unreadable, ...)."""


def _is_corrupt(exc: sqlite3.DatabaseError) -> bool:
    """The file is not a database or is malformed (SQLITE_NOTADB /
    SQLITE_CORRUPT), as opposed to locked, busy or unopenable."""
    name = getattr(exc, "sqlite_errorname", None)
    if name:
        return name == "SQLITE_NOTADB" or name.startswith("SQLITE_CORRUPT")
    msg = str(exc).lower()
    return "not a database" in msg or "malformed" in msg


def _quick_check(db: sqlite3.Connection) -> str | None:
    """None if PRAGMA quick_check passes (or runs out of time: a slow check
    is no proof of damage), else what it found."""
    deadline = time.monotonic() + QUICK_CHECK_BUDGET_S
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        found = [str(r[0]) for r in db.execute("PRAGMA quick_check(5)").fetchall()]
    except sqlite3.OperationalError as exc:
        if "interrupt" not in str(exc).lower():
            raise
        log.warning("events.db quick_check took over %.0f s; skipped it", QUICK_CHECK_BUDGET_S)
        return None
    finally:
        db.set_progress_handler(None, 0)
    return None if found == ["ok"] else "; ".join(found)[:500]


def _connect(db_path: Path) -> tuple[sqlite3.Connection, str | None]:
    """Open events.db and check it. Returns the connection and None, or the
    connection (still open, so SQLite does not clean up its -wal and -shm
    before they are moved aside) and why the file is corrupt. Any other
    failure (locked past BUSY_TIMEOUT_MS, unopenable, I/O) raises
    EventLogError: the file may be fine, so it is left where it is."""
    try:
        db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None,
                             timeout=BUSY_TIMEOUT_MS / 1000)
    except sqlite3.Error as exc:
        raise EventLogError(f"could not open the event log {db_path}: {exc}") from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
        problem = _quick_check(db)
        if problem is None:
            db.executescript(SCHEMA)
            # Take the write lock once, so a database another process holds
            # locked fails here (SQLITE_BUSY after BUSY_TIMEOUT_MS), clearly.
            db.execute("BEGIN IMMEDIATE")
            db.execute("ROLLBACK")
    except sqlite3.DatabaseError as exc:
        if _is_corrupt(exc):
            return db, str(exc) or type(exc).__name__
        db.close()
        held = " (another process holds it)" if "locked" in str(exc) or "busy" in str(exc) else ""
        raise EventLogError(f"could not open the event log {db_path}: {exc}{held}; "
                            "the file was left as it is") from exc
    except BaseException:
        db.close()
        raise
    return db, problem


def _quarantine(db_path: Path) -> Path:
    """Move events.db and its -wal / -shm aside as
    events.db.corrupt-<UTC time>(-wal/-shm); never deletes them. Returns the
    new path of the database file."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    suffixes = ("", "-wal", "-shm")
    base, n = db_path.with_name(f"{db_path.name}.corrupt-{stamp}"), 1
    while any(base.with_name(base.name + s).exists() for s in suffixes):
        base, n = db_path.with_name(f"{db_path.name}.corrupt-{stamp}-{n}"), n + 1
    for s in suffixes:
        src = db_path.with_name(db_path.name + s)
        if src.exists() or src.is_symlink():
            src.rename(base.with_name(base.name + s))
    return base


class EventLog:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.quarantined: Path | None = None  # where an unreadable events.db was moved
        self._db, problem = _connect(db_path)
        if problem is not None:
            # A corrupt events.db would fail every start (launchd restarts
            # the daemon every 10 s): move it aside and start a new one. The
            # files are renamed before the connection closes, so SQLite's
            # cleanup on close cannot delete or rewrite them in place.
            try:
                self.quarantined = _quarantine(db_path)
            except OSError as exc:
                raise EventLogError(f"the event log {db_path} is unreadable ({problem}) "
                                    f"and could not be moved aside: {exc}") from exc
            finally:
                self._db.close()
            log.error("the event log %s is unreadable (%s); moved it to %s and started a new one",
                      db_path, problem, self.quarantined)
            self._db, again = _connect(db_path)
            if again is not None:
                self._db.close()
                raise EventLogError(f"a new event log at {db_path} fails its check too: {again}")
        self.scrub_payloads()
        if self.quarantined is not None:
            self.insert(kind="system", status="error", decision=None,
                        reason=f"{db_path.name} was unreadable; moved it to {self.quarantined.name} "
                               "and started a new event log",
                        error=problem[:1000])

    def scrub_payloads(self) -> int:
        """Once per database: rewrite the payloads of hook and alert rows
        stored before 0.3.5 (prompts, replies and tool commands verbatim)
        through common.project_payload, then VACUUM so the old text leaves
        the file. Guarded by PRAGMA user_version; projecting is idempotent,
        so running it again changes nothing. Returns how many rows changed."""
        with self._lock:
            if self._db.execute("PRAGMA user_version").fetchone()[0] >= SCRUBBED_VERSION:
                return 0
            changed, last_id = 0, 0
            self._db.execute("BEGIN IMMEDIATE")
            try:
                while True:
                    rows = self._db.execute(
                        "SELECT id, payload FROM events WHERE id > ? AND payload IS NOT NULL "
                        "AND kind IN ('hook', 'alert') ORDER BY id LIMIT 500", (last_id,)).fetchall()
                    if not rows:
                        break
                    for r in rows:
                        last_id = r["id"]
                        try:
                            new = _dump(common.project_payload(json.loads(r["payload"])))
                        except ValueError:
                            new = None  # not JSON: nothing of it is worth keeping
                        if new != r["payload"]:
                            self._db.execute("UPDATE events SET payload = ? WHERE id = ?", (new, r["id"]))
                            changed += 1
                self._db.execute(f"PRAGMA user_version={SCRUBBED_VERSION}")
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            if changed:
                log.info("removed prompt, reply and tool text from %d stored hook payload(s)", changed)
                self._vacuum()
        return changed

    def _vacuum(self) -> None:
        """VACUUM, then empty the WAL (VACUUM writes the whole database
        through it). Caller holds the lock."""
        try:
            self._db.execute("VACUUM")
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:
            log.warning("VACUUM of the event log failed", exc_info=True)

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

    def session_activity(self, limit: int = 500) -> dict[str, dict[str, Any]]:
        """Per-session activity from hook events: newest time, counts, and
        the newest event."""
        with self._lock:
            rows = self._db.execute(
                "SELECT session_id, MAX(ts) AS last_seen, COUNT(*) AS events, "
                "SUM(status = 'spoken') AS spoken, MAX(id) AS last_id "
                "FROM events WHERE kind = 'hook' AND session_id IS NOT NULL AND session_id != '' "
                "GROUP BY session_id ORDER BY last_seen DESC LIMIT ?",
                (max(1, min(int(limit), 5000)),),
            ).fetchall()
            last_ids = [r["last_id"] for r in rows]
            last = {}
            if last_ids:
                marks = ",".join("?" * len(last_ids))
                for r in self._db.execute(
                    f"SELECT id, ts, status, reason, text, cwd FROM events WHERE id IN ({marks})", last_ids
                ).fetchall():
                    last[r["id"]] = dict(r)
        out = {}
        for r in rows:
            ev = last.get(r["last_id"], {})
            out[r["session_id"]] = {
                "last_seen": r["last_seen"],
                "events": r["events"],
                "spoken": r["spoken"] or 0,
                "cwd": ev.get("cwd"),
                "last_event": {k: ev.get(k) for k in ("id", "ts", "status", "reason", "text")} if ev else None,
            }
        return out

    def session_activity_for(self, session_id: str) -> dict[str, Any] | None:
        """One session's activity (uses the session_id index)."""
        with self._lock:
            r = self._db.execute(
                "SELECT MAX(ts) AS last_seen, COUNT(*) AS events, SUM(status = 'spoken') AS spoken, MAX(id) AS last_id "
                "FROM events WHERE session_id = ? AND kind = 'hook'",
                (session_id,),
            ).fetchone()
            if not r or not r["events"]:
                return None
            ev = self._db.execute(
                "SELECT id, ts, status, reason, text, cwd FROM events WHERE id = ?", (r["last_id"],)
            ).fetchone()
        ev = dict(ev) if ev else {}
        return {
            "last_seen": r["last_seen"], "events": r["events"], "spoken": r["spoken"] or 0, "cwd": ev.get("cwd"),
            "last_event": {k: ev.get(k) for k in ("id", "ts", "status", "reason", "text")} if ev else None,
        }

    def prune_before(self, cutoff_ts: float, vacuum_at: int = 100) -> tuple[int, list[str]]:
        """Delete events older than `cutoff_ts` (rows still in flight stay).
        Returns how many went and the clip paths they pointed at, for the
        caller to delete. A large prune is followed by VACUUM and a WAL
        checkpoint, so the file and its -wal shrink too."""
        with self._lock:
            clips = [r["clip"] for r in self._db.execute(
                "SELECT clip FROM events WHERE ts < ? AND clip IS NOT NULL "
                "AND status NOT IN ('queued','synthesizing','playing')", (cutoff_ts,)
            ).fetchall()]
            n = self._db.execute(
                "DELETE FROM events WHERE ts < ? AND status NOT IN ('queued','synthesizing','playing')",
                (cutoff_ts,),
            ).rowcount
            if n >= vacuum_at:
                self._vacuum()
        return n, clips

    def forget_clip(self, event_id: int) -> None:
        """The clip file was pruned: stop listing it in history."""
        with self._lock:
            self._db.execute("UPDATE events SET clip = NULL WHERE id = ?", (event_id,))

    def history(self, limit: int = 50, before_id: int | None = None) -> list[dict[str, Any]]:
        """Events that still have their audio clip on disk, newest first."""
        limit = max(1, min(int(limit), 500))
        out: list[dict[str, Any]] = []
        cursor = before_id
        while len(out) < limit:
            sql = "SELECT id, ts, session_id, cwd, profile, text, status, timings, clip FROM events WHERE clip IS NOT NULL"
            args: list[Any] = []
            if cursor is not None:
                sql += " AND id < ?"
                args.append(int(cursor))
            sql += " ORDER BY id DESC LIMIT ?"
            args.append(limit * 2)
            with self._lock:
                rows = self._db.execute(sql, args).fetchall()
            if not rows:
                break
            for r in rows:
                cursor = r["id"]
                if not Path(r["clip"]).is_file():
                    continue
                t = json.loads(r["timings"]) if r["timings"] else {}
                out.append({
                    "id": r["id"], "ts": r["ts"], "session_id": r["session_id"], "cwd": r["cwd"],
                    "profile": r["profile"], "text": r["text"], "status": r["status"],
                    "audio_s": t.get("audio_s"), "playback_rate": t.get("playback_rate", 1.0),
                    "synth_backend": t.get("synth_backend"),
                    "url": f"/api/clips/{r['id']}", "download_url": f"/api/clips/{r['id']}?download=1",
                })
                if len(out) >= limit:
                    break
        return out

    def spoken_lines(self, session_id: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        """The last `limit` spoken lines (optionally of one session), oldest first."""
        limit = max(1, min(int(limit), 5000))
        sql = "SELECT id, ts, session_id, cwd, profile, text FROM events WHERE status = 'spoken' AND text IS NOT NULL AND text != ''"
        args: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            args.append(session_id)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [dict(r) for r in reversed(rows)]

    def voice_stats(self, days: float = 7, now: float | None = None) -> dict[str, dict[str, Any]]:
        """Per profile over the last `days`: spoken lines, audio seconds, mean synth ms."""
        since = (time.time() if now is None else now) - days * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT profile, timings FROM events WHERE status = 'spoken' AND profile IS NOT NULL AND ts >= ?", (since,)).fetchall()
        acc: dict[str, dict[str, Any]] = {}
        for r in rows:
            try:
                t = json.loads(r["timings"]) if r["timings"] else {}
            except ValueError:
                t = {}
            if not isinstance(t, dict):
                t = {}
            a = acc.setdefault(r["profile"], {"lines": 0, "audio": 0.0, "synth": []})
            a["lines"] += 1
            if isinstance(t.get("audio_s"), (int, float)):
                a["audio"] += float(t["audio_s"])
            if isinstance(t.get("synth_ms"), (int, float)):
                a["synth"].append(float(t["synth_ms"]))
        return {name: {"lines_7d": a["lines"], "audio_s_7d": round(a["audio"], 2),
                       "synth_ms_avg_7d": round(sum(a["synth"]) / len(a["synth"]), 1) if a["synth"] else None}
                for name, a in acc.items()}

    def stats(self, limit: int = 200) -> dict[str, Any]:
        limit = max(1, min(int(limit), 5000))
        with self._lock:
            rows = self._db.execute(
                "SELECT status, reason, timings FROM events WHERE kind IN ('hook', 'test') AND status != 'info' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        by_status: dict[str, int] = {}
        by_reason: dict[str, int] = {}
        series: dict[str, list[float]] = {k: [] for k in STAT_KEYS}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r["reason"]:
                by_reason[r["reason"]] = by_reason.get(r["reason"], 0) + 1
            try:
                t = json.loads(r["timings"]) if r["timings"] else {}
            except ValueError:
                t = {}
            for k in STAT_KEYS:
                v = t.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    series[k].append(float(v))
        return {
            "count": len(rows),
            "by_status": by_status,
            "by_reason": dict(sorted(by_reason.items(), key=lambda kv: -kv[1])[:20]),
            "timings": {k: _summary_stats(v) for k, v in series.items()},
        }

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
    decision = rec.get("decision") if rec.get("decision") in ("speak", "skip", "info") else "error"
    fallback = rec.get("fallback")
    if decision == "speak":
        status = "spoken" if fallback == "say" else ("error" if fallback == "error" else "skipped")
        reason = {
            "say": "daemon down: spoken by say fallback",
            "error": "daemon down: say fallback failed",
        }.get(fallback, "daemon down: fallback disabled")
    elif decision == "skip":
        status, reason = "skipped", _s(rec.get("reason"), 200)
    elif decision == "info":
        status, reason = "info", _s(rec.get("reason"), 200)
    else:
        status, reason = "error", "error: malformed spool record"
    t_hook = rec.get("t_hook")
    ts = float(t_hook) if isinstance(t_hook, (int, float)) and not isinstance(t_hook, bool) else time.time()
    # A spoken alert (the hook's `say` fallback) is an alert row, like the
    # daemon's own, so it stays out of the speech stats.
    alert = rec.get("alert") if isinstance(rec.get("alert"), dict) else None
    if alert is not None and decision == "speak":
        reason = reason.replace("spoken by say fallback", "alert spoken by say fallback")
    events.insert(
        ts=ts,
        kind="alert" if alert is not None and decision == "speak" else "hook",
        hook_event=_s(rec.get("event"), 64) or "",
        session_id=_s(rec.get("session_id"), 256),
        cwd=_s(rec.get("cwd")),
        profile=_s(rec.get("profile"), 64),
        decision=decision,
        status=status,
        reason=reason,
        text=_s(rec.get("text"), 5000),
        attrs=rec.get("attrs") if isinstance(rec.get("attrs"), dict) else None,
        parsed=rec.get("parsed") if isinstance(rec.get("parsed"), dict) else ({"alert": common.cap_strings(alert)}
                                                                              if alert is not None else None),
        payload=common.project_payload(rec.get("payload")),
        delivery="spool",
        error=_s(rec.get("fallback_error")),
    )
    return True
