"""Assembles what the browser needs: widget payloads, project status, links, and reminder alerts."""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta
from typing import Any

from . import __version__, appearance, config, db, procs
from .health import Health
from .schemas import TV_DEFAULTS, TV_PANELS
from .widgets import discover
from .widgets.base import Widget


def now() -> datetime:
    return datetime.now(config.TZ)


# ---- widgets -----------------------------------------------------------------------------------

def widget_payload(w: Widget, at: datetime | None = None) -> dict[str, Any]:
    at = at or now()
    settings = db.get_settings(w.id, w.defaults)
    cached = db.cache_get(w.id)
    configured = w.configured(settings)
    return {
        "id": w.id,
        "title": w.title,
        "configured": configured,
        "data": w.view(cached["data"], settings, at) if configured else None,
        "fetched_at": cached["fetched_at"],
        "error": cached["error"],
        "error_at": cached["error_at"],
    }


def widgets_state() -> dict[str, dict[str, Any]]:
    at = now()
    return {wid: widget_payload(w, at) for wid, w in discover().items()}


# ---- projects / links --------------------------------------------------------------------------

DEFAULT_PROJECT_ICON = {"web": "server", "desktop": "play"}   # used for a Launchpad tile until the owner picks one


async def projects_state(health: Health) -> list[dict[str, Any]]:
    with db.db() as con:
        rows = con.execute("SELECT * FROM projects WHERE enabled = 1 ORDER BY pin_order IS NULL, pin_order, sort, name").fetchall()
    try:
        active = await procs.active_units()
    except procs.ProcError:
        active = set()
    try:
        since = await procs.active_since()
    except procs.ProcError:
        since = {}
    ip = config.lan_ip()
    out = []
    for r in rows:
        managed = procs.unit(r["slug"]) in active
        h = health.state.get(f"project:{r['slug']}")
        up = bool(h and h["ok"]) if r["kind"] == "web" else managed
        started = procs.started_at.get(r["slug"])
        if managed and up:
            state = "running"
        elif managed:
            state = "starting" if not started or now().timestamp() - started < 90 else "unhealthy"
        elif up:
            state = "external"   # something else (e.g. run.sh) is serving the port; we can't stop what we didn't start
        else:
            state = "stopped"
        out.append({
            "slug": r["slug"], "name": r["name"], "description": r["description"], "kind": r["kind"],
            "url": r["url"].replace("{ip}", ip) if r["url"] else None,
            "port": r["port"], "lan_visible": bool(r["lan_visible"]),
            "state": state, "managed": managed, "latency_ms": h["ms"] if h and h["ok"] else None,
            "since": since.get(procs.unit(r["slug"])) if managed else None,
            "pinned": r["pin_order"] is not None, "pin_order": r["pin_order"],
            "show_projects": bool(r["show_projects"]), "show_launchpad": bool(r["show_launchpad"]),
            "icon": r["icon"] or DEFAULT_PROJECT_ICON.get(r["kind"], "server"),
            "for_music": bool(r["for_music"]), "for_sql": bool(r["for_sql"]),
        })
    return out


def links_state(health: Health, guest_only: bool = False) -> list[dict[str, Any]]:
    with db.db() as con:
        rows = con.execute(
            "SELECT * FROM links" + (" WHERE show_guest = 1" if guest_only else "") + " ORDER BY pin_order IS NULL, pin_order, tier = 'more', sort, name"
        ).fetchall()
    out = []
    for r in rows:
        h = health.state.get(f"link:{r['id']}")
        out.append({
            "id": r["id"], "name": r["name"], "subtitle": r["subtitle"], "url": r["url"], "icon": r["icon"],
            "primary": bool(r["is_primary"]), "tier": r["tier"], "pinned": r["pin_order"] is not None, "pin_order": r["pin_order"],
            "for_media": bool(r["for_media"]), "for_music": bool(r["for_music"]), "for_sql": bool(r["for_sql"]),
            "for_ai": bool(r["for_ai"]),
            "up": (h["ok"] if h else None),   # None = no health check configured / not probed yet
        })
    return out


# ---- alerts ------------------------------------------------------------------------------------

BIN_NAMES = {"domestic": "Domestic", "blue": "Blue", "brown": "Brown", "food": "Food & garden"}


def compute_alerts(widgets: dict[str, dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    """Reminders that belong in the top strip. Bin day is the main one: 'out tonight' from the evening before."""
    alerts: list[dict[str, Any]] = []

    bins = widgets.get("bins", {})
    if bins.get("data"):
        settings = db.get_settings("bins", {"put_out_hour": 17, "alert_streams": ["domestic", "blue", "brown"]})
        for item in bins["data"]["next"].values():
            if not item or item["stream"] not in settings["alert_streams"]:
                continue
            name = BIN_NAMES.get(item["stream"], item["stream"])
            what = "recycling" if item["stream"] in ("blue", "brown") else "domestic waste"
            if item["in_days"] == 1 and at.hour >= settings["put_out_hour"]:
                alerts.append({"kind": "bin", "icon": "bin", "title": f"{name} bin out tonight",
                               "detail": f"{what} collected tomorrow", "chip": "Tonight", "level": "now"})
            elif item["in_days"] == 0 and at.hour < 12:
                alerts.append({"kind": "bin", "icon": "bin", "title": f"{name} bin — collection today",
                               "detail": f"{what} collected this morning", "chip": "Today", "level": "now"})

    gigs = widgets.get("gigs", {})
    if gigs.get("data") and gigs["data"].get("next"):
        g = gigs["data"]["next"]
        if g["in_days"] in (0, 1):
            where = ", ".join(x for x in (g["venue"], g["city"]) if x)
            detail = " · ".join(x for x in (where, f"doors {g['doors']}" if g["doors"] else "") if x) or "gig"
            alerts.append({"kind": "gig", "icon": "bolt", "title": g["artist"] if g["in_days"] else f"{g['artist']} tonight",
                           "detail": detail, "chip": "Tonight" if g["in_days"] == 0 else "Tomorrow", "level": "now" if g["in_days"] == 0 else "soon"})

    diary = widgets.get("diary", {})
    if diary.get("data"):
        for e in diary["data"]["events"]:
            if e["all_day"] or e["date"] != at.date().isoformat() or not e["time"]:
                continue
            start = at.replace(hour=int(e["time"][:2]), minute=int(e["time"][3:]), second=0, microsecond=0)
            if timedelta(0) <= start - at <= timedelta(hours=3):
                mins = int((start - at).total_seconds() // 60)
                alerts.append({"kind": "event", "icon": "cal", "title": e["title"],
                               "detail": e["location"] or e["time"], "chip": f"in {mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"in {mins}m", "level": "soon"})
                break

    vinyl = widgets.get("vinyl", {})
    if vinyl.get("data"):
        for p in vinyl["data"].get("preorders", []):
            if p["in_days"] is None or not (0 <= p["in_days"] <= 7):
                continue
            chip = "Today" if p["in_days"] == 0 else "Tomorrow" if p["in_days"] == 1 else f"in {p['in_days']} days"
            variant = (p.get("meta") or {}).get("variant") or (p.get("meta") or {}).get("format") or "pre-order"
            alerts.append({"kind": "vinyl", "icon": "disc", "title": f"{p['artist']} — {p['title']}" if p["artist"] else p["title"],
                           "detail": variant, "chip": chip, "level": "now" if p["in_days"] == 0 else "soon"})
    return alerts


# ---- the full owner snapshot -------------------------------------------------------------------

async def owner_state(health: Health, device: dict[str, Any], attention: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    at = now()
    widgets = widgets_state()
    return {
        "now": at.isoformat(),
        "host": socket.gethostname(),
        "ip": config.lan_ip(),
        "version": __version__,
        "device": {"id": device["id"], "name": device["name"], "role": device["role"]},
        "widgets": widgets,
        "projects": await projects_state(health),
        "links": links_state(health),
        "health": health.state,
        "alerts": [*compute_alerts(widgets, at), *(attention or [])],      # owner only: a sync that keeps failing is a thing to know about
        "appearance": {"themes": appearance.themes()},
    }


# ---- screens (TV / kiosk) ------------------------------------------------------------------------

def tv_settings() -> dict[str, Any]:
    """Which panels a screen may show. Panels are opt-in: the calendar is private and guests can see a TV."""
    raw = db.get_settings("tv", TV_DEFAULTS)
    return {
        "panels": [p for p in raw.get("panels", []) if p in TV_PANELS],
        "rotate_seconds": max(5, min(300, int(raw.get("rotate_seconds", 12)))),
        "show_alerts": bool(raw.get("show_alerts", True)),
    }


def kiosk_state() -> dict[str, Any]:
    """Everything a TV may see — nothing more. Widgets it isn't allowed to show are simply absent."""
    at = now()
    tv = tv_settings()
    allowed = set(tv["panels"])
    all_widgets = widgets_state()
    alerts = []
    if tv["show_alerts"]:
        for a in compute_alerts(all_widgets, at):
            if (a["kind"] == "bin" and "bins" in allowed) or (a["kind"] == "event" and "diary" in allowed) or (a["kind"] == "gig" and "gigs" in allowed):
                alerts.append(a)
    return {
        "now": at.isoformat(),
        "tv": tv,
        "widgets": {wid: w for wid, w in all_widgets.items() if wid in allowed},
        "alerts": alerts,
    }
