import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Speed budgets (upper time limits) are set on a developer Mac; shared CI
# runners are slower. AFTERWORD_TEST_TIME_SCALE overrides; CI defaults to 3x.
TIME_SCALE = float(os.environ.get("AFTERWORD_TEST_TIME_SCALE") or (3.0 if os.environ.get("CI") else 1.0))
HOOK = ROOT / "afterword" / "hooks" / "afterword_hook.py"


@pytest.fixture(autouse=True)
def no_launchd(monkeypatch):
    """No test reaches the real launchctl or ~/Library/LaunchAgents: every
    launchctl call answers "not loaded", and the agents directory is one no
    test can write to. tests/test_service.py installs its own fake. A daemon
    a test starts as a subprocess gets a launchctl binary that does not
    exist, and nothing opens Finder."""
    from afterword import service

    def not_loaded(*args):
        return service.Result(service.NOT_FOUND_RC, "", "Bad request.\nCould not find service (test)")

    def no_open(*args):
        raise AssertionError(f"a test ran `open {' '.join(args)}`; replace service.run_open")

    monkeypatch.setattr(service, "launchctl", not_loaded)
    monkeypatch.setattr(service, "run_open", no_open)
    monkeypatch.setenv(service.AGENTS_DIR_ENV, "/nonexistent/afterword-tests/LaunchAgents")
    monkeypatch.setenv(service.LAUNCHCTL_ENV, "/nonexistent/afterword-tests/launchctl")
    monkeypatch.delenv(service.SERVICE_ENV, raising=False)


@pytest.fixture
def aw_home(tmp_path, monkeypatch):
    """An isolated AFTERWORD_HOME. Unix socket paths are limited to ~104
    bytes on macOS, so use a short directory under /tmp."""
    import tempfile

    d = Path(tempfile.mkdtemp(prefix="aw-", dir="/tmp"))
    monkeypatch.setenv("AFTERWORD_HOME", str(d))
    monkeypatch.delenv("AFTERWORD_FALLBACK", raising=False)
    monkeypatch.delenv("AFTERWORD_DISABLE", raising=False)
    # Apple voices: tests that want them install a stub helper; never compile
    # the real one into a test home.
    monkeypatch.setenv("AFTERWORD_APPLE_HELPER", "none")
    yield d
    import shutil

    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def say_stub(tmp_path, monkeypatch):
    """Replace /usr/bin/say with a script that records what it was asked to
    speak. Returns the path of the record file."""
    record = tmp_path / "said.txt"
    script = tmp_path / "fake_say.py"
    script.write_text(
        "import sys\n"
        "data = sys.stdin.read()\n"
        "open(%r, 'a', encoding='utf-8').write(data + '\\n')\n" % str(record)
    )
    monkeypatch.setenv("AFTERWORD_SAY_CMD", "%s %s" % (sys.executable, script))
    return record


def wait_for(predicate, timeout=5.0, interval=0.02):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def run_hook(stdin, args=("stop",), env=None, python=None, timeout=15):
    """Run the hook as Claude Code would. Returns (CompletedProcess, seconds)."""
    if isinstance(stdin, (dict, list)):
        stdin = json.dumps(stdin)
    if isinstance(stdin, str):
        stdin = stdin.encode()
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    t = time.time()
    proc = subprocess.run(
        [python or sys.executable, str(HOOK), *args],
        input=stdin,
        capture_output=True,
        env=full_env,
        timeout=timeout,
    )
    return proc, time.time() - t


def stop_payload(message, session_id="sess-1", **extra):
    payload = {
        "session_id": session_id,
        "transcript_path": "/nonexistent/transcript.jsonl",
        "cwd": "/tmp/project",
        "permission_mode": "default",
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": message,
    }
    payload.update(extra)
    return payload


def hook_log(home):
    p = Path(home) / "hook.log"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def make_unwritable(path):
    path.chmod(stat.S_IRUSR | stat.S_IXUSR)


@pytest.fixture(autouse=True)
def no_real_updates(monkeypatch, tmp_path):
    """No test runs the updater's programs for real (git, uv, a release's
    python, claude, codex): every run fails the test, and no tool is found,
    so the updater stops with "git was not found" first. tests/test_update.py
    installs its own fakes. The Claude Code and Codex plugin records are
    looked up in empty temp directories, never in ~/.claude or ~/.codex."""
    from afterword import update

    def refuse(argv, *args, **kwargs):
        raise AssertionError(f"a test ran {argv!r} for real; replace update.run_cmd")

    monkeypatch.setattr(update, "run_cmd", refuse)
    monkeypatch.setattr(update, "which", lambda name: None)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-home"))
