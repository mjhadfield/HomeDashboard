"""The reminder strip: dismissing (household-wide, one occurrence at a time) and the newer kinds of reminder."""
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app import alerts, config, content, db, state
from app.monitor import memory_tight
from app.widgets import f1cal, radar

BASE = "/api/owner"
AT = datetime(2026, 10, 29, 9, 0, tzinfo=config.TZ)          # a Thursday morning


@pytest.fixture(autouse=True)
def _clean(client):
    def wipe():
        with db.db() as con:
            for t in ("alert_dismissals", "feed_items", "followed_artists"):
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM widget_settings WHERE widget IN ('alerts', 'tv', 'bins')")
            con.execute("DELETE FROM widget_cache WHERE widget = 'f1cal'")
    wipe()
    yield
    wipe()


def day(n, at=AT):
    return (at.date() + timedelta(days=n)).isoformat()


def bins(stream, in_days, at=AT):
    return {"bins": {"data": {"next": {"recycling": {"stream": stream, "date": day(in_days, at), "in_days": in_days}, "domestic": None}}}}


# ---- dismissing ----------------------------------------------------------------------------------

FAKE = [{"kind": "fresh", "key": "fresh:x", "level": "info", "title": "C", "detail": "", "chip": "New", "icon": "star"},
        {"kind": "bin", "key": "bin:blue:2026-10-29", "level": "now", "title": "A", "detail": "", "chip": "Today", "icon": "bin"},
        {"kind": "gig", "key": "gig:1:2026-10-30", "level": "soon", "title": "B", "detail": "", "chip": "Tomorrow", "icon": "bolt"}]


def test_dismissing_hides_a_reminder_everywhere_until_undone(monkeypatch, owner_client):
    monkeypatch.setattr(state, "compute_alerts", lambda widgets, at: [dict(a) for a in FAKE])
    owner_client.put(f"{BASE}/screens", json={"panels": ["bins", "gigs"], "rotate_seconds": 12, "show_alerts": True})
    owner = lambda: [a["key"] for a in owner_client.get(f"{BASE}/state").json()["alerts"] if a["kind"] != "maintenance"]
    tv = lambda: [a["key"] for a in owner_client.get("/api/kiosk/state").json()["alerts"]]
    assert owner() == ["bin:blue:2026-10-29", "gig:1:2026-10-30", "fresh:x"]          # most urgent first
    assert owner_client.post(f"{BASE}/alerts/dismiss", json={"key": "bin:blue:2026-10-29"}).json() == {"ok": True}
    assert owner() == ["gig:1:2026-10-30", "fresh:x"] and "bin:blue:2026-10-29" not in tv()
    assert owner_client.post(f"{BASE}/alerts/dismiss", json={"key": "bin:blue:2026-10-29"}).status_code == 200   # twice is harmless
    owner_client.post(f"{BASE}/alerts/restore", json={"key": "bin:blue:2026-10-29"})
    assert owner()[0] == "bin:blue:2026-10-29" and "bin:blue:2026-10-29" in tv()
    assert owner_client.post(f"{BASE}/alerts/dismiss", json={"key": ""}).status_code == 422


def test_guests_cannot_dismiss(guest_client):
    for path in ("/alerts/dismiss", "/alerts/restore"):
        assert guest_client.post(BASE + path, json={"key": "bin:x"}, headers={"X-Hub": "1"}).status_code == 403


def test_put_out_and_collection_day_share_a_key_and_next_week_comes_back():
    evening = AT.replace(hour=19) - timedelta(days=1)
    tonight = state.compute_alerts(bins("blue", 1, evening), evening)
    morning = state.compute_alerts(bins("blue", 0), AT)
    assert tonight[0]["key"] == morning[0]["key"] == f"bin:blue:{day(0)}"
    alerts.dismiss(morning[0]["key"])
    assert alerts.visible(morning) == []
    next_week = state.compute_alerts(bins("brown", 0, AT + timedelta(days=7)), AT + timedelta(days=7))
    assert alerts.visible(next_week)[0]["key"] == f"bin:brown:{day(7)}"


def test_old_dismissals_are_tidied():
    alerts.dismiss("old")
    alerts.dismiss("recent")
    with db.db() as con:
        con.execute("UPDATE alert_dismissals SET dismissed_at = ? WHERE key = 'old'", (int(time.time()) - 50 * 86400,))
    assert alerts.prune() == 1 and alerts.dismissed() == {"recent"}


# ---- album releases ------------------------------------------------------------------------------

def releases(*items):
    return {"releases": {"data": {"items": list(items)}}}


def test_album_reminders_show_the_day_before_and_the_day_only():
    rel = lambda d: {"id": 7, "artist": "Queens Of The Stone Age", "title": "Perfecth", "date": d, "url": "https://musicbrainz.org/release-group/x", "meta": {"type": "Album"}}
    got = lambda d: [a for a in state.compute_alerts(releases(rel(d)), AT) if a["kind"] == "release"]
    tomorrow, today = got(day(1)), got(day(0))
    assert tomorrow[0]["chip"] == "Tomorrow" and tomorrow[0]["level"] == "soon" and tomorrow[0]["title"] == "Queens Of The Stone Age — Perfecth"
    assert today[0]["chip"] == "Out today" and today[0]["level"] == "now" and today[0]["key"] == "release:7" and today[0]["done"] == "Listened"
    assert got(day(-1)) == [] and got(day(2)) == [] and got("2026-10") == []           # gone after the day; month-only dates never count


def test_switching_a_kind_off_in_settings(owner_client):
    rel = {"id": 7, "artist": "A", "title": "B", "date": day(0), "url": None, "meta": {}}
    assert owner_client.put(f"{BASE}/alerts/settings", json={**alerts.DEFAULTS, "releases": False}).json()["releases"] is False
    assert not any(a["kind"] == "release" for a in state.compute_alerts(releases(rel), AT))
    assert owner_client.get(f"{BASE}/config").json()["alerts"]["releases"] is False
    assert owner_client.put(f"{BASE}/alerts/settings", json={**alerts.DEFAULTS, "top_artists": 0}).status_code == 422


# ---- F1 ------------------------------------------------------------------------------------------

RACES = [{"season": "2026", "round": "21", "raceName": "São Paulo Grand Prix", "date": "2026-11-08", "time": "17:00:00Z",
          "Circuit": {"circuitName": "Interlagos", "Location": {"locality": "São Paulo"}},
          "FirstPractice": {"date": "2026-11-06", "time": "14:30:00Z"}, "Qualifying": {"date": "2026-11-07", "time": "18:00:00Z"},
          "Sprint": {"date": "2026-11-07", "time": "14:00:00Z"}, "SprintQualifying": {"date": "2026-11-06", "time": "18:30:00Z"}}]


def test_f1_calendar_keeps_every_competitive_session_but_not_practice():
    r = f1cal.parse_races(RACES)[0]
    assert [s["key"] for s in r["sessions"]] == ["SprintQualifying", "Sprint", "Qualifying", "Race"]


def test_f1_reminder_from_three_hours_before_until_the_session_is_over():
    db.cache_put("f1cal", {"season": 2026, "rounds": f1cal.parse_races(RACES)})
    race = datetime.fromtimestamp(f1cal.parse_races(RACES)[0]["race"], config.TZ)
    f1 = lambda at: [a for a in state.compute_alerts({}, at) if a["kind"] == "f1"]
    assert f1(race - timedelta(hours=3, minutes=1)) == []
    soon = f1(race - timedelta(hours=2, minutes=10))
    assert soon[0]["title"] == "São Paulo GP — Race" and soon[0]["chip"] == "in 2h 10m" and soon[0]["key"] == "f1:2026:21:Race"
    assert f1(race + timedelta(minutes=30))[0]["chip"] == "Live"
    assert f1(race + timedelta(hours=2, minutes=1)) == []
    assert f1(race + timedelta(days=30)) == []


def test_old_f1_cache_without_sessions_is_quietly_ignored():
    db.cache_put("f1cal", {"season": 2026, "rounds": [{"round": 1, "short": "X GP"}]})
    assert not any(a["kind"] == "f1" for a in state.compute_alerts({}, AT))


# ---- ticket on-sales -----------------------------------------------------------------------------

def tm_event(**sales):
    return {"_embedded": {"events": [{"id": "e1", "name": "Trivium", "url": "https://www.ticketmaster.co.uk/e1", "sales": sales,
                                      "dates": {"start": {"localDate": "2027-03-14", "localTime": "19:00:00"}, "status": {"code": "onsale"}},
                                      "_embedded": {"venues": [{"name": "Academy", "city": {"name": "Manchester"}}], "attractions": [{"name": "Trivium"}]}}]}}


def test_ticketmaster_on_sale_times_are_kept():
    meta = radar.parse_ticketmaster(tm_event(public={"startDateTime": "2026-10-30T09:00:00Z"},
                                             presales=[{"name": "Artist presale", "startDateTime": "2026-10-29T09:00:00Z"}, {"name": "No time"}]), "Trivium")[0]["meta"]
    assert meta["onsale"] == "2026-10-30T09:00:00Z" and meta["presales"] == [{"at": "2026-10-29T09:00:00Z", "name": "Artist presale"}]
    tbd = radar.parse_ticketmaster(tm_event(public={"startDateTime": "1900-01-01T00:00:00Z", "startTBD": True}), "Trivium")[0]["meta"]
    assert tbd["onsale"] is None and tbd["presales"] == []
    assert radar.parse_ticketmaster(tm_event(), "Trivium")[0]["meta"]["onsale"] is None


def test_on_sale_reminder_covers_today_and_tomorrow():
    gig = lambda onsale, presales=(), tribute=False: {"radar": {"data": {"items": [{"id": 3, "artist": "Trivium", "title": "Trivium", "subtitle": "Academy, Manchester",
        "date": "2027-03-14", "url": "https://www.ticketmaster.co.uk/e1", "meta": {"venue": "Academy", "onsale": onsale, "presales": list(presales), "tribute": tribute}}]}}}
    got = lambda w, at=AT: [a for a in state.compute_alerts(w, at) if a["kind"] == "onsale"]
    tomorrow_ten = (AT + timedelta(days=1)).replace(hour=10).astimezone(config.TZ).isoformat()
    a = got(gig(tomorrow_ten))[0]
    assert a["title"] == "Trivium tickets on sale" and a["chip"] == "Tomorrow 10:00" and a["detail"] == "Academy · Sun 14 Mar 2027"
    assert a["href"] == "https://www.ticketmaster.co.uk/e1" and a["level"] == "soon"
    assert got(gig(tomorrow_ten), AT.replace(hour=9, minute=59) + timedelta(days=1))[0]["chip"] == "in 1m"
    assert got(gig(tomorrow_ten), AT + timedelta(days=1, hours=2)) == []                 # on sale now: nothing to remind
    assert got(gig((AT + timedelta(days=2)).isoformat())) == []                         # not yet
    assert got(gig(tomorrow_ten, tribute=True)) == []
    pre = got(gig(None, [{"at": (AT + timedelta(hours=3)).isoformat(), "name": "Artist presale"}]))[0]
    assert pre["title"] == "Trivium — Artist presale" and pre["chip"] == "Today 12:00" and pre["level"] == "now"
    assert got(gig("not a date")) == []


# ---- fresh announcements -------------------------------------------------------------------------

def test_fresh_announcements_group_per_kind_and_follow_the_card():
    for name, weight in (("Trivium", 9), ("Ghost", 8), ("Nobody", 0)):
        content.add_artist(name)
        with db.db() as con:
            con.execute("UPDATE followed_artists SET weight = ? WHERE name = ?", (weight, name))
    content.upsert_items("radar", "tm", [{"external_id": "1", "artist": "Trivium", "title": "Trivium live", "date": "2027-03-14"},
                                          {"external_id": "2", "artist": "Ghost", "title": "Ghost live", "date": "2027-04-01"},
                                          {"external_id": "3", "artist": "Nobody", "title": "Nobody live", "date": "2027-04-02"}], baseline=False)
    content.upsert_items("release", "mb", [{"external_id": "r", "artist": "Ghost", "title": "Skeletá II", "date": "2027-01-10"}], baseline=False)
    widgets = lambda: {k: {"data": content.list_items(k)} for k in ("radar", "release", "vinyl")}
    fresh = lambda top=2: {a["icon"]: a for a in state._fresh_alerts(widgets(), datetime.now(config.TZ), top)}

    got = fresh()
    assert got["radar"]["title"] == "2 new gigs on your radar" and got["radar"]["detail"] in ("Trivium, Ghost", "Ghost, Trivium")
    assert got["star"]["title"] == "Ghost — Skeletá II" and got["star"]["detail"].startswith("New album announced")
    assert "Nobody" not in got["radar"]["detail"] and "disc" not in got                  # not a top artist; no new vinyl at all
    assert fresh(top=1)["radar"]["title"] == "Trivium — Trivium live"                   # only the heaviest artist counts now

    gig = next(i for i in content.list_items("radar")["items"] if i["artist"] == "Ghost")
    content.set_status(gig["id"], "seen")                                              # cleared on the card -> gone here too
    assert fresh()["radar"]["title"] == "Trivium — Trivium live"
    with db.db() as con:
        con.execute("UPDATE feed_items SET first_seen = first_seen - 2 * 86400")
    assert fresh() == {}                                                                 # a day later it's not news


# ---- something's down ----------------------------------------------------------------------------

def test_a_running_project_or_a_link_down_for_ten_minutes():
    t = AT.timestamp()
    health = SimpleNamespace(state={
        "project:odysseus": {"ok": False, "down_since": int(t - 900)},
        "project:stopped": {"ok": False, "down_since": int(t - 900)},
        "link:4": {"ok": False, "down_since": int(t - 300)},
        "link:5": {"ok": True, "down_since": None}})
    projects = [{"slug": "odysseus", "name": "Odysseus", "managed": True, "kind": "web"},
                {"slug": "stopped", "name": "Stopped one", "managed": False, "kind": "web"}]
    links = [{"id": 4, "name": "Jellyfin"}, {"id": 5, "name": "Companion"}]
    got = [a for a in state.service_alerts(projects, links, health, None, AT) if a["kind"] == "down"]
    assert [a["title"] for a in got] == ["Odysseus isn't answering"] and got[0]["detail"] == "down since 08:45"
    health.state["link:4"]["down_since"] = int(t - 700)
    assert [a["title"] for a in state.service_alerts(projects, links, health, None, AT) if a["kind"] == "down"] == ["Odysseus isn't answering", "Jellyfin isn't answering"]


def test_health_remembers_when_a_run_of_failures_began(monkeypatch):
    from app.health import Health
    h = Health(bus=SimpleNamespace(publish=lambda *a: None))
    clock = iter([100, 130, 160, 190])
    monkeypatch.setattr("app.health.time.time", lambda: next(clock))
    h._record("x", False, 0); h._record("x", False, 0)
    assert h.state["x"]["down_since"] == 100
    h._record("x", True, 5)
    assert h.state["x"]["down_since"] is None
    h._record("x", False, 0)
    assert h.state["x"]["down_since"] == 190


def test_memory_is_tight_only_when_ram_is_low_and_swap_is_filling():
    gb = 1024**3
    mem = lambda avail, swap_used: {"total": 32 * gb, "available": avail * gb, "swap_total": 16 * gb, "swap_used": swap_used * gb}
    assert memory_tight(mem(2, 12)) and not memory_tight(mem(2, 4)) and not memory_tight(mem(8, 12))
    assert not memory_tight({"total": 32 * gb, "available": gb, "swap_total": 0, "swap_used": 0})     # no swap: not this alert's business
    monitor = SimpleNamespace(mem_tight_since=AT.timestamp() - 200, latest={"mem": mem(2, 12)})
    sysalerts = [a for a in state.service_alerts([], [], SimpleNamespace(state={}), monitor, AT) if a["key"].startswith("sys:mem")]
    assert sysalerts[0]["detail"] == "2.0 GB RAM free · swap 75% full"
