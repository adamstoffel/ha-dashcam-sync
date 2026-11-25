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
        self._seq = 0

    @property
    def seq(self) -> int:
        """Number of the newest event; a status read taken now reflects every event up to it."""
        return self._seq

    def mark(self) -> int:
        """Take a number for state about to be sampled outside an event.

        Every sample gets its own number, so two samples of changing state (a status
        read and a hub snapshot, say) never share one and can always be ordered.
        """
        self._seq += 1
        return self._seq

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._maxsize)
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, event_type: str, *, as_of: int | None = None, **data: Any) -> dict[str, Any]:
        """Send an event to every subscriber.

        Every event carries ``rev``: its number, or for state sampled earlier (``as_of``)
        the number current when sampling began. A client that also reads the status keeps
        a pushed state only when its ``rev`` is higher than the status answer's.
        """
        self._seq += 1
        rev = self._seq if as_of is None else as_of
        event = {"type": event_type, "ts": time.time(), "rev": rev, **data}
        for queue in list(self._subs):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)
        return event
