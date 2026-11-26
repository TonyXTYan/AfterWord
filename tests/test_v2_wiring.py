"""Cross-cutting checks for the v2 features (docs/design-v2.md): the plugin
manifest and `install-hooks` register the same hooks, a fresh home's default
config is clean on both the daemon and the hook side, every hook module is
stdlib-only on the oldest supported Python, every write route is guarded,
and every hook event survives type-confused payloads in a "busy" home."""

import json
import subprocess
import sys
import time
import tomllib

import pytest
from fastapi.testclient import TestClient

from conftest import ROOT, TIME_SCALE, run_hook, SYSTEM_PYTHON, needs_system_python, require_system_python

HOOKS_DIR = ROOT / "afterword" / "hooks"
EVENT_ARGS = ("session-start", "user-prompt-submit", "message-display", "stop", "permission-request", "notification")


# --------------------------------------------------------------------------- #
# Manifest, install-hooks, hook dispatcher                                      #
# --------------------------------------------------------------------------- #


def test_plugin_manifest_matches_install_hooks_and_the_hook_dispatcher():
    from afterword import cli
    from afterword.hooks import afterword_hook

    plugin = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(plugin) == set(cli.HOOK_EVENTS)
    assert {arg for arg, _ in cli.HOOK_EVENTS.values()} == set(EVENT_ARGS)
    for event, (arg, extra) in cli.HOOK_EVENTS.items():
        (group,) = plugin[event]
        (hook,) = group["hooks"]
        assert hook["type"] == "command"
        assert hook["command"].endswith("afterword_hook.py\" " + arg), (event, hook["command"])
        # Same async flag and timeout in both ways of installing.
        assert {k: v for k, v in hook.items() if k not in ("type", "command")} == extra, event
        # The hook knows the argument and maps it to this event.
        assert afterword_hook._EVENTS[arg] == event and afterword_hook._EVENTS[event] == event


# --------------------------------------------------------------------------- #
# Default config in a fresh home                                                #
# --------------------------------------------------------------------------- #


def test_fresh_home_default_config_loads_clean_on_both_sides(aw_home):
    from afterword.daemon import config
    from afterword.hooks import alerting, common

    store = config.ConfigStore()
    assert store.ensure_default_file() is True
    assert store.ensure_default_file() is False  # never overwritten
    path = aw_home / "config.toml"
    assert path.read_text() == config.DEFAULT_CONFIG_TOML
    cfg = store.get()
    assert store.error is None and store.warnings == []
    assert cfg.daemon.stream is True and cfg.daemon.early_say is True and cfg.alerts.enabled is False
    assert list(cfg.profiles) == ["nova", "ember"] and cfg.pronounce == ()

    # The hook on Python < 3.11 reads the same file with its mini parser.
    text = path.read_text()
    mini = common._mini_toml(text)
    assert mini == tomllib.loads(text)
    hook_alerts = alerting.settings(mini)
    assert hook_alerts["enabled"] is cfg.alerts.enabled
    assert tuple(hook_alerts["events"]) == cfg.alerts.events
    assert hook_alerts["done_after_s"] == cfg.alerts.done_after_s
    assert common.early_say(mini) is cfg.daemon.early_say
    assert common.fallback_mode(mini) == cfg.daemon.fallback
    assert common.profile_max_chars(mini, "nova") == cfg.profile("nova").max_chars


@needs_system_python
def test_system_python_reads_the_default_config_like_tomllib(aw_home):
    from afterword.daemon import config

    path = aw_home / "config.toml"
    path.write_text(config.DEFAULT_CONFIG_TOML)
    code = (
        "import json, sys; sys.path.insert(0, %r); import common; "
        "print(json.dumps(common._mini_toml(open(%r, encoding='utf-8').read())))"
    ) % (str(HOOKS_DIR), str(path))
    out = subprocess.run([SYSTEM_PYTHON, "-S", "-I", "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == json.loads(json.dumps(tomllib.loads(config.DEFAULT_CONFIG_TOML)))


# --------------------------------------------------------------------------- #
# Hook modules: stdlib only, oldest Python                                      #
# --------------------------------------------------------------------------- #


def _hook_modules():
    return sorted(p.stem for p in HOOKS_DIR.glob("*.py") if p.stem != "__init__")


@pytest.mark.parametrize("python", [sys.executable, SYSTEM_PYTHON])
def test_every_hook_module_imports_without_site_packages(python):
    if python == SYSTEM_PYTHON:
        require_system_python()
    mods = _hook_modules()
    assert {"afterword_hook", "alerting", "common", "earlysay", "pronounce", "scriptsplit", "tags"} <= set(mods)
    code = "import sys; sys.path.insert(0, %r)\n" % str(HOOKS_DIR)
    code += "".join("import %s\n" % m for m in mods)
    code += "print(sys.version_info[:2] >= (3, 9))"
    out = subprocess.run([python, "-S", "-I", "-c", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.strip() == "True", out.stderr


def test_missing_system_python_fails_on_ci(monkeypatch):
    import conftest

    monkeypatch.setattr(conftest, "SYSTEM_PYTHON", "/nonexistent/python3")
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        conftest.require_system_python()
    monkeypatch.delenv("CI")
    with pytest.raises(pytest.skip.Exception):
        conftest.require_system_python()


def test_every_hook_file_is_python_3_9_syntax():
    """Static check that runs even where /usr/bin/python3 is missing: no
    match statements, parenthesised context managers or other 3.10+ syntax
    in any file the hook can load (including the say runner's source)."""
    import ast

    from afterword.hooks import afterword_hook

    files = sorted(HOOKS_DIR.glob("*.py"))
    assert len(files) >= 8
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))
    ast.parse(afterword_hook._SAY_RUNNER, filename="_SAY_RUNNER", feature_version=(3, 9))


# --------------------------------------------------------------------------- #
# HTTP: every write route is guarded                                            #
# --------------------------------------------------------------------------- #


def test_every_write_route_needs_the_header(aw_home, monkeypatch):
    from test_lab import FakeEngine, _daemon

    from afterword.daemon.web import build_app

    app = build_app(_daemon(aw_home, monkeypatch, FakeEngine()), 47800)
    client = TestClient(app, base_url="http://127.0.0.1:47800", raise_server_exceptions=False)
    checked = []
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or ()):
            if method in ("GET", "HEAD"):
                continue
            path = route.path.replace("{", "").replace("}", "")  # any value for a path parameter
            res = client.request(method, path, json={})
            assert res.status_code == 403 and "X-AfterWord" in res.json()["error"], (method, route.path)
            checked.append(route.path)
    # The v2 routes are among them.
    for path in ("/api/pronounce", "/api/alerts", "/api/alerts/phrases", "/api/alerts/generate",
                 "/api/alerts/hear", "/api/lab/explore", "/api/lab/explore/cursor", "/api/lab/explore/stop",
                 "/api/lab/stars", "/api/lab/stars/{star_id}", "/api/lab/stability"):
        assert path in checked, path


# --------------------------------------------------------------------------- #
# Every hook event, type-confused payloads, a home with state in it             #
# --------------------------------------------------------------------------- #

BUSY_CONFIG = """\
[daemon]
fallback = "say"
early_say = true

[alerts]
enabled = true
events = ["permission", "input", "done", "idle"]
permission_when = "now"
done_after_s = 0

[profiles.nova]
voice = "builtin:Serena"
lang = "en"
say_voice = "Samantha"

[profiles.nova.langs.zh]
say_voice = "Tingting"

[pronounce]
"uv" = "you vee"
"""

CONFUSED = [
    # every field of every event, each with the wrong type
    {"session_id": "s1", "cwd": 5, "prompt": ["x"], "prompt_id": {"a": 1}, "turn_id": None, "message_id": [],
     "index": "zero", "final": "yes", "delta": 7, "last_assistant_message": {"say": 1}, "tool_name": 3,
     "tool_input": [1, 2], "notification_type": ["idle_prompt"], "message": 9, "source": 1.5},
    # plausible types, odd values
    {"session_id": "s1", "cwd": "/w/" + "p" * 5000, "prompt": "/afterword:voice " + "x" * 300, "prompt_id": "P1",
     "turn_id": "T1", "message_id": "M1", "index": -3, "final": True,
     "delta": "<say lang='zz' pace='nan'>" + "字" * 3000 + "</say>\n", "notification_type": "idle_prompt",
     "last_assistant_message": "<say>" * 50 + "</say>" * 3, "tool_name": "mcp__x__y",
     "tool_input": {"description": None, "command": {"nested": True}}, "message": "needs permission to use Bash"},
    # a huge index and float timestamps where strings are expected
    {"session_id": "s1", "cwd": "/w/demo", "prompt_id": "P1", "message_id": "M2", "index": 10 ** 12, "final": False,
     "delta": "<say>Hi there, 用 uv 安装。</say>\n\n", "notification_type": "permission_prompt", "message": None,
     "tool_name": "Bash", "tool_input": {"description": 1.5e308}},
]


@pytest.mark.parametrize("arg", EVENT_ARGS)
def test_every_event_survives_confused_payloads_in_a_busy_home(aw_home, say_stub, arg):
    (aw_home / "config.toml").write_text(BUSY_CONFIG)
    state_dir = aw_home / "state"
    state_dir.mkdir()
    (state_dir / "s1.json").write_text(json.dumps({
        "profile": "nova", "turn_started": time.time() - 120, "turn": "P1",
        "pending_permissions": [{"tool_name": "Bash", "tool": "Bash", "action": "x", "t": time.time()}],
    }))
    stream = aw_home / "stream"
    stream.mkdir()
    (stream / "s1.json").write_text('{"messages": {"M1": {"batches": {"0": 5}, "sent": "x"}}}')
    for payload in CONFUSED:
        proc, elapsed = run_hook(dict(payload, hook_event_name="ignored"), args=(arg,))
        assert proc.returncode == 0 and proc.stderr == b"", (arg, proc.stderr)
        assert elapsed < 5.0 * TIME_SCALE, (arg, elapsed)
        if proc.stdout:  # only the documented JSON, only from the synchronous hooks
            assert arg in ("session-start", "user-prompt-submit"), (arg, proc.stdout)
            out = json.loads(proc.stdout)
            assert set(out) <= {"hookSpecificOutput", "systemMessage", "suppressOutput"}, out
    log = [json.loads(line) for line in (aw_home / "hook.log").read_text().splitlines() if line.strip()] \
        if (aw_home / "hook.log").exists() else []
    assert not [r for r in log if r.get("msg") == "hook crashed"], log
