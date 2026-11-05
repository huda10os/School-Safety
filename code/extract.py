"""Text extraction from cached PDF, DOCX and plain-text documents.

Runs inside worker processes (see pipeline.py), so it takes a cached file path
rather than bytes and imports parsers lazily. OCR is out of scope here: a PDF
page without a text layer marks the record needs_ocr (no text) or partial_ocr
(some pages empty) for the later OCR step.
"""
from __future__ import annotations

import gzip
import io
import re

from config import MAX_PARSE_BYTES, ROOT

MIN_PAGE_CHARS = 20  # a page with less selectable text than this is treated as scanned


def clean_text(value: str) -> str:
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def _kind(content: bytes, content_type: str, url: str) -> str:
    content_type = (content_type or "").lower()
    path = (url or "").lower().split("?")[0]
    if content.startswith(b"%PDF") or "pdf" in content_type:
        return "pdf"
    if content.startswith(b"PK\x03\x04") and ("word" in content_type or path.endswith(".docx")):
        return "docx"
    if content.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"  # legacy .doc/.xls
    if content_type.startswith("text/plain") or path.endswith(".txt"):
        return "plain"
    return "unknown"


def extract_file(raw_path: str, content_type: str = "", url: str = "") -> dict:
    """Return text, text_status, text_method, page_count for a cached document."""
    result = dict(text=None, text_status="error", text_method=None, page_count=None, error=None)
    try:
        compressed = (ROOT / raw_path).read_bytes()
        content = gzip.decompress(compressed)
        if len(content) > MAX_PARSE_BYTES:
            result.update(text_status="too_large", error=f"{len(content)} bytes")
            return result
        kind = _kind(content, content_type, url)
        if kind == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                reader.decrypt("")
            pages = [clean_text(page.extract_text() or "") for page in reader.pages]
            empty = sum(len(page) < MIN_PAGE_CHARS for page in pages)
            text = "\n\n".join(page for page in pages if page)
            status = "ok" if not empty else ("needs_ocr" if empty == len(pages) else "partial_ocr")
            result.update(text=text or None, text_status=status, text_method="pdf",
                          page_count=len(pages))
        elif kind == "docx":
            from docx import Document
            document = Document(io.BytesIO(content))
            blocks = [p.text for p in document.paragraphs]
            blocks += [" | ".join(c.text for c in row.cells) for t in document.tables for row in t.rows]
            text = clean_text("\n".join(blocks))
            result.update(text=text or None, text_status="ok" if text else "empty", text_method="docx")
        elif kind == "plain":
            text = clean_text(content.decode("utf-8", errors="replace"))
            result.update(text=text or None, text_status="ok" if text else "empty", text_method="plain")
        else:
            result.update(text_status="unsupported", error=f"unsupported document type ({kind}, {content_type})")
    except Exception as exc:  # one bad file must not stop a batch
        result.update(text_status="error", error=f"{type(exc).__name__}: {exc}"[:500])
    return result
