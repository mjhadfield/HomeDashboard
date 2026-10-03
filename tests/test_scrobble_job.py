"""The scheduled Last.fm import (app/music/scrobble_job.py), against a fake Music Companion maintenance server."""
import asyncio

import httpx
import pytest

from app import maintenance
from app.music import scrobble_job
from app.music.scrobble_job import ScrobbleJob


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def job(monkeypatch):
    j = ScrobbleJob()
    monkeypatch.setattr(scrobble_job, "POLL_SECONDS", 0)
    monkeypatch.setattr(ScrobbleJob, "_cfg", staticmethod(lambda: {"maintenance_password": "s3cret-pw"}))
    return j


def server(responses, seen=None):
    """A fake maintenance server: POST starts (or skips), GETs report progress from `responses` in turn."""
    polls = iter(responses["polls"])

    def handle(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.method, req.url.path, req.headers.get("authorization")))
        if responses.get("status"):
            return httpx.Response(responses["status"])
        if req.method == "POST":
            return httpx.Response(200, json=responses["start"])
        return httpx.Response(200, json=next(polls))
    return httpx.MockTransport(handle)


def test_it_is_scheduled_every_two_hours_with_an_hour_floor_and_listed_in_settings(owner_client):
    j = owner_client.app.state.scheduler.widgets["music-lastfm"]
    assert (j.interval, j.min_interval, j.kind) == (2 * 3600, 3600, "sync")
    assert "music-lastfm" not in maintenance.jobs()                       # not housekeeping: it talks to Last.fm
    rows = owner_client.get("/api/owner/maintenance").json()["jobs"]
    assert any(r["id"] == "music-lastfm" for r in rows)


def test_without_the_maintenance_password_it_is_not_set_up(monkeypatch):
    monkeypatch.setattr(ScrobbleJob, "_cfg", staticmethod(lambda: {}))
    assert not ScrobbleJob().configured({})


def test_a_run_waits_for_the_import_and_reports_what_arrived(job):
    seen = []
    job.transport = server({"start": {"jobId": "j1"}, "polls": [
        {"done": False},
        {"done": True, "returncode": 0, "inboxPending": 5,
         "summary": {"imported": 18, "releasesLookedUp": 2, "events": {"new_song": 3, "new_album": 1}}}]}, seen)
    data = run(job.fetch({}))
    assert data == {"imported": 18, "events": {"new_song": 3, "new_album": 1}, "lookups": 2, "inbox": 5}
    assert job.summarise(data) == "18 new plays · 1 new album · 3 new songs · inbox: 5 to review"
    assert seen[0][:2] == ("POST", "/api/scheduled/lastfm") and seen[0][2].startswith("Basic ")
    assert job.retry_after(data) is None


def test_a_busy_server_is_a_retry_soon_not_a_failure(job):
    job.transport = server({"start": {"skipped": "a manual refresh is running"}, "polls": []})
    data = run(job.fetch({}))
    assert data == {"skipped": "a manual refresh is running"}
    assert job.retry_after(data) == 15 * 60
    assert "skipped" in job.summarise(data)


def test_a_wrong_password_says_where_to_fix_it(job):
    job.transport = server({"status": 401, "polls": []})
    with pytest.raises(RuntimeError, match="Settings → Music page"):
        run(job.fetch({}))


def test_a_failed_import_fails_the_job_and_the_password_is_a_secret(job):
    job.transport = server({"start": {"jobId": "j2"}, "polls": [{"done": True, "returncode": 1, "tail": ["...", "requests.exceptions.HTTPError: 503"]}]})
    with pytest.raises(RuntimeError, match="503"):
        run(job.fetch({}))
    assert job.secrets({}) == ["s3cret-pw"]
