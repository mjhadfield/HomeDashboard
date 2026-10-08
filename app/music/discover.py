"""Discover: artists you'd probably like but have never (or barely) played, and a few for each vibe you listen to.

Spotify no longer tells personal apps who's "related", so this is built from Last.fm's similar artists and MusicBrainz's
band memberships, weighted by how much you play the artists they're connected to (your top 100 — "seeds"); solo careers
and side projects of those artists are left out. An artist
similar to several of your seeds ranks higher, but only a little: its closest link counts in full and each further one
for a third, and no one vibe or seed fills more than a few places — otherwise whatever you play most crowds out the rest.
It's slow to work out (MusicBrainz asks for one request a second, and Spotify is only asked a little at a time), so it's
done in the background: overnight once a week, or when asked for (see "when it's worked out" below)."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import Counter
from typing import Any

from .. import config, content, db
from ..widgets.base import Widget
from . import library, store
from . import matcher, vibes
from .services import lastfm, musicbrainz as mb
from .services import spotify as sp
from .services.base import ServiceError

log = logging.getLogger("hub.music")
BASES = {"12m": "your last 12 months", "all": "all time", "shelf": "your records", "gigs": "gigs you’ve been to"}
WEEK = 7 * 86400
BARELY = 3                     # this many plays or fewer still counts as "barely tried"
SEEDS = 100                    # your most played artists that suggestions are drawn from
SHOW_EACH = 15                 # never-played and barely-tried artists on the page, each (a 5×3 grid apiece)
SCAN = 400                     # candidates looked at to find them
STACK = 0.3                    # each further seed an artist is similar to adds this share of that link
PER_VIBE, PER_SEED = 3, 2      # most of a group's places one vibe / one of your artists can account for, until nothing else is left
VIBE_SHOW, VIBE_LOOK = 5, 30   # artists per vibe, and candidates looked at (their tags checked) to find them
SAME_CROWD = 0.95              # Last.fm similarity at which a band sharing a fifth or more of its members with yours is taken for its
                               # side project (Slo Burn: 1 of 5 from Kyuss) — Dark Tranquillity (2 of 13 once in In Flames) isn't
GIVE_UP = 3                    # a Spotify / Last.fm hiccup skips one artist; this many and the rest wait for the next refresh
VERSION = 6                    # bump when what a result holds changes, so the nightly job redoes an older one (6: albums, not top 10s)
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


def seeds(basis: str, n: int = SEEDS) -> list[dict[str, Any]]:
    """Your top artists for a basis, each with `n` (its plays on that basis) and `w` (its weight: √ of its share of the top one's)."""
    idx = library.artist_index()
    if basis == "shelf":
        owned = library.owned_by_artist()
        picked = sorted((a for k, a in idx.items() if owned.get(k)), key=lambda a: -(owned[library.anorm(a["name"])] * 50 + a["plays"]))
    elif basis == "gigs":
        seen = {library.anorm(s["artist"]) for s in library.shows(400)}
        picked = sorted((a for k, a in idx.items() if k in seen), key=lambda a: -a["plays"])
    else:
        picked = library.top_artists(365 if basis == "12m" else 0, n + 20)
    picked = [a for a in picked if not content.is_junk_artist(a["name"])][:n]
    measure = "recent" if basis == "12m" else "plays"
    top = max((a[measure] for a in picked), default=1) or 1
    return [{**a, "n": a[measure], "w": math.sqrt(a[measure] / top)} for a in picked]


def _weighted(seed_list: list[dict[str, Any]]) -> list[tuple[dict[str, Any], float]]:
    top = max((s["plays"] for s in seed_list), default=1) or 1                  # seeds from elsewhere (ingredients) carry no weight yet
    return [(s, s["w"] if "w" in s else math.sqrt(s["plays"] / top)) for s in seed_list]


def _known(name: str) -> int:
    a = library.artist_index().get(library.anorm(name))
    return a["plays"] if a else 0


def _entry(name: str, plays: int, mbid: str | None = None) -> dict[str, Any]:
    return {"name": name, "mbid": mbid, "score": 0.0, "best": 0.0, "likes": [], "via": [], "links": [], "mb": 0.0, "mb_seed": None, "plays": plays}


def _rescore(e: dict[str, Any]) -> dict[str, Any]:
    """score: the closest Last.fm link in full, each further one for STACK, plus any band-member bonus. best: the closest
    link's own strength (Last.fm's 0–1 "match"), which is what the page shows as "% match"."""
    e["via"].sort(key=lambda v: -v["match"] * v["w"])
    parts = [v["match"] * v["w"] for v in e["via"]]
    e["score"] = (parts[0] + STACK * sum(parts[1:]) if parts else 0.0) + e["mb"]
    e["best"] = max((v["match"] for v in e["via"]), default=0.0)
    e["likes"] = [v["seed"] for v in e["via"]]
    return e


def _lead(e: dict[str, Any]) -> str | None:
    """The seed an artist is mostly here because of."""
    return e["likes"][0] if e["likes"] else e["mb_seed"]


def _within(e: dict[str, Any], seed_names: set[str]) -> dict[str, Any] | None:
    """An artist as seen from some of your seeds only (one vibe's), or None if none of them leads to it."""
    via = [v for v in e["via"] if v["seed"] in seed_names]
    mb = e["mb"] if e["mb_seed"] in seed_names else 0.0
    if not via and not mb:
        return None
    return _rescore({**e, "via": via, "mb": mb, "links": e["links"] if mb else []})


async def similar_artists(seed_list: list[dict[str, Any]], *, include_barely: bool = True) -> dict[str, dict[str, Any]]:
    """Candidates from Last.fm: anorm -> {name, score, best, likes: [seed names, closest first], via: [{seed, match, w}]}."""
    out: dict[str, dict[str, Any]] = {}
    for s, w in _weighted(seed_list):
        for c in await lastfm.similar(s["name"]):
            k = library.anorm(c["name"])
            plays = _known(c["name"])
            if plays > (BARELY if include_barely else 0) or content.is_junk_artist(c["name"]):
                continue
            out.setdefault(k, _entry(c["name"], plays, c.get("mbid")))["via"].append({"seed": s["name"], "match": c["match"], "w": w})
    for e in out.values():
        _rescore(e)
    return out


async def related_artists(seed_list: list[dict[str, Any]], *, max_seeds: int = 10, max_members: int = 6) -> dict[str, dict[str, Any]]:
    """Candidates from MusicBrainz: bands that share a member with a band you play (and solo artists' other bands)."""
    out: dict[str, dict[str, Any]] = {}
    for s, w in [(s, w) for s, w in _weighted(seed_list) if s.get("mbid")][:max_seeds]:
        rels = await mb.artist_rels(s["mbid"])
        people = rels["members"][:max_members] if rels["type"] != "Person" else [{"name": s["name"], "mbid": s["mbid"]}]
        for person in people:
            prels = rels if person["mbid"] == s["mbid"] else await mb.artist_rels(person["mbid"])
            for band in prels["member_of"]:
                k = library.anorm(band["name"])
                if k == library.anorm(s["name"]) or _known(band["name"]) > BARELY or content.is_junk_artist(band["name"]):
                    continue
                e = out.setdefault(k, _entry(band["name"], _known(band["name"]), band["mbid"]))
                e["mb"] = min(MB_CAP * w, e["mb"] + MB_LINK * w)
                e["mb_seed"] = e["mb_seed"] or s["name"]
                link = f"{person['name']} ({s['name']})" if person["name"] != s["name"] else s["name"]
                if link not in e["links"]:
                    e["links"].append(link)
    for e in out.values():
        _rescore(e)
    return out


async def seed_vibes(seed_list: list[dict[str, Any]]) -> dict[str, str | None]:
    """seed name -> its vibe, from its Last.fm tags."""
    return {s["name"]: vibes.of(await lastfm.top_tags(s["name"], 6)) for s in seed_list}


async def artist_tracks(name: str, n: int = 3) -> list[str]:
    """A few of an artist's best-known songs: Last.fm's most played (Spotify no longer shares its own top tracks)."""
    try:
        return (await lastfm.top_tracks(name, max(n, 5)))[:n]
    except ServiceError:
        return []


async def playable(name: str, want: int = 3) -> list[dict[str, str]]:
    """Songs by this artist that Spotify can actually play: [{title, uri}] — empty if it hasn't got them. Their best-known
    songs (Last.fm) first, found with a single Spotify search; the answer is kept for a month either way."""
    key = f"spotify:artist:{library.anorm(name)}"
    hit = store.cache_get(key, MONTH)
    if hit is not None:
        return hit
    found = await matcher.artist_songs(name, await artist_tracks(name, 6), want)
    store.cache_put(key, found)
    return found


async def biggest_album(name: str) -> str | None:
    """Their most-listened album on Last.fm, or None (Last.fm not set up, or it can't say)."""
    if not lastfm.service.ready():
        return None
    try:
        return await lastfm.top_album(name)
    except ServiceError:
        return None


async def spotify_album(artist: str, title: str | None, *, exact: bool = False) -> dict[str, str]:
    """{uri, name} of one of the artist's albums on Spotify — `title` (their biggest, from Last.fm) when Spotify has it,
    else the first full album of theirs it turns up — or {} if Spotify has nothing of theirs. `exact`: a search for that
    album by name (a new release, which a search of the artist could miss). One search, kept for a month."""
    key = f"spotify:album{':exact' if exact else ''}:{library.anorm(artist)}|{content.norm(title or '')}"
    hit = store.cache_get(key, MONTH)
    if hit is not None:
        return hit
    q = lambda t: t.replace('"', " ")                                                 # noqa: E731
    want_a, want_t = library.anorm(artist), content.norm(library.clean_title(title or ""))
    query = f'album:"{q(title or "")}" artist:"{q(artist)}"' if exact else f'artist:"{q(artist)}"'
    items = [a for a in await sp.client.search(query, "album", 10)
             if want_a in {library.anorm(x.get("name", "")) for x in a.get("artists", [])}]
    pick = next((a for a in items if want_t and content.norm(library.clean_title(a.get("name", ""))) == want_t), None)
    if exact:                                                        # the search was for that title: its best answer will do
        pick = pick or (items[0] if items else None)
    else:
        pick = pick or next((a for a in items if a.get("album_type") == "album"), None) or (items[0] if items else None)
    found = {"uri": pick["uri"], "name": pick.get("name", "")} if pick else {}
    if found or not exact:                                           # a new release not there yet is looked for again next time
        store.cache_put(key, found)
    return found


async def _ready(e: dict[str, Any], check: bool, errors: list[str]) -> bool:
    """Give a candidate its album (Last.fm's biggest, found on Spotify); False if Spotify (once connected) has none of theirs,
    or couldn't say (noted in `errors`, this step's own list). Raises ServiceError once GIVE_UP have gone wrong."""
    e["album"] = await biggest_album(e["name"])
    e["album_uri"] = ""
    if check:
        try:
            found = await spotify_album(e["name"], e["album"])
        except ServiceError as err:
            errors.append(str(err))
            if len(errors) >= GIVE_UP or isinstance(err, sp.Resting):
                raise
            return False
        if not found:
            return False
        e["album"], e["album_uri"] = found["name"] or e["album"], found["uri"]
    return True


def _offshoot(e: dict[str, Any], seed_keys: set[str]) -> bool:
    """"Sublime with Rome", "Marilyn Manson & The Spooky Kids": one of your own artists under another name."""
    k = library.anorm(e["name"])
    return any(s in k for s in seed_keys if len(s) >= 4)


class SideProjects:
    """Tells a solo career or side project of one of your artists (MusicBrainz band memberships) from a band that merely
    shares a member: a person who is or was in your band; a band at least half made of your band's members; or one that
    shares a fifth of its members and that Last.fm's listeners treat as the same band (SAME_CROWD). Fu Manchu, whose drummer
    was once in Kyuss, stays; Slo Burn (Kyuss's singer) and Kurt Cobain go. MusicBrainz down or switched off: nothing is hidden."""
    def __init__(self, seed_list: list[dict[str, Any]]):
        self.mbids = {s["name"]: s.get("mbid") for s in seed_list}
        self.ok = mb.service.enabled()

    async def __call__(self, e: dict[str, Any]) -> bool:
        if not self.ok or not e.get("mbid"):
            return False
        try:
            cand = await mb.artist_rels(e["mbid"])
            people = {e["mbid"]} if cand["type"] == "Person" else {m["mbid"] for m in cand["members"]}
            close = {v["seed"]: v["match"] for v in e["via"]}
            for name in set(close) | ({e["mb_seed"]} if e["mb_seed"] else set()):
                if not (mbid := self.mbids.get(name)):
                    continue
                rels = await mb.artist_rels(mbid)
                theirs = {mbid} if rels["type"] == "Person" else {m["mbid"] for m in rels["members"]}
                shared = people & theirs
                if shared and (cand["type"] == "Person" or 2 * len(shared) >= len(people)
                               or (5 * len(shared) >= len(people) and close.get(name, 0) >= SAME_CROWD)):
                    return True
        except Exception:                                        # MusicBrainz busy etc. — stop asking for this build
            log.warning("Discover: MusicBrainz side-project check failed; showing them unfiltered", exc_info=True)
            self.ok = False
        return False


async def _pick(ranked: list[dict[str, Any]], vibe_of: dict[str, str | None], check: bool, errors: list[str], side: SideProjects) -> list[dict[str, Any]]:
    """Up to SHOW_EACH never-played and SHOW_EACH barely-tried artists, best first — but no more than PER_VIBE from one
    vibe or PER_SEED because of one of your artists, unless there's nothing else to fill the places with."""
    kept, counts, used, spill = [], {"never": 0, "barely": 0}, Counter(), []
    for capped, cands in ((True, ranked[:SCAN]), (False, spill)):
        for e in cands:
            if all(n >= SHOW_EACH for n in counts.values()):
                break
            group = "never" if not e["plays"] else "barely"
            if counts[group] >= SHOW_EACH:
                continue
            vibe, lead = (group, "vibe", vibe_of.get(_lead(e))), (group, "seed", _lead(e))
            if capped and (used[vibe] >= PER_VIBE or used[lead] >= PER_SEED):
                spill.append(e)
                continue
            if await side(e):
                continue
            try:
                if not await _ready(e, check, errors):
                    continue
            except ServiceError:
                return sorted(kept, key=lambda e: -e["score"])
            kept.append(e)
            counts[group] += 1
            used[vibe] += 1
            used[lead] += 1
    return sorted(kept, key=lambda e: -e["score"])


async def _vibe_rows(seed_list: list[dict[str, Any]], vibe_of: dict[str, str | None], pool: dict[str, dict[str, Any]], check: bool,
                     errors: list[str], side: SideProjects) -> list[dict[str, Any]]:
    """For each vibe among your seeds, the VIBE_SHOW closest artists that its seeds lead to and that Last.fm's listeners tag
    the same way — the vibes you play least first. An artist only turns up under one vibe, and no more than PER_SEED of a
vibe's come from one of your artists while others are left (or a band's members' side projects would fill it)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for s in seed_list:
        if vibe_of.get(s["name"]):
            groups.setdefault(vibe_of[s["name"]], []).append(s)
    total = sum(s["n"] for s in seed_list) or 1
    rows, taken = [], set()
    for vibe in sorted(groups, key=lambda v: sum(s["n"] for s in groups[v])):
        mine = sorted(groups[vibe], key=lambda s: -s["n"])
        names = {s["name"] for s in mine}
        cands = sorted((x for k, e in pool.items() if k not in taken and (x := _within(e, names))), key=lambda e: -e["score"])
        picked, per_seed, spill = [], Counter(), []
        for capped, look in ((True, cands[:VIBE_LOOK]), (False, spill)):
            for e in look:
                if len(picked) == VIBE_SHOW:
                    break
                if capped and per_seed[_lead(e)] >= PER_SEED:
                    spill.append(e)
                    continue
                try:
                    tags = await lastfm.top_tags(e["name"], 6)
                    if not vibes.fits(tags, vibe) or await side(e) or not await _ready(e, check, errors):
                        continue
                except ServiceError as err:                      # rate-limited mid-way: keep the vibes worked out so far
                    errors.append(str(err))
                    return rows
                per_seed[_lead(e)] += 1
                e["tags"] = tags[:4]
                picked.append(e)
                taken.add(library.anorm(e["name"]))
        if picked:
            plays = sum(s["n"] for s in mine)
            rows.append({"id": vibe, "name": vibes.NAMES[vibe], "seeds": [s["name"] for s in mine], "plays": plays,
                         "share": round(100 * plays / total, 1), "artists": sorted(picked, key=lambda e: -e["best"])})
    return rows


async def _tile(e: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    reasons = []
    if e["links"]:
        reasons.append({"src": "mb", "text": f"Shares a member: {', '.join(e['links'][:2])}"})
    if e["likes"]:
        reasons.append({"src": "lfm", "text": f"Similar to {', '.join(e['likes'][:3])}"})
    tags = e.get("tags", [])
    if lastfm.service.ready() and not tags:
        try:                                                       # genres, for the tile
            tags = await lastfm.top_tags(e["name"])
        except ServiceError as err:
            errors.append(str(err))
    return {"artist": e["name"], "match": max(1, round(100 * e["best"])) if e["best"] else None, "plays": e["plays"], "reasons": reasons,
            "tags": tags, "album": e.get("album"), "album_uri": e.get("album_uri", "")}


async def build(basis: str = "12m") -> dict[str, Any]:
    seed_list = seeds(basis)
    errors, failed, pool, vibe_of = [], [], {}, {}                # failed: what went wrong, as opposed to what isn't set up
    if lastfm.service.ready():
        try:
            pool = await similar_artists(seed_list)
            vibe_of = await seed_vibes(seed_list)
        except ServiceError as e:
            failed.append(str(e))
    else:
        errors.append("Last.fm isn’t set up, so similar artists are missing")
    if mb.service.enabled():
        try:
            for k, e in (await related_artists(seed_list)).items():
                cur = pool.setdefault(k, _entry(e["name"], e["plays"], e["mbid"]))
                cur["mb"], cur["mb_seed"], cur["links"] = e["mb"], e["mb_seed"], e["links"]
                _rescore(cur)
        except Exception as e:                                   # MusicBrainz busy etc. — similar artists still stand
            failed.append(f"MusicBrainz: {e}")
    nope = {k for k, v in feedback().items() if v == "nope"}
    seed_keys = {library.anorm(s["name"]) for s in seed_list}
    pool = {k: e for k, e in pool.items() if k not in nope and not _offshoot(e, seed_keys)}
    check = sp.service.ready()                                   # only artists Spotify can play are shown — once it's connected
    if not check:
        errors.append("Connect Spotify so only artists it can play are shown")
    pick_errs, vibe_errs = [], []                                # each step gets its own GIVE_UP allowance
    side = SideProjects(seed_list)
    picked = await _pick(sorted(pool.values(), key=lambda e: -e["score"]), vibe_of, check, pick_errs, side)
    rows = await _vibe_rows(seed_list, vibe_of, pool, check, vibe_errs, side) if vibe_of else []
    failed += pick_errs + vibe_errs
    out = [await _tile(e, failed) for e in picked]
    for row in rows:
        row["artists"] = [await _tile(e, failed) for e in row["artists"]]
    result = {"basis": basis, "label": BASES.get(basis, basis), "built_at": int(time.time()), "artists": out, "vibes": rows,
              "seeds": len(seed_list), "errors": list(dict.fromkeys(errors + failed)), "partial": bool(failed), "checked": check,
              "version": VERSION}
    old = cached(basis)
    if failed and sp.resting_until() and old and not old.get("partial"):
        log.warning("Discover (%s): Spotify asked Citadel to wait mid-way; keeping the last full result", basis, extra={"source": "music"})
        return old                                               # half a result isn't worth losing a whole one for
    store.cache_put(f"discover:{basis}", result)
    return result


# ---- when it's worked out ---------------------------------------------------------------------------------------------------
# A result is kept until a newer one replaces it — never thrown away for its age or because this code changed — so a
# restart costs Spotify nothing. The nightly job (DiscoverJob) redoes stale ones, one at a time, at Spotify's background
# pace; a basis never worked out before is done when first asked for, and Refresh queues one by hand, at the same pace.

def cached(basis: str) -> dict[str, Any] | None:
    """The last result for a basis, however old."""
    return store.cache_get(f"discover:{basis}", float("inf"))


def stale(basis: str) -> bool:
    """Due to be worked out again: a week old, made by older code, cut short, or made before Spotify could vouch for its artists."""
    r = cached(basis)
    return (r is None or time.time() - r.get("built_at", 0) >= WEEK or r.get("version") != VERSION or bool(r.get("partial"))
            or (not r.get("checked") and sp.service.ready()))


_lock: asyncio.Lock | None = None


def refresh(basis: str) -> bool:
    """Queue a (re)build in the background; False if one is already queued or running (or this is a no-network test/dev
    run). Builds take turns — never two at once — and their Spotify calls use the background trickle."""
    global _lock
    if config.NO_FETCH or running(basis):
        return False
    _lock = _lock or asyncio.Lock()
    lock = _lock

    async def run():
        try:
            async with lock:
                with sp.background():
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
    global _lock
    for t in _running.values():
        t.cancel()
    await asyncio.gather(*_running.values(), return_exceptions=True)
    _running.clear()
    _lock = None


async def nightly() -> dict[str, Any]:
    """Redo every stale result that's been looked at (Your last 12 months always), one after another."""
    built, waiting = [], []
    for basis in BASES:
        if not stale(basis) or (basis != "12m" and cached(basis) is None):
            continue
        if sp.resting_until():
            waiting.append(basis)
            continue
        refresh(basis)
        if task := _running.get(basis):
            await task
        built.append(basis)
    return {"built": built, "waiting": waiting, "at": int(time.time())}


class DiscoverJob(Widget):
    id = "music-discover"
    title = "Discover (similar artists)"
    daily_at = "03:00"               # overnight, when nothing else wants Spotify; only stale results are redone
    interval = 24 * 3600
    min_interval = 12 * 3600
    kind = "sync"
    timeout = 6 * 3600               # a full rebuild at the background pace (a Spotify call every 6 s) takes a while
    setup_hint = "Add a Last.fm API key in Settings → Music page → Music services"

    def configured(self, settings: dict[str, Any]) -> bool:
        return lastfm.service.ready() and library.available()

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await nightly()

    def summarise(self, data: dict[str, Any] | None) -> str:
        data = data or {}
        names = lambda keys: ", ".join(BASES.get(k, k) for k in keys)            # noqa: E731
        parts = [f"worked out {names(data['built'])}"] if data.get("built") else ["nothing due"]
        if data.get("waiting"):
            parts.append(f"{names(data['waiting'])} waiting for Spotify")
        return " · ".join(parts)


job = DiscoverJob()


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
