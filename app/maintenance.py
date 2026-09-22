"""Housekeeping that runs on the same scheduler as the data syncs: a nightly backup, tidying old data, and a daily check
that every integration is still working. Also the read/write side of Settings → Maintenance (the schedule table).

The daily check is the "did something quietly break?" net. A sync that *fails* is logged the moment it fails; this catches
the slower kinds of breakage — a job that hasn't succeeded for far longer than its schedule allows, or a run of failures.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, content, db, logbook
from .scheduler import Scheduler, describe, due_at, parse_hhmm, schedule_of
from .widgets import discover
from .widgets.base import Widget

log = logging.getLogger("hub.maintenance")

KEEP_BACKUPS = 7
MIN_FREE_BYTES = 1_000_000_000


# ---- the jobs -------------------------------------------------------------------------------------------------

class Job(Widget):
    kind = "maintenance"
    local = True
    interval = 24 * 3600
    min_interval = 3600
    timeout = 300


def _backup_dir() -> Path:
    d = config.DATA_DIR / "backups"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _quick_check(path: Path) -> str:
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()


def _do_backup(day: str) -> dict[str, Any]:
    """A consistent copy of the database (safe while the app is writing), checked, with old copies trimmed."""
    target = _backup_dir() / f"hub-{day}.sqlite"
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    src, dst = sqlite3.connect(config.DB_PATH), sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    verdict = _quick_check(tmp)
    if verdict != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"the backup failed its integrity check ({verdict[:80]})")
    os.chmod(tmp, 0o600)                                   # it holds the same secrets as the live database
    tmp.replace(target)
    for f in sorted(_backup_dir().glob("hub-*.sqlite"))[:-KEEP_BACKUPS]:
        f.unlink(missing_ok=True)
    return {"file": target.name, "bytes": target.stat().st_size, "kept": len(list(_backup_dir().glob("hub-*.sqlite")))}


class BackupJob(Job):
    id = "maint-backup"
    title = "Database backup"
    daily_at = "02:30"

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(_do_backup, datetime.now(config.TZ).date().isoformat())

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        return f"{d.get('file', '')} ({d.get('bytes', 0) // 1024} KB), keeping the last {KEEP_BACKUPS}"


# Items that haven't been seen for this long are forgotten (pinned ones never are). Widgets also trim as they run; this is the safety net.
PRUNE_DAYS = {"release": 400, "radar": 30, "news": 45, "vinyl": 120}


def _do_tidy() -> dict[str, int]:
    now = int(time.time())
    out = {"log_entries": logbook.prune(), "items": sum(content.prune(kind, older_than_days=days) for kind, days in PRUNE_DAYS.items())}
    with db.db() as con:
        out["expired_links"] = con.execute("DELETE FROM enrol_tokens WHERE expires_at < ? OR used = 1", (now,)).rowcount \
            + con.execute("DELETE FROM pairings WHERE expires_at < ?", (now,)).rowcount
    return out


class TidyJob(Job):
    id = "maint-tidy"
    title = "Tidy old data"
    daily_at = "02:45"

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(_do_tidy)

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        return f"removed {d.get('log_entries', 0)} old log entries, {d.get('items', 0)} old items, {d.get('expired_links', 0)} expired links"


def stale_after(sched: dict[str, Any]) -> float:
    """How long a job may go without succeeding before it counts as stale: a schedule and a half, plus slack."""
    period = 86400 if sched["mode"] == "daily" else sched["seconds"]
    return period * 1.5 + 3600


def _do_check() -> dict[str, Any]:
    """Database health, disk space, and every integration's recent record."""
    problems: list[str] = []
    with db.db() as con:
        verdict = con.execute("PRAGMA quick_check").fetchone()[0]
        con.execute("PRAGMA optimize")
    if verdict != "ok":
        problems.append("database")
        log.error("The database failed its integrity check: %s", verdict[:200])
    con = db.connect()
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()
    free = shutil.disk_usage(config.DATA_DIR).free
    if free < MIN_FREE_BYTES:
        problems.append("disk space")
        log.warning("Only %d MB of disk space is free where the dashboard keeps its data", free // 1_000_000)

    overrides, states, now = db.get_settings("_schedule"), db.job_all(), time.time()
    for w in [*discover().values(), *jobs().values()]:
        st = states.get(w.id)
        if not st or not w.configured(db.get_settings(w.id, w.defaults)):
            continue
        sched = schedule_of(w, overrides)
        if st.get("streak", 0) >= 3:
            problems.append(w.id)
            log.warning("%s has failed %d syncs in a row (since %s): %s", w.title, st["streak"], datetime.fromtimestamp(st["failing_since"] or st["last_run"], config.TZ).strftime("%a %d %b %H:%M"), st["message"], extra={"source": w.id})
        elif st.get("last_ok") and now - st["last_ok"] > stale_after(sched):
            problems.append(w.id)
            log.warning("%s hasn't synced successfully for %d hours (its schedule is %s)", w.title, (now - st["last_ok"]) // 3600, describe(sched), extra={"source": w.id})
    return {"problems": problems}


class CheckJob(Job):
    id = "maint-check"
    title = "Health check"
    daily_at = "06:00"

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(_do_check)

    def summarise(self, data: dict[str, Any] | None) -> str:
        p = (data or {}).get("problems") or []
        return "everything looks healthy" if not p else f"needs attention: {', '.join(p)}"


_jobs: dict[str, Job] = {}


def jobs() -> dict[str, Job]:
    if not _jobs:
        for j in (BackupJob(), TidyJob(), CheckJob()):
            _jobs[j.id] = j
    return _jobs


# ---- what Settings → Maintenance shows -------------------------------------------------------------------------

def overview(scheduler: Scheduler) -> dict[str, Any]:
    overrides, states, now = db.get_settings("_schedule"), db.job_all(), time.time()
    rows = []
    for w in scheduler.widgets.values():
        sched, st = schedule_of(w, overrides), states.get(w.id)
        configured = w.configured(db.get_settings(w.id, w.defaults))
        due = due_at(w, sched, st, db.cache_get(w.id)["data"])
        if w.id in scheduler.running:
            status = "running"
        elif not configured:
            status = "not set up"
        elif not st or st.get("last_run") is None:
            status = "waiting"
        elif st["streak"]:
            status = "failing"
        elif st.get("last_ok") and now - st["last_ok"] > stale_after(sched):
            status = "stale"
        else:
            status = "ok"
        rows.append({
            "id": w.id, "title": w.title, "kind": w.kind, "local": w.local, "status": status, "configured": configured,
            "schedule": sched, "schedule_text": describe(sched), "default_text": describe(schedule_of(w)), "min_interval": w.min_interval,
            "last_run": st and st.get("last_run"), "last_ok": st and st.get("last_ok"), "seconds": st and st.get("last_seconds"),
            "streak": (st or {}).get("streak", 0), "failing_since": st and st.get("failing_since"), "message": (st or {}).get("message", ""),
            "next_run": max(due, scheduler._not_before.get(w.id, 0.0)) if configured else None,
        })
    return {"now": now, "jobs": rows, "log_counts": logbook.counts(), "backups": sorted(p.name for p in _backup_dir().glob("hub-*.sqlite"))}


def set_schedule(scheduler: Scheduler, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Validate and save a schedule override. {"mode": "daily", "at": "04:00"} | {"mode": "every", "seconds": N} | {"mode": "default"}."""
    w = scheduler.widgets.get(job_id)
    if not w:
        raise KeyError(job_id)
    overrides = dict(db.get_settings("_schedule"))
    mode = body.get("mode")
    if mode == "default":
        overrides.pop(job_id, None)
    elif mode == "daily":
        if not parse_hhmm(body.get("at")):
            raise ValueError("choose a time of day like 04:00")
        overrides[job_id] = {"mode": "daily", "at": body["at"]}
    elif mode == "every":
        seconds = body.get("seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds > 30 * 86400:
            raise ValueError("choose how often, as a whole number of seconds")
        if seconds < w.min_interval:
            raise ValueError(f"{w.title} can't sync more often than every {describe({'mode': 'every', 'seconds': w.min_interval}).removeprefix('every ')} — that would be hard on the service it uses")
        overrides[job_id] = {"mode": "every", "seconds": seconds}
    else:
        raise ValueError("mode must be daily, every or default")
    db.put_settings("_schedule", overrides)
    return schedule_of(w, overrides)


def attention(scheduler: Scheduler) -> list[dict[str, Any]]:
    """Alerts for the owner's dashboard strip: a sync that keeps failing, or one that has gone stale. (Not shown on TVs or to guests.)"""
    out = []
    for j in overview(scheduler)["jobs"]:
        if j["status"] == "failing" and j["streak"] >= 2 or j["status"] == "stale":
            out.append({"kind": "maintenance", "icon": "alert", "title": f"{j['title']} needs attention",
                        "detail": (j["message"] or "hasn't synced for a while")[:140], "chip": "Settings", "level": "soon"})
    return out
