"""The `say` fallback speaks one line at a time: the detached runner holds
<home>/say.lock through its parts, alerts go the same way, a runner that
waited too long drops its line, and `afterword test` takes the lock too."""

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import pytest

from afterword import cli
from afterword.hooks import afterword_hook
from conftest import HOOK, SYSTEM_PYTHON, hook_log, require_system_python, run_hook, stop_payload, wait_for

SPEAK_S = 0.4


@pytest.fixture
def timed_say(tmp_path, monkeypatch):
    """A `say` stub that sleeps SPEAK_S and records [start, end, text]."""
    record = tmp_path / "spans.jsonl"
    script = tmp_path / "timed_say.py"
    script.write_text(
        "import json, sys, time\n"
        "data = sys.stdin.read()\n"
        "t0 = time.time()\n"
        "time.sleep(%r)\n"
        "line = json.dumps([t0, time.time(), data]) + '\\n'\n"
        "open(%r, 'a', encoding='utf-8').write(line)\n" % (SPEAK_S, str(record))
    )
    monkeypatch.setenv("AFTERWORD_SAY_CMD", "%s %s" % (sys.executable, script))
    return record


def spans(record, n, timeout=15):
    def got():
        if not record.exists():
            return None
        lines = [json.loads(x) for x in record.read_text().splitlines() if x.strip()]
        return lines if len(lines) >= n else None
    return wait_for(got, timeout=timeout) or []


def assert_no_overlap(lines):
    lines = sorted(lines)
    for (_s0, end0, text0), (start1, _e1, text1) in zip(lines, lines[1:], strict=False):
        assert start1 >= end0, "%r started before %r ended" % (text1, text0)


@contextmanager
def held(home):
    """Hold <home>/say.lock from the test process."""
    fd = os.open(str(home / "say.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@pytest.mark.parametrize("python", [None, SYSTEM_PYTHON])
def test_parallel_stops_with_the_daemon_down_do_not_overlap(aw_home, timed_say, python):
    if python:
        require_system_python()
    env = dict(os.environ)
    procs = []
    for i in range(3):
        payload = json.dumps(stop_payload('<voice set="nova"/><say>line %d</say>' % i, session_id="s%d" % i))
        p = subprocess.Popen([python or sys.executable, str(HOOK), "stop"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        procs.append((p, payload))
    for p, payload in procs:
        p.communicate(payload.encode(), timeout=15)
    lines = spans(timed_say, 3)
    assert sorted(text.strip() for _s, _e, text in lines) == ["line 0", "line 1", "line 2"]
    assert_no_overlap(lines)


def test_a_multi_part_line_keeps_the_lock_between_its_parts(aw_home, timed_say):
    """A second Stop cannot slip in between the parts of the first one."""
    run_hook(stop_payload('<voice set="nova"/><say>one</say><say>two</say>', session_id="a"))
    time.sleep(0.15)  # the first runner holds the lock by now
    run_hook(stop_payload('<voice set="nova"/><say>three</say>', session_id="b"))
    lines = sorted(spans(timed_say, 3))
    assert [text.strip() for _s, _e, text in lines] == ["one", "two", "three"]
    assert_no_overlap(lines)


def test_alert_fallback_waits_for_the_lock(aw_home, timed_say):
    (aw_home / "config.toml").write_text(
        '[daemon]\nfallback = "say"\n[alerts]\nenabled = true\nevents = ["permission"]\n'
        'permission_when = "now"\n[profiles.nova]\nvoice = "builtin:Serena"\nlang = "en"\n')
    payload = {"session_id": "s1", "transcript_path": "/nonexistent.jsonl", "cwd": "/w/shop-api",
               "hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    with held(aw_home):
        proc, _ = run_hook(payload, args=("permission-request",))
        assert proc.returncode == 0
        time.sleep(0.5)
        assert not timed_say.exists()  # queued behind the lock
        released = time.time()
    lines = spans(timed_say, 1)
    assert len(lines) == 1 and lines[0][0] >= released


def test_runner_drops_its_line_after_waiting_too_long(aw_home, timed_say):
    base = os.environ["AFTERWORD_SAY_CMD"].split()
    request = {"jobs": [{"argvs": [base], "text": "too late"}], "lock": str(aw_home / "say.lock"),
               "wait": 0.3, "hooks": str(HOOK.parent)}
    with held(aw_home):
        t0 = time.time()
        proc = subprocess.run([sys.executable, "-I", "-c", afterword_hook._SAY_RUNNER],
                              input=json.dumps(request).encode(), capture_output=True, timeout=15)
        assert proc.returncode == 0 and proc.stderr == b""
        assert time.time() - t0 < 5
    time.sleep(SPEAK_S + 0.2)
    assert not timed_say.exists()
    rows = [r for r in hook_log(aw_home) if r.get("msg", "").startswith("say fallback dropped")]
    assert len(rows) == 1 and rows[0]["parts"] == 1 and rows[0]["chars"] == len("too late")


def test_runner_speaks_when_the_lock_cannot_be_used(aw_home, timed_say):
    (aw_home / "say.lock").mkdir()  # not a file: no lock, but the line is still spoken
    base = os.environ["AFTERWORD_SAY_CMD"].split()
    request = {"jobs": [{"argvs": [base], "text": "anyway"}], "lock": str(aw_home / "say.lock"),
               "wait": 0.3, "hooks": str(HOOK.parent)}
    subprocess.run([sys.executable, "-I", "-c", afterword_hook._SAY_RUNNER],
                   input=json.dumps(request).encode(), timeout=15)
    assert [text for _s, _e, text in spans(timed_say, 1)] == ["anyway"]


def test_cli_test_fallback_waits_for_the_lock(aw_home, timed_say, capsys):
    out = {}

    def run():
        out["rc"] = cli.main(["test", "from the cli"])

    with held(aw_home):
        th = threading.Thread(target=run)
        th.start()
        time.sleep(0.5)
        assert th.is_alive()  # waiting for the lock
        release_at = time.time()
    th.join(15)
    assert out["rc"] == 0
    lines = spans(timed_say, 1)
    assert lines[0][2] == "from the cli" and lines[0][0] >= release_at


def test_cli_test_fallback_gives_up_after_the_wait(aw_home, timed_say, monkeypatch, capsys):
    monkeypatch.setattr(afterword_hook, "SAY_LOCK_WAIT_S", 0.2)
    with held(aw_home):
        assert cli.main(["test", "never heard"]) == 1
    assert "gave up waiting" in capsys.readouterr().out
    assert not timed_say.exists()
