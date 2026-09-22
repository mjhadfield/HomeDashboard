"""Content cards: parsers (against fixtures shaped like the real APIs), curation rules, seeding, and the fetch loops."""
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

REAL_SLEEP = asyncio.sleep
REAL_CLIENT = httpx.AsyncClient          # captured once: patching must never wrap an already-patched client

from app import config, content, coverart, db, musicbrainz
from app.widgets import gigs, news, radar, releases, vinyl

TODAY = "2026-09-21"
NOW = datetime(2026, 9, 21, 12, tzinfo=config.TZ)


@pytest.fixture(autouse=True)
def _clean(client):
    def wipe():
        with db.db() as con:
            for t in ("feed_items", "followed_artists", "gigs"):
                con.execute(f"DELETE FROM {t}")
    wipe()
    yield
    wipe()


def run(coro):
    return asyncio.run(coro)


def age_checks(hours=2):
    """Pretend every artist was last checked `hours` ago (a 'Sync now' skips anyone looked at within the last hour)."""
    with db.db() as con:
        con.execute("UPDATE followed_artists SET mb_checked_at = mb_checked_at - ?, radar_checked_at = radar_checked_at - ?", (hours * 3600, hours * 3600))


def checked_at(column, name=None):
    with db.db() as con:
        row = con.execute(f"SELECT {column} FROM followed_artists" + (" WHERE name = ?" if name else " LIMIT 1"), (name,) if name else ()).fetchone()
    return row[0]


# =============================================================== parsers

RG = {"count": 7, "release-groups": [
    {"id": "rg-album", "title": "Sample Album", "primary-type": "Album", "secondary-types": [], "first-release-date": "2026-09-25"},
    {"id": "rg-live", "title": "Sample Live", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2026-10-16"},
    {"id": "rg-remix", "title": "Remixed", "primary-type": "Album", "secondary-types": ["Remix"], "first-release-date": "2026-09-30"},
    {"id": "rg-old", "title": "Old One", "primary-type": "Album", "secondary-types": [], "first-release-date": "2019-01-01"},
    {"id": "rg-nodate", "title": "No Date", "primary-type": "Single", "secondary-types": []},
    {"id": "rg-radio", "title": "Radio thing", "primary-type": "Broadcast", "secondary-types": [], "first-release-date": "2026-09-01"},
    {"id": "rg-ep", "title": "Partial EP", "primary-type": "EP", "secondary-types": [], "first-release-date": "2026-11"}]}


def test_release_groups_are_filtered_and_labelled():
    everything = ["Album", "EP", "Single"]
    items = releases.parse_release_groups(RG, "Gojira", cutoff="2026-05", horizon="2027-09-21", types=everything)
    assert [i["external_id"] for i in items] == ["rg-album", "rg-ep"]                     # live albums are out unless asked for; remix, old, undated, broadcast always
    by = {i["external_id"]: i for i in items}
    assert by["rg-album"]["subtitle"] == "Album" and by["rg-ep"]["date"] == "2026-11"
    assert by["rg-album"]["url"] == "https://musicbrainz.org/release-group/rg-album" and by["rg-album"]["artist"] == "Gojira"
    with_extras = releases.parse_release_groups(RG, "G", cutoff="2026-05", horizon="2027-09-21", types=everything, include_extras=True)
    assert [i["external_id"] for i in with_extras] == ["rg-album", "rg-live", "rg-ep"] and {i["external_id"]: i["subtitle"] for i in with_extras}["rg-live"] == "Live album"
    assert [i["external_id"] for i in releases.parse_release_groups(RG, "G", cutoff="2026-05", horizon="2027-09-21", types=["EP"])] == ["rg-ep"]


def test_only_studio_albums_by_default_across_every_kind_of_noise():
    """The complaint: singles, compilations, live albums and soundtracks cluttered Releases. Default = plain albums."""
    noise = {"release-groups": [
        {"id": "a", "title": "Real Album", "primary-type": "Album", "secondary-types": [], "first-release-date": "2026-10-01"},
        {"id": "b", "title": "A Single", "primary-type": "Single", "secondary-types": [], "first-release-date": "2026-10-01"},
        {"id": "c", "title": "Greatest Hits", "primary-type": "Album", "secondary-types": ["Compilation"], "first-release-date": "2026-10-01"},
        {"id": "d", "title": "Live In Paris", "primary-type": "Album", "secondary-types": ["Live"], "first-release-date": "2026-10-01"},
        {"id": "e", "title": "The Film", "primary-type": "Album", "secondary-types": ["Soundtrack"], "first-release-date": "2026-10-01"},
        {"id": "f", "title": "Live Single", "primary-type": "Single", "secondary-types": ["Live"], "first-release-date": "2026-10-01"},
        {"id": "g", "title": "An EP", "primary-type": "EP", "secondary-types": [], "first-release-date": "2026-10-01"}]}
    got = releases.parse_release_groups(noise, "X", cutoff="2026-01", horizon="2027-09-21", types=releases.ReleasesWidget.defaults["types"], include_extras=releases.ReleasesWidget.defaults["include_extras"])
    assert [i["title"] for i in got] == ["Real Album"]


def test_narrowing_the_filter_takes_effect_immediately_without_a_refetch():
    content.upsert_items("release", "musicbrainz", [
        {"external_id": "1", "artist": "X", "title": "Album", "date": "2026-10-01", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "2", "artist": "X", "title": "Live", "date": "2026-10-02", "meta": {"type": "Album", "secondary": ["Live"]}},
        {"external_id": "3", "artist": "X", "title": "Single", "date": "2026-10-03", "meta": {"type": "Single", "secondary": []}}], baseline=True)
    view = lambda **kw: [i["title"] for i in releases.widget.view(None, {"types": kw.get("types", ["Album"]), "include_extras": kw.get("extras", False), "horizon_days": 365, "lookback_days": 31}, NOW)["items"]]
    assert view() == ["Album"]                                            # what's stored from before the filter changed is hidden at once
    assert sorted(view(types=["Album", "Single"])) == ["Album", "Single"]
    assert sorted(view(extras=True)) == ["Album", "Live"]
    assert releases.widget.view(None, {"types": ["Album"], "include_extras": False, "horizon_days": 365, "lookback_days": 31}, NOW)["total"] == 1


def test_releases_look_a_year_ahead_and_ignore_placeholder_dates_further_out():
    rgs = {"release-groups": [
        {"id": "soon", "title": "Soon", "primary-type": "Album", "secondary-types": [], "first-release-date": "2026-10-30"},
        {"id": "next-year", "title": "Next Year", "primary-type": "Album", "secondary-types": [], "first-release-date": "2027-08-01"},
        {"id": "placeholder", "title": "Placeholder", "primary-type": "Album", "secondary-types": [], "first-release-date": "2030-01-01"},
        {"id": "year-only-far", "title": "Someday", "primary-type": "Album", "secondary-types": [], "first-release-date": "2028"},
        {"id": "year-only-near", "title": "This Year", "primary-type": "Album", "secondary-types": [], "first-release-date": "2027"}]}
    got = releases.parse_release_groups(rgs, "X", cutoff="2026-05", horizon="2027-09-21", types=["Album"])
    assert [i["external_id"] for i in got] == ["soon", "next-year", "year-only-near"]        # '2027' could be January, so it is kept; '2028' is too far
    assert releases.earliest("2027") == "2027-01-01" and releases.earliest("2027-03") == "2027-03-01" and releases.earliest("2027-03-09") == "2027-03-09"


def test_releases_ask_musicbrainz_only_for_the_wanted_types_inside_the_window():
    q = releases.search_query("abc-123", "2026-05", "2027-09-21", ["Album", "EP"])
    assert q == "arid:abc-123 AND primarytype:(album OR ep) AND firstreleasedate:[2026-05 TO 2027-09-21]"


def test_horizon_is_enforced_when_reading_too_so_shrinking_it_takes_effect_at_once():
    content.upsert_items("release", "musicbrainz", [
        {"external_id": "1", "artist": "X", "title": "Near", "date": "2026-11-01", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "2", "artist": "X", "title": "Far", "date": "2027-06-01", "meta": {"type": "Album", "secondary": []}}], baseline=True)
    view = lambda days: [i["title"] for i in releases.widget.view(None, {"types": ["Album"], "include_extras": False, "horizon_days": days, "lookback_days": 31}, NOW)["items"]]
    assert view(365) == ["Near", "Far"] and view(90) == ["Near"]


def test_releases_settings_validate_the_horizon():
    assert releases.widget.merge_settings({}, {"horizon_days": 180})["horizon_days"] == 180
    for bad in (0, 10, 5000, "365", 1.5):
        with pytest.raises(ValueError):
            releases.widget.merge_settings({}, {"horizon_days": bad})


TM = {"_embedded": {"events": [
    {"id": "e1", "name": "Ghost - Skeletá Tour", "url": "https://www.ticketmaster.co.uk/e1", "dates": {"start": {"localDate": "2026-12-03", "localTime": "19:00:00"}, "status": {"code": "onsale"}},
     "_embedded": {"venues": [{"name": "AO Arena", "city": {"name": "Manchester"}}], "attractions": [{"name": "Ghost"}]}},
    {"id": "e2", "name": "Ghostly Tribute Night", "url": "https://www.ticketmaster.co.uk/e2", "dates": {"start": {"localDate": "2026-11-01"}, "status": {"code": "onsale"}},
     "_embedded": {"venues": [{"name": "Pub", "city": {"name": "Leeds"}}], "attractions": [{"name": "Tribute Band"}]}},
    {"id": "e3", "name": "Ghost", "url": "https://www.ticketmaster.co.uk/e3", "dates": {"start": {"localDate": "2026-12-04"}, "status": {"code": "cancelled"}}, "_embedded": {"venues": [{}]}},
    {"id": "e4", "name": "Ghost live", "url": "javascript:alert(1)", "dates": {"start": {"localDate": "2026-12-05"}, "status": {"code": "onsale"}}},
    {"id": "e5", "name": "Support acts", "url": "https://www.ticketmaster.co.uk/e5", "dates": {"start": {"localDate": "2026-12-06"}, "status": {"code": "onsale"}},
     "_embedded": {"attractions": [{"name": "Openers"}, {"name": "GHOST"}], "venues": [{"name": "Academy", "city": {"name": "Sheffield"}}]}}]}}


def test_ticketmaster_keeps_only_real_matches():
    items = radar.parse_ticketmaster(TM, "Ghost")
    assert [i["external_id"] for i in items] == ["e1", "e5"]           # 'Ghostly Tribute' fuzzy hit, cancelled and javascript: link all rejected
    e1 = items[0]
    assert e1["date"] == "2026-12-03" and e1["subtitle"] == "AO Arena, Manchester" and e1["meta"]["time"] == "19:00"
    assert radar.parse_ticketmaster({}, "Ghost") == [] and radar.parse_ticketmaster({"_embedded": {}}, "Ghost") == []


def _tm(*names):
    return {"_embedded": {"events": [
        {"id": f"p{i}", "name": n, "url": f"https://www.ticketmaster.co.uk/p{i}", "dates": {"start": {"localDate": "2027-01-23"}, "status": {"code": "onsale"}},
         "_embedded": {"venues": [{"name": "Co-op Live", "city": {"name": "Manchester"}}], "attractions": [{"name": "Electric Callboy"}]}} for i, n in enumerate(names)]}}


def test_premium_packages_are_dropped_but_merch_packages_and_normal_shows_stay():
    names = ["Electric Callboy", "Electric Callboy - VIP Guided Tour Experience", "Electric Callboy - Premium Packages", "Electric Callboy - Venue Premium Tickets",
             "Electric Callboy - VIP Merch Package", "Electric Callboy Hospitality", "Electric Callboy - Meet & Greet", "Electric Callboy + T-Shirt Bundle",
             "Electric Callboy - Platinum Upgrade", "Electric Callboy - Soundcheck Party", "Electric Callboy - Lose Yourself Tour 2027"]
    kept = [i["title"] for i in radar.parse_ticketmaster(_tm(*names), "Electric Callboy")]
    assert kept == ["Electric Callboy", "Electric Callboy - VIP Merch Package", "Electric Callboy + T-Shirt Bundle", "Electric Callboy - Lose Yourself Tour 2027"]


def test_premium_words_only_count_as_whole_words():
    for ok in ("Vipers of Doom", "Premiumtown Rock Night", "The Jimi Hendrix Experience", "Ghost - Skeletour", "Sabaton - The Legendary Tour"):
        assert not radar.is_premium_package(ok), ok
    for bad in ("Ghost - VIP Package", "GHOST: PREMIUM TICKETS", "Ghost Hospitality", "Ghost - Meet and Greet Upgrade", "Ghost Fast Lane"):
        assert radar.is_premium_package(bad), bad


TRIBUTE_CLASS = [{"subType": {"name": "Tribute Band"}, "type": {"name": "Group"}}]


def test_tribute_acts_are_flagged_by_ticketmasters_own_classification():
    """Real Ticketmaster data: 'Rumours of Fleetwood Mac' at The Lowry carries a 'Tribute Band' subType but the event
    name itself says nothing about it — text matching alone would miss it."""
    ev = {"name": "Rumours of Fleetwood Mac - 50th Anniversary Tour 2027", "classifications": TRIBUTE_CLASS}
    assert radar.looks_like_tribute(ev, [{"name": "Rumours of Fleetwood Mac"}], "Fleetwood Mac")
    assert not radar.looks_like_tribute({"name": "Fleetwood Mac", "classifications": [{"subType": {"name": "Band"}}]}, [], "Fleetwood Mac")


def test_tribute_acts_are_also_caught_by_the_words_in_their_name():
    """Real Ticketmaster data: 'KISS TRIBUTE - Hotter Than Hell' has an 'Undefined' classification — only the wording
    ('a Tribute To Kiss') gives it away."""
    ev = {"name": "KISS TRIBUTE - HOTTER THAN HELL", "classifications": [{"subType": {"name": "Undefined"}}]}
    assert radar.looks_like_tribute(ev, [{"name": "Hotter Than Hell - a Tribute To Kiss", "classifications": [{"subType": {"name": "Undefined"}}]}], "Kiss")
    for name in ("A Tribute To Queen", "The Music of Fleetwood Mac", "ABBA Tribute Band Live", "We Pay Tribute To The Kings Of Rock"):
        assert radar.looks_like_tribute({"name": name}, [], "Queen"), name
    # a stylised brand name with no telltale word at all ('The Bootleg Beatles') needs the classification signal, not text


def test_a_genuine_act_is_never_flagged():
    for ev in ({"name": "Amon Amarth", "classifications": [{"subType": {"name": "Undefined"}, "genre": {"name": "Metal"}}]},
               {"name": "Electric Callboy - Lose Yourself Tour 2027", "classifications": []},
               {"name": "Judas Priest: Invincible Shield Tour", "classifications": [{"type": {"name": "Group"}}]}):
        assert not radar.looks_like_tribute(ev, [{"name": ev["name"], "classifications": ev.get("classifications", [])}], ev["name"].split(":")[0].split(" - ")[0])


def test_an_unrelated_support_acts_classification_never_flags_the_real_headliner():
    """Real Ticketmaster data: an Eagles of Death Metal show was wrongly flagged tribute because its support act,
    'Yachtley Crew', carries Ticketmaster's own 'Tribute Band' subType — that must never taint the headliner."""
    ev = {"name": "Eagles of Death Metal", "classifications": [{"subType": {"name": "Undefined"}, "genre": {"name": "Rock"}}]}
    attractions = [{"name": "Eagles of Death Metal", "classifications": [{"subType": {"name": "Undefined"}}]},
                   {"name": "Yachtley Crew", "classifications": [{"subType": {"name": "Tribute Band"}}]}]
    assert not radar.looks_like_tribute(ev, attractions, "Eagles of Death Metal")
    # but a bill where the searched act's *own* listed name plainly says so is still caught
    assert radar.looks_like_tribute({"name": "NIN UK + KORN AGAIN + DEAFTONES"},
        [{"name": "Korn Again", "classifications": [{"subType": {"name": "Concert"}}]},
         {"name": "NIN UK - Nine Inch Nails Tribute", "classifications": [{"subType": {"name": "Concert"}}]},
         {"name": "Deaftones (tribute)", "classifications": [{"subType": {"name": "Tribute Band"}}]}], "Korn")


def test_tribute_flag_is_stored_on_the_item():
    tm = {"_embedded": {"events": [
        {"id": "trib1", "name": "Rumours of Fleetwood Mac - 50th Anniversary Tour 2027", "url": "https://www.ticketmaster.co.uk/t1",
         "dates": {"start": {"localDate": "2027-06-20"}, "status": {"code": "onsale"}}, "classifications": TRIBUTE_CLASS,
         "_embedded": {"venues": [{"name": "The Lowry", "city": {"name": "Salford"}}], "attractions": [{"name": "Rumours of Fleetwood Mac", "classifications": TRIBUTE_CLASS}]}},
        {"id": "real1", "name": "Fleetwood Mac", "url": "https://www.ticketmaster.co.uk/r1",
         "dates": {"start": {"localDate": "2027-06-21"}, "status": {"code": "onsale"}}, "classifications": [{"subType": {"name": "Band"}}],
         "_embedded": {"venues": [{"name": "The AO Arena", "city": {"name": "Manchester"}}], "attractions": [{"name": "Fleetwood Mac"}]}}]}}
    items = {i["external_id"]: i for i in radar.parse_ticketmaster(tm, "Fleetwood Mac")}
    assert items["trib1"]["meta"]["tribute"] is True and items["real1"]["meta"]["tribute"] is False


def test_packages_already_stored_are_hidden_when_reading():
    content.upsert_items("radar", "ticketmaster", [
        {"external_id": "a", "artist": "Korn", "title": "Korn", "date": "2026-10-31", "meta": {}},
        {"external_id": "b", "artist": "Korn", "title": "Korn - Venue Premium Tickets", "date": "2026-10-31", "meta": {}},
        {"external_id": "c", "artist": "Korn", "title": "Korn - VIP Merch Package", "date": "2026-10-31", "meta": {}}], baseline=True)
    got = radar.widget.view(None, {"radius_miles": 20}, NOW)
    assert sorted(i["title"] for i in got["items"]) == ["Korn", "Korn - VIP Merch Package"] and got["total"] == 2


def test_gig_radar_is_soonest_first_pins_lead_and_past_shows_are_hidden():
    content.upsert_items("radar", "ticketmaster", [
        {"external_id": "1", "artist": "A", "title": "Oct", "date": "2026-10-09", "meta": {}}, {"external_id": "2", "artist": "B", "title": "Apr", "date": "2027-04-02", "meta": {}},
        {"external_id": "3", "artist": "C", "title": "Jan", "date": "2027-01-16", "meta": {}}, {"external_id": "4", "artist": "D", "title": "Yesterday", "date": "2026-09-20", "meta": {}}], baseline=True)
    titles = lambda: [i["title"] for i in radar.widget.view(None, {"radius_miles": 20}, NOW)["items"]]
    assert titles() == ["Oct", "Jan", "Apr"]                          # like Next Gig's own countdown: what's coming up soonest, first
    pin = next(i["id"] for i in radar.widget.view(None, {}, NOW)["items"] if i["title"] == "Apr")
    content.set_status(pin, "pinned")
    assert titles() == ["Apr", "Oct", "Jan"]


RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Test Metal</title>
<item><title>Gojira announce <b>huge</b> tour</title><link>https://ex.com/1</link><guid>g1</guid><pubDate>Sat, 19 Sep 2026 10:00:00 GMT</pubDate><description>Tour dates &amp; vinyl</description></item>
<item><title>Very old story</title><link>https://ex.com/2</link><guid>g2</guid><pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
<item><title>Celebrity gossip</title><link>https://ex.com/3</link><guid>g3</guid><pubDate>Sun, 20 Sep 2026 10:00:00 GMT</pubDate><description>nothing to do with music</description></item>
<item><title>Bad link story</title><link>javascript:alert(1)</link><guid>g4</guid><pubDate>Sun, 20 Sep 2026 11:00:00 GMT</pubDate></item>
<item><title>New vinyl reissue of a classic</title><link>https://ex.com/5</link><guid>g5</guid><pubDate>Sun, 20 Sep 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
FEED = {"id": "t", "name": "Test Metal"}


def test_rss_dates_html_and_links_are_handled():
    items = news.parse_feed(RSS, FEED, cutoff="2026-08-22", include=[], exclude=[])
    assert [i["external_id"] for i in items] == ["g1", "g3", "g5"]     # too old + javascript: link dropped
    assert items[0]["title"] == "Gojira announce huge tour" and items[0]["date"] == "2026-09-19" and items[0]["subtitle"] == "Test Metal"


def test_rss_keyword_rules():
    inc = news.parse_feed(RSS, FEED, cutoff="2026-08-22", include=["vinyl", "TOUR"], exclude=[])
    assert [i["external_id"] for i in inc] == ["g1", "g5"]             # case-insensitive; matches summary text too ("vinyl" in g1's description)
    exc = news.parse_feed(RSS, FEED, cutoff="2026-08-22", include=[], exclude=["gossip"])
    assert [i["external_id"] for i in exc] == ["g1", "g5"]
    both = news.parse_feed(RSS, FEED, cutoff="2026-08-22", include=["vinyl"], exclude=["reissue"])
    assert [i["external_id"] for i in both] == ["g1"]


def test_atom_feeds_work_too():
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title><entry><title>Atom story</title><id>a1</id>
      <link href="https://ex.com/a"/><updated>2026-09-20T10:00:00Z</updated></entry></feed>"""
    assert [i["title"] for i in news.parse_feed(atom, FEED, cutoff="2026-08-01", include=[], exclude=[])] == ["Atom story"]


# =============================================================== curation rules

def item(eid, date="2026-10-01", artist="Ghost", title=None, **kw):
    return {"external_id": eid, "artist": artist, "title": title or f"Title {eid}", "date": date, **kw}


def test_first_look_is_a_silent_baseline_and_later_finds_are_new():
    assert content.upsert_items("release", "musicbrainz", [item("a"), item("b")], baseline=True) == 2
    assert content.list_items("release")["new"] == 0          # the back catalogue doesn't light up
    assert content.upsert_items("release", "musicbrainz", [item("a"), item("c")], baseline=False) == 1     # only c is genuinely new
    got = content.list_items("release")
    assert got["new"] == 1
    assert sorted(i["title"] for i in got["items"] if i["status"] == "new") == ["Title c"]


def test_a_refresh_updates_details_but_never_the_curation_status():
    content.upsert_items("release", "musicbrainz", [item("a", title="Old title")], baseline=False)
    iid = content.list_items("release")["items"][0]["id"]
    content.set_status(iid, "pinned")
    content.upsert_items("release", "musicbrainz", [item("a", title="Corrected title", date="2026-11-11")], baseline=False)
    got = content.list_items("release")["items"][0]
    assert got["status"] == "pinned" and got["title"] == "Corrected title" and got["date"] == "2026-11-11"
    content.set_status(iid, "dismissed")
    content.upsert_items("release", "musicbrainz", [item("a")], baseline=False)
    both = content.list_items("release")
    assert both["items"] == [] and [i["id"] for i in both["dismissed"]] == [iid]           # still dismissed after another sync
    assert content.set_status(iid, "seen") and content.list_items("release")["items"][0]["status"] == "seen"
    assert content.set_status(999999, "seen") is False
    with pytest.raises(ValueError):
        content.set_status(iid, "bogus")


def test_every_card_reads_newest_to_oldest_and_pins_lead():
    content.upsert_items("release", "mb", [item("far", "2026-12-01"), item("soon", "2026-09-25"), item("recent", "2026-09-10"), item("older", "2026-06-01")], baseline=True)
    titles = lambda: [i["title"] for i in content.list_items("release")["items"]]
    assert titles() == ["Title far", "Title soon", "Title recent", "Title older"]
    pin = next(i["id"] for i in content.list_items("release")["items"] if i["title"] == "Title older")
    content.set_status(pin, "pinned")
    assert titles() == ["Title older", "Title far", "Title soon", "Title recent"]


def test_partial_dates_sort_as_the_end_of_their_period_and_undated_items_go_last():
    content.upsert_items("release", "mb", [item("exact", "2027-03-09"), item("year", "2027"), item("month", "2027-03"), item("none", None), item("later", "2027-12-01")], baseline=True)
    assert [i["title"] for i in content.list_items("release")["items"]] == ["Title year", "Title later", "Title month", "Title exact", "Title none"]
    assert content.sort_date("2027") == "2027-12-31" and content.sort_date("2027-03") == "2027-03-31" and content.sort_date(None) == ""


def test_muted_artists_are_hidden_but_not_deleted():
    content.add_artist("Ghost"); content.add_artist("Sabaton")
    content.upsert_items("release", "mb", [item("g", artist="Ghost"), item("s", artist="Sabaton")], baseline=True)
    ghost = next(a["id"] for a in content.list_artists() if a["name"] == "Ghost")
    content.set_artist_status(ghost, "mute")
    assert [i["artist"] for i in content.list_items("release")["items"]] == ["Sabaton"]
    content.set_artist_status(ghost, "follow")
    assert len(content.list_items("release")["items"]) == 2         # unmuting brings them straight back


def test_the_same_album_from_two_shops_shows_once_preferring_the_first():
    content.upsert_items("vinyl", "shop-b", [item("m", artist="Ghost", title="Skeletá")], baseline=True)
    content.upsert_items("vinyl", "shop-a", [item("d", artist="Ghost", title="Skeleta")], baseline=True)      # accent differs -> different key, both kept
    content.upsert_items("vinyl", "shop-a", [item("d2", artist="ghost", title="Skeletá")], baseline=True)
    got = content.list_items("vinyl", source_priority=("shop-a", "shop-b"))["items"]
    assert sorted(i["source"] for i in got if i["title"] == "Skeletá") == ["shop-a"]        # exact duplicate collapsed to the higher-priority shop


def test_a_leading_the_does_not_make_the_same_record_two_rows():
    content.upsert_items("vinyl", "shop-a", [item("1", artist="Amon Amarth", title="The Allfather Awakens")], baseline=True)
    content.upsert_items("vinyl", "shop-b", [item("2", artist="Amon Amarth", title="Allfather Awakens"), item("3", artist="Amon Amarth", title="Something Else")], baseline=True)
    got = content.list_items("vinyl", source_priority=("shop-a", "shop-b"))["items"]
    assert sorted((i["source"], i["title"]) for i in got) == [("shop-a", "The Allfather Awakens"), ("shop-b", "Something Else")]


def test_a_duplicate_lends_its_release_date_and_shop_name_to_the_preferred_row():
    content.upsert_items("vinyl", "shop-a", [item("1", artist="Saxon", title="Gods of Thunder", date=None, meta={"shop": "Earache", "listed": "2026-09-01"})], baseline=True)
    content.upsert_items("vinyl", "shop-b", [item("2", artist="Saxon", title="Gods Of Thunder", date="2027-01-22", meta={"shop": "Juno"})], baseline=True)
    row = content.list_items("vinyl", source_priority=("shop-a", "shop-b"))["items"]
    assert len(row) == 1 and row[0]["source"] == "shop-a" and row[0]["date"] == "2027-01-22" and row[0]["meta"]["also"] == ["Juno"]
    dated = content.list_items("vinyl", source_priority=("shop-a", "shop-b"))["items"][0]
    assert dated["meta"]["shop"] == "Earache"                                  # still presented as the preferred store's listing


def test_the_new_badge_only_counts_items_that_are_actually_delivered():
    content.upsert_items("news", "f", [item(f"n{i}", date=f"2026-08-{(i % 28) + 1:02d}", artist=None) for i in range(20)], baseline=True)
    with db.db() as con:
        con.execute("UPDATE feed_items SET status = 'new' WHERE external_id IN ('n0', 'n5')")
    everything = content.list_items("news", limit=100)
    assert everything["new"] == 2 and len(everything["items"]) == 20
    cut = content.list_items("news", limit=3)
    delivered_new = sum(1 for i in cut["items"] if i["status"] == "new")
    assert cut["total"] == 20 and cut["new"] == delivered_new             # never more "new" than the browser can show


def test_mark_all_seen_and_prune():
    content.upsert_items("news", "f", [item("1"), item("2")], baseline=False)
    assert content.list_items("news")["new"] == 2
    assert content.mark_all_seen("news") == 2 and content.list_items("news")["new"] == 0
    iid = content.list_items("news")["items"][0]["id"]
    content.set_status(iid, "pinned")
    with db.db() as con:
        con.execute("UPDATE feed_items SET last_seen = ?", (int(time.time()) - 90 * 86400,))
    assert content.prune("news", older_than_days=45) == 1                                   # the pinned one survives
    assert len(content.list_items("news")["items"]) == 1


def test_mark_read_is_independent_of_status():
    content.upsert_items("news", "f", [item("1")], baseline=False)
    iid = content.list_items("news")["items"][0]["id"]
    assert content.list_items("news")["items"][0]["meta"].get("read") is None                # untouched -> no key at all
    assert content.mark_read(iid) is True
    it = content.list_items("news")["items"][0]
    assert it["meta"]["read"] is True and it["status"] == "new"                               # reading never touches status
    content.mark_all_seen("news")
    assert content.list_items("news")["items"][0]["meta"]["read"] is True                     # and marking seen never touches read
    assert content.mark_read(999999) is False                                                 # no such item


# =============================================================== artists + seeding

def test_pick_artists_orders_pinned_first_then_least_recently_checked():
    for n in ("A", "B", "C", "D"):
        content.add_artist(n)
    ids = {a["name"]: a["id"] for a in content.list_artists()}
    content.set_artist_status(ids["D"], "mute")
    content.set_artist_status(ids["C"], "pin")
    content.mark_checked(ids["A"], "mb_checked_at")
    got = [a["name"] for a in content.pick_artists("mb_checked_at", 10)]
    assert got == ["C", "B", "A"]                                # pinned, then never-checked, then checked; muted excluded
    assert [a["name"] for a in content.pick_artists("mb_checked_at", 1)] == ["C"]
    with pytest.raises(ValueError):
        content.pick_artists("name; DROP TABLE x", 5)             # column names are whitelisted (they're interpolated into SQL)


def make_music_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
      CREATE TABLE artists (id INTEGER PRIMARY KEY, mbid TEXT, name TEXT);
      CREATE TABLE albums (id INTEGER PRIMARY KEY, mbid TEXT, artist_id INTEGER, title TEXT);
      CREATE TABLE scrobbles (id INTEGER PRIMARY KEY, artist_id INTEGER);
      CREATE TABLE vinyl_holdings (id INTEGER PRIMARY KEY, album_id INTEGER, discogs_release_id INTEGER);
      CREATE TABLE setlists (id INTEGER PRIMARY KEY, artist_id INTEGER);
      INSERT INTO artists VALUES (1,'mb-gojira','Gojira'),(2,'mb-ghost','Ghost'),(3,NULL,'Barely Played'),(4,'mb-vinylonly','Vinyl Only'),(5,'mb-gig','Gig Only');
      INSERT INTO albums VALUES (10,'rg-fortitude',1,'Fortitude'),(11,NULL,4,'Some LP');
      INSERT INTO vinyl_holdings VALUES (1,10,5551),(2,11,5552);
      INSERT INTO setlists VALUES (1,5),(2,5);
    """)
    con.executemany("INSERT INTO scrobbles (artist_id) VALUES (?)", [(1,)] * 500 + [(2,)] * 80 + [(3,)] * 5)
    con.commit(); con.close()


def test_seeding_from_music_companion_is_read_only_and_respects_your_choices(tmp_path):
    path = tmp_path / "music.sqlite"
    make_music_db(path)
    before = (path.stat().st_mtime_ns, path.stat().st_size)
    assert content.seed_from_music_db(path, limit=10) == {"found": 4, "added": 4, "updated": 0}
    names = {a["name"]: a for a in content.list_artists()}
    assert set(names) == {"Gojira", "Ghost", "Vinyl Only", "Gig Only"}                       # 'Barely Played' (5 plays, no vinyl/gigs) isn't followed
    assert names["Gojira"]["weight"] > names["Ghost"]["weight"] and names["Gojira"]["mbid"] == "mb-gojira" and names["Gojira"]["source"] == "music-companion"
    content.set_artist_status(names["Ghost"]["id"], "mute")
    assert content.seed_from_music_db(path, limit=10) == {"found": 4, "added": 0, "updated": 4}
    assert next(a for a in content.list_artists() if a["name"] == "Ghost")["status"] == "mute"     # a resync never undoes a mute
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before                             # never modifies the other project's data
    assert content.seed_from_music_db(path, limit=2)["found"] == 2 and len(content.list_artists()) == 4        # a smaller limit never un-follows anyone
    assert content.seed_from_music_db(tmp_path / "missing.sqlite") == {"found": 0, "added": 0, "updated": 0}


# =============================================================== fetch loops (network faked)

class FakeMB:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    async def get(self, path, **params):
        self.calls.append((path, params))
        return self.responses[path]


def test_attach_art_fills_meta_image_and_survives_a_bad_lookup(monkeypatch):
    """One release group's art fails to resolve (network hiccup, bad id, whatever) — that must never lose the
    others', or the whole sync. front_thumb() itself already turns a 404 into None (tested in test_coverart.py);
    this covers attach_art()'s own job: wiring that per-item and not letting one exception stop the batch."""
    async def fake_front_thumb(mbid):
        if mbid == "rg-good":
            return "https://coverartarchive.org/release/x/y-250.jpg"
        if mbid == "rg-bad":
            raise httpx.ConnectError("boom")
        return None
    monkeypatch.setattr(coverart.client, "front_thumb", fake_front_thumb)

    items = [{"external_id": "rg-good", "title": "A"}, {"external_id": "rg-bad", "title": "B"}, {"external_id": "rg-none", "title": "C"}]
    run(releases.attach_art(items))
    assert items[0]["meta"]["image"] == "https://coverartarchive.org/release/x/y-250.jpg"
    assert items[1]["meta"]["image"] is None            # the failed lookup didn't crash attach_art()...
    assert items[2]["meta"]["image"] is None             # ...and didn't stop it reaching the next item either


def test_releases_fetch_stores_cover_art_in_item_meta(monkeypatch):
    content.add_artist("Gojira")
    aid = content.list_artists()[0]["id"]
    content.set_mbid(aid, "mb-gojira")
    fake = FakeMB({"release-group": RG})
    monkeypatch.setattr(musicbrainz, "client", fake)
    monkeypatch.setattr(releases.mb, "client", fake)
    monkeypatch.setattr(coverart.client, "front_thumb", lambda mbid: _resolved("https://coverartarchive.org/release/x/y-250.jpg"))
    settings = {"batch": 30, "lookback_days": 200, "horizon_days": 365, "types": ["Album", "EP", "Single"]}

    run(releases.widget.fetch(settings))
    got = content.list_items("release")["items"]
    assert got and all(i["meta"]["image"] == "https://coverartarchive.org/release/x/y-250.jpg" for i in got)


async def _resolved(value):
    return value


def test_releases_fetch_baselines_then_flags_only_new(monkeypatch):
    content.add_artist("Gojira")
    aid = content.list_artists()[0]["id"]
    content.set_mbid(aid, "mb-gojira")
    fake = FakeMB({"release-group": RG})
    monkeypatch.setattr(musicbrainz, "client", fake)
    monkeypatch.setattr(releases.mb, "client", fake)
    settings = {"batch": 30, "lookback_days": 200, "horizon_days": 365, "types": ["Album", "EP", "Single"]}       # cutoff 2026-03: the 2019 release is out

    assert run(releases.widget.fetch(settings))["new"] == 2                                          # the album and the EP (the live album is filtered out)
    assert fake.calls[0][0] == "release-group" and fake.calls[0][1]["query"].startswith("arid:mb-gojira AND primarytype:(album OR ep OR single) AND firstreleasedate:[")
    assert content.list_items("release")["new"] == 0                   # first pass = baseline
    assert checked_at("mb_checked_at") is not None

    fake.responses["release-group"] = {"release-groups": RG["release-groups"] + [
        {"id": "rg-fresh", "title": "Fresh Drop", "primary-type": "Single", "secondary-types": [], "first-release-date": "2026-10-02"}]}
    assert run(releases.widget.fetch(settings))["checked"] == 0                    # checked a moment ago: left alone (each artist is looked at about once a day)
    assert run(releases.widget.fetch({**settings, "_forced": True}))["checked"] == 0    # even "Sync now" leaves anyone checked in the last hour alone
    age_checks()
    assert run(releases.widget.fetch({**settings, "_forced": True}))["new"] == 1   # …but an hour on, it looks again
    got = content.list_items("release")
    assert got["new"] == 1 and [i["title"] for i in got["items"] if i["status"] == "new"] == ["Fresh Drop"]
    view = releases.widget.view(None, settings, NOW)
    assert view["following"] == 1 and view["new"] == 1


def test_releases_resolves_a_missing_artist_id_by_exact_name(monkeypatch):
    content.add_artist("Ghost")
    fake = FakeMB({"artist": {"artists": [{"id": "wrong", "name": "Ghostface Killah", "score": 100}, {"id": "mb-ghost", "name": "GHOST", "score": 98}]},
                   "release-group": {"release-groups": []}})
    monkeypatch.setattr(musicbrainz, "client", fake); monkeypatch.setattr(releases.mb, "client", fake)
    run(releases.widget.fetch({"batch": 5, "lookback_days": 90, "horizon_days": 365, "types": ["Album"]}))
    assert content.list_artists()[0]["mbid"] == "mb-ghost"                    # exact name only — not the near miss
    assert fake.calls[0][0] == "artist" and fake.calls[1][1]["query"].startswith("arid:mb-ghost")

    content.add_artist("Nobody Real")
    fake.responses["artist"] = {"artists": [{"id": "x", "name": "Somebody Else", "score": 100}]}
    run(releases.widget.fetch({"batch": 5, "lookback_days": 90, "horizon_days": 365, "types": ["Album"]}))
    nobody = next(a for a in content.list_artists() if a["name"] == "Nobody Real")
    assert nobody["mbid"] is None and checked_at("mb_checked_at", "Nobody Real") is not None      # no wrong artist guessed; not retried every run


def handler_factory(routes):
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                return resp(request) if callable(resp) else resp
        return httpx.Response(404)
    return handler


def patch_http(monkeypatch, module, routes):
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(handler_factory(routes)), **kw))


def test_radar_fetch_sends_location_and_key_and_reports_bad_keys(monkeypatch):
    content.add_artist("Ghost")
    seen = {}
    def ok(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json=TM)
    patch_http(monkeypatch, radar, {"https://app.ticketmaster.com/": ok})
    monkeypatch.setattr(radar, "asyncio", SimpleNamespace(sleep=lambda s: REAL_SLEEP(0)))         # skip the politeness delay for this widget only
    settings = {"api_key": "K" * 20, "latlong": "53.48,-2.24", "radius_miles": 50, "batch": 10}
    assert run(radar.widget.fetch(settings))["checked"] == 1
    assert seen["keyword"] == "Ghost" and seen["latlong"] == "53.48,-2.24" and seen["radius"] == "50" and seen["unit"] == "miles" and seen["apikey"] == "K" * 20
    assert sorted(i["title"] for i in content.list_items("radar")["items"]) == ["Ghost - Skeletá Tour", "Support acts"]

    forced = {**settings, "_forced": True}
    age_checks()
    patch_http(monkeypatch, radar, {"https://app.ticketmaster.com/": httpx.Response(401)})
    with pytest.raises(RuntimeError, match="rejected the API key"):
        run(radar.widget.fetch(forced))
    patch_http(monkeypatch, radar, {"https://app.ticketmaster.com/": httpx.Response(429)})
    with pytest.raises(RuntimeError, match="rate limit"):
        run(radar.widget.fetch(forced))


def test_radar_view_drops_past_gigs():
    content.upsert_items("radar", "ticketmaster", [item("past", "2026-08-01"), item("future", "2026-12-01")], baseline=True)
    assert [i["date"] for i in radar.widget.view(None, {}, NOW)["items"]] == ["2026-12-01"]


def test_news_fetch_survives_one_broken_feed(monkeypatch):
    patch_http(monkeypatch, news, {"https://good.example/": httpx.Response(200, content=RSS), "https://bad.example/": httpx.Response(500)})
    settings = {"feeds": [{"id": "good", "name": "Good", "url": "https://good.example/feed", "enabled": True},
                          {"id": "bad", "name": "Bad", "url": "https://bad.example/feed", "enabled": True},
                          {"id": "off", "name": "Off", "url": "https://off.example/feed", "enabled": False}],
                "include": [], "exclude": [], "max_age_days": 3650}
    out = run(news.widget.fetch(settings))
    assert out["feeds"] == 1 and out["errors"] == ["Bad: HTTPStatusError"]
    assert {i["source"] for i in content.list_items("news")["items"]} == {"good"}
    assert content.list_items("news")["new"] == 0                   # first fetch of a feed is a baseline

    patch_http(monkeypatch, news, {"https://good.example/": httpx.Response(500), "https://bad.example/": httpx.Response(500)})
    with pytest.raises(RuntimeError, match="Good: HTTPStatusError; Bad"):
        run(news.widget.fetch(settings))                                                        # everything failing IS an error (stale badge)


def test_news_fetch_prunes_by_the_configured_retention_not_a_fixed_45_days(monkeypatch):
    content.upsert_items("news", "stale-source", [item("old")], baseline=True)
    with db.db() as con:
        con.execute("UPDATE feed_items SET last_seen = ? WHERE source = 'stale-source'", (int(time.time()) - 100 * 86400,))
    patch_http(monkeypatch, news, {"https://good.example/": httpx.Response(200, content=RSS)})
    settings = {"feeds": [{"id": "good", "name": "Good", "url": "https://good.example/feed", "enabled": True}],
                "include": [], "exclude": [], "max_age_days": 180}      # the item is 100 days old: a fixed 45-day prune would wrongly delete it
    run(news.widget.fetch(settings))
    assert "stale-source" in {i["source"] for i in content.list_items("news")["items"]}          # survives — 100 < the configured 180


def test_match_artist_tags_prefers_longest_name_and_ignores_partial_words():
    assert news.match_artist_tags("Ghost B.C. announces a tour", ["Ghost", "Ghost B.C."]) == ["Ghost B.C."]
    assert news.match_artist_tags("A ghostwriter's tale", ["Ghost"]) == []                # not part of a larger word
    assert news.match_artist_tags("No mention here", ["Gojira"]) == []
    assert news.match_artist_tags("Mastodon and Gojira co-headline", ["Gojira", "Mastodon"]) == ["Mastodon", "Gojira"]  # order of appearance in the title
    assert news.match_artist_tags("", ["Gojira"]) == [] and news.match_artist_tags("Gojira tour", []) == []


def test_news_fetch_tags_items_with_mentioned_followed_artists(monkeypatch):
    content.add_artist("Gojira")
    patch_http(monkeypatch, news, {"https://good.example/": httpx.Response(200, content=RSS)})
    settings = {"feeds": [{"id": "good", "name": "Good", "url": "https://good.example/feed", "enabled": True}],
                "include": [], "exclude": [], "max_age_days": 3650}
    run(news.widget.fetch(settings))
    items = {i["title"]: i for i in content.list_items("news")["items"]}
    assert items["Gojira announce huge tour"]["meta"]["artists"] == ["Gojira"]
    assert items["Celebrity gossip"]["meta"]["artists"] == []                             # no follow mentioned -> tagged, but empty


def test_news_per_feed_keywords_override_the_global_list_not_add_to_it(monkeypatch):
    patch_http(monkeypatch, news, {"https://good.example/": httpx.Response(200, content=RSS)})
    settings = {"feeds": [{"id": "good", "name": "Good", "url": "https://good.example/feed", "enabled": True, "include": ["gossip"]}],
                "include": ["vinyl"], "exclude": [], "max_age_days": 3650}                # global include would keep the vinyl stories instead
    run(news.widget.fetch(settings))
    titles = {i["title"] for i in content.list_items("news")["items"]}
    assert titles == {"Celebrity gossip"}                                                 # only the feed's own include list applied, not a union with the global one


def test_switching_a_feed_off_hides_its_items_immediately():
    content.upsert_items("news", "a", [item("1", artist=None)], baseline=True)
    content.upsert_items("news", "b", [item("2", artist=None)], baseline=True)
    s = {"feeds": [{"id": "a", "name": "A", "enabled": True}, {"id": "b", "name": "B", "enabled": False}]}
    assert {i["source"] for i in news.widget.view(None, s, NOW)["items"]} == {"a"}


# =============================================================== booked gigs + settings validation

def test_gigs_view_counts_down_and_splits_past_from_upcoming(client):
    with db.db() as con:
        for a, d in (("Old Band", "2026-08-01"), ("Gojira", "2026-09-26"), ("Mastodon", "2026-10-30"), ("Tonight", "2026-09-21")):
            con.execute("INSERT INTO gigs (artist, venue, city, date, doors, created_at) VALUES (?, 'V', 'C', ?, '19:00', 0)", (a, d))
    v = gigs.widget.view(None, {}, NOW)
    assert v["next"]["artist"] == "Tonight" and v["next"]["in_days"] == 0
    assert [(g["artist"], g["in_days"]) for g in v["upcoming"]] == [("Tonight", 0), ("Gojira", 5), ("Mastodon", 39)]
    assert [g["artist"] for g in v["past"]] == ["Old Band"] and v["count"] == 3


def test_content_settings_are_validated_and_secrets_stay_secret():
    r = radar.widget
    assert r.merge_settings({"api_key": "OLDKEYOLDKEYOLDKEY1"}, {"api_key": ""})["api_key"] == "OLDKEYOLDKEYOLDKEY1"        # blank keeps the stored key
    assert r.public_settings({"api_key": "SECRETSECRETSECRET1", "radius_miles": 50}) == {"radius_miles": 50, "has_api_key": True}
    assert r.merge_settings({}, {"latlong": " 53.48 , -2.24 "})["latlong"] == "53.48,-2.24"
    for bad in ({"latlong": "nowhere"}, {"latlong": "95,0"}, {"radius_miles": 2}, {"api_key": "too short"}, {"batch": 1}):
        with pytest.raises(ValueError):
            r.merge_settings({}, bad)
    for w, bad in ((releases.widget, {"types": ["Podcast"]}), (releases.widget, {"lookback_days": 1}), (vinyl.widget, {"max_pages": 0}), (vinyl.widget, {"shops": [{"name": "x", "url": "ftp://x"}]}), (vinyl.widget, {"shops": [{"name": "x", "url": "https://x", "collection": "Not A Handle!"}]}),
                   (news.widget, {"feeds": [{"name": "x", "url": "ftp://x"}]}), (news.widget, {"include": "vinyl"}), (news.widget, {"max_age_days": 1})):
        with pytest.raises(ValueError):
            w.merge_settings({}, bad)
    feeds = news.widget.merge_settings({}, {"feeds": [{"name": "Same Name", "url": "https://a/f"}, {"name": "Same Name", "url": "https://b/f"}]})["feeds"]
    assert [f["id"] for f in feeds] == ["same-name", "same-name-2"]                       # ids stay unique


def test_per_feed_keywords_are_validated_and_only_stored_when_set():
    feeds = news.widget.merge_settings({}, {"feeds": [
        {"name": "Custom", "url": "https://a/f", "include": ["vinyl", " tour "], "exclude": []},
        {"name": "Plain", "url": "https://b/f"},
    ]})["feeds"]
    assert feeds[0]["include"] == ["vinyl", "tour"] and "exclude" not in feeds[0]          # trimmed; an empty list isn't worth storing
    assert "include" not in feeds[1] and "exclude" not in feeds[1]                         # falls back to the global lists at fetch time
    with pytest.raises(ValueError):
        news.widget.merge_settings({}, {"feeds": [{"name": "x", "url": "https://a/f", "include": "vinyl"}]})
    with pytest.raises(ValueError):
        news.widget.merge_settings({}, {"feeds": [{"name": "x", "url": "https://a/f", "include": ["y" * 61]}]})


# =============================================================== catch-up backlog

def test_a_backlog_makes_the_widget_ask_for_an_early_rerun(monkeypatch):
    for n in ("A", "B", "C"):
        content.add_artist(n)
    assert content.count_unchecked("mb_checked_at") == 3
    fake = FakeMB({"artist": {"artists": []}, "release-group": {"release-groups": []}})
    monkeypatch.setattr(musicbrainz, "client", fake); monkeypatch.setattr(releases.mb, "client", fake)
    settings = {"batch": 2, "lookback_days": 31, "horizon_days": 365, "types": ["Album"]}
    out = run(releases.widget.fetch(settings))
    assert out["checked"] == 2 and out["pending"] == 1                                   # one artist still unvisited
    assert releases.widget.retry_after(out) == 20                                        # so: come back shortly…
    out = run(releases.widget.fetch(settings))
    assert out["pending"] == 0 and releases.widget.retry_after(out) is None              # …until everyone has had a look, then wait for the schedule


def test_artists_are_only_rechecked_once_they_are_a_day_stale():
    for n in ("A", "B", "C"):
        content.add_artist(n)
    ids = {a["name"]: a["id"] for a in content.list_artists()}
    now = int(time.time())
    with db.db() as con:
        con.execute("UPDATE followed_artists SET mb_checked_at = ? WHERE name = 'A'", (now - 2 * 3600,))          # 2 hours ago
        con.execute("UPDATE followed_artists SET mb_checked_at = ? WHERE name = 'B'", (now - 30 * 3600,))         # yesterday
    names = lambda age: sorted(a["name"] for a in content.pick_artists("mb_checked_at", 10, min_age=age))
    assert names(content.FRESH_SECONDS) == ["B", "C"] and names(content.FORCED_SECONDS) == ["A", "B", "C"] and names(60) == ["A", "B", "C"] and names(0) == ["A", "B", "C"]
    assert content.count_stale("mb_checked_at", content.FRESH_SECONDS) == 2 and content.count_stale("mb_checked_at", 60) == 3


# =============================================================== scheduler: runs never overlap

def test_a_manual_refresh_never_overlaps_a_running_fetch():
    """Regression: a scheduled run and a 'sync now' used to run concurrently, and the one finishing last overwrote the other's result."""
    from app.scheduler import Scheduler
    from app.widgets.base import Widget

    class Slow(Widget):
        id, title, interval, defaults = "slowtest", "Slow", 3600, {}
        active = peak = runs = 0

        async def fetch(self, settings):
            Slow.active += 1; Slow.peak = max(Slow.peak, Slow.active); Slow.runs += 1
            await asyncio.sleep(0.05)
            Slow.active -= 1
            return {"n": Slow.runs}

    w = Slow()
    events = []

    async def scenario():
        sched = Scheduler({"slowtest": w}, events.append)
        first = asyncio.create_task(sched.run_once(w))            # e.g. the scheduled run…
        await asyncio.sleep(0.01)
        for _ in range(5):
            sched.refresh("slowtest")                             # …while the user hammers "Sync now"
        await first
        await asyncio.sleep(0.3)                                  # let the queued run finish
    from app import config as cfg
    saved = cfg.NO_FETCH
    cfg.NO_FETCH = False
    try:
        asyncio.run(scenario())
    finally:
        cfg.NO_FETCH = saved
    assert Slow.peak == 1                                         # never two at once
    assert Slow.runs == 2                                         # the running one + ONE coalesced follow-up (not five)


# =============================================================== 200 followed artists, the re-check migration, the CSP

def test_the_top_two_hundred_artists_are_followed_by_default(tmp_path):
    assert content.SEED_LIMIT == 200 and content.seed_from_music_db.__kwdefaults__["limit"] == 200
    path = tmp_path / "many.sqlite"
    con = sqlite3.connect(path)
    con.executescript("""CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT, mbid TEXT);
      CREATE TABLE scrobbles (id INTEGER PRIMARY KEY, artist_id INTEGER);
      CREATE TABLE albums (id INTEGER PRIMARY KEY, artist_id INTEGER);
      CREATE TABLE vinyl_holdings (id INTEGER PRIMARY KEY, album_id INTEGER, discogs_release_id INTEGER);
      CREATE TABLE setlists (id INTEGER PRIMARY KEY, artist_id INTEGER);""")
    con.executemany("INSERT INTO artists (id, name) VALUES (?, ?)", [(i, f"Band {i}") for i in range(1, 251)])
    con.executemany("INSERT INTO scrobbles (artist_id) VALUES (?)", [(i,) for i in range(1, 251) for _ in range(60 + i)])
    con.commit(); con.close()
    assert content.seed_from_music_db(path)["found"] == 200 and len(content.list_artists()) == 200


def test_the_recheck_migration_sends_every_artist_back_through_the_new_releases_query():
    content.add_artist("Ghost"); content.add_artist("Sabaton")
    for a in content.list_artists():
        content.mark_checked(a["id"], "mb_checked_at"); content.mark_checked(a["id"], "radar_checked_at")
    assert content.count_unchecked("mb_checked_at") == 0
    with db.db() as con:
        con.executescript((db.MIGRATIONS / "006_recheck_releases.sql").read_text())
    assert content.count_unchecked("mb_checked_at") == 2 and content.count_unchecked("radar_checked_at") == 0        # only releases is redone


def test_artwork_hosts_are_the_only_images_the_csp_lets_through(client):
    csp = client.get("/guest").headers["content-security-policy"]
    img = next(part for part in csp.split(";") if part.strip().startswith("img-src"))
    assert set(img.split()[1:]) == {"'self'", "data:", "https://cdn.shopify.com", "https://imagescdn.juno.co.uk", "https://coverartarchive.org", "https://archive.org", "https://*.archive.org"}
    assert "script-src 'self'" in [part.strip() for part in csp.split(";")]                    # scripts are still first-party only


# =============================================================== releases: last month + everything announced, oldest to newest

def test_releases_read_oldest_to_newest_only_a_month_back_pins_first():
    """The one card that runs oldest -> newest: last month's releases, then what's announced, furthest away last."""
    cfg = {**releases.widget.defaults}                                            # NOW = 21 Sep 2026: the window opens 21 Aug
    assert cfg["lookback_days"] == 31
    content.upsert_items("release", "mb", [
        {"external_id": "1", "artist": "X", "title": "Far", "date": "2027-02-05", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "2", "artist": "X", "title": "Soon", "date": "2026-10-02", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "3", "artist": "X", "title": "Last week", "date": "2026-09-14", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "4", "artist": "X", "title": "Five weeks ago", "date": "2026-08-14", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "5", "artist": "X", "title": "Ages ago", "date": "2026-03-01", "meta": {"type": "Album", "secondary": []}},
        {"external_id": "6", "artist": "X", "title": "This year, day unknown", "date": "2026", "meta": {"type": "Album", "secondary": []}}], baseline=True)
    titles = lambda: [i["title"] for i in releases.widget.view(None, cfg, NOW)["items"]]
    assert titles() == ["Last week", "Soon", "This year, day unknown", "Far"]     # ascending; '2026' (no day) sorts as the end of 2026; older than a month is gone
    pin = next(i["id"] for i in releases.widget.view(None, cfg, NOW)["items"] if i["title"] == "Far")
    content.set_status(pin, "pinned")
    assert titles() == ["Far", "Last week", "Soon", "This year, day unknown"]      # pinned first, the rest still oldest to newest
    assert [i["title"] for i in releases.widget.view(None, {**cfg, "lookback_days": 60}, NOW)["items"]][1] == "Five weeks ago"       # the window is a setting (after the pin)


def test_releases_lookback_is_validated_and_defaults_to_a_month():
    assert releases.widget.merge_settings({}, {"lookback_days": 45})["lookback_days"] == 45
    for bad in (0, 3, 400, "31"):
        with pytest.raises(ValueError):
            releases.widget.merge_settings({}, {"lookback_days": bad})


def test_the_musicbrainz_window_starts_a_month_back_to_the_day():
    q = releases.search_query("abc", "2026-08-21", "2027-09-21", ["Album"])
    assert q.endswith("firstreleasedate:[2026-08-21 TO 2027-09-21]")
    rgs = {"release-groups": [{"id": "a", "title": "Edge", "primary-type": "Album", "secondary-types": [], "first-release-date": "2026-08-21"},
                              {"id": "b", "title": "Just out", "primary-type": "Album", "secondary-types": [], "first-release-date": "2026-08-20"}]}
    assert [i["title"] for i in releases.parse_release_groups(rgs, "X", cutoff="2026-08-21", horizon="2027-09-21", types=["Album"])] == ["Edge"]
