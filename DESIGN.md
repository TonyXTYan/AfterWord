# AfterWord — v1 design

Spoken replies for Claude Code, using local Qwen3-TTS on Apple Silicon.
Speech is **off by default**, **set per session**, and **triggered only by a tag**.

The v2 features (pronunciations, a voice per language, streaming and early
speaking, spoken alerts, the seed explorer) are specified in
[docs/design-v2.md](docs/design-v2.md); the rules below still hold.

## Goals (v1)
1. Speak Claude Code replies only when Claude includes a `<say>` tag in a reply and the session has voice on.
2. Each session gets its own voice profile, so parallel sessions can be told apart by ear.
3. The accent stays the same across languages: one cloned or built-in Qwen3-TTS voice is used for every language.
4. Zero tokens are spent in sessions with voice off.
5. A local GUI for logs and debugging.

Out of scope for v1: microphone input, screen-context suggestions, Windows/Linux.

## Architecture
```
Claude Code hooks (stdlib-only Python, exit in <50 ms)
   │  JSON over a Unix socket: ~/.afterword/afterword.sock
   ▼
afterwordd daemon (Python, single process)
   ├─ session state      ~/.afterword/state/<session_id>.json
   ├─ profiles           ~/.afterword/config.toml (hot-reload)
   ├─ queue              one global playback queue; newer item from same session supersedes its queued older item
   ├─ synth              Qwen3-TTS via mlx-audio, model loaded once, kept warm
   ├─ player             afplay / sounddevice; interruptible
   ├─ event log          SQLite ~/.afterword/events.db + rotating text log
   └─ HTTP UI            http://127.0.0.1:47800 (FastAPI + one static HTML page, SSE for live updates)
Fallback: if the socket is unreachable, the hook runs `say` with the plain text (or exits silently if fallback is disabled).
```

## Protocol inside Claude's replies
- Speak: `<say mood="dry" lang="ja" pace="1.1">one or two sentences</say>`. All attributes are optional and override the profile for that message.
- Set or clear the session voice: `<voice set="nova"/>`, `<voice set="off"/>`.
- No tag → silent. This is what makes "off by default" work.

## Hooks
| Event | Job |
|---|---|
| `UserPromptSubmit` | If the session is on, inject about 40 tokens of `additionalContext`: "End your reply with `<say …>` containing a 1–2 sentence spoken summary in <lang>." If off, output nothing. |
| `Stop` | Read `last_assistant_message`. Parse `<voice>` and save it to state. Parse `<say>` and send it to the daemon. Check `stop_hook_active`. |
| `SessionStart` | Seed state from env `AFTERWORD_VOICE=<profile>` if set. |

Enable at launch with `AFTERWORD_VOICE=nova claude`. Toggle mid-session with the `/voice <profile|off>` slash command, whose prompt makes Claude emit `<voice set=…/>`; the Stop hook then saves it, keyed by `session_id`.

All hooks are `async: true` where possible. They must never block or fail Claude Code: catch every error, log it, and exit 0.

## config.toml (sketch)
```toml
[daemon]
port = 47800
fallback = "say"        # or "none"
announce_session = false # speak the session label before each message

[profiles.nova]
voice = "builtin:Chelsie"         # or "clone:~/voices/me.wav"
ref_text = ""                      # transcript of the clone clip, if the model needs one
lang = "en"
pace = 1.0
style = "calm, slightly wry"
max_chars = 400
```

## GUI (served by the daemon)
- **Live log**: every hook call with its raw payload, parsed tags, and the decision (spoken or skipped), with the skip reason shown: `session off`, `no <say> tag`, `daemon busy`, `error`.
- **Sessions**: id, cwd, profile, on/off toggle, mute, last message.
- **Queue**: pending and now playing, with skip and stop buttons.
- **Timings** per item: hook→daemon, synthesis time, real-time factor, time to first audio.
- **History**: the last N clips cached as WAV, with replay and download.
- **Voice lab**: text box + profile + attributes → preview. A/B two profiles.
- **Config**: view the TOML and reload it. Show model load status and memory use.
- Optional: a menu-bar icon (`rumps`) with mute-all, open UI, and recent items.

## Repo layout
```
afterword/
  hooks/afterword_hook.py   # one entry point: `afterword_hook.py <event>`
  daemon/{server,queue,synth,player,state,log}.py
  ui/index.html
  cli.py                    # afterword serve | status | test "text" | install-hooks
plugin/                     # Claude Code plugin: hooks.json + commands/voice.md
tests/                      # tag parser, state, queue supersede, hook never-fails
```
Packaging: `uvx afterword serve`, plus the Claude Code plugin for the hooks and `/voice`.

## Build order
1. Tag parser and its tests.
2. Stop hook with `say` fallback only. Prove the end-to-end path.
3. Daemon, socket, queue, event log, and the GUI log page.
4. Qwen3-TTS synth backend. Check the current mlx-audio API for built-in voices and cloning (CustomVoice / Base variants) before writing it.
5. UserPromptSubmit injection, SessionStart env seeding, `/voice` command.
6. The rest of the GUI, the plugin manifest, and a README with a demo GIF.

## Licence and ethics
MIT. Ship only built-in voices. Document that clone clips should come from the user's own voice or a voice used with consent.

## Implementation notes (as built)
- Plugin layout: the repository root is the plugin (`.claude-plugin/plugin.json`,
  `hooks/hooks.json`, `commands/voice.md`, marketplace source `"./"`), not a
  `plugin/` folder. A marketplace install copies only the plugin's own folder,
  so the hook script has to live inside it.
- `/voice`: Claude Code has a built-in `/voice` (dictation), so the command is
  `/afterword:voice` (plugin) or `/afterword-voice` (install-hooks). The
  `UserPromptSubmit` hook sees the raw command text before expansion and applies
  it itself, so the change never depends on Claude writing a `<voice>` tag (the
  tag still works).
- `UserPromptSubmit` and `SessionStart` are synchronous (their output must reach
  Claude first); `Stop` is async. Each turn is claimed once (`~/.afterword/seen`),
  so a plugin plus settings.json install does not speak twice.
- Qwen3-TTS has no speed control: `pace` is applied at playback.
