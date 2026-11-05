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
import random
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

from config import HOST_DELAY, MAX_DOWNLOAD_BYTES, MIN_FREE_BYTES, RAW, USER_AGENT


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


class SkippedDownload(Exception):
    """A response deliberately not downloaded; status is a records text_status."""

    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def _read_body(response) -> bytes:
    """Read a streamed body, refusing media and anything over MAX_DOWNLOAD_BYTES.

    Districts link meeting recordings (WAV files of several hundred MB) next to
    their minutes; reading those whole would exhaust a worker's memory.
    """
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type.startswith(("audio/", "video/")):
        response.close()
        raise SkippedDownload("recording", f"media file not downloaded ({content_type})")
    declared = int(response.headers.get("Content-Length") or 0)
    if declared > MAX_DOWNLOAD_BYTES:
        response.close()
        raise SkippedDownload("too_large", f"not downloaded: {declared} bytes")
    chunks, size = [], 0
    for chunk in response.iter_content(1 << 20):
        size += len(chunk)
        if size > MAX_DOWNLOAD_BYTES:
            response.close()
            raise SkippedDownload("too_large", f"not downloaded: over {MAX_DOWNLOAD_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


class Fetcher:
    """Thread-safe downloader with a per-host minimum delay and a disk cache."""

    def __init__(self, raw_root: Path = RAW):
        self.raw_root = raw_root
        raw_root.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_next: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._guard = threading.Lock()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            session = requests.Session()
            session.headers["User-Agent"] = USER_AGENT
            # 429 and 503 are handled in get() so the host's pace can slow down.
            retry = Retry(total=4, backoff_factor=2, status_forcelist=[500, 502, 504],
                          allowed_methods=None)
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
            # Jitter spreads requests out instead of a fixed metronome; the
            # backoff multiplier slows a host that has pushed back.
            pace = delay * self._backoff.get(key, 1.0) * random.uniform(0.7, 1.3)
            self._host_next[key] = start + pace
        if start > now:
            time.sleep(start - now)

    def _slow_down(self, url: str, retry_after: str | None) -> float:
        """Double the host's delay (up to 32x) and return seconds to wait."""
        key, delay = _host_delay(url)
        with self._guard:
            factor = min(self._backoff.get(key, 1.0) * 2, 32.0)
            self._backoff[key] = factor
        print(json.dumps(dict(event="host_backoff", host=key, factor=factor)), flush=True)
        try:
            return max(float(retry_after), delay * factor)
        except (TypeError, ValueError):
            return delay * factor * 4

    def _speed_up(self, url: str) -> None:
        key, _ = _host_delay(url)
        if self._backoff.get(key, 1.0) > 1.0:
            with self._guard:
                self._backoff[key] = max(1.0, self._backoff[key] * 0.99)

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
            cached_headers = {k.lower(): v for k, v in meta["headers"].items()}
            cached_type = cached_headers.get("content-type", "").lower()
            if cached_type.startswith(("audio/", "video/")):
                raise SkippedDownload("recording", f"media file not read ({cached_type})")
            if int(cached_headers.get("content-length") or 0) > MAX_DOWNLOAD_BYTES:
                raise SkippedDownload("too_large", f"not read: {cached_headers['content-length']} bytes")
            content = gzip.decompress(payload_path.read_bytes())
            return Response(meta["url"], meta["status"], meta["headers"], content,
                            rel, meta["sha256"], True)
        while shutil.disk_usage(self.raw_root.parent).free < MIN_FREE_BYTES:
            time.sleep(60)
        for _ in range(6):
            self._wait_turn(url)
            response = self._session().request(method, url, data=data, headers=headers,
                                               timeout=(20, timeout), stream=True)
            if response.status_code not in (429, 503):
                break
            response.close()
            time.sleep(self._slow_down(url, response.headers.get("Retry-After")))
        response.raise_for_status()
        self._speed_up(url)
        content = _read_body(response)
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
