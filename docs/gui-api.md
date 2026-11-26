# GUI HTTP API (daemon, http://127.0.0.1:47800)

Contract between the daemon (`afterword/daemon/web.py`) and the pages in
`afterword/ui/index.html` other than the voice lab (which is in
`docs/lab-api.md`). Same rules: POST/DELETE need the header `X-AfterWord: 1`;
errors are JSON `{"error": "..."}` with a 4xx/5xx status (except FastAPI's own
422 `{"detail": [...]}` for a malformed path or query parameter).

Existing endpoints (unchanged): `GET /api/status`, `GET /api/events`,
`GET /api/events/{id}`, `GET /api/queue`, `POST /api/queue/skip`,
`POST /api/queue/stop`, `GET /api/clips/{id}`, `GET /api/stream` (SSE).

## Status: the player

`GET /api/status` (existing) has `"player"`: `"stream"` (streaming to the
audio device), `"afplay"` (whole clips, the v1 path) or `"null"`; and
```json
"stream": {"enabled": true,     // [daemon] stream
           "active": true,      // a streaming player is in use
           "error": null}       // why streaming is not available, or why the last attempt fell back
```
`error` clears once the device opens again. A hot-reloaded `[daemon] player`
or `stream` shows here at once while nothing is playing; a line that is
playing keeps the player it started with.

`config` in `GET /api/status` also has `"early_say": true` (`[daemon] early_say`).

`config` in `GET /api/status` also has `"profile_colors"`: every profile's
`[profiles.<name>] color`, `{"nova": "teal", "ember": "#12ab9f", "echo": null}`
(null: automatic). The GUI colours each voice from it (see Voices below).

## Early speaking: event rows

Lines spoken while Claude is still writing (the `MessageDisplay` hook) are
hook events with `hook_event: "early"`, one per hook call (one or more
`<say>` blocks). They share the reply's turn with the `Stop` line, so they
queue in order. A `Stop` event whose blocks were all spoken early is
`skipped`, reason `"spoken early"`; an early event whose blocks were already
spoken in this turn is `skipped`, reason `"already spoken"`; with
`[daemon] early_say = false` the daemon skips early events,
`"early speaking off"`; an early event without a `session_id` and `turn`
is skipped, `"early without a turn"` (the hook leaves those to Stop).
`timings.early_dropped` (absent when 0) counts the
blocks left out because they were spoken early (on a Stop event) or already
spoken (on an early event).

## Event timings

`timings` on a spoken (or skipped part-way) event, besides `hook_ms`,
`hook_to_daemon_ms`, `queue_wait_ms` and `spoken_text`:
```json
{
  "stream": true,               // false: rendered whole, then played with afplay
  "first_audio_ms": 380.2,      // hook (or enqueue) to the first sample out of the device
  "first_chunk_ms": 240.1,      // render start to the first audio chunk (streaming only)
  "synth_ms": 4700.0,           // render time, all segments (a part-way render counts); null if nothing was rendered
  "audio_s": 21.4,              // rendered audio, all segments (before the pace stretch)
  "rtf": 0.22,                  // synth_ms / audio_s (null when either is 0/null)
  "synth_backend": "qwen", "model": "custom_voice", "seed": 1234, "load_ms": 0,
  "playback_rate": 1.05,        // pace (absent at 1.0)
  "play_s": 20.5,               // device start to end
  "underruns": 0,               // absent when 0: the device ran dry while the render lagged
  "segments": [                 // one per segment, in play order (streaming; also the afplay path when there are several)
    {"lang": "en", "voice": "builtin:Ryan", "model": "custom_voice", "backend": "qwen",
     "seed": 1234, "audio_s": 21.4, "synth_ms": 4700.0}   // + "cancelled": true if cut short (synth_ms: time until then)
  ]
}
```
A segment is one `<say>` block, or one language's part of it: for a profile
with `[profiles.x.langs.*]`, a block without `lang` is split by script and each
part is rendered with its language's voice (`voice` above is that voice).
On the afplay path, several segments are rendered one by one and joined into
one clip with the same 250 ms gaps; if their paces differ, each is
time-stretched into the clip and `playback_rate` is absent (1.0).
The clip (`GET /api/clips/{id}`) is the rendered audio before the pace
stretch, at 24 kHz, so play it at `playback_rate`; when the segments' paces
differ (streaming too) it holds the audio as heard, stretched per segment, and
`playback_rate` is absent. A line skipped while it was
still rendering keeps a clip of the part that was rendered. If the device
fails or stalls after audio was heard, the render is stopped too and the event
is `error` (`"audio device: ..."`) with the clip rendered so far.

## Live updates (SSE, `GET /api/stream`)

Event types: `event` (an event row changed; summary without payload),
`queue` (queue snapshot), and new:

- `sessions`: `{"session_id": "...", "session": {...session object...}}` when a
  session's state changes (voice on/off/profile, mute), from the hook, the GUI
  or the `/voice` command.
- `mute`: `{"muted_all": true}` when mute-all changes.
- `explore`: the voice lab's seed explorer, `{"type": "take", "take": {...}}`
  and `{"type": "state", "state": {...}}` (contract in docs/lab-api.md,
  "Seed explorer").

## Sessions

`GET /api/sessions?limit=100`
```json
{
  "muted_all": false,
  "profiles": ["nova", "ember"],
  "voice_in_use_s": 7200,             // how recently a session counts as using its voice (common.VOICE_IN_USE_S)
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
`voice_in_use_s` is the same 2 hour window as `in_use` in `GET /api/voices`.
The Sessions page calls a session live when its voice is on, it is not muted
and its `last_seen` is less than this many seconds ago, and warns on every
live session that shares its profile with another one.

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
where item = `{"event_id", "kind", "session_id", "profile", "text", "turn", "enqueued_at"}`
(`kind`: `"speech"` or `"alert"`; alerts follow their own rules, see Alerts).
`turn` is Claude's `prompt_id` for the reply (else `"event-<id>"`, a turn of
its own). A new line from a session with the same turn as that session's
waiting lines is queued after them; a new turn supersedes them (they are
logged as `skipped`, `superseded by #<id>`). The playing line is never
superseded.

`POST /api/queue/skip` stops the current line, `POST /api/queue/stop` stops it
and clears the queue, and new:
`POST /api/queue/remove/{event_id}` drops one pending line →
`{"removed": event_id}` (404 if not pending).

## Speak and replay

`POST /api/speak` speaks any text, as `afterword test` does (the socket
`test` request): the same parse (`<say>` blocks are honoured, else the text
is cleaned), the profile's `max_chars`, mute-all, the pronunciation list and
the profile's language voices.
```json
{"text": "Deploy is done.",   // required: 1..2000 characters after trimming
 "profile": "nova",           // required: a configured profile
 "lang": "ja"}                // optional: ISO code or language name, or "auto"
```
→ `{"ok": true, "event_id": 52, "status": "queued", "position": 0}`. The row is
`kind: "test"` with `session_id: "gui"` (the Transcript shows it as from
"gui"). With `lang` every block is spoken in that language's voice
(`[profiles.x.langs.<lang>]`); without it a profile with `[langs]` splits the
line by script as usual. Lines typed this way queue after each other (one
turn), never superseding one another. When everything is muted the line is
logged and the answer is `{"ok": true, "event_id": 53, "status": "skipped",
"reason": "all muted"}`. 400 for empty or too long text, a missing or unknown
profile (the message lists the profiles) or an unknown language.

`POST /api/events/{id}/replay` (no body) queues a past event's spoken text
again, as a new event: the same profile, session and folder; its `<say>`
blocks again one by one with their attrs (`lang`, `mood`, `pace`) when it had
several (found from `parsed.segments`), else the text as one block. The new
row is `kind: "test"`, `hook_event: "replay"`, `payload: {"replay_of": id}`
and, while it waits, `reason: "replay of #<id>"` (the reason then follows its
status, as for any line). It queues after that session's waiting lines
instead of superseding them, and is skipped (`session muted`, `all muted`)
like any line of that session. → the same answer as `/api/speak`. 404 for an
unknown event; 400 when the event has no spoken text (a system row, a skipped
row without `<say>`).

Both answer 503 when the daemon cannot queue the line: it is stopping, or the
queue is full (`daemon busy`; that row is logged as `skipped`).

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
default, from the last `keep_days` days, 7 by default), newest first. `?download=1` serves the WAV as an attachment
(`afterword-41.wav`). Play at `playback_rate` to hear it as it was spoken.

## Export

`GET /api/export?session_id=<id>&format=md|txt&limit=1000` downloads the
spoken lines (status `spoken`, newest last) as an attachment
(`Content-Disposition: attachment; filename="afterword-<session|all>-transcript.md"`).
`session_id` is optional (all sessions when absent); `format` defaults to `md`;
`limit` (1 to 5000, default 1000) keeps the most recent lines; out-of-range
values give 422.

- `md`: a `# AfterWord transcript` title, then one paragraph per line,
  `**nova** · 2026-11-25 14:02 — text`. Without `session_id` the lines are
  grouped under a `## folder (session)` heading per session.
- `txt`: one line each, `[2026-11-25 14:02] nova: text`.

Times are the daemon's local time. A plain GET, so no `X-AfterWord` header.

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
    "first_chunk_ms":    {"n": 80, "p50": 240.0, "p95": 300.0, "max": 900.0},
    "first_audio_ms":    {"n": 80, "p50": 400.0, "p95": 700.0, "max": 2400.0}
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

## Daemon

The Config page's first section. launchctl is run in a thread, and its
answers are reused for 3 s, so a polling page does not run it every time;
a POST asks afresh.

`GET /api/service`
```json
{
  "label": "io.github.tonyxtyan.afterword",
  "installed": true,            // the plist exists
  "plist": "/Users/me/Library/LaunchAgents/io.github.tonyxtyan.afterword.plist",
  "loaded": true,               // null: launchctl gave no usable answer
  "running": true,
  "state": "running",           // as `launchctl print` says it
  "service_pid": 4321,          // launchd's process for the agent (null when not running)
  "last_exit": "75: EX_TEMPFAIL",  // or "0", "(never exited)", "signal Killed: 9", null
  "error": null,                // why launchctl gave no answer
  "login": true,                // start at login (not `launchctl disable`d); null: unknown
  "summary": "loaded, running pid 4321, last exit 0, start at login: on",  // `afterword status`'s service line
  "mode": "service",            // "service": this daemon is launchd's (or AFTERWORD_SERVICE=1);
                                // "manual": started by hand; "unknown": launchctl gave no answer
  "service_home": "/Users/me/.afterword",  // the home the installed plist runs (null if none)
  "other_home": false,          // the installed service runs a different home than this daemon
  "install_command": "afterword install-service",
  "restart_exit": 75,           // the exit status of a restart from the GUI
  "logs": {"daemon": "/Users/me/.afterword/afterwordd.log", "service": "/Users/me/.afterword/service.log"},
  "version": "0.3.4",
  "pid": 4321,                  // this daemon
  "started_at": 1795480000.0,
  "uptime_s": 11520.4,
  "home": "/Users/me/.afterword",
  "storage": {
    "db_path": "/Users/me/.afterword/events.db",
    "db_bytes": 1258291,        // with its -wal and -shm files
    "events": 3400,
    "clips_dir": "/Users/me/.afterword/clips",
    "clips": 12, "clips_bytes": 3565158,
    "keep_days": 7, "history": 50,   // [daemon] in config.toml (read-only here)
    "config": "/Users/me/.afterword/config.toml"
  }
}
```

`POST /api/service` `{"login": true|false}` runs `launchctl enable|disable
gui/<uid>/io.github.tonyxtyan.afterword` and answers with the shape above.
It is kept across reboots. `false` does not stop or unload the running
daemon; launchd just does not load the agent at the next login. 400 for
another body; 409 when the service is not installed (`afterword
install-service` first) or the installed plist runs another AfterWord home
than this daemon's; 502 with launchctl's message when it fails.

`POST /api/service/restart` (no body): under launchd (`mode` "service") it
answers 202 `{"restarting": true, "pid": 4321, "exit_code": 75}`, then
shuts down as for SIGTERM and exits 75 (EX_TEMPFAIL). KeepAlive
(`SuccessfulExit` false) makes launchd start it again, no sooner than 10 s
(ThrottleInterval) after its last launch. The page waits until `GET
/api/status` answers with another `pid`, for up to 45 s. 409 `"started by
hand: restart it in its terminal, or run afterword install-service"` when
`mode` is "manual", 409 when it is "unknown", 503 while the daemon is
already stopping. A SIGTERM stop (`afterword stop`, Ctrl-C) still exits 0.

`POST /api/service/prune` (no body) deletes transcript lines older than
`[daemon] keep_days` now, with their clips (the daemon also does it every
hour; 0 days keeps everything): `{"pruned": 3, "storage": {…as above…}}`.

`POST /api/service/reveal` (no body) runs `open -R <home>`: AfterWord's home
selected in a Finder window. The path is always the daemon's home, never one
from the request. `{"revealed": "/Users/me/.afterword"}`; 502 if `open` fails.

`GET /api/logs/{name}?lines=200`: the last lines of a log. `name` is
`daemon` (afterwordd.log) or `service` (service.log); anything else is 404.
`lines` is capped at 1000 (default 200) and at most the last 512 KB of the
file is read; a symlink is not followed (500).
```json
{"name": "daemon", "path": "/Users/me/.afterword/afterwordd.log", "exists": true,
 "size": 1048576, "lines": ["2026-11-26 17:42:01,790 INFO …", "…"],
 "truncated": true}             // more before the first line returned
```
A missing log is `{"exists": false, "size": 0, "lines": [], "truncated": false}`.

## Updates

The Daemon section's Updates part (afterword/update.py; `afterword update`
uses the same endpoints). `GET /api/status` carries the same object as
`update`.

`GET /api/update`
```json
{
  "install": "managed",         // "managed": runs from ~/.afterword/app/releases/<tag>; "dev": a checkout
  "service": true,              // launchd started this daemon (AFTERWORD_SERVICE=1)
  "mode": "auto",               // [update] mode: "auto" | "notify" | "off"
  "check_every_h": 6.0,
  "repo": "https://github.com/TonyXTYan/AfterWord.git",
  "running": "0.3.6",           // this daemon's version
  "current": "v0.3.6",          // what app/current points at (null without a managed install)
  "previous": "v0.3.5",         // kept for rollback
  "latest": "v0.3.7",           // the newest release tag at the last check (null: none yet)
  "available": true,            // latest is newer than running
  "last_check": 1795480000.0,
  "next_check": 1795501600.0,   // null until the daemon's loop has started
  "last_result": "v0.3.7 is available (running 0.3.6)",
  "last_error": null,           // why the last check or update failed
  "last_result_at": 1795480000.0,
  "phase": "idle",              // "idle" | "checking" | "preparing" | "waiting" (for nothing to be said)
                                // | "switching" | "restarting"
  "phase_tag": null,            // the tag being prepared or switched to
  "pending": false,             // switched, not yet confirmed (after 60 s of serving)
  "attempts": 0,                // starts of a pending release so far (rolled back after 3)
  "switched_at": 1795400000.0,
  "confirmed_at": 1795400070.0,
  "rolled_back": null,          // a tag that was rolled back; not installed again automatically
  "rollback_reason": null,
  "prepared": ["v0.3.5", "v0.3.6"],   // release dirs that passed the smoke test
  "notes": "- …",               // the available release's CHANGELOG.md section (null if none)
  "notes_tag": "v0.3.7",
  "plugins": {                  // the last plugin refresh, after a confirmed update (null: none yet)
    "at": 1795400071.0, "version": "0.3.6",
    "claude": {"ok": true, "message": "updated; restart open Claude Code sessions to load the new hooks"},
    "codex": {"ok": null, "message": "codex is not on PATH; skipped"}    // ok null: skipped; false: failed
  },
  "apply_blocked": null         // why this daemon cannot install updates (a dev checkout, started by hand)
}
```

`POST /api/update/check` (no body) runs `git ls-remote --tags <repo> 'v*'`
now (and fetches the newest tag's CHANGELOG.md when it is newer) and answers
with the shape above; a failed check is `last_error`, still 200. While a
check or update is already running it answers at once with the current state.

`POST /api/update/apply` `{"force": false, "wait_s": 600}` (both optional):
"Update now". It checks, prepares the newest release (worktree, `uv sync
--frozen`, smoke test), waits up to `wait_s` seconds (default 600) for
nothing to be queued, said or rendered (`force`: no wait), switches and
restarts. It answers 202 `{"started": true, "tag": "v0.3.7", "pid": 4321,
"update": {…}}` at once (`tag` is null when no check has run yet) and works
in the background: follow `phase` in `GET /api/status` until `pid` changes
(updated) or `phase` is "idle" again (stopped: `last_result`, `last_error`).
It works in any `mode`, and also retries a rolled-back tag. 409 when this
daemon cannot apply updates (`apply_blocked`), when the newest known release
is not newer, or while an update step is running; 400 for a bad body; 503
while the daemon is stopping. All POSTs need `X-AfterWord: 1`.

## Voices

`GET /api/voices` → every voice profile with its usage, in config order (the
same order as `/api/status` `config.profiles`):
```json
{"profiles": [{"name": "nova", "description": "calm narrator", "voice": "say:Samantha",
  "lang": "en", "style": "…", "persona": "", "pace": 1.0, "langs": {}, "color": "teal",
  "last_used": 1795597634.0, "last_session": "abc", "last_cwd": "/path",
  "in_use": [{"session_id": "abc", "cwd": "/path", "ts": 1795597634.0}],
  "stats": {"lines_7d": 38, "audio_s_7d": 712.4, "synth_ms_avg_7d": 912.0}}]}
```
The usage fields come from `~/.afterword/voices.json` (written by the hooks
and the daemon): `last_used`, `last_session` and `last_cwd` are null for a
voice nobody has used. `in_use` lists the sessions that chose or spoke with
the voice within the last 2 hours (`common.VOICE_IN_USE_S`), newest first.
`stats` covers the last 7 days of spoken events with that profile, from the
events DB: `lines_7d` (count), `audio_s_7d` (summed `audio_s`) and
`synth_ms_avg_7d` (mean `synth_ms`, null when nothing was timed). A voice not
used that week reports `0`, `0.0`, `null`. Only `GET /api/voices` carries it.
Each profile also carries the other lab fields (`ref_text`, `max_chars`,
`say_voice`, `seed`, `temperature`).
`color` is the profile's GUI colour: one of the 64 palette names or
`"#rrggbb"`, both lower case, or null when none is set. The palette names
are 16 families (`yellow`, `cyan`, `green`, `magenta`, `white`, `orange`,
`blue`, `red`, `violet`, `lime`, `pink`, `brown`, `teal`, `grey`, `olive`,
`sky`) and each family with `-deep`, `-light` or `-pale` (`teal-deep`), in
that order (`config.SPEAKER_COLORS`). The GUI gives a voice without one the
first palette colours no explicit choice uses (every family, then the deep,
light and pale rows), in `config.profiles` order, wrapping when there are
more such voices than free colours. A bad
value in config.toml is a config warning and counts as unset.

`PATCH /api/lab/profiles/<name>` with `{"description": "text"}` and/or
`{"color": "teal"}` (a palette name, `"#RRGGBB"`, or `""`) sets the
profile's one-line description (trimmed, at most 300 characters; `""` removes
it) and its colour (either case, stored lower case; `""` removes it, so the
GUI picks one), and returns `{"ok": true, "profile": {…one item of GET
/api/voices…}}`. Only those keys of `[profiles.<name>]` are written, in one
write, so comments and the rest of the table stay as they are; the daemon
then reloads the config. 404 for an unknown profile; 400 when neither key is
sent, for a non-string or too long description, or for a bad colour (nothing
is written then); 403 without the `X-AfterWord: 1` header. A save from the
lab (`POST /api/lab/profiles`) keeps the colour.

`POST /api/lab/profiles` (save from the lab) takes the same optional
`description` (at most 300 characters). When it is present it is written
(`""` removes it); when it is left out, a description already in the file is
kept, so a save from the lab never deletes it.

## Pronunciations

`GET /api/pronounce`
```json
{
  "path": "/Users/me/.afterword/config.toml",
  "global": {"SQLite": "sequel lite", "uv": "you vee"},      // [pronounce]
  "profiles": {"nova": {}, "tutor": {"GIF": "jif"}},          // [profiles.x.pronounce], every profile
  "default_profile": "nova",        // the first profile (what an unknown profile borrows); null if none
  "error": null,                    // config.toml parse error, as in /api/config
  "warnings": ["pronounce.x: expected a string, got 1"],     // pronounce-related config warnings
  "limits": {"entries": 500, "key": 100, "value": 300}
}
```
Values are the validated tables the daemon is using (invalid entries are left
out and reported in `warnings`; the Config page lists them).

`POST /api/pronounce` replaces one table, either
```json
{"global": {"SQLite": "sequel lite"}}
```
or
```json
{"profile": "tutor", "entries": {"GIF": "jif"}}     // "profile": null = the global table
```
and returns the same shape as GET (after the hot reload). The file is edited
in place with tomlkit: comments and the rest of the file are kept, kept keys
keep their place, removed keys are deleted, new keys are added at the end of
the table, entries GET left out (invalid ones, or past the 500-entry limit)
are kept as they are unless the body names the same key (which replaces it), and a missing table is created (`[pronounce]` at the end of the
file, `[profiles.x.pronounce]` under its profile). The table itself is never
removed, so comments inside it stay. The result is re-read with tomllib and
`build_config` before it is written. 400 for a non-string value, an empty
key, a key over 100 or a value over 300 characters, more than 500 entries, a
bad body (including `"profile": ""`), or a `config.toml` that does not
parse; 404 for an unknown profile. Every `config.toml` write (this one and
the lab's profile save and delete) holds one lock, so concurrent saves
cannot undo each other.

Applied events: the daemon rewrites the text right before synthesis (after
the session-name prefix). `events.text` keeps the original; when the spoken
text differs from it, `timings.spoken_text` holds what was spoken.

## Alerts

Spoken alerts (design: `docs/design-v2.md` C). `config` in `GET /api/status`
has `"alerts": true|false` (`[alerts] enabled`).

Event rows. An alert that is spoken (or skipped) is an event with
`kind: "alert"` (not `"hook"`), so `/api/stats` and the session counts leave
it out. `hook_event` is the hook it came from (`"PermissionRequest"`,
`"Notification"` or `"Stop"` for "done"); `text` is the phrase as filled in;
`profile` the profile that speaks it; `attrs` is `{}` or `{"lang": "en"}`
when the phrase is from another language's pool than the profile's; and
`parsed.alert` describes it:
```json
{"event": "permission",           // permission | input | idle | done
 "trigger": "notification",       // request (PermissionRequest) | notification | stop
 "action": "Remove node_modules directory", "tool": "Bash",
 "notification_type": "permission_prompt", "message": "Claude needs your permission to use Bash",
 "turn_s": 75.2, "minutes": 1,     // "done" only; minutes only for turn_s >= 60
 "template": "{project} needs your OK to {action}.", "lang": "en"}
```
`action`/`tool` are present only when the hook could tell which permission
request the prompt is about (otherwise phrases with `{action}`/`{tool}` are
skipped in the pick). Statuses as for speech, plus `skipped` with reason `"session muted"` /
`"all muted"`, `"superseded by #<id>"` (a newer alert of the same session),
or `"alert dropped: waited N s in the queue"` (more than 30 s). A
PermissionRequest or Notification call that is not spoken is a `kind: "hook"`
`info` row with the reason (`"alert permission: alerts off"`, `"alert idle:
not in [alerts] events"`, `"permission pending: spoken if the prompt is still
waiting after ~6 s"`, `"permission prompt still waiting (already spoken when
it was asked)"`, `"notification auth_success: not an alert"`). A "done" alert
is a second row next to its Stop row. Queue items (`/api/queue`, SSE `queue`)
carry `"kind": "speech" | "alert"`.

`GET /api/alerts`
```json
{
  "settings": {"enabled": true, "profile": "", "events": ["permission", "input", "done"],
               "done_after_s": 60.0, "permission_when": "away"},     // [alerts] as validated
  "events": ["permission", "input", "idle", "done"],
  "placeholders": {"permission": ["project", "action", "tool"], "input": ["project"],
                   "idle": ["project"], "done": ["project", "minutes"]},
  "limits": {"phrase": 120, "pool": 60, "count": 30},
  "profiles": [{"name": "nova", "lang": "en", "pool_lang": "en", "style": "…", "persona": ""}],
  "default_profile": "nova",       // [alerts] profile if valid, else the first profile
  "pools": {"en": {"permission": ["…"], …}, "zh": {…}},  // in use: alerts.json, else built-in, per event
  "custom": {"zh": ["done", "idle"]},  // which events alerts.json sets, per language
  "builtin_langs": ["en", "zh"],
  "path": "/Users/me/.afterword/alerts.json",
  "error": null,                   // alerts.json unreadable: the last good pools stay in use
  "warnings": ["alerts.json.en.idle[2]: unknown placeholder {who}"],   // also [alerts] config warnings
  "generating": false
}
```

`POST /api/alerts` sets any of `enabled` (bool), `events` (list of the four),
`profile` (a profile name or `""`), `done_after_s` (0–86400),
`permission_when` (`"away"` | `"now"`) in `[alerts]` of config.toml (tomlkit,
comments kept, the table is added at the end when missing, the result is
read back) and returns the GET shape. 400 for a bad value or an unknown
profile.

`POST /api/alerts/phrases` `{"lang": "zh", "pools": {"idle": ["{project} 在等你。"], …}}`
replaces that language's pools in alerts.json (an event left out or empty
uses the built-in pool); `"pools": null` removes the language (built-in
again). Each phrase: a string of at most 120 characters, one line, only the
placeholders of its event. 400 with the first problem
(`"idle phrase 2: {tool} is not available for idle (use {project})"`).
Returns the GET shape.

`POST /api/alerts/generate` `{"profile": "mei", "count": 12, "persona": "…"}`
(all optional: the default profile, 12 per event, the profile's `persona`)
runs `claude -p --model haiku --output-format json --tools "" --no-session-persistence`
with a prompt built from the profile (name, language, style, persona), with
`AFTERWORD_DISABLE=1` so AfterWord's own hooks stay out of it; 180 s limit.
Returns
```json
{"profile": "mei", "lang": "zh", "count": 12, "persona": "…",
 "added": {"permission": ["…"], "input": [], …},   // new phrases merged into alerts.json
 "rejected": ["permission: '…' ({minutes} is not available for permission …)"],
 "pools": {"permission": [...], …}}                // the language's pools now
```
One run at a time (409 while one runs); 400 for a bad argument (also a
`profile` that is not a string), 502 when claude fails or its reply is
unusable (nothing is written then). Stopping the daemon kills a run in
progress (502 `"cancelled: the daemon is shutting down"`, nothing written).

`POST /api/alerts/hear` `{"event": "done", "profile"?: "mei", "phrase"?: "…", "lang"?: "zh"}`
renders one alert as a voice lab preview (it never goes through the queue):
`phrase` (checked like a saved one) or a random phrase of the profile's pool,
with sample values (`my-project`, "run the test suite", `Bash`, 3). `lang` is
the pool the phrase was written for (default: the profile's). Returns the
lab preview response (`url`, `pace`, … as in lab-api.md) plus `phrase`,
`alert_text`, `event`, `alert_profile`.
