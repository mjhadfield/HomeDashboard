"""The Companion page embeds one link's url in an iframe — for_music marks which one, mirroring for_media
(see test_media_link.py) but as a fully independent column: setting one must never touch the other."""
import pytest

from app import db

BASE = "/api/owner"
LINK = {"name": "pytest link", "url": "http://192.168.0.50/", "icon": "globe"}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db.db() as con:
        con.execute("DELETE FROM links WHERE name LIKE 'pytest%'")


def mk_link(c, name, **kw):
    return c.post(f"{BASE}/links", json={**LINK, "name": f"pytest {name}", **kw}).json()["id"]


def links_cfg(c):
    return {l["name"]: l for l in c.get(f"{BASE}/config").json()["links"] if l["name"].startswith("pytest")}


def state_links(c):
    return {l["name"]: l for l in c.get(f"{BASE}/state").json()["links"] if l["name"].startswith("pytest")}


def test_for_music_can_be_set_on_create_and_shows_in_state(owner_client):
    mk_link(owner_client, "a", for_music=True)
    got = state_links(owner_client)["pytest a"]
    assert got["for_music"] is True and got["url"] == LINK["url"]


def test_only_one_link_is_for_music_at_a_time(owner_client):
    a, b = mk_link(owner_client, "a", for_music=True), mk_link(owner_client, "b")
    assert links_cfg(owner_client)["pytest a"]["for_music"] is True

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest b", "for_music": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_music"] is True
    assert cfg["pytest a"]["for_music"] is False        # setting b cleared a, not just added b


def test_for_music_can_be_unset(owner_client):
    lid = mk_link(owner_client, "a", for_music=True)
    owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "name": "pytest a", "for_music": False})
    assert links_cfg(owner_client)["pytest a"]["for_music"] is False


def test_for_music_defaults_off(owner_client):
    mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["for_music"] is False


def test_for_media_and_for_music_are_independent(owner_client):
    """The two flags are separate columns on the same link, and separate columns across different links --
    setting one must never clear or imply the other, in either direction."""
    lid = mk_link(owner_client, "a", for_media=True, for_music=True)
    got = links_cfg(owner_client)["pytest a"]
    assert got["for_media"] is True and got["for_music"] is True

    other = mk_link(owner_client, "b")
    owner_client.put(f"{BASE}/links/{other}", json={**LINK, "name": "pytest b", "for_music": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_music"] is True
    assert cfg["pytest a"]["for_music"] is False         # cleared, same column
    assert cfg["pytest a"]["for_media"] is True          # untouched, different column
