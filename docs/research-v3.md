# AfterWord v3: more voice models, and widgets (research and plan)

Research done 2026-11-25. Part 1 (more engines) is built; see "As built" at the end. Part 2 (widgets) is not. Figures for speed and
latency are vendor claims unless marked as checked. The mlx-audio model list is
from its `main` branch; PyPI's newest release is 0.6.8 (2026-11-23), and some
models may only be on `main` (check before pinning).

## Part 1: other voice models

### Where AfterWord stands today

- `afterword/daemon/synth.py` has a `Synth` protocol (`synth()` to a WAV file,
  `stream()` to a sink of float32 chunks at 24 kHz) and `make_synth()`, which
  picks **one global backend** (`qwen`, `say`, `null`).
- The backend is chosen per voice by the spec prefix (`voices.parse_voice`):
  `builtin:` / `clone:` / `design:` go to Qwen3-TTS, `say:` goes to macOS
  `say`. Profiles already carry a voice per language.
- `QwenEngine` (`qwen.py`) owns model loading, the LIVE/LAB priority queue,
  cancel and preempt, `keep_loaded`, and memory status. It is Qwen-specific
  only in `_load_model`, `_warmup`, `_render` and `build_request`.

So the natural extension is **more prefixes**, with the per-language voice map
mixing engines for free (for example Kokoro for English, a Qwen clone for
Chinese).

### Candidates

**Local, through mlx-audio** (same `load_model(repo).generate(...)` shape as
Qwen3-TTS; streaming and cloning were checked in the mlx-audio source):

| Model | Size | zh / ja / ko | Clone | Streams | Licence | Rate | Fit |
|---|---|---|---|---|---|---|---|
| Kokoro | 82M | zh, ja | no (54 presets) | per segment | Apache-2.0 | 24k | fast "lite" voice, tiny download |
| Chatterbox Multilingual v3 | ~0.5B | all three (23 langs) | yes | yes | MIT | 24k | closest drop-in to Qwen3-TTS; has an exaggeration (emotion) control |
| MOSS-TTS-Nano | 100M | all three (20 langs) | yes | yes | Apache-2.0 | 48k | small, clones, streams |
| VoxCPM2 | 2B | all three (30 langs) | yes + voice design | no | Apache-2.0 | 48k | quality option; no streaming |
| Ming-3.0 TTS (bailingmm) | 0.5B / MoE | all three | yes + presets | Tiny: yes | MIT | 44.1k | emotion and dialect tags; good for Japanese |
| Irodori v4.1 | small | ja only | yes | yes | MIT | 48k | Japanese specialist |

Avoid as defaults because the licence is non-commercial or restricted: Fish S2
and Drama 3, Higgs Audio v3, Voxtral TTS, Spark, Phonon, Reverb. Higgs v2 uses
its own community licence.

**Local, outside mlx-audio:**

- **Apple AVSpeechSynthesizer through a small Swift helper** is the better
  fallback than `say`. It is free and offline and has Premium voices for
  zh/ja/ko. `write(_:toBufferCallback:)` gives PCM buffers, so speech goes
  through AfterWord's own player (pace, queue, skip, clip history).
- **Personal Voice** needs a TCC grant (`kTCCServiceVoiceBanking`) tied to a
  signed app bundle. It does **not** support the buffer callback, so it could
  only be speak-only. This rests on Apple developer-forum reports; Apple does
  not document it.
- **sherpa-onnx / kokoro-onnx** (Apache/MIT) are a way to run without MLX.
  AfterWord is Apple Silicon only, so this is low priority.
- **Piper** (GPL-3.0) is not worth it: it is lower quality than Kokoro, and
  the licence is viral.

**Cloud (opt-in only, because the spoken text leaves the machine):**

| Provider | First audio | zh/ja | Clone | ~$ per 1M chars | Raw PCM | Notes |
|---|---|---|---|---|---|---|
| ElevenLabs Flash v2.5 / v4 Turbo | ~75–100 ms | yes | yes | $40 (v4) | `pcm_24000` | has `seed`; matches our 24 kHz pipeline |
| Gemini 3.8 Flash TTS | n/a | yes (130+) | needs consent audio | ~$15 now, ~$30 from 2027 (estimate) | L16 24k | free tier; style prompts |
| Cartesia Sonic 3 | ~90–170 ms | yes | yes | ~$37–50 | any rate | WebSocket |
| Azure Neural HD | low | strong | gated | $15–22 | 24k PCM | |
| MiniMax Speech 2.8 | low | strong | yes | $60–100 | hex PCM over SSE | strongest Chinese |

Dropped: Hume (API shut down 2026-11-13) and OpenAI gpt-4o-mini-tts (shuts
down 2027-01-06; its replacement is the Realtime API only).

### Recommendation

1. **Kokoro**: the cheapest way to prove a multi-backend design, and a
   genuinely useful fast voice.
2. **Chatterbox Multilingual v3**: a second cloning engine with streaming. The
   same clip in `~/.afterword/voices/` can be tried on both engines.
3. **AVSpeech helper**: replaces `say` as the fallback.
4. **One cloud backend, ElevenLabs**: opt-in, with the key in the Keychain or
   an env var, never in `config.toml`.
5. **Later trials**, through the voice lab, before shipping any of them:
   MOSS-TTS-Nano, VoxCPM2, Ming, Irodori.

### Design sketch (to settle before coding)

- **Voice specs.** Keep `builtin:` / `clone:` / `design:` as the Qwen forms for
  compatibility. Add new forms:
  - `kokoro:<voice>`
  - `chatterbox:<clip>`
  - `apple:<voice-id>`
  - `eleven:<voice-id>`

  Alternatively, a general `<engine>/<kind>:<value>` form. That is the first
  decision.
- **Engine host.** Split `QwenEngine` into a generic `ModelHost` (one worker
  thread, priorities, cancel and preempt, an LRU with a memory budget across
  *all* models) plus per-model adapters (`load`, `warmup`, `render(stream)`,
  `sample_rate`, `capabilities`). The cloud and AVSpeech backends are adapters
  with no model to load.
- **Capabilities per adapter**: streams, clones, seeded, native speed, style
  or mood control, languages.
  - A model that doesn't stream gets speech sentence by sentence through the
    existing script split, so first audio stays acceptable.
  - The seed explorer is offered only for seeded backends.
  - `mood` / `style` map to each backend's control, or are logged as ignored.
  - `pace` uses native speed where it exists, otherwise playback rate as now.
- **Sample rates.** Rates differ by model: 16k, 22.05k, 24k, 44.1k and 48k all
  occur. Resample to `STREAM_RATE` at the adapter boundary; `stream.resample`
  exists already.
- **Hooks and prompt.** These don't change. The injected prompt can stay the
  same.
- **GUI.** Add a backend column in timings. The lab gets A/B *across* engines
  for the same text and clip. The status page shows per-model memory and
  download state, and cost per line for cloud backends.
- **Tests.** Adapter contract tests run against a fake adapter. Real-model
  tests are skipped unless the model is downloaded, as for Qwen today.

## Part 2: widgets (clawd-on-desk and others)

### What AfterWord can offer a widget today

- `GET /api/stream` (SSE) already sends `queue` (now playing and pending, with
  the text), `sessions` (voice on/off, profile, mute), `mute` and `event`
  messages.
- **Missing pieces:**
  - **Per-line lifecycle events:** line started, segment started, line ended
    or skipped, each with session, profile, lang and text.
  - **An audio level stream** (an RMS envelope at about 30 Hz) for mouth
    movement.

  Both are cheap to add, since the stream player already has every chunk.
- Widgets can control AfterWord through the existing POST routes (with
  `X-AfterWord: 1`): skip, stop, mute all, and voice on/off per session.

### clawd-on-desk

Checked in its source, v1.7.0 from 2026-11-21:

- **What it is:** an Electron desktop pet, AGPL-3.0. Its artwork is "All
  Rights Reserved", and Clawd itself is Anthropic fan art. It has about 6.8k
  stars and is very active.
- **How it watches agents:** it installs its own Claude Code hooks
  (`hooks/clawd-hook.js` in `~/.claude/settings.json`) and Codex hooks
  (`~/.codex/hooks.json`). It listens on 127.0.0.1, on ports 23333–23337 or a
  dynamic one, and writes the live port to `~/.clawd/runtime.json`.
- **External push point:** its "Custom HTTP Agent" API
  (`docs/guides/custom-agent-http.md`). You register an app in its Settings to
  get an `agent_id`, then `POST /state` with
  `{agent_id, session_id, state, event}`.
- **Limits of that push point:**
  - It is **state-only**: there is no text field and no permission protocol.
  - The state must exist in the active theme, or it returns 400.
  - A custom agent appears as its **own** session card, separate from the
    Claude Code session Clawd already shows.
- **No speaking state, no lip-sync, no TTS.** Clawd only plays two sound
  effects (on Stop and on permission). Nobody has asked for voice in its
  issues yet.

So the options are:

1. **Now, without asking anyone:** an AfterWord adapter that posts
   `notification` or `attention` while a line plays. It works, but it's coarse:
   Clawd reacts with an existing animation on a separate card, and there is no
   bubble and no mouth.
2. **Properly: an upstream PR to clawd-on-desk.** Add an optional `speaking`
   theme state (themes can add optional states, as `executing` was), plus an
   optional `text` / `bubble` field on `/state` for custom agents. Pushing
   over HTTP does not put AfterWord under the AGPL. The PR needs maintainer
   buy-in, and new artwork, which the art licence complicates. A good first
   step is an issue proposing it.
3. **Coexistence fixes, worth doing regardless:**
   - Document that Clawd's completion and permission chimes overlap
     AfterWord's speech (turn them off in Clawd).
   - Both tools hook `Stop` and `Notification`, which is fine because Claude
     Code runs every hook. Both also merge `~/.codex/hooks.json`; check that
     each keeps the other's entries.
   - Clawd's blocking `PermissionRequest` bubble can answer a prompt while
     AfterWord is still announcing it.

### Other widgets

| Target | How AfterWord would feed it | Effort | Value |
|---|---|---|---|
| **AfterWord's own overlay page** (`/overlay`) | transparent page on the daemon, SSE for the bubble, levels for a talking avatar, plus a mute button | low | usable as an OBS Browser Source, in any always-on-top webview, and as the base for everything else |
| **SwiftBar streamable plugin** | a shell script that runs `curl -N /api/stream` and prints the state; clicks POST mute or skip | very low | **is** the unbuilt menu-bar icon, with almost no code |
| **SketchyBar** | daemon runs `sketchybar --trigger afterword_speech STATE=… TEXT=…` | low | for SketchyBar users |
| **VTube Studio** (Live2D) | WebSocket on `:8001`, `InjectParameterDataRequest` on the mouth parameter from the level stream, re-sent at least once per second | medium | real lip-sync on an avatar |
| **Stream Deck** | SDK plugin with its own SSE client; key image shows speaking, keys mute or skip | medium | a physical mute button |
| **Home Assistant** | webhook or `POST /api/states/sensor.afterword` | low (config) | lights for speech or alerts |
| **Notch and pet apps** (CodeIsland, vibe-notch, notchi) | each reads its own hook socket, and none has a public push API | medium–high | upstream PRs only; notchi (GPL, active, emotion sprites) is the most plausible |
| Raycast, Übersicht | pull-based or commands only | low | commands like mute, replay, switch voice; weak for live state |

Most of these fit one **generic "sinks" feature** in `config.toml`:

- `[[widgets]]` entries of type `clawd`, `webhook`, `command` and `vts`.
- Fire-and-forget, with short timeouts, off the playback path.
- Every send is logged in the event log like other side effects.

## Proposed order

These are independent tracks; each could be its own branch.

1. **Speech events and levels.** Add lifecycle SSE events and a level stream,
   documented in `docs/gui-api.md`. Small, and every widget needs it.
2. **Backend abstraction plus Kokoro.** Build `ModelHost` and the adapters,
   with spec prefixes, capabilities and resampling. Kokoro is the first new
   engine; the Qwen behaviour must stay the same, with the existing tests
   still passing.
3. **Overlay page plus SwiftBar plugin.** This closes the "menu-bar icon"
   item.
4. **Chatterbox Multilingual**, with cross-engine A/B in the voice lab.
5. **Widget sinks.** First Clawd (the coarse state push), then the webhook and
   command sinks; then open the clawd-on-desk issue or PR for a `speaking`
   state and bubble.
6. **AVSpeech Swift helper** to replace `say` as the fallback.
7. **Opt-in cloud backend** (ElevenLabs).
8. **VTube Studio lip-sync.**

## Open questions

- Are cloud backends acceptable at all, given that AfterWord is local-first?
  Proposed: opt-in per profile, with a warning on the config page.
- Spec syntax: new top-level prefixes (`kokoro:`), or `<engine>/<kind>:`?
- Clawd: settle for the coarse push, or go for the upstream proposal (an issue
  first)? Do you run Clawd day to day? That decides how high it ranks.
- Which mlx-audio models are in the 0.6.8 release rather than only on `main`?
  That decides whether a version bump or a git pin is needed.
- Shipping only built-in voices still holds (DESIGN.md ethics); new cloning
  engines get the same consent note.

## As built (more engines)

Built: Kokoro, Chatterbox Multilingual and Apple voices. ElevenLabs and the
other cloud backends were left out for now (local only).

- **Voice specs:** `kokoro:<voice>`, `chatterbox:<clip>` and `apple:<voice>`
  are new top-level prefixes. The Qwen forms are unchanged. `VoiceSpec.backend`
  names the engine family, which is recorded on events and previews.
- **One engine thread for every local model:** `QwenEngine` (the module and
  class keep their v1 names) hosts all of them, with the kinds `kokoro` and
  `chatterbox` next to the three Qwen3-TTS kinds.
  - It handles loading, keep-warm, LIVE/LAB priority, cancel and preempt in
    one place.
  - Kokoro is a `LIGHT_KINDS` model, so it does not count against
    `keep_loaded`.
  - Each kind has its own required files, and Chatterbox's companion repo
    (`mlx-community/S3TokenizerV2`) is pulled and checked with it.
- **Kokoro:**
  - It renders sentence by sentence (`SENTENCE_SPLIT`), so a long line
    streams.
  - The pipeline language follows the line's language when Kokoro has it,
    otherwise the voice's first letter. Korean, for example, is an error, so
    the line falls back.
  - Warm-up builds the English pipeline (spaCy), which takes about 3 s with
    the load.
  - Measured on this Mac: first audio 0.1 s in English and 0.2–0.6 s in
    Chinese and Japanese, warm.
  - It needs `misaki[en,ja,zh]` and `en_core_web_sm` (the `kokoro` extra),
    plus UniDic, which is a separate download, for Japanese.
- **Chatterbox:**
  - Multilingual v3 has no built-in voice; it always clones from a clip.
  - The clip's conditioning is computed once per clip file, and is cached by
    path, mtime and size.
  - Generation is capped at 25 speech tokens per second of plausible audio.
  - mlx-audio yields one chunk for a short line. Measured warm: first audio
    1.0–2.2 s for a 2.5–4 s line.
- **Apple:**
  - `apple/avspeech.swift` is compiled with swiftc into
    `~/.afterword/bin/avspeech-<source hash>` on first use (about 2 s). It
    streams float32 PCM at the voice's rate (22.05 kHz), which
    `StreamResampler` takes to 24 kHz without seams at chunk boundaries.
  - `apple:` alone picks the system's region for the language, then the best
    quality. Personal Voice is listed but not used.
  - `AFTERWORD_APPLE_HELPER` overrides the helper; tests set it to `none` or
    to a stub.
- **Fallback:** `[daemon] model_fallback` (`apple` by default) makes a failed
  model voice try the best Apple voice for the line's language, then `say`.
  The note on the event names both (`kokoro failed (...); spoke with apple`).
  The hook's own fallback, used when the daemon is down, is still `say`.
- **Config:** a `[models]` section holds the Kokoro and Chatterbox repos.
  `[qwen] keep_loaded` and `warm_on_start` apply to all local models.
- **Seeds:** Kokoro and Apple renders record no seed. The seed explorer
  refuses `kokoro:`, `apple:` and `say:` voices.
- **GUI:** the voice builder has Kokoro (voice list), Chatterbox (clip list)
  and Apple (installed voices, or the best one for the language) sources. The
  engine panel lists the new models.
- **Tests:** `tests/test_engines.py` covers specs, language rules, engine
  dispatch with fake models, the conditioning cache, the light-model rule,
  the Apple helper (a stub, plus a real compile when swiftc exists), the
  fallback chain, config and the lab.
