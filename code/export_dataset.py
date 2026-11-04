"""Streaming Parquet snapshots: every discovered record, including pending text."""
from datetime import date, datetime, timezone
import json
import fcntl
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'
COLUMNS=['school_district','county_name','date','detail','meeting_type','text','source_link','reviewed',
 'record_id','district_id','platform','extraction_status','extraction_error','extraction_method',
 'review_status','review_notes','review_evidence','organization_url','listing_url','discovery_url',
 'wikipedia_url','county_source_link','boardbook_name','meeting_id','meeting_title','meeting_row_text',
 'date_raw','resolved_source_link','text_source_link','content_type','attachments_json','collected_at',
 'reviewed_at','document_id','document_format','ocr_performed','cancelled',
 'source_title','source_context','raw_path','raw_sha256','quality_flags','fetched_at']
SCHEMA=pa.schema([pa.field(k,pa.date32() if k=='date' else pa.bool_() if k in ('reviewed','ocr_performed','cancelled') else pa.string()) for k in COLUMNS])

def load(path,default=None):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,json.JSONDecodeError):return default

def current_review(fields, policy):
    required=policy.get('reviewer_version')
    if not required:return fields
    evidence=fields.get('review_evidence') or '{}'
    if isinstance(evidence,str):
        try:evidence=json.loads(evidence)
        except ValueError:evidence={}
    if fields and evidence.get('reviewer')!=required:
        return {**fields,'reviewed':False,'review_status':'outdated_review',
                'review_notes':'Review policy changed; independent review is queued again.'}
    return fields

def load_review(rid,policy):
    record=DATA/'staging/records'/f'{rid}.json'
    review=DATA/'staging/reviews'/f'{rid}.json'
    fields=current_review(load(review,{}),policy)
    if fields and record.exists() and review.stat().st_mtime_ns < record.stat().st_mtime_ns:
        return {**fields,'reviewed':False,'review_status':'outdated_review',
                'review_notes':'Extraction changed after review; independent verification is queued again.'}
    return fields

def iter_records():
    policy=load(DATA/'review-policy.json',{})
    seen=set()
    for path in sorted((DATA/'staging/inventory').glob('*.json')):
        if '.failed.' in path.name:continue
        info=load(path,{})
        for record in info.get('records',[]):
            if record['record_id'] in seen:continue
            seen.add(record['record_id'])
            saved=load(DATA/'staging/records'/f"{record['record_id']}.json")
            r=saved if saved is not None else {**record,'text':None,'extraction_status':'pending','review_status':'pending'}
            review=load_review(record['record_id'],policy)
            yield {**r,**review}
    # Fallback staging is authoritative for the IDs of district-website records.
    for path in sorted((DATA/'staging/fallback').glob('*.json')):
        result=load(path,{})
        for record in result.get('records',[]):
            rid=record.get('record_id')
            if not rid or rid in seen:continue
            seen.add(rid)
            saved=load(DATA/'staging/records'/f'{rid}.json',record)
            review=load_review(rid,policy)
            yield {**saved,**review}

    # A crash may leave completed record checkpoints before a district manifest.
    for path in sorted((DATA/'staging/records').glob('*.json')):
        if path.stem in seen:continue
        record=load(path,{})
        if not record.get('record_id'):continue
        review=load_review(path.stem,policy)
        yield {**record,**review}

def serialize(r):
    row={}
    for key in COLUMNS:
        v=r.get(key)
        if key=='attachments_json':v=r.get('attachments',[])
        if key=='date':
            try:v=date.fromisoformat(str(v)[:10]) if v else None
            except ValueError:v=None
        elif key in ('reviewed','ocr_performed','cancelled'):v=bool(v) if v is not None else False
        elif isinstance(v,(dict,list)):v=json.dumps(v,ensure_ascii=False)
        elif v is not None:v=str(v)
        row[key]=v
    return row

def export_dataset():
    with (DATA/'export.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        return _export_dataset()

def _export_dataset():
    registry=load(DATA/'districts.json',[])
    aggregates={d['district_id']:dict(records=0,collected=0,review_attempted=0,reviewed=0,
        extraction_ok=0,errors=0,ocr=0,pending=0,first_date=None,last_date=None,
        first_minutes_date=None,last_minutes_date=None,
        first_collected_minutes_date=None,last_collected_minutes_date=None,details={}) for d in registry}
    temp=DATA/'records.tmp.parquet'
    batch=[];count=0
    with pq.ParquetWriter(temp,SCHEMA,compression='zstd') as writer:
        for r in iter_records():
            a=aggregates.get(r.get('district_id'))
            if a is None:continue
            a['records']+=1;count+=1
            pending=r.get('extraction_status')=='pending';a['pending']+=pending;a['collected']+=not pending
            a['review_attempted']+=r.get('review_status','pending') not in ('pending','')
            a['reviewed']+=bool(r.get('reviewed'));a['extraction_ok']+=r.get('extraction_status')=='ok'
            a['errors']+=r.get('extraction_status') not in ('ok','pending')
            a['ocr']+=bool(r.get('ocr_performed')) or 'ocr' in r.get('extraction_method','')
            detail=r.get('detail') or 'Unspecified';a['details'][detail]=a['details'].get(detail,0)+1
            dt=r.get('date')
            if dt:
                dt=str(dt)[:10];a['first_date']=min(a['first_date'] or dt,dt);a['last_date']=max(a['last_date'] or dt,dt)
                if 'minute' in detail.lower():
                    if r.get('text') and r.get('extraction_status')=='ok':
                        a['first_collected_minutes_date']=min(a['first_collected_minutes_date'] or dt,dt)
                        a['last_collected_minutes_date']=max(a['last_collected_minutes_date'] or dt,dt)
                    a['first_minutes_date']=min(a['first_minutes_date'] or dt,dt);a['last_minutes_date']=max(a['last_minutes_date'] or dt,dt)
            batch.append(serialize(r))
            if len(batch)>=2048:writer.write_table(pa.Table.from_pylist(batch,schema=SCHEMA));batch=[]
        if batch:writer.write_table(pa.Table.from_pylist(batch,schema=SCHEMA))
    temp.replace(DATA/'records.parquet')
    coverage=[]
    for d in registry:
        a=aggregates[d['district_id']]
        first,last=a['first_date'],a['last_date']
        days=(date.fromisoformat(last)-date.fromisoformat(first)).days if first and last else 0
        inv=load(DATA/'staging/inventory'/f"{d['district_id']}.json",{})
        fallback=load(DATA/'staging/fallback'/f"{d['district_id']}.json",{})
        coverage.append({**d,**a,'details':json.dumps(a['details']),
            'short_history_flag':days<730,'history_days':days,
            'inventory_status':inv.get('status','not_boardbook' if not d.get('boardbook_url') else 'pending'),
            'inventory_error':inv.get('error',''),
            'fallback_coverage':json.dumps(fallback.get('coverage',{})),
            'fallback_errors':json.dumps(fallback.get('errors',[]))})
    pq.write_table(pa.Table.from_pylist(coverage),DATA/'district_coverage.tmp.parquet',compression='zstd')
    (DATA/'district_coverage.tmp.parquet').replace(DATA/'district_coverage.parquet')
    summary=dict(districts_total=len(registry),districts_with_records=sum(a['records']>0 for a in aggregates.values()),
        records=count,**{k:sum(a[k] for a in aggregates.values()) for k in ['collected','pending','review_attempted','reviewed','extraction_ok','errors','ocr']},
        boardbook_records_discovered=sum(a['records'] for k,a in aggregates.items() if next((d.get('boardbook_url') for d in registry if d['district_id']==k),False)),
        exported_at=datetime.now(timezone.utc).isoformat())
    (DATA/'summary.tmp.json').write_text(json.dumps(summary,indent=2));(DATA/'summary.tmp.json').replace(DATA/'summary.json')
    print(json.dumps(summary),flush=True)
    return summary

if __name__=='__main__':export_dataset()
