"""Diary: iCal expansion (recurrence, all-day, cancelled, time zones) and secret handling."""
from datetime import datetime, timedelta

import pytest

from app import config
from app.widgets import diary

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:a
DTSTART;TZID=Europe/London:20260926T180000
DTEND;TZID=Europe/London:20260926T210000
SUMMARY:Gig night
LOCATION:O2 Academy\\, Leeds
END:VEVENT
BEGIN:VEVENT
UID:b
DTSTART;VALUE=DATE:20260927
DTEND;VALUE=DATE:20260928
SUMMARY:Payday
END:VEVENT
BEGIN:VEVENT
UID:c
DTSTART;TZID=Europe/London:20260923T190000
DTEND;TZID=Europe/London:20260923T200000
RRULE:FREQ=WEEKLY;COUNT=3
SUMMARY:Five-a-side
END:VEVENT
BEGIN:VEVENT
UID:d
DTSTART;TZID=Europe/London:20260924T100000
DTEND;TZID=Europe/London:20260924T110000
STATUS:CANCELLED
SUMMARY:Cancelled thing
END:VEVENT
BEGIN:VEVENT
UID:e
DTSTART;VALUE=DATE:20260919
DTEND;VALUE=DATE:20260924
SUMMARY:Holiday
END:VEVENT
BEGIN:VEVENT
UID:f
DTSTART:20260929T170000Z
DTEND:20260929T180000Z
SUMMARY:UTC meeting
END:VEVENT
END:VCALENDAR
"""
START = datetime(2026, 9, 21, tzinfo=config.TZ)
CAL = {"name": "Test", "colour": "violet"}


def events():
    return diary.expand(ICS, CAL, START, START + timedelta(days=30))


def by_title(title):
    return [e for e in events() if e["title"] == title]


def test_timed_event_fields():
    (e,) = by_title("Gig night")
    assert (e["date"], e["time"], e["end_time"], e["all_day"]) == ("2026-09-26", "18:00", "21:00", False)
    assert e["location"] == "O2 Academy, Leeds" and e["cal"] == "Test"


def test_all_day_event_has_no_time():
    (e,) = by_title("Payday")
    assert e["all_day"] and e["time"] is None and e["date"] == "2026-09-27" and e["until"] is None


def test_recurring_events_are_expanded():
    assert [e["date"] for e in by_title("Five-a-side")] == ["2026-09-23", "2026-09-30", "2026-10-07"]


def test_cancelled_events_are_skipped():
    assert by_title("Cancelled thing") == []


def test_event_already_under_way_shows_from_today_with_an_end_date():
    (e,) = by_title("Holiday")
    assert e["date"] == "2026-09-21" and e["until"] == "2026-09-23"


def test_utc_times_are_converted_to_local_time():
    (e,) = by_title("UTC meeting")
    assert e["time"] == "18:00"       # 17:00Z is 18:00 in London (BST)


def test_view_drops_past_events():
    data = {"events": [{"date": "2026-09-20", "until": None}, {"date": "2026-09-22", "until": None},
                       {"date": "2026-09-18", "until": "2026-09-22"}], "calendars": []}
    out = diary.widget.view(data, {}, datetime(2026, 9, 21, 9, tzinfo=config.TZ))
    assert [e["date"] for e in out["events"]] == ["2026-09-22", "2026-09-18"]


SECRET = "https://calendar.example/private-abc/basic.ics"
OLD = {"calendars": [{"id": "c1", "name": "Main", "colour": "violet", "url": SECRET}], "horizon_days": 60}


def test_calendar_urls_are_never_exposed():
    public = diary.widget.public_settings(OLD)
    assert SECRET not in str(public) and public["calendars"][0]["has_url"] is True and public["calendars"][0]["id"] == "c1"


def test_editing_a_calendar_keeps_its_secret_even_when_renamed():
    """Regression: calendars used to be matched by name, so renaming one silently disconnected it."""
    merged = diary.widget.merge_settings(OLD, {"calendars": [{"id": "c1", "name": "A completely new name", "colour": "silver"}]})
    cal = merged["calendars"][0]
    assert cal["url"] == SECRET and cal["name"] == "A completely new name" and cal["colour"] == "silver" and cal["id"] == "c1"


def test_calendars_stored_before_ids_existed_keep_working():
    legacy = {"calendars": [{"name": "Old", "colour": "violet", "url": SECRET}]}
    public = diary.widget.public_settings(legacy)
    assert public["calendars"][0]["id"] == "cal0"
    merged = diary.widget.merge_settings(legacy, {"calendars": [{"id": "cal0", "name": "Renamed"}]})
    assert merged["calendars"][0]["url"] == SECRET


def test_new_calendars_need_an_address_and_get_an_id():
    with pytest.raises(ValueError, match="needs its secret"):
        diary.widget.merge_settings(OLD, {"calendars": [{"name": "No address"}]})
    added = diary.widget.merge_settings(OLD, {"calendars": [{"id": "c1"}, {"name": "Work", "url": "https://x.example/w.ics"}]})["calendars"]
    assert len(added) == 2 and added[1]["id"] and added[1]["id"] != "c1" and added[0]["url"] == SECRET


def test_removed_calendars_are_dropped_and_others_untouched():
    two = {"calendars": [{"id": "a", "name": "A", "url": "https://x/a.ics"}, {"id": "b", "name": "B", "url": "https://x/b.ics"}]}
    kept = diary.widget.merge_settings(two, {"calendars": [{"id": "b"}]})["calendars"]
    assert [c["id"] for c in kept] == ["b"] and kept[0]["url"] == "https://x/b.ics"


def test_calendar_input_is_validated():
    for bad in ({"id": "c1", "url": "file:///etc/passwd"}, {"id": "c1", "colour": "neon"}):
        with pytest.raises(ValueError):
            diary.widget.merge_settings(OLD, {"calendars": [bad]})
    with pytest.raises(ValueError):
        diary.widget.merge_settings(OLD, {"calendars": [{"name": str(i), "url": "https://x/1.ics"} for i in range(9)]})


def test_request_urls_are_not_logged():
    """httpx logs full URLs at INFO, which would leak the secret iCal address. app.main must keep those loggers quiet."""
    import logging
    import app.main  # noqa: F401  (import applies the logger levels)
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
