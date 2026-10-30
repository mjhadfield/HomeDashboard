"""Status dots: probes each project's port and each link's health URL on a timer.

Projects are probed with a plain TCP connect to 127.0.0.1 (fast, no protocol assumptions); links with a
`health_url` get an HTTP GET (any response below 500 counts as up). Results are held in memory and pushed
to browsers over SSE only when something changes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .bus import Bus
from .db import db

log = logging.getLogger("hub.health")


class Health:
    def __init__(self, bus: Bus, interval: float = 30.0) -> None:
        self.bus = bus
        self.interval = interval
        self.state: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
            while True:
                try:
                    if await self.check_all(client):
                        self.bus.publish("health", {})
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("health sweep failed")
                await asyncio.sleep(self.interval)

    @staticmethod
    async def _tcp(port: int) -> tuple[bool, int]:
        t0 = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
            writer.close()
            return True, round((time.perf_counter() - t0) * 1000)
        except (OSError, asyncio.TimeoutError):
            return False, 0

    @staticmethod
    async def _http(client: httpx.AsyncClient, url: str) -> tuple[bool, int]:
        t0 = time.perf_counter()
        try:
            resp = await client.get(url)
            return resp.status_code < 500, round((time.perf_counter() - t0) * 1000)
        except httpx.HTTPError:
            return False, 0

    def _record(self, key: str, ok: bool, ms: int) -> bool:
        old = self.state.get(key)
        at = int(time.time())
        # `down_since`: when this run of failed probes began (for the "something's down" reminder), None while up.
        down_since = None if ok else (old.get("down_since") if old and not old["ok"] and old.get("down_since") else at)
        self.state[key] = {"ok": ok, "ms": ms, "at": at, "down_since": down_since}
        return old is None or old["ok"] != ok

    async def check_all(self, client: httpx.AsyncClient) -> bool:
        with db() as con:
            projects = con.execute("SELECT slug, port FROM projects WHERE enabled = 1 AND kind = 'web' AND port IS NOT NULL").fetchall()
            links = con.execute("SELECT id, health_url FROM links WHERE health_url IS NOT NULL AND health_url != ''").fetchall()
        jobs: list[tuple[str, Any]] = [(f"project:{p['slug']}", self._tcp(p["port"])) for p in projects]
        jobs += [(f"link:{l['id']}", self._http(client, l["health_url"])) for l in links]
        results = await asyncio.gather(*(j[1] for j in jobs))
        current = {key for key, _ in jobs}
        changed = bool(set(self.state) - current)          # something was deleted: browsers should refresh
        self.state = {k: v for k, v in self.state.items() if k in current}
        for (key, _), (ok, ms) in zip(jobs, results):
            changed |= self._record(key, ok, ms)
        return changed

    async def sweep_now(self) -> None:
        """Probe everything immediately (after links/projects were edited)."""
        async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
            if await self.check_all(client):
                self.bus.publish("health", {})

    async def recheck_project(self, slug: str, port: int, expect_up: bool, timeout: float = 45.0) -> None:
        """After a start/stop, poll quickly until the port reflects the change so the UI updates promptly."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ok, ms = await self._tcp(port)
            if self._record(f"project:{slug}", ok, ms) or ok == expect_up:
                self.bus.publish("health", {})
            if ok == expect_up:
                return
            await asyncio.sleep(1)
