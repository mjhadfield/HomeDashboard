"""Validation for everything the owner can edit in Settings. Nothing here trusts the browser: URLs must be
http(s), icons must exist in the sprite, commands become argument lists (never shell strings), env names are
constrained."""
from __future__ import annotations

import os
import re
import shlex
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

# Must match the symbols in web/js/icons.js.
ICONS = ["globe", "film", "router", "disc", "bolt", "play", "term", "chip", "server", "cards", "heart",
         "news", "cal", "grid", "home", "bin", "star", "radar", "alert", "pin", "wave"]
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,60}$")


def http_url(value: str, *, field: str = "url", allow_placeholders: bool = False) -> str:
    probe = value.replace("{host}", "host").replace("{ip}", "0.0.0.0") if allow_placeholders else value
    parts = urlsplit(probe)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{field} must be a full http:// or https:// address")
    return value.strip()


class LinkIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    subtitle: str = Field("", max_length=80)
    url: str = Field(max_length=500)
    icon: str = "globe"
    health_url: str | None = Field(None, max_length=500)
    is_primary: bool = False
    show_guest: bool = False
    tier: Literal["main", "more"] = "main"
    pinned: bool | None = None          # None = leave unchanged (so older clients can't accidentally unpin)
    for_media: bool = False             # this link's url is what the Media page embeds — at most one at a time
    for_music: bool = False             # same idea, for the Companion page — independent of for_media
    for_sql: bool = False               # same idea, for the SQL DB page — independent of the other two

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return http_url(v)

    @field_validator("health_url")
    @classmethod
    def _health(cls, v: str | None) -> str | None:
        return http_url(v, field="health check address") if v and v.strip() else None

    @field_validator("icon")
    @classmethod
    def _icon(cls, v: str) -> str:
        if v not in ICONS:
            raise ValueError(f"icon must be one of: {', '.join(ICONS)}")
        return v

    @field_validator("name", "subtitle")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field("", max_length=120)
    kind: Literal["web", "desktop"] = "web"
    url: str | None = Field(None, max_length=500)          # may contain {host} / {ip}
    port: int | None = Field(None, ge=1, le=65535)
    cwd: str = Field(min_length=1, max_length=500)
    command: str = Field(min_length=1, max_length=1000)     # parsed with shlex into an argv list; never run through a shell
    env: dict[str, str] = Field(default_factory=dict)
    lan_visible: bool = True
    enabled: bool = True
    pinned: bool | None = None
    for_music: bool | None = None   # the Companion page embeds this project (None = leave unchanged)
    for_sql: bool | None = None     # the SQL DB page embeds this project
    show_projects: bool = True     # the Projects card (start/stop); a standalone app with nothing to "stop" might turn this off
    show_launchpad: bool = False
    icon: str = ""       # "" = a sensible default for its kind, chosen client-side; otherwise must be a real icon

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return http_url(v, allow_placeholders=True) if v and v.strip() else None

    @field_validator("icon")
    @classmethod
    def _icon(cls, v: str) -> str:
        if v and v not in ICONS:
            raise ValueError(f"icon must be one of: {', '.join(ICONS)}")
        return v

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, v: str) -> str:
        if not os.path.isabs(v.strip()):
            raise ValueError("working directory must be an absolute path")
        return v.strip()

    @field_validator("command")
    @classmethod
    def _command(cls, v: str) -> str:
        try:
            argv = shlex.split(v)
        except ValueError as exc:
            raise ValueError(f"can't parse the command: {exc}")
        if not argv or len(argv) > 60:
            raise ValueError("command needs 1–60 arguments")
        return v.strip()

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 30:
            raise ValueError("too many environment variables")
        for key, val in v.items():
            if not ENV_RE.match(key):
                raise ValueError(f"bad environment variable name: {key!r}")
            if len(val) > 500 or "\n" in val:
                raise ValueError(f"bad value for {key}")
        return v

    def argv(self) -> list[str]:
        return shlex.split(self.command)


class ProjectCreate(ProjectIn):
    slug: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not SLUG_RE.match(v):
            raise ValueError("id must be lowercase letters, digits and dashes (it names the systemd unit)")
        return v


class PinIn(BaseModel):
    pinned: bool


class LaunchpadIn(BaseModel):
    show: bool


class IdList(BaseModel):
    ids: list[Any] = Field(max_length=200)


TV_PANELS = ("bins", "diary", "gigs")
TV_DEFAULTS: dict[str, Any] = {"panels": ["bins"], "rotate_seconds": 12, "show_alerts": True}


class ScreensIn(BaseModel):
    panels: list[Literal["bins", "diary", "gigs"]] = Field(default_factory=list, max_length=6)
    rotate_seconds: int = Field(12, ge=5, le=300)
    show_alerts: bool = True

    @field_validator("panels")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))
