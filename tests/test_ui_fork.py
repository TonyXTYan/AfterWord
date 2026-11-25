"""The "Open in voice lab" button on a Transcript row (afterword/ui/index.html): the pure
forkDetail() that picks the line, profile and language out of an event's
detail, run in node; plus the wiring around it (the detail button, the
aw:fork-to-lab event the Transcript sends and the Voice lab listens for).
The node part is skipped when node is not installed."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"
NODE = shutil.which("node")

HARNESS = r"""
const src = require("fs").readFileSync(0, "utf8");
const forkDetail = new Function(src + "\nreturn forkDetail;")();
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cases.map((c) => forkDetail(c))));
"""


def _text() -> str:
    return UI.read_text(encoding="utf-8")


def _block() -> str:
    m = re.search(r"// ---------- fork to lab\n(.*?)\n// ---------- end fork to lab", _text(), re.S)
    assert m, "fork to lab block not found in index.html"
    return m.group(1)


def _run(cases: list) -> list:
    proc = subprocess.run([NODE, "-e", HARNESS, json.dumps(cases)], input=_block(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_line_profile_and_tagged_language():
    [f] = _run([{"text": "  Hello there.  ", "profile": "nova", "kind": "stop", "attrs": {"lang": "ja"},
                 "timings": {"segments": [{"lang": "en"}]}}])
    assert f == {"text": "Hello there.", "profile": "nova", "lang": "ja"}


@needs_node
def test_language_from_segments_only_when_they_agree():
    one, mixed, none, auto = _run([
        {"text": "a", "profile": "nova", "timings": {"segments": [{"lang": "zh"}, {"lang": "zh"}]}},
        {"text": "a", "profile": "nova", "timings": {"segments": [{"lang": "zh"}, {"lang": "en"}]}},
        {"text": "a", "profile": "nova", "timings": {}},
        {"text": "a", "profile": "nova", "attrs": {"lang": "auto"}, "timings": {"segments": [{"lang": "en"}]}},
    ])
    assert one["lang"] == "zh"
    assert mixed["lang"] is None
    assert none["lang"] is None
    assert auto["lang"] == "en"   # "auto" is no language: fall back to the segments


@needs_node
def test_default_profile_is_null():
    a, b = _run([{"text": "x", "profile": None}, {"text": "x", "profile": ""}])
    assert a["profile"] is None and b["profile"] is None


@needs_node
def test_nothing_to_fork():
    out = _run([None, {}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "done", "kind": "system"}])
    assert out == [None] * 6


@needs_node
def test_text_capped_at_the_lab_limit():
    [f] = _run([{"text": "x" * 1500, "profile": "nova"}])
    assert len(f["text"]) == 1000


@needs_node
def test_bad_shapes_do_not_throw():
    [f] = _run([{"text": "x", "attrs": "nope", "timings": {"segments": "nope"}, "profile": 3}])
    assert f == {"text": "x", "profile": None, "lang": None}


SETTINGS_HARNESS = r"""
const src = require("fs").readFileSync(0, "utf8");
const forkSettings = new Function(src + "\nreturn forkSettings;")();
const cases = JSON.parse(process.argv[1]);
console.log(JSON.stringify(cases.map(([p, code]) => forkSettings(p, code))));
"""

NOVA = {"voice": "builtin:Ryan", "ref_text": "", "lang": "en", "pace": 1.1, "style": "calm", "seed": 7, "temperature": 0.8,
        "max_chars": 400, "say_voice": "Samantha", "description": "d", "color": None,
        "langs": {"zh": {"voice": "builtin:Vivian", "pace": 0.9},
                  "ja": {"style": "bright"},
                  "ko": {"voice": "clone:me", "ref_text": "annyeong", "seed": 3}}}
CLONE = {"voice": "clone:base", "ref_text": "the base clip's words", "lang": "en", "pace": 1, "style": "", "seed": None,
         "temperature": None, "langs": {"zh": {"voice": "clone:zh"}, "ja": {"pace": 1.2}}}


def _settings(cases: list) -> list:
    proc = subprocess.run([NODE, "-e", SETTINGS_HARNESS, json.dumps(cases)], input=_block(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_language_override_is_loaded_over_the_profile():
    zh, ja, ko, region = _settings([[NOVA, "zh"], [NOVA, "ja"], [NOVA, "ko"], [NOVA, "zh_CN"]])
    assert zh["override"] == "zh" and zh["lang"] == "zh"
    assert zh["settings"] == {"voice": "builtin:Vivian", "ref_text": "", "lang": "zh", "pace": 0.9, "style": "calm",
                              "mood": None, "seed": 7, "temperature": 0.8}
    # a table without its own voice keeps the profile's voice, takes the rest
    assert ja["override"] == "ja" and ja["settings"]["voice"] == "builtin:Ryan" and ja["settings"]["style"] == "bright"
    assert ko["settings"]["voice"] == "clone:me" and ko["settings"]["ref_text"] == "annyeong" and ko["settings"]["seed"] == 3
    # a regional code finds the language's table; the builder gets the code as given
    assert region["override"] == "zh" and region["settings"]["voice"] == "builtin:Vivian" and region["lang"] == "zh-cn"


@needs_node
def test_a_language_voice_does_not_inherit_the_profiles_transcript():
    zh, ja = _settings([[CLONE, "zh"], [CLONE, "ja"]])
    assert zh["settings"]["voice"] == "clone:zh" and zh["settings"]["ref_text"] == ""
    assert ja["settings"]["voice"] == "clone:base" and ja["settings"]["ref_text"] == "the base clip's words"
    assert ja["settings"]["pace"] == 1.2


@needs_node
def test_no_table_for_the_language_keeps_the_profile():
    de, auto, none, bad = _settings([[NOVA, "de"], [NOVA, "auto"], [NOVA, None], [None, "zh"]])
    assert de["override"] is None and de["lang"] == "de"
    assert de["settings"]["voice"] == "builtin:Ryan" and de["settings"]["lang"] == "de" and de["settings"]["pace"] == 1.1
    assert auto == none and auto["override"] is None and auto["lang"] is None and auto["settings"]["lang"] == "en"
    assert bad["override"] is None and bad["settings"].get("voice") is None   # no profile: nothing to load (no throw)


def test_wiring():
    text = _text()
    # the detail offers the button when there is something to fork
    assert 'forkDetail(d) ? `<button type="button" data-act="fork"' in text
    assert ">Open in voice lab</button>" in text
    # the Transcript dispatches, the lab listens (they share nothing else)
    assert 'document.dispatchEvent(new CustomEvent("aw:fork-to-lab", {detail: f}))' in text
    lab = text[text.index("const Lab = (() => {"):text.index("const Pronounce = (() => {")]
    assert 'document.addEventListener("aw:fork-to-lab"' in lab
    # a fork before the lab's first show waits for init: show() applies it
    assert "if (forkPending) applyFork();" in lab
    # applying it loads the profile the way the Profiles list's Load does
    apply = lab[lab.index("async function applyFork()"):]
    apply = apply[:apply.index("\n  }\n")]
    assert "loadProfile(f.profile)" in apply and 'setMode("preview")' in apply and "focus()" in apply
    # the line's language table ([profiles.x.langs.<lang>]) is loaded over the profile
    assert "forkSettings(profilesMap()[f.profile], lang)" in apply and "writeBuilder(fs.settings)" in apply
    assert "forkLang = {profile: f.profile, lang: fs.override}" in apply
    # Save profile then writes the builder to that language's row, not the base voice
    save = lab[lab.index("async function saveProfile()"):]
    save = save[:save.index("\n  }\n")]
    assert "forkLang.profile === name" in save and "row.voice = s.voice" in save
