"""Per-session state as the GUI sees it.

The hook owns most writes (it must work while the daemon is down); the GUI
and the daemon write through `set_session`. Both use the same atomic
writer in afterword.hooks.common, so a file is never half-written.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from afterword.hooks import common

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}\Z")


class SessionError(ValueError):
    pass


def get_session(session_id: str) -> dict[str, Any]:
    return common.load_state(session_id)


def read_states(limit: int = 500) -> dict[str, dict[str, Any]]:
    d = common.state_dir()
    if not d.is_dir():
        return {}
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        try:
            data = json.loads(common.read_regular_file(f, common.STATE_MAX_BYTES).decode("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("session_id"), str):
            out[data["session_id"]] = data
    return out


def session_view(session_id: str, state: dict[str, Any] | None, activity: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    activity = activity or {}
    profile = state.get("profile") if isinstance(state.get("profile"), str) else None
    updated = state.get("updated") if isinstance(state.get("updated"), (int, float)) else None
    last_seen = max(x for x in (activity.get("last_seen"), updated, 0.0) if x is not None) or None
    return {
        "session_id": session_id,
        "cwd": state.get("cwd") if isinstance(state.get("cwd"), str) else activity.get("cwd"),
        "profile": profile,
        "muted": bool(state.get("muted")),
        "source": state.get("source") if isinstance(state.get("source"), str) else None,
        "updated": updated,
        "last_seen": last_seen,
        "events": int(activity.get("events") or 0),
        "spoken": int(activity.get("spoken") or 0),
        "last_event": activity.get("last_event"),
    }


def list_sessions(activity: dict[str, dict[str, Any]], limit: int = 100) -> list[dict[str, Any]]:
    states = read_states()
    ids = set(states) | set(activity)
    views = [session_view(sid, states.get(sid), activity.get(sid)) for sid in ids]
    views.sort(key=lambda v: v["last_seen"] or 0, reverse=True)
    return views[: max(1, min(limit, 1000))]


def set_session(
    session_id: str,
    *,
    profile: Any = ...,
    muted: Any = ...,
    source: str = "gui",
    known_profiles: list[str] | None = None,
) -> dict[str, Any]:
    """Change a session's voice and/or mute. `...` means "leave as is"."""
    if not isinstance(session_id, str) or not SESSION_ID_RE.match(session_id):
        raise SessionError("bad session id")
    if profile is not ... and profile is not None:
        if not isinstance(profile, str) or (known_profiles is not None and profile not in known_profiles):
            listed = ", ".join(known_profiles or []) or "none configured"
            raise SessionError(f"unknown profile {profile!r} (profiles: {listed})")
    if muted is not ... and not isinstance(muted, bool):
        raise SessionError("muted must be true or false")

    def change(state: dict[str, Any]) -> None:
        if profile is not ...:
            state["profile"] = profile
            if profile:
                state["last_profile"] = profile
            state["source"] = source
        if muted is not ...:
            state["muted"] = muted

    # Under the state lock: a hook's own read-modify-write of this file
    # (turn start, pending permission) cannot undo this change.
    common.update_state(session_id, change)
    return common.load_state(session_id)


# A state file that holds nothing the user chose (no voice, never had one,
# not muted) is only the hooks' turn bookkeeping (spoken alerts write one per
# session): removed once it has not changed for this long.
PRUNE_AFTER_S = 7 * 86400
PRUNE_EVERY_S = 3600
_last_prune = 0.0


def _user_set(data: Any) -> bool:
    return not isinstance(data, dict) or bool(data.get("profile")) or bool(data.get("last_profile")) \
        or data.get("muted") is True


def prune_states(now: float | None = None, force: bool = False) -> int:
    """Delete bookkeeping-only state files untouched for PRUNE_AFTER_S. At
    most once per PRUNE_EVERY_S unless `force`. Returns how many went."""
    global _last_prune
    now = time.time() if now is None else now
    if not force and now - _last_prune < PRUNE_EVERY_S:
        return 0
    _last_prune = now
    d = common.state_dir()
    removed = 0
    try:
        files = list(d.glob("*.json"))
    except OSError:
        return 0
    for f in files:
        try:
            if now - f.lstat().st_mtime < PRUNE_AFTER_S:
                continue
            with common.state_lock():
                if now - f.lstat().st_mtime < PRUNE_AFTER_S:
                    continue  # written meanwhile
                data = json.loads(common.read_regular_file(f, common.STATE_MAX_BYTES).decode("utf-8"))
                if _user_set(data):
                    continue
                f.unlink()
                removed += 1
        except (OSError, ValueError):
            continue
    return removed
