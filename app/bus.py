"""In-process pub/sub feeding the Server-Sent-Events stream."""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any


class Bus:
    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[tuple[str, Any]]] = set()

    def publish(self, event: str, data: Any) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:  # a stalled client must never block the app; it will resync on reconnect
                self._subs.discard(q)

    async def stream(self, keepalive: float = 20.0) -> AsyncIterator[str]:
        q: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=200)
        self._subs.add(q)
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event, data = await asyncio.wait_for(q.get(), timeout=keepalive)
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            self._subs.discard(q)


bus = Bus()
