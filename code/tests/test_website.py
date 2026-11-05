"""Website collection contracts using small, offline CMS responses."""
import hashlib
import json
from pathlib import Path

import pytest

from fetch import Response
from platforms import website

FIXTURES = Path(__file__).parent / "fixtures" / "website"
DISTRICT = dict(tea_id="123456", school_district="Example ISD", county_name="Example")
BASE = "https://www.example.edu"


class Fetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        value = self.pages[url]
        if isinstance(value, Response):
            return value
        body = value.encode()
        return Response(url, 200, {"Content-Type": "text/html"}, body, "data/raw/body.gz",
                        hashlib.sha256(body).hexdigest(), True)


def fixture(name):
    return (FIXTURES / name).read_text()


@pytest.mark.parametrize("filename,expected", [
    ("June 26, 2017 Board Packet.pdf", "2017-06-26"),
    ("2023-08-14 Regular Meeting Minutes", "2023-08-14"),
    ("Minutes 8.14.23", "2023-08-14"),
    ("Minutes_January13__2020.docx", "2020-01-13"),
    ("11142016_regular_minutespdf.pdf", "2016-11-14"),
    ("min071626.doc", "2026-07-16"),
    ("2026-02-30 Minutes", None),
    ("1022017 called meeting", None),
    ("august 13 2019 board agenda 2019-08-09 17 00 05 doc", "2019-08-13"),
])
def test_filename_dates(filename, expected):
    assert website.parse_date(filename)[0] == expected


def test_collects_archives_and_excludes_unrelated_files():
    entry = BASE + "/school-board"
    fetcher = Fetcher({entry: fixture("board.html"), BASE + "/school-board/archive/2023": fixture("archive.html")})
    rows = website.discover(DISTRICT, entry, fetcher)
    assert len(rows) == 7
    by_label = {row["detail_raw"]: row for row in rows}
    assert by_label["Minutes 8.14.23"]["date"] == "2023-08-14"
    assert by_label["June 26, 2017 Board Packet.pdf"]["detail"] == "Extras"
    assert by_label["Board Meeting Notice 9/25/23"]["detail"] == "Other"
    assert by_label["Part 1"]["date"] == "2024-02-20"
    assert by_label["Part 1"]["detail"] == "Agenda"
    assert all(kw.get("refresh") for _, kw in fetcher.calls)
    assert not any("evil.example" in row["source_link"] for row in rows)
    assert by_label["Board Meeting Notice 9/25/23"]["listing_url"].endswith("/archive/2023")


def test_apptegy_json_tree_and_nuxt_references():
    entry = BASE + "/documents/board/1"
    payload = json.loads(fixture("apptegy.json"))
    # Indexed Nuxt/devalue JSON uses integer references, not literal values.
    nuxt = [["ShallowReactive", 1], {"data": 2}, {"folder": 3},
            {"foldersAndDocuments": 4}, [5], {"file_name": 6, "url": 7},
            "Board Minutes 2023-08-14", "https://aptg.co/minutes"]
    html = '<h1>Board Minutes</h1><script id="__NUXT_DATA__" type="application/json">' + json.dumps(nuxt) + '</script>'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: html}))
    assert [(r["date"], r["detail"], r["source_link"]) for r in rows] == [
        ("2023-08-14", "Minutes", "https://aptg.co/minutes")]
    fetcher = Fetcher({entry: json.dumps(payload), BASE + "/documents/board-minutes/789": '{"documents": []}'})
    rows = website.discover(DISTRICT, entry, fetcher)
    assert len(rows) == 1
    assert rows[0]["source_link"].endswith("Minutes_2023-08-14.docx")


def test_finalsite_collection_and_article_metadata():
    entry = BASE + "/school-board/minutes"
    ajax = BASE + "/fs/elements/30075?depth=2&display_in_element=true&element_id=30075&page_id=3141&resource_collection=1097"
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture("finalsite.html"), ajax: fixture("finalsite_collection.html")}))
    assert len(rows) == 4
    by_label = {r["detail_raw"]: r for r in rows}
    assert by_label["Workshop Agenda"]["meeting_type"] == "Workshop"
    assert by_label["Workshop Agenda"]["date"] == "2026-07-21"
    assert by_label["Regular Meeting Minutes"]["meeting_type"] == "Regular"
    assert by_label["June 16, 2025 Minutes"]["listing_url"] == ajax
    assert by_label["Agenda"]["date"] == "2024-05-21"
    assert by_label["Agenda"]["meeting_type"] is None


def test_crawl_cap_is_observable(monkeypatch):
    monkeypatch.setattr(website, "MAX_LISTING_PAGES", 1)
    entry = BASE + "/school-board"
    website.discover(DISTRICT, entry, Fetcher({entry: fixture("board.html")}))
    assert website.DISCOVERY_STATS["123456|" + entry]["cap_hit"] is True


def test_binary_google_export_and_html_resolution():
    source = "https://docs.google.com/document/d/abc/edit"
    export = "https://docs.google.com/document/d/abc/export?format=txt"
    raw = Response(export, 200, {"Content-Type": "text/plain"}, b"Board minutes", "data/raw/a.gz", "hash", True)
    resolved = website.resolve({"source_link": source, "listing_url": BASE}, Fetcher({export: raw}))
    assert resolved["text"] is None
    assert resolved["raw_path"] == "data/raw/a.gz"
    assert resolved["document_link"] == export
    url = BASE + "/board/minutes-2023-08-14"
    resolved = website.resolve({"source_link": url, "listing_url": BASE}, Fetcher({url: '<html><body><nav>Ignore navigation</nav><main><h1>Board Minutes August 14, 2023</h1><p>The board approved the budget.</p></main></body></html>'}))
    assert resolved["text"] == "Board Minutes August 14, 2023\nThe board approved the budget."
    assert resolved["text_status"] == "ok"
    pdf = BASE + "/minutes.pdf"
    raw = Response(pdf, 200, {"Content-Type": "application/pdf"}, b"%PDF example", "data/raw/b.gz", "pdfhash", True)
    assert website.resolve({"source_link": pdf, "listing_url": BASE}, Fetcher({pdf: raw}))["text"] is None


def test_apptegy_api_pagination_refreshes_every_listing():
    entry = BASE + "/documents/board-minutes/42"
    endpoint = "https://thrillshare-cmsv2.services.thrillshare.com/api/v2/s/123/documents"
    api = endpoint + "?locale=en&not_reset_video_dimensions=true&page_size=40&folder_id=42"
    following = endpoint + "?folder_id=42&page_no=2"
    fetcher = Fetcher({
        entry: '<h1>Board Minutes</h1><script type="application/json">' + json.dumps({"documents": endpoint}) + '</script>',
        api: json.dumps({"documents": [{"file_name": "Minutes 8.14.23", "url": "https://5il.co/first"}],
                         "meta": {"current_folder": "Board Minutes", "links": {"next": following}}}),
        following: json.dumps({"documents": [{"file_name": "Minutes 9.18.23", "url": "https://aptg.co/second"}],
                               "meta": {"current_folder": "Board Minutes", "links": {"next": None}}}),
    })
    rows = website.discover(DISTRICT, entry, fetcher)
    assert {r["date"] for r in rows} == {"2023-08-14", "2023-09-18"}
    assert {r["listing_url"] for r in rows} == {api, following}
    assert all(kwargs["refresh"] for _, kwargs in fetcher.calls)


def test_bot_challenge_closes_browser_and_preserves_published_links(monkeypatch):
    entry = BASE + "/page/school-board"
    closed = []
    monkeypatch.setattr(website.browser, "page_html", lambda url, session: (
        url, '<main><h1>School Board</h1><a href="https://5il.co/board">Board Minutes 8.14.23</a></main>'))
    monkeypatch.setattr(website.browser, "cookies", lambda session: {})
    monkeypatch.setattr(website.browser, "close", closed.append)
    rows = website.discover(DISTRICT, entry, Fetcher({entry: '<title>Client Challenge</title>'}))
    assert rows[0]["source_link"] == "https://5il.co/board"
    assert len(closed) == 1
    assert website.DISCOVERY_STATS["123456|" + entry]["browser_pages"] == 1


def test_stays_in_board_section_and_skips_opaque_nonmeeting_files():
    entry = BASE + "/school-board/minutes"
    html = '''<h1>Board Minutes</h1><h2>Board Operating Procedures</h2>
    <a href="/fs/resource-manager/view/procedures"></a>
    <a href="/departments/innovation/board-agendas">Board Agendas 2023</a>
    <h2>School Board Minutes</h2><a href="/files/alice.pdf">Alice Smith</a>
    <a href="/files/staff.pdf">Staff Directory</a>
    <a href="/files/8_24_2026_regular_meeting.pdf">8 24 2026 Regular Meeting</a>'''
    rows = website.discover(DISTRICT, entry, Fetcher({entry: html}))
    assert [(r["date"], r["detail"]) for r in rows] == [("2026-08-24", "Minutes")]


def test_schoolmessenger_rendered_folders_report_client_pagination():
    entry = BASE + "/school_board"
    folder = BASE + "/cms/One.aspx?objectId=42"
    html = '''<main><h1>School Board</h1><ul class="documentList"><li>
    <a class="copyLink" href="#" data-item="content_file" title="Click here to copy file url 'August 14, 2025 Minutes'" data-clipboard-text="/common/DisplayFile.aspx?itemId=123">Copy Link</a>
    </li></ul>PAGE 1 OF 2</main>'''
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture("schoolmessenger.html"), folder: html}))
    assert len(rows) == 1
    assert rows[0]["date"] == "2025-08-14"
    assert rows[0]["detail"] == "Minutes"
    assert rows[0]["source_link"] == BASE + "/common/DisplayFile.aspx?itemId=123"
    assert rows[0]["detail_raw"] == "August 14, 2025 Minutes"
    assert website.DISCOVERY_STATS["123456|" + entry]["warnings"] == [
        "SchoolMessenger client-side pagination may be incomplete: " + folder]


def test_apptegy_year_folders_reuse_json_api():
    endpoint = "https://thrillshare-cmsv2.services.thrillshare.com/api/v2/s/123/documents"
    entry = endpoint + "?folder_id=42&page_size=40"
    child = endpoint + "?folder_id=99&page_size=40"
    fetcher = Fetcher({
        entry: json.dumps({"documents": [], "meta": {"current_folder": "Board Minutes", "folders": [{"id": 99, "folder_name": "2023"}]}}),
        child: json.dumps({"documents": [{"file_name": "Minutes 8.14.23", "url": "https://aptg.co/file"}], "meta": {"current_folder": "2023"}}),
    })
    rows = website.discover(DISTRICT, entry, fetcher)
    assert [(r["source_link"], r["date"]) for r in rows] == [("https://aptg.co/file", "2023-08-14")]
    assert [url for url, _ in fetcher.calls] == [entry, child]


def test_legacy_html_minutes_are_documents_on_district_subdomain():
    entry = BASE + "/our-district/board/meeting-dates"
    source = "http://web.example.edu/school_board/Minutes/2010-2011/June/6-21-11BoardWorkshopMinutes.htm"
    rows = website.discover(DISTRICT, entry, Fetcher({entry: '<h1>Board Meetings</h1><a href="' + source + '">Minutes</a>'}))
    assert [(r["date"], r["detail"], r["meeting_type"]) for r in rows] == [("2011-06-21", "Minutes", "Workshop")]
    assert rows[0]["source_link"] == source
    final_url = source.replace('http:', 'https:')
    body = '<html><head><meta charset="windows-1252"></head><body><p>Board Workshop Minutes</p><p>June 21, 2011</p><p>Approved – 6:00 p.m.</p></body></html>'.encode('cp1252')
    response = Response(final_url, 200, {"Content-Type": "text/html"}, body, "data/raw/html.gz", hashlib.sha256(body).hexdigest(), True)
    result = website.resolve(rows[0], Fetcher({final_url: response}))
    assert result["text"] == "Board Workshop Minutes\nJune 21, 2011\nApproved – 6:00 p.m."
    assert result["text_method"] == "html"


@pytest.mark.parametrize("context,expected", [
    ("Minutes 2023-2024", "2024-01-15"),
    ("Minutes 2023-24", "2024-01-15"),
    ("Minutes 2023", "2023-01-15"),
])
def test_month_day_uses_calendar_or_school_year_folder(context, expected):
    assert website.parse_date("January 15", context)[0] == expected
