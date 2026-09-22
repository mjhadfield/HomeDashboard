"""Project control, against real systemd --user (skipped where a user session isn't available)."""
import asyncio
import json
import shutil
import socket
import subprocess
import time

import pytest

from app import db, procs

needs_systemd = pytest.mark.skipif(
    shutil.which("systemd-run") is None or subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True).returncode not in (0, 1),
    reason="no systemd user session",
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(fn, timeout=20.0, interval=0.3):
    end = time.time() + timeout
    while time.time() < end:
        if (v := fn()):
            return v
        time.sleep(interval)
    return None


@pytest.fixture()
def dummy_project(tmp_path):
    """A throwaway web project: python's http.server on a free port."""
    port = free_port()
    (tmp_path / "index.html").write_text("hello from the dummy project")
    slug = f"pytest-{port}"
    with db.db() as con:
        con.execute(
            "INSERT INTO projects (slug, name, kind, url, port, cwd, start_argv, env) VALUES (?, 'Dummy', 'web', ?, ?, ?, ?, '{}')",
            (slug, f"http://{{host}}:{port}/", port, str(tmp_path),
             json.dumps(["python3", "-u", "-m", "http.server", str(port), "--bind", "127.0.0.1"])),
        )
    yield slug, port
    subprocess.run(["systemctl", "--user", "stop", f"hub-proj-{slug}.service"], capture_output=True)
    with db.db() as con:
        con.execute("DELETE FROM projects WHERE slug = ?", (slug,))


def project(owner_client, slug):
    return next(p for p in owner_client.get("/api/owner/state").json()["projects"] if p["slug"] == slug)


def test_seeded_projects_and_links_exist(owner_client):
    s = owner_client.get("/api/owner/state").json()
    slugs = {p["slug"] for p in s["projects"]}
    assert {"music-companion", "setlist-spotify", "star-epr", "terminal", "hadfield-casino"} <= slugs
    spotify = next(p for p in s["projects"] if p["slug"] == "setlist-spotify")
    assert spotify["lan_visible"] is False
    # {ip} is filled in server-side; {host} is left for the browser
    star = next(p for p in s["projects"] if p["slug"] == "star-epr")
    assert "{ip}" not in star["url"] and star["url"].endswith(":5173/StarEPR/")


def test_unknown_project_is_404(owner_client):
    assert owner_client.post("/api/owner/projects/nope/start").status_code == 404
    assert owner_client.get("/api/owner/projects/nope/logs").status_code == 404


@needs_systemd
def test_start_run_logs_and_stop(owner_client, dummy_project):
    slug, port = dummy_project
    assert project(owner_client, slug)["state"] == "stopped"

    assert owner_client.post(f"/api/owner/projects/{slug}/start").json() == {"ok": True}
    running = wait_for(lambda: (p := project(owner_client, slug))["state"] == "running" and p)
    assert running, "project never reached 'running'"
    assert running["managed"] is True and running["latency_ms"] is not None
    assert running["since"] is not None and 0 <= time.time() - running["since"] < 30      # real uptime, from systemd's own record
    with socket.create_connection(("127.0.0.1", port), timeout=3):
        pass                                   # it really is listening

    # starting an already-running project is harmless
    assert owner_client.post(f"/api/owner/projects/{slug}/start").status_code == 200

    lines = owner_client.get(f"/api/owner/projects/{slug}/logs").json()["lines"]
    assert lines and all({"t", "msg"} <= set(l) for l in lines)

    assert owner_client.post(f"/api/owner/projects/{slug}/stop").json() == {"ok": True}
    stopped = wait_for(lambda: (p := project(owner_client, slug))["state"] == "stopped" and p)
    assert stopped, "project never stopped"
    assert stopped["since"] is None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    # stopping something that is already stopped is harmless too
    assert owner_client.post(f"/api/owner/projects/{slug}/stop").status_code == 200


@needs_systemd
def test_externally_started_project_is_reported_not_controlled(owner_client, dummy_project, tmp_path):
    slug, port = dummy_project
    proc = subprocess.Popen(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"], cwd=tmp_path,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0)
        # health sweeps run every 30s; force one through the recheck helper used after start/stop
        owner_client.app.state.health.state[f"project:{slug}"] = {"ok": True, "ms": 1, "at": int(time.time())}
        p = project(owner_client, slug)
        assert p["state"] == "external" and p["managed"] is False and p["since"] is None   # not ours to track the uptime of
    finally:
        proc.terminate()
        proc.wait(5)


def test_bad_start_command_reports_an_error(owner_client):
    with db.db() as con:
        con.execute("INSERT INTO projects (slug, name, kind, cwd, start_argv) VALUES ('pytest-bad', 'Bad', 'desktop', '/nonexistent-dir', '[\"x\"]')")
    try:
        r = owner_client.post("/api/owner/projects/pytest-bad/start")
        assert r.status_code == 502 and "does not exist" in r.json()["detail"]
    finally:
        with db.db() as con:
            con.execute("DELETE FROM projects WHERE slug = 'pytest-bad'")


# ---- active_since(): parsing, independent of a real systemd session -------------------------------------------

def fake_run(monkeypatch, rc, out, err=""):
    async def go(*args, **kwargs):
        return rc, out, err
    monkeypatch.setattr(procs, "_run", go)


def test_active_since_reads_systemds_monotonic_clock_not_a_parsed_date_string(monkeypatch):
    """Deliberately not parsed from ActiveEnterTimestamp's human string (locale/timezone-abbreviation dependent);
    Python's time.monotonic() reads the same underlying clock as systemd's monotonic timestamps on Linux."""
    monkeypatch.setattr(procs.time, "monotonic", lambda: 5_000.0)
    monkeypatch.setattr(procs.time, "time", lambda: 1_800_000_000.0)
    usec = int((5_000.0 - 42.5) * 1_000_000)          # became active 42.5s ago, in monotonic terms
    fake_run(monkeypatch, 0, f"Id=hub-proj-a.service\nActiveEnterTimestampMonotonic={usec}\n")
    since = asyncio.run(procs.active_since())
    assert since == {"hub-proj-a.service": pytest.approx(1_800_000_000.0 - 42.5, abs=0.01)}


def test_active_since_parses_multiple_units_and_skips_ones_never_started(monkeypatch):
    monkeypatch.setattr(procs.time, "monotonic", lambda: 5_000.0)
    monkeypatch.setattr(procs.time, "time", lambda: 1_800_000_000.0)
    out = (
        "Id=hub-proj-a.service\nActiveEnterTimestampMonotonic=4000000000\n\n"
        "Id=hub-proj-b.service\nActiveEnterTimestampMonotonic=0\n\n"                # never active: excluded, not "just started"
        "Id=hub-proj-c.service\nActiveEnterTimestampMonotonic=4900000000\n"
    )
    fake_run(monkeypatch, 0, out)
    since = asyncio.run(procs.active_since())
    assert set(since) == {"hub-proj-a.service", "hub-proj-c.service"}
    assert since["hub-proj-a.service"] < since["hub-proj-c.service"]                # a started earlier than c


def test_active_since_is_empty_when_nothing_matches_or_the_command_fails(monkeypatch):
    fake_run(monkeypatch, 0, "")
    assert asyncio.run(procs.active_since()) == {}
    fake_run(monkeypatch, 1, "", "no such thing")
    assert asyncio.run(procs.active_since()) == {}


def test_since_is_never_shown_for_a_project_that_isnt_actively_managed(owner_client, monkeypatch):
    """The two systemctl queries (list-units for what's active, show for when it started) could in principle
    disagree — e.g. a unit that failed rather than stopping cleanly might still carry a stale ActiveEnterTimestamp
    while no longer counting as active. That must never leak through as an uptime for a project we don't control."""
    async def fake_active_units():
        return set()                                         # nothing currently active
    async def fake_active_since():
        return {"hub-proj-music-companion.service": time.time() - 500}   # a stale/leftover timestamp
    monkeypatch.setattr(procs, "active_units", fake_active_units)
    monkeypatch.setattr(procs, "active_since", fake_active_since)
    p = project(owner_client, "music-companion")
    assert p["managed"] is False and p["since"] is None


# ---- Launchpad tiles for projects --------------------------------------------------------------------------

def new_project_body(**overrides):
    return {"slug": "pytest-launch", "name": "Pytest Launch", "kind": "web", "cwd": "/tmp",
            "command": "python3 -m http.server 9999", "url": "http://{host}:9999/", "port": 9999, **overrides}


def delete_pytest_launch():
    with db.db() as con:
        con.execute("DELETE FROM projects WHERE slug = 'pytest-launch'")


def test_a_project_can_opt_into_the_launchpad_with_its_own_icon(owner_client):
    try:
        r = owner_client.post("/api/owner/projects", json=new_project_body(show_launchpad=True, icon="chip"))
        assert r.status_code == 201
        cfg = next(p for p in owner_client.get("/api/owner/config").json()["projects"] if p["slug"] == "pytest-launch")
        assert cfg["show_launchpad"] is True and cfg["icon"] == "chip"
        live = project(owner_client, "pytest-launch")
        assert live["show_launchpad"] is True and live["icon"] == "chip"
    finally:
        delete_pytest_launch()


def test_a_projects_icon_defaults_per_kind_until_one_is_chosen(owner_client):
    try:
        owner_client.post("/api/owner/projects", json=new_project_body())
        cfg = next(p for p in owner_client.get("/api/owner/config").json()["projects"] if p["slug"] == "pytest-launch")
        assert cfg["icon"] == ""                                              # Settings picks its own default; nothing is forced into storage
        assert project(owner_client, "pytest-launch")["icon"] == "server"     # the live dashboard payload does resolve one, for a web project
    finally:
        delete_pytest_launch()
    try:
        owner_client.post("/api/owner/projects", json=new_project_body(kind="desktop", url=None, port=None))
        assert project(owner_client, "pytest-launch")["icon"] == "play"       # a different default for a desktop project
    finally:
        delete_pytest_launch()


def test_an_invalid_project_icon_is_refused_like_a_links_icon_is(owner_client):
    r = owner_client.post("/api/owner/projects", json=new_project_body(icon="not-a-real-icon"))
    assert r.status_code == 422 and "icon must be one of" in r.text


def test_a_web_project_without_a_url_warns_if_shown_in_the_launchpad(owner_client):
    try:
        r = owner_client.post("/api/owner/projects", json=new_project_body(show_launchpad=True, url=None))
        assert r.status_code == 201 and any("needs a url" in w for w in r.json()["warnings"])
    finally:
        delete_pytest_launch()
    try:
        r = owner_client.post("/api/owner/projects", json=new_project_body(show_launchpad=False, url=None))
        assert not any("needs a url" in w for w in r.json()["warnings"])      # only a problem if it's actually going to be a tile
    finally:
        delete_pytest_launch()


def test_show_launchpad_and_icon_round_trip_through_an_update(owner_client):
    try:
        owner_client.post("/api/owner/projects", json=new_project_body())
        body = new_project_body(show_launchpad=True, icon="radar")
        del body["slug"]
        r = owner_client.put("/api/owner/projects/pytest-launch", json=body)
        assert r.status_code == 200
        live = project(owner_client, "pytest-launch")
        assert live["show_launchpad"] is True and live["icon"] == "radar"
    finally:
        delete_pytest_launch()


def test_show_projects_defaults_to_true(owner_client):
    try:
        owner_client.post("/api/owner/projects", json=new_project_body())
        assert project(owner_client, "pytest-launch")["show_projects"] is True
        cfg = next(p for p in owner_client.get("/api/owner/config").json()["projects"] if p["slug"] == "pytest-launch")
        assert cfg["show_projects"] is True
    finally:
        delete_pytest_launch()


def test_a_project_hidden_from_projects_is_not_disabled(owner_client):
    """Setting show_projects off (the Hadfield Casino case: a standalone app with nothing to stop, so it doesn't
    need a row in Projects) must never be confused with disabling the project — it's still fully controllable,
    just not listed on the card."""
    try:
        r = owner_client.post("/api/owner/projects", json=new_project_body(
            kind="desktop", url=None, port=None, show_projects=False, show_launchpad=True))
        assert r.status_code == 201
        live = project(owner_client, "pytest-launch")
        assert live["show_projects"] is False and live["show_launchpad"] is True
        assert live["state"] == "stopped"                                    # visible to the API (not disabled), just not to the card
        cfg = next(p for p in owner_client.get("/api/owner/config").json()["projects"] if p["slug"] == "pytest-launch")
        assert cfg["enabled"] is True
    finally:
        delete_pytest_launch()


@needs_systemd
def test_a_project_hidden_from_projects_can_still_be_started_from_its_launchpad_tile(owner_client, dummy_project):
    """The same guarantee, proven against a real unit: hiding a project from the Projects card must never make it
    unstartable — the Launchpad tile is now its only way to launch it."""
    slug, port = dummy_project
    with db.db() as con:
        con.execute("UPDATE projects SET show_projects = 0, show_launchpad = 1 WHERE slug = ?", (slug,))
    assert owner_client.post(f"/api/owner/projects/{slug}/start").json() == {"ok": True}
    running = wait_for(lambda: (p := project(owner_client, slug))["state"] == "running" and p)
    assert running, "a project hidden from the Projects card still needs to actually start"
    assert running["show_projects"] is False
    owner_client.post(f"/api/owner/projects/{slug}/stop")


def test_widget_settings_roundtrip_hides_secrets(owner_client):
    r = owner_client.put("/api/owner/widgets/diary/settings", json={"calendars": [{"name": "T", "url": "https://x.example/secret.ics"}]})
    assert r.status_code == 200 and "secret.ics" not in r.text
    assert "secret.ics" not in owner_client.get("/api/owner/widgets/diary/settings").text
    assert owner_client.put("/api/owner/widgets/bins/settings", json={"uprn": "nope"}).status_code == 422
    assert owner_client.put("/api/owner/widgets/bins/settings", json={"bogus": 1}).status_code == 422
    assert owner_client.get("/api/owner/widgets/nope/settings").status_code == 404
