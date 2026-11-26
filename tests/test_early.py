"""Early speaking (design-v2 E, build step 4): the MessageDisplay hook speaks
each <say> block as soon as Claude has written it, and Stop speaks only the
blocks that were not spoken early."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from afterword.hooks import tags
from conftest import TIME_SCALE, HOOK, hook_log, run_hook, stop_payload, wait_for, SYSTEM_PYTHON, needs_system_python
from test_daemon import RunningDaemon
from test_hook import FakeDaemon, assert_clean_exit

from afterword.hooks import earlysay


def voice_on(home, session="s1", profile="nova", **extra):
    d = Path(home) / "state"
    d.mkdir(parents=True, exist_ok=True)
    (d / (session + ".json")).write_text(json.dumps({"profile": profile, **extra}))


def md_payload(index, delta, final=False, session="s1", message="M1", prompt="P1", **extra):
    p = {
        "session_id": session, "transcript_path": "/nonexistent.jsonl", "cwd": "/tmp/project",
        "hook_event_name": "MessageDisplay", "prompt_id": prompt, "turn_id": "T-" + str(prompt),
        "message_id": message, "index": index, "final": final, "delta": delta,
    }
    p.update(extra)
    return p


def md(index, delta, final=False, python=None, **kw):
    proc, elapsed = run_hook(md_payload(index, delta, final, **kw), args=("message-display",), python=python)
    assert_clean_exit(proc, elapsed)
    return proc


def feed(text_batches, final_last=True, **kw):
    for i, delta in enumerate(text_batches):
        md(i, delta, final=final_last and i == len(text_batches) - 1, **kw)


@pytest.fixture
def daemon(aw_home, monkeypatch):
    d = RunningDaemon(aw_home, monkeypatch)
    yield d
    d.stop()


def hook_events(d, event=None):
    evs = [e for e in reversed(d.events()) if e["kind"] == "hook"]
    return [e for e in evs if event is None or e["hook_event"] == event]


def settled(d, n, event=None):
    """Wait until n hook events (optionally of one kind) are logged and done."""
    def ready():
        evs = hook_events(d, event)
        return evs if len(evs) >= n and all(e["status"] in ("spoken", "skipped", "info", "error") for e in evs) else None
    return wait_for(ready, 5) or hook_events(d, event)


# --------------------------------------------------------------------------- #
# The hook and the daemon together                                            #
# --------------------------------------------------------------------------- #


def test_say_split_across_batches_is_spoken_once_complete(aw_home, daemon):
    voice_on(aw_home)
    md(0, "Let me look.\n")
    md(1, "<say>Checking the\n")
    time.sleep(0.2)
    assert hook_events(daemon) == []  # nothing until the block is closed
    md(2, "tests now.</say>\n")
    md(3, "", final=True)
    early = settled(daemon, 1, "early")
    assert [e["text"] for e in early] == ["Checking the tests now."]
    assert early[0]["status"] == "spoken"
    run_hook(stop_payload("Let me look.\n<say>Checking the\ntests now.</say>\n", session_id="s1", prompt_id="P1"))
    stop = settled(daemon, 1, "Stop")[0]
    assert stop["status"] == "skipped" and stop["reason"] == "spoken early"
    assert len(hook_events(daemon, "early")) == 1


def test_code_blocks_are_masked_across_batches(aw_home, daemon):
    voice_on(aw_home)
    feed(["Example:\n", "```xml\n", "<say>not this one</say>\n", "```\n", "<say>This one.</say>\n", ""])
    early = settled(daemon, 1, "early")
    assert [e["text"] for e in early] == ["This one."]
    # An unclosed fence hides everything after it, batch after batch.
    feed(["```\n", "<say>hidden</say>\n", "more\n", ""], message="M2")
    time.sleep(0.3)
    assert [e["text"] for e in hook_events(daemon, "early")] == ["This one."]


def test_inline_code_still_open_waits_for_the_paragraph(aw_home, daemon):
    voice_on(aw_home)
    md(0, "Use `x\n")
    md(1, "<say>maybe code</say>\n")
    time.sleep(0.3)
    assert hook_events(daemon, "early") == []  # a later backtick could still close the span
    md(2, "y` here\n")
    md(3, "", final=True)
    time.sleep(0.3)
    assert hook_events(daemon, "early") == []  # it was code after all
    # Same shape, but the span never closes: spoken once the paragraph ends.
    md(0, "Use `x\n", message="M2")
    md(1, "<say>Not code.</say>\n", message="M2")
    md(2, "\nNext paragraph.\n", message="M2")
    assert [e["text"] for e in settled(daemon, 1, "early")] == ["Not code."]


def test_two_says_in_one_message(aw_home, daemon):
    voice_on(aw_home)
    md(0, "<say>One.</say> text <say>Two.</say>\n", final=True)  # -p: one call per message
    early = settled(daemon, 1, "early")
    assert len(early) == 1 and early[0]["text"] == "One. Two."
    feed(["<say>Three.</say>\n", "middle\n", "<say>Four.</say>\n", ""], message="M2")
    assert [e["text"] for e in settled(daemon, 3, "early")] == ["One. Two.", "Three.", "Four."]


def test_stop_after_early_says_speaks_only_the_rest(aw_home, daemon):
    voice_on(aw_home)
    msg = "<say>First part.</say>\n\nSome work.\n<say>Second part.</say>"
    md(0, "<say>First part.</say>\n")
    md(1, "\n")
    assert [e["text"] for e in settled(daemon, 1, "early")] == ["First part."]
    # The batch with the second block never made it (e.g. the session ended).
    run_hook(stop_payload(msg, session_id="s1", prompt_id="P1"))
    stop = settled(daemon, 1, "Stop")[0]
    assert stop["status"] == "spoken" and stop["text"] == "Second part."
    assert stop["timings"]["early_dropped"] == 1
    assert [r for r in hook_log(aw_home) if r.get("hook_event") == "Stop"][-1]["early_dropped"] == 1


def test_early_items_share_the_turn_so_they_queue_in_order(aw_home, monkeypatch):
    """Early lines and Stop's line of one turn queue one after another; none
    supersedes another (a new turn still supersedes)."""
    from test_stream import make_daemon

    d = make_daemon(aw_home, monkeypatch)
    t = time.time()
    a = d.dispatch(_early_msg([_seg("One.")]), t)
    b = d.dispatch(_early_msg([_seg("Two.", mid="M2")]), t)
    c = d.dispatch(_early_msg([_seg("Three.", key=False)], event="Stop"), t)
    assert [i.event_id for i in d.queue._items] == [a["event_id"], b["event_id"], c["event_id"]]
    assert {i.turn for i in d.queue._items} == {"P1"}
    n = d.dispatch(_early_msg([_seg("Next turn.", mid="M3")], turn="P2"), t)
    assert [i.event_id for i in d.queue._items] == [n["event_id"]]


def test_message_before_a_tool_call_in_p_mode(aw_home, daemon):
    """-p: one call per message, after it completes, before the tool runs.
    The final message's call and Stop arrive together; either order speaks
    the final block once."""
    voice_on(aw_home)
    md(0, "<say>Starting the wait.</say>", final=True, message="A")
    final = "<say>Done waiting.</say>"
    # Stop first (its MessageDisplay call is slower), then the early call.
    run_hook(stop_payload(final, session_id="s1", prompt_id="P1"))
    md(0, final, final=True, message="B")
    evs = settled(daemon, 3)
    spoken = [e["text"] for e in evs if e["status"] == "spoken"]
    assert spoken == ["Starting the wait.", "Done waiting."]
    late = [e for e in evs if e["hook_event"] == "early" and e["status"] == "skipped"][0]
    assert late["reason"] == "already spoken"


def test_out_of_order_and_parallel_batches_speak_each_block_once(aw_home, daemon):
    voice_on(aw_home)
    md(1, "<say>Second batch first.</say>\n")
    time.sleep(0.2)
    assert hook_events(daemon, "early") == []  # batch 0 is missing: wait
    md(0, "Intro.\n")
    assert [e["text"] for e in settled(daemon, 1, "early")] == ["Second batch first."]
    # The same batches sent by several hook processes at once (two installs,
    # or Claude Code calling in parallel): one line each.
    batches = ["<say>Alpha.</say>\n", "<say>Beta.</say>\n", "end\n", ""]
    procs = []
    for _copy in range(3):
        for i, delta in enumerate(batches):
            data = json.dumps(md_payload(i, delta, final=i == len(batches) - 1, message="M9")).encode()
            p = subprocess.Popen([sys.executable, str(HOOK), "message-display"], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            procs.append((p, data))
    for p, data in procs:
        out, err = p.communicate(data, timeout=15)
        assert p.returncode == 0 and out == b"" and err == b""
    time.sleep(0.5)
    texts = sorted(e["text"] for e in hook_events(daemon, "early") if e["status"] != "skipped")
    assert texts == ["Alpha.", "Beta.", "Second batch first."]


def test_voice_off_muted_and_early_say_off_do_nothing(aw_home, daemon):
    md(0, "<say>Voice is off.</say>\n", final=True)
    assert not (aw_home / "stream").exists()  # the fast path touches nothing
    voice_on(aw_home, muted=True)
    md(0, "<say>Muted.</say>\n", final=True)
    voice_on(aw_home)
    (aw_home / "config.toml").write_text("[daemon]\nearly_say = false\n")
    md(0, "<say>Early off.</say>\n", final=True)
    time.sleep(0.3)
    assert hook_events(daemon) == []
    assert not (aw_home / "stream").exists()
    # Then Stop speaks it as in v1.
    run_hook(stop_payload("<say>Early off.</say>", session_id="s1", prompt_id="P1"))
    assert settled(daemon, 1, "Stop")[0]["status"] == "spoken"


def test_commented_out_block_is_not_spoken_early(aw_home, daemon):
    """An unclosed `<!--` closed by a later batch masks a block in between."""
    voice_on(aw_home)
    feed(["Note:\n<!--\n\n", "<say>hidden draft line</say>\n\n", "-->\n<say>visible</say>\n"])
    early = settled(daemon, 1, "early")
    assert [e["text"] for e in early] == ["visible"]
    msg = "Note:\n<!--\n\n<say>hidden draft line</say>\n\n-->\n<say>visible</say>\n"
    run_hook(stop_payload(msg, session_id="s1", prompt_id="P1"))
    stop = settled(daemon, 1, "Stop")[0]
    assert stop["status"] == "skipped" and stop["reason"] == "spoken early"
    assert [e["text"] for e in hook_events(daemon, "early")] == ["visible"]


def test_nested_block_is_spoken_once(aw_home, daemon):
    """The inner pair of a <say> still open is not spoken early: once the
    outer one closes only the outer block (with the inner text) is kept."""
    voice_on(aw_home)
    feed(["<say>Outer start <say>inner one.</say>\n", "\n", "outer end.</say>\n", "tail\n"])
    early = settled(daemon, 1, "early")
    assert [e["text"] for e in early] == ["Outer start inner one. outer end."]
    msg = "<say>Outer start <say>inner one.</say>\n\nouter end.</say>\ntail\n"
    run_hook(stop_payload(msg, session_id="s1", prompt_id="P1"))
    assert settled(daemon, 1, "Stop")[0]["reason"] == "spoken early"
    assert len(hook_events(daemon, "early")) == 1


def test_voice_tag_in_the_message_is_left_to_stop(aw_home, daemon):
    voice_on(aw_home)
    md(0, '<voice set="off"/><say>Bye.</say>\n', final=True)
    time.sleep(0.3)
    assert hook_events(daemon, "early") == []


def test_daemon_down_leaves_the_line_to_stop(aw_home, say_stub):
    voice_on(aw_home)
    md(0, "<say>Nobody listens.</say>\n", final=True)
    time.sleep(0.3)
    assert not say_stub.exists()  # no `say` fallback from MessageDisplay
    rec = earlysay.load("s1")["messages"]["M1"]
    assert rec["sent"] == {}
    proc, elapsed = run_hook(stop_payload("<say>Nobody listens.</say>", session_id="s1", prompt_id="P1"))
    assert_clean_exit(proc, elapsed)
    assert wait_for(lambda: say_stub.exists() and "Nobody listens." in say_stub.read_text())


# --------------------------------------------------------------------------- #
# The daemon's refusal of repeats                                             #
# --------------------------------------------------------------------------- #


def _early_msg(says, session="s1", turn="P1", event="early", **extra):
    msg = {
        "v": 1, "type": "hook", "event": event, "t_hook": time.time(), "t_sent": time.time(),
        "session_id": session, "cwd": "/w/p", "profile": "nova", "decision": "speak",
        "text": " ".join(s["text"] for s in says), "attrs": {}, "segments": says, "turn": turn,
    }
    msg.update(extra)
    return json.dumps(msg).encode()


def _seg(text, mid="M1", i=0, key=True):
    seg = {"text": text, "attrs": {}, "digest": earlysay.digest(text)}
    if key:
        seg["early_key"] = earlysay.early_key(mid, i, text)
    return seg


def test_duplicate_early_key_is_refused_per_turn(aw_home, monkeypatch):
    from test_stream import make_daemon

    d = make_daemon(aw_home, monkeypatch)
    a = _seg("Hello there.")
    first = d.dispatch(_early_msg([a]), time.time())
    assert first["status"] == "queued" and first["taken"] == [a["early_key"]]
    again = d.dispatch(_early_msg([a]), time.time())
    assert again["status"] == "skipped" and again["reason"] == "already spoken"
    assert again["taken"] == [a["early_key"]]  # the hook may mark it as sent
    # Another turn, or another session: a new line.
    assert d.dispatch(_early_msg([a], turn="P2"), time.time())["status"] == "queued"
    assert d.dispatch(_early_msg([a], session="s2"), time.time())["status"] == "queued"
    # The same text in another message of the turn is a new block.
    b = _seg("Hello there.", mid="M2")
    assert d.dispatch(_early_msg([a, b]), time.time())["taken"] == [a["early_key"], b["early_key"]]
    ev = d.events.get(d.dispatch(_early_msg([a, _seg("New.", i=1)]), time.time())["event_id"])
    assert ev["text"] == "New." and ev["timings"]["early_dropped"] == 1


def test_stop_drops_blocks_spoken_early_by_key_or_text(aw_home, monkeypatch):
    from test_stream import make_daemon

    d = make_daemon(aw_home, monkeypatch)
    d.dispatch(_early_msg([_seg("One."), _seg("Two.", i=1)]), time.time())
    # Stop without the message id (it could not match the stream file): by text.
    ack = d.dispatch(_early_msg([_seg("One.", key=False), _seg("Three.", key=False)], event="Stop"), time.time())
    ev = d.events.get(ack["event_id"])
    assert ack["status"] == "queued" and ev["text"] == "Three." and d.queue._items[-1].segments == [
        {"text": "Three.", "attrs": {}}]
    ack = d.dispatch(_early_msg([_seg("Two.", i=1)], event="Stop"), time.time())
    assert ack["status"] == "skipped" and ack["reason"] == "spoken early"
    # And an early block arriving after Stop sent the same text is refused.
    late = d.dispatch(_early_msg([_seg("Three.", mid="M7")]), time.time())
    assert late["status"] == "skipped" and late["reason"] == "already spoken"


def test_busy_daemon_does_not_take_the_key(aw_home, monkeypatch):
    from test_stream import make_daemon

    (aw_home / "config.toml").write_text("[daemon]\nmax_queue = 1\n[profiles.nova]\nvoice = \"builtin:Serena\"\n")
    d = make_daemon(aw_home, monkeypatch)
    d.dispatch(_early_msg([_seg("Other.")], session="s0"), time.time())
    a = _seg("Hello.")
    busy = d.dispatch(_early_msg([a]), time.time())
    assert busy["reason"] == "daemon busy" and busy["taken"] == []
    d.queue.clear()
    assert d.dispatch(_early_msg([a]), time.time())["status"] == "queued"  # Stop or a retry may speak it


def test_daemon_refuses_early_when_early_say_is_off(aw_home, monkeypatch):
    from test_stream import make_daemon

    (aw_home / "config.toml").write_text("[daemon]\nearly_say = false\n[profiles.nova]\nvoice = \"builtin:Serena\"\n")
    d = make_daemon(aw_home, monkeypatch)
    ack = d.dispatch(_early_msg([_seg("Hello.")]), time.time())
    assert ack["status"] == "skipped" and ack["reason"] == "early speaking off" and ack["taken"] == []
    assert d.status()["config"]["early_say"] is False


def test_early_say_config_is_validated():
    from afterword.daemon.config import build_config

    cfg, errors = build_config({"daemon": {"early_say": "yes"}})
    assert cfg.daemon.early_say is True and any("early_say" in e for e in errors)
    assert build_config({"daemon": {"early_say": False}})[0].daemon.early_say is False


# --------------------------------------------------------------------------- #
# The stream file                                                             #
# --------------------------------------------------------------------------- #


def test_stream_file_is_pruned_and_complete_messages_keep_no_text(aw_home, daemon):
    voice_on(aw_home)
    voice_on(aw_home, session="old")
    d = aw_home / "stream"
    d.mkdir()
    stale = d / "old.json"
    stale.write_text(json.dumps({"v": 1, "messages": {"X": {"turn": "P0", "updated": 1, "parts": {"0": "x"}, "sent": {}}}}))
    os.utime(stale, (1, 1))
    data = {"v": 1, "messages": {"OLD": {"turn": "P0", "updated": time.time() - 3600, "parts": {}, "sent": {}}}}
    (d / "s1.json").write_text(json.dumps(data))
    feed(["<say>Pruned.</say>\n", "tail\n", ""])
    settled(daemon, 1, "early")
    msgs = earlysay.load("s1")["messages"]
    assert list(msgs) == ["M1"] and not stale.exists()
    assert msgs["M1"]["parts"] == {} and msgs["M1"]["done"] == "complete" and len(msgs["M1"]["sent"]) == 1


def test_stream_file_limits(aw_home, monkeypatch):
    rec = {"parts": {}, "sent": {}}
    assert earlysay.add_batch(rec, 0, "x" * 100, False)
    assert not earlysay.add_batch(rec, 1, "y" * earlysay.MESSAGE_MAX_CHARS, False)
    assert rec["done"] == "message too long" and rec["parts"] == {}
    monkeypatch.setattr(earlysay, "FILE_MAX_BYTES", 2000)
    big = {"v": 1, "messages": {
        "m%d" % i: {"updated": i, "parts": {"0": "z" * 300}, "sent": {}} for i in range(20)}}
    earlysay.save("s1", big)
    left = earlysay.load("s1")["messages"]
    assert 0 < len(left) < 20 and "m19" in left and "m0" not in left
    (aw_home / "stream" / "s1.json").write_text("{corrupt")
    assert earlysay.load("s1") == {"v": 1, "messages": {}}


def test_stream_file_cap_counts_bytes(aw_home, monkeypatch):
    """CJK text is 3 bytes a character: a cap in characters let the file
    pass the byte limit `load` reads, and the whole file was lost."""
    monkeypatch.setattr(earlysay, "FILE_MAX_BYTES", 3000)
    big = {"v": 1, "messages": {
        "m%d" % i: {"updated": i, "parts": {"0": "\u4f60\u597d" * 150}, "sent": {"k%d" % i: {"i": 0}}}
        for i in range(5)}}
    earlysay.save("s1", big)
    assert (aw_home / "stream" / "s1.json").stat().st_size <= 3000
    left = earlysay.load("s1")["messages"]
    assert left and "m4" in left and left["m4"]["sent"] == {"k4": {"i": 0}}


def test_joined_and_stable_count():
    rec = {"parts": {"0": "a\n", "2": "c"}, "final": 2}
    assert earlysay.joined(rec) == ("a\n", False)
    rec["parts"]["1"] = "b\n"
    assert earlysay.joined(rec) == ("a\nb\nc", True)
    text = "x `y\n<say>One.</say>\n"
    parsed = tags.parse(text)
    assert earlysay.stable_count(text, parsed, False) == 0
    assert earlysay.stable_count(text, parsed, True) == 1
    assert earlysay.stable_count(text + "\n", tags.parse(text + "\n"), False) == 1


def _stable(text):
    return earlysay.stable_count(text, tags.parse(text), False)


def test_a_block_after_an_unpaired_say_opener_is_not_settled():
    """A later </say> could nest it in a new outer block (whose text
    includes it): speaking it early would say it twice."""
    text = "<say>Outer start <say>inner one.</say>\n"
    assert [t.text for t in tags.parse(text).says] == ["inner one."] and _stable(text) == 0
    assert _stable(text + "\n") == 0  # a blank line does not settle it either
    whole = text + "outer end.</say>\n"
    assert [t.text for t in tags.parse(whole).says] == ["Outer start inner one. outer end."]
    assert _stable(whole) == 1
    # A bare <say> in prose before the block: held back too (a stray </say>
    # later would close it), until the message is complete.
    prose = "Use a <say> tag.\n\n<say>Hi.</say>\n\n"
    assert [t.text for t in tags.parse(prose).says] == ["Hi."] and _stable(prose) == 0
    assert earlysay.stable_count(prose, tags.parse(prose), True) == 1
    # An unpaired opener after the block cannot change it.
    after = "<say>Hi.</say>\n\nThen <say> more"
    assert _stable(after) == 1
    # Self-closing tags are not openers.
    assert _stable("<say/> x\n<say>Hi.</say>\n\n") == 1


def test_a_block_after_an_unclosed_comment_is_not_settled():
    """A later `-->` masks every paragraph up to it."""
    text = "Note <!-- draft\n\n<say>Hidden.</say>\n\nmore\n"
    parsed = tags.parse(text)
    assert [t.text for t in parsed.says] == ["Hidden."] and parsed.open_comments == [5]
    assert _stable(text) == 0
    assert tags.parse(text + "more -->\n<say>Shown.</say>").says[0].text == "Shown."
    # A closed comment, or one opened after the block, does not hold it back.
    assert _stable("<!-- a -->\n\n<say>Hi.</say>\n\n") == 1
    assert _stable("<say>Hi.</say>\n\n<!-- later\n") == 1
    # `<!--` inside a code span or a fence is not a comment.
    assert _stable("`<!--`\n\n<say>Hi.</say>\n\n") == 1
    assert _stable("```\n<!--\n```\n\n<say>Hi.</say>\n\n") == 1


def test_keys_match_between_early_and_stop():
    """Stop finds the message by its text and computes the same keys."""
    data = {"v": 1, "messages": {"M1": {"turn": "P1", "parts": {"0": "<say>Hi.</say>\n"}, "sent": {}}}}
    assert earlysay.find_message(data, "P1", "<say>Hi.</say>") == "M1"
    assert earlysay.find_message(data, "P2", "<say>Hi.</say>") is None
    earlysay.finish(data["messages"]["M1"], "<say>Hi.</say>\n")
    assert earlysay.find_message(data, "P1", " <say>Hi.</say>\n\n") == "M1"
    says = tags.parse("<say>Hi.</say>").says
    keyed = earlysay.segments_with_keys(says, tags.limit_says(says, 400, keep_index=True), "M1")
    assert keyed[0][1]["early_key"] == earlysay.early_key("M1", 0, "Hi.")
    assert keyed[0][1]["digest"] == earlysay.digest("Hi.") and "i" not in keyed[0][1]


def test_stop_payload_without_prompt_id_takes_the_turn_from_the_stream_file(aw_home, monkeypatch):
    voice_on(aw_home)
    fake = FakeDaemon(aw_home / "afterword.sock",
                      reply=b'{"ok": true, "event_id": 1, "status": "skipped", "taken": []}\n')
    try:
        md(0, "<say>Early.</say>\n<say>Later.</say>", final=True, prompt="P9")
        run_hook(stop_payload("<say>Early.</say>\n<say>Later.</say>", session_id="s1"))
        stop = [m for m in fake.received if m["event"] == "Stop"][0]
        assert stop["turn"] == "P9"
        keys = [s["early_key"] for s in stop["segments"]]
        assert keys == [earlysay.early_key("M1", 0, "Early."), earlysay.early_key("M1", 1, "Later.")]
        early = [m for m in fake.received if m["event"] == "early"][0]
        assert early["turn"] == "P9" and [s["early_key"] for s in early["segments"]] == keys
    finally:
        fake.close()


@pytest.mark.parametrize("prompt", [None, "", 7, "P" * 201])
def test_message_display_without_a_prompt_id_is_left_to_stop(aw_home, daemon, prompt):
    """turn_id is not Stop's turn: early blocks sent under it (or under no
    turn) could not be deduped, and were spoken on every batch and by Stop."""
    voice_on(aw_home)
    feed(["<say>Only once.</say>\n\n", "more\n", "even more\n"], prompt=prompt)
    time.sleep(0.3)
    assert hook_events(daemon, "early") == []
    assert not (aw_home / "stream" / "s1.json").exists()
    run_hook(stop_payload("<say>Only once.</say>\n\nmore\neven more\n", session_id="s1",
                          prompt_id=prompt if isinstance(prompt, str) and prompt else None))
    evs = settled(daemon, 1)
    assert [(e["hook_event"], e["status"], e["text"]) for e in evs] == [("Stop", "spoken", "Only once.")]


def test_daemon_skips_an_early_event_without_a_turn(aw_home, monkeypatch):
    from test_stream import make_daemon

    d = make_daemon(aw_home, monkeypatch)
    for msg in (_early_msg([_seg("No turn.")], turn=None), _early_msg([_seg("No turn.")], session="")):
        ack = d.dispatch(msg, time.time())
        assert ack["status"] == "skipped" and ack["reason"] == "early without a turn" and ack["taken"] == []
    assert len(d.queue._items) == 0


# --------------------------------------------------------------------------- #
# Never fails, and fast                                                       #
# --------------------------------------------------------------------------- #

GARBAGE = [
    {},
    {"session_id": "s1"},
    {"session_id": "s1", "message_id": "M", "index": "0", "delta": "<say>x</say>"},
    {"session_id": "s1", "message_id": "M", "index": True, "delta": "<say>x</say>"},
    {"session_id": "s1", "message_id": "M", "index": -1, "delta": "<say>x</say>"},
    {"session_id": "s1", "message_id": "M", "index": 10 ** 12, "delta": "<say>x</say>"},
    {"session_id": "s1", "message_id": 5, "index": 0, "delta": "<say>x</say>"},
    {"session_id": "s1", "message_id": "M" * 5000, "index": 0, "delta": "<say>x</say>"},
    {"session_id": "s1", "message_id": "../../etc", "index": 0, "delta": "<say>x</say>", "final": True},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": ["<say>"], "final": "yes"},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": None, "final": True},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": "<say>" * 50000, "final": False},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": "<say " * 30000 + "</say>" * 9000, "final": True},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": "```\n<say>x</say>\n" * 5000, "final": True},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": "<say>\ud800</say>", "final": True},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": "x" * (3 * 1024 * 1024)},
    {"session_id": "s1", "message_id": "M", "index": 0, "delta": "<say>a</say>", "prompt_id": {"x": 1}},
    {"session_id": ["s1"], "message_id": "M", "index": 0, "delta": "<say>a</say>"},
]


@pytest.mark.parametrize("payload", GARBAGE, ids=range(len(GARBAGE)))
def test_message_display_never_fails_on_garbage(aw_home, say_stub, payload):
    voice_on(aw_home)
    proc, elapsed = run_hook(payload, args=("message-display",))
    assert_clean_exit(proc, elapsed)
    assert not say_stub.exists()


@pytest.mark.parametrize("stdin", [b"", b"not json", b"[]", b"\xff\xfe", b"null"])
def test_message_display_never_fails_on_bad_stdin(aw_home, stdin):
    voice_on(aw_home)
    proc, elapsed = run_hook(stdin, args=("message-display",))
    assert_clean_exit(proc, elapsed)


@pytest.mark.parametrize("what", ["stream-dir-is-file", "stream-file-is-dir", "lock-is-fifo", "corrupt"])
def test_message_display_never_fails_on_a_broken_stream_dir(aw_home, what):
    voice_on(aw_home)
    d = aw_home / "stream"
    if what == "stream-dir-is-file":
        d.write_text("x")
    else:
        d.mkdir()
        if what == "stream-file-is-dir":
            (d / "s1.json").mkdir()
        elif what == "lock-is-fifo":
            os.mkfifo(d / "s1.lock")
        else:
            (d / "s1.json").write_bytes(b"\x00\xff{{{")
    md(0, "<say>Still fine.</say>\n")
    md(1, "", final=True)
    proc, elapsed = run_hook(stop_payload("<say>Still fine.</say>", session_id="s1", prompt_id="P1"))
    assert_clean_exit(proc, elapsed)


def test_held_lock_does_not_block_the_hook(aw_home):
    import fcntl

    voice_on(aw_home)
    d = aw_home / "stream"
    d.mkdir()
    fd = os.open(str(d / "s1.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t = time.time()
        md(0, "<say>Waiting.</say>\n", final=True)
        assert time.time() - t < earlysay.LOCK_WAIT_S + 1.0 * TIME_SCALE
    finally:
        os.close(fd)
    assert any(r.get("reason") == "stream file busy" for r in hook_log(aw_home))


@needs_system_python
def test_message_display_runs_on_system_python(aw_home, daemon):
    voice_on(aw_home)
    md(0, "Hi.\n", python=SYSTEM_PYTHON)
    md(1, "<say>From python three nine.</say>\n", python=SYSTEM_PYTHON)
    md(2, "", final=True, python=SYSTEM_PYTHON)
    assert [e["text"] for e in settled(daemon, 1, "early")] == ["From python three nine."]
    run_hook(stop_payload("Hi.\n<say>From python three nine.</say>\n", session_id="s1", prompt_id="P1"),
             python=SYSTEM_PYTHON)
    assert settled(daemon, 1, "Stop")[0]["reason"] == "spoken early"


def test_message_display_is_fast(aw_home, daemon):
    """Timing budget. The fast path (voice off, or a whole message without
    <say>) reads one small state file; the full path parses and talks to the
    daemon. Times are after interpreter start (the hook's own clock), the
    best of several runs. That clock still counts the CPU the other test
    workers take, so in parallel the budgets get half as much again; what
    they guard against (an import of the daemon's modules, a parse of a
    whole message on the fast path) costs several times more."""
    env = {"AFTERWORD_HOOK_LOG_ALL": "1"}
    budget = TIME_SCALE * (1.5 if os.environ.get("PYTEST_XDIST_WORKER") else 1.0)

    def best(payload, n=8):
        start = len(hook_log(aw_home))
        for _ in range(n):
            proc, _el = run_hook(payload, args=("message-display",), env=env)
            assert proc.returncode == 0
        return min(r["elapsed_ms"] for r in hook_log(aw_home)[start:])

    off = best(md_payload(0, "<say>x</say>\n" + "words " * 2000, session="off"))
    voice_on(aw_home)
    no_say = best(md_payload(0, "plain line\n" * 200, final=True))
    full = min(best(md_payload(0, "<say>Line %d.</say>\n" % i, final=True, message="F%d" % i), n=1) for i in range(8))
    print("MessageDisplay in-hook: voice off %.1f ms, no <say> %.1f ms, early say %.1f ms" % (off, no_say, full))
    assert off < 15 * budget, off
    assert no_say < 20 * budget, no_say
    assert full < 60 * budget, full


def test_a_reply_spoken_only_early_still_counts_as_voice_use(aw_home, daemon):
    # voices.json drives the /afterword:voice pick menu: a session whose lines
    # all go out early must not look idle there.
    voice_on(aw_home)
    feed(["<say>All done early.</say>\n", ""])
    settled(daemon, 1, "early")
    run_hook(stop_payload("<say>All done early.</say>\n", session_id="s1", prompt_id="P1"))
    assert settled(daemon, 1, "Stop")[0]["reason"] == "spoken early"
    usage = json.loads((Path(aw_home) / "voices.json").read_text())["profiles"]
    assert "s1" in usage["nova"]["sessions"]
