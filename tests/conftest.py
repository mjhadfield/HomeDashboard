"""Test harness: every test session gets a private data dir, so nothing touches the real database or .env."""
import os
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="citadel-test-")
os.environ["HUB_DATA_DIR"] = _TMP
os.environ["HUB_PORT"] = "18080"
os.environ["HUB_NO_FETCH"] = "1"                                  # no live MusicBrainz / RSS / calendar traffic from tests
os.environ["HUB_MUSIC_DB"] = os.path.join(_TMP, "no-music-db.sqlite")   # never seed from (or touch) the real Music Companion
# Make sure no real secrets/settings leak into tests via the environment.
for var in ("BINS_UPRN", "DIARY_ICAL_URL", "JELLYFIN_URL", "HUB_SECRET"):
    os.environ[var] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Import after the environment is set (config reads it at import time). load_dotenv doesn't override
# values that are already present, so the blanks above win over the real .env.
from app import auth, coverart, db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_cover_art(monkeypatch):
    """Never hit the real Cover Art Archive from a test — same "nothing external in tests" principle as
    HUB_NO_FETCH, for the one network call that isn't gated by it (releases.widget.fetch() is called directly
    by several tests, bypassing the scheduler). A test that wants to exercise art-fetching overrides this."""
    async def _no_art(_mbid):
        return None
    monkeypatch.setattr(coverart.client, "front_thumb", _no_art)


@pytest.fixture(scope="session")
def client():
    """A running app (lifespan included) against the temp database."""
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def owner_client(client):
    """A client that has enrolled as an owner device, sending the X-Hub header like the real UI does."""
    token = auth.create_enrol_token("owner", "pytest")
    with TestClient(app, base_url="http://testserver") as c:
        r = c.post(f"/enrol/{token}", follow_redirects=False)
        assert r.status_code == 303, r.text
        c.headers["X-Hub"] = "1"
        yield c


@pytest.fixture()
def guest_client(client):
    with TestClient(app) as c:
        yield c
