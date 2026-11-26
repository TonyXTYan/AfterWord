"""The Config page's Daemon section (docs/gui-api.md, "Daemon"): GET/POST
/api/service, /api/service/restart, /api/service/prune,
/api/service/reveal and /api/logs/{name}, against the fake launchd of
tests/test_service.py; plus the daemon's exit status for a SIGTERM stop
(0) and for a restart from the GUI (75)."""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from afterword import service
from test_lab import FakeEngine, _daemon
from test_service import FakeLaunchd, install_plist

H = {"X-AfterWord": "1"}


@pytest.fixture
def launchd(aw_home, tmp_path, monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(service, "launchctl", fake)
    monkeypatch.setattr(service, "pid_alive", fake.alive)
    monkeypatch.setenv(service.AGENTS_DIR_ENV, str(tmp_path / "LaunchAgents"))
    return fake


@pytest.fixture
def gui(aw_home, launchd, monkeypatch):
    from afterword.daemon import web

    monkeypatch.setattr(web, "SERVICE_CACHE_S", 0.0)  # every request asks the fake afresh
    monkeypatch.setattr(web, "RESTART_DELAY_S", 0.0)
    d = _daemon(aw_home, monkeypatch, FakeEngine())
    client = TestClient(web.build_app(d, 47800), base_url="http://127.0.0.1:47800", raise_server_exceptions=False)
    return d, client


def as_service(launchd):
    """launchd's job is this test process, as when launchd started the daemon."""
    install_plist()
    launchd.loaded = True
    launchd.pid = os.getpid()


# --------------------------------------------------------------------------- #
# GET /api/service                                                            #
# --------------------------------------------------------------------------- #


def test_status_under_launchd(gui, launchd, aw_home):
    d, client = gui
    as_service(launchd)
    body = client.get("/api/service").json()
    assert body["mode"] == "service"
    assert body["installed"] is True and body["loaded"] is True and body["running"] is True
    assert body["service_pid"] == os.getpid() == body["pid"]
    assert body["login"] is True and body["other_home"] is False
    assert body["last_exit"] == "(never exited)"
    assert body["summary"].endswith("start at login: on")
    assert body["logs"] == {"daemon": str(aw_home / "afterwordd.log"), "service": str(aw_home / "service.log")}
    assert body["home"] == str(aw_home) and body["version"]
    assert body["restart_exit"] == 75 and body["uptime_s"] >= 0
    st = body["storage"]
    assert st["keep_days"] == 7 and st["history"] == 50 and st["config"] == str(d.store.path)
    assert st["events"] == d.events.count() and st["db_bytes"] > 0


def test_status_started_by_hand(gui, launchd):
    d, client = gui
    install_plist()
    launchd.loaded = True
    launchd.spawn()  # launchd's job is some other process
    body = client.get("/api/service").json()
    assert body["mode"] == "manual" and body["service_pid"] == launchd.pid != body["pid"]


def test_status_not_installed(gui, launchd):
    d, client = gui
    body = client.get("/api/service").json()
    assert body["installed"] is False and body["loaded"] is False and body["mode"] == "manual"
    assert body["service_home"] is None and body["other_home"] is False
    assert body["install_command"] == "afterword install-service"
    assert body["summary"].startswith("not installed")


def test_status_when_launchctl_gives_no_answer(gui, launchd):
    d, client = gui
    launchd.fail["print"] = service.Result(1, "", "launchctl: some error\n")
    launchd.fail["print-disabled"] = service.Result(1, "", "launchctl: some error\n")
    body = client.get("/api/service").json()
    assert body["mode"] == "unknown" and body["loaded"] is None and body["login"] is None
    assert "some error" in body["error"]


def test_status_counts_clips_and_login_off(gui, launchd):
    d, client = gui
    as_service(launchd)
    launchd.disabled = True
    for i in range(3):
        (d.clips_dir / f"{i}.wav").write_bytes(b"x" * 100)
    (d.clips_dir / "note.txt").write_bytes(b"not a clip")
    body = client.get("/api/service").json()
    assert body["login"] is False and body["summary"].endswith("start at login: off")
    assert body["storage"]["clips"] == 3 and body["storage"]["clips_bytes"] == 300


def test_launchctl_answers_are_cached(gui, launchd, monkeypatch):
    from afterword.daemon import web

    d, client = gui
    monkeypatch.setattr(web, "SERVICE_CACHE_S", 60.0)
    as_service(launchd)
    for _ in range(5):
        assert client.get("/api/service").status_code == 200
    assert [c[0] for c in launchd.calls] == ["print", "print-disabled"]  # once, not five times


# --------------------------------------------------------------------------- #
# POST /api/service {"login": ...}                                            #
# --------------------------------------------------------------------------- #


def test_login_toggle(gui, launchd):
    d, client = gui
    as_service(launchd)
    r = client.post("/api/service", json={"login": False}, headers=H)
    assert r.status_code == 200 and r.json()["login"] is False
    assert launchd.mutations() == [("disable", service.target())]
    assert launchd.loaded and launchd.pid == os.getpid()  # nothing stopped or unloaded
    r = client.post("/api/service", json={"login": True}, headers=H)
    assert r.status_code == 200 and r.json()["login"] is True
    assert launchd.mutations()[-1] == ("enable", f"gui/{os.getuid()}/io.github.tonyxtyan.afterword")


def test_login_refused_when_not_installed(gui, launchd):
    d, client = gui
    r = client.post("/api/service", json={"login": False}, headers=H)
    assert r.status_code == 409 and "afterword install-service" in r.json()["error"]
    assert launchd.mutations() == []


def test_login_refused_for_another_home(gui, launchd, monkeypatch, tmp_path):
    """The installed plist runs a different AfterWord home: this daemon
    (started by hand on a test home, say) must not change that service."""
    d, client = gui
    install_plist()
    monkeypatch.setattr(service, "plist_home", lambda path=None: str(tmp_path / "other-home"))
    body = client.get("/api/service").json()
    assert body["other_home"] is True
    r = client.post("/api/service", json={"login": False}, headers=H)
    assert r.status_code == 409 and "other-home" in r.json()["error"]
    assert launchd.mutations() == []


def test_login_bad_body_and_failure(gui, launchd):
    d, client = gui
    as_service(launchd)
    for body in ({}, {"login": "off"}, {"login": 0}, []):
        assert client.post("/api/service", json=body, headers=H).status_code == 400
    launchd.fail["disable"] = service.Result(1, "", "Not privileged to disable service.\n")
    r = client.post("/api/service", json={"login": False}, headers=H)
    assert r.status_code == 502 and "Not privileged" in r.json()["error"]


def test_posts_need_the_header(gui, launchd):
    d, client = gui
    as_service(launchd)
    for path, body in (("/api/service", {"login": False}), ("/api/service/restart", None),
                       ("/api/service/prune", None), ("/api/service/reveal", None)):
        r = client.post(path, json=body)
        assert r.status_code == 403 and "X-AfterWord" in r.json()["error"]
        r = client.post(path, json=body, headers={**H, "Origin": "http://evil.example"})
        assert r.status_code == 403
    assert launchd.mutations() == [] and d.exit_code == 0 and not d._stopping


# --------------------------------------------------------------------------- #
# POST /api/service/restart                                                   #
# --------------------------------------------------------------------------- #


def test_restart_under_launchd(gui, launchd):
    d, client = gui
    as_service(launchd)
    r = client.post("/api/service/restart", headers=H)
    assert r.status_code == 202
    assert r.json() == {"restarting": True, "pid": os.getpid(), "exit_code": 75}
    assert d.exit_code == 75 and d._stopping  # the background task ran once the answer was sent
    assert launchd.mutations() == []  # launchd relaunches it on its own (KeepAlive)
    assert client.post("/api/service/restart", headers=H).status_code == 503  # already stopping


def test_restart_with_the_service_env(gui, launchd, monkeypatch):
    d, client = gui
    monkeypatch.setenv(service.SERVICE_ENV, "1")
    launchd.fail["print"] = service.Result(1, "", "launchctl: some error\n")
    assert client.post("/api/service/restart", headers=H).status_code == 202
    assert d.exit_code == 75


def test_restart_started_by_hand(gui, launchd):
    d, client = gui
    r = client.post("/api/service/restart", headers=H)
    assert r.status_code == 409
    assert r.json()["error"] == "started by hand: restart it in its terminal, or run afterword install-service"
    assert d.exit_code == 0 and not d._stopping


def test_restart_unknown_mode(gui, launchd):
    d, client = gui
    launchd.fail["print"] = service.Result(1, "", "launchctl: some error\n")
    r = client.post("/api/service/restart", headers=H)
    assert r.status_code == 409 and "afterword restart" in r.json()["error"]
    assert not d._stopping


# --------------------------------------------------------------------------- #
# prune, logs, reveal                                                         #
# --------------------------------------------------------------------------- #


def test_prune_now(gui):
    d, client = gui
    old = time.time() - 30 * 86400
    for i in range(3):
        d.events.insert(kind="hook", status="spoken", text=f"old {i}", ts=old)
    d.events.insert(kind="hook", status="spoken", text="new")
    r = client.post("/api/service/prune", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["pruned"] == 3 and body["storage"]["events"] == d.events.count()
    assert client.post("/api/service/prune", headers=H).json()["pruned"] == 0


def test_log_tail(gui, aw_home):
    d, client = gui
    (aw_home / "afterwordd.log").write_text("".join(f"line {i}\n" for i in range(500)))
    body = client.get("/api/logs/daemon", params={"lines": 3}).json()
    assert body["name"] == "daemon" and body["exists"] is True
    assert body["lines"] == ["line 497", "line 498", "line 499"] and body["truncated"] is True
    assert body["path"] == str(aw_home / "afterwordd.log")
    assert len(client.get("/api/logs/daemon").json()["lines"]) == 200  # the default
    assert len(client.get("/api/logs/daemon", params={"lines": 100000}).json()["lines"]) == 500
    assert client.get("/api/logs/daemon", params={"lines": 0}).json()["lines"] == ["line 499"]
    missing = client.get("/api/logs/service").json()
    assert missing == {"name": "service", "path": str(aw_home / "service.log"), "exists": False, "size": 0,
                       "lines": [], "truncated": False}


def test_log_tail_caps(gui, aw_home):
    d, client = gui
    (aw_home / "service.log").write_text("".join(f"{i:06d} " + "x" * 993 + "\n" for i in range(2000)))  # ~2 MB
    body = client.get("/api/logs/service", params={"lines": 1000}).json()
    assert len(body["lines"]) <= 1000 and body["truncated"] is True
    assert body["lines"][-1].startswith("001999 ")
    assert all(len(l) == 1000 for l in body["lines"])  # the partial first line was dropped
    assert sum(len(l) + 1 for l in body["lines"]) <= service.TAIL_MAX_BYTES
    assert body["size"] == (aw_home / "service.log").stat().st_size


def test_log_unknown_names_and_traversal(gui, aw_home):
    d, client = gui
    (aw_home / "config.toml").write_text("secret = 1\n")
    for name in ("config", "afterwordd.log", "..%2Fconfig.toml", "daemon.log", "DAEMON"):
        r = client.get(f"/api/logs/{name}")
        assert r.status_code == 404, name
    assert client.get("/api/logs/../config.toml").status_code == 404


def test_log_symlink_is_not_followed(gui, aw_home, tmp_path):
    d, client = gui
    secret = tmp_path / "elsewhere.txt"
    secret.write_text("not a log\n")
    (aw_home / "service.log").symlink_to(secret)
    r = client.get("/api/logs/service")
    assert r.status_code == 500 and "not a log" not in r.text


def test_reveal_uses_the_fixed_home(gui, aw_home, monkeypatch):
    d, client = gui
    calls = []
    monkeypatch.setattr(service, "run_open", lambda *a: calls.append(a) or service.Result(0))
    r = client.post("/api/service/reveal", json={"path": "/etc"}, headers=H)
    assert r.status_code == 200 and r.json() == {"revealed": str(aw_home)}
    assert calls == [("-R", str(aw_home))]
    monkeypatch.setattr(service, "run_open", lambda *a: service.Result(1, "", "LSOpenURLsWithRole() failed\n"))
    r = client.post("/api/service/reveal", headers=H)
    assert r.status_code == 502 and "LSOpen" in r.json()["error"]


# --------------------------------------------------------------------------- #
# The daemon's exit status                                                    #
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _serve(aw_home, port):
    """`afterword serve` as launchd runs it (AFTERWORD_SERVICE=1), with a
    launchctl that always fails, so it can never reach the real launchd."""
    env = dict(os.environ, AFTERWORD_SYNTH="null", AFTERWORD_PLAYER="null", AFTERWORD_HOME=str(aw_home),
               AFTERWORD_SERVICE="1", AFTERWORD_LAUNCHCTL="/usr/bin/false")
    p = subprocess.Popen([sys.executable, "-m", "afterword.cli", "serve", "--port", str(port)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    end = time.monotonic() + 30
    while time.monotonic() < end:
        assert p.poll() is None, p.communicate()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as r:
                if r.status == 200:
                    return p
        except OSError:
            time.sleep(0.1)
    p.kill()
    pytest.fail("the daemon never answered")


def test_sigterm_exits_0(aw_home):
    p = _serve(aw_home, _free_port())
    try:
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=20)
        assert p.returncode == 0
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()


def test_restart_from_the_gui_exits_75(aw_home):
    port = _free_port()
    p = _serve(aw_home, port)
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/service/restart", method="POST",
                                     headers={"X-AfterWord": "1"})
        with urllib.request.urlopen(req, timeout=5) as r:
            assert r.status == 202
        p.communicate(timeout=20)
        assert p.returncode == 75
        assert "restart asked for in the GUI" in (aw_home / "afterwordd.log").read_text()
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
