"""Appearance: custom themes and background pictures (Settings → Appearance).

A custom theme starts from one of the built-in modes (dark or light, which supply the text colours and everything
else) and changes what you see most: the accent colour of the UI, the colour and see-through-ness of the panels,
and an optional background picture with how it's fitted and how much it's dimmed. Themes are stored here, so every
device can use them; which theme a device uses is that device's own choice (kept in its browser, as dark/light
already was).

Background pictures are uploaded to the data folder and served by Citadel itself (see main.py), because the page's
CSP only lets the browser load images from a few named CDNs.
"""
from __future__ import annotations

import hashlib
import re
import time
import uuid
from typing import Any

from . import config, db

KEY = "appearance"
DEFAULTS: dict[str, Any] = {"themes": []}
MAX_THEMES = 20
MAX_BACKGROUND = 15_000_000                      # bytes — a big phone photo is well under this
FITS = ("cover", "contain", "tile", "center")
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ID_RE = re.compile(r"^[a-z0-9]{1,24}$")
BG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|webp|gif)$")
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
BASE_PANEL = {"dark": "#0e0b14", "light": "#f8f0e2"}      # the built-in themes' own --surface
BASE_ACCENT = {"dark": "#8b5cf6", "light": "#b5541f"}


class AppearanceError(ValueError):
    pass


def bg_dir():
    return config.DATA_DIR / "backgrounds"


def themes() -> list[dict[str, Any]]:
    return db.get_settings(KEY, DEFAULTS).get("themes", [])


def _save(all_themes: list[dict[str, Any]]) -> None:
    db.put_settings(KEY, {"themes": all_themes})


def clean(t: dict[str, Any]) -> dict[str, Any]:
    """A theme as stored: every field checked, anything missing filled from its base."""
    name = str(t.get("name") or "").strip()[:40]
    if not name:
        raise AppearanceError("give the theme a name")
    base = t.get("base")
    if base not in ("dark", "light"):
        raise AppearanceError("a theme starts from dark or light")
    accent = str(t.get("accent") or BASE_ACCENT[base])
    panel = str(t.get("panel") or BASE_PANEL[base])
    for label, v in (("accent", accent), ("panel", panel)):
        if not HEX_RE.match(v):
            raise AppearanceError(f"the {label} colour must look like #8b5cf6")
    try:
        opacity = int(t.get("panel_opacity", 100))
    except (TypeError, ValueError):
        raise AppearanceError("panel see-through must be a number")
    bg = t.get("background") or {}
    if not isinstance(bg, dict):
        raise AppearanceError("background must be an object")
    file = str(bg.get("file") or "")
    if file and (not BG_NAME_RE.match(file) or not (bg_dir() / file).is_file()):
        raise AppearanceError("that background picture isn’t here any more — upload it again")
    fit = bg.get("fit") or "cover"
    if fit not in FITS:
        raise AppearanceError("unknown background fit")
    try:
        dim = int(bg.get("dim", 40))
    except (TypeError, ValueError):
        raise AppearanceError("dim must be a number")
    return {"id": str(t.get("id") or ""), "name": name, "base": base, "accent": accent.lower(), "panel": panel.lower(),
            "panel_opacity": max(30, min(100, opacity)),
            "background": {"file": file, "fit": fit, "dim": max(0, min(90, dim)), "glow": bool(bg.get("glow", True))}}


def save_theme(t: dict[str, Any], theme_id: str | None = None) -> dict[str, Any]:
    """Create (no id) or replace (id) a theme. Returns it as stored."""
    theme = clean(t)
    all_themes = themes()
    if theme_id:
        if not any(x["id"] == theme_id for x in all_themes):
            raise LookupError("no such theme")
        theme["id"] = theme_id
        all_themes = [theme if x["id"] == theme_id else x for x in all_themes]
    else:
        if len(all_themes) >= MAX_THEMES:
            raise AppearanceError(f"at most {MAX_THEMES} themes")
        theme["id"] = uuid.uuid4().hex[:12]
        all_themes.append(theme)
    _save(all_themes)
    tidy()
    return theme


def delete_theme(theme_id: str) -> None:
    all_themes = themes()
    if not any(x["id"] == theme_id for x in all_themes):
        raise LookupError("no such theme")
    _save([x for x in all_themes if x["id"] != theme_id])
    tidy()


def sniff(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


def store_background(data: bytes) -> str:
    """Keep an uploaded picture (PNG, JPEG, WebP or GIF; checked by its bytes, not its name) and return its file name."""
    if not data:
        raise AppearanceError("that file is empty")
    if len(data) > MAX_BACKGROUND:
        raise AppearanceError(f"pictures up to {MAX_BACKGROUND // 1_000_000} MB, please")
    ext = sniff(data)
    if not ext:
        raise AppearanceError("use a JPEG, PNG, WebP or GIF picture")
    folder = bg_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{hashlib.sha256(data).hexdigest()[:32]}.{ext}"
    if not (folder / name).exists():
        (folder / name).write_bytes(data)
    return name


def tidy(older_than: float = 24 * 3600) -> None:
    """Remove pictures no theme uses. A fresh upload is spared for a day, so one waiting in an open, unsaved
    editor isn't swept away by someone saving a different theme meanwhile."""
    folder = bg_dir()
    if not folder.exists():
        return
    used = {t["background"]["file"] for t in themes() if t["background"]["file"]}
    for f in folder.iterdir():
        if f.is_file() and f.name not in used and time.time() - f.stat().st_mtime > older_than:
            f.unlink(missing_ok=True)
