"""Sessions, mute, queue removal, history, stats and config endpoints
(docs/gui-api.md)."""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from afterword.hooks import common
from test_lab import FakeEngine, _daemon, run

H = {"X-AfterWord": "1"}


@pytest.fixture
def gui(aw_home, monkeypatch):
    from afterword.daemon.web import build_app

    d = _daemon(aw_home, monkeypatch, FakeEngine())
    client = TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800", raise_server_exceptions=False)
    return d, client


def hook_msg(sid, text="hi", decision="speak", profile="nova", state_changed=False):
    return json.dumps({
        "type": "hook", "event": "Stop", "session_id": sid, "cwd": f"/tmp/{sid}", "profile": profile,
        "decision": decision, "reason": None if decision == "speak" else "session off", "text": text,
        "attrs": {}, "t_hook": time.time(), "state_changed": state_changed, "payload": {},
    }).encode()


def test_sessions_merge_state_files_and_events(gui):
    d, client = gui
    common.save_state("on-1", {"profile": "nova", "cwd": "/w/on", "source": "env"})
    d.dispatch(hook_msg("off-1", decision="skip", profile=None), time.time())
    d.dispatch(hook_msg("on-1"), time.time())
    body = client.get("/api/sessions").json()
    assert body["muted_all"] is False and body["profiles"] == ["nova", "ember"]
    assert body["voice_in_use_s"] == common.VOICE_IN_USE_S  # the window the Sessions page warns on
    by_id = {s["session_id"]: s for s in body["sessions"]}
    assert by_id["on-1"]["profile"] == "nova" and by_id["on-1"]["source"] == "env"
    assert by_id["on-1"]["cwd"] == "/w/on" and by_id["on-1"]["events"] == 1
    assert by_id["off-1"]["profile"] is None and by_id["off-1"]["cwd"] == "/tmp/off-1"
    assert by_id["off-1"]["last_event"]["status"] == "skipped"
    assert body["sessions"][0]["session_id"] == "on-1"  # newest activity first


def test_set_session_profile_off_and_mute(gui):
    d, client = gui
    r = client.post("/api/sessions/s1", json={"profile": "ember"}, headers=H)
    assert r.status_code == 200 and r.json()["session"]["profile"] == "ember"
    assert common.load_state("s1")["source"] == "gui"
    assert common.load_state("s1")["last_profile"] == "ember"  # so /voice on returns to it
    assert client.post("/api/sessions/s1", json={"muted": True}, headers=H).json()["session"]["muted"] is True
    # muted: lines are logged but not played
    ack = d.dispatch(hook_msg("s1"), time.time())
    assert ack["status"] == "skipped" and ack["reason"] == "session muted"
    assert client.post("/api/sessions/s1", json={"profile": None}, headers=H).json()["session"]["profile"] is None
    r = client.post("/api/sessions/s1", json={"profile": "nope"}, headers=H)
    assert r.status_code == 400 and "nova" in r.json()["error"]
    assert client.post("/api/sessions/bad%20id", json={"muted": True}, headers=H).status_code == 400
    assert client.post("/api/sessions/s1", json={}, headers=H).status_code == 400
    assert client.post("/api/sessions/s1", json={"muted": "yes"}, headers=H).status_code == 400
    assert client.post("/api/sessions/s1", json={"muted": True}).status_code == 403  # no header


def test_mute_all_persists_and_skips(gui, aw_home, monkeypatch):
    d, client = gui
    assert client.post("/api/mute", json={"muted": True}, headers=H).json() == {"muted_all": True}
    ack = d.dispatch(hook_msg("s2"), time.time())
    assert ack["reason"] == "all muted"
    d2 = _daemon(aw_home, monkeypatch, FakeEngine())
    assert d2.muted_all is True
    assert client.post("/api/mute", json={"muted": "no"}, headers=H).status_code == 400
    client.post("/api/mute", json={"muted": False}, headers=H)
    assert d.dispatch(hook_msg("s2"), time.time())["status"] == "queued"


def test_queue_remove(gui):
    d, client = gui
    a = d.dispatch(hook_msg("qa"), time.time())
    b = d.dispatch(hook_msg("qb"), time.time())
    r = client.post(f"/api/queue/remove/{a['event_id']}", headers=H)
    assert r.json() == {"removed": a["event_id"]}
    assert d.events.get(a["event_id"])["reason"] == "removed by user"
    assert [i["event_id"] for i in client.get("/api/queue").json()["pending"]] == [b["event_id"]]
    assert client.post(f"/api/queue/remove/{a['event_id']}", headers=H).status_code == 404


def test_history_lists_only_clips_on_disk(gui):
    d, client = gui
    for i in range(3):
        ev = d.events.insert(kind="hook", status="spoken", text=f"t{i}", session_id="h",
                             timings={"audio_s": 1.5, "playback_rate": 1.1}, clip=str(d.clips_dir / f"{i + 1}.wav"))
        if i != 1:
            (d.clips_dir / f"{ev['id']}.wav").write_bytes(b"RIFF")
            d.events.update(ev["id"], clip=str(d.clips_dir / f"{ev['id']}.wav"))
    items = client.get("/api/history").json()["items"]
    assert [i["text"] for i in items] == ["t2", "t0"]
    assert items[0]["playback_rate"] == 1.1 and items[0]["url"] == f"/api/clips/{items[0]['id']}"
    r = client.get(items[0]["download_url"])
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert client.get("/api/history", params={"before": items[0]["id"]}).json()["items"][0]["text"] == "t0"
    assert client.get("/api/clips/999").json() == {"error": "clip not kept"}


def test_stats(gui):
    d, client = gui
    for i, ms in enumerate([10, 20, 30, 40]):
        d.events.insert(kind="hook", status="spoken" if i else "skipped", reason=None if i else "session off",
                        timings={"hook_ms": ms, "rtf": 0.25})
    d.events.insert(kind="system", status="info", reason="ignored")
    d.events.insert(kind="hook", hook_event="UserPromptSubmit", status="info", reason="added <say>",
                    timings={"hook_ms": 999})
    s = client.get("/api/stats").json()
    assert s["count"] == 4 and s["by_status"] == {"spoken": 3, "skipped": 1}
    assert s["by_reason"] == {"session off": 1}
    assert s["timings"]["hook_ms"] == {"n": 4, "p50": 30.0, "p95": 40.0, "max": 40.0}
    assert s["timings"]["synth_ms"]["n"] == 0


def test_config_view_and_reload(gui, aw_home):
    d, client = gui
    c = client.get("/api/config").json()
    assert c["path"].endswith("config.toml") and "[profiles.nova]" in c["text"]
    assert c["error"] is None and c["synth"] == "qwen" and "models" in c["engine"]
    (aw_home / "config.toml").write_text("not [valid toml")
    c = client.post("/api/config/reload", headers=H).json()
    assert c["error"] and "nova" in d.store.get().profiles  # last good config kept
    assert c["text"] == "not [valid toml"


def test_hook_state_change_is_published(gui):
    d, client = gui
    q = d.events.subscribe()
    common.save_state("pub", {"profile": "nova"})
    d.dispatch(hook_msg("pub", decision="skip", state_changed=True), time.time())
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait())
    sess = [m for m in kinds if m["type"] == "sessions"]
    assert sess and sess[0]["data"]["session"]["profile"] == "nova"


def test_muting_or_turning_off_drops_queued_lines(gui):
    d, client = gui
    common.save_state("q1", {"profile": "nova"})
    a = d.dispatch(hook_msg("q1", text="one"), time.time())
    b = d.dispatch(hook_msg("q2", text="two"), time.time())
    client.post("/api/sessions/q1", json={"muted": True}, headers=H)
    assert d.events.get(a["event_id"])["reason"] == "session muted"
    assert [i.event_id for i in d.queue._items] == [b["event_id"]]
    client.post("/api/sessions/q2", json={"profile": None}, headers=H)
    assert d.events.get(b["event_id"])["reason"] == "session off"
    assert d.queue._items == []


def test_worker_rechecks_mute_before_synthesizing(gui):
    d, client = gui
    ack = d.dispatch(hook_msg("w1"), time.time())
    common.save_state("w1", {"profile": "nova", "muted": True})  # e.g. /voice mute after it was queued
    item = d.queue.pop_nowait()
    run(d._play_item(item))
    ev = d.events.get(ack["event_id"])
    assert ev["status"] == "skipped" and ev["reason"] == "session muted"


def test_pruned_clips_leave_history(gui):
    d, client = gui
    ids = []
    for i in range(3):
        ev = d.events.insert(kind="hook", status="spoken", text=f"c{i}", timings={})
        path = d.clips_dir / f"{ev['id']}.wav"
        path.write_bytes(b"RIFF")
        os.utime(path, (time.time() + i, time.time() + i))
        d.events.update(ev["id"], clip=str(path))
        ids.append(ev["id"])
    d._prune_clips(1)
    assert [i["id"] for i in client.get("/api/history").json()["items"]] == [ids[-1]]
    assert d.events.get(ids[0])["clip"] is None


def test_session_activity_for_matches_bulk(gui):
    d, _ = gui
    for sid in ("x", "x", "y"):
        d.dispatch(hook_msg(sid, decision="skip", profile=None), time.time())
    bulk = d.events.session_activity(100)
    assert d.events.session_activity_for("x")["events"] == bulk["x"]["events"] == 2
    assert d.events.session_activity_for("nobody") is None


# --------------------------------------------------------------------------- #
# Voices: GET /api/voices, PATCH /api/lab/profiles/{name}                      #
# --------------------------------------------------------------------------- #

COMMENTED = """# my voices
[profiles.nova]   # the first one
voice = "say:Samantha"
style = "calm"
description = "old text"   # keep my comment
custom_key = "stay"

[profiles.ember]
voice = "say:Alex"
"""


def _write_cfg(aw_home, d, text=COMMENTED):
    (aw_home / "config.toml").write_text(text)
    d.store.reload(force=True)


def test_voices_lists_profiles_with_usage(gui, aw_home):
    d, client = gui
    _write_cfg(aw_home, d)
    now = time.time()
    common.note_voice("s-old", "nova", "/old", now=now - 3 * 3600)  # past the in-use window
    common.note_voice("s-a", "nova", "/a", now=now - 600)
    common.note_voice("s-b", "nova", None, now=now - 60)
    r = client.get("/api/voices")
    assert r.status_code == 200
    profiles = r.json()["profiles"]
    assert [p["name"] for p in profiles] == list(d.store.get().profiles)
    assert [p["name"] for p in profiles][:2] == ["nova", "ember"]
    nova, ember = profiles[0], profiles[1]
    assert nova["voice"] == "say:Samantha" and nova["style"] == "calm" and nova["description"] == "old text"
    assert nova["lang"] == "en" and nova["persona"] == ""
    assert [u["session_id"] for u in nova["in_use"]] == ["s-b", "s-a"]  # newest first, stale one out
    assert nova["in_use"][1]["cwd"] == "/a" and nova["in_use"][0]["cwd"] is None
    assert nova["last_session"] == "s-b" and nova["last_used"] == pytest.approx(now - 60)
    assert nova["last_cwd"] is None
    assert ember["in_use"] == [] and ember["last_used"] is None
    assert ember["last_session"] is None and ember["last_cwd"] is None and ember["description"] == ""


def test_voices_without_usage_file(gui):
    _, client = gui
    for p in client.get("/api/voices").json()["profiles"]:
        assert p["in_use"] == [] and p["last_used"] is None


def test_patch_description_sets_trims_and_removes(gui, aw_home):
    d, client = gui
    _write_cfg(aw_home, d)
    r = client.patch("/api/lab/profiles/ember", json={"description": "  warm and low  "}, headers=H)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["profile"]["name"] == "ember" and r.json()["profile"]["description"] == "warm and low"
    assert "in_use" in r.json()["profile"] and "persona" in r.json()["profile"]
    text = (aw_home / "config.toml").read_text()
    assert 'description = "warm and low"' in text
    # comments, other keys and the other profile are untouched
    for kept in ("# my voices", "# the first one", "# keep my comment", 'custom_key = "stay"',
                 'description = "old text"', 'voice = "say:Alex"'):
        assert kept in text
    assert d.store.get().profiles["ember"].description == "warm and low"
    r = client.patch("/api/lab/profiles/nova", json={"description": ""}, headers=H)
    assert r.status_code == 200 and r.json()["profile"]["description"] == ""
    text = (aw_home / "config.toml").read_text()
    assert "old text" not in text and 'custom_key = "stay"' in text and "# my voices" in text
    assert d.store.get().profiles["nova"].description == ""


def test_patch_description_errors(gui, aw_home):
    d, client = gui
    _write_cfg(aw_home, d)
    before = (aw_home / "config.toml").read_text()
    assert client.patch("/api/lab/profiles/nope", json={"description": "x"}, headers=H).status_code == 404
    assert client.patch("/api/lab/profiles/nova", json={"description": "x" * 301}, headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nova", json={"description": 5}, headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nova", json={"description": None}, headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nova", json={}, headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nova", json=["x"], headers=H).status_code == 400
    assert client.patch("/api/lab/profiles/nova", json={"description": "x"}).status_code == 403  # no header
    assert (aw_home / "config.toml").read_text() == before
    assert client.patch("/api/lab/profiles/nova", json={"description": "x" * 300}, headers=H).status_code == 200


def test_lab_save_keeps_description_unless_given(gui, aw_home):
    d, client = gui
    _write_cfg(aw_home, d)
    body = {"name": "nova", "voice": "say:Daniel", "overwrite": True}
    r = client.post("/api/lab/profiles", json=body, headers=H)
    assert r.status_code == 200
    assert r.json()["profile"]["voice"] == "say:Daniel" and r.json()["profile"]["description"] == "old text"
    assert d.store.get().profiles["nova"].description == "old text"
    r = client.post("/api/lab/profiles", json={**body, "description": " new one "}, headers=H)
    assert r.json()["profile"]["description"] == "new one"
    r = client.post("/api/lab/profiles", json={**body, "description": ""}, headers=H)
    assert r.json()["profile"]["description"] == ""
    assert "description" not in (aw_home / "config.toml").read_text().split("[profiles.ember]")[0]
    assert client.post("/api/lab/profiles", json={**body, "description": "y" * 301}, headers=H).status_code == 400
    r = client.post("/api/lab/profiles", json={"name": "fresh", "voice": "say:Alex", "description": "hi"}, headers=H)
    assert r.status_code == 200 and d.store.get().profiles["fresh"].description == "hi"
