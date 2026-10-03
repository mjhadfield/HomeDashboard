"""Music page settings, the outside-services response cache, and change notifications.

Settings live in widget_settings under 'music' (what Settings → Music page edits) and the Spotify login under
'music-spotify' (never sent to a browser). Each service's own fields sit under settings["services"][<id>], so a new
service needs no change here.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .. import db, logbook
from ..bus import bus

KEY = "music"
AUTH_KEY = "music-spotify"
PREFIX_RE = re.compile(r"^[^\x00-\x1f]{0,24}$")

DEFAULTS: dict[str, Any] = {
    "prefix": "Citadel · ",              # every playlist Citadel makes on Spotify is named with this in front
    "public": True,                      # new playlists start public
    "rotate_at": "06:00",                # default time rotating playlists rebuild
    "desktop_argv": ["flatpak", "run", "com.spotify.Client"],   # how "Open Spotify" starts the desktop app
    "services": {},                      # <service id> -> {"enabled": bool, <its own fields>}
}


def settings() -> dict[str, Any]:
    s = db.get_settings(KEY, DEFAULTS)
    s["services"] = {**s.get("services", {})}
    return s


def service_cfg(service_id: str) -> dict[str, Any]:
    return {"enabled": True, **settings()["services"].get(service_id, {})}


def put_settings(values: dict[str, Any]) -> None:
    db.put_settings(KEY, values)


def auth() -> dict[str, Any]:
    return db.get_settings(AUTH_KEY, {})


def put_auth(values: dict[str, Any]) -> None:
    logbook.learn([values.get("access_token", ""), values.get("refresh_token", "")])      # never let a token reach the event log
    db.put_settings(AUTH_KEY, values)


def learn_secrets() -> None:
    """Tell the event log every music secret (service keys, Spotify tokens) so it blanks them wherever they appear."""
    a = auth()
    values = [a.get("access_token", ""), a.get("refresh_token", "")]
    for cfg in settings()["services"].values():
        values += [v for k, v in cfg.items() if isinstance(v, str) and k in ("api_key", "client_secret", "client_id")]
    logbook.learn(values)


# ---- cache -------------------------------------------------------------------------------------------------------

def cache_get(key: str, max_age: float) -> Any | None:
    with db.db() as con:
        row = con.execute("SELECT json, fetched_at FROM music_cache WHERE key = ?", (key,)).fetchone()
    if not row or time.time() - row["fetched_at"] > max_age:
        return None
    return json.loads(row["json"])


def cache_put(key: str, value: Any) -> None:
    with db.db() as con:
        con.execute("INSERT INTO music_cache (key, json, fetched_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET json = excluded.json, fetched_at = excluded.fetched_at",
                    (key, json.dumps(value), int(time.time())))


def cache_clear(prefix: str) -> int:
    with db.db() as con:
        return con.execute("DELETE FROM music_cache WHERE key LIKE ?", (prefix.replace("%", "") + "%",)).rowcount


async def cached(key: str, max_age: float, fetch) -> Any:
    """The cached answer if it's fresh enough, else ask (and remember)."""
    hit = cache_get(key, max_age)
    if hit is not None:
        return hit
    value = await fetch()
    cache_put(key, value)
    return value


# ---- change notifications (a trigger only; the page re-fetches what it shows) ---------------------------------------

def changed(what: str = "") -> None:
    bus.publish("music", {"what": what})
