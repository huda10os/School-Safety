#!/usr/bin/env python3
"""Maintain a self-contained, automatically refreshed HTML collection tracker."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import time
import shutil
from runtime import workers, register

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'

def read(path,default=None):
    try:return json.loads(path.read_text())
    except (OSError,json.JSONDecodeError):return default

def escaped(value):return html.escape(str(value or ''))

class Tracker:
    def __init__(self):
        self.records={};self.reviews={};self.inventory={};self.fallback={};self.seen={};self.events=[]

    def update(self):
        registry=read(DATA/'districts.json',[])
        required_review_version=read(DATA/'review-policy.json',{}).get('reviewer_version')
        groups=[('inventory',self.inventory),('fallback',self.fallback),('records',self.records),('reviews',self.reviews)]
        changes={}
        for group,cache in groups:
            added=0
            present=set()
            for path in (DATA/'staging'/group).glob('*.json'):
                if '.failed.' in path.name:continue
                present.add(path.stem)
                try:stamp=path.stat().st_mtime_ns
                except OSError:continue
                key=(group,path.name)
                if self.seen.get(key)==stamp:continue
                data=read(path)
                if data is None:continue
                self.seen[key]=stamp
                if group=='records':
                    # Keep a small metadata projection, never the extracted document bodies.
                    data={k:data.get(k) for k in ['district_id','date','detail','extraction_status','extraction_method','platform','ocr_performed']}
                elif group=='reviews':
                    evidence=data.get('review_evidence') or '{}'
                    if isinstance(evidence,str):
                        try:evidence=json.loads(evidence)
                        except ValueError:evidence={}
                    data={**{k:data.get(k) for k in ['reviewed','review_status','review_notes','district_id']},'reviewer_version':evidence.get('reviewer')}
                elif group=='inventory':
                    rows=data.get('records',[])
                    data=dict(district=data.get('district'),status=data.get('status'),error=data.get('error'),
                        count=len(rows),dates=sorted({r['date'] for r in rows if r.get('date')}),
                        details=dict(Counter(r.get('detail','') for r in rows)))
                elif group=='fallback':
                    data=dict(coverage=data.get('coverage',{}),errors=data.get('errors',[]),count=len(data.get('records',[])))
                cache[path.stem]=data;added+=1
            for removed in set(cache)-present:
                del cache[removed]
                self.seen.pop((group,removed+'.json'),None)
            changes[group]=added
        bydistrict={d['district_id']:dict(discovered=0,collected=0,reviewed=0,attempted=0,issues=0,ocr=0,
            first=None,last=None,details={},status='Pending',note='') for d in registry}
        for key,inv in self.inventory.items():
            if key not in bydistrict:continue
            a=bydistrict[key];a['discovered']=inv['count'];a['details']=inv['details'];a['status']='Inventoried' if inv['status']!='error' else 'Inventory error';a['note']=inv.get('error') or ''
            if inv['dates']:a['first']=inv['dates'][0];a['last']=inv['dates'][-1]
        for key,fallback in self.fallback.items():
            if key not in bydistrict:continue
            a=bydistrict[key];a['discovered']+=fallback['count'];a['note']='; '.join(map(str,fallback['errors']))[:600]
            if fallback['count']:a['status']='Website records found'
            elif not a['discovered']:a['status']='Needs follow-up'
        for key,r in self.records.items():
            a=bydistrict.get(r.get('district_id'))
            if a is None:continue
            a['collected']+=1;a['issues']+=r.get('extraction_status')!='ok'
            a['ocr']+=bool(r.get('ocr_performed')) or 'ocr' in str(r.get('extraction_method') or '')
            rev=self.reviews.get(key,{})
            a['attempted']+=bool(rev)
            fresh=self.seen.get(('reviews',key+'.json'),0)>=self.seen.get(('records',key+'.json'),0)
            a['reviewed']+=fresh and bool(rev.get('reviewed')) and (not required_review_version or rev.get('reviewer_version')==required_review_version)
            dt=r.get('date')
            if dt:a['first']=min(a['first'] or dt,dt);a['last']=max(a['last'] or dt,dt)
        for a in bydistrict.values():
            a['discovered']=max(a['discovered'],a['collected'])
            if a['collected']:
                a['status']='Collecting' if a['collected']<a['discovered'] else 'Reviewing'
                if a['reviewed']==a['collected'] and a['collected']>=a['discovered']:a['status']='Verified'
        totals={k:sum(a[k] for a in bydistrict.values()) for k in ['discovered','collected','reviewed','attempted','issues','ocr']}
        now=datetime.now(timezone.utc)
        event=f"{now.astimezone().strftime('%b %d, %I:%M:%S %p %Z')} — {totals['collected']:,} collected; {totals['attempted']:,} reviewed by the audit process; {totals['reviewed']:,} passed verification."
        if changes.get('records') or changes.get('reviews') or not self.events:self.events.insert(0,event)
        self.events=self.events[:40]
        snapshot=dict(updated_at=now.isoformat(),totals=totals,workers=workers(),free_gib=round(shutil.disk_usage(DATA).free/1024**3,1),
            inventory_districts=len(self.inventory),fallback_districts=len(self.fallback),
            district_count=len(registry),districts=[{**d,**bydistrict[d['district_id']]} for d in registry],
            events=self.events)
        output=DATA/'progress.json';tmp=output.with_suffix('.tmp.json');tmp.write_text(json.dumps(snapshot,indent=2));tmp.replace(output)
        self.render(snapshot)
        return snapshot

    def render(self,s):
        t=s['totals'];total=t['discovered'] or 1
        cards=[('Districts in scope',f"{s['district_count']:,}",'Statewide Wikipedia registry'),
               ('Records discovered',f"{t['discovered']:,}",'One row per record link'),
               ('Collection attempted',f"{t['collected']:,}",f"{t['collected']/total:.1%} of discovered records"),
               ('Verified records',f"{t['reviewed']:,}",f"{t['attempted']:,} review attempts completed")]
        rows=[]
        for d in s['districts']:
            source=d.get('boardbook_url') or read(DATA/'official-websites.json',{}).get(d['district_id']) or d.get('wikipedia_url')
            title=f'<a href="{escaped(source)}" target="_blank" rel="noopener">{escaped(d["school_district"])}</a>' if source else escaped(d['school_district'])
            span=f'{d["first"] or "—"} → {d["last"] or "—"}'
            note=d.get('note') or ''
            rows.append(f'<tr data-search="{escaped((d["school_district"]+" "+d["county_name"]+" "+d["status"]).lower())}" data-status="{escaped(d["status"])}"><td>{title}<small>{escaped(note)}</small></td><td>{escaped(d["county_name"])}</td><td><span class="badge">{escaped(d["status"])}</span></td><td>{d["discovered"]:,}</td><td>{d["collected"]:,}</td><td>{d["reviewed"]:,}<small>{d["attempted"]:,} audit attempts</small></td><td>{d["issues"]:,}</td><td class="dates">{escaped(span)}</td></tr>')
        latest=read(DATA/'summary.json',{})
        cards_html=''.join(f'<article class="card"><span>{label}</span><strong>{value}</strong><small>{hint}</small></article>' for label,value,hint in cards)
        active=[w['role'] for w in s['workers'] if w.get('running') and w['role'] not in ('tracker','snapshots','preview')]
        status='Active workers: '+', '.join(active) if active else 'Collection workers stopped; saved progress preserved'
        if s['free_gib']<5:status='Low storage: collection waiting for free space'
        status+=f" · {s['free_gib']} GiB free"
        if (DATA/'collection-complete.json').exists():status='Collection pass finished; review status below'
        template='''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="30"><title>Texas School Board Records — Progress</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,sans-serif;color:#15303c;background:#f3f6f7;font-size:15px}*{box-sizing:border-box}body{margin:0}header{background:#0a2734;color:#fff;padding:38px max(28px,calc((100vw - 1440px)/2));border-bottom:5px solid #20c5b1}header .eyebrow{letter-spacing:2px;text-transform:uppercase;font-size:12px;color:#7de0d3}h1{font-weight:650;font-size:34px;margin:12px 0}header p{color:#bbd2db;margin:8px 0}.live{display:inline-block;background:#163e4b;border-radius:20px;padding:7px 12px;color:#9af0d7;font-size:13px}.live:before{content:'●';margin-right:8px;color:#36d8b5}main{max-width:1496px;margin:0 auto;padding:28px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:16px}.card{background:white;border:1px solid #dce6e9;padding:22px;border-radius:12px;box-shadow:0 3px 10px #16374705}.card span{font-size:13px;color:#54707c}.card strong{display:block;font-size:36px;letter-spacing:-1px;margin:10px 0}.card small,td small{display:block;color:#67808b;font-size:12px}.panel{background:#fff;border:1px solid #dce6e9;border-radius:12px;padding:24px;margin-top:22px}h2{margin:0 0 18px;font-size:20px}.bars{display:grid;grid-template-columns:1fr 1fr;gap:28px}.barlabel{display:flex;justify-content:space-between;font-size:13px;margin-bottom:10px}.track{height:10px;background:#e9eff1;border-radius:8px;overflow:hidden}.fill{height:100%;background:#00a997;min-width:2px}.fill.blue{background:#3f78ae}.note{background:#f3f8f9;border-left:3px solid #25a899;padding:14px 18px;margin-top:20px;color:#45616c;font-size:13px;line-height:1.6}.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:18px}input,select,button{font:inherit;border:1px solid #cbdade;border-radius:7px;padding:10px 12px;background:white;color:#173744}input{min-width:290px;flex:1}button{cursor:pointer}.tablewrap{overflow:auto;max-height:650px;border:1px solid #e1eaec;border-radius:8px}table{border-collapse:collapse;width:100%;font-size:13px}th{position:sticky;top:0;background:#edf4f5;text-align:left;color:#42606b;font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:13px;z-index:1}td{padding:13px;border-top:1px solid #e7eef0;vertical-align:top}td:first-child{min-width:260px}td small{margin-top:5px;max-width:370px;line-height:1.4}a{color:#087e78;text-decoration:none}a:hover{text-decoration:underline}.badge{display:inline-block;padding:4px 8px;border-radius:4px;background:#e9f3f4;color:#365e6c;font-size:11px;white-space:nowrap}.dates{white-space:nowrap;font-variant-numeric:tabular-nums;color:#54717e}.bottom{display:grid;grid-template-columns:2fr 1fr;gap:22px}.event{font-size:13px;padding:10px 0;border-bottom:1px solid #edf2f3;line-height:1.5}.links a{display:block;padding:9px 0}footer{font-size:12px;color:#69808a;padding:24px 0;line-height:1.7}@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}.bars,.bottom{grid-template-columns:1fr}h1{font-size:26px}main{padding:18px}.card strong{font-size:29px}}@media(max-width:500px){.cards{grid-template-columns:1fr}header{padding:25px}input{min-width:100%}}
</style></head><body><header><div class="eyebrow">School Safety Research / Public records collection</div><h1>Texas school board records</h1><p>Statewide collection, extraction, and independent source verification.</p><span class="live">{{STATUS}}</span><p style="font-size:12px;margin-top:16px">Updated {{TIME}} · This file refreshes every 30 seconds while open.</p></header><main><section class="cards">{{CARDS}}</section><section class="panel"><div class="bars"><div><div class="barlabel"><span>Record collection</span><b>{{COLLECT_PCT}}</b></div><div class="track"><div class="fill" style="width:{{COLLECT_WIDTH}}%"></div></div></div><div><div class="barlabel"><span>Independent verification passed</span><b>{{REVIEW_PCT}}</b></div><div class="track"><div class="fill blue" style="width:{{REVIEW_WIDTH}}%"></div></div></div></div><div class="note"><b>How to read this tracker.</b> Discovered records include links whose text is still pending. “Collection attempted” includes empty, scanned, or failed documents; {{ISSUES}} records currently have extraction issues. “Verified” requires the reviewer to reopen the source and verify the district, county, date, record type, meeting type, and complete extracted text. {{OCR}} records used or require OCR. Date ranges reflect records found and do not establish that an archive is complete. The Parquet export was last updated {{EXPORTED}}.</div></section><section class="panel"><h2>District coverage</h2><div class="controls"><input id="search" placeholder="Search district, county, or status" aria-label="Search district coverage"><select id="status" aria-label="Filter by district status"><option value="">All statuses</option>{{OPTIONS}}</select><button onclick="location.reload()">Refresh now</button><span id="shown" style="font-size:12px;color:#63808b"></span></div><div class="tablewrap"><table><thead><tr><th>School district</th><th>County</th><th>Status</th><th>Discovered</th><th>Collected</th><th>Verified</th><th>Issues</th><th>Available date range</th></tr></thead><tbody>{{ROWS}}</tbody></table></div></section><div class="bottom"><section class="panel"><h2>Recent updates</h2>{{EVENTS}}</section><section class="panel links"><h2>Project files</h2><a href="data/records.parquet">Records dataset (.parquet)</a><a href="data/district_coverage.parquet">District coverage (.parquet)</a><a href="data/progress.json">Current progress (.json)</a><a href="data/summary.json">Last export summary (.json)</a><a href="README.md">Run instructions and data definitions</a><a href="data/extraction.log">Collection log</a></section></div><footer>County is the Wikipedia grouping county; some districts span multiple counties. Sources: <a href="https://en.wikipedia.org/wiki/List_of_school_districts_in_Texas">Wikipedia district registry</a> · <a href="https://meetings.boardbook.org/Public">BoardBook public directory</a>. Local processing continues only while the collection and tracker processes are running on this computer. A stale update time indicates the tracker has stopped.</footer></main><script>
const q=document.querySelector('#search'),s=document.querySelector('#status');q.value=sessionStorage.getItem('districtQuery')||'';s.value=sessionStorage.getItem('districtStatus')||'';function filter(){let n=0;for(const r of document.querySelectorAll('tbody tr')){let yes=r.dataset.search.includes(q.value.toLowerCase())&&(!s.value||s.value===r.dataset.status);r.hidden=!yes;if(yes)n++}document.querySelector('#shown').textContent=n+' districts shown';sessionStorage.setItem('districtQuery',q.value);sessionStorage.setItem('districtStatus',s.value)}q.addEventListener('input',filter);s.addEventListener('change',filter);filter();
</script></body></html>'''
        replacements={'STATUS':escaped(status),'TIME':escaped(datetime.fromisoformat(s['updated_at']).astimezone().strftime('%b %d, %Y at %I:%M:%S %p %Z')),
            'CARDS':cards_html,'COLLECT_PCT':f'{t["collected"]/total:.2%}','COLLECT_WIDTH':str(min(100,t['collected']/total*100)),
            'REVIEW_PCT':f'{t["reviewed"]/total:.2%}','REVIEW_WIDTH':str(min(100,t['reviewed']/total*100)),
            'ISSUES':f'{t["issues"]:,}','OCR':f'{t["ocr"]:,}','EXPORTED':escaped(latest.get('exported_at','not yet')),
            'ROWS':'\n'.join(rows),'EVENTS':''.join(f'<div class="event">{escaped(e)}</div>' for e in s['events']),
            'OPTIONS':''.join(f'<option>{escaped(status)}</option>' for status in sorted({d['status'] for d in s['districts']}))}
        for key,value in replacements.items():template=template.replace('{{'+key+'}}',value)
        target=ROOT/'progress.html';temp=target.with_suffix('.tmp.html');temp.write_text(template);temp.replace(target)

def main():
    p=argparse.ArgumentParser();p.add_argument('--watch',action='store_true');p.add_argument('--interval',type=int,default=30);a=p.parse_args()
    register('tracker')
    tracker=Tracker()
    while True:
        result=tracker.update();print(json.dumps(dict(updated_at=result['updated_at'],totals=result['totals'])),flush=True)
        if not a.watch:return
        time.sleep(a.interval)

if __name__=='__main__':main()
