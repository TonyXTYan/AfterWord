# Current state

As of 2026-11-25. AfterWord is a voice-out daemon for Claude Code and Codex: replies tagged with `<say>` are spoken in a per-session voice, one at a time, with a local GUI that shows what was said and why anything wasn't. It is not a context bus yet.

Test suite: 963 passed, 2 skipped, 132 s (`uv run pytest -q`), measured at commit 2fd720a. The working tree now collects 967 tests with the uncommitted `tests/test_ui_speakers.py` additions. CI runs on every push (`.github/workflows/tests.yml`).

## What has shipped

| Area | What works | Where |
|---|---|---|
| Speaking | Tag-gated speech, up to four `<say>` blocks per reply, early speech from `MessageDisplay`, streaming playback | `afterword/hooks/afterword_hook.py`, `afterword/hooks/earlysay.py`, `afterword/daemon/stream.py` |
| Queue | One global `PlaybackQueue`, one consumer. A newer turn supersedes an older queued one from the same session. Overflow past 16 is skipped as "daemon busy". | `afterword/daemon/queue.py`, `afterword/daemon/server.py` |
| Engines | Qwen3-TTS, Kokoro, Chatterbox Multilingual, Apple AVSpeech. Fallback chain: `model_fallback=apple`, then `say`. | `afterword/daemon/qwen.py`, `afterword/daemon/apple.py`, `docs/research-v3.md` |
| Voices | Per-session voice state, `voices.json` "in use" window of 2 h, `/afterword:voice pick` prefers free and least-used profiles, a voice per language, pronunciations | `afterword/hooks/common.py`, `afterword/hooks/pronounce.py` |
| Alerts | Spoken alerts for permission and notification events, with an optional Haiku-written phrase via `claude -p` | `afterword/daemon/alerts.py`, `afterword/hooks/alerting.py` |
| Explaining silence | Every hook outcome records a decision and a reason ("session off", "no <say> tag", "duplicate", "daemon unreachable", ...). The Transcript shows it per row, filters by it, and has a "By reason" table. | `afterword/hooks/afterword_hook.py`, `afterword/ui/index.html` |
| GUI | Transcript, Sessions (voice roster, colours), Voice lab with seed explorer and Engines mode, Config, subtitle bar, keyboard shortcuts, Speak and replay, export. Web Audio playback so media keys stay with the music. | `afterword/ui/index.html`, `afterword/daemon/web.py`, `docs/gui-api.md`, `docs/lab-api.md` |
| CLI and plugins | `afterword serve`, `status`, `test`, `models`, `alerts`, `install-hooks`, `voices`. Claude Code plugin and a Codex plugin. | `afterword/cli.py`, `hooks/hooks.json`, `hooks/codex-hooks.json` |
| Privacy basics | HTTP bound to 127.0.0.1, Host allow-list, POSTs need `X-AfterWord: 1`, per-session mute, clip pruning | `afterword/daemon/web.py`, `afterword/daemon/state.py` |

### Recent pull requests

| PR | What it added |
|---|---|
| #1 `claude/whats-next-7f655f` (merged) | Kokoro, Chatterbox Multilingual and Apple speech engines; language-name normalising; CI. |
| #2 `claude/macos-media-keys-audio-e217ee` (merged) | GUI v2 (Transcript, Sessions, Voice lab, Config pages), Web Audio playback, least-used voice pick, `afterword voices`, export and per-voice stats, Speak and replay, keyboard shortcuts, fix-a-word popover. |
| `claude/speaker-colours-engine-lab` (open, no PR yet) | 64 named speaker colours with a hue-by-shade picker, per-profile colour key, Voice lab Engines mode, "Open in voice lab" from the Transcript, Sessions empty-state wording. |

## What P0 still lacks

P0 "done when": parallel sessions speak in different voices, don't overlap, and the GUI explains every time a reply stayed silent.

| Clause | State | Gap |
|---|---|---|
| Don't overlap | Met | None. Covered by `tests/test_queue.py` and `tests/test_stream.py`. |
| GUI explains every silent reply | Mostly met | Reasons are free text, so the "By reason" table fragments. "Voice on, but Claude forgot the tag" looks the same as "voice off". Nothing shows when the hook never ran. |
| Different voices | Not met by default | Nothing assigns a distinct voice on `SessionStart`. Two sessions can both use `nova` with only a GUI hint. When the daemon is down, the `say` fallback uses a different voice from the profile. No test checks N sessions get N voices. |

README still says "Not built: the optional menu-bar icon", and the outlook labels P0 "in progress". Both need updating when P0 closes. The closing checklist is in [p0-voice-out.md](p0-voice-out.md).

## Tech debt that matters for later phases

| Item | Why it matters | When to fix |
|---|---|---|
| `afterword/ui/index.html` is about 5,600 lines of inline HTML, CSS and JS | Every P1 page (timeline, suggestions, metrics) lands here. 6 `tests/test_ui_*.py` files (engines, explorer, fork, keys, player, speakers) and `tests/test_gui_api.py` depend on it. | Before P1 M4 (timeline). See decision D8. |
| `afterword/daemon/server.py` (1,532 lines) mixes socket, HTTP, enqueueing and alerts | P1 needs one clean ingestion seam for bus events. | During P1 M2. |
| `afterword/hooks/afterword_hook.py` is 1,658 lines | Tool hooks will run on every tool call; they should be a small separate entry point. | P1 M3. |
| `events` table has no schema version, no migrations, no retention | `executescript(SCHEMA)` only creates if missing, so new columns silently do nothing on old databases. `events.db` grows forever. | P1 M1, M2. |
| Turn id is parsed but not stored (`server.py`, `fields`) | The timeline can't group a turn without re-parsing payload JSON. | P1 M1. |
| Raw prompts and payloads stored with only a length cap | Tool events will carry commands and output that may hold secrets. | Redaction before P1 M3. |
| No daemon service | No launchd agent, no `afterword start|stop`. When the daemon is down, voices fall back to `say`, and that is logged only after the daemon returns and imports the spool (reason text "daemon down: spoken by say fallback"). P2 and P3 need an always-on service. | P0-6. |
| No hook/daemon version handshake | A plugin update changes the hook but not a daemon running from a clone. `plugin.json` has no version. | P1 M2. |
| Hook runtime | Each hook spawns `python3` (about 20-30 ms). Hooks run on stock `python3` 3.9+, the daemon on 3.12. No per-hook p95 is measured. | Measure in P1 M3 before adding tool hooks. |
| Test runtime | 132 s serial. No fast/slow split, no `pytest-xdist`. Real-time `NullPlayer` and fixed sleeps likely dominate. | Before P1 gets busy; see decision D14. |
| Writes under one lock on the event loop (`log.py`, `server.py`) | Tool-event volume could stall playback scheduling. | P1 M3 (writer thread). |
| Swift helper binaries are hash-named and unsigned (`afterword/daemon/apple.py`) | Fine for AVSpeech. Wrong for `afterword-sense`: TCC grants would reset on every rebuild. | Start of P2. |
