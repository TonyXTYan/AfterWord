"""The Voice lab's Engines mode (afterword/ui/index.html): the pure part that
turns GET /api/lab/catalog into one row per engine (default voice for the
text's language, state: ready / on disk / failed / not downloaded /
unavailable), run in node; plus the mode's wiring. The node part is skipped
when node is not installed."""

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
const E = new Function(src + "\nreturn {enGuessLang, enLang, enRows, enUsable};")();
const [cat, calls] = JSON.parse(process.argv[1]);
const out = {};
out.guess = ["Hello there", "重构已经完成了", "リファクタリングが終わって", "리팩터링을 마쳤고", "Привет", "", null].map(E.enGuessLang);
out.lang = [
  E.enLang("auto", "重构", cat.lang_codes), E.enLang("auto", "Hello", cat.lang_codes),
  E.enLang("ja", "Hello", cat.lang_codes), E.enLang("chinese", "Hello", cat.lang_codes), E.enLang("en-gb", "", cat.lang_codes),
  E.enLang("auto", "Привет", cat.lang_codes), E.enLang("", "重构", {}),
];
out.rows = {};
for (const [name, c, lang, opt] of calls) out.rows[name] = E.enRows(c === "cat" ? cat : c, lang, opt || {}).map((r) => ({...r, usable: E.enUsable(r)}));
console.log(JSON.stringify(out));
"""

CAT = {
    "engine": {
        "available": True,
        "pull_command": "uv run afterword models pull",
        "models": {
            "custom_voice": {"downloaded": True, "loaded": True, "error": None},
            "base": {"downloaded": True, "loaded": False, "error": None},
            "voice_design": {"downloaded": False, "loaded": False, "error": None},
            "kokoro": {"downloaded": True, "loaded": False, "error": "boom"},
            "chatterbox": {"downloaded": True, "loaded": True, "error": None},
        },
    },
    "speakers": [{"name": "Eric", "native": "chinese", "dialect": "sichuan"}, {"name": "Vivian", "native": "chinese", "dialect": None},
                 {"name": "Ryan", "native": "english", "dialect": None}],
    "lang_codes": {"en": "english", "zh": "chinese", "ja": "japanese"},
    "say_voices": [{"name": "Alex", "locale": "en_US"}, {"name": "Samantha", "locale": "en_US"}, {"name": "Tingting", "locale": "zh_CN"},
                   {"name": "Mei-Jia", "locale": "zh_TW"}],
    "kokoro_voices": [{"name": "af_bella", "lang": "American English", "lang_code": "en", "gender": "female"},
                      {"name": "af_heart", "lang": "American English", "lang_code": "en", "gender": "female"},
                      {"name": "zm_yunxi", "lang": "Mandarin Chinese", "lang_code": "zh", "gender": "male"}],
    "apple": {"available": True, "error": None},
    "apple_voices": [{"id": "com.apple.voice.premium.en-US.Ava", "name": "Ava", "lang": "en-US", "quality": "premium", "novelty": False},
                     {"id": "com.apple.speech.synthesis.voice.Bubbles", "name": "Bubbles", "lang": "en-US", "quality": "default", "novelty": True}],
    "clips": [],
}


def _block() -> str:
    text = UI.read_text(encoding="utf-8")
    m = re.search(r"  // ---------- engines \(pure\)\n(.*?)\n  // ---------- end engines \(pure\)", text, re.S)
    assert m, "engines (pure) block not found in index.html"
    return m.group(1)


@pytest.fixture(scope="module")
def out() -> dict:
    if NODE is None:
        pytest.skip("node is not installed")
    no_engine = {**CAT, "engine": {"available": False, "error": "mlx-audio missing", "models": {}},
                 "apple": {"available": False, "error": "no Swift"}, "say_voices": []}
    with_clips = {**CAT, "clips": [{"name": "me"}, {"name": "you"}]}
    calls = [["en", "cat", "en"], ["zh", "cat", "zh"], ["ja", "cat", "ja"], ["down", no_engine, "en"],
             ["clips", with_clips, "en", {"builderDesc": "A gravelly old pirate."}], ["empty", {}, "en"]]
    proc = subprocess.run([NODE, "-e", HARNESS, json.dumps([CAT, calls])], input=_block(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _by_kind(rows: list) -> dict:
    return {r["kind"]: r for r in rows}


def test_guess_language_from_script(out):
    assert out["guess"] == [None, "zh", "ja", "ko", "ru", None, None]


def test_language_builder_first_then_text_then_english(out):
    # send: what each render asks for. The builder's as set; the text's when its
    # script settles it (so Apple and Chatterbox do not fall back to English);
    # "auto" for Latin text, or a guess the daemon has no code for.
    assert out["lang"] == [
        {"lang": "zh", "from": "text", "send": "zh"}, {"lang": "en", "from": "default", "send": "auto"},
        {"lang": "ja", "from": "builder", "send": "ja"}, {"lang": "zh", "from": "builder", "send": "chinese"},
        {"lang": "en", "from": "builder", "send": "en-gb"},
        {"lang": "ru", "from": "text", "send": "auto"}, {"lang": "zh", "from": "text", "send": "auto"},
    ]


def test_one_row_per_engine_in_order(out):
    assert [r["kind"] for r in out["rows"]["en"]] == ["builtin", "design", "clone", "kokoro", "chatterbox", "apple", "say"]
    assert [r["label"] for r in out["rows"]["en"]] == ["Qwen built-in", "Qwen design", "Qwen clone", "Kokoro", "Chatterbox", "Apple", "macOS say"]


def test_states(out):
    r = _by_kind(out["rows"]["en"])
    assert r["builtin"]["state"] == "ready"
    assert r["design"]["state"] == "missing" and r["design"]["pull"] == "uv run afterword models pull voice_design"
    assert r["clone"]["state"] == "unavailable" and "Reference clips" in r["clone"]["why"]   # no clips
    assert r["kokoro"]["state"] == "failed" and "boom" in r["kokoro"]["why"]
    assert r["chatterbox"]["state"] == "unavailable"   # loaded, but nothing to clone
    assert r["apple"]["state"] == "ready" and r["say"]["state"] == "ready"
    assert [k for k, v in r.items() if v["usable"]] == ["builtin", "kokoro", "apple", "say"]
    c = _by_kind(out["rows"]["clips"])
    assert c["clone"]["state"] == "cold" and c["clone"]["usable"] and c["clone"]["def"] == "me"
    assert c["chatterbox"]["state"] == "ready" and [o["value"] for o in c["chatterbox"]["options"]] == ["me", "you"]


def test_defaults_follow_the_language(out):
    en, zh, ja = (_by_kind(out["rows"][k]) for k in ("en", "zh", "ja"))
    assert en["builtin"]["def"] == "Ryan"
    assert zh["builtin"]["def"] == "Vivian"   # a plain speaker over a dialect one
    assert ja["builtin"]["def"] == "Eric"     # nothing matches: the first
    assert en["kokoro"]["def"] == "af_heart" and zh["kokoro"]["def"] == "zm_yunxi"
    assert en["say"]["def"] == "Samantha" and zh["say"]["def"] == "Tingting"
    assert en["apple"]["def"] == "" and en["apple"]["options"][0]["value"] == ""
    assert all(o["value"] != "com.apple.speech.synthesis.voice.Bubbles" for o in en["apple"]["options"])   # novelty voices left out


def test_design_offers_the_builders_description_first(out):
    d = _by_kind(out["rows"]["clips"])["design"]
    assert d["options"][0] == {"value": "A gravelly old pirate.", "label": "The builder’s description"}
    assert d["def"] == "A gravelly old pirate."
    assert len(_by_kind(out["rows"]["en"])["design"]["options"]) == 3


def test_everything_unavailable_without_engines(out):
    r = _by_kind(out["rows"]["down"])
    for k in ("builtin", "design", "clone", "kokoro", "chatterbox"):
        assert r[k]["state"] == "unavailable" and "mlx-audio missing" in r[k]["why"]
    assert r["apple"]["state"] == "unavailable" and r["apple"]["why"] == "no Swift"
    assert r["say"]["state"] == "unavailable"
    assert not any(v["usable"] for v in r.values())
    assert all(v["state"] == "unavailable" for v in out["rows"]["empty"])


def test_wiring():
    text = UI.read_text(encoding="utf-8")
    assert '<input type="radio" name="lab-mode" value="engines">Engines</label>' in text
    assert '<div data-mode="engines" hidden>' in text
    assert 'const MODES = ["preview", "langs", "ab", "explore", "engines"];' in text
    assert "engines: doEngines" in text
    lab = text[text.index("const Lab = (() => {"):text.index("const Pronounce = (() => {")]
    run = lab[lab.index("async function doEngines()"):]
    run = run[:run.index("\n  }\n")]
    assert "playChain(" in run and 'loadLabel: "Use in builder"' in run
    assert "enCancel" in run and "setLabel(" in run
    # an engine comparison: no builder profile's pronunciations, tag or colour on the cards
    assert "previewBody(text, {voice, lang}, {profile: false})" in run
    assert "profile: null, noSpeaker: true" in run
    # the language the rows were picked for is the one sent (one shared helper)
    assert "const lang = enLangNow().send;" in run
    paint = lab[lab.index("function enPaint()"):]
    paint = paint[:paint.index("\n  }\n")]
    assert "const L = enLangNow();" in paint
    # a repaint keeps a row's state cell (a running Warm) while its state is unchanged
    assert "old && old.key === stKey ? old.node : enStateNode(row)" in paint
    # programmatic builder loads (Load, Use in builder, Open in voice lab) refresh the rows
    wb = lab[lab.index("function writeBuilder(s)"):]
    wb = wb[:wb.index("\n  }\n")]
    assert "enPaint();" in wb
    assert 'const EN_KEY = "aw.lab.engines";' in lab and "localStorage.setItem(EN_KEY" in lab
