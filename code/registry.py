"""Rebuild the district registry using saved, attributable source snapshots."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
WIKI = 'https://en.wikipedia.org/wiki/List_of_school_districts_in_Texas'
BOARD = 'https://meetings.boardbook.org/Public'

def parse_wikipedia(html):
    soup = BeautifulSoup(html, 'html.parser')
    rows, region, county = [], None, None
    for el in soup.select('h2,h3,li'):
        if el.name == 'h2':
            title = el.get_text(' ', strip=True).replace('[edit]', '').strip()
            region = title if re.fullmatch(r'Region \d+', title) else None
        elif el.name == 'h3':
            county = el.get_text(' ', strip=True).replace('[edit]', '').strip()
        elif region and county and not el.find_parent('li'):
            a = el.find('a')
            name = a.get_text(' ', strip=True) if a else el.get_text(' ', strip=True)
            if 'School District' in name:
                rows.append(dict(name=name, county=county, region=region,
                                 url=urljoin(WIKI, a['href']) if a else '',
                                 text=el.get_text(' ', strip=True)))
    if len(rows) < 900:
        raise ValueError('Incomplete Wikipedia source; retain the supplied complete JSON snapshot')
    return rows

def build(refresh=False):
    raw = ROOT / 'data/raw'
    raw.mkdir(parents=True, exist_ok=True)
    warnings = []
    session = requests.Session()
    session.headers['User-Agent'] = 'TexasSchoolBoardResearch/1.0 (public records collection)'
    if refresh:
        for url, filename in [(WIKI, 'wikipedia-districts.html'), (BOARD, 'boardbook-directory.html')]:
            try:
                response = session.get(url, timeout=60)
                response.raise_for_status()
                if url == WIKI:
                    rows = parse_wikipedia(response.text)
                    (ROOT/'data/wikipedia-districts.json').write_text(json.dumps(rows, indent=2))
                (raw/filename).write_text(response.text)
            except Exception as exc:
                warnings.append(f'{url}: {exc}; retained saved source snapshot')
    rows = json.loads((ROOT/'data/wikipedia-districts.json').read_text())
    seed = json.loads((ROOT/'data/district-matching-seed.json').read_text())
    old = {(r['name'], r['county']): r for r in seed['comparison']}
    directory = BeautifulSoup((raw/'boardbook-directory.html').read_text(), 'html.parser')
    current = {urljoin(BOARD, a['href']).lower(): a.get_text(' ',strip=True)
               for a in directory.select('a[href]') if '/public/organization/' in a['href'].lower()}
    districts=[]
    for row in rows:
        prior = old.get((row['name'], row['county']), {})
        matches = [m for m in prior.get('matches', []) if m['url'].lower() in current]
        key = hashlib.sha256((row['name']+'|'+row['county']).encode()).hexdigest()[:16]
        districts.append(dict(district_id=key, school_district=row['name'], county_name=row['county'],
            region=row['region'], wikipedia_url=row['url'], county_source=WIKI,
            boardbook_url=matches[0]['url'] if len(matches)==1 else None,
            boardbook_name=matches[0]['name'] if len(matches)==1 else None,
            registry_note=prior.get('note',''), official_website=None))
    target=ROOT/'data/districts.json'
    target.write_text(json.dumps(districts,indent=2))
    (ROOT/'data/registry-provenance.json').write_text(json.dumps(dict(
        wikipedia_source=WIKI, boardbook_source=BOARD, wikipedia_snapshot_date='2026-11-02',
        counties='County grouping in Wikipedia; districts may span other counties.',
        matching='Frozen manually resolved name/county mapping; new districts require explicit source matching.',
        warnings=warnings),indent=2))
    return districts

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--refresh',action='store_true')
    args=parser.parse_args();rows=build(args.refresh)
    print(json.dumps(dict(districts=len(rows),boardbook=sum(bool(r['boardbook_url']) for r in rows))))
