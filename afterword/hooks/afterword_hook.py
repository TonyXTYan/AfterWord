#!/usr/bin/env python3
"""AfterWord's single Claude Code hook entry point.

    afterword_hook.py <event>          # event: stop (others reserved)

Reads the hook payload (JSON) from stdin. Contract with Claude Code:

* never fails: every error is caught and logged, the exit code is always 0;
* never blocks: hard deadline via an interval timer, short socket timeouts,
  the `say` fallback is spawned detached and not waited for;
* never prints to stdout for Stop (stdout is reserved for hook output).

Stdlib-only, Python 3.9+.
"""

from __future__ import annotations

import os
import sys
import time

_T0 = time.time()
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


def _last_message_from_transcript(path: object) -> str:
    """Fallback when the payload lacks `last_assistant_message`: the text of
    the last assistant message in the JSONL transcript."""
    import json

    if not isinstance(path, str) or not path:
        return ""
    import stat

    try:
        # O_NONBLOCK: opening a FIFO with no writer would otherwise block.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                return ""
            fh.seek(max(0, st.st_size - TRANSCRIPT_TAIL_BYTES))
            data = fh.read(TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return ""
    lines = data.decode("utf-8", "replace").splitlines()
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
    return "\n".join(texts)


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


def _send_to_daemon(message: dict) -> dict:
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


def _say_fallback(text: str) -> None:
    """Speak with macOS `say`, detached. Text goes over stdin so a leading
    '-' can never be read as an option; `say`'s embedded [[commands]] are
    stripped first."""
    import subprocess
    import tags

    proc = subprocess.Popen(
        _common.say_argv(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    try:
        proc.stdin.write(tags.strip_speech_commands(text).encode("utf-8", "replace"))  # type: ignore[union-attr]
    finally:
        proc.stdin.close()  # type: ignore[union-attr]
    proc.returncode = 0  # detached on purpose; silences the "still running" ResourceWarning


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
    state_changed = False
    if voice is not None and voice.profile != state.get("profile"):
        state["profile"] = voice.profile
        state["cwd"] = payload.get("cwd")
        state["source"] = "voice-tag"
        state_changed = True
    if state_changed and session_id:
        try:
            c.save_state(session_id, state)
        except Exception as exc:
            _log("error", "could not save session state", error=repr(exc))

    profile = state.get("profile")
    say = parsed.say
    text = ""
    if not profile:
        decision, reason = "skip", "session off"
    elif say is None:
        decision, reason = "skip", "no <say> tag"
    else:
        text = tags.truncate(say.text, c.profile_max_chars(cfg, profile))
        decision, reason = "speak", None

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
        "attrs": dict(say.attrs) if (say is not None and decision == "speak") else {},
        "parsed": parsed.to_dict(),
        "message_source": source,
        "stop_hook_active": bool(payload.get("stop_hook_active")),
        "state_changed": state_changed,
        "config_error": cfg_error,
        "payload": c.cap_strings(payload),
    }

    outcome = {"decision": decision, "reason": reason, "text": text, "profile": profile}
    try:
        ack = _send_to_daemon(request)
        outcome["delivered"] = "daemon"
        outcome["ack"] = ack
        if not ack.get("ok"):
            _log("error", "daemon did not acknowledge", ack=ack)
    except DaemonUnreachable as exc:
        outcome["delivered"] = "spool"
        if decision == "speak" and c.fallback_mode(cfg) == "say":
            try:
                _say_fallback(text)
                outcome["fallback"] = "say"
            except Exception as fexc:
                outcome["fallback"] = "error"
                outcome["fallback_error"] = repr(fexc)
        else:
            outcome["fallback"] = "none"
        spooled = dict(request)
        spooled["payload"] = c.cap_strings(payload, limit=8 * 1024)
        spooled["unreachable"] = str(exc)
        spooled["fallback"] = outcome["fallback"]
        spooled["fallback_error"] = outcome.get("fallback_error")
        try:
            c.append_jsonl(c.spool_path(), spooled, c.SPOOL_MAX_BYTES, rotate=False)
        except Exception as sexc:
            _log("error", "could not spool event", error=repr(sexc))
    return outcome


_EVENTS = {
    "stop": "Stop",
    "Stop": "Stop",
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

    event = argv[1] if len(argv) > 1 else payload.get("hook_event_name")
    event = _EVENTS.get(str(event), str(event))
    if event == "Stop":
        result = handle_stop(payload)
    else:
        # UserPromptSubmit / SessionStart arrive in a later build step.
        result = {"decision": "ignored", "reason": "unsupported event %s" % event}
    elapsed_ms = round((time.time() - _T0) * 1000, 1)
    result["elapsed_ms"] = elapsed_ms
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
