"""Song -> Spotify track. Each answer is remembered (music_matches), so a song is only ever searched for once; one that
Spotify doesn't have is asked about again after a month, and a pick made by hand is kept for good.

Spotify's search is fuzzy, so a result is only accepted when the artist matches and the title matches once the usual
noise is stripped ("- 2018 Remaster", "(feat. …)"); a live recording isn't used for a studio song unless asked for."""
from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import db
from ..content import norm
from .library import anorm, clean_title
from .services.spotify import client

RETRY_MISSING = 30 * 86400
LIVE = ("live", "en vivo", "unplugged")


def remembered(keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    out = {}
    with db.db() as con:
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            for r in con.execute(f"SELECT * FROM music_matches WHERE key IN ({','.join('?' * len(chunk))})", chunk):
                out[r["key"]] = dict(r)
    return out


def remember(key: str, status: str, item: dict[str, Any] | None = None) -> None:
    item = item or {}
    with db.db() as con:
        con.execute("INSERT INTO music_matches (key, status, uri, name, artist, album, checked_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET status = excluded.status, uri = excluded.uri, name = excluded.name, artist = excluded.artist, "
                    "album = excluded.album, checked_at = excluded.checked_at",
                    (key, status, item.get("uri", ""), item.get("name", ""), ", ".join(a["name"] for a in item.get("artists", [])),
                     (item.get("album") or {}).get("name", ""), int(time.time())))


def forget_all() -> int:
    with db.db() as con:
        return con.execute("DELETE FROM music_matches WHERE status != 'manual'").rowcount


def _score(item: dict[str, Any], artist: str, title: str) -> int:
    want_a = anorm(artist)
    names = [anorm(a.get("name", "")) for a in item.get("artists", [])]
    if not any(n == want_a or (min(len(n), len(want_a)) > 3 and (n in want_a or want_a in n)) for n in names):
        return 0
    want_t, got_t = norm(clean_title(title)), norm(clean_title(item.get("name", "")))
    if not want_t or not got_t:
        return 0
    if want_t == got_t:
        score = 6
    elif (got_t.startswith(want_t) or want_t.startswith(got_t)) and min(len(got_t), len(want_t)) / max(len(got_t), len(want_t)) >= 0.6:
        score = 4
    else:
        return 0
    wants_live = any(w in title.lower() for w in LIVE)
    is_live = any(w in item.get("name", "").lower() for w in LIVE) or any(w in (item.get("album") or {}).get("name", "").lower() for w in LIVE)
    if is_live != wants_live:
        score -= 3
    if (item.get("album") or {}).get("album_type") == "compilation":
        score -= 1
    return score


def best(items: list[dict[str, Any]], artist: str, title: str) -> dict[str, Any] | None:
    scored = sorted(((s, i) for i in items if (s := _score(i, artist, title)) >= 3), key=lambda x: -x[0])
    return scored[0][1] if scored else None


def _q(text: str) -> str:
    return text.replace('"', " ").strip()


async def search_track(artist: str, title: str) -> dict[str, Any] | None:
    t = clean_title(title)
    items = await client.search(f'track:"{_q(t)}" artist:"{_q(artist)}"', "track", 10)
    hit = best(items, artist, title)
    if not hit:
        hit = best(await client.search(f"{_q(artist)} {_q(t)}", "track", 10), artist, title)
    return hit


async def match(tracks: list[dict[str, Any]], *, limit: int = 3) -> dict[str, str]:
    """key -> spotify uri for every track that can be matched ('' for ones Spotify doesn't have)."""
    known = remembered([t["key"] for t in tracks])
    out: dict[str, str] = {}
    todo = []
    now = time.time()
    for t in tracks:
        m = known.get(t["key"])
        if m and (m["status"] != "missing" or now - m["checked_at"] < RETRY_MISSING):
            out[t["key"]] = m["uri"]
        elif t["key"] not in out:
            out[t["key"]] = ""
            todo.append(t)
    sem = asyncio.Semaphore(limit)

    async def one(t):
        async with sem:
            hit = await search_track(t["artist"], t["title"])
        remember(t["key"], "ok" if hit else "missing", hit)
        out[t["key"]] = hit["uri"] if hit else ""
    await asyncio.gather(*(one(t) for t in {t["key"]: t for t in todo}.values()))
    return out


async def candidates(artist: str, title: str) -> list[dict[str, Any]]:
    """Possible Spotify tracks for one the matcher couldn't place, for picking by hand."""
    items = await client.search(f"{_q(artist)} {_q(clean_title(title))}", "track", 10)
    if len(items) < 3:
        items += await client.search(_q(clean_title(title)), "track", 10)
    seen, out = set(), []
    for i in items:
        if i["uri"] in seen:
            continue
        seen.add(i["uri"])
        out.append({"uri": i["uri"], "name": i["name"], "artist": ", ".join(a["name"] for a in i.get("artists", [])),
                    "album": (i.get("album") or {}).get("name", ""), "year": ((i.get("album") or {}).get("release_date") or "")[:4]})
    return out[:8]
