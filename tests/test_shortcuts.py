"""Shortcuts: the row of web links across the top of the dashboard, and their icons."""
import asyncio

import httpx
import pytest

from app import config, db
from app.widgets import shortcuts as sc
from tests.test_content import patch_http

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
ICO = b"\x00\x00\x01\x00" + b"\x01" * 40
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'


@pytest.fixture(autouse=True)
def _clean(client):
    with db.db() as con:
        con.execute("DELETE FROM widget_settings WHERE widget = 'shortcuts'")
        con.execute("DELETE FROM widget_cache WHERE widget = 'shortcuts'") if con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'widget_cache'").fetchone() else None
    for f in sc.icon_dir().glob("*") if sc.icon_dir().exists() else []:
        f.unlink()
    yield


def run(coro):
    return asyncio.run(coro)


def settings(*items, show=True):
    return sc.widget.merge_settings(sc.widget.defaults, {"show": show, "shortcuts": list(items)})


# ---- settings ----------------------------------------------------------------------------------------------------

def test_settings_are_cleaned_and_get_stable_ids():
    s = settings({"label": " Gmail ", "url": "https://mail.google.com/"}, {"url": "http://192.168.0.1/", "icon_mode": "builtin", "icon": "router"})
    a, b = s["shortcuts"]
    assert a["label"] == "Gmail" and a["icon_mode"] == "site" and a["image"] == "" and len(a["id"]) == 12
    assert b["label"] == "" and b["icon"] == "router"
    again = sc.widget.merge_settings(s, {"shortcuts": s["shortcuts"]})
    assert [x["id"] for x in again["shortcuts"]] == [a["id"], b["id"]]          # ids survive a re-save


@pytest.mark.parametrize("bad", [
    {"url": ""}, {"url": "javascript:alert(1)"}, {"url": "ftp://x.example"}, {"url": "https://x.example", "icon_mode": "emoji"},
    {"url": "https://x.example", "icon_mode": "builtin", "icon": "not-an-icon"}, {"url": "https://x.example", "icon_mode": "image"},
    {"url": "https://x.example", "icon_mode": "image", "image": "data:image/png;base64,AAAA"},
])
def test_bad_shortcuts_are_refused(bad):
    with pytest.raises(ValueError):
        settings(bad)


def test_at_most_thirty_and_show_must_be_a_bool():
    with pytest.raises(ValueError):
        settings(*[{"url": f"https://{i}.example"} for i in range(31)])
    with pytest.raises(ValueError):
        sc.widget.merge_settings(sc.widget.defaults, {"show": "yes"})


# ---- finding a site's icon ---------------------------------------------------------------------------------------

def test_icon_candidates_best_first():
    page = """<html><head><base href="/app/">
      <link rel="icon" href="/favicon-16.png" sizes="16x16">
      <link rel="mask-icon" href="/pin.svg">
      <link rel="icon" type="image/svg+xml" href="icon.svg">
      <link rel="apple-touch-icon" sizes="180x180" href="https://cdn.example/touch.png">
      <link rel="icon" href="data:image/png;base64,AAAA">
    </head><body><link rel="icon" href="/late.png"></body></html>"""
    assert sc.icon_candidates(page, "https://site.example/start") == [
        "https://cdn.example/touch.png", "https://site.example/app/icon.svg", "https://site.example/favicon-16.png",
        "https://site.example/late.png", "https://site.example/favicon.ico"]
    assert sc.icon_candidates("", "http://192.168.0.1/admin") == ["http://192.168.0.1/favicon.ico"]       # no page: still try the classic


def test_sniff_trusts_the_bytes_not_the_header():
    assert sc.sniff(PNG, "application/octet-stream") == "png" and sc.sniff(ICO, "") == "ico" and sc.sniff(SVG, "text/plain") == "svg"
    assert sc.sniff(b"<html>not found</html>", "text/html") is None
    assert sc.sniff(b"??", "image/webp") == "webp"


# ---- fetching and serving ----------------------------------------------------------------------------------------

def routes(**extra):
    base = {
        "https://site.example/favicon.ico": httpx.Response(200, content=ICO),          # (matched by prefix: specific addresses first)
        "https://site.example/touch.png": httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
        "https://site.example/": httpx.Response(200, text='<head><link rel="apple-touch-icon" href="/touch.png"></head>'),
        "https://bare.example/favicon.ico": httpx.Response(200, content=ICO),
        "https://logos.example/mine.svg": httpx.Response(200, content=SVG),
        "https://huge.example/": httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * (sc.MAX_IMAGE + 10)),
    }
    base.update(extra)
    return base


def test_fetch_downloads_icons_and_view_points_at_them(monkeypatch):
    patch_http(monkeypatch, sc, routes())
    s = settings({"label": "Site", "url": "https://site.example/"}, {"label": "Bare", "url": "https://bare.example/page"},
                 {"label": "Logo", "url": "https://x.example/", "icon_mode": "image", "image": "https://logos.example/mine.svg"},
                 {"label": "Router", "url": "http://192.168.0.1/", "icon_mode": "builtin", "icon": "router"},
                 {"label": "Nothing", "url": "https://gone.example/"})
    data = run(sc.widget.fetch(s))
    ids = [x["id"] for x in s["shortcuts"]]
    assert set(data["icons"]) == {ids[0], ids[1], ids[2]} and data["failed"] == ["Nothing"]
    files = {p.name for p in sc.icon_dir().iterdir()}
    assert data["icons"][ids[0]]["file"].endswith(".png") and data["icons"][ids[1]]["file"].endswith(".ico") and data["icons"][ids[2]]["file"].endswith(".svg")
    assert files == {v["file"] for v in data["icons"].values()}
    v = sc.widget.view(data, s, None)
    assert v["show"] and [x["img"] is not None for x in v["shortcuts"]] == [True, True, True, False, False]
    assert v["shortcuts"][0]["img"] == f"/shortcut-icons/{data['icons'][ids[0]]['file']}"


def test_a_failed_refresh_keeps_the_last_good_icon_and_a_changed_address_drops_it(monkeypatch):
    s = settings({"label": "Site", "url": "https://site.example/"})
    patch_http(monkeypatch, sc, routes())
    first = run(sc.widget.fetch(s))
    monkeypatch.setattr(sc.db, "cache_get", lambda w: {"data": first})
    patch_http(monkeypatch, sc, {})                                         # the site is down
    again = run(sc.widget.fetch(s))
    assert again["icons"] == first["icons"] and again["failed"] == ["Site"]
    moved = sc.widget.merge_settings(s, {"shortcuts": [{**s["shortcuts"][0], "url": "https://elsewhere.example/"}]})
    assert sc.widget.view(again, moved, None)["shortcuts"][0]["img"] is None   # an old icon isn't shown for a new address
    assert run(sc.widget.fetch(moved))["icons"] == {} and not any(sc.icon_dir().iterdir())   # …and its file is tidied away


def test_oversized_files_are_not_icons(monkeypatch):
    patch_http(monkeypatch, sc, routes())
    s = settings({"label": "Huge", "url": "https://x.example/", "icon_mode": "image", "image": "https://huge.example/"})
    assert run(sc.widget.fetch(s))["failed"] == ["Huge"]


def test_icons_are_served_owner_only_and_sandboxed(owner_client, guest_client, monkeypatch):
    patch_http(monkeypatch, sc, routes())
    s = settings({"label": "Logo", "url": "https://x.example/", "icon_mode": "image", "image": "https://logos.example/mine.svg"})
    name = run(sc.widget.fetch(s))["icons"][s["shortcuts"][0]["id"]]["file"]
    r = owner_client.get(f"/shortcut-icons/{name}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and "sandbox" in r.headers["content-security-policy"]
    assert guest_client.get(f"/shortcut-icons/{name}").status_code == 403
    for bad in ("../hub.sqlite", "secret.key", "ABC.png", f"{name}.html"):
        assert owner_client.get(f"/shortcut-icons/{bad}").status_code == 404


def test_settings_round_trip_through_the_api_and_reach_the_dashboard(owner_client):
    r = owner_client.put("/api/owner/widgets/shortcuts/settings", json={"show": True, "shortcuts": [
        {"label": "Router", "url": "http://192.168.0.1/", "icon_mode": "builtin", "icon": "router"}]})
    assert r.status_code == 200, r.text
    w = owner_client.get("/api/owner/state").json()["widgets"]["shortcuts"]["data"]
    assert w["show"] and w["shortcuts"][0]["label"] == "Router" and w["shortcuts"][0]["icon"] == "router"
    assert owner_client.put("/api/owner/widgets/shortcuts/settings", json={"shortcuts": [{"url": "javascript:x"}]}).status_code == 422
    owner_client.put("/api/owner/widgets/shortcuts/settings", json={"show": False})
    assert owner_client.get("/api/owner/state").json()["widgets"]["shortcuts"]["data"]["show"] is False
