"""Speaker colours: the 16-colour palette in afterword/ui/index.html (text
tones readable on --paper and --sheet in both schemes, names matching
config.SPEAKER_COLORS), the per-profile `color` key in config.toml, and the
API that reads and writes it (GET /api/status, GET /api/voices, PATCH
/api/lab/profiles/{name})."""

import re
from pathlib import Path

import pytest

from afterword.daemon.config import SPEAKER_COLORS, build_config, parse_color
from test_gui_api import H, _write_cfg, gui  # noqa: F401  (the fixture)

UI = Path(__file__).resolve().parent.parent / "afterword" / "ui" / "index.html"
PAPERS = {"light": ("#E8EBEE", "#F7F8F9"), "dark": ("#121417", "#1A1D21")}  # --paper, --sheet


def _lum(hx):
    c = [int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def _contrast(a, b):
    x, y = _lum(a), _lum(b)
    return (max(x, y) + 0.05) / (min(x, y) + 0.05)


def _palette():
    src = UI.read_text(encoding="utf-8")
    block = src[src.index("const SPEAKER_PALETTE = ["):]
    block = block[:block.index("];")]
    return re.findall(r'\["([a-z]+)", "(#[0-9A-F]{6})", "(#[0-9A-F]{6})", "(#[0-9A-F]{6})"\]', block)


# --------------------------------------------------------------------------- #
# The palette                                                                 #
# --------------------------------------------------------------------------- #

def test_palette_has_sixteen_named_colours_matching_the_config():
    pal = _palette()
    assert len(pal) == 16
    assert tuple(p[0] for p in pal) == SPEAKER_COLORS
    assert len({p[1] for p in pal}) == 16  # no two swatches alike


def test_palette_keeps_the_original_six_first():
    assert _palette()[:6] == [
        ("yellow", "#F5C400", "#6E5600", "#FFD43B"),
        ("cyan", "#00B8D9", "#00687A", "#4FD6EE"),
        ("green", "#2FBF71", "#1B6B40", "#63D99A"),
        ("magenta", "#D63AAF", "#8A1F70", "#F17ACF"),
        ("white", "#FFFFFF", "#3A4048", "#F2F4F6"),
        ("orange", "#F08C00", "#8A4B00", "#FFA94D"),
    ]


@pytest.mark.parametrize("entry", _palette(), ids=lambda e: e[0])
def test_palette_text_tones_meet_4_5_to_1(entry):
    _, _, light, dark = entry
    for paper in PAPERS["light"]:
        assert _contrast(light, paper) >= 4.5, (light, paper)
    for paper in PAPERS["dark"]:
        assert _contrast(dark, paper) >= 4.5, (dark, paper)


def test_palette_papers_match_the_css_tokens():
    src = UI.read_text(encoding="utf-8")
    assert "--paper: #E8EBEE; --sheet: #F7F8F9;" in src
    assert "--paper: #121417; --sheet: #1A1D21;" in src
    assert 'SPEAKER_PAPERS = {light: ["#E8EBEE", "#F7F8F9"], dark: ["#121417", "#1A1D21"]}' in src


# --------------------------------------------------------------------------- #
# config.toml                                                                 #
# --------------------------------------------------------------------------- #

def test_parse_color():
    assert parse_color("teal") == "teal"
    assert parse_color(" Teal ") == "teal"
    assert parse_color("#12AB9F") == "#12ab9f"
    assert parse_color("") == ""
    for bad in ("#abc", "12ab9f", "#12ab9g", "teal2", "rgb(1,2,3)", 5, None, ["teal"]):
        with pytest.raises(ValueError):
            parse_color(bad)


def test_profile_color_parses_with_warnings_for_bad_values():
    cfg, warnings = build_config({"profiles": {
        "a": {"voice": "say:Alex", "color": "Violet"},
        "b": {"voice": "say:Alex", "color": "#AABBCC"},
        "c": {"voice": "say:Alex"},
        "d": {"voice": "say:Alex", "color": "chartreuse"},
        "e": {"voice": "say:Alex", "color": 7},
    }})
    p = cfg.profiles
    assert (p["a"].color, p["b"].color, p["c"].color, p["d"].color, p["e"].color) == ("violet", "#aabbcc", "", "", "")
    assert any(w.startswith("profiles.d.color:") for w in warnings)
    assert any(w.startswith("profiles.e.color:") for w in warnings)
    assert not any(w.startswith(("profiles.a.", "profiles.b.", "profiles.c.")) for w in warnings)


# --------------------------------------------------------------------------- #
# API                                                                         #
# --------------------------------------------------------------------------- #

COLOURED = """# my voices
[profiles.nova]   # the first one
voice = "say:Samantha"
description = "calm"
color = "teal"   # keep my comment

[profiles.ember]
voice = "say:Alex"
"""


def test_status_and_voices_expose_colours(gui, aw_home):  # noqa: F811
    d, client = gui
    _write_cfg(aw_home, d, COLOURED)
    cfg = client.get("/api/status").json()["config"]
    assert cfg["profile_colors"]["nova"] == "teal" and cfg["profile_colors"]["ember"] is None
    assert set(cfg["profile_colors"]) == set(cfg["profiles"])
    voices = {p["name"]: p for p in client.get("/api/voices").json()["profiles"]}
    assert voices["nova"]["color"] == "teal" and voices["ember"]["color"] is None


def test_patch_color_sets_and_clears(gui, aw_home):  # noqa: F811
    d, client = gui
    _write_cfg(aw_home, d, COLOURED)
    r = client.patch("/api/lab/profiles/ember", json={"color": "#12AB9F"}, headers=H)
    assert r.status_code == 200 and r.json()["profile"]["color"] == "#12ab9f"
    assert "in_use" in r.json()["profile"]
    text = (aw_home / "config.toml").read_text()
    assert 'color = "#12ab9f"' in text
    assert d.store.get().profiles["ember"].color == "#12ab9f"
    # a palette name, together with a description, in one write
    r = client.patch("/api/lab/profiles/ember", json={"color": "Sky", "description": "bright"}, headers=H)
    assert r.status_code == 200 and r.json()["profile"]["color"] == "sky" and r.json()["profile"]["description"] == "bright"
    # "" clears it (automatic); comments and other keys stay
    r = client.patch("/api/lab/profiles/nova", json={"color": ""}, headers=H)
    assert r.status_code == 200 and r.json()["profile"]["color"] is None
    text = (aw_home / "config.toml").read_text()
    assert 'color = "teal"' not in text
    for kept in ("# my voices", "# the first one", 'description = "calm"', 'color = "sky"', 'description = "bright"'):
        assert kept in text
    assert client.get("/api/status").json()["config"]["profile_colors"] == {"ember": "sky", "nova": None}
    # the description alone leaves the colour alone
    client.patch("/api/lab/profiles/ember", json={"description": "x"}, headers=H)
    assert d.store.get().profiles["ember"].color == "sky"


def test_patch_color_errors(gui, aw_home):  # noqa: F811
    d, client = gui
    _write_cfg(aw_home, d, COLOURED)
    before = (aw_home / "config.toml").read_text()
    for bad in ("chartreuse", "#abc", "#12ab9g", 5, None, ["teal"]):
        r = client.patch("/api/lab/profiles/nova", json={"color": bad}, headers=H)
        assert r.status_code == 400, bad
    # a bad colour fails the whole request: the description is not written either
    assert client.patch("/api/lab/profiles/nova", json={"color": "nope", "description": "y"}, headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nope", json={"color": "teal"}, headers=H).status_code == 404
    assert client.patch("/api/lab/profiles/nova", json={"colour": "teal"}, headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nova", json={"color": "teal"}).status_code == 403  # no header
    assert (aw_home / "config.toml").read_text() == before


def test_lab_save_keeps_the_colour(gui, aw_home):  # noqa: F811
    d, client = gui
    _write_cfg(aw_home, d, COLOURED)
    r = client.post("/api/lab/profiles", json={"name": "nova", "voice": "say:Daniel", "overwrite": True}, headers=H)
    assert r.status_code == 200 and r.json()["profile"]["color"] == "teal"
    assert d.store.get().profiles["nova"].color == "teal"
