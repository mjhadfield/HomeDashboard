"""The Odysseus page embeds one link's url in an iframe — for_ai marks which one, mirroring for_media/for_music/for_sql
exactly, as its own independent column."""
import pytest

from app import db

BASE = "/api/owner"
LINK = {"name": "pytest link", "url": "http://192.168.0.50/", "icon": "spark"}


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


def test_spark_is_an_allowed_link_icon(owner_client):
    lid = mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["icon"] == "spark"
    assert "spark" in owner_client.get(f"{BASE}/config").json()["icons"]


def test_for_ai_can_be_set_on_create_and_shows_in_state(owner_client):
    mk_link(owner_client, "a", for_ai=True)
    got = state_links(owner_client)["pytest a"]
    assert got["for_ai"] is True and got["url"] == LINK["url"]


def test_only_one_link_is_for_ai_at_a_time(owner_client):
    a, b = mk_link(owner_client, "a", for_ai=True), mk_link(owner_client, "b")
    assert links_cfg(owner_client)["pytest a"]["for_ai"] is True

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest b", "for_ai": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_ai"] is True
    assert cfg["pytest a"]["for_ai"] is False           # setting b cleared a, not just added b


def test_for_ai_can_be_unset(owner_client):
    lid = mk_link(owner_client, "a", for_ai=True)
    owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "name": "pytest a", "for_ai": False})
    assert links_cfg(owner_client)["pytest a"]["for_ai"] is False


def test_for_ai_defaults_off(owner_client):
    mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["for_ai"] is False


def test_for_ai_is_independent_of_the_other_pages(owner_client):
    lid = mk_link(owner_client, "a", for_media=True, for_music=True, for_sql=True, for_ai=True)
    other = mk_link(owner_client, "b")
    owner_client.put(f"{BASE}/links/{other}", json={**LINK, "name": "pytest b", "for_ai": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_ai"] is True
    assert cfg["pytest a"]["for_ai"] is False                # cleared, same column
    assert cfg["pytest a"]["for_media"] is True and cfg["pytest a"]["for_music"] is True and cfg["pytest a"]["for_sql"] is True
