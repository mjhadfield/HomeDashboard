"""MusicBrainz: band memberships — who plays in which bands — for Discover's "band connections". No key needed; it
shares Citadel's one polite MusicBrainz client (about a request a second), and answers are kept for a month."""
from __future__ import annotations

from typing import Any

from ... import musicbrainz
from .. import store
from .base import Service, ServiceError

MONTH = 30 * 86400


class MusicBrainzService(Service):
    async def test(self) -> str:
        mbid = await musicbrainz.resolve_artist_id("Mastodon")
        return "Working — found Mastodon" if mbid else "Reachable, but no exact match for Mastodon"


service = MusicBrainzService(
    id="musicbrainz", name="MusicBrainz", order=40,
    blurb="Band members and their other bands, and album tracklists for deep cuts. No key needed.",
    link="https://musicbrainz.org/",
)


async def artist_rels(mbid: str) -> dict[str, Any]:
    """{name, type, members: [{name, mbid}], member_of: [{name, mbid}]} for one artist."""
    if not service.enabled():
        raise ServiceError("MusicBrainz is switched off in Settings → Music page")

    async def fetch():
        data = await musicbrainz.client.get(f"artist/{mbid}", inc="artist-rels")
        members, member_of = [], []
        for rel in data.get("relations", []):
            if rel.get("type") != "member of band" or not rel.get("artist"):
                continue
            other = {"name": rel["artist"]["name"], "mbid": rel["artist"]["id"]}
            (members if rel.get("direction") == "backward" else member_of).append(other)
        return {"name": data.get("name", ""), "type": data.get("type", ""), "members": members, "member_of": member_of}
    return await store.cached(f"mb:rels:{mbid}", MONTH, fetch)
