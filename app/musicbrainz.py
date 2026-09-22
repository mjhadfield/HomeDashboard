"""A polite MusicBrainz client. MusicBrainz allows ~1 request/second for anonymous clients and returns 503 when it
is busy (we saw this while testing), so every call goes through one lock + minimum spacing, and busy responses
back off and retry. All widgets share the single instance so their combined rate still respects the limit."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import __version__, config

BASE = "https://musicbrainz.org/ws/2/"
MIN_GAP = 1.1          # seconds between requests


class MusicBrainzBusy(RuntimeError):
    pass


class Client:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})", "Accept": "application/json"}, timeout=30)
        return self._http

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        async with self._lock:
            for attempt in range(4):
                wait = self._last + MIN_GAP - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                resp = await self._client().get(BASE + path, params={**params, "fmt": "json"})
                self._last = time.monotonic()
                if resp.status_code in (429, 503):                       # busy: back off, then try again
                    await asyncio.sleep(min(30.0, 2.0 ** (attempt + 1)))
                    continue
                resp.raise_for_status()
                return resp.json()
        raise MusicBrainzBusy("MusicBrainz is busy — will try again next sync")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None


client = Client()


async def resolve_artist_id(name: str) -> str | None:
    """Look an artist up by name. Only an exact (case-insensitive) name match with a high score is accepted —
    a wrong artist is worse than none."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    data = await client.get("artist", query=f'artist:"{escaped}"', limit=5)
    for a in data.get("artists", []):
        if a.get("name", "").casefold() == name.casefold() and int(a.get("score", 0)) >= 90:
            return a["id"]
    return None
