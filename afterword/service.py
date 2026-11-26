"""The daemon as a per-user LaunchAgent: `afterword install-service`,
`start`, `stop`, `restart`, `uninstall-service`, `login`, the service line in
`afterword status`, and what the GUI's Daemon section asks about launchd.

Every launchctl call goes through `launchctl()`, which tests replace; the
binary can be swapped with AFTERWORD_LAUNCHCTL (a fake for a throwaway
daemon) and the LaunchAgents directory moved with AFTERWORD_LAUNCH_AGENTS_DIR,
so tests never touch the real launchd or ~/Library/LaunchAgents.

Semantics:
- install-service writes the plist and loads it (bootstrap). The agent
  starts at login (RunAtLoad) and is restarted after a crash, but not after
  a clean exit (KeepAlive SuccessfulExit = false).
- stop unloads it (bootout). A plain kill would be restarted by KeepAlive
  if the daemon did not exit 0 (SIGKILL, a hung shutdown); an unloaded job
  is never restarted. The plist stays, so it comes back at the next login
  or with `afterword start`.
- start loads it if it is not loaded, else kickstarts it.
- restart sends SIGTERM through launchd and waits for the clean exit (the
  daemon's own shutdown runs), then kickstarts it; `kickstart -k` only if
  it does not exit in time.
- uninstall-service unloads it and deletes the plist, and clears a
  `login off` (enables it again), so a later install starts at login.
- login on|off is `launchctl enable|disable`: launchd's own per-user
  override, kept across reboots. Off does not stop or unload a running
  daemon; launchd just does not load the agent at the next login. A
  disabled agent cannot be bootstrapped, so start, install-service and
  restart enable it for their bootstrap or kickstart and disable it again
  right after: the login setting is kept.
- The GUI's Restart makes the daemon exit RESTART_EXIT (non-zero), which
  KeepAlive (SuccessfulExit = false) answers by launching it again.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from afterword.hooks import common

LABEL = "io.github.tonyxtyan.afterword"
SERVICE_ENV = "AFTERWORD_SERVICE"  # set to "1" in the plist: the daemon knows launchd started it
AGENTS_DIR_ENV = "AFTERWORD_LAUNCH_AGENTS_DIR"  # tests point this at a temp dir
LAUNCHCTL_ENV = "AFTERWORD_LAUNCHCTL"  # the launchctl binary; tests and throwaway daemons point it at a fake
LOG_NAME = "service.log"
LOCK_NAME = "afterwordd.lock"  # taken by server._acquire_lock, which writes its pid into it
LOG_TRIM_OVER = 5 * 1024 * 1024
LOG_KEEP = 1024 * 1024
STOP_WAIT_S = 15.0
THROTTLE_S = 10
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
NOT_FOUND_RC = 113  # `launchctl print` of a service that is not loaded
IN_PROGRESS_RC = 36  # `launchctl bootout` while the job is still exiting (EINPROGRESS)
DISABLED_RC = 119  # launchd's "Service is disabled"
# The daemon's exit status for a restart asked for in the GUI: EX_TEMPFAIL.
# Any non-zero status makes launchd launch it again (KeepAlive
# SuccessfulExit = false), no sooner than ThrottleInterval after the last launch.
RESTART_EXIT = 75
# Passed from the installing shell into the plist when set: where the models
# were downloaded (Hugging Face honours these) and which Claude config
# `claude -p` uses for alert phrases. Never secrets: the plist is 0644.
PASS_ENV = ("HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "XDG_CACHE_HOME", "HF_HUB_OFFLINE", "CLAUDE_CONFIG_DIR")


# --------------------------------------------------------------------------- #
# launchctl                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def error(self) -> str:
        text = (self.stderr or self.stdout).strip()
        return text or f"exit status {self.returncode}"


def launchctl(*args: str) -> Result:
    """Run `launchctl <args>`. The only place that does; tests replace it."""
    exe = os.environ.get(LAUNCHCTL_ENV) or "launchctl"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return Result(127, "", f"{exe} not found (AfterWord's service needs macOS)")
    except OSError as exc:
        return Result(126, "", f"cannot run {exe}: {exc}")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"launchctl {' '.join(args)} timed out")
    return Result(p.returncode, p.stdout, p.stderr)


def domain() -> str:
    return f"gui/{os.getuid()}"


def target() -> str:
    return f"{domain()}/{LABEL}"


def agents_dir() -> Path:
    override = os.environ.get(AGENTS_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def plist_path() -> Path:
    return agents_dir() / f"{LABEL}.plist"


def log_path() -> Path:
    return common.home() / LOG_NAME


def _cmd(*args: str) -> str:
    return "launchctl " + " ".join(args)


# --------------------------------------------------------------------------- #
# The plist                                                                   #
# --------------------------------------------------------------------------- #


def python_executable() -> str:
    """This interpreter as the venv knows it: sys.executable made absolute but
    not resolved. A venv's bin/python is a symlink to the base interpreter,
    and running that directly would lose the venv's packages."""
    return os.path.abspath(sys.executable)


def service_path(python: str) -> str:
    """PATH for the agent: launchd's default is only /usr/bin:/bin:/usr/sbin:/sbin.
    The venv's bin first, then ~/.local/bin and the directory of `claude`
    (alert generation runs `claude -p`), then Homebrew and the system."""
    dirs = [str(Path(python).parent), str(Path.home() / ".local" / "bin")]
    claude = shutil.which("claude")
    if claude:
        dirs.append(str(Path(claude).parent))
    dirs.extend(SYSTEM_PATH)
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return ":".join(seen)


def build_plist(python: str | None = None) -> dict[str, Any]:
    py = python or python_executable()
    home = common.home().absolute()
    env = {"PATH": service_path(py), SERVICE_ENV: "1"}
    if os.environ.get("AFTERWORD_HOME"):
        env["AFTERWORD_HOME"] = str(home)
    if os.environ.get("LANG"):
        env["LANG"] = os.environ["LANG"]  # `say` and `claude` get the same locale as from a shell
    for key in PASS_ENV:
        if os.environ.get(key):
            env[key] = os.environ[key]
    log = str(home / LOG_NAME)
    return {
        "Label": LABEL,
        "ProgramArguments": [py, "-m", "afterword.cli", "serve"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_S,
        # No ProcessType: "Background" throttles CPU and I/O and coalesces
        # timers, which would delay speech; unset means launchd's Standard.
        "EnvironmentVariables": env,
        "WorkingDirectory": str(home),
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def _write_plist(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o644)  # launchd refuses a group- or world-writable plist
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# What is running                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class ServiceInfo:
    installed: bool  # the plist exists
    plist: str
    loaded: bool | None  # None: launchctl gave no usable answer
    state: str | None = None  # "running", "not running", ... as launchctl prints it
    pid: int | None = None
    last_exit: str | None = None  # "0", "(never exited)", "78: EX_CONFIG", ...
    error: str | None = None
    login: bool | None = None  # starts at login (not disabled); None: not asked, or no answer

    @property
    def running(self) -> bool:
        return bool(self.loaded and self.state == "running" and self.pid)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": LABEL, "installed": self.installed, "plist": self.plist, "loaded": self.loaded,
            "state": self.state, "pid": self.pid, "last_exit": self.last_exit, "error": self.error,
            "login": self.login,
        }

    def summary(self) -> str:
        """One line for `afterword status`."""
        line = self._summary()
        if self.installed and self.login is not None:
            line += f", start at login: {'on' if self.login else 'off'}"
        return line

    def _summary(self) -> str:
        if self.loaded is None:
            head = "installed" if self.installed else "not installed"
            return f"{head}, state unknown ({self.error or 'launchctl gave no answer'})"
        if not self.loaded:
            if not self.installed:
                return "not installed (`afterword install-service` runs the daemon at login)"
            return "installed, not loaded (`afterword start`)"
        parts = ["loaded"] if self.installed else [f"loaded, but {self.plist} is missing"]
        if self.state == "running" and self.pid:
            parts.append(f"running pid {self.pid}")
        else:
            parts.append(self.state if self.state not in (None, "unknown") else "state unknown")
        if self.last_exit:
            parts.append(f"last exit {self.last_exit}")
        return ", ".join(parts)


_PRINT_LINE = re.compile(r"^\t([a-z][a-z ]*?) = (.*)$")


def parse_print(text: str) -> dict[str, Any]:
    """state, pid and last exit code from `launchctl print <service>`. Its
    format is not an API: only the service's own top-level `key = value`
    lines (one tab in) are read, and anything missing is None."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        m = _PRINT_LINE.match(line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    pid = None
    with contextlib.suppress(ValueError, TypeError):
        pid = int(fields.get("pid", ""))
    last_exit = fields.get("last exit code") or None
    if last_exit is None and fields.get("last terminating signal"):
        last_exit = f"signal {fields['last terminating signal']}"  # e.g. "signal Killed: 9"
    return {"state": fields.get("state") or None, "pid": pid if pid and pid > 0 else None,
            "last_exit": last_exit}


def service_info(login: bool = False) -> ServiceInfo:
    """What launchd says about the agent. `login` also asks whether it starts
    at login (one more launchctl call)."""
    path = plist_path()
    info = ServiceInfo(installed=path.is_file(), plist=str(path), loaded=None)
    r = launchctl("print", target())
    if r.ok:
        info.loaded = True
        parsed = parse_print(r.stdout)
        info.state, info.pid, info.last_exit = parsed["state"], parsed["pid"], parsed["last_exit"]
        if info.state is None:
            info.state = "unknown"
    elif r.returncode == NOT_FOUND_RC or "could not find service" in r.error().lower():
        info.loaded = False
    else:
        info.error = r.error().splitlines()[-1]
    if login:
        info.login, _ = login_state()
    return info


_DISABLED_LINE = re.compile(r'^\s*"([^"]+)"\s*=>\s*(\S+)\s*$')


def parse_disabled(text: str, label: str = LABEL) -> bool:
    """Whether `label` is disabled in `launchctl print-disabled <domain>`,
    whose lines read `"<label>" => disabled` (or `=> true` on older macOS).
    A label missing from the list is enabled."""
    for line in text.splitlines():
        m = _DISABLED_LINE.match(line)
        if m and m.group(1) == label:
            return m.group(2).lower() in ("disabled", "true")
    return False


def login_state() -> tuple[bool | None, str | None]:
    """(starts at login, error): True unless launchd has the agent disabled;
    None with the error when launchctl gives no answer."""
    r = launchctl("print-disabled", domain())
    if not r.ok:
        return None, r.error().splitlines()[-1]
    return not parse_disabled(r.stdout), None


def set_login(on: bool) -> Result:
    """`launchctl enable|disable` the agent. Persistent across reboots;
    disable does not stop or unload a running daemon."""
    return launchctl("enable" if on else "disable", target())


def plist_home(path: Path | None = None) -> str | None:
    """The AfterWord home the installed plist runs with (its AFTERWORD_HOME,
    else its WorkingDirectory), or None when there is no readable plist."""
    try:
        data = plistlib.loads((path or plist_path()).read_bytes())
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    if not isinstance(data, dict):
        return None
    env = data.get("EnvironmentVariables")
    home = env.get("AFTERWORD_HOME") if isinstance(env, dict) else None
    home = home or data.get("WorkingDirectory")
    return home if isinstance(home, str) and home else None


def same_home(a: str | Path | None, b: str | Path | None) -> bool:
    if not a or not b:
        return False
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


def daemon_mode(info: ServiceInfo, pid: int | None = None) -> str:
    """How the daemon with `pid` (default: this process) was started:
    "service" when it is launchd's process for the agent (or launchd's plist
    set AFTERWORD_SERVICE=1 in its environment), "manual" when it is not,
    "unknown" when launchctl gave no answer."""
    pid = os.getpid() if pid is None else pid
    if info.loaded and info.pid == pid:
        return "service"
    if os.environ.get(SERVICE_ENV) == "1":
        return "service"
    if info.loaded is None:
        return "unknown"
    return "manual"


def _lock_holder() -> tuple[bool, int | None]:
    """(held, pid) for the daemon's lock file. Taking the lock for a moment
    is the only way to test it; it is released at once."""
    try:
        fd = os.open(str(common.home() / LOCK_NAME), os.O_RDONLY)
    except OSError:
        return False, None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False, None
        try:
            return True, int(os.read(fd, 32).strip())
        except ValueError:
            return True, None
    finally:
        os.close(fd)


def _ping_pid(timeout: float = 1.0) -> int | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(common.socket_path()))
        s.sendall(b'{"type": "ping"}\n')
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        pid = json.loads(buf.split(b"\n", 1)[0] or b"{}").get("status", {}).get("pid")
        return pid if isinstance(pid, int) and pid > 0 else None
    except (OSError, ValueError, AttributeError):
        return None
    finally:
        s.close()


def daemon_pid() -> tuple[bool, int | None]:
    """(running, pid) of any afterwordd on this AFTERWORD_HOME, launchd's or
    not: the socket's answer, else the lock file."""
    pid = _ping_pid()
    if pid:
        return True, pid
    return _lock_holder()


def pid_alive(pid: int) -> bool:
    """Whether process `pid` still exists (tests replace this)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_stopped(pid: int | None, timeout: float | None = None) -> bool:
    """Wait until no daemon (or a different one) holds the socket and lock,
    and process `pid` has exited. The daemon lets go of the lock tens of ms
    before its process ends (asyncio teardown, interpreter exit), and
    launchd still counts the job as running until then."""
    end = time.monotonic() + (STOP_WAIT_S if timeout is None else timeout)
    while True:
        running, now = daemon_pid()
        released = not running or (pid is not None and now is not None and now != pid)
        if released and (pid is None or not pid_alive(pid)):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)


def _stop_manual(pid: int | None, out) -> bool:
    """SIGTERM to a daemon started outside launchd (run_daemon's handler runs
    its normal shutdown), then wait for it to let go of the lock."""
    if not pid or pid == os.getpid():
        print(f"afterwordd's lock is held but holds no usable pid ({pid}); stop the daemon yourself, "
              "then run this again", file=sys.stderr)
        return False
    print(f"stopping afterwordd pid {pid}, which was started outside launchd", file=out)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError as exc:
        print(f"cannot signal pid {pid}: {exc}", file=sys.stderr)
        return False
    if _wait_stopped(pid):
        return True
    print(f"afterwordd pid {pid} did not stop within {STOP_WAIT_S:.0f} s; not starting the service",
          file=sys.stderr)
    return False


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


def _fail(what: str, r: Result) -> int:
    print(f"{what} failed: {r.error()}", file=sys.stderr)
    return 1


def _not_installed() -> int:
    print(f"the service is not installed ({plist_path()} does not exist); run `afterword install-service`",
          file=sys.stderr)
    return 1


def _manual_daemon(info: ServiceInfo) -> tuple[bool, int | None]:
    """A daemon that is running but is not the service's process."""
    running, pid = daemon_pid()
    if running and not (info.running and pid == info.pid):
        return True, pid
    return False, None


def _bootout(info: ServiceInfo) -> Result:
    """Unload the job and wait until launchd has let go of it, so a
    bootstrap right after does not fail with "5: Input/output error".
    bootout may answer 36 (EINPROGRESS) while the job is still exiting:
    that is a stop under way, not a failure."""
    r = launchctl("bootout", target())
    if not r.ok and r.returncode != IN_PROGRESS_RC:
        return r
    end = time.monotonic() + STOP_WAIT_S
    while True:
        now = service_info()
        if now.loaded is False:
            break
        if time.monotonic() >= end:
            return Result(1, "", f"{LABEL} is still loaded {STOP_WAIT_S:.0f} s after bootout")
        if r.returncode == IN_PROGRESS_RC:
            r = launchctl("bootout", target())  # as `brew services stop` does
        time.sleep(0.1)
    if info.pid:
        _wait_stopped(info.pid)
    return Result(0)


def _disabled_error(r: Result) -> bool:
    return r.returncode == DISABLED_RC or "disabled" in r.error().lower()


def _while_enabled(args: tuple[str, ...], out) -> Result:
    """`launchctl <args>` (a bootstrap or kickstart) with the agent enabled.
    launchd refuses to load a disabled agent, and `afterword login off`
    disables it; so it is enabled for this one call and disabled again right
    after, whatever the call returned: start at login stays off."""
    login, _ = login_state()
    if login is not False:
        return launchctl(*args)
    r = set_login(True)
    if not r.ok:
        return Result(r.returncode, r.stdout, f"{_cmd('enable', target())} failed: {r.error()}")
    try:
        result = launchctl(*args)
    finally:
        back = set_login(False)
        if not back.ok:
            print(f"warning: {_cmd('disable', target())} failed: {back.error()}; start at login is on "
                  "now (`afterword login off` turns it off)", file=sys.stderr)
    if result.ok and back.ok:
        print("start at login is off (`afterword login on` turns it on)", file=out)
    return result


def _launch(args: tuple[str, ...], out) -> int:
    r = _while_enabled(args, out)
    if not r.ok:
        _fail(_cmd(*args), r)
        if _disabled_error(r):
            print("launchd has the service disabled; `afterword login on` enables it", file=sys.stderr)
        return 1
    return 0


def _bootstrap(out=None) -> int:
    return _launch(("bootstrap", domain(), str(plist_path())), out or sys.stdout)


def install(dry_run: bool = False, start: bool = True, out=None) -> int:
    out = out or sys.stdout
    path = plist_path()
    py = python_executable()
    data = plistlib.dumps(build_plist(py))
    info = service_info()
    if start and info.loaded is None:
        # Without a usable answer we cannot tell whether an older agent is
        # loaded: writing the plist and bootstrapping could leave a new plist
        # beside an old, still-loaded job.
        print(f"cannot tell whether the service is loaded: {info.error}; nothing written", file=sys.stderr)
        return 1
    if "/.cache/uv/" in py or "/uv/archive-" in py:
        print("warning: the interpreter is inside uv's cache (uvx); install with `uv tool install` "
              "or from a clone so the path stays valid.", file=sys.stderr)
    if dry_run:
        print(f"# would write {path}", file=out)
        out.write(data.decode())
        if not start:
            return 0
        if info.loaded:
            print(f"# would run: {_cmd('bootout', target())}", file=out)
        manual, pid = _manual_daemon(info)
        if manual:
            print(f"# would stop afterwordd pid {pid} (started outside launchd) with SIGTERM", file=out)
        disabled = login_state()[0] is False
        if disabled:
            print(f"# would run: {_cmd('enable', target())}  (start at login is off)", file=out)
        print(f"# would run: {_cmd('bootstrap', domain(), str(path))}", file=out)
        if disabled:
            print(f"# would run: {_cmd('disable', target())}  (keeps start at login off)", file=out)
        return 0
    common.ensure_home()  # WorkingDirectory and the log live there
    _write_plist(path, data)
    print(f"wrote {path}", file=out)
    if not start:
        if info.loaded:
            print("the loaded service keeps its old settings until `afterword stop` and `afterword start`",
                  file=out)
        else:
            print("not started; `afterword start` starts it", file=out)
        return 0
    was_loaded = bool(info.loaded)
    if was_loaded:  # reload, so launchd reads the new plist
        r = _bootout(info)
        if not r.ok:
            return _fail(_cmd("bootout", target()), r)
        info = service_info()
    manual, pid = _manual_daemon(info)
    if manual and not _stop_manual(pid, out):
        print("the plist is written; run `afterword start` once it has stopped", file=sys.stderr)
        return 1
    if _bootstrap(out):
        return 1
    now = service_info(login=True)
    where = f", pid {now.pid}" if now.running else ""
    at_login = "start at login is off, and it" if now.login is False else "it starts at login and"
    print(f"service {'reloaded' if was_loaded else 'started'}{where}: {at_login} restarts "
          f"after a crash; log {log_path()}", file=out)
    return 0


def start(out=None) -> int:
    out = out or sys.stdout
    if not plist_path().is_file():
        return _not_installed()
    info = service_info()
    if info.loaded is None:
        print(f"cannot tell whether the service is loaded: {info.error}", file=sys.stderr)
        return 1
    if info.running:
        print(f"already running, pid {info.pid}", file=out)
        return 0
    manual, pid = _manual_daemon(info)
    if manual:
        print(f"afterwordd pid {pid} is running outside launchd; stop it first, or run "
              "`afterword install-service`, which stops it and starts the service", file=sys.stderr)
        return 1
    if info.loaded:
        if _launch(("kickstart", target()), out):
            return 1
    elif _bootstrap(out):
        return 1
    now = service_info()
    print("started" + (f", pid {now.pid}" if now.running else ""), file=out)
    return 0


def stop(out=None) -> int:
    out = out or sys.stdout
    if not plist_path().is_file():
        return _not_installed()
    info = service_info()
    if info.loaded is None:
        print(f"cannot tell whether the service is loaded: {info.error}", file=sys.stderr)
        return 1
    if not info.loaded:
        manual, pid = _manual_daemon(info)
        note = f"; afterwordd pid {pid} is running outside launchd" if manual else ""
        print(f"not running (the service is not loaded){note}", file=out)
        return 0
    r = _bootout(info)
    if not r.ok:
        return _fail(_cmd("bootout", target()), r)
    if info.pid and pid_alive(info.pid):
        print(f"stopped the service; pid {info.pid} is still shutting down", file=out)
        return 0
    if login_state()[0] is False:
        print("stopped; start at login is off, so it stays stopped until `afterword start`", file=out)
    else:
        print("stopped; it starts again at the next login, or with `afterword start`", file=out)
    return 0


def restart(out=None) -> int:
    out = out or sys.stdout
    if not plist_path().is_file():
        return _not_installed()
    info = service_info()
    if info.loaded is None:
        print(f"cannot tell whether the service is loaded: {info.error}", file=sys.stderr)
        return 1
    if not info.loaded or not info.running:
        return start(out)
    # SIGTERM first so the daemon's own shutdown runs; it then exits 0,
    # which KeepAlive (SuccessfulExit = false) does not restart by itself.
    # Then kickstart -k: once the old process is gone it simply starts a new
    # one; if it is somehow still running (the wait timed out), -k ends it,
    # where a plain kickstart would do nothing to a running job.
    r = launchctl("kill", "SIGTERM", target())
    if r.ok:
        _wait_stopped(info.pid)
    if _launch(("kickstart", "-k", target()), out):
        return 1
    now = service_info()
    print("restarted" + (f", pid {now.pid}" if now.running else ""), file=out)
    return 0


def uninstall(out=None) -> int:
    out = out or sys.stdout
    path = plist_path()
    info = service_info()
    if info.loaded is None:  # it may be loaded: deleting the plist would orphan it
        print(f"cannot tell whether the service is loaded: {info.error}; nothing removed", file=sys.stderr)
        return 1
    if info.loaded:
        r = _bootout(info)
        if not r.ok:
            return _fail(_cmd("bootout", target()), r)
    if not path.exists() and not info.loaded:
        print("the service is not installed", file=out)
        return 0
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    print(f"service removed: {'stopped it and ' if info.loaded else ''}deleted {path}", file=out)
    # `afterword login off` left a disabled override in launchd; clear it so
    # a later install-service starts at login again, as a fresh install does.
    if login_state()[0] is False:
        r = set_login(True)
        if r.ok:
            print("start at login was off; reset to on for a later install-service", file=out)
        else:
            print(f"warning: {_cmd('enable', target())} failed: {r.error()}", file=sys.stderr)
    return 0


def login(state: str | None = None, out=None) -> int:
    """`afterword login [on|off]`: print or set whether launchd loads the
    agent at login."""
    out = out or sys.stdout
    installed = plist_path().is_file()
    if state is None:
        now, error = login_state()
        if now is None:
            print(f"start at login: unknown ({error})", file=out)
            return 1
        note = "" if installed else " (the service is not installed; `afterword install-service` installs it)"
        print(f"start at login: {'on' if now else 'off'}{note}", file=out)
        return 0
    if state not in ("on", "off"):
        print("usage: afterword login [on|off]", file=sys.stderr)
        return 2
    if not installed:
        return _not_installed()
    on = state == "on"
    r = set_login(on)
    if not r.ok:
        return _fail(_cmd("enable" if on else "disable", target()), r)
    if on:
        later = "" if service_info().loaded else "; `afterword start` starts it now"
        print(f"start at login: on; launchd loads the service at the next login{later}", file=out)
    else:
        print("start at login: off; launchd will not load the service at the next login "
              "(a running daemon keeps running; `afterword stop` stops it)", file=out)
    return 0


def run_open(*args: str) -> Result:
    """Run `open <args>`. The only place that does; tests replace it."""
    try:
        p = subprocess.run(["open", *args], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return Result(127, "", "open not found (Reveal in Finder needs macOS)")
    except subprocess.TimeoutExpired:
        return Result(124, "", "open timed out")
    return Result(p.returncode, p.stdout, p.stderr)


def reveal(path: Path) -> Result:
    """Show `path` selected in a Finder window."""
    return run_open("-R", str(path))


# --------------------------------------------------------------------------- #
# Logs                                                                        #
# --------------------------------------------------------------------------- #

TAIL_MAX_LINES = 1000
TAIL_MAX_BYTES = 512 * 1024


def tail(path: Path, lines: int = 200, max_bytes: int = TAIL_MAX_BYTES) -> dict[str, Any]:
    """The last `lines` lines of `path` (at most TAIL_MAX_LINES), from at
    most its last `max_bytes`, so a big log costs no more than a small one.
    A symlink is not followed. `truncated`: there is more before the first
    line returned."""
    lines = max(1, min(int(lines), TAIL_MAX_LINES))
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {"path": str(path), "exists": False, "size": 0, "lines": [], "truncated": False}
    with os.fdopen(fd, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        start = max(0, size - max_bytes)
        fh.seek(start)
        data = fh.read(max_bytes)
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""  # drop the partial first line
    text = data.decode("utf-8", errors="replace").splitlines()
    return {"path": str(path), "exists": True, "size": size, "lines": text[-lines:],
            "truncated": start > 0 or len(text) > lines}


# --------------------------------------------------------------------------- #
# service.log                                                                 #
# --------------------------------------------------------------------------- #


def trim_log(path: Path, limit: int = LOG_TRIM_OVER, keep: int = LOG_KEEP) -> bool:
    """If `path` is over `limit` bytes, keep only its last `keep` bytes
    (from the first full line), in place. launchd opened the file with
    O_APPEND, so its writes keep landing at the new end; renaming the file
    away would leave launchd writing to the old one. True if trimmed."""
    try:
        if path.stat().st_size <= limit:
            return False
    except OSError:
        return False
    with open(path, "r+b") as fh:
        size = os.fstat(fh.fileno()).st_size  # the size now, not at the stat above
        if size <= limit or size <= keep:
            return False
        fh.seek(size - keep - 1)
        before = fh.read(1)
        tail = fh.read(keep)
        if len(tail) < keep:  # shrank under us (another trim): leave it
            return False
        nl = tail.find(b"\n")
        if before != b"\n" and 0 <= nl < len(tail) - 1:
            tail = tail[nl + 1:]  # drop the partial first line
        fh.seek(0)
        fh.write(tail)
        fh.truncate()
    return True
