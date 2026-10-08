"""Spotify: controls the desktop app (or any of your devices) and keeps Citadel's playlists on your account.

Login. Spotify only allows a plain-http return address for 127.0.0.1, so the login finishes on the Citadel PC itself:
Settings asks for a login link, the link is opened in a browser on this PC, Spotify sends it back to
http://127.0.0.1:<port>/music/spotify/callback, and that page checks the one-time `state` it was issued with (no owner
cookie is needed there — 127.0.0.1 is a different site to the browser, so it wouldn't have one). After that every
device can use the page: the tokens stay on the server and are never sent to a browser.

Spotify's rules for personal ("development mode") apps, as of 2026, shape what's here: the owner needs Premium; a
login lasts six months from when it was granted, however often it's refreshed, then needs doing again (Settings warns
two weeks ahead); recommendations, related artists, audio features and artists' top tracks aren't available (Last.fm
and MusicBrainz stand in); search returns at most 10 results; playlists are created with POST /me/playlists and
filled through /playlists/{id}/items; Liked Songs are saved through /me/library.

Rate limits. Spotify allows each app some number of calls in a rolling 30-second window. It doesn't publish the number,
and a personal app's is low: on 2026-10-07 a Discover rebuild making about two searches a second earned Citadel a
19-hour ban. So every call goes through one Pacer, which keeps everything together to BUDGET calls a window, and
background work (Discover, playlist matching, rotations) to a trickle of its own. That leaves room for the player and
its buttons. A refusal (429) is never retried: its Retry-After is written down, and nothing at all is sent to
Spotify until that time has passed, restarts included.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import logging
import secrets
import time
import urllib.parse
from collections import deque
from datetime import datetime
from typing import Any

import httpx

from ... import config, db
from .. import store
from .base import Field, Service, ServiceError

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
SCOPES = ("playlist-modify-public playlist-modify-private user-read-playback-state user-modify-playback-state "
          "user-library-read user-library-modify")          # only what's used (a new set applies from the next Connect)
LOGIN_LIFETIME = 182 * 86400          # a login lasts six months from when it was granted
STATE_TTL = 15 * 60
OAUTH_KEY = "music-spotify-oauth"
COOLDOWN_KEY = "music-spotify-cooldown"
WINDOW = 30.0                         # Spotify counts calls over a rolling 30 seconds
BUDGET = 15                           # calls in any WINDOW, everything together
BACKGROUND_BUDGET = 5                 # of which background work may use this many...
BACKGROUND_GAP = 6.0                  # ...spread out, one at least this far apart
MIN_GAP = 0.2                         # and no two calls of any kind closer than this (a queued top 10 is ten calls)
DEFAULT_RETRY = 60                    # a 429 without a Retry-After (or a 503) waits this long the first time...
MAX_REST = 24 * 3600                  # ...doubling with each refusal in a row, up to this; a call that works starts again

log = logging.getLogger("hub.music")


def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/music/spotify/callback"


class SpotifyService(Service):
    def extra(self) -> dict[str, Any]:
        a = store.auth()
        connected = bool(a.get("refresh_token")) and not a.get("expired")
        until = (a.get("authorised_at") or 0) + LOGIN_LIFETIME if a.get("authorised_at") else None
        resting = resting_until()
        return {"redirect_uri": redirect_uri(), "connected": connected, "expired": bool(a.get("expired")),
                "resting_until": resting, "resting": resting_message(resting) if resting else "",
                "user": (a.get("user") or {}).get("display_name") or (a.get("user") or {}).get("id") or "",
                "login_until": until, "days_left": int((until - time.time()) // 86400) if until else None}

    def ready(self) -> bool:
        return self.enabled() and self.configured() and self.extra()["connected"]

    async def test(self) -> str:
        me = await client.api("GET", "/me")
        return f"Connected as {me.get('display_name') or me.get('id')}"


service = SpotifyService(
    id="spotify", name="Spotify", order=10,
    blurb="Plays on your desktop app or phone, and keeps Citadel’s playlists on your account. Needs Premium.",
    link="https://developer.spotify.com/dashboard",
    fields=(Field("client_id", "Client ID", placeholder="from your app in the Spotify developer dashboard"),
            Field("client_secret", "Client secret", secret=True)),
)


# ---- login ------------------------------------------------------------------------------------------------------

def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def login_url() -> str:
    cfg = service.cfg()
    if not service.configured(cfg):
        raise ServiceError("Add the Spotify app’s client ID and secret first")
    state = secrets.token_urlsafe(24)
    db.put_settings(OAUTH_KEY, {"state": _hash(state), "expires": int(time.time()) + STATE_TTL})
    return f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "client_id": cfg["client_id"], "response_type": "code", "redirect_uri": redirect_uri(),
        "scope": SCOPES, "state": state, "show_dialog": "true"})


def _basic(cfg: dict[str, Any]) -> str:
    return "Basic " + base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()


async def finish_login(code: str, state: str) -> str:
    """Spotify sent the browser back with a code. Returns who logged in."""
    pending = db.get_settings(OAUTH_KEY, {})
    db.put_settings(OAUTH_KEY, {})                                    # one use only, whatever happens next
    if not state or not pending.get("state") or not secrets.compare_digest(_hash(state), pending["state"]) or time.time() > pending.get("expires", 0):
        raise ServiceError("That login link has expired or was already used — start again from Settings → Music page")
    cfg = service.cfg()
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.post(f"{ACCOUNTS}/api/token", headers={"Authorization": _basic(cfg)},
                            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()})
        if r.status_code != 200:
            raise ServiceError(f"Spotify didn’t accept the login ({r.json().get('error_description', r.status_code) if r.headers.get('content-type', '').startswith('application/json') else r.status_code})")
        tok = r.json()
        me = await http.get(f"{API}/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
    user = me.json() if me.status_code == 200 else {}
    now = int(time.time())
    store.put_auth({"access_token": tok["access_token"], "refresh_token": tok["refresh_token"], "expires_at": now + int(tok.get("expires_in", 3600)) - 60,
                    "authorised_at": now, "scope": tok.get("scope", ""), "user": {"id": user.get("id", ""), "display_name": user.get("display_name", "")}})
    store.changed("spotify")
    return user.get("display_name") or user.get("id") or "your account"


def disconnect() -> None:
    store.put_auth({})
    store.changed("spotify")


# ---- staying within Spotify's limits ------------------------------------------------------------------------------

class Resting(ServiceError):
    """Spotify asked Citadel to wait, and that time hasn't come yet."""


_background = contextvars.ContextVar("spotify_background", default=False)


@contextlib.contextmanager
def background():
    """Calls made inside (and in tasks started inside) use the background trickle, not the page's share."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def resting_until() -> float | None:
    """When Spotify said Citadel may call again, if that's still to come."""
    until = db.get_settings(COOLDOWN_KEY, {}).get("until") or 0
    return until if until > time.time() else None


def _until_text(until: float) -> str:
    d = datetime.fromtimestamp(until, config.TZ)
    today = datetime.now(config.TZ).date()
    day = "" if d.date() == today else " tomorrow" if (d.date() - today).days == 1 else d.strftime(" on %a %d %b")
    return f"{d:%H:%M}{day}"


def resting_message(until: float) -> str:
    return f"Spotify asked Citadel to pause until {_until_text(until)} — it picks up again by itself then"


def _rest(retry_after: str | None) -> float:
    """Spotify refused a call (429, or 503 busy): wait as long as it says, and longer each time it happens again in a row."""
    cur = db.get_settings(COOLDOWN_KEY, {})
    strikes = int(cur.get("strikes") or 0) + 1
    try:
        asked = max(1, int(float(retry_after)))
    except (TypeError, ValueError):
        asked = 0
    wait = min(MAX_REST, max(asked, DEFAULT_RETRY * 2 ** (strikes - 1)))
    until = max(time.time() + wait, cur.get("until") or 0)
    db.put_settings(COOLDOWN_KEY, {"until": until, "since": time.time(), "retry_after": asked or None, "strikes": strikes})
    log.warning("Spotify refused a call (%s in a row; Retry-After %s): no Spotify calls until %s", strikes, asked or "none",
                _until_text(until), extra={"source": "music"})
    store.changed("spotify")
    return until


def _worked() -> None:
    """A call got through: the next refusal starts from the first step again."""
    cur = db.get_settings(COOLDOWN_KEY, {})
    if cur.get("strikes"):
        db.put_settings(COOLDOWN_KEY, {**cur, "strikes": 0})


class Pacer:
    """Hands out turns to call Spotify: at most BUDGET in any WINDOW, of which background work gets BACKGROUND_BUDGET,
    one per BACKGROUND_GAP, and never while the page's share is nearly used up."""
    def __init__(self) -> None:
        self._calls: deque[tuple[float, bool]] = deque()
        self._last = self._last_bg = -1e9

    def _wait(self, bg: bool, now: float) -> float:
        while self._calls and now - self._calls[0][0] >= WINDOW:
            self._calls.popleft()
        waits = [self._last + MIN_GAP - now]
        limit = BUDGET - (BUDGET - BACKGROUND_BUDGET) // 2 if bg else BUDGET       # background leaves half the rest free
        if len(self._calls) >= limit:
            waits.append(self._calls[len(self._calls) - limit][0] + WINDOW - now)
        if bg:
            mine = [t for t, b in self._calls if b]
            waits.append(self._last_bg + BACKGROUND_GAP - now)
            if len(mine) >= BACKGROUND_BUDGET:
                waits.append(mine[len(mine) - BACKGROUND_BUDGET] + WINDOW - now)
        return max(waits)

    async def turn(self, bg: bool | None = None) -> None:
        bg = _background.get() if bg is None else bg
        while (wait := self._wait(bg, time.monotonic())) > 0:
            await asyncio.sleep(wait)
        now = time.monotonic()
        self._calls.append((now, bg))
        self._last = now
        if bg:
            self._last_bg = now

    def reset(self) -> None:
        self._calls.clear()
        self._last = self._last_bg = -1e9


pacer = Pacer()


# ---- API client ---------------------------------------------------------------------------------------------------

class Client:
    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._refresh_lock = asyncio.Lock()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20)
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def _token(self, force: bool = False) -> str:
        a = store.auth()
        if not a.get("refresh_token") or a.get("expired"):
            raise ServiceError("Spotify isn’t connected — connect it in Settings → Music page")
        if not force and a.get("access_token") and time.time() < a.get("expires_at", 0):
            return a["access_token"]
        async with self._refresh_lock:
            a = store.auth()
            if not force and a.get("access_token") and time.time() < a.get("expires_at", 0):
                return a["access_token"]
            r = await self._client().post(f"{ACCOUNTS}/api/token", headers={"Authorization": _basic(service.cfg())},
                                          data={"grant_type": "refresh_token", "refresh_token": a["refresh_token"]})
            if r.status_code in (400, 401):
                store.put_auth({**a, "access_token": "", "expired": True})
                store.changed("spotify")
                raise ServiceError("The Spotify login has run out (they last six months) — reconnect in Settings → Music page")
            r.raise_for_status()
            tok = r.json()
            store.put_auth({**a, "access_token": tok["access_token"], "refresh_token": tok.get("refresh_token") or a["refresh_token"],
                            "expires_at": int(time.time()) + int(tok.get("expires_in", 3600)) - 60})
            return tok["access_token"]

    async def api(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: Any = None) -> Any:
        """One Web API call, when the Pacer allows. Returns the decoded body (None for an empty one); raises ServiceError in
        plain words (Resting while Spotify has asked Citadel to wait)."""
        if not service.enabled():
            raise ServiceError("Spotify is switched off in Settings → Music page")
        if until := resting_until():
            raise Resting(resting_message(until))
        for attempt in range(2):
            await pacer.turn()
            if until := resting_until():                 # checked after the wait too: another call may have been refused meanwhile
                raise Resting(resting_message(until))
            token = await self._token(force=attempt > 0)
            r = await self._client().request(method, API + path, params=params, json=json, headers={"Authorization": f"Bearer {token}"})
            if r.status_code in (429, 503):              # never retried: wait as long as Spotify says, everything included
                raise Resting(resting_message(_rest(r.headers.get("Retry-After"))))
            if r.status_code != 401:
                break
        if r.status_code < 400:
            _worked()
        if r.status_code in (202, 204) or not r.content:
            if r.status_code < 300:
                return None
        if r.status_code >= 400:
            msg = ""
            try:
                msg = (r.json().get("error") or {}).get("message", "")
            except ValueError:
                pass
            if r.status_code == 404 and path.startswith("/me/player"):
                raise ServiceError("No Spotify device is active — open Spotify (or press Open Spotify) first")
            if r.status_code == 403:
                raise ServiceError(f"Spotify refused that{': ' + msg if msg else ''}")
            raise ServiceError(f"Spotify: {msg or r.status_code}")
        try:
            return r.json()
        except ValueError:                       # player commands answer 200 with a body that isn't JSON: success, nothing to read
            return None

    # ---- search ----
    async def search(self, q: str, kind: str = "track", limit: int = 10) -> list[dict[str, Any]]:
        data = await self.api("GET", "/search", params={"q": q, "type": kind, "limit": max(1, min(limit, 10))})
        return ((data or {}).get(f"{kind}s") or {}).get("items") or []

    # ---- playlists ----
    async def create_playlist(self, name: str, public: bool, description: str) -> dict[str, Any]:
        return await self.api("POST", "/me/playlists", json={"name": name, "public": public, "description": description})

    async def update_playlist(self, playlist_id: str, name: str, public: bool, description: str) -> None:
        await self.api("PUT", f"/playlists/{playlist_id}", json={"name": name, "public": public, "description": description})

    async def set_items(self, playlist_id: str, uris: list[str]) -> None:
        """Replace everything in a playlist (100 at a time, Spotify's limit per call)."""
        await self.api("PUT", f"/playlists/{playlist_id}/items", json={"uris": uris[:100]})
        for i in range(100, len(uris), 100):
            await self.api("POST", f"/playlists/{playlist_id}/items", json={"uris": uris[i:i + 100]})

    async def album_tracks(self, album_id: str) -> list[dict[str, Any]]:
        data = await self.api("GET", f"/albums/{album_id}/tracks", params={"limit": 50})
        return (data or {}).get("items") or []

    # ---- library ----
    async def save(self, uri: str) -> None:
        await self.api("PUT", "/me/library", params={"uris": uri})

    async def unsave(self, uri: str) -> None:
        await self.api("DELETE", "/me/library", params={"uris": uri})

    async def saved(self, uri: str) -> bool:
        data = await self.api("GET", "/me/library/contains", params={"uris": uri})
        return bool(data and data[0])


client = Client()
