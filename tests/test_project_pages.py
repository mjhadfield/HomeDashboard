"""The Companion and SQL DB pages can embed a project instead of a link (so an app needn't be a Launchpad link just
to be embedded). One source per page across links and projects together."""
import pytest

from app import db

BASE = "/api/owner"
PROJECT = {"name": "pytest proj", "kind": "web", "port": 9556, "url": "http://{host}:9556/", "cwd": "/tmp", "command": "sleep 1"}
LINK = {"name": "pytest link", "url": "http://192.168.0.50/", "icon": "globe"}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db.db() as con:
        con.execute("DELETE FROM projects WHERE slug LIKE 'pytest-%'")
        con.execute("DELETE FROM links WHERE name LIKE 'pytest%'")


def mk(c, slug, **kw):
    assert c.post(f"{BASE}/projects", json={**PROJECT, "slug": f"pytest-{slug}", "name": f"pytest {slug}", **kw}).status_code == 201
    return f"pytest-{slug}"


def flags(c):
    cfg = {p["slug"]: (p["for_music"], p["for_sql"]) for p in c.get(f"{BASE}/config").json()["projects"] if p["slug"].startswith("pytest")}
    st = {p["slug"]: (p["for_music"], p["for_sql"]) for p in c.get(f"{BASE}/state").json()["projects"] if p["slug"].startswith("pytest")}
    assert cfg == st
    return cfg


def edit(c, slug, **kw):
    assert c.put(f"{BASE}/projects/{slug}", json={**PROJECT, "name": slug, **kw}).status_code == 200


def test_a_project_can_be_each_page_and_only_one_at_a_time(owner_client):
    a, b = mk(owner_client, "a", for_music=True), mk(owner_client, "b")
    assert flags(owner_client) == {a: (True, False), b: (False, False)}
    edit(owner_client, b, for_music=True, for_sql=True)                 # b takes the Companion page from a
    assert flags(owner_client) == {a: (False, False), b: (True, True)}
    edit(owner_client, b, for_sql=False)                                # an edit without the field leaves for_music alone
    assert flags(owner_client) == {a: (False, False), b: (True, False)}


def test_a_link_and_a_project_never_both_claim_a_page(owner_client):
    def link_flag():
        return next(l["for_music"] for l in owner_client.get(f"{BASE}/config").json()["links"] if l["name"] == "pytest link")
    owner_client.post(f"{BASE}/links", json={**LINK, "for_music": True})
    p = mk(owner_client, "p", for_music=True)                           # the project takes it from the link…
    assert link_flag() is False and flags(owner_client)[p] == (True, False)
    lid = next(l["id"] for l in owner_client.get(f"{BASE}/config").json()["links"] if l["name"] == "pytest link")
    owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "for_music": True})   # …and a link can take it back
    assert link_flag() is True and flags(owner_client)[p] == (False, False)
