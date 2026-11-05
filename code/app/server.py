#!/usr/bin/env python3
"""Local web app for watching collection progress and browsing board records.

    .venv/bin/python code/app/server.py --port 8791 [--host 127.0.0.1] [--data-dir data]

Serves a single-page UI from code/app/static and a JSON API under api/. Every
URL the page uses is relative, so it works at / and behind a path prefix such
as `tailscale serve --set-path /boards http://127.0.0.1:8791`. Set
APP_PASSWORD to require HTTP Basic auth (any user name).
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent))

from config import DATA  # noqa: E402
from records import FIELDS  # noqa: E402
from store import Store  # noqa: E402

STATIC = APP / "static"
STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
FILTER_KEYS = ("tea_id", "county", "detail", "meeting_type", "platform", "text_status",
               "date_from", "date_to", "q", "name", "sort")
REFRESH_SECONDS = 20
SAMPLE_SECONDS = 60


def inline_script_hashes(html: str) -> list[str]:
    hashes = []
    for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S):
        digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
        hashes.append(f"'sha256-{digest}'")
    return hashes


class Handler(BaseHTTPRequestHandler):
    server_version = "BoardRecords/1"
    store: Store
    password: str | None
    csp: str

    def log_message(self, fmt, *args):
        if os.environ.get("APP_LOG"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------ plumbing
    def _authorized(self) -> bool:
        if not self.password:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            _, _, given = base64.b64decode(header[6:]).decode("utf-8", "replace").partition(":")
        except ValueError:
            return False
        return hmac.compare_digest(given.encode(), self.password.encode())

    def _headers(self, status: int, ctype: str, length: int | None = None, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", self.csp)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self._headers(status, ctype, len(body), extra)
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value, status: int = 200):
        body = json.dumps(value, default=str, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if not self._authorized():
            self._send(401, b"Authentication required.\n", "text/plain; charset=utf-8",
                       {"WWW-Authenticate": 'Basic realm="Board records", charset="UTF-8"'})
            return
        url = urlparse(self.path)
        params = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        try:
            if path.startswith("/api/"):
                route = getattr(self, "api_" + path[5:].replace(".", "_").replace("-", "_"), None)
                if route is None:
                    self._json({"error": "unknown endpoint"}, 404)
                else:
                    route(params)
            else:
                self._static(url.path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # pragma: no cover - surfaced to the client and log
            traceback.print_exc()
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except OSError:
                pass

    def _static(self, raw_path: str):
        name = raw_path.lstrip("/") or "index.html"
        target = (STATIC / name).resolve()
        if STATIC not in target.parents or not target.is_file():
            self._send(404, b"Not found.\n", "text/plain; charset=utf-8")
            return
        ctype = STATIC_TYPES.get(target.suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype, {"Cache-Control": "no-cache"})

    @staticmethod
    def _filters(params: dict) -> dict:
        return {k: params[k] for k in FILTER_KEYS if params.get(k)}

    # ------------------------------------------------------------ API
    def api_health(self, params):
        self._json({"ok": True, "index": self.store.index_state})

    def api_progress(self, params):
        self._json(self.store.progress())

    def api_districts(self, params):
        self._json({"items": self.store.districts()})

    def api_district(self, params):
        result = self.store.district(params.get("tea_id", ""))
        if result is None:
            self._json({"error": "district not found"}, 404)
        else:
            result["coverage"] = self.store.coverage(params["tea_id"])
            self._json(result)

    def api_facets(self, params):
        self._json(self.store.facets())

    def api_fields(self, params):
        self._json({name: {"type": t, "description": d} for name, (t, d) in FIELDS.items()})

    def api_records(self, params):
        try:
            limit = int(params.get("limit", 50))
            offset = int(params.get("offset", 0))
        except ValueError:
            self._json({"error": "limit and offset must be integers"}, 400)
            return
        self._json(self.store.records(self._filters(params), limit, offset))

    def api_record(self, params):
        result = self.store.record(params.get("id", ""))
        if result is None:
            self._json({"error": "record not found"}, 404)
        else:
            self._json(result)

    def api_record_text(self, params):
        text = self.store.record_text(params.get("id", ""))
        if text is None:
            self._send(404, b"No text for this record.\n", "text/plain; charset=utf-8")
        else:
            self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8",
                       {"Content-Disposition": f'inline; filename="{params.get("id", "record")[:16]}.txt"'})

    def api_coverage(self, params):
        rows = self.store.coverage(params.get("tea_id"))
        self._json({"available": rows is not None, "items": rows or []})

    def api_export_csv(self, params):
        rows = self.store.export_rows(self._filters(params))
        self._headers(200, "text/csv; charset=utf-8", None, {
            "Content-Disposition": 'attachment; filename="board-records.csv"',
            "Cache-Control": "no-store", "Connection": "close"})
        if self.command == "HEAD":
            return
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        for i, row in enumerate(rows):
            writer.writerow(["" if v is None else v for v in row])
            if i % 500 == 0 or buffer.tell() > 256_000:
                self.wfile.write(buffer.getvalue().encode("utf-8"))
                buffer.seek(0)
                buffer.truncate()
        self.wfile.write(buffer.getvalue().encode("utf-8"))
        self.close_connection = True


def background(store: Store, stop: threading.Event) -> None:
    last_sample = 0.0
    while not stop.is_set():
        try:
            store.refresh()
            if time.time() - last_sample >= SAMPLE_SECONDS:
                store.sample()
                last_sample = time.time()
        except Exception:  # keep serving; the next pass retries
            traceback.print_exc()
        stop.wait(REFRESH_SECONDS)


def make_server(data_dir: Path, host: str = "127.0.0.1", port: int = 8791,
                password: str | None = None, run_background: bool = True):
    store = Store(Path(data_dir))
    html = (STATIC / "index.html").read_text()
    script_src = " ".join(["'self'", *inline_script_hashes(html)])
    handler = type("BoundHandler", (Handler,), {
        "store": store, "password": password,
        "csp": (f"default-src 'self'; script-src {script_src}; style-src 'self'; img-src 'self' data:; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"),
    })
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    stop = threading.Event()
    if run_background:
        threading.Thread(target=background, args=(store, stop), daemon=True, name="indexer").start()
    httpd.stop_event = stop
    httpd.store = store
    return httpd


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--data-dir", default=str(DATA))
    args = parser.parse_args(argv)
    httpd = make_server(Path(args.data_dir), args.host, args.port, os.environ.get("APP_PASSWORD") or None)
    print(f"Serving on http://{args.host}:{args.port}/ (data: {args.data_dir})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.stop_event.set()
        httpd.server_close()


if __name__ == "__main__":
    main()
