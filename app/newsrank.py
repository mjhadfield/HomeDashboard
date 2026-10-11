"""Turning a pile of stories into a calm page: one story per event, ranked, with what you've taught it applied.

* **Merging.** The same event from several outlets becomes one story: the section's first-listed source (BBC by default)
  keeps it, the others become "also" links. Two headlines are one story when they share enough significant words.
* **Ranking.** More outlets covering it, a higher place in its own feed (BBC's feed order *is* its running order), being
  in the BBC's Most read, what you've liked, and freshness. Every part is a plain number, kept explainable on purpose.
* **Learning.** A 👍/👎 moves the weights of the story's topics (newstopics.py); weights fade with a ~60-day half-life,
  so tastes can drift. Stories you've clearly gone off are held back (shown on request), never silently lost.
"""
from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

from . import newstopics

HALF_LIFE = 60 * 86400
SOURCE_WEIGHT = 0.25          # a vote nudges the outlet a little, not like a topic
HIDE_BELOW = -2.0             # a story whose learned score is this low waits under "hidden for you"
SAME_WINDOW = 36 * 3600
RARE_PAIR = 8                 # …and a word in at most this many counts towards "four uncommon words in common"
RARE = 3                      # a name in at most this many of a section's recent headlines is distinctive enough to match on

_STEM = re.compile(r"(?:ing|ed|es|s)$")


def url_key(url: str | None) -> str:
    """The same article whatever the tracking parameters or BBC domain: host (bbc.com = bbc.co.uk) + path."""
    if not url:
        return ""
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url
    host = (u.hostname or "").removeprefix("www.")
    if host in ("bbc.com", "bbc.co.uk"):
        host = "bbc.co.uk"
    return f"{host}{u.path.rstrip('/')}".casefold()


def tokens(title: str) -> set[str]:
    """Headline words for "is this the same story?": lower-case, stop words out, crudely stemmed so
    "livestreamed"/"livestream" and "strikes"/"strike" agree."""
    out = set()
    for t in re.findall(r"[a-z0-9]+", (title or "").casefold().replace("’", "'").replace("'s ", " ")):
        if len(t) < 3 or t in newstopics.STOPWORDS:
            continue
        out.add(_STEM.sub("", t) if len(t) > 5 else t)
    return out


def same_story(a: set[str], b: set[str], phrases: list[set[str]] = (), df: dict[str, int] | None = None, all_names: list[set[str]] = ()) -> bool:
    """Enough shared headline words — or fewer, when both name the same *uncommon* person/place/thing and share another
    plain word besides: "Anne Carson wins Nobel prize in literature" is "Canadian poet Anne Carson wins Nobel…", while two
    "South Korea" stories, or two about Trump, usually aren't. Errs towards keeping stories apart: a wrong merge hides a
    story under "also", a missed one only shows a near-duplicate."""
    shared = a & b
    inter = len(shared)
    if inter >= 3 and (inter / len(a | b) >= 0.45 or inter / min(len(a), len(b)) >= 0.7):
        return True
    if inter < 2 or df is None:
        return False
    # Title Case and ALL-CAPS headlines (music sites) hide which words are names, so lean on rarity instead: four or more
    # shared words that few of this section's recent headlines use ("judas", "priest", "ian", "hill") is one event.
    if sum(1 for t in shared if df.get(t, 0) <= RARE_PAIR) >= 4:
        return True
    names = set().union(*all_names) if all_names else set()
    plain = shared - names                                    # the other shared word mustn't just be part of a name ("South" + "Korea")
    return bool(plain) and any(df.get(t, 0) <= RARE for ph in phrases for t in ph & shared)


def name_phrases(item: dict[str, Any]) -> list[set[str]]:
    """Each name in a headline as its words (plus its first word, which may be a name the extractor couldn't be sure of)."""
    out = [tokens(label) for kind, label in (item.get("meta") or {}).get("topics") or [] if kind == "p"]
    first = re.match(r"\s*([A-Z][\w'’-]+)", item.get("title") or "")
    if first and first.group(1).casefold() not in newstopics.STOPWORDS:
        out.append(tokens(first.group(1)))
    return [ph for ph in out if ph]


def when(item: dict[str, Any]) -> float:
    """When it was published (or, failing that, first seen)."""
    p = (item.get("meta") or {}).get("published")
    return float(p) if isinstance(p, (int, float)) else float(item.get("first_seen") or 0)


def merge(items: list[dict[str, Any]], source_order: dict[str, int]) -> list[dict[str, Any]]:
    """Group one section's stories into events. Returns the keepers, each with `also` = the others."""
    ordered = sorted(items, key=lambda i: (source_order.get(i["source"], 99), (i.get("meta") or {}).get("pos", 99), -when(i)))
    toks_of = {i["id"]: tokens(i["title"]) for i in items}
    df: dict[str, int] = {}                                   # how many of these headlines use each word: a rare name is telling
    for t in toks_of.values():
        for w in t:
            df[w] = df.get(w, 0) + 1
    groups: list[tuple[dict[str, Any], set[str], list[dict[str, Any]]]] = []
    by_url: dict[str, int] = {}
    for it in ordered:
        k = url_key(it.get("url"))
        if k and k in by_url:                                   # the very same article listed by two feeds
            groups[by_url[k]][2].append(it)
            continue
        toks = toks_of[it["id"]]
        for gi, (keeper, ktoks, members) in enumerate(groups):
            if keeper["source"] != it["source"] and abs(when(keeper) - when(it)) <= SAME_WINDOW and \
                    same_story(ktoks, toks, [ph for ph in name_phrases(it) if any(ph <= q or q <= ph for q in name_phrases(keeper))], df,
                               name_phrases(it) + name_phrases(keeper)):
                members.append(it)
                break
        else:
            groups.append((it, toks, []))
            gi = len(groups) - 1
        if k:
            by_url.setdefault(k, gi)
    out = []
    for keeper, _, members in groups:
        seen = {keeper["source"]}
        also = []
        for m in members:
            if m["source"] not in seen:
                seen.add(m["source"])
                also.append(m)
        out.append({**keeper, "also": also, "members": members})
    return out


# ---- learning ---------------------------------------------------------------------------------------------------------

def decayed(weight: float, updated_at: float, now: float) -> float:
    return weight * 0.5 ** (max(0.0, now - updated_at) / HALF_LIFE)


def topic_keys(item: dict[str, Any], source_name: str = "") -> list[tuple[str, str, float]]:
    """(key, label, how much one vote moves it) for each thing a vote on this story teaches."""
    out = [(newstopics.key(k, label), label, newstopics.KIND_WEIGHT.get(k, 0.3)) for k, label in (item.get("meta") or {}).get("topics") or []]
    if source_name:
        out.append((newstopics.key("s", source_name), source_name, SOURCE_WEIGHT))
    return out


def preference(item: dict[str, Any], weights: dict[str, float], source_name: str = "") -> tuple[float, list[str]]:
    """The learned score for a story (raw sum of its topics' current weights), and the labels that drove it most."""
    parts = [(weights.get(k, 0.0), label) for k, label, _ in topic_keys(item, source_name)]
    raw = sum(w for w, _ in parts)
    drivers = [label for w, label in sorted(parts, key=lambda p: -abs(p[0])) if abs(w) >= 0.25][:3]
    return raw, drivers


def score(story: dict[str, Any], now: float, *, most_read: int | None = None, pref: float = 0.0, lean: float = 0.0, mine: float = 0.0) -> float:
    """`lean` is the section's favour/play-down nudge; `mine` how much the artists in a Music story mean to you (0–3)."""
    coverage = 1 + len(story.get("also") or [])
    pos = min([(story.get("meta") or {}).get("pos", 20)] + [(m.get("meta") or {}).get("pos", 20) for m in story.get("members") or []])
    age_h = max(0.0, now - when(story)) / 3600
    return (1.2 * (coverage - 1) + max(0, 8 - pos) / 8 + ((11 - most_read) / 10 if most_read else 0)
            + 1.5 * math.tanh(pref / 2) + 2.0 * math.exp(-age_h / 18) + lean + mine)


def muted_by(item: dict[str, Any], rules: list[dict[str, Any]], section: str | None, source_name: str) -> str | None:
    """The rule that keeps this story out, if any: a source, a topic, or a word/phrase in the headline."""
    if not rules:
        return None
    meta = item.get("meta") or {}
    topic_labels = {label.casefold() for _, label in meta.get("topics") or []}
    text = f" {item.get('title') or ''} {meta.get('summary') or ''} ".casefold()
    for r in rules:
        if r.get("section") and r["section"] != section:
            continue
        term = r["term"].casefold()
        if r["kind"] == "source":
            if term in (source_name.casefold(), (item.get("source") or "").casefold()):
                return r["term"]
        elif term in topic_labels or re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text):
            return r["term"]
    return None



def _matches(item: dict[str, Any], terms: list[str]) -> list[str]:
    """Which of these terms a story matches: one of the feed's own categories, a whole word/phrase in its headline or
    summary, or (a term starting with "/") part of its address."""
    if not terms:
        return []
    meta = item.get("meta") or {}
    cats = {label.casefold() for kind, label in meta.get("topics") or [] if kind == "c"}
    text = f" {item.get('title') or ''} {meta.get('summary') or ''} ".casefold()
    path = (urlsplit(item.get("url") or "").path or "").casefold()
    out = []
    for t in terms:
        f = t.casefold()
        if (f.startswith("/") and f in path) or f in cats or (not f.startswith("/") and re.search(rf"(?<!\w){re.escape(f)}(?!\w)", text)):
            out.append(t)
    return out


def leaning(item: dict[str, Any], favour: list[str], play_down: list[str], weights: dict[str, float]) -> float:
    """How far a section's leanings move this story: up if it's the kind of thing the section favours, down if it's what
    it plays down (both can apply). Nothing is hidden by this, only reordered."""
    up = weights.get("favour", 0) if _matches(item, favour) else 0.0
    down = weights.get("play_down", 0) if _matches(item, play_down) else 0.0
    return up - down
