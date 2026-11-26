"""Step 5: UserPromptSubmit injection, SessionStart seeding, /voice, and
the Claude Code plugin files."""

import json
import os
import re
import time

import pytest

from conftest import TIME_SCALE, ROOT, hook_log, run_hook, stop_payload, wait_for, SYSTEM_PYTHON, needs_system_python, require_system_python

CONFIG = """
[profiles.nova]
voice = "builtin:Serena"
lang = "en"
[profiles.ember]
voice = "builtin:Aiden"
lang = "ja"
"""


@pytest.fixture
def home(aw_home):
    (aw_home / "config.toml").write_text(CONFIG)
    return aw_home


def prompt(text, sid="s1", **env):
    return run_hook({"session_id": sid, "prompt": text, "cwd": "/w/p", "hook_event_name": "UserPromptSubmit"},
                    args=("user-prompt-submit",), env=env or None)


def context(proc):
    if not proc.stdout:
        return None
    out = json.loads(proc.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    return out["hookSpecificOutput"]["additionalContext"]


def state(home, sid="s1"):
    p = home / "state" / f"{sid}.json"
    return json.loads(p.read_text()) if p.exists() else None


def start(sid="s1", source="startup", **env):
    return run_hook({"session_id": sid, "source": source, "cwd": "/w/p", "hook_event_name": "SessionStart"},
                    args=("session-start",), env=env or None)


# --------------------------------------------------------------------------- #
# Zero tokens while off                                                        #
# --------------------------------------------------------------------------- #


def test_voice_off_adds_nothing(home):
    proc, elapsed = prompt("Fix the failing test.")
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    assert hook_log(home)[-1]["decision"] == "none"
    assert state(home) is None  # an ordinary prompt never creates state


def test_voice_on_adds_short_instruction_in_profile_language(home):
    from afterword.hooks import common

    common.save_state("s1", {"profile": "ember"})
    proc, _ = prompt("Fix the failing test.")
    ctx = context(proc)
    assert "profile ember" in ctx and "<say>" in ctx and "Japanese" in ctx
    assert "heard sooner" in ctx  # the start placement (early speaking)
    assert len(ctx) < 230  # about 45 tokens
    assert proc.stderr == b""


def test_muted_session_still_gets_instruction(home):
    from afterword.hooks import common

    common.save_state("s1", {"profile": "nova", "muted": True})
    assert "profile nova" in context(prompt("hello")[0])


# --------------------------------------------------------------------------- #
# /voice                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cmd", ["/afterword:voice nova", "/afterword-voice nova", "  /afterword:voice   nova  ",
                                 "afterword:voice nova", "afterword-voice nova", "  afterword:voice nova\n"])  # no slash: Codex
def test_voice_command_turns_on_and_instructs_same_turn(home, cmd):
    ctx = context(prompt(cmd)[0])
    assert "now on for this session with profile nova" in ctx
    assert "End your reply with <say>" in ctx  # the confirmation itself is spoken
    st = state(home)
    assert st["profile"] == "nova" and st["source"] == "voice-command" and st["cwd"] == "/w/p"


def test_voice_off_on_mute_status(home):
    prompt("/afterword:voice ember")
    ctx = context(prompt("/afterword:voice off")[0])
    assert "now off" in ctx and "<say>" not in ctx
    assert state(home)["profile"] is None
    ctx = context(prompt("/afterword:voice on")[0])
    assert state(home)["profile"] == "ember"  # back to the last profile
    context(prompt("/afterword:voice mute")[0])
    assert state(home)["muted"] is True
    ctx = context(prompt("/afterword:voice")[0])
    assert "ember (muted)" in ctx and "nova, ember" in ctx
    context(prompt("/afterword:voice unmute")[0])
    assert state(home)["muted"] is False


@pytest.mark.parametrize("arg", ["nobody", "../etc", "a b"])
def test_unknown_profile_asks_claude_to_pick_and_leaves_voice_unchanged(home, arg):
    prompt("/afterword:voice nova")
    ctx = context(prompt(f"/afterword:voice {arg}")[0])
    assert "pick this session's voice" in ctx and f"asked for: {json.dumps(arg)}" in ctx
    assert "- nova:" in ctx and "- ember:" in ctx and '<voice set="NAME"/>' in ctx
    assert state(home)["profile"] == "nova"


# --------------------------------------------------------------------------- #
# Claude picks the voice: the menu and voices.json                             #
# --------------------------------------------------------------------------- #

PICK_CONFIG = """
[profiles.nova]
voice = "builtin:Serena"
lang = "en"
style = "calm, slightly wry"
[profiles.ember]
voice = "kokoro:af_heart"
lang = "en"
description = "warm storyteller for long explanations"
persona = "not shown: the description wins"
[profiles.tutor]
voice = "say:Samantha"
lang = "zh"
persona = "a patient teacher"
[profiles.tutor.langs.en]
voice = "say:Daniel"
"""


@pytest.fixture
def pick_home(aw_home):
    (aw_home / "config.toml").write_text(PICK_CONFIG)
    return aw_home


def voices(home):
    p = home / "voices.json"
    return json.loads(p.read_text())["profiles"] if p.exists() else {}


def menu_lines(ctx):
    return [line for line in ctx.splitlines() if line.startswith("- ")]


@pytest.mark.parametrize("cmd", ["/afterword:voice", "/afterword:voice pick", "/afterword:voice auto",
                                 "afterword:voice pick", "/afterword-voice ?"])
def test_pick_lists_every_profile_with_its_description(pick_home, cmd):
    ctx = context(prompt(cmd)[0])
    assert "pick this session's voice" in ctx and "asked for" not in ctx
    lines = menu_lines(ctx)
    assert len(lines) == 3
    assert "- ember: warm storyteller for long explanations (kokoro, en); free, never used" in lines
    assert "- nova: calm, slightly wry (builtin, en); free, never used" in lines
    assert "- tutor: a patient teacher (say, zh/en); free, never used" in lines
    assert '<voice set="NAME"/>' in ctx and "<say>" in ctx
    assert len(ctx) < 900  # about 150 tokens for three profiles
    assert state(pick_home) is None  # nothing changes until Claude answers


def test_bare_command_shows_the_state_while_the_voice_is_on(pick_home):
    prompt("/afterword:voice nova")
    ctx = context(prompt("/afterword:voice")[0])
    assert "voice for this session: nova" in ctx and "pick" not in ctx
    assert "voice for this session: nova" in context(prompt("/afterword:voice status")[0])


def test_free_text_is_passed_on_as_the_request(pick_home):
    ctx = context(prompt("/afterword:voice something calm,\n  for \"long\" reads")[0])
    assert 'The user asked for: "something calm, for \\"long\\" reads".' in ctx
    long = context(prompt("/afterword:voice " + "x " * 400)[0])
    assert "..." in long and len(long) < 1200


def test_free_and_least_recently_used_voices_come_first(pick_home):
    from afterword.hooks import common

    now = time.time()
    common.note_voice("old", "tutor", "/a", now=now - 5 * 3600)   # used, but long enough ago to be free
    prompt("/afterword:voice nova", sid="s2")                     # another session uses nova now
    ctx = context(prompt("/afterword:voice pick")[0])
    names = [line.split(":")[0][2:] for line in menu_lines(ctx)]
    assert names == ["ember", "tutor", "nova"]
    assert "tutor: a patient teacher (say, zh/en); free, last used 5 h ago" in ctx
    assert "nova: calm, slightly wry (builtin, en); in use by 1 other session" in ctx
    # This session's own voice is not "in use by another session", but goes after the free ones.
    prompt("/afterword:voice ember")
    ctx = context(prompt("/afterword:voice choose")[0])  # a new prompt (the same text twice is a duplicate)
    assert [line.split(":")[0][2:] for line in menu_lines(ctx)] == ["tutor", "ember", "nova"]
    assert "ember: warm storyteller for long explanations (kokoro, en); this session's voice now" in ctx


def test_voices_json_follows_each_session(pick_home):
    prompt("/afterword:voice nova")
    v = voices(pick_home)
    assert v["nova"]["last_session"] == "s1" and v["nova"]["sessions"]["s1"]["cwd"] == "/w/p"
    prompt("/afterword:voice ember")  # moves: one profile per session
    v = voices(pick_home)
    assert "s1" not in v["nova"]["sessions"] and "s1" in v["ember"]["sessions"]
    assert v["nova"]["last_used"] is not None  # the history stays
    prompt("/afterword:voice mute")
    assert "s1" in voices(pick_home)["ember"]["sessions"]  # muted is still using it
    prompt("/afterword:voice off")
    assert all("s1" not in e["sessions"] for e in voices(pick_home).values())
    start(sid="s3", AFTERWORD_VOICE="tutor")
    assert "s3" in voices(pick_home)["tutor"]["sessions"]


def test_claude_answers_the_menu_with_a_voice_tag(pick_home, say_stub):
    context(prompt("/afterword:voice something warm")[0])
    reply = 'Ember fits a warm request. <voice set="ember"/> <say>Hello, I am ember.</say>'
    run_hook(stop_payload(reply, session_id="s1"))
    assert state(pick_home)["profile"] == "ember" and state(pick_home)["source"] == "voice-tag"
    assert wait_for(lambda: say_stub.exists() and "Hello, I am ember." in say_stub.read_text())
    first = voices(pick_home)["ember"]["sessions"]["s1"]["ts"]
    time.sleep(0.01)
    run_hook(stop_payload("Done. <say>Done.</say>", session_id="s1"))  # each spoken reply refreshes it
    assert voices(pick_home)["ember"]["sessions"]["s1"]["ts"] > first


def test_gui_voice_change_is_logged(pick_home):
    from afterword.daemon import state as st

    st.set_session("g1", profile="nova", known_profiles=["nova", "ember", "tutor"])
    assert "g1" in voices(pick_home)["nova"]["sessions"]
    st.set_session("g1", profile=None)
    assert "g1" not in voices(pick_home)["nova"]["sessions"]


def test_voices_json_prunes_and_survives_corruption(pick_home):
    from afterword.hooks import common

    now = time.time()
    for i in range(common.VOICE_SESSIONS_MAX + 5):
        common.note_voice(f"x{i}", "nova", None, now=now + i)
    common.note_voice("stale", "ember", None, now=now - common.VOICE_SESSION_KEEP_S - 10)
    common.note_voice("s9", "tutor", None, now=now + 100)
    v = voices(pick_home)
    assert len(v["nova"]["sessions"]) == common.VOICE_SESSIONS_MAX and "x0" not in v["nova"]["sessions"]
    assert v["ember"]["sessions"] == {} and v["ember"]["last_used"] is not None
    (pick_home / "voices.json").write_text("{not json")
    ctx = context(prompt("/afterword:voice pick")[0])
    assert len(menu_lines(ctx)) == 3 and "never used" in ctx
    prompt("/afterword:voice nova")
    assert "s1" in voices(pick_home)["nova"]["sessions"]


def test_pick_without_profiles(aw_home):
    ctx = context(prompt("/afterword:voice pick")[0])
    assert "no voice profiles configured" in ctx


@pytest.mark.parametrize("text", ["please explain the /afterword:voice nova command", "/voice nova", "/voice",
                                  "explain afterword:voice nova", "voice nova", "afterwordvoice nova",
                                  "afterword-voice is broken in codex, can you look?",
                                  "afterword:voice\n\nHere is the log", "afterword-voice off\nthen fix the tests"])
def test_not_our_command(home, text):
    # A bare /voice belongs to Claude Code (dictation); AfterWord ignores it.
    proc, _ = prompt(text)
    assert proc.stdout == b""
    assert state(home) is None


def test_stop_after_voice_command_speaks(home, say_stub):
    prompt("/afterword:voice nova")
    run_hook(stop_payload("Voice is on. <say>Voice is on now.</say>", session_id="s1"))
    assert wait_for(lambda: say_stub.exists() and "Voice is on now." in say_stub.read_text())


# --------------------------------------------------------------------------- #
# SessionStart and AFTERWORD_VOICE                                             #
# --------------------------------------------------------------------------- #


def test_env_seeds_new_session(home):
    proc, _ = start(AFTERWORD_VOICE="nova")
    assert proc.returncode == 0 and proc.stdout == b""
    st = state(home)
    assert st["profile"] == "nova" and st["source"] == "env" and st["last_profile"] == "nova"
    assert "profile nova" in context(prompt("hi")[0])


def test_env_off_and_invalid(home):
    start(AFTERWORD_VOICE="off")
    assert state(home)["profile"] is None
    start(sid="s2", AFTERWORD_VOICE="../bad")
    assert state(home, "s2") is None
    assert "invalid" in hook_log(home)[-1]["reason"]


def test_resume_keeps_the_sessions_choice(home):
    start(AFTERWORD_VOICE="nova")
    prompt("/afterword:voice off")
    start(source="resume", AFTERWORD_VOICE="nova")
    assert state(home)["profile"] is None
    start(sid="fresh", source="resume", AFTERWORD_VOICE="ember")  # no state yet: seed
    assert state(home, "fresh")["profile"] == "ember"


def test_no_env_no_state(home):
    proc, _ = start()
    assert proc.stdout == b"" and state(home) is None


# --------------------------------------------------------------------------- #
# Never fails, for the new events too                                          #
# --------------------------------------------------------------------------- #

BAD = [b"", b"not json", b"[]", b'{"session_id": 5, "prompt": ["x"]}', b'{"prompt": "/afterword:voice nova"}',
       b'{"session_id": "s", "prompt": "/afterword:voice ' + b"x" * 100000 + b'"}', b'{"session_id": "s", "source": 7}']


@pytest.mark.parametrize("event", ["user-prompt-submit", "session-start"])
@pytest.mark.parametrize("stdin", BAD, ids=range(len(BAD)))
def test_new_events_never_fail(home, event, stdin):
    proc, elapsed = run_hook(stdin, args=(event,), env={"AFTERWORD_VOICE": "nova"})
    assert proc.returncode == 0 and proc.stderr == b"" and elapsed < 2.0 * TIME_SCALE
    if proc.stdout:
        json.loads(proc.stdout)  # anything printed is valid hook JSON


def test_prompt_hook_with_corrupt_config_and_closed_stdout(home):
    import subprocess
    import sys

    (home / "config.toml").write_text("[[[ broken")
    from afterword.hooks import common

    common.save_state("s1", {"profile": "nova"})
    proc = subprocess.run(
        [sys.executable, str(ROOT / "afterword/hooks/afterword_hook.py"), "user-prompt-submit"],
        input=json.dumps({"session_id": "s1", "prompt": "hi"}).encode(),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        preexec_fn=lambda: (os.close(1), os.close(2)),
    )
    assert proc.returncode == 0




@needs_system_python
def test_prompt_hook_on_system_python(home):
    proc, _ = run_hook({"session_id": "s9", "prompt": "/afterword:voice nova"}, args=("user-prompt-submit",),
                       python=SYSTEM_PYTHON)
    assert "profile nova" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_prompt_hook_is_fast(home):
    from afterword.hooks import common

    common.save_state("s1", {"profile": "nova"})
    for _ in range(3):
        prompt("hi")
    times = [r["elapsed_ms"] for r in hook_log(home) if r.get("hook_event") == "UserPromptSubmit"]
    assert min(times) < 50 * TIME_SCALE


# --------------------------------------------------------------------------- #
# Daemon logging of prompt/session hooks                                       #
# --------------------------------------------------------------------------- #


def test_info_events_reach_the_daemon(home, monkeypatch):
    from test_daemon import RunningDaemon

    d = RunningDaemon(home, monkeypatch)
    try:
        start(AFTERWORD_VOICE="nova")
        prompt("/afterword:voice ember")
        prompt("hello")
        events = wait_for(lambda: [e for e in d.events() if e["hook_event"] in ("SessionStart", "UserPromptSubmit")]
                          if len([e for e in d.events() if e["hook_event"] in ("SessionStart", "UserPromptSubmit")]) >= 3
                          else None)
        assert events, d.events()
        reasons = {e["reason"] for e in events}
        assert "voice seeded from AFTERWORD_VOICE=nova" in reasons
        assert "/voice ember -> ember" in reasons and "added <say> instruction (ember)" in reasons
        assert all(e["status"] == "info" for e in events)
        sessions = d.get("/api/sessions")["sessions"]
        assert any(s["session_id"] == "s1" and s["profile"] == "ember" for s in sessions)
    finally:
        d.stop()


def test_info_events_spool_when_daemon_down(home):
    prompt("hello")
    rec = json.loads((home / "spool.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "info" and rec["event"] == "UserPromptSubmit"


# --------------------------------------------------------------------------- #
# Plugin files                                                                 #
# --------------------------------------------------------------------------- #


def test_plugin_manifest_and_marketplace():
    plugin = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", plugin["name"])
    assert market["plugins"][0]["name"] == plugin["name"] and market["plugins"][0]["source"] == "./"
    assert market["owner"]["name"]


def test_plugin_hooks_point_inside_the_plugin():
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "MessageDisplay", "Stop", "PermissionRequest", "Notification"}
    for event, groups in hooks.items():
        for h in groups[0]["hooks"]:
            cmd = h["command"]
            assert "${CLAUDE_PLUGIN_ROOT}/afterword/hooks/afterword_hook.py" in cmd
            assert ".." not in cmd
            if event == "UserPromptSubmit":
                assert not h.get("async")  # context must be read before the prompt goes to Claude
            if event in ("Stop", "MessageDisplay", "PermissionRequest", "Notification"):
                assert h.get("async") is True  # never delays Claude, holds the screen or decides a permission
    assert (ROOT / "afterword/hooks/afterword_hook.py").is_file()


def test_codex_plugin_manifest_and_hooks():
    """Codex reads .codex-plugin/plugin.json first. Its hooks file runs the same
    script with the same arguments as hooks/hooks.json, but only for events
    Codex has, synchronously (codex exec can exit before an async hook
    finishes), and with Codex's ${PLUGIN_ROOT}."""
    codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    claude = json.loads((ROOT / ".claude-plugin/plugin.json").read_text())
    assert codex["name"] == claude["name"]
    assert codex["hooks"] == "./hooks/codex-hooks.json"
    hooks = json.loads((ROOT / "hooks/codex-hooks.json").read_text())["hooks"]
    claude_hooks = json.loads((ROOT / "hooks/hooks.json").read_text())["hooks"]
    codex_events = {"PreToolUse", "PermissionRequest", "PostToolUse", "PreCompact", "PostCompact",
                    "SessionStart", "SessionEnd", "UserPromptSubmit", "SubagentStart", "SubagentStop",
                    "Stop", "Interrupt"}
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "Stop", "PermissionRequest"}
    assert set(hooks) <= codex_events and set(hooks) <= set(claude_hooks)
    for event, groups in hooks.items():
        (group,) = groups
        (h,) = group["hooks"]
        (ch,) = claude_hooks[event][0]["hooks"]
        assert h["type"] == "command" and "async" not in h, event
        assert h["command"] == ch["command"].replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}") + " codex", event
        assert h["timeout"] == ch["timeout"], event


def test_voice_command_file():
    text = (ROOT / "commands/voice.md").read_text()
    assert text.startswith("---\n") and "description:" in text and "$ARGUMENTS" in text
    assert "afterword:voice-command" in text and "disable-model-invocation: true" in text


def test_install_hooks_registers_all_events_and_voice_command(tmp_path):
    from afterword import cli

    settings = tmp_path / ".claude" / "settings.json"
    assert cli.main(["install-hooks", "--settings", str(settings), "--python", "/usr/bin/python3"]) == 0
    hooks = json.loads(settings.read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "MessageDisplay", "Stop", "PermissionRequest", "Notification"}
    assert hooks["PermissionRequest"][0]["hooks"][0]["async"] is True
    assert hooks["Notification"][0]["hooks"][0]["command"].endswith(" notification")
    ups = hooks["UserPromptSubmit"][0]["hooks"][0]
    assert ups["command"].endswith(" user-prompt-submit") and "async" not in ups
    cmd = tmp_path / ".claude" / "commands" / "afterword-voice.md"
    assert "afterword:voice-command" in cmd.read_text() and "disable-model-invocation: true" in cmd.read_text()
    assert not (tmp_path / ".claude" / "commands" / "voice.md").exists()  # never shadows the built-in /voice
    assert cli.main(["install-hooks", "--settings", str(settings), "--uninstall"]) == 0
    assert not cmd.exists() and "hooks" not in json.loads(settings.read_text())


def test_install_hooks_leaves_someone_elses_voice_command(tmp_path):
    from afterword import cli

    settings = tmp_path / ".claude" / "settings.json"
    cmd = tmp_path / ".claude" / "commands" / "afterword-voice.md"
    cmd.parent.mkdir(parents=True)
    cmd.write_text("my own command")
    cli.main(["install-hooks", "--settings", str(settings)])
    cli.main(["install-hooks", "--settings", str(settings), "--uninstall"])
    assert cmd.read_text() == "my own command"


# --------------------------------------------------------------------------- #
# Installed twice (plugin + settings.json): each turn is handled once          #
# --------------------------------------------------------------------------- #


def _parallel_hooks(payload, args, n=2, env=None):
    """Start n hook processes and feed them all before reading any output,
    so they really run at the same time."""
    import subprocess
    import sys
    import threading

    full = dict(os.environ, **(env or {}))
    procs = [subprocess.Popen([sys.executable, str(ROOT / "afterword/hooks/afterword_hook.py"), *args],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=full)
             for _ in range(n)]
    data = json.dumps(payload).encode()
    results = [None] * n
    barrier = threading.Barrier(n)

    def run(i, p):
        barrier.wait()
        out, err = p.communicate(data, timeout=10)
        results[i] = (out, err, p.returncode)

    threads = [threading.Thread(target=run, args=(i, p)) for i, p in enumerate(procs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_duplicate_stop_hooks_speak_once(home, say_stub):
    from afterword.hooks import common

    common.save_state("s1", {"profile": "nova"})
    payload = stop_payload("<say>Only once, please.</say>", session_id="s1", prompt_id="p-1")
    results = _parallel_hooks(payload, ("stop",), n=3)
    assert all(rc == 0 and out == b"" for out, _, rc in results)
    time.sleep(0.5)
    assert say_stub.read_text().count("Only once, please.") == 1
    reasons = [r.get("reason") for r in hook_log(home) if r.get("hook_event") == "Stop"]
    assert sum(1 for r in reasons if r and r.startswith("duplicate")) == 2


def test_same_text_on_a_later_turn_is_spoken_again(home, say_stub):
    from afterword.hooks import common

    common.save_state("s1", {"profile": "nova"})
    run_hook(stop_payload("<say>Done.</say>", session_id="s1", prompt_id="p-1"))
    run_hook(stop_payload("<say>Done.</say>", session_id="s1", prompt_id="p-2"))
    assert wait_for(lambda: say_stub.exists() and say_stub.read_text().count("Done.") == 2)


def test_duplicate_prompt_hooks_inject_once(home):
    payload = {"session_id": "s1", "prompt": "/afterword:voice nova", "prompt_id": "p-9", "cwd": "/w"}
    results = _parallel_hooks(payload, ("user-prompt-submit",), n=2)
    outputs = [out for out, _, rc in results if out]
    assert len(outputs) == 1 and "profile nova" in outputs[0].decode()
    assert all(rc == 0 for _, _, rc in results)


def test_claim_markers_expire(home, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("hook_claims", ROOT / "afterword/hooks/afterword_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    import sys

    sys.path.insert(0, str(ROOT / "afterword/hooks"))
    import common

    hook._common = common
    assert hook.claim("stop", "s", "k") and not hook.claim("stop", "s", "k")
    for p in (home / "seen").iterdir():
        os.utime(p, (time.time() - hook.CLAIM_TTL_S - 5,) * 2)
    assert hook.claim("stop", "s", "k")  # stale marker: claimable again
    hook.claim("stop", "s", "other")
    old = next((home / "seen").iterdir())
    os.utime(old, (time.time() - hook.CLAIM_TTL_S - 5,) * 2)
    hook.claim("stop", "s", "third")
    assert not old.exists()  # stale markers are pruned
    assert len(list((home / "seen").iterdir())) == 2


# --------------------------------------------------------------------------- #
# Regressions from the step 5/6 review                                         #
# --------------------------------------------------------------------------- #


def test_dotted_profile_names_on_system_python(home):
    import tomllib  # noqa: F401  (3.12 test runner)

    (home / "config.toml").write_text('[profiles."nova.v2"]\nvoice = "builtin:Serena"\n[profiles.plain]\nvoice = "builtin:Ryan"\n')
    require_system_python()
    proc, _ = run_hook({"session_id": "d1", "prompt": "/afterword:voice nova.v2"}, args=("user-prompt-submit",),
                       python=SYSTEM_PYTHON)
    assert "profile nova.v2" in proc.stdout.decode()
    proc, _ = run_hook({"session_id": "d2", "prompt": "/afterword:voice nova"}, args=("user-prompt-submit",),
                       python=SYSTEM_PYTHON)
    out = proc.stdout.decode()
    assert "pick this session's voice" in out and "- nova.v2: (builtin, auto)" in out


def test_mini_toml_quoted_headers():
    import sys

    sys.path.insert(0, str(ROOT / "afterword/hooks"))
    import common

    assert common._mini_toml('[profiles."a.b"]\nx = 1\n[profiles.\'c.d\'.e]\ny = 2') == {
        "profiles": {"a.b": {"x": 1}, "c.d": {"e": {"y": 2}}}}


def test_long_voice_argument_is_not_echoed(home):
    junk = "x" * 5000
    ctx = context(prompt(f"/afterword:voice {junk}")[0])
    assert len(ctx) < 900 and "x" * 201 not in ctx  # passed on to Claude, capped
    assert len(json.dumps(hook_log(home)[-1])) < 2000


def test_repeated_prompt_without_prompt_id_is_not_dropped(home):
    # No prompt_id: only near-simultaneous duplicates are merged.
    run_hook({"session_id": "r1", "prompt": "/afterword:voice off"}, args=("user-prompt-submit",))

    for p in (home / "seen").iterdir():
        os.utime(p, (time.time() - 20,) * 2)  # older than the no-id window
    run_hook({"session_id": "r1", "prompt": "/afterword:voice nova"}, args=("user-prompt-submit",))
    for p in (home / "seen").iterdir():
        os.utime(p, (time.time() - 20,) * 2)
    proc, _ = run_hook({"session_id": "r1", "prompt": "/afterword:voice off"}, args=("user-prompt-submit",))
    assert "now off" in context(proc)
    assert state(home, "r1")["profile"] is None


def test_muted_session_never_uses_say_fallback(home, say_stub):
    from afterword.hooks import common

    common.save_state("m1", {"profile": "nova", "muted": True})
    run_hook(stop_payload("<say>Should stay quiet.</say>", session_id="m1", prompt_id="a"))
    (home / "daemon.json").write_text('{"muted_all": true}')
    common.save_state("m2", {"profile": "nova"})
    run_hook(stop_payload("<say>Also quiet.</say>", session_id="m2", prompt_id="b"))
    time.sleep(0.4)
    assert not say_stub.exists()
    reasons = [json.loads(l)["reason"] for l in (home / "spool.jsonl").read_text().splitlines()]
    assert reasons[-2:] == ["session muted", "all muted"]


def test_fire_and_forget_does_not_log_errors(home, monkeypatch):
    from test_daemon import RunningDaemon

    d = RunningDaemon(home, monkeypatch)
    try:
        for _ in range(3):
            prompt("hello")
        time.sleep(0.5)
    finally:
        d.stop()
    log = (home / "afterwordd.log").read_text() if (home / "afterwordd.log").exists() else ""
    assert "socket connection failed" not in log


def test_dry_run_reports_command_file_and_local_scope_skips(tmp_path, capsys, monkeypatch):
    from afterword import cli

    settings = tmp_path / ".claude" / "settings.json"
    cli.main(["install-hooks", "--settings", str(settings), "--dry-run"])
    out = capsys.readouterr().out
    assert "would write" in out and "afterword-voice.md" in out
    assert not settings.exists()
    monkeypatch.chdir(tmp_path)
    cli.main(["install-hooks", "--scope", "local"])
    out = capsys.readouterr().out
    assert "skip the /afterword-voice command" in out
    assert not (tmp_path / ".claude" / "commands").exists()
