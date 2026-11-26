"""Spoken alerts (docs/design-v2.md section C): [alerts] config, the
PermissionRequest / Notification hooks, pending permissions, "done" at Stop,
queue rules, phrase pools and picking, the `say` fallback, `afterword alerts
generate` (with a fake runner: claude is never called here) and the HTTP API."""

import json
import os
import random
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from afterword.daemon import alerts
from afterword.daemon.config import DEFAULT_CONFIG_TOML, build_config
from afterword.daemon.queue import PlaybackQueue, QueueItem
from afterword.hooks import alerting, common
from conftest import TIME_SCALE, hook_log, run_hook, stop_payload, wait_for, SYSTEM_PYTHON, needs_system_python

H = {"X-AfterWord": "1"}

CONFIG = """
[daemon]
fallback = "say"

[alerts]
enabled = true
events = ["permission", "input", "idle", "done"]
done_after_s = 0
permission_when = "away"

[profiles.nova]
voice = "builtin:Serena"
lang = "en"
say_voice = "Samantha"

[profiles.mei]
voice = "builtin:Vivian"
lang = "zh"
style = "warm"
persona = "a patient older sister who teases a little"
"""


@pytest.fixture
def home(aw_home):
    (aw_home / "config.toml").write_text(CONFIG)
    return aw_home


def perm(sid="s1", command="rm -rf node_modules", description="Remove node_modules directory", **extra):
    tool_input = {"command": command}
    if description is not None:
        tool_input["description"] = description
    p = {"session_id": sid, "transcript_path": "/nonexistent.jsonl", "cwd": "/w/shop-api",
         "hook_event_name": "PermissionRequest", "permission_mode": "default",
         "tool_name": "Bash", "tool_input": tool_input}
    p.update(extra)
    return p


def note(ntype="permission_prompt", sid="s1", message="Claude needs your permission to use Bash"):
    return {"session_id": sid, "transcript_path": "/nonexistent.jsonl", "cwd": "/w/shop-api",
            "hook_event_name": "Notification", "message": message, "notification_type": ntype}


def state(home, sid="s1"):
    p = home / "state" / f"{sid}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def alert_rows(d):
    return [e for e in d.events() if e["kind"] == "alert"]


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #


def test_alerts_are_off_by_default_and_in_the_default_config():
    import tomllib

    cfg, warnings = build_config(tomllib.loads(DEFAULT_CONFIG_TOML))
    assert warnings == []
    assert cfg.alerts.enabled is False
    assert cfg.alerts.events == ("permission", "input", "done")
    assert cfg.alerts.permission_when == "away" and cfg.alerts.done_after_s == 60 and cfg.alerts.profile == ""
    assert build_config({})[0].alerts.enabled is False


def test_alerts_config_is_validated():
    cfg, w = build_config({"alerts": {"enabled": "yes", "events": ["idle", "nope", "idle"], "done_after_s": -3,
                                      "permission_when": "soon", "profile": "ghost", "extra": 1},
                           "profiles": {"nova": {"voice": "builtin:Ryan", "persona": "dry wit"}}})
    a = cfg.alerts
    assert a.enabled is False and a.events == ("idle",) and a.done_after_s == 60 and a.permission_when == "away"
    text = " ".join(w)
    for bit in ("alerts.enabled", "unknown event 'nope'", "alerts.done_after_s", "permission_when", "ghost", "alerts.extra"):
        assert bit in text, bit
    assert cfg.profiles["nova"].persona == "dry wit"
    cfg, w = build_config({"alerts": {"events": "permission"}})
    assert cfg.alerts.events == ("permission", "input", "done") and "alerts.events" in w[0]


def test_hook_side_settings_degrade_to_defaults():
    s = alerting.settings({"alerts": {"enabled": True, "events": ["idle", "bogus"], "done_after_s": 5,
                                      "permission_when": "now", "profile": "mei"}})
    assert s == {"enabled": True, "events": ("idle",), "permission_when": "now", "done_after_s": 5.0, "profile": "mei"}
    # What the mini TOML parser gives on Python < 3.11: the array is read
    # (review finding: it was not, so the `say` fallback spoke events the
    # user had turned off); an unreadable one means the defaults.
    mini = common._mini_toml('[alerts]\nenabled = true\nevents = ["idle"]\npermission_when = "later"\n')
    s = alerting.settings(mini)
    assert s["enabled"] is True and s["events"] == ("idle",) and s["permission_when"] == "away"
    mini = common._mini_toml('[alerts]\nevents = [["idle"]]\n')
    assert "events" not in mini["alerts"] and alerting.settings(mini)["events"] == alerting.DEFAULT_EVENTS
    assert alerting.settings({"alerts": {"enabled": 1}})["enabled"] is False
    assert alerting.settings("garbage")["enabled"] is False


def test_describe_action():
    d = alerting.describe_action
    assert d("Bash", {"command": "rm -rf x", "description": "Remove the build folder"}) == "Remove the build folder"
    assert d("Bash", {"command": "git push origin main --force-with-lease and then some more words"}) == \
        "run git push origin main --force-with-lease and then some …"
    # Codex's description is its approval question: the command is spoken instead.
    assert d("Bash", {"command": "touch /tmp/x", "description":
                      "Do you approve running `touch /tmp/x` outside the sandbox?"}) == "run touch /tmp/x"
    assert d("Edit", {"file_path": "/a/b/models.py", "old_string": "x"}) == "edit models.py"
    assert d("WebFetch", {"url": "https://docs.example.com/x?y"}) == "fetch docs.example.com"
    assert d("mcp__github__create_issue", {"title": "Bug"}) == "use github create issue Bug"
    assert d(None, None) == "" and d(5, [1, 2]) == ""
    assert len(d("Bash", {"description": "x" * 500})) <= alerting.ACTION_MAX
    assert "\n" not in d("Bash", {"description": "two\nlines"})
    assert alerting.project_name("/w/shop-api/") == "shop-api" and alerting.project_name(None) == "Claude"


# --------------------------------------------------------------------------- #
# Queue                                                                        #
# --------------------------------------------------------------------------- #


def item(eid, sid, kind="speech", turn=None):
    return QueueItem(event_id=eid, session_id=sid, text=f"t{eid}", turn=turn or f"turn-{eid}", kind=kind)


def test_alerts_never_supersede_speech_and_speech_leaves_alerts():
    q = PlaybackQueue(16)
    q.put(item(1, "A"))
    r = q.put(item(2, "A", "alert"))
    assert r.accepted and r.superseded == [] and [i.event_id for i in q._items] == [1, 2]
    r = q.put(item(3, "A"))  # a new turn of speech supersedes speech only
    assert [i.event_id for i in r.superseded] == [1]
    assert [i.event_id for i in q._items] == [3, 2]


def test_an_alert_supersedes_the_sessions_older_waiting_alerts_only():
    q = PlaybackQueue(16)
    q.put(item(1, "A", "alert"))
    q.put(item(2, "B", "alert"))
    r = q.put(item(3, "A", "alert"))
    assert [i.event_id for i in r.superseded] == [1] and [i.event_id for i in q._items] == [3, 2]
    q.put(item(4, "A"))
    q._items.append(item(6, "A", "alert"))  # two waiting alerts (as if put before the rule)
    r = q.put(item(5, "A", "alert"))
    assert [i.event_id for i in r.superseded] == [3, 6]
    assert [i.event_id for i in q._items] == [5, 2, 4]
    assert q.snapshot()["pending"][0]["kind"] == "alert"


def test_alert_goes_to_the_end_and_full_queue_refuses():
    q = PlaybackQueue(2)
    q.put(item(1, "A"))
    q.put(item(2, "B"))
    assert not q.put(item(3, "A", "alert")).accepted


# --------------------------------------------------------------------------- #
# Phrases                                                                      #
# --------------------------------------------------------------------------- #


def test_builtin_pools_are_valid_and_have_both_languages():
    for lang in ("en", "zh"):
        for event in alerts.PLACEHOLDERS:
            pool = alerts.BUILTIN[lang][event]
            assert len(pool) >= 4, (lang, event)
            assert all(alerts.check_phrase(p, event) is None for p in pool), (lang, event)
    assert alerting.fallback_phrase("done", "zh", "x") == "x 完成了。"
    assert alerting.fallback_phrase("idle", "ja", "x") == "x is waiting for you."


def test_check_phrase():
    c = alerts.check_phrase
    assert c("{project} needs your OK to {action}.", "permission") is None
    assert "not available" in c("{project} done after {action}", "done")
    assert "unknown placeholder" in c("{who} is done", "done")
    assert "unbalanced" in c("{project} is {done", "done")
    assert "longer than" in c("x" * 121, "idle")
    assert c("", "idle") == "empty" and c(5, "idle") == "not a string" and "line" in c("a\nb", "idle")


def test_pick_avoids_the_last_three_and_fills_placeholders(tmp_path):
    p = alerts.AlertPhrases(tmp_path / "alerts.json", rng=random.Random(7))
    values = {"project": "shop-api", "action": "Remove node_modules directory", "tool": "Bash"}
    seen = []
    for _ in range(40):
        text, template, lang = p.pick("permission", "en", values)
        assert lang == "en" and "{" not in text and template in alerts.BUILTIN["en"]["permission"]
        if "{action}" in template:
            assert "remove node_modules directory" in text  # lowercased after "to"
        assert template not in seen[-3:]
        seen.append(template)
    assert len(set(seen)) == len(alerts.BUILTIN["en"]["permission"])


def test_pick_skips_phrases_whose_placeholders_have_no_value(tmp_path):
    p = alerts.AlertPhrases(tmp_path / "alerts.json")
    for _ in range(20):
        text, template, _ = p.pick("permission", "zh", {"project": "x", "action": "", "tool": ""})
        assert "{action}" not in template and "{tool}" not in template
    text, _, _ = p.pick("done", "en", {"project": "x", "minutes": "1"})
    assert "1 minutes" not in text
    # A language without a pool: English.
    assert p.pick("idle", "fr", {"project": "x"})[2] == "en"


def test_small_pool_still_varies(tmp_path):
    (tmp_path / "alerts.json").write_text(json.dumps({"en": {"idle": ["one {project}", "two {project}"]}}))
    p = alerts.AlertPhrases(tmp_path / "alerts.json")
    got = [p.pick("idle", "en", {"project": "x"})[1] for _ in range(6)]
    assert all(a != b for a, b in zip(got, got[1:], strict=False))


def test_file_pools_override_builtin_and_bad_entries_are_dropped(tmp_path):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps({
        "en": {"idle": ["Ping from {project}.", "bad {who}", 7, "x" * 200], "done": []},
        "chinese": {"done": ["{project} 好了。"]},
        "xx": {"idle": ["x"]},
        "zh": {"idle": ["dup"]},
    }))
    p = alerts.AlertPhrases(path)
    pools = p.pools()
    assert pools["en"]["idle"] == ["Ping from {project}."]
    assert pools["en"]["done"] == alerts.BUILTIN["en"]["done"]  # empty in the file: built-in
    assert pools["zh"]["done"] == ["{project} 好了。"]
    w = " ".join(p.warnings)
    assert "unknown placeholder {who}" in w and "not a string" in w and "longer than" in w
    assert "unknown language 'xx'" in w and "given twice" in w
    # Hot reload: the file changed.
    time.sleep(0.01)
    path.write_text(json.dumps({"en": {"idle": ["Hello {project}."]}}))
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert p.pool("en", "idle") == ["Hello {project}."]
    # Broken file: the last good pools stay, with an error.
    path.write_text("{not json")
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert p.pool("en", "idle") == ["Hello {project}."] and p.error


def test_save_lang_validates_and_resets(tmp_path):
    p = alerts.AlertPhrases(tmp_path / "alerts.json")
    p.save_lang("en", {"idle": ["Still here, {project}?", "Still here, {project}?"]})
    assert json.loads((tmp_path / "alerts.json").read_text()) == {"en": {"idle": ["Still here, {project}?"]}}
    with pytest.raises(alerts.AlertError, match="phrase 1"):
        p.save_lang("en", {"done": ["{action} done"]})
    with pytest.raises(alerts.AlertError, match="unknown language"):
        p.save_lang("klingon", {})
    p.save_lang("english", None)
    assert json.loads((tmp_path / "alerts.json").read_text()) == {}
    assert p.pool("en", "idle") == alerts.BUILTIN["en"]["idle"]


# --------------------------------------------------------------------------- #
# Generate (fake runner)                                                       #
# --------------------------------------------------------------------------- #


class FakeClaude:
    def __init__(self, reply=None, stdout=None, returncode=0, exc=None):
        self.reply, self.stdout, self.returncode, self.exc = reply, stdout, returncode, exc
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((argv, timeout))
        if self.exc:
            raise self.exc
        out = self.stdout if self.stdout is not None else json.dumps(
            {"type": "result", "is_error": False, "result": "```json\n" + json.dumps(self.reply, ensure_ascii=False) + "\n```"})
        return subprocess.CompletedProcess(argv, self.returncode, out, "")


ZH_REPLY = {
    "permission": ["{project} 要{action}，你同意吗？", "{project} 想调用 {tool}。", "{project} 用了 {minutes}", "太" * 130],
    "input": ["{project} 有个问题。"],
    "idle": ["{project} 等你呢。", "{project} 等你呢。"],
    "done": ["{project} 干完啦，用了 {minutes} 分钟。", "“{project} 好啦。”"],
}


def test_generate_builds_the_prompt_validates_and_merges(tmp_path):
    cfg, _ = build_config(__import__("tomllib").loads(CONFIG))
    phrases = alerts.AlertPhrases(tmp_path / "alerts.json")
    fake = FakeClaude(ZH_REPLY)
    res = alerts.generate(cfg, phrases, "mei", count=5, runner=fake)
    argv, timeout = fake.calls[0]
    assert argv[:6] == ["claude", "-p", "--model", "haiku", "--output-format", "json"]
    prompt = argv[-1]
    assert "Chinese" in prompt and "warm" in prompt and "patient older sister" in prompt and "'mei'" in prompt
    assert "{action}" in prompt and "{minutes}" in prompt and "5 different phrases" in prompt
    assert res["lang"] == "zh" and res["profile"] == "mei"
    assert res["added"]["permission"] == ["{project} 要{action}，你同意吗？", "{project} 想调用 {tool}。"]
    assert res["added"]["done"] == ["{project} 干完啦，用了 {minutes} 分钟。", "{project} 好啦。"]  # quotes stripped
    assert len(res["rejected"]) == 2  # {minutes} in permission, too long
    data = json.loads((tmp_path / "alerts.json").read_text())
    # Merged with the pools in use (built-in ones included), only zh written.
    assert set(data) == {"zh"}
    assert data["zh"]["idle"] == alerts.BUILTIN["zh"]["idle"] + ["{project} 等你呢。"]
    # A second run adds only new phrases.
    res2 = alerts.generate(cfg, phrases, "mei", count=5, runner=FakeClaude(ZH_REPLY))
    assert all(v == [] for v in res2["added"].values())


def test_generate_english_with_persona_override(tmp_path):
    cfg, _ = build_config(__import__("tomllib").loads(CONFIG))
    phrases = alerts.AlertPhrases(tmp_path / "alerts.json")
    fake = FakeClaude({"en": {"idle": ["Yo {project}, you there?"]}})
    res = alerts.generate(cfg, phrases, None, count=3, persona="a pirate", runner=fake)
    assert res["profile"] == "nova" and res["lang"] == "en"
    assert "English" in fake.calls[0][0][-1] and "a pirate" in fake.calls[0][0][-1]
    assert res["added"]["idle"] == ["Yo {project}, you there?"]


@pytest.mark.parametrize("fake,match", [
    (FakeClaude(stdout="oops"), "did not return JSON"),
    (FakeClaude(stdout=json.dumps({"is_error": True, "result": "rate limited"})), "rate limited"),
    (FakeClaude(stdout=json.dumps({"result": "no json here"})), "no JSON object"),
    (FakeClaude({"other": ["x"]}), "none of the events"),
    (FakeClaude({"idle": ["{bad}"]}), "all rejected"),
    (FakeClaude(stdout="", returncode=1), "exit 1"),
    (FakeClaude(exc=FileNotFoundError("claude")), "not found"),
    (FakeClaude(exc=subprocess.TimeoutExpired("claude", 1)), "did not answer"),
])
def test_generate_failures_change_nothing(tmp_path, fake, match):
    cfg, _ = build_config(__import__("tomllib").loads(CONFIG))
    phrases = alerts.AlertPhrases(tmp_path / "alerts.json")
    with pytest.raises(alerts.GenerateFailed, match=match):
        alerts.generate(cfg, phrases, "nova", runner=fake)
    assert not (tmp_path / "alerts.json").exists()


def test_generate_bad_arguments(tmp_path):
    cfg, _ = build_config(__import__("tomllib").loads(CONFIG))
    phrases = alerts.AlertPhrases(tmp_path / "alerts.json")
    for kwargs, match in (({"profile_name": "ghost"}, "no profile"), ({"count": 0}, "count"),
                          ({"count": True}, "count"), ({"persona": 5}, "persona")):
        with pytest.raises(alerts.AlertError, match=match):
            alerts.generate(cfg, phrases, runner=FakeClaude(ZH_REPLY), **kwargs)


def test_cli_alerts_generate_and_show(home, monkeypatch, capsys):
    from afterword import cli

    fake = FakeClaude(ZH_REPLY)
    monkeypatch.setattr(alerts, "default_runner", fake)
    assert cli.main(["alerts", "generate", "--profile", "mei", "--count", "4"]) == 0
    out = capsys.readouterr().out
    assert "language zh" in out and "+ {project} 要{action}，你同意吗？" in out
    assert cli.main(["alerts", "show", "--lang", "zh"]) == 0
    out = capsys.readouterr().out
    assert "[zh] permission" in out and "alerts.json" in out
    monkeypatch.setattr(alerts, "default_runner", FakeClaude(stdout="nope"))
    assert cli.main(["alerts", "generate"]) == 1


def test_default_runner_keeps_our_hooks_out():
    import sys

    code = "import os, sys; print(os.environ.get('AFTERWORD_DISABLE'), sys.stdin.read() == '')"
    proc = alerts.default_runner([sys.executable, "-c", code], 5)
    assert proc.returncode == 0 and proc.stdout.split() == ["1", "True"]
    with pytest.raises(subprocess.TimeoutExpired):
        alerts.default_runner([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    assert not alerts._RUNNING


def test_cancel_running_ends_a_generate_in_progress(tmp_path):
    """Review finding: Generate ran `claude -p` with subprocess.run in an
    executor thread, so a daemon shutdown waited for it (up to 180 s) and it
    then still merged phrases. Now shutdown kills it and nothing is merged."""
    import sys
    import threading

    import tomllib

    cfg = build_config(tomllib.loads(CONFIG))[0]
    phrases = alerts.AlertPhrases(tmp_path / "alerts.json")
    script = "import time, json; time.sleep(30); print(json.dumps({'result': {'idle': ['{project} waits.']}}))"
    got = {}

    def go():
        t0 = time.monotonic()
        try:
            alerts.generate(cfg, phrases, "nova", 3,
                            runner=lambda argv, timeout: alerts.default_runner([sys.executable, "-c", script], timeout))
        except alerts.AlertError as exc:
            got["error"] = exc
        got["s"] = time.monotonic() - t0

    th = threading.Thread(target=go)
    th.start()
    assert wait_for(lambda: alerts._RUNNING or None, timeout=5)
    assert alerts.cancel_running() == 1
    th.join(10)
    assert not th.is_alive() and got["s"] < 5
    assert isinstance(got["error"], alerts.GenerateFailed) and "shutting down" in str(got["error"])
    assert not (tmp_path / "alerts.json").exists() and not alerts._RUNNING


def test_write_settings_keeps_comments_and_adds_the_table(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("# mine\n[daemon]\nport = 1234  # keep\n\n[profiles.nova]\nvoice = \"builtin:Ryan\"\n")
    alerts.write_settings(path, {"enabled": True, "events": ["idle"]}, ["nova"])
    text = path.read_text()
    assert "# mine" in text and "# keep" in text
    cfg, w = build_config(__import__("tomllib").loads(text))
    assert cfg.alerts.enabled and cfg.alerts.events == ("idle",) and not w
    alerts.write_settings(path, {"enabled": False, "permission_when": "now"}, ["nova"])
    cfg, _ = build_config(__import__("tomllib").loads(path.read_text()))
    assert not cfg.alerts.enabled and cfg.alerts.permission_when == "now" and cfg.alerts.events == ("idle",)
    with pytest.raises(alerts.AlertError, match="no profile"):
        alerts.write_settings(path, {"profile": "ghost"}, ["nova"])
    with pytest.raises(alerts.AlertError):
        alerts.settings_body({"enabled": "yes"})
    with pytest.raises(alerts.AlertError):
        alerts.settings_body({"events": ["nope"]})
    assert alerts.settings_body({"events": ["done", "idle"], "done_after_s": 90.0}) == \
        {"events": ["idle", "done"], "done_after_s": 90}


# --------------------------------------------------------------------------- #
# Hooks: never fail, print nothing                                              #
# --------------------------------------------------------------------------- #

BAD = [b"", b"not json", b"[]", b'{"session_id": 5, "tool_name": ["x"], "tool_input": "y"}',
       b'{"session_id": "s", "notification_type": 7, "message": {}}',
       b'{"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "' + b"x" * 200000 + b'"}}',
       b'{"session_id": "../../etc", "notification_type": "permission_prompt"}']


@pytest.mark.parametrize("event", ["permission-request", "notification"])
@pytest.mark.parametrize("stdin", BAD, ids=range(len(BAD)))
def test_alert_hooks_never_fail(home, say_stub, event, stdin):
    proc, elapsed = run_hook(stdin, args=(event,))
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b"" and elapsed < 3.0 * TIME_SCALE


@pytest.mark.parametrize("event", ["permission-request", "notification"])
def test_alert_hooks_never_fail_with_corrupt_state_and_config(home, say_stub, event):
    (home / "config.toml").write_text("[[[ broken")
    (home / "state").mkdir()
    (home / "state" / "s1.json").write_text("{nope")
    proc, _ = run_hook(perm() if event == "permission-request" else note(), args=(event,))
    assert proc.returncode == 0 and proc.stdout == b""


@needs_system_python
def test_alert_hooks_on_system_python(home, say_stub):
    """Python 3.9 reads [alerts] with the mini parser (events: the defaults)."""
    proc, _ = run_hook(perm(), args=("permission-request",), python=SYSTEM_PYTHON)
    assert proc.returncode == 0 and proc.stdout == b""
    assert state(home)["pending_permissions"][0]["action"] == "Remove node_modules directory"
    proc, _ = run_hook(note(), args=("notification",), python=SYSTEM_PYTHON)
    assert proc.returncode == 0 and proc.stdout == b""
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "shop-api needs your OK."


def test_hook_modules_stay_stdlib_only():
    import sys

    code = "import sys; sys.path.insert(0, %r); import alerting; print('ok')" % str(
        os.path.join(os.path.dirname(alerting.__file__)))
    out = subprocess.run([sys.executable, "-S", "-I", "-c", code], capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr


# --------------------------------------------------------------------------- #
# Hooks with the daemon down: pending state and the `say` fallback             #
# --------------------------------------------------------------------------- #


def test_away_permission_is_spoken_by_the_notification(home, say_stub):
    run_hook(perm(), args=("permission-request",))
    st = state(home)
    assert st["pending_permissions"][0]["tool"] == "Bash" and st["profile"] is None
    time.sleep(0.3)
    assert not say_stub.exists()  # "away": the request itself stays quiet
    run_hook(note(), args=("notification",))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "shop-api needs your OK."
    assert "pending_permissions" not in state(home)  # used up
    recs = [json.loads(line) for line in (home / "spool.jsonl").read_text().splitlines()]
    assert [r["event"] for r in recs] == ["PermissionRequest", "Notification"]
    assert recs[0]["decision"] == "info" and recs[1]["decision"] == "speak" and recs[1]["fallback"] == "say"
    assert recs[1]["alert"]["action"] == "Remove node_modules directory"
    # Imported later: an alert row, not counted as speech.
    from afterword.daemon.log import EventLog, ingest_spool

    events = EventLog(home / "events.db")
    try:
        assert ingest_spool(events) == 2
        rows = events.recent()
        assert [r["kind"] for r in rows] == ["alert", "hook"]
        assert rows[0]["status"] == "spoken" and "alert spoken by say fallback" in rows[0]["reason"]
        assert events.stats()["count"] == 0
    finally:
        events.close()


def test_now_permission_speaks_at_once_in_chinese(home, say_stub):
    cfg = CONFIG.replace('permission_when = "away"', 'permission_when = "now"').replace('profile = ""', '')
    cfg = cfg.replace("[alerts]\n", '[alerts]\nprofile = "mei"\n')
    (home / "config.toml").write_text(cfg)
    run_hook(perm(), args=("permission-request",))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "shop-api 需要你批准。"
    run_hook(note(), args=("notification",))
    time.sleep(0.4)
    assert say_stub.read_text().strip() == said  # not twice


def test_codex_permission_speaks_at_once_even_when_away(home, say_stub):
    # Codex has no Notification event: its PermissionRequest ("codex" argument)
    # is spoken at once under the default "away", and nothing is left pending.
    run_hook(perm(), args=("permission-request", "codex"))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "shop-api needs your OK."
    assert "pending_permissions" not in (state(home) or {})


@pytest.mark.parametrize("change", ["off", "muted", "muted_all", "fallback_none", "not_in_events"])
def test_fallback_stays_quiet(home, say_stub, change):
    if change == "off":
        (home / "config.toml").write_text(CONFIG.replace("enabled = true", "enabled = false"))
    elif change == "muted":
        common.save_state("s1", {"profile": None, "muted": True})
    elif change == "muted_all":
        (home / "daemon.json").write_text(json.dumps({"muted_all": True}))
    elif change == "fallback_none":
        (home / "config.toml").write_text(CONFIG.replace('fallback = "say"', 'fallback = "none"'))
    else:
        (home / "config.toml").write_text(CONFIG.replace('"idle", "done"', '"done"'))
    run_hook(note("idle_prompt" if change == "not_in_events" else "elicitation_dialog"), args=("notification",))
    time.sleep(0.4)
    assert not say_stub.exists()


def test_unrelated_notification_is_only_logged(home, say_stub):
    run_hook(note("auth_success"), args=("notification",))
    time.sleep(0.3)
    assert not say_stub.exists()
    rec = json.loads((home / "spool.jsonl").read_text().splitlines()[-1])
    assert rec["decision"] == "info" and "not an alert" in rec["reason"]


def test_done_is_timed_from_the_prompt(home, say_stub):
    common.save_state("s1", {"profile": None})
    proc, _ = run_hook({"session_id": "s1", "prompt": "do it", "prompt_id": "p1", "cwd": "/w/shop-api"},
                       args=("user-prompt-submit",))
    assert proc.stdout == b""  # voice off: still no tokens
    st = state(home)
    assert isinstance(st["turn_started"], float) and st["turn"] == "p1"
    run_hook(stop_payload("All done, no say tag.", session_id="s1", cwd="/w/shop-api", prompt_id="p1"))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "shop-api is done."
    assert "turn_started" not in state(home)
    rec = [json.loads(x) for x in (home / "spool.jsonl").read_text().splitlines()]
    assert [r["event"] for r in rec][-2:] == ["Stop", "Stop"] and rec[-1]["alert"]["event"] == "done"


def test_done_not_for_a_short_turn_or_a_spoken_one(home, say_stub):
    (home / "config.toml").write_text(CONFIG.replace("done_after_s = 0", "done_after_s = 30"))
    run_hook({"session_id": "s1", "prompt": "x", "cwd": "/w/p"}, args=("user-prompt-submit",))
    run_hook(stop_payload("short turn", session_id="s1"))
    time.sleep(0.4)
    assert not say_stub.exists()
    (home / "config.toml").write_text(CONFIG)
    common.save_state("s2", {"profile": "nova"})
    run_hook({"session_id": "s2", "prompt": "x", "cwd": "/w/p"}, args=("user-prompt-submit",))
    run_hook(stop_payload("<say>The line itself.</say>", session_id="s2"))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    time.sleep(0.3)
    assert say_stub.read_text().strip() == said == "The line itself."


def test_alerts_off_writes_no_turn_state(home):
    (home / "config.toml").write_text(CONFIG.replace("enabled = true", "enabled = false"))
    run_hook({"session_id": "s7", "prompt": "x"}, args=("user-prompt-submit",))
    run_hook(perm("s7"), args=("permission-request",))
    assert not (home / "state" / "s7.json").exists()


def test_duplicate_hooks_alert_once(home, say_stub):
    run_hook(perm(), args=("permission-request",))
    procs = [subprocess.Popen([__import__("sys").executable, str(__import__("conftest").HOOK), "notification"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    for p in procs:
        p.communicate(json.dumps(note()).encode(), timeout=10)
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    time.sleep(0.4)
    assert say_stub.read_text().strip() == said == "shop-api needs your OK."
    assert any("duplicate" in (r.get("reason") or "") for r in hook_log(home))


# --------------------------------------------------------------------------- #
# Through the daemon                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture
def daemon(home, monkeypatch):
    from test_daemon import RunningDaemon

    d = RunningDaemon(home, monkeypatch)
    yield d
    d.stop()


def post(d, path, body, headers=H):
    req = urllib.request.Request(d.base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_permission_pair_logs_one_alert_with_the_tool_description(daemon, home, say_stub):
    run_hook(perm(), args=("permission-request",))
    run_hook(note(), args=("notification",))
    rows = wait_for(lambda: [e for e in alert_rows(daemon) if e["status"] == "spoken"] or None)
    assert rows and len(alert_rows(daemon)) == 1, daemon.events()
    ev = rows[0]
    assert ev["hook_event"] == "Notification" and ev["profile"] == "nova"  # voice off: the first profile
    assert ev["parsed"]["alert"]["action"] == "Remove node_modules directory"
    tmpl = ev["parsed"]["alert"]["template"]
    if "{action}" in tmpl:
        assert "remove node_modules directory" in ev["text"]
    assert "shop-api" in ev["text"] or "{project}" not in tmpl
    info = [e for e in daemon.events() if e["hook_event"] == "PermissionRequest"]
    assert len(info) == 1 and info[0]["status"] == "info" and "pending" in info[0]["reason"]
    stats = daemon.get("/api/stats")
    assert stats["count"] == 0  # alerts are not speech
    assert not say_stub.exists()


def test_codex_permission_is_spoken_by_the_daemon_when_away(daemon, home, say_stub):
    run_hook(perm(), args=("permission-request", "codex"))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["status"] == "spoken"), None))
    assert ev["hook_event"] == "PermissionRequest" and ev["parsed"]["alert"]["trigger"] == "only"
    assert not say_stub.exists()


def test_every_alert_names_the_action_when_the_pool_requires_it(daemon, home):
    (home / "alerts.json").write_text(json.dumps({"en": {"permission": ["{project} needs your OK to {action}."]}}))
    run_hook(perm(), args=("permission-request",))
    run_hook(note(), args=("notification",))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["status"] == "spoken"), None))
    assert ev["text"] == "shop-api needs your OK to remove node_modules directory."


def test_now_mode_and_events_through_the_daemon(daemon, home):
    (home / "config.toml").write_text(CONFIG.replace('permission_when = "away"', 'permission_when = "now"')
                                      .replace('"input", "idle", "done"', '"input", "done"'))
    daemon.daemon.store.reload(force=True)
    run_hook(perm(), args=("permission-request",))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["status"] == "spoken"), None))
    assert ev["hook_event"] == "PermissionRequest"
    run_hook(note(), args=("notification",))
    run_hook(note("idle_prompt"), args=("notification",))
    run_hook(note("agent_needs_input"), args=("notification",))
    got = wait_for(lambda: len(alert_rows(daemon)) >= 2 and
                   len([e for e in daemon.events() if e["hook_event"] == "Notification"]) >= 3 and daemon.events())
    reasons = {e["reason"] for e in got if e["hook_event"] == "Notification" and e["status"] == "info"}
    assert "alert idle: not in [alerts] events" in reasons
    assert any("already spoken" in r for r in reasons)
    assert [e["parsed"]["alert"]["event"] for e in alert_rows(daemon)] == ["input", "permission"]


def test_alerts_speak_with_voice_off_but_not_when_muted(daemon, home):
    run_hook(note("elicitation_dialog", sid="v-off"), args=("notification",))
    assert wait_for(lambda: any(e["status"] == "spoken" for e in alert_rows(daemon)))
    common.save_state("m1", {"profile": "mei", "muted": True})
    run_hook(note("elicitation_dialog", sid="m1"), args=("notification",))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["session_id"] == "m1"), None))
    assert ev["status"] == "skipped" and ev["reason"] == "session muted"
    status, _ = post(daemon, "/api/mute", {"muted": True})
    assert status == 200
    run_hook(note("idle_prompt", sid="v-off"), args=("notification",))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["parsed"]["alert"]["event"] == "idle"), None))
    assert ev["status"] == "skipped" and ev["reason"] == "all muted"


def test_session_profile_and_language_pick_the_pool(daemon, home):
    common.save_state("zh1", {"profile": "mei"})
    run_hook(note("idle_prompt", sid="zh1"), args=("notification",))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["session_id"] == "zh1"), None))
    assert ev["profile"] == "mei" and ev["parsed"]["alert"]["lang"] == "zh" and ev["attrs"] == {}
    assert ev["parsed"]["alert"]["template"] in alerts.BUILTIN["zh"]["idle"]


def test_done_alert_through_the_daemon(daemon, home):
    run_hook({"session_id": "d1", "prompt": "x", "prompt_id": "p9", "cwd": "/w/shop-api"}, args=("user-prompt-submit",))
    run_hook(stop_payload("finished, nothing to say", session_id="d1", cwd="/w/shop-api", prompt_id="p9"))
    ev = wait_for(lambda: next((e for e in alert_rows(daemon) if e["session_id"] == "d1"), None))
    assert ev["hook_event"] == "Stop" and ev["parsed"]["alert"]["event"] == "done"
    stop = next(e for e in daemon.events() if e["kind"] == "hook" and e["hook_event"] == "Stop")
    assert stop["status"] == "skipped" and stop["reason"] == "session off"


def test_done_alert_not_when_the_turn_spoke(daemon, home):
    common.save_state("d2", {"profile": "nova"})
    run_hook({"session_id": "d2", "prompt": "x", "prompt_id": "p2", "cwd": "/w/p"}, args=("user-prompt-submit",))
    run_hook(stop_payload("<say>Spoken.</say>", session_id="d2", prompt_id="p2"))
    assert wait_for(lambda: any(e["text"] == "Spoken." and e["status"] == "spoken" for e in daemon.events()))
    time.sleep(0.2)
    assert not alert_rows(daemon)


def test_expired_alert_is_dropped(daemon):
    d = daemon.daemon

    def put():
        ev = d.events.insert(kind="alert", status="queued", text="late", session_id="x")
        d.queue.put(QueueItem(event_id=ev["id"], session_id="x", text="late", kind="alert",
                              enqueued_at=time.time() - 45))
        return ev["id"]

    eid = daemon.call(put)
    ev = wait_for(lambda: (e := daemon.get(f"/api/events/{eid}")) and e["status"] == "skipped" and e)
    assert ev["reason"].startswith("alert dropped: waited 45 s")


def test_session_off_keeps_waiting_alerts(daemon):
    d = daemon.daemon

    def go():
        d.queue.put(QueueItem(event_id=900, session_id="z", text="a", kind="alert"))
        d.queue.put(QueueItem(event_id=901, session_id="z", text="b"))
        dropped = d.apply_session_change("z", {"profile": None})
        kinds = [i.kind for i in d.queue._items]
        d.queue.clear()
        return dropped, kinds

    # The worker may pop one meanwhile; what matters is which kind is dropped.
    dropped, kinds = daemon.call(go)
    assert 901 in dropped and 900 not in dropped


def test_alerts_http_api(daemon, home):
    st = daemon.get("/api/status")
    assert st["config"]["alerts"] is True
    view = daemon.get("/api/alerts")
    assert view["settings"]["enabled"] is True and view["events"] == list(alerts.PLACEHOLDERS)
    assert view["pools"]["zh"]["idle"] == alerts.BUILTIN["zh"]["idle"] and view["custom"] == {}
    assert {p["name"]: p["pool_lang"] for p in view["profiles"]} == {"nova": "en", "mei": "zh"}
    assert view["default_profile"] == "nova" and view["generating"] is False
    # The header guard applies.
    status, _ = post(daemon, "/api/alerts", {"enabled": False}, headers={})
    assert status == 403
    status, v = post(daemon, "/api/alerts", {"enabled": False, "events": ["idle"], "profile": "mei"})
    assert status == 200 and v["settings"] == {**view["settings"], "enabled": False, "events": ["idle"], "profile": "mei"}
    assert "profile = \"mei\"" in (home / "config.toml").read_text()
    status, err = post(daemon, "/api/alerts", {"profile": "ghost"})
    assert status == 400 and "ghost" in err["error"]
    # Phrases.
    status, v = post(daemon, "/api/alerts/phrases", {"lang": "en", "pools": {"idle": ["Hey {project}."]}})
    assert status == 200 and v["pools"]["en"]["idle"] == ["Hey {project}."] and v["custom"] == {"en": ["idle"]}
    status, err = post(daemon, "/api/alerts/phrases", {"lang": "en", "pools": {"idle": ["{tool} idle"]}})
    assert status == 400 and "not available" in err["error"]
    status, v = post(daemon, "/api/alerts/phrases", {"lang": "en", "pools": None})
    assert status == 200 and v["custom"] == {}


def test_generate_and_hear_endpoints(daemon, home):
    fake = FakeClaude(ZH_REPLY)
    daemon.daemon.alert_runner = fake
    status, res = post(daemon, "/api/alerts/generate", {"profile": "mei", "count": 3})
    assert status == 200 and res["lang"] == "zh" and res["added"]["input"] == ["{project} 有个问题。"]
    assert fake.calls and "Chinese" in fake.calls[0][0][-1]
    daemon.daemon.alert_runner = FakeClaude(stdout="garbage")
    status, err = post(daemon, "/api/alerts/generate", {"profile": "mei"})
    assert status == 502 and "JSON" in err["error"]
    status, err = post(daemon, "/api/alerts/generate", {"count": 99})
    assert status == 400

    seen = []

    async def fake_preview(body):
        seen.append(body)
        return {"id": "p1", "url": "/api/lab/previews/p1.wav", "pace": 1.0}

    daemon.daemon.lab.preview = fake_preview
    status, res = post(daemon, "/api/alerts/hear", {"event": "done", "profile": "mei"})
    assert status == 200 and res["url"].startswith("/api/lab/") and "{" not in res["alert_text"]
    assert seen[-1]["profile"] == "mei" and "lang" not in seen[-1]
    status, res = post(daemon, "/api/alerts/hear", {"event": "permission", "phrase": "{project}: {action}?"})
    assert status == 200 and res["alert_text"] == "my-project: run the test suite?" and seen[-1]["profile"] == "nova"
    status, _ = post(daemon, "/api/alerts/hear", {"event": "nope"})
    assert status == 400
    status, _ = post(daemon, "/api/alerts/hear", {"event": "idle", "phrase": "{action}"})
    assert status == 400


# --------------------------------------------------------------------------- #
# Review fixes                                                                  #
# --------------------------------------------------------------------------- #


def _entry(tool_name, action, t, label=None):
    return {"tool_name": tool_name, "tool": label or alerting.tool_label(tool_name), "action": action, "t": t}


def test_take_pending_names_only_a_request_it_is_sure_of():
    now = 1000.0
    msg = "Claude needs your permission to use %s"

    def st(*entries):
        return {"pending_permissions": [dict(e) for e in entries]}

    rm = _entry("Bash", "Remove node_modules", now - 30)
    # The message names another tool: not the stored Bash request.
    s = st(rm)
    assert alerting.take_pending(s, msg % "Edit", now) is None and len(s["pending_permissions"]) == 1
    # A sandboxed network request has no PermissionRequest of its own.
    assert alerting.take_pending(st(rm), "Claude needs your permission to access the network", now) is None
    # Parallel requests (made together) for the same tool: ambiguous.
    a, b = _entry("Bash", "Run tests", now - 7), _entry("Bash", "Push", now - 6.5)
    assert alerting.take_pending(st(a, b), msg % "Bash", now) is None
    # Parallel requests for different tools: the one the message names.
    e, b2 = _entry("Edit", "edit app.py", now - 7), _entry("Bash", "Push", now - 6.9)
    s = st(e, b2)
    assert alerting.take_pending(s, msg % "Edit", now)["action"] == "edit app.py"
    assert [x["action"] for x in s["pending_permissions"]] == ["Push"]
    # One answered quickly, a later one waiting: the later one, and it is used up.
    s = st(rm, _entry("Bash", "Push", now - 6))
    assert alerting.take_pending(s, msg % "Bash", now)["action"] == "Push"
    assert [x["action"] for x in s["pending_permissions"]] == ["Remove node_modules"]
    # A message without a tool name (the terminal's): the newest, if unambiguous.
    assert alerting.take_pending(st(rm), "Claude needs your permission", now)["action"] == "Remove node_modules"
    # MCP tools: "github - create_issue (MCP)" and the like still match.
    mcp = _entry("mcp__github__create_issue", "create issue x", now - 6)
    assert alerting.take_pending(st(mcp), msg % "github - create_issue (MCP)", now)["action"] == "create issue x"
    # Expired or malformed entries are ignored.
    assert alerting.take_pending(st(_entry("Bash", "old", now - 700)), msg % "Bash", now) is None
    assert alerting.take_pending({"pending_permissions": [{"t": "x"}, 3, None]}, msg % "Bash", now) is None


def test_drop_answered_keeps_only_just_made_requests():
    now = 1000.0
    s = {"pending_permissions": [_entry("Bash", "old", now - 5), _entry("Edit", "new", now - 0.5)]}
    assert alerting.drop_answered(s, now) is True
    assert [e["action"] for e in s["pending_permissions"]] == ["new"]
    assert alerting.drop_answered(s, now) is False
    s = {"pending_permissions": [_entry("Bash", "old", now - 5)], "pending_permission": {"t": now}}
    assert alerting.drop_answered(s, now) is True and s == {}


def _age_pending(home, sid="s1", by=10.0):
    p = home / "state" / f"{sid}.json"
    st = json.loads(p.read_text())
    for e in st.get("pending_permissions", []):
        e["t"] -= by
    p.write_text(json.dumps(st))


def _spooled(home):
    return [json.loads(x) for x in (home / "spool.jsonl").read_text().splitlines()]


def test_notification_does_not_name_a_request_answered_before_new_text(home, say_stub):
    """Review finding: a request answered within ~6 s stayed stored, and a
    later permission_prompt (a network request, which has no
    PermissionRequest) named it. New assistant text (MessageDisplay) now
    means it was answered."""
    run_hook(perm(), args=("permission-request",))
    _age_pending(home)
    run_hook({"session_id": "s1", "message_id": "m1", "index": 0, "delta": "Now the next step."},
             args=("message-display",))
    assert "pending_permissions" not in state(home)
    run_hook(note(), args=("notification",))
    assert wait_for(lambda: say_stub.exists() and say_stub.read_text().strip()) == "shop-api needs your OK."
    rec = _spooled(home)[-1]
    assert rec["event"] == "Notification" and "action" not in rec["alert"]


def test_message_display_keeps_a_request_just_made(home):
    run_hook(perm(), args=("permission-request",))
    run_hook({"session_id": "s1", "message_id": "m1", "index": 0, "delta": "text"}, args=("message-display",))
    assert len(state(home)["pending_permissions"]) == 1


def test_parallel_requests_the_notification_names_its_own_tool(home, say_stub):
    run_hook(perm(tool_name="Edit", tool_input={"file_path": "/w/shop-api/app.py", "old_string": "a"}),
             args=("permission-request",))
    run_hook(perm(), args=("permission-request",))
    run_hook(note(message="Claude needs your permission to use Edit"), args=("notification",))
    wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    rec = _spooled(home)[-1]
    assert rec["alert"]["action"] == "edit app.py" and rec["alert"]["tool"] == "Edit"
    assert [e["tool"] for e in state(home)["pending_permissions"]] == ["Bash"]


def test_a_second_prompt_for_the_same_tool_is_not_a_duplicate(home, say_stub):
    """Review finding: the alert claim lasted 15 s and is keyed on content,
    so a second Bash prompt whose notification came within 15 s (same
    message) was dropped as a duplicate hook. Duplicate hooks run within a
    second; the claim now lasts 3 s."""
    run_hook(perm(description="Run the tests"), args=("permission-request",))
    run_hook(note(), args=("notification",))
    assert wait_for(lambda: say_stub.exists() and say_stub.read_text().count("\n") == 1)
    for f in (home / "seen").iterdir():  # 4 s later
        t = f.stat().st_mtime - 4
        os.utime(f, (t, t))
    _age_pending(home, by=4)
    run_hook(perm(description="Push to origin"), args=("permission-request",))
    run_hook(note(), args=("notification",))
    assert wait_for(lambda: say_stub.read_text().count("\n") == 2)
    recs = [r for r in _spooled(home) if r["event"] == "Notification"]
    assert [r["alert"].get("action") for r in recs] == ["Run the tests", "Push to origin"]
    assert "pending_permissions" not in state(home)
    assert not any("duplicate" in (r.get("reason") or "") for r in hook_log(home))


def test_done_has_no_minutes_for_a_turn_under_a_minute(home, say_stub):
    """Review finding: {minutes} was at least 1, so with a low done_after_s a
    12 s turn could be "finished after 1 minute"."""
    run_hook({"session_id": "s1", "prompt": "x", "prompt_id": "p1", "cwd": "/w/shop-api"},
             args=("user-prompt-submit",))
    run_hook(stop_payload("no tag", session_id="s1", cwd="/w/shop-api", prompt_id="p1"))
    wait_for(lambda: say_stub.exists())
    done = _spooled(home)[-1]["alert"]
    assert done["event"] == "done" and "minutes" not in done and done["turn_s"] < 60
    common.save_state("s2", {"profile": None, "turn_started": time.time() - 130, "turn": "p2"})
    run_hook(stop_payload("no tag", session_id="s2", cwd="/w/shop-api", prompt_id="p2"))
    assert wait_for(lambda: _spooled(home)[-1].get("session_id") == "s2")
    assert _spooled(home)[-1]["alert"]["minutes"] == 2


def test_daemon_skips_minutes_phrases_for_a_short_turn(daemon, home):
    (home / "alerts.json").write_text(json.dumps({"en": {"done": [
        "{project} finished after {minutes} minutes.", "{project} is done.", "Done after {minutes} minutes."]}}))
    for i in range(4):
        sid = f"short{i}"
        run_hook({"session_id": sid, "prompt": "x", "prompt_id": "p", "cwd": "/w/shop-api"},
                 args=("user-prompt-submit",))
        run_hook(stop_payload("no tag", session_id=sid, cwd="/w/shop-api", prompt_id="p"))
    rows = wait_for(lambda: len(alert_rows(daemon)) >= 4 and alert_rows(daemon))
    assert {e["text"] for e in rows} == {"shop-api is done."}
    # An older hook that still sends minutes for a short turn: not used either.
    ack = daemon.call(lambda: daemon.daemon._speak_alert(
        daemon.daemon.store.get(), {"event": "done", "trigger": "stop", "turn_s": 12.0, "minutes": 1},
        {"cwd": "/w/shop-api", "session_id": "x"}, None, None))
    ev = daemon.get(f"/api/events/{ack['event_id']}")
    assert ev["text"] == "shop-api is done."


def test_state_writes_do_not_undo_a_gui_change(home):
    """Review finding: hooks did an unlocked load-modify-save of the state
    file, so a GUI mute landing in between was written back over. Both
    sides now hold the state lock for the whole read-modify-write."""
    import threading

    from afterword.daemon import state as session_state

    common.save_state("s1", {"profile": None})
    with common.state_lock() as held:
        assert held
        proc = subprocess.Popen([__import__("sys").executable, str(__import__("conftest").HOOK),
                                 "permission-request"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        proc.stdin.write(json.dumps(perm()).encode())
        proc.stdin.close()
        time.sleep(0.4)  # the hook has read the state and waits for the lock
        st = common.load_state("s1")
        st["muted"] = True  # what set_session writes while holding the lock
        common.save_state("s1", st)
    assert proc.wait(10) == 0
    st = state(home)
    assert st["muted"] is True and st["pending_permissions"][0]["tool"] == "Bash"

    # set_session waits for a hook's read-modify-write.
    done = []
    with common.state_lock():
        th = threading.Thread(target=lambda: done.append(session_state.set_session("s1", muted=False)))
        th.start()
        time.sleep(0.2)
        assert not done
    th.join(5)
    assert done and done[0]["muted"] is False and done[0]["pending_permissions"]


def test_state_lock_gives_up_instead_of_hanging(home):
    with common.state_lock() as held:
        t0 = time.monotonic()
        with common.state_lock(wait=0.2) as again:
            assert held and not again
        assert 0.15 < time.monotonic() - t0 < 2
    (home / "state" / ".lock").unlink()
    os.mkfifo(home / "state" / ".lock")
    with common.state_lock() as held:  # not a regular file: no lock, no hang
        assert held is False
    st = common.update_state("s9", lambda s: s.update(x=1))
    assert st["x"] == 1 and common.load_state("s9")["x"] == 1


def test_bookkeeping_only_state_files_are_pruned(home):
    """Review finding: with alerts on, every session got a state file for
    its turn bookkeeping, and nothing removed them."""
    from afterword.daemon import state as session_state

    old = time.time() - 8 * 86400
    files = {
        "book": {"profile": None, "turn_started": 1.0, "turn": "p"},
        "voiced": {"profile": "nova", "turn_started": 1.0},
        "was_voiced": {"profile": None, "last_profile": "nova"},
        "muted": {"profile": None, "muted": True},
        "fresh": {"profile": None, "turn_started": 1.0},
    }
    for sid, data in files.items():
        common.save_state(sid, data)
        if sid != "fresh":
            os.utime(common.state_file(sid), (old, old))
    (home / "state" / "bad.json").write_text("{nope")
    os.utime(home / "state" / "bad.json", (old, old))
    assert session_state.prune_states(force=True) == 1
    left = sorted(p.stem for p in (home / "state").glob("*.json"))
    assert left == ["bad", "fresh", "muted", "voiced", "was_voiced"]
    assert session_state.prune_states() == 0  # at most hourly


def test_mini_toml_arrays_match_tomllib():
    import tomllib

    text = ('[alerts]\nevents = ["permission", \'input\',]  # c\nempty = []\nnums = [1, 2.5, true]\n'
            'multi = [\n  "a",  # one\n  "b,]",\n]\nafter = 1\n[x]\ny = "z"\n')
    assert common._mini_toml(text) == tomllib.loads(text)
    mini = common._mini_toml('a = [[1]]\nb = [{x = 1}]\nc = ["x" "y"]\nd = ["open"\ne = 2\n')
    assert mini == {}  # unreadable: left out (d swallows the broken line after it)


@needs_system_python
def test_system_python_fallback_honours_the_events_setting(home, say_stub):
    """Review finding: Python 3.9 could not read `events`, so the `say`
    fallback spoke "done" although the user had turned it off."""
    (home / "config.toml").write_text(CONFIG.replace('events = ["permission", "input", "idle", "done"]',
                                                     'events = [\n  "permission",\n  "input",\n]'))
    run_hook({"session_id": "s1", "prompt": "x", "prompt_id": "p1", "cwd": "/w/shop-api"},
             args=("user-prompt-submit",), python=SYSTEM_PYTHON)
    proc, _ = run_hook(stop_payload("no tag", session_id="s1", cwd="/w/shop-api", prompt_id="p1"),
                       python=SYSTEM_PYTHON)
    assert proc.returncode == 0 and proc.stdout == b""
    run_hook(note("idle_prompt"), args=("notification",), python=SYSTEM_PYTHON)
    time.sleep(0.4)
    assert not say_stub.exists()
    run_hook(note("elicitation_dialog"), args=("notification",), python=SYSTEM_PYTHON)
    assert wait_for(lambda: say_stub.exists() and say_stub.read_text().strip()) == "shop-api needs your input."


def test_generate_refuses_a_profile_that_is_not_text(tmp_path, daemon):
    import tomllib

    cfg = build_config(tomllib.loads(CONFIG))[0]
    for bad in (["a"], {"x": 1}, 3):
        with pytest.raises(alerts.AlertError, match="profile"):
            alerts.generate(cfg, alerts.AlertPhrases(tmp_path / "a.json"), bad, 3, runner=FakeClaude(ZH_REPLY))
        status, err = post(daemon, "/api/alerts/generate", {"profile": bad})
        assert status == 400 and "profile" in err["error"]
