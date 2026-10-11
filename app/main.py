"""The Citadel web app: pages, the owner/guest APIs, and the event stream."""
from __future__ import annotations

import asyncio
import html
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import api_content, api_maintenance, api_music, api_news, api_owner, api_screens, auth, config, content, coverart, db, logbook, maintenance, monitor, musicbrainz, notify, procs, seed, state
from .health import Health
from .music import store as music_store
from .music.rotator import Rotator
from .music.services.spotify import client as spotify_client
from .bus import bus
from .scheduler import Scheduler
from .widgets import discover

log = logging.getLogger("hub")
# httpx logs every request URL at INFO. Calendar addresses are secrets, so those loggers stay quiet.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# Calendar titles are external content, so pages get a strict CSP as defence in depth against script injection.
# Record artwork (vinyl radar, releases) loads straight from the CDNs/archives the sources use, and nowhere else.
# Cover Art Archive's own image URLs 307-redirect twice — first to archive.org, then to one of its many
# load-balanced storage nodes (iaNNNNNN.us.archive.org, which one varies and isn't a fixed list) — verified
# live, not guessed. The browser enforces img-src against that final URL, so a wildcard subdomain is the only
# workable allowlist here, even though every image is only ever *linked* from coverartarchive.org itself.
# frame-src is Jellyfin (Media page), Music Companion (Companion page), the Music DB Explorer (SQL DB page) and
# Odysseus (Odysseus page, :7011 — its own framing rules are widened for Citadel by a proxy in ~/Projects/Odysseus) —
# fixed addresses, hardcoded here the same way the image hosts above are; if any changes in Settings, this needs
# a matching edit. i.scdn.co is Spotify's artwork CDN (the Music page's player bar). The last seven image hosts are the News
# page's story pictures (widgets/news.py IMAGE_HOSTS keeps only pictures from these). :8643 (Music Companion's maintenance server) is here too, even though nothing on Citadel's
# own pages ever creates an iframe pointed at it directly -- Companion's own settings-icon link navigates the
# *existing* #music-frame iframe to it, and frame-src also governs subsequent navigations of a nested browsing
# context, not just its initial src.
CSP = ("default-src 'self'; img-src 'self' data: https://cdn.shopify.com https://imagescdn.juno.co.uk https://static.wixstatic.com https://coverartarchive.org https://archive.org https://*.archive.org https://i.scdn.co "
       "https://ichef.bbci.co.uk https://i.guim.co.uk https://e3.365dm.com https://cdn.arstechnica.net https://www.newscientist.com https://www.quantamagazine.org https://www.sciencenews.org; "
       "style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; font-src 'self'; frame-src http://192.168.0.70:8096 http://192.168.0.66:8642 http://192.168.0.66:8643 http://192.168.0.66:8644 http://192.168.0.66:7011; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class NoCacheStatic(StaticFiles):
    """Static assets revalidate every time — cheap on a LAN, and edits show up without a hard refresh."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


async def _watch_units(app: FastAPI) -> None:
    """Publish project status when a unit appears/disappears (e.g. a project crashed, or was started elsewhere)."""
    last: set[str] | None = None
    while True:
        try:
            current = await procs.active_units()
            if last is not None and current != last:
                notify.projects()
            last = current
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("unit watcher failed")
        await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    for name in db.migrate():
        log.info("applied migration %s", name)
    logbook.install()                                # from here on, warnings and errors land in the event log (Settings → Maintenance)
    if (added := seed.seed()) and any(added.values()):
        log.info("seeded %s", added)

    if (purged := content.purge_junk()):
        log.info("removed %s placeholder artists/items (e.g. 'Various')", purged)
    if not content.list_artists() and config.MUSIC_DB.exists():          # first run: follow your top artists from Music Companion
        log.info("followed artists seeded from Music Companion: %s", content.seed_from_music_db(config.MUSIC_DB))

    widgets = discover()
    health = Health(bus)
    # Data syncs and housekeeping share one scheduler. Events carry only an id; clients re-fetch their own filtered state.
    from .music.scrobble_job import job as scrobbles   # Music Companion's scheduled Last.fm import (no dashboard card)
    from .music.buying import job as buying            # the Music page's "Worth buying" prices from Discogs (no dashboard card)
    from .music.discover import job as discovering    # the Music page's Discover, redone overnight when stale (no dashboard card)

    def on_update(wid: str) -> None:
        if wid in widgets:
            notify.widget(wid)
        elif wid == buying.id:
            music_store.changed("buying")              # the Music page re-reads its prices

    scheduler = Scheduler({**widgets, **maintenance.jobs(), scrobbles.id: scrobbles, buying.id: buying, discovering.id: discovering}, on_update)
    mon = monitor.Monitor()
    rotator = Rotator()                              # rotating playlists on the Music page (app/music/rotator.py)
    app.state.health, app.state.scheduler, app.state.monitor, app.state.rotator = health, scheduler, mon, rotator
    music_store.learn_secrets()
    await health.start()
    await scheduler.start()
    await mon.start()
    await rotator.start()
    watcher = asyncio.create_task(_watch_units(app), name="unit-watcher")

    if auth.needs_bootstrap():
        token = auth.create_enrol_token("owner", "First owner device")
        log.warning("No owner device yet. Open this on the device you want as owner (valid 15 min): %s/enrol/%s",
                    config.public_url(), token)
    log.info("Citadel up on %s (widgets: %s)", config.public_url(), ", ".join(widgets), extra={"keep": True})
    try:
        yield
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        await rotator.stop()
        await mon.stop()
        await scheduler.stop()
        await health.stop()
        await musicbrainz.client.close()
        await coverart.client.close()
        await spotify_client.close()


app = FastAPI(title="Citadel", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", NoCacheStatic(directory=config.WEB_DIR), name="static")
# Manually-added purchases' cover images (vinyl.fetch_manual_cover) — fetched once server-side and served
# same-origin, rather than hotlinked, since the CSP's img-src below only allows a fixed set of CDNs. The
# directory is created lazily on first use, so check_dir=False avoids a startup dependency on it existing yet.
(config.DATA_DIR / "covers").mkdir(parents=True, exist_ok=True)
app.mount("/covers", NoCacheStatic(directory=config.DATA_DIR / "covers"), name="covers")


# Shortcut icons (widgets/shortcuts.py), downloaded server-side for the same CSP reason. Only plain file names the
# widget itself wrote are served, owner-only, and with a sandboxing CSP of their own: a site's SVG icon could carry
# script, which an <img> never runs, but opening the file directly would otherwise run it as Citadel.
from .widgets import shortcuts as _shortcuts   # noqa: E402


from . import appearance as _appearance   # noqa: E402


@app.get("/backgrounds/{name}", include_in_schema=False)
async def background_picture(name: str, request: Request):
    """Custom themes' background pictures (Settings → Appearance): owner-only, plain names only, content-addressed."""
    auth.require_owner(request)
    path = _appearance.bg_dir() / name
    if not _appearance.BG_NAME_RE.fullmatch(name) or not path.is_file():
        raise HTTPException(404, "no such picture")
    return FileResponse(path, media_type=_appearance.MEDIA_TYPES[name.rsplit(".", 1)[1]],
                        headers={"Cache-Control": "private, max-age=31536000, immutable", "Content-Security-Policy": "default-src 'none'; sandbox"})


@app.get("/shortcut-icons/{name}", include_in_schema=False)
async def shortcut_icon(name: str, request: Request):
    auth.require_owner(request)
    path = _shortcuts.icon_dir() / name
    if not _shortcuts.ICON_NAME_RE.fullmatch(name) or not path.is_file():
        raise HTTPException(404, "no such icon")
    return FileResponse(path, media_type=_shortcuts.MEDIA_TYPES[name.rsplit(".", 1)[1]],
                        headers={"Cache-Control": "private, max-age=86400", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


# ---- pages -------------------------------------------------------------------------------------

def _page(name: str) -> FileResponse:
    return FileResponse(config.WEB_DIR / name, headers={"Cache-Control": "no-cache"})


@app.get("/", include_in_schema=False)
async def index(request: Request):
    # One URL for everyone: owners get the dashboard, paired screens get the TV view, anyone else the guest page.
    return _page({"owner": "index.html", "kiosk": "tv.html"}.get(auth.role_of(request), "guest.html"))


@app.get("/tv", include_in_schema=False)
async def tv_page(request: Request):
    # An unpaired screen lands on the pairing page and shows a code for an owner to approve.
    return _page("tv.html" if auth.role_of(request) in ("owner", "kiosk") else "pair.html")


@app.get("/settings", include_in_schema=False)
async def settings_page(request: Request):
    if auth.role_of(request) != "owner":
        return RedirectResponse("/", status_code=303)
    return _page("settings.html")


@app.get("/guest", include_in_schema=False)
async def guest_page():
    return _page("guest.html")


@app.get("/healthz", include_in_schema=False)
async def healthz():
    return {"ok": True}


def _enrol_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Citadel</title>
<link rel="stylesheet" href="/static/css/tokens.css"><link rel="stylesheet" href="/static/css/components.css">
<link rel="stylesheet" href="/static/css/guest.css"></head><body><main class="guest"><div class="guest__wrap">
<span class="wordmark">Citadel</span><p class="guest__hello">{title}</p>{body}</div></main></body></html>""", status_code=status)


@app.get("/enrol/{token}", include_in_schema=False)
async def enrol_confirm(token: str):
    # GET only *shows* the request; a POST consumes the token, so link-preview bots can't burn it.
    if not (tok := auth.peek_enrol_token(token)):
        return _enrol_page("This enrolment link has expired or was already used.", '<a class="btn" href="/">Back</a>', 410)
    return _enrol_page(
        f"Enrol this device as <b>{html.escape(tok['role'])}</b> — “{html.escape(tok['name'])}”?",
        f'<form method="post" action="/enrol/{html.escape(token)}"><button class="btn btn--go" type="submit">Enrol this device</button></form>')


@app.post("/enrol/{token}", include_in_schema=False)
async def enrol_redeem(token: str, request: Request):
    cookie = auth.redeem_enrol_token(token, request.headers.get("user-agent", ""))
    if not cookie:
        return _enrol_page("This enrolment link has expired or was already used.", '<a class="btn" href="/">Back</a>', 410)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(auth.COOKIE, cookie, max_age=auth.COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/")
    return resp


# ---- guest API (no auth: deliberately tiny) ------------------------------------------------------

@app.get("/api/guest")
async def guest_api(request: Request):
    return {"links": state.links_state(request.app.state.health, guest_only=True)}


app.include_router(api_owner.router)
app.include_router(api_maintenance.router)
app.include_router(api_content.router)
app.include_router(api_music.router)
app.include_router(api_news.router)
app.include_router(api_music.public)
app.include_router(api_screens.public)
app.include_router(api_screens.kiosk)
