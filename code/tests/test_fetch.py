from types import SimpleNamespace

import fetch


class FakeSession:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        status = self.statuses.pop(0)
        response = SimpleNamespace(status_code=status, url=url, content=b"ok",
                                   headers={"Retry-After": "0"} if status == 429 else {})
        response.raise_for_status = lambda: None if status < 400 else (_ for _ in ()).throw(RuntimeError(status))
        return response


def test_429_slows_the_host_then_succeeds(tmp_path, monkeypatch):
    raw = tmp_path / "data" / "raw"
    fetcher = fetch.Fetcher(raw)
    session = FakeSession([429, 200])
    monkeypatch.setattr(fetcher, "_session", lambda: session)
    monkeypatch.setattr(fetch.time, "sleep", lambda seconds: None)
    response = fetcher.get("https://meetings.boardbook.org/Public/Organization/1")
    assert session.calls == 2
    assert response.content == b"ok"
    assert fetcher._backoff["meetings.boardbook.org"] > 1.0
    cached = fetcher.get("https://meetings.boardbook.org/Public/Organization/1")
    assert cached.from_cache and session.calls == 2
