"""Updates: the managed install, release checks, the switch, rollback, and
the plugin refresh after an update.

The service runs from its own copy of AfterWord, which only this module
changes (`afterword install-service`; `--dev` runs a checkout instead):

    <home>/app/repo.git           a bare git repository, the object store
    <home>/app/releases/<tag>/    a git worktree of release tag <tag>, with
                                  its own .venv (uv sync --frozen) and a
                                  marker file once it passed the smoke test
    <home>/app/current            symlink -> releases/<tag>
    <home>/update.json            the updater's state

The LaunchAgent runs <home>/app/current/.venv/bin/python -P -m
afterword.cli serve, so switching versions is a symlink swap plus a
restart. Only release tags (vX.Y.Z) are installed. The newest two releases
are kept: the current one and the previous one, for rollback.

The daemon checks `git ls-remote --tags` about 5 min after it starts and
then every [update] check_every_h hours. In "auto" mode, a managed install
run by launchd prepares a newer release (worktree, uv sync, smoke test)
while the old one keeps speaking, waits until nothing is queued, playing or
rendering, records the switch in update.json (pending), swaps the symlink
and exits 75, which launchd answers by starting the new release. That
daemon confirms the update after 60 s of serving. A release that does not
get there is started at most MAX_START_ATTEMPTS times: the next start
(`afterword serve` under launchd, before the daemon is imported) swaps the
symlink back, marks the tag rolled back (not retried automatically) and
starts the previous release in its place. After a confirmed update the
Claude Code and Codex plugins are refreshed, when their CLIs are on PATH
and the plugin is installed.

Every program (git, uv, the new release's python, claude, codex) runs
through `run_cmd()`, and tools are found with `which()`: tests replace both.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from afterword import __version__
from afterword.changelog import section as changelog_section
from afterword.hooks import common

log = logging.getLogger("afterword.update")

DEFAULT_REPO = "https://github.com/TonyXTYan/AfterWord.git"
MODES = ("auto", "notify", "off")
STATE_NAME = "update.json"
RUN_LOCK_NAME = "update.lock"  # held while a release is prepared or switched to
APP_DIR = "app"
REPO_DIR = "repo.git"
RELEASES_DIR = "releases"
CURRENT_LINK = "current"
READY_MARKER = ".afterword-release.json"  # in a release dir once it passed the smoke test
KEEP_RELEASES = 2  # current + previous
# The first release that has this module: an older one could not update
# itself, so the managed install refuses it (use --dev until it is tagged).
FIRST_UPDATER_VERSION = (0, 3, 6)
# Optional extras, and a module each one installs: the managed release gets
# the extras that are importable where install-service runs.
EXTRA_MODULES = {"qwen": "mlx_audio", "kokoro": "misaki", "chatterbox": "pykakasi"}

FIRST_CHECK_S = 300.0  # after the daemon starts
IDLE_POLL_S = 10.0
IDLE_SETTLE_S = 30.0  # idle this long before switching
IDLE_WAIT_S = 2 * 3600.0  # then give up until the next check
APPLY_WAIT_S = 600.0  # "Update now": how long it waits for idle
CONFIRM_AFTER_S = 60.0
MAX_START_ATTEMPTS = 3

LS_REMOTE_TIMEOUT_S = 60
FETCH_TIMEOUT_S = 600
GIT_TIMEOUT_S = 120
UV_TIMEOUT_S = 3600
SMOKE_TIMEOUT_S = 300
PLUGIN_TIMEOUT_S = 180

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
# Environment that would point uv or the new python at another environment.
_ENV_DROP = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP",
             "CONDA_PREFIX", "AFTERWORD_SERVICE")

# After a confirmed update, non-interactive: refresh the marketplace, then
# update the installed plugin. Claude Code loads new hooks in new sessions;
# Codex asks to review changed hooks again.
PLUGIN_ID = "afterword@afterword"
MARKETPLACE = "afterword"
PLUGIN_STEPS = {
    "claude": (("plugin", "marketplace", "update", MARKETPLACE), ("plugin", "update", PLUGIN_ID)),
    "codex": (("plugin", "marketplace", "upgrade", MARKETPLACE), ("plugin", "add", PLUGIN_ID)),
}
PLUGIN_NOTES = {
    "claude": "restart open Claude Code sessions to load the new hooks",
    "codex": "Codex asks you to review the changed hooks at its next start",
}


class UpdateError(Exception):
    pass


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


def run_cmd(argv: list[str], cwd: str | Path | None = None, env: dict[str, str] | None = None,
            timeout: float = 60) -> Result:
    """Run a program. The only place the updater does; tests replace it."""
    try:
        p = subprocess.run([str(a) for a in argv], cwd=None if cwd is None else str(cwd), env=env,
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return Result(127, "", f"{argv[0]} not found")
    except subprocess.TimeoutExpired:
        return Result(124, "", f"{Path(str(argv[0])).name} {' '.join(map(str, argv[1:3]))} timed out after {timeout:.0f} s")
    except OSError as exc:
        return Result(126, "", f"cannot run {argv[0]}: {exc}")
    return Result(p.returncode, p.stdout, p.stderr)


def which(name: str) -> str | None:
    """shutil.which; tests replace it."""
    return shutil.which(name)


def _last_lines(text: str, n: int = 6, limit: int = 1500) -> str:
    lines = [l for l in text.strip().splitlines() if l.strip()]
    out = "\n".join(lines[-n:])
    return out[-limit:]


# --------------------------------------------------------------------------- #
# Versions and tags                                                           #
# --------------------------------------------------------------------------- #


def parse_version(value: Any) -> tuple[int, int, int] | None:
    """(major, minor, patch) for "1.2.3" or "v1.2.3"; None for anything else."""
    if not isinstance(value, str):
        return None
    m = VERSION_RE.match(value.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def newer(tag: str | None, than: str | None) -> bool:
    a, b = parse_version(tag), parse_version(than)
    return a is not None and (b is None or a > b)


def parse_tags(ls_remote: str) -> list[str]:
    """Release tags (vX.Y.Z, nothing else) from `git ls-remote --tags`,
    oldest first. Peeled lines (`^{}`) and other tags are left out."""
    tags: set[str] = set()
    for line in ls_remote.splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/"):]
        if name.endswith("^{}"):
            name = name[:-3]
        if TAG_RE.match(name):
            tags.add(name)
    return sorted(tags, key=lambda t: parse_version(t))


def newest(tags: list[str]) -> str | None:
    return max(tags, key=lambda t: parse_version(t)) if tags else None


def install_tag(tags: list[str], version: str = __version__) -> str | None:
    """The release a managed install starts from: this code's own tag when
    it is a release, else the newest release."""
    own = f"v{version}"
    return own if own in tags else newest(tags)


def detect_extras() -> list[str]:
    """The optional extras importable in this environment (tests replace it)."""
    out = []
    for extra, module in EXTRA_MODULES.items():
        try:
            if importlib.util.find_spec(module) is not None:
                out.append(extra)
        except (ImportError, ValueError):
            pass
    return out


# --------------------------------------------------------------------------- #
# Settings ([update] in config.toml; validated in daemon/config.py)           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UpdateSettings:
    mode: str = "auto"  # "auto" | "notify" | "off"
    check_every_h: float = 6.0
    repo: str = DEFAULT_REPO


def config_settings() -> UpdateSettings:
    """[update] from config.toml (the daemon's validation), defaults if it
    cannot be read."""
    try:
        from afterword.daemon.config import ConfigStore

        return ConfigStore().get().update
    except Exception:
        return UpdateSettings()


# --------------------------------------------------------------------------- #
# Layout and state                                                            #
# --------------------------------------------------------------------------- #


class Layout:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.app = self.home / APP_DIR
        self.repo = self.app / REPO_DIR
        self.releases = self.app / RELEASES_DIR
        self.current = self.app / CURRENT_LINK
        self.state = self.home / STATE_NAME
        self.run_lock = self.home / RUN_LOCK_NAME

    def release(self, tag: str) -> Path:
        if not TAG_RE.match(tag):
            raise UpdateError(f"{tag!r} is not a release tag (vX.Y.Z)")
        return self.releases / tag

    @staticmethod
    def python_in(root: Path) -> Path:
        return root / ".venv" / "bin" / "python"

    def service_python(self) -> str:
        """What the plist runs: through the `current` link, never resolved."""
        return str(self.python_in(self.current).absolute())

    def current_tag(self) -> str | None:
        try:
            target = os.readlink(self.current)
        except OSError:
            return None
        name = Path(target).name
        return name if TAG_RE.match(name) else None

    def marker(self, tag: str) -> dict[str, Any] | None:
        try:
            data = json.loads((self.release(tag) / READY_MARKER).read_text(encoding="utf-8"))
        except (OSError, ValueError, UpdateError):
            return None
        return data if isinstance(data, dict) and data.get("tag") == tag else None

    def ready(self) -> list[str]:
        """Release tags with a passed smoke test, oldest first."""
        try:
            names = [p.name for p in self.releases.iterdir() if TAG_RE.match(p.name)]
        except OSError:
            return []
        return sorted((n for n in names if self.marker(n)), key=lambda t: parse_version(t))


def read_state(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((Path(home) / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def _flock(path: Path, blocking: bool = True):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


def change_state(home: Path, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into update.json (None deletes a key), atomically and
    under a lock; returns the new state."""
    home = Path(home)
    path = home / STATE_NAME
    with _flock(home / (STATE_NAME + ".lock")):
        state = read_state(home)
        for k, v in fields.items():
            if v is None:
                state.pop(k, None)
            else:
                state[k] = v
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    return state


def swap_current(layout: Layout, tag: str) -> None:
    """Point `current` at releases/<tag> in one step: a new symlink under a
    temporary name, renamed over the old one (rename(2) replaces the link
    itself, never what it points at)."""
    layout.release(tag)  # validates the tag
    layout.app.mkdir(parents=True, exist_ok=True)
    tmp = layout.app / f".current.{os.getpid()}.{secrets.token_hex(3)}"
    os.symlink(f"{RELEASES_DIR}/{tag}", tmp)
    try:
        os.replace(tmp, layout.current)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def package_dir() -> Path:
    """Where the running afterword package lives (tests replace it)."""
    import afterword

    return Path(afterword.__file__).resolve().parent


def running_kind(home: Path) -> str:
    """"managed" when this code runs from <home>/app/releases/<tag>, else "dev"."""
    releases = os.path.realpath(Layout(home).releases)
    pkg = os.path.realpath(package_dir())
    return "managed" if pkg.startswith(releases + os.sep) else "dev"


def plist_kind(home: Path) -> str:
    """What the installed LaunchAgent runs: "managed", "dev", or "none"."""
    import plistlib

    from afterword import service

    try:
        data = plistlib.loads(service.plist_path().read_bytes())
        py = data["ProgramArguments"][0]
    except Exception:
        return "none"
    return "managed" if str(py).startswith(str(Layout(home).current.absolute()) + os.sep) else "dev"


# --------------------------------------------------------------------------- #
# Plugins                                                                     #
# --------------------------------------------------------------------------- #


def plugin_installed(tool: str) -> bool:
    """Whether AfterWord's plugin is installed for `tool`, from that tool's
    own records (read only)."""
    if tool == "claude":
        root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
        try:
            data = json.loads((root / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        plugins = data.get("plugins") if isinstance(data, dict) else None
        return isinstance(plugins, dict) and bool(plugins.get(PLUGIN_ID))
    if tool == "codex":
        import tomllib

        root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
        try:
            data = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        plugins = data.get("plugins")
        return isinstance(plugins, dict) and isinstance(plugins.get(PLUGIN_ID), dict)
    return False


# --------------------------------------------------------------------------- #
# The updater                                                                 #
# --------------------------------------------------------------------------- #


class Updater:
    """Checks, prepares, switches and confirms. The blocking parts (check,
    prepare, switch, confirm, refresh_plugins) run in a thread; the async
    parts drive them from the daemon's loop.

    idle(): nothing queued, playing or rendering (the daemon's); restart():
    exit for launchd to start the daemon again; note(status, reason): a
    system row in the event log; serving(): the daemon is still up."""

    def __init__(self, home: Path, settings: Callable[[], UpdateSettings] | None = None, *,
                 version: str = __version__, runner: Callable[..., Result] | None = None,
                 which_fn: Callable[[str], str | None] | None = None, clock: Callable[[], float] | None = None,
                 sleep=None, kind: Callable[[], str] | None = None, as_service: Callable[[], bool] | None = None,
                 idle: Callable[[], bool] | None = None, restart: Callable[[], None] | None = None,
                 note: Callable[[str, str], None] | None = None, serving: Callable[[], bool] | None = None,
                 extras: Callable[[], list[str]] | None = None):
        self.home = Path(home)
        self.layout = Layout(self.home)
        self.settings = settings or UpdateSettings
        self.version = version
        self._run = runner or (lambda *a, **k: run_cmd(*a, **k))
        self._which = which_fn or (lambda name: which(name))
        self.clock = clock or time.time
        self._sleep = sleep or asyncio.sleep
        self._kind = kind or (lambda: running_kind(self.home))
        self._as_service = as_service or (lambda: os.environ.get("AFTERWORD_SERVICE") == "1")
        self.idle = idle or (lambda: True)
        self.restart = restart
        self._note = note or (lambda status, reason: None)
        self.serving = serving or (lambda: True)
        self._extras = extras or (lambda: detect_extras())
        self.phase = "idle"  # idle | checking | preparing | waiting | switching | restarting
        self.phase_tag: str | None = None
        self.next_check: float | None = None
        self._job = threading.Lock()  # one check or apply at a time in this process
        self._apply_task: asyncio.Task | None = None
        self._state_cache: tuple[float | None, dict[str, Any]] = (None, {})

    # -- state ----------------------------------------------------------- #

    def state(self) -> dict[str, Any]:
        """update.json, re-read only when it changed (status asks often)."""
        try:
            mtime = self.layout.state.stat().st_mtime_ns
        except OSError:
            mtime = None
        if mtime is None:
            return {}
        if self._state_cache[0] != mtime:
            self._state_cache = (mtime, read_state(self.home))
        return dict(self._state_cache[1])

    def change(self, **fields: Any) -> dict[str, Any]:
        return change_state(self.home, **fields)

    def _record(self, result: str, error: str | None = None) -> None:
        self.change(last_result=result, last_error=error, last_result_at=self.clock())

    # -- what may happen ------------------------------------------------- #

    def kind(self) -> str:
        return self._kind()

    def apply_blocked(self) -> str | None:
        """Why this process cannot switch versions, or None."""
        if self.kind() != "managed":
            return ("this daemon runs from a development checkout (`afterword install-service --dev`): "
                    "updates are reported, not applied; `afterword install-service` moves it to the managed install")
        if not self._as_service():
            return "this daemon was started by hand: updates are applied only when launchd runs it"
        return None

    def view(self) -> dict[str, Any]:
        st = self.state()
        s = self.settings()
        latest = st.get("latest")
        available = newer(latest, self.version)
        return {
            "install": self.kind(),
            "service": self._as_service(),
            "mode": s.mode,
            "check_every_h": s.check_every_h,
            "repo": s.repo,
            "running": self.version,
            "current": self.layout.current_tag(),
            "previous": st.get("previous"),
            "latest": latest,
            "available": available,
            "last_check": st.get("last_check"),
            "next_check": self.next_check,
            "last_result": st.get("last_result"),
            "last_error": st.get("last_error"),
            "last_result_at": st.get("last_result_at"),
            "phase": self.phase,
            "phase_tag": self.phase_tag,
            "pending": bool(st.get("pending")),
            "attempts": st.get("attempts", 0),
            "switched_at": st.get("switched_at"),
            "confirmed_at": st.get("confirmed_at"),
            "rolled_back": st.get("rolled_back"),
            "rollback_reason": st.get("rollback_reason"),
            "prepared": self.layout.ready(),
            "notes": st.get("notes") if available and st.get("notes_tag") == latest else None,
            "notes_tag": latest if available else None,
            "plugins": st.get("plugins"),
            "apply_blocked": self.apply_blocked(),
        }

    # -- tools ----------------------------------------------------------- #

    def git(self) -> str:
        exe = self._which("git")
        if not exe:
            raise UpdateError("git was not found on PATH (Xcode's command line tools install it)")
        return exe

    def uv(self) -> str:
        saved = self.state().get("uv")
        if isinstance(saved, str) and os.path.isabs(saved) and os.access(saved, os.X_OK):
            return saved
        exe = self._which("uv")
        if not exe:
            raise UpdateError("uv was not found (not where install-service found it, nor on PATH): "
                              "install it (https://docs.astral.sh/uv/) and run `afterword install-service` again")
        return os.path.abspath(exe)

    def _git_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # a public repo: never ask for credentials
        return env

    def _clean_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP}
        env["UV_NO_PROGRESS"] = "1"
        return env

    def _repo_url(self) -> str:
        url = self.settings().repo
        if not url or url.startswith("-"):
            raise UpdateError(f"[update] repo {url!r} is not a git URL")
        return url

    # -- check ----------------------------------------------------------- #

    def remote_tags(self) -> list[str]:
        url = self._repo_url()
        r = self._run([self.git(), "ls-remote", "--tags", url, "v*"], env=self._git_env(),
                      timeout=LS_REMOTE_TIMEOUT_S)
        if not r.ok:
            raise UpdateError(f"git ls-remote {url} failed: {_last_lines(r.error(), 3)}")
        return parse_tags(r.stdout)

    def check(self) -> dict[str, Any]:
        """Ask the repository for release tags; record the newest. Never
        raises: a failure is recorded as last_error. Returns view()."""
        try:
            tags = self.remote_tags()
        except UpdateError as exc:
            self.change(last_check=self.clock(), last_error=str(exc), last_result="check failed",
                        last_result_at=self.clock())
            log.warning("update check failed: %s", exc)
            return self.view()
        latest = newest(tags)
        fields: dict[str, Any] = {"last_check": self.clock(), "latest": latest, "last_error": None}
        if newer(latest, self.version):
            fields["last_result"] = f"{latest} is available (running {self.version})"
            st = self.state()
            if st.get("notes_tag") != latest:
                notes = self._notes(latest)
                if notes is not None:
                    fields.update(notes=notes, notes_tag=latest)
        else:
            fields["last_result"] = f"up to date ({self.version})" if latest else "no release tags found"
        fields["last_result_at"] = self.clock()
        self.change(**fields)
        return self.view()

    def _notes(self, tag: str) -> str | None:
        """The tag's CHANGELOG.md section, fetched into the object store
        (best effort: None on any failure)."""
        try:
            git = self.git()
            with _flock(self.layout.run_lock, blocking=False):
                self._ensure_repo(git)
                self._fetch_tag(git, tag)
            r = self._run([git, "-C", str(self.layout.repo), "show", f"{tag}:CHANGELOG.md"],
                          env=self._git_env(), timeout=GIT_TIMEOUT_S)
        except (UpdateError, BlockingIOError, OSError) as exc:
            log.info("no release notes for %s: %s", tag, exc)
            return None
        if not r.ok:
            return None
        return changelog_section(r.stdout, tag)

    # -- prepare --------------------------------------------------------- #

    def _ensure_repo(self, git: str) -> None:
        repo = self.layout.repo
        if (repo / "HEAD").is_file():
            return
        self.layout.app.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.app / f".{REPO_DIR}.{os.getpid()}.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        r = self._run([git, "init", "--bare", "--quiet", str(tmp)], env=self._git_env(), timeout=GIT_TIMEOUT_S)
        if not r.ok:
            shutil.rmtree(tmp, ignore_errors=True)
            raise UpdateError(f"git init failed: {_last_lines(r.error(), 3)}")
        os.replace(tmp, repo)

    def _fetch_tag(self, git: str, tag: str) -> None:
        url = self._repo_url()
        r = self._run([git, "-C", str(self.layout.repo), "fetch", "--quiet", "--no-tags", url,
                       f"+refs/tags/{tag}:refs/tags/{tag}"], env=self._git_env(), timeout=FETCH_TIMEOUT_S)
        if not r.ok:
            raise UpdateError(f"git fetch {tag} failed: {_last_lines(r.error(), 3)}")

    def _worktree(self, git: str, tag: str) -> Path:
        dest = self.layout.release(tag)
        if (dest / ".git").exists():
            return dest
        if dest.exists():
            if dest.name == self.layout.current_tag():
                raise UpdateError(f"{dest} is the current release but not a git worktree; not touching it")
            shutil.rmtree(dest)
        self.layout.releases.mkdir(parents=True, exist_ok=True)
        repo = str(self.layout.repo)
        self._run([git, "-C", repo, "worktree", "prune"], env=self._git_env(), timeout=GIT_TIMEOUT_S)
        r = self._run([git, "-C", repo, "worktree", "add", "--detach", "--force", str(dest), f"{tag}^{{commit}}"],
                      env=self._git_env(), timeout=GIT_TIMEOUT_S)
        if not r.ok:
            shutil.rmtree(dest, ignore_errors=True)
            raise UpdateError(f"git worktree add {tag} failed: {_last_lines(r.error(), 3)}")
        return dest

    def extras(self) -> list[str]:
        saved = self.state().get("extras")
        if isinstance(saved, list) and all(isinstance(e, str) and e in EXTRA_MODULES for e in saved):
            return sorted(saved)
        return sorted(self._extras())

    def prepare(self, tag: str) -> Path:
        """Make releases/<tag> ready to run: worktree, uv sync --frozen with
        the extras, smoke test. Raises UpdateError; the running release is
        never touched. A release already prepared with the same extras is
        reused as it is."""
        dest = self.layout.release(tag)
        git, uv = self.git(), self.uv()
        extras = self.extras()
        try:
            with _flock(self.layout.run_lock, blocking=False):
                marker = self.layout.marker(tag)
                if marker and sorted(marker.get("extras") or []) == extras and \
                        self.layout.python_in(dest).exists():
                    return dest
                self._ensure_repo(git)
                self._fetch_tag(git, tag)
                self._worktree(git, tag)
                (dest / READY_MARKER).unlink(missing_ok=True)
                argv = [uv, "sync", "--frozen", "--no-dev"]
                for e in extras:
                    argv += ["--extra", e]
                t0 = time.monotonic()
                r = self._run(argv, cwd=dest, env=self._clean_env(), timeout=UV_TIMEOUT_S)
                if not r.ok:
                    raise UpdateError(f"uv sync for {tag} failed: {_last_lines(r.error())}")
                sync_s = time.monotonic() - t0
                self.smoke_test(dest, tag)
                (dest / READY_MARKER).write_text(json.dumps({
                    "tag": tag, "extras": extras, "uv": uv, "prepared_at": self.clock(),
                    "sync_s": round(sync_s, 1)}, indent=2) + "\n", encoding="utf-8")
        except BlockingIOError:
            raise UpdateError("another update is in progress (update.lock is held)") from None
        except OSError as exc:
            raise UpdateError(f"preparing {tag} failed: {exc}") from None
        log.info("prepared %s in %s (extras: %s)", tag, dest, ", ".join(extras) or "none")
        return dest

    def smoke_test(self, dest: Path, tag: str) -> None:
        py = self.layout.python_in(dest)
        env = self._clean_env()
        r = self._run([str(py), "-P", "-m", "afterword.cli", "--version"], cwd=dest, env=env,
                      timeout=SMOKE_TIMEOUT_S)
        want = f"afterword {tag[1:]}"
        if not r.ok or r.stdout.strip() != want:
            got = r.stdout.strip() or _last_lines(r.error(), 3)
            raise UpdateError(f"smoke test of {tag} failed: `afterword --version` gave {got!r}, not {want!r}")
        r = self._run([str(py), "-P", "-c", "import afterword.daemon.server, afterword.daemon.web"],
                      cwd=dest, env=env, timeout=SMOKE_TIMEOUT_S)
        if not r.ok:
            raise UpdateError(f"smoke test of {tag} failed: importing the daemon failed: {_last_lines(r.error())}")

    # -- switch ---------------------------------------------------------- #

    def switch(self, tag: str, pending: bool = True) -> str | None:
        """Point `current` at the prepared `tag`; with `pending`, record it
        so the next starts count towards a rollback. Returns the previous
        tag. Older releases than current and previous are deleted."""
        if not self.layout.marker(tag):
            raise UpdateError(f"{tag} is not prepared")
        try:
            with _flock(self.layout.run_lock, blocking=False):
                old = self.layout.current_tag()
                if old == tag:
                    return old
                fields: dict[str, Any] = {"previous": old, "current": tag, "switched_at": self.clock(),
                                          "rolled_back": None, "rollback_reason": None}
                if pending and old:
                    fields.update(pending=True, attempts=0)
                else:
                    fields.update(pending=None, attempts=None)
                self.change(**fields)  # before the swap: a crash in between still rolls back
                swap_current(self.layout, tag)
                self.prune(keep={tag, old} if old else {tag})
        except BlockingIOError:
            raise UpdateError("another update is in progress (update.lock is held)") from None
        log.info("switched %s -> %s", old, tag)
        return old

    def prune(self, keep: set[str | None]) -> list[str]:
        """Delete release dirs not in `keep` (at most KEEP_RELEASES are kept)."""
        removed = []
        current = self.layout.current_tag()
        try:
            dirs = [p for p in self.layout.releases.iterdir() if TAG_RE.match(p.name)]
        except OSError:
            return removed
        for p in dirs:
            if p.name in keep or p.name == current:
                continue
            git = self._which("git")
            if git:
                self._run([git, "-C", str(self.layout.repo), "worktree", "remove", "--force", str(p)],
                          env=self._git_env(), timeout=GIT_TIMEOUT_S)
            shutil.rmtree(p, ignore_errors=True)
            removed.append(p.name)
        if removed and self._which("git"):
            self._run([self._which("git"), "-C", str(self.layout.repo), "worktree", "prune"],
                      env=self._git_env(), timeout=GIT_TIMEOUT_S)
        for stale in self.layout.app.glob(".current.*"):
            with contextlib.suppress(OSError):
                stale.unlink()
        return removed

    # -- after a switch -------------------------------------------------- #

    def confirm(self) -> bool:
        """The running release has served long enough: the update stands."""
        st = self.state()
        if not st.get("pending") or st.get("current") != f"v{self.version}":
            return False
        self.change(pending=None, attempts=None, confirmed_at=self.clock(), plugins_pending=True,
                    last_result=f"updated to v{self.version}" + (f" from {st['previous']}" if st.get("previous") else ""),
                    last_error=None, last_result_at=self.clock())
        log.info("update to v%s confirmed", self.version)
        return True

    def refresh_plugins(self) -> dict[str, Any]:
        """Update the Claude Code and Codex plugins, each only when its CLI
        is on PATH and AfterWord's plugin is installed there. Failures are
        recorded, never raised."""
        results: dict[str, Any] = {}
        for tool, steps in PLUGIN_STEPS.items():
            exe = self._which(tool)
            if not exe:
                results[tool] = {"ok": None, "message": f"{tool} is not on PATH; skipped"}
                continue
            if not plugin_installed(tool):
                results[tool] = {"ok": None, "message": f"the {tool} plugin is not installed; skipped"}
                continue
            for args in steps:
                r = self._run([exe, *args], timeout=PLUGIN_TIMEOUT_S)
                if not r.ok:
                    results[tool] = {"ok": False, "message": f"`{tool} {' '.join(args)}` failed: {_last_lines(r.error(), 3)}"}
                    log.warning("plugin refresh: %s", results[tool]["message"])
                    break
            else:
                results[tool] = {"ok": True, "message": f"updated; {PLUGIN_NOTES[tool]}"}
        self.change(plugins={"at": self.clock(), "version": self.version, **results}, plugins_pending=None)
        return results

    # -- the daemon's loop ----------------------------------------------- #

    async def _thread(self, fn, *args):
        """Run `fn` in a daemon thread: unlike asyncio.to_thread, a long uv
        sync does not hold up the daemon's exit."""
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        def work():
            try:
                value = fn(*args)
            except BaseException as exc:  # noqa: BLE001 - handed to the awaiting task
                # Bound now: `exc` is unset once this except block ends.
                done = lambda e=exc: fut.done() or fut.set_exception(e)  # noqa: E731
            else:
                done = lambda: fut.done() or fut.set_result(value)  # noqa: E731
            with contextlib.suppress(RuntimeError):  # the loop has closed: nobody waits any more
                loop.call_soon_threadsafe(done)

        threading.Thread(target=work, name=f"update-{getattr(fn, '__name__', 'job')}", daemon=True).start()
        return await fut

    async def startup(self) -> None:
        """Once the daemon is up: report a rollback, confirm a pending update
        after CONFIRM_AFTER_S of serving, and refresh the plugins after it."""
        st = self.state()
        if st.get("rollback_notice"):
            self.change(rollback_notice=None)
            self._note("error", f"update to {st.get('rolled_back')} rolled back: {st.get('rollback_reason')}")
        if st.get("pending") and st.get("current") == f"v{self.version}" and self.kind() == "managed":
            t0 = self.clock()
            while self.clock() - t0 < CONFIRM_AFTER_S:
                await self._sleep(min(5.0, CONFIRM_AFTER_S - (self.clock() - t0)))
            if not self.serving():
                return
            if await self._thread(self.confirm):
                st = self.state()
                self._note("info", st.get("last_result") or f"updated to v{self.version}")
        if self.state().get("plugins_pending"):
            results = await self._thread(self.refresh_plugins)
            failed = [r["message"] for r in results.values() if r.get("ok") is False]
            if failed:
                self._note("error", "plugin refresh: " + "; ".join(failed))
            elif any(r.get("ok") for r in results.values()):
                self._note("info", "plugins updated: " + "; ".join(
                    f"{t}: {r['message']}" for t, r in results.items() if r.get("ok")))

    async def run_loop(self) -> None:
        """The daemon's update task."""
        t0 = self.clock()
        try:
            await self.startup()
        except Exception:
            log.exception("update startup step failed")
        self.next_check = t0 + FIRST_CHECK_S
        while True:
            wait = self.next_check - self.clock()
            if wait > 0:
                await self._sleep(min(wait, 60.0))  # short naps: a changed check_every_h applies soon
                continue
            s = self.settings()
            self.next_check = self.clock() + max(1.0, s.check_every_h) * 3600
            if s.mode == "off":
                continue
            try:
                await self.tick()
            except Exception:
                log.exception("update check failed")
                self.phase, self.phase_tag = "idle", None

    async def check_now(self) -> dict[str, Any]:
        if not self._job.acquire(blocking=False):
            return self.view()  # a check or apply is running: its result comes soon
        try:
            self.phase = "checking"
            return await self._thread(self.check)
        finally:
            self.phase = "idle" if self.phase == "checking" else self.phase
            self._job.release()

    async def tick(self) -> None:
        """A scheduled check; in auto mode, prepare and switch to a newer release."""
        if not self._job.acquire(blocking=False):
            return
        try:
            self.phase = "checking"
            view = await self._thread(self.check)
            self.phase = "idle"
            if self.settings().mode != "auto" or not view["available"]:
                return
            blocked = self.apply_blocked()
            if blocked:
                return
            latest = view["latest"]
            if latest == self.state().get("rolled_back"):
                self._record(f"{latest} was rolled back; it is not retried automatically (Update now retries it)")
                return
            await self._apply(latest, force=False, wait_s=IDLE_WAIT_S)
        finally:
            self._job.release()

    def start_apply(self, force: bool = False, wait_s: float | None = None) -> str:
        """"Update now": check, prepare, wait for idle (up to wait_s; not at
        all with force), switch. Runs in the background; returns the tag it
        goes for (None when no check has run yet: the job checks first).
        Raises UpdateError when it cannot start."""
        blocked = self.apply_blocked()
        if blocked:
            raise UpdateError(blocked)
        if self.phase == "restarting" or not self._job.acquire(blocking=False):
            # (restarting: switched already; the daemon is on its way out)
            raise UpdateError(f"an update step is already running ({self.phase})")
        latest = self.state().get("latest")
        if latest is not None and not newer(latest, self.version):
            self._job.release()
            raise UpdateError(f"already up to date: the newest release is {latest} (running {self.version})")
        wait = APPLY_WAIT_S if wait_s is None else max(0.0, float(wait_s))
        self.phase, self.phase_tag = "checking", None  # before the answer goes out: the page polls for it

        async def job():
            try:
                self.phase = "checking"
                view = await self._thread(self.check)
                target = view["latest"] if view["available"] else None
                self.phase = "idle"
                if not target:
                    self._record(view.get("last_result") or "nothing to update", view.get("last_error"))
                    return
                if target == self.state().get("rolled_back"):
                    self.change(rolled_back=None, rollback_reason=None)  # an explicit retry
                await self._apply(target, force=force, wait_s=wait)
            except Exception as exc:
                log.exception("update failed")
                self._record("update failed", f"{type(exc).__name__}: {exc}")
            finally:
                if self.phase != "restarting":
                    self.phase, self.phase_tag = "idle", None
                self._job.release()

        self._apply_task = asyncio.get_running_loop().create_task(job(), name="update-apply")
        return latest

    async def wait_idle(self, max_s: float) -> bool:
        """True once idle() has held for IDLE_SETTLE_S; False after max_s."""
        end = self.clock() + max_s
        since = None
        while True:
            now = self.clock()
            if self.idle():
                since = now if since is None else since
                if now - since >= IDLE_SETTLE_S:
                    return True
            else:
                since = None
            if now >= end:
                return False
            await self._sleep(IDLE_POLL_S)

    async def _apply(self, tag: str, force: bool, wait_s: float) -> bool:
        self.phase, self.phase_tag = "preparing", tag
        try:
            await self._thread(self.prepare, tag)
        except UpdateError as exc:
            self._record(f"could not prepare {tag}; staying on {self.version}", str(exc))
            self._note("error", f"update to {tag} not prepared: {exc}")
            self.phase, self.phase_tag = "idle", None
            return False
        if not force:
            self.phase = "waiting"
            if not await self.wait_idle(wait_s):
                self._record(f"{tag} is ready; waiting for a moment with nothing to say (next try at the next check)")
                self.phase, self.phase_tag = "idle", None
                return False
        self.phase = "switching"
        try:
            old = await self._thread(self.switch, tag)
        except UpdateError as exc:
            self._record(f"could not switch to {tag}", str(exc))
            self.phase, self.phase_tag = "idle", None
            return False
        self._record(f"switched from {old} to {tag}; restarting", None)
        self._note("info", f"update: switching from {old or self.version} to {tag}; restarting")
        self.phase = "restarting"
        if self.restart is not None:
            self.restart()
        return True


def daemon_idle(d) -> bool:
    """Nothing queued, playing or rendering in the daemon `d`: no line, no
    voice lab preview or stability run, no seed-explorer take."""
    if getattr(d, "_stopping", False):
        return False
    if d.queue.now_playing is not None or len(d.queue) > 0 or getattr(d, "_current", None) is not None:
        return False
    engine = d.engine
    if getattr(engine, "_busy", False) or getattr(engine, "_pending", 0):
        return False
    lab = d.lab
    for lock in ("_say_lock", "_stability_lock"):
        held = getattr(lab, lock, None)
        if held is not None and held.locked():
            return False
    x = getattr(getattr(lab, "explorer", None), "x", None)
    return not (x is not None and getattr(x, "rendering", None) is not None)


def for_daemon(d) -> Updater:
    """The daemon's updater: its config, idle state, restart and event log."""

    def note(status: str, reason: str) -> None:
        try:
            d.events.insert(kind="system", status=status, reason=reason)
        except Exception:
            log.warning("could not log %r", reason, exc_info=True)

    return Updater(d.home, settings=lambda: d.store.get().update, idle=lambda: daemon_idle(d),
                   restart=d.request_restart, note=note, serving=lambda: not getattr(d, "_stopping", False))


# --------------------------------------------------------------------------- #
# Rollback at startup (afterword serve, before the daemon is imported)        #
# --------------------------------------------------------------------------- #


def startup_check(home: Path | None = None, exec_fn=None, argv: list[str] | None = None, out=None) -> str:
    """Under launchd, with a switch pending: count this start; after more
    than MAX_START_ATTEMPTS starts without a confirmation, point `current`
    back at the previous release and start that instead (exec). Returns
    "none", "counted", "rolled-back" or "no-rollback" (tests); after a
    rollback it only returns when exec_fn does."""
    if os.environ.get("AFTERWORD_SERVICE") != "1":
        return "none"
    home = Path(home) if home is not None else common.home()
    state = read_state(home)
    if not state.get("pending"):
        return "none"
    if running_kind(home) != "managed":
        return "none"
    out = out or sys.stderr
    attempts = int(state.get("attempts") or 0) + 1
    state = change_state(home, attempts=attempts)
    if attempts <= MAX_START_ATTEMPTS:
        return "counted"
    layout = Layout(home)
    bad, prev = state.get("current"), state.get("previous")
    reason = (f"{bad} did not stay up for {CONFIRM_AFTER_S:.0f} s in {attempts - 1} starts")
    if not prev or not layout.marker(prev):
        change_state(home, pending=None, attempts=None, rolled_back=bad,
                     rollback_reason=reason + f"; no previous release to go back to", rollback_notice=True)
        print(f"afterword: {reason}, and there is no previous release to go back to", file=out)
        return "no-rollback"
    swap_current(layout, prev)
    change_state(home, pending=None, attempts=None, current=prev, previous=None, rolled_back=bad,
                 rollback_reason=reason, rolled_back_at=time.time(), rollback_notice=True,
                 last_result=f"rolled back from {bad} to {prev}", last_error=reason, last_result_at=time.time())
    print(f"afterword: rolling back: {reason}; starting {prev}", file=out, flush=True)
    py = layout.service_python()
    args = list(sys.argv[1:] if argv is None else argv)
    (exec_fn or os.execv)(py, [py, "-P", "-m", "afterword.cli", *args])
    return "rolled-back"


# --------------------------------------------------------------------------- #
# install-service                                                             #
# --------------------------------------------------------------------------- #


def install_managed(dry_run: bool = False, start: bool = True, out=None, updater: Updater | None = None) -> int:
    """`afterword install-service`: make (or refresh) the managed install at
    this code's release, then point the LaunchAgent at it. Everything is
    prepared and smoke-tested before the running service is touched."""
    from afterword import service

    out = out or sys.stdout
    home = common.home()
    up = updater or Updater(home, settings=config_settings, version=__version__)
    lay = up.layout
    try:
        git, uv = up.git(), up.uv()
        tags = up.remote_tags()
    except UpdateError as exc:
        print(f"install-service: {exc}; nothing changed", file=sys.stderr)
        return 1
    tag = install_tag(tags, up.version)
    if tag is None:
        print(f"install-service: {up.settings().repo} has no release tags (vX.Y.Z); "
              "`afterword install-service --dev` runs this checkout instead", file=sys.stderr)
        return 1
    if parse_version(tag) < FIRST_UPDATER_VERSION:
        first = "v" + ".".join(map(str, FIRST_UPDATER_VERSION))
        print(f"install-service: the newest release, {tag}, predates the updater ({first}) and could not "
              f"update itself; `afterword install-service --dev` runs this checkout until {first} is released",
              file=sys.stderr)
        return 1
    extras = sorted(up._extras())  # what this environment has; later updates keep it
    if dry_run:
        print(f"# managed install of {tag} in {lay.release(tag)} (extras: {', '.join(extras) or 'none'})", file=out)
        print(f"# would fetch {tag} from {up.settings().repo} into {lay.repo}", file=out)
        print(f"# would run: {uv} sync --frozen --no-dev" + "".join(f" --extra {e}" for e in extras), file=out)
        print(f"# would smoke-test it, then point {lay.current} at releases/{tag}", file=out)
        return service.install(dry_run=True, start=start, out=out, python=lay.service_python())
    common.ensure_home()
    up.change(uv=uv, extras=extras)
    if not lay.marker(tag):
        print(f"preparing {tag} in {lay.release(tag)} (extras: {', '.join(extras) or 'none'}); this can take a "
              "few minutes the first time", file=out, flush=True)
    try:
        up.prepare(tag)
        old = up.switch(tag, pending=False)
    except UpdateError as exc:
        print(f"install-service: {exc}; the service was not touched", file=sys.stderr)
        return 1
    up.change(install="managed", pending=None, attempts=None)
    note = f" (was {old})" if old and old != tag else ""
    print(f"{lay.current} -> releases/{tag}{note}", file=out)
    return service.install(dry_run=False, start=start, out=out, python=lay.service_python())


def note_dev_install() -> None:
    """`install-service --dev`: no switch is pending any more."""
    home = common.home()
    if (home / STATE_NAME).exists():
        change_state(home, install="dev", pending=None, attempts=None)


# --------------------------------------------------------------------------- #
# Text for `afterword status` and `afterword update`                          #
# --------------------------------------------------------------------------- #


def _ago(ts: Any, now: float | None = None) -> str:
    if not isinstance(ts, (int, float)):
        return "never"
    s = max(0, int((now or time.time()) - ts))
    for unit, size in (("d", 86400), ("h", 3600), (" min", 60)):
        if s >= size:
            return f"{s // size}{unit} ago"
    return "just now"


def summary(v: dict[str, Any]) -> str:
    """One line for `afterword status`."""
    install = "managed" + (f" {v['current']}" if v.get("current") else "") if v.get("install") == "managed" else "dev checkout"
    head = f"{v.get('mode')}, {install}"
    if v.get("pending"):
        return f"{head}; switched to {v.get('current')}, not confirmed yet"
    if v.get("phase") not in (None, "idle"):
        return f"{head}; {v['phase']} {v.get('phase_tag') or ''}".rstrip()
    if v.get("available"):
        state = f"{v['latest']} available"
    elif v.get("latest"):
        state = "up to date"
    else:
        state = "not checked yet" if not v.get("last_check") else "no release found"
    if v.get("last_error"):
        state += f" (last error: {v['last_error'].splitlines()[0][:120]})"
    return f"{head}; {state}, checked {_ago(v.get('last_check'))}"


def describe(v: dict[str, Any]) -> list[str]:
    """`afterword update` without options."""
    lines = [
        f"running   {v.get('running')} ({'managed install' if v.get('install') == 'managed' else 'development checkout'}"
        + (", under launchd)" if v.get("service") else ")"),
        f"mode      {v.get('mode')} (every {v.get('check_every_h')} h from {v.get('repo')})",
        f"latest    {v.get('latest') or '—'}" + (" (available)" if v.get("available") else ""),
        f"checked   {_ago(v.get('last_check'))}",
        f"result    {v.get('last_result') or '—'}",
    ]
    if v.get("last_error"):
        lines.append(f"error     {v['last_error']}")
    if v.get("install") == "managed":
        lines.append(f"current   {v.get('current') or '—'}   previous {v.get('previous') or '—'}   "
                     f"prepared {', '.join(v.get('prepared') or []) or '—'}")
    if v.get("phase") not in (None, "idle"):
        lines.append(f"now       {v['phase']} {v.get('phase_tag') or ''}".rstrip())
    if v.get("pending"):
        lines.append(f"pending   switched to {v.get('current')}, {v.get('attempts', 0)} start(s), not confirmed yet")
    if v.get("rolled_back"):
        lines.append(f"rolled back {v['rolled_back']}: {v.get('rollback_reason')} (not retried automatically; "
                     "`afterword update --now` retries it)")
    plugins = v.get("plugins")
    if isinstance(plugins, dict):
        for tool in PLUGIN_STEPS:
            r = plugins.get(tool)
            if isinstance(r, dict):
                lines.append(f"plugin    {tool}: {r.get('message')}")
    if v.get("apply_blocked") and v.get("available"):
        lines.append(f"note      {v['apply_blocked']}")
    return lines


def local_updater(home: Path | None = None) -> Updater:
    """An Updater for the CLI when no daemon answers: the install kind is
    what the LaunchAgent runs, and it never restarts anything."""
    home = Path(home) if home is not None else common.home()
    kind = plist_kind(home)
    current = Layout(home).current_tag() if kind == "managed" else None
    # The version that matters is what the service runs, not this CLI's.
    return Updater(home, settings=config_settings, version=current[1:] if current else __version__,
                   kind=lambda: "managed" if kind == "managed" else "dev", as_service=lambda: kind == "managed")
