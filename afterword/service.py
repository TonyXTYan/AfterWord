"""The daemon as a per-user LaunchAgent: `afterword install-service`,
`start`, `stop`, `restart`, `uninstall-service`, and the service line in
`afterword status`.

Every launchctl call goes through `launchctl()`, which tests replace, and
the LaunchAgents directory can be moved with AFTERWORD_LAUNCH_AGENTS_DIR,
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
- uninstall-service unloads it and deletes the plist.
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
LOG_NAME = "service.log"
LOCK_NAME = "afterwordd.lock"  # taken by server._acquire_lock, which writes its pid into it
LOG_TRIM_OVER = 5 * 1024 * 1024
LOG_KEEP = 1024 * 1024
STOP_WAIT_S = 15.0
THROTTLE_S = 10
SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
NOT_FOUND_RC = 113  # `launchctl print` of a service that is not loaded
IN_PROGRESS_RC = 36  # `launchctl bootout` while the job is still exiting (EINPROGRESS)
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
    try:
        p = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return Result(127, "", "launchctl not found (AfterWord's service needs macOS)")
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

    @property
    def running(self) -> bool:
        return bool(self.loaded and self.state == "running" and self.pid)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": LABEL, "installed": self.installed, "plist": self.plist, "loaded": self.loaded,
            "state": self.state, "pid": self.pid, "last_exit": self.last_exit, "error": self.error,
        }

    def summary(self) -> str:
        """One line for `afterword status`."""
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
    return {"state": fields.get("state") or None, "pid": pid if pid and pid > 0 else None,
            "last_exit": fields.get("last exit code") or None}


def service_info() -> ServiceInfo:
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
    return info


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


def _bootstrap() -> int:
    r = launchctl("bootstrap", domain(), str(plist_path()))
    if not r.ok:
        return _fail(_cmd("bootstrap", domain(), str(plist_path())), r)
    return 0


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
        print(f"# would run: {_cmd('bootstrap', domain(), str(path))}", file=out)
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
    if _bootstrap():
        return 1
    now = service_info()
    where = f", pid {now.pid}" if now.running else ""
    print(f"service {'reloaded' if was_loaded else 'started'}{where}: it starts at login and restarts "
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
        r = launchctl("kickstart", target())
        if not r.ok:
            return _fail(_cmd("kickstart", target()), r)
    elif _bootstrap():
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
    args = ("kickstart", "-k", target())
    r = launchctl(*args)
    if not r.ok:
        return _fail(_cmd(*args), r)
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
    return 0


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
