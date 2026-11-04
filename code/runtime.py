"""Worker liveness and storage safeguards for resumable collectors."""
import json
import os
from pathlib import Path
import shutil
import time
from datetime import datetime, timezone

DATA=Path(__file__).resolve().parents[1]/'data'

def register(role):
    directory=DATA/'workers';directory.mkdir(exist_ok=True)
    (directory/f'{role}.json').write_text(json.dumps(dict(role=role,pid=os.getpid(),started_at=datetime.now(timezone.utc).isoformat())))

def wait_for_storage(minimum_gib=5):
    while shutil.disk_usage(DATA).free < minimum_gib*1024**3:
        print(json.dumps(dict(status='waiting_for_storage',minimum_gib=minimum_gib)),flush=True)
        time.sleep(30)

def workers():
    result=[]
    for path in (DATA/'workers').glob('*.json'):
        try:
            worker=json.loads(path.read_text())
            try:os.kill(int(worker['pid']),0);worker['running']=True
            except PermissionError:worker['running']=True
            except ProcessLookupError:worker['running']=False
            result.append(worker)
        except (OSError,ValueError,KeyError):continue
    return result
