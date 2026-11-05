"""Mapping contracts: names must match variants, vendor lookalikes must not."""
import json
import pytest
from bs4 import BeautifulSoup

from detect_platform import google_drive_folder, classify_link, detect_cms, document_evidence, links_from
from match_boardbook import match_district, normalize_name


@pytest.mark.parametrize('left,right', [
    ('Clarendon ISD', 'Clarendon CISD'),
    ('Carrizo Springs CISD', 'Carrizo Springs C.I.S.D.'),
    ('Goldthwaite ISD', 'Goldthwaite Consolidated Independent School District'),
    ('Mt. Vernon ISD', 'Mount Vernon Independent School District'),
    ('Eagle Mt-Saginaw ISD', 'Eagle Mountain-Saginaw ISD'),
    ('Fort Hancock ISD', 'Ft. Hancock Independent School District'),
    ('Chapel Hill ISD', 'Chapel Hill ISD -Titus County'),
    ('Edgewood ISD', 'Edgewood ISD – Van Zandt County'),
    ('Alba-Golden ISD', 'Alba Golden I.S.D.'),
])
def test_normalized_district_variants(left, right):
    assert normalize_name(left) == normalize_name(right)


@pytest.mark.parametrize('url,expected', [
    ('https://meetings.boardbook.org/Public/Organization/1111', ('boardbook', 'https://meetings.boardbook.org/Public/Organization/1111')),
    ('https://go.boarddocs.com/tx/austin/Board.nsf/goto?open&id=123', ('boarddocs', 'https://go.boarddocs.com/tx/austin/Board.nsf/Public')),
    ('https://austinisd.community.diligentoneplatform.com/Portal/MeetingInformation.aspx?Id=1', ('diligent', 'https://austinisd.community.diligentoneplatform.com/Portal/')),
    ('https://shermanisd.diligent.community/Portal/', ('diligent', 'https://shermanisd.diligent.community/Portal/')),
    ('https://district.civicweb.net/filepro/documents/123', ('diligent', 'https://district.civicweb.net/Portal/')),
    ('https://district.legistar.com/MeetingDetail.aspx?ID=1', ('legistar', 'https://district.legistar.com/Calendar.aspx')),
    ('https://simbli.eboardsolutions.com/SB_Meetings/MeetingsListing.aspx?S=123', ('simbli', 'https://simbli.eboardsolutions.com/Index.aspx?S=123')),
    ('https://www.school.edu/redirect?url=https%3A%2F%2Fgo.boarddocs.com%2Ftx%2Fa%2FBoard.nsf', ('boarddocs', 'https://go.boarddocs.com/tx/a/Board.nsf/Public')),
    ('https://boardbook.org.evil.test/Public/Organization/1', None),
    ('https://notboarddocs.com/tx/a/Board.nsf/Public', None),
    ('https://meetings.boardbook.org/Public/Agenda/123', None),
    ('https://www.google.com/url?q=https%3A%2F%2Fgo.boarddocs.com%2Ftx%2Fexample%2FBoard.nsf', ('boarddocs', 'https://go.boarddocs.com/tx/example/Board.nsf/Public')),
    ('https://school.edu/board/minutes', None),
    ('https://[malformed', None),
])
def test_vendor_link_classification(url, expected):
    assert classify_link(url) == expected


def test_duplicate_name_requires_matching_county():
    district = dict(tea_id='225906', school_district='Chapel Hill ISD', county_name='Titus County')
    common = dict(url='https://meetings.boardbook.org/Public/Organization/1', meetings=2,
                  texas='Texas 75455', sample='Mount Pleasant Texas 75455')
    wrong = dict(common, name='Chapel Hill ISD-Smith County')
    correct = dict(common, name='Chapel Hill ISD -Titus County')
    assert not match_district(district, [wrong], True)['sources']
    assert len(match_district(district, [correct], True)['sources']) == 1
    assert not match_district(district, [dict(common, name='Chapel Hill ISD')], True)['sources']


def test_archive_requires_documents_not_global_navigation():
    base = 'https://school.edu/board/minutes'
    markup = '<a id="content"></a><nav><a href="/board/minutes">Minutes</a></nav><main><h1>Board minutes</h1><a href="/docs/2025.pdf">January 2025 minutes</a></main>'
    soup = BeautifulSoup(markup, 'lxml')
    assert document_evidence(soup, links_from(soup, base), base, 1)
    soup = BeautifulSoup('<nav><a href="/board/minutes">Minutes</a></nav><main><h1>Staff</h1></main>', 'lxml')
    assert not document_evidence(soup, links_from(soup, base), base, 1)


def test_cms_markup_identifies_hosted_vendor():
    assert detect_cms('<script src="https://cmsv2-assets.apptegy.net/app.js"></script>') == 'Apptegy/Thrillshare'
    assert detect_cms('<meta name="generator" content="WordPress">') == 'WordPress'


def test_county_districts_and_sands_are_distinct():
    assert normalize_name('Reagan County ISD') != normalize_name('Borden County ISD')
    assert normalize_name('West Hardin County CISD') != normalize_name('West ISD')
    assert normalize_name('S and S CISD') != normalize_name('Sands CISD')


def test_apptegy_embedded_menu_discovers_board_archive():
    state = dict(pathPrefix='/o/district', menu=[dict(type='header', name='School Board',
        children=[dict(type='page', name='Board Meeting Agendas', slug='agendas'),
                  dict(type='link', name='Minutes', url='https://meetings.boardbook.org/Public/Organization/1')])])
    script = 'window.clientWorkStateTemp = JSON.parse(' + json.dumps(json.dumps(state)) + ');'
    soup = BeautifulSoup('<script>' + script + '</script>', 'lxml')
    links = links_from(soup, 'https://school.edu/')
    assert ('https://school.edu/o/district/page/agendas', 'Board Meeting Agendas') in links
    assert ('https://meetings.boardbook.org/Public/Organization/1', 'Minutes') in links


@pytest.mark.parametrize('extra_link', ['', '<a href="https://drive.google.com/drive/folders/shac123">SHAC Posted Agendas</a>'])
def test_crawl_follows_board_url_when_link_label_is_generic(extra_link):
    from types import SimpleNamespace
    from detect_platform import crawl_district

    pages = {
        'https://example.edu/': '<title>Example ISD</title><a href="/schoolboard">Governance</a>' + extra_link,
        'https://example.edu/schoolboard': '<title>Board minutes</title><main><h1>Board minutes</h1><a href="/files/minutes.pdf">October minutes</a></main>',
    }

    class Site:
        def get(self, url, **kwargs):
            return SimpleNamespace(url=url, text=pages[url], status=200,
                                   raw_path='unused', from_cache=True)

    district = dict(tea_id='000001', school_district='Example ISD', website='https://example.edu/')
    result = crawl_district(Site(), district, [])
    assert [source['entry_url'] for source in result['sources']] == ['https://example.edu/schoolboard']


@pytest.mark.parametrize('url,expected', [
    ('https://drive.google.com/drive/folders/folder123?usp=sharing&resourcekey=0-demo',
     'https://drive.google.com/drive/folders/folder123?resourcekey=0-demo'),
    ('https://drive.google.com/embeddedfolderview?id=folder123&resourcekey=0-demo#grid',
     'https://drive.google.com/drive/folders/folder123?resourcekey=0-demo'),
    ('https://drive.google.com.evil.test/drive/folders/folder123', None),
])
def test_google_folder_links_preserve_public_access_key(url, expected):
    assert google_drive_folder(url) == expected
