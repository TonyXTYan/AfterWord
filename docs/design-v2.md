# AfterWord v2 features: design

Five features on top of v1: a pronunciation list, profiles with a voice per
language plus several `<say>` blocks per reply, streaming playback (fast first
audio) with early speaking while Claude writes, spoken alerts, and a seed
explorer in the voice lab. This file is the contract the code follows; the
"as built" notes at the end record anything that changed while building.

Rules that still hold everywhere (from DESIGN.md): the hooks never fail (exit 0,
no stdout except the documented JSON, bounded time, stdlib only, Python 3.9+);
speech is off by default and set per session; the daemon is one asyncio process;
POSTs need `X-AfterWord: 1`; config is hot-reloaded and a bad file keeps the
last good config.

Research behind these choices (Claude Code 2.1.330, mlx-audio 0.6.8):

- Hooks never see tokens. `MessageDisplay` runs "each time a batch of newly
  completed lines is ready to render" (interactive) or once per assistant
  message after it completes (`-p`/SDK), before a following tool call runs. It
  carries `turn_id`, `message_id`, `index`, `final`, `delta` (whole lines).
- `PermissionRequest` fires immediately with `tool_name` and `tool_input`
  (Bash has a human `description`). `Notification` fires with
  `notification_type` (`permission_prompt` ~6 s after a prompt is left
  waiting, `idle_prompt` ~60 s after a reply, `elicitation_dialog`,
  `agent_needs_input`, …). Notification does not fire in `-p`.
- Qwen3-TTS in mlx-audio streams for all three variants
  (`stream=True, streaming_interval=…`): first audio about 0.24 s at
  interval 1.0 against 5.3 s for a 21 s line; same seed gives the same audio;
  chunks concatenate into the same WAV. `sounddevice` (PortAudio) is already
  installed with mlx-audio and works from a background thread.
- afplay costs 0.6–1.0 s per process, so per-sentence afplay would leave gaps.

## D. Pronunciation list

Config:

```toml
[pronounce]                 # applied to every line before it is rendered
"SQLite" = "sequel lite"
"kubectl" = "cube control"
"uv" = "you vee"
"__init__" = "dunder init"

[profiles.tutor.pronounce]  # optional per-profile entries, override global ones
"GIF" = "jif"
```

- Matching: longest key first, one pass (a replacement is never re-matched).
  A key that starts and ends with a word character matches only at word
  boundaries (`(?<![\w])key(?![\w])`), so `uv` does not hit `uvicorn`. Other
  keys (`C++`, `->`, CJK) match anywhere. Smart case: an all-lowercase key
  matches case-insensitively; a key with any capital matches exactly.
- Code lives in `afterword/hooks/pronounce.py` (stdlib, 3.9) so both the hook's
  `say` fallback and the daemon use it. The hook reads the tables with the mini
  TOML parser (quoted keys, scalar strings: supported).
- Applied in the daemon right before synthesis (after the announce prefix,
  before the script split), in the lab preview (with a "skip pronunciations"
  toggle), and in the hook's `say` fallback. `events.text` keeps the original;
  the spoken text goes in `timings.spoken_text` when it differs.
- GUI: the Config page gets a Pronunciations table (add, edit, delete, "hear
  it" with the current default profile) saved through
  `GET/POST /api/pronounce` (tomlkit, comments kept, validated by
  `build_config`). Body: `{"global": {k: v}, "profile": name|null, "entries": {k: v}}`
  replaces that one table.

## B. One profile, a voice per language; several `<say>` per reply

Config:

```toml
[profiles.tutor]
voice = "clone:tutor-en"
lang = "en"                 # the primary language
pace = 0.95

[profiles.tutor.langs.zh]   # any subset of: voice, ref_text, style, pace, say_voice, seed
voice = "clone:tutor-zh"
say_voice = "Tingting"
```

- `Profile.langs: Mapping[str, LangVoice]` (frozen). A missing field inherits
  from the profile. Keys are ISO codes or Qwen names, normalised to ISO.
  `Config.voice_for(profile, lang)` returns the effective settings for one
  segment. `models_in_use`/`warm_models` include per-language voices.
- Several `<say>`: `ParseResult.says` is every non-empty `<say>` in order (at
  most 4; extra ones are dropped with a warning). `.say` stays as the last one
  for compatibility. Each is truncated to the profile's `max_chars`; the total
  is capped at 2 × `max_chars`.
- Segments: the hook sends `segments: [{"text", "attrs"}]` (plus `text` = the
  segments joined with a space, for the log, spool and `say` fallback). The
  daemon turns each `<say>` into one or more segments:
  - `lang` attr given → that language, no split.
  - no `lang` (or `auto`) and the profile has `langs` → split by script:
    Han → zh, kana → ja, Hangul → ko, everything else → the primary language.
    Digits, punctuation, spaces, and Latin runs of at most 3 words inside a CJK
    run stay with the CJK run (`用 Python 写` stays Chinese). Runs shorter
    than 2 letters merge into a neighbour. The split happens at the nearest
    sentence or clause boundary.
  - the profile has no `langs` → one segment, as today.
- The prompt note (UserPromptSubmit) for a profile with `langs`:
  `AfterWord voice is on (profile tutor: en, zh). Add <say lang="en|zh">1-2
  plain spoken sentences</say>, one per language if you like; first one at the
  start is heard sooner (optional mood="…"; no markdown inside).` Profiles
  without `langs` keep today's note but also mention the start placement. The
  hook learns the languages from `[profiles.x.langs.*]` table names (mini TOML
  handles nested tables).
- `say` fallback (daemon down): each segment is spoken in order with its
  language's `say_voice`.

## E. Streaming playback, fast first audio, early speaking

### Player

- New `StreamPlayer` (sounddevice `OutputStream`, 24 kHz mono float32, opened
  per queue item on a player thread, about 0.3 s pre-buffer). The daemon feeds
  it PCM chunks as the engine yields them. Skip/stop aborts the stream at once
  and cancels the render. `AFTERWORD_PLAYER=null` gives a `NullStreamPlayer`
  that records what it was fed (tests). If sounddevice cannot be imported or
  the device cannot be opened, the daemon uses the v1 path (render the whole
  item to a WAV, then `afplay -r`), and logs why once.
- Pace: a small streaming WSOLA time-stretch in numpy (pitch kept, 0.5–2.0)
  sits between the engine and the stream. Pace 1.0 skips it.
- Segments of one item play back to back in the same stream, with 250 ms of
  silence between segments and the ~70 ms leading silence of each later
  segment trimmed. `say`-rendered segments (fallback) are decoded with
  miniaudio, resampled to 24 kHz, and fed the same way.
- The engine gets priority classes: live speech jobs run before lab jobs
  (a priority queue keyed by class, then arrival). A job carries a cancel
  event checked at every chunk. Streaming uses `streaming_interval=1.0`.
- History: the chunks of all segments, as played (before time-stretch, so
  replay at `playback_rate` sounds the same), are written to
  `clips/{event_id}.wav` when the item finishes or is skipped part-way
  (then the clip is the part that was rendered).
- Timings: `first_audio_ms` (hook to first sample out), `first_chunk_ms`
  (render start to first chunk), `synth_ms` (sum), `audio_s` (sum),
  `rtf`, `segments` (list of `{lang, voice, model, backend, seed, audio_s,
  synth_ms}`), `stream: true|false`.

### Turns and the queue

- Queue items carry `turn` (Claude's `prompt_id`, else the event id). A new
  item from the same session and the same turn is appended after that
  session's items; a new turn from the same session supersedes that
  session's waiting items from older turns (today's rule). Alerts are their
  own kind (see C).

### Early speaking (MessageDisplay)

- A new `message-display` hook (async, so it never holds the screen) reads the
  `delta`. Fast path: exit at once unless the session's voice is on (state
  file) and the text seen so far for this message contains `<say`. It keeps
  the text seen so far per `message_id` in `~/.afterword/stream/<session>.json`
  (small, pruned after 10 minutes), parses it with the normal parser (code
  blocks masked), and sends each newly completed `<say>` block to the daemon
  at once as `event: "early"`, with `turn` and an `early_key` =
  `sha1(message_id + index of the say in that message + text)`.
- The Stop hook parses the whole reply, computes the same keys, and sends only
  the `<say>` blocks not already sent early (it reads the stream file; the
  daemon also refuses duplicate `early_key`s for a turn). If every block was
  spoken early, the Stop event is logged as `skipped`, reason
  `spoken early`.
- `[daemon] early_say = true` (default) turns it off when false. The hook
  never blocks: on any doubt it does nothing and Stop speaks the line as in v1.

## C. Spoken alerts

Config (off by default):

```toml
[alerts]
enabled = false
profile = ""                 # "" = the session's profile if its voice is on, else the first profile
events = ["permission", "input", "done"]   # also "idle"
done_after_s = 60            # "done": a turn this long finished with nothing spoken
permission_when = "away"     # "away" = after Claude Code's ~6 s prompt timer; "now" = immediately
```

- Events:
  - `permission`: `PermissionRequest` stores the pending tool and a short
    description (`tool_input.description`, else the tool name and the first
    words of its input) in the session state. With `permission_when = "now"`
    it speaks at once; with `"away"` the `Notification`
    (`permission_prompt`) speaks it, so a prompt you answer within ~6 s stays
    quiet.
  - `input`: `Notification` with `elicitation_dialog` or `agent_needs_input`.
  - `idle`: `Notification` with `idle_prompt`.
  - `done`: the Stop hook, when the turn (UserPromptSubmit to Stop, stored in
    the session state) took at least `done_after_s` and nothing was spoken for
    it.
- Alerts speak even when the session's voice is off (that is their point), but
  not when the session or everything is muted. They are queue items of kind
  `alert`: they never supersede speech, they supersede the same session's
  older waiting alerts, and they are dropped if they wait more than 30 s.
- Phrases: `~/.afterword/alerts.json` holds pools per language and event:
  `{"en": {"permission": ["{project} needs your OK to {action}.", …], …}, "zh": {…}}`.
  Placeholders: `{project}` (the folder name of `cwd`), `{action}` (the tool
  description), `{tool}`, `{minutes}`. Built-in pools (a few per event, English
  and Chinese) are used when the file is missing. The daemon picks at random,
  never one of the last 3 used for that event.
- "LLM random": `afterword alerts generate [--profile NAME] [--count 12]` and a
  "Generate phrases" button on the Config page run
  `claude -p --model haiku --output-format json` with a prompt built from the
  profile (its name, language, style, and the persona note below) and ask for
  JSON pools; the result is validated (strings ≤ 120 chars, only known
  placeholders) and merged into `alerts.json` for that language. A profile can
  carry `persona = "…"` (free text, used only for this). Nothing is generated
  unless you run it.
- The daemon logs each alert as an event (`kind = "alert"`), excluded from the
  speech stats.
- Hooks: `PermissionRequest`, `Notification` (both async) are added to
  `hooks/hooks.json` and `install-hooks`. Daemon down: the hook speaks a fixed
  built-in phrase with `say` if `[alerts] enabled = true`.
- GUI: the Config page shows the alert settings (toggle, events, profile) and
  the phrase pools, with Generate and "hear one" buttons.

## A. Seed explorer (voice lab)

- `POST /api/lab/explore` `{settings: <same fields as /api/lab/preview>, start_seed: 1, ahead: 4}`
  starts an exploration (one at a time; a new one replaces the old one).
  The daemon renders seeds `start_seed, start_seed+1, …` one job at a time, at
  lab priority, staying `ahead` takes past the cursor. Each take is a preview
  plus `seed`, `f0_hz` (median pitch), `f0_spread_hz` (10th–90th percentile),
  `audio_s`. Takes arrive as SSE `explore` events
  `{"type": "take", "take": …}` and `{"type": "state", "state": …}`.
- `POST /api/lab/explore/cursor` `{seed}` moves the cursor (the page sends it
  as you move). `POST /api/lab/explore/stop`. `GET /api/lab/explore` returns
  the state and takes. The explorer pauses when the cursor has not moved for
  10 minutes and stops after 500 takes.
- Stars: `POST /api/lab/stars` `{preview_id, note?}` copies the take into
  `~/.afterword/lab/starred/` and records its settings, seed and metrics in
  `~/.afterword/lab/starred.json`, so stars survive daemon restarts;
  `GET /api/lab/stars`, `DELETE /api/lab/stars/{id}`. `/api/lab/freeze`
  accepts a star id as well as a preview id.
- Stability check: `POST /api/lab/stability` `{star_id}` (or settings + seed)
  renders the same settings and seed on three other sentences (one in another
  language) and returns the previews with their pitch, so drift shows before
  you freeze.
- "Freeze & save": the page freezes a star into a clip, then saves a profile
  `voice = "clone:<clip>"` with the take's language, pace and seed.
- Page: an "Explore seeds" panel in the lab: the usual voice settings, start
  seed, Start/Stop; a list of take cards (seed, pitch, length, play); keys
  `space` play/replay, `j`/`k` next/previous, `s` star, `x` drop (hide);
  auto-advance toggle; a pitch filter (min/max Hz); the starred shortlist with
  play, stability check, freeze & save, delete.
- Pitch: a numpy autocorrelation F0 estimate (70–400 Hz, voiced frames only);
  `null` when numpy is missing.

## Build order

1. D pronunciation list.
2. E streaming player, engine priority and cancel, clip writing.
3. B per-language voices, several `<say>`, script split; queue turns.
4. E early speaking (MessageDisplay).
5. C spoken alerts.
6. A seed explorer.

## As built

(Filled in while building.)

### D. Pronunciation list

- Code: `afterword/hooks/pronounce.py` (`clean_table`, `merge`,
  `pairs_from_config`, `apply`); daemon side `Config.pronounce` (global
  pairs), `Profile.pronounce` (own pairs), `Config.pronunciations(profile)` /
  `Config.speakable(text, profile)`. Tables are tuples of `(key, value)`
  pairs; profile entries come first so they win ties. Limits per table: 500
  entries, key 100 chars, value 300 chars (extra or bad entries are skipped
  with a config warning).
- Boundaries are checked per end, not only for keys that start and end with
  a word character: a key whose first character is a word character needs a
  non-word character before it, likewise at the end. So `C++` does not hit
  `ABC++` and `.NET` does not hit `.NETCore`; `->` and CJK keys still match
  anywhere. "Word character" is `\w` minus CJK/kana/Hangul, so `uv` matches
  in `用uv安装` (the design's `(?<![\w])` would not, since Python's `\w`
  includes Han).
- Tie-break at equal length: exact-case keys before lowercase ones, then
  profile before global. `apply` only indexes the keys that occur in the
  text (substring prefilter; see the review fixes below for the matcher). Hook
  fallback with a 498-entry table on /usr/bin/python3: 72 ms in all.
- An unknown profile name borrows the first profile's entries, like its
  voice (daemon and hook alike). Lab previews without `profile` use only
  the global table.
- Daemon: applied after the announce prefix, before synthesis (B's script
  split must go after this line in `_play_item`). `timings.spoken_text` is
  set when the spoken text differs from `events.text` (so it also shows the
  announce prefix when that is on). The Log row shows "spoken as: …".
- Lab: request field `skip_pronounce` (bool); response adds `spoken_text`
  (null when unchanged) and `skip_pronounce`. A frozen preview's transcript
  is the spoken text, so it matches the audio.
- Hook: `_speakable()` in the `say` fallback only; the mini TOML parser now
  also reads `'literal'` keys. Spool/log `text` stays the original.
- `/api/pronounce` lives in `lab.py` (`pronounce_view`, `save_pronounce`,
  `write_pronounce`) and is routed in `web.py`. POST accepts
  `{"global": {...}}` or `{"profile": name|null, "entries": {...}}`; it
  never deletes the table (keeps its comments) and re-reads the result
  with tomllib before writing.
- Default config: a real `[pronounce]` table at the end of the file with the
  explanation and examples as comments inside it, so GUI-added entries land
  under the examples and the lab's profile writes never land after them.
- GUI: "hear it" renders the row's "spoken as" text with
  `skip_pronounce: true` (works for unsaved rows) using the selected
  profile's voice, or the default profile when editing the global table.
- Review fixes. Matching is a scan, not one regex: keys are bucketed by
  case-folded first character and boundaries are checked in Python (the
  CJK-aware lookaround cost ~1 ms compile per key; Python's `re` also tries
  every alternative at every position). 1000 keys all present in 5000 chars:
  ~15 ms (3.12), ~35 ms (/usr/bin/python3). A test checks it against the old
  lookaround regex on random input.
- Lab: `pronounce_profile` picks the profile table without making the
  profile the settings base; builder previews (main, every language, A/B
  builder slot) send it when the profile-name field names an existing
  profile (set by Load). A/B profile slots honour "skip pronunciations".
- `write_pronounce` keeps entries GET could not show (invalid, or past 500)
  unless the body names that key; the Config page lists `warnings`.
  `"profile": ""` is a 400. All config.toml read-modify-writes hold
  `lab.CONFIG_WRITE_LOCK` (pronounce runs in a thread, profiles on the loop).
- Mini TOML (hook on < 3.11): dotted keys (`pronounce.GIF = "jif"`), one-line
  triple-quoted strings, TOML escapes; values it cannot read (arrays, inline
  tables, multi-line strings, dates) leave the key out instead of becoming
  raw text, and multi-line string bodies are skipped.

### E. Streaming player (build step 2)

- Code: `afterword/daemon/stream.py` (`TimeStretch` WSOLA, `Playback`,
  `StreamPlayer`, `NullStreamPlayer`, `make_stream_player`,
  `decode_to_stream_rate`). Each `Playback` has its own player thread that
  opens the device, time-stretches (so the engine thread never does), fills
  the buffer, starts after 0.3 s is buffered (or the input ended) and stops
  or aborts. 20 ms blocks, `latency="low"`. `first_audio_at` = first callback
  with data + the stream's reported output latency. PortAudio is
  re-initialised (~2 ms) before each open so a default-device change is
  followed. No callback for 3 s while playing = device error.
- Config: new `[daemon] stream = true` (default). `player` keeps
  `"afplay" | "null"`; with streaming on, `"afplay"` means the device through
  PortAudio with afplay as the fallback, `"null"` (or `AFTERWORD_PLAYER=null`)
  the `NullStreamPlayer` (real-time fake device; `last.fed` / `last.played`).
  `/api/status`: `player` is `"stream"`, `"afplay"` or `"null"`, plus
  `stream: {enabled, active, error}`.
- Fallbacks: sounddevice/numpy not importable → v1 path for every item. The
  device cannot be opened → v1 path for that item (tried again next item).
  Each distinct reason is logged once. The stream fails after opening with
  nothing heard → the rendered clip is played with afplay. A failure after
  audio was heard → status `error`, partial clip kept.
- Engine: `PriorityQueue` of `(priority, seq)`; `qwen.LIVE` = 0 (playback
  queue, warm-up at start, trims), `qwen.LAB` = 1 (previews, lab warm).
  `submit_render(req, priority=, cancel=, on_chunk=, stream=)`;
  `RenderCancelled`. The cancel event is checked before the job starts and
  at every chunk, and the waiter (`synth.wait_future`) cancels a job still
  queued. A running lab render is not pre-empted. `MAX_PENDING` still limits
  lab work only. Streaming kwargs are passed to `_render` only when used.
- Synth API: `stream(text, profile, attrs, sink, cancel) -> StreamResult`
  on qwen, say and null. `say` streams at pace 1.0 (the stretch applies pace)
  and is rendered whole to a temp WAV, then fed in 0.5 s chunks (no
  incremental `say`). The stream is 24 kHz; other model rates are resampled.
- Segments: `server.Segment(text, profile, attrs)` with `.lang` and `.pace`,
  built by `Daemon._segments(item, profile, text)`, which returns one
  segment today. **Step B replaces `_segments`**. Per-segment pace works
  (`Playback.begin_segment(rate)`), gap and trim are in `_Feed` (trim drops
  leading samples below -40 dBFS, at most 70 ms, on later segments only).
  `playback_rate` in timings is the first segment's pace. **The v1 path
  (`_play_whole`) still takes one text**: B must make it handle several
  segments (for example render each and join them).
- Clip: 24 kHz 16-bit mono, written after playback ends (also when skipped or
  failed part-way). Skip reason: `stopped by user` once audio was heard,
  else `skipped by user` (as in v1).
- Timings as designed, plus `play_s`, `underruns` (when > 0) and
  `segments[].cancelled`. `first_chunk_ms` is the first segment's
  generation start (after any model load) to its first chunk.
  `/api/stats` adds `first_chunk_ms`; the Log row shows "first chunk" and
  "(streamed)". `afterword test --wait` prints one line per segment.
- Measured with my own daemon (CustomVoice 8-bit, `builtin:Ryan`, warm,
  `afterword test`): first chunk 251 / 236 ms, first audio 366 / 342 ms
  (enqueue to first sample out), RTF 0.25 / 0.23; pace 1.25 sounded right.
  Skipping 1.5 s in stopped a 20 s render with 6.7 s rendered (clip kept).
  A `say:` voice: first audio 1.35 s (say renders the whole line first).
  WSOLA costs about 1 ms per second of audio.
- Engine-level live check of the other voice kinds (no audio out, 3.5 s
  line): `design:` first chunk 0.20 s, `clone:` (ICL) 0.25 s warm / 0.35 s
  cold, 4 chunks each.
- Review fixes. `warm_async` defaults to `LAB`; only the start-up warm-up
  passes `LIVE`. The 250 ms gap is fed at rate 1.0 (not stretched by the next
  segment's pace). A render cancelled or failed part-way attaches its timings
  to the exception (`exc.partial`: `qwen.RenderResult` from the engine,
  `synth.StreamResult` from `stream()`, read with `synth.partial_result`), so
  skipped lines log real `synth_ms` / `rtf` / `first_chunk_ms`; `synth_ms`
  is null when nothing was rendered. `_Feed` raises `_DeviceGone` (a
  `RenderCancelled`) once the device errored or stalled after audio was
  heard, so the render stops; before anything was heard it renders on for
  the afplay replay. `stream_error` clears on a successful open; `status()`
  applies `[daemon] player`/`stream` (`_refresh_players`) when idle.
  `StreamPlayer` tracks every not-done `Playback` and serialises PortAudio
  calls with a lock: no re-initialise while a timed-out open is still inside
  PortAudio (the next open then waits on the lock and may time out too).

### B. Voice per language, several `<say>`, turns (build step 3)

- Code: `afterword/hooks/scriptsplit.py` (`split(text, primary, other)`),
  stdlib/3.9 in the hooks package rather than the daemon, because the hook's
  `say` fallback splits too. `config.LangVoice`, `Profile.langs` (a read-only
  mapping, left out of the hash), `Config.voice_for(profile, lang) -> Profile`,
  `config.lang_code`. `tags.MAX_SAYS`, `tags.limit_says`. Daemon:
  `server.clean_says` (checks the hook's `segments`), `Daemon._spoken_says`
  (announce prefix and pronunciations per block), `Daemon._segments(item,
  cfg, profile, says)` (**the signature changed**), `Daemon._render_joined`.
- `voice_for`: a language that sets its own `voice` does not inherit the
  profile's `ref_text` (it is the transcript of another clip); every other
  missing field inherits. A language without a table gets the profile with
  `lang` set. Config warnings: unknown language key, a language given twice
  (`zh` and `chinese`), a non-table, unknown fields; a bad field inherits.
- Parser: `says` keeps only non-empty blocks (so `say_count` counts those),
  sorted by position, the first 4. `to_dict()` adds `segments` (a 200-char
  preview of each block plus `chars`; see the review fixes).
  `limit_says` drops a later block that would get fewer than 20 characters.
- Hook message: `segments` (truncated, as spoken), `text` (their join),
  `attrs` (the first block's), `turn` (`prompt_id` or null). The daemon falls
  back to one block of `text`/`attrs` when `segments` is missing or unusable
  (at most 8 blocks, 5000 chars in all, attrs reduced to mood/lang/pace).
  `afterword test` text with several `<say>` speaks all of them.
- Splitter details: sentences end at `. ! ? …` before whitespace or at
  `。！？`; clauses at `, ; :` before whitespace or `，、；：`. The CJK
  language is decided per sentence (kana → ja, kanji included; else Hangul →
  ko; else zh; a Han-only sentence next to a Japanese one is ja). Whether a
  clause is CJK at all is decided by weight (see the review fixes). A Latin
  run of 4+ words with CJK on both sides in one clause is cut out at the
  script change (there is no punctuation to cut at). Parts with < 2 letters join a neighbour in the
  same sentence first. At most 12 parts per block, 16 segments per item
  (the rest joins the last).
- Non-CJK text goes to the primary language; **when the primary is itself
  zh/ja/ko it goes to the profile's first non-CJK `langs` entry** (else the
  primary), so a Chinese-primary profile with `[langs.en]` still speaks
  English parts in English. Primary `auto`: no lang (the model's auto).
- Prompt note without `langs`: "End your reply with <say>…</say>, or open
  with it to be heard sooner (…)" (202 chars for the test profile; the
  test's bound went from 200 to 230). With `langs`: as designed; the list is
  the primary language, then the `langs` keys as ISO codes. An unknown
  profile name gets the plain note (no borrowing for the note).
- `say` fallback: each part is `say -v <say_voice>` (its language's, else
  the profile's; v1 used the system voice), run in order by a detached
  `python3 -I -c` runner that retries a refused voice without `-v`. One part
  without a voice still runs `say` directly. A missing `say` binary is still
  reported as `fallback: error`.
- v1 path (stream off or no device) with several segments: each is rendered
  to a WAV, decoded to 24 kHz and joined with the stream's 250 ms gap and
  lead trim into one clip. Equal paces stay a `playback_rate`; mixed paces
  are WSOLA-stretched into the clip (played at 1.0, no `playback_rate`).
  Without numpy: one render of the joined text with the first segment's
  voice. The streaming path's "device failed before anything was heard"
  afplay replay, and History replay of a streamed clip (`playback_rate`),
  used the first segment's pace for the whole clip (fixed in the review:
  mixed paces now stretch the streamed clip too).
- Queue: `QueueItem.turn` (default `event-<id>`) and `.segments`;
  `PutResult.superseded` is now a list (a new turn supersedes all of that
  session's waiting items). `turn` is in queue summaries, not in the events
  table. Each segment gets its own random seed unless the profile (or the
  language) sets one.
- Lab: profile objects carry `langs`; a save with `langs` replaces the
  tables in place (matching `[langs.chinese]` to `zh`, keeping comments),
  without `langs` keeps them, `{}` removes them. The write is read back with
  tomllib and refused if any field landed elsewhere. Preview with `profile`
  and `lang` starts from `voice_for`. GUI: "Voice per language" rows in the
  profile editor (language, voice spec, "← builder", say voice, pace, ▶ plays
  the language's sample, ✕); a row's `ref_text`/`style`/`seed` are kept but
  not editable there. The Log row shows "N parts en·zh·en".
- For step 4 (early speaking): Stop still sends all of a reply's blocks as
  one item. Early items should carry the same `turn` as Stop (Stop's
  `prompt_id`; check how MessageDisplay's `turn_id` maps to it), so that
  same-turn items queue in order instead of superseding each other. The
  script split runs per item in the daemon, so early items are split too.
- Measured (own daemon, CustomVoice 8-bit warm, null player):
  `Not quite. 别急嘛，用 Python 再试一次。 Try again.` → en (Ryan) / zh
  (Vivian) / en (Ryan); first chunk 224 / 226 ms, first audio 228 / 229 ms,
  RTF 0.22, play time = audio + 2 × 250 ms (no extra gaps). Real Stop hook
  on /usr/bin/python3 with two blocks: hook 49 ms (same as before the change),
  first audio 272 ms. v1 path with four segments (one at pace 1.3): first
  audio 1.18 s (whole render first), RTF 0.20.

- Review fixes.
  - Splitter: "inside a CJK run" is taken literally. A clause that mixes
    scripts goes to the side with more weight (CJK letters against Latin
    words; a tie goes to the sentence's weight, then to the primary). A
    Latin-only clause of ≤ 3 words takes the CJK language only when its
    sentence is mostly CJK. Long Latin runs are cut out only between CJK
    letters, never at a clause edge. So `The word 你好 means hello.` is one
    English part (no mid-clause cut; the English voice reads 你好, and with
    `say` an English voice reads it as silence; `<say lang="zh">` gets the
    Chinese voice), `Next word: 苹果, which means apple.` is en / zh / en,
    `我喜欢 Python。` stays zh. A closing quote after `.` now ends a
    sentence (`"你好." Then`).
  - `say_voice` for a zh/ja/ko segment of a profile whose primary is another
    language is not inherited (macOS renders CJK text with an English voice
    as a silent WAV, exit 0): the language's `say_voice`, else the name in a
    `say:` language voice, else `scriptsplit.CJK_SAY_VOICES` (Tingting,
    Kyoko, Yuna). Same rule in `Config.voice_for` and the hook fallback
    (`scriptsplit.say_voice_for`). Note: macOS `say -v <unknown>` also exits
    0 (system voice), so the runner's retry only covers other refusals.
  - `say` runner: exit by signal (`killall say`) ends the whole run; a part
    past its time limit (60 s + 0.2 s per char) is not retried; only an exit
    status > 0 retries without `-v`.
  - Nested `<say>`: only the outermost block is kept (warning "nested tag(s)
    ignored"), so nothing is spoken twice and `.say` is the outer block as in
    v1.
  - Streaming clip with mixed segment paces: `_Feed.clip_audio()` stretches
    each segment into the clip as heard (gaps unstretched) and drops
    `playback_rate`, so the afplay replay and History match what was heard.
    One pace: un-stretched clip plus `playback_rate`, as before.
  - Lab: the langs read-back compares only `LANG_FIELDS`; a stray key in a
    language table stays in the file and no longer blocks every save. GUI:
    `langs` is sent only when the rows were loaded from a profile or edited
    (a fresh page saving over an existing name kept wiping its tables); the
    overwrite prompt says whether the language voices are kept or replaced.
  - `to_dict()["segments"]` is a 200-char preview per block plus `chars`,
    so events.parsed and the spool do not carry each block's full text again.
  - Hook mini TOML reads one-line inline tables (`langs = { zh = {…} }`;
    arrays inside are left out, malformed ones skip the key). Dotted keys
    (`langs.zh.voice = …`) already worked.

### E. Early speaking (build step 4)

- Code: `afterword/hooks/earlysay.py` (stream file, lock, `early_key`,
  `digest`, `stable_count`, `find_message`); `handle_message_display` and
  `_early_filter` in the hook; `Daemon._said_filter` / `_said_remember` in
  the daemon; `[daemon] early_say` (`DaemonSettings.early_say`,
  `common.early_say` on the hook side). Registered as `MessageDisplay`
  (`message-display`, `async: true`, `timeout: 5`) in `hooks/hooks.json` and
  `cli.HOOK_EVENTS`. Async is allowed for MessageDisplay (any command hook);
  its default timeout is 10 s and is not enforced on async hooks. The hook
  prints nothing (`displayContent` is never used).
- Turn: MessageDisplay carries `prompt_id` (the same as Stop's) as well as
  `turn_id` (different); early items use `prompt_id`. A MessageDisplay
  payload without a usable `prompt_id` (missing, not a string, > 200 chars)
  does nothing (`turn_id` would not match Stop's turn, so neither the hook
  nor the daemon could dedupe); the daemon also skips an early event without
  session or turn (`early without a turn`, `taken: []`). A Stop payload
  without `prompt_id` takes the turn from the matching stream record.
- Fast path: no session, voice off, session muted or mute-all exit after
  reading the state file (no stream file is touched; nothing goes to
  hook.log, `AFTERWORD_HOOK_LOG_ALL=1` logs them), as does a single
  final batch without `<say` (every `-p` message without a tag) and
  `early_say = false`. **Deviation:** once the voice is on, every batch of a
  message is stored, also before `<say` appears, because the code-fence
  state of a later block depends on the earlier lines; the parse runs only
  once `<say` was seen. Measured in-hook (after interpreter start): voice off
  ~8 ms, a whole message without a tag ~10 ms, an early send ~18 ms. To get
  there `common` now imports `tomllib` and `tempfile` lazily (13.7 → 6.8 ms
  of imports).
- Batches can arrive out of order and in parallel (async: one process per
  batch), so the file is keyed by batch index, only the run from index 0
  without a gap is parsed, and a per-session `flock` (`stream/<sid>.lock`,
  waited for at most 1 s, then the batch is skipped) serialises the
  processes. A complete message keeps only `text_sha` and `sent`; a message
  past 200 KB of text is given up (`done`), and the file is capped at 2 MB
  (UTF-8 bytes) and 64 messages (oldest dropped). Records and other sessions' files untouched
  for 10 minutes are removed.
- "Newly completed" also means settled: a block is sent only when the
  message is complete, a blank line follows it, or its paragraph up to the
  block's end has no backtick or `<!--` (an inline code span opened there
  could still close later and mask it), and no unpaired `<say>` opener
  (`ParseResult.open_says`: a later `</say>` would nest the block) and no
  `<!--` without a `-->` after it (`ParseResult.open_comments`: a later `-->`
  masks across paragraphs) comes before it. Sending stops at the first
  unsettled block, so a block's index in the message never changes. A
  `<voice>` tag in the message stops early speaking for the rest of that
  message (Stop decides); blocks before the tag that already went out are
  not taken back (known limit: `<say>…</say>` then `<voice set="off"/>` in a
  later batch is heard). Each
  message is limited like a reply (`limit_says`, now with `keep_index`).
- Hook → daemon: `event: "early"`, `segments[]` carry `early_key` and
  `digest` (sha1 of the block's cleaned text), plus `message_id` and `says`
  (indices). Stop's segments carry `digest` always and `early_key` when the
  stream file has the message (matched by its text, which equals
  `last_assistant_message`), and `early_dropped`. The ack has `taken`: the
  keys the daemon now counts as handled (queued, muted-skip, or already
  taken); the hook marks only those as sent, so a "daemon busy" or a refused
  early (`early_say` off) is left to Stop. Daemon down: MessageDisplay does
  nothing (no `say` fallback, no spool); Stop speaks or falls back as in v1.
- **Addition to the design:** the daemon refuses by key *and* by text. Per
  (session, turn) it remembers the keys and the digests of blocks sent early
  and of blocks sent by Stop (256 turns, 1 h). An early block is refused if
  its key was taken or Stop already sent its text; a Stop block if its key or
  its text was spoken early. Reason: in `-p` the final message's
  MessageDisplay call and Stop run at the same time, and Stop may read the
  stream file before that call wrote it (then it cannot know the message id,
  and the keys differ). Consequence: a block that repeats, word for word, a
  block spoken earlier in the same turn is not spoken by Stop. Two equal
  blocks in different messages are both spoken early (different keys).
- Event rows: `hook_event: "early"`; Stop with everything spoken early is
  `skipped`, reason `spoken early`; a refused early event `already spoken`;
  `timings.early_dropped`. `/api/status` `config.early_say`. The Log row's
  event cell has a tooltip for early rows and the timings line shows
  "N spoken early".
- Live check (Claude Code 2.1.330, `claude -p --model haiku --plugin-dir
  <repo>` with the marketplace copy disabled, own daemon, null synth/player,
  prompt: a `<say>` first, then `sleep 5`, then a closing `<say>`): the early
  line was enqueued 0.22 s after its text block's transcript timestamp (at
  the same time as the tool_use entry) and 7.7 s before the tool result,
  first audio 21 ms later; the closing line spoken early 1.8 s before Stop, which was
  `skipped`, `spoken early`, `early_dropped: 1`. hook.log had one entry each
  for SessionStart, UserPromptSubmit and Stop (no duplicate), so only the
  repo's hooks ran. Interactive mode (several batches per message) was only
  tested with simulated batches.

### C. Spoken alerts (build step 5)

- Code: hook side `afterword/hooks/alerting.py` (stdlib/3.9: `settings`,
  `describe_action`, `NOTIFICATION_EVENTS`, the fixed `say` phrases) and
  `handle_permission_request` / `handle_notification` / `_done_alert` in the
  hook; daemon side `afterword/daemon/alerts.py` (`AlertPhrases`, `BUILTIN`,
  `fill`, `generate`, `write_settings`), `config.AlertSettings`
  (`Config.alerts`), `Profile.persona`, `Daemon.ingest_alert` /
  `_speak_alert` / `_done_alert`, `QueueItem.kind`. Hooks registered as
  `permission-request` / `notification` (`async: true`, `timeout: 10`).
- The daemon decides (its `[alerts]`, mute) and picks the phrase; the hook
  sends `alert: {event, trigger, action?, tool?, notification_type?,
  message?, turn_s?, minutes?}` with `decision: "info"`. The hook's own
  reading of `[alerts]` only decides the fast path (alerts off: an info row,
  no state writes) and the `say` fallback (the mini TOML parser on Python
  < 3.11 reads the `events` array since the review fixes below).
- Rows: a spoken (or muted/superseded/expired) alert is `kind: "alert"`
  (`parsed.alert` has the details and the template); a PermissionRequest or
  Notification that is not spoken is a `kind: "hook"` `info` row with the
  reason; "done" is its own alert row next to the Stop row. Spooled alerts
  (daemon down) import as `kind: "alert"` when the fallback spoke.
- **Additions/deviations:** `elicitation_url_dialog` also counts as `input`.
  Speech does not supersede waiting alerts either (the two kinds never touch:
  an alert supersedes only its session's older waiting alerts and otherwise
  goes to the end of the line). The 30 s limit is checked when the worker
  takes the alert (the row stays `queued` until then). Turning a session's
  voice off leaves its waiting alerts (mute still drops them).
- State: UserPromptSubmit writes `turn_started` / `turn` and drops a stale
  `pending_permission`, only while `[alerts] enabled` (so voice-off sessions
  get a state file only then). PermissionRequest keeps the pending tool;
  the `permission_prompt` Notification uses it (see the review fixes below
  for which one) and removes it; Stop removes both. "Nothing spoken"
  for "done" = the Stop row is not queued, no block of the turn was spoken
  early, and the Stop was not muted or busy.
- Phrases: the pool language is the speaking profile's primary language
  (`auto` → en); without a pool for it the English pool is used with
  `lang="en"` on the segment. The profile's own language gets no lang attr,
  so a profile with `[langs]` splits an English `{action}` out of a Chinese
  phrase. Allowed placeholders per event: permission `{project} {action}
  {tool}`, input/idle `{project}`, done `{project} {minutes}`; a phrase whose
  placeholder has no value (no pending tool) is skipped in the pick. English
  fill lowercases an action like "Remove …" and says "1 minute". The file's
  pool for a (language, event) replaces the built-in one when non-empty.
  Limits: 120 chars, 60 phrases per pool, 12 languages, 512 KB file.
- Generate: `claude -p --model haiku --output-format json --tools ""
  --no-session-persistence <prompt>` run from the temp dir with
  `AFTERWORD_DISABLE=1` (our hooks stay out), 180 s limit; `--count` is per
  event (1–30). Merging starts from the pools in use, so the built-in
  phrases are copied into the file with the new ones. `persona` can be
  overridden per run (CLI `--persona`, GUI field), not saved; the lab does
  not edit `persona` but keeps it on save (not in `PROFILE_FIELDS`). Also
  `afterword alerts show`. Duplicate hooks (plugin + settings) are deduped
  with the claim markers (3 s since the review fixes below).
- GUI: Config page "Spoken alerts" panel; "hear one" renders through
  `/api/alerts/hear` as a lab preview (not the queue). Default config gets
  an `[alerts]` table before `[qwen]`; a POST adds a missing one at the end.
- Live check (own daemon, null synth/player, hook on /usr/bin/python3):
  PermissionRequest + Notification(`permission_prompt`) gave one info row
  and one alert row, "shop-api is waiting for your approval to remove
  node_modules directory.", hook 31 ms each, first audio 31 ms after the
  Notification hook started. `afterword alerts generate` (haiku, count 6):
  Chinese profile with a persona 22.6 s, English 17.2 s, 24 new phrases each,
  none rejected.
- Review fixes. Pending permissions are a list, `state["pending_permissions"]`
  (`{tool_name, tool, action, t}`, newest last, at most 8, 10 min), in
  `alerting.add_pending` / `take_pending` / `drop_answered`. Neither hook
  payload has a request id, so the `permission_prompt` Notification names an
  action only when it is sure: its message must name the entry's tool
  (`"… to use Bash"`, canUseTool hosts; MCP tools match by server and tool
  name), a message without a tool name (terminal) takes the newest entry,
  "network" in the message takes none, and two candidates made within 3 s
  of each other (parallel calls) are ambiguous. A MessageDisplay call (Claude
  writing again) drops entries older than 2 s, since those prompts were
  answered. Known limit: a sandboxed network request whose message neither
  names a tool nor says "network", right after a quickly answered prompt with
  no text in between, can still get that prompt's action.
- Alert claims last 3 s (`CLAIM_TTL_ALERT_S`), not 15: a second prompt for
  the same tool has the same content key and its notification can come
  within 15 s.
- State writes: `common.update_state(session_id, change)` does the
  read-modify-write under an flock on `state/.lock` (wait at most 1 s, then
  without it); every hook state write and the daemon's `set_session` use it,
  and the hook writes only the keys it changed (`_merge_keys`).
  `state.prune_states` (hourly, from the spool poller) removes state files
  untouched for 7 days that hold no profile, `last_profile` or mute.
- `done`: the hook sends `minutes` only for `turn_s >= 60` and the daemon
  ignores it for a shorter turn, so `{minutes}` phrases are skipped then.
- The hook's mini TOML reads arrays of scalars (also over several lines), so
  `[alerts] events` works on Python 3.9; a nested or broken array is left out
  (the defaults).
- Generate: `alerts.default_runner` starts claude in its own process group
  and `alerts.cancel_running()` (daemon shutdown and `request_shutdown`)
  kills it, so the daemon exits at once and nothing is merged afterwards.
  A non-string `profile` is a 400.

### A. Seed explorer (build step 6)

- Code: `afterword/daemon/explore.py` (`Explorer`, `StarStore`,
  `stability_plan`, `STABILITY_TEXTS`), `afterword/daemon/pitch.py`
  (`f0_stats`, `f0_of_wav`); `Lab._prepare` / `_response` /
  `new_preview_path` / `_entry` (a preview or an explorer take),
  `Lab.explore_*`, `star`, `unstar`, `stability`; routes in `web.py`;
  `Lab(..., publish=events.publish)`. The run loop is an asyncio task on the
  daemon loop; `Explorer.close()` runs at shutdown.
- **Addition: real pre-emption.** "One job at a time at lab priority" alone
  still lets live speech wait for the running take (0.7-1.5 s for a design
  voice). Explorer renders are submitted with
  `submit_render(..., stream=True, preemptible=True)`: a LIVE `_render`
  submitted while one runs sets its preempt event, the render stops at its
  next chunk and fails with `qwen.RenderPreempted` (a `RenderCancelled`), and
  the explorer renders the same seed again (`state.preempted`). Housekeeping
  LIVE jobs (trim, warm-up) and ordinary previews never pre-empt or get
  pre-empted. Live check: a line spoken during a take had first audio 405 ms
  against 262 ms with the explorer idle.
- **Addition: the explorer yields to live speech.** A reply's segments are
  submitted one at a time, so a take re-queued right after a pre-emption
  would start in every gap between segments (its model load cannot be
  interrupted, and with `keep_loaded` too small it evicts the live model).
  The explorer starts no take while `Lab.live_busy()` (the daemon: synth is
  qwen and an item is playing or queued) or within `LIVE_QUIET_S` (1 s) of a
  LIVE render (`QwenEngine.live_quiet_s()`); state meanwhile is `running`
  with reason `holding for live speech`. The engine sets `_current` under
  its lock and pre-empts a preemptible job dequeued while a LIVE render
  waits before it starts (no race window).
- A new exploration checks the voice first (`Lab.check_voice`: missing clip
  404, model not downloaded / no mlx-audio 503) and only then discards the
  old one; other errors (unknown speaker, unreadable clip) still end the new
  exploration as `error` at its first render.
- Takes stream, previews do not: same seed, same tokens; the audio differs by
  < 1e-3 (research note), so a take and a preview of the same seed sound the
  same and the stability check (ordinary previews) matches the star.
- Takes are not in `Lab.previews` (that keeps 100) but in the exploration
  (up to 500 files, ~170 KB each for a 3.5 s line); they are deleted when a
  new exploration replaces the old one and at restart. Stop keeps them.
- Cursor: clamped to `start_seed … next_seed`. `auto: true` (the page's
  auto-advance) moves it without counting as activity, so a page left
  auto-playing pauses after 10 minutes instead of rendering 500 takes.
  Statuses: running, waiting (caught up), paused, stopped, done, error.
  Three failed renders in a row, or a 400/404/503 (model missing, bad clip),
  end it as `error`; a single failed seed is skipped (`failed`).
- Starts refuse `say:` voices and a `settings.seed` (use `start_seed`).
- Pitch: frames of 40 ms every 10 ms on audio decimated by an integer
  factor to <= 16 kHz (`pitch.analysis_step`: 24 kHz Qwen audio → 12 kHz,
  44.1/48 kHz → 14.7/16 kHz; block averages), unbiased
  normalised autocorrelation, voiced = within 35 dB of the loudest frame and
  peak >= 0.5, smallest lag within 85% of the best peak (avoids octave-down
  errors). ~10 ms per 5 s of audio. Every preview response now carries
  `f0_hz` / `f0_spread_hz` too. On a real Ryan render most frames sat in
  85-235 Hz with a few octave-up frames around 300 Hz; the median is robust
  to them, the spread less so.
- Stars: ids `s` + 10 hex, `lab/starred/<id>.wav` + `lab/starred.json`
  (`{"version": 1, "stars": [...]}`, atomic write, 0600). Entries whose WAV is
  missing are dropped on load; an unreadable index is moved aside to
  `starred.json.bad-<time>` (reported as `error`). Starring a preview twice
  returns the first star. Max 1000. The star keeps the text, spoken text,
  pronunciation choice, settings, seed and pitch; freeze uses the spoken text
  as the transcript.
- Stability: two sentences in the take's language (built-in pairs for the 10
  Qwen languages; `auto` → en) plus one in another (zh for en, else en);
  `f0_delta_st` per sentence (semitones against the star), `max_delta_st`,
  `f0_range_hz`. One check at a time (409). Live check of a design voice
  (seed 104): 0.09 / 0.58 / 0.2 st; another design voice (seed 3) drifted
  3.8 / 1.8 / 4.4 st, which is what the check is for.
- Page: the panel uses the builder's settings (its own text box, else the
  main text; the builder's seed is ignored). Dropped takes are hidden on the
  page only. `j` past the last take moves the cursor to the newest take and
  selects (and plays) the next take that passes the filter when it arrives;
  takes that arrive hidden by the filter move the cursor on (`auto: true`)
  so the explorer keeps rendering until one shows. `j`/`k`/`x` step from the
  selected take's place among all takes, even when it is hidden.
  Keys are ignored in inputs, textareas, selects and contenteditable, and
  `space` also on focused buttons, links and audio controls. "Freeze & save"
  prompts once for a name used for both the clip and the profile
  (`voice = "clone:<name>"`, the take's `lang`, `pace`, `seed`), asking
  before replacing either.
- Live check (own daemon on 47911, VoiceDesign 8-bit, null player): first
  take 1.25 s after Start (0.5 s model load), then one take every ~0.75 s
  (3.5-4 s of audio, synth ~0.7 s); four takes (start + 3 ahead), then
  `waiting`; cursor 102 → two more in 1.7 s, cursor 105 → three more in 2.5 s;
  pitch 166-214 Hz across seeds. Star survived a restart (previews cleared,
  `starred/` kept); stability check took 4.4 s.


### Completeness review (after step 6)

- Checked each section against code, tests and docs; a live run of every
  hook (system python) against a fresh home's daemon matched the design.
  Fresh-home default config: no warnings, and the hook's mini TOML reads it
  exactly like tomllib.
- Added `tests/test_v2_wiring.py`: `hooks/hooks.json`, `cli.HOOK_EVENTS` and
  the hook's event map agree (args, `async`, `timeout`); the default config
  is clean on both sides (also on /usr/bin/python3); every hook module
  imports with `-S -I` (no site-packages) on 3.9; every POST/DELETE route is
  refused without `X-AfterWord`; all six hook events survive type-confused
  payloads with voice on, alerts on and a stream file present.
- Open (not built or only simulated): interactive-mode MessageDisplay with
  many batches was tested only with simulated batches; the known limits
  above (a later `<voice set="off"/>` after an early block; an unnamed
  network prompt right after a quickly answered one) remain.

### Live verification fix (after the build)

- A real `claude -p` turn that wrote `<voice set="tutor-bi"/>` in a message
  before a tool call stayed silent: Stop gets only the final message in
  `last_assistant_message` (a v1 limit, now more visible because turns span
  several messages). Stop now falls back to the last `<voice>` tag in the
  turn's earlier assistant messages, read from the transcript tail (they are
  written before the tool call; each message is parsed on its own, so code
  masking applies). The final message's own tag still wins. `<say>` blocks in
  that earlier message are not spoken (the voice was off when it was shown).
  Tests: `test_voice_tag_written_before_a_tool_call_turns_the_session_on`,
  `test_turn_voice_ignores_earlier_turns_and_code_and_prefers_the_final_message`.
