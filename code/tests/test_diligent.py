import json
from pathlib import Path
from types import SimpleNamespace

from platforms import diligent

FIXTURES = Path(__file__).parent / "fixtures" / "diligent"
DISTRICT = {"tea_id": "123456", "school_district": "Example ISD", "county_name": "Example"}


class Fetcher:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        name = "documents.json" if "meetingDocuments" in url else "meetings.json"
        data = json.loads((FIXTURES / name).read_text())
        return SimpleNamespace(json=lambda: data)


def test_public_documents_and_placeholder_date():
    fetcher = Fetcher()
    rows = diligent.discover(DISTRICT, "https://example.org/Portal/", fetcher)
    assert [row["detail"] for row in rows] == ["Agenda", "Extras", "Minutes"] * 2
    assert rows[0]["source_link"] == "https://example.org/document/10"
    assert rows[0]["date"] == "2024-05-01"
    assert rows[3]["date"] is None and rows[3]["date_raw"] == "2001-01-01"
    assert fetcher.calls[0][1] == {"refresh": True}
    assert len(fetcher.calls) == 3


def test_resolve_html():
    response = SimpleNamespace(url="https://example.org/document/10", headers={"Content-Type": "text/html"},
        content=b"<p>Agenda</p><script>bad</script>", text="<p>Agenda</p><script>bad</script>",
        raw_path="data/raw/a.gz", sha256="abc")
    result = diligent.resolve({"source_link": response.url}, SimpleNamespace(get=lambda url: response))
    assert result["text"] == "Agenda" and result["text_method"] == "html"
