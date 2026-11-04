# Texas school board public records

The collection is **in progress**, not a finished statewide archive. Open [progress.html](progress.html) for current counts, district-level coverage, issues, and review results. It automatically reloads every 30 seconds while the tracker process runs. The same report is currently served at `http://127.0.0.1:8765/progress.html` on this computer.

`data/records.parquet` is a periodically regenerated snapshot. It includes every discovered BoardBook Details link, including **pending rows with null text**. Use `extraction_status == 'ok'` for successful text extraction and `reviewed == true` for the subset that passed independent automated source verification. Do not interpret discovered rows as fully extracted or verified.

## Files

- `code/`: reproducible collection, extraction, OCR, independent review, export, and tracking scripts.
- `data/records.parquet`: one row per district and linked record type.
- `data/district_coverage.parquet`: all 1,020 districts, counts, dates, extraction issues, and discovery gaps.
- `data/districts.json`: district registry and county-specific BoardBook mapping.
- `data/wikipedia-districts.json`: complete structured Wikipedia regional-list snapshot.
- `data/district-matching-seed.json`: explicitly resolved name/county mappings used to rebuild the registry.
- `data/official-websites.json` and `data/official-website-evidence.json`: official-site candidates observed on district Wikipedia articles. Candidate sites can be outdated; discovery and review log errors rather than guessing replacements.
- `data/raw/`: downloaded evidence. HTTP response bytes are compressed and content hashes retained.
- `data/staging/`: resumable per-district discovery, per-record extraction, and independent review evidence.
- `data/progress.json`, `data/summary.json`: live tracker state and last Parquet export summary.

## Reproduce or resume

Run from this repository's root. Python 3.12 is used in the current environment.

```sh
python3 -m venv .venv
.venv/bin/pip install -r code/requirements.txt
.venv/bin/python code/registry.py
.venv/bin/python code/collect.py inventory --workers 8
.venv/bin/python code/collect.py extract --workers 12
.venv/bin/python code/fallback.py retry --workers 6 --max-pages 80
.venv/bin/python code/fallback.py metadata
.venv/bin/python code/fallback.py repair --workers 2
.venv/bin/python code/retry_boardbook_errors.py --workers 2
.venv/bin/python code/review.py --watch --input data/staging/records --output data/staging/reviews --county-map data/reviewer-county-map.json --workers 6 --retry-unverified
```

The reviewer watcher intentionally waits for additional records until `data/collection-complete.json` exists. Run it in a separate terminal while collection proceeds. To review one bounded input instead, pass a JSONL or Parquet file without `--watch`; an optional `--limit` is available for debugging, not full production review.

For ongoing snapshots and the HTML tracker, use separate terminals:

```sh
.venv/bin/python -u code/snapshot_loop.py --interval 300
.venv/bin/python -u code/progress.py --watch --interval 30
python3 -m http.server 8765 --bind 127.0.0.1
```

Then open `http://127.0.0.1:8765/progress.html`. The self-contained HTML can also be opened directly from the filesystem. These are local processes; the computer must remain awake and the processes must remain running. A stale update timestamp indicates the tracker has stopped.

One-off snapshot and tracker regeneration:

```sh
.venv/bin/python code/export_dataset.py
.venv/bin/python code/progress.py
```

Inventory and extraction skip completed checkpoint files. Failed or incomplete checkpoint files are preserved rather than silently erased. To retry a district or record, move its checkpoint out of the relevant staging directory and rerun the appropriate phase. Refreshing live registry sources is optional with `code/registry.py --refresh`; when Wikipedia blocks HTTP access the script explicitly retains the complete supplied structured snapshot. New or changed BoardBook matches need source review before modifying the frozen mapping.

Download workers wait when free storage falls below 5 GiB. The tracker reports active worker processes and available storage. Parquet exports use a file lock to avoid overlapping writes. Changed extraction checkpoints are automatically queued for independent review again.

Avoid starting duplicate copies of a collection or review phase against the same output directory. Updates are atomic at the individual-file level, but there is no distributed job scheduler.

## Requested columns

| Column | Meaning |
| --- | --- |
| `school_district` | District name from the Wikipedia regional list. |
| `county_name` | County under which Wikipedia groups the district; not an exhaustive list of counties served. |
| `date` | Meeting date, stored as Parquet `date32`, not the date of download. Null when unsupported by available evidence. |
| `detail` | Exact BoardBook Details label, or supported document type on an official district source (Agenda, Minutes, Projector, Extras, Public Notice, etc.). |
| `meeting_type` | Source's meeting type, such as Regular or Special. Null if the source does not establish it. |
| `text` | Extracted record body. Pending rows have null text; failed/scanned rows remain explicitly flagged. |
| `source_link` | Exact record link. Document viewers additionally retain the resolved binary URL in `text_source_link`. Diligent API URLs include a document-ID fragment selecting a public document from the response. |
| `reviewed` | True only when the independent review checks pass. False includes pending, failed, incomplete, and unresolved reviews. |

Additional columns preserve record IDs, district IDs, meeting-list provenance, exact row text, extraction methods, OCR flags, raw-response hashes/paths where available, errors, and independent review evidence.

## Source and review policy

Sources: [Wikipedia regional list](https://en.wikipedia.org/wiki/List_of_school_districts_in_Texas), [BoardBook public directory](https://meetings.boardbook.org/Public), and district-linked official archives. Wikipedia's separate defunct-district section is excluded. The registry snapshot was independently reopened in the browser for county verification. That evidence is saved in `data/reviewer-county-map.json`.

BoardBook discovery reads the entire public meeting table, not the browser's truncated accessibility listing. It preserves every link in each row's Details column. District-site adapters include static HTML/PDF/DOCX, Apptegy document trees, Finalsite resource collections, and Diligent public meeting documents. Diligent documents marked non-public are excluded even if returned by an endpoint. Generic website discovery is bounded by `--max-pages`; a completed discovery pass does not establish exhaustive historical coverage. Blocked sites, inaccessible legacy archives, unsupported formats, missing metadata, and discovery limits remain reported gaps requiring follow-up.

The reviewer is separate from the collector and does not import its extraction parser. It independently reopens record links and their source listings, checks district identity, county evidence, meeting date/type, detail type, and complete normalized text. Independent automated verification is **not a human visual review of every document**. OCR-only records remain unverified until image-to-text verification is performed. Source links and hashes allow a later reviewer to inspect them.

macOS OCR uses local Apple Vision and PDFKit through `code/ocr_macos.swift`, compiled with the system Swift compiler into `data/tools/`. It requires Apple's command-line developer tools. No documents are uploaded to an OCR service. OCR output can contain transcription errors and is labeled accordingly. Other platforms may install optional `pymupdf` and `rapidocr_onnxruntime` for the alternate local OCR implementation. Legacy binary `.doc` files currently require a separate conversion adapter; they are explicitly reported rather than decoded as HTML.

Coverage dates describe available links, including scheduled future meetings where the source publishes an agenda. A short-history flag is set for fewer than two years of observed dates; this is a follow-up heuristic, not proof of missing records. Minutes-specific start and end dates are also retained. `first_collected_minutes_date` and `last_collected_minutes_date` separately describe successfully extracted minutes, excluding pending and failed text. Broken links and server-side changes can affect a future rerun, so retained raw responses support auditing the collected snapshot.
