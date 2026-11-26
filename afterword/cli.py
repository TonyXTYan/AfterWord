"""afterword serve | status | test "text" | models | alerts | voices | install-hooks
| install-service | start | stop | restart | uninstall-service | login | update"""

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
DESCRIBE_COMMAND_SRC = Path(__file__).resolve().parent.parent / "commands" / "describe-voices.md"
DESCRIBE_COMMAND_MARKER = "afterword:describe-voices-command"


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
    from afterword import update

    # Under launchd with an update pending: count this start, and go back to
    # the previous release after too many (before the daemon is imported).
    update.startup_check()
    from afterword.daemon.server import main as serve_main

    return serve_main(port=args.port, http=not args.no_http, verbose=args.verbose)


def cmd_status(args: argparse.Namespace) -> int:
    from afterword import service

    error = None
    try:
        reply = _request({"type": "ping"})
    except DaemonUnreachable as exc:
        error = f"afterwordd is not running ({common.socket_path()}: {exc})"
    except (OSError, ValueError) as exc:
        error = f"afterwordd did not answer: {exc}"
    svc = service.service_info(login=True)
    if error:
        if args.json:
            print(json.dumps({"error": error, "service": svc.as_dict()}, indent=2))
        else:
            from afterword import update

            print(error)
            print(f"  service   {svc.summary()}")
            print(f"  update    {update.summary(update.local_updater().view())}")
        return 1
    st = reply.get("status", {})
    if args.json:
        print(json.dumps({**st, "service": svc.as_dict()}, indent=2))
        return 0
    q = st.get("queue", {})
    np = q.get("now_playing")
    print(f"afterwordd {st.get('version')}  pid {st.get('pid')}  up {st.get('uptime_s')}s")
    print(f"  UI        {st.get('http') or '(disabled)'}")
    print(f"  socket    {st.get('socket')}")
    print(f"  service   {svc.summary()}")
    if isinstance(st.get("update"), dict):
        from afterword import update

        print(f"  update    {update.summary(st['update'])}")
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
        with _say_lock() as got:
            if not got:
                print("another `say` fallback is still speaking; gave up waiting")
                return 1
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


@contextlib.contextmanager
def _say_lock():
    """Hold <home>/say.lock, the lock the hook's `say` fallback runner takes,
    so `afterword test` with the daemon down does not talk over a fallback
    line (or the other way round). Yields False after waiting
    SAY_LOCK_WAIT_S; True when it holds the lock, or when the lock file
    cannot be used at all (never silence a line over that)."""
    import fcntl

    from afterword.hooks.afterword_hook import SAY_LOCK_NAME, SAY_LOCK_WAIT_S

    path = common.home() / SAY_LOCK_NAME
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        yield True
        return
    try:
        end = time.monotonic() + SAY_LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    yield False
                    return
                time.sleep(0.05)
            except OSError:
                break  # cannot lock: speak anyway
        yield True
    finally:
        os.close(fd)


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
    from afterword.daemon import apple
    from afterword.daemon.config import ConfigStore
    from afterword.daemon.qwen import (
        EXTRA_REPOS, MODEL_KINDS, QWEN_KINDS, label, local_snapshot, mlx_audio_available, required_files,
    )

    repos = ConfigStore().get().qwen.repos()
    default = list(MODEL_KINDS) if args.action == "list" else list(QWEN_KINDS)
    kinds = args.kinds or default
    unknown = [k for k in kinds if k not in MODEL_KINDS]
    if unknown:
        print(f"unknown model kind(s): {', '.join(unknown)} (choose from {', '.join(MODEL_KINDS)})")
        return 2
    ok, error = mlx_audio_available()
    if args.action == "list":
        uses = {"custom_voice": "builtin:<Speaker>", "base": "clone:<clip>", "voice_design": "design:<description>",
                "kokoro": "kokoro:<voice>", "chatterbox": "chatterbox:<clip>"}
        for kind in kinds:
            path = local_snapshot(repos[kind], required_files(kind))
            extras = [r for r in EXTRA_REPOS.get(kind, ()) if local_snapshot(r, required_files(r)) is None]
            state = "downloaded" if path and not extras else "not downloaded"
            print(f"{kind:13} {state:15} {repos[kind]}   ({uses[kind]}, {label(kind)})")
        apple_ok, apple_error = apple.available()
        print(f"{'apple':13} {'available' if apple_ok else 'unavailable':15} AVSpeechSynthesizer helper   "
              f"(apple:<voice>{'' if apple_ok else ': ' + str(apple_error)})")
        if not ok:
            print(f"note: {error}")
        return 0
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is missing; run `uv sync --extra qwen` first")
        return 1
    for kind in kinds:
        for repo in (repos[kind],) + tuple(EXTRA_REPOS.get(kind, ())):
            print(f"pulling {kind}: {repo}")
            snapshot_download(repo)
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
DESCRIBE_COMMAND_FILE = "afterword-describe-voices.md"  # /afterword-describe-voices


def describe_command_text() -> str:
    try:
        return DESCRIBE_COMMAND_SRC.read_text(encoding="utf-8")
    except OSError:  # installed without the plugin files
        return (
            "---\ndescription: Write one-line descriptions for AfterWord's voice profiles\n"
            "argument-hint: \"[profile ...]\"\n"
            "disable-model-invocation: true\n---\n"
            f"<!-- {DESCRIBE_COMMAND_MARKER} -->\n"
            "Run `afterword voices --json`. For each profile without a description (or the ones named in\n"
            "`$ARGUMENTS`), propose a one-line description (80 characters at most), show the proposals to\n"
            "the user and ask which to keep, then write each kept one with\n"
            "`afterword voices describe <name> \"<text>\"` and finish with `afterword voices`.\n"
        )


def _command_plan(settings_path: Path, scope: str | None, remove: bool, filename: str,
                  marker: str) -> tuple[str, Path]:
    """What install/uninstall would do with a command file:
    ('write'|'remove'|'leave'|'skip', path)."""
    target = settings_path.parent / "commands" / filename
    if scope == "local" and not remove:
        return "skip", target  # .claude/commands is shared with the project; local settings are not
    ours = target.is_file() and marker in target.read_text(encoding="utf-8", errors="replace")
    if remove:
        return ("remove" if ours else "leave"), target
    if target.exists() and not ours:
        return "leave", target
    return "write", target


def _voice_command_plan(settings_path: Path, scope: str | None, remove: bool) -> tuple[str, Path]:
    return _command_plan(settings_path, scope, remove, VOICE_COMMAND_FILE, VOICE_COMMAND_MARKER)


def _install_command(settings_path: Path, scope: str | None, remove: bool, dry_run: bool,
                     filename: str, marker: str, text_fn) -> None:
    """A plain command next to the settings file (~/.claude/commands/ for
    user scope), e.g. `/afterword-voice`. The plugin provides `/afterword:voice`."""
    action, target = _command_plan(settings_path, scope, remove, filename, marker)
    slash = "/" + filename[:-3]
    prefix = "# would " if dry_run else ""
    if action == "skip":
        print(f"{prefix}skip the {slash} command for --scope local (use user or project scope for it)")
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
    target.write_text(text_fn(), encoding="utf-8")
    print(f"  {slash} -> {target}")


def _install_voice_command(settings_path: Path, scope: str | None, remove: bool, dry_run: bool) -> None:
    _install_command(settings_path, scope, remove, dry_run, VOICE_COMMAND_FILE, VOICE_COMMAND_MARKER,
                     voice_command_text)
    _install_command(settings_path, scope, remove, dry_run, DESCRIBE_COMMAND_FILE, DESCRIBE_COMMAND_MARKER,
                     describe_command_text)


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), (" min", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _voices_items() -> list[dict]:
    """The same entries as GET /api/voices, from the config file and
    voices.json directly (no daemon needed)."""
    from afterword.daemon import lab
    from afterword.daemon.config import ConfigStore

    cfg = ConfigStore().get()
    usage = common.load_voice_usage()
    now = time.time()
    return [lab.voice_item(p, usage, now) for p in cfg.profiles.values()]


def cmd_voices(args: argparse.Namespace) -> int:
    """`afterword voices [--json]` lists the profiles; `afterword voices
    describe <name> <text>` sets one's description in config.toml."""
    if args.action == "describe":
        return _voices_describe(args)
    if args.rest:
        print("usage: afterword voices [--json] | afterword voices describe <name> <text>", file=sys.stderr)
        return 2
    items = _voices_items()
    if args.json:
        print(json.dumps({"profiles": items}, indent=2, ensure_ascii=False))
        return 0
    if not items:
        print("no voice profiles configured")
        return 0
    now = time.time()
    for it in items:
        n = len(it["in_use"])
        if n:
            use = f"in use by {n} session{'s' if n != 1 else ''}"
        elif it.get("last_used"):
            use = f"last used {_ago(now - it['last_used'])}"
        else:
            use = "never used"
        persona = " ".join((it.get("persona") or "").split())
        if len(persona) > 60:
            persona = persona[:59].rstrip() + "\u2026"
        print(it["name"])
        print(f"  voice:       {it['voice'] or '(default)'}")
        print(f"  lang:        {it['lang']}")
        print(f"  style:       {it['style'] or '(none)'}")
        print(f"  persona:     {persona or '(none)'}")
        print(f"  description: {it.get('description') or '(none)'}")
        print(f"  usage:       {use}")
    return 0


def _voices_describe(args: argparse.Namespace) -> int:
    from afterword.daemon import lab
    from afterword.daemon.config import ConfigStore

    if len(args.rest) != 2:
        print('usage: afterword voices describe <name> "<text>"  ("" removes the description)', file=sys.stderr)
        return 2
    name, text = args.rest[0], args.rest[1].strip()
    if len(text) > lab.DESCRIPTION_MAX:
        print(f"description is {len(text)} characters; at most {lab.DESCRIPTION_MAX}.", file=sys.stderr)
        return 1
    store = ConfigStore()
    profiles = store.get().profiles
    if name not in profiles:
        names = ", ".join(profiles) or "(none)"
        print(f"no profile named {name!r}. Profiles: {names}", file=sys.stderr)
        return 1
    old = profiles[name].description
    try:
        found = lab.write_description(store.path, name, text)
    except (ValueError, OSError) as exc:
        print(f"config.toml could not be updated: {exc}", file=sys.stderr)
        return 1
    if not found:
        print(f"no profile named {name!r} in {store.path}", file=sys.stderr)
        return 1
    print(f"{name}: description")
    print(f"  was: {old or '(none)'}")
    print(f"  now: {text or '(none)'}")
    return 0


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


def cmd_install_service(args: argparse.Namespace) -> int:
    from afterword import service, update

    if args.dev:
        code = service.install(dry_run=args.dry_run, start=not args.no_start)
        if code == 0 and not args.dry_run:
            update.note_dev_install()
        return code
    return update.install_managed(dry_run=args.dry_run, start=not args.no_start)


def _http(base: str, method: str, path: str, body: dict | None = None, timeout: float = 120.0) -> tuple[int, dict]:
    """One request to the daemon's HTTP API (POSTs carry X-AfterWord)."""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if method != "GET":
        req.add_header("X-AfterWord", "1")
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {"error": f"HTTP {exc.code}"}


def _daemon_status() -> dict | None:
    """The daemon's status when one answers, else None."""
    try:
        return _request({"type": "ping"}).get("status") or {}
    except (OSError, ValueError, DaemonUnreachable):
        return None


def cmd_update(args: argparse.Namespace) -> int:
    """`afterword update`: status; --check checks now; --now installs the
    newest release now (through the daemon when one runs)."""
    from afterword import update

    st = _daemon_status()
    if st is None:
        return _update_local(args)
    base = st.get("http")
    if not base:
        if args.check or args.now:
            print("afterwordd runs without its HTTP UI (--no-http): restart it with the UI to check or update",
                  file=sys.stderr)
            return 1
        view = st.get("update") or {}
        print(json.dumps(view, indent=2) if args.json else "\n".join(update.describe(view)))
        return 0
    try:
        if args.check or args.now:
            code, view = _http(base, "POST", "/api/update/check")
        else:
            code, view = _http(base, "GET", "/api/update")
        if code == 404:
            print(f"the running afterwordd ({st.get('version')}) has no updater: `afterword install-service` "
                  "sets up the managed install", file=sys.stderr)
            return 1
        if code != 200:
            print(f"afterwordd answered {code}: {view.get('error')}", file=sys.stderr)
            return 1
        if args.json and not args.now:
            print(json.dumps(view, indent=2))
            return 0
        if not args.now:
            print("\n".join(update.describe(view)))
            return 1 if args.check and view.get("last_error") else 0
        if view.get("last_error"):
            print(f"check failed: {view['last_error']}", file=sys.stderr)
            return 1
        if not view.get("available"):
            print(f"already up to date ({view.get('running')})")
            return 0
        code, ack = _http(base, "POST", "/api/update/apply", {"force": args.force, "wait_s": args.wait})
    except OSError as exc:
        print(f"afterwordd did not answer: {exc}", file=sys.stderr)
        return 1
    if code != 202:
        print(f"not updated: {ack.get('error')}", file=sys.stderr)
        return 1
    return _follow_update(base, st.get("pid"), ack.get("tag") or view.get("latest"), args)


def _follow_update(base: str, old_pid, tag, args) -> int:
    """Print the daemon's update phases until it restarts on the new release
    (or stops trying)."""
    from afterword import update

    print(f"updating to {tag}: preparing (the first time can take a few minutes)…", flush=True)
    seen = "preparing"
    end = time.time() + update.UV_TIMEOUT_S + (0 if args.force else args.wait) + 120
    while time.time() < end:
        time.sleep(2)
        try:
            _, view = _http(base, "GET", "/api/update", timeout=10)
        except OSError:
            break  # down for the restart
        phase = view.get("phase")
        if phase == seen:
            continue
        seen = phase
        if phase == "waiting":
            print(f"prepared; waiting up to {args.wait:.0f} s for a moment with nothing to say…", flush=True)
        elif phase in ("switching", "restarting"):
            print(f"{phase}…", flush=True)
        elif phase == "idle":
            print(view.get("last_result") or "stopped", file=sys.stderr)
            if view.get("last_error"):
                print(view["last_error"], file=sys.stderr)
            return 1
    # launchd starts it again, no sooner than 10 s after its last launch.
    end = time.time() + 90
    while time.time() < end:
        time.sleep(2)
        st = _daemon_status()
        if st and st.get("pid") != old_pid:
            print(f"afterwordd {st.get('version')} is running, pid {st.get('pid')}; it confirms the update "
                  f"after {update.CONFIRM_AFTER_S:.0f} s")
            return 0
    print("afterwordd did not come back within 90 s: see `afterword status` and service.log", file=sys.stderr)
    return 1


def _update_local(args: argparse.Namespace) -> int:
    """`afterword update` with no daemon answering: works on update.json and
    the managed install directly (it never restarts anything)."""
    from afterword import update

    up = update.local_updater()
    view = up.check() if (args.check or args.now) else up.view()
    if args.json and not args.now:
        print(json.dumps(view, indent=2))
        return 0
    if not args.now:
        print("afterwordd is not running")
        print("\n".join(update.describe(view)))
        return 1 if args.check and view.get("last_error") else 0
    if view.get("last_error"):
        print(f"check failed: {view['last_error']}", file=sys.stderr)
        return 1
    if view.get("install") != "managed":
        print("the service is not a managed install (`afterword install-service` sets one up); "
              "nothing to update", file=sys.stderr)
        return 1
    if not view.get("available"):
        print(f"already up to date ({view.get('current') or view.get('running')})")
        return 0
    tag = view["latest"]
    print(f"preparing {tag} (the daemon is not running)…", flush=True)
    try:
        up.prepare(tag)
        old = up.switch(tag, pending=True)
    except update.UpdateError as exc:
        print(f"not updated: {exc}", file=sys.stderr)
        return 1
    print(f"switched from {old} to {tag}; `afterword start` runs it")
    return 0


def cmd_service(args: argparse.Namespace) -> int:
    from afterword import service

    return {"start": service.start, "stop": service.stop, "restart": service.restart,
            "uninstall-service": service.uninstall}[args.cmd]()


def cmd_login(args: argparse.Namespace) -> int:
    from afterword import service

    return service.login(args.state)


SERVICE_HELP = """\
The service is a per-user LaunchAgent, io.github.tonyxtyan.afterword, in
~/Library/LaunchAgents. It runs `afterword serve` from AfterWord's managed
install (app/current in AfterWord's home: a release tag, which the daemon
and `afterword update` keep up to date), or with --dev with this checkout's
interpreter. It starts at login, and is restarted after a crash (not after
a clean exit).
Its stdout and stderr go to service.log in AfterWord's home (~/.afterword,
or $AFTERWORD_HOME); the daemon's own log is afterwordd.log next to it.

  install-service    set up the managed install at this code's release (or
                     the newest release) and test it, before anything else
                     changes; then write the plist and load it. A daemon
                     already running outside launchd is stopped first
                     (SIGTERM). Running it again refreshes both. With --dev
                     it runs this checkout (updates are only reported).
  start              load the service (launchctl bootstrap), or start its
                     daemon if it is loaded (launchctl kickstart)
  stop               unload it (launchctl bootout): it stays stopped, even
                     with KeepAlive, until the next login or `afterword start`
  restart            SIGTERM, wait for the clean exit, kickstart; starts it
                     if it is not loaded
  uninstall-service  unload it and delete the plist (and undo `login off`)
  login [on|off]     whether launchd loads it at login (launchctl enable /
                     disable; kept across reboots). Off leaves a running
                     daemon running. start, install-service and restart
                     still work while it is off, and keep it off.
"""


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

    s = sub.add_parser("models", help="list or download the local TTS models")
    s.add_argument("action", choices=("list", "pull"), nargs="?", default="list")
    s.add_argument("kinds", nargs="*", help="custom_voice, base, voice_design, kokoro, chatterbox "
                   "(default: all for list, the three Qwen3-TTS models for pull)")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("alerts", help="spoken alert phrases: generate them with claude, or show them")
    s.add_argument("action", choices=("generate", "show"))
    s.add_argument("--profile", help="profile whose language, style and persona to write for (default: [alerts] profile, else the first)")
    s.add_argument("--count", type=int, default=12, help="phrases per event (1-30, default 12)")
    s.add_argument("--persona", help="persona note for this run (default: the profile's persona)")
    s.add_argument("--lang", help="show: only this language")
    s.set_defaults(func=cmd_alerts)

    s = sub.add_parser("voices", help="list the voice profiles, or describe one for /afterword:voice pick")
    s.add_argument("action", nargs="?", choices=("list", "describe"), default="list")
    s.add_argument("rest", nargs="*", metavar="name text", help="describe: the profile and its description")
    s.add_argument("--json", action="store_true", help="same data as GET /api/voices")
    s.set_defaults(func=cmd_voices)

    s = sub.add_parser("install-hooks", help="add AfterWord's hooks to a Claude Code settings file")
    s.add_argument("--scope", choices=("user", "project", "local"), default="user",
                   help="user: ~/.claude/settings.json; project/local: ./.claude/settings(.local).json")
    s.add_argument("--settings", help="explicit settings file path (overrides --scope)")
    s.add_argument("--python", help="interpreter for the hook (default: this one)")
    s.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    s.add_argument("--uninstall", action="store_true", help="remove AfterWord's hooks")
    s.add_argument("--no-command", action="store_true", help="do not install the /afterword-voice and /afterword-describe-voices command files")
    s.set_defaults(func=cmd_install_hooks)

    fmt = argparse.RawDescriptionHelpFormatter
    s = sub.add_parser("install-service", help="run the daemon as a LaunchAgent: at login, restarted after a crash",
                       description=SERVICE_HELP, formatter_class=fmt)
    s.add_argument("--dry-run", action="store_true", help="print the plist and the launchctl commands, write nothing")
    s.add_argument("--no-start", action="store_true", help="write the plist but do not load it")
    s.add_argument("--dev", action="store_true",
                   help="run this checkout's interpreter instead of the managed install (for development: "
                        "updates are then reported, never applied)")
    s.set_defaults(func=cmd_install_service)
    for name, text in (
        ("start", "start the service (bootstrap it, or kickstart it if loaded)"),
        ("stop", "stop the service until the next login or `afterword start` (bootout; the plist stays)"),
        ("restart", "restart the service's daemon (SIGTERM, then kickstart); start it if not loaded"),
        ("uninstall-service", "stop the service and delete its plist"),
    ):
        s = sub.add_parser(name, help=text, description=SERVICE_HELP, formatter_class=fmt)
        s.set_defaults(func=cmd_service)
    s = sub.add_parser("login", help="show or set whether the service starts at login",
                       description=SERVICE_HELP, formatter_class=fmt)
    s.add_argument("state", nargs="?", choices=("on", "off"), help="omit to print the current setting")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("update", help="show update status; check for or install a new release",
                       description="Releases are the repository's vX.Y.Z tags. [update] mode in config.toml: "
                                   "auto (the daemon installs a new release when nothing is being said), "
                                   "notify (it only reports it), off.")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true", help="check for a new release now")
    g.add_argument("--now", action="store_true", help="install the newest release now")
    s.add_argument("--wait", type=float, default=600.0, metavar="S",
                   help="with --now: wait up to S seconds for nothing to be said (default 600)")
    s.add_argument("--force", action="store_true", help="with --now: switch without waiting (cuts off speech)")
    s.add_argument("--json", action="store_true", help="the status as JSON (GET /api/update)")
    s.set_defaults(func=cmd_update)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
