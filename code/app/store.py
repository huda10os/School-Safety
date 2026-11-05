"""Incremental DuckDB index over the staging files, plus the queries the app serves.

The collection jobs write JSONL while this runs, so the index never loads the
dataset into Python. Each staging file is tracked by (inode, size, mtime):

* records/<stem>.jsonl is append-only. Only bytes after the last indexed
  newline are parsed; a partial final line is left for the next pass. A
  record_id seen again replaces the earlier row (last line wins).
* listings/<stem>.jsonl is replaced atomically by the pipeline, so a change
  reindexes that one file.
* records.parquet, when present, is loaded through DuckDB directly; staging
  rows take precedence over it for the same record_id.

The index lives in <data>/app/index.duckdb and survives restarts.
"""
from __future__ import annotations

import csv
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa

SCHEMA_VERSION = 3
STATUSES = ("ok", "needs_ocr", "partial_ocr", "empty", "too_large", "unsupported", "error")
LISTING_COLS = ["record_id", "tea_id", "school_district", "county_name", "platform", "date",
                "date_raw", "meeting_type", "meeting_title", "meeting_id", "detail",
                "detail_raw", "source_link", "listing_url", "listing_row_text"]
DOC_COLS = ["document_link", "content_type", "raw_path", "content_sha256", "text_status",
            "text_method", "page_count", "char_count", "attachment_links", "error",
            "collected_at", "text"]
INT_COLS = {"page_count", "char_count"}
META_COLS = [c for c in LISTING_COLS + DOC_COLS if c != "text"]
BATCH_BYTES = 16 * 1024 * 1024
BATCH_ROWS = 2000


def _arrow_table(rows: list[dict], cols: list[str]) -> pa.Table:
    arrays = {}
    for col in cols:
        values = [row.get(col) for row in rows]
        if col in INT_COLS:
            arrays[col] = pa.array([v if isinstance(v, int) and not isinstance(v, bool) else None
                                    for v in values], pa.int64())
        elif col == "attachment_links":
            arrays[col] = pa.array([json.dumps(v) if v else None for v in values], pa.string())
        else:
            arrays[col] = pa.array([None if v is None else str(v) for v in values], pa.string())
    return pa.table(arrays)


def _select_list(cols: list[str]) -> str:
    return ", ".join(f"TRY_CAST({c} AS DATE) AS {c}" if c == "date" else c for c in cols)


class Store:
    def __init__(self, data_dir: Path, memory_limit: str = "160MB", threads: int = 2):
        self.data = Path(data_dir)
        self.listings_dir = self.data / "staging" / "listings"
        self.records_dir = self.data / "staging" / "records"
        self.app_dir = self.data / "app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.app_dir / "progress_history.jsonl"
        self.lock = threading.Lock()          # serialises index writes
        self.cursor_lock = threading.Lock()
        self.db = duckdb.connect(str(self.app_dir / "index.duckdb"))
        self.db.execute(f"SET memory_limit='{memory_limit}'")
        self.db.execute(f"SET threads={threads}")
        self.db.execute("SET preserve_insertion_order=false")
        self._init_schema()
        self.statuses: dict[str, dict] = {}   # stem -> status.json contents
        self._status_stats: dict[str, tuple] = {}
        self._csv_cache: dict[str, tuple] = {}
        self.last_change = 0.0
        self.index_state = {"running": False, "files_total": 0, "files_done": 0,
                            "first_pass_done": False, "last_pass_at": None, "last_pass_seconds": None}
        self._cache: dict[str, tuple[float, object]] = {}

    # ---------------------------------------------------------------- schema
    def _init_schema(self) -> None:
        con = self.db
        con.execute("CREATE TABLE IF NOT EXISTS meta (key VARCHAR PRIMARY KEY, value VARCHAR)")
        row = con.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        if row is None or int(row[0]) != SCHEMA_VERSION:
            for table in ("files", "listings", "records"):
                con.execute(f"DROP TABLE IF EXISTS {table}")
            con.execute("DELETE FROM meta")
            con.execute("INSERT INTO meta VALUES ('schema', ?)", [str(SCHEMA_VERSION)])
        con.execute("""CREATE TABLE IF NOT EXISTS files (
            path VARCHAR PRIMARY KEY, kind VARCHAR, inode BIGINT, size BIGINT,
            mtime DOUBLE, offset_ BIGINT)""")
        listing_defs = ", ".join(f"{c} DATE" if c == "date" else f"{c} VARCHAR" for c in LISTING_COLS)
        con.execute(f"CREATE TABLE IF NOT EXISTS listings ({listing_defs}, src VARCHAR)")
        doc_defs = ", ".join(f"{c} BIGINT" if c in INT_COLS else f"{c} VARCHAR" for c in DOC_COLS)
        con.execute(f"CREATE TABLE IF NOT EXISTS records ({listing_defs}, {doc_defs}, src VARCHAR)")
        # Every discovered or collected record once; collected values win.
        meta_r = ", ".join(META_COLS)
        meta_l = ", ".join(c if c in LISTING_COLS else f"NULL AS {c}" for c in META_COLS)
        con.execute(f"""CREATE OR REPLACE VIEW allrec AS
            SELECT {meta_r}, coalesce(text_status, 'pending') <> 'pending' AS collected FROM records
            UNION ALL
            SELECT {meta_l}, FALSE AS collected FROM listings l
            WHERE NOT EXISTS (SELECT 1 FROM records r WHERE r.record_id = l.record_id)""")

    def cursor(self):
        with self.cursor_lock:
            return self.db.cursor()

    # ---------------------------------------------------------------- indexing
    def refresh(self) -> dict:
        """One indexing pass over every staging file. Safe to call repeatedly."""
        with self.lock:
            started = time.time()
            self.index_state["running"] = True
            try:
                con = self.cursor()
                known = {r[0]: r[1:] for r in con.execute(
                    "SELECT path, inode, size, mtime, offset_ FROM files").fetchall()}
                todo = []
                seen = set()
                for kind, folder in (("listing", self.listings_dir), ("record", self.records_dir)):
                    if not folder.is_dir():
                        continue
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            name = entry.name
                            if name.endswith(".status.json") and kind == "listing":
                                self._read_status(entry)
                                continue
                            if not name.endswith(".jsonl"):
                                continue
                            st = entry.stat()
                            self.last_change = max(self.last_change, st.st_mtime)
                            rel = f"{kind}s/{name}"
                            seen.add(rel)
                            prev = known.get(rel)
                            if prev and prev[0] == st.st_ino and prev[1] == st.st_size and prev[2] == st.st_mtime:
                                continue
                            todo.append((kind, rel, Path(entry.path), st, prev))
                parquet = self.data / "records.parquet"
                # records.parquet is built from the staging files, so while they
                # exist it adds nothing; reading it only matters for a data dir
                # that holds the finished dataset alone.
                staging_present = any(rel.startswith("records/") for rel in seen)
                if parquet.exists() and not staging_present:
                    st = parquet.stat()
                    seen.add("records.parquet")
                    self.last_change = max(self.last_change, st.st_mtime)
                    prev = known.get("records.parquet")
                    if not prev or prev[1] != st.st_size or prev[2] != st.st_mtime:
                        todo.insert(0, ("parquet", "records.parquet", parquet, st, prev))
                for gone in set(known) - seen:
                    self._drop(con, gone)
                self.index_state.update(files_total=len(todo), files_done=0)
                for kind, rel, path, st, prev in todo:
                    try:
                        if kind == "listing":
                            self._index_listing(con, rel, path, st)
                        elif kind == "record":
                            self._index_records(con, rel, path, st, prev)
                        else:
                            self._index_parquet(con, rel, path, st)
                    except FileNotFoundError:
                        pass  # replaced mid-read; next pass picks it up
                    self.index_state["files_done"] += 1
                if todo:
                    self._cache.clear()
                return {"changed": len(todo)}
            finally:
                self.index_state.update(running=False, first_pass_done=True,
                                        last_pass_at=datetime.now(timezone.utc).isoformat(),
                                        last_pass_seconds=round(time.time() - started, 2))

    def _read_status(self, entry) -> None:
        st = entry.stat()
        stem = entry.name[: -len(".status.json")]
        key = (st.st_size, st.st_mtime)
        if self._status_stats.get(stem) == key:
            return
        try:
            data = json.loads(Path(entry.path).read_text())
        except (OSError, json.JSONDecodeError):
            return  # mid-write; keep the previous value
        data.pop("traceback", None)
        self.statuses[stem] = data
        self._status_stats[stem] = key
        self.last_change = max(self.last_change, st.st_mtime)
        self._cache.clear()

    def _drop(self, con, rel: str) -> None:
        con.execute("BEGIN")
        if rel.startswith("listings/"):
            con.execute("DELETE FROM listings WHERE src=?", [rel])
        else:
            con.execute("DELETE FROM records WHERE src=?", [rel])
        con.execute("DELETE FROM files WHERE path=?", [rel])
        con.execute("COMMIT")

    def _save_file(self, con, rel, kind, st, offset) -> None:
        con.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?, ?, ?)",
                    [rel, kind, st.st_ino, st.st_size, st.st_mtime, offset])

    def _index_listing(self, con, rel: str, path: Path, st) -> None:
        rows = {}
        with open(path, "rb") as handle:
            for line in handle:
                row = _parse(line)
                if row and row.get("record_id"):
                    rows[row["record_id"]] = row
        table = _arrow_table(list(rows.values()), LISTING_COLS)
        con.execute("BEGIN")
        con.execute("DELETE FROM listings WHERE src=?", [rel])
        if rows:
            con.register("batch", table)
            con.execute(f"INSERT INTO listings SELECT {_select_list(LISTING_COLS)}, ? FROM batch", [rel])
            con.unregister("batch")
        self._save_file(con, rel, "listing", st, st.st_size)
        con.execute("COMMIT")

    def _index_records(self, con, rel: str, path: Path, st, prev) -> None:
        offset = 0
        if prev and prev[0] == st.st_ino and st.st_size >= prev[1]:
            offset = prev[3]  # append-only growth: resume after the last full line
        else:
            con.execute("BEGIN")
            con.execute("DELETE FROM records WHERE src=?", [rel])
            con.execute("COMMIT")
        with open(path, "rb") as handle:
            handle.seek(offset)
            batch, batch_bytes, end = {}, 0, offset
            while True:
                line = handle.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    break  # partial line still being written
                end += len(line)
                row = _parse(line)
                if row and row.get("record_id"):
                    batch.pop(row["record_id"], None)
                    batch[row["record_id"]] = row
                    batch_bytes += len(line)
                if batch_bytes >= BATCH_BYTES or len(batch) >= BATCH_ROWS:
                    self._write_records(con, rel, batch, st, end)
                    batch, batch_bytes = {}, 0
            self._write_records(con, rel, batch, st, end)

    def _write_records(self, con, rel, batch: dict, st, end: int) -> None:
        con.execute("BEGIN")
        if batch:
            table = _arrow_table(list(batch.values()), LISTING_COLS + DOC_COLS)
            con.register("batch", table)
            con.execute("DELETE FROM records WHERE record_id IN (SELECT record_id FROM batch)")
            con.execute(f"INSERT INTO records SELECT {_select_list(LISTING_COLS + DOC_COLS)}, ? FROM batch",
                        [rel])
            con.unregister("batch")
        self._save_file(con, rel, "record", st, end)
        con.execute("COMMIT")

    def _index_parquet(self, con, rel: str, path: Path, st) -> None:
        cols = [r[0] for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()]
        exprs = []
        for c in LISTING_COLS + DOC_COLS:
            if c not in cols:
                exprs.append(f"NULL AS {c}")
            elif c == "date":
                exprs.append("TRY_CAST(date AS DATE) AS date")
            elif c in INT_COLS:
                exprs.append(f"TRY_CAST({c} AS BIGINT) AS {c}")
            elif c == "attachment_links":
                exprs.append("CAST(to_json(attachment_links) AS VARCHAR) AS attachment_links")
            else:
                exprs.append(f"CAST({c} AS VARCHAR) AS {c}")
        con.execute("BEGIN")
        try:
            con.execute("DELETE FROM records WHERE src=?", [rel])
            con.execute(f"""INSERT INTO records SELECT {', '.join(exprs)}, '{rel}' FROM read_parquet(?) p
                WHERE p.record_id NOT IN (SELECT record_id FROM records)""", [str(path)])
            self._save_file(con, rel, "parquet", st, st.st_size)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    # ---------------------------------------------------------------- reference data
    def _csv(self, path: Path) -> list[dict]:
        try:
            st = path.stat()
        except FileNotFoundError:
            return []
        key = (st.st_size, st.st_mtime)
        cached = self._csv_cache.get(str(path))
        if cached and cached[0] == key:
            return cached[1]
        with open(path, newline="", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if text and not text.endswith("\n"):
            text = text[: text.rfind("\n") + 1]  # drop a partially written last row
        rows = [r for r in csv.DictReader(text.splitlines()) if r.get("tea_id")]
        self._csv_cache[str(path)] = (key, rows)
        return rows

    def districts_ref(self) -> list[dict]:
        return self._csv(self.data / "reference" / "districts.csv")

    def platforms_ref(self) -> list[dict]:
        return self._csv(self.data / "reference" / "platforms.csv")

    def platforms_file_exists(self) -> bool:
        return (self.data / "reference" / "platforms.csv").exists()

    # ---------------------------------------------------------------- caching
    def cached(self, key: str, ttl: float, fn):
        hit = self._cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    # ---------------------------------------------------------------- aggregates
    def progress(self) -> dict:
        return self.cached("progress", 30, self._progress)

    def _progress(self) -> dict:
        con = self.cursor()
        districts = self.districts_ref()
        mapping = self.platforms_ref()
        by_district: dict[str, set] = {}
        for row in mapping:
            by_district.setdefault(row["tea_id"], set()).add((row.get("platform") or "").strip() or "none")
        platform_counts: dict[str, int] = {}
        for tea_id in {d["tea_id"] for d in districts}:
            plats = by_district.get(tea_id)
            if not plats:
                platform_counts["not mapped yet"] = platform_counts.get("not mapped yet", 0) + 1
                continue
            real = sorted(p for p in plats if p not in ("none", "unknown"))
            label = " + ".join(real) if real else ("none" if "none" in plats else "unknown")
            platform_counts[label] = platform_counts.get(label, 0) + 1
        mapped = sum(1 for d in districts if d["tea_id"] in by_district)
        with_platform = sum(1 for d in districts
                            if any(p not in ("none", "unknown") for p in by_district.get(d["tea_id"], ())))
        discovered_districts = len({s.get("tea_id") for s in self.statuses.values()
                                    if not s.get("error") and s.get("tea_id")})
        discovery_errors = sum(1 for s in self.statuses.values() if s.get("error"))
        totals = con.execute("""SELECT
                (SELECT count(*) FROM listings),
                (SELECT count(*) FROM allrec),
                (SELECT count(*) FROM allrec WHERE collected),
                (SELECT count(*) FROM allrec WHERE collected AND text_status <> 'error'),
                (SELECT count(DISTINCT tea_id) FROM allrec WHERE collected)""").fetchone()
        listed, discovered, collected, done, collected_districts = totals
        by_status = dict(con.execute(
            "SELECT text_status, count(*) FROM allrec WHERE collected GROUP BY 1").fetchall())
        recent = con.execute("""SELECT count(*) FROM records
            WHERE TRY_CAST(collected_at AS TIMESTAMPTZ) >= now() - INTERVAL 30 MINUTE""").fetchone()[0]
        backlog = max(discovered - done, 0)
        history = self.read_history(hours=48)
        rate, rate_basis = self._rate(history, collected, recent)
        eta_minutes = round(backlog / rate, 1) if rate and backlog else (0 if not backlog else None)
        chart = [{"t": h["t"], "collected": h["collected"], "discovered": h["discovered"]} for h in history]
        if len(chart) < 3:
            chart = self._chart_from_timestamps(con)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "districts_total": len(districts),
            "districts_mapped": mapped,
            "districts_with_platform": with_platform,
            "platforms_file": self.platforms_file_exists(),
            "platform_counts": dict(sorted(platform_counts.items(), key=lambda kv: -kv[1])),
            "district_platforms_discovered": len(self.statuses),
            "districts_discovered": discovered_districts,
            "districts_collected": collected_districts,
            "discovery_errors": discovery_errors,
            "records_listed": listed,
            "records_discovered": discovered,
            "records_collected": collected,
            "records_done": done,
            "records_backlog": backlog,
            "collected_pct": round(100 * done / discovered, 1) if discovered else 0.0,
            "by_status": by_status,
            "collected_last_30_min": recent,
            "rate_per_min": round(rate, 1) if rate is not None else None,
            "rate_basis": rate_basis,
            "eta_minutes": eta_minutes,
            "mapping_incomplete": mapped < len(districts),
            "last_data_change": (datetime.fromtimestamp(self.last_change, timezone.utc).isoformat()
                                 if self.last_change else None),
            "index": dict(self.index_state),
            "chart": chart,
        }

    @staticmethod
    def _rate(history: list[dict], collected_now: int, recent: int):
        if history:
            newest = datetime.fromisoformat(history[-1]["t"])
            window = [h for h in history if datetime.fromisoformat(h["t"]) >= newest - timedelta(minutes=31)]
            first = window[0]
            minutes = (newest - datetime.fromisoformat(first["t"])).total_seconds() / 60
            if minutes >= 5:
                return max(history[-1]["collected"] - first["collected"], 0) / minutes, \
                    f"server samples over the last {round(minutes)} min"
        return recent / 30, "collection timestamps in the last 30 min"

    @staticmethod
    def _chart_from_timestamps(con) -> list[dict]:
        rows = con.execute("""
            WITH t AS (SELECT date_trunc('minute', TRY_CAST(collected_at AS TIMESTAMPTZ)) m FROM records
                       WHERE collected_at IS NOT NULL),
            b AS (SELECT time_bucket(INTERVAL 10 MINUTE, m) m, count(*) n FROM t WHERE m IS NOT NULL GROUP BY 1)
            SELECT strftime(m AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S+00:00'),
                   sum(n) OVER (ORDER BY m) FROM b ORDER BY m""").fetchall()
        return [{"t": t, "collected": int(n), "discovered": None} for t, n in rows[-300:]]

    def read_history(self, hours: float = 48) -> list[dict]:
        if not self.history_path.exists():
            return []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        out = []
        with open(self.history_path, "rb") as handle:
            size = handle.seek(0, 2)
            # 48 h of one-per-minute samples is ~600 KB; read only the tail.
            handle.seek(max(size - 2_000_000, 0))
            for line in handle:
                row = _parse(line)
                if row and "t" in row:
                    try:
                        if datetime.fromisoformat(row["t"]) >= cutoff:
                            out.append(row)
                    except ValueError:
                        continue
        return out

    def sample(self) -> dict | None:
        """Append one progress sample; skipped until the first index pass is complete."""
        if not self.index_state["first_pass_done"]:
            return None
        con = self.cursor()
        discovered, collected, done = con.execute("""SELECT count(*), count(*) FILTER (collected),
            count(*) FILTER (collected AND text_status <> 'error') FROM allrec""").fetchone()
        row = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "discovered": discovered, "collected": collected, "done": done}
        with open(self.history_path, "a") as handle:
            handle.write(json.dumps(row) + "\n")
        self._cache.pop("progress", None)
        return row

    def districts(self) -> list[dict]:
        return self.cached("districts", 30, self._districts)

    def _districts(self) -> list[dict]:
        con = self.cursor()
        stats = {r[0]: r for r in con.execute("""
            WITH a AS (SELECT * FROM allrec),
            s AS (SELECT tea_id, count(*) n_disc,
                    count(*) FILTER (collected) n_coll,
                    count(*) FILTER (collected AND text_status <> 'error') n_done,
                    count(*) FILTER (text_status = 'error') n_err,
                    count(*) FILTER (text_status IN ('needs_ocr', 'partial_ocr')) n_ocr,
                    min(date) FILTER (detail = 'Minutes') min_min,
                    max(date) FILTER (detail = 'Minutes') max_min,
                    count(*) FILTER (detail = 'Minutes') n_min,
                    list(DISTINCT platform) plats
                  FROM a GROUP BY tea_id),
            d AS (SELECT DISTINCT tea_id, date FROM a WHERE date IS NOT NULL AND date <= current_date),
            g AS (SELECT tea_id, date - lag(date) OVER (PARTITION BY tea_id ORDER BY date) gap, date FROM d),
            gm AS (SELECT tea_id, max(gap) max_gap, arg_max(date, gap) gap_end FROM g GROUP BY tea_id)
            SELECT s.*, gm.max_gap, gm.gap_end FROM s LEFT JOIN gm USING (tea_id)""").fetchall()}
        mapping: dict[str, list[dict]] = {}
        for row in self.platforms_ref():
            mapping.setdefault(row["tea_id"], []).append(row)
        status_by: dict[str, list[dict]] = {}
        for stem, st in self.statuses.items():
            status_by.setdefault(st.get("tea_id") or stem.split("__")[0], []).append(st)
        out = []
        for d in self.districts_ref():
            tid = d["tea_id"]
            s = stats.get(tid)
            rows = mapping.get(tid, [])
            mapped = sorted({(r.get("platform") or "none").strip() or "none" for r in rows})
            real = [p for p in mapped if p not in ("none", "unknown")]
            seen_plats = set(s[9]) if s else set()
            all_plats = sorted(set(real) | {p for p in seen_plats if p})
            sts = status_by.get(tid, [])
            disc_errors = [f"{x.get('platform')}: {x.get('error')}" for x in sts if x.get("error")]
            n_disc, n_coll, n_done, n_err, n_ocr = (s[1:6] if s else (0, 0, 0, 0, 0))
            min_min, max_min, n_min = (s[6], s[7], s[8]) if s else (None, None, 0)
            max_gap, gap_end = (s[10], s[11]) if s else (None, None)
            flags = []
            if n_disc and not min_min:
                flags.append("no_minutes")
            elif n_disc:
                if min_min > _date("2018-01-01"):
                    flags.append("late_start")
                if (max_min - min_min).days < 3 * 365:
                    flags.append("short_span")
            if max_gap is not None and max_gap > 183:
                flags.append("gap")
            if len(all_plats) > 1:
                flags.append("multi_platform")
            if not rows:
                state = "not_mapped"
            elif not real and not seen_plats:
                state = "no_platform"
            elif disc_errors and not n_disc:
                state = "discovery_error"
            elif not sts and not n_disc:
                state = "waiting_discovery"
            elif not n_disc:
                state = "no_records"
            elif any(f"{tid}__{name}" not in self.statuses for name in real):
                state = "partial_discovery"  # some mapped platforms not read yet
            elif n_done >= n_disc:
                state = "complete"
            elif n_coll:
                state = "collecting"
            else:
                state = "discovered"
            out.append({
                "tea_id": tid, "school_district": d["school_district"], "county_name": d["county_name"],
                "website": d.get("website"), "platforms": all_plats or mapped,
                "state": state, "discovered": n_disc, "collected": n_coll, "done": n_done,
                "errors": n_err, "needs_ocr": n_ocr, "minutes": n_min,
                "minutes_first": min_min.isoformat() if min_min else None,
                "minutes_last": max_min.isoformat() if max_min else None,
                "max_gap_days": max_gap, "gap_end": gap_end.isoformat() if gap_end else None,
                "flags": flags, "discovery_errors": disc_errors,
            })
        return out

    def district(self, tea_id: str) -> dict | None:
        base = next((d for d in self.districts() if d["tea_id"] == tea_id), None)
        if base is None:
            return None
        con = self.cursor()
        mapping = [r for r in self.platforms_ref() if r["tea_id"] == tea_id]
        per_platform = {r[0]: r for r in con.execute("""SELECT platform, count(*),
                count(*) FILTER (collected), count(*) FILTER (text_status='ok'),
                count(*) FILTER (text_status='error'), min(date), max(date),
                max(collected_at) FROM allrec WHERE tea_id=? GROUP BY platform""", [tea_id]).fetchall()}
        platforms = []
        names = [(r.get("platform") or "none") for r in mapping] + \
            [p for p in per_platform if p not in {(r.get("platform") or "none") for r in mapping}]
        for name in dict.fromkeys(names):
            ref = next((r for r in mapping if (r.get("platform") or "none") == name), {})
            st = self.statuses.get(f"{tea_id}__{name}", {})
            agg = per_platform.get(name)
            platforms.append({
                "platform": name, "entry_url": ref.get("entry_url") or st.get("entry_url"),
                "evidence": ref.get("evidence"), "note": ref.get("note"),
                "discovered_at": st.get("discovered_at"), "discovery_error": st.get("error"),
                "listed": st.get("records"),
                "discovered": agg[1] if agg else 0, "collected": agg[2] if agg else 0,
                "ok": agg[3] if agg else 0, "errors": agg[4] if agg else 0,
                "first_date": agg[5].isoformat() if agg and agg[5] else None,
                "last_date": agg[6].isoformat() if agg and agg[6] else None,
                "last_collected_at": agg[7] if agg else None,
            })
        timeline = con.execute("""SELECT coalesce(CAST(year(date) AS VARCHAR), 'no date') y, detail, count(*)
            FROM allrec WHERE tea_id=? GROUP BY 1, 2 ORDER BY 1""", [tea_id]).fetchall()
        years: dict[str, dict] = {}
        for y, detail, n in timeline:
            years.setdefault(y, {})[detail or "Other"] = n
        by_status = dict(con.execute("""SELECT coalesce(text_status, 'pending'), count(*) FROM allrec
            WHERE tea_id=? GROUP BY 1""", [tea_id]).fetchall())
        return {**base, "platform_rows": platforms, "timeline": years, "by_status": by_status}

    def facets(self) -> dict:
        def build():
            con = self.cursor()
            return {
                "counties": sorted({d["county_name"] for d in self.districts_ref() if d.get("county_name")}),
                "platforms": [r[0] for r in con.execute(
                    "SELECT DISTINCT platform FROM allrec WHERE platform IS NOT NULL ORDER BY 1").fetchall()],
                "details": ["Agenda", "Minutes", "Projector", "Extras", "Other"],
                "meeting_types": [r[0] for r in con.execute("""SELECT meeting_type FROM allrec
                    WHERE meeting_type IS NOT NULL GROUP BY 1 ORDER BY count(*) DESC LIMIT 40""").fetchall()],
                "text_statuses": ["pending", *STATUSES],
                "districts": [{"tea_id": d["tea_id"], "name": d["school_district"]} for d in self.districts_ref()],
            }
        return self.cached("facets", 60, build)

    # ---------------------------------------------------------------- records
    @staticmethod
    def _where(f: dict, collected_only: bool = False) -> tuple[str, list]:
        """SQL filter for allrec (or for records when collected_only)."""
        clauses, params = [], []
        for key, col in (("tea_id", "tea_id"), ("county", "county_name"), ("detail", "detail"),
                         ("meeting_type", "meeting_type"), ("platform", "platform")):
            if f.get(key):
                clauses.append(f"{col} = ?")
                params.append(f[key])
        if f.get("text_status"):
            if f["text_status"] == "pending":
                clauses.append("FALSE" if collected_only else "NOT collected")
            else:
                clauses.append("text_status = ?")
                params.append(f["text_status"])
        if f.get("date_from"):
            clauses.append("date >= TRY_CAST(? AS DATE)")
            params.append(f["date_from"])
        if f.get("date_to"):
            clauses.append("date <= TRY_CAST(? AS DATE)")
            params.append(f["date_to"])
        if f.get("name"):
            clauses.append("(meeting_title ILIKE ? ESCAPE '\\' OR listing_row_text ILIKE ? ESCAPE '\\')")
            params += [_like(f["name"])] * 2
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def records(self, f: dict, limit: int = 50, offset: int = 0) -> dict:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        order = "date DESC NULLS LAST, school_district, detail" if f.get("sort") != "asc" \
            else "date ASC NULLS LAST, school_district, detail"
        names = ["record_id", "tea_id", "school_district", "county_name", "platform", "date",
                 "meeting_type", "meeting_title", "detail", "detail_raw", "source_link", "text_status",
                 "char_count", "page_count", "collected_at"]
        con = self.cursor()
        q = (f.get("q") or "").strip()
        if q:
            # Text search reads only collected rows and is not counted: a broad term
            # would otherwise scan the whole corpus twice. One extra row tells us
            # whether another page exists.
            where, params = self._where(f, collected_only=True)
            where = (where + " AND" if where else " WHERE") + " text ILIKE ? ESCAPE '\\'"
            rows = con.execute(f"SELECT {', '.join(names)}, TRUE FROM records{where} "
                               f"ORDER BY {order} LIMIT ? OFFSET ?",
                               [*params, _like(q), limit + 1, offset]).fetchall()
            items = [dict(zip(names + ["collected"], r)) for r in rows[:limit]]
            snips = self._snippets([i["record_id"] for i in items], q) if items else {}
            for item in items:
                item["snippet"] = snips.get(item["record_id"])
            total, has_more = None, len(rows) > limit
        else:
            where, params = self._where(f)
            rows = con.execute(f"SELECT {', '.join(names)}, collected FROM allrec{where} "
                               f"ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset]).fetchall()
            items = [dict(zip(names + ["collected"], r)) for r in rows]
            total = con.execute(f"SELECT count(*) FROM allrec{where}", params).fetchone()[0]
            has_more = offset + len(items) < total
        for item in items:
            if item.get("date") is not None:
                item["date"] = item["date"].isoformat()
        return {"items": items, "total": total, "has_more": has_more, "limit": limit, "offset": offset}

    def _snippets(self, ids: list[str], q: str, width: int = 160) -> dict:
        con = self.cursor()
        placeholders = ",".join("?" * len(ids))
        rows = con.execute(f"""SELECT record_id, strpos(lower(text), lower(?)) p,
                substr(text, greatest(strpos(lower(text), lower(?)) - {width // 2}, 1), {width + len(q)})
                FROM records WHERE record_id IN ({placeholders})""", [q, q, *ids]).fetchall()
        return {rid: " ".join((snip or "").split()) for rid, _, snip in rows}

    def record(self, record_id: str, text_limit: int | None = 200_000) -> dict | None:
        con = self.cursor()
        all_cols = LISTING_COLS + DOC_COLS
        text_expr = f"substr(text, 1, {int(text_limit)})" if text_limit else "text"
        cols_sql = ", ".join(text_expr + " AS text" if c == "text" else c for c in all_cols)
        row = con.execute(f"SELECT {cols_sql}, length(text), src FROM records WHERE record_id=?",
                          [record_id]).fetchone()
        if row:
            rec = dict(zip(all_cols + ["text_length", "src"], row))
            rec["collected"] = (rec.get("text_status") or "pending") != "pending"
        else:
            row = con.execute(f"SELECT {', '.join(LISTING_COLS)}, src FROM listings WHERE record_id=?",
                              [record_id]).fetchone()
            if not row:
                return None
            rec = dict(zip(LISTING_COLS + ["src"], row))
            rec.update(collected=False, text_status="pending", text=None, text_length=0)
        if rec.get("date") is not None:
            rec["date"] = rec["date"].isoformat()
        if rec.get("attachment_links"):
            try:
                rec["attachment_links"] = json.loads(rec["attachment_links"])
            except (TypeError, json.JSONDecodeError):
                pass
        rec["text_truncated"] = bool(text_limit and (rec.get("text_length") or 0) > text_limit)
        return rec

    def record_text(self, record_id: str) -> str | None:
        row = self.cursor().execute("SELECT text FROM records WHERE record_id=?", [record_id]).fetchone()
        return row[0] if row else None

    def export_rows(self, f: dict, batch: int = 2000):
        """Yield the header, then metadata rows matching the filters, in batches."""
        cols = [c for c in META_COLS if c != "attachment_links"]
        con = self.cursor()
        q = (f.get("q") or "").strip()
        if q:
            where, params = self._where(f, collected_only=True)
            where = (where + " AND" if where else " WHERE") + " text ILIKE ? ESCAPE '\\'"
            sql = f"SELECT {', '.join(cols)} FROM records{where} ORDER BY tea_id, date"
            params = [*params, _like(q)]
        else:
            where, params = self._where(f)
            sql = f"SELECT {', '.join(cols)} FROM allrec{where} ORDER BY tea_id, date"
        result = con.execute(sql, params)
        yield cols
        while True:
            chunk = result.fetchmany(batch)
            if not chunk:
                break
            yield from chunk

    def coverage(self, tea_id: str | None = None, limit: int = 2000) -> list[dict] | None:
        path = self.data / "coverage.parquet"
        if not path.exists():
            return None
        con = self.cursor()
        rel = con.execute("SELECT * FROM read_parquet(?)", [str(path)])
        cols = [c[0] for c in rel.description]
        if tea_id and "tea_id" in cols:
            rel = con.execute("SELECT * FROM read_parquet(?) WHERE CAST(tea_id AS VARCHAR)=? LIMIT ?",
                              [str(path), tea_id, limit])
        else:
            rel = con.execute("SELECT * FROM read_parquet(?) LIMIT ?", [str(path), limit])
        return [{k: _jsonable(v) for k, v in zip(cols, r)} for r in rel.fetchall()]

    def close(self) -> None:
        with self.lock:
            self.db.close()


def _parse(line: bytes):
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None  # truncated or interleaved line from an interrupted writer
    return row if isinstance(row, dict) else None


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _date(text: str):
    return datetime.strptime(text, "%Y-%m-%d").date()


def _jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
