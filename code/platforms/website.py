"""Collect board meeting documents from district websites and their file CDNs.

The crawl is bounded to 200 listing requests, including JSON/AJAX folders.
DISCOVERY_STATS and the completion log expose partial crawls to callers without
changing the platform's list-of-records interface.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from datetime import date
from urllib.parse import parse_qs, urlencode, unquote, urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup, NavigableString
from requests.exceptions import HTTPError, RetryError, SSLError

import browser
from extract import clean_text
from records import make_record

PLATFORM = "website"
MAX_LISTING_PAGES = 200
DISCOVERY_STATS: dict[str, dict] = {}
_LOG = logging.getLogger(__name__)
# Up to BROWSER_SLOTS districts may run a browser session at once; each holds
# its slot for the whole open-read-close sequence of one page.
_BROWSER_LOCK = threading.BoundedSemaphore(min(2, browser.BROWSER_SLOTS))
_DOC = re.compile(r"\.(?:pdf|docx?|txt|rtf|jpe?g|png)(?:$|[?#])", re.I)
_TOPIC = re.compile(r"minutes?|\bmins\b|agendas?|packets?|supporting|attachments?|meeting\s+(?:notice|material)|notice\s+(?:of\s+)?(?:public\s+)?meeting|bulletins?", re.I)
_BOARD = re.compile(r"board|trustees?", re.I)
_EXCLUDE = re.compile(r"staff\s+directory|operations\s+plan|public\s+comment|sign\s+up|lunch|breakfast|menu|calendar|policy|policies|manual|operating\s+procedures|election|ballot|candidate|qualifications|conflict\s+of\s+interest|check\s+register|report\s+card|\btapr\b|\bfirst\s+rating|superintendent\s+contract|tax\s+rate\s+calculation|adopted\s+budget|proposed\s+budget|curriculum|innovation\s+plan", re.I)
_CDNS = ("thrillshare.com", "apptegy.net", "5il.co", "aptg.co", "finalsite.net", "edl.io", "parentsquare.com", "sharpschool.com", "cdn-website.com", "filesusr.com", "wixstatic.com", "core-docs.s3.amazonaws.com", "core-docs.s3.us-east-1.amazonaws.com", "docs.google.com", "drive.google.com")
_CDNS += ("myconnectsuite.com", "schoolinsites.com", "ucarecd.net",
          "ecs-cluster-bucket-wsos-prod-two.s3.us-west-2.amazonaws.com")
_MONTHS = {name.lower(): n for n, names in enumerate([
    (), ("January", "Jan"), ("February", "Feb"), ("March", "Mar"),
    ("April", "Apr"), ("May",), ("June", "Jun"), ("July", "Jul"),
    ("August", "Aug"), ("September", "Sept", "Sep"), ("October", "Oct"),
    ("November", "Nov"), ("December", "Dec")]) for name in names}
_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Sept?|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Oct|Nov|Dec)"


def _words(value: str) -> str:
    return re.sub(r"\s+", " ", unquote(value).replace("_", " ").replace("\xa0", " ")).strip()


# Format reliability, most trusted first: a written month name with a
# four-digit year, then numeric forms with a four-digit year, then two-digit
# years and run-together digits. A name can hold several dates (a plan's
# "2023-2028" span next to its "4-18-2023" meeting date); the most reliable
# plausible one wins, not the leftmost.
_RANK = {"named": 0, "named_ymd": 0, "mdy4": 1, "ymd": 1, "compact": 2, "mdy2": 3, "short": 4, "ambiguous": 5}


def _plausible(value: date) -> bool:
    """Meetings in this archive run from 2000 to a few months past the run
    date (boards post upcoming agendas days or weeks ahead); anything else is
    a misread string, not a meeting."""
    return date(2000, 1, 1) <= value and (value - date.today()).days <= 120


def _explicit_dates(text: str) -> list[tuple[int, int, str, str]]:
    """Return (rank, position, iso date, matched text) for each full date in text."""
    patterns = [
        (r"(?<!\d)((?:19|20)\d{2})[-./ ](\d{1,2})[-./ ](\d{1,2})(?!\d)", "ymd"),
        (r"(?<!\d)(\d{1,2})[-./ ]+(\d{1,2})[-./ ]+(\d{4}|\d{2})(?!\d)", "mdy"),
        (rf"\b({_MONTH})\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?[,.\s-]+((?:19|20)\d{{2}})\b", "named"),
        (rf"\b((?:19|20)\d{{2}})[-\s]+({_MONTH})\.?[-\s]+(\d{{1,2}})\b", "named_ymd"),
        (r"(?<!\d)(\d{8})(?!\d)", "compact"),
        (r"(?<!\d)(\d{6})(?!\d)", "short"),
        (r"(?<!\d)(\d{7})(?!\d)", "ambiguous"),
    ]
    found = []
    for pattern, order in patterns:
        # A lookahead lets matches overlap, so a rejected "1. 9-28" (2028) does
        # not hide the "9-28-22" that follows it.
        for match in re.finditer(rf"(?={pattern})", text, re.I):
            g = match.groups()
            matched = text[match.start(1):match.end(len(g))]
            try:
                if order == "ymd":
                    candidates = [date(*map(int, g))]
                elif order == "mdy":
                    month, day, year = map(int, g)
                    order = "mdy4" if len(g[2]) == 4 else "mdy2"
                    year = year + 2000 if year < 100 else year
                    candidates = [date(year, month, day)]
                elif order == "named":
                    candidates = [date(int(g[2]), _MONTHS[g[0].lower()], int(g[1]))]
                elif order == "named_ymd":
                    candidates = [date(int(g[0]), _MONTHS[g[1].lower()], int(g[2]))]
                elif order in ("compact", "short"):
                    token = g[0]
                    if order == "compact" and token[:4].startswith(("19", "20")):
                        candidates = [date(int(token[:4]), int(token[4:6]), int(token[6:]))]
                    elif order == "compact":
                        candidates = [date(int(token[4:]), int(token[:2]), int(token[2:4]))]
                    else:
                        candidates = [date(2000 + int(token[4:]), int(token[:2]), int(token[2:4]))]
                else:
                    # Seven digits (1022017) can mean Jan 02 or Oct 2: keep only if one reading is valid.
                    token, candidates = g[0], []
                    for split in (1, 2):
                        try:
                            candidates.append(date(int(token[-4:]), int(token[:split]), int(token[split:-4])))
                        except ValueError:
                            pass
                    if len({c for c in candidates if _plausible(c)}) != 1:
                        continue
            except (ValueError, KeyError):
                continue
            candidates = [c for c in candidates if _plausible(c)]
            if len(candidates) == 1:
                found.append((_RANK[order], match.start(), candidates[0].isoformat(), matched))
            order = "mdy" if order in ("mdy4", "mdy2") else order
    return found


def parse_date(value: str, context: str = "", partial: bool = True) -> tuple[str | None, str | None]:
    """Parse a plausible explicit date from value; optionally fall back to a
    month/day label plus a year from the folder or heading context.

    Genuinely ambiguous or implausible values stay null rather than inventing
    a meeting date.
    """
    text = _words(value)
    found = _explicit_dates(text)
    if found:
        _, _, parsed, raw = min(found)
        return parsed, raw
    if not partial:
        return None, None
    label = re.search(rf"\b({_MONTH})\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\b", text, re.I)
    years = re.findall(r"\b(?:19|20)\d{2}\b", context)
    short_year = re.search(r"\b((?:19|20)\d{2})\s*[-–]\s*(\d{2})(?!\d)", context)
    if short_year and len(years) == 1:
        end = int(short_year[1]) // 100 * 100 + int(short_year[2])
        if end == int(short_year[1]) + 1:
            years.append(str(end))
    if label and years:
        month, day = _MONTHS[label[1].lower()], int(label[2])
        year = int(years[0])
        # School-year folders run July through June; a single year is calendar year.
        if len(years) >= 2 and int(years[1]) == year + 1 and month < 7:
            year += 1
        try:
            value = date(year, month, day)
        except ValueError:
            return None, None
        if _plausible(value):
            return value.isoformat(), label.group() + " | " + context
    return None, None


def date_for(label: str, url: str, context: str) -> tuple[str | None, str | None]:
    """Meeting date for a website document, most trusted source first: a full
    date in the link text, then in the file name, then a month/day label with
    a context year, then a date in the listing context (a "February 20"
    heading above links named "Part 1", with the year from an "Agendas 2024"
    heading).

    Also used by build_dataset to re-date already-collected rows."""
    filename = unquote(urlparse(url).path.split("/")[-1])
    stem = re.sub(r"\.[a-z0-9]+$", "", filename, flags=re.I)
    if re.fullmatch(r"[0-9a-f_-]{20,}", stem, re.I):
        filename = ""  # CMS resource identifiers are not date-bearing names.
    for value, ctx, partial in ((label, "", False), (filename, "", False), (label, context, True), (context, context, True)):
        parsed, raw = parse_date(value, ctx, partial)
        if parsed:
            return parsed, raw
    return None, None


def classify_detail(label: str, context: str = "") -> str:
    for text in (label, *context.split("|")):
        if re.search(r"packet|supporting|attachment|exhibits?|supplement|bulletin|presentation", text, re.I):
            return "Extras"
        if re.search(r"minutes?|\bmins\b|\bmin\d", text, re.I):
            return "Minutes"
        if re.search(r"agenda", text, re.I):
            return "Agenda"
    return "Other"


def _meeting_type(label: str, context: str) -> str | None:
    # A document's own label wins over an article titled Workshop & Regular.
    for text in (label, context):
        m = re.search(r"special[ -]called|special|workshop|called|regular|emergency|public\s+hearing|training", text, re.I)
        if m:
            return m.group().replace("-", " ").title()
    return None


def _district_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _allowed(url: str, entry: str) -> bool:
    parsed = urlparse(url)
    host, base = (parsed.hostname or "").lower(), _district_host(entry)
    return parsed.scheme in ("http", "https") and bool(host) and (
        host.removeprefix("www.") == base or host.endswith("." + base)
        or any(host == suffix or host.endswith("." + suffix) for suffix in _CDNS))


def _paperless_agenda(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.hostname in ("www.mywebschooltools.com", "mywebschooltools.com") and parsed.path.endswith("/viewagenda.php")


def _novus_agenda(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").endswith(".novusagenda.com") and parsed.path.lower().endswith("/meetingview.aspx")


def _document_url(url: str) -> bool:
    p = urlparse(url)
    return bool(_DOC.search(url) or p.hostname in ("5il.co", "aptg.co")
                or _paperless_agenda(url) or _novus_agenda(url)
                or (p.hostname == "drive.google.com" and p.path in ("/open", "/uc") and parse_qs(p.query).get("id"))
                or (p.hostname or "").endswith(".ucarecd.net")
                or re.search(r"/fs/resource-manager/(?:view|download)/|/api/documents/|/document/d/|/file/d/|/file/|file\.ashx|/uc\?|/(?:Agenda|Minutes)Viewer\.php|/videos/\d+/agenda", url, re.I))


def _drive_folder(url: str) -> str | None:
    parsed = urlparse(url)
    if parsed.hostname not in ("drive.google.com", "docs.google.com"):
        return None
    match = re.search(r"/folders/([\w-]+)", parsed.path)
    params = parse_qs(parsed.query)
    folder_id = match[1] if match else None
    if not folder_id and parsed.path in ("/embeddedfolderview", "/folderview"):
        folder_id = params.get("id", [None])[0]
    if not folder_id:
        return None
    query = {"id": folder_id}
    if params.get("resourcekey"):
        query["resourcekey"] = params["resourcekey"][0]
    return "https://drive.google.com/embeddedfolderview?" + urlencode(query)


def _drive_file(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname != "drive.google.com":
        return url
    match = re.search(r"/file/d/([\w-]+)", parsed.path)
    params = parse_qs(parsed.query)
    file_id = match[1] if match else params.get("id", [None])[0] if parsed.path in ("/open", "/uc") else None
    if not file_id:
        return url
    # Sharing UI adds different usp values for the same file in embedded and
    # hydrated listings. Keep the resource key, which can grant public access.
    query = urlencode({"resourcekey": params["resourcekey"][0]}) if params.get("resourcekey") else ""
    return "https://drive.google.com/file/d/" + file_id + "/view" + ("?" + query if query else "")


def _board_document(label: str, context: str) -> bool:
    if _EXCLUDE.search(label) or _EXCLUDE.search(context.split("|")[0]):
        return False
    # Local folder headings can identify opaque files, but a broad "District"
    # page cannot turn every PDF on the page into a board document.
    context = re.sub(r"Agendas,? Minutes,? and Information", "", context, flags=re.I)
    if _TOPIC.search(label):
        return True
    return bool(_TOPIC.search(context) and (
        parse_date(label, context)[0] or re.search(r"\b(?:meeting|regular|called|special|workshop|training|part\s*\d|notice)\b", label, re.I)
        or re.fullmatch(rf"(?:{_MONTH})(?:[- ](?:19|20)\d{{2}})?(?:\.pdf)?", label.strip(), re.I)))


def _page_context(soup, inherited: str) -> str:
    headings = [h.get_text(" ", strip=True) for h in soup.select("h1")]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    return " | ".join([inherited, *headings[:2], title])[:700]


def _link_context(a, page_context: str) -> str:
    pieces = [a["data-file-name"]] if a.get("data-file-name") else []
    cell = a.find_parent(["td", "th"])
    table = a.find_parent("table")
    if cell and table:
        cells = cell.parent.find_all(["td", "th"], recursive=False)
        header = table.find("tr")
        headers = header.find_all(["td", "th"], recursive=False) if header else []
        if header is not cell.parent and len(headers) == len(cells):
            index = next((i for i, item in enumerate(cells) if item is cell), None)
            if index is not None:
                pieces.append(headers[index].get_text(" ", strip=True))
    for parent in a.parents:
        if parent.name in ("li", "tr", "article") or "fsPostItem" in parent.get("class", []):
            text = parent.get_text(" ", strip=True)
            if len(text) < 500:
                pieces.append(text)
                break
        if parent.name in ("main", "body"):
            break
    # Prefer section headings over whole-page text, especially on Wix pages
    # containing both finance postings and board minutes.
    block = a.find_parent(["p", "h2", "h3", "h4", "h5", "h6"])
    if block:
        prefix = []
        for node in block.descendants:
            if node is a:
                break
            if isinstance(node, NavigableString):
                prefix.append(str(node))
        nearby = " ".join(prefix).rsplit("|", 1)[-1].strip()
        if nearby and len(nearby) < 120:
            pieces.append(nearby)
    for heading in ([] if table else a.find_all_previous(re.compile(r"^h[1-6]$|^strong$"), limit=20)):
        if heading.find_parent("a"):
            continue
        if table and heading.find_parent("table") is table:
            continue
        prefix = []
        for node in heading.descendants:
            if getattr(node, "name", None) == "a":
                break
            if isinstance(node, NavigableString):
                prefix.append(str(node))
        text = " ".join(prefix).strip()
        if not text or len(text) > 180:
            continue
        pieces.append(text)
        if _TOPIC.search(text):
            break
        if len(pieces) >= 5:
            break
    return " | ".join([*pieces, page_context])[:1000]


def _nuxt_data(values):
    """Decode Nuxt/devalue's indexed JSON, without evaluating JavaScript."""
    memo = {}

    def decode(index):
        if not isinstance(index, int) or index < 0 or index >= len(values):
            return None
        if index in memo:
            return memo[index]
        value = values[index]
        if isinstance(value, dict):
            out = memo[index] = {}
            out.update({k: decode(v) for k, v in value.items()})
            return out
        if isinstance(value, list):
            if value and isinstance(value[0], str) and value[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef"):
                return decode(value[1])
            out = memo[index] = []
            out.extend(decode(v) for v in value)
            return out
        return value

    root = decode(0)
    if isinstance(root, dict):
        page = root.get("pinia", {}).get("main", {}).get("page")
        if page:
            return page
    return root.get("data", root) if isinstance(root, dict) else root


def _json_links(value, base: str, context: str = ""):
    """Yield embedded Apptegy files/folders and HTML stored in CMS JSON."""
    if isinstance(value, list):
        local = context
        for item in value:
            if isinstance(item, dict):
                content = item.get("content")
                html = content.get("html", "") if isinstance(content, dict) else ""
                heading = BeautifulSoup(html, "lxml").find(["h1", "h2", "h3", "h4", "strong"]) if html else None
                if heading:
                    text = heading.get_text(" ", strip=True)
                    if _TOPIC.search(text) or re.fullmatch(r"20\d{2}(?:[-–]\d{2,4})?", text):
                        local = text + " | " + context
            yield from _json_links(item, base, local)
    elif isinstance(value, dict):
        properties = value.get("properties") or {}
        label = str(value.get("file_name") or value.get("folder_name") or properties.get("text") or value.get("title") or value.get("text") or value.get("name") or "")
        local = " | ".join(x for x in (label, context) if x)[:1000]
        if str(value.get("type", "")).startswith("LAYOUT_NODE_"):
            columns = value.get("nodes", [])
            nodes = columns[0].get("nodes", []) if columns else []
            html = nodes[0].get("content", {}).get("html", "") if nodes else ""
            heading = BeautifulSoup(html, "lxml").find(["h1", "h2", "h3", "h4", "strong"]) if html else None
            if heading and _TOPIC.search(heading.get_text()):
                local = heading.get_text(" ", strip=True) + " | " + local
        url = value.get("viewLink") or value.get("external_url") or value.get("url") or value.get("file_url") or value.get("download_url") or value.get("short_url") or value.get("href") or value.get("link")
        if isinstance(url, str):
            yield urljoin(base, url), label, context
        folder_id = value.get("id") or value.get("folder_id")
        if folder_id and (value.get("folder_name") or value.get("type") == "folder") and not value.get("file_name") and not url:
            yield urljoin(base, "/documents/" + re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") + "/" + str(folder_id)), label, context
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                yield from _json_links(item, base, local)
            elif isinstance(item, str) and "<a " in item:
                fragment = BeautifulSoup(item, "lxml")
                for a in fragment.select("a[href]"):
                    yield urljoin(base, a["href"]), a.get_text(" ", strip=True), _link_context(a, local)


def _flight_payloads(soup):
    """Read JSON records from Next.js hydration, without executing scripts.

    SchoolBlocks includes public Drive file titles/viewLinks here. Text
    records may end immediately before a JSON record rather than a newline.
    """
    chunks = []
    for script in soup.select("script"):
        match = re.fullmatch(r"\s*self\.__next_f\.push\((\[.*\])\)\s*;?\s*", script.get_text(), re.S)
        if match:
            try:
                value = json.loads(match[1])
                if len(value) > 1 and isinstance(value[1], str):
                    chunks.append(value[1])
            except (ValueError, TypeError):
                continue
    stream = "".join(chunks)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"(?:^|\n|(?<=[>}\]]))[a-f0-9]+:(?=[{\[])", stream):
        try:
            payload, _ = decoder.raw_decode(stream, match.end())
            yield payload
        except ValueError:
            continue


def _render(fetcher, url: str, stats: dict) -> tuple[str, str]:
    with _BROWSER_LOCK:
        session = "website-" + str(threading.get_ident()) + "-" + re.sub(r"\W", "", _district_host(url))
        try:
            try:
                final_url, html = browser.page_html(url, session=session)
            except RuntimeError as exc:
                if not any(marker in str(exc) for marker in ("ERR_NETWORK_CHANGED", "ERR_CERT_VERIFIER_CHANGED")):
                    raise
                browser.close(session)
                final_url, html = browser.page_html(url, session=session)
            cookies = browser.cookies(session)
            if cookies and hasattr(fetcher, "_session"):
                host = urlparse(final_url).hostname or urlparse(url).hostname
                if host:
                    for name, value in cookies.items():
                        fetcher._session().cookies.set(name, value, domain=host, path="/")
            stats["browser_pages"] += 1
            stats["browser_sources"].append(url)
            if browser.is_challenge(html):
                raise RuntimeError("browser did not clear Client Challenge: " + url)
            return final_url, html
        finally:
            browser.close(session)


def _read(fetcher, url: str, stats: dict) -> tuple[object, str, str]:
    _LOG.info("website listing %s", url)
    try:
        response = fetcher.get(url, refresh=True, timeout=35)
    except HTTPError as exc:
        if exc.response is None or exc.response.status_code not in (403, 404):
            raise
        final_url, html = _render(fetcher, url, stats)
        return None, final_url, html
    html, final_url = response.text, response.url
    if browser.is_challenge(html) or "new ContentItemListUI(" in html:
        final_url, html = _render(fetcher, url, stats)
    return response, final_url, html


def _listing_link(url: str, label: str, context: str, entry: str, current: str) -> bool:
    alias = (urlparse(url).hostname or "").endswith(".sites.thrillshare.com") and "/documents/" in urlparse(url).path
    if _district_host(url) != _district_host(entry) and not (urlparse(url).hostname or "").endswith("." + _district_host(entry)) and not alias:
        return False
    path = unquote(urlparse(url).path).lower()
    words = _words(label + " " + path)
    if _EXCLUDE.search(label) or re.search(r"members?|elections?|polic|training.hours|videos?$|contact|login|search|/portlets/", path):
        return False
    if re.search(r"/(?:documents|browse)/", path) and _BOARD.search(words) and re.search(r"meetings?|agenda|minute", words, re.I):
        return True
    entry_parts = urlparse(entry).path.strip("/").split("/")
    for index, part in enumerate(entry_parts):
        if part in ("board", "school-board", "school_board"):
            prefix = "/" + "/".join(entry_parts[:index + 1])
            if (not path.startswith(prefix) and path.count("/") > 1
                    and not path.startswith(("/fs/pages/", "/documents/"))):
                return False
            break
    if _BOARD.search(words) and (_TOPIC.search(words) or re.search(r"archives?|school.board/?$|board.of.(?:education|trustees)/?$", words)):
        return True
    current_path = urlparse(current).path.rstrip("/")
    if current_path and path.startswith(current_path.lower() + "/"):
        return bool(re.search(r"20\d\d|archive|folder|documents|minute|agenda", words, re.I))
    # Root-level year/archive routes used by SmartSites and Duda.
    if _TOPIC.search(words) and (_BOARD.search(context + " " + entry) or _TOPIC.search(entry)):
        return True
    if (re.fullmatch(r"\s*(?:(?:19|20)\d\d(?:\s*[-–]\s*(?:(?:19|20)?\d\d))?(?:\s+School Year)?|next|older|previous|page\s*\d+)\s*", label, re.I)
            and (_BOARD.search(context) or _TOPIC.search(context) or re.search(r"/meetings(?:[/?]|$)", current))):
        return True
    same_page = urlparse(url)._replace(query="", fragment="") == urlparse(current)._replace(query="", fragment="")
    params = parse_qs(urlparse(url).query)
    return same_page and any(key in ("page", "page_no", "p", "offset", "start")
                             and value[0].isdigit() for key, value in params.items())


def _archive_folder(label: str, context: str) -> bool:
    if _EXCLUDE.search(label):
        return False
    if _BOARD.search(label) or _TOPIC.search(label):
        return True
    return bool((_BOARD.search(context) or _TOPIC.search(context)) and (
        re.fullmatch(r"20\d{2}(?:\s*[-–]\s*(?:20)?\d{2})?(?: School Year)?", label.strip(), re.I)
        or re.fullmatch(rf"{_MONTH}(?:\s+20\d{{2}})?", label.strip(), re.I)))


def discover(district: dict, entry_url: str, fetcher) -> list[dict]:
    stats = dict(pages=0, cap_hit=False, browser_pages=0, browser_sources=[], failed_pages=[], warnings=[])
    DISCOVERY_STATS[district["tea_id"] + "|" + entry_url] = stats
    entry_url = entry_url.rstrip("\\")
    start = _drive_folder(entry_url) or entry_url
    queue = deque([(start, "Board meeting materials" if _drive_folder(start) else "")])
    queued, seen, records = {start}, set(), {}

    def enqueue(url, context):
        url = _drive_folder(url) or urldefrag(url)[0]
        if url and url not in queued and _allowed(url, entry_url):
            queued.add(url)
            queue.append((url, context))

    def collect(url, label, context, listing):
        url = _drive_file(url)
        if not _allowed(url, entry_url) or not (_board_document(label, context) or _board_document(label + " " + unquote(urlparse(url).path.split("/")[-1]), context)):
            return
        filename = unquote(urlparse(url).path.split("/")[-1])
        stem = re.sub(r"\.[a-z0-9]+$", "", filename, flags=re.I)
        if re.fullmatch(r"[0-9a-f_-]{20,}", stem, re.I):
            filename = ""  # CMS resource identifiers are not date-bearing names.
        detail = classify_detail(label, filename + " | " + context)
        parsed, raw = date_for(label, url, context)
        row = make_record(district, PLATFORM, source_link=url, listing_url=listing,
                          listing_row_text=(label + " | " + context)[:1000], detail=detail,
                          detail_raw=label, date=parsed, date_raw=raw,
                          meeting_type=_meeting_type(label + " " + filename, context),
                          meeting_title=label or filename)
        records.setdefault(row["record_id"], row)

    while queue and stats["pages"] < MAX_LISTING_PAGES:
        url, inherited = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        stats["pages"] += 1
        try:
            response, final_url, html = _read(fetcher, url, stats)
            seen.add(final_url)
        except Exception as exc:
            stats["failed_pages"].append(dict(url=url, error=str(exc)[:250]))
            if stats["pages"] == 1:
                raise
            continue
        if _DOC.search(final_url) or (response and response.content.startswith((b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0"))):
            collect(final_url, inherited, inherited, url)
            continue
        soup = BeautifulSoup(html, "lxml")
        # Some CMSs hydrate their document blocks only after page load.
        # Embedded bootstrap/JSON is handled below without opening a browser.
        if (not _drive_folder(final_url) and not html.lstrip().startswith(("{", "["))
                and final_url not in stats["browser_sources"] and soup.select_one("script[src]")
                and not soup.select_one("script#__NUXT_DATA__") and "clientWorkStateTemp" not in html and "self.__next_f.push" not in html
                and not any(_document_url(urljoin(final_url, a["href"]))
                            and (_board_document(a.get_text(" ", strip=True), _link_context(a, _page_context(soup, inherited)))
                                 or _board_document(a.get_text(" ", strip=True) + " " + unquote(urlparse(a["href"]).path.split("/")[-1]),
                                                    _link_context(a, _page_context(soup, inherited))))
                            for a in soup.select("a[href]"))
                and not any(_drive_folder(urljoin(final_url, a.get("href") or a.get("src")))
                            for a in soup.select("a[href], iframe[src]"))):
            try:
                final_url, html = _render(fetcher, final_url, stats)
            except Exception as exc:
                stats["failed_pages"].append(dict(url=url, error="browser render: " + str(exc)[:220]))
                if stats["pages"] == 1:
                    raise
                continue
            soup = BeautifulSoup(html, "lxml")
        context = _page_context(soup, inherited)
        for payload in _flight_payloads(soup):
            for link, label, local in _json_links(payload, final_url, context):
                if _drive_folder(link) and not _EXCLUDE.search(label) and (_BOARD.search(local) or _TOPIC.search(local)):
                    enqueue(link, label + " | " + local)
                elif _document_url(link):
                    collect(link, label, local, final_url)
                elif not _EXCLUDE.search(label) and _listing_link(link, label, local, entry_url, final_url):
                    enqueue(link, label + " | " + local)
        # SchoolMessenger has no navigable folder hrefs. Its rendered "Copy
        # Link" controls expose stable URLs for the same folder and file tree.
        for item in soup.select(".documentList a.copyLink[data-clipboard-text]"):
            link = urljoin(final_url, item["data-clipboard-text"])
            row = next((parent for parent in item.parents if parent.name == "li"
                        and set(parent.get("class", [])) & {"folder", "file", "link", "dataRow"}), None)
            title = row.select_one(".docTitle, .titleUser .item") if row else None
            label = title.get_text(" ", strip=True) if title else item.get("title", "")
            quoted = re.search(r"url\s+'(.*)'$", label)
            if quoted:
                label = quoted[1]
            local = label + " | " + context
            if item.get("data-item") == "content_folder":
                if _board_document(label, local):
                    enqueue(link, local)
            elif _allowed(link, entry_url):
                collect(link, label, local, final_url)
        if soup.select_one(".documentList") and re.search(r"PAGE\s+\d+\s+OF\s+(?:[2-9]|\d{2,})", soup.get_text(" ", strip=True), re.I):
            stats["warnings"].append("SchoolMessenger client-side pagination may be incomplete: " + final_url)
        for a in soup.select("a[href], iframe[src], embed[src], object[data]"):
            href = a.get("href") or a.get("src") or a.get("data") or ""
            if href.startswith(("#", "javascript:", "mailto:", "tel:")):
                continue
            link = urldefrag(urljoin(final_url, href))[0]
            if not _allowed(link, entry_url):
                continue
            label = a.get_text(" ", strip=True) or a.get("data-resource-title") or a.get("title") or a.get("aria-label") or ""
            local = _link_context(a, context)
            html_document = (re.search(r"\.html?$", urlparse(link).path, re.I)
                             and _TOPIC.search(label + " " + unquote(urlparse(link).path))
                             and parse_date(unquote(urlparse(link).path.split("/")[-1]))[0])
            folder = _drive_folder(link)
            if folder:
                if not _EXCLUDE.search(label) and (_drive_folder(final_url) or _BOARD.search(context) or _TOPIC.search(local)):
                    enqueue(folder, label + " | " + local)
            elif _document_url(link) or html_document:
                collect(link, label, local, final_url)
            elif _listing_link(link, label, local, entry_url, final_url):
                # Date-specific HTML minutes are records; archives remain listings.
                if parse_date(label)[0] and _TOPIC.search(label) and not re.search(r"archive|folder", label, re.I):
                    collect(link, label, local, final_url)
                else:
                    enqueue(link, label + " | " + local)
        for script in soup.select('script'):
            try:
                text = script.get_text()
                bootstrap = re.search(r'window\.clientWorkStateTemp\s*=\s*JSON\.parse\(("(?:\\.|[^"\\])*")\)', text)
                if bootstrap:
                    payload = json.loads(json.loads(bootstrap[1])).get("page", {})
                elif script.get("type") == "application/json" or script.get("id") == "__NUXT_DATA__":
                    payload = json.loads(text)
                else:
                    continue
                if script.get("id") == "__NUXT_DATA__" and isinstance(payload, list):
                    payload = _nuxt_data(payload)
                for link, label, local in _json_links(payload, final_url, context):
                    if _drive_folder(link) and not _EXCLUDE.search(label) and (_BOARD.search(local) or _TOPIC.search(local)):
                        enqueue(link, label + " | " + local)
                    elif _document_url(link):
                        collect(link, label, local, final_url)
                    elif not _EXCLUDE.search(label) and _listing_link(link, label, local, entry_url, final_url):
                        enqueue(link, label + " | " + local)
            except (ValueError, TypeError, RecursionError) as exc:
                stats["failed_pages"].append(dict(url=final_url, error="embedded JSON: " + str(exc)[:150]))
        # Apptegy's bootstrap advertises its public JSON API. Reuse it for
        # folder pagination, instead of rendering each tree in the browser.
        for endpoint in set(re.findall(r'https://[^"\s<>]*thrillshare\.com/api/[^"\s<>]*?/documents', html)):
            params = dict(locale="en", not_reset_video_dimensions="true", page_size=40)
            folder = re.search(r"/documents/(?:.*/)?(\d+)(?:[/?]|$)", final_url)
            if folder:
                params["folder_id"] = folder[1]
                enqueue(endpoint + "?" + urlencode(params), context)
        # Finalsite resource collections are AJAX fragments, not navigable hrefs.
        for a in soup.select("a[data-resource-collection-id]"):
            section = a.find_parent(attrs={"data-source-element-id": True})
            if not section:
                continue
            element, page = section.get("data-source-element-id"), section.get("data-source-page-id")
            heading = section.select_one(".fsElementTitle")
            local = (heading.get_text(" ", strip=True) if heading else inherited) + " | " + a.get_text(" ", strip=True)
            query = urlencode(dict(depth=2, display_in_element="true", element_id=element,
                                   page_id=page, resource_collection=a["data-resource-collection-id"]))
            enqueue(urljoin(final_url, "/fs/elements/" + element) + "?" + query, local)
        for a in soup.select(".fsElementPagination a[data-page]"):
            section = a.find_parent(attrs={"data-source-element-id": True})
            if section:
                element = section["data-source-element-id"]
                params = dict(element_id=element, page_id=section.get("data-source-page-id"),
                              resource_collection=section.get("data-resource-collection-id"))
                params["element_" + element + "_page"] = a["data-page"]
                enqueue(urljoin(final_url, "/fs/elements/" + element) + "?" + urlencode({k: v for k, v in params.items() if v is not None}), inherited)
        # Plain JSON listing endpoints are also accepted as crawl entry points.
        if html.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(html)
                meta = payload.get("meta", {}) if isinstance(payload, dict) else {}
                local_context = " | ".join([str(meta.get("current_folder") or ""), inherited])
                for link, label, local in _json_links(payload, entry_url, local_context):
                    if _document_url(link):
                        collect(link, label, local, final_url)
                    elif "/documents/" in link and _archive_folder(label, local):
                        if (urlparse(final_url).hostname or "").endswith("thrillshare.com"):
                            folder = re.search(r"/(\d+)/?$", urlparse(link).path)
                            if folder:
                                params = {key: value[0] for key, value in parse_qs(urlparse(final_url).query).items()}
                                params["folder_id"] = folder[1]
                                params.pop("page_no", None)
                                link = final_url.split("?", 1)[0] + "?" + urlencode(params)
                        enqueue(link, local + " | " + label)
                following = meta.get("links", {}).get("next")
                if following:
                    enqueue(urljoin(final_url, following), inherited)
            except ValueError:
                pass
    stats["cap_hit"] = bool(queue)
    stats["records"] = len(records)
    _LOG.warning("website discovery %s %s", district["tea_id"], json.dumps(stats))
    return list(records.values())


def _export_url(url: str) -> str:
    match = re.search(r"docs\.google\.com/document/d/([^/]+)", url)
    if match:
        return "https://docs.google.com/document/d/" + match[1] + "/export?format=pdf"
    match = re.search(r"drive\.google\.com/file/d/([^/]+)", url)
    file_id = match[1] if match else parse_qs(urlparse(url).query).get("id", [None])[0]
    if file_id and urlparse(url).hostname == "drive.google.com":
        params = dict(id=file_id, export="download")
        resource_key = parse_qs(urlparse(url).query).get("resourcekey")
        if resource_key:
            params["resourcekey"] = resource_key[0]
        return "https://drive.usercontent.google.com/download?" + urlencode(params)
    return url


def resolve(record: dict, fetcher) -> dict:
    url = _export_url(record["source_link"])
    browser_stats = dict(browser_pages=0, browser_sources=[])
    if _paperless_agenda(url):
        # This public portal initializes an anonymous guest at its meeting
        # index. Direct agenda requests without that cookie return HTTP 500.
        index = urljoin(url, "meetings.php")
        session = fetcher._session() if hasattr(fetcher, "_session") else None
        primed = getattr(session, "_website_public_roots", set())
        if index not in primed:
            fetcher.get(index, refresh=True, timeout=35)
            if session is not None:
                primed.add(index)
                session._website_public_roots = primed
    for _ in range(4):
        # Legacy district file servers often publish http links on https pages.
        # Prefer TLS without changing the source's original URL in the record.
        if url.startswith("http://"):
            url = "https://" + url[7:]
        try:
            response = fetcher.get(url)
        except SSLError as exc:
            # This published bucket contains an underscore, which is not a
            # valid TLS hostname. S3's path-style endpoint keeps TLS verified.
            failed = urlparse(exc.request.url) if exc.request is not None else None
            if not failed or failed.hostname != "granicus_production_attachments.s3.amazonaws.com":
                raise
            url = "https://s3.amazonaws.com/granicus_production_attachments" + failed.path
            if failed.query:
                url += "?" + failed.query
            continue
        except RetryError:
            # Native Google documents can be shared through /file/d aliases;
            # the binary download endpoint returns 5xx instead of exporting.
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            file_id = params.get("id", [""])[0]
            if parsed.hostname != "drive.usercontent.google.com" or not re.fullmatch(r"[\w-]+", file_id):
                raise
            query = {"format": "pdf"}
            if params.get("resourcekey"):
                query["resourcekey"] = params["resourcekey"][0]
            url = "https://docs.google.com/document/d/" + file_id + "/export?" + urlencode(query)
            continue
        except HTTPError as exc:
            if exc.response is None or exc.response.status_code != 403 or url in browser_stats["browser_sources"]:
                raise
            _render(fetcher, url, browser_stats)
            fetcher.get(url, refresh=True)
            continue
        content_type = response.headers.get("Content-Type", response.headers.get("content-type", ""))
        disposition = response.headers.get("Content-Disposition", response.headers.get("content-disposition", ""))
        filename = unquote(disposition + " " + (record.get("detail_raw") or ""))
        if (content_type.split(";", 1)[0].lower() in ("", "application/octet-stream", "binary/octet-stream", "application/zip")
                and response.content.startswith(b"PK\x03\x04") and re.search(r"\.docx\b", filename, re.I)):
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        result = dict(raw_path=response.raw_path, content_type=content_type,
                      document_link=response.url, content_sha256=response.sha256, text=None)
        binary = response.content.startswith((b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0"))
        is_html = "html" in content_type.lower() or response.content.lstrip().lower().startswith((b"<!doctype html", b"<html"))
        if binary or not is_html:
            return result
        if browser.is_challenge(response.text):
            if url not in browser_stats["browser_sources"]:
                _render(fetcher, url, browser_stats)
                fetcher.get(url, refresh=True)
                continue
            raise RuntimeError("document download returned a bot challenge: " + response.url)
        soup = BeautifulSoup(response.content, "lxml")
        if urlparse(response.url).hostname in ("accounts.google.com", "drive.google.com", "docs.google.com"):
            raise RuntimeError("Google file export returned HTML (access restricted or download unavailable)")
        # Resource viewers and embedded PDFs sometimes return an HTML wrapper.
        targets = soup.select("iframe[src], embed[src], object[data], a[download], a.fsResourceLink")
        target = next((urljoin(response.url, t.get("src") or t.get("data") or t.get("href"))
                       for t in targets if t.get("src") or t.get("data") or t.get("href")), None)
        if target and target != url and _allowed(target, record["listing_url"]):
            url = _export_url(target)
            continue
        for tag in soup.select("script, style, nav, header, footer, aside, .fsNavigation, .fsBreadcrumb"):
            tag.decompose()
        main = (soup.select_one("#agenda") if _paperless_agenda(record["source_link"]) else None)
        if _paperless_agenda(record["source_link"]) and main is None:
            raise RuntimeError("public Paperless agenda was not returned: " + response.url)
        novus = _novus_agenda(response.url)
        if novus:
            main = soup.select_one("form#Form1")
            messages = soup.select("#lblMVMessage, #labelMessage")
            if main is None or any(m.get_text(strip=True) for m in messages) or not _BOARD.search(main.get_text(" ", strip=True)):
                raise RuntimeError("public Novus agenda was not returned: " + response.url)
            attachments = []
            for link in main.select('a[href*="CoverSheet.aspx"]'):
                target = urljoin(response.url, link["href"])
                if urlparse(target).scheme in ("http", "https") and _district_host(target) == _district_host(response.url):
                    attachments.append(target)
            result["attachment_links"] = list(dict.fromkeys(attachments))
        main = main or soup.select_one("main, article, #content, #fsPageContent, .content") or soup.body or soup
        text = clean_text(main.get_text("\n", strip=True))
        if _document_url(record["source_link"]) and not (_paperless_agenda(record["source_link"]) or novus):
            raise RuntimeError("document URL returned an HTML page instead of file bytes: " + response.url)
        return {**result, "text": text or None, "text_method": "html", "text_status": "ok" if text else "empty"}
    raise RuntimeError("document viewer redirect limit exceeded")
