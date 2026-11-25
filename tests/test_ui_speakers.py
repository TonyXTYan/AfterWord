"""The speaker colour helpers (afterword/ui/index.html, between
"// ---------- speakers" and "// ---------- end speakers"), run in node
against a tiny fake DOM: slots follow the configured profile order, unknown
names hash to a stable base colour, a new profile list repaints what is
already on the page, and the tag HTML escapes the name. Per-profile colours:
an explicit palette name or hex wins, the other voices take the free palette
colours in order (all sixteen base colours, then the deep, light and pale
rows; more than 64 wrap), a custom hex gets a generated class with readable
text tones, and swatches too close to the sheet get a ring. Skipped when node
is not installed."""

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
  " SPEAKER_PALETTE, SPEAKER_FAMILIES, SPEAKER_SHADES, get colors() { return speakerColors; }};")(
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

// The configured order sets the slots (0, 1, 2, … in list order); the page repaints.
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
// v0 … v15 choose the 16 base colours; v16 … v19 stay automatic.
const all16 = Object.fromEntries(lib.SPEAKER_PALETTE.slice(0, 16).map((p, i) => ["v" + i, p[0]]));
out.wrap_full = [...lib.speakerAssign(many, all16).values()].slice(16);
// All 64 colours chosen: nothing is free, so the automatic voice wraps over the palette.
const v65 = Array.from({length: 65}, (_, i) => "v" + i);
const all64 = Object.fromEntries(lib.SPEAKER_PALETTE.map((p, i) => ["v" + i, p[0]]));
out.full64 = [...lib.speakerAssign(v65, all64).values()];
// Automatic order: base, deep, light, pale; explicit choices (names of any
// row, or a palette hex) are skipped; past 64 the automatic voices wrap.
const seventy = Array.from({length: 70}, (_, i) => "w" + i);
out.order = [...lib.speakerAssign(seventy, {w0: "teal-deep", w1: "yellow", w2: "#B99300", w3: "Sky-Pale"}).values()];
out.order_none = [...lib.speakerAssign(seventy, {}).values()];
out.names64 = lib.SPEAKER_PALETTE.map((p) => p[0]);
out.shape = [lib.SPEAKER_FAMILIES, lib.SPEAKER_SHADES];
out.values = ["teal", " TEAL ", "#ABCDEF", "#abc", "teal2", 5, null, "", "Teal-Deep", "white-pale", "teal-dark"].map((v) => lib.speakerColorValue(v));
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
    assert all(isinstance(s, int) and 0 <= s <= 15 for s in r["unknown_first"])  # a base colour
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
    assert ".spk-63 {" in css and ".spk-64" not in css
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


def test_more_voices_than_base_colours_take_the_deep_row(r):
    # v0 sky (15), v1 yellow (0); the others take 1..14 in order, then the deep row (16 …)
    assert r["wrap"][:2] == [15, 0]
    assert r["wrap"][2:16] == list(range(1, 15))
    assert r["wrap"][16:] == [16, 17, 18, 19]
    assert r["wrap_full"] == [16, 17, 18, 19]  # every base colour chosen: deep next


def test_every_colour_chosen_the_automatic_voice_still_gets_one(r):
    # v0 … v63 chose all 64; v64 is automatic with nothing free: it wraps to 0
    assert r["full64"] == list(range(64)) + [0]


def test_automatic_order_is_base_deep_light_pale_then_wraps(r):
    # nothing chosen: 0 … 63 in palette order (base, deep, light, pale), then again
    assert r["order_none"] == list(range(64)) + list(range(6))
    # w0 teal-deep (28), w1 yellow (0), w2 yellow-deep's hex (16), w3 sky-pale (63)
    order = r["order"]
    assert order[:4] == [28, 0, 16, 63]
    free = [i for i in range(64) if i not in (0, 16, 28, 63)]
    assert order[4:4 + len(free)] == free  # 1 … 15, then 17 … 27, 29 … 62
    assert order[4 + len(free):] == free[:70 - 4 - len(free)]  # past 64: wrap over the free ones
    assert len(set(order)) == 64


def test_palette_shape(r):
    families, shades = r["shape"]
    assert families == 16 and shades == ["base", "deep", "light", "pale"]
    names = r["names64"]
    assert len(names) == 64 and names[28] == "teal-deep" and names[63] == "sky-pale"
    for i, n in enumerate(names):
        row = i // 16
        assert n == names[i % 16] + ("", "-deep", "-light", "-pale")[row]


def test_faint_swatches_get_a_ring_and_a_strong_line(r):
    css = r["colors"]["css"]
    light, dark = css.split("@media (prefers-color-scheme: dark)")
    # white (4) and the palest shades are close to the light sheet: ring + text tone for lines
    for i in (4, 48 + 4, 48 + 15):
        assert re.search(rf"\.spk-{i} \{{ --spk: #[0-9A-F]{{6}}; --spk-ink: #[0-9A-F]{{6}}; "
                         r"--spk-ring: var\(--rule\); --spk-strong: var\(--spk-ink\); \}", light), i
        # not faint on the dark sheet: reset there
        assert re.search(rf"\.spk-{i} \{{ --spk-ink: #[0-9A-F]{{6}}; --spk-ring: initial; --spk-strong: initial; \}}", dark), i
    assert "--spk-ring" not in re.search(r"\.spk-12 \{[^}]*\}", light).group(0)  # teal: plain


def test_colour_values_are_normalised(r):
    assert r["values"] == ["teal", "teal", "#abcdef", "", "", "", "", "", "teal-deep", "white-pale", ""]


def test_custom_text_tones_read_on_both_papers(r):
    assert r["contrast"][0] == pytest.approx(21) and r["contrast"][1] == pytest.approx(1)
    for hx, light, dark in r["inks"]:
        for paper in ("#E8EBEE", "#F7F8F9"):
            assert _contrast(light, paper) >= 4.5, (hx, light, paper)
        for paper in ("#121417", "#1A1D21"):
            assert _contrast(dark, paper) >= 4.5, (hx, dark, paper)
    # a colour that already reads is kept as it is
    assert {hx: lt for hx, lt, _ in r["inks"]}["#000080"] == "#000080"


# --------------------------------------------------------------------------- #
# Colour picker: the dots on a swatch and the cap on the hover card           #
# --------------------------------------------------------------------------- #

def test_picker_marks_and_card_cap(tmp_path):
    src = UI.read_text(encoding="utf-8")
    start = src.index("// ---------- cp marks")
    end = src.index("// ---------- end cp marks")
    assert start < end
    harness = tmp_path / "marks.js"
    harness.write_text(
        'const src = require("fs").readFileSync(0, "utf8");\n'
        'const lib = new Function(src + "\\nreturn {cpMarks, cpCap, cpSaveError};")();\n'
        "const names = Array.from({length: 15}, (_, i) => 'v' + i);\n"
        "process.stdout.write(JSON.stringify({\n"
        "  marks: [0, 1, 2, 3, 4, 9, -1, null, 'x', 2.7].map((n) => lib.cpMarks(n)),\n"
        "  cap_small: lib.cpCap(names.slice(0, 3)), cap_edge: lib.cpCap(names.slice(0, 12)).rest,\n"
        "  cap_big: lib.cpCap(names), cap_two: lib.cpCap(names, 2),\n"
        "  save_err: [{status: 404, error: \"no profile named 'nova'\"}, {status: 404, error: 'HTTP 404'},\n"
        "    {status: 405, error: 'Method Not Allowed'}, {status: 404, error: 'Not Found'}, {status: 400, error: 'color: not a colour'},\n"
        "    {status: 0, error: 'Daemon unreachable.'}, {status: 500}, null].map((x) => lib.cpSaveError(x)),\n"
        "}));\n",
        encoding="utf-8",
    )
    p = subprocess.run([NODE, str(harness)], input=src[start:end], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    r = json.loads(p.stdout)
    # one dot per other voice up to three; four or more: two dots and a plus
    assert r["marks"] == [
        {"dots": 0, "more": False}, {"dots": 1, "more": False}, {"dots": 2, "more": False}, {"dots": 3, "more": False},
        {"dots": 2, "more": True}, {"dots": 2, "more": True}, {"dots": 0, "more": False}, {"dots": 0, "more": False},
        {"dots": 0, "more": False}, {"dots": 2, "more": False},
    ]
    assert r["cap_small"] == {"shown": ["v0", "v1", "v2"], "rest": 0} and r["cap_edge"] == 0
    assert r["cap_big"]["shown"] == names12() and r["cap_big"]["rest"] == 3
    assert r["cap_two"] == {"shown": ["v0", "v1"], "rest": 13}
    # a failed save: the daemon's own words; only a bare 404/405 is "can't set colours yet"
    old = "Couldn't save: this daemon can't set colours yet."
    assert r["save_err"] == [
        "Couldn't save: no profile named 'nova'", old, old, old, "Couldn't save: color: not a colour",
        "Couldn't save: Daemon unreachable.", "Couldn't save: no response", "Couldn't save: no response",
    ]


def names12():
    return [f"v{i}" for i in range(12)]


def test_picker_has_no_legend_and_a_summary():
    src = UI.read_text(encoding="utf-8")
    block = src[src.index("// ---------- speaker colour picker"):src.index("// ---------- end speaker colour picker")]
    assert "cp-use" not in src and "In use" not in block.replace("colours in use", "").replace("colour in use", "")
    assert "cp-sum" in block and "cp-tip" in block


def test_picker_card_goes_on_scroll_and_shows_on_keyboard_focus_only():
    src = UI.read_text(encoding="utf-8")
    block = src[src.index("// ---------- speaker colour picker"):src.index("// ---------- end speaker colour picker")]
    place = block[block.index("function cpPlace()"):]
    place = place[:place.index("\n}\n")]
    # scroll (the page, or the chart sideways: the listener captures) and resize move the
    # popover; the card would stay at the old coordinates, so it goes first
    assert place.splitlines()[1].strip() == "cpTipHide();" and "cp.retip" in place
    assert 'window.addEventListener("scroll", cpPlace, true);' in block
    # focus shows the card only after keyboard input (a mouse open focuses the selected swatch)
    assert 'b.addEventListener("focus", () => { describe(i); if (cpKeys) showTip(b, i); });' in block
    assert 'document.addEventListener("keydown", () => { cpKeys = true; }, true);' in block
    assert 'document.addEventListener("pointerdown", () => { cpKeys = false; }, true);' in block
    assert "cpRender(focusKey, cpSaveError(r), \"err\")" in block
