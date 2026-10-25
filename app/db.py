"""SQLite access. Connections are cheap and short-lived (one per request / background job); WAL mode lets
the CLI and the server touch the same file safely."""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import config

MIGRATIONS = Path(__file__).parent / "migrations"


def connect() -> sqlite3.Connection:
    config.ensure_dirs()
    fresh = not config.DB_PATH.exists()
    con = sqlite3.connect(config.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=5000")
    if fresh:
        os.chmod(config.DB_PATH, 0o600)
    return con


@contextlib.contextmanager
def db() -> Iterator[sqlite3.Connection]:
    """`with db() as con:` — commits on success, rolls back on error, always closes."""
    con = connect()
    try:
        with con:
            yield con
    finally:
        con.close()


def migrate() -> list[str]:
    """Apply pending scripts in order, each atomically with its marker. Returns the names applied."""
    applied: list[str] = []
    con = connect()
    try:
        con.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)")
        done = {r["name"] for r in con.execute("SELECT name FROM schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in done:
                continue
            with con:
                # executescript commits a pending transaction before running, so BEGIN must be
                # inside the script. The context commits the script and marker together, or rolls
                # both back if a statement, the marker insert, or the final commit fails.
                con.executescript("BEGIN IMMEDIATE;\n" + path.read_text() + "\n")
                con.execute("INSERT INTO schema_migrations VALUES (?, ?)", (path.name, int(time.time())))
            applied.append(path.name)
    finally:
        con.close()
    return applied


# ---- widget settings / cache -------------------------------------------------------------------

def get_settings(widget: str, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    with db() as con:
        row = con.execute("SELECT json FROM widget_settings WHERE widget = ?", (widget,)).fetchone()
    return {**(defaults or {}), **(json.loads(row["json"]) if row else {})}


def put_settings(widget: str, values: dict[str, Any]) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO widget_settings (widget, json) VALUES (?, ?) "
            "ON CONFLICT(widget) DO UPDATE SET json = excluded.json",
            (widget, json.dumps(values)),
        )


def cache_get(widget: str) -> dict[str, Any]:
    with db() as con:
        row = con.execute("SELECT * FROM widget_cache WHERE widget = ?", (widget,)).fetchone()
    if not row:
        return {"data": None, "fetched_at": None, "error": None, "error_at": None}
    return {
        "data": json.loads(row["data"]) if row["data"] else None,
        "fetched_at": row["fetched_at"],
        "error": row["error"],
        "error_at": row["error_at"],
    }


def cache_put(widget: str, data: Any) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO widget_cache (widget, data, fetched_at, error, error_at) VALUES (?, ?, ?, NULL, NULL) "
            "ON CONFLICT(widget) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at, "
            "error = NULL, error_at = NULL",
            (widget, json.dumps(data), int(time.time())),
        )


def cache_error(widget: str, message: str) -> None:
    """Record a failed refresh without touching the last good data."""
    with db() as con:
        con.execute(
            "INSERT INTO widget_cache (widget, error, error_at) VALUES (?, ?, ?) "
            "ON CONFLICT(widget) DO UPDATE SET error = excluded.error, error_at = excluded.error_at",
            (widget, message[:500], int(time.time())),
        )


# ---- job state (when each sync last ran) -----------------------------------------------------------------

def job_get(job: str) -> dict[str, Any] | None:
    with db() as con:
        row = con.execute("SELECT * FROM job_state WHERE job = ?", (job,)).fetchone()
    return dict(row) if row else None


def job_finished(job: str, *, ok: bool, seconds: float, message: str, at: int | None = None) -> int:
    """Record an attempt. Returns the number of consecutive failed attempts now (0 after a success)."""
    at = int(at if at is not None else time.time())
    with db() as con:
        prev = con.execute("SELECT streak, failing_since FROM job_state WHERE job = ?", (job,)).fetchone()
        streak = 0 if ok else (prev["streak"] if prev else 0) + 1
        since = None if ok else (prev["failing_since"] if prev and prev["streak"] else at)
        con.execute(
            "INSERT INTO job_state (job, last_run, last_ok, last_seconds, streak, failing_since, message) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(job) DO UPDATE SET last_run = excluded.last_run, last_ok = COALESCE(excluded.last_ok, job_state.last_ok), "
            "last_seconds = excluded.last_seconds, streak = excluded.streak, failing_since = excluded.failing_since, message = excluded.message",
            (job, at, at if ok else None, round(seconds, 2), streak, since, message[:500]),
        )
    return streak


def job_all() -> dict[str, dict[str, Any]]:
    with db() as con:
        return {r["job"]: dict(r) for r in con.execute("SELECT * FROM job_state")}
