"""This season's Formula 1 calendar, drivers and teams — what the News page's spoiler shield (spoilers.py) runs on.

From the Jolpica API (the community successor to Ergast; free, no key, asks for no more than a few requests a second):
three small requests once a day. If a fetch fails the last good calendar stays in use; if it's more than two weeks old
the shield stops trusting it and hides every F1-looking story instead.
"""
from __future__ import annotations

import calendar
import re
import time
from datetime import datetime
from typing import Any

import httpx

from .. import __version__, config
from . import news
from .base import Widget

BASE = "https://api.jolpi.ca/ergast/f1/current"
# Sessions that can produce a result someone could spoil, in the order a weekend runs them.
COMPETITIVE = [("SprintQualifying", "Sprint qualifying"), ("SprintShootout", "Sprint shootout"), ("Sprint", "Sprint"), ("Qualifying", "Qualifying")]


def _epoch(session: dict[str, Any] | None) -> int | None:
    if not session or not session.get("date"):
        return None
    t = (session.get("time") or "00:00:00Z").rstrip("Z")
    try:
        return calendar.timegm(time.strptime(f"{session['date']} {t[:8]}", "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return None


def parse_races(races: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in races:
        try:
            rnd = int(r["round"])
        except (KeyError, ValueError):
            continue
        race = _epoch({"date": r.get("date"), "time": r.get("time")})
        firsts = [(e, label) for key, label in COMPETITIVE if (e := _epoch(r.get(key)))]
        first, first_label = min(firsts) if firsts else (race, "Race")
        loc = (r.get("Circuit") or {}).get("Location") or {}
        name = r.get("raceName") or f"Round {rnd}"
        short = re.sub(r"\s+Grand Prix.*$", "", name).strip()
        # The city and the circuit, not the country: hiding every "United States" story for a weekend would be too blunt
        # (the race itself is already caught as "… Grand Prix" / "… GP").
        venues = [v for v in {loc.get("locality"), (r.get("Circuit") or {}).get("circuitName")} if v]
        out.append({"round": rnd, "name": name, "short": f"{short} GP" if short != name else name, "race": race, "first_session": first,
                    "first_label": first_label, "sprint": any(k in r for k in ("Sprint", "SprintQualifying", "SprintShootout")), "venues": sorted(venues)})
    return sorted(out, key=lambda x: x["round"])


class F1CalendarWidget(Widget):
    id = "f1cal"
    title = "F1 calendar"
    daily_at = "05:30"
    timeout = 60

    def configured(self, settings: dict[str, Any]) -> bool:
        return bool(news.load().get("f1_shield", True))

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}, timeout=20, follow_redirects=True) as client:
            async def table(path: str, key: str, inner: str) -> list[dict[str, Any]]:
                resp = await client.get(f"{BASE}/{path}/?format=json&limit=100")
                resp.raise_for_status()
                return resp.json()["MRData"][key][inner]
            races = await table("races", "RaceTable", "Races")
            drivers = await table("drivers", "DriverTable", "Drivers")
            teams = await table("constructors", "ConstructorTable", "Constructors")
        rounds = parse_races(races)
        if not rounds:
            raise ValueError("the calendar came back empty")
        season = int(races[0].get("season") or datetime.now(config.TZ).year)
        driver_names = sorted({n for d in drivers for n in (d.get("familyName"), f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()) if n})
        team_names = sorted({re.sub(r"\s+F1 Team$", "", t.get("name", "")).strip() for t in teams if t.get("name")})
        return {"season": season, "rounds": rounds, "drivers": driver_names, "teams": team_names}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        return f"{d.get('season', '?')} season, {len(d.get('rounds', []))} rounds"

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        d = data or {}
        return {"season": d.get("season"), "rounds": len(d.get("rounds", []))}


widget = F1CalendarWidget()
