"""The daemon end to end: real hook subprocess -> Unix socket -> queue ->
(null) synth -> (null) player -> SQLite event log -> HTTP API / SSE."""

import asyncio
import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from conftest import TIME_SCALE, run_hook, stop_payload, wait_for


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class RunningDaemon:
    def __init__(self, home, monkeypatch, synth_seconds=0.05):
        monkeypatch.setenv("AFTERWORD_SYNTH", "null")
        monkeypatch.setenv("AFTERWORD_PLAYER", "null")
        from afterword.daemon import server

        self.port = free_port()
        self.daemon = None
        self.loop = None
        ready = threading.Event()

        def on_ready(d):
            self.daemon = d
            self.loop = asyncio.get_running_loop()
            ready.set()

        def target():
            asyncio.run(server.run_daemon(port=self.port, http=True, on_ready=on_ready))

        self.thread = threading.Thread(target=target, daemon=True)
        self.thread.start()
        assert ready.wait(10 * TIME_SCALE), "daemon did not start"
        assert wait_for(lambda: self._http_up(), 5 * TIME_SCALE)
        self.base = f"http://127.0.0.1:{self.port}"

    def _http_up(self):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/status", timeout=0.5)
            return True
        except OSError:
            return False

    def get(self, path, headers=None):
        req = urllib.request.Request(self.base + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=3 * TIME_SCALE) as r:
            return json.load(r)

    def post(self, path, headers=None):
        req = urllib.request.Request(self.base + path, data=b"", method="POST", headers=headers or {})
        with urllib.request.urlopen(req, timeout=3 * TIME_SCALE) as r:
            return json.load(r)

    def events(self):
        return self.get("/api/events")["events"]

    def call(self, fn, *args):
        """Run fn in the daemon's loop thread and return its result."""
        fut = asyncio.run_coroutine_threadsafe(_wrap(fn, *args), self.loop)
        return fut.result(5 * TIME_SCALE)

    def stop(self):
        self.loop.call_soon_threadsafe(self.daemon.request_shutdown)
        self.thread.join(10 * TIME_SCALE)


async def _wrap(fn, *args):
    return fn(*args)


@pytest.fixture
def daemon(aw_home, monkeypatch):
    d = RunningDaemon(aw_home, monkeypatch)
    yield d
    d.stop()


def event_by_text(d, text):
    return next((e for e in d.events() if e.get("text") == text), None)


def test_default_config_written_and_status(daemon, aw_home):
    assert (aw_home / "config.toml").exists()
    st = daemon.get("/api/status")
    assert st["synth"] == "null" and st["player"] == "null"
    assert "nova" in st["config"]["profiles"]
    assert oct((aw_home / "afterword.sock").stat().st_mode & 0o777) == "0o600"


def test_hook_event_is_spoken_and_logged(daemon, aw_home, say_stub):
    proc, _ = run_hook(stop_payload('<voice set="nova"/><say mood="dry">Through the daemon.</say>'))
    assert proc.returncode == 0
    ev = wait_for(lambda: (e := event_by_text(daemon, "Through the daemon.")) and e["status"] == "spoken" and e)
    assert ev, daemon.events()
    assert ev["kind"] == "hook" and ev["hook_event"] == "Stop" and ev["delivery"] == "socket"
    assert ev["profile"] == "nova" and ev["attrs"] == {"mood": "dry"}
    for key in ("hook_ms", "hook_to_daemon_ms", "queue_wait_ms", "synth_ms", "audio_s", "rtf", "first_audio_ms"):
        assert key in ev["timings"], key
    full = daemon.get(f"/api/events/{ev['id']}")
    # The stored payload is the projection: the reply's length, not its text.
    assert "last_assistant_message" not in full["payload"]
    assert full["payload"]["last_assistant_message_len"] == len('<voice set="nova"/><say mood="dry">Through the daemon.</say>')
    assert full["payload"]["transcript_path"] == "transcript.jsonl"
    assert full["parsed"]["voice"] == {"set": "nova"}
    time.sleep(0.2)
    assert not say_stub.exists()  # the daemon spoke, not the fallback


def test_skips_are_logged_with_reason(daemon):
    run_hook(stop_payload("<say>off session</say>", session_id="off-1"))
    run_hook(stop_payload('<voice set="nova"/>', session_id="on-1"))
    reasons = wait_for(lambda: {e["reason"] for e in daemon.events() if e["kind"] == "hook"} >= {"session off", "no <say> tag"} and True)
    assert reasons, daemon.events()


def test_cli_test_message_and_status(daemon, capsys):
    from afterword import cli

    assert cli.main(["test", "Hello from the CLI", "--wait"]) == 0
    out = capsys.readouterr().out
    assert "spoken" in out and "synth_ms" in out
    assert cli.main(["status"]) == 0
    assert "afterwordd" in capsys.readouterr().out


def test_supersede_and_busy_are_logged(aw_home, monkeypatch):
    """Deterministic: a Daemon with no worker running, so nothing leaves the queue."""
    monkeypatch.setenv("AFTERWORD_SYNTH", "null")
    monkeypatch.setenv("AFTERWORD_PLAYER", "null")
    from afterword.daemon.config import ConfigStore
    from afterword.daemon.log import EventLog
    from afterword.daemon.server import Daemon

    (aw_home / "config.toml").write_text("[daemon]\nmax_queue = 2\n")

    async def main():
        events = EventLog(aw_home / "events.db")
        d = Daemon(aw_home, ConfigStore(), events)

        def send(text, sid):
            return d.dispatch(json.dumps({"type": "test", "text": text, "session_id": sid}).encode(), time.time())

        a1 = send("a one", "A")
        b1 = send("b one", "B")
        c1 = send("c one", "C")          # queue full -> busy
        a2 = send("a two", "A")          # supersedes a1, keeps its place
        assert [i.text for i in d.queue._items] == ["a two", "b one"]
        out = {k: events.get(v["event_id"]) for k, v in dict(a1=a1, b1=b1, c1=c1, a2=a2).items()}
        events.close()
        return out

    ev = asyncio.run(main())
    assert ev["c1"]["status"] == "skipped" and ev["c1"]["reason"] == "daemon busy"
    assert ev["a1"]["status"] == "skipped" and ev["a1"]["reason"] == f"superseded by #{ev['a2']['id']}"
    assert ev["a2"]["status"] == "queued" and ev["b1"]["status"] == "queued"


def test_stop_all_interrupts_playback(aw_home, monkeypatch):
    from afterword.daemon import synth

    monkeypatch.setattr(synth.NullSynth, "__init__", lambda self, seconds=5.0: setattr(self, "seconds", 5.0))
    d = RunningDaemon(aw_home, monkeypatch)
    try:
        ack = d.call(d.daemon.dispatch, json.dumps({"type": "test", "text": "long one"}).encode(), time.time())
        assert wait_for(lambda: d.get(f"/api/events/{ack['event_id']}")["status"] == "playing")
        res = d.post("/api/queue/stop", headers={"X-AfterWord": "1"})
        assert res["current"] == ack["event_id"]
        ev = wait_for(lambda: (e := d.get(f"/api/events/{ack['event_id']}")) and e["status"] == "skipped" and e)
        assert ev["reason"] == "stopped by user"
    finally:
        d.stop()


def test_spooled_events_are_imported_on_start(aw_home, monkeypatch, say_stub):
    run_hook(stop_payload('<voice set="nova"/><say>while down</say>'))
    assert (aw_home / "spool.jsonl").exists()
    d = RunningDaemon(aw_home, monkeypatch)
    try:
        ev = event_by_text(d, "while down")
        assert ev and ev["delivery"] == "spool" and ev["status"] == "spoken"
        assert ev["reason"] == "daemon down: spoken by say fallback"
        assert not (aw_home / "spool.jsonl").exists()
    finally:
        d.stop()


def test_post_requires_header_and_host_is_checked(daemon):
    with pytest.raises(urllib.error.HTTPError) as e:
        daemon.post("/api/queue/stop")
    assert e.value.code == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        daemon.get("/api/status", headers={"Host": "evil.example:47800"})
    assert e.value.code == 403


def test_ui_page_and_sse_stream(daemon):
    with urllib.request.urlopen(daemon.base + "/", timeout=3 * TIME_SCALE) as r:
        html = r.read().decode()
    assert "AfterWord" in html and "/api/stream" in html

    got = []

    def reader():
        with urllib.request.urlopen(daemon.base + "/api/stream", timeout=5) as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:") and "sse probe" in line:
                    got.append(json.loads(line[5:]))
                    return

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.3)
    daemon.call(daemon.daemon.dispatch, json.dumps({"type": "test", "text": "sse probe"}).encode(), time.time())
    t.join(5)
    assert got and got[0]["text"] == "sse probe"


def test_second_daemon_refuses_to_start(daemon, aw_home):
    from afterword.daemon import server

    with pytest.raises(server.AlreadyRunning):
        asyncio.run(server.run_daemon(port=free_port(), http=False))
    # Even with the lock file gone, a refused start must leave the running
    # daemon's socket alone.
    (aw_home / "afterwordd.lock").unlink()
    with pytest.raises(server.AlreadyRunning):
        asyncio.run(server.run_daemon(port=free_port(), http=False))
    assert (aw_home / "afterword.sock").exists()
    assert daemon.get("/api/status")["pid"]


def test_bad_socket_messages_get_error_acks(daemon, aw_home):
    def send(raw):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(str(aw_home / "afterword.sock"))
        s.sendall(raw)
        data = s.recv(65536)
        s.close()
        return json.loads(data)

    assert send(b"garbage\n")["ok"] is False
    assert send(b"[1,2]\n")["ok"] is False
    assert send(b'{"type":"nope"}\n')["ok"] is False
    ack = send(b'{"type":"hook","decision":"speak","text":"   "}\n')
    assert ack["ok"] and ack["status"] == "skipped"
    ack = send(b'{"type":"hook","decision":42}\n')
    assert ack["status"] == "error"


def test_cross_site_and_wrong_port_refused(daemon):
    for headers in (
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": "https://evil.example"},
        {"Host": f"127.0.0.1:{daemon.port + 1}"},
    ):
        with pytest.raises(urllib.error.HTTPError) as e:
            daemon.get("/api/status", headers=headers)
        assert e.value.code == 403, headers
    ok = daemon.get("/api/status", headers={"Sec-Fetch-Site": "same-origin", "Origin": daemon.base})
    assert ok["pid"]
    with urllib.request.urlopen(daemon.base + "/", timeout=3 * TIME_SCALE) as r:
        assert r.headers["X-Frame-Options"] == "DENY"


def test_unfinished_rows_recovered_and_queue_drained_on_shutdown(aw_home, monkeypatch):
    from afterword.daemon import synth

    monkeypatch.setattr(synth.NullSynth, "__init__", lambda self, seconds=5.0: setattr(self, "seconds", 5.0))
    d = RunningDaemon(aw_home, monkeypatch)
    acks = [
        d.call(d.daemon.dispatch, json.dumps({"type": "test", "text": f"line {i}", "session_id": f"s{i}"}).encode(), time.time())
        for i in range(3)
    ]
    assert wait_for(lambda: d.get(f"/api/events/{acks[0]['event_id']}")["status"] == "playing")
    d.stop()
    d2 = RunningDaemon(aw_home, monkeypatch)
    try:
        for ack in acks:
            ev = d2.get(f"/api/events/{ack['event_id']}")
            assert ev["status"] == "skipped", ev
            assert ev["reason"] in ("daemon shutting down", "stopped by user"), ev
    finally:
        d2.stop()


def test_recover_unfinished_marks_stuck_rows(aw_home):
    from afterword.daemon.log import EventLog

    ev = EventLog(aw_home / "events.db")
    a = ev.insert(kind="hook", status="playing")
    b = ev.insert(kind="hook", status="spoken")
    assert ev.recover_unfinished("daemon restarted") == 1
    assert ev.get(a["id"])["status"] == "skipped" and ev.get(a["id"])["reason"] == "daemon restarted"
    assert ev.get(b["id"])["status"] == "spoken"
    ev.close()


def test_bad_spool_records_and_leftover_claims(aw_home):
    from afterword.daemon.log import EventLog, ingest_spool

    good = {"decision": "skip", "reason": "session off", "session_id": "s", "event": "Stop", "t_hook": 1.0}
    bad = {"decision": "speak", "cwd": ["not", "a", "string"], "session_id": {"x": 1}, "text": 5,
           "t_hook": "soon", "fallback": "say"}
    (aw_home / "spool.jsonl").write_text("\n".join([json.dumps(bad), "not json", json.dumps(good)]) + "\n")
    (aw_home / "spool.jsonl.123.ingest").write_text(json.dumps(good) + "\n")
    ev = EventLog(aw_home / "events.db")
    assert ingest_spool(ev) == 3
    rows = ev.recent()
    assert sorted(r["status"] for r in rows) == ["skipped", "skipped", "spoken"]
    assert not list(aw_home.glob("spool.jsonl*"))
    ev.close()


def test_unknown_backend_in_config_is_a_warning(aw_home):
    from afterword.daemon.config import ConfigStore

    (aw_home / "config.toml").write_text('[daemon]\nsynth = "festival"\nplayer = "vlc"\n')
    store = ConfigStore()
    cfg = store.get()
    assert cfg.daemon.synth == "qwen" and cfg.daemon.player == "afplay"
    assert any("daemon.synth" in w for w in store.warnings)


def test_max_queue_hot_reload_applies_at_enqueue(aw_home, monkeypatch):
    monkeypatch.setenv("AFTERWORD_SYNTH", "null")
    monkeypatch.setenv("AFTERWORD_PLAYER", "null")
    from afterword.daemon.config import ConfigStore
    from afterword.daemon.log import EventLog
    from afterword.daemon.server import Daemon

    cfg = aw_home / "config.toml"
    cfg.write_text("[daemon]\nmax_queue = 1\n")

    async def main():
        events = EventLog(aw_home / "events.db")
        d = Daemon(aw_home, ConfigStore(), events)
        send = lambda t, sid: d.dispatch(json.dumps({"type": "test", "text": t, "session_id": sid}).encode(), time.time())
        assert send("a", "A")["status"] == "queued"
        assert send("b", "B")["reason"] == "daemon busy"
        cfg.write_text("[daemon]\nmax_queue = 5\n")
        os.utime(cfg, (time.time() + 5, time.time() + 5))
        r = send("c", "C")
        events.close()
        return r

    import os

    assert asyncio.run(main())["status"] == "queued"


def test_shutdown_with_open_sse_stream_is_prompt(aw_home, monkeypatch):
    d = RunningDaemon(aw_home, monkeypatch)
    opened = threading.Event()

    def reader():
        try:
            with urllib.request.urlopen(d.base + "/api/stream", timeout=10) as r:
                opened.set()
                for _ in r:
                    pass
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()
    assert opened.wait(5)
    t = time.time()
    d.stop()
    assert time.time() - t < 1.5 * TIME_SCALE


def test_sigterm_runs_cleanup(aw_home, tmp_path):
    """A real `afterword serve` process: SIGTERM must exit 0, remove the
    socket and log 'daemon stopped'."""
    import os
    import signal
    import subprocess
    import sys

    env = dict(os.environ, AFTERWORD_SYNTH="null", AFTERWORD_PLAYER="null")
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "afterword.cli", "serve", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        def up():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=0.5)
                return True
            except OSError:
                return False

        assert wait_for(up, 15)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert not (aw_home / "afterword.sock").exists()
    from afterword.daemon.log import EventLog

    ev = EventLog(aw_home / "events.db")
    assert ev.recent(1)[0]["reason"] == "daemon stopped"
    ev.close()
