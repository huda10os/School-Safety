#!/usr/bin/env python3
"""Build data/records.parquet and data/coverage.parquet from staging files.

Reads one district/platform records file at a time, so memory stays at the
size of the largest single district. Within a file the last line per
record_id wins (retries append). Districts whose listing was discovered but
not yet collected contribute their listing records with text_status "pending".

Coverage flags, per district (the "short history" follow-up list):
    late_start      earliest minutes date after 2018-01-01
    short_span      minutes cover less than 3 years
    long_gap        a gap of more than 6 months between consecutive meetings
    multi_platform  records found on more than one platform
    no_records      no records discovered at all
"""
from __future__ import annotations

import csv
import fcntl
import json
import re
from collections import defaultdict
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

from config import DATA, REFERENCE, STAGING
from platforms import website
from records import FIELDS, LISTING_FIELDS, PREFERENCE, read_jsonl, record_id

ARROW_TYPES = {"string": pa.string(), "date": pa.date32(), "int32": pa.int32(), "float32": pa.float32(),
               "bool": pa.bool_(), "list<string>": pa.list_(pa.string())}
SCHEMA = pa.schema([(name, ARROW_TYPES[kind]) for name, (kind, _) in FIELDS.items()])
LATE_START = date(2018, 1, 1)


def _date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _meeting_key(record: dict) -> tuple:
    return (record["platform"], record.get("meeting_id") or "",
            record.get("date") or "", record.get("meeting_title") or "")


# Meeting video links that appear inside a listing row (BoardBook rows often
# carry a YouTube "virtual meeting location" or a Swagit recording link).
VIDEO_LINK = re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be|vimeo\.com|[\w-]+\.(?:new\.)?swagit\.com)"
                        r"[^\s\"'<>)\]]*", re.I)


def _recording_rows(rows: list[dict]) -> list[dict]:
    """Recordings are kept as links for a later transcription step.

    Direct audio/video files were already listed as documents and skipped at
    download; they become detail Recording. Video links inside a listing row
    become one Recording row per meeting and link.
    """
    extra: dict[str, dict] = {}
    for row in rows:
        if row.get("text_status") in ("unsupported", "recording") and \
                (row.get("error") or "").startswith("media file not"):
            row.update(detail="Recording", text_status="recording")
        for url in VIDEO_LINK.findall(row.get("listing_row_text") or ""):
            url = url.rstrip(".,;")
            rid = record_id(row["tea_id"], row["platform"], f"{url}|{row.get('date_raw') or ''}", "Recording")
            if rid in extra:
                continue
            extra[rid] = {**{k: row.get(k) for k in LISTING_FIELDS}, "record_id": rid,
                          "detail": "Recording", "detail_raw": "Video link in meeting listing",
                          "source_link": url, "text": None, "text_status": "recording",
                          "attachment_links": [], "error": None}
    return list(extra.values())


def _apply_ocr(row: dict) -> None:
    """Use the OCR'd text (code/ocr.py) for scanned documents when it exists."""
    if row.get("text_status") not in ("needs_ocr", "partial_ocr") or not row.get("content_sha256"):
        return
    path = STAGING / "ocr" / row["content_sha256"][:2] / f"{row['content_sha256']}.json"
    if not path.exists():
        return
    ocr = json.loads(path.read_text())
    if not ocr.get("ocr_pages"):
        return
    row.update(text=ocr.get("text"), text_status="ok" if ocr.get("text") else "empty",
               text_method="pdf+ocr", ocr_pages=ocr["ocr_pages"], ocr_mean_conf=ocr.get("mean_conf"))


HEADER_CHARS = 800  # a meeting's own date sits in its first lines (notice, title block)


def _redate(row: dict) -> None:
    """Set a reliable meeting date and record where it came from.

    Platform meeting lists (BoardBook, Diligent, BoardDocs, Legistar) carry
    the date as data. Website rows were dated from link text and file names
    when collected; re-run the current rules on what was stored, so parser
    fixes apply without re-crawling, then prefer the document's own header
    date when it disagrees by more than a week.
    """
    if row.get("platform") != "website":
        row["date_source"] = "listing"
        return
    context = (row.get("listing_row_text") or "").split(" | ", 1)[-1]
    parsed, raw = website.date_for(row.get("detail_raw") or "", row.get("source_link") or "", context)
    row.update(date=parsed, date_raw=raw, date_source="link_or_filename" if parsed else None)
    text = row.get("text")
    if not text or row.get("detail") not in ("Minutes", "Agenda", "Other"):
        return
    header = [f for f in website._explicit_dates(text[:HEADER_CHARS]) if f[0] <= 1]
    if not header:
        return
    _, _, found, raw = min(header)
    if not parsed or abs((date.fromisoformat(found) - date.fromisoformat(parsed)).days) > 7:
        row.update(date=found, date_raw=raw, date_source="document_text")


def district_records(tea_id_platform: str) -> list[dict]:
    collected: dict[str, dict] = {}
    for record in read_jsonl(STAGING / "records" / f"{tea_id_platform}.jsonl"):
        collected[record["record_id"]] = record
    for record in read_jsonl(STAGING / "listings" / f"{tea_id_platform}.jsonl"):
        if record["record_id"] not in collected:
            collected[record["record_id"]] = {**record, "text_status": "pending"}
    rows = list(collected.values())
    for row in rows:
        _apply_ocr(row)
        _redate(row)
    rows += _recording_rows(rows)
    best: dict[tuple, dict] = {}
    for row in rows:
        row["preferred_for_meeting"] = False
        rank = PREFERENCE.get(row.get("detail"))
        if rank is None or row.get("text_status") in ("error", "pending"):
            continue
        key = _meeting_key(row)
        if key not in best or rank < PREFERENCE[best[key]["detail"]]:
            best[key] = row
    for row in best.values():
        row["preferred_for_meeting"] = True
    for row in rows:
        row.setdefault("reviewed", "not_reviewed")
        row["reviewed"] = row.get("reviewed") or "not_reviewed"
        row["date"] = _date(row.get("date"))
        text = row.get("text")
        row["char_count"] = len(text) if text else 0
    return rows


def coverage_row(district: dict, platforms: set, rows: list[dict]) -> dict:
    minutes = sorted({r["date"] for r in rows if r.get("detail") == "Minutes" and r.get("date")})
    meetings = sorted({r["date"] for r in rows if r.get("date")})
    gaps = [(b - a).days for a, b in zip(meetings, meetings[1:])]
    status = defaultdict(int)
    for r in rows:
        status[r.get("text_status") or "pending"] += 1
    span_days = (minutes[-1] - minutes[0]).days if minutes else 0
    flags = []
    if not rows:
        flags.append("no_records")
    if minutes and minutes[0] > LATE_START:
        flags.append("late_start")
    if not minutes or span_days < 3 * 365:
        flags.append("short_span")
    if gaps and max(gaps) > 183:
        flags.append("long_gap")
    if len(platforms) > 1:
        flags.append("multi_platform")
    return dict(tea_id=district["tea_id"], school_district=district["school_district"],
                county_name=district["county_name"], platforms=",".join(sorted(platforms)),
                records=len(rows), meetings=len(meetings),
                first_meeting_date=meetings[0] if meetings else None,
                last_meeting_date=meetings[-1] if meetings else None,
                first_minutes_date=minutes[0] if minutes else None,
                last_minutes_date=minutes[-1] if minutes else None,
                minutes_count=sum(r.get("detail") == "Minutes" for r in rows),
                agenda_count=sum(r.get("detail") == "Agenda" for r in rows),
                longest_gap_days=max(gaps) if gaps else None,
                text_status_counts=json.dumps(dict(status), sort_keys=True),
                flags=",".join(flags))


def build() -> dict:
    """Build both Parquet files; concurrent callers (each collection service
    ends with a build, plus the hourly timer) wait for one another."""
    DATA.mkdir(parents=True, exist_ok=True)
    with open(DATA / ".build.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _build()


def _build() -> dict:
    with open(REFERENCE / "districts.csv", newline="") as handle:
        districts = list(csv.DictReader(handle))
    stems = defaultdict(set)
    for path in (STAGING / "listings").glob("*.jsonl"):
        tea_id, platform = path.stem.split("__", 1)
        stems[tea_id].add(platform)
    target = DATA / "records.parquet"
    tmp = target.with_suffix(".parquet.tmp")
    writer = pq.ParquetWriter(tmp, SCHEMA, compression="zstd")
    coverage, total = [], 0
    try:
        for district in districts:
            rows = []
            for platform in sorted(stems.get(district["tea_id"], ())):
                platform_rows = district_records(f"{district['tea_id']}__{platform}")
                for row in platform_rows:
                    # The registry is authoritative; staging rows keep the
                    # name and county they were discovered with.
                    row["school_district"] = district["school_district"]
                    row["county_name"] = district["county_name"]
                if platform_rows:
                    table = pa.Table.from_pylist(
                        [{name: r.get(name) for name in SCHEMA.names} for r in platform_rows], schema=SCHEMA)
                    writer.write_table(table, row_group_size=5000)
                    total += len(platform_rows)
                # coverage only needs metadata; drop text before holding rows longer
                rows += [{k: r.get(k) for k in ("detail", "date", "text_status")} for r in platform_rows]
            coverage.append(coverage_row(district, stems.get(district["tea_id"], set()), rows))
    finally:
        writer.close()
    tmp.replace(target)
    pq.write_table(pa.Table.from_pylist(coverage), DATA / "coverage.parquet", compression="zstd")
    flagged = sum(bool(c["flags"]) for c in coverage)
    return dict(records=total, districts=len(coverage), flagged_districts=flagged)


if __name__ == "__main__":
    print(json.dumps(build()))
