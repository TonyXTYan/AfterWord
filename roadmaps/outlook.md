# AfterWord — Outlook

Where AfterWord is heading after v1 (see `DESIGN.md`). The point of this file is to agree on the end goal, the order of work, and how we'll measure progress. Each phase needs to deliver something you'd use every day on its own merits.

---

## 1. North star

> A local, always-on context engine that keeps an up-to-date picture of what I'm doing — what I say, what's on screen, which files are open, and every Claude Code session — and offers **ready-to-send prompts** for the right session. It **revises and withdraws** them as things change, and **speaks results back** when work finishes.

The full loop:

```
 sense ──► understand ──► suggest ──► I accept/edit/ignore ──► Claude acts ──► AfterWord speaks
   ▲                                                                                │
   └──────────────────────────── outcome feeds back into context ◄──────────────────┘
```

v1 (voice out) is the last step of that loop. Everything after it fills in the steps before.

---

## 2. Principles

1. **Local-first.** Capture, speech-to-text, context and suggestions all run on the Mac. Any cloud call is opt-in, per feature, and shown in the GUI.
2. **Structured over pixels.** Use the accessibility tree, file paths and hook payloads first. OCR comes second and vision models last. Structured data is cheaper, exact and easy to diff.
3. **Event-driven, not polling.** React to app switches, focus changes, the end of an utterance, and hook events. CPU use should be near zero when idle.
4. **Suggestions are cheap to ignore.** They never steal focus, never auto-send, and go away when they stop being relevant. A wrong suggestion should cost one glance.
5. **Edits, not regeneration.** The suggester changes a persistent set of suggestions (add, revise, merge, dismiss). It never rewrites the whole list, so suggestions stay stable and the work per update stays bounded.
6. **Everything observable.** Each suggestion can show the evidence it came from. The debug GUI is a core feature.
7. **Privacy built in.** A pause hotkey, per-app exclusions, redaction, retention limits, and no raw audio kept by default.

---

## 3. Target architecture

```
SENSORS                    CONTEXT                         SUGGESTER                 SURFACES
────────                   ───────                         ─────────                 ────────
Claude Code hooks ─┐       event bus (typed, timestamped)  working-memory summary    menu-bar panel
  (all sessions)   │       ├─ raw events (SQLite, TTL)     + current suggestion set  hotkey palette
Screen/AX helper ──┼─────► ├─ entities: sessions, projects,──► LLM returns edit ops ──► inline "send to
  (Swift)          │       │   files, apps, people             add/revise/merge/       session X"
Mic → VAD → STT ───┤       ├─ rolling summary (N min)          dismiss                 AfterWord voice
Clipboard/files ───┘       └─ context fingerprint          ranker + lifecycle        GUI timeline/debug
                                                                    ▲
                                    feedback: accepted / edited / dismissed / expired
```

**Processes**
- `afterwordd` (Python): event bus, store, suggester, TTS, GUI. Grows out of the v1 daemon.
- `afterword-sense` (Swift, signed helper): accessibility tree, app/window focus, selected text, front document path, ScreenCaptureKit + Vision OCR fallback, and possibly the mic via SpeechAnalyzer. Swift is used because the macOS permission model (TCC) and the native APIs work best from a signed native binary.
- Claude Code plugin: the hooks send every session's events to the bus, not only speech events.

**Suggestion object**
```json
{
  "id": "s_0193",
  "text": "Add a dry-run flag to the Stop hook and log skipped reasons",
  "target": {"session_id": "…", "cwd": "~/code/AfterWord"},
  "rationale": "You said 'I can't tell why it stayed silent' while viewing hook.py",
  "evidence": ["evt_8812 (speech)", "evt_8790 (AX: hook.py focused)"],
  "confidence": 0.72,
  "fingerprint": "app=Code;file=hook.py;topic=silent-hook",
  "created": "…", "updated": "…", "ttl_s": 900,
  "status": "live | accepted | edited | dismissed | expired | superseded"
}
```

**Lifecycle rules for dismissal**
- *Context drift:* the current fingerprint moves far enough from the suggestion's fingerprint and stays there for longer than a grace period.
- *TTL* runs out without the suggestion being reinforced.
- *Superseded:* the LLM merges or revises it into another suggestion.
- *Done:* a `UserPromptSubmit` in the target session closely matches the suggestion, or a `Stop` result shows the task is complete.
- *Explicit:* I dismiss it. That is recorded as a negative example.

---

## 4. Phases

Each phase ends with a demo I'd actually use and a metric we can check.

### P0: Voice out (AfterWord v1), in progress
Tag-gated, per-session Qwen3-TTS, the queue, and the debug GUI.
**Done when:** parallel sessions speak in different voices, don't overlap, and the GUI explains every time a reply stayed silent.

### P1: Context bus from Claude Code alone
Turn the daemon into an event bus. Collect `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` and `Stop` from **all** sessions. Add a GUI timeline across sessions.
First suggestions come from session history alone: follow-ups ("tests failed in session B → suggest fix prompt"), stalled sessions, and repeated manual steps.
**Why first:** it's the most useful signal and costs no new permissions. It also proves out the suggestion lifecycle and the UI before any sensor work.
**Done when:** at least 30% of follow-up suggestions get sent, whether as-is or edited.

### P2: Screen context
`afterword-sense` helper: frontmost app and window, focused element, selected text, document path or URL, and app-switch events. OCR only when the accessibility tree is empty. Redaction and per-app exclusions from day one.
**Adopt or build:** evaluate Shadow (MIT) and OpenCapture (MIT) as the capture layer against a small Swift helper we write ourselves. Criteria: does it give the accessibility tree, idle CPU, licence, and how easily can it emit events to our bus.
**Done when:** suggestions cite on-screen evidence correctly at least 90% of the time (spot-checked in the GUI), and idle CPU stays under 2%.

### P3: Speech in
Voice-activity detection, streaming on-device speech-to-text (Apple SpeechAnalyzer or Parakeet/Whisper via MLX), and endpointing that detects when I've stopped speaking. Diarization to tell me apart from other speakers. Transcript segments go onto the bus; audio is discarded once transcribed.
Modes: *push-to-talk* → *auto while a hotkey is held* → *always-on with pause*. Always-on only after consent is settled (see §7).
**Done when:** from the end of an utterance, a transcript segment is on the bus in under 1 s, and word error rate on my own speech is acceptable on a recorded test set.

### P4: The real suggester
Rolling working-memory summary over the last N minutes, plus the entity store, plus the current suggestions → the LLM returns edit ops (add, revise, merge, dismiss). A ranker scores relevance, novelty and confidence, and the lifecycle rules in §3 apply.
Surfaces: a menu-bar panel and a hotkey palette. "Send" copies to the clipboard, or types into the target session if it runs under tmux.
**Latency budget** (end of speech → suggestion visible): endpoint ~0.7 s + STT final ~0.2 s + LLM ops ~1.5 s + UI ≈ **under 3 s**. That decides the local model size: whatever MLX model fits, measured, not assumed.
**Done when:** acceptance (sent as-is or edited) is at least 25%, stale suggestions visible are under 10%, and there are fewer than 2 unwanted suggestions an hour.

### P5: Closed loop and learning
Use feedback signals (accepted, edited and the diff, dismissed, expired) to tune ranking, thresholds and how prompts are phrased. Build a **replay harness**: record a day's event stream (with consent, redacted), replay it through new suggester versions, and score them offline. This is how we improve without guessing.
**Done when:** each suggester change is checked by replaying recorded days before it ships.

### P6: Stretch goals
- Suggestions that can run small actions, with explicit approval each time.
- iPhone as a remote surface and push-to-talk mic.
- Suggestions that span projects (for example: "this error in session A was solved last week in project B").
- Speaking suggestions aloud when I'm away from the screen.

---

## 5. Metrics (tracked in the GUI from P1 on)

| Metric | Definition | Target (P4) |
|---|---|---|
| Acceptance rate | (sent as-is + sent edited) / shown | ≥ 25% |
| Edit distance | normalised diff between the suggestion and what was actually sent | falls over time |
| Time to suggestion | end of utterance or context event → visible | p50 < 3 s |
| Stale rate | visible suggestions whose fingerprint no longer matches | < 10% |
| Interruption rate | suggestions dismissed within 5 s | < 2 per hour |
| Resource cost | idle CPU / RAM / battery drain | < 2% / < 2 GB / minimal |

---

## 6. Non-goals

- Auto-sending prompts. A human always presses send.
- Cloud storage or syncing captured context.
- Spying on other people. Diarization exists so the system focuses on *me*, not to profile others.
- Windows or Linux before P4 works well on macOS.
- Replacing Claude Code's own UI. We sit beside it.

---

## 7. Privacy, consent and safety

- **Pause** with a global hotkey and menu-bar toggle; status always visible.
- **Exclusions:** password managers, banking, and private browsing by default. The user can add app or URL rules.
- **Redaction** before storage: secrets, keys, card-like numbers, emails if configured.
- **Retention:** raw events kept a few days by default, summaries longer, raw audio never.
- **Other people's voices:** recording-consent law varies by jurisdiction, so check yours. Default to mic-off or push-to-talk in meetings, and only go always-on once you know where you stand. Transcribing and then deleting the audio may not exempt you.
- **Encryption at rest** for the store. All GUI endpoints bound to localhost only.
- **Prompt injection:** screen text and transcripts are *untrusted data*. The suggester treats them as evidence and never as instructions. Suggestions are never executed, only proposed.

---

## 8. Open questions to settle

1. **One repo or two?** Keep AfterWord as the umbrella (voice now, context engine later) as a monorepo with separate packages, or split the context engine into its own repo once P1 starts?
2. **Capture layer:** adopt Shadow or OpenCapture, or own a minimal Swift helper? Decide at the start of P2 using the criteria above.
3. **Local LLM:** which MLX model meets the P4 latency budget with good edit-op quality? Settle it with a benchmark on the replay harness, not by preference.
4. **Cloud escalation:** allow an opt-in Claude Haiku/Sonnet call for hard cases (summarising long sessions)? If so, add per-call visibility and a budget cap.
5. **How a suggestion reaches a session:** clipboard only, tmux `send-keys`, or a future official Claude Code input channel?
6. **Name the suggester component** separately from the voice output?

---

## 9. Immediate next steps

1. Finish P0 per `DESIGN.md`.
2. Design the bus event schema in P0's event log now, so P1 extends it instead of migrating it.
3. Keep every hook event (even ones that don't produce speech) in the v1 event log behind a flag. That gives P1's replay harness data from day one.
