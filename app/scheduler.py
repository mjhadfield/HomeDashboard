"""Runs each widget's fetch() (and the maintenance jobs) on a schedule, caching results and surviving failures.

Each job has a schedule: "daily at HH:MM" or "every N seconds". What is *due* is worked out from the persisted record of
when it last succeeded (`job_state`), not from when the service started — so a restart doesn't re-run everything and
hammer other people's services, and a job that was missed while the service was down simply runs when it comes back.

After a failure a job retries with a growing pause (5 min, 15, 30, 60, then every 2 hours) rather than every couple of
minutes, so an outage heals on its own without hammering. A job with a backlog (artists not yet looked at) asks to be
called back sooner via `retry_after()` until the backlog is gone.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, time as dtime, timedelta
from typing import Any

from . import config, db, logbook
from .widgets.base import Widget

log = logging.getLogger("hub.scheduler")

BACKOFF = (300, 900, 1800, 3600, 7200)      # pause after the 1st, 2nd, 3rd… consecutive failure
STAGGER = 15.0                              # seconds between jobs that are already overdue at start-up
TICK = 60.0                                 # a sleeping job re-checks its schedule this often (so Settings edits apply without a restart)


def parse_hhmm(text: Any) -> tuple[int, int] | None:
    try:
        h, m = str(text).split(":")
        h, m = int(h), int(m)
    except ValueError:
        return None
    return (h, m) if len(str(text)) == 5 and 0 <= h <= 23 and 0 <= m <= 59 else None


def schedule_of(w: Widget, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The schedule in force: the user's override if it is valid, else the widget's own default."""
    ov = (overrides or {}).get(w.id)
    if isinstance(ov, dict):
        if ov.get("mode") == "daily" and parse_hhmm(ov.get("at")):
            return {"mode": "daily", "at": ov["at"]}
        if ov.get("mode") == "every" and isinstance(ov.get("seconds"), int) and ov["seconds"] >= w.min_interval:
            return {"mode": "every", "seconds": ov["seconds"]}
    if w.daily_at and parse_hhmm(w.daily_at):
        return {"mode": "daily", "at": w.daily_at}
    return {"mode": "every", "seconds": w.interval}


def next_slot(after: float, at: str, tz=config.TZ) -> float:
    """The first local HH:MM strictly after `after` (epoch seconds). Built from calendar dates so daylight-saving changes are safe."""
    h, m = parse_hhmm(at) or (0, 0)
    d = datetime.fromtimestamp(after, tz)
    slot = datetime.combine(d.date(), dtime(h, m), tzinfo=tz)
    if slot <= d:
        slot = datetime.combine(d.date() + timedelta(days=1), dtime(h, m), tzinfo=tz)
    return slot.timestamp()


def due_at(w: Widget, sched: dict[str, Any], state: dict[str, Any] | None, data: dict[str, Any] | None, tz=config.TZ) -> float:
    """Epoch seconds at which this job is next due (0 = right now)."""
    if not state or state.get("last_run") is None:
        return 0.0                                                   # never run
    if state.get("streak"):
        return state["last_run"] + BACKOFF[min(state["streak"], len(BACKOFF)) - 1]
    if (soon := w.retry_after(data)):
        return state["last_run"] + soon                              # still working through a backlog
    base = state.get("last_ok") or state["last_run"]
    if sched["mode"] == "daily":
        return next_slot(base, sched["at"], tz)
    return base + sched["seconds"]


def describe(sched: dict[str, Any]) -> str:
    if sched["mode"] == "daily":
        return f"daily at {sched['at']}"
    s = sched["seconds"]
    return f"every {s // 3600} h" if s % 3600 == 0 else f"every {s // 60} min" if s % 60 == 0 and s >= 120 else f"every {s} s"


class Scheduler:
    def __init__(self, widgets: dict[str, Widget], on_update: Callable[[str], None]) -> None:
        self.widgets = widgets
        self.on_update = on_update
        self._tasks: list[asyncio.Task] = []
        self._locks = {wid: asyncio.Lock() for wid in widgets}       # one run of a widget at a time
        self._queued: set[str] = set()                               # manual refreshes waiting behind a running one
        self._not_before: dict[str, float] = {}
        self.running: set[str] = set()

    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(w, i), name=f"widget:{w.id}") for i, w in enumerate(self.widgets.values())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ---- timing -----------------------------------------------------------------------------------------------

    def schedule(self, w: Widget) -> dict[str, Any]:
        return schedule_of(w, db.get_settings("_schedule"))

    def due_at(self, w: Widget) -> float:
        due = due_at(w, self.schedule(w), db.job_get(w.id), db.cache_get(w.id)["data"])
        return max(due, self._not_before.get(w.id, 0.0))

    async def _loop(self, w: Widget, index: int) -> None:
        await asyncio.sleep(0.2)                                    # let the server finish starting
        if self.due_at(w) <= time.time():                           # overdue at start-up: queue up politely rather than all at once
            self._not_before[w.id] = time.time() + STAGGER * index
        while True:
            wait = self.due_at(w) - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, TICK))
                continue
            try:
                await self.run_once(w)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a scheduler bug must not silently end this job's loop
                log.exception("scheduler error while running %s", w.id, extra={"source": w.id})
                await asyncio.sleep(TICK)

    # ---- running ----------------------------------------------------------------------------------------------

    async def run_once(self, w: Widget, *, forced: bool = False) -> bool:
        # Serialised per widget: a scheduled run and a "sync now" must never overlap, or whichever finishes last would
        # overwrite the other's result (a real failure could be hidden behind a stale success).
        async with self._locks[w.id]:
            return await self._run(w, forced)

    async def _run(self, w: Widget, forced: bool = False) -> bool:
        settings = db.get_settings(w.id, w.defaults)
        logbook.learn(w.secrets(settings))
        if config.NO_FETCH or not w.configured(settings):
            db.job_finished(w.id, ok=True, seconds=0, message="fetching disabled" if config.NO_FETCH else "not set up")
            self.on_update(w.id)
            return True
        prior = db.job_get(w.id)
        started = time.monotonic()
        self.running.add(w.id)
        try:
            data = await asyncio.wait_for(w.fetch({**settings, "_forced": forced}), timeout=w.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # any failure keeps the last good data and is shown as a stale badge
            reason = str(exc) or (f"took longer than {w.timeout} s" if isinstance(exc, TimeoutError) else "")
            message = logbook.redact(f"{type(exc).__name__}: {reason}".rstrip(": "))
            db.cache_error(w.id, message)
            streak = db.job_finished(w.id, ok=False, seconds=time.monotonic() - started, message=message)
            log.warning("%s sync failed (attempt %d in a row): %s", w.title, streak, message, extra={"source": w.id})
            self.on_update(w.id)
            return False
        finally:
            self.running.discard(w.id)
        db.cache_put(w.id, data)
        try:
            summary = w.summarise(data)
        except Exception:  # noqa: BLE001
            summary = ""
        seconds = time.monotonic() - started
        db.job_finished(w.id, ok=True, seconds=seconds, message=summary)
        if prior and prior.get("streak"):
            log.info("%s is working again after %d failed attempt%s", w.title, prior["streak"], "" if prior["streak"] == 1 else "s", extra={"source": w.id, "keep": True})
        elif w.log_success:
            log.info("%s synced in %.0f s%s", w.title, seconds, f" — {summary}" if summary else "", extra={"source": w.id, "keep": True})
        self.on_update(w.id)
        return True

    def refresh(self, widget_id: str) -> None:
        """"Sync now": queue one forced run (repeated requests while one is already waiting collapse into it). It starts once
        any run in progress has finished, so it always uses the latest settings."""
        w = self.widgets.get(widget_id)
        if not w or widget_id in self._queued:
            return
        self._queued.add(widget_id)

        async def go() -> None:
            try:
                await self._locks[widget_id].acquire()
                self._queued.discard(widget_id)                       # from here on a new request may queue another run
                try:
                    await self._run(w, forced=True)
                finally:
                    self._locks[widget_id].release()
            finally:
                self._queued.discard(widget_id)

        asyncio.create_task(go())
