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
  ocr.py, ocr/        Indra OCR of scanned pages (Docker, CPU)
  measures/           meeting table, safety/temperament definitions, rule flags, model labels
  run_statewide.sh    every stage with the settings used for the published run
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

One command runs every stage with the settings used for the published run:

```sh
code/run_statewide.sh          # full run
code/run_statewide.sh update   # later: re-read listings, collect only new documents
```

It runs, in order:

1. `registry.py` builds the 1,018-district list from TEA's district file. Counties come from TEA's county numbers.
2. `match_boardbook.py` and `detect_platform.py` build the platform map, unless `data/reference/platforms.csv` already exists. The committed map plus `platform_overrides.csv` (hand-verified sources) is what the published run used.
3. One `pipeline.py` process per platform, in parallel: `discover` reads each district's meeting listing; `documents` downloads every listed document and extracts its text, minutes first for every district, then agendas, then the rest.
4. `ocr.py` reads scanned pages with Indra OCR in a Docker container capped at 6 CPUs and 4 GB (`docker build -t txboards-indra:1.1.2 code/ocr` first). Only pages without a text layer are OCR'd. About 5–6 hours for the ~314,000 scanned pages.
5. `build_dataset.py` writes `data/records.parquet` and `data/coverage.parquet`.
6. `measures/run.py` builds meetings, candidate passages and rule flags from `code/measures/definitions.yaml`. See [Measures](#measures).

Everything is resumable: responses are cached in `data/raw/`, finished documents are skipped, and OCR output is kept per document. Limit any `pipeline.py` stage with `--tea-ids 031901,057905` or `--platform boardbook`.

**Settings that matter.** These were measured on a 16-thread, 59 GB machine:

- **Request spacing per host** (`code/config.py`): BoardBook 0.1 s, with automatic back-off on 429/503 responses; BoardDocs and each Diligent portal 3 s; everything else 1 s.
- **District-website discovery:** above about 6 threads it ran out of memory (crawls of up to 200 pages per district, plus browser sessions for bot-checked sites). Up to 3 browser sessions run at once, configurable with `TXBOARDS_BROWSER_SLOTS`.
- **Downloads:** streamed. Audio and video files are recorded as `recording` links rather than downloaded. Files over 150 MB are `too_large`.
- **Skipped sources:** sources that hang or duplicate another platform are listed with a reason in `data/reference/skip_sources.csv`. Dallas ISD's website crawl hangs; its meetings come from Diligent.
- **What does not repeat exactly:** pages behind bot checks depend on browser timing, and very large site archives stop at the 200-page crawl limit (Del Valle ISD and El Paso ISD in the published run). Sites also change over time.

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

- Transcription of `Recording` rows. They hold links only: audio/video files posted on district sites, and YouTube, Swagit and Vimeo links found in meeting listings. Video links embedded elsewhere on district websites are not yet collected.
- Legacy `.doc` conversion.
- Independent review of extracted records (planned with TypeSafe's jev).
