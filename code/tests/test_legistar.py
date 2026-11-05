import json
from pathlib import Path
from types import SimpleNamespace

from platforms import legistar

FIXTURE = Path(__file__).parent / "fixtures" / "legistar" / "events.json"
DISTRICT = {"tea_id": "123456", "school_district": "Example ISD", "county_name": "Example"}


def test_pagination_and_files(monkeypatch):
    monkeypatch.setattr(legistar, "PAGE_SIZE", 2)
    events = json.loads(FIXTURE.read_text())
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(json=lambda: events if len(calls) == 1 else [])
    rows = legistar.discover(DISTRICT, "https://houstonisd.legistar.com/Calendar.aspx", SimpleNamespace(get=get))
    assert [row["detail"] for row in rows] == ["Agenda", "Minutes"]
    assert rows[0]["date"] == "2023-03-02"
    assert "$skip=2" in calls[1][0] and calls[0][1] == {"refresh": True}


def test_resolve_binary():
    response = SimpleNamespace(url="https://example.org/a.pdf", headers={"Content-Type": "application/pdf"},
        content=b"%PDF-", raw_path="data/raw/a.gz", sha256="abc")
    result = legistar.resolve({"source_link": response.url}, SimpleNamespace(get=lambda url: response))
    assert result["raw_path"] == "data/raw/a.gz" and "text" not in result
