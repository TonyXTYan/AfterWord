"""`afterword install-service | start | stop | restart | uninstall-service |
login` and the service line in `status`, against a fake launchd: no test
runs the real launchctl or writes to ~/Library/LaunchAgents."""

import fcntl
import json
import os
import plistlib
import signal
import subprocess
import sys

import pytest

from afterword import cli, service

REAL_LAUNCHCTL = service.launchctl  # before the autouse fixture replaces it

PRINT_SAMPLE = """\
gui/501/io.github.tonyxtyan.afterword = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/io.github.tonyxtyan.afterword.plist
\ttype = LaunchAgent
\tstate = running

\tprogram = /Users/me/AfterWord/.venv/bin/python
\targuments = {
\t\t/Users/me/AfterWord/.venv/bin/python
\t\t-m
\t\tafterword.cli
\t\tserve
\t}

\tworking directory = /Users/me/.afterword

\tstdout path = /Users/me/.afterword/service.log
\tstderr path = /Users/me/.afterword/service.log
\tdefault environment = {
\t\tPATH => /usr/bin:/bin:/usr/sbin:/sbin
\t}

\tenvironment = {
\t\tAFTERWORD_SERVICE => 1
\t\tPATH => /Users/me/AfterWord/.venv/bin:/opt/homebrew/bin:/usr/bin:/bin
\t\tXPC_SERVICE_NAME => io.github.tonyxtyan.afterword
\t}

\tdomain = gui/501 [100021]
\tasid = 100021
\tminimum runtime = 10
\texit timeout = 5
\truns = 1
\tpid = {pid}
\timmediate reason = speculative
\tforks = 0
\texecs = 1
\tinitialized = 1
\ttrampolined = 1
\tstarted suspended = 0
\tproxy started suspended = 0
\tlast exit code = {last_exit}

\tsemaphores = {
\t\tsuccessful exit => 0
\t}

\tspawn type = daemon (3)
\tjetsam priority = 40
\tproperties = keepalive | runatload | inferred program
}
"""

NOT_FOUND = service.Result(113, "", 'Bad request.\nCould not find service "io.github.tonyxtyan.afterword" '
                                    "in domain for user gui: 501\n")


def disabled_text(entries):
    """`launchctl print-disabled gui/<uid>` as macOS prints it."""
    body = "".join(f'\t\t"{label}" => {state}\n' for label, state in entries)
    return f"\tdisabled services = {{\n{body}\t}}\n"


def print_text(pid, last_exit):
    text = PRINT_SAMPLE.replace("{pid}", str(pid)).replace("{last_exit}", last_exit)
    if pid is None:
        text = text.replace("\tstate = running", "\tstate = not running").replace("\tpid = None\n", "")
    return text


class FakeLaunchd:
    """Just enough of launchd: one job, loaded or not, running or not, and
    enabled or disabled (`launchctl disable`). A disabled job cannot be
    bootstrapped or kickstarted here; what real launchd answers is not
    known for sure (5: Input/output error, or 119: Service is disabled), so
    the fake refuses both and the code must not depend on which."""

    def __init__(self):
        self.calls = []
        self.loaded = False
        self.pid = None
        self.last_exit = "(never exited)"
        self.fail = {}  # subcommand -> Result to return instead
        self.on_call = None
        self._next = 4320
        self.linger = 0  # after kill/bootout, the old process outlives its lock for this many checks
        self._dying = 0
        self.bootout_in_progress = 0  # answer 36 (EINPROGRESS) to this many bootouts, then unload
        self.disabled = False
        self.others = [("com.example.other", "disabled"), ("com.example.fine", "enabled")]

    def alive(self, pid):
        """service.pid_alive: only the job's own process is alive."""
        if pid is None or pid != self.pid:
            return False
        if self._dying:
            self._dying -= 1
            if not self._dying:
                self.pid, self.last_exit = None, "0"
            return True
        return True

    def _term(self):
        if self.linger:
            self._dying = self.linger  # still running for a while: print shows it, kickstart ignores it
        else:
            self.pid, self.last_exit = None, "0"

    def spawn(self):
        self._next += 1
        self.pid = self._next

    def mutations(self):
        return [c for c in self.calls if c[0] not in ("print", "print-disabled")]

    def __call__(self, *args):
        self.calls.append(args)
        if self.on_call:
            self.on_call(args)
        cmd = args[0]
        if cmd in self.fail:
            return self.fail[cmd]
        if cmd == "print":
            assert args[1] == service.target()
            if not self.loaded:
                return NOT_FOUND
            return service.Result(0, print_text(self.pid, self.last_exit))
        if cmd == "print-disabled":
            assert args[1:] == (service.domain(),)
            mine = [(service.LABEL, "disabled" if self.disabled else "enabled")] if self.disabled is not None else []
            return service.Result(0, disabled_text(self.others + mine))
        if cmd in ("enable", "disable"):
            assert args[1:] == (service.target(),)
            self.disabled = cmd == "disable"
            return service.Result(0)
        if cmd == "bootstrap":
            assert args[1:] == (service.domain(), str(service.plist_path()))
            if self.disabled:
                return service.Result(5, "", "Bootstrap failed: 5: Input/output error\n")
            if self.loaded:
                return service.Result(5, "", "Bootstrap failed: 5: Input/output error\n")
            self.loaded = True
            self.spawn()
        elif cmd == "bootout":
            assert args[1:] == (service.target(),)
            if not self.loaded:
                return service.Result(3, "", "Boot-out failed: 3: No such process\n")
            if self.bootout_in_progress:
                self.bootout_in_progress -= 1
                return service.Result(36, "", "Boot-out failed: 36: Operation now in progress\n")
            self.loaded, self.pid, self.last_exit = False, None, "0"
        elif cmd == "kickstart":
            if self.disabled:
                return service.Result(119, "", f'Could not kickstart service "{service.LABEL}": 119: Service is disabled\n')
            if "-k" in args or self.pid is None:
                self.spawn()
        elif cmd == "kill":
            assert args[1:] == ("SIGTERM", service.target())
            self._term()
        return service.Result(0)


@pytest.fixture
def launchd(aw_home, tmp_path, monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(service, "launchctl", fake)
    monkeypatch.setattr(service, "pid_alive", fake.alive)
    monkeypatch.setenv(service.AGENTS_DIR_ENV, str(tmp_path / "LaunchAgents"))
    return fake


def install_plist():
    service.plist_path().parent.mkdir(parents=True, exist_ok=True)
    service.plist_path().write_bytes(plistlib.dumps(service.build_plist()))


MANUAL_DAEMON = """\
import fcntl, os, sys, time
fh = open(sys.argv[1], "a+")
fcntl.flock(fh, fcntl.LOCK_EX)
fh.seek(0)
fh.truncate()
fh.write(str(os.getpid()))
fh.flush()
if sys.argv[2] == "ignore":
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture
def manual_daemon(aw_home):
    """A process holding afterwordd.lock with its pid in it, as a daemon
    started by hand with `afterword serve` does."""
    procs = []

    def start(on_sigterm="exit"):
        proc = subprocess.Popen([sys.executable, "-c", MANUAL_DAEMON, str(aw_home / "afterwordd.lock"), on_sigterm],
                                stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "ready"
        procs.append(proc)
        return proc

    yield start
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(5)


# --------------------------------------------------------------------------- #
# The plist                                                                   #
# --------------------------------------------------------------------------- #


def test_plist_content(launchd, aw_home, tmp_path, monkeypatch):
    monkeypatch.setenv("LANG", "en_AU.UTF-8")
    assert cli.main(["install-service", "--dev", "--no-start"]) == 0
    path = tmp_path / "LaunchAgents" / "io.github.tonyxtyan.afterword.plist"
    assert service.plist_path() == path
    assert oct(path.stat().st_mode & 0o777) == "0o644"
    plist = plistlib.loads(path.read_bytes())
    py = os.path.abspath(sys.executable)
    assert plist["Label"] == "io.github.tonyxtyan.afterword"
    assert plist["ProgramArguments"] == [py, "-P", "-m", "afterword.cli", "serve"]
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["ThrottleInterval"] == 10
    assert "ProcessType" not in plist
    assert plist["WorkingDirectory"] == str(aw_home)
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(aw_home / "service.log")
    env = plist["EnvironmentVariables"]
    assert env["AFTERWORD_SERVICE"] == "1"
    assert env["AFTERWORD_HOME"] == str(aw_home)
    assert env["LANG"] == "en_AU.UTF-8"
    dirs = env["PATH"].split(":")
    assert dirs[0] == os.path.dirname(py)
    for d in ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"):
        assert d in dirs
    assert len(dirs) == len(set(dirs))
    assert launchd.mutations() == []  # --no-start loads nothing


def test_plist_without_afterword_home_has_no_override(launchd, monkeypatch):
    monkeypatch.delenv("AFTERWORD_HOME")
    env = service.build_plist("/x/.venv/bin/python")["EnvironmentVariables"]
    assert "AFTERWORD_HOME" not in env
    assert service.build_plist("/x/.venv/bin/python")["WorkingDirectory"].endswith("/.afterword")


def test_python_is_the_venv_link_not_the_base_interpreter(tmp_path, monkeypatch):
    link = tmp_path / "venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(link))
    assert service.python_executable() == str(link)
    assert service.build_plist()["ProgramArguments"][0] == str(link)


def test_python_m_cli_runs():
    """ProgramArguments run `python -P -m afterword.cli`."""
    args = service.build_plist()["ProgramArguments"]
    p = subprocess.run([sys.executable, *args[1:-1], "--version"], capture_output=True, text=True, cwd="/")
    assert p.returncode == 0 and p.stdout.startswith("afterword ")


def test_cwd_python_file_does_not_shadow_stdlib(tmp_path):
    """The LaunchAgent's WorkingDirectory holds user files; -P keeps a stray .py there off sys.path."""
    (tmp_path / "json.py").write_text("raise RuntimeError('shadowed')\n")
    args = service.build_plist()["ProgramArguments"]
    assert args[1] == "-P"
    p = subprocess.run([sys.executable, "-P", "-c", "import json; print('ok')"], capture_output=True, text=True,
                       cwd=str(tmp_path))
    assert p.stdout.strip() == "ok", p.stderr


def test_dry_run_writes_nothing(launchd, aw_home, tmp_path, capsys):
    before = sorted(p.name for p in aw_home.iterdir())
    assert cli.main(["install-service", "--dev", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert not (tmp_path / "LaunchAgents").exists()
    assert sorted(p.name for p in aw_home.iterdir()) == before
    assert launchd.mutations() == []
    assert "<string>io.github.tonyxtyan.afterword</string>" in out
    assert f"launchctl bootstrap gui/{os.getuid()} {service.plist_path()}" in out
    assert "bootout" not in out  # not loaded, so nothing to unload


def test_dry_run_when_loaded_shows_the_reload(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    assert cli.main(["install-service", "--dev", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"launchctl bootout gui/{os.getuid()}/io.github.tonyxtyan.afterword" in out
    assert launchd.mutations() == []


# --------------------------------------------------------------------------- #
# install-service                                                             #
# --------------------------------------------------------------------------- #


def test_install_loads_the_agent(launchd, capsys):
    assert cli.main(["install-service", "--dev"]) == 0
    assert launchd.mutations() == [("bootstrap", service.domain(), str(service.plist_path()))]
    out = capsys.readouterr().out
    assert "service started, pid 4321" in out and "service.log" in out


def test_install_again_rewrites_and_reloads(launchd, capsys):
    assert cli.main(["install-service", "--dev"]) == 0
    service.plist_path().write_text("stale")
    launchd.calls.clear()
    assert cli.main(["install-service", "--dev"]) == 0
    assert [c[0] for c in launchd.mutations()] == ["bootout", "bootstrap"]
    assert plistlib.loads(service.plist_path().read_bytes())["Label"] == service.LABEL
    assert "service reloaded, pid 4322" in capsys.readouterr().out


def test_install_stops_a_manual_daemon_first(launchd, manual_daemon, capsys):
    proc = manual_daemon()
    seen = {}
    launchd.on_call = lambda args: args[0] == "bootstrap" and seen.setdefault("exited", proc.poll())
    assert cli.main(["install-service", "--dev"]) == 0
    assert seen["exited"] == -signal.SIGTERM  # stopped before launchd started its own
    out = capsys.readouterr().out
    assert f"stopping afterwordd pid {proc.pid}, which was started outside launchd" in out
    assert "service started" in out


def test_install_gives_up_if_the_manual_daemon_does_not_stop(launchd, manual_daemon, monkeypatch, capsys):
    monkeypatch.setattr(service, "STOP_WAIT_S", 0.5)
    proc = manual_daemon("ignore")
    assert cli.main(["install-service", "--dev"]) == 1
    assert proc.poll() is None
    assert launchd.mutations() == []
    assert service.plist_path().is_file()
    err = capsys.readouterr().err
    assert "did not stop within" in err and "afterword start" in err


def test_install_reports_bootstrap_failure(launchd, capsys):
    launchd.fail["bootstrap"] = service.Result(5, "", "Bootstrap failed: 5: Input/output error\n")
    assert cli.main(["install-service", "--dev"]) == 1
    assert "Bootstrap failed: 5: Input/output error" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# start, stop, restart, uninstall-service                                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cmd", ["start", "stop", "restart"])
def test_commands_without_plist_point_at_install(launchd, cmd, capsys):
    assert cli.main([cmd]) == 1
    assert "afterword install-service" in capsys.readouterr().err
    assert launchd.mutations() == []


def test_start_bootstraps_when_not_loaded(launchd, capsys):
    install_plist()
    assert cli.main(["start"]) == 0
    assert launchd.mutations() == [("bootstrap", service.domain(), str(service.plist_path()))]
    assert capsys.readouterr().out.strip() == "started, pid 4321"


def test_start_kickstarts_when_loaded(launchd, capsys):
    install_plist()
    launchd.loaded = True
    assert cli.main(["start"]) == 0
    assert launchd.mutations() == [("kickstart", service.target())]
    assert capsys.readouterr().out.strip() == "started, pid 4321"


def test_start_when_running(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    assert cli.main(["start"]) == 0
    assert launchd.mutations() == []
    assert capsys.readouterr().out.strip() == "already running, pid 4321"


def test_start_refuses_while_a_manual_daemon_runs(launchd, monkeypatch, capsys):
    install_plist()
    monkeypatch.setattr(service, "daemon_pid", lambda: (True, 999))
    assert cli.main(["start"]) == 1
    assert "pid 999 is running outside launchd" in capsys.readouterr().err
    assert launchd.mutations() == []


def test_stop_boots_out(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    assert cli.main(["stop"]) == 0
    assert launchd.mutations() == [("bootout", service.target())]
    assert service.plist_path().is_file()
    assert capsys.readouterr().out.strip() == "stopped; it starts again at the next login, or with `afterword start`"


def test_stop_when_not_loaded(launchd, capsys):
    install_plist()
    assert cli.main(["stop"]) == 0
    assert launchd.mutations() == []
    assert "not running" in capsys.readouterr().out


def test_stop_failure_exits_non_zero_with_launchctl_stderr(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.fail["bootout"] = service.Result(5, "", "Boot-out failed: 5: Input/output error\n")
    assert cli.main(["stop"]) == 1
    assert "Boot-out failed: 5: Input/output error" in capsys.readouterr().err


def test_restart_terms_then_kickstarts(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    assert cli.main(["restart"]) == 0
    assert launchd.mutations() == [("kill", "SIGTERM", service.target()), ("kickstart", "-k", service.target())]
    assert capsys.readouterr().out.strip() == "restarted, pid 4322"


def test_restart_waits_for_the_old_process_not_just_its_lock(launchd, capsys):
    """The daemon releases its lock tens of ms before its process exits; a
    kickstart in that window would hit a job that is still running."""
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    old = launchd.pid
    launchd.linger = 3
    assert cli.main(["restart"]) == 0
    assert launchd.pid not in (None, old)
    assert capsys.readouterr().out.strip() == f"restarted, pid {launchd.pid}"


def test_stop_treats_bootout_in_progress_as_stopping(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    launchd.bootout_in_progress = 1
    assert cli.main(["stop"]) == 0
    assert [c[0] for c in launchd.mutations()] == ["bootout", "bootout"]
    assert not launchd.loaded
    assert capsys.readouterr().out.startswith("stopped;")


def test_stop_fails_if_the_job_stays_loaded(launchd, monkeypatch, capsys):
    monkeypatch.setattr(service, "STOP_WAIT_S", 0.3)
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    launchd.bootout_in_progress = 10**6
    assert cli.main(["stop"]) == 1
    assert "still loaded" in capsys.readouterr().err


def test_plist_passes_model_and_claude_locations(launchd, monkeypatch):
    monkeypatch.setenv("HF_HOME", "/Volumes/models/hf")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/Users/x/.claude-alt")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-copied")
    env = service.build_plist("/venv/bin/python")["EnvironmentVariables"]
    assert env["HF_HOME"] == "/Volumes/models/hf" and env["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude-alt"
    assert "ANTHROPIC_API_KEY" not in env


def test_restart_falls_back_to_kickstart_k(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    launchd.fail["kill"] = service.Result(3, "", "Could not kill\n")
    assert cli.main(["restart"]) == 0
    assert launchd.mutations()[-1] == ("kickstart", "-k", service.target())


def test_restart_when_not_loaded_starts(launchd, capsys):
    install_plist()
    assert cli.main(["restart"]) == 0
    assert [c[0] for c in launchd.mutations()] == ["bootstrap"]
    assert capsys.readouterr().out.strip() == "started, pid 4321"


def test_uninstall_boots_out_and_deletes(launchd, capsys):
    assert cli.main(["install-service", "--dev"]) == 0
    launchd.calls.clear()
    assert cli.main(["uninstall-service"]) == 0
    assert launchd.mutations() == [("bootout", service.target())]
    assert not service.plist_path().exists()
    assert "service removed: stopped it and deleted" in capsys.readouterr().out


def test_uninstall_and_install_refuse_when_launchctl_gives_no_answer(launchd, capsys):
    """If launchctl print fails, the agent may still be loaded: deleting or
    rewriting its plist would leave a loaded job beside a missing or
    different plist."""
    assert cli.main(["install-service", "--dev"]) == 0
    before = service.plist_path().read_bytes()
    launchd.calls.clear()
    launchd.fail["print"] = service.Result(1, "", "launchctl print: some transient error\n")
    assert cli.main(["uninstall-service"]) == 1
    assert service.plist_path().read_bytes() == before
    assert cli.main(["install-service", "--dev"]) == 1
    assert service.plist_path().read_bytes() == before
    assert launchd.mutations() == []
    err = capsys.readouterr().err
    assert err.count("cannot tell whether the service is loaded") == 2 and "transient error" in err


def test_uninstall_when_not_installed(launchd, capsys):
    assert cli.main(["uninstall-service"]) == 0
    assert launchd.mutations() == []
    assert capsys.readouterr().out.strip() == "the service is not installed"


# --------------------------------------------------------------------------- #
# status                                                                      #
# --------------------------------------------------------------------------- #


def test_parse_print_sample():
    parsed = service.parse_print(print_text(123, "0"))
    assert parsed == {"state": "running", "pid": 123, "last_exit": "0"}
    parsed = service.parse_print(print_text(None, "78: EX_CONFIG"))
    assert parsed == {"state": "not running", "pid": None, "last_exit": "78: EX_CONFIG"}


@pytest.mark.parametrize("text", ["", "garbage\n\x00\xff", "state: running\npid: 12", "{\n\t\tpid = 5\n}"])
def test_parse_print_garbage(text):
    assert service.parse_print(text) == {"state": None, "pid": None, "last_exit": None}


def test_status_line_for_garbage_print(launchd, monkeypatch):
    install_plist()
    monkeypatch.setattr(service, "launchctl", lambda *a: service.Result(0, "nonsense"))
    info = service.service_info()
    assert info.loaded is True and info.state == "unknown" and info.pid is None
    assert info.summary() == "loaded, state unknown"


def test_status_line_when_launchctl_fails(launchd, monkeypatch):
    monkeypatch.setattr(service, "launchctl", lambda *a: service.Result(127, "", "launchctl not found"))
    info = service.service_info()
    assert info.loaded is None
    assert info.summary() == "not installed, state unknown (launchctl not found)"


def test_status_when_down_shows_the_service(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.last_exit = "78: EX_CONFIG"
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "not running" in out
    assert "  service   loaded, not running, last exit 78: EX_CONFIG" in out


def test_status_json_when_down_has_service(launchd, capsys):
    assert cli.main(["status", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert "not running" in data["error"]
    assert data["service"]["installed"] is False and data["service"]["loaded"] is False


def test_status_when_up_shows_the_service(launchd, monkeypatch, capsys):
    install_plist()
    launchd.loaded = True
    launchd.pid, launchd.last_exit = 123, "0"
    monkeypatch.setattr(cli, "_request", lambda msg, timeout=2.0: {"status": {"version": "x", "pid": 123}})
    assert cli.main(["status"]) == 0
    assert "  service   loaded, running pid 123, last exit 0, start at login: on\n" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pid"] == 123
    assert data["service"] == {
        "label": service.LABEL, "installed": True, "plist": str(service.plist_path()), "loaded": True,
        "state": "running", "pid": 123, "last_exit": "0", "error": None, "login": True,
    }


# --------------------------------------------------------------------------- #
# The daemon under launchd                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("as_service, code", [(True, 0), (False, 1)])
def test_already_running_exit_code(aw_home, as_service, code):
    """Under launchd (AFTERWORD_SERVICE=1) a daemon that finds another one
    holding the lock exits 0, so KeepAlive does not retry it every 10 s;
    `afterword serve` by hand still exits 1."""
    with open(aw_home / "afterwordd.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = dict(os.environ, AFTERWORD_SYNTH="null", AFTERWORD_PLAYER="null")
        if as_service:
            env["AFTERWORD_SERVICE"] = "1"
        p = subprocess.run([sys.executable, "-m", "afterword.cli", "serve", "--no-http"], env=env,
                           capture_output=True, text=True, timeout=30)
    assert p.returncode == code
    assert "another afterwordd is running" in p.stderr
    assert ("started by launchd" in p.stderr) is as_service


@pytest.mark.parametrize("as_service", [True, False])
def test_ui_port_in_use(aw_home, as_service):
    """Under launchd a taken UI port must not make the daemon exit (KeepAlive
    would relaunch it every 10 s): it keeps speaking over the socket. By
    hand, `afterword serve` still fails with exit 1."""
    import socket as _socket
    import time as _time

    busy = _socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    env = dict(os.environ, AFTERWORD_SYNTH="null", AFTERWORD_PLAYER="null")
    env.pop("AFTERWORD_SERVICE", None)
    if as_service:
        env["AFTERWORD_SERVICE"] = "1"
    p = subprocess.Popen([sys.executable, "-m", "afterword.cli", "serve", "--port", str(port)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if not as_service:
            _, err = p.communicate(timeout=30)
            assert p.returncode == 1 and "could not start" in err
            return
        log = aw_home / "afterwordd.log"
        end = _time.monotonic() + 20
        while _time.monotonic() < end:
            assert p.poll() is None, p.communicate()
            if log.exists() and "speaking without the UI" in log.read_text(errors="replace"):
                break
            _time.sleep(0.1)
        else:
            pytest.fail("the daemon never reported running without the UI")
        assert p.poll() is None
        assert service._ping_pid() == p.pid
        p.terminate()
        p.communicate(timeout=20)
        assert p.returncode == 0
    finally:
        busy.close()
        if p.poll() is None:
            p.kill()
            p.communicate()


def test_trim_log_keeps_the_tail_in_place(tmp_path):
    path = tmp_path / "service.log"
    line = b"x" * 99 + b"\n"
    path.write_bytes(b"".join(b"%07d " % i + line for i in range(60000)))  # ~6.4 MB
    appender = os.open(path, os.O_WRONLY | os.O_APPEND)  # as launchd holds it
    try:
        inode = path.stat().st_ino
        assert service.trim_log(path) is True
        data = path.read_bytes()
        assert len(data) <= service.LOG_KEEP
        assert data.startswith(b"00") and data.endswith(b"%07d " % 59999 + line)
        assert path.stat().st_ino == inode  # not renamed or replaced
        os.write(appender, b"after trim\n")
        assert path.read_bytes() == data + b"after trim\n"
    finally:
        os.close(appender)


def test_trim_log_leaves_small_or_missing_files(tmp_path):
    path = tmp_path / "service.log"
    assert service.trim_log(path) is False
    path.write_bytes(b"short\n" * 10)
    assert service.trim_log(path) is False
    assert path.read_bytes() == b"short\n" * 10
    assert service.trim_log(path, limit=10, keep=12) is True
    assert path.read_bytes() == b"short\nshort\n"  # the cut fell on a line boundary: nothing dropped
    path.write_bytes(b"short\n" * 10)
    assert service.trim_log(path, limit=10, keep=9) is True
    assert path.read_bytes() == b"short\n"  # the partial line before it is dropped


# --------------------------------------------------------------------------- #
# start at login (launchctl enable / disable)                                 #
# --------------------------------------------------------------------------- #


def test_parse_disabled():
    text = disabled_text([("com.example.a", "enabled"), (service.LABEL, "disabled")])
    assert service.parse_disabled(text) is True
    assert service.parse_disabled(text, "com.example.a") is False
    assert service.parse_disabled(text, "com.example.missing") is False  # not listed: enabled
    assert service.parse_disabled(disabled_text([(service.LABEL, "true")])) is True  # older macOS
    assert service.parse_disabled(disabled_text([(service.LABEL, "false")])) is False
    # a longer label that merely starts with ours is another service
    assert service.parse_disabled(disabled_text([(service.LABEL + ".helper", "disabled")])) is False
    assert service.parse_disabled("") is False


def test_login_state(launchd, monkeypatch):
    assert service.login_state() == (True, None)
    launchd.disabled = True
    assert service.login_state() == (False, None)
    launchd.disabled = None  # never enabled or disabled: not in the list
    assert service.login_state() == (True, None)
    launchd.fail["print-disabled"] = service.Result(1, "", "Usage: launchctl print-disabled <domain>\n")
    assert service.login_state() == (None, "Usage: launchctl print-disabled <domain>")


def test_login_prints_the_state(launchd, capsys):
    install_plist()
    assert cli.main(["login"]) == 0
    assert capsys.readouterr().out.strip() == "start at login: on"
    launchd.disabled = True
    assert cli.main(["login"]) == 0
    assert capsys.readouterr().out.strip() == "start at login: off"
    assert launchd.mutations() == []


def test_login_without_plist(launchd, capsys):
    assert cli.main(["login"]) == 0
    assert "not installed" in capsys.readouterr().out
    for state in ("on", "off"):
        assert cli.main(["login", state]) == 1
        assert "afterword install-service" in capsys.readouterr().err
    assert launchd.mutations() == []


def test_login_off_and_on(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    pid = launchd.pid
    assert cli.main(["login", "off"]) == 0
    assert launchd.mutations() == [("disable", service.target())]
    assert launchd.disabled is True
    assert launchd.loaded and launchd.pid == pid  # disable leaves the running daemon alone
    out = capsys.readouterr().out
    assert out.startswith("start at login: off;") and "keeps running" in out
    assert cli.main(["login", "on"]) == 0
    assert launchd.mutations()[-1] == ("enable", service.target())
    assert launchd.disabled is False
    assert capsys.readouterr().out.strip() == "start at login: on; launchd loads the service at the next login"


def test_login_on_when_not_loaded_points_at_start(launchd, capsys):
    install_plist()
    launchd.disabled = True
    assert cli.main(["login", "on"]) == 0
    assert "`afterword start` starts it now" in capsys.readouterr().out


def test_login_failure(launchd, capsys):
    install_plist()
    launchd.fail["disable"] = service.Result(1, "", "Not privileged to disable service.\n")
    assert cli.main(["login", "off"]) == 1
    assert "Not privileged" in capsys.readouterr().err


def test_status_shows_login_off(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.disabled = True
    assert cli.main(["status"]) == 1  # no daemon answering in the test home
    assert "  service   loaded, not running, last exit (never exited), start at login: off" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["service"]["login"] is False


def test_status_without_plist_has_no_login_part(launchd, capsys):
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "  service   not installed" in out and "start at login" not in out


def _enable_bootstrap_disable():
    return [("enable", service.target()), ("bootstrap", service.domain(), str(service.plist_path())),
            ("disable", service.target())]


def test_start_on_a_disabled_service_keeps_login_off(launchd, capsys):
    install_plist()
    launchd.disabled = True
    assert cli.main(["start"]) == 0
    assert launchd.mutations() == _enable_bootstrap_disable()
    assert launchd.loaded and launchd.pid and launchd.disabled is True
    assert capsys.readouterr().out.splitlines() == [
        "start at login is off (`afterword login on` turns it on)", "started, pid 4321"]


def test_start_kickstart_on_a_disabled_loaded_service(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.disabled = True
    assert cli.main(["start"]) == 0
    assert launchd.mutations() == [("enable", service.target()), ("kickstart", service.target()),
                                   ("disable", service.target())]
    assert launchd.pid and launchd.disabled is True


def test_install_on_a_disabled_service_keeps_login_off(launchd, capsys):
    launchd.disabled = True
    assert cli.main(["install-service", "--dev"]) == 0
    assert launchd.mutations() == _enable_bootstrap_disable()
    assert launchd.disabled is True
    out = capsys.readouterr().out
    assert "start at login is off (`afterword login on` turns it on)" in out
    assert "service started, pid 4321: start at login is off, and it restarts after a crash" in out


def test_install_dry_run_on_a_disabled_service_shows_the_enable(launchd, capsys):
    launchd.disabled = True
    assert cli.main(["install-service", "--dev", "--dry-run"]) == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("# would run:")]
    assert [l.split()[4] for l in lines] == ["enable", "bootstrap", "disable"]
    assert launchd.mutations() == [] and launchd.disabled is True


def test_restart_on_a_disabled_service(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    launchd.disabled = True
    assert cli.main(["restart"]) == 0
    # Enabled before anything is stopped, so a failed enable stops nothing.
    assert launchd.mutations() == [("enable", service.target()), ("kill", "SIGTERM", service.target()),
                                   ("kickstart", "-k", service.target()), ("disable", service.target())]
    assert launchd.disabled is True
    assert capsys.readouterr().out.strip().endswith("restarted, pid 4322")


def test_restart_with_login_off_stops_nothing_if_enable_fails(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    pid = launchd.pid
    launchd.disabled = True
    launchd.fail["enable"] = service.Result(1, "", "Not privileged to enable service\n")
    assert cli.main(["restart"]) == 1
    assert launchd.mutations() == [("enable", service.target())]
    assert launchd.pid == pid  # still running
    assert "Not privileged" in capsys.readouterr().err


def test_login_window_holds_hangup_until_login_is_off_again(launchd):
    """A closed terminal mid-start must not leave start at login on: SIGHUP
    (and Ctrl-C, SIGTERM) wait until the disable has run."""
    import signal as _signal

    install_plist()
    launchd.disabled = True
    seen = {}

    def during(args):
        if args[0] == "bootstrap":
            seen["blocked"] = set(_signal.pthread_sigmask(_signal.SIG_BLOCK, []))

    launchd.on_call = during
    before = set(_signal.pthread_sigmask(_signal.SIG_BLOCK, []))
    assert cli.main(["start"]) == 0
    assert {_signal.SIGHUP, _signal.SIGINT, _signal.SIGTERM} <= seen["blocked"]
    assert set(_signal.pthread_sigmask(_signal.SIG_BLOCK, [])) == before  # restored after
    assert launchd.disabled is True


def test_restart_falls_back_to_start_on_a_disabled_service(launchd, capsys):
    install_plist()
    launchd.disabled = True
    assert cli.main(["restart"]) == 0
    assert launchd.mutations() == _enable_bootstrap_disable()
    assert launchd.disabled is True


def test_bootstrap_failure_still_disables_again(launchd, capsys):
    install_plist()
    launchd.disabled = True
    launchd.fail["bootstrap"] = service.Result(5, "", "Bootstrap failed: 5: Input/output error\n")
    assert cli.main(["start"]) == 1
    assert launchd.mutations()[-1] == ("disable", service.target())
    assert launchd.disabled is True
    assert "Bootstrap failed: 5" in capsys.readouterr().err


def test_enable_failure_stops_before_bootstrap(launchd, capsys):
    install_plist()
    launchd.disabled = True
    launchd.fail["enable"] = service.Result(1, "", "Not privileged to enable service.\n")
    assert cli.main(["start"]) == 1
    assert [c[0] for c in launchd.mutations()] == ["enable"]
    err = capsys.readouterr().err
    assert "launchctl enable" in err and "Not privileged" in err


def test_disable_failure_after_start_warns(launchd, capsys):
    install_plist()
    launchd.disabled = True
    launchd.fail["disable"] = service.Result(1, "", "nope\n")
    assert cli.main(["start"]) == 0
    captured = capsys.readouterr()
    assert "start at login is on now" in captured.err
    assert "start at login is off" not in captured.out


def test_disabled_refusal_without_an_answer_from_print_disabled(launchd, capsys):
    """When print-disabled gives no answer the bootstrap is tried as is; a
    "Service is disabled" refusal points at `afterword login on`."""
    install_plist()
    launchd.fail["print-disabled"] = service.Result(1, "", "launchctl: some error\n")
    launchd.fail["bootstrap"] = service.Result(119, "", "Bootstrap failed: 119: Service is disabled\n")
    assert cli.main(["start"]) == 1
    assert "`afterword login on` enables it" in capsys.readouterr().err
    assert [c[0] for c in launchd.mutations()] == ["bootstrap"]


def test_parse_print_last_terminating_signal():
    text = print_text(123, "0").replace("\tlast exit code = 0\n", "\tlast terminating signal = Killed: 9\n")
    assert service.parse_print(text)["last_exit"] == "signal Killed: 9"


def test_daemon_mode(launchd, monkeypatch):
    info = service.ServiceInfo(installed=True, plist="x", loaded=True, state="running", pid=os.getpid())
    assert service.daemon_mode(info) == "service"
    info.pid = os.getpid() + 1
    assert service.daemon_mode(info) == "manual"
    info.loaded, info.pid = None, None
    assert service.daemon_mode(info) == "unknown"
    monkeypatch.setenv(service.SERVICE_ENV, "1")
    assert service.daemon_mode(info) == "service"


def test_plist_home(launchd, aw_home, tmp_path, monkeypatch):
    assert service.plist_home() is None
    install_plist()
    assert service.same_home(service.plist_home(), aw_home)
    monkeypatch.delenv("AFTERWORD_HOME")
    install_plist()  # no AFTERWORD_HOME: the WorkingDirectory says it
    assert service.plist_home().endswith("/.afterword")
    service.plist_path().write_text("not a plist")
    assert service.plist_home() is None


def test_launchctl_binary_override(monkeypatch, tmp_path):
    """AFTERWORD_LAUNCHCTL swaps the binary: a missing one is an answer, not a crash."""
    monkeypatch.setattr(service, "launchctl", REAL_LAUNCHCTL)  # never the real binary: see the env below
    monkeypatch.setenv(service.LAUNCHCTL_ENV, str(tmp_path / "no-such-launchctl"))
    r = service.launchctl("print", service.target())
    assert r.returncode == 127 and "no-such-launchctl not found" in r.error()
    fake = tmp_path / "launchctl"
    fake.write_text("#!/bin/sh\necho \"fake $*\"\n")
    fake.chmod(0o755)
    monkeypatch.setenv(service.LAUNCHCTL_ENV, str(fake))
    assert service.launchctl("print-disabled", "gui/1").stdout == "fake print-disabled gui/1\n"


def test_stop_with_login_off_says_it_stays_stopped(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    launchd.disabled = True
    assert cli.main(["stop"]) == 0
    assert launchd.mutations() == [("bootout", service.target())]
    assert capsys.readouterr().out.strip() == "stopped; start at login is off, so it stays stopped until `afterword start`"


def test_uninstall_clears_login_off(launchd, capsys):
    install_plist()
    launchd.loaded = True
    launchd.spawn()
    launchd.disabled = True
    assert cli.main(["uninstall-service"]) == 0
    assert launchd.mutations() == [("bootout", service.target()), ("enable", service.target())]
    assert launchd.disabled is False and not service.plist_path().exists()
    assert "reset to on" in capsys.readouterr().out
