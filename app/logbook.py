"""The event log: what the dashboard did, and what went wrong — kept in the database so Settings can show it.

Two things write to it:
  * `record()` — explicit events ("releases synced in 41s: 200 artists checked").
  * a logging handler that captures every WARNING/ERROR the app logs (a store that failed, a page whose layout
    changed, an unexpected traceback), plus INFO lines that opt in with `extra={"keep": True}`.

Nothing is stored before it has been scrubbed of secrets. That matters here: httpx puts the full request URL in its
error messages, and two of our URLs are secrets (the Ticketmaster key travels in the query string; a Google Calendar
"secret address" *is* the credential). `redact()` removes those, plus any value a widget has told us is secret.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import traceback
from typing import Any

from . import db

RETENTION_DAYS = 30
MAX_ROWS = 5000
MAX_MESSAGE = 600
MAX_DETAIL = 2000

_QUERY_SECRET = re.compile(r"(?i)((?:api[_-]?key|apikey|key|token|secret|password|passwd|signature|sig|auth|access[_-]?token)=)[^&\s'\")>]+")
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_CALENDAR_PRIVATE = re.compile(r"/private-[0-9A-Za-z_-]{8,}")
_ENROL_LINK = re.compile(r"/enrol/[A-Za-z0-9_-]{8,}")
_CALENDAR_URL = re.compile(r"https?://calendar\.google\.com/[^\s'\")>]+")

_known: set[str] = set()
_lock = threading.Lock()


def learn(values: list[str]) -> None:
    """Remember secret values (an API key, a calendar address) so they are blanked wherever they turn up in a message."""
    with _lock:
        _known.update(v for v in values if isinstance(v, str) and len(v.strip()) >= 8)


def redact(text: Any) -> str:
    out = str(text)
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for secret in known:
        out = out.replace(secret, "***")
    out = _ENROL_LINK.sub("/enrol/***", out)                      # a one-time enrolment link is a credential while it is valid
    out = _CALENDAR_URL.sub("https://calendar.google.com/…", out)
    out = _CALENDAR_PRIVATE.sub("/private-***", out)
    out = _QUERY_SECRET.sub(r"\1***", out)
    return _BEARER.sub(r"\1 ***", out)


def record(level: str, source: str, message: str, detail: str | None = None, *, at: int | None = None) -> None:
    """Add an entry. Never raises: logging must not be able to take the dashboard down."""
    if level not in ("info", "warning", "error"):
        level = "info"
    try:
        with db.db() as con:
            con.execute("INSERT INTO event_log (ts, level, source, message, detail) VALUES (?, ?, ?, ?, ?)",
                        (int(at if at is not None else time.time()), level, source[:40], redact(message)[:MAX_MESSAGE],
                         redact(detail)[:MAX_DETAIL] if detail else None))
    except Exception:  # noqa: BLE001 — see docstring
        pass


_LEVELS = {logging.WARNING: "warning", logging.ERROR: "error", logging.CRITICAL: "error"}
_busy = threading.local()


def _source_of(name: str) -> str:
    if name.startswith("uvicorn"):
        return "server"
    parts = name.split(".")
    return parts[-1] if len(parts) > 1 and parts[0] == "hub" else "app"


class DbHandler(logging.Handler):
    """Copies WARNING+ records (and INFO records marked `keep`) into the event log. `extra={"source": "vinyl"}` sets the source."""

    def emit(self, record_: logging.LogRecord) -> None:
        keep = getattr(record_, "keep", False)
        if record_.levelno < logging.WARNING and not keep:
            return
        if getattr(_busy, "on", False):                # a write that itself logs must not recurse
            return
        _busy.on = True
        try:
            level = _LEVELS.get(record_.levelno, "warning" if record_.levelno > logging.WARNING else "info")
            detail = "".join(traceback.format_exception(*record_.exc_info))[-MAX_DETAIL:] if record_.exc_info else None
            record(level, getattr(record_, "source", None) or _source_of(record_.name), record_.getMessage(), detail, at=int(record_.created))
        except Exception:  # noqa: BLE001
            pass
        finally:
            _busy.on = False


def install() -> None:
    """Attach the handler to the app's loggers (idempotent)."""
    for name in ("hub", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(h, DbHandler) for h in logger.handlers):
            logger.addHandler(DbHandler(level=logging.INFO))
        if name == "hub" and logger.level in (logging.NOTSET, logging.WARNING):
            logger.setLevel(logging.INFO)


# ---- reading ------------------------------------------------------------------------------------------------

def query(*, level: str | None = None, source: str | None = None, text: str | None = None, limit: int = 200, before: int | None = None) -> list[dict[str, Any]]:
    """Newest first. `level` is a minimum: 'warning' returns warnings and errors."""
    where, args = [], []
    if level == "warning":
        where.append("level IN ('warning', 'error')")
    elif level in ("error", "info"):
        where.append("level = ?"); args.append(level)
    if source:
        where.append("source = ?"); args.append(source)
    if text:
        where.append("(message LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\')")
        like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args += [like, like]
    if before:
        where.append("id < ?"); args.append(before)
    sql = "SELECT id, ts, level, source, message, detail FROM event_log" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?"
    with db.db() as con:
        return [dict(r) for r in con.execute(sql, (*args, max(1, min(limit, 500))))]


def sources() -> list[str]:
    with db.db() as con:
        return [r[0] for r in con.execute("SELECT DISTINCT source FROM event_log ORDER BY source")]


def counts(since_seconds: int = 86400) -> dict[str, int]:
    with db.db() as con:
        rows = con.execute("SELECT level, COUNT(*) FROM event_log WHERE ts >= ? GROUP BY level", (int(time.time()) - since_seconds,)).fetchall()
    return {r[0]: r[1] for r in rows}


def prune() -> int:
    """Forget entries older than RETENTION_DAYS, and keep at most MAX_ROWS."""
    with db.db() as con:
        n = con.execute("DELETE FROM event_log WHERE ts < ?", (int(time.time()) - RETENTION_DAYS * 86400,)).rowcount
        n += con.execute("DELETE FROM event_log WHERE id <= (SELECT id FROM event_log ORDER BY id DESC LIMIT 1 OFFSET ?)", (MAX_ROWS,)).rowcount
    return n


def clear() -> int:
    with db.db() as con:
        return con.execute("DELETE FROM event_log").rowcount
