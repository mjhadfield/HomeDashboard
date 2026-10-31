"""Storage and curation for the content cards: followed artists, discovered items, booked gigs.

Two ideas matter here:

* **Silent baseline.** The first time we look at an artist, everything already out there is stored as 'seen'.
  Only things that appear *later* are 'new' — otherwise following an artist would light up the whole back catalogue.
* **Curation survives refreshes.** A refresh updates an item's details but never its status, so a pin or a
  dismissal sticks.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .db import db

STATUSES = ("new", "seen", "pinned", "dismissed", "purchased")
ARTIST_STATUSES = ("follow", "pin", "mute")
CHECK_COLUMNS = ("mb_checked_at", "vinyl_checked_at", "radar_checked_at")     # whitelist: these are interpolated into SQL


def norm(text: str | None) -> str:
    """Loose comparison key: lower-case letters/digits only ('Motörhead' -> 'motorhead' isn't attempted; accents kept)."""
    return re.sub(r"[^\w]+", "", (text or "").casefold())


# MusicBrainz/Discogs use placeholder "artists" for compilations and unknowns. They are never something to follow.
JUNK_ARTISTS = {"various", "various artists", "unknown", "[unknown]", "no artist", "[no artist]", "anonymous", "[anonymous]", "traditional", "[traditional]", "n/a"}
JUNK_MBIDS = {"89ad4ac3-39f7-470e-963a-56509c546377",   # Various Artists
              "125ec42a-7229-4250-afc5-e057484327fe",   # [unknown]
              "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",   # [no artist]
              "f731ccc4-e22a-43af-a747-64213329e088"}   # [anonymous]
_JUNK_KEYS = {norm(j) for j in JUNK_ARTISTS}


def is_junk_artist(name: str | None, mbid: str | None = None) -> bool:
    return (mbid in JUNK_MBIDS) or norm(name) in _JUNK_KEYS


def purge_junk() -> int:
    """Remove placeholder artists (and anything found for them) that an earlier version may have followed."""
    removed = 0
    with db() as con:
        for r in con.execute("SELECT id, name, mbid FROM followed_artists").fetchall():
            if is_junk_artist(r["name"], r["mbid"]):
                con.execute("DELETE FROM followed_artists WHERE id = ?", (r["id"],))
                removed += 1
        for r in con.execute("SELECT DISTINCT artist FROM feed_items WHERE artist IS NOT NULL").fetchall():
            if is_junk_artist(r["artist"]):
                removed += con.execute("DELETE FROM feed_items WHERE artist = ?", (r["artist"],)).rowcount
    return removed


# ---- items ---------------------------------------------------------------------------------------------------

# Things the *person* set on an item (opened it, voted on it, chose to see a spoiler) rather than the source: a refresh
# rewrites the rest of `meta` but carries these over, so they're never silently lost while a story is still in its feed.
STICKY_META = ("read", "vote", "revealed", "own")
# A confirmed pre-order corrected by hand (edit_purchase) keeps the owner's version in meta['own']; these are the parts of
# it that live in `meta` itself — the rest (artist, title, url) are columns, put back after the store's refresh.
OWN_META = ("variant", "image", "shop")


def upsert_items(kind: str, source: str, items: list[dict[str, Any]], *, baseline: bool) -> int:
    """Store items. New rows start 'seen' when `baseline` (first look at that artist) else 'new'.
    Existing rows are refreshed but keep their status (and STICKY_META). Returns how many were newly inserted."""
    now = int(time.time())
    inserted = 0
    with db() as con:
        for it in items:
            exists = con.execute("SELECT meta FROM feed_items WHERE kind = ? AND source = ? AND external_id = ?", (kind, source, it["external_id"])).fetchone()
            meta = dict(it.get("meta") or {})
            own = None
            if exists:
                old = json.loads(exists["meta"] or "{}")
                meta.update({k: old[k] for k in STICKY_META if k in old})
                if own := old.get("own"):
                    meta.update({k: own[k] for k in OWN_META if k in own})
            con.execute(
                "INSERT INTO feed_items (kind, source, external_id, artist, title, subtitle, url, date, meta, status, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, source, external_id) DO UPDATE SET artist = excluded.artist, title = excluded.title, subtitle = excluded.subtitle, "
                "url = excluded.url, date = CASE WHEN feed_items.date_set = 1 THEN feed_items.date ELSE excluded.date END, "   # a hand-set date wins
                "meta = excluded.meta, last_seen = excluded.last_seen",
                (kind, source, it["external_id"], it.get("artist"), it["title"][:300], (it.get("subtitle") or "")[:300], it.get("url"), it.get("date"),
                 json.dumps(meta), "seen" if baseline else "new", now, now),
            )
            if own:                                      # the owner's correction of a confirmed pre-order beats the store's listing
                con.execute("UPDATE feed_items SET artist = ?, title = ?, url = ? WHERE kind = ? AND source = ? AND external_id = ?",
                            (own.get("artist"), own.get("title"), own.get("url"), kind, source, it["external_id"]))
            inserted += 0 if exists else 1
    return inserted


def prune(kind: str, *, older_than_days: int, sources: list[str] | None = None, keep_liked: bool = False) -> int:
    """Forget items we haven't seen for a while — except pinned or purchased ones (and, with keep_liked, ones voted 👍).
    `sources` limits it to those sources: News keeps each section for as long as that section says."""
    cutoff = int(time.time()) - older_than_days * 86400
    sql = "DELETE FROM feed_items WHERE kind = ? AND last_seen < ? AND status NOT IN ('pinned', 'purchased')"
    args: list[Any] = [kind, cutoff]
    if sources is not None:
        if not sources:
            return 0
        sql += f" AND source IN ({','.join('?' * len(sources))})"
        args += sources
    if keep_liked:
        sql += " AND COALESCE(json_extract(meta, '$.vote'), 0) != 1"
    with db() as con:
        return con.execute(sql, args).rowcount


def has_source(kind: str, source: str) -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM feed_items WHERE kind = ? AND source = ? LIMIT 1", (kind, source)).fetchone() is not None


def drop_missing(kind: str, source: str, keep_ids: set[str]) -> int:
    """After a *successful* look at a source, forget its items that were not there this time (e.g. a pre-order that has
    shipped or sold out). Pinned or purchased items are kept. Compared by id, not by clock, and never called after a
    failed fetch, so an outage can't empty the card."""
    with db() as con:
        have = [r["external_id"] for r in con.execute("SELECT external_id FROM feed_items WHERE kind = ? AND source = ? AND status NOT IN ('pinned', 'purchased')", (kind, source))]
        gone = [e for e in have if e not in keep_ids]
        for e in gone:
            con.execute("DELETE FROM feed_items WHERE kind = ? AND source = ? AND external_id = ?", (kind, source, e))
    return len(gone)


def set_status(item_id: int, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    with db() as con:
        return con.execute("UPDATE feed_items SET status = ? WHERE id = ?", (status, item_id)).rowcount > 0


def mark_all_seen(kind: str) -> int:
    with db() as con:
        return con.execute("UPDATE feed_items SET status = 'seen' WHERE kind = ? AND status = 'new'", (kind,)).rowcount


def mark_read(item_id: int) -> bool:
    """Track that an item was actually opened — separate from `status`, which 'seen'/'Mark all seen' already use
    for a different meaning (acknowledged, not necessarily read). Kept in `meta` since it's a per-kind concept
    (News, today) rather than something every kind needs, so it doesn't touch the shared status enum/CHECK."""
    with db() as con:
        row = con.execute("SELECT meta FROM feed_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return False
        meta = json.loads(row["meta"] or "{}")
        if not meta.get("read"):
            meta["read"] = True
            con.execute("UPDATE feed_items SET meta = ? WHERE id = ?", (json.dumps(meta), item_id))
    return True


def list_purchased(kind: str) -> list[dict[str, Any]]:
    """Confirmed pre-orders: everything marked Purchased, soonest release first (undated last) — same ordering
    rule as everywhere else, via _ordered."""
    with db() as con:
        rows = con.execute("SELECT * FROM feed_items WHERE kind = ? AND status = 'purchased' ORDER BY last_seen DESC", (kind,)).fetchall()
    return _ordered([_row_to_item(r) for r in rows], oldest_first=True)


def add_manual_purchase(kind: str, *, artist: str, title: str, variant: str, date: str | None, image: str, url: str | None = None, shop: str = "") -> int:
    """A record bought somewhere this app doesn't watch, entered by hand — stored the same way as a scraped-then-
    purchased item so Confirmed Pre-orders is one list regardless of where a record came from. Inserted directly
    rather than through upsert_items(), which is shaped for a widget's own fetch cycle, not a one-off owner entry."""
    now = int(time.time())
    with db() as con:
        cur = con.execute(
            "INSERT INTO feed_items (kind, source, external_id, artist, title, subtitle, url, date, meta, status, first_seen, last_seen) "
            "VALUES (?, 'manual', ?, ?, ?, '', ?, ?, ?, 'purchased', ?, ?)",
            (kind, f"manual-{uuid4().hex}", artist[:300], title[:300], url, date, json.dumps({"variant": variant[:120], "image": image, "shop": shop[:60]}), now, now),
        )
    return cur.lastrowid


def edit_purchase(item_id: int, *, artist: str, title: str, variant: str, shop: str, url: str | None, image: str | None = None) -> bool:
    """Correct a confirmed pre-order by hand — e.g. it was ticked off a tracked store's listing but actually bought
    somewhere else, as a different variant. `image` None keeps the current cover. The correction is also kept in
    meta['own'], which upsert_items re-applies, so the store's next sync of that listing can't quietly undo it.
    False if there's no such confirmed pre-order."""
    with db() as con:
        row = con.execute("SELECT meta FROM feed_items WHERE id = ? AND status = 'purchased'", (item_id,)).fetchone()
        if not row:
            return False
        meta = json.loads(row["meta"] or "{}")
        own = {"artist": artist[:300], "title": title[:300], "url": url, "variant": variant[:120], "shop": shop[:60],
               "image": meta.get("image", "") if image is None else image}
        meta.update({k: own[k] for k in OWN_META})
        meta["own"] = own
        con.execute("UPDATE feed_items SET artist = ?, title = ?, url = ?, meta = ? WHERE id = ?", (own["artist"], own["title"], url, json.dumps(meta), item_id))
    return True


def set_date(item_id: int, date: str | None) -> bool:
    """A release date set by hand. It's kept through later syncs (upsert_items won't overwrite it); clearing it hands
    the date back to the store."""
    with db() as con:
        return con.execute("UPDATE feed_items SET date = ?, date_set = ? WHERE id = ?", (date, int(date is not None), item_id)).rowcount > 0


def delete_item(item_id: int) -> bool:
    with db() as con:
        return con.execute("DELETE FROM feed_items WHERE id = ?", (item_id,)).rowcount > 0


def _row_to_item(r: sqlite3.Row) -> dict[str, Any]:
    return {"id": r["id"], "artist": r["artist"], "title": r["title"], "subtitle": r["subtitle"], "url": r["url"], "date": r["date"],
            "status": r["status"], "source": r["source"], "meta": json.loads(r["meta"] or "{}")}


def sort_date(d: str | None) -> str:
    """A comparable full date for sorting: sources sometimes only know a year or a month ('2027', '2026-12'); those sort as
    the last day of that period so they land where a reader expects."""
    d = d or ""
    if len(d) == 4:
        return f"{d}-12-31"
    if len(d) == 7:
        return f"{d}-31"
    return d


def _newest(i: dict[str, Any]) -> str:
    """The date the item is about (release / show / story), else when it was listed."""
    return sort_date(i["date"]) or i["meta"].get("listed") or ""


def _ordered(items: list[dict[str, Any]], oldest_first: bool = False) -> list[dict[str, Any]]:
    """Pinned items first, then newest to oldest (or oldest to newest), undated last either way."""
    def by_date(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        dated = sorted([i for i in rows if _newest(i)], key=_newest, reverse=not oldest_first)
        return dated + [i for i in rows if not _newest(i)]
    return by_date([i for i in items if i["status"] == "pinned"]) + by_date([i for i in items if i["status"] != "pinned"])


EXCLUSIVE_NOTE = re.compile(r"\s*[\(\[][^()\[\]]*\bexclusive\b[^()\[\]]*[\)\]]\s*$", re.I)


def _album_key(it: dict[str, Any]) -> str:
    """One record across stores: "The Album" and "Album" are the same, and so is a store's own "Album (Metal Injection
    Exclusive)" — the note stays in the title shown, it just doesn't make a different record."""
    title = EXCLUSIVE_NOTE.sub("", it["title"]) or it["title"]
    return f"{norm(it['artist'])}|{norm(re.sub(r'^the\s+', '', title, flags=re.I))}"


def list_items(kind: str, *, oldest_first: bool = False, limit: int = 150, dismissed_limit: int = 30, source_priority: tuple[str, ...] = (), keep=None) -> dict[str, Any]:
    """Items to show for a card. Muted artists are hidden (not deleted). Dismissed items come back separately so the
    expanded view can offer to restore them."""
    with db() as con:
        rows = con.execute(
            "SELECT f.* FROM feed_items f LEFT JOIN followed_artists a ON a.name = f.artist COLLATE NOCASE "
            "WHERE f.kind = ? AND (a.status IS NULL OR a.status != 'mute') ORDER BY f.last_seen DESC LIMIT 1500", (kind,)).fetchall()
    items = [_row_to_item(r) for r in rows]
    bought = {_album_key(i) for i in items if i["status"] == "purchased"}
    items = [i for i in items if not is_junk_artist(i["artist"]) and i["status"] != "purchased" and (keep is None or keep(i))]

    if source_priority:                                       # the same album from two sources: keep the preferred one…
        items = [i for i in items if _album_key(i) not in bought]          # …and none at all once it's been bought, from any of them
        rank = {s: i for i, s in enumerate(source_priority)}
        best: dict[str, dict[str, Any]] = {}
        for it in sorted(items, key=lambda i: rank.get(i["source"], 99)):
            k = _album_key(it)
            if k not in best:
                best[k] = it
                continue
            keeper = best[k]                                   # …but it learns what the others know: a release date it lacked, and everywhere else it's sold
            if not keeper["date"] and it["date"]:
                keeper["date"] = it["date"]
            shop = it["meta"].get("shop")
            also = keeper["meta"].setdefault("also", [])
            if shop and shop != keeper["meta"].get("shop") and not any(a["shop"] == shop for a in also):
                eds = it["meta"].get("editions") or []          # this store's own pricing/pressing, kept rather than reduced to a bare name
                also.append({"shop": shop, "name": eds[0]["name"] if eds else (it["meta"].get("format") or "Standard"),
                             "price": eds[0]["price"] if eds else it["meta"].get("price", ""), "url": eds[0]["url"] if eds else it.get("url")})
        items = list(best.values())

    active = _ordered([i for i in items if i["status"] != "dismissed"], oldest_first)
    dismissed = _ordered([i for i in items if i["status"] == "dismissed"], oldest_first)[:dismissed_limit]
    shown = active[:limit]
    # `new` counts only what is actually delivered, so the badge never promises items the browser can't show.
    return {"items": shown, "dismissed": dismissed, "total": len(active), "new": sum(1 for i in shown if i["status"] == "new")}


# ---- followed artists ------------------------------------------------------------------------------------------

def list_artists() -> list[dict[str, Any]]:
    with db() as con:
        rows = con.execute("SELECT id, name, mbid, status, source, weight FROM followed_artists ORDER BY (status = 'pin') DESC, (status = 'mute'), weight DESC, name").fetchall()
    return [dict(r) for r in rows]


def top_artists(limit: int) -> set[str]:
    """Normalised names of the artists you care most about: every pinned one, plus the `limit` heaviest followed ones."""
    with db() as con:
        rows = con.execute("SELECT name FROM followed_artists WHERE status = 'pin' UNION "
                           "SELECT name FROM (SELECT name FROM followed_artists WHERE status = 'follow' ORDER BY weight DESC LIMIT ?)", (limit,)).fetchall()
    return {norm(r["name"]) for r in rows}


def fresh_items(kind: str, since: int, artists: set[str]) -> list[dict[str, Any]]:
    """Items first found since `since` that are still unread ('new') and by one of `artists` — newest first."""
    with db() as con:
        rows = con.execute("SELECT * FROM feed_items WHERE kind = ? AND status = 'new' AND first_seen >= ? ORDER BY first_seen DESC, id DESC",
                           (kind, since)).fetchall()
    return [{**_row_to_item(r), "first_seen": r["first_seen"]} for r in rows if norm(r["artist"]) in artists]


FRESH_SECONDS = 20 * 3600          # an artist checked within this long is not looked at again: each is checked about once a day
FORCED_SECONDS = 3600              # "Sync now" re-checks anything not looked at in the last hour (so double-clicking doesn't thrash a service)


def pick_artists(column: str, limit: int, *, min_age: int = 0) -> list[dict[str, Any]]:
    """Which artists to check this run: pinned first, then the least recently checked (never-checked first). Anyone checked
    within `min_age` seconds is left alone."""
    if column not in CHECK_COLUMNS:
        raise ValueError("bad column")
    with db() as con:
        rows = con.execute(
            f"SELECT * FROM followed_artists WHERE status IN ('follow', 'pin') AND ({column} IS NULL OR {column} <= ?) "
            f"ORDER BY (status = 'pin') DESC, {column} IS NOT NULL, {column}, weight DESC LIMIT ?", (int(time.time()) - min_age, limit)).fetchall()
    return [dict(r) for r in rows]


def count_unchecked(column: str) -> int:
    """Followed artists we have never looked at (for this source) — the catch-up backlog."""
    if column not in CHECK_COLUMNS:
        raise ValueError("bad column")
    with db() as con:
        return con.execute(f"SELECT COUNT(*) c FROM followed_artists WHERE status IN ('follow', 'pin') AND {column} IS NULL").fetchone()["c"]


def count_stale(column: str, min_age: int) -> int:
    """Followed artists that are due a check (never checked, or not within `min_age` seconds)."""
    if column not in CHECK_COLUMNS:
        raise ValueError("bad column")
    with db() as con:
        return con.execute(f"SELECT COUNT(*) c FROM followed_artists WHERE status IN ('follow', 'pin') AND ({column} IS NULL OR {column} <= ?)",
                           (int(time.time()) - min_age,)).fetchone()["c"]


def mark_checked(artist_id: int, column: str) -> None:
    if column not in CHECK_COLUMNS:
        raise ValueError("bad column")
    with db() as con:
        con.execute(f"UPDATE followed_artists SET {column} = ? WHERE id = ?", (int(time.time()), artist_id))


def set_mbid(artist_id: int, mbid: str) -> None:
    with db() as con:
        con.execute("UPDATE followed_artists SET mbid = ? WHERE id = ?", (mbid, artist_id))


def add_artist(name: str, *, source: str = "manual", status: str = "follow") -> int | None:
    name = name.strip()
    if not name:
        raise ValueError("artist name is empty")
    if is_junk_artist(name):
        raise ValueError(f"“{name}” is a placeholder used for compilations, not an artist you can follow")
    with db() as con:
        try:
            return con.execute("INSERT INTO followed_artists (name, status, source, created_at) VALUES (?, ?, ?, ?)", (name[:80], status, source, int(time.time()))).lastrowid
        except sqlite3.IntegrityError:
            return None                                          # already followed


def set_artist_status(artist_id: int, status: str) -> bool:
    if status not in ARTIST_STATUSES:
        raise ValueError(f"status must be one of {ARTIST_STATUSES}")
    with db() as con:
        return con.execute("UPDATE followed_artists SET status = ? WHERE id = ?", (status, artist_id)).rowcount > 0


def mute_artist_by_name(name: str) -> bool:
    with db() as con:
        return con.execute("UPDATE followed_artists SET status = 'mute' WHERE name = ?", (name.strip(),)).rowcount > 0


def delete_artist(artist_id: int) -> bool:
    with db() as con:
        return con.execute("DELETE FROM followed_artists WHERE id = ?", (artist_id,)).rowcount > 0


# ---- seeding from Music Companion ----------------------------------------------------------------------------------

def _ro(path: Path) -> sqlite3.Connection:
    """Open the Music Companion database read-only — this dashboard never modifies another project's data."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


SEED_LIMIT = 200          # how many of your top artists are followed

SEED_SQL = """
SELECT name, mbid, plays, vinyl, gigs, (plays / 50.0 + vinyl * 6 + gigs * 4) AS score FROM (
    SELECT a.name AS name, a.mbid AS mbid,
           (SELECT COUNT(*) FROM scrobbles s WHERE s.artist_id = a.id) AS plays,
           (SELECT COUNT(*) FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id WHERE al.artist_id = a.id) AS vinyl,
           (SELECT COUNT(*) FROM setlists sl WHERE sl.artist_id = a.id) AS gigs
    FROM artists a)
WHERE plays >= ? OR vinyl > 0 OR gigs > 0
ORDER BY score DESC LIMIT ?
"""


def seed_from_music_db(path: Path, *, limit: int = SEED_LIMIT, min_plays: int = 50) -> dict[str, int]:
    """Follow your top artists (by listening, vinyl owned and gigs seen). Existing choices — a mute, a pin — are kept;
    only the score and a missing MusicBrainz id are refreshed."""
    if not path.exists():
        return {"found": 0, "added": 0, "updated": 0}
    con = _ro(path)
    try:
        rows = con.execute(SEED_SQL, (min_plays, limit + 20)).fetchall()
    finally:
        con.close()
    rows = [r for r in rows if not is_junk_artist(r[0], r[1])][:limit]
    added = updated = 0
    now = int(time.time())
    with db() as d:
        for name, mbid, _plays, _vinyl, _gigs, score in rows:
            cur = d.execute("SELECT id FROM followed_artists WHERE name = ?", (name,)).fetchone()
            if cur:
                d.execute("UPDATE followed_artists SET weight = ?, mbid = COALESCE(mbid, ?) WHERE id = ?", (score, mbid, cur["id"]))
                updated += 1
            else:
                d.execute("INSERT INTO followed_artists (name, mbid, status, source, weight, created_at) VALUES (?, ?, 'follow', 'music-companion', ?, ?)", (name, mbid, score, now))
                added += 1
    return {"found": len(rows), "added": added, "updated": updated}


# ---- booked gigs -------------------------------------------------------------------------------------------------------

def list_gigs() -> list[dict[str, Any]]:
    with db() as con:
        return [dict(r) for r in con.execute("SELECT * FROM gigs ORDER BY date, id")]
