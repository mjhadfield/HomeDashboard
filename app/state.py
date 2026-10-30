"""Assembles what the browser needs: widget payloads, project status, links, and reminder alerts."""
from __future__ import annotations

import json
import re
import socket
from datetime import date, datetime, timedelta
from typing import Any

from . import __version__, alerts as alert_settings, appearance, config, content, db, procs
from .health import Health
from .monitor import Monitor, low_disks
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
# Each alert has a `key` naming that one occurrence, so it can be dismissed (app/alerts.py) without hiding the next one.

BIN_NAMES = {"domestic": "Domestic", "blue": "Blue", "brown": "Brown", "food": "Food & garden"}
FULL_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
F1_LEAD = 3 * 3600                                     # "starting soon" from this long before a session…
F1_LIVE = {"Race": 2 * 3600}                           # …and "live" for this long after it starts (others: an hour)
FRESH_FOR = 24 * 3600                                  # a new announcement is news for a day
FRESH_LABELS = {"release": ("New album announced", "new albums announced", "star"),
                "radar": ("New gig on your radar", "new gigs on your radar", "radar"),
                "vinyl": ("New vinyl pre-order", "new vinyl pre-orders", "disc")}
DOWN_AFTER = 10 * 60                                   # a service has to stay unreachable this long to be worth a reminder
MEM_TIGHT_FOR = 2 * 60


def _day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}" + (f" {d.year}" if d.year != now().year else "")


def _in(seconds: float) -> str:
    mins = max(0, int(seconds // 60))
    return f"in {mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"in {mins}m"


def _when(t: datetime, at: datetime) -> str:
    """A chip for something later today or tomorrow: 'in 40m', 'Today 10:00', 'Tomorrow 09:00'."""
    if t - at < timedelta(hours=1):
        return _in((t - at).total_seconds())
    return f"{'Today' if t.date() == at.date() else 'Tomorrow' if t.date() == at.date() + timedelta(days=1) else _day(t.date())} {t:%H:%M}"


def _names(items: list[dict[str, Any]], show: int = 2) -> str:
    names = list(dict.fromkeys(i["artist"] or i["title"] for i in items))
    return ", ".join(names[:show]) + (f" +{len(names) - show}" if len(names) > show else "")


def _items(widgets: dict[str, dict[str, Any]], wid: str) -> list[dict[str, Any]]:
    return ((widgets.get(wid) or {}).get("data") or {}).get("items") or []


def compute_alerts(widgets: dict[str, dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    """Reminders that belong in the top strip. Bin day is the main one: 'out tonight' from the evening before."""
    alerts: list[dict[str, Any]] = []
    on = alert_settings.settings()

    bins = widgets.get("bins", {})
    if bins.get("data"):
        settings = db.get_settings("bins", {"put_out_hour": 17, "alert_streams": ["domestic", "blue", "brown"]})
        for item in bins["data"]["next"].values():
            if not item or item["stream"] not in settings["alert_streams"]:
                continue
            name = BIN_NAMES.get(item["stream"], item["stream"])
            what = "recycling" if item["stream"] in ("blue", "brown") else "domestic waste"
            key = f"bin:{item['stream']}:{item['date']}"          # one key for both: putting the bin out answers tomorrow's too
            if item["in_days"] == 1 and at.hour >= settings["put_out_hour"]:
                alerts.append({"kind": "bin", "key": key, "icon": "bin", "title": f"{name} bin out tonight",
                               "detail": f"{what} collected tomorrow", "chip": "Tonight", "level": "now"})
            elif item["in_days"] == 0 and at.hour < 12:
                alerts.append({"kind": "bin", "key": key, "icon": "bin", "title": f"{name} bin — collection today",
                               "detail": f"{what} collected this morning", "chip": "Today", "level": "now"})

    gigs = widgets.get("gigs", {})
    if gigs.get("data") and gigs["data"].get("next"):
        g = gigs["data"]["next"]
        if g["in_days"] in (0, 1):
            where = ", ".join(x for x in (g["venue"], g["city"]) if x)
            detail = " · ".join(x for x in (where, f"doors {g['doors']}" if g["doors"] else "") if x) or "gig"
            alerts.append({"kind": "gig", "key": f"gig:{g['id']}:{g['date']}", "icon": "bolt", "title": g["artist"] if g["in_days"] else f"{g['artist']} tonight",
                           "detail": detail, "chip": "Tonight" if g["in_days"] == 0 else "Tomorrow", "level": "now" if g["in_days"] == 0 else "soon",
                           "href": g.get("ticket_url") or None})

    diary = widgets.get("diary", {})
    if diary.get("data"):
        for e in diary["data"]["events"]:
            if e["all_day"] or e["date"] != at.date().isoformat() or not e["time"]:
                continue
            start = at.replace(hour=int(e["time"][:2]), minute=int(e["time"][3:]), second=0, microsecond=0)
            if timedelta(0) <= start - at <= timedelta(hours=3):
                alerts.append({"kind": "event", "key": f"event:{e['date']}:{e['time']}:{e['title']}", "icon": "cal", "title": e["title"],
                               "detail": e["location"] or e["time"], "chip": _in((start - at).total_seconds()), "level": "soon"})
                break

    vinyl = widgets.get("vinyl", {})
    if vinyl.get("data"):
        for p in vinyl["data"].get("preorders", []):
            if p["in_days"] is None or not (0 <= p["in_days"] <= 7):
                continue
            chip = "Today" if p["in_days"] == 0 else "Tomorrow" if p["in_days"] == 1 else f"in {p['in_days']} days"
            variant = (p.get("meta") or {}).get("variant") or (p.get("meta") or {}).get("format") or "pre-order"
            alerts.append({"kind": "vinyl", "key": f"vinyl:{p['id']}", "icon": "disc", "title": f"{p['artist']} — {p['title']}" if p["artist"] else p["title"],
                           "detail": variant, "chip": chip, "level": "now" if p["in_days"] == 0 else "soon", "href": p.get("url") or None})

    # Album releases: the day before and the day itself (only albums the Releases card shows, so its type filter applies).
    if on["releases"]:
        for r in _items(widgets, "releases"):
            if not FULL_DATE.match(r["date"] or ""):
                continue
            days = (date.fromisoformat(r["date"]) - at.date()).days
            if days in (0, 1):
                kind = (r.get("meta") or {}).get("type") or "Album"
                alerts.append({"kind": "release", "key": f"release:{r['id']}", "icon": "disc", "title": f"{r['artist']} — {r['title']}",
                               "detail": f"new {kind.lower()} {'out today' if days == 0 else 'out tomorrow'}", "chip": "Out today" if days == 0 else "Tomorrow",
                               "level": "now" if days == 0 else "soon", "href": r.get("url") or None, "done": "Listened"})

    if on["f1"]:
        alerts += _f1_alerts(at)
    if on["onsale"]:
        alerts += _onsale_alerts(widgets, at)
    if on["fresh"]:
        alerts += _fresh_alerts(widgets, at, int(on.get("top_artists") or 50))
    return alerts


def _f1_alerts(at: datetime) -> list[dict[str, Any]]:
    """A competitive session or the race starting within F1_LEAD, or under way. Names only — nothing that could spoil."""
    cal = db.cache_get("f1cal")["data"] or {}
    t, out = at.timestamp(), []
    for rnd in cal.get("rounds", []):
        for sess in rnd.get("sessions", []):
            ahead = sess["at"] - t
            if not -F1_LIVE.get(sess["key"], 3600) <= ahead <= F1_LEAD:
                continue
            start = datetime.fromtimestamp(sess["at"], config.TZ)
            live = ahead <= 0
            out.append({"kind": "f1", "key": f"f1:{cal.get('season')}:{rnd['round']}:{sess['key']}", "icon": "bolt",
                        "title": f"{rnd['short']} — {sess['label']}", "detail": "under way" if live else f"starts {start:%H:%M}",
                        "chip": "Live" if live else _in(ahead), "level": "now" if live or ahead <= 3600 else "soon"})
    return out


def _onsale_alerts(widgets: dict[str, dict[str, Any]], at: datetime) -> list[dict[str, Any]]:
    """Gigs on the radar whose tickets (or a presale) go on sale later today or tomorrow."""
    out = []
    for g in _items(widgets, "radar"):
        meta = g.get("meta") or {}
        if meta.get("tribute"):
            continue
        times = [(meta["onsale"], "tickets on sale")] if meta.get("onsale") else []
        times += [(p["at"], p["name"]) for p in meta.get("presales") or []]
        for iso, what in times:
            try:
                when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(config.TZ)
            except (ValueError, AttributeError):
                continue
            if when <= at or (when.date() - at.date()).days > 1:
                continue
            where = meta.get("venue") or g.get("subtitle") or ""
            gig_day = _day(date.fromisoformat(g["date"])) if FULL_DATE.match(g["date"] or "") else ""
            out.append({"kind": "onsale", "key": f"onsale:{g['id']}:{iso}", "icon": "bolt",
                        "title": f"{g['artist']} {what}" if what == "tickets on sale" else f"{g['artist']} — {what}",
                        "detail": " · ".join(x for x in (where, gig_day) if x), "chip": _when(when, at),
                        "level": "now" if when.date() == at.date() else "soon", "href": g.get("url") or None})
    return out


def _fresh_alerts(widgets: dict[str, dict[str, Any]], at: datetime, top: int) -> list[dict[str, Any]]:
    """Newly found albums, gigs and pre-orders by your top artists — one grouped alert per kind, so a busy day isn't a flood.
    Only what the card itself would show counts (its own filters apply); marking it seen on the card clears it here too."""
    artists = content.top_artists(top)
    out = []
    for kind, (one, many, icon) in FRESH_LABELS.items():
        shown = {i["id"] for i in _items(widgets, kind)}
        items = [i for i in content.fresh_items(kind, int(at.timestamp()) - FRESH_FOR, artists) if i["id"] in shown]
        if not items:
            continue
        key = f"fresh:{kind}:{items[0]['first_seen']}"            # newest first: anything newer makes a new key, and so a new alert
        if len(items) == 1:
            i = items[0]
            when = _day(date.fromisoformat(i["date"])) if FULL_DATE.match(i["date"] or "") else ""
            out.append({"kind": "fresh", "key": key, "icon": icon, "title": f"{i['artist']} — {i['title']}" if i["artist"] else i["title"],
                        "detail": " · ".join(x for x in (one, when) if x), "chip": "New", "level": "info", "href": i.get("url") or None})
        else:
            out.append({"kind": "fresh", "key": key, "icon": icon, "title": f"{len(items)} {many}", "detail": _names(items),
                        "chip": "New", "level": "info"})
    return out


def service_alerts(projects: list[dict[str, Any]], links: list[dict[str, Any]], health: Health, monitor: Monitor | None, at: datetime) -> list[dict[str, Any]]:
    """Owner only: something that should be answering has been unreachable for a while, or this PC is short of room.
    A project only counts while its service is running (a stopped project is meant to be down)."""
    if not alert_settings.settings()["down"]:
        return []
    t, out = at.timestamp(), []
    watch = [(f"project:{p['slug']}", p["name"]) for p in projects if p["managed"] and p["kind"] == "web"]
    watch += [(f"link:{l['id']}", l["name"]) for l in links]
    for hkey, name in watch:
        h = health.state.get(hkey)
        if not h or h["ok"] or not h.get("down_since") or t - h["down_since"] < DOWN_AFTER:
            continue
        since = datetime.fromtimestamp(h["down_since"], config.TZ)
        out.append({"kind": "down", "key": f"down:{hkey}:{h['down_since']}", "icon": "alert", "title": f"{name} isn't answering",
                    "detail": f"down since {since:%H:%M}" if since.date() == at.date() else f"down since {_day(since.date())}", "chip": "Down", "level": "soon"})
    if monitor and monitor.mem_tight_since and t - monitor.mem_tight_since >= MEM_TIGHT_FOR and monitor.latest:
        m = monitor.latest["mem"]
        out.append({"kind": "system", "key": f"sys:mem:{at.date()}", "icon": "alert", "title": "This PC is short of memory",
                    "detail": f"{m['available'] / 1024**3:.1f} GB RAM free · swap {round(100 * m['swap_used'] / m['swap_total'])}% full", "chip": "Memory", "level": "now"})
    for d in low_disks():
        out.append({"kind": "system", "key": f"sys:disk:{d['mount']}:{at.date()}", "icon": "alert", "title": f"Disk {d['mount']} is nearly full",
                    "detail": f"{d['free'] / 1024**3:.0f} GB free of {d['total'] / 1024**3:.0f} GB", "chip": "Disk", "level": "soon"})
    return out


# ---- the full owner snapshot -------------------------------------------------------------------

async def owner_state(health: Health, device: dict[str, Any], attention: list[dict[str, Any]] | None = None,
                      monitor: Monitor | None = None) -> dict[str, Any]:
    at = now()
    widgets = widgets_state()
    projects, links = await projects_state(health), links_state(health)
    return {
        "now": at.isoformat(),
        "host": socket.gethostname(),
        "ip": config.lan_ip(),
        "version": __version__,
        "device": {"id": device["id"], "name": device["name"], "role": device["role"]},
        "widgets": widgets,
        "projects": projects,
        "links": links,
        "health": health.state,
        # owner only: a sync that keeps failing, or a service that's down, is a thing to know about
        "alerts": alert_settings.visible([*compute_alerts(widgets, at), *service_alerts(projects, links, health, monitor, at), *(attention or [])]),
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
        for a in alert_settings.visible(compute_alerts(all_widgets, at)):
            if (a["kind"] == "bin" and "bins" in allowed) or (a["kind"] == "event" and "diary" in allowed) or (a["kind"] == "gig" and "gigs" in allowed):
                alerts.append(a)
    return {
        "now": at.isoformat(),
        "tv": tv,
        "widgets": {wid: w for wid, w in all_widgets.items() if wid in allowed},
        "alerts": alerts,
    }
