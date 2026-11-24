"""Per-session state as the GUI sees it.

The hook owns most writes (it must work while the daemon is down); the GUI
and the daemon write through `set_session`. Both use the same atomic
writer in afterword.hooks.common, so a file is never half-written.
"""

from __future__ import annotations

import json
import re
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
    state = common.load_state(session_id)
    if profile is not ...:
        if profile is not None:
            if not isinstance(profile, str) or (known_profiles is not None and profile not in known_profiles):
                listed = ", ".join(known_profiles or []) or "none configured"
                raise SessionError(f"unknown profile {profile!r} (profiles: {listed})")
        state["profile"] = profile
        if profile:
            state["last_profile"] = profile
        state["source"] = source
    if muted is not ...:
        if not isinstance(muted, bool):
            raise SessionError("muted must be true or false")
        state["muted"] = muted
    common.save_state(session_id, state)
    return common.load_state(session_id)
