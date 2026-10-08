"""Staying within other people's limits: Spotify's pacing and its "wait until" (Retry-After), Last.fm backing off, and
Discover being worked out one basis at a time, overnight, without a restart throwing a result away.

Nothing here talks to a real service."""
import asyncio
import functools
import time

import pytest

from app import config, db
from app.music import discover, player, store
from app.music.rotator import Rotator
from app.music.services import lastfm
from app.music.services import spotify as sp
from app.music.services.base import ServiceError


def sync(fn):
    @functools.wraps(fn)
    def run(*a, **k):
        return asyncio.run(fn(*a, **k))
    return run


@pytest.fixture(autouse=True)
def _app(client):
    """The app has started (so its database exists)."""


@pytest.fixture()
def connected():
    store.put_settings({**store.settings(), "services": {**store.settings().get("services", {}),
                                                         "spotify": {"enabled": True, "client_id": "cid1234567", "client_secret": "csecret1234567"}}})
    store.put_auth({"access_token": "acc-token-123456", "refresh_token": "ref-token-123456", "expires_at": time.time() + 3600,
                    "authorised_at": int(time.time()), "user": {"id": "mike"}})
    db.put_settings(sp.COOLDOWN_KEY, {})
    sp.pacer.reset()
    yield
    db.put_settings(sp.COOLDOWN_KEY, {})
    sp.pacer.reset()


class Resp:
    def __init__(self, status, headers=None, body=b"{}"):
        self.status_code, self.headers, self.content = status, headers or {}, body

    def json(self):
        return {}


class Http:
    def __init__(self, *responses):
        self.responses, self.sent = list(responses), []

    async def request(self, method, url, **k):
        self.sent.append((method, url))
        return self.responses.pop(0) if self.responses else Resp(200)


# ---- the Pacer ------------------------------------------------------------------------------------------------------------

def test_the_pacer_keeps_to_the_budget_and_background_work_to_a_trickle():
    p = sp.Pacer()
    now = 1000.0
    assert p._wait(False, now) <= 0
    for i in range(sp.BUDGET):                                   # the page's calls, as fast as MIN_GAP allows
        t = now + i * sp.MIN_GAP
        assert p._wait(False, t) <= 1e-9
        p._calls.append((t, False)); p._last = t
    t = now + sp.BUDGET * sp.MIN_GAP
    assert p._wait(False, t) == pytest.approx(now + sp.WINDOW - t)            # budget spent: wait for the oldest to leave the window
    assert p._wait(True, t) > 0                                                # and background work certainly waits

    p = sp.Pacer()
    p._calls.append((now, True)); p._last = p._last_bg = now
    assert p._wait(False, now + 1) <= 0                                        # the page isn't held up by background work...
    assert p._wait(True, now + 1) == pytest.approx(sp.BACKGROUND_GAP - 1)     # ...which is spread out
    for i in range(1, sp.BACKGROUND_BUDGET):                                  # a burst of background calls (gap or not)...
        t = now + i
        p._calls.append((t, True)); p._last = p._last_bg = t
    t = now + sp.BACKGROUND_BUDGET + sp.BACKGROUND_GAP
    assert p._wait(True, t) == pytest.approx(now + sp.WINDOW - t)              # ...uses up background's share of the window


def test_background_work_leaves_room_for_the_page():
    p = sp.Pacer()
    now = 1000.0
    busy = sp.BUDGET - (sp.BUDGET - sp.BACKGROUND_BUDGET) // 2
    for i in range(busy):                                          # the page has been busy
        p._calls.append((now + i * 0.3, False))
    t = now + busy * 0.3 + 1
    p._last = t - 1
    assert p._wait(True, t) > 0                                    # so background work holds back
    assert p._wait(False, t) <= 0                                  # while the page still has room


@sync
async def test_calls_inside_background_use_the_trickle(connected, monkeypatch):
    lanes = []
    async def turn(bg=None):
        lanes.append(sp._background.get() if bg is None else bg)
    monkeypatch.setattr(sp.pacer, "turn", turn)
    monkeypatch.setattr(sp.client, "_client", lambda: Http())
    await sp.client.api("GET", "/me")
    with sp.background():
        await sp.client.api("GET", "/me")
        await asyncio.create_task(sp.client.api("GET", "/me"))   # tasks started inside count as background too
    assert lanes == [False, True, True]


# ---- Retry-After ------------------------------------------------------------------------------------------------------------

@sync
async def test_a_429_stops_every_spotify_call_until_its_retry_after(connected, monkeypatch):
    http = Http(Resp(429, {"Retry-After": "69466"}))
    monkeypatch.setattr(sp.client, "_client", lambda: http)
    with pytest.raises(sp.Resting) as e:
        await sp.client.api("GET", "/search", params={"q": "x"})
    assert "pause until" in str(e.value)
    assert len(http.sent) == 1                                     # never retried
    until = sp.resting_until()
    assert until and abs(until - (time.time() + 69466)) < 5        # written down, so a restart honours it too
    with pytest.raises(sp.Resting):
        await sp.client.api("PUT", "/me/player/pause")
    assert len(http.sent) == 1                                     # nothing else was sent
    st = await player.state(fresh=True)
    assert st["resting_until"] == until and not st["active"] and "pause until" in st["error"]
    assert len(http.sent) == 1
    assert sp.service.extra()["resting_until"] == until             # the page and Settings show it
    db.put_settings(sp.COOLDOWN_KEY, {"until": time.time() - 1})   # once the time has passed, calls go again
    await sp.client.api("GET", "/me")
    assert len(http.sent) == 2


@sync
async def test_a_429_without_retry_after_still_waits(connected, monkeypatch):
    monkeypatch.setattr(sp.client, "_client", lambda: Http(Resp(429)))
    with pytest.raises(sp.Resting):
        await sp.client.api("GET", "/me")
    assert sp.resting_until() - time.time() == pytest.approx(sp.DEFAULT_RETRY, abs=5)


@sync
async def test_the_rotator_does_nothing_while_spotify_is_resting(connected, monkeypatch):
    from app.music import playlists
    def due(now=None):
        raise AssertionError("shouldn't look at playlists while resting")
    monkeypatch.setattr(playlists, "all_playlists", due)
    db.put_settings(sp.COOLDOWN_KEY, {"until": time.time() + 600})
    assert await Rotator().tick() == []


# ---- Last.fm ------------------------------------------------------------------------------------------------------------------

@sync
async def test_lastfm_backs_off_when_it_says_too_many(monkeypatch):
    store.put_settings({**store.settings(), "services": {**store.settings().get("services", {}), "lastfm": {"enabled": True, "api_key": "k" * 32}}})
    sent = []
    class R:
        status_code = 200
        def json(self):
            return {"error": 29, "message": "Rate Limit Exceeded"}
    class C:
        def __init__(self, **k):
            sent.append(k.get("headers", {}))
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def get(self, *a, **k):
            return R()
    monkeypatch.setattr(lastfm.httpx, "AsyncClient", C)
    monkeypatch.setattr(lastfm, "_resting_until", 0.0)
    with pytest.raises(ServiceError, match="leave it alone"):
        await lastfm.call("artist.getSimilar", artist="Gojira")
    assert sent[0]["User-Agent"].startswith("Citadel/")
    with pytest.raises(ServiceError, match="slow down"):
        await lastfm.call("artist.getSimilar", artist="Gojira")
    assert len(sent) == 1                                          # the second wasn't sent


# ---- Discover: overnight, one at a time, never thrown away ------------------------------------------------------------------

@sync
async def test_discover_builds_take_turns_in_the_background(monkeypatch):
    monkeypatch.setattr(config, "NO_FETCH", False)
    live, most, lanes = 0, 0, []
    async def build(basis):
        nonlocal live, most
        live += 1
        most = max(most, live)
        lanes.append(sp._background.get())
        await asyncio.sleep(0.01)
        live -= 1
        return {}
    monkeypatch.setattr(discover, "build", build)
    assert discover.refresh("12m") and discover.refresh("shelf")
    assert not discover.refresh("12m")                              # already queued
    await asyncio.gather(*discover._running.values())
    assert most == 1 and lanes == [True, True]
    await discover.stop()


@sync
async def test_the_nightly_job_redoes_only_stale_results_and_waits_for_spotify(connected, monkeypatch):
    monkeypatch.setattr(config, "NO_FETCH", False)
    built = []
    async def build(basis):
        built.append(basis)
        store.cache_put(f"discover:{basis}", {"built_at": int(time.time()), "checked": True, "version": discover.VERSION, "artists": []})
        return {}
    monkeypatch.setattr(discover, "build", build)
    for b in discover.BASES:
        store.cache_clear(f"discover:{b}")
    fresh = {"built_at": int(time.time()), "checked": True, "version": discover.VERSION, "artists": []}
    store.cache_put("discover:12m", {**fresh, "built_at": int(time.time()) - discover.WEEK - 1})     # a week old
    store.cache_put("discover:shelf", fresh)                                                         # fresh
    store.cache_put("discover:all", {**fresh, "version": discover.VERSION - 1})                      # made by older code
    r = await discover.nightly()                                    # "gigs" was never looked at: left alone
    assert built == ["12m", "all"] and r["built"] == ["12m", "all"]
    built.clear()
    store.cache_put("discover:12m", {**fresh, "partial": True})
    db.put_settings(sp.COOLDOWN_KEY, {"until": time.time() + 600})
    r = await discover.nightly()
    assert built == [] and r["waiting"] == ["12m"]
    assert "waiting for Spotify" in discover.job.summarise(r)
    await discover.stop()


@sync
async def test_a_build_cut_short_by_spotify_keeps_the_last_full_result(connected, lib_less, monkeypatch):
    full = {"built_at": int(time.time()) - 86400, "checked": True, "version": discover.VERSION, "artists": [{"artist": "Kept"}], "vibes": []}
    store.cache_put("discover:12m", full)
    monkeypatch.setattr(discover, "seeds", lambda basis: [])
    async def pick(ranked, vibe_of, check, errors, side):
        db.put_settings(sp.COOLDOWN_KEY, {"until": time.time() + 600})
        errors.append(sp.resting_message(time.time() + 600))
        return []
    monkeypatch.setattr(discover, "_pick", pick)
    r = await discover.build("12m")
    assert r == full and discover.cached("12m") == full


@pytest.fixture()
def lib_less(monkeypatch):
    """Last.fm and MusicBrainz off, so a build reaches the Spotify step straight away."""
    from app.music.services import musicbrainz as mbs
    monkeypatch.setattr(lastfm.service, "ready", lambda: False)
    monkeypatch.setattr(mbs.service, "enabled", lambda: False)


def test_a_restart_doesnt_queue_a_rebuild_of_a_result_it_has(owner_client, monkeypatch):
    started = []
    monkeypatch.setattr(discover, "refresh", lambda basis: started.append(basis) or True)
    store.cache_put("discover:12m", {"built_at": 1, "checked": False, "version": 1, "artists": []})          # old in every way
    r = owner_client.get("/api/owner/music/discover?basis=12m").json()
    assert r["result"]["version"] == 1 and started == []
    store.cache_clear("discover:gigs")
    owner_client.get("/api/owner/music/discover?basis=gigs")                                                # never worked out: done now
    assert started == ["gigs"]


@sync
async def test_refusals_in_a_row_wait_longer_each_time_and_a_success_starts_again(connected, monkeypatch):
    http = Http(Resp(429, {"Retry-After": "5"}), Resp(503), Resp(429, {"Retry-After": "5"}))
    monkeypatch.setattr(sp.client, "_client", lambda: http)
    waits = []
    for _ in range(3):
        with pytest.raises(sp.Resting):
            await sp.client.api("GET", "/me")
        waits.append(round(sp.resting_until() - time.time()))
        db.put_settings(sp.COOLDOWN_KEY, {**db.get_settings(sp.COOLDOWN_KEY, {}), "until": time.time() - 1})    # time passes
    assert waits == [pytest.approx(sp.DEFAULT_RETRY, abs=2), pytest.approx(2 * sp.DEFAULT_RETRY, abs=2), pytest.approx(4 * sp.DEFAULT_RETRY, abs=2)]
    await sp.client.api("GET", "/me")                                    # this one works
    assert not db.get_settings(sp.COOLDOWN_KEY, {}).get("strikes")
    http.responses.append(Resp(429, {"Retry-After": "7200"}))           # Spotify's own wait wins when it's longer
    with pytest.raises(sp.Resting):
        await sp.client.api("GET", "/me")
    assert sp.resting_until() - time.time() == pytest.approx(7200, abs=5)


def test_only_the_scopes_citadel_uses_are_asked_for():
    assert set(sp.SCOPES.split()) == {"playlist-modify-public", "playlist-modify-private", "user-read-playback-state",
                                      "user-modify-playback-state", "user-library-read", "user-library-modify"}
