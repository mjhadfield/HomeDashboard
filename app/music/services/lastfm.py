"""Last.fm: similar artists, an artist's most-played tracks, and a tag's top tracks — the parts of discovery Spotify no
longer offers personal apps. Last.fm's terms ask for no more than five requests a second (averaged over five minutes)
and an identifying User-Agent; Citadel stays at about three a second, and if Last.fm still says it's had too many
(error 29) — or answers 429/5xx — nothing more is sent for a while. Answers are cached for weeks, since none of this
changes quickly."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from ... import __version__, config
from .. import store
from .base import Field, Service, ServiceError

API = "https://ws.audioscrobbler.com/2.0/"
MIN_GAP = 0.34
MONTH = 30 * 86400
REST = 15 * 60                          # after "too many requests" (or Last.fm being down), nothing is sent for this long
USER_AGENT = f"Citadel/{__version__} ({config.CONTACT})"


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
_resting_until = 0.0


def _rest(why: str) -> ServiceError:
    global _resting_until
    _resting_until = time.time() + REST
    return ServiceError(f"Last.fm {why} — Citadel will leave it alone for {REST // 60} minutes")


async def call(method: str, **params: Any) -> dict[str, Any]:
    global _last
    if not service.enabled():
        raise ServiceError("Last.fm is switched off in Settings → Music page")
    cfg = service.cfg()
    if not cfg.get("api_key"):
        raise ServiceError("Last.fm needs an API key — Settings → Music page")
    async with _lock:
        if time.time() < _resting_until:
            raise ServiceError("Last.fm asked Citadel to slow down — trying again shortly")
        wait = _last + MIN_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as http:
            r = await http.get(API, params={"method": method, "api_key": cfg["api_key"], "format": "json", "autocorrect": 1, **params})
        _last = time.monotonic()
        if r.status_code == 429 or r.status_code >= 500:
            raise _rest("is busy" if r.status_code >= 500 else "says that's too many requests")
    try:
        data = r.json()
    except ValueError:
        raise ServiceError(f"Last.fm answered {r.status_code}")
    if "error" in data:
        if data["error"] in (6, 7):                       # "artist/tag not found": an empty answer, not a fault
            return {}
        if data["error"] in (11, 16, 29):                 # service offline / temporarily unavailable / rate limit exceeded
            raise _rest("says that's too many requests" if data["error"] == 29 else "is having trouble")
        raise ServiceError(f"Last.fm: {data.get('message', data['error'])}")
    return data


async def similar(artist: str, limit: int = 30) -> list[dict[str, Any]]:
    """[{name, match 0-1, mbid}] — artists Last.fm's listeners link to this one (mbid: their MusicBrainz ID, or None)."""
    async def fetch():
        data = await call("artist.getSimilar", artist=artist, limit=limit)
        return [{"name": a["name"], "match": float(a.get("match") or 0), "mbid": a.get("mbid") or None}
                for a in (data.get("similarartists") or {}).get("artist", [])]
    return await store.cached(f"lastfm:similar:v2:{artist.lower()}", MONTH, fetch)


async def top_tracks(artist: str, limit: int = 10) -> list[str]:
    """An artist's most-played track titles on Last.fm (stands in for Spotify's withdrawn "top tracks")."""
    async def fetch():
        data = await call("artist.getTopTracks", artist=artist, limit=limit)
        return [t["name"] for t in (data.get("toptracks") or {}).get("track", [])]
    return await store.cached(f"lastfm:top:{artist.lower()}", MONTH, fetch)


# tags that say nothing about the music
TAG_NOISE = {"seen live", "favorites", "favourites", "favorite", "favourite", "my favorite", "my favourites", "awesome", "albums i own",
             "spotify", "all", "love", "loved", "good", "cool", "beautiful", "amazing", "epic", "best", "under 2000 listeners",
             "male vocalists", "female vocalists", "vocalists", "check out", "to listen", "favorite artists", "favourite artists"}


async def top_tags(artist: str, n: int = 4) -> list[str]:
    """An artist's genres as Last.fm's listeners tag them ("melodic death metal"), the noise left out."""
    async def fetch():
        data = await call("artist.getTopTags", artist=artist)
        return [t["name"] for t in (data.get("toptags") or {}).get("tag", []) if int(t.get("count") or 0) >= 10]
    tags = await store.cached(f"lastfm:tags:{artist.lower()}", MONTH, fetch)
    me = artist.lower()
    out = []
    for t in tags:
        k = t.strip().lower()
        if k in TAG_NOISE or k == me or len(k) > 28 or k in out:
            continue
        out.append(k)
        if len(out) == n:
            break
    return out


async def top_album(artist: str) -> str | None:
    """An artist's most-listened album on Last.fm ("Biggest album")."""
    async def fetch():
        data = await call("artist.getTopAlbums", artist=artist, limit=5)
        names = [a.get("name") for a in (data.get("topalbums") or {}).get("album", [])]
        return {"name": next((x for x in names if x and x != "(null)"), None)}
    return (await store.cached(f"lastfm:topalbum:{artist.lower()}", MONTH, fetch))["name"]


async def tag_tracks(tag: str, limit: int = 100) -> list[dict[str, str]]:
    async def fetch():
        data = await call("tag.getTopTracks", tag=tag, limit=limit)
        return [{"title": t["name"], "artist": t["artist"]["name"]} for t in (data.get("tracks") or {}).get("track", [])]
    return await store.cached(f"lastfm:tag:{tag.lower()}", MONTH, fetch)
