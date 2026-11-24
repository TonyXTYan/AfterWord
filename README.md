# AfterWord

Spoken replies for Claude Code, with local Qwen3-TTS on Apple Silicon.
Speech is **off by default**, **set per session**, and **triggered only by a tag**
in Claude's reply. See [DESIGN.md](DESIGN.md).

Status: build steps 1–4 are done, plus the voice lab: tag parser, Stop hook
with `say` fallback, daemon (socket, queue, event log, GUI), and the
Qwen3-TTS backend through mlx-audio. Prompt injection, `/voice` and the plugin
come next (step 5).

## How it works

Claude ends a reply with a tag:

```
<voice set="nova"/>                         turn this session's voice on (or set="off")
<say mood="dry" lang="en" pace="1.1">…</say>  speak this
```

The `Stop` hook reads the reply, saves the session's voice to
`~/.afterword/state/<session_id>.json`, and sends the `<say>` text to the
daemon over `~/.afterword/afterword.sock`. The daemon renders it with the
session's voice profile and plays it. If the daemon is down, the hook speaks
with `say` directly (`fallback = "none"` turns that off). If Qwen3-TTS cannot
render a line (model missing, bad voice), the daemon speaks it with `say` and
notes why in the log. Tags inside code blocks, inline code and HTML comments
are ignored.

## Run it

```bash
uv sync --extra qwen
```

```bash
uv run afterword models pull
```

That downloads the three Qwen3-TTS 1.7B 8-bit models (about 3.1 GB each) into
the Hugging Face cache. `uv run afterword models list` shows what is there;
pull only `custom_voice` if you only want the built-in speakers.

```bash
uv run afterword serve
```

Open http://127.0.0.1:47800 for the live log, and http://127.0.0.1:47800/#lab
for the voice lab. Then, in another shell:

```bash
uv run afterword test "Hello from AfterWord" --profile nova --wait
```

## Voices and profiles

A profile in `~/.afterword/config.toml` picks a voice and how it speaks:

```toml
[profiles.nova]
voice = "builtin:Serena"     # see the voice kinds below
lang = "en"
pace = 1.0                   # 0.5–2.0, applied at playback, pitch kept
style = "calm, slightly wry" # style instruction (builtin and design voices)
seed = 1234                  # optional: the same line always sounds the same
max_chars = 400
say_voice = "Samantha"       # macOS voice used if Qwen3-TTS is unavailable
```

| `voice` | Model | Notes |
|---|---|---|
| `builtin:<Speaker>` | CustomVoice | Serena, Vivian, Uncle_Fu, Dylan, Eric, Ryan, Aiden, Ono_Anna, Sohee; follows `style` and `mood` |
| `clone:<clip>` | Base | clones `~/.afterword/voices/<clip>.wav`; `clone:~/path.wav` also works |
| `design:<description>` | VoiceDesign | a voice described in words; can drift between lines, so freeze one into a clip for a stable voice |
| `say:<MacVoice>` | macOS `say` | no model needed |

One voice is used for every language, so the accent stays the same.

### Voice lab

The lab (http://127.0.0.1:47800/#lab) is for trying voices and building
profiles: preview any voice with any style, mood, pace and seed; render the
same sentence in several languages to check the accent; compare two profiles
A/B; save the settings as a profile (written into `config.toml`, keeping your
comments); and manage clone clips (upload a file or record from the
microphone). To get a stable designed voice, design it, then "Freeze as clip"
and use `clone:<name>`.

Clone clips must be your own voice or a voice used with the speaker's consent;
the lab asks you to confirm this for every upload or recording.

## Hook it into Claude Code

Preview the change first, then write it. `--scope user` (default) edits
`~/.claude/settings.json`; `project` and `local` edit `./.claude/`. A backup is
written next to the file, and other hooks are left alone.

```bash
uv run afterword install-hooks --dry-run
```

```bash
uv run afterword install-hooks
```

In a session, ask Claude to reply with `<voice set="nova"/>` to turn voice on.
Turn-by-turn instructions to add `<say>` (UserPromptSubmit) and the `/voice`
command arrive in build step 5; until then Claude speaks only when it writes
the tag.

## Files

| Path | What |
|---|---|
| `~/.afterword/config.toml` | daemon settings, `[qwen]` models, voice profiles (hot-reloaded; written on first `serve`) |
| `~/.afterword/voices/` | clone clips (`<name>.wav` + `<name>.txt` transcript) |
| `~/.afterword/lab/` | voice lab previews (cleared when the daemon restarts) |
| `~/.afterword/state/` | per-session voice state (written by the hook) |
| `~/.afterword/events.db` | event log (SQLite) shown in the UI |
| `~/.afterword/clips/` | last N spoken clips, replayable from the log |
| `~/.afterword/afterwordd.log` | daemon text log (rotating) |
| `~/.afterword/hook.log` | hook log (JSON lines; errors land here, never in Claude Code) |

Environment: `AFTERWORD_HOME` relocates `~/.afterword`; `AFTERWORD_FALLBACK=none`
disables the `say` fallback; `AFTERWORD_DISABLE=1` makes the hook a no-op;
`AFTERWORD_SYNTH=say|null` and `AFTERWORD_PLAYER=null` override the backends.

## Development

```bash
uv run pytest -q
```

Tests that need the real models are skipped unless you opt in:

```bash
AFTERWORD_LIVE_QWEN=1 uv run pytest -q -k live
```

The hooks (`afterword/hooks/`) are stdlib-only and run on Python 3.9+, so they
work under macOS's stock `python3` as well as the project's 3.12. The tests
check this, along with the rule that the hook never fails: exit code 0, no
output, and a bounded run time for any input. The lab's HTTP contract is in
[docs/lab-api.md](docs/lab-api.md).

## Licence

MIT. Ship only built-in voices. Clone clips should come from your own voice or
a voice used with consent.
