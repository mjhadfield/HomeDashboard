"""Owner API for the News page (#news): the front page, each section's latest stories, the archive, and the things you
tell it — read, 👍/👎, save, "never show", "I've watched the race".

Every story leaves here through `present()`, which applies the F1 spoiler shield: a held-back story is sent as an id
and its outlet only — no headline, summary, link or picture ever reaches the browser.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Literal

import feedparser
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from . import __version__, auth, config, db, newsmusic, newsrank, notify, spoilers
from .widgets import news

router = APIRouter(prefix="/api/owner/news", dependencies=[Depends(auth.require_owner)])

MOST_READ = {"id": "mostread", "name": "Most read"}


# ---- context: settings, learning, the shield ----------------------------------------------------------------------------

class Ctx:
    """Everything needed to present stories for one request."""

    def __init__(self, now: float | None = None) -> None:
        self.now = time.time() if now is None else now
        self.settings = news.load()
        self.sections = self.settings["sections"]
        self.feeds = {f["id"]: {"name": f["name"], "section": s["id"], "order": i} for s in self.sections for i, f in enumerate(s["feeds"])}
        self.feeds[news.MOST_READ_SOURCE] = {"name": "BBC News", "section": MOST_READ["id"], "order": 0}
        self.weights = load_weights(self.now)
        self.rules = load_rules()
        cal = db.cache_get("f1cal")
        self.lock = spoilers.lock_state(cal["data"], cal["fetched_at"], self.settings.get("f1_watched"), self.now, enabled=self.settings.get("f1_shield", True))
        self.matcher = spoilers.Matcher(cal["data"], self.settings.get("f1_extra_terms"), self.lock.get("venues"))
        self.calendar = cal

    def name(self, it: dict[str, Any]) -> str:
        return self.feeds.get(it["source"], {}).get("name") or it.get("subtitle") or it["source"]

    def section_of(self, it: dict[str, Any]) -> str | None:
        return self.feeds.get(it["source"], {}).get("section")

    def section_rules(self, sec: dict[str, Any]) -> list[dict[str, Any]]:
        return self.rules + [{"term": t, "kind": "topic", "section": sec["id"]} for t in sec.get("exclude") or []]

    def muted(self, it: dict[str, Any], rules: list[dict[str, Any]] | None = None) -> str | None:
        return newsrank.muted_by(it, self.rules if rules is None else rules, self.section_of(it), self.name(it))

    def shielded(self, it: dict[str, Any]) -> bool:
        return spoilers.hidden(it, self.lock, self.matcher)

    def public_lock(self) -> dict[str, Any]:
        return {k: v for k, v in self.lock.items() if k != "venues"}


def load_weights(now: float) -> dict[str, float]:
    with db.db() as con:
        return {r["key"]: newsrank.decayed(r["weight"], r["updated_at"], now) for r in con.execute("SELECT key, weight, updated_at FROM news_topic_weights")}


def load_rules() -> list[dict[str, Any]]:
    with db.db() as con:
        return [dict(r) for r in con.execute("SELECT id, term, kind, section, created_at FROM news_rules ORDER BY created_at DESC")]


# ---- rows ----------------------------------------------------------------------------------------------------------------

def _row(r: Any) -> dict[str, Any]:
    return {"id": r["id"], "source": r["source"], "title": r["title"], "subtitle": r["subtitle"], "url": r["url"], "date": r["date"],
            "status": r["status"], "first_seen": r["first_seen"], "meta": json.loads(r["meta"] or "{}")}


WHEN = "COALESCE(json_extract(meta, '$.published'), first_seen)"


def rows(where: str = "", args: list[Any] | tuple = (), *, order: str = f"{WHEN} DESC, id DESC", limit: int = 2000) -> list[dict[str, Any]]:
    sql = f"SELECT *, {WHEN} AS t FROM feed_items WHERE kind = 'news' AND status != 'dismissed'{(' AND ' + where) if where else ''} ORDER BY {order} LIMIT ?"
    with db.db() as con:
        return [{**_row(r), "t": r["t"]} for r in con.execute(sql, [*args, limit])]


def _in(ids: list[str]) -> str:
    return f"source IN ({','.join('?' * len(ids))})" if ids else "0"


def get_item(item_id: int) -> dict[str, Any]:
    found = rows("id = ?", [item_id], limit=1)
    if not found:
        raise HTTPException(404, "no such story")
    return found[0]


def save_meta(item_id: int, meta: dict[str, Any]) -> None:
    with db.db() as con:
        con.execute("UPDATE feed_items SET meta = ? WHERE id = ?", (json.dumps(meta), item_id))


# ---- presenting ------------------------------------------------------------------------------------------------------------

def chips(it: dict[str, Any]) -> list[str]:
    """The "Never show:" choices offered on 👎 — the story's names and categories first, plain words only if needed."""
    topics = (it.get("meta") or {}).get("topics") or []
    strong = [label for kind, label in topics if kind in ("p", "c")]
    weak = [label for kind, label in topics if kind == "w"]
    return (strong + weak)[:4] if len(strong) < 2 else strong[:4]


def present(it: dict[str, Any], ctx: Ctx, *, most_read: int | None = None, artists: list[str] | None = None, links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    meta = it.get("meta") or {}
    base = {"id": it["id"], "source": ctx.name(it), "section": ctx.section_of(it), "t": it.get("t") or newsrank.when(it)}
    if most_read:
        base["most_read"] = most_read
    if ctx.shielded(it):
        return {**base, "shielded": True}
    also = [{"name": ctx.name(m), "url": m["url"]} for m in it.get("also") or [] if not ctx.shielded(m)]
    return {**base, "title": it["title"], "url": it["url"], "summary": meta.get("summary") or "", "image": meta.get("image") or "",
            "published": meta.get("published"), "read": bool(meta.get("read")), "saved": it.get("status") == "pinned",
            "vote": meta.get("vote") or 0, "topics": chips(it), "artists": meta.get("artists") or [] if artists is None else artists, "links": links or [], "also": also,
            "peak_rank": meta.get("peak_rank"), "revealed": bool(meta.get("revealed"))}


def most_read_ranks() -> dict[str, int]:
    data = db.cache_get("mostread")["data"] or {}
    return {newsrank.url_key(i["url"]): i["rank"] for i in data.get("items", [])}


def _include(it: dict[str, Any], terms: list[str]) -> bool:
    if not terms:
        return True
    text = f"{it['title']} {(it.get('meta') or {}).get('summary') or ''}".casefold()
    return any(t.casefold() in text for t in terms)


def section_view(sec: dict[str, Any], ctx: Ctx, ranks: dict[str, int] | None = None, *, everything: bool = False) -> dict[str, Any]:
    """A section's Latest: the last `latest_hours` of stories, merged and ranked, capped; what learning held back, apart.
    A "your artists" section (Music) ranks by how much the artists in a story mean to you, and — unless `everything` —
    shows only stories about them."""
    ranks = most_read_ranks() if ranks is None else ranks
    ids = [f["id"] for f in sec["feeds"] if f.get("enabled")]
    since = ctx.now - ctx.settings["latest_hours"] * 3600
    found = [i for i in rows(f"{_in(ids)} AND {WHEN} >= ?", [*ids, since]) if _include(i, sec.get("include") or [])]
    rules = ctx.section_rules(sec)
    kept, muted = [], 0
    for it in found:
        if ctx.muted(it, rules):
            muted += 1
        else:
            kept.append(it)
    order = {fid: n for n, fid in enumerate(ids)}
    personal = bool(sec.get("personal"))
    shown, held, others = [], [], 0
    for st in newsrank.merge(kept, order):
        rank = min([r for r in (ranks.get(newsrank.url_key(m.get("url"))) for m in [st, *st["members"]]) if r], default=None)
        pref, drivers = newsrank.preference(st, ctx.weights, ctx.name(st))
        st["_mine"] = newsmusic.match(st["title"]) if personal else []
        lean = newsrank.leaning(st, sec.get("favour") or [], sec.get("play_down") or [], news.LEANING)
        st["_rank"], st["_score"] = rank, newsrank.score(st, ctx.now, most_read=rank, pref=pref, lean=lean,
                                                         mine=max((a["affinity"] for a in st["_mine"]), default=0.0))
        if personal and not everything and not st["_mine"]:
            others += 1
            continue
        if (st.get("meta") or {}).get("vote") == -1:
            held.append((st, "You said less like this"))
        elif pref <= newsrank.HIDE_BELOW:
            held.append((st, f"Less like: {', '.join(drivers)}" if drivers else "Learned from your 👎"))
        else:
            shown.append(st)
    shown.sort(key=lambda s: -s["_score"])
    cap = ctx.settings["latest_cap"]
    held.sort(key=lambda h: -h[0]["_score"])
    links = newsmusic.card_links({a["key"] for s in [*shown[:cap], *(h[0] for h in held)] for a in s["_mine"]}) if personal else {}

    def show(s: dict[str, Any], **extra: Any) -> dict[str, Any]:
        extra = {"score": round(s["_score"], 3), **extra}               # lets the front page rank stories from tiles it combines
        if not personal:
            return {**present(s, ctx, most_read=s["_rank"]), **extra}
        tied = [{**x, "artist": a["name"]} for a in s["_mine"] for x in links.get(a["key"], [])]
        return {**present(s, ctx, most_read=s["_rank"], artists=[a["name"] for a in s["_mine"]], links=tied), **extra}

    stories = [show(s) for s in shown[:cap]]
    return {"id": sec["id"], "name": sec["name"], "total": len(shown), "muted": muted, "personal": personal, "everything": everything, "others": others,
            "stories": stories, "hidden": [show(s, reason=reason) for s, reason in held],
            "artists": sorted({a["name"] for s in shown for a in s["_mine"]}) if personal else
                       sorted({a for s in shown for a in (s.get("meta") or {}).get("artists") or []})}


def most_read_view(ctx: Ctx) -> dict[str, Any]:
    cache = db.cache_get("mostread")
    data = cache["data"] or {}
    entries = data.get("items", [])
    keys = [newsrank.url_key(e["url"]) for e in entries]
    stored = {newsrank.url_key(i["url"]): i for i in rows("source = ?", [news.MOST_READ_SOURCE], limit=400)}
    # The same article in a section feed knows more: its summary, picture, real publish time — and whether you've read it.
    others = {}
    for i in rows(f"source != ? AND first_seen >= ?", [news.MOST_READ_SOURCE, ctx.now - 4 * 86400], limit=3000):
        others.setdefault(newsrank.url_key(i["url"]), i)
    out = []
    for e, k in zip(entries, keys):
        own = stored.get(k)
        if not own:
            continue
        twin = others.get(k)
        merged = {**own, "meta": {**own["meta"]}}
        if twin:
            merged["meta"].update({x: twin["meta"].get(x) for x in ("summary", "image", "published") if twin["meta"].get(x)})
            merged["meta"]["read"] = own["meta"].get("read") or twin["meta"].get("read")
        mute = ctx.muted(merged)
        if mute:
            out.append({"id": own["id"], "rank": e["rank"], "muted": mute})
            continue
        p = present(merged, ctx)
        p.update({"rank": e["rank"], "prev_rank": e.get("prev_rank"), "fresh": data.get("had_previous") and e.get("prev_rank") is None})
        out.append(p)
    return {"items": out, "at": cache["fetched_at"], "error": cache["error"], "enabled": ctx.settings.get("most_read", True)}


def _synced() -> dict[str, Any]:
    c = db.cache_get("news")
    return {"at": c["fetched_at"], "error": c["error"], "feed_errors": (c["data"] or {}).get("errors", [])}


# ---- reading ---------------------------------------------------------------------------------------------------------------

@router.get("")
async def overview():
    ctx = Ctx()
    return {"sections": [{"id": s["id"], "name": s["name"], "front": s["front"]} for s in ctx.sections if s["enabled"]],
            "shield": ctx.public_lock(), "synced": _synced(), "most_read": ctx.settings.get("most_read", True)}


@router.get("/front")
async def front():
    """The Most read list and one tile per front-page section — or per group of sections that share a tile ("World & UK"):
    a lead story (with a picture if one of the top few has one), then the next few, ranked across the group."""
    ctx = Ctx()
    ranks = most_read_ranks()
    n = ctx.settings["front_per_section"]
    on_front = [s for s in ctx.sections if s["enabled"] and s["front"]]
    ids = {s["id"] for s in on_front}
    groups: dict[str, list[dict[str, Any]]] = {}
    for sec in on_front:                                        # a section sharing with one that isn't on the front gets its own tile
        groups.setdefault(sec["front_with"] if sec.get("front_with") in ids else sec["id"], []).append(sec)
    blocks = []
    for root, members in groups.items():
        members.sort(key=lambda m: m["id"] != root)
        views = [section_view(sec, ctx, ranks) for sec in members]
        pool, seen = [], set()
        for v in views:
            for st in v["stories"]:
                k = st.get("url") and newsrank.url_key(st["url"]) or f"id:{st['id']}"
                if k not in seen:
                    seen.add(k)
                    pool.append({**st, "_from": v["id"]})
        pool.sort(key=lambda st: (bool(st.get("shielded")), -st.get("score", 0)))
        visible = [st for st in pool if not st.get("shielded")]
        lead = next((st for st in visible[:3] if st.get("image")), visible[0] if visible else None)
        rest = [st for st in pool if st is not lead][: n - (1 if lead else 0)]
        for v in views[1:] if len(views) > 1 else []:           # every section in a shared tile gets at least one story
            if not any(st["_from"] == v["id"] for st in [lead, *rest] if st):
                best = next((st for st in pool if st["_from"] == v["id"] and st is not lead and not st.get("shielded")), None)
                if best and rest:
                    rest[-1] = best
        rest.sort(key=lambda st: (bool(st.get("shielded")), -st.get("score", 0)))
        if len(views) > 1:                                       # a section's only story in a shared tile must survive the screen-fitting
            shown = [st for st in [lead, *rest] if st]
            for st in shown:
                st["sole"] = sum(1 for x in shown if x["_from"] == st["_from"]) == 1
        blocks.append({"id": root, "name": " & ".join(m["name"] for m in members), "lead": lead, "items": rest,
                       "total": sum(v["total"] for v in views), "personal": any(v["personal"] for v in views),
                       "tabs": [{"id": v["id"], "name": v["name"], "total": v["total"]} for v in views]})
    return {"most_read": most_read_view(ctx) if ctx.settings.get("most_read", True) else None, "sections": blocks,
            "shield": ctx.public_lock(), "synced": _synced()}


@router.get("/section/{section_id}")
async def section(section_id: str, everything: bool = False):
    ctx = Ctx()
    sec = next((s for s in ctx.sections if s["id"] == section_id and s["enabled"]), None)
    if not sec:
        raise HTTPException(404, "no such section")
    return {**section_view(sec, ctx, everything=everything), "shield": ctx.public_lock(), "synced": _synced(),
            "sources": [{"id": f["id"], "name": f["name"]} for f in sec["feeds"] if f.get("enabled")]}


@router.get("/archive")
async def archive(section: str = "", source: str = "", q: str = Query("", max_length=100), saved: bool = False, liked: bool = False,
                  unread: bool = False, before: float | None = None, before_id: int | None = None, limit: int = Query(100, ge=10, le=200)):
    """Everything still kept, newest first, in pages. A search never matches a held-back F1 story (that alone would hint
    at it), and "never show" stories stay out entirely."""
    ctx = Ctx()
    where, args = [], []
    if section == MOST_READ["id"]:
        where.append("source = ?"); args.append(news.MOST_READ_SOURCE)
    elif section:
        sec = next((s for s in ctx.sections if s["id"] == section), None)
        ids = [f["id"] for f in sec["feeds"]] if sec else []
        where.append(_in(ids)); args += ids
    if source:
        where.append("source = ?"); args.append(source)
    if saved:
        where.append("status = 'pinned'")
    if liked:
        where.append("json_extract(meta, '$.vote') = 1")
    if unread:
        where.append("json_extract(meta, '$.read') IS NULL")
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(title LIKE ? OR json_extract(meta, '$.summary') LIKE ?)"); args += [like, like]
    # Most-read entries that are also in a section feed show once, as the section's story, wearing their best rank.
    peaks = {newsrank.url_key(i["url"]): i["meta"].get("peak_rank") for i in rows("source = ?", [news.MOST_READ_SOURCE], limit=2000)}
    section_keys: set[str] | None = None
    out: list[dict[str, Any]] = []
    cur, cur_id = before, before_id
    batch_size, done = limit * 2, False
    while len(out) < limit and not done:
        w, a = list(where), list(args)
        if cur is not None:
            w.append(f"({WHEN} < ? OR ({WHEN} = ? AND id < ?))")
            a += [cur, cur, cur_id if cur_id is not None else 2**62]
        batch = rows(" AND ".join(w), a, limit=batch_size)
        done = len(batch) < batch_size
        if not batch:
            cur = None
        for n, it in enumerate(batch):
            cur, cur_id = it["t"], it["id"]
            if n == len(batch) - 1 and done:
                cur = None                                     # nothing older left
            if ctx.muted(it):
                continue
            if it["source"] == news.MOST_READ_SOURCE and section != MOST_READ["id"]:
                if section_keys is None:
                    section_keys = {newsrank.url_key(i["url"]) for i in rows("source != ?", [news.MOST_READ_SOURCE], limit=20000)}
                if newsrank.url_key(it["url"]) in section_keys:
                    continue
            if ctx.shielded(it):
                if not q.strip():
                    out.append(present(it, ctx))
            else:
                pr = present(it, ctx)
                pr["peak_rank"] = pr.get("peak_rank") or peaks.get(newsrank.url_key(it["url"]))
                out.append(pr)
            if len(out) == limit:
                break
    return {"items": out, "next": {"before": cur, "before_id": cur_id} if cur is not None else None, "shield": ctx.public_lock(),
            "sections": [{"id": s["id"], "name": s["name"], "sources": [{"id": f["id"], "name": f["name"]} for f in s["feeds"]]} for s in ctx.sections]
                        + [{**MOST_READ, "sources": []}]}


# ---- story actions ------------------------------------------------------------------------------------------------------------

@router.post("/items/{item_id}/read")
async def read(item_id: int):
    it = get_item(item_id)
    if not it["meta"].get("read"):
        save_meta(item_id, {**it["meta"], "read": True})
    return {"ok": True}


class VoteIn(BaseModel):
    vote: Literal[-1, 0, 1]


def apply_vote(it: dict[str, Any], new: int, ctx: Ctx) -> None:
    """Move the story's topics (and, a little, its outlet) by the change in vote. Voting again with the same value is a
    no-op; 0 undoes; switching 👍→👎 moves twice as far."""
    old = it["meta"].get("vote") or 0
    if old == new:
        return
    delta = new - old
    with db.db() as con:
        for key, label, w in newsrank.topic_keys(it, ctx.name(it)):
            row = con.execute("SELECT weight, votes, updated_at FROM news_topic_weights WHERE key = ?", (key,)).fetchone()
            cur = newsrank.decayed(row["weight"], row["updated_at"], ctx.now) if row else 0.0
            weight = cur + delta * w
            votes = (row["votes"] if row else 0) + (1 if new else 0) - (1 if old else 0)
            if abs(weight) < 1e-6 and votes <= 0:
                con.execute("DELETE FROM news_topic_weights WHERE key = ?", (key,))
            else:
                con.execute("INSERT INTO news_topic_weights (key, label, weight, votes, updated_at) VALUES (?, ?, ?, ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET label = excluded.label, weight = excluded.weight, votes = excluded.votes, updated_at = excluded.updated_at",
                            (key, label, weight, max(votes, 0), int(ctx.now)))
    meta = {**it["meta"]}
    if new:
        meta["vote"] = new
    else:
        meta.pop("vote", None)
    save_meta(it["id"], meta)


@router.put("/items/{item_id}/vote")
async def vote(item_id: int, body: VoteIn):
    ctx = Ctx()
    it = get_item(item_id)
    apply_vote(it, body.vote, ctx)
    return {"ok": True, "vote": body.vote, "topics": chips(it), "source": ctx.name(it)}


@router.post("/items/{item_id}/save")
async def save(item_id: int):
    get_item(item_id)
    with db.db() as con:
        con.execute("UPDATE feed_items SET status = 'pinned' WHERE id = ?", (item_id,))
    return {"ok": True}


@router.delete("/items/{item_id}/save")
async def unsave(item_id: int):
    get_item(item_id)
    with db.db() as con:
        con.execute("UPDATE feed_items SET status = 'seen' WHERE id = ?", (item_id,))
    return {"ok": True}


@router.post("/items/{item_id}/reveal")
async def reveal(item_id: int):
    """You chose to see one held-back story: it stays visible from now on."""
    it = get_item(item_id)
    save_meta(item_id, {**it["meta"], "revealed": True})
    it["meta"]["revealed"] = True
    return present(it, Ctx())


# ---- learning and rules ------------------------------------------------------------------------------------------------------

@router.get("/learning")
async def learning():
    now = time.time()
    with db.db() as con:
        topics = [{"key": r["key"], "kind": r["key"].split(":", 1)[0], "label": r["label"], "votes": r["votes"],
                   "weight": round(newsrank.decayed(r["weight"], r["updated_at"], now), 3)} for r in con.execute("SELECT * FROM news_topic_weights")]
    topics = [t for t in topics if abs(t["weight"]) >= 0.05]
    topics.sort(key=lambda t: -abs(t["weight"]))
    return {"topics": topics, "rules": load_rules()}


@router.delete("/learning/topics")
async def forget_topic(key: str = Query(..., max_length=200)):
    with db.db() as con:
        con.execute("DELETE FROM news_topic_weights WHERE key = ?", (key,))
    notify.widget("news")
    return {"ok": True}


@router.post("/learning/reset")
async def reset_learning():
    with db.db() as con:
        con.execute("DELETE FROM news_topic_weights")
        n = con.execute("UPDATE feed_items SET meta = json_remove(meta, '$.vote') WHERE kind = 'news' AND json_extract(meta, '$.vote') IS NOT NULL").rowcount
    notify.widget("news")
    return {"ok": True, "votes_cleared": n}


class RuleIn(BaseModel):
    term: str = Field(min_length=1, max_length=60)
    kind: Literal["topic", "source"] = "topic"
    section: str | None = Field(default=None, max_length=40)


@router.post("/rules", status_code=201)
async def add_rule(body: RuleIn):
    term = re.sub(r"\s+", " ", body.term).strip()
    if not term:
        raise HTTPException(422, "say what to hide")
    with db.db() as con:
        exists = con.execute("SELECT id FROM news_rules WHERE term = ? COLLATE NOCASE AND kind = ? AND section IS ?", (term, body.kind, body.section)).fetchone()
        rid = exists["id"] if exists else con.execute("INSERT INTO news_rules (term, kind, section, created_at) VALUES (?, ?, ?, ?)",
                                                      (term, body.kind, body.section, int(time.time()))).lastrowid
    notify.widget("news")
    return {"ok": True, "id": rid}


@router.delete("/rules/{rule_id}")
async def delete_rule(rule_id: int):
    with db.db() as con:
        if not con.execute("DELETE FROM news_rules WHERE id = ?", (rule_id,)).rowcount:
            raise HTTPException(404, "no such rule")
    notify.widget("news")
    return {"ok": True}


# ---- F1 --------------------------------------------------------------------------------------------------------------------

@router.get("/f1")
async def f1():
    ctx = Ctx()
    cal = ctx.calendar
    data = cal["data"] or {}
    return {"shield": ctx.public_lock(), "enabled": ctx.settings.get("f1_shield", True), "watched": ctx.settings.get("f1_watched"),
            "extra_terms": ctx.settings.get("f1_extra_terms", []), "season": data.get("season"),
            "rounds": [{k: r.get(k) for k in ("round", "name", "short", "first_session", "first_label", "race", "sprint")} for r in data.get("rounds", [])],
            "calendar_at": cal["fetched_at"], "calendar_error": cal["error"]}


class WatchedIn(BaseModel):
    round: int = Field(ge=0, le=40)
    season: int | None = None


@router.post("/f1/watched")
async def f1_watched(body: WatchedIn):
    cal = db.cache_get("f1cal")["data"] or {}
    season = body.season or cal.get("season")
    if not isinstance(season, int):
        raise HTTPException(409, "the F1 calendar hasn't loaded yet — try again in a minute")
    merged = news.widget.merge_settings(db.get_settings("news", news.widget.defaults), {"f1_watched": {"season": season, "round": body.round}})
    db.put_settings("news", merged)
    notify.widget("news")
    return {"ok": True, "shield": Ctx().public_lock()}


# ---- trying a feed ------------------------------------------------------------------------------------------------------------

class FeedTestIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)


@router.post("/test-feed")
async def test_feed(body: FeedTestIn):
    url = body.url.strip()
    if not url.startswith(("https://", "http://")):
        return {"ok": False, "error": "That needs to be an http(s) address."}
    if news.is_tabloid(url):
        return {"ok": False, "error": "That's a tabloid site — those are never allowed."}
    try:
        async with httpx.AsyncClient(headers={"User-Agent": f"Citadel/{__version__} ({config.CONTACT})"}, timeout=20, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
        if len(resp.content) > news.MAX_BYTES:
            return {"ok": False, "error": "That feed is too large."}
        parsed = feedparser.parse(resp.content)
        items = news.parse_feed(resp.content, {"id": "test", "name": "test"}, cutoff=0)
    except Exception as exc:  # noqa: BLE001 — whatever went wrong, say so plainly
        return {"ok": False, "error": f"Couldn't read it: {type(exc).__name__}"}
    if not items:
        return {"ok": False, "error": "That address answered, but it isn't a feed with any stories in it."}
    now = time.time()
    return {"ok": True, "title": news.clean(parsed.feed.get("title")) or "", "items": len(items),
            "last24": sum(1 for i in items if (i["meta"]["published"] or now) >= now - 86400),
            "images": sum(1 for i in items if i["meta"]["image"]), "sample": [i["title"] for i in items[:3]]}
