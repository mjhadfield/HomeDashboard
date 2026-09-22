"""Endpoints for screens (TVs): unauthenticated pairing, and the read-only kiosk API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, state
from .bus import bus

# --- pairing: public by necessity (the TV has no credentials yet), but tiny and bounded ------------------
public = APIRouter(prefix="/api/pair")


class PollIn(BaseModel):
    secret: str = Field(min_length=10, max_length=100)


@public.post("/start")
async def pair_start():
    started = auth.pair_start()
    if not started:
        raise HTTPException(429, "Too many screens are waiting to be paired. Try again in a few minutes.")
    return started


@public.post("/poll")
async def pair_poll(body: PollIn, request: Request, response: Response):
    status, cookie = auth.pair_claim(body.secret, request.headers.get("user-agent", ""))
    if status == "gone":
        raise HTTPException(410, "This pairing code has expired.")
    if status == "ok":
        response.set_cookie(auth.COOKIE, cookie, max_age=auth.COOKIE_MAX_AGE, httponly=True, samesite="lax", path="/")
    return {"status": status}


# --- kiosk: what a paired screen may read ---------------------------------------------------------------
kiosk = APIRouter(prefix="/api/kiosk", dependencies=[Depends(auth.require_kiosk)])


@kiosk.get("/state")
async def kiosk_state():
    return state.kiosk_state()


@kiosk.get("/events")
async def kiosk_events():
    return StreamingResponse(bus.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
