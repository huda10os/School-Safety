"""Granicus Legistar public event documents."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from extract import clean_text
from records import make_record

PLATFORM = "legistar"
PAGE_SIZE = 100


def discover(district: dict, entry_url: str, fetcher) -> list[dict]:
    host = urlsplit(entry_url).hostname or ""
    if not host.lower().endswith(".legistar.com"):
        raise ValueError("Legistar entry URL must use a legistar.com host")
    client = host.split(".")[0]
    api = f"https://webapi.legistar.com/v1/{client}/events"
    records = []
    skip = 0
    while True:
        url = f"{api}?$top={PAGE_SIZE}&$skip={skip}&$orderby=EventId%20asc"
        events = fetcher.get(url, refresh=True).json()
        for event in events:
            raw_date = event.get("EventDate")
            try:
                parsed = date.fromisoformat(raw_date[:10]).isoformat()
            except (TypeError, ValueError):
                parsed = None
            for key, detail in (("EventAgendaFile", "Agenda"), ("EventMinutesFile", "Minutes")):
                if not event.get(key):
                    continue
                records.append(make_record(district, PLATFORM, date=parsed, date_raw=raw_date,
                    meeting_type=event.get("EventBodyName"), meeting_title=event.get("EventBodyName"),
                    meeting_id=str(event["EventId"]), detail=detail, detail_raw=detail,
                    source_link=event[key], listing_url=url,
                    listing_row_text=json.dumps(event, ensure_ascii=False, separators=(",", ":"))))
        if len(events) < PAGE_SIZE:
            break
        skip += len(events)
    return records


def resolve(record: dict, fetcher) -> dict:
    response = fetcher.get(record["source_link"])
    content_type = response.headers.get("Content-Type", response.headers.get("content-type", ""))
    result = dict(document_link=response.url, content_type=content_type, raw_path=response.raw_path,
                  content_sha256=response.sha256)
    if "html" in content_type.lower() and not response.content.startswith(b"%PDF"):
        soup = BeautifulSoup(response.text, "html.parser")
        for node in soup(["script", "style", "noscript"]):
            node.decompose()
        result.update(text=clean_text(soup.get_text("\n")), text_method="html")
    return result
