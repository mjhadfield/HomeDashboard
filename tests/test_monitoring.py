"""The Monitoring tile's endpoint: owner-only, serves a background loop's latest reading (app/monitor.py) so
the request path never does a live sensor read (nvidia-smi's subprocess spawn) and always returns instantly."""
from app.monitor import _collect

BASE = "/api/owner"


def test_monitoring_is_owner_only(guest_client):
    assert guest_client.get(f"{BASE}/monitoring").status_code == 403


def test_monitoring_shape(owner_client):
    r = owner_client.get(f"{BASE}/monitoring")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"cpu", "gpu", "mem", "load"}
    assert "percent" in body["cpu"] and "per_core" in body["cpu"] and "temp" in body["cpu"]
    assert "package" in body["cpu"]["temp"] and "chiplets" in body["cpu"]["temp"]
    assert set(body["mem"]) == {"total", "used", "available", "swap_total", "swap_used", "buffers", "cached"}
    assert len(body["load"]) == 3
    # gpu is None on a machine without nvidia-smi — either is valid, but if present it must be fully shaped
    if body["gpu"] is not None:
        assert set(body["gpu"]) == {"name", "percent", "temp", "mem_used_mb", "mem_total_mb",
                                     "power_w", "power_limit_w", "clock_sm_mhz", "clock_mem_mhz", "fan_percent"}


def test_cpu_percent_needs_a_second_reading():
    """The very first read of /proc/stat has no prior sample to diff against, so percent is None until a
    second read — a fresh service start briefly shows '—' on the tile, by design, not a bug. Exercised at the
    collection function directly, not the endpoint: the background loop is already running for the whole
    test (started by the app's own lifespan), which makes the endpoint's exact timing non-deterministic."""
    from app import monitor
    monitor._prev_cpu.clear()
    first = _collect()
    assert first["cpu"]["percent"] is None
    second = _collect()
    assert second["cpu"]["percent"] is not None


def test_endpoint_falls_back_to_a_direct_read_when_the_background_loop_hasnt_populated_yet(owner_client):
    """Covers the startup window before the loop's first tick — the endpoint must still return something
    usable, not block forever or error."""
    mon = owner_client.app.state.monitor
    prior = mon.latest
    mon.latest = None
    try:
        body = owner_client.get(f"{BASE}/monitoring").json()
        assert set(body) == {"cpu", "gpu", "mem", "load"}
    finally:
        mon.latest = prior


def test_endpoint_serves_the_cached_reading_as_is(owner_client):
    """Once the background loop has a reading, the endpoint must serve it verbatim — no live read in the
    request path — even though a fresh collection would look completely different from this sentinel."""
    mon = owner_client.app.state.monitor
    prior = mon.latest
    mon.latest = {"cpu": "sentinel", "gpu": None, "mem": "sentinel", "load": [0, 0, 0]}
    try:
        assert owner_client.get(f"{BASE}/monitoring").json() == mon.latest
    finally:
        mon.latest = prior


def test_background_loop_populates_latest_on_its_own(owner_client):
    """The whole point of the background loop: without any request ever calling snapshot() directly, `latest`
    should fill in by itself shortly after startup."""
    import time
    mon = owner_client.app.state.monitor
    for _ in range(100):                      # up to ~2s — well over the loop's 1s interval
        if mon.latest is not None:
            break
        time.sleep(0.02)
    assert mon.latest is not None
