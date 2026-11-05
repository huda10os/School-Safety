"""Match the public BoardBook directory to TEA districts with location evidence.

Run from code/: ../.venv/bin/python match_boardbook.py [--refresh]
Both discovery stages append resumable evidence; platforms.csv is rebuilt from
that journal after each run. No name-only match is accepted without Texas or
county evidence. Ambiguous names can subsequently be resolved by official links.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from config import REFERENCE
from fetch import Fetcher
from records import append_jsonl, read_jsonl

DIRECTORY = 'https://meetings.boardbook.org/Public'
EVIDENCE = REFERENCE / 'platform_evidence.jsonl'
MAP_FIELDS = ['tea_id', 'school_district', 'platform', 'entry_url', 'evidence', 'note']


def normalize_name(name: str) -> str:
    name = re.sub('[‐‑‒–—−]', '-', name)
    name = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    name = re.sub(r'\bmt\.?\b', 'mount', name)
    name = re.sub(r'\bft\.?\b', 'fort', name)
    name = re.sub(r'\beagle\s+mount\b', 'eagle mountain', name)
    name = re.sub(r'\b(?:(?:independent|consolidated|common|municipal)\s+)+school\s+district\b', '', name)
    name = re.sub(r'\b(?:c\s*\.?\s*i\s*\.?\s*s\s*\.?\s*d|[icm]\s*\.?\s*s\s*\.?\s*d|cisd|isd|csd|msd)\b\.?', '', name)
    name = re.sub(r'\b(?:trial|texas|tx|consolidated|ccsd)\b', '', name)
    name = re.sub(r'\(\d{6}\)', '', name)
    name = re.sub(r'\bschools?\s*$', '', name)
    name = re.sub(r'(?:\s*-\s*|\s*\()\s*[a-z]+(?:\s+[a-z]+)*\s+county\s*\)?\s*$', '', name)
    name = re.sub(r'\band\b', '&', name)
    return re.sub(r'[^a-z0-9&]', '', name)


def load_csv(path: Path) -> list[dict]:
    with path.open(newline='') as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fields: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.csv.new')
    with temporary.open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def latest_evidence() -> dict[tuple[str, str], dict]:
    return {(row['tea_id'], row['stage']): row for row in read_jsonl(EVIDENCE)
            if 'tea_id' in row and 'stage' in row}


def rebuild_map() -> list[dict]:
    evidence = latest_evidence()
    rows = []
    for district in load_csv(REFERENCE / 'districts.csv'):
        tea_id = district['tea_id']
        scan = evidence.get((tea_id, 'detect'))
        book = evidence.get((tea_id, 'boardbook'), {})
        # A site scan includes its validated BoardBook seeds. New matcher
        # evidence is also merged so running either stage cannot erase a source.
        sources = list(book.get('sources', [])) + list((scan or {}).get('sources', []))
        unique = {}
        for source in sources:
            unique[(source['platform'], source['entry_url'])] = source
        if not unique:
            reason = (scan or {}).get('reason', 'not yet scanned')
            unique[('none', '')] = dict(platform='none', entry_url='', evidence='', note=reason)
        for source in unique.values():
            rows.append(dict(tea_id=tea_id, school_district=district['school_district'],
                             **{field: source.get(field, '') for field in MAP_FIELDS[2:]}))
    rows.sort(key=lambda row: (row['tea_id'], row['platform'], row['entry_url']))
    write_csv(REFERENCE / 'platforms.csv', MAP_FIELDS, rows)
    return rows


def fetch_directory(fetcher: Fetcher, refresh=False) -> list[dict]:
    response = fetcher.get(DIRECTORY, refresh=refresh)
    soup = BeautifulSoup(response.text, 'lxml')
    orgs = {}
    for anchor in soup.select('a[href]'):
        match = re.search(r'/Public/Organization/(\d+)', anchor['href'])
        if match:
            org_id = match.group(1)
            orgs[org_id] = dict(org_id=org_id, name=anchor.get_text(' ', strip=True),
                               url=f'https://meetings.boardbook.org/Public/Organization/{org_id}')
    if len(orgs) < 1000:
        raise ValueError(f'Incomplete BoardBook directory: only {len(orgs)} organizations')
    rows = sorted(orgs.values(), key=lambda row: int(row['org_id']))
    write_csv(REFERENCE / 'boardbook_orgs.csv', ['org_id', 'name', 'url'], rows)
    return rows


def inspect_org(fetcher: Fetcher, org: dict, refresh=False) -> dict:
    result = dict(org)
    try:
        response = fetcher.get(org['url'], refresh=refresh, timeout=25)
        soup = BeautifulSoup(response.text, 'lxml')
        meetings = soup.select('#PublicMeetingsTable tbody tr')
        document_links = soup.select('a[href*="/Public/Agenda/"], a[href*="/Public/Minutes/"], a[href*="/Public/Meeting/"], a[href*="/Public/PublicNotice/"]')
        text = soup.get_text(' ', strip=True)
        texas = re.search(r'\b(?:Texas|TX)\s+\d{5}(?:-\d{4})?\b', text, re.I)
        result.update(meetings=len(meetings), document_links=len(document_links), texas=texas.group(0) if texas else '',
                      sample=text[max(0, texas.start()-160):texas.end()+50] if texas else text[:600],
                      raw_path=response.raw_path, title=soup.title.get_text() if soup.title else '')
        soup.decompose()
    except Exception as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'[:300]
    return result


def match_district(district: dict, candidates: list[dict], duplicate: bool) -> dict:
    sources = []
    county = district['county_name'].removesuffix(' County').lower()
    for org in candidates:
        if org.get('error') or not org.get('meetings') or not org.get('texas'):
            continue
        org_county = re.search(r'(?:[-–—]|\()\s*([a-z]+(?:\s+[a-z]+)*)\s+county\b', org['name'], re.I)
        if org_county and org_county.group(1).lower() != county:
            continue
        if duplicate and (not org_county or org_county.group(1).lower() != county):
            continue  # the official-site scanner can prove the organization id
        evidence = f"Directory: {org['name']}; {org['sample']}"
        sources.append(dict(platform='boardbook', entry_url=org['url'], evidence=evidence,
                            note=f"Public meetings: {org['meetings']}; name + Texas location"
                                 + (' + county' if org_county else '')))
    return dict(tea_id=district['tea_id'], school_district=district['school_district'],
                stage='boardbook', completed_at=datetime.now(timezone.utc).isoformat(),
                candidates=candidates, sources=sources,
                reason='matched' if sources else 'no verified BoardBook name match')


def run(refresh=False, workers=8, tea_ids=None):
    start = time.monotonic()
    fetcher = Fetcher()
    districts = load_csv(REFERENCE / 'districts.csv')
    directory = fetch_directory(fetcher, refresh)
    by_name = defaultdict(list)
    district_names = defaultdict(list)
    for org in directory:
        by_name[normalize_name(org['name'])].append(org)
    for district in districts:
        district_names[normalize_name(district['school_district'])].append(district)
    done = latest_evidence()
    pending = [d for d in districts if (refresh or (d['tea_id'], 'boardbook') not in done)
               and (not tea_ids or d['tea_id'] in tea_ids)]
    needed = {org['org_id']: org for d in pending
              for org in by_name[normalize_name(d['school_district'])]}
    inspected = {}
    waiting = defaultdict(list)
    completed = set()
    for district in pending:
        name = normalize_name(district['school_district'])
        candidates = by_name[name]
        if not candidates:
            append_jsonl(EVIDENCE, match_district(district, [], False))
            completed.add(district['tea_id'])
        for org in candidates:
            waiting[org['org_id']].append(district)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(inspect_org, fetcher, org, refresh): key for key, org in needed.items()}
        for number, future in enumerate(as_completed(futures), 1):
            org_id = futures[future]
            inspected[org_id] = future.result()
            for district in waiting[org_id]:
                if district['tea_id'] in completed:
                    continue
                name = normalize_name(district['school_district'])
                if all(org['org_id'] in inspected for org in by_name[name]):
                    candidates = [inspected[org['org_id']] for org in by_name[name]]
                    append_jsonl(EVIDENCE, match_district(district, candidates, len(district_names[name]) > 1))
                    completed.add(district['tea_id'])
            if number % 100 == 0:
                print(json.dumps(dict(stage='boardbook_orgs', done=number, total=len(needed))), flush=True)
    rows = rebuild_map()
    print(json.dumps(dict(stage='boardbook', districts=len(pending), organizations=len(directory),
                          matched=sum(row['platform'] == 'boardbook' for row in rows),
                          wall_seconds=round(time.monotonic()-start, 1))), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--tea-ids')
    args = parser.parse_args()
    run(args.refresh, args.workers, set(args.tea_ids.split(',')) if args.tea_ids else None)
