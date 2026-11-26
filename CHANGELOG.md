# Changelog

How versions are numbered: [AGENTS.md](AGENTS.md#versioning). Versions before
0.3.1 were never tagged; their entries were written afterwards from the git
history, and the last commit of each is given.

## 0.3.4 (2026-11-26)

- A Daemon section first on the Config page: whether the daemon runs under
  launchd or by hand, its pid, uptime and last exit; a **Start at login**
  switch (`launchctl enable` / `disable`, which keeps the running daemon);
  **Restart** (under launchd the daemon exits 75 and KeepAlive brings it
  back; the page waits for the new pid); the event log's size, clips and
  retention with **Prune now**; the last lines of `afterwordd.log` and
  `service.log`; **Copy status**; and **Show in Finder** for `~/.afterword`.
  New endpoints: `GET /api/service`, `POST /api/service` (`login`),
  `POST /api/service/restart`, `/prune`, `/reveal`, `GET /api/logs/{name}`.
- `afterword login [on|off]`, and "start at login" in `afterword status`.
  `start`, `install-service` and `restart` still work with start at login
  off: they enable the service for the moment and turn login back off.

## 0.3.3 (2026-11-26)

- The daemon can run as a LaunchAgent: `afterword install-service` writes and
  loads `~/Library/LaunchAgents/io.github.tonyxtyan.afterword.plist` (it
  starts at login and restarts after a crash), with `afterword start`,
  `stop`, `restart` and `uninstall-service`. `afterword status` shows the
  service. Its output goes to `~/.afterword/service.log`, trimmed at startup
  (P0-6, except surfacing "daemon down" replies in Sessions, which waits on
  P0-4).
- The Sessions page warns when two live sessions (voice on, not muted, active
  in the last 2 hours) share a voice, on every row involved, with a
  "Pick another" button that moves that session to the least recently used
  free voice. `GET /api/sessions` gains `voice_in_use_s` (P0-2).

## 0.3.2 (2026-11-26)

- The event log has a retention limit: `[daemon] keep_days` (7 by default,
  0 = keep everything). Every hour, and when the daemon starts, lines older
  than that are deleted from `events.db` together with their clips, and a
  large prune compacts the file.

## 0.3.1 (2026-11-26)

- GUI clips follow the system's default output. A play that starts from
  silence opens the current default device (an old audio context could stay
  tied to headphones that were gone and play nothing until a reload), and a
  clip that is playing moves when the browser reports a device change.
- The version is set in the plugin manifests too, and a test keeps every copy
  of it in step.

## 0.3.0 (2026-11-26, last commit 21f7718)

More engines and the GUI's second pass ([docs/research-v3.md](docs/research-v3.md)).

- Kokoro, Chatterbox Multilingual and Apple voices next to Qwen3-TTS, mixed
  per profile and language.
- GUI v2: transcript, sessions, voice lab workbench and config pages; one
  colour per voice; clips through Web Audio so media keys stay with music.
- Claude picks a session's voice from the least-used profiles; voice
  descriptions; speak anything and replay; export and per-voice stats;
  keyboard shortcuts and fix-a-word.
- Speaker colours with a picker, and an Engines mode in the voice lab.

## 0.2.0 (2026-11-25, last commit b7d7089)

[docs/design-v2.md](docs/design-v2.md) and Codex support: pronunciations, a
voice per language with several `<say>` blocks per reply, streaming playback
and early speaking, spoken alerts, and the seed explorer.

## 0.1.0 (2026-11-24, last commit b91bbe0)

[DESIGN.md](DESIGN.md), the v1 build: tag parser, hooks with a `say`
fallback, the daemon (queue, event log, Qwen3-TTS), per-session voice control,
the Claude Code plugin, and the first GUI.
