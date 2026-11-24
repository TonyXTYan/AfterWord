"""One global playback queue.

Items carry the turn they belong to (Claude's prompt id, else their own
event id). A new item from a session:

- with the same turn as that session's waiting items is appended after
  them (several lines of one reply all play, in order);
- with another turn supersedes that session's waiting items from other
  turns: the first one's place in line goes to the new item, the rest are
  dropped.

Alerts (kind "alert", docs/design-v2.md C) live beside speech: they never
supersede speech and speech never supersedes them; a new alert supersedes
only that session's older waiting alerts, and otherwise goes to the end of
the line. (The worker drops an alert that waited more than 30 s.)

The playing item is never superseded. When the queue is full and nothing
was superseded, the new item is refused ("daemon busy").
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueueItem:
    event_id: int
    session_id: str
    text: str
    profile: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    cwd: str | None = None
    t_hook: float | None = None
    enqueued_at: float = field(default_factory=time.time)
    turn: str | None = None  # None: a turn of its own
    # The <say> blocks: [{"text", "attrs"}]. Empty means one block (text, attrs).
    segments: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "speech"  # "speech" | "alert"

    def summary(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "profile": self.profile,
            "text": self.text,
            "turn": self.turn,
            "enqueued_at": self.enqueued_at,
        }

    def same_turn(self, other: QueueItem) -> bool:
        return self.turn is not None and self.turn == other.turn


@dataclass
class PutResult:
    accepted: bool
    superseded: list[QueueItem] = field(default_factory=list)
    position: int | None = None


class PlaybackQueue:
    def __init__(self, max_size: int = 16):
        self.max_size = max_size
        self._items: list[QueueItem] = []
        self._wakeup = asyncio.Event()
        self.now_playing: QueueItem | None = None

    def __len__(self) -> int:
        return len(self._items)

    def put(self, item: QueueItem) -> PutResult:
        alert = item.kind == "alert"
        mine = [i for i, old in enumerate(self._items)
                if old.session_id == item.session_id and (old.kind == "alert") == alert]
        older = mine if alert else [i for i in mine if not self._items[i].same_turn(item)]
        if older:
            first = older[0]
            superseded = [self._items[i] for i in older]
            self._items[first] = item
            for i in reversed(older[1:]):
                del self._items[i]
            self._wakeup.set()
            return PutResult(True, superseded=superseded, position=first)
        if len(self._items) >= self.max_size:
            return PutResult(False)
        # Speech: after this turn's waiting items. An alert: at the end.
        pos = mine[-1] + 1 if mine and not alert else len(self._items)
        self._items.insert(pos, item)
        self._wakeup.set()
        return PutResult(True, position=pos)

    def pop_nowait(self) -> QueueItem | None:
        if not self._items:
            self._wakeup.clear()
            return None
        item = self._items.pop(0)
        if not self._items:
            self._wakeup.clear()
        return item

    async def get(self) -> QueueItem:
        while True:
            item = self.pop_nowait()
            if item is not None:
                return item
            await self._wakeup.wait()

    def clear(self) -> list[QueueItem]:
        dropped, self._items = self._items, []
        self._wakeup.clear()
        return dropped

    def remove(self, event_id: int) -> QueueItem | None:
        for i, item in enumerate(self._items):
            if item.event_id == event_id:
                return self._items.pop(i)
        return None

    def snapshot(self) -> dict[str, Any]:
        return {
            "now_playing": self.now_playing.summary() if self.now_playing else None,
            "pending": [i.summary() for i in self._items],
            "max_size": self.max_size,
        }
