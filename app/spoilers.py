"""The F1 spoiler shield: Formula 1 stories stay hidden from a weekend's first competitive session until you say
you've watched that race.

* **When.** The race calendar (widgets/f1cal.py, from the Jolpica API) gives each round's first competitive session —
  sprint qualifying on a sprint weekend, otherwise qualifying. Once that has started for a round after the one you
  last watched, the shield is *locked*, and stays locked until you mark the round watched. No auto-unlock.
* **What.** Anything that looks like F1 (deliberately generous: it only costs anything while locked) and was published
  at or after that first session. A story with no known publish time counts as published now — the BBC's Most read
  list has none.
* **Fail-safe.** No calendar yet, or one more than 14 days old: every F1-looking story is hidden until it recovers.

The API applies this before anything leaves the server, so a hidden headline never reaches a browser.
"""
from __future__ import annotations

import re
import time
import unicodedata
from typing import Any

STALE_AFTER = 14 * 86400
UNSEEN_GRACE = 7 * 86400          # with nothing marked watched yet, a race that started within this long counts as unwatched

# Case-insensitive: the sport's own vocabulary and its sections' addresses.
TERMS = [r"\bF1\b", r"\bformula\s*(?:1|one)\b", r"\bgrands?\s*prix\b", r"\bpole\s*position\b", r"\bpole[- ]sitter\b",
         r"\bpodium\b", r"\bchequered\s*flag\b", r"\bdrivers'?\s*(?:world\s*)?(?:championship|title|standings)\b",
         r"\bconstructors'?\s*(?:championship|title|standings)\b", r"\bsprint\s*(?:race|qualifying|shootout)\b",
         r"\bfastest\s*lap\b", r"\bsafety\s*car\b", r"\bpit[- ]?(?:stop|lane|wall)\b", r"\bparc\s*ferm[eé]\b", r"\bFIA\b"]
# With their capitals: "Singapore GP" is a race, a "GP surgery" is a doctor's.
CASED_TERMS = [r"\b[A-Z][\w-]+ GPs?\b(?! (?:surgery|surgeries|practice|appointment|appointments|services?|contract|partner|receptionist))"]
URL_TERMS = [r"/formula1\b", r"/formula-1\b", r"/formula-one\b", r"/f1/", r"/f1-", r"/sport/motorsport\b", r"/motorsport/"]
# Fallback names while the calendar has never loaded (it brings the real, current list).
FALLBACK_TEAMS = ["Ferrari", "McLaren", "Mercedes", "Red Bull", "Racing Bulls", "Aston Martin", "Alpine", "Williams", "Haas", "Sauber", "Audi", "Cadillac"]
FALLBACK_DRIVERS = ["Verstappen", "Norris", "Piastri", "Leclerc", "Hamilton", "Russell", "Antonelli", "Alonso", "Sainz", "Albon"]


def fold(text: str) -> str:
    """Accents off ("Hülkenberg" -> "Hulkenberg", "Pérez" -> "Perez"), so either spelling matches."""
    return "".join(c for c in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(c))


def watched_round(watched: Any, calendar: dict[str, Any], now: float) -> int:
    """The last round of the calendar's season you've seen. Nothing marked yet: every race that started more than a
    week ago counts as seen, anything more recent doesn't — the safe default for a first run mid-weekend."""
    season = calendar.get("season")
    if isinstance(watched, dict) and isinstance(watched.get("round"), int):
        if watched.get("season") == season:
            return watched["round"]
        if isinstance(watched.get("season"), int) and isinstance(season, int) and watched["season"] > season:
            return 10_000                                  # you've moved on to a season the calendar doesn't know yet
        return 0                                           # a new season: nothing of it seen yet
    seen = [r["round"] for r in calendar.get("rounds", []) if r.get("race") and r["race"] <= now - UNSEEN_GRACE]
    return max(seen, default=0)


def lock_state(calendar: dict[str, Any] | None, fetched_at: float | None, watched: Any, now: float | None = None, *, enabled: bool = True) -> dict[str, Any]:
    """Whether F1 stories are being held back right now, since when, and for which rounds."""
    now = time.time() if now is None else now
    if not enabled:
        return {"on": False, "locked": False}
    if not calendar or not calendar.get("rounds") or not fetched_at:
        return {"on": True, "locked": True, "since": 0, "reason": "no-calendar", "rounds": []}
    if now - fetched_at > STALE_AFTER:
        return {"on": True, "locked": True, "since": 0, "reason": "stale", "rounds": []}
    seen = watched_round(watched, calendar, now)
    rounds = sorted(calendar["rounds"], key=lambda r: r["round"])
    pending = [r for r in rounds if r["round"] > seen and r.get("first_session") and r["first_session"] <= now]
    brief = lambda r: {k: r.get(k) for k in ("round", "name", "first_session", "first_label", "race")}
    if not pending:
        nxt = next((r for r in rounds if r["round"] > seen and r.get("first_session")), None)
        return {"on": True, "locked": False, "season": calendar.get("season"), "watched": seen, "next": brief(nxt) if nxt else None}
    return {"on": True, "locked": True, "since": min(r["first_session"] for r in pending), "reason": "race", "season": calendar.get("season"),
            "watched": seen, "rounds": [brief(r) for r in pending], "venues": sorted({v for r in pending for v in (r.get("venues") or []) if v})}


class Matcher:
    """Does this story look like Formula 1? The sport's own words, addresses and your extra words match in any case.
    Names match with their capitals (so "a stroll" isn't Lance Stroll), and a one-word name only counts when it isn't
    part of somebody else's: "Lewis Hamilton", "Hamilton wins" and "as Hamilton" do; "Margaret Hamilton",
    "Hayley Williams" and "Russell T Davies" don't."""

    def __init__(self, calendar: dict[str, Any] | None, extra: list[str] | None = None, venues: list[str] | None = None) -> None:
        cal = calendar or {}
        names = {fold(n).strip() for n in set(cal.get("drivers") or FALLBACK_DRIVERS) | set(cal.get("teams") or FALLBACK_TEAMS) | set(venues or []) if n}
        names = {n for n in names if len(n) >= 3}
        self._given: dict[str, set[str]] = {}
        for n in names:
            if " " in n:
                first, last = n.rsplit(" ", 1)
                self._given.setdefault(last, set()).add(first.split()[-1])
        self._known = names | {w for n in names for w in n.split()}
        multi = sorted((n for n in names if " " in n), key=len, reverse=True)
        single = sorted(n for n in names if " " not in n)
        self._multi = re.compile("|".join([rf"(?<!\w){re.escape(n)}(?!\w)" for n in multi] + CASED_TERMS))
        self._single = re.compile("|".join(rf"(?<!\w){re.escape(n)}(?!\w)" for n in single)) if single else None
        words = TERMS + [rf"(?<!\w){re.escape(fold(t).strip())}(?!\w)" for t in (extra or []) if fold(t).strip()]
        self._terms = re.compile("|".join(words), re.IGNORECASE)
        self._url = re.compile("|".join(URL_TERMS), re.IGNORECASE)

    def _lone_name(self, text: str) -> bool:
        found, others = [], set()
        for m in self._single.finditer(text) if self._single else ():
            name = m.group(0)
            before = re.search(r"([A-Z][\w'’.-]*)\s+$", text[: m.start()])
            after = re.match(r"\s+([A-Z][\w'’-]*)", text[m.end():])
            if before and before.group(1) not in self._given.get(name, ()) and before.group(1) not in self._known:
                others.add(name)                               # someone else's surname: Margaret Hamilton, Jacob Aron
            elif after and after.group(1) not in self._known:
                others.add(name)                               # part of a longer name: Russell T Davies
            else:
                found.append(name)
        # Once a story has named another Hamilton in full, its bare "Hamilton"s are about them too.
        return any(n not in others for n in found)

    def __call__(self, item: dict[str, Any]) -> bool:
        meta = item.get("meta") or {}
        topics = [t[1] for t in meta.get("topics") or [] if isinstance(t, (list, tuple)) and len(t) == 2]
        # " · " keeps the parts apart, so the end of one never reads as the start of a name in the next.
        text = fold(" · ".join(str(x) for x in (item.get("title"), meta.get("summary"), *topics) if x))
        if self._terms.search(text) or self._multi.search(text) or self._lone_name(text):
            return True
        return bool(item.get("url") and self._url.search(item["url"]))


def hidden(item: dict[str, Any], lock: dict[str, Any], matcher: Matcher) -> bool:
    """Hold this story back? Only while locked, only F1, and only if it could know a result (published since the lock)."""
    if not lock.get("locked") or item.get("meta", {}).get("revealed"):
        return False
    published = (item.get("meta") or {}).get("published")
    if isinstance(published, (int, float)) and published < lock.get("since", 0):
        return False
    return matcher(item)
