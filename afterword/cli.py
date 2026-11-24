"""afterword serve | status | test "text" | models | alerts | install-hooks"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import socket
import sys
import time
from pathlib import Path

from afterword import __version__
from afterword.hooks import common

HOOK_SCRIPT = Path(__file__).resolve().parent / "hooks" / "afterword_hook.py"
HOOK_MARKER = "afterword_hook.py"
# event -> (argument, extra hook fields). UserPromptSubmit and SessionStart
# must be synchronous: their output (the <say> instruction) is read before
# the prompt goes to Claude. Stop, MessageDisplay, PermissionRequest and
# Notification are async: speaking never delays Claude or holds the screen
# (and an async PermissionRequest hook can never decide a permission).
HOOK_EVENTS = {
    "SessionStart": ("session-start", {"timeout": 5}),
    "UserPromptSubmit": ("user-prompt-submit", {"timeout": 5}),
    "MessageDisplay": ("message-display", {"async": True, "timeout": 5}),
    "Stop": ("stop", {"async": True, "timeout": 10}),
    "PermissionRequest": ("permission-request", {"async": True, "timeout": 10}),
    "Notification": ("notification", {"async": True, "timeout": 10}),
}
VOICE_COMMAND_SRC = Path(__file__).resolve().parent.parent / "commands" / "voice.md"
VOICE_COMMAND_MARKER = "afterword:voice-command"


class DaemonUnreachable(Exception):
    pass


def _request(message: dict, timeout: float = 2.0) -> dict:
    """One request/response over the daemon socket. Raises DaemonUnreachable
    only when connecting fails; errors after connecting raise OSError or
    ValueError (the daemon may already be acting on the message)."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(common.socket_path()))
        except OSError as exc:
            raise DaemonUnreachable(str(exc)) from exc
        s.sendall(json.dumps(message).encode() + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.split(b"\n", 1)[0] or b"{}")


# --------------------------------------------------------------------------- #
# Commands                                                                    #
# --------------------------------------------------------------------------- #


def cmd_serve(args: argparse.Namespace) -> int:
    from afterword.daemon.server import main as serve_main

    return serve_main(port=args.port, http=not args.no_http, verbose=args.verbose)


def cmd_status(args: argparse.Namespace) -> int:
    try:
        reply = _request({"type": "ping"})
    except DaemonUnreachable as exc:
        print(f"afterwordd is not running ({common.socket_path()}: {exc})")
        return 1
    except (OSError, ValueError) as exc:
        print(f"afterwordd did not answer: {exc}")
        return 1
    st = reply.get("status", {})
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    q = st.get("queue", {})
    np = q.get("now_playing")
    print(f"afterwordd {st.get('version')}  pid {st.get('pid')}  up {st.get('uptime_s')}s")
    print(f"  UI        {st.get('http') or '(disabled)'}")
    print(f"  socket    {st.get('socket')}")
    print(f"  synth     {st.get('synth')}   player {st.get('player')}")
    cfg = st.get("config", {})
    print(f"  config    {cfg.get('path')}  profiles: {', '.join(cfg.get('profiles') or []) or 'none'}")
    if cfg.get("error"):
        print(f"  CONFIG ERROR: {cfg['error']}")
    for w in cfg.get("warnings") or []:
        print(f"  config warning: {w}")
    print(f"  playing   {np['text'][:70] if np else '—'}")
    print(f"  queued    {len(q.get('pending', []))}   events logged: {st.get('events')}")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    message = {"type": "test", "text": args.text, "profile": args.profile, "session_id": args.session}
    try:
        reply = _request(message)
    except DaemonUnreachable as exc:
        mode = common.fallback_mode(common.load_config_raw())
        if args.no_fallback or mode == "none":
            print(f"afterwordd is not running ({exc}); fallback is off")
            return 1
        import subprocess

        from afterword.hooks import tags

        text = tags.clean_text(args.text)
        print("afterwordd is not running; speaking with `say` instead")
        subprocess.run(common.say_argv(), input=text.encode())
        return 0
    except (OSError, ValueError) as exc:
        # Connected but no usable reply: the daemon may still speak it, so
        # do not fall back (that could play the line twice).
        print(f"afterwordd did not acknowledge: {exc}")
        return 1
    if not reply.get("ok"):
        print(f"daemon refused: {reply.get('error')}")
        return 1
    print(f"event #{reply.get('event_id')}: {reply.get('status')}"
          + (f" ({reply['reason']})" if reply.get("reason") else ""))
    if args.wait and reply.get("status") == "queued":
        return _wait_for_event(reply["event_id"])
    return 0


def _wait_for_event(event_id: int, timeout: float = 60.0) -> int:
    import urllib.request

    try:
        st = _request({"type": "ping"}).get("status", {})
    except (OSError, ValueError, DaemonUnreachable):
        return 0
    base = st.get("http")
    if not base:
        return 0
    end = time.time() + timeout
    status = None
    while time.time() < end:
        with urllib.request.urlopen(f"{base}/api/events/{event_id}") as r:
            ev = json.load(r)
        if ev["status"] != status:
            status = ev["status"]
            print(f"  … {status}")
        if status in ("spoken", "skipped", "error"):
            t = dict(ev.get("timings") or {})
            segments = t.pop("segments", None)
            print("  timings: " + ", ".join(f"{k}={v}" for k, v in t.items()))
            for i, seg in enumerate(segments if isinstance(segments, list) else []):
                if isinstance(seg, dict):
                    print(f"  segment {i + 1}: " + ", ".join(f"{k}={v}" for k, v in seg.items() if v is not None))
            return 0 if status == "spoken" else 1
        time.sleep(0.1)
    print("  timed out waiting")
    return 1


def cmd_models(args: argparse.Namespace) -> int:
    from afterword.daemon.config import ConfigStore
    from afterword.daemon.qwen import MODEL_KINDS, local_snapshot, mlx_audio_available

    repos = ConfigStore().get().qwen.repos()
    kinds = args.kinds or list(MODEL_KINDS)
    unknown = [k for k in kinds if k not in MODEL_KINDS]
    if unknown:
        print(f"unknown model kind(s): {', '.join(unknown)} (choose from {', '.join(MODEL_KINDS)})")
        return 2
    ok, error = mlx_audio_available()
    if args.action == "list":
        uses = {"custom_voice": "builtin:<Speaker>", "base": "clone:<clip>", "voice_design": "design:<description>"}
        for kind in kinds:
            path = local_snapshot(repos[kind])
            state = "downloaded" if path else "not downloaded"
            print(f"{kind:13} {state:15} {repos[kind]}   ({uses[kind]})")
        if not ok:
            print(f"note: {error}")
        return 0
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is missing; run `uv sync --extra qwen` first")
        return 1
    for kind in kinds:
        print(f"pulling {kind}: {repos[kind]}")
        snapshot_download(repos[kind])
    print("done. The daemon picks the models up on next use.")
    return 0


def cmd_alerts(args: argparse.Namespace) -> int:
    """`afterword alerts generate|show`: works with or without the daemon
    (it re-reads alerts.json when the file changes)."""
    from afterword.daemon import alerts
    from afterword.daemon.config import ConfigStore

    store = ConfigStore()
    phrases = alerts.AlertPhrases(common.home() / alerts.FILE_NAME)
    cfg = store.get()
    if args.action == "show":
        pools = phrases.pools()
        langs = [args.lang] if args.lang else list(pools)
        for lang in langs:
            for event, items in pools.get(lang, {}).items():
                src = "alerts.json" if phrases.file_pools.get(lang, {}).get(event) else "built-in"
                print(f"[{lang}] {event} ({len(items)}, {src})")
                for p in items:
                    print(f"  {p}")
        for w in phrases.warnings:
            print(f"warning: {w}")
        return 0
    name = args.profile or alerts.alert_profile_name(cfg, None)
    print(f"asking claude (haiku) for {args.count} phrases per event for profile {name} …", flush=True)
    try:
        result = alerts.generate(cfg, phrases, args.profile, args.count, args.persona)
    except alerts.AlertError as exc:
        print(f"not generated: {exc}")
        return 1
    print(f"profile {result['profile']}, language {result['lang']}; merged into {phrases.path}")
    for event, items in result["added"].items():
        print(f"{event}: {len(items)} new")
        for p in items:
            print(f"  + {p}")
    for r in result["rejected"]:
        print(f"rejected {r}")
    return 0


def hook_command(python: str | None = None) -> str:
    py = python or sys.executable
    return f"{shlex.quote(py)} {shlex.quote(str(HOOK_SCRIPT))}"


def settings_path_for(scope: str, project: Path) -> Path:
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "project":
        return project / ".claude" / "settings.json"
    if scope == "local":
        return project / ".claude" / "settings.local.json"
    raise ValueError(scope)


def merge_hooks(settings: dict, command: str, remove: bool = False) -> dict:
    """Add (or remove) AfterWord's hook entries. Idempotent: existing
    AfterWord entries are replaced, other hooks are left untouched."""
    hooks = settings.setdefault("hooks", {})
    for event, (arg, extra) in HOOK_EVENTS.items():
        groups = hooks.get(event, [])
        kept = []
        for group in groups:
            inner = [h for h in group.get("hooks", []) if HOOK_MARKER not in str(h.get("command", ""))]
            if inner:
                kept.append({**group, "hooks": inner})
        if not remove:
            kept.append({"hooks": [{"type": "command", "command": f"{command} {arg}", **extra}]})
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks", None)
    return settings


def voice_command_text() -> str:
    try:
        return VOICE_COMMAND_SRC.read_text(encoding="utf-8")
    except OSError:  # installed without the plugin files
        return (
            "---\ndescription: Turn AfterWord's spoken replies on or off for this session\n"
            "argument-hint: \"[profile | on | off | mute | unmute]\"\n"
            "disable-model-invocation: true\n---\n"
            f"<!-- {VOICE_COMMAND_MARKER} -->\n"
            "The user ran AfterWord's voice command with: `$ARGUMENTS`.\n\n"
            "AfterWord's hook has already applied it; the result is in the AfterWord note in\n"
            "this turn's context. Reply with that result in one short sentence. Do not use\n"
            "any tools.\n"
        )


VOICE_COMMAND_FILE = "afterword-voice.md"  # /afterword-voice; Claude Code has its own /voice


def _voice_command_plan(settings_path: Path, scope: str | None, remove: bool) -> tuple[str, Path]:
    """What install/uninstall would do with the command file:
    ('write'|'remove'|'leave'|'skip', path)."""
    target = settings_path.parent / "commands" / VOICE_COMMAND_FILE
    if scope == "local" and not remove:
        return "skip", target  # .claude/commands is shared with the project; local settings are not
    ours = target.is_file() and VOICE_COMMAND_MARKER in target.read_text(encoding="utf-8", errors="replace")
    if remove:
        return ("remove" if ours else "leave"), target
    if target.exists() and not ours:
        return "leave", target
    return "write", target


def _install_voice_command(settings_path: Path, scope: str | None, remove: bool, dry_run: bool) -> None:
    """`/afterword-voice` as a plain command next to the settings file
    (~/.claude/commands/ for user scope). The plugin provides
    `/afterword:voice` instead."""
    action, target = _voice_command_plan(settings_path, scope, remove)
    prefix = "# would " if dry_run else ""
    if action == "skip":
        print(f"{prefix}skip the /afterword-voice command for --scope local (use user or project scope for it)")
        return
    if action == "leave":
        if target.exists():
            print(f"{prefix}leave {target}: it is not AfterWord's")
        return
    if dry_run:
        print(f"# would {action} {target}")
        return
    if action == "remove":
        target.unlink()
        print(f"removed {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(voice_command_text(), encoding="utf-8")
    print(f"  /afterword-voice -> {target}")


def cmd_install_hooks(args: argparse.Namespace) -> int:
    path = Path(args.settings) if args.settings else settings_path_for(args.scope, Path.cwd())
    command = hook_command(args.python)
    if "/.cache/uv/" in command or "/uv/archive-" in command:
        print("warning: the hook path is inside uv's cache (uvx); install with `uv tool install` "
              "or from a clone so the path stays valid.", file=sys.stderr)
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        print(f"{path} is not valid JSON ({exc}); not touching it.", file=sys.stderr)
        return 1
    if not isinstance(settings, dict):
        print(f"{path} is not a JSON object; not touching it.", file=sys.stderr)
        return 1
    updated = merge_hooks(json.loads(json.dumps(settings)), command, remove=args.uninstall)
    text = json.dumps(updated, indent=2) + "\n"
    if args.dry_run:
        print(f"# would write {path}")
        print(text, end="")
        if not args.no_command:
            _install_voice_command(path, None if args.settings else args.scope, remove=args.uninstall, dry_run=True)
        return 0
    target = path.resolve() if path.is_symlink() else path  # keep dotfile symlinks intact
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    if target.exists():
        mode = target.stat().st_mode & 0o777
        backup = target.with_name(target.name + f".afterword-backup-{int(time.time())}")
        shutil.copy2(target, backup)
        os.chmod(backup, mode & 0o700 or 0o600)
        print(f"backup: {backup}")
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    verb = "removed from" if args.uninstall else "installed in"
    print(f"AfterWord hooks {verb} {path}")
    if not args.uninstall:
        for event, (arg, _) in HOOK_EVENTS.items():
            print(f"  {event} -> {command} {arg}")
    if not args.no_command:
        _install_voice_command(path, None if args.settings else args.scope, remove=args.uninstall, dry_run=False)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="afterword", description="Spoken replies for Claude Code.")
    p.add_argument("--version", action="version", version=f"afterword {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the daemon in the foreground")
    s.add_argument("--port", type=int, help="HTTP UI port (default from config, 47800)")
    s.add_argument("--no-http", action="store_true", help="socket only, no UI")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("status", help="show daemon status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("test", help="speak a line through the daemon")
    s.add_argument("text")
    s.add_argument("--profile")
    s.add_argument("--session", default="cli-test")
    s.add_argument("--wait", action="store_true", help="wait until it has been spoken; print timings")
    s.add_argument("--no-fallback", action="store_true", help="fail instead of using `say` if the daemon is down")
    s.set_defaults(func=cmd_test)

    s = sub.add_parser("models", help="list or download the Qwen3-TTS models")
    s.add_argument("action", choices=("list", "pull"), nargs="?", default="list")
    s.add_argument("kinds", nargs="*", help="custom_voice, base, voice_design (default: all)")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("alerts", help="spoken alert phrases: generate them with claude, or show them")
    s.add_argument("action", choices=("generate", "show"))
    s.add_argument("--profile", help="profile whose language, style and persona to write for (default: [alerts] profile, else the first)")
    s.add_argument("--count", type=int, default=12, help="phrases per event (1-30, default 12)")
    s.add_argument("--persona", help="persona note for this run (default: the profile's persona)")
    s.add_argument("--lang", help="show: only this language")
    s.set_defaults(func=cmd_alerts)

    s = sub.add_parser("install-hooks", help="add AfterWord's hooks to a Claude Code settings file")
    s.add_argument("--scope", choices=("user", "project", "local"), default="user",
                   help="user: ~/.claude/settings.json; project/local: ./.claude/settings(.local).json")
    s.add_argument("--settings", help="explicit settings file path (overrides --scope)")
    s.add_argument("--python", help="interpreter for the hook (default: this one)")
    s.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    s.add_argument("--uninstall", action="store_true", help="remove AfterWord's hooks")
    s.add_argument("--no-command", action="store_true", help="do not install the /afterword-voice command file")
    s.set_defaults(func=cmd_install_hooks)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
