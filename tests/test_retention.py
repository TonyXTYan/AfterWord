"""events.db retention: [daemon] keep_days (7 by default, 0 = keep all).
Old rows go with the clips they point at; rows still in flight stay."""

import time
from pathlib import Path
from types import SimpleNamespace

from afterword.daemon.config import DaemonSettings, build_config
from afterword.daemon.log import EventLog
from afterword.daemon.server import Daemon

DAY = 86400


def _settings(**daemon):
    return SimpleNamespace(daemon=DaemonSettings(**daemon))


def _host(tmp_path: Path, keep_days: int):
    clips = tmp_path / "clips"
    clips.mkdir()
    events = EventLog(tmp_path / "events.db")
    store = SimpleNamespace(get=lambda: _settings(keep_days=keep_days))
    return SimpleNamespace(store=store, events=events, clips_dir=clips), events, clips


def _row(events, clips, age_days, now, status="spoken", clip=True):
    ev = events.insert(ts=now - age_days * DAY, kind="hook", status=status, text=f"{age_days} days old")
    if clip:
        path = clips / f"{ev['id']}.wav"
        path.write_bytes(b"RIFF")
        events.update(ev["id"], clip=str(path))
    return ev["id"]


def test_default_is_seven_days_and_zero_is_allowed():
    assert DaemonSettings().keep_days == 7
    cfg, errors = build_config({"daemon": {"keep_days": 0}})
    assert cfg.daemon.keep_days == 0 and not errors
    bad, errors = build_config({"daemon": {"keep_days": -1}})
    assert bad.daemon.keep_days == 7 and any("keep_days" in e for e in errors)


def test_old_rows_and_their_clips_are_deleted(tmp_path):
    now = time.time()
    host, events, clips = _host(tmp_path, keep_days=7)
    old = _row(events, clips, 8, now)
    old_no_clip = _row(events, clips, 30, now, clip=False)
    fresh = _row(events, clips, 6, now)
    in_flight = _row(events, clips, 9, now, status="playing")

    assert Daemon.prune_events(host, now=now) == 2
    left = {e["id"] for e in events.recent(100)}
    assert left == {fresh, in_flight}
    assert old_no_clip not in left
    assert not (clips / f"{old}.wav").exists()
    assert (clips / f"{fresh}.wav").exists() and (clips / f"{in_flight}.wav").exists()
    assert Daemon.prune_events(host, now=now) == 0


def test_zero_days_keeps_everything(tmp_path):
    now = time.time()
    host, events, clips = _host(tmp_path, keep_days=0)
    old = _row(events, clips, 400, now)
    assert Daemon.prune_events(host, now=now) == 0
    assert [e["id"] for e in events.recent(10)] == [old]
    assert (clips / f"{old}.wav").exists()


def test_only_files_in_the_clips_folder_are_deleted(tmp_path):
    now = time.time()
    host, events, clips = _host(tmp_path, keep_days=7)
    outside = tmp_path / "keep-me.wav"
    outside.write_bytes(b"RIFF")
    ev = events.insert(ts=now - 10 * DAY, kind="hook", status="spoken", text="x", clip=str(outside))
    assert Daemon.prune_events(host, now=now) == 1
    assert events.get(ev["id"]) is None
    assert outside.exists()


def test_a_large_prune_shrinks_the_file(tmp_path):
    now = time.time()
    events = EventLog(tmp_path / "events.db")
    for _ in range(400):
        events.insert(ts=now - 10 * DAY, kind="hook", status="spoken", text="x" * 2000)
    events._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = (tmp_path / "events.db").stat().st_size
    n, clips = events.prune_before(now - 7 * DAY)
    events._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert n == 400 and clips == []
    assert (tmp_path / "events.db").stat().st_size < before / 4
