"""Voice lab seed explorer (design-v2 A): pitch metrics, preemptible lab
renders, the explorer's state machine, stars that survive restarts, freeze
from a star, the stability check and the HTTP routes. A fake model stands in
for Qwen3-TTS: it yields a tone whose pitch depends on the seed."""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from afterword.daemon import explore as exploremod
from afterword.daemon import pitch, qwen, voices
from afterword.daemon.config import ConfigStore
from afterword.daemon.lab import Lab, LabError

from test_lab import SR, FakeEngine, FakeModel, _daemon, run
from conftest import TIME_SCALE


def tone(f0, seconds, sr=SR):
    t = np.arange(int(sr * seconds)) / sr
    return (sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 6)) * 0.2).astype(np.float32)


class _Chunk:
    def __init__(self, audio):
        self.audio = audio


class ToneModel(FakeModel):
    """Design voice whose pitch is drawn from mlx's seeded RNG (so the same
    seed gives the same pitch); streams `chunks` chunks of `chunk_s`, each
    after `delay` s, or yields once at the end when not streaming."""

    chunks = 3
    chunk_s = 0.4
    delay = 0.0

    def _f0(self):
        import mlx.core as mx

        return 90.0 + float(mx.random.uniform().item()) * 200.0

    def _gen(self, kind, kw):
        self.calls.append((kind, kw))
        f0 = self._f0()
        if kw.get("stream"):
            for _ in range(self.chunks):
                if self.delay:
                    time.sleep(self.delay)
                yield _Chunk(tone(f0, self.chunk_s))
        else:
            if self.delay:
                time.sleep(self.delay * self.chunks)
            yield _Chunk(tone(f0, self.chunk_s * self.chunks))

    def generate_voice_design(self, **kw):
        return self._gen("voice_design", kw)

    def generate_custom_voice(self, **kw):
        return self._gen("custom_voice", kw)


class ToneEngine(FakeEngine):
    def __init__(self, chunks=3, chunk_s=0.4, delay=0.0, **kw):
        self.tone = (chunks, chunk_s, delay)
        super().__init__(**kw)

    def _load_model(self, kind, path):
        if kind in self.fail_load:
            raise ValueError("Speech tokenizer not loaded")
        model = ToneModel(kind)
        model.chunks, model.chunk_s, model.delay = self.tone
        self.models_made[kind] = model
        self.loads.append((kind, str(path)))
        return model


DESIGN = {"text": "Hello there.", "voice": "design:A calm narrator.", "lang": "en", "pace": 1.1}


@pytest.fixture
def short_live_quiet(monkeypatch):
    """The explorer waits LIVE_QUIET_S after live speech; set it here so
    tests that cross that wait know it. Tests using it keep their gaps
    between live segments (0.05 s) far under it, scaled for slow runners, so
    a loaded one does not stretch a gap past it."""
    quiet = 0.6 * TIME_SCALE
    monkeypatch.setattr(exploremod, "LIVE_QUIET_S", quiet)
    return quiet


def make(home, engine=None, events=None):
    store = ConfigStore()
    store.ensure_default_file()
    sink = events if events is not None else []
    return Lab(home, store, engine or ToneEngine(), voices.ClipStore(home / "voices"),
               publish=lambda kind, data: sink.append((kind, data)))


async def until(pred, timeout=10.0 * TIME_SCALE):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# --------------------------------------------------------------------------- #
# Pitch                                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("f0", [80, 120, 200, 330])
def test_pitch_of_harmonic_tones(f0):
    got = pitch.f0_stats(tone(f0, 2.0), SR)
    assert got["f0_hz"] == pytest.approx(f0, rel=0.02)
    assert got["f0_spread_hz"] < 3


def test_pitch_spread_of_a_glide():
    t = np.arange(SR * 3) / SR
    phase = 2 * np.pi * np.cumsum(np.linspace(100, 200, t.size)) / SR
    got = pitch.f0_stats(np.sin(phase) * 0.3 + 0.1 * np.sin(2 * phase), SR)
    assert got["f0_hz"] == pytest.approx(150, abs=5)
    assert got["f0_spread_hz"] == pytest.approx(80, abs=8)  # 10th to 90th percentile


@pytest.mark.parametrize("audio", [np.zeros(SR), np.zeros(SR) + 0.01, np.zeros(10),
                                   np.random.default_rng(1).standard_normal(SR * 2) * 0.1, []])
def test_no_pitch_for_silence_noise_or_too_short(audio):
    assert pitch.f0_stats(audio, SR) == {"f0_hz": None, "f0_spread_hz": None, "voiced_s": None}


def test_pitch_is_null_without_numpy(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)  # import numpy -> ImportError
    assert pitch.f0_stats([0.0] * 100, SR)["f0_hz"] is None
    assert pitch.f0_of_wav("/nonexistent.wav")["f0_hz"] is None


@pytest.mark.parametrize("sr,step", [(8000, 1), (16000, 1), (22050, 2), (24000, 2), (44100, 3), (48000, 3)])
def test_pitch_works_at_most_at_16_khz(sr, step):
    assert pitch.analysis_step(sr) == step and sr / step <= 16000
    assert pitch.f0_stats(tone(180, 1.5, sr), sr)["f0_hz"] == pytest.approx(180, rel=0.02)


def test_pitch_of_a_wav_file(tmp_path):
    path = tmp_path / "t.wav"
    voices.write_wav(path, tone(150, 1.5), SR)
    assert pitch.f0_of_wav(path)["f0_hz"] == pytest.approx(150, rel=0.02)
    assert pitch.f0_of_wav(tmp_path / "missing.wav")["f0_hz"] is None


# --------------------------------------------------------------------------- #
# Engine: preemptible lab renders                                              #
# --------------------------------------------------------------------------- #


def _req(text, kind="design"):
    spec = voices.parse_voice("design:calm" if kind == "design" else "builtin:Ryan")
    return qwen.build_request(text, spec, voices.ClipStore("/nonexistent"), lang="en", style=None,
                              mood=None, ref_text=None, seed=7, temperature=None)


def test_live_render_preempts_a_preemptible_lab_render():
    eng = ToneEngine(chunks=24, delay=0.05)  # ~1.2 s if it ran to the end
    lab = eng.submit_render(_req("explore"), limit=False, stream=True, preemptible=True)
    assert lab.result is not None
    end = time.time() + 5
    while not eng._busy and time.time() < end:
        time.sleep(0.005)
    time.sleep(0.1)  # a couple of chunks in
    t0 = time.monotonic()
    live = eng.submit_render(_req("live", "builtin"), limit=False, priority=qwen.LIVE)
    with pytest.raises(qwen.RenderPreempted) as info:
        lab.result(5)
    assert time.monotonic() - t0 < 0.3 * TIME_SCALE  # at its next chunk, not after the ~1.1 s it had left
    assert isinstance(info.value, qwen.RenderCancelled)
    assert info.value.partial.seed == 7  # its timings are kept, as for a skip
    live.result(15)  # the live render itself is ~1.2 s here
    assert eng.preemptions == 1
    eng.shutdown()


def test_ordinary_lab_render_and_non_render_live_jobs_do_not_preempt():
    eng = ToneEngine(chunks=6, delay=0.03)
    lab = eng.submit_render(_req("preview"), stream=True)  # not preemptible
    end = time.time() + 5
    while not eng._busy and time.time() < end:
        time.sleep(0.005)
    live = eng.submit_render(_req("live", "builtin"), limit=False, priority=qwen.LIVE)
    assert lab.result(5).audio.size == int(SR * 0.4) * 6
    live.result(5)
    explore = eng.submit_render(_req("explore"), limit=False, stream=True, preemptible=True)
    while not eng._busy and time.time() < end:
        time.sleep(0.005)
    eng._submit(lambda: None, limit=False, priority=qwen.LIVE)  # housekeeping (trim), not speech
    assert explore.result(5).audio.size > 0
    assert eng.preemptions == 0
    eng.shutdown()


class RacyEngine(ToneEngine):
    """Runs `hook` on the engine thread after it takes the next preemptible
    job off the queue and before it starts it: the window in which live
    speech used to slip past without pre-empting."""

    hook = None

    def _run_job(self, job):
        if job.preempt is not None and self.hook is not None:
            hook, self.hook = self.hook, None
            hook()
        super()._run_job(job)


def test_live_render_queued_as_a_lab_job_is_dequeued_still_preempts_it():
    eng = RacyEngine(chunks=20, delay=0.05)
    live = {}
    eng.hook = lambda: live.setdefault("f", eng.submit_render(_req("live", "builtin"), limit=False,
                                                             priority=qwen.LIVE))
    lab = eng.submit_render(_req("explore"), limit=False, stream=True, preemptible=True)
    with pytest.raises(qwen.RenderPreempted):
        lab.result(5)
    assert not live["f"].done()  # failed before it ran: the live line was next
    live["f"].result(5)
    assert "voice_design" not in eng.models_made  # the lab take never started (or loaded)
    assert eng.preemptions == 1
    eng.shutdown()


def test_live_quiet_s():
    eng = ToneEngine(chunks=4, delay=0.05)
    assert eng.live_quiet_s() == float("inf")
    fut = eng.submit_render(_req("live", "builtin"), limit=False, priority=qwen.LIVE)
    assert eng.live_quiet_s() == 0.0  # queued or running
    fut.result(5)
    time.sleep(0.05)
    assert 0.04 < eng.live_quiet_s() < 1.0
    eng.submit_render(_req("preview"), limit=False).result(5)  # lab work does not count
    assert eng.live_quiet_s() > 0.04
    eng.shutdown()


def test_preemptible_job_still_honours_its_own_cancel():
    eng = ToneEngine(chunks=20, delay=0.03)
    cancel = threading.Event()
    fut = eng.submit_render(_req("explore"), limit=False, stream=True, preemptible=True, cancel=cancel)
    time.sleep(0.1)
    cancel.set()
    with pytest.raises(qwen.RenderCancelled) as info:
        fut.result(5)
    assert not isinstance(info.value, qwen.RenderPreempted)
    eng.shutdown()


# --------------------------------------------------------------------------- #
# Explorer state machine                                                       #
# --------------------------------------------------------------------------- #


def test_explorer_renders_ahead_of_the_cursor_with_pitch(home):
    events = []

    async def main():
        lab = make(home, events=events)
        view = await lab.explore_start({"settings": DESIGN, "start_seed": 10, "ahead": 2})
        assert view["state"]["status"] == "running" and view["takes"] == []
        x = lab.explorer.x
        await until(lambda: x.status == "waiting")
        first = [t["seed"] for t in lab.explore_view()["takes"]]
        lab.explore_cursor({"seed": 12})
        await until(lambda: x.status == "waiting" and x.count == 5)
        lab.explore_cursor({"seed": 11})  # moving back renders nothing new
        await asyncio.sleep(0.1)
        return lab, first, lab.explore_view(), lab.engine.models_made["voice_design"].calls

    lab, first, view, calls = run(main())
    assert first == [10, 11, 12]  # the cursor's take plus 2 ahead
    takes = view["takes"]
    assert [t["seed"] for t in takes] == [10, 11, 12, 13, 14]
    st = view["state"]
    assert st["cursor"] == 11 and st["next_seed"] == 15 and st["count"] == 5 and st["rendering"] is None
    assert st["settings"]["voice"] == "design:A calm narrator." and st["settings"]["pace"] == 1.1
    for t in takes:
        assert t["settings"]["seed"] == t["seed"] and t["explore_id"] == st["id"]
        assert 85 < t["f0_hz"] < 295 and t["f0_spread_hz"] is not None and t["audio_s"] == pytest.approx(1.2)
        assert t["url"] == f"/api/lab/previews/{t['id']}.wav" and lab.preview_path(t["id"]).is_file()
    assert len({t["f0_hz"] for t in takes}) > 1  # different seeds, different voices
    # Streamed (so it can be pre-empted), one job at a time, seeded per take.
    assert all(kw["stream"] is True for _, kw in calls)
    kinds = [(m["type"], m.get("take", {}).get("seed")) for k, m in events if k == "explore"]
    assert ("take", 10) in kinds and ("take", 14) in kinds and ("state", None) in kinds


def test_same_seed_same_take(home):
    async def main():
        lab = make(home)
        await lab.explore_start({"settings": DESIGN, "start_seed": 3, "ahead": 1})
        await until(lambda: lab.explorer.x.status == "waiting")
        a = lab.explore_view()["takes"][0]
        await lab.explore_start({"settings": DESIGN, "start_seed": 3, "ahead": 1})
        await until(lambda: lab.explorer.x.status == "waiting")
        b = lab.explore_view()["takes"][0]
        p = await lab.preview({**DESIGN, "seed": 3})
        return a, b, p

    a, b, p = run(main())
    assert a["seed"] == b["seed"] == p["seed"] == 3
    assert a["f0_hz"] == b["f0_hz"] == p["f0_hz"]  # previews carry the pitch too
    assert a["id"] != b["id"]


def test_new_exploration_replaces_the_old_one(home):
    async def main():
        lab = make(home)
        await lab.explore_start({"settings": DESIGN, "start_seed": 1, "ahead": 1})
        await until(lambda: lab.explorer.x.status == "waiting")
        old = lab.explore_view()
        await lab.explore_start({"settings": {**DESIGN, "text": "Another line."}, "start_seed": 50, "ahead": 1})
        await until(lambda: lab.explorer.x.status == "waiting")
        return lab, old, lab.explore_view()

    lab, old, new = run(main())
    assert new["state"]["id"] != old["state"]["id"] and new["state"]["text"] == "Another line."
    assert [t["seed"] for t in new["takes"]] == [50, 51]
    for t in old["takes"]:  # discarded with their files
        assert lab.preview_path(t["id"]) is None and not (home / "lab" / f"{t['id']}.wav").exists()
    assert sorted(p.stem for p in (home / "lab").glob("p*.wav")) == sorted(t["id"] for t in new["takes"])


def test_stop_keeps_the_takes_and_cursor_is_clamped(home):
    async def main():
        lab = make(home)
        await lab.explore_start({"settings": DESIGN, "start_seed": 5, "ahead": 1})
        x = lab.explorer.x
        await until(lambda: x.status == "waiting")
        moved = lab.explore_cursor({"seed": 999})["state"]  # never past the next seed
        low = lab.explore_cursor({"seed": 0, "auto": True})["state"]
        stopped = lab.explore_stop()["state"]
        await asyncio.sleep(0.2)
        again = lab.explore_cursor({"seed": 6})["state"]  # a stopped exploration stays stopped
        return lab, moved, low, stopped, again

    lab, moved, low, stopped, again = run(main())
    assert moved["cursor"] == 7 and low["cursor"] == 5
    assert stopped["status"] == "stopped" and again["status"] == "stopped"
    view = lab.explore_view()
    assert view["state"]["count"] == len(view["takes"]) >= 2
    assert all(lab.preview_path(t["id"]).is_file() for t in view["takes"])


def test_explorer_pauses_when_idle_and_auto_moves_do_not_count(home, monkeypatch):
    monkeypatch.setattr(exploremod, "IDLE_PAUSE_S", 0.3)

    async def main():
        lab = make(home)
        await lab.explore_start({"settings": DESIGN, "start_seed": 1, "ahead": 1})
        x = lab.explorer.x
        await until(lambda: x.status == "waiting")
        await asyncio.sleep(0.4)
        lab.explore_cursor({"seed": 2, "auto": True})  # auto-advance: not "being there"
        await until(lambda: x.status == "paused")
        paused = dict(lab.explore_view()["state"])
        lab.explore_cursor({"seed": 2})  # a real move resumes
        await until(lambda: x.status == "waiting" and x.count == 3)
        return paused, lab.explore_view()["state"]

    paused, after = run(main())
    assert paused["count"] == 2 and "has not moved" in paused["reason"]
    assert after["count"] == 3 and after["status"] == "waiting"


def test_explorer_stops_after_the_take_cap(home, monkeypatch):
    monkeypatch.setattr(exploremod, "MAX_TAKES", 3)

    async def main():
        lab = make(home)
        await lab.explore_start({"settings": DESIGN, "start_seed": 1, "ahead": 20})
        await until(lambda: lab.explorer.x.status == "done")
        return lab.explore_view()

    view = run(main())
    assert view["state"]["count"] == 3 and len(view["takes"]) == 3
    assert "3 takes" in view["state"]["reason"]


@pytest.mark.parametrize("body,status", [
    ("nope", 400),
    ({}, 400),
    ({"settings": {"voice": "design:x"}}, 400),                           # no text
    ({"settings": {**DESIGN, "voice": "say:Samantha"}}, 400),             # say has no seeds
    ({"settings": {**DESIGN, "seed": 4}}, 400),                           # use start_seed
    ({"settings": DESIGN, "start_seed": -1}, 400),
    ({"settings": DESIGN, "start_seed": 2**31}, 400),
    ({"settings": DESIGN, "ahead": 0}, 400),
    ({"settings": DESIGN, "ahead": 21}, 400),
    ({"settings": {**DESIGN, "profile": "nope"}}, 404),
])
def test_explore_start_rejects(home, body, status):
    async def main():
        lab = make(home)
        with pytest.raises(LabError) as info:
            await lab.explore_start(body)
        return info.value.status

    assert run(main()) == status


def test_cursor_and_stop_without_an_exploration(home):
    lab = make(home)
    with pytest.raises(LabError) as info:
        lab.explore_cursor({"seed": 1})
    assert info.value.status == 404
    assert lab.explore_stop()["state"]["status"] == "idle"
    assert lab.explore_view() == {"state": lab.explorer.state(), "takes": []}


def test_a_bad_voice_is_refused_before_the_old_exploration_goes(home):
    """A missing model or clip is an error from the start request, and the
    exploration already there (and its takes) stays."""

    async def main():
        lab = make(home, engine=ToneEngine(downloaded=("voice_design",)))
        takes = await _explore_two(lab)
        x = lab.explorer.x
        errs = []
        for settings in ({**DESIGN, "voice": "clone:nosuchclip"}, {**DESIGN, "voice": "builtin:Ryan"}):
            with pytest.raises(LabError) as info:
                await lab.explore_start({"settings": settings})
            errs.append((info.value.status, info.value.message))
        return lab, x, takes, errs

    lab, x, takes, errs = run(main())
    assert errs[0][0] == 404 and "nosuchclip" in errs[0][1]
    assert errs[1][0] == 503 and "not downloaded" in errs[1][1]
    assert lab.explorer.x is x and x.status == "waiting"
    assert [t["seed"] for t in lab.explore_view()["takes"]] == [t["seed"] for t in takes]
    assert all(lab.preview_path(t["id"]).is_file() for t in takes)
    assert lab.star({"preview_id": takes[0]["id"]})["created"]


def test_explorer_reports_a_render_error(home):
    """Errors only a render finds (an unknown speaker) still end it as error."""

    async def main():
        lab = make(home)
        await lab.explore_start({"settings": {**DESIGN, "voice": "builtin:Nobody"}})
        await until(lambda: lab.explorer.x.status == "error")
        return lab.explore_view()["state"]

    st = run(main())
    assert "unknown speaker" in st["reason"] and st["count"] == 0


def test_live_speech_preempts_the_explorer(home, monkeypatch, short_live_quiet):
    """A line the daemon speaks while the explorer renders stops that render
    at its next chunk; the seed is rendered again afterwards."""
    eng = ToneEngine(chunks=12, chunk_s=0.1, delay=0.1)  # 1.2 s per take (and per live line), 1.2 s of audio

    async def main():
        d = _daemon(home, monkeypatch, eng)
        worker = asyncio.create_task(d.worker())
        await d.lab.explore_start({"settings": DESIGN, "start_seed": 1, "ahead": 1})
        x = d.lab.explorer.x
        await until(lambda: x.count >= 1 and x.rendering is not None and eng._busy)
        await asyncio.sleep(0.1)  # inside a take's render, with about 1.1 s of it left
        seed = x.rendering
        t0 = time.time()
        ack = d.dispatch(json.dumps({"type": "test", "text": "hi", "profile": "ember"}).encode(), t0)
        await until(lambda: d.events.get(ack["event_id"])["status"] in ("spoken", "error", "skipped"))
        ev = d.events.get(ack["event_id"])
        await until(lambda: x.status == "waiting")
        worker.cancel()
        return ev, seed, x.preempted, d.lab.explore_view()

    ev, seed, preempted, view = run(main())
    assert ev["status"] == "spoken", ev
    assert preempted >= 1 and eng.preemptions >= 1
    # Heard after a chunk of each (0.2 s here), not after the ~1.1 s the
    # take had left and a chunk (1.2 s): the margin is for a loaded runner.
    assert ev["timings"]["first_audio_ms"] < 800 * TIME_SCALE
    seeds = [t["seed"] for t in view["takes"]]
    assert seeds == [1, 2] and seed in seeds  # the pre-empted seed came back
    assert view["state"]["preempted"] == preempted


def test_explorer_holds_back_while_live_speech_is_busy(home):
    busy = {"on": False}

    async def main():
        lab = make(home)
        lab.live_busy = lambda: busy["on"]
        busy["on"] = True
        await lab.explore_start({"settings": DESIGN, "start_seed": 1, "ahead": 2})
        x = lab.explorer.x
        await until(lambda: x.reason == exploremod.LIVE_HOLD)
        await asyncio.sleep(0.4)
        held = dict(lab.explore_view()["state"])
        busy["on"] = False
        await until(lambda: x.status == "waiting")
        return held, lab.explore_view()["state"]

    held, after = run(main())
    assert held["status"] == "running" and held["count"] == 0 and held["rendering"] is None
    assert after["count"] == 3 and after["reason"] != exploremod.LIVE_HOLD


def test_explorer_does_not_start_a_take_between_live_segments(home, short_live_quiet):
    """A reply's segments reach the engine one at a time. Before this, the
    pre-empted take was queued again at once, started in the gap after each
    segment and was pre-empted again by the next one (a model load and a
    first chunk in front of every segment)."""
    eng = ToneEngine(chunks=10, chunk_s=0.1, delay=0.05)  # 0.5 s per render

    async def main():
        lab = make(home, engine=eng)
        await asyncio.wrap_future(eng.submit_render(_req("warm", "builtin"), limit=False, priority=qwen.LIVE))
        await asyncio.sleep(short_live_quiet + 0.1)
        await lab.explore_start({"settings": DESIGN, "start_seed": 1, "ahead": 5})
        x = lab.explorer.x
        await until(lambda: x.rendering is not None and eng._busy)
        await asyncio.sleep(0.05)  # inside the take, with about 0.45 s of it left
        for i in range(3):  # three segments, each submitted when the last is done
            await asyncio.wrap_future(eng.submit_render(_req(f"segment {i}", "builtin"), limit=False,
                                                        priority=qwen.LIVE))
            await asyncio.sleep(0.05)  # the pipeline's gap before the next segment
        segments_done = eng.preemptions
        await until(lambda: x.count >= 2, timeout=15 * TIME_SCALE)
        return segments_done, x.preempted

    segments_done, preempted = run(main())
    assert segments_done == 1  # only the take that was running when the reply started
    assert preempted == 1


# --------------------------------------------------------------------------- #
# Stars, freeze, stability                                                     #
# --------------------------------------------------------------------------- #


async def _explore_two(lab):
    await lab.explore_start({"settings": DESIGN, "start_seed": 20, "ahead": 1})
    await until(lambda: lab.explorer.x.status == "waiting")
    return lab.explore_view()["takes"]


def test_stars_survive_a_restart(home):
    async def main():
        lab = make(home)
        takes = await _explore_two(lab)
        s1 = lab.star({"preview_id": takes[0]["id"], "note": "warm"})
        again = lab.star({"preview_id": takes[0]["id"]})
        p = await lab.preview({**DESIGN, "seed": 99})
        s2 = lab.star({"preview_id": p["id"]})
        return takes, s1, again, s2

    takes, s1, again, s2 = run(main())
    star = s1["star"]
    assert s1["created"] and not again["created"] and again["star"]["id"] == star["id"]
    assert star["seed"] == 20 and star["settings"]["seed"] == 20 and star["note"] == "warm"
    assert star["f0_hz"] == takes[0]["f0_hz"] and star["text"] == "Hello there."
    assert star["url"] == f"/api/lab/stars/{star['id']}.wav"
    assert (home / "lab" / "starred" / f"{star['id']}.wav").is_file()
    index = json.loads((home / "lab" / "starred.json").read_text())
    assert [s["id"] for s in index["stars"]] == [star["id"], s2["star"]["id"]]
    # "Restart": a new Lab clears the previews but keeps the stars.
    lab2 = make(home)
    assert not list((home / "lab").glob("p*.wav"))
    listed = lab2.stars_view()["stars"]
    assert [s["id"] for s in listed] == [s2["star"]["id"], star["id"]]  # newest first
    assert lab2.star_path(star["id"]).is_file()
    assert lab2.unstar(star["id"]) == {"ok": True}
    assert not (home / "lab" / "starred" / f"{star['id']}.wav").exists()
    with pytest.raises(LabError):
        lab2.unstar(star["id"])
    assert [s["id"] for s in make(home).stars_view()["stars"]] == [s2["star"]["id"]]


def test_star_errors_and_a_corrupt_index(home):
    lab = make(home)
    for body in ({"preview_id": "pnope"}, {"preview_id": 3}, "x"):
        with pytest.raises(LabError):
            lab.star(body)
    with pytest.raises(LabError) as info:
        lab.star_path("../config")
    assert info.value.status == 404
    (home / "lab").mkdir(exist_ok=True)
    (home / "lab" / "starred.json").write_text("{not json")
    lab2 = make(home)
    assert lab2.stars_view()["stars"] == [] and "could not be read" in lab2.stars_view()["error"]
    assert list((home / "lab").glob("starred.json.bad-*"))


def test_star_and_freeze_of_a_file_deleted_meanwhile_are_404(home, monkeypatch):
    """The take's file goes (a new exploration discards it, a star is
    deleted) between the check and the copy: 404, not a 500."""

    async def main():
        lab = make(home)
        takes = await _explore_two(lab)
        star = lab.star({"preview_id": takes[1]["id"]})["star"]
        return lab, takes, star

    lab, takes, star = run(main())
    real_add = exploremod.StarStore.add

    def add_after_delete(self, pid, entry, note, f0):
        Path(entry["path"]).unlink()
        return real_add(self, pid, entry, note, f0)

    monkeypatch.setattr(exploremod.StarStore, "add", add_after_delete)
    with pytest.raises(LabError) as info:
        lab.star({"preview_id": takes[0]["id"]})
    assert info.value.status == 404
    assert len(lab.stars_view()["stars"]) == 1

    real_read = Path.read_bytes
    star_wav = lab.star_path(star["id"])

    def read_after_delete(self):
        if self == star_wav:
            self.unlink()
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_after_delete)
    with pytest.raises(LabError) as info:
        lab.freeze({"star_id": star["id"], "name": "gone"})
    assert info.value.status == 404 and info.value.message == "star not found"
    assert not lab.clips.exists("gone")


def test_freeze_accepts_a_star(home):
    async def main():
        lab = make(home)
        takes = await _explore_two(lab)
        star = lab.star({"preview_id": takes[1]["id"]})["star"]
        a = lab.freeze({"star_id": star["id"], "name": "narrator"})
        b = lab.freeze({"preview_id": star["id"], "name": "narrator2"})
        c = lab.freeze({"preview_id": takes[0]["id"], "name": "take0"})  # an explorer take
        return star, a, b, c

    star, a, b, c = run(main())
    assert a["clip"]["name"] == "narrator" and a["clip"]["ref_text"] == "Hello there."
    assert b["clip"]["name"] == "narrator2" and c["clip"]["name"] == "take0"
    with pytest.raises(LabError) as info:
        make(home).freeze({"star_id": "s0000000000", "name": "x"})
    assert info.value.status == 404


def test_stability_check_of_a_star(home):
    async def main():
        lab = make(home)
        takes = await _explore_two(lab)
        star = lab.star({"preview_id": takes[0]["id"]})["star"]
        return star, await lab.stability({"star_id": star["id"]}), lab

    star, res, lab = run(main())
    assert res["star_id"] == star["id"] and res["seed"] == 20 and res["voice"] == "design:A calm narrator."
    assert res["base"]["f0_hz"] == star["f0_hz"]
    assert [t["check_lang"] for t in res["takes"]] == ["en", "en", "zh"]
    assert all(t["seed"] == 20 and t["settings"]["seed"] == 20 for t in res["takes"])
    assert all(t["text"] != "Hello there." for t in res["takes"])
    # The fake's pitch depends only on the seed: no drift.
    assert all(t["f0_delta_st"] == pytest.approx(0, abs=0.3) for t in res["takes"])
    assert res["max_delta_st"] == pytest.approx(0, abs=0.3) and res["errors"] == []
    assert all(lab.preview_path(t["id"]).is_file() for t in res["takes"])  # playable, freezable


def test_stability_check_from_settings_and_errors(home):
    async def main():
        lab = make(home)
        res = await lab.stability({"settings": {**DESIGN, "lang": "zh"}, "seed": 4})
        errs = []
        for body in ({"star_id": "s0000000000"}, {"settings": DESIGN}, {"settings": {**DESIGN, "voice": "say:Alex"}, "seed": 1}, "x"):
            try:
                await lab.stability(body)
            except LabError as exc:
                errs.append(exc.status)
        await lab._stability_lock.acquire()
        try:
            await lab.stability({"settings": DESIGN, "seed": 4})
        except LabError as exc:
            errs.append(exc.status)
        return res, errs

    res, errs = run(main())
    assert [t["check_lang"] for t in res["takes"]] == ["zh", "zh", "en"]
    assert res["base"] is None and res["star_id"] is None and res["seed"] == 4
    assert all(t["f0_delta_st"] is None for t in res["takes"])
    assert errs == [404, 400, 400, 400, 409]


def test_stability_plan():
    plan = exploremod.stability_plan("en", exploremod.STABILITY_TEXTS["en"][0])
    assert [c for c, _ in plan] == ["en", "en", "zh"]
    assert exploremod.STABILITY_TEXTS["en"][0] not in [t for _, t in plan]
    assert [c for c, _ in exploremod.stability_plan(None, "x")] == ["en", "en", "zh"]
    assert [c for c, _ in exploremod.stability_plan("ja", "x")] == ["ja", "ja", "en"]
    assert [c for c, _ in exploremod.stability_plan("xx", "x")] == ["en", "en", "zh"]
    assert all(len(v) == 2 for v in exploremod.STABILITY_TEXTS.values())


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def test_explore_http_routes(home, monkeypatch):
    from fastapi.testclient import TestClient

    from afterword.daemon.web import build_app

    d = _daemon(home, monkeypatch, ToneEngine())
    H = {"X-AfterWord": "1"}
    with TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800") as client:
        assert client.get("/api/lab/explore").json()["state"]["status"] == "idle"
        body = {"settings": DESIGN, "start_seed": 7, "ahead": 1}
        assert client.post("/api/lab/explore", json=body).status_code == 403  # the guard
        r = client.post("/api/lab/explore", json=body, headers=H)
        assert r.status_code == 200, r.text
        end = time.time() + 10
        while client.get("/api/lab/explore").json()["state"]["status"] != "waiting" and time.time() < end:
            time.sleep(0.02)
        view = client.get("/api/lab/explore").json()
        assert [t["seed"] for t in view["takes"]] == [7, 8]
        assert client.get(view["takes"][0]["url"]).headers["content-type"] == "audio/wav"
        r = client.post("/api/lab/explore/cursor", json={"seed": 8}, headers=H)
        assert r.status_code == 200 and r.json()["state"]["cursor"] == 8
        assert client.post("/api/lab/explore/cursor", json={"seed": "x"}, headers=H).status_code == 400
        star = client.post("/api/lab/stars", json={"preview_id": view["takes"][1]["id"], "note": "nice"}, headers=H)
        assert star.status_code == 200, star.text
        sid = star.json()["star"]["id"]
        assert client.get("/api/lab/stars").json()["stars"][0]["id"] == sid
        assert client.get(f"/api/lab/stars/{sid}.wav").status_code == 200
        assert client.get("/api/lab/stars/..%2Fstarred.json").status_code == 404
        r = client.post("/api/lab/stability", json={"star_id": sid}, headers=H)
        assert r.status_code == 200 and len(r.json()["takes"]) == 3
        r = client.post("/api/lab/freeze", json={"star_id": sid, "name": "explored"}, headers=H)
        assert r.status_code == 200 and r.json()["clip"]["name"] == "explored"
        assert client.post("/api/lab/explore/stop", headers=H).json()["state"]["status"] == "stopped"
        assert client.delete(f"/api/lab/stars/{sid}", headers=H).json() == {"ok": True}
        assert client.delete(f"/api/lab/stars/{sid}", headers=H).status_code == 404


def test_close_ends_a_running_exploration(home):
    """Daemon shutdown: the loop ends and the engine job is cancelled."""
    eng = ToneEngine(chunks=20, delay=0.05)

    async def main():
        lab = make(home, engine=eng)
        await lab.explore_start({"settings": DESIGN, "ahead": 5})
        x = lab.explorer.x
        await until(lambda: x.rendering is not None and eng._busy)
        lab.explorer.close()
        try:
            await x.task
        except asyncio.CancelledError:
            pass
        await until(lambda: not eng._busy, timeout=3 * TIME_SCALE)
        return x

    x = run(main())
    assert x.task.done() and x.stopped and x.count == 0
    eng.shutdown()
