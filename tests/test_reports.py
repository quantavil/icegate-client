import io
import zipfile
from datetime import date
from types import SimpleNamespace

import pytest

from icegate_client.enquiry import EnquiryClient
from icegate_client.jobstatus import JobStatusClient, parse_xlsx
from icegate_client.myiec import MyIecClient
from icegate_client.shared.errors import IcegateError


class FakeSession:
    settings = SimpleNamespace(icegate_id="TEST")
    role_id, user_type = 3, "organization"

    def __init__(self, result=None, content=b"", status=200, content_type="application/json"):
        self.result, self.content, self.status, self.content_type, self.calls = result, content, status, content_type, []

    def url(self, path):
        return "https://example.test" + path

    def auth_headers(self):
        return {"Authorization": "secret"}

    def send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return SimpleNamespace(status_code=self.status, json=lambda: self.result, content=self.content,
                               headers={"Content-Type": self.content_type})


def _xlsx(rows):
    strings, body = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = "ABCDEFGH"[c] + str(r)
            if value is None:
                continue
            if isinstance(value, str):
                strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        body.append(f'<row r="{r}">{"".join(cells)}</row>')
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData>{"".join(body)}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}">' + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
    return out.getvalue()


def test_parse_xlsx_keeps_empty_cells_and_whole_numbers():
    data = _xlsx([["SB No", "Status", "Error"], [6760213.0, "Sent to EDPMS", None]])
    assert parse_xlsx(data) == [{"SB No": "6760213", "Status": "Sent to EDPMS", "Error": ""}]


def test_parse_xlsx_rejects_non_xlsx():
    with pytest.raises(IcegateError):
        parse_xlsx(b"not a zip")


def test_job_status_posts_role_without_dates():
    session = FakeSession(content=_xlsx([["SB No"], [1]]), content_type="application/octet-stream")
    assert JobStatusClient(session).detailed() == [{"SB No": "1"}]
    method, url, kwargs = session.calls[0]
    assert (method, url.endswith("/dashboard/jobStatus/sbExcel")) == ("POST", True)
    assert kwargs["json_body"] == {"icegateId": "TEST", "roleId": 3, "userType": "organization"}


def test_job_status_rejects_http_error_and_json_error():
    with pytest.raises(IcegateError):
        JobStatusClient(FakeSession(status=500)).detailed()
    with pytest.raises(IcegateError):
        JobStatusClient(FakeSession(content=b"{}")).detailed()


def test_my_iec_lists_all_rows():
    rows = [{"locationCode": "INDER6", "iecName": "A", "iecCode": "0510012027"}]
    session = FakeSession(rows)
    assert MyIecClient(session).list() == rows
    _, url, kwargs = session.calls[0]
    assert url.endswith("/dashboard/myIec/getMyIecDetails/")
    assert kwargs["json_body"] == {"icegateId": "TEST", "roleId": 3, "iec": None, "locationCode": None}


def test_my_iec_rejects_unexpected_response():
    with pytest.raises(IcegateError):
        MyIecClient(FakeSession({"x": 1})).list()


def test_enquiry_is_public_and_returns_every_section():
    body = {"sbDetailsModel": [{"jobNo": "1"}], "drawbackQueryDetailsModel": []}
    session = FakeSession(body)
    assert EnquiryClient(session).shipping_bill(" INMBD6 ", "6817710", date(2026, 9, 14)) == body
    _, url, kwargs = session.calls[0]
    assert url.endswith("/enquiry/publicEnquiries/SBTrack_Ices_action_Public")
    assert kwargs["json_body"] == {"location": "INMBD6", "sbNo": "6817710", "sbDate": "20260914"}
    assert "headers" not in kwargs


def test_enquiry_rejects_bad_input_and_response():
    with pytest.raises(IcegateError):
        EnquiryClient(FakeSession({})).shipping_bill("", "1", date(2026, 9, 14))
    with pytest.raises(IcegateError):
        EnquiryClient(FakeSession([1])).shipping_bill("INMBD6", "1", date(2026, 9, 14))
    with pytest.raises(IcegateError):
        EnquiryClient(FakeSession({}, status=500)).shipping_bill("INMBD6", "1", date(2026, 9, 14))
