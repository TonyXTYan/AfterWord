"""Design-v2 B: a voice per language, several <say> per reply, the script
splitter, and turns in the queue (part of E)."""

import asyncio
import json
import random
import subprocess
import sys
import time
import tomllib

import numpy as np
import pytest

from afterword.daemon.config import LangVoice, build_config, lang_code
from afterword.daemon.stream import SAMPLE_RATE
from afterword.daemon.synth import NullSynth, StreamResult
from afterword.hooks import scriptsplit, tags
from afterword.hooks.scriptsplit import split

from conftest import HOOK, hook_log, run_hook, stop_payload, wait_for, SYSTEM_PYTHON, needs_system_python, require_system_python
from test_hook import FakeDaemon, assert_clean_exit
from test_stream import make_daemon, read_wav

SR = SAMPLE_RATE
MIXED = "Not quite. 别急嘛，用 Python 再试一次。 Try again."

TUTOR = """
[daemon]
fallback = "say"

[profiles.tutor]
voice = "builtin:Ryan"
lang = "en"
pace = 0.95
ref_text = "english transcript"
say_voice = "Daniel"
max_chars = 60

[profiles.tutor.langs.zh]
voice = "builtin:Vivian"
say_voice = "Tingting"
pace = 1.1

[profiles.tutor.langs.ja]
say_voice = "Kyoko"

[profiles.plain]
voice = "builtin:Serena"
lang = "en"
"""


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Script splitter                                                              #
# --------------------------------------------------------------------------- #


def test_mixed_line_splits_en_zh_en_and_keeps_python_in_the_chinese_run():
    assert split(MIXED, "en") == [
        ("en", "Not quite."),
        ("zh", "别急嘛，用 Python 再试一次。"),
        ("en", "Try again."),
    ]


@pytest.mark.parametrize("text,primary,other,expected", [
    ("好的。", "en", None, [("zh", "好的。")]),
    ("我们来运行 npm test。", "en", None, [("zh", "我们来运行 npm test。")]),
    ("OK，我们开始吧。", "en", None, [("zh", "OK，我们开始吧。")]),  # short Latin clause in a CJK sentence
    ("Sure. 好，我们走吧。", "en", None, [("en", "Sure."), ("zh", "好，我们走吧。")]),  # tiny 好 joins its sentence
    ("我说 this is a really long English phrase 然后继续。", "en", None,
     [("zh", "我说"), ("en", "this is a really long English phrase"), ("zh", "然后继续。")]),
    ("はい。東京。行きます。", "en", None, [("ja", "はい。東京。行きます。")]),  # kanji-only sentence among Japanese
    ("東京大学に行きます。", "en", None, [("ja", "東京大学に行きます。")]),
    ("안녕하세요. Hello there.", "en", None, [("ko", "안녕하세요."), ("en", "Hello there.")]),
    ("3.14 is pi. 圆周率是 3.14。", "en", None, [("en", "3.14 is pi."), ("zh", "圆周率是 3.14。")]),
    ("A. 中文在这里。", "en", None, [("zh", "A. 中文在这里。")]),  # fewer than 2 letters: merged
    ("Just English, nothing else.", "en", None, [("en", "Just English, nothing else.")]),
    ("Run uv sync. 然后再试试。", "auto", None, [(None, "Run uv sync."), ("zh", "然后再试试。")]),
    ("这个可以。 This part is English.", "zh", "en", [("zh", "这个可以。"), ("en", "This part is English.")]),
    ("这个可以。 This part is English.", "zh", None, [("zh", "这个可以。 This part is English.")]),
    ("", "en", None, []),
    ("   ", "en", None, []),
    # An English sentence that quotes a CJK word stays English (no mid-clause
    # cut, not the CJK voice): the tutor's most common line.
    ("The word 你好 means hello.", "en", None, [("en", "The word 你好 means hello.")]),
    ("Say 谢谢 to thank someone.", "en", None, [("en", "Say 谢谢 to thank someone.")]),
    ("In Chinese, 谢谢 means thanks.", "en", None, [("en", "In Chinese, 谢谢 means thanks.")]),
    ("Use the 设置 menu to change it.", "en", None, [("en", "Use the 设置 menu to change it.")]),
    ("Open 设置 and then click the button.", "en", None, [("en", "Open 设置 and then click the button.")]),
    ("Say 你好 to greet someone, it means hello.", "en", None,
     [("en", "Say 你好 to greet someone, it means hello.")]),
    ("In Chinese, hello is 你好, and thanks is 谢谢.", "en", None,
     [("en", "In Chinese, hello is 你好, and thanks is 谢谢.")]),
    ('He said "你好." Then left.', "en", None, [("en", 'He said "你好." Then left.')]),
    ('Great, "谢谢" means thank you.', "en", None, [("en", 'Great, "谢谢" means thank you.')]),
    # A CJK clause of its own is cut at its clause boundaries.
    ("Next word: 苹果, which means apple.", "en", None,
     [("en", "Next word:"), ("zh", "苹果,"), ("en", "which means apple.")]),
    # Mostly-CJK clauses keep their edge Latin words.
    ("我喜欢 Python。", "en", None, [("zh", "我喜欢 Python。")]),
    ("我们今天学 present perfect tense 的用法。", "en", None, [("zh", "我们今天学 present perfect tense 的用法。")]),
    ("好的, let us go now.", "en", None, [("zh", "好的,"), ("en", "let us go now.")]),
    ("The word 你好 means hello.", "zh", "en", [("en", "The word 你好 means hello.")]),
    # A closing quote after the full stop still ends the sentence.
    ('他说 "好的。" Then he left.', "en", None, [("zh", '他说 "好的。"'), ("en", "Then he left.")]),
])
def test_split_cases(text, primary, other, expected):
    assert split(text, primary, other) == expected


def test_split_parts_are_substrings_in_order_and_never_raise():
    rng = random.Random(7)
    alphabet = list("abc XYZ.,!?;:，。！？、") + ["用", "试", "の", "カ", "한", "글", "1", "\u3000", "\ud800", "\n"]
    for _ in range(500):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 60)))
        parts = split(text, rng.choice(["en", "zh", None, "auto"]), rng.choice([None, "en"]))
        assert len(parts) <= scriptsplit.MAX_PARTS
        pos = 0
        for _lang, part in parts:
            assert part and part == part.strip()
            i = text.find(part, pos)
            assert i >= 0, (text, parts)
            pos = i + len(part)
    assert split(None, "en") == [] and split(123, "en") == []


def test_split_caps_the_number_of_parts():
    text = " ".join("Word number %d. 第%d句。" % (i, i) for i in range(20))
    parts = split(text, "en")
    assert len(parts) == scriptsplit.MAX_PARTS
    assert parts[-1][1].endswith("第19句。")


@needs_system_python
def test_splitter_runs_on_system_python():
    code = "import sys; sys.path.insert(0, %r); import scriptsplit; print(scriptsplit.split(%r, 'en'))" % (
        str(HOOK.parent), MIXED)
    out = subprocess.run([SYSTEM_PYTHON, "-S", "-I", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "Python 再试一次" in out.stdout and out.stdout.count("'en'") == 2


# --------------------------------------------------------------------------- #
# Several <say>                                                                #
# --------------------------------------------------------------------------- #


def test_every_nonempty_say_kept_in_order_at_most_four():
    r = tags.parse("<say>one</say> x <say lang='zh'>二</say> <say></say> <say>three</say> <say>four</say> <say>five</say>")
    assert [t.text for t in r.says] == ["one", "二", "three", "four"]
    assert r.say.text == "four"  # .say: the last kept one
    assert any("extra" in w and "at most 4" in w for w in r.warnings)
    d = r.to_dict()
    assert [s["text"] for s in d["segments"]] == ["one", "二", "three", "four"]
    assert d["segments"][1]["attrs"] == {"lang": "zh"} and d["say_count"] == 4


def test_says_inside_code_do_not_count():
    r = tags.parse("```\n<say>no</say>\n```\n<say>yes</say> `<say>no</say>`")
    assert [t.text for t in r.says] == ["yes"]


def test_limit_says_truncates_each_and_caps_the_total():
    says = tags.parse(
        "<say>" + "A short one. " + "word " * 30 + "</say>"
        "<say>Second sentence here. And more words to cut off later on.</say>"
        "<say>Third block is long too, with plenty of words in it.</say>"
    ).says
    out = tags.limit_says(says, 40)
    texts = [s["text"] for s in out]
    assert all(len(t) <= 40 for t in texts)
    assert sum(len(t) for t in texts) + len(texts) - 1 <= 80
    assert texts[0].startswith("A short one. word") and not texts[0].endswith(" ")
    assert len(texts) == 3
    # A later block that would get fewer than 20 characters is dropped.
    big = tags.parse("<say>" + "x" * 50 + "</say><say>" + "y" * 50 + "</say><say>z z z</say>").says
    assert [len(s["text"]) for s in tags.limit_says(big, 50)] == [50, 49]


# --------------------------------------------------------------------------- #
# Config: [profiles.x.langs.*]                                                 #
# --------------------------------------------------------------------------- #


def test_langs_parsed_normalised_and_inherited():
    cfg, warnings = build_config(tomllib.loads(TUTOR))
    assert warnings == []
    tutor = cfg.profiles["tutor"]
    assert set(tutor.langs) == {"zh", "ja"}
    assert tutor.langs["zh"] == LangVoice(voice="builtin:Vivian", say_voice="Tingting", pace=1.1)
    hash(tutor)  # still a frozen, hashable dataclass
    zh = cfg.voice_for("tutor", "zh")
    assert (zh.voice, zh.say_voice, zh.pace, zh.lang) == ("builtin:Vivian", "Tingting", 1.1, "zh")
    assert zh.ref_text == ""  # its own voice: the English clip's transcript is not inherited
    assert zh.max_chars == 60 and zh.name == "tutor"
    ja = cfg.voice_for(tutor, "japanese")  # Qwen names work too
    assert (ja.voice, ja.say_voice, ja.pace, ja.lang, ja.ref_text) == ("builtin:Ryan", "Kyoko", 0.95, "ja", "english transcript")
    fr = cfg.voice_for(tutor, "fr")  # no entry: the profile, lang set
    assert (fr.voice, fr.lang) == ("builtin:Ryan", "fr")
    assert cfg.voice_for(tutor, None) is tutor and cfg.voice_for(tutor, "auto") is tutor
    assert cfg.voice_for(tutor, "en") is tutor
    assert cfg.profiles["plain"].langs == {}


def test_cjk_language_without_a_say_voice_does_not_inherit_the_english_one():
    # macOS `say -v Samantha` renders Chinese or Japanese text as silence
    # (exit 0), so a zh/ja/ko part needs a voice of its own language.
    cfg, warnings = build_config(tomllib.loads("""
[profiles.t]
voice = "builtin:Ryan"
lang = "en"
say_voice = "Samantha"
[profiles.t.langs.ja]
voice = "say:Otoya"
[profiles.t.langs.zh]
voice = "builtin:Vivian"
[profiles.t.langs.fr]
voice = "builtin:Ryan"
[profiles.z]
voice = "builtin:Vivian"
lang = "zh"
say_voice = "Meijia"
"""))
    assert warnings == []
    assert cfg.voice_for("t", "ja").say_voice == "Otoya"  # its say: voice
    assert cfg.voice_for("t", "zh").say_voice == "Tingting"  # built-in for the language
    assert cfg.voice_for("t", "ko").say_voice == "Yuna"  # no table at all
    assert cfg.voice_for("t", "fr").say_voice == "Samantha"  # not CJK: inherits
    assert cfg.voice_for("t", "en").say_voice == "Samantha"
    assert cfg.voice_for("z", "zh").say_voice == "Meijia"  # its own primary language
    assert scriptsplit.say_voice_for("zh", "en", "  ", None, "Samantha") == "Tingting"
    assert scriptsplit.say_voice_for(None, "en", None, None, " ") is None


def test_lang_code():
    assert [lang_code(x) for x in ("zh", "ZH", "zh-CN", "zh_tw", "chinese", "Japanese", "xx", "", None, 3)] == [
        "zh", "zh", "zh", "zh", "zh", "ja", None, None, None, None]


def test_bad_langs_are_warned_and_skipped():
    cfg, warnings = build_config(tomllib.loads("""
[profiles.p]
voice = "builtin:Ryan"
[profiles.p.langs.zh]
voice = "nonsense"
pace = 9
colour = "blue"
[profiles.p.langs.chinese]
voice = "builtin:Serena"
[profiles.p.langs.klingon]
voice = "builtin:Serena"
[profiles.q]
voice = "builtin:Ryan"
langs = "zh"
"""))
    text = "\n".join(warnings)
    assert "profiles.p.langs.zh.voice" in text and "profiles.p.langs.zh.pace" in text
    assert "profiles.p.langs.zh.colour: unknown field" in text
    assert "profiles.p.langs.chinese: 'zh' is already set" in text
    assert "unknown language 'klingon'" in text
    assert "profiles.q.langs: expected a table" in text
    assert set(cfg.profiles["p"].langs) == {"zh"} and cfg.profiles["p"].langs["zh"].pace is None
    assert cfg.profiles["q"].langs == {}


# --------------------------------------------------------------------------- #
# Daemon: segments, voices, turns                                              #
# --------------------------------------------------------------------------- #


class RecSynth:
    """Records each segment it renders: (text, voice, lang, pace, say_voice)."""

    name = "fake"

    def __init__(self, seconds=0.1):
        self.seconds = seconds
        self.calls = []

    def _rec(self, text, profile, attrs):
        self.calls.append((text, profile.voice, attrs.get("lang") or profile.lang, profile.pace, profile.say_voice))

    def stream(self, text, profile, attrs, sink, cancel):
        self._rec(text, profile, attrs)
        sink(np.full(int(SR * self.seconds), 0.2, np.float32))
        return StreamResult(synth_s=0.01, audio_s=self.seconds, first_chunk_s=0.001, backend="fake", model="m")

    def synth(self, text, profile, attrs, out):
        self._rec(text, profile, attrs)
        return NullSynth(seconds=self.seconds).synth(text, profile, attrs, out)


def tutor_daemon(home, monkeypatch, synth=None):
    (home / "config.toml").write_text(TUTOR)
    d = make_daemon(home, monkeypatch, synth or RecSynth())
    return d


def hook_msg(says, session="s1", turn="turn-1", profile="tutor", **extra):
    msg = {
        "v": 1, "type": "hook", "event": "Stop", "t_hook": time.time(), "t_sent": time.time(),
        "session_id": session, "cwd": "/w/p", "profile": profile, "decision": "speak",
        "text": " ".join(s["text"] for s in says), "attrs": dict(says[0].get("attrs", {})) if says else {},
        "segments": says, "turn": turn,
    }
    msg.update(extra)
    return json.dumps(msg).encode()


async def drain(d, acks, timeout=10):
    worker = asyncio.create_task(d.worker())
    end = time.time() + timeout
    while time.time() < end:
        evs = [d.events.get(a["event_id"]) for a in acks]
        if all(e["status"] in ("spoken", "error", "skipped") for e in evs):
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    return [d.events.get(a["event_id"]) for a in acks]


def test_segments_rendered_in_order_with_the_voice_of_each_language(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)
    synth = d.synth

    async def go():
        ack = d.dispatch(hook_msg([{"text": MIXED, "attrs": {"mood": "dry"}}]), time.time())
        return (await drain(d, [ack]))[0]

    ev = run(go())
    assert ev["status"] == "spoken", ev
    assert synth.calls == [
        ("Not quite.", "builtin:Ryan", "en", 0.95, "Daniel"),
        ("别急嘛，用 Python 再试一次。", "builtin:Vivian", "zh", 1.1, "Tingting"),
        ("Try again.", "builtin:Ryan", "en", 0.95, "Daniel"),
    ]
    segs = ev["timings"]["segments"]
    assert [(s["lang"], s["voice"]) for s in segs] == [("en", "builtin:Ryan"), ("zh", "builtin:Vivian"), ("en", "builtin:Ryan")]
    # Three segments, two 250 ms gaps, one stream. The paces differ (0.95,
    # 1.1), so the clip holds the audio as heard (stretched per segment).
    played = np.concatenate(d.stream_player.last.played)
    assert abs(len(read_wav(ev["clip"])) - len(played)) < 16
    assert "playback_rate" not in ev["timings"]
    assert ev["timings"]["stream"] is True
    assert "spoken_text" not in ev["timings"]  # the split itself is not a rewrite


def test_lang_attr_is_not_split_and_several_says_play_in_order(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)

    async def go():
        says = [{"text": "Chinese 里面 English words.", "attrs": {"lang": "zh"}},
                {"text": "Then English.", "attrs": {}},
                {"text": "日本語です。", "attrs": {"lang": "auto"}}]
        return (await drain(d, [d.dispatch(hook_msg(says), time.time())]))[0]

    ev = run(go())
    assert ev["status"] == "spoken", ev
    assert [(c[0], c[1], c[2]) for c in d.synth.calls] == [
        ("Chinese 里面 English words.", "builtin:Vivian", "zh"),
        ("Then English.", "builtin:Ryan", "en"),
        ("日本語です。", "builtin:Ryan", "ja"),  # ja has no voice of its own: the profile's
    ]
    assert d.synth.calls[2][4] == "Kyoko"


def test_profile_without_langs_gets_one_segment_per_say(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)

    async def go():
        says = [{"text": MIXED, "attrs": {}}, {"text": "Second.", "attrs": {"pace": 1.2}}]
        return (await drain(d, [d.dispatch(hook_msg(says, profile="plain"), time.time())]))[0]

    ev = run(go())
    assert ev["status"] == "spoken", ev
    assert [c[0] for c in d.synth.calls] == [MIXED, "Second."]
    assert all(c[1] == "builtin:Serena" for c in d.synth.calls)


def test_pronunciations_apply_before_the_split(aw_home, monkeypatch):
    (aw_home / "config.toml").write_text(TUTOR + '\n[pronounce]\n"Python" = "派森"\n')
    d = make_daemon(aw_home, monkeypatch, RecSynth())

    async def go():
        return (await drain(d, [d.dispatch(hook_msg([{"text": MIXED, "attrs": {}}]), time.time())]))[0]

    ev = run(go())
    assert [c[0] for c in d.synth.calls][1] == "别急嘛，用 派森 再试一次。"
    assert ev["timings"]["spoken_text"] == MIXED.replace("Python", "派森")


def test_v1_path_joins_segments_into_one_clip(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)
    d.stream_player = None  # [daemon] stream = false, or no PortAudio
    played = []

    async def fake_play(path, rate):
        played.append((str(path), rate))
        return True

    d.player.play = fake_play

    async def go():
        return (await drain(d, [d.dispatch(hook_msg([{"text": MIXED, "attrs": {}}]), time.time())]))[0]

    ev = run(go())
    assert ev["status"] == "spoken", ev
    assert [c[1] for c in d.synth.calls] == ["builtin:Ryan", "builtin:Vivian", "builtin:Ryan"]
    t = ev["timings"]
    assert t["stream"] is False and len(t["segments"]) == 3
    # Mixed paces (0.95, 1.1) are baked into the clip, which plays at 1.0.
    assert played == [(ev["clip"], 1.0)] and "playback_rate" not in t
    clip = read_wav(ev["clip"])
    # Silent test audio: later segments lose 70 ms of leading silence, then
    # each is stretched by its own pace.
    n, trim, gap = int(0.1 * SR), int(0.07 * SR), int(0.25 * SR)
    expected = n / 0.95 + gap + (n - trim) / 1.1 + gap + (n - trim) / 0.95
    assert abs(len(clip) - expected) < 0.01 * SR


def test_v1_path_same_pace_keeps_a_playback_rate(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)
    d.stream_player = None
    played = []

    async def fake_play(path, rate):
        played.append(rate)
        return True

    d.player.play = fake_play

    async def go():
        says = [{"text": "One.", "attrs": {}}, {"text": "Two.", "attrs": {}}]
        return (await drain(d, [d.dispatch(hook_msg(says), time.time())]))[0]

    ev = run(go())
    assert played == [0.95] and ev["timings"]["playback_rate"] == 0.95
    # Un-stretched (the rate is applied at playback); the second segment's
    # leading 70 ms of (test) silence is trimmed.
    assert len(read_wav(ev["clip"])) == 2 * int(0.1 * SR) + int(0.25 * SR) - int(0.07 * SR)


def test_qwen_failure_falls_back_to_say_per_segment_with_its_say_voice(aw_home, monkeypatch):
    from afterword.daemon import server

    d = tutor_daemon(aw_home, monkeypatch)
    used = []

    class Boom(RecSynth):
        name = "qwen"

        def stream(self, *a, **k):
            raise RuntimeError("model missing")

    class FakeSay:
        def stream(self, text, profile, attrs, sink, cancel):
            used.append((text, profile.say_voice))
            sink(np.zeros(2400, np.float32))
            return StreamResult(synth_s=0.01, audio_s=0.1, backend="say")

    d.synth = Boom()
    monkeypatch.setattr(server, "SaySynth", FakeSay)

    async def go():
        return (await drain(d, [d.dispatch(hook_msg([{"text": MIXED, "attrs": {}}]), time.time())]))[0]

    ev = run(go())
    assert ev["status"] == "spoken", ev
    assert used == [("Not quite.", "Daniel"), ("别急嘛，用 Python 再试一次。", "Tingting"), ("Try again.", "Daniel")]


def test_hook_segments_are_validated(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)
    raw = hook_msg([], text="fallback text", segments=[None, {"text": 3}, {"text": "  ", "attrs": {}},
                                                       {"text": "ok", "attrs": {"pace": "fast", "lang": 5, "mood": "dry"}}])
    ack = d.dispatch(raw, time.time())
    item = d.queue._items[0]
    assert item.segments == [{"text": "ok", "attrs": {"mood": "dry"}}]
    assert d.events.get(ack["event_id"])["text"] == "fallback text"
    # No usable segments: the text is the one block (older hooks).
    d2 = d.dispatch(hook_msg([], session="s2", text="just text", segments="nope"), time.time())
    assert d.queue._items[1].segments == [{"text": "just text", "attrs": {}}] and d2["status"] == "queued"
    # Segments but no text: text is their join.
    d3 = d.dispatch(hook_msg([{"text": "a"}, {"text": "b"}], session="s3", text=""), time.time())
    assert d.events.get(d3["event_id"])["text"] == "a b"


def test_same_turn_appends_and_a_new_turn_supersedes(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)
    t = time.time()
    a1 = d.dispatch(hook_msg([{"text": "first"}], turn="T1"), t)
    a2 = d.dispatch(hook_msg([{"text": "second"}], turn="T1"), t)
    b1 = d.dispatch(hook_msg([{"text": "other session"}], session="s2", turn="X"), t)
    a3 = d.dispatch(hook_msg([{"text": "third"}], turn="T1"), t)
    assert [i.event_id for i in d.queue._items] == [a1["event_id"], a2["event_id"], a3["event_id"], b1["event_id"]]
    assert d.queue.snapshot()["pending"][0]["turn"] == "T1"
    c1 = d.dispatch(hook_msg([{"text": "new turn"}], turn="T2"), t)
    assert [i.event_id for i in d.queue._items] == [c1["event_id"], b1["event_id"]]
    for a in (a1, a2, a3):
        ev = d.events.get(a["event_id"])
        assert ev["status"] == "skipped" and ev["reason"] == f"superseded by #{c1['event_id']}"
    # Without a turn (older hooks): every line is its own turn, as in v1.
    n1 = d.dispatch(hook_msg([{"text": "x"}], session="s3", turn=None), t)
    n2 = d.dispatch(hook_msg([{"text": "y"}], session="s3", turn=None), t)
    assert d.events.get(n1["event_id"])["reason"] == f"superseded by #{n2['event_id']}"


def test_cli_test_message_speaks_every_say(aw_home, monkeypatch):
    d = tutor_daemon(aw_home, monkeypatch)
    raw = json.dumps({"type": "test", "profile": "tutor", "text": "<say>One.</say> <say lang='zh'>二。</say>"}).encode()

    async def go():
        return (await drain(d, [d.dispatch(raw, time.time())]))[0]

    ev = run(go())
    assert ev["text"] == "One. 二。" and ev["status"] == "spoken"
    assert [(c[0], c[1]) for c in d.synth.calls] == [("One.", "builtin:Ryan"), ("二。", "builtin:Vivian")]


def test_models_in_use_include_language_voices(aw_home, monkeypatch):
    (aw_home / "config.toml").write_text("""
[profiles.a]
voice = "builtin:Ryan"
[profiles.a.langs.zh]
voice = "clone:me-zh"
[profiles.a.langs.ja]
voice = "design:A calm voice"
""")
    d = make_daemon(aw_home, monkeypatch, RecSynth())
    assert d.models_in_use() == ["custom_voice", "base", "voice_design"]


# --------------------------------------------------------------------------- #
# Hook                                                                         #
# --------------------------------------------------------------------------- #


@pytest.fixture
def daemon_sock(aw_home):
    d = FakeDaemon(aw_home / "afterword.sock")
    yield d
    d.close()


def test_hook_sends_segments_text_and_turn(aw_home, daemon_sock):
    (aw_home / "config.toml").write_text(TUTOR)
    msg = ('<voice set="tutor"/><say>' + "First block. " * 8 + '</say> text '
           '<say lang="zh" mood="warm">第二。</say> <say>Third.</say>')
    proc, elapsed = run_hook(stop_payload(msg, prompt_id="p-123"))
    assert_clean_exit(proc, elapsed)
    sent = daemon_sock.received[0]
    segs = sent["segments"]
    assert [s["text"] for s in segs] == [tags.truncate(("First block. " * 8).strip(), 60), "第二。", "Third."]
    assert segs[1]["attrs"] == {"lang": "zh", "mood": "warm"}
    assert sent["text"] == " ".join(s["text"] for s in segs)
    assert sent["turn"] == "p-123" and sent["decision"] == "speak"
    assert len(sent["parsed"]["segments"]) == 3


def _argv_stub(tmp_path, monkeypatch):
    record = tmp_path / "said.jsonl"
    script = tmp_path / "fake_say_argv.py"
    script.write_text(
        "import json, sys\n"
        "data = sys.stdin.read()\n"
        "if '-v' in sys.argv and sys.argv[sys.argv.index('-v') + 1] == 'Nope':\n"
        "    sys.exit(1)\n"
        "open(%r, 'a', encoding='utf-8').write(json.dumps([sys.argv[1:], data]) + '\\n')\n" % str(record)
    )
    monkeypatch.setenv("AFTERWORD_SAY_CMD", "%s %s" % (sys.executable, script))
    return record


def _said(record, n):
    def got():
        if not record.exists():
            return None
        lines = [json.loads(x) for x in record.read_text().splitlines() if x.strip()]
        return lines if len(lines) >= n else None
    return wait_for(got, timeout=8)


@pytest.mark.parametrize("python", [None, SYSTEM_PYTHON])
def test_say_fallback_speaks_each_part_in_order_with_its_say_voice(aw_home, tmp_path, monkeypatch, python):
    if python:
        require_system_python()
    record = _argv_stub(tmp_path, monkeypatch)
    (aw_home / "config.toml").write_text(TUTOR)
    msg = '<voice set="tutor"/><say>%s</say><say lang="ja">はい。</say>' % MIXED
    proc, elapsed = run_hook(stop_payload(msg), python=python)
    assert_clean_exit(proc, elapsed)
    said = _said(record, 4)
    assert said == [
        [["-v", "Daniel"], "Not quite."],
        [["-v", "Tingting"], "别急嘛，用 Python 再试一次。"],
        [["-v", "Daniel"], "Try again."],
        [["-v", "Kyoko"], "はい。"],
    ]
    assert hook_log(aw_home)[-1]["fallback"] == "say"


def test_say_fallback_retries_an_unknown_voice_with_the_system_voice(aw_home, tmp_path, monkeypatch):
    record = _argv_stub(tmp_path, monkeypatch)
    (aw_home / "config.toml").write_text('[profiles.p]\nvoice = "builtin:Ryan"\nsay_voice = "Nope"\n')
    run_hook(stop_payload('<voice set="p"/><say>hello</say>'))
    assert _said(record, 1) == [[[], "hello"]]


def test_say_fallback_gives_cjk_parts_a_voice_of_their_language(aw_home, tmp_path, monkeypatch):
    record = _argv_stub(tmp_path, monkeypatch)
    (aw_home / "config.toml").write_text(
        '[profiles.t]\nvoice = "builtin:Ryan"\nlang = "en"\nsay_voice = "Samantha"\n'
        '[profiles.t.langs.ja]\nvoice = "say:Otoya"\n[profiles.t.langs.zh]\nvoice = "builtin:Vivian"\n')
    run_hook(stop_payload('<voice set="t"/><say>Fine. 好的。</say><say lang="ja">はい。</say>'))
    assert _said(record, 3) == [[["-v", "Samantha"], "Fine."], [["-v", "Tingting"], "好的。"], [["-v", "Otoya"], "はい。"]]


def _run_runner(jobs, timeout=20):
    from afterword.hooks import afterword_hook

    return subprocess.run([sys.executable, "-I", "-c", afterword_hook._SAY_RUNNER], input=json.dumps(jobs).encode(),
                          capture_output=True, timeout=timeout)


def _runner_stub(tmp_path, body):
    record = tmp_path / "ran.jsonl"
    script = tmp_path / "stub.py"
    script.write_text("import json, os, signal, sys, time\n"
                      "data = sys.stdin.read()\n"
                      "open(%r, 'a').write(json.dumps([sys.argv[1:], data]) + '\\n')\n" % str(record) + body)
    return record, [sys.executable, str(script)]


def test_say_runner_stops_everything_when_say_is_killed(tmp_path):
    # `killall say` must silence the fallback: no retry, no later parts.
    record, base = _runner_stub(tmp_path, "if '-v' in sys.argv: os.kill(os.getpid(), signal.SIGTERM)\n")
    proc = _run_runner([{"argvs": [base + ["-v", "X"], base], "text": "one"},
                        {"argvs": [base + ["-v", "X"], base], "text": "two"}])
    assert proc.returncode == 0
    lines = [json.loads(x) for x in record.read_text().splitlines()]
    assert lines == [[["-v", "X"], "one"]]


def test_say_runner_does_not_retry_a_part_that_ran_out_of_time(tmp_path):
    record, base = _runner_stub(tmp_path, "time.sleep(5)\n")
    t0 = time.time()
    proc = _run_runner([{"argvs": [base + ["-v", "X"], base], "text": "one", "limit": 0.5},
                        {"argvs": [base], "text": "two", "limit": 0.5}])
    assert proc.returncode == 0 and time.time() - t0 < 4
    lines = [json.loads(x) for x in record.read_text().splitlines()]
    assert lines == [[["-v", "X"], "one"], [[], "two"]]  # no restart of "one" in the system voice


def test_say_runner_retries_a_refused_voice(tmp_path):
    record, base = _runner_stub(tmp_path, "sys.exit(1 if '-v' in sys.argv else 0)\n")
    _run_runner([{"argvs": [base + ["-v", "X"], base], "text": "one"}])
    lines = [json.loads(x) for x in record.read_text().splitlines()]
    assert lines == [[["-v", "X"], "one"], [[], "one"]]


INLINE_LANGS = """
[daemon]
fallback = "say"

[profiles.t]
voice = "builtin:Ryan"
lang = "en"
say_voice = "Samantha"
langs = { zh = { voice = "builtin:Vivian", say_voice = "Tingting" } }

[profiles.d]
voice = "builtin:Ryan"
lang = "en"
langs.ja.say_voice = "Kyoko"
"""


@pytest.mark.parametrize("python", [None, SYSTEM_PYTHON])
def test_hook_reads_inline_and_dotted_langs(aw_home, tmp_path, monkeypatch, python):
    if python:
        require_system_python()
    from afterword.hooks import common

    (aw_home / "config.toml").write_text(INLINE_LANGS)
    cfg, warnings = build_config(tomllib.loads(INLINE_LANGS))
    assert warnings == [] and set(cfg.profiles["t"].langs) == {"zh"} and set(cfg.profiles["d"].langs) == {"ja"}
    for prof, langs in (("t", "en, zh"), ("d", "en, ja")):
        common.save_state("s-" + prof, {"profile": prof})
        payload = {"session_id": "s-" + prof, "prompt": "hi", "cwd": "/w/p", "hook_event_name": "UserPromptSubmit"}
        proc, _ = run_hook(payload, args=("user-prompt-submit",), python=python)
        assert "(profile %s: %s)" % (prof, langs) in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    record = _argv_stub(tmp_path, monkeypatch)
    proc, elapsed = run_hook(stop_payload('<voice set="t"/><say>Not quite. 别急嘛，再试一次。</say>'), python=python)
    assert_clean_exit(proc, elapsed)
    assert _said(record, 2) == [[["-v", "Samantha"], "Not quite."], [["-v", "Tingting"], "别急嘛，再试一次。"]]


def test_mini_toml_inline_tables_match_tomllib():
    from afterword.hooks import common

    text = ('[a]\nx = { p = 1, q = "s, }", r = { s = true, "t.u" = \'v\' }, w.z = 2.5 }  # note\n'
            'empty = {}\n')
    assert common._mini_toml(text) == tomllib.loads(text)
    # What the subset cannot read is left out, never raised or read as text.
    odd = '[a]\nx = { p = [1, {b = 2}], q = 3 }\nbad = { p = 1\nworse = { p = 1 } junk\ny = { = 1 }\nok = 1\n'
    assert common._mini_toml(odd) == {"a": {"x": {"q": 3}, "ok": 1}}


def test_say_fallback_without_voices_speaks_every_say_in_order(aw_home, say_stub):
    (aw_home / "config.toml").write_text('[profiles.p]\nvoice = "builtin:Ryan"\n')
    run_hook(stop_payload('<voice set="p"/><say>one</say><say>two</say>'))
    said = wait_for(lambda: say_stub.exists() and say_stub.read_text().count("\n") >= 2 and say_stub.read_text())
    assert said.split("\n")[:2] == ["one", "two"]


@pytest.mark.parametrize("python", [None, SYSTEM_PYTHON])
def test_prompt_note_lists_languages_for_a_profile_with_langs(aw_home, python):
    if python:
        require_system_python()
    from afterword.hooks import common

    (aw_home / "config.toml").write_text(TUTOR + '\n[profiles."x.y"]\nvoice = "builtin:Ryan"\n'
                                         '[profiles."x.y".langs.chinese]\nvoice = "builtin:Serena"\n')
    common.save_state("s1", {"profile": "tutor"})
    payload = {"session_id": "s1", "prompt": "hi", "cwd": "/w/p", "hook_event_name": "UserPromptSubmit"}
    proc, _ = run_hook(payload, args=("user-prompt-submit",), python=python)
    assert proc.returncode == 0 and proc.stderr == b""
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert ctx == ('AfterWord voice is on (profile tutor: en, zh, ja). Add <say lang="en|zh|ja">1-2 plain '
                   'spoken sentences</say>, one per language if you like; first one at the start is heard '
                   'sooner (optional mood="..."; no markdown inside).')
    common.save_state("s1", {"profile": "x.y"})
    proc, _ = run_hook(dict(payload, prompt="again"), args=("user-prompt-submit",), python=python)
    assert "(profile x.y: zh)" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    common.save_state("s1", {"profile": "plain"})
    proc, _ = run_hook(dict(payload, prompt="third"), args=("user-prompt-submit",), python=python)
    ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "End your reply with <say>" in ctx and "heard sooner" in ctx and "lang=" not in ctx


def test_hook_never_fails_on_odd_langs_config(aw_home, say_stub):
    (aw_home / "config.toml").write_text(
        '[profiles.p]\nvoice = "builtin:Ryan"\nlang = 5\nlangs = "zh"\n'
        '[profiles.q]\nlangs.zh = "x"\n[profiles.r.langs.zh]\nsay_voice = 7\n')
    for prof in ("p", "q", "r"):
        proc, elapsed = run_hook(stop_payload('<voice set="%s"/><say>%s</say>' % (prof, MIXED), session_id=prof))
        assert_clean_exit(proc, elapsed)
    assert wait_for(lambda: say_stub.exists() and say_stub.read_text().count("Not quite.") >= 2)


# --------------------------------------------------------------------------- #
# Voice lab: profiles with langs                                               #
# --------------------------------------------------------------------------- #

from test_lab import FakeEngine  # noqa: E402


def test_catalog_lists_langs(make_lab):
    lab = make_lab(config_text=TUTOR)
    profs = lab.catalog()["profiles"]
    assert profs["tutor"]["langs"] == {"zh": {"voice": "builtin:Vivian", "say_voice": "Tingting", "pace": 1.1},
                                       "ja": {"say_voice": "Kyoko"}}
    assert profs["plain"]["langs"] == {}


def test_save_profile_writes_replaces_and_keeps_langs(make_lab, home):
    lab = make_lab(config_text=TUTOR.replace("[profiles.tutor.langs.zh]", "[profiles.tutor.langs.chinese]  # mine"))
    base = {"name": "tutor", "voice": "builtin:Ryan", "lang": "en", "overwrite": True}
    out = lab.save_profile({**base, "langs": {"zh": {"voice": "builtin:Serena", "say_voice": "Tingting", "pace": 1.05},
                                              "ko": {"voice": "builtin:Sohee", "say_voice": ""}}})
    assert out["profile"]["langs"] == {"zh": {"voice": "builtin:Serena", "say_voice": "Tingting", "pace": 1.05},
                                       "ko": {"voice": "builtin:Sohee"}}
    text = (home / "config.toml").read_text()
    assert "[profiles.tutor.langs.chinese]  # mine" in text  # kept in place, with its comment
    assert "[profiles.tutor.langs.ja]" not in text and "[profiles.tutor.langs.ko]" in text
    assert lab.store.get().profiles["tutor"].langs["zh"].voice == "builtin:Serena"
    # Without `langs` the tables stay as they are.
    lab.save_profile({**base, "pace": 0.9})
    assert set(lab.store.get().profiles["tutor"].langs) == {"zh", "ko"}
    assert lab.store.get().profiles["tutor"].pace == 0.9
    # An empty object removes them all; a new profile can carry them.
    lab.save_profile({**base, "langs": {}})
    assert "langs" not in (home / "config.toml").read_text()
    lab.save_profile({"name": "fresh", "voice": "builtin:Ryan", "langs": {"japanese": {}}})
    assert set(lab.store.get().profiles["fresh"].langs) == {"ja"}
    assert lab.store.warnings == []


def test_save_profile_with_a_stray_key_in_a_lang_table(make_lab, home):
    # A typo in a language table (only warned about) must not block saves.
    lab = make_lab(config_text=TUTOR.replace('say_voice = "Kyoko"', 'say_voice = "Kyoko"\nsay_vioce = "Otoya"'))
    assert any("say_vioce" in w for w in lab.store.warnings)
    out = lab.save_profile({"name": "tutor", "voice": "builtin:Ryan", "lang": "en", "overwrite": True,
                            "langs": {"zh": {"voice": "builtin:Vivian"}, "ja": {"say_voice": "Kyoko", "pace": 1.2}}})
    assert out["profile"]["langs"]["ja"] == {"say_voice": "Kyoko", "pace": 1.2}
    assert 'say_vioce = "Otoya"' in (home / "config.toml").read_text()  # left alone


def test_delete_profile_removes_its_lang_tables(make_lab, home):
    lab = make_lab(config_text=TUTOR)
    lab.delete_profile("tutor")
    text = (home / "config.toml").read_text()
    assert "tutor" not in text and "[profiles.plain]" in text


@pytest.mark.parametrize("langs,needle", [
    ("zh", "langs must be an object"),
    ({"xx": {}}, "unknown language"),
    ({"zh": {}, "chinese": {}}, "given twice"),
    ({"zh": "builtin:Ryan"}, "must be an object"),
    ({"zh": {"colour": "blue"}}, "unknown field"),
    ({"zh": {"voice": "nonsense"}}, "langs.zh.voice"),
    ({"zh": {"pace": 5}}, "langs.zh: pace"),
])
def test_save_profile_rejects_bad_langs(make_lab, home, langs, needle):
    from afterword.daemon.lab import LabError

    lab = make_lab(config_text=TUTOR)
    before = (home / "config.toml").read_text()
    with pytest.raises(LabError) as exc:
        lab.save_profile({"name": "tutor", "voice": "builtin:Ryan", "overwrite": True, "langs": langs})
    assert exc.value.status == 400 and needle in exc.value.message
    assert (home / "config.toml").read_text() == before


def test_preview_of_a_profile_uses_the_voice_for_the_requested_language(make_lab):
    eng = FakeEngine()  # its fake model knows Serena, Ryan and Aiden
    lab = make_lab(engine=eng, config_text=TUTOR.replace("builtin:Vivian", "builtin:Aiden"))
    res = run(lab.preview({"text": "你好。", "profile": "tutor", "lang": "zh"}))
    assert res["settings"]["voice"] == "builtin:Aiden" and res["pace"] == 1.1
    kw = eng.models_made["custom_voice"].calls[-1][1]
    assert kw["speaker"] == "aiden" and kw["language"] == "chinese"
    res = run(lab.preview({"text": "Hello.", "profile": "tutor"}))  # primary language: the profile's voice
    assert res["settings"]["voice"] == "builtin:Ryan" and res["pace"] == 0.95
    res = run(lab.preview({"text": "你好。", "profile": "tutor", "lang": "zh", "voice": "builtin:Serena"}))
    assert res["settings"]["voice"] == "builtin:Serena"  # an explicit voice still wins
