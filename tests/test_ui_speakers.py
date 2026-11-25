"""The speaker colour helpers (afterword/ui/index.html, between
"// ---------- speakers" and "// ---------- end speakers"), run in node
against a tiny fake DOM: slots follow the configured profile order, unknown
names hash to a stable slot, a new profile list repaints what is already on
the page, and the tag HTML escapes the name. Skipped when node is not
installed."""

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
// A fake element: classList over a Set, a dataset object.
function fakeEl(classes, spk) {
  const set = new Set(classes);
  return {
    classList: {
      add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
      [Symbol.iterator]: () => set[Symbol.iterator](),
    },
    dataset: spk == null ? {} : {spk},
    classes: () => [...set].sort(),
  };
}
const onPage = [];
const dispatched = [];
const document = {
  querySelectorAll: (sel) => (sel === "[data-spk]" ? onPage.filter((e) => "spk" in e.dataset) : []),
  dispatchEvent: (ev) => { dispatched.push(ev); return true; },
};
class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } }
const h = (tag, props, ...kids) => ({tag, props, kids});
const lib = new Function("document", "CustomEvent", "h", src +
  "\nreturn {setSpeakerProfiles, paintSpeaker, speakerName, speakerSlot, speakerTag, speakerTagHTML};")(
  document, CustomEvent, h);
const out = {};

// Unknown names before any profile list: a stable slot from the name alone.
const names = ["nova", "echo", "fable", "onyx", "zed", "日本語", "a much longer profile name", ""];
out.unknown_first = names.map((n) => lib.speakerSlot(n));
out.unknown_again = names.map((n) => lib.speakerSlot(n));
out.default_name = [lib.speakerName(null), lib.speakerName(""), lib.speakerName(undefined)];
out.default_slot = [lib.speakerSlot(null), lib.speakerSlot("default")];

// Elements drawn before the list arrives: a tag and a row, both carrying data-spk.
const tag = fakeEl(["spk"]); lib.paintSpeaker(tag, "echo");
const row = fakeEl(["row", "spk-rule"]); lib.paintSpeaker(row, "nova");
const plain = fakeEl(["spk-3"]);   // not a speaker element: no data-spk
onPage.push(tag, row, plain);
out.before = {tag: tag.classes(), row: row.classes(), tag_spk: tag.dataset.spk};

// The configured order sets the slots (wrapping after six); the page repaints.
const order = ["echo", "nova", "fable", "onyx", "alloy", "shimmer", "seventh"];
lib.setSpeakerProfiles(order);
out.ordered = order.map((n) => lib.speakerSlot(n));
out.after = {tag: tag.classes(), row: row.classes(), plain: plain.classes()};
out.events = dispatched.map((e) => ({type: e.type, detail: e.detail}));
out.unknown_after = ["zed", "日本語"].map((n) => lib.speakerSlot(n));

// The same list again changes nothing; a reordered list repaints again.
lib.setSpeakerProfiles(order.slice());
out.events_same = dispatched.length;
lib.setSpeakerProfiles(["nova", "echo"]);
out.reordered = {tag: tag.classes(), row: row.classes(), events: dispatched.length};
lib.setSpeakerProfiles("not a list");
out.events_bad = dispatched.length;

// Tags: the element form and the HTML form, with a hostile name.
const evil = `<img src=x onerror="alert(1)">&'`;
out.html = lib.speakerTagHTML(evil);
out.html_default = lib.speakerTagHTML(null);
out.html_known = lib.speakerTagHTML("echo");
const el = lib.speakerTag("nova", {class: "big", title: "t"});
out.el = {cls: el.props.class, spk: el.props["data-spk"], title: el.props.title, text: el.kids[1]};
process.stdout.write(JSON.stringify(out));
"""


def _block() -> str:
    src = UI.read_text(encoding="utf-8")
    start = src.index("// ---------- speakers")
    end = src.index("// ---------- end speakers")
    assert start < end
    # esc() lives with the shared helpers above; the tag HTML depends on it.
    esc = re.search(r"^const esc = .*$", src, re.M)
    assert esc, "const esc = ... not found"
    return esc.group(0) + "\n" + src[start:end]


@pytest.fixture(scope="module")
def r(tmp_path_factory):
    harness = tmp_path_factory.mktemp("ui") / "speakers.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run([NODE, str(harness)], input=_block(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_unknown_names_hash_to_a_stable_slot(r):
    assert r["unknown_first"] == r["unknown_again"]
    assert all(isinstance(s, int) and 0 <= s <= 5 for s in r["unknown_first"])
    assert len(set(r["unknown_first"])) > 1  # not everything in one colour
    # A profile list that does not name them leaves their slots alone.
    assert r["unknown_after"] == r["unknown_first"][4:6]


def test_no_profile_is_the_default_speaker(r):
    assert r["default_name"] == ["default", "default", "default"]
    assert r["default_slot"][0] == r["default_slot"][1]


def test_slots_follow_the_configured_order(r):
    assert r["ordered"] == [0, 1, 2, 3, 4, 5, 0]


def test_a_new_profile_list_repaints_the_page(r):
    before, after = r["before"], r["after"]
    assert before["tag_spk"] == "echo"
    assert "spk" in before["tag"] and "spk-rule" in before["row"]
    # echo is first and nova second in the configured order.
    assert after["tag"] == ["spk", "spk-0"]
    assert after["row"] == ["row", "spk-1", "spk-rule"]
    assert after["plain"] == ["spk-3"]  # no data-spk: left alone
    assert r["events"] == [{"type": "aw:speakers", "detail": ["echo", "nova", "fable", "onyx", "alloy", "shimmer", "seventh"]}]
    assert r["events_same"] == 1  # same list: no repaint, no event
    assert r["reordered"] == {"tag": ["spk", "spk-1"], "row": ["row", "spk-0", "spk-rule"], "events": 2}
    assert r["events_bad"] == 2


def test_tag_html_escapes_the_name(r):
    html = r["html"]
    assert "<img" not in html and 'onerror="' not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;&#39;" in html
    assert re.fullmatch(r'<span class="spk spk-[0-5]" data-spk="[^"<>]*"><i aria-hidden="true"></i>[^<>]*</span>', html)
    assert 'data-spk="default"' in r["html_default"] and r["html_default"].endswith(">default</span>")
    assert r["html_known"].startswith('<span class="spk spk-1" data-spk="echo">')  # slot from the last list (nova, echo)


def test_tag_element(r):
    el = r["el"]
    assert el["cls"] == "spk spk-0 big" and el["spk"] == "nova" and el["title"] == "t" and el["text"] == "nova"
