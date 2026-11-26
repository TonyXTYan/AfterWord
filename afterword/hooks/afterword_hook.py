#!/usr/bin/env python3
"""AfterWord's single hook entry point, for Claude Code and Codex.

    afterword_hook.py <event> [codex]   # stop | user-prompt-submit | session-start | message-display
                                        # | permission-request | notification

Codex (hooks/codex-hooks.json) passes "codex" as the second argument and uses
only session-start, user-prompt-submit, stop and permission-request; it has
no MessageDisplay or Notification event.

Reads the hook payload (JSON) from stdin. Contract with Claude Code:

* never fails: every error is caught and logged, the exit code is always 0;
* never blocks: hard deadline via an interval timer, short socket timeouts,
  the `say` fallback is spawned detached and not waited for;
* stdout carries hook output only: nothing for Stop, SessionStart,
  MessageDisplay, PermissionRequest and Notification (a PermissionRequest
  hook never decides anything), and for UserPromptSubmit the additionalContext JSON, only
  while voice is on (so a session with voice off costs zero tokens).

Stdlib-only, Python 3.9+.
"""

from __future__ import annotations

import os
import sys
import time

_T0 = time.time()
_HOST = "claude"  # "codex" when hooks/codex-hooks.json passes it as the second argument
_HERE = os.path.dirname(os.path.abspath(__file__))

# Generous upper bound. The normal path finishes in a few tens of ms.
HARD_DEADLINE_S = 3.0
STDIN_MAX_BYTES = 16 * 1024 * 1024
STDIN_TIMEOUT_S = 1.0
CONNECT_TIMEOUT_S = 0.15
ACK_TIMEOUT_S = 0.35
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024

_common = None  # set once imports succeed


def _log(level: str, message: str, **fields: object) -> None:
    """Best-effort line in ~/.afterword/hook.log. Never raises."""
    try:
        if _common is None:
            return
        record = {"ts": time.time(), "pid": os.getpid(), "level": level, "msg": message}
        record.update(fields)
        _common.append_jsonl(
            _common.hook_log_path(), record, _common.HOOK_LOG_MAX_BYTES, rotate=True
        )
    except BaseException:
        pass


_deadline_timer = None


def _arm_deadline() -> None:
    """Exit with code 0 if anything hangs.

    Two layers: SIGALRM handles hangs in Python code; a watchdog thread
    handles hangs inside a blocking C call (e.g. a read that never returns),
    where Python signal handlers cannot run but other threads can, because
    blocking I/O releases the GIL.

    The escape itself must never block: the "deadline hit" log line is
    written from a helper thread that gets 0.2 s, then os._exit(0) runs
    regardless.
    """
    global _deadline_timer
    try:
        deadline = float(os.environ.get("AFTERWORD_HOOK_DEADLINE", HARD_DEADLINE_S))
        if not deadline == deadline:  # NaN
            raise ValueError
        deadline = min(60.0, max(0.05, deadline))
    except (TypeError, ValueError, OverflowError):
        deadline = HARD_DEADLINE_S

    def _bail(*_args):  # pragma: no cover - timing dependent
        try:
            import threading

            t = threading.Thread(target=_log, args=("error", "hard deadline hit, exiting"))
            t.daemon = True
            t.start()
            t.join(0.2)
        except BaseException:
            pass
        os._exit(0)

    try:
        import signal

        signal.signal(signal.SIGALRM, _bail)
        signal.setitimer(signal.ITIMER_REAL, deadline)
    except BaseException:
        pass
    try:
        import threading

        timer = threading.Timer(deadline + 0.25, _bail)
        timer.daemon = True
        timer.start()
        _deadline_timer = timer
    except BaseException:
        pass


def _disarm_deadline() -> None:
    """Cancel both layers. Matters when main() is called in-process."""
    global _deadline_timer
    try:
        import signal

        signal.setitimer(signal.ITIMER_REAL, 0)
    except BaseException:
        pass
    try:
        if _deadline_timer is not None:
            _deadline_timer.cancel()
    except BaseException:
        pass
    _deadline_timer = None


def _read_stdin() -> bytes:
    """Read the payload without ever blocking past STDIN_TIMEOUT_S."""
    import select

    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return b""
    try:
        if os.isatty(fd):
            return b""
    except OSError:
        return b""
    chunks = []
    total = 0
    deadline = time.time() + STDIN_TIMEOUT_S
    while total < STDIN_MAX_BYTES:
        remaining = deadline - time.time()
        if remaining <= 0:
            _log("warn", "stdin read timed out", bytes=total)
            break
        try:
            ready, _, _ = select.select([fd], [], [], remaining)
        except (OSError, ValueError):
            break
        if not ready:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _transcript_tail(path: object) -> list:
    """The last TRANSCRIPT_TAIL_BYTES of the JSONL transcript, as lines;
    [] if it cannot be read (or is not a regular file)."""
    if not isinstance(path, str) or not path:
        return []
    import stat

    try:
        # O_NONBLOCK: opening a FIFO with no writer would otherwise block.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                return []
            fh.seek(max(0, st.st_size - TRANSCRIPT_TAIL_BYTES))
            data = fh.read(TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()


def _codex_entry_texts(entry: dict):
    """(message id, [text blocks]) of an assistant message in a Codex
    rollout transcript: {"type": "response_item", "payload": {"type":
    "message", "role": "assistant", "content": [{"type": "output_text",
    "text": ...}]}}. (None, []) for any other entry."""
    p = entry.get("payload")
    if not isinstance(p, dict) or p.get("type") != "message" or p.get("role") != "assistant":
        return None, []
    content = p.get("content")
    texts = [
        b.get("text", "")
        for b in (content if isinstance(content, list) else [])
        if isinstance(b, dict) and b.get("type") == "output_text" and isinstance(b.get("text"), str)
    ]
    return p.get("id"), texts


def _is_codex_turn_start(entry: dict) -> bool:
    """A Codex rollout line that opens a turn: {"type": "event_msg",
    "payload": {"type": "task_started", ...}}."""
    p = entry.get("payload")
    return entry.get("type") == "event_msg" and isinstance(p, dict) and p.get("type") == "task_started"


def _entry_texts(entry: dict):
    """(message id, [text blocks]) of an assistant transcript entry."""
    msg = entry.get("message")
    if not isinstance(msg, dict):
        return None, []
    content = msg.get("content")
    if isinstance(content, str):
        texts = [content]
    else:
        texts = [
            b.get("text", "")
            for b in (content if isinstance(content, list) else [])
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
    return msg.get("id") or entry.get("uuid"), texts


def _turn_messages_from_transcript(path: object, needle: str = "") -> list:
    """The text of each assistant message of the current turn (everything
    after the last real user prompt), oldest first. Claude writes a message
    to the transcript before the tool call that follows it, so at Stop this
    holds the messages written before tool calls as well. Codex rollout
    transcripts work the same way: a turn starts at its task_started event
    and assistant messages are response_item entries. With `needle`,
    only messages containing it are returned, and other assistant lines are
    not even parsed (Stop calls this on every turn)."""
    import json

    lines = _transcript_tail(path)
    if needle and not any(needle in line for line in lines):
        return []
    order = []
    by_id = {}
    for line in reversed(lines):
        if needle and needle not in line and '"user"' not in line and '"task_started"' not in line:
            continue  # neither a turn boundary nor a message we want
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        if kind == "user" and _is_user_prompt(entry):
            break
        if _is_codex_turn_start(entry):
            break
        if kind == "assistant":
            mid, texts = _entry_texts(entry)
        elif kind == "response_item":
            mid, texts = _codex_entry_texts(entry)
        else:
            continue
        if not texts:
            continue
        key = mid if isinstance(mid, str) else id(entry)
        if key not in by_id:
            by_id[key] = []
            order.append(key)
        by_id[key][:0] = texts
    return ["\n".join(by_id[k]) for k in reversed(order)]


def _turn_voice(path: object):
    """The last <voice> tag written in the current turn, or None. Used when
    the final message has none, so a tag in a message before a tool call
    still counts. (The final message may or may not be in the transcript
    yet; with no tag of its own it changes nothing here.) Each message is
    parsed on its own, so code masking works as for Stop."""
    import tags

    voice = None
    for text in _turn_messages_from_transcript(path, needle="<voice"):
        got = tags.parse(text).voice
        if got is not None:
            voice = got
    return voice


def _last_message_from_transcript(path: object) -> str:
    """Fallback when the payload lacks `last_assistant_message`: the text of
    the last assistant message in the JSONL transcript."""
    import json

    lines = _transcript_tail(path)
    if not lines:
        return ""
    target_id = None
    texts = []
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "user":
            if _is_user_prompt(entry):
                break  # never look past the start of the current turn
            if target_id is not None:
                break
            continue
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        block_texts = [
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]
        if isinstance(content, str):
            block_texts = [content]
        mid = msg.get("id") or entry.get("uuid")
        if target_id is None:
            if not block_texts:
                continue
            target_id = mid
        elif mid != target_id:
            break
        texts[:0] = block_texts
    if texts:
        return "\n".join(texts)
    # Codex rollout transcripts (Stop's last_assistant_message can be null).
    msgs = _turn_messages_from_transcript(path)
    return msgs[-1] if msgs else ""


def _is_user_prompt(entry: dict) -> bool:
    """A real prompt typed by the user, as opposed to a tool_result entry."""
    msg = entry.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    return False


# --------------------------------------------------------------------------- #
# Talking to the daemon                                                       #
# --------------------------------------------------------------------------- #


class DaemonUnreachable(Exception):
    pass


def _send_to_daemon(message: dict, wait_ack: bool = True) -> dict:
    """Send one JSON line, wait briefly for a one-line JSON ack.

    Raises DaemonUnreachable if we could not connect (safe to fall back).
    Any other failure after connecting returns {"ok": False, ...} and the
    caller must NOT fall back, or the user could hear the line twice.
    """
    import json
    import socket

    path = str(_common.socket_path())
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(CONNECT_TIMEOUT_S)
        try:
            sock.connect(path)
        except (OSError, ValueError) as exc:
            raise DaemonUnreachable("%s: %s" % (type(exc).__name__, exc))
        try:
            sock.settimeout(ACK_TIMEOUT_S)
            # ensure_ascii: lone surrogates go out as \\u escapes instead of
            # raising UnicodeEncodeError.
            sock.sendall(json.dumps(message).encode("ascii") + b"\n")
            if not wait_ack:
                return {"ok": True, "sent": True}
            buf = b""
            while b"\n" not in buf and len(buf) < 65536:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            ack = json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "error": "bad ack %r" % buf[:200]}
        return ack if isinstance(ack, dict) else {"ok": False, "error": "bad ack"}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _spawn_detached(argv: list, data: bytes) -> None:
    """Start `argv` in its own session, write `data` to its stdin, and do not
    wait for it."""
    import subprocess

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    try:
        proc.stdin.write(data)  # type: ignore[union-attr]
    finally:
        proc.stdin.close()  # type: ignore[union-attr]
    proc.returncode = 0  # detached on purpose; silences the "still running" ResourceWarning


# Runs `say` once per part, in order, in a detached child (the hook never
# waits). A part `say` refuses (exit status > 0, e.g. a voice it cannot use)
# is retried with the system voice. A `say` killed by a signal (`killall
# say`) ends the whole run, and one that runs past its time limit (generous:
# about 3x the time the text takes to speak) is not retried.
#
# One line at a time: before its first part the runner takes an exclusive
# flock on <home>/say.lock and holds it until its last part is done, so
# parallel Stops (and alerts) with the daemon down speak one after another,
# not over each other. A runner that waited SAY_LOCK_WAIT_S for the lock
# drops its line (stale by then) and logs that to hook.log. A lock file that
# cannot be opened or locked never silences a line: it is spoken anyway.
# The daemon does not take this lock, so a fallback line started while the
# daemon was down can overlap the daemon's first line once it is back.
#
# stdin: {"jobs": [...], "lock": path, "wait": s, "hooks": dir}, or a bare
# list of jobs (no lock).
_SAY_RUNNER = r"""
import fcntl, json, os, signal, stat, subprocess, sys, time
req = json.loads(sys.stdin.read())
if isinstance(req, list):
    req = {"jobs": req}
jobs = req["jobs"]
lock = req.get("lock")
class Late(Exception):
    pass
def late(*_args):
    raise Late()
if lock:
    t0 = time.time()
    fd = None
    try:
        os.makedirs(os.path.dirname(lock), 0o700, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            fd = None
    except OSError:
        fd = None
    if fd is not None:
        signal.signal(signal.SIGALRM, late)
        try:
            signal.setitimer(signal.ITIMER_REAL, max(0.01, float(req.get("wait") or 60)))
            fcntl.flock(fd, fcntl.LOCK_EX)
            signal.setitimer(signal.ITIMER_REAL, 0)
        except Late:
            try:
                sys.path.insert(0, req["hooks"])
                import common
                common.append_jsonl(common.hook_log_path(), {
                    "ts": time.time(), "pid": os.getpid(), "level": "warn",
                    "msg": "say fallback dropped: waited too long for say.lock",
                    "waited_s": round(time.time() - t0, 1), "parts": len(jobs),
                    "chars": sum(len(j["text"]) for j in jobs)}, common.HOOK_LOG_MAX_BYTES, rotate=True)
            except Exception:
                pass
            sys.exit(0)
        except OSError:
            signal.setitimer(signal.ITIMER_REAL, 0)
for job in jobs:
    data = job["text"].encode("utf-8", "replace")
    limit = job.get("limit") or 60 + 0.2 * len(job["text"])
    for argv in job["argvs"]:
        try:
            r = subprocess.run(argv, input=data, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=limit)
        except subprocess.TimeoutExpired:
            break
        except Exception:
            continue
        if r.returncode < 0:
            sys.exit(0)
        if r.returncode == 0:
            break
"""

SAY_LOCK_NAME = "say.lock"  # in <home>; `afterword test` takes it too
SAY_LOCK_WAIT_S = 60.0


def _say_fallback(parts: list) -> None:
    """Speak [(text, macOS voice or None)] with `say`, in order, detached,
    always through _SAY_RUNNER (which waits its turn on say.lock). Text goes
    over stdin so a leading '-' can never be read as an option; `say`'s
    embedded [[commands]] are stripped first."""
    import json
    import shutil
    import tags

    base = _common.say_argv()
    jobs = []
    for text, voice in parts:
        text = tags.strip_speech_commands(text)
        if not text.strip():
            continue
        argvs = [base + ["-v", voice], base] if voice else [base]
        jobs.append({"argvs": argvs, "text": text})
    if not jobs:
        return
    if not base or shutil.which(base[0]) is None:
        raise FileNotFoundError("say command not found: %r" % (base[:1],))
    request = {"jobs": jobs, "lock": str(_common.home() / SAY_LOCK_NAME), "wait": SAY_LOCK_WAIT_S, "hooks": _HERE}
    python = sys.executable or "/usr/bin/python3"
    _spawn_detached([python, "-I", "-c", _SAY_RUNNER], json.dumps(request).encode("utf-8", "replace"))


def _lang_code(value: object):
    """ISO code for an ISO code, regional code or language name; else None."""
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace("_", "-")
    if v in _LANG_NAMES:
        return v
    for code, name in _LANG_NAMES.items():
        if v == name.lower():
            return code
    head = v.split("-", 1)[0]
    return head if head in _LANG_NAMES and head != v else None


def _profile_table(cfg: dict, profile, borrow: bool = True) -> dict:
    """[profiles.<profile>] from the raw config; an unknown name borrows the
    first profile's table (as the daemon does) when `borrow`."""
    profiles = cfg.get("profiles") if isinstance(cfg, dict) else None
    if not isinstance(profiles, dict):
        return {}
    table = profiles.get(profile) if isinstance(profile, str) else None
    if table is None and borrow:
        table = next((v for v in profiles.values() if isinstance(v, dict)), None)
    return table if isinstance(table, dict) else {}


def _lang_tables(table: dict) -> dict:
    """{iso: [profiles.x.langs.<lang>] table}, first spelling of a language wins."""
    langs = table.get("langs") if isinstance(table, dict) else None
    out = {}
    if isinstance(langs, dict):
        for key, sub in langs.items():
            code = _lang_code(key)
            if code and code not in out and isinstance(sub, dict):
                out[code] = sub
    return out


def _fallback_parts(segments: list, cfg: dict, profile) -> list:
    """[(text, say voice)] for the `say` fallback: each <say> after the
    pronunciation list and, for a profile with per-language voices, split by
    script like the daemon does; each part gets its language's say_voice."""
    table = _profile_table(cfg, profile)
    langs = _lang_tables(table)
    own = table.get("say_voice") if isinstance(table.get("say_voice"), str) else None
    primary = _lang_code(table.get("lang"))

    import scriptsplit

    def voice(code):
        # Same rule as the daemon's Config.voice_for.
        sub = (langs.get(code) if code else None) or {}
        return scriptsplit.say_voice_for(code, primary, sub.get("say_voice"), sub.get("voice"), own)

    parts = []
    for seg in segments:
        text = _speakable(seg["text"], cfg, profile)
        lang = seg["attrs"].get("lang")
        if isinstance(lang, str) and lang and lang != "auto":
            parts.append((text, voice(_lang_code(lang))))
        elif langs:
            import scriptsplit

            other = next((c for c in langs if c not in scriptsplit.CJK_LANGS), None)
            for code, part in scriptsplit.split(text, primary, other):
                parts.append((part, voice(code)))
        else:
            parts.append((text, voice(primary)))
    return parts


def _speakable(text: str, cfg: dict, profile) -> str:
    """`text` after the [pronounce] tables (global plus the profile's), as the
    daemon would speak it. Any problem: the text unchanged."""
    try:
        import pronounce

        return pronounce.apply(text, pronounce.pairs_from_config(cfg, profile))
    except Exception as exc:
        _log("error", "pronunciation list not applied", error=repr(exc))
        return text


# --------------------------------------------------------------------------- #
# Events                                                                      #
# --------------------------------------------------------------------------- #


def handle_stop(payload: dict) -> dict:
    """Process a Stop payload. Returns a summary dict (for logs and tests)."""
    import tags

    c = _common
    session_id = payload.get("session_id")
    if not isinstance(session_id, str):
        session_id = ""

    message = payload.get("last_assistant_message")
    source = "payload"
    if not isinstance(message, str):
        message = _last_message_from_transcript(payload.get("transcript_path"))
        source = "transcript"

    try:
        cfg = c.read_config_raw()
        cfg_error = None
    except Exception as exc:
        cfg = {}
        cfg_error = "%s: %s" % (type(exc).__name__, exc)
        _log("error", "config unreadable, using defaults", error=cfg_error)

    parsed = tags.parse(message)
    state = c.load_state(session_id)
    voice = parsed.voice
    if voice is None:
        # A <voice> tag in an earlier message of this turn (written before a
        # tool call) would otherwise be lost: Stop only gets the last one.
        try:
            voice = _turn_voice(payload.get("transcript_path"))
        except Exception as exc:
            _log("error", "could not read the turn's earlier messages", error=repr(exc))
            voice = None
    state_changed = False
    if voice is not None and voice.profile != state.get("profile") and not _already_claimed("stop", session_id, payload, message):
        state["profile"] = voice.profile
        if voice.profile:
            state["last_profile"] = voice.profile
        state["cwd"] = payload.get("cwd")
        state["source"] = "voice-tag"
        state_changed = True
    if state_changed and session_id:
        _update_state(session_id, _merge_keys(state, ("profile", "last_profile", "cwd", "source")))

    profile = state.get("profile")
    segments = []
    text = ""
    duplicate = bool(session_id) and not claim("stop", session_id, _turn_key(payload, message), _turn_ttl(payload))
    if duplicate:
        decision, reason = "skip", "duplicate: another AfterWord hook already handled this reply"
    elif not profile:
        decision, reason = "skip", "session off"
    elif not parsed.says:
        decision, reason = "skip", "no <say> tag"
    turn = payload.get("prompt_id") if isinstance(payload.get("prompt_id"), str) else None
    early_dropped = 0
    if not duplicate and profile and parsed.says:
        # Each <say> truncated to max_chars, all of them to 2 x max_chars;
        # then the ones already spoken early (MessageDisplay) are left out.
        segments = tags.limit_says(parsed.says, c.profile_max_chars(cfg, profile), keep_index=True)
        segments, turn, early_dropped = _early_filter(session_id, turn, message, parsed, segments)
        text = " ".join(seg["text"] for seg in segments)
        if segments:
            decision, reason = "speak", None
        elif early_dropped:
            decision, reason = "skip", "spoken early"
        else:
            decision, reason = "skip", "no <say> tag"

    request = {
        "v": 1,
        "type": "hook",
        "event": "Stop",
        "t_hook": _T0,
        "t_sent": time.time(),
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "profile": profile,
        "decision": decision,
        "reason": reason,
        "text": text,
        "attrs": dict(segments[0]["attrs"]) if (segments and decision == "speak") else {},
        "segments": segments if decision == "speak" else [],
        "turn": turn,
        "early_dropped": early_dropped,
        "parsed": parsed.to_dict(),
        "message_source": source,
        "stop_hook_active": bool(payload.get("stop_hook_active")),
        "state_changed": state_changed,
        "config_error": cfg_error,
        "payload": c.project_payload(payload),
    }

    outcome = {"decision": decision, "reason": reason, "text": text, "profile": profile}
    # Lines spoken early (MessageDisplay) count as use too: a session that
    # only speaks that way would otherwise look idle in voices.json.
    if session_id and not duplicate and (state_changed or decision == "speak" or early_dropped):
        _note_voice(session_id, profile, payload.get("cwd"))
    if early_dropped:
        outcome["early_dropped"] = early_dropped
    alerts = _alert_settings(cfg)
    done = None if duplicate else _done_alert(alerts, state, decision, early_dropped)
    if done is not None:
        request["alert"] = done  # the daemon decides (its settings, mute, early lines)
        outcome["alert"] = done
    turn_keys = ("turn_started", "pending_permissions", "pending_permission")
    if session_id and not duplicate and any(k in state for k in turn_keys):
        def end_turn(st):
            if not any(k in st for k in turn_keys):
                return False
            for k in turn_keys:
                st.pop(k, None)

        _update_state(session_id, end_turn)
    try:
        ack = _send_to_daemon(request)
        outcome["delivered"] = "daemon"
        outcome["ack"] = ack
        if not ack.get("ok"):
            _log("error", "daemon did not acknowledge", ack=ack)
    except DaemonUnreachable as exc:
        outcome["delivered"] = "spool"
        muted = "all muted" if c.muted_all() else ("session muted" if state.get("muted") is True else None)
        if decision == "speak" and muted:
            decision, reason = "skip", muted
            request["decision"], request["reason"] = decision, reason
            outcome["decision"], outcome["reason"] = decision, reason
        if decision == "speak" and c.fallback_mode(cfg) == "say":
            try:
                _say_fallback(_fallback_parts(segments, cfg, profile))
                outcome["fallback"] = "say"
            except Exception as fexc:
                outcome["fallback"] = "error"
                outcome["fallback_error"] = repr(fexc)
        else:
            outcome["fallback"] = "none"
        spooled = dict(request)
        spooled.pop("alert", None)
        spooled["unreachable"] = str(exc)
        spooled["fallback"] = outcome["fallback"]
        spooled["fallback_error"] = outcome.get("fallback_error")
        try:
            c.append_jsonl(c.spool_path(), spooled, c.SPOOL_MAX_BYTES, rotate=False)
        except Exception as sexc:
            _log("error", "could not spool event", error=repr(sexc))
        if done is not None:
            # The "done" alert gets its own record, like the daemon's own row.
            alert_req = {k: request[k] for k in ("v", "type", "event", "t_hook", "session_id", "cwd", "profile")}
            alert_req.update(t_sent=time.time(), decision="info", reason="alert: done", text="", alert=done,
                             payload=c.project_payload(payload))
            got = _alert_down(alerts, cfg, state, alert_req, done, exc)
            outcome["alert_fallback"] = got["fallback"]
    return outcome


# --------------------------------------------------------------------------- #
# MessageDisplay: early speaking                                              #
# --------------------------------------------------------------------------- #


def _quiet(reason: str) -> dict:
    """A MessageDisplay call that did nothing: not written to hook.log (a
    long message makes many calls)."""
    return {"decision": "none", "reason": reason, "quiet": True}


def handle_message_display(payload: dict) -> dict:
    """Speak each newly completed <say> block of a message while Claude is
    still writing (or, in -p, as soon as a message before a tool call is
    done). On any doubt it does nothing: Stop then speaks the line as in v1.
    Prints nothing (the display is never changed)."""
    c = _common
    session_id = payload.get("session_id")
    message_id = payload.get("message_id")
    index = payload.get("index")
    delta = payload.get("delta")
    if not isinstance(session_id, str) or not session_id:
        return _quiet("no session")
    if not isinstance(delta, str):
        delta = ""
    # Fast path: the voice is off for most sessions.
    state = c.load_state(session_id)
    if "pending_permissions" in state or "pending_permission" in state:
        # Claude is writing again, so the permission prompts it asked
        # before were answered (spoken alerts: alerting.take_pending).
        import alerting

        now = time.time()
        _update_state(session_id, lambda st: alerting.drop_answered(st, now))
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    if not profile:
        return _quiet("session off")
    if state.get("muted") is True or c.muted_all():
        return _quiet("muted")
    import earlysay

    if not earlysay.valid_id(message_id) or isinstance(index, bool) or not isinstance(index, int) \
            or not 0 <= index <= earlysay.MAX_INDEX:
        return _quiet("bad payload")
    final = payload.get("final") is True
    has_say = "<say" in delta.lower()
    if index == 0 and final and not has_say:
        return _quiet("no <say>")  # a whole message (-p) without a tag
    # The turn is Claude's prompt_id, the same value Stop carries: the daemon
    # dedupes per (session, turn) and the queue orders a turn's items by it.
    # Without one (turn_id is a different value) nothing could match the
    # early blocks to Stop's, so the message is left to Stop.
    turn = payload.get("prompt_id")
    if not earlysay.valid_id(turn):
        return _quiet("no prompt_id")
    cfg = _read_cfg()
    if not c.early_say(cfg):
        return _quiet("early_say off")
    with earlysay.locked(session_id) as got:
        if not got:
            return {"decision": "skip", "reason": "stream file busy"}
        return _early_locked(payload, session_id, message_id, index, final, delta, turn, profile, cfg)


def _early_locked(payload, session_id, message_id, index, final, delta, turn, profile, cfg) -> dict:
    import earlysay
    import tags

    c = _common
    now = time.time()
    data = earlysay.load(session_id)
    earlysay.prune(data, now)
    msgs = data["messages"]
    rec = msgs.get(message_id)
    if rec is None:
        if "<say" not in delta.lower() and index == 0 and final:
            return _quiet("no <say>")
        rec = msgs[message_id] = {"turn": turn, "t": now, "parts": {}, "sent": {}}
        earlysay.prune_files(now, keep=session_id)
    rec["updated"] = now
    if rec.get("done"):
        earlysay.save(session_id, data)
        return _quiet(rec["done"])
    earlysay.add_batch(rec, index, delta, final)
    text, complete = earlysay.joined(rec)
    if rec.get("done") or "<say" not in text.lower():
        if complete and not rec.get("done"):
            earlysay.finish(rec, text)
        earlysay.save(session_id, data)
        return _quiet(rec.get("done") or "no <say> yet")
    parsed = tags.parse(text)
    if parsed.voices:
        # A <voice> tag changes the session: Stop decides, as in v1.
        rec["done"] = "voice tag"
        earlysay.save(session_id, data)
        return {"decision": "skip", "reason": "voice tag: left to Stop"}
    ready = earlysay.stable_count(text, parsed, complete)
    segments = tags.limit_says(parsed.says[:ready], c.profile_max_chars(cfg, profile), keep_index=True)
    keyed = earlysay.segments_with_keys(parsed.says, segments, message_id)
    new = [(i, seg) for i, seg in keyed if seg["early_key"] not in rec["sent"]]
    if complete:
        earlysay.finish(rec, text)
    # Store the batch before talking to the daemon (the deadline could hit).
    earlysay.save(session_id, data)
    if not new:
        return _quiet("nothing new")
    sending = [seg for _i, seg in new]
    text_out = " ".join(seg["text"] for seg in sending)
    request = {
        "v": 1,
        "type": "hook",
        "event": "early",
        "t_hook": _T0,
        "t_sent": time.time(),
        "session_id": session_id,
        "cwd": payload.get("cwd"),
        "profile": profile,
        "decision": "speak",
        "reason": None,
        "text": text_out,
        "attrs": dict(sending[0]["attrs"]),
        "segments": sending,
        "turn": turn,
        "message_id": message_id,
        "says": [i for i, _seg in new],
        "parsed": parsed.to_dict(),
        "payload": c.project_payload(payload),
    }
    outcome = {"decision": "speak", "reason": None, "text": text_out, "profile": profile,
               "message_id": message_id, "index": index, "says": request["says"]}
    try:
        ack = _send_to_daemon(request)
    except DaemonUnreachable as exc:
        # Nothing is spoken now: Stop speaks the line (or falls back to `say`).
        outcome.update(decision="skip", reason="daemon unreachable: left to Stop", error=str(exc))
        return outcome
    outcome["delivered"] = "daemon"
    outcome["ack"] = ack
    taken = ack.get("taken") if isinstance(ack.get("taken"), list) else []
    if not ack.get("ok"):
        _log("error", "daemon did not acknowledge", ack=ack)
    sent_now = 0
    for i, seg in new:
        if seg["early_key"] in taken:
            rec["sent"][seg["early_key"]] = {"i": i, "digest": seg["digest"], "t": time.time(),
                                             "event_id": ack.get("event_id")}
            sent_now += 1
    outcome["taken"] = sent_now
    if sent_now:
        try:
            earlysay.save(session_id, data)
        except Exception as exc:
            _log("error", "could not save the stream file", error=repr(exc))
    return outcome


def _early_filter(session_id: str, turn, message: str, parsed, segments: list):
    """Stop: the segments not already spoken early, each with its
    `early_key`/`digest`; also the turn (from the stream file when the
    payload has none) and how many were dropped. Never raises."""
    try:
        import earlysay

        data = earlysay.load(session_id)
        mid = earlysay.find_message(data, turn, message)
        if mid is not None and turn is None:
            rec_turn = data["messages"][mid].get("turn")
            turn = rec_turn if earlysay.valid_id(rec_turn) else None
        sent_keys = set(data["messages"][mid]["sent"]) if mid is not None else set()
        sent_digests = earlysay.turn_digests(data, turn)
        keyed = earlysay.segments_with_keys(parsed.says, segments, mid)
        kept = [seg for _i, seg in keyed
                if seg.get("early_key") not in sent_keys and seg["digest"] not in sent_digests]
        return kept, turn, len(keyed) - len(kept)
    except Exception as exc:
        _log("error", "stream file not read", error=repr(exc))
        for seg in segments:
            seg.pop("i", None)
        return segments, turn, 0


# --------------------------------------------------------------------------- #
# De-duplication                                                              #
# --------------------------------------------------------------------------- #

CLAIM_TTL_S = 600


CLAIM_TTL_NO_ID_S = 15  # without a per-turn id, only catch parallel duplicates
# Alerts: neither PermissionRequest nor Notification has a request id, so the
# key is their content, and a second prompt for the same tool can follow a
# few seconds later. Duplicate hooks (plugin + settings) run within ~1 s.
CLAIM_TTL_ALERT_S = 3


def claim(kind: str, session_id: str, key: str, ttl: float = CLAIM_TTL_S) -> bool:
    """True the first time (kind, session, key) is seen; False for a second
    AfterWord hook handling the same turn (e.g. installed both as a plugin
    and in settings.json). Atomic across parallel hook processes."""
    import hashlib

    c = _common
    d = c.home() / "seen"
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        return True  # cannot record: never drop speech because of this
    digest = hashlib.sha256(("%s\0%s" % (session_id, key)).encode("utf-8", "replace")).hexdigest()[:24]
    path = d / ("%s-%s" % (kind, digest))
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        claimed = True
    except FileExistsError:
        try:
            claimed = time.time() - path.stat().st_mtime > ttl  # stale marker: reuse
            if claimed:
                os.utime(str(path), None)
        except OSError:
            claimed = True
    except OSError:
        claimed = True
    _prune_claims(d)
    return claimed


def _prune_claims(d) -> None:
    now = time.time()
    try:
        with os.scandir(str(d)) as it:
            for entry in it:
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime > CLAIM_TTL_S:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def _already_claimed(kind: str, session_id: str, payload: dict, text: object) -> bool:
    """Peek (without claiming) whether another hook took this turn, so a
    duplicate does not also rewrite state."""
    import hashlib

    if not session_id:
        return False
    digest = hashlib.sha256(("%s\0%s" % (session_id, _turn_key(payload, text))).encode("utf-8", "replace")).hexdigest()[:24]
    path = _common.home() / "seen" / ("%s-%s" % (kind, digest))
    try:
        return time.time() - path.stat().st_mtime <= _turn_ttl(payload)
    except OSError:
        return False


def _turn_id(payload: dict):
    """The turn's id for claims: Claude Code's prompt_id, else Codex's
    turn_id (Codex sends no prompt_id). None if neither is there."""
    for key in ("prompt_id", "turn_id"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _turn_ttl(payload: dict) -> float:
    return CLAIM_TTL_S if _turn_id(payload) else CLAIM_TTL_NO_ID_S


def _turn_key(payload: dict, text: object) -> str:
    return "%s\0%s" % (_turn_id(payload) or "", text if isinstance(text, str) else "")


# --------------------------------------------------------------------------- #
# UserPromptSubmit / SessionStart (build step 5)                              #
# --------------------------------------------------------------------------- #

# Every language some engine speaks; a copy of voices.LANGUAGE_NAMES (the
# hook cannot import the daemon; tests keep the two equal).
_LANG_NAMES = {
    "en": "English", "zh": "Chinese", "ja": "Japanese", "ko": "Korean", "de": "German",
    "fr": "French", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "hi": "Hindi", "ar": "Arabic", "da": "Danish", "el": "Greek", "fi": "Finnish",
    "he": "Hebrew", "ms": "Malay", "nl": "Dutch", "no": "Norwegian", "pl": "Polish",
    "sv": "Swedish", "sw": "Swahili", "tr": "Turkish",
}


def _profile_names(cfg: dict) -> list:
    profiles = cfg.get("profiles") if isinstance(cfg, dict) else None
    if not isinstance(profiles, dict):
        return []
    return [k for k, v in profiles.items() if isinstance(v, dict)]


def _profile_language(cfg: dict, profile: str) -> str:
    lang = ""
    try:
        lang = str(cfg["profiles"][profile].get("lang") or "")
    except (KeyError, TypeError, AttributeError):
        pass
    lang = lang.strip().lower().replace("_", "-")
    if not lang or lang == "auto":
        return "the language of the conversation"
    return _LANG_NAMES.get(lang.split("-", 1)[0], lang.capitalize())


def _profile_langs(cfg: dict, profile: str) -> list:
    """The languages of a profile with [profiles.x.langs.*] tables: the
    primary language first, then the others; [] for a profile without."""
    table = _profile_table(cfg, profile, borrow=False)
    langs = list(_lang_tables(table))
    if not langs:
        return []
    primary = _lang_code(table.get("lang"))
    return ([primary] if primary else []) + [code for code in langs if code != primary]


def say_instruction(cfg: dict, profile: str) -> str:
    """The ~40-token note added to each prompt while a session's voice is on."""
    langs = _profile_langs(cfg, profile)
    if langs:
        return (
            "AfterWord voice is on (profile %s: %s). Add <say lang=\"%s\">1-2 plain spoken "
            "sentences</say>, one per language if you like; first one at the start is heard sooner "
            "(optional mood=\"...\"; no markdown inside)." % (profile, ", ".join(langs), "|".join(langs))
        )
    return (
        "AfterWord voice is on (profile %s). End your reply with <say>1-2 plain spoken "
        "sentences summing it up, in %s</say>, or open with it to be heard sooner "
        "(optional mood=\"...\"; no markdown inside)." % (profile, _profile_language(cfg, profile))
    )


def _voice_command(prompt: object):
    """'/afterword:voice nova' (plugin) or '/afterword-voice nova'
    (install-hooks) -> 'nova'; no argument -> ''. None otherwise. Codex
    rejects slash commands it doesn't know, so there the whole prompt is
    typed without the slash: 'afterword:voice nova'."""
    import re

    if not isinstance(prompt, str):
        return None
    # Not a bare /voice: Claude Code has a built-in /voice (dictation).
    m = re.match(r"\s*/afterword[:-]voice(?:\s+(.*?))?\s*\Z", prompt, re.S)
    if m:
        return (m.group(1) or "").strip()
    # Without the slash, only a one-line prompt of at most one word after it,
    # so a sentence that starts with "afterword-voice ..." stays a prompt.
    m = re.match(r"[ \t\r\n]*afterword[:-]voice(?:[ \t]+(\S+))?[ \t\r\n]*\Z", prompt)
    if m:
        return m.group(1) or ""
    return None


def apply_voice_command(session_id: str, arg: str, cfg: dict, state: dict, cwd: object):
    """Apply /voice <arg>. Returns (note for Claude, state changed?)."""
    import tags

    names = _profile_names(cfg)
    listed = ", ".join(names) if names else "none configured"
    word = arg.strip()
    low = word.lower()
    current = state.get("profile")
    changed = False

    def describe():
        voice = current or "off"
        return "AfterWord voice for this session: %s%s. Profiles: %s." % (
            voice, " (muted)" if state.get("muted") else "", listed)

    if low == "status" or (low == "" and current):
        return describe() + " Tell the user this in one short sentence.", False
    if low == "" or low in PICK_WORDS:
        return voice_menu(session_id, cfg, state), False
    if low in ("off", "stop", "none"):
        state["profile"] = None
        changed = current is not None
        note = "AfterWord voice is now off for this session. Confirm in one short sentence."
    elif low in ("mute", "unmute"):
        state["muted"] = low == "mute"
        changed = True
        note = "AfterWord voice is %s for this session. Confirm in one short sentence." % (
            "muted (still on, nothing is played)" if low == "mute" else "unmuted")
    else:
        target = word
        if low == "on":
            target = state.get("last_profile") or (names[0] if names else "")
        if not target or not tags.PROFILE_NAME_RE.match(target) or (names and target not in names):
            # Not a profile name: what the user wants ("something calm", a
            # typo) is Claude's to match against the profiles.
            return voice_menu(session_id, cfg, state, hint=word), False
        state["profile"] = target
        state["last_profile"] = target
        changed = target != current
        note = "AfterWord voice is now on for this session with profile %s. Confirm in one short sentence." % target
    if session_id:
        state["source"] = "voice-command"
        if isinstance(cwd, str):
            state["cwd"] = cwd
        _common.update_state(session_id, _merge_keys(state, ("profile", "last_profile", "muted", "source", "cwd")))
        if low not in ("mute", "unmute"):
            _note_voice(session_id, state.get("profile"), cwd)
    return note, changed


PICK_WORDS = ("pick", "auto", "any", "choose", "random", "?")
VOICE_IN_USE_S = 2 * 3600  # another session that chose or spoke with a voice this recently is using it
VOICE_MENU_MAX = 12
HINT_MAX_CHARS = 200


def _note_voice(session_id: str, profile, cwd) -> None:
    """common.note_voice, never raising (the usage log is a nicety)."""
    if not session_id:
        return
    try:
        _common.note_voice(session_id, profile if isinstance(profile, str) else None, cwd)
    except Exception as exc:
        _log("error", "could not update voices.json", error=repr(exc))


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return "%d min ago" % round(seconds / 60)
    if seconds < 36 * 3600:
        return "%d h ago" % round(seconds / 3600)
    return "%d d ago" % round(seconds / 86400)


def _profile_blurb(cfg: dict, name: str) -> str:
    """'calm, slightly wry (builtin, en)': the profile's description (else its
    persona, else its style), its voice kind and languages."""
    import re

    table = _profile_table(cfg, name, borrow=False)
    desc = ""
    for key in ("description", "persona", "style"):
        v = table.get(key)
        if isinstance(v, str) and v.strip():
            desc = re.sub(r"\s+", " ", v).strip()
            break
    if len(desc) > 70:
        desc = desc[:69].rstrip() + "\u2026"
    voice = table.get("voice") if isinstance(table.get("voice"), str) else ""
    kind = voice.split(":", 1)[0].strip() if ":" in voice else ("say" if not voice else voice)
    langs = _profile_langs(cfg, name) or [_lang_code(table.get("lang")) or "auto"]
    tail = "%s, %s" % (kind, "/".join(langs))
    return "%s (%s)" % (desc, tail) if desc else "(%s)" % tail


def voice_menu(session_id: str, cfg: dict, state: dict, hint: str = "", now=None) -> str:
    """The note that asks Claude to pick this session's voice: the profiles,
    free ones and the least recently used first, each with a short
    description; Claude answers with <voice set="..."/>. About 150 tokens."""
    import json

    names = _profile_names(cfg)
    if not names:
        return ("AfterWord has no voice profiles configured, so the voice is unchanged. Tell the user to "
                "add a [profiles.<name>] table to ~/.afterword/config.toml (or use the voice lab).")
    now = time.time() if now is None else now
    try:
        usage = _common.load_voice_usage()
    except Exception:
        usage = {}
    current = state.get("profile") if isinstance(state.get("profile"), str) else None
    rows = []
    for name in names:
        u = usage.get(name) or {}
        others = [sid for sid, s in (u.get("sessions") or {}).items()
                  if sid != session_id and now - s["ts"] < VOICE_IN_USE_S]
        last = u.get("last_used")
        if name == current:
            status = "this session's voice now"
        elif others:
            status = "in use by %d other session%s" % (len(others), "" if len(others) == 1 else "s")
        elif last:
            status = "free, last used %s" % _ago(max(0.0, now - last))
        else:
            status = "free, never used"
        rows.append(((bool(others), name == current, last or 0.0), "- %s: %s; %s" % (name, _profile_blurb(cfg, name), status)))
    rows.sort(key=lambda r: r[0])
    lines = [line for _, line in rows[:VOICE_MENU_MAX]]
    if len(rows) > VOICE_MENU_MAX:
        lines.append("- (%d more in use)" % (len(rows) - VOICE_MENU_MAX))
    hint = " ".join(hint.split())
    if len(hint) > HINT_MAX_CHARS:
        hint = hint[:HINT_MAX_CHARS] + "..."
    asked = " The user asked for: %s." % json.dumps(hint, ensure_ascii=False) if hint else ""
    return ("AfterWord: the user wants you to pick this session's voice.%s Profiles, best first:\n%s\n"
            "Choose the one that fits best, preferring a free one (if the request names a profile, "
            "maybe misspelled, take that one). Reply with one short sentence saying which voice and why, "
            "and include <voice set=\"NAME\"/> and <say>a short line in that voice</say>."
            % (asked, "\n".join(lines)))


def _notify(event: str, payload: dict, profile, reason: str, state_changed: bool) -> str:
    """Log a non-Stop hook call with the daemon (fire and forget), or spool it."""
    c = _common
    request = {
        "v": 1,
        "type": "hook",
        "event": event,
        "t_hook": _T0,
        "t_sent": time.time(),
        "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else "",
        "cwd": payload.get("cwd"),
        "profile": profile,
        "decision": "info",
        "reason": reason,
        "text": "",
        "state_changed": state_changed,
        "no_ack": True,
        "payload": c.project_payload(payload),
    }
    try:
        _send_to_daemon(request, wait_ack=False)
        return "daemon"
    except DaemonUnreachable as exc:
        request["unreachable"] = str(exc)
        try:
            c.append_jsonl(c.spool_path(), request, c.SPOOL_MAX_BYTES, rotate=False)
        except Exception as sexc:
            _log("error", "could not spool event", error=repr(sexc))
        return "spool"


def _emit(obj: dict) -> None:
    """Write hook output to stdout in one unbuffered write."""
    import json

    data = (json.dumps(obj) + "\n").encode("utf-8")
    while data:
        n = os.write(1, data)
        data = data[n:]


def _read_cfg() -> dict:
    try:
        return _common.read_config_raw()
    except Exception as exc:
        _log("error", "config unreadable, using defaults", error="%s: %s" % (type(exc).__name__, exc))
        return {}


def handle_user_prompt(payload: dict) -> dict:
    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    if session_id and not claim("prompt", session_id, _turn_key(payload, payload.get("prompt")), _turn_ttl(payload)):
        return {"decision": "none", "reason": "duplicate: another AfterWord hook already handled this prompt"}
    cfg = _read_cfg()
    state = _common.load_state(session_id)
    notes = []
    changed = False
    command = _voice_command(payload.get("prompt"))
    if command is not None:
        note, changed = apply_voice_command(session_id, command, cfg, state, payload.get("cwd"))
        notes.append(note)
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    if profile:
        notes.append(say_instruction(cfg, profile))
    if notes:
        _emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": " ".join(notes)}})
    if command is not None:
        shown = command if len(command) <= 64 else command[:64] + "..."
        reason = "/voice %s -> %s" % (shown or "(status)", profile or "off")
    elif profile:
        reason = "added <say> instruction (%s)" % profile
    else:
        reason = "voice off: nothing added"
    if session_id and _alert_settings(cfg)["enabled"]:
        # Spoken alerts: the turn starts now ("done" at Stop); a permission
        # still pending from the last turn was answered.
        pid = payload.get("prompt_id")
        now = time.time()

        def start_turn(st):
            st["turn_started"] = now
            st["turn"] = pid if isinstance(pid, str) else None
            st.pop("pending_permissions", None)
            st.pop("pending_permission", None)

        _update_state(session_id, start_turn)
    delivered = _notify("UserPromptSubmit", payload, profile, reason, changed)
    return {"decision": "inject" if notes else "none", "reason": reason, "profile": profile,
            "delivered": delivered, "context_chars": sum(len(n) for n in notes)}


def handle_session_start(payload: dict) -> dict:
    import tags

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    source = payload.get("source")
    env = os.environ.get("AFTERWORD_VOICE")
    state = _common.load_state(session_id)
    changed = False
    reason = "no AFTERWORD_VOICE"
    if env is not None and session_id:
        value = env.strip()
        profile = None if value.lower() in ("", "off", "none", "0") else value
        has_state = _common.state_file(session_id).is_file()
        if profile is not None and not tags.PROFILE_NAME_RE.match(profile):
            reason = "ignored invalid AFTERWORD_VOICE=%r" % value[:40]
        elif source in ("startup", "fork") or not has_state:
            state["profile"] = profile
            if profile:
                state["last_profile"] = profile
            state["source"] = "env"
            if isinstance(payload.get("cwd"), str):
                state["cwd"] = payload["cwd"]
            _common.update_state(session_id, _merge_keys(state, ("profile", "last_profile", "source", "cwd")))
            _note_voice(session_id, profile, payload.get("cwd"))
            changed = True
            reason = "voice seeded from AFTERWORD_VOICE=%s" % (profile or "off")
        else:
            reason = "kept the session's voice on %s" % source
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    delivered = _notify("SessionStart", payload, profile, reason, changed)
    return {"decision": "none", "reason": reason, "profile": profile, "delivered": delivered}


# --------------------------------------------------------------------------- #
# Spoken alerts: PermissionRequest, Notification, and "done" at Stop          #
# --------------------------------------------------------------------------- #


def _alert_settings(cfg: dict) -> dict:
    try:
        import alerting

        return alerting.settings(cfg)
    except Exception as exc:
        _log("error", "alert settings unreadable", error=repr(exc))
        return {"enabled": False, "events": (), "permission_when": "away", "done_after_s": 60.0, "profile": ""}


def _update_state(session_id: str, change) -> None:
    """Read the session state fresh, let `change(state)` edit it, save it
    unless `change` returns False (common.update_state: under the state
    lock, so a GUI mute or voice change is not undone). Never raises."""
    if not session_id:
        return
    try:
        _common.update_state(session_id, change)
    except Exception as exc:
        _log("error", "could not save session state", error=repr(exc))


def _merge_keys(state: dict, keys) -> object:
    """A change for _update_state that copies `keys` of `state` (those it
    has) onto the fresh state."""
    def change(st):
        for k in keys:
            if k in state:
                st[k] = state[k]
    return change


def _alert_wanted(s: dict, alert: dict) -> bool:
    """Whether the hook's own reading of [alerts] would speak this alert (the
    daemon decides with its own; this is for the `say` fallback)."""
    if not s.get("enabled") or alert.get("event") not in s.get("events", ()):
        return False
    if alert.get("event") == "permission":
        if alert.get("trigger") == "only":  # Codex: no Notification can follow
            return True
        want = "request" if s.get("permission_when") == "now" else "notification"
        return alert.get("trigger") == want
    return True


def _alert_say(s: dict, cfg: dict, state: dict, event: str, cwd) -> str:
    """Speak the fixed phrase for `event` with `say` (daemon down). Returns
    the text."""
    import alerting
    import scriptsplit

    names = _profile_names(cfg)
    own = state.get("profile") if isinstance(state.get("profile"), str) else None
    name = s.get("profile") if s.get("profile") in names else (own or (names[0] if names else None))
    table = _profile_table(cfg, name)
    primary = _lang_code(table.get("lang"))
    lang = "zh" if primary == "zh" else "en"
    text = alerting.fallback_phrase(event, lang, alerting.project_name(cwd))
    say_voice = table.get("say_voice") if isinstance(table.get("say_voice"), str) else None
    voice = scriptsplit.say_voice_for(lang, primary, None, None, say_voice) or scriptsplit.CJK_SAY_VOICES.get(lang)
    _say_fallback([(text, voice)])
    return text


def _alert_down(s: dict, cfg: dict, state: dict, request: dict, alert: dict, exc) -> dict:
    """The daemon is down: speak the fixed phrase if the alert would be
    spoken (and nothing is muted), and spool a record of it."""
    c = _common
    out = {"fallback": "none"}
    muted = c.muted_all() or state.get("muted") is True
    if _alert_wanted(s, alert) and not muted and c.fallback_mode(cfg) == "say":
        try:
            request["text"] = _alert_say(s, cfg, state, alert["event"], request.get("cwd"))
            request["decision"], request["reason"] = "speak", None
            out["fallback"] = "say"
        except Exception as fexc:
            request["decision"], request["reason"] = "speak", None
            out["fallback"] = "error"
            out["fallback_error"] = repr(fexc)
    elif muted and _alert_wanted(s, alert):
        request["reason"] = "alert: muted"
    spooled = dict(request)
    spooled["unreachable"] = str(exc)
    spooled["fallback"] = out["fallback"]
    spooled["fallback_error"] = out.get("fallback_error")
    try:
        c.append_jsonl(c.spool_path(), spooled, c.SPOOL_MAX_BYTES, rotate=False)
    except Exception as sexc:
        _log("error", "could not spool event", error=repr(sexc))
    return out


def _send_alert(event: str, payload: dict, alert: dict, s: dict, cfg: dict, state: dict) -> dict:
    """Hand an alert to the daemon, which decides (its [alerts] settings,
    mute) and picks the phrase. Daemon down: the `say` fallback."""
    request = {
        "v": 1,
        "type": "hook",
        "event": event,
        "t_hook": _T0,
        "t_sent": time.time(),
        "session_id": payload.get("session_id") if isinstance(payload.get("session_id"), str) else "",
        "cwd": payload.get("cwd"),
        "profile": state.get("profile") if isinstance(state.get("profile"), str) else None,
        "decision": "info",
        "reason": "alert: %s" % alert.get("event"),
        "text": "",
        "alert": alert,
        "payload": _common.project_payload(payload),
    }
    outcome = {"decision": "alert", "reason": request["reason"], "alert": alert}
    try:
        ack = _send_to_daemon(request)
        outcome["delivered"] = "daemon"
        outcome["ack"] = ack
        if not ack.get("ok"):
            _log("error", "daemon did not acknowledge", ack=ack)
    except DaemonUnreachable as exc:
        outcome["delivered"] = "spool"
        outcome.update(_alert_down(s, cfg, state, request, alert, exc))
    return outcome


def handle_permission_request(payload: dict) -> dict:
    """Store the pending tool in the session state (for the Notification
    that follows if the prompt waits) and hand the alert to the daemon.
    Prints nothing: the permission decision is never touched."""
    import json

    import alerting

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    cfg = _read_cfg()
    s = _alert_settings(cfg)
    state = _common.load_state(session_id)
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    if not s["enabled"]:
        return {"decision": "none", "reason": "alerts off",
                "delivered": _notify("PermissionRequest", payload, profile, "alerts off", False)}
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    try:
        key = json.dumps([tool, tool_input], sort_keys=True, default=str)[:4000]
    except (TypeError, ValueError):
        key = repr(tool)
    if session_id and not claim("alert", session_id, "perm\0" + key, CLAIM_TTL_ALERT_S):
        return {"decision": "none", "reason": "duplicate: another AfterWord hook already handled this request"}
    action = alerting.describe_action(tool, tool_input)
    label = alerting.tool_label(tool)
    now = time.time()
    if _HOST == "codex":
        # Codex has no Notification event, so no "still waiting" alert can
        # follow: "only" is spoken now whatever permission_when says.
        alert = {"event": "permission", "trigger": "only", "action": action, "tool": label}
        return _send_alert("PermissionRequest", payload, alert, s, cfg, state)

    def remember(st):
        alerting.add_pending(st, tool, label, action, now)

    _update_state(session_id, remember)
    alert = {"event": "permission", "trigger": "request", "action": action, "tool": label}
    return _send_alert("PermissionRequest", payload, alert, s, cfg, state)


def handle_notification(payload: dict) -> dict:
    import alerting

    session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
    ntype = payload.get("notification_type")
    ntype = ntype[:64] if isinstance(ntype, str) else ""
    event = alerting.NOTIFICATION_EVENTS.get(ntype)
    cfg = _read_cfg()
    s = _alert_settings(cfg)
    state = _common.load_state(session_id)
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    if not s["enabled"] or event is None:
        reason = "alerts off" if not s["enabled"] else "notification %s: not an alert" % (ntype or "(no type)")
        return {"decision": "none", "reason": reason,
                "delivered": _notify("Notification", payload, profile, reason, False)}
    message = payload.get("message") if isinstance(payload.get("message"), str) else ""
    if session_id and not claim("alert", session_id, "note\0%s\0%s" % (ntype, message), CLAIM_TTL_ALERT_S):
        return {"decision": "none", "reason": "duplicate: another AfterWord hook already handled this notification"}
    alert = {"event": event, "trigger": "notification", "notification_type": ntype, "message": message[:200]}
    if event == "permission" and session_id:
        # Only a pending request this prompt is surely about names the action
        # (alerting.take_pending); otherwise the phrase goes without it.
        now = time.time()
        taken = []

        def take(st):
            got = alerting.take_pending(st, message, now)
            if got is None:
                return False
            taken.append(got)

        _update_state(session_id, take)
        for k in ("action", "tool"):
            if taken and taken[0].get(k):
                alert[k] = taken[0][k][:alerting.ACTION_MAX]
    return _send_alert("Notification", payload, alert, s, cfg, state)


def _done_alert(s: dict, state: dict, decision: str, early_dropped: int):
    """Stop: the "done" alert for a turn that took at least done_after_s
    and spoke nothing, or None. The turn start is UserPromptSubmit's."""
    started = state.get("turn_started")
    if not s.get("enabled") or isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    turn_s = time.time() - started
    if decision == "speak" or early_dropped or not 0 <= turn_s <= 7 * 86400 or turn_s < s["done_after_s"]:
        return None
    alert = {"event": "done", "trigger": "stop", "turn_s": round(turn_s, 1)}
    if turn_s >= 60:  # a phrase with {minutes} only for a turn of a minute or more
        alert["minutes"] = int(round(turn_s / 60))
    return alert


_EVENTS = {
    "stop": "Stop",
    "Stop": "Stop",
    "permission-request": "PermissionRequest",
    "PermissionRequest": "PermissionRequest",
    "notification": "Notification",
    "Notification": "Notification",
    "user-prompt-submit": "UserPromptSubmit",
    "UserPromptSubmit": "UserPromptSubmit",
    "session-start": "SessionStart",
    "SessionStart": "SessionStart",
    "message-display": "MessageDisplay",
    "MessageDisplay": "MessageDisplay",
}


def run(argv: list) -> dict:
    import json

    raw = _read_stdin()
    try:
        payload = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
    except ValueError as exc:
        _log("error", "payload is not JSON", error=str(exc), head=raw[:200].decode("utf-8", "replace"))
        return {"decision": "error", "reason": "bad payload"}
    if not isinstance(payload, dict):
        _log("error", "payload is not an object", kind=type(payload).__name__)
        return {"decision": "error", "reason": "bad payload"}

    global _HOST
    _HOST = argv[2] if len(argv) > 2 and argv[2] in ("codex",) else "claude"
    event = argv[1] if len(argv) > 1 else payload.get("hook_event_name")
    event = _EVENTS.get(str(event), str(event))
    if event == "Stop":
        result = handle_stop(payload)
    elif event == "UserPromptSubmit":
        result = handle_user_prompt(payload)
    elif event == "SessionStart":
        result = handle_session_start(payload)
    elif event == "MessageDisplay":
        result = handle_message_display(payload)
    elif event == "PermissionRequest":
        result = handle_permission_request(payload)
    elif event == "Notification":
        result = handle_notification(payload)
    else:
        result = {"decision": "ignored", "reason": "unsupported event %s" % event}
    elapsed_ms = round((time.time() - _T0) * 1000, 1)
    result["elapsed_ms"] = elapsed_ms
    if result.pop("quiet", False) and os.environ.get("AFTERWORD_HOOK_LOG_ALL") != "1":
        return result
    _log("info", "hook done", hook_event=event, **{k: v for k, v in result.items() if k != "ack"})
    return result


def main(argv=None) -> int:
    global _common
    argv = list(sys.argv if argv is None else argv)
    try:
        if os.environ.get("AFTERWORD_DISABLE") == "1":
            return 0
        _arm_deadline()
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        import common

        _common = common
        run(argv)
    except BaseException as exc:  # includes SystemExit and KeyboardInterrupt
        try:
            import traceback

            _log("error", "hook crashed", error=repr(exc), tb=traceback.format_exc()[-4000:])
        except BaseException:
            pass
    finally:
        _disarm_deadline()
    return 0


if __name__ == "__main__":
    try:
        main()
    finally:
        # os._exit skips interpreter teardown, which could otherwise fail on
        # a closed stdout/stderr and turn our exit code into 120.
        os._exit(0)
