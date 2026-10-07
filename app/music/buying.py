"""Worth buying (Music page): albums you play a lot that you don't own on vinyl, with the cheapest **original pressing**
and the cheapest **coloured edition** on the Discogs marketplace right now.

How it gets there, once every few hours (BuyingJob, on the shared scheduler):
  1. Candidates, from Music Companion (read-only): albums scored by all-time plays + 2 × plays in the last year — long-time
     favourites and current obsessions both — leaving out anything you own (the album, a set it's part of, the same
     MusicBrainz release group, or the same artist + title).
  2. Each album's Discogs *master* (every pressing of it): MusicBrainz's own link from the release group when there is one
     (exact), else a Discogs search on artist + title that must match exactly ("probable").
  3. Its vinyl versions, sorted into originals (the first year's pressings, nothing marked reissue / repress / remaster)
     and coloured editions (the format names a colour, splatter, marble, clear...).
  4. The marketplace: the lowest price and how many are for sale for a few of each, and Discogs' suggested price for a
     VG+ copy — "usual price".

Left out on the way: compilations (MusicBrainz says so, or most of the album's pressings on Discogs are marked "Comp";
soundtracks stay), and anything already on Citadel's Confirmed pre-orders ("Magma 10th Anniversary" counts as Magma).

Which are *affordable* is decided when the page asks, with your settings at that moment: at or under the most you'd pay,
and not above the usual price (where Discogs has one). So changing the limit needs no new fetch. Ids and versions (CC0
catalogue data) are kept for days; prices are never shown more than a few hours old (Discogs' terms ask for six at most).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import date, timedelta
from typing import Any

from .. import db, musicbrainz
from ..widgets.base import Widget
from . import library, store
from .services import discogs
from .services.base import ServiceError

log = logging.getLogger("hub.music.buying")

DAY = 86400
MASTER_TTL = 30 * DAY        # album -> master id
VERSIONS_TTL = 7 * DAY       # a master's vinyl versions
PER_KIND = 4                 # pressings priced per album, for originals and for coloured editions each
MIN_PLAYS = 15
DISMISSED_KEY = "music-buying"    # widget_settings: {"dismissed": [album ids]}
REGIONS = ("UK", "Europe", "UK & Europe", "US", "Germany", "Netherlands", "EU")

REISSUE = {"re", "rp", "rm", "reissue", "repress", "remastered", "unofficial", "unofficial release"}
COLOURS = {
    "red", "blue", "yellow", "green", "orange", "purple", "violet", "white", "clear", "transparent", "translucent", "gold", "silver",
    "pink", "rose", "brown", "grey", "gray", "marbled", "marble", "splatter", "swirl", "smoke", "coloured", "colored", "cream", "beige",
    "bone", "amber", "teal", "turquoise", "magenta", "maroon", "burgundy", "olive", "mint", "aqua", "cyan", "lilac", "lavender", "copper",
    "bronze", "galaxy", "neon", "glow", "crystal", "ruby", "emerald", "sapphire", "oxblood", "coke bottle", "half & half", "split", "tri-color",
    # Discogs' own abbreviations
    "blu", "yel", "gre", "ora", "pur", "whi", "cle", "tra", "gol", "sil", "pin", "ros", "bro", "gry", "mar", "spl",
}
COLOUR_RE = re.compile(r"\b(" + "|".join(sorted((re.escape(c) for c in COLOURS), key=len, reverse=True)) + r")\b", re.I)


# ---- pure helpers (tested) ------------------------------------------------------------------------------------------------

def tokens(fmt: str) -> list[str]:
    return [t.strip() for part in re.split(r"\s*\+\s*", fmt or "") for t in part.split(",") if t.strip()]


def is_reissue(fmt: str) -> bool:
    return any(t.lower() in REISSUE for t in tokens(fmt))


LABEL_PHRASE = re.compile(r"\b(?:white|black|blue|red|green|yellow|orange|pink|purple|grey|gray|silver|gold)\s+labels?\b", re.I)
NOT_FOR_SALE = {"test pressing", "tp", "promo", "unofficial release", "unofficial"}


def colour_of(fmt: str, text: str | None = None) -> str | None:
    """The vinyl's colour, from the pressing's own words ("Blue Translucent", "Clear With Blue Splatter") or its format
    ("LP, Album, Ltd, Red Marbled"). A black or plain pressing -> None; so is "White Label" (that's the label, not the vinyl)."""
    for part in re.split(r"\s*[;,]\s*", text or ""):
        if part and COLOUR_RE.search(LABEL_PHRASE.sub("", part)):
            return part.strip()
    hits = [t for t in tokens(fmt) if COLOUR_RE.search(LABEL_PHRASE.sub("", t))]
    return ", ".join(hits) if hits else None


def buyable(v: dict[str, Any]) -> bool:
    """Not a test pressing, promo or bootleg -- not what you'd be shopping for."""
    return not any(t.lower() in NOT_FOR_SALE for t in tokens(v.get("format", "")))


def year_of(v: dict[str, Any]) -> int | None:
    m = re.match(r"(\d{4})", str(v.get("released") or ""))
    return int(m.group(1)) if m and m.group(1) != "0000" else None


def sort_versions(versions: list[dict[str, Any]], master_year: int | None) -> dict[str, list[dict[str, Any]]]:
    """{original: [...], coloured: [...]}, each most promising first (UK/Europe/US, then most owned on Discogs)."""
    vinyl = [v for v in versions if "Vinyl" in (v.get("major_formats") or ["Vinyl"]) and buyable(v)]
    years = [y for y in (year_of(v) for v in vinyl) if y]
    # the first *vinyl* year: a CD-era album's original vinyl may come a year or more after the album itself
    first = min(years) if years else master_year
    rank = lambda v: (0 if (v.get("country") or "") in REGIONS else 1,  # noqa: E731
                      -int(((v.get("stats") or {}).get("community") or {}).get("in_collection") or 0))
    original = [v for v in vinyl if first and year_of(v) == first and not is_reissue(v.get("format", ""))]
    coloured = [v for v in vinyl if colour_of(v.get("format", ""), v.get("text")) and v not in original]
    return {"original": sorted(original, key=rank), "coloured": sorted(coloured, key=rank), "first_vinyl_year": first}


def affordable(p: dict[str, Any], max_price: float) -> bool:
    """Within budget: Discogs' suggested price for a VG+ copy is at or under your limit, and copies are for sale.
    (The marketplace's lowest price can't be used: it's any condition, from any country, before postage -- the API
    can't see a listing's grade, location or postage, so a Poor copy in Spain or one in Canada that won't ship here
    would look like a bargain.)"""
    guide = p.get("usual")
    return bool(p.get("for_sale")) and guide is not None and guide <= max_price


def sell_url(release_id: int) -> str:
    """The pressing's marketplace page: UK sellers, cheapest first -- real copies, with their postage."""
    return f"https://www.discogs.com/sell/release/{release_id}?ships_from=United+Kingdom&currency=GBP&sort=price%2Casc"


def score(plays: int, recent: int) -> int:
    return plays + 2 * recent


EDITION_WORDS = re.compile(r"\b(?:\d+\s*(?:st|nd|rd|th)?\s+)?anniversary(?:\s+edition)?\b|\b(?:deluxe|expanded|limited|special|collectors?)(?:\s+edition)?\b"
                           r"|\bremaster(?:ed)?\b|\b(?:original\s+)?(?:game\s+)?soundtrack\b", re.I)


def album_key(title: str) -> str:
    """"Magma 10th Anniversary", "DOOM (Original Soundtrack)", "Sacrament (20th Anniversary)" -> the album itself."""
    return re.sub(r"\s+", " ", EDITION_WORDS.sub(" ", library.norm_title(title))).strip()


def same_album(artist: str, title: str, other_artist: str, other_title: str) -> bool:
    """The same album by (allowing a typo in) the same artist: "System Of A Town" -- "Toxicity" is Toxicity."""
    if not album_key(title) or album_key(title) != album_key(other_title):
        return False
    a, b = library.anorm(artist), library.anorm(other_artist)
    return a == b or library._similarity(a, b) >= 0.8


def preorders() -> list[tuple[str, str]]:
    """Citadel's Confirmed pre-orders (the vinyl radar's "Purchased", and ones added by hand): [(artist, title)]."""
    with db.db() as con:
        return [(r["artist"] or "", r["title"]) for r in con.execute("SELECT artist, title FROM feed_items WHERE kind = 'vinyl' AND status = 'purchased'")]


def mostly_compilation(versions: list[dict[str, Any]]) -> bool:
    vinyl = [v for v in versions if "Vinyl" in (v.get("major_formats") or ["Vinyl"])]
    comp = [v for v in vinyl if any(t.lower() in ("comp", "compilation") for t in tokens(v.get("format", "")))]
    return bool(vinyl) and len(comp) * 2 > len(vinyl)


# ---- Music Companion: what you play and what you own ----------------------------------------------------------------------

def candidates(limit: int, skipped: dict[str, list] | None = None) -> list[dict[str, Any]]:
    """The albums you play most (blended), that you don't own on vinyl or have on pre-order, most-played first.
    `skipped` collects the pre-ordered ones, to say so on the page."""
    since = (date.today() - timedelta(days=365)).isoformat()
    owned_ids, owned_mbids, owned_keys = set(), set(), set()
    has_parts = bool(library._q("SELECT 1 FROM sqlite_master WHERE name = 'album_parts'"))
    for r in library._q("SELECT al.id, al.mbid, al.title, ar.name AS artist FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id "
                        "JOIN artists ar ON ar.id = al.artist_id"):
        owned_ids.add(r["id"])
        if r["mbid"]:
            owned_mbids.add(r["mbid"])
        owned_keys.add(library.key(r["artist"], r["title"]))
    if has_parts:
        for r in library._q("SELECT ap.part_album_id AS id, al.mbid, al.title, ar.name AS artist FROM album_parts ap "
                            "JOIN albums al ON al.id = ap.part_album_id JOIN artists ar ON ar.id = al.artist_id "
                            "WHERE ap.album_id IN (SELECT album_id FROM vinyl_holdings)"):
            owned_ids.add(r["id"])
            if r["mbid"]:
                owned_mbids.add(r["mbid"])
            owned_keys.add(library.key(r["artist"], r["title"]))
    rows = library._q("""SELECT al.id, al.title, al.mbid, al.year, ar.name AS artist, count(*) AS plays, sum(s.played_at >= ?) AS recent
                         FROM scrobbles s JOIN albums al ON al.id = s.album_id JOIN artists ar ON ar.id = al.artist_id
                         GROUP BY al.id HAVING plays >= ?""", since, MIN_PLAYS)
    dismissed = set(db.get_settings(DISMISSED_KEY, {"dismissed": []}).get("dismissed", []))
    pre = preorders()
    out = []
    for r in sorted(rows, key=lambda r: -score(r["plays"], r["recent"] or 0)):
        if r["id"] in owned_ids or (r["mbid"] and r["mbid"] in owned_mbids) or library.key(r["artist"], r["title"]) in owned_keys:
            continue
        if re.match(r"^various( artists)?$", r["artist"] or "", re.I) or r["id"] in dismissed:
            continue
        if any(same_album(r["artist"], r["title"], a, t) for a, t in pre):
            if skipped is not None and len(out) < limit:
                skipped.setdefault("preordered", []).append(f"{r['artist']} — {r['title']}")
            continue
        out.append({"album_id": r["id"], "artist": r["artist"], "title": r["title"], "mbid": r["mbid"], "year": r["year"],
                    "plays": r["plays"], "recent": r["recent"] or 0, "score": score(r["plays"], r["recent"] or 0)})
    out.sort(key=lambda a: -a["score"])
    return out[:limit]


# ---- Discogs ---------------------------------------------------------------------------------------------------------------

DISCOGS_MASTER_RE = re.compile(r"discogs\.com/master/(\d+)")


async def master_for(album: dict[str, Any]) -> dict[str, Any] | None:
    """{id, how: exact|probable, compilation} -- MusicBrainz's link first, else a Discogs search that matches exactly.
    `compilation`: MusicBrainz's own type (a soundtrack isn't one), or the search result's format."""
    async def find():
        types: list[str] = []
        if album.get("mbid"):
            try:
                rg = await musicbrainz.client.get(f"release-group/{album['mbid']}", inc="url-rels")
                types = rg.get("secondary-types") or []
                for rel in rg.get("relations", []):
                    m = DISCOGS_MASTER_RE.search(((rel.get("url") or {}).get("resource")) or "")
                    if m:
                        return {"id": int(m.group(1)), "how": "exact", "compilation": "Compilation" in types}
            except Exception:  # noqa: BLE001 -- MusicBrainz down: fall through to a search
                log.info("MusicBrainz lookup failed for %s", album["title"])
        found = await discogs.call("/database/search", type="master", artist=album["artist"], release_title=library.clean_title(album["title"]), per_page=10)
        want = library.key(album["artist"], album["title"])
        for res in found.get("results", []):
            artist, _, title = (res.get("title") or "").partition(" - ")
            if library.key(re.sub(r"\s*\(\d+\)", "", artist), title) == want:
                return {"id": int(res["id"]), "how": "probable", "compilation": "Compilation" in types or "Compilation" in (res.get("format") or [])}
        return {"id": None, "how": "none", "compilation": "Compilation" in types}
    hit = await store.cached(f"discogs:master2:{album['album_id']}:{album.get('mbid') or ''}", MASTER_TTL, find)
    return hit if hit.get("id") or hit.get("compilation") else None


async def versions_of(master_id: int, album: dict[str, Any]) -> dict[str, Any]:
    """Every vinyl pressing of the album, each with its own words about the vinyl (`text`: "Blue Translucent"). The
    versions list has no such words, so they come from a release search (each result names its master)."""
    async def fetch():
        master = await discogs.call(f"/masters/{master_id}")
        vs, page = [], 1
        while page <= 3:                                   # 300 vinyl versions is plenty to find the originals and colours
            d = await discogs.call(f"/masters/{master_id}/versions", format="Vinyl", per_page=100, page=page)
            vs += d.get("versions", [])
            if page >= ((d.get("pagination") or {}).get("pages") or 1):
                break
            page += 1
        texts, page = {}, 1
        while page <= 3:
            d = await discogs.call("/database/search", type="release", artist=album["artist"], release_title=library.clean_title(album["title"]),
                                   format="Vinyl", per_page=100, page=page)
            for r in d.get("results", []):
                if r.get("master_id") == master_id:
                    t = "; ".join(f["text"] for f in r.get("formats") or [] if f.get("text"))
                    if t:
                        texts[r["id"]] = t
            if page >= ((d.get("pagination") or {}).get("pages") or 1):
                break
            page += 1
        keep = ("id", "title", "format", "label", "country", "released", "catno", "major_formats", "stats")
        return {"year": master.get("year") or None, "versions": [{**{k: v.get(k) for k in keep}, "text": texts.get(v["id"])} for v in vs]}
    return await store.cached(f"discogs:versions2:{master_id}", VERSIONS_TTL, fetch)


async def price(release_id: int) -> dict[str, Any]:
    stats = await discogs.call(f"/marketplace/stats/{release_id}", curr_abbr="GBP")
    low = (stats.get("lowest_price") or {}).get("value")
    out = {"price": round(float(low), 2) if low is not None else None, "for_sale": int(stats.get("num_for_sale") or 0), "usual": None}
    if out["for_sale"]:
        sug = await discogs.call(f"/marketplace/price_suggestions/{release_id}")
        vgp = (sug.get("Very Good Plus (VG+)") or {}).get("value")
        out["usual"] = round(float(vgp), 2) if vgp is not None else None
    return out


def pressing(v: dict[str, Any], kind: str) -> dict[str, Any]:
    return {"id": v["id"], "kind": kind, "title": v.get("title"), "label": v.get("label"), "country": v.get("country"),
            "year": year_of(v), "format": v.get("format"), "colour": colour_of(v.get("format", ""), v.get("text")), "catno": v.get("catno"),
            "url": f"https://www.discogs.com/release/{v['id']}",
            "sell_url": sell_url(v["id"])}


async def gather(limit: int) -> dict[str, Any]:
    albums, unmatched, skipped = [], 0, {"preordered": [], "compilations": []}
    for a in candidates(limit * 3, skipped):              # some won't be on Discogs, or are compilations: look further
        if len(albums) >= limit:
            break
        m = await master_for(a)
        if m and m.get("compilation"):
            skipped["compilations"].append(f"{a['artist']} — {a['title']}")
            continue
        if not m:
            unmatched += 1
            continue
        vs = await versions_of(m["id"], a)
        if mostly_compilation(vs["versions"]):
            skipped["compilations"].append(f"{a['artist']} — {a['title']}")
            continue
        groups = sort_versions(vs["versions"], vs["year"])
        first_vinyl = groups.pop("first_vinyl_year")
        priced = {}
        for kind, list_ in groups.items():
            out = []
            for v in list_[:PER_KIND]:
                out.append({**pressing(v, kind), **await price(v["id"])})
            priced[kind] = [p for p in out if p["for_sale"]]
        albums.append({**a, "master_id": m["id"], "match": m["how"], "master_url": f"https://www.discogs.com/master/{m['id']}",
                       "original_year": vs["year"], "first_vinyl_year": first_vinyl, "counts": {k: len(v) for k, v in groups.items()}, **priced})
    skipped["preordered"] = skipped["preordered"][:limit]
    return {"albums": albums, "unmatched": unmatched, "skipped": skipped, "priced_at": int(time.time())}


# ---- what the page shows: the rules applied now, with your settings ---------------------------------------------------------

def view(data: dict[str, Any] | None) -> dict[str, Any]:
    max_price = discogs.service.max_price()
    dismissed = set(db.get_settings(DISMISSED_KEY, {"dismissed": []}).get("dismissed", []))
    out = []
    for a in (data or {}).get("albums", []):
        if a["album_id"] in dismissed:
            continue
        pick = {}
        for kind in ("original", "coloured"):
            offers = sorted(({**p, "sell_url": sell_url(p["id"])} for p in a.get(kind) or []),     # by the VG+ guide price
                            key=lambda p: p["usual"] if p.get("usual") is not None else 1e9)
            ok = [p for p in offers if affordable(p, max_price)]
            pick[kind] = {"best": ok[0] if ok else None, "cheapest": offers[0] if offers else None, "known": a["counts"].get(kind, 0)}
        out.append({k: a.get(k) for k in ("album_id", "artist", "title", "mbid", "plays", "recent", "score", "match", "master_url", "original_year", "first_vinyl_year")} | pick)
    names = db.get_settings(DISMISSED_KEY, {}).get("names", {})
    hidden = [{"album_id": i, "name": names.get(str(i), f"Album {i}")} for i in sorted(dismissed, key=lambda i: names.get(str(i), ""))]
    return {"albums": out, "max_price": max_price, "priced_at": (data or {}).get("priced_at"), "ready": discogs.service.ready(),
            "unmatched": (data or {}).get("unmatched", 0), "skipped": (data or {}).get("skipped") or {}, "hidden": hidden}


def dismiss(album_id: int, undo: bool = False) -> None:
    s = db.get_settings(DISMISSED_KEY, {"dismissed": []})
    d = [x for x in s.get("dismissed", []) if x != album_id] + ([] if undo else [album_id])
    names = {k: v for k, v in (s.get("names") or {}).items() if k != str(album_id)}
    if not undo:                                      # remembered by name, for the page's Hidden list
        row = library._q("SELECT al.title, ar.name AS artist FROM albums al JOIN artists ar ON ar.id = al.artist_id WHERE al.id = ?", album_id)
        names[str(album_id)] = f"{row[0]['artist']} — {row[0]['title']}" if row else f"Album {album_id}"
    db.put_settings(DISMISSED_KEY, {**s, "dismissed": d, "names": names})


# ---- the scheduled job ---------------------------------------------------------------------------------------------------------

class BuyingJob(Widget):
    id = "music-buying"
    title = "Worth buying (Discogs prices)"
    interval = 5 * 3600              # prices on the page are never more than ~6 hours old (Discogs' terms)
    min_interval = 3 * 3600
    kind = "sync"
    timeout = 40 * 60                # ~30 albums x a dozen requests at a request a second, plus first-time lookups
    setup_hint = "Add a Discogs personal access token in Settings → Music page → Music services → Discogs"

    def configured(self, settings: dict[str, Any]) -> bool:
        return discogs.service.ready() and library.available()

    def secrets(self, settings: dict[str, Any]) -> list[str]:
        return discogs.service.secrets()

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        try:
            return await gather(discogs.service.watch())
        except ServiceError as e:
            raise RuntimeError(str(e)) from None

    def summarise(self, data: dict[str, Any] | None) -> str:
        v = view(data)
        n_orig = sum(1 for a in v["albums"] if a["original"]["best"])
        n_col = sum(1 for a in v["albums"] if a["coloured"]["best"])
        return f"{len(v['albums'])} albums priced · {n_orig} originals and {n_col} coloured editions within £{v['max_price']:g}"


job = BuyingJob()
