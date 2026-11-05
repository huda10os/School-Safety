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
