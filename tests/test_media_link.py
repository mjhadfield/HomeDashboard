"""The Media page embeds one link's url in an iframe — for_media marks which one, at most one at a time."""
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


def test_for_media_can_be_set_on_create_and_shows_in_state(owner_client):
    lid = mk_link(owner_client, "a", for_media=True)
    got = state_links(owner_client)["pytest a"]
    assert got["for_media"] is True and got["url"] == LINK["url"]


def test_only_one_link_is_for_media_at_a_time(owner_client):
    a, b = mk_link(owner_client, "a", for_media=True), mk_link(owner_client, "b")
    assert links_cfg(owner_client)["pytest a"]["for_media"] is True

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest b", "for_media": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_media"] is True
    assert cfg["pytest a"]["for_media"] is False        # setting b cleared a, not just added b


def test_for_media_can_be_unset(owner_client):
    lid = mk_link(owner_client, "a", for_media=True)
    owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "name": "pytest a", "for_media": False})
    assert links_cfg(owner_client)["pytest a"]["for_media"] is False


def test_for_media_defaults_off(owner_client):
    mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["for_media"] is False
