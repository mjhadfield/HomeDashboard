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
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
import urllib.parse
from typing import Any

import httpx

from ... import config, db
from .. import store
from .base import Field, Service, ServiceError

ACCOUNTS = "https://accounts.spotify.com"
API = "https://api.spotify.com/v1"
SCOPES = ("playlist-modify-public playlist-modify-private playlist-read-private user-read-playback-state "
          "user-modify-playback-state user-read-currently-playing user-library-read user-library-modify")
LOGIN_LIFETIME = 182 * 86400          # a login lasts six months from when it was granted
STATE_TTL = 15 * 60
OAUTH_KEY = "music-spotify-oauth"


def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/music/spotify/callback"


class SpotifyService(Service):
    def extra(self) -> dict[str, Any]:
        a = store.auth()
        connected = bool(a.get("refresh_token")) and not a.get("expired")
        until = (a.get("authorised_at") or 0) + LOGIN_LIFETIME if a.get("authorised_at") else None
        return {"redirect_uri": redirect_uri(), "connected": connected, "expired": bool(a.get("expired")),
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
        """One Web API call. Returns the decoded body (None for an empty one); raises ServiceError in plain words."""
        if not service.enabled():
            raise ServiceError("Spotify is switched off in Settings → Music page")
        token = await self._token()
        for attempt in range(3):
            r = await self._client().request(method, API + path, params=params, json=json, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 0:
                token = await self._token(force=True)
                continue
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", "2"))
                if wait > 15 or attempt == 2:
                    raise ServiceError("Spotify is rate-limiting Citadel — try again in a minute")
                await asyncio.sleep(wait)
                continue
            break
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
