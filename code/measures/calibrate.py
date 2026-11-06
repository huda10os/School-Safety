"""Calibrate mention thresholds against independently coded gold labels.

The no-cutoff target is screening recall: at least 95% of gold positives
must remain above the automatic-no cutoff (yes OR uncertain). It is not
negative-class recall. Point estimates have no held-out accuracy guarantee.
Unscreened meetings are excluded and reported. Rule-only negatives enter at
probability zero so gold-positive rule misses cannot disappear from recall.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'measures'

from .common import DEFAULT_OUTPUT, rows


def metrics(pairs, threshold):
    tp = sum(y and p >= threshold for p, y in pairs)
    fp = sum(not y and p >= threshold for p, y in pairs)
    fn = sum(y and p < threshold for p, y in pairs)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    return dict(threshold=threshold, precision=precision, recall=recall, f1=f1, tp=tp, fp=fp, fn=fn)


def calibrate_pairs(pairs, yes_precision=0.90, screen_recall=0.95):
    if not pairs:
        return dict(n=0, status='no scored gold rows', curve=[])
    if not 0 < yes_precision <= 1 or not 0 < screen_recall <= 1:
        raise ValueError('Targets must be in (0, 1]')
    thresholds = sorted({i / 100 for i in range(101)} | {p for p, _ in pairs})
    curve = [metrics(pairs, t) for t in thresholds]
    valid_yes = [m for m in curve if m['precision'] is not None and m['precision'] >= yes_precision]
    yes = min(valid_yes, key=lambda m: m['threshold']) if valid_yes else None
    positives = sum(y for _, y in pairs)
    valid_no = [t for t in thresholds if (yes is None or t < yes['threshold']) and positives
                and sum(y and p > t for p, y in pairs) / positives >= screen_recall]
    no = max(valid_no) if valid_no else None
    no_screen_recall = sum(y and p > no for p, y in pairs) / positives if no is not None else None
    uncertain = sum((no is None or p > no) and (yes is None or p < yes['threshold']) for p, _ in pairs)
    return dict(n=len(pairs), positives=positives, yes=yes['threshold'] if yes else None, no=no,
                yes_metrics=yes, screen_recall=no_screen_recall, uncertain=uncertain,
                uncertain_fraction=uncertain / len(pairs), curve=curve,
                status='ok' if yes is not None and no is not None else 'target unattainable on supplied gold')


def calibrate(gold, meetings, target, yes_precision=0.90, screen_recall=0.95):
    with open(gold, newline='') as handle:
        labels = {r['meeting_id']: r for r in csv.DictReader(handle)}
    result = dict(yes_precision_target=yes_precision, screen_recall_target=screen_recall,
                  interpretation='screen_recall is fraction of gold positives not classified automatic no', questions={})
    pairs, skipped = {}, {}
    versions = set()
    for key, column in [('safety', 'safety'), ('temperament', 'temperament'), ('event', 'event_reference')]:
        pairs[key], skipped[key] = [], dict(unscored=0, rule_only=0, invalid_gold=0)
    for meeting in rows(meetings):
        versions.add(meeting['definition_version'])
        gold_row = labels.get(meeting['meeting_id'])
        if gold_row is None:
            continue
        for key, column in [('safety', 'safety'), ('temperament', 'temperament'), ('event', 'event_reference')]:
            truth = (gold_row.get(column) or '').strip().lower()
            if truth not in ('yes', 'no', '1', '0', 'true', 'false'):
                skipped[key]['invalid_gold'] += 1
                continue
            prob = meeting.get(key + '_prob')
            if prob is None:
                if meeting[column] == 'no':
                    prob = 0.0
                    skipped[key]['rule_only'] += 1
                else:
                    skipped[key]['unscored'] += 1
                    continue
            pairs[key].append((prob, truth in ('yes', '1', 'true')))
    for key in pairs:
        result['questions'][key] = {**calibrate_pairs(pairs[key], yes_precision, screen_recall),
                                    'excluded': skipped[key]}
    result['definition_versions'] = sorted(versions)
    result['gold_ids_not_in_meetings'] = sorted(set(labels) - {r['meeting_id'] for r in rows(meetings, ['meeting_id'])})
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, indent=2) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gold', type=Path, default=DEFAULT_OUTPUT / 'gold/labels.csv')
    parser.add_argument('--meetings', type=Path, default=DEFAULT_OUTPUT / 'meetings.parquet')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT / 'gold/calibration.json')
    parser.add_argument('--yes-precision', type=float, default=0.9)
    parser.add_argument('--screen-recall', type=float, default=0.95)
    args = parser.parse_args()
    result = calibrate(args.gold, args.meetings, args.output, args.yes_precision, args.screen_recall)
    print(json.dumps({k: {x: y for x, y in v.items() if x != 'curve'} for k, v in result['questions'].items()}, indent=2))


if __name__ == '__main__':
    main()
