"""Diary: upcoming events from Google Calendar (or any calendar) via its secret iCal address.

The iCal address is read-only and needs no OAuth, but anyone holding it can read the calendar, so it is
treated as a secret: stored only in the (0600) database, never sent back to the browser, never logged.
Google can lag a few hours refreshing these feeds — that is a Google behaviour, not ours.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import date, datetime, timedelta
from typing import Any

import httpx
import icalendar
import recurring_ical_events

from .. import config
from .base import Widget

MAX_EVENTS = 400


def _as_local(value: datetime) -> datetime:
    return (value if value.tzinfo else value.replace(tzinfo=config.TZ)).astimezone(config.TZ)


def expand(content: bytes, cal: dict[str, Any], start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Expand recurring events in [start, end) into flat, JSON-ready dicts. CPU-bound: run in a thread."""
    calendar = icalendar.Calendar.from_ical(content)
    today = start.astimezone(config.TZ).date()
    out: list[dict[str, Any]] = []
    for ev in recurring_ical_events.of(calendar).between(start, end):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        dtstart = ev["DTSTART"].dt
        dtend = ev["DTEND"].dt if "DTEND" in ev else None
        all_day = not isinstance(dtstart, datetime)
        if all_day:
            first = dtstart
            last = (dtend - timedelta(days=1)) if isinstance(dtend, date) and dtend > dtstart else dtstart
            time_, end_time = None, None
        else:
            local = _as_local(dtstart)
            first, last = local.date(), local.date()
            time_ = local.strftime("%H:%M")
            end_time = _as_local(dtend).strftime("%H:%M") if isinstance(dtend, datetime) else None
        out.append({
            "date": max(first, today).isoformat(),          # an event already under way shows from today
            "until": last.isoformat() if last > first else None,
            "time": time_,
            "end_time": end_time,
            "all_day": all_day,
            "title": str(ev.get("SUMMARY") or "(no title)").strip()[:120],
            "location": str(ev.get("LOCATION") or "").split("\n")[0].strip()[:80],
            "cal": cal.get("name", "Calendar"),
            "colour": cal.get("colour", "violet"),
        })
    return out


class DiaryWidget(Widget):
    id = "diary"
    title = "Diary"
    interval = 30 * 60           # one small request per calendar; changes are worth seeing within the half hour
    min_interval = 5 * 60
    log_success = False
    secret_keys = ("calendars",)
    defaults = {"calendars": [], "horizon_days": 60}

    def secrets(self, settings: dict[str, Any]) -> list[str]:
        return [c["url"] for c in settings.get("calendars", []) if c.get("url")]

    def configured(self, settings: dict[str, Any]) -> bool:
        return any(c.get("url") for c in settings.get("calendars", []))

    @staticmethod
    def _cid(cal: dict[str, Any], index: int) -> str:
        """A calendar's stable id. Calendars stored before ids existed get a positional one, so they keep working."""
        return cal.get("id") or f"cal{index}"

    def public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        cals = [{"id": self._cid(c, i), "name": c.get("name", ""), "colour": c.get("colour", "violet"), "has_url": bool(c.get("url"))}
                for i, c in enumerate(settings.get("calendars", []))]
        return {**{k: v for k, v in settings.items() if k != "calendars"}, "calendars": cals}

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "calendars" in new:
            if not isinstance(new["calendars"], list) or len(new["calendars"]) > 8:
                raise ValueError("calendars must be a list of at most 8")
            # Match by id, never by name: renaming a calendar must not lose its secret address.
            stored = {self._cid(c, i): c.get("url") for i, c in enumerate(old.get("calendars", []))}
            cals = []
            for c in new["calendars"]:
                url = (c.get("url") or "").strip() or stored.get(c.get("id"))     # omitted url = keep the stored secret
                if not url:
                    raise ValueError("every calendar needs its secret iCal address")
                if not str(url).startswith(("https://", "http://")):
                    raise ValueError("calendar url must be http(s)")
                colour = c.get("colour", "violet")
                if colour not in ("violet", "silver", "indigo"):
                    raise ValueError("colour must be violet, silver or indigo")
                cals.append({"id": c.get("id") or secrets.token_hex(4), "name": str(c.get("name") or "Calendar")[:40], "colour": colour, "url": url})
            merged["calendars"] = cals
        if "horizon_days" in new:
            horizon = new["horizon_days"]
            if not isinstance(horizon, int) or isinstance(horizon, bool) or not 7 <= horizon <= 365:
                raise ValueError("horizon_days must be a whole number from 7 to 365")
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        start = datetime.now(config.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=int(settings.get("horizon_days", 60)))
        events: list[dict[str, Any]] = []
        calendars = [c for c in settings["calendars"] if c.get("url")]
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for cal in calendars:
                resp = await client.get(cal["url"])
                resp.raise_for_status()
                events += await asyncio.to_thread(expand, resp.content, cal, start, end)
        seen, unique = set(), []
        for e in sorted(events, key=lambda e: (e["date"], e["time"] or "", e["title"])):
            key = (e["date"], e["time"], e["title"], e["cal"])
            if key not in seen:
                seen.add(key)
                unique.append(e)
        return {
            "events": unique[:MAX_EVENTS],
            "calendars": [{"name": c.get("name", "Calendar"), "colour": c.get("colour", "violet")} for c in calendars],
        }

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        if not data:
            return {}
        today = now.date().isoformat()
        return {**data, "events": [e for e in data["events"] if (e["until"] or e["date"]) >= today]}


widget = DiaryWidget()
