"""Streaming playback (design-v2 E, player part): the WSOLA time-stretch,
the stream players, engine priority and cancel, and the daemon's streaming
path with its clip, timings and afplay fallback. No audio device is used."""

import asyncio
import itertools
import json
import logging
import sys
import threading
import time
import wave

import numpy as np
import pytest

from afterword.daemon import qwen, stream, voices
from afterword.daemon.config import ConfigStore, Profile, build_config
from afterword.daemon.stream import SAMPLE_RATE, NullStreamPlayer, TimeStretch
from afterword.daemon.synth import SaySynth, StreamResult

from test_lab import FakeEngine, FakeModel
from conftest import TIME_SCALE

SR = SAMPLE_RATE


def run(coro):
    return asyncio.run(coro)


def sine(freq, seconds, amp=0.5):
    t = np.arange(int(SR * seconds)) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def peak_hz(x):
    seg = x[len(x) // 4: 3 * len(x) // 4]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return float(np.fft.rfftfreq(len(seg), 1 / SR)[np.argmax(spec)])


def stretch_in_chunks(x, rate, seed=0):
    ts = TimeStretch(rate)
    rng = np.random.default_rng(seed)
    out, i = [], 0
    while i < len(x):
        n = int(rng.integers(50, 30000))
        out.append(ts.process(x[i:i + n]))
        i += n
    out.append(ts.flush())
    return np.concatenate(out)


# --------------------------------------------------------------------------- #
# WSOLA                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rate", [0.5, 0.8, 1.05, 1.25, 1.6, 2.0])
def test_wsola_length_follows_pace_and_pitch_is_kept(rate):
    x = sine(220, 3.0)
    y = stretch_in_chunks(x, rate)
    assert abs(len(y) - len(x) / rate) <= 2
    assert abs(peak_hz(y) - 220) < 2.0
    mid = y[len(y) // 4: 3 * len(y) // 4]
    assert 0.3 < float(np.sqrt((mid ** 2).mean())) < 0.4  # no gain jumps (sine rms 0.354)


def test_wsola_chunking_does_not_change_the_output():
    x = sine(180, 1.5) + sine(410, 1.5, 0.2)
    a = stretch_in_chunks(x, 1.3, seed=1)
    b = stretch_in_chunks(x, 1.3, seed=2)
    whole = TimeStretch(1.3)
    c = np.concatenate([whole.process(x), whole.flush()])
    assert len(a) == len(b) == len(c)
    assert np.allclose(a, c, atol=1e-5) and np.allclose(b, c, atol=1e-5)


def test_wsola_clamps_pace_and_handles_tiny_input():
    assert TimeStretch(5.0).rate == 2.0 and TimeStretch(0.1).rate == 0.5
    ts = TimeStretch(1.5)
    out = np.concatenate([ts.process(np.ones(10, np.float32) * 0.1), ts.flush()])
    assert len(out) == round(10 / 1.5)
    ts = TimeStretch(1.5)
    assert len(ts.flush()) == 0


# --------------------------------------------------------------------------- #
# Players                                                                      #
# --------------------------------------------------------------------------- #


def test_null_player_records_chunks_in_order_and_plays_them_out():
    player = NullStreamPlayer(speed=20)
    first = []
    pb = player.open(1.0, on_first_audio=lambda: first.append(time.time()))
    chunks = [np.full(2400, i / 10, np.float32) for i in range(1, 6)]
    for c in chunks:
        pb.write(c)
    pb.finish()
    assert pb.wait(5) and pb.completed
    assert len(pb.fed) == 5 and all(a is b for a, b in zip(pb.fed, chunks, strict=True))
    assert np.array_equal(np.concatenate(pb.played), np.concatenate(chunks))  # rate 1.0: no stretch
    assert pb.played_samples == 5 * 2400 and first and pb.first_audio_at


def test_null_player_stretches_and_switches_rate_between_segments():
    pb = NullStreamPlayer(speed=50).open(1.5)
    pb.write(sine(200, 1.0))
    pb.begin_segment(1.0)
    pb.write(sine(200, 0.5))
    pb.finish()
    assert pb.wait(5)
    played = sum(len(c) for c in pb.played)
    assert abs(played - (SR / 1.5 + SR * 0.5)) <= 4


def test_prebuffer_waits_for_audio_before_starting():
    pb = NullStreamPlayer(speed=10).open(1.0)
    pb.write(np.zeros(int(SR * 0.1), np.float32))
    time.sleep(0.2)
    assert pb.started_at is None  # 0.1 s < the 0.3 s pre-buffer
    pb.write(np.zeros(int(SR * 0.3), np.float32))
    assert _until(lambda: pb.started_at is not None)
    pb.finish()
    assert pb.wait(5)


def test_abort_stops_at_once():
    pb = NullStreamPlayer().open(1.0)  # real time
    pb.write(np.zeros(SR * 10, np.float32))
    pb.finish()
    assert _until(lambda: pb.started_at is not None)
    t = time.time()
    pb.abort()
    assert pb.wait(1) and time.time() - t < 0.5 * TIME_SCALE
    assert pb.aborted and not pb.completed and pb.played_s < 2


def test_device_open_failure_raises_player_unavailable():
    with pytest.raises(stream.PlayerUnavailable, match="no such device"):
        NullStreamPlayer(fail_open="no such device").open(1.0)


def test_stream_player_uses_sounddevice_output_stream():
    made = {}

    class FakeStream:
        latency = 0.01

        def __init__(self, **kw):
            made.update(kw)
            self.cb = kw["callback"]
            self.out = []
            self._stop = threading.Event()

        def start(self):
            def loop():
                while not self._stop.is_set():
                    buf = np.zeros((480, 1), np.float32)
                    self.cb(buf, 480, None, None)
                    self.out.append(buf[:, 0].copy())
                    time.sleep(0.001)
            threading.Thread(target=loop, daemon=True).start()

        def stop(self):
            self._stop.set()

        abort = stop

        def close(self):
            self._stop.set()

    class FakeSD:
        OutputStream = FakeStream
        inits = 0

        @classmethod
        def _terminate(cls):
            pass

        @classmethod
        def _initialize(cls):
            cls.inits += 1

    pb = stream.StreamPlayer(FakeSD).open(1.0)
    pb.write(np.full(4800, 0.25, np.float32))
    pb.finish()
    assert pb.wait(5) and pb.completed
    assert made["samplerate"] == SR and made["channels"] == 1 and made["dtype"] == "float32"
    assert pb.played_samples == 4800 and pb.latency == 0.01
    assert FakeSD.inits == 1  # the device list is refreshed before each open


def test_portaudio_is_not_reinitialised_while_a_timed_out_open_is_still_running(monkeypatch):
    """An open that timed out leaves its player thread inside PortAudio:
    the next item must not terminate/initialise PortAudio under it."""
    monkeypatch.setattr(stream, "OPEN_TIMEOUT_S", 0.2)
    gate = threading.Event()
    state = {"inside": 0, "terminated_while_inside": 0, "terminates": 0, "opens": 0}

    class SlowStream:
        latency = 0.0

        def __init__(self, **kw):
            state["inside"] += 1
            state["opens"] += 1
            try:
                if state["opens"] == 1:
                    gate.wait(5)  # a Bluetooth device taking its time
            finally:
                state["inside"] -= 1

        def start(self):
            pass

        def stop(self):
            pass

        abort = stop

        def close(self):
            pass

    class FakeSD:
        OutputStream = SlowStream

        @staticmethod
        def _terminate():
            state["terminates"] += 1
            if state["inside"]:
                state["terminated_while_inside"] += 1

        @staticmethod
        def _initialize():
            pass

    player = stream.StreamPlayer(FakeSD)
    with pytest.raises(stream.PlayerUnavailable, match="timed out"):
        player.open(1.0)
    assert state["terminates"] == 1  # the first open refreshed (nothing was in flight)
    with pytest.raises(stream.PlayerUnavailable, match="timed out"):
        player.open(1.0)  # waits behind the stuck open, never re-initialises under it
    gate.set()
    assert _until(lambda: not any(not pb.done.is_set() for pb in player._playbacks))
    pb = player.open(1.0)  # nothing in flight any more: refresh again
    pb.abort()
    assert pb.wait(2)
    assert state["terminated_while_inside"] == 0 and state["terminates"] == 2


def test_make_stream_player_without_sounddevice(monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)  # import fails
    player, why = stream.make_stream_player("afplay")
    assert player is None and "sounddevice is not available" in why
    player, why = stream.make_stream_player("null")
    assert isinstance(player, NullStreamPlayer) and why is None


def _until(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- #
# Engine: priority, cancel, streaming                                          #
# --------------------------------------------------------------------------- #


class StreamingModel(FakeModel):
    """Yields `n` chunks of 0.1 s, each with a distinct value, `delay` apart."""

    n = 20
    delay = 0.0

    def generate_custom_voice(self, **kw):
        self.calls.append(("custom_voice", kw))
        for i in range(self.n):
            if self.delay:
                time.sleep(self.delay)
            self.yielded = i + 1
            yield _Chunk(np.full(SR // 10, (i + 1) / 100, np.float32))


class _Chunk:
    def __init__(self, audio):
        self.audio = audio


class StreamingEngine(FakeEngine):
    def __init__(self, n=20, delay=0.0, **kw):
        self.n, self.delay = n, delay
        super().__init__(**kw)

    def _load_model(self, kind, path):
        model = StreamingModel(kind)
        model.n, model.delay = self.n, self.delay
        self.models_made[kind] = model
        self.loads.append((kind, str(path)))
        return model


def _req(text, clips):
    return qwen.build_request(text, voices.parse_voice("builtin:Ryan"), clips, lang="en", style=None,
                              mood=None, ref_text=None, seed=1, temperature=None)


def test_live_job_runs_before_waiting_lab_jobs(tmp_path):
    eng = StreamingEngine(n=1)
    clips = voices.ClipStore(tmp_path / "voices")
    gate = threading.Event()
    blocker = eng._submit(gate.wait, limit=False)  # holds the engine thread
    lab1 = eng.submit_render(_req("lab one", clips))
    lab2 = eng.submit_render(_req("lab two", clips))
    live = eng.submit_render(_req("live", clips), limit=False, priority=qwen.LIVE)
    gate.set()
    for f in (blocker, lab1, lab2, live):
        f.result(5)
    texts = [kw["text"] for _, kw in eng.models_made["custom_voice"].calls]
    assert texts == ["live", "lab one", "lab two"]
    eng.shutdown()


def test_lab_warm_up_waits_behind_live_speech(aw_home):
    """POST /api/lab/warm loads at LAB priority: a live line queued after it
    still runs first. The daemon's start-up warm-up stays LIVE."""
    from afterword.daemon.lab import Lab

    eng = StreamingEngine(n=1, keep_loaded=2)
    clips = voices.ClipStore(aw_home / "voices")
    store = ConfigStore()
    store.ensure_default_file()
    lab = Lab(aw_home, store, eng, clips)
    gate = threading.Event()

    async def scenario():
        eng._submit(gate.wait, limit=False, priority=qwen.LIVE)  # holds the engine thread
        warm = asyncio.create_task(lab.warm({"model": "base"}))
        await asyncio.sleep(0.05)  # the warm-up is queued
        live = eng.submit_render(_req("live", clips), limit=False, priority=qwen.LIVE)
        gate.set()
        await warm
        live.result(5)

    run(scenario())
    assert [kind for kind, _ in eng.loads] == ["custom_voice", "base"]
    eng.shutdown()


def test_streaming_render_hands_chunks_over_in_order(tmp_path):
    eng = StreamingEngine(n=5)
    got = []
    res = eng.render(_req("hello", voices.ClipStore(tmp_path / "v")), limit=False, priority=qwen.LIVE,
                     stream=True, on_chunk=lambda a, sr: got.append((a[0], sr)))
    assert [round(float(v), 2) for v, _ in got] == [0.01, 0.02, 0.03, 0.04, 0.05]
    assert {sr for _, sr in got} == {SR}
    assert res.first_chunk_s is not None and res.audio.size == 5 * SR // 10
    kw = eng.models_made["custom_voice"].calls[-1][1]
    assert kw["stream"] is True and kw["streaming_interval"] == 1.0
    eng.shutdown()


def test_cancel_stops_a_render_mid_way(tmp_path):
    eng = StreamingEngine(n=50, delay=0.01)
    cancel = threading.Event()
    got = []

    def on_chunk(a, sr):
        got.append(a)
        if len(got) == 3:
            cancel.set()

    with pytest.raises(qwen.RenderCancelled):
        eng.render(_req("long", voices.ClipStore(tmp_path / "v")), limit=False, stream=True,
                   cancel=cancel, on_chunk=on_chunk)
    assert len(got) == 3 and eng.models_made["custom_voice"].yielded <= 4
    # The engine is free for the next job.
    assert eng.render(_req("next", voices.ClipStore(tmp_path / "v"))).audio.size
    eng.shutdown()


def test_cancel_before_start_drops_the_job(tmp_path):
    from afterword.daemon.synth import wait_future

    eng = StreamingEngine(n=1)
    gate = threading.Event()
    eng._submit(gate.wait, limit=False)
    cancel = threading.Event()
    fut = eng.submit_render(_req("never", voices.ClipStore(tmp_path / "v")), cancel=cancel)
    cancel.set()
    with pytest.raises(qwen.RenderCancelled):
        wait_future(fut, cancel)
    gate.set()
    eng.render(_req("after", voices.ClipStore(tmp_path / "v")))
    assert [kw["text"] for _, kw in eng.models_made["custom_voice"].calls] == ["after"]
    eng.shutdown()


# --------------------------------------------------------------------------- #
# say segments                                                                 #
# --------------------------------------------------------------------------- #


@pytest.fixture
def fake_say(tmp_path):
    """A `say` stand-in that writes 1 s of a 22.05 kHz tone to -o."""
    script = tmp_path / "say"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import sys, wave, math, struct\n"
        "out = sys.argv[sys.argv.index('-o') + 1]\n"
        "sys.stdin.read()\n"
        "w = wave.open(out, 'wb'); w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)\n"
        "w.writeframes(b''.join(struct.pack('<h', int(8000 * math.sin(2 * math.pi * 300 * i / 22050))) for i in range(22050)))\n"
        "w.close()\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.mark.parametrize("miniaudio_ok", [True, False])
def test_say_stream_is_decoded_and_resampled_to_24k(fake_say, monkeypatch, miniaudio_ok):
    if not miniaudio_ok:
        monkeypatch.setitem(sys.modules, "miniaudio", None)
    got = []
    res = SaySynth(binary=fake_say).stream("hi", Profile(name="p", pace=1.3), {}, got.append, threading.Event())
    audio = np.concatenate(got)
    assert abs(len(audio) - SR) <= 2 and res.backend == "say"
    assert abs(res.audio_s - 1.0) < 0.01
    assert abs(peak_hz(audio) - 300) < 3
    assert max(len(c) for c in got) <= SR // 2  # fed in chunks


# --------------------------------------------------------------------------- #
# The daemon's streaming path                                                  #
# --------------------------------------------------------------------------- #


class ChunkSynth:
    """A synth that streams the given chunks (optionally slowly)."""

    name = "fake"

    def __init__(self, chunks, delay=0.0, fail=None):
        self.chunks = chunks
        self.delay = delay
        self.fail = fail
        self.texts = []
        self.cancelled = False
        self.sent = 0

    def stream(self, text, profile, attrs, sink, cancel):
        self.texts.append(text)
        t = time.perf_counter()
        n = 0
        for c in self.chunks:
            if cancel.is_set():
                self.cancelled = True
                raise qwen.RenderCancelled("cancelled")
            if self.delay:
                time.sleep(self.delay)
            try:
                sink(c)
            except qwen.RenderCancelled:
                self.cancelled = True
                raise
            self.sent += 1
            n += len(c)
        if self.fail:
            raise RuntimeError(self.fail)
        return StreamResult(synth_s=time.perf_counter() - t, audio_s=n / SR, first_chunk_s=0.001,
                            backend="fake", model="m", seed=7)

    def synth(self, text, profile, attrs, out):
        from afterword.daemon.synth import NullSynth

        return NullSynth().synth(text, profile, attrs, out)


def make_daemon(home, monkeypatch, synth=None, speed=50.0, player_env="null"):
    monkeypatch.setenv("AFTERWORD_SYNTH", "null")
    if player_env:
        monkeypatch.setenv("AFTERWORD_PLAYER", player_env)
    else:
        monkeypatch.delenv("AFTERWORD_PLAYER", raising=False)
    from afterword.daemon import server
    from afterword.daemon.log import EventLog

    monkeypatch.setattr(server, "QwenEngine", lambda *a, **k: FakeEngine())
    store = ConfigStore()
    store.ensure_default_file()
    d = server.Daemon(home, store, EventLog(home / "events.db"))
    if synth is not None:
        d.synth = synth
        d._refresh_backends = lambda: None  # keep the test synth
    if isinstance(d.stream_player, NullStreamPlayer):
        d.stream_player.speed = speed
    return d


async def play_one(d, text="hello there", until=("spoken", "error", "skipped"), during=None, timeout=10):
    return (await play_many(d, [text], until, during, timeout))[0]


async def play_many(d, texts, until=("spoken", "error", "skipped"), during=None, timeout=10):
    """Queue each text (one session each) and wait until all are done."""
    worker = asyncio.create_task(d.worker())
    acks = [
        d.dispatch(json.dumps({"type": "test", "text": t, "profile": "nova", "session_id": f"s{i}"}).encode(),
                   time.time())
        for i, t in enumerate(texts)
    ]
    end = time.time() + timeout
    fired = False
    while time.time() < end:
        evs = [d.events.get(a["event_id"]) for a in acks]
        if during and not fired and during[0](evs[0]):
            fired = True
            during[1]()
        if all(e["status"] in until for e in evs):
            break
        await asyncio.sleep(0.01)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    return [d.events.get(a["event_id"]) for a in acks]


def ramp_chunks(n=4, size=2400):
    return [np.linspace(-0.5, 0.5, size, dtype=np.float32) * (i + 1) / n for i in range(n)]


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        assert w.getframerate() == SR
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32767


def test_streamed_item_feeds_every_chunk_in_order_and_clip_matches(aw_home, monkeypatch):
    chunks = ramp_chunks()
    d = make_daemon(aw_home, monkeypatch, ChunkSynth(chunks))
    ev = run(play_one(d))
    assert ev["status"] == "spoken", ev
    fed = d.stream_player.last.fed
    assert len(fed) == len(chunks) and all(np.array_equal(a, b) for a, b in zip(fed, chunks, strict=True))
    clip = read_wav(ev["clip"])
    assert np.allclose(clip, np.concatenate(chunks), atol=1 / 16000)
    t = ev["timings"]
    assert t["stream"] is True and t["synth_backend"] == "fake" and t["model"] == "m" and t["seed"] == 7
    for key in ("first_audio_ms", "first_chunk_ms", "synth_ms", "audio_s", "rtf", "queue_wait_ms", "play_s"):
        assert key in t, key
    assert t["audio_s"] == round(4 * 2400 / SR, 3)
    assert t["segments"] == [{"lang": "en", "voice": "builtin:Serena", "model": "m", "backend": "fake",
                              "seed": 7, "audio_s": 0.4, "synth_ms": t["synth_ms"]}]
    assert d.status()["player"] == "null" and d.status()["stream"]["active"] is True


def test_segments_play_back_to_back_with_a_gap_and_trimmed_lead(aw_home, monkeypatch):
    from afterword.daemon.server import Segment

    lead = np.zeros(int(0.1 * SR), np.float32)  # 100 ms of silence before each segment
    body = np.full(4800, 0.3, np.float32)
    synth = ChunkSynth([np.concatenate([lead, body])])
    d = make_daemon(aw_home, monkeypatch, synth)
    profile = d.store.get().profile("nova")
    d._segments = lambda item, cfg, prof, says: [Segment("one", profile, {}), Segment("two", profile, {"pace": 1.5})]
    ev = run(play_one(d))
    assert ev["status"] == "spoken", ev
    assert synth.texts == ["one", "two"]
    clip = read_wav(ev["clip"])
    gap = int(0.25 * SR)
    trimmed = len(lead) - int(0.07 * SR)  # 70 ms of the second lead goes
    first = len(lead) + len(body)
    assert [s["audio_s"] for s in ev["timings"]["segments"]] == [round((len(lead) + 4800) / SR, 3),
                                                                 round((trimmed + 4800) / SR, 3)]
    # Pace applies to the second segment only; the 250 ms gap is played in
    # real time, not stretched by that pace.
    played = np.concatenate(d.stream_player.last.played)
    assert abs(len(played) - (first + gap + (trimmed + len(body)) / 1.5)) < 8
    assert np.allclose(played[first:first + gap], 0)
    # Mixed paces: the clip is stretched per segment, as heard, and has no
    # single playback_rate (History replays it at 1.0).
    assert abs(len(clip) - len(played)) < 8
    assert np.allclose(clip[first:first + gap], 0)
    assert "playback_rate" not in ev["timings"]


def test_segments_at_one_pace_keep_the_unstretched_clip_and_a_playback_rate(aw_home, monkeypatch):
    from afterword.daemon.server import Segment

    lead = np.zeros(int(0.1 * SR), np.float32)
    body = np.full(4800, 0.3, np.float32)
    synth = ChunkSynth([np.concatenate([lead, body])])
    d = make_daemon(aw_home, monkeypatch, synth)
    profile = d.store.get().profile("nova")
    d._segments = lambda item, cfg, prof, says: [Segment("one", profile, {"pace": 1.5}),
                                                 Segment("two", profile, {"pace": 1.5})]
    ev = run(play_one(d))
    assert ev["status"] == "spoken", ev
    gap = int(0.25 * SR)
    trimmed = len(lead) - int(0.07 * SR)
    assert len(read_wav(ev["clip"])) == len(lead) + len(body) + gap + trimmed + len(body)
    assert ev["timings"]["playback_rate"] == 1.5


def test_skip_part_way_cancels_the_render_and_keeps_the_rendered_clip(aw_home, monkeypatch):
    chunks = [np.full(SR // 2, 0.1, np.float32) for _ in range(40)]  # 20 s, 50 ms apart
    synth = ChunkSynth(chunks, delay=0.05)
    d = make_daemon(aw_home, monkeypatch, synth, speed=1.0)
    ev = run(play_one(d, during=(lambda e: e["status"] == "playing", d.skip)))
    assert ev["status"] == "skipped" and ev["reason"] == "stopped by user", ev
    assert synth.cancelled and synth.sent < len(chunks)
    clip = read_wav(ev["clip"])
    assert 0 < len(clip) < len(chunks) * SR // 2 and len(clip) % (SR // 2) == 0
    seg = ev["timings"]["segments"][0]
    assert seg["cancelled"] is True and seg["audio_s"] == round(len(clip) / SR, 3)


def test_engine_render_failure_mid_way_keeps_the_partial_clip(aw_home, monkeypatch):
    d = make_daemon(aw_home, monkeypatch, ChunkSynth(ramp_chunks(2), fail="model blew up"))
    ev = run(play_one(d))
    assert ev["status"] == "error" and "model blew up" in ev["error"]
    assert len(read_wav(ev["clip"])) == 2 * 2400


def test_device_open_failure_falls_back_to_afplay_path_and_logs_once(aw_home, monkeypatch, caplog):
    d = make_daemon(aw_home, monkeypatch, ChunkSynth(ramp_chunks()))
    d.stream_player = NullStreamPlayer(fail_open="device busy")
    caplog.set_level(logging.WARNING, logger="afterword.daemon")
    for ev in run(play_many(d, ["first", "second"])):
        assert ev["status"] == "spoken", ev
        assert ev["timings"]["stream"] is False  # the v1 path (null afplay player)
    warnings = [r for r in caplog.records if "device busy" in r.getMessage()]
    assert len(warnings) == 1
    assert "device busy" in d.status()["stream"]["error"]


def test_stream_failing_after_open_plays_the_clip_with_afplay(aw_home, monkeypatch):
    class BrokenOutput(stream._NullOutput):
        def start(self):
            raise OSError("stream start failed")

    class BrokenStart(NullStreamPlayer):
        def _opener(self, callback):
            return BrokenOutput(callback)

    played = []
    d = make_daemon(aw_home, monkeypatch, ChunkSynth(ramp_chunks()))
    d.stream_player = BrokenStart()

    async def fake_play(path, rate=1.0):
        played.append((path, rate))
        return True

    d.player.play = fake_play
    ev = run(play_one(d))
    assert ev["status"] == "spoken" and ev["timings"]["stream"] is False, ev
    assert played and str(played[0][0]) == ev["clip"]


def test_without_sounddevice_the_daemon_uses_afplay(aw_home, monkeypatch, tmp_path):
    """sounddevice cannot be imported: v1 path, the real AfplayPlayer
    (pointed at a stub binary) plays the whole WAV with -r for pace."""
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    log_file = tmp_path / "afplay.args"
    stub = tmp_path / "afplay"
    stub.write_text(f"#!/bin/sh\necho \"$@\" >> {log_file}\n")
    stub.chmod(0o755)
    (aw_home / "config.toml").write_text('[profiles.nova]\nvoice = "builtin:Serena"\npace = 1.2\n')
    d = make_daemon(aw_home, monkeypatch, player_env=None)
    assert d.stream_player is None and d.player.name == "afplay"
    st = d.status()
    assert st["player"] == "afplay" and st["stream"]["active"] is False
    assert "sounddevice is not available" in st["stream"]["error"]
    d.player.binary = str(stub)
    ev = run(play_one(d))
    assert ev["status"] == "spoken", ev
    assert ev["timings"]["stream"] is False
    args = log_file.read_text().split()
    assert args[:2] == ["-r", "1.200"] and args[-1] == ev["clip"]


def test_stream_setting_is_validated():
    cfg, errors = build_config({"daemon": {"stream": "yes"}})
    assert cfg.daemon.stream is True and any("daemon.stream" in e for e in errors)
    cfg, errors = build_config({"daemon": {"stream": False}})
    assert cfg.daemon.stream is False and not errors


def test_qwen_synth_streams_into_the_daemon(aw_home, monkeypatch):
    """The real QwenSynth and engine thread (fake model): chunks reach the
    player as the model yields them, at LIVE priority."""
    from afterword.daemon.synth import QwenSynth

    d = make_daemon(aw_home, monkeypatch)
    eng = StreamingEngine(n=6)
    d.engine = eng
    d.synth = QwenSynth(eng, d.voice_clips)
    d._refresh_backends = lambda: None
    ev = run(play_one(d))
    assert ev["status"] == "spoken", ev
    fed = d.stream_player.last.fed
    assert [round(float(c[0]), 2) for c in fed] == [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
    t = ev["timings"]
    assert t["synth_backend"] == "qwen" and t["model"] == "custom_voice" and isinstance(t["seed"], int)
    assert t["first_chunk_ms"] >= 0
    assert t["segments"][0]["backend"] == "qwen" and t["audio_s"] == 0.6
    eng.shutdown()


# --------------------------------------------------------------------------- #
# Review fixes: partial timings, dead devices, status freshness                #
# --------------------------------------------------------------------------- #


def test_cancelled_render_keeps_its_partial_timings(tmp_path):
    eng = StreamingEngine(n=50, delay=0.01)
    cancel = threading.Event()
    got = []

    def on_chunk(a, sr):
        got.append(a)
        if len(got) == 3:
            cancel.set()

    with pytest.raises(qwen.RenderCancelled) as info:
        eng.render(_req("long", voices.ClipStore(tmp_path / "v")), limit=False, stream=True,
                   cancel=cancel, on_chunk=on_chunk)
    part = info.value.partial
    assert part.synth_s > 0 and part.first_chunk_s is not None and part.model == "custom_voice"
    assert part.audio.size == 3 * SR // 10
    eng.shutdown()


def test_say_stream_cut_off_keeps_its_partial_timings(fake_say):
    from afterword.daemon.synth import partial_result

    cancel = threading.Event()
    got = []

    def sink(chunk):
        got.append(chunk)
        cancel.set()  # skip after the first chunk

    with pytest.raises(qwen.RenderCancelled) as info:
        SaySynth(binary=fake_say).stream("hi", Profile(name="p"), {}, sink, cancel)
    part = partial_result(info.value)
    assert part is not None and part.backend == "say" and part.synth_s > 0
    assert part.first_chunk_s is not None and part.audio_s == len(got[0]) / SR


def test_qwen_line_skipped_part_way_logs_the_render_time(aw_home, monkeypatch):
    """A streamed Qwen line stopped part-way still records how long the
    engine rendered (not synth_ms 0 / rtf null), so /api/stats stays true."""
    from afterword.daemon.synth import QwenSynth

    d = make_daemon(aw_home, monkeypatch, speed=1.0)
    eng = StreamingEngine(n=200, delay=0.02)  # 20 s of audio, rendered over 4 s
    d.engine = eng
    d.synth = QwenSynth(eng, d.voice_clips)
    d._refresh_backends = lambda: None
    ev = run(play_one(d, during=(lambda e: e["status"] == "playing", d.skip)))
    assert ev["status"] == "skipped", ev
    t = ev["timings"]
    assert t["synth_ms"] > 0 and t["rtf"] is not None and t["first_chunk_ms"] >= 0
    assert t["model"] == "custom_voice" and t["synth_backend"] == "qwen"
    seg = t["segments"][0]
    assert seg["cancelled"] is True and seg["synth_ms"] > 0 and seg["model"] == "custom_voice"
    assert eng.models_made["custom_voice"].yielded < 200
    stats = d.events.stats()["timings"]["synth_ms"]
    assert stats["n"] == 1 and stats["p50"] == t["synth_ms"]
    eng.shutdown()


def test_line_skipped_before_any_render_has_no_synth_time(aw_home, monkeypatch):
    synth = ChunkSynth(ramp_chunks())
    d = make_daemon(aw_home, monkeypatch, synth)
    real_open = d.stream_player.open

    def open_then_skip(*a, **k):
        pb = real_open(*a, **k)
        d._skip_current = True  # skipped while the device was opening
        return pb

    d.stream_player.open = open_then_skip
    ev = run(play_one(d))
    assert ev["status"] == "skipped", ev
    assert ev["timings"]["synth_ms"] is None and ev["timings"]["rtf"] is None
    assert d.events.stats()["timings"]["synth_ms"]["n"] == 0


def test_device_dying_mid_line_stops_the_render(aw_home, monkeypatch):
    """The device stops calling back after audio was heard: the render is
    cancelled at its next chunk instead of running on into a dead stream."""
    monkeypatch.setattr(stream, "STALL_S", 0.2)

    class DyingOutput(stream._NullOutput):
        def __init__(self, callback, speed=1.0):
            calls = [0]

            def cb(*a):
                calls[0] += 1
                if calls[0] <= 10:  # 200 ms, then the device goes quiet
                    callback(*a)

            super().__init__(cb, speed)

    class DyingPlayer(NullStreamPlayer):
        def _opener(self, callback):
            return DyingOutput(callback, self.speed)

    chunks = [np.full(SR // 2, 0.1, np.float32) for _ in range(40)]  # 20 s, rendered over 2 s
    synth = ChunkSynth(chunks, delay=0.05)
    d = make_daemon(aw_home, monkeypatch, synth, speed=1.0)
    d.stream_player = DyingPlayer(speed=1.0)
    t0 = time.time()
    ev = run(play_one(d))
    assert ev["status"] == "error" and "stopped responding" in ev["error"], ev
    assert synth.cancelled and synth.sent < 20 and time.time() - t0 < 1.8
    assert 0 < len(read_wav(ev["clip"])) < len(chunks) * SR // 2
    assert ev["timings"]["segments"][0]["cancelled"] is True


def test_stream_error_clears_once_the_device_opens_again(aw_home, monkeypatch):
    class FlakyPlayer(NullStreamPlayer):
        def _opener(self, callback):
            if self.opened == 1:
                raise OSError("device busy")
            return super()._opener(callback)

    d = make_daemon(aw_home, monkeypatch, ChunkSynth(ramp_chunks()))
    d.stream_player = FlakyPlayer(speed=50.0)
    first, second = run(play_many(d, ["first", "second"]))
    assert first["status"] == "spoken" and first["timings"]["stream"] is False
    assert second["status"] == "spoken" and second["timings"]["stream"] is True
    assert d.status()["stream"]["error"] is None


def test_status_follows_a_hot_reloaded_stream_setting_while_idle(aw_home, monkeypatch):
    d = make_daemon(aw_home, monkeypatch)
    cfg = aw_home / "config.toml"
    base = cfg.read_text()
    assert d.status()["stream"] == {"enabled": True, "active": True, "error": None}
    cfg.write_text(base.replace("stream = true", "stream = false"))
    os_utime_bump(cfg)
    st = d.status()
    assert st["stream"]["enabled"] is False and st["stream"]["active"] is False
    cfg.write_text(base.replace("stream = true", 'stream = "maybe"'))  # invalid: the default (true)
    os_utime_bump(cfg)
    st = d.status()
    assert st["stream"]["enabled"] is True and st["stream"]["active"] is True and st["player"] == "null"


_utime_steps = itertools.count(1)


def os_utime_bump(path):
    """Push the file's mtime a little further into the future each call, so
    the config store sees a change even within one filesystem timestamp tick."""
    import os

    t = time.time() + next(_utime_steps)
    os.utime(path, (t, t))
