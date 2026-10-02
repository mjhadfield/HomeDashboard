"""Appearance: custom themes and their background pictures."""
import os
import time

import pytest

from app import appearance, db

BASE = "/api/owner/appearance"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 64


@pytest.fixture(autouse=True)
def _clean(client):
    with db.db() as con:
        con.execute("DELETE FROM widget_settings WHERE widget = 'appearance'")
    folder = appearance.bg_dir()
    if folder.exists():
        for f in folder.iterdir():
            f.unlink()
    yield


def theme(**kw):
    return {"name": "Midnight", "base": "dark", "accent": "#3B82F6", "panel": "#0a1020", "panel_opacity": 80,
            "background": {"file": "", "fit": "cover", "dim": 40, "glow": True}, **kw}


def test_a_theme_is_cleaned_and_defaults_come_from_its_base():
    t = appearance.clean({"name": " Sunset ", "base": "light"})
    assert t["name"] == "Sunset" and t["accent"] == appearance.BASE_ACCENT["light"] and t["panel"] == appearance.BASE_PANEL["light"]
    assert t["panel_opacity"] == 100 and t["background"] == {"file": "", "fit": "cover", "dim": 40, "glow": True}
    assert appearance.clean(theme(panel_opacity=5, background={"dim": 400}))["panel_opacity"] == 30          # clamped, not refused
    assert appearance.clean(theme())["accent"] == "#3b82f6"


@pytest.mark.parametrize("bad", [
    {"name": ""}, {"base": "blue"}, {"accent": "red"}, {"accent": "#12345"}, {"panel": "url(x)"},
    {"background": {"fit": "stretch"}}, {"background": {"file": "../hub.sqlite"}}, {"background": {"file": "0" * 32 + ".png"}},   # not uploaded
    {"panel_opacity": "lots"},
])
def test_bad_themes_are_refused(bad):
    with pytest.raises(appearance.AppearanceError):
        appearance.clean(theme(**bad))


def test_create_edit_delete_through_the_api_and_every_screen_sees_them(owner_client):
    r = owner_client.post(f"{BASE}/themes", json=theme())
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert owner_client.get("/api/owner/state").json()["appearance"]["themes"][0]["name"] == "Midnight"
    assert owner_client.get("/api/owner/config").json()["appearance"]["themes"][0]["id"] == tid
    assert owner_client.put(f"{BASE}/themes/{tid}", json=theme(name="Midnight 2")).json()["name"] == "Midnight 2"
    assert owner_client.put(f"{BASE}/themes/{tid}", json=theme(accent="nope")).status_code == 422
    assert owner_client.put(f"{BASE}/themes/nope", json=theme()).status_code == 404
    assert owner_client.delete(f"{BASE}/themes/{tid}").status_code == 200
    assert owner_client.get("/api/owner/state").json()["appearance"]["themes"] == []


def test_guests_cannot_change_themes(guest_client):
    assert guest_client.post(f"{BASE}/themes", json=theme(), headers={"X-Hub": "1"}).status_code == 403


def test_background_upload_checks_the_bytes_and_serves_owner_only_and_sandboxed(owner_client, guest_client):
    r = owner_client.post(f"{BASE}/backgrounds", content=JPG, headers={"Content-Type": "image/jpeg"})
    assert r.status_code == 201, r.text
    name = r.json()["file"]
    assert name.endswith(".jpg")
    # an SVG (could carry script) or a page pretending to be a picture is refused, whatever it claims to be
    assert owner_client.post(f"{BASE}/backgrounds", content=b"<svg onload=alert(1)></svg>", headers={"Content-Type": "image/png"}).status_code == 422
    assert owner_client.post(f"{BASE}/backgrounds", content=b"<html></html>", headers={"Content-Type": "image/jpeg"}).status_code == 422
    assert owner_client.post(f"{BASE}/backgrounds", content=b"", headers={"Content-Type": "image/png"}).status_code == 422
    got = owner_client.get(f"/backgrounds/{name}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg" and "sandbox" in got.headers["content-security-policy"]
    assert guest_client.get(f"/backgrounds/{name}").status_code == 403
    for bad in ("../hub.sqlite", "secret.key", name.upper(), f"{name}.html"):
        assert owner_client.get(f"/backgrounds/{bad}").status_code == 404
    # and a theme can now use it
    t = owner_client.post(f"{BASE}/themes", json=theme(background={"file": name, "fit": "tile", "dim": 25, "glow": False})).json()
    assert t["background"] == {"file": name, "fit": "tile", "dim": 25, "glow": False}


def test_oversized_pictures_are_refused(owner_client, monkeypatch):
    monkeypatch.setattr(appearance, "MAX_BACKGROUND", 100)
    assert owner_client.post(f"{BASE}/backgrounds", content=PNG + b"\x00" * 200, headers={"Content-Type": "image/png"}).status_code == 413


def test_unused_pictures_are_tidied_but_a_fresh_upload_is_spared():
    used, fresh, old = appearance.store_background(PNG), appearance.store_background(JPG), appearance.store_background(PNG + b"\x01")
    appearance.save_theme(theme(background={"file": used}))
    week_ago = time.time() - 7 * 86400
    for n in (used, old):
        os.utime(appearance.bg_dir() / n, (week_ago, week_ago))
    appearance.tidy()
    assert {f.name for f in appearance.bg_dir().iterdir()} == {used, fresh}      # the old unused one went; the in-use and the fresh stayed
