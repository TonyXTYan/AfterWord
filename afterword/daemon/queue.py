"""One global playback queue.

A newer item from a session supersedes that session's *queued* (not yet
playing) item, taking over its place in line. When the queue is full and
nothing can be superseded, the new item is refused ("daemon busy").
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

    def summary(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "profile": self.profile,
            "text": self.text,
            "enqueued_at": self.enqueued_at,
        }


@dataclass
class PutResult:
    accepted: bool
    superseded: QueueItem | None = None
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
        for i, old in enumerate(self._items):
            if old.session_id == item.session_id:
                self._items[i] = item
                self._wakeup.set()
                return PutResult(True, superseded=old, position=i)
        if len(self._items) >= self.max_size:
            return PutResult(False)
        self._items.append(item)
        self._wakeup.set()
        return PutResult(True, position=len(self._items) - 1)

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
