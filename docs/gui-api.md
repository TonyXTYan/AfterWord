# GUI HTTP API (daemon, http://127.0.0.1:47800)

Contract between the daemon (`afterword/daemon/web.py`) and the pages in
`afterword/ui/index.html` other than the voice lab (which is in
`docs/lab-api.md`). Same rules: POST/DELETE need the header `X-AfterWord: 1`;
errors are JSON `{"error": "..."}` with a 4xx/5xx status (except FastAPI's own
422 `{"detail": [...]}` for a malformed path or query parameter).

Existing endpoints (unchanged): `GET /api/status`, `GET /api/events`,
`GET /api/events/{id}`, `GET /api/queue`, `POST /api/queue/skip`,
`POST /api/queue/stop`, `GET /api/clips/{id}`, `GET /api/stream` (SSE).

## Live updates (SSE, `GET /api/stream`)

Event types: `event` (an event row changed; summary without payload),
`queue` (queue snapshot), and new:

- `sessions`: `{"session_id": "...", "session": {...session object...}}` when a
  session's state changes (voice on/off/profile, mute), from the hook, the GUI
  or the `/voice` command.
- `mute`: `{"muted_all": true}` when mute-all changes.

## Sessions

`GET /api/sessions?limit=100`
```json
{
  "muted_all": false,
  "profiles": ["nova", "ember"],
  "sessions": [
    {
      "session_id": "5faab7a2-…",
      "cwd": "/Users/me/project",
      "profile": "nova",              // null = voice off
      "muted": false,                 // this session's mute
      "source": "voice-tag",          // what last set the voice: env | voice-tag | voice-command | gui
      "updated": 1795480000.0,        // state change time (null if never set)
      "last_seen": 1795480100.0,      // newest event time (or updated)
      "events": 12,                   // events logged for this session
      "spoken": 5,                    // of which spoken
      "last_event": {"id": 41, "ts": 1795480100.0, "status": "spoken", "reason": null, "text": "…"}
    }
  ]
}
```
Sessions come from state files and from sessions seen in the event log, newest
activity first. Sessions with voice off that never set a voice still appear
(from their events).

`POST /api/sessions/{session_id}` with any of:
```json
{"profile": "ember"}     // turn voice on with this profile (must exist)
{"profile": null}        // turn voice off
{"muted": true}          // mute / unmute this session (voice stays on)
```
→ `{"session": {...}}`. Muting a session, or turning its voice off, also drops
its waiting lines and stops its current one. 400 for an unknown profile (message lists the
profiles) or a bad session id.

Mute means "keep the voice on but play nothing": lines are still logged, with
status `skipped` and reason `session muted` (or `all muted`).

`POST /api/mute` `{"muted": true|false}` → `{"muted_all": bool}`. Mute-all
persists across daemon restarts.

## Queue

`GET /api/queue` → `{"now_playing": item|null, "pending": [item…], "max_size": 16}`
where item = `{"event_id", "session_id", "profile", "text", "enqueued_at"}`.

`POST /api/queue/skip` stops the current line, `POST /api/queue/stop` stops it
and clears the queue, and new:
`POST /api/queue/remove/{event_id}` drops one pending line →
`{"removed": event_id}` (404 if not pending).

## History

`GET /api/history?limit=50&before=<id>`
```json
{"items": [
  {"id": 41, "ts": 1795480100.0, "session_id": "…", "cwd": "/…", "profile": "nova",
   "text": "…", "status": "spoken", "audio_s": 3.2, "playback_rate": 1.05,
   "synth_backend": "qwen", "url": "/api/clips/41", "download_url": "/api/clips/41?download=1"}
]}
```
Lines whose audio clip is still on disk (the last `history` clips, 50 by
default), newest first. `?download=1` serves the WAV as an attachment
(`afterword-41.wav`). Play at `playback_rate` to hear it as it was spoken.

## Timings

`GET /api/stats?limit=200` (over the newest `limit` hook/test events, not
counting the `info` rows that UserPromptSubmit and SessionStart log)
```json
{
  "count": 120,
  "by_status": {"spoken": 80, "skipped": 38, "error": 2},
  "by_reason": {"session off": 30, "no <say> tag": 8},
  "timings": {
    "hook_ms":           {"n": 80, "p50": 16.0, "p95": 22.1, "max": 30.2},
    "hook_to_daemon_ms": {"n": 80, "p50": 17.0, "p95": 24.0, "max": 31.0},
    "synth_ms":          {"n": 80, "p50": 900.0, "p95": 1600.0, "max": 2100.0},
    "rtf":               {"n": 80, "p50": 0.24, "p95": 0.31, "max": 0.6},
    "first_audio_ms":    {"n": 80, "p50": 950.0, "p95": 1700.0, "max": 2400.0}
  }
}
```

## Config

`GET /api/config`
```json
{
  "path": "/Users/me/.afterword/config.toml",
  "text": "# AfterWord config…",     // the raw file ("" if missing)
  "error": null,                     // parse error: the daemon keeps the last good config
  "warnings": ["profiles.x.pace: …"],
  "loaded_at": 1795480000.0,
  "synth": "qwen",                   // in use
  "configured_synth": "qwen",
  "engine": { …same as catalog.engine in lab-api.md… }
}
```
`POST /api/config/reload` forces a reload and returns the same shape.
