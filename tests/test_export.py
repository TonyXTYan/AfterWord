"""GET /api/export and the per-voice stats of GET /api/voices."""
import time

from test_gui_api import gui, hook_msg  # noqa: F401


def speak(d, sid, text, profile="nova", ts=None, timings=None):
    d.events.insert(kind="hook", session_id=sid, cwd=f"/w/{sid}", profile=profile, status="spoken",
                    text=text, ts=ts or time.time(), timings=timings or {})


def test_export_txt_and_md_single_session(gui):
    d, client = gui
    speak(d, "s1", "first")
    speak(d, "s1", "second", profile="ember")
    speak(d, "s2", "other")
    d.events.insert(kind="hook", session_id="s1", cwd="/w/s1", profile="nova", status="skipped", text="nope")
    r = client.get("/api/export?session_id=s1&format=txt")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    lines = r.text.splitlines()
    assert len(lines) == 2 and lines[0].endswith("] nova: first") and lines[1].endswith("] ember: second")
    md = client.get("/api/export?session_id=s1&format=md")
    assert md.text.startswith("# AfterWord transcript") and "**nova** · " in md.text and " — first" in md.text
    assert "other" not in md.text and "nope" not in md.text and "## " not in md.text


def test_export_all_groups_by_folder_and_limits(gui):
    d, client = gui
    for i in range(5):
        speak(d, "s1" if i % 2 else "s2", f"line {i}")
    md = client.get("/api/export").text
    assert "## s1 (s1)" in md and "## s2 (s2)" in md
    txt = client.get("/api/export?format=txt&limit=2").text.splitlines()
    assert [l.split(": ", 1)[1] for l in txt] == ["line 3", "line 4"]
    assert client.get("/api/export?limit=0").status_code == 422
    assert client.get("/api/export?limit=5001").status_code == 422
    assert client.get("/api/export?format=pdf").status_code == 422


def test_voice_stats_last_seven_days(gui):
    d, client = gui
    speak(d, "s1", "a", timings={"audio_s": 2.0, "synth_ms": 800})
    speak(d, "s1", "b", timings={"audio_s": 3.0, "synth_ms": 1000})
    speak(d, "s1", "old", ts=time.time() - 9 * 86400, timings={"audio_s": 50, "synth_ms": 1})
    profiles = {p["name"]: p for p in client.get("/api/voices").json()["profiles"]}
    assert profiles["nova"]["stats"] == {"lines_7d": 2, "audio_s_7d": 5.0, "synth_ms_avg_7d": 900.0}
    assert profiles["ember"]["stats"] == {"lines_7d": 0, "audio_s_7d": 0.0, "synth_ms_avg_7d": None}
    assert "last_used" in profiles["nova"]
