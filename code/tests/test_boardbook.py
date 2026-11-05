"""BoardBook discovery and resolution contracts using small saved HTML pages."""
import gzip
import hashlib
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fetch import Response
from platforms import boardbook
from records import DOCUMENT_FIELDS

FIXTURES = Path(__file__).parent / "fixtures" / "boardbook"
BASE = "https://meetings.boardbook.org"
DISTRICT = dict(tea_id="108908", school_district="Mission CISD", county_name="Hidalgo County")


def response(fixture, url, content_type="text/html"):
    content = (FIXTURES / fixture).read_bytes()
    return Response(url, 200, {"Content-Type": content_type}, content,
                    "data/raw/fixture.gz", hashlib.sha256(content).hexdigest(), True)


class Fetcher:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]


def test_discover_direct_rows_all_history_and_source_metadata():
    url = BASE + "/Public/Organization/741"
    fetcher = Fetcher({url: response("listing.html", url)})
    records = boardbook.discover(DISTRICT, url, fetcher)
    assert len(records) == 7
    assert [r["detail"] for r in records] == ["Agenda", "Minutes", "Projector", "Extras", "Extras", "Other", "Agenda"]
    assert records[4]["detail_raw"] == "Supplementals"
    assert records[0]["date"] == "2026-10-21"
    assert records[0]["date_raw"] == "October 21, 2026"
    assert records[0]["meeting_type"] == "Regular"
    assert records[0]["meeting_id"] == "771619"
    assert records[0]["source_link"] == BASE + "/Public/Agenda/741?meeting=771619"
    assert records[0]["listing_url"] == url
    assert "Nested row" in records[0]["listing_row_text"]
    assert records[-1]["date"] == "2017-01-03"
    assert records[-1]["meeting_type"] == "Special"
    assert "CANCELLED" in records[-1]["meeting_title"]
    assert fetcher.calls == [(url, {"refresh": True})]


def test_minutes_redirect_to_listing_is_an_error_without_text():
    source = BASE + "/Public/Minutes/741?meeting=999"
    fetcher = Fetcher({source: response("listing.html", BASE + "/Public/Organization/741")})
    result = boardbook.resolve(dict(source_link=source, detail="Minutes"), fetcher)
    assert result["text_status"] == "error"
    assert result["text"] is None
    assert "organization listing" in result["error"]
    assert "not published" in result["error"]


@pytest.mark.parametrize("fixture,source,download", [
    ("viewer.html", "/Public/Minutes/741?meeting=771619", "/Documents/DownloadPDF/14593205?org=741"),
    ("standard_minutes.html", "/Public/Minutes/2197?meeting=766085", "/Public/DownloadMinutes/2197?meeting=766085"),
])
def test_minutes_returns_actual_pdf_for_pipeline(fixture, source, download):
    pdf = b"%PDF-1.7\nfixture"
    document = Response(BASE + download, 200, {"Content-Type": "application/pdf"}, pdf,
                        "data/raw/actual-pdf.gz", hashlib.sha256(pdf).hexdigest(), True)
    fetcher = Fetcher({BASE + source: response(fixture, BASE + source), BASE + download: document})
    result = boardbook.resolve(dict(source_link=BASE + source, detail="Minutes"), fetcher)
    assert set(result) == set(DOCUMENT_FIELDS)
    assert result["document_link"] == BASE + download
    assert result["raw_path"] == "data/raw/actual-pdf.gz"
    assert result["content_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert result["content_type"] == "application/pdf"
    assert result["text"] is None and result["text_status"] is None


@pytest.mark.parametrize("detail,fixture", [("Agenda", "agenda.html"), ("Projector", "projector.html")])
def test_meeting_html_excludes_navigation_and_duplicate_toc(detail, fixture):
    source = BASE + f"/Public/{detail}/741?meeting=771619"
    fetcher = Fetcher({source: response(fixture, source)})
    result = boardbook.resolve(dict(source_link=source, detail=detail), fetcher)
    assert result["text_status"] == "ok"
    assert result["text_method"] == "html"
    assert result["text"].count("Call Meeting to Order") == 1
    assert "Home" not in result["text"] and "Web Viewer" not in result["text"]
    assert "Show Everything" not in result["text"] and "secret javascript" not in result["text"]
    assert result["attachment_links"] == [BASE + "/Documents/DownloadPDF/14563545?org=741"]
    assert len(fetcher.calls) == 1
    if detail == "Agenda":
        assert "Discuss the district safety plan" in result["text"]
    else:
        assert "Safety plan" in result["text"]


@pytest.mark.parametrize("content,url", [
    (b'<title>Sign in</title><input type="password">', "/Account/Login"),
    (b'<title>Server Error</title><main>Try again</main>', "/Public/Agenda/741?meeting=1"),
    (b'<title>Public Meeting Agenda</title><main>Help Home</main>', "/Public/Agenda/741?meeting=1"),
])
def test_non_documents_are_not_stored_as_text(content, url):
    source = BASE + "/Public/Agenda/741?meeting=1"
    page = Response(BASE + url, 200, {"Content-Type": "text/html"}, content, "raw.gz", "sha", True)
    result = boardbook.resolve(dict(source_link=source, detail="Agenda"), Fetcher({source: page}))
    assert result["text_status"] == "error"
    assert result["text"] is None


def test_extras_combines_document_text_with_titles(tmp_path, monkeypatch):
    from docx import Document
    source = BASE + "/Meeting/Supplementals/741?meeting=771619"
    responses = {source: response("extras.html", source)}
    for document_id, text in [("14563108", "Taxpayer impact: $125 per household."),
                               ("14563109", "Safety report: all drills completed.")]:
        url = BASE + f"/Documents/DownloadPDF/{document_id}?org=741"
        raw_path = document_id + ".gz"
        document = Document()
        document.add_paragraph(text)
        output = io.BytesIO()
        document.save(output)
        content = output.getvalue()
        (tmp_path / raw_path).write_bytes(gzip.compress(content))
        responses[url] = Response(url, 200, {"Content-Type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}, content,
                                  raw_path, hashlib.sha256(content).hexdigest(), True)
    import extract
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    result = boardbook.resolve(dict(source_link=source, detail="Extras"), Fetcher(responses))
    assert result["text_status"] == "ok"
    assert "Taxpayer impact statement\nTaxpayer impact: $125 per household." in result["text"]
    assert "Safety report\nSafety report: all drills completed." in result["text"]
    assert len(result["attachment_links"]) == 2


def test_html_only_minutes_keep_their_item_text_without_inventing_a_pdf():
    source = BASE + "/Public/Minutes/841?meeting=763061"
    fetcher = Fetcher({source: response("html_minutes.html", source)})
    result = boardbook.resolve(dict(source_link=source, detail="Minutes"), fetcher)
    assert result["text_status"] == "ok"
    assert result["text_method"] == "html"
    assert result["text"]
    assert "Back to the Public Page" not in result["text"]
    assert len(fetcher.calls) == 1


def test_m4a_served_as_plain_text_is_unsupported_not_document_text(tmp_path, monkeypatch):
    import extract
    monkeypatch.setattr(extract, "ROOT", tmp_path)
    content = b"\x00\x00\x00\x1cftypM4A \x00\x00\x00\x00M4A isommp42\x00\x00"
    (tmp_path / "audio.gz").write_bytes(gzip.compress(content))
    source = BASE + "/Meeting/Supplementals/841?meeting=763200"
    download = BASE + "/Documents/DownloadOriginalOrPublic/841?file=14411969"
    audio = Response(download, 200, {"Content-Type": "text/plain"}, content,
                     "audio.gz", hashlib.sha256(content).hexdigest(), True)
    result = boardbook.resolve(dict(source_link=source, detail="Extras"), Fetcher({
        source: response("audio.html", source), download: audio,
    }))
    assert result["text_status"] == "unsupported"
    assert result["text"] is None
    assert "audio/video" in result["error"]
    assert result["attachment_links"] == [download]
