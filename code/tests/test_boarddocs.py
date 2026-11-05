import json
from pathlib import Path
from types import SimpleNamespace

from platforms import boarddocs

FIXTURES = Path(__file__).parent / 'fixtures' / 'boarddocs'
BASE = 'https://go.boarddocs.com/tx/example/Board.nsf/'


def response(body):
    return SimpleNamespace(text=body, content=body.encode(), headers={'Content-Type': 'text/html'},
                           raw_path='data/raw/example.gz', sha256='abc', json=lambda: json.loads(body))


class Fetcher:
    def get(self, url):
        return response((FIXTURES / 'public.html').read_text())

    def post(self, url, data):
        if 'GetMeetingsList' in url:
            assert data == {'current_committee_id': 'BOARD'}
            return response('[{"unique":"M1","name":"Regular Meeting","numberdate":"20240102"}]')
        if 'GetMeeting?' in url:
            return response((FIXTURES / 'meeting.html').read_text())
        return response((FIXTURES / 'agenda.html').read_text())


def test_discover_and_resolve_public_board_documents():
    district = {'tea_id': '123456', 'school_district': 'Example', 'county_name': 'Example'}
    records = boarddocs.discover(district, BASE + 'Public', Fetcher())
    assert [row['detail'] for row in records] == ['Agenda', 'Minutes']
    assert records[0]['date'] == '2024-01-02'
    assert records[0]['meeting_title'] == 'Regular Meeting'
    assert records[0]['source_link'] == BASE + 'goto?open&id=M1'
    assert json.loads(records[0]['listing_row_text'])['meeting_form']['current_committee_id'] == 'BOARD'
    result = boarddocs.resolve(records[0], Fetcher())
    assert result['text'] == 'Call to Order\nPolicy\nBudget'
    assert result['attachment_links'] == [BASE + 'files/agenda.pdf']
    assert result['raw_path'] == 'data/raw/example.gz'


def test_absent_minutes_do_not_create_a_record():
    class AgendaOnly(Fetcher):
        def post(self, url, data):
            if 'GetMeeting?' in url:
                return response('<a id="btn-view-agenda">Agenda</a>')
            return super().post(url, data)

    district = {'tea_id': '123456', 'school_district': 'Example', 'county_name': 'Example'}
    records = boarddocs.discover(district, BASE + 'Public', AgendaOnly())
    assert [row['detail'] for row in records] == ['Agenda']


def test_pdf_minutes_are_left_for_file_extraction():
    class PdfFetcher(Fetcher):
        def post(self, url, data):
            assert 'BD-GetMinutes?open' in url
            result = response('')
            result.content = b'%PDF-1.4'
            result.headers = {'Content-Type': 'application/pdf'}
            return result

    record = {'source_link': BASE + 'goto?open&id=M1', 'detail': 'Minutes',
              'listing_row_text': json.dumps({'meeting_form': {'id': 'M1', 'current_committee_id': 'BOARD'}})}
    result = boarddocs.resolve(record, PdfFetcher())
    assert result['raw_path'] == 'data/raw/example.gz'
    assert 'text' not in result
