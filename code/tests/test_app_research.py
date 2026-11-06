"""Research API tests against the synthetic fixture (code/app/fixture.py)."""
from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pyarrow.parquet as pq
import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP.parent))

import fixture  # noqa: E402
import research  # noqa: E402
import server  # noqa: E402


@pytest.fixture(scope="module")
def fixture_dir(tmp_path_factory):
    out = tmp_path_factory.mktemp("research")
    fixture.build(out)
    return out


def serve(data_dir):
    httpd = server.make_server(data_dir, port=0, run_background=False)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


@pytest.fixture(scope="module")
def app(fixture_dir):
    httpd, base = serve(fixture_dir)
    yield base
    httpd.shutdown()
    httpd.server_close()
    httpd.store.close()
    httpd.research.close()


def get(url):
    with urllib.request.urlopen(url) as res:
        return res.status, res.headers, res.read()


def js(url):
    return json.loads(get(url)[2])


def status_of(url):
    with pytest.raises(urllib.error.HTTPError) as err:
        get(url)
    return err.value.code


def rows_csv(body: bytes):
    return list(csv.DictReader(io.StringIO(body.decode())))


def test_meta_reports_fixture_and_events(app):
    m = js(app + "api/meta")
    assert m["measures"] is True and m["synthetic"] is True
    assert {"uvalde", "santa_fe"} <= {e["key"] for e in m["events"]}
    uv = next(e for e in m["events"] if e["key"] == "uvalde")
    assert uv["in_texas"] and uv["date"] == "2022-05-24"
    assert "armed_staff" in m["subtopics"]
    assert m["chip"]["districts"] == 8 and m["chip"]["meetings"] > 0


def test_trend_shares_denominators_and_csv(app):
    t = js(app + "api/trend?grain=year&from=2014&to=2026")
    assert t["rows"] and all(0 <= r["share"] <= 1 and r["lo"] <= r["share"] <= r["hi"] for r in t["rows"])
    assert sum(r["meetings"] for r in t["rows"]) == t["chip"]["meetings"]
    sub = {r["period"]: r["yes"] for r in js(app + "api/trend?grain=year&subtopic=hardening")["rows"]}
    assert all(sub[r["period"]] <= r["yes"] for r in t["rows"])
    _, headers, body = get(app + "api/trend?grain=year&format=csv&subtopic=hardening")
    assert headers["Content-Type"].startswith("text/csv")
    rows = rows_csv(body)
    assert rows and rows[0]["measure"].startswith("Building security") and rows[0]["sample_rule"] == "all"
    stable = js(app + "api/trend?grain=year&from=2015&to=2025&sample=stable")
    assert stable["chip"]["stable_districts"] == 8


def test_bad_parameters_are_400(app):
    assert status_of(app + "api/trend?grain=week") == 400
    assert status_of(app + "api/trend?subtopic=nope") == 400
    assert status_of(app + "api/event?key=nope") == 404
    assert status_of(app + "api/kwic?key=uvalde&word=a%20b") == 400
    assert status_of(app + "api/search?q=ab") == 400


def test_event_nearby_rules_and_balanced_sample(app):
    e = js(app + "api/event?key=uvalde")
    names = {d["school_district"] for d in e["districts"]}
    assert names == {"Uvalde CISD", "Knippa ISD", "Sabinal ISD"}
    near = e["summary"]["nearby"]
    assert near["districts_total"] == 3 and near["districts_qualifying"] == 3
    assert near["after"] > near["before"]                 # the fixture adds a nearby rise
    assert all(-12 <= r["rel"] <= 11 for g in e["series"].values() for r in g)
    assert sum(r["meetings"] for r in e["series"]["nearby"]) == near["meetings_before"] + near["meetings_after"]
    assert e["pretrend"] and len(e["pretrend"]) == 24
    small = js(app + "api/event?key=uvalde&radius=5")
    assert [d["school_district"] for d in small["districts"]] == ["Uvalde CISD"]
    county = js(app + "api/event?key=santa_fe&same_county=1")
    assert {d["school_district"] for d in county["districts"]} == {"Santa Fe ISD", "Dickinson ISD"}
    outside = js(app + "api/event?key=parkland")
    assert outside["districts"] == [] and outside["summary"]["comparison"]["districts_total"] == 8
    rows = rows_csv(get(app + "api/event?key=uvalde&format=csv")[2])
    assert {r["group"] for r in rows} == {"nearby", "comparison"} and rows[0]["nearby_rule"] == "within 50 miles"


def test_events_summary_rows(app):
    s = js(app + "api/events_summary")
    keys = {r["key"]: r for r in s["rows"]}
    assert keys["uvalde"]["group"] == "nearby" and keys["uvalde"]["districts"] == 3
    assert keys["parkland"]["group"] == "all Texas"
    assert keys["uvalde"]["change"] == pytest.approx(keys["uvalde"]["after"] - keys["uvalde"]["before"])


def test_log_odds_direction_and_shrinkage():
    scores = research.log_odds({"guardian": 30, "the": 500}, {"guardian": 5, "the": 500}, 1000, 1000)
    assert scores["guardian"][1] > 2 > abs(scores["the"][1])
    rare = research.log_odds({"x": 2, "y": 998}, {"x": 1, "y": 999}, 1000, 1000)
    assert abs(rare["x"][1]) < 1                          # the prior shrinks tiny counts


def test_event_words_and_kwic(app):
    w = js(app + "api/event_words?key=uvalde&scope=all")
    words = [x["word"] for x in w["words_after"]]
    assert "uvalde" in words and all(x["z"] > 0 for x in w["words_after"])
    assert w["docs_after"] > 0 and w["docs_before"] > 0
    k = js(app + "api/kwic?key=uvalde&word=uvalde&scope=all")
    assert k["items"] and all(i["match"].lower() == "uvalde" and i["date"] > "2022-05-24" for i in k["items"])
    assert k["items"][0]["source_link"].startswith("https://example.org/")
    rows = rows_csv(get(app + "api/event_words?key=uvalde&scope=all&format=csv")[2])
    assert {r["direction"] for r in rows} <= {"more_after", "more_before"}


def test_board_and_meetings(app):
    b = js(app + "api/board?tea_id=232903")
    s = b["summary"]
    assert s["meetings"] == len(b["meetings"]) and s["yes"] == sum(m["hit"] for m in b["meetings"])
    assert s["scans"] == len(b["scan_dates"]) > 0
    assert {e["key"] for e in b["events"]} >= {"uvalde", "santa_fe"}
    assert b["strip"][0]["year"] == 2014
    page = js(app + "api/board_meetings?tea_id=232903&only=safety&limit=5")
    assert page["total"] == s["yes"] and len(page["items"]) == 5
    assert all(it["safety"] == "yes" for it in page["items"])
    assert page["items"][0]["evidence"][0]["excerpt"].startswith("(synthetic)")
    assert status_of(app + "api/board?tea_id=999999") == 404


def test_map_and_coverage_grid(app):
    m = js(app + "api/map?from=2015&to=2026&min=12")
    assert len(m["districts"]) == 8 and all(d["lat"] and d["passes"] for d in m["districts"])
    assert all(0 <= d["share"] <= 1 for d in m["districts"])
    g = js(app + "api/coverage_grid")
    assert len(g["districts"]) == 8 and len(g["districts"][0]["cells"]) == len(g["years"])
    assert set("".join(d["cells"] for d in g["districts"])) <= set("RSU.")


def test_search_passages_and_full_text(app):
    p = js(app + "api/search?q=vestibule")
    assert p["scope"] == "passages" and p["items"] and all("vestibule" in i["snippet"].lower() for i in p["items"])
    f = js(app + "api/search?q=vestibule&scope=full&tea_id=232903&from=2023&to=2023")
    assert f["items"] and all(i["tea_id"] == "232903" and i["date"].startswith("2023") for i in f["items"])
    t = js(app + "api/search?q=anxious&scope=passages&tone=positive")
    assert t["items"] == []                               # tone filters by the meeting's tone label
    assert js(app + "api/search?q=anxious&scope=passages&tone=negative")["items"]


def test_method_definitions_assumptions_calibration(app):
    m = js(app + "api/method")
    assert m["definitions"]["version"] and m["calibration"]["questions"]["safety"]["precision"] == 0.91
    assert any(a["path"].startswith("safety") for a in m["assumptions"])
    assert all("ASSUMPTION" not in a["text"][:12] for a in m["assumptions"])


def test_panel_exports(app, fixture_dir):
    _, headers, body = get(app + "api/panel?format=csv&window=12&radius=50")
    rows = rows_csv(body)
    assert headers["Content-Disposition"].endswith('panel.csv"')
    uv = [r for r in rows if r["event_key"] == "uvalde"]
    assert {r["within_radius"] for r in uv if r["school_district"] == "Uvalde CISD"} == {"true"}
    assert {r["within_radius"] for r in uv if r["school_district"] == "Amarillo ISD"} == {"false"}
    assert all(-12 <= int(r["rel_month"]) <= 11 for r in uv)
    _, _, pbody = get(app + "api/panel?format=parquet")
    path = fixture_dir / "panel-test.parquet"
    path.write_bytes(pbody)
    table = pq.read_table(path)
    assert table.num_rows == len(rows) and "safety_subtopics" in table.column_names
    _, _, mbody = get(app + "api/file?name=meetings")
    assert mbody[:4] == b"PAR1"
    assert status_of(app + "api/file?name=records") == 404


def test_no_measures_mode_and_pickup(tmp_path, monkeypatch):
    fixture.build(tmp_path)
    measures = tmp_path / "measures"
    hold = tmp_path / "held"
    shutil.move(measures, hold)
    monkeypatch.setattr(research, "CHECK_SECONDS", 0)
    httpd, base = serve(tmp_path)
    try:
        m = js(base + "api/meta")
        assert m["measures"] is False and m["chip"]["meetings"] > 0
        t = js(base + "api/trend?grain=year")
        assert t["rows"] and all(r["share"] is None and r["meetings"] > 0 for r in t["rows"])
        e = js(base + "api/event?key=uvalde")
        assert e["summary"]["nearby"]["before"] is None and e["summary"]["nearby"]["districts_qualifying"] == 3
        assert js(base + "api/story")["subtopics"] is None
        w = js(base + "api/event_words?key=uvalde")
        assert w["scope"] == "all"
        html = get(base)[2].decode()
        assert "Safety and temperament measures are not computed yet." in html
        shutil.move(hold, measures)                       # the measures file appears while serving
        assert js(base + "api/meta")["measures"] is True
        assert js(base + "api/trend?grain=year")["rows"][0]["share"] is not None
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.store.close()
        httpd.research.close()


def test_partial_measures_hide_shares(tmp_path, monkeypatch):
    fixture.build(tmp_path)
    path = tmp_path / "measures" / "meetings.parquet"
    table = pq.read_table(path)
    safety = table.column("safety").to_pylist()
    # Leave most meetings unscreened, as a labelling run in progress does.
    partial = ["not_screened" if i % 4 else v for i, v in enumerate(safety)]
    pq.write_table(table.set_column(table.schema.get_field_index("safety"), "safety",
                                    __import__("pyarrow").array(partial)), path)
    httpd, base = serve(tmp_path)
    try:
        m = js(base + "api/meta")
        assert m["measures_file"] is True and m["measures_status"] == "partial" and m["measures"] is False
        assert 0 < m["screening"]["share"] < research.MIN_SCREENED
        assert all(r["share"] is None for r in js(base + "api/trend?grain=year")["rows"])
    finally:
        httpd.shutdown()
        httpd.server_close()
        httpd.store.close()
        httpd.research.close()


def test_static_research_assets(app):
    html = get(app)[2].decode()
    for name in ("research.css", "charts.js", "research.js"):
        assert name in html and f'"/{name}' not in html
    assert "<noscript><section" in html and "<script" not in html.split("<noscript>")[1].split("</noscript>")[0]
    for name in ("charts.js", "research.js"):
        src = get(app + name)[2].decode()
        assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "outerHTML" not in src
        assert 'fetch("/' not in src and "fetch(`/" not in src and 'href: "/' not in src
    _, headers, body = get(app + "fonts/newsreader.woff2")
    assert headers["Content-Type"] == "font/woff2" and body[:4] == b"wOF2"
    outline = js(app + "texas.json")
    assert outline["rings"] and "Census" in outline["source"]


def test_fixture_refuses_real_data_dir():
    with pytest.raises(SystemExit):
        fixture.build(fixture.ROOT / "data")
