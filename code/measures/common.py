"""Shared storage contracts, with additive provenance fields for cache safety."""
from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DEFINITIONS = Path(__file__).with_name('definitions.yaml')
DEFAULT_OUTPUT = ROOT / 'data/measures'
STR = pa.string()
STRINGS = pa.list_(STR)
EVIDENCE = pa.list_(pa.struct([('passage_id', STR), ('measure', STR), ('excerpt', STR)]))
MEETING_SCHEMA = pa.schema([
    *[(k, STR) for k in ('meeting_id', 'tea_id', 'school_district', 'county_name', 'county_fips', 'nces_id')],
    ('district_lat', pa.float64()), ('district_lon', pa.float64()), ('esc_region', STR),
    ('date', pa.date32()), ('date_source', STR), ('meeting_type', STR), ('meeting_title', STR),
    ('platforms', STRINGS), ('record_ids', STRINGS), ('best_record_id', STR), ('best_detail', STR),
    ('has_minutes_text', pa.bool_()), ('has_agenda_text', pa.bool_()), ('readable', pa.bool_()),
    ('text_chars', pa.int64()), ('n_passages', pa.int32()), ('closed_security_session', pa.bool_()),
    ('named_events', STRINGS), ('safety', STR), ('safety_prob', pa.float64()),
    ('safety_subtopics', STRINGS), ('safety_resolved_by', STR), ('temperament', STR),
    ('temperament_prob', pa.float64()), ('temperament_tone', STR), ('temperament_who', STR),
    ('event_reference', STR), ('event_prob', pa.float64()), ('event_keys', STRINGS),
    ('evidence', EVIDENCE), ('definition_version', STR), ('label_version', STR),
])
PASSAGE_SCHEMA = pa.schema([
    *[(k, STR) for k in ('passage_id', 'meeting_id', 'record_id', 'tea_id')],
    ('date', pa.date32()), ('detail', STR), ('start', pa.int64()), ('end', pa.int64()),
    ('text', STR), *[(k, STRINGS) for k in ('candidate_for', 'matched_terms', 'subtopic_hits', 'excluded_hits')],
    ('definition_version', STR),
])
LABEL_SCHEMA = pa.schema([
    *[(k, STR) for k in ('meeting_id', 'question_key', 'question_hash', 'model', 'state_hash', 'request_id')],
    ('prob', pa.float64()), ('choice', STR), ('choice_conf', pa.float64()), ('probabilities', STR),
    ('input_tokens', pa.int64()), ('created_at', STR), ('definition_version', STR),
    ('resolved_model', STR), ('label_version', STR),
])


def digest(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def definitions(path=DEFAULT_DEFINITIONS):
    with open(path) as handle:
        result = yaml.safe_load(handle)
    for key in ('safety', 'temperament', 'event'):
        # YAML 1.1 reads unquoted yes/no mapping keys as booleans.
        zone = result['thresholds'][key]
        result['thresholds'][key] = {('yes' if k is True else 'no' if k is False else k): v
                                     for k, v in zone.items()}
        if not 0 <= result['thresholds'][key]['no'] < result['thresholds'][key]['yes'] <= 1:
            raise ValueError(f'Invalid thresholds for {key}')
    return result


def rows(path, columns=None):
    path = Path(path)
    if not path.exists():
        return
    if path.is_dir():
        for part in sorted(path.glob('*.parquet')):
            yield from rows(part, columns)
        return
    for batch in pq.ParquetFile(path).iter_batches(batch_size=512, columns=columns, use_threads=False):
        yield from batch.to_pylist()


@contextmanager
def writer(path, schema):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.writing')
    try:
        with pq.ParquetWriter(temporary, schema, compression='zstd') as target:
            yield lambda values: target.write_table(pa.Table.from_pylist(values, schema=schema)) if values else None
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def redact(text, limit=None):
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b', '[email]', text)
    text = re.sub(r'(?<!\w)(?:\+?1[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[ .-]?)\d{3}[ .-]?\d{4}(?!\w)', '[phone]', text)
    # Conservative explicit person roles only; this is not general named-entity recognition.
    text = re.sub(r'\b([Ss]tudent|[Pp]arent|[Cc]itizen)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})',
                  r'\1 [name]', text)
    return text[:limit] if limit is not None else text
