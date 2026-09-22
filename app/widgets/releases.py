"""New releases from the artists you follow, via MusicBrainz (no key needed).

Each run checks the artists least recently checked (a batch), so the whole list is covered over a day without
hammering MusicBrainz's 1-request/second limit. The first look at an artist is a silent baseline — only releases
that appear afterwards are flagged New.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from .. import config, content, coverart, musicbrainz as mb
from .base import Widget

log = logging.getLogger("hub.releases")


async def attach_art(items: list[dict[str, Any]]) -> None:
    """Best-effort front-cover thumbnail per item, from the Cover Art Archive (keyed by the release-group id
    already in `external_id`). Most release groups have none — that's not an error. Re-fetched every sync
    rather than cached forward, since the actual volume here is tiny (a handful of items at a time, not an
    archive) and this keeps items self-healing if art shows up later, with no extra bookkeeping."""
    for it in items:
        meta = it.setdefault("meta", {})
        try:
            meta["image"] = await coverart.client.front_thumb(it["external_id"])
        except Exception:
            meta["image"] = None       # same shape as "no art found" — a lookup failure isn't different from one
            log.warning("cover art lookup failed for %s (%s)", it.get("title"), it["external_id"], exc_info=True)

TYPES = ("Album", "EP", "Single")
# Never wanted, whatever the settings: not really "music you'd buy".
HARD_EXCLUDE = {"Remix", "DJ-mix", "Mixtape/Street", "Demo", "Interview", "Audiobook", "Audio drama", "Spokenword", "Field recording"}
# Off by default (studio albums only); one switch in Settings turns them on.
EXTRAS = {"Live", "Compilation", "Soundtrack"}


def wanted(primary: str | None, secondary: list[str], types: list[str], include_extras: bool) -> bool:
    return primary in types and not (HARD_EXCLUDE & set(secondary)) and (include_extras or not (EXTRAS & set(secondary)))


def type_label(primary: str, secondary: list[str]) -> str:
    if "Live" in secondary:
        return "Live album" if primary == "Album" else f"Live {primary}"
    if "Compilation" in secondary:
        return "Compilation"
    if "Soundtrack" in secondary:
        return "Soundtrack"
    return primary


def earliest(d: str) -> str:
    """The first day a partial MusicBrainz date could mean ('2027' -> 2027-01-01), so a far-off year is never mistaken for near."""
    return {4: f"{d}-01-01", 7: f"{d}-01"}.get(len(d), d)


def within(d: str, cutoff: str, horizon: str) -> bool:
    """Is this release date inside [cutoff, horizon] (both YYYY-MM-DD)? A partial date counts if any day of it could be
    inside; placeholder dates years out are dropped."""
    return bool(d) and content.sort_date(d) >= cutoff and earliest(d) <= horizon


def parse_release_groups(data: dict[str, Any], artist: str, *, cutoff: str, horizon: str, types: list[str], include_extras: bool = False) -> list[dict[str, Any]]:
    """MusicBrainz release-group search result -> feed items. `cutoff`/`horizon` are YYYY-MM-DD; MusicBrainz dates may be partial."""
    out = []
    for rg in data.get("release-groups", []):
        primary, secondary = rg.get("primary-type"), rg.get("secondary-types") or []
        d = rg.get("first-release-date") or ""
        if not wanted(primary, secondary, types, include_extras) or not within(d, cutoff, horizon):
            continue
        out.append({"external_id": rg["id"], "artist": artist, "title": rg["title"], "subtitle": type_label(primary, secondary),
                    "url": f"https://musicbrainz.org/release-group/{rg['id']}", "date": d, "meta": {"type": primary, "secondary": secondary}})
    return out


def search_query(mbid: str, cutoff: str, horizon: str, types: list[str]) -> str:
    """Ask MusicBrainz only for the release types we show, inside the window: results come back by relevance, so an artist
    with a pile of singles or live tracks could otherwise push a genuinely upcoming album out of the first page."""
    kinds = " OR ".join(t.lower() for t in types)
    return f"arid:{mbid} AND primarytype:({kinds}) AND firstreleasedate:[{cutoff} TO {horizon}]"


class ReleasesWidget(Widget):
    id = "releases"
    title = "Releases"
    interval = 24 * 3600
    daily_at = "04:00"
    min_interval = 6 * 3600
    timeout = 600
    defaults = {"batch": 50, "lookback_days": 31, "horizon_days": 365, "types": ["Album"], "include_extras": False}

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "batch" in new and not (isinstance(new["batch"], int) and 5 <= new["batch"] <= 80):
            raise ValueError("batch must be a whole number from 5 to 80")
        if "lookback_days" in new and not (isinstance(new["lookback_days"], int) and 7 <= new["lookback_days"] <= 365):
            raise ValueError("lookback_days must be a whole number from 7 to 365")
        if "horizon_days" in new and not (isinstance(new["horizon_days"], int) and 30 <= new["horizon_days"] <= 730):
            raise ValueError("horizon_days must be a whole number from 30 to 730")
        if "types" in new and not (isinstance(new["types"], list) and new["types"] and set(new["types"]) <= set(TYPES)):
            raise ValueError(f"types must be a non-empty list drawn from {', '.join(TYPES)}")
        if "include_extras" in new and not isinstance(new["include_extras"], bool):
            raise ValueError("include_extras must be true or false")
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        today = datetime.now(config.TZ).date()
        cutoff = (today - timedelta(days=settings["lookback_days"])).isoformat()
        horizon = (today + timedelta(days=settings["horizon_days"])).isoformat()
        min_age = content.FORCED_SECONDS if settings.get("_forced") else content.FRESH_SECONDS
        checked = added = 0
        for a in content.pick_artists("mb_checked_at", settings["batch"], min_age=min_age):
            mbid = a["mbid"]
            if not mbid and (mbid := await mb.resolve_artist_id(a["name"])):
                content.set_mbid(a["id"], mbid)
            if mbid:
                data = await mb.client.get("release-group", query=search_query(mbid, cutoff, horizon, settings["types"]), limit=100)
                if "release-groups" not in data:                    # MusicBrainz always sends the key (empty list if nothing): its absence means the API changed
                    log.warning("MusicBrainz answered without a 'release-groups' list for %s — has its API changed? Keys: %s", a["name"], ", ".join(sorted(data)) or "none")
                items = parse_release_groups(data, a["name"], cutoff=cutoff, horizon=horizon, types=settings["types"], include_extras=settings.get("include_extras", False))
                await attach_art(items)
                added += content.upsert_items("release", "musicbrainz", items, baseline=a["mb_checked_at"] is None)
            content.mark_checked(a["id"], "mb_checked_at")        # committed per artist: a timeout loses nothing already done
            checked += 1
        content.prune("release", older_than_days=400)
        return {"checked": checked, "new": added, "pending": content.count_stale("mb_checked_at", min_age)}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        return f"checked {d.get('checked', 0)} artists, {d.get('new', 0)} new" + (f", {d['pending']} still to do" if d.get("pending") else "")

    def retry_after(self, data: dict[str, Any] | None) -> int | None:
        return 20 if data and data.get("pending") else None          # still working through the artists: next batch shortly

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        # Judged against the *current* settings at read time, so narrowing the filter takes effect at once (no re-fetch).
        cutoff = (now.date() - timedelta(days=settings["lookback_days"])).isoformat()
        horizon = (now.date() + timedelta(days=settings["horizon_days"])).isoformat()
        keep = lambda i: wanted(i["meta"].get("type"), i["meta"].get("secondary") or [], settings["types"], settings.get("include_extras", False)) \
            and within(i["date"] or "", cutoff, horizon)
        # The one card that reads oldest to newest: last month's releases first, then everything announced, furthest away last.
        return {**content.list_items("release", keep=keep, oldest_first=True), "following": len(content.list_artists()), "horizon": horizon}


widget = ReleasesWidget()
