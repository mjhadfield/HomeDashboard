"""Your artists, for the News page's Music section: who a story is about, how much they mean to you, and what Citadel's
other cards know about them (a new album on Releases, a pre-order on Vinyl radar, a gig you have tickets for).

"Your artists" are everyone you've played a fair bit, own a record by or have seen live (Music Companion, read-only), plus
the artists Citadel follows — minus any you've muted. How much one means to you (0–3) comes from all of that, and lifts
their stories up the Music tab. Names are matched with their capitals and accents folded, so "a ghost town" isn't Ghost
and "Motorhead" is still Motörhead. Rebuilt at most every ten minutes, like the rest of the library.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any

from . import content, db, newstopics
from .music import library
from .spoilers import fold

MIN_PLAYS = 10
TTL_KEY = "news-artists"
# Band names that are everyday words: too likely to be something else in a headline ("Live", "Yes", "Love").
COMMON = frozenset("""live yes love air heart hole europe america chicago boston kansas journey poison cream free bush garbage
blur suede pulp oasis travis berlin genesis toto tool filter creed lit cake failure helmet train muse rush kiss ghost queen
death venom sleep low ozone japan alabama asia wolves wolf crown sabbath""".split())


def _seen_live() -> dict[str, int]:
    return {library.anorm(r["name"]): r["n"] for r in library._q("SELECT a.name, COUNT(*) AS n FROM setlists s JOIN artists a ON a.id = s.artist_id GROUP BY a.id")}


def affinity(plays: int, recent: int, owned: int, seen: int, followed: bool) -> float:
    """0–3: lots of plays (and recent ones), records on the shelf, gigs seen; a followed artist gets a floor."""
    a = 0.55 * math.log10(1 + plays) + 0.35 * math.log10(1 + recent) + 0.15 * min(owned, 6) + 0.15 * min(seen, 4)
    return round(min(3.0, max(a, 0.6 if followed else 0.0)), 3)


def artists() -> dict[str, dict[str, Any]]:
    """anorm(name) -> {name, affinity} for every artist that's yours."""
    def build() -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        followed = {library.anorm(a["name"]): a for a in content.list_artists()}
        muted = {k for k, a in followed.items() if a["status"] == "mute"}
        idx, owned, seen = ({}, {}, {})
        if library.available():
            try:
                idx, owned, seen = library.artist_index(), library.owned_by_artist(), _seen_live()
            except Exception:  # noqa: BLE001 — Music Companion busy or mid-migration: fall back to the followed artists
                idx, owned, seen = {}, {}, {}
        for k in set(idx) | set(owned) | set(seen) | set(followed):
            if k in muted or not k or content.is_junk_artist(k):
                continue
            a = idx.get(k, {})
            plays, recent = a.get("plays", 0), a.get("recent", 0)
            if plays < MIN_PLAYS and not owned.get(k) and not seen.get(k) and k not in followed:
                continue
            name = a.get("name") or (followed.get(k) or {}).get("name") or k
            if content.is_junk_artist(name):
                continue
            out[k] = {"name": name, "affinity": affinity(plays, recent, owned.get(k, 0), seen.get(k, 0), k in followed)}
        return out
    return library._memoised(TTL_KEY, build)


def _matcher() -> tuple[list[re.Pattern], dict[str, str]]:
    """Two patterns: names of several words match in any case ("Bring Me the Horizon" / "Bring Me The Horizon"); one-word
    names only with their capital, and an everyday-word name ("Ghost", "Muse") only for an artist you really play."""
    def build():
        forms: dict[str, str] = {}                       # folded form (casefolded for multi-word names) -> anorm key
        every = artists()
        for k, a in every.items():
            name = fold(a["name"]).strip()
            bare = re.sub(r"^the\s+", "", name, flags=re.I)          # "The Black Dahlia Murder" is usually just "Black Dahlia Murder"
            for form in {name, bare}:
                low = form.casefold()
                if len(form) < 3 or low in newstopics.STOPWORDS or (low in COMMON and a["affinity"] < 1.5):
                    continue
                forms.setdefault(low if " " in form else form, k)
        multi = sorted((f for f in forms if " " in f), key=len, reverse=True)
        single = sorted((f for f in forms if " " not in f), key=len, reverse=True)
        pats = [re.compile("|".join(rf"(?<![\w&]){re.escape(f)}(?![\w&])" for f in group), flags) for group, flags in ((multi, re.I), (single, 0)) if group]
        return pats, forms
    return library._memoised(TTL_KEY + "-re", build)


def match(text: str) -> list[dict[str, Any]]:
    """Your artists a headline mentions, best-loved first: [{key, name, affinity}]."""
    patterns, forms = _matcher()
    if not patterns or not text:
        return []
    every = artists()
    found: dict[str, dict[str, Any]] = {}
    folded = fold(text)
    for pattern in patterns:
        for m in pattern.finditer(folded):
            k = forms.get(m.group(0)) or forms.get(m.group(0).casefold())
            if k and k in every:
                found[k] = {"key": k, **every[k]}
    return sorted(found.values(), key=lambda a: -a["affinity"])


def card_links(keys: set[str], today: date | None = None) -> dict[str, list[dict[str, Any]]]:
    """anorm key -> what Citadel's other cards say about that artist right now."""
    if not keys:
        return {}
    today = today or date.today()
    out: dict[str, list[dict[str, Any]]] = {}
    recent = (today - timedelta(days=30)).isoformat()
    with db.db() as con:
        releases = con.execute("SELECT artist, title, date, url FROM feed_items WHERE kind = 'release' AND status != 'dismissed' AND date >= ? ORDER BY date", (recent,)).fetchall()
        vinyl = con.execute("SELECT artist, title, url, status FROM feed_items WHERE kind = 'vinyl' AND status != 'dismissed' ORDER BY status = 'purchased' DESC").fetchall()
        gigs = con.execute("SELECT artist, date, venue, city, ticket_url FROM gigs WHERE date >= ? ORDER BY date", (today.isoformat(),)).fetchall()
    for r in releases:
        k = library.anorm(r["artist"] or "")
        if k in keys and not any(x["kind"] == "release" for x in out.get(k, [])):
            out.setdefault(k, []).append({"kind": "release", "title": r["title"], "date": r["date"], "url": r["url"], "out": (r["date"] or "") <= today.isoformat()})
    for r in vinyl:
        k = library.anorm(r["artist"] or "")
        if k in keys and not any(x["kind"] == "vinyl" for x in out.get(k, [])):
            out.setdefault(k, []).append({"kind": "vinyl", "title": r["title"], "url": r["url"], "bought": r["status"] == "purchased"})
    for r in gigs:
        k = library.anorm(r["artist"] or "")
        if k in keys and not any(x["kind"] == "gig" for x in out.get(k, [])):
            out.setdefault(k, []).append({"kind": "gig", "date": r["date"], "where": ", ".join(x for x in (r["venue"], r["city"]) if x), "url": r["ticket_url"]})
    return out
