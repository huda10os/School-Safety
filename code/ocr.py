"""Cached local PDF OCR; no external OCR uploads. OCR still needs visual review.

macOS uses built-in Vision/PDFKit via adjacent Swift source. Other systems can
install optional pymupdf and rapidocr_onnxruntime for local OCR.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import tempfile
import threading
from pathlib import Path

_ENGINE = None
_LOCK = threading.RLock()
OCR_VERSION = "apple-vision-200dpi-v1" if platform.system() == "Darwin" else "rapidocr-200dpi-v1"


def _macos_ocr(content: bytes, indices: list[int], cache: Path) -> dict[int, dict]:
    source = Path(__file__).with_name("ocr_macos.swift")
    tools = cache.parent / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    binary = tools / "ocr-macos"
    if not binary.exists() or binary.stat().st_mtime < source.stat().st_mtime:
        module_cache = Path(tempfile.gettempdir()) / "school-safety-swift-cache"
        subprocess.run(["/usr/bin/swiftc", "-O", "-module-cache-path", str(module_cache),
                        str(source), "-o", str(binary)], check=True, capture_output=True)
    with tempfile.NamedTemporaryFile(suffix=".pdf") as pdf:
        pdf.write(content)
        pdf.flush()
        completed = subprocess.run([str(binary.resolve()), pdf.name, ",".join(map(str, indices))],
                                   check=True, capture_output=True, timeout=1800)
    return {p["page"] - 1: p for p in json.loads(completed.stdout)}


def _rapidocr(content: bytes, indices: list[int]) -> dict[int, dict]:
    global _ENGINE
    import pymupdf
    import numpy as np
    from rapidocr_onnxruntime import RapidOCR
    if _ENGINE is None:
        _ENGINE = RapidOCR(intra_op_num_threads=1, inter_op_num_threads=1,
                           det_limit_side_len=2048, text_score=0.3)
    document = pymupdf.open(stream=content, filetype="pdf")
    results = {}
    try:
        for index in indices:
            image = document[index].get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
            array = np.frombuffer(image.samples, dtype=np.uint8).reshape(image.height, image.width, 3)
            recognized, _ = _ENGINE(array)
            recognized = recognized or []
            scores = [float(line[2]) for line in recognized]
            results[index] = {"page": index+1, "text": "\n".join(line[1] for line in recognized),
                              "mean_confidence": sum(scores)/len(scores) if scores else None,
                              "minimum_confidence": min(scores) if scores else None}
    finally:
        document.close()
    return results


def extract_pdf(content: bytes, cache_dir: str | Path | None = None) -> dict:
    """Keep embedded text, OCR image-only pages, preserve original page order.

    Per-page method/confidence and empty OCR pages are retained in cache.
    SCHOOL_SAFETY_OCR=0 disables OCR without poisoning the enabled cache.
    """
    from pypdf import PdfReader
    cache = Path(cache_dir or os.environ.get("SCHOOL_SAFETY_OCR_CACHE", "data/ocr"))
    fingerprint = hashlib.sha256(content).hexdigest()
    destination = cache / f"{fingerprint}-{OCR_VERSION}.json"
    with _LOCK:
        if destination.exists():
            return json.loads(destination.read_text())
        reader = PdfReader(io.BytesIO(content))
        embedded = [page.extract_text() or "" for page in reader.pages]
        indices = [i for i, text in enumerate(embedded) if not text.strip()]
        enabled = os.environ.get("SCHOOL_SAFETY_OCR", "1") != "0"
        recognized = {}
        if indices and enabled:
            recognized = (_macos_ocr(content, indices, cache) if platform.system() == "Darwin"
                          else _rapidocr(content, indices))
        pages = []
        for i, text in enumerate(embedded):
            if i in recognized:
                pages.append({**recognized[i], "method": "ocr"})
            else:
                pages.append({"page": i+1, "text": text,
                              "method": "embedded" if text.strip() else "ocr_disabled",
                              "mean_confidence": None, "minimum_confidence": None})
        result = {"text": "\n\n".join(p["text"] for p in pages), "pages": pages,
                  "page_count": len(pages), "ocr_performed": bool(recognized),
                  "empty_pages": [p["page"] for p in pages if not p["text"].strip()],
                  "content_sha256": fingerprint, "ocr_version": OCR_VERSION,
                  "runtime_platform": platform.platform(),
                  "recognition_languages": ["en-US"] if platform.system() == "Darwin" else ["model_default"]}
        if enabled:
            cache.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_suffix(f".{os.getpid()}.tmp")
            temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2))
            temporary.replace(destination)
        return result
