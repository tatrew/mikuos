"""In-process async event bus. Thread-safe publish (hotkey / observer threads)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("mikuos.events")

Handler = Callable[["Event"], Awaitable[None] | None]


@dataclass
class Event:
    type: str
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {"type": self.type, "data": self.data, "ts": self.ts}


class EventBus:
    def __init__(self, history: int = 200):
        self._subs: dict[str, list[Handler]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.history: deque[Event] = deque(maxlen=history)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """event_type '*' receives everything."""
        self._subs.setdefault(event_type, []).append(handler)

    async def publish(self, event_type: str, data: dict | None = None) -> Event:
        ev = Event(event_type, data or {})
        self.history.append(ev)
        for h in self._subs.get(event_type, []) + self._subs.get("*", []):
            try:
                r = h(ev)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception("event handler failed for %s", event_type)
        return ev

    def publish_threadsafe(self, event_type: str, data: dict | None = None) -> None:
        if not self._loop:
            raise RuntimeError("EventBus loop not bound")
        asyncio.run_coroutine_threadsafe(self.publish(event_type, data), self._loop)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return [e.as_dict() for e in list(self.history)[-limit:]]
