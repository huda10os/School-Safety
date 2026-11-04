"""Public district website discovery and conservative meeting-record collection.

Adapters: Apptegy/Nuxt document folders, Finalsite resource collections, direct
PDF/DOCX/HTML links. Dynamic or inaccessible archives are reported as gaps.
No page is treated as minutes solely because a site navigation mentions minutes.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import io
import json
import re
import time
import zipfile
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
from xml.etree import ElementTree as ET
from bs4 import BeautifulSoup

SEED_SOURCES = {
    'Apple Springs Independent School District': 'https://www.asisd.com/documents/board-meeting-minutes/52780',
    'Austin Independent School District': 'https://austin-isd.community.diligentoneplatform.com/portal/',
    'Aledo Independent School District': 'https://www.aledoisd.org/school-board/school-board/board-meeting-bulletinsminutes',
    'Walnut Springs Independent School District': 'https://www.walnutspringsisd.net/district',
    'Neches Independent School District': 'https://www.nechesisd.com/',
}
MONTHS = '|'.join(['January','February','March','April','May','June','July','August','September','October','November','December'])
DATE_RX = re.compile(r'\b('+MONTHS+r')\s+(\d{1,2})(?:st|nd|rd|th)?\s*,?\s*(20\d{2}|19\d{2})\b', re.I)
NAV_RX = re.compile(r'board|trustee|meeting|minute|agenda', re.I)
DOC_RX = re.compile(r'\.(pdf|docx?|txt)(?:\?|$)', re.I)


def extract_date(text):
    """Return an explicit source date; never fill a missing year from scrape time."""
    text = re.sub(r'[_\xa0]', ' ', text)
    ordinal=re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:day\s+of\s+)?('+MONTHS+r')\s*,?\s*(20\d{2}|19\d{2})\b',text,re.I)
    if ordinal:
        try:return dt.datetime.strptime(' '.join([ordinal.group(2),ordinal.group(1),ordinal.group(3)]),'%B %d %Y').date().isoformat()
        except ValueError:pass
    m = DATE_RX.search(text)
    if m:
        try: return dt.datetime.strptime(' '.join(m.groups()), '%B %d %Y').date().isoformat()
        except ValueError: pass
    for pat, fmt in [(r'\b(20\d{2}-\d{2}-\d{2})\b','%Y-%m-%d'),
                     (r'\b(\d{1,2}/\d{1,2}/(?:20\d{2}|19\d{2}))\b','%m/%d/%Y'),
                     (r'\b(\d{1,2}/\d{1,2}/\d{2})\b','%m/%d/%y')]:
        m = re.search(pat, text)
        if m:
            try: return dt.datetime.strptime(m.group(1),fmt).date().isoformat()
            except ValueError: pass
    return None


def extract_document(content, content_type='', url=''):
    """Extract the full document text; scanned PDFs remain explicitly empty."""
    if content.startswith(b'%PDF') or 'application/pdf' in content_type:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        pages=[p.extract_text() or '' for p in reader.pages]
        if any(not p.strip() for p in pages):
            from ocr import extract_pdf
            result=extract_pdf(content)
            method='pdf_ocr' if result['ocr_performed'] else 'pdf_ocr_required'
            if result['empty_pages']:method='pdf_partial_ocr_required'
            return result['text'].strip(),method
        return '\n\n'.join(pages).strip(), 'pdf'
    if content.startswith(b'PK'):
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            root=ET.fromstring(z.read('word/document.xml'))
        ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n'.join(''.join(t.text or '' for t in p.findall('.//w:t',ns)) for p in root.findall('.//w:p',ns)).strip(),'docx'
    if content.startswith(b'\xd0\xcf\x11\xe0') or content.lstrip().startswith(b'{\\rtf'):
        import platform,subprocess,tempfile
        if platform.system()!='Darwin':raise ValueError('Legacy DOC/RTF conversion requires macOS textutil or a separately configured converter')
        suffix='.rtf' if content.lstrip().startswith(b'{\\rtf') else '.doc'
        with tempfile.NamedTemporaryFile(suffix=suffix) as source:
            source.write(content);source.flush()
            converted=subprocess.run(['/usr/bin/textutil','-convert','txt','-stdout','-encoding','UTF-8',source.name],capture_output=True,check=True,timeout=60)
        text=converted.stdout.decode('utf-8',errors='strict').strip()
        if not text:raise ValueError('Legacy DOC/RTF converter returned empty text')
        return text,'rtf_textutil' if suffix=='.rtf' else 'legacy_doc_textutil'
    soup=BeautifulSoup(content,'html.parser')
    for e in soup.select('script,style,nav,header,footer,aside'): e.decompose()
    main=soup.select_one('main,article,#content,#MainContent') or soup.body or soup
    return main.get_text('\n',strip=True), 'html'


class Fetcher:
    def __init__(self,session,cache_dir):
        self.session=session; self.root=Path(cache_dir); self.root.mkdir(parents=True,exist_ok=True)
    def get(self,url):
        key=hashlib.sha256(url.encode()).hexdigest()
        raw=self.root/(key+'.bin'); meta=self.root/(key+'.json')
        if raw.exists() and meta.exists():
            try:
                m=json.loads(meta.read_text());cached=raw.read_bytes()
                if hashlib.sha256(cached).hexdigest()==m.get('sha256'):return cached,m
            except (OSError,ValueError):pass
        try:
            from runtime import wait_for_storage
            wait_for_storage()
        except ImportError:
            import shutil
            while shutil.disk_usage(self.root).free < 5*1024**3:
                time.sleep(30)
        request_url='https://'+url[7:] if url.startswith('http://') else url
        r=self.session.get(request_url,timeout=35); r.raise_for_status()
        m={'requested_url':url,'url':r.url,'content_type':r.headers.get('content-type',''),'fetched_at':dt.datetime.now(dt.timezone.utc).isoformat(),'sha256':hashlib.sha256(r.content).hexdigest(),'raw_path':str(raw)}
        import os,threading
        suffix='.'+str(os.getpid())+'.'+str(threading.get_ident())+'.tmp'
        raw_temp=raw.with_suffix(raw.suffix+suffix);meta_temp=meta.with_suffix(meta.suffix+suffix)
        raw_temp.write_bytes(r.content);raw_temp.replace(raw)
        meta_temp.write_text(json.dumps(m,indent=2));meta_temp.replace(meta)
        return r.content,m


def official_website(session,wikipedia_url,cache_dir):
    fetch=Fetcher(session,cache_dir); body,meta=fetch.get(wikipedia_url)
    soup=BeautifulSoup(body,'html.parser')
    for tr in soup.select('table.infobox tr'):
        th=tr.find('th')
        if th and th.get_text(' ',strip=True).lower() in ('website','web site'):
            for a in tr.select('a[href]'):
                u=urljoin(meta['url'],a['href'])
                if urlparse(u).scheme in ('http','https') and not urlparse(u).hostname.endswith('wikipedia.org'): return u
    return None


def _nuxt_documents(soup):
    script=soup.find('script',id='__NUXT_DATA__')
    if not script:return []
    try: pool=json.loads(script.string or script.get_text())
    except (ValueError,TypeError): return []
    out=[]
    # Nuxt uses an indexed devalue pool. Read only explicit document entries.
    for obj in pool:
        if not isinstance(obj,dict) or not {'file_name','url'}.issubset(obj):continue
        def val(k):
            n=obj.get(k)
            return pool[n] if isinstance(n,int) and 0<=n<len(pool) else n
        u=val('url');title=val('file_name')
        if isinstance(u,str) and u.startswith(('https://','http://')):
            out.append({'url':u,'title':str(title),'context':str(title),'detail_hint':None,'meeting_type_hint':None})
    return out


def _anchor_candidates(soup,page_url):
    candidates=[]
    main=soup.select_one('main') or soup
    for a in main.select('a[href]'):
        u=urljoin(page_url,a['href']);title=a.get_text(' ',strip=True)
        if urlparse(u).scheme not in ('http','https'):continue
        if not DOC_RX.search(u):continue
        parent=a.find_parent(['section','article','tr','li','div'])
        context=parent.get_text(' ',strip=True)[:2000] if parent else title
        if NAV_RX.search(title+' '+u) or DATE_RX.search(title) or re.search(r'\b('+MONTHS+r')\s+\d{1,2}',title,re.I):
            heading=a.find_parent(['h1','h2','h3','h4','h5','h6'])
            previous=(heading.find_previous(['h1','h2','h3','h4','h5','h6']) if heading else a.find_previous(['h1','h2','h3','h4','h5','h6']))
            heading_text=previous.get_text(' ',strip=True) if previous else ''
            hint=_classify(heading_text,'') if len(heading_text)<120 else None
            year_match=re.search(r'\b(20\d{2})\b',heading_text)
            date_hint=extract_date(title+' '+year_match.group(1)) if hint and year_match else None
            candidates.append({'url':u,'title':title,'context':context,'detail_hint':hint,'date_hint':date_hint,'meeting_type_hint':None})
    return candidates


def _classify(title,text,hint=None):
    if re.search(r'\bresolution\b',title,re.I):return 'Extra: Resolution'
    for token,detail in [('minutes','Minutes'),('agenda','Agenda'),('projector','Projector')]:
        if re.search(r'\b'+token+r'\b',title,re.I):return detail
    if hint:return hint
    # Use title first; full document body may cite previous minutes/agenda.
    for sample in (title,text[:1000]):
        if re.search(r'\bminutes\b',sample,re.I):return 'Minutes'
        if re.search(r'\bagenda\b',sample,re.I):return 'Agenda'
        if re.search(r'\bprojector\b',sample,re.I):return 'Projector'
    return None


def collect_fallback(session,district,cache_dir,max_pages=20,max_records=None,**kwargs):
    """Return records, coverage and errors for a Wikipedia district mapping.

    max_pages bounds generic site discovery, not linked document downloads.
    max_records defaults to unlimited and is recorded as a truncation if set.
    Empty text and unknown dates/types are retained with explicit quality flags.
    """
    name=district.get('school_district') or district.get('name')
    county=district.get('county_name') or district.get('county')
    wikipedia=district.get('wikipedia_url') or district.get('url')
    fetch=Fetcher(session,cache_dir); errors=[]; records=[]; evidence=[]
    source=district.get('archive_url') or SEED_SOURCES.get(name) or district.get('official_website') or district.get('official_url')
    if not source and wikipedia:
        try:source=official_website(session,wikipedia,cache_dir)
        except Exception as e:errors.append({'stage':'official_website','url':wikipedia,'error':str(e)})
    if not source:return {'records':[],'coverage':{'status':'official_website_not_found','school_district':name},'errors':errors}
    if 'diligentoneplatform.com' in source:
        return collect_diligent(session,district,cache_dir,source,max_records=max_records)
    queue=deque([source]);seen=set();doc_seen=set();candidates=[]
    while queue and len(seen)<max_pages:
        url=queue.popleft()
        if url in seen:continue
        seen.add(url)
        try:body,meta=fetch.get(url)
        except Exception as e:
            errors.append({'stage':'discovery','url':url,'error':str(e)});continue
        page_url=meta['url'];soup=BeautifulSoup(body,'html.parser');evidence.append(page_url)
        if 'diligentoneplatform.com' in page_url:
            hosted=collect_diligent(session,district,cache_dir,page_url,max_records=max_records)
            records.extend(hosted['records']);errors.extend(hosted['errors']);continue
        if re.search(r'meetings\.boardbook\.org/public/organization/[^/?]+',page_url,re.I):
            try:
                from boardbook import discover_meetings,extract_record
                def response_fetch(u):
                    import requests
                    b,m=fetch.get(u);r=requests.Response();r.status_code=200;r.url=m['url'];r._content=b;r.headers['Content-Type']=m['content_type'];return r
                hosted=discover_meetings(session,page_url,name,county,fetch=response_fetch)
                for item in hosted:
                    extraction=extract_record(session,item['source_link'],fetch=response_fetch)
                    records.append({**item,**extraction,'reviewed':False,'discovery_url':source,'document_format':'boardbook_hosted'})
            except Exception as e:errors.append({'stage':'boardbook_hosted','url':page_url,'error':str(e)})
            continue
        if DOC_RX.search(page_url):candidates.append({'url':page_url,'title':'','context':'','detail_hint':None,'meeting_type_hint':None,'discovery_url':url});continue
        found=_nuxt_documents(soup)+_anchor_candidates(soup,page_url)
        for c in found:c['discovery_url']=page_url
        candidates.extend(found)
        # Finalsite resource collections explicitly linked in page DOM.
        for a in soup.select('a[data-resource-collection-id]'):
            element=a.find_parent(attrs={'data-source-element-id':True})
            if not element:continue
            eid=element['data-source-element-id'];pid=element.get('data-source-page-id','')
            from urllib.parse import urlencode
            child=urljoin(page_url,'/fs/elements/'+eid)+'?'+urlencode({'element_id':eid,'page_id':pid,'resource_collection':a['data-resource-collection-id'],'display_in_element':'true'})
            if child not in seen:queue.append(child)
        for a in soup.select('a[href]'):
            href=a['href'];label=a.get_text(' ',strip=True);u=urldefrag(urljoin(page_url,href))[0]
            if any(bad in u.lower() for bad in ['college-board','advanced-academics','special-programs','dweic','growth-committee']):continue
            if not NAV_RX.search(label+' '+href) or u in seen or DOC_RX.search(u):continue
            if urlparse(u).scheme not in ('http','https') or href.startswith('#'):continue
            if urlparse(u).hostname==urlparse(page_url).hostname or any(h in (urlparse(u).hostname or '') for h in ['boardbook.org','diligentoneplatform.com','boarddocs.com','simbli.eboardsolutions.com','.sites.thrillshare.com','.finalsite.com','.edlio.com','.gabbart.com']):queue.append(u)
    # Download each source once. Same PDF can be mislabeled by multiple anchors.
    for c in candidates:
        if c['url'] in doc_seen:continue
        doc_seen.add(c['url'])
        if max_records is not None and len(records)>=max_records:break
        try:
            raw,meta=fetch.get(c['url']);text,fmt=extract_document(raw,meta['content_type'],meta['url'])
            detail=_classify(c['title'],text,c.get('detail_hint'))
            if not detail:continue
            date=extract_date(text[:1500]) or extract_date(c['title']) or c.get('date_hint')
            mt=c.get('meeting_type_hint')
            m=re.search(r'\b(regular|special|emergency|called|workshop|work session)\b',c['title']+' '+text[:1500],re.I)
            if not mt and m:mt=m.group(1).title()
            flags=[]
            if not date:flags.append('date_not_found')
            if not mt:flags.append('meeting_type_not_found')
            if not text:flags.append('empty_text_requires_ocr')
            records.append({'school_district':name,'county_name':county,'date':date,'detail':detail,'meeting_type':mt,'text':text,'source_link':meta['url'],'reviewed':False,'discovery_url':c['discovery_url'],'source_title':c['title'],'source_context':c['context'],'document_format':fmt,'extraction_method':fmt,'ocr_performed':fmt.startswith('pdf_ocr'),'raw_path':meta['raw_path'],'raw_sha256':meta['sha256'],'quality_flags':flags,'fetched_at':meta['fetched_at']})
        except Exception as e:errors.append({'stage':'document','url':c['url'],'error':str(e)})
    dates=[r['date'] for r in records if r['date']]
    gaps=[]
    if queue:gaps.append('discovery_page_limit_reached')
    if max_records is not None and len(records)>=max_records:gaps.append('record_limit_reached')
    if any('diligentoneplatform.com' in u for u in seen) and not records:gaps.append('diligent_dynamic_portal_adapter_required')
    if any('boardbook.org' in u for u in seen):gaps.append('boardbook_discovered_use_boardbook_adapter')
    if not records:gaps.append('no_extractable_records_found')
    return {'records':records,'coverage':{'school_district':name,'county_name':county,'source_platform':'district_website','official_website':source,'status':'partial' if gaps or errors else 'collected','record_count':len(records),'date_start':min(dates) if dates else None,'date_end':max(dates) if dates else None,'discovery_pages':evidence,'coverage_flags':gaps,'discovery_complete':False},'errors':errors}


def collect_diligent(session,district,cache_dir,portal,max_records=None):
    """Read public Diligent meeting documents through its observed GET services.

    Only documents explicitly marked IsPublic are collected. Source URL points
    to the public service, with document ID retained to disambiguate its array.
    """
    fetch=Fetcher(session,cache_dir);name=district.get('school_district') or district.get('name')
    county=district.get('county_name') or district.get('county');records=[];errors=[];ids={};seen=[]
    base=f'{urlparse(portal).scheme}://{urlparse(portal).netloc}'
    try:
        raw,_=fetch.get(base+'/Portal/MeetingTypeList.aspx');soup=BeautifulSoup(raw,'html.parser')
        pages={urljoin(base,a['href']) for a in soup.select('a[href]') if '/Portal/MeetingInformation.aspx?' in a['href']}
        for page in sorted(pages):
            try:
                raw,_=fetch.get(page);seen.append(page);soup=BeautifulSoup(raw,'html.parser')
                for button in soup.select('button[onclick]'):
                    m=re.search(r'meetingClick\(this,\s*(\d+)',button.get('onclick',''))
                    if not m:continue
                    label=button.select_one('.meeting-list-item-button-name')
                    ids[m.group(1)]=label.get_text(' ',strip=True) if label else button.get_text(' ',strip=True)
            except Exception as e:errors.append({'stage':'diligent_meeting_list','url':page,'error':str(e)})
        for mid,title in ids.items():
            if max_records is not None and len(records)>=max_records:break
            url=base+'/Services/MeetingsService.svc/meetings/'+mid+'/meetingDocuments'
            try:
                raw,meta=fetch.get(url);data=json.loads(raw)
                for document in data.get('Documents',[]):
                    if document.get('IsPublic') is not True:continue
                    dtype=document.get('DocumentType');kind={1:'Agenda',2:'Minutes',55:'Draft Minutes'}.get(dtype)
                    if not kind:kind={1:'Agenda',2:'Minutes'}.get(document.get('Type'))
                    if not kind:continue
                    html=document.get('Html') or document.get('AgendaCover') or ''
                    if not html:continue
                    text,_=extract_document(html.encode(),'text/html',url)
                    date=extract_date(data.get('MeetingDateFormatted','')) or extract_date(text[:2000])
                    mt_match=re.search(r'\b(regular|special|emergency|called|workshop|work session|retreat|public hearing|information session)\b',title,re.I)
                    flags=[]
                    if not text:flags.append('empty_text')
                    if not date:flags.append('date_not_found')
                    mt=mt_match.group(1).title() if mt_match else None
                    if not mt:flags.append('meeting_type_not_found')
                    # A fragment identifies the exact document while keeping GET reproducible.
                    records.append({'school_district':name,'county_name':county,'date':date,'detail':kind,'meeting_type':mt,'text':text,'source_link':url+'#document='+str(document['Id']),'reviewed':False,'discovery_url':base+'/Portal/MeetingInformation.aspx?Id='+mid,'source_title':document.get('Name',title),'source_context':title,'document_format':'diligent_json_html','document_id':str(document['Id']),'raw_path':meta['raw_path'],'raw_sha256':meta['sha256'],'quality_flags':flags,'fetched_at':meta['fetched_at']})
            except Exception as e:errors.append({'stage':'diligent_document','url':url,'error':str(e)})
    except Exception as e:errors.append({'stage':'diligent_discovery','url':portal,'error':str(e)})
    dates=[r['date'] for r in records if r['date']]
    return {'records':records,'coverage':{'school_district':name,'county_name':county,'official_website':portal,'status':'partial' if errors else 'collected','record_count':len(records),'date_start':min(dates) if dates else None,'date_end':max(dates) if dates else None,'discovery_pages':seen,'meetings_discovered':len(ids),'coverage_flags':['record_limit_reached'] if max_records and len(records)>=max_records else [],'discovery_complete':not bool(errors)},'errors':errors}


def repair_staged_fallback(root=None,workers=2):
    """Re-extract failed/scanned fallback records from retained raw documents.

    Uses local OCR, keeps changed rows unreviewed, and invalidates stale review
    artifacts. Does not substitute the title for missing record text.
    """
    from concurrent.futures import ThreadPoolExecutor
    root=Path(root or Path(__file__).resolve().parents[1]);data=root/'data'
    from runtime import register
    register('fallback-repair')
    paths=[]
    for path in (data/'staging/records').glob('*.json'):
        record=json.loads(path.read_text())
        if record.get('platform')!='district_website':continue
        if record.get('document_format')=='diligent_json_html':continue
        if record.get('raw_path') and (not record.get('text') or 'ocr_required' in record.get('document_format','')):paths.append(path)
    def one(path):
        from runtime import wait_for_storage
        wait_for_storage()
        record=json.loads(path.read_text());rawpath=Path(record['raw_path'])
        try:
            raw=rawpath.read_bytes();text,fmt=extract_document(raw,'',record['source_link'])
            if not text:return {'record_id':record['record_id'],'status':'still_empty'}
            record['text']=text;record['document_format']=fmt;record['extraction_method']=fmt;record['ocr_performed']=fmt.startswith('pdf_ocr');record['reviewed']=False
            record['extraction_status']='partial_ocr_required' if 'partial_ocr' in fmt else 'ok'
            date=extract_date(text[:1500]) or extract_date(record.get('source_title',''))
            if date:record['date']=date
            mt=re.search(r'\b(regular|special|emergency|called|workshop|work session)\b',record.get('source_title','')+' '+text[:1500],re.I)
            if mt:record['meeting_type']=mt.group(1).title()
            record['quality_flags']=[flag for flag in record.get('quality_flags',[]) if flag!='empty_text_requires_ocr' and not (flag=='date_not_found' and record.get('date')) and not (flag=='meeting_type_not_found' and record.get('meeting_type'))]
            record['extraction_revision']='fallback-local-ocr-v1'
            tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(record,ensure_ascii=False,indent=2));tmp.replace(path)
            review=data/'staging/reviews'/path.name
            if review.exists():review.rename(review.with_suffix('.invalidated.json.bak'))
            return {'record_id':record['record_id'],'status':'repaired','characters':len(text)}
        except Exception as e:return {'record_id':record.get('record_id'),'status':'error','error':str(e)}
    results=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(one,paths):
            results.append(result);print(json.dumps(result),flush=True)
    log=data/'fallback-repair-log.json';log.write_text(json.dumps(results,indent=2))
    # Refresh manifests after all row writes; no concurrent manifest edits needed.
    for manifest in (data/'staging/fallback').glob('*.json'):
        result=json.loads(manifest.read_text());changed=False
        for i,record in enumerate(result.get('records',[])):
            path=data/'staging/records'/f"{record.get('record_id')}.json"
            if path.exists():
                updated=json.loads(path.read_text())
                if updated.get('extraction_revision')=='fallback-local-ocr-v1':result['records'][i]=updated;changed=True
        if changed:
            tmp=manifest.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(manifest)
    return results


def retry_fallback(root=None,workers=4,max_pages=80):
    """Resume unfinished districts and retry explicit gaps using latest sources.

    Completed outputs and prior sources are preserved. Run this after an
    interrupted collection; it does not assume an existing manifest is complete.
    """
    from concurrent.futures import ThreadPoolExecutor,as_completed
    import threading
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    root=Path(root or Path(__file__).resolve().parents[1]);data=root/'data'
    import os
    worker=data/'workers/fallback.json';worker.parent.mkdir(parents=True,exist_ok=True)
    worker.write_text(json.dumps({'pid':os.getpid(),'role':'fallback','started_at':dt.datetime.now(dt.timezone.utc).isoformat()}))
    registry=json.loads((data/'districts.json').read_text())
    targets=[d for d in registry if not d.get('boardbook_url') or 'trial' in (d.get('boardbook_name') or '').lower()]
    local=threading.local();state_dir=data/'staging/fallback-retries';state_dir.mkdir(parents=True,exist_ok=True)
    def write(path,value):
        path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_suffix('.retry.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)
    def run(d):
        state=state_dir/(d['district_id']+'.json');manifest=data/'staging/fallback'/(d['district_id']+'.json')
        previous=json.loads(manifest.read_text()) if manifest.exists() else {'records':[],'coverage':{},'errors':[]}
        needed=(not manifest.exists() or not previous.get('records') or previous.get('errors') or previous.get('coverage',{}).get('status')=='partial' or previous.get('coverage',{}).get('coverage_flags'))
        if state.exists():
            prior=json.loads(state.read_text())
            if prior.get('revision')=='fallback-resume-v4' and prior.get('status') in ('retried','retry_not_needed'):return prior
        if not needed:
            result={'district_id':d['district_id'],'revision':'fallback-resume-v4','status':'retry_not_needed','records':len(previous.get('records',[]))};write(state,result);return result
        sites=json.loads((data/'official-websites.json').read_text())
        if not getattr(local,'session',None):
            local.session=requests.Session();local.session.headers['User-Agent']='TexasSchoolBoardResearch/1.0 (public records collection)'
            local.session.mount('https://',HTTPAdapter(max_retries=Retry(total=2,backoff_factor=1,status_forcelist=[429,500,502,503,504])))
        try:
            result=collect_fallback(local.session,{**d,'official_website':sites.get(d['district_id'])},data/'raw/fallback',max_pages=max_pages)
            merged={r['source_link']:r for r in previous.get('records',[])}
            for r in result['records']:
                r.update(district_id=d['district_id'],wikipedia_url=d['wikipedia_url'],county_source_link=d['county_source'],platform='district_website')
                r['record_id']=hashlib.sha256((d['district_id']+'|'+r['source_link']+'|'+r['detail']).encode()).hexdigest()
                r.setdefault('extraction_status','ok' if r.get('text') else 'empty_record')
                prior=merged.get(r['source_link']);merged[r['source_link']]=r
                destination=data/'staging/records'/(r['record_id']+'.json')
                old=json.loads(destination.read_text()) if destination.exists() else None
                if old is None or any(old.get(k)!=r.get(k) for k in ['text','date','meeting_type','detail','source_link']):
                    write(destination,r)
                    audit=data/'staging/reviews'/(r['record_id']+'.json')
                    if audit.exists():audit.rename(audit.with_suffix('.retry-invalidated.json.bak'))
                if prior and prior.get('record_id') and prior['record_id']!=r['record_id']:
                    obsolete=data/'staging/records'/(prior['record_id']+'.json')
                    if obsolete.exists():obsolete.rename(obsolete.with_suffix('.superseded.json.bak'))
            result['records']=list(merged.values());result['coverage']['record_count']=len(merged)
            dates=[r['date'] for r in merged.values() if r.get('date')]
            result['coverage'].update(date_start=min(dates) if dates else None,date_end=max(dates) if dates else None)
            backup=manifest.with_suffix('.pre-retry.json.bak')
            if manifest.exists() and not backup.exists():backup.write_bytes(manifest.read_bytes())
            write(manifest,result)
            status={'district_id':d['district_id'],'revision':'fallback-resume-v4','status':'retried','records':len(merged),'errors':result['errors'],'coverage':result['coverage']}
            write(state,status)
            print(json.dumps({'retry_district':d['school_district'],'records':len(merged),'errors':len(result['errors'])}),flush=True)
            return status
        except Exception as e:
            status={'district_id':d['district_id'],'revision':'fallback-resume-v4','status':'retry_error','error':str(e)};write(state,status)
            print(json.dumps({'retry_district':d['school_district'],'error':str(e)}),flush=True);return status
    print(json.dumps({'phase':'fallback-resume','districts':len(targets),'workers':workers}),flush=True)
    outcomes=[]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(run,d) for d in targets]):
            outcomes.append(future.result())
            write(data/'fallback-retry-status.json',{'districts_total':len(targets),'districts_processed':len(outcomes),'finished':len(outcomes)==len(targets)})
    repair_metadata(root)
    return outcomes


def repair_metadata(root=None):
    """Reconcile fallback metadata against explicit titles and document headers."""
    root=Path(root or Path(__file__).resolve().parents[1]);data=root/'data'
    replacements={};changed=0
    for path in list((data/'staging/records').glob('*.json')):
        try:record=json.loads(path.read_text())
        except (FileNotFoundError,ValueError):continue
        if record.get('platform')!='district_website' or record.get('document_format') in ('diligent_json_html','boardbook_hosted'):continue
        title=record.get('source_title','');text=record.get('text','')
        body_date=extract_date(text[:1500]);label_date=extract_date(title)
        detail=_classify(title,text) or record.get('detail')
        date=body_date or label_date or record.get('date')
        flags=list(record.get('quality_flags',[]))
        if body_date and label_date and body_date!=label_date:
            if 'source_label_date_conflict' not in flags:flags.append('source_label_date_conflict')
        else:flags=[f for f in flags if f!='source_label_date_conflict']
        if date==record.get('date') and detail==record.get('detail') and flags==record.get('quality_flags',[]):continue
        old=record['record_id'];record.update(date=date,detail=detail,quality_flags=flags,source_label_date=label_date,document_meeting_date=body_date,reviewed=False,metadata_revision='document-header-and-explicit-title-v2')
        record['record_id']=hashlib.sha256((record['district_id']+'|'+record['source_link']+'|'+detail).encode()).hexdigest()
        destination=path.with_name(record['record_id']+'.json');temp=destination.with_suffix('.metadata.tmp')
        temp.write_text(json.dumps(record,ensure_ascii=False,indent=2));temp.replace(destination)
        if destination!=path and path.exists():path.rename(path.with_suffix('.superseded.json.bak'))
        review=data/'staging/reviews'/f'{old}.json'
        if review.exists():review.rename(review.with_suffix('.metadata-invalidated.json.bak'))
        replacements[old]=record;changed+=1
    for manifest in (data/'staging/fallback').glob('*.json'):
        result=json.loads(manifest.read_text());dirty=False
        for i,r in enumerate(result.get('records',[])):
            if r.get('record_id') in replacements:result['records'][i]=replacements[r['record_id']];dirty=True
        if dirty:
            dates=[r.get('date') for r in result['records'] if r.get('date')]
            result.setdefault('coverage',{}).update(date_start=min(dates) if dates else None,date_end=max(dates) if dates else None)
            tmp=manifest.with_suffix('.metadata.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(manifest)
    print(json.dumps({'metadata_records_repaired':changed}),flush=True)
    return changed


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['repair','metadata','retry'])
    parser.add_argument('--workers',type=int,default=2)
    parser.add_argument('--max-pages',type=int,default=80)
    arguments=parser.parse_args()
    if arguments.operation=='repair':repair_staged_fallback(workers=arguments.workers)
    elif arguments.operation=='metadata':repair_metadata()
    else:retry_fallback(workers=arguments.workers,max_pages=arguments.max_pages)
