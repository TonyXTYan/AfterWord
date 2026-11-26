"""Pronunciation list (docs/design-v2.md section D): the matching rules, the
config tables, where it is applied (daemon, lab, hook fallback) and
GET/POST /api/pronounce."""

import asyncio
import json
import os
import subprocess
import time
import tomllib

import pytest
from fastapi.testclient import TestClient

from afterword.daemon.config import DEFAULT_CONFIG_TOML, build_config
from afterword.hooks import common, pronounce
from conftest import TIME_SCALE, ROOT, run_hook, stop_payload, wait_for
from test_lab import FakeEngine, _daemon

H = {"X-AfterWord": "1"}
SYSTEM_PYTHON = "/usr/bin/python3"


def say(text, table):
    return pronounce.apply(text, tuple(table.items()))


# --------------------------------------------------------------------------- #
# Matching rules                                                               #
# --------------------------------------------------------------------------- #


def test_word_boundaries():
    t = {"uv": "you vee", "__init__": "dunder init", "kubectl": "cube control"}
    assert say("Use uv, not uvicorn or fuv.", t) == "Use you vee, not uvicorn or fuv."
    assert say("uv_tool and uv-tool and uv2", t) == "uv_tool and you vee-tool and uv2"
    assert say("see pkg/__init__.py", t) == "see pkg/dunder init.py"
    assert say("kubectl-apply", t) == "cube control-apply"


def test_punctuation_edges_match_anywhere_but_word_edges_still_guard():
    t = {"->": " to ", "C++": "see plus plus", ".NET": "dot net"}
    assert say("a->b", t) == "a to b"
    assert say("C++ and C++17", t) == "see plus plus and see plus plus17"
    assert say("ABC++", t) == "ABC++"          # starts with a letter: needs a boundary before it
    assert say("ASP.NET and .NETCore", t) == "ASPdot net and .NETCore"


def test_smart_case():
    t = {"sqlite": "sequel lite", "GIF": "jif"}
    assert say("SQLite, sqlite, SQLITE", t) == "sequel lite, sequel lite, sequel lite"
    assert say("GIF gif Gif", t) == "jif gif Gif"


def test_exact_key_beats_lowercase_key_of_same_length():
    assert say("GIF gif", {"gif": "giff", "GIF": "jif"}) == "jif giff"


def test_longest_first():
    t = {"uv": "you vee", "uv run": "you vee run it", "git": "git", "github": "git hub"}
    assert say("uv run main.py; uv sync", t) == "you vee run it main.py; you vee sync"
    assert say("github and git", t) == "git hub and git"


def test_single_pass_never_rematches_a_replacement():
    t = {"a": "b", "b": "c", "SQL": "sequel", "sequel": "WRONG"}
    assert say("a b", t) == "b c"
    assert say("SQL", t) == "sequel"


def test_cjk_keys_match_inside_cjk_text_and_cjk_is_not_a_word_character():
    t = {"张三": "Zhang San", "uv": "you vee", "東京": "とうきょう", "한국": "Hanguk"}
    assert say("我是张三的朋友", t) == "我是Zhang San的朋友"
    assert say("用uv安装", t) == "用you vee安装"
    assert say("東京タワー", t) == "とうきょうタワー"
    assert say("한국어", t) == "Hanguk어"
    assert not pronounce.is_word_char("张") and pronounce.is_word_char("é") and pronounce.is_word_char("_")


@pytest.mark.parametrize(
    "key,text,expected",
    [
        ("a.b", "a.b axb", "X axb"),
        ("(x)", "f(x) and x", "fX and x"),
        ("$HOME", "cd $HOME now", "cd X now"),
        ("\\d", "\\d and 5", "X and 5"),
        ("a*", "a* aa", "X aa"),
        ("[ok]", "[ok] o k", "X o k"),
        ("x|y", "x|y x y", "X x y"),
        ("^v", "^v v", "X v"),
    ],
)
def test_regex_metacharacters_in_keys_are_literal(key, text, expected):
    assert say(text, {key: "X"}) == expected


def test_replacement_is_literal_text():
    assert say("x", {"x": "\\1 \\g<0> $&"}) == "\\1 \\g<0> $&"


def test_empty_and_bad_tables_are_harmless():
    assert pronounce.apply("hello", ()) == "hello"
    assert pronounce.apply("", (("a", "b"),)) == ""
    assert pronounce.apply("hello", (("", "x"),)) == "hello"
    assert pronounce.clean_table("nope") == ()
    errors = []
    pairs = pronounce.clean_table({"ok": "fine", "n": 3, " ": "x", "k" * 101: "v", "t": {"a": "b"}}, errors)
    assert pairs == (("ok", "fine"),) and len(errors) == 4


def test_merge_profile_overrides_global():
    pairs = pronounce.merge((("uv", "you vee"), ("GIF", "gif")), (("GIF", "jif"),))
    assert pairs == (("GIF", "jif"), ("uv", "you vee"))
    assert pronounce.apply("GIF uv", pairs) == "jif you vee"


def test_large_table_is_fast():
    pairs = tuple(("word%d" % i, "spoken %d" % i) for i in range(pronounce.MAX_ENTRIES))
    text = " ".join("word%d" % (i * 7 % 600) for i in range(200))
    t = time.perf_counter()
    out = pronounce.apply(text, pairs)
    assert time.perf_counter() - t < 0.5 * TIME_SCALE
    assert out.startswith("spoken 0 spoken 7")


def _reference_apply(text, pairs):
    """The rules as one regex with per-key lookarounds (the first, slow
    implementation): the scan in pronounce.apply must agree with it."""
    import re

    W = pronounce._W
    order = sorted(range(len(pairs)), key=lambda i: (-len(pairs[i][0]), pairs[i][0] == pairs[i][0].lower(), i))
    parts = []
    for i in order:
        k = pairs[i][0]
        body = re.escape(k)
        if k == k.lower():
            body = "(?i:" + body + ")"
        if pronounce.is_word_char(k[0]):
            body = "(?<!" + W + ")" + body
        if pronounce.is_word_char(k[-1]):
            body += "(?!" + W + ")"
        parts.append("(" + body + ")")
    values = [pairs[i][1] for i in order]
    return re.compile("|".join(parts)).sub(lambda m: values[m.lastindex - 1], text)


def test_scan_agrees_with_the_lookaround_regex():
    import random

    rng = random.Random(1234)
    alphabet = "abAB_1 -.+>张三用ü"
    for _ in range(400):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        keys = {}
        for _ in range(rng.randint(1, 8)):
            k = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 4)))
            if k.strip():
                keys[k] = "<%d>" % len(keys)
        pairs = tuple(keys.items())
        assert pronounce.apply(text, pairs) == _reference_apply(text, pairs), (text, pairs)


def test_merged_tables_with_every_key_present_stay_fast():
    # Review finding: one lookaround class per key cost ~1 ms each to compile
    # (seconds for a merged 500 + 500 table) on every new line.
    glob = tuple(("g%03dx" % i, "G%d" % i) for i in range(pronounce.MAX_ENTRIES))
    prof = tuple(("P%03dY" % i, "p%d" % i) for i in range(pronounce.MAX_ENTRIES))
    pairs = pronounce.merge(glob, prof)
    text = " ".join(k for k, _ in pairs)[:5000]
    t = time.perf_counter()
    out = pronounce.apply(text, pairs)
    assert time.perf_counter() - t < 0.3 * TIME_SCALE
    assert out.startswith("p0 p1 ")
    short = tuple((("io", "id", "in", "on")[i % 4] + ("" if i < 4 else str(i)), "x") for i in range(500))
    t = time.perf_counter()
    pronounce.apply(("Pinion idiot station within dominion " * 140)[:5000], short)
    assert time.perf_counter() - t < 0.3 * TIME_SCALE


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system python3")
def test_module_runs_on_system_python():
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import pronounce\n"
        "print(pronounce.apply('Use uv, not uvicorn; 用uv装 张三 C++', "
        "(('uv', 'you vee'), ('张三', 'Zhang'), ('C++', 'cpp'))))\n" % str(ROOT / "afterword" / "hooks")
    )
    out = subprocess.run([SYSTEM_PYTHON, "-I", "-c", code], capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr.decode()
    assert out.stdout.decode().strip() == "Use you vee, not uvicorn; 用you vee装 Zhang cpp"


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #


CONFIG = """\
[pronounce]                 # applied to every line
"SQLite" = "sequel lite"
"uv" = "you vee"
'C++' = "see plus plus"
"张三" = "Zhang San"

[profiles.nova]
voice = "builtin:Serena"

[profiles.tutor]
voice = "builtin:Ryan"

[profiles.tutor.pronounce]  # overrides global entries
"GIF" = "jif"
"uv" = "oov"
"""


def test_build_config_reads_global_and_profile_tables():
    cfg, warnings = build_config(tomllib.loads(CONFIG))
    assert warnings == []
    assert dict(cfg.pronounce) == {"SQLite": "sequel lite", "uv": "you vee", "C++": "see plus plus", "张三": "Zhang San"}
    assert dict(cfg.profiles["tutor"].pronounce) == {"GIF": "jif", "uv": "oov"}
    assert cfg.speakable("uv GIF SQLite", "tutor") == "oov jif sequel lite"
    assert cfg.speakable("uv GIF SQLite", "nova") == "you vee GIF sequel lite"
    assert cfg.speakable("uv", None) == "you vee"  # unknown/None borrows the first profile (nova)


def test_build_config_reports_bad_entries():
    _, warnings = build_config({"pronounce": {"a": 1, "": "x"}, "profiles": {"p": {"voice": "builtin:Ryan", "pronounce": "x"}}})
    assert "pronounce.a: expected a string, got 1" in warnings
    assert "pronounce: empty key skipped" in warnings
    assert any(w.startswith("profiles.p.pronounce: expected a table") for w in warnings)


def test_default_config_has_a_commented_example_table():
    assert "\n[pronounce]\n# Pronunciations:" in DEFAULT_CONFIG_TOML
    assert '\n# "SQLite" = "sequel lite"\n' in DEFAULT_CONFIG_TOML
    cfg, warnings = build_config(tomllib.loads(DEFAULT_CONFIG_TOML))
    assert warnings == [] and cfg.pronounce == ()


# --------------------------------------------------------------------------- #
# The hook's mini TOML parser and say fallback                                 #
# --------------------------------------------------------------------------- #


def test_mini_toml_reads_quoted_and_literal_keys():
    text = CONFIG + '"say \\"hi\\"" = "q"  # comment\n"a=b # c" = "eq"\n'
    mini = common._mini_toml(text)
    assert mini == tomllib.loads(text)
    pairs = pronounce.pairs_from_config(mini, "tutor")
    assert dict(pairs)["uv"] == "oov" and dict(pairs)["C++"] == "see plus plus"


def test_mini_toml_reads_what_the_gui_writes(tmp_path):
    from afterword.daemon.lab import write_pronounce

    path = tmp_path / "config.toml"
    path.write_text(CONFIG)
    entries = {"SQLite": "sequel lite", "C++": "see plus plus", "a\\b\"c": "q", "it's": "it is", "東京": "Tokyo", "x.y": "x y"}
    write_pronounce(path, None, entries)
    write_pronounce(path, "nova", {"GIF": "jif"})
    text = path.read_text()
    assert common._mini_toml(text) == tomllib.loads(text)
    assert dict(pronounce.pairs_from_config(common._mini_toml(text), "nova")) == {"GIF": "jif", **entries}


DOTTED = '''\
[pronounce]
"uv" = ["you", "vee"]          # an array by mistake: the daemon skips it
"m" = """multi
fake = "inside the string"
line"""
"ok" = "fine"
'three' = """one-line triple"""

[profiles.tutor]
voice = "builtin:Ryan"
pronounce.GIF = "jif"
"pronounce" . 'C++' = "see plus plus"
'''


def test_mini_toml_reads_dotted_keys_and_skips_what_it_cannot_read():
    # Review findings: dotted keys were ignored on Python < 3.11, and values
    # the subset could not parse came back as raw TOML text to be spoken.
    mini = common._mini_toml(DOTTED)
    full = tomllib.loads(DOTTED)
    assert mini["profiles"] == full["profiles"]
    # The array as tomllib reads it (pairs_from_config skips it); no multi-line text.
    assert mini["pronounce"] == {"uv": ["you", "vee"], "ok": "fine", "three": "one-line triple"}
    assert "fake" not in mini["pronounce"]
    got = dict(pronounce.pairs_from_config(mini, "tutor"))
    assert got == {"GIF": "jif", "C++": "see plus plus", "ok": "fine", "three": "one-line triple"}
    assert common._mini_toml('x = "a\\tb\\u00e9\\U0001F600 \\"q\\" \\\\"\ny = "\\q"\nz = "a" "b"\n') == {
        "x": 'a\tbé😀 "q" \\'}


def test_mini_toml_reads_dotted_keys_the_gui_keeps(tmp_path):
    from afterword.daemon.lab import write_pronounce

    path = tmp_path / "config.toml"
    path.write_text('[profiles.tutor]\nvoice = "builtin:Ryan"\npronounce.GIF = "jif"\n')
    write_pronounce(path, "tutor", {"GIF": "jif", "uv": "you vee"})
    text = path.read_text()
    assert common._mini_toml(text) == tomllib.loads(text)
    assert dict(pronounce.pairs_from_config(common._mini_toml(text), "tutor")) == {"GIF": "jif", "uv": "you vee"}


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system python3")
def test_hook_say_fallback_on_system_python_with_dotted_keys_and_odd_values(aw_home, say_stub):
    (aw_home / "config.toml").write_text(DOTTED)
    proc, _ = run_hook(stop_payload('<voice set="tutor"/><say>Make a GIF in C++ with uv.</say>'), python=SYSTEM_PYTHON)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "Make a jif in see plus plus with uv."


def test_hook_say_fallback_speaks_the_pronounced_text(aw_home, say_stub):
    (aw_home / "config.toml").write_text(CONFIG)
    proc, _ = run_hook(stop_payload('<voice set="tutor"/><say>Run uv on the GIF, SQLite too.</say>'))
    assert proc.returncode == 0 and proc.stdout == b""
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "Run oov on the jif, sequel lite too."
    spool = [json.loads(line) for line in (aw_home / "spool.jsonl").read_text().splitlines()]
    assert spool[-1]["text"] == "Run uv on the GIF, SQLite too."  # the log keeps the original


@pytest.mark.skipif(not os.path.exists(SYSTEM_PYTHON), reason="no system python3")
def test_hook_say_fallback_on_system_python(aw_home, say_stub):
    (aw_home / "config.toml").write_text(CONFIG)
    proc, _ = run_hook(stop_payload('<voice set="nova"/><say>张三 uses uv and C++.</say>'), python=SYSTEM_PYTHON)
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().strip())
    assert said == "Zhang San uses you vee and see plus plus."


@pytest.mark.parametrize("table", ['[pronounce]\n"x" = [1, 2]\n', '[pronounce]\n"' + "k" * 5000 + '" = "v"\n',
                                   "pronounce = 5\n", "[pronounce]\n'unterminated = \"x\"\n", "[pronounce.deep]\na = 'b'\n"])
def test_hook_survives_odd_pronounce_tables(aw_home, say_stub, table):
    (aw_home / "config.toml").write_text(table + '[profiles.nova]\nvoice = "builtin:Ryan"\n')
    proc, _ = run_hook(stop_payload('<voice set="nova"/><say>still spoken</say>'))
    assert proc.returncode == 0 and proc.stdout == b"" and proc.stderr == b""
    assert wait_for(lambda: say_stub.exists() and "still spoken" in say_stub.read_text())


# --------------------------------------------------------------------------- #
# Daemon and lab                                                               #
# --------------------------------------------------------------------------- #


def _play(d, profile, *texts):
    async def main():
        worker = asyncio.create_task(d.worker())
        out = []
        for text in texts:
            ack = d.dispatch(json.dumps({"type": "test", "text": text, "profile": profile}).encode(), time.time())
            for _ in range(200):
                ev = d.events.get(ack["event_id"])
                if ev["status"] in ("spoken", "error", "skipped"):
                    break
                await asyncio.sleep(0.02)
            out.append(ev)
        worker.cancel()
        return out

    return asyncio.run(main())


def test_daemon_speaks_pronounced_text_and_logs_the_original(home, monkeypatch):
    (home / "config.toml").write_text(CONFIG)
    engine = FakeEngine()
    d = _daemon(home, monkeypatch, engine)
    ev, plain = _play(d, "tutor", "Ship the GIF with uv.", "Nothing to change.")
    assert ev["status"] == "spoken", ev
    assert ev["text"] == "Ship the GIF with uv."
    assert ev["timings"]["spoken_text"] == "Ship the jif with oov."
    calls = [kw["text"] for kind, kw in engine.models_made["custom_voice"].calls]
    assert "Ship the jif with oov." in calls and "Ship the GIF with uv." not in calls
    assert plain["status"] == "spoken" and "spoken_text" not in plain["timings"]


def test_lab_preview_applies_the_list_unless_skipped(make_lab):  # noqa: F811
    engine = FakeEngine()
    lab = make_lab(engine, CONFIG)
    res = asyncio.run(lab.preview({"text": "uv and GIF", "profile": "tutor"}))
    assert res["text"] == "uv and GIF" and res["spoken_text"] == "oov and jif" and res["skip_pronounce"] is False
    assert engine.models_made["custom_voice"].calls[-1][1]["text"] == "oov and jif"
    res = asyncio.run(lab.preview({"text": "uv and GIF", "voice": "builtin:Ryan"}))  # no profile: global only
    assert res["spoken_text"] == "you vee and GIF"
    res = asyncio.run(lab.preview({"text": "uv and GIF", "profile": "tutor", "skip_pronounce": True}))
    assert res["spoken_text"] is None and engine.models_made["custom_voice"].calls[-1][1]["text"] == "uv and GIF"
    from afterword.daemon.lab import LabError

    with pytest.raises(LabError) as e:
        asyncio.run(lab.preview({"text": "x", "voice": "builtin:Ryan", "skip_pronounce": "yes"}))
    assert e.value.status == 400


def test_lab_preview_pronounce_profile_picks_the_table_without_changing_the_voice(make_lab):  # noqa: F811
    # Review finding: builder previews never applied [profiles.x.pronounce].
    engine = FakeEngine()
    lab = make_lab(engine, CONFIG)
    res = asyncio.run(lab.preview({"text": "uv and GIF", "voice": "builtin:Aiden", "pronounce_profile": "tutor"}))
    assert res["spoken_text"] == "oov and jif" and res["pronounce_profile"] == "tutor"
    assert res["settings"]["voice"] == "builtin:Aiden"  # the profile is not the settings base
    res = asyncio.run(lab.preview({"text": "uv and GIF", "voice": "builtin:Aiden"}))
    assert res["spoken_text"] == "you vee and GIF" and res["pronounce_profile"] is None
    res = asyncio.run(lab.preview({"text": "uv", "profile": "tutor", "pronounce_profile": "nova"}))
    assert res["spoken_text"] == "you vee" and res["settings"]["voice"] == "builtin:Ryan"
    res = asyncio.run(lab.preview({"text": "uv", "voice": "builtin:Aiden", "pronounce_profile": "tutor",
                                   "skip_pronounce": True}))
    assert res["spoken_text"] is None and res["pronounce_profile"] is None
    from afterword.daemon.lab import LabError

    with pytest.raises(LabError) as e:
        asyncio.run(lab.preview({"text": "x", "voice": "builtin:Ryan", "pronounce_profile": "nope"}))
    assert e.value.status == 404


def test_frozen_preview_transcript_is_the_spoken_text(make_lab):  # noqa: F811
    lab = make_lab(None, CONFIG)
    res = asyncio.run(lab.preview({"text": "A long narration about SQLite and uv.", "voice": "design:A warm narrator"}))
    clip = lab.freeze({"preview_id": res["id"], "name": "narrator"})["clip"]
    assert clip["ref_text"] == "A long narration about sequel lite and you vee."


def test_saving_a_profile_keeps_its_pronounce_table(make_lab, home):  # noqa: F811
    lab = make_lab(None, CONFIG)
    lab.save_profile({"name": "tutor", "voice": "builtin:Aiden", "seed": 7, "overwrite": True})
    cfg = lab.store.get()
    assert cfg.profiles["tutor"].seed == 7 and dict(cfg.profiles["tutor"].pronounce) == {"GIF": "jif", "uv": "oov"}


# --------------------------------------------------------------------------- #
# GET/POST /api/pronounce                                                      #
# --------------------------------------------------------------------------- #


@pytest.fixture
def gui(home, monkeypatch):
    from afterword.daemon.web import build_app

    (home / "config.toml").write_text("# my notes\n" + CONFIG + "# trailing note\n")
    d = _daemon(home, monkeypatch, FakeEngine())
    return d, TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800", raise_server_exceptions=False)


def test_api_get(gui):
    _, client = gui
    body = client.get("/api/pronounce").json()
    assert body["global"]["SQLite"] == "sequel lite" and body["global"]["C++"] == "see plus plus"
    assert body["profiles"] == {"nova": {}, "tutor": {"GIF": "jif", "uv": "oov"}}
    assert body["default_profile"] == "nova" and body["warnings"] == [] and body["limits"]["entries"] == 500


def test_api_round_trip_keeps_comments(gui, home):
    d, client = gui
    new = {"SQLite": "sequel lite", "kubectl": "cube control", "张三": "Zhang San"}
    r = client.post("/api/pronounce", json={"global": new}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["global"] == new
    text = (home / "config.toml").read_text()
    for comment in ("# my notes", "# applied to every line", "# overrides global entries", "# trailing note"):
        assert comment in text
    assert '"uv"' not in text.split("[profiles.nova]")[0]  # removed from the global table
    assert d.store.get().speakable("kubectl", "nova") == "cube control"  # hot-reloaded

    r = client.post("/api/pronounce", json={"profile": "nova", "entries": {"GIF": "jif", "uv": "uv"}}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["profiles"]["nova"] == {"GIF": "jif", "uv": "uv"}
    r = client.post("/api/pronounce", json={"profile": "tutor", "entries": {}}, headers=H)
    assert r.json()["profiles"]["tutor"] == {}
    text = (home / "config.toml").read_text()
    assert "[profiles.nova.pronounce]" in text and "# overrides global entries" in text
    cfg, warnings = build_config(tomllib.loads(text))
    assert warnings == [] and cfg.profiles["nova"].voice == "builtin:Serena"
    assert client.get("/api/pronounce").json() == r.json()

    r = client.post("/api/pronounce", json={"profile": None, "entries": {}}, headers=H)
    assert r.status_code == 200 and r.json()["global"] == {}


def test_api_creates_the_global_table_when_missing(gui, home):
    _, client = gui
    (home / "config.toml").write_text('# hi\n[profiles.nova]\nvoice = "builtin:Serena"\n')
    r = client.post("/api/pronounce", json={"global": {"uv": "you vee"}}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["global"] == {"uv": "you vee"}
    assert (home / "config.toml").read_text().startswith("# hi\n")


@pytest.mark.parametrize(
    "body,status",
    [
        ({"global": {"x": 1}}, 400),
        ({"global": {" ": "x"}}, 400),
        ({"global": {"k" * 101: "v"}}, 400),
        ({"global": {"k": "v" * 301}}, 400),
        ({"global": {"w%d" % i: "x" for i in range(501)}}, 400),
        ({"global": ["x"]}, 400),
        ({"profile": "nope", "entries": {"a": "b"}}, 404),
        ({"profile": 5, "entries": {}}, 400),
        ({"profile": "", "entries": {"zz": "zed"}}, 400),  # not the global table
        ({"entries": None}, 400),
        ({}, 400),
        ([], 400),
    ],
)
def test_api_rejects(gui, home, body, status):
    _, client = gui
    before = (home / "config.toml").read_text()
    r = client.post("/api/pronounce", json=body, headers=H)
    assert r.status_code == status, r.text
    assert "error" in r.json()
    assert (home / "config.toml").read_text() == before


def test_api_save_keeps_entries_the_gui_cannot_show(gui, home):
    # Review finding: GET leaves out invalid entries, so a save used to delete them.
    _, client = gui
    (home / "config.toml").write_text(
        '[pronounce]\n"kubectl" = ["cube control"]  # a mistake to fix by hand\n"uv" = "you vee"\n'
        '"' + "k" * 101 + '" = "too long"\n"old" = "gone"\n'
        '[profiles.nova]\nvoice = "builtin:Serena"\n')
    view = client.get("/api/pronounce").json()
    assert view["global"] == {"uv": "you vee", "old": "gone"}
    assert any("kubectl" in w for w in view["warnings"])
    r = client.post("/api/pronounce", json={"global": {"uv": "you vee", "SQLite": "sequel lite"}}, headers=H)
    assert r.status_code == 200, r.text
    parsed = tomllib.loads((home / "config.toml").read_text())["pronounce"]
    assert parsed == {"kubectl": ["cube control"], "uv": "you vee", "k" * 101: "too long", "SQLite": "sequel lite"}
    assert "# a mistake to fix by hand" in (home / "config.toml").read_text()
    # Posting a hidden key replaces it: that is how the GUI fixes one.
    r = client.post("/api/pronounce", json={"global": {"kubectl": "cube control"}}, headers=H)
    assert tomllib.loads((home / "config.toml").read_text())["pronounce"] == {
        "kubectl": "cube control", "k" * 101: "too long"}


def test_config_writes_are_serialized(home, monkeypatch):
    # Review finding: the pronunciation save runs in a worker thread; a profile
    # save landing between its read and its write was lost.
    import threading

    from afterword.daemon import lab as labmod

    path = home / "config.toml"
    path.write_text(CONFIG)
    loaded, release = threading.Event(), threading.Event()
    real_load = labmod._load_doc

    def slow_load(p):
        doc = real_load(p)
        if threading.current_thread().name == "pron":
            loaded.set()
            release.wait(5)
        return doc

    monkeypatch.setattr(labmod, "_load_doc", slow_load)
    t = threading.Thread(target=labmod.write_pronounce, args=(path, None, {"zz": "zed"}), name="pron")
    t.start()
    assert loaded.wait(5)
    saver = threading.Thread(target=labmod.write_profile, args=(path, "added", {"voice": "builtin:Aiden"}, False))
    saver.start()
    saver.join(0.2)
    assert saver.is_alive()  # waits for the pronunciation write instead of racing it
    release.set()
    t.join(5)
    saver.join(5)
    parsed = tomllib.loads(path.read_text())
    assert parsed["pronounce"] == {"zz": "zed"} and parsed["profiles"]["added"]["voice"] == "builtin:Aiden"


def test_api_needs_the_header_and_a_parsable_file(gui, home):
    _, client = gui
    assert client.post("/api/pronounce", json={"global": {}}).status_code == 403
    (home / "config.toml").write_text("[pronounce\nbroken")
    r = client.post("/api/pronounce", json={"global": {"a": "b"}}, headers=H)
    assert r.status_code == 400 and "could not be updated" in r.json()["error"]
    assert (home / "config.toml").read_text() == "[pronounce\nbroken"
