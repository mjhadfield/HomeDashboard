"""The SQL DB page embeds one link's url in an iframe — for_sql marks which one, mirroring for_media/for_music
(see test_media_link.py, test_music_link.py) but as a fully independent column: setting one must never touch
the others."""
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


def test_for_sql_can_be_set_on_create_and_shows_in_state(owner_client):
    mk_link(owner_client, "a", for_sql=True)
    got = state_links(owner_client)["pytest a"]
    assert got["for_sql"] is True and got["url"] == LINK["url"]


def test_only_one_link_is_for_sql_at_a_time(owner_client):
    a, b = mk_link(owner_client, "a", for_sql=True), mk_link(owner_client, "b")
    assert links_cfg(owner_client)["pytest a"]["for_sql"] is True

    owner_client.put(f"{BASE}/links/{b}", json={**LINK, "name": "pytest b", "for_sql": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_sql"] is True
    assert cfg["pytest a"]["for_sql"] is False        # setting b cleared a, not just added b


def test_for_sql_can_be_unset(owner_client):
    lid = mk_link(owner_client, "a", for_sql=True)
    owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "name": "pytest a", "for_sql": False})
    assert links_cfg(owner_client)["pytest a"]["for_sql"] is False


def test_for_sql_defaults_off(owner_client):
    mk_link(owner_client, "a")
    assert links_cfg(owner_client)["pytest a"]["for_sql"] is False


def test_for_media_for_music_and_for_sql_are_independent(owner_client):
    """The three flags are separate columns on the same link, and separate columns across different links --
    setting one must never clear or imply the others, in either direction."""
    lid = mk_link(owner_client, "a", for_media=True, for_music=True, for_sql=True)
    got = links_cfg(owner_client)["pytest a"]
    assert got["for_media"] is True and got["for_music"] is True and got["for_sql"] is True

    other = mk_link(owner_client, "b")
    owner_client.put(f"{BASE}/links/{other}", json={**LINK, "name": "pytest b", "for_sql": True})
    cfg = links_cfg(owner_client)
    assert cfg["pytest b"]["for_sql"] is True
    assert cfg["pytest a"]["for_sql"] is False           # cleared, same column
    assert cfg["pytest a"]["for_media"] is True          # untouched, different column
    assert cfg["pytest a"]["for_music"] is True          # untouched, different column
