"""Your own music, from Music Companion's database — opened read-only, every time (Citadel never writes to another
project's data).

Everything a playlist ingredient picks comes back as a *track*: {key, title, artist, album, mbid, plays, last, src, why}
— `key` is the normalised "artist|title" used everywhere to recognise the same song (plays, Spotify matches, history).
Heavier lookups (every song's play count, every record's tracklist) are kept in memory for ten minutes."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import unicodedata
from datetime import date, timedelta
from typing import Any

from .. import config
from ..content import is_junk_artist, norm

TTL = 600
_memo: dict[str, tuple[float, Any]] = {}

VERSION_RE = re.compile(r"\s*(?:[-–]\s*(?:\d{4}\s+)?(?:re-?master(?:ed)?|remix(?:ed)?|mono|stereo|single|radio)(?:\s+(?:version|edit|mix))?(?:\s+\d{4})?.*$"
                        r"|[\(\[](?:[^)\]]*?(?:re-?master|feat\.?|ft\.|explicit|bonus|deluxe|mono|stereo|single version|radio edit)[^)\]]*)[\)\]])", re.I)
SKIP_TITLE_RE = re.compile(r"^\s*(?:\[.*\]|intro|outro|untitled|hidden track|silence)\s*$", re.I)


# ---- "have you played this?" — the same rules as Music Companion's own collection page (site/js/collection.js), so the two
# apps agree on what's played. A title compares without case, accents, apostrophes, "&"/"and", anything in brackets, or an
# edition note after " - " ("- 2009 Remaster", "- Live at …"). Live/remix/demo/acoustic/instrumental versions stay distinct.
EDITION_TAIL = re.compile(r"\s+-\s+(?=.*\b(?:remaster(?:ed)?|live|version|mix|edit|demo|mono|stereo|single|bonus|edition|re-?record(?:ing)?|session|take|acoustic|instrumental|\d{4})\b).*$", re.I)


def norm_title(title: str) -> str:
    s = unicodedata.normalize("NFKD", str(title or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = re.sub(r"\s*[(\[].*?[)\]]", "", s)
    s = EDITION_TAIL.sub("", s)
    s = re.sub(r"['’`´]", "", s).replace("&", " and ")
    return re.sub(r"[\W_]+", " ", s).strip()


def variant_of(title: str) -> str:
    """A different recording rather than another edition — read only from the bracketed / " - " tail."""
    t = str(title or "")
    tails = re.findall(r"[(\[]([^)\]]*)[)\]]", t)
    if (dash := t.find(" - ")) > 0:
        tails.append(t[dash + 3:])
    tail = " ".join(tails).lower()
    if re.search(r"\b(?:live|unplugged)\b", tail):
        return "live"
    if re.search(r"\bremix\b|\bmix\b", tail) and not re.search(r"\boriginal mix\b", tail):
        return "remix"
    for v in ("demo", "acoustic", "instrumental"):
        if re.search(rf"\b{v}\b", tail):
            return v
    return ""


def track_key(title: str) -> str:
    v = variant_of(title)
    return norm_title(title) + (f"#{v}" if v else "")


def _similarity(a: str, b: str) -> float:
    """1 - edit distance / length, for "Seperate" vs "Separate" (Companion's titleSimilarity)."""
    if not a or not b:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return 1 - prev[-1] / max(len(a), len(b))


class LibraryMissing(RuntimeError):
    pass


def clean_title(title: str) -> str:
    """'Hereafter - 2018 Remaster' / 'Song (feat. X)' -> the song itself. Live/acoustic/demo versions stay distinct."""
    return VERSION_RE.sub("", title or "").strip() or (title or "").strip()


def anorm(artist: str) -> str:
    return norm(re.sub(r"^the\s+", "", (artist or "").replace("&", "and"), flags=re.I))


def key(artist: str, title: str) -> str:
    return f"{anorm(artist)}|{norm(clean_title(title))}"


def _ro() -> sqlite3.Connection:
    if not config.MUSIC_DB.exists():
        raise LibraryMissing(f"Music Companion's database isn't at {config.MUSIC_DB}")
    con = sqlite3.connect(f"file:{config.MUSIC_DB}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _q(sql: str, *args: Any) -> list[sqlite3.Row]:
    con = _ro()
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def _memoised(name: str, build):
    hit = _memo.get(name)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    value = build()
    _memo[name] = (time.time(), value)
    return value


def forget() -> None:
    _memo.clear()


def available() -> bool:
    return config.MUSIC_DB.exists()


def _cutoff(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


# ---- indexes --------------------------------------------------------------------------------------------------------

def plays_index() -> dict[str, dict[str, Any]]:
    """key -> {title, artist, album, mbid, plays, first, last, peak, peak_n} for every song you've played."""
    def build():
        years: dict[int, tuple[str, int]] = {}
        for r in _q("SELECT song_id, substr(played_at, 1, 4) AS y, COUNT(*) AS n FROM scrobbles WHERE song_id IS NOT NULL GROUP BY 1, 2"):
            if r["n"] > years.get(r["song_id"], ("", 0))[1]:
                years[r["song_id"]] = (r["y"], r["n"])
        out: dict[str, dict[str, Any]] = {}
        for r in _q("""SELECT so.id, so.title, a.name AS artist, al.title AS album, al.mbid, COUNT(s.id) AS plays,
                              MIN(s.played_at) AS first, MAX(s.played_at) AS last
                       FROM scrobbles s JOIN songs so ON so.id = s.song_id JOIN artists a ON a.id = so.artist_id
                       LEFT JOIN albums al ON al.id = so.album_id GROUP BY so.id"""):
            if is_junk_artist(r["artist"]):                          # "Various", "[unknown]" — never a real artist to play
                continue
            k = key(r["artist"], r["title"])
            prev = out.get(k)
            if prev and prev["plays"] >= r["plays"]:
                prev["plays"] += r["plays"]
                prev["first"] = min(prev["first"], r["first"][:10]); prev["last"] = max(prev["last"], r["last"][:10])
                continue
            peak = years.get(r["id"], ("", 0))
            out[k] = {"key": k, "title": r["title"], "artist": r["artist"], "album": r["album"] or "", "mbid": r["mbid"],
                      "plays": r["plays"] + (prev["plays"] if prev else 0), "first": r["first"][:10], "last": r["last"][:10],
                      "peak": peak[0], "peak_n": peak[1]}
        return out
    return _memoised("plays", build)


def artist_index() -> dict[str, dict[str, Any]]:
    """anorm(artist) -> {name, mbid, plays, recent (last 12 months), first, last, years {YYYY: n}}."""
    def build():
        out: dict[str, dict[str, Any]] = {}
        cut = _cutoff(365)
        for r in _q("""SELECT a.id, a.name, a.mbid, substr(s.played_at, 1, 4) AS y, COUNT(*) AS n, SUM(s.played_at >= ?) AS recent,
                              MIN(s.played_at) AS first, MAX(s.played_at) AS last
                       FROM scrobbles s JOIN artists a ON a.id = s.artist_id GROUP BY a.id, y""", cut):
            k = anorm(r["name"])
            a = out.setdefault(k, {"name": r["name"], "mbid": r["mbid"], "plays": 0, "recent": 0, "first": r["first"][:10], "last": r["last"][:10], "years": {}})
            a["plays"] += r["n"]; a["recent"] += r["recent"] or 0
            a["years"][r["y"]] = a["years"].get(r["y"], 0) + r["n"]
            a["first"] = min(a["first"], r["first"][:10]); a["last"] = max(a["last"], r["last"][:10])
        return out
    return _memoised("artists", build)


def song_groups() -> dict[str, Any]:
    """Every song you've played, one entry per track per artist (remaster/duplicate song rows folded together, as Music
    Companion does), with lookups by artist, by album (and the albums its plays came from), by MusicBrainz recording, by
    song row, and the tracklist lines matched by hand in Companion's maintenance (track_links)."""
    def build():
        groups: dict[tuple[str, str], dict[str, Any]] = {}
        by_song: dict[int, dict[str, Any]] = {}
        rows = _q("""SELECT so.id, so.title, so.mbid, so.album_id, a.name AS artist, COUNT(s.id) AS plays, MIN(s.played_at) AS first, MAX(s.played_at) AS last,
                            al.title AS album, al.mbid AS album_mbid
                     FROM songs so JOIN artists a ON a.id = so.artist_id JOIN scrobbles s ON s.song_id = so.id LEFT JOIN albums al ON al.id = so.album_id
                     GROUP BY so.id ORDER BY plays DESC""")
        for r in rows:
            if is_junk_artist(r["artist"]):
                continue
            tk = track_key(r["title"])
            g = groups.get((anorm(r["artist"]), tk))
            if not g:
                n = norm_title(r["title"])
                g = groups[(anorm(r["artist"]), tk)] = {"title": r["title"], "artist": r["artist"], "album": r["album"] or "", "mbid": r["album_mbid"],
                                                        "plays": 0, "first": r["first"][:10], "last": r["last"][:10], "n": n, "c": n.replace(" ", ""),
                                                        "v": variant_of(r["title"]), "albums": set(), "recordings": set()}
            g["plays"] += r["plays"]
            g["first"], g["last"] = min(g["first"], r["first"][:10]), max(g["last"], r["last"][:10])
            if r["album_id"]:
                g["albums"].add(r["album_id"])
            if r["mbid"]:
                g["recordings"].add(r["mbid"])
            by_song[r["id"]] = g
        for r in _q("SELECT DISTINCT song_id, album_id FROM scrobbles WHERE album_id IS NOT NULL AND song_id IS NOT NULL"):
            if (g := by_song.get(r["song_id"])):
                g["albums"].add(r["album_id"])
        by_artist: dict[str, list] = {}
        by_album: dict[int, list] = {}
        by_rec: dict[str, dict] = {}
        for (a, _), g in groups.items():
            by_artist.setdefault(a, []).append(g)
            for al in g["albums"]:
                by_album.setdefault(al, []).append(g)
            for rec in g["recordings"]:
                by_rec.setdefault(rec, g)
        parts: dict[int, list[int]] = {}
        try:
            for r in _q("SELECT album_id, part_album_id FROM album_parts ORDER BY position"):
                parts.setdefault(r["album_id"], []).append(r["part_album_id"])
        except sqlite3.OperationalError:
            pass
        links: dict[tuple[int, str], int] = {}
        try:
            for r in _q("SELECT album_id, track_title, song_id FROM track_links ORDER BY id"):
                links.setdefault((r["album_id"], r["track_title"].strip().lower()), r["song_id"])
        except sqlite3.OperationalError:
            pass
        return {"by_artist": by_artist, "by_album": by_album, "by_rec": by_rec, "by_song": by_song, "parts": parts, "links": links}
    return _memoised("groups", build)


def _pick(title: str, cands: list[dict[str, Any]], *, loose: bool, listing: list[str] | None = None) -> dict[str, Any] | None:
    k, v = norm_title(title), variant_of(title)
    c = k.replace(" ", "")
    cands = list({id(g): g for g in cands}.values())
    same = [g for g in cands if g["v"] == v]                            # a live track only matches live songs
    exact = [g for g in same if g["n"] == k]
    if exact:
        return max(exact, key=lambda g: g["plays"])
    only = lambda xs: xs[0] if len(xs) == 1 else None                    # noqa: E731 — one clear winner, or nothing
    if (hit := only([g for g in same if g["c"] == c])):                  # spacing-blind: "Good Morning" = "Goodmorning"
        return hit
    if not loose:
        return None
    if len(k) >= 4 and (hit := only([g for g in same if g["n"].startswith(f"{k} ") or k.startswith(f"{g['n']} ")])):
        return hit                                                        # "Total Hate '95" = "Total Hate"
    if len(c) >= 5:
        scored = sorted(((sim, g) for g in same if (sim := _similarity(c, g["c"])) >= 0.85), key=lambda x: -x[0])
        if len(scored) == 1 or (len(scored) > 1 and scored[0][0] - scored[1][0] >= 0.08):
            return scored[0][1]                                           # "Seperate" = "Separate"
    # a record listing "Paradox" when what you've played from it is "Paradox - Live at Edmonton": that's the track —
    # but only when the album has no plain version of it
    if not v and not any(g["n"] == k and not g["v"] for g in cands) and (hit := only([g for g in cands if g["v"] and g["n"] == k])):
        return hit
    # a pressing calling the track itself an instrumental ("Orion (Instrumental)") means the plain song — unless the album
    # has an instrumental version of it, or the pressing lists the plain one too
    if v == "instrumental" and not any(norm_title(t) == k and not variant_of(t) for t in (listing or [])):
        return only([g for g in cands if not g["v"] and g["n"] == k])
    return None


def played(artist: str, title: str, *, album_id: int | None = None, recording: str | None = None, song_id: int | None = None,
           listing: list[str] | None = None) -> dict[str, Any] | None:
    """The song of yours this is, if you've played it: by song row, MusicBrainz recording, a hand-made link, or by title
    among the album's own songs (and its box set's parts) — then, more strictly, among everything by the artist."""
    G = song_groups()
    if song_id and (g := G["by_song"].get(song_id)):
        return g
    if recording and (g := G["by_rec"].get(recording)):
        return g
    if album_id:
        scope = [album_id, *G["parts"].get(album_id, [])]
        for a in scope:
            if (sid := G["links"].get((a, (title or "").strip().lower()))) and (g := G["by_song"].get(sid)):
                return g
        if (g := _pick(title, [g for a in scope for g in G["by_album"].get(a, [])], loose=True, listing=listing)):
            return g
    return _pick(title, G["by_artist"].get(anorm(artist), []), loose=False)


def lookup(artist: str, title: str, **where: Any) -> dict[str, Any]:
    """played(), as {plays, last, album, mbid} — or {} for something you've never played."""
    g = played(artist, title, **where)
    return {"plays": g["plays"], "last": g["last"], "album": g["album"], "mbid": g["mbid"]} if g else {}


def records() -> list[dict[str, Any]]:
    """Every record you own with its pressing's tracklist: [{artist, album, year, mbid, tracks: [{pos, title}]}]."""
    def build():
        out = []
        for r in _q("""SELECT v.album_id, a.name AS artist, COALESCE(v.display_title, al.title) AS album, COALESCE(al.year, v.release_year, v.pressing_year) AS year,
                              al.mbid, d.tracklist
                       FROM vinyl_holdings v JOIN albums al ON al.id = v.album_id JOIN artists a ON a.id = al.artist_id
                       LEFT JOIN vinyl_details d ON d.holding_id = v.id"""):
            try:
                tl = json.loads(r["tracklist"] or "[]")
            except ValueError:
                tl = []
            tracks = [{"pos": t.get("position") or "", "title": t["title"], "secs": _secs(t.get("duration"))} for t in tl if t.get("type", "track") == "track" and t.get("title")]
            out.append({"album_id": r["album_id"], "artist": r["artist"], "album": r["album"], "year": r["year"], "mbid": r["mbid"], "tracks": tracks})
        return out
    return _memoised("records", build)


def owned_by_artist() -> dict[str, int]:
    def build():
        counts: dict[str, int] = {}
        for rec in records():
            counts[anorm(rec["artist"])] = counts.get(anorm(rec["artist"]), 0) + 1
        return counts
    return _memoised("owned", build)


def summary() -> dict[str, int]:
    r = _q("SELECT (SELECT COUNT(*) FROM scrobbles) AS scrobbles, (SELECT COUNT(*) FROM vinyl_holdings) AS records, (SELECT COUNT(*) FROM setlists) AS shows")[0]
    return dict(r)


def genres(limit: int = 80) -> list[str]:
    return [r["name"] for r in _q("""SELECT g.name FROM scrobbles s JOIN album_genres ag ON ag.album_id = s.album_id JOIN genres g ON g.id = ag.genre_id
                                     GROUP BY g.id ORDER BY COUNT(*) DESC LIMIT ?""", limit)]


def top_artists(days: int = 365, n: int = 25) -> list[dict[str, Any]]:
    idx = artist_index()
    field = "recent" if days else "plays"
    return sorted((a for a in idx.values() if a[field]), key=lambda a: -a[field])[:n]


# ---- what ingredients pick from -----------------------------------------------------------------------------------

def _track(p: dict[str, Any], src: str, why: str) -> dict[str, Any]:
    return {"key": p["key"], "title": p["title"], "artist": p["artist"], "album": p.get("album", ""), "mbid": p.get("mbid"),
            "plays": p.get("plays", 0), "last": p.get("last", ""), "src": src, "why": why}


def history(*, min_plays: int = 20, max_plays: int = 0, not_played_days: int = 365, played_within_days: int = 0) -> list[dict[str, Any]]:
    """Songs you've played, most played first: e.g. ≥20 plays and nothing in the last year = forgotten favourites."""
    quiet, recent = (_cutoff(not_played_days) if not_played_days else None), (_cutoff(played_within_days) if played_within_days else None)
    out = []
    for p in plays_index().values():
        if p["plays"] < min_plays or (max_plays and p["plays"] > max_plays):
            continue
        if quiet and p["last"] >= quiet:
            continue
        if recent and p["last"] < recent:
            continue
        why = f"{p['plays']} plays, peaked {p['peak']}" + (f" · not since {p['last'][:7]}" if quiet else f" · last {p['last']}")
        out.append(_track(p, "history", why))
    return sorted(out, key=lambda t: -t["plays"])


def capsule(start: str, end: str, label: str = "") -> list[dict[str, Any]]:
    """What you were playing between two dates (YYYY-MM-DD), most played first."""
    idx = plays_index()
    out = []
    for r in _q("""SELECT so.title, a.name AS artist, COUNT(*) AS n FROM scrobbles s JOIN songs so ON so.id = s.song_id JOIN artists a ON a.id = so.artist_id
                   WHERE s.played_at >= ? AND s.played_at < date(?, '+1 day') GROUP BY so.id ORDER BY n DESC LIMIT 400""", start, end):
        p = idx.get(key(r["artist"], r["title"])) or {"key": key(r["artist"], r["title"]), "title": r["title"], "artist": r["artist"], "plays": r["n"]}
        out.append(_track(p, "history", f"{r['n']} plays in {label or f'{start} – {end}'}"))
    return out


def by_artists(names: list[str], mode: str = "most") -> list[dict[str, Any]]:
    wanted = {anorm(n) for n in names}
    rows = [p for p in plays_index().values() if anorm(p["artist"]) in wanted]
    rows.sort(key=lambda p: p["plays"] if mode == "least" else -p["plays"])
    return [_track(p, "history", f"{p['plays']} plays · {p['artist']}") for p in rows]


def by_genre(genre: str) -> list[dict[str, Any]]:
    idx = plays_index()
    out = []
    for r in _q("""SELECT so.title, a.name AS artist, COUNT(*) AS n FROM scrobbles s JOIN songs so ON so.id = s.song_id JOIN artists a ON a.id = so.artist_id
                   JOIN album_genres ag ON ag.album_id = s.album_id JOIN genres g ON g.id = ag.genre_id
                   WHERE g.name = ? COLLATE NOCASE GROUP BY so.id ORDER BY n DESC LIMIT 400""", genre):
        p = idx.get(key(r["artist"], r["title"]))
        if p:
            out.append(_track(p, "history", f"{genre} · {p['plays']} plays"))
    return out


def shelf(mode: str = "mixed") -> list[dict[str, Any]]:
    """Tracks from the records you own. 'most': your most played from each record; 'never': ones you've never streamed;
    'mixed': both. Each says where it is on the record ("side B, track 3")."""
    on, off = [], []
    for rec in records():
        if anorm(rec["artist"]) in ("various", "variousartists"):
            continue
        for t in rec["tracks"]:
            if SKIP_TITLE_RE.match(t["title"]):
                continue
            k = key(rec["artist"], t["title"])
            p = played(rec["artist"], t["title"], album_id=rec["album_id"], listing=[x["title"] for x in rec["tracks"]])
            pos = t["pos"]
            where = f"side {pos[0]}, track {pos[1:]}" if re.fullmatch(r"[A-Z]\d+", pos or "") else (f"track {pos}" if pos else "")
            base = {"key": k, "title": p["title"] if p else t["title"], "artist": rec["artist"], "album": rec["album"], "mbid": rec["mbid"],
                    "plays": p["plays"] if p else 0, "last": p["last"] if p else ""}
            why = f"your {rec['album']} LP" + (f" · {where}" if where else "") + (f" · {base['plays']} plays" if base["plays"] else " · never streamed")
            (on if base["plays"] else off).append(_track(base, "shelf", why))
    on.sort(key=lambda t: -t["plays"])
    if mode == "most":
        return on
    if mode == "never":
        return off
    out = []
    for i in range(max(len(on), len(off))):                      # interleave: two favourites, one never-streamed
        out += on[2 * i:2 * i + 2] + off[i:i + 1]
    return out


def _secs(text: Any) -> int | None:
    """"4:32" -> 272 (None if it isn't a time)."""
    try:
        parts = [int(p) for p in str(text or "").strip().split(":")]
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total or None


TOO_SHORT_MS = 30_000        # Last.fm never counts a play of a track this short: it would stay "never played" forever


def _short_lines() -> set[tuple[int, str]]:
    """(album id, lower-case title) of tracklist lines known to be 30 s or less (Music Companion's track_lengths)."""
    if not _q("SELECT 1 FROM sqlite_master WHERE name = 'track_lengths'"):
        return set()
    return {(r["album_id"], r["title"].lower()) for r in _q("SELECT album_id, title FROM track_lengths WHERE length_ms > 0 AND length_ms <= ?", TOO_SHORT_MS)}


def deep_cuts(*, top_n: int = 10, days: int = 365) -> list[dict[str, Any]]:
    """Album tracks you've never played, by the artists you play most (MusicBrainz tracklists, plus your pressings')."""
    tops = top_artists(days, top_n)
    if not tops:
        return []
    wanted = {anorm(a["name"]): a["name"] for a in tops}
    out, seen = [], set()
    has_len = any(c["name"] == "length_ms" for c in _q("PRAGMA table_info(album_tracklists)"))
    rows = _q(f"""SELECT t.album_id, a.name AS artist, al.title AS album, al.year, al.mbid, t.title, t.recording_mbid, {"t.length_ms" if has_len else "NULL AS length_ms"} FROM album_tracklists t
                  JOIN albums al ON al.id = t.album_id JOIN artists a ON a.id = al.artist_id
                  WHERE a.name IN ({','.join('?' * len(wanted))}) ORDER BY al.year, t.position""", *wanted.values())
    pressings = [{"album_id": r["album_id"], "artist": r["artist"], "album": r["album"], "year": r["year"], "mbid": r["mbid"], "title": t["title"], "recording_mbid": None,
                  "listing": [x["title"] for x in r["tracks"]], "length_ms": (t.get("secs") or 0) * 1000 or None}
                 for r in records() if anorm(r["artist"]) in wanted for t in r["tracks"]]
    short = _short_lines()
    for r in [dict(x) for x in rows] + pressings:
        k = key(r["artist"], r["title"])
        if k in seen or SKIP_TITLE_RE.match(r["title"]) or re.search(r"\b(?:live|demo|instrumental)\b", r["title"], re.I):
            continue
        if (r.get("length_ms") and r["length_ms"] <= TOO_SHORT_MS) or (r["album_id"], r["title"].lower()) in short:
            continue                                               # too short to ever be scrobbled ("Embryo", 0:23): not a deep cut
        if played(r["artist"], r["title"], album_id=r["album_id"], recording=r["recording_mbid"], listing=r.get("listing")):
            seen.add(k)
            continue
        seen.add(k)
        out.append({"key": k, "title": clean_title(r["title"]), "artist": r["artist"], "album": r["album"], "mbid": r["mbid"], "plays": 0, "last": "",
                    "src": "deep", "why": f"never played · from {r['album']}" + (f" ({r['year']})" if r.get("year") else "")})
    return out


# ---- gigs you've been to ------------------------------------------------------------------------------------------

def shows(limit: int = 30) -> list[dict[str, Any]]:
    return [dict(r) for r in _q("""SELECT s.id, s.raw_artist_text AS artist, s.event_date AS date, COALESCE(v.name, '') AS venue, COALESCE(v.city, '') AS city,
                                          COALESCE(s.tour_name, '') AS tour, (SELECT COUNT(*) FROM setlist_songs x WHERE x.setlist_id = s.id) AS songs
                                   FROM setlists s LEFT JOIN venues v ON v.id = s.venue_id ORDER BY s.event_date DESC LIMIT ?""", limit)]


def show_tracks(setlist_id: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    head = _q("""SELECT s.raw_artist_text AS artist, s.event_date AS date, COALESCE(v.name, '') AS venue FROM setlists s
                 LEFT JOIN venues v ON v.id = s.venue_id WHERE s.id = ?""", setlist_id)
    if not head:
        raise LookupError("no such show")
    h = dict(head[0])
    out = []
    for r in _q("SELECT song_id, raw_song_text, is_cover, cover_of_artist_text FROM setlist_songs WHERE setlist_id = ? ORDER BY position", setlist_id):
        artist = r["cover_of_artist_text"] if r["is_cover"] and r["cover_of_artist_text"] else h["artist"]
        k = key(artist, r["raw_song_text"])
        p = lookup(artist, r["raw_song_text"], song_id=r["song_id"])
        out.append({"key": k, "title": r["raw_song_text"], "artist": artist, "album": p.get("album", ""), "mbid": p.get("mbid"), "plays": p.get("plays", 0),
                    "last": p.get("last", ""), "src": "gig", "why": f"played at {h['venue'] or 'the show'}, {h['date']}" + (f" · cover of {artist}" if artist != h["artist"] else "")})
    return h, out


# ---- Discover's "rediscover" and the player's fact line -----------------------------------------------------------------

def rediscover(n: int = 15) -> list[dict[str, Any]]:
    """Artists you played far more than you do now: a big year, and little in the last twelve months."""
    this_year = date.today().year
    owned = owned_by_artist()
    out = []
    for k, a in artist_index().items():
        if a["plays"] < 200 or a["recent"] > a["plays"] * 0.15:
            continue
        peak_y, peak_n = max(a["years"].items(), key=lambda kv: kv[1])
        if int(peak_y) >= this_year - 1 or peak_n < 100:
            continue
        first_y = min(int(y) for y in a["years"])
        out.append({"artist": a["name"], "total": a["plays"], "recent": a["recent"], "peak": int(peak_y), "peak_n": peak_n, "owned": owned.get(k, 0),
                    "years": [{"y": y, "n": a["years"].get(str(y), 0)} for y in range(first_y, this_year + 1)],
                    "score": peak_n / max(1, a["recent"])})
    return sorted(out, key=lambda r: -r["score"])[:n]


def fact(artist: str, title: str) -> str:
    """A line about a song from your own history, for the player bar: "your 214th play · first 12/03/2019 · on your shelf"."""
    if not available():
        return ""
    p = played(artist, title)
    bits = []
    if p:
        n = p["plays"] + 1
        bits.append(f"your {n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')} play")
        y, m, d = p["first"].split("-")
        bits.append(f"first played {d}/{m}/{y}")
    else:
        a = artist_index().get(anorm(artist))
        bits.append("first time hearing this" if not a else f"new song from an artist you’ve played {a['plays']:,} times")
    for rec in records():
        if p and anorm(rec["artist"]) == anorm(artist) and any(played(rec["artist"], t["title"], album_id=rec["album_id"]) is p for t in rec["tracks"]):
            bits.append(f"on your {rec['album']} LP")
            break
    return " · ".join(bits)
