"""Speak anything and replay (docs/gui-api.md, "Speak and replay"):
POST /api/speak and POST /api/events/{id}/replay."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from afterword.daemon.web import replay_says
from test_lab import FakeEngine, _daemon, run

H = {"X-AfterWord": "1"}

LANGS_CFG = """
[profiles.nova]
voice = "say:Samantha"
lang = "en"
max_chars = 400

[profiles.nova.langs.ja]
voice = "say:Kyoko"

[profiles.ember]
voice = "say:Daniel"
lang = "en"

[pronounce]
"SQLite" = "sequel lite"
"""


@pytest.fixture
def gui(aw_home, monkeypatch):
    from afterword.daemon.web import build_app

    d = _daemon(aw_home, monkeypatch, FakeEngine())
    client = TestClient(build_app(d, 47800), base_url="http://127.0.0.1:47800", raise_server_exceptions=False)
    return d, client


def _write_cfg(aw_home, d, text=LANGS_CFG):
    (aw_home / "config.toml").write_text(text)
    d.store.reload(force=True)


def hook_msg(sid, text="hi", segments=None, parsed=None, turn=None, profile="nova"):
    return json.dumps({
        "type": "hook", "event": "Stop", "session_id": sid, "cwd": f"/tmp/{sid}", "profile": profile,
        "decision": "speak", "reason": None, "text": text, "attrs": {}, "segments": segments or [],
        "turn": turn, "parsed": parsed, "t_hook": time.time(), "payload": {},
    }).encode()


def pending(d):
    return [i.event_id for i in d.queue._items]


# --------------------------------------------------------------------------- #
# POST /api/speak                                                              #
# --------------------------------------------------------------------------- #


def test_speak_queues_a_gui_line(gui):
    d, client = gui
    r = client.post("/api/speak", json={"text": "  Hello from the page.  ", "profile": "ember"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "queued"
    ev = d.events.get(body["event_id"])
    assert ev["kind"] == "test" and ev["session_id"] == "gui" and ev["profile"] == "ember"
    assert ev["status"] == "queued" and ev["text"] == "Hello from the page."
    snap = client.get("/api/queue").json()
    assert [i["event_id"] for i in snap["pending"]] == [body["event_id"]]
    assert snap["pending"][0]["session_id"] == "gui"
    # The page's header is required, like every write.
    assert client.post("/api/speak", json={"text": "x", "profile": "nova"}).status_code == 403


def test_speak_validates_its_input(gui):
    d, client = gui
    cases = [
        ({"text": "", "profile": "nova"}, "empty"),
        ({"text": "   \n ", "profile": "nova"}, "empty"),
        ({"text": "x" * 2001, "profile": "nova"}, "2000"),
        ({"text": "hi"}, "nova"),
        ({"text": "hi", "profile": "nope"}, "ember"),
        ({"text": "hi", "profile": 3}, "profile"),
        ({"text": 5, "profile": "nova"}, "text"),
        ({"text": "hi", "profile": "nova", "lang": "klingon"}, "language"),
        ({"text": "hi", "profile": "nova", "lang": 7}, "lang"),
    ]
    for body, word in cases:
        r = client.post("/api/speak", json=body, headers=H)
        assert r.status_code == 400, body
        assert word in r.json()["error"], (body, r.json())
    assert client.post("/api/speak", content=b"{", headers=H).status_code == 400
    assert client.post("/api/speak", json=["hi"], headers=H).status_code == 400
    assert d.events.count() == 0 and pending(d) == []
    # 2000 characters after trimming is fine.
    r = client.post("/api/speak", json={"text": "  " + "a " * 999 + "a  ", "profile": "nova"}, headers=H)
    assert r.status_code == 200 and r.json()["status"] == "queued"


def test_speak_lines_queue_in_order(gui):
    d, client = gui
    ids = [client.post("/api/speak", json={"text": f"line {n}", "profile": "nova"}, headers=H).json()["event_id"]
           for n in range(3)]
    assert pending(d) == ids  # typed lines do not supersede each other
    assert all(d.events.get(i)["status"] == "queued" for i in ids)


def test_speak_honours_mute_all(gui):
    d, client = gui
    client.post("/api/mute", json={"muted": True}, headers=H)
    r = client.post("/api/speak", json={"text": "quiet please", "profile": "nova"}, headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "skipped" and body["reason"] == "all muted"
    assert d.events.get(body["event_id"])["reason"] == "all muted" and pending(d) == []


def test_speak_lang_and_pronunciations(gui, aw_home):
    d, client = gui
    _write_cfg(aw_home, d)
    r = client.post("/api/speak", json={"text": "SQLite がいい", "profile": "nova", "lang": "Japanese"}, headers=H)
    assert r.status_code == 200
    item = d.queue._items[-1]
    assert item.event_id == r.json()["event_id"]
    assert item.attrs == {"lang": "ja"} and d.events.get(item.event_id)["attrs"] == {"lang": "ja"}
    cfg = d.store.get()
    profile = cfg.profile(item.profile)
    says = d._spoken_says(item, cfg, profile)
    assert says[0][0] == "sequel lite がいい"  # the pronunciation list applies
    segs = d._segments(item, cfg, profile, says)
    assert [s.profile.voice for s in segs] == ["say:Kyoko"]  # the profile's ja voice
    # Without lang: as any line of a profile with [langs], split by script.
    client.post("/api/speak", json={"text": "Hello there, this is fine. こんにちは、元気ですか。", "profile": "nova"}, headers=H)
    item = d.queue._items[-1]
    segs = d._segments(item, cfg, profile, d._spoken_says(item, cfg, profile))
    assert [s.profile.voice for s in segs] == ["say:Samantha", "say:Kyoko"]


def test_speak_is_spoken_by_the_worker(gui, aw_home):
    d, client = gui
    _write_cfg(aw_home, d)
    r = client.post("/api/speak", json={"text": "SQLite works", "profile": "nova"}, headers=H)
    item = d.queue.pop_nowait()
    run(d._play_item(item))
    ev = d.events.get(r.json()["event_id"])
    assert ev["status"] == "spoken", ev
    assert ev["timings"]["spoken_text"] == "sequel lite works"


def test_speak_503_when_stopping_or_full(gui):
    d, client = gui
    d._stopping = True
    r = client.post("/api/speak", json={"text": "hi", "profile": "nova"}, headers=H)
    assert r.status_code == 503 and "stopping" in r.json()["error"]
    assert d.events.count() == 0
    d._stopping = False
    for n in range(d.store.get().daemon.max_queue):
        d.dispatch(hook_msg(f"full-{n}"), time.time())
    r = client.post("/api/speak", json={"text": "one too many", "profile": "nova"}, headers=H)
    assert r.status_code == 503 and "busy" in r.json()["error"]


# --------------------------------------------------------------------------- #
# POST /api/events/{id}/replay                                                 #
# --------------------------------------------------------------------------- #

TWO_SAYS = dict(
    text="Hello there. こんにちは。",
    segments=[{"text": "Hello there.", "attrs": {"pace": 1.1}}, {"text": "こんにちは。", "attrs": {"lang": "ja"}}],
    parsed={"segments": [{"text": "Hello there.", "chars": 12, "attrs": {"pace": 1.1}},
                         {"text": "こんにちは。", "chars": 6, "attrs": {"lang": "ja"}}], "say_count": 2},
    turn="t1",
)


def test_replay_requeues_the_segments(gui):
    d, client = gui
    first = d.dispatch(hook_msg("r1", **TWO_SAYS), time.time())
    d.queue.pop_nowait()
    d.events.update(first["event_id"], status="spoken")
    r = client.post(f"/api/events/{first['event_id']}/replay", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["status"] == "queued" and body["event_id"] != first["event_id"]
    ev = d.events.get(body["event_id"])
    assert ev["session_id"] == "r1" and ev["profile"] == "nova" and ev["cwd"] == "/tmp/r1"
    assert ev["reason"] == f"replay of #{first['event_id']}" and ev["status"] == "queued"
    assert ev["kind"] == "test" and ev["hook_event"] == "replay"
    assert ev["payload"] == {"replay_of": first["event_id"]}
    assert ev["text"] == "Hello there. こんにちは。"
    item = d.queue._items[-1]
    assert item.event_id == body["event_id"]
    assert item.segments == [{"text": "Hello there.", "attrs": {"pace": 1.1}},
                             {"text": "こんにちは。", "attrs": {"lang": "ja"}}]
    # The original row is untouched.
    assert d.events.get(first["event_id"])["status"] == "spoken"
    assert client.post(f"/api/events/{first['event_id']}/replay").status_code == 403  # no header


def test_replay_single_line_and_test_events(gui):
    d, client = gui
    r = client.post("/api/speak", json={"text": "typed once", "profile": "ember", "lang": "en"}, headers=H)
    old = r.json()["event_id"]
    d.queue.pop_nowait()
    r = client.post(f"/api/events/{old}/replay", headers=H)
    item = d.queue._items[-1]
    assert item.event_id == r.json()["event_id"] and item.session_id == "gui" and item.profile == "ember"
    assert item.segments == [{"text": "typed once", "attrs": {"lang": "en"}}]


def test_replay_errors(gui):
    d, client = gui
    assert client.post("/api/events/999/replay", headers=H).status_code == 404
    sys_ev = d.events.insert(kind="system", status="info", reason="daemon started")
    r = client.post(f"/api/events/{sys_ev['id']}/replay", headers=H)
    assert r.status_code == 400 and "no spoken text" in r.json()["error"]
    blank = d.events.insert(kind="hook", status="skipped", reason="no <say> tag", text="  ", session_id="x")
    assert client.post(f"/api/events/{blank['id']}/replay", headers=H).status_code == 400
    ok = d.dispatch(hook_msg("r2", text="again"), time.time())
    d._stopping = True
    r = client.post(f"/api/events/{ok['event_id']}/replay", headers=H)
    assert r.status_code == 503
    d._stopping = False
    assert client.post(f"/api/events/{ok['event_id']}/replay", headers=H).status_code == 200


def test_replay_does_not_supersede_waiting_lines(gui):
    d, client = gui
    old = d.dispatch(hook_msg("r3", text="an older line"), time.time())
    d.queue.pop_nowait()
    fresh = d.dispatch(hook_msg("r3", text="a new reply", turn="t9"), time.time())
    r = client.post(f"/api/events/{old['event_id']}/replay", headers=H)
    assert pending(d) == [fresh["event_id"], r.json()["event_id"]]
    assert d.events.get(fresh["event_id"])["status"] == "queued"


def test_replay_honours_mute(gui):
    d, client = gui
    old = d.dispatch(hook_msg("r4", text="hello"), time.time())
    client.post("/api/sessions/r4", json={"muted": True}, headers=H)
    r = client.post(f"/api/events/{old['event_id']}/replay", headers=H)
    assert r.status_code == 200
    assert r.json()["status"] == "skipped" and r.json()["reason"] == "session muted"
    assert d.events.get(r.json()["event_id"])["reason"] == "session muted"


def test_replay_says_from_previews():
    long_first = "A" + " word" * 60  # longer than the 200-character preview
    spoken_first = long_first[:150]
    ev = {
        "text": f"{spoken_first} Then. Short one.",
        "attrs": {"mood": "dry"},
        "parsed": {"segments": [
            {"text": long_first[:200], "chars": len(long_first), "attrs": {"mood": "dry"}},
            {"text": "Dropped early.", "chars": 14, "attrs": {}},
            {"text": "Then.", "chars": 5, "attrs": {"lang": "en"}},
            {"text": "Short one.", "chars": 10, "attrs": {"pace": 9}},
        ]},
    }
    says = replay_says(ev)
    assert [s["text"] for s in says] == [spoken_first, "Then.", "Short one."]
    assert says[0]["attrs"] == {"mood": "dry"} and says[1]["attrs"] == {"lang": "en"}
    assert says[2]["attrs"]["pace"] <= 2  # cleaned like any <say> attrs
    # A later block's words inside an earlier whole block are not its start.
    ev = {"text": "Done. Yes. Yes.", "parsed": {"segments": [
        {"text": "Done. Yes.", "chars": 10, "attrs": {}}, {"text": "Yes.", "chars": 4, "attrs": {"lang": "en"}}]}}
    assert [s["text"] for s in replay_says(ev)] == ["Done. Yes.", "Yes."]
    # Nothing usable: one block of the whole text with the event's attrs.
    assert replay_says({"text": " plain ", "attrs": {"lang": "fr"}, "parsed": None}) == [
        {"text": "plain", "attrs": {"lang": "fr"}}]
    assert replay_says({"text": "x y", "parsed": {"segments": [{"text": "zzz", "chars": 3}, {"text": "y"}]}}) == [
        {"text": "x y", "attrs": {}}]
