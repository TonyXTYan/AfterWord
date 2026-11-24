"""The seed explorer panel's navigation logic (afterword/ui/index.html), run
in node against stubbed DOM helpers: which take j / x select and which
cursor the page posts, with dropped takes and the pitch filter. Skipped
when node is not installed."""

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
const block = require("fs").readFileSync(0, "utf8");
const els = {};
const el = (id) => els[id] || (els[id] = {
  id, value: "", checked: false, disabled: false, textContent: "", scrollTop: 0,
  replaceChildren() {}, querySelector() { return null; }, addEventListener() {},
});
const calls = [];
const env = {
  $: el,
  h: (tag, attrs, ...kids) => ({tag, attrs, kids, append() {}}),
  isNum: (v) => typeof v === "number" && Number.isFinite(v),
  num: (v) => v,
  flashNote() {}, note() {}, play() {}, clampPace: (p) => p || 1,
  api: async (method, path, body) => { calls.push({path, body}); return {ok: true, data: {state: S.state}}; },
  CSS: {escape: (s) => s},
  document: {querySelectorAll: () => []},
  Audio: class { constructor() { this.dataset = {}; this.paused = true; } pause() { this.paused = true; } },
  setTimeout: (fn) => { fn(); return 0; },
  clearTimeout() {},
};
const S = {};
const names = Object.keys(env);
const api = new Function(...names, block + "\nreturn {ex, exNext, exDrop, exEvent, exSelect, exApply, exList};")(
  ...names.map((n) => env[n]));
const take = (seed, f0) => ({id: "p" + seed, seed, f0_hz: f0, explore_id: "x1", url: "", settings: {}});
const ids = () => api.exList().map((t) => t.seed);
const out = {};
const sel = () => (api.ex.takes.get(api.ex.sel) || {}).seed ?? null;
const lastCursor = () => { const c = calls.filter((c) => c.path === "/api/lab/explore/cursor").pop(); return c ? c.body : null; };
function reset(status, seeds, f0) {
  calls.length = 0;
  el("ex-fmin").value = ""; el("ex-fmax").value = ""; el("ex-showdrop").checked = false;
  S.state = {id: "x1", status, ahead: 4, cursor: seeds[0], count: seeds.length, preempted: 0};
  api.exApply({state: null, takes: []});
  api.exApply({state: S.state, takes: seeds.map((s) => take(s, f0 ? f0(s) : 150 + s))});
}

// 1. x on the newest take: stay at the front, ask for the next take.
reset("waiting", [1, 2, 3, 4, 5, 6, 7, 8]);
api.exSelect("p8"); calls.length = 0;
api.exDrop();
out.drop_newest = {sel: sel(), want: !!api.ex.want, cursor: lastCursor()};
api.exEvent({type: "take", take: take(9, 160)});
out.drop_newest_then = {sel: sel(), want: !!api.ex.want, cursor: lastCursor()};

// 2. x in the middle: the next take; k from a hidden take: the one before it.
reset("waiting", [1, 2, 3, 4, 5]);
api.exSelect("p3"); api.exDrop();
out.drop_middle = sel();
api.ex.sel = "p3"; api.exNext(-1);  // p3 is dropped (hidden)
out.back_from_hidden = sel();

// 3. j while the selected take is hidden by the filter: the next shown one after it.
reset("waiting", [40, 41, 42, 43, 44, 45], (s) => ({40: 250, 41: 260, 42: 300, 43: 240, 44: 310, 45: 320}[s]));
api.exSelect("p43"); el("ex-fmin").value = "300"; calls.length = 0;
api.exNext(1);
out.filtered_next = {sel: sel(), cursor: lastCursor()};

// 4. Nothing passes the filter: j still asks for more, hidden arrivals move the cursor on.
reset("waiting", [40, 41], () => 250);
el("ex-fmin").value = "300"; calls.length = 0;
api.exNext(1, {play: true});
out.none_shown = {want: !!api.ex.want, cursor: lastCursor()};
api.exEvent({type: "take", take: take(42, 260)});
out.hidden_arrival = {want: !!api.ex.want, cursor: lastCursor(), sel: sel()};
api.exEvent({type: "take", take: take(43, 320)});
out.shown_arrival = {want: !!api.ex.want, sel: sel(), cursor: lastCursor()};

// 5. A stopped exploration: x on the last take selects the one before it.
reset("stopped", [1, 2, 3]);
api.exSelect("p3"); calls.length = 0;
api.exDrop();
out.drop_last_stopped = {sel: sel(), want: !!api.ex.want, posts: calls.length};

process.stdout.write(JSON.stringify(out));
"""


def _block():
    src = UI.read_text(encoding="utf-8")
    start = src.index("  // ---------- seed explorer")
    end = src.index("  function exKey(e)")
    return src[start:end]


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    harness = tmp_path_factory.mktemp("ui") / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)], input=_block(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_drop_the_newest_take_waits_for_the_next(results):
    r = results["drop_newest"]
    # Not back to seed 1: the selection stays at the front and the cursor
    # stays on seed 8 so the explorer renders 9..12.
    assert r["sel"] == 8 and r["want"] is True
    assert r["cursor"]["seed"] == 8
    then = results["drop_newest_then"]
    assert then["sel"] == 9 and then["want"] is False and then["cursor"]["seed"] == 9


def test_drop_in_the_middle_and_step_back_from_a_hidden_take(results):
    assert results["drop_middle"] == 4
    assert results["back_from_hidden"] == 2


def test_next_from_a_take_hidden_by_the_filter(results):
    r = results["filtered_next"]
    assert r["sel"] == 44 and r["cursor"]["seed"] == 44  # not back to 42, the first shown


def test_the_filter_never_stalls_the_explorer(results):
    assert results["none_shown"] == {"want": True, "cursor": {"seed": 41, "auto": False}}
    hidden = results["hidden_arrival"]
    # A take the filter hides moves the cursor on, as auto-advance (the idle
    # pause still applies), so the explorer keeps rendering.
    assert hidden["want"] is True and hidden["cursor"] == {"seed": 42, "auto": True}
    shown = results["shown_arrival"]
    assert shown["want"] is False and shown["sel"] == 43 and shown["cursor"]["seed"] == 43


def test_drop_the_last_take_of_a_stopped_exploration(results):
    r = results["drop_last_stopped"]
    assert r["sel"] == 2 and r["want"] is False and r["posts"] == 0


def test_block_markers_exist():
    block = _block()
    assert re.search(r"function exNext\(", block) and "function exArrived(" in block
