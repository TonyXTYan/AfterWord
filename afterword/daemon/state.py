"""Read-side view of per-session state.

The hook owns writes (it must work while the daemon is down); the daemon
reads the same files from ~/.afterword/state/.
"""

from __future__ import annotations

import json
from typing import Any

from afterword.hooks import common


def get_session(session_id: str) -> dict[str, Any]:
    return common.load_state(session_id)


def list_sessions(limit: int = 200) -> list[dict[str, Any]]:
    d = common.state_dir()
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    out = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append(data)
    return out
