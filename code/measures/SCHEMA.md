# Measures data contract

Produced by `code/measures/run.py` from `data/records.parquet` and
`code/measures/definitions.yaml`; read by the web app and by researchers.
All files live in `data/measures/` (not committed). Every row carries
`definition_version` (from definitions.yaml) so results from different
definitions can coexist and be compared.

## meetings.parquet: one row per board meeting

| column | type | meaning |
| --- | --- | --- |
| meeting_id | string | stable id: sha256 of tea_id, date and the platform meeting id or normalized title |
| tea_id, school_district, county_name, county_fips | string | from data/reference/districts.csv |
| nces_id | string | NCES district id (join key for outside data) |
| district_lat, district_lon | float | district office location (TEA) |
| esc_region | string | Education Service Center region number, when available |
| date | date | meeting date (null when unknown; such meetings are excluded from time views) |
| date_source | string | listing / link_or_filename / document_text |
| meeting_type, meeting_title | string | as published |
| platforms | list<string> | platforms the meeting's documents came from |
| record_ids | list<string> | all documents of the meeting (records.parquet) |
| best_record_id, best_detail | string | the document measured: Minutes > Agenda > Projector > other text |
| has_minutes_text, has_agenda_text | bool | which document kinds have readable text |
| readable | bool | best document has text (ok or OCR'd) |
| text_chars | int | characters in the measured text |
| n_passages | int | candidate passages found |
| closed_security_session | bool | agenda/minutes cite Gov't Code 551.076 / 551.089 (rule) |
| named_events | list<string> | event keys whose terms appear (rule, before model confirmation) |
| safety | string | yes / no / uncertain / not_screened (no candidate passage = no) |
| safety_prob | float | highest jev probability over the meeting's passages |
| safety_subtopics | list<string> | subtopic keys judged present |
| safety_resolved_by | string | jev / claude / human / rule |
| temperament | string | yes / no / uncertain / not_screened |
| temperament_prob | float | |
| temperament_tone | string | negative / positive / mixed / none |
| temperament_who | string | students / staff / both / none |
| event_reference | string | yes / no / uncertain / not_screened |
| event_keys | list<string> | events confirmed as referenced |
| evidence | list<struct{passage_id, measure, excerpt}> | up to 3 excerpts per measure, <=280 chars, names of students/private citizens redacted |
| definition_version, label_version | string | definitions.yaml version; labelling run id |

## passages.parquet: candidate text spans

| column | type | meaning |
| --- | --- | --- |
| passage_id | string | sha256 of record_id, start, end |
| meeting_id, record_id, tea_id | string | |
| date | date | |
| detail | string | document type the passage came from |
| start, end | int | character offsets in the record text |
| text | string | passage text |
| candidate_for | list<string> | safety / temperament / event |
| matched_terms | list<string> | terms that made it a candidate |
| subtopic_hits | list<string> | subtopic keys whose terms matched |
| excluded_hits | list<string> | excluded-sense keys that matched (e.g. cyber) |
| definition_version | string | |

## labels.parquet: model answers (append-only cache)

| column | type | meaning |
| --- | --- | --- |
| passage_id or meeting_id | string | what was labelled (jev runs per meeting with its passages as state) |
| question_key | string | safety / temperament / event / subtopic / tone / who |
| question_hash | string | sha256 of the exact question wording |
| model | string | e.g. jev-1.14.0 / claude-opus-5-5 / human:<initials> |
| prob | float | yes-probability (yes/no questions) |
| choice, choice_conf, probabilities | string, float, json | choice questions |
| input_tokens | int | billed tokens (jev) |
| created_at | string | UTC timestamp |

## gold/ : hand-checked sample

`gold/sample.parquet` (meeting_ids drawn for coding), `gold/prelabels.jsonl`
(Claude's draft answers with quotes), `gold/labels.csv` (corrected labels:
meeting_id, safety, subtopics, temperament, tone, who, event_reference,
coder, notes), `gold/calibration.json` (per-question precision/recall at the
chosen thresholds and the size of the uncertain band).

## Thresholds

Yes/no questions use three zones from definitions.yaml `thresholds`
(`prob >= yes` -> yes, `prob <= no` -> no, otherwise uncertain). Changing
thresholds recomputes labels from stored probabilities without new model calls.
