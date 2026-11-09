#!/usr/bin/env python3
"""Build Citadel's online demo: a frozen, read-only copy of the dashboard for mikehadfield.co.uk.

    .venv/bin/python tools/build_demo.py [--out ~/Projects/webserver/demo/citadel]

Citadel's frontend is plain ES modules reading a handful of /api/owner/* GETs, so a static copy can replay them. This:

1. copies the live database into a scratch folder (SQLite backup API, read-only on the original) and starts the app
   in-process on that copy with HUB_NO_FETCH=1, so the live service, its data and outside sites are left alone;
2. saves the GET responses the dashboard, News and Music pages ask for, as data/<key>.json;
3. censors them: bookmarks, the shortcut row, addresses, booked-gig order links and notes, calendar events (swapped
   for made-up ones), machine names, and reminders that could name any of those;
4. copies web/ with its absolute /static/ paths made relative, and adds demo.js (tools/demo/), which answers /api/*
   from data/, refuses every change, stubs the event stream and freezes the clock at the snapshot time;
5. fails if any address, home path, calendar link or known secret turns up anywhere in the output.

Everything written to --out becomes public once the website repo is pushed (its mirror publishes every committed file).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LIVE_DATA = ROOT / "data"
WEB = ROOT / "web"
TEMPLATES = Path(__file__).resolve().parent / "demo"
DEFAULT_OUT = Path.home() / "Projects/webserver/demo/citadel"
MARKER = "demo-build.json"          # written into every build; a folder without it is never wiped

COMPANION_URL = "https://mjhadfield.github.io/music-companion/"
KEEP_ALERTS = {"bin", "release", "f1", "onsale", "fresh", "vinyl", "gig"}   # not: event (calendar), down, system, maintenance
MONITOR_FRAMES, MONITOR_EVERY = 20, 1.5

# The pages that only exist on the home network. Icons are Citadel's own (icons.js).
NOT_HERE = {
    "media": ("film", "Media runs on a Jellyfin server on my home network."),
    "ai": ("spark", "Odysseus is a self-hosted AI app on my home network."),
    "house": ("tool", "House is a private repair tracker for my home."),
}

# Made-up diary entries, (days from the snapshot, start, end, title); None = all day.
SAMPLE_DIARY = [
    (0, "09:00", "13:00", "Record fair"),
    (2, "19:30", "21:30", "Band practice"),
    (4, None, None, "Bin day swap — check the council site"),
    (6, "10:15", "10:45", "Dentist"),
    (9, "18:00", None, "Pub quiz"),
    (13, None, None, "Payday!"),
    (17, "08:30", "09:30", "Car MOT"),
    (24, "19:00", None, "Cinema"),
]


def key_of(path: str) -> str:
    """The file a GET is saved as. Must match keyOf() in tools/demo/demo.js."""
    rest = path.removeprefix("/api/owner")
    return re.sub(r"[^A-Za-z0-9]+", "_", rest).strip("_") or "index"


# ---- 1. a scratch copy, and the app running on it -----------------------------------------------------------------------

def make_scratch() -> Path:
    scratch = Path(tempfile.mkdtemp(prefix="citadel-demo-"))
    src = sqlite3.connect(f"file:{LIVE_DATA / 'hub.sqlite'}?mode=ro", uri=True)
    dst = sqlite3.connect(scratch / "hub.sqlite")
    src.backup(dst)
    dst.close()
    src.close()
    shutil.copy2(LIVE_DATA / "secret.key", scratch / "secret.key")
    os.chmod(scratch, 0o700)
    return scratch


def snapshot(scratch: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Every response the demo needs, plus the monitoring readings and the secret values to check the output against."""
    os.environ.update(HUB_DATA_DIR=str(scratch), HUB_NO_FETCH="1", HUB_PORT="18097")
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient

    from app import auth, logbook
    from app.main import app
    from app.music import discover
    from app.widgets import discover as widgets
    from app import db

    out: dict[str, Any] = {}
    with TestClient(app) as c:
        token = auth.create_enrol_token("owner", "Online demo build")
        r = c.post(f"/enrol/{token}", follow_redirects=False)
        assert r.status_code == 303, r.text
        c.headers["X-Hub"] = "1"
        print("waiting for health probes and the monitor…")
        time.sleep(5)

        def get(path: str) -> Any:
            r = c.get(path)
            r.raise_for_status()
            out[path] = r.json()
            return out[path]

        get("/api/owner/state")
        news = get("/api/owner/news")
        get("/api/owner/news/front")
        get("/api/owner/news/archive?")
        for s in news["sections"]:
            for q in ("", "?everything=true"):
                get(f"/api/owner/news/section/{s['id']}{q}")
            get(f"/api/owner/news/archive?section={s['id']}")
        music = get("/api/owner/music")
        for pl in music.get("playlists", []):
            get(f"/api/owner/music/playlists/{pl['id']}")
        for basis in discover.BASES:
            if discover.cached(basis) is not None:          # an uncached basis would start a Discover run; leave it out
                get(f"/api/owner/music/discover?basis={basis}")
        get("/api/owner/music/rediscover")
        get("/api/owner/music/gigs")
        # No Spotify call for the player: the demo just shows "Nothing playing".
        out["/api/owner/music/player"] = {"ready": True, "active": False, "playing": False}

        frames = []
        for _ in range(MONITOR_FRAMES):
            frames.append(c.get("/api/owner/monitoring").json())
            time.sleep(MONITOR_EVERY)

        secrets = set(logbook._known)
        for w in widgets().values():
            settings = db.get_settings(w.id, w.defaults)
            secrets.update(w.secrets(settings))
            for k in ("latlong", "uprn", "cal_url"):
                if isinstance(settings.get(k), str):
                    secrets.add(settings[k])
    return out, frames, sorted(s for s in secrets if isinstance(s, str) and len(s.strip()) >= 6)


# ---- 2. censoring ----------------------------------------------------------------------------------------------------------

def sample_diary(day: date) -> list[dict[str, Any]]:
    return [{"date": (day + timedelta(days=d)).isoformat(), "until": None, "time": start, "end_time": end, "all_day": start is None,
             "title": title, "location": "", "cal": "Google Calendar", "colour": "violet"} for d, start, end, title in SAMPLE_DIARY]


def clean_gig(g: dict[str, Any]) -> dict[str, Any]:
    return {**g, "notes": "", "ticket_url": None}          # booked tickets link to Ticketmaster *orders*


def censor_state(s: dict[str, Any]) -> dict[str, Any]:
    day = datetime.fromisoformat(s["now"]).date()
    s["host"], s["ip"] = "Online demo", ""
    s["device"] = {"id": 0, "name": "Demo", "role": "owner"}
    s["links"] = [{"id": 1, "name": "Music Companion", "subtitle": "The public copy", "url": COMPANION_URL, "icon": "disc",
                   "primary": False, "tier": "main", "pinned": False, "pin_order": None,
                   "for_media": False, "for_music": True, "for_sql": False, "for_ai": False, "for_house": False, "up": True}]
    s["projects"] = [{**p, "url": None} for p in s["projects"]]     # no addresses; ports and "this PC only" are harmless
    s["health"] = {k: v for k, v in s["health"].items() if k.startswith("project:")}
    s["alerts"] = [a for a in s["alerts"] if a.get("kind") in KEEP_ALERTS]

    w = s["widgets"]
    if "shortcuts" in w and w["shortcuts"].get("data"):
        w["shortcuts"]["data"] = {"show": False, "shortcuts": [], "failed": []}
    if "diary" in w and w["diary"].get("data"):
        w["diary"]["data"]["events"] = sample_diary(day)
    if "gigs" in w and (g := w["gigs"].get("data")):
        if g.get("next"):
            g["next"] = clean_gig(g["next"])
        for k in ("upcoming", "past"):
            g[k] = [clean_gig(x) for x in g.get(k) or []]
    if "system" in w and (sysd := w["system"].get("data")):
        sysd["host"] = "citadel"
        disks = []
        for d in sysd.get("disks", []):
            if d.get("mount") in ("/", "/boot", "/home"):
                disks.append(d)
            elif d.get("name") == "NAS":
                disks.append({**d, "mount": "/mnt/nas"})
        sysd["disks"] = disks
    return s


def censor_music(path: str, data: dict[str, Any]) -> dict[str, Any]:
    if path == "/api/owner/music":
        data["settings"] = {k: v for k, v in data.get("settings", {}).items() if k != "desktop_command"}
        data.pop("services", None)                          # Settings' service forms (paths, client ids): the Music page never reads them
        if isinstance(data.get("spotify"), dict):
            data["spotify"]["redirect_uri"] = ""
    if path == "/api/owner/music/gigs":
        data["booked"] = [clean_gig(g) for g in data.get("booked", [])]
    return data


# ---- 3. the frontend -------------------------------------------------------------------------------------------------------

def must_replace(text: str, old: str, new: str, where: str, count: int | None = None) -> str:
    n = text.count(old)
    if not n or (count is not None and n != count):
        raise SystemExit(f"build_demo: expected {count or 'some'} of {old!r} in {where}, found {n} — Citadel's code has moved; update the build")
    return text.replace(old, new)


def copy_frontend(out: Path, snap_label: str) -> None:
    for sub in ("css", "js", "fonts", "img"):
        shutil.copytree(WEB / sub, out / sub)
    for js in (out / "js").glob("*.js"):
        if js.name in ("settings.js", "tv.js", "guest.js", "pair.js"):
            js.unlink()                                       # pages the demo doesn't have
    (out / "css" / "settings.css").unlink(missing_ok=True)
    (out / "css" / "tv.css").unlink(missing_ok=True)
    (out / "css" / "guest.css").unlink(missing_ok=True)
    (out / "css" / "jellyfin-theme.css").unlink(missing_ok=True)

    def edit(rel: str, *pairs: tuple[str, str]) -> None:
        p = out / rel
        text = p.read_text()
        for old, new in pairs:
            text = must_replace(text, old, new, rel)
        p.write_text(text)

    edit("js/music.js",
         ("/static/img/", "img/"),
         (", ['buying', 'Worth buying', 'bag']]", "]"))          # Discogs' terms restrict republishing prices
    edit("js/content-cards.js", ("startsWith('/covers/')", "startsWith('covers/')"))
    edit("js/app.js", ('src="/backgrounds/', 'src="backgrounds/'),
         ("let MUSIC_ORIGIN = 'http://192.168.0.66:8642';", f"let MUSIC_ORIGIN = '{COMPANION_URL.split('/', 3)[0]}//{COMPANION_URL.split('/', 3)[2]}';"))
    # A url() in a custom property set from script is resolved against… it depends; an absolute URL settles it.
    edit("js/theme.js", ('`url("/backgrounds/${bg.file}")`', '`url("${new URL(`backgrounds/${bg.file}`, document.baseURI).href}")`'))

    html = (WEB / "index.html").read_text()
    html = must_replace(html, '"/static/', '"', "index.html")
    html = must_replace(html, '<script src="js/theme-init.js"></script>',
                        '<script src="demo.js"></script>\n<script src="js/theme-init.js"></script>', "index.html", 1)
    html = must_replace(html, '<link rel="stylesheet" href="css/news.css">',
                        '<link rel="stylesheet" href="css/news.css">\n<link rel="stylesheet" href="demo.css">', "index.html", 1)
    html = must_replace(html, '<title>Citadel</title>', '<title>Citadel · online demo</title>', "index.html", 1)
    html = must_replace(html, '<span class="hud" id="host-label"></span>',
                        f'<span class="hud" id="host-label"></span><span class="chip demo-chip" title="A frozen copy of my home dashboard: nothing here is live or saved">Snapshot · {snap_label} · read-only</span>',
                        "index.html", 1)
    for view, (icon, why) in NOT_HERE.items():
        html = re.sub(rf'(<section class="view" id="view-{view}"[^>]*>\s*)<div class="frame-shell">',
                      rf'\1<div class="frame-shell" hidden>', html, count=1)
        old = f'id="{view}-frame"'
        i = html.find(old)
        if i < 0:
            raise SystemExit(f"build_demo: no #{view}-frame in index.html")
        end = html.find("</div>", i) + len("</div>")
        html = html[:end] + (f'\n      <div class="demo-na"><svg class="i"><use href="#i-{icon}"/></svg>'
                             f'<b>Not available in the online demo</b><span>{why}</span></div>') + html[end:]
    if html.count('class="frame-shell" hidden') != len(NOT_HERE):
        raise SystemExit("build_demo: couldn't hide every home-network frame in index.html")
    (out / "index.html").write_text(html)

    for p in out.rglob("*"):
        if p.suffix in (".js", ".css", ".html") and "/static/" in p.read_text():
            raise SystemExit(f"build_demo: an absolute /static/ path is left in {p.relative_to(out)}")


def rewrite_paths(obj: Any, files: set[str]) -> Any:
    """Same-origin pictures (/covers/…, /backgrounds/…) become relative, and are noted to be copied in."""
    if isinstance(obj, dict):
        out = {k: rewrite_paths(v, files) for k, v in obj.items()}
        bg = out.get("background")
        if isinstance(bg, dict) and isinstance(bg.get("file"), str):
            files.add(f"backgrounds/{bg['file']}")
        return out
    if isinstance(obj, list):
        return [rewrite_paths(v, files) for v in obj]
    if isinstance(obj, str) and obj.startswith(("/covers/", "/backgrounds/")):
        files.add(obj[1:])
        return obj[1:]
    return obj


# Covers show as small thumbnails and backgrounds fill a screen at most, but the originals are full-size scans (a few MB
# each). ImageMagick, when it's installed, makes web-sized JPEGs; otherwise the originals are copied as they are.
PICTURE_MAX = {"covers": 400, "backgrounds": 1920}


def copy_pictures(out: Path, files: set[str]) -> dict[str, str]:
    """Copy the referenced pictures in; returns {old file name: new file name} for any that became .jpg."""
    renamed: dict[str, str] = {}
    magick = shutil.which("magick")
    for rel in sorted(files):
        src = LIVE_DATA / rel
        if not src.is_file():
            print(f"note: {rel} is referenced but missing from data/; it will show as a broken picture")
            continue
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if magick and src.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            jpg = dst.with_suffix(".jpg")
            size = PICTURE_MAX.get(Path(rel).parts[0], 1200)
            r = subprocess.run([magick, str(src), "-auto-orient", "-resize", f"{size}x{size}>", "-background", "black", "-flatten",
                                "-strip", "-quality", "82", str(jpg)], capture_output=True)
            if r.returncode == 0 and jpg.stat().st_size < src.stat().st_size:
                if jpg.name != src.name:
                    renamed[src.name] = jpg.name
                continue
            jpg.unlink(missing_ok=True)
        shutil.copy2(src, dst)
    return renamed


# ---- 4. the leak check -----------------------------------------------------------------------------------------------------

LEAKS = [
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}"), "a LAN address"),
    (re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "a Tailscale address"),
    (re.compile(r"/home/mike"), "a home folder path"),
    (re.compile(r"calendar\.google\.com/calendar/ical|/private-[0-9A-Za-z_-]{8,}"), "a calendar address"),
    (re.compile(r"ticketmaster\.[a-z.]+/user/"), "a Ticketmaster order link"),
    (re.compile(r'"Gaming"'), "the PC's hostname"),            # not checked in news files: "Gaming" is a real topic tag there
    (re.compile(r"/enrol/[A-Za-z0-9_-]{8,}"), "an enrolment link"),
    (re.compile(r"@gmail\.com"), "an email address"),
]


def env_values() -> list[str]:
    vals = []
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if len(v) >= 6:
                    vals.append(v)
    return vals


def leak_check(out: Path, secrets: list[str]) -> None:
    problems = []
    for p in sorted(out.rglob("*")):
        if not p.is_file() or p.suffix not in (".json", ".js", ".css", ".html"):
            continue
        text = p.read_text(errors="replace")
        rel = str(p.relative_to(out))
        for rx, what in LEAKS:
            if what == "the PC's hostname" and rel.startswith("data/news"):
                continue
            if m := rx.search(text):
                problems.append(f"{p.relative_to(out)}: {what} ({m.group(0)[:40]!r})")
        for s in secrets:
            if s in text:
                problems.append(f"{p.relative_to(out)}: a secret value (from .env or a widget's settings)")
    if problems:
        print("\n".join(problems), file=sys.stderr)
        raise SystemExit("build_demo: the output contains private data — nothing should be published until this is fixed")


# ---- main --------------------------------------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"where the demo goes (default {DEFAULT_OUT})")
    args = ap.parse_args()
    out: Path = args.out.expanduser().resolve()
    if out.exists() and any(out.iterdir()) and not (out / MARKER).exists():
        raise SystemExit(f"build_demo: {out} isn't empty and isn't an earlier demo build ({MARKER} missing); not touching it")

    scratch = make_scratch()
    try:
        responses, frames, secrets = snapshot(scratch)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    secrets = sorted(set(secrets) | set(env_values()))

    state = responses["/api/owner/state"]
    snap_at = datetime.fromisoformat(state["now"])
    snap_label = f"{snap_at.astimezone():%-d %b %Y}"

    files: set[str] = set()
    cleaned: dict[str, Any] = {}
    for path, data in responses.items():
        if path == "/api/owner/state":
            data = censor_state(data)
        elif path.startswith("/api/owner/music"):
            data = censor_music(path, data)
        cleaned[path] = rewrite_paths(data, files)

    tmp_out = out.with_name(out.name + ".building")
    shutil.rmtree(tmp_out, ignore_errors=True)
    tmp_out.mkdir(parents=True)
    copy_frontend(tmp_out, snap_label)
    renamed = copy_pictures(tmp_out, files)
    (tmp_out / "data").mkdir()
    for path, data in cleaned.items():
        text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        for old_name, new_name in renamed.items():
            text = text.replace(old_name, new_name)
        (tmp_out / "data" / f"{key_of(path)}.json").write_text(text)
    (tmp_out / "data" / "monitoring_frames.json").write_text(json.dumps(frames, separators=(",", ":")))
    # what was saved, so demo.js answers "not available" for anything else without asking the server (no 404s)
    (tmp_out / "data" / "index.json").write_text(json.dumps(sorted([key_of(p) for p in cleaned] + ["monitoring_frames"])))
    shim = (TEMPLATES / "demo.js").read_text().replace("__SNAPSHOT_MS__", str(int(snap_at.timestamp() * 1000)))
    (tmp_out / "demo.js").write_text(shim)
    shutil.copy2(TEMPLATES / "demo.css", tmp_out / "demo.css")
    (tmp_out / MARKER).write_text(json.dumps({"snapshot": state["now"], "built": datetime.now().astimezone().isoformat(timespec="seconds"),
                                              "by": "HomeDashboard/tools/build_demo.py"}, indent=2) + "\n")

    leak_check(tmp_out, secrets)

    shutil.rmtree(out, ignore_errors=True)
    tmp_out.rename(out)
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"saved {len(cleaned)} responses + {len(frames)} monitoring readings, {len(files)} pictures")
    for path in sorted(cleaned):
        print(f"  {path}")
    print(f"demo written to {out} ({size / 1e6:.1f} MB), snapshot {snap_label}; leak check passed")


if __name__ == "__main__":
    main()
