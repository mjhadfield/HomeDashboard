"""What's playing, and the remote control. Spotify does the playing — on the desktop app, a phone, a speaker — and
Citadel just steers it. If nothing is open, "Open Spotify" starts the desktop app on this PC (as a transient user unit,
the same way Citadel starts a desktop project) and moves playback to it once it shows up.

The page polls this every few seconds while it's open; answers are shared for a few seconds so several open pages don't
each ask Spotify, and while Spotify has asked Citadel to wait the page is told so without asking it at all."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from typing import Any

from .. import procs
from . import library, store
from .services import spotify as sp
from .services.base import ServiceError

_cache: tuple[float, dict[str, Any]] | None = None
_liked: dict[str, bool] = {}
SHARE = 4.0
UNIT = "spotify-desktop"


def _image(item: dict[str, Any]) -> str:
    imgs = (item.get("album") or {}).get("images") or []
    small = sorted(imgs, key=lambda i: i.get("width") or 0)
    pick = next((i for i in small if (i.get("width") or 0) >= 120), small[-1] if small else None)
    return pick["url"] if pick and str(pick.get("url", "")).startswith("https://i.scdn.co/") else ""


async def state(fresh: bool = False) -> dict[str, Any]:
    global _cache
    if not sp.service.ready():
        return {"ready": False, "why": "Spotify isn’t connected" if sp.service.configured() else "Spotify isn’t set up"}
    if until := sp.resting_until():
        return {"ready": True, "active": False, "error": sp.resting_message(until), "resting_until": until}
    if not fresh and _cache and time.time() - _cache[0] < SHARE:
        return _cache[1]
    data = await sp.client.api("GET", "/me/player", params={"additional_types": "track"})
    if not data:
        out = {"ready": True, "active": False}
    else:
        item = data.get("item") or {}
        artists = [a["name"] for a in item.get("artists", [])]
        uri = item.get("uri", "")
        if uri and uri not in _liked:
            try:
                _liked[uri] = await sp.client.saved(uri)
            except ServiceError:
                _liked[uri] = False
        dev = data.get("device") or {}
        out = {"ready": True, "active": True, "playing": bool(data.get("is_playing")), "progress": data.get("progress_ms") or 0,
               "shuffle": bool(data.get("shuffle_state")), "repeat": data.get("repeat_state", "off"),
               "device": {"id": dev.get("id"), "name": dev.get("name", ""), "type": dev.get("type", ""), "volume": dev.get("volume_percent")},
               "track": {"uri": uri, "name": item.get("name", ""), "artists": artists, "album": (item.get("album") or {}).get("name", ""),
                         "image": _image(item), "duration": item.get("duration_ms") or 0, "url": (item.get("external_urls") or {}).get("spotify", "")},
               "liked": _liked.get(uri, False),
               "context": ((data.get("context") or {}).get("uri") or "")}
        try:
            out["fact"] = library.fact(artists[0], item.get("name", "")) if artists and item.get("name") else ""
        except Exception:
            out["fact"] = ""
    _cache = (time.time(), out)
    return out


def _forget() -> None:
    global _cache
    _cache = None


async def devices() -> list[dict[str, Any]]:
    data = await sp.client.api("GET", "/me/player/devices")
    return [{"id": d["id"], "name": d["name"], "type": d["type"], "active": d["is_active"], "volume": d.get("volume_percent")} for d in (data or {}).get("devices", [])]


async def _target() -> str | None:
    """A device to play on when nothing is active: this PC's desktop app first."""
    devs = await devices()
    if any(d["active"] for d in devs):
        return None
    host = socket.gethostname().lower()
    for pick in (lambda d: d["name"].lower() == host, lambda d: d["type"] == "Computer", lambda d: True):
        hit = next((d for d in devs if pick(d)), None)
        if hit:
            return hit["id"]
    raise ServiceError("Spotify isn’t open anywhere — press Open Spotify")


async def command(cmd: str, value: Any = None) -> None:
    c = sp.client
    if cmd == "play":
        await c.api("PUT", "/me/player/play", params=_dev(await _target()))
    elif cmd == "pause":
        await c.api("PUT", "/me/player/pause")
    elif cmd == "next":
        await c.api("POST", "/me/player/next")
    elif cmd == "previous":
        await c.api("POST", "/me/player/previous")
    elif cmd == "seek":
        await c.api("PUT", "/me/player/seek", params={"position_ms": max(0, int(value))})
    elif cmd == "volume":
        await c.api("PUT", "/me/player/volume", params={"volume_percent": max(0, min(100, int(value)))})
    elif cmd == "shuffle":
        await c.api("PUT", "/me/player/shuffle", params={"state": "true" if value else "false"})
    elif cmd == "repeat":
        await c.api("PUT", "/me/player/repeat", params={"state": value if value in ("off", "context", "track") else "off"})
    elif cmd == "transfer":
        await c.api("PUT", "/me/player", json={"device_ids": [str(value)], "play": True})
    elif cmd == "like":
        st = await state(fresh=True)
        uri = (st.get("track") or {}).get("uri")
        if not uri:
            raise ServiceError("Nothing is playing")
        await (c.unsave(uri) if value is False else c.save(uri))
        _liked[uri] = value is not False
    else:
        raise ValueError("unknown command")
    _forget()


def _dev(device_id: str | None) -> dict[str, str] | None:
    return {"device_id": device_id} if device_id else None


async def play_uris(uris: list[str], *, context: str | None = None) -> None:
    if not uris and not context:
        raise ServiceError("Couldn’t find that on Spotify")
    body = {"context_uri": context} if context else {"uris": uris[:100]}
    await sp.client.api("PUT", "/me/player/play", params=_dev(await _target()), json=body)
    _forget()


async def queue_uris(uris: list[str]) -> None:
    """Add tracks to the end of the queue, in order (Spotify takes them one at a time). Needs something playing or a
    device that's open."""
    if not uris:
        raise ServiceError("Couldn’t find that on Spotify")
    device = _dev(await _target())
    for uri in uris[:50]:
        await sp.client.api("POST", "/me/player/queue", params={"uri": uri, **(device or {})})
    _forget()


async def open_desktop() -> dict[str, Any]:
    """Start the Spotify desktop app here (if it isn't already) and wait for it to appear as a device."""
    argv = store.settings()["desktop_argv"]
    project = {"slug": UNIT, "name": "Spotify", "start_argv": json.dumps(argv), "cwd": os.path.expanduser("~"), "env": "{}"}
    try:
        await procs.start(project)
    except procs.ProcError as e:
        raise ServiceError(f"Couldn’t start Spotify: {e}")
    host = socket.gethostname().lower()
    for _ in range(10):                                  # every 3 s for half a minute: each look is a Spotify call
        await asyncio.sleep(3)
        try:
            devs = await devices()
        except ServiceError:
            continue
        mine = next((d for d in devs if d["name"].lower() == host or d["type"] == "Computer"), None)
        if mine:
            if not any(d["active"] for d in devs):
                try:
                    await sp.client.api("PUT", "/me/player", json={"device_ids": [mine["id"]], "play": False})
                except ServiceError:
                    pass
            _forget()
            return {"device": mine["name"]}
    raise ServiceError("Spotify started, but hasn’t connected yet — give it a moment")
