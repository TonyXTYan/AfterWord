"""Spoken alerts, the hook side: [alerts] settings as the hook reads them,
the short description of a pending tool, and the fixed phrases the `say`
fallback speaks while the daemon is down.

Stdlib-only and Python 3.9+ (see tags.py). The daemon validates [alerts]
itself (config.AlertSettings) and picks the real phrases (daemon/alerts.py);
this module only has to be good enough for the hook's decisions.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

EVENTS = ("permission", "input", "idle", "done")
DEFAULT_EVENTS = ("permission", "input", "done")
PERMISSION_WHEN = ("away", "now")
DEFAULT_DONE_AFTER_S = 60.0
PENDING_TTL_S = 600.0  # a pending permission older than this is not spoken
PENDING_MAX = 8  # pending permissions kept per session (newest last)
PENDING_PARALLEL_S = 3.0  # same-tool requests this close together: ambiguous
PENDING_ANSWERED_S = 2.0  # new assistant text: requests older than this were answered
ACTION_MAX = 100
ACTION_WORDS = 8

# Notification.notification_type -> alert event.
NOTIFICATION_EVENTS = {
    "permission_prompt": "permission",
    "elicitation_dialog": "input",
    "elicitation_url_dialog": "input",
    "agent_needs_input": "input",
    "idle_prompt": "idle",
}

# The `say` fallback's fixed phrases (the daemon has the real pools).
FALLBACK = {
    "en": {
        "permission": "{project} needs your OK.",
        "input": "{project} needs your input.",
        "idle": "{project} is waiting for you.",
        "done": "{project} is done.",
    },
    "zh": {
        "permission": "{project} 需要你批准。",
        "input": "{project} 需要你回答。",
        "idle": "{project} 在等你。",
        "done": "{project} 完成了。",
    },
}


def settings(cfg: Any) -> Dict[str, Any]:
    """[alerts] from the raw config, every value checked (a bad one is the
    default). `events` is an array of names (the mini TOML parser on Python
    < 3.11 reads arrays of scalars); a missing or broken one means the
    default events."""
    raw = cfg.get("alerts") if isinstance(cfg, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    events = raw.get("events")
    if isinstance(events, list) and all(isinstance(e, str) for e in events):
        events = tuple(e for e in events if e in EVENTS)
    else:
        events = DEFAULT_EVENTS
    when = raw.get("permission_when")
    after = raw.get("done_after_s")
    if isinstance(after, bool) or not isinstance(after, (int, float)) or not 0 <= after <= 86400:
        after = DEFAULT_DONE_AFTER_S
    profile = raw.get("profile")
    return {
        "enabled": raw.get("enabled") is True,
        "events": events,
        "permission_when": when if when in PERMISSION_WHEN else "away",
        "done_after_s": float(after),
        "profile": profile if isinstance(profile, str) else "",
    }


def project_name(cwd: object) -> str:
    """The folder name of `cwd`, for {project}; "Claude" without one."""
    if isinstance(cwd, str) and cwd.strip():
        name = os.path.basename(cwd.rstrip("/\\")) or ""
        name = re.sub(r"\s+", " ", name).strip()[:60]
        if name:
            return name
    return "Claude"


def tool_label(tool_name: object) -> str:
    """A speakable tool name: mcp__github__create_issue -> github create issue."""
    if not isinstance(tool_name, str) or not tool_name.strip():
        return ""
    name = tool_name.strip()[:80]
    if name.startswith("mcp__"):
        name = name[5:]
    return re.sub(r"[_\s]+", " ", name).strip()


def _first_words(text: str, words: int = ACTION_WORDS) -> str:
    parts = text.split()
    out = " ".join(parts[:words])
    if len(parts) > words:
        out += " …"
    return out


_VERBS = {
    "bash": ("run", "command"), "edit": ("edit", "file_path"), "multiedit": ("edit", "file_path"),
    "write": ("write", "file_path"), "read": ("read", "file_path"), "notebookedit": ("edit", "notebook_path"),
    "webfetch": ("fetch", "url"), "websearch": ("search for", "query"), "glob": ("search for", "pattern"),
    "grep": ("search for", "pattern"),
}


def describe_action(tool_name: object, tool_input: object) -> str:
    """A short spoken description of a pending tool call: the tool's own
    `description` (Bash has one), else the tool name and the first words of
    its input. At most ACTION_MAX characters, one line. Codex's description
    is its approval question ("Do you approve running `touch x` outside the
    sandbox?"), which would read oddly inside a phrase; the command is used
    instead."""
    inp = tool_input if isinstance(tool_input, dict) else {}
    desc = inp.get("description")
    if isinstance(desc, str) and re.match(r"\s*do you approve\b", desc, re.I):
        desc = None
    if isinstance(desc, str) and desc.strip():
        text = desc
    else:
        label = tool_label(tool_name)
        verb, key = _VERBS.get(label.replace(" ", "").lower(), ("use " + label if label else "", ""))
        value = inp.get(key) if key else None
        if not isinstance(value, str) or not value.strip():
            value = next((v for v in inp.values() if isinstance(v, str) and v.strip()), "")
        if key in ("file_path", "notebook_path") and value:
            value = os.path.basename(value.rstrip("/")) or value
        elif key == "url" and value:
            m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)", value.strip(), re.I)
            value = m.group(1) if m else value
        text = (verb + " " + _first_words(value)).strip() if value else verb
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > ACTION_MAX:
        text = text[:ACTION_MAX - 1].rstrip() + "…"
    return text


def fallback_phrase(event: str, lang: Optional[str], project: str) -> str:
    table = FALLBACK.get(lang or "", FALLBACK["en"])
    return table.get(event, FALLBACK["en"]["permission"]).replace("{project}", project)


# --------------------------------------------------------------------------- #
# Pending permissions (state["pending_permissions"], newest last)              #
# --------------------------------------------------------------------------- #
#
# PermissionRequest has no request id and the permission_prompt Notification
# none either, so the Notification can only guess which stored request it is
# about. These rules keep it from naming the wrong one: the Notification's
# message names the tool ("Claude needs your permission to use Bash") and must
# match; a sandboxed network request (no PermissionRequest of its own) names
# no entry; same-tool requests made together are ambiguous; and new assistant
# text (MessageDisplay) means the earlier requests were answered. When in
# doubt the alert is spoken without {action}.

PENDING_KEY = "pending_permissions"
_USE_RE = re.compile(r"permission to use (.+?)[.!]?\s*$", re.I | re.S)


def pending_entries(state: Any, now: float) -> list:
    """The session's live pending permissions, oldest first."""
    raw = state.get(PENDING_KEY) if isinstance(state, dict) else None
    out = []
    for e in raw if isinstance(raw, list) else []:
        t = e.get("t") if isinstance(e, dict) else None
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= now - t <= PENDING_TTL_S:
            continue
        if all(isinstance(e.get(k), str) for k in ("tool_name", "tool", "action")):
            out.append({"tool_name": e["tool_name"][:200], "tool": e["tool"][:ACTION_MAX],
                        "action": e["action"][:ACTION_MAX], "t": float(t)})
    out.sort(key=lambda e: e["t"])
    return out[-PENDING_MAX:]


def add_pending(state: Dict[str, Any], tool_name: object, tool: str, action: str, now: float) -> None:
    entries = pending_entries(state, now)
    entries.append({"tool_name": tool_name[:200] if isinstance(tool_name, str) else "",
                    "tool": tool, "action": action, "t": now})
    state[PENDING_KEY] = entries[-PENDING_MAX:]
    state.pop("pending_permission", None)  # the single entry of an earlier build


def drop_answered(state: Dict[str, Any], now: float) -> bool:
    """New assistant text: keep only requests made in the last
    PENDING_ANSWERED_S (the hook for the text before a tool call can run a
    little after that call's PermissionRequest). True if anything changed."""
    raw = state.get(PENDING_KEY)
    if raw is None and "pending_permission" not in state:
        return False
    keep = [e for e in pending_entries(state, now) if now - e["t"] < PENDING_ANSWERED_S]
    state.pop("pending_permission", None)
    if keep:
        state[PENDING_KEY] = keep
    else:
        state.pop(PENDING_KEY, None)
    return keep != raw


def _norm(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", text.lower())


def _names_tool(named: str, entry: Dict[str, Any]) -> bool:
    n = _norm(named)
    if not n:
        return False
    if n in (_norm(entry["tool_name"]), _norm(entry["tool"])):
        return True
    raw = entry["tool_name"]
    if raw.startswith("mcp__"):  # "github - create_issue (MCP)" and the like
        parts = [_norm(p) for p in raw[5:].split("__") if _norm(p)]
        return bool(parts) and all(p in n for p in parts)
    return False


def take_pending(state: Dict[str, Any], message: object, now: float) -> Optional[Dict[str, Any]]:
    """The pending permission a permission_prompt Notification is about,
    removed from `state`; None when nothing matches for sure (the entries
    are then left as they are)."""
    entries = pending_entries(state, now)
    text = message if isinstance(message, str) else ""
    if not entries or "network" in text.lower():
        return None
    m = _USE_RE.search(text)
    candidates = [e for e in entries if _names_tool(m.group(1), e)] if m else entries
    if not candidates:
        return None
    newest = candidates[-1]
    if any(e is not newest and newest["t"] - e["t"] < PENDING_PARALLEL_S for e in candidates):
        return None  # made together (parallel calls): which one is on screen?
    entries.remove(newest)
    if entries:
        state[PENDING_KEY] = entries
    else:
        state.pop(PENDING_KEY, None)
    state.pop("pending_permission", None)
    return newest
