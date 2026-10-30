"""The reminder strip's dismissals and switches.

Every alert carries a `key` naming one occurrence of it ("bin:blue:2026-10-29", "f1:2026:21:race"). Dismissing
stores that key, so the same reminder never comes back — but the next bin day or the next race has a new key and
shows as usual. Dismissals are household-wide: the TV honours them too. Rows older than KEEP_DAYS are tidied
nightly (by then every key they could match is long past).
"""
from __future__ import annotations

import time
from typing import Any

from . import db

KEEP_DAYS = 45
LEVELS = {"now": 0, "soon": 1, "info": 2}

# Which of the newer reminder kinds are on (Settings → Notifications). Bins keep their own stream choice in Settings → Bins.
DEFAULTS: dict[str, Any] = {"releases": True, "f1": True, "onsale": True, "fresh": True, "down": True, "top_artists": 50}
KINDS = ("releases", "f1", "onsale", "fresh", "down")


def settings() -> dict[str, Any]:
    return db.get_settings("alerts", DEFAULTS)


def dismiss(key: str) -> None:
    with db.db() as con:
        con.execute("INSERT INTO alert_dismissals (key, dismissed_at) VALUES (?, ?) ON CONFLICT(key) DO NOTHING", (key, int(time.time())))


def undismiss(key: str) -> bool:
    with db.db() as con:
        return con.execute("DELETE FROM alert_dismissals WHERE key = ?", (key,)).rowcount > 0


def dismissed() -> set[str]:
    with db.db() as con:
        return {r["key"] for r in con.execute("SELECT key FROM alert_dismissals")}


def visible(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop what's been dismissed; most urgent first (a stable sort, so each kind keeps its own order)."""
    gone = dismissed()
    return sorted((a for a in alerts if a.get("key") not in gone), key=lambda a: LEVELS.get(a.get("level", "info"), 2))


def prune() -> int:
    with db.db() as con:
        return con.execute("DELETE FROM alert_dismissals WHERE dismissed_at < ?", (int(time.time()) - KEEP_DAYS * 86400,)).rowcount
