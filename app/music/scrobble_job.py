"""Scheduled Last.fm scrobble import for Music Companion -- Settings → Maintenance's "Last.fm scrobbles" row.

Citadel doesn't import anything itself: it asks Music Companion's maintenance server (its own project, port 8643) to run
the same incremental Last.fm pull as its manual Refresh button, then reports what arrived. The maintenance server accepts
the import as it lands -- anything new (artists, albums, songs) goes to its import Inbox for review -- and takes a safety
snapshot first. It politely declines while it's busy (a manual refresh, a sweep, a publish); the job then tries again
sooner rather than counting a failure.

Every 2 hours by default (at least 1): an incremental pull is one Last.fm request per 200 new plays, far inside Last.fm's
limits, and the MusicBrainz lookups it makes depend on how much new music you play, not on how often it runs.
The maintenance server's address and password live on the Music Companion connection (Settings → Music page).
"""
from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..widgets.base import Widget
from . import store

DEFAULT_URL = "http://127.0.0.1:8643"
POLL_SECONDS = 3.0
RETRY_WHEN_BUSY = 15 * 60


class ScrobbleJob(Widget):
    id = "music-lastfm"
    title = "Last.fm scrobbles"
    interval = 2 * 3600
    min_interval = 3600
    kind = "sync"
    timeout = 30 * 60              # a big catch-up (days of plays + new editions to look up) can take a while
    setup_hint = "Add the maintenance password in Settings → Music page → Music services → Music Companion"
    transport: httpx.AsyncBaseTransport | None = None   # tests swap in a fake maintenance server

    @staticmethod
    def _cfg() -> dict[str, Any]:
        return store.service_cfg("companion")

    def configured(self, settings: dict[str, Any]) -> bool:
        return bool(self._cfg().get("maintenance_password"))

    def secrets(self, settings: dict[str, Any]) -> list[str]:
        pw = self._cfg().get("maintenance_password")
        return [str(pw)] if pw else []

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        cfg = self._cfg()
        base = (cfg.get("maintenance_url") or DEFAULT_URL).rstrip("/")
        auth = ("citadel", str(cfg.get("maintenance_password") or ""))
        async with httpx.AsyncClient(timeout=60, auth=auth, transport=self.transport) as client:
            r = await client.post(f"{base}/api/scheduled/lastfm", json={})
            if r.status_code == 401:
                raise RuntimeError("the Music Companion maintenance server didn't accept the password — update it in Settings → Music page → Music Companion")
            r.raise_for_status()
            started = r.json()
            if started.get("skipped"):
                return {"skipped": started["skipped"]}
            job = started["jobId"]
            while True:
                await asyncio.sleep(POLL_SECONDS)
                res = await client.get(f"{base}/api/scheduled/lastfm", params={"jobId": job})
                if res.status_code == 404:
                    raise RuntimeError("the maintenance server restarted while the import was running — it'll be picked up next time")
                res.raise_for_status()
                out = res.json()
                if out.get("done"):
                    break
        if out.get("returncode"):
            tail = [x for x in out.get("tail") or [] if x.strip()]
            raise RuntimeError(f"the import failed{': ' + tail[-1][:200] if tail else ''}")
        s = out.get("summary") or {}
        return {"imported": s.get("imported", 0), "events": s.get("events") or {}, "lookups": s.get("releasesLookedUp", 0),
                "inbox": out.get("inboxPending", 0)}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        if d.get("skipped"):
            return f"skipped — {d['skipped']}; trying again in {RETRY_WHEN_BUSY // 60} min"
        ev = d.get("events") or {}
        new = [f"{n} new {what}{'s' if n != 1 else ''}" for what, n in (("artist", ev.get("new_artist", 0)), ("album", ev.get("new_album", 0)),
                                                                      ("song", ev.get("new_song", 0))) if n]
        plays = d.get("imported", 0)
        parts = [f"{plays} new play{'s' if plays != 1 else ''}"] + new
        if d.get("inbox"):
            parts.append(f"inbox: {d['inbox']} to review")
        return " · ".join(parts)

    def retry_after(self, data: dict[str, Any] | None) -> int | None:
        return RETRY_WHEN_BUSY if (data or {}).get("skipped") else None


job = ScrobbleJob()
