"""What a playlist can be made of. Each kind says what it's called, which services it needs, its settings (checked here)
and how to pick candidate songs; the builder (playlists.py) mixes them by share and applies the playlist's rules.

To add one: write an Ingredient with a `pick` and add it to KINDS. A new outside service usually arrives as a new
ingredient that uses it."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

from .. import db
from . import discover, gigs, library
from .services import all_services, lastfm, setlistfm
from .services.base import ServiceError
from .services.spotify import client as spotify

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Ingredient:
    id: str
    name: str
    icon: str
    src: str                                   # colour family on the page: history | shelf | deep | gig | new
    blurb: str
    defaults: dict[str, Any]
    pick: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]
    summary: Callable[[dict[str, Any]], str]
    needs: tuple[str, ...] = ()               # service ids
    ordered: bool = False                      # keep the order it gives (a setlist) instead of sampling
    clean: Callable[[dict[str, Any]], dict[str, Any]] = field(default=lambda p: p)

    def check(self, params: dict[str, Any]) -> dict[str, Any]:
        p = {**self.defaults, **{k: v for k, v in (params or {}).items() if k in self.defaults}}
        return self.clean(p)

    def ready(self) -> tuple[bool, str]:
        svcs = all_services()
        for s in self.needs:
            if s in svcs and not svcs[s].ready():
                return False, f"needs {svcs[s].name}"
        return True, ""

    def public(self) -> dict[str, Any]:
        ok, why = self.ready()
        return {"id": self.id, "name": self.name, "icon": self.icon, "src": self.src, "blurb": self.blurb, "defaults": self.defaults,
                "ready": ok, "why": why, "needs": list(self.needs)}


def _int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _choice(v: Any, options: tuple[str, ...]) -> str:
    if v not in options:
        raise ValueError(f"must be one of {', '.join(options)}")
    return v


def _plays(n: int) -> str:
    return f"{n} play{'' if n == 1 else 's'}"


# ---- the kinds ---------------------------------------------------------------------------------------------------

def _history_clean(p):
    return {"min_plays": _int(p["min_plays"], 1, 5000, 20), "not_played_days": _int(p["not_played_days"], 0, 3650, 365),
            "played_within_days": _int(p["played_within_days"], 0, 3650, 0)}


async def _history(p):
    return library.history(min_plays=p["min_plays"], not_played_days=p["not_played_days"], played_within_days=p["played_within_days"])


def _history_sum(p):
    bits = [f"≥ {_plays(p['min_plays'])}"]
    if p["not_played_days"]:
        bits.append(f"not played in {_span(p['not_played_days'])}")
    if p["played_within_days"]:
        bits.append(f"played in the last {_span(p['played_within_days'])}")
    return " · ".join(bits)


def _span(days: int) -> str:
    return f"{days // 365} year{'s' if days >= 730 else ''}" if days % 365 == 0 else f"{days // 30} months" if days % 30 == 0 and days >= 60 else f"{days} days"


def _capsule_clean(p):
    for k in ("start", "end"):
        if not DATE_RE.match(str(p[k])):
            raise ValueError("dates must be YYYY-MM-DD")
    if p["start"] > p["end"]:
        raise ValueError("the start date is after the end date")
    return {"start": p["start"], "end": p["end"], "label": str(p.get("label") or "")[:40]}


async def _capsule(p):
    return library.capsule(p["start"], p["end"], p["label"])


def _shelf_clean(p):
    return {"mode": _choice(p["mode"], ("mixed", "most", "never"))}


async def _shelf(p):
    return library.shelf(p["mode"])


def _deep_clean(p):
    return {"top_n": _int(p["top_n"], 1, 100, 10), "days": _int(p["days"], 0, 3650, 365)}


async def _deep(p):
    return library.deep_cuts(top_n=p["top_n"], days=p["days"])


def _artists_clean(p):
    names = [str(n).strip()[:80] for n in (p["names"] if isinstance(p["names"], list) else str(p["names"]).split(",")) if str(n).strip()][:20]
    return {"names": names, "mode": _choice(p["mode"], ("most", "least"))}


async def _artists(p):
    return library.by_artists(p["names"], p["mode"])


def _genre_clean(p):
    return {"genre": str(p["genre"]).strip()[:60] or "heavy metal"}


async def _genre(p):
    return library.by_genre(p["genre"])


def _gig_clean(p):
    kind = _choice(p["kind"], ("predict", "show", "setlistfm"))
    return {"kind": kind, "artist": str(p.get("artist") or "")[:80], "show_id": _int(p.get("show_id"), 0, 10**9, 0), "id": str(p.get("id") or "")[:16]}


async def _gig(p):
    if p["kind"] == "show":
        return library.show_tracks(p["show_id"])[1]
    if p["kind"] == "setlistfm":
        s = await setlistfm.by_id(p["id"])
        return gigs.tracks_from_setlist(s, f"played at {s['venue']}, {s['date']}")
    pred = await gigs.predict(p["artist"])
    out = []
    for s in pred["songs"]:
        artist = s["cover_of"] or p["artist"]
        k = library.key(artist, s["title"])
        q = library.lookup(artist, s["title"]) if library.available() else {}
        out.append({"key": k, "title": s["title"], "artist": artist, "album": q.get("album", ""), "mbid": q.get("mbid"), "plays": s["plays"], "last": q.get("last", ""),
                    "src": "gig", "why": f"{s['pct']}% of recent shows" + (f" · {s['tag']}" if s["tag"] else "") + f" · {_plays(s['plays']) if s['plays'] else 'never played'}"})
    return out


def _gig_sum(p):
    return {"predict": f"{p['artist']} · predicted from recent shows", "show": "a show you were at", "setlistfm": "a setlist from setlist.fm"}[p["kind"]]


def _releases_clean(p):
    return {"days": _int(p["days"], 7, 365, 60), "per_album": _int(p["per_album"], 1, 20, 3)}


async def _releases(p):
    today = date.today().isoformat()
    with db.db() as con:
        rows = con.execute("SELECT artist, title, date FROM feed_items WHERE kind = 'release' AND status NOT IN ('dismissed') AND date <= ? AND date >= date(?, ?) ORDER BY date DESC",
                           (today, today, f"-{p['days']} days")).fetchall()
    out = []
    for r in rows[:20]:
        albums = await spotify.search(f'album:"{r["title"]}" artist:"{r["artist"]}"', "album", 5)
        hit = next((a for a in albums if library.anorm(r["artist"]) in {library.anorm(x["name"]) for x in a.get("artists", [])}), None)
        if not hit:
            continue
        for t in (await spotify.album_tracks(hit["id"]))[:p["per_album"] * 3]:
            out.append({"key": library.key(r["artist"], t["name"]), "title": t["name"], "artist": r["artist"], "album": r["title"], "mbid": None, "plays": 0, "last": "",
                        "uri": t["uri"], "src": "new", "why": f"{r['title']} · out {r['date']}"})
    return out


def _seeded_clean(p):
    return {"seeds": _int(p["seeds"], 3, 100, 25), "per_artist": _int(p["per_artist"], 1, 5, 2)}


async def _similar(p):
    return await discover.candidate_tracks("similar", library.top_artists(365, p["seeds"]), p["per_artist"])


async def _related(p):
    return await discover.candidate_tracks("related", library.top_artists(365, p["seeds"]), p["per_artist"])


def _tag_clean(p):
    return {"tag": str(p["tag"]).strip()[:40] or "sludge"}


async def _tag(p):
    have = library.available()
    out = []
    for t in await lastfm.tag_tracks(p["tag"]):
        k = library.key(t["artist"], t["title"])
        if have and library.played(t["artist"], t["title"]):
            continue
        out.append({"key": k, "title": t["title"], "artist": t["artist"], "album": "", "mbid": None, "plays": 0, "last": "", "src": "tag", "why": f"Last.fm’s top {p['tag']} tracks"})
    return out


KINDS: dict[str, Ingredient] = {i.id: i for i in [
    Ingredient("history", "Your history", "history", "history", "Your plays — by play count, and when you last played them",
               {"min_plays": 20, "not_played_days": 365, "played_within_days": 0}, _history, _history_sum, ("companion",), clean=_history_clean),
    Ingredient("capsule", "Time capsule", "cal", "history", "What you were playing in a season, year or month",
               {"start": f"{date.today().year - 1}-06-01", "end": f"{date.today().year - 1}-08-31", "label": f"summer {date.today().year - 1}"},
               _capsule, lambda p: p["label"] or f"{p['start']} – {p['end']}", ("companion",), clean=_capsule_clean),
    Ingredient("shelf", "Your shelf", "disc", "shelf", "Tracks from the records you own — including ones you’ve never streamed",
               {"mode": "mixed"}, _shelf, lambda p: {"mixed": "most played + never streamed", "most": "your most played", "never": "never streamed"}[p["mode"]], ("companion",), clean=_shelf_clean),
    Ingredient("deep", "Deep cuts", "dig", "deep", "Album tracks you’ve never played, by the artists you play most",
               {"top_n": 10, "days": 365}, _deep, lambda p: f"never played · from your top {p['top_n']} artists" + (" this year" if p["days"] == 365 else ""), ("companion",), clean=_deep_clean),
    Ingredient("artists", "Picked artists", "user", "history", "Named artists — their most or least played",
               {"names": [], "mode": "most"}, _artists, lambda p: f"{', '.join(p['names'][:3]) or 'no artists yet'}{'…' if len(p['names']) > 3 else ''} · {p['mode']} played", ("companion",), clean=_artists_clean),
    Ingredient("genre", "Genre", "tag", "history", "One of your genres, from your own plays",
               {"genre": "heavy metal"}, _genre, lambda p: p["genre"], ("companion",), clean=_genre_clean),
    Ingredient("gig", "Gig setlist", "bolt", "gig", "A predicted setlist, a show you were at, or any setlist.fm setlist",
               {"kind": "predict", "artist": "", "show_id": 0, "id": ""}, _gig, _gig_sum, (), ordered=True, clean=_gig_clean),
    Ingredient("releases", "New releases", "radar", "new", "Albums out lately by artists you follow (from the Releases feed)",
               {"days": 60, "per_album": 3}, _releases, lambda p: f"followed artists · out in the last {p['days']} days", ("spotify",), clean=_releases_clean),
    Ingredient("similar", "Similar artists", "spark", "new", "Artists Last.fm links to the ones you play — only ones you’ve never played",
               {"seeds": 25, "per_artist": 2}, _similar, lambda p: f"Last.fm · new to you · seeded by your top {p['seeds']}", ("lastfm",), clean=_seeded_clean),
    Ingredient("related", "Band connections", "users", "new", "Bands sharing a member with ones you play (MusicBrainz)",
               {"seeds": 10, "per_artist": 2}, _related, lambda p: f"MusicBrainz · members’ other bands · top {p['seeds']}", ("musicbrainz", "lastfm"), clean=_seeded_clean),
    Ingredient("tag", "Last.fm tag", "tag", "new", "Last.fm’s top tracks for a tag (e.g. sludge, NWOBHM), minus what you know",
               {"tag": "sludge"}, _tag, lambda p: f"{p['tag']} · new to you", ("lastfm",), clean=_tag_clean),
]}


def check(ings: Any) -> list[dict[str, Any]]:
    """Validate a playlist's ingredient list: [{kind, share 1-100, params}] — at most eight."""
    if not isinstance(ings, list) or len(ings) > 8:
        raise ValueError("ingredients must be a list of at most 8")
    out = []
    for i in ings:
        if not isinstance(i, dict) or i.get("kind") not in KINDS:
            raise ValueError("unknown ingredient")
        try:
            params = KINDS[i["kind"]].check(i.get("params") or {})
        except ValueError as e:
            raise ValueError(f"{KINDS[i['kind']].name}: {e}")
        out.append({"kind": i["kind"], "share": _int(i.get("share"), 1, 100, 50), "params": params})
    return out


async def pick(ing: dict[str, Any]) -> list[dict[str, Any]]:
    kind = KINDS[ing["kind"]]
    ok, why = kind.ready()
    if not ok:
        raise ServiceError(f"{kind.name} {why}")
    return await kind.pick(ing["params"])


def describe(ing: dict[str, Any]) -> str:
    return KINDS[ing["kind"]].summary(ing["params"])
