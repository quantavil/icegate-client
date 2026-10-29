from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from icegate_client import cli
from icegate_client.ecopy import ECopyClient
from icegate_client.shared.errors import IcegateError


class FakeSession:
    settings = SimpleNamespace(icegate_id="TEST")

    def __init__(self, result, status=200):
        self.result, self.status, self.calls = result, status, []

    def url(self, path):
        return "https://example.test" + path

    def auth_headers(self):
        return {"Authorization": "secret"}

    def send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=self.status, json=lambda: self.result)


@pytest.mark.parametrize("kind", ["SB", "BE"])
def test_date_search_uses_portal_contract(kind):
    session = FakeSession([{"jobNo": "123"}])
    result = ECopyClient(session).search_by_date(kind, date(2026, 10, 27), date(2026, 10, 28))
    assert result == [{"jobNo": "123"}]
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/dashboard-utility/download-utility/searchByDate")
    assert kwargs["json_body"] == {"icegateId": "TEST", "documentType": kind,
                                    "fromDate": "2026-10-27T00:00:00.000Z",
                                    "toDate": "2026-10-28T00:00:00.000Z"}


def test_job_search_uses_portal_contract():
    session = FakeSession([])
    assert ECopyClient(session).search_by_job("BE", "INMBD6", "123", date(2026, 10, 28)) == []
    _, url, kwargs = session.calls[0]
    assert url.endswith("/dashboard-utility/download-utility/searchByJobNumber")
    assert kwargs["json_body"] == {"icegateId": "TEST", "documentType": "BE",
                                    "location": "INMBD6", "jobNumber": "123",
                                    "jobDate": "2026-10-28T00:00:00.000Z"}


def test_rejects_unexpected_response_and_invalid_range():
    session = FakeSession({"error": "not a list"})
    with pytest.raises(IcegateError):
        ECopyClient(session).search_by_date("SB", date(2026, 10, 28), date(2026, 10, 28))
    with pytest.raises(IcegateError):
        ECopyClient(session).search_by_date("SB", date(2026, 10, 29), date(2026, 10, 28))


def test_cli_prints_returned_be_records_as_json(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    def authenticate(ctx):
        ctx.session.send = FakeSession([{"jobNumber": "123", "docNumber": "456"}]).send
    monkeypatch.setattr(cli.Context, "authenticate", authenticate)
    assert cli.main(["ecopy", "search", "BE", "--from", "2026-10-27", "--to", "2026-10-28",
                     "--json", "--state-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == [{"jobNumber": "123", "docNumber": "456"}]


@pytest.mark.parametrize("flag,offset", [("--today", 0), ("--yesterday", 1), ("--yestarday", 1)])
def test_cli_relative_date_flags(tmp_path, monkeypatch, flag, offset):
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    session = FakeSession([])
    def authenticate(ctx):
        ctx.session.send = session.send
    monkeypatch.setattr(cli.Context, "authenticate", authenticate)
    assert cli.main(["ecopy", "search", "SB", flag, "--state-dir", str(tmp_path)]) == 0
    expected = (datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=offset)).isoformat()
    body = session.calls[0][2]["json_body"]
    assert body["fromDate"] == body["toDate"] == expected + "T00:00:00.000Z"


def test_relative_date_cannot_be_combined_with_explicit_range(tmp_path, monkeypatch):
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    assert cli.main(["ecopy", "search", "SB", "--today", "--from", "2026-10-27",
                     "--to", "2026-10-28", "--state-dir", str(tmp_path)]) != 0
