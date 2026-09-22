"""Pinning: pinned links/projects sort to the top everywhere, and nothing unpins them by accident."""
import pytest

from app import db

BASE = "/api/owner"
LINK = {"name": "pytest link", "url": "http://192.168.0.50/", "icon": "globe"}
PROJECT = {"name": "pytest proj", "kind": "web", "port": 9555, "cwd": "/tmp", "command": "sleep 1"}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db.db() as con:
        con.execute("DELETE FROM links WHERE name LIKE 'pytest%'")
        con.execute("DELETE FROM projects WHERE slug LIKE 'pytest-%'")


def mk_link(c, name, **kw):
    return c.post(f"{BASE}/links", json={**LINK, "name": f"pytest {name}", **kw}).json()["id"]


def mk_project(c, slug, **kw):
    assert c.post(f"{BASE}/projects", json={**PROJECT, "slug": f"pytest-{slug}", "name": f"pytest {slug}", **kw}).status_code == 201
    return f"pytest-{slug}"


def names(items, prefix="pytest"):
    return [i["name"] for i in items if i["name"].startswith(prefix)]


def pinned_names(items, prefix="pytest"):
    return [i["name"] for i in items if i["name"].startswith(prefix) and i["pinned"]]


def state_links(c):
    return c.get(f"{BASE}/state").json()["links"]


def test_pinned_link_moves_to_the_top_and_back(owner_client):
    a, b, c = (mk_link(owner_client, n) for n in "abc")
    assert names(state_links(owner_client)) == ["pytest a", "pytest b", "pytest c"]

    assert owner_client.post(f"{BASE}/links/{c}/pin", json={"pinned": True}).json() == {"ok": True, "pinned": True}
    links = state_links(owner_client)
    assert links[0]["name"] == "pytest c" and links[0]["pinned"] is True          # first of ALL links, not just ours
    assert names(links) == ["pytest c", "pytest a", "pytest b"]

    owner_client.post(f"{BASE}/links/{c}/pin", json={"pinned": False})
    assert names(state_links(owner_client)) == ["pytest a", "pytest b", "pytest c"]


def test_a_pin_outranks_expanded_only_tier(owner_client):
    main = mk_link(owner_client, "main")
    hidden = mk_link(owner_client, "hidden", tier="more")
    assert names(state_links(owner_client)) == ["pytest main", "pytest hidden"]           # 'more' normally sorts last
    owner_client.post(f"{BASE}/links/{hidden}/pin", json={"pinned": True})
    assert names(state_links(owner_client)) == ["pytest hidden", "pytest main"]           # a pin brings it to the front


def test_manual_order_is_kept_within_the_pinned_group(owner_client):
    a, b, c = (mk_link(owner_client, n) for n in "abc")
    for i in (a, b):
        owner_client.post(f"{BASE}/links/{i}/pin", json={"pinned": True})
    everything = [l["id"] for l in owner_client.get(f"{BASE}/config").json()["links"]]
    first_two = [i for i in everything if i in (a, b)]
    reordered = [i for i in everything if i not in (a, b)]
    owner_client.post(f"{BASE}/links/reorder", json={"ids": [first_two[1], first_two[0]] + reordered})
    got = owner_client.get(f"{BASE}/config").json()["links"]
    assert [l["id"] for l in got if l["id"] in (a, b)] == [first_two[1], first_two[0]]
    assert all(l["pinned"] for l in got[:2])                                                # pinned group stays on top


def test_editing_without_the_pinned_field_never_unpins(owner_client):
    """Regression guard: an older client that doesn't know about pins must not clear them."""
    lid = mk_link(owner_client, "keep", pinned=True)
    assert next(l for l in owner_client.get(f"{BASE}/config").json()["links"] if l["id"] == lid)["pinned"] is True
    body = {**LINK, "name": "pytest keep (edited)"}                                          # no `pinned` key at all
    assert owner_client.put(f"{BASE}/links/{lid}", json=body).status_code == 200
    got = next(l for l in owner_client.get(f"{BASE}/config").json()["links"] if l["id"] == lid)
    assert got["pinned"] is True and got["name"] == "pytest keep (edited)"
    owner_client.put(f"{BASE}/links/{lid}", json={**body, "pinned": False})                  # explicit false does unpin
    assert next(l for l in owner_client.get(f"{BASE}/config").json()["links"] if l["id"] == lid)["pinned"] is False


def test_guest_page_also_lists_pinned_links_first(owner_client, guest_client):
    plain = mk_link(owner_client, "plain", show_guest=True)
    pinned = mk_link(owner_client, "pinned", show_guest=True)
    owner_client.post(f"{BASE}/links/{pinned}/pin", json={"pinned": True})
    assert names(guest_client.get("/api/guest").json()["links"]) == ["pytest pinned", "pytest plain"]


def test_projects_pin_like_links(owner_client):
    a, b = mk_project(owner_client, "a"), mk_project(owner_client, "b")
    order = lambda: [p["slug"] for p in owner_client.get(f"{BASE}/state").json()["projects"] if p["slug"].startswith("pytest-")]
    assert order() == [a, b]
    assert owner_client.post(f"{BASE}/projects/{b}/pin", json={"pinned": True}).status_code == 200
    projects = owner_client.get(f"{BASE}/state").json()["projects"]
    assert projects[0]["slug"] == b and projects[0]["pinned"] is True
    assert order() == [b, a]
    # editing without the field keeps the pin
    owner_client.put(f"{BASE}/projects/{b}", json={**PROJECT, "name": "pytest b2"})
    assert owner_client.get(f"{BASE}/state").json()["projects"][0]["slug"] == b


def test_pin_endpoints_are_owner_only_and_validated(owner_client, guest_client):
    lid = mk_link(owner_client, "x")
    slug = mk_project(owner_client, "x")
    h = {"X-Hub": "1"}
    assert guest_client.post(f"{BASE}/links/{lid}/pin", json={"pinned": True}, headers=h).status_code == 403
    assert guest_client.post(f"{BASE}/projects/{slug}/pin", json={"pinned": True}, headers=h).status_code == 403
    assert owner_client.post(f"{BASE}/links/99999/pin", json={"pinned": True}).status_code == 404
    assert owner_client.post(f"{BASE}/projects/nope/pin", json={"pinned": True}).status_code == 404
    assert owner_client.post(f"{BASE}/links/{lid}/pin", json={"pinned": "maybe"}).status_code == 422
    assert owner_client.post(f"{BASE}/links/{lid}/pin", json={}).status_code == 422


def test_pin_position_is_stable_and_new_pins_take_the_next_free_slot(owner_client):
    """Pinning is a positional stack, not a sort key: each pin takes the next slot and keeps it until
    unpinned; unpinning one doesn't move the others, and a later pin never reuses a freed slot."""
    a, b, c = (mk_link(owner_client, n) for n in "abc")
    for i in (a, b, c):
        owner_client.post(f"{BASE}/links/{i}/pin", json={"pinned": True})
    assert pinned_names(state_links(owner_client)) == ["pytest a", "pytest b", "pytest c"]

    owner_client.post(f"{BASE}/links/{b}/pin", json={"pinned": False})          # unpin the middle one
    assert pinned_names(state_links(owner_client)) == ["pytest a", "pytest c"]  # a and c didn't move, just closed up

    d = mk_link(owner_client, "d")
    owner_client.post(f"{BASE}/links/{d}/pin", json={"pinned": True})
    assert pinned_names(state_links(owner_client)) == ["pytest a", "pytest c", "pytest d"]   # next slot, not b's old one

    owner_client.post(f"{BASE}/links/{b}/pin", json={"pinned": True})   # re-pinning b is a new pin, not a restore
    assert pinned_names(state_links(owner_client)) == ["pytest a", "pytest c", "pytest d", "pytest b"]


def test_re_pinning_an_already_pinned_item_does_not_move_it(owner_client):
    a, b = mk_link(owner_client, "a"), mk_link(owner_client, "b")
    owner_client.post(f"{BASE}/links/{a}/pin", json={"pinned": True})
    owner_client.post(f"{BASE}/links/{b}/pin", json={"pinned": True})
    assert pinned_names(state_links(owner_client)) == ["pytest a", "pytest b"]
    owner_client.post(f"{BASE}/links/{a}/pin", json={"pinned": True})           # pin something that's already pinned
    assert pinned_names(state_links(owner_client)) == ["pytest a", "pytest b"]  # ...stays put, doesn't jump to the back


def test_pin_order_is_shared_across_links_and_projects(owner_client):
    """The Launchpad merges links and projects into one tile list ordered by pin_order — that only lines up
    with the order things were actually pinned in if both tables draw from the same sequence."""
    proj_a = mk_project(owner_client, "a")
    link_b = mk_link(owner_client, "b")
    proj_c = mk_project(owner_client, "c")
    owner_client.post(f"{BASE}/projects/{proj_a}/pin", json={"pinned": True})
    owner_client.post(f"{BASE}/links/{link_b}/pin", json={"pinned": True})
    owner_client.post(f"{BASE}/projects/{proj_c}/pin", json={"pinned": True})

    got = owner_client.get(f"{BASE}/state").json()
    pa = next(p for p in got["projects"] if p["slug"] == proj_a)
    pc = next(p for p in got["projects"] if p["slug"] == proj_c)
    lb = next(l for l in got["links"] if l["id"] == link_b)
    assert pa["pin_order"] < lb["pin_order"] < pc["pin_order"]
