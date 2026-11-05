"""Bounded official-site crawl for board portals and document archives.

Run from code/: ../.venv/bin/python detect_platform.py [--refresh]
Evidence is append-only and resumable by stage and TEA id. Page bodies are kept
in Fetcher's disk cache; evidence keeps links, excerpts, CMS and failure reasons.
"""
from __future__ import annotations

import argparse
import html as html_module
import gzip
import json
import re
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

import browser
from config import REFERENCE, ROOT
from fetch import Fetcher
from match_boardbook import (EVIDENCE, inspect_org, latest_evidence, load_csv,
                            normalize_name, rebuild_map)
from records import append_jsonl

BOARD_WORDS = re.compile(r'\b(?:board|trustees?|meetings?|agendas?|minutes)\b', re.I)
DOCUMENT_WORDS = re.compile(r'\b(?:agendas?|minutes)\b', re.I)
NON_BOARD_ARCHIVE = re.compile(r'\bshac\b|school[\s-]+health[\s-]+advisory|safety.*security|campaign[\s-]*finance', re.I)
_browser_lock = threading.Lock()  # the whole open/read/close session is serialized


def classify_link(url: str) -> tuple[str, str] | None:
    """Canonical public listing URLs, accepting only actual vendor host suffixes."""
    url = html_module.unescape(url).strip().replace('\\/', '/')
    if url.startswith('//'):
        url = 'https:' + url
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower()
    except ValueError:
        return None
    host = (parts.hostname or '').lower()
    path = unquote(parts.path)
    query = parse_qs(parts.query)
    # Follow common outbound redirect wrappers before classifying their target.
    redirect_keys = ('url', 'URL', 'u', 'target', 'redirect')
    if host in ('google.com', 'www.google.com') and path == '/url':
        redirect_keys += ('q',)
    for key in redirect_keys:
        for target in query.get(key, []):
            if target.startswith(('https://', 'http://')) and target != url:
                match = classify_link(target)
                if match:
                    return match
    if host == 'boardbook.org' or host.endswith('.boardbook.org'):
        match = re.search(r'/Public/Organization/(\d+)', path, re.I)
        if match:
            return 'boardbook', f'https://meetings.boardbook.org/Public/Organization/{match.group(1)}'
    if host == 'boarddocs.com' or host.endswith('.boarddocs.com'):
        match = re.search(r'/tx/([^/]+)/', path, re.I)
        if match:
            return 'boarddocs', f'https://go.boarddocs.com/tx/{match.group(1)}/Board.nsf/Public'
    diligent = ('community.diligentoneplatform.com', 'community.highbond.com',
                'communitybydiligent.com', 'diligent.community', 'civicweb.net', 'icompasstech.com')
    if any(host == suffix or host.endswith('.' + suffix) for suffix in diligent):
        return 'diligent', f'https://{host}/Portal/'
    if host.endswith('.legistar.com'):
        return 'legistar', f'https://{host}/Calendar.aspx'
    if host == 'simbli.eboardsolutions.com':
        sid = next(iter(query.get('S', query.get('s', []))), '')
        if sid.isdigit():
            return 'simbli', f'https://simbli.eboardsolutions.com/Index.aspx?S={sid}'
    # Granicus and Swagit are video archives, represented as website sources.
    if host.endswith(('.granicus.com', '.swagit.com')):
        return 'website', urlunsplit((parts.scheme or 'https', parts.netloc, parts.path, parts.query, ''))
    return None


def google_drive_folder(url: str) -> str | None:
    parts = urlsplit(html_module.unescape(url))
    if parts.hostname != 'drive.google.com':
        return None
    query = parse_qs(parts.query)
    match = re.search(r'/drive/(?:u/\d+/)?folders/([\w-]+)', parts.path)
    folder_id = match.group(1) if match else ''
    if parts.path in ('/embeddedfolderview', '/folderview'):
        folder_id = query.get('id', [''])[0]
    if not folder_id or not re.fullmatch(r'[\w-]+', folder_id):
        return None
    canonical = 'https://drive.google.com/drive/folders/' + folder_id
    # Public access to some older shared folders requires this query value.
    if query.get('resourcekey'):
        canonical += '?resourcekey=' + quote(query['resourcekey'][0], safe='')
    return canonical


def detect_cms(markup: str) -> str:
    text = markup.lower()
    for name, markers in (
        ('Finalsite', ('finalsite', 'schoolwires', 'blackboard', 'fsclient', 'fs_pagebody')),
        ('Apptegy/Thrillshare', ('apptegy', 'thrillshare', 'cmsv2-assets', 'core-docs.s3')),
        ('Edlio', ('edlio', 'edl.io', 'edl-page')),
        ('ParentSquare SmartSites', ('smartsites', 'gabbart', 'parentsquarecdn', 'files.gabbart')),
        ('SchoolBlocks', ('schoolblocks',)),
        ('WordPress', ('wp-content/', 'wp-includes/', 'content="wordpress')),
    ):
        if any(marker in text for marker in markers):
            return name
    return 'other'


def clean_url(url: str, base: str) -> str:
    if 'left off the folder name' in unquote(url).lower():
        return ''  # an unfilled Edlio navigation template, not a published archive
    try:
        result = urljoin(base, html_module.unescape(url.strip()))
        parts = urlsplit(result)
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.netloc:
        return ''
    if parts.hostname in ('google.com', 'www.google.com') and parts.path == '/url':
        target = parse_qs(parts.query).get('q', [''])[0]
        if target.startswith(('https://', 'http://')):
            return clean_url(target, base)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or '/', parts.query, ''))


def apptegy_menu_links(script: str, base: str) -> list[tuple[str, str]]:
    if 'clientWorkStateTemp' not in script:
        return []
    match = re.search(r'JSON.parse\(("(?:\\.|[^"\\])*")\)', script)
    if not match:
        return []
    try:
        state = json.loads(json.loads(match.group(1)))
    except (ValueError, TypeError):
        return []
    parts = urlsplit(base)
    origin = f'{parts.scheme}://{parts.netloc}'
    prefix = state.get('pathPrefix') or ''
    result = []

    def walk(nodes):
        for node in nodes:
            if not isinstance(node, dict) or node.get('hidden'):
                continue
            label = (node.get('name') or '').encode('utf-8', errors='replace').decode('utf-8')
            target = node.get('url')
            if node.get('type') == 'page' and node.get('slug'):
                target = origin + prefix.rstrip('/') + '/page/' + node['slug']
            if isinstance(target, str):
                target = target.encode('utf-8', errors='replace').decode('utf-8')
                url = clean_url(target, base)
                if url:
                    result.append((url, label))
            walk(node.get('children') or [])
    walk(state.get('menu') or [])
    walk(state.get('customMenuItems') or [])
    return result


def links_from(soup, base: str) -> list[tuple[str, str]]:
    links = []
    for anchor in soup.select('a[href], iframe[src]'):
        url = clean_url(anchor.get('href', anchor.get('src', '')), base)
        if url:
            links.append((url, anchor.get_text(' ', strip=True)[:250] or anchor.get('title', '')))
    # Apptegy menus and older JavaScript navigation often put URLs in JSON.
    for script in soup.select('script'):
        links.extend(apptegy_menu_links(script.string or script.get_text(), base))
        for url in re.findall(r'https?:(?:\\/|/){2}[^\s<>"\']+', script.string or script.get_text()):
            url = html_module.unescape(url.replace('\\/', '/')).rstrip('),;')
            if classify_link(url):
                links.append((url, 'embedded navigation URL'))
        navigation = script.string or ''
        for _ in range(2):
            navigation = navigation.replace('\\"', '"').replace('\\/', '/')
        for relative in re.findall(r'"(?:url|href|link)"\s*:\s*"([^"\n]+)"', navigation):
            url = clean_url(relative.replace('\\/', '/'), base)
            if url:
                links.append((url, unquote(relative)))
    return list(dict.fromkeys(links))


def document_evidence(soup, links, url: str, depth: int) -> list[str]:
    """Require document links or year-folder links in board-specific content."""
    main = soup
    for selector in ('main', '#main', '#content', '.main-content', 'article'):
        candidate = soup.select_one(selector)
        if candidate is not None and candidate.name not in ('a', 'script', 'style') and candidate.get_text(strip=True):
            main = candidate
            break
    # Remove global menu links so a footer's 'Board minutes' cannot make every
    # unrelated content page look like a board archive.
    for element in main.select('nav, header, footer, script, style, aside, [role="navigation"], .subnav, #subnav, .navigation, .fsNavigation'):
        element.decompose()
    content = main.get_text(' ', strip=True)
    headings = ' '.join(x.get_text(' ', strip=True) for x in main.select('h1'))
    if soup.title:
        headings += ' ' + soup.title.get_text(' ', strip=True)
    if NON_BOARD_ARCHIVE.search(headings):
        return []
    content_links = links_from(main, url)
    board_context = bool(BOARD_WORDS.search(unquote(url)) or DOCUMENT_WORDS.search(headings)
                         or any(DOCUMENT_WORDS.search(label) for _, label in content_links))
    if not board_context and not (depth > 0 and DOCUMENT_WORDS.search(content[:1000])):
        return []
    documents, folders = [], []
    for target, label in content_links:
        context = f'{label} {unquote(target)}'
        if NON_BOARD_ARCHIVE.search(context) and not re.search(r'\bboard\b', label, re.I):
            continue
        is_doc = bool(re.search(r'\.(?:pdf|docx?|rtf)(?:$|[?#])', target, re.I)
                      or re.search(r'drive.google.com/file/', target)
                      or google_drive_folder(target)
                      or 'docs.google.com/document/' in target
                      or (urlsplit(target).hostname or '') == '5il.co'
                      or re.search(r'/(?:o/[^/]+/)?documents?/', target, re.I)
                      or '/fs/resource-manager/view/' in target.lower()
                      or '/cms/lib/' in target.lower()
                      or 'docviewer.aspx' in target.lower())
        excluded = bool(re.search(r'calendar|monitoring|campaign|finance.report|board.policy|election|newsletter', context, re.I))
        if is_doc and not excluded and (DOCUMENT_WORDS.search(context) or DOCUMENT_WORDS.search(headings)):
            documents.append(f'{label}: {target}')
        if (re.search(r'\b20\d{2}\b', label) and BOARD_WORDS.search(content[:1500])
                and not re.search(r'calendar|news|athletics|budget|tax|election|financ', context, re.I)):
            folders.append(f'{label}: {target}')
    return (documents or folders)[:8]


def get_page(fetcher, url: str, refresh=False) -> tuple[str, str, dict]:
    info = dict(url=url)
    markup = ''
    try:
        response = fetcher.get(url, refresh=refresh, timeout=18)
        final_url, markup = response.url, response.text
        info.update(final_url=final_url, status=response.status, raw_path=response.raw_path,
                    from_cache=response.from_cache)
    except Exception as exc:
        response = getattr(exc, 'response', None)
        if response is None or not browser.is_challenge(response.text):
            info['error'] = f'{type(exc).__name__}: {exc}'[:350]
            return url, '', info
        markup, final_url = response.text, response.url
    if browser.is_challenge(markup):
        info['challenge'] = True
        with _browser_lock:
            try:
                final_url, markup = browser.page_html(url, session='tx-platform-map', wait_ms=1500)
                if browser.is_challenge(markup):
                    info['error'] = 'bot-blocked and browser failed: challenge persists'
                    markup = ''
                else:
                    info['browser'] = True
                    info['final_url'] = final_url
            except Exception as exc:
                info['error'] = f'bot-blocked and browser failed: {exc}'[:350]
                markup = ''
    # Browser DOM strings can contain isolated UTF-16 surrogate code points.
    markup = markup.encode('utf-8', errors='replace').decode('utf-8')
    return final_url, markup, info


def wikipedia_website(fetcher, district: dict) -> tuple[str, dict]:
    name = re.sub(r'\bISD\b', 'Independent School District', district['school_district'])
    name = re.sub(r'\bCISD\b', 'Consolidated Independent School District', name)
    titles = [name, f"{name} ({district['county_name']}, Texas)", f'{name} (Texas)']
    info = dict(source='Wikipedia MediaWiki API official website field', attempts=[])
    for title in titles:
        url = ('https://en.wikipedia.org/w/api.php?action=parse&prop=text&format=json&redirects=1&page='
               + quote(title.replace(' ', '_')))
        attempt = dict(url=url)
        info['attempts'].append(attempt)
        try:
            data = fetcher.get(url, timeout=15).json()
            markup = data.get('parse', {}).get('text', {}).get('*', '')
            soup = BeautifulSoup(markup, 'lxml')
            official = []
            for row in soup.select('table.infobox tr'):
                header = row.find('th')
                if header and header.get_text(' ', strip=True).lower() == 'website':
                    official.extend(a['href'] for a in row.select('a[href^="http"]'))
            if not official:
                official = [a['href'] for a in soup.select('a.external[href^="http"]')
                            if re.fullmatch(r'official (?:web)?site', a.get_text(' ', strip=True), re.I)]
            soup.decompose()
            official = [link for link in official if not any(
                part in (urlsplit(link).hostname or '') for part in ('archive.org', 'wikipedia.org'))]
            if official:
                info.update(url=url, official_website=official[0])
                return official[0], info
            attempt['error'] = 'article has no official website field'
        except Exception as exc:
            attempt['error'] = f'{type(exc).__name__}: {exc}'[:250]
    info['error'] = 'Wikipedia supplied no unambiguous district website'
    return '', info


def unrelated_home(markup: str, district: dict | None = None) -> bool:
    soup = BeautifulSoup(markup, 'lxml')
    text = soup.get_text(' ', strip=True)[:80000].lower()
    page_title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    identity_text = text + ' ' + ' '.join(image.get('alt', '') for image in soup.select('img[alt]'))
    soup.decompose()
    if district:
        identity_text = unicodedata.normalize('NFKD', identity_text).encode('ascii', 'ignore').decode()
        identity_text = re.sub(r'\bmt\.?\b', 'mount', identity_text)
        identity_text = re.sub(r'\bft\.?\b', 'fort', identity_text)
        identity_text = re.sub(r'\beagle\s+mount\b', 'eagle mountain', identity_text)
        identity_text = re.sub(r'\band\b', '&', identity_text)
        identity = re.sub(r'[^a-z0-9&]', '', identity_text)
        expected = normalize_name(district['school_district'])
        aliases = {expected, expected.replace('county', '')}
        if '-' in district['school_district']:
            aliases.add(normalize_name(district['school_district'].split('-')[0]))
        if page_title in ('', 'home', 'homepage', 'welcome'):
            identity += (urlsplit(district.get('website', '')).hostname or '').replace('.', '')
        if not any(alias and alias in identity for alias in aliases):
            return True
    return (bool(re.search(r'domain (?:is )?for sale|buy this domain|casino|online slots|sports betting', text))
            or not re.search(r'school|district|\bisd\b|education|student|campus|trustee', text))


def crawl_district(fetcher, district: dict, seeds: list[dict], refresh=False, max_pages=15) -> dict:
    start = time.monotonic()
    pages, sources, cms = [], {(s['platform'], s['entry_url']): s for s in seeds}, {}
    root = district['website']
    queue = [(root, 0, 'TEA website')] if root else []
    visited, docs_pages, portal_evidence = set(), [], []
    fallback_attempted = False
    budget = min(max_pages, 2) if seeds else max_pages
    official_host = (urlsplit(root).hostname or '').removeprefix('www.')
    while queue and len(visited) < budget:
        queue.sort(key=lambda item: (-link_priority(item[0], item[2]), item[1]))
        url, depth, label = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        final, markup, info = get_page(fetcher, url, refresh)
        info.update(depth=depth, link_text=label)
        pages.append(info)
        if depth == 0 and (not markup or unrelated_home(markup, district)):
            if markup:
                info['error'] = 'unrelated or parked TEA website'
            if not fallback_attempted:
                fallback_attempted = True
                parts = urlsplit(url)
                host = parts.hostname or ''
                if host.startswith('www') and not host.startswith('www.'):
                    repaired = urlunsplit((parts.scheme, 'www.' + host[3:], parts.path, parts.query, ''))
                    pages.append(dict(source='repair missing www dot in TEA URL', original_url=url, url=repaired))
                    queue.append((repaired, 0, 'TEA URL repaired missing www dot'))
                if not markup and 'SSLError' in info.get('error', '') and parts.scheme == 'https':
                    http_url = urlunsplit(('http', parts.netloc, parts.path, parts.query, ''))
                    queue.append((http_url, 0, 'HTTP fallback after TLS failure'))
                fallback, evidence = wikipedia_website(fetcher, district)
                pages.append(evidence)
                if fallback and fallback not in visited:
                    root = fallback
                    official_host = (urlsplit(root).hostname or '').removeprefix('www.')
                    queue.append((fallback, 0, 'Wikipedia official website fallback'))
            if not markup or info.get('error'):
                continue
        if not markup:
            continue
        if depth == 0:
            queue = [item for item in queue if item[1] != 0]
            official_host = (urlsplit(final).hostname or '').removeprefix('www.')
        cms[final] = detect_cms(markup)
        info['cms'] = cms[final]
        soup = BeautifulSoup(markup, 'lxml')
        info['title'] = soup.title.get_text(' ', strip=True)[:180] if soup.title else ''
        links = links_from(soup, final)
        info['board_links'] = []
        for target, text in links:
            folder = google_drive_folder(target)
            embedded_board_folder = ('/embeddedfolderview' in target and bool(
                re.search(r'board|trustee|agenda|minute', unquote(final) + ' ' + info['title'], re.I)))
            excluded_folder = NON_BOARD_ARCHIVE.search(text + ' ' + info['title'] + ' ' + unquote(final)) and not re.search(r'\bboard\b', text, re.I)
            if folder and not excluded_folder and (DOCUMENT_WORDS.search(text) or embedded_board_folder):
                sources[('website', folder)] = dict(platform='website', entry_url=folder,
                    evidence=f'{final} links to {target} ({text})', note='CMS: Google Drive; official board archive link')
            classified = classify_link(target)
            if classified:
                platform, entry = classified
                # Never accept generic BoardBook/BoardDocs vendor homepages.
                evidence = f'{final} links to {target} ({text})'
                if platform == 'boardbook':
                    key = (platform, entry)
                    if key not in sources:
                        org = inspect_org(fetcher, dict(org_id=entry.rsplit('/',1)[-1], name=text, url=entry))
                        if not org.get('meetings'):
                            info.setdefault('rejected_portals', []).append(dict(url=entry, reason=org))
                            continue
                sources[(platform, entry)] = dict(platform=platform, entry_url=entry,
                                                   evidence=evidence, note='Linked from district website')
                portal_evidence.append(dict(page=final, target=target, platform=platform, entry_url=entry, text=text))
                continue
            if depth >= 2:
                continue
            host = (urlsplit(target).hostname or '').removeprefix('www.')
            if not (host == official_host or host.endswith('.' + official_host)):
                continue
            if re.search(r'\.(?:pdf|docx?|xlsx?|jpe?g|png|zip|mp4|m4a)(?:$|[?#])', target, re.I) or '/fs/resource-manager/view/' in target:
                continue
            context = text + ' ' + unquote(urlsplit(target).path).replace('-', ' ').replace('_', ' ')
            generic_hub = depth == 0 and bool(re.fullmatch(
                r'(?:our\s+)?district|about(?:\s+us)?|administration|school\s+board|leadership|district\s+information|required\s+(?:postings|notices)', text.strip(), re.I))
            url_hint = re.search(r'(?<!dash)board|trustee|agenda|minutes?', unquote(urlsplit(target).path), re.I)
            if (BOARD_WORDS.search(context) or url_hint or generic_hub) and not re.search(
                    r'/(?:news|announcements|article|calendar|events)(?:/|$|\?)|finance.report|election|board.policy',
                    unquote(target), re.I):
                info['board_links'].append(dict(url=target, text=text))
                if target not in visited and all(item[0] != target for item in queue):
                    queue.append((target, depth+1, text))
        docs = document_evidence(soup, links, final, depth)
        if docs:
            info['document_examples'] = docs
            docs_pages.append(dict(platform='website', entry_url=final,
                                   evidence='; '.join(docs)[:1800], note=f'CMS: {cms[final]}'))
        # On a verified portal district, the first three useful board pages
        # normally expose both old and current platform links.
        soup.decompose()
        if len(visited) >= 3 and portal_evidence:
            break
    # Prefer actual document listings to broad parent hubs with year links.
    for source in docs_pages:
        sources[('website', source['entry_url'])] = source
    if not sources:
        errors = [page.get('error', '') for page in pages]
        if any('bot-blocked' in error for error in errors):
            reason = 'bot-blocked and browser failed'
        elif any('unrelated or parked' in error for error in errors):
            reason = 'unrelated/outdated TEA website; official-site fallback failed'
        elif not any(page.get('title') for page in pages):
            reason = 'site down or inaccessible; official-site fallback failed'
        elif any('login' in page.get('title', '').lower() for page in pages):
            reason = 'login required'
        else:
            reason = 'no board documents page found within crawl limit'
    else:
        reason = 'found'
    return dict(tea_id=district['tea_id'], school_district=district['school_district'], stage='detect',
                completed_at=datetime.now(timezone.utc).isoformat(), sources=list(sources.values()),
                pages=pages, matched_links=portal_evidence, cms=cms, reason=reason,
                elapsed_seconds=round(time.monotonic()-start, 2), pages_visited=len(visited))


def link_priority(url: str, text: str) -> int:
    value = (unquote(url) + ' ' + text).lower()
    return (8 * bool(re.search(r'agenda|minutes', value))
            + 5 * bool(re.search(r'board.*meeting|meeting.*board', value))
            + 3 * bool(re.search(r'trustee|school.board|board.of', value))
            - 8 * bool(re.search(r'policy|election|member|recognition|budget', value)))


def run(refresh=False, workers=16, tea_ids=None, max_pages=15):
    start = time.monotonic()
    fetcher = Fetcher()
    evidence = latest_evidence()
    districts = [d for d in load_csv(REFERENCE / 'districts.csv')
                 if (refresh or (d['tea_id'], 'detect') not in evidence)
                 and (not tea_ids or d['tea_id'] in tea_ids)]
    print(json.dumps(dict(stage='detect', pending=len(districts))), flush=True)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(crawl_district, fetcher, d,
                                   evidence.get((d['tea_id'], 'boardbook'), {}).get('sources', []),
                                   refresh, max_pages): d for d in districts}
            counts = Counter()
            for number, future in enumerate(as_completed(futures), 1):
                district = futures.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(tea_id=district['tea_id'], school_district=district['school_district'],
                                  stage='detect', sources=evidence.get((district['tea_id'], 'boardbook'), {}).get('sources', []),
                                  pages=[], cms={}, reason='discovery error: ' + str(exc)[:250],
                                  completed_at=datetime.now(timezone.utc).isoformat())
                append_jsonl(EVIDENCE, result)
                counts[result['reason']] += 1
                if number % 25 == 0 or number == len(districts):
                    rebuild_map()
                    print(json.dumps(dict(stage='detect', done=number, total=len(districts),
                                          reasons=dict(counts), wall_seconds=round(time.monotonic()-start, 1))), flush=True)
        if districts:
            canonicalize_portals(fetcher)
            validate_drive_archives(fetcher)
        rebuild_map()
    finally:
        with _browser_lock:
            browser.close('tx-platform-map')



def canonicalize_portals(fetcher: Fetcher) -> dict:
    """Collapse vendor aliases only after observing a redirect to the same platform."""
    evidence = latest_evidence()
    entries = {source['entry_url'] for (key, stage), row in evidence.items() if stage == 'detect'
               for source in row.get('sources', []) if source['platform'] == 'diligent'}
    resolved, checks = {}, {}
    for entry in sorted(entries):
        final, markup, check = get_page(fetcher, entry)
        classified = classify_link(final)
        if markup and classified and classified[0] == 'diligent':
            resolved[entry] = classified[1]
        checks[entry] = check
    for (key, stage), row in evidence.items():
        if stage != 'detect':
            continue
        original_entries = {s['entry_url'] for s in row.get('sources', [])}
        unique, changed = {}, False
        for source in row.get('sources', []):
            entry = source['entry_url']
            if entry in resolved and resolved[entry] != entry:
                source = dict(source, entry_url=resolved[entry],
                              note=source.get('note', '') + f'; redirects from {entry}')
                changed = True
            unique[(source['platform'], source['entry_url'])] = source
        if changed:
            row['sources'] = list(unique.values())
            row['portal_redirect_checks'] = {url: checks[url] for url in checks
                                              if url in original_entries}
            append_jsonl(EVIDENCE, row)
    for (key, stage), row in evidence.items():
        if stage == 'spot_check' and row.get('verified') and row['entry_url'] in resolved:
            row['opened_url'] = row['entry_url']
            row['entry_url'] = resolved[row['entry_url']]
            append_jsonl(EVIDENCE, row)
    rebuild_map()
    return dict(aliases=sum(url != final for url, final in resolved.items()), verified=len(resolved))


def validate_drive_archives(fetcher: Fetcher) -> dict:
    """Reject private, deleted, and explicitly non-board Drive collections."""
    evidence = latest_evidence()
    urls = {s['entry_url'] for (key, stage), row in evidence.items() if stage == 'detect'
            for s in row.get('sources', []) if urlsplit(s['entry_url']).hostname == 'drive.google.com'}
    checks = {c['url']: c for (key, stage), row in evidence.items() if stage == 'detect'
              for c in row.get('archive_checks', [])}

    def check(url):
        final, markup, page = get_page(fetcher, url)
        soup = BeautifulSoup(markup, 'lxml')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        excerpt = soup.get_text(' ', strip=True)[:800]
        soup.decompose()
        return dict(url=url, title=title, final_url=final, excerpt=excerpt, page=page,
                    nonboard=bool(NON_BOARD_ARCHIVE.search(title)) and not re.search(r'\bboard\b', title, re.I),
                    private=(urlsplit(final).hostname == 'accounts.google.com'
                             or bool(re.search(r'^sign in|access denied|google drive: sign-in', title, re.I))),
                    gone=bool(re.search(r'HTTPError: (?:404|410)\b', page.get('error', ''))))

    with ThreadPoolExecutor(max_workers=8) as pool:
        for result in pool.map(check, sorted(urls)):
            checks[result['url']] = result
    bad = {url for url, check in checks.items() if check['nonboard'] or check['private'] or check['gone']}
    bad_ids = {urlsplit(url).path.rsplit('/', 1)[-1] for url in bad}
    removed = 0
    for (key, stage), row in evidence.items():
        if stage == 'spot_check' and row['entry_url'] in bad:
            row.update(verified=False, verification_note='Not a public school board archive')
            append_jsonl(EVIDENCE, row)
        if stage != 'detect':
            continue
        relevant = {s['entry_url'] for s in row.get('sources', []) if s['entry_url'] in checks}
        relevant.update(c['url'] for c in row.get('archive_checks', []))
        if not relevant:
            continue
        row['archive_checks'] = [checks[url] for url in sorted(relevant)]
        before = len(row['sources'])
        row['sources'] = [s for s in row['sources'] if s['entry_url'] not in bad]
        if relevant & bad:
            for page in row.get('pages', []):
                raw = page.get('raw_path')
                if page.get('error') or not raw or not (ROOT / raw).exists():
                    continue
                markup = gzip.decompress((ROOT / raw).read_bytes()).decode('utf-8', errors='replace')
                if browser.is_challenge(markup):
                    continue
                url = page.get('final_url', page.get('url', ''))
                soup = BeautifulSoup(markup, 'lxml')
                for node in list(soup.select('a[href],iframe[src]')):
                    folder = google_drive_folder(clean_url(node.get('href', node.get('src', '')), url))
                    if folder and urlsplit(folder).path.rsplit('/', 1)[-1] in bad_ids:
                        node.decompose()
                examples = document_evidence(soup, links_from(soup, url), url, page.get('depth', 1))
                soup.decompose()
                if not examples:
                    row['sources'] = [s for s in row['sources'] if not (s['platform'] == 'website' and s['entry_url'] == url)]
                else:
                    for source in row['sources']:
                        if source['platform'] == 'website' and source['entry_url'] == url:
                            source['evidence'] = '; '.join(examples)[:1800]
        removed += before - len(row['sources'])
        if row['sources']:
            row['reason'] = 'found'
        elif any(checks[url]['private'] for url in relevant):
            row['reason'] = 'login required'
        elif any(checks[url]['gone'] for url in relevant):
            row['reason'] = 'archive links unavailable (HTTP 404/410)'
        else:
            row['reason'] = 'no board documents page found within crawl limit'
        append_jsonl(EVIDENCE, row)
    rebuild_map()
    return dict(checked=len(urls), rejected=len(bad), removed_sources=removed)


def mapping_summary() -> dict:
    """Report source counts, CMS by unique website host, and unfinished scans."""
    rows = load_csv(REFERENCE / 'platforms.csv')
    evidence = latest_evidence()
    districts = {d['tea_id']: d for d in load_csv(REFERENCE / 'districts.csv')}
    per_district = {key: set() for key in districts}
    hosts = {}
    host_counts, host_districts = Counter(), {}
    for row in rows:
        per_district[row['tea_id']].add(row['platform'])
        if row['platform'] == 'website':
            host = urlsplit(row['entry_url']).hostname or ''
            scan = evidence.get((row['tea_id'], 'detect'), {})
            cms = scan.get('cms', {}).get(row['entry_url'])
            if not cms:
                cms = row['note'].removeprefix('CMS: ').split(';')[0] if row['note'].startswith('CMS: ') else 'other'
            hosts.setdefault(host, Counter())[cms] += 1
            host_counts[host] += 1
            host_districts.setdefault(host, set()).add(row['tea_id'])
    host_cms = {host: votes.most_common(1)[0][0] for host, votes in hosts.items()}
    current_urls = {(r['tea_id'], r['entry_url']) for r in rows}
    return dict(
        districts=len(per_district), rows=len(rows),
        counts_by_platform=dict(Counter(r['platform'] for r in rows)),
        districts_by_platform={p: sum(p in values for values in per_district.values())
                               for p in sorted({r['platform'] for r in rows})},
        none_by_reason=dict(Counter(r['note'] for r in rows if r['platform'] == 'none')),
        multiple_platforms=[dict(tea_id=key, school_district=districts[key]['school_district'], platforms=sorted(values))
                            for key, values in per_district.items() if len(values - {'none'}) > 1],
        website_hosts=dict(sorted(host_cms.items())),
        website_host_frequency=[dict(host=host, cms=host_cms[host], source_rows=count,
                                     districts=len(host_districts[host]))
                                for host, count in host_counts.most_common()],
        website_cms_by_unique_host=dict(Counter(host_cms.values())),
        unfinished=[key for key in districts if (key, 'detect') not in evidence],
        spot_checks=[r for (key, stage), r in evidence.items()
                     if stage == 'spot_check' and r.get('verified') and (key, r['entry_url']) in current_urls],
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--report', action='store_true', help='print mapping counts and evidence summary without HTTP')
    parser.add_argument('--workers', type=int, default=16)
    parser.add_argument('--max-pages', type=int, default=15)
    parser.add_argument('--tea-ids')
    args = parser.parse_args()
    if args.report:
        print(json.dumps(mapping_summary(), indent=2, ensure_ascii=True))
    else:
        run(args.refresh, args.workers, set(args.tea_ids.split(',')) if args.tea_ids else None, args.max_pages)
