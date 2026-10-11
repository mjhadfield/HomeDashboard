"""The News page's stories: RSS/Atom feeds grouped into sections (World, UK, Technology, Science, Music…).

Each section lists its own feeds, in order of preference (the first one keeps a story several outlets carry), and keeps
its stories for as long as it says. Everything is editable in Settings → News. A feed that fails is reported but
doesn't stop the others. Tabloids are refused outright, whatever the feed (see TABLOIDS).

What the page shows — merged, ranked, learned-from, spoiler-shielded — is worked out per request in api_news.py; this
widget only keeps the store of stories fresh, so nothing heavy rides along in the dashboard's own state.
"""
from __future__ import annotations

import asyncio
import calendar
import html
import json
import logging
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import feedparser
import httpx

from .. import __version__, config, content, db, newsrank, newstopics
from .base import Widget

log = logging.getLogger("hub.news")

MAX_BYTES = 5_000_000
GENERAL_KEEP = 30              # days, for a new section and for stories whose feed has since been removed
MOST_READ_SOURCE = "bbc-most-read"

# Never shown, whatever feed they arrive through: UK tabloids (and their sister/regional sites).
TABLOIDS = ("thesun.co.uk", "thesun.ie", "the-sun.com", "dailymail.co.uk", "mailonsunday.co.uk", "mailplus.co.uk", "express.co.uk",
            "dailystar.co.uk", "mirror.co.uk", "irishmirror.ie", "dailyrecord.co.uk", "metro.co.uk")
# Story pictures are only kept from hosts the page's CSP allows (main.py); anything else would just be a broken image.
IMAGE_HOSTS = ("ichef.bbci.co.uk", "i.guim.co.uk", "e3.365dm.com", "cdn.arstechnica.net", "www.newscientist.com", "www.quantamagazine.org",
               "www.sciencenews.org")

# What a section leans towards and away from (Settings → News → each section). A story matching "favour" moves up, one
# matching "play down" sinks; nothing is hidden. A term matches a feed's own category, a whole word or phrase in the
# headline or summary, or — starting with "/" — part of the story's address. Science leans hard: almost every outlet
# files environment under science, and that's not the science Mike reads for.
DEFAULT_LEANINGS = {
    "science": {
        "favour": ["space", "astronomy", "astronomer", "astronomers", "astronaut", "astronauts", "telescope", "SpaceX", "rocket",
                   "Mars", "moon", "lunar", "planet", "planets", "exoplanet", "galaxy", "galaxies", "black hole", "universe", "cosmic", "comet",
                   "asteroid", "physics", "physicist", "physicists", "quantum", "particle", "CERN", "neutrino", "neutrinos", "chemistry", "chemist",
                   "chemists", "molecule", "molecules", "molecular", "biology", "biologist", "biologists", "genetics", "gene", "genes", "genome",
                   "DNA", "evolution", "evolved", "fossil", "fossils", "dinosaur", "dinosaurs", "palaeontology", "neuroscience", "brain", "cell",
                   "cells", "protein", "bacteria", "microbes", "archaeology", "archaeologists", "Neanderthal", "Neanderthals", "ancient humans",
                   "mathematics", "maths", "Nobel"],
        "play_down": ["climate", "climate change", "climate crisis", "environment", "environmental", "emissions", "carbon", "net zero",
                      "pollution", "sewage", "flood", "floods", "flooding", "drought", "weather", "extreme weather", "hurricane", "storm",
                      "heatwave", "wildfire", "wildfires", "glacier", "glaciers", "El Niño", "recycling", "waste", "plastic", "plastics",
                      "renewable", "renewables", "solar panels", "wind farm", "energy", "conservation", "wildlife", "WWF", "deforestation",
                      "biodiversity", "nature", "trees", "woodland", "/weather/", "/environment/", "/commentisfree/", "comment",
                      "/news-release/", "/earth/"],     # NASA: press releases and Earth observation
    },
}
LEANING = {"favour": 1.5, "play_down": 2.0}
# Text articles only: pages that *are* a programme, a podcast or a video (BBC Sounds/iPlayer, the Guardian's audio and video
# pages…) are dropped whatever the feed. Written articles with a clip in them ("Watch: …" posts) are kept.
MEDIA_PATHS = re.compile(r"/(?:sounds|iplayer|programmes|av|videos?|audio|podcasts?)(?:/|$)", re.I)
# Sections that share a front-page tile by default (Settings → News → each section → "On the front page").
DEFAULT_FRONT_WITH = {"uk": "world"}     # how far a match moves a story up or down (cf. newsrank.score)


def _feed(fid: str, name: str, url: str, enabled: bool = True) -> dict[str, Any]:
    return {"id": fid, "name": name, "url": url, "enabled": enabled}


def _section(sid: str, name: str, feeds: list[dict[str, Any]], *, front: bool = True, keep_days: int = GENERAL_KEEP) -> dict[str, Any]:
    lean = DEFAULT_LEANINGS.get(sid, {})
    return {"id": sid, "name": name, "enabled": True, "front": front, "keep_days": keep_days, "include": [], "exclude": [],
            "favour": list(lean.get("favour", [])), "play_down": list(lean.get("play_down", [])), "personal": sid == "music",
            "front_with": DEFAULT_FRONT_WITH.get(sid), "feeds": feeds}


MUSIC_FEEDS = [
    _feed("blabbermouth", "Blabbermouth", "https://blabbermouth.net/feed"),
    _feed("metal-injection", "Metal Injection", "https://metalinjection.net/feed"),
    _feed("louder", "Louder", "https://www.loudersound.com/feeds/all"),
    _feed("metalsucks", "MetalSucks", "https://www.metalsucks.net/feed/"),
    _feed("angry-metal-guy", "Angry Metal Guy", "https://www.angrymetalguy.com/feed/"),
    _feed("bandcamp-daily", "Bandcamp Daily", "https://daily.bandcamp.com/feed"),
    _feed("loudwire", "Loudwire", "https://loudwire.com/feed/"),
    _feed("theprp", "Theprp", "https://www.theprp.com/feed/"),
    _feed("udiscover", "uDiscover Music", "https://www.udiscovermusic.com/feed/"),
]
# Science without the environment: these only cover the sciences, to balance the BBC's "Science & Environment".
SCIENCE_ONLY = [
    _feed("ars-science", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/science"),
    _feed("science-news", "Science News", "https://www.sciencenews.org/feed"),
    _feed("nasa", "NASA", "https://www.nasa.gov/feed/"),
    _feed("esa-science", "ESA", "https://www.esa.int/rssfeed/Our_Activities/Space_Science"),
]
DEFAULT_SECTIONS = [
    _section("world", "World", [_feed("bbc-world", "BBC News", "https://feeds.bbci.co.uk/news/world/rss.xml"),
                                _feed("guardian-world", "The Guardian", "https://www.theguardian.com/world/rss"),
                                _feed("aljazeera", "Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
                                _feed("npr-world", "NPR", "https://feeds.npr.org/1004/rss.xml", False)]),
    _section("uk", "UK", [_feed("bbc-uk", "BBC News", "https://feeds.bbci.co.uk/news/uk/rss.xml"),
                          _feed("guardian-uk", "The Guardian", "https://www.theguardian.com/uk-news/rss"),
                          _feed("sky-uk", "Sky News", "https://feeds.skynews.com/feeds/rss/uk.xml")]),
    _section("tech", "Technology", [_feed("bbc-tech", "BBC News", "https://feeds.bbci.co.uk/news/technology/rss.xml"),
                                    _feed("ars-technica", "Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
                                    _feed("guardian-tech", "The Guardian", "https://www.theguardian.com/uk/technology/rss"),
                                    _feed("the-verge", "The Verge", "https://www.theverge.com/rss/index.xml", False)]),
    _section("science", "Science", [_feed("bbc-science", "BBC News", "https://feeds.bbci.co.uk/news/science_and_environment/rss.xml"),
                                    _feed("new-scientist", "New Scientist", "https://www.newscientist.com/feed/home/"),
                                    _feed("guardian-science", "The Guardian", "https://www.theguardian.com/science/rss"),
                                    _feed("quanta", "Quanta Magazine", "https://www.quantamagazine.org/feed/"),
                                    *SCIENCE_ONLY]),
    _section("music", "Music", MUSIC_FEEDS),
]
# Settings → News → "Add a suggested source": vetted, working feeds for each default section.
SUGGESTED = {
    "world": DEFAULT_SECTIONS[0]["feeds"] + [_feed("dw-world", "DW", "https://rss.dw.com/rdf/rss-en-world"),
                                             _feed("france24", "France 24", "https://www.france24.com/en/rss")],
    "uk": DEFAULT_SECTIONS[1]["feeds"] + [_feed("independent-uk", "The Independent", "https://www.independent.co.uk/news/uk/rss")],
    "tech": DEFAULT_SECTIONS[2]["feeds"] + [_feed("the-register", "The Register", "https://www.theregister.com/headlines.atom"),
                                            _feed("mit-tech-review", "MIT Technology Review", "https://www.technologyreview.com/feed/"),
                                            _feed("wired", "Wired", "https://www.wired.com/feed/rss")],
    "science": DEFAULT_SECTIONS[3]["feeds"] + [_feed("phys-org", "Phys.org", "https://phys.org/rss-feed/"),
                                               _feed("physics-world", "Physics World", "https://physicsworld.com/feed/")],
    "music": MUSIC_FEEDS,
}

TAGS = re.compile(r"<[^>]+>")
LEGACY_KEYS = ("feeds", "include", "exclude", "max_age_days")
RESERVED_IDS = {"front", "archive", "mostread"}         # the News page's own tabs


def _terms(value: Any, label: str, most: int = 40) -> list[str]:
    """Validate a keyword list: up to `most` short words/phrases."""
    if not isinstance(value, list) or len(value) > most or not all(isinstance(t, str) and len(t) <= 60 for t in value):
        raise ValueError(f"{label} must be a list of up to {most} short words or phrases")
    return [t.strip() for t in value if t.strip()]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", str(text).lower()).strip("-")[:40]


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", text or ""))).strip()


def is_media(url: str | None) -> bool:
    """A programme, podcast or video page rather than a written article."""
    return bool(MEDIA_PATHS.search(urlsplit(url or "").path or ""))


def is_tabloid(url: str | None) -> bool:
    host = (urlsplit(url or "").hostname or "").lower()
    return any(host == t or host.endswith(f".{t}") for t in TABLOIDS)


def match_artist_tags(title: str, names: list[str]) -> list[str]:
    """Which followed artists a headline's title mentions, longest name first so e.g. "Ghost B.C." wins over "Ghost"
    on the same text. Case-insensitive, not part of a larger word — using lookaround rather than \\b, since \\b
    requires a word/non-word *transition* and so never matches right after a name ending in punctuation ("Ghost
    B.C.", "P.O.D."): both the trailing "." and a following space are non-word characters, so there's no transition
    for \\b to find there, and a real band would silently never get tagged. Simple on purpose otherwise, so this
    will still sometimes false-tag a common-word band name (Ghost, Kiss, Rush...) on unrelated text — accepted
    tradeoff for now, revisit only if that turns out noisy in practice, not preemptively."""
    if not title or not names:
        return []
    ordered = sorted({n for n in names if n}, key=len, reverse=True)
    pattern = re.compile("|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in ordered), re.IGNORECASE)
    by_fold = {n.casefold(): n for n in ordered}
    seen, out = set(), []
    for m in pattern.findall(title):
        canon = by_fold.get(m.casefold())
        if canon and canon not in seen:
            seen.add(canon)
            out.append(canon)
    return out


def _image(entry: Any) -> str:
    """The story's picture, if its host is one the page may show. Prefers a mid-sized rendition (Guardian lists several)."""
    cands = []
    for m in list(entry.get("media_content") or []) + list(entry.get("media_thumbnail") or []):
        if m.get("url") and (not m.get("medium") or m.get("medium") == "image"):
            try:
                width = int(m.get("width") or 0)
            except ValueError:
                width = 0
            cands.append((width, m["url"]))
    for link in entry.get("links") or []:
        if link.get("rel") == "enclosure" and "image" in (link.get("type") or "") and link.get("href"):
            cands.append((0, link["href"]))
    good = [(w, u) for w, u in cands if u.startswith("https://") and (urlsplit(u).hostname or "") in IMAGE_HOSTS]
    if not good:
        return ""
    mid = [c for c in good if 200 <= c[0] <= 700]
    return (max(mid) if mid else good[0])[1]


def parse_feed(raw: bytes, feed: dict[str, Any], *, cutoff: float) -> list[dict[str, Any]]:
    """RSS/Atom bytes -> stories in the feed's own order (its `pos` is part of the ranking), dropping anything older than
    `cutoff` (epoch) and anything linking to a tabloid."""
    parsed = feedparser.parse(raw)
    out, seen = [], set()
    for pos, e in enumerate(parsed.entries):
        title, link = clean(e.get("title")), (e.get("link") or "").strip()
        if not title or not link.startswith(("https://", "http://")) or is_tabloid(link) or is_media(link):
            continue
        key = newsrank.url_key(link)                     # a story is its address: the BBC lists one article under several ids ("…#0", "…#1")
        if key in seen:
            continue
        seen.add(key)
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        published = calendar.timegm(stamp) if stamp else None
        if published is not None and published < cutoff:
            continue
        date = datetime.fromtimestamp(published or time.time(), config.TZ).date().isoformat()
        summary = clean(e.get("summary"))
        summary = "" if summary.casefold() == title.casefold() else (summary[:237].rsplit(" ", 1)[0] + "…" if len(summary) > 240 else summary)
        meta = {"feed": feed["id"], "pos": pos, "published": published, "summary": summary, "image": _image(e),
                "topics": newstopics.extract(title, e.get("tags"))}
        out.append({"external_id": key[:300], "artist": None, "title": title, "subtitle": feed["name"], "url": link,
                    "date": date, "meta": meta})
    return out


def rekey() -> int:
    """Stories stored before they were keyed by address (some feeds list one article under several ids) are re-keyed,
    and same-address copies within a feed folded into one — keeping whichever you saved, voted on or read. Cheap and
    idempotent, so it simply runs with every fetch."""
    with db.db() as con:
        rows = con.execute("SELECT id, source, external_id, url, status, meta FROM feed_items WHERE kind = 'news' AND source != ?", (MOST_READ_SOURCE,)).fetchall()
        media = [r for r in rows if is_media(r["url"]) and r["status"] != "pinned" and not json.loads(r["meta"] or "{}").get("vote")]
        for r in media:                                    # stored before "text articles only": gone, unless you saved or voted on it
            con.execute("DELETE FROM feed_items WHERE id = ?", (r["id"],))
        rows = [r for r in rows if r not in media]
        groups: dict[tuple[str, str], list[Any]] = {}
        for r in rows:
            if r["url"]:
                groups.setdefault((r["source"], newsrank.url_key(r["url"])[:300]), []).append(r)
        fixed = 0
        for (source, key), rs in groups.items():
            if len(rs) == 1 and rs[0]["external_id"] == key:
                continue
            def worth(r: Any) -> tuple:
                m = json.loads(r["meta"] or "{}")
                return (r["status"] == "pinned", bool(m.get("vote")), bool(m.get("read")), bool(m.get("revealed")), -r["id"])
            keep, *rest = sorted(rs, key=worth, reverse=True)
            meta = json.loads(keep["meta"] or "{}")
            for r in rest:                                     # a mark on any copy survives on the one kept
                m = json.loads(r["meta"] or "{}")
                for k in content.STICKY_META:
                    if k in m and k not in meta:
                        meta[k] = m[k]
                con.execute("DELETE FROM feed_items WHERE id = ?", (r["id"],))
            con.execute("UPDATE feed_items SET external_id = ?, meta = ? WHERE id = ?", (key, json.dumps(meta), keep["id"]))
            fixed += 1
    return fixed


# ---- settings ---------------------------------------------------------------------------------------------------------

def upgrade(settings: dict[str, Any]) -> dict[str, Any]:
    """Settings saved before sections existed ({feeds, include, exclude, max_age_days}) become the Music section —
    feeds, on/off switches and keyword lists exactly as they were — alongside the new default sections. Done on read,
    so nothing needs migrating; the next save writes the new shape."""
    s = {k: v for k, v in settings.items() if k not in LEGACY_KEYS}
    if isinstance(settings.get("feeds"), list):
        keep = settings.get("max_age_days")
        music = {**_section("music", "Music", [], front=False, keep_days=keep if isinstance(keep, int) and 3 <= keep <= 365 else GENERAL_KEEP),
                 "include": list(settings.get("include") or []), "exclude": list(settings.get("exclude") or []),
                 "feeds": [_feed(f.get("id") or _slug(f.get("name", "feed")), f.get("name", ""), f.get("url", ""), bool(f.get("enabled", True)))
                           for f in settings["feeds"] if isinstance(f, dict)]}
        s["sections"] = [sec for sec in DEFAULT_SECTIONS if sec["id"] != "music"] + [music]
    # Sections saved before leanings/"your artists" existed get them now, the way a new section would.
    s["sections"] = [{**{k: v for k, v in _section(sec["id"], sec.get("name", ""), []).items() if k in ("favour", "play_down", "personal", "front_with")}, **sec}
                     for sec in s.get("sections") or []]
    return s


def load() -> dict[str, Any]:
    """The News settings in force (upgraded)."""
    return upgrade(db.get_settings(widget.id, widget.defaults))


def _int(value: Any, label: str, lo: int, hi: int) -> int:
    if not (isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi):
        raise ValueError(f"{label} must be a whole number from {lo} to {hi}")
    return value


def _sections(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise ValueError("sections must be a list of 1 to 12")
    sids, fids, out = set(), set(), []
    for sec in value:
        if not isinstance(sec, dict):
            raise ValueError("each section must be an object")
        name = str(sec.get("name", "")).strip()[:30]
        if not name:
            raise ValueError("every section needs a name")
        sid = _slug(sec.get("id") or name) or "section"
        while sid in sids or sid in RESERVED_IDS:
            sid += "-2"
        sids.add(sid)
        feeds = sec.get("feeds", [])
        if not isinstance(feeds, list) or len(feeds) > 15:
            raise ValueError(f"section {name!r}: at most 15 feeds")
        clean_feeds = []
        for f in feeds:
            fname, url = str(f.get("name", "")).strip()[:40], str(f.get("url", "")).strip()
            if not fname or not url.startswith(("https://", "http://")):
                raise ValueError(f"section {name!r}: every feed needs a name and an http(s) address")
            if is_tabloid(url):
                raise ValueError(f"{fname} is a tabloid site — those are never allowed")
            fid = _slug(f.get("id") or f"{sid}-{fname}") or "feed"
            while fid in fids or fid == MOST_READ_SOURCE:
                fid += "-2"
            fids.add(fid)
            clean_feeds.append(_feed(fid, fname, url, bool(f.get("enabled", True))))
        out.append({"id": sid, "name": name, "enabled": bool(sec.get("enabled", True)), "front": bool(sec.get("front", True)),
                    "keep_days": _int(sec.get("keep_days", GENERAL_KEEP), f"{name}: keep for (days)", 3, 365),
                    "include": _terms(sec.get("include", []), f"{name}: only show"), "exclude": _terms(sec.get("exclude", []), f"{name}: hide"),
                    "favour": _terms(sec.get("favour", []), f"{name}: favour", 150), "play_down": _terms(sec.get("play_down", []), f"{name}: play down", 150),
                    "personal": bool(sec.get("personal", False)), "front_with": sec.get("front_with") or None, "feeds": clean_feeds})
    ids = {x["id"] for x in out}
    for x in out:                                       # sharing a tile only with a section that exists — and not with itself
        if x["front_with"] not in ids or x["front_with"] == x["id"]:
            x["front_with"] = None
    return out


class NewsWidget(Widget):
    id = "news"
    title = "News"
    interval = 3600              # ~15 small RSS requests an hour
    min_interval = 30 * 60
    log_success = False
    timeout = 120
    defaults = {"sections": DEFAULT_SECTIONS, "most_read": True, "latest_hours": 48, "latest_cap": 20, "front_per_section": 4,
                "f1_shield": True, "f1_watched": None, "f1_extra_terms": []}

    def public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {**upgrade(settings), "suggested": SUGGESTED, "tabloids": list(TABLOIDS)}

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        if unknown := set(new) - set(self.defaults):
            raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        merged = {**self.defaults, **upgrade(old)}
        if "sections" in new:
            merged["sections"] = _sections(new["sections"])
        for key in ("most_read", "f1_shield"):
            if key in new:
                merged[key] = bool(new[key])
        if "latest_hours" in new:
            merged["latest_hours"] = _int(new["latest_hours"], "latest window (hours)", 12, 168)
        if "latest_cap" in new:
            merged["latest_cap"] = _int(new["latest_cap"], "stories per section", 5, 50)
        if "front_per_section" in new:
            merged["front_per_section"] = _int(new["front_per_section"], "stories per section on the front page", 2, 8)
        if "f1_extra_terms" in new:
            merged["f1_extra_terms"] = _terms(new["f1_extra_terms"], "extra F1 words")
        if "f1_watched" in new:
            w = new["f1_watched"]
            if w is not None and not (isinstance(w, dict) and isinstance(w.get("season"), int) and isinstance(w.get("round"), int) and 0 <= w["round"] <= 40):
                raise ValueError("f1_watched must be {season, round} or null")
            merged["f1_watched"] = None if w is None else {"season": w["season"], "round": w["round"]}
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        settings = upgrade(settings)
        await asyncio.to_thread(rekey)
        now = time.time()
        names = [a["name"] for a in content.list_artists()]
        status: dict[str, dict[str, Any]] = {}
        ok, errors = 0, []
        async with httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}, timeout=20, follow_redirects=True) as client:
            for sec in [s for s in settings["sections"] if s.get("enabled")]:
                cutoff = now - sec["keep_days"] * 86400
                for feed in [f for f in sec["feeds"] if f.get("enabled")]:
                    try:
                        resp = await client.get(feed["url"])
                        resp.raise_for_status()
                        if len(resp.content) > MAX_BYTES:
                            raise ValueError("feed is too large")
                        items = await asyncio.to_thread(parse_feed, resp.content, feed, cutoff=cutoff)
                        if sec["id"] == "music":
                            # Tagging is against the title only (keeps false positives down for common-word band names),
                            # as a story is ingested: following a new artist doesn't re-tag old stories.
                            for it in items:
                                it["meta"]["artists"] = match_artist_tags(it["title"], names)
                        content.upsert_items("news", feed["id"], items, baseline=True)
                        status[feed["id"]] = {"ok": True, "items": len(items), "last24": sum(1 for i in items if (i["meta"]["published"] or now) >= now - 86400)}
                        ok += 1
                    except Exception as exc:
                        errors.append(f"{feed['name']} ({sec['name']}): {type(exc).__name__}")
                        status[feed["id"]] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
                        log.warning("Feed '%s' couldn't be read: %s: %s", feed["name"], type(exc).__name__, exc)
        self._prune(settings)
        if errors and not ok:
            raise RuntimeError("; ".join(errors))
        return {"feeds": status, "errors": errors, "ok": ok}

    def _prune(self, settings: dict[str, Any]) -> None:
        """Each section keeps its stories for its own number of days (saved and liked ones for good); stories from feeds
        that are no longer in any section go after GENERAL_KEEP."""
        known = {MOST_READ_SOURCE}
        for sec in settings["sections"]:
            ids = [f["id"] for f in sec["feeds"]]
            known.update(ids)
            content.prune("news", older_than_days=sec["keep_days"], sources=ids, keep_liked=True)
        with db.db() as con:
            orphans = [r["source"] for r in con.execute("SELECT DISTINCT source FROM feed_items WHERE kind = 'news'") if r["source"] not in known]
        content.prune("news", older_than_days=GENERAL_KEEP, sources=orphans, keep_liked=True)

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        stories = sum(f.get("items", 0) for f in (d.get("feeds") or {}).values())
        return f"{d.get('ok', 0)} feeds, {stories} stories" + (f", {len(d['errors'])} failed" if d.get("errors") else "")

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        # Deliberately small (no headlines): this rides along in every dashboard refresh. The News page asks
        # /api/owner/news for its stories, which is also where the F1 spoiler shield is applied.
        d = data or {}
        return {"feeds": d.get("feeds", {}), "errors": d.get("errors", [])}


widget = NewsWidget()
