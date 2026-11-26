"""Speaker colours: the 64-colour palette in afterword/ui/index.html (16 hue
families × base, deep, light, pale; text tones readable on --paper and
--sheet in both schemes, no two swatches closer than CIEDE2000 9, names
matching config.SPEAKER_COLORS), the per-profile `color` key in
config.toml, and the API that reads and writes it (GET /api/status, GET
/api/voices, PATCH /api/lab/profiles/{name})."""

import itertools
import math
import re
from pathlib import Path

import pytest

from afterword.daemon.config import SPEAKER_COLORS, SPEAKER_FAMILIES, build_config, parse_color
from test_gui_api import H, _write_cfg

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
    return re.findall(r'\["([a-z]+(?:-[a-z]+)?)", "(#[0-9A-F]{6})", "(#[0-9A-F]{6})", "(#[0-9A-F]{6})"\]', block)


# CIEDE2000 between two "#RRGGBB" colours (sRGB, D65), after Sharma, Wu and
# Dalal (2005).
def _lab(hx):
    c = [int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    xyz = ((0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047,
           0.2126729 * r + 0.7151522 * g + 0.0721750 * b,
           (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883)
    fx, fy, fz = (t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116 for t in xyz)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _de2000(h1, h2):
    return _de2000_lab(_lab(h1), _lab(h2))


def _de2000_lab(lab1, lab2):
    (L1, a1, b1), (L2, a2, b2) = lab1, lab2
    cb = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(cb ** 7 / (cb ** 7 + 25 ** 7)))
    a1, a2 = (1 + g) * a1, (1 + g) * a2
    c1, c2 = math.hypot(a1, b1), math.hypot(a2, b2)
    h1 = math.degrees(math.atan2(b1, a1)) % 360 if c1 else 0.0
    h2 = math.degrees(math.atan2(b2, a2)) % 360 if c2 else 0.0
    dh = 0.0 if c1 * c2 == 0 else (h2 - h1 + 180) % 360 - 180
    dL, dC, dH = L2 - L1, c2 - c1, 2 * math.sqrt(c1 * c2) * math.sin(math.radians(dh / 2))
    lb, cbp = (L1 + L2) / 2, (c1 + c2) / 2
    if c1 * c2 == 0:
        hb = h1 + h2
    elif abs(h1 - h2) <= 180:
        hb = (h1 + h2) / 2
    else:
        hb = (h1 + h2 + 360) / 2 if h1 + h2 < 360 else (h1 + h2 - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hb - 30)) + 0.24 * math.cos(math.radians(2 * hb))
         + 0.32 * math.cos(math.radians(3 * hb + 6)) - 0.20 * math.cos(math.radians(4 * hb - 63)))
    sl = 1 + 0.015 * (lb - 50) ** 2 / math.sqrt(20 + (lb - 50) ** 2)
    sc, sh = 1 + 0.045 * cbp, 1 + 0.015 * cbp * t
    rt = -math.sin(math.radians(60 * math.exp(-((hb - 275) / 25) ** 2))) * 2 * math.sqrt(cbp ** 7 / (cbp ** 7 + 25 ** 7))
    return math.sqrt((dL / sl) ** 2 + (dC / sc) ** 2 + (dH / sh) ** 2 + rt * (dC / sc) * (dH / sh))


# No two palette swatches closer than this (CIEDE2000; about 1 is a just
# noticeable difference). The palette as shipped reaches 9.08 (sky / sky-light).
MIN_DE2000 = 9.0


# --------------------------------------------------------------------------- #
# The palette                                                                 #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("lab1, lab2, want", [
    # from the test data of Sharma, Wu and Dalal (2005), pairs 1, 7, 17, 25 and 34
    ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
    ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
    ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
    ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ((22.7233, 20.0904, -46.6940), (23.0331, 14.9730, -42.5619), 2.0373),
])
def test_de2000_reference_pairs(lab1, lab2, want):
    assert _de2000_lab(lab1, lab2) == pytest.approx(want, abs=1e-4)
    assert _de2000_lab(lab2, lab1) == pytest.approx(want, abs=1e-4)
    assert _de2000("#000000", "#FFFFFF") == pytest.approx(100, abs=0.01)


def test_palette_has_sixty_four_named_colours_matching_the_config():
    pal = _palette()
    assert len(pal) == 64
    assert tuple(p[0] for p in pal) == SPEAKER_COLORS
    assert len({p[1] for p in pal}) == 64  # no two swatches alike
    # four rows of the same sixteen families: base, deep, light, pale
    for row, suffix in enumerate(("", "-deep", "-light", "-pale")):
        assert tuple(p[0] for p in pal[row * 16:row * 16 + 16]) == tuple(f + suffix for f in SPEAKER_FAMILIES)


def test_palette_first_sixteen_are_unchanged():
    assert _palette()[:16] == [
        ("yellow", "#F5C400", "#6E5600", "#FFD43B"),
        ("cyan", "#00B8D9", "#00687A", "#4FD6EE"),
        ("green", "#2FBF71", "#1B6B40", "#63D99A"),
        ("magenta", "#D63AAF", "#8A1F70", "#F17ACF"),
        ("white", "#FFFFFF", "#3A4048", "#F2F4F6"),
        ("orange", "#F08C00", "#8A4B00", "#FFA94D"),
        ("blue", "#3B5BDB", "#3451C7", "#8DA2FB"),
        ("red", "#E03131", "#B71C1C", "#FF8A8A"),
        ("violet", "#9061F9", "#6A35E0", "#B69CFF"),
        ("lime", "#94D82D", "#4D7215", "#A9E34B"),
        ("pink", "#FF8FAB", "#B4235A", "#FFA8C0"),
        ("brown", "#8D5524", "#7A4A1F", "#D9A066"),
        ("teal", "#0B8A80", "#08665F", "#3CC7B9"),
        ("grey", "#868E96", "#5C636B", "#ADB5BD"),
        ("olive", "#8F9A1F", "#5C6312", "#C3CF48"),
        ("sky", "#74C0FC", "#0B5FA5", "#8ACBFF"),
    ]


def test_palette_swatches_are_far_apart():
    pal = _palette()
    closest = min(((_de2000(a[1], b[1]), a[0], b[0]) for a, b in itertools.combinations(pal, 2)))
    assert closest[0] >= MIN_DE2000, closest


def _l(hx):
    return _lab(hx)[0]


def test_palette_shades_step_from_their_base():
    # deep is darker than the base, light lighter, pale lighter still
    # (white, already the lightest, steps down: light grey, then off-whites)
    pal = {p[0]: p[1] for p in _palette()}
    for f in SPEAKER_FAMILIES:
        base, deep, light, pale = (pal[f + s] for s in ("", "-deep", "-light", "-pale"))
        assert _l(deep) < _l(base) and _l(deep) < _l(light) < _l(pale), f
        if f != "white":
            assert _l(base) < _l(light), f


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
    assert parse_color("Teal-Deep") == "teal-deep" and parse_color("white-pale") == "white-pale"
    assert parse_color("#12AB9F") == "#12ab9f"
    assert parse_color("") == ""
    for bad in ("#abc", "12ab9f", "#12ab9g", "teal2", "teal-dark", "deep", "teal deep", "rgb(1,2,3)", 5, None, ["teal"]):
        with pytest.raises(ValueError):
            parse_color(bad)


def test_profile_color_parses_with_warnings_for_bad_values():
    cfg, warnings = build_config({"profiles": {
        "a": {"voice": "say:Alex", "color": "Violet"},
        "f": {"voice": "say:Alex", "color": "sky-pale"},
        "b": {"voice": "say:Alex", "color": "#AABBCC"},
        "c": {"voice": "say:Alex"},
        "d": {"voice": "say:Alex", "color": "chartreuse"},
        "e": {"voice": "say:Alex", "color": 7},
    }})
    p = cfg.profiles
    assert (p["a"].color, p["b"].color, p["c"].color, p["d"].color, p["e"].color) == ("violet", "#aabbcc", "", "", "")
    assert p["f"].color == "sky-pale"
    assert any(w.startswith("profiles.d.color:") for w in warnings)
    assert any(w.startswith("profiles.e.color:") for w in warnings)
    assert not any(w.startswith(("profiles.a.", "profiles.b.", "profiles.c.", "profiles.f.")) for w in warnings)


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
