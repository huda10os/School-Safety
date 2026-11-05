"""Record schema and staging-file helpers shared by all platforms.

A record is one document link on a district's meeting listing: one row per
Agenda, Minutes, Projector, Extras (or website document) link.

Platform modules (code/platforms/<name>.py) implement:

    PLATFORM: str
    discover(district: dict, entry_url: str, fetcher) -> list[dict]
        Read the district's meeting listing and return one listing record per
        document link, with every LISTING_FIELDS key that the source supports
        (unknown values None). Use make_record() to fill ids and defaults.
    resolve(record: dict, fetcher) -> dict
        Download the document behind record["source_link"] and return
        DOCUMENT_FIELDS values. For HTML documents the platform returns the
        cleaned "text" itself (text_method "html"). For PDF/DOCX it returns the
        cached file's raw_path and leaves "text" unset; extract.py parses it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DETAILS = ("Agenda", "Minutes", "Projector", "Extras", "Recording", "Other")
# Order used to choose one preferred document per meeting.
PREFERENCE = {"Minutes": 0, "Agenda": 1, "Projector": 2}

# Field spec: name -> (type, description). Doubles as the spec for later review.
FIELDS = {
    "record_id": ("string", "SHA-256 of tea_id|platform|source_link|detail; stable across reruns."),
    "tea_id": ("string", "TEA 6-digit district id."),
    "school_district": ("string", "District name as listed by TEA."),
    "county_name": ("string", "County of the district's TEA county number."),
    "platform": ("string", "boardbook | diligent | boarddocs | legistar | website"),
    "date": ("date", "Meeting date parsed from date_raw; null if the source gives none."),
    "date_raw": ("string", "Date text exactly as shown on the listing or document."),
    "meeting_type": ("string", "Source's meeting type, e.g. Regular, Special; null if not given."),
    "meeting_title": ("string", "Meeting title as shown on the listing."),
    "meeting_id": ("string", "Platform's meeting identifier when it has one."),
    "detail": ("string", "One of Agenda, Minutes, Projector, Extras, Recording, Other."),
    "detail_raw": ("string", "Link label exactly as shown on the source."),
    "source_link": ("string", "Link to the record as published on the listing."),
    "listing_url": ("string", "Page or API URL the record was discovered on."),
    "listing_row_text": ("string", "Exact text of the listing row the metadata came from."),
    "document_link": ("string", "URL of the bytes actually parsed (PDF behind a viewer, etc.)."),
    "content_type": ("string", "Content-Type of the parsed document."),
    "raw_path": ("string", "Repo-relative path of the cached gzip body that was parsed."),
    "content_sha256": ("string", "SHA-256 of the parsed document bytes."),
    "text": ("string", "Extracted text; null when text_status is not ok."),
    "text_status": ("string", "ok | needs_ocr | partial_ocr | empty | too_large | unsupported | unavailable (source no longer publishes it) | recording (audio/video link kept, not downloaded or transcribed) | error | pending"),
    "text_method": ("string", "html | pdf | docx | plain"),
    "page_count": ("int32", "Pages in a PDF document."),
    "char_count": ("int32", "Characters in text."),
    "attachment_links": ("list<string>", "Attachment URLs linked from an agenda (text not collected)."),
    "error": ("string", "Error message when text_status is error."),
    "preferred_for_meeting": ("bool", "True on the single best document per meeting (Minutes > Agenda > Projector)."),
    "reviewed": ("string", "not_reviewed | pass | flagged"),
    "collected_at": ("string", "UTC timestamp of document collection."),
}
LISTING_FIELDS = ["record_id", "tea_id", "school_district", "county_name", "platform", "date",
                  "date_raw", "meeting_type", "meeting_title", "meeting_id", "detail",
                  "detail_raw", "source_link", "listing_url", "listing_row_text"]
DOCUMENT_FIELDS = ["document_link", "content_type", "raw_path", "content_sha256", "text",
                   "text_method", "attachment_links", "page_count", "text_status", "error"]


def record_id(tea_id: str, platform: str, source_link: str, detail: str) -> str:
    return hashlib.sha256(f"{tea_id}|{platform}|{source_link}|{detail}".encode()).hexdigest()


def make_record(district: dict, platform: str, **values) -> dict:
    """Return a listing record with ids filled and all LISTING_FIELDS present."""
    record = {name: None for name in LISTING_FIELDS}
    record.update(tea_id=district["tea_id"], school_district=district["school_district"],
                  county_name=district["county_name"], platform=platform)
    record.update(values)
    if record["detail"] not in DETAILS:
        raise ValueError(f"detail must be one of {DETAILS}, got {record['detail']!r}")
    record["record_id"] = record_id(record["tea_id"], platform, record["source_link"], record["detail"])
    return record


def write_jsonl(path: Path, rows) -> None:
    """Atomically replace path with rows, one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    tmp.replace(path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def read_jsonl(path: Path):
    if not path.exists():
        return
    with open(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # a partially written final line from an interrupted run
