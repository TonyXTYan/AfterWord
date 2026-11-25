# Decisions

The open questions from outlook §8 (D1 to D6), plus new ones raised by the 2026-11-25 repo audits (D7 onward). Each has a recommendation; the owner decides.

Status values: **open** (no firm recommendation yet; a plan may be written), **proposed** (recommendation written, waiting on the owner), **decided** (owner chose; record the date and choice). When a decision is made, update the plans that depend on it.

## Summary

| # | Question | Recommendation | Needed by | Status |
|---|---|---|---|---|
| D1 | One repo or two? | Monorepo, separate packages | P1 M2 | proposed |
| D2 | Capture layer: adopt or build? | Own a minimal Swift helper; Spike compares Shadow, OpenCapture and an own helper on the outlook's four criteria | P2 start | proposed |
| D3 | Which local LLM? | Decide by benchmark on recorded events | P4 start | open |
| D4 | Allow opt-in cloud escalation? | Yes, off by default, with a ledger and a cap | P4 | proposed |
| D5 | How a suggestion reaches a session | Clipboard first; tmux without Enter second | P1 M5 | proposed |
| D6 | Name the suggester separately? | Yes, name the package by function now | P1 M2 | proposed |
| D7 | Assign distinct voices by default? | Yes, `auto_assign = "distinct"` on | P0-1 | proposed |
| D8 | Split `index.html` before P1's timeline? | Yes, ES modules, no build step | P1 M4 | proposed |
| D9 | New `bus` table or a new `kind` in `events`? | New table | P1 M1 | proposed |
| D10 | Default bus record level | `"lifecycle"` | P1 M2 | proposed |
| D11 | Retention and what raw text to keep | 3 days data, 14 days rows, 180 days suggestions | P1 M2 | proposed |
| D12 | Daemon as a launchd service | Yes, before any Swift helper | P0-6 | proposed |
| D13 | Menu-bar surface | Native Swift menu-bar app for P4; SwiftBar only for status | P4 | proposed |
| D14 | Fast test tier | Yes, split fast and slow | before P1 M3 | proposed |
| D15 | Tool hooks for Codex | Not until measured | P1 M3 | proposed |
| D16 | Hook runtime per event | Keep `python3` for now; measure; small client if p95 is high | P1 M3 | open |
| D17 | Encryption at rest | Defer to P2; FileVault plus file permissions until then | P2 | open |

## Outlook §8 questions

### D1 One repo or two

- **Options:** monorepo with separate packages; split the context engine into its own repo when P1 starts.
- **Recommendation:** monorepo. Keep `afterword/` as the voice package, add a sibling package for the bus and suggester, and a `sense/` folder for Swift sources. Hooks stay stdlib-only.
- **Reasoning:** hooks, daemon, event log and GUI are shared today. Splitting before the bus schema settles means pinning versions across repos.
- **What would change it:** a second consumer of the bus that isn't voice, contributors who want only one half, or the Swift helper needing its own release and signing cadence. Revisit after P2.
- **Status:** proposed.

### D2 Capture layer

- **Options:** adopt Shadow (MIT); adopt OpenCapture (MIT); own a minimal Swift helper.
- **Recommendation:** own a small helper for AX focus, app switch and document path. The P2 spike still compares Shadow and OpenCapture with it on the outlook's four criteria (AX tree, idle CPU, licence, ease of emitting events); narrowing to OCR only depends on this decision being made.
- **Reasoning:** the signing and TCC identity problem is the same whichever is picked. AX events are small, and the repo already has the compile-and-run pattern (`afterword/daemon/apple.py`).
- **What would change it:** poor idle CPU or AX coverage in the P2 spike, or a library that gives capture and OCR with low idle cost.
- **Status:** proposed. Settle at P2 start per the outlook.

### D3 Local LLM

- **Options:** 3-4B instruct models at 4-bit via MLX; larger models if memory allows; cloud for hard cases (D4).
- **Recommendation:** none yet. Run the P4 spike: replay a recorded day, require valid edit-op JSON on at least 95% of cases, and meet the 1.5 s LLM share of the 3 s budget.
- **Reasoning:** memory is the binding constraint. Two warm TTS models already take about 6 GB (`keep_loaded = 2`).
- **What would change it:** 32 GB or more of RAM lets a larger model fit; poor edit-op quality pushes hard cases to D4.
- **Status:** open.

### D4 Cloud escalation

- **Options:** never; opt-in per feature; on by default.
- **Recommendation:** opt-in, off by default, copying the `claude -p` runner in `afterword/daemon/alerts.py` (uses the existing Claude Code login, has timeout and cancellation, testable). Before first use, add a ledger table (time, model, input size, bytes sent), a monthly cap, and a GUI view of each call. Send only redacted summaries, never raw screen text.
- **What would change it:** if any screen-derived text leaving the Mac conflicts with outlook §2.1, limit escalation to hook-derived session summaries.
- **Status:** proposed.

### D5 How a suggestion reaches a session

- **Options:** clipboard; tmux `send-keys`; `UserPromptSubmit` `additionalContext`; a future official input channel.
- **Recommendation:** clipboard first. tmux `send-keys` second, never sending Enter, so a human always presses send (outlook §6). Use `additionalContext` only for context hints: it fires when the user submits, so it can't deliver a suggestion.
- **What would change it:** an official Claude Code input channel or MCP-style channel should replace tmux if one ships. tmux needs a session-to-pane map, which isn't tracked today.
- **Status:** proposed.

### D6 Name the suggester

- **Options:** keep everything as "AfterWord"; name the context engine separately now; wait until P4.
- **Recommendation:** name the package directory by function now (for example `context/`), and pick a product name later.
- **Reasoning:** matches D1 at no cost. A product name can wait until P4.
- **Status:** proposed.

## New questions from the audits

### D7 Assign distinct voices by default

- **Options:** keep voices opt-in (today); auto-assign the least-used free profile at first speech.
- **Recommendation:** auto-assign on by default, with `auto_assign = "off"` to opt out.
- **Reasoning:** P0's "done when" needs different voices without the user doing anything.
- **What would change it:** if users want one voice for all sessions, keep it on but let a global `AFTERWORD_VOICE` win.
- **Status:** proposed.

### D8 Split the GUI file

- **Options:** keep one `afterword/ui/index.html` (about 5,600 lines); split into ES modules served statically; adopt a framework with a build step.
- **Recommendation:** ES modules, no framework, no build step, before P1 M4.
- **Reasoning:** the timeline, suggestions rail and metrics panel all add to this file. Splitting is cheap now and costly later. No build step keeps the plugin install simple.
- **Status:** proposed.

### D9 Where bus events live

- **Options:** a new `bus` table; a new `kind` in `events`.
- **Recommendation:** new append-only `bus` table in the same database.
- **Reasoning:** `events` rows are mutable speech records that the Transcript and `session_activity()` read. Tool events there would push spoken lines off the 500-row page, inflate session counts and flood SSE.
- **Deviation from outlook §9.3:** §9.3 says to keep every hook event in the v1 event log behind a flag. This puts them in a separate `bus` table instead, for the reasons above. Owner to ratify. (README says the outlook wins on disagreement.)
- **Status:** proposed.

### D10 Default bus record level

- **Options:** `"off"`, `"lifecycle"`, `"tools"`.
- **Recommendation:** `"lifecycle"` by default; `"tools"` once redaction and the writer thread land (P1 M3).
- **Reasoning:** outlook §9.3 asks to keep every hook event from day one. Lifecycle events are already received today, so this adds no hook cost.
- **Deviation from outlook §9.3:** the outlook's "every hook event" would include tool events by default. Defaulting to lifecycle only keeps hook cost at zero for users who haven't opted in (tool hooks are an opt-in install, see P1 M3). Owner to ratify.
- **Status:** proposed.

### D11 Retention and raw text

- **Options:** keep full prompt text for N days; keep only hashes and match results.
- **Recommendation:** `bus.data` 3 days, rows 14 days, voice `events` 90 days or 100k rows, suggestions 180 days. Keep prompt text for 3 days (redacted), since matching runs online.
- **What would change it:** if prompts prove sensitive in practice, store only a hash plus similarity results.
- **Status:** proposed.

### D12 Daemon as a service

- **Options:** foreground `afterword serve` (today); LaunchAgent via `afterword install-service`; a menu-bar app that owns the daemon.
- **Recommendation:** LaunchAgent now, before any Swift helper or menu-bar app.
- **Reasoning:** when the daemon is down, voices fall back to `say` and P1 events go to the spool. P2 and P3 need an always-on process.
- **Status:** proposed.

### D13 Menu-bar surface

- **Options:** the SwiftBar plugin and overlay page planned in `docs/research-v3.md`; a native Swift menu-bar app (possibly hosted by `afterword-sense`).
- **Recommendation:** use the SwiftBar/overlay track for status only (closes README's "menu-bar icon"). Build the P4 suggestion panel as a native app. Don't build two panels.
- **Reasoning:** SwiftBar menus can't host an interactive, live-updating suggestion list.
- **Status:** proposed.

### D14 Fast test tier

- **Options:** keep one serial run (132 s); add `pytest-xdist`; mark slow tests and run a fast tier by default.
- **Recommendation:** mark real-time and sleep-based tests as slow, make the default run fast, run everything in CI. Try `pytest-xdist` after.
- **Status:** proposed.

### D15 Tool hooks for Codex

- **Options:** same tool hooks as Claude Code; lifecycle only.
- **Recommendation:** lifecycle only until the existing `hook_ms` stat shows the cost. Codex hooks run synchronously (`hooks/codex-hooks.json`), so each tool hook would add roughly 30-60 ms per tool call (an estimate; the measured baseline is 10-40 ms for the interpreter alone).
- **Status:** proposed.

### D16 Hook runtime per event

- **Options:** keep spawning `python3` per event; a tiny compiled client that writes to the socket; a long-lived helper.
- **Recommendation:** none yet. Measure p50/p95 per hook in P1 M3. If tool-hook CPU or latency is noticeable, write a small client for tool events only.
- **Status:** open.

### D17 Encryption at rest

- **Options:** rely on FileVault and file modes; SQLCipher; encrypt only `data` columns.
- **Recommendation:** none yet. Until screen or mic data exists, FileVault plus `0600` files under `~/.afterword` is the floor. Decide before P2 stores screen text.
- **Deviation from outlook §7:** the outlook lists encryption at rest unconditionally, but P1 already stores redacted prompts and 2 KB of Bash stdout and stderr in plaintext SQLite for 3-14 days. `0600` mode is confirmed (`umask(0o077)` in `afterword/daemon/server.py`). Either bring this decision forward to before P1 M3, or the owner ratifies the deviation for P1.
- **Status:** open.
