"""More speech engines: Kokoro and Chatterbox on the shared engine, Apple
voices through the Swift helper, and the model fallback chain."""

import asyncio
import json
import struct
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from afterword.daemon import apple, qwen, server, voices
from afterword.daemon.config import ConfigStore, Profile, build_config
from afterword.daemon.stream import SAMPLE_RATE, StreamResampler, resample
from afterword.daemon.synth import QwenSynth, StreamResult

from test_lab import FakeEngine, FakeModel, _Result
from test_langs import drain, hook_msg
from test_stream import make_daemon


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Voice specs and languages                                                   #
# --------------------------------------------------------------------------- #


def test_new_voice_kinds_parse():
    assert voices.parse_voice("kokoro:ZF_Xiaobei") == voices.VoiceSpec("kokoro", "zf_xiaobei")
    assert voices.parse_voice("kokoro:af_heart,af_bella").value == "af_heart,af_bella"
    assert voices.parse_voice("chatterbox:tutor-en").model == "chatterbox"
    assert voices.parse_voice("apple:").value == ""  # the best voice for the language
    assert voices.parse_voice("apple:Ava (Premium)").value == "Ava (Premium)"
    assert voices.parse_voice("apple:Ava").model is None
    for spec, backend in [("builtin:Ryan", "qwen"), ("clone:x", "qwen"), ("design:calm", "qwen"),
                          ("kokoro:af_heart", "kokoro"), ("chatterbox:x", "chatterbox"),
                          ("apple:", "apple"), ("say:Daniel", "say")]:
        assert voices.parse_voice(spec).backend == backend
    for bad in ["kokoro:", "kokoro:heart", "kokoro:xf_bad", "chatterbox:", "apple:" + "x" * 201]:
        with pytest.raises(voices.VoiceError):
            voices.parse_voice(bad)


def test_lang_helpers():
    assert voices.lang_iso("auto") is None and voices.lang_iso(None) is None
    assert voices.lang_iso("ZH") == "zh" and voices.lang_iso("japanese") == "ja"
    assert voices.lang_iso("en-GB") == "en-gb" and voices.lang_iso("pt-BR") == "pt"
    # Kokoro: the line's language wins; auto follows the voice's first letter.
    assert voices.kokoro_lang("zf_xiaobei", "zh") == "z"
    assert voices.kokoro_lang("zf_xiaobei", None) == "z"
    assert voices.kokoro_lang("af_heart", "ja") == "j"
    assert voices.kokoro_lang("bf_emma", "en") == "b"  # British voice keeps its accent
    assert voices.kokoro_lang("af_heart", "en-gb") == "b"
    with pytest.raises(voices.VoiceError, match="Kokoro has no ko"):
        voices.kokoro_lang("af_heart", "ko")
    assert voices.chatterbox_lang(None) == "en"
    assert voices.chatterbox_lang("ko") == "ko"
    with pytest.raises(voices.VoiceError, match="Chatterbox has no vi"):
        voices.chatterbox_lang("vi")


def test_kokoro_voices_from_a_snapshot(tmp_path):
    (tmp_path / "voices").mkdir()
    for name in ["af_heart", "zm_yunxi", "bf_emma", "not-a-voice"]:
        (tmp_path / "voices" / f"{name}.safetensors").write_bytes(b"x")
    (tmp_path / "voices" / "af_heart.pt").write_bytes(b"x")
    got = voices.kokoro_voices(tmp_path)
    assert [v["name"] for v in got] == ["af_heart", "bf_emma", "zm_yunxi"]
    assert got[0] == {"name": "af_heart", "lang": "American English", "lang_code": "en", "gender": "female"}
    assert got[1]["lang_code"] == "en-gb" and got[2]["lang_code"] == "zh" and got[2]["gender"] == "male"
    assert voices.kokoro_voices(None) == [] and voices.kokoro_voices(tmp_path / "nope") == []


def test_build_request_for_new_kinds(tmp_path):
    clips = voices.ClipStore(tmp_path)
    clips.add("me", voices.silence_wav(2.0, 24000))
    req = qwen.build_request("hi", voices.parse_voice("chatterbox:me"), clips, lang="ja", style="calm",
                             mood="dry", ref_text="ignored", seed=3, temperature=0.7)
    assert req.ref_audio == tmp_path / "me.wav" and req.ref_text is None and req.instruct is None
    assert req.lang == "ja" and req.seed == 3 and req.temperature == 0.7
    req = qwen.build_request("hi", voices.parse_voice("kokoro:af_heart"), clips, lang="auto", style="calm",
                             mood=None, ref_text="x", seed=None, temperature=None)
    assert req.lang is None and req.instruct is None and req.ref_text is None
    with pytest.raises(voices.VoiceError, match="Kokoro has no ko"):
        qwen.build_request("hi", voices.parse_voice("kokoro:af_heart"), clips, lang="ko", style=None,
                           mood=None, ref_text=None, seed=None, temperature=None)
    with pytest.raises(voices.VoiceError, match="no reference clip"):
        qwen.build_request("hi", voices.parse_voice("chatterbox:nobody"), clips, lang=None, style=None,
                           mood=None, ref_text=None, seed=None, temperature=None)
    assert not qwen.uses_style(voices.parse_voice("kokoro:af_heart"))
    assert not qwen.uses_style(voices.parse_voice("chatterbox:me"))


# --------------------------------------------------------------------------- #
# The engine                                                                  #
# --------------------------------------------------------------------------- #


class KokoroModel(FakeModel):
    def generate(self, **kw):
        self.calls.append(("kokoro", kw))
        yield _Result(0.2)
        yield _Result(0.3)


class ChatterboxModel(FakeModel):
    def __init__(self, kind):
        super().__init__(kind)
        self.prepared = []

    def prepare_conditionals(self, ref_wav, ref_sr):
        self.prepared.append((ref_wav, ref_sr))
        return {"conds": ref_wav}

    def generate(self, **kw):
        self.calls.append(("chatterbox", kw))
        yield _Result(0.4)


class MultiEngine(FakeEngine):
    def __init__(self, extras_missing=(), **kw):
        self.extras_missing = set(extras_missing)
        kw.setdefault("downloaded", qwen.MODEL_KINDS)
        super().__init__(**kw)

    def _extra_snapshot(self, repo):
        return None if repo in self.extras_missing else Path("/fake") / repo

    def _load_model(self, kind, path):
        if kind == "kokoro":
            model = KokoroModel(kind)
        elif kind == "chatterbox":
            model = ChatterboxModel(kind)
        else:
            return super()._load_model(kind, path)
        self.models_made[kind] = model
        self.loads.append((kind, str(path)))
        return model


def _req(spec, clips=None, **kw):
    kw.setdefault("lang", None)
    return qwen.build_request(kw.pop("text", "Hello there. Second one."), voices.parse_voice(spec), clips,
                              style=None, mood=None, ref_text=None, seed=kw.pop("seed", None),
                              temperature=kw.pop("temperature", None), **kw)


def test_kokoro_render_on_the_engine():
    eng = MultiEngine()
    try:
        chunks = []
        res = eng.render(_req("kokoro:zf_xiaobei", lang="zh"), limit=False,
                         on_chunk=lambda a, r: chunks.append(a.size), stream=True)
        kind, kw = eng.models_made["kokoro"].calls[-1]
        assert kw["voice"] == "zf_xiaobei" and kw["lang_code"] == "z" and kw["speed"] == 1.0
        assert kw["split_pattern"] == qwen.SENTENCE_SPLIT and "stream" not in kw
        assert res.model == "kokoro" and res.seed is None  # deterministic: no seed recorded
        assert len(chunks) == 2 and res.audio.size == sum(chunks)
    finally:
        eng.shutdown()


def test_sentence_split_pattern():
    import re

    parts = [p for p in re.split(qwen.SENTENCE_SPLIT, "One. Two! 三。四！Five?") if p.strip()]
    assert parts == ["One.", "Two!", "三。", "四！", "Five?"]


def test_chatterbox_render_caches_the_clip_conditioning(tmp_path):
    clips = voices.ClipStore(tmp_path)
    clips.add("me", voices.silence_wav(2.0, 24000))
    eng = MultiEngine()
    try:
        r1 = eng.render(_req("chatterbox:me", clips, lang="ja", seed=7, temperature=0.6), limit=False, stream=True)
        eng.render(_req("chatterbox:me", clips, lang="en"), limit=False)
        model = eng.models_made["chatterbox"]
        assert model.prepared == [(str(tmp_path / "me.wav"), qwen_sr())]
        (_, kw1), (_, kw2) = model.calls
        assert kw1["lang_code"] == "ja" and kw1["temperature"] == 0.6 and kw1["stream"] is True
        assert kw1["conds"] == {"conds": str(tmp_path / "me.wav")} and kw1["verbose"] is False
        assert 0 < kw1["max_new_tokens"] <= qwen.CHATTERBOX_MAX_TOKENS
        assert kw2["lang_code"] == "en" and "temperature" not in kw2 and "stream" not in kw2
        assert r1.seed == 7 and r1.model == "chatterbox"
        # A new clip file (same name) is conditioned again.
        time.sleep(0.01)
        clips.add("me", voices.silence_wav(3.0, 24000), overwrite=True)
        eng.render(_req("chatterbox:me", clips), limit=False)
        assert len(model.prepared) == 2
    finally:
        eng.shutdown()


def qwen_sr():
    from test_lab import SR

    return SR


def test_chatterbox_needs_its_companion_repo():
    eng = MultiEngine(extras_missing={"mlx-community/S3TokenizerV2"})
    try:
        with pytest.raises(qwen.ModelNotDownloaded, match="S3TokenizerV2.*models pull chatterbox"):
            eng.check_ready("chatterbox")
        assert eng.status()["models"]["chatterbox"]["downloaded"] is False
        assert eng.status()["models"]["chatterbox"]["label"] == "Chatterbox Multilingual"
        eng.check_ready("kokoro")
    finally:
        eng.shutdown()


def test_kokoro_does_not_count_against_keep_loaded():
    eng = MultiEngine(keep_loaded=1)
    try:
        eng._submit(eng._ensure, "custom_voice", limit=False).result(5)
        eng._submit(eng._ensure, "kokoro", limit=False).result(5)
        assert set(eng._models) == {"custom_voice", "kokoro"}
        eng._submit(eng._ensure, "base", limit=False).result(5)
        assert set(eng._models) == {"kokoro", "base"}
        eng.configure(eng.repos, keep_loaded=1)
        assert set(eng.status()["models"]) == set(qwen.MODEL_KINDS)
    finally:
        eng.shutdown()


def test_snapshot_files_per_kind(tmp_path):
    (tmp_path / "config.json").write_text("{}")
    assert not qwen.snapshot_complete(tmp_path, qwen.required_files("kokoro"))
    (tmp_path / "voices").mkdir()
    (tmp_path / "voices" / "af_heart.safetensors").write_bytes(b"x")
    (tmp_path / "kokoro-v1_0.safetensors").write_bytes(b"x")
    assert qwen.snapshot_complete(tmp_path, qwen.required_files("kokoro"))
    assert not qwen.snapshot_complete(tmp_path, qwen.required_files("chatterbox"))
    assert qwen.required_files("custom_voice") == qwen.REQUIRED_FILES


def test_stream_resampler_matches_whole_resample():
    x = np.sin(np.arange(22050) / 22050 * 2 * np.pi * 330).astype(np.float32)
    r = StreamResampler(22050)
    y = np.concatenate([r.feed(x[i:i + 777]) for i in range(0, x.size, 777)])
    z = resample(x, 22050)
    n = min(y.size, z.size)
    assert abs(y.size - z.size) <= 1 and np.abs(y[:n] - z[:n]).max() < 1e-5
    same = StreamResampler(SAMPLE_RATE)
    assert same.feed(x[:100]) is not None and same.feed(x[:100]).size == 100


# --------------------------------------------------------------------------- #
# Apple voices (stub helper)                                                  #
# --------------------------------------------------------------------------- #

STUB = r'''
import json, os, struct, sys, time
args = sys.argv[1:]
log = os.environ["STUB_LOG"]
if args == ["--list"]:
    print(json.dumps([
        {"id": "com.apple.voice.premium.en-US.Ava", "name": "Ava", "lang": "en-US", "quality": "premium",
         "novelty": False, "personal": False},
        {"id": "com.apple.voice.compact.zh-CN.Tingting", "name": "Tingting", "lang": "zh-CN",
         "quality": "default", "novelty": False, "personal": False},
        {"id": "personal", "name": "Me", "lang": "en-US", "quality": "default", "novelty": False, "personal": True},
    ]))
    sys.exit(0)
text = sys.stdin.read()
with open(log, "a") as f:
    f.write(json.dumps({"args": args, "text": text}) + "\n")
if "--voice" in args and args[args.index("--voice") + 1] == "Nope":
    sys.stderr.write("no voice named Nope\n"); sys.exit(2)
mode = os.environ.get("STUB_MODE", "ok")
if mode == "slow":
    time.sleep(30)
if mode == "noisy":
    sys.stderr.write("diagnostic line\n" * 20000); sys.stderr.flush()  # ~300 KB: more than a pipe holds
if mode == "partial":
    sys.stdout.buffer.write(b"AWPCM1" + struct.pack("<I", 22050) + struct.pack("<2205f", *([0.1] * 2205)))
    sys.stdout.buffer.flush()
    sys.stderr.write("helper crashed\n"); sys.exit(1)
out = sys.stdout.buffer
out.write(b"AWPCM1" + struct.pack("<I", 22050)); out.flush()
for _ in range(5):
    out.write(struct.pack("<2205f", *([0.1] * 2205))); out.flush()
'''


@pytest.fixture
def apple_stub(tmp_path, monkeypatch):
    script = tmp_path / "stub_avspeech.py"
    script.write_text(STUB)
    log = tmp_path / "stub.log"
    monkeypatch.setenv("AFTERWORD_APPLE_HELPER", f"{sys.executable} {script}")
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.setattr(apple, "_voice_cache", None)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


def test_apple_stream_and_synth(apple_stub, tmp_path):
    profile = Profile(name="p", voice="apple:Ava (Premium)", lang="en", pace=1.3)
    got = []
    res = apple.AppleSynth().stream("Hello.", profile, {"lang": "zh"}, got.append, threading.Event())
    assert res.backend == "apple" and res.first_chunk_s is not None
    total = sum(c.size for c in got)
    assert abs(total - round(5 * 2205 * SAMPLE_RATE / 22050)) <= 2 and abs(res.audio_s - total / SAMPLE_RATE) < 1e-9
    assert apple_stub()[-1] == {"args": ["--voice", "Ava (Premium)", "--lang", "zh"], "text": "Hello."}
    out = tmp_path / "a.wav"
    sr = apple.AppleSynth().synth("Hi", profile, {}, out)
    with wave.open(str(out)) as w:
        assert w.getframerate() == 22050 and w.getnframes() == 5 * 2205
    assert sr.playback_rate == 1.3 and sr.backend == "apple" and sr.sample_rate == 22050
    # auto_voice (the fallback): no --voice, the line's language.
    apple.AppleSynth(auto_voice=True).synth("Hi", profile, {}, out)
    assert apple_stub()[-1]["args"] == ["--lang", "en"]


def test_apple_errors_and_cancel(apple_stub, monkeypatch):
    with pytest.raises(voices.VoiceError, match="no voice named Nope"):
        apple.AppleSynth().stream("x", Profile(name="p", voice="apple:Nope"), {}, lambda c: None)
    monkeypatch.setenv("STUB_MODE", "slow")
    cancel = threading.Event()
    threading.Timer(0.3, cancel.set).start()
    t = time.perf_counter()
    with pytest.raises(qwen.RenderCancelled):
        apple.AppleSynth().stream("x", Profile(name="p", voice="apple:"), {}, lambda c: None, cancel)
    assert time.perf_counter() - t < 5


def test_apple_list_and_availability(apple_stub, monkeypatch):
    names = [v["name"] for v in apple.list_voices(refresh=True)]
    assert names == ["Ava", "Me", "Tingting"]  # sorted by language, then quality
    assert apple.available() == (True, None)
    monkeypatch.setenv("AFTERWORD_APPLE_HELPER", "none")
    ok, why = apple.available()
    assert not ok and "turned off" in why
    with pytest.raises(apple.AppleUnavailable):
        apple.helper_command()


def test_qwen_synth_routes_apple_voices(apple_stub):
    synth = QwenSynth(engine=None, clips=None)
    got = []
    res = synth.stream("Hi", Profile(name="p", voice="apple:"), {}, got.append, threading.Event())
    assert res.backend == "apple" and got


def test_real_helper_source_compiles_and_lists(tmp_path, monkeypatch):
    """The shipped Swift source builds and runs (skipped without swiftc)."""
    import shutil

    if not shutil.which("swiftc"):
        pytest.skip("no swiftc")
    monkeypatch.delenv("AFTERWORD_APPLE_HELPER", raising=False)
    monkeypatch.setenv("AFTERWORD_HOME", str(tmp_path))
    monkeypatch.setattr(apple, "_build_error", None)
    monkeypatch.setattr(apple, "_voice_cache", None)
    cmd = apple.helper_command()
    assert Path(cmd[0]).parent == tmp_path / "bin"
    installed = {v["lang"] for v in apple.list_voices(refresh=True)}
    # A script subtag matches voices that report only language-region.
    import subprocess

    for tag, need in [("zh-Hant-TW", "zh-TW"), ("zh-Hans-CN", "zh-CN"), ("zh-Hant", "zh-TW")]:
        if need in installed:
            proc = subprocess.run(cmd + ["--lang", tag], input="测试".encode(), capture_output=True, timeout=60)
            assert proc.returncode == 0 and proc.stdout[:6] == apple.MAGIC, (tag, proc.stderr)


# --------------------------------------------------------------------------- #
# Fallback when a model voice fails                                           #
# --------------------------------------------------------------------------- #

KOKORO_CONFIG = """
[daemon]
model_fallback = "%s"

[profiles.k]
voice = "kokoro:af_heart"
lang = "en"
"""


class BoomSynth:
    name = "qwen"

    def stream(self, *a, **k):
        raise RuntimeError("model missing")

    def synth(self, *a, **k):
        raise RuntimeError("model missing")


def _fallback_daemon(home, monkeypatch, mode):
    (home / "config.toml").write_text(KOKORO_CONFIG % mode)
    d = make_daemon(home, monkeypatch, BoomSynth())
    used = []

    class FakeSay:
        def stream(self, text, profile, attrs, sink, cancel):
            used.append("say")
            sink(np.zeros(2400, np.float32))
            return StreamResult(synth_s=0.01, audio_s=0.1, backend="say")

    monkeypatch.setattr(server, "SaySynth", FakeSay)
    return d, used


def _speak(d):
    async def go():
        return (await drain(d, [d.dispatch(hook_msg([{"text": "All done."}], profile="k"), time.time())]))[0]

    return run(go())


def test_failed_model_voice_falls_back_to_apple(aw_home, monkeypatch, apple_stub):
    d, used = _fallback_daemon(aw_home, monkeypatch, "apple")
    ev = _speak(d)
    assert ev["status"] == "spoken", ev
    assert "kokoro failed (RuntimeError: model missing); spoke with apple" in json.dumps(ev)
    assert used == [] and apple_stub()[-1]["args"] == ["--lang", "en"]


def test_apple_fallback_unavailable_uses_say(aw_home, monkeypatch):
    d, used = _fallback_daemon(aw_home, monkeypatch, "apple")  # conftest: AFTERWORD_APPLE_HELPER=none
    ev = _speak(d)
    assert ev["status"] == "spoken" and used == ["say"]
    assert "spoke with say" in json.dumps(ev)


def test_model_fallback_none_fails_the_line(aw_home, monkeypatch, apple_stub):
    d, used = _fallback_daemon(aw_home, monkeypatch, "none")
    ev = _speak(d)
    assert ev["status"] == "error" and used == [] and apple_stub() == []


# --------------------------------------------------------------------------- #
# Config and the lab                                                          #
# --------------------------------------------------------------------------- #


def test_config_models_section_and_model_fallback():
    cfg, errors = build_config({"models": {"kokoro": "me/kokoro"}, "daemon": {"model_fallback": "say"}})
    assert errors == []
    assert cfg.qwen.repos()["kokoro"] == "me/kokoro"
    assert cfg.qwen.repos()["chatterbox"] == qwen.DEFAULT_REPOS["chatterbox"]
    assert cfg.daemon.model_fallback == "say"
    cfg, errors = build_config({"daemon": {"model_fallback": "loud"}})
    assert cfg.daemon.model_fallback == "apple" and any("model_fallback" in e for e in errors)
    _, errors = build_config({"profiles": {"x": {"voice": "kokoro:nope"}}})
    assert any("not a Kokoro voice" in e for e in errors)


def test_default_config_documents_the_new_engines(aw_home):
    store = ConfigStore()
    store.ensure_default_file()
    text = (aw_home / "config.toml").read_text()
    assert "[models]" in text and "model_fallback" in text
    cfg = store.get()
    assert cfg.qwen.repos() == {k: qwen.DEFAULT_REPOS[k] for k in qwen.MODEL_KINDS}


def test_lab_catalog_lists_new_voices(make_lab_engine, apple_stub, tmp_path):
    lab = make_lab_engine()
    cat = lab.catalog()
    assert [v["name"] for v in cat["apple_voices"]] == ["Ava", "Tingting"]  # no Personal Voice
    assert cat["apple"] == {"available": True, "error": None}
    assert "zh" in cat["kokoro_langs"] and "ko" in cat["chatterbox_langs"]
    assert cat["kokoro_voices"] == []  # the fake snapshot has no voices folder
    assert set(cat["engine"]["models"]) == set(qwen.MODEL_KINDS)


def test_lab_previews_apple_and_kokoro(make_lab_engine, apple_stub):
    lab = make_lab_engine()
    res = run(lab.preview({"text": "Hello there.", "voice": "apple:Ava", "lang": "en"}))
    assert res["backend"] == "apple" and res["seed"] is None and res["audio_s"] > 0
    res = run(lab.preview({"text": "Hello there.", "voice": "kokoro:af_heart"}))
    assert res["backend"] == "kokoro" and res["model"] == "kokoro" and res["seed"] is None
    from afterword.daemon.lab import LabError

    with pytest.raises(LabError, match="Kokoro has no ko"):
        run(lab.preview({"text": "Hello", "voice": "kokoro:af_heart", "lang": "ko"}))
    with pytest.raises(LabError, match="no voice named Nope"):
        run(lab.preview({"text": "Hello", "voice": "apple:Nope"}))


def test_explorer_refuses_unseeded_voices(make_lab_engine):
    from afterword.daemon.lab import LabError

    lab = make_lab_engine()
    for voice in ["kokoro:af_heart", "apple:", "say:Daniel"]:
        with pytest.raises(LabError, match="no seed to explore"):
            run(lab.explorer.start({"settings": {"text": "Hi", "voice": voice}}))


@pytest.fixture
def make_lab_engine(aw_home):
    from afterword.daemon.lab import Lab

    def make():
        store = ConfigStore()
        store.ensure_default_file()
        return Lab(aw_home, store, MultiEngine(), voices.ClipStore(aw_home / "voices"))

    return make


# --------------------------------------------------------------------------- #
# Review fixes                                                                #
# --------------------------------------------------------------------------- #


def test_lang_tag_keeps_the_region():
    assert voices.lang_tag("zh_tw") == "zh-TW" and voices.lang_tag("en-au") == "en-AU"
    assert voices.lang_tag("zh-hant-tw") == "zh-Hant-TW" and voices.lang_tag("es-419") == "es-419"
    assert voices.lang_tag("Chinese") == "zh" and voices.lang_tag("auto") is None and voices.lang_tag("") is None
    assert voices.lang_tag("x1") is None


def test_apple_passes_the_regional_tag(apple_stub):
    apple.AppleSynth().stream("Hi", Profile(name="p", voice="apple:", lang="en"), {"lang": "zh-tw"},
                              lambda c: None, threading.Event())
    assert apple_stub()[-1]["args"] == ["--lang", "zh-TW"]
    apple.AppleSynth().stream("Hi", Profile(name="p", voice="apple:", lang="en-AU"), {}, lambda c: None)
    assert apple_stub()[-1]["args"] == ["--lang", "en-AU"]


def test_every_engine_language_is_a_profile_language():
    from afterword.daemon.config import lang_code
    from afterword.daemon.lab import _langs_body
    from afterword.hooks import afterword_hook

    assert afterword_hook._LANG_NAMES == voices.LANGUAGE_NAMES  # the hook's copy
    assert set(voices.ISO_TO_QWEN) <= set(voices.LANGUAGE_NAMES)
    assert set(voices.CHATTERBOX_LANGS) <= set(voices.LANGUAGE_NAMES)
    assert {k.split("-")[0] for k in voices.KOKORO_LANGS} <= set(voices.LANGUAGE_NAMES)
    assert lang_code("hi") == "hi" and lang_code("Hindi") == "hi" and lang_code("ar-EG") == "ar"
    cfg, errors = build_config({"profiles": {"mix": {"voice": "kokoro:af_heart", "lang": "en",
                                                     "langs": {"hi": {"voice": "kokoro:hf_alpha"}}}}})
    assert errors == []
    assert cfg.voice_for("mix", "hi").voice == "kokoro:hf_alpha"
    assert _langs_body({"hindi": {"voice": "kokoro:hf_alpha"}}) == {"hi": {"voice": "kokoro:hf_alpha"}}
    assert afterword_hook._lang_code("arabic") == "ar"


def test_lab_lists_every_language(make_lab_engine):
    cat = make_lab_engine().catalog()
    assert "hindi" in cat["languages"] and "swahili" in cat["languages"]
    assert cat["lang_codes"]["hi"] == "hindi" and cat["lang_codes"]["zh"] == "chinese"
    assert len(cat["languages"]) == len(set(cat["languages"]))


def test_apple_preview_drops_a_supplied_seed(make_lab_engine, apple_stub):
    res = run(make_lab_engine().preview({"text": "Hello there.", "voice": "apple:", "seed": 42}))
    assert res["seed"] is None and res["settings"]["seed"] is None


def test_a_fallback_heard_then_failing_reports_its_own_error(aw_home, monkeypatch, apple_stub):
    monkeypatch.setenv("STUB_MODE", "partial")
    d, used = _fallback_daemon(aw_home, monkeypatch, "apple")
    ev = _speak(d)
    assert ev["status"] == "error", ev
    assert "helper crashed" in (ev.get("error") or "") and used == []  # not the model's error, and no say on top


def test_no_mlx_audio_keeps_the_voice_router(aw_home, monkeypatch, apple_stub):
    d, used = _fallback_daemon(aw_home, monkeypatch, "none")
    monkeypatch.setattr(server, "mlx_audio_available", lambda: (False, "mlx-audio is not installed"))
    monkeypatch.delenv("AFTERWORD_SYNTH", raising=False)
    assert d._make_synth("qwen").name == "qwen"  # apple: and say: still routed, models fall back


def test_language_names_reach_the_models(tmp_path):
    assert voices.lang_iso("hindi") == "hi" and voices.lang_iso("Arabic") == "ar"
    clips = voices.ClipStore(tmp_path)
    clips.add("me", voices.silence_wav(2.0, 24000))
    req = qwen.build_request("hi", voices.parse_voice("chatterbox:me"), clips, lang="arabic", style=None,
                             mood=None, ref_text=None, seed=None, temperature=None)
    assert req.lang == "ar" and voices.chatterbox_lang(req.lang) == "ar"
    assert voices.kokoro_lang("af_heart", "hindi") == "h"


def test_split_by_duration_bounds_every_piece():
    long_en = "This is a sentence that goes on for a while. " * 40
    pieces = qwen.split_by_duration(long_en, 20.0)
    assert len(pieces) > 1 and all(qwen.speech_seconds(x) <= 20.0 for x in pieces)
    assert " ".join(pieces) == long_en.strip()
    zh = "重构已经完成了，所有测试现在都通过了。" * 12
    pieces = qwen.split_by_duration(zh, 20.0)
    assert "".join(pieces) == zh and all(qwen.speech_seconds(x) <= 20.0 for x in pieces)
    assert all(len(x) <= 80 for x in qwen.split_by_duration("字" * 500, 20.0))  # no punctuation at all
    assert qwen.split_by_duration("Short.", 20.0) == ["Short."] and qwen.split_by_duration("  ", 20.0) == []


def test_long_chatterbox_text_renders_in_bounded_pieces(tmp_path):
    clips = voices.ClipStore(tmp_path)
    clips.add("me", voices.silence_wav(2.0, 24000))
    eng = MultiEngine()
    try:
        text = "The build is green and the docs are updated. " * 60
        chunks = []
        res = eng.render(_req("chatterbox:me", clips, text=text), limit=False,
                         on_chunk=lambda a, r: chunks.append(a.size), stream=True)
        calls = [kw for kind, kw in eng.models_made["chatterbox"].calls]
        assert len(calls) > 1 and len(chunks) == len(calls)
        # Each piece's expected tokens are at most half its budget (and the budget the model's cap).
        for kw in calls:
            expected = qwen.CHATTERBOX_TOKENS_PER_SECOND * qwen.speech_seconds(kw["text"])
            assert kw["max_new_tokens"] <= qwen.CHATTERBOX_MAX_TOKENS and expected * 2 <= kw["max_new_tokens"]
        assert " ".join(kw["text"] for kw in calls).split() == text.split()
        assert res.audio.size == sum(chunks)
        assert len(eng.models_made["chatterbox"].prepared) == 1  # one conditioning for every piece
    finally:
        eng.shutdown()


def test_split_keeps_numbers_urls_and_spacing():
    line = "The release is version 3.14 and costs $12.50, with a URL of example.com. "
    pieces = qwen.split_by_duration(line * 6, 20.0)
    assert len(pieces) > 1
    joined = " ".join(pieces)
    assert joined == (line * 6).strip()  # nothing inserted, nothing lost
    for bad in ("3. 14", "$12. 50", "example. com"):
        assert bad not in joined
    assert all(p.endswith("com.") for p in pieces)  # cut only at sentence ends
    mixed = "第一句。第二句！" * 30
    assert "".join(qwen.split_by_duration(mixed, 5.0)) == mixed


def test_a_noisy_helper_never_blocks_on_stderr(apple_stub, monkeypatch):
    monkeypatch.setenv("STUB_MODE", "noisy")
    t = time.perf_counter()
    got = []
    res = apple.AppleSynth().stream("Hi", Profile(name="p", voice="apple:"), {}, got.append, threading.Event())
    assert res.audio_s > 0 and got and time.perf_counter() - t < 10


def test_startup_warms_light_models_beyond_keep_loaded(aw_home, monkeypatch):
    (aw_home / "config.toml").write_text(
        '[profiles.k]\nvoice = "kokoro:af_heart"\n\n[profiles.q]\nvoice = "builtin:Ryan"\n\n'
        '[profiles.c]\nvoice = "clone:me"\n')
    d = make_daemon(aw_home, monkeypatch, BoomSynth())
    assert d.models_in_use() == ["kokoro", "custom_voice", "base"]
    assert d.models_to_warm(1) == ["kokoro", "custom_voice"]
    assert d.models_to_warm(2) == ["kokoro", "custom_voice", "base"]
