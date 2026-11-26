# Changelog

How versions are numbered: [AGENTS.md](AGENTS.md#versioning). Versions before
0.3.1 were never tagged; their entries were written afterwards from the git
history, and the last commit of each is given.

## 0.3.6 (2026-11-26)

Updates by themselves: merge a version bump on GitHub (from a cloud session,
say) and every Mac running the service picks it up.

- A GitHub Action tags each merged version (`vX.Y.Z`) and makes a release
  from its CHANGELOG entry.
- `afterword install-service` now runs the daemon from its own managed copy
  in `~/.afterword/app` (a release per tag, each with its own venv, and a
  `current` link), not from the checkout you run it in; `--dev` keeps the old
  behaviour for development. Run it once to move over.
- The daemon checks for new release tags (5 minutes after starting, then
  every 6 hours), installs and smoke-tests a new one in the background,
  switches to it once nothing has been spoken or queued for 30 s, and
  restarts. If the new version crashes or fails at startup three times
  before it has run for a minute (time asleep does not count, and neither
  do clean restarts), it goes back to the last confirmed version and does
  not retry that tag by itself.
- Only tags whose commit is on `main` are installed. The release the
  service runs, and the one it would go back to, are never re-synced in
  place: changed extras come with the next release. With Kokoro, the UniDic
  dictionary is carried over into each release.
- After an update is confirmed, the Claude Code and Codex plugins are
  refreshed too (open Claude Code sessions load the new hooks after a
  restart; a disabled Codex plugin is left alone; a failed refresh is tried
  again at the next checks).
- `[update]` in config.toml: `mode = "auto"` (default), `"notify"` or
  `"off"`, and `check_every_h`. `afterword update` shows the state, `--check`
  checks now and `--now` updates now (and takes over an update waiting for
  quiet); the Config page's Daemon section has the same, with the new
  release's notes.

## 0.3.5 (2026-11-26)

Hardening, from an audit of the repo.

- Speech keeps going when something inside the daemon fails: a failed event
  log write no longer ends the playback worker, and the worker, spool,
  retention and warm-up tasks are restarted with backoff if they stop (one
  that fails 8 times in a row, each within 2 minutes of starting, is given up
  on until the daemon restarts).
- Playback has a time limit (the clip's length plus 10 s): a stuck audio
  device or afplay is stopped, the line is marked "playback timed out", and
  the next lines use afplay until the device recovers. A skipped or stopped
  line waits only those 10 s for a stuck device, not its whole length.
- Crashes leave a trace: native crash stacks go to `service.log`, and a
  daemon that did not shut down cleanly is noted in the event log at the
  next start.
- A corrupt `events.db` is moved aside (`events.db.corrupt-<time>`) and a
  fresh one started, instead of the service restarting every 10 s; a locked
  one gives a clear error and is left alone. The WAL file shrinks after a
  prune.
- Privacy: the event log keeps only a projection of each hook payload (ids,
  event names, folder, model, tool name, and the lengths of text fields),
  never the prompt, the reply or tool inputs. Existing rows are scrubbed once.
- The `say` fallback (daemon down) takes turns through `~/.afterword/say.lock`,
  so parallel sessions no longer talk over each other; `afterword test` does
  too.
- The hook's tag parser caps message and block sizes, so a crafted reply
  cannot use up the hook's time budget.
- The LaunchAgent runs Python with `-P` (run `afterword install-service`
  again to update the plist); the GUI is served with a strict Content
  Security Policy (per-response script nonces); state files are written with
  fsync.
- Development: tests run in parallel (`pytest -n 4`, about 40 s instead of
  2.5 min), ruff checks `F`, `E9` and `B` in CI, and hook code is checked to
  stay Python 3.9 compatible.

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
