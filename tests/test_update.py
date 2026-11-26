"""Updates (afterword/update.py): release checks, the managed install,
prepare / switch / rollback / confirm, the plugin refresh, the daemon's
loop, `afterword install-service` and `afterword update`, the HTTP API,
the GUI's Updates part, and the release workflow. git, uv, the release's
python, claude, codex and launchctl are all fakes; every home is a temp
directory."""

import asyncio
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from afterword import cli, service, update
from afterword.changelog import section
from test_service import FakeLaunchd

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

CHANGELOG = """\
# Changelog

Intro text.

## 0.3.7 (2026-12-01)

- Seven.
- More seven.

## 0.3.6 (2026-11-30)

- Six.

## 0.3.5 (2026-11-27)

- Five.
"""


def ls_remote(*tags):
    out = []
    for i, t in enumerate(tags):
        out.append(f"{i:040x}\trefs/tags/{t}")
        out.append(f"{i + 100:040x}\trefs/tags/{t}^{{}}")
    return "\n".join(out) + "\n"


class Tools:
    """Fake git, uv, release python, claude and codex: just enough to build
    the managed layout on disk and answer like the real programs."""

    def __init__(self, tags=("v0.3.5", "v0.3.6", "v0.3.7")):
        self.tags = list(tags)
        self.calls = []
        # key -> Result: "ls-remote", "fetch", "worktree", "sync:<tag>", "import:<tag>", "<tool> <sub>", "cp"
        self.fail = {}
        self.versions = {}  # tag -> what `--version` prints instead of the tag's version
        self.commits = {}  # tag -> the commit it points at (default: a hash of the tag's name)
        self.off_main = set()  # tags whose commit is not on main
        self.changelog = CHANGELOG
        self.paths = {"git": "/usr/bin/git", "uv": "/fake/bin/uv", "claude": "/fake/bin/claude",
                      "codex": "/fake/bin/codex"}

    def which(self, name):
        return self.paths.get(name)

    def names(self):
        """Each call as a short name: "ls-remote", "fetch v0.3.7", "uv sync v0.3.7", ..."""
        return [n for n, _ in self.calls]

    def _tag_of(self, path):
        return Path(path).name

    def sha(self, tag):
        return self.commits.get(tag) or hashlib.sha1(tag.encode()).hexdigest()

    def tag_of_sha(self, sha):
        return next((t for t in self.tags if self.sha(t) == sha), sha[:7])

    def __call__(self, argv, cwd=None, env=None, timeout=60):
        argv = [str(a) for a in argv]
        exe = Path(argv[0]).name
        name, result = self._answer(exe, argv, cwd)
        self.calls.append((name, argv))
        return result

    def _answer(self, exe, argv, cwd):
        ok = update.Result(0)
        if exe == "git":
            if argv[1] == "ls-remote":
                assert argv[2:] == ["--tags", update.DEFAULT_REPO, "v*"]
                return "ls-remote", self.fail.get("ls-remote") or update.Result(0, ls_remote(*self.tags, "latest", "v1.0"))
            if argv[1] == "init":
                path = Path(argv[-1])
                path.mkdir(parents=True)
                (path / "HEAD").write_text("ref: refs/heads/main\n")
                return "init", ok
            assert argv[1] == "-C"
            sub = argv[3]
            if sub == "fetch":
                spec = next(a for a in argv if a.startswith("+refs/tags/"))
                assert argv[-1] == "+refs/heads/main:refs/heads/main"  # main comes with it
                tag = spec.split(":")[-1].rsplit("/", 1)[-1]
                return f"fetch {tag}", self.fail.get("fetch") or ok
            if sub == "rev-parse" and argv[-1] == "HEAD":  # in a release worktree
                head = Path(argv[2]) / ".fake-head"
                name = f"head {Path(argv[2]).name}"
                return name, update.Result(0, head.read_text() + "\n") if head.exists() else update.Result(128)
            if sub == "rev-parse":
                assert argv[2].endswith("repo.git") and argv[4:6] == ["--verify", "--quiet"]
                tag = argv[-1].removesuffix("^{commit}")
                return f"rev-parse {tag}", update.Result(0, self.sha(tag) + "\n") if tag in self.tags \
                    else update.Result(1)
            if sub == "merge-base":
                assert argv[4] == "--is-ancestor" and argv[-1] == "refs/heads/main"
                tag = self.tag_of_sha(argv[5])
                return f"merge-base {tag}", update.Result(1 if tag in self.off_main else 0)
            if sub == "worktree" and argv[4] == "add":
                dest = Path(argv[-2])
                if "worktree" in self.fail:
                    return f"worktree add {dest.name}", self.fail["worktree"]
                dest.mkdir(parents=True)
                (dest / ".git").write_text("gitdir: ../../repo.git/worktrees/x\n")
                (dest / ".fake-head").write_text(argv[-1])
                return f"worktree add {dest.name}", ok
            if sub == "worktree" and argv[4] == "remove":
                shutil.rmtree(argv[-1], ignore_errors=True)
                return f"worktree remove {Path(argv[-1]).name}", ok
            if sub == "worktree":
                return "worktree prune", ok
            if sub == "show":
                return f"show {argv[4]}", update.Result(0, self.changelog)
            raise AssertionError(argv)
        if exe == "uv":
            tag = self._tag_of(cwd)
            assert argv[1:4] == ["sync", "--frozen", "--no-dev"]
            if f"sync:{tag}" in self.fail:
                return f"uv sync {tag}", self.fail[f"sync:{tag}"]
            py = Path(cwd) / ".venv" / "bin" / "python"
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
            if "kokoro" in argv:  # misaki[ja] brings unidic, without its dictionary
                (Path(cwd) / ".venv" / "lib" / "python3.12" / "site-packages" / "unidic").mkdir(parents=True, exist_ok=True)
            return f"uv sync {tag}", ok
        if exe == "cp":
            assert argv[:4] == ["/bin/cp", "-c", "-R", argv[3]]
            if "cp" in self.fail:
                return "cp", self.fail["cp"]
            shutil.copytree(argv[3], argv[4])
            return "cp", ok
        if exe == "python":
            tag = Path(argv[0]).parents[2].name
            assert argv[1] == "-P"
            if argv[2] == "-m":
                assert argv[3:] == ["afterword.cli", "--version"]
                return f"version {tag}", update.Result(0, self.versions.get(tag, f"afterword {tag[1:]}") + "\n")
            assert argv[2:] == ["-c", "import afterword.daemon.server, afterword.daemon.web"]
            return f"import {tag}", self.fail.get(f"import:{tag}") or ok
        if exe in ("claude", "codex"):
            key = f"{exe} {argv[2] if argv[1] == 'plugin' and argv[2] == 'marketplace' else argv[2]}"
            return " ".join([exe, *argv[1:]]), self.fail.get(key) or ok
        raise AssertionError(argv)


class Clock:
    """Wall-clock time (calling it) and monotonic time (mono); sleep()
    advances both. Values in `asleep` are system sleeps, one per sleep()
    call: the wall clock jumps by that much more, the monotonic one does not
    (as on macOS)."""

    def __init__(self, t=1_000_000.0):
        self.t = t
        self.m = 5_000.0
        self.slept = []
        self.until = None
        self.asleep = []

    def __call__(self):
        return self.t

    def mono(self):
        return self.m

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s + (self.asleep.pop(0) if self.asleep else 0)
        self.m += s
        if self.until is not None and self.t > self.until:
            raise _Stop()
        await asyncio.sleep(0)


class _Stop(Exception):
    pass


@pytest.fixture
def tools(monkeypatch):
    t = Tools()
    monkeypatch.setattr(update, "run_cmd", t)
    monkeypatch.setattr(update, "which", t.which)
    monkeypatch.setattr(update, "detect_extras", lambda: ["qwen"])
    # The code under test is an untagged build older than the fake releases
    # (so install-service picks the newest one), whatever the repo's real
    # version is: tests that need their own tag set it themselves.
    monkeypatch.setattr(update, "__version__", "0.3.4")
    return t


def make(home, tools=None, version="0.3.6", mode="auto", kind="managed", service_=True, **kw):
    s = update.UpdateSettings(mode=mode)
    kw.setdefault("settings", lambda: s)
    kw.setdefault("extras", lambda: ["qwen"])
    kw.setdefault("unidic", lambda: None)
    return update.Updater(home, version=version, kind=lambda: kind, as_service=lambda: service_, **kw)


def install_release(up, tag):
    """A prepared release made the current one, as after install-service."""
    up.prepare(tag)
    up.switch(tag, pending=False)


# --------------------------------------------------------------------------- #
# Versions and tags                                                           #
# --------------------------------------------------------------------------- #


def test_version_compare():
    assert update.parse_version("v0.3.10") == (0, 3, 10) == update.parse_version("0.3.10")
    for bad in ("v1.0", "1.0.0-rc1", "latest", "", None, "v1.2.3.4", " v1.x.3"):
        assert update.parse_version(bad) is None, bad
    assert update.newer("v0.3.10", "0.3.9") and update.newer("v1.0.0", "0.99.99")
    assert not update.newer("v0.3.6", "0.3.6") and not update.newer("v0.3.5", "0.3.6")
    assert not update.newer(None, "0.3.6") and not update.newer("garbage", "0.3.6")


def test_check_picks_the_newest_tag_and_ignores_others():
    text = ls_remote("v0.3.9", "v0.3.10", "v0.2.99", "v1.0.0-rc1", "latest", "v0.4", "x0.5.0") + \
        "deadbeef\trefs/heads/v9.9.9\n"
    tags = update.parse_tags(text)
    assert tags == ["v0.2.99", "v0.3.9", "v0.3.10"]
    assert update.newest(tags) == "v0.3.10" and update.newest([]) is None
    assert update.install_tag(tags, "0.3.9") == "v0.3.9"  # its own tag
    assert update.install_tag(tags, "0.3.11") == "v0.3.10"  # untagged: the newest


def test_check_records_latest_and_its_changelog(aw_home, tools):
    up = make(aw_home, version="0.3.6")
    v = up.check()
    assert tools.names() == ["ls-remote", "init", "fetch v0.3.7", "show v0.3.7:CHANGELOG.md"]
    assert v["latest"] == "v0.3.7" and v["available"] is True
    assert v["notes"] == "- Seven.\n- More seven." and v["notes_tag"] == "v0.3.7"
    assert v["last_error"] is None and "v0.3.7 is available" in v["last_result"]
    tools.calls.clear()
    up.check()  # the notes are kept: no second fetch
    assert tools.names() == ["ls-remote"]


def test_check_up_to_date_and_failure(aw_home, tools):
    up = make(aw_home, version="0.3.7")
    v = up.check()
    assert v["available"] is False and v["notes"] is None and v["last_result"] == "up to date (0.3.7)"
    tools.fail["ls-remote"] = update.Result(128, "", "fatal: unable to access 'https://github.com/...': "
                                                     "Could not resolve host: github.com")
    v = up.check()  # never raises
    assert "Could not resolve host" in v["last_error"] and v["latest"] == "v0.3.7"


def test_repo_url_from_config_and_bad_values():
    from afterword.daemon.config import DEFAULT_CONFIG_TOML, build_config
    import tomllib

    cfg, errors = build_config(tomllib.loads(DEFAULT_CONFIG_TOML))
    assert cfg.update == update.UpdateSettings() and errors == []
    assert cfg.update.mode == "auto" and cfg.update.check_every_h == 6 and cfg.update.repo == update.DEFAULT_REPO
    cfg, errors = build_config({"update": {"mode": "notify", "check_every_h": 12, "repo": "file:///tmp/aw.git"}})
    assert cfg.update == update.UpdateSettings("notify", 12.0, "file:///tmp/aw.git") and errors == []
    cfg, errors = build_config({"update": {"mode": "always", "check_every_h": 0.5, "repo": "--upload-pack=x",
                                           "extra": 1}})
    assert cfg.update == update.UpdateSettings()
    assert len(errors) == 4 and any("check_every_h" in e and "out of range" in e for e in errors)


# --------------------------------------------------------------------------- #
# Prepare                                                                     #
# --------------------------------------------------------------------------- #


def test_prepare_builds_a_tested_release(aw_home, tools):
    up = make(aw_home)
    dest = up.prepare("v0.3.7")
    assert dest == aw_home / "app" / "releases" / "v0.3.7"
    assert tools.names() == ["init", "fetch v0.3.7", "rev-parse v0.3.7", "merge-base v0.3.7", "worktree prune",
                             "worktree add v0.3.7", "uv sync v0.3.7", "version v0.3.7", "import v0.3.7"]
    calls = dict(tools.calls)
    assert calls["uv sync v0.3.7"] == ["/fake/bin/uv", "sync", "--frozen", "--no-dev", "--extra", "qwen"]
    assert calls["worktree add v0.3.7"][-1] == tools.sha("v0.3.7")  # the commit that was checked
    assert calls["version v0.3.7"][0] == str(dest / ".venv" / "bin" / "python")
    marker = json.loads((dest / update.READY_MARKER).read_text())
    assert marker["tag"] == "v0.3.7" and marker["extras"] == ["qwen"] and marker["commit"] == tools.sha("v0.3.7")
    assert up.layout.ready() == ["v0.3.7"]
    tools.calls.clear()
    up.prepare("v0.3.7")  # ready with the same extras, at the tag's commit: reused as it is
    assert tools.names() == ["rev-parse v0.3.7", "head v0.3.7"]


def test_prepare_without_uv_or_git_says_so(aw_home, tools):
    up = make(aw_home)
    tools.paths.pop("uv")
    with pytest.raises(update.UpdateError, match="uv was not found"):
        up.prepare("v0.3.7")
    tools.paths.pop("git")
    with pytest.raises(update.UpdateError, match="git was not found"):
        up.prepare("v0.3.7")
    assert tools.calls == []


def test_prepare_failure_keeps_the_old_release(aw_home, tools):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    tools.fail["sync:v0.3.7"] = update.Result(2, "", "error: Failed to download `mlx==9`\n  network down")
    with pytest.raises(update.UpdateError, match="(?s)uv sync for v0.3.7 failed: .*network down"):
        up.prepare("v0.3.7")
    assert up.layout.current_tag() == "v0.3.6" and up.layout.ready() == ["v0.3.6"]
    # The next try reuses the worktree and syncs again.
    tools.fail.clear()
    tools.calls.clear()
    up.prepare("v0.3.7")
    assert "worktree add v0.3.7" not in tools.names() and "uv sync v0.3.7" in tools.names()


@pytest.mark.parametrize("broken", ["version", "import"])
def test_smoke_test_failure(aw_home, tools, broken):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    if broken == "version":
        tools.versions["v0.3.7"] = "afterword 0.3.6"  # a tag whose code says another version
        match = r"--version` gave 'afterword 0.3.6', not 'afterword 0.3.7'"
    else:
        tools.fail["import:v0.3.7"] = update.Result(1, "", "Traceback ...\nModuleNotFoundError: No module named 'x'")
        match = "(?s)importing the daemon failed: .*No module named 'x'"
    with pytest.raises(update.UpdateError, match=match):
        up.prepare("v0.3.7")
    assert up.layout.marker("v0.3.7") is None and up.layout.current_tag() == "v0.3.6"


def test_prepare_refuses_while_another_update_runs(aw_home, tools):
    up = make(aw_home)
    with update._flock(up.layout.run_lock):
        # A second open file description: as another process would hold it.
        with pytest.raises(update.UpdateError, match="another update is in progress"):
            up.prepare("v0.3.7")


# --------------------------------------------------------------------------- #
# Switch                                                                      #
# --------------------------------------------------------------------------- #


def test_symlink_swap_is_atomic_and_relative(aw_home, tools, monkeypatch):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src).name, os.readlink(src) if os.path.islink(src) else None, Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(update.os, "replace", spy)
    assert up.switch("v0.3.7") == "v0.3.6"
    swaps = [r for r in replaced if r[2] == up.layout.current]
    assert len(swaps) == 1 and swaps[0][0].startswith(".current.") and swaps[0][1] == "releases/v0.3.7"
    assert os.readlink(up.layout.current) == "releases/v0.3.7"
    assert (up.layout.current / ".venv" / "bin" / "python").exists()
    assert not list(up.layout.app.glob(".current.*"))
    st = update.read_state(aw_home)
    assert st["pending"] is True and st["attempts"] == 0
    assert st["previous"] == "v0.3.6" and st["current"] == "v0.3.7" and st["switched_at"]


def test_switch_keeps_the_newest_two_releases(aw_home, tools):
    tools.tags = ["v0.3.6", "v0.3.7", "v0.3.8", "v0.3.9"]
    up = make(aw_home)
    install_release(up, "v0.3.6")
    for tag in ("v0.3.7", "v0.3.8", "v0.3.9"):
        up.prepare(tag)
        up.switch(tag)
        up.version = tag[1:]  # each one confirmed before the next
        assert up.confirm()
    assert sorted(p.name for p in up.layout.releases.iterdir()) == ["v0.3.8", "v0.3.9"]
    assert "worktree remove v0.3.6" in tools.names() and "worktree remove v0.3.7" in tools.names()
    with pytest.raises(update.UpdateError, match="not prepared"):
        up.switch("v0.3.5")


# --------------------------------------------------------------------------- #
# The daemon's loop: idle wait, switch, restart                               #
# --------------------------------------------------------------------------- #


def daemon_updater(home, tools, clock, idle=lambda: True, **kw):
    restarts, notes = [], []
    up = make(home, clock=clock, mono=clock.mono, sleep=clock.sleep, idle=idle, restart=lambda: restarts.append(clock()),
              note=lambda s, r: notes.append((s, r)), **kw)
    return up, restarts, notes


def test_auto_switches_when_idle(aw_home, tools):
    clock = Clock()
    up, restarts, notes = daemon_updater(aw_home, tools, clock)
    install_release(up, "v0.3.6")
    asyncio.run(up.tick())
    assert up.layout.current_tag() == "v0.3.7" and restarts and up.phase == "restarting"
    assert update.read_state(aw_home)["pending"] is True
    # It waited for IDLE_SETTLE_S of idle, in IDLE_POLL_S steps.
    assert sum(clock.slept) >= update.IDLE_SETTLE_S and set(clock.slept) == {update.IDLE_POLL_S}
    assert notes[-1] == ("info", "update: switching from v0.3.6 to v0.3.7; restarting")


def test_not_idle_waits_then_tries_at_the_next_check(aw_home, tools):
    clock = Clock()
    busy = {"on": True}
    up, restarts, notes = daemon_updater(aw_home, tools, clock, idle=lambda: not busy["on"])
    install_release(up, "v0.3.6")
    asyncio.run(up.tick())
    assert restarts == [] and up.layout.current_tag() == "v0.3.6" and up.phase == "idle"
    assert sum(clock.slept) >= update.IDLE_WAIT_S
    st = update.read_state(aw_home)
    assert "waiting for a moment with nothing to say" in st["last_result"] and not st.get("pending")
    assert up.layout.ready() == ["v0.3.6", "v0.3.7"]  # prepared and kept for the next try
    busy["on"] = False
    tools.calls.clear()
    asyncio.run(up.tick())
    assert restarts and up.layout.current_tag() == "v0.3.7"
    assert "uv sync v0.3.7" not in tools.names()  # the prepared release was reused


def test_idle_must_hold_for_the_settle_time(aw_home, tools):
    clock = Clock()
    answers = iter([True, True, False] + [True] * 10)
    up, _, _ = daemon_updater(aw_home, tools, clock, idle=lambda: next(answers))
    assert asyncio.run(up.wait_idle(600)) is True
    # 2 idle polls, a busy one, then IDLE_SETTLE_S of idle again.
    assert len(clock.slept) == 2 + 1 + int(update.IDLE_SETTLE_S / update.IDLE_POLL_S)


def test_daemon_idle():
    class Q(list):
        now_playing = None

    class Lock:
        def __init__(self, held=False):
            self.held = held

        def locked(self):
            return self.held

    class D:
        _stopping = False
        _current = None
        queue = Q()
        engine = type("E", (), {"_busy": False, "_pending": 0})()
        lab = type("L", (), {"_say_lock": Lock(), "_stability_lock": Lock(),
                             "explorer": type("X", (), {"x": None})()})()

    d = D()
    assert update.daemon_idle(d)
    d.queue.append("line")
    assert not update.daemon_idle(d)
    d.queue.clear()
    d.engine._pending = 1
    assert not update.daemon_idle(d)
    d.engine._pending = 0
    d.lab._say_lock.held = True
    assert not update.daemon_idle(d)
    d.lab._say_lock.held = False
    d.lab.explorer.x = type("Take", (), {"rendering": 7})()
    assert not update.daemon_idle(d)
    d.lab.explorer.x.rendering = None
    d._stopping = True
    assert not update.daemon_idle(d)


def test_prepare_failure_in_the_loop_is_recorded_and_retried_later(aw_home, tools):
    clock = Clock()
    up, restarts, notes = daemon_updater(aw_home, tools, clock)
    install_release(up, "v0.3.6")
    tools.fail["sync:v0.3.7"] = update.Result(1, "", "boom")
    asyncio.run(up.tick())
    assert restarts == [] and up.layout.current_tag() == "v0.3.6" and up.phase == "idle"
    st = update.read_state(aw_home)
    assert st["last_result"] == "could not prepare v0.3.7; staying on 0.3.6" and "boom" in st["last_error"]
    assert notes[-1][0] == "error"


def test_run_loop_schedule_and_modes(aw_home, tools):
    """First check 5 min after the start, then every check_every_h; notify
    checks but never prepares; off never checks."""
    for mode, expect_checks in (("auto", 3), ("notify", 3), ("off", 0)):
        tools.calls.clear()
        clock = Clock()
        t0 = clock.t
        clock.until = t0 + update.FIRST_CHECK_S + 2 * 6 * 3600 + 30
        checks = []
        def runner(*a, _checks=checks, _clock=clock, **k):
            _checks.append(_clock())
            return tools(*a, **k)
        up, restarts, _ = daemon_updater(aw_home, tools, clock, mode=mode, version="0.3.7", runner=runner)
        with pytest.raises(_Stop):
            asyncio.run(up.run_loop())
        assert len(checks) == expect_checks, mode
        if checks:
            assert checks[0] == t0 + update.FIRST_CHECK_S
            assert checks[1] - checks[0] == checks[2] - checks[1] == 6 * 3600
        assert restarts == []


def test_a_shorter_check_every_h_applies_to_the_wait_under_way(aw_home, tools):
    """Lowering check_every_h (config.toml is hot-reloaded) moves the next
    check forward instead of waiting out the old interval."""
    clock = Clock()
    t0 = clock.t
    cfg = {"s": update.UpdateSettings(mode="notify", check_every_h=24)}
    checks = []

    def runner(*a, **k):
        if a and "ls-remote" in [str(x) for x in a[0]]:
            checks.append(clock())
            if len(checks) == 1:
                cfg["s"] = update.UpdateSettings(mode="notify", check_every_h=1)  # changed after the first
        return tools(*a, **k)

    clock.until = t0 + update.FIRST_CHECK_S + 2 * 3600 + 120
    up, _, _ = daemon_updater(aw_home, tools, clock, mode="notify", version="0.3.7", runner=runner,
                              settings=lambda: cfg["s"])
    with pytest.raises(_Stop):
        asyncio.run(up.run_loop())
    assert len(checks) == 3, checks  # 5 min in, then hourly, not 24 h later
    assert checks[1] - checks[0] <= 3600 + 60 and checks[2] - checks[1] <= 3600 + 60


def test_notify_mode_only_reports(aw_home, tools):
    clock = Clock()
    up, restarts, _ = daemon_updater(aw_home, tools, clock, mode="notify")
    install_release(up, "v0.3.6")
    tools.calls.clear()
    asyncio.run(up.tick())
    assert up.view()["available"] and restarts == []
    assert not any(n.startswith(("uv sync", "worktree add")) for n in tools.names())


@pytest.mark.parametrize("kind,as_service,why", [("dev", True, "development checkout"),
                                                 ("managed", False, "started by hand")])
def test_dev_or_manual_daemons_only_report(aw_home, tools, kind, as_service, why):
    clock = Clock()
    up, restarts, _ = daemon_updater(aw_home, tools, clock, kind=kind, service_=as_service)
    asyncio.run(up.tick())
    v = up.view()
    assert v["available"] and why in v["apply_blocked"] and restarts == []
    assert "uv sync v0.3.7" not in tools.names()

    async def now():
        up.start_apply()

    with pytest.raises(update.UpdateError, match=why):
        asyncio.run(now())


def test_update_now_forced_does_not_wait(aw_home, tools):
    clock = Clock()
    up, restarts, _ = daemon_updater(aw_home, tools, clock, idle=lambda: False)
    install_release(up, "v0.3.6")

    async def now():
        tag = up.start_apply(force=True)
        assert up.phase == "checking"  # already, for the page that polls
        await up._apply_task
        return tag

    assert asyncio.run(now()) is None  # nothing checked before: the job checked first
    assert restarts and up.layout.current_tag() == "v0.3.7" and clock.slept == []


def test_update_now_when_up_to_date(aw_home, tools):
    up = make(aw_home, version="0.3.7")
    up.check()

    async def now():
        up.start_apply()

    with pytest.raises(update.UpdateError, match="already up to date"):
        asyncio.run(now())
    assert up._job.acquire(blocking=False)  # released again


# --------------------------------------------------------------------------- #
# Rollback and confirmation                                                   #
# --------------------------------------------------------------------------- #


def switched(aw_home, tools, monkeypatch):
    """v0.3.6 -> v0.3.7 switched and pending; this process is v0.3.7's daemon."""
    up = make(aw_home)
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    up.switch("v0.3.7")
    monkeypatch.setenv("AFTERWORD_SERVICE", "1")
    monkeypatch.setattr(update, "package_dir", lambda: up.layout.releases / "v0.3.7" / "afterword")
    return up


def crash(aw_home):
    """The daemon started by the last startup_check dies without a clean
    shutdown: its daemon.run stays, under a pid that is gone."""
    (aw_home / update.RUN_MARKER_NAME).write_text(json.dumps({"pid": 2 ** 22 + 12345, "started_at": 1}))


def test_rollback_after_three_failed_starts(aw_home, tools, monkeypatch, capsys):
    up = switched(aw_home, tools, monkeypatch)
    execs = []
    # The first start follows the old release's clean exit: not a failure.
    assert update.startup_check(aw_home, exec_fn=lambda *a: execs.append(a), argv=["serve"]) == "clean"
    assert json.loads((aw_home / update.RUN_MARKER_NAME).read_text())["pid"] == os.getpid()
    for n in range(1, 3):
        crash(aw_home)
        assert update.startup_check(aw_home, exec_fn=lambda *a: execs.append(a), argv=["serve"]) == "counted"
        assert update.read_state(aw_home)["attempts"] == n
    assert up.layout.current_tag() == "v0.3.7" and execs == []
    crash(aw_home)
    assert update.startup_check(aw_home, exec_fn=lambda *a: execs.append(a), argv=["serve", "-v"]) == "rolled-back"
    assert up.layout.current_tag() == "v0.3.6"
    py = str(aw_home / "app" / "current" / ".venv" / "bin" / "python")
    assert execs == [(py, [py, "-P", "-m", "afterword.cli", "serve", "-v"])]
    st = update.read_state(aw_home)
    assert not st.get("pending") and st["rolled_back"] == "v0.3.7" and st["current"] == "v0.3.6"
    assert "did not stay up for 60 s in 3 starts" in st["rollback_reason"] and st["rollback_notice"]
    assert "rolling back" in capsys.readouterr().err

    # The old release's daemon reports it once in the event log ...
    notes = []
    old = make(aw_home, version="0.3.6", note=lambda s, r: notes.append((s, r)))
    asyncio.run(old.startup())
    asyncio.run(old.startup())
    assert len(notes) == 1 and notes[0][0] == "error" and "v0.3.7 rolled back" in notes[0][1]
    # ... and does not take v0.3.7 again by itself.
    clock = Clock()
    old, restarts, _ = daemon_updater(aw_home, tools, clock, version="0.3.6")
    tools.calls.clear()
    asyncio.run(old.tick())
    assert restarts == [] and "not retried automatically" in update.read_state(aw_home)["last_result"]
    assert "uv sync v0.3.7" not in tools.names()

    # "Update now" does.
    async def now():
        old.start_apply(force=True)
        await old._apply_task

    asyncio.run(now())
    assert restarts and up.layout.current_tag() == "v0.3.7"
    st = update.read_state(aw_home)
    assert st["pending"] is True and not st.get("rolled_back")


def test_startup_check_does_nothing_outside_the_service(aw_home, tools, monkeypatch):
    up = switched(aw_home, tools, monkeypatch)
    monkeypatch.delenv("AFTERWORD_SERVICE")
    assert update.startup_check(aw_home, exec_fn=None) == "none"
    monkeypatch.setenv("AFTERWORD_SERVICE", "1")
    monkeypatch.setattr(update, "package_dir", lambda: ROOT / "afterword")  # a dev checkout
    assert update.startup_check(aw_home, exec_fn=None) == "none"
    assert update.read_state(aw_home)["attempts"] == 0 and up.layout.current_tag() == "v0.3.7"


def test_serve_runs_the_startup_check_before_the_daemon(monkeypatch):
    seen = []
    monkeypatch.setattr(update, "startup_check", lambda: seen.append("check"))
    from afterword.daemon import server

    monkeypatch.setattr(server, "main", lambda **kw: seen.append("serve") or 0)
    assert cli.main(["serve", "--no-http"]) == 0
    assert seen == ["check", "serve"]


def write_plugin_records(tmp_path, claude=True, codex=True):
    if claude:
        d = tmp_path / "no-claude-config" / "plugins"
        d.mkdir(parents=True)
        (d / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {
            "afterword@afterword": [{"scope": "user", "version": "0.3.0"}]}}))
    if codex:
        d = tmp_path / "no-codex-home"
        d.mkdir(parents=True)
        (d / "config.toml").write_text('[plugins."afterword@afterword"]\nenabled = true\n')


def test_confirmation_after_60_s_then_plugins(aw_home, tools, monkeypatch, tmp_path):
    switched(aw_home, tools, monkeypatch)
    write_plugin_records(tmp_path)
    clock = Clock()
    up, _, notes = daemon_updater(aw_home, tools, clock, version="0.3.7")
    tools.calls.clear()
    asyncio.run(up.startup())
    assert sum(clock.slept) == update.CONFIRM_AFTER_S
    st = update.read_state(aw_home)
    assert not st.get("pending") and st["confirmed_at"] == clock.t
    assert st["last_result"] == "updated to v0.3.7 from v0.3.6"
    assert tools.names() == ["claude plugin marketplace update afterword", "claude plugin update afterword@afterword",
                             "codex plugin marketplace upgrade afterword", "codex plugin add afterword@afterword"]
    assert st["plugins"]["claude"]["ok"] is True and "restart open Claude Code sessions" in st["plugins"]["claude"]["message"]
    assert st["plugins"]["codex"]["ok"] is True and not st.get("plugins_pending")
    assert notes[0] == ("info", "updated to v0.3.7 from v0.3.6") and notes[1][1].startswith("plugins updated")


def test_no_confirmation_when_stopping_first(aw_home, tools, monkeypatch):
    switched(aw_home, tools, monkeypatch)
    clock = Clock()
    up, _, _ = daemon_updater(aw_home, tools, clock, version="0.3.7", serving=lambda: False)
    asyncio.run(up.startup())
    assert update.read_state(aw_home)["pending"] is True


def test_plugin_refresh_tolerates_failures_and_missing_tools(aw_home, tools, tmp_path):
    write_plugin_records(tmp_path)
    up = make(aw_home)
    tools.fail["claude marketplace"] = update.Result(1, "", "network down")
    res = up.refresh_plugins()
    assert res["claude"]["ok"] is False and "network down" in res["claude"]["message"]
    assert res["codex"]["ok"] is True
    assert "claude plugin update afterword@afterword" not in tools.names()  # stopped after the failed step
    tools.paths.pop("codex")
    res = up.refresh_plugins()
    assert res["codex"] == {"ok": None, "message": "codex is not on PATH; skipped"}


def test_plugin_refresh_skips_tools_without_the_plugin(aw_home, tools, tmp_path):
    write_plugin_records(tmp_path, claude=True, codex=False)
    res = make(aw_home).refresh_plugins()
    assert res["codex"]["ok"] is None and "not installed" in res["codex"]["message"]
    assert not any(n.startswith("codex") for n in tools.names())


# --------------------------------------------------------------------------- #
# install-service                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture
def launchd(aw_home, tmp_path, monkeypatch):
    fake = FakeLaunchd()
    monkeypatch.setattr(service, "launchctl", fake)
    monkeypatch.setattr(service, "pid_alive", fake.alive)
    monkeypatch.setenv(service.AGENTS_DIR_ENV, str(tmp_path / "LaunchAgents"))
    return fake


def test_install_service_sets_up_the_managed_install(launchd, tools, aw_home, monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "0.3.99")  # an untagged checkout: the newest release is used
    launchd.on_call = lambda args: tools.calls.append((f"launchctl {args[0]}", list(args)))
    assert cli.main(["install-service"]) == 0
    lay = update.Layout(aw_home)
    assert lay.current_tag() == "v0.3.7"
    plist = plistlib.loads(service.plist_path().read_bytes())
    py = str(aw_home / "app" / "current" / ".venv" / "bin" / "python")
    assert plist["ProgramArguments"] == [py, "-P", "-m", "afterword.cli", "serve"]
    assert plist["EnvironmentVariables"]["PATH"].split(":")[0] == str(aw_home / "app" / "current" / ".venv" / "bin")
    assert launchd.mutations() == [("bootstrap", service.domain(), str(service.plist_path()))]
    # Prepared and tested before launchd was asked anything that changes it.
    names = tools.names()
    assert names.index("uv sync v0.3.7") < names.index("import v0.3.7") < names.index("launchctl bootstrap")
    assert not any(n.startswith("launchctl") for n in names[:names.index("import v0.3.7")])
    st = update.read_state(aw_home)
    assert st["install"] == "managed" and st["uv"] == "/fake/bin/uv" and st["extras"] == ["qwen"]
    assert not st.get("pending")
    assert update.plist_kind(aw_home) == "managed"
    out = capsys.readouterr().out
    assert "releases/v0.3.7" in out and "service started" in out


def test_install_service_uses_its_own_release_tag(launchd, tools, aw_home, monkeypatch):
    monkeypatch.setattr(update, "__version__", "0.3.6")
    up = update.Updater(aw_home, settings=update.UpdateSettings, version="0.3.6")
    assert update.install_managed(updater=up) == 0
    assert update.Layout(aw_home).current_tag() == "v0.3.6"


def test_install_service_failure_touches_nothing(launchd, tools, aw_home, capsys):
    tools.fail["sync:v0.3.7"] = update.Result(1, "", "uv exploded")
    assert cli.main(["install-service"]) == 1
    assert not service.plist_path().exists() and launchd.mutations() == []
    assert update.Layout(aw_home).current_tag() is None
    assert "the service was not touched" in capsys.readouterr().err


def test_a_failed_install_service_leaves_update_json_as_it_was(launchd, tools, aw_home, capsys):
    """Run from a venv without kokoro, a failed install must not leave
    extras = ["qwen"] behind for every later update."""
    update.change_state(aw_home, extras=["kokoro", "qwen"], uv="/old/uv")
    tools.fail["sync:v0.3.7"] = update.Result(1, "", "uv exploded")
    assert cli.main(["install-service"]) == 1
    st = update.read_state(aw_home)
    assert st["extras"] == ["kokoro", "qwen"] and st["uv"] == "/old/uv"
    assert "the service was not touched" in capsys.readouterr().err


def test_state_and_ready_marker_are_written_with_fsync(aw_home, tools, monkeypatch):
    seen = []
    real = update.common.atomic_write
    monkeypatch.setattr(update.common, "atomic_write", lambda p, *a, **k: (seen.append(Path(p).name), real(p, *a, **k))[1])
    up = make(aw_home, tools=tools, version="0.3.7")
    up.prepare("v0.3.7")
    update.change_state(aw_home, note="x")
    assert update.STATE_NAME in seen and update.READY_MARKER in seen


def test_install_service_refuses_a_release_without_the_updater(launchd, tools, aw_home, capsys):
    tools.tags = ["v0.3.3", "v0.3.5"]
    assert cli.main(["install-service"]) == 1
    err = capsys.readouterr().err
    assert "predates the updater" in err and "afterword install-service --dev" in err
    assert not service.plist_path().exists() and launchd.mutations() == []
    assert "uv sync v0.3.5" not in tools.names()


def test_install_service_dry_run_writes_nothing(launchd, tools, aw_home, capsys):
    assert cli.main(["install-service", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "# managed install of v0.3.7" in out and "sync --frozen --no-dev --extra qwen" in out
    assert str(aw_home / "app" / "current" / ".venv" / "bin" / "python") in out
    assert tools.names() == ["ls-remote"] and launchd.mutations() == []
    assert not (aw_home / "app").exists() and not service.plist_path().exists()


def test_install_service_dev_keeps_the_old_behaviour(launchd, tools, aw_home):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    up.switch("v0.3.7")  # pending
    tools.calls.clear()
    assert cli.main(["install-service", "--dev"]) == 0
    plist = plistlib.loads(service.plist_path().read_bytes())
    assert plist["ProgramArguments"] == [os.path.abspath(sys.executable), "-P", "-m", "afterword.cli", "serve"]
    assert tools.calls == []
    st = update.read_state(aw_home)
    assert st["install"] == "dev" and not st.get("pending")
    assert update.plist_kind(aw_home) == "dev"


# --------------------------------------------------------------------------- #
# afterword update / status, without a daemon                                 #
# --------------------------------------------------------------------------- #


def test_update_cli_without_a_daemon(launchd, tools, aw_home, capsys):
    assert cli.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "afterwordd is not running" in out and "development checkout" in out and "not checked" not in out
    assert cli.main(["update", "--check"]) == 0
    out = capsys.readouterr().out
    assert "latest    v0.3.7 (available)" in out
    assert cli.main(["update", "--now"]) == 1  # a dev install: nothing to switch
    assert "not a managed install" in capsys.readouterr().err


def test_update_now_without_a_daemon_switches_the_managed_install(launchd, tools, aw_home, capsys):
    tools.tags = ["v0.3.6"]
    assert cli.main(["install-service", "--no-start"]) == 0
    tools.tags = ["v0.3.6", "v0.3.7"]
    assert cli.main(["update", "--now"]) == 0
    assert "switched from v0.3.6 to v0.3.7" in capsys.readouterr().out
    assert update.Layout(aw_home).current_tag() == "v0.3.7"
    assert update.read_state(aw_home)["pending"] is True  # the next start counts towards a rollback
    assert cli.main(["update", "--now"]) == 0
    assert "already up to date (v0.3.7)" in capsys.readouterr().out


def test_status_line(launchd, aw_home, capsys):
    assert cli.main(["status"]) == 1  # no daemon
    assert "  update    auto, dev checkout; not checked yet, checked never" in capsys.readouterr().out
    v = {"mode": "auto", "install": "managed", "current": "v0.3.6", "latest": "v0.3.7", "available": True,
         "last_check": 0, "phase": "idle"}
    assert update.summary(v).startswith("auto, managed v0.3.6; v0.3.7 available, checked ")
    assert update.summary({**v, "pending": True, "current": "v0.3.7"}) == \
        "auto, managed v0.3.7; switched to v0.3.7, not confirmed yet"
    assert update.summary({**v, "phase": "waiting", "phase_tag": "v0.3.7"}) == "auto, managed v0.3.6; waiting v0.3.7"


# --------------------------------------------------------------------------- #
# HTTP API                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
def gui(aw_home, launchd, tools, monkeypatch):
    from fastapi.testclient import TestClient

    from afterword.daemon import web
    from test_lab import FakeEngine, _daemon

    d = _daemon(aw_home, monkeypatch, FakeEngine())
    # As a context manager: one event loop for all requests, so a background
    # update job outlives the request that started it, as in the daemon.
    with TestClient(web.build_app(d, 47800), base_url="http://127.0.0.1:47800",
                    raise_server_exceptions=False) as client:
        yield d, client


H = {"X-AfterWord": "1"}


def test_api_status_check_and_apply_from_a_dev_daemon(gui, tools):
    d, client = gui
    body = client.get("/api/update").json()
    assert body["install"] == "dev" and body["mode"] == "auto" and body["available"] is False
    assert client.get("/api/status").json()["update"]["install"] == "dev"
    assert client.post("/api/update/check").status_code == 403  # the POST guard
    r = client.post("/api/update/check", headers=H)
    assert r.status_code == 200 and r.json()["latest"] == "v0.3.7" and r.json()["available"] is True
    assert r.json()["phase"] == "idle"  # the check is over by the time it answers
    assert r.json()["notes"] == "- Seven.\n- More seven."
    r = client.post("/api/update/apply", headers=H, json={})
    assert r.status_code == 409 and "development checkout" in r.json()["error"]
    r = client.post("/api/update/apply", headers=H, json={"force": "yes"})
    assert r.status_code == 400


def test_api_apply_on_a_managed_service(gui, tools, monkeypatch):
    d, client = gui
    restarts = []
    up = d.updater
    up._kind = lambda: "managed"
    up._as_service = lambda: True
    up.restart = lambda: restarts.append(1)
    up.version = "0.3.6"
    up.prepare("v0.3.6")
    up.switch("v0.3.6", pending=False)
    r = client.post("/api/update/apply", headers=H, json={"force": True})
    assert r.status_code == 202, r.text
    assert r.json()["started"] is True and r.json()["pid"] == os.getpid()
    assert r.json()["update"]["phase"] == "checking"
    r2 = client.post("/api/update/apply", headers=H, json={"force": True})
    assert r2.status_code == 409 and "already running" in r2.json()["error"]


# --------------------------------------------------------------------------- #
# The GUI's Updates part                                                      #
# --------------------------------------------------------------------------- #

HARNESS = r"""
const src = require("fs").readFileSync(0, "utf8");
const D = new Function(src + "\nreturn {upInstallLine, upModeLine, upLatestLine, upPhaseLine, upApply, upPluginsLine, upFollow};")();
const [fn, args] = JSON.parse(process.argv[1]);
console.log(JSON.stringify(D[fn](...args)));
"""
UI = ROOT / "afterword" / "ui" / "index.html"


def js(fn, *args):
    t = UI.read_text(encoding="utf-8")
    block = re.search(r"// ---------- daemon section\n(.*?)\n// ---------- end daemon section", t, re.S).group(1)
    proc = subprocess.run([NODE, "-e", HARNESS, json.dumps([fn, list(args)])], input=block,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def uview(**over):
    v = {"install": "managed", "service": True, "mode": "auto", "check_every_h": 6, "running": "0.3.6",
         "current": "v0.3.6", "previous": "v0.3.5", "latest": "v0.3.7", "available": True, "last_check": 1,
         "phase": "idle", "phase_tag": None, "pending": False, "rolled_back": None, "apply_blocked": None,
         "plugins": None, "notes": "- Seven.", "notes_tag": "v0.3.7"}
    v.update(over)
    return v


@needs_node
def test_ui_lines_and_buttons():
    assert js("upInstallLine", uview()) == "managed install, v0.3.6 (previous v0.3.5, kept for rollback)"
    assert js("upInstallLine", uview(install="dev")).startswith("development checkout")
    assert js("upModeLine", uview()) == "auto: a new release is installed when nothing is being said; checked every 6 h"
    assert js("upModeLine", uview(mode="off")) == "off: no checks"
    assert js("upLatestLine", uview()) == "v0.3.7 is available (running 0.3.6)"
    assert js("upLatestLine", uview(available=False, latest="v0.3.6")) == "up to date (newest release v0.3.6)"
    assert js("upLatestLine", uview(available=False, latest=None, last_check=None)) == "not checked yet"
    assert js("upPhaseLine", uview()) is None
    assert js("upPhaseLine", uview(phase="preparing", phase_tag="v0.3.7")) == "preparing v0.3.7"
    assert js("upApply", uview())["enabled"] is True
    assert js("upApply", uview(apply_blocked="dev checkout")) == {"enabled": False, "title": "dev checkout"}
    assert js("upApply", uview(available=False))["enabled"] is False
    assert js("upApply", uview(phase="waiting"))["enabled"] is False
    # A scheduled update waiting for quiet: the button switches to it now.
    take = js("upApply", uview(phase="waiting", phase_tag="v0.3.7", scheduled=True))
    assert take["enabled"] is True and take["force"] is True and take["label"] == "Switch to v0.3.7 now"
    assert js("upApply", uview(phase="preparing", phase_tag="v0.3.7", scheduled=True))["force"] is True
    assert js("upApply", uview(phase="switching", phase_tag="v0.3.7", scheduled=False))["enabled"] is False
    assert "rolled back before" in js("upApply", uview(rolled_back="v0.3.7", rollback_reason="crashed"))["title"]
    plugins = {"claude": {"ok": True, "message": "updated; restart open Claude Code sessions to load the new hooks"},
               "codex": {"ok": None, "message": "codex is not on PATH; skipped"}}
    assert js("upPluginsLine", uview(plugins=plugins)) == \
        "Claude Code: updated; restart open Claude Code sessions to load the new hooks; Codex: codex is not on PATH; skipped"


@needs_node
def test_ui_follow():
    assert js("upFollow", 10, None) == {"done": False, "ok": False, "kind": "run", "text": "restarting…"}
    assert js("upFollow", 10, {"pid": 11, "version": "0.3.7"})["ok"] is True
    rolled = js("upFollow", 10, {"pid": 12, "version": "0.3.6",
                                  "update": uview(rolled_back="v0.3.7", rollback_reason="crashed")}, "v0.3.7")
    assert rolled["ok"] is False and rolled["kind"] == "err" and "rolled back: crashed" in rolled["text"]
    assert js("upFollow", 10, {"pid": 10, "update": uview(phase="preparing", phase_tag="v0.3.7")})["text"] == \
        "preparing v0.3.7…"
    stopped = js("upFollow", 10, {"pid": 10, "update": uview(last_result="could not prepare v0.3.7", last_error="boom")})
    assert stopped["done"] is True and stopped["ok"] is False and stopped["text"] == "could not prepare v0.3.7: boom"


def test_ui_wiring():
    t = UI.read_text(encoding="utf-8")
    panel = re.search(r"const UpdatePanel = \(\(\) => \{(.*?)\n\}\)\(\);", t, re.S).group(1)
    for call in ('api("GET", "/api/update")', 'api("POST", "/api/update/check")',
                 'api("POST", "/api/update/apply", ap.force ? {force: true} : {})'):
        assert call in panel, call
    for el in ("up-check", "up-apply", "up-meta", "up-msg", "up-notes", "up-notesbox"):
        assert f'id="{el}"' in t, el
    section = t[t.index('id="cfg-daemon"'):t.index('id="cfg-file"')]
    assert 'id="dm-update"' in section  # inside the Daemon section
    assert t.count("UpdatePanel.load();") == 2  # loaded and polled with the Config page
    assert not re.search(r"<[^>]+\son[a-z]+=", section)  # no inline handlers (CSP)
    assert "restart" in section and "Claude Code" in section


# --------------------------------------------------------------------------- #
# The release workflow                                                        #
# --------------------------------------------------------------------------- #


def test_release_workflow_is_valid():
    yaml = pytest.importorskip("yaml")
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads a bare `on` key as True
    assert on["push"]["branches"] == ["main"] and "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "write"}
    steps = wf["jobs"]["tag"]["steps"]
    text = json.dumps(steps)
    assert "python3 -m afterword.changelog" in text and "gh release create" in text
    assert "--target \\\"$GITHUB_SHA\\\"" in text and "git ls-remote --exit-code --tags origin" in text


def test_update_cli_through_a_running_daemon(monkeypatch, capsys):
    calls = []
    view = {"install": "dev", "mode": "auto", "running": "0.3.6", "latest": "v0.3.7", "available": True,
            "last_check": 1, "phase": "idle", "apply_blocked": "this daemon runs from a development checkout"}
    answers = {("POST", "/api/update/check"): (200, view), ("GET", "/api/update"): (200, view),
               ("POST", "/api/update/apply"): (409, {"error": view["apply_blocked"]})}

    def http(base, method, path, body=None, timeout=120.0):
        calls.append((method, path, body))
        return answers[(method, path)]

    monkeypatch.setattr(cli, "_daemon_status", lambda: {"http": "http://127.0.0.1:47800", "pid": 7, "version": "0.3.6"})
    monkeypatch.setattr(cli, "_http", http)
    assert cli.main(["update"]) == 0
    assert "latest    v0.3.7 (available)" in capsys.readouterr().out
    assert cli.main(["update", "--now", "--wait", "30"]) == 1
    assert "not updated: this daemon runs from a development checkout" in capsys.readouterr().err
    assert calls[-1] == ("POST", "/api/update/apply", {"force": False, "wait_s": 30.0})
    answers[("GET", "/api/update")] = (404, {"error": "Not Found"})
    assert cli.main(["update"]) == 1
    assert "has no updater" in capsys.readouterr().err


def _follow(monkeypatch, statuses, tag="v0.3.7", wait=30.0):
    """Run cli._follow_update against a scripted sequence of daemon statuses
    (None = not answering), without sleeping."""
    seq = iter(statuses)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_daemon_status", lambda: next(seq))
    args = SimpleNamespace(force=False, wait=wait)
    return cli._follow_update("http://127.0.0.1:47800", 7, tag, args)


def st(pid, phase, **update):
    return {"pid": pid, "version": "0.3.7" if pid != 7 else "0.3.6", "update": {"phase": phase, **update}}


def test_update_now_succeeds_when_launchd_restarts_it_between_polls(monkeypatch, capsys):
    """launchd relaunches a daemon that ran over 10 s at once, so the next
    poll already reaches the new one, idle: that is success, not a failure."""
    rc = _follow(monkeypatch, [st(7, "waiting"), st(7, "restarting"), st(8, "idle")])
    assert rc == 0
    out = capsys.readouterr()
    assert "afterwordd 0.3.7 is running, pid 8" in out.out and out.err == ""


def test_update_now_follows_a_restart_with_a_gap(monkeypatch, capsys):
    assert _follow(monkeypatch, [st(7, "switching"), None, None, st(9, "idle")]) == 0
    assert "pid 9" in capsys.readouterr().out


def test_update_now_reports_a_failure_from_the_old_daemon(monkeypatch, capsys):
    rc = _follow(monkeypatch, [st(7, "preparing"), st(7, "idle", last_result="not updated", last_error="uv failed")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not updated" in err and "uv failed" in err


def test_update_now_reports_a_rollback(monkeypatch, capsys):
    rc = _follow(monkeypatch, [st(7, "restarting"), None,
                               st(8, "idle", rolled_back="v0.3.7", rollback_reason="crashed at start")])
    assert rc == 1
    assert "v0.3.7 did not stay up and was rolled back: crashed at start" in capsys.readouterr().err


def test_changelog_section():
    assert section(CHANGELOG, "0.3.6") == "- Six."
    assert section(CHANGELOG, "v0.3.7") == "- Seven.\n- More seven."
    assert section(CHANGELOG, "0.3.5") == "- Five."  # the last one runs to the end
    assert section(CHANGELOG, "0.3.8") is None and section(CHANGELOG, "0.3") is None
    assert section("## 1.0.0 (x)\n\n## 0.9.0\n- a\n", "1.0.0") is None  # empty


def test_changelog_inline_triple_backticks_are_not_a_fence():
    text = "## 0.3.7 (x)\n\n```inline``` code in a line\n- Seven.\n\n## 0.3.6 (y)\n\n- Six.\n"
    assert section(text, "0.3.7") == "```inline``` code in a line\n- Seven."
    assert section(text, "0.3.6") == "- Six."


def test_a_broken_repo_store_is_started_again(aw_home, tools):
    """repo.git without HEAD (an interrupted first run) must not make every
    later prepare fail on the rename."""
    up = make(aw_home, tools=tools, version="0.3.7")
    up.layout.repo.mkdir(parents=True)
    (up.layout.repo / "leftover").write_text("x")
    up.prepare("v0.3.7")
    assert up.layout.marker("v0.3.7") and not (up.layout.repo / "leftover").exists()


def test_changelog_script_as_the_workflow_runs_it(tmp_path):
    from afterword import __version__

    p = subprocess.run([sys.executable, "-m", "afterword.changelog", __version__, "CHANGELOG.md"], cwd=ROOT,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    real = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert p.stdout.strip() == section(real, __version__) and p.stdout.strip()
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    p = subprocess.run([sys.executable, "-m", "afterword.changelog", "9.9.9", str(tmp_path / "CHANGELOG.md")],
                       cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 1 and "no (or an empty) section for 9.9.9" in p.stderr and p.stdout == ""


# --------------------------------------------------------------------------- #
# Where tags come from, and the commit a release is at                        #
# --------------------------------------------------------------------------- #


def test_prepare_refuses_a_tag_that_is_not_on_main(aw_home, tools):
    up = make(aw_home)
    tools.off_main = {"v0.3.7"}
    with pytest.raises(update.UpdateError, match=r"v0.3.7 \([0-9a-f]{12}\) is not on main"):
        up.prepare("v0.3.7")
    assert not any(n.startswith(("worktree add", "uv sync", "version")) for n in tools.names())
    assert up.layout.marker("v0.3.7") is None


def test_a_moved_tag_is_checked_out_again(aw_home, tools):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    tools.fail["sync:v0.3.7"] = update.Result(1, "", "network down")
    with pytest.raises(update.UpdateError):
        up.prepare("v0.3.7")  # half prepared: a worktree at the first commit, no marker
    tools.fail.clear()
    tools.commits["v0.3.7"] = "7" * 40  # the maintainer moved the tag
    tools.calls.clear()
    up.prepare("v0.3.7")
    names = tools.names()
    assert names.index("worktree remove v0.3.7") < names.index("worktree add v0.3.7") < names.index("uv sync v0.3.7")
    assert dict(tools.calls)["worktree add v0.3.7"][-1] == "7" * 40
    assert up.layout.marker("v0.3.7")["commit"] == "7" * 40
    # Prepared, then moved again: not reused, made again at the new commit.
    tools.commits["v0.3.7"] = "8" * 40
    tools.calls.clear()
    up.prepare("v0.3.7")
    assert "uv sync v0.3.7" in tools.names() and up.layout.marker("v0.3.7")["commit"] == "8" * 40


def test_switch_refuses_a_release_not_at_its_checked_commit(aw_home, tools):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    (up.layout.release("v0.3.7") / ".fake-head").write_text("9" * 40)  # someone checked out another commit
    with pytest.raises(update.UpdateError, match="not at the commit"):
        up.switch("v0.3.7")
    assert up.layout.current_tag() == "v0.3.6" and not update.read_state(aw_home).get("pending")


# --------------------------------------------------------------------------- #
# The running release and the rollback target are never synced again         #
# --------------------------------------------------------------------------- #


def test_install_service_never_resyncs_the_running_release(launchd, tools, aw_home, monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "0.3.6")
    up = make(aw_home)
    assert update.install_managed(updater=up) == 0
    assert up.layout.marker("v0.3.6")["extras"] == ["qwen"]
    # The checkout gained kokoro; install-service again, with uv failing if it ran.
    up = make(aw_home, extras=lambda: ["kokoro", "qwen"])
    tools.fail["sync:v0.3.6"] = update.Result(1, "", "uv must not run here")
    tools.calls.clear()
    capsys.readouterr()
    assert update.install_managed(updater=up) == 0
    assert not any(n.startswith(("uv sync", "worktree")) for n in tools.names())
    assert up.layout.marker("v0.3.6")["extras"] == ["qwen"] and up.layout.current_tag() == "v0.3.6"
    assert "kokoro, qwen come with the next release" in capsys.readouterr().out
    assert update.read_state(aw_home)["extras"] == ["kokoro", "qwen"]
    # ... which they do.
    tools.fail.clear()
    up.prepare("v0.3.7")
    assert dict(tools.calls)["uv sync v0.3.7"][-4:] == ["--extra", "kokoro", "--extra", "qwen"]


def test_prepare_never_resyncs_the_rollback_release(aw_home, tools):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    up.switch("v0.3.7")
    up.change(extras=["chatterbox", "qwen"])
    tools.calls.clear()
    assert up.prepare("v0.3.6") == up.layout.release("v0.3.6")
    assert up.prepare("v0.3.7") == up.layout.release("v0.3.7")
    assert not any(n.startswith(("uv sync", "worktree", "fetch")) for n in tools.names())
    assert up.layout.ready() == ["v0.3.6", "v0.3.7"]


# --------------------------------------------------------------------------- #
# Kokoro's UniDic dictionary                                                  #
# --------------------------------------------------------------------------- #


def unidic_src(tmp_path):
    d = tmp_path / "checkout-venv" / "unidic" / "dicdir"
    d.mkdir(parents=True)
    (d / "dicrc").write_text("dicrc")
    (d / "sys.dic").write_bytes(b"\0" * 64)
    return d


@pytest.mark.parametrize("clone", [True, False])
def test_prepare_carries_the_unidic_dictionary_over(aw_home, tools, tmp_path, clone):
    src = unidic_src(tmp_path)
    if not clone:
        tools.fail["cp"] = update.Result(1, "", "cp: clonefile failed: Cross-device link")
    up = make(aw_home, extras=lambda: ["kokoro", "qwen"], unidic=lambda: src)
    dest = up.prepare("v0.3.7")
    target = dest / ".venv" / "lib" / "python3.12" / "site-packages" / "unidic" / "dicdir"
    assert (target / "dicrc").read_text() == "dicrc" and (target / "sys.dic").stat().st_size == 64
    assert "cp" in tools.names() and dict(tools.calls)["cp"][:3] == ["/bin/cp", "-c", "-R"]
    assert up.layout.marker("v0.3.7")["unidic"] == "copied" and up.unidic_warning("v0.3.7") is None
    assert not list(target.parent.glob(".dicdir.*"))


def test_prepare_says_when_there_is_no_unidic_dictionary(launchd, tools, aw_home, monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "0.3.6")
    up = make(aw_home, extras=lambda: ["kokoro"], unidic=lambda: None)
    assert update.install_managed(updater=up) == 0
    assert up.layout.marker("v0.3.6")["unidic"] == "missing"
    out = capsys.readouterr().out
    py = up.layout.release("v0.3.6") / ".venv" / "bin" / "python"
    assert f"note: v0.3.6 has no UniDic dictionary, so Kokoro cannot speak Japanese: run `{py} -m unidic download`" in out
    # Without kokoro nothing is looked for.
    up.change(extras=["qwen"])
    up = make(aw_home, unidic=lambda: pytest.fail("looked for UniDic without kokoro"))
    up.prepare("v0.3.7")
    assert "unidic" not in up.layout.marker("v0.3.7")


# --------------------------------------------------------------------------- #
# Switching while an update is pending; a failed swap                         #
# --------------------------------------------------------------------------- #


def test_switch_while_pending_keeps_the_last_confirmed_release(aw_home, tools):
    tools.tags = ["v0.3.6", "v0.3.7", "v0.3.8", "v0.3.9"]
    up = make(aw_home)
    install_release(up, "v0.3.6")  # confirmed
    up.prepare("v0.3.7")
    up.switch("v0.3.7")  # pending, crash-looping say
    up.prepare("v0.3.8")
    assert up.switch("v0.3.8") == "v0.3.7"
    st = update.read_state(aw_home)
    assert st["previous"] == "v0.3.6" and st["current"] == "v0.3.8" and st["pending"] is True
    assert up.layout.ready() == ["v0.3.6", "v0.3.7", "v0.3.8"]  # v0.3.7 still runs until the restart
    up.prepare("v0.3.9")
    up.switch("v0.3.9")
    assert update.read_state(aw_home)["previous"] == "v0.3.6"
    assert up.layout.ready() == ["v0.3.6", "v0.3.8", "v0.3.9"]


def test_a_failed_swap_leaves_the_state_as_it_was(aw_home, tools, monkeypatch):
    up = make(aw_home)
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    before = update.read_state(aw_home)

    def broken(layout, tag):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(update, "swap_current", broken)
    with pytest.raises(update.UpdateError, match="could not point .* at releases/v0.3.7: .*No space left"):
        up.switch("v0.3.7")
    after = update.read_state(aw_home)
    assert after == before and not after.get("pending") and up.layout.current_tag() == "v0.3.6"


def test_startup_check_clears_a_pending_update_that_is_not_running(aw_home, tools, monkeypatch):
    up = switched(aw_home, tools, monkeypatch)
    # As if the swap never happened: v0.3.6 still runs, update.json says v0.3.7.
    monkeypatch.setattr(update, "package_dir", lambda: up.layout.releases / "v0.3.6" / "afterword")
    crash(aw_home)
    assert update.startup_check(aw_home, exec_fn=lambda *a: pytest.fail("rolled back")) == "stale"
    st = update.read_state(aw_home)
    assert not st.get("pending") and not st.get("attempts") and not st.get("rolled_back")
    for _ in range(4):
        crash(aw_home)
        assert update.startup_check(aw_home, exec_fn=lambda *a: pytest.fail("rolled back")) == "none"


# --------------------------------------------------------------------------- #
# Which starts count towards a rollback                                       #
# --------------------------------------------------------------------------- #


def test_clean_restarts_do_not_count_towards_a_rollback(aw_home, tools, monkeypatch):
    up = switched(aw_home, tools, monkeypatch)
    for _ in range(6):  # afterword restart, the GUI's Restart, logout/login, install-service's reload
        assert update.startup_check(aw_home, exec_fn=lambda *a: pytest.fail("rolled back")) == "clean"
        (aw_home / update.RUN_MARKER_NAME).unlink()  # that daemon then shut down cleanly
    st = update.read_state(aw_home)
    assert st["pending"] is True and st["attempts"] == 0 and up.layout.current_tag() == "v0.3.7"
    # A marker of a daemon that is running right now is not a crash either.
    (aw_home / update.RUN_MARKER_NAME).write_text(json.dumps({"pid": os.getppid()}))
    assert update.startup_check(aw_home, exec_fn=lambda *a: pytest.fail("rolled back")) == "clean"
    # An unreadable one is.
    (aw_home / update.RUN_MARKER_NAME).write_text("{not json")
    assert update.startup_check(aw_home, exec_fn=lambda *a: pytest.fail("rolled back")) == "counted"


def test_the_run_marker_is_the_daemons_and_stays_after_an_exception(aw_home, monkeypatch):
    from afterword.daemon import server

    assert update.RUN_MARKER_NAME == server.RUN_MARKER

    def boom(events):
        raise RuntimeError("the new release fails at startup")

    monkeypatch.setattr(server, "ingest_spool", boom)
    with pytest.raises(RuntimeError, match="fails at startup"):
        asyncio.run(server.run_daemon(http=False))
    # Not a clean shutdown: the next start sees it (and counts it, with an update pending).
    assert json.loads((aw_home / server.RUN_MARKER).read_text())["pid"] == os.getpid()


# --------------------------------------------------------------------------- #
# Time asleep, takeover and settings during the wait                          #
# --------------------------------------------------------------------------- #


def test_confirmation_counts_only_time_awake(aw_home, tools, monkeypatch):
    switched(aw_home, tools, monkeypatch)
    clock = Clock()
    clock.asleep = [8 * 3600]  # the lid closes 5 s after the start, for 8 h
    up, _, _ = daemon_updater(aw_home, tools, clock, version="0.3.7")
    asyncio.run(up.startup())
    assert sum(clock.slept) == update.CONFIRM_AFTER_S
    assert not update.read_state(aw_home).get("pending")


def test_idle_settle_time_does_not_count_time_asleep(aw_home, tools):
    clock = Clock()
    clock.asleep = [3600]
    up, _, _ = daemon_updater(aw_home, tools, clock)
    assert asyncio.run(up.wait_idle(600)) is True
    assert len(clock.slept) == int(update.IDLE_SETTLE_S / update.IDLE_POLL_S)


@pytest.mark.parametrize("force", [True, False])
def test_update_now_takes_over_a_scheduled_wait(aw_home, tools, force):
    clock = Clock()
    s = {"mode": "auto"}
    taken, views = [], []
    polls = {"n": 0}

    def idle():
        polls["n"] += 1
        if polls["n"] == 3:  # busy so far; the user presses Update now
            views.append(up.view())
            taken.append(up.start_apply(force=force))
            s["mode"] = "notify"  # and a manual update switches whatever the mode is
        return polls["n"] > 3 and not force  # without force: once it is quiet

    up, restarts, _ = daemon_updater(aw_home, tools, clock, idle=idle,
                                     settings=lambda: update.UpdateSettings(mode=s["mode"]))
    install_release(up, "v0.3.6")
    asyncio.run(up.tick())
    assert taken == ["v0.3.7"] and views[0]["phase"] == "waiting" and views[0]["scheduled"] is True
    assert restarts and up.layout.current_tag() == "v0.3.7" and up.phase == "restarting"
    if force:
        assert sum(clock.slept) == 2 * update.IDLE_POLL_S  # no settle time after the takeover
    assert up.view()["scheduled"] is False


def test_a_manual_takeover_keeps_its_own_shorter_wait(aw_home, tools):
    """`afterword update --now --wait 60` taking over a scheduled wait gives
    up after its 60 s, not the schedule's 2 h."""
    clock = Clock()
    polls = {"n": 0}

    def idle():
        polls["n"] += 1
        if polls["n"] == 2:
            up.start_apply(force=False, wait_s=60)
        return False  # never quiet

    up, restarts, _ = daemon_updater(aw_home, tools, clock, idle=idle)
    install_release(up, "v0.3.6")
    asyncio.run(up.tick())
    assert restarts == [] and up.layout.current_tag() == "v0.3.6"
    assert sum(clock.slept) <= 60 + 2 * update.IDLE_POLL_S  # not IDLE_WAIT_S


def test_rollback_needs_the_previous_releases_python(aw_home, tools, monkeypatch, capsys):
    """A previous release whose venv is gone is no rollback target: execv
    would fail and leave nothing running."""
    up = switched(aw_home, tools, monkeypatch)
    execs = []
    assert update.startup_check(aw_home, exec_fn=lambda *a: execs.append(a), argv=["serve"]) == "clean"
    py = up.layout.python_in(up.layout.release("v0.3.6"))
    if py.exists() or py.is_symlink():
        py.unlink()
    for _ in range(3):
        crash(aw_home)
        rc = update.startup_check(aw_home, exec_fn=lambda *a: execs.append(a), argv=["serve"])
    assert rc == "no-rollback" and execs == [] and up.layout.current_tag() == "v0.3.7"
    assert "no previous release to go back to" in update.read_state(aw_home)["rollback_reason"]


@pytest.mark.parametrize("change", ["mode", "rolled_back"])
def test_the_scheduled_switch_rechecks_after_the_wait(aw_home, tools, change):
    clock = Clock()
    s = {"mode": "auto"}

    def idle():
        if change == "mode":
            s["mode"] = "off"
        else:
            up.change(rolled_back="v0.3.7")
        return True

    up, restarts, _ = daemon_updater(aw_home, tools, clock, idle=idle,
                                     settings=lambda: update.UpdateSettings(mode=s["mode"]))
    install_release(up, "v0.3.6")
    asyncio.run(up.tick())
    assert restarts == [] and up.layout.current_tag() == "v0.3.6" and up.phase == "idle"
    st = update.read_state(aw_home)
    assert ("mode is now 'off'" if change == "mode" else "not retried automatically") in st["last_result"]
    assert not st.get("pending")


# --------------------------------------------------------------------------- #
# start_apply's wait_s; check_now's answer                                    #
# --------------------------------------------------------------------------- #


def test_start_apply_rejects_a_bad_wait_and_keeps_the_lock_free(aw_home, tools):
    clock = Clock()
    up, restarts, _ = daemon_updater(aw_home, tools, clock)
    install_release(up, "v0.3.6")

    async def now(wait):
        return up.start_apply(wait_s=wait)

    for bad in (10 ** 400, -1, float("nan"), float("inf"), update.MAX_WAIT_S + 1, True, "5"):
        with pytest.raises(update.UpdateError, match="wait_s"):
            asyncio.run(now(bad))
        assert up.phase == "idle"
    assert up._job.acquire(blocking=False)
    up._job.release()
    assert update.parse_wait(0) == 0.0 and update.parse_wait(update.MAX_WAIT_S) == update.MAX_WAIT_S


def test_api_apply_rejects_a_bad_wait(gui, tools):
    d, client = gui
    for raw in ('{"wait_s": 1' + "0" * 400 + "}", '{"wait_s": -5}', '{"wait_s": 1e999}', '{"wait_s": 86401}'):
        r = client.post("/api/update/apply", headers={**H, "Content-Type": "application/json"}, content=raw)
        assert r.status_code == 400 and "wait_s" in r.json()["error"], raw
    assert d.updater._job.acquire(blocking=False)
    d.updater._job.release()


def test_check_now_answers_with_the_phase_reset(aw_home, tools):
    up = make(aw_home)
    v = asyncio.run(up.check_now())
    assert v["phase"] == "idle" and v["available"] is True and up.phase == "idle"


# --------------------------------------------------------------------------- #
# Plugins: a disabled Codex plugin, retries                                   #
# --------------------------------------------------------------------------- #


def test_plugin_refresh_leaves_a_disabled_codex_plugin_alone(aw_home, tools, tmp_path):
    write_plugin_records(tmp_path)
    (tmp_path / "no-codex-home" / "config.toml").write_text('[plugins."afterword@afterword"]\nenabled = false\n')
    assert update.plugin_status("codex") == "disabled" and not update.plugin_installed("codex")
    res = make(aw_home).refresh_plugins()
    assert res["codex"] == {"ok": None, "message": "the codex plugin is disabled; skipped"}
    assert not any(n.startswith("codex") for n in tools.names()) and res["claude"]["ok"] is True


def test_a_failed_plugin_refresh_is_retried_at_the_next_checks(aw_home, tools, tmp_path):
    write_plugin_records(tmp_path)
    clock = Clock()
    up, _, notes = daemon_updater(aw_home, tools, clock, version="0.3.7")
    up.change(plugins_pending=True)  # as confirm() leaves it
    tools.fail["claude marketplace"] = update.Result(1, "", "Could not resolve host")
    asyncio.run(up.startup())
    st = update.read_state(aw_home)
    assert st["plugins_pending"] is True and st["plugins_tries"] == 1
    assert "tried again at the next check" in notes[-1][1]
    for _ in range(update.PLUGIN_RETRIES + 2):
        asyncio.run(up.tick())
    # The first try and PLUGIN_RETRIES more, then it stops.
    assert tools.names().count("claude plugin marketplace update afterword") == 1 + update.PLUGIN_RETRIES
    st = update.read_state(aw_home)
    assert not st.get("plugins_pending") and not st.get("plugins_tries") and st["plugins"]["claude"]["ok"] is False
    # One that works at a retry is done.
    up.change(plugins_pending=True)
    asyncio.run(up.startup())
    tools.fail.clear()
    asyncio.run(up.tick())
    st = update.read_state(aw_home)
    assert not st.get("plugins_pending") and st["plugins"]["claude"]["ok"] is True
    assert notes[-1][1].startswith("plugins updated")


# --------------------------------------------------------------------------- #
# install-service with an update pending                                      #
# --------------------------------------------------------------------------- #


def test_install_service_on_the_pending_release_keeps_its_rollback(launchd, tools, aw_home, monkeypatch):
    monkeypatch.setattr(update, "__version__", "0.3.7")
    up = make(aw_home, version="0.3.7")
    install_release(up, "v0.3.6")
    up.prepare("v0.3.7")
    up.switch("v0.3.7")
    up.change(attempts=2)  # crash-looping
    assert update.install_managed(updater=up) == 0
    st = update.read_state(aw_home)
    assert st["pending"] is True and st["attempts"] == 2 and st["previous"] == "v0.3.6"
    assert st["install"] == "managed"


def test_changelog_ignores_headings_in_code_blocks():
    text = ("## 0.3.7 (x)\n\n- Seven:\n\n```sh\n## restart\nafterword restart\n```\n\n~~~\n## 0.3.6 not this\n~~~\n"
            "- After.\n\n## 0.3.6 (y)\n\n- Six.\n")
    body = section(text, "0.3.7")
    assert body.endswith("~~~\n- After.") and "## restart" in body and body.count("```") == 2
    assert section(text, "0.3.6") == "- Six."
    # A heading only inside a code block is no section.
    assert section("```\n## 1.0.0 (x)\n- a\n```\n", "1.0.0") is None
