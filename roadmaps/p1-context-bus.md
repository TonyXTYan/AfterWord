# P1: Context bus from Claude Code alone

Outlook "done when": at least 30% of follow-up suggestions get sent, as-is or edited.

## Goals

1. Record every session's hook events as typed, timestamped rows (a bus), from all sessions where the plugin is installed.
2. Show them on a GUI timeline across sessions.
3. Produce the first suggestions from session history alone: follow-ups after failed tests, stalled sessions, repeated manual steps.
4. Prove the suggestion lifecycle (live, accepted, edited, dismissed, expired, superseded, as in outlook §3) and its UI before any sensor work.
5. Measure the acceptance metric from real prompts, not from button clicks.

Not in P1: screen or mic input, an LLM suggester, a menu-bar panel. Suggestions in P1 come from rules.

## Starting point

- One `events` table in `afterword/daemon/log.py`, built around speech: `kind` is `hook | test | system | alert`, and `status` is rewritten as speech moves through the queue. Indexes on `session_id` and `clip` only.
- No schema version. `executescript(SCHEMA)` only creates missing tables, so new columns would silently not appear on existing databases.
- Retention on `events` is by age only: `[daemon] keep_days` (7 by default) is pruned hourly by `Daemon.retention_loop`, with the rows' clips. Clip WAVs are also capped by count (`history = 50`).
- Hooks today: SessionStart, UserPromptSubmit (sync, 5 s), Stop, PermissionRequest, Notification, MessageDisplay (async). Codex runs four of these synchronously. Not captured: PreToolUse, PostToolUse, SubagentStop, SessionEnd, PreCompact.
- The daemon parses the turn id (`prompt_id`) but doesn't store it.
- The Transcript loads `/api/events?limit=500` (`loadEvents` in `afterword/ui/index.html`; 200 is only the API default), and `session_activity()` counts every `kind = 'hook'` row. **Tool events must not go into `events`**: they would push spoken lines off the page, inflate session counts and flood SSE.

## Event schema

A new append-only `bus` table in the same SQLite file, added by a v2 migration keyed on `PRAGMA user_version` (see decision D9 for the alternative).

### `bus` fields

| Field | Type | Notes |
|---|---|---|
| `id` | integer pk | Monotonic; the timeline pages on `after_id`. |
| `ts` | real | Event time from the hook. |
| `recv_ts` | real | Daemon receive time; differs from `ts` for spooled events. |
| `v` | integer | Body version for `type`, starts at 1. |
| `source` | text | `claude`, `codex`, `afterword`, `gui`. Later: `sense`, `stt`. |
| `type` | text | See kinds below. |
| `session_id`, `turn_id` | text | `turn_id` = `prompt_id` (Claude) or `turn_id` (Codex sends no `prompt_id`). |
| `cwd`, `project` | text | `project` = git root of `cwd`. |
| `tool`, `tool_use_id`, `ok`, `dur_ms` | text, text, int, real | Tool events only. |
| `summary` | text | At most 300 chars, redacted. What the timeline shows. |
| `data` | text (JSON) | Typed body per `type`, redacted and capped. Set to NULL by retention. |
| `voice_event_id` | integer | Links to the `events` row when this hook message also produced speech. This is the only link; `events` gets no `bus_id` column. |
| `delivery` | text | `socket` or `spool`. |
| `redactions` | integer | Count of values the redactor replaced. |

Indexes: `(session_id, ts)`, `(type, ts)`, unique `(session_id, tool_use_id, type)` where `tool_use_id` is not null (dedupes double plugin installs).

### Event kinds

| `type` | From | Body (`data`) keeps |
|---|---|---|
| `session.start`, `session.end` | SessionStart, SessionEnd | source, reason |
| `prompt.submit` | UserPromptSubmit | prompt text (redacted), length |
| `turn.stop` | Stop | last 2 KB of the assistant message, `<say>` count |
| `subagent.stop` | SubagentStop | agent type, reason |
| `tool.pre` | PreToolUse (Bash only) | command |
| `tool.post`, `tool.fail` | PostToolUse | Bash: command, exit code, interrupted, last 2 KB of stdout and stderr, parsed test summary. Edit/Write: `file_path` only. Never Read or Grep bodies. |
| `permission.request`, `notification` | same-named hooks | tool, message |
| `compact.pre` | PreCompact | trigger |
| `message.display` | MessageDisplay | text length, `<say>` count (the early-speech hook; one row per message) |
| `voice.spoken` | daemon | profile, text, clip id |
| `suggestion.created`, `.revised`, `.withdrawn`, `.expired`, `.superseded`, `.accepted`, `.dismissed` | daemon | suggestion id, reason. `.accepted` covers exact and edited matches; `.dismissed` is kept as a negative example. |

Example row:

```json
{"id": 18412, "ts": 1764079210.4, "source": "claude", "type": "tool.post",
 "session_id": "b7e1…", "turn_id": "p_31…", "project": "~/GitHub/AfterWord",
 "tool": "Bash", "tool_use_id": "toolu_01…", "ok": 0, "dur_ms": 41250,
 "summary": "uv run pytest -q → exit 1, 3 failed, 960 passed",
 "data": {"command": "uv run pytest -q", "exit": 1,
          "tests": {"runner": "pytest", "failed": 3, "passed": 960,
                    "names": ["tests/test_queue.py::test_supersede", "…"]},
          "stderr_tail": "…"},
 "redactions": 0}
```

### Suggestion tables

These hold the suggestion object from outlook §3 and its history. Suggestions are also the P5 training data.

```sql
CREATE TABLE suggestions (id TEXT PRIMARY KEY, kind TEXT, text TEXT, target_session TEXT,
  rationale TEXT, evidence TEXT /* JSON list of bus ids */, confidence REAL, fingerprint TEXT,
  created REAL, updated REAL, ttl_s REAL, status TEXT /* live|accepted|edited|dismissed|expired|superseded, as in outlook §3 */);
CREATE TABLE suggestion_actions (id INTEGER PRIMARY KEY, suggestion_id TEXT, ts REAL,
  action TEXT /* shown|copied|tmux|matched_exact|matched_edited|dismissed|expired|superseded|withdrawn */,
  detail TEXT /* JSON: similarity, edit_distance, bus_id */);
```

### Wire protocol

- Hook message goes from `"v": 1` to `"v": 2` with a `bus` body. The daemon keeps accepting v1.
- Each hook message writes exactly one `bus` row. If it also produces speech, `voice_event_id` links them.
- v2 messages carry the hook's package version. The daemon logs and shows a warning when hook and daemon versions differ (closes the silent skew after plugin updates).

## Hooks to add, and their cost

Measured baseline: bare `python3` start-up with stdlib imports is about 20-30 ms on this Mac, plus one socket connect and send with no ack (connect timeout 0.15 s).

| Hook | Mode | Volume | Cost to Claude | Notes |
|---|---|---|---|---|
| PostToolUse, matcher `Bash\|Edit\|Write\|MultiEdit\|NotebookEdit\|Task` | async, timeout 5 | 10-50 per turn | none to Claude's latency (async), but one interpreter spawn of CPU each | `hooks/hooks.json` is static: if these are listed there, they spawn on every tool call for every user, even with `record = "lifecycle"`. So ship them as an opt-in: a separate hooks file installed by `afterword install-hooks --tools`, not in the default `hooks.json`. |
| PreToolUse, matcher `Bash` | async | about half of the above | none to latency; same spawn cost, same opt-in | Keep it print-nothing anyway, since stdout from a PreToolUse hook is read as a permission decision if the mode is ever changed to sync. Used for duration and hung-command detection. |
| SubagentStop, SessionEnd, PreCompact | async | rare | none | SessionEnd withdraws that session's suggestions. |

Rules for tool events:

- Tool hooks are registered only when `record = "tools"` (opt-in install above), because each one costs a spawn (10-50 per turn) for every user otherwise.
- A separate small entry point, not `afterword/hooks/afterword_hook.py`. Stdlib only, Python 3.9 compatible.
- Dedupe on `tool_use_id` (the unique index).
- When the daemon is down, write to a separate `bus.spool` capped at 2 MB, or drop. Never use the shared 10 MB spool that Stop records depend on.
- Codex stays on lifecycle events: its hooks are synchronous, so each tool hook would add about 30-60 ms to every tool call. Revisit after measuring with the existing `hook_ms` stat (decision D15).

## Storage, retention and redaction

| Setting | Default | Where |
|---|---|---|
| `[bus] record` | `"lifecycle"` (the events captured today). `"tools"` adds tool hooks; `"off"` writes nothing. | config; decision D10 |
| `bus.data` kept | 3 days, then set to NULL | hourly prune in `spool_poller` (`afterword/daemon/server.py`) |
| `bus` rows kept | 14 days | same |
| `events` (voice) kept | `[daemon] keep_days`, 7 days by default (no row cap) | `Daemon.retention_loop` |
| `suggestions` and actions kept | 180 days | same |
| `auto_vacuum` | `INCREMENTAL`, set in the v2 migration with one `VACUUM` | `afterword/daemon/log.py` |
| Pause | A pause flag stops bus writes; voice keeps working. Shown in the GUI header. | config and GUI |

Redaction: one stdlib `redact()` in `afterword/hooks/common.py`, run in the hook before sending or spooling, so secrets never reach disk. It replaces `AKIA…`, `ghp_` / `gho_`, `sk-…`, `xox[bp]-`, PEM blocks, `Bearer …`, `KEY=value` lines in env style, Luhn-valid card-like numbers, and emails when configured. It counts replacements in `redactions`. Tests cover each pattern and a no-false-positive set (git SHAs, UUIDs, test names).

Database writes move to a writer thread with batched `executemany`, so tool-event bursts can't stall playback scheduling on the event loop.

## First suggestion types

All are rules over `bus` rows. Each suggestion lists the bus ids it came from as evidence.

| Type | Fires when | Suggested prompt | Withdrawn when |
|---|---|---|---|
| Follow-up after failed tests | A `tool.post` Bash matching a test runner (pytest, npm/pnpm test, jest, vitest, cargo test, go test) exits non-zero or parses "N failed", with no passing run later in the same turn. Evaluated at `turn.stop`. | "Fix the N failing tests in <file>: <names>", targeted at that session | a later passing run; a matching `prompt.submit`; `session.end`; TTL 30 min |
| Stalled session | No `prompt.submit` or `tool.*` for 5 min after: a `permission.request` or `notification`; a `turn.stop` whose last message ends in a question; or a `tool.pre` with no `tool.post` (hung command) | "Session B is waiting on <reason>" with a reply suggestion where one fits | any new event in that session; `session.end` |
| Repeated manual step | The same normalised prompt, or the same user-typed Bash command, appears 3 or more times across sessions in 7 days (hash of shingled tokens) | Re-send it, or "turn this into a /command" | dismissed; TTL 1 day |

Suggested prompts are built from untrusted text (test names, file paths, the assistant's last message), which outlook §7 treats as evidence, never instructions. Rules: names and paths are length-capped (80 chars each, at most 5 names) and stripped of control characters and newlines; each prompt comes from a fixed template with values inside quotes; free text from tool output or the assistant message is never copied into a suggestion. A test feeds test names and messages containing instructions ("ignore previous instructions", newlines, markdown) and asserts the output matches the template with the values quoted and capped.

Only follow-ups count toward the P1 metric. The other two are measured but not gated.

## Surfaces

1. **Timeline tab** (fifth tab in `afterword/ui/index.html`). One swimlane per session, colour-keyed as on Sessions: prompt markers, tool ticks coloured by `ok`, test badges, Stop blocks, spoken lines in their speaker colour, shaded spans for permission waits and stalls.
2. **Suggestions rail** beside the timeline: status, text, evidence chips that jump to the cited rows, and Copy, Dismiss and (optional) Send via tmux.
3. **Send** = copy to clipboard first. tmux `send-keys` is optional and never presses Enter (outlook §6: a human always presses send). See decision D5.
4. **API**: `GET /api/bus?after_id=&session_id=&types=&limit=`, `GET /api/suggestions`, `POST /api/suggestions/{id}/action`. A new SSE message `bus`, summary only, coalesced to at most 4 per second. `/api/events` stays voice-only. Document in `docs/gui-api.md`.

## Metrics and how they are measured

Copy and tmux show intent only. Ground truth is the next `prompt.submit` in the target session.

| Metric | Definition | How |
|---|---|---|
| Follow-up acceptance (P1 gate, ≥ 30%) | (`matched_exact` + `matched_edited`) / shown, follow-up type only | On each `prompt.submit`, compare it with that session's suggestions that were live, or copied in the last 15 min, and shown before the prompt. Similarity ≥ 0.9 is `matched_exact`; 0.5 to 0.9 is `matched_edited` with edit distance logged. "Shown" means visible for at least 1 s. Matching runs online, so results survive redaction and pruning. |
| Copy rate | copied / shown | `suggestion_actions` |
| Edit distance | normalised diff, suggestion vs sent prompt | stored on `matched_edited` |
| Stale rate | Outlook §5: visible suggestions whose fingerprint no longer matches. Until a fingerprint exists (P4), P1 uses the proxy: shown suggestions later withdrawn or expired without action. | `suggestion_actions`; switch to the fingerprint definition when `suggestions.fingerprint` is populated |
| Time to suggestion (p50 < 3 s) | Context event (the triggering `bus` row) to suggestion visible | `bus.recv_ts` vs the `shown` action |
| Interruption rate (< 2 per hour) | Suggestions of any type dismissed within 5 s of being shown, per hour | `suggestion_actions`; M6 checks it for stall suggestions only |
| Resource cost (< 2% idle CPU, < 2 GB) | Daemon idle CPU and RSS | sampled by the daemon, shown in the metrics panel |
| Per-type breakdown | all of the above per suggestion type | metrics panel |

Read the gate only after at least 50 shown follow-ups.

## Milestones

### M0 Prep: split the GUI file

- [ ] Split `afterword/ui/index.html` into ES modules served statically, no framework, no build step (decision D8).
  *Done when:* page behaviour is unchanged, all `tests/test_ui_*.py` pass, and no single file is over 1,500 lines.

### M1 Schema versioning and the bus table

- [ ] Add a migration runner on `PRAGMA user_version` in `EventLog.__init__`. v2 creates `bus`, `suggestions`, `suggestion_actions`; adds `events.turn` (no `bus_id`: `bus.voice_event_id` is the single link); adds `events.reason_code` and `events.reason_detail` (used by P0-4); backfills `bus` from existing `kind = 'hook'` rows and from `kind = 'alert'` rows (mapped to `permission.request` or `notification`); sets `auto_vacuum`.
- [ ] Store the turn id on `events`.
  *Done when:* tests show a fresh database and a v1 database with data both end at v2 with the same schema; running the migration twice changes nothing; it works on a WAL database with a second connection open but idle (the one `VACUUM` cannot run while another connection holds a read transaction, so the migration runs at daemon startup before it accepts hook traffic, and the test asserts it fails cleanly if a reader is active); backfilled row count equals the hook plus alert row count.

### M2 Record lifecycle events to the bus

- [ ] `[bus] record` setting, default `"lifecycle"`, and a pause flag.
- [ ] Hook message v2 with a `bus` body and the hook version; daemon accepts v1 and v2 and warns on version skew.
- [ ] `redact()` in the hook path.
- [ ] Hourly retention prune.
- [ ] One ingestion function in the daemon for bus rows (pulled out of `afterword/daemon/server.py`).
  *Done when:* each of today's six hook events (including `MessageDisplay` as `message.display`) writes exactly one `bus` row (test); speech rows link through `voice_event_id`; redaction tests pass; a prune test with a fake clock deletes and nulls by the table above; `/api/events` output is unchanged.

### M3 Tool hooks

- [ ] New tool-hook entry point; PostToolUse, PreToolUse (Bash), SubagentStop, SessionEnd, PreCompact in a separate opt-in hooks file, registered only when `record = "tools"`.
- [ ] Separate `bus.spool`; dedupe on `tool_use_id`; test-summary parser for pytest, jest/vitest, cargo, go.
- [ ] Writer thread with batched inserts.
  *Done when:* measured hook overhead is reported (p50/p95 per event type, in the GUI stats); PreToolUse prints nothing (test, kept as a guard even though the hook is async); the default `hooks/hooks.json` registers no tool hooks and `afterword install-hooks` without `--tools` leaves them out (test); a 200-event burst does not delay a queued playback start by more than 50 ms (test); no tool event appears in `events`.

### M4 Timeline

- [ ] Timeline tab, `/api/bus`, SSE `bus` message.
  *Done when:* a day with 3 sessions renders all lanes; scrolling back 24 h loads in pages; clicking a row shows its redacted `data`; UI tests cover lanes and paging.

### M5 Suggestion store and the first type

- [ ] Suggestion engine with the lifecycle (create, revise, withdraw, expire), the failed-tests follow-up rule, the suggestions rail with Copy and Dismiss, `bus` rows for each suggestion event.
  *Done when:* a scenario test drives failing then passing test runs and sees the suggestion created, then withdrawn; evidence chips open the cited rows; Copy puts the exact text on the clipboard.

### M6 Stalled sessions and repeated steps

- [ ] The two other rules.
  *Done when:* scenario tests for each trigger and each withdrawal; with three real sessions over a day, fewer than 2 stall suggestions an hour are dismissed within 5 s.

### M7 Metric

- [ ] Prompt matching against shown suggestions; metrics panel with the table above and a per-type breakdown.
  *Done when:* matching tests cover exact, edited and unrelated prompts; the panel shows follow-up acceptance with its sample size; after 50 shown follow-ups the number is read and recorded on the board in [README.md](README.md).

### M8 Optional: send via tmux

- [ ] Map session cwd to a tmux pane; `send-keys` without Enter.
  *Done when:* the text appears in the right pane's input and is not submitted; sessions not under tmux show only Copy.

P1 is done when M0 to M7 are ticked (M0 is required before M4) and follow-up acceptance is at least 30% over at least 50 shown.

## Risks

| Risk | Mitigation |
|---|---|
| Database lock: tool volume stalls playback | Writer thread with batched writes (M3), test with a burst |
| Shared spool fills up and Stop records are lost | Separate, capped `bus.spool` |
| Secrets in prompts and tool output | Redact in the hook before sending; keep only `file_path` for edits; null `data` after 3 days |
| PreToolUse output read as a permission decision | Print nothing; test it |
| Codex latency from synchronous hooks | Lifecycle only for Codex until measured |
| False matches inflate the metric | Require shown-before-prompt and a similarity threshold; report copy rate alongside |
| Session id changes across resume or compact | Key the timeline on project plus session; link via `compact.pre` and `session.start` source |
| Migration on a live WAL database | Idempotent steps, run at daemon startup, tested with an idle second connection; `VACUUM` needs no active readers |
| `index.html` growth makes UI work slow | M0 before M4 |
| Redacted prompts and 2 KB of Bash output sit in plaintext SQLite for 3-14 days, while outlook §7 lists encryption at rest unconditionally | Explicit deviation, see decision D17: `0600` file mode (`umask(0o077)` in `afterword/daemon/server.py`), FileVault, redaction in the hook, `data` nulled after 3 days. Owner to ratify before M3 or move the decision forward. |

## Open questions

- Is 5 minutes the right stall threshold, and should it differ for permission waits? Start at 5 and tune from dismissals.
- Should `prompt.submit` keep full prompt text for 3 days, or only a hash plus the similarity results? (decision D11)
- Should repeated-step detection read user-typed Bash commands from `tool.pre`, given Claude also types commands? Needs a way to tell them apart; start with prompts only if not.
- Does the Codex plugin expose tool hooks with the same payloads? Check before M3.
