# P0: Voice out (closeout)

Outlook "done when": parallel sessions speak in different voices, don't overlap, and the GUI explains every time a reply stayed silent.

No overlap is met. Explaining silence is mostly met. Distinct voices is opt-in only. This checklist closes the gap. Background is in [current-state.md](current-state.md#what-p0-still-lacks).

## Checklist

- [ ] **P0-1 Assign a distinct voice automatically.**
  Add `[daemon] auto_assign = "distinct" | "off"`. On a session's first speech, if it has no voice from `AFTERWORD_VOICE`, `/afterword:voice` or the GUI, pick the least-recently-used free profile, using the same ranking as `/afterword:voice pick` (`afterword/hooks/afterword_hook.py`, `afterword/hooks/common.py` `VOICE_IN_USE_S`). Default: see decision D7.
  *Done when:* with `auto_assign = "distinct"` and at least three free profiles, three new sessions with no voice set each get a different profile, and the choice shows in Sessions with "auto" as its source.

- [ ] **P0-2 Warn when two live sessions share a voice.**
  Today the only hint is the "used by" dots in `afterword/ui/index.html`.
  *Done when:* Sessions shows a warning on both rows when two sessions active within `VOICE_IN_USE_S` use the same profile, with a one-click "pick another" action; a `tests/test_ui_*.py` test covers it.

- [ ] **P0-3 Scenario test: three sessions, three voices, no overlap.**
  *Done when:* a test starts three sessions with no voice set, sends overlapping Stop events, and asserts three distinct profiles, strictly sequential playback, and one logged decision per reply. It runs in CI.

- [ ] **P0-4 Make skip reasons a fixed set.**
  Split `reason` into `reason_code` (enum: `session_off`, `no_tag`, `spoken_early`, `duplicate`, `stream_busy`, `daemon_unreachable`, `alerts_off`, `unsupported_event`, `all_muted`, `session_muted`, `superseded`, `queue_full`, `early_say_off`, `no_session`, `bad_payload`, `empty_text`, `user_stopped`, `daemon_stopping`, `synth_error`, `stream_file_busy`, `nothing_new`, `no_tag_while_on` (from P0-5), `other`) and `reason_detail` (free text, such as "/voice X -> Y"). Mapping from today's literals: "no session" to `no_session`; "no prompt_id", "bad payload" and "malformed hook message" to `bad_payload`; "empty text" and "nothing to speak" to `empty_text`; "stopped by user" and "removed by user" to `user_stopped`; "daemon shutting down" and "daemon stopped" to `daemon_stopping`; "stream file busy" to `stream_file_busy`; "nothing new" to `nothing_new`; synthesis errors to `synth_error`. Old rows map by prefix, else to `other`. The new columns need the migration runner from [P1 M1](p1-context-bus.md#m1-schema-versioning-and-the-bus-table), which adds `reason_code` and `reason_detail` to `events`.
  *Done when:* every decision path in the hook and daemon writes a code from the set (a test enumerates them and fails on any reason literal in the code that is not mapped); `other` stays under 2% of rows in the test fixtures; and the "By reason" table groups by code with no value-embedded rows.

- [ ] **P0-5 Flag "voice on, but no tag" as a warning.**
  Separate "voice off, so no tag was asked for" from "voice on, Claude replied without `<say>`". The second is a real failure.
  *Done when:* the second case gets `reason_code = "no_tag_while_on"`, shows with a warning style in the Transcript, and has its own count in "By reason".

- [ ] **P0-6 Run the daemon as a service.**
  Add `afterword install-service` (writes a LaunchAgent plist for `afterword serve`) and `afterword start | stop | restart | uninstall-service`. Today the spool import in `afterword/daemon/log.py` already records "daemon down: spoken by say fallback" (or "say fallback failed", or "fallback disabled") as the reason, but only once the daemon is back, and Sessions doesn't surface it. This item keeps that text as `reason_detail` under `reason_code = daemon_unreachable` and surfaces it in Sessions. See decision D12.
  *Done when:* after `afterword install-service`, the daemon starts at login and restarts after a crash; `afterword status` reports the service state; a reply sent while the daemon was down shows `daemon_unreachable` with the existing "spoken by say fallback" detail in the Transcript once the daemon is back and imports the spool, and Sessions shows a fallback warning for that session.

- [ ] **P0-7 Update the status wording.**
  *Done when:* README drops "Not built: the optional menu-bar icon" and points at the SwiftBar track in `docs/research-v3.md` (deferred, per decision D13); the P4 suggestion panel is a separate item. The board in [README.md](README.md) says P0 is done, and the owner has updated the P0 label in `outlook.md`.

## Bridge to P1 (outlook §9 steps 2 and 3)

Outlook §9 asks for these during P0 so that P1 extends the log instead of migrating it, and so replay data starts accumulating now. They are designed in the P1 plan and tracked there, so there is one source of truth:

- [ ] **Bus-ready schema in the event log** = [P1 M1](p1-context-bus.md#m1-schema-versioning-and-the-bus-table).
- [ ] **Keep every hook event behind a flag** = [P1 M2](p1-context-bus.md#m2-record-lifecycle-events-to-the-bus), with `[bus] record = "lifecycle"`.

Recommended order: P1 M1 first (the migration runner, with `reason_code` and `reason_detail` in its v2 migration), then P0-4 (it needs those columns), then P1 M2, then P0-1, P0-2, P0-3, P0-5, P0-6, P0-7. P0 can be declared done without the bridge items, but every day without M2 is a day of replay data lost.
