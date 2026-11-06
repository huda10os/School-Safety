from argparse import Namespace

import pipeline


def test_jobs_merge_entry_urls_per_district_platform(tmp_path, monkeypatch):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "districts.csv").write_text(
        "tea_id,school_district,county_name,county_fips,website,tea_source\n"
        "001906,Neches ISD,Anderson County,48001,https://www.nechesisd.com,x\n")
    platforms = reference / "platforms.csv"
    platforms.write_text(
        "tea_id,platform,entry_url,evidence\n"
        "001906,website,https://www.nechesisd.com/school_board,a\n"
        "001906,website,https://www.nechesisd.com/school_board_archives,b\n"
        "001906,website,https://www.nechesisd.com/school_board,duplicate\n"
        "001906,none,,c\n")
    monkeypatch.setattr(pipeline, "REFERENCE", reference)
    args = Namespace(platforms=str(platforms), tea_ids=None, platform=None)
    selected = pipeline.jobs(args)
    assert len(selected) == 1
    district, job = selected[0]
    assert district["school_district"] == "Neches ISD"
    assert job["entry_urls"] == ["https://www.nechesisd.com/school_board",
                                 "https://www.nechesisd.com/school_board_archives"]


def test_pending_records_yield_minutes_before_agendas(tmp_path, monkeypatch):
    import json
    listings, records = tmp_path / "listings", tmp_path / "records"
    listings.mkdir()
    monkeypatch.setattr(pipeline, "LISTINGS", listings)
    monkeypatch.setattr(pipeline, "RECORDS", records)
    rows = []
    for tea_id, details in (("000001", ["Agenda", "Minutes"]), ("000002", ["Agenda", "Minutes"])):
        lines = [json.dumps({"record_id": f"{tea_id}-{d}", "detail": d}) for d in details]
        (listings / f"{tea_id}__boardbook.jsonl").write_text("\n".join(lines) + "\n")
        rows.append(({"tea_id": tea_id}, {"platform": "boardbook"}))
    order = [record["record_id"] for _, record in pipeline.pending_records(rows)]
    assert order == ["000001-Minutes", "000002-Minutes", "000001-Agenda", "000002-Agenda"]


def test_jobs_drop_sources_on_the_skip_list(tmp_path, monkeypatch):
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "districts.csv").write_text(
        "tea_id,school_district,county_name,county_fips,website,tea_source\n057905,Dallas ISD,Dallas County,48113,x,x\n")
    (reference / "platforms.csv").write_text(
        "tea_id,platform,entry_url,evidence\n057905,website,https://hang.example/live,a\n"
        "057905,diligent,https://d.example/Portal/,b\n")
    (reference / "skip_sources.csv").write_text(
        "tea_id,platform,entry_url,reason\n057905,website,https://hang.example/live,hangs\n")
    monkeypatch.setattr(pipeline, "REFERENCE", reference)
    args = Namespace(platforms=str(reference / "platforms.csv"), tea_ids=None, platform=None)
    assert [job["platform"] for _, job in pipeline.jobs(args)] == ["diligent"]
