#!/usr/bin/env python3
"""Resumable statewide public-meeting discovery and text collection."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from boardbook import discover_meetings, extract_record
from runtime import register, wait_for_storage

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
LOCAL=threading.local()

def session():
    if not hasattr(LOCAL,'session'):
        s=requests.Session();s.headers['User-Agent']='TexasSchoolBoardResearch/1.0 (public records collection)'
        s.mount('https://',HTTPAdapter(max_retries=Retry(total=2,backoff_factor=1,status_forcelist=[429,500,502,503,504])))
        LOCAL.session=s
    return LOCAL.session

def save_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(path)

def fetch(url):
    """Cache exact response bytes and final URL; never cache a failed response."""
    key=hashlib.sha256(url.encode()).hexdigest()
    base=DATA/'raw/http'/key[:2]/key
    meta=base.with_suffix('.json');payload=base.with_suffix('.gz')
    if meta.exists() and payload.exists():
        info=json.loads(meta.read_text());r=requests.Response();r.status_code=200
        r._content=gzip.decompress(payload.read_bytes());r.url=info['url'];r.headers.update(info['headers'])
        return r
    wait_for_storage()
    r=session().get(url,timeout=(20,90));r.raise_for_status()
    base.parent.mkdir(parents=True,exist_ok=True)
    payload.write_bytes(gzip.compress(r.content))
    save_json(meta,dict(requested_url=url,url=r.url,headers=dict(r.headers),
                        fetched_at=datetime.now(timezone.utc).isoformat(),sha256=hashlib.sha256(r.content).hexdigest()))
    return r

def inventory_one(d):
    target=DATA/'staging/inventory'/f"{d['district_id']}.json"
    if target.exists():return json.loads(target.read_text())
    result=dict(district=d,records=[],status='discovered',error='')
    try:
        result['records']=discover_meetings(session(),d['boardbook_url'],d['school_district'],d['county_name'],fetch=fetch)
        for r in result['records']:
            r.update(district_id=d['district_id'],wikipedia_url=d['wikipedia_url'],
                     county_source_link=d['county_source'],platform='boardbook')
    except Exception as exc:result.update(status='error',error=str(exc))
    save_json(target,result);return result

def extract_one(record):
    target=DATA/'staging/records'/f"{record['record_id']}.json"
    if target.exists():return json.loads(target.read_text())
    result={**record,**extract_record(session(),record['source_link'],fetch=fetch),
            'collected_at':datetime.now(timezone.utc).isoformat()}
    result['reviewed']=False
    save_json(target,result)
    return result

def export():
    from export_dataset import export_dataset
    return export_dataset()


def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['inventory','extract','fallback','export']);p.add_argument('--workers',type=int,default=6)
    p.add_argument('--limit',type=int);p.add_argument('--max-pages',type=int,default=40)
    a=p.parse_args();districts=json.loads((DATA/'districts.json').read_text())
    register('boardbook_'+a.phase)
    if a.phase=='export':export();return
    if a.phase=='inventory':
        jobs=[d for d in districts if d['boardbook_url']];function=inventory_one
    elif a.phase=='extract':
        jobs=[r for path in sorted((DATA/'staging/inventory').glob('*.json')) for r in json.loads(path.read_text())['records']]
        jobs=[r for r in jobs if not (DATA/'staging/records'/f"{r['record_id']}.json").exists()];function=extract_one
    else:
        from fallback import collect_fallback
        sites_path=DATA/'official-websites.json';sites=json.loads(sites_path.read_text()) if sites_path.exists() else {}
        jobs=[d for d in districts if not d['boardbook_url'] or 'Trial' in (d.get('boardbook_name') or '')]
        def function(d):
            target=DATA/'staging/fallback'/f"{d['district_id']}.json"
            if target.exists():return json.loads(target.read_text())
            d={**d,'official_website':sites.get(d['district_id'],d.get('official_website'))}
            try:result=collect_fallback(session(),d,DATA/'raw/fallback',max_pages=a.max_pages)
            except Exception as exc:result=dict(records=[],coverage={},errors=[str(exc)])
            for r in result['records']:
                r.update(district_id=d['district_id'],wikipedia_url=d['wikipedia_url'],county_source_link=d['county_source'],platform='district_website')
                r['record_id']=hashlib.sha256((d['district_id']+'|'+r['source_link']+'|'+r['detail']).encode()).hexdigest()
                r.setdefault('extraction_status','ok' if r.get('text') else 'empty_record')
                save_json(DATA/'staging/records'/f"{r['record_id']}.json",r)
            save_json(target,result);return result
    if a.limit:jobs=jobs[:a.limit]
    print(json.dumps(dict(phase=a.phase,jobs=len(jobs),workers=a.workers)),flush=True)
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        todo=iter(jobs);futures={};completed=0
        def submit_more():
            while len(futures)<a.workers*3:
                try:r=next(todo)
                except StopIteration:break
                futures[pool.submit(function,r)]=r
        submit_more()
        while futures:
            done,_=wait(futures,return_when=FIRST_COMPLETED)
            for future in done:
                r=futures.pop(future);completed+=1
                try:future.result()
                except Exception as exc:print(json.dumps(dict(error=str(exc),job=str(r)[:100])),flush=True)
                if completed%100==0 or completed==len(jobs):print(json.dumps(dict(phase=a.phase,completed=completed,total=len(jobs))),flush=True)
            submit_more()
    export()

if __name__=='__main__':main()
