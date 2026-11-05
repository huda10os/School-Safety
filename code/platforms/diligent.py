"""Diligent Community meeting listings and public documents."""
from __future__ import annotations

import json
from datetime import date
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from extract import clean_text
from records import make_record

PLATFORM = "diligent"

# Open document types published by the portal. Closed types are never public.
_TYPES = {1: "Agenda", 2: "Agenda", 4: "Extras", 9: "Minutes", 10: "Extras",
          49: "Minutes", 53: "Minutes", 55: "Minutes"}
_CLOSED_TYPES = {5, 6, 8, 11, 12, 51, 54, 56}


def discover(district: dict, entry_url: str, fetcher) -> list[dict]:
    parts = urlsplit(entry_url)
    base = f"{parts.scheme}://{parts.netloc}/"
    listing = urljoin(base, "Services/MeetingsService.svc/meetings?from=1990-01-01&to=2027-12-31&loadall=true")
    meetings = fetcher.get(listing, refresh=True).json()
    records = []
    for meeting in meetings:
        if not meeting.get("Published") or not meeting.get("Id"):
            continue
        url = urljoin(base, f"Services/MeetingsService.svc/meetings/{meeting['Id']}/meetingDocuments")
        documents = fetcher.get(url).json().get("Documents", [])
        raw_date = meeting.get("MeetingDate")
        try:
            parsed = date.fromisoformat(raw_date[:10]).isoformat()
        except (TypeError, ValueError):
            parsed = None
        if parsed == "2001-01-01" and "test" in (meeting.get("Name") or "").lower():
            parsed = None
        for doc in documents:
            kind = doc.get("DocumentType")
            if doc.get("IsPublic") is not True or kind in _CLOSED_TYPES or not doc.get("Id"):
                continue
            detail = _TYPES.get(kind, "Other")
            label = {1: "Agenda", 2: "Agenda split screen", 4: "Agenda packet", 9: "Minutes",
                     10: "Minutes packet", 49: "Minutes split screen", 53: "Adopted minutes PDF",
                     55: "Adopted minutes HTML"}.get(kind, doc.get("Name") or "Other")
            link = urljoin(base, f"document/{doc['Id']}")
            records.append(make_record(district, PLATFORM, date=parsed, date_raw=raw_date,
                meeting_type=(meeting.get("MeetingTypeName") or "").strip() or None,
                meeting_title=meeting.get("Name"), meeting_id=str(meeting["Id"]), detail=detail,
                detail_raw=label, source_link=link, listing_url=url,
                listing_row_text=json.dumps(meeting, ensure_ascii=False, separators=(",", ":"))))
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
