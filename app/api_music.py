"""The Music page's API (owner only), plus the one public page Spotify's login returns to.

Errors from outside services come back as 502 with a plain-words message the page shows as it is; bad input is 422."""
from __future__ import annotations

import html
import shlex
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from . import auth, db
from .music import buying, discover, gigs, ingredients, library, matcher, player, playlists, store
from .music.services import all_services, setlistfm
from .music.services import spotify as sp
from .music.services.base import ServiceError
from .scheduler import parse_hhmm

router = APIRouter(prefix="/api/owner/music", dependencies=[Depends(auth.require_owner)])
public = APIRouter()


def _svc(exc: Exception) -> HTTPException:
    return HTTPException(502, str(exc))


async def _guard(coro):
    """Run something that talks to the outside world; turn its failures into messages for the page."""
    try:
        return await coro
    except ServiceError as e:
        raise _svc(e)
    except library.LibraryMissing as e:
        raise HTTPException(503, str(e))
    except playlists.PlaylistError as e:
        raise HTTPException(422, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


# ---- overview -------------------------------------------------------------------------------------------------------

def _settings_view() -> dict[str, Any]:
    s = store.settings()
    return {"prefix": s["prefix"], "public": s["public"], "rotate_at": s["rotate_at"], "desktop_command": shlex.join(s["desktop_argv"])}


@router.get("")
async def overview():
    try:
        genres = library.genres(60) if library.available() else []
        stats = library.summary() if library.available() else {}
        owned = library.owned_by_artist() if library.available() else {}
    except Exception:
        genres, stats, owned = [], {}, {}
    return {
        "settings": _settings_view(),
        "services": [s.public() for s in all_services().values()],
        "spotify": sp.service.extra() | {"configured": sp.service.configured(), "enabled": sp.service.enabled()},
        "ingredients": [k.public() for k in ingredients.KINDS.values()],
        "templates": [{"id": k, "name": t["name"], "blurb": t["blurb"], "kinds": [i[0] for i in t["ings"]]} for k, t in playlists.TEMPLATES.items()],
        "orders": list(playlists.ORDERS),
        "library": {"available": library.available(), "genres": genres, "owned": owned, **stats},
        "playlists": playlists.all_playlists(),
    }


# ---- settings (Settings → Music page) -----------------------------------------------------------------------------------

class SettingsIn(BaseModel):
    prefix: str | None = Field(None, max_length=24)
    public: bool | None = None
    rotate_at: str | None = None
    desktop_command: str | None = Field(None, max_length=300)
    services: dict[str, dict[str, Any]] = {}


@router.put("/settings")
async def put_settings(body: SettingsIn, request: Request):
    s = store.settings()
    if body.prefix is not None:
        if not store.PREFIX_RE.match(body.prefix):
            raise HTTPException(422, "the name prefix can’t contain control characters")
        s["prefix"] = body.prefix
    if body.public is not None:
        s["public"] = body.public
    if body.rotate_at is not None:
        if not parse_hhmm(body.rotate_at):
            raise HTTPException(422, "rotation time must look like 06:00")
        s["rotate_at"] = body.rotate_at
    if body.desktop_command is not None:
        try:
            argv = shlex.split(body.desktop_command)
        except ValueError:
            raise HTTPException(422, "the Spotify command has unbalanced quotes")
        if not argv:
            raise HTTPException(422, "say how to start the Spotify desktop app")
        s["desktop_argv"] = argv
    svcs = all_services()
    for sid, values in body.services.items():
        if sid not in svcs:
            raise HTTPException(422, f"unknown service {sid}")
        try:
            s["services"][sid] = svcs[sid].merge(s["services"].get(sid, {}), values)
        except ValueError as e:
            raise HTTPException(422, str(e))
    before_discogs = store.settings()["services"].get("discogs", {})
    store.put_settings(s)
    store.learn_secrets()
    store.changed("settings")
    if s["services"].get("discogs", {}) != before_discogs and buying.job.configured({}):
        request.app.state.scheduler.refresh(buying.job.id)      # a new token or limit: check prices now, not in five hours
    return {"ok": True}


@router.post("/services/{service_id}/test")
async def test_service(service_id: str):
    svc = all_services().get(service_id)
    if not svc:
        raise HTTPException(404, "no such service")
    try:
        return {"ok": True, "message": await svc.test()}
    except Exception as e:                                         # a test reports anything, in words
        return {"ok": False, "message": str(e) or e.__class__.__name__}


@router.post("/spotify/login")
async def spotify_login():
    try:
        return {"url": sp.login_url(), "redirect_uri": sp.redirect_uri()}
    except ServiceError as e:
        raise HTTPException(422, str(e))


@router.post("/spotify/logout")
async def spotify_logout():
    sp.disconnect()
    return {"ok": True}


@router.delete("/matches")
async def forget_matches():
    return {"forgotten": matcher.forget_all()}


@public.get("/music/spotify/callback", include_in_schema=False)
async def spotify_callback(code: str = "", state: str = "", error: str = ""):
    def page(title: str, text: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(f"""<!doctype html><html lang="en-GB"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Citadel · Spotify</title><link rel="stylesheet" href="/static/css/tokens.css"><link rel="stylesheet" href="/static/css/components.css">
<link rel="stylesheet" href="/static/css/guest.css"></head><body><main class="guest"><div class="guest__wrap"><span class="wordmark">Citadel</span>
<p class="guest__hello">{html.escape(title)}</p><p>{html.escape(text)}</p></div></main></body></html>""", status_code=status)
    if error:
        return page("Spotify login cancelled", "Nothing was changed. You can close this tab.", 400)
    try:
        who = await sp.finish_login(code, state)
    except ServiceError as e:
        return page("Couldn’t connect Spotify", str(e), 400)
    return page(f"Spotify connected — {who}", "You can close this tab. The Music page works from any of your devices now.")


# ---- player -------------------------------------------------------------------------------------------------------------

@router.get("/player")
async def player_state():
    try:
        return await player.state()
    except ServiceError as e:
        return {"ready": True, "active": False, "error": str(e)}


@router.get("/player/devices")
async def player_devices():
    return {"devices": await _guard(player.devices())}


class CommandIn(BaseModel):
    value: Any = None


@router.post("/player/{cmd}")
async def player_command(cmd: Literal["play", "pause", "next", "previous", "seek", "volume", "shuffle", "repeat", "transfer", "like"], body: CommandIn):
    await _guard(player.command(cmd, body.value))
    return {"ok": True}


class PlayIn(BaseModel):
    kind: Literal["track", "album", "artist", "playlist", "uris"]
    artist: str = Field("", max_length=200)
    title: str = Field("", max_length=300)
    playlist_id: int | None = None
    start: int = 0
    uris: list[str] = []


async def _top_uris(artist: str, n: int = 10) -> list[str]:
    """An artist's top songs on Spotify, in order: Last.fm's most played (Spotify no longer shares its own top tracks),
    else Spotify's search."""
    titles = await discover.artist_tracks(artist, n)
    found = await matcher.match([{"key": library.key(artist, t), "title": t, "artist": artist} for t in titles])
    uris = [found[library.key(artist, t)] for t in titles if found.get(library.key(artist, t))]
    if not uris:
        items = await sp.client.search(f'artist:"{artist}"', "track", 10)
        uris = [i["uri"] for i in items if library.anorm(artist) in {library.anorm(a["name"]) for a in i.get("artists", [])}]
    return uris


async def _album_hit(artist: str, title: str) -> dict[str, Any] | None:
    albums = await sp.client.search(f'album:"{title}" artist:"{artist}"', "album", 5)
    return next((a for a in albums if library.anorm(artist) in {library.anorm(x["name"]) for x in a.get("artists", [])}), None)


async def _play(body: PlayIn) -> dict[str, Any]:
    if body.kind == "playlist":
        pl = playlists.get(body.playlist_id or 0)
        if pl.get("spotify_id"):
            await player.play_uris([], context=f"spotify:playlist:{pl['spotify_id']}")
            return {"what": playlists.spotify_name(pl)}
        rows = [t for t in playlists.tracks(pl["id"]) if t["uri"]]
        await player.play_uris([t["uri"] for t in rows[body.start:] + rows[:body.start]])
        return {"what": pl["name"]}
    if body.kind == "uris":
        await player.play_uris([u for u in body.uris if u.startswith("spotify:track:")])
        return {"what": ""}                                         # the page names what it asked for
    if body.kind == "album":
        hit = await _album_hit(body.artist, body.title)
        if hit:
            await player.play_uris([], context=hit["uri"])
            return {"what": hit["name"]}
        body.kind = "artist"                                       # not out yet (or not on Spotify): their best-known songs instead
    if body.kind == "artist":
        await player.play_uris(await _top_uris(body.artist))
        return {"what": f"{body.artist} — their top 10"}
    hit = await matcher.search_track(body.artist, body.title)
    if not hit:
        raise ServiceError(f"Couldn’t find “{body.title}” by {body.artist} on Spotify")
    await player.play_uris([hit["uri"]])
    return {"what": hit["name"]}


@router.post("/play")
async def play(body: PlayIn):
    return await _guard(_play(body))


async def _queue(body: PlayIn) -> dict[str, Any]:
    """Add to the end of what's playing (the desktop client's "Add to queue"): an artist's top 10, or a whole album."""
    if body.kind == "album":
        hit = await _album_hit(body.artist, body.title)
        if not hit:
            raise ServiceError(f"Couldn’t find {body.title} by {body.artist} on Spotify")
        uris, what = [t["uri"] for t in await sp.client.album_tracks(hit["id"]) if t.get("uri")], hit["name"]
    elif body.kind == "artist":
        uris, what = await _top_uris(body.artist), f"{body.artist}’s top 10"
    else:
        raise HTTPException(422, "queue an artist's top 10 or an album")
    if not uris:
        raise ServiceError("Couldn’t find that on Spotify")
    await player.queue_uris(uris)
    return {"what": what, "queued": len(uris)}


@router.post("/queue")
async def queue(body: PlayIn):
    return await _guard(_queue(body))


@router.post("/player-open")
async def open_spotify():
    return await _guard(player.open_desktop())


# ---- playlists ----------------------------------------------------------------------------------------------------------

def _full(pid: int) -> dict[str, Any]:
    pl = playlists.get(pid)
    pl["next_run"] = playlists.next_run(pl)
    pl["tracks"] = playlists.tracks(pid)
    pl["summaries"] = [ingredients.describe(i) for i in pl["ings"]]
    pl["spotify_name"] = playlists.spotify_name(pl)
    return pl


@router.get("/playlists/{pid}")
async def get_playlist(pid: int):
    return await _guard(_async(_full, pid))


async def _async(fn, *a):
    return fn(*a)


class NewPlaylistIn(BaseModel):
    template: str = Field(..., max_length=20)
    name: str | None = Field(None, max_length=80)


@router.post("/playlists", status_code=201)
async def new_playlist(body: NewPlaylistIn):
    over = {"name": body.name} if body.name else {}
    pl = await _guard(_async(lambda: playlists.from_template(body.template, **over)))
    return _full(pl["id"])


class DesignIn(BaseModel):
    name: str = Field(..., max_length=80)
    mode: Literal["fixed", "rot"]
    schedule: dict[str, Any] = {}
    public: bool = True
    ings: list[dict[str, Any]] = []
    rules: dict[str, Any] = {}


@router.put("/playlists/{pid}")
async def put_playlist(pid: int, body: DesignIn):
    await _guard(_async(lambda: playlists.save_design(body.model_dump(), pid)))
    return _full(pid)


@router.delete("/playlists/{pid}")
async def delete_playlist(pid: int):
    await _guard(_async(playlists.delete, pid))
    return {"ok": True}


class ReorderIn(BaseModel):
    ids: list[int]


@router.post("/playlists/reorder")
async def reorder_playlists(body: ReorderIn):
    playlists.reorder(body.ids)
    return {"ok": True}


@router.post("/playlists/{pid}/build")
async def build_playlist(pid: int):
    result = await _guard(playlists.build(pid))
    return {**result, "playlist": _full(pid)}


@router.post("/playlists/{pid}/save")
async def save_playlist(pid: int):
    result = await _guard(playlists.save(pid))
    return {**result, "playlist": _full(pid)}


class TrackOpIn(BaseModel):
    op: Literal["lock", "unlock", "remove", "move"]
    pos: int
    to: int | None = None


@router.post("/playlists/{pid}/tracks")
async def edit_track(pid: int, body: TrackOpIn):
    await _guard(_async(lambda: playlists.edit_tracks(pid, body.op, body.pos, body.to)))
    return _full(pid)


class MatchIn(BaseModel):
    pos: int
    uri: str | None = None
    name: str = Field("", max_length=300)
    artist: str = Field("", max_length=300)
    album: str = Field("", max_length=300)


@router.post("/playlists/{pid}/match")
async def match_track(pid: int, body: MatchIn):
    await _guard(_async(lambda: playlists.set_match(pid, body.pos, body.uri, body.name, body.artist, body.album)))
    return _full(pid)


@router.get("/candidates")
async def candidates(artist: str, title: str):
    return {"items": await _guard(matcher.candidates(artist, title))}


# ---- worth buying (Discogs) ----------------------------------------------------------------------------------------------

@router.get("/buying")
async def get_buying(request: Request):
    """The last priced run, with your current limit applied (app/music/buying.py)."""
    c = db.cache_get(buying.job.id)
    error = (c["error"] or "").removeprefix("RuntimeError: ") or None
    return {**buying.view(c["data"]), "error": error, "error_at": c["error_at"],
            "running": buying.job.id in request.app.state.scheduler.running}


@router.post("/buying/refresh", status_code=202)
async def refresh_buying(request: Request):
    """Check prices now (queued behind a run already going)."""
    if not buying.job.configured({}):
        raise HTTPException(422, "Discogs isn't set up — add a personal access token in Settings → Music page")
    request.app.state.scheduler.refresh(buying.job.id)
    return {"ok": True}


class DismissIn(BaseModel):
    undo: bool = False


@router.post("/buying/{album_id}/dismiss")
async def dismiss_buying(album_id: int, body: DismissIn):
    buying.dismiss(album_id, body.undo)
    store.changed("buying")
    return {"ok": True}


# ---- discover -------------------------------------------------------------------------------------------------------------

@router.get("/discover")
async def get_discover(basis: str = "12m"):
    if basis not in discover.BASES:
        raise HTTPException(422, "unknown basis")
    result = discover.cached(basis)
    if result is None and not discover.running(basis):
        discover.refresh(basis)
    fb = discover.feedback()
    return {"basis": basis, "bases": discover.BASES, "running": discover.running(basis), "result": result,
            "following": [k for k, v in fb.items() if v == "follow"]}


@router.get("/rediscover")
async def get_rediscover(n: int = 15):
    """Artists you played far more than you do now — from your own history, so nothing to wait for."""
    if not library.available():
        return {"artists": [], "available": False}
    return {"artists": library.rediscover(max(1, min(n, 40))), "available": True}


@router.post("/discover/refresh")
async def refresh_discover(basis: str = "12m"):
    if basis not in discover.BASES:
        raise HTTPException(422, "unknown basis")
    return {"started": discover.refresh(basis)}


class FeedbackIn(BaseModel):
    artist: str = Field(..., min_length=1, max_length=200)
    verdict: Literal["follow", "nope"] | None = None


@router.post("/discover/feedback")
async def discover_feedback(body: FeedbackIn):
    discover.set_feedback(body.artist, body.verdict)
    store.changed("discover")
    return {"ok": True}


class RemixIn(BaseModel):
    artist: str = Field(..., min_length=1, max_length=200)


@router.post("/discover/rediscover")
async def rediscover_mix(body: RemixIn):
    """A fixed 'Rediscover: <artist>' playlist: their most played songs, plus ones you never got to."""
    pl = await _guard(_async(lambda: playlists.from_template("blank", name=f"Rediscover: {body.artist}", template="rediscover",
                                                             ings=[{"kind": "artists", "share": 70, "params": {"names": [body.artist], "mode": "most"}},
                                                                   {"kind": "artists", "share": 30, "params": {"names": [body.artist], "mode": "least"}}],
                                                             rules={"len": 20, "per": 0, "order": "shuffle"})))
    await _guard(playlists.build(pl["id"]))
    return _full(pl["id"])


# ---- gigs -----------------------------------------------------------------------------------------------------------------

@router.get("/gigs")
async def get_gigs():
    try:
        shows = library.shows(40) if library.available() else []
    except Exception:
        shows = []
    made = {}
    for p in playlists.all_playlists():
        m = p["meta"]
        if m.get("show_id"):
            made[f"show:{m['show_id']}"] = p["id"]
        if (m.get("gig") or {}).get("id"):
            made[f"gig:{m['gig']['id']}"] = p["id"]
    return {"booked": gigs.booked(), "shows": shows, "made": made, "setlistfm": setlistfm.service.ready()}


@router.get("/gigs/predict")
async def predict(artist: str):
    return await _guard(gigs.predict(artist))


class GigIn(BaseModel):
    gig_id: int


@router.post("/gigs/pregig")
async def pregig(body: GigIn):
    g = next((x for x in gigs.booked() if x["id"] == body.gig_id), None)
    if not g:
        raise HTTPException(404, "no such upcoming gig")
    for p in playlists.all_playlists():
        if (p["meta"].get("gig") or {}).get("id") == g["id"]:
            return _full(p["id"])
    pl = await _guard(_async(lambda: playlists.from_template(
        "pregig", name=f"Pre-gig: {g['artist']}", mode="rot", public=store.settings()["public"],
        schedule={"every": "daily", "at": store.settings()["rotate_at"], "until": g["date"]},
        ings=[{"kind": "gig", "share": 100, "params": {"kind": "predict", "artist": g["artist"]}}],
        meta={"gig": {"id": g["id"], "artist": g["artist"], "date": g["date"], "venue": g["venue"]}})))
    await _guard(playlists.build(pl["id"]))
    return _full(pl["id"])


class ShowIn(BaseModel):
    show_id: int | None = None
    url: str | None = Field(None, max_length=400)


@router.post("/gigs/setlist-playlist")
async def setlist_playlist(body: ShowIn):
    """A fixed playlist of one show: one you were at (from Music Companion), or any setlist.fm link."""
    if body.url:
        sid = setlistfm.id_from_url(body.url)
        if not sid:
            raise HTTPException(422, "that doesn’t look like a setlist.fm setlist link")
        s = await _guard(setlistfm.by_id(sid))
        name, params, meta = f"{s['artist']} @ {s['venue']} — {s['date']}", {"kind": "setlistfm", "id": sid}, {"setlistfm": sid}
    elif body.show_id:
        try:
            h, _ = library.show_tracks(body.show_id)
        except LookupError:
            raise HTTPException(404, "no such show")
        name, params, meta = f"{h['artist']} @ {h['venue']} — {h['date']}", {"kind": "show", "show_id": body.show_id}, {"show_id": body.show_id}
    else:
        raise HTTPException(422, "give a show or a setlist.fm link")
    pl = await _guard(_async(lambda: playlists.from_template("pregig", name=name[:80], mode="fixed", meta=meta,
                                                             ings=[{"kind": "gig", "share": 100, "params": params}])))
    result = await _guard(playlists.build(pl["id"]))
    saved = None
    if sp.service.ready():
        try:
            saved = await playlists.save(pl["id"])
        except ServiceError as e:
            result["errors"].append(str(e))
    return {**result, "saved": saved, "playlist": _full(pl["id"])}
