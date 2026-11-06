#!/usr/bin/env python3
"""Build meetings -> rules -> optional jev -> apply, with bounded resources.

Example: .venv/bin/python code/measures/run.py --skip-jev
Then: PYTHONPATH=code .venv/bin/python -m measures.jev --max-usd 0.25 --meeting-ids data/measures/gold/slice60.parquet
Threshold-only replay: PYTHONPATH=code .venv/bin/python -c 'from measures.apply import apply; apply()'
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import time
from collections import Counter
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'measures'

from .common import DEFAULT_DEFINITIONS, DEFAULT_OUTPUT, ROOT, MEETING_SCHEMA, PASSAGE_SCHEMA, definitions, writer
from .meetings import district_records, group_meetings, registry, summarize
from .rules import Rules


def build(records, output, defs, limit=None):
    started = time.monotonic()
    districts, rules = registry(), Rules(defs)
    counts, subtopics, events = Counter(), Counter(), Counter()
    with writer(output / 'meetings-base.parquet', MEETING_SCHEMA) as write_meetings, \
            writer(output / 'passages.parquet', PASSAGE_SCHEMA) as write_passages:
        for tea, documents in district_records(records):
            metadata = districts.get(tea, documents[0])
            meetings, passages = [], []
            for group in group_meetings(documents):
                if limit is not None and counts['meetings'] >= limit:
                    break
                meeting, best = summarize(group, metadata, defs['version'])
                extracted = rules.measure(meeting, group, best)
                meetings.append(meeting)
                passages.extend(extracted)
                counts['meetings'] += 1
                counts['readable'] += meeting['readable']
                counts['with_passages'] += bool(extracted)
                counts['passages'] += len(extracted)
                counts['closed_security_session'] += meeting['closed_security_session']
                subtopics.update({k for p in extracted for k in p['subtopic_hits']})
                events.update(meeting['named_events'])
                if len(passages) >= 1024:
                    write_passages(passages)
                    passages.clear()
            write_meetings(meetings)
            write_passages(passages)
            if counts['meetings'] and counts['meetings'] % 10000 < len(meetings):
                print(f"rules: {counts['meetings']:,} meetings", flush=True)
            if limit is not None and counts['meetings'] >= limit:
                break
    return dict(counts, subtopic_candidates=dict(subtopics), named_events=dict(events),
                rules_seconds=round(time.monotonic() - started, 3),
                peak_rss_mib=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
                source_records=str(records), source_bytes=records.stat().st_size,
                definition_version=defs['version'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-jev', action='store_true')
    parser.add_argument('--max-usd', type=float, default=0)
    parser.add_argument('--limit-meetings', type=int)
    parser.add_argument('--definitions', type=Path, default=DEFAULT_DEFINITIONS)
    parser.add_argument('--records', type=Path, default=ROOT / 'data/records.parquet')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--model', default='jev-latest')
    args = parser.parse_args()
    if args.limit_meetings is not None and args.limit_meetings < 1:
        parser.error('--limit-meetings must be positive')
    # Arrow pools are process-local; no changes to the concurrent OCR job.
    import pyarrow as pa
    pa.set_cpu_count(2)
    pa.set_io_thread_count(2)
    defs = definitions(args.definitions)
    started = time.monotonic()
    summary = build(args.records, args.output, defs, args.limit_meetings)
    from .apply import apply
    if not args.skip_jev:
        from .jev import label
        summary['jev'] = label(args.output, defs, args.max_usd, concurrency=args.concurrency, model=args.model)
    summary.update(apply(args.output, defs, args.model))
    summary['cost_usd'] = summary.get('jev', {}).get('cost_usd', 0)
    summary['runtime_seconds'] = round(time.monotonic() - started, 3)
    summary['peak_rss_mib'] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == '__main__':
    main()
