"""The GUI's keyboard shortcuts and fix-a-word popover logic (afterword/ui/index.html),
run in node against stubbed DOM helpers: the key to action mapping (what is ignored
while typing, with modifiers, on the Voice lab explorer), the actions that click the
existing controls, row selection on the Transcript, and the pure parts of fix-a-word
(which selections qualify, the POST body that keeps existing entries). Skipped when
node is not installed."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const src = require("fs").readFileSync(0, "utf8");
const [keysBlock, fixBlock] = JSON.parse(src);
const clicks = [], flashes = [], toggled = [], hashes = [], listeners = [];
const mkEl = (id) => ({id, clicks: 0, click() { clicks.push(id); }, focus() { this.focused = true; }, select() {}});
const els = {};
const $ = (id) => els[id] || (els[id] = mkEl(id));
const mkRow = (id, opts = {}) => ({
  dataset: {id: String(id)}, scrolled: 0, scrollIntoView() { this.scrolled++; },
  querySelector(sel) {
    if (sel === '[data-act="play"]') return opts.noClip ? null : mkEl("play" + id);
    if (sel === ".line") return opts.quiet ? null : {textContent: "line " + id};
    return null;
  },
});
let rows = [];
els.rows = {querySelectorAll: () => rows, addEventListener() {}};
let bodyClasses = new Set(["tab-log"]);
const written = [];
const env = {
  $, h: (tag, props, ...kids) => ({tag, props, kids, remove() {}, append() {}, focus() {}}),
  document: {
    body: {classList: {contains: (c) => bodyClasses.has(c)}, append() {}},
    head: {appendChild: (n) => { n.textContent = ""; return n; }},
    activeElement: null,
  },
  window: {addEventListener: (t, fn, o) => listeners.push({t, fn, o})},
  location: {set hash(v) { hashes.push(v); }, get hash() { return hashes[hashes.length - 1] || ""; }},
  navigator: {clipboard: {writeText: async (t) => { written.push(t); }}},
  flashNote: (...a) => flashes.push(a),
  toggleDetail: (id) => toggled.push(id),
  fwClose: () => clicks.push("fwClose"),
  Promise, Number, Set,
};
const names = Object.keys(env);
const fn = (code, ret) => new Function(...names, code + "\nreturn " + ret + ";")(...names.map((n) => env[n]));
// the keydown listener at the end of the block needs a document that can take one
env.document.addEventListener = () => {};
const K = fn(keysBlock, "{keyAction, keyRun, keyMove, keyState, keyTyping, keyRows}");
const out = {};
const ev = (key, extra = {}) => ({key, target: {tagName: "BODY"}, ...extra});
const base = {page: "log", explorer: false, help: false, fix: false};
const act = (key, extra, ctx) => K.keyAction(ev(key, extra), {...base, ...(ctx || {})});

// 1. the mapping
out.map = Object.fromEntries(["?", "/", "1", "2", "3", "4", "5", ".", "n", "m", "j", "k", "Enter", "p", "c", "x", "Escape"].map((k) => [k, act(k)]));
out.other_page = Object.fromEntries(["j", "k", "Enter", "p", "c", "m", "3"].map((k) => [k, act(k, {}, {page: "config"})]));

// 2. ignored while typing, with modifiers, in the explorer
out.typing = ["INPUT", "TEXTAREA", "SELECT"].map((t) => act("m", {target: {tagName: t}}));
out.editable = act("m", {target: {tagName: "DIV", isContentEditable: true}});
out.mods = [{metaKey: true}, {ctrlKey: true}, {altKey: true}].map((m) => act("m", m));
out.explorer = Object.fromEntries(["j", "k", "x", "s", " ", "m", "1", "?"].map((k) => [JSON.stringify(k), act(k, {}, {page: "lab", explorer: true})]));
out.lab_idle = act("j", {}, {page: "lab"});
out.enter_on_button = act("Enter", {target: {tagName: "BUTTON"}});
out.nokey = K.keyAction({target: {tagName: "BODY"}}, base);

// 3. overlays: Esc closes the popover first, then help; help swallows the other keys
out.esc = [act("Escape", {}, {fix: true, help: true}), act("Escape", {}, {help: true}), act("Escape", {target: {tagName: "INPUT"}}, {fix: true})];
out.in_help = [act("m", {}, {help: true}), act("?", {}, {help: true}), act("j", {}, {help: true})];

// 4. actions click the existing controls
K.keyRun("stop"); K.keyRun("skip"); K.keyRun("mute");
out.clicks = clicks.slice(); clicks.length = 0;
K.keyRun("page:2"); K.keyRun("page:0");   // on tab-log: page 0 is a no-op
bodyClasses = new Set(["tab-lab"]);
K.keyRun("page:3"); K.keyRun("page:0");
out.hashes = hashes.slice(); hashes.length = 0;
K.keyRun("fix-close");
out.fixclose = clicks.slice(); clicks.length = 0;

// 5. "/" on another page waits for the route, then focuses the search; on the transcript it focuses now
bodyClasses = new Set(["tab-config"]);
K.keyRun("search");
out.search_other = {hashes: hashes.slice(), focused: !!$("q").focused, waiting: listeners.filter((l) => l.t === "hashchange").length};
listeners.pop().fn();
out.search_after = !!$("q").focused;
hashes.length = 0; $("q").focused = false; bodyClasses = new Set(["tab-log"]);
K.keyRun("search");
out.search_log = {hashes: hashes.slice(), focused: !!$("q").focused};

// 6. j / k selection, then Enter / p / c on it
bodyClasses = new Set(["tab-log"]);
rows = [mkRow(30), mkRow(20, {noClip: true}), mkRow(10, {quiet: true})];
K.keyRun("toggle");   // nothing selected yet
out.no_sel_toggle = toggled.slice();
const sel = () => K.keyState.sel;
const seq = [];
for (const a of ["sel:-1", "sel:1", "sel:1", "sel:1", "sel:-1", "sel:-1", "sel:-1"]) { K.keyRun(a); seq.push(sel()); }
out.sel_seq = seq;
out.sel_style = K.keyState.style.textContent;
out.scrolled = rows.map((r) => r.scrolled > 0);
K.keyRun("toggle"); out.toggled = toggled.slice();
K.keyState.sel = 30; K.keyRun("play");
K.keyState.sel = 20; K.keyRun("play");
out.play_clicks = clicks.slice(); clicks.length = 0;
out.play_flash = flashes.slice(); flashes.length = 0;
K.keyState.sel = 30; K.keyRun("copy");
K.keyState.sel = 10; K.keyRun("copy");   // a quiet row has no text
await_(() => { out.copied = written.slice(); finish(); });
function await_(fn) { setTimeout(fn, 20); }

// 7. fix-a-word pure parts
function finish() {
  const F = fn(fixBlock, "{fwWord, fwTarget, fwBody}");
  out.word = ["", "  ", "SQLite", "  uv  ", "a\nb", "x".repeat(60), "x".repeat(61), null].map((s) => F.fwWord(s));
  const line = {closest: (s) => (s === ".ev" ? ev_ : null)};
  const ev_ = {dataset: {id: "7"}, classList: {contains: (c) => c === "err" && ev_.err}};
  const textNode = {nodeType: 3, parentElement: {nodeType: 1, closest: (s) => (s === ".ev .line" ? line : null)}};
  const rowsEl = {contains: (n) => n === line};
  const selOf = (text, node, collapsed = false) => ({isCollapsed: collapsed, rangeCount: 1, toString: () => text,
    getRangeAt: () => ({commonAncestorContainer: node})});
  const t = F.fwTarget(selOf(" sequel ", textNode), rowsEl);
  out.target = t && {word: t.word, id: t.id};
  out.target_none = [
    F.fwTarget(selOf("sequel", textNode, true), rowsEl),
    F.fwTarget(selOf("", textNode), rowsEl),
    F.fwTarget(selOf("two\nlines", textNode), rowsEl),
    F.fwTarget(selOf("x".repeat(80), textNode), rowsEl),
    F.fwTarget(selOf("sequel", {nodeType: 1, closest: () => null}), rowsEl),
    F.fwTarget(selOf("sequel", textNode), {contains: () => false}),
    F.fwTarget({isCollapsed: false, rangeCount: 0, toString: () => "sequel"}, rowsEl),
    F.fwTarget(null, rowsEl),
  ].map((x) => x === null);
  ev_.err = true;
  out.target_err = F.fwTarget(selOf("sequel", textNode), rowsEl) === null;
  const data = {global: {SQLite: "sequel lite"}, profiles: {nova: {GIF: "jif"}, tutor: {}}};
  out.body_profile = F.fwBody(data, "profile", "nova", "uv", "you vee");
  out.body_global = F.fwBody(data, "global", "nova", "GIF", "gif");
  out.body_replace = F.fwBody(data, "profile", "nova", "GIF", "giff");
  out.body_new_table = F.fwBody({global: {}, profiles: {}}, "profile", "ghost", "a", "b");
  out.data_untouched = JSON.stringify(data.profiles.nova);
  console.log(JSON.stringify(out));
}
"""


def _blocks() -> tuple[str, str]:
    text = UI.read_text(encoding="utf-8")
    pairs = (("// ---------- keys", "// ---------- end keys"), ("// ---------- fix word", "// ---------- end fix word"))
    found = []
    for start, end in pairs:
        m = re.search(re.escape(start) + r"\n(.*?)\n" + re.escape(end), text, re.S)
        assert m, f"{start} block not found in index.html"
        found.append(m.group(1))
    return found[0], found[1]


@pytest.fixture(scope="module")
def out() -> dict:
    keys, fix = _blocks()
    proc = subprocess.run([NODE, "-e", HARNESS], input=json.dumps([keys, fix]), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_markers_present():
    keys, fix = _blocks()
    assert "function keyAction" in keys and "function fwBody" in fix


def test_key_mapping(out):
    assert out["map"] == {
        "?": "help", "/": "search", "1": "page:0", "2": "page:1", "3": "page:2", "4": "page:3", "5": None,
        ".": "stop", "n": "skip", "m": "mute", "j": "sel:1", "k": "sel:-1", "Enter": "toggle", "p": "play",
        "c": "copy", "x": None, "Escape": None,
    }


def test_transcript_keys_only_on_the_transcript(out):
    assert out["other_page"] == {"j": None, "k": None, "Enter": None, "p": None, "c": None, "m": "mute", "3": "page:2"}


def test_ignored_while_typing_or_with_modifiers(out):
    assert out["typing"] == [None, None, None]
    assert out["editable"] is None
    assert out["mods"] == [None, None, None]
    assert out["nokey"] is None
    assert out["enter_on_button"] is None   # Enter activates the focused button


def test_explorer_keeps_its_own_keys(out):
    ex = out["explorer"]
    assert [ex[k] for k in ('"j"', '"k"', '"x"', '"s"', '" "')] == [None] * 5
    assert ex['"m"'] == "mute" and ex['"1"'] == "page:0" and ex['"?"'] == "help"
    assert out["lab_idle"] is None   # j is a Transcript key, and no explorer is running


def test_escape_and_help(out):
    assert out["esc"] == ["fix-close", "help-close", "fix-close"]
    assert out["in_help"] == [None, "help-close", None]


def test_actions_click_existing_controls(out):
    assert out["clicks"] == ["stop", "skip", "muteall"]
    assert out["hashes"] == ["#lab", "#config", "#transcript"]
    assert out["fixclose"] == ["fwClose"]


def test_search_focuses_after_the_page_switch(out):
    assert out["search_other"] == {"hashes": ["#transcript"], "focused": False, "waiting": 1}
    assert out["search_after"] is True
    assert out["search_log"] == {"hashes": [], "focused": True}


def test_row_selection_and_row_actions(out):
    assert out["no_sel_toggle"] == []
    # k with nothing selected lands on the first row; then j, j, j (clamped), k, k, k (clamped)
    assert out["sel_seq"] == [30, 20, 10, 10, 20, 30, 30]
    assert '.tx .ev[data-id="30"]' in out["sel_style"]
    assert out["scrolled"] == [True, True, True]
    assert out["toggled"] == [30]
    assert out["play_clicks"] == ["play30"]
    assert out["play_flash"][0][:2] == ["tx-msg", "No clip kept for this line"]
    assert out["copied"] == ["line 30"]


def test_fix_word_selection(out):
    assert out["word"] == [None, None, "SQLite", "uv", None, "x" * 60, None, None]
    assert out["target"] == {"word": "sequel", "id": 7}
    assert all(out["target_none"])
    assert out["target_err"] is True


def test_fix_word_body_keeps_existing_entries(out):
    assert out["body_profile"] == {"profile": "nova", "entries": {"GIF": "jif", "uv": "you vee"}}
    assert out["body_global"] == {"profile": None, "entries": {"SQLite": "sequel lite", "GIF": "gif"}}
    assert out["body_replace"] == {"profile": "nova", "entries": {"GIF": "giff"}}
    assert out["body_new_table"] == {"profile": "ghost", "entries": {"a": "b"}}
    assert json.loads(out["data_untouched"]) == {"GIF": "jif"}
