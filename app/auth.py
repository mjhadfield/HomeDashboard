"""Owner-by-device access.

Everyone is a *guest* by default. A device becomes *owner* (or *kiosk*, for a TV) by redeeming a one-time
enrolment token, which the host prints via `python -m app.cli enrol`. Redeeming sets a signed, HttpOnly,
long-lived cookie tied to a row in `devices`, so a device can be revoked individually.

Mutating owner requests must also send `X-Hub: 1`. Browsers can't add a custom header cross-site without a CORS
preflight (which we never grant), so this blocks CSRF even though the cookie is only SameSite=Lax.
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import re
import secrets
import time
from typing import Any

from fastapi import HTTPException, Request

from . import config
from .db import db

COOKIE = "hub_device"
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600
TOKEN_TTL = 15 * 60
ROLES = ("owner", "kiosk")


@functools.lru_cache(maxsize=1)
def _key() -> bytes:
    return config.secret_key()


def _sig(device_id: str) -> str:
    return hmac.new(_key(), device_id.encode(), hashlib.sha256).hexdigest()[:40]


def make_cookie(device_id: str) -> str:
    return f"{device_id}.{_sig(device_id)}"


def _device_id_from_cookie(value: str | None) -> str | None:
    if not value or "." not in value:
        return None
    device_id, _, sig = value.partition(".")
    return device_id if hmac.compare_digest(sig, _sig(device_id)) else None


def current_device(request: Request) -> dict[str, Any] | None:
    """The enrolled device behind this request, or None for a guest. Cached on the request."""
    if hasattr(request.state, "device"):
        return request.state.device
    device = None
    if device_id := _device_id_from_cookie(request.cookies.get(COOKIE)):
        with db() as con:
            row = con.execute("SELECT * FROM devices WHERE id = ? AND revoked = 0", (device_id,)).fetchone()
            if row:
                device = dict(row)
                now = int(time.time())
                if not row["last_seen"] or now - row["last_seen"] > 60:
                    con.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, device_id))
    request.state.device = device
    return device


def role_of(request: Request) -> str:
    device = current_device(request)
    return device["role"] if device else "guest"


def require_owner(request: Request) -> dict[str, Any]:
    device = current_device(request)
    if not device or device["role"] != "owner":
        raise HTTPException(403, "This needs an enrolled owner device.")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-hub") != "1":
        raise HTTPException(403, "Missing X-Hub header.")
    return device


def require_kiosk(request: Request) -> dict[str, Any]:
    """Screens (kiosk) and owners may read screen data. Never writes."""
    device = current_device(request)
    if not device or device["role"] not in ("kiosk", "owner"):
        raise HTTPException(403, "This needs an enrolled device.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        raise HTTPException(405, "Screens are read-only.")
    return device


# ---- enrolment ---------------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_enrol_token(role: str = "owner", name: str = "New device", ttl: int = TOKEN_TTL) -> str:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    token = secrets.token_urlsafe(24)
    with db() as con:
        con.execute("DELETE FROM enrol_tokens WHERE expires_at < ? OR used = 1", (int(time.time()),))
        con.execute(
            "INSERT INTO enrol_tokens (token_hash, role, name, expires_at) VALUES (?, ?, ?, ?)",
            (_hash(token), role, name.strip()[:60] or "New device", int(time.time()) + ttl),
        )
    return token


def peek_enrol_token(token: str) -> dict[str, Any] | None:
    """Look at a token without consuming it (for the confirm page, so link-preview bots can't burn it)."""
    with db() as con:
        row = con.execute(
            "SELECT role, name FROM enrol_tokens WHERE token_hash = ? AND used = 0 AND expires_at > ?",
            (_hash(token), int(time.time())),
        ).fetchone()
    return dict(row) if row else None


def redeem_enrol_token(token: str, user_agent: str = "") -> str | None:
    """Consume a token and create the device. Returns the cookie value, or None if the token is bad."""
    with db() as con:
        # The UPDATE is the atomic "claim": only one redeemer can flip used 0 -> 1.
        claimed = con.execute(
            "UPDATE enrol_tokens SET used = 1 WHERE token_hash = ? AND used = 0 AND expires_at > ?",
            (_hash(token), int(time.time())),
        ).rowcount
        if not claimed:
            return None
        tok = con.execute("SELECT role, name FROM enrol_tokens WHERE token_hash = ?", (_hash(token),)).fetchone()
        device_id = secrets.token_urlsafe(12)
        con.execute(
            "INSERT INTO devices (id, name, role, user_agent, created_at, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
            (device_id, tok["name"], tok["role"], user_agent[:300], int(time.time()), int(time.time())),
        )
    return make_cookie(device_id)


def list_devices() -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute(
            "SELECT id, name, role, user_agent, created_at, last_seen, revoked FROM devices ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_device(device_id: str) -> bool:
    with db() as con:
        return con.execute("UPDATE devices SET revoked = 1 WHERE id = ? AND revoked = 0", (device_id,)).rowcount > 0


def needs_bootstrap() -> bool:
    """True on a fresh install: no live owner device and no usable owner token."""
    with db() as con:
        owners = con.execute("SELECT COUNT(*) c FROM devices WHERE role = 'owner' AND revoked = 0").fetchone()["c"]
        tokens = con.execute(
            "SELECT COUNT(*) c FROM enrol_tokens WHERE role = 'owner' AND used = 0 AND expires_at > ?",
            (int(time.time()),),
        ).fetchone()["c"]
    return owners == 0 and tokens == 0


# ---- screen pairing ----------------------------------------------------------------------------
# A TV can't sensibly type a long enrolment link. Instead it shows a short code and polls; an owner types
# the code into Settings to approve it. Pairing can only ever create 'kiosk' devices — never owners.

PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"    # no 0/O/1/I
PAIR_TTL = 10 * 60
PAIR_MAX_PENDING = 8                                   # caps what an unauthenticated LAN visitor can create


def normalise_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def pair_start() -> dict[str, Any] | None:
    """Begin pairing for an unenrolled screen. Returns None if too many are already waiting."""
    now = int(time.time())
    with db() as con:
        con.execute("DELETE FROM pairings WHERE expires_at < ?", (now,))
        if con.execute("SELECT COUNT(*) c FROM pairings WHERE status = 'pending'").fetchone()["c"] >= PAIR_MAX_PENDING:
            return None
        while True:
            code = "".join(secrets.choice(PAIR_ALPHABET) for _ in range(6))
            if not con.execute("SELECT 1 FROM pairings WHERE code = ?", (code,)).fetchone():
                break
        secret = secrets.token_urlsafe(24)
        con.execute("INSERT INTO pairings (code, secret_hash, created_at, expires_at) VALUES (?, ?, ?, ?)",
                    (code, _hash(secret), now, now + PAIR_TTL))
    return {"code": f"{code[:3]}-{code[3:]}", "secret": secret, "expires_in": PAIR_TTL}


def pair_approve(code: str, name: str) -> bool:
    with db() as con:
        return con.execute(
            "UPDATE pairings SET status = 'approved', name = ? WHERE code = ? AND status = 'pending' AND expires_at > ?",
            (name.strip()[:60] or "TV", normalise_code(code), int(time.time())),
        ).rowcount > 0


def pair_claim(secret: str, user_agent: str = "") -> tuple[str, str | None]:
    """Called by the waiting screen. ('pending', None) | ('gone', None) | ('ok', cookie)."""
    now = int(time.time())
    with db() as con:
        row = con.execute("SELECT code, status, name, expires_at FROM pairings WHERE secret_hash = ?", (_hash(secret),)).fetchone()
        if not row or row["expires_at"] < now or row["status"] == "claimed":
            return "gone", None
        if row["status"] == "pending":
            return "pending", None
        # approved: claim atomically so a replayed poll can't mint a second device
        if not con.execute("UPDATE pairings SET status = 'claimed' WHERE code = ? AND status = 'approved'", (row["code"],)).rowcount:
            return "gone", None
        device_id = secrets.token_urlsafe(12)
        con.execute("INSERT INTO devices (id, name, role, user_agent, created_at, last_seen) VALUES (?, ?, 'kiosk', ?, ?, ?)",
                    (device_id, row["name"] or "TV", user_agent[:300], now, now))
    return "ok", make_cookie(device_id)
