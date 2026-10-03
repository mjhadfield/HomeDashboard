"""Setlists: what a band will probably play (from their last ten shows on Setlist.fm), what they did play, and the gigs
you've booked (Settings → Music) and been to (Music Companion)."""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import content, db, musicbrainz
from ..content import norm
from . import library
from .services import setlistfm

SHOWS = 10                     # how many recent shows a prediction is based on


def booked() -> list[dict[str, Any]]:
    today = date.today()
    out = []
    for g in content.list_gigs():
        d = date.fromisoformat(g["date"])
        if d >= today:
            out.append({**g, "in_days": (d - today).days})
    return out


async def artist_mbid(artist: str) -> str | None:
    with db.db() as con:
        row = con.execute("SELECT mbid FROM followed_artists WHERE name = ? AND mbid IS NOT NULL", (artist,)).fetchone()
    if row:
        return row["mbid"]
    if library.available() and (a := library.artist_index().get(library.anorm(artist))) and a.get("mbid"):
        return a["mbid"]
    try:
        return await musicbrainz.resolve_artist_id(artist)
    except Exception:
        return None


async def predict(artist: str) -> dict[str, Any]:
    """{artist, shows, tour, latest, songs: [{title, pct, tag, plays, cover_of, pos}]} — songs played at a quarter or more
    of the recent shows, in the order they usually come."""
    mbid = await artist_mbid(artist)
    sets = [s for s in await setlistfm.recent(artist, mbid) if len(s["songs"]) >= 5][:SHOWS]
    if not sets:
        return {"artist": artist, "shows": 0, "tour": "", "latest": None, "songs": []}
    have = library.available()
    tally: dict[str, dict[str, Any]] = {}
    firsts, lasts = {}, {}
    for s in sets:
        n = len(s["songs"])
        for i, song in enumerate(s["songs"]):
            k = norm(song["title"])
            e = tally.setdefault(k, {"title": song["title"], "count": 0, "pos": 0.0, "encore": 0, "cover_of": song["cover_of"]})
            e["count"] += 1
            e["pos"] += i / max(1, n - 1)
            e["encore"] += bool(song["set"] and "encore" in song["set"].lower())
        firsts[norm(s["songs"][0]["title"])] = firsts.get(norm(s["songs"][0]["title"]), 0) + 1
        lasts[norm(s["songs"][-1]["title"])] = lasts.get(norm(s["songs"][-1]["title"]), 0) + 1
    opener = max(firsts, key=firsts.get)
    closer = max(lasts, key=lasts.get)
    songs = []
    for k, e in tally.items():
        pct = round(100 * e["count"] / len(sets))
        if pct < 25:
            continue
        tag = "opener" if k == opener else "closer" if k == closer else "encore" if e["encore"] > e["count"] / 2 else ""
        p = library.lookup(e["cover_of"] or artist, e["title"]) if have else {}
        songs.append({"title": e["title"], "pct": pct, "tag": tag, "plays": p.get("plays", 0), "cover_of": e["cover_of"], "pos": e["pos"] / e["count"]})
    songs.sort(key=lambda s: (s["tag"] == "closer", s["tag"] != "opener", s["pos"]))
    latest = sets[0]
    return {"artist": artist, "shows": len(sets), "tour": latest["tour"], "latest": {"date": latest["date"], "venue": latest["venue"], "city": latest["city"]}, "songs": songs}


async def as_played(artist: str, on: str) -> dict[str, Any] | None:
    """The real setlist from a show on a given day (YYYY-MM-DD), once someone has put it on Setlist.fm."""
    from . import store
    mbid = await artist_mbid(artist)
    store.cache_clear(f"setlistfm:recent:{(mbid or artist).lower()}")      # after the show, ask again rather than reuse last week's answer
    for s in await setlistfm.recent(artist, mbid, pages=1):
        if s["date"] == on and s["songs"]:
            return s
    return None


def tracks_from_setlist(s: dict[str, Any], why: str) -> list[dict[str, Any]]:
    have = library.available()
    out = []
    for song in s["songs"]:
        artist = song["cover_of"] or s["artist"]
        k = library.key(artist, song["title"])
        p = library.lookup(artist, song["title"]) if have else {}
        out.append({"key": k, "title": song["title"], "artist": artist, "album": p.get("album", ""), "mbid": p.get("mbid"), "plays": p.get("plays", 0),
                    "last": p.get("last", ""), "src": "gig", "why": why + (f" · cover of {artist}" if song["cover_of"] else "")})
    return out
