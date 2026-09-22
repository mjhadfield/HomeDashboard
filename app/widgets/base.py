"""The widget contract.

A widget is a module in this package exposing a `widget` instance. To add one, drop in a file — it is
discovered automatically (see __init__.py). The scheduler calls `fetch()` on `interval`; the result is
cached in the database so the dashboard renders instantly and keeps showing the last good data if the
source goes down. `view()` derives what the browser needs from the cached data at request time (e.g.
"days until", which changes daily without any new fetch).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any


class Widget:
    id: str = ""
    title: str = ""
    interval: int = 600          # seconds between refreshes ("every" schedule)
    daily_at: str | None = None  # "HH:MM" local time: run once a day at that time instead of on an interval
    min_interval: int = 60       # the shortest "every" schedule Settings will accept: a floor that protects other people's services
    local: bool = False          # True = talks to nothing outside this machine (shown as such in Settings)
    log_success: bool = True     # write "synced in 41s" to the event log (failures are always logged)
    kind: str = "sync"           # 'sync' (a data source) or 'maintenance' (housekeeping)
    timeout: int = 45            # a fetch running longer than this is abandoned
    defaults: dict[str, Any] = {}
    secret_keys: tuple[str, ...] = ()   # setting keys that must never be sent back to the browser

    def configured(self, settings: dict[str, Any]) -> bool:
        """False -> the scheduler skips fetching and the card shows a 'not set up' state."""
        return True

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def secrets(self, settings: dict[str, Any]) -> list[str]:
        """Secret values (API keys, private calendar addresses) — scrubbed from anything written to the log or shown as an error."""
        return [str(settings[k]) for k in self.secret_keys if isinstance(settings.get(k), str) and settings.get(k)]

    def summarise(self, data: dict[str, Any] | None) -> str:
        """A few words on what a run achieved, for the schedule table and the log."""
        return ""

    def retry_after(self, data: dict[str, Any] | None) -> int | None:
        """Seconds until the next run, if sooner than `interval` — e.g. while working through a backlog."""
        return None

    def public_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """What the settings UI may see: secrets are never sent to the browser."""
        out = {k: v for k, v in settings.items() if k not in self.secret_keys}
        for key in self.secret_keys:
            out[f"has_{key}"] = bool(settings.get(key))
        return out

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        """Validate and merge an update from the UI. Raise ValueError for anything unacceptable."""
        if unknown := set(new) - set(self.defaults):
            raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        new = {k: v for k, v in new.items() if not (k in self.secret_keys and v in ("", None))}   # blank secret = keep the stored one
        return {**old, **new}

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        return data or {}
