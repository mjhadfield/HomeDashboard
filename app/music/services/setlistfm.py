"""Setlist.fm: what a band has been playing lately (to predict a setlist) and what they actually played (after the
show, or from a setlist.fm link). Its free keys allow two requests a second; searches are cached for half a day."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from ... import __version__, config
from .. import store
from .base import Field, Service, ServiceError

API = "https://api.setlist.fm/rest/1.0"
MIN_GAP = 0.6
URL_ID_RE = re.compile(r"setlist\.fm/setlist/.+-([0-9a-f]{6,10})\.html", re.I)


class SetlistfmService(Service):
    async def test(self) -> str:
        data = await call("/search/artists", artistName="Gojira", sort="relevance")
        return f"Working — found {data.get('total', 0)} artist(s) called Gojira"


service = SetlistfmService(
    id="setlistfm", name="Setlist.fm", order=30,
    blurb="Recent setlists — predicted setlists before a gig, the real one after, and playlists from a setlist.fm link.",
    link="https://api.setlist.fm/",
    fields=(Field("api_key", "API key", secret=True),),
)

_lock = asyncio.Lock()
_last = 0.0


async def call(path: str, **params: Any) -> dict[str, Any]:
    global _last
    if not service.enabled():
        raise ServiceError("Setlist.fm is switched off in Settings → Music page")
    key = service.cfg().get("api_key")
    if not key:
        raise ServiceError("Setlist.fm needs an API key — Settings → Music page")
    async with _lock:
        wait = _last + MIN_GAP - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=20, headers={"x-api-key": key, "Accept": "application/json", "Accept-Language": "en",
                                                         "User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}) as http:
            r = await http.get(API + path, params=params)
        _last = time.monotonic()
    if r.status_code == 404:
        return {}
    if r.status_code in (401, 403):
        raise ServiceError("Setlist.fm didn’t accept the API key")
    if r.status_code == 429:
        raise ServiceError("Setlist.fm is rate-limiting Citadel — try again shortly")
    r.raise_for_status()
    return r.json()


def songs_of(setlist: dict[str, Any]) -> list[dict[str, Any]]:
    """A setlist's songs in order: [{title, cover_of, set}] — intro/outro tapes left out."""
    out = []
    for s in (setlist.get("sets") or {}).get("set", []):
        name = s.get("name") or ("Encore" if s.get("encore") else "")
        for song in s.get("song", []):
            if song.get("tape") or not song.get("name"):
                continue
            out.append({"title": song["name"], "cover_of": (song.get("cover") or {}).get("name"), "set": name})
    return out


async def recent(artist: str, mbid: str | None, pages: int = 2) -> list[dict[str, Any]]:
    """The artist's most recent setlists (newest first), each as {id, date, venue, city, tour, songs}."""
    async def fetch():
        found = []
        for p in range(1, pages + 1):
            params = {"artistMbid": mbid, "p": p} if mbid else {"artistName": artist, "p": p}
            data = await call("/search/setlists", **params)
            for s in data.get("setlist", []):
                if not mbid and (s.get("artist") or {}).get("name", "").casefold() != artist.casefold():
                    continue
                found.append(_brief(s))
            if len(data.get("setlist", [])) < 20:
                break
        return found
    return await store.cached(f"setlistfm:recent:{(mbid or artist).lower()}", 12 * 3600, fetch)


async def by_id(setlist_id: str) -> dict[str, Any]:
    async def fetch():
        data = await call(f"/setlist/{setlist_id}")
        if not data:
            raise ServiceError("Setlist.fm has no setlist with that address")
        return _brief(data)
    return await store.cached(f"setlistfm:id:{setlist_id}", 30 * 86400, fetch)


def id_from_url(url: str) -> str | None:
    m = URL_ID_RE.search(url or "")
    return m.group(1) if m else None


def _brief(s: dict[str, Any]) -> dict[str, Any]:
    d = s.get("eventDate", "")                                  # dd-MM-yyyy
    iso = f"{d[6:10]}-{d[3:5]}-{d[0:2]}" if len(d) == 10 else ""
    venue = s.get("venue") or {}
    return {"id": s.get("id"), "artist": (s.get("artist") or {}).get("name", ""), "date": iso, "venue": venue.get("name", ""),
            "city": (venue.get("city") or {}).get("name", ""), "tour": (s.get("tour") or {}).get("name", ""), "url": s.get("url", ""),
            "songs": songs_of(s)}
