"""Paths and limits shared by every collection script."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RAW = DATA / "raw"            # gzip-compressed response bodies, never committed
STAGING = DATA / "staging"    # one JSONL file per district and platform
REFERENCE = DATA / "reference"  # small committed inputs: district list, platform map

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

# Minimum seconds between requests to the same host. Hosts not listed use the
# default. BoardDocs and Diligent publish restrictive robots.txt files, so they
# are fetched slowest.
HOST_DELAY = {
    "default": 1.0,
    "meetings.boardbook.org": 0.1,  # ~10 requests/s; slows automatically on 429/503
    "go.boarddocs.com": 3.0,
    "community.diligentoneplatform.com": 3.0,
    "community.highbond.com": 3.0,
    "communitybydiligent.com": 3.0,
}

# Files above this size are cached but not parsed in-process; their record gets
# text_status "too_large" so the OCR/post-processing step can handle them.
MAX_PARSE_BYTES = 60 * 1024 * 1024
# Free-disk floor; downloads pause below it.
MIN_FREE_BYTES = 20 * 1024**3
