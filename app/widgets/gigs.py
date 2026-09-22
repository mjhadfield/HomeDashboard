"""Booked gigs: tickets you have, with a countdown. Entirely local data (edited in Settings) — nothing to fetch."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .. import content
from .base import Widget


class GigsWidget(Widget):
    id = "gigs"
    title = "Gigs"
    interval = 3600
    local = True
    log_success = False
    defaults: dict[str, Any] = {}

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {}

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        today = now.date()
        gigs = []
        for g in content.list_gigs():
            days = (date.fromisoformat(g["date"]) - today).days
            gigs.append({**{k: g[k] for k in ("id", "artist", "venue", "city", "date", "doors", "ticket_url", "notes")}, "in_days": days})
        upcoming = [g for g in gigs if g["in_days"] >= 0]
        past = [g for g in gigs if g["in_days"] < 0][::-1][:12]
        return {"next": upcoming[0] if upcoming else None, "upcoming": upcoming[:30], "past": past, "count": len(upcoming)}


widget = GigsWidget()
