"""Draw reproducible stratified coding samples and privacy-filtered packets.

Strata are year band, primary platform, and candidate presence. Each candidate
class cycles evenly over its shuffled strata (not population-proportional).
Sampling probabilities are therefore unequal; use a held-out representative
sample before treating calibration estimates as statewide accuracy.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'measures'

from .common import DEFAULT_OUTPUT, ROOT, MEETING_SCHEMA, redact, rows, writer
from .meetings import district_records


def stratum(meeting):
    year = meeting['date'].year if meeting['date'] else None
    band = 'unknown' if year is None else 'pre2018' if year < 2018 else '2018-2021' if year < 2022 else '2022+'
    return band, '+'.join(meeting['platforms']), bool(meeting['n_passages'])


def sample(meetings, size=500, candidate_fraction=0.7, seed=7321, readable_only=False):
    if not 0 <= candidate_fraction <= 1 or size < 1:
        raise ValueError('Invalid sample size or candidate fraction')
    rng = random.Random(seed)
    strata = defaultdict(list)
    for meeting in meetings:
        if readable_only and not meeting['readable']:
            continue
        strata[stratum(meeting)].append(meeting)
    for key in sorted(strata):
        rng.shuffle(strata[key])
    chosen = []
    for candidate, quota in [(True, round(size * candidate_fraction)),
                              (False, size - round(size * candidate_fraction))]:
        keys = [key for key in sorted(strata) if key[2] == candidate]
        rng.shuffle(keys)
        count = 0
        while count < quota and any(strata[k] for k in keys):
            for key in keys:
                if strata[key] and count < quota:
                    chosen.append(strata[key].pop())
                    count += 1
    leftovers = [row for values in strata.values() for row in values]
    rng.shuffle(leftovers)
    chosen.extend(leftovers[:max(0, size - len(chosen))])
    return sorted(chosen, key=lambda r: (r['tea_id'], str(r['date']), r['meeting_id']))


def generate(output=DEFAULT_OUTPUT, records=ROOT / 'data/records.parquet', size=500,
             candidate_fraction=0.7, seed=7321, name='sample', readable_only=False):
    output, records = Path(output), Path(records)
    gold = output / 'gold'
    gold.mkdir(parents=True, exist_ok=True)
    selected = sample(rows(output / 'meetings.parquet'), size, candidate_fraction, seed, readable_only)
    ids = {m['meeting_id'] for m in selected}
    passages = defaultdict(list)
    for row in rows(output / 'passages.parquet'):
        if row['meeting_id'] in ids:
            passages[row['meeting_id']].append({**row, 'text': redact(row['text'])})
    by_district = defaultdict(list)
    for m in selected:
        by_district[m['tea_id']].append(m)
    excerpts = {}
    for tea, documents in district_records(records):
        if tea not in by_district:
            continue
        wanted = {m['best_record_id'] for m in by_district[tea]}
        excerpts.update({r['record_id']: redact(r.get('text') or '', 3000)
                         for r in documents if r['record_id'] in wanted})
    with writer(gold / f'{name}.parquet', MEETING_SCHEMA) as write:
        write(selected)
    packet_name = 'packet.jsonl' if name == 'sample' else name + '-packet.jsonl'
    with open(gold / packet_name, 'w') as handle:
        for m in selected:
            item = dict(meeting_id=m['meeting_id'], district=m['school_district'], tea_id=m['tea_id'],
                        definition_version=m['definition_version'],
                        date=m['date'], stratum=stratum(m), readable=m['readable'], best_detail=m['best_detail'],
                        measured_text_excerpt=excerpts.get(m['best_record_id'], ''), passages=passages[m['meeting_id']])
            handle.write(json.dumps(item, default=str, ensure_ascii=False) + '\n')
    if name == 'sample':
        with open(gold / 'labels-template.csv', 'w', newline='') as handle:
            fields = ['meeting_id', 'safety', 'subtopics', 'temperament', 'tone', 'who', 'event_reference', 'coder', 'notes']
            target = csv.DictWriter(handle, fields)
            target.writeheader()
            target.writerows({'meeting_id': m['meeting_id']} for m in selected)
    summary = dict(size=len(selected), with_passages=sum(bool(m['n_passages']) for m in selected),
                   readable=sum(m['readable'] for m in selected), seed=seed,
                   strata={str(k): sum(stratum(m) == k for m in selected) for k in sorted({stratum(m) for m in selected})})
    (gold / f'{name}-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary), flush=True)
    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--records', type=Path, default=ROOT / 'data/records.parquet')
    parser.add_argument('--size', type=int, default=500)
    parser.add_argument('--candidate-fraction', type=float, default=0.7)
    parser.add_argument('--seed', type=int, default=7321)
    parser.add_argument('--name', default='sample')
    parser.add_argument('--readable-only', action='store_true')
    args = parser.parse_args()
    generate(args.output, args.records, args.size, args.candidate_fraction, args.seed, args.name, args.readable_only)


if __name__ == '__main__':
    main()
