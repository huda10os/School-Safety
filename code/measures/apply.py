"""Apply current thresholds to exact-state cached probabilities, without calls.

Evidence is a candidate excerpt, not a model-generated supporting quotation.
Email and phone redaction plus explicit student/parent/citizen name patterns are
best effort; unidentified names can remain and packets need human privacy review.
"""
from __future__ import annotations

import re
import argparse
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = 'measures'

from .common import DEFAULT_DEFINITIONS, DEFAULT_OUTPUT, MEETING_SCHEMA, definitions, redact, rows, writer
from .jev import Cache, passage_groups, questions_for, state_chunks


def zone(prob, thresholds):
    if prob is None:
        return 'not_screened'
    if prob >= thresholds['yes']:
        return 'yes'
    if prob <= thresholds['no']:
        return 'no'
    return 'uncertain'


def excerpt(passage):
    from .rules import phrase
    matches = [re.search(phrase(term), passage['text'], re.I) for term in passage['matched_terms']]
    start = max(0, min((m.start() for m in matches if m), default=0) - 90)
    return redact(passage['text'][start:start + 400], 280)


def apply_meeting(meeting, passages, defs, cache, model='jev-latest'):
    questions = questions_for(defs, passages) if passages and meeting['meeting_id'] in cache.meeting_ids else {}
    states = state_chunks(passages, questions) if questions else []
    answers = {key: [cache.get(meeting['meeting_id'], q, model, state) for state in states]
               for key, q in questions.items()}
    candidates = {k for p in passages for k in p['candidate_for']}
    used = [a for values in answers.values() for a in values if a]
    meeting.update(safety_subtopics=[], temperament_tone=None, temperament_who=None,
                   event_keys=[], evidence=[],
                   label_version=max((a.get('label_version') or a['created_at'] for a in used), default=None))
    for key, field in [('safety', 'safety'), ('temperament', 'temperament'), ('event', 'event_reference')]:
        values = answers.get(key, [])
        complete = values and all(a and a['prob'] is not None for a in values)
        prob = max(a['prob'] for a in values) if complete else None
        if not meeting['readable']:
            decision = 'not_screened'
        elif not passages:
            decision, prob = 'no', None
        else:
            decision = zone(prob, defs['thresholds'][key])
        meeting[field], meeting[key + '_prob'] = decision, prob
        if key == 'safety':
            meeting['safety_resolved_by'] = 'rule' if decision == 'no' and not passages else 'jev' if complete else None
        if decision in ('yes', 'uncertain'):
            seen = set()
            for p in passages:
                text = excerpt(p)
                if (key in p['candidate_for'] or key not in candidates) and text not in seen:
                    meeting['evidence'].append(dict(passage_id=p['passage_id'], measure=key, excerpt=text))
                    seen.add(text)
                    if len(seen) == 3:
                        break
    confidence = defs['thresholds']['choice_min_confidence']
    def choices(key):
        return {a['choice'] for a in answers.get(key, [])
                if a and a['choice_conf'] is not None and a['choice_conf'] >= confidence}
    if meeting['safety'] == 'yes':
        meeting['safety_subtopics'] = sorted(key.split(':', 1)[1] for key in answers
                                            if key.startswith('subtopic:') and 'present' in choices(key))
    if meeting['event_reference'] == 'yes':
        meeting['event_keys'] = sorted(key.split(':', 1)[1] for key in answers
                                       if key.startswith('event:') and 'present' in choices(key))
    if meeting['temperament'] in ('yes', 'uncertain'):
        tones, who = choices('tone') - {'none'}, choices('who') - {'none'}
        meeting['temperament_tone'] = 'mixed' if 'mixed' in tones or len(tones) > 1 else next(iter(tones), None)
        meeting['temperament_who'] = 'both' if 'both' in who or len(who) > 1 else next(iter(who), None)
    elif meeting['temperament'] == 'no':
        meeting.update(temperament_tone='none', temperament_who='none')
    meeting['definition_version'] = defs['version']
    return meeting


def apply(output=DEFAULT_OUTPUT, defs=None, model='jev-latest'):
    output, defs = Path(output), defs or definitions()
    cache = Cache(output / 'labels.parquet')
    groups = iter(passage_groups(output / 'passages.parquet'))
    pending = next(groups, None)
    labelled, count, batch = 0, 0, []
    with writer(output / 'meetings.parquet', MEETING_SCHEMA) as write:
        for meeting in rows(output / 'meetings-base.parquet'):
            passages = []
            if pending and pending[0] == meeting['meeting_id']:
                passages = pending[1]
                pending = next(groups, None)
            meeting = apply_meeting(meeting, passages, defs, cache, model)
            labelled += meeting['label_version'] is not None
            batch.append(meeting)
            count += 1
            if len(batch) == 512:
                write(batch)
                batch.clear()
        write(batch)
    if pending:
        raise ValueError('Passages are not in meetings-base order')
    return dict(meetings=count, labelled=labelled)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument('--definitions', type=Path, default=DEFAULT_DEFINITIONS)
    parser.add_argument('--model', default='jev-latest')
    args = parser.parse_args()
    print(apply(args.output, definitions(args.definitions), args.model))
