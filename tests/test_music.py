"""The Music page: settings and secrets, your library, building and saving playlists, rotation, setlists, matching.

Nothing here talks to Spotify, Last.fm, Setlist.fm or MusicBrainz: a small fake Music Companion database stands in for
your listening history, and the outside services are replaced with fakes."""
import asyncio
import functools
import json
import sqlite3
import time
from datetime import date, datetime, timedelta

import pytest

from app import config, db, logbook
from app.music import gigs, ingredients, library, matcher, playlists, player, store
from app.music.rotator import Rotator
from app.music.services import lastfm, setlistfm
from app.music.services import spotify as sp
from app.music.services.base import ServiceError

API = "/api/owner/music"
TODAY = date.today()
OLD = (TODAY - timedelta(days=800)).isoformat()
RECENT = (TODAY - timedelta(days=10)).isoformat()


def sync(fn):
    """Run an async test on its own event loop (this suite has no async test plugin; fixtures still resolve)."""
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


# ---- a fake Music Companion ---------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE artists (id INTEGER PRIMARY KEY, name TEXT, mbid TEXT);
CREATE TABLE albums (id INTEGER PRIMARY KEY, mbid TEXT, artist_id INTEGER, title TEXT, year INTEGER);
CREATE TABLE album_artists (album_id INTEGER, artist_id INTEGER, position INTEGER);
CREATE TABLE songs (id INTEGER PRIMARY KEY, artist_id INTEGER, album_id INTEGER, title TEXT, mbid TEXT);
CREATE TABLE scrobbles (id INTEGER PRIMARY KEY, song_id INTEGER, artist_id INTEGER, album_id INTEGER, played_at TEXT);
CREATE TABLE vinyl_holdings (id INTEGER PRIMARY KEY, album_id INTEGER, display_title TEXT, release_year INTEGER, pressing_year INTEGER);
CREATE TABLE vinyl_details (holding_id INTEGER PRIMARY KEY, tracklist TEXT);
CREATE TABLE album_tracklists (album_id INTEGER, position INTEGER, title TEXT, recording_mbid TEXT);
CREATE TABLE venues (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
CREATE TABLE setlists (id INTEGER PRIMARY KEY, artist_id INTEGER, venue_id INTEGER, event_date TEXT, tour_name TEXT, raw_artist_text TEXT);
CREATE TABLE setlist_songs (id INTEGER PRIMARY KEY, setlist_id INTEGER, song_id INTEGER, position INTEGER, raw_song_text TEXT, is_cover INTEGER DEFAULT 0, cover_of_artist_text TEXT);
CREATE TABLE genres (id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE album_genres (album_id INTEGER, genre_id INTEGER);
"""


def _fake_library(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO artists VALUES (?, ?, ?)", [(1, "Gojira", "gj"), (2, "Mastodon", "ma"), (3, "Architects", None), (4, "Various", None)])
    con.executemany("INSERT INTO albums VALUES (?, ?, ?, ?, ?)", [(10, "rg-magma", 1, "Magma", 2016), (20, "rg-skye", 2, "Crack the Skye", 2009), (30, None, 3, "Holy Hell", 2018), (40, None, 4, "Hits", 2000)])
    con.executemany("INSERT INTO songs (id, artist_id, album_id, title) VALUES (?, ?, ?, ?)", [
        (100, 1, 10, "Stranded"), (101, 1, 10, "Silvera"), (200, 2, 20, "Oblivion"), (300, 3, 30, "Hereafter"), (301, 3, 30, "Doomsday"), (400, 4, 40, "Machine Music")])
    rows = []
    def plays(song, artist, album, n, when):
        rows.extend((song, artist, album, f"{when}T12:00:00+00:00") for _ in range(n))
    plays(100, 1, 10, 60, RECENT); plays(101, 1, 10, 40, RECENT); plays(200, 2, 20, 50, RECENT)
    plays(300, 3, 30, 139, OLD); plays(301, 3, 30, 102, OLD); plays(400, 4, 40, 80, OLD)
    con.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at) VALUES (?, ?, ?, ?)", rows)
    con.execute("INSERT INTO vinyl_holdings VALUES (1, 10, 'Magma', 2016, 2016)")
    con.execute("INSERT INTO vinyl_details VALUES (1, ?)", (json.dumps([{"position": "A1", "title": "The Shooting Star", "type": "track"},
                                                                      {"position": "A2", "title": "Silvera", "type": "track"},
                                                                      {"position": "B1", "title": "Stranded", "type": "track"}]),))
    con.executemany("INSERT INTO album_tracklists (album_id, position, title) VALUES (?, ?, ?)", [(20, 1, "Oblivion"), (20, 2, "Divinations"), (20, 3, "Quintessence"), (10, 1, "The Shooting Star"), (10, 2, "Stranded")])
    con.execute("INSERT INTO venues VALUES (1, 'Co-op Live', 'Manchester')")
    con.execute("INSERT INTO setlists VALUES (7, 1, 1, '2026-07-03', 'Tour', 'Gojira')")
    con.executemany("INSERT INTO setlist_songs (setlist_id, position, raw_song_text, is_cover, cover_of_artist_text) VALUES (7, ?, ?, ?, ?)",
                    [(1, "Silvera", 0, None), (2, "Stranded", 0, None), (3, "Ah! Ça ira!", 1, "Traditional")])
    con.execute("INSERT INTO genres VALUES (1, 'progressive metal')")
    con.executemany("INSERT INTO album_genres VALUES (?, 1)", [(10,), (20,)])
    con.commit(); con.close()


@pytest.fixture(autouse=True)
def _clean(client):
    with db.db() as con:
        for t in ("music_playlist_tracks", "music_playlist_history", "music_playlist_bans", "music_playlists", "music_matches", "music_cache", "music_feedback"):
            con.execute(f"DELETE FROM {t}")
        con.execute("DELETE FROM widget_settings WHERE widget LIKE 'music%'")
    library.forget()
    yield
    library.forget()


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    path = tmp_path / "music.sqlite"
    _fake_library(path)
    monkeypatch.setattr(config, "MUSIC_DB", path)
    library.forget()
    return path


class FakeSpotify:
    """Stands in for the Spotify Web API client."""
    def __init__(self):
        self.calls, self.made = [], 0
        self.catalogue = {}                    # artist -> their songs, for an artist-only search (Spotify's 10 at most)
        self.albums = {}                       # artist -> their albums

    async def search(self, q, kind="track", limit=10):
        self.calls.append(("search", q))
        if kind == "album":
            artist = q.split('artist:"')[1].split('"')[0]
            want = q.split('album:"')[1].split('"')[0] if 'album:"' in q else None
            return [{"uri": f"spotify:album:{abs(hash((artist, a))) % 10**12}", "name": a, "album_type": "album", "artists": [{"name": artist}]}
                    for a in self.albums.get(artist, []) if want is None or want.lower() in a.lower()][:limit]
        if q.startswith('artist:"') and 'track:"' not in q:
            artist = q.split('"')[1]
            return [{"uri": f"spotify:track:{abs(hash((artist, t))) % 10**12}", "name": t, "artists": [{"name": artist}],
                     "album": {"name": "x", "album_type": "album"}} for t in self.catalogue.get(artist, [])[:limit]]
        title = q.split('track:"')[1].split('"')[0] if 'track:"' in q else q
        artist = q.split('artist:"')[1].split('"')[0] if 'artist:"' in q else ""
        if "Divinations" in q or not artist:
            return []
        return [{"uri": f"spotify:track:{abs(hash(title)) % 10**12}", "name": title, "artists": [{"name": artist}], "album": {"name": "x", "album_type": "album"}}]

    async def create_playlist(self, name, public, description):
        self.made += 1
        self.calls.append(("create", name, public))
        return {"id": f"pl{self.made}", "external_urls": {"spotify": f"https://open.spotify.com/playlist/pl{self.made}"}}

    async def update_playlist(self, pid, name, public, description):
        self.calls.append(("update", pid, name, public))

    async def set_items(self, pid, uris):
        self.calls.append(("items", pid, len(uris)))


@pytest.fixture()
def spotify(monkeypatch):
    fake = FakeSpotify()
    for name in ("search", "create_playlist", "update_playlist", "set_items"):
        monkeypatch.setattr(sp.client, name, getattr(fake, name))
    store.put_settings({**store.settings(), "services": {"spotify": {"enabled": True, "client_id": "cid1234567", "client_secret": "csecret1234567"}}})
    store.put_auth({"access_token": "acc-token-123456", "refresh_token": "ref-token-123456", "expires_at": time.time() + 3600, "authorised_at": int(time.time()), "user": {"id": "mike"}})
    return fake


# ---- settings and secrets ----------------------------------------------------------------------------------------

def test_guests_cannot_reach_the_music_api(guest_client):
    assert guest_client.get(API).status_code == 403
    assert guest_client.put(f"{API}/settings", json={}, headers={"X-Hub": "1"}).status_code == 403


def test_service_keys_are_saved_but_never_sent_back(owner_client):
    r = owner_client.put(f"{API}/settings", json={"services": {"lastfm": {"api_key": "secret-lastfm-key", "username": "mike"}, "spotify": {"client_id": "abc", "client_secret": "shh-secret-123"}}})
    assert r.status_code == 200, r.text
    body = owner_client.get(API).text
    assert "secret-lastfm-key" not in body and "shh-secret-123" not in body
    svc = {s["id"]: s for s in owner_client.get(API).json()["services"]}
    assert svc["lastfm"]["has"]["api_key"] and svc["lastfm"]["values"]["username"] == "mike" and svc["lastfm"]["configured"]
    owner_client.put(f"{API}/settings", json={"services": {"lastfm": {"api_key": "", "username": "mike2"}}})       # blank secret = keep it
    assert store.service_cfg("lastfm")["api_key"] == "secret-lastfm-key"
    assert "secret-lastfm-key" not in logbook.redact("GET ?api_key=x secret-lastfm-key")


@pytest.mark.parametrize("body", [{"services": {"nope": {}}}, {"services": {"lastfm": {"colour": "red"}}}, {"rotate_at": "25:00"}, {"desktop_command": "\"unbalanced"}, {"prefix": "bad\x01"}])
def test_bad_settings_are_refused(owner_client, body):
    assert owner_client.put(f"{API}/settings", json=body).status_code == 422


def test_spotify_login_link_needs_the_app_keys_and_its_state_is_single_use(owner_client):
    assert owner_client.post(f"{API}/spotify/login").status_code == 422
    owner_client.put(f"{API}/settings", json={"services": {"spotify": {"client_id": "cid", "client_secret": "sec"}}})
    r = owner_client.post(f"{API}/spotify/login").json()
    assert r["url"].startswith("https://accounts.spotify.com/authorize?") and "redirect_uri=http%3A%2F%2F127.0.0.1" in r["url"] and r["redirect_uri"].endswith("/music/spotify/callback")
    page = owner_client.get("/music/spotify/callback", params={"code": "x", "state": "forged"})
    assert page.status_code == 400 and "expired or was already used" in page.text


def test_a_spotify_token_never_reaches_the_event_log(spotify):
    assert "acc-token-123456" not in logbook.redact("Authorization failed for acc-token-123456")


# ---- your library -----------------------------------------------------------------------------------------------

def test_library_queries(lib):
    forgotten = library.history(min_plays=20, not_played_days=365)
    assert [t["title"] for t in forgotten] == ["Hereafter", "Doomsday"]                       # "Various" is never a real artist
    assert "139 plays" in forgotten[0]["why"]
    shelf = {t["title"]: t for t in library.shelf("mixed")}
    assert shelf["The Shooting Star"]["why"].endswith("side A, track 1 · never streamed") and shelf["Stranded"]["plays"] == 60
    deep = {t["title"] for t in library.deep_cuts(top_n=5, days=365)}
    assert {"Divinations", "Quintessence", "The Shooting Star"} <= deep and "Oblivion" not in deep and "Stranded" not in deep
    assert [t["title"] for t in library.by_genre("Progressive Metal")][:1] == ["Stranded"]
    head, tracks = library.show_tracks(7)
    assert head["venue"] == "Co-op Live" and tracks[2]["artist"] == "Traditional"
    assert library.fact("Gojira", "Stranded").startswith("your 61st play") and "Magma LP" in library.fact("Gojira", "Stranded")
    assert library.key("The Architects", "Hereafter - 2018 Remaster") == library.key("Architects", "Hereafter")


def test_ingredient_settings_are_checked():
    assert ingredients.check([{"kind": "history", "share": 500, "params": {"min_plays": -5}}])[0] == {"kind": "history", "share": 100, "params": {"min_plays": 1, "not_played_days": 365, "played_within_days": 0}}
    for bad in ([{"kind": "nope"}], [{"kind": "shelf", "params": {"mode": "all"}}], [{"kind": "capsule", "params": {"start": "2020-02-01", "end": "2020-01-01"}}], "x", [{"kind": "deep"}] * 9):
        with pytest.raises(ValueError):
            ingredients.check(bad)


# ---- playlists ---------------------------------------------------------------------------------------------------

@sync
async def test_building_mixes_ingredients_and_follows_the_rules(lib):
    pl = playlists.from_template("blank", name="Mix", ings=[{"kind": "history", "share": 50, "params": {"min_plays": 20, "not_played_days": 0}},
                                                          {"kind": "deep", "share": 50, "params": {}}], rules={"len": 6, "per": 2})
    r = await playlists.build(pl["id"], seed=1)
    rows = playlists.tracks(pl["id"])
    assert r["tracks"] == len(rows) == 6
    per_artist = {}
    for t in rows:
        per_artist[t["artist"]] = per_artist.get(t["artist"], 0) + 1
    assert max(per_artist.values()) <= 2 and len({t["key"] for t in rows}) == 6
    assert {t["src"] for t in rows} == {"history", "deep"}


@sync
async def test_locked_tracks_survive_and_removed_ones_stay_out(lib):
    pl = playlists.from_template("forgotten", rules={"len": 2, "per": 0, "norep": 0})
    await playlists.build(pl["id"], seed=2)
    first = playlists.tracks(pl["id"])
    playlists.edit_tracks(pl["id"], "lock", 0)
    playlists.edit_tracks(pl["id"], "remove", 1)
    for seed in range(5):
        await playlists.build(pl["id"], seed=seed)
        rows = playlists.tracks(pl["id"])
        assert rows[0]["key"] == first[0]["key"] and rows[0]["locked"]
        assert first[1]["key"] not in {t["key"] for t in rows}


def test_bad_designs_are_refused(owner_client):
    pl = owner_client.post(f"{API}/playlists", json={"template": "deep"}).json()
    for body in ({"name": "", "mode": "fixed"}, {"name": "x", "mode": "rot", "schedule": {"every": "hourly"}}, {"name": "x", "mode": "rot", "schedule": {"every": "monthly", "day": 31}},
                 {"name": "x", "mode": "fixed", "rules": {"order": "random"}}, {"name": "x", "mode": "fixed", "ings": [{"kind": "nope"}]}):
        assert owner_client.put(f"{API}/playlists/{pl['id']}", json=body).status_code == 422, body
    assert owner_client.post(f"{API}/playlists", json={"template": "nope"}).status_code == 422
    assert owner_client.get(f"{API}/playlists/999999").status_code == 404


@sync
async def test_saving_creates_once_then_replaces_and_remembers_what_went_in(lib, spotify):
    pl = playlists.from_template("blank", name="Deep", ings=[{"kind": "deep", "share": 100, "params": {}}], rules={"len": 5, "per": 0})
    r = await playlists.build(pl["id"], seed=3)
    assert r["missing"] == 1                                                   # "Divinations" isn't on (fake) Spotify
    saved = await playlists.save(pl["id"])
    assert r["tracks"] == 3                                                    # every deep cut the little library has
    assert saved["tracks"] == 2 and saved["skipped"] == 1 and saved["url"].endswith("/pl1")
    assert ("create", "Citadel · Deep", True) in spotify.calls and ("items", "pl1", 2) in spotify.calls
    searches = len([c for c in spotify.calls if c[0] == "search"])
    await playlists.build(pl["id"], seed=4)
    await playlists.save(pl["id"])
    assert spotify.made == 1 and ("update", "pl1", "Citadel · Deep", True) in spotify.calls          # same playlist, replaced
    assert len([c for c in spotify.calls if c[0] == "search"]) - searches <= 1                       # matches are remembered (only the missing one is asked again)
    with db.db() as con:
        assert con.execute("SELECT COUNT(DISTINCT run) FROM music_playlist_history WHERE playlist_id = ?", (pl["id"],)).fetchone()[0] >= 1


@sync
async def test_saving_needs_spotify(lib):
    pl = playlists.from_template("deep")
    await playlists.build(pl["id"])
    with pytest.raises(ServiceError):
        await playlists.save(pl["id"])


def test_manual_matches_are_kept_for_every_playlist(lib):
    pl = playlists.from_template("deep")
    with db.db() as con:
        playlists._write_tracks(con, pl["id"], [{"key": "mastodon|divinations", "title": "Divinations", "artist": "Mastodon"}])
    playlists.set_match(pl["id"], 0, "spotify:track:abcdefghij1234567890ab", "Divinations", "Mastodon")
    assert playlists.tracks(pl["id"])[0]["uri"] == "spotify:track:abcdefghij1234567890ab"
    assert matcher.remembered(["mastodon|divinations"])["mastodon|divinations"]["status"] == "manual"
    assert matcher.forget_all() == 0                                                                  # a hand-picked match isn't forgotten
    with pytest.raises(playlists.PlaylistError):
        playlists.set_match(pl["id"], 0, "javascript:alert(1)")


def test_the_matcher_prefers_the_right_artist_and_the_studio_version():
    items = [{"name": "Hereafter - Live", "artists": [{"name": "Architects"}], "album": {"name": "Live at Brixton"}, "uri": "live"},
             {"name": "Hereafter", "artists": [{"name": "Somebody Else"}], "album": {"name": "x"}, "uri": "wrong"},
             {"name": "Hereafter - 2018 Remaster", "artists": [{"name": "Architects"}], "album": {"name": "Holy Hell", "album_type": "album"}, "uri": "right"}]
    assert matcher.best(items, "Architects", "Hereafter")["uri"] == "right"
    assert matcher.best(items, "Architects", "Hereafter (live)")["uri"] == "live"
    assert matcher.best(items, "Gojira", "Hereafter") is None


# ---- rotation ------------------------------------------------------------------------------------------------------

def _at(y, m, d, h=12):
    return int(datetime(y, m, d, h, tzinfo=config.TZ).timestamp())


def test_when_rotating_playlists_next_run():
    base = {"mode": "rot", "spotify_id": "x", "built_at": None}
    weekly = {**base, "schedule": {"every": "weekly", "day": 0, "at": "06:00"}, "saved_at": _at(2026, 10, 1)}            # a Thursday
    assert datetime.fromtimestamp(playlists.next_run(weekly), config.TZ) == datetime(2026, 10, 5, 6, 0, tzinfo=config.TZ)
    monthly = {**base, "schedule": {"every": "monthly", "day": 1, "at": "07:30"}, "saved_at": _at(2026, 10, 1, 8)}
    assert datetime.fromtimestamp(playlists.next_run(monthly), config.TZ) == datetime(2026, 11, 1, 7, 30, tzinfo=config.TZ)
    ended = {**base, "schedule": {"every": "daily", "at": "06:00", "until": "2026-10-01"}, "saved_at": _at(2026, 10, 1, 9)}
    assert playlists.next_run(ended) is None
    assert playlists.next_run({**weekly, "mode": "fixed"}) is None and playlists.next_run({**weekly, "spotify_id": None}) is None


@sync
async def test_the_rotator_rebuilds_whats_due_and_backs_off_after_a_failure(lib, spotify, monkeypatch):
    pl = playlists.from_template("forgotten", rules={"len": 2})
    await playlists.build(pl["id"])
    await playlists.save(pl["id"])
    with db.db() as con:
        con.execute("UPDATE music_playlists SET saved_at = ? WHERE id = ?", (int(time.time()) - 8 * 86400, pl["id"]))
    rot = Rotator()
    assert await rot.tick() == ["Forgotten Favourites"]
    assert await rot.tick() == []                                              # just rotated: not due again
    with db.db() as con:
        con.execute("UPDATE music_playlists SET saved_at = ? WHERE id = ?", (int(time.time()) - 8 * 86400, pl["id"]))
    async def boom(pid):
        raise ServiceError("Spotify is down")
    monkeypatch.setattr(playlists, "rotate", boom)
    assert await rot.tick() == [] and await rot.tick() == []                  # failed once, then waits rather than retrying every minute


# ---- setlists --------------------------------------------------------------------------------------------------------

def _set(date_, songs, encore=()):
    return {"id": date_, "artist": "Amon Amarth", "date": date_, "venue": "V", "city": "C", "tour": "Tour", "url": "",
            "songs": [{"title": s, "cover_of": None, "set": "Encore" if s in encore else ""} for s in songs]}


@sync
async def test_a_predicted_setlist_counts_recent_shows(lib, monkeypatch):
    shows = [_set(f"2026-09-{d:02d}", ["Guardians", "Raven", "Shield Wall", "Rare", "Horns", "Twilight"], encore=("Horns", "Twilight")) for d in range(1, 9)]
    shows += [_set("2026-08-30", ["Guardians", "Raven", "Shield Wall", "Other", "Horns", "Twilight"]), _set("2026-08-29", ["x"] * 3)]   # a too-short one is ignored
    async def recent(artist, mbid, pages=2):
        return shows
    async def mbid(artist):
        return None
    monkeypatch.setattr(setlistfm, "recent", recent)
    monkeypatch.setattr(gigs, "artist_mbid", mbid)
    p = await gigs.predict("Amon Amarth")
    by = {s["title"]: s for s in p["songs"]}
    assert p["shows"] == 9 and by["Guardians"]["tag"] == "opener" and by["Twilight"]["tag"] == "closer" and by["Horns"]["tag"] == "encore"
    assert by["Rare"]["pct"] == 89 and "Other" not in by                      # played at 1 of 9 shows: below the cut
    assert [s["title"] for s in p["songs"]][0] == "Guardians" and p["songs"][-1]["title"] == "Twilight"


def test_setlist_links_are_recognised():
    assert setlistfm.id_from_url("https://www.setlist.fm/setlist/evanescence/2026/first-direct-bank-arena-leeds-england-434cc7e7.html") == "434cc7e7"
    assert setlistfm.id_from_url("https://example.com/whatever") is None


# ---- the page's other endpoints -----------------------------------------------------------------------------------

def test_overview_and_playlist_round_trip(owner_client, lib):
    ov = owner_client.get(API).json()
    assert {s["id"] for s in ov["services"]} >= {"spotify", "lastfm", "setlistfm", "musicbrainz", "companion"}
    assert ov["library"]["available"] and ov["library"]["owned"] == {"gojira": 1}
    made = owner_client.post(f"{API}/playlists", json={"template": "shelf"}).json()
    built = owner_client.post(f"{API}/playlists/{made['id']}/build").json()
    assert built["tracks"] == 2 and built["playlist"]["tracks"][0]["src"] == "shelf"       # three Gojira tracks, but the template allows two per artist
    assert owner_client.post(f"{API}/playlists/{made['id']}/save").status_code == 502          # Spotify not connected: a message, not a crash
    assert owner_client.delete(f"{API}/playlists/{made['id']}").status_code == 200
    assert owner_client.get(f"{API}/player").json() == {"ready": False, "why": "Spotify isn’t set up"}


def test_gig_playlists_from_a_show_you_were_at(owner_client, lib):
    gigs_ = owner_client.get(f"{API}/gigs").json()
    assert gigs_["shows"][0]["artist"] == "Gojira"
    r = owner_client.post(f"{API}/gigs/setlist-playlist", json={"show_id": 7}).json()
    assert r["playlist"]["name"].startswith("Gojira @ Co-op Live") and [t["title"] for t in r["playlist"]["tracks"]] == ["Silvera", "Stranded", "Ah! Ça ira!"]
    assert owner_client.get(f"{API}/gigs").json()["made"] == {"show:7": r["playlist"]["id"]}
    assert owner_client.post(f"{API}/gigs/setlist-playlist", json={"url": "https://example.com/x"}).status_code == 422


def test_discover_feedback_follows_or_hides(owner_client, lib):
    from app import content
    assert owner_client.post(f"{API}/discover/feedback", json={"artist": "Gone Is Gone", "verdict": "follow"}).status_code == 200
    assert any(a["name"] == "Gone Is Gone" for a in content.list_artists())
    owner_client.post(f"{API}/discover/feedback", json={"artist": "Car Bomb", "verdict": "nope"})
    d = owner_client.get(f"{API}/discover").json()
    assert d["running"] is False and d["following"] == ["goneisgone"]               # no-network test run: nothing is worked out in the background
    assert "rediscover" not in d                                                # it has its own section now
    redis = owner_client.get(f"{API}/rediscover").json()
    assert redis["available"] and isinstance(redis["artists"], list)


@sync
async def test_player_reports_not_ready_without_spotify():
    assert (await player.state())["ready"] is False


# ---- Discover only shows what Spotify can play -----------------------------------------------------------------------

@sync
async def test_discover_shows_only_artists_spotify_can_play_and_last_fm_leads(lib, spotify, monkeypatch):
    from app.music import discover
    from app.music.services import lastfm, musicbrainz as mbs
    similar = {"Gojira": [{"name": "Dark Tranquillity", "match": 0.9}, {"name": "Obscure Demo Band", "match": 0.8}, {"name": "Mastodon", "match": 1.0}],
               "Mastodon": [{"name": "Kvelertak", "match": 0.6}]}
    async def sim(artist, limit=30):
        return similar.get(artist, [])
    async def top(artist, limit=10):
        return {"Dark Tranquillity": ["ThereIn", "Lost to Apathy"], "Kvelertak": ["Blodtørst"], "Obscure Demo Band": ["Demo 1", "Demo 2", "Demo 3"],
                "Side Project": ["Jam"]}.get(artist, [])
    async def rels(mbid):
        if mbid == "gj":
            return {"name": "Gojira", "type": "Group", "members": [{"name": "Joe", "mbid": "joe"}], "member_of": []}
        return {"name": "Joe", "type": "Person", "members": [], "member_of": [{"name": "Side Project", "mbid": "sp"}, {"name": "Gojira", "mbid": "gj"}]}
    real_search = spotify.search
    async def search(q, kind="track", limit=10):
        if "Obscure Demo Band" in q or "Side Project" in q:
            return []                                                      # Spotify hasn't got them
        return await real_search(q, kind, limit)
    monkeypatch.setattr(sp.client, "search", search)
    monkeypatch.setattr(lastfm, "similar", sim)
    monkeypatch.setattr(lastfm, "top_tracks", top)
    async def tags(artist, n=4):
        return {"Dark Tranquillity": ["melodic death metal", "swedish"]}.get(artist, [])
    async def album(artist):
        return {"Dark Tranquillity": "Damage Done"}.get(artist)
    monkeypatch.setattr(lastfm, "top_tags", tags)
    monkeypatch.setattr(lastfm, "top_album", album)
    monkeypatch.setattr(mbs, "artist_rels", rels)
    store.put_settings({**store.settings(), "services": {**store.settings()["services"], "lastfm": {"enabled": True, "api_key": "k" * 32}}})
    spotify.albums.update({"Dark Tranquillity": ["Projector", "Damage Done"], "Kvelertak": ["Meir"]})
    r = await discover.build("12m")
    names = [a["artist"] for a in r["artists"]]
    assert names == ["Dark Tranquillity", "Kvelertak"] and r["checked"]          # the unplayable ones are gone; Mastodon is already played
    assert sum(1 for c in spotify.calls if c[0] == "search") == 2                 # one Spotify search per artist shown
    assert all(a["album_uri"].startswith("spotify:album:") and "tracks" not in a for a in r["artists"])
    dt, kv = r["artists"]
    assert (dt["tags"], dt["album"]) == (["melodic death metal", "swedish"], "Damage Done")       # genre bubbles and their biggest album (Last.fm)
    assert kv["album"] == "Meir"                                                                  # Last.fm couldn't say: Spotify's first
    assert all(x["src"] != "you" for a in r["artists"] for x in a["reasons"])                     # no "your data" line any more
    assert discover.cached("12m") == r
    assert store.cache_get("spotify:album:obscuredemoband|", 10**9) == {}        # remembered as "not on Spotify"
    monkeypatch.setattr(discover, "SHOW_EACH", 1)                              # each group fills to its own limit
    assert [a["artist"] for a in (await discover.build("12m"))["artists"]] == ["Dark Tranquillity"]


@sync
async def test_discover_spreads_its_picks_and_groups_them_by_vibe(lib, monkeypatch):
    """Gojira (100 plays this year) is death metal, Mastodon (50) stoner rock — so two vibes, the smaller one first."""
    from app.music import discover
    from app.music.services import lastfm, musicbrainz as mbs
    similar = {"Gojira": [{"name": "Alpha", "match": 0.9}, {"name": "Bravo", "match": 0.8}, {"name": "Charlie", "match": 0.7}, {"name": "Dub Band", "match": 0.6}],
               "Mastodon": [{"name": "Alpha", "match": 0.5}, {"name": "Echo", "match": 0.4}, {"name": "Foxtrot", "match": 0.3}]}
    tagged = {"Gojira": ["death metal", "metal"], "Mastodon": ["metal", "stoner rock"], "Alpha": ["death metal"], "Bravo": ["melodic death metal"],
              "Charlie": ["metal"], "Dub Band": ["reggae", "dub"], "Echo": ["stoner rock"], "Foxtrot": ["desert rock", "rock"]}
    async def sim(artist, limit=30):
        return similar.get(artist, [])
    async def tags(artist, n=4):
        return tagged.get(artist, [])[:n]
    async def top(artist, limit=10):
        return ["Song"]
    async def album(artist):
        return None
    async def rels(mbid):
        return {"name": "", "type": "Group", "members": [], "member_of": []}
    for name, fake in (("similar", sim), ("top_tags", tags), ("top_tracks", top), ("top_album", album)):
        monkeypatch.setattr(lastfm, name, fake)
    monkeypatch.setattr(mbs, "artist_rels", rels)
    store.put_settings({**store.settings(), "services": {**store.settings()["services"], "lastfm": {"enabled": True, "api_key": "k" * 32}}})

    r = await discover.build("12m")
    alpha = r["artists"][0]
    assert alpha["artist"] == "Alpha" and alpha["match"] == 90                  # the closest link's own strength, not relative to #1
    assert alpha["reasons"][-1]["text"] == "Similar to Gojira, Mastodon"
    assert [a["artist"] for a in r["artists"]] == ["Alpha", "Bravo", "Charlie", "Dub Band", "Echo", "Foxtrot"]   # room for all: best first
    monkeypatch.setattr(discover, "SHOW_EACH", 4)
    r = await discover.build("12m")
    assert [a["artist"] for a in r["artists"]] == ["Alpha", "Bravo", "Echo", "Foxtrot"]   # Gojira leads to only two while others wait

    assert [(v["name"], v["seeds"]) for v in r["vibes"]] == [("Desert & Stoner Rock", ["Mastodon"]), ("Death & Melodeath", ["Gojira"])]
    desert, death = r["vibes"]
    assert [a["artist"] for a in desert["artists"]] == ["Echo", "Foxtrot"]       # Alpha is similar to Mastodon too, but it isn't stoner rock
    assert [a["artist"] for a in death["artists"]] == ["Alpha", "Bravo"]        # Dub Band's reggae; plain "metal" makes Charlie heavy metal
    assert death["artists"][0]["reasons"][-1]["text"] == "Similar to Gojira"    # only this vibe's seeds count here
    assert desert["share"] == round(100 * 50 / 150, 1)


@sync
async def test_discover_skips_a_spotify_hiccup_and_your_own_artists_side_names(lib, spotify, monkeypatch):
    from app.music import discover
    from app.music.services import lastfm, musicbrainz as mbs
    async def sim(artist, limit=30):
        return [{"name": "Gojira & Friends", "match": 1.0}, {"name": "Flaky", "match": 0.9}, {"name": "Steady", "match": 0.8}] if artist == "Gojira" else []
    async def top(artist, limit=10):
        return ["Song"]
    async def nothing(*a, **k):
        return None
    async def rels(mbid):
        return {"name": "", "type": "Group", "members": [], "member_of": []}
    real_search = spotify.search
    async def search(q, kind="track", limit=10):
        if "Flaky" in q:
            raise ServiceError("Spotify: An unexpected error occurred. Please try again later.")
        return await real_search(q, kind, limit)
    monkeypatch.setattr(sp.client, "search", search)
    for name, fake in (("similar", sim), ("top_tracks", top), ("top_album", nothing)):
        monkeypatch.setattr(lastfm, name, fake)
    async def tags(artist, n=4):
        return []
    monkeypatch.setattr(lastfm, "top_tags", tags)
    monkeypatch.setattr(mbs, "artist_rels", rels)
    store.put_settings({**store.settings(), "services": {**store.settings()["services"], "lastfm": {"enabled": True, "api_key": "k" * 32}}})
    spotify.albums["Steady"] = ["Steady State"]
    r = await discover.build("12m")
    assert [a["artist"] for a in r["artists"]] == ["Steady"]                   # one failed check skips Flaky, not everyone after it
    assert any("unexpected error" in e for e in r["errors"])


@sync
async def test_discover_leaves_out_solo_careers_and_side_projects(lib, monkeypatch):
    from app.music import discover
    from app.music.services import lastfm, musicbrainz as mbs
    sim = [{"name": "Joe Solo", "match": 1.0, "mbid": "joe"}, {"name": "Mostly Them", "match": 0.7, "mbid": "mostly"},
           {"name": "Same Crowd", "match": 0.97, "mbid": "crowd"}, {"name": "Once Had Their Drummer", "match": 0.88, "mbid": "fu"},
           {"name": "Big Family", "match": 1.0, "mbid": "big"},
           {"name": "Strangers", "match": 0.6, "mbid": "strangers"}, {"name": "No Id", "match": 0.5}]
    people = lambda *ids: [{"name": i, "mbid": i} for i in ids]
    rels_by = {"gj": {"type": "Group", "members": people("joe", "mario", "christian", "jean")},
               "joe": {"type": "Person", "members": []}, "mostly": {"type": "Group", "members": people("joe", "mario", "x")},
               "crowd": {"type": "Group", "members": people("joe", "a", "b", "c")}, "fu": {"type": "Group", "members": people("jean", "d", "e", "f")},
               "strangers": {"type": "Group", "members": people("g", "h")}, "big": {"type": "Group", "members": people("jean", *"klmnop")}}
    async def rels(mbid):
        return {"name": "", "member_of": [], **rels_by.get(mbid, {"type": "Group", "members": []})}
    async def similar(artist, limit=30):
        return sim if artist == "Gojira" else []
    async def top(artist, limit=10):
        return ["Song"]
    async def nothing(*a, **k):
        return None
    async def tags(artist, n=4):
        return []
    for name, fake in (("similar", similar), ("top_tracks", top), ("top_album", nothing), ("top_tags", tags)):
        monkeypatch.setattr(lastfm, name, fake)
    monkeypatch.setattr(mbs, "artist_rels", rels)
    store.put_settings({**store.settings(), "services": {**store.settings()["services"], "lastfm": {"enabled": True, "api_key": "k" * 32}}})
    r = await discover.build("12m")
    assert {a["artist"] for a in r["artists"]} == {"Once Had Their Drummer", "Strangers", "No Id", "Big Family"}   # 1 of 7: its own band

    async def down(mbid):
        raise RuntimeError("MusicBrainz is busy")
    monkeypatch.setattr(mbs, "artist_rels", down)                              # can't tell: nothing is hidden
    r = await discover.build("12m")
    assert len(r["artists"]) == 7, ([a["artist"] for a in r["artists"]], r["errors"])


def test_vibes_prefer_the_specific_tag():
    from app.music import vibes
    assert vibes.of(["rock", "ska", "pop"]) == "reggae" and vibes.of(["metal", "german"]) == "heavy" and vibes.of(["seattle"]) is None
    assert vibes.fits(["reggae", "rock"], "reggae") and not vibes.fits(["reggae", "rock"], "classic") and vibes.fits(["rock"], "classic")


def test_an_old_or_unchecked_discover_result_is_kept_until_the_nightly_job_redoes_it(spotify):
    from app.music import discover
    fresh = {"artists": [], "built_at": int(time.time()), "checked": True, "version": discover.VERSION}
    for stale in ({**fresh, "checked": False}, {**fresh, "version": discover.VERSION - 1}, {**fresh, "partial": True},
                  {**fresh, "built_at": int(time.time()) - discover.WEEK}):
        store.cache_put("discover:12m", stale)
        assert discover.cached("12m") == stale and discover.stale("12m")          # still shown (a restart costs Spotify nothing)
    store.cache_put("discover:12m", fresh)
    assert not discover.stale("12m")


@sync
async def test_a_player_command_with_a_non_json_success_body_is_a_success(spotify, monkeypatch):
    class Resp:
        status_code, content, headers = 200, b"OK", {}
        def json(self):
            raise ValueError("not json")
    class Http:
        async def request(self, *a, **k):
            return Resp()
    monkeypatch.setattr(sp.client, "_client", lambda: Http())
    assert await sp.client.api("PUT", "/me/player/pause") is None


# ---- "have you played this?" — Music Companion's rules ----------------------------------------------------------------------

def test_played_matches_the_way_music_companion_does(lib):
    con = sqlite3.connect(lib)
    con.executemany("INSERT INTO songs (id, artist_id, album_id, title) VALUES (?, ?, ?, ?)", [(201, 2, 20, "Quintessence - Live at the Aragon"), (202, 2, 20, "Divinations"),
                                                              (203, 2, 20, "The Czar - Usurper"), (204, 2, 20, "The Czar - Spiral")])
    con.executemany("INSERT INTO scrobbles (song_id, artist_id, album_id, played_at) VALUES (?, 2, 20, ?)", [(s, RECENT) for s in (201, 201, 201, 202, 202, 203, 204)])
    con.execute("INSERT INTO vinyl_holdings VALUES (2, 20, 'Crack the Skye', 2009, 2009)")
    con.execute("INSERT INTO vinyl_details VALUES (2, ?)", (json.dumps([{"position": t, "title": n, "type": "track"} for t, n in
                                                                      [("A1", "Oblivion '09"), ("A2", "Divinations (Instrumental)"), ("B1", "Quintessence"), ("B2", "The Czar")]]),))
    con.commit(); con.close()
    library.forget()
    rec = next(r for r in library.records() if r["album"] == "Crack the Skye")
    on = lambda t: library.lookup("Mastodon", t, album_id=rec["album_id"], listing=[x["title"] for x in rec["tracks"]]).get("plays", 0)   # noqa: E731
    assert on("Oblivion '09") == 50                  # a suffix on the record's title: still "Oblivion" (the "Total Hate '95" case)
    assert on("Quintessence") == 3                   # only the live version was ever played from this album: that's the track
    assert on("Divinations (Instrumental)") == 2     # the record calling it an instrumental means the song itself
    assert on("The Czar") == 0                       # two different songs fit — no guessing
    assert library.lookup("Mastodon", "Oblivion '09") == {}        # without the album, only exact (or spacing-blind) titles count
    assert library.norm_title("Tomorrow’s Dream – Héllo & Goodbye (2009 Remaster)") == "tomorrows dream – hello and goodbye".replace("–", "").replace("  ", " ")
    shelf = {t["title"]: t["plays"] for t in library.shelf("mixed") if t["artist"] == "Mastodon"}
    assert shelf["Oblivion"] == 50 and shelf["Quintessence - Live at the Aragon"] == 3 and shelf["The Czar"] == 0


@sync
async def test_lastfm_tags_leave_out_the_noise(monkeypatch):
    store.put_settings({**store.settings(), "services": {**store.settings()["services"], "lastfm": {"enabled": True, "api_key": "k" * 32}}})
    async def call(method, **params):
        assert method == "artist.getTopTags"
        return {"toptags": {"tag": [{"name": n, "count": c} for n, c in [("melodic death metal", 100), ("seen live", 90), ("Dark Tranquillity", 80),
                                                                             ("swedish", 60), ("metal", 50), ("rare tag", 3), ("death metal", 40), ("gothenburg", 30)]]}}
    monkeypatch.setattr(lastfm, "call", call)
    assert await lastfm.top_tags("Dark Tranquillity") == ["melodic death metal", "swedish", "metal", "death metal"]


def test_play_and_queue_are_albums_and_reuse_what_discover_found(owner_client, spotify, monkeypatch):
    from app.music import player
    queued, played = [], []
    async def api(method, path, params=None, json=None):
        if path == "/me/player/queue":
            queued.append(params["uri"])
        if path == "/me/player/play":
            played.append(json)
        return None
    async def target():
        return "dev1"
    async def album(artist):
        return "Damage Done"
    async def album_tracks(album_id):
        assert album_id == "alb1"
        return [{"uri": f"spotify:track:a{i}"} for i in range(12)]
    monkeypatch.setattr(sp.client, "api", api)
    monkeypatch.setattr(player, "_target", target)
    monkeypatch.setattr(lastfm, "top_album", album)
    monkeypatch.setattr(lastfm.service, "ready", lambda: True)
    monkeypatch.setattr(sp.client, "album_tracks", album_tracks)
    r = owner_client.post(f"{API}/queue", json={"kind": "album", "artist": "Dark Tranquillity", "title": "Damage Done", "uri": "spotify:album:alb1"}).json()
    assert (r["queued"], r["what"]) == (12, "Damage Done") and queued[0] == "spotify:track:a0"
    assert not [c for c in spotify.calls if c[0] == "search"]                              # Discover's album: no search
    assert owner_client.post(f"{API}/queue", json={"kind": "artist", "artist": "Dark Tranquillity"}).status_code == 422     # no top 10s
    spotify.albums["Dark Tranquillity"] = ["Projector", "Damage Done"]
    r = owner_client.post(f"{API}/play", json={"kind": "artist", "artist": "Dark Tranquillity"}).json()            # not out yet: biggest album
    assert r["what"] == "Damage Done" and played[-1]["context_uri"].startswith("spotify:album:")
    owner_client.post(f"{API}/play", json={"kind": "artist", "artist": "Dark Tranquillity"})
    assert len([c for c in spotify.calls if c[0] == "search"]) == 1                        # remembered: the second play searched nothing



def test_a_track_too_short_to_scrobble_is_never_a_deep_cut(lib):
    # Last.fm never counts a play of a track of 30 s or less, so it would sit in Deep cuts forever ("Embryo", 0:23)
    assert "Divinations" in {t["title"] for t in library.deep_cuts(top_n=5, days=365)}
    con = sqlite3.connect(lib)
    con.execute("CREATE TABLE track_lengths (album_id INTEGER, title TEXT, length_ms INTEGER, source TEXT, PRIMARY KEY (album_id, title))")
    con.execute("INSERT INTO track_lengths VALUES (20, 'Divinations', 23000, 'lastfm'), (20, 'Quintessence', 327000, 'lastfm')")
    con.commit()
    con.close()
    library.forget()
    deep = {t["title"] for t in library.deep_cuts(top_n=5, days=365)}
    assert "Divinations" not in deep and "Quintessence" in deep
