"""Bin collections, from the council's website.

Provider: Salford City Council. The parsing follows the approach used by the open-source
UKBinCollectionData project (robbrad/UKBinCollectionData, `SalfordCityCouncil`): request the council's
"your bin collections" page for a property (identified by its UPRN) and read the dated lists. It is a few
lines of httpx + BeautifulSoup, so we keep it in-tree rather than pulling in that project's whole
dependency tree. If the council changes its page, the widget reports the parse error and keeps
showing the last good schedule.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .base import Widget

URL = "https://www.salford.gov.uk/bins-and-recycling/bin-collection-days/your-bin-collections/?UPRN={uprn}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")

# How each council stream is presented. Keyed by our stream id; the browser reads this rather than hard-coding it.
STREAMS = {
    "domestic": {"title": "Domestic", "sub": "General waste", "colour": "#6b7280", "group": "domestic"},
    "blue":     {"title": "Recycling · Blue", "sub": "Paper & card", "colour": "#5b7fd6", "group": "recycling"},
    "brown":    {"title": "Recycling · Brown", "sub": "Bottles & cans", "colour": "#8a6a3f", "group": "recycling"},
    "food":     {"title": "Food & garden", "sub": "Food and garden waste", "colour": "#5f9a6b", "group": "food"},
}


def classify(label: str) -> str | None:
    text = label.lower()
    for keyword, stream in (("domestic", "domestic"), ("blue", "blue"), ("brown", "brown"), ("food", "food"), ("garden", "food")):
        if keyword in text:
            return stream
    return None


def parse_salford(html: str) -> list[dict[str, str]]:
    """[{date: 'YYYY-MM-DD', stream, label}] sorted by date. Raises if the page has no recognisable schedule."""
    soup = BeautifulSoup(html, "lxml")  # html.parser chokes on the council's malformed markup
    block = soup.find("div", class_="wastefurther")
    if block is None:
        raise ValueError("bin schedule not found on the council page (has its layout changed?)")
    items: list[dict[str, str]] = []
    for col in block.find_all("div", class_=lambda c: c and "col-12" in c):
        strong = col.find("p").find("strong") if col.find("p") else None
        ul = col.find("ul")
        if not strong or not ul:
            continue
        label = strong.get_text(strip=True).rstrip(":")
        stream = classify(label)
        if stream is None:
            continue
        for li in ul.find_all("li"):
            m = DATE_RE.search(li.get_text(" ", strip=True))
            if m and (month := MONTHS.get(m.group(2).lower())):
                items.append({"date": date(int(m.group(3)), month, int(m.group(1))).isoformat(), "stream": stream, "label": label})
    if not items:
        raise ValueError("no collection dates parsed from the council page")
    return sorted(items, key=lambda i: (i["date"], i["stream"]))


class BinsWidget(Widget):
    id = "bins"
    title = "Bins"
    interval = 24 * 3600
    daily_at = "03:00"           # the council page changes about weekly
    min_interval = 3 * 3600
    defaults = {"provider": "salford", "uprn": "", "put_out_hour": 17, "alert_streams": ["domestic", "blue", "brown"]}

    def configured(self, settings: dict[str, Any]) -> bool:
        return bool(str(settings.get("uprn", "")).strip())

    def merge_settings(self, old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        merged = super().merge_settings(old, new)
        if merged.get("uprn") and not re.fullmatch(r"\d{5,14}", str(merged["uprn"]).strip()):
            raise ValueError("uprn must be 5–14 digits")
        if "put_out_hour" in new:
            hour = new["put_out_hour"]
            if not isinstance(hour, int) or isinstance(hour, bool) or not 0 <= hour <= 23:
                raise ValueError("put_out_hour must be a whole number from 0 to 23")
        if "alert_streams" in new:
            streams = new["alert_streams"]
            if not isinstance(streams, list) or not set(streams) <= set(STREAMS):
                raise ValueError(f"alert_streams must be a list drawn from: {', '.join(STREAMS)}")
        return merged

    async def fetch(self, settings: dict[str, Any]) -> dict[str, Any]:
        uprn = re.sub(r"\D", "", str(settings["uprn"]))
        async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True) as client:
            resp = await client.get(URL.format(uprn=uprn))
            resp.raise_for_status()
        return {"collections": parse_salford(resp.text)}

    def view(self, data: dict[str, Any] | None, settings: dict[str, Any], now: datetime) -> dict[str, Any]:
        if not data:
            return {}
        today = now.date()
        future = [c for c in data["collections"] if c["date"] >= today.isoformat()]

        def nxt(streams: tuple[str, ...]) -> dict[str, Any] | None:
            for c in future:
                if c["stream"] in streams:
                    d = date.fromisoformat(c["date"])
                    return {"date": c["date"], "in_days": (d - today).days, "stream": c["stream"]}
            return None

        by_date: dict[str, list[str]] = {}
        for c in future:
            if date.fromisoformat(c["date"]) <= today + timedelta(days=100):
                by_date.setdefault(c["date"], []).append(c["stream"])
        return {
            "streams": STREAMS,
            # The two things the Now card shows: next domestic, and next recycling (blue and brown alternate).
            "next": {"domestic": nxt(("domestic",)), "recycling": nxt(("blue", "brown"))},
            "upcoming": [
                {"date": d, "in_days": (date.fromisoformat(d) - today).days, "streams": sorted(s)}
                for d, s in sorted(by_date.items())
            ],
        }


widget = BinsWidget()
