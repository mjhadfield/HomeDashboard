"""Discogs: the Music page's "Worth buying" — albums you play a lot but don't own on vinyl, with today's cheapest
original pressing and coloured edition on the Discogs marketplace (app/music/buying.py).

A personal access token (discogs.com → Settings → Developers) allows 60 requests a minute; this keeps under that with
one request per MIN_GAP across everything. Discogs counts a refused (429) request too and gives no Retry-After, so a 429
waits out its one-minute window before one retry. Its terms want prices no more than six hours old and "Data provided by
Discogs" beside them — the page does both.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from .base import Field, Service, ServiceError

API = "https://api.discogs.com"
UA = "CitadelHomeDashboard/1.0 +http://192.168.0.66:8080"
MIN_GAP = 1.1          # ~54 a minute, under the 60 a token allows
DEFAULT_MAX_PRICE = 35.0
DEFAULT_WATCH = 30


TOKEN_NOISE = re.compile(r"^\s*(?:DISCOGS_TOKEN\s*=|Discogs\s+token\s*=|token\s*=)\s*", re.I)


def clean_token(value: str | None) -> str:
    """A pasted token, as Discogs wants it: without a copied "DISCOGS_TOKEN=" (a whole .env line), "Discogs token=" or
    quotes around it."""
    v = TOKEN_NOISE.sub("", str(value or "").strip())
    return v.strip().strip("'\"").strip()


class DiscogsService(Service):
    async def test(self) -> str:
        me = await call("/oauth/identity")
        return f"Working — signed in as {me.get('username', '?')}"

    def max_price(self) -> float:
        try:
            return max(1.0, float(str(self.cfg().get("max_price") or DEFAULT_MAX_PRICE).replace("£", "")))
        except ValueError:
            return DEFAULT_MAX_PRICE

    def watch(self) -> int:
        try:
            return min(60, max(5, int(self.cfg().get("watch") or DEFAULT_WATCH)))
        except ValueError:
            return DEFAULT_WATCH

    def secrets(self) -> list[str]:
        raw = super().secrets()
        return raw + [clean_token(v) for v in raw if clean_token(v) != v]      # the event log blanks the token however it was pasted

    def merge(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        if new.get("api_key"):
            new = {**new, "api_key": clean_token(new["api_key"])}
        out = super().merge(old, new)
        for name, lo, hi in (("max_price", 1, 10000), ("watch", 5, 60)):
            v = str(out.get(name) or "").replace("£", "").strip()
            if v:
                try:
                    n = float(v)
                except ValueError:
                    raise ValueError(f"Discogs: {name.replace('_', ' ')} must be a number") from None
                if not lo <= n <= hi:
                    raise ValueError(f"Discogs: {name.replace('_', ' ')} must be between {lo} and {hi}")
                out[name] = v
        return out


service = DiscogsService(
    id="discogs", name="Discogs", order=45,
    blurb="Worth buying: albums you play a lot but don't own on vinyl, with the cheapest original and coloured pressings for sale.",
    link="https://www.discogs.com/settings/developers",
    fields=(Field("api_key", "Personal access token", secret=True),
            Field("max_price", "Most you'd pay (£)", placeholder=str(int(DEFAULT_MAX_PRICE)), required=False),
            Field("watch", "Albums to watch", placeholder=str(DEFAULT_WATCH), required=False)),
)

_lock = asyncio.Lock()
_last = 0.0
transport: httpx.AsyncBaseTransport | None = None     # tests swap in a fake Discogs


async def call(path: str, **params: Any) -> dict[str, Any]:
    """GET a Discogs resource ({} for a 404). Throttled across every caller."""
    global _last
    if not service.enabled():
        raise ServiceError("Discogs is switched off in Settings → Music page")
    token = clean_token(service.cfg().get("api_key"))
    if not token:
        raise ServiceError("Discogs needs a personal access token — Settings → Music page")
    headers = {"User-Agent": UA, "Authorization": f"Discogs token={token}", "Accept": "application/json"}
    for attempt in range(2):
        async with _lock:
            wait = _last + MIN_GAP - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            async with httpx.AsyncClient(timeout=20, headers=headers, transport=transport) as http:
                r = await http.get(API + path, params={k: v for k, v in params.items() if v is not None})
            _last = time.monotonic()
        if r.status_code == 429 and attempt == 0:
            await asyncio.sleep(61)          # the window is a minute, and the refused request counted
            continue
        break
    if r.status_code == 404:
        return {}
    if r.status_code == 401:
        raise ServiceError("Discogs didn't accept the token")
    if r.status_code == 429:
        raise ServiceError("Discogs is rate-limiting Citadel — the next run will carry on")
    if r.status_code == 403:
        return {"_forbidden": True}           # e.g. price suggestions before the seller settings are filled in
    r.raise_for_status()
    return r.json()
