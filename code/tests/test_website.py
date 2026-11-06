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
    export = "https://docs.google.com/document/d/abc/export?format=pdf"
    raw = Response(export, 200, {"Content-Type": "application/pdf"}, b"%PDF Board minutes", "data/raw/a.gz", "hash", True)
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


@pytest.mark.parametrize("label,url,context,expected", [
    ("1. 9-28", "https://x/04.a1.%209-28-22%20Minutes.pdf", "", "2022-09-28"),
    ("Public Hearing", "https://x/Public_Hearing_Innovation_2023-2028__4-18-2023.pdf", "", "2023-04-18"),
    ("Nov 10", "https://x/11102025-WorkshopMeeting-Minutes.pdf", "Board Named a 2026 Texas Top 5 Honor Board", "2025-11-10"),
    ("Agenda", "https://x/120246", "", None),
    ("3271932", "https://x/a.pdf", "", None),
    ("Minutes", "https://x/2026-09-16_-_06_-_att_1_-_2029-08-19_board_meeting_minutes_1.pdf", "", "2026-09-16"),
    ("May 26", "https://x/2026-May-26%2C--Agenda.pdf", "2026-2027 Board Agenda/Minutes", "2026-05-26"),
    ("Code of Conduct", "https://x/EPISD%20CODE%202026-2027%207-9-26%20BOARD%20APPROVED%207-14-26.pdf", "", "2026-07-09"),
])
def test_dates_reject_implausible_years_and_prefer_reliable_sources(label, url, context, expected):
    assert website.date_for(label, url, context)[0] == expected


def test_public_drive_folders_recurse_and_preserve_dates_and_resource_keys():
    entry = 'https://drive.google.com/drive/folders/root'
    embedded = 'https://drive.google.com/embeddedfolderview?id=root'
    child = 'https://drive.google.com/embeddedfolderview?id=child&resourcekey=0-key'
    rows = website.discover(DISTRICT, entry, Fetcher({embedded: fixture('drive_folder.html'), child: fixture('drive_child.html')}))
    assert {(r['date'], r['detail']) for r in rows} == {('2023-08-14', 'Minutes'), ('2023-09-18', 'Minutes'), ('2024-01-15', 'Minutes')}
    assert len(rows) == 3
    assert all('policy' not in r['detail_raw'] for r in rows)


def test_cms_download_hosts_and_extensionless_documents():
    entry = BASE + '/board'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture('cms_hosts.html')}))
    assert {r['date'] for r in rows} == {'2023-08-14', '2023-09-18', '2023-10-16', '2023-11-13'}
    assert len(rows) == 4


def test_apptegy_collapsed_accordion_bootstrap():
    entry = BASE + '/page/board-meeting-information'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture('apptegy_bootstrap.html')}))
    assert [(r['date'], r['detail'], r['source_link']) for r in rows] == [('2024-01-15', 'Extras', 'https://5il.co/packet')]


@pytest.mark.parametrize('blocked', [False, True])
def test_browser_fallback_for_js_shell_and_http_403(monkeypatch, blocked):
    import requests
    entry = BASE + '/board'
    closed = []
    class ShellFetcher(Fetcher):
        def get(self, url, **kwargs):
            if blocked:
                response = requests.Response()
                response.status_code = 403
                raise requests.HTTPError('403 Forbidden', response=response)
            return super().get(url, **kwargs)
    monkeypatch.setattr(website.browser, 'page_html', lambda url, session: (url, fixture('archive.html')))
    monkeypatch.setattr(website.browser, 'cookies', lambda session: {})
    monkeypatch.setattr(website.browser, 'close', closed.append)
    rows = website.discover(DISTRICT, entry, ShellFetcher({entry: '<title>Board Minutes</title><div id="app"></div><script src="/app.js"></script>'}))
    assert rows
    assert len(closed) == 1


def test_granicus_viewers_are_documents_and_escaped_entry_is_normalized():
    entry = 'https://episd.granicus.com/ViewPublisher.php?view_id=1'
    rows = website.discover(DISTRICT, entry + '\\\\', Fetcher({entry: fixture('granicus.html')}))
    assert {(r['date'], r['detail']) for r in rows} == {('2026-10-20', 'Agenda'), ('2026-10-20', 'Minutes')}
    assert len(rows) == 2


def test_swagit_agendas_use_meeting_row_dates():
    entry = 'https://crowleyisdtx.new.swagit.com/views/75/'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture('swagit.html')}))
    assert [(r['date'], r['detail']) for r in rows] == [('2026-10-29', 'Agenda')]


def test_apptegy_nodes_keep_accordion_and_bold_date_context():
    entry = BASE + '/board'
    html = '<h1>School Board</h1><script type="application/json">' + fixture('apptegy_nodes.json') + '</script>'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: html}))
    assert {(r['source_link'], r['date'], r['detail']) for r in rows} == {
        ('https://aptg.co/agenda', '2025-09-15', 'Agenda'),
        ('https://aptg.co/second', '2025-10-20', 'Agenda'),
        ('https://aptg.co/minutes', None, 'Minutes'),
    }


def test_drive_downloads_and_granicus_tls_redirect():
    import requests
    source = 'https://drive.google.com/file/d/pdf/view?resourcekey=0-key'
    download = 'https://drive.usercontent.google.com/download?id=pdf&export=download&resourcekey=0-key'
    pdf = Response(download, 200, {'Content-Type': 'application/pdf'}, b'%PDF bytes', 'data/raw/pdf.gz', 'pdfhash', True)
    assert website.resolve({'source_link': source, 'listing_url': BASE}, Fetcher({download: pdf}))['document_link'] == download
    urls = json.loads(fixture('granicus_download.json'))
    class RedirectFetcher(Fetcher):
        def get(self, url, **kwargs):
            if url == urls['viewer']:
                raise requests.exceptions.SSLError('hostname mismatch', request=requests.Request('GET', urls['redirect']).prepare())
            return super().get(url, **kwargs)
    result = website.resolve({'source_link': urls['viewer'], 'listing_url': 'https://episd.granicus.com'}, RedirectFetcher({urls['download']: pdf}))
    assert result['raw_path'] == 'data/raw/pdf.gz'


def test_layout_table_policy_navigation_does_not_hide_agendas():
    entry = BASE + '/board'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture('socs_layout.html')}))
    assert [(r['date'], r['detail']) for r in rows] == [('2024-10-16', 'Agenda')]


def test_schoolblocks_flight_files_use_titles_not_modified_timestamps():
    entry = BASE + '/schoolboard'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture('schoolblocks_flight.html')}))
    assert [(r['date'], r['detail'], r['source_link']) for r in rows] == [
        ('2026-09-14', 'Minutes', 'https://drive.google.com/file/d/axtell/view')]


def test_apptegy_two_column_minutes_keep_month_only_dates_unknown():
    entry = BASE + '/board'
    html = '<h1>School Board</h1><script type="application/json">' + fixture('apptegy_columns.json') + '</script>'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: html}))
    assert len(rows) == 13
    assert {r['detail'] for r in rows} == {'Minutes'}
    assert sum(r['date'] is None for r in rows) == 12
    assert next(r['date'] for r in rows if 'Special Session' in r['detail_raw']) == '2026-08-31'


def test_apptegy_indexed_pinia_page_is_not_hidden_by_empty_data():
    entry = BASE + '/board'
    html = '<script id="__NUXT_DATA__" type="application/json">' + fixture('apptegy_pinia.json') + '</script>'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: html}))
    assert [(r['date'], r['detail']) for r in rows] == [('2025-09-15', 'Minutes')]


def test_apptegy_tenant_alias_and_browse_school_year_archives():
    entry = BASE + '/page/meetings'
    folder = 'https://example.sites.thrillshare.com/documents/district/school-board/meetings/2026-27-school-year/42'
    archive = BASE + '/browse/43'
    rows = website.discover(DISTRICT, entry, Fetcher({
        entry: '<h1>Board Meetings</h1><a href="' + folder + '">2026-27 School Year</a><a href="/browse/43">2025-26 School Year</a>',
        folder: '<h1>Board Minutes</h1><a href="https://5il.co/first">Minutes September 14, 2026</a>',
        archive: '<h1>Board Minutes</h1><a href="https://5il.co/second">Minutes September 15, 2025</a>',
    }))
    assert {r['date'] for r in rows} == {'2025-09-15', '2026-09-14'}


def test_drive_tracking_links_do_not_duplicate_the_same_document():
    entry = BASE + '/board'
    rows = website.discover(DISTRICT, entry, Fetcher({entry: '''<h1>Board Minutes</h1>
        <a href="https://drive.google.com/file/d/same/view?usp=sharing">Minutes August 14, 2023</a>
        <a href="https://drive.google.com/file/d/same/view?usp=drive_web">Minutes August 14, 2023</a>
        <a href="https://drive.google.com/open?id=same">Minutes August 14, 2023</a>'''}))
    assert len(rows) == 1
    assert rows[0]['source_link'] == 'https://drive.google.com/file/d/same/view'


def test_board_context_does_not_admit_unrelated_district_folders():
    entry = BASE + '/page/school-board'
    staar = BASE + '/documents/district-info/staar/42'
    minutes = BASE + '/documents/district-info/school-board/meeting-minutes/43'
    fetcher = Fetcher({
        entry: '<h1>School Board Minutes</h1><script type="application/json">' + fixture('apptegy_navigation.json') + '</script>',
        staar: '<h1>Assessment</h1><a href="https://5il.co/assessment">Agenda August 14, 2023</a>',
        minutes: '<h1>Board Minutes</h1><a href="https://5il.co/minutes">Minutes August 14, 2023</a>',
    })
    rows = website.discover(DISTRICT, entry, fetcher)
    assert len(rows) == 1
    assert rows[0]['source_link'] == 'https://5il.co/minutes'
    assert staar not in {url for url, _ in fetcher.calls}


def test_browser_transient_network_change_retries_without_losing_documents(monkeypatch):
    entry = BASE + '/board'
    opened, closed = [], []
    def render(url, session):
        opened.append(url)
        if len(opened) == 1:
            raise RuntimeError('agent-browser open failed: net::ERR_NETWORK_CHANGED')
        return url, '<h1>Board Minutes</h1><a href="https://5il.co/minutes">Minutes August 14, 2023</a>'
    monkeypatch.setattr(website.browser, 'page_html', render)
    monkeypatch.setattr(website.browser, 'cookies', lambda session: {})
    monkeypatch.setattr(website.browser, 'close', closed.append)
    rows = website.discover(DISTRICT, entry, Fetcher({entry: '<title>Client Challenge</title>'}))
    assert len(rows) == 1 and rows[0]['date'] == '2023-08-14'
    assert len(opened) == 2 and len(closed) == 2


def test_php_content_routes_are_not_mistaken_for_pagination():
    entry = BASE + '/index.php?page=school-board'
    employment = BASE + '/index.php?page=employment&mo=11&yr=2026'
    fetcher = Fetcher({entry: fixture('php_board.html'),
                       employment: '<h1>Employment</h1><a href="https://5il.co/hiring">Hiring Agenda August 14, 2023</a>',
                       BASE + '/index.php?page=2': '<h1>Board Minutes</h1><a href="https://5il.co/next">Minutes September 18, 2023</a>'})
    rows = website.discover(DISTRICT, entry, fetcher)
    assert {r['date'] for r in rows} == {'2023-08-14', '2023-09-18'}
    assert employment not in {url for url, _ in fetcher.calls}


def test_public_drive_iframe_does_not_require_browser_rendering(monkeypatch):
    entry = BASE + '/board'
    def unexpected(*args, **kwargs):
        raise AssertionError('public Drive folder already present in HTML')
    monkeypatch.setattr(website.browser, 'page_html', unexpected)
    monkeypatch.setattr(website.browser, 'close', lambda session: None)
    rows = website.discover(DISTRICT, entry, Fetcher({
        entry: fixture('drive_iframe.html'),
        'https://drive.google.com/embeddedfolderview?id=root': fixture('drive_folder.html'),
        'https://drive.google.com/embeddedfolderview?id=child&resourcekey=0-key': fixture('drive_child.html'),
    }))
    assert len(rows) == 3


def test_failed_optional_child_render_keeps_already_discovered_documents(monkeypatch):
    entry = BASE + '/board'
    def unavailable(*args, **kwargs):
        raise RuntimeError('browser unavailable')
    monkeypatch.setattr(website.browser, 'page_html', unavailable)
    monkeypatch.setattr(website.browser, 'close', lambda session: None)
    rows = website.discover(DISTRICT, entry, Fetcher({
        entry: '<h1>Board Minutes</h1><a href="https://5il.co/minutes">Minutes August 14, 2023</a><a href="/board/archive">Minutes Archive</a>',
        BASE + '/board/archive': '<h1>Board Minutes</h1><script src="/app.js"></script>',
    }))
    assert len(rows) == 1
    assert website.DISCOVERY_STATS['123456|' + entry]['failed_pages']


def test_drive_octet_stream_docx_reaches_text_extractor(tmp_path):
    import gzip
    import extract
    source = 'https://drive.google.com/file/d/word/view'
    download = 'https://drive.usercontent.google.com/download?id=word&export=download'
    body = (FIXTURES / 'drive_minutes.docx').read_bytes()
    raw = tmp_path / 'minutes.gz'
    raw.write_bytes(gzip.compress(body))
    response = Response(download, 200, {'Content-Type': 'application/octet-stream',
                        'Content-Disposition': 'attachment; filename="Board Minutes 2023-08-14.docx"'},
                        body, str(raw), hashlib.sha256(body).hexdigest(), True)
    resolved = website.resolve({'source_link': source, 'listing_url': BASE}, Fetcher({download: response}))
    parsed = extract.extract_file(resolved['raw_path'], resolved['content_type'], resolved['document_link'])
    assert parsed['text_status'] == 'ok'
    assert 'The board approved the school safety plan.' in parsed['text']


def test_paperless_public_agendas_use_listing_dates_and_anonymous_session(monkeypatch):
    def unexpected_browser(*args, **kwargs):
        raise AssertionError("published HTML agendas do not need rendering")
    monkeypatch.setattr(website.browser, "page_html", unexpected_browser)
    monkeypatch.setattr(website.browser, "close", lambda session: None)
    entry = 'https://www.mywebschooltools.com/schools/Charlotte/BoardMeeting/meetings.php'
    first = entry.replace('meetings.php', 'viewagenda.php?tblName=meeting0928202648')
    second = entry.replace('meetings.php', 'viewagenda.php?tblName=meeting0908202613')
    rows = website.discover(DISTRICT, entry, Fetcher({entry: fixture('paperless_meetings.html')}))
    assert {(r['date'], r['detail']) for r in rows} == {('2026-10-21', 'Agenda'), ('2026-09-16', 'Agenda')}
    assert len(rows) == 2
    class PublicSessionFetcher(Fetcher):
        primed = False
        def get(self, url, **kwargs):
            if url == entry:
                self.primed = True
            elif not self.primed:
                raise RuntimeError('public guest session must be initialized at meetings.php')
            return super().get(url, **kwargs)
    fetcher = PublicSessionFetcher({entry: fixture('paperless_meetings.html'), second: fixture('paperless_agenda.html')})
    resolved = website.resolve(next(r for r in rows if r['source_link'] == second), fetcher)
    assert resolved['text_status'] == 'ok'
    assert 'Call to Order' in resolved['text']
    assert 'Logout' not in resolved['text']


def test_paperless_login_html_is_not_reported_as_agenda_text():
    entry = 'https://www.mywebschooltools.com/schools/Charlotte/BoardMeeting/meetings.php'
    source = entry.replace('meetings.php', 'viewagenda.php?tblName=meeting0928202648')
    with pytest.raises(RuntimeError, match='agenda'):
        website.resolve({'source_link': source, 'listing_url': entry}, Fetcher({
            entry: fixture('paperless_meetings.html'), source: fixture('paperless_unavailable.html')}))


def test_granicus_redirected_novus_agenda_is_real_html_document():
    source = 'https://episd.granicus.com/AgendaViewer.php?view_id=1&clip_id=268'
    target = 'https://episd.novusagenda.com/agendapublic/MeetingView.aspx?MeetingID=1376'
    body = fixture('novus_agenda.html').encode()
    response = Response(target, 200, {'Content-Type': 'text/html'}, body, 'data/raw/novus.gz', 'hash', True)
    result = website.resolve({'source_link': source, 'listing_url': 'https://episd.granicus.com'}, Fetcher({source: response}))
    assert result['text_status'] == 'ok'
    assert 'Governance Team Training Session' in result['text']
    assert result['document_link'] == target
    assert result['attachment_links'] == ['https://episd.novusagenda.com/agendapublic/CoverSheet.aspx?ItemID=8211&MeetingID=1376']


def test_novus_unavailable_form_is_not_reported_as_agenda_text():
    source = 'https://episd.granicus.com/AgendaViewer.php?view_id=1&clip_id=268'
    target = 'https://episd.novusagenda.com/agendapublic/MeetingView.aspx?MeetingID=1376'
    response = Response(target, 200, {'Content-Type': 'text/html'}, fixture('novus_unavailable.html').encode(), 'data/raw/novus.gz', 'hash', True)
    with pytest.raises(RuntimeError, match='agenda'):
        website.resolve({'source_link': source, 'listing_url': 'https://episd.granicus.com'}, Fetcher({source: response}))
