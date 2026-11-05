"""API tests for code/app against a small fake data directory."""
from __future__ import annotations

import base64
import csv
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent))

import server  # noqa: E402

LISTING = ["record_id", "tea_id", "school_district", "county_name", "platform", "date", "date_raw",
           "meeting_type", "meeting_title", "meeting_id", "detail", "detail_raw", "source_link",
           "listing_url", "listing_row_text"]


def listing(rid, date, detail="Minutes", tea_id="000001", name="Alpha ISD", county="Able County"):
    return {"record_id": rid, "tea_id": tea_id, "school_district": name, "county_name": county,
            "platform": "boardbook", "date": date, "date_raw": date, "meeting_type": "Regular",
            "meeting_title": f"Regular meeting {date}", "meeting_id": rid, "detail": detail,
            "detail_raw": detail, "source_link": f"https://example.org/{rid}",
            "listing_url": "https://example.org/list", "listing_row_text": f"Row for {rid}"}


def collected(row, text, status="ok", **extra):
    return {**row, "text": text, "text_status": status, "char_count": len(text or ""),
            "collected_at": "2026-11-05T00:00:00+00:00", "error": None, **extra}


def write_lines(path: Path, rows, tail: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.write(tail)


@pytest.fixture()
def data_dir(tmp_path):
    ref = tmp_path / "reference"
    ref.mkdir()
    with open(ref / "districts.csv", "w", newline="") as handle:
        w = csv.writer(handle)
        w.writerow(["tea_id", "school_district", "county_name", "county_fips", "website"])
        w.writerow(["000001", "Alpha ISD", "Able County", "48001", "https://alpha.example"])
        w.writerow(["000002", "Beta ISD", "Baker County", "48003", ""])
        w.writerow(["000003", "Gamma ISD", "Baker County", "48003", ""])
    # platforms.csv with a partially written last row
    (ref / "platforms.csv").write_text(
        "tea_id,school_district,platform,entry_url,evidence,note\n"
        "000001,Alpha ISD,boardbook,https://example.org/list,seen,\n"
        "000002,Beta ISD,none,,,\n"
        "000003,Gamma ISD,board")
    a = listing("a1", "2016-01-10")
    b = listing("b1", "2020-03-01")
    c = listing("c1", "2021-01-05", detail="Agenda")
    d = listing("d1", "2021-02-05", detail="Agenda")
    staging = tmp_path / "staging"
    write_lines(staging / "listings" / "000001__boardbook.jsonl", [a, b, c, d])
    (staging / "listings" / "000001__boardbook.status.json").write_text(json.dumps(
        {"tea_id": "000001", "platform": "boardbook", "records": 4, "error": None,
         "discovered_at": "2026-11-05T00:00:00+00:00"}))
    write_lines(staging / "records" / "000001__boardbook.jsonl", [
        collected(a, None, status="error", error="Timeout"),
        collected(b, "Budget <script>alert(1)</script> & school safety plan"),
        collected(a, "Final minutes text about the bond election"),   # retry: last line wins
    ], tail='{"record_id": "c1", "text": "half writ')                  # truncated tail
    return tmp_path


@pytest.fixture()
def app(data_dir, monkeypatch):
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    httpd = server.make_server(data_dir, port=0, run_background=False)
    httpd.store.refresh()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/"
    yield base, httpd
    httpd.shutdown()
    httpd.server_close()
    httpd.store.close()


def get(url, auth=None):
    req = urllib.request.Request(url)
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req) as res:
        return res.status, res.headers, res.read()


def get_json(url):
    return json.loads(get(url)[2])


def test_progress_counts_and_last_line_wins(app):
    base, _ = app
    p = get_json(base + "api/progress")
    assert p["districts_total"] == 3
    assert p["records_discovered"] == 4
    assert p["records_collected"] == 2           # a1 counted once despite the retry
    assert p["by_status"] == {"ok": 2}           # the error line was superseded
    assert p["records_backlog"] == 2
    assert p["platform_counts"]["boardbook"] == 1 and p["platform_counts"]["none"] == 1
    assert p["platform_counts"]["not mapped yet"] == 1  # Gamma's row was cut off mid-write


def test_truncated_tail_is_picked_up_once_complete(app, data_dir):
    base, httpd = app
    path = data_dir / "staging" / "records" / "000001__boardbook.jsonl"
    content = path.read_text()
    complete = collected(listing("c1", "2021-01-05", detail="Agenda"), "Agenda text")
    path.write_text(content[: content.rfind("\n") + 1] + json.dumps(complete) + "\n")
    httpd.store.refresh()
    assert get_json(base + "api/progress")["records_collected"] == 3


def test_record_dedupe_and_fields(app):
    base, _ = app
    rec = get_json(base + "api/record?id=a1")
    assert rec["text"] == "Final minutes text about the bond election"
    assert rec["text_status"] == "ok" and rec["error"] is None
    pending = get_json(base + "api/record?id=d1")
    assert pending["collected"] is False and pending["text_status"] == "pending"


def test_districts_flags_and_states(app):
    base, _ = app
    items = {d["tea_id"]: d for d in get_json(base + "api/districts")["items"]}
    alpha = items["000001"]
    assert alpha["discovered"] == 4 and alpha["collected"] == 2
    assert alpha["minutes_first"] == "2016-01-10" and alpha["minutes_last"] == "2020-03-01"
    assert "gap" in alpha["flags"] and "late_start" not in alpha["flags"]
    assert alpha["state"] == "collecting"
    assert items["000002"]["state"] == "no_platform"
    assert items["000003"]["state"] == "not_mapped"


def test_district_detail(app):
    base, _ = app
    d = get_json(base + "api/district?tea_id=000001")
    assert d["timeline"]["2021"] == {"Agenda": 2}
    assert d["platform_rows"][0]["platform"] == "boardbook"
    assert d["platform_rows"][0]["discovered"] == 4
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base + "api/district?tea_id=999999")
    assert err.value.code == 404


def test_records_filters_and_search(app):
    base, _ = app
    assert get_json(base + "api/records")["total"] == 4
    assert get_json(base + "api/records?detail=Agenda")["total"] == 2
    assert get_json(base + "api/records?text_status=pending")["total"] == 2
    assert get_json(base + "api/records?date_from=2020-01-01&date_to=2020-12-31")["total"] == 1
    hits = get_json(base + "api/records?q=SAFETY")
    assert [r["record_id"] for r in hits["items"]] == ["b1"]
    assert "safety" in hits["items"][0]["snippet"]
    assert get_json(base + "api/records?q=100%25")["items"] == []  # "%" is literal, not a wildcard


def test_record_text_is_served_as_data_not_html(app):
    base, _ = app
    status, headers, body = get(base + "api/record?id=b1")
    assert headers["Content-Type"].startswith("application/json")
    assert "<script>" in json.loads(body)["text"]           # stored verbatim
    status, headers, body = get(base + "api/record_text?id=b1")
    assert headers["Content-Type"].startswith("text/plain")
    assert headers["X-Content-Type-Options"] == "nosniff"
    # The UI renders text with textContent only; guard against innerHTML creeping in.
    js = (APP / "static" / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js


def test_export_csv(app):
    base, _ = app
    status, headers, body = get(base + "api/export.csv?detail=Minutes")
    rows = list(csv.reader(body.decode().splitlines()))
    assert headers["Content-Type"].startswith("text/csv")
    assert rows[0][0] == "record_id" and "text" not in rows[0]
    assert sorted(r[0] for r in rows[1:]) == ["a1", "b1"]


def test_static_assets_use_relative_urls(app):
    base, _ = app
    _, headers, body = get(base)
    html = body.decode()
    assert 'href="app.css"' in html and 'src="app.js"' in html
    assert 'href="/' not in html and 'src="/' not in html
    js = get(base + "app.js")[2].decode()
    assert "fetch(`api/" in js and 'fetch("/' not in js and "fetch(`/" not in js
    with pytest.raises(urllib.error.HTTPError) as err:
        get(base + "../server.py")
    assert err.value.code == 404


def test_basic_auth(data_dir):
    httpd = server.make_server(data_dir, port=0, password="s3cret", run_background=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}/"
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            get(base + "api/health")
        assert err.value.code == 401
        assert "Basic" in err.value.headers["WWW-Authenticate"]
        with pytest.raises(urllib.error.HTTPError):
            get(base + "api/health", auth="huda:wrong")
        assert get(base + "api/health", auth="huda:s3cret")[0] == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.store.close()


def test_missing_platforms_and_staging(tmp_path):
    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "districts.csv").write_text(
        "tea_id,school_district,county_name,county_fips,website\n000001,Alpha ISD,Able County,48001,\n")
    httpd = server.make_server(tmp_path, port=0, run_background=False)
    try:
        httpd.store.refresh()
        p = httpd.store.progress()
        assert p["platforms_file"] is False and p["records_discovered"] == 0
        assert httpd.store.districts()[0]["state"] == "not_mapped"
    finally:
        httpd.server_close()
        httpd.store.close()
