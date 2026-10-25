"""Playlists Citadel designs and keeps on Spotify.

A playlist is a design — ingredients with shares, plus rules — and a current track list. *Building* picks a fresh list
from the design (locked tracks stay put); *saving* writes it to Spotify, creating the playlist the first time and
replacing its contents after that, so it keeps the same link, followers and place in your library. A *fixed* playlist
only changes when you rebuild it; a *rotating* one is rebuilt and saved on its schedule by the rotator.

Picking is deliberately a little random (weighted towards the strongest candidates), so a rebuild or a rotation gives a
different mix rather than the same top twenty every time."""
from __future__ import annotations

import json
import logging
import random
import re
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from .. import config, db
from ..scheduler import parse_hhmm
from . import gigs, ingredients, library, matcher, store
from .services import spotify as sp
from .services.base import ServiceError

log = logging.getLogger("hub.music")

ORDERS = ("spread", "shuffle", "chrono", "setlist", "plays")
EVERY = ("daily", "weekly", "monthly")
RULE_DEFAULTS = {"len": 20, "per": 3, "fam": 50, "skip_days": 0, "norep": 0, "order": "spread"}

# What "New playlist" offers. Names are what the person sees; ingredients use their kind's defaults unless given here.
TEMPLATES: dict[str, dict[str, Any]] = {
    "forgotten": {"name": "Forgotten Favourites", "blurb": "Songs you used to hammer and haven’t played in a year", "mode": "rot", "every": "weekly",
                  "ings": [("history", 80, {}), ("deep", 20, {})], "rules": {"fam": 85, "norep": 4}},
    "deep": {"name": "Deep Cuts", "blurb": "Album tracks you’ve never played, by artists you love", "ings": [("deep", 100, {})], "rules": {"fam": 0}},
    "shelf": {"name": "From the Shelf", "blurb": "Your records — including tracks you’ve never streamed", "mode": "rot", "every": "monthly",
              "ings": [("shelf", 100, {})], "rules": {"per": 2, "norep": 3}},
    "capsule": {"name": "Time Capsule", "blurb": "A season or year of your listening", "ings": [("capsule", 100, {})], "rules": {"order": "plays", "fam": 100}},
    "new": {"name": "New from Followed", "blurb": "Fresh albums from the artists you follow", "mode": "rot", "every": "weekly", "day": 4,
            "ings": [("releases", 100, {})], "rules": {"len": 30, "per": 3, "fam": 0}},
    "never": {"name": "Discovery", "blurb": "Artists like yours that you’ve never played", "mode": "rot", "every": "weekly", "day": 4,
              "ings": [("similar", 60, {}), ("related", 40, {})], "rules": {"len": 16, "per": 2, "fam": 0, "norep": 8}},
    "genre": {"name": "Genre Mix", "blurb": "One genre — your favourites and some new names", "ings": [("genre", 70, {}), ("tag", 30, {})], "rules": {"fam": 70}},
    "pregig": {"name": "Pre-gig", "blurb": "Predicted setlist for a booked gig", "ings": [("gig", 100, {})], "rules": {"order": "setlist", "per": 0, "len": 30}},
    "blank": {"name": "Untitled", "blurb": "Start from nothing", "ings": [], "rules": {}},
}


class PlaylistError(ValueError):
    pass


# ---- reading and writing designs -------------------------------------------------------------------------------------

def _row(r) -> dict[str, Any]:
    d = dict(r)
    for k in ("schedule", "ings", "rules", "meta"):
        d[k] = json.loads(d[k] or ("[]" if k == "ings" else "{}"))
    d["public"] = bool(d["public"])
    d["rules"] = {**RULE_DEFAULTS, **d["rules"]}
    return d


def get(pid: int) -> dict[str, Any]:
    with db.db() as con:
        r = con.execute("SELECT * FROM music_playlists WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise LookupError("no such playlist")
    return _row(r)


def tracks(pid: int) -> list[dict[str, Any]]:
    with db.db() as con:
        return [dict(r) | {"locked": bool(r["locked"])} for r in con.execute("SELECT * FROM music_playlist_tracks WHERE playlist_id = ? ORDER BY pos", (pid,))]


def all_playlists() -> list[dict[str, Any]]:
    with db.db() as con:
        rows = [_row(r) for r in con.execute("SELECT * FROM music_playlists ORDER BY sort, id")]
        counts = {r["playlist_id"]: (r["n"], r["matched"], r["covers"]) for r in con.execute(
            "SELECT playlist_id, COUNT(*) AS n, SUM(uri != '') AS matched, json_group_array(COALESCE(mbid, '')) AS covers FROM music_playlist_tracks GROUP BY playlist_id")}
    for p in rows:
        n, matched, covers = counts.get(p["id"], (0, 0, "[]"))
        p["count"], p["matched"] = n, matched or 0
        p["covers"] = [c for c in json.loads(covers) if c][:4]
        p["next_run"] = next_run(p)
    return rows


def _check_schedule(s: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode != "rot":
        return {}
    every = s.get("every", "weekly")
    if every not in EVERY:
        raise PlaylistError("refresh must be daily, weekly or monthly")
    at = s.get("at") or store.settings()["rotate_at"]
    if not parse_hhmm(at):
        raise PlaylistError("time must look like 06:00")
    day = int(s.get("day") or (0 if every == "weekly" else 1))
    if every == "weekly" and not 0 <= day <= 6 or every == "monthly" and not 1 <= day <= 28:
        raise PlaylistError("pick a day of the week, or a day of the month from 1 to 28")
    out = {"every": every, "day": day, "at": at}
    if s.get("until"):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(s["until"])):
            raise PlaylistError("until must be a date")
        out["until"] = s["until"]
    return out


def _check_rules(r: dict[str, Any]) -> dict[str, Any]:
    r = {**RULE_DEFAULTS, **{k: v for k, v in (r or {}).items() if k in RULE_DEFAULTS}}
    try:
        out = {"len": max(5, min(200, int(r["len"]))), "per": max(0, min(50, int(r["per"]))), "fam": max(0, min(100, int(r["fam"]))),
               "skip_days": max(0, min(365, int(r["skip_days"]))), "norep": max(0, min(12, int(r["norep"])))}
    except (TypeError, ValueError):
        raise PlaylistError("rules must be numbers")
    if r["order"] not in ORDERS:
        raise PlaylistError("unknown order")
    out["order"] = r["order"]
    return out


def save_design(body: dict[str, Any], pid: int | None = None) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()[:80]
    if not name:
        raise PlaylistError("give the playlist a name")
    mode = body.get("mode", "fixed")
    if mode not in ("fixed", "rot"):
        raise PlaylistError("a playlist is fixed or rotating")
    try:
        ings = ingredients.check(body.get("ings", []))
    except ValueError as e:
        raise PlaylistError(str(e))
    values = (name, mode, json.dumps(_check_schedule(body.get("schedule") or {}, mode)), int(bool(body.get("public", store.settings()["public"]))),
              json.dumps(ings), json.dumps(_check_rules(body.get("rules") or {})))
    with db.db() as con:
        if pid is None:
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            cur = con.execute("INSERT INTO music_playlists (name, mode, schedule, public, ings, rules, template, meta, created_at, sort) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort), 0) + 1 FROM music_playlists))",
                              (*values, str(body.get("template") or "")[:20], json.dumps(meta), int(time.time())))
            pid = cur.lastrowid
        elif con.execute("UPDATE music_playlists SET name = ?, mode = ?, schedule = ?, public = ?, ings = ?, rules = ? WHERE id = ?", (*values, pid)).rowcount == 0:
            raise LookupError("no such playlist")
    store.changed("playlists")
    return get(pid)


def from_template(tpl: str, **over: Any) -> dict[str, Any]:
    t = TEMPLATES.get(tpl)
    if not t:
        raise PlaylistError("unknown template")
    body = {"name": t["name"], "mode": t.get("mode", "fixed"), "template": tpl,
            "schedule": {"every": t.get("every", "weekly"), "day": t.get("day", 0 if t.get("every", "weekly") == "weekly" else 1)},
            "ings": [{"kind": k, "share": s, "params": p} for k, s, p in t["ings"]], "rules": t["rules"]}
    body.update(over)
    return save_design(body)


def delete(pid: int) -> None:
    with db.db() as con:
        if con.execute("DELETE FROM music_playlists WHERE id = ?", (pid,)).rowcount == 0:
            raise LookupError("no such playlist")
    store.changed("playlists")


def reorder(ids: list[int]) -> None:
    with db.db() as con:
        for i, pid in enumerate(ids):
            con.execute("UPDATE music_playlists SET sort = ? WHERE id = ?", (i, pid))
    store.changed("playlists")


def _write_tracks(con, pid: int, rows: list[dict[str, Any]]) -> None:
    con.execute("DELETE FROM music_playlist_tracks WHERE playlist_id = ?", (pid,))
    con.executemany("INSERT INTO music_playlist_tracks (playlist_id, pos, key, title, artist, album, mbid, src, why, plays, uri, locked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(pid, i, t["key"], t["title"], t["artist"], t.get("album") or "", t.get("mbid"), t.get("src", ""), t.get("why", ""), int(t.get("plays") or 0),
                      t.get("uri") or "", int(bool(t.get("locked")))) for i, t in enumerate(rows)])


def edit_tracks(pid: int, op: str, pos: int, to: int | None = None) -> None:
    """lock / unlock / remove (a removed song is left out of this playlist from then on) / move."""
    rows = tracks(pid)
    if not 0 <= pos < len(rows):
        raise PlaylistError("no such track")
    with db.db() as con:
        if op in ("lock", "unlock"):
            rows[pos]["locked"] = op == "lock"
        elif op == "remove":
            gone = rows.pop(pos)
            con.execute("INSERT OR IGNORE INTO music_playlist_bans (playlist_id, key) VALUES (?, ?)", (pid, gone["key"]))
        elif op == "move" and to is not None and 0 <= to < len(rows):
            rows.insert(to, rows.pop(pos))
        else:
            raise PlaylistError("unknown change")
        _write_tracks(con, pid, rows)
    store.changed("playlists")


def set_match(pid: int, pos: int, uri: str | None, name: str = "", artist: str = "", album: str = "") -> None:
    """A song matched by hand (remembered for every playlist), or left out (uri None)."""
    rows = tracks(pid)
    if not 0 <= pos < len(rows):
        raise PlaylistError("no such track")
    if uri is None:
        edit_tracks(pid, "remove", pos)
        return
    if not re.fullmatch(r"spotify:track:[A-Za-z0-9]{10,40}", uri):
        raise PlaylistError("that isn’t a Spotify track")
    matcher.remember(rows[pos]["key"], "manual", {"uri": uri, "name": name, "artists": [{"name": artist}] if artist else [], "album": {"name": album}})
    rows[pos]["uri"] = uri
    with db.db() as con:
        _write_tracks(con, pid, rows)
    store.changed("playlists")


# ---- building ------------------------------------------------------------------------------------------------------

def _sample(pool: list[dict[str, Any]], rnd: random.Random) -> list[dict[str, Any]]:
    """The pool reordered at random, weighted towards its front (Efraimidis–Spirakis with 1/(rank+8) weights)."""
    return [t for _, t in sorted(((rnd.random() ** (rank + 8), t) for rank, t in enumerate(pool)), key=lambda x: -x[0])]


def _spread(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid the same artist twice in a row where possible."""
    out, rest = [], rows[:]
    while rest:
        i = next((j for j, t in enumerate(rest) if not out or library.anorm(t["artist"]) != library.anorm(out[-1]["artist"])), 0)
        out.append(rest.pop(i))
    return out


def _quotas(ings: list[dict[str, Any]], n: int) -> list[int]:
    total = sum(i["share"] for i in ings) or 1
    raw = [n * i["share"] / total for i in ings]
    q = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: -(raw[i] - q[i]))[: n - sum(q)]:
        q[i] += 1
    return q


async def build(pid: int, *, seed: int | None = None, unattended: bool = False) -> dict[str, Any]:
    """Pick a fresh track list. Unattended builds keep the previous list if any input fails."""
    rows, errors = await _build_rows(get(pid), tracks(pid), seed=seed)
    if unattended:
        _require_complete_build(pid, rows, errors)
    return _commit_build(pid, rows, errors)


async def _build_rows(pl: dict[str, Any], old: list[dict[str, Any]], *, seed: int | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Stage picks and matches without changing the playlist's tracks, design or timestamps."""
    pid = pl["id"]
    rules, rnd = pl["rules"], random.Random(seed)
    locked = [t for t in old if t["locked"]]
    with db.db() as con:
        bans = {r["key"] for r in con.execute("SELECT key FROM music_playlist_bans WHERE playlist_id = ?", (pid,))}
        recent: set[str] = set()
        if rules["norep"]:
            runs = [r["run"] for r in con.execute("SELECT DISTINCT run FROM music_playlist_history WHERE playlist_id = ? ORDER BY run DESC LIMIT ?", (pid, rules["norep"]))]
            if runs:
                recent = {r["key"] for r in con.execute(f"SELECT key FROM music_playlist_history WHERE playlist_id = ? AND run IN ({','.join('?' * len(runs))})", (pid, *runs))}
    skip_before = (date.today() - timedelta(days=rules["skip_days"])).isoformat() if rules["skip_days"] else None
    taken = {t["key"] for t in locked}
    per: dict[str, int] = {}
    for t in locked:
        per[library.anorm(t["artist"])] = per.get(library.anorm(t["artist"]), 0) + 1
    cap = rules["per"] or 10**6

    def ok(t: dict[str, Any]) -> bool:
        a = library.anorm(t["artist"])
        if t["key"] in taken or t["key"] in bans or t["key"] in recent or per.get(a, 0) >= cap:
            return False
        if skip_before and t.get("last") and t["last"] >= skip_before:
            return False
        return True

    def take(t: dict[str, Any], out: list) -> None:
        taken.add(t["key"])
        per[library.anorm(t["artist"])] = per.get(library.anorm(t["artist"]), 0) + 1
        out.append(t)

    errors, pools = [], []
    for ing in pl["ings"]:
        try:
            pool = await ingredients.pick(ing)
        except (ServiceError, library.LibraryMissing, LookupError) as e:
            errors.append(str(e))
            pool = []
        except Exception as e:                                       # one ingredient failing mustn't sink the rest
            log.warning("playlist %s: %s failed: %s", pl["name"], ing["kind"], e)
            errors.append(f"{ingredients.KINDS[ing['kind']].name}: {e}")
            pool = []
        if not ingredients.KINDS[ing["kind"]].ordered:
            fam, new = [t for t in pool if t.get("plays")], [t for t in pool if not t.get("plays")]
            if fam and new:                                           # a mixed pool follows the familiar ↔ new slider
                fam, new = _sample(fam, rnd), _sample(new, rnd)
                pool, fi, ni = [], 0, 0
                while fi < len(fam) or ni < len(new):
                    if ni >= len(new) or (fi < len(fam) and rnd.random() * 100 < rules["fam"]):
                        pool.append(fam[fi]); fi += 1
                    else:
                        pool.append(new[ni]); ni += 1
            else:
                pool = _sample(pool, rnd)
        pools.append(pool)

    want = max(0, rules["len"] - len(locked))
    chosen: list[dict[str, Any]] = []
    leftovers: list[dict[str, Any]] = []
    for pool, quota in zip(pools, _quotas(pl["ings"], want)):
        got = []
        for t in pool:
            if len(got) >= quota:
                leftovers.append(t)
            elif ok(t):
                take(t, got)
        chosen += got
    for t in leftovers:                                               # short somewhere: top up from what the others had spare
        if len(chosen) >= want:
            break
        if ok(t):
            take(t, chosen)

    order = rules["order"]
    if order == "setlist":
        pass
    elif order == "chrono":
        chosen.sort(key=lambda t: (t.get("last") or "9999", t["artist"]))
    elif order == "plays":
        chosen.sort(key=lambda t: -(t.get("plays") or 0))
    else:
        rnd.shuffle(chosen)
        if order == "spread":
            chosen = _spread(chosen)
    rows = locked + chosen if order == "setlist" else _merge_locked(old, chosen)
    errors += await _match_rows(rows)
    return rows, errors


def _require_complete_build(pid: int, rows: list[dict[str, Any]], errors: list[str]) -> None:
    """A failed input is not a valid empty pool; never publish its partial result unattended."""
    if errors:
        problem = "; ".join(errors)
    elif not rows:
        problem = "no tracks were found"
    elif not any(r.get("uri") for r in rows):
        problem = "no tracks could be matched on Spotify"
    else:
        return
    message = f"Kept the previous playlist: {problem}"
    with db.db() as con:
        con.execute("UPDATE music_playlists SET last_error = ? WHERE id = ?", (message[:300], pid))
    store.changed("playlists")
    raise PlaylistError(message)


def _commit_build(pid: int, rows: list[dict[str, Any]], errors: list[str]) -> dict[str, Any]:
    with db.db() as con:
        _write_tracks(con, pid, rows)
        con.execute("UPDATE music_playlists SET built_at = ? WHERE id = ?", (int(time.time()), pid))
    store.changed("playlists")
    return {"tracks": len(rows), "missing": sum(1 for r in rows if not r.get("uri")), "errors": errors}


def _merge_locked(old: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Locked tracks keep their place; the new picks fill the gaps around them."""
    out: list[dict[str, Any] | None] = [None] * (len(fresh) + sum(1 for t in old if t["locked"]))
    for i, t in enumerate(old):
        if t["locked"] and i < len(out):
            out[i] = t
    it = iter(fresh)
    rows = [slot if slot is not None else next(it, None) for slot in out]
    rows += [t for t in old if t["locked"] and t not in rows]
    return [r for r in rows if r is not None]


async def _match_rows(rows: list[dict[str, Any]]) -> list[str]:
    """Fill in Spotify links (remembered, or searched for) — only once Spotify is connected."""
    if not sp.service.ready():
        return []
    preset = [t for t in rows if t.get("uri")]                     # picks that came with their Spotify track (new releases, Discover's checked songs)
    known = matcher.remembered([t["key"] for t in preset])
    for t in preset:
        if t["key"] not in known:                                    # never overwrite what's remembered — a hand-picked match stays hand-picked
            matcher.remember(t["key"], "ok", {"uri": t["uri"], "name": t["title"], "artists": [{"name": t["artist"]}], "album": {"name": t.get("album", "")}})
    try:
        found = await matcher.match([t for t in rows if not t.get("uri")])
    except ServiceError as e:
        return [str(e)]
    for t in rows:
        if not t.get("uri"):
            t["uri"] = found.get(t["key"], "")
    return []


# ---- saving to Spotify -------------------------------------------------------------------------------------------------

def spotify_name(pl: dict[str, Any]) -> str:
    return f"{store.settings()['prefix']}{pl['name']}"[:100]


def description(pl: dict[str, Any]) -> str:
    bits = [ingredients.describe(i) for i in pl["ings"][:3]]
    s = pl["schedule"]
    when = ""
    if pl["mode"] == "rot":
        day = ["Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays", "Sundays"][s["day"]] if s["every"] == "weekly" else f"the {s['day']}{'st' if s['day'] in (1, 21) else 'nd' if s['day'] in (2, 22) else 'rd' if s['day'] in (3, 23) else 'th'}" if s["every"] == "monthly" else "day"
        when = f" Refreshes every {day} at {s['at']}." if s["every"] != "daily" else f" Refreshes daily at {s['at']}."
    return f"Made by Citadel from your listening: {'; '.join(bits)}.{when}"[:300]


async def save(pid: int) -> dict[str, Any]:
    """Write the current track list to Spotify. Returns {url, tracks, skipped}."""
    pl = get(pid)
    if not sp.service.ready():
        raise ServiceError("Connect Spotify in Settings → Music page first")
    rows = tracks(pid)
    if not rows:
        raise PlaylistError("there’s nothing in it yet — build it first")
    if any(not r["uri"] for r in rows):
        await _match_rows(rows)
        with db.db() as con:
            _write_tracks(con, pid, rows)
    uris = [r["uri"] for r in rows if r["uri"]]
    try:
        if pl["spotify_id"]:
            await sp.client.update_playlist(pl["spotify_id"], spotify_name(pl), pl["public"], description(pl))
            url = pl["spotify_url"]
        else:
            made = await sp.client.create_playlist(spotify_name(pl), pl["public"], description(pl))
            url = (made.get("external_urls") or {}).get("spotify", "")
            with db.db() as con:
                con.execute("UPDATE music_playlists SET spotify_id = ?, spotify_url = ? WHERE id = ?", (made["id"], url, pid))
            pl["spotify_id"] = made["id"]
        await sp.client.set_items(pl["spotify_id"], uris)
    except ServiceError as e:
        with db.db() as con:
            con.execute("UPDATE music_playlists SET last_error = ? WHERE id = ?", (str(e)[:300], pid))
        store.changed("playlists")
        raise
    now = int(time.time())
    with db.db() as con:
        run = pl["built_at"] or now
        con.executemany("INSERT OR IGNORE INTO music_playlist_history (playlist_id, run, key) VALUES (?, ?, ?)", [(pid, run, r["key"]) for r in rows])
        con.execute("DELETE FROM music_playlist_history WHERE playlist_id = ? AND run NOT IN (SELECT DISTINCT run FROM music_playlist_history WHERE playlist_id = ? ORDER BY run DESC LIMIT 12)", (pid, pid))
        con.execute("UPDATE music_playlists SET saved_at = ?, last_error = '' WHERE id = ?", (now, pid))
    store.changed("playlists")
    return {"url": url, "tracks": len(uris), "skipped": len(rows) - len(uris)}


# ---- rotation -------------------------------------------------------------------------------------------------------------

def next_run(pl: dict[str, Any], tz=config.TZ) -> int | None:
    """When a rotating playlist next rebuilds (epoch seconds), or None (fixed, never saved, or past its end date)."""
    if pl["mode"] != "rot" or not pl.get("spotify_id"):
        return None
    s = pl["schedule"]
    h, m = parse_hhmm(s.get("at")) or (6, 0)
    last = datetime.fromtimestamp(pl.get("saved_at") or pl.get("built_at") or time.time(), tz)
    d = last.date()
    for _ in range(400):
        slot = datetime.combine(d, dtime(h, m), tzinfo=tz)
        if slot > last and (s["every"] == "daily" or (s["every"] == "weekly" and d.weekday() == s["day"]) or (s["every"] == "monthly" and d.day == s["day"])):
            if s.get("until") and d.isoformat() > s["until"]:
                return None
            return int(slot.timestamp())
        d += timedelta(days=1)
    return None


def due(now: float | None = None) -> list[dict[str, Any]]:
    now = now or time.time()
    return [p for p in all_playlists() if (n := p["next_run"]) and n <= now]


async def rotate(pid: int) -> dict[str, Any]:
    result = await build(pid, unattended=True)
    saved = await save(pid)
    return {**result, **saved}


async def after_gig(pl: dict[str, Any]) -> bool:
    """A pre-gig playlist whose gig has happened: swap the prediction for what they actually played, once it's on
    Setlist.fm, and stop rotating. True once done (or given up on, a week later)."""
    gig = pl["meta"].get("gig") or {}
    if not gig.get("date") or date.today().isoformat() <= gig["date"]:
        return False
    real = await gigs.as_played(gig["artist"], gig["date"])
    if real:
        updated = {**pl, "name": f"{gig['artist']} — as played, {gig['date']}", "mode": "fixed",
                   "ings": ingredients.check([{"kind": "gig", "share": 100, "params": {"kind": "setlistfm", "id": real["id"]}}])}
        rows, errors = await _build_rows(updated, [{**t, "locked": False} for t in tracks(pl["id"])])
        _require_complete_build(pl["id"], rows, errors)
        save_design(updated, pl["id"])
        _commit_build(pl["id"], rows, errors)
        if pl.get("spotify_id"):
            await save(pl["id"])
        return True
    if (date.today() - date.fromisoformat(gig["date"])).days > 7:
        save_design({**pl, "mode": "fixed"}, pl["id"])               # nobody posted it: leave the prediction as it was
        return True
    return False
