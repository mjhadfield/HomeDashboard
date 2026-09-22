"""The Cover Art Archive client: picks the front cover's small thumbnail, and treats "no art" (common) as a
plain None rather than an error. No real network — httpx.MockTransport stands in for coverartarchive.org."""
import asyncio

import httpx
import pytest

from app.coverart import Client

IMAGES_RESPONSE = {
    "images": [
        {"front": False, "thumbnails": {"250": "https://coverartarchive.org/release/x/back-250.jpg"}},
        {"front": True, "thumbnails": {"250": "https://coverartarchive.org/release/x/front-250.jpg", "small": "https://coverartarchive.org/release/x/front-small.jpg"}},
    ],
}


def run(coro):
    return asyncio.run(coro)


def make_client(handler) -> Client:
    c = Client()
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c._last = 0.0    # skip the polite pacing delay — irrelevant to what these tests check
    return c


def test_picks_the_front_covers_thumbnail_not_just_the_first_image():
    client = make_client(lambda req: httpx.Response(200, json=IMAGES_RESPONSE))
    assert run(client.front_thumb("mbid-1")) == "https://coverartarchive.org/release/x/front-250.jpg"


def test_404_means_no_art_not_an_error():
    client = make_client(lambda req: httpx.Response(404))
    assert run(client.front_thumb("mbid-none")) is None


def test_no_images_in_the_response_means_no_art():
    client = make_client(lambda req: httpx.Response(200, json={"images": []}))
    assert run(client.front_thumb("mbid-empty")) is None


def test_falls_back_to_the_first_image_if_none_is_marked_front():
    resp = {"images": [{"front": False, "thumbnails": {"250": "https://coverartarchive.org/release/x/only-250.jpg"}}]}
    client = make_client(lambda req: httpx.Response(200, json=resp))
    assert run(client.front_thumb("mbid-noflag")) == "https://coverartarchive.org/release/x/only-250.jpg"


def test_a_real_server_error_is_not_swallowed():
    """404 (no art) is expected and fine; anything else (the archive having a bad day) should surface, not
    hide as None — it's attach_art() in the releases widget that deliberately catches this, not this client."""
    client = make_client(lambda req: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        run(client.front_thumb("mbid-broken"))
