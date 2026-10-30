import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from icegate_client import cli
from icegate_client.ecopy import ECopyCache, ECopyClient
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


@pytest.mark.parametrize("kind", ["SB", "BE", "GATEPASS"])
def test_date_search_uses_portal_contract(kind):
    session = FakeSession([{"jobNo": "123"}])
    result = ECopyClient(session).search_by_date(kind, date(2026, 10, 27), date(2026, 10, 28))
    assert result == [{"jobNo": "123"}]
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/dashboard-utility/download-utility/searchByDate")
    assert kwargs["json_body"] == {"icegateId": "TEST", "documentType": {"GATEPASS": "LEOGate"}.get(kind, kind),
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


def test_download_ack_uses_filename_and_authenticated_request():
    session = FakeSession(None)
    def send(method, url, **kwargs):
        session.calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=200, content=b'acknowledgement', headers={'Content-Type': 'text/plain'})
    session.send = send
    assert ECopyClient(session).download_ack('record.ack') == b'acknowledgement'
    _, url, kwargs = session.calls[0]
    assert url.endswith('/dashboard-utility/download-utility/downloadAckFile')
    assert kwargs['json_body'] == {'icegateId': 'TEST', 'fileName': 'record.ack'}


def test_download_signed_uses_portal_irn_and_user_id():
    session = FakeSession(None)
    def send(method, url, **kwargs):
        session.calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=200, content=b'%PDF-1.7\nexample', headers={'Content-Type': 'application/pdf'})
    session.send = send
    assert ECopyClient(session).download_signed('1234567890123456', 'USER') == b'%PDF-1.7\nexample'
    _, url, kwargs = session.calls[0]
    assert url.endswith('/esanchit/getDocument')
    assert kwargs['json_body'] == {'irn': '1234567890123456', 'userId': 'USER'}


def test_search_remembers_user_id_for_later_download(tmp_path, monkeypatch):
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    monkeypatch.setattr(cli.Context, 'authenticate', lambda self: None)
    monkeypatch.setattr(ECopyClient, 'search_by_date', lambda *a: [{'irn': '123', 'userId': 'PORTAL-USER'}])
    monkeypatch.setattr(ECopyClient, 'download_signed', lambda self, irn, user_id:
                        b'%PDF-1.7' if (irn, user_id) == ('123', 'PORTAL-USER') else b'wrong')
    state = str(tmp_path / 'state')
    assert cli.main(['ecopy', 'sb', '--today', '--state-dir', state]) == 0
    output = tmp_path / 'result.pdf'
    assert cli.main(['ecopy', 'download', '--irn', '123', '-o', str(output), '--state-dir', state]) == 0
    assert output.read_bytes() == b'%PDF-1.7'


def test_search_cache_is_scoped_to_account(tmp_path):
    first = ECopyCache(tmp_path, 'FIRST')
    first.remember([{'irn': '123', 'userId': 'PORTAL-USER'}])
    assert first.user_id('123') == 'PORTAL-USER'
    assert ECopyCache(tmp_path, 'SECOND').user_id('123') is None
    assert first.user_id('missing') is None


def test_download_rejects_errors_and_non_pdf_signed_response():
    session = FakeSession(None)
    session.send = lambda *a, **kw: SimpleNamespace(status_code=200, content=b'{"error":"failure"}', headers={'Content-Type':'application/json'})
    with pytest.raises(IcegateError):
        ECopyClient(session).download_signed('1234567890123456', 'USER')
    with pytest.raises(IcegateError):
        ECopyClient(session).download_ack('record.ack')


def test_cli_download_signed_writes_private_pdf_without_overwriting(tmp_path, monkeypatch, capsys):
    import json
    import stat
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    monkeypatch.setattr(cli.Context, 'authenticate', lambda self: None)
    monkeypatch.setattr(ECopyClient, 'download_signed', lambda self, irn, user_id: b'%PDF-1.7\nexample')
    output = tmp_path / 'signed.pdf'
    args = ['ecopy', 'download', 'signed', '--irn', '1234567890123456', '--user-id', 'USER',
            '--output', str(output), '--json', '--state-dir', str(tmp_path / 'state')]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['bytes'] == len(output.read_bytes())
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert cli.main(args) == 1
    assert output.read_bytes() == b'%PDF-1.7\nexample'


def test_download_rejects_portal_http_error_without_retry():
    session = FakeSession(None, status=500)
    with pytest.raises(IcegateError, match='HTTP 500'):
        ECopyClient(session).download_ack('record.ack')
    assert len(session.calls) == 1


def test_download_cli_does_not_create_file_after_portal_error(tmp_path, monkeypatch):
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    monkeypatch.setattr(cli.Context, 'authenticate', lambda self: None)
    def fail(self, filename):
        raise IcegateError('e-copy download failed with HTTP 409')
    monkeypatch.setattr(ECopyClient, 'download_ack', fail)
    output = tmp_path / 'record.ack'
    assert cli.main(['ecopy', 'download', 'ack', '--filename', 'record.ack', '--output', str(output),
                     '--state-dir', str(tmp_path / 'state')]) == 1
    assert not output.exists()


def test_download_409_says_portal_is_still_retrieving():
    with pytest.raises(IcegateError, match="retry in about 60 minutes"):
        ECopyClient(FakeSession({}, status=409)).download_signed("1234567890123456", "USER")
