"""Headlines from RSS/Atom feeds you choose, filtered by keyword rules you set.

Defaults are metal/vinyl-leaning feeds that were checked to work; everything is editable in Settings. A feed that
fails is reported but doesn't stop the others.
"""
from __future__ import annotations

import asyncio
import logging
import html
import re
import time
from datetime import datetime, timedelta
from typing import Any

import feedparser
import httpx

from .. import __version__, config, content
from .base import Widget

log = logging.getLogger("hub.news")

MAX_BYTES = 5_000_000
DEFAULT_FEEDS = [
    {"id": "blabbermouth", "name": "Blabbermouth", "url": "https://blabbermouth.net/feed", "enabled": True},
    {"id": "metal-injection", "name": "Metal Injection", "url": "https://metalinjection.net/feed", "enabled": True},
    {"id": "louder", "name": "Louder", "url": "https://www.loudersound.com/feeds/all", "enabled": True},
    {"id": "metalsucks", "name": "MetalSucks", "url": "https://www.metalsucks.net/feed/", "enabled": True},
    {"id": "angry-metal-guy", "name": "Angry Metal Guy", "url": "https://www.angrymetalguy.com/feed/", "enabled": True},
    {"id": "bandcamp-daily", "name": "Bandcamp Daily", "url": "https://daily.bandcamp.com/feed", "enabled": True},
    {"id": "loudwire", "name": "Loudwire", "url": "https://loudwire.com/feed/", "enabled": False},
    {"id": "theprp", "name": "Theprp", "url": "https://www.theprp.com/feed/", "enabled": False},
    {"id": "udiscover", "name": "uDiscover Music", "url": "https://www.udiscovermusic.com/feed/", "enabled": False},
]
TAGS = re.compile(r"<[^>]+>")


def _terms(value: Any, label: str) -> list[str]:
    """Validate a keyword list (include/exclude, global or per-feed): up to 40 short words/phrases."""
    if not isinstance(value, list) or len(value) > 40 or not all(isinstance(t, str) and len(t) <= 60 for t in value):
        raise ValueError(f"{label} must be a list of up to 40 short words or phrases")
    return [t.strip() for t in value if t.strip()]


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", text or ""))).strip()


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


def parse_feed(raw: bytes, feed: dict[str, Any], *, cutoff: str, include: list[str], exclude: list[str]) -> list[dict[str, Any]]:
    """RSS/Atom bytes -> feed items, newest first, dropping old items and applying the keyword rules."""
    parsed = feedparser.parse(raw)
    inc, exc = [t.casefold() for t in include if t.strip()], [t.casefold() for t in exclude if t.strip()]
    out = []
    for e in parsed.entries:
        title, link = clean(e.get("title")), (e.get("link") or "").strip()
        if not title or not link.startswith(("https://", "http://")):
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        date = time.strftime("%Y-%m-%d", stamp) if stamp else datetime.now(config.TZ).date().isoformat()
        if date < cutoff:
            continue
        haystack = f"{title} {clean(e.get('summary'))}".casefold()
        if (inc and not any(t in haystack for t in inc)) or any(t in haystack for t in exc):
            continue
        out.append({"external_id": (e.get("id") or link)[:300], "artist": None, "title": title, "subtitle": feed["name"], "url": link, "date": date,
                    "meta": {"feed": feed["id"]}})
    return out


class NewsWidget(Widget):
    id = "news"
    title = "Headlines"
    interval = 3 * 3600          # a handful of RSS requests
    min_interval = 30 * 60
    log_success = False
    timeout = 120
    defaults = {"feeds": DEFAULT_FEEDS, "include": [], "exclude": [], "max_age_days": 30}

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "feeds" in new:
            feeds = new["feeds"]
            if not isinstance(feeds, list) or len(feeds) > 25:
                raise ValueError("feeds must be a list of at most 25")
            seen, clean_feeds = set(), []
            for f in feeds:
                name, url = str(f.get("name", "")).strip()[:40], str(f.get("url", "")).strip()
                if not name or not url.startswith(("https://", "http://")):
                    raise ValueError("every feed needs a name and an http(s) address")
                fid = re.sub(r"[^a-z0-9-]+", "-", str(f.get("id") or name).lower()).strip("-")[:40] or "feed"
                while fid in seen:
                    fid += "-2"
                seen.add(fid)
                clean = {"id": fid, "name": name, "url": url, "enabled": bool(f.get("enabled", True))}
                for key in ("include", "exclude"):
                    if f.get(key):
                        clean[key] = _terms(f[key], f"feed {name!r} {key}")
                clean_feeds.append(clean)
            merged["feeds"] = clean_feeds
        for key in ("include", "exclude"):
            if key in new:
                merged[key] = _terms(new[key], key)
        if "max_age_days" in new and not (isinstance(new["max_age_days"], int) and 3 <= new["max_age_days"] <= 180):
            raise ValueError("max_age_days must be a whole number from 3 to 180")
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        cutoff = (datetime.now(config.TZ).date() - timedelta(days=settings["max_age_days"])).isoformat()
        # Tagging is against the title only (not the summary — keeps the false-positive surface down for
        # common-word band names) and only as a headline is first ingested: following a new artist won't
        # retroactively tag old stories already in the DB, same "silent baseline" behaviour as everywhere else.
        names = [a["name"] for a in content.list_artists()]
        ok, errors = 0, []
        async with httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}, timeout=20, follow_redirects=True) as client:
            for feed in [f for f in settings["feeds"] if f.get("enabled")]:
                try:
                    resp = await client.get(feed["url"])
                    resp.raise_for_status()
                    if len(resp.content) > MAX_BYTES:
                        raise ValueError("feed is too large")
                    feed_include = feed.get("include") or settings["include"]
                    feed_exclude = feed.get("exclude") or settings["exclude"]
                    items = await asyncio.to_thread(parse_feed, resp.content, feed, cutoff=cutoff, include=feed_include, exclude=feed_exclude)
                    for it in items:
                        it["meta"]["artists"] = match_artist_tags(it["title"], names)
                    content.upsert_items("news", feed["id"], items, baseline=not content.has_source("news", feed["id"]))
                    ok += 1
                except Exception as exc:
                    errors.append(f"{feed['name']}: {type(exc).__name__}")
                    log.warning("Feed '%s' couldn't be read: %s: %s", feed["name"], type(exc).__name__, exc)
        # "Keep stories for" is what it says on the tin: how long a story sticks around, not just how far back a
        # fresh sync will accept one — the two used to be different (fetch used this setting, but deletion was a
        # separate hardcoded 45 days), so a story could quietly vanish long before the number the settings page
        # showed. Pinned items are exempt regardless (see content.prune); unpinning makes one eligible again on
        # the very next sync, no separate bookkeeping needed for that.
        content.prune("news", older_than_days=settings["max_age_days"])
        if errors and not ok:
            raise RuntimeError("; ".join(errors))
        return {"feeds": ok, "errors": errors}

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        out = content.list_items("news", limit=300)
        enabled = {f["id"]: f["name"] for f in settings["feeds"] if f.get("enabled")}
        keep = lambda items: [i for i in items if i["source"] in enabled]                  # a feed you switched off disappears immediately
        return {**out, "items": keep(out["items"]), "dismissed": keep(out["dismissed"]), "sources": enabled, "errors": (data or {}).get("errors", [])}


widget = NewsWidget()
