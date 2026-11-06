"""Research views: server-side aggregation over the measures and records files.

Reads, all optional except districts.csv:

* data/measures/meetings.parquet and passages.parquet (contract in
  code/measures/SCHEMA.md). Until meetings.parquet exists the app runs in
  "measures not computed yet" mode: denominators, coverage and word
  comparisons still work from records.parquet, and every measure is null.
* data/records.parquet (document metadata and text) and data/coverage.parquet.
* code/measures/definitions.yaml, gold/calibration.json.
* code/app/reference/district_points.csv (TEA office points, NCES enrollment)
  and code/app/event_places.json.

Small tables are rebuilt in a fresh in-memory DuckDB connection whenever an
input file changes (checked at most every CHECK_SECONDS), then swapped in, so
new measures files are picked up without a restart. Results are cached per
(snapshot version, endpoint, parameters).
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict
from datetime import date, timedelta
from pathlib import Path

import duckdb
import yaml

APP = Path(__file__).resolve().parent
ROOT = APP.parents[1]
DEFINITIONS = ROOT / "code" / "measures" / "definitions.yaml"
POINTS = APP / "reference" / "district_points.csv"
EVENT_PLACES = APP / "event_places.json"
CHECK_SECONDS = 15
MONTH_DAYS = 30.4375
THIN_MEETINGS = 30          # brief 9.4: dashed below this many meetings per period
MIN_SCREENED = 0.95        # shares are shown only once this share of readable meetings is screened
WORDS_VERSION = 3          # bump when the word comparison changes, to skip stale disk caches
MIN_EVENT_DISTRICTS = 5     # dumbbell rows below this are marked thin
FIRST_YEAR = 2003
READABLE = ("ok", "partial_ocr")
SCAN = ("needs_ocr",)
SIZE_BANDS = [(0, 500, "Under 500 students"), (500, 2000, "500 to 1,999"), (2000, 10000, "2,000 to 9,999"),
              (10000, 50000, "10,000 to 49,999"), (50000, 10**9, "50,000 or more")]
MEASURES = ("safety", "temperament", "event_reference", "closed_session")
WORD_RE = r"[a-z][a-z]+(?:['\-][a-z]+)*"
WORD_RE_CASED = r"[A-Za-z][A-Za-z]+(?:['\-][A-Za-z]+)*"
STOPWORDS = set("""
a about above after again against all also am an and any are aren't as at be because been before being below
between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each few for
from further had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself him himself
his how how's i i'd i'll i'm i've if in into is isn't it it's its itself let's me more most mustn't my myself no nor
not of off on once only or other ought our ours ourselves out over own same shan't she she'd she'll she's should
shouldn't so some such than that that's the their theirs them themselves then there there's these they they'd
they'll they're they've this those through to too under until up very was wasn't we we'd we'll we're we've were
weren't what what's when when's where where's which while who who's whom why why's with won't would wouldn't you
you'd you'll you're you've your yours yourself yourselves shall may might must will upon per via within without
january february march april may june july august september october november december jan feb mar apr jun jul aug
sep sept oct nov dec monday tuesday wednesday thursday friday saturday sunday mon tue tues wed thu thurs fri sat sun
one two three four five six seven eight nine ten am pm
""".split())


class NotFound(Exception):
    pass


class BadRequest(Exception):
    pass


def _q(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def wilson(k: int, n: int, z: float = 1.96):
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (c - r) / d), min(1.0, (c + r) / d)


def miles(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def wls(points):
    """Weighted least squares y = a + b x over (x, y, w); None when under 3 points."""
    pts = [(x, y, w) for x, y, w in points if y is not None and w]
    if len(pts) < 3:
        return None
    sw = sum(w for _, _, w in pts)
    mx = sum(w * x for x, _, w in pts) / sw
    my = sum(w * y for _, y, w in pts) / sw
    sxx = sum(w * (x - mx) ** 2 for x, _, w in pts)
    if not sxx:
        return None
    b = sum(w * (x - mx) * (y - my) for x, y, w in pts) / sxx
    return my - b * mx, b


def log_odds(after: dict, before: dict, n_after: int, n_before: int):
    """Weighted log-odds ratio with an informative Dirichlet prior (Monroe, Colaresi and
    Quinn 2008, eq. 16-22). The prior is the pooled before+after counts (empirical Bayes,
    as in the R package tidylo). Returns {word: (delta, z)}; z > 0 means more often after."""
    pooled = {w: after.get(w, 0) + before.get(w, 0) for w in set(after) | set(before)}
    a0 = n_after + n_before
    out = {}
    for w, aw in pooled.items():
        ya, yb = after.get(w, 0), before.get(w, 0)
        la = math.log((ya + aw) / (n_after + a0 - ya - aw))
        lb = math.log((yb + aw) / (n_before + a0 - yb - aw))
        delta = la - lb
        out[w] = (delta, delta / math.sqrt(1 / (ya + aw) + 1 / (yb + aw)))
    return out


class Research:
    def __init__(self, data_dir: Path, store=None, memory_limit: str = "320MB", threads: int = 2):
        self.data = Path(data_dir)
        self.store = store
        self.memory_limit = memory_limit
        self.threads = threads
        self.files = {
            "meetings": self.data / "measures" / "meetings.parquet",
            "passages": self.data / "measures" / "passages.parquet",
            "records": self.data / "records.parquet",
            "coverage": self.data / "coverage.parquet",
            "districts": self.data / "reference" / "districts.csv",
            "definitions": DEFINITIONS,
            "calibration": self._calibration_path(),
        }
        self.cache_dir = self.data / "app" / "cache"
        self._lock = threading.Lock()
        self._cache: OrderedDict = OrderedDict()
        self._cache_lock = threading.Lock()
        self._sig = None
        self._checked = 0.0
        self.version = 0
        self.con = None
        self.info: dict = {}
        self.defs: dict = {}
        self.term_words: set = set()
        self._old: list = []
        self.events: dict = {}

    # ------------------------------------------------------------ snapshot
    def _calibration_path(self) -> Path:
        for p in (self.data / "measures" / "gold" / "calibration.json", self.data / "gold" / "calibration.json",
                  ROOT / "gold" / "calibration.json"):
            if p.exists():
                return p
        return self.data / "gold" / "calibration.json"

    def _signature(self):
        self.files["calibration"] = self._calibration_path()
        sig = []
        for key, path in sorted(self.files.items()):
            try:
                st = path.stat()
                sig.append((key, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((key, None, None))
        return tuple(sig)

    def ensure(self) -> None:
        if self.con is not None and time.monotonic() - self._checked < CHECK_SECONDS:
            return
        with self._lock:
            if self.con is not None and time.monotonic() - self._checked < CHECK_SECONDS:
                return
            sig = self._signature()
            if sig != self._sig:
                self._build()
                self._sig = sig
                self.version += 1
                with self._cache_lock:
                    self._cache.clear()
            self._checked = time.monotonic()

    def cursor(self):
        self.ensure()
        return self.con.cursor()

    def cached(self, key, fn):
        k = (self.version, key)
        with self._cache_lock:
            if k in self._cache:
                self._cache.move_to_end(k)
                return self._cache[k]
        value = fn()
        with self._cache_lock:
            self._cache[k] = value
            while len(self._cache) > 300:
                self._cache.popitem(last=False)
        return value

    def _connect(self):
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(config={"threads": self.threads, "memory_limit": self.memory_limit,
                                     "temp_directory": str(self.cache_dir / "duckdb_tmp"),
                                     "preserve_insertion_order": False})
        return con

    def _build(self) -> None:
        con = self._connect()
        defs = self._definitions()
        self.defs = defs
        self.term_words = term_words(defs)
        districts = self._csv(self.files["districts"])
        points = {r["tea_id"]: r for r in self._csv(POINTS)}
        rows = []
        for d in districts:
            p = points.get(d["tea_id"], {})
            enrollment = int(p["enrollment"]) if p.get("enrollment") else None
            band = next((label for lo, hi, label in SIZE_BANDS if enrollment is not None and lo <= enrollment < hi), None)
            rows.append((d["tea_id"], d.get("school_district"), d.get("county_name"), d.get("county_fips"),
                         float(p["lat"]) if p.get("lat") else None, float(p["lon"]) if p.get("lon") else None,
                         p.get("city") or None, enrollment, band, p.get("nces_id") or None))
        con.execute("""create table dist (tea_id varchar, school_district varchar, county_name varchar,
                       county_fips varchar, lat double, lon double, city varchar, enrollment integer,
                       size_band varchar, nces_id varchar, esc_region varchar)""")
        if rows:
            con.executemany("insert into dist values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, null)", rows)

        has_records = self.files["records"].exists()
        if has_records:
            con.execute(f"""create table docs as select tea_id, date, year(date)::int as year, detail,
                case when text_status in {READABLE} then 'readable' when text_status in {SCAN} then 'scan'
                     when text_status = 'recording' then 'recording' else 'unavailable' end as state,
                coalesce(preferred_for_meeting, false) as preferred
                from read_parquet({_q(self.files['records'])})""")
        else:
            con.execute("create table docs (tea_id varchar, date date, year int, detail varchar, state varchar, preferred boolean)")

        measures = self.files["meetings"].exists()
        if measures:
            con.execute(f"""create table meet as select meeting_id, tea_id, date, year(date)::int as year,
                coalesce(readable, false) as readable, safety, coalesce(safety_subtopics, []) as subs,
                temperament, temperament_tone as tone, temperament_who as who, event_reference,
                coalesce(event_keys, []) as event_keys, coalesce(closed_security_session, false) as closed,
                best_detail, meeting_type, meeting_title, best_record_id, esc_region, definition_version
                from read_parquet({_q(self.files['meetings'])})""")
            con.execute("""update dist set esc_region = r.esc_region from (select tea_id, max(esc_region) esc_region
                           from meet where esc_region is not null group by 1) r where dist.tea_id = r.tea_id""")
            versions = [r[0] for r in con.execute("select distinct definition_version from meet").fetchall()]
        elif has_records:
            # One row per meeting from the preferred document; measures are null.
            con.execute(f"""create table meet as select record_id as meeting_id, tea_id, date, year(date)::int as year,
                text_status in {READABLE} as readable, null::varchar as safety, []::varchar[] as subs,
                null::varchar as temperament, null::varchar as tone, null::varchar as who,
                null::varchar as event_reference, []::varchar[] as event_keys, false as closed,
                detail as best_detail, meeting_type, meeting_title, record_id as best_record_id,
                null::varchar as esc_region, null::varchar as definition_version
                from read_parquet({_q(self.files['records'])}) where preferred_for_meeting""")
            versions = []
        else:
            con.execute("""create table meet (meeting_id varchar, tea_id varchar, date date, year int, readable boolean,
                safety varchar, subs varchar[], temperament varchar, tone varchar, who varchar, event_reference varchar,
                event_keys varchar[], closed boolean, best_detail varchar, meeting_type varchar, meeting_title varchar,
                best_record_id varchar, esc_region varchar, definition_version varchar)""")
            versions = []
        con.execute("delete from meet where date is null")

        # Events, with distance from every district office.
        places = json.loads(EVENT_PLACES.read_text()) if EVENT_PLACES.exists() else {}
        events = []
        for key, e in (defs.get("events", {}).get("names") or {}).items():
            p = places.get(key, {})
            events.append({"key": key, "label": e.get("label", key), "date": str(e.get("date")),
                           "lat": p.get("lat"), "lon": p.get("lon"), "county": p.get("county"),
                           "place": p.get("place"), "kind": p.get("kind"), "deaths": p.get("deaths"),
                           "in_texas": p.get("lat") is not None})
        events.sort(key=lambda e: e["date"])
        self.events = {e["key"]: e for e in events}
        dist_rows = con.execute("select tea_id, lat, lon, county_name from dist").fetchall()
        ed = []
        for e in events:
            for tea_id, lat, lon, county in dist_rows:
                m = miles(e["lat"], e["lon"], lat, lon) if e["in_texas"] and lat is not None else None
                ed.append((e["key"], tea_id, m, bool(e["county"]) and county == e["county"]))
        con.execute("create table ev_dist (event varchar, tea_id varchar, miles double, same_county boolean)")
        if ed:
            con.executemany("insert into ev_dist values (?, ?, ?, ?)", ed)

        # A measures file can exist mid-run. Unscreened meetings are the ones with candidate
        # passages, so shares computed before screening finishes are biased toward zero.
        screening = None
        status = "absent"
        if measures:
            readable, screened = con.execute("""select count(*) filter (where readable),
                count(*) filter (where readable and safety in ('yes', 'no', 'uncertain')) from meet""").fetchone()
            share = screened / readable if readable else 0.0
            screening = {"readable": readable, "screened": screened, "share": share, "required": MIN_SCREENED}
            status = "ready" if share >= MIN_SCREENED else "partial"
        synthetic = (self.data / "SYNTHETIC_FIXTURE").exists() or any(
            (v or "").startswith("synthetic") for v in versions)
        mtimes = [p.stat().st_mtime for k, p in self.files.items() if k in ("meetings", "records") and p.exists()]
        self.info = {
            "measures": status == "ready",
            "measures_file": measures,
            "measures_status": status,
            "screening": screening,
            "passages": self.files["passages"].exists(),
            "records": has_records,
            "synthetic": synthetic,
            "definition_versions": versions,
            "definitions_version": str(defs.get("version", "")),
            "data_as_of": date.fromtimestamp(max(mtimes)).isoformat() if mtimes else None,
            "measures_as_of": date.fromtimestamp(self.files["meetings"].stat().st_mtime).isoformat() if measures else None,
            "min_screened": MIN_SCREENED,
            "calibrated": self.files["calibration"].exists(),
        }
        old, self.con = self.con, con
        if old is not None:
            # Requests may still be reading the old snapshot; close it a minute later.
            self._old.append(old)
            timer = threading.Timer(60, self._close_old, args=(old,))
            timer.daemon = True
            timer.start()

    @staticmethod
    def _csv(path: Path) -> list[dict]:
        if not path.exists():
            return []
        with open(path, newline="") as handle:
            return list(csv.DictReader(handle))

    def _definitions(self) -> dict:
        try:
            defs = yaml.safe_load(DEFINITIONS.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return {}
        for zone in (defs.get("thresholds") or {}).values():
            if isinstance(zone, dict):
                for k in list(zone):
                    if k is True or k is False:
                        zone["yes" if k else "no"] = zone.pop(k)
        return defs

    def _close_old(self, con):
        if con in self._old:
            self._old.remove(con)
            con.close()

    def close(self):
        for con in self._old + ([self.con] if self.con is not None else []):
            con.close()
        self._old = []
        self.con = None

    # ------------------------------------------------------------ parameters
    def subtopics(self) -> dict:
        subs = (self.defs.get("safety") or {}).get("subtopics") or {}
        return {k: v.get("label", k) for k, v in subs.items()}

    def _measure(self, params: dict):
        """SQL fragments for the chosen measure: (screened, hit, uncertain, label)."""
        measure = params.get("measure") or "safety"
        if measure not in MEASURES:
            raise BadRequest(f"measure must be one of {', '.join(MEASURES)}")
        if not self.info.get("measures"):
            # Denominators only: every readable meeting counts, no measure values exist yet.
            return "readable", "false", "false", "measures not computed yet"
        if measure == "closed_session":
            return "readable", "closed", "false", "closed security session cited"
        col = measure
        screened = f"readable and {col} in ('yes', 'no', 'uncertain')"
        hit = f"{col} = 'yes'"
        label = {"safety": "any safety subtopic", "temperament": "temperament (how students or staff feel or behave)",
                 "event_reference": "a reference to a named shooting"}[measure]
        sub = params.get("subtopic") or ""
        if measure == "safety" and sub and sub != "any":
            if sub not in self.subtopics():
                raise BadRequest("unknown subtopic")
            hit += f" and list_contains(subs, '{sub}')"
            label = self.subtopics()[sub]
        tone = params.get("tone") or ""
        if measure == "temperament" and tone:
            if tone not in ("negative", "positive", "mixed"):
                raise BadRequest("tone must be negative, positive or mixed")
            hit += f" and tone = '{tone}'"
        return screened, hit, f"{col} = 'uncertain'", label

    def _share(self, k: int, n: int):
        """(share, lo, hi), or Nones while the measures are not computed."""
        if not self.info.get("measures") or not n:
            return None, None, None
        return (k / n, *wilson(k, n))

    def _where(self, params: dict, alias: str = "m") -> tuple[str, list]:
        sql, args = [], []
        if params.get("region"):
            sql.append(f"{alias}.tea_id in (select tea_id from dist where esc_region = ?)")
            args.append(params["region"])
        if params.get("size"):
            sql.append(f"{alias}.tea_id in (select tea_id from dist where size_band = ?)")
            args.append(params["size"])
        if params.get("tea_id"):
            sql.append(f"{alias}.tea_id = ?")
            args.append(params["tea_id"])
        y0, y1 = self._years(params)
        sql.append(f"{alias}.year between ? and ?")
        args += [y0, y1]
        return " and ".join(sql), args

    @staticmethod
    def _int(params, key, default, lo, hi):
        try:
            v = int(float(params.get(key, default)))
        except (TypeError, ValueError):
            raise BadRequest(f"{key} must be a number")
        return max(lo, min(hi, v))

    def _years(self, params):
        y0 = self._int(params, "from", FIRST_YEAR, 1990, 2100)
        y1 = self._int(params, "to", date.today().year, 1990, 2100)
        return min(y0, y1), max(y0, y1)

    def _event(self, key: str) -> dict:
        self.ensure()
        if key not in self.events:
            raise NotFound("unknown event")
        return self.events[key]

    # ------------------------------------------------------------ meta
    def meta(self) -> dict:
        self.ensure()

        def build():
            cur = self.con.cursor()
            chip = self._chip(cur, {})
            regions = [r[0] for r in cur.execute(
                "select distinct esc_region from dist where esc_region is not null order by try_cast(esc_region as int), 1").fetchall()]
            return {**self.info, "events": list(self.events.values()), "subtopics": self.subtopics(),
                    "regions": regions, "size_bands": [b[2] for b in SIZE_BANDS], "chip": chip,
                    "thin_meetings": THIN_MEETINGS, "first_year": FIRST_YEAR, "last_year": date.today().year}
        return self.cached(("meta",), build)

    def _chip(self, cur, params) -> dict:
        where, args = self._where(params)
        screened, _, _, _ = self._measure({"measure": "safety"})
        r = cur.execute(f"""select count(*) filter (where {screened}), count(distinct tea_id) filter (where {screened}),
                            count(*) filter (where readable), count(*) from meet m where {where}""", args).fetchone()
        d = cur.execute("""select count(*) filter (where detail = 'Minutes' and state = 'scan'),
                           count(*) filter (where detail = 'Minutes'), count(*) filter (where state = 'scan'), count(*)
                           from docs""").fetchone()
        return {"meetings": r[0], "districts": r[1], "readable_meetings": r[2], "all_meetings": r[3],
                "not_screened": r[2] - r[0] if self.info["measures"] else None,
                "minutes_unread": d[0], "minutes_total": d[1], "minutes_unread_share": d[0] / d[1] if d[1] else None,
                "scans_unread": d[2], "documents": d[3]}

    # ------------------------------------------------------------ trend
    def trend(self, params: dict) -> dict:
        self.ensure()
        grain = params.get("grain") or "month"
        if grain not in ("month", "quarter", "year"):
            raise BadRequest("grain must be month, quarter or year")
        sample = params.get("sample") or "all"
        if sample not in ("all", "stable"):
            raise BadRequest("sample must be all or stable")
        screened, hit, unc, label = self._measure(params)
        where, args = self._where(params)
        key = ("trend", grain, sample, screened, hit, where, tuple(args))

        def build():
            cur = self.con.cursor()
            y0, y1 = self._years(params)
            stable = ""
            sargs = []
            if sample == "stable":
                stable = f"""and tea_id in (select tea_id from meet m where {where} and {screened}
                             group by 1 having count(distinct year) = ?)"""
                sargs = args + [y1 - y0 + 1]
            rows = cur.execute(f"""select date_trunc('{grain}', date)::date as period, count(*) n,
                    count(*) filter (where {hit}) k, count(*) filter (where {unc}) u, count(distinct tea_id) d
                    from meet m where {where} and {screened} {stable} group by 1 order by 1""", args + sargs).fetchall()
            out = []
            for period, n, k, u, d in rows:
                share, lo, hi = self._share(k, n)
                out.append({"period": period.isoformat(), "meetings": n, "yes": k, "uncertain": u, "districts": d,
                            "share": share, "lo": lo, "hi": hi, "thin": n < THIN_MEETINGS})
            chip = self._chip(cur, params)
            if sample == "stable":
                chip["stable_districts"] = cur.execute(
                    f"select count(*) from (select tea_id from meet m where {where} and {screened} group by 1 having count(distinct year) = ?)",
                    sargs).fetchone()[0]
            return {"measures": self.info["measures"], "label": label, "grain": grain, "sample": sample,
                    "rows": out, "chip": chip, "events": [e for e in self.events.values() if y0 <= int(e["date"][:4]) <= y1]}
        return self.cached(key, build)

    def subtopic_trend(self, params: dict) -> dict:
        self.ensure()
        where, args = self._where(params)

        def build():
            cur = self.con.cursor()
            subs = list(self.subtopics())
            cols = ", ".join(f"count(*) filter (where safety = 'yes' and list_contains(subs, '{s}')) as \"{s}\"" for s in subs)
            rows = cur.execute(f"""select year, count(*) n, {cols} from meet m
                where {where} and readable and safety in ('yes', 'no', 'uncertain') group by 1 order by 1""", args).fetchall()
            series = {s: [] for s in subs}
            for row in rows:
                year, n = row[0], row[1]
                for i, s in enumerate(subs):
                    k = row[2 + i]
                    share, lo, hi = self._share(k, n)
                    series[s].append({"year": year, "meetings": n, "yes": k, "share": share,
                                      "lo": lo, "hi": hi, "thin": n < THIN_MEETINGS})
            return {"measures": self.info["measures"], "labels": self.subtopics(), "series": series}
        return self.cached(("subtopics", where, tuple(args)), build)

    def tone(self, params: dict) -> dict:
        self.ensure()
        where, args = self._where(params)

        def build():
            cur = self.con.cursor()
            groups = {"students": "('students', 'both')", "staff": "('staff', 'both')"}
            cols = ", ".join(f"count(*) filter (where temperament = 'yes' and tone = '{t}' and who in {w})"
                             for w in groups.values() for t in ("negative", "positive", "mixed"))
            rows = cur.execute(f"""select year, count(*), {cols} from meet m
                where {where} and readable and temperament in ('yes', 'no', 'uncertain') group by 1 order by 1""", args).fetchall()
            # Denominator: every screened meeting that year, whether or not it mentions the group.
            out = []
            for row in rows:
                for i, g in enumerate(groups):
                    neg, pos, mixed = row[2 + 3 * i: 5 + 3 * i]
                    out.append({"year": row[0], "who": g, "meetings": row[1], "negative": neg, "positive": pos, "mixed": mixed})
            return {"measures": self.info["measures"], "rows": out, "quotes": self._quotes(cur, where, args)}
        return self.cached(("tone", where, tuple(args)), build)

    def _quotes(self, cur, where, args, limit=2):
        if not self.info["measures"]:
            return []
        out = []
        for tone in ("negative", "positive"):
            rows = cur.execute(f"""select m.meeting_id from meet m where {where} and temperament = 'yes' and tone = ?
                                   order by date desc limit 40""", args + [tone]).fetchall()
            ids = [r[0] for r in rows]
            if not ids:
                continue
            ev = self._evidence(cur, ids, "temperament")
            for mid in ids:
                if ev.get(mid):
                    out.append({**ev[mid][0], "tone": tone})
                    break
        return out[:limit]

    def _evidence(self, cur, meeting_ids: list[str], measure: str | None = None) -> dict:
        if not meeting_ids or not self.info["measures"]:
            return {}
        rows = cur.execute(f"""select e.meeting_id, e.tea_id, d.school_district, e.date, e.best_detail, e.best_record_id,
                    e.temperament_tone, e.evidence
                from read_parquet({_q(self.files['meetings'])}) e left join dist d using (tea_id)
                where e.meeting_id in (select unnest(?::varchar[]))""", [meeting_ids]).fetchall()
        links = self._links(cur, [r[5] for r in rows if r[5]], [r[1] for r in rows])
        out = {}
        for mid, tea_id, name, d, detail, rid, tone, evidence in rows:
            items = [x for x in (evidence or []) if measure is None or x.get("measure") == measure]
            out[mid] = [{"meeting_id": mid, "tea_id": tea_id, "school_district": name, "date": d.isoformat() if d else None,
                         "detail": detail, "record_id": rid, "source_link": links.get(rid), "measure": x.get("measure"),
                         "excerpt": x.get("excerpt"), "tone": tone} for x in items]
        return out

    def _links(self, cur, record_ids: list[str], tea_ids: list[str] | None = None) -> dict:
        """record_id -> source_link. Passing the districts lets DuckDB skip row groups
        (records.parquet is written in district order)."""
        if not record_ids or not self.info["records"]:
            return {}
        where = "record_id in (select unnest(?::varchar[]))"
        args = [list(set(record_ids))]
        if tea_ids:
            where = "tea_id in (select unnest(?::varchar[])) and " + where
            args.insert(0, list(set(tea_ids)))
        rows = cur.execute(f"""select record_id, source_link from read_parquet({_q(self.files['records'])})
                               where {where}""", args).fetchall()
        return dict(rows)

    # ------------------------------------------------------------ coverage
    def coverage_grid(self, params: dict) -> dict:
        self.ensure()

        def build():
            cur = self.con.cursor()
            y1 = date.today().year
            rows = cur.execute(f"""select d.tea_id, d.school_district, d.county_name, d.enrollment, d.size_band, d.esc_region,
                    x.year, count(*) filter (where x.state = 'readable'), count(*) filter (where x.state = 'scan'),
                    count(*) filter (where x.state in ('unavailable'))
                from dist d left join docs x on x.tea_id = d.tea_id and x.year between {FIRST_YEAR} and {y1}
                    and x.detail in ('Minutes', 'Agenda', 'Projector')
                group by all""").fetchall()
            by = {}
            for tea_id, name, county, enr, band, region, year, r, s, u in rows:
                d = by.setdefault(tea_id, {"tea_id": tea_id, "school_district": name, "county_name": county,
                                           "enrollment": enr, "size_band": band, "esc_region": region, "cells": {}})
                if year is not None:
                    d["cells"][year] = "R" if r else "S" if s else "U" if u else ""
            years = list(range(FIRST_YEAR, y1 + 1))
            out = []
            for d in by.values():
                cells = "".join(d["cells"].get(y, "") or "." for y in years)
                first = next((y for y in years if d["cells"].get(y)), None)
                out.append({**{k: v for k, v in d.items() if k != "cells"}, "cells": cells, "first_year": first})
            out.sort(key=lambda d: (d["first_year"] or 9999, d["school_district"] or ""))
            per_year = [{"year": y, "readable": sum(1 for d in out if d["cells"][i] == "R"),
                         "scan": sum(1 for d in out if d["cells"][i] == "S"),
                         "unavailable": sum(1 for d in out if d["cells"][i] == "U")} for i, y in enumerate(years)]
            flags = {}
            for r in self._csv_parquet(cur, "select flags from read_parquet({cov})"):
                for f in (r[0] or "").split(","):
                    if f:
                        flags[f] = flags.get(f, 0) + 1
            return {"years": years, "districts": out, "per_year": per_year, "flags": flags,
                    "legend": {"R": "Readable minutes or agenda", "S": "Scans awaiting OCR",
                               "U": "Listed, but text unavailable", ".": "No record found"}}
        return self.cached(("coverage_grid",), build)

    def _csv_parquet(self, cur, sql):
        if not self.files["coverage"].exists():
            return []
        return cur.execute(sql.format(cov=_q(self.files["coverage"]))).fetchall()

    # ------------------------------------------------------------ story
    def story(self) -> dict:
        self.ensure()

        def build():
            cur = self.con.cursor()
            docs = cur.execute("""select count(*), count(distinct tea_id), count(*) filter (where state = 'readable'),
                    count(*) filter (where state = 'scan'), count(*) filter (where state = 'unavailable'),
                    min(year) filter (where year >= 1990), max(year) filter (where year <= year(current_date))
                    from docs""").fetchone()
            total_districts = cur.execute("select count(*) from dist").fetchone()[0]
            dots = cur.execute(f"""select d.tea_id, d.school_district, d.enrollment,
                    count(x.tea_id), count(*) filter (where x.state = 'readable'), count(*) filter (where x.state = 'scan'),
                    min(x.year) filter (where x.year >= {FIRST_YEAR - 13}) as first_year
                from dist d left join docs x using (tea_id) group by all order by d.tea_id""").fetchall()
            dot_rows = []
            for tea_id, name, enr, n, r, s, first in dots:
                state = "none" if not n else "readable" if r >= max(s, 1) else "scan" if s else "unavailable"
                dot_rows.append({"tea_id": tea_id, "name": name, "enrollment": enr, "state": state,
                                 "documents": n, "readable": r, "scan": s,
                                 "first_year": max(first, FIRST_YEAR) if first else None,
                                 "first_year_raw": first})
            first_hist = {}
            for d in dot_rows:
                if d["first_year"]:
                    first_hist[d["first_year"]] = first_hist.get(d["first_year"], 0) + 1
            year = self.trend({"grain": "year"})
            month = self.trend({"grain": "month", "from": 2010})
            meetings_year = cur.execute("""select year, count(*) filter (where readable), count(*) from meet
                    where year between ? and year(current_date) group by 1 order by 1""", [FIRST_YEAR]).fetchall()
            out = {
                **self.info,
                "documents": docs[0], "districts_with_records": docs[1], "districts_total": total_districts,
                "readable_docs": docs[2], "scan_docs": docs[3], "unavailable_docs": docs[4],
                "first_year": max(docs[5] or FIRST_YEAR, FIRST_YEAR), "last_year": docs[6],
                "meetings": cur.execute("select count(*) from meet").fetchone()[0],
                "dots": dot_rows, "first_year_hist": first_hist,
                "meetings_by_year": [{"year": y, "readable": r, "all": a} for y, r, a in meetings_year],
                "year_trend": year["rows"], "month_trend": month["rows"], "chip": year["chip"],
                "events": [self.events[k] for k in ("santa_fe", "uvalde") if k in self.events],
                "pretrend": None, "subtopics": None, "tone": None,
            }
            if self.info["measures"] and "uvalde" in self.events:
                out["pretrend"] = self._statewide_pretrend(month["rows"], self.events["uvalde"]["date"])
                out["subtopics"] = self.subtopic_trend({"from": 2010})
                out["tone"] = self.tone({"from": 2010})
            return out
        return self.cached(("story",), build)

    @staticmethod
    def _statewide_pretrend(rows, event_date: str, months: int = 24):
        ev = date.fromisoformat(event_date)
        pts = []
        for r in rows:
            p = date.fromisoformat(r["period"])
            rel = (p.year - ev.year) * 12 + p.month - ev.month
            if -months <= rel < 0 and r["share"] is not None:
                pts.append((rel, r["share"], r["meetings"]))
        fit = wls(pts)
        if not fit:
            return None
        a, b = fit
        after = [r for r in rows if 0 < (date.fromisoformat(r["period"]).year - ev.year) * 12
                 + date.fromisoformat(r["period"]).month - ev.month <= 12]
        actual = sum(r["yes"] for r in after) / sum(r["meetings"] for r in after) if after and sum(r["meetings"] for r in after) else None
        projected = sum(a + b * ((date.fromisoformat(r["period"]).year - ev.year) * 12 + date.fromisoformat(r["period"]).month - ev.month)
                        for r in after) / len(after) if after else None
        before = [p for p in pts if p[0] >= -12]
        before_share = sum(s * n for _, s, n in before) / sum(n for _, _, n in before) if before else None
        line = []
        for rel in range(-months, 13):
            y, m = divmod(ev.month - 1 + rel, 12)
            line.append({"period": date(ev.year + y, m + 1, 1).isoformat(), "share": max(0.0, a + b * rel), "fitted": rel < 0})
        return {"event": "uvalde", "slope_per_year": b * 12, "line": line, "after_actual": actual,
                "after_projected": max(0.0, projected) if projected is not None else None,
                "before_share": before_share, "months": months}

    # ------------------------------------------------------------ events
    def _nearby_sql(self, params: dict):
        radius = self._int(params, "radius", 50, 5, 500)
        same_county = str(params.get("same_county", "0")) in ("1", "true", "yes")
        return ("ev.same_county" if same_county else f"ev.miles <= {radius}"), radius, same_county

    def event(self, params: dict) -> dict:
        e = self._event(params.get("key") or "uvalde")
        window = self._int(params, "window", 12, 3, 36)
        sample = params.get("sample") or "balanced"
        if sample not in ("balanced", "all"):
            raise BadRequest("sample must be balanced or all")
        near, radius, same_county = self._nearby_sql(params)
        screened, hit, unc, label = self._measure(params)
        key = ("event", e["key"], window, sample, near, screened, hit)

        def build():
            cur = self.con.cursor()
            ev = date.fromisoformat(e["date"])
            span = int(window * MONTH_DAYS)
            near_expr = near if e["in_texas"] else "false"
            base = f"""with g as (select ev.tea_id, ev.miles, ({near_expr}) as nearby from ev_dist ev where ev.event = ?),
                mm as (select m.tea_id, datediff('day', ?::date, m.date) as days, ({hit}) as hit, ({unc}) as unc
                    from meet m where m.date > ?::date - {span} and m.date <= ?::date + {span} and {screened}),
                q as (select tea_id, count(*) filter (where days <= 0) nb, count(*) filter (where days > 0) na
                    from mm group by 1),
                mg as (select mm.*, g.nearby, case when days <= 0 then -1 - floor(-days / {MONTH_DAYS})
                    else floor((days - 1) / {MONTH_DAYS}) end::int as rel,
                    (q.nb > 0 and q.na > 0) as qualifies
                    from mm join g using (tea_id) join q using (tea_id))"""
            args = [e["key"], e["date"], e["date"], e["date"]]
            filt = "where qualifies" if sample == "balanced" else ""
            rows = cur.execute(base + f"""select nearby, rel, count(*), count(*) filter (where hit),
                    count(*) filter (where unc), count(distinct tea_id) from mg {filt} group by 1, 2 order by 1, 2""", args).fetchall()
            series = {"nearby": [], "comparison": []}
            for nearby, rel, n, k, u, d in rows:
                share, lo, hi = self._share(k, n)
                series["nearby" if nearby else "comparison"].append({
                    "rel": rel, "meetings": n, "yes": k, "uncertain": u, "districts": d,
                    "share": share, "lo": lo, "hi": hi, "thin": n < THIN_MEETINGS})
            summary = {}
            for grp, flag in (("nearby", True), ("comparison", False)):
                b = [r for r in series[grp] if r["rel"] < 0]
                a = [r for r in series[grp] if r["rel"] >= 0]
                nb, kb = sum(r["meetings"] for r in b), sum(r["yes"] for r in b)
                na, ka = sum(r["meetings"] for r in a), sum(r["yes"] for r in a)
                sb, sa = self._share(kb, nb), self._share(ka, na)
                summary[grp] = {"before": sb[0], "after": sa[0], "meetings_before": nb, "meetings_after": na,
                                "yes_before": kb, "yes_after": ka, "before_ci": sb[1:], "after_ci": sa[1:]}
            counts = cur.execute(base + """select g.nearby, count(*) as total,
                    count(*) filter (where q.nb > 0 and q.na > 0), count(*) filter (where coalesce(q.nb, 0) = 0),
                    count(*) filter (where coalesce(q.na, 0) = 0)
                from g left join q using (tea_id) group by 1""", args).fetchall()
            for nearby, total, qual, no_before, no_after in counts:
                summary["nearby" if nearby else "comparison"].update(
                    {"districts_total": total, "districts_qualifying": qual,
                     "districts_no_before": no_before, "districts_no_after": no_after})
            fit = self.info["measures"] and wls([(r["rel"], r["share"], r["meetings"]) for r in series["nearby" if e["in_texas"] else "comparison"]
                       if r["rel"] < 0])
            pretrend = None
            if fit:
                a, b = fit
                pretrend = [{"rel": rel, "share": max(0.0, a + b * rel), "fitted": rel < 0} for rel in range(-window, window)]
                after = summary["nearby" if e["in_texas"] else "comparison"]
                after["projected_after"] = max(0.0, sum(a + b * rel for rel in range(0, window)) / window)
            districts = cur.execute(base + f"""select g.tea_id, d.school_district, d.county_name, g.miles, coalesce(q.nb, 0),
                    coalesce(q.na, 0), q.nb > 0 and q.na > 0 from g join dist d using (tea_id) left join q using (tea_id)
                where g.nearby order by g.miles nulls last""", args).fetchall()
            return {"measures": self.info["measures"], "event": e, "window": window, "radius": radius,
                    "same_county": same_county, "sample": sample, "label": label, "series": series,
                    "summary": summary, "pretrend": pretrend,
                    "districts": [{"tea_id": t, "school_district": n, "county_name": c,
                                   "miles": round(mi, 1) if mi is not None else None, "meetings_before": b,
                                   "meetings_after": a, "qualifies": bool(q)} for t, n, c, mi, b, a, q in districts]}
        self.ensure()
        return self.cached(key, build)

    def events_summary(self, params: dict) -> dict:
        self.ensure()
        out = []
        for key, e in self.events.items():
            r = self.event({**params, "key": key})
            s = r["summary"]
            grp = s["nearby"] if e["in_texas"] else s["comparison"]
            out.append({"key": key, "label": e["label"], "date": e["date"], "place": e["place"], "deaths": e["deaths"],
                        "in_texas": e["in_texas"], "group": "nearby" if e["in_texas"] else "all Texas",
                        "before": grp.get("before"), "after": grp.get("after"),
                        "change": (grp["after"] - grp["before"]) if grp.get("after") is not None and grp.get("before") is not None else None,
                        "comparison_before": s["comparison"].get("before") if e["in_texas"] else None,
                        "comparison_after": s["comparison"].get("after") if e["in_texas"] else None,
                        "districts": grp.get("districts_qualifying", 0), "districts_total": grp.get("districts_total", 0),
                        "meetings": grp.get("meetings_before", 0) + grp.get("meetings_after", 0),
                        "thin": grp.get("districts_qualifying", 0) < MIN_EVENT_DISTRICTS})
        out.sort(key=lambda r: -(r["deaths"] or 0))
        first = self.event({**params, "key": next(iter(self.events))}) if self.events else {}
        return {"measures": self.info["measures"], "rows": out, "window": first.get("window"),
                "radius": first.get("radius"), "same_county": first.get("same_county"), "sample": first.get("sample"),
                "label": first.get("label"), "min_districts": MIN_EVENT_DISTRICTS}

    # ------------------------------------------------------------ words
    def _event_docs_sql(self, e: dict, params: dict, scope: str):
        window = self._int(params, "window", 12, 3, 36)
        near, radius, same_county = self._nearby_sql(params)
        span = int(window * MONTH_DAYS)
        ev = e["date"]
        tea = f"select tea_id from ev_dist ev where ev.event = '{e['key']}' and {near}" if e["in_texas"] else "select tea_id from dist"
        if scope == "passages":
            src = f"""select p.passage_id as doc_id, p.tea_id, p.date, p.record_id, p.detail, p.text
                from read_parquet({_q(self.files['passages'])}) p
                where list_contains(p.candidate_for, 'safety') and p.tea_id in ({tea})
                  and p.date > '{ev}'::date - {span} and p.date <= '{ev}'::date + {span}"""
        else:
            src = f"""select r.record_id as doc_id, r.tea_id, r.date, r.record_id, r.detail, left(r.text, 60000) as text
                from read_parquet({_q(self.files['records'])}) r
                where r.tea_id in ({tea}) and r.date > '{ev}'::date - {span} and r.date <= '{ev}'::date + {span}
                  and r.text_status in {READABLE}
                  and r.record_id in (select best_record_id from meet where tea_id in ({tea})
                      and date > '{ev}'::date - {span} and date <= '{ev}'::date + {span} and readable)"""
        return src, window, radius, same_county

    def _scope(self, params):
        scope = params.get("scope") or ("passages" if self.info["passages"] else "all")
        if scope not in ("all", "passages"):
            raise BadRequest("scope must be all or passages")
        if scope == "passages" and not self.info["passages"]:
            raise BadRequest("passages.parquet does not exist yet; use scope=all")
        if scope == "all" and not self.info["records"]:
            raise BadRequest("records.parquet does not exist")
        return scope

    def event_words(self, params: dict) -> dict:
        e = self._event(params.get("key") or "uvalde")
        scope = self._scope(params)
        n_top = self._int(params, "n", 40, 5, 200)
        src, window, radius, same_county = self._event_docs_sql(e, params, scope)
        key = ("words", WORDS_VERSION, e["key"], scope, src)

        def build():
            cached = self._disk_cache(key)
            if cached is not None:
                return cached
            cur = self.con.cursor()
            stop = sorted(STOPWORDS)
            # Units are documents: a word counts once per document, so one long
            # document or one district's template cannot dominate. Capitalisation
            # is kept to drop words that are almost never written in lower case
            # (mostly people's names).
            rows = cur.execute(f"""with docs as ({src}),
                toks as (select doc_id, tea_id, (date > '{e['date']}'::date) as after,
                    unnest(regexp_extract_all(text, '{WORD_RE_CASED.replace(chr(39), chr(39) * 2)}')) as tok from docs),
                dw as (select after, tea_id, doc_id, lower(tok) w, count(*) c,
                    count(*) filter (where tok = lower(tok)) lc from toks
                    where length(tok) between 3 and 30 and not list_contains(?, lower(tok)) group by all),
                sides as (select count(distinct doc_id) filter (where after) da, count(distinct doc_id) filter (where not after) db,
                    count(distinct tea_id) filter (where after) ta, count(distinct tea_id) filter (where not after) tb,
                    count(*) filter (where after) na, count(*) filter (where not after) nb from dw)
                select w, count(*) filter (where after), count(*) filter (where not after),
                    count(distinct tea_id) filter (where after), count(distinct tea_id) filter (where not after),
                    sum(c), sum(lc), any_value(sides.na), any_value(sides.nb), any_value(sides.da), any_value(sides.db),
                    any_value(sides.ta), any_value(sides.tb)
                from dw, sides group by w having count(*) >= 5""", [stop]).fetchall()
            if not rows:
                return {"words_after": [], "words_before": [], "units_after": 0, "units_before": 0,
                        "docs_after": 0, "docs_before": 0, "districts_after": 0, "districts_before": 0}
            na, nb, da, db, ta, tb = (int(x or 0) for x in rows[0][7:13])
            info = {r[0]: r for r in rows if (r[6] or 0) >= 0.05 * (r[5] or 1) or r[0] in self.term_words}
            after = {w: int(r[1] or 0) for w, r in info.items()}
            before = {w: int(r[2] or 0) for w, r in info.items()}
            scores = log_odds(after, before, max(na, 1), max(nb, 1)) if na and nb else {}
            min_a, min_b = max(3, math.ceil(0.15 * ta)), max(3, math.ceil(0.15 * tb))

            def item(w, d, z):
                r = info[w]
                return {"word": w, "z": round(z, 2), "delta": round(d, 3), "docs_after": int(r[1] or 0),
                        "docs_before": int(r[2] or 0), "districts_after": r[3], "districts_before": r[4],
                        "share_after": round((r[1] or 0) / da, 4) if da else None,
                        "share_before": round((r[2] or 0) / db, 4) if db else None}
            ranked = sorted(scores.items(), key=lambda kv: -kv[1][1])
            up = [item(w, d, z) for w, (d, z) in ranked if z > 0 and info[w][3] >= min_a][:n_top]
            down = [item(w, d, z) for w, (d, z) in reversed(ranked) if z < 0 and info[w][4] >= min_b][:n_top]
            result = {"words_after": up, "words_before": down, "units_after": na, "units_before": nb,
                      "docs_after": da, "docs_before": db, "districts_after": ta, "districts_before": tb,
                      "min_districts_after": min_a, "min_districts_before": min_b}
            self._disk_cache(key, result)
            return result
        result = self.cached(key, build)
        return {"measures": self.info["measures"], "event": e, "scope": scope, "window": window, "radius": radius,
                "same_county": same_county, "method": "Weighted log-odds ratio with an informative Dirichlet prior "
                "(Monroe, Colaresi and Quinn 2008). Counts are documents containing the word; the prior is the pooled before "
                "and after counts. Words must appear in at least 15% of the districts on the side they favour (minimum 3), "
                "and words almost never written in lower case (mostly names) are dropped unless they appear in the measure "
                "definitions (so Uvalde or Guardian stay). |z| above about 2 is unlikely by chance.",
                **result}

    def _disk_cache(self, key, value=None):
        sig = json.dumps([str(self._sig), repr(key)])
        path = self.cache_dir / f"words-{hashlib.sha256(sig.encode()).hexdigest()[:20]}.json"
        if value is None:
            try:
                stored = json.loads(path.read_text())
                return stored["value"] if stored.get("sig") == sig else None
            except (OSError, ValueError, KeyError):
                return None
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"sig": sig, "value": value}))
        tmp.replace(path)
        return value

    def kwic(self, params: dict) -> dict:
        e = self._event(params.get("key") or "uvalde")
        word = (params.get("word") or "").lower()
        if not re.fullmatch(WORD_RE, word) or len(word) > 30:
            raise BadRequest("word must be a single lowercase word")
        side = params.get("side") or "after"
        if side not in ("after", "before"):
            raise BadRequest("side must be after or before")
        scope = self._scope(params)
        limit = self._int(params, "limit", 12, 1, 50)
        src, *_ = self._event_docs_sql(e, params, scope)
        cmp = ">" if side == "after" else "<="
        pattern = r"\b" + word.replace("-", r"\-") + r"\b"

        def build():
            cur = self.con.cursor()
            rows = cur.execute(f"""with docs as ({src}) select doc_id, tea_id, date, record_id, detail, text from docs
                where date {cmp} '{e['date']}'::date and regexp_matches(lower(text), ?)
                order by date {'asc' if side == 'after' else 'desc'} limit {limit}""", [pattern]).fetchall()
            names = dict(cur.execute("select tea_id, school_district from dist").fetchall())
            links = self._links(cur, [r[3] for r in rows], [r[1] for r in rows])
            rx = re.compile(pattern, re.I)
            out = []
            for doc_id, tea_id, d, rid, detail, text in rows:
                for m in list(rx.finditer(text or ""))[:2]:
                    s, t = max(0, m.start() - 140), min(len(text), m.end() + 140)
                    out.append({"tea_id": tea_id, "school_district": names.get(tea_id), "date": d.isoformat(),
                                "detail": detail, "record_id": rid, "source_link": links.get(rid),
                                "before": ("…" if s else "") + re.sub(r"\s+", " ", text[s:m.start()]),
                                "match": text[m.start():m.end()],
                                "after": re.sub(r"\s+", " ", text[m.end():t]) + ("…" if t < len(text) else "")})
            return out
        items = self.cached(("kwic", e["key"], word, side, src, limit), build)
        return {"event": e, "word": word, "side": side, "scope": scope, "items": items}

    # ------------------------------------------------------------ district
    def board(self, params: dict) -> dict:
        self.ensure()
        tea_id = params.get("tea_id") or ""

        def build():
            cur = self.con.cursor()
            row = cur.execute("select * from dist where tea_id = ?", [tea_id]).fetchone()
            if row is None:
                raise NotFound("district not found")
            cols = [c[0] for c in cur.description]
            info = dict(zip(cols, row))
            strip = cur.execute(f"""select year, count(*) filter (where state = 'readable'), count(*) filter (where state = 'scan'),
                    count(*) filter (where state = 'unavailable'), count(*) from docs where tea_id = ? and year between {FIRST_YEAR} and year(current_date)
                    group by 1 order by 1""", [tea_id]).fetchall()
            screened, hit, unc, _ = self._measure({"measure": "safety"})
            ticks = cur.execute(f"""select meeting_id, date, readable, ({screened}) as screened, ({hit}) as hit, ({unc}) as unc,
                    subs, tone, closed, event_keys, best_detail, best_record_id from meet m where tea_id = ? order by date""", [tea_id]).fetchall()
            # Scans awaiting OCR have no meeting row in the measures; mark their dates from documents.
            scans = cur.execute("""select distinct date from docs where tea_id = ? and state = 'scan' and preferred
                                   and date is not null order by 1""", [tea_id]).fetchall()
            ev = cur.execute("""select event, miles, same_county from ev_dist where tea_id = ?
                                and (miles <= 100 or same_county or event in ('uvalde', 'santa_fe'))""", [tea_id]).fetchall()
            cov = None
            if self.files["coverage"].exists():
                cur.execute(f"select * from read_parquet({_q(self.files['coverage'])}) where tea_id = ?", [tea_id])
                names = [c[0] for c in cur.description]
                found = cur.fetchone()
                if found:
                    keep = ("flags", "platforms", "first_meeting_date", "last_meeting_date", "first_minutes_date",
                            "last_minutes_date", "longest_gap_days", "records", "meetings")
                    cov = {k: v for k, v in zip(names, found) if k in keep}
            meetings = [{"meeting_id": m, "date": d.isoformat(), "readable": r, "screened": s, "hit": bool(h), "uncertain": bool(u),
                         "subtopics": list(sb or []), "tone": t, "closed": c, "event_keys": list(ek or []), "detail": det,
                         "record_id": rid}
                        for m, d, r, s, h, u, sb, t, c, ek, det, rid in ticks]
            return {"measures": self.info["measures"], "district": info,
                    "strip": [{"year": y, "readable": r, "scan": s, "unavailable": u, "documents": n} for y, r, s, u, n in strip],
                    "meetings": meetings, "scan_dates": [d[0].isoformat() for d in scans],
                    "events": [{**self.events[k], "miles": round(mi, 1) if mi is not None else None, "same_county": sc}
                               for k, mi, sc in ev if k in self.events and self.events[k]["in_texas"]],
                    "coverage": cov,
                    "summary": {"meetings": len(meetings), "readable": sum(1 for m in meetings if m["readable"]),
                                "screened": sum(1 for m in meetings if m["screened"]),
                                "yes": sum(1 for m in meetings if m["hit"]),
                                "uncertain": sum(1 for m in meetings if m["uncertain"]), "scans": len(scans)}}
        return self.cached(("board", tea_id), build)

    def board_meetings(self, params: dict) -> dict:
        self.ensure()
        tea_id = params.get("tea_id") or ""
        only = params.get("only") or ""
        offset = self._int(params, "offset", 0, 0, 10**6)
        limit = self._int(params, "limit", 30, 1, 100)
        cur = self.con.cursor()
        cond = "and safety = 'yes'" if only == "safety" else "and temperament = 'yes'" if only == "temperament" else ""
        total = cur.execute(f"select count(*) from meet where tea_id = ? {cond}", [tea_id]).fetchone()[0]
        rows = cur.execute(f"""select meeting_id, date, meeting_type, meeting_title, best_detail, best_record_id, readable,
                safety, subs, temperament, tone, who, event_reference, event_keys, closed
                from meet where tea_id = ? {cond} order by date desc limit {limit} offset {offset}""", [tea_id]).fetchall()
        ev = self._evidence(cur, [r[0] for r in rows])
        links = self._links(cur, [r[5] for r in rows if r[5]], [tea_id])
        items = []
        for (mid, d, mtype, title, detail, rid, readable, safety, subs, temper, tone, who, evref, ekeys, closed) in rows:
            items.append({"meeting_id": mid, "date": d.isoformat(), "meeting_type": mtype, "meeting_title": title,
                          "detail": detail, "record_id": rid, "source_link": links.get(rid), "readable": readable,
                          "safety": safety, "subtopics": list(subs or []), "temperament": temper, "tone": tone, "who": who,
                          "event_reference": evref, "event_keys": list(ekeys or []), "closed": closed,
                          "evidence": [{"measure": x["measure"], "excerpt": x["excerpt"]} for x in ev.get(mid, [])]})
        return {"measures": self.info["measures"], "total": total, "offset": offset, "items": items}

    # ------------------------------------------------------------ map
    def map(self, params: dict) -> dict:
        self.ensure()
        screened, hit, unc, label = self._measure(params)
        where, args = self._where({k: v for k, v in params.items() if k in ("from", "to")})
        min_meetings = self._int(params, "min", 12, 1, 500)

        def build():
            cur = self.con.cursor()
            rows = cur.execute(f"""select d.tea_id, d.school_district, d.county_name, d.lat, d.lon, d.enrollment, d.size_band,
                    count(m.tea_id) filter (where {screened}), count(m.tea_id) filter (where {screened} and {hit}),
                    count(m.tea_id) filter (where {unc}), count(m.tea_id)
                from dist d left join meet m on m.tea_id = d.tea_id and {where}
                group by all""", args).fetchall()
            out = []
            for t, name, county, lat, lon, enr, band, n, k, u, allm in rows:
                out.append({"tea_id": t, "school_district": name, "county_name": county, "lat": lat, "lon": lon,
                            "enrollment": enr, "size_band": band, "meetings": n, "yes": k, "uncertain": u,
                            "all_meetings": allm, "share": k / n if n and self.info["measures"] else None,
                            "passes": n >= min_meetings})
            return {"measures": self.info["measures"], "label": label, "min": min_meetings, "districts": out,
                    "events": [e for e in self.events.values() if e["in_texas"]]}
        return self.cached(("map", screened, hit, where, tuple(args), min_meetings), build)

    # ------------------------------------------------------------ search
    def search(self, params: dict) -> dict:
        self.ensure()
        q = (params.get("q") or "").strip()
        if len(q) < 3:
            raise BadRequest("search for at least 3 characters")
        scope = params.get("scope") or ("passages" if self.info["passages"] else "full")
        if scope not in ("passages", "full"):
            raise BadRequest("scope must be passages or full")
        if scope == "passages" and not self.info["passages"]:
            raise BadRequest("passages.parquet does not exist yet; use scope=full")
        if scope == "full" and not self.info["records"]:
            raise BadRequest("records.parquet does not exist")
        offset = self._int(params, "offset", 0, 0, 5000)
        y0, y1 = self._years(params)
        # RE2 case-insensitive literal match: measured faster than ILIKE over the full text.
        pattern = "(?i)" + re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", q)
        cond, args = ["regexp_matches(s.text, ?)", "year(s.date) between ? and ?"], [pattern, y0, y1]
        if params.get("tea_id"):
            cond.append("s.tea_id = ?")
            args.append(params["tea_id"])
        if params.get("detail"):
            cond.append("s.detail = ?")
            args.append(params["detail"])
        mcond = []
        if params.get("subtopic"):
            if params["subtopic"] not in self.subtopics():
                raise BadRequest("unknown subtopic")
            mcond.append(f"list_contains(m.subs, '{params['subtopic']}') and m.safety = 'yes'")
        if params.get("tone"):
            if params["tone"] not in ("negative", "positive", "mixed"):
                raise BadRequest("unknown tone")
            mcond.append(f"m.tone = '{params['tone']}' and m.temperament = 'yes'")
        if scope == "passages":
            src = f"""select p.meeting_id, p.record_id, p.tea_id, p.date, p.detail, p.text, 'ok' as text_status
                      from read_parquet({_q(self.files['passages'])}) p"""
            join = "join meet m using (meeting_id)" if mcond else ""
        else:
            src = f"""select null as meeting_id, r.record_id, r.tea_id, r.date, r.detail, r.text, r.text_status, r.source_link
                      from read_parquet({_q(self.files['records'])}) r"""
            join = "join meet m on m.best_record_id = s.record_id" if mcond else ""
        where = " and ".join(cond + mcond)
        key = ("search", scope, where, tuple(args), offset)

        def build():
            cur = self.con.cursor()
            t0 = time.monotonic()
            rows = cur.execute(f"""select s.record_id, s.tea_id, s.date, s.detail, s.text_status, s.meeting_id,
                    substr(s.text, greatest(1, instr(lower(s.text), lower(?)) - 160), 360 + {len(q)}) as snippet,
                    instr(lower(s.text), lower(?)) > 161 as cut
                from ({src}) s {join} where {where} order by s.date desc nulls last, s.record_id
                limit 21 offset {offset}""", [q, q] + args).fetchall()
            names = dict(cur.execute("select tea_id, school_district from dist").fetchall())
            links = self._links(cur, [r[0] for r in rows], [r[1] for r in rows])
            items = [{"record_id": rid, "tea_id": t, "school_district": names.get(t), "date": d.isoformat() if d else None,
                      "detail": det, "text_status": ts, "meeting_id": mid, "source_link": links.get(rid),
                      "snippet": ("…" if cut else "") + re.sub(r"\s+", " ", snip or "") + "…"}
                     for rid, t, d, det, ts, mid, snip, cut in rows[:20]]
            return {"q": q, "scope": scope, "offset": offset, "items": items, "more": len(rows) > 20,
                    "seconds": round(time.monotonic() - t0, 2)}
        return {"measures": self.info["measures"], **self.cached(key, build)}

    # ------------------------------------------------------------ method
    def method(self) -> dict:
        self.ensure()

        def build():
            raw = DEFINITIONS.read_text() if DEFINITIONS.exists() else ""
            calibration = None
            if self.files["calibration"].exists():
                try:
                    calibration = json.loads(self.files["calibration"].read_text())
                except ValueError:
                    calibration = None
            cur = self.con.cursor()
            states = dict(cur.execute("select state, count(*) from docs group by 1").fetchall())
            ocr = None
            if self.store is not None:
                try:
                    ocr = self.store.progress().get("ocr")
                except Exception:  # progress is optional context here
                    ocr = None
            return {**self.info, "definitions": self.defs, "assumptions": assumptions(raw),
                    "calibration": calibration, "calibration_path": str(self.files["calibration"].relative_to(ROOT))
                    if self.files["calibration"].is_relative_to(ROOT) else str(self.files["calibration"]),
                    "doc_states": states, "ocr": ocr, "chip": self._chip(cur, {}),
                    "thin_meetings": THIN_MEETINGS, "min_event_districts": MIN_EVENT_DISTRICTS,
                    "size_bands": [b[2] for b in SIZE_BANDS],
                    "files": {k: self.files[k].exists() for k in ("meetings", "passages", "records", "coverage")}}
        return self.cached(("method",), build)

    # ------------------------------------------------------------ exports
    def panel_sql(self, params: dict) -> str:
        """Event x district x meeting panel over every district, for R or Stata."""
        self.ensure()
        window = self._int(params, "window", 12, 3, 36)
        radius = self._int(params, "radius", 50, 5, 500)
        span = int(window * MONTH_DAYS)
        evs = [(e["key"], e["label"], e["date"]) for e in self.events.values()]
        if not evs:
            raise NotFound("no events defined")
        values = ", ".join(f"('{k}', '{label.replace(chr(39), chr(39) * 2)}', '{d}'::date)" for k, label, d in evs)
        return f"""select e.event_key, e.event_label, e.event_date, d.tea_id, d.school_district, d.county_name,
                d.nces_id, d.esc_region, d.lat as district_lat, d.lon as district_lon, d.enrollment,
                round(ev.miles, 2) as distance_mi, ev.same_county, coalesce(ev.miles <= {radius}, false) as within_radius,
                m.meeting_id, m.date, datediff('day', e.event_date, m.date) as days_from_event,
                case when m.date <= e.event_date then -1 - floor(datediff('day', m.date, e.event_date) / {MONTH_DAYS})
                     else floor((datediff('day', e.event_date, m.date) - 1) / {MONTH_DAYS}) end::int as rel_month,
                m.date > e.event_date as after, m.readable, m.best_detail, m.safety, array_to_string(m.subs, ';') as safety_subtopics,
                m.temperament, m.tone as temperament_tone, m.who as temperament_who, m.event_reference,
                array_to_string(m.event_keys, ';') as event_keys, m.closed as closed_security_session,
                m.definition_version, {str(bool(self.info['measures'])).lower()} as measures_computed
            from (values {values}) e(event_key, event_label, event_date)
            join meet m on m.date > e.event_date - {span} and m.date <= e.event_date + {span}
            join dist d on d.tea_id = m.tea_id
            left join ev_dist ev on ev.event = e.event_key and ev.tea_id = m.tea_id
            order by e.event_key, d.tea_id, m.date"""

    def export_file(self, sql: str, fmt: str, name: str) -> Path:
        """Write a query to a cached CSV or Parquet file and return its path."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        sig = hashlib.sha256(repr((self._sig, sql, fmt)).encode()).hexdigest()[:16]
        path = self.cache_dir / f"{name}-{sig}.{fmt}"
        if not path.exists():
            tmp = path.with_suffix(".part")
            opts = "(format parquet, compression zstd)" if fmt == "parquet" else "(header, delimiter ',')"
            cur = self.cursor()
            cur.execute(f"copy ({sql}) to {_q(tmp)} {opts}")
            tmp.replace(path)
            for old in self.cache_dir.glob(f"{name}-*.{fmt}"):
                if old != path and time.time() - old.stat().st_mtime > 600:
                    old.unlink(missing_ok=True)
        return path


def term_words(defs: dict) -> set[str]:
    """Every word in the definitions' term lists and event names."""
    found: set[str] = set()

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k))
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and key in ("terms", "general_terms", "generic_terms", "ignore_phrases", "label",
                                                "negative", "positive", "students", "staff") or key.endswith("_terms"):
            found.update(re.findall(WORD_RE, str(value).lower()))
    for section in ("safety", "temperament", "events"):
        walk(defs.get(section) or {})
    return found


def assumptions(raw: str) -> list[dict]:
    """ASSUMPTION comments in definitions.yaml with the key path they sit under."""
    out, stack, block, block_path = [], [], [], None
    for line in raw.splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("#"):
            text = stripped.lstrip("#").strip()
            if "ASSUMPTION" in text or (block and text):
                block.append(text)
                block_path = block_path or ".".join(k for _, k in stack)
            elif block:
                out.append({"path": block_path, "text": " ".join(block)})
                block, block_path = [], None
            continue
        if block:
            out.append({"path": block_path, "text": " ".join(block)})
            block, block_path = [], None
        m = re.match(r"\s*([A-Za-z_][\w-]*):", line)
        if m:
            while stack and stack[-1][0] >= indent:
                stack.pop()
            stack.append((indent, m.group(1)))
            inline = line.split("#", 1)[1].strip() if "#" in line and "ASSUMPTION" in line.split("#", 1)[1] else None
            if inline:
                out.append({"path": ".".join(k for _, k in stack), "text": inline})
    if block:
        out.append({"path": block_path, "text": " ".join(block)})
    for item in out:
        item["text"] = re.sub(r"(^ASSUMPTION[.:]?\s*|\s*ASSUMPTION\.?$)", "", item["text"]).strip()
    return out


def table_rows(rows: list[dict], columns: list[str] | None = None) -> tuple[list[str], list[list]]:
    columns = columns or (list(rows[0]) if rows else [])
    out = []
    for r in rows:
        line = []
        for c in columns:
            v = r.get(c)
            if isinstance(v, (list, tuple)):
                v = ";".join("" if x is None else str(x) for x in v)
            elif isinstance(v, float):
                v = round(v, 6)
            line.append(v)
        out.append(line)
    return columns, out
