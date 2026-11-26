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

# macOS's stock python3 (3.9), which runs the hooks for most users.
SYSTEM_PYTHON = "/usr/bin/python3"


def require_system_python():
    """Skip a test that needs SYSTEM_PYTHON when it is missing, except on CI
    (CI set), where that fails: the 3.9 checks must not quietly stop running."""
    if os.path.exists(SYSTEM_PYTHON):
        return SYSTEM_PYTHON
    if os.environ.get("CI"):
        pytest.fail("%s is missing on CI; the Python 3.9 hook tests cannot run" % SYSTEM_PYTHON)
    pytest.skip("no system python3")


# Marks a test that runs SYSTEM_PYTHON (see require_system_python).
needs_system_python = pytest.mark.system_python


def pytest_configure(config):
    config.addinivalue_line("markers", "system_python: runs the hook code on /usr/bin/python3")


def pytest_runtest_setup(item):
    if item.get_closest_marker("system_python") is not None:
        require_system_python()


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
def home(aw_home):
    return aw_home


@pytest.fixture
def make_lab(home):
    """A voice Lab over a fake engine; shared by the lab, explorer, langs and
    pronounce tests."""
    from test_lab import FakeEngine

    from afterword.daemon import voices
    from afterword.daemon.config import ConfigStore
    from afterword.daemon.lab import Lab

    def make(engine=None, config_text=None):
        if config_text is not None:
            (home / "config.toml").write_text(config_text)
        store = ConfigStore()
        store.ensure_default_file()
        return Lab(home, store, engine or FakeEngine(), voices.ClipStore(home / "voices"))

    return make


@pytest.fixture
def gui(aw_home, monkeypatch):
    """(daemon, TestClient) over a fake engine; files that need a different
    setup define their own `gui`."""
    from fastapi.testclient import TestClient
    from test_lab import FakeEngine, _daemon

    from afterword.daemon.web import build_app

    d = _daemon(aw_home, monkeypatch, FakeEngine())
    client = TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800", raise_server_exceptions=False)
    return d, client


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
