"""Last.fm: similar artists, an artist's most-played tracks, and a tag's top tracks — the parts of discovery Spotify no
longer offers personal apps. Last.fm asks for no more than about five requests a second; answers are cached for weeks,
since none of this changes quickly."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .. import store
from .base import Field, Service, ServiceError

API = "https://ws.audioscrobbler.com/2.0/"
MIN_GAP = 0.25
MONTH = 30 * 86400


class LastfmService(Service):
    async def test(self) -> str:
        data = await call("artist.getSimilar", artist="Gojira", limit=3)
        names = [a["name"] for a in (data.get("similarartists") or {}).get("artist", [])]
        return f"Working — e.g. like Gojira: {', '.join(names) or 'nothing returned'}"


service = LastfmService(
    id="lastfm", name="Last.fm", order=20,
    blurb="Similar artists and most-played tracks — powers Discover and the “new to you” ingredients.",
    link="https://www.last.fm/api/account/create",
    fields=(Field("api_key", "API key", secret=True), Field("username", "Username (optional)", required=False)),
)

_lock = asyncio.Lock()
_last = 0.0


async def call(method: str, **params: Any) -> dict[str, Any]:
    global _last
    if not service.enabled():
        raise ServiceError("Last.fm is switched off in Settings → Music page")
    cfg = service.cfg()
    if not cfg.get("api_key"):
        raise ServiceError("Last.fm needs an API key — Settings → Music page")
    async with _lock:
        wait = _last + MIN_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20) as http:
            r = await http.get(API, params={"method": method, "api_key": cfg["api_key"], "format": "json", "autocorrect": 1, **params})
        _last = time.monotonic()
    try:
        data = r.json()
    except ValueError:
        raise ServiceError(f"Last.fm answered {r.status_code}")
    if "error" in data:
        if data["error"] in (6, 7):                       # "artist/tag not found": an empty answer, not a fault
            return {}
        raise ServiceError(f"Last.fm: {data.get('message', data['error'])}")
    return data


async def similar(artist: str, limit: int = 30) -> list[dict[str, Any]]:
    """[{name, match 0-1}] — artists Last.fm's listeners link to this one."""
    async def fetch():
        data = await call("artist.getSimilar", artist=artist, limit=limit)
        return [{"name": a["name"], "match": float(a.get("match") or 0)} for a in (data.get("similarartists") or {}).get("artist", [])]
    return await store.cached(f"lastfm:similar:{artist.lower()}", MONTH, fetch)


async def top_tracks(artist: str, limit: int = 10) -> list[str]:
    """An artist's most-played track titles on Last.fm (stands in for Spotify's withdrawn "top tracks")."""
    async def fetch():
        data = await call("artist.getTopTracks", artist=artist, limit=limit)
        return [t["name"] for t in (data.get("toptracks") or {}).get("track", [])]
    return await store.cached(f"lastfm:top:{artist.lower()}", MONTH, fetch)


async def tag_tracks(tag: str, limit: int = 100) -> list[dict[str, str]]:
    async def fetch():
        data = await call("tag.getTopTracks", tag=tag, limit=limit)
        return [{"title": t["name"], "artist": t["artist"]["name"]} for t in (data.get("tracks") or {}).get("track", [])]
    return await store.cached(f"lastfm:tag:{tag.lower()}", MONTH, fetch)
