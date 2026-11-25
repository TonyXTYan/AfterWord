"""The speaker colour helpers (afterword/ui/index.html, between
"// ---------- speakers" and "// ---------- end speakers"), run in node
against a tiny fake DOM: slots follow the configured profile order, unknown
names hash to a stable slot, a new profile list repaints what is already on
the page, and the tag HTML escapes the name. Per-profile colours: an
explicit palette name or hex wins, the other voices take the free palette
colours in order, a custom hex gets a generated class with readable text
tones. Skipped when node is not installed."""

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
  "\nreturn {setSpeakerProfiles, paintSpeaker, speakerName, speakerSlot, speakerTag, speakerTagHTML," +
  " speakerAssign, speakerColorValue, speakerSwatch, speakerColorLabel, speakerInk, speakerContrast, speakerCssText," +
  " SPEAKER_PALETTE, get colors() { return speakerColors; }};")(
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

// ---- colours ----
const nEvents = () => dispatched.length;
const c0 = nEvents();
const voices = ["a", "b", "c", "d", "e"];
const t1 = fakeEl(["spk"]); lib.paintSpeaker(t1, "b");
const t2 = fakeEl(["spk"]); lib.paintSpeaker(t2, "d");
onPage.push(t1, t2);
// b picks teal (12), d a custom hex, e the cyan swatch's hex (= palette 1); a and c are automatic.
lib.setSpeakerProfiles(voices, {b: "Teal", d: "#12AB9F", e: "#00b8d9", zz: "red", c: "not a colour"});
out.colors = {slots: voices.map((n) => lib.speakerSlot(n)), stored: lib.colors, events: nEvents() - c0,
  t1: t1.classes(), t2: t2.classes(), swatch: voices.map((n) => lib.speakerSwatch(n)),
  label: voices.map((n) => lib.speakerColorLabel(n)), css: lib.speakerCssText(),
  html: lib.speakerTagHTML("d")};
// The list alone keeps the colours; the same colours again change nothing.
lib.setSpeakerProfiles(voices);
lib.setSpeakerProfiles(voices.slice(), {b: "teal", d: "#12ab9f", e: "#00B8D9"});
out.colors_same = nEvents() - c0;
// Only a colour changes: repaint and an event; the custom class goes away.
lib.setSpeakerProfiles(voices, {b: "teal", d: "red"});
out.colors_changed = {events: nEvents() - c0, t2: t2.classes(), slots: voices.map((n) => lib.speakerSlot(n))};
// null clears every explicit colour.
lib.setSpeakerProfiles(voices, null);
out.colors_cleared = {slots: voices.map((n) => lib.speakerSlot(n)), stored: lib.colors, t1: t1.classes()};
// More voices than colours: the automatic ones wrap over the free colours.
const many = Array.from({length: 20}, (_, i) => "v" + i);
out.wrap = [...lib.speakerAssign(many, {v0: "sky", v1: "yellow"}).values()];
const all16 = Object.fromEntries(lib.SPEAKER_PALETTE.map((p, i) => ["v" + i, p[0]]));
out.wrap_full = [...lib.speakerAssign(many, all16).values()].slice(16);
out.values = ["teal", " TEAL ", "#ABCDEF", "#abc", "teal2", 5, null, ""].map((v) => lib.speakerColorValue(v));
// Text tones for awkward custom colours, light and dark.
const odd = ["#ffff00", "#000080", "#808080", "#ffffff", "#000000", "#12ab9f", "#ff00ff", "#00ff00"];
out.inks = odd.map((hx) => [hx, lib.speakerInk(hx, false), lib.speakerInk(hx, true)]);
out.contrast = [lib.speakerContrast("#000000", "#ffffff"), lib.speakerContrast("#ffffff", "#ffffff")];
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
    assert all(isinstance(s, int) and 0 <= s <= 15 for s in r["unknown_first"])
    assert len(set(r["unknown_first"])) > 1  # not everything in one colour
    # A profile list that does not name them leaves their slots alone.
    assert r["unknown_after"] == r["unknown_first"][4:6]


def test_no_profile_is_the_default_speaker(r):
    assert r["default_name"] == ["default", "default", "default"]
    assert r["default_slot"][0] == r["default_slot"][1]


def test_slots_follow_the_configured_order(r):
    # sixteen palette colours: a seventh voice no longer shares the first one's
    assert r["ordered"] == [0, 1, 2, 3, 4, 5, 6]


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
    assert re.fullmatch(r'<span class="spk spk-(?:[0-9]|1[0-5])" data-spk="[^"<>]*"><i aria-hidden="true"></i>[^<>]*</span>', html)
    assert 'data-spk="default"' in r["html_default"] and r["html_default"].endswith(">default</span>")
    assert r["html_known"].startswith('<span class="spk spk-1" data-spk="echo">')  # slot from the last list (nova, echo)


def test_tag_element(r):
    el = r["el"]
    assert el["cls"] == "spk spk-0 big" and el["spk"] == "nova" and el["title"] == "t" and el["text"] == "nova"


# --------------------------------------------------------------------------- #
# Per-profile colours                                                         #
# --------------------------------------------------------------------------- #

def _lum(hx):
    c = [int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def test_explicit_colours_win_and_the_rest_take_free_ones(r):
    c = r["colors"]
    # a: first free (0, yellow); b: teal = 12; c: bad value, so automatic: the next free
    # one (2; 1 is taken by e's cyan hex); d: custom hex; e: the cyan swatch's hex = palette 1
    assert c["slots"] == [0, 12, 2, "x0", 1]
    assert c["stored"] == {"b": "teal", "d": "#12ab9f", "e": "#00b8d9"}  # normalised, unknown names dropped
    assert c["events"] == 1
    assert c["t1"] == ["spk", "spk-12"] and c["t2"] == ["spk", "spk-x0"]
    assert c["swatch"] == ["#F5C400", "#0B8A80", "#2FBF71", "#12ab9f", "#00B8D9"]
    assert c["label"] == ["yellow (automatic)", "teal", "green (automatic)", "#12ab9f", "cyan"]
    assert c["html"].startswith('<span class="spk spk-x0" data-spk="d">')


def test_custom_hex_gets_a_generated_class(r):
    css = r["colors"]["css"]
    assert ".spk-0 { --spk: #F5C400; --spk-ink: #6E5600; }" in css
    assert ".spk-15 {" in css and ".spk-16" not in css
    light = re.search(r"\.spk-x0 \{ --spk: #12ab9f; --spk-ink: (#[0-9a-f]{6}); \}", css)
    dark = re.search(r"@media \(prefers-color-scheme: dark\) \{.*\.spk-x0 \{ --spk-ink: (#[0-9a-f]{6}); \}", css, re.S)
    assert light and dark
    for paper in ("#E8EBEE", "#F7F8F9"):
        assert _contrast(light.group(1), paper) >= 4.5
    for paper in ("#121417", "#1A1D21"):
        assert _contrast(dark.group(1), paper) >= 4.5


def test_colour_changes_repaint_and_list_alone_keeps_them(r):
    assert r["colors_same"] == 1  # the list alone, and the same colours re-cased: no event
    ch = r["colors_changed"]
    assert ch["events"] == 2 and ch["t2"] == ["spk", "spk-7"]  # d: red
    assert ch["slots"] == [0, 12, 1, 7, 2]  # e lost its colour: automatic, after a and c
    cl = r["colors_cleared"]
    assert cl["slots"] == [0, 1, 2, 3, 4] and cl["stored"] == {} and cl["t1"] == ["spk", "spk-1"]


def test_more_voices_than_colours_wrap(r):
    # v0 sky (15), v1 yellow (0); the others take 1..14 in order, then wrap over those 14
    assert r["wrap"][:2] == [15, 0]
    assert r["wrap"][2:16] == list(range(1, 15))
    assert r["wrap"][16:] == [1, 2, 3, 4]
    assert r["wrap_full"] == [0, 1, 2, 3]  # every colour chosen: wrap over all sixteen


def test_colour_values_are_normalised(r):
    assert r["values"] == ["teal", "teal", "#abcdef", "", "", "", "", ""]


def test_custom_text_tones_read_on_both_papers(r):
    assert r["contrast"][0] == pytest.approx(21) and r["contrast"][1] == pytest.approx(1)
    for hx, light, dark in r["inks"]:
        for paper in ("#E8EBEE", "#F7F8F9"):
            assert _contrast(light, paper) >= 4.5, (hx, light, paper)
        for paper in ("#121417", "#1A1D21"):
            assert _contrast(dark, paper) >= 4.5, (hx, dark, paper)
    # a colour that already reads is kept as it is
    assert {hx: lt for hx, lt, _ in r["inks"]}["#000080"] == "#000080"
