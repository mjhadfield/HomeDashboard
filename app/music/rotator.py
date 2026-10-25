"""Rebuilds rotating playlists on their schedule and turns a pre-gig playlist into the real setlist after the show,
asking Spotify at its background pace (and not at all while Spotify has asked Citadel to wait). One instance per app run, on app.state (started and stopped in main.py's lifespan), like Monitor.

What's due is worked out from each playlist's own last save, so a restart neither repeats a rotation nor skips one —
one missed while Citadel was off simply happens when it's back."""
from __future__ import annotations

import asyncio
import logging
import time

from .. import config
from . import discover, playlists, store
from .services import spotify as sp
from .services.base import ServiceError

log = logging.getLogger("hub.music")
TICK = 60.0
BACKOFF = 1800.0                             # a rotation that failed waits this long before trying again
SETLIST_EVERY = 6 * 3600                     # after a gig, look for the real setlist this often


class Rotator:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._failed: dict[int, float] = {}
        self._looked: dict[int, float] = {}
        self.last_check = 0.0

    async def start(self) -> None:
        if config.NO_FETCH:                      # tests / dev: nothing talks to the outside world on its own
            return
        self._task = asyncio.create_task(self._loop(), name="music-rotator")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await discover.stop()

    async def _loop(self) -> None:
        await asyncio.sleep(20)                  # let the rest of Citadel start first
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("music rotator failed")
            await asyncio.sleep(TICK)

    async def tick(self, now: float | None = None) -> list[str]:
        self.last_check = now or time.time()
        if not sp.service.ready() or sp.resting_until():
            return []
        with sp.background():
            return await self._tick(now)

    async def _tick(self, now: float | None) -> list[str]:
        done = []
        for pl in playlists.all_playlists():
            if pl["mode"] == "rot" and pl["meta"].get("gig") and self.last_check - self._looked.get(pl["id"], 0) >= SETLIST_EVERY:
                if pl["meta"]["gig"].get("date", "9999") < time.strftime("%Y-%m-%d"):
                    self._looked[pl["id"]] = self.last_check
                try:
                    if await playlists.after_gig(pl):
                        log.info("Music: %s now has the real setlist (or stopped waiting for one)", pl["name"])
                        done.append(pl["name"])
                        continue
                except (ServiceError, LookupError, playlists.PlaylistError) as e:
                    log.warning("Music: couldn’t check %s’s real setlist: %s", pl["name"], e)
        for pl in playlists.due(now):
            if self.last_check - self._failed.get(pl["id"], 0) < BACKOFF:
                continue
            try:
                r = await playlists.rotate(pl["id"])
                log.info("Music: rotated %s — %s tracks%s", pl["name"], r["tracks"], f", {r['skipped']} not on Spotify" if r.get("skipped") else "")
                done.append(pl["name"])
                self._failed.pop(pl["id"], None)
            except Exception as e:               # tried again after BACKOFF; a broken one is visible on the page and in the log
                self._failed[pl["id"]] = self.last_check
                log.warning("Music: couldn’t rotate %s: %s", pl["name"], e)
        if done:
            store.changed("playlists")
        return done
