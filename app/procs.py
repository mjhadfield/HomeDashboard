"""Start/stop projects as transient systemd *user* units (`hub-proj-<slug>.service`).

Why systemd rather than subprocess: each project becomes a supervised unit with a proper cgroup (so `stop`
kills the whole process tree, e.g. Vite's children), journald captures its output for the log view, and it
keeps running if the dashboard itself restarts.

Commands are argv lists and are never passed through a shell by us.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
started_at: dict[str, float] = {}  # slug -> when we last started it (for the "starting" state)


class ProcError(Exception):
    pass


def unit(slug: str) -> str:
    if not SLUG_RE.match(slug):
        raise ProcError(f"invalid project slug {slug!r}")
    return f"hub-proj-{slug}.service"


async def _run(*args: str, timeout: float = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ProcError(f"{args[0]} timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def active_units() -> set[str]:
    """Names of hub-proj units that are currently running (transient units vanish once stopped)."""
    rc, out, _ = await _run("systemctl", "--user", "list-units", "hub-proj-*.service", "--all", "--no-legend", "--plain", "--no-pager")
    active: set[str] = set()
    for line in out.splitlines():
        parts = line.split(None, 4)  # UNIT LOAD ACTIVE SUB DESCRIPTION
        if len(parts) >= 4 and parts[2] in ("active", "activating", "reloading"):
            active.add(parts[0])
    return active


async def active_since() -> dict[str, float]:
    """unit name -> epoch seconds it became active, for every hub-proj unit currently active. Reads systemd's own
    record of when the unit started (not our own memory of it), so it's right even across a dashboard restart, and
    for a project someone started outside the dashboard by re-running `hub-proj-<slug>` themselves.

    Uses the *monotonic* timestamp systemd tracks (microseconds since boot) rather than parsing its human-readable
    wall-clock one, which is locale- and timezone-abbreviation-dependent and awkward to parse reliably. Python's own
    `time.monotonic()` reads the same underlying clock (CLOCK_MONOTONIC) on Linux, so the two are directly comparable."""
    rc, out, _ = await _run("systemctl", "--user", "show", "hub-proj-*.service", "--property=Id,ActiveEnterTimestampMonotonic", "--no-pager")
    if rc != 0:
        return {}
    now_mono, now_wall = time.monotonic(), time.time()
    since: dict[str, float] = {}
    for block in out.split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        usec = int(props.get("ActiveEnterTimestampMonotonic") or 0)
        if props.get("Id") and usec:
            since[props["Id"]] = now_wall - (now_mono - usec / 1_000_000)
    return since


async def start(project: dict[str, Any]) -> None:
    slug = project["slug"]
    argv = json.loads(project["start_argv"])
    if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)):
        raise ProcError("start command must be a non-empty list of strings")
    cwd = project["cwd"]
    if not os.path.isdir(cwd):
        raise ProcError(f"working directory does not exist: {cwd}")
    exe = argv[0]
    if "/" in exe and not os.path.isabs(exe):
        exe = os.path.normpath(os.path.join(cwd, exe))  # NOT resolve(): a venv's python is a symlink and must stay one
    args = [
        "systemd-run", "--user", f"--unit=hub-proj-{slug}", "--collect", "--quiet",
        f"--description=Citadel: {project['name']}", f"--working-directory={cwd}",
        "--property=TimeoutStopSec=10", "--property=KillMode=control-group",
    ]
    for key, value in json.loads(project.get("env") or "{}").items():
        args += ["--setenv", f"{key}={value}"]
    rc, _, err = await _run(*args, "--", exe, *argv[1:])
    if rc != 0:
        if "already" in err.lower():
            return  # already running under our unit name — treat start as idempotent
        raise ProcError(err.strip().splitlines()[-1] if err.strip() else f"systemd-run exited {rc}")
    started_at[slug] = time.time()


async def stop(slug: str) -> None:
    rc, _, err = await _run("systemctl", "--user", "stop", unit(slug))
    started_at.pop(slug, None)
    if rc != 0 and "not loaded" not in err.lower():
        raise ProcError(err.strip() or f"systemctl stop exited {rc}")


async def logs(slug: str, lines: int = 200) -> list[dict[str, str]]:
    """Recent journal lines for a project unit (works after the unit has exited, too)."""
    rc, out, _ = await _run(
        "journalctl", "--user", "-u", unit(slug), "-n", str(max(1, min(lines, 1000))),
        "--no-pager", "-o", "json", "--output-fields=MESSAGE,__REALTIME_TIMESTAMP",
    )
    result = []
    for raw in out.splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        message = entry.get("MESSAGE")
        if isinstance(message, list):  # journald encodes non-UTF-8 output as a byte array
            message = bytes(message).decode(errors="replace")
        ts = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1_000_000
        result.append({"t": time.strftime("%H:%M:%S", time.localtime(ts)), "msg": str(message or "")})
    return result
