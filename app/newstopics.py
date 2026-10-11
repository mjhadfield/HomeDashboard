"""What a news story is about, as a short list of topics the News page can learn from.

Three kinds, strongest first: names in the headline ("Keir Starmer", "SpaceX"), the feed's own categories (Guardian
and Ars tag every story), and plain significant words. Each topic is [kind, label]; `key()` is the stable form used
for the learned weights and for "Never show" rules. Deliberately simple and explainable — Settings lists every topic
the page has learned, so a reader can always see why something moved.
"""
from __future__ import annotations

import re
from typing import Any

KIND_WEIGHT = {"p": 1.0, "c": 0.7, "w": 0.3}
MAX_TOPICS = 12

STOPWORDS = frozenset("""
a about above after again against all almost also am among an and any are aren't around as at back be because been
before being below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down
during each even ever every few for from further get gets getting give given go goes going gone got had hadn't has
hasn't have haven't having he he'd he'll he's her here here's hers herself him himself his how how's however i i'd
i'll i'm i've if in into is isn't it it's its itself just keep last least less let let's like made make makes many may
me might more most much must mustn't my myself near need needs new news next no nor not now of off on once one only
or other ought our ours ourselves out over own per plus put said same say says see seen set shall shan't she she'd
she'll she's should shouldn't show since so some still such take than that that's the their theirs them themselves
then there there's these they they'd they'll they're they've this those though three through to too took two under
until up upon us use used very via was wasn't way we we'd we'll we're we've were weren't what what's when when's
where where's whether which while who who's whom why why's will with within without won't would wouldn't year years
yet you you'd you'll you're you've your yours yourself yourselves live latest update updates watch video report
reports week day days today first time times call calls told tell tells amid after over warns warn urges urge
""".split())

# Feed categories that just restate the section — every story in the feed has them, so they say nothing about taste.
_GENERIC_CATEGORIES = frozenset({"news", "world news", "uk news", "world", "uk", "technology", "science", "tech", "music",
                                 "features", "feature", "opinion", "analysis", "article", "articles", "home", "top stories"})

_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][\wÀ-ÖØ-öø-ÿ'’.&-]*")


def key(kind: str, label: str) -> str:
    return f"{kind}:{label.casefold().strip()}"


def _strip(tok: str) -> str:
    tok = tok.replace("’", "'")
    tok = re.sub(r"'s$", "", tok)                     # Starmer's -> Starmer
    return tok.strip("'.-&")


def _is_cap(tok: str) -> bool:
    return bool(tok) and (tok[0].isupper() or (len(tok) > 1 and any(c.isupper() for c in tok[1:]) and tok[0].isalpha()))


def _title_case(tokens: list[str]) -> bool:
    """Ars/Verge-style Title Case Headlines capitalise every word, so capitals say nothing about names there."""
    words = [t for t in tokens if t and t[0].isalpha() and t.casefold() not in STOPWORDS]
    return len(words) >= 3 and sum(1 for t in words if t[0].isupper()) / len(words) > 0.75


def names(title: str) -> list[str]:
    """Runs of capitalised words, e.g. "Keir Starmer", "James Webb Space Telescope". A lone capitalised first word
    ("Drought declared…") is grammar, not a name, so it only counts as part of a longer run. In a Title Case headline
    only internal-capital words (SpaceX, iPhone) and acronyms (NASA) are trusted."""
    out: list[str] = []
    for clause in re.split(r"[:;|–—]|\s-\s", title):
        found = _WORD.findall(clause)
        tokens = [_strip(t) for t in found]
        breaks = {i for i, t in enumerate(found) if re.search(r"['’]s$", t)}    # "NASA's James Webb": a possessive ends a name
        tokens, breaks = [t for t in tokens if t], {i for i in breaks if tokens[i]}
        if not tokens:
            continue
        if _title_case(tokens):
            out += [t for t in tokens if (t.isupper() and 2 <= len(t) <= 6 and t.isalpha()) or (t[0].isupper() and any(c.isupper() for c in t[1:]) and not t.isupper())]
            continue
        run: list[str] = []
        runs: list[tuple[int, list[str]]] = []
        for i, t in enumerate(tokens + [""]):
            if t and _is_cap(t) and t.casefold() not in STOPWORDS:
                if not run:
                    start = i
                run.append(t)
                if i in breaks:
                    runs.append((start, run))
                    run = []
            else:
                if run:
                    runs.append((start, run))
                run = []
        for start, r in runs:
            if start == 0 and len(r) == 1 and not (r[0].isupper() and len(r[0]) > 1) and 0 not in breaks:
                continue                                   # a sentence's first word (but "Starmer's …" is a name)
            out.append(" ".join(r))
    seen, uniq = set(), []
    for n in out:
        if n.casefold() not in seen and len(n) >= 2:
            seen.add(n.casefold())
            uniq.append(n)
    return uniq


def words(title: str, exclude: set[str] = frozenset()) -> list[str]:
    """Significant plain words, lower-cased (≥4 letters, not stop words, not already part of a name)."""
    out = []
    for t in _WORD.findall(title):
        w = _strip(t).casefold()
        if len(w) >= 4 and w.isalpha() and w not in STOPWORDS and w not in exclude and w not in out:
            out.append(w)
    return out


def categories(tags: Any) -> list[str]:
    """The feed's own tags (feedparser `entry.tags`), minus the section-level ones every story in a feed shares."""
    out = []
    for t in tags or []:
        term = (t.get("term") if isinstance(t, dict) else getattr(t, "term", None)) or ""
        term = re.sub(r"\s+", " ", str(term)).strip()
        if 2 <= len(term) <= 40 and term.casefold() not in _GENERIC_CATEGORIES and term not in out:
            out.append(term)
    return out[:4]


def extract(title: str, tags: Any = None) -> list[list[str]]:
    """[[kind, label], …] for a story, strongest first, at most MAX_TOPICS."""
    ns = names(title)
    in_names = {w.casefold() for n in ns for w in n.split()}
    topics = [["p", n] for n in ns[:6]] + [["c", c] for c in categories(tags)] + [["w", w] for w in words(title, in_names)[:6]]
    seen, out = set(), []
    for kind, label in topics:
        k = key(kind, label)
        if k not in seen:
            seen.add(k)
            out.append([kind, label])
    return out[:MAX_TOPICS]
