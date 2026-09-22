"""Screens (TVs): pairing by short code, the read-only kiosk role, and what a TV is allowed to see."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import auth, db, notify, state
from app.bus import bus
from app.main import app

BASE = "/api/owner"


@pytest.fixture(autouse=True)
def _clean_pairings():
    with db.db() as con:
        con.execute("DELETE FROM pairings")
    yield
    with db.db() as con:
        con.execute("DELETE FROM pairings")


def start(tv):
    r = tv.post("/api/pair/start")
    assert r.status_code == 200
    return r.json()


def test_pairing_flow_end_to_end(owner_client):
    tv = TestClient(app)
    p = start(tv)
    assert len(p["code"].replace("-", "")) == 6 and p["code"][3] == "-"

    assert tv.post("/api/pair/poll", json={"secret": p["secret"]}).json() == {"status": "pending"}
    assert tv.get("/api/kiosk/state").status_code == 403                       # not yet

    # the owner types the code any way they like: lower case, no dash, stray spaces
    assert owner_client.post(f"{BASE}/pair", json={"code": " " + p["code"].lower().replace("-", "") + " ", "name": "Games room TV"}).status_code == 200

    r = tv.post("/api/pair/poll", json={"secret": p["secret"]})
    assert r.json() == {"status": "ok"} and auth.COOKIE in r.headers["set-cookie"]
    assert "httponly" in r.headers["set-cookie"].lower()

    assert tv.get("/api/kiosk/state").status_code == 200
    assert tv.get("/").text.count('id="stage"') == 1                            # the TV view at /
    devices = owner_client.get(f"{BASE}/devices").json()
    tvdev = next(d for d in devices if d["name"] == "Games room TV")
    assert tvdev["role"] == "kiosk"                                             # pairing can never create an owner

    # a replayed poll can't mint a second device
    assert tv.post("/api/pair/poll", json={"secret": p["secret"]}).status_code == 410
    assert sum(d["name"] == "Games room TV" for d in owner_client.get(f"{BASE}/devices").json()) == 1


def test_a_paired_screen_is_read_only_and_not_an_owner(owner_client):
    tv = TestClient(app)
    p = start(tv)
    owner_client.post(f"{BASE}/pair", json={"code": p["code"], "name": "TV"})
    tv.post("/api/pair/poll", json={"secret": p["secret"]})
    for path in (f"{BASE}/state", f"{BASE}/config", f"{BASE}/devices"):
        assert tv.get(path).status_code == 403, path
    assert tv.post(f"{BASE}/pair", json={"code": "AAAAAA"}, headers={"X-Hub": "1"}).status_code == 403
    assert tv.post("/api/kiosk/state", headers={"X-Hub": "1"}).status_code == 405
    assert tv.get("/settings", follow_redirects=False).status_code == 303
    assert tv.get("/tv").text.count('id="stage"') == 1


def test_unpaired_screen_gets_the_pairing_page(guest_client):
    assert 'id="code"' in guest_client.get("/tv").text
    assert 'id="code"' not in guest_client.get("/").text            # '/' is still the guest page for strangers


def test_bad_expired_and_stolen_pairings(owner_client):
    tv = TestClient(app)
    assert tv.post("/api/pair/poll", json={"secret": "x" * 30}).status_code == 410       # unknown secret
    p = start(tv)
    other = TestClient(app)
    assert other.post("/api/pair/poll", json={"secret": "y" * 30}).status_code == 410     # can't guess someone else's
    assert owner_client.post(f"{BASE}/pair", json={"code": "ZZZZZZ"}).status_code == 404  # no such code
    with db.db() as con:
        con.execute("UPDATE pairings SET expires_at = ?", (int(time.time()) - 1,))
    assert owner_client.post(f"{BASE}/pair", json={"code": p["code"]}).status_code == 404  # expired: can't be approved
    assert tv.post("/api/pair/poll", json={"secret": p["secret"]}).status_code == 410


def test_unauthenticated_pairing_is_bounded(guest_client):
    for _ in range(auth.PAIR_MAX_PENDING):
        assert guest_client.post("/api/pair/start").status_code == 200
    assert guest_client.post("/api/pair/start").status_code == 429
    with db.db() as con:
        assert con.execute("SELECT COUNT(*) c FROM pairings").fetchone()["c"] == auth.PAIR_MAX_PENDING


def test_a_code_can_only_be_approved_once(owner_client):
    tv = TestClient(app)
    p = start(tv)
    assert owner_client.post(f"{BASE}/pair", json={"code": p["code"]}).status_code == 200
    assert owner_client.post(f"{BASE}/pair", json={"code": p["code"]}).status_code == 404


# ---- what a TV may see -------------------------------------------------------------------------------------

def screens(owner_client, **kw):
    body = {"panels": ["bins"], "rotate_seconds": 12, "show_alerts": True, **kw}
    assert owner_client.put(f"{BASE}/screens", json=body).status_code == 200


def test_diary_is_opt_in_and_absent_from_the_payload_by_default(owner_client):
    screens(owner_client)                                        # bins only
    s = owner_client.get("/api/kiosk/state").json()
    assert set(s["widgets"]) == {"bins"} and "diary" not in owner_client.get("/api/kiosk/state").text
    screens(owner_client, panels=["bins", "diary"])
    assert set(owner_client.get("/api/kiosk/state").json()["widgets"]) == {"bins", "diary"}


def test_alerts_only_reach_the_tv_for_panels_it_is_allowed(monkeypatch, owner_client):
    both = [{"kind": "bin", "title": "Blue bin", "level": "now"}, {"kind": "event", "title": "Dentist", "level": "soon"}]
    monkeypatch.setattr(state, "compute_alerts", lambda widgets, at: both)
    kinds = lambda: [a["kind"] for a in state.kiosk_state()["alerts"]]
    screens(owner_client, panels=["bins"]);           assert kinds() == ["bin"]
    screens(owner_client, panels=["diary"]);          assert kinds() == ["event"]
    screens(owner_client, panels=["bins", "diary"]);  assert kinds() == ["bin", "event"]
    screens(owner_client, panels=["bins", "diary"], show_alerts=False); assert kinds() == []


def test_screen_settings_are_validated(owner_client):
    for bad in ({"panels": ["system"]}, {"panels": ["bins"], "rotate_seconds": 1}, {"panels": ["bins"], "rotate_seconds": 9999}, {"panels": "bins"}):
        assert owner_client.put(f"{BASE}/screens", json=bad).status_code == 422, bad
    assert owner_client.put(f"{BASE}/screens", json={"panels": ["bins", "bins"]}).json()["panels"] == ["bins"]   # de-duplicated


# ---- the event stream carries no data ------------------------------------------------------------------------

def test_events_are_triggers_only_so_a_tv_can_never_receive_calendar_data():
    q: asyncio.Queue = asyncio.Queue()
    bus._subs.add(q)
    try:
        notify.widget("diary"); notify.projects(); notify.health(); notify.config()
        got = [q.get_nowait() for _ in range(4)]
    finally:
        bus._subs.discard(q)
    assert got == [("widget", {"id": "diary"}), ("projects", {}), ("health", {}), ("config", {})]
