#!/usr/bin/env python3
"""Run collection: discover listings, then resolve and extract every document.

    python code/pipeline.py discover  [--tea-ids 031901,057905] [--platform boardbook] [--refresh]
    python code/pipeline.py documents [--tea-ids ...] [--platform ...]
    python code/pipeline.py all       [...]

Inputs: data/reference/platforms.csv (tea_id, platform, entry_url, evidence)
and data/reference/districts.csv. Outputs are per district and platform:
    data/staging/listings/<tea_id>__<platform>.jsonl   listing records
    data/staging/listings/<tea_id>__<platform>.status.json
    data/staging/records/<tea_id>__<platform>.jsonl    listing + document fields

Both stages are resumable. Documents already in a records file are skipped,
so an update run (discover --refresh, then documents) only downloads new links.
Memory stays bounded: work is submitted in a sliding window, PDF parsing runs
in a small process pool whose workers are recycled, and each record is
appended to disk as soon as it is finished.
"""
from __future__ import annotations

import argparse
import csv
import importlib
import json
import sys
import threading
import traceback
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timezone
from pathlib import Path

from config import REFERENCE, STAGING
from extract import extract_file
from fetch import Fetcher
from records import append_jsonl, read_jsonl, write_jsonl

LISTINGS = STAGING / "listings"
RECORDS = STAGING / "records"


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="") as handle:
        return list(csv.DictReader(handle))


def platform_module(name: str):
    return importlib.import_module(f"platforms.{name}")


def jobs(args) -> list[tuple[dict, dict]]:
    districts = {d["tea_id"]: d for d in load_csv(REFERENCE / "districts.csv")}
    rows = load_csv(Path(args.platforms))
    # Hand-verified sources for districts the automatic map missed or got wrong.
    overrides = REFERENCE / "platform_overrides.csv"
    if overrides.exists() and Path(args.platforms) == REFERENCE / "platforms.csv":
        rows += load_csv(overrides)
    wanted = set(args.tea_ids.split(",")) if args.tea_ids else None
    # A district can list documents on several pages of one platform (e.g. a
    # current-minutes page and an archive page); they share one staging file.
    grouped: dict[tuple[str, str], dict] = {}
    for row in rows:
        if not row.get("entry_url") or row["platform"] in ("", "none", "unknown"):
            continue
        if wanted and row["tea_id"] not in wanted:
            continue
        if args.platform and row["platform"] != args.platform:
            continue
        key = (row["tea_id"], row["platform"])
        job = grouped.setdefault(key, {**row, "entry_urls": []})
        if row["entry_url"] not in job["entry_urls"]:
            job["entry_urls"].append(row["entry_url"])
    return [(districts[tea_id], job) for (tea_id, _), job in grouped.items()]


def stem(district: dict, row: dict) -> str:
    return f"{district['tea_id']}__{row['platform']}"


def discover_one(fetcher: Fetcher, district: dict, row: dict, refresh: bool) -> dict:
    target = LISTINGS / f"{stem(district, row)}.jsonl"
    status_path = target.with_suffix(".status.json")
    if target.exists() and status_path.exists() and not refresh:
        return json.loads(status_path.read_text())
    status = dict(tea_id=district["tea_id"], platform=row["platform"], entry_url=row["entry_url"],
                  entry_urls=row["entry_urls"], discovered_at=datetime.now(timezone.utc).isoformat(),
                  records=0, error=None)
    try:
        module = platform_module(row["platform"])
        records = [r for url in row["entry_urls"] for r in module.discover(district, url, fetcher)]
        seen, unique = set(), []
        for record in records:
            if record["record_id"] not in seen:
                seen.add(record["record_id"])
                unique.append(record)
        write_jsonl(target, unique)
        status["records"] = len(unique)
    except Exception as exc:
        status["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        status["traceback"] = traceback.format_exc()[-3000:]
    status_path.write_text(json.dumps(status, indent=2))
    return status


def run_discover(args) -> None:
    fetcher = Fetcher()
    selected = jobs(args)
    print(json.dumps(dict(stage="discover", jobs=len(selected))), flush=True)
    done = errors = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(discover_one, fetcher, d, r, args.refresh) for d, r in selected]
        for future in futures:
            status = future.result()
            done += 1
            errors += bool(status.get("error"))
            if status.get("error"):
                print(json.dumps(dict(tea_id=status["tea_id"], platform=status["platform"],
                                      error=status["error"][:200])), flush=True)
            if done % 25 == 0 or done == len(selected):
                print(json.dumps(dict(stage="discover", done=done, errors=errors)), flush=True)


class ParserPool:
    """Process pool that replaces itself if a worker dies.

    A worker killed mid-parse (for example by the kernel on a pathological
    PDF) breaks a ProcessPoolExecutor for every later task; without this an
    overnight run would mark all remaining documents as errors.
    """

    def __init__(self, size: int):
        self.size = size
        self.lock = threading.Lock()
        self.pool = self._new()

    def _new(self):
        # max_tasks_per_child recycles workers so one large document cannot
        # leave a process holding its memory for the rest of the run.
        return ProcessPoolExecutor(max_workers=self.size, max_tasks_per_child=40)

    def parse(self, *args) -> dict:
        pool = self.pool
        try:
            return pool.submit(extract_file, *args).result(timeout=600)
        except BrokenProcessPool:
            with self.lock:
                if self.pool is pool:
                    self.pool = self._new()
            raise RuntimeError("parser process died on this document")

    def shutdown(self):
        self.pool.shutdown(cancel_futures=True)


def document_one(fetcher: Fetcher, processes: ParserPool, record: dict) -> dict:
    result = {**record, "collected_at": datetime.now(timezone.utc).isoformat(), "error": None}
    try:
        resolved = platform_module(record["platform"]).resolve(record, fetcher)
        result.update(resolved)
        if result.get("text") is None and result.get("raw_path") and not resolved.get("text_status"):
            parsed = processes.parse(result["raw_path"], result.get("content_type") or "",
                                     result.get("document_link") or record["source_link"])
            result.update(parsed)
        if result.get("text_status") is None:
            result["text_status"] = "ok" if result.get("text") else "empty"
    except Exception as exc:
        result.update(text=None, text_status="error", error=f"{type(exc).__name__}: {exc}"[:1000])
    text = result.get("text")
    result["char_count"] = len(text) if text else 0
    result.setdefault("attachment_links", [])
    return result


DETAIL_ORDER = ("Minutes", "Agenda", "Projector", "Extras", "Other")


def pending_records(selected):
    """Yield (records_path, record) for listing records not yet collected.

    Statewide collection takes days, so documents are collected in passes by
    detail type: every district's Minutes first, then Agendas, then the rest.
    An interrupted run therefore holds the most useful documents first.
    """
    for detail in DETAIL_ORDER:
        for district, row in selected:
            listing = LISTINGS / f"{stem(district, row)}.jsonl"
            target = RECORDS / f"{stem(district, row)}.jsonl"
            done = None
            for record in read_jsonl(listing):
                if record.get("detail") != detail:
                    continue
                if done is None:
                    done = {r["record_id"] for r in read_jsonl(target) if r.get("text_status") != "error"}
                if record["record_id"] not in done:
                    yield target, record


def run_documents(args) -> None:
    fetcher = Fetcher()
    selected = jobs(args)
    lock = threading.Lock()
    counts: dict[str, int] = {}
    total = 0
    print(json.dumps(dict(stage="documents", districts=len(selected))), flush=True)
    processes = ParserPool(args.processes)
    with ThreadPoolExecutor(max_workers=args.workers) as threads:
        todo = pending_records(selected)
        inflight = {}

        def refill():
            while len(inflight) < args.workers * 2:
                try:
                    target, record = next(todo)
                except StopIteration:
                    return
                inflight[threads.submit(document_one, fetcher, processes, record)] = target

        refill()
        while inflight:
            finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
            for future in finished:
                target = inflight.pop(future)
                result = future.result()
                with lock:
                    append_jsonl(target, result)
                counts[result["text_status"]] = counts.get(result["text_status"], 0) + 1
                total += 1
                if total % 200 == 0:
                    print(json.dumps(dict(stage="documents", done=total, **counts)), flush=True)
            refill()
    processes.shutdown()
    print(json.dumps(dict(stage="documents", done=total, **counts)), flush=True)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("stage", choices=["discover", "documents", "build", "all"])
    parser.add_argument("--platforms", default=str(REFERENCE / "platforms.csv"))
    parser.add_argument("--tea-ids", help="comma-separated TEA ids to limit the run")
    parser.add_argument("--platform", help="limit to one platform")
    parser.add_argument("--refresh", action="store_true", help="re-read listings (update run)")
    parser.add_argument("--workers", type=int, default=24, help="network threads (hosts are rate-limited)")
    parser.add_argument("--processes", type=int, default=6, help="PDF/DOCX parser processes")
    args = parser.parse_args(argv)
    if args.stage in ("discover", "all"):
        run_discover(args)
    if args.stage in ("documents", "all"):
        run_documents(args)
    if args.stage in ("build", "all"):
        from build_dataset import build
        print(json.dumps(dict(stage="build", **build())), flush=True)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
