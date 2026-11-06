from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from measures.common import MEETING_SCHEMA, PASSAGE_SCHEMA, definitions, rows, writer
from measures.meetings import group_meetings, summarize
from measures.rules import Rules
from measures.jev import Budget, Cache, PRICE, REQUEST_RESERVE, label, questions_for, state_chunks
from measures.apply import apply_meeting
from measures.calibrate import calibrate_pairs
from measures.gold import sample


@pytest.fixture
def defs():
    return definitions()


def document(rid='r1', **changes):
    row = dict(record_id=rid, tea_id='001902', school_district='Example ISD', county_name='Anderson County',
               platform='boardbook', date=date(2023, 6, 1), date_source='listing', meeting_id='m1',
               meeting_type='Regular', meeting_title='Regular Meeting', detail='Minutes',
               text='A school resource officer will protect students.', text_status='ok',
               preferred_for_meeting=False, content_sha256=None)
    row.update(changes)
    return row


def measured(defs, documents=None):
    documents = documents or [document()]
    meeting, best = summarize(documents, documents[0], defs['version'])
    passages = Rules(defs).measure(meeting, documents, best)
    return meeting, passages


def test_same_day_regular_special_and_distinct_ids_are_separate():
    docs = [document(), document('agenda', detail='Agenda'),
            document('special', meeting_id='m2', meeting_type='Special', meeting_title='Special Meeting'),
            document('regular2', meeting_id='m3')]
    assert sorted(sorted(r['record_id'] for r in g) for g in group_meetings(docs)) == [
        ['agenda', 'r1'], ['regular2'], ['special']]


def test_cross_platform_merge_and_readable_preference(defs):
    docs = [document(text='', text_status='needs_ocr'),
            document('a', detail='Agenda', text='Agenda with school safety'),
            document('w', platform='website', meeting_id=None, detail='Minutes', text_status='partial_ocr')]
    groups = group_meetings(docs)
    assert len(groups) == 1
    meeting, best = summarize(groups[0], docs[0], defs['version'])
    assert best['record_id'] == 'w'
    assert meeting['record_ids'] == ['a', 'r1', 'w']
    assert meeting['has_minutes_text'] and meeting['has_agenda_text']
    assert summarize(list(reversed(groups[0])), docs[0], defs['version'])[0]['meeting_id'] == meeting['meeting_id']


def test_two_platform_ids_merge_only_when_unambiguous():
    docs = [document(), document('w', platform='website', meeting_id='remote')]
    assert len(group_meetings(docs)) == 1
    docs.append(document('second', meeting_id='m2'))
    assert len(group_meetings(docs)) == 3


def test_distinct_published_times_and_undated_documents_do_not_collapse():
    docs = [document(meeting_id=None, meeting_title='Regular Meeting at 5:00 PM'),
            document('b', meeting_id=None, meeting_title='Regular Meeting at 7:00 PM')]
    assert len(group_meetings(docs)) == 2
    assert len(group_meetings([document(meeting_id=None, date=None), document('b', meeting_id=None, date=None)])) == 2


def test_whole_phrases_merge_windows_record_exclusions_and_ignore_idioms(defs):
    meeting, passages = measured(defs, [document(text='Happy to report on insecurity. Social SECURITY and data security. School resource officer with a school safety allotment.')])
    assert len(passages) == 1
    passage = passages[0]
    assert set(passage['excluded_hits']) == {'financial', 'cyber'}
    assert set(passage['subtopic_hits']) == {'police_sro', 'hardening'}
    assert 'temperament' not in passage['candidate_for']
    assert 'insecurity' not in passage['matched_terms']


def test_flags_measure_extras_but_passages_use_best_and_agenda(defs):
    docs = [document(text='Routine vote.'), document('a', detail='Agenda', text='School safety funding.'),
            document('x', detail='Extras', text='Closed under 551.076 and 551.089 after the Uvalde shooting.')]
    meeting, passages = measured(defs, docs)
    assert meeting['closed_security_session'] is True
    assert meeting['named_events'] == ['uvalde']
    assert {p['record_id'] for p in passages} == {'a'}


@pytest.mark.parametrize('text,expected', [('551.076', True), ('551.089', True), ('security audits', True),
                                         ('551x076', False), ('section 551.074 personnel', False)])
def test_closed_session_patterns(defs, text, expected):
    assert measured(defs, [document(text=text)])[0]['closed_security_session'] is expected


def test_long_windows_have_valid_offsets_and_keep_triggers(defs):
    defs['passages'].update(window_chars=50, merge_gap_chars=100, max_passage_chars=180)
    text = ('school resource officer ' + 'x' * 60 + ' ')*20
    _, passages = measured(defs, [document(text=text)])
    assert len(passages) > 1
    assert all(p['text'] == text[p['start']:p['end']] and len(p['text']) <= 180 for p in passages)
    assert all('school resource officer' in p['text'] for p in passages)


def fake_response(payload):
    answers = {}
    for key, q in payload['questions'].items():
        answers[key] = {'noul': .75} if q['type'] == 'noul' else {
            'choice': 'present' if key.startswith(('subtopic:', 'event:')) else 'none',
            'confidence': .99, 'probabilities': {}}
    return {'model': 'jev-test', 'usage': {'input_tokens': 1000}, 'answers': answers}


def test_live_boundary_cache_skip_state_change_and_threshold_replay(tmp_path, defs):
    meeting, passages = measured(defs)
    with writer(tmp_path / 'passages.parquet', PASSAGE_SCHEMA) as write:
        write(passages)
    client = Mock()
    client.ask.side_effect = fake_response
    result = label(tmp_path, defs, .01, client=client)
    assert result['input_tokens'] == 1000 and client.ask.call_count == 1
    assert sum(r['input_tokens'] for r in rows(tmp_path / 'labels.parquet')) == 1000
    assert label(tmp_path, defs, .01, client=client)['input_tokens'] == 0
    assert client.ask.call_count == 1
    cache = Cache(tmp_path / 'labels.parquet')
    assert apply_meeting(dict(meeting), passages, defs, cache)['safety'] == 'uncertain'
    defs['thresholds']['safety']['yes'] = .70
    updated = apply_meeting(dict(meeting), passages, defs, cache)
    assert updated['safety'] == 'yes' and updated['safety_subtopics'] == ['police_sro']
    assert client.ask.call_count == 1
    changed = [dict(p, text=p['text'] + ' New facts.') for p in passages]
    assert apply_meeting(dict(meeting), changed, defs, cache)['safety'] == 'not_screened'
    assert all(len(e['excerpt']) <= 280 for e in updated['evidence'])


def test_hard_cap_does_not_dispatch_unreserved_request(tmp_path, defs):
    _, passages = measured(defs)
    with writer(tmp_path / 'passages.parquet', PASSAGE_SCHEMA) as write:
        write(passages)
    client = Mock()
    result = label(tmp_path, defs, REQUEST_RESERVE * PRICE - .000001, client=client)
    assert result['cap_reached'] and result['input_tokens'] == 0
    client.ask.assert_not_called()


def test_no_passage_and_unreadable_are_distinct(tmp_path, defs):
    cache = Cache(tmp_path / 'labels.parquet')
    meeting, passages = measured(defs, [document(text='Routine approval of minutes.')])
    assert apply_meeting(meeting, passages, defs, cache)['safety'] == 'no'
    meeting, passages = measured(defs, [document(text='', text_status='needs_ocr')])
    assert apply_meeting(meeting, passages, defs, cache)['safety'] == 'not_screened'


def test_calibration_respects_precision_and_positive_screening_recall():
    pairs = [(.95, True), (.9, True), (.3, True), (.4, False), (.1, False)]
    result = calibrate_pairs(pairs)
    assert result['yes_metrics']['precision'] >= .9
    assert result['screen_recall'] >= .95
    assert result['no'] < result['yes']
    assert result['uncertain'] > 0
    assert calibrate_pairs([(.8, False)])['status'] != 'ok'


def test_stratified_sample_70_30_reproducible(defs):
    base, _ = measured(defs)
    population = [dict(base, meeting_id=str(i), n_passages=1 if i < 800 else 0) for i in range(1000)]
    chosen = sample(population)
    assert len(chosen) == 500 and len({m['meeting_id'] for m in chosen}) == 500
    assert sum(bool(m['n_passages']) for m in chosen) == 350
    assert chosen == sample(population)


def test_whitespace_and_unicode_offsets_remain_source_offsets(defs):
    text = 'Éxample   SCHOOL\n  safety and a student John Smith called 512-555-0101, private@example.org.'
    _, passages = measured(defs, [document(text=text)])
    assert len(passages) == 1
    assert 'school safety' in passages[0]['matched_terms']
    assert passages[0]['text'] == text[passages[0]['start']:passages[0]['end']]
    from measures.common import redact
    cleaned = redact(text + ' 5125550101 (512)555-0101 Student Jane Doe.')
    assert all(private not in cleaned for private in ('512-555-0101', '5125550101', '(512)555-0101', 'private@example.org', 'John Smith', 'Jane Doe'))


def test_small_parquet_pipeline_keeps_all_meetings_and_candidate_evidence(tmp_path, defs):
    from measures.run import build
    from measures.apply import apply
    docs = [document(text='Students are stressed about school security.'),
            document('unreadable', meeting_id='m2', date=date(2023, 6, 2), text=None, text_status='needs_ocr')]
    source = tmp_path / 'records.parquet'
    pq.write_table(pa.Table.from_pylist(docs), source)
    output = tmp_path / 'measures'
    summary = build(source, output, defs)
    assert summary['meetings'] == 2 and summary['readable'] == 1 and summary['with_passages'] == 1
    client = Mock()
    client.ask.side_effect = fake_response
    label(output, defs, .01, client=client)
    applied = apply(output, defs)
    result = list(rows(output / 'meetings.parquet'))
    assert applied == {'meetings': 2, 'labelled': 1}
    assert result[0]['safety_prob'] == .75 and result[0]['temperament_prob'] == .75
    assert result[0]['evidence'] and result[1]['safety'] == 'not_screened'


def test_client_retries_only_explicit_rate_limit(monkeypatch):
    from measures.jev import Client
    import requests
    rate = Mock(status_code=429, headers={'Retry-After': '0'})
    success = Mock(status_code=200)
    success.json.return_value = {'answers': {}, 'usage': {'input_tokens': 10}}
    post = Mock(side_effect=[rate, success])
    monkeypatch.setattr('measures.jev.requests.post', post)
    monkeypatch.setattr('measures.jev.time.sleep', Mock())
    assert Client('test-key').ask({'state': 'x'})['usage']['input_tokens'] == 10
    assert post.call_count == 2
    post.side_effect = requests.Timeout('ambiguous receipt')
    with pytest.raises(requests.Timeout):
        Client('test-key').ask({'state': 'x'})
    assert post.call_count == 3


def test_undated_titled_documents_have_distinct_meeting_ids(defs):
    docs = [document(meeting_id=None, date=None), document('other', meeting_id=None, date=None)]
    ids = [summarize(group, docs[0], defs['version'])[0]['meeting_id'] for group in group_meetings(docs)]
    assert len(set(ids)) == 2


def test_reused_platform_id_does_not_merge_regular_and_special(defs):
    docs = [document(), document('special', meeting_title='Special Meeting', meeting_type='Special')]
    groups = group_meetings(docs)
    assert len(groups) == 2
    assert len({summarize(g, docs[0], defs['version'])[0]['meeting_id'] for g in groups}) == 2


def test_calibration_counts_gold_positive_rule_misses(tmp_path, defs):
    from measures.calibrate import calibrate
    import csv
    meeting, _ = measured(defs, [document(text='Routine business.')])
    meeting.update(safety='no', temperament='no', event_reference='no')
    with writer(tmp_path / 'meetings.parquet', MEETING_SCHEMA) as write:
        write([meeting])
    with open(tmp_path / 'labels.csv', 'w', newline='') as handle:
        target = csv.DictWriter(handle, ['meeting_id', 'safety', 'temperament', 'event_reference'])
        target.writeheader()
        target.writerow(dict(meeting_id=meeting['meeting_id'], safety='yes', temperament='no', event_reference='no'))
    result = calibrate(tmp_path / 'labels.csv', tmp_path / 'meetings.parquet', tmp_path / 'calibration.json')
    assert result['questions']['safety']['n'] == 1
    assert result['questions']['safety']['positives'] == 1
    assert result['questions']['safety']['status'] != 'ok'
    assert result['questions']['safety']['excluded']['rule_only'] == 1


def test_ambiguous_dated_titles_cannot_share_an_id(defs):
    docs = [document('one', meeting_id='first'), document('two', meeting_id='second'),
            document('loose1', platform='website', meeting_id=None),
            document('loose2', platform='website', meeting_id=None)]
    groups = group_meetings(docs)
    ids = [summarize(g, docs[0], defs['version'])[0]['meeting_id'] for g in groups]
    assert len(ids) == len(set(ids))


def test_date_only_website_title_merges_unique_platform_meeting():
    docs = [document(), document('web', platform='website', meeting_id=None,
                                  meeting_title='June 1, 2023', meeting_type=None)]
    assert len(group_meetings(docs)) == 1


def test_context_limit_splits_without_dropping_candidate_passages():
    from measures.jev import estimate_tokens
    passages = [dict(passage_id=str(i).zfill(12), detail='Minutes',
                     text='School safety and emergency plans. ' * 300) for i in range(20)]
    questions = {'safety': {'type': 'noul', 'instructions': 'Is school safety discussed?'}}
    chunks = state_chunks(passages, questions)
    assert len(chunks) >= 2
    assert all(estimate_tokens(state) + estimate_tokens(str(questions['safety'])) + 600 < 32000 for state in chunks)
    for i in range(20):
        assert sum(state.count('passage ' + str(i).zfill(12)) for state in chunks) == 1
