"""The schedule (what is due, and when), the event log and its secret-scrubbing, the maintenance jobs, and Settings → Maintenance."""
import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from app import config, content, db, logbook, maintenance, scheduler as sched_mod
from app.scheduler import BACKOFF, Scheduler, describe, due_at, next_slot, parse_hhmm, schedule_of
from app.widgets import radar, releases, vinyl
from app.widgets.base import Widget
from tests.test_content import REAL_CLIENT, REAL_SLEEP, handler_factory, patch_http  # noqa: F401

LONDON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _clean(client):
    def wipe():
        with db.db() as con:
            for t in ("event_log", "job_state", "feed_items", "followed_artists", "widget_cache"):
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM widget_settings WHERE widget IN ('_schedule', 'radar')")
        logbook._known.clear()
    wipe()
    logbook.install()
    yield
    wipe()


def run(coro):
    return asyncio.run(coro)


def at(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).timestamp()


class Probe(Widget):
    """A widget we can watch: counts its runs, and can be told to fail."""
    id, title, interval, defaults, min_interval = "probe", "Probe", 3600, {}, 600

    def __init__(self, daily_at=None):
        self.daily_at = daily_at
        self.runs, self.seen_settings, self.fail = 0, [], None

    async def fetch(self, settings):
        self.runs += 1
        self.seen_settings.append(dict(settings))
        if self.fail:
            raise self.fail
        return {"n": self.runs}

    def summarise(self, data):
        return f"run {data['n']}"


@pytest.fixture()
def live_fetching():
    saved = config.NO_FETCH
    config.NO_FETCH = False
    yield
    config.NO_FETCH = saved


# =============================================================== the schedule

def test_times_of_day_are_parsed_strictly():
    assert parse_hhmm("04:00") == (4, 0) and parse_hhmm("23:59") == (23, 59)
    for bad in ("4:00", "24:00", "04:60", "0400", "", None, "ab:cd", "04:00:00"):
        assert parse_hhmm(bad) is None, bad


def test_the_next_daily_slot_is_the_first_one_after_the_last_success():
    assert next_slot(at(2026, 9, 22, 3, 0), "04:00") == at(2026, 9, 22, 4, 0)            # before today's slot -> today
    assert next_slot(at(2026, 9, 22, 4, 5), "04:00") == at(2026, 9, 22, 4, 0) + 86400      # just after -> tomorrow
    assert next_slot(at(2026, 9, 22, 4, 0), "04:00") == at(2026, 9, 23, 4, 0)             # exactly on it -> the next one


def test_daily_slots_survive_the_clocks_changing():
    spring, autumn = at(2026, 3, 28, 12), at(2026, 10, 24, 12)                            # UK clocks change on 29 Mar and 25 Oct 2026
    assert datetime.fromtimestamp(next_slot(spring, "04:00"), LONDON).strftime("%d %H:%M") == "29 04:00"
    assert datetime.fromtimestamp(next_slot(autumn, "04:00"), LONDON).strftime("%d %H:%M") == "25 04:00"
    assert 0 < next_slot(spring, "01:30") - spring < 26 * 3600                            # a slot that doesn't exist that night still lands sensibly


def test_a_widgets_default_schedule_and_valid_overrides():
    w = Probe(daily_at="04:00")
    assert schedule_of(w) == {"mode": "daily", "at": "04:00"} and schedule_of(Probe()) == {"mode": "every", "seconds": 3600}
    assert schedule_of(w, {"probe": {"mode": "every", "seconds": 1800}}) == {"mode": "every", "seconds": 1800}
    assert schedule_of(w, {"probe": {"mode": "daily", "at": "07:30"}}) == {"mode": "daily", "at": "07:30"}
    for bad in ({"mode": "every", "seconds": 60}, {"mode": "every", "seconds": "x"}, {"mode": "daily", "at": "7am"}, {"mode": "hourly"}, "nonsense"):
        assert schedule_of(w, {"probe": bad}) == {"mode": "daily", "at": "04:00"}, bad      # a bad override never takes effect: the default holds
    assert describe({"mode": "daily", "at": "04:00"}) == "daily at 04:00" and describe({"mode": "every", "seconds": 10800}) == "every 3 h"
    assert describe({"mode": "every", "seconds": 1800}) == "every 30 min" and describe({"mode": "every", "seconds": 30}) == "every 30 s"


def test_what_is_due_comes_from_the_record_not_from_the_clock_at_start_up():
    w, daily = Probe(), {"mode": "daily", "at": "04:00"}
    every = {"mode": "every", "seconds": 3600}
    assert due_at(w, daily, None, None) == 0 and due_at(w, daily, {"last_run": None}, None) == 0            # never run -> now
    ok_yesterday = {"last_run": at(2026, 9, 21, 4, 5), "last_ok": at(2026, 9, 21, 4, 5), "streak": 0}
    assert due_at(w, daily, ok_yesterday, None) == at(2026, 9, 22, 4, 0)                                    # daily: the next 04:00
    assert due_at(w, every, ok_yesterday, None) == at(2026, 9, 21, 5, 5)                                    # interval: an hour after the last success
    ok_today = {**ok_yesterday, "last_run": at(2026, 9, 22, 4, 6), "last_ok": at(2026, 9, 22, 4, 6)}
    assert due_at(w, daily, ok_today, None) == at(2026, 9, 23, 4, 0)                                        # already done today: not until tomorrow


def test_failures_back_off_instead_of_hammering():
    w, daily = Probe(), {"mode": "daily", "at": "04:00"}
    base = {"last_run": 1_000_000, "last_ok": 500_000}
    waits = [due_at(w, daily, {**base, "streak": n}, None) - 1_000_000 for n in (1, 2, 3, 4, 5, 6, 40)]
    assert waits == [300, 900, 1800, 3600, 7200, 7200, 7200]                                                # 5 min, 15, 30, 1 h, then every 2 h at most
    assert BACKOFF[0] >= 300


def test_a_backlog_is_worked_through_at_the_widgets_own_pace_but_failure_backoff_wins():
    class Backlog(Probe):
        def retry_after(self, data):
            return 20 if data and data.get("pending") else None
    w, daily = Backlog(), {"mode": "daily", "at": "04:00"}
    state = {"last_run": 1_000_000, "last_ok": 1_000_000, "streak": 0}
    assert due_at(w, daily, state, {"pending": 4}) == 1_000_020
    assert due_at(w, daily, {**state, "streak": 1}, {"pending": 4}) == 1_000_300
    assert due_at(w, daily, state, {"pending": 0}) > 1_000_020


def test_a_restart_does_not_rerun_jobs_that_are_not_due(monkeypatch, live_fetching):
    """The old scheduler fetched everything at every start. Now a job that has just succeeded is left alone until its next
    slot, while a job that has never run runs once."""
    monkeypatch.setattr(sched_mod, "STAGGER", 0.05)
    fresh, never = Probe(daily_at="04:00"), Probe(daily_at="04:00")
    fresh.id, never.id = "fresh", "never"
    db.job_finished("fresh", ok=True, seconds=1, message="")
    db.cache_put("fresh", {"n": 0})

    async def scenario():
        s = Scheduler({"fresh": fresh, "never": never}, lambda _wid: None)
        await s.start()
        await asyncio.sleep(0.7)
        await s.stop()
    asyncio.run(scenario())
    assert fresh.runs == 0 and never.runs == 1


def test_overdue_jobs_are_staggered_at_start_up(monkeypatch, live_fetching):
    monkeypatch.setattr(sched_mod, "STAGGER", 0.4)
    a, b = Probe(), Probe()
    a.id, b.id = "a", "b"
    stamps = {}

    async def scenario():
        s = Scheduler({"a": a, "b": b}, lambda wid: stamps.setdefault(wid, time.monotonic()))
        t0 = time.monotonic()
        await s.start()
        await asyncio.sleep(1.0)
        await s.stop()
        return t0
    t0 = asyncio.run(scenario())
    assert a.runs == 1 and b.runs == 1 and stamps["b"] - stamps["a"] >= 0.3                                 # b waited its turn behind a


# =============================================================== running: recording, logging, backoff

def test_a_successful_run_is_recorded_and_logged(live_fetching):
    w = Probe()
    s = Scheduler({"probe": w}, lambda _wid: None)
    assert run(s.run_once(w)) is True
    st = db.job_get("probe")
    assert st["streak"] == 0 and st["last_ok"] and st["message"] == "run 1" and st["last_seconds"] is not None
    assert db.cache_get("probe")["data"] == {"n": 1}
    entries = logbook.query(source="probe")
    assert [(e["level"], e["message"].split(" — ")[0].startswith("Probe synced in")) for e in entries] == [("info", True)] and entries[0]["message"].endswith("run 1")


def test_a_failure_is_recorded_backed_off_and_the_last_good_data_kept(live_fetching):
    w = Probe()
    s = Scheduler({"probe": w}, lambda _wid: None)
    run(s.run_once(w))
    w.fail = RuntimeError("the site changed its layout")
    assert run(s.run_once(w)) is False and run(s.run_once(w)) is False
    st = db.job_get("probe")
    assert st["streak"] == 2 and st["failing_since"] and "layout" in st["message"]
    assert db.cache_get("probe")["data"] == {"n": 1} and "layout" in db.cache_get("probe")["error"]         # still showing the last good data, with the error beside it
    warnings = logbook.query(level="warning")
    assert [e["message"] for e in warnings] == ["Probe sync failed (attempt 2 in a row): RuntimeError: the site changed its layout",
                                                "Probe sync failed (attempt 1 in a row): RuntimeError: the site changed its layout"]
    assert s.due_at(w) == pytest.approx(st["last_run"] + 900, abs=2)                                        # the second failure waits 15 minutes

    w.fail = None
    assert run(s.run_once(w)) is True
    assert db.job_get("probe")["streak"] == 0 and db.job_get("probe")["failing_since"] is None
    assert "is working again after 2 failed attempts" in logbook.query(source="probe")[0]["message"]


def test_a_timeout_says_so(live_fetching):
    class Hangs(Probe):
        timeout = 0.05
        async def fetch(self, settings):
            await asyncio.sleep(1)
    w = Hangs()
    assert run(Scheduler({"probe": w}, lambda _wid: None).run_once(w)) is False
    assert "TimeoutError: took longer than 0.05 s" in db.job_get("probe")["message"]


def test_sync_now_is_forced_and_a_scheduled_run_is_not(live_fetching):
    w = Probe()

    async def scenario():
        s = Scheduler({"probe": w}, lambda _wid: None)
        await s.run_once(w)
        s.refresh("probe")
        await asyncio.sleep(0.2)
    asyncio.run(scenario())
    assert [x.get("_forced") for x in w.seen_settings] == [False, True]


def test_quiet_widgets_only_log_failures(live_fetching):
    class Quiet(Probe):
        log_success = False
    w = Quiet()
    s = Scheduler({"probe": w}, lambda _wid: None)
    run(s.run_once(w))
    assert logbook.query() == []
    w.fail = ValueError("boom")
    run(s.run_once(w))
    assert [e["level"] for e in logbook.query()] == ["warning"]


def test_unconfigured_and_disabled_jobs_do_not_spin(live_fetching):
    class Unset(Probe):
        def configured(self, settings):
            return False
    w = Unset()
    s = Scheduler({"probe": w}, lambda _wid: None)
    assert run(s.run_once(w)) is True and w.runs == 0
    assert db.job_get("probe")["message"] == "not set up" and s.due_at(w) > time.time() + 3000            # asked again next period, not in a tight loop


# =============================================================== the event log and secrets

def test_secrets_are_scrubbed_before_anything_is_stored_or_shown():
    url_error = "Client error '500 Server Error' for url 'https://app.ticketmaster.com/discovery/v2/events.json?apikey=SUPERSECRETKEY123&keyword=Ghost&radius=20'"
    out = logbook.redact(url_error)
    assert "SUPERSECRETKEY123" not in out and "apikey=***" in out and "keyword=Ghost" in out
    cal = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-0123456789abcdef0123456789abcdef/basic.ics"
    assert "0123456789ab" not in logbook.redact(f"Client error for url '{cal}'") and "private" not in logbook.redact(cal)
    assert "0123456789ab" not in logbook.redact("path /private-0123456789abcdef0123456789abcdef/basic.ics")
    assert logbook.redact("Authorization: Bearer abcdefghij1234567890") == "Authorization: Bearer ***"
    assert logbook.redact("Open http://192.168.0.66:8080/enrol/Zx9_abcDEF12345 to enrol") == "Open http://192.168.0.66:8080/enrol/*** to enrol"
    assert logbook.redact("plain text, keyword=metal, monkey business") == "plain text, keyword=metal, monkey business"       # ordinary words survive


def test_values_a_widget_declares_secret_are_scrubbed_wherever_they_appear():
    logbook.learn(["my-private-widget-secret-value"])
    assert logbook.redact("failed talking to my-private-widget-secret-value at 09:00") == "failed talking to *** at 09:00"
    logbook.learn(["short"])                                                                                 # too short to be a secret: would mangle normal text
    assert logbook.redact("a short message") == "a short message"


def test_a_failing_sync_never_leaks_its_secret_into_the_cache_the_log_or_the_ui_error(live_fetching):
    class Leaky(Probe):
        secret_keys = ("api_key",)
        defaults = {"api_key": "K3Y-VALUE-THAT-MUST-NOT-LEAK"}

        async def fetch(self, settings):
            raise httpx.HTTPStatusError("Server error for url 'https://x.example/api?apikey=K3Y-VALUE-THAT-MUST-NOT-LEAK'", request=None, response=None)
    w = Leaky()
    run(Scheduler({"probe": w}, lambda _wid: None).run_once(w))
    everything = [db.cache_get("probe")["error"], db.job_get("probe")["message"], *[f"{e['message']} {e['detail'] or ''}" for e in logbook.query()]]
    assert all("K3Y-VALUE" not in x for x in everything) and any("apikey=***" in x for x in everything)


def test_the_diary_declares_its_secret_calendar_addresses():
    from app.widgets import diary
    got = diary.widget.secrets({"calendars": [{"name": "Me", "url": "https://calendar.google.com/calendar/ical/x/private-abc123def456/basic.ics"}, {"name": "No url"}]})
    assert got == ["https://calendar.google.com/calendar/ical/x/private-abc123def456/basic.ics"]
    assert radar.widget.secrets({"api_key": "ABCDEFGHIJKLMNOP"}) == ["ABCDEFGHIJKLMNOP"] and radar.widget.secrets({"api_key": ""}) == []


def test_warnings_and_errors_from_anywhere_in_the_app_reach_the_log():
    logging.getLogger("hub.vinyl").warning("Juno couldn't be read: HTTPStatusError")
    logging.getLogger("hub").error("something odd", exc_info=(ValueError, ValueError("with detail"), None))
    logging.getLogger("hub.widgets").info("chatter that should not be kept")
    logging.getLogger("hub.scheduler").info("a milestone", extra={"keep": True, "source": "releases"})
    logging.getLogger("elsewhere").warning("not ours")
    got = {(e["level"], e["source"], e["message"]) for e in logbook.query()}
    assert got == {("warning", "vinyl", "Juno couldn't be read: HTTPStatusError"), ("error", "app", "something odd"), ("info", "releases", "a milestone")}


def test_a_traceback_is_kept_as_detail_and_scrubbed():
    try:
        raise RuntimeError("bad url https://api.example/x?token=TOPSECRETTOKEN99")
    except RuntimeError:
        logging.getLogger("hub").exception("unit watcher failed")
    e = logbook.query()[0]
    assert e["message"] == "unit watcher failed" and "RuntimeError" in e["detail"] and "TOPSECRETTOKEN99" not in e["detail"]


def test_the_log_can_be_filtered_searched_paged_and_cleared():
    for i in range(6):
        logbook.record("info" if i % 2 == 0 else "warning", "vinyl" if i < 3 else "radar", f"entry number {i}", at=1_000 + i)
    logbook.record("error", "radar", "100% broken_thing", at=2_000)
    assert [e["message"] for e in logbook.query(level="error")] == ["100% broken_thing"]
    assert {e["level"] for e in logbook.query(level="warning")} == {"warning", "error"}                        # 'warning' means warnings and worse
    assert {e["source"] for e in logbook.query(source="vinyl")} == {"vinyl"} and len(logbook.query(source="vinyl")) == 3
    assert [e["message"] for e in logbook.query(text="number 4")] == ["entry number 4"]
    assert [e["message"] for e in logbook.query(text="100%")] == ["100% broken_thing"]                        # LIKE wildcards are literal
    assert logbook.query(text="broken_thing") and not logbook.query(text="broken?thing")
    assert [e["message"] for e in logbook.query(text="%")] == ["100% broken_thing"] and not logbook.query(text="n_m")     # '%' and '_' are literal, never wildcards
    page1 = logbook.query(limit=3)
    assert len(page1) == 3 and [e["id"] for e in page1] == sorted((e["id"] for e in page1), reverse=True)        # newest first
    assert all(e["id"] < page1[-1]["id"] for e in logbook.query(limit=10, before=page1[-1]["id"]))
    assert logbook.sources() == ["radar", "vinyl"] and logbook.clear() == 7 and logbook.query() == []


def test_old_entries_are_pruned_and_the_log_is_capped(monkeypatch):
    now = int(time.time())
    logbook.record("info", "x", "ancient", at=now - 40 * 86400)
    logbook.record("info", "x", "recent", at=now - 86400)
    assert logbook.prune() == 1 and [e["message"] for e in logbook.query()] == ["recent"]
    monkeypatch.setattr(logbook, "MAX_ROWS", 3)
    for i in range(6):
        logbook.record("info", "x", f"n{i}")
    logbook.prune()
    assert len(logbook.query()) == 3 and logbook.query()[0]["message"] == "n5"                                 # the newest survive


def test_logging_can_never_take_the_app_down(monkeypatch):
    # a context, so the broken database is put right before the autouse clean-up's own teardown needs the real one
    with monkeypatch.context() as m:
        m.setattr(logbook.db, "db", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
        logbook.record("error", "x", "goes nowhere")                                                           # no exception
        logging.getLogger("hub").warning("also fine")


# =============================================================== maintenance jobs

def test_the_backup_is_a_consistent_checked_copy_and_only_the_last_seven_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for day in range(1, 10):
        out = maintenance._do_backup(f"2026-09-{day:02d}")
    files = sorted(p.name for p in (tmp_path / "backups").glob("hub-*.sqlite"))
    assert files == [f"hub-2026-09-{d:02d}.sqlite" for d in range(3, 10)] and out["kept"] == 7
    latest = tmp_path / "backups" / "hub-2026-09-09.sqlite"
    assert oct(latest.stat().st_mode & 0o777) == "0o600" and oct((tmp_path / "backups").stat().st_mode & 0o777) == "0o700"     # it holds secrets: owner-only
    con = sqlite3.connect(latest)
    assert con.execute("PRAGMA quick_check").fetchone()[0] == "ok" and con.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] >= 7
    con.close()
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_a_corrupt_backup_is_refused_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(maintenance, "_quick_check", lambda path: "*** in database main ***")
    with pytest.raises(RuntimeError, match="integrity check"):
        maintenance._do_backup("2026-09-22")
    assert not list((tmp_path / "backups").glob("hub-*")) and not list((tmp_path / "backups").glob("*.tmp"))        # nothing half-written is left behind


def test_tidying_removes_old_log_entries_stale_items_and_expired_links_but_never_pins():
    now = int(time.time())
    logbook.record("info", "x", "ancient", at=now - 60 * 86400)
    content.upsert_items("news", "f", [{"external_id": "old", "artist": None, "title": "Old story", "date": "2026-01-01"},
                                        {"external_id": "kept", "artist": None, "title": "Pinned old story", "date": "2026-01-01"}], baseline=True)
    kept = next(i["id"] for i in content.list_items("news")["items"] if i["title"] == "Pinned old story")
    content.set_status(kept, "pinned")
    with db.db() as con:
        con.execute("UPDATE feed_items SET last_seen = ?", (now - 90 * 86400,))
        con.execute("INSERT INTO enrol_tokens (token_hash, role, name, expires_at, used) VALUES ('h', 'owner', 't', ?, 0)", (now - 3600,))
    out = maintenance._do_tidy()
    assert out["log_entries"] == 1 and out["items"] == 1 and out["expired_links"] >= 1
    assert [i["title"] for i in content.list_items("news")["items"]] == ["Pinned old story"]


def test_the_daily_check_flags_failing_and_stale_integrations_and_stays_quiet_otherwise(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    now = int(time.time())
    quiet = maintenance._do_check()
    assert quiet == {"problems": []} and logbook.query(level="warning") == []
    db.put_settings("radar", {**radar.widget.defaults, "api_key": "A" * 20})                                 # configured, so the check applies to it
    db.job_finished("radar", ok=False, seconds=1, message="RuntimeError: Ticketmaster rejected the API key", at=now - 20000)
    for _ in range(2):
        db.job_finished("radar", ok=False, seconds=1, message="RuntimeError: Ticketmaster rejected the API key", at=now - 10000)
    with db.db() as con:                                                                                       # releases last worked three days ago
        con.execute("INSERT INTO job_state (job, last_run, last_ok, streak, message) VALUES ('releases', ?, ?, 0, 'ok')", (now - 3 * 86400, now - 3 * 86400))
    out = maintenance._do_check()
    assert sorted(out["problems"]) == ["radar", "releases"]
    text = [e["message"] for e in logbook.query(level="warning")]
    assert any("Gig radar has failed 3 syncs in a row" in m and "rejected the API key" in m for m in text)
    assert any("Releases hasn't synced successfully for 72 hours (its schedule is daily at 04:00)" in m for m in text)


def test_the_daily_check_ignores_jobs_that_are_not_set_up(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    with db.db() as con:                                                                                       # radar has no key: it isn't expected to sync
        con.execute("INSERT INTO job_state (job, last_run, last_ok, streak, message) VALUES ('radar', 1, 1, 0, 'not set up')")
    assert maintenance._do_check() == {"problems": []}


def test_the_maintenance_jobs_run_on_the_scheduler_at_night_and_are_not_data_widgets():
    jobs = maintenance.jobs()
    assert {j.id: j.daily_at for j in jobs.values()} == {"maint-backup": "02:30", "maint-tidy": "02:45", "maint-check": "06:00"}
    from app.widgets import discover
    assert not (set(jobs) & set(discover()))                                                                   # they never appear as dashboard cards
    assert all(j.kind == "maintenance" and j.local for j in jobs.values())


def test_the_data_syncs_are_daily_and_staggered_and_the_local_ones_are_marked():
    from app.widgets import discover
    ws = discover()
    times = {wid: schedule_of(w) for wid, w in ws.items()}
    daily = {wid: t["at"] for wid, t in times.items() if t["mode"] == "daily"}
    assert daily == {"bins": "03:00", "releases": "04:00", "vinyl": "04:30", "radar": "05:00", "shortcuts": "05:45"}   # one at a time, well apart, in the small hours
    assert times["news"] == {"mode": "every", "seconds": 3 * 3600} and times["diary"] == {"mode": "every", "seconds": 1800}
    assert {wid for wid, w in ws.items() if w.local} == {"gigs", "system"}
    assert all(ws[w].min_interval >= 6 * 3600 for w in ("releases", "vinyl", "radar"))                          # never let anyone poll the heavy ones hourly


# =============================================================== Settings → Maintenance (API)

@pytest.fixture()
def owner(owner_client):
    return owner_client


def test_the_maintenance_api_is_owner_only(owner, guest_client):
    for method, url in (("get", "/api/owner/maintenance"), ("get", "/api/owner/logs"), ("delete", "/api/owner/logs"),
                        ("put", "/api/owner/maintenance/releases/schedule"), ("post", "/api/owner/maintenance/releases/run")):
        assert getattr(guest_client, method)(url, headers={"X-Hub": "1"}).status_code == 403, url


def test_the_schedule_table_lists_every_job_with_its_state(owner):
    now = int(time.time())
    db.job_finished("releases", ok=True, seconds=41.2, message="checked 200 artists, 3 new", at=now - 600)
    db.job_finished("vinyl", ok=False, seconds=3, message="RuntimeError: Juno's metal pre-order page had no readable records — has its layout changed?", at=now - 300)
    body = owner.get("/api/owner/maintenance").json()
    jobs = {j["id"]: j for j in body["jobs"]}
    assert set(jobs) >= {"releases", "vinyl", "radar", "news", "bins", "diary", "gigs", "system", "maint-backup", "maint-tidy", "maint-check"}
    r, v = jobs["releases"], jobs["vinyl"]
    assert r["status"] == "ok" and r["schedule_text"] == "daily at 04:00" and r["message"] == "checked 200 artists, 3 new" and r["kind"] == "sync" and not r["local"]
    assert r["next_run"] > now and v["status"] == "failing" and v["streak"] == 1 and "layout" in v["message"] and v["next_run"] == pytest.approx(now - 300 + 300, abs=5)
    assert jobs["radar"]["status"] == "not set up" and jobs["radar"]["next_run"] is None
    assert jobs["maint-backup"]["kind"] == "maintenance" and jobs["system"]["local"] is True and "log_counts" in body


def test_schedules_can_be_changed_reset_and_are_validated(owner):
    put = lambda job, **body: owner.put(f"/api/owner/maintenance/{job}/schedule", json=body)
    assert put("releases", mode="daily", at="03:15").json()["schedule"] == {"mode": "daily", "at": "03:15"}
    assert {j["id"]: j for j in owner.get("/api/owner/maintenance").json()["jobs"]}["releases"]["schedule_text"] == "daily at 03:15"
    assert put("news", mode="every", seconds=7200).json()["schedule"] == {"mode": "every", "seconds": 7200}
    assert put("releases", mode="default").json()["schedule"] == {"mode": "daily", "at": "04:00"}
    r = put("releases", mode="every", seconds=600)                                                              # hourly-ish polling of MusicBrainz: refused
    assert r.status_code == 422 and "can't sync more often" in r.json()["detail"]
    assert put("releases", mode="daily", at="25:00").status_code == 422 and put("releases", mode="daily").status_code == 422
    assert put("releases", mode="every", seconds=10 ** 9).status_code == 422 and put("releases", mode="weekly").status_code == 422
    assert put("nonsense", mode="default").status_code == 404
    assert owner.put("/api/owner/maintenance/news/schedule", json={"mode": "default"}).status_code == 200


def test_the_scheduler_picks_up_a_changed_schedule_without_a_restart(owner, live_fetching):
    w = Probe(daily_at="04:00")
    s = Scheduler({"probe": w}, lambda _wid: None)
    db.job_finished("probe", ok=True, seconds=1, message="", at=int(time.time()) - 60)
    before = s.due_at(w)
    db.put_settings("_schedule", {"probe": {"mode": "every", "seconds": 900}})
    assert s.due_at(w) < before and s.due_at(w) == pytest.approx(db.job_get("probe")["last_ok"] + 900, abs=2)


def test_run_now_queues_a_forced_run_and_404s_for_unknown_jobs(owner):
    assert owner.post("/api/owner/maintenance/maint-check/run").status_code == 202
    assert owner.post("/api/owner/maintenance/nonsense/run").status_code == 404


def test_the_log_endpoint_filters_and_clearing_is_itself_recorded(owner):
    logbook.clear()
    logbook.record("warning", "vinyl", "Juno couldn't be read: HTTPStatusError")
    logbook.record("info", "releases", "Releases synced in 41 s")
    logbook.record("error", "radar", "Ticketmaster rejected the API key")
    got = owner.get("/api/owner/logs", params={"level": "warning"}).json()
    assert [e["source"] for e in got["items"]] == ["radar", "vinyl"] and got["sources"] == ["radar", "releases", "vinyl"] and got["counts"] == {"warning": 1, "info": 1, "error": 1}
    assert [e["message"] for e in owner.get("/api/owner/logs", params={"source": "releases"}).json()["items"]] == ["Releases synced in 41 s"]
    assert [e["source"] for e in owner.get("/api/owner/logs", params={"q": "rejected"}).json()["items"]] == ["radar"]
    assert owner.get("/api/owner/logs", params={"level": "loud"}).status_code == 422 and owner.get("/api/owner/logs", params={"limit": 9999}).status_code == 422
    assert owner.delete("/api/owner/logs").json()["cleared"] == 3
    assert [e["message"] for e in logbook.query()] == ["The event log was cleared (3 entries)"]


def test_a_failing_integration_shows_up_in_the_owners_alert_strip_but_not_for_screens(owner):
    now = int(time.time())
    for _ in range(3):
        db.job_finished("releases", ok=False, seconds=1, message="RuntimeError: MusicBrainz is busy", at=now)
    alerts = owner.get("/api/owner/state").json()["alerts"]
    mine = [a for a in alerts if a["kind"] == "maintenance"]
    assert mine and mine[0]["title"] == "Releases needs attention" and "MusicBrainz is busy" in mine[0]["detail"] and mine[0]["icon"] == "alert"
    db.job_finished("releases", ok=True, seconds=1, message="fine", at=now)
    assert not [a for a in owner.get("/api/owner/state").json()["alerts"] if a["kind"] == "maintenance"]        # gone once it works again


def test_one_failed_attempt_is_a_log_line_not_an_alert(owner):
    db.job_finished("releases", ok=False, seconds=1, message="RuntimeError: blip", at=int(time.time()))
    assert not [a for a in owner.get("/api/owner/state").json()["alerts"] if a["kind"] == "maintenance"]


# =============================================================== integrations notice when a site or API changes

def test_a_juno_page_with_no_readable_records_is_an_error_not_an_empty_shelf(monkeypatch):
    """If Juno redesigns, the crawl must fail loudly — an empty result would look like 'everything sold out' and wipe the stored pre-orders."""
    JUNO = {"id": "juno", "name": "Juno", "url": "https://www.juno.example", "kind": "juno", "collection": "metal", "enabled": True}
    patch_http(monkeypatch, vinyl, {"https://www.juno.example/robots.txt": httpx.Response(200, text=""),
                                    "https://www.juno.example/metal/preorders/": httpx.Response(200, text="<html><body><div class='new-design'>hello</div></body></html>")})

    async def go():
        async with httpx.AsyncClient() as client:
            return await vinyl.fetch_juno(client, JUNO, sleep=lambda s: asyncio.sleep(0))
    with pytest.raises(RuntimeError, match="layout changed"):
        run(go())


def test_a_store_that_suddenly_lists_nothing_keeps_its_items_and_says_so(monkeypatch):
    content.add_artist("Gojira")
    shop = {"id": "earache", "name": "Earache", "url": "https://earache.example", "collection": "pre-orders", "enabled": True}
    content.upsert_items("vinyl", "earache", [{"external_id": "earache:g:m", "artist": "Gojira", "title": "Magma", "date": None, "meta": {"shop": "Earache"}}], baseline=True)
    routes = {"https://earache.example/collections/": httpx.Response(200, json={"products": []}), "https://earache.example/meta.json": httpx.Response(200, json={"currency": "GBP"})}
    patch_http(monkeypatch, vinyl, routes)
    monkeypatch.setattr(vinyl.db, "cache_get", lambda widget: {"data": None})
    out = run(vinyl.widget.fetch({**vinyl.widget.defaults, "shops": [shop]}))
    assert out["stores"] == 1 and [i["title"] for i in content.list_items("vinyl")["items"]] == ["Magma"]          # not wiped
    assert any("returned no products" in e["message"] for e in logbook.query(level="warning"))


def test_one_broken_store_is_logged_by_name_while_the_others_carry_on(monkeypatch):
    content.add_artist("Gojira")
    good = {"id": "earache", "name": "Earache", "url": "https://earache.example", "collection": "pre-orders", "enabled": True}
    bad = {"id": "gone", "name": "Gone Store", "url": "https://gone.example", "collection": "pre-orders", "enabled": True}
    ok = httpx.Response(200, json={"products": [{"title": 'Gojira "Magma" Vinyl - PRE-ORDER', "handle": "m", "product_type": "12\"", "tags": [], "published_at": "2026-09-01T00:00:00Z", "variants": [{"price": "30", "available": True}]}]})
    patch_http(monkeypatch, vinyl, {"https://earache.example/collections/": ok, "https://earache.example/meta.json": httpx.Response(200, json={"currency": "GBP"}), "https://gone.example/": httpx.Response(500)})
    monkeypatch.setattr(vinyl.db, "cache_get", lambda widget: {"data": None})
    vinyl._currency_cache.clear()
    out = run(vinyl.widget.fetch({**vinyl.widget.defaults, "shops": [good, bad]}))
    assert out["stores"] == 1 and out["errors"] == ["Gone Store: HTTPStatusError"]
    assert any(e["source"] == "vinyl" and e["message"].startswith("Gone Store couldn't be read: HTTPStatusError") for e in logbook.query(level="warning"))


def test_an_api_that_changes_shape_is_reported(monkeypatch):
    content.add_artist("Ghost")
    patch_http(monkeypatch, radar, {"https://app.ticketmaster.com/": httpx.Response(200, json={"results": [], "v3": True})})
    monkeypatch.setattr(radar, "asyncio", SimpleNamespace(sleep=lambda s: REAL_SLEEP(0)))
    run(radar.widget.fetch({"api_key": "K" * 20, "latlong": "53.48,-2.24", "radius_miles": 20, "batch": 5}))
    assert any("unexpected shape" in e["message"] and "results, v3" in e["message"] for e in logbook.query(level="warning"))
    logbook.clear()
    content.mark_checked(content.list_artists()[0]["id"], "radar_checked_at")
    patch_http(monkeypatch, radar, {"https://app.ticketmaster.com/": httpx.Response(200, json={"page": {"totalElements": 0}})})       # a normal empty answer: no complaint
    run(radar.widget.fetch({"api_key": "K" * 20, "latlong": "53.48,-2.24", "radius_miles": 20, "batch": 5, "_forced": True}))
    assert logbook.query(level="warning") == []


def test_musicbrainz_answering_without_its_usual_list_is_reported(monkeypatch):
    from tests.test_content import FakeMB
    content.add_artist("Gojira")
    content.set_mbid(content.list_artists()[0]["id"], "mb-gojira")
    fake = FakeMB({"release-group": {"error": "something new"}})
    monkeypatch.setattr(releases.mb, "client", fake)
    run(releases.widget.fetch({**releases.widget.defaults}))
    assert any("release-groups" in e["message"] and "has its API changed" in e["message"] for e in logbook.query(level="warning"))
