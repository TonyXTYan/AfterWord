"""The Config page's Daemon section (afterword/ui/index.html, between
"// ---------- daemon section" and "// ---------- end daemon section"), run
in node: the mode line, which controls are enabled and why (restart only
under launchd, start at login only for an installed service of this home),
the restart's new-pid check, sizes and uptimes, and the status text that
Copy puts on the clipboard. Plus static checks of the wiring: the section
comes first, every control is hooked to the endpoint docs/gui-api.md names,
POSTs go through api() (which sends X-AfterWord), and the whole page script
still parses. The node parts are skipped when node is not installed."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const src = require("fs").readFileSync(0, "utf8");
const D = new Function(src + "\nreturn {dmBytes, dmUptime, dmModeLine, dmRestart, dmLogin, dmLastExit, dmStatusText, dmRestarted};")();
const [fn, args] = JSON.parse(process.argv[1]);
console.log(JSON.stringify(D[fn](...args)));
"""


def _text() -> str:
    return UI.read_text(encoding="utf-8")


def _block() -> str:
    m = re.search(r"// ---------- daemon section\n(.*?)\n// ---------- end daemon section", _text(), re.S)
    assert m, "daemon section block not found in index.html"
    return m.group(1)


def js(fn, *args):
    proc = subprocess.run([NODE, "-e", HARNESS, json.dumps([fn, list(args)])], input=_block(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def svc(**over):
    d = {
        "label": "io.github.tonyxtyan.afterword", "installed": True, "loaded": True, "running": True,
        "service_pid": 4321, "pid": 4321, "last_exit": "0", "error": None, "login": True,
        "summary": "loaded, running pid 4321, last exit 0, start at login: on", "mode": "service",
        "service_home": "/Users/me/.afterword", "other_home": False, "install_command": "afterword install-service",
        "restart_exit": 75, "logs": {"daemon": "/Users/me/.afterword/afterwordd.log", "service": "/Users/me/.afterword/service.log"},
        "version": "0.3.4", "uptime_s": 3 * 3600 + 125, "home": "/Users/me/.afterword",
        "storage": {"db_bytes": 1258291, "events": 3400, "clips": 12, "clips_bytes": 3565158, "keep_days": 7, "history": 50},
    }
    d.update(over)
    return d


@needs_node
def test_sizes_and_uptimes():
    assert [js("dmBytes", n) for n in (0, 999, 2048, 1258291, 5 * 1024 ** 3, -1)] == \
        ["0 B", "999 B", "2.0 KB", "1.2 MB", "5.0 GB", "—"]
    assert [js("dmUptime", s) for s in (5, 59, 60, 3600, 3 * 3600 + 125, 86400 * 2 + 3600 * 5, 86400)] == \
        ["5 s", "59 s", "1 min", "1 h", "3 h 2 min", "2 d 5 h", "1 d"]


@needs_node
def test_mode_lines():
    assert js("dmModeLine", svc()) == "Running under launchd, pid 4321, up 3 h 2 min"
    assert js("dmModeLine", svc(mode="manual", pid=99, uptime_s=30)) == \
        "Started by hand (pid 99, up 30 s): it stops when its terminal closes"
    line = js("dmModeLine", svc(mode="unknown", error="launchctl not found"))
    assert "launchctl gave no answer (launchctl not found)" in line


@needs_node
def test_restart_only_under_launchd():
    assert js("dmRestart", svc())["enabled"] is True
    manual = js("dmRestart", svc(mode="manual"))
    assert manual == {"enabled": False,
                      "title": "Started by hand: restart it in its terminal, or run afterword install-service"}
    assert js("dmRestart", svc(mode="unknown"))["enabled"] is False


@needs_node
def test_login_switch_states():
    on = js("dmLogin", svc())
    assert on["enabled"] and on["on"] is True
    off = js("dmLogin", svc(login=False))
    assert off["enabled"] and off["on"] is False and "keeps running" in off["hint"]
    missing = js("dmLogin", svc(installed=False, login=True))
    assert missing["enabled"] is False and "afterword install-service" in missing["title"]
    other = js("dmLogin", svc(other_home=True, service_home="/Users/me/.afterword"))
    assert other["enabled"] is False and "another AfterWord home" in other["title"]
    unknown = js("dmLogin", svc(login=None))
    assert unknown["enabled"] is False and unknown["title"] == "launchctl gave no answer"


@needs_node
def test_last_exit_and_restart_check():
    assert js("dmLastExit", svc(last_exit="75: EX_TEMPFAIL")) == "75: EX_TEMPFAIL (a restart from this page)"
    assert js("dmLastExit", svc(last_exit="0")) == "0"
    assert js("dmLastExit", svc(last_exit=None)) is None
    assert js("dmRestarted", 100, 101) is True
    assert js("dmRestarted", 100, 100) is False  # still the old daemon
    assert js("dmRestarted", 100, None) is False  # still down


@needs_node
def test_status_text_reads_like_afterword_status():
    text = js("dmStatusText", svc())
    assert text.splitlines() == [
        "afterwordd 0.3.4  pid 4321  up 3 h 2 min",
        "  mode      under launchd",
        "  service   loaded, running pid 4321, last exit 0, start at login: on",
        "  home      /Users/me/.afterword",
        "  storage   events.db 1.2 MB (3400 events), 12 clips 3.4 MB, keep_days 7, history 50",
        "  logs      /Users/me/.afterword/afterwordd.log, /Users/me/.afterword/service.log",
    ]
    assert "started by hand" in js("dmStatusText", svc(mode="manual"))


def test_daemon_is_the_first_config_section():
    t = _text()
    nav = re.search(r'<nav class="cfg-index".*?</nav>', t, re.S).group(0)
    assert re.findall(r'data-sec="([^"]+)"', nav)[0] == "cfg-daemon"
    body = t[t.index('<main id="view-config"'):]
    assert re.findall(r'<section class="panel cfg-sec" id="([^"]+)"', body)[0] == "cfg-daemon"


def test_wiring():
    t = _text()
    m = re.search(r"const DaemonPanel = \(\(\) => \{(.*?)\n\}\)\(\);", t, re.S)
    assert m, "DaemonPanel not found"
    panel = m.group(1)
    for call in ('api("GET", "/api/service")', 'api("POST", "/api/service", {login: want})',
                 'api("POST", "/api/service/restart")', 'api("POST", "/api/service/prune")',
                 'api("POST", "/api/service/reveal")', 'api("GET", `/api/logs/${name}?lines=200`)'):
        assert call in panel, call
    assert "fetch(\"/api/status\"" in panel  # the restart waits for a new pid there
    assert "sse.on(\"open\"" in panel  # and looks again as soon as the stream is back
    assert "DM_RESTART_WAIT_MS" in panel and "const DM_RESTART_WAIT_MS = 45000;" in _block()
    for el in ("dm-login", "dm-restart", "dm-prune", "dm-reveal", "dm-copy", "dm-log-daemon", "dm-log-service",
               "dm-logbox", "dm-log", "dm-meta", "dm-msg"):
        assert f'id="{el}"' in t, el
    assert re.search(r'id="dm-login" class="dm-switch" type="button" aria-pressed="false"', t)
    # The Config page loads and polls the section with the others.
    assert t.count("DaemonPanel.load();") == 2


@needs_node
def test_page_script_parses():
    t = _text()
    script = t[t.rindex("<script>") + len("<script>"):t.rindex("</script>")]
    proc = subprocess.run([NODE, "-e", "new Function(require('fs').readFileSync(0, 'utf8'))"], input=script,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr


def test_review_fixes_are_wired():
    """PR review of 0.3.4: the restart waits on the pid that answered the
    POST, a stale flash cannot clear a newer note, and a log response that
    arrives after another Show or a Hide is dropped."""
    t = _text()
    panel = re.search(r"const DaemonPanel = \(\(\) => \{(.*?)\n\}\)\(\);", t, re.S).group(1)
    assert "r.data && isNum(r.data.pid) ? r.data.pid : old" in panel
    assert "restarting = {old: going," in panel
    assert "const my = ++logSeq;" in panel and "if (my !== logSeq) return;" in panel
    assert "function hideLog() { logSeq++;" in panel
    assert "the restart did not happen" in panel  # the old pid still answering is not "did not come back"
    note = re.search(r"function note\(id, text, kind = \"info\"\) \{(.*?)\}\n", t).group(1)
    assert "el._tok = (el._tok || 0) + 1" in note
