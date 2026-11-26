"""The shared-voice warning on the Sessions page (afterword/ui/index.html, between
"// ---------- shared voices" and "// ---------- end shared voices"), run in node:
which sessions are live (voice on, not muted, seen within the window sent by
GET /api/sessions as voice_in_use_s), which of them share a profile, and which
profile "Pick another" switches to (never the current one, only profiles no
live session uses, least recently used first). Plus static checks that the
button goes through the page's change() helper and the window is not
hard-coded in the page. Skipped when node is not installed."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

NOW = 1_800_000_000.0
WINDOW = 7200

HARNESS = r"""
const src = require("fs").readFileSync(0, "utf8");
const fns = new Function(src + "\nreturn {sharedVoiceLive, sharedVoiceWarnings, pickAnotherVoice};")();
const a = JSON.parse(process.argv[1]);
const out = {};
if (a.live) out.live = a.live.map((s) => fns.sharedVoiceLive(s, a.now, a.window));
if (a.sessions && !a.pick) {
  const w = fns.sharedVoiceWarnings(a.sessions, a.now, a.window);
  out.warnings = Object.fromEntries([...w].map(([id, v]) => [id, {profile: v.profile, others: v.others.map((o) => o.session_id)}]));
}
if (a.pick) {
  const s = a.sessions.find((x) => x.session_id === a.pick);
  out.pick = fns.pickAnotherVoice(s, a.sessions, a.profiles, a.lastUsed, a.now, a.window);
}
console.log(JSON.stringify(out));
"""


def _text() -> str:
    return UI.read_text(encoding="utf-8")


def _block() -> str:
    m = re.search(r"// ---------- shared voices\n(.*?)\n// ---------- end shared voices", _text(), re.S)
    assert m, "shared voices block not found in index.html"
    return m.group(1)


def _run(**args) -> dict:
    args.setdefault("now", NOW)
    args.setdefault("window", WINDOW)
    proc = subprocess.run([NODE, "-e", HARNESS, json.dumps(args)], input=_block(),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def sess(sid, profile="nova", ago=60, muted=False, **extra):
    return {"session_id": sid, "cwd": f"/w/{sid}", "profile": profile, "muted": muted,
            "last_seen": NOW - ago, "updated": NOW - ago, **extra}


def test_live_needs_voice_on_not_muted_and_recent():
    out = _run(live=[
        sess("ok"),
        sess("off", profile=None),
        sess("blank", profile=""),
        sess("muted", muted=True),
        sess("stale", ago=WINDOW + 1),
    ])
    assert out["live"] == [True, False, False, False, False]


def test_live_window_boundary_is_strictly_less():
    # Like the hooks' "in use" check (now - ts < VOICE_IN_USE_S): seen exactly
    # the window ago is no longer live; one second sooner still is.
    out = _run(live=[sess("edge", ago=WINDOW), sess("inside", ago=WINDOW - 1)])
    assert out["live"] == [False, True]


def test_live_falls_back_to_updated_without_last_seen():
    fresh = {"session_id": "u1", "profile": "nova", "muted": False, "last_seen": None, "updated": NOW - 10}
    stale = {"session_id": "u2", "profile": "nova", "muted": False, "updated": NOW - WINDOW - 5}
    never = {"session_id": "u3", "profile": "nova", "muted": False}
    assert _run(live=[fresh, stale, never])["live"] == [True, False, False]


def test_no_window_means_nothing_is_live():
    # An older daemon sends no voice_in_use_s: the page must not guess a window.
    assert _run(live=[sess("a")], window=None)["live"] == [False]
    assert _run(live=[sess("a")], window=0)["live"] == [False]
    # A session seen a moment "in the future" (clock skew) is still not live
    # without a window, nor with a negative one.
    assert _run(live=[sess("ahead", ago=-0.5)], window=0)["live"] == [False]
    assert _run(live=[sess("ahead", ago=-0.5)], window=-10)["live"] == [False]


def test_two_live_sessions_on_one_profile_both_warn():
    out = _run(sessions=[sess("a"), sess("b"), sess("c", profile="ember")])
    assert out["warnings"] == {
        "a": {"profile": "nova", "others": ["b"]},
        "b": {"profile": "nova", "others": ["a"]},
    }


def test_three_sharing_name_the_other_two():
    out = _run(sessions=[sess("a"), sess("b"), sess("c")])
    assert out["warnings"]["a"]["others"] == ["b", "c"]
    assert out["warnings"]["c"]["others"] == ["a", "b"]


def test_muted_off_and_stale_sessions_do_not_count_or_warn():
    out = _run(sessions=[
        sess("a"),
        sess("muted", muted=True),
        sess("off", profile=None),
        sess("stale", ago=WINDOW + 60),
        sess("edge", ago=WINDOW),
    ])
    assert out["warnings"] == {}


def test_distinct_profiles_do_not_warn():
    out = _run(sessions=[sess("a"), sess("b", profile="ember"), sess("c", profile="sage")])
    assert out["warnings"] == {}


def test_warning_clears_when_one_session_switches():
    both = _run(sessions=[sess("a"), sess("b")])["warnings"]
    after = _run(sessions=[sess("a"), sess("b", profile="ember")])["warnings"]
    assert set(both) == {"a", "b"} and after == {}


def test_pick_prefers_unused_then_least_recently_used():
    sessions = [sess("a"), sess("b"), sess("c", profile="ember")]
    # ember is used by a live session; sage and moss are free; moss was used longest ago.
    out = _run(sessions=sessions, pick="a", profiles=["nova", "ember", "sage", "moss"],
               lastUsed={"nova": NOW - 5, "ember": NOW - 1, "sage": NOW - 600, "moss": NOW - 9000})
    assert out["pick"] == {"name": "moss", "why": ""}


def test_pick_counts_never_used_as_oldest_and_keeps_config_order_on_ties():
    sessions = [sess("a"), sess("b")]
    out = _run(sessions=sessions, pick="a", profiles=["nova", "sage", "moss", "dawn"],
               lastUsed={"sage": NOW - 100})
    assert out["pick"]["name"] == "moss"   # moss and dawn never used: config order, before sage
    out = _run(sessions=sessions, pick="a", profiles=["nova", "sage", "moss"], lastUsed=None)
    assert out["pick"]["name"] == "sage"   # no usage data loaded: config order


def test_pick_never_returns_the_current_profile():
    # nova is the least recently used and nobody else is on it, but it is this session's own.
    sessions = [sess("a"), sess("b")]
    out = _run(sessions=sessions, pick="a", profiles=["nova", "ember"], lastUsed={"nova": NOW - 99999, "ember": NOW - 1})
    assert out["pick"]["name"] == "ember"


def test_pick_ignores_muted_off_and_stale_sessions_when_finding_free_profiles():
    sessions = [sess("a"), sess("b"), sess("m", profile="ember", muted=True),
                sess("s", profile="sage", ago=WINDOW + 10)]
    out = _run(sessions=sessions, pick="a", profiles=["nova", "ember", "sage"], lastUsed={"ember": NOW - 1, "sage": NOW - 2})
    assert out["pick"]["name"] == "sage"   # ember (muted) and sage (stale) are free; sage is older


def test_pick_none_when_every_other_profile_is_in_use():
    sessions = [sess("a"), sess("b"), sess("c", profile="ember"), sess("d", profile="sage")]
    out = _run(sessions=sessions, pick="a", profiles=["nova", "ember", "sage"], lastUsed={})
    assert out["pick"] == {"name": None, "why": "all-in-use"}


def test_pick_none_when_only_one_profile_exists():
    out = _run(sessions=[sess("a"), sess("b")], pick="a", profiles=["nova"], lastUsed={})
    assert out["pick"] == {"name": None, "why": "none-configured"}


def test_pick_unlisted_current_profile_does_not_hide_the_others():
    # "(not in config)" profile: still shared, and every configured profile is a candidate.
    sessions = [sess("a", profile="old"), sess("b", profile="old")]
    out = _run(sessions=sessions, pick="a", profiles=["nova", "ember"], lastUsed={})
    assert out["pick"]["name"] == "nova"


# ---- wiring in the page

def test_button_uses_the_existing_change_path():
    text = _text()
    assert re.search(r'"data-k": "pick"', text)
    assert re.search(r'btn\.addEventListener\("click", \(\) => \{ if \(pick\.name\) change\(s\.session_id, \{profile: pick\.name\}\); \}\);', text)
    # change() is the helper the voice chip uses: POST /api/sessions/<id> with the body, busy/pending, notes.
    chip = re.search(r'sel\.addEventListener\("change", \(\) => change\(id, \{profile: sel\.value \|\| null\}\)\);', text)
    assert chip, "voice chip no longer calls change()"
    assert 'api("POST", "/api/sessions/" + encodeURIComponent(id), body)' in text


def test_disabled_button_explains_why():
    text = _text()
    assert 'disabled: busy || !pick.name' in text
    assert "only voice in config" in text and "Every other voice is in use by a live session" in text


def test_window_comes_from_the_api_not_the_page():
    text = _text()
    assert 'voiceInUseS = isNum(r.data.voice_in_use_s) ? r.data.voice_in_use_s : 0;' in text
    assert "7200" not in _block() and "2 * 3600" not in _block()
