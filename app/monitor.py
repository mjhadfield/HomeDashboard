"""Live hardware stats for the Monitoring tile: CPU/GPU utilisation and temperature, memory.

Collected by a small background loop (started/stopped in main.py's lifespan, same pattern as Health and the
Scheduler) rather than the widget/scheduler/cache machinery those use — this needs a much tighter cadence than
any widget, and belongs to no one client. `/api/owner/monitoring` just returns the latest reading instantly;
the actual sensor read (mostly nvidia-smi's ~30ms subprocess spawn) never happens inside a request.
"""
from __future__ import annotations

import asyncio
import glob
import logging
import subprocess
from typing import Any

log = logging.getLogger("hub.monitor")

# Per-name (aggregate 'cpu' and each 'cpuN') previous (idle, total) tick counts, for delta-based percentages.
_prev_cpu: dict[str, tuple[int, int]] = {}
_k10temp_dir: str | None | object = "unchecked"   # cached hwmon path lookup; sentinel until first checked


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


def _cpu() -> dict[str, Any]:
    rows = [l.split() for l in _read("/proc/stat").splitlines() if l.startswith("cpu")]
    percents: dict[str, float | None] = {}
    for fields in rows:
        name = fields[0]
        nums = [int(x) for x in fields[1:]]
        idle, total = nums[3] + nums[4], sum(nums)
        prev = _prev_cpu.get(name)
        _prev_cpu[name] = (idle, total)
        percents[name] = round(100 * (1 - (idle - prev[0]) / (total - prev[1])), 1) if prev and total != prev[1] else None
    return {"percent": percents.get("cpu"), "per_core": [percents[f"cpu{i}"] for i in range(len(rows) - 1)]}


def _find_k10temp() -> str | None:
    global _k10temp_dir
    if _k10temp_dir == "unchecked":
        _k10temp_dir = next((d for d in glob.glob("/sys/class/hwmon/hwmon*") if _read(f"{d}/name").strip() == "k10temp"), None)
    return _k10temp_dir


def _cpu_temp() -> dict[str, Any]:
    d = _find_k10temp()
    if not d:
        return {"package": None, "chiplets": []}
    out: dict[str, Any] = {"package": None, "chiplets": []}
    for f in sorted(glob.glob(f"{d}/temp*_label")):
        idx = f.split("temp")[-1].split("_")[0]
        label = _read(f).strip()
        val = round(int(_read(f"{d}/temp{idx}_input")) / 1000, 1)
        if label == "Tctl":
            out["package"] = val
        elif label.startswith("Tccd"):
            out["chiplets"].append({"label": label, "temp": val})
    return out


def _mem() -> dict[str, int]:
    raw = {k: int(v.split()[0]) * 1024 for k, v in (l.split(":", 1) for l in _read("/proc/meminfo").splitlines())}
    return {
        "total": raw["MemTotal"], "used": raw["MemTotal"] - raw["MemAvailable"], "available": raw["MemAvailable"],
        "swap_total": raw.get("SwapTotal", 0), "swap_used": raw.get("SwapTotal", 0) - raw.get("SwapFree", 0),
        "buffers": raw.get("Buffers", 0), "cached": raw.get("Cached", 0),
    }


def _load() -> list[float]:
    return [float(x) for x in _read("/proc/loadavg").split()[:3]]


# utilization.memory (how much of the time the memory *interface* was busy) deliberately isn't queried here —
# it looks like a "how full is VRAM" figure but isn't one; mem_used_mb/mem_total_mb below is the real one.
GPU_FIELDS = ["name", "utilization.gpu", "temperature.gpu", "memory.used", "memory.total",
              "power.draw", "power.limit", "clocks.sm", "clocks.mem", "fan.speed"]


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None   # nvidia-smi prints "[N/A]" for anything the card/driver doesn't expose


def _gpu() -> dict[str, Any] | None:
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}", "--format=csv,noheader,nounits"],
                              capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    parts = [p.strip() for p in out.stdout.strip().splitlines()[0].split(",")]
    if len(parts) != len(GPU_FIELDS):
        return None
    name, percent, temp, mem_used, mem_total, power, power_limit, clock_sm, clock_mem, fan = parts
    return {
        "name": name, "percent": _num(percent), "temp": _num(temp),
        "mem_used_mb": _num(mem_used), "mem_total_mb": _num(mem_total),
        "power_w": _num(power), "power_limit_w": _num(power_limit),
        "clock_sm_mhz": _num(clock_sm), "clock_mem_mhz": _num(clock_mem), "fan_percent": _num(fan),
    }


def _collect() -> dict[str, Any]:
    cpu = _cpu()
    cpu["temp"] = _cpu_temp()
    return {"cpu": cpu, "gpu": _gpu(), "mem": _mem(), "load": _load()}


INTERVAL = 1.0   # background collection cadence — independent of how often any client polls the endpoint


class Monitor:
    """One instance per app lifespan (held on app.state, same pattern as Health/Scheduler) — not module-level
    state, since a module global would be shared (and its background task fought over) across the several
    independent app lifespans a test run creates on the same process."""

    def __init__(self) -> None:
        self.latest: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None
        self._warned = False

    async def _loop(self) -> None:
        while True:
            try:
                self.latest = await asyncio.to_thread(_collect)
                self._warned = False
            except asyncio.CancelledError:
                raise
            except Exception:
                if not self._warned:      # don't spam the log every second if something's genuinely broken
                    log.exception("monitoring collection failed")
                    self._warned = True
            await asyncio.sleep(INTERVAL)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def snapshot(self) -> dict[str, Any]:
        """The background loop's latest reading — instant, no live sensor read in the request path. Falls back
        to a direct read only in the brief window before the loop's first tick has completed."""
        return self.latest if self.latest is not None else await asyncio.to_thread(_collect)
