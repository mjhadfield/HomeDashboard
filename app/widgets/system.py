"""Host vitals: uptime, load, memory, CPU, and disks.

Disks come from the existing local `disk-usage-server` (it already knows about the NAS/seedbox mounts and
tolerates them being slow); if that isn't running we fall back to `statvfs('/')` so the card still works.
"""
from __future__ import annotations

import asyncio
import os
import socket
from typing import Any

import httpx

from .. import config
from .base import Widget


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class SystemWidget(Widget):
    id = "system"
    title = "System"
    interval = 30
    min_interval = 5
    local = True
    log_success = False
    timeout = 15
    defaults: dict[str, Any] = {}

    def __init__(self) -> None:
        self._prev_cpu: tuple[int, int] | None = None

    def _cpu_percent(self) -> float | None:
        fields = [int(x) for x in _read("/proc/stat").splitlines()[0].split()[1:]]
        idle, total = fields[3] + fields[4], sum(fields)
        prev, self._prev_cpu = self._prev_cpu, (idle, total)
        if not prev or total == prev[1]:
            return None
        return round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1)

    def _disks(self) -> list[dict[str, Any]]:
        try:
            rows = httpx.get(config.DISK_SERVER_URL, timeout=3).json()
            return [{"name": r["name"], "mount": r["mount"], "size": r["size"], "used": r["used"], "percent": r["percent"]} for r in rows]
        except Exception:
            st = os.statvfs("/")
            size, used = st.f_blocks * st.f_frsize, (st.f_blocks - st.f_bfree) * st.f_frsize
            return [{"name": "System", "mount": "/", "size": size, "used": used, "percent": round(100 * used / size)}]

    def _collect(self) -> dict[str, Any]:
        mem = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in _read("/proc/meminfo").splitlines())}
        return {
            "host": socket.gethostname(),
            "uptime_s": int(float(_read("/proc/uptime").split()[0])),
            "load": [float(x) for x in _read("/proc/loadavg").split()[:3]],
            "cpu_percent": self._cpu_percent(),
            "mem": {"total": mem["MemTotal"], "used": mem["MemTotal"] - mem["MemAvailable"]},
            "disks": self._disks(),
        }

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._collect)


widget = SystemWidget()
