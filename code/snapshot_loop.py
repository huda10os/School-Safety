#!/usr/bin/env python3
"""Refresh Parquet snapshots periodically without interrupting collection."""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from export_dataset import export_dataset
from runtime import register, wait_for_storage

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--interval',type=int,default=300);parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    register('snapshots')
    while True:
        wait_for_storage(1)
        started=time.monotonic()
        try:export_dataset()
        except Exception as exc:print(json.dumps(dict(error=str(exc),at=datetime.now(timezone.utc).isoformat())),flush=True)
        if args.once:return
        remaining=max(1,args.interval-(time.monotonic()-started))
        while remaining>0:
            delay=min(30,remaining);time.sleep(delay);remaining-=delay

if __name__=='__main__':main()
