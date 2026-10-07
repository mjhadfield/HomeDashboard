"""Worth buying (app/music/buying.py): which albums qualify, how pressings are sorted into originals and coloured
editions, the two price rules, and the whole run against a fake Discogs and MusicBrainz — nothing external."""
import asyncio
import json
import sqlite3
from datetime import date, timedelta

import httpx
import pytest

from app import config, db, musicbrainz
from app.music import buying, library, store
from app.music.services import discogs

RECENT = (date.today() - timedelta(days=20)).isoformat()
OLD = (date.today() - timedelta(days=900)).isoformat()


def _library(path):
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT, mbid TEXT);
CREATE TABLE albums (id INTEGER PRIMARY KEY, mbid TEXT, artist_id INTEGER, title TEXT, year INTEGER);
CREATE TABLE scrobbles (id INTEGER PRIMARY KEY, song_id INTEGER, artist_id INTEGER, album_id INTEGER, played_at TEXT);
CREATE TABLE vinyl_holdings (id INTEGER PRIMARY KEY, album_id INTEGER);
CREATE TABLE album_parts (album_id INTEGER, part_album_id INTEGER, position INTEGER);
CREATE TABLE setlists (id INTEGER PRIMARY KEY, artist_id INTEGER, venue_id INTEGER, event_date TEXT);
""")
    con.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(1, "Alice In Chains", None), (2, "Gojira", None), (3, "Various Artists", None), (4, "Led Zeppelin", None)])
    con.executemany("INSERT INTO albums VALUES (?, ?, ?, ?, ?)", [
        (10, "rg-dirt", 1, "Dirt", 1992),            # played a lot, not owned -> a candidate
        (11, "rg-jar", 1, "Jar of Flies", 1994),     # played a little recently -> lower
        (20, "rg-magma", 2, "Magma", 2016),          # owned
        (21, "rg-magma", 2, "Magma (Deluxe)", 2016),  # the same release group as an owned album -> not a candidate
        (30, None, 3, "Now 42", 1999),               # Various Artists -> never
        (40, None, 4, "Physical Graffiti", 1975),    # owned as part of a set
        (41, None, 4, "Led Zeppelin IV", 1971),      # only 5 plays -> under the floor
    ])
    rows = []
    def plays(album, artist, n, when):
        rows.extend((artist, album, f"{when}T12:00:00+00:00") for _ in range(n))
    plays(10, 1, 100, OLD); plays(10, 1, 20, RECENT); plays(11, 1, 40, RECENT); plays(20, 2, 200, RECENT); plays(21, 2, 50, RECENT)
    plays(30, 3, 300, RECENT); plays(40, 4, 90, RECENT); plays(41, 4, 5, RECENT)
    con.executemany("INSERT INTO scrobbles (artist_id, album_id, played_at) VALUES (?, ?, ?)", rows)
    con.executemany("INSERT INTO vinyl_holdings VALUES (?, ?)", [(1, 20), (2, 99)])
    con.execute("INSERT INTO albums VALUES (99, NULL, 4, 'Physical Graffiti (set)', 1975)")
    con.execute("INSERT INTO album_parts VALUES (99, 40, 1)")
    con.commit()
    con.close()


@pytest.fixture(autouse=True)
def _clean(client):
    with db.db() as con:
        con.execute("DELETE FROM music_cache")
        con.execute("DELETE FROM widget_settings WHERE widget LIKE 'music%'")
        con.execute("DELETE FROM widget_cache WHERE widget = 'music-buying'")
    library.forget()
    yield
    library.forget()


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    path = tmp_path / "music.sqlite"
    _library(path)
    monkeypatch.setattr(config, "MUSIC_DB", path)
    library.forget()


def _token(max_price="30"):
    s = store.settings()
    s["services"]["discogs"] = {"enabled": True, "api_key": "tok", "max_price": max_price, "watch": "10"}
    store.put_settings(s)


# ---- the pure rules ------------------------------------------------------------------------------------------------------

def test_formats_say_what_a_pressing_is():
    assert buying.is_reissue("LP, Album, RE, Gat") and buying.is_reissue("LP, Album, Remastered")
    assert not buying.is_reissue("LP, Album, Gat")
    assert buying.colour_of("LP, Album, Ltd, Red Marbled") == "Red Marbled"
    assert buying.colour_of("2xLP, Album, Cle") == "Cle"
    assert buying.colour_of("LP, Album, Black") is None and buying.colour_of("LP, Album, Gat") is None


def test_the_colour_comes_from_the_pressings_own_words():
    assert buying.colour_of("LP, Album, Reissue", "Blue Translucent") == "Blue Translucent"
    assert buying.colour_of("LP, Album", "180 Gram; Clear With Blue Splatter") == "Clear With Blue Splatter"
    assert buying.colour_of("LP, Album, Test Pressing, White Label") is None             # the label's colour, not the vinyl's
    assert buying.colour_of("LP, Album", "Black Labels, EMI Pressing") is None
    assert buying.colour_of("LP, Album", "Black 180g") is None
    assert not buying.buyable({"format": "LP, Album, Test Pressing"}) and not buying.buyable({"format": "LP, Album, Promo"})


def test_versions_sort_into_originals_and_coloured_editions():
    vs = [
        {"id": 1, "format": "LP, Album", "country": "US", "released": "1992-09-29", "major_formats": ["Vinyl"], "stats": {"community": {"in_collection": 900}}},
        {"id": 2, "format": "LP, Album", "country": "UK", "released": "1992", "major_formats": ["Vinyl"], "stats": {"community": {"in_collection": 100}}},
        {"id": 3, "format": "LP, Album, RE", "country": "UK", "released": "1992", "major_formats": ["Vinyl"]},            # a same-year reissue isn't original
        {"id": 4, "format": "LP, Album, RE, Ltd, Yellow", "country": "Europe", "released": "2022", "major_formats": ["Vinyl"]},
        {"id": 5, "format": "LP, Album, RE", "country": "Europe", "released": "2009", "major_formats": ["Vinyl"]},        # neither
        {"id": 6, "format": "CD, Album", "country": "US", "released": "1992", "major_formats": ["CD"]},                 # not vinyl
    ]
    g = buying.sort_versions(vs, 1992)
    assert [v["id"] for v in g["original"]] == [1, 2]          # UK/Europe/US first (both here), then the most owned
    assert [v["id"] for v in g["coloured"]] == [4]
    assert g["first_vinyl_year"] == 1992
    cd_era = buying.sort_versions([{"id": 7, "format": "LP, Album", "country": "Germany", "released": "2001", "major_formats": ["Vinyl"]},
                                   {"id": 8, "format": "LP, Album, Promo", "country": "Germany", "released": "2000", "major_formats": ["Vinyl"]}], 2000)
    assert [v["id"] for v in cd_era["original"]] == [7] and cd_era["first_vinyl_year"] == 2001   # the album was 2000 (CD); vinyl first 2001


def test_within_budget_means_the_vg_plus_guide_price_is_under_your_limit():
    assert buying.affordable({"price": 25.0, "usual": 30.0, "for_sale": 3}, 35)
    assert not buying.affordable({"price": 7.97, "usual": 60.0, "for_sale": 2}, 35)   # a cheap listing (Poor, or abroad) doesn't make it a bargain
    assert buying.affordable({"price": 45.0, "usual": 30.0, "for_sale": 1}, 35)       # judged by the guide, not the lowest listing
    assert not buying.affordable({"price": 20.0, "usual": None, "for_sale": 4}, 35)   # no guide price: can't say
    assert not buying.affordable({"price": None, "usual": 20.0, "for_sale": 0}, 35)   # none for sale


# ---- candidates from Music Companion ----------------------------------------------------------------------------------------

def test_candidates_are_played_a_lot_and_not_owned(lib):
    got = buying.candidates(10)
    assert [a["title"] for a in got] == ["Dirt", "Jar of Flies"]          # owned, same-release-group, set parts, Various, under the floor: out
    assert got[0]["score"] == 120 + 2 * 20                                # blended: all time + 2 x the last year
    buying.dismiss(10)
    assert [a["title"] for a in buying.candidates(10)] == ["Jar of Flies"]
    buying.dismiss(10, undo=True)
    assert len(buying.candidates(10)) == 2


# ---- a whole run, against a fake Discogs + MusicBrainz -----------------------------------------------------------------------

VERSIONS = {"versions": [
    {"id": 101, "title": "Dirt", "format": "LP, Album", "label": "Columbia", "country": "Europe", "released": "1992", "major_formats": ["Vinyl"]},
    {"id": 102, "title": "Dirt", "format": "LP, Album", "label": "Columbia", "country": "US", "released": "1992", "major_formats": ["Vinyl"]},
    {"id": 103, "title": "Dirt", "format": "LP, Album, RE, Ltd, Red", "label": "Columbia", "country": "Europe", "released": "2022", "major_formats": ["Vinyl"]},
    {"id": 104, "title": "Dirt", "format": "2xLP, Album, RE, Gold", "label": "MOV", "country": "Europe", "released": "2017", "major_formats": ["Vinyl"]},
    {"id": 105, "title": "Dirt", "format": "LP, Album, Reissue", "label": "Columbia", "country": "Europe", "released": "2024", "major_formats": ["Vinyl"]},   # colour only in its words
    {"id": 106, "title": "Dirt", "format": "LP, Album, Test Pressing, White Label", "label": "Columbia", "country": "US", "released": "1992", "major_formats": ["Vinyl"]},
], "pagination": {"pages": 1}}
PRICES = {101: (95.0, 3, 120.0), 102: (60.0, 1, 55.0), 103: (24.0, 12, 28.0), 104: (31.0, 5, 30.0), 105: (20.0, 4, 26.0)}


def _fake_discogs(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    assert request.headers["Authorization"] == "Discogs token=tok"
    if path == "/masters/555":
        return httpx.Response(200, json={"id": 555, "year": 1992})
    if path == "/masters/555/versions":
        return httpx.Response(200, json=VERSIONS)
    if path == "/database/search" and request.url.params.get("type") == "release":     # the pressings' own words (colours)
        return httpx.Response(200, json={"results": [{"id": 105, "master_id": 555, "formats": [{"name": "Vinyl", "text": "Blue Translucent"}]},
                                                     {"id": 999, "master_id": 1, "formats": [{"name": "Vinyl", "text": "Red"}]}], "pagination": {"pages": 1}})
    if path == "/database/search":
        title = request.url.params.get("release_title")
        return httpx.Response(200, json={"results": [{"id": 777, "title": "Alice In Chains - Jar Of Flies"}] if title == "Jar of Flies" else []})
    if path == "/masters/777":
        return httpx.Response(200, json={"id": 777, "year": 1994})
    if path == "/masters/777/versions":
        return httpx.Response(200, json={"versions": [], "pagination": {"pages": 1}})
    if path.startswith("/marketplace/stats/"):
        p, n, _ = PRICES[int(path.rsplit("/", 1)[1])]
        assert request.url.params.get("curr_abbr") == "GBP"
        return httpx.Response(200, json={"lowest_price": {"currency": "GBP", "value": p}, "num_for_sale": n})
    if path.startswith("/marketplace/price_suggestions/"):
        return httpx.Response(200, json={"Very Good Plus (VG+)": {"currency": "GBP", "value": PRICES[int(path.rsplit("/", 1)[1])][2]}})
    return httpx.Response(404, json={})


def test_a_run_prices_originals_and_coloured_editions_and_the_limit_applies_live(lib, monkeypatch):
    _token("35")
    monkeypatch.setattr(discogs, "transport", httpx.MockTransport(_fake_discogs))
    monkeypatch.setattr(discogs, "MIN_GAP", 0)

    async def mb_get(path, **params):
        assert path == "release-group/rg-dirt" and params == {"inc": "url-rels"}       # Dirt: MusicBrainz knows its master
        return {"relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/master/555"}}]}
    async def mb_none(path, **params):
        return {"relations": []}
    monkeypatch.setattr(musicbrainz.client, "get", lambda path, **p: (mb_get if "dirt" in path else mb_none)(path, **p))

    data = asyncio.run(buying.gather(10))
    dirt, jar = data["albums"]
    assert (dirt["master_id"], dirt["match"]) == (555, "exact")
    assert (jar["master_id"], jar["match"]) == (777, "probable")         # found by an exact artist + title search
    assert [p["id"] for p in dirt["original"]] == [101, 102]                         # not the 1992 test pressing
    assert [p["id"] for p in dirt["coloured"]] == [103, 104, 105]                    # 105's colour was only in its own words
    assert next(p for p in dirt["coloured"] if p["id"] == 105)["colour"] == "Blue Translucent"

    db.cache_put(buying.job.id, data)
    v = buying.view(data)["albums"][0]
    assert v["original"]["best"] is None and v["original"]["cheapest"]["id"] == 102   # VG+ guides £55 and £120: over £35
    assert v["coloured"]["best"]["id"] == 105                                          # VG+ guide £26 (the red's is £28)
    assert v["coloured"]["best"]["sell_url"].endswith("ships_from=United+Kingdom&currency=GBP&sort=price%2Casc")

    _token("100")                                                                       # a higher limit applies without a new fetch
    v = buying.view(data)["albums"][0]
    assert v["original"]["best"]["id"] == 102                                           # VG+ guide £55: now within £100
    _token("30")
    assert buying.view(data)["albums"][0]["coloured"]["best"]["id"] == 105


def test_the_page_endpoint_and_dismiss(lib, owner_client, monkeypatch):
    _token("35")
    db.cache_put(buying.job.id, {"albums": [{"album_id": 10, "artist": "Alice In Chains", "title": "Dirt", "mbid": "rg-dirt", "plays": 120, "recent": 20,
                                             "score": 160, "match": "exact", "master_url": "https://www.discogs.com/master/555", "original_year": 1992,
                                             "counts": {"original": 2, "coloured": 2}, "original": [], "coloured": []}], "priced_at": 1, "unmatched": 0})
    r = owner_client.get("/api/owner/music/buying").json()
    assert [a["title"] for a in r["albums"]] == ["Dirt"] and r["max_price"] == 35 and r["ready"]
    assert owner_client.post("/api/owner/music/buying/10/dismiss", json={}).json() == {"ok": True}
    assert owner_client.get("/api/owner/music/buying").json()["albums"] == []
    owner_client.post("/api/owner/music/buying/10/dismiss", json={"undo": True})
    assert len(owner_client.get("/api/owner/music/buying").json()["albums"]) == 1


def test_the_token_is_a_secret_and_settings_are_checked():
    _token()
    assert discogs.service.public()["values"]["api_key"] == ""            # never sent back to the browser
    assert discogs.service.secrets() == ["tok"]
    with pytest.raises(ValueError):
        discogs.service.merge({}, {"max_price": "lots"})
    assert discogs.service.merge({}, {"max_price": "£40"})["max_price"] == "40"


def test_a_token_pasted_with_its_env_name_still_works(owner_client, monkeypatch):
    # what happened live: the whole .env line was pasted into Settings
    assert discogs.clean_token("DISCOGS_TOKEN=abc123") == "abc123"
    assert discogs.clean_token(' "abc123" ') == "abc123"
    seen = {}

    def fake(request):
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={"username": "mike"})
    monkeypatch.setattr(discogs, "transport", httpx.MockTransport(fake))
    s = store.settings()
    s["services"]["discogs"] = {"enabled": True, "api_key": "DISCOGS_TOKEN=abc123"}      # stored before the clean-up existed
    store.put_settings(s)
    assert asyncio.run(discogs.service.test()) == "Working — signed in as mike"
    assert seen["auth"] == "Discogs token=abc123"
    assert "abc123" in discogs.service.secrets()                                        # the event log blanks the bare token too
    r = owner_client.put("/api/owner/music/settings", json={"services": {"discogs": {"api_key": "DISCOGS_TOKEN=xyz789"}}})
    assert r.status_code == 200 and store.service_cfg("discogs")["api_key"] == "xyz789"   # saved clean


def test_check_prices_now(lib, owner_client, monkeypatch):
    queued = []
    monkeypatch.setattr(owner_client.app.state.scheduler, "refresh", lambda wid: queued.append(wid))
    assert owner_client.post("/api/owner/music/buying/refresh", json={}).status_code == 422     # no token yet
    _token()
    assert owner_client.post("/api/owner/music/buying/refresh", json={}).status_code == 202
    assert queued == ["music-buying"]
    owner_client.put("/api/owner/music/settings", json={"services": {"discogs": {"max_price": "50"}}})
    assert queued == ["music-buying", "music-buying"]                                          # a settings change checks again
    db.cache_error(buying.job.id, "RuntimeError: Discogs didn't accept the token")
    r = owner_client.get("/api/owner/music/buying").json()
    assert r["error"] == "Discogs didn't accept the token" and r["running"] is False


def test_preordered_albums_and_compilations_are_left_out(lib, monkeypatch):
    # on Confirmed pre-orders, spelled the way a shop or a hand-typed entry spells it
    now = int(__import__("time").time())
    with db.db() as con:
        con.execute("DELETE FROM feed_items WHERE source = 'test-buying'")
        con.execute("INSERT INTO feed_items (kind, source, external_id, artist, title, status, first_seen, last_seen) "
                    "VALUES ('vinyl', 'test-buying', 'x1', 'Alice in Chains', 'Dirt (30th Anniversary Edition)', 'purchased', ?, ?)", (now, now))
    sk = {}
    assert [a["title"] for a in buying.candidates(10, sk)] == ["Jar of Flies"]
    assert sk["preordered"] == ["Alice In Chains — Dirt"]
    with db.db() as con:
        con.execute("DELETE FROM feed_items WHERE source = 'test-buying'")

    _token()
    monkeypatch.setattr(discogs, "transport", httpx.MockTransport(_fake_discogs))
    monkeypatch.setattr(discogs, "MIN_GAP", 0)
    async def mb(path, **params):     # MusicBrainz calls Dirt a compilation here (soundtracks would stay)
        return {"secondary-types": ["Compilation"], "relations": [{"type": "discogs", "url": {"resource": "https://www.discogs.com/master/555"}}]}
    monkeypatch.setattr(musicbrainz.client, "get", mb)
    data = asyncio.run(buying.gather(10))
    assert data["skipped"]["compilations"] == ["Alice In Chains — Dirt", "Alice In Chains — Jar of Flies"] and data["albums"] == []


def test_hidden_albums_are_listed_by_name_and_can_come_back(lib):
    buying.dismiss(10)
    v = buying.view({"albums": []})
    assert v["hidden"] == [{"album_id": 10, "name": "Alice In Chains — Dirt"}]
    buying.dismiss(10, undo=True)
    assert buying.view({"albums": []})["hidden"] == []
