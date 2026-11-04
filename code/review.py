#!/usr/bin/env python3
"""Independently reopen collected records and conservatively audit source/field/text fidelity.

The reviewer never imports the collector's parsers. ``reviewed`` is true only if
all checks pass; failed/unsupported/unavailable records retain their text but are
not represented as verified. Reopening sources is deliberate (no collector cache).
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
import re
import threading
import time
import unicodedata
from urllib.parse import urljoin, urlparse, parse_qs
import requests
from bs4 import BeautifulSoup

_AGENT = 'SchoolSafetyResearchReviewer/1.0 (public meeting record accuracy audit)'
_local = threading.local()
_listing_cache = {}
_county_cache = {}
_county_map = {}
_county_map_evidence = {}
_lock = threading.Lock()
REVIEWER_VERSION = 'independent-review-agent-v4'


def meeting_types(value, contextual=False):
    """Preserve combined types rather than accepting just one matching word."""
    text = normalized(value)
    text = re.sub(r'\bspecial called\b', 'special', text)
    if not contextual:
        return set(re.findall(r'\b(?:information session|work session|public hearing|regular|special|emergency|called|workshop|retreat)\b', text))
    kinds = set(re.findall(r'\b(?:information session|work session|public hearing|retreat)\b', text))
    kinds.update(re.findall(r'\b(regular|special|emergency|workshop)(?:\s+(?:board|trustees|school|business|committee|public|joint|of|the))*\s+(?:meeting|session)\b', text))
    if re.search(r'\bcalled\s+(?:board\s+)?meeting\b', text):
        kinds.add('called')
    return kinds


def normalized(text):
    return ' '.join(re.findall(r'\w+', unicodedata.normalize('NFKC', str(text)).casefold()))


def identity(text):
    text = normalized(text).replace(' and ', ' ')
    text = re.sub(r'\b(?:independent|consolidated|collegiate|common|municipal|school|district|schools|isd|cisd|csd|ccsd|msd|administration|trial)\b', ' ', text)
    text = re.sub(r'\bmt\b', 'mount', text)
    return ' '.join(text.split())


def load_county_map(path):
    global _county_map, _county_map_evidence
    data = Path(path).read_bytes()
    payload = json.loads(data)
    _county_map = {(normalized(r['school_district']), normalized(r['county_name'])): r for r in payload['rows']}
    _county_map_evidence = {'source_link': payload['source_link'], 'retrieved_at': payload['retrieved_at'], 'method': payload['method'], 'snapshot_sha256':sha256(data).hexdigest()}


def session():
    if not hasattr(_local, 'session'):
        _local.session = requests.Session()
        _local.session.headers['User-Agent'] = _AGENT
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        _local.session.mount('https://',HTTPAdapter(max_retries=Retry(total=3,backoff_factor=0.5,status_forcelist=[429,500,502,503,504],allowed_methods=['GET'])))
    return _local.session


def get(url, timeout=60):
    r = session().get(url, timeout=timeout)
    r.raise_for_status()
    return r


def text_from_response(response):
    """Separate, straightforward extraction, including linked PDF document viewers."""
    data = response.content
    content_type = response.headers.get('Content-Type', '').lower()
    evidence = {'response_url': response.url, 'source_sha256': sha256(data).hexdigest(), 'content_type': content_type}
    if data.startswith(b'%PDF') or 'application/pdf' in content_type:
        from pypdf import PdfReader
        pdf = PdfReader(BytesIO(data))
        text = '\n\n'.join(p.extract_text(extraction_mode='plain') or '' for p in pdf.pages)
        evidence['page_count'] = len(pdf.pages)
        evidence['parser'] = 'independent pypdf plain-text extraction; OCR not performed'
        return text, evidence
    if data.startswith(b'PK') or '.docx' in urlparse(response.url).path.lower():
        from docx import Document
        document = Document(BytesIO(data))
        text = '\n'.join([p.text for p in document.paragraphs] + [' | '.join(c.text for c in row.cells) for t in document.tables for row in t.rows])
        evidence['parser'] = 'independent DOCX paragraphs and tables'
        return text, evidence
    if data.startswith(b'\xd0\xcf\x11\xe0') or data.lstrip().startswith(b'{\\rtf'):
        converter = Path('/usr/bin/textutil')
        if not converter.is_file():
            raise ValueError('Binary DOC/RTF requires a local document converter; never decoded as HTML')
        suffix = '.rtf' if data.lstrip().startswith(b'{\\rtf') else '.doc'
        with tempfile.TemporaryDirectory(prefix='school-safety-independent-review-') as folder:
            document_path = Path(folder) / ('source' + suffix)
            document_path.write_bytes(data)
            converted = subprocess.run(
                [str(converter), '-convert', 'txt', '-stdout', '-encoding', 'UTF-8', str(document_path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=60,
            )
        text = converted.stdout.decode('utf-8', errors='strict').strip()
        if not text:
            raise ValueError('Independently reopened DOC/RTF converted to empty text')
        evidence['parser'] = 'independent local macOS textutil conversion from reopened ' + suffix
        evidence['converter'] = str(converter)
        evidence['converter_limitation'] = 'Independent fetch and conversion code; same system document renderer may be used by collection'
        return text, evidence
    soup = BeautifulSoup(data, 'html.parser')
    # Document links observed in the original HTML, never inferred document IDs.
    candidates = []
    for element in soup.find_all(['iframe', 'embed', 'object']):
        value = element.get('src') or element.get('data')
        if value and re.search(r'\.pdf(?:[?#]|$)|DownloadPDF', value, re.I):
            candidates.append(urljoin(response.url, value))
    doc_id = soup.find(id='NewDocumentViewerDocumentID')
    if doc_id:
        value = doc_id.get('value') or doc_id.get_text(strip=True)
        for script in soup.find_all('script'):
            match = re.search(r"loadUrlFormat\s*[:=]\s*['\"]([^'\"]+)['\"]", script.string or script.get_text())
            if match and 'TBR_AAAA_' in match.group(1):
                candidates.append(urljoin(response.url, match.group(1).replace('TBR_AAAA_', value)))
    if candidates:
        payload = get(candidates[0])
        text, linked_evidence = text_from_response(payload)
        evidence['linked_document'] = linked_evidence
        evidence['document_url_observed_in_viewer'] = candidates[0]
        return text, evidence
    heading = soup.select_one('#MeetingHeader')
    heading_text = heading.get_text(' ', strip=True) if heading else ''
    for node in soup(['script', 'style', 'nav', 'header', 'footer', 'noscript', 'button', 'input', 'select']):
        node.decompose()
    content = soup.select_one('#Tab-AgendaItem') or soup.find('main') or soup.select_one('#MainPage') or soup.body or soup
    for node in content.select('.back-to-top, .ShowOnOffline, #reloadMeetingButton, #agenda-tableofcontents-container, #Tab-WebViewer'):
        node.decompose()
    evidence['parser'] = 'independent HTML visible DOM text'
    evidence['title'] = soup.title.get_text(' ', strip=True) if soup.title else ''
    text = content.get_text(' ', strip=True)
    if heading_text and heading not in content.descendants:
        text = heading_text + ' ' + text
    if '/Meeting/Supplementals/' in response.url:
        documents = []
        seen = set()
        for anchor in content.find_all('a', href=True):
            document_url = urljoin(response.url, anchor['href'])
            if document_url not in seen and re.search(r'/Documents/|\.pdf|\.docx?(?:[?#]|$)', document_url, re.I):
                seen.add(document_url)
                document_text, document_evidence = text_from_response(get(document_url))
                text += ' ' + anchor.get_text(' ',strip=True) + ' ' + document_text
                documents.append(document_evidence)
        evidence['supplemental_documents'] = documents
    return text, evidence


def iso_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    value = str(value or '').replace('_',' ')
    if re.match(r'^\d{4}-\d{2}-\d{2}', value):
        return value[:10]
    month_names = 'January|February|March|April|May|June|July|August|September|October|November|December'
    ordinal = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?(' + month_names + r')\s*,?\s*(\d{4})',value,re.I)
    if ordinal:
        return datetime.strptime(' '.join(ordinal.groups()),'%d %B %Y').date().isoformat()
    match = re.search(r'\b(' + month_names + r')\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(\d{4})', value, re.I)
    if match:
        return datetime.strptime(' '.join(match.groups()), '%B %d %Y').date().isoformat()
    match = re.search(r'(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](20\d{2}|19\d{2})(?!\d)',value)
    if match:
        try:
            return date(int(match.group(3)),int(match.group(1)),int(match.group(2))).isoformat()
        except ValueError:
            pass
    return ''


def fetch_listing(url):
    with _lock:
        cached = _listing_cache.get(url)
    if cached:
        return cached
    response = get(url)
    soup = BeautifulSoup(response.content, 'html.parser')
    rows = []
    for tr in soup.find_all('tr'):
        cells = tr.find_all('td', recursive=False)
        if len(cells) < 3:
            continue
        row_text = cells[0].get_text(' ', strip=True)
        match = re.search(r'Meeting Type:\s*(.*)', row_text, re.I)
        label = ''
        if match:
            bold = cells[0].find(string=re.compile('Meeting Type:', re.I))
            if bold:
                label = bold.parent.parent.get_text(' ', strip=True)
                label = re.sub(r'^Meeting Type:\s*', '', label).strip()
        rows.append({'date': iso_date(row_text), 'meeting_type': label,
                     'links': [(urljoin(response.url, a.get('href', '')), a.get_text(' ', strip=True)) for a in cells[-1].find_all('a', href=True)],
                     'row_text': tr.get_text(' ', strip=True)})
    value = {'rows': rows, 'title': soup.title.get_text(' ', strip=True) if soup.title else '', 'url': response.url,
             'sha256': sha256(response.content).hexdigest(), 'text': soup.get_text(' ', strip=True)}
    with _lock:
        _listing_cache[url] = value
    return value


def _url_key(url):
    parsed = urlparse(url)
    return (parsed.netloc.lower(), parsed.path.lower().rstrip('/'), tuple(sorted((k, tuple(v)) for k,v in parse_qs(parsed.query).items())))


def verify_record(record):
    record = dict(record)
    checks = {}
    notes = []
    evidence = {'reviewer': REVIEWER_VERSION, 'reviewed_at_utc': datetime.now(timezone.utc).isoformat()}
    try:
        source = record.get('source_link') or record.get('source_url')
        text = record.get('text') or record.get('record_text') or record.get('information_text') or ''
        if not source:
            raise ValueError('Missing source_link')
        response = get(source)
        diligent = record.get('document_format') == 'diligent_json_html'
        diligent_payload = None
        diligent_document = None
        if diligent:
            diligent_payload = response.json()
            document_id = str(record.get('document_id') or urlparse(source).fragment.removeprefix('document='))
            diligent_document = next((d for d in diligent_payload.get('Documents',[]) if str(d.get('Id'))==document_id),None)
            if diligent_document is None or diligent_document.get('IsPublic') is not True:
                raise ValueError('Selected Diligent document absent or not explicitly public')
            html = diligent_document.get('Html') or diligent_document.get('AgendaCover') or ''
            document_soup = BeautifulSoup(html,'html.parser')
            for node in document_soup(['script','style','nav','header','footer','aside']):
                node.decompose()
            body = document_soup.find('main') or document_soup.find('article') or document_soup.select_one('#content, #MainContent') or document_soup.body or document_soup
            source_text = body.get_text(' ',strip=True)
            source_evidence = {'response_url':response.url,'source_sha256':sha256(response.content).hexdigest(),'document_id':document_id,'IsPublic':True,'parser':'independent Diligent public-document HTML extraction'}
        else:
            source_text, source_evidence = text_from_response(response)
        evidence['record_source'] = source_evidence
        checks['source_http_ok'] = True
        checks['no_source_date_conflict'] = 'source_label_date_conflict' not in (record.get('quality_flags') or [])
        observed, extracted = normalized(source_text), normalized(text)
        # Exact full text containment verifies every extracted word in its order.
        # Whitespace, case, Unicode compatibility characters and punctuation normalize.
        checks['complete_extraction_status'] = record.get('extraction_status', 'ok') == 'ok'
        checks['nonempty_text'] = len(extracted) >= 15
        checks['text_fidelity'] = bool(extracted) and extracted in observed
        if not checks['text_fidelity'] and extracted:
            # HTML text-node boundary artifacts are allowed only if all normalized
            # characters still occur consecutively (e.g. artificial line breaks).
            checks['text_fidelity'] = extracted.replace(' ', '') in observed.replace(' ', '')
        checks['text_completeness'] = bool(extracted) and extracted.replace(' ', '') == observed.replace(' ', '')
        evidence['extracted_text_sha256'] = sha256(str(text).encode()).hexdigest()
        evidence['normalized_extracted_characters'] = len(extracted)
        evidence['normalized_source_characters'] = len(observed)
        evidence['text_test'] = 'normalized extracted text equals independently extracted complete record body (punctuation/whitespace compatibility normalized)'
        boardbook = 'meetings.boardbook.org' in urlparse(source).netloc.lower()
        listing_url = record.get('listing_url') or record.get('organization_url') or record.get('discovery_url')
        if diligent:
            metadata_url = response.url.split('/meetingDocuments')[0] + '/meetingData'
            metadata_response = get(metadata_url)
            metadata = metadata_response.json()
            evidence['meeting_metadata_url'] = metadata_url
            evidence['meeting_metadata_sha256'] = sha256(metadata_response.content).hexdigest()
            evidence['meeting_metadata'] = metadata
            evidence['document_name'] = diligent_document.get('Name')
            checks['public_document'] = diligent_document.get('IsPublic') is True
            checks['source_district'] = identity(record.get('school_district','')) in identity(source_text)
            checks['date'] = iso_date(diligent_payload.get('MeetingDateFormatted')) == iso_date(record.get('date')) and bool(iso_date(record.get('date')))
            kind = {1:'Agenda',2:'Minutes',55:'Draft Minutes'}.get(diligent_document.get('DocumentType')) or {1:'Agenda',2:'Minutes'}.get(diligent_document.get('Type'))
            checks['detail'] = normalized(kind or '') == normalized(record.get('detail',''))
            mt = record.get('meeting_type')
            checks['meeting_type'] = bool(mt) and normalized(mt) in normalized(metadata.get('Name',''))
            observed_types = meeting_types(metadata.get('Name',''), contextual=True)
            checks['meeting_type_complete'] = bool(meeting_types(mt)) and meeting_types(mt) == observed_types
            evidence['independent_meeting_types'] = sorted(observed_types)
            checks['meeting_document_identity'] = normalized(str(diligent_document.get('Name',''))) == normalized(str(metadata.get('Name','')))
        elif boardbook and listing_url:
            listing = fetch_listing(listing_url)
            evidence['listing_sha256'] = listing['sha256']
            evidence['listing_url'] = listing['url']
            evidence['listing_title'] = listing['title']
            row = next((r for r in listing['rows'] if any(_url_key(u) == _url_key(source) for u, label in r['links'])), None)
            checks['linked_from_meeting_row'] = row is not None
            if row:
                label = next(label for u, label in row['links'] if _url_key(u) == _url_key(source))
                checks['detail'] = normalized(label) == normalized(record.get('detail', ''))
                checks['date'] = bool(row['date']) and row['date'] == iso_date(record.get('date'))
                checks['meeting_type'] = bool(row['meeting_type']) and normalized(row['meeting_type']) == normalized(record.get('meeting_type', ''))
                evidence['independently_observed_row'] = row
            expected_name = record.get('boardbook_name') or record.get('school_district') or ''
            checks['source_district'] = bool(identity(expected_name)) and identity(expected_name) in identity(listing['title'])
            expected_district = identity(record.get('school_district', ''))
            checks['district_mapping'] = bool(expected_district) and (expected_district in identity(expected_name) or identity(expected_name) in expected_district)
            if 'county' in normalized(expected_name):
                checks['county_label_identity'] = normalized(record.get('county_name','')) in normalized(expected_name)
            if expected_district == 'northside':
                checks['district_mapping'] = ('/2695' in listing_url and normalized(record.get('county_name','')) == 'wilbarger county')
            if expected_district == 'wylie' and '(collin county)' not in expected_name.casefold():
                checks['district_mapping'] = normalized(record.get('county_name','')) == 'taylor county'
            evidence['district_identity_basis'] = 'organization title and exact record hyperlink from organization meeting row'
            # County does not appear reliably on BoardBook; verify against district registry only if a provenance link is supplied.
            checks['county_provenance'] = bool(record.get('county_name') or record.get('county')) and bool(record.get('wikipedia_url') or record.get('district_wikipedia_url') or record.get('county_source_link'))
            if not checks['county_provenance']:
                notes.append('County lacks independently inspectable provenance; cannot fully verify all fields')
        else:
            # Official sites vary. Require the collected listing to visibly link the
            # record, and require date/type/district to appear in the document itself.
            listing = fetch_listing(listing_url) if listing_url else None
            if listing:
                raw = get(listing_url)
                listing_soup = BeautifulSoup(raw.content, 'html.parser')
                matching_anchors = [a for a in listing_soup.find_all('a',href=True) if _url_key(urljoin(raw.url,a['href']))==_url_key(source)]
                observed_label = ''
                observed_title = ''
                if matching_anchors:
                    anchor = matching_anchors[0]
                    previous_heading = anchor.find_previous(['h1','h2','h3','h4','h5','h6'])
                    observed_title = anchor.get_text(' ',strip=True)
                    observed_label = observed_title + ' ' + (previous_heading.get_text(' ',strip=True) if previous_heading else '')
                embedded_match = False
                for script in listing_soup.find_all('script',id='__NUXT_DATA__'):
                    try:
                        pool = json.loads(script.string or script.get_text())
                    except (ValueError,TypeError):
                        continue
                    for obj in pool:
                        if not isinstance(obj,dict) or 'url' not in obj or 'file_name' not in obj:
                            continue
                        u = pool[obj['url']] if isinstance(obj['url'],int) else obj['url']
                        label = pool[obj['file_name']] if isinstance(obj['file_name'],int) else obj['file_name']
                        if isinstance(u,str) and _url_key(u)==_url_key(source):
                            embedded_match = True
                            observed_label = str(label)
                            observed_title = str(label)
                checks['linked_from_official_listing'] = bool(matching_anchors) or embedded_match
                evidence['independently_observed_document_label'] = observed_label
                evidence['independently_observed_document_title'] = observed_title
                evidence['listing_url'] = raw.url
                evidence['listing_sha256'] = sha256(raw.content).hexdigest()
            else:
                checks['linked_from_official_listing'] = False
            expected = identity(record.get('school_district',''))
            checks['source_district'] = bool(expected) and expected in identity(source_text)
            checks['date'] = bool(iso_date(record.get('date'))) and iso_date(record.get('date')) in {iso_date(source_text),iso_date(evidence.get('independently_observed_document_label',''))}
            observed_kind = None
            for candidate in [evidence.get('independently_observed_document_title',''),source_text[:350],evidence.get('independently_observed_document_label','')]:
                hit = re.search(r'\b(minutes|agenda|projector)\b',candidate,re.I)
                if hit:
                    observed_kind = hit.group(1)
                    break
            checks['detail'] = bool(observed_kind) and normalized(record.get('detail','')) == normalized(observed_kind)
            evidence['independent_record_type'] = observed_kind
            link_date = iso_date(evidence.get('independently_observed_document_title',''))
            document_date = iso_date(source_text)
            checks['date_source_consistency'] = not (link_date and document_date and link_date != document_date)
            mt = record.get('meeting_type','')
            checks['meeting_type'] = bool(mt) and normalized(mt) in normalized(source_text[:3000])
            header_types = meeting_types(source_text[:600], contextual=True)
            checks['meeting_type_complete'] = bool(meeting_types(mt)) and (not header_types or meeting_types(mt) == header_types)
            evidence['independent_meeting_types'] = sorted(header_types)
            checks['county_provenance'] = bool(record.get('county_name') or record.get('county')) and bool(record.get('wikipedia_url') or record.get('district_wikipedia_url') or record.get('county_source_link'))
            evidence['fallback_review_policy'] = 'district/date/type must be supported by document or official listing; exact live linkage required'
        # A county source URL must be opened: mere existence is not verification.
        county_url = record.get('county_source_link') or record.get('wikipedia_url') or record.get('district_wikipedia_url')
        county_key = (normalized(record.get('school_district','')), normalized(record.get('county_name') or record.get('county') or ''))
        if _county_map:
            checks['county_provenance'] = county_key in _county_map
            evidence['county_source'] = _county_map_evidence
            evidence['county_observed_entry'] = _county_map.get(county_key)
        elif county_url:
            with _lock:
                county_cached = _county_cache.get(county_url)
            if county_cached is None:
                county_response = get(county_url)
                county_soup = BeautifulSoup(county_response.content,'html.parser')
                county_cached = (normalized(county_soup.get_text(' ',strip=True)), county_response.url, sha256(county_response.content).hexdigest())
                with _lock:
                    _county_cache[county_url] = county_cached
            county_text, county_resolved_url, county_hash = county_cached
            county = record.get('county_name') or record.get('county') or ''
            checks['county_provenance'] = bool(county) and normalized(county) in county_text
            evidence['county_source_link'] = county_resolved_url
            evidence['county_source_sha256'] = county_hash
        evidence['checks'] = checks
        record['reviewed'] = bool(checks) and all(checks.values())
        record['review_status'] = 'verified' if record['reviewed'] else 'needs_review'
        failed = [k for k,v in checks.items() if not v]
        if failed:
            notes.append('Failed checks: ' + ', '.join(failed))
    except Exception as exc:
        record['reviewed'] = False
        record['review_status'] = 'error'
        notes.append(f'{type(exc).__name__}: {exc}')
        evidence['checks'] = checks
    record['review_notes'] = '; '.join(notes)
    record['review_evidence'] = json.dumps(evidence,ensure_ascii=False,default=str)
    return record


def review_records(records, workers=4):
    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(verify_record, records))


def watch_records(input_dir, output_dir, workers=6, stop_file='data/collection-complete.json', poll_seconds=10, retry_unverified=False):
    directory = Path(input_dir)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    policy_path = Path('data/review-policy.json')
    policy_path.parent.mkdir(parents=True,exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    policy_path.write_text(json.dumps({'reviewer_version':REVIEWER_VERSION,'pid':os.getpid(),'started_at_utc':started,'requires_complete_text_equality':True,'county_map_provenance':_county_map_evidence,'ocr_policy':'OCR-only text requires independent visual review; never verified solely by OCR equality'},indent=2))
    worker_path = Path('data/workers/reviewer.json')
    worker_path.parent.mkdir(parents=True,exist_ok=True)
    worker_path.write_text(json.dumps({'pid':os.getpid(),'role':'reviewer','started_at':started,'reviewer_version':REVIEWER_VERSION}))
    processed = set()
    for path in output.glob('*.json'):
        try:
            item = json.loads(path.read_text())
            audit = json.loads(item.get('review_evidence') or '{}')
            if audit.get('reviewer') == REVIEWER_VERSION and (not retry_unverified or item.get('reviewed') is True):
                processed.add(path.stem)
        except (OSError, ValueError):
            continue
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while True:
            if shutil.disk_usage(output).free < 5 * 1024**3:
                print(json.dumps({'status':'waiting_disk_space','free_bytes':shutil.disk_usage(output).free}),flush=True)
                time.sleep(min(poll_seconds,30))
                continue
            def needs_review(path):
                target = output / path.name
                try:
                    return path.stem not in processed or not target.exists() or path.stat().st_mtime_ns > target.stat().st_mtime_ns
                except OSError:
                    return False
            pending = [p for p in directory.glob('*.json') if needs_review(p)]
            if pending:
                def work(path):
                    try:
                        original_input = path.read_bytes()
                        record = json.loads(original_input)
                    except (OSError,ValueError):
                        return None, 'input_changed_during_review'
                    reviewed = verify_record(record)
                    try:
                        if path.read_bytes() != original_input:
                            return None, 'input_changed_during_review'
                    except OSError:
                        return None, 'input_changed_during_review'
                    fields = {k:v for k,v in reviewed.items() if k.startswith('review')}
                    fields['record_id'] = reviewed.get('record_id',path.stem)
                    target = output / (path.stem + '.json')
                    temporary = target.with_suffix('.tmp')
                    temporary.write_text(json.dumps(fields,ensure_ascii=False,default=str))
                    temporary.replace(target)
                    return path.stem, fields.get('review_status')
                for record_id, status in executor.map(work,pending):
                    if record_id is not None:
                        processed.add(record_id)
                    if record_id is not None and len(processed)%100==0:
                        print(json.dumps({'reviewed_records':len(processed),'last_status':status}),flush=True)
            elif Path(stop_file).exists():
                break
            else:
                time.sleep(min(poll_seconds,30))
    print(json.dumps({'reviewed_records':len(processed),'status':'complete'}),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--retry-unverified', action='store_true')
    parser.add_argument('--stop-file', default='data/collection-complete.json')
    parser.add_argument('--county-map', default='data/reviewer-county-map.json')
    parser.add_argument('--output', required=True)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    if Path(args.county_map).exists():
        load_county_map(args.county_map)
    if args.watch:
        return watch_records(args.input,args.output,args.workers,args.stop_file,retry_unverified=args.retry_unverified)
    path = Path(args.input)
    if path.suffix == '.parquet':
        import pyarrow.parquet as pq
        records = pq.read_table(path).to_pylist()
    else:
        records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if args.limit:
        records = records[:args.limit]
    result = review_records(records,args.workers)
    output = Path(args.output)
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.suffix == '.parquet':
        import pyarrow as pa
        import pyarrow.parquet as pq
        pq.write_table(pa.Table.from_pylist(result),output,compression='zstd')
    else:
        output.write_text(''.join(json.dumps(r,default=str,ensure_ascii=False)+'\n' for r in result))
    counts = {}
    for r in result:
        counts[r['review_status']] = counts.get(r['review_status'],0)+1
    print(json.dumps({'records':len(result),'review_status':counts,'output':str(output)}))

if __name__=='__main__':
    main()
