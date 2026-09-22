"""Settings API: everything the owner edits is validated server-side (the browser is not trusted)."""
import json
import shlex
import time

import pytest

from app import db
from tests.test_projects import needs_systemd, wait_for  # reuse the systemd helpers

BASE = "/api/owner"
LINK = {"name": "NAS", "subtitle": "192.168.0.10", "url": "http://192.168.0.10:5000/", "icon": "server", "health_url": "http://192.168.0.10:5000/",
        "is_primary": False, "show_guest": False, "tier": "main"}
PROJECT = {"slug": "pytest-app", "name": "Test app", "description": "d", "kind": "web", "url": "http://{host}:9999/", "port": 9999,
           "cwd": "/tmp", "command": "python3 -m http.server 9999", "env": {"FOO": "bar"}, "lan_visible": True, "enabled": True}


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with db.db() as con:
        con.execute("DELETE FROM links WHERE name LIKE 'pytest%' OR name = 'NAS'")
        con.execute("DELETE FROM projects WHERE slug LIKE 'pytest-%'")


def cfg(c):
    return c.get(f"{BASE}/config").json()


# ---- links -------------------------------------------------------------------------------------------

def test_link_crud_and_order(owner_client):
    r = owner_client.post(f"{BASE}/links", json=LINK)
    assert r.status_code == 201
    lid = r.json()["id"]
    got = next(l for l in cfg(owner_client)["links"] if l["id"] == lid)
    assert got["name"] == "NAS" and got["icon"] == "server" and got["health_url"] == LINK["health_url"]

    assert owner_client.put(f"{BASE}/links/{lid}", json={**LINK, "name": "pytest NAS", "show_guest": True, "tier": "more", "health_url": ""}).status_code == 200
    got = next(l for l in cfg(owner_client)["links"] if l["id"] == lid)
    assert got["name"] == "pytest NAS" and got["show_guest"] and got["tier"] == "more" and got["health_url"] == ""

    second = owner_client.post(f"{BASE}/links", json={**LINK, "name": "pytest two"}).json()["id"]
    ids = [l["id"] for l in cfg(owner_client)["links"]]
    order = [second] + [i for i in ids if i != second]
    assert owner_client.post(f"{BASE}/links/reorder", json={"ids": order}).status_code == 200
    assert [l["id"] for l in cfg(owner_client)["links"]][0] == second

    assert owner_client.delete(f"{BASE}/links/{lid}").status_code == 200
    assert owner_client.delete(f"{BASE}/links/{lid}").status_code == 404
    assert owner_client.put(f"{BASE}/links/{lid}", json=LINK).status_code == 404


@pytest.mark.parametrize("patch", [
    {"url": "javascript:alert(1)"}, {"url": "ftp://x/"}, {"url": "not a url"}, {"url": ""},
    {"health_url": "file:///etc/passwd"}, {"icon": "<script>"}, {"name": ""}, {"tier": "huge"},
])
def test_link_validation_rejects_bad_input(owner_client, patch):
    r = owner_client.post(f"{BASE}/links", json={**LINK, **patch})
    assert r.status_code == 422, patch


def test_validation_errors_are_readable(owner_client):
    detail = owner_client.post(f"{BASE}/links", json={**LINK, "url": "nope"}).json()["detail"]
    assert isinstance(detail, list) and "http" in json.dumps(detail).lower()


# ---- projects ----------------------------------------------------------------------------------------

def test_project_crud_parses_the_command_into_argv(owner_client):
    r = owner_client.post(f"{BASE}/projects", json={**PROJECT, "command": 'python3 -c "print(\'hi there\')"'})
    assert r.status_code == 201
    with db.db() as con:
        row = con.execute("SELECT * FROM projects WHERE slug = 'pytest-app'").fetchone()
    assert json.loads(row["start_argv"]) == ["python3", "-c", "print('hi there')"]        # quotes group words; no shell involved
    assert json.loads(row["env"]) == {"FOO": "bar"}

    got = next(p for p in cfg(owner_client)["projects"] if p["slug"] == "pytest-app")
    assert shlex.split(got["command"]) == ["python3", "-c", "print('hi there')"]      # round-trips exactly, quoting included
    assert got["cwd_exists"] is True

    r = owner_client.put(f"{BASE}/projects/pytest-app", json={**{k: v for k, v in PROJECT.items() if k != "slug"}, "name": "Renamed", "port": 9998})
    assert r.status_code == 200
    assert next(p for p in cfg(owner_client)["projects"] if p["slug"] == "pytest-app")["port"] == 9998

    assert owner_client.delete(f"{BASE}/projects/pytest-app").status_code == 200
    assert owner_client.delete(f"{BASE}/projects/pytest-app").status_code == 404


@pytest.mark.parametrize("patch", [
    {"slug": "Bad Slug"}, {"slug": "-lead"}, {"slug": "a" * 60}, {"cwd": "relative/path"}, {"command": ""}, {"command": "echo 'unterminated"},
    {"env": {"1BAD": "x"}}, {"env": {"OK": "line1\nline2"}}, {"port": 0}, {"port": 70000}, {"url": "javascript:1"}, {"kind": "daemon"},
])
def test_project_validation_rejects_bad_input(owner_client, patch):
    assert owner_client.post(f"{BASE}/projects", json={**PROJECT, **patch}).status_code == 422, patch


def test_duplicate_project_id_is_a_conflict(owner_client):
    assert owner_client.post(f"{BASE}/projects", json=PROJECT).status_code == 201
    assert owner_client.post(f"{BASE}/projects", json=PROJECT).status_code == 409


def test_missing_directory_is_a_warning_not_an_error(owner_client):
    r = owner_client.post(f"{BASE}/projects", json={**PROJECT, "cwd": "/definitely/not/here"})
    assert r.status_code == 201 and any("doesn't exist" in w for w in r.json()["warnings"])


@needs_systemd
def test_deleting_a_running_project_stops_it_first(owner_client, tmp_path):
    body = {**PROJECT, "slug": "pytest-sleeper", "kind": "desktop", "url": None, "port": None, "cwd": str(tmp_path), "command": "sleep 300", "env": {}}
    assert owner_client.post(f"{BASE}/projects", json=body).status_code == 201
    assert owner_client.post(f"{BASE}/projects/pytest-sleeper/start").status_code == 200
    from app import procs
    import asyncio
    assert wait_for(lambda: procs.unit("pytest-sleeper") in asyncio.run(procs.active_units()))
    assert owner_client.delete(f"{BASE}/projects/pytest-sleeper").status_code == 200
    assert not wait_for(lambda: procs.unit("pytest-sleeper") in asyncio.run(procs.active_units()), timeout=2)


def test_disabled_projects_disappear_from_the_dashboard_state(owner_client):
    owner_client.post(f"{BASE}/projects", json=PROJECT)
    assert any(p["slug"] == "pytest-app" for p in owner_client.get(f"{BASE}/state").json()["projects"])
    owner_client.put(f"{BASE}/projects/pytest-app", json={**{k: v for k, v in PROJECT.items() if k != "slug"}, "enabled": False})
    assert not any(p["slug"] == "pytest-app" for p in owner_client.get(f"{BASE}/state").json()["projects"])
    assert any(p["slug"] == "pytest-app" and not p["enabled"] for p in cfg(owner_client)["projects"])   # still editable in Settings


# ---- widget settings ---------------------------------------------------------------------------------

def test_bins_and_diary_settings_are_range_checked(owner_client):
    ok = owner_client.put(f"{BASE}/widgets/bins/settings", json={"put_out_hour": 18, "alert_streams": ["blue", "brown"]})
    assert ok.status_code == 200 and ok.json()["put_out_hour"] == 18
    for bad in ({"put_out_hour": 24}, {"put_out_hour": "x"}, {"put_out_hour": True}, {"alert_streams": ["glitter"]}, {"alert_streams": "blue"}):
        assert owner_client.put(f"{BASE}/widgets/bins/settings", json=bad).status_code == 422, bad
    for bad in ({"horizon_days": 3}, {"horizon_days": 9999}, {"horizon_days": "x"}):
        assert owner_client.put(f"{BASE}/widgets/diary/settings", json=bad).status_code == 422, bad


# ---- devices / config ---------------------------------------------------------------------------------

def test_config_shape_and_no_secrets(owner_client):
    owner_client.put(f"{BASE}/widgets/diary/settings", json={"calendars": [{"name": "T", "url": "https://x.example/private-SECRETTOKEN/basic.ics"}]})
    text = owner_client.get(f"{BASE}/config").text
    assert "SECRETTOKEN" not in text                                   # calendar URLs never leave the server
    c = json.loads(text)
    assert {"links", "projects", "widgets", "devices", "screens", "icons", "streams", "colours", "paths"} <= set(c)
    assert c["widgets"]["diary"]["settings"]["calendars"][0]["has_url"] is True


def test_rename_device_and_enrol_link_with_qr(owner_client):
    me = next(d for d in owner_client.get(f"{BASE}/devices").json() if d["current"])
    assert owner_client.patch(f"{BASE}/devices/{me['id']}", json={"name": "Renamed by test"}).status_code == 200
    assert next(d for d in cfg(owner_client)["devices"] if d["current"])["name"] == "Renamed by test"
    assert owner_client.patch(f"{BASE}/devices/nope", json={"name": "x"}).status_code == 404
    r = owner_client.post(f"{BASE}/devices/enrol", json={"role": "owner", "name": "Phone"}).json()
    assert "/enrol/" in r["url"] and "<svg" in r["qr_svg"] and "<script" not in r["qr_svg"]


def test_guest_cannot_use_any_new_settings_route(guest_client):
    h = {"X-Hub": "1"}
    assert guest_client.get(f"{BASE}/config").status_code == 403
    assert guest_client.post(f"{BASE}/links", json=LINK, headers=h).status_code == 403
    assert guest_client.post(f"{BASE}/projects", json=PROJECT, headers=h).status_code == 403
    assert guest_client.put(f"{BASE}/screens", json={"panels": ["diary"]}, headers=h).status_code == 403
    assert guest_client.post(f"{BASE}/pair", json={"code": "ABC123"}, headers=h).status_code == 403
    assert guest_client.get("/settings", follow_redirects=False).status_code == 303


def test_settings_page_for_owner(owner_client):
    r = owner_client.get("/settings")
    assert r.status_code == 200 and 'id="panel"' in r.text
