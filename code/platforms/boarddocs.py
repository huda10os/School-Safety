"""BoardDocs public meeting lists and documents."""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup
from extract import clean_text
from records import make_record

PLATFORM = 'boarddocs'


def _base(url):
    parts = urlsplit(url)
    match = re.match(r'(?i)^(.*?/board\.nsf)/', parts.path)
    if not match:
        raise ValueError(f'Not a BoardDocs public URL: {url}')
    return f'{parts.scheme}://{parts.netloc}{match.group(1)}/'


def _soup(response):
    return BeautifulSoup(response.text, 'html.parser')


def _visible(soup):
    for node in soup.select('script, style, noscript, [hidden], [aria-hidden="true"], .sr-only'):
        node.decompose()
    for node in soup.select('[style]'):
        if re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', node.get('style', ''), re.I):
            node.decompose()
    return clean_text(soup.get_text('\n', strip=True))


def discover(district, entry_url, fetcher):
    base = _base(entry_url)
    options = _soup(fetcher.get(entry_url)).select('option[value]')
    committees = [(o.get('value', '').strip(), o.get_text(' ', strip=True)) for o in options]
    committees = [(key, label) for key, label in committees if key and re.fullmatch(
        r'(?:board of (?:trustees|education)|main governing board|governing board)', label.strip(), re.I)]
    if not committees:
        raise ValueError('No identifiable governing board committee on public page')
    records = []
    for committee, label in committees:
        list_url = urljoin(base, 'BD-GetMeetingsList?open')
        form = {'current_committee_id': committee}
        for meeting in fetcher.post(list_url, form).json():
            meeting_id = meeting.get('unique')
            if not meeting_id:
                continue
            detail_form = {'id': meeting_id, 'current_committee_id': committee}
            meeting_url = urljoin(base, 'BD-GetMeeting?open')
            view = _soup(fetcher.post(meeting_url, detail_form))
            raw_date = str(meeting.get('numberdate') or '')
            try:
                date = datetime.strptime(raw_date, '%Y%m%d').date().isoformat()
            except ValueError:
                date = None
            title = meeting.get('name') or None
            match = re.search(r'\b(regular|special|workshop|work\s*study|called|emergency|organizational)\b', title or '', re.I)
            source = urljoin(base, 'goto?open&id=' + quote(meeting_id, safe=''))
            provenance = json.dumps({'committee': label, 'list_form': form, 'meeting': meeting,
                                     'meeting_url': meeting_url, 'meeting_form': detail_form}, ensure_ascii=False)
            for detail, selector in [('Agenda', '#btn-view-agenda'), ('Minutes', '.btn-view-minutes')]:
                if view.select_one(selector):
                    records.append(make_record(district, PLATFORM, date=date, date_raw=raw_date or None,
                        meeting_type=match.group(0) if match else None, meeting_title=title,
                        meeting_id=meeting_id, detail=detail, detail_raw=detail, source_link=source,
                        listing_url=list_url, listing_row_text=provenance))
    return records


def resolve(record, fetcher):
    base = _base(record['source_link'])
    form = json.loads(record['listing_row_text'])['meeting_form']
    endpoint = 'PRINT-AgendaDetailed?open' if record['detail'] == 'Agenda' else 'BD-GetMinutes?open'
    url = urljoin(base, endpoint)
    response = fetcher.post(url, form)
    content_type = response.headers.get('Content-Type', response.headers.get('content-type', ''))
    result = dict(document_link=url, content_type=content_type, raw_path=response.raw_path,
                  content_sha256=response.sha256, attachment_links=[])
    if response.content.startswith(b'%PDF') or 'pdf' in content_type.lower():
        return result
    soup = _soup(response)
    for link in soup.select('.print-files a[href], .public-file a[href]'):
        href = link.get('href', '')
        if href and not href.startswith(('#', 'javascript:', 'mailto:')):
            result['attachment_links'].append(urljoin(base, href))
    result.update(text=_visible(soup) or None, text_method='html')
    return result
