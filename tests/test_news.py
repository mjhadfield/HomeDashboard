"""The News page: settings upgrade, Most read, merging/ranking, learning, "never show" rules, the F1 spoiler shield,
the archive — and that a held-back headline never leaves the server."""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest

from app import api_news, content, db, newsrank, newstopics, spoilers
from app.widgets import f1cal, mostread, news

FIXTURES = Path(__file__).parent / "fixtures"
API = "/api/owner/news"
REAL_CLIENT = httpx.AsyncClient
NOW = time.time()

# Exactly what Mike had saved before sections existed (2026-10-09).
LEGACY = {"feeds": [{"id": "blabbermouth", "name": "Blabbermouth", "url": "https://blabbermouth.net/feed", "enabled": False},
                    {"id": "metal-injection", "name": "Metal Injection", "url": "https://metalinjection.net/feed", "enabled": False},
                    {"id": "louder", "name": "Louder", "url": "https://www.loudersound.com/feeds/all", "enabled": False},
                    {"id": "metalsucks", "name": "MetalSucks", "url": "https://www.metalsucks.net/feed/", "enabled": False},
                    {"id": "angry-metal-guy", "name": "Angry Metal Guy", "url": "https://www.angrymetalguy.com/feed/", "enabled": True},
                    {"id": "loudwire", "name": "Loudwire", "url": "https://loudwire.com/feed/", "enabled": False},
                    {"id": "theprp", "name": "Theprp", "url": "https://www.theprp.com/feed/", "enabled": False},
                    {"id": "udiscover", "name": "uDiscover Music", "url": "https://www.udiscovermusic.com/feed/", "enabled": False}],
          "include": [], "exclude": [], "max_age_days": 180}

SEC = {"id": "world", "name": "World", "enabled": True, "front": True, "keep_days": 30, "include": [], "exclude": [],
       "feeds": [{"id": "bbc", "name": "BBC News", "url": "https://bbc.example/rss", "enabled": True},
                 {"id": "grd", "name": "The Guardian", "url": "https://grd.example/rss", "enabled": True}]}
F1SEC = {**SEC, "id": "sport", "name": "Sport", "feeds": [{"id": "spt", "name": "BBC Sport", "url": "https://spt.example/rss", "enabled": True}]}


def calendar_data(now=NOW):
    """Round 1 finished long ago; round 2 (a sprint weekend) started an hour ago; round 3 is next week."""
    return {"season": 2026, "drivers": ["Lando Norris", "Norris", "Lewis Hamilton", "Hamilton", "Max Verstappen", "Verstappen"], "teams": ["McLaren", "Ferrari"],
            "rounds": [{"round": 1, "name": "Australian Grand Prix", "first_session": now - 40 * 86400, "first_label": "Qualifying", "race": now - 39 * 86400, "venues": ["Melbourne"]},
                       {"round": 2, "name": "Singapore Grand Prix", "first_session": now - 3600, "first_label": "Sprint qualifying", "race": now + 2 * 86400, "venues": ["Marina Bay"]},
                       {"round": 3, "name": "United States Grand Prix", "first_session": now + 7 * 86400, "first_label": "Qualifying", "race": now + 8 * 86400, "venues": ["Austin"]}]}


@pytest.fixture(autouse=True)
def _clean(client):
    def wipe():
        with db.db() as con:
            for t in ("feed_items", "news_topic_weights", "news_rules", "followed_artists"):
                con.execute(f"DELETE FROM {t}")
            con.execute("DELETE FROM widget_settings WHERE widget = 'news'")
            con.execute("DELETE FROM widget_cache WHERE widget IN ('news', 'mostread', 'f1cal')")
    wipe()
    yield
    wipe()


def settings(**over):
    db.put_settings("news", {**news.widget.defaults, "sections": [SEC, F1SEC], **over})


def story(source, eid, title, *, published=None, pos=0, summary="", url=None, **meta):
    published = NOW - 3600 if published is None else published
    m = {"feed": source, "pos": pos, "published": published, "summary": summary, "image": "", "topics": newstopics.extract(title), **meta}
    content.upsert_items("news", source, [{"external_id": eid, "artist": None, "title": title, "subtitle": "", "url": url or f"https://{source}.example/{eid}", "date": "2026-10-09", "meta": m}], baseline=True)
    return id_of(eid)


def id_of(eid):
    with db.db() as con:
        return con.execute("SELECT id FROM feed_items WHERE external_id = ?", (eid,)).fetchone()["id"]


def run(coro):
    return asyncio.run(coro)


# =============================================================== settings

def test_old_headlines_settings_become_the_music_section_exactly():
    s = news.upgrade({**news.widget.defaults, **LEGACY})
    assert [x["id"] for x in s["sections"]] == ["world", "uk", "tech", "science", "music"]
    music = s["sections"][-1]
    assert [(f["id"], f["enabled"]) for f in music["feeds"]] == [(f["id"], f["enabled"]) for f in LEGACY["feeds"]]   # same feeds, same switches
    assert music["keep_days"] == 180 and music["front"] is False
    assert not set(news.LEGACY_KEYS) & set(s)
    merged = news.widget.merge_settings({**news.widget.defaults, **LEGACY}, {"latest_hours": 24})      # the first save writes the new shape
    assert "feeds" not in merged and merged["sections"][-1]["feeds"][4]["id"] == "angry-metal-guy" and merged["latest_hours"] == 24


def test_tabloid_feeds_are_refused_on_save(owner_client):
    r = owner_client.put("/api/owner/widgets/news/settings", json={"sections": [{"name": "UK", "feeds": [{"name": "Sun", "url": "https://www.thesun.co.uk/feed/"}]}]})
    assert r.status_code == 400 or r.status_code == 422
    assert "tabloid" in r.text
    r = owner_client.post(f"{API}/test-feed", json={"url": "https://www.dailymail.co.uk/home/index.rss"})
    assert r.json() == {"ok": False, "error": "That's a tabloid site — those are never allowed."}


def test_settings_page_gets_suggestions_and_the_upgraded_shape(owner_client):
    db.put_settings("news", LEGACY)
    s = owner_client.get("/api/owner/widgets/news/settings").json()
    assert s["sections"][-1]["id"] == "music" and "feeds" not in s and "world" in s["suggested"] and "thesun.co.uk" in s["tabloids"]


# =============================================================== Most read

def test_most_read_parser_reads_the_real_page_and_only_the_list():
    items = mostread.parse((FIXTURES / "bbc_most_read.html").read_text())
    assert len(items) == 10 and [i["rank"] for i in items] == list(range(1, 11))
    assert items[1] == {"rank": 2, "title": "Firing squad execution to be livestreamed, Pentagon says", "url": "https://www.bbc.co.uk/news/articles/cmy0r96xygx6o"}
    assert all("decoy" not in i["url"] for i in items)                                 # a promo list before the block isn't Most read
    assert items[0]["title"] == "Alison Hammond: 'I thought I was having heart attack on air'"   # entities decoded


def test_a_changed_page_fails_loudly_and_keeps_the_last_list():
    with pytest.raises(ValueError, match="found 0 stories"):
        mostread.parse("<html><body><h2>Something else</h2><ol><li><a href='/news/articles/x'>x</a></li></ol></body></html>")


def test_most_read_fetch_tracks_movement_and_peak(monkeypatch):
    page = (FIXTURES / "bbc_most_read.html").read_text()
    monkeypatch.setattr(mostread.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)), **kw))
    first = run(mostread.widget.fetch({}))
    assert first["items"][0]["prev_rank"] is None and first["had_previous"] is False
    db.cache_put("mostread", {**first, "items": [{**first["items"][0], "rank": 4}, *first["items"][1:]]})   # an hour ago, #1 was #4
    second = run(mostread.widget.fetch({}))
    assert second["items"][0]["prev_rank"] == 4 and second["items"][1]["prev_rank"] == 2
    with db.db() as con:
        metas = [json.loads(r["meta"]) for r in con.execute("SELECT meta FROM feed_items WHERE source = 'bbc-most-read'")]
    assert len(metas) == 10 and sorted(m["peak_rank"] for m in metas) == list(range(1, 11))


# =============================================================== merging and ranking

def test_the_same_story_from_two_outlets_is_one_story_led_by_the_first_source():
    settings()
    story("grd", "g1", "Pentagon to livestream firing squad execution of Fort Hood gunman")
    story("bbc", "b1", "Firing squad execution to be livestreamed, Pentagon says", pos=3)
    story("bbc", "b2", "Drought declared in Yorkshire after dry weather", pos=0)
    story("grd", "g2", "Squad rotation leaves manager firing on all cylinders")        # shares words, but a different story
    v = api_news.section_view(SEC, api_news.Ctx())
    by_title = {s["title"]: s for s in v["stories"]}
    assert len(v["stories"]) == 3
    assert [a["name"] for a in by_title["Firing squad execution to be livestreamed, Pentagon says"]["also"]] == ["The Guardian"]
    assert v["stories"][0]["title"] == "Firing squad execution to be livestreamed, Pentagon says"   # two outlets beat a better feed position


def test_url_key_ignores_tracking_and_bbc_domains():
    assert newsrank.url_key("https://www.bbc.com/news/articles/abc?at_medium=RSS") == newsrank.url_key("https://www.bbc.co.uk/news/articles/abc/")


def test_front_page_lead_and_most_read_badges():
    settings(front_per_section=3)
    sid = story("bbc", "b1", "Drought declared in Yorkshire after dry weather", url="https://www.bbc.co.uk/news/articles/c6lye90xj478o?at_medium=RSS")
    db.cache_put("mostread", {"items": [{"rank": 7, "title": "Drought declared in Yorkshire after dry weather", "url": "https://www.bbc.co.uk/news/articles/c6lye90xj478o", "prev_rank": None}], "had_previous": False})
    mostread.store([{"rank": 7, "title": "Drought declared in Yorkshire after dry weather", "url": "https://www.bbc.co.uk/news/articles/c6lye90xj478o"}])
    content.mark_read(sid)
    front = run(api_news.front())
    world = next(b for b in front["sections"] if b["id"] == "world")
    assert world["lead"]["most_read"] == 7
    mr = front["most_read"]["items"][0]
    assert mr["rank"] == 7 and mr["read"] is True                                     # read in its section counts for Most read too


# =============================================================== learning

def test_topics_pick_out_names_categories_and_words():
    t = newstopics.extract("NASA’s James Webb Space Telescope spots oldest galaxy", [{"term": "Space"}, {"term": "World news"}])
    assert ["p", "NASA"] in t and ["p", "James Webb Space Telescope"] in t and ["c", "Space"] in t and ["w", "galaxy"] in t
    assert ["c", "World news"] not in t                                                 # section-wide categories say nothing
    assert newstopics.names("Drought declared in Yorkshire") == ["Yorkshire"]           # a first word is grammar, not a name
    assert newstopics.names("Starmer's Labour faces revolt") == ["Starmer", "Labour"]
    assert "SpaceX" in newstopics.names("SpaceX Launches Another Batch Of Starlink Satellites")   # Title Case: only internal capitals count


def test_a_vote_and_its_undo_leave_the_weights_exactly_where_they_were(owner_client):
    settings()
    iid = story("bbc", "b1", "Keir Starmer unveils NHS waiting list plan")
    assert owner_client.put(f"{API}/items/{iid}/vote", json={"vote": 1}).json()["vote"] == 1
    w = api_news.load_weights(time.time())
    assert w["p:keir starmer"] == pytest.approx(1.0) and w["s:bbc news"] == pytest.approx(newsrank.SOURCE_WEIGHT)
    owner_client.put(f"{API}/items/{iid}/vote", json={"vote": -1})                   # 👍 -> 👎 moves twice as far
    assert api_news.load_weights(time.time())["p:keir starmer"] == pytest.approx(-1.0, abs=1e-4)
    owner_client.put(f"{API}/items/{iid}/vote", json={"vote": 0})
    assert api_news.load_weights(time.time()) == {}                                    # nothing left behind
    assert owner_client.put(f"{API}/items/{iid}/vote", json={"vote": 2}).status_code == 422


def test_weights_fade_with_time():
    assert newsrank.decayed(1.0, 0, newsrank.HALF_LIFE) == pytest.approx(0.5)
    assert newsrank.decayed(-2.0, 100, 100) == -2.0


def test_disliked_topics_drop_down_then_get_held_back():
    settings()
    ctx = api_news.Ctx()
    a = story("bbc", "b1", "Love Island star joins Strictly line-up", pos=0)
    story("bbc", "b2", "Bank of England holds interest rates", pos=5)
    with db.db() as con:
        con.execute("INSERT INTO news_topic_weights (key, label, weight, votes, updated_at) VALUES ('p:love island', 'Love Island', -1.0, 1, ?)", (int(time.time()),))
    v = api_news.section_view(SEC, api_news.Ctx())
    assert [s["title"] for s in v["stories"]][-1] == "Love Island star joins Strictly line-up"    # demoted, not hidden yet
    with db.db() as con:
        con.execute("UPDATE news_topic_weights SET weight = -2.5")
    v = api_news.section_view(SEC, api_news.Ctx())
    assert [s["title"] for s in v["stories"]] == ["Bank of England holds interest rates"]
    assert v["hidden"][0]["id"] == a and "Love Island" in v["hidden"][0]["reason"]     # shown on request, with why
    assert ctx.lock                                                                    # (context builds fine with no calendar)


def test_the_story_you_gave_a_thumbs_down_waits_under_held_back(owner_client):
    settings()
    iid = story("bbc", "b1", "A perfectly ordinary story about councils")
    owner_client.put(f"{API}/items/{iid}/vote", json={"vote": -1})
    v = owner_client.get(f"{API}/section/world").json()
    assert v["stories"] == [] and v["hidden"][0]["reason"] == "You said less like this"


def test_never_show_rules_hide_everywhere_and_keep_most_read_ranks(owner_client):
    settings()
    story("bbc", "b1", "Love Island final draws record audience")
    story("grd", "g1", "Rishi Sunak backs new housing plan")
    mostread.store([{"rank": 1, "title": "Love Island final draws record audience", "url": "https://bbc.example/b1"},
                    {"rank": 2, "title": "Rishi Sunak backs new housing plan", "url": "https://grd.example/g1"}])
    db.cache_put("mostread", {"items": [{"rank": 1, "title": "x", "url": "https://bbc.example/b1"}, {"rank": 2, "title": "y", "url": "https://grd.example/g1"}], "had_previous": False})
    assert owner_client.post(f"{API}/rules", json={"term": "Love Island"}).status_code == 201
    assert owner_client.post(f"{API}/rules", json={"term": "love island"}).json()["id"] == api_news.load_rules()[0]["id"]   # no duplicates
    owner_client.post(f"{API}/rules", json={"term": "The Guardian", "kind": "source", "section": "world"})
    v = owner_client.get(f"{API}/section/world").json()
    assert v["stories"] == [] and v["muted"] == 2
    mr = owner_client.get(f"{API}/front").json()["most_read"]["items"]
    assert mr[0] == {"id": mr[0]["id"], "rank": 1, "muted": "Love Island"} and mr[1]["title"] == "Rishi Sunak backs new housing plan"   # the outlet rule is World-only
    arch = owner_client.get(f"{API}/archive").json()["items"]
    assert all("Love Island" not in (i.get("title") or "") for i in arch)
    rid = api_news.load_rules()[0]["id"]
    assert owner_client.delete(f"{API}/rules/{rid}").status_code == 200 and owner_client.delete(f"{API}/rules/{rid}").status_code == 404


def test_section_hide_and_only_show_lists_apply_when_reading():
    settings(sections=[{**SEC, "exclude": ["cricket"]}, F1SEC])
    story("bbc", "b1", "England win the cricket")
    story("bbc", "b2", "Parliament returns")
    assert [s["title"] for s in api_news.section_view({**SEC, "exclude": ["cricket"]}, api_news.Ctx())["stories"]] == ["Parliament returns"]
    assert [s["title"] for s in api_news.section_view({**SEC, "include": ["cricket"]}, api_news.Ctx())["stories"]] == ["England win the cricket"]


def test_switching_a_feed_off_hides_its_stories_at_once():
    off = {**SEC, "feeds": [SEC["feeds"][0], {**SEC["feeds"][1], "enabled": False}]}
    settings(sections=[off, F1SEC])
    story("bbc", "b1", "From the BBC")
    story("grd", "g1", "From the Guardian")
    assert [s["source"] for s in api_news.section_view(off, api_news.Ctx())["stories"]] == ["BBC News"]


# =============================================================== F1 spoiler shield

def test_lock_follows_the_calendar_and_what_you_have_watched():
    cal = calendar_data()
    assert spoilers.lock_state(cal, NOW, {"season": 2026, "round": 1}, NOW)["locked"] is True
    lock = spoilers.lock_state(cal, NOW, {"season": 2026, "round": 1}, NOW)
    assert lock["since"] == cal["rounds"][1]["first_session"] and [r["round"] for r in lock["rounds"]] == [2]   # sprint qualifying starts the lock
    free = spoilers.lock_state(cal, NOW, {"season": 2026, "round": 2}, NOW)
    assert free["locked"] is False and free["next"]["round"] == 3
    behind = spoilers.lock_state(cal, NOW, {"season": 2026, "round": 0}, NOW)
    assert [r["round"] for r in behind["rounds"]] == [1, 2] and behind["since"] == cal["rounds"][0]["first_session"]
    assert spoilers.lock_state(cal, NOW, {"season": 2025, "round": 22}, NOW)["rounds"][0]["round"] == 1   # last season's progress doesn't count
    assert spoilers.lock_state(cal, NOW, None, NOW)["watched"] == 1                    # nothing marked: races over a week old count as seen
    assert spoilers.lock_state(cal, NOW, None, NOW, enabled=False) == {"on": False, "locked": False}


def test_no_or_stale_calendar_hides_every_f1_story():
    assert spoilers.lock_state(None, None, None, NOW) == {"on": True, "locked": True, "since": 0, "reason": "no-calendar", "rounds": []}
    assert spoilers.lock_state(calendar_data(), NOW - 15 * 86400, None, NOW)["reason"] == "stale"


def test_f1_matching_is_generous_but_not_silly():
    m = spoilers.Matcher(calendar_data(), ["Toto Wolff"], ["Marina Bay"])
    yes = ["Norris takes pole in Singapore", "Lewis Hamilton signs new deal", "Pressure mounts on Hamilton", "Singapore GP: what we learned", "Formula One chief speaks",
           "Ferrari sack team boss", "Toto Wolff fumes", "Chaos at Marina Bay", "Hülkenberg scores points"]
    no = ["Margaret Hamilton, Apollo coder, dies. Hamilton was 90", "GP surgeries under strain", "Hayley Williams tribute", "A stroll in the park", "Budget day"]
    assert [t for t in yes if not m({"title": t})] == ["Hülkenberg scores points"]     # not in this fixture's driver list (the real one has him)
    assert [t for t in no if m({"title": t})] == []
    assert m({"title": "x", "url": "https://www.bbc.co.uk/sport/formula1/articles/abc"})
    assert spoilers.Matcher({**calendar_data(), "drivers": ["Nico Hülkenberg", "Hülkenberg"]})({"title": "Hulkenberg scores points"})   # accents either way


def test_shield_only_hides_what_was_published_since_the_lock():
    cal = calendar_data()
    lock = spoilers.lock_state(cal, NOW, {"season": 2026, "round": 1}, NOW)
    m = spoilers.Matcher(cal)
    assert spoilers.hidden({"title": "Norris wins", "meta": {"published": NOW - 60}}, lock, m)
    assert not spoilers.hidden({"title": "Norris preview", "meta": {"published": NOW - 7200}}, lock, m)   # before qualifying: can't know the result
    assert spoilers.hidden({"title": "Norris wins", "meta": {"published": None}}, lock, m)               # unknown time: assume now
    assert not spoilers.hidden({"title": "Norris wins", "meta": {"published": NOW, "revealed": True}}, lock, m)


def _f1_world(watched=1):
    settings(sections=[SEC, F1SEC], f1_watched={"season": 2026, "round": watched})
    db.cache_put("f1cal", calendar_data())
    hid = story("spt", "s1", "Norris storms to Singapore sprint pole", summary="Verstappen crashes", url="https://www.bbc.co.uk/sport/formula1/articles/s1")
    mostread.store([{"rank": 1, "title": "Norris storms to Singapore sprint pole", "url": "https://www.bbc.co.uk/sport/formula1/articles/s1"}])
    db.cache_put("mostread", {"items": [{"rank": 1, "title": "Norris storms to Singapore sprint pole", "url": "https://www.bbc.co.uk/sport/formula1/articles/s1"}], "had_previous": False})
    return hid


def test_a_held_back_headline_never_leaves_the_server(owner_client):
    _f1_world()
    for path in ("/front", "/section/sport", "/archive", "/archive?section=mostread"):
        body = owner_client.get(API + path).text
        assert "Norris" not in body and "Verstappen" not in body and "sprint pole" not in body and "formula1" not in body, path
        assert '"shielded":true' in body.replace(" ", ""), path
    assert owner_client.get(f"{API}/archive?q=Norris").json()["items"] == []            # a search can't hint at it either
    assert owner_client.get(f"{API}/front").json()["shield"]["rounds"][0]["name"] == "Singapore Grand Prix"


def test_watching_the_race_lifts_the_shield_and_reveal_shows_just_one(owner_client):
    hid = _f1_world()
    r = owner_client.post(f"{API}/items/{hid}/reveal").json()
    assert r["title"] == "Norris storms to Singapore sprint pole"
    assert "Norris" in owner_client.get(f"{API}/section/sport").text                   # stays revealed
    _f1_world()
    with db.db() as con:
        con.execute("UPDATE feed_items SET meta = json_remove(meta, '$.revealed')")
    assert "Norris" not in owner_client.get(f"{API}/section/sport").text
    assert owner_client.post(f"{API}/f1/watched", json={"round": 2}).json()["shield"]["locked"] is False
    assert "Norris" in owner_client.get(f"{API}/section/sport").text
    assert news.load()["f1_watched"] == {"season": 2026, "round": 2}


def test_f1_calendar_parsing():
    races = [{"season": "2026", "round": "17", "raceName": "Singapore Grand Prix", "date": "2026-10-11", "time": "12:00:00Z",
              "Circuit": {"circuitName": "Marina Bay Street Circuit", "Location": {"locality": "Marina Bay", "country": "Singapore"}},
              "Qualifying": {"date": "2026-10-10", "time": "13:00:00Z"}, "Sprint": {"date": "2026-10-10", "time": "09:00:00Z"},
              "SprintQualifying": {"date": "2026-10-09", "time": "12:30:00Z"}}]
    r = f1cal.parse_races(races)[0]
    assert r["round"] == 17 and r["first_label"] == "Sprint qualifying" and r["sprint"] is True
    assert r["first_session"] == 1791549000 and r["race"] == 1791720000
    assert r["venues"] == ["Marina Bay", "Marina Bay Street Circuit"]                   # never the whole country


# =============================================================== archive, saving, pruning

def test_archive_pages_filters_and_saved(owner_client):
    settings()
    ids = [story("bbc" if n % 2 else "grd", f"a{n}", f"Story number {n} about councils", published=NOW - n * 600) for n in range(25)]
    owner_client.post(f"{API}/items/{ids[3]}/save")
    owner_client.put(f"{API}/items/{ids[4]}/vote", json={"vote": 1})
    owner_client.post(f"{API}/items/{ids[5]}/read")
    first = owner_client.get(f"{API}/archive?limit=10").json()
    assert [i["id"] for i in first["items"]] == ids[:10] and first["next"]
    seen = [i["id"] for i in first["items"]]
    nxt = first["next"]
    while nxt:
        page = owner_client.get(f"{API}/archive", params={"limit": 10, "before": nxt["before"], "before_id": nxt["before_id"]}).json()
        seen += [i["id"] for i in page["items"]]
        nxt = page["next"]
    assert seen == ids                                                                  # every story once, newest first
    assert [i["id"] for i in owner_client.get(f"{API}/archive?saved=true").json()["items"]] == [ids[3]]
    assert [i["id"] for i in owner_client.get(f"{API}/archive?liked=true").json()["items"]] == [ids[4]]
    assert ids[5] not in [i["id"] for i in owner_client.get(f"{API}/archive?unread=true").json()["items"]]
    assert {i["source"] for i in owner_client.get(f"{API}/archive?source=bbc").json()["items"]} == {"BBC News"}
    assert [i["title"] for i in owner_client.get(f"{API}/archive?q=number 7 about").json()["items"]] == ["Story number 7 about councils"]
    owner_client.delete(f"{API}/items/{ids[3]}/save")
    assert owner_client.get(f"{API}/archive?saved=true").json()["items"] == []


def test_saved_and_liked_stories_outlive_their_section():
    settings()
    keep_saved = story("bbc", "s", "Saved one")
    keep_liked = story("bbc", "l", "Liked one", vote=1)
    story("bbc", "x", "Ordinary one")
    content.set_status(keep_saved, "pinned")
    with db.db() as con:
        con.execute("UPDATE feed_items SET last_seen = ?", (int(time.time()) - 60 * 86400,))
    news.widget._prune(news.load())
    left = {r["title"] for r in api_news.rows()}
    assert left == {"Saved one", "Liked one"}


def test_learning_endpoints(owner_client):
    settings()
    iid = story("bbc", "b1", "Keir Starmer unveils NHS waiting list plan")
    owner_client.put(f"{API}/items/{iid}/vote", json={"vote": 1})
    L = owner_client.get(f"{API}/learning").json()
    assert L["topics"][0]["label"] == "Keir Starmer" and L["topics"][0]["weight"] == 1.0
    assert owner_client.delete(f"{API}/learning/topics", params={"key": "p:keir starmer"}).status_code == 200
    assert "Keir Starmer" not in [t["label"] for t in owner_client.get(f"{API}/learning").json()["topics"]]
    assert owner_client.post(f"{API}/learning/reset").json()["votes_cleared"] == 1
    assert owner_client.get(f"{API}/learning").json()["topics"] == []


def test_test_feed_reports_what_it_found(owner_client, monkeypatch):
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Science Daily-ish</title>
      <item><title>Comet spotted</title><link>https://ex.com/1</link><guid>1</guid></item></channel></rss>"""
    monkeypatch.setattr(api_news.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=rss)), **kw))
    r = owner_client.post(f"{API}/test-feed", json={"url": "https://ex.com/feed"}).json()
    assert r["ok"] is True and r["title"] == "Science Daily-ish" and r["items"] == 1 and r["sample"] == ["Comet spotted"]
    monkeypatch.setattr(api_news.httpx, "AsyncClient", lambda **kw: REAL_CLIENT(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>hi</html>")), **kw))
    assert "isn't a feed" in owner_client.post(f"{API}/test-feed", json={"url": "https://ex.com/page"}).json()["error"]


def test_guests_cannot_reach_the_news_api(guest_client):
    for method, path in (("get", "/front"), ("get", "/archive"), ("put", "/items/1/vote"), ("post", "/rules"), ("post", "/f1/watched")):
        assert getattr(guest_client, method)(API + path, headers={"X-Hub": "1"}).status_code == 403, path


# =============================================================== leanings, duplicates, your artists

def test_science_leans_away_from_the_environment():
    sci = {**SEC, "id": "science", "name": "Science", "favour": news.DEFAULT_LEANINGS["science"]["favour"], "play_down": news.DEFAULT_LEANINGS["science"]["play_down"]}
    settings(sections=[sci, F1SEC])
    story("bbc", "e1", "Swiss glaciers suffer disastrous year of ice loss", pos=0)                 # the BBC's top story…
    story("bbc", "e2", "Warning more homes will be uninsurable", pos=1, url="https://bbc.example/weather/articles/x")
    story("grd", "e3", "Record El Nino threatens reef", pos=0, categories=None)
    with db.db() as con:                                                                         # …and a feed category says "environment"
        con.execute("UPDATE feed_items SET meta = json_set(meta, '$.topics', json('[[\"c\", \"Climate change\"]]')) WHERE external_id = 'e3'")
    story("grd", "s1", "Astronomers spot the oldest galaxy yet", pos=9)
    story("bbc", "s2", "Chemistry Nobel goes to mirror-image molecules", pos=7)
    titles = [s["title"] for s in api_news.section_view(sci, api_news.Ctx())["stories"]]
    assert set(titles[:2]) == {"Astronomers spot the oldest galaxy yet", "Chemistry Nobel goes to mirror-image molecules"}
    assert len(titles) == 5                                                                     # played down, never hidden


def test_leanings_match_categories_words_and_addresses():
    it = {"title": "A storm is coming", "url": "https://x.example/news/weather/1", "meta": {"summary": "", "topics": [["c", "Space"]]}}
    assert newsrank._matches(it, ["space", "storm", "/weather/", "galaxy", "storms"]) == ["space", "storm", "/weather/"]
    assert newsrank.leaning(it, ["space"], ["storm"], news.LEANING) == pytest.approx(1.5 - 2.0)


def test_saved_sections_pick_up_leanings_and_your_artists():
    saved = news.widget.merge_settings({}, {"sections": [{"id": "science", "name": "Science"}, {"id": "music", "name": "Music"}]})
    for sec in saved["sections"]:                         # as if saved before these settings existed
        for k in ("favour", "play_down", "personal"):
            sec.pop(k)
    up = {s["id"]: s for s in news.upgrade(saved)["sections"]}
    assert "climate" in up["science"]["play_down"] and "physics" in up["science"]["favour"] and up["science"]["personal"] is False
    assert up["music"]["personal"] is True and up["music"]["favour"] == []
    kept = news.upgrade({**saved, "sections": [{**saved["sections"][0], "favour": [], "play_down": []}]})["sections"][0]
    assert kept["play_down"] == [] and kept["favour"] == []                                   # emptied on purpose stays empty


def test_one_article_listed_twice_is_one_story():
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>BBC</title>
      <item><title>Rainmakers</title><link>https://www.bbc.co.uk/news/articles/abc?at_medium=RSS</link><guid>https://www.bbc.co.uk/news/articles/abc#0</guid></item>
      <item><title>Rainmakers</title><link>https://www.bbc.co.uk/news/articles/abc?at_medium=RSS</link><guid>https://www.bbc.co.uk/news/articles/abc#3</guid></item></channel></rss>"""
    assert [i["external_id"] for i in news.parse_feed(rss, {"id": "b", "name": "BBC"}, cutoff=0)] == ["bbc.co.uk/news/articles/abc"]


def test_old_duplicates_fold_into_one_keeping_your_marks():
    for eid, extra in (("https://bbc.example/a#0", {"read": True}), ("https://bbc.example/a#1", {"vote": 1}), ("https://bbc.example/b#0", {})):
        story("bbc", eid, "Story", url=eid.split("#")[0] + "?at_medium=RSS", **extra)
    assert news.rekey() == 2 and news.rekey() == 0                                              # idempotent
    rows = {r["meta"].get("read"): r for r in api_news.rows()}
    kept = next(r for r in api_news.rows() if r["url"].startswith("https://bbc.example/a"))
    assert len(api_news.rows()) == 2 and kept["meta"]["read"] is True and kept["meta"]["vote"] == 1
    with db.db() as con:
        assert {r[0] for r in con.execute("SELECT external_id FROM feed_items")} == {"bbc.example/a", "bbc.example/b"}


@pytest.fixture()
def my_artists(monkeypatch):
    from app import newsmusic
    from app.music import library
    library.forget()
    mine = {"gojira": {"name": "Gojira", "affinity": 2.9}, "ghost": {"name": "Ghost", "affinity": 2.5}, "muse": {"name": "Muse", "affinity": 0.9},
            "bringmethehorizon": {"name": "Bring Me the Horizon", "affinity": 1.2}, "motorhead": {"name": "Motörhead", "affinity": 1.0}}
    monkeypatch.setattr(newsmusic, "artists", lambda: mine)
    yield newsmusic
    library.forget()


def test_your_artists_are_matched_sensibly(my_artists):
    names = lambda t: [a["name"] for a in my_artists.match(t)]
    assert names("Gojira and Ghost co-headline") == ["Gojira", "Ghost"]                          # best-loved first
    assert names("A ghost town in Texas") == []                                                  # one-word names need their capital
    assert names("Muse tease album") == []                                                       # an everyday word, and not one you play much
    assert names("Bring Me The Horizon drop single") == ["Bring Me the Horizon"]                 # several words: any case
    assert names("Motorhead box set") == ["Motörhead"]                                           # accents folded


def test_music_shows_your_artists_first_and_only_by_default(my_artists, owner_client):
    music = {**SEC, "id": "music", "name": "Music", "personal": True}
    settings(sections=[music, F1SEC])
    story("bbc", "m1", "Gojira announce European tour", pos=5)
    story("grd", "m2", "Ghost share new video", pos=0)
    story("bbc", "m3", "Some band you've never heard of signs deal", pos=0)
    content.upsert_items("release", "musicbrainz", [{"external_id": "r", "artist": "Gojira", "title": "Mega", "date": "2099-11-14", "meta": {}}], baseline=True)
    with db.db() as con:
        con.execute("INSERT INTO gigs (artist, venue, city, date, created_at) VALUES ('Ghost', 'O2', 'Manchester', '2099-12-26', 0)")
    v = owner_client.get(f"{API}/section/music").json()
    by = {s["title"]: s for s in v["stories"]}
    assert set(by) == {"Gojira announce European tour", "Ghost share new video"} and v["others"] == 1
    goj, gho = by["Gojira announce European tour"], by["Ghost share new video"]
    assert goj["artists"] == ["Gojira"] and goj["links"][0]["kind"] == "release" and goj["links"][0]["title"] == "Mega"
    assert gho["links"][0] == {"kind": "gig", "date": "2099-12-26", "where": "O2, Manchester", "url": None, "artist": "Ghost"}
    every = owner_client.get(f"{API}/section/music?everything=true").json()
    assert len(every["stories"]) == 3 and every["stories"][-1]["title"].startswith("Some band")
    front = owner_client.get(f"{API}/front").json()["sections"][0]
    assert front["personal"] is True and front["lead"]["title"] in by
    with db.db() as con:
        con.execute("DELETE FROM gigs")


# =============================================================== text articles only, shared front-page tiles

def test_programmes_podcasts_and_videos_are_not_articles():
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>BBC</title>
      <item><title>BBC Inside Science</title><link>https://www.bbc.co.uk/sounds/play/w3ct9785?at_medium=RSS</link></item>
      <item><title>Tech Now</title><link>https://www.bbc.co.uk/iplayer/episode/m0031qjy</link></item>
      <item><title>Nobel physics explained</title><link>https://www.theguardian.com/science/video/2026/oct/06/nobel</link></item>
      <item><title>Burnham's gamble - podcast</title><link>https://www.theguardian.com/politics/audio/2026/sep/29/burnham</link></item>
      <item><title>Watch: Priest play without Hill</title><link>https://blabbermouth.net/news/watch-judas-priest</link></item>
      <item><title>Comet spotted</title><link>https://www.bbc.co.uk/news/articles/abc</link></item></channel></rss>"""
    assert [i["title"] for i in news.parse_feed(rss, {"id": "b", "name": "BBC"}, cutoff=0)] == ["Watch: Priest play without Hill", "Comet spotted"]


def test_stored_programme_pages_are_cleared_unless_you_kept_them():
    story("bbc", "p1", "BBC Inside Science", url="https://www.bbc.co.uk/sounds/play/w1")
    saved = story("bbc", "p2", "Tech Life", url="https://www.bbc.co.uk/sounds/play/w2")
    content.set_status(saved, "pinned")
    story("bbc", "a1", "Comet spotted", url="https://www.bbc.co.uk/news/articles/abc")
    news.rekey()
    assert {r["title"] for r in api_news.rows()} == {"Tech Life", "Comet spotted"}


def test_world_and_uk_share_one_front_page_tile(owner_client):
    uk = {**SEC, "id": "uk", "name": "UK", "front_with": "world", "feeds": [{"id": "bbcuk", "name": "BBC News", "url": "https://uk.example/rss", "enabled": True}]}
    settings(sections=[SEC, uk, {**F1SEC, "front": False}], front_per_section=3)
    for n in range(5):
        story("bbc", f"w{n}", f"World story number {n} about diplomacy", pos=n)
    story("bbcuk", "u1", "UK story about councils", pos=9, published=NOW - 30 * 3600)    # weaker than every World story…
    tiles = owner_client.get(f"{API}/front").json()["sections"]
    assert [t["name"] for t in tiles] == ["World & UK"] and [t["id"] for t in tiles[0]["tabs"]] == ["world", "uk"]
    shown = [tiles[0]["lead"], *tiles[0]["items"]]
    assert len(shown) == 3 and "UK story about councils" in [s["title"] for s in shown]          # …but it still gets a place
    assert next(s for s in shown if s["title"] == "UK story about councils")["sole"] is True             # and survives screen-fitting


def test_front_with_is_validated_and_uk_shares_with_world_by_default():
    secs = news.widget.merge_settings({}, {"sections": [{"name": "A", "front_with": "a"}, {"name": "B", "front_with": "a"}, {"name": "C", "front_with": "nope"}]})["sections"]
    assert [s["front_with"] for s in secs] == [None, "a", None]
    assert {s["id"]: s["front_with"] for s in news.upgrade(news.widget.defaults)["sections"]}["uk"] == "world"
