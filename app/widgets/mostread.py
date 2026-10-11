"""The BBC's "Most read" top 10, for the News page's front page.

The BBC dropped its Most read RSS feed, but https://www.bbc.co.uk/news/popular/read still serves the list as plain
server-rendered HTML: an <ol> of ranked links inside <div data-component="mostRead">. One fetch an hour (robots.txt
doesn't ask otherwise). If the page changes shape and fewer than five stories come back, the fetch fails loudly
(event log, "couldn't refresh" on the page) and the last good list stays up.

Each story is also stored with the rest of the news (source 'bbc-most-read'), so the archive remembers what made the
list and how high it got, and a 👍/👎 on it teaches the page like any other story.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .. import __version__, config, content, db, newsrank, newstopics
from . import news
from .base import Widget

log = logging.getLogger("hub.mostread")

URL = "https://www.bbc.co.uk/news/popular/read"
MAX_BYTES = 6_000_000
MIN_ITEMS = 5


class _MostRead(HTMLParser):
    """Collects the links inside the Most read block, in order, stopping at the end of its list."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.active = self.done = False
        self.href: str | None = None
        self.text: list[str] = []
        self.items: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if self.done:
            return
        if not self.active and (a.get("data-component") == "mostRead" or a.get("id") == "mostRead-label"):
            self.active = True
        elif self.active and tag == "a" and a.get("href"):
            self.href, self.text = a["href"], []

    def handle_endtag(self, tag: str) -> None:
        if self.active and tag == "a" and self.href is not None:
            title = " ".join(" ".join(self.text).split())
            if title:
                self.items.append((self.href, title))
            self.href = None
        elif self.active and tag == "ol" and self.items:
            self.done = True

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.text.append(data)


def parse(page: str) -> list[dict[str, Any]]:
    """[{rank, title, url}] — the top 10. Raises ValueError if the list can't be found."""
    p = _MostRead()
    p.feed(page)
    out, seen = [], set()
    for href, title in p.items:
        url = urljoin("https://www.bbc.co.uk/", href)
        host = urlsplit(url).hostname or ""
        if host.removeprefix("www.") not in ("bbc.co.uk", "bbc.com") or url in seen or news.is_media(url):
            continue
        seen.add(url)
        out.append({"rank": len(out) + 1, "title": title[:300], "url": url})
        if len(out) == 10:
            break
    if len(out) < MIN_ITEMS:
        raise ValueError(f"found {len(out)} stories in the BBC's Most read list (expected 10) — the page may have changed")
    return out


def store(items: list[dict[str, Any]]) -> None:
    """Keep each story in the news store, remembering the highest place it reached."""
    today = datetime.now(config.TZ).date().isoformat()
    with db.db() as con:
        peaks = {r["external_id"]: json.loads(r["meta"] or "{}").get("peak_rank")
                 for r in con.execute("SELECT external_id, meta FROM feed_items WHERE kind = 'news' AND source = ?", (news.MOST_READ_SOURCE,))}
    rows = []
    for it in items:
        ext = newsrank.url_key(it["url"])
        peak = min(p for p in (it["rank"], peaks.get(ext)) if p)
        rows.append({"external_id": ext, "artist": None, "title": it["title"], "subtitle": "BBC News", "url": it["url"], "date": today,
                     "meta": {"rank": it["rank"], "peak_rank": peak, "published": None, "summary": "", "topics": newstopics.extract(it["title"])}})
    content.upsert_items("news", news.MOST_READ_SOURCE, rows, baseline=True)


class MostReadWidget(Widget):
    id = "mostread"
    title = "BBC Most read"
    interval = 3600
    min_interval = 30 * 60
    log_success = False
    timeout = 60

    def configured(self, settings: dict[str, Any]) -> bool:
        return bool(news.load().get("most_read", True))

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}, timeout=20, follow_redirects=True) as client:
            resp = await client.get(URL)
            resp.raise_for_status()
            if len(resp.content) > MAX_BYTES:
                raise ValueError("page is too large")
        items = parse(resp.text)
        prev = {newsrank.url_key(i["url"]): i["rank"] for i in (db.cache_get(self.id)["data"] or {}).get("items", [])}
        for it in items:
            it["prev_rank"] = prev.get(newsrank.url_key(it["url"]))
        store(items)
        content.prune("news", older_than_days=news.GENERAL_KEEP, sources=[news.MOST_READ_SOURCE], keep_liked=True)
        return {"items": items, "at": int(time.time()), "had_previous": bool(prev)}

    def summarise(self, data: dict[str, Any] | None) -> str:
        return f"{len((data or {}).get('items', []))} stories"

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        # Headlines never ride along in the dashboard state: the News page gets them from /api/owner/news, shielded.
        return {"count": len((data or {}).get("items", []))}


widget = MostReadWidget()
