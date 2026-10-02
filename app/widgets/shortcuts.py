"""Shortcuts: a row of your own web links across the top of the dashboard, for using Citadel as the browser's
home page (the Launchpad is left for applications).

Each shortcut has a label, an address and an icon, one of:
  site     the site's own icon, found and downloaded by the server (apple-touch-icon, then the best <link rel=icon>,
           then /favicon.ico)
  builtin  one of Citadel's own icons
  image    an image address of your choosing, downloaded once
  letter   just the label's first letter
Downloaded icons are stored in the data folder and served by Citadel itself (see main.py), because the dashboard's
CSP only lets the browser load images from a few named CDNs. A refresh runs whenever the shortcuts are saved, and
once a day after that; an icon that can't be fetched keeps the last good copy, or falls back to the letter.
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from .. import __version__, config, db
from ..schemas import ICONS, http_url
from .base import Widget

log = logging.getLogger("hub.shortcuts")

MODES = ("site", "builtin", "image", "letter")
MAX_SHORTCUTS = 30
MAX_PAGE = 1_500_000          # bytes of a site's HTML we'll read to find its icon (it's in <head>, near the start)
MAX_IMAGE = 600_000           # bytes; real favicons are tiny, a big file is something else
ICON_NAME_RE = re.compile(r"^[0-9a-f]{24}\.(?:png|ico|svg|jpg|gif|webp)$")
TYPES = {"image/png": "png", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico", "image/ico": "ico", "image/svg+xml": "svg",
         "image/jpeg": "jpg", "image/jpg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MEDIA_TYPES = {"png": "image/png", "ico": "image/x-icon", "svg": "image/svg+xml", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
USER_AGENT = f"Mozilla/5.0 (compatible; Citadel/{__version__}; {config.CONTACT})"


def icon_dir():
    return config.DATA_DIR / "shortcut-icons"


def sniff(data: bytes, content_type: str) -> str | None:
    """The image's file type, from its first bytes (servers often say application/octet-stream), else its header."""
    head = data[:512].lstrip()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"\x00\x00\x01\x00":
        return "ico"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048]):
        return "svg"
    return None if "html" in content_type else TYPES.get(content_type.split(";")[0].strip().lower())


class _IconLinks(HTMLParser):
    """Collects the <link rel=…icon…> tags (and any <base href>) from a page's <head>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self.base = ""
        self.done = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]
        elif tag == "link" and "icon" in a.get("rel", "").lower() and a.get("href"):
            self.links.append(a)
        elif tag == "body":
            self.done = True


def _size(sizes: str) -> int:
    """'180x180' -> 180; 'any' (an SVG) counts as big; nothing given counts as small."""
    if "any" in sizes.lower():
        return 256
    nums = [int(n) for n in re.findall(r"(\d+)x\d+", sizes)]
    return max(nums) if nums else 16


def icon_candidates(page: str, page_url: str) -> list[str]:
    """Where a page says its icons are, best first: apple-touch-icon and big sized icons (sharp on a high-DPI
    screen), then SVG, then any other icon, then the classic /favicon.ico at the site root."""
    p = _IconLinks()
    try:
        p.feed(page)
    except Exception:      # a malformed page: use whatever was found before the parser gave up
        pass
    base = urljoin(page_url, p.base) if p.base else page_url

    def score(l: dict[str, str]) -> tuple[int, int]:
        rel, size = l["rel"].lower(), _size(l.get("sizes", ""))
        if "mask-icon" in rel:                          # single-colour Safari pinned-tab icons look wrong elsewhere
            return (-1, 0)
        if size > 512:                                  # huge splash images: avoid
            return (0, -size)
        if "apple-touch-icon" in rel:
            return (3, size)
        if l.get("type", "").endswith("svg+xml") or l["href"].lower().split("?")[0].endswith(".svg"):
            return (2, 256)
        return (2 if size >= 64 else 1, size)

    ranked = sorted((l for l in p.links if score(l)[0] >= 0), key=score, reverse=True)
    out = [urljoin(base, l["href"]) for l in ranked if not l["href"].startswith("data:")]
    parts = urlsplit(page_url)
    out.append(f"{parts.scheme}://{parts.netloc}/favicon.ico")
    seen: set[str] = set()
    return [u for u in out if u.startswith(("http://", "https://")) and not (u in seen or seen.add(u))]


async def _read(client: httpx.AsyncClient, url: str, limit: int) -> tuple[bytes, str] | None:
    async with client.stream("GET", url) as r:
        if r.status_code != 200:
            return None
        if int(r.headers.get("content-length") or 0) > limit:
            return None
        body = b""
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > limit:
                return None
        return body, r.headers.get("content-type", "")


async def download_image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    """(bytes, file type) of an image at url, or None if it isn't a small image."""
    try:
        got = await _read(client, url, MAX_IMAGE)
    except httpx.HTTPError:
        return None
    if not got or not got[0]:
        return None
    ext = sniff(*got)
    return (got[0], ext) if ext else None


async def site_icon(client: httpx.AsyncClient, url: str) -> tuple[bytes, str] | None:
    """The best icon the site at url offers."""
    page = ""
    try:
        got = await _read(client, url, MAX_PAGE)
        if got:
            page = got[0].decode("utf-8", "replace")
    except httpx.HTTPError:
        pass                                            # the page itself won't load: /favicon.ico may still
    for cand in icon_candidates(page, url)[:6]:
        if img := await download_image(client, cand):
            return img
    return None


def source_of(s: dict[str, Any]) -> str | None:
    """What an icon is fetched from: changing it (a new address, a new image) means fetching again."""
    return f"site:{s['url']}" if s["icon_mode"] == "site" else f"image:{s['image']}" if s["icon_mode"] == "image" else None


class ShortcutsWidget(Widget):
    id = "shortcuts"
    title = "Shortcuts"
    interval = 24 * 3600
    daily_at = "05:45"
    min_interval = 3600
    timeout = 300
    log_success = False
    defaults = {"show": True, "shortcuts": []}

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "show" in new and not isinstance(new["show"], bool):
            raise ValueError("show must be true or false")
        if "shortcuts" in new:
            items = new["shortcuts"]
            if not isinstance(items, list) or len(items) > MAX_SHORTCUTS:
                raise ValueError(f"at most {MAX_SHORTCUTS} shortcuts")
            clean, seen = [], set()
            for i, s in enumerate(items, start=1):
                if not isinstance(s, dict):
                    raise ValueError("each shortcut must be an object")
                label = str(s.get("label") or "").strip()[:30]
                url = str(s.get("url") or "").strip()
                if not url:
                    raise ValueError(f"shortcut {i} needs an address")
                url = http_url(url, field=f"shortcut {i}’s address")
                mode = s.get("icon_mode") or "site"
                if mode not in MODES:
                    raise ValueError(f"shortcut {i}: unknown icon type")
                icon = str(s.get("icon") or "globe")
                if mode == "builtin" and icon not in ICONS:
                    raise ValueError(f"shortcut {i}: unknown icon")
                image = str(s.get("image") or "").strip()
                if mode == "image":
                    if not image:
                        raise ValueError(f"shortcut {i} needs an image address")
                    image = http_url(image, field=f"shortcut {i}’s image address")
                sid = str(s.get("id") or "")
                if not re.fullmatch(r"[a-z0-9]{1,24}", sid) or sid in seen:
                    sid = uuid.uuid4().hex[:12]
                seen.add(sid)
                clean.append({"id": sid, "label": label, "url": url, "icon_mode": mode, "icon": icon if icon in ICONS else "globe",
                              "image": image if mode == "image" else ""})
            merged["shortcuts"] = clean
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        old = (db.cache_get(self.id)["data"] or {}).get("icons") or {}
        folder = icon_dir()
        folder.mkdir(parents=True, exist_ok=True)
        icons: dict[str, dict[str, str]] = {}
        failed: list[str] = []
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT, "Accept": "text/html,image/*;q=0.9,*/*;q=0.5"},
                                     timeout=10, follow_redirects=True) as client:
            for s in settings["shortcuts"]:
                src = source_of(s)
                if not src:
                    continue
                got = await (site_icon(client, s["url"]) if s["icon_mode"] == "site" else download_image(client, s["image"]))
                if got:
                    data, ext = got
                    name = f"{hashlib.sha256(data).hexdigest()[:24]}.{ext}"
                    path = folder / name
                    if not path.exists():
                        path.write_bytes(data)
                    icons[s["id"]] = {"src": src, "file": name}
                else:
                    failed.append(s["label"] or urlsplit(s["url"]).hostname or s["url"])
                    prev = old.get(s["id"])
                    if prev and prev.get("src") == src and (folder / prev["file"]).exists():
                        icons[s["id"]] = prev           # keep the last good copy
        keep = {v["file"] for v in icons.values()}
        for f in folder.iterdir():                      # tidy away icons nothing uses any more
            if f.is_file() and f.name not in keep:
                f.unlink(missing_ok=True)
        return {"icons": icons, "failed": failed}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        n = len(d.get("icons") or {})
        return f"{n} icon{'s' if n != 1 else ''}" + (f"; couldn't get {', '.join(d['failed'])}" if d.get("failed") else "")

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now) -> dict[str, Any]:
        icons = (data or {}).get("icons") or {}
        out = []
        for s in settings["shortcuts"]:
            got = icons.get(s["id"])
            img = f"/shortcut-icons/{got['file']}" if got and got.get("src") == source_of(s) else None
            out.append({"id": s["id"], "label": s["label"], "url": s["url"], "icon_mode": s["icon_mode"], "icon": s["icon"], "img": img})
        return {"show": settings["show"], "shortcuts": out, "failed": (data or {}).get("failed", [])}


widget = ShortcutsWidget()
