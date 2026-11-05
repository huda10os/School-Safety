"""Polite, cached HTTP downloads shared by every platform scraper.

Every successful response body is stored once under data/raw/<sha[:2]>/<sha>.gz,
keyed by the SHA-256 of the request (method, URL, form body). A rerun reads the
cache instead of the network, which makes collection resumable and lets the
later OCR and review steps reopen the exact bytes that were parsed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import HOST_DELAY, MIN_FREE_BYTES, RAW, USER_AGENT


@dataclass
class Response:
    url: str            # final URL after redirects
    status: int
    headers: dict
    content: bytes
    raw_path: str       # repo-relative path of the cached body
    sha256: str         # hash of the body bytes
    from_cache: bool

    @property
    def text(self) -> str:
        return self.content.decode(_charset(self.headers), errors="replace")

    def json(self):
        return json.loads(self.content)


def _charset(headers: dict) -> str:
    content_type = headers.get("Content-Type", headers.get("content-type", ""))
    for part in content_type.split(";"):
        if part.strip().lower().startswith("charset="):
            return part.split("=", 1)[1].strip() or "utf-8"
    return "utf-8"


def _host_delay(url: str) -> tuple[str, float]:
    """Return (rate-limit key, delay). Each hostname is limited separately; the
    delay comes from the most specific HOST_DELAY suffix, so every Diligent
    tenant subdomain gets its own 3 s slot while go.boarddocs.com, one host for
    all districts, stays a single shared slot."""
    host = urlparse(url).hostname or ""
    for suffix, delay in HOST_DELAY.items():
        if suffix != "default" and (host == suffix or host.endswith("." + suffix)):
            return host, delay
    return host, HOST_DELAY["default"]


class Fetcher:
    """Thread-safe downloader with a per-host minimum delay and a disk cache."""

    def __init__(self, raw_root: Path = RAW):
        self.raw_root = raw_root
        raw_root.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_next: dict[str, float] = {}
        self._guard = threading.Lock()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            retry = Retry(total=4, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504],
                          allowed_methods=None, respect_retry_after_header=True)
            session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
            session.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=4))
            self._local.session = session
        return self._local.session

    def _wait_turn(self, url: str) -> None:
        key, delay = _host_delay(url)
        with self._guard:
            lock = self._host_locks.setdefault(key, threading.Lock())
        with lock:
            now = time.monotonic()
            start = max(now, self._host_next.get(key, 0.0))
            self._host_next[key] = start + delay
        if start > now:
            time.sleep(start - now)

    def _paths(self, key: str) -> tuple[Path, Path]:
        base = self.raw_root / key[:2] / key
        return base.with_suffix(".gz"), base.with_suffix(".json")

    def get(self, url: str, *, method: str = "GET", data: dict | None = None,
            headers: dict | None = None, refresh: bool = False, timeout: int = 90) -> Response:
        """Return the response for url, from cache unless refresh is set.

        Listing pages that change over time (meeting tables) should be fetched
        with refresh=True on update runs; documents are immutable and cached.
        Raises requests.HTTPError for non-2xx responses, which are not cached.
        """
        body = urlencode(sorted(data.items())) if data else ""
        key = hashlib.sha256(f"{method}\n{url}\n{body}".encode()).hexdigest()
        payload_path, meta_path = self._paths(key)
        rel = str(payload_path.relative_to(self.raw_root.parent.parent))
        if not refresh and payload_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            content = gzip.decompress(payload_path.read_bytes())
            return Response(meta["url"], meta["status"], meta["headers"], content,
                            rel, meta["sha256"], True)
        while shutil.disk_usage(self.raw_root.parent).free < MIN_FREE_BYTES:
            time.sleep(60)
        self._wait_turn(url)
        response = self._session().request(method, url, data=data, headers=headers,
                                           timeout=(20, timeout))
        response.raise_for_status()
        content = response.content
        digest = hashlib.sha256(content).hexdigest()
        payload_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = payload_path.with_suffix(".gz.tmp")
        tmp.write_bytes(gzip.compress(content, compresslevel=6))
        tmp.replace(payload_path)
        meta = dict(request_url=url, method=method, form=data or {}, url=response.url,
                    status=response.status_code, headers=dict(response.headers),
                    sha256=digest, fetched_at=datetime.now(timezone.utc).isoformat())
        tmp = meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(meta_path)
        return Response(response.url, response.status_code, dict(response.headers), content,
                        rel, digest, False)

    def post(self, url: str, data: dict, **kwargs) -> Response:
        return self.get(url, method="POST", data=data, **kwargs)
