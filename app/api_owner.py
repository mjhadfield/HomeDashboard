"""Owner-only API: live state, project control, and everything editable from Settings."""
from __future__ import annotations

import asyncio
import io
import json
import os
import shlex
import socket
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, alerts, appearance, auth, config, db, notify, procs, schemas, state
from .bus import bus
from .health import Health
from .widgets import discover
from . import maintenance
from .widgets.bins import STREAMS

router = APIRouter(prefix="/api/owner", dependencies=[Depends(auth.require_owner)])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


# ---- live state + events ------------------------------------------------------------------------

@router.get("/state")
async def owner_state(request: Request):
    return await state.owner_state(request.app.state.health, auth.current_device(request), maintenance.attention(request.app.state.scheduler),
                                   request.app.state.monitor)


@router.get("/events")
async def owner_events():
    return StreamingResponse(bus.stream(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/monitoring")
async def monitoring(request: Request):
    """CPU/GPU/memory stats — the latest reading from a background loop (app/monitor.py), polled directly by
    the frontend, deliberately outside the widget/state/SSE machinery."""
    return await request.app.state.monitor.snapshot()


# ---- the reminder strip --------------------------------------------------------------------------

@router.post("/alerts/dismiss")
async def dismiss_alert(body: schemas.AlertKeyIn):
    """Hide one reminder everywhere (TVs too). Its key names one occurrence, so next time's comes back by itself."""
    alerts.dismiss(body.key)
    notify.config()
    return {"ok": True}


@router.post("/alerts/restore")
async def restore_alert(body: schemas.AlertKeyIn):
    """Undo a dismissal."""
    alerts.undismiss(body.key)
    notify.config()
    return {"ok": True}


@router.get("/alerts/settings")
async def get_alert_settings():
    return alerts.settings()


@router.put("/alerts/settings")
async def put_alert_settings(body: schemas.AlertSettingsIn):
    db.put_settings("alerts", body.model_dump())
    notify.config()
    return alerts.settings()


# ---- project control ---------------------------------------------------------------------------

def _project(slug: str, *, enabled_only: bool = True) -> dict[str, Any]:
    with db.db() as con:
        row = con.execute("SELECT * FROM projects WHERE slug = ?" + (" AND enabled = 1" if enabled_only else ""), (slug,)).fetchone()
    if not row:
        raise HTTPException(404, "no such project")
    return dict(row)


async def _project_action(request: Request, slug: str, action: Literal["start", "stop"]) -> dict[str, bool]:
    project = _project(slug)
    health: Health = request.app.state.health
    try:
        await (procs.start(project) if action == "start" else procs.stop(slug))
    except procs.ProcError as exc:
        raise HTTPException(502, str(exc))
    notify.projects()
    if project["port"]:
        asyncio.create_task(health.recheck_project(slug, project["port"], expect_up=action == "start"))
    return {"ok": True}


@router.post("/projects/{slug}/start")
async def start_project(slug: str, request: Request):
    return await _project_action(request, slug, "start")


@router.post("/projects/{slug}/stop")
async def stop_project(slug: str, request: Request):
    return await _project_action(request, slug, "stop")


@router.get("/projects/{slug}/logs")
async def project_logs(slug: str, n: int = 200):
    _project(slug)
    return {"lines": await procs.logs(slug, n)}


# ---- everything the Settings page shows ---------------------------------------------------------

def _link_cfg(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "name": r["name"], "subtitle": r["subtitle"], "url": r["url"], "icon": r["icon"],
            "health_url": r["health_url"] or "", "is_primary": bool(r["is_primary"]), "show_guest": bool(r["show_guest"]), "tier": r["tier"],
            "pinned": r["pin_order"] is not None, "for_media": bool(r["for_media"]), "for_music": bool(r["for_music"]), "for_sql": bool(r["for_sql"]),
            "for_ai": bool(r["for_ai"])}


def _project_cfg(r: sqlite3.Row) -> dict[str, Any]:
    return {"slug": r["slug"], "name": r["name"], "description": r["description"], "kind": r["kind"], "url": r["url"] or "",
            "port": r["port"], "cwd": r["cwd"], "command": shlex.join(json.loads(r["start_argv"])), "env": json.loads(r["env"]),
            "lan_visible": bool(r["lan_visible"]), "enabled": bool(r["enabled"]), "pinned": r["pin_order"] is not None, "cwd_exists": os.path.isdir(r["cwd"]),
            "show_projects": bool(r["show_projects"]), "show_launchpad": bool(r["show_launchpad"]), "icon": r["icon"] or "",
            "for_music": bool(r["for_music"]), "for_sql": bool(r["for_sql"])}


@router.get("/config")
async def get_config(request: Request):
    with db.db() as con:
        links = [_link_cfg(r) for r in con.execute("SELECT * FROM links ORDER BY pin_order IS NULL, pin_order, sort, name")]
        projects = [_project_cfg(r) for r in con.execute("SELECT * FROM projects ORDER BY pin_order IS NULL, pin_order, sort, name")]
    me = auth.current_device(request)["id"]
    return {
        "version": __version__, "host": socket.gethostname(), "ip": config.lan_ip(), "public_url": config.public_url(),
        "paths": {"database": str(config.DB_PATH), "data": str(config.DATA_DIR), "projects": str(config.PROJECTS_DIR)},
        "links": links, "projects": projects,
        "widgets": {wid: {"title": w.title, "settings": w.public_settings(db.get_settings(w.id, w.defaults))} for wid, w in discover().items()},
        "devices": [{**d, "current": d["id"] == me} for d in auth.list_devices() if not d["revoked"]],
        "screens": state.tv_settings(), "tv_panels": list(schemas.TV_PANELS), "alerts": alerts.settings(),
        "icons": schemas.ICONS, "streams": {k: v["title"] for k, v in STREAMS.items()}, "colours": ["violet", "silver", "indigo"],
        "appearance": {"themes": appearance.themes(), "base_panel": appearance.BASE_PANEL, "base_accent": appearance.BASE_ACCENT,
                       "fits": list(appearance.FITS), "max_background_mb": appearance.MAX_BACKGROUND // 1_000_000},
    }


def _reorder(table: str, key: str, ids: list[Any]) -> None:
    """A manual up/down move in Settings. `ids` is the whole list in its new order — pinned items are kept
    contiguous at the front by the UI (it only lets you swap within the same pinned/unpinned group), so we
    renumber pin_order 1..k for that pinned prefix, in the order given; sort orders everyone else as before."""
    with db.db() as con:
        pinned = {r[key] for r in con.execute(f"SELECT {key} FROM {table} WHERE pin_order IS NOT NULL")}
        pin_i = 0
        for i, ident in enumerate(ids):
            if ident in pinned:
                pin_i += 1
                con.execute(f"UPDATE {table} SET sort = ?, pin_order = ? WHERE {key} = ?", (i * 10, pin_i, ident))
            else:
                con.execute(f"UPDATE {table} SET sort = ? WHERE {key} = ?", (i * 10, ident))


# ---- links ---------------------------------------------------------------------------------------

def _next_pin_order(con: sqlite3.Connection) -> int:
    """Pin order is one sequence shared by links and projects, so a merged view (the Launchpad) can sort by
    it directly and get the real pin order across both, regardless of which kind of thing was pinned when."""
    m1 = con.execute("SELECT MAX(pin_order) m FROM links").fetchone()["m"] or 0
    m2 = con.execute("SELECT MAX(pin_order) m FROM projects").fetchone()["m"] or 0
    return max(m1, m2) + 1


def _apply_pin(con: sqlite3.Connection, table: str, key: str, ident: Any, pinned: bool | None) -> None:
    """None = leave unchanged. True on something already pinned is a no-op — it keeps its position, it
    doesn't jump to the front. False always clears it, freeing nothing else (later pins just count higher)."""
    if pinned is None:
        return
    if not pinned:
        con.execute(f"UPDATE {table} SET pin_order = NULL WHERE {key} = ?", (ident,))
        return
    cur = con.execute(f"SELECT pin_order FROM {table} WHERE {key} = ?", (ident,)).fetchone()
    if cur and cur["pin_order"] is not None:
        return
    con.execute(f"UPDATE {table} SET pin_order = ? WHERE {key} = ?", (_next_pin_order(con), ident))


def _link_values(b: schemas.LinkIn) -> tuple:
    return (b.name, b.subtitle, b.url, b.icon, b.health_url, int(b.is_primary), int(b.show_guest), b.tier)


def _apply_for_media(con: sqlite3.Connection, link_id: Any, for_media: bool) -> None:
    """At most one link at a time — the Media page has exactly one iframe. Setting this one clears it on
    every other link first, same transaction, so there's never a moment with two (or the wrong one)."""
    if not for_media:
        con.execute("UPDATE links SET for_media = 0 WHERE id = ?", (link_id,))
        return
    con.execute("UPDATE links SET for_media = 0 WHERE id != ?", (link_id,))
    con.execute("UPDATE links SET for_media = 1 WHERE id = ?", (link_id,))


def _apply_for_music(con: sqlite3.Connection, link_id: Any, for_music: bool) -> None:
    """Same idea as _apply_for_media, for the Companion page's iframe — a separate column, so setting one
    never touches the other."""
    if not for_music:
        con.execute("UPDATE links SET for_music = 0 WHERE id = ?", (link_id,))
        return
    con.execute("UPDATE links SET for_music = 0 WHERE id != ?", (link_id,))
    con.execute("UPDATE links SET for_music = 1 WHERE id = ?", (link_id,))
    con.execute("UPDATE projects SET for_music = 0")


def _apply_for_sql(con: sqlite3.Connection, link_id: Any, for_sql: bool) -> None:
    """Same idea as _apply_for_media, for the SQL DB page's iframe — a separate column, so setting one
    never touches the others."""
    if not for_sql:
        con.execute("UPDATE links SET for_sql = 0 WHERE id = ?", (link_id,))
        return
    con.execute("UPDATE links SET for_sql = 0 WHERE id != ?", (link_id,))
    con.execute("UPDATE links SET for_sql = 1 WHERE id = ?", (link_id,))
    con.execute("UPDATE projects SET for_sql = 0")


def _apply_for_ai(con: sqlite3.Connection, link_id: Any, for_ai: bool) -> None:
    """Same idea as _apply_for_media, for the Odysseus page's iframe — a separate column, so setting one
    never touches the others. Link-only, like Media: Odysseus runs under Docker, not as a Citadel project."""
    if not for_ai:
        con.execute("UPDATE links SET for_ai = 0 WHERE id = ?", (link_id,))
        return
    con.execute("UPDATE links SET for_ai = 0 WHERE id != ?", (link_id,))
    con.execute("UPDATE links SET for_ai = 1 WHERE id = ?", (link_id,))


def _apply_page(con: sqlite3.Connection, slug: str, column: str, on: bool | None) -> None:
    """A project as the Companion (for_music) or SQL DB (for_sql) page's source. One source per page across links
    and projects together, so choosing a project here takes the job from whichever link or project had it."""
    if on is None:
        return
    if not on:
        con.execute(f"UPDATE projects SET {column} = 0 WHERE slug = ?", (slug,))
        return
    con.execute(f"UPDATE projects SET {column} = 0 WHERE slug != ?", (slug,))
    con.execute(f"UPDATE links SET {column} = 0")
    con.execute(f"UPDATE projects SET {column} = 1 WHERE slug = ?", (slug,))


@router.post("/links", status_code=201)
async def create_link(body: schemas.LinkIn, request: Request):
    with db.db() as con:
        sort = con.execute("SELECT COALESCE(MAX(sort), 0) m FROM links").fetchone()["m"] + 10
        cur = con.execute("INSERT INTO links (name, subtitle, url, icon, health_url, is_primary, show_guest, tier, sort) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (*_link_values(body), sort))
        _apply_pin(con, "links", "id", cur.lastrowid, body.pinned)
        _apply_for_media(con, cur.lastrowid, body.for_media)
        _apply_for_music(con, cur.lastrowid, body.for_music)
        _apply_for_sql(con, cur.lastrowid, body.for_sql)
        _apply_for_ai(con, cur.lastrowid, body.for_ai)
    notify.config()
    asyncio.create_task(request.app.state.health.sweep_now())
    return {"id": cur.lastrowid}


@router.post("/links/reorder")
async def reorder_links(body: schemas.IdList):
    _reorder("links", "id", [int(i) for i in body.ids])
    notify.config()
    return {"ok": True}


@router.put("/links/{link_id}")
async def update_link(link_id: int, body: schemas.LinkIn, request: Request):
    with db.db() as con:
        n = con.execute("UPDATE links SET name=?, subtitle=?, url=?, icon=?, health_url=?, is_primary=?, show_guest=?, tier=? WHERE id=?",
                        (*_link_values(body), link_id)).rowcount
        if n:
            _apply_pin(con, "links", "id", link_id, body.pinned)
            _apply_for_media(con, link_id, body.for_media)
            _apply_for_music(con, link_id, body.for_music)
            _apply_for_sql(con, link_id, body.for_sql)
            _apply_for_ai(con, link_id, body.for_ai)
    if not n:
        raise HTTPException(404, "no such link")
    notify.config()
    asyncio.create_task(request.app.state.health.sweep_now())
    return {"ok": True}


@router.delete("/links/{link_id}")
async def delete_link(link_id: int, request: Request):
    with db.db() as con:
        n = con.execute("DELETE FROM links WHERE id = ?", (link_id,)).rowcount
    if not n:
        raise HTTPException(404, "no such link")
    notify.config()
    asyncio.create_task(request.app.state.health.sweep_now())
    return {"ok": True}


# ---- projects (configuration) --------------------------------------------------------------------

def _project_values(b: schemas.ProjectIn) -> tuple:
    return (b.name, b.description, b.kind, b.url, b.port if b.kind == "web" else None, b.cwd,
            json.dumps(b.argv()), json.dumps(b.env), int(b.lan_visible), int(b.enabled), int(b.show_projects), int(b.show_launchpad), b.icon)


def _warnings(b: schemas.ProjectIn) -> list[str]:
    out = []
    if not os.path.isdir(b.cwd):
        out.append("That working directory doesn't exist on this machine (yet), so Start will fail until it does.")
    if b.kind == "web" and not b.port:
        out.append("A web project needs a port, otherwise its status can't be checked.")
    if b.show_launchpad and b.kind == "web" and not b.url:
        out.append("A web project needs a url to be a useful Launchpad tile — otherwise there's nothing to link to.")
    return out


@router.post("/projects/reorder")
async def reorder_projects(body: schemas.IdList):
    _reorder("projects", "slug", [str(i) for i in body.ids])
    notify.config()
    return {"ok": True}


@router.post("/projects", status_code=201)
async def create_project(body: schemas.ProjectCreate, request: Request):
    try:
        with db.db() as con:
            sort = con.execute("SELECT COALESCE(MAX(sort), 0) m FROM projects").fetchone()["m"] + 10
            con.execute("INSERT INTO projects (slug, name, description, kind, url, port, cwd, start_argv, env, lan_visible, enabled, show_projects, show_launchpad, icon, sort) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (body.slug, *_project_values(body), sort))
            _apply_pin(con, "projects", "slug", body.slug, body.pinned)
            _apply_page(con, body.slug, "for_music", body.for_music)
            _apply_page(con, body.slug, "for_sql", body.for_sql)
    except sqlite3.IntegrityError:
        raise HTTPException(409, f"a project with id '{body.slug}' already exists")
    notify.config()
    notify.projects()
    asyncio.create_task(request.app.state.health.sweep_now())
    return {"ok": True, "warnings": _warnings(body)}


@router.put("/projects/{slug}")
async def update_project(slug: str, body: schemas.ProjectIn, request: Request):
    before = _project(slug, enabled_only=False)
    with db.db() as con:
        con.execute("UPDATE projects SET name=?, description=?, kind=?, url=?, port=?, cwd=?, start_argv=?, env=?, lan_visible=?, enabled=?, show_projects=?, show_launchpad=?, icon=? WHERE slug=?",
                    (*_project_values(body), slug))
        _apply_pin(con, "projects", "slug", slug, body.pinned)
        _apply_page(con, slug, "for_music", body.for_music)
        _apply_page(con, slug, "for_sql", body.for_sql)
    if before["enabled"] and not body.enabled:          # hiding a project shouldn't leave it running unseen
        try:
            await procs.stop(slug)
        except procs.ProcError:
            pass
    notify.config()
    notify.projects()
    asyncio.create_task(request.app.state.health.sweep_now())
    warnings = _warnings(body)
    if procs.unit(slug) in await procs.active_units():
        warnings.append("It's running now — changes to the command, folder or environment apply the next time you start it.")
    return {"ok": True, "warnings": warnings}


@router.delete("/projects/{slug}")
async def delete_project(slug: str, request: Request):
    _project(slug, enabled_only=False)
    try:
        await procs.stop(slug)
    except procs.ProcError as exc:
        raise HTTPException(502, f"couldn't stop it first: {exc}")
    with db.db() as con:
        con.execute("DELETE FROM projects WHERE slug = ?", (slug,))
    notify.config()
    notify.projects()
    asyncio.create_task(request.app.state.health.sweep_now())
    return {"ok": True}


# ---- pinning (one click from the dashboard or Settings) ------------------------------------------

@router.post("/links/{link_id}/pin")
async def pin_link(link_id: int, body: schemas.PinIn):
    with db.db() as con:
        row = con.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such link")
        _apply_pin(con, "links", "id", link_id, body.pinned)
    notify.config()
    return {"ok": True, "pinned": body.pinned}


@router.post("/projects/{slug}/pin")
async def pin_project(slug: str, body: schemas.PinIn):
    _project(slug, enabled_only=False)
    with db.db() as con:
        _apply_pin(con, "projects", "slug", slug, body.pinned)
    notify.config()
    notify.projects()
    return {"ok": True, "pinned": body.pinned}


@router.post("/projects/{slug}/launchpad")
async def project_launchpad(slug: str, body: schemas.LaunchpadIn):
    """Settings → Projects' one-click "Launchpad" toggle (the same flag the Edit form's "Show in Launchpad" sets)."""
    _project(slug, enabled_only=False)
    with db.db() as con:
        con.execute("UPDATE projects SET show_launchpad = ? WHERE slug = ?", (int(body.show), slug))
    notify.config()
    notify.projects()
    return {"ok": True, "show_launchpad": body.show}


# ---- appearance (custom themes, background pictures) ---------------------------------------------

@router.post("/appearance/themes", status_code=201)
async def create_theme(body: dict[str, Any]):
    try:
        theme = appearance.save_theme(body)
    except appearance.AppearanceError as exc:
        raise HTTPException(422, str(exc).capitalize())
    notify.config()
    return theme


@router.put("/appearance/themes/{theme_id}")
async def update_theme(theme_id: str, body: dict[str, Any]):
    try:
        theme = appearance.save_theme(body, theme_id)
    except LookupError:
        raise HTTPException(404, "no such theme")
    except appearance.AppearanceError as exc:
        raise HTTPException(422, str(exc).capitalize())
    notify.config()
    return theme


@router.delete("/appearance/themes/{theme_id}")
async def remove_theme(theme_id: str):
    try:
        appearance.delete_theme(theme_id)
    except LookupError:
        raise HTTPException(404, "no such theme")
    notify.config()
    return {"ok": True}


@router.post("/appearance/backgrounds", status_code=201)
async def upload_background(request: Request):
    """The picture itself is the request body (no form encoding): checked by its bytes, capped in size as it arrives."""
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > appearance.MAX_BACKGROUND:
            raise HTTPException(413, f"Pictures up to {appearance.MAX_BACKGROUND // 1_000_000} MB, please")
    try:
        return {"file": appearance.store_background(bytes(data))}
    except appearance.AppearanceError as exc:
        raise HTTPException(422, str(exc).capitalize())


# ---- widgets -------------------------------------------------------------------------------------

@router.post("/widgets/{widget_id}/refresh", status_code=202)
async def refresh_widget(widget_id: str, request: Request):
    if widget_id not in discover():
        raise HTTPException(404, "no such widget")
    request.app.state.scheduler.refresh(widget_id)
    return {"ok": True}


@router.get("/widgets/{widget_id}/settings")
async def get_widget_settings(widget_id: str):
    if not (w := discover().get(widget_id)):
        raise HTTPException(404, "no such widget")
    return w.public_settings(db.get_settings(w.id, w.defaults))


@router.put("/widgets/{widget_id}/settings")
async def put_widget_settings(widget_id: str, values: dict[str, Any], request: Request):
    if not (w := discover().get(widget_id)):
        raise HTTPException(404, "no such widget")
    try:
        merged = w.merge_settings(db.get_settings(w.id, w.defaults), values)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    db.put_settings(w.id, merged)
    notify.widget(w.id)                    # screens redraw with the new settings now, not only once the refresh has finished
    request.app.state.scheduler.refresh(w.id)
    return w.public_settings(merged)


# ---- devices, screens, pairing -------------------------------------------------------------------

class EnrolIn(BaseModel):
    role: Literal["owner", "kiosk"] = "owner"
    name: str = Field("New device", max_length=60)


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class PairIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)
    name: str = Field("TV", max_length=60)


def _qr_svg(url: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@router.get("/devices")
async def get_devices(request: Request):
    me = auth.current_device(request)["id"]
    return [{**d, "current": d["id"] == me} for d in auth.list_devices()]


@router.post("/devices/enrol")
async def new_enrolment(body: EnrolIn):
    token = auth.create_enrol_token(body.role, body.name)
    url = f"{config.public_url()}/enrol/{token}"
    return {"url": url, "qr_svg": _qr_svg(url), "expires_in": auth.TOKEN_TTL}


@router.patch("/devices/{device_id}")
async def rename_device(device_id: str, body: RenameIn):
    with db.db() as con:
        n = con.execute("UPDATE devices SET name = ? WHERE id = ? AND revoked = 0", (body.name.strip(), device_id)).rowcount
    if not n:
        raise HTTPException(404, "no such device")
    return {"ok": True}


@router.delete("/devices/{device_id}")
async def delete_device(device_id: str, request: Request):
    if device_id == auth.current_device(request)["id"]:
        raise HTTPException(400, "You can't revoke the device you're using.")
    if not auth.revoke_device(device_id):
        raise HTTPException(404, "no such device")
    return {"ok": True}


@router.post("/pair")
async def approve_pairing(body: PairIn):
    if not auth.pair_approve(body.code, body.name):
        raise HTTPException(404, "No screen is waiting with that code (it may have expired — refresh the TV and try again).")
    return {"ok": True}


@router.get("/screens")
async def get_screens():
    return state.tv_settings()


@router.put("/screens")
async def put_screens(body: schemas.ScreensIn):
    db.put_settings("tv", body.model_dump())
    notify.config()
    return state.tv_settings()
