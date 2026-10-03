"""Music Companion: your own listening history, records and gigs, read straight from its database — read-only, always.
This is what makes the playlists yours; the outside services only add what you haven't heard."""
from __future__ import annotations

from typing import Any

from ... import config
from .. import library
from .base import Field, Service, ServiceError


class CompanionService(Service):
    def extra(self) -> dict[str, Any]:
        return {"path": str(config.MUSIC_DB), "found": config.MUSIC_DB.exists()}

    def configured(self, cfg=None) -> bool:
        return config.MUSIC_DB.exists()

    async def test(self) -> str:
        if not config.MUSIC_DB.exists():
            raise ServiceError(f"No database at {config.MUSIC_DB}")
        s = library.summary()
        return f"{s['scrobbles']:,} plays, {s['records']} records, {s['shows']} setlists"


service = CompanionService(
    id="companion", name="Music Companion", order=5, always_on=True,
    blurb="Your plays, records and gigs — read-only. Where the database is comes from HUB_MUSIC_DB in .env. "
          "The maintenance server's password lets Settings → Maintenance run its scheduled Last.fm import.",
    fields=(Field("maintenance_url", "Maintenance server", placeholder="http://127.0.0.1:8643", required=False),
            Field("maintenance_password", "Maintenance password", secret=True, required=False)),
)
