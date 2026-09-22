"""Gig radar: upcoming shows near you for the artists you follow, from the Ticketmaster Discovery API.

Needs a free API key (developer.ticketmaster.com — 5,000 calls/day). Each run checks a batch of artists, searching
by name within a radius of your location, and keeps only events that genuinely match the artist (Ticketmaster's
keyword search is fuzzy). Until a key is set the card says so.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import httpx

from .. import content
from .base import Widget

log = logging.getLogger("hub.radar")

URL = "https://app.ticketmaster.com/discovery/v2/events.json"
LATLONG_RE = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


# Ticketmaster lists hospitality / VIP / upgrade "packages" as separate events for the same show, so one gig shows up
# several times. Those are dropped; a package that is about merchandise is kept (it is the show plus a bundle you may want).
PREMIUM_RE = re.compile(r"\b(?:vip|premium|hospitality|platinum|meet\s*(?:&|and)\s*greet|soundcheck|sound\s+check|upgrades?|lounge|fast[\s-]?lane|priority\s+(?:entry|access)|early\s+entry|ultimate\s+fan)\b", re.I)
MERCH_RE = re.compile(r"\b(?:merch(?:andise)?|t-?shirts?|hoodies?|bundles?|posters?)\b", re.I)


def is_premium_package(name: str) -> bool:
    return bool(PREMIUM_RE.search(name)) and not MERCH_RE.search(name)


# A "gig radar" searching by artist name inevitably turns up tribute/cover acts — Ticketmaster sometimes tags these
# properly (a "Tribute Band" subType/type on the event or the attraction), and sometimes only says so in the name
# ("Rumours of Fleetwood Mac", "A Tribute To Kiss", "The Music of Queen"). Neither signal alone catches everything
# (real example: "Rumours of Fleetwood Mac" carries the classification but no word "tribute"; "KISS TRIBUTE - Hotter
# Than Hell" has the word but an "Undefined" classification) — so both are checked.
TRIBUTE_TEXT_RE = re.compile(r"\btributes?\b|\bcover\s*band\b|\btribute\s*band\b|\bthe\s+music\s+of\b|\bpays?\s+tribute\b", re.I)


def looks_like_tribute(ev: dict[str, Any], attractions: list[dict[str, Any]], artist: str) -> bool:
    """Text is checked across the whole bill — an explicit word anywhere ("support: X Tribute") is a real signal that
    the night is tribute-themed. Ticketmaster's own classification is checked only on the event itself and on the
    attraction that is actually `artist` — never on an unrelated support act sharing the bill (real example: an
    "Eagles of Death Metal" show was wrongly flagged because "Yachtley Crew", a support act, is tagged Tribute Band)."""
    want = content.norm(artist)
    own = [a for a in attractions if content.norm(a.get("name", "")) == want]
    for cls in [*(ev.get("classifications") or []), *(c for a in own for c in (a.get("classifications") or []))]:
        if any("tribute" in ((cls.get(field) or {}).get("name") or "").lower() for field in ("subType", "type", "genre", "subGenre")):
            return True
    names = " ".join([ev.get("name", ""), *(a.get("name", "") for a in attractions)])
    return bool(TRIBUTE_TEXT_RE.search(names))


def parse_ticketmaster(data: dict[str, Any], artist: str) -> list[dict[str, Any]]:
    """Discovery API events -> feed items, keeping only events that are really by `artist`."""
    want = content.norm(artist)
    as_words = re.compile(rf"(?<!\w){re.escape(artist.casefold())}(?!\w)")       # "Ghost" must not match "Ghostly Tribute Night"
    out = []
    for ev in (data.get("_embedded") or {}).get("events", []):
        name = ev.get("name", "")
        if is_premium_package(name):
            continue
        attractions_raw = (ev.get("_embedded") or {}).get("attractions", [])
        attractions = [a.get("name", "") for a in attractions_raw]
        if not as_words.search(name.casefold()) and not any(content.norm(a) == want for a in attractions):
            continue                                                       # fuzzy keyword hit, not this artist
        start = (ev.get("dates") or {}).get("start") or {}
        status = ((ev.get("dates") or {}).get("status") or {}).get("code", "")
        if not start.get("localDate") or status == "cancelled" or not str(ev.get("url", "")).startswith(("https://", "http://")):
            continue
        venues = (ev.get("_embedded") or {}).get("venues") or [{}]
        venue, city = venues[0].get("name", ""), (venues[0].get("city") or {}).get("name", "")
        out.append({"external_id": ev["id"], "artist": artist, "title": name, "subtitle": ", ".join(x for x in (venue, city) if x),
                    "url": ev["url"], "date": start["localDate"],
                    "meta": {"time": (start.get("localTime") or "")[:5], "venue": venue, "city": city, "status": status,
                             "tribute": looks_like_tribute(ev, attractions_raw, artist)}})
    return out


class RadarWidget(Widget):
    id = "radar"
    title = "Gig radar"
    interval = 24 * 3600
    daily_at = "05:00"
    min_interval = 6 * 3600
    timeout = 600
    secret_keys = ("api_key",)
    defaults = {"api_key": "", "latlong": "53.4808,-2.2426", "radius_miles": 50, "batch": 200}       # default: Manchester

    def configured(self, settings: dict[str, Any]) -> bool:
        return bool(str(settings.get("api_key", "")).strip())

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "latlong" in new:
            m = LATLONG_RE.match(str(new["latlong"]))
            if not (m and -90 <= float(m.group(1)) <= 90 and -180 <= float(m.group(2)) <= 180):
                raise ValueError("location must look like 53.48,-2.24 (latitude, longitude)")
            merged["latlong"] = f"{m.group(1)},{m.group(2)}"
        if "radius_miles" in new and not (isinstance(new["radius_miles"], int) and 5 <= new["radius_miles"] <= 500):
            raise ValueError("radius must be a whole number of miles from 5 to 500")
        if "batch" in new and not (isinstance(new["batch"], int) and 5 <= new["batch"] <= 300):
            raise ValueError("batch must be a whole number from 5 to 300")
        if "api_key" in new and new["api_key"] and not re.fullmatch(r"[A-Za-z0-9]{16,64}", str(new["api_key"]).strip()):
            raise ValueError("that doesn't look like a Ticketmaster API key")
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        checked = added = 0
        async with httpx.AsyncClient(timeout=30) as client:
            min_age = content.FORCED_SECONDS if settings.get("_forced") else content.FRESH_SECONDS
            for a in content.pick_artists("radar_checked_at", settings["batch"], min_age=min_age):
                await asyncio.sleep(0.3)                                   # well under Ticketmaster's 5 requests/second
                resp = await client.get(URL, params={"apikey": settings["api_key"].strip(), "keyword": a["name"], "classificationName": "music",
                                                     "latlong": settings["latlong"], "radius": settings["radius_miles"], "unit": "miles",
                                                     "sort": "date,asc", "size": 20})
                if resp.status_code in (401, 403):
                    raise RuntimeError("Ticketmaster rejected the API key — check it in Settings")
                if resp.status_code == 429:
                    raise RuntimeError("Ticketmaster rate limit hit — will retry next sync")
                resp.raise_for_status()
                body = resp.json()
                if not isinstance(body, dict) or ("page" not in body and "_embedded" not in body):     # Ticketmaster always includes 'page': its absence means the API changed
                    log.warning("Ticketmaster's answer for %s had an unexpected shape (keys: %s) — has its API changed?", a["name"], ", ".join(sorted(body)) if isinstance(body, dict) else type(body).__name__)
                added += content.upsert_items("radar", "ticketmaster", parse_ticketmaster(body, a["name"]), baseline=a["radar_checked_at"] is None)
                content.mark_checked(a["id"], "radar_checked_at")
                checked += 1
        content.prune("radar", older_than_days=30)
        return {"checked": checked, "new": added, "pending": content.count_stale("radar_checked_at", min_age)}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        return f"checked {d.get('checked', 0)} artists, {d.get('new', 0)} new" + (f", {d['pending']} still to do" if d.get("pending") else "")

    def retry_after(self, data: dict[str, Any] | None) -> int | None:
        return 20 if data and data.get("pending") else None          # still working through the artists: next batch shortly

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        today = now.date().isoformat()
        # A past gig is no use on a radar, and packages already stored from before the filter existed stay hidden.
        # Soonest show first (like Next Gig's own countdown) — the one exception besides Releases to the newest-first rule.
        out = content.list_items("radar", oldest_first=True, keep=lambda i: (i["date"] or "") >= today and not is_premium_package(i["title"]))
        return {**out, "radius": settings.get("radius_miles"), "following": len(content.list_artists())}


widget = RadarWidget()
