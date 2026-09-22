"""Cover Art Archive: front-cover thumbnails for MusicBrainz release groups.

A separate service from MusicBrainz itself (musicbrainz.org's own 1 req/s limit doesn't apply here), but paced
the same polite way regardless, since we have no published limit to go by. Most release groups have no cover
art at all — a 404 is the normal case, not a problem to log or retry.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from . import __version__, config

BASE = "https://coverartarchive.org/release-group/"
MIN_GAP = 1.0


class Client:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}, timeout=15, follow_redirects=True)
        return self._http

    async def front_thumb(self, release_group_mbid: str) -> str | None:
        """A small thumbnail of the release group's front cover, or None if it has none — the common case."""
        async with self._lock:
            wait = self._last + MIN_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._client().get(BASE + release_group_mbid)
            finally:
                self._last = time.monotonic()
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        images: list[dict[str, Any]] = resp.json().get("images") or []
        front = next((i for i in images if i.get("front")), images[0] if images else None)
        if not front:
            return None
        thumbs = front.get("thumbnails") or {}
        return thumbs.get("250") or thumbs.get("small")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None


client = Client()
