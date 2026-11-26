"""events.db robustness (a corrupt file is moved aside, a locked one is left
alone, the WAL shrinks after a prune) and payload privacy (only an
allow-listed projection of each hook payload is stored)."""

import json
import logging
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from afterword.daemon.log import EventLog, EventLogError, _import_record, ingest_spool
from afterword.hooks import common
from conftest import run_hook, stop_payload, wait_for

SECRET = "hunter2-private-words"


def _corrupt_copies(path: Path) -> list[Path]:
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


def _db_bytes(path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in path.parent.glob(path.name + "*") if p.is_file())


# --------------------------------------------------------------------------- #
# A corrupt, truncated or locked events.db                                    #
# --------------------------------------------------------------------------- #


def test_a_garbage_file_is_moved_aside_and_a_new_log_started(tmp_path):
    db = tmp_path / "events.db"
    db.write_bytes(b"this is not sqlite " * 300)
    (tmp_path / "events.db-wal").write_bytes(b"stale wal")
    (tmp_path / "events.db-shm").write_bytes(b"stale shm")
    events = EventLog(db)
    try:
        moved = _corrupt_copies(db)
        assert [p.name for p in moved] == [events.quarantined.name, events.quarantined.name + "-shm",
                                           events.quarantined.name + "-wal"]
        assert events.quarantined.read_bytes() == b"this is not sqlite " * 300  # kept, never deleted
        assert (tmp_path / (events.quarantined.name + "-wal")).read_bytes() == b"stale wal"
        rows = events.recent(10)
        assert len(rows) == 1 and rows[0]["kind"] == "system" and rows[0]["status"] == "error"
        assert events.quarantined.name in rows[0]["reason"]
        assert "not a database" in rows[0]["error"]
        events.insert(kind="hook", status="spoken", text="after")
        assert events.count() == 2
    finally:
        events.close()
    # The next start opens the new file as it is.
    again = EventLog(db)
    assert again.quarantined is None and again.count() == 2
    again.close()
    assert len(_corrupt_copies(db)) == 3


def test_a_truncated_database_is_moved_aside(tmp_path):
    db = tmp_path / "events.db"
    events = EventLog(db)
    for i in range(2000):
        events.insert(kind="hook", status="spoken", text=f"line {i} " + "x" * 200)
    events.close()
    size = db.stat().st_size
    with open(db, "r+b") as f:
        f.truncate(size // 2)
    events = EventLog(db)
    try:
        assert events.quarantined is not None and events.quarantined.stat().st_size == size // 2
        rows = events.recent(10)
        assert [r["kind"] for r in rows] == ["system"] and rows[0]["status"] == "error"
    finally:
        events.close()


def test_a_locked_database_fails_clearly_and_is_left_alone(tmp_path):
    db = tmp_path / "events.db"
    events = EventLog(db)
    events.insert(kind="hook", status="spoken", text="keep me")
    events.close()
    other = sqlite3.connect(str(db), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO events (ts, kind, status) VALUES (1, 'hook', 'info')")
    try:
        t = time.monotonic()
        with pytest.raises(EventLogError, match="locked.*left as it is"):
            EventLog(db)
        assert time.monotonic() - t < 10
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert _corrupt_copies(db) == []
    events = EventLog(db)
    assert [r["text"] for r in events.recent(10)] == ["keep me"]
    events.close()


def test_the_wal_is_small_after_a_large_prune(tmp_path):
    now = time.time()
    events = EventLog(tmp_path / "events.db")
    for _ in range(400):
        events.insert(ts=now - 10 * 86400, kind="hook", status="spoken", text="x" * 20_000)
    n, _clips = events.prune_before(now - 7 * 86400)
    assert n == 400
    # No checkpoint of our own: prune_before's VACUUM is followed by one.
    assert (tmp_path / "events.db-wal").stat().st_size < 64 * 1024
    assert events._db.execute("PRAGMA journal_size_limit").fetchone()[0] == 8 * 1024 * 1024
    events.close()


# --------------------------------------------------------------------------- #
# The payload projection                                                      #
# --------------------------------------------------------------------------- #


RAW = {
    "session_id": "sess-1",
    "transcript_path": "/Users/me/.claude/projects/-Users-me-secret-project/abc.jsonl",
    "cwd": "/Users/me/project",
    "permission_mode": "default",
    "hook_event_name": "PermissionRequest",
    "prompt_id": "p-1",
    "model": "claude-opus",
    "stop_hook_active": False,
    "prompt": "please " + SECRET,
    "last_assistant_message": "reply " + SECRET,
    "message": "Claude needs your permission: " + SECRET,
    "tool_name": "Bash",
    "tool_input": {"command": "echo " + SECRET, "description": SECRET},
    "tool_response": {"stdout": SECRET, "stderr": ""},
    "permission_suggestions": [{"rules": [SECRET]}],
    "index": 3,
}


def test_projection_keeps_the_allow_list_and_drops_text():
    p = common.project_payload(RAW)
    assert SECRET not in json.dumps(p)
    for key in ("session_id", "cwd", "permission_mode", "hook_event_name", "prompt_id", "model", "tool_name"):
        assert p[key] == RAW[key], key
    assert p["stop_hook_active"] is False and p["index"] == 3
    assert p["transcript_path"] == "abc.jsonl"
    assert p["prompt_len"] == len(RAW["prompt"])
    assert p["last_assistant_message_len"] == len(RAW["last_assistant_message"])
    assert p["message_len"] == len(RAW["message"])
    assert p["tool_input_keys"] == ["command", "description"] and p["tool_input_len"] > 0
    assert p["tool_response_keys"] == ["stderr", "stdout"]
    assert p["permission_suggestions_len"] > 0
    for gone in ("prompt", "last_assistant_message", "message", "tool_input", "tool_response"):
        assert gone not in p


def test_projection_is_idempotent_and_reads_old_caps():
    p = common.project_payload(RAW)
    assert common.project_payload(p) == p
    long_key = {"k" * 59: "text", "k" * 64: "text"}
    once = common.project_payload(long_key)
    assert once == {"k" * 59 + "_len": 4} and common.project_payload(once) == once
    # A reply an older hook capped with cap_strings: the full length counts.
    capped = common.cap_strings({"last_assistant_message": "y" * 100}, limit=10)
    assert common.project_payload(capped) == {"last_assistant_message_len": 100}
    assert common.project_payload(None) is None and common.project_payload(["x"]) is None


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no stock python3")
def test_projection_runs_on_the_hooks_python():
    code = ("import json, sys; sys.path.insert(0, %r); import common; "
            "print(json.dumps(common.project_payload({'prompt': 'abc', 'session_id': 's'})))"
            % str(Path(common.__file__).parent))
    out = subprocess.run(["/usr/bin/python3", "-S", "-I", "-c", code], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"prompt_len": 3, "session_id": "s"}


# --------------------------------------------------------------------------- #
# What gets stored                                                            #
# --------------------------------------------------------------------------- #


def test_spooled_hook_events_hold_no_text(aw_home):
    # Daemon down, no fallback: the hook spools the event.
    message = f"<voice set=\"nova\"/>thinking about {SECRET}\n<say>Spoken line.</say>"
    proc, _ = run_hook(stop_payload(message), env={"AFTERWORD_FALLBACK": "none"})
    assert proc.returncode == 0
    proc, _ = run_hook({"session_id": "sess-1", "hook_event_name": "UserPromptSubmit", "prompt": SECRET,
                        "cwd": "/tmp/project"}, args=("user-prompt-submit",), env={"AFTERWORD_FALLBACK": "none"})
    assert proc.returncode == 0
    spool = aw_home / "spool.jsonl"
    assert spool.exists() and SECRET not in spool.read_text()
    events = EventLog(aw_home / "events.db")
    assert ingest_spool(events) == 2
    rows = [events.get(r["id"]) for r in events.recent(10)]
    stop = next(r for r in rows if r["hook_event"] == "Stop")
    assert stop["text"] == "Spoken line."
    assert stop["payload"]["last_assistant_message_len"] == len(message)
    prompt = next(r for r in rows if r["hook_event"] == "UserPromptSubmit")
    assert prompt["payload"]["prompt_len"] == len(SECRET)
    events.close()
    assert SECRET.encode() not in _db_bytes(aw_home / "events.db")


def test_raw_payloads_from_an_older_hook_are_projected(tmp_path):
    events = EventLog(tmp_path / "events.db")
    _import_record(events, {"decision": "info", "reason": "prompt", "event": "UserPromptSubmit",
                            "session_id": "s", "payload": dict(RAW)})
    row = events.get(events.recent(1)[0]["id"])
    assert row["payload"] == common.project_payload(RAW)
    events.close()


def _send_raw(sock_path: Path, message: dict) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(str(sock_path))
    s.sendall(json.dumps(message).encode() + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf)


def test_the_daemon_stores_no_prompt_or_reply_text(aw_home, monkeypatch):
    from test_daemon import RunningDaemon

    d = RunningDaemon(aw_home, monkeypatch)
    try:
        # The real hook: a reply with text outside its <say>.
        proc, _ = run_hook(stop_payload(f"<voice set=\"nova\"/>private {SECRET} notes <say>Only this.</say>", session_id="priv-1"))
        assert proc.returncode == 0
        ev = wait_for(lambda: next((e for e in d.events() if e.get("text") == "Only this."
                                    and e["status"] == "spoken"), None))
        assert ev
        # An older hook that still sends the raw payload.
        raw = {"v": 1, "type": "hook", "event": "PermissionRequest", "t_hook": time.time(),
               "session_id": "priv-1", "decision": "info", "reason": "alerts off", "text": "",
               "payload": dict(RAW)}
        ack = _send_raw(aw_home / "afterword.sock", raw)
        assert ack.get("ok"), ack
        full = d.get(f"/api/events/{ack['event_id']}")
        assert full["payload"] == common.project_payload(RAW)
        assert SECRET not in json.dumps(d.get("/api/events?full=1"))
    finally:
        d.stop()
    assert SECRET.encode() not in _db_bytes(aw_home / "events.db")


def test_old_raw_rows_are_scrubbed_once_at_startup(tmp_path, caplog):
    db = tmp_path / "events.db"
    events = EventLog(db)
    hook = events.insert(kind="hook", status="spoken", text="Spoken.", payload=dict(RAW))["id"]
    alert = events.insert(kind="alert", status="spoken", text="Needs you.", payload=dict(RAW))["id"]
    test = events.insert(kind="test", status="spoken", text="Test.", payload={"text": "<say>Test.</say>"})["id"]
    bad = events.insert(kind="hook", status="info", text="")["id"]
    events._db.execute("UPDATE events SET payload = ? WHERE id = ?", ("not json " + SECRET, bad))
    # As a database from before 0.3.5 (and its raw rows) would be.
    events._db.execute("PRAGMA user_version = 0")
    events.close()
    assert SECRET.encode() in _db_bytes(db)

    with caplog.at_level(logging.INFO, logger="afterword.log"):
        events = EventLog(db)
    try:
        assert any("3 stored hook payload" in r.getMessage() for r in caplog.records)
        assert events.get(hook)["payload"] == common.project_payload(RAW)
        assert events.get(alert)["payload"] == common.project_payload(RAW)
        assert events.get(test)["payload"] == {"text": "<say>Test.</say>"}  # `afterword test` input stays
        assert events.get(bad)["payload"] is None
        assert events.get(hook)["text"] == "Spoken."
        assert events._db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert events.scrub_payloads() == 0
    finally:
        events.close()
    assert SECRET.encode() not in _db_bytes(db)  # VACUUMed out of the file and its WAL
