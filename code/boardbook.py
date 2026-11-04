"""Read-only BoardBook Premier discovery and record extraction.

One discovered record corresponds to one link in an organization's Details
column. The organization response contains the complete public meeting table;
the apparent 500-element limit in browser accessibility snapshots is not a
server-side pagination limit. No date or meeting-type filter is imposed here.
PDFs rendered in BoardBook's custom minutes viewer are downloaded and parsed,
not replaced with the viewer's short title. Image-only pages undergo local OCR;
OCR usage and any pages still lacking text remain explicitly flagged.
"""
from __future__ import annotations

import hashlib
import io
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup


def clean_text(value: str) -> str:
    """Collapse insignificant HTML whitespace while preserving paragraph breaks."""
    return "\n".join(
        line for line in (re.sub(r"\s+", " ", x).strip() for x in value.splitlines()) if line
    )


def _get(session, url, fetch=None):
    response = fetch(url) if fetch is not None else session.get(url, timeout=90)
    response.raise_for_status()
    return response


def discover_meetings(session, organization_url: str, district: str, county: str,
                      fetch=None) -> list[dict]:
    """Return every Details link, with fields copied from its owning table row."""
    response = _get(session, organization_url, fetch)
    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.select_one("#PublicMeetingsTable")
    if table is None:
        if "No Public Meetings Exist" in soup.get_text(" ", strip=True):
            return []
        raise ValueError(f"Expected public meeting table missing: {response.url}")
    organization_input = soup.select_one("#organizationName")
    heading = soup.find("h1")
    boardbook_name = (organization_input.get("value", "") if organization_input else
                     re.sub(r"\s+Public View$", "", heading.get_text(" ", strip=True)) if heading else "")
    records = []
    seen = set()
    # Meeting descriptions may contain their own HTML tables. Descendant CSS
    # selection would accidentally parse those rows as separate meetings.
    bodies = table.find_all("tbody", recursive=False)
    meeting_rows = [row for body in bodies for row in body.find_all("tr", recursive=False)]
    if not bodies:
        meeting_rows = table.find_all("tr", recursive=False)
    for row in meeting_rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) < 3:
            continue
        header = cells[0].find("div", recursive=False)
        date_raw = header.get_text(" ", strip=True) if header else cells[0].get_text(" ", strip=True)
        match = re.search(r"([A-Za-z]+\s+\d{1,2},\s+\d{4})\b", date_raw)
        if not match:
            if not cells[2].select("a[href]"):
                continue
            raise ValueError(f"Cannot parse meeting date {date_raw!r} at {response.url}")
        meeting_date = datetime.strptime(match.group(1), "%B %d, %Y").date().isoformat()
        type_label = cells[0].find(["b", "strong"], string=re.compile(r"Meeting Type:"))
        meeting_type = ""
        if type_label:
            meeting_type = type_label.parent.get_text(" ", strip=True).split("Meeting Type:", 1)[-1].strip()
        title = date_raw.split(" - ", 1)[-1] if " - " in date_raw else date_raw
        for link in cells[2].select("a[href]"):
            href = link.get("href", "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            source = urljoin(response.url, href)
            detail = link.get_text(" ", strip=True)
            key = (source, detail)
            if key in seen:
                continue
            seen.add(key)
            query = parse_qs(urlparse(source).query)
            record = {
                "school_district": district, "county_name": county,
                "date": meeting_date, "date_raw": date_raw,
                "meeting_title": title, "meeting_type": meeting_type,
                "cancelled": "cancelled" in date_raw.lower(),
                "meeting_row_text": row.get_text(" ", strip=True),
                "detail": detail, "source_link": source,
                "organization_url": organization_url, "listing_url": response.url,
                "boardbook_name": boardbook_name,
                "meeting_id": query.get("meeting", [""])[0],
                "reviewed": False,
            }
            record["record_id"] = hashlib.sha256(
                (district + "|" + county + "|" + source + "|" + detail).encode()
            ).hexdigest()
            records.append(record)
    return records


def _binary_text(content: bytes, content_type: str, url: str) -> tuple[str, str]:
    content_type = content_type.lower()
    if content.startswith(b"%PDF") or "application/pdf" in content_type:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = clean_text("\n\n".join(pages))
        if not text or any(not p.strip() for p in pages):
            # Import lazily: HTML/selectable-PDF collection does not initialize
            # an OCR engine. The local content cache avoids repeated OCR.
            from ocr import extract_pdf
            extracted = extract_pdf(content)
            text = clean_text(extracted["text"])
            if extracted["ocr_performed"]:
                return text, "pdf_ocr_partial" if extracted["empty_pages"] else "pdf_ocr"
            return text, "pdf_partial_ocr_required" if text else "pdf_ocr_required"
        return text, "pdf"
    if content.startswith(b"PK\x03\x04") and ("word" in content_type or url.lower().split("?")[0].endswith(".docx")):
        from docx import Document
        document = Document(io.BytesIO(content))
        blocks = [p.text for p in document.paragraphs]
        blocks += [" | ".join(cell.text for cell in row.cells) for table in document.tables for row in table.rows]
        return clean_text("\n".join(blocks)), "docx"
    if "application/msword" in content_type or url.lower().split("?")[0].endswith(".doc"):
        raise ValueError("Legacy .doc file requires a dedicated converter; not decoded as HTML")
    if content_type.startswith("text/plain"):
        return clean_text(content.decode("utf-8", errors="replace")), "plain_text"
    return "", "html"


def _viewer_download(soup, html: str, url: str) -> str | None:
    """Resolve document ID using the exact download template exposed by the page."""
    identifier = soup.select_one("#NewDocumentViewerDocumentID")
    template = re.search(r"loadUrlFormat\s*=\s*['\"]([^'\"]+)['\"]", html)
    if identifier and template and identifier.get("value"):
        return urljoin(url, template.group(1).replace("TBR_AAAA_", identifier["value"]))
    for embed in soup.select("iframe[src], embed[src], object[data]"):
        source = embed.get("src") or embed.get("data")
        if source and ("pdf" in source.lower() or "/Documents/" in source):
            return urljoin(url, source)
    return None


def _status_for(method: str, text: str) -> str:
    if method in ("pdf_partial_ocr_required", "pdf_ocr_partial"):
        return "partial_ocr_required" if method == "pdf_partial_ocr_required" else "partial_ocr_empty_pages"
    return "ok" if text else "ocr_required"


def extract_record(session, url: str, fetch=None) -> dict:
    """Extract the linked record, following a custom viewer to its actual PDF.

    Agenda attachment links are recorded as provenance, not recursively included
    in an agenda's text. Extras attachment contents are included because those
    attachments constitute the Extras record. No fallback substitutes an agenda
    for a broken minutes URL.
    """
    result = {"record_text": "", "text": "", "source_link": url,
              "resolved_source_link": url, "text_source_link": url,
              "content_type": "", "attachments": [],
              "extraction_status": "error", "extraction_error": ""}
    try:
        response = _get(session, url, fetch)
        result["resolved_source_link"] = response.url
        result["content_type"] = response.headers.get("Content-Type", "")
        text, method = _binary_text(response.content, result["content_type"], response.url)
        if method != "html":
            result.update(record_text=text, text=text, extraction_method=method,
                          ocr_performed=method.startswith("pdf_ocr") and method != "pdf_ocr_required",
                          extraction_status=_status_for(method, text))
            return result
        html = response.text
        soup = BeautifulSoup(response.content, "html.parser")
        # Authentication/error pages must never be mistaken for meeting text.
        if "/Account/" in response.url or "/LogOn" in response.url or soup.select_one("input[type=password]"):
            raise ValueError("Record redirected to authentication page")
        viewer_url = _viewer_download(soup, html, response.url)
        if viewer_url:
            document = _get(session, viewer_url, fetch)
            result["text_source_link"] = document.url
            result["document_content_type"] = document.headers.get("Content-Type", "")
            text, method = _binary_text(document.content, document.headers.get("Content-Type", ""), document.url)
            if method == "html":
                raise ValueError("Document viewer download did not return a PDF/document")
            result.update(record_text=text, text=text, text_source_link=document.url,
                          document_content_type=document.headers.get("Content-Type", ""),
                          extraction_method=method,
                          ocr_performed=method.startswith("pdf_ocr") and method != "pdf_ocr_required",
                          extraction_status=_status_for(method, text))
            result["attachments"] = [{"title": "Embedded record document", "source_link": document.url}]
            return result
        content = soup.select_one("#Tab-AgendaItem") or soup.select_one("#MainPage") or soup.find("main")
        if content is None:
            raise ValueError("Record main content container not found")
        attachments = []
        attachment_seen = set()
        for a in content.select("a[href]"):
            source = urljoin(response.url, a["href"])
            if any(token in source for token in ("/Documents/", ".pdf", ".docx", ".doc")) and source not in attachment_seen:
                attachments.append({"title": a.get_text(" ", strip=True), "source_link": source})
                attachment_seen.add(source)
        result["attachments"] = attachments
        for element in content.select("script, style, button, input, select, nav, .back-to-top, .ShowOnOffline, #reloadMeetingButton, #agenda-tableofcontents-container, #Tab-WebViewer"):
            element.decompose()
        # Include the date header once for agenda pages; projector body has a
        # separate table of contents deliberately excluded by #Tab-AgendaItem.
        heading = soup.select_one("#MeetingHeader")
        text = clean_text(content.get_text("\n", strip=True))
        if heading and heading not in content.descendants:
            text = clean_text(heading.get_text(" ", strip=True) + "\n" + text)
        if "/Meeting/Supplementals/" in response.url:
            pieces = [text] if text else []
            for attachment in attachments:
                nested = extract_record(session, attachment["source_link"], fetch)
                if nested["extraction_status"] == "ok":
                    pieces.append(attachment["title"] + "\n" + nested["record_text"])
                else:
                    result["extraction_error"] += f"Attachment {attachment['source_link']}: {nested['extraction_status']}; "
            text = clean_text("\n\n".join(pieces))
        result.update(record_text=text, text=text, extraction_method="html",
                      extraction_status="ok" if text else "empty_record")
        if result["extraction_error"] and text:
            result["extraction_status"] = "partial"
    except Exception as exc:
        result["extraction_error"] = f"{type(exc).__name__}: {exc}"
    return result
