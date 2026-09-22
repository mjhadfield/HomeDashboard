"""Owner API for Settings → Maintenance: the schedule table, running a job now, and the event log."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import auth, logbook, maintenance

router = APIRouter(prefix="/api/owner", dependencies=[Depends(auth.require_owner)])
log = logging.getLogger("hub.maintenance")


@router.get("/maintenance")
async def get_maintenance(request: Request):
    return maintenance.overview(request.app.state.scheduler)


class ScheduleIn(BaseModel):
    mode: Literal["daily", "every", "default"]
    at: str | None = Field(None, max_length=5)
    seconds: int | None = None


@router.put("/maintenance/{job_id}/schedule")
async def put_schedule(job_id: str, body: ScheduleIn, request: Request):
    try:
        sched = maintenance.set_schedule(request.app.state.scheduler, job_id, body.model_dump(exclude_none=True))
    except KeyError:
        raise HTTPException(404, "no such job")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"ok": True, "schedule": sched}


@router.post("/maintenance/{job_id}/run", status_code=202)
async def run_job(job_id: str, request: Request):
    if job_id not in request.app.state.scheduler.widgets:
        raise HTTPException(404, "no such job")
    request.app.state.scheduler.refresh(job_id)
    return {"ok": True}


@router.get("/logs")
async def get_logs(level: Literal["all", "warning", "error", "info"] = "all", source: str = Query("", max_length=40), q: str = Query("", max_length=100),
                   limit: int = Query(200, ge=1, le=500), before: int | None = Query(None, ge=1)):
    items = logbook.query(level=None if level == "all" else level, source=source or None, text=q or None, limit=limit, before=before)
    return {"items": items, "sources": logbook.sources(), "counts": logbook.counts()}


@router.delete("/logs")
async def clear_logs(request: Request):
    n = logbook.clear()
    log.info("The event log was cleared (%d entries)", n, extra={"keep": True})
    return {"ok": True, "cleared": n}
