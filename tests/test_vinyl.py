"""Vinyl radar (label/shop pre-orders) and the 'Various Artists' clean-up. Fixtures use the real title formats seen on the stores."""
import asyncio
import re
import sqlite3
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest

from app import config, content, db
from app.widgets import vinyl
from tests.test_content import REAL_CLIENT, REAL_SLEEP, checked_at, handler_factory, make_music_db, patch_http  # noqa: F401

TODAY = date(2026, 9, 22)
NOW = datetime(2026, 9, 22, 12, tzinfo=config.TZ)
FOLLOWED = {vinyl.key(n): n for n in ("Gojira", "Ghost", "Anthrax", "Amon Amarth", "Judas Priest", "Saxon", "System of a Down", "Outer Heaven", "Kiss")}


@pytest.fixture(autouse=True)
def _clean(client):
    def wipe():
        with db.db() as con:
            for t in ("feed_items", "followed_artists", "gigs"):
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM widget_settings WHERE widget = 'vinyl'")
    wipe()
    yield
    wipe()


def run(coro):
    return asyncio.run(coro)


def prod(title, *, ptype='12"', tags=(), price="24.99", available=True, published="2026-09-10T10:00:00-04:00", body=""):
    return {"title": title, "handle": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"), "product_type": ptype, "tags": list(tags),
            "published_at": published, "variants": [{"price": price, "available": available}], "body_html": body}


# Crash Records' actual description text (trimmed): the release date sits in the first <p>, split across two <strong>
# tags with a non-breaking space, and month/day order and wording vary a little between listings.
CRASH_BODY = "<p><strong>Release Date Friday 25th September</strong><strong>\xa02026<br></strong><strong>All pre-orders will be dispatched/made ready for collection on that day.</strong></p>"
CRASH_BODY_ALT = "<p>Released Friday 22nd January 2027 All pre orders will be fulfilled/made ready for collection on that date.</p>"


SHOP = {"id": "earache", "name": "Earache", "url": "https://earache.example", "collection": "pre-orders", "enabled": True}


# ======================================================= title handling (real strings from the stores)

@pytest.mark.parametrize("title,artist,album", [
    ('Gojira "Magma (10th Anniversary) Red / Black Vinyl - PRE-ORDER', "Gojira", "Magma (10th Anniversary)"),        # unclosed quote: a real typo on Earache
    ('System of a Down "Toxicity (25th Anniversary)" Burgundy Shadow Vinyl & Bonus 7" Vinyl - PRE-ORDER', "System of a Down", "Toxicity (25th Anniversary)"),
    ("Saxon \"Gods of Thunder\" 'Transparent Ghost' Vinyl - PRE-ORDER", "Saxon", "Gods of Thunder"),
    ("Amon Amarth - The Allfather Awakens Oden's Eye Blood Filled - Liquid Vinyl", "Amon Amarth", "The Allfather Awakens Oden's Eye Blood Filled"),
    ("Marilyn Manson - One Assassination Under God - Chapter 2 Ltd. \"The Claymore\" - Colored Vinyl", "Marilyn Manson", "One Assassination Under God"),
    ("Judas Priest - Turbo (Ltd. 40th Anniversary) - Picture Vinyl", "Judas Priest", "Turbo (Ltd. 40th Anniversary)"),
    ("Somebody", "Somebody", ""),
    # Crash Records: '*Pre-Order'/'*Pre Order' (no space, sometimes no hyphen) and a trailing '(The)' instead of a leading 'The'
    ("Gorillaz - Plastic Beach (Colour Blend) *Pre-Order", "Gorillaz", "Plastic Beach (Colour Blend)"),
    ("Sepultura - Roots *Pre Order", "Sepultura", "Roots"),
    ("Sensational Alex Harvey Band (The) - Framed (National Album Day 2026 UK Exclusive) *Pre-Order",
     "Sensational Alex Harvey Band (The)", "Framed (National Album Day 2026 UK Exclusive)"),
    # a ' - ' that sits inside parentheses (a year range, a note) must not be treated as the artist/album separator
    ("Various Artists - Clapton Hymns Vol. 1: 10 Years Of Church Of Sound (2016 - 2026) *Pre-Order",
     "Various Artists", "Clapton Hymns Vol. 1: 10 Years Of Church Of Sound (2016 - 2026)"),
    ("House of Love (The) - THE GIFT (REHEARSALS AND LIVE RECORDINGS 1986 - 1988)   (National Album Day 2026 UK Exclusive) *Pre-Order",
     "House of Love (The)", "THE GIFT (REHEARSALS AND LIVE RECORDINGS 1986 - 1988) (National Album Day 2026 UK Exclusive)"),
])
def test_titles_split_into_artist_and_album(title, artist, album):
    assert vinyl.parse_title(title) == (artist, album)


def test_a_dash_inside_parentheses_is_never_a_split_point():
    assert vinyl.split_top_level("Artist - Album (1986 - 1988) (Reissue) - Gold Vinyl") == ["Artist", "Album (1986 - 1988) (Reissue)", "Gold Vinyl"]
    assert vinyl.split_top_level("No dashes here") == ["No dashes here"]
    assert vinyl.split_top_level("(Only - inside parens)") == ["(Only - inside parens)"]


def test_key_treats_a_trailing_the_the_same_as_a_leading_one():
    assert vinyl.key("The Farm") == vinyl.key("Farm (The)") == vinyl.key("farm") == "farm"
    assert vinyl.key("Sensational Alex Harvey Band (The)") == vinyl.key("The Sensational Alex Harvey Band")
    assert vinyl.key("(The) Weird Case") == content.norm("(The) Weird Case")           # only a *trailing* '(The)' is special-cased


def test_variants_of_one_album_collapse_to_its_real_name():
    albums = ["Cursum Perficio Ltd. Cream White/Yellow/Black", "Cursum Perficio", "Cursum Perficio Transparent Magenta", "Cursum Perficio Ltd.",
              "The Allfather Awakens Oden's Eye Blood Filled", "The Allfather Awakens Plain Raven", "Sad Wings Of Destiny Ltd. Galaxy Yellow/Orange Mix",
              "Turbo (Ltd. 40th Anniversary)", "Live", "Live In Japan"]
    core = vinyl.cluster_albums(albums)
    assert {core[a] for a in albums[:4]} == {"Cursum Perficio"}
    assert {core[a] for a in albums[4:6]} == {"The Allfather Awakens"}
    assert core[albums[6]] == "Sad Wings Of Destiny"                       # a lone item still loses its 'Ltd. <colour>' tail
    assert core[albums[7]] == "Turbo (Ltd. 40th Anniversary)"               # …but a parenthetical edition is kept
    assert len({core[albums[8]], core[albums[9]]}) == 1                     # documented trade-off: 'Live' is a whole-word start of 'Live In Japan'


def test_different_numbered_volumes_are_never_collapsed_into_one():
    """Regression: a themed reissue series ('(Volume 2: …)' vs '(Volume 3: …)', 'Vol. 1' vs 'Vol. 3') shares a long
    leading phrase, but each volume is a genuinely different record — merging them would silently hide one."""
    vols = ["The Traveling Wilburys (Vol. 3) (2016 Remaster)", "The Traveling Wilburys (Vol. 1) (2016 Remaster)"]
    core = vinyl.cluster_albums(vols)
    assert core[vols[0]] == vols[0] and core[vols[1]] == vols[1] and core[vols[0]] != core[vols[1]]
    heart = ["Heart Of Gold: The Songs Of Neil Young (Volume 3: Into the Black)", "Heart Of Gold: The Songs Of Neil Young (Volume 2: Out Of The Blue)"]
    core2 = vinyl.cluster_albums(heart)
    assert len({core2[a] for a in heart}) == 2 and all("(" in v and v.count("(") == v.count(")") for v in core2.values())  # never a truncated, unbalanced '('


# ======================================================= who is it by?

@pytest.mark.parametrize("title,expected", [
    ('Gojira "Magma" Vinyl - PRE-ORDER', "Gojira"),
    ("Ghost - Skeletá - Vinyl", "Ghost"),
    ("The Ghost Inside - Dear Youth Ltd. US-Edition - Vinyl", None),         # a different band: whole names only
    ("Ghostface Killah - Something - Vinyl", None),
    ('Outer Heaven / Temple Of Void "Split" 7"', "Outer Heaven"),           # split release: either side counts
    ('Temple Of Void / Outer Heaven "Split" 7"', "Outer Heaven"),
    ("GOJIRA - Fortitude - Vinyl", "Gojira"),                               # case-insensitive
    ("Somebody Else - Album - Vinyl", None),
])
def test_artist_matching_is_by_whole_name(title, expected):
    assert vinyl.match_artist({"title": title, "tags": []}, FOLLOWED) == expected


def test_tags_are_a_careful_fallback():
    assert vinyl.match_artist({"title": "Compilation Vol. 2 - Vinyl", "tags": ["Aged-15+", "Anthrax", "media"]}, FOLLOWED) == "Anthrax"
    assert vinyl.match_artist({"title": "Compilation - Vinyl", "tags": ["Artists A-Z: Artists A-Z: Saxon"]}, FOLLOWED) == "Saxon"      # Earache style
    followed_kiss = {vinyl.key("Metal"): "Metal", **FOLLOWED}
    assert vinyl.match_artist({"title": "Nobody - Album", "tags": ["metal", "vinyl", "lp"]}, followed_kiss) is None                       # generic genre tags never match


@pytest.mark.parametrize("p,expected", [
    (prod("Anthrax - Cursum Perficio - 2 Vinyl", ptype="Vinyl"), True),
    (prod('Suicide Silence - Graceless - Translucent Tan LP', ptype='12"'), True),
    (prod("Suicide Silence - Graceless - CD", ptype="CD"), False),
    (prod("Band - Album - Cassette", ptype="Cassette"), False),
    (prod("Band Logo T-Shirt", ptype="T-Shirt", tags=["vinyl"]), False),
    (prod('Band "Album" Poster', ptype="Poster"), False),
    (prod("Band Spin Album - Vinyl", ptype="Vinyl"), True),                # 'Spin' is not a 'pin' badge
    (prod("Band - Album - LP + Poster", ptype="Vinyl"), True),               # a record that comes with a poster is still a record
    (prod('Band "Album" T-Shirt', ptype="Artists A-Z"), False),              # Earache's product types are useless: the title decides
])
def test_only_records_count(p, expected):
    assert vinyl.is_vinyl(p) is expected


def test_release_dates_only_where_a_store_exposes_them():
    assert vinyl.release_date(prod("x", ptype="2026-11-13B"), TODAY) == "2026-11-13"                          # Earache: product type
    assert vinyl.release_date(prod("x", tags=["Pre-Order", "meta-10-02-26"]), TODAY) == "2026-10-02"          # Deathwish: MM-DD-YY tag
    assert vinyl.release_date(prod("x", ptype="Vinyl"), TODAY) is None
    assert vinyl.release_date(prod("x", ptype="2099-01-01"), TODAY) is None                                  # implausible: ignored
    assert vinyl.release_date(prod("x", ptype="2026-13-45"), TODAY) is None                                  # not a real date
    assert vinyl.release_date(prod("x", ptype="Music", body=CRASH_BODY), TODAY) == "2026-09-25"              # Crash Records: written in the description
    assert vinyl.release_date(prod("x", ptype="Music", body=CRASH_BODY_ALT), TODAY) == "2027-01-22"          # a second wording ('Released ... All pre orders')
    assert vinyl.release_date(prod("x", ptype="Music", body="<p>No date mentioned here at all.</p>"), TODAY) is None
    assert vinyl.release_date(prod("x", ptype="Music", body="<p>Release Date Friday 31st Februark 2026</p>"), TODAY) is None   # not a real month
    assert vinyl.release_date(prod("x", ptype="2026-11-13B", body=CRASH_BODY), TODAY) == "2026-11-13"        # a real product_type date always wins over the description


# ======================================================= pricing: the vinyl variant, never the CD sold alongside it

def variant_prod(title, variants, **kw):
    p = prod(title, **kw)
    p["variants"] = variants
    return p


def test_the_price_is_the_vinyl_variant_not_whichever_is_cheapest():
    """Real Crash Records data: one product, a 'Format' option offering both a vinyl edition and a CD at a different
    (usually lower) price — picking the cheapest of the lot silently reports the CD's price for a vinyl radar entry."""
    p = variant_prod("Judas Priest - Sad Wings Of Destiny (Original 1976 Masters) *Pre-Order", [
        {"title": "Limited Galaxy Yellow/Orange Mix Vinyl LP", "price": "26.99", "available": True},
        {"title": "CD Album", "price": "13.99", "available": True}])
    assert vinyl._price(p) == 26.99


@pytest.mark.parametrize("variant_title", ["CD Album", "2CD Album", "39CD Album Box Set", "Cassette Tape", "Digital Download", "DVD"])
def test_every_non_vinyl_format_word_is_recognised(variant_title):
    p = variant_prod("x", [{"title": "Black Vinyl LP", "price": "20.00", "available": True}, {"title": variant_title, "price": "5.00", "available": True}])
    assert vinyl._price(p) == 20.0


def test_two_vinyl_formats_on_one_product_still_take_the_cheaper_one():
    """Not every multi-variant product is a vinyl/CD split — a 7" single alongside an LP are both genuinely vinyl,
    and the cheaper one is a fair price to show."""
    p = variant_prod("x", [{"title": "Pink Vinyl LP", "price": "22.99", "available": True}, {"title": "Coke Bottle Clear 7\" Single", "price": "11.99", "available": True}])
    assert vinyl._price(p) == 11.99


def test_a_single_unnamed_variant_is_unaffected():
    """Most stores (Earache and friends) give a product one variant with no descriptive title at all — nothing to filter."""
    assert vinyl._price(prod("x", price="24.99")) == 24.99
    assert vinyl.vinyl_variants(prod("x", price="24.99"))[0]["price"] == "24.99"


def test_sold_out_is_judged_by_the_vinyl_variant_not_a_cd_reprint_still_in_stock():
    p = variant_prod("Band - Album", [{"title": "Black Vinyl LP", "price": "20.00", "available": False}, {"title": "CD Album", "price": "10.00", "available": True}])
    items = vinyl.build_items([p], SHOP, {vinyl.key("Band"): "Band"}, currency="GBP", today=TODAY)
    assert items == []                                                             # sold out of vinyl: the CD being available doesn't rescue it
    q = variant_prod("Band - Album", [{"title": "Black Vinyl LP", "price": "20.00", "available": True}, {"title": "CD Album", "price": "10.00", "available": False}])
    assert len(vinyl.build_items([q], SHOP, {vinyl.key("Band"): "Band"}, currency="GBP", today=TODAY)) == 1   # vinyl in stock: the CD being sold out is irrelevant


def test_editions_in_the_expanded_view_also_show_the_vinyl_price_not_the_cd_price():
    products = [
        crash_prod("Judas Priest - Sad Wings Of Destiny (Original 1976 Masters) *Pre-Order", price="26.99",
                   tags=("Vinyl", "LP (Album)", "CD Albums", "Pre-order")),
    ]
    products[0]["variants"] = [{"title": "Limited Galaxy Yellow/Orange Mix Vinyl LP", "price": "26.99", "available": True},
                               {"title": "CD Album", "price": "13.99", "available": True}]
    items = vinyl.build_items(products, CRASH_SHOP, {vinyl.key("Judas Priest"): "Judas Priest"}, currency="GBP", today=TODAY)
    assert items[0]["meta"]["price"] == "£26.99" and items[0]["subtitle"] == "LP · £26.99 · Crash Records"


# ======================================================= turning a store's catalogue into items

def test_build_items_groups_variants_prices_and_filters():
    products = [
        prod('Gojira "Magma (10th Anniversary)" Red / Black Vinyl - PRE-ORDER', ptype="Artists A-Z", price="31.99", published="2026-09-10T09:00:00Z"),
        prod('Amon Amarth "The Allfather Awakens" Black Vinyl - PRE-ORDER', price="25.99", published="2026-09-08T09:00:00Z"),
        prod('Amon Amarth "The Allfather Awakens" \'Wave Ruler\' Blue Marbled Vinyl - PRE-ORDER', price="27.99", published="2026-09-05T09:00:00Z"),
        prod('Amon Amarth "The Allfather Awakens" CD - PRE-ORDER', ptype="CD", price="9.99"),                        # not vinyl
        prod('Amon Amarth "Old Sold Out" Vinyl - PRE-ORDER', price="20.00", available=False),                        # sold out
        prod('Somebody Else "Nope" Vinyl - PRE-ORDER'),                                                              # not followed
        prod("Saxon Logo T-Shirt - PRE-ORDER", ptype="T-Shirt"),                                                      # merch
    ]
    items = {i["artist"]: i for i in vinyl.build_items(products, SHOP, FOLLOWED, currency="GBP", today=TODAY)}
    assert set(items) == {"Gojira", "Amon Amarth"}
    g, a = items["Gojira"], items["Amon Amarth"]
    assert g["title"] == "Magma (10th Anniversary)" and g["subtitle"] == "Vinyl · £31.99 · Earache" and g["date"] is None and g["meta"]["listed"] == "2026-09-10"
    assert a["title"] == "The Allfather Awakens" and a["subtitle"] == '12" · 2 variants · from £25.99 · Earache' and a["meta"]["variants"] == 2
    assert a["url"] == "https://earache.example/products/amon-amarth-the-allfather-awakens-wave-ruler-blue-marbled-vinyl-pre-order"     # earliest-listed variant
    assert a["external_id"].startswith("earache:") and len(a["external_id"]) < 200


def test_a_shop_without_a_preorder_collection_needs_the_preorder_flag():
    shop = {**SHOP, "collection": ""}
    products = [prod('Gojira "Magma" Vinyl', tags=["Pre-Order"]), prod('Ghost "Prequelle" Vinyl', tags=["new"]), prod('Kiss "Destroyer" Vinyl - PRE-ORDER')]
    got = sorted(i["artist"] for i in vinyl.build_items(products, shop, FOLLOWED, currency="USD", today=TODAY))
    assert got == ["Gojira", "Kiss"]                                                    # 'Ghost' has no pre-order flag: it's just a normal product
    assert vinyl.build_items(products[:1], shop, FOLLOWED, currency="USD", today=TODAY)[0]["subtitle"] == '12" · $24.99 · Earache'


def test_preorders_read_newest_to_oldest_by_release_date_else_listing_date_with_pins_first():
    content.upsert_items("vinyl", "s", [
        {"external_id": "a", "artist": "A", "title": "Undated old listing", "date": None, "meta": {"listed": "2026-08-01"}},
        {"external_id": "b", "artist": "B", "title": "Dated soon", "date": "2026-10-01", "meta": {"listed": "2026-09-01"}},
        {"external_id": "c", "artist": "C", "title": "Undated new listing", "date": None, "meta": {"listed": "2026-09-20"}},
        {"external_id": "d", "artist": "D", "title": "Dated later", "date": "2026-12-25", "meta": {"listed": "2026-09-02"}},
        {"external_id": "e", "artist": "E", "title": "Already released", "date": "2026-09-01", "meta": {"listed": "2026-07-01"}},
        {"external_id": "f", "artist": "F", "title": "Nothing known", "date": None, "meta": {}}], baseline=True)
    titles = lambda: [i["title"] for i in content.list_items("vinyl")["items"]]
    assert titles() == ["Dated later", "Dated soon", "Undated new listing", "Already released", "Undated old listing", "Nothing known"]
    content.set_status(next(i["id"] for i in content.list_items("vinyl")["items"] if i["title"] == "Undated old listing"), "pinned")
    assert titles()[0] == "Undated old listing"


# ======================================================= the sync itself (network faked)

def shop_routes(products_by_shop, currency="GBP"):
    def products(req):
        return httpx.Response(200, json={"products": products_by_shop[req.url.host].get(int(req.url.params.get("page", 1)), [])})
    routes = {}
    for host in products_by_shop:
        routes[f"https://{host}/collections/"] = products
        routes[f"https://{host}/products.json"] = products
        routes[f"https://{host}/meta.json"] = httpx.Response(200, json={"currency": currency})
    return routes


def with_shops(*shops):
    return {"shops": list(shops), "max_pages": 5}


def test_sync_finds_preorders_baselines_then_flags_new_and_drops_vanished(monkeypatch):
    for n in ("Gojira", "Ghost"):
        content.add_artist(n)
    a = {"1": [prod('Gojira "Magma" Vinyl - PRE-ORDER')]}
    live = {"earache.example": {1: a["1"]}}
    patch_http(monkeypatch, vinyl, shop_routes(live))
    s = with_shops(SHOP)
    out = run(vinyl.widget.fetch(s))
    assert out == {"stores": 1, "errors": [], "found": 1, "crawled": {}}
    got = content.list_items("vinyl")
    assert [i["title"] for i in got["items"]] == ["Magma"] and got["new"] == 0                     # first sight of a shop: silent baseline

    live["earache.example"][1] = [prod('Gojira "Magma" Vinyl - PRE-ORDER'), prod('Ghost "Skeletá" Vinyl - PRE-ORDER')]
    assert run(vinyl.widget.fetch(s))["found"] == 2
    got = content.list_items("vinyl")
    assert got["new"] == 1 and [i["title"] for i in got["items"] if i["status"] == "new"] == ["Skeletá"]     # a NEW pre-order appears

    live["earache.example"][1] = [prod('Ghost "Skeletá" Vinyl - PRE-ORDER')]                            # Magma shipped / sold out
    ghost = next(i["id"] for i in got["items"] if i["title"] == "Skeletá")
    run(vinyl.widget.fetch(s))
    assert [i["title"] for i in content.list_items("vinyl")["items"]] == ["Skeletá"]
    content.set_status(ghost, "pinned")
    live["earache.example"][1] = []
    run(vinyl.widget.fetch(s))
    assert [i["title"] for i in content.list_items("vinyl")["items"]] == ["Skeletá"]   # a pin outlives the listing


def test_pagination_and_currency(monkeypatch):
    content.add_artist("Gojira")
    filler = [prod(f"Filler {i} - Vinyl - PRE-ORDER") for i in range(250)]
    pages = {"earache.example": {1: filler, 2: [prod('Gojira "Fortitude" Vinyl - PRE-ORDER', price="30.00")]}}
    patch_http(monkeypatch, vinyl, shop_routes(pages, currency="EUR"))
    monkeypatch.setattr(vinyl, "asyncio", SimpleNamespace(sleep=lambda s: REAL_SLEEP(0)))          # skip the politeness delay between pages
    vinyl._currency_cache.clear()
    run(vinyl.widget.fetch(with_shops(SHOP)))
    item = content.list_items("vinyl")["items"][0]
    assert item["title"] == "Fortitude" and item["subtitle"] == '12" · €30.00 · Earache'          # found on page 2, priced in the shop's currency


def test_one_broken_store_does_not_hide_the_others_and_never_empties_its_own_items(monkeypatch):
    content.add_artist("Gojira"); content.add_artist("Ghost")
    other = {**SHOP, "id": "other", "name": "Other", "url": "https://other.example"}
    live = {"earache.example": {1: [prod('Gojira "Magma" Vinyl - PRE-ORDER')]}, "other.example": {1: [prod('Ghost "Skeletá" Vinyl - PRE-ORDER')]}}
    vinyl._currency_cache.clear()
    patch_http(monkeypatch, vinyl, shop_routes(live))
    s = with_shops(SHOP, other)
    assert run(vinyl.widget.fetch(s))["found"] == 2

    routes = shop_routes(live)
    routes["https://other.example/collections/"] = httpx.Response(503)                              # Other goes down
    patch_http(monkeypatch, vinyl, routes)
    out = run(vinyl.widget.fetch(s))
    assert out["stores"] == 1 and out["errors"] == ["Other: HTTPStatusError"]
    titles = sorted(i["title"] for i in content.list_items("vinyl")["items"])
    assert titles == ["Magma", "Skeletá"]                                                           # Other's items are kept through its outage

    patch_http(monkeypatch, vinyl, {**routes, "https://earache.example/collections/": httpx.Response(500)})
    with pytest.raises(RuntimeError, match="Earache: HTTPStatusError; Other: HTTPStatusError"):
        run(vinyl.widget.fetch(s))                                                                  # everything down = a real error (stale badge)


def test_muted_artists_are_not_searched_for_and_disabled_shops_hide_at_once(monkeypatch):
    content.add_artist("Gojira"); content.add_artist("Ghost")
    ghost = next(a["id"] for a in content.list_artists() if a["name"] == "Ghost")
    content.set_artist_status(ghost, "mute")
    live = {"earache.example": {1: [prod('Gojira "Magma" Vinyl - PRE-ORDER'), prod('Ghost "Skeletá" Vinyl - PRE-ORDER')]}}
    vinyl._currency_cache.clear()
    patch_http(monkeypatch, vinyl, shop_routes(live))
    run(vinyl.widget.fetch(with_shops(SHOP)))
    assert [i["title"] for i in content.list_items("vinyl")["items"]] == ["Magma"]
    off = {**SHOP, "enabled": False}
    assert vinyl.widget.view(None, with_shops(off), NOW)["items"] == [] and vinyl.widget.view(None, with_shops(SHOP), NOW)["stores"] == 1


def test_the_same_record_at_two_shops_shows_once_from_the_first_listed():
    for shop, price in (("earache", "£25.00"), ("nuclear-blast", "€28.99")):
        content.upsert_items("vinyl", shop, [{"external_id": shop, "artist": "Anthrax", "title": "Cursum Perficio", "subtitle": price, "date": None, "meta": {}}], baseline=True)
    s = with_shops(SHOP, {**SHOP, "id": "nuclear-blast", "name": "NB"})
    items = vinyl.widget.view(None, s, NOW)["items"]
    assert [(i["source"], i["subtitle"]) for i in items] == [("earache", "£25.00")]


def test_view_splits_out_purchased_items_with_a_countdown_and_excludes_them_from_items():
    content.upsert_items("vinyl", "earache", [{"external_id": "1", "artist": "Ghost", "title": "Skeletá", "date": "2026-09-29", "meta": {"shop": "Earache"}}], baseline=True)
    iid = content.list_items("vinyl")["items"][0]["id"]
    content.set_status(iid, "purchased")
    v = vinyl.widget.view(None, with_shops(SHOP), NOW)                            # NOW = 2026-09-22
    assert all(i["id"] != iid for i in v["items"])
    pre = next(p for p in v["preorders"] if p["id"] == iid)
    assert pre["in_days"] == 7

    content.add_manual_purchase("vinyl", artist="Ghost", title="TBC one", variant="", date=None, image="")
    tbc = next(p for p in vinyl.widget.view(None, with_shops(SHOP), NOW)["preorders"] if p["title"] == "TBC one")
    assert tbc["in_days"] is None


def test_fetch_manual_cover_downloads_saves_locally_and_is_best_effort(monkeypatch, tmp_path):
    monkeypatch.setattr(vinyl, "COVERS_DIR", tmp_path / "covers")
    patch_http(monkeypatch, vinyl, {
        "https://good.example/cover.jpg": httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"\xff\xd8\xff fake jpeg"),
        "https://bad.example/not-an-image": httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>"),
        "https://down.example/x.jpg": httpx.Response(500),
    })
    path = run(vinyl.fetch_manual_cover("https://good.example/cover.jpg"))
    assert path.startswith("/covers/") and path.endswith(".jpg")
    assert (tmp_path / "covers" / path.removeprefix("/covers/")).read_bytes() == b"\xff\xd8\xff fake jpeg"

    assert run(vinyl.fetch_manual_cover("https://bad.example/not-an-image")) == ""            # wrong content-type
    assert run(vinyl.fetch_manual_cover("https://down.example/x.jpg")) == ""                  # HTTP error -> best-effort, no raise


# ======================================================= settings + store check

def test_store_settings_are_validated_and_ids_stay_unique():
    w = vinyl.widget
    ok = w.merge_settings({}, {"shops": [{"name": "Shop", "url": "https://a.example/", "collection": "pre-orders"}, {"name": "Shop", "url": "https://b.example"}]})
    assert [(s["id"], s["url"], s["collection"], s["enabled"]) for s in ok["shops"]] == [("shop", "https://a.example", "pre-orders", True), ("shop-2", "https://b.example", "", True)]
    for bad in ({"shops": [{"name": "", "url": "https://x"}]}, {"shops": [{"name": "x", "url": "javascript:1"}]}, {"shops": [{"name": "x", "url": "https://x", "collection": "../etc"}]},
                {"shops": "nope"}, {"max_pages": 99}, {"bogus": 1}):
        with pytest.raises(ValueError):
            w.merge_settings({}, bad)


def test_check_shop_reports_what_it_finds(monkeypatch):
    routes = shop_routes({"good.example": {1: [prod('Gojira "Magma" Vinyl - PRE-ORDER'), prod("Band - CD", ptype="CD"), prod('Ghost "Skeletá" Vinyl')]}}, currency="GBP")
    patch_http(monkeypatch, vinyl, routes)
    vinyl._currency_cache.clear()
    res = run(vinyl.check_shop("https://good.example", "pre-orders"))
    assert res["ok"] is True and res["products"] == 3 and res["vinyl_preorders"] == 2 and res["currency"] == "GBP" and res["sample"][0].startswith("Gojira")
    res = run(vinyl.check_shop("https://good.example", ""))
    assert res["ok"] and res["vinyl_preorders"] == 1 and "pre-order collection" in res["note"]         # no collection: only flagged products
    patch_http(monkeypatch, vinyl, {"https://bad.example/": httpx.Response(404)})
    res = run(vinyl.check_shop("https://bad.example", ""))
    assert res["ok"] is False and "Couldn't read a Shopify catalogue" in res["error"]


def test_check_shop_endpoint_is_owner_only_and_validated(owner_client, guest_client, monkeypatch):
    async def fake(url, collection, kind="shopify"):
        return {"ok": True, "products": 1, "vinyl_preorders": 1, "currency": "GBP", "sample": ["x"], "note": ""}
    monkeypatch.setattr(vinyl, "check_shop", fake)
    assert owner_client.post("/api/owner/shops/check", json={"url": "https://a.example", "collection": "pre-orders"}).json()["ok"] is True
    assert owner_client.post("/api/owner/shops/check", json={"url": "ftp://x"}).status_code == 422
    assert owner_client.post("/api/owner/shops/check", json={"url": "https://a.example", "collection": "Bad Handle"}).status_code == 422
    assert guest_client.post("/api/owner/shops/check", json={"url": "https://a.example"}, headers={"X-Hub": "1"}).status_code == 403


# ======================================================= "Various" and other placeholder artists

@pytest.mark.parametrize("name", ["Various", "various artists", "VARIOUS ARTISTS", "[unknown]", "Unknown", "[no artist]", "[anonymous]"])
def test_placeholder_artists_are_recognised(name):
    assert content.is_junk_artist(name)


def test_real_artists_are_not_mistaken_for_placeholders():
    for name in ("Gojira", "Kiss", "The Various Arts", "Unknown Mortal Orchestra", "Anonymous Chorus"):
        assert not content.is_junk_artist(name), name
    assert content.is_junk_artist("Some Name", "89ad4ac3-39f7-470e-963a-56509c546377")               # MusicBrainz's Various Artists id


def test_you_cannot_follow_a_placeholder(owner_client):
    for name in ("Various", "Various Artists"):
        r = owner_client.post("/api/owner/artists", json={"name": name})
        assert r.status_code == 422 and "placeholder" in r.json()["detail"]
    with pytest.raises(ValueError):
        content.add_artist("various")
    assert content.list_artists() == []


def test_seeding_from_music_companion_skips_placeholders_even_when_they_rank_top(tmp_path):
    path = tmp_path / "music.sqlite"
    make_music_db(path)
    con = sqlite3.connect(path)
    con.executescript("INSERT INTO artists VALUES (99,'89ad4ac3-39f7-470e-963a-56509c546377','Various'), (98,NULL,'Various Artists');")
    con.executemany("INSERT INTO scrobbles (artist_id) VALUES (?)", [(99,)] * 5000 + [(98,)] * 3000)      # they'd out-rank every real artist
    con.commit(); con.close()
    content.seed_from_music_db(path, limit=3)
    names = [a["name"] for a in content.list_artists()]
    assert "Various" not in names and "Various Artists" not in names and len(names) == 3               # the limit is filled with real artists


def test_existing_placeholder_rows_are_purged_and_hidden():
    with db.db() as con:
        con.execute("INSERT INTO followed_artists (name, mbid, source, created_at) VALUES ('Various', '89ad4ac3-39f7-470e-963a-56509c546377', 'music-companion', 0), ('Gojira', 'g', 'manual', 0)")
    content.upsert_items("release", "musicbrainz", [{"external_id": "v", "artist": "Various", "title": "NOW Yearbook 1971", "date": "2026-10-02"},
                                                    {"external_id": "g", "artist": "Gojira", "title": "Real", "date": "2026-10-03"}], baseline=True)
    assert [i["artist"] for i in content.list_items("release")["items"]] == ["Gojira"]     # hidden even before the purge
    assert content.purge_junk() == 2                                                                                       # one artist + one item
    assert [a["name"] for a in content.list_artists()] == ["Gojira"]
    with db.db() as con:
        assert con.execute("SELECT COUNT(*) c FROM feed_items WHERE artist = 'Various'").fetchone()["c"] == 0
    assert content.purge_junk() == 0                                                                                       # idempotent


# ======================================================= artwork, formats, editions

def test_artwork_is_only_ever_taken_from_hosts_the_page_is_allowed_to_load():
    assert vinyl.safe_image("https://cdn.shopify.com/s/files/1/x/a.jpg?v=1") == "https://cdn.shopify.com/s/files/1/x/a.jpg?v=1"
    assert vinyl.safe_image("https://imagescdn.juno.co.uk/300/CS1-01A-MED.jpg")
    for bad in ("http://cdn.shopify.com/a.jpg", "https://evil.example/a.jpg", "javascript:alert(1)", "https://cdn.shopify.com.evil.example/a.jpg", "", None, 5):
        assert vinyl.safe_image(bad) == "", bad


@pytest.mark.parametrize("text,expected", [
    ("Gojira Magma - Black Vinyl LP", "LP"), ("Band - Album 2xLP", "2×LP"), ("Band Album Double LP Gatefold", "2×LP"), ('Band - Song 7"', '7"'),
    ('Band - Album 12" Vinyl', '12"'), ("Band Album Picture Disc", "Picture disc"), ("Band Album Box Set 3LP", "Box set"), ("Band Album Red Vinyl", "Vinyl"), ("purple vinyl LP", "LP"),
])
def test_format_labels(text, expected):
    assert vinyl.format_label(text) == expected


def test_edition_is_what_is_left_after_the_artist_and_album():
    assert vinyl.edition_of('Anthrax "Cursum Perficio" Ltd. Cream White Vinyl LP - PRE-ORDER') == "Ltd. Cream White Vinyl LP"
    assert vinyl.edition_of("Judas Priest - Turbo (Ltd. 40th Anniversary) - Gold Vinyl") == "Gold Vinyl"
    assert vinyl.edition_of("Gojira - Magma") == ""


def test_edition_is_read_from_the_titles_own_spelling_not_the_followed_artists_stored_name():
    """Crash Records styles a leading 'The' as a trailing '(The)' — 'edition_of' must still recognise its own artist text,
    not the differently-spelled name match_artist resolved it to, or that name is left behind as a fake 'edition'."""
    assert vinyl.edition_of('Sensational Alex Harvey Band (The) - Framed (National Album Day 2026 UK Exclusive) *Pre-Order') == ""


def test_items_carry_format_label_artwork_and_every_edition_with_its_price():
    def art(p, src):
        return {**p, "images": [{"src": src}]}
    products = [
        art(prod('Anthrax "Cursum Perficio" Ltd. Cream White Vinyl LP - PRE-ORDER', price="29.99", published="2026-09-01T00:00:00Z"), "https://cdn.shopify.com/s/files/1/a.jpg?v=1"),
        art(prod('Anthrax "Cursum Perficio" Transparent Magenta Vinyl LP - PRE-ORDER', price="27.99", published="2026-09-02T00:00:00Z"), "https://cdn.shopify.com/s/files/1/b.jpg?v=1"),
        art(prod('Saxon "Hell, Fire" Black Vinyl LP - PRE-ORDER'), "https://evil.example/tracker.png"),
    ]
    products[0]["vendor"] = "Earache Records Ltd"          # the store's own name (however it is spelled): not a label worth showing
    products[2]["vendor"] = "Silver Lining Music"
    items = {i["artist"]: i for i in vinyl.build_items(products, SHOP, FOLLOWED, currency="GBP", today=TODAY)}
    a, sx = items["Anthrax"], items["Saxon"]
    assert a["meta"]["format"] == "LP" and a["meta"]["variants"] == 2 and a["meta"]["price"] == "£27.99" and a["meta"]["label"] == ""
    assert a["meta"]["image"] == "https://cdn.shopify.com/s/files/1/a.jpg?v=1"
    assert [(e["name"], e["price"]) for e in a["meta"]["editions"]] == [("Ltd. Cream White Vinyl LP", "£29.99"), ("Transparent Magenta Vinyl LP", "£27.99")]
    assert all(e["url"].startswith("https://earache.example/products/") for e in a["meta"]["editions"])
    assert sx["meta"]["image"] == "" and sx["meta"]["label"] == "Silver Lining Music"          # an unknown image host is never stored


# ======================================================= Crash Records (a general Shopify record shop)

CRASH_SHOP = {"id": "crash-records", "name": "Crash Records", "url": "https://www.crashrecords.co.uk", "collection": "music-pre-orders", "enabled": True}
CRASH_FOLLOWED = {vinyl.key(n): n for n in ("Gorillaz", "Sam Fender", "The Sensational Alex Harvey Band", "Sepultura")}


def crash_prod(title, *, price, tags=("Vinyl", "LP (Album)", "Pre-order"), body=CRASH_BODY, vendor=None):
    p = prod(title, ptype="Music", tags=tags, price=price, body=body)
    p["vendor"] = vendor or vinyl.parse_title(title)[0]     # Crash Records' 'vendor' field is the artist, not a label
    return p


def test_crash_records_is_in_the_default_stores_and_is_shopify():
    shop = next(s for s in vinyl.DEFAULT_SHOPS if s["id"] == "crash-records")
    assert shop["enabled"] and shop.get("kind", "shopify") == "shopify" and shop["url"] == "https://www.crashrecords.co.uk" and shop["collection"] == "music-pre-orders"


def test_crash_records_products_become_items_with_real_release_dates_and_no_stray_label():
    products = [
        crash_prod("Gorillaz - Plastic Beach (Colour Blend) *Pre-Order", price="25.99"),
        crash_prod("Sam Fender - Seventeen Going Under (Clear) *Pre-Order", price="35.99"),
        crash_prod("Sensational Alex Harvey Band (The) - Framed (National Album Day 2026 UK Exclusive) *Pre-Order", price="33.99"),
        crash_prod("Sepultura - Roots *Pre Order", price="26.99", body=CRASH_BODY_ALT),
        crash_prod("Kate Bush - Hounds Of Love *Pre-Order", price="30.00"),          # not followed: dropped
    ]
    items = {i["artist"]: i for i in vinyl.build_items(products, CRASH_SHOP, CRASH_FOLLOWED, currency="GBP", today=TODAY)}
    assert set(items) == {"Gorillaz", "Sam Fender", "The Sensational Alex Harvey Band", "Sepultura"}
    g, sah, sep = items["Gorillaz"], items["The Sensational Alex Harvey Band"], items["Sepultura"]
    assert g["title"] == "Plastic Beach (Colour Blend)" and g["date"] == "2026-09-25" and g["subtitle"] == "LP · £25.99 · Crash Records"
    assert sah["title"] == "Framed (National Album Day 2026 UK Exclusive)" and sah["meta"]["label"] == ""    # matched despite the title's '(The)' styling, and no fake label left behind
    assert sah["meta"]["editions"] == [] or sah["meta"]["editions"][0]["name"] == "Standard"                  # a single edition, not the mis-parsed artist name
    assert sep["date"] == "2027-01-22"                                                                        # the second real-world wording of the release-date sentence


def test_crash_records_check_and_fetch_use_the_normal_shopify_path(monkeypatch):
    """Crash Records is an ordinary Shopify store (unlike Juno): the existing fetch/check code needs nothing special for it."""
    content.add_artist("Gorillaz")
    routes = {**shop_routes({"www.crashrecords.co.uk": {1: [crash_prod("Gorillaz - Plastic Beach (Colour Blend) *Pre-Order", price="25.99")]}})}
    patch_http(monkeypatch, vinyl, routes)
    vinyl._currency_cache.clear()
    out = run(vinyl.widget.fetch(with_shops(CRASH_SHOP)))
    assert out["stores"] == 1 and out["found"] == 1
    assert [i["title"] for i in content.list_items("vinyl")["items"]] == ["Plastic Beach (Colour Blend)"]
    res = run(vinyl.check_shop("https://www.crashrecords.co.uk", "music-pre-orders"))
    assert res["ok"] and res["vinyl_preorders"] == 1


# ======================================================= Juno (HTML listing, not Shopify)

JUNO = {"id": "juno", "name": "Juno", "url": "https://www.juno.example", "kind": "juno", "collection": "metal", "enabled": True}
JUNO_FOLLOWED = {vinyl.key(n): n for n in ("Brutal Truth", "Lizzy Borden", "Death Void Terror", "Goetia")}
FIXTURE = (__import__("pathlib").Path(__file__).parent / "fixtures" / "juno_preorders.html").read_text(encoding="utf8")
ROBOTS = "User-agent: *\nCrawl-delay: 5\nDisallow: /cart/\nDisallow: /api/\n"


def test_juno_listing_page_is_parsed_from_real_markup():
    products, last = vinyl.parse_juno_page(FIXTURE, "metal")
    assert last == 3 and len(products) == 4                                     # the record with no price (sold out) is skipped
    by = {p["_artist"]: p for p in products}
    bt = by["BRUTAL TRUTH"]
    assert (bt["_album"], bt["_edition"], bt["_label"], bt["_date"]) == ("Kill Trend Suicide", "purple vinyl LP", "Relapse US", "2026-09-25")
    assert bt["variants"] == [{"price": "18.25", "available": True}] and bt["_url"] == "https://www.juno.co.uk/products/brutal-truth-kill-trend-suicide-vinyl/1174158-01/"
    assert bt["images"] == [{"src": "https://imagescdn.juno.co.uk/300/CS1174158-01A-MED.jpg"}]
    assert by["DEATH VOID TERROR"]["_date"] is None                              # no release date given -> none invented
    assert by["LIZZY BORDEN"]["_edition"] == "'blood moon' swirl vinyl 2xLP" and vinyl.is_vinyl(by["LIZZY BORDEN"])
    assert not vinyl.is_vinyl(by["GOETIA"])                                      # CDs are not vinyl
    assert vinyl.parse_juno_page("<html>nothing here</html>", "metal") == ([], 1)


def test_juno_dates():
    assert vinyl.juno_date("est. release 25 Sep 26") == "2026-09-25" and vinyl.juno_date("Cat: G 006. Rel:\xa025 Sep 26") == "2026-09-25"
    assert vinyl.juno_date("Rel: 31 Feb 26") is None and vinyl.juno_date("Rel: 5 Foo 26") is None and vinyl.juno_date("") is None


def test_juno_products_become_items_like_any_other_store():
    products, _ = vinyl.parse_juno_page(FIXTURE, "metal")
    items = {i["artist"]: i for i in vinyl.build_items(products, JUNO, JUNO_FOLLOWED, currency="GBP", today=TODAY)}
    assert set(items) == {"Brutal Truth", "Lizzy Borden", "Death Void Terror"}      # Goetia is on CD only
    bt = items["Brutal Truth"]
    assert bt["title"] == "Kill Trend Suicide" and bt["date"] == "2026-09-25" and bt["subtitle"] == "LP · £18.25 · Juno"
    assert bt["url"].startswith("https://www.juno.co.uk/products/") and bt["meta"]["label"] == "Relapse US" and bt["meta"]["image"].startswith("https://imagescdn.juno.co.uk/")
    assert items["Lizzy Borden"]["meta"]["format"] == "2×LP" and items["Death Void Terror"]["date"] is None


def juno_routes(pages_by_section, seen=None, robots=ROBOTS):
    def page(req):
        if seen is not None:
            seen.append(req.url.path)
        parts = [x for x in req.url.path.split("/") if x]                           # ['metal', 'preorders', '2']
        n = int(parts[2]) if len(parts) > 2 else 1
        html = pages_by_section.get(parts[0], {}).get(n, "<html></html>")
        return httpx.Response(200, text=html)
    routes = {"https://www.juno.example/robots.txt": httpx.Response(200, text=robots)}
    for section in pages_by_section:
        routes[f"https://www.juno.example/{section}/preorders/"] = page
    return routes


def test_juno_crawl_walks_every_page_of_each_section_obeying_crawl_delay_and_dedupes(monkeypatch):
    seen, sleeps = [], []
    async def nap(seconds):
        sleeps.append(seconds)
    pages = {"metal": {1: FIXTURE, 2: FIXTURE, 3: FIXTURE}, "rock": {1: FIXTURE}}      # rock repeats the same products: one record, not two
    patch_http(monkeypatch, vinyl, juno_routes(pages, seen))

    async def go():
        async with httpx.AsyncClient() as client:
            return await vinyl.fetch_juno(client, {**JUNO, "collection": "metal,rock"}, sleep=nap)
    got = run(go())
    assert seen == ["/metal/preorders/", "/metal/preorders/2/", "/metal/preorders/3/", "/rock/preorders/"]
    assert sleeps == [5.0, 5.0, 5.0]                                                    # robots.txt says Crawl-delay: 5 — between pages, none before the first
    assert len(got) == 4                                                                # 4 distinct products however many pages repeat them


def test_juno_crawl_never_goes_past_max_pages_and_never_hammers_when_robots_is_silent(monkeypatch):
    seen, sleeps = [], []
    async def nap(seconds):
        sleeps.append(seconds)
    patch_http(monkeypatch, vinyl, juno_routes({"metal": {1: FIXTURE, 2: FIXTURE, 3: FIXTURE}}, seen, robots=""))

    async def go():
        async with httpx.AsyncClient() as client:
            return await vinyl.fetch_juno(client, JUNO, max_pages=2, sleep=nap)
    run(go())
    assert seen == ["/metal/preorders/", "/metal/preorders/2/"] and sleeps == [2.0]         # a floor of 2s even with no Crawl-delay


def test_juno_crawl_refuses_pages_robots_txt_disallows(monkeypatch):
    patch_http(monkeypatch, vinyl, juno_routes({"metal": {1: FIXTURE}}, robots="User-agent: *\nDisallow: /metal/\n"))

    async def go():
        async with httpx.AsyncClient() as client:
            return await vinyl.fetch_juno(client, JUNO)
    with pytest.raises(RuntimeError, match="robots.txt"):
        run(go())


def test_juno_is_read_at_most_once_a_day_and_again_when_its_sections_change(monkeypatch):
    for n in ("Brutal Truth", "Lizzy Borden"):
        content.add_artist(n)
    seen = []
    patch_http(monkeypatch, vinyl, juno_routes({"metal": {1: FIXTURE, 2: "<html></html>"}, "rock": {1: FIXTURE}}, seen))
    async def nap(seconds):
        pass
    monkeypatch.setattr(vinyl, "asyncio", SimpleNamespace(sleep=nap))
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(vinyl, "time", SimpleNamespace(time=lambda: clock["now"]))
    cache = {"data": None}
    monkeypatch.setattr(vinyl.db, "cache_get", lambda widget: {"data": cache["data"]})
    s = with_shops(JUNO)

    out = run(vinyl.widget.fetch(s)); cache["data"] = out
    assert out["stores"] == 1 and out["found"] == 2 and out["crawled"]["juno"] == {"at": 1_800_000_000, "sig": "metal"}
    first = len(seen)
    assert first > 0
    clock["now"] += 3 * 3600                                                            # the scheduler comes round again 3 hours later…
    out = run(vinyl.widget.fetch(s)); cache["data"] = out
    assert len(seen) == first and out["stores"] == 1 and out["found"] == 0             # …Juno is not read again, and nothing is lost
    assert len(content.list_items("vinyl")["items"]) == 2

    clock["now"] += 22 * 3600                                                           # a day on: read again
    run(vinyl.widget.fetch(s)); assert len(seen) == first * 2
    changed = with_shops({**JUNO, "collection": "metal,rock"})                          # sections edited in Settings: read again straight away
    before = len(seen)
    run(vinyl.widget.fetch(changed)); assert len(seen) > before


def test_juno_failure_is_reported_without_touching_other_stores_or_its_own_items(monkeypatch):
    content.add_artist("Brutal Truth"); content.add_artist("Gojira")
    routes = {**shop_routes({"earache.example": {1: [prod('Gojira "Magma" Vinyl - PRE-ORDER')]}}), "https://www.juno.example/": httpx.Response(503)}
    patch_http(monkeypatch, vinyl, routes)
    monkeypatch.setattr(vinyl.db, "cache_get", lambda widget: {"data": None})
    vinyl._currency_cache.clear()
    out = run(vinyl.widget.fetch(with_shops(SHOP, JUNO)))
    assert out["stores"] == 1 and out["errors"] == ["Juno: HTTPStatusError"] and "juno" not in out["crawled"]
    assert [i["title"] for i in content.list_items("vinyl")["items"]] == ["Magma"]


def test_juno_settings_are_validated_and_the_defaults_are_valid():
    ok = vinyl.widget.merge_settings({}, {"shops": [{"name": "Juno", "url": "https://www.juno.co.uk", "kind": "juno", "collection": " metal, rock ,punk-hardcore "}]})
    assert ok["shops"][0]["collection"] == "metal,rock,punk-hardcore" and ok["shops"][0]["kind"] == "juno"
    for bad in ({"url": "https://evil.example", "collection": "metal"}, {"url": "https://www.juno.co.uk", "collection": ""}, {"url": "https://www.juno.co.uk", "collection": "Metal!"},
                {"url": "https://www.juno.co.uk", "collection": ",".join(f"s{i}" for i in range(9))}, {"url": "https://www.juno.co.uk", "collection": "metal", "kind": "wordpress"}):
        with pytest.raises(ValueError):
            vinyl.widget.merge_settings({}, {"shops": [{"name": "Juno", "kind": bad.get("kind", "juno"), **{k: v for k, v in bad.items() if k != "kind"}}]})
    assert vinyl.widget.merge_settings({}, {"shops": vinyl.DEFAULT_SHOPS})["shops"]                 # what ships must itself pass validation
    juno = next(s for s in vinyl.DEFAULT_SHOPS if s["id"] == "juno")
    assert juno["enabled"] and juno["kind"] == "juno" and "metal" in juno["collection"]


def test_check_a_juno_section(monkeypatch):
    patch_http(monkeypatch, vinyl, {"https://www.juno.example/metal/preorders/": httpx.Response(200, text=FIXTURE)})
    res = run(vinyl.check_shop("https://www.juno.example", "metal,rock", "juno"))
    assert res["ok"] and res["products"] == 4 and res["vinyl_preorders"] == 3 and res["currency"] == "GBP" and "3 pages" in res["note"] and res["sample"][0].startswith("BRUTAL TRUTH")
    patch_http(monkeypatch, vinyl, {"https://www.juno.example/": httpx.Response(500)})
    assert run(vinyl.check_shop("https://www.juno.example", "metal", "juno"))["ok"] is False


def test_check_endpoint_accepts_juno_sections_but_not_a_list_for_shopify(owner_client, monkeypatch):
    async def fake(url, collection, kind="shopify"):
        return {"ok": True, "kind": kind, "collection": collection, "products": 0, "vinyl_preorders": 0, "currency": "GBP", "sample": [], "note": ""}
    monkeypatch.setattr(vinyl, "check_shop", fake)
    r = owner_client.post("/api/owner/shops/check", json={"url": "https://www.juno.co.uk", "collection": "metal,rock", "kind": "juno"})
    assert r.status_code == 200 and r.json()["kind"] == "juno" and r.json()["collection"] == "metal,rock"
    assert owner_client.post("/api/owner/shops/check", json={"url": "https://a.example", "collection": "metal,rock"}).status_code == 422
    assert owner_client.post("/api/owner/shops/check", json={"url": "https://a.example", "kind": "wordpress"}).status_code == 422


# ======================================================= Confirmed Pre-orders: a hand-set release date

def test_a_release_date_set_by_hand_survives_the_next_store_sync(owner_client):
    item = {"external_id": "earache:lamb-of-god:sacrament", "artist": "Lamb Of God", "title": "Sacrament", "url": "https://earache.example/x", "date": None, "meta": {}}
    content.upsert_items("vinyl", "earache", [item], baseline=True)
    iid = content.list_items("vinyl")["items"][0]["id"]
    content.set_status(iid, "purchased")
    assert owner_client.patch(f"/api/owner/items/{iid}/date", json={"date": "2026-11-27"}).status_code == 200

    content.upsert_items("vinyl", "earache", [item], baseline=False)            # the store still says TBC
    assert content.list_purchased("vinyl")[0]["date"] == "2026-11-27"            # the hand-set date stays

    owner_client.patch(f"/api/owner/items/{iid}/date", json={"date": None})      # back to TBC by hand…
    content.upsert_items("vinyl", "earache", [{**item, "date": "2026-12-04"}], baseline=False)
    assert content.list_purchased("vinyl")[0]["date"] == "2026-12-04"            # …so a date the store publishes comes through again


def test_store_dates_still_update_when_nobody_has_set_one():
    item = {"external_id": "earache:x:y", "artist": "X", "title": "Y", "url": "https://earache.example/y", "date": None, "meta": {}}
    content.upsert_items("vinyl", "earache", [item], baseline=True)
    content.upsert_items("vinyl", "earache", [{**item, "date": "2027-01-15"}], baseline=False)
    assert content.list_items("vinyl")["items"][0]["date"] == "2027-01-15"
