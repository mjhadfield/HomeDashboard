"""Content API: curation, followed artists, booked gigs — and how gigs feed the alerts strip and the TV."""
import json
from datetime import datetime, timedelta

import pytest

from app import config, content, db
from app.widgets import vinyl
from tests.test_content import make_music_db

BASE = "/api/owner"


@pytest.fixture(autouse=True)
def _clean(client):
    def wipe():
        with db.db() as con:
            for t in ("feed_items", "followed_artists", "gigs"):
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM widget_settings WHERE widget IN ('radar', 'tv')")
    wipe()
    yield
    wipe()


def local_date(days=0):
    return (datetime.now(config.TZ).date() + timedelta(days=days)).isoformat()


GIG = {"artist": "Gojira", "venue": "O2 Academy", "city": "Leeds", "date": "2026-12-26", "doors": "18:30", "ticket_url": "https://tickets.example/1", "notes": "block B"}


def test_guests_cannot_touch_any_content_route(guest_client):
    h = {"X-Hub": "1"}
    assert guest_client.get(f"{BASE}/artists").status_code == 403
    assert guest_client.get(f"{BASE}/gigs").status_code == 403
    for method, path, body in (("post", "/artists", {"name": "X"}), ("patch", "/artists/1", {"status": "mute"}), ("delete", "/artists/1", None),
                               ("post", "/artists/resync", None), ("post", "/gigs", GIG), ("put", "/gigs/1", GIG), ("delete", "/gigs/1", None),
                               ("post", "/items/1/status", {"status": "seen"}), ("post", "/items/mark-seen/news", None), ("post", "/items/1/read", None),
                               ("patch", "/items/1/date", {"date": None}), ("delete", "/items/1", None), ("post", "/vinyl/preorders", {"artist": "X", "title": "Y"}),
                               ("put", "/vinyl/preorders/1", {"artist": "X", "title": "Y"}),
                               ("post", "/radar/1/book", GIG)):
        r = getattr(guest_client, method)(BASE + path, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (method, path)


def test_item_curation_endpoints(owner_client):
    content.upsert_items("news", "f", [{"external_id": "1", "title": "A story", "date": "2026-09-20"}], baseline=False)
    iid = content.list_items("news")["items"][0]["id"]
    assert owner_client.post(f"{BASE}/items/{iid}/status", json={"status": "pinned"}).status_code == 200
    assert content.list_items("news")["items"][0]["status"] == "pinned"
    assert owner_client.post(f"{BASE}/items/{iid}/status", json={"status": "bogus"}).status_code == 422
    assert owner_client.post(f"{BASE}/items/99999/status", json={"status": "seen"}).status_code == 404
    content.upsert_items("news", "f", [{"external_id": "2", "title": "Another", "date": "2026-09-20"}], baseline=False)
    assert owner_client.post(f"{BASE}/items/mark-seen/news").json()["cleared"] == 1
    assert owner_client.post(f"{BASE}/items/mark-seen/bogus").status_code == 422
    assert owner_client.post(f"{BASE}/items/{iid}/read").status_code == 200
    reread = next(i for i in content.list_items("news")["items"] if i["id"] == iid)
    assert reread["meta"].get("read") is True and reread["status"] == "pinned"              # reading doesn't disturb status


def test_marking_an_item_purchased_moves_it_to_confirmed_preorders(owner_client):
    content.upsert_items("vinyl", "shop-a", [{"external_id": "1", "artist": "Ghost", "title": "Skeletá", "date": "2027-01-01"}], baseline=True)
    iid = content.list_items("vinyl")["items"][0]["id"]
    assert owner_client.post(f"{BASE}/items/{iid}/status", json={"status": "purchased"}).status_code == 200
    state = owner_client.get(f"{BASE}/state").json()
    assert all(i["id"] != iid for i in state["widgets"]["vinyl"]["data"]["items"])
    pre = next(p for p in state["widgets"]["vinyl"]["data"]["preorders"] if p["id"] == iid)
    assert pre["artist"] == "Ghost" and pre["title"] == "Skeletá"


def test_removing_a_confirmed_preorder_requires_purchased_status(owner_client):
    content.upsert_items("vinyl", "shop-a", [{"external_id": "1", "artist": "Ghost", "title": "Skeletá", "date": "2027-01-01"}], baseline=True)
    iid = content.list_items("vinyl")["items"][0]["id"]
    assert owner_client.delete(f"{BASE}/items/{iid}").status_code == 409                     # not purchased yet
    owner_client.post(f"{BASE}/items/{iid}/status", json={"status": "purchased"})
    assert owner_client.delete(f"{BASE}/items/{iid}").status_code == 200
    assert content.list_purchased("vinyl") == []
    assert owner_client.delete(f"{BASE}/items/99999").status_code == 404


def test_inline_date_edit_validates_and_accepts_tbc(owner_client):
    iid = content.add_manual_purchase("vinyl", artist="Ghost", title="Skeletá", variant="", date=None, image="")
    assert owner_client.patch(f"{BASE}/items/{iid}/date", json={"date": "not-a-date"}).status_code == 422
    assert owner_client.patch(f"{BASE}/items/{iid}/date", json={"date": "2027-06-01"}).status_code == 200
    assert content.list_purchased("vinyl")[0]["date"] == "2027-06-01"
    assert owner_client.patch(f"{BASE}/items/{iid}/date", json={"date": None}).status_code == 200
    assert content.list_purchased("vinyl")[0]["date"] is None
    assert owner_client.patch(f"{BASE}/items/99999/date", json={"date": None}).status_code == 404


def test_manual_purchase_endpoint_creates_a_confirmed_preorder(owner_client, monkeypatch):
    async def fake_cover(url):
        return "/covers/fake.jpg"
    monkeypatch.setattr(vinyl, "fetch_manual_cover", fake_cover)

    body = {"artist": "Ghost", "title": "Skeletá", "variant": "Ltd. clear vinyl", "date": "2027-06-01", "image_url": "https://example.com/x.jpg"}
    r = owner_client.post(f"{BASE}/vinyl/preorders", json=body)
    assert r.status_code == 201
    p = content.list_purchased("vinyl")[0]
    assert p["artist"] == "Ghost" and p["source"] == "manual" and p["meta"]["image"] == "/covers/fake.jpg"

    assert owner_client.post(f"{BASE}/vinyl/preorders", json={**body, "image_url": "not a url"}).status_code == 422


def test_editing_a_store_preorder_survives_the_stores_next_sync(owner_client, monkeypatch):
    async def fake_cover(url):
        return "/covers/mi.jpg"
    monkeypatch.setattr(vinyl, "fetch_manual_cover", fake_cover)

    listing = {"external_id": "earache:trivium:citg", "artist": "Trivium", "title": "Crown In The Grave", "url": "https://earache.com/products/citg",
               "date": "2026-11-20", "meta": {"shop": "Earache", "format": "2×LP", "image": "https://cdn.shopify.com/earache.jpg"}}
    content.upsert_items("vinyl", "earache", [listing], baseline=True)
    iid = content.list_items("vinyl")["items"][0]["id"]
    owner_client.post(f"{BASE}/items/{iid}/status", json={"status": "purchased"})

    body = {"artist": "Trivium", "title": "Crown In The Grave", "variant": "Metal Injection exclusive", "shop": "Metal Injection",
            "date": "2026-11-20", "product_url": "https://store.metalinjection.net/products/citg", "image_url": "https://cdn.shopify.com/mi.jpg"}
    r = owner_client.put(f"{BASE}/vinyl/preorders/{iid}", json=body)
    assert r.status_code == 200 and r.json()["cover"] is True

    content.upsert_items("vinyl", "earache", [listing], baseline=False)          # Earache still lists it the next night
    p = content.list_purchased("vinyl")[0]
    assert p["url"] == body["product_url"] and p["meta"]["shop"] == "Metal Injection" and p["meta"]["variant"] == "Metal Injection exclusive"
    assert p["meta"]["image"] == "/covers/mi.jpg" and p["meta"]["format"] == "2×LP"
    with db.db() as con:
        assert con.execute("SELECT date_set FROM feed_items WHERE id = ?", (iid,)).fetchone()[0] == 0   # unchanged date isn't frozen as hand-set


def test_editing_a_preorder_keeps_the_cover_unless_a_new_one_is_fetched(owner_client, monkeypatch):
    async def failing_cover(url):
        return ""
    monkeypatch.setattr(vinyl, "fetch_manual_cover", failing_cover)
    iid = content.add_manual_purchase("vinyl", artist="System Of A Town", title="Toxicity", variant="", date=None, image="/covers/old.jpg")
    body = {"artist": "System Of A Down", "title": "Toxicity", "date": "2026-11-20"}
    assert owner_client.put(f"{BASE}/vinyl/preorders/{iid}", json=body).json()["cover"] is None
    assert owner_client.put(f"{BASE}/vinyl/preorders/{iid}", json={**body, "image_url": "https://example.com/x.jpg"}).json()["cover"] is False
    p = content.list_purchased("vinyl")[0]
    assert p["artist"] == "System Of A Down" and p["date"] == "2026-11-20" and p["meta"]["image"] == "/covers/old.jpg"
    assert owner_client.put(f"{BASE}/vinyl/preorders/{iid}", json={**body, "product_url": "nope"}).status_code == 422
    assert owner_client.put(f"{BASE}/vinyl/preorders/99999", json=body).status_code == 404
    content.upsert_items("vinyl", "shop-a", [{"external_id": "1", "artist": "Ghost", "title": "Skeletá"}], baseline=True)
    unbought = content.list_items("vinyl")["items"][0]["id"]
    assert owner_client.put(f"{BASE}/vinyl/preorders/{unbought}", json=body).status_code == 404     # only confirmed pre-orders


def test_a_preorder_within_seven_days_raises_a_reminder_with_a_persistent_badge_outside_it(owner_client):
    assert not any(a["kind"] == "vinyl" for a in alerts(owner_client))
    content.add_manual_purchase("vinyl", artist="Ghost", title="Far off", variant="", date=local_date(20), image="")
    assert not any(a["kind"] == "vinyl" for a in alerts(owner_client))                        # 20 days out: no alert yet
    far = next(p for p in owner_client.get(f"{BASE}/state").json()["widgets"]["vinyl"]["data"]["preorders"] if p["title"] == "Far off")
    assert far["in_days"] == 20                                                               # but the badge is always there

    content.add_manual_purchase("vinyl", artist="Ghost", title="Soon", variant="", date=local_date(1), image="")
    a = next(a for a in alerts(owner_client) if a["kind"] == "vinyl")
    assert a["chip"] == "Tomorrow" and a["level"] == "soon" and "Soon" in a["title"]

    with db.db() as con:
        con.execute("UPDATE feed_items SET date = ? WHERE title = 'Soon'", (local_date(0),))
    a = next(a for a in alerts(owner_client) if a["kind"] == "vinyl")
    assert a["chip"] == "Today" and a["level"] == "now"


def test_tv_never_gets_vinyl_alerts(owner_client):
    content.add_manual_purchase("vinyl", artist="Ghost", title="Soon", variant="", date=local_date(0), image="")
    owner_client.put(f"{BASE}/screens", json={"panels": ["bins", "diary", "gigs"], "rotate_seconds": 12, "show_alerts": True})
    s = owner_client.get("/api/kiosk/state").json()
    assert not any(a["kind"] == "vinyl" for a in s["alerts"])
    assert owner_client.post(f"{BASE}/items/99999/read").status_code == 404


def test_followed_artists_lifecycle(owner_client):
    r = owner_client.post(f"{BASE}/artists", json={"name": "  Gojira  "})
    assert r.status_code == 201
    aid = r.json()["id"]
    assert owner_client.post(f"{BASE}/artists", json={"name": "gojira"}).status_code == 409           # names are unique, case-insensitively
    assert owner_client.post(f"{BASE}/artists", json={"name": ""}).status_code == 422
    listing = owner_client.get(f"{BASE}/artists").json()
    assert [a["name"] for a in listing["artists"]] == ["Gojira"] and listing["music_db"] is False
    assert owner_client.patch(f"{BASE}/artists/{aid}", json={"status": "mute"}).status_code == 200
    assert owner_client.get(f"{BASE}/artists").json()["artists"][0]["status"] == "mute"
    assert owner_client.patch(f"{BASE}/artists/{aid}", json={"status": "adore"}).status_code == 422
    assert owner_client.patch(f"{BASE}/artists/99999", json={"status": "pin"}).status_code == 404
    assert owner_client.delete(f"{BASE}/artists/{aid}").status_code == 200
    assert owner_client.delete(f"{BASE}/artists/{aid}").status_code == 404


def test_muting_an_artist_from_a_card(owner_client):
    aid = owner_client.post(f"{BASE}/artists", json={"name": "Ghost"}).json()["id"]
    content.upsert_items("release", "mb", [{"external_id": "1", "artist": "Ghost", "title": "T", "date": "2026-10-01"}], baseline=False)
    assert owner_client.post(f"{BASE}/artists/mute", json={"name": "Ghost"}).status_code == 200
    assert owner_client.get(f"{BASE}/artists").json()["artists"][0]["status"] == "mute"
    assert owner_client.get(f"{BASE}/state").json()["widgets"]["releases"]["data"]["items"] == []      # hidden straight away
    assert owner_client.post(f"{BASE}/artists/mute", json={"name": "Nobody"}).status_code == 404
    assert owner_client.post(f"{BASE}/artists/mute", json={"name": "x"}, headers={"X-Hub": ""}).status_code == 403


def test_resync_needs_music_companion_and_never_undoes_a_mute(owner_client, tmp_path, monkeypatch):
    assert owner_client.post(f"{BASE}/artists/resync").status_code == 404                              # no database at the configured path
    path = tmp_path / "music.sqlite"
    make_music_db(path)
    monkeypatch.setattr(config, "MUSIC_DB", path)
    assert owner_client.post(f"{BASE}/artists/resync").json() == {"found": 4, "added": 4, "updated": 0}
    ghost = next(a for a in owner_client.get(f"{BASE}/artists").json()["artists"] if a["name"] == "Ghost")
    owner_client.patch(f"{BASE}/artists/{ghost['id']}", json={"status": "mute"})
    assert owner_client.post(f"{BASE}/artists/resync").json()["added"] == 0
    assert next(a for a in owner_client.get(f"{BASE}/artists").json()["artists"] if a["name"] == "Ghost")["status"] == "mute"


def test_gig_crud_and_validation(owner_client):
    gid = owner_client.post(f"{BASE}/gigs", json=GIG).json()["id"]
    assert owner_client.get(f"{BASE}/gigs").json()["gigs"][0]["artist"] == "Gojira"
    assert owner_client.put(f"{BASE}/gigs/{gid}", json={**GIG, "venue": "First Direct Arena", "ticket_url": None}).status_code == 200
    got = owner_client.get(f"{BASE}/gigs").json()["gigs"][0]
    assert got["venue"] == "First Direct Arena" and got["ticket_url"] is None
    for bad in ({"date": "2026-13-40"}, {"date": "tomorrow"}, {"doors": "25:00"}, {"doors": "7pm"}, {"ticket_url": "ftp://x"}, {"ticket_url": "javascript:1"}, {"artist": ""}):
        assert owner_client.post(f"{BASE}/gigs", json={**GIG, **bad}).status_code == 422, bad
    assert owner_client.put("/api/owner/gigs/99999", json=GIG).status_code == 404
    assert owner_client.delete(f"{BASE}/gigs/{gid}").status_code == 200
    assert owner_client.delete(f"{BASE}/gigs/{gid}").status_code == 404


def test_booking_a_radar_show_makes_a_gig_and_takes_it_off_the_radar(owner_client):
    content.upsert_items("radar", "ticketmaster", [{"external_id": "tm1", "artist": "Gojira", "title": "Gojira", "date": local_date(30)}], baseline=True)
    iid = content.list_items("radar")["items"][0]["id"]
    for bad in ({"date": "tomorrow"}, {"doors": "7pm"}, {"artist": ""}):           # a refused form changes nothing
        assert owner_client.post(f"{BASE}/radar/{iid}/book", json={**GIG, **bad}).status_code == 422, bad
    assert content.list_gigs() == [] and content.list_items("radar")["total"] == 1

    r = owner_client.post(f"{BASE}/radar/{iid}/book", json={**GIG, "date": local_date(30)})
    assert r.status_code == 201
    gig = content.list_gigs()[0]
    assert gig["id"] == r.json()["id"] and gig["artist"] == "Gojira" and gig["notes"] == "block B" and gig["date"] == local_date(30)
    assert content.list_items("radar")["total"] == 0
    state = owner_client.get(f"{BASE}/state").json()
    assert state["widgets"]["gigs"]["data"]["next"]["artist"] == "Gojira"

    assert owner_client.post(f"{BASE}/radar/{iid}/book", json=GIG).status_code == 409    # booked twice
    assert owner_client.post(f"{BASE}/radar/99999/book", json=GIG).status_code == 404
    content.upsert_items("news", "f", [{"external_id": "n1", "title": "A story", "date": "2026-09-20"}], baseline=False)
    nid = content.list_items("news")["items"][0]["id"]
    assert owner_client.post(f"{BASE}/radar/{nid}/book", json=GIG).status_code == 404      # only radar shows can be booked
    assert len(content.list_gigs()) == 1


def alerts(c):
    return c.get(f"{BASE}/state").json()["alerts"]


def test_a_gig_tomorrow_or_tonight_raises_a_reminder(owner_client):
    assert not any(a["kind"] == "gig" for a in alerts(owner_client))
    owner_client.post(f"{BASE}/gigs", json={**GIG, "date": local_date(3)})
    assert not any(a["kind"] == "gig" for a in alerts(owner_client))                                 # 3 days out: no nag
    with db.db() as con:
        con.execute("DELETE FROM gigs")
    owner_client.post(f"{BASE}/gigs", json={**GIG, "date": local_date(1)})
    a = next(a for a in alerts(owner_client) if a["kind"] == "gig")
    assert a["chip"] == "Tomorrow" and a["title"] == "Gojira" and "O2 Academy, Leeds" in a["detail"] and "doors 18:30" in a["detail"]
    with db.db() as con:
        con.execute("DELETE FROM gigs")
    owner_client.post(f"{BASE}/gigs", json={**GIG, "date": local_date(0)})
    a = next(a for a in alerts(owner_client) if a["kind"] == "gig")
    assert a["chip"] == "Tonight" and a["title"] == "Gojira tonight" and a["level"] == "now"


def test_tv_gets_gigs_only_when_that_panel_is_allowed(owner_client):
    owner_client.post(f"{BASE}/gigs", json={**GIG, "date": local_date(1)})
    put = lambda panels: owner_client.put(f"{BASE}/screens", json={"panels": panels, "rotate_seconds": 12, "show_alerts": True})
    assert put(["bins"]).status_code == 200
    s = owner_client.get("/api/kiosk/state").json()
    assert "gigs" not in s["widgets"] and not any(a["kind"] == "gig" for a in s["alerts"])            # a booked gig is personal: opt-in
    assert put(["bins", "gigs"]).status_code == 200
    s = owner_client.get("/api/kiosk/state").json()
    assert s["widgets"]["gigs"]["data"]["next"]["artist"] == "Gojira" and any(a["kind"] == "gig" for a in s["alerts"])
    assert put(["podcasts"]).status_code == 422


def test_every_content_widget_is_in_the_dashboard_state(owner_client):
    w = owner_client.get(f"{BASE}/state").json()["widgets"]
    assert {"gigs", "releases", "vinyl", "radar", "news"} <= set(w)
    assert w["radar"]["configured"] is False and w["radar"]["data"] is None                          # no Ticketmaster key yet
    assert w["releases"]["data"]["following"] == 0 and w["news"]["data"] == {"feeds": {}, "errors": []}
    assert "items" not in (w["mostread"]["data"] or {})                                            # headlines never ride along in the dashboard state


def test_the_api_keys_are_write_only(owner_client):
    key = "ABCDEF0123456789ABCDEF0123456789"
    r = owner_client.put(f"{BASE}/widgets/radar/settings", json={"api_key": key, "latlong": "53.5,-2.2"})
    assert r.status_code == 200 and key not in r.text and r.json()["has_api_key"] is True
    assert key not in owner_client.get(f"{BASE}/config").text and key not in owner_client.get(f"{BASE}/state").text
    assert owner_client.get(f"{BASE}/state").json()["widgets"]["radar"]["configured"] is True         # it's now considered set up
    assert owner_client.put(f"{BASE}/widgets/radar/settings", json={"api_key": ""}).json()["has_api_key"] is True     # blank = keep
    assert owner_client.put(f"{BASE}/widgets/radar/settings", json={"api_key": "short"}).status_code == 422
