# P2 to P6: later phases

One section per phase. Each lists what the repo already gives us, what is missing, a first spike small enough to answer the biggest unknown, and the exit criteria from `outlook.md`. Turn a section into its own file when that phase starts.

## Prerequisites for all of them

These come out of P0 and P1 and should be in place before P2 starts:

- [ ] Typed, timestamped bus with redaction and retention ([P1 M1, M2](p1-context-bus.md#milestones)). Check: the M1 and M2 done-when tests pass in CI.
- [ ] A pause switch that every sensor honours (P1 M2 adds the flag; P2 adds the hotkey). Check: with the flag set, a test sensor's events are not written.
- [ ] The daemon running as a service ([P0-6](p0-voice-out.md#checklist)). P2 and P3 sensors need an always-on process. Check: the P0-6 done-when passes (restart after a crash).
- [ ] A stable, signed path for native helpers (below). Check: rebuild the helper twice and the Accessibility grant survives.

## P2 Screen context

**Exit criteria (outlook):** suggestions cite on-screen evidence correctly at least 90% of the time (spot-checked in the GUI), and idle CPU stays under 2%.

| Already in the repo | Gap |
|---|---|
| A build-and-cache pattern for a Swift helper: `afterword/daemon/apple.py` compiles `afterword/daemon/apple/avspeech.swift` with `swiftc` to `~/.afterword/bin/avspeech-<sha12>` | Hash-named, unsigned binaries. TCC ties Accessibility and Screen Recording grants to the binary's identity and path, so grants would reset on every rebuild. Needs a fixed path (`~/.afterword/bin/afterword-sense` or an `.app` with a bundle id) and a stable signature (decision D2). |
| A subprocess watchdog and line-based protocol (`apple.py`) | An AX event protocol to the bus: newline-delimited JSON on stdout is enough. |
| `AFTERWORD_APPLE_HELPER` override as a seam for swapping helpers | The same kind of override for `afterword-sense`. |
| An event-driven daemon with near-zero idle work | A GUI onboarding panel for granting Accessibility and Screen Recording, showing the real binary path. |
| | Per-app exclusions (password managers, banking, private browsing by default) and redaction of screen text. Nothing exists yet. |
| | Pause hotkey and always-visible status. |

**First spike:** about 150 lines of Swift that observe `NSWorkspace` app activation and `AXFocusedWindowChanged`, and print JSON lines (bundle id, window title, `AXDocument` path). Run it from a fixed path with an ad-hoc signature for 24 h. Pass if idle CPU stays below 2% and the AX-empty rate is recorded (set a threshold from that number). Also compare Shadow and OpenCapture against this helper on all four of the outlook's criteria (AX tree, idle CPU, licence, ease of emitting events); narrowing the comparison to OCR only waits on decision D2.

## P3 Speech in

**Exit criteria (outlook):** from the end of an utterance, a transcript segment is on the bus in under 1 s, and word error rate on my own speech is acceptable on a recorded test set.

| Already in the repo | Gap |
|---|---|
| AVFoundation experience in the Apple helper | No audio capture, VAD, endpointing, STT or diarization. |
| A single engine thread that owns MLX models (`afterword/daemon/qwen.py`) | The engine host is shaped for TTS. `docs/research-v3.md` already proposes a generic `ModelHost`; STT would be a new model kind. |
| `keep_loaded` and memory reporting for warm models | Microphone TCC grant (same signing problem as P2). |
| | A recorded test set of the owner's speech and a WER script. |
| | Consent settings: mic-off or push-to-talk by default (outlook §7). |

**First spike:** push-to-talk only, inside `afterword-sense`, using Apple `SpeechAnalyzer` (no model memory in our process). Put `stt.segment` rows on the bus and log end-of-speech to row latency. Benchmark Parakeet or Whisper via MLX afterwards against the 1 s target.

## P4 The real suggester

**Exit criteria (outlook):** acceptance (sent as-is or edited) is at least 25%, stale suggestions visible are under 10%, and fewer than 2 unwanted suggestions an hour. Latency from end of speech to visible suggestion: p50 under 3 s (the outlook §5 metric, checked here too).

| Already in the repo | Gap |
|---|---|
| Warm-model management: `keep_loaded` (default 2, about 3 GB each, `afterword/daemon/config.py`), memory reporting, `LIGHT_KINDS` not counted against the limit | Memory budget. Two warm TTS models already use about 6 GB; the outlook's resource target is under 2 GB. A 3-4B 4-bit LLM is about 2-3 GB. Needs its own `keep_loaded` kind and limit, or unloading TTS while the LLM is active. |
| A subprocess runner for `claude -p --model haiku` with timeout and cancellation (`afterword/daemon/alerts.py`) | No `mlx-lm` dependency, no edit-op schema (add, revise, merge, dismiss) or validator. |
| The P1 suggestion store and lifecycle | A ranker, a working-memory summary, an entity store. |
| | The engine thread is single (LIVE/LAB priority). A suggester call during synthesis would wait behind it; it needs its own priority tier or thread. |
| | Menu-bar panel and hotkey palette. `docs/research-v3.md` plans an overlay page and a SwiftBar plugin; SwiftBar suits status, not an interactive panel (decision D13). |

**First spike:** offline, no UI. Take a recorded day of bus events from P1, run 2-3 MLX models over a fixed prompt, and measure time to first edit op and the share of valid edit-op JSON (target 95% or more). This is the replay harness in miniature and it answers decision D3.

## P5 Closed loop and learning

**Exit criteria (outlook):** each suggester change is checked by replaying recorded days before it ships.

| Already in the repo | Gap |
|---|---|
| Event store with timings (`afterword/daemon/log.py`) | A replay harness and a scoring report. |
| `voice_stats` as a precedent for aggregate metrics | Consent-and-redaction pass for recorded days kept beyond retention. |
| Export (`/api/export`, `tests/test_export.py`) | Encryption at rest (decision D17). |
| P1's `suggestion_actions` (accepted, edited, dismissed, expired) | A way to version suggesters so results compare across runs. |

**First spike:** `afterword replay --from <ts> --to <ts> --suggester <name>` that reads `bus` rows, runs the P1 rule engine against them in simulated time, and prints suggestions created and how many matched the real prompts. Pass if replaying a recorded day reproduces the live suggestion count within 5% (tolerance to be confirmed). Use it to tune the P1 thresholds.

## P6 Stretch goals

**Exit criteria:** none set in the outlook. Each item gets its own "done when" when picked up.

| Item | Already in the repo | Gap | First spike |
|---|---|---|---|
| Suggestions that run small actions, with approval each time | `PermissionRequest` and `Notification` hooks show approval-style flows | An action model and an approval UI | none until P4 is stable. Done when: P4 exit criteria hold for 2 weeks. |
| iPhone as a remote surface and push-to-talk mic | GUI over HTTP | GUI is localhost-only by design (outlook §7). Remote access needs a new, authenticated surface. | none yet. Done when: written per item when picked up. |
| Suggestions across projects | `project` on bus rows (P1) | Cross-project search over past sessions | search `turn.stop` text for an error string across projects. Done when: results from at least two projects return in under 1 s. |
| Speaking suggestions aloud when away | TTS pipeline and spoken alerts (`afterword/daemon/alerts.py`) | Away detection | speak a suggestion through the existing alert path. Done when: it is spoken within 2 s of creation, only when away detection says away. |
