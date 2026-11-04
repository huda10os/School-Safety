"""Retry saved BoardBook extraction errors, preserving the earlier evidence."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
from collect import DATA, fetch, save_json, session
from boardbook import extract_record
from runtime import register, wait_for_storage

def retry(path):
    wait_for_storage()
    old=json.loads(path.read_text())
    save_json(DATA/'retry-backups'/path.name,old)
    result={**old,**extract_record(session(),old['source_link'],fetch=fetch),
            'reviewed':False,'collected_at':datetime.now(timezone.utc).isoformat()}
    save_json(path,result)
    print(json.dumps({'record_id':result['record_id'],'status':result['extraction_status']}),flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--workers',type=int,default=2);args=parser.parse_args()
    register('boardbook_retry')
    paths=[]
    for path in (DATA/'staging/records').glob('*.json'):
        r=json.loads(path.read_text())
        if r.get('platform')=='boardbook' and r.get('extraction_status')=='error':paths.append(path)
    print(json.dumps({'retry_records':len(paths)}),flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:list(pool.map(retry,paths))

if __name__=='__main__':main()
