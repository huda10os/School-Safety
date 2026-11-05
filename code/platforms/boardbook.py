"""BoardBook Premier public meeting listings and documents."""
from __future__ import annotations

import html
import re
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from extract import clean_text, extract_file
from records import DOCUMENT_FIELDS, make_record

PLATFORM = "boardbook"


def _visible(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _detail(label: str) -> str:
    value = label.lower()
    if "supplement" in value or "extra" in value:
        return "Extras"
    for detail in ("Minutes", "Projector", "Agenda"):
        if detail.lower() in value:
            return detail
    return "Other"


def discover(district: dict, entry_url: str, fetcher) -> list[dict]:
    """Read every document link from the complete public meeting table."""
    response = fetcher.get(entry_url, refresh=True)
    soup = BeautifulSoup(response.text, "lxml")
    table = soup.select_one("#PublicMeetingsTable")
    if table is None:
        raise ValueError("BoardBook public meeting table missing")
    body = table.find("tbody", recursive=False)
    records = []
    # Meeting descriptions may contain tables with their own rows and links.
    for row in (body or table).find_all("tr", recursive=False):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        header = cells[0].find("div", recursive=False) or cells[0]
        title = _visible(header)
        match = re.search(r"\b([A-Za-z]+ \d{1,2}, \d{4})\b", title)
        date_raw = match.group(1) if match else None
        date = None
        if date_raw:
            try:
                date = datetime.strptime(date_raw, "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
        type_label = cells[0].find(string=re.compile(r"^\s*Meeting Type:\s*$"))
        meeting_type = None
        if type_label:
            container = type_label.parent
            if container.name in ("b", "strong", "span"):
                container = container.parent
            meeting_type = _visible(container).partition("Meeting Type:")[2].strip() or None
        for link in cells[-1].select("a[href]"):
            source = urljoin(response.url, link["href"])
            if urlparse(source).scheme not in ("http", "https"):
                continue
            label = _visible(link)
            records.append(make_record(
                district, PLATFORM, date=date, date_raw=date_raw,
                meeting_type=meeting_type, meeting_title=title,
                meeting_id=parse_qs(urlparse(source).query).get("meeting", [None])[0],
                detail=_detail(label), detail_raw=label, source_link=source,
                listing_url=entry_url, listing_row_text=_visible(row)[:2000],
            ))
    return records


def _fields(response) -> dict:
    result = dict.fromkeys(DOCUMENT_FIELDS)
    result.update(document_link=response.url, raw_path=response.raw_path,
                  content_sha256=response.sha256,
                  content_type=response.headers.get("Content-Type", response.headers.get("content-type", "")),
                  attachment_links=[])
    return result


def _is_file(response) -> bool:
    return response.content.startswith((b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0"))


def _page(response):
    soup = BeautifulSoup(response.text, "lxml")
    if soup.select_one("#PublicMeetingsTable") or re.search(r"/Public/Organization/", response.url, re.I):
        raise ValueError("BoardBook redirected to organization listing; document is not published or unavailable")
    if soup.select_one('input[type="password"]') or re.search(r"/(?:Account/)?Log(?:in|on)\b", response.url, re.I):
        raise ValueError("BoardBook returned a login page instead of a document")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    if re.search(r"\b(?:error|access denied|not found|unavailable)\b", title, re.I):
        raise ValueError(f"BoardBook returned an error page: {title}")
    return soup


def _download_template(soup) -> str | None:
    for script in soup.find_all("script"):
        match = re.search(r"\bloadUrlFormat\s*=\s*(['\"])(.*?)\1", script.get_text(), re.S)
        if match:
            return html.unescape(match.group(2)).replace(r"\/", "/")
    return None


def _file_response(response, soup, fetcher):
    viewer_id = soup.select_one("#NewDocumentViewerDocumentID")
    template = _download_template(soup)
    if viewer_id and viewer_id.get("value") and template and "TBR_AAAA_" in template:
        url = urljoin(response.url, template.replace("TBR_AAAA_", viewer_id["value"]))
    elif template and "TBR_AAAA_" not in template and "Download" in template:
        # Snapshot viewers (/Documents/WebViewer?snapshotUri=...) embed the
        # finished download URL directly.
        url = urljoin(response.url, template)
    else:
        link = soup.select_one('a[href*="/Public/DownloadMinutes/"]')
        if link is None:
            if soup.select_one('#Tab-AgendaItem .agenda-items-section'):
                raise ValueError("BoardBook Minutes page publishes HTML only; no PDF/DOCX download is available")
            raise ValueError("BoardBook document viewer has no downloadable document")
        url = urljoin(response.url, link["href"])
    downloaded = fetcher.get(url)
    if not _is_file(downloaded):
        _page(downloaded)
        raise ValueError("BoardBook download returned HTML or an unsupported document instead of PDF/DOCX")
    return downloaded


def _attachments(node, response, soup) -> list[tuple[str, str]]:
    template = _download_template(soup)
    attachments = {}
    for link in node.select("a[href]"):
        href = link["href"]
        document_id = link.get("data-documentid")
        if document_id and template and "TBR_AAAA_" in template:
            url = urljoin(response.url, template.replace("TBR_AAAA_", document_id))
        elif re.search(r"/Documents/(?:Download|FileViewer)", href, re.I):
            url = urljoin(response.url, href)
        else:
            continue
        attachments.setdefault(url, _visible(link))
    return list(attachments.items())


def _extras(response, soup, fetcher) -> dict:
    node = soup.select_one("#Tab-AgendaItem")
    if node is None:
        raise ValueError("BoardBook Extras document list missing")
    attachments = _attachments(node, response, soup)
    result = _fields(response)
    result["attachment_links"] = [url for url, _ in attachments]
    blocks, statuses, errors = [], [], []
    pages = 0
    for url, title in attachments:
        try:
            document = fetcher.get(url)
            content_type = _fields(document)["content_type"].lower()
            # BoardBook serves M4A recordings as text/plain. Sniff media before
            # either the HTML parser or the shared plain-text extractor sees it.
            if (document.content[4:8] == b"ftyp"
                    or document.content.startswith((b"ID3", b"OggS", b"fLaC", b"RIFF"))
                    or content_type.startswith(("audio/", "video/"))):
                statuses.append("unsupported")
                errors.append(f"{title}: audio/video attachment has no supported text extraction")
                continue
            if not _is_file(document):
                document = _file_response(document, _page(document), fetcher)
            parsed = extract_file(document.raw_path, _fields(document)["content_type"], document.url)
            statuses.append(parsed["text_status"])
            pages += parsed.get("page_count") or 0
            if parsed.get("text"):
                blocks.append(f"{title}\n{parsed['text']}")
            if parsed.get("error"):
                errors.append(f"{title}: {parsed['error']}")
        except Exception as exc:
            # fetch.SkippedDownload carries its own status (media, oversize).
            statuses.append(getattr(exc, "status", "error"))
            errors.append(f"{title}: {type(exc).__name__}: {exc}")
    text = clean_text("\n\n".join(blocks)) or None
    if not statuses:
        status = "empty"
    elif all(s == "ok" for s in statuses):
        status = "ok"
    elif "error" in statuses:
        status = "error"
    elif "too_large" in statuses:
        status = "too_large"
    elif "unsupported" in statuses:
        status = "unsupported"
    elif text:
        status = "partial_ocr"
    else:
        status = "needs_ocr" if "needs_ocr" in statuses else "empty"
    result.update(text=text, text_status=status, text_method="html", page_count=pages or None,
                  error="; ".join(errors)[:1000] or None)
    return result


def _meeting_text(response, soup, detail: str) -> dict:
    if detail == "Projector":
        node = soup.select_one("#in-meeting-agenda-details-table")
    else:
        node = soup.select_one("#Tab-AgendaItem .agenda-items-section")
    if node is None:
        raise ValueError("BoardBook meeting content missing; refusing to store page chrome")
    attachments = _attachments(node, response, soup)
    header = soup.select_one("#MeetingHeader")
    heading = _visible(header) if header else ""
    # displayNone also marks real descriptions and projector items. Keep those,
    # but drop the offline duplicate links and the interactive viewer controls.
    for unwanted in node.select("script, style, input, button, nav, .ShowOnOffline, .Attachments, "
                                 ".agenda-item-fullrow.agenda-item-alwayshow, a.btn, .DisableOnOffline"):
        unwanted.decompose()
    text = clean_text((heading + "\n" if heading else "") + node.get_text("\n", strip=True))
    result = _fields(response)
    result.update(text=text or None, text_status="ok" if text else "empty", text_method="html",
                  attachment_links=[url for url, _ in attachments])
    return result


def resolve(record: dict, fetcher) -> dict:
    """Resolve HTML meetings or cached document bytes for process-pool parsing."""
    response = fetcher.get(record["source_link"])
    try:
        if _is_file(response):
            return _fields(response)
        soup = _page(response)
        if record["detail"] == "Extras":
            return _extras(response, soup, fetcher)
        has_viewer = soup.select_one("#NewDocumentViewerDocumentID")
        has_download = soup.select_one('a[href*="/Public/DownloadMinutes/"]')
        if record["detail"] == "Minutes" and not has_viewer and not has_download \
                and soup.select_one("#Tab-AgendaItem .agenda-items-section"):
            # Some districts publish minutes only as BoardBook HTML items.
            return _meeting_text(response, soup, record["detail"])
        # Some districts publish agendas, too, as snapshot PDFs behind
        # /Documents/WebViewer rather than as HTML agenda items.
        snapshot = "/Documents/WebViewer/" in response.url and _download_template(soup)
        if record["detail"] == "Minutes" or has_viewer or snapshot:
            return _fields(_file_response(response, soup, fetcher))
        return _meeting_text(response, soup, record["detail"])
    except ValueError as exc:
        result = _fields(response)
        # A custom-minutes page with nothing attached is a published but empty
        # record, not a failure to retry.
        if "/Documents/CustomMinutesForMeeting/" in response.url:
            status = "empty"
        elif "redirected to organization listing" in str(exc):
            # BoardBook stopped publishing this document (common for old
            # projector views); retrying will not change that.
            status = "unavailable"
        else:
            status = "error"
        result.update(text_status=status, error=str(exc))
        return result
