"""Group district documents without loading statewide text.

Same-platform explicit IDs anchor compatible titles. Cross-platform groups merge only
when their meeting kind/title and published time are compatible and the match
is unambiguous. Undated records never merge on a missing date alone.
Without a native meeting ID, a record anchor disambiguates repeated generic
titles. IDs are deterministic for an unchanged source set; adding previously
missing documents can change such ambiguous anchors and require relabelling.
"""
from __future__ import annotations

import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pyarrow.dataset as ds
import pyarrow.parquet as pq

from .common import ROOT, digest

PREFERENCE = {'Minutes': 0, 'Agenda': 1, 'Projector': 2, 'Other': 3, 'Extras': 4}
COLUMNS = ['record_id', 'tea_id', 'school_district', 'county_name', 'platform', 'date',
           'date_source', 'meeting_id', 'meeting_type', 'meeting_title', 'detail',
           'text', 'text_status', 'preferred_for_meeting', 'content_sha256']


def readable(row):
    return row.get('text_status') in ('ok', 'partial_ocr') and bool((row.get('text') or '').strip())


def title_key(row):
    title = (row.get('meeting_title') or '').lower()
    # BoardBook prefixes dates and times before the actual meeting title.
    title = re.sub(r'^.*?\b(?:am|pm)\s*[-–:]\s*', '', title)
    title = re.sub(r'\b(?:agenda|minutes|projector|notice|board|of|trustees|meeting)\b', ' ', title)
    title = re.sub(r'\b\d{1,4}\b', ' ', title)
    title = re.sub(r'\b(?:january|february|march|april|may|june|july|august|september|october|november|december|'
                   r'jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|monday|tuesday|wednesday|thursday|friday|'
                   r'saturday|sunday|mon|tue|wed|thu|fri|sat|sun|at|am|pm)\b', ' ', title)
    return re.sub(r'[^a-z]+', ' ', title).strip()


def kind(row):
    title = (row.get('meeting_title') or '').lower()
    for name, pattern in [('hearing', r'hearing'), ('workshop', r'workshop|work session'),
                          ('special', r'special|called'), ('regular', r'regular')]:
        if re.search(pattern, title):
            return name
    value = (row.get('meeting_type') or '').lower()
    return value if value in ('regular', 'special', 'workshop', 'hearing') else ''


def time_key(row):
    match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?',
                      row.get('meeting_title') or '', re.I)
    if match:
        return (int(match[1]) % 12 + (12 if match[3].lower() == 'p' else 0), int(match[2] or 0))
    return None


def compatible(left, right):
    if kind(left) and kind(right) and kind(left) != kind(right):
        return False
    if time_key(left) and time_key(right) and time_key(left) != time_key(right):
        return False
    a, b = title_key(left), title_key(right)
    return not a or not b or a == b or (kind(left) and kind(left) == kind(right)
                                      and a in ('regular', 'special') and b in ('regular', 'special'))


def group_meetings(records):
    days = defaultdict(list)
    for row in records:
        days[row.get('date')].append(row)
    result = []
    for day, documents in sorted(days.items(), key=lambda pair: str(pair[0])):
        explicit = defaultdict(list)
        loose = []
        for row in sorted(documents, key=lambda r: r['record_id']):
            if row.get('meeting_id'):
                explicit[(row['platform'], row['meeting_id'])].append(row)
            else:
                loose.append(row)
        groups = []
        for same_id in explicit.values():
            partitions = []
            for row in same_id:
                match = next((g for g in partitions if compatible(row, g[0])), None)
                if match is None:
                    partitions.append([row])
                else:
                    match.append(row)
            groups.extend(partitions)
        # Only merge unique, reciprocal cross-platform matches.
        for group in list(groups):
            if group not in groups:
                continue
            matches = [g for g in groups if g is not group
                       and not ({r['platform'] for r in g} & {r['platform'] for r in group})
                       and day is not None and compatible(group[0], g[0])]
            if len(matches) == 1:
                other = matches[0]
                reverse = [g for g in groups if g is not other
                           and not ({r['platform'] for r in g} & {r['platform'] for r in other})
                           and compatible(other[0], g[0])]
                if len(reverse) == 1:
                    group.extend(other)
                    groups.remove(other)
        for row in loose:
            matches = [g for g in groups if day is not None and compatible(row, g[0])]
            if len(matches) == 1:
                matches[0].append(row)
            else:
                groups.append([row])
        result.extend(groups)
    return result


def summarize(documents, district, version):
    docs = sorted(documents, key=lambda r: (r.get('platform') or '', r.get('meeting_id') or '', r['record_id']))
    first = docs[0]
    explicit = [(r['platform'], str(r['meeting_id'])) for r in docs if r.get('meeting_id')]
    anchor = ([min(explicit), kind(first), title_key(first), time_key(first)] if explicit
              else [title_key(first), first['record_id']])
    day = first.get('date')
    if isinstance(day, str):
        day = date.fromisoformat(day[:10])
    mid = digest([first['tea_id'], str(day), anchor])
    usable = [r for r in docs if readable(r)]
    best = min(usable, key=lambda r: (PREFERENCE.get(r.get('detail'), 5),
                                     not bool(r.get('preferred_for_meeting')), -len(r['text']), r['record_id'])) if usable else None
    row = {k: district.get(k) or None for k in ('tea_id', 'school_district', 'county_name',
                                               'county_fips', 'nces_id', 'esc_region')}
    for key in ('district_lat', 'district_lon'):
        row[key] = float(district[key]) if district.get(key) else None
    row.update(meeting_id=mid, date=day, date_source=first.get('date_source'),
               meeting_type=first.get('meeting_type'), meeting_title=first.get('meeting_title'),
               platforms=sorted({r['platform'] for r in docs}), record_ids=sorted(r['record_id'] for r in docs),
               best_record_id=best['record_id'] if best else None, best_detail=best.get('detail') if best else None,
               has_minutes_text=any(r.get('detail') == 'Minutes' for r in usable),
               has_agenda_text=any(r.get('detail') == 'Agenda' for r in usable), readable=bool(best),
               text_chars=len(best['text']) if best else 0, n_passages=0, closed_security_session=False,
               named_events=[], safety='not_screened', temperament='not_screened', event_reference='not_screened',
               safety_subtopics=[], event_keys=[], evidence=[], definition_version=version, label_version=None)
    return row, best


def district_records(path=ROOT / 'data/records.parquet'):
    """Use per-row-group TEA statistics when present, and a filtered scan otherwise."""
    parquet = pq.ParquetFile(path)
    column = parquet.schema.names.index('tea_id')
    groups = defaultdict(list)
    for i in range(parquet.num_row_groups):
        stats = parquet.metadata.row_group(i).column(column).statistics
        if not stats or not stats.has_min_max or stats.min != stats.max:
            dataset = ds.dataset(path, format='parquet')
            ids = sorted(set(dataset.to_table(columns=['tea_id']).column(0).to_pylist()))
            for tea in ids:
                yield tea, dataset.to_table(columns=COLUMNS, filter=ds.field('tea_id') == tea,
                                             use_threads=False).to_pylist()
            return
        groups[stats.min].append(i)
    for tea, indices in sorted(groups.items()):
        yield tea, parquet.read_row_groups(indices, columns=COLUMNS, use_threads=False).to_pylist()


def registry(path=ROOT / 'data/reference/districts.csv'):
    with open(path, newline='') as handle:
        return {r['tea_id']: r for r in csv.DictReader(handle)}
