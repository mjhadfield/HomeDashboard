"""First-run seed data: the projects and links Mike starts with. Everything here is editable afterwards,
and seeding only fills empty tables — it never overwrites what has been changed in the database.

Bootstrap values (calendar URL, UPRN, Jellyfin address) come from .env on the first run only; after that the
database is the source of truth, and they are changed through the app.
"""
from __future__ import annotations

import json
import os
import struct
from typing import Any
from urllib.parse import urlsplit

from . import config, db
from .db import get_settings, put_settings

P = config.PROJECTS_DIR

PROJECTS: list[dict[str, Any]] = [
    dict(slug="music-companion", name="Music Companion", description="Vinyl, scrobbles and gigs", kind="web", sort=10,
         url="http://{host}:8642/", port=8642, cwd=str(P / "MusicDatabase"),
         argv=["python3", "-m", "http.server", "8642", "--directory", "site"]),
    dict(slug="setlist-spotify", name="Setlist → Spotify", description="setlist.fm to Spotify playlists", kind="web", sort=20,
         url="http://127.0.0.1:8888/", port=8888, cwd=str(P / "SpotifyPlaylists"), lan_visible=0,   # binds loopback; OAuth redirect is loopback too
         argv=["venv/bin/python", "app.py"]),
    dict(slug="star-epr", name="Star EPR", description="Dummy patient record front-end", kind="web", sort=30,
         url="http://{ip}:5173/StarEPR/", port=5173, cwd=str(P / "StarEPR"),   # {ip}: Vite blocks unknown hostnames, not IPs
         argv=["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"], env={"BROWSER": "none"}),  # BROWSER=none: don't pop a window on start
    dict(slug="terminal", name="Terminal", description="pygame terminal (web build)", kind="web", sort=40,
         url="http://{host}:8000/", port=8000, cwd=str(P / "Terminal"),
         argv=[".venv/bin/python", "-m", "pygbag", "--bind", "0.0.0.0", "--template", "web-template/loader.tmpl",
               "--title", "Mike Hadfield Terminal", "--width", "900", "--height", "560", "."]),
    dict(slug="hadfield-casino", name="Hadfield Casino", description="Casino games (desktop app)", kind="desktop", sort=50,
         url=None, port=None, cwd=str(P / "Casino"), argv=["python3", "main.py"]),
]


def default_gateway() -> str | None:
    try:
        with open("/proc/net/route") as f:
            for line in list(f)[1:]:
                iface, dest, gw, flags, *_ = line.split()
                if dest == "00000000" and int(flags, 16) & 2:
                    return ".".join(str(b) for b in struct.pack("<I", int(gw, 16)))
    except OSError:
        pass
    return None


def links() -> list[dict[str, Any]]:
    jelly = os.environ.get("JELLYFIN_URL", "").strip()
    out: list[dict[str, Any]] = []
    if jelly:
        parts = urlsplit(jelly)
        out.append(dict(name="Jellyfin", subtitle=parts.netloc, url=jelly, icon="film", is_primary=1, show_guest=1,
                        health_url=f"{parts.scheme}://{parts.netloc}/health"))
    if gw := default_gateway():
        out.append(dict(name="Router", subtitle=gw, url=f"http://{gw}/", icon="router", health_url=f"http://{gw}/"))
    out += [
        dict(name="Music Companion", subtitle=":8642", url="http://127.0.0.1:8642/", icon="disc", health_url="http://127.0.0.1:8642/"),
        dict(name="Discogs", subtitle="collection", url="https://www.discogs.com/", icon="disc"),
        dict(name="Setlist.fm", subtitle="attended", url="https://www.setlist.fm/", icon="bolt"),
        dict(name="Last.fm", subtitle="scrobbles", url="https://www.last.fm/", icon="disc"),
        dict(name="Bandcamp", subtitle="feed", url="https://bandcamp.com/", icon="globe"),
        dict(name="Spotify", subtitle="web player", url="https://open.spotify.com/", icon="play"),
        dict(name="GitHub", subtitle="mjhadfield", url="https://github.com/mjhadfield", icon="term"),
        dict(name="mikehadfield.co.uk", subtitle="public site", url="https://www.mikehadfield.co.uk/", icon="globe", tier="more"),
        dict(name="Ollama", subtitle=":11434", url="http://127.0.0.1:11434/", icon="chip", health_url="http://127.0.0.1:11434/", tier="more"),
    ]
    return out


def seed(force: bool = False) -> dict[str, int]:
    """Fill empty tables once. Later runs do nothing unless force=True (which still only fills what is empty),
    so deleting every link in the UI does not bring the defaults back on the next restart."""
    added = {"projects": 0, "links": 0, "settings": 0}
    meta = get_settings("_meta", {})
    if meta.get("seeded") and not force:
        return added
    with db.db() as con:
        if con.execute("SELECT COUNT(*) c FROM projects").fetchone()["c"] == 0:
            for p in PROJECTS:
                con.execute(
                    "INSERT INTO projects (slug, name, description, kind, url, port, cwd, start_argv, env, lan_visible, sort) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p["slug"], p["name"], p["description"], p["kind"], p["url"], p["port"], p["cwd"],
                     json.dumps(p["argv"]), json.dumps(p.get("env", {})), p.get("lan_visible", 1), p["sort"]),
                )
                added["projects"] += 1
        if con.execute("SELECT COUNT(*) c FROM links").fetchone()["c"] == 0:
            for i, l in enumerate(links()):
                con.execute(
                    "INSERT INTO links (name, subtitle, url, icon, health_url, is_primary, show_guest, tier, sort) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (l["name"], l["subtitle"], l["url"], l["icon"], l.get("health_url"), l.get("is_primary", 0),
                     l.get("show_guest", 0), l.get("tier", "main"), i * 10),
                )
                added["links"] += 1

    # Widget settings: only fill what's still empty, so re-running never clobbers UI changes.
    bins = get_settings("bins", {"uprn": ""})
    if not bins.get("uprn") and (uprn := os.environ.get("BINS_UPRN", "").strip()):
        put_settings("bins", {**bins, "uprn": uprn})
        added["settings"] += 1
    diary = get_settings("diary", {"calendars": []})
    if not diary.get("calendars") and (url := os.environ.get("DIARY_ICAL_URL", "").strip()):
        put_settings("diary", {**diary, "calendars": [{"name": "Google Calendar", "url": url, "colour": "violet"}]})
        added["settings"] += 1
    put_settings("_meta", {**meta, "seeded": True})
    return added
