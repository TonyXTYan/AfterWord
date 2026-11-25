# Voice lab HTTP API (daemon, http://127.0.0.1:47800)

Contract between the daemon (`afterword/daemon/lab.py`, `web.py`) and the lab
tab in `afterword/ui/index.html`. All POST/DELETE requests must send the header
`X-AfterWord: 1` (the daemon refuses them otherwise). Errors from lab routes
always come back as JSON `{"error": "<human-readable message>"}` with a
4xx/5xx status, including unexpected failures (500 `render failed: …`).

## Voice specs

A profile's `voice` string picks the source:

| spec | model | meaning |
|---|---|---|
| `builtin:<Speaker>` | Qwen3-TTS CustomVoice | preset speaker; `style` + `mood` become the style instruction |
| `clone:<clip-name>` | Qwen3-TTS Base | clone from `~/.afterword/voices/<clip-name>.wav` (transcript in `ref_text`, or the clip's saved transcript) |
| `clone:<absolute or ~ path>` | Qwen3-TTS Base | clone from that audio file |
| `design:<description>` | Qwen3-TTS VoiceDesign | voice invented from the description each time (can drift between renders; "freeze" a preview into a clip for a stable voice) |
| `say:<MacVoice>` | macOS `say` | no model needed; useful as a baseline |

`lang` accepts an ISO code (`en`, `ja`, `zh`, `es`, …), a Qwen language name
(`english`, `japanese`, …) or `auto`.
`pace` (0.5–2.0) is applied at playback: the browser sets
`audio.playbackRate = pace` (pitch is preserved by default), the daemon uses
`afplay -r`. Qwen3-TTS has no native speed control.

## GET /api/lab/catalog

```json
{
  "engine": {
    "available": true,              // mlx-audio importable
    "error": null,                  // why not, if unavailable
    "busy": false,                  // a render is running
    "pending": 0,                   // renders waiting
    "memory": {"active_gb": 3.1, "peak_gb": 3.4},   // or null
    "models": {
      "custom_voice": {"repo": "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit", "downloaded": true, "loaded": true, "load_ms": 2100.5, "error": null},
      "base":         {"repo": "...", "downloaded": true, "loaded": false, "load_ms": null, "error": null},
      "voice_design": {"repo": "...", "downloaded": false, "loaded": false, "load_ms": null, "error": "why the last load failed, or null"},
      "kokoro":       {"label": "Kokoro", "repo": "mlx-community/Kokoro-82M-bf16", "downloaded": true, "loaded": false, "load_ms": null, "error": null},
      "chatterbox":   {"label": "Chatterbox Multilingual", "repo": "mlx-community/chatterbox-multilingual-v3", "downloaded": true, ...}
    },                                // every model also has "label"; chatterbox counts as downloaded only with its S3TokenizerV2
    "pull_command": "uv run afterword models pull"
  },
  "speakers": [{"name": "Ryan", "native": "english", "dialect": null},
               {"name": "Eric", "native": "chinese", "dialect": "sichuan"}],   // [] if CustomVoice not downloaded
  "languages": ["auto", "english", "chinese", "japanese"],               // Qwen names
  "lang_codes": {"en": "english", "zh": "chinese", "ja": "japanese"},    // ISO -> Qwen name
  "say_voices": [{"name": "Samantha", "locale": "en_US"}],
  "kokoro_voices": [{"name": "zf_xiaobei", "lang": "Mandarin Chinese", "lang_code": "zh", "gender": "female"}],  // [] if not downloaded
  "kokoro_langs": ["en", "es", "fr", "hi", "it", "ja", "pt", "zh"],
  "chatterbox_langs": ["ar", "da", "de", "...", "zh"],
  "apple": {"available": true, "error": null},     // the Swift helper can run (built on first use)
  "apple_voices": [{"id": "com.apple.voice.premium.en-US.Ava", "name": "Ava", "lang": "en-US",
                    "quality": "premium", "novelty": false, "personal": false}],   // Personal Voice left out
  "clips": [{"name": "me", "url": "/api/lab/clips/me", "duration_s": 6.2, "ref_text": "transcript or empty", "created": 1795480000.0}],
  "profiles": {
    "nova": {"voice": "builtin:Serena", "ref_text": "", "lang": "en", "pace": 1.0, "style": "calm, slightly wry",
             "max_chars": 400, "say_voice": "Samantha", "seed": null, "temperature": null,
             "langs": {"zh": {"voice": "builtin:Vivian", "say_voice": "Tingting"}}}   // [profiles.x.langs.*]; {} if none
  },
  "daemon_synth": "qwen",          // synth the daemon is actually using
  "configured_synth": "qwen",      // what config.toml asks for (differs if Qwen is unavailable)
  "samples": {"en": "…", "ja": "…", "zh": "…", "es": "…", "fr": "…", "de": "…"}   // same sentence in several languages
}
```

## POST /api/lab/preview

Render one line. Not queued for speaker playback; the browser plays it.

Request (every field except `text` optional; fields override the profile's):
```json
{"text": "Hello there.", "profile": "nova", "voice": "builtin:Ryan", "ref_text": null,
 "lang": "en", "pace": 1.1, "style": "warm", "mood": "dry", "seed": 1234, "temperature": 0.8,
 "skip_pronounce": false, "pronounce_profile": null}
```
`text` is 1–1000 chars. If `seed` is null the daemon picks one and returns it,
so a good render can be reproduced. The pronunciation list is applied before
rendering unless `skip_pronounce` is true: `[pronounce]`, plus
`[profiles.<name>.pronounce]` for `pronounce_profile` when given, else for
`profile`. `pronounce_profile` only picks the table (the builder sends the
profile it was loaded from without making it the base of the settings);
404 if no such profile. With `profile` and a `lang` that the profile has a
`[profiles.x.langs.<lang>]` table for, the base settings are that language's
(its voice, pace, style, …, as the daemon would speak it); explicit fields
still override them.

Response 200:
```json
{"id": "p17", "url": "/api/lab/previews/p17.wav", "text": "Hello there.",
 "spoken_text": null, "skip_pronounce": false, "pronounce_profile": null,
 "backend": "qwen", "model": "custom_voice",   // backend: qwen | kokoro | chatterbox | apple | say
 "synth_ms": 812.3, "load_ms": 0.0, "audio_s": 2.41, "rtf": 0.34,
 "f0_hz": 142.5, "f0_spread_hz": 61.0,
 "pace": 1.1, "seed": 1234,
 "settings": {"voice": "builtin:Ryan", "ref_text": "", "lang": "en", "pace": 1.1, "style": "warm",
              "mood": "dry", "seed": 1234, "temperature": 0.8}}
```
`text` is the cleaned input; `spoken_text` is what was rendered when the
pronunciation list changed it (else null); `pronounce_profile` is the profile
whose own entries were applied (null: global only, or skipped).
`load_ms` > 0 means a model had to be loaded first (first use is slow).
`f0_hz` is the median pitch and `f0_spread_hz` the 10th-to-90th percentile
range (an autocorrelation estimate, 70-400 Hz, voiced frames only); both are
null when there is no estimate (silence, noise, or numpy missing).
`settings.style` and `settings.mood` are null when the voice ignores them
(clone and say voices). Each render is capped at a length proportional to
the text, so a degenerate render cannot run for minutes.
Previews are rendered whole (not streamed), at lab priority: a line the
daemon is about to speak runs before any waiting lab render (a running one
finishes first), so a preview can take longer while replies are being spoken.
Errors: 400 bad input, 404 unknown profile/clip, 429 too many renders waiting,
503 model not downloaded or mlx-audio missing (message includes the pull command).

`GET /api/lab/previews/<id>.wav` serves the audio. The last 100 previews
are kept, and all of them are cleared when the daemon restarts.

## Profiles

`POST /api/lab/profiles` saves into `~/.afterword/config.toml`, keeping the
file's comments and other content; the daemon hot-reloads it.
```json
{"name": "nova", "voice": "builtin:Serena", "ref_text": "", "lang": "en", "pace": 1.0, "style": "calm",
 "max_chars": 400, "say_voice": "", "seed": 1234, "temperature": null,
 "langs": {"zh": {"voice": "builtin:Vivian", "say_voice": "Tingting", "pace": 1.05}, "ja": {}},
 "overwrite": false}
```
Name: `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. Response `{"ok": true, "profile": {...}}`.
409 if the profile exists and `overwrite` is false. 400 on invalid values.

`langs` (optional) replaces the profile's `[profiles.<name>.langs.*]` tables:
keys are ISO codes or Qwen names (at most 12), values any of `voice`,
`ref_text`, `style`, `pace`, `say_voice`, `seed` (empty strings and nulls are
left out, so the profile's value applies; `{}` keeps the language with no
fields of its own). A language already in the file keeps its table, spelling
(`[...langs.chinese]`) and comments; the others are removed; new ones are
added. `"langs": {}` removes them all; leaving `langs` out keeps the file's
(the GUI leaves it out unless its rows were loaded from a profile or edited).
A key in a language table that is not one of these fields (a typo) is left
in the file as it is. 400 for an unknown language, a language given twice, an
unknown field, or a bad value.

`DELETE /api/lab/profiles/<name>` → `{"ok": true}` (404 if missing); the
profile's `pronounce` and `langs` tables go with it.

## Reference clips (for cloning)

`POST /api/lab/clips?name=<name>&consent=1&ref_text=<url-encoded transcript>`
with the raw audio as the request body (`Content-Type: audio/wav`, WAV
preferred; MP3/FLAC also decoded). Max 20 MB, max 60 s. `consent=1` is
required: the uploader confirms the voice is theirs or used with consent.
Response `{"clip": {...same shape as catalog.clips[]}}`. 409 if the name exists
(add `&overwrite=1` to replace).

`GET /api/lab/clips/<name>` serves the clip. `DELETE /api/lab/clips/<name>`
removes it (and its transcript).

`POST /api/lab/freeze` turns a preview, a seed-explorer take or a star into a
clip (typically a VoiceDesign render, to get a stable voice to clone):
```json
{"preview_id": "p17", "name": "narrator", "overwrite": false}
{"star_id": "s0c4bc55c77", "name": "narrator"}     // a star (a star id as preview_id works too)
```
→ `{"clip": {...}}`; the clip's transcript is the preview's spoken text (after
the pronunciation list), so it matches the audio. Then use
`voice = "clone:narrator"`. 409 if the clip exists and `overwrite` is false;
400 if the preview is shorter than 1 s or longer than 60 s.

## POST /api/lab/warm

`{"model": "custom_voice" | "base" | "voice_design"}` → loads it now so the
next render is fast. Response: the `engine` object from the catalog. Runs at
lab priority: live speech queued after it still goes first.

## Seed explorer

Listens through one voice setting seed by seed. One exploration at a time;
starting a new one replaces the old one (its takes and their files are
discarded; stars are kept).

`POST /api/lab/explore`
```json
{"settings": {"text": "Morning. The tests pass.", "voice": "design:A warm, low voice…", "lang": "en",
              "pace": 1.0, "style": null, "mood": null, "temperature": null,
              "skip_pronounce": false, "pronounce_profile": null},
 "start_seed": 1, "ahead": 4}
```
`settings` takes the fields of `POST /api/lab/preview` (with `text`, without
`seed`: 400). `start_seed` 0-2147483647 (default 1), `ahead` 1-20 (default
4). `say:`, `apple:` and `kokoro:` voices are refused (no seed). The voice is checked before the
old exploration is replaced: a missing clone clip is a 404 and a model that
is not downloaded (or no mlx-audio) a 503, and the old exploration stays.
Response: the same as `GET`.

The daemon renders seeds `start_seed`, `start_seed + 1`, … one at a time at
lab priority until it has rendered `ahead` takes past the cursor (the cursor
starts at `start_seed`). Explorer renders stream and are pre-emptible: a line
the daemon is about to speak stops the running take at its next chunk (about
a quarter of a second) and the same seed is rendered again afterwards
(`state.preempted` counts these). It starts no take while the daemon is
saying a line or has one queued (Qwen only), nor within a second of a live
render, so a reply's segments are not slowed by takes started between them;
meanwhile the state is `running` with reason `"holding for live speech"`.
It pauses when the cursor has not moved for 10 minutes and stops after 500
takes; three failed renders in a row, or an error only a render finds (an
unknown speaker, an unreadable clip), end it with status `error`.

`GET /api/lab/explore` → `{"state": {...}, "takes": [take, ...]}` (takes in
seed order). With no exploration: `{"state": {"id": null, "status": "idle", ...}, "takes": []}`.
```json
"state": {"id": "0fefac65", "status": "waiting",   // running | waiting | paused | stopped | done | error
          "reason": "3 takes ready past the cursor",  // why (or null)
          "text": "Morning. The tests pass.", "spoken_text": null,
          "settings": {"voice": "design:…", "ref_text": null, "lang": "en", "pace": 1.0, "style": null, "mood": null, "temperature": null},
          "start_seed": 100, "ahead": 3, "cursor": 100, "next_seed": 104,
          "rendering": null,          // the seed being rendered, or null
          "count": 4, "preempted": 0, "failed": 0,
          "started_at": 1795528000.1, "moved_at": 1795528000.1,
          "max_takes": 500, "pause_after_s": 600.0, "ahead_max": 20}
```
`waiting` = caught up (`ahead` takes past the cursor); `paused` = idle for 10
minutes (a cursor move resumes it); `stopped`, `done` and `error` are final
(start again to go on). A take is a preview response (see above; its
`settings.seed` is its seed) plus `"explore_id"`, served at its `url`,
freezable and starrable like any preview, and kept until the exploration is
replaced or the daemon restarts.

`POST /api/lab/explore/cursor` `{"seed": 102, "auto": false}` → `{"state": …}`.
The page sends the seed of the take it shows. The cursor is clamped to
`start_seed … next_seed`. `"auto": true` (auto-advance) moves the cursor but
does not count as activity for the 10-minute pause. 404 with no exploration.

`POST /api/lab/explore/stop` → `{"state": …}` (takes stay).

SSE (`GET /api/stream`), event type `explore`:
`{"type": "take", "take": {...}}` for each new take and
`{"type": "state", "state": {...}}` whenever the state changes.

## Stars

`POST /api/lab/stars` `{"preview_id": "p7c378510c8", "note": "warm"}` copies a
preview or take into `~/.afterword/lab/starred/<id>.wav` and records it in
`~/.afterword/lab/starred.json`, so stars survive daemon restarts (the
previews themselves do not). Starring the same preview again returns the
existing star (`"created": false`, the note updated when given).
```json
{"star": {"id": "s0c4bc55c77", "created": 1795528076.5, "note": "warm", "preview_id": "p7c378510c8",
          "text": "Morning. The tests pass.", "spoken": "Morning. The tests pass.",
          "skip_pronounce": false, "pronounce_profile": null,
          "settings": {"voice": "design:…", "ref_text": null, "lang": "en", "pace": 1.0, "style": null,
                       "mood": null, "seed": 104, "temperature": null},
          "seed": 104, "audio_s": 3.44, "model": "voice_design", "backend": "qwen",
          "f0_hz": 200.3, "f0_spread_hz": 77.7, "url": "/api/lab/stars/s0c4bc55c77.wav"},
 "created": true}
```
404 if the preview is gone (also when it is discarded while being
copied), 409 past 1000 stars. `GET /api/lab/stars` →
`{"stars": [star, ...], "error": null}` (newest first; `error` says when an
unreadable `starred.json` was moved aside). `GET /api/lab/stars/<id>.wav`
serves the audio; `DELETE /api/lab/stars/<id>` → `{"ok": true}` (404 if
unknown) removes the entry and its file.

## Stability check

`POST /api/lab/stability` `{"star_id": "s0c4bc55c77"}`, or
`{"settings": {...preview fields...}, "seed": 104}`, renders the same
settings and seed on three other sentences: two in the take's language (`en`
for `auto`) and one in another (Chinese for English, else English). Each is
an ordinary preview (playable, freezable, starrable). Takes about as long as
three previews; one check at a time (409).
```json
{"star_id": "s0c4bc55c77", "seed": 104, "voice": "design:…",
 "base": {"text": "Morning. The tests pass.", "f0_hz": 200.3, "f0_spread_hz": 77.7},   // null for the settings form
 "takes": [{...preview..., "check_lang": "en", "f0_delta_st": 0.09},
           {...preview..., "check_lang": "en", "f0_delta_st": 0.58},
           {...preview..., "check_lang": "zh", "f0_delta_st": 0.2}],
 "errors": [],                 // a sentence that failed after the first one rendered
 "max_delta_st": 0.58,         // largest |pitch change| against the take, in semitones (null without a base)
 "f0_range_hz": 6.8}           // highest minus lowest median pitch, take included
```
400 for a `say:` voice or a missing seed, 404 for an unknown star.
