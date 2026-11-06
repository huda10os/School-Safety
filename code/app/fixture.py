#!/usr/bin/env python3
"""Synthetic measures fixture for tests and local visual QA. Never real data.

    python code/app/fixture.py OUT_DIR [--full]

Writes OUT_DIR/{reference/districts.csv, records.parquet, coverage.parquet,
measures/meetings.parquet, measures/passages.parquet, gold/calibration.json}
following code/measures/SCHEMA.md, plus a SYNTHETIC_FIXTURE marker file that
makes the app show a "synthetic data" banner. Without --full it uses eight
real district ids (so distance rules work); with --full it uses every
district in data/reference/districts.csv. Every excerpt says "(synthetic)".
Do not point it at data/ itself.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

APP = Path(__file__).resolve().parent
ROOT = APP.parents[1]
SMALL = [
    ("232903", "Uvalde CISD", "Uvalde County", "48463"),
    ("232901", "Knippa ISD", "Uvalde County", "48463"),
    ("232902", "Sabinal ISD", "Uvalde County", "48463"),
    ("084909", "Santa Fe ISD", "Galveston County", "48167"),
    ("084901", "Dickinson ISD", "Galveston County", "48167"),
    ("057905", "Dallas ISD", "Dallas County", "48113"),
    ("152901", "Lubbock ISD", "Lubbock County", "48303"),
    ("188901", "Amarillo ISD", "Potter County", "48375"),
]
EVENTS = {"uvalde": (date(2022, 5, 24), 29.2097, -99.7862), "santa_fe": (date(2018, 5, 18), 29.3780, -95.1055)}
SUBTOPICS = ["armed_staff", "police_sro", "hardening", "threat_assessment", "drills_emergency", "mental_health"]
SAFETY_TEXT = {
    "armed_staff": "The board reviewed the guardian program and the training required for armed staff.",
    "police_sro": "The superintendent presented an interlocal agreement for a school resource officer.",
    "hardening": "Trustees approved a secure entry vestibule and new access control on every campus.",
    "threat_assessment": "Staff described the behavioral threat assessment team and the anonymous tip line.",
    "drills_emergency": "The district reported lockdown drills and an updated emergency operations plan.",
    "mental_health": "Counselors outlined mental health supports and the crisis team for students.",
}
ROUTINE = ["The board approved the consent agenda and the minutes of the previous meeting.",
           "Trustees discussed the budget calendar, tax rate and the annual audit.",
           "The board recognized students for academic and athletic achievements.",
           "The board approved bids for roofing, cafeteria equipment and bus purchases.",
           "Personnel items were approved, including resignations and new hires.",
           "The superintendent gave an update on enrollment and attendance."]
TEMPER = {"negative": "Teachers reported that students were anxious and stressed after the lockdown.",
          "positive": "Principals said students were proud and excited about the new year.",
          "mixed": "Staff morale is low, but teachers said students were happy to be back."}
STR, STRS = pa.string(), pa.list_(pa.string())
EVIDENCE = pa.list_(pa.struct([("passage_id", STR), ("measure", STR), ("excerpt", STR)]))
MEETING_SCHEMA = pa.schema([
    *[(k, STR) for k in ("meeting_id", "tea_id", "school_district", "county_name", "county_fips", "nces_id")],
    ("district_lat", pa.float64()), ("district_lon", pa.float64()), ("esc_region", STR),
    ("date", pa.date32()), ("date_source", STR), ("meeting_type", STR), ("meeting_title", STR),
    ("platforms", STRS), ("record_ids", STRS), ("best_record_id", STR), ("best_detail", STR),
    ("has_minutes_text", pa.bool_()), ("has_agenda_text", pa.bool_()), ("readable", pa.bool_()),
    ("text_chars", pa.int64()), ("n_passages", pa.int32()), ("closed_security_session", pa.bool_()),
    ("named_events", STRS), ("safety", STR), ("safety_prob", pa.float64()), ("safety_subtopics", STRS),
    ("safety_resolved_by", STR), ("temperament", STR), ("temperament_prob", pa.float64()),
    ("temperament_tone", STR), ("temperament_who", STR), ("event_reference", STR), ("event_keys", STRS),
    ("evidence", EVIDENCE), ("definition_version", STR), ("label_version", STR)])
PASSAGE_SCHEMA = pa.schema([
    *[(k, STR) for k in ("passage_id", "meeting_id", "record_id", "tea_id")], ("date", pa.date32()), ("detail", STR),
    ("start", pa.int64()), ("end", pa.int64()), ("text", STR),
    *[(k, STRS) for k in ("candidate_for", "matched_terms", "subtopic_hits", "excluded_hits")], ("definition_version", STR)])
RECORD_SCHEMA = pa.schema([
    *[(k, STR) for k in ("record_id", "tea_id", "school_district", "county_name", "platform")], ("date", pa.date32()),
    *[(k, STR) for k in ("meeting_type", "meeting_title", "meeting_id", "detail", "source_link", "text", "text_status")],
    ("char_count", pa.int32()), ("preferred_for_meeting", pa.bool_())])


def sha(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def miles(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def build(out: Path, full: bool = False, seed: int = 7) -> dict:
    out = Path(out)
    if out.resolve() == (ROOT / "data").resolve():
        raise SystemExit("refusing to write a synthetic fixture into data/")
    rng = random.Random(seed)
    points = {r["tea_id"]: r for r in csv.DictReader(open(APP / "reference" / "district_points.csv"))}
    if full:
        districts = [(r["tea_id"], r["school_district"], r["county_name"], r["county_fips"])
                     for r in csv.DictReader(open(ROOT / "data" / "reference" / "districts.csv"))]
    else:
        districts = SMALL
    (out / "reference").mkdir(parents=True, exist_ok=True)
    (out / "measures").mkdir(exist_ok=True)
    (out / "gold").mkdir(exist_ok=True)
    with open(out / "reference" / "districts.csv", "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["tea_id", "school_district", "county_name", "county_fips", "website"])
        for d in districts:
            w.writerow([*d, ""])
    meetings, passages, records, coverage = [], [], [], []
    today = date(2026, 10, 31)
    for i, (tea_id, name, county, fips) in enumerate(districts):
        p = points.get(tea_id, {})
        lat, lon = float(p.get("lat") or 31), float(p.get("lon") or -99)
        if full and i % 25 == 24:
            coverage.append({"tea_id": tea_id, "school_district": name, "county_name": county, "platforms": "",
                             "first_meeting_date": None, "last_meeting_date": None, "longest_gap_days": None,
                             "flags": "no_records"})
            continue
        start = 2014 if not full else rng.choice([2004, 2008, 2010, 2012, 2014, 2016, 2018, 2019, 2020, 2021, 2022, 2023])
        scan_rate = rng.choice([0.05, 0.1, 0.15, 0.4]) if full else 0.1
        region = str(1 + int((lon + 106.6) / 1.9) % 20)
        d = date(start, 1, 12)
        first = None
        while d <= today:
            day = d + timedelta(days=rng.randint(0, 8))
            first = first or day
            dist = {k: miles(lat, lon, e[1], e[2]) for k, e in EVENTS.items()}
            p_safe = 0.12 + 0.012 * (day.year - 2010)
            p_temper = 0.10
            evkeys = []
            for k, (edate, _, _) in EVENTS.items():
                months = (day - edate).days / 30.4
                if 0 < months <= 12:
                    p_safe += (0.06 if k == "uvalde" else 0.03) + 0.30 * math.exp(-dist[k] / 60)
                    p_temper += 0.08 * math.exp(-dist[k] / 80)
                    if rng.random() < 0.15 + 0.4 * math.exp(-dist[k] / 60):
                        evkeys.append(k)
            p_safe = min(p_safe, 0.9)
            mid = sha(tea_id, day, "regular")
            rid = sha("rec", mid)[:32]
            readable = rng.random() > scan_rate
            r = rng.random()
            safety = "yes" if r < p_safe else "uncertain" if r < p_safe + 0.04 else "no"
            subs = []
            if safety == "yes":
                weights = {"armed_staff": 1 + 2 * (day.year >= 2018), "police_sro": 3, "hardening": 2 + 3 * (day.year >= 2022),
                           "threat_assessment": 1 + 2 * (day.year >= 2019), "drills_emergency": 3,
                           "mental_health": 1 + 3 * (day.year >= 2020)}
                subs = sorted(set(rng.choices(list(weights), weights=list(weights.values()), k=rng.randint(1, 3))))
            temper = "yes" if rng.random() < p_temper else "no"
            tone = rng.choices(["negative", "positive", "mixed"], [0.55, 0.35, 0.10])[0] if temper == "yes" else "none"
            who = rng.choice(["students", "students", "staff", "both"]) if temper == "yes" else "none"
            body = rng.sample(ROUTINE, 3)
            evidence = []
            if safety == "yes":
                for s in subs:
                    body.append(SAFETY_TEXT[s])
                    evidence.append({"passage_id": sha(mid, s), "measure": "safety", "excerpt": "(synthetic) " + SAFETY_TEXT[s]})
            if day > EVENTS["uvalde"][0] and dist["uvalde"] < 150:
                body.append("Trustees discussed the Uvalde tragedy, a safety audit and the new vestibule plan.")
            if temper == "yes":
                body.append(TEMPER[tone])
                evidence.append({"passage_id": sha(mid, "t"), "measure": "temperament", "excerpt": "(synthetic) " + TEMPER[tone]})
            text = " ".join(body) if readable else None
            records.append({"record_id": rid, "tea_id": tea_id, "school_district": name, "county_name": county,
                            "platform": "synthetic", "date": day, "meeting_type": "Regular",
                            "meeting_title": f"Regular meeting {day:%B %-d, %Y}", "meeting_id": mid, "detail": "Minutes",
                            "source_link": f"https://example.org/synthetic/{rid}", "text": text,
                            "text_status": "ok" if readable else "needs_ocr", "char_count": len(text or ""),
                            "preferred_for_meeting": True})
            if readable:
                for s in subs + (["t"] if temper == "yes" else []):
                    passage = SAFETY_TEXT.get(s) or TEMPER[tone]
                    start_at = text.find(passage)
                    passages.append({"passage_id": sha(mid, s), "meeting_id": mid, "record_id": rid, "tea_id": tea_id,
                                     "date": day, "detail": "Minutes", "start": start_at, "end": start_at + len(passage),
                                     "text": passage, "candidate_for": ["temperament"] if s == "t" else ["safety"],
                                     "matched_terms": [], "subtopic_hits": [] if s == "t" else [s], "excluded_hits": [],
                                     "definition_version": "synthetic-fixture"})
            meetings.append({
                "meeting_id": mid, "tea_id": tea_id, "school_district": name, "county_name": county, "county_fips": fips,
                "nces_id": p.get("nces_id"), "district_lat": lat, "district_lon": lon, "esc_region": region,
                "date": day, "date_source": "listing", "meeting_type": "Regular", "meeting_title": f"Regular meeting",
                "platforms": ["synthetic"], "record_ids": [rid], "best_record_id": rid, "best_detail": "Minutes",
                "has_minutes_text": readable, "has_agenda_text": False, "readable": readable,
                "text_chars": len(text or ""), "n_passages": len(subs), "closed_security_session": readable and rng.random() < 0.03 + 0.04 * (day.year >= 2019),
                "named_events": evkeys, "safety": safety if readable else "not_screened",
                "safety_prob": {"yes": 0.95, "no": 0.03, "uncertain": 0.5}[safety] if readable else None,
                "safety_subtopics": subs if readable else [], "safety_resolved_by": "rule",
                "temperament": temper if readable else "not_screened", "temperament_prob": 0.9 if temper == "yes" else 0.05,
                "temperament_tone": tone if readable else None, "temperament_who": who if readable else None,
                "event_reference": ("yes" if evkeys else "no") if readable else "not_screened",
                "event_keys": evkeys if readable else [], "evidence": evidence if readable else [],
                "definition_version": "synthetic-fixture", "label_version": "synthetic"})
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 12)
        coverage.append({"tea_id": tea_id, "school_district": name, "county_name": county, "platforms": "synthetic",
                         "first_meeting_date": first, "last_meeting_date": day, "longest_gap_days": 40,
                         "flags": "late_start" if start > 2018 else ""})
    pq.write_table(pa.Table.from_pylist(meetings, MEETING_SCHEMA), out / "measures" / "meetings.parquet")
    pq.write_table(pa.Table.from_pylist(passages, PASSAGE_SCHEMA), out / "measures" / "passages.parquet")
    pq.write_table(pa.Table.from_pylist(records, RECORD_SCHEMA), out / "records.parquet")
    pq.write_table(pa.Table.from_pylist(coverage), out / "coverage.parquet")
    (out / "gold" / "calibration.json").write_text(json.dumps({
        "_note": "synthetic", "n_meetings": 400, "definition_version": "synthetic-fixture",
        "questions": {"safety": {"precision": 0.91, "recall": 0.87, "n": 400, "uncertain_share": 0.06, "yes": 0.8, "no": 0.2},
                      "temperament": {"precision": 0.78, "recall": 0.70, "n": 400, "uncertain_share": 0.11, "yes": 0.8, "no": 0.2}}},
        indent=1))
    (out / "SYNTHETIC_FIXTURE").write_text("Synthetic data generated by code/app/fixture.py. Not real findings.\n")
    return {"meetings": len(meetings), "passages": len(passages), "records": len(records), "districts": len(districts)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    target = Path(args.out)
    if target.exists() and (target / "SYNTHETIC_FIXTURE").exists():
        shutil.rmtree(target)
    print(build(target, full=args.full))
    sys.exit(0)
