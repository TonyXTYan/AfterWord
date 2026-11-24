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
      "voice_design": {"repo": "...", "downloaded": false, "loaded": false, "load_ms": null, "error": "why the last load failed, or null"}
    },
    "pull_command": "uv run afterword models pull"
  },
  "speakers": [{"name": "Ryan", "native": "english", "dialect": null},
               {"name": "Eric", "native": "chinese", "dialect": "sichuan"}],   // [] if CustomVoice not downloaded
  "languages": ["auto", "english", "chinese", "japanese"],               // Qwen names
  "lang_codes": {"en": "english", "zh": "chinese", "ja": "japanese"},    // ISO -> Qwen name
  "say_voices": [{"name": "Samantha", "locale": "en_US"}],
  "clips": [{"name": "me", "url": "/api/lab/clips/me", "duration_s": 6.2, "ref_text": "transcript or empty", "created": 1795480000.0}],
  "profiles": {
    "nova": {"voice": "builtin:Serena", "ref_text": "", "lang": "en", "pace": 1.0, "style": "calm, slightly wry",
             "max_chars": 400, "say_voice": "Samantha", "seed": null, "temperature": null}
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
 "lang": "en", "pace": 1.1, "style": "warm", "mood": "dry", "seed": 1234, "temperature": 0.8}
```
`text` is 1–1000 chars. If `seed` is null the daemon picks one and returns it,
so a good render can be reproduced.

Response 200:
```json
{"id": "p17", "url": "/api/lab/previews/p17.wav", "text": "Hello there.",
 "backend": "qwen", "model": "custom_voice",
 "synth_ms": 812.3, "load_ms": 0.0, "audio_s": 2.41, "rtf": 0.34,
 "pace": 1.1, "seed": 1234,
 "settings": {"voice": "builtin:Ryan", "ref_text": "", "lang": "en", "pace": 1.1, "style": "warm",
              "mood": "dry", "seed": 1234, "temperature": 0.8}}
```
`load_ms` > 0 means a model had to be loaded first (first use is slow).
`settings.style` and `settings.mood` are null when the voice ignores them
(clone and say voices). Each render is capped at a length proportional to
the text, so a degenerate render cannot run for minutes.
Errors: 400 bad input, 404 unknown profile/clip, 429 too many renders waiting,
503 model not downloaded or mlx-audio missing (message includes the pull command).

`GET /api/lab/previews/<id>.wav` serves the audio. The last 100 previews
are kept, and all of them are cleared when the daemon restarts.

## Profiles

`POST /api/lab/profiles` saves into `~/.afterword/config.toml`, keeping the
file's comments and other content; the daemon hot-reloads it.
```json
{"name": "nova", "voice": "builtin:Serena", "ref_text": "", "lang": "en", "pace": 1.0, "style": "calm",
 "max_chars": 400, "say_voice": "", "seed": 1234, "temperature": null, "overwrite": false}
```
Name: `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. Response `{"ok": true, "profile": {...}}`.
409 if the profile exists and `overwrite` is false. 400 on invalid values.

`DELETE /api/lab/profiles/<name>` → `{"ok": true}` (404 if missing).

## Reference clips (for cloning)

`POST /api/lab/clips?name=<name>&consent=1&ref_text=<url-encoded transcript>`
with the raw audio as the request body (`Content-Type: audio/wav`, WAV
preferred; MP3/FLAC also decoded). Max 20 MB, max 60 s. `consent=1` is
required: the uploader confirms the voice is theirs or used with consent.
Response `{"clip": {...same shape as catalog.clips[]}}`. 409 if the name exists
(add `&overwrite=1` to replace).

`GET /api/lab/clips/<name>` serves the clip. `DELETE /api/lab/clips/<name>`
removes it (and its transcript).

`POST /api/lab/freeze` turns a preview into a clip (typically a VoiceDesign
render, to get a stable voice to clone):
```json
{"preview_id": "p17", "name": "narrator", "overwrite": false}
```
→ `{"clip": {...}}`; the clip's transcript is the preview's text. Then use
`voice = "clone:narrator"`. 409 if the clip exists and `overwrite` is false;
400 if the preview is shorter than 1 s or longer than 60 s.

## POST /api/lab/warm

`{"model": "custom_voice" | "base" | "voice_design"}` → loads it now so the
next render is fast. Response: the `engine` object from the catalog.
