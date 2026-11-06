#!/usr/bin/env python3
"""OCR scanned PDFs with Indra OCR in a resource-capped container.

    python code/ocr.py [--cpus 6] [--memory 4g] [--batch 200] [--limit N]

Work list: records whose text_status is needs_ocr or partial_ocr and whose
cached body is a PDF, minutes first, then agendas, then the rest; identical
files (same content_sha256) are processed once. For a needs_ocr PDF every page
is OCR'd; for partial_ocr only pages whose text layer has fewer than 20
characters (extract.MIN_PAGE_CHARS). Pages that already have text keep it.

Output, one file per document, read by build_dataset.py:
    data/staging/ocr/<sha[:2]>/<sha>.json
        {sha, text (all pages, original order), ocr_pages, pages_total,
         mean_conf, model, ocr_at}
Progress for the web app: data/staging/ocr/progress.json.

Resource limits: the container gets a hard CPU cap (--cpus), a memory cap,
and a low CPU share so other work on the machine wins under contention. The
run is resumable: documents with an output file are skipped.

Requires Docker and the image built from code/ocr/Dockerfile
(docker build -t txboards-indra:1.1.2 code/ocr). Model weights are read from
data/cache/huggingface (downloaded on first run when that folder is empty).
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.compute as pc
import pyarrow.parquet as pq

from config import DATA, ROOT
from extract import MIN_PAGE_CHARS, clean_text

IMAGE = "txboards-indra:1.1.2"
MODEL = "indra-ocr==1.1.2; models datalab-to/indra-ocr bd02167f"
OUT = DATA / "staging" / "ocr"
WORK = DATA / "ocr_work"
ORDER = {"Minutes": 0, "Agenda": 1}


def sidecar(sha: str) -> Path:
    return OUT / sha[:2] / f"{sha}.json"


def worklist() -> list[dict]:
    table = pq.read_table(DATA / "records.parquet",
                          columns=["text_status", "raw_path", "content_sha256", "detail", "content_type"])
    table = table.filter(pc.is_in(table["text_status"], value_set=__import__("pyarrow").array(["needs_ocr", "partial_ocr"])))
    seen, jobs = set(), []
    for row in table.to_pylist():
        sha = row["content_sha256"]
        if not sha or not row["raw_path"] or sha in seen:
            continue
        seen.add(sha)
        jobs.append(dict(sha=sha, raw_path=row["raw_path"], status=row["text_status"], detail=row["detail"]))
    jobs.sort(key=lambda j: (ORDER.get(j["detail"], 2), j["sha"]))
    return jobs


def prepare(job: dict) -> dict | None:
    """Write a PDF holding only the pages to OCR; return its page map, or None."""
    from pypdf import PdfReader, PdfWriter
    try:
        content = gzip.decompress((ROOT / job["raw_path"]).read_bytes())
    except (OSError, EOFError):
        return None
    if not content.startswith(b"%PDF"):
        return None  # HTML Extras bundles and other non-PDF bodies are out of scope here
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            reader.decrypt("")
        texts = [clean_text(page.extract_text() or "") for page in reader.pages]
        targets = [i for i, t in enumerate(texts) if len(t) < MIN_PAGE_CHARS]
        if not targets:
            return None
        writer = PdfWriter()
        for i in targets:
            writer.add_page(reader.pages[i])
        path = WORK / "in" / f"{job['sha']}.pdf"
        with open(path, "wb") as handle:
            writer.write(handle)
    except Exception as exc:  # a malformed PDF is recorded, not fatal
        return dict(job, error=f"{type(exc).__name__}: {exc}"[:300], texts=[], targets=[])
    return dict(job, texts=texts, targets=targets, path=path)


def run_indra(paths: list[Path], cpus: str, memory: str) -> None:
    out = WORK / "out"
    cmd = ["docker", "run", "--rm", "--network", "none", "--user", f"{os.getuid()}:{os.getgid()}",
           f"--cpus={cpus}", f"--memory={memory}", f"--memory-swap={memory}", "--cpu-shares=256",
           "-e", "HF_HUB_OFFLINE=1", "-e", "HF_HOME=/cache/huggingface", "-e", "TMPDIR=/work/tmp",
           "-v", f"{DATA / 'cache'}:/cache:ro", "-v", f"{WORK}:/work", IMAGE,
           "indra-ocr", *[f"/work/in/{p.name}" for p in paths], "--out", "/work/out",
           "--mode", "ocr", "--format", "json", "--keep-furniture",
           "--cores", str(max(1, int(float(cpus)))), "--dpi", "200"]
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=7200)


def collect(prepared: dict) -> dict:
    """Merge OCR'd pages into the document text and write its sidecar."""
    result = dict(sha=prepared["sha"], model=MODEL, ocr_at=datetime.now(timezone.utc).isoformat(),
                  pages_total=len(prepared["texts"]), ocr_pages=0, mean_conf=None, text=None,
                  error=prepared.get("error"))
    texts = list(prepared["texts"])
    out_file = WORK / "out" / f"{prepared['sha']}.jsonl"
    confs = []
    if out_file.exists():
        for line in out_file.read_text().splitlines():
            page = json.loads(line)
            index = page.get("page")
            if index is None or index >= len(prepared["targets"]):
                continue
            lines = page.get("lines") or []
            text = clean_text("\n".join(l.get("text", "") for l in lines))
            confs += [l["conf"] for l in lines if "conf" in l]
            texts[prepared["targets"][index]] = text
            result["ocr_pages"] += 1
        out_file.unlink()
    elif not result["error"]:
        result["error"] = "OCR produced no output"
    merged = "\n\n".join(t for t in texts if t)
    result.update(text=merged or None, mean_conf=round(sum(confs) / len(confs), 4) if confs else None)
    target = sidecar(prepared["sha"])
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(result, ensure_ascii=False))
    tmp.replace(target)
    if prepared.get("path"):
        Path(prepared["path"]).unlink(missing_ok=True)
    return result


def write_progress(state: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = OUT / "progress.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(OUT / "progress.json")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cpus", default="6", help="hard CPU cap for the OCR container")
    parser.add_argument("--memory", default="4g", help="memory cap for the OCR container")
    parser.add_argument("--batch", type=int, default=200, help="PDFs per container run")
    parser.add_argument("--prep-workers", type=int, default=2, help="threads preparing page subsets")
    parser.add_argument("--limit", type=int, help="stop after this many documents (testing)")
    args = parser.parse_args()
    if not shutil.which("docker"):
        sys.exit("docker is required")
    for sub in ("in", "out", "tmp"):
        (WORK / sub).mkdir(parents=True, exist_ok=True)
    jobs = worklist()
    todo = [j for j in jobs if not sidecar(j["sha"]).exists()]
    if args.limit:
        todo = todo[:args.limit]
    state = dict(started_at=datetime.now(timezone.utc).isoformat(), model=MODEL,
                 cpus=args.cpus, documents_total=len(jobs), documents_done=len(jobs) - len(todo),
                 documents_this_run=len(todo), ocr_pages_done=0, errors=0,
                 phase="running", current_detail=None, updated_at=None)
    write_progress(state)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.prep_workers) as prep:
        for start in range(0, len(todo), args.batch):
            chunk = todo[start:start + args.batch]
            prepared = [p for p in prep.map(prepare, chunk)]
            runnable = [p for p in prepared if p and p.get("path")]
            if runnable:
                run_indra([p["path"] for p in runnable], args.cpus, args.memory)
            for job, p in zip(chunk, prepared):
                if p is None:  # nothing to OCR (not a PDF, or every page already has text)
                    p = dict(job, texts=[], targets=[], error="no OCR target pages")
                r = collect(p)
                state["documents_done"] += 1
                state["ocr_pages_done"] += r["ocr_pages"]
                state["errors"] += bool(r["error"])
            elapsed = time.monotonic() - started
            done_run = min(start + args.batch, len(todo))
            state.update(current_detail=chunk[-1]["detail"], elapsed_seconds=round(elapsed),
                         docs_per_minute=round(60 * done_run / elapsed, 1) if elapsed else None,
                         pages_per_second=round(state["ocr_pages_done"] / elapsed, 2) if elapsed else None,
                         updated_at=datetime.now(timezone.utc).isoformat())
            remaining = len(todo) - done_run
            state["eta_minutes"] = round(remaining / (done_run / elapsed) / 60, 1) if done_run and elapsed else None
            write_progress(state)
            print(json.dumps({k: state[k] for k in ("documents_done", "ocr_pages_done", "errors",
                                                    "pages_per_second", "eta_minutes")}), flush=True)
    state.update(phase="finished", updated_at=datetime.now(timezone.utc).isoformat())
    write_progress(state)


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
