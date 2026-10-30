"""Owner API for the content cards: curating items, following artists, booking gigs."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from . import auth, config, content, db, notify
from .schemas import http_url
from .widgets import vinyl

router = APIRouter(prefix="/api/owner", dependencies=[Depends(auth.require_owner)])

KIND_WIDGET = {"release": "releases", "vinyl": "vinyl", "radar": "radar", "news": "news"}


# ---- curating items ------------------------------------------------------------------------------------------

class ItemStatusIn(BaseModel):
    status: Literal["new", "seen", "pinned", "dismissed", "purchased"]


@router.post("/items/{item_id}/status")
async def item_status(item_id: int, body: ItemStatusIn):
    with db.db() as con:
        row = con.execute("SELECT kind FROM feed_items WHERE id = ?", (item_id,)).fetchone()
    if not row or not content.set_status(item_id, body.status):
        raise HTTPException(404, "no such item")
    notify.widget(KIND_WIDGET[row["kind"]])
    return {"ok": True}


@router.post("/items/mark-seen/{kind}")
async def mark_seen(kind: Literal["release", "vinyl", "radar", "news"]):
    n = content.mark_all_seen(kind)
    notify.widget(KIND_WIDGET[kind])
    return {"ok": True, "cleared": n}


@router.post("/items/{item_id}/read")
async def item_read(item_id: int):
    """Opened, not just acknowledged — distinct from status='seen' (see content.mark_read)."""
    with db.db() as con:
        row = con.execute("SELECT kind FROM feed_items WHERE id = ?", (item_id,)).fetchone()
    if not row or not content.mark_read(item_id):
        raise HTTPException(404, "no such item")
    notify.widget(KIND_WIDGET[row["kind"]])
    return {"ok": True}


class ItemDateIn(BaseModel):
    date: str | None = None

    @field_validator("date")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            raise ValueError("date must be a real date like 2026-09-26, or blank for TBC")


@router.patch("/items/{item_id}/date")
async def item_date(item_id: int, body: ItemDateIn):
    """A pre-order's own release date is store-supplied and often TBC at first — this lets it be filled in or
    corrected by hand once known, without waiting for the store to publish one (or overriding a wrong one)."""
    with db.db() as con:
        row = con.execute("SELECT kind FROM feed_items WHERE id = ?", (item_id,)).fetchone()
    if not row or not content.set_date(item_id, body.date):
        raise HTTPException(404, "no such item")
    notify.widget(KIND_WIDGET[row["kind"]])
    return {"ok": True}


@router.delete("/items/{item_id}")
async def item_delete(item_id: int):
    """Removing an item outright is deliberately scoped to Confirmed Pre-orders ('received it, done tracking it')
    — not a general delete-anything endpoint; every other card curates via status (dismiss/pin/etc.)."""
    with db.db() as con:
        row = con.execute("SELECT kind, status FROM feed_items WHERE id = ?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "no such item")
    if row["status"] != "purchased":
        raise HTTPException(409, "only a purchased item can be removed this way")
    content.delete_item(item_id)
    notify.widget(KIND_WIDGET[row["kind"]])
    return {"ok": True}


# ---- vinyl: manually-added purchases --------------------------------------------------------------------------

class ManualPreorderIn(BaseModel):
    artist: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    variant: str = Field("", max_length=120)
    date: str | None = None
    image_url: str | None = Field(None, max_length=500)
    product_url: str | None = Field(None, max_length=500)

    @field_validator("date")
    @classmethod
    def _date(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            raise ValueError("date must be a real date like 2026-09-26, or blank if TBC")

    @field_validator("image_url")
    @classmethod
    def _image(cls, v: str | None) -> str | None:
        return http_url(v, field="cover image") if v and v.strip() else None

    @field_validator("product_url")
    @classmethod
    def _product(cls, v: str | None) -> str | None:
        return http_url(v, field="product page link") if v and v.strip() else None


@router.post("/vinyl/preorders", status_code=201)
async def add_manual_preorder(body: ManualPreorderIn):
    """Settings → Music → Vinyl pre-orders: a record bought somewhere this app doesn't watch, added by hand so it
    shows up in Confirmed Pre-orders alongside anything bought straight from a tracked store."""
    image = await vinyl.fetch_manual_cover(body.image_url) if body.image_url else ""
    iid = content.add_manual_purchase("vinyl", artist=body.artist.strip(), title=body.title.strip(),
                                       variant=body.variant.strip(), date=body.date, image=image, url=body.product_url)
    notify.widget("vinyl")
    return {"id": iid}


# ---- followed artists ----------------------------------------------------------------------------------------

class ArtistIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ArtistPatch(BaseModel):
    status: Literal["follow", "pin", "mute"]


def _kick(request: Request) -> None:
    """A change to who you follow: check the new artists soon rather than at the next scheduled sync."""
    for wid in ("releases", "vinyl", "radar"):
        request.app.state.scheduler.refresh(wid)


@router.get("/artists")
async def get_artists():
    return {"artists": content.list_artists(), "music_db": config.MUSIC_DB.exists()}


@router.post("/artists", status_code=201)
async def add_artist(body: ArtistIn, request: Request):
    try:
        aid = content.add_artist(body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if aid is None:
        raise HTTPException(409, f"You already follow {body.name.strip()}.")
    notify.config()
    _kick(request)
    return {"id": aid}


@router.post("/artists/mute")
async def mute_artist(body: ArtistIn):
    """One click from a card: hide everything about this artist. Undo from Settings → Music."""
    if not content.mute_artist_by_name(body.name):
        raise HTTPException(404, "You don't follow that artist.")
    notify.config()
    for wid in KIND_WIDGET.values():
        notify.widget(wid)
    return {"ok": True}


@router.patch("/artists/{artist_id}")
async def patch_artist(artist_id: int, body: ArtistPatch):
    if not content.set_artist_status(artist_id, body.status):
        raise HTTPException(404, "no such artist")
    notify.config()
    for wid in KIND_WIDGET.values():
        notify.widget(wid)                                # muting hides that artist's items straight away
    return {"ok": True}


@router.delete("/artists/{artist_id}")
async def remove_artist(artist_id: int):
    if not content.delete_artist(artist_id):
        raise HTTPException(404, "no such artist")
    notify.config()
    return {"ok": True}


@router.post("/artists/resync")
async def resync_artists(request: Request):
    if not config.MUSIC_DB.exists():
        raise HTTPException(404, f"Music Companion's database wasn't found at {config.MUSIC_DB}")
    result = content.seed_from_music_db(config.MUSIC_DB)
    notify.config()
    _kick(request)
    return result


# ---- booked gigs ---------------------------------------------------------------------------------------------

class GigIn(BaseModel):
    artist: str = Field(min_length=1, max_length=80)
    venue: str = Field("", max_length=80)
    city: str = Field("", max_length=60)
    date: str
    doors: str = Field("", max_length=5)
    ticket_url: str | None = Field(None, max_length=500)
    notes: str = Field("", max_length=200)

    @field_validator("date")
    @classmethod
    def _date(cls, v: str) -> str:
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            raise ValueError("date must be a real date like 2026-09-26")

    @field_validator("doors")
    @classmethod
    def _doors(cls, v: str) -> str:
        if v and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("doors must look like 18:30")
        return v

    @field_validator("ticket_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return http_url(v, field="ticket link") if v and v.strip() else None


def _gig_values(g: GigIn) -> tuple:
    return (g.artist.strip(), g.venue.strip(), g.city.strip(), g.date, g.doors, g.ticket_url, g.notes.strip())


@router.get("/gigs")
async def get_gigs():
    return {"gigs": content.list_gigs()}


@router.post("/gigs", status_code=201)
async def create_gig(body: GigIn):
    with db.db() as con:
        gid = con.execute("INSERT INTO gigs (artist, venue, city, date, doors, ticket_url, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (*_gig_values(body), int(time.time()))).lastrowid
    notify.widget("gigs")
    return {"id": gid}


@router.post("/radar/{item_id}/book", status_code=201)
async def book_from_radar(item_id: int, body: GigIn):
    """Gig radar → Booked: the show becomes a booked gig and leaves the radar (status 'purchased', which list_items
    already hides — and with it the show's on-sale reminders). One transaction, so it never half-happens. The diary
    entry is the browser's job: it opens a filled-in Google Calendar event, as the Diary card can only read."""
    with db.db() as con:
        row = con.execute("SELECT kind, status FROM feed_items WHERE id = ?", (item_id,)).fetchone()
        if not row or row["kind"] != "radar":
            raise HTTPException(404, "no such show on the radar")
        if row["status"] == "purchased":
            raise HTTPException(409, "that show is already booked")
        gid = con.execute("INSERT INTO gigs (artist, venue, city, date, doors, ticket_url, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (*_gig_values(body), int(time.time()))).lastrowid
        con.execute("UPDATE feed_items SET status = 'purchased' WHERE id = ?", (item_id,))
    notify.widget("gigs")
    notify.widget("radar")
    return {"id": gid}


@router.put("/gigs/{gig_id}")
async def update_gig(gig_id: int, body: GigIn):
    with db.db() as con:
        n = con.execute("UPDATE gigs SET artist=?, venue=?, city=?, date=?, doors=?, ticket_url=?, notes=? WHERE id=?", (*_gig_values(body), gig_id)).rowcount
    if not n:
        raise HTTPException(404, "no such gig")
    notify.widget("gigs")
    return {"ok": True}


@router.delete("/gigs/{gig_id}")
async def delete_gig(gig_id: int):
    with db.db() as con:
        n = con.execute("DELETE FROM gigs WHERE id = ?", (gig_id,)).rowcount
    if not n:
        raise HTTPException(404, "no such gig")
    notify.widget("gigs")
    return {"ok": True}


# ---- checking a store before adding it ----------------------------------------------------------------------

class ShopCheckIn(BaseModel):
    url: str = Field(max_length=300)
    collection: str = Field("", max_length=120)
    kind: str = Field("shopify", pattern="^(shopify|juno|wix)$")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return http_url(v, field="store address")

    @field_validator("collection")
    @classmethod
    def _coll(cls, v: str) -> str:
        v = v.strip()
        if v and not re.fullmatch(r"[a-z0-9][a-z0-9_,-]{0,118}", v):
            raise ValueError("collection must be the handle from the store's address, e.g. pre-orders")
        return v


@router.post("/shops/check")
async def check_shop(body: ShopCheckIn):
    """Try a store: is it readable, how many vinyl pre-orders does it list, and a few sample titles."""
    if body.kind == "shopify" and "," in body.collection:
        raise HTTPException(422, "a Shopify collection is a single handle, e.g. pre-orders")
    return await vinyl.check_shop(body.url, body.collection, body.kind)
