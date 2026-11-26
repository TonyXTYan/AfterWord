"""Daemon resilience: the worker survives failed bookkeeping, background
tasks are restarted, playback waits are bounded, and crashes leave a
trace (faulthandler, the daemon.run marker)."""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from afterword.daemon import player as players
from afterword.daemon import server, stream
from afterword.daemon.stream import NullStreamPlayer

from conftest import ROOT, TIME_SCALE, wait_for
from test_daemon import RunningDaemon
from test_stream import ChunkSynth, make_daemon, play_many, ramp_chunks, run


def _queue(d, text, session):
    ack = d.dispatch(json.dumps({"type": "test", "text": text, "profile": "nova", "session_id": session}).encode(),
                     time.time())
    return ack["event_id"]


async def _until(pred, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return pred()


# --------------------------------------------------------------------------- #
# The worker's error path                                                       #
# --------------------------------------------------------------------------- #


class FailFirst(ChunkSynth):
    """Fails "bad" before any audio; streams everything else."""

    def stream(self, text, profile, attrs, sink, cancel):
        if text == "bad":
            raise RuntimeError("render failed")
        return super().stream(text, profile, attrs, sink, cancel)


def test_failed_error_bookkeeping_does_not_stop_the_worker(aw_home, monkeypatch, caplog):
    """events.update raising in the worker's error path (a locked
    events.db) used to end the worker: every later line stayed queued."""
    d = make_daemon(aw_home, monkeypatch, FailFirst(ramp_chunks()))
    real_update = d.events.update

    def update(event_id, **fields):
        if fields.get("status") == "error":
            raise sqlite3.OperationalError("database is locked")
        return real_update(event_id, **fields)

    d.events.update = update

    async def go():
        worker = asyncio.create_task(d.worker())
        bad = _queue(d, "bad", "s0")
        good = [_queue(d, f"good {i}", f"s{i + 1}") for i in range(2)]
        ok = await _until(lambda: all(d.events.get(e)["status"] == "spoken" for e in good))
        alive = not worker.done()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        return bad, good, ok, alive

    bad, good, ok, alive = run(go())
    assert ok and alive, [d.events.get(e) for e in good]
    assert any("could not mark event %d failed" % bad in r.getMessage() for r in caplog.records)
    assert d.queue.now_playing is None


# --------------------------------------------------------------------------- #
# TaskSupervisor                                                                #
# --------------------------------------------------------------------------- #


class FakeEvents:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def insert(self, **fields):
        if self.fail:
            raise sqlite3.OperationalError("disk I/O error")
        self.rows.append(fields)
        return fields


def fast_supervisor(events, stopping=lambda: False, max_rapid=8):
    sup = server.TaskSupervisor(stopping, events)
    sup.RESTART_FIRST_S = 0.01
    sup.RESTART_MAX_S = 0.04
    sup.MAX_RAPID_FAILURES = max_rapid
    return sup


def test_a_task_that_raises_is_restarted_with_a_system_row():
    events = FakeEvents()
    runs = []

    async def flaky():
        runs.append(time.monotonic())
        if len(runs) <= 2:
            raise RuntimeError(f"boom {len(runs)}")
        await asyncio.sleep(30)

    async def go():
        sup = fast_supervisor(events)
        sup.start("worker", flaky)
        await _until(lambda: len(runs) == 3, 3)
        await asyncio.sleep(0.05)
        snap = sup.snapshot()
        for t in sup.close():
            t.cancel()
        return sup, snap

    sup, snap = run(go())
    assert len(runs) == 3 and sup.restarts == {"worker": 2}
    assert snap == {"worker": "running"}
    assert [r["kind"] for r in events.rows] == ["system", "system"]
    assert all(r["status"] == "error" for r in events.rows)
    assert "daemon task worker failed: RuntimeError: boom 1; restarting it in 0 s" == events.rows[0]["reason"]
    # The backoff doubles (capped): the second wait is no shorter than the first.
    assert runs[2] - runs[1] >= runs[1] - runs[0] - 0.005


def test_a_loop_that_returns_is_restarted_and_a_failed_record_is_tolerated():
    events = FakeEvents(fail=True)  # recording the failure fails too: still restarted
    runs = []

    async def returns():
        runs.append(1)
        if len(runs) > 1:
            await asyncio.sleep(30)

    async def go():
        sup = fast_supervisor(events)
        sup.start("spool", returns)
        await _until(lambda: len(runs) == 2, 3)
        for t in sup.close():
            t.cancel()
        return sup

    sup = run(go())
    assert len(runs) == 2 and sup.restarts == {"spool": 1}


def test_supervisor_gives_up_after_rapid_failures():
    events = FakeEvents()
    runs = []

    async def always():
        runs.append(1)
        raise ValueError("bad config")

    async def go():
        sup = fast_supervisor(events, max_rapid=3)
        sup.start("retention", always)
        await _until(lambda: "retention" in sup.given_up, 3)
        await asyncio.sleep(0.1)
        snap = sup.snapshot()
        sup.close()
        return sup, snap

    sup, snap = run(go())
    assert len(runs) == 4  # the first run and three restarts
    assert snap == {"retention": "failed"}
    assert "gave up after 4 failures in a row" in events.rows[-1]["reason"]


def test_oneshot_that_finishes_is_left_alone_and_stopping_stops_restarts():
    events = FakeEvents()
    stopping = [False]
    warm_runs, loop_runs = [], []

    async def warm():
        warm_runs.append(1)

    async def loop_task():
        loop_runs.append(1)
        raise RuntimeError("died during shutdown")

    async def go():
        sup = fast_supervisor(events, stopping=lambda: stopping[0])
        sup.start("warm", warm, oneshot=True)
        await asyncio.sleep(0.1)
        stopping[0] = True
        sup.start("worker", loop_task)
        await asyncio.sleep(0.1)
        snap = sup.snapshot()
        sup.close()
        return snap

    snap = run(go())
    assert warm_runs == [1] and loop_runs == [1] and events.rows == []
    assert snap == {"warm": "done", "worker": "done"}


def test_running_daemon_restarts_its_worker(aw_home, monkeypatch):
    monkeypatch.setattr(server.TaskSupervisor, "RESTART_FIRST_S", 0.05)
    d = RunningDaemon(aw_home, monkeypatch)
    try:
        st = d.get("/api/status")
        assert st["tasks"]["worker"] == "running" and st["tasks"]["spool"] == "running"
        old = d.daemon.supervisor.tasks["worker"]
        d.loop.call_soon_threadsafe(old.cancel)  # the worker dies while the daemon runs
        assert wait_for(lambda: d.daemon.supervisor.tasks["worker"] is not old
                        and not d.daemon.supervisor.tasks["worker"].done(), 5)
        ack = d.call(d.daemon.dispatch, json.dumps({"type": "test", "text": "after the restart",
                                                    "session_id": "s1"}).encode(), time.time())
        assert wait_for(lambda: d.get(f"/api/events/{ack['event_id']}")["status"] == "spoken", 5)
        rows = [e for e in d.events() if e.get("kind") == "system" and e.get("status") == "error"]
        assert any("daemon task worker was cancelled; restarting it" in (e.get("reason") or "") for e in rows)
    finally:
        d.stop()


# --------------------------------------------------------------------------- #
# Bounded playback waits                                                        #
# --------------------------------------------------------------------------- #


def test_play_timeout_bound():
    assert players.play_timeout_s(None) == players.PLAY_UNKNOWN_S == 60.0
    assert players.play_timeout_s(float("nan")) == 60.0
    assert players.play_timeout_s(4.0, 2.0) == pytest.approx(12.0)
    assert players.play_timeout_s(4.0, 0.5) == pytest.approx(18.0)


class HangingOutput(stream._NullOutput):
    """A device whose stop / abort never return until `release` is set
    (PortAudio stuck in Pa_StopStream): the Playback thread never ends."""

    def __init__(self, callback, speed, release):
        super().__init__(callback, speed)
        self.release = release

    def stop(self):
        super().stop()
        self.release.wait()

    abort = stop

    def close(self):
        self.stop()


class HangingPlayer(NullStreamPlayer):
    def __init__(self, release, **kw):
        super().__init__(**kw)
        self.release = release
        self.playbacks = []

    def _opener(self, callback):
        if self.opened == 1:
            return HangingOutput(callback, self.speed, self.release)
        return super()._opener(callback)

    def open(self, rate=1.0, **kw):
        pb = super().open(rate, **kw)
        self.playbacks.append(pb)
        return pb


def test_stuck_stream_times_out_and_later_lines_use_afplay(aw_home, monkeypatch):
    monkeypatch.setattr(players, "PLAY_SLACK_S", 0.3)
    release = threading.Event()
    d = make_daemon(aw_home, monkeypatch, ChunkSynth(ramp_chunks()))  # 0.4 s of audio
    d.stream_player = HangingPlayer(release, speed=50.0)

    async def go():
        worker = asyncio.create_task(d.worker())
        try:
            t0 = time.time()
            ids = [_queue(d, t, f"s{i}") for i, t in enumerate(["first", "second", "third"])]
            assert await _until(lambda: all(d.events.get(e)["status"] in ("spoken", "error") for e in ids))
            elapsed = time.time() - t0
            first, second, third = (d.events.get(e) for e in ids)
            stream_error = d.status()["stream"]["error"]
            release.set()
            assert await _until(lambda: d.stream_player.playbacks[0].done.is_set(), 5)
            fourth = _queue(d, "fourth", "s9")
            assert await _until(lambda: d.events.get(fourth)["status"] in ("spoken", "error"))
            return first, second, third, d.events.get(fourth), elapsed, stream_error
        finally:
            release.set()
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    first, second, third, fourth, elapsed, stream_error = run(go())
    assert first["status"] == "error" and first["reason"] == "playback timed out", first
    assert "did not finish within" in first["error"] and first["clip"]
    assert elapsed < 5
    # The next line plays with afplay (the null player here), and so does
    # the one after: the stuck stream's thread had not ended.
    for ev in (second, third):
        assert ev["status"] == "spoken" and ev["timings"]["stream"] is False, ev
    assert "did not finish within" in stream_error
    # Once it has ended, lines stream again.
    assert fourth["status"] == "spoken" and fourth["timings"]["stream"] is True, fourth
    assert d.status()["stream"]["error"] is None


def test_line_after_a_timeout_uses_afplay_even_once_the_stream_ends(aw_home, monkeypatch):
    monkeypatch.setattr(players, "PLAY_SLACK_S", 0.3)
    release = threading.Event()
    d = make_daemon(aw_home, monkeypatch, ChunkSynth(ramp_chunks()))
    d.stream_player = HangingPlayer(release, speed=50.0)

    async def go():
        worker = asyncio.create_task(d.worker())
        try:
            first = _queue(d, "first", "s0")
            assert await _until(lambda: d.events.get(first)["status"] == "error")
            release.set()
            assert await _until(lambda: d.stream_player.playbacks[0].done.is_set(), 5)
            ids = [_queue(d, t, f"s{i + 1}") for i, t in enumerate(["second", "third"])]
            assert await _until(lambda: all(d.events.get(e)["status"] == "spoken" for e in ids))
            return [d.events.get(e) for e in [first] + ids]
        finally:
            release.set()
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker

    first, second, third = run(go())
    assert first["reason"] == "playback timed out"
    assert second["timings"]["stream"] is False and third["timings"]["stream"] is True


def _stub(tmp_path, body):
    """An afplay stand-in running `body` for a clip. It is run once first:
    macOS checks a new executable on its first launch, which can take
    longer than the short test timeouts."""
    stub = tmp_path / "afplay"
    stub.write_text('#!/bin/sh\ncase "$1" in *.wav) ;; *) exit 0 ;; esac\n' + body + "\n")
    stub.chmod(0o755)
    subprocess.run([str(stub), "warm-up"], check=True, timeout=30)
    return stub


def test_afplay_that_never_finishes_is_killed_and_the_queue_goes_on(aw_home, monkeypatch, tmp_path):
    monkeypatch.setattr(players, "PLAY_SLACK_S", 1.5 * TIME_SCALE)
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    d = make_daemon(aw_home, monkeypatch, player_env=None)
    assert d.stream_player is None and d.player.name == "afplay"
    pid_file = tmp_path / "pid"
    afplay = d.player
    afplay.binary = str(_stub(tmp_path, f"echo $$ > {pid_file}\nexec sleep 60"))
    real_play = afplay.play
    calls = []

    async def play(path, rate=1.0):
        calls.append(path)
        if len(calls) == 1:
            return await real_play(path, rate)
        return True  # the next line: a player that works

    afplay.play = play
    first, second = run(play_many(d, ["first", "second"], timeout=30))
    assert first["status"] == "error" and first["reason"] == "playback timed out", first
    assert "afplay did not finish within" in first["error"]
    assert second["status"] == "spoken", second
    # (Process launches can be slow here: if afplay was killed before the
    # shell got to write its pid, there is nothing more to check.)
    if pid_file.exists():
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)  # killed and reaped
    assert afplay._proc is None


# --------------------------------------------------------------------------- #
# Crash traces                                                                  #
# --------------------------------------------------------------------------- #


def test_clean_run_leaves_no_marker_and_no_unclean_row(aw_home, monkeypatch):
    d = RunningDaemon(aw_home, monkeypatch)
    marker = aw_home / server.RUN_MARKER
    try:
        data = json.loads(marker.read_text())
        assert data["pid"] == os.getpid() and data["started_at"] <= time.time()
        assert not any("did not shut down cleanly" in (e.get("reason") or "") for e in d.events())
    finally:
        d.stop()
    assert not marker.exists()


def test_leftover_marker_is_reported_as_a_system_row(aw_home, monkeypatch, caplog):
    started = time.time() - 3600
    (aw_home / server.RUN_MARKER).write_text(json.dumps({"pid": 999999, "started_at": started}))
    d = RunningDaemon(aw_home, monkeypatch)
    try:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
        want = f"previous daemon (pid 999999, started {when}) did not shut down cleanly"
        rows = [e for e in d.events() if e.get("reason") == want]
        assert len(rows) == 1 and rows[0]["kind"] == "system" and rows[0]["status"] == "error"
        assert any(want in r.getMessage() for r in caplog.records)
        assert json.loads((aw_home / server.RUN_MARKER).read_text())["pid"] == os.getpid()
    finally:
        d.stop()


def test_unreadable_marker_still_counts(aw_home, monkeypatch):
    (aw_home / server.RUN_MARKER).write_text("{not json")
    d = RunningDaemon(aw_home, monkeypatch)
    try:
        assert any(e.get("reason") == "previous daemon (pid unknown, started unknown time) did not shut down cleanly"
                   for e in d.events())
    finally:
        d.stop()


def test_crash_handlers_trace_native_crashes_and_thread_exceptions():
    """In a child process: a thread's uncaught exception is logged, and a
    segfault prints every thread's stack to stderr."""
    code = (
        "import threading, time, faulthandler\n"
        "from afterword.daemon.server import install_crash_handlers\n"
        "assert install_crash_handlers()\n"
        "def boom():\n"
        "    raise RuntimeError('thread blew up')\n"
        "t = threading.Thread(target=boom, name='boomer'); t.start(); t.join()\n"
        "threading.Thread(target=time.sleep, args=(30,), name='sleeper', daemon=True).start()\n"
        "time.sleep(0.1)\n"
        "faulthandler._sigsegv()\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, cwd=str(ROOT))
    assert proc.returncode != 0
    err = proc.stderr
    assert "uncaught exception in thread boomer" in err and "RuntimeError: thread blew up" in err
    assert "Fatal Python error" in err
    assert err.count("Thread 0x") >= 2 or "Current thread" in err and "Thread 0x" in err


def test_crash_handlers_without_a_real_stderr(monkeypatch):
    import io

    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    assert server.install_crash_handlers(io.StringIO()) is False
    assert threading.excepthook is server._thread_excepthook
