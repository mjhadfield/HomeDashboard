"""Bins: parsing of the council page (synthetic fixture mirroring its structure) and the 'next' logic."""
from datetime import datetime

import pytest

from app import config
from app.widgets import bins

PAGE = """<html><body><div class="wastefurther"><div class="row">
<div class="col-12 col-md-6"><p><strong>Domestic waste:</strong></p><ul>
  <li>Thursday 01 October 2026</li><li>Thursday 22 October 2026</li></ul></div>
<div class="col-12 col-md-6"><p><strong>Blue recycling (paper and card):</strong></p><ul>
  <li>Thursday 01 October 2026</li><li>Thursday 15 October 2026</li></ul></div>
<div class="col-12 col-md-6"><p><strong>Food and garden waste:</strong></p><ul>
  <li>Thursday 24 September 2026</li><li>Thursday 01 October 2026</li></ul></div>
<div class="col-12 col-md-6"><p><strong>Brown recycling (bottles and cans):</strong></p><ul>
  <li>Thursday 24 September 2026</li><li>Thursday 08 October 2026</li></ul></div>
<div class="col-12"><p><strong>Something unrelated:</strong></p><ul><li>Friday 02 October 2026</li></ul></div>
</div></div></body></html>"""


def at(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=config.TZ)


def view(now):
    return bins.widget.view({"collections": bins.parse_salford(PAGE)}, {}, now)


def test_parse_classifies_streams_and_ignores_unknown():
    items = bins.parse_salford(PAGE)
    assert {i["stream"] for i in items} == {"domestic", "blue", "brown", "food"}
    assert items == sorted(items, key=lambda i: (i["date"], i["stream"]))
    assert {"date": "2026-09-24", "stream": "brown", "label": "Brown recycling (bottles and cans)"} in items


def test_next_domestic_and_recycling_from_a_monday():
    v = view(at(2026, 9, 21))
    # Recycling alternates blue/brown: the brown collection on Thu 24 comes first, not the blue one.
    assert v["next"]["recycling"] == {"date": "2026-09-24", "in_days": 3, "stream": "brown"}
    assert v["next"]["domestic"] == {"date": "2026-10-01", "in_days": 10, "stream": "domestic"}


def test_next_moves_on_after_collection_day():
    v = view(at(2026, 9, 25))           # the day after brown was collected
    assert v["next"]["recycling"]["stream"] == "blue" and v["next"]["recycling"]["date"] == "2026-10-01"


def test_collection_day_itself_counts_as_today():
    v = view(at(2026, 10, 1, 7))
    assert v["next"]["domestic"]["in_days"] == 0 and v["next"]["recycling"]["in_days"] == 0


def test_upcoming_groups_streams_per_date_and_drops_the_past():
    v = view(at(2026, 9, 25))
    assert all(u["date"] >= "2026-09-25" for u in v["upcoming"])
    first = v["upcoming"][0]
    assert first["date"] == "2026-10-01" and first["streams"] == ["blue", "domestic", "food"]


def test_page_without_schedule_is_an_error_not_an_empty_list():
    with pytest.raises(ValueError):
        bins.parse_salford("<html><body>maintenance</body></html>")
    with pytest.raises(ValueError):
        bins.parse_salford('<div class="wastefurther"></div>')


def test_uprn_validation():
    with pytest.raises(ValueError):
        bins.widget.merge_settings({}, {"uprn": "abc"})
    with pytest.raises(ValueError):
        bins.widget.merge_settings({}, {"nonsense": 1})
    assert bins.widget.merge_settings({}, {"uprn": "123456789012"})["uprn"] == "123456789012"
