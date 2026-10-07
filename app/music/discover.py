"""Discover: artists you'd probably like but have never (or barely) played, and ones you've drifted away from.

Spotify no longer tells personal apps who's "related", so this is built from Last.fm's similar artists and MusicBrainz's
band memberships, weighted by how much you play the artists they're connected to. It takes a minute or two (MusicBrainz
asks for one request a second), so it's worked out in the background, kept for a week, and refreshed on request."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from .. import config, content, db
from . import library, store
from . import matcher
from .services import lastfm, musicbrainz as mb
from .services import spotify as sp
from .services.base import ServiceError

log = logging.getLogger("hub.music")
BASES = {"12m": "your last 12 months", "all": "all time", "shelf": "your records", "gigs": "gigs you’ve been to"}
WEEK = 7 * 86400
BARELY = 3                     # this many plays or fewer still counts as "barely tried"
SHOW_EACH = 15                 # never-played and barely-tried artists on the page, each (a 5×3 grid apiece)
SCAN = 150                     # candidates looked at (on Spotify) to find them
VERSION = 3                    # bump when what a result holds changes, so an older one is worked out again (3: genres + biggest album)
MB_LINK, MB_CAP = 0.15, 0.3    # a shared member is a bonus on top of Last.fm's similarity, not a reason on its own —
                               # left uncapped, every member's side projects (mostly not on Spotify) crowded everything out
MONTH = 30 * 86400
_running: dict[str, asyncio.Task] = {}


def feedback() -> dict[str, str]:
    with db.db() as con:
        return {library.anorm(r["artist"]): r["verdict"] for r in con.execute("SELECT artist, verdict FROM music_feedback")}


def set_feedback(artist: str, verdict: str | None) -> None:
    with db.db() as con:
        if verdict is None:
            con.execute("DELETE FROM music_feedback WHERE artist = ?", (artist,))
        else:
            con.execute("INSERT INTO music_feedback (artist, verdict, at) VALUES (?, ?, ?) ON CONFLICT(artist) DO UPDATE SET verdict = excluded.verdict, at = excluded.at",
                        (artist, verdict, int(time.time())))
    if verdict == "follow":
        content.add_artist(artist, source="manual")               # Releases, Vinyl radar and Gig radar start watching them


def seeds(basis: str, n: int = 25) -> list[dict[str, Any]]:
    idx = library.artist_index()
    if basis == "shelf":
        owned = library.owned_by_artist()
        picked = sorted((a for k, a in idx.items() if owned.get(k)), key=lambda a: -(owned[library.anorm(a["name"])] * 50 + a["plays"]))
    elif basis == "gigs":
        seen = {library.anorm(s["artist"]) for s in library.shows(400)}
        picked = sorted((a for k, a in idx.items() if k in seen), key=lambda a: -a["plays"])
    else:
        picked = library.top_artists(365 if basis == "12m" else 0, n)
    return [a for a in picked if not content.is_junk_artist(a["name"])][:n]


def _known(name: str) -> int:
    a = library.artist_index().get(library.anorm(name))
    return a["plays"] if a else 0


async def similar_artists(seed_list: list[dict[str, Any]], *, include_barely: bool = True) -> dict[str, dict[str, Any]]:
    """Candidates from Last.fm: anorm -> {name, score, likes: [seed names]}."""
    out: dict[str, dict[str, Any]] = {}
    top = max((s["plays"] for s in seed_list), default=1) or 1
    for s in seed_list:
        w = math.sqrt(s["plays"] / top)
        for c in await lastfm.similar(s["name"]):
            k = library.anorm(c["name"])
            plays = _known(c["name"])
            if plays > (BARELY if include_barely else 0) or content.is_junk_artist(c["name"]):
                continue
            e = out.setdefault(k, {"name": c["name"], "score": 0.0, "likes": [], "links": [], "plays": plays})
            e["score"] += c["match"] * w
            e["likes"].append(s["name"])
    return out


async def related_artists(seed_list: list[dict[str, Any]], *, max_seeds: int = 10, max_members: int = 6) -> dict[str, dict[str, Any]]:
    """Candidates from MusicBrainz: bands that share a member with a band you play (and solo artists' other bands)."""
    out: dict[str, dict[str, Any]] = {}
    top = max((s["plays"] for s in seed_list), default=1) or 1
    for s in [x for x in seed_list if x.get("mbid")][:max_seeds]:
        w = math.sqrt(s["plays"] / top)
        rels = await mb.artist_rels(s["mbid"])
        people = rels["members"][:max_members] if rels["type"] != "Person" else [{"name": s["name"], "mbid": s["mbid"]}]
        for person in people:
            prels = rels if person["mbid"] == s["mbid"] else await mb.artist_rels(person["mbid"])
            for band in prels["member_of"]:
                k = library.anorm(band["name"])
                if k == library.anorm(s["name"]) or _known(band["name"]) > BARELY or content.is_junk_artist(band["name"]):
                    continue
                e = out.setdefault(k, {"name": band["name"], "score": 0.0, "likes": [], "links": [], "plays": _known(band["name"])})
                e["score"] = min(MB_CAP * w, e["score"] + MB_LINK * w)
                link = f"{person['name']} ({s['name']})" if person["name"] != s["name"] else s["name"]
                if link not in e["links"]:
                    e["links"].append(link)
    return out


async def artist_tracks(name: str, n: int = 3) -> list[str]:
    """A few of an artist's best-known songs: Last.fm's most played (Spotify no longer shares its own top tracks)."""
    try:
        return (await lastfm.top_tracks(name, max(n, 5)))[:n]
    except ServiceError:
        return []


async def playable(name: str, want: int = 3) -> list[dict[str, str]]:
    """Songs by this artist that Spotify can actually play: [{title, uri}] — empty if it hasn't got them. Tries their
    best-known songs (Last.fm) first, then Spotify's own search; the answer is kept for a month either way."""
    key = f"spotify:artist:{library.anorm(name)}"
    hit = store.cache_get(key, MONTH)
    if hit is not None:
        return hit
    found: list[dict[str, str]] = []
    titles = await artist_tracks(name, 6)
    for i, title in enumerate(titles):
        uri = (await matcher.match([{"key": library.key(name, title), "title": title, "artist": name}])).get(library.key(name, title))
        if uri:
            found.append({"title": title, "uri": uri})
        if len(found) >= want or (not found and i >= 2):        # three misses in a row: Spotify probably hasn't got them
            break
    if not found:
        want_a = library.anorm(name)
        for item in await sp.client.search(f'artist:"{name.replace(chr(34), " ")}"', "track", 10):
            if want_a in {library.anorm(a["name"]) for a in item.get("artists", [])} and item["uri"] not in {f["uri"] for f in found}:
                found.append({"title": item["name"], "uri": item["uri"]})
            if len(found) >= want:
                break
    store.cache_put(key, found)
    return found


async def build(basis: str = "12m") -> dict[str, Any]:
    seed_list = seeds(basis)
    errors, pool = [], {}
    if lastfm.service.ready():
        try:
            pool = await similar_artists(seed_list)
        except ServiceError as e:
            errors.append(str(e))
    else:
        errors.append("Last.fm isn’t set up, so similar artists are missing")
    if mb.service.enabled():
        try:
            for k, e in (await related_artists(seed_list)).items():
                cur = pool.setdefault(k, {**e, "score": 0.0, "likes": [], "links": []})
                cur["score"] += e["score"]
                cur["links"] = e["links"]
        except Exception as e:                                   # MusicBrainz busy etc. — similar artists still stand
            errors.append(f"MusicBrainz: {e}")
    nope = {k for k, v in feedback().items() if v == "nope"}
    ranked = sorted((e for k, e in pool.items() if k not in nope), key=lambda e: -e["score"])[:SCAN]
    check = sp.service.ready()                                   # only artists Spotify can play are shown — once it's connected
    if not check:
        errors.append("Connect Spotify so only artists it can play are shown")
    kept, counts = [], {"never": 0, "barely": 0}
    for e in ranked:
        group = "never" if not e["plays"] else "barely"
        if counts[group] >= SHOW_EACH:
            if all(n >= SHOW_EACH for n in counts.values()):
                break
            continue
        if check:
            try:
                e["tracks"] = await playable(e["name"])
            except ServiceError as err:                          # rate-limited mid-way: show what's been checked so far
                errors.append(str(err))
                break
            if not e["tracks"]:
                continue
        else:
            e["tracks"] = [{"title": t, "uri": ""} for t in await artist_tracks(e["name"])]
        kept.append(e)
        counts[group] += 1
    ranked = kept
    best = ranked[0]["score"] if ranked else 1
    out = []
    for e in ranked:
        reasons = []
        if e["links"]:
            reasons.append({"src": "mb", "text": f"Shares a member: {', '.join(e['links'][:2])}"})
        if e["likes"]:
            reasons.append({"src": "lfm", "text": f"Similar to {', '.join(e['likes'][:3])}"})
        tags, album = [], None
        if lastfm.service.ready():
            try:                                                   # genres and their biggest album, for the tile
                tags, album = await lastfm.top_tags(e["name"]), await lastfm.top_album(e["name"])
            except ServiceError as err:
                errors.append(str(err))
        out.append({"artist": e["name"], "match": max(5, round(100 * e["score"] / best)), "plays": e["plays"], "reasons": reasons,
                    "tracks": e["tracks"], "tags": tags, "album": album})
    result = {"basis": basis, "label": BASES.get(basis, basis), "built_at": int(time.time()), "artists": out, "errors": errors, "checked": check, "version": VERSION}
    store.cache_put(f"discover:{basis}", result)
    return result


def cached(basis: str) -> dict[str, Any] | None:
    """The last result, unless it's a week old — or was worked out before Spotify could vouch for every artist in it."""
    result = store.cache_get(f"discover:{basis}", WEEK)
    if result and ((not result.get("checked") and sp.service.ready()) or result.get("version") != VERSION):
        return None
    return result


def refresh(basis: str) -> bool:
    """Start (re)building in the background; False if one is already running (or this is a no-network test/dev run)."""
    if config.NO_FETCH or ((t := _running.get(basis)) and not t.done()):
        return False

    async def run():
        try:
            await build(basis)
        except Exception:
            log.exception("Discover (%s) failed", basis)
        finally:
            store.changed("discover")
    _running[basis] = asyncio.create_task(run(), name=f"discover-{basis}")
    return True


def running(basis: str) -> bool:
    return bool((t := _running.get(basis)) and not t.done())


async def stop() -> None:
    for t in _running.values():
        t.cancel()
    await asyncio.gather(*_running.values(), return_exceptions=True)
    _running.clear()


async def candidate_tracks(kind: str, seed_list: list[dict[str, Any]], per_artist: int = 2, n_artists: int = 20) -> list[dict[str, Any]]:
    """Songs by new-to-you artists, for the "Similar artists" and "Band connections" ingredients."""
    pool = await (similar_artists(seed_list, include_barely=False) if kind == "similar" else related_artists(seed_list))
    nope = {k for k, v in feedback().items() if v == "nope"}
    ranked = sorted((e for k, e in pool.items() if k not in nope), key=lambda e: -e["score"])[:n_artists]
    out = []
    check = sp.service.ready()
    for e in ranked:
        why = f"like {', '.join(e['likes'][:2])}" if kind == "similar" and e["likes"] else f"shares a member: {', '.join(e['links'][:1])}"
        songs = (await playable(e["name"], per_artist)) if check else [{"title": t, "uri": ""} for t in await artist_tracks(e["name"], per_artist)]
        for song in songs[:per_artist]:
            out.append({"key": library.key(e["name"], song["title"]), "title": song["title"], "artist": e["name"], "album": "", "mbid": None, "plays": 0, "last": "",
                        "uri": song["uri"], "src": kind, "why": why})
    return out
