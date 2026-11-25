"""The Stop hook: its decisions, the `say` fallback, and the rule that it
never fails (exit 0, no stdout, bounded time) whatever it is fed."""

import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import TIME_SCALE, HOOK, hook_log, run_hook, stop_payload, wait_for

# The hook must not wait on a slow daemon or a FIFO (which would take 3 s or
# forever): 1 s locally, scaled on CI but kept well below that.
HANG_LIMIT_S = min(1.0 * TIME_SCALE, 2.0)

# Python startup alone is ~20 ms; this bound is about "never hangs", the
# real timing is reported by test_hook_is_fast.
MAX_SECONDS = 2.0


def assert_clean_exit(proc, elapsed):
    assert proc.returncode == 0, proc.stderr.decode()
    assert proc.stdout == b"", proc.stdout
    assert proc.stderr == b"", proc.stderr
    assert elapsed < MAX_SECONDS, elapsed


# --------------------------------------------------------------------------- #
# Decisions and the fallback                                                  #
# --------------------------------------------------------------------------- #


def test_voice_tag_turns_session_on_and_say_falls_back(aw_home, say_stub):
    msg = 'Done.\n<voice set="nova"/>\n<say>Voice is on now.</say>'
    proc, elapsed = run_hook(stop_payload(msg))
    assert_clean_exit(proc, elapsed)

    state = json.loads((aw_home / "state" / "sess-1.json").read_text())
    assert state["profile"] == "nova"
    assert wait_for(lambda: say_stub.exists() and "Voice is on now." in say_stub.read_text())

    spooled = [json.loads(line) for line in (aw_home / "spool.jsonl").read_text().splitlines()]
    assert spooled[-1]["decision"] == "speak"
    assert spooled[-1]["fallback"] == "say"


def test_session_off_by_default_is_silent(aw_home, say_stub):
    proc, elapsed = run_hook(stop_payload("<say>Should not be heard.</say>"))
    assert_clean_exit(proc, elapsed)
    time.sleep(0.3)
    assert not say_stub.exists()
    log = hook_log(aw_home)
    assert log[-1]["decision"] == "skip" and log[-1]["reason"] == "session off"


def test_no_say_tag_is_silent_when_on(aw_home, say_stub):
    run_hook(stop_payload('<voice set="nova"/>'))
    proc, elapsed = run_hook(stop_payload("A reply without any tag."))
    assert_clean_exit(proc, elapsed)
    time.sleep(0.3)
    assert not say_stub.exists()
    assert hook_log(aw_home)[-1]["reason"] == "no <say> tag"


def test_voice_off_turns_session_off(aw_home, say_stub):
    run_hook(stop_payload('<voice set="nova"/>'))
    run_hook(stop_payload('<voice set="off"/><say>quiet</say>'))
    state = json.loads((aw_home / "state" / "sess-1.json").read_text())
    assert state["profile"] is None
    time.sleep(0.3)
    assert not say_stub.exists()


def test_sessions_are_independent(aw_home, say_stub):
    run_hook(stop_payload('<voice set="nova"/>', session_id="a"))
    run_hook(stop_payload("<say>from b</say>", session_id="b"))
    time.sleep(0.3)
    assert not say_stub.exists()


def test_fallback_none_disables_say(aw_home, say_stub):
    env = {"AFTERWORD_FALLBACK": "none"}
    run_hook(stop_payload('<voice set="nova"/><say>no</say>'), env=env)
    time.sleep(0.3)
    assert not say_stub.exists()


def test_fallback_none_from_config(aw_home, say_stub):
    (aw_home / "config.toml").write_text('[daemon]\nfallback = "none"\n')
    run_hook(stop_payload('<voice set="nova"/><say>no</say>'))
    time.sleep(0.3)
    assert not say_stub.exists()


def test_text_is_truncated_to_profile_max_chars(aw_home, say_stub):
    (aw_home / "config.toml").write_text("[profiles.nova]\nmax_chars = 30\n")
    long = "One short sentence. " + "and then a lot more words " * 10
    run_hook(stop_payload('<voice set="nova"/><say>%s</say>' % long))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "One short sentence."


def test_text_with_leading_dash_is_not_an_option(aw_home, say_stub):
    run_hook(stop_payload('<voice set="nova"/><say>-v Zarvox hello</say>'))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "-v Zarvox hello"


def test_message_read_from_transcript_when_payload_lacks_it(aw_home, say_stub, tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "old <say>old</say>"}]}},
        {"type": "user", "message": {"role": "user", "content": "again"}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": '<voice set="nova"/>'}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "<say>from transcript</say>"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    payload = stop_payload(None, transcript_path=str(transcript))
    del payload["last_assistant_message"]
    proc, elapsed = run_hook(payload)
    assert_clean_exit(proc, elapsed)
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "from transcript"


def _turn_transcript(tmp_path, earlier_texts):
    """A transcript whose current turn has the given assistant messages, each
    followed by a tool call and its result (an earlier turn says "nova")."""
    transcript = tmp_path / "turn.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "first prompt"}},
        {"type": "assistant", "message": {"id": "old", "content": [{"type": "text", "text": '<voice set="nova"/>'}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "second prompt"}]}},
    ]
    for i, text in enumerate(earlier_texts):
        mid = "m%d" % i
        lines.append({"type": "assistant", "message": {"id": mid, "content": [{"type": "text", "text": text}]}})
        lines.append({"type": "assistant", "message": {"id": mid, "content": [
            {"type": "tool_use", "id": "t%d" % i, "name": "Bash", "input": {"command": "sleep 1"}}]}})
        lines.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t%d" % i, "content": "ok"}]}})
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(transcript)


def test_voice_tag_written_before_a_tool_call_turns_the_session_on(aw_home, say_stub, tmp_path):
    # Stop's last_assistant_message is only the final message; a <voice> tag
    # in a message before a tool call comes from the transcript.
    path = _turn_transcript(tmp_path, ['<voice set="ember"/>\nChecking that now.'])
    proc, elapsed = run_hook(stop_payload("Done. <say>after the tool</say>", transcript_path=path))
    assert_clean_exit(proc, elapsed)
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said.endswith("after the tool")
    state = json.loads((Path(aw_home) / "state" / "sess-1.json").read_text())
    assert state["profile"] == "ember"


def test_turn_voice_ignores_earlier_turns_and_code_and_prefers_the_final_message(aw_home, say_stub, tmp_path):
    # The earlier turn's "nova" tag is not this turn's.
    path = _turn_transcript(tmp_path, ["no tag here"])
    run_hook(stop_payload("<say>quiet</say>", transcript_path=path))
    time.sleep(0.5)
    assert not (say_stub.exists() and say_stub.read_text().strip())
    # A tag inside a code fence is not a tag.
    path = _turn_transcript(tmp_path, ['```\n<voice set="ember"/>\n```'])
    run_hook(stop_payload("<say>still quiet</say>", transcript_path=path, session_id="sess-2"))
    time.sleep(0.5)
    assert not (say_stub.exists() and say_stub.read_text().strip())
    # The final message's own tag wins over an earlier one.
    path = _turn_transcript(tmp_path, ['<voice set="ember"/>'])
    run_hook(stop_payload('<voice set="nova"/><say>final wins</say>', transcript_path=path, session_id="sess-3"))
    assert wait_for(lambda: say_stub.exists() and "final wins" in say_stub.read_text())
    state = json.loads((Path(aw_home) / "state" / "sess-3.json").read_text())
    assert state["profile"] == "nova"
    # The last of several earlier tags wins; voice off in a later message.
    path = _turn_transcript(tmp_path, ['<voice set="ember"/>', '<voice set="off"/>'])
    run_hook(stop_payload("<say>off again</say>", transcript_path=path, session_id="sess-4"))
    time.sleep(0.5)
    assert "off again" not in say_stub.read_text()


def _codex_turn_transcript(tmp_path, earlier_texts):
    """A Codex rollout transcript (task_started opens each turn, assistant
    messages are response_item entries) whose current turn has the given
    assistant messages, each followed by a tool call. The earlier turn says
    "nova", and the current turn's user prompt carries a tag of its own that
    must not count (only assistant text does)."""
    transcript = tmp_path / "rollout.jsonl"

    def msg(role, text, mid):
        kind = "output_text" if role == "assistant" else "input_text"
        return {"type": "response_item", "payload": {
            "type": "message", "id": mid, "role": role, "content": [{"type": kind, "text": text}]}}

    lines = [
        {"type": "session_meta", "payload": {"id": "s"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1"}},
        msg("user", "first prompt", "u1"),
        msg("assistant", '<voice set="nova"/>', "old"),
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1",
                                          "last_agent_message": '<voice set="nova"/>'}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2"}},
        msg("user", 'second prompt <voice set="off"/>', "u2"),
        msg("developer", '<voice set="off"/>', "d2"),
    ]
    for i, text in enumerate(earlier_texts):
        lines.append(msg("assistant", text, "m%d" % i))
        lines.append({"type": "response_item", "payload": {
            "type": "custom_tool_call", "call_id": "c%d" % i, "name": "exec", "input": "sleep 1"}})
        lines.append({"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "c%d" % i, "output": [{"type": "input_text", "text": "ok"}]}})
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return str(transcript)


def test_codex_voice_tag_written_before_a_tool_call_turns_the_session_on(aw_home, say_stub, tmp_path):
    path = _codex_turn_transcript(tmp_path, ['<voice set="ember"/>\nChecking that now.'])
    proc, elapsed = run_hook(stop_payload("Done. <say>after the codex tool</say>", transcript_path=path))
    assert_clean_exit(proc, elapsed)
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said.endswith("after the codex tool")
    state = json.loads((Path(aw_home) / "state" / "sess-1.json").read_text())
    assert state["profile"] == "ember"


def test_codex_turn_voice_stops_at_the_turn_start_and_reads_only_assistant_text(aw_home, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(HOOK).parent))  # the hook imports its siblings (tags)
    import afterword_hook as h
    # The earlier turn's "nova" and the user/developer "off" tags don't count.
    path = _codex_turn_transcript(tmp_path, ["no tag here"])
    assert h._turn_voice(path) is None
    assert h._turn_messages_from_transcript(path) == ["no tag here"]
    path = _codex_turn_transcript(tmp_path, ['<voice set="ember"/>', "middle", '<voice set="off"/>'])
    off = h._turn_voice(path)
    assert off is not None and off.profile is None and off.raw == '<voice set="off"/>'
    assert h._turn_messages_from_transcript(path, needle="<voice") == ['<voice set="ember"/>', '<voice set="off"/>']
    path = _codex_turn_transcript(tmp_path, ['```\n<voice set="ember"/>\n```'])
    assert h._turn_voice(path) is None


def test_codex_turns_with_the_same_text_are_not_duplicates(aw_home, say_stub):
    # Codex sends turn_id (no prompt_id): two quick turns that end with the
    # same reply are both spoken; the same turn twice is still a duplicate.
    run_hook(stop_payload('<voice set="nova"/><say>Done.</say>', turn_id="T1"))
    assert wait_for(lambda: say_stub.exists() and say_stub.read_text().count("Done.") == 1)
    proc, _ = run_hook(stop_payload('<say>Done.</say>', turn_id="T2"))
    assert wait_for(lambda: say_stub.read_text().count("Done.") == 2)
    run_hook(stop_payload('<say>Done.</say>', turn_id="T2"))
    time.sleep(0.5)
    assert say_stub.read_text().count("Done.") == 2


def test_codex_null_message_falls_back_to_the_rollout(aw_home, say_stub, tmp_path):
    path = _codex_turn_transcript(tmp_path, ['<voice set="ember"/>', "Done. <say>from the rollout</say>"])
    run_hook(stop_payload(None, transcript_path=path, turn_id="T9"))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said.endswith("from the rollout")


def test_event_name_from_payload_when_no_arg(aw_home, say_stub):
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>no arg</say>'), args=())
    assert_clean_exit(proc, elapsed)
    assert wait_for(lambda: say_stub.exists() and "no arg" in say_stub.read_text())


def test_disable_env_short_circuits(aw_home, say_stub):
    proc, elapsed = run_hook(
        stop_payload('<voice set="nova"/><say>x</say>'), env={"AFTERWORD_DISABLE": "1"}
    )
    assert_clean_exit(proc, elapsed)
    assert not (aw_home / "state").exists()


# --------------------------------------------------------------------------- #
# Talking to a daemon (a stand-in socket server)                               #
# --------------------------------------------------------------------------- #


class FakeDaemon:
    def __init__(self, path, reply=b'{"ok": true, "event_id": 1, "status": "queued"}\n', delay=0.0):
        self.path = str(path)
        self.reply = reply
        self.delay = delay
        self.received = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(2)
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                self.received.append(json.loads(buf.split(b"\n")[0]))
                time.sleep(self.delay)
                if self.reply is not None:
                    try:
                        conn.sendall(self.reply)
                    except OSError:
                        pass

    def close(self):
        self._stop = True
        self.thread.join(2)
        self.sock.close()


@pytest.fixture
def fake_daemon(aw_home):
    daemons = []

    def make(**kw):
        d = FakeDaemon(aw_home / "afterword.sock", **kw)
        daemons.append(d)
        return d

    yield make
    for d in daemons:
        d.close()


def test_daemon_receives_event_and_no_fallback(aw_home, say_stub, fake_daemon):
    d = fake_daemon()
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say lang="en">To the daemon.</say>'))
    assert_clean_exit(proc, elapsed)
    assert len(d.received) == 1
    msg = d.received[0]
    assert msg["type"] == "hook" and msg["event"] == "Stop"
    assert msg["decision"] == "speak"
    assert msg["text"] == "To the daemon."
    assert msg["attrs"] == {"lang": "en"}
    assert msg["profile"] == "nova"
    assert msg["payload"]["session_id"] == "sess-1"
    assert msg["t_hook"] <= msg["t_sent"]
    time.sleep(0.3)
    assert not say_stub.exists()
    assert not (aw_home / "spool.jsonl").exists()


def test_skips_are_still_reported_to_daemon(aw_home, fake_daemon):
    d = fake_daemon()
    run_hook(stop_payload("no tags at all"))
    assert d.received[0]["decision"] == "skip"
    assert d.received[0]["reason"] == "session off"


def test_slow_daemon_does_not_hang_hook_or_double_speak(aw_home, say_stub, fake_daemon):
    fake_daemon(delay=3.0)
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>slow</say>'))
    assert_clean_exit(proc, elapsed)
    assert elapsed < HANG_LIMIT_S
    time.sleep(0.3)
    assert not say_stub.exists()  # connected: the daemon owns playback


def test_daemon_closing_without_ack(aw_home, say_stub, fake_daemon):
    fake_daemon(reply=None)
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>x</say>'))
    assert_clean_exit(proc, elapsed)
    time.sleep(0.3)
    assert not say_stub.exists()


def test_daemon_garbage_ack(aw_home, fake_daemon):
    fake_daemon(reply=b"\xff\xfe not json\n")
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>x</say>'))
    assert_clean_exit(proc, elapsed)


def test_stale_socket_file_falls_back(aw_home, say_stub):
    # A socket file with nobody listening: connect() is refused.
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(aw_home / "afterword.sock"))
    s.close()
    run_hook(stop_payload('<voice set="nova"/><say>stale</say>'))
    assert wait_for(lambda: say_stub.exists() and "stale" in say_stub.read_text())


def test_socket_path_is_a_regular_file(aw_home, say_stub):
    (aw_home / "afterword.sock").write_text("not a socket")
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>file</say>'))
    assert_clean_exit(proc, elapsed)
    assert wait_for(lambda: say_stub.exists() and "file" in say_stub.read_text())


# --------------------------------------------------------------------------- #
# Never fails                                                                 #
# --------------------------------------------------------------------------- #

BAD_STDIN = [
    b"",
    b"   \n",
    b"not json",
    b"{",
    b"[]",
    b"null",
    b"42",
    b'"a string"',
    b"\xff\xfe\x00\x01",
    b'{"session_id": 5, "last_assistant_message": ["x"]}',
    b'{"session_id": null, "last_assistant_message": null, "transcript_path": 7}',
    b'{"session_id": "../../etc/passwd", "last_assistant_message": "<voice set=\\"nova\\"/><say>x</say>"}',
    b'{"session_id": "' + b"x" * 10000 + b'", "last_assistant_message": "<voice set=\\"n\\"/>"}',
    json.dumps({"last_assistant_message": "<say>" * 100000}).encode(),
    json.dumps({"session_id": "s", "last_assistant_message": "\ud800 lone surrogate"}).encode(),
    json.dumps({"session_id": "s", "transcript_path": "/dev/zero"}).encode(),
    json.dumps({"session_id": "s", "transcript_path": "/"}).encode(),
    json.dumps({"deeply": [[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["x"]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]]}).encode(),
]


@pytest.mark.parametrize("stdin", BAD_STDIN, ids=range(len(BAD_STDIN)))
def test_never_fails_on_bad_input(aw_home, say_stub, stdin):
    proc, elapsed = run_hook(stdin)
    assert_clean_exit(proc, elapsed)


def test_never_blocks_on_fifo_transcript(aw_home, tmp_path):
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    proc, elapsed = run_hook({"session_id": "s", "transcript_path": str(fifo)})
    assert_clean_exit(proc, elapsed)
    assert elapsed < HANG_LIMIT_S


def test_never_fails_on_huge_input(aw_home):
    msg = "word " * (4 * 1024 * 1024)  # ~20 MB, above the stdin cap
    proc, elapsed = run_hook(stop_payload(msg))
    assert_clean_exit(proc, elapsed)


@pytest.mark.parametrize("args", [(), ("stop",), ("Stop",), ("bogus-event",), ("stop", "extra", "args")])
def test_never_fails_on_any_argv(aw_home, args):
    proc, elapsed = run_hook(stop_payload("<say>x</say>"), args=args)
    assert_clean_exit(proc, elapsed)


def test_never_fails_with_unwritable_home(aw_home, say_stub):
    os.chmod(aw_home, 0o500)
    try:
        proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>still speaks</say>'))
        assert_clean_exit(proc, elapsed)
    finally:
        os.chmod(aw_home, 0o700)


def test_never_fails_when_home_is_a_file(tmp_path, monkeypatch):
    f = tmp_path / "afterword-home"
    f.write_text("oops")
    monkeypatch.setenv("AFTERWORD_HOME", str(f))
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>x</say>'))
    assert_clean_exit(proc, elapsed)


def test_never_fails_with_corrupt_state_and_config(aw_home, say_stub):
    (aw_home / "state").mkdir()
    (aw_home / "state" / "sess-1.json").write_text("{corrupt")
    (aw_home / "config.toml").write_text("this is = = not toml [[[")
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>recovers</say>'))
    assert_clean_exit(proc, elapsed)
    assert wait_for(lambda: say_stub.exists() and "recovers" in say_stub.read_text())
    assert any(r.get("msg") == "config unreadable, using defaults" for r in hook_log(aw_home))


def test_never_fails_with_state_path_a_directory(aw_home):
    (aw_home / "state" / "sess-1.json").mkdir(parents=True)
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>x</say>'))
    assert_clean_exit(proc, elapsed)


def test_never_fails_when_say_binary_missing(aw_home):
    env = {"AFTERWORD_SAY_CMD": "/nonexistent/say-binary"}
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>x</say>'), env=env)
    assert_clean_exit(proc, elapsed)
    spooled = json.loads((aw_home / "spool.jsonl").read_text().splitlines()[-1])
    assert spooled["fallback"] == "error"


def test_never_fails_with_bad_say_cmd_quoting(aw_home):
    proc, elapsed = run_hook(
        stop_payload('<voice set="nova"/><say>x</say>'), env={"AFTERWORD_SAY_CMD": "'unclosed"}
    )
    assert_clean_exit(proc, elapsed)


def test_never_fails_with_closed_stdout(aw_home):
    import subprocess

    payload = json.dumps(stop_payload("<say>x</say>")).encode()
    proc = subprocess.run(
        [sys.executable, str(HOOK), "stop"],
        input=payload,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        # Close fds 1 and 2 in the child entirely.
        preexec_fn=lambda: (os.close(1), os.close(2)),
    )
    assert proc.returncode == 0


def test_never_hangs_on_open_stdin(aw_home):
    """If the writer never closes stdin the hook still exits promptly."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, str(HOOK), "stop"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(b'{"session_id": "s"')  # no closing brace, no EOF
    proc.stdin.flush()
    t = time.time()
    code = proc.wait(timeout=10)
    proc.stdin.close()
    assert code == 0
    assert time.time() - t < MAX_SECONDS


HANG_WRAPPER = """
import os, signal, sys, time
sys.argv = ["afterword_hook.py", "stop"]
import importlib.util as u
s = u.spec_from_file_location("hook_under_test", %r)
m = u.module_from_spec(s); s.loader.exec_module(m)
def hang_python(payload):
    while True:
        time.sleep(0.01)
def hang_in_c(payload):
    # Mask SIGALRM so only the watchdog thread can end this, then block in
    # a C-level read that never returns (we hold the pipe's write end).
    signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGALRM])
    r, w = os.pipe()
    os.read(r, 1)
m.handle_stop = {"python": hang_python, "c": hang_in_c}[os.environ["HANG_KIND"]]
try:
    m.main()
finally:
    os._exit(0)
"""


@pytest.mark.parametrize("kind", ["python", "c"])
def test_hard_deadline_ends_real_hangs(aw_home, kind):
    """Only the deadline can end these hangs: SIGALRM for Python code, the
    watchdog thread for a blocking C call with SIGALRM masked."""
    import subprocess

    env = dict(os.environ, AFTERWORD_HOOK_DEADLINE="0.3", HANG_KIND=kind)
    t = time.time()
    proc = subprocess.run(
        [sys.executable, "-c", HANG_WRAPPER % str(HOOK)],
        input=json.dumps(stop_payload("<say>x</say>")).encode(),
        capture_output=True, env=env, timeout=10,
    )
    elapsed = time.time() - t
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == b"" and proc.stderr == b""
    assert 0.3 <= elapsed < 1.5, elapsed
    assert any(r.get("msg") == "hard deadline hit, exiting" for r in hook_log(aw_home))


@pytest.mark.parametrize("name", ["hook.log", "spool.jsonl", "state/sess-1.json", "config.toml"])
def test_fifo_in_home_never_blocks(aw_home, say_stub, name):
    path = aw_home / name
    path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(path)
    proc, elapsed = run_hook(stop_payload('<voice set="nova"/><say>fifo</say>'))
    assert_clean_exit(proc, elapsed)
    assert elapsed < HANG_LIMIT_S, elapsed


def test_symlinked_hook_log_is_not_followed(aw_home, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("")
    (aw_home / "hook.log").symlink_to(target)
    proc, elapsed = run_hook(stop_payload("<say>x</say>"))
    assert_clean_exit(proc, elapsed)
    assert target.read_text() == ""


def test_lone_surrogate_payload_reaches_daemon(aw_home, say_stub, fake_daemon):
    d = fake_daemon()
    raw = '{"session_id": "s", "last_assistant_message": "<voice set=\\"nova\\"/><say>Hello there.</say> \\ud83d"}'
    proc, elapsed = run_hook(raw.encode())
    assert_clean_exit(proc, elapsed)
    assert len(d.received) == 1 and d.received[0]["text"] == "Hello there."


def test_transcript_fallback_never_reaches_previous_turn(aw_home, say_stub, tmp_path):
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "turn one"}},
        {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": '<voice set="nova"/>Done. <say>Turn one finished.</say>'}]}},
        {"type": "user", "message": {"role": "user", "content": "turn two: run the tests"}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    run_hook(stop_payload('<voice set="nova"/>'))  # session on
    payload = {"session_id": "sess-1", "transcript_path": str(transcript)}
    proc, elapsed = run_hook(payload)
    assert_clean_exit(proc, elapsed)
    time.sleep(0.3)
    assert not say_stub.exists()
    assert hook_log(aw_home)[-1]["reason"] == "no <say> tag"


def test_in_process_errors_are_swallowed(aw_home, monkeypatch, capsys):
    """Every internal step raising must still give exit code 0, and main()
    must leave no deadline timer behind (it would os._exit the caller)."""
    import importlib.util
    import signal
    import threading

    spec = importlib.util.spec_from_file_location("aw_hook_under_test", HOOK)
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    def boom(*a, **k):
        raise RuntimeError("boom")

    for name in ("run", "_read_stdin", "handle_stop", "_send_to_daemon", "_arm_deadline"):
        monkeypatch.setattr(hook, name, boom)
        assert hook.main(["hook", "stop"]) == 0
        monkeypatch.undo()
        monkeypatch.setenv("AFTERWORD_HOME", str(aw_home))
    for exc in (KeyboardInterrupt, SystemExit):
        def raiser(*a, _e=exc, **k):
            raise _e()

        monkeypatch.setattr(hook, "run", raiser)
        assert hook.main(["hook", "stop"]) == 0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    time.sleep(0.05)
    assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer) and t.is_alive()]
    assert capsys.readouterr().out == ""


SYSTEM_PYTHON = "/usr/bin/python3"


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system python3")
def test_runs_on_system_python(aw_home, say_stub):
    """Hooks are stdlib-only and must work on macOS's stock python3 (3.9)."""
    proc, elapsed = run_hook(
        stop_payload('<voice set="nova"/><say>old python</say>'), python=SYSTEM_PYTHON
    )
    assert_clean_exit(proc, elapsed)
    assert wait_for(lambda: say_stub.exists() and "old python" in say_stub.read_text())
    assert hook_log(aw_home)[-1]["decision"] == "speak"


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system python3")
def test_system_python_reads_config(aw_home, say_stub):
    """Without tomllib (3.9/3.10) the hook still honours fallback = "none"
    and max_chars via its small TOML reader."""
    (aw_home / "config.toml").write_text(
        '[daemon]\nfallback = "none"  # quiet\n[profiles.nova]\nmax_chars = 25\n'
    )
    proc, elapsed = run_hook(
        stop_payload('<voice set="nova"/><say>should stay quiet</say>'), python=SYSTEM_PYTHON
    )
    assert_clean_exit(proc, elapsed)
    time.sleep(0.3)
    assert not say_stub.exists()
    (aw_home / "config.toml").write_text("[profiles.nova]\nmax_chars = 25\n")
    run_hook(stop_payload("<say>Short one here. And then a much longer tail.</say>"), python=SYSTEM_PYTHON)
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "Short one here."


def test_hook_is_stdlib_only():
    """Import the hook modules with site-packages disabled (-S -I)."""
    import subprocess

    code = (
        "import sys; sys.path.insert(0, %r); import tags, common, pronounce, scriptsplit; "
        "import importlib.util as u; s = u.spec_from_file_location('h', %r); "
        "m = u.module_from_spec(s); s.loader.exec_module(m); print('ok')"
    ) % (str(HOOK.parent), str(HOOK))
    out = subprocess.run([sys.executable, "-S", "-I", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


def test_hook_is_fast(aw_home, say_stub):
    """Typical latency, measured. Prints the numbers for the record."""
    times = []
    for i in range(5):
        _, elapsed = run_hook(stop_payload('<voice set="nova"/><say>t%d</say>' % i))
        times.append(elapsed)
    best = min(times)
    print("hook wall time incl. interpreter start: best %.1f ms" % (best * 1000))
    in_hook = [r["elapsed_ms"] for r in hook_log(aw_home) if "elapsed_ms" in r]
    print("hook time after interpreter start: best %.1f ms" % min(in_hook))
    assert min(in_hook) < 50 * TIME_SCALE
