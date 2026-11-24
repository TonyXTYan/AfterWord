"""Qwen backend and voice lab. A fake mlx model stands in for Qwen3-TTS so
these run without the 3 GB models; tests marked `live` use the real ones
(set AFTERWORD_LIVE_QWEN=1)."""

import asyncio
import io
import json
import os
import stat
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from afterword.daemon import lab as labmod
from afterword.daemon import qwen, voices
from afterword.daemon.config import ConfigStore, Profile, build_config
from afterword.daemon.lab import Lab, LabError, delete_profile, write_profile

SR = 24000


# --------------------------------------------------------------------------- #
# A fake Qwen3-TTS model                                                       #
# --------------------------------------------------------------------------- #


class _Result:
    def __init__(self, seconds):
        self.audio = np.zeros(int(SR * seconds), dtype=np.float32) + 0.01


class FakeModel:
    sample_rate = SR

    def __init__(self, kind):
        self.kind = kind
        self.calls = []

    def get_supported_speakers(self):
        return ["serena", "ryan", "aiden"] if self.kind == "custom_voice" else []

    def generate_custom_voice(self, **kw):
        self.calls.append(("custom_voice", kw))
        yield _Result(0.5)

    def generate_voice_design(self, **kw):
        self.calls.append(("voice_design", kw))
        yield _Result(1.5)

    def generate(self, **kw):
        self.calls.append(("base", kw))
        yield _Result(0.3)
        yield _Result(0.3)


class FakeEngine(qwen.QwenEngine):
    """The real QwenEngine (thread, queueing, LRU, warm-up bookkeeping,
    render routing) with only the mlx boundary faked: which models are on
    disk, and what loading one returns."""

    def __init__(self, downloaded=("custom_voice", "base", "voice_design"), fail_load=(), **kw):
        self.downloaded = set(downloaded)
        self.fail_load = set(fail_load)
        self.loads = []
        self.models_made = {}
        super().__init__(**kw)

    def _available(self):
        return True, None

    def _snapshot(self, repo):
        kind = next((k for k, r in self.repos.items() if r == repo), None)
        return Path("/fake") / repo if kind in self.downloaded else None

    def _load_model(self, kind, path):
        if kind in self.fail_load:
            raise ValueError("Speech tokenizer not loaded")
        model = FakeModel(kind)
        self.models_made[kind] = model
        self.loads.append((kind, str(path)))
        return model


@pytest.fixture
def home(aw_home):
    return aw_home


@pytest.fixture
def make_lab(home):
    def make(engine=None, config_text=None):
        if config_text is not None:
            (home / "config.toml").write_text(config_text)
        store = ConfigStore()
        store.ensure_default_file()
        return Lab(home, store, engine or FakeEngine(), voices.ClipStore(home / "voices"))

    return make


def wav_bytes(seconds, rate=SR, channels=1):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x01" * int(seconds * rate) * channels)
    return buf.getvalue()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Voice specs and languages                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spec,kind,value,model",
    [
        ("builtin:Ryan", "builtin", "Ryan", "custom_voice"),
        ("clone:me", "clone", "me", "base"),
        ("clone:~/voices/me.wav", "clone", "~/voices/me.wav", "base"),
        ("design: A warm voice ", "design", "A warm voice", "voice_design"),
        ("say:Samantha", "say", "Samantha", None),
        ("say:", "say", "", None),
        ("BUILTIN:Aiden", "builtin", "Aiden", "custom_voice"),
    ],
)
def test_parse_voice(spec, kind, value, model):
    v = voices.parse_voice(spec)
    assert (v.kind, v.value, v.model) == (kind, value, model)


@pytest.mark.parametrize("spec", ["", None, "Ryan", "builtin:", "clone:", "robot:x", "design:" + "x" * 1001])
def test_parse_voice_rejects(spec):
    with pytest.raises(voices.VoiceError):
        voices.parse_voice(spec)


@pytest.mark.parametrize(
    "lang,name",
    [("en", "english"), ("EN_us", "english"), ("ja", "japanese"), ("zh-Hans", "chinese"), ("japanese", "japanese"),
     ("auto", "auto"), (None, "auto"), ("xx", "auto"), ("", "auto")],
)
def test_language_name(lang, name):
    assert voices.language_name(lang) == name


def test_compose_instruct():
    assert voices.compose_instruct("calm, slightly wry", None, "  dry ") == "calm, slightly wry. dry."
    assert voices.compose_instruct(None, "") is None


def test_speakers_from_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"talker_config": {
        "spk_id": {"ryan": 1, "uncle_fu": 2, "eric": 3},
        "spk_is_dialect": {"ryan": False, "eric": "sichuan_dialect"},
        "codec_language_id": {"english": 1, "chinese": 2, "sichuan_dialect": 3},
    }}))
    assert voices.speakers_from_config(cfg) == [
        {"name": "Ryan", "native": "english", "dialect": None},
        {"name": "Uncle_Fu", "native": "chinese", "dialect": None},
        {"name": "Eric", "native": "chinese", "dialect": "sichuan"},
    ]
    assert voices.languages_from_config(cfg) == ["english", "chinese"]


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #


def test_config_profile_fields_and_qwen_section():
    cfg, warnings = build_config({
        "qwen": {"keep_loaded": 9, "warm_on_start": "yes", "base": "me/base"},
        "profiles": {
            "a": {"voice": "builtin:Ryan", "seed": 5, "temperature": 0.7},
            "b": {"voice": "Ryan", "seed": -1, "temperature": 9},
        },
    })
    assert cfg.qwen.keep_loaded == 2 and cfg.qwen.warm_on_start is True and cfg.qwen.base == "me/base"
    assert cfg.profiles["a"].seed == 5 and cfg.profiles["a"].temperature == 0.7
    assert cfg.profiles["b"].seed is None and cfg.profiles["b"].temperature is None
    joined = " ".join(warnings)
    assert "qwen.keep_loaded" in joined and "warm_on_start" in joined
    assert "profiles.b.voice" in joined and "profiles.b.seed" in joined


def test_unknown_profile_borrows_first_profile():
    cfg, _ = build_config({"profiles": {"nova": {"voice": "builtin:Serena", "style": "calm"}}})
    p = cfg.profile("nope")
    assert p.voice == "builtin:Serena" and p.name == "nope" and p.known is False
    empty, _ = build_config({})
    assert empty.profile("x").voice == "builtin:Ryan"


def test_default_config_is_valid():
    import tomllib

    from afterword.daemon.config import DEFAULT_CONFIG_TOML

    cfg, warnings = build_config(tomllib.loads(DEFAULT_CONFIG_TOML))
    assert warnings == []
    assert cfg.daemon.synth == "qwen"
    for p in cfg.profiles.values():
        assert voices.parse_voice(p.voice).kind == "builtin"


# --------------------------------------------------------------------------- #
# Engine                                                                       #
# --------------------------------------------------------------------------- #


def test_engine_routes_by_voice_kind(home):
    eng = FakeEngine()
    clips = voices.ClipStore(home / "voices")
    clips.add("me", wav_bytes(2), "my transcript")
    for spec, expected in (("builtin:Ryan", "custom_voice"), ("design:A warm voice", "voice_design"), ("clone:me", "base")):
        req = qwen.build_request("Hello.", voices.parse_voice(spec), clips, lang="ja", style="calm",
                                 mood="dry", ref_text=None, seed=11, temperature=0.6)
        res = eng.render(req)
        assert res.model == expected and res.seed == 11 and res.sample_rate == SR
        call_kind, kw = eng.models_made[expected].calls[-1]
        assert call_kind == expected
        if expected == "custom_voice":
            assert kw["speaker"] == "ryan" and kw["instruct"] == "calm. dry." and kw["language"] == "japanese"
        if expected == "voice_design":
            assert kw["instruct"] == "A warm voice. calm. dry."
        if expected == "base":
            assert kw["ref_text"] == "my transcript" and kw["ref_audio"].endswith("me.wav")
            assert res.audio.size == int(SR * 0.3) * 2  # chunks concatenated
    eng.shutdown()


def test_engine_unknown_speaker_and_missing_model(home):
    eng = FakeEngine(downloaded=("custom_voice",))
    clips = voices.ClipStore(home / "voices")
    with pytest.raises(voices.VoiceError, match="unknown speaker 'Chelsie'"):
        eng.render(qwen.build_request("x", voices.parse_voice("builtin:Chelsie"), clips, lang="en",
                                      style=None, mood=None, ref_text=None, seed=None, temperature=None))
    with pytest.raises(qwen.ModelNotDownloaded, match="afterword models pull"):
        eng.render(qwen.build_request("x", voices.parse_voice("design:x"), clips, lang="en",
                                      style=None, mood=None, ref_text=None, seed=None, temperature=None))
    eng.shutdown()


def test_engine_lru_keeps_at_most_n_models(home):
    eng = FakeEngine(keep_loaded=2)
    for kind in ("custom_voice", "voice_design", "custom_voice", "base"):
        eng._submit(eng._ensure, kind).result()
    assert list(eng._models) == ["custom_voice", "base"]
    assert [k for k, _ in eng.loads] == ["custom_voice", "voice_design", "base"]
    status = eng.status()["models"]
    assert status["base"]["loaded"] and not status["voice_design"]["loaded"]
    assert status["voice_design"]["load_ms"] is not None  # remembered after unload
    eng.shutdown(wait=2)


def test_engine_configure_trims_and_reloads_changed_repo(home):
    eng = FakeEngine(keep_loaded=3)
    for kind in ("custom_voice", "voice_design", "base"):
        eng._submit(eng._ensure, kind).result()
    eng.configure({"custom_voice": eng.repos["custom_voice"]}, keep_loaded=1)
    eng._submit(lambda: None).result()  # wait for the trim
    assert list(eng._models) == ["base"]
    eng.configure({"base": "someone/other-base"}, keep_loaded=1)
    eng._submit(lambda: None).result()
    assert eng._models == {}  # the old repo's weights are gone
    eng.downloaded.add("base")
    eng._submit(eng._ensure, "base").result()
    assert eng._models["base"].repo == "someone/other-base"
    assert eng.loads[-1] == ("base", "/fake/someone/other-base")
    eng.shutdown(wait=2)


def test_engine_failed_warmup_fails_the_load(home):
    eng = FakeEngine(fail_load=("custom_voice",))
    with pytest.raises(ValueError, match="Speech tokenizer"):
        eng._submit(eng._ensure, "custom_voice").result()
    st = eng.status()["models"]["custom_voice"]
    assert not st["loaded"] and "Speech tokenizer" in st["error"]
    eng.shutdown(wait=2)


def test_engine_shutdown_cancels_queue_and_never_blocks_exit(home):
    import threading

    eng = FakeEngine()
    gate = threading.Event()
    started = threading.Event()
    running = eng._submit(lambda: (started.set(), gate.wait(30)), limit=False)
    assert started.wait(5)
    queued = eng._submit(lambda: "never")
    t = time.time()
    eng.shutdown()
    assert time.time() - t < 0.5
    assert queued.cancelled()
    assert eng._thread.daemon  # a stuck render cannot keep the process alive
    with pytest.raises(qwen.EngineStopped):
        eng._submit(lambda: None)
    gate.set()
    running.result(5)


def test_render_is_capped_by_text_length(home):
    eng = FakeEngine()
    clips = voices.ClipStore(home / "voices")
    req = qwen.build_request("Short.", voices.parse_voice("builtin:Ryan"), clips, lang="en",
                             style=None, mood=None, ref_text=None, seed=1, temperature=None)
    eng.render(req)
    _, kw = eng.models_made["custom_voice"].calls[-1]
    assert kw["max_tokens"] == qwen.max_tokens_for("Short.") < 200
    assert qwen.max_tokens_for("x" * 1000) < 4000
    eng.shutdown(wait=2)


def test_snapshot_complete(tmp_path):
    for rel in qwen.REQUIRED_FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("{}")
    assert not qwen.snapshot_complete(tmp_path)  # no weights yet
    (tmp_path / "model.safetensors").write_text("w")
    assert qwen.snapshot_complete(tmp_path)
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {"a": "model-2.safetensors"}}))
    assert not qwen.snapshot_complete(tmp_path)  # a listed shard is missing
    (tmp_path / "model-2.safetensors").write_text("w")
    assert qwen.snapshot_complete(tmp_path)
    (tmp_path / "speech_tokenizer" / "model.safetensors").unlink()
    assert not qwen.snapshot_complete(tmp_path)


def test_engine_refuses_when_too_many_pending(home):
    import threading

    eng = FakeEngine()
    gate = threading.Event()
    started = threading.Event()
    try:
        eng._submit(lambda: (started.set(), gate.wait(10)), limit=False)  # occupy the engine thread
        assert started.wait(5)
        futures = [eng._submit(lambda: None) for _ in range(qwen.MAX_PENDING)]
        with pytest.raises(qwen.EngineBusy):
            eng._submit(lambda: None)
        futures.append(eng._submit(lambda: None, limit=False))  # the playback queue is never refused
        assert eng.status()["pending"] == qwen.MAX_PENDING + 1 and eng.status()["busy"]
    finally:
        gate.set()
    for f in futures:
        f.result(5)
    eng.shutdown()


def test_real_engine_reports_missing_download(monkeypatch):
    monkeypatch.setattr(qwen, "local_snapshot", lambda repo: None)
    monkeypatch.setattr(qwen, "mlx_audio_available", lambda: (True, None))
    eng = qwen.QwenEngine()
    status = eng.status()
    assert all(not m["downloaded"] for m in status["models"].values())
    with pytest.raises(qwen.ModelNotDownloaded, match="models pull custom_voice"):
        eng._submit(eng._ensure, "custom_voice").result()
    eng.shutdown()


# --------------------------------------------------------------------------- #
# Clips                                                                        #
# --------------------------------------------------------------------------- #


def test_clip_store_roundtrip(home):
    clips = voices.ClipStore(home / "voices")
    info = clips.add("me", wav_bytes(3, rate=48000, channels=2), "  hello there  ")
    assert info["duration_s"] == 3.0 and info["ref_text"] == "hello there"
    with wave.open(str(home / "voices" / "me.wav")) as w:
        assert (w.getnchannels(), w.getframerate()) == (1, SR)
    assert [c["name"] for c in clips.list()] == ["me"]
    path, ref = clips.resolve("me")
    assert path.name == "me.wav" and ref == "hello there"
    with pytest.raises(FileExistsError):
        clips.add("me", wav_bytes(2))
    assert clips.delete("me") and not clips.delete("me")
    assert not (home / "voices" / "me.txt").exists()


@pytest.mark.parametrize(
    "name,data,match",
    [
        ("../x", None, "clip name"),
        ("ok", b"", "empty"),
        ("ok", b"not audio at all" * 10, "decode"),
        ("ok", "short", "too short"),
        ("ok", "long", "too long"),
    ],
)
def test_clip_store_rejects(home, name, data, match):
    clips = voices.ClipStore(home / "voices")
    if data == "short":
        data = wav_bytes(0.3)
    elif data == "long":
        data = wav_bytes(61)
    elif data is None:
        data = wav_bytes(2)
    with pytest.raises(voices.VoiceError, match=match):
        clips.add(name, data)


def test_clone_path_spec(home, tmp_path):
    clip = tmp_path / "ref.wav"
    clip.write_bytes(wav_bytes(2))
    clip.with_suffix(".txt").write_text("sidecar transcript")
    path, ref = voices.ClipStore(home / "voices").resolve(str(clip))
    assert path == clip and ref == "sidecar transcript"
    with pytest.raises(voices.VoiceError, match="not found"):
        voices.ClipStore(home / "voices").resolve(str(tmp_path / "missing.wav"))


# --------------------------------------------------------------------------- #
# Lab                                                                          #
# --------------------------------------------------------------------------- #


def test_catalog_shape(make_lab):
    lab = make_lab()
    cat = lab.catalog()
    assert set(cat) >= {"engine", "speakers", "languages", "lang_codes", "say_voices", "clips", "profiles",
                        "daemon_synth", "samples"}
    assert cat["languages"][0] == "auto"
    assert set(cat["engine"]["models"]) == set(qwen.MODEL_KINDS)
    assert cat["profiles"]["nova"]["voice"] == "builtin:Serena"
    assert cat["samples"]["en"] and cat["samples"]["ja"]


def test_preview_builtin_with_profile_and_overrides(make_lab):
    lab = make_lab()
    res = run(lab.preview({"text": "Hello **there**.", "profile": "nova", "mood": "dry", "seed": 42, "pace": 1.2}))
    assert res["backend"] == "qwen" and res["model"] == "custom_voice"
    assert res["text"] == "Hello there." and res["seed"] == 42 and res["pace"] == 1.2
    assert res["settings"]["voice"] == "builtin:Serena" and res["settings"]["mood"] == "dry"
    assert res["url"] == f"/api/lab/previews/{res['id']}.wav"
    assert lab.preview_path(res["id"]).is_file()
    assert res["audio_s"] == 0.5 and res["rtf"] is not None


def test_preview_seed_is_returned_when_random(make_lab):
    res = run(make_lab().preview({"text": "x", "voice": "builtin:Ryan"}))
    assert isinstance(res["seed"], int) and res["settings"]["seed"] == res["seed"]


@pytest.mark.parametrize(
    "body,status",
    [
        ({"text": "hi", "voice": "builtin:Ryan", "seed": 1e400}, 400),
        ({"text": "hi", "voice": "builtin:Ryan", "seed": 2.5}, 400),
        ({"text": "hi", "voice": "builtin:Ryan", "pace": "1e999"}, 400),
        ({"text": ""}, 400),
        ({"text": "x" * 1001, "voice": "builtin:Ryan"}, 400),
        ({"text": "hi"}, 400),  # no voice and no profile
        ({"text": "hi", "profile": "missing"}, 404),
        ({"text": "hi", "voice": "robot:x"}, 400),
        ({"text": "hi", "voice": "clone:nobody"}, 404),
        ({"text": "hi", "voice": "builtin:Ryan", "pace": 3}, 400),
        ({"text": "hi", "voice": "builtin:Ryan", "seed": "abc"}, 400),
        ({"text": "hi", "voice": "builtin:Ryan", "temperature": float("nan")}, 400),
        ({"text": "hi", "voice": "builtin:Nobody"}, 400),
        ([1, 2], 400),
    ],
)
def test_preview_errors(make_lab, body, status):
    with pytest.raises(LabError) as e:
        run(make_lab().preview(body))
    assert e.value.status == status, e.value.message


def test_preview_model_not_downloaded_is_503(make_lab):
    lab = make_lab(engine=FakeEngine(downloaded=()))
    with pytest.raises(LabError) as e:
        run(lab.preview({"text": "hi", "voice": "builtin:Ryan"}))
    assert e.value.status == 503 and "models pull" in e.value.message


def test_previews_are_pruned(make_lab, monkeypatch):
    monkeypatch.setattr(labmod, "PREVIEW_KEEP", 3)
    lab = make_lab()
    ids = [run(lab.preview({"text": f"n{i}", "voice": "builtin:Ryan"}))["id"] for i in range(5)]
    assert list(lab.previews) == ids[2:]
    assert not lab.preview_path(ids[0])
    assert len(list((lab.previews_dir).glob("*.wav"))) == 3


def test_freeze_design_preview_into_clip_then_clone(make_lab):
    lab = make_lab()
    res = run(lab.preview({"text": "A line long enough to clone from.", "voice": "design:A warm narrator"}))
    clip = lab.freeze({"preview_id": res["id"], "name": "narrator"})["clip"]
    assert clip["ref_text"] == "A line long enough to clone from."
    with pytest.raises(LabError) as e:
        lab.freeze({"preview_id": res["id"], "name": "narrator"})
    assert e.value.status == 409
    with pytest.raises(LabError) as e:
        lab.freeze({"preview_id": "nope", "name": "x"})
    assert e.value.status == 404
    out = run(lab.preview({"text": "Cloned.", "voice": "clone:narrator"}))
    assert out["model"] == "base"


def test_add_clip_requires_consent(make_lab):
    lab = make_lab()
    with pytest.raises(LabError) as e:
        run(lab.add_clip("me", wav_bytes(2), consent=False, ref_text="", overwrite=False))
    assert e.value.status == 400 and "consent" in e.value.message
    assert run(lab.add_clip("me", wav_bytes(2), True, "hi", False))["clip"]["name"] == "me"
    with pytest.raises(LabError) as e:
        run(lab.add_clip("me", wav_bytes(2), True, "", False))
    assert e.value.status == 409
    assert run(lab.add_clip("me", wav_bytes(3), True, "", True))["clip"]["duration_s"] == 3.0


# --------------------------------------------------------------------------- #
# Saving profiles to config.toml                                               #
# --------------------------------------------------------------------------- #


def test_save_profile_keeps_comments_and_reloads(make_lab, home):
    lab = make_lab()
    original = (home / "config.toml").read_text()
    out = lab.save_profile({"name": "deep", "voice": "design:A deep voice", "lang": "en", "pace": 0.9,
                            "seed": 7, "temperature": None, "style": ""})
    assert out["profile"]["seed"] == 7 and out["profile"]["pace"] == 0.9
    text = (home / "config.toml").read_text()
    for line in original.splitlines():
        if line.startswith("#"):
            assert line in text  # comments survive
    assert "temperature" not in text.split("[profiles.deep]")[1]
    assert "deep" in lab.store.get().profiles
    with pytest.raises(LabError) as e:
        lab.save_profile({"name": "deep", "voice": "builtin:Ryan"})
    assert e.value.status == 409
    lab.save_profile({"name": "deep", "voice": "builtin:Ryan", "overwrite": True})
    assert lab.store.get().profiles["deep"].voice == "builtin:Ryan"
    assert lab.store.get().profiles["deep"].seed is None  # cleared fields are removed
    assert lab.delete_profile("deep") == {"ok": True}
    with pytest.raises(LabError) as e:
        lab.delete_profile("deep")
    assert e.value.status == 404


@pytest.mark.parametrize(
    "body",
    [
        {"name": "bad name", "voice": "builtin:Ryan"},
        {"name": "x\n", "voice": "builtin:Ryan"},
        {"name": "x", "voice": ""},
        {"name": "x", "voice": "nope"},
        {"name": "x", "voice": "builtin:Ryan", "pace": 9},
        {"name": "x", "voice": "builtin:Ryan", "max_chars": 5},
        {"name": "x", "voice": "builtin:Ryan", "seed": 1.7},
        {"name": "x", "voice": "builtin:Ryan", "seed": 1e400},
        {"name": "x", "voice": "builtin:Ryan", "seed": "inf"},
        {"name": "x", "voice": "builtin:Ryan", "style": "s" * 301},
    ],
)
def test_save_profile_rejects(make_lab, home, body):
    lab = make_lab()
    before = (home / "config.toml").read_text()
    with pytest.raises(LabError) as e:
        lab.save_profile(body)
    assert e.value.status == 400, e.value.message
    assert (home / "config.toml").read_text() == before


def test_save_profile_stores_toml_syntax_as_data(make_lab):
    lab = make_lab()
    voice = 'design:a "quoted"\n[daemon]\nport = 1'
    lab.save_profile({"name": "x", "voice": voice})
    cfg = lab.store.get()
    assert cfg.daemon.port == 47800
    assert cfg.profiles["x"].voice == voice


def test_save_profile_through_symlink_keeps_symlink(make_lab, home, tmp_path):
    lab = make_lab()
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text((home / "config.toml").read_text())
    (home / "config.toml").unlink()
    (home / "config.toml").symlink_to(real)
    lab.save_profile({"name": "linked", "voice": "builtin:Ryan"})
    assert (home / "config.toml").is_symlink()
    assert "[profiles.linked]" in real.read_text()


def test_write_profile_creates_file_with_private_mode(tmp_path):
    path = tmp_path / "config.toml"
    write_profile(path, "a", {"voice": "builtin:Ryan", "pace": 1.0}, overwrite=False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "[profiles.a]" in path.read_text()
    assert delete_profile(path, "a") and not delete_profile(path, "a")


# --------------------------------------------------------------------------- #
# Daemon integration: qwen failures fall back to say, pace reaches the player   #
# --------------------------------------------------------------------------- #


def _daemon(home, monkeypatch, engine):
    monkeypatch.delenv("AFTERWORD_SYNTH", raising=False)
    monkeypatch.setenv("AFTERWORD_PLAYER", "null")
    from afterword.daemon import server
    from afterword.daemon.log import EventLog

    monkeypatch.setattr(server, "QwenEngine", lambda *a, **k: engine)
    store = ConfigStore()
    store.ensure_default_file()
    return server.Daemon(home, store, EventLog(home / "events.db"))


def test_queue_uses_qwen_and_passes_pace_to_player(home, monkeypatch):
    async def main():
        d = _daemon(home, monkeypatch, FakeEngine())
        assert d.synth.name == "qwen"
        worker = asyncio.create_task(d.worker())
        ack = d.dispatch(json.dumps({"type": "test", "text": "hi", "profile": "ember"}).encode(), time.time())
        for _ in range(100):
            ev = d.events.get(ack["event_id"])
            if ev["status"] in ("spoken", "error", "skipped"):
                break
            await asyncio.sleep(0.02)
        worker.cancel()
        return ev, d.stream_player.last.rate, d.engine.models_made["custom_voice"].calls

    ev, rate, calls = run(main())
    assert ev["status"] == "spoken", ev
    assert ev["timings"]["synth_backend"] == "qwen" and ev["timings"]["model"] == "custom_voice"
    assert ev["timings"]["stream"] is True
    assert rate == 1.05 and ev["timings"]["playback_rate"] == 1.05
    # Live speech streams at a 1 s interval.
    kw = calls[-1][1]
    assert kw["stream"] is True and kw["streaming_interval"] == qwen.STREAMING_INTERVAL == 1.0


def test_queue_v1_path_passes_pace_to_afplay_player(home, monkeypatch):
    """[daemon] stream = false: the whole line is rendered, then played."""
    (home / "config.toml").write_text(
        '[daemon]\nstream = false\n[profiles.ember]\nvoice = "builtin:Aiden"\npace = 1.05\n'
    )

    async def main():
        d = _daemon(home, monkeypatch, FakeEngine())
        assert d.stream_player is None
        worker = asyncio.create_task(d.worker())
        ack = d.dispatch(json.dumps({"type": "test", "text": "hi", "profile": "ember"}).encode(), time.time())
        for _ in range(100):
            ev = d.events.get(ack["event_id"])
            if ev["status"] in ("spoken", "error", "skipped"):
                break
            await asyncio.sleep(0.02)
        worker.cancel()
        return ev, d.player.last_rate, d.engine.models_made["custom_voice"].calls

    ev, rate, calls = run(main())
    assert ev["status"] == "spoken", ev
    assert ev["timings"]["stream"] is False and ev["timings"]["playback_rate"] == 1.05
    assert rate == 1.05
    assert "stream" not in calls[-1][1]


def test_queue_falls_back_to_say_when_model_missing(home, monkeypatch):
    async def main():
        d = _daemon(home, monkeypatch, FakeEngine(downloaded=()))
        worker = asyncio.create_task(d.worker())
        ack = d.dispatch(json.dumps({"type": "test", "text": "still heard", "profile": "nova"}).encode(), time.time())
        for _ in range(300):
            ev = d.events.get(ack["event_id"])
            if ev["status"] in ("spoken", "error", "skipped"):
                break
            await asyncio.sleep(0.02)
        worker.cancel()
        return ev

    ev = run(main())
    assert ev["status"] == "spoken", ev
    assert ev["timings"]["synth_backend"] == "say"
    assert ev["reason"].startswith("qwen failed (ModelNotDownloaded")


def test_models_in_use(home, monkeypatch):
    (home / "config.toml").write_text(
        '[profiles.a]\nvoice = "clone:x"\n[profiles.b]\nvoice = "builtin:Ryan"\n'
        '[profiles.c]\nvoice = "clone:y"\n[profiles.d]\nvoice = "say:Alex"\n'
    )
    d = _daemon(home, monkeypatch, FakeEngine())
    assert d.models_in_use() == ["base", "custom_voice"]


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def test_lab_http_routes(home, monkeypatch):
    from fastapi.testclient import TestClient

    from afterword.daemon.web import build_app

    d = _daemon(home, monkeypatch, FakeEngine())
    client = TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800")
    H = {"X-AfterWord": "1"}
    assert client.get("/api/lab/catalog").json()["profiles"]["nova"]
    assert client.post("/api/lab/preview", json={"text": "hi", "voice": "builtin:Ryan"}).status_code == 403
    r = client.post("/api/lab/preview", json={"text": "hi", "voice": "builtin:Ryan"}, headers=H)
    assert r.status_code == 200, r.text
    wav = client.get(r.json()["url"])
    assert wav.status_code == 200 and wav.headers["content-type"] == "audio/wav"
    assert client.get("/api/lab/previews/../../etc/passwd").status_code == 404
    assert client.post("/api/lab/preview", content=b"{bad", headers=H).json()["error"].startswith("request body")
    assert client.post("/api/lab/clips?name=me", content=wav_bytes(2), headers=H).status_code == 400
    r = client.post("/api/lab/clips?name=me&consent=1&ref_text=hello%20there", content=wav_bytes(2), headers=H)
    assert r.status_code == 200 and r.json()["clip"]["ref_text"] == "hello there"
    assert client.get("/api/lab/clips/me").status_code == 200
    assert client.get("/api/lab/clips/..%2Fconfig").status_code in (400, 404)
    big = {"X-AfterWord": "1", "Content-Length": str(voices.CLIP_MAX_BYTES + 1)}
    assert client.post("/api/lab/clips?name=big&consent=1", content=b"x" * 10, headers=big).status_code in (400, 413)
    r = client.post("/api/lab/profiles", json={"name": "z", "voice": "clone:me"}, headers=H)
    assert r.status_code == 200, r.text
    assert client.delete("/api/lab/profiles/z", headers=H).json() == {"ok": True}
    assert client.delete("/api/lab/clips/me", headers=H).json() == {"ok": True}
    assert client.post("/api/lab/warm", json={"model": "nope"}, headers=H).status_code == 400
    assert client.post("/api/lab/warm", json={"model": "base"}, headers=H).json()["models"]["base"]["loaded"]


# --------------------------------------------------------------------------- #
# Live (real Qwen3-TTS)                                                        #
# --------------------------------------------------------------------------- #

live = pytest.mark.skipif(os.environ.get("AFTERWORD_LIVE_QWEN") != "1", reason="set AFTERWORD_LIVE_QWEN=1")


@live
def test_live_render_is_seeded_and_faster_than_real_time(home):
    eng = qwen.QwenEngine()
    clips = voices.ClipStore(home / "voices")
    req = qwen.build_request("The tests pass.", voices.parse_voice("builtin:Ryan"), clips, lang="en",
                             style="calm", mood=None, ref_text=None, seed=5, temperature=None)
    a = eng.render(req)
    b = eng.render(req)
    assert a.audio.shape == b.audio.shape and np.allclose(a.audio, b.audio)
    assert b.synth_s < b.audio.size / b.sample_rate
    eng.shutdown()


# --------------------------------------------------------------------------- #
# Regressions from the step 4 review                                           #
# --------------------------------------------------------------------------- #


def test_previews_from_an_earlier_run_are_cleared(make_lab, home):
    (home / "lab").mkdir()
    (home / "lab" / "pdeadbeef00.wav").write_bytes(b"old")
    (home / "lab" / "keep.txt").write_text("not a preview")
    make_lab()
    assert not (home / "lab" / "pdeadbeef00.wav").exists()
    assert (home / "lab" / "keep.txt").exists()


def test_style_and_mood_echo_null_when_unused(make_lab):
    lab = make_lab()
    lab.clips.add("me", wav_bytes(2), "hi")
    out = run(lab.preview({"text": "x", "voice": "clone:me", "style": "warm", "mood": "dry"}))
    assert out["settings"]["style"] is None and out["settings"]["mood"] is None
    out = run(lab.preview({"text": "x", "voice": "builtin:Ryan", "style": "warm", "mood": "dry"}))
    assert out["settings"]["style"] == "warm" and out["settings"]["mood"] == "dry"


def test_render_failure_is_json_500_and_leaves_no_file(make_lab):
    class Boom(FakeEngine):
        def _render(self, req):
            raise RuntimeError("kaboom")

    lab = make_lab(engine=Boom())
    with pytest.raises(LabError) as e:
        run(lab.preview({"text": "x", "voice": "builtin:Ryan"}))
    assert e.value.status == 500 and "kaboom" in e.value.message
    assert list(lab.previews_dir.glob("*.wav")) == []


def test_catalog_reports_active_synth(make_lab):
    lab = make_lab()
    lab.active_synth = lambda: "say"
    cat = lab.catalog()
    assert cat["daemon_synth"] == "say" and cat["configured_synth"] == "qwen"


def test_config_inf_and_overflow_are_warnings_not_crashes(home):
    cfg, warnings = build_config({"profiles": {"a": {"voice": "builtin:Ryan", "seed": float("inf"),
                                                     "pace": float("nan"), "max_chars": 3.5}}})
    p = cfg.profiles["a"]
    assert p.seed is None and p.pace == 1.0 and p.max_chars == 400
    assert len(warnings) == 3
    (home / "config.toml").write_text('[profiles.a]\nvoice = "builtin:Ryan"\n')
    store = ConfigStore()
    (home / "config.toml").write_text('[profiles.a]\nvoice = "builtin:Ryan"\nseed = inf\n')
    os.utime(home / "config.toml", (time.time() + 5, time.time() + 5))
    assert store.get().profiles["a"].seed is None and store.error is None
    assert any("seed" in w for w in store.warnings)


def test_reload_callback_reconfigures_engine(home, monkeypatch):
    d = _daemon(home, monkeypatch, FakeEngine(keep_loaded=2))
    cfg = home / "config.toml"
    cfg.write_text(cfg.read_text().replace("keep_loaded = 2", "keep_loaded = 1"))
    os.utime(cfg, (time.time() + 5, time.time() + 5))
    d.lab.catalog()  # any read of the config applies it
    assert d.engine.keep_loaded == 1


def test_concurrent_clip_writes_never_mix(home):
    import threading

    clips = voices.ClipStore(home / "voices")
    errors, oks = [], []

    def write(i):
        try:
            clips.add("same", wav_bytes(1 + i % 3), f"take {i}", overwrite=False)
            oks.append(i)
        except FileExistsError:
            pass
        except Exception as exc:  # pragma: no cover - the bug
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(oks) == 1
    info = clips.info("same")
    assert info["ref_text"] == f"take {oks[0]}" and info["duration_s"] == 1 + oks[0] % 3
    assert not list((home / "voices").glob(".*"))


def test_lab_http_errors_and_traversal(home, monkeypatch):
    import threading

    from fastapi.testclient import TestClient

    from afterword.daemon.web import build_app

    eng = FakeEngine(downloaded=("custom_voice",))
    d = _daemon(home, monkeypatch, eng)
    client = TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800", raise_server_exceptions=False)
    H = {"X-AfterWord": "1"}
    # Traversal never reaches a file: the router refuses encoded slashes, and
    # the handlers only serve ids/names they know.
    r = client.get("/api/lab/previews/..%2F..%2Fetc%2Fpasswd.wav")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    r = client.get("/api/lab/previews/..wav")
    assert r.status_code == 404 and r.json() == {"error": "preview not found"}
    assert d.lab.preview_path("../../etc/passwd") is None
    assert client.get("/api/lab/clips/..%2Fconfig").status_code == 404
    r = client.get("/api/lab/clips/.hidden")
    assert r.status_code == 400 and "clip name" in r.json()["error"]
    # 503: model not downloaded
    r = client.post("/api/lab/preview", json={"text": "hi", "voice": "design:x"}, headers=H)
    assert r.status_code == 503 and "models pull" in r.json()["error"]
    r = client.post("/api/lab/warm", json={"model": "base"}, headers=H)
    assert r.status_code == 503
    # freeze: 200, 409, 404, too short
    p = client.post("/api/lab/preview", json={"text": "hi", "voice": "builtin:Ryan"}, headers=H).json()
    r = client.post("/api/lab/freeze", json={"preview_id": p["id"], "name": "n"}, headers=H)
    assert r.status_code == 400 and "too short" in r.json()["error"]  # the fake render is 0.5 s
    assert client.post("/api/lab/freeze", json={"preview_id": "nope", "name": "n"}, headers=H).status_code == 404
    # 429: occupy the engine, fill the queue
    gate, started = threading.Event(), threading.Event()
    eng._submit(lambda: (started.set(), gate.wait(10)), limit=False)
    assert started.wait(5)
    held = [eng._submit(lambda: None) for _ in range(qwen.MAX_PENDING)]
    try:
        r = client.post("/api/lab/preview", json={"text": "hi", "voice": "builtin:Ryan"}, headers=H)
        assert r.status_code == 429 and "busy" in r.json()["error"]
    finally:
        gate.set()
    for f in held:
        f.result(5)
    # 413 on a streamed body without Content-Length
    def chunks():
        for _ in range(voices.CLIP_MAX_BYTES // 65536 + 2):
            yield b"x" * 65536

    r = client.post("/api/lab/clips?name=big&consent=1", content=chunks(), headers=H)
    assert r.status_code == 413
    # Unexpected failures are JSON too.
    monkeypatch.setattr(d.lab, "catalog", lambda: 1 / 0)
    r = client.get("/api/lab/catalog")
    assert r.status_code == 500 and "ZeroDivisionError" in r.json()["error"]


def test_profile_name_with_trailing_newline_is_rejected():
    from afterword.hooks.tags import PROFILE_NAME_RE, parse

    assert not PROFILE_NAME_RE.match("nova\n")
    assert parse('<voice set="nova\n"/>').voice.profile == "nova"  # attribute values are stripped


def test_shutdown_skips_say_fallback_for_failed_renders(home, monkeypatch):
    d = _daemon(home, monkeypatch, FakeEngine(downloaded=()))
    result, note = d._synthesize("x", d.store.get().profile("nova"), {}, home / "a.wav")
    assert result.backend == "say" and "qwen failed" in note  # normal: fall back
    d._stopping = True
    with pytest.raises(qwen.ModelNotDownloaded):
        d._synthesize("x", d.store.get().profile("nova"), {}, home / "b.wav")  # shutting down: no say


@live
def test_live_design_then_clone_with_one_model_loaded(home):
    """VoiceDesign render -> saved as a clip -> cloned with the Base model,
    with keep_loaded=1 so the engine has to swap models."""
    eng = qwen.QwenEngine(keep_loaded=1)
    clips = voices.ClipStore(home / "voices")
    text = "Every evening the keeper climbed the stairs and lit the lamp."
    design = eng.render(qwen.build_request(text, voices.parse_voice("design:A calm older narrator"), clips,
                                           lang="en", style=None, mood=None, ref_text=None, seed=3, temperature=None))
    out = home / "d.wav"
    voices.write_wav(out, design.audio, design.sample_rate)
    clips.add("narr", out.read_bytes(), text)
    clone = eng.render(qwen.build_request("Now in a cloned voice.", voices.parse_voice("clone:narr"), clips,
                                          lang="en", style=None, mood=None, ref_text=None, seed=1, temperature=None))
    assert clone.model == "base" and clone.audio.size > 0
    assert list(eng._models) == ["base"]
    assert clone.audio.size / clone.sample_rate < 15  # bounded, not a runaway
    eng.shutdown()
