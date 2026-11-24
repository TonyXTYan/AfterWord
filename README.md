# AfterWord

Spoken replies for Claude Code. Speech is **off by default**, **set per session**,
and **triggered only by a tag** in Claude's reply. See [DESIGN.md](DESIGN.md).

Status: build steps 1–3 are done (tag parser, Stop hook with `say` fallback,
daemon with socket, queue, event log and a GUI log page). The Qwen3-TTS backend,
prompt injection, `/voice`, and the plugin come next. Until the Qwen backend
lands, the daemon renders speech with macOS `say`.

## How it works

Claude ends a reply with a tag:

```
<voice set="nova"/>                         turn this session's voice on (or set="off")
<say mood="dry" lang="en" pace="1.1">…</say>  speak this
```

The `Stop` hook reads the reply, saves the session's voice to
`~/.afterword/state/<session_id>.json`, and sends the `<say>` text to the
daemon over `~/.afterword/afterword.sock`. If the daemon is down, the hook
speaks with `say` directly (`fallback = "none"` turns that off), and the
daemon imports what happened from `~/.afterword/spool.jsonl` when it next starts.
Tags inside code blocks, inline code and HTML comments are ignored.

## Run it

```bash
uv sync
```

```bash
uv run afterword serve
```

Open http://127.0.0.1:47800 for the live log. Then in another shell:

```bash
uv run afterword test "Hello from AfterWord" --wait
```

```bash
uv run afterword status
```

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

To try it without touching your settings, pass a settings file to one run:

```bash
claude --settings /path/to/settings.json
```

where the file contains the `hooks` block that `install-hooks --dry-run` prints.

In a session, ask Claude to reply with `<voice set="nova"/>` to turn voice on.
Turn-by-turn instructions to add `<say>` (UserPromptSubmit) and the `/voice`
command arrive in build step 5; until then Claude speaks only when it writes
the tag.

## Files

| Path | What |
|---|---|
| `~/.afterword/config.toml` | daemon settings and voice profiles (hot-reloaded; written on first `serve`) |
| `~/.afterword/state/` | per-session voice state (written by the hook) |
| `~/.afterword/events.db` | event log (SQLite) shown in the UI |
| `~/.afterword/afterwordd.log` | daemon text log (rotating) |
| `~/.afterword/hook.log` | hook log (JSON lines; errors land here, never in Claude Code) |
| `~/.afterword/clips/` | last N rendered clips, replayable from the UI |

Environment: `AFTERWORD_HOME` relocates `~/.afterword`; `AFTERWORD_FALLBACK=none`
disables the `say` fallback; `AFTERWORD_DISABLE=1` makes the hook a no-op;
`AFTERWORD_SYNTH=null` / `AFTERWORD_PLAYER=null` run the daemon silently.

## Development

```bash
uv run pytest -q
```

The hooks (`afterword/hooks/`) are stdlib-only and run on Python 3.9+, so they
work under macOS's stock `python3` as well as the project's 3.12. The tests
check this, along with the rule that the hook never fails: exit code 0, no
output, and a bounded run time for any input.

## Licence

MIT. Ship only built-in voices. Clone clips should come from your own voice or
a voice used with consent.
