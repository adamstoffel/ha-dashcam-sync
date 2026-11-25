"""In-process event bus feeding the WebSocket and the engine's observers."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    """Fan-out of small JSON events. Slow subscribers lose the oldest events."""

    def __init__(self, maxsize: int = 500) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._maxsize)
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, event_type: str, **data: Any) -> dict[str, Any]:
        event = {"type": event_type, "ts": time.time(), **data}
        for queue in list(self._subs):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        return event
