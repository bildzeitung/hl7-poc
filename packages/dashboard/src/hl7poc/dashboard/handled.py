"""In-memory store for POST /api/handled events: a last-N ring buffer plus
running totals by outcome.

State lives only in this process's memory -- restarting the dashboard resets
it, which is acceptable for the by-hand demo this dashboard serves.
"""

from __future__ import annotations

from collections import deque
from typing import Any


class HandledStore:
    def __init__(self, max_events: int) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._completed = 0
        self._dead_lettered = 0

    def record(self, event: dict[str, Any]) -> None:
        self._events.append(event)
        if event.get("outcome") == "dead_lettered":
            self._dead_lettered += 1
        else:
            self._completed += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "completed": self._completed,
            "dead_lettered": self._dead_lettered,
            "total": self._completed + self._dead_lettered,
            "events": list(self._events),
        }
