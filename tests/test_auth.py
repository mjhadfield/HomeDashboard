"""Roles: guests get the guest page and nothing else; owners are made by one-time enrolment."""
import time

from fastapi.testclient import TestClient

from app import auth, db
from app.main import app

OWNER_GET_ROUTES = ["/api/owner/state", "/api/owner/devices", "/api/owner/widgets/bins/settings", "/api/owner/projects/x/logs"]


def test_guest_root_serves_guest_page(guest_client):
    r = guest_client.get("/")
    assert r.status_code == 200 and 'id="doors"' in r.text and "grid" not in r.text


def test_guest_cannot_reach_any_owner_route(guest_client):
    for path in OWNER_GET_ROUTES:
        assert guest_client.get(path).status_code == 403, path
    for path in ["/api/owner/projects/x/start", "/api/owner/projects/x/stop", "/api/owner/widgets/bins/refresh", "/api/owner/devices/enrol"]:
        assert guest_client.post(path, headers={"X-Hub": "1"}).status_code == 403, path
    assert guest_client.put("/api/owner/widgets/bins/settings", json={}, headers={"X-Hub": "1"}).status_code == 403
    assert guest_client.delete("/api/owner/devices/abc", headers={"X-Hub": "1"}).status_code == 403


def test_guest_api_exposes_only_guest_links(guest_client):
    with db.db() as con:
        con.execute("INSERT INTO links (name, url, show_guest) VALUES ('Public', 'http://x/', 1), ('Private', 'http://y/', 0)")
    names = [l["name"] for l in guest_client.get("/api/guest").json()["links"]]
    assert "Public" in names and "Private" not in names


def test_enrol_flow_and_single_use(guest_client):
    token = auth.create_enrol_token("owner", "Laptop")
    # GET only previews: link-preview bots must not be able to burn the token.
    assert guest_client.get(f"/enrol/{token}").status_code == 200
    assert guest_client.get(f"/enrol/{token}").status_code == 200
    r = guest_client.post(f"/enrol/{token}", follow_redirects=False)
    assert r.status_code == 303 and auth.COOKIE in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    assert guest_client.get("/api/owner/state").status_code == 200
    # Spent:
    other = TestClient(app)
    assert other.post(f"/enrol/{token}", follow_redirects=False).status_code == 410
    assert other.get(f"/enrol/{token}").status_code == 410


def test_expired_and_bogus_tokens(guest_client):
    token = auth.create_enrol_token("owner", "x", ttl=-1)
    assert guest_client.post(f"/enrol/{token}", follow_redirects=False).status_code == 410
    assert guest_client.post("/enrol/not-a-token", follow_redirects=False).status_code == 410


def test_tampered_cookie_is_a_guest(owner_client):
    good = owner_client.cookies.get(auth.COOKIE)
    forged = TestClient(app)
    forged.cookies.set(auth.COOKIE, good.split(".")[0] + ".deadbeef" * 5)
    assert forged.get("/api/owner/state").status_code == 403
    unsigned = TestClient(app)
    unsigned.cookies.set(auth.COOKIE, good.split(".")[0])
    assert unsigned.get("/api/owner/state").status_code == 403


def test_owner_writes_need_the_csrf_header(owner_client):
    assert owner_client.post("/api/owner/widgets/system/refresh", headers={"X-Hub": ""}).status_code == 403
    assert owner_client.post("/api/owner/widgets/system/refresh").status_code == 202  # fixture supplies X-Hub


def test_kiosk_device_is_not_an_owner(guest_client):
    token = auth.create_enrol_token("kiosk", "TV")
    assert guest_client.post(f"/enrol/{token}", follow_redirects=False).status_code == 303
    assert guest_client.get("/api/owner/state").status_code == 403


def test_revoked_device_loses_access(owner_client):
    me = owner_client.get("/api/owner/devices").json()
    current = next(d for d in me if d["current"])
    # can't lock yourself out by accident
    assert owner_client.delete(f"/api/owner/devices/{current['id']}").status_code == 400
    auth.revoke_device(current["id"])
    assert owner_client.get("/api/owner/state").status_code == 403


def test_owner_root_serves_dashboard(owner_client):
    r = owner_client.get("/")
    assert r.status_code == 200 and 'id="grid"' in r.text


def test_security_headers(guest_client):
    r = guest_client.get("/")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "frame-src http://192.168.0.70:8096" in r.headers["content-security-policy"]   # lets the Media page embed Jellyfin
    assert r.headers["x-content-type-options"] == "nosniff"
    # API docs are not exposed
    assert guest_client.get("/docs").status_code == 404 and guest_client.get("/openapi.json").status_code == 404
