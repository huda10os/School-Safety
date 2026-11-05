# Texas school board meeting records

Agendas, minutes, projector views and meeting extras for Texas's 1,018 regular public school districts (ISDs, CISDs, CSDs and the one MSD), collected from each district's public meeting-records platform. Charter schools are out of scope.

One row in the dataset is one document link on a district's meeting listing. For example, Brownsville ISD's BoardBook row "October 21, 2026 - Regular Meeting" with links Agenda, Projector and Minutes becomes three rows, each with the document's extracted text.

## Layout

```
code/                 collection scripts (Python 3.12)
  registry.py         TEA district file -> data/reference/districts.csv
  match_boardbook.py  BoardBook organizations matched to districts
  detect_platform.py  finds each remaining district's meeting platform -> data/reference/platforms.csv
  pipeline.py         discover listings, collect documents, build the dataset
  platforms/          one adapter per platform: boardbook, diligent, boarddocs, legistar, website
  fetch.py            polite cached HTTP (per-host delays, raw response cache)
  browser.py          agent-browser fallback for sites behind a bot check
  extract.py          text from PDF, DOCX and plain text (no OCR)
  build_dataset.py    data/records.parquet and data/coverage.parquet
  app/                local web app: progress, districts, record browser
  tests/              pytest suite
data/reference/       small committed inputs (district list, platform map, BoardBook orgs)
data/                 everything else is local and not committed (raw cache, staging, Parquet)
```

## Setup

```sh
uv venv --python 3.12 .venv            # or: python3.12 -m venv .venv
uv pip install --python .venv/bin/python -r code/requirements.txt
npm install -g agent-browser           # only needed for bot-protected district sites
```

Works on Linux and macOS.

## Reproduce

Run from `code/`:

```sh
../.venv/bin/python registry.py                 # 1,018 districts from TEA
../.venv/bin/python match_boardbook.py          # BoardBook org matches
../.venv/bin/python detect_platform.py --workers 16   # platform map for every district
../.venv/bin/python pipeline.py all --workers 24 --processes 4
```

`pipeline.py all` runs three stages, each of which can also be run alone:

1. `discover` reads every district's meeting listing into `data/staging/listings/`.
2. `documents` downloads every listed document and extracts its text into `data/staging/records/`. Minutes are collected for every district first, then agendas, then the rest.
3. `build` writes `data/records.parquet` and `data/coverage.parquet`.

Limit a run with `--tea-ids 031901,057905` or `--platform boardbook`. Everything is resumable: downloads are cached in `data/raw/`, and finished documents are skipped. To pick up new meetings later, run `pipeline.py discover --refresh` and then `pipeline.py documents`; only new links are downloaded.

Requests are spaced per host (`code/config.py`): BoardBook 0.5 s, BoardDocs and each Diligent portal 3 s, everything else 1 s. Statewide BoardBook collection therefore takes a couple of days.

Memory: network work runs in threads; PDF parsing runs in a small process pool whose workers are recycled and replaced if one dies. A full run stays within a few GB.

## Dataset

`data/records.parquet`, one row per document link:

| Column | Meaning |
| --- | --- |
| `school_district`, `tea_id` | District name and TEA 6-digit id |
| `county_name` | County of the district's TEA county number |
| `date`, `date_raw` | Meeting date, and the date text exactly as shown |
| `detail`, `detail_raw` | Agenda, Minutes, Projector, Extras, Recording or Other, and the link label as shown |
| `meeting_type`, `meeting_title` | Regular, Special, Workshop, etc. as the source states it |
| `text` | Extracted text; null unless `text_status` is `ok` or `partial_ocr` |
| `text_status` | `ok`, `needs_ocr` (scanned, no text layer), `partial_ocr`, `empty`, `unsupported`, `too_large`, `unavailable` (no longer published), `recording` (audio/video link only), `error`, `pending` |
| `source_link` | The link as published; `document_link` is the file actually parsed |
| `listing_row_text` | Exact text of the listing row the metadata came from |
| `raw_path`, `content_sha256` | Cached original bytes, for OCR and review |
| `attachment_links` | Agenda item attachments (their text is not collected) |
| `preferred_for_meeting` | True on the best document per meeting: Minutes, else Agenda, else Projector |
| `reviewed` | `not_reviewed` until the planned review step runs |

Full field descriptions are in `code/records.py` (`FIELDS`).

`data/coverage.parquet`, one row per district, with date ranges, counts and follow-up flags:

- `late_start`: earliest minutes after 2018-01-01
- `short_span`: minutes cover less than 3 years
- `long_gap`: more than 6 months between consecutive meetings
- `multi_platform`: records on more than one platform (often a platform switch)
- `no_records`: nothing found

Flagged districts are candidates to contact directly for older records.

## Web app

```sh
.venv/bin/python code/app/server.py --port 8791
```

Shows collection progress with a rate and estimated time to finish, every district with its coverage flags, and a searchable record browser with CSV export. Set `APP_PASSWORD` to require a password.

## Not yet done

- OCR for `needs_ocr` and `partial_ocr` documents (raw files are kept for it).
- Transcription of `Recording` rows. They hold links only: audio/video files posted on district sites, and YouTube, Swagit and Vimeo links found in meeting listings. Video links embedded elsewhere on district websites are not yet collected.
- Legacy `.doc` conversion.
- Independent review of extracted records (planned with TypeSafe's jev).
