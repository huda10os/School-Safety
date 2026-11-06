#!/usr/bin/env bash
# Reproduce the statewide collection with the settings used for the published run.
#
#   code/run_statewide.sh            # every stage, platforms in parallel
#   code/run_statewide.sh update     # re-read listings, collect only new documents
#
# Each platform runs as its own process because they hit different servers and
# have different memory profiles (measured 2026-11-05 on a 16-thread, 59 GB
# machine): district-website discovery runs a crawler and browser sessions and
# needs few threads; BoardBook is one host at ~10 requests/s. Run from the repo
# root. Requires .venv (code/requirements.txt), Docker for OCR, and
# agent-browser for bot-protected sites. Logs go to data/logs/.
set -euo pipefail
cd "$(dirname "$0")"
PY=../.venv/bin/python
mkdir -p ../data/logs
refresh=""
[ "${1:-}" = "update" ] && refresh="--refresh"

$PY registry.py
[ -f ../data/reference/platforms.csv ] || { $PY match_boardbook.py; $PY detect_platform.py --workers 16; }

run() {  # platform, discover threads, document threads, parser processes
  $PY -u pipeline.py discover --platform "$1" $refresh --workers "$2" >> "../data/logs/$1.log" 2>&1
  $PY -u pipeline.py documents --platform "$1" --workers "$3" --processes "$4" >> "../data/logs/$1.log" 2>&1
}
run boardbook 24 24 4 &
run website 6 16 4 &      # website discovery above ~6 threads exceeded 10 GB
run diligent 18 24 2 &
run boarddocs 3 4 1 &
run legistar 2 4 1 &
wait

$PY ocr.py --cpus 6 --memory 4g          # scanned pages; ~5-6 h at 6 cores
$PY build_dataset.py                      # data/records.parquet, data/coverage.parquet
$PY measures/run.py --skip-jev            # meetings, passages, rule flags (add jev with a TypeSafe key)
