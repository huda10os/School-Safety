import build_dataset


def row(**values):
    base = dict(record_id="r1", tea_id="000001", school_district="Example ISD", county_name="X County",
                platform="boardbook", date="2021-01-28", date_raw="January 28, 2021", meeting_type="Regular",
                meeting_title="Called Meeting", meeting_id="1", detail="Agenda", detail_raw="Agenda",
                source_link="https://meetings.boardbook.org/Public/Agenda/1?meeting=1", listing_url="u",
                listing_row_text="", text_status="ok", error=None)
    base.update(values)
    return base


def test_video_links_in_a_listing_row_become_one_recording_per_meeting():
    text = "VIRTUAL MEETING LOCATION https://www.youtube.com/@RomaISDEducation/featured. Agenda Minutes"
    rows = [row(listing_row_text=text), row(record_id="r2", detail="Minutes", listing_row_text=text)]
    extra = build_dataset._recording_rows(rows)
    assert len(extra) == 1
    assert extra[0]["detail"] == "Recording"
    assert extra[0]["source_link"] == "https://www.youtube.com/@RomaISDEducation/featured"
    assert extra[0]["date_raw"] == "January 28, 2021" and extra[0]["text_status"] == "recording"


def test_skipped_media_file_is_relabelled_as_a_recording():
    rows = [row(detail="Other", text_status="unsupported", error="media file not downloaded (audio/wav)")]
    build_dataset._recording_rows(rows)
    assert rows[0]["detail"] == "Recording" and rows[0]["text_status"] == "recording"


def test_website_rows_are_redated_and_document_header_wins_when_far_off():
    stale = row(platform="website", detail="Minutes", detail_raw="Nov 10",
                source_link="https://x/11102025-WorkshopMeeting-Minutes.pdf",
                listing_row_text="Nov 10 | Board Named a 2026 Texas Top 5 Honor Board",
                date="2026-11-10", text="WORKSHOP MEETING\nNovember 10, 2025\nCall to order")
    build_dataset._redate(stale)
    assert stale["date"] == "2025-11-10" and stale["date_source"] == "link_or_filename"
    wrong = row(platform="website", detail="Agenda", detail_raw="Agenda", source_link="https://x/a.pdf",
                listing_row_text="Agenda | Board Agendas 2020", text="NOTICE OF REGULAR MEETING\nJanuary 27, 2022 at 6 PM")
    build_dataset._redate(wrong)
    assert wrong["date"] == "2022-01-27" and wrong["date_source"] == "document_text"
    listed = row()
    build_dataset._redate(listed)
    assert listed["date"] == "2021-01-28" and listed["date_source"] == "listing"


def test_ocr_sidecar_replaces_scanned_text(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(build_dataset, "STAGING", tmp_path)
    sha = "ab" + "0" * 62
    (tmp_path / "ocr" / "ab").mkdir(parents=True)
    (tmp_path / "ocr" / "ab" / f"{sha}.json").write_text(json.dumps(
        {"sha": sha, "text": "Minutes of Regular Meeting", "ocr_pages": 4, "mean_conf": 0.97}))
    scanned = row(text=None, text_status="needs_ocr", content_sha256=sha)
    build_dataset._apply_ocr(scanned)
    assert scanned["text"] == "Minutes of Regular Meeting" and scanned["text_status"] == "ok"
    assert scanned["text_method"] == "pdf+ocr" and scanned["ocr_pages"] == 4
