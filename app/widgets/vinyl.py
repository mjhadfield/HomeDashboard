"""Vinyl radar: records you can PRE-ORDER by artists you follow, from label and record-shop stores.

Why shops rather than MusicBrainz/Discogs: those are catalogues of pressings that already exist, so they can't tell you
about a record you can still pre-order. Labels list a pressing the moment it goes on pre-order, and many stores run on
Shopify, which publishes its catalogue (including a pre-order collection) as public JSON. One small request per store
lists everything on pre-order there; we keep the vinyl that is by an artist you follow.

Titles, artists and prices come straight from the store. Release dates are only known where a store exposes one
(Earache, Deathwish, Juno and Crash Records do); otherwise we show when the pre-order was listed.

Juno is not Shopify: it is read from its public pre-order listing pages (metal, rock, punk…), politely — paced by the
Crawl-delay in its robots.txt, only pages robots.txt allows, and at most once a day.

Wix stores (Nesi Media) have no catalogue feed, so the whole shop is watched instead of a pre-order list: the product
sitemap (one request) names every product page; only pages whose address starts with an artist you follow are read,
slowly, and each page's standard schema.org product data gives the title, price, stock and artwork.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.robotparser import RobotFileParser
from uuid import uuid4

import httpx
from bs4 import BeautifulSoup

from .. import __version__, config, content, db
from ..schemas import http_url
from .base import Widget

log = logging.getLogger("hub.vinyl")

DEFAULT_SHOPS = [   # checked against the live stores; order = priority when the same record is on several
    {"id": "earache", "name": "Earache", "url": "https://earache.com", "collection": "pre-orders", "enabled": True},               # GBP
    {"id": "nuclear-blast", "name": "Nuclear Blast", "url": "https://www.nuclearblast.com", "collection": "pre-order", "enabled": True},   # EUR
    {"id": "century-media", "name": "Century Media", "url": "https://centurymedia.store", "collection": "pre-orders", "enabled": True},
    {"id": "relapse", "name": "Relapse", "url": "https://relapse.com", "collection": "pre-orders", "enabled": True},
    {"id": "deathwish", "name": "Deathwish", "url": "https://www.deathwishinc.com", "collection": "pre-order", "enabled": True},
    {"id": "crash-records", "name": "Crash Records", "url": "https://www.crashrecords.co.uk", "collection": "music-pre-orders", "enabled": True},   # GBP; general record shop (not metal-specific), by far the biggest catalogue here
    {"id": "juno", "name": "Juno", "url": "https://www.juno.co.uk", "kind": "juno", "collection": "metal,rock,punk-hardcore", "enabled": True},   # GBP; HTML listing, not Shopify
    {"id": "nesi-media", "name": "Nesi Media", "url": "https://www.nesimedia.com", "kind": "wix", "collection": "", "enabled": True},   # HKD; Hong Kong label (licensed variants); Wix, whole shop — not just pre-orders
    {"id": "nuclear-blast-us", "name": "Nuclear Blast (US)", "url": "https://shop.nuclearblast.com", "collection": "pre-order", "enabled": False},   # mostly the EU catalogue again
]
JUNO_EVERY_HOURS = 24            # a full Juno pass is ~100 polite page requests, so it runs at most this often
JUNO_MAX_PAGES = 60              # per section (50 records a page)
IMAGE_HOSTS = {"cdn.shopify.com", "imagescdn.juno.co.uk", "static.wixstatic.com"}      # artwork is only ever loaded from these (the page's CSP allows just them)
SYMBOLS = {"USD": "$", "GBP": "£", "EUR": "€", "CAD": "CA$", "AUD": "A$", "HKD": "HK$"}
VINYL_RE = re.compile(r'vinyl|\bLPs?\b|\b\dx?LP\b|\b12"|\b10"|\b7"|picture ?disc', re.I)
NOT_MUSIC_RE = re.compile(r"\b(?:t-?shirts?|hoodies?|sweatshirts?|longsleeves?|jackets?|posters?|patch(?:es)?|flags?|slipmats?|beanies?|caps?|totes?|mugs?|pins?|stickers?|puzzles?|books?|blankets?|dresses|shorts|socks)\b", re.I)
PREORDER_RE = re.compile(r"pre-?orders?", re.I)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")   # a full release date, as opposed to a partial/placeholder one — only these get a countdown
GENERIC_TAGS = {"vinyl", "lp", "cd", "music", "media", "metal", "rock", "punk", "hardcore", "new", "limited", "preorder", "colored", "coloured", "men", "women", "apparel", "merch"}
SPLITTERS = re.compile(r"\s+(?:/|&|\+|x|and|feat\.?|ft\.?|vs\.?)\s+", re.I)


def key(name: str) -> str:
    """Comparison key for artist names: case/punctuation-insensitive, ignoring 'The' whichever end it's on
    ('The Farm' and, as Crash Records styles it, 'Farm (The)' must compare equal)."""
    return content.norm(re.sub(r"^the\s+|\s*\(the\)\s*$", "", name.strip(), flags=re.I))


PREORDER_SUFFIX = re.compile(r"[\s*]*(?:[-–—]\s*)?[\(\[]?\s*pre[\s-]?orders?\s*[\)\]]?[\s*]*$", re.I)
FORMAT_WORD = re.compile(r"\b(?:Vinyl|LP|\d*x?12\"|Gatefold|Digipak|Picture|Colou?red|Transparent|Translucent|Clear|Black|White|Red|Blue|Green|Purple|Yellow|Orange|Pink|Silver|Gold|Grey|Gray)\b")
EDITION_CUT = re.compile(r"\s+(?:Ltd\.?|Limited|Indie Exclusive|Exclusive|Digipak|Gatefold|Box ?set|Boxset|Picture)\b", re.I)


def split_top_level(text: str) -> list[str]:
    """Split on ' - ' (or en/em dash), but never inside parentheses — so a year range like '(1986 - 1988)' or an
    exclusive-edition note like '(National Album Day 2026 UK Exclusive)' stays whole instead of being cut apart."""
    parts, depth, last, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        if c in "([":
            depth += 1
        elif c in ")]":
            depth = max(0, depth - 1)
        elif depth == 0 and text[i:i + 3] in (" - ", " – ", " — "):
            parts.append(text[last:i])
            last = i = i + 3
            continue
        i += 1
    parts.append(text[last:])
    return parts


def parse_title(title: str) -> tuple[str, str]:
    """('Artist', 'Album') from the styles these stores use: `Artist - Album - Format`, `Artist "Album" Format`, `Artist`."""
    t = re.sub(r"\s{2,}", " ", PREORDER_SUFFIX.sub("", title.strip()))
    quoted = re.search(r'\s["“]([^"”]+)(["”]|$)', t)
    dash = re.search(r"\s[-–—]\s", t)
    if quoted and (not dash or quoted.start() < dash.start()):
        album = quoted.group(1).strip()
        if not quoted.group(2):                                    # an unclosed quote (a typo some stores have): the album is
            if ")" in album:                                       # up to its closing parenthesis, else up to the first format/colour word
                album = album[:album.rindex(")") + 1]
            elif (m := FORMAT_WORD.search(album, 1)):
                album = album[:m.start()].strip()
        return t[:quoted.start()].strip(), album
    if dash:
        parts = split_top_level(t)
        return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")
    return t, ""


def cut_edition(album: str) -> str:
    """Drop trailing 'Ltd. <colour>' style text, unless it sits inside parentheses ('Turbo (Ltd. 40th Anniversary)')."""
    m = EDITION_CUT.search(album)
    if m and album[:m.start()].count("(") <= album[:m.start()].count(")"):
        return album[:m.start()].strip(" -–—:,")
    return album.strip()


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text.strip()) if w]


def _common(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if content.norm(x) != content.norm(y):
            break
        n += 1
    return n


def _balanced_parens(text: str) -> bool:
    return text.count("(") == text.count(")")


def _digit_led(word: str) -> bool:
    return bool(re.match(r"\(?\d", word))


def cluster_albums(albums: list[str]) -> dict[str, str]:
    """Group one artist's album strings that are really one album with different edition/colour text after it
    ('Cursum Perficio Ltd. Cream White…', 'Cursum Perficio Transparent Magenta' -> 'Cursum Perficio').
    Two strings share an album if they begin with the same 2+ words, or one is a whole-word start of the other —
    unless what comes right after is a number ('(Volume 3: …' vs '(Volume 2: …', 'Vol. 1' vs 'Vol. 3'), which means
    different volumes, not a colour variant of the same one."""
    uniq = list(dict.fromkeys(albums))
    parent = {a: a for a in uniq}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    ws = {a: _words(a) for a in uniq}
    for i, a in enumerate(uniq):
        for b in uniq[i + 1:]:
            c = _common(ws[a], ws[b])
            if c < 1 or not _balanced_parens(" ".join(ws[a][:c])) or any(len(ws[x]) > c and _digit_led(ws[x][c]) for x in (a, b)):
                continue
            if c >= 2 or c in (len(ws[a]), len(ws[b])):
                parent[find(a)] = find(b)
    clusters: dict[str, list[str]] = {}
    for a in uniq:
        clusters.setdefault(find(a), []).append(a)
    out: dict[str, str] = {}
    for members in clusters.values():
        if len(members) == 1:
            core = cut_edition(members[0])
        else:
            shared = min(_common(ws[members[0]], ws[m]) for m in members[1:])
            while shared > 1 and not _balanced_parens(" ".join(ws[members[0]][:shared])):    # a defensive backstop: never show a truncated '(…'
                shared -= 1
            core = cut_edition(" ".join(ws[members[0]][:max(shared, 1)]))
        for m in members:
            out[m] = core or m
    return out


def match_artist(p: dict[str, Any], followed: dict[str, str]) -> str | None:
    """The followed artist this product is by, or None. Matching is by the artist part of the title (whole names only,
    so 'The Ghost Inside' is not 'Ghost'), including split releases ('A / B'); tags are a careful fallback."""
    artist_part = p.get("_artist") or parse_title(p.get("title", ""))[0]
    for cand in [artist_part, *SPLITTERS.split(artist_part)]:
        if (k := key(cand)) and k in followed:
            return followed[k]
    tags = p.get("tags") or []
    tags = tags if isinstance(tags, list) else str(tags).split(",")
    for t in tags:
        k = key(t.split(":")[-1])
        if len(k) >= 3 and k not in GENERIC_TAGS and k in followed:
            return followed[k]
    return None


def is_vinyl(p: dict[str, Any]) -> bool:
    tags = p.get("tags") or []
    tags = " ".join(tags) if isinstance(tags, list) else str(tags)
    title, ptype = p.get("title", ""), p.get("product_type") or ""
    if NOT_MUSIC_RE.search(ptype) or (NOT_MUSIC_RE.search(title) and not VINYL_RE.search(title)):
        return False                                     # a T-shirt, poster, patch… (a record *with* a poster in the title still counts)
    return bool(VINYL_RE.search(f"{title} {ptype} {tags}"))


def has_preorder_flag(p: dict[str, Any]) -> bool:
    tags = p.get("tags") or []
    tags = " ".join(tags) if isinstance(tags, list) else str(tags)
    return bool(PREORDER_RE.search(f"{p.get('title', '')} {p.get('product_type', '')} {tags}"))


def _plausible(y: int, m: int, d: int, today: date) -> str | None:
    try:
        when = date(y, m, d)
    except ValueError:
        return None
    return when.isoformat() if today - timedelta(days=30) <= when <= today + timedelta(days=800) else None


FULL_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
BODY_DATE_RE = re.compile(r"Releas(?:e\s*Date|ed)\b[:\s]*(?:[A-Za-z]+\s+)?(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]+)\s+(\d{4})", re.I)
TAG_TEXT = re.compile(r"<[^>]+>")


def release_date(p: dict[str, Any], today: date) -> str | None:
    """Only where a store exposes one: Earache puts it in the product type (2026-11-13B), Deathwish in a meta-MM-DD-YY tag,
    Juno states it on the listing, Crash Records writes it in the product description ("Release Date Friday 4th December 2026")."""
    if p.get("_date"):
        return p["_date"]
    if m := re.match(r"^(20\d\d)-(\d\d)-(\d\d)[A-Za-z]?$", (p.get("product_type") or "").strip()):
        return _plausible(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
    tags = p.get("tags") or []
    for t in (tags if isinstance(tags, list) else str(tags).split(",")):
        if m := re.match(r"^meta-(\d\d)-(\d\d)-(\d\d)$", t.strip(), re.I):
            return _plausible(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)), today)
    if m := BODY_DATE_RE.search(TAG_TEXT.sub(" ", p.get("body_html") or "")):
        if (month := FULL_MONTHS.get(m.group(2).lower())):
            return _plausible(int(m.group(3)), month, int(m.group(1)), today)
    return None


NOT_VINYL_VARIANT_RE = re.compile(r"\b\d*CDs?\b|\bcassette\b|\btapes?\b|\bdigital\b|\bdownloads?\b|\bblu-?ray\b|\bdvds?\b|\bbooks?\b", re.I)


def vinyl_variants(p: dict[str, Any]) -> list[dict[str, Any]]:
    """This product's own variants that are vinyl — not, say, the CD or cassette edition sold as another option on the
    same product page (common on general record shops like Crash Records: one product, a 'Format' option with both a
    Vinyl LP and a CD choice, at different prices). Falls back to every variant when none of their names say anything —
    the product as a whole already passed is_vinyl()."""
    variants = p.get("variants") or []
    named = [v for v in variants if v.get("title") and v["title"] != "Default Title"]
    if not named:
        return variants
    vinyl = [v for v in named if not NOT_VINYL_VARIANT_RE.search(v["title"])]
    return vinyl or variants


def _price(p: dict[str, Any]) -> float | None:
    prices = []
    for v in vinyl_variants(p):
        try:
            prices.append(float(v["price"]))
        except (KeyError, TypeError, ValueError):
            pass
    return min(prices) if prices else None


def safe_image(url: Any) -> str:
    """Artwork we're willing to show: https, from a host the page's CSP allows. Anything else is dropped."""
    u = urlparse(url) if isinstance(url, str) else None
    return url if u and u.scheme == "https" and u.hostname in IMAGE_HOSTS else ""


def format_label(text: str) -> str:
    """A short format name ('LP', '2×LP', '7"', 'Picture disc') from a product's title/format text."""
    if re.search(r"\bpicture ?disc\b", text, re.I):
        return "Picture disc"
    if re.search(r"\bbox ?set\b", text, re.I):
        return "Box set"
    if m := re.search(r"\b(\d)\s?x\s?(?:LP|12\")", text, re.I):
        return f"{m.group(1)}×LP"
    if re.search(r"\b(?:double|2)\s?LP\b", text, re.I):
        return "2×LP"
    if m := re.search(r"\b(7|10|12)\"", text):
        return f'{m.group(1)}"'
    return "LP" if re.search(r"\bLP\b", text) else "Vinyl"


def edition_of(title: str) -> str:
    """What is left of a product title once its own artist and album (as this title literally spells them — not the
    followed artist's name, which a store can style differently: Crash Records writes 'Sensational Alex Harvey Band
    (The)') are taken out: 'Ltd. Cream White Vinyl LP'."""
    t = re.sub(r"\s{2,}", " ", PREORDER_SUFFIX.sub("", title).strip())   # the same normalisation parse_title applies, or its pieces won't line up for removal
    for part in parse_title(title):
        if part:
            t = re.sub(re.escape(part), "", t, count=1, flags=re.I)
    t = re.sub(r'["“”]', "", t)
    return re.sub(r"^[\s\-–—:,/]+|[\s\-–—:,/]+$", "", t)[:80]


def product_url(shop: dict[str, Any], p: dict[str, Any]) -> str:
    return p.get("_url") or f"{shop['url'].rstrip('/')}/products/{p['handle']}"


def build_items(products: list[dict[str, Any]], shop: dict[str, Any], followed: dict[str, str], *, currency: str, today: date) -> list[dict[str, Any]]:
    """Store products -> one item per (artist, album): vinyl only, on pre-order, by someone you follow, still available."""
    # a pre-order collection means everything in it is a pre-order; Juno is read from its pre-order pages; a Wix shop is
    # watched whole (anything by a followed artist that's for sale, in stock or on pre-order)
    need_flag = shop.get("kind") not in ("juno", "wix") and not shop.get("collection")
    matched: list[tuple[str, str, dict[str, Any]]] = []
    for p in products:
        if not is_vinyl(p) or (need_flag and not has_preorder_flag(p)):
            continue
        if p.get("variants") and not any(v.get("available", True) for v in vinyl_variants(p)):
            continue                                     # sold out — of the vinyl edition specifically; a CD reprint being in stock doesn't count
        artist = match_artist(p, followed)
        if artist:
            matched.append((artist, p.get("_album") or parse_title(p["title"])[1] or PREORDER_SUFFIX.sub("", p["title"]), p))

    cores: dict[str, dict[str, str]] = {}
    for artist in {a for a, _, _ in matched}:
        cores[artist] = cluster_albums([alb for a, alb, _ in matched if a == artist])

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for artist, raw, p in matched:
        title = cores[artist][raw]
        g = groups.setdefault((content.norm(artist), content.norm(title)), {"artist": artist, "title": title, "products": []})
        g["products"].append(p)

    sym = SYMBOLS.get(currency, "")
    money = lambda x: f"{sym}{x:.2f}" if x is not None else ""
    items = []
    for (ak, tk), g in groups.items():
        ps = sorted(g["products"], key=lambda p: p.get("published_at") or "")
        prices = [x for x in map(_price, ps) if x is not None]
        price_text = money(min(prices)) if prices else ""
        dates = [d for d in (release_date(p, today) for p in ps) if d]
        n = len(ps)
        fmt = format_label(ps[0].get("title", ""))
        if fmt == "Vinyl":                              # the title didn't say: fall back to the store's own format field (Juno)…
            fmt = format_label(ps[0].get("product_type") or "")
        if fmt == "Vinyl":                              # …or its tags (Crash Records puts the format there, e.g. "LP (Album)")
            t = ps[0].get("tags") or []
            fmt = format_label(" ".join(t) if isinstance(t, list) else str(t))
        stock = ps[0].get("_stock") or ""               # only whole-shop stores say: "in stock" / "pre-order"
        subtitle = " · ".join(x for x in (fmt, f"{n} variants" if n > 1 else "", f"from {price_text}" if n > 1 and price_text else price_text, stock, shop["name"]) if x)
        vendor = str(ps[0].get("_label") or ps[0].get("vendor") or "").strip()
        editions = [{"name": edition_of(p.get("title", "")) or p.get("_edition") or "Standard", "price": money(_price(p)), "url": product_url(shop, p)}
                    for p in ps[:8]]
        items.append({"external_id": f"{shop['id']}:{ak}:{tk}"[:200], "artist": g["artist"], "title": g["title"], "subtitle": subtitle,
                      "url": product_url(shop, ps[0]), "date": min(dates) if dates else None,
                      "meta": {"listed": (ps[0].get("published_at") or "")[:10], "variants": n, "price": price_text, "shop": shop["name"], "format": fmt,
                               "label": "" if content.norm(shop["name"]) in content.norm(vendor) or key(vendor) == key(g["artist"]) else vendor[:60],   # a store's own name isn't the record label; 'key' so 'Farm (The)' still matches 'The Farm'
                               "image": next((img for img in (safe_image(((p.get("images") or [{}])[0]).get("src")) for p in ps) if img), ""),
                               "editions": editions if n > 1 or editions[0]["name"] != "Standard" else []}})
    return items


# ---- Juno (HTML listing pages) ---------------------------------------------------------------------------------------

MONTHS = {m: i for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
JUNO_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Z][a-z]{2})\s+(\d{2})\b")
JUNO_PRICE_RE = re.compile(r"([£$€])\s?(\d+(?:\.\d\d)?)")
JUNO_SECTION_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
USER_AGENT = f"Citadel/{__version__} ({config.CONTACT})"


def juno_date(text: str) -> str | None:
    """'25 Sep 26' -> '2026-09-25'."""
    m = JUNO_DATE_RE.search(text or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(2000 + int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()
    except ValueError:
        return None


def parse_juno_page(html: str, section: str, page: int = 1) -> tuple[list[dict[str, Any]], int]:
    """One Juno listing page -> (products in the shape build_items expects, the last page number). Records without a price
    (sold out) are skipped."""
    soup = BeautifulSoup(html, "lxml")
    last = page
    for a in soup.select(f'a[href*="/{section}/preorders/"]'):
        if m := re.search(rf"/{re.escape(section)}/preorders/(\d+)/", a.get("href", "")):
            last = max(last, int(m.group(1)))
    out = []
    for el in soup.select("div.dv-item"):
        artist_a = el.select_one('.pl-info strong a[href^="/artists/"]')
        title_a = el.select_one('.pl-info a.text-md[href^="/products/"]')
        price_el = el.select_one(".pl-big-price .price_lrg")
        price = JUNO_PRICE_RE.search(price_el.get_text(" ", strip=True)) if price_el else None
        if not (artist_a and title_a and price):
            continue
        fmt_el = title_a.find_next_sibling("span", class_="text-primary")
        fmt = re.sub(r"^\((.*?)\)?$", r"\1", fmt_el.get_text(" ", strip=True)) if fmt_el else ""
        label_a = el.select_one('.pl-info a[href^="/labels/"]')
        text = el.select_one(".dvi-info").get_text(" ", strip=True) if el.select_one(".dvi-info") else ""
        est = el.select_one(".pl-big-price em")
        img = el.select_one(".dvi-img img")
        href = title_a["href"]
        out.append({"id": el.get("id") or href, "_artist": artist_a.get_text(strip=True), "_album": title_a.get_text(strip=True), "_edition": fmt or "Vinyl",
                    "title": f"{artist_a.get_text(strip=True)} - {title_a.get_text(strip=True)}", "product_type": fmt, "tags": [], "_label": label_a.get_text(strip=True) if label_a else "",
                    "variants": [{"price": price.group(2), "available": True}], "_currency": {"£": "GBP", "$": "USD", "€": "EUR"}[price.group(1)],
                    "_date": juno_date(est.get_text(" ", strip=True) if est else "") or juno_date(text),
                    "_url": f"https://www.juno.co.uk{href}", "images": [{"src": img["src"]}] if img and img.get("src") else []})
    return out, last


async def fetch_juno(client: httpx.AsyncClient, shop: dict[str, Any], *, max_pages: int = JUNO_MAX_PAGES, sleep=None) -> list[dict[str, Any]]:
    """Walk Juno's pre-order listing for each chosen section. Obeys robots.txt: only pages it allows, spaced by its Crawl-delay."""
    base = shop["url"].rstrip("/")
    robots = RobotFileParser()
    resp = await client.get(f"{base}/robots.txt")
    robots.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    delay = max(float(robots.crawl_delay(USER_AGENT) or 0), 2.0)
    seen: dict[str, dict[str, Any]] = {}
    first = True
    for section in [x.strip() for x in shop.get("collection", "").split(",") if x.strip()]:
        page = last = 1
        while page <= min(last, max_pages):
            url = f"{base}/{section}/preorders/" + (f"{page}/" if page > 1 else "")
            if not robots.can_fetch(USER_AGENT, url):
                raise RuntimeError(f"robots.txt doesn't allow {url}")
            if not first:
                await (sleep or asyncio.sleep)(delay)
            first = False
            r = await client.get(url)
            r.raise_for_status()
            found, last = parse_juno_page(r.text, section, page)
            if not found and page == 1:
                # A pre-order listing with no records at all means the page changed, not that Juno has nothing: say so
                # rather than quietly treating it as "everything sold out" (which would wipe the stored pre-orders).
                raise RuntimeError(f"Juno's {section} pre-order page had no readable records — has its layout changed?")
            for prod in found:
                seen.setdefault(prod["id"], prod)
            if not found:
                break
            page += 1
    return list(seen.values())


# ---- Wix (whole shop, from the product sitemap) ---------------------------------------------------------------------------

WIX_MAX_PAGES = 100             # product pages read per pass, at most (newest first)
SITEMAP_URL_RE = re.compile(r"<url>\s*<loc>([^<]+)</loc>(?:\s*<lastmod>([^<]+)</lastmod>)?", re.S)
LD_JSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
FOR_SALE = ("InStock", "PreOrder", "PreSale", "LimitedAvailability", "OnlineOnly")
INVISIBLE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]")      # e.g. the left-to-right mark in "Children Of Bodom \u200e- Hatebreeder"
# Nesi titles run the edition straight on from the album: "Will To Power Golden Vinyl LP", "Ategnatos Splatter Vinyl 2LP
# China Version". Cut at the first edition word; a colour/effect/version word only counts when a format word follows it
# later, so an album like "White Pony" (in "White Pony White Vinyl") keeps its name.
WIX_EDITION = re.compile(r"\s+(?:(?:Ltd|Limited)\b|\d*x?LP\b|\d+\"|Vinyl\b|(?:Golden|Gold|Silver|Splatter|Swirl|Smok(?:e|ing|y)|Floating|Liquid|Sand|Clear|"
                         r"Transparent|Colou?r(?:ed)?|Picture|Marble|Black|White|Red|Blue|Green|Purple|Yellow|Orange|Pink|Grey|Gray|"
                         r"Swedish|English|China|Chinese|Japan(?:ese)?)\b(?=.*\b(?:Vinyl|LP|Version)\b))", re.I)


def wix_album(album: str) -> str:
    for m in WIX_EDITION.finditer(album):
        if album[:m.start()].strip():
            return album[:m.start()].strip(" -–—:,/")
    return album.strip()


def slug_of(name: str) -> str:
    """How a shop turns a name into the start of a product address: 'At the Gates' -> 'at-the-gates', 'Bæst' -> 'bæst'."""
    return re.sub(r"[^\w]+", "-", name.lower()).strip("-")


def wix_candidates(sitemap: str, followed: dict[str, str]) -> list[tuple[str, str]]:
    """(product page, last changed) for every product whose address starts with a followed artist's name, newest first.
    Only a first cut: the real title decides (see match_artist), so 'ghost-bath-…' is read but never counted as Ghost."""
    prefixes = set()
    for name in followed.values():
        s = slug_of(name)
        prefixes |= {s, re.sub(r"^the-", "", s)}          # 'The X' is often filed as 'X'
    prefixes.discard("")
    out = []
    for loc, lastmod in SITEMAP_URL_RE.findall(sitemap):
        loc = html.unescape(loc.strip())
        slug = re.sub(r"^(?:copy-of-)+", "", unquote(loc.rstrip("/").rsplit("/", 1)[-1]).lower())   # Wix names duplicated products 'Copy of …'
        if any(slug == p or slug.startswith(p + "-") for p in prefixes):
            out.append((loc, (lastmod or "").strip()))
    return sorted(out, key=lambda x: x[1], reverse=True)


def parse_wix_product(page: str, url: str, lastmod: str = "") -> dict[str, Any] | None:
    """A product page's schema.org Product data -> a product in the shape build_items expects. None if there isn't one."""
    for m in LD_JSON_RE.finditer(page):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for d in data if isinstance(data, list) else [data]:
            if not isinstance(d, dict) or d.get("@type") != "Product" or not d.get("name"):
                continue
            offer = d.get("offers") or {}
            offer = (offer[0] if offer else {}) if isinstance(offer, list) else offer
            images = d.get("image") or []
            first = (images[0] if isinstance(images, list) and images else images) or {}
            img = first.get("contentUrl") if isinstance(first, dict) else first
            desc = html.unescape(str(d.get("description") or ""))
            brand = d.get("brand") or {}
            title = re.sub(r"\s+", " ", INVISIBLE.sub("", html.unescape(d["name"]))).strip()
            return {"id": url, "title": title, "_album": wix_album(parse_title(title)[1]), "product_type": "", "tags": [],
                    "_label": brand.get("name", "") if isinstance(brand, dict) else str(brand),
                    "variants": [{"price": str(offer.get("price") or ""), "available": str(offer.get("availability") or "").rsplit("/", 1)[-1] in FOR_SALE}],
                    "_currency": str(offer.get("priceCurrency") or ""), "_url": url, "images": [{"src": img}] if isinstance(img, str) else [],
                    "body_html": desc, "published_at": lastmod,
                    "_stock": "pre-order" if PREORDER_RE.search(f"{title} {desc}") else "in stock"}
    return None


async def fetch_wix(client: httpx.AsyncClient, shop: dict[str, Any], followed: dict[str, str], *, max_pages: int = WIX_MAX_PAGES, sleep=None) -> list[dict[str, Any]]:
    """Read the product sitemap, then (slowly, as robots.txt allows) the product pages of followed artists."""
    base = shop["url"].rstrip("/")
    robots = RobotFileParser()
    resp = await client.get(f"{base}/robots.txt")
    robots.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    delay = max(float(robots.crawl_delay(USER_AGENT) or 0), 2.0)
    sitemap_url = f"{base}/store-products-sitemap.xml"
    if not robots.can_fetch(USER_AGENT, sitemap_url):
        raise RuntimeError(f"robots.txt doesn't allow {sitemap_url}")
    r = await client.get(sitemap_url)
    r.raise_for_status()
    if not SITEMAP_URL_RE.search(r.text):
        # an empty product sitemap means the shop changed, not that it sells nothing: don't wipe what was found before
        raise RuntimeError(f"{shop['name']}'s product sitemap had no products — has the shop moved?")
    wanted = wix_candidates(r.text, followed)
    if len(wanted) > max_pages:
        log.warning("%s: %d product pages match followed artists; reading the newest %d", shop["name"], len(wanted), max_pages)
    out = []
    for url, lastmod in wanted[:max_pages]:
        if not robots.can_fetch(USER_AGENT, url):
            continue
        await (sleep or asyncio.sleep)(delay)
        page = await client.get(url)
        if page.status_code in (404, 410):
            continue                                   # the sitemap can lag a deleted product
        page.raise_for_status()
        if p := parse_wix_product(page.text, url, lastmod):
            out.append(p)
    return out


_currency_cache: dict[str, str] = {}


async def fetch_products(client: httpx.AsyncClient, shop: dict[str, Any], max_pages: int) -> list[dict[str, Any]]:
    base = shop["url"].rstrip("/")
    path = f"/collections/{shop['collection']}/products.json" if shop.get("collection") else "/products.json"
    out: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        resp = await client.get(base + path, params={"limit": 250, "page": page})
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        out += batch
        if len(batch) < 250:
            break
        await asyncio.sleep(0.7)                       # gentle on someone else's shop
    return out


async def currency_of(client: httpx.AsyncClient, shop: dict[str, Any]) -> str:
    base = shop["url"].rstrip("/")
    if base not in _currency_cache:
        try:
            resp = await client.get(base + "/meta.json")
            _currency_cache[base] = (resp.json().get("currency") or "") if resp.status_code == 200 else ""
        except (httpx.HTTPError, ValueError):
            _currency_cache[base] = ""
    return _currency_cache[base]


async def check_shop(url: str, collection: str, kind: str = "shopify") -> dict[str, Any]:
    """For Settings → 'Check this store': is it a store we can read, and what does its pre-order list look like?"""
    shop = {"url": url, "collection": collection or "", "id": "check", "name": "check", "kind": kind}
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=25, follow_redirects=True) as client:
        if kind == "wix":                                             # the sitemap alone says how much would be read
            try:
                r = await client.get(f"{url.rstrip('/')}/store-products-sitemap.xml")
                r.raise_for_status()
            except httpx.HTTPError as exc:
                return {"ok": False, "error": f"Couldn't read a Wix product sitemap there ({type(exc).__name__})."}
            followed = {key(a["name"]): a["name"] for a in content.list_artists() if a["status"] != "mute"}
            total, wanted = len(SITEMAP_URL_RE.findall(r.text)), wix_candidates(r.text, followed)
            return {"ok": bool(total), "products": total, "vinyl_preorders": len(wanted), "currency": "", "sample": [u.rsplit("/", 1)[-1][:70] for u, _ in wanted[:4]],
                    "note": f"Whole shop, not just pre-orders: {min(len(wanted), WIX_MAX_PAGES)} product pages by artists you follow would be read, a couple of seconds apart, once a day.",
                    **({} if total else {"error": "That sitemap lists no products."})}
        if kind == "juno":                                            # one page of the first section is enough to tell
            section = (collection.split(",")[0].strip() if collection else "") or "metal"
            try:
                r = await client.get(f"{url.rstrip('/')}/{section}/preorders/")
                r.raise_for_status()
                products, last = parse_juno_page(r.text, section)
            except httpx.HTTPError as exc:
                return {"ok": False, "error": f"Couldn't read Juno's {section} pre-orders ({type(exc).__name__})."}
            vinyl = [p for p in products if is_vinyl(p)]
            return {"ok": True, "products": len(products), "vinyl_preorders": len(vinyl), "currency": "GBP", "sample": [p["title"][:70] for p in vinyl[:4]],
                    "note": f"{last} pages in “{section}” (about {last * 5 // 60 + 1} min at the pace Juno asks for); each section is read slowly, and only once a day."}
        try:
            products = await fetch_products(client, shop, 1)
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "error": f"Couldn't read a Shopify catalogue there ({type(exc).__name__}). Check the address" + (" and collection name." if collection else ".")}
        currency = await currency_of(client, shop)
    vinyl = [p for p in products if is_vinyl(p) and (collection or has_preorder_flag(p))]
    return {"ok": True, "products": len(products), "vinyl_preorders": len(vinyl), "currency": currency,
            "sample": [p["title"][:70] for p in vinyl[:4]],
            "note": "" if collection else "No collection given: only products flagged as pre-order in the first 250 will be found — a pre-order collection (e.g. “pre-orders”) is better."}


COVER_MAX_BYTES = 8_000_000
COVERS_DIR = config.DATA_DIR / "covers"


async def fetch_manual_cover(url: str) -> str:
    """Best-effort: an owner-pasted image URL for a manually-added purchase, fetched and saved once rather than
    hotlinked — the page's CSP only allows img-src from a fixed, curated set of CDNs (see main.py), so an arbitrary
    URL would just be silently blocked in the browser. A failure here isn't different from "no picture given" —
    same stance as attach_art elsewhere in this app — so this never raises, it just returns "" on any problem."""
    try:
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if not ctype.startswith("image/") or len(resp.content) > COVER_MAX_BYTES:
                return ""
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}.get(ctype.split(";")[0].strip(), "jpg")
            COVERS_DIR.mkdir(parents=True, exist_ok=True)
            name = f"{uuid4().hex}.{ext}"
            (COVERS_DIR / name).write_bytes(resp.content)
            return f"/covers/{name}"
    except Exception:
        log.warning("Couldn't fetch manual cover image from %s", url, exc_info=True)
        return ""


class VinylWidget(Widget):
    id = "vinyl"
    title = "Vinyl radar"
    interval = 24 * 3600
    daily_at = "04:30"
    min_interval = 6 * 3600
    timeout = 1800                     # a Juno pass is ~100 polite page requests; the Shopify stores commit long before it ends
    defaults = {"shops": DEFAULT_SHOPS, "max_pages": 5}

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if "max_pages" in new and not (isinstance(new["max_pages"], int) and 1 <= new["max_pages"] <= 10):
            raise ValueError("max_pages must be a whole number from 1 to 10")
        if "shops" in new:
            shops = new["shops"]
            if not isinstance(shops, list) or len(shops) > 20:
                raise ValueError("shops must be a list of at most 20")
            seen, clean = set(), []
            for s in shops:
                name, url, coll = str(s.get("name", "")).strip()[:40], str(s.get("url", "")).strip(), str(s.get("collection", "")).strip()
                kind = s.get("kind") or "shopify"
                if kind not in ("shopify", "juno", "wix"):
                    raise ValueError("unknown store type")
                if not name:
                    raise ValueError("every store needs a name")
                http_url(url, field="store address")
                if kind == "juno":
                    sections = [x.strip() for x in coll.split(",") if x.strip()]
                    if not (urlparse(url).hostname or "").endswith("juno.co.uk"):
                        raise ValueError("a Juno store must be on juno.co.uk")
                    if not sections or len(sections) > 8 or not all(JUNO_SECTION_RE.fullmatch(x) for x in sections):
                        raise ValueError("Juno sections are genre names from its address, e.g. metal,rock,punk-hardcore")
                    coll = ",".join(sections)
                elif kind == "wix":
                    coll = ""                                     # a Wix shop is watched whole
                elif coll and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,80}", coll):
                    raise ValueError("collection must be the handle from the store's address, e.g. pre-orders")
                sid = re.sub(r"[^a-z0-9-]+", "-", str(s.get("id") or name).lower()).strip("-")[:40] or "shop"
                while sid in seen:
                    sid += "-2"
                seen.add(sid)
                clean.append({"id": sid, "name": name, "url": url.rstrip("/"), "kind": kind, "collection": coll, "enabled": bool(s.get("enabled", True))})
            merged["shops"] = clean
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        today = datetime.now(config.TZ).date()
        followed = {key(a["name"]): a["name"] for a in content.list_artists() if a["status"] != "mute"}
        crawled = dict((db.cache_get(self.id)["data"] or {}).get("crawled") or {})        # when each slow store was last read
        ok, errors, found = 0, [], 0
        async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True) as client:
            for shop in [s for s in settings["shops"] if s.get("enabled")]:
                try:
                    if shop.get("kind") == "juno":
                        seen = crawled.get(shop["id"]) or {}
                        if seen.get("sig") == shop["collection"] and time.time() - seen.get("at", 0) < JUNO_EVERY_HOURS * 3600:
                            ok += 1                                       # read recently: keep what we have, don't crawl again yet
                            continue
                        products = await fetch_juno(client, shop)
                        currency = "GBP"
                    elif shop.get("kind") == "wix":
                        products = await fetch_wix(client, shop, followed)
                        currency = next((p["_currency"] for p in products if p.get("_currency")), "")
                    else:
                        products = await fetch_products(client, shop, settings["max_pages"])
                        if not products:                                  # a store that suddenly lists nothing: renamed collection? keep what we have
                            log.warning("%s returned no products for '%s' — the collection may have been renamed or emptied; keeping what was found before", shop["name"], shop.get("collection") or "all products")
                            ok += 1
                            continue
                        currency = await currency_of(client, shop)
                    items = build_items(products, shop, followed, currency=currency, today=today)
                    content.upsert_items("vinyl", shop["id"], items, baseline=not content.has_source("vinyl", shop["id"]))
                    content.drop_missing("vinyl", shop["id"], {i["external_id"] for i in items})   # only after a successful look
                    if shop.get("kind") == "juno":
                        crawled[shop["id"]] = {"at": int(time.time()), "sig": shop["collection"]}
                    ok += 1
                    found += len(items)
                except Exception as exc:
                    errors.append(f"{shop['name']}: {type(exc).__name__}")
                    log.warning("%s couldn't be read: %s: %s", shop["name"], type(exc).__name__, exc)
        if errors and not ok:
            raise RuntimeError("; ".join(errors))
        return {"stores": ok, "errors": errors, "found": found, "crawled": crawled}

    def summarise(self, data: dict[str, Any] | None) -> str:
        d = data or {}
        return f"{d.get('stores', 0)} stores read, {d.get('found', 0)} pre-orders matched" + (f"; couldn't read {', '.join(d['errors'])}" if d.get("errors") else "")

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        enabled = {s["id"]: s["name"] for s in settings["shops"] if s.get("enabled")}
        out = content.list_items("vinyl", source_priority=tuple(s["id"] for s in settings["shops"]),
                                 keep=lambda i: i["source"] in enabled)
        today = now.date()
        preorders = []
        for p in content.list_purchased("vinyl"):
            in_days = (date.fromisoformat(p["date"]) - today).days if p["date"] and DATE_RE.match(p["date"]) else None
            preorders.append({**p, "in_days": in_days})
        return {**out, "stores": len(enabled), "following": len(content.list_artists()), "errors": (data or {}).get("errors", []), "preorders": preorders}


widget = VinylWidget()
