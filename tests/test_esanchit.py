from __future__ import annotations

import pytest
from conftest import (
    EMPTY_HOME_HTML,
    FakeResponse,
    ScriptedSessionMixin,
    staged_html,
)

from icegate_client.esanchit.client import (
    FORM_ACTION_ALLOWLIST,
    EsanchitClient,
    assert_action_allowed,
)
from icegate_client.esanchit.page import HomePage, split_option
from icegate_client.shared.errors import HandoffError, StagingError
from icegate_client.shared.session import IcegateSession


class FakeSession(ScriptedSessionMixin, IcegateSession):
    pass


@pytest.fixture
def session(settings, journal):
    return FakeSession(settings, journal)


@pytest.fixture
def client(session, journal):
    return EsanchitClient(session, journal)


# --------------------------------------------------------------------- parsing
def test_parse_staged_page_reads_input_values():
    page = HomePage.parse(staged_html())
    assert page.tab_id == "TAB0123456789"
    assert len(page.rows) == 1
    row = page.rows[0]
    assert row.file_name == "invoice.pdf"
    assert row.drn == ""
    assert row.irn == ""
    assert row.start_date == "2026-10-28 21:39:15"
    assert row.sign_status == ""
    assert not row.verified
    assert row.select_id == "docTypeSelect_0"
    assert row.select_name == "detailBean[0].docTyp"
    assert row.doc_type_code == ""
    assert row.doc_type_label == "--- Select ---"
    assert not row.has_document_type
    assert len(row.options) == 3


def test_parse_empty_page_has_no_rows():
    page = HomePage.parse(EMPTY_HOME_HTML)
    assert page.rows == []
    assert page.tab_id == "TAB0123456789"
    assert page.max_file_bytes == 2 * 1024 * 1024
    assert page.max_files == 5


def test_parse_rows_populated_by_submit():
    page = HomePage.parse(staged_html(drn="DRN0001", irn="IRN0001", signature="Verified"))
    row = page.rows[0]
    assert row.drn == "DRN0001"
    assert row.irn == "IRN0001"
    assert row.verified


def test_parse_errors():
    html = staged_html().replace(
        '<div id="docs_error"></div>',
        '<div id="docs_error">Digital signature validation failed</div>',
    )
    page = HomePage.parse(html)
    assert page.errors
    assert "Digital signature" in page.errors[0]


def test_describe_limits():
    assert HomePage.parse(staged_html()).describe_limits() == "2 MB per file, 5 files per DRN"
    assert HomePage.parse("<html></html>").describe_limits() == "not advertised"


# --------------------------------------------------------------- option format
@pytest.mark.parametrize(
    "value,label,expected",
    [
        ("101000-Registration Document", "101000-Registration Document", ("101000", "Registration Document")),
        ("331000-Commercial invoice", "", ("331000", "Commercial invoice")),
        ("NONE", "--- Select ---", ("", "--- Select ---")),
        ("", "--Select--", ("", "--Select--")),
        ("77", "Seventy Seven", ("77", "Seventy Seven")),
    ],
)
def test_split_option(value, label, expected):
    assert split_option(value, label) == expected


def test_option_for_matches_code():
    row = HomePage.parse(staged_html()).rows[0]
    assert row.option_for("101000") == (
        "101000-Registration Document",
        "101000-Registration Document",
    )
    assert row.option_for("999999") is None


# ---------------------------------------------------------------- allowlisting
def test_allowlist_is_upload_only():
    assert FORM_ACTION_ALLOWLIST == {"uploadDocs"}


def test_allowlist_accepts_upload():
    assert assert_action_allowed("uploadDocs") == "uploadDocs"
    assert assert_action_allowed("/esanchit/uploadDocs") == "uploadDocs"
    assert assert_action_allowed("/esanchit/uploadDocs?x=1") == "uploadDocs"


@pytest.mark.parametrize(
    "action",
    ["submitDocs", "/esanchit/submitDocs", "finalSubmit", "submit", ""],
)
def test_allowlist_rejects_submission(action):
    with pytest.raises(StagingError):
        assert_action_allowed(action)


@pytest.mark.parametrize(
    "action", ["updateDocType", "saveDocType", "deleteEverything", "home"]
)
def test_allowlist_rejects_everything_else(action):
    with pytest.raises(StagingError):
        assert_action_allowed(action)


# --------------------------------------------------------------------- upload
def test_stage_posts_only_to_uploaddocs(client, session, signed_pdf):
    session._install([FakeResponse(200, text=staged_html())])
    result = client.stage([signed_pdf], page=HomePage.parse(EMPTY_HOME_HTML))

    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/esanchit/uploadDocs")
    assert kwargs["data"]["tabId"] == "TAB0123456789"
    assert kwargs["data"]["uploadForm"] == "Upload"
    assert kwargs["files"][0][0] == "docs"
    assert kwargs["files"][0][1][0] == "invoice.pdf"
    assert result.rows[0].file_name == "invoice.pdf"


def test_stage_requires_tab_id(client, session, signed_pdf):
    page = HomePage.parse(EMPTY_HOME_HTML)
    page.tab_id = None
    session._install([])
    with pytest.raises(StagingError):
        client.stage([signed_pdf], page=page)


def test_stage_surfaces_page_errors(client, session, signed_pdf):
    bad = staged_html().replace(
        '<div id="docs_error"></div>',
        '<div id="docs_error">File signature invalid</div>',
    )
    session._install([FakeResponse(200, text=bad)])
    with pytest.raises(StagingError):
        client.stage([signed_pdf], page=HomePage.parse(EMPTY_HOME_HTML))


def test_stage_follows_redirect_and_upgrades_to_https(client, session, signed_pdf):
    session._install(
        [
            FakeResponse(
                302,
                headers={"Location": "http://www.icegate.gov.in/esanchit/home"},
            ),
            FakeResponse(200, text=staged_html()),
        ]
    )
    result = client.stage([signed_pdf], page=HomePage.parse(EMPTY_HOME_HTML))
    assert result.rows[0].file_name == "invoice.pdf"
    assert session.calls[1][0] == "GET"
    assert session.calls[1][1].startswith("https://www.icegate.gov.in/")


def test_stage_rejects_body_replay(client, session, signed_pdf):
    session._install(
        [
            FakeResponse(
                307,
                headers={"Location": "https://www.icegate.gov.in/esanchit/home"},
            ),
        ]
    )
    with pytest.raises(StagingError):
        client.stage([signed_pdf], page=HomePage.parse(EMPTY_HOME_HTML))


def test_stage_rejects_offsite_redirect(client, session, signed_pdf):
    session._install(
        [FakeResponse(302, headers={"Location": "https://evil.example/esanchit/home"})]
    )
    with pytest.raises(StagingError):
        client.stage([signed_pdf], page=HomePage.parse(EMPTY_HOME_HTML))


# -------------------------------------------------------------------- handoff
def test_open_from_ticket_upgrades_http_and_redacts(client, session, journal):
    session._install(
        [
            FakeResponse(
                302,
                headers={"Location": "http://www.icegate.gov.in/esanchit/?ticket=SECRETTICKET"},
            ),
            FakeResponse(302, headers={"Location": "/esanchit/home"}),
            FakeResponse(200, text=staged_html()),
        ]
    )
    page = client.open_from_ticket(
        "https://foservices.icegate.gov.in/esanchit?ticket=SECRETTICKET"
    )
    assert page.tab_id == "TAB0123456789"
    assert session.calls[1][1].startswith("https://www.icegate.gov.in/")
    written = journal.path.read_text(encoding="utf-8")
    assert "SECRETTICKET" not in written


def test_open_from_ticket_rejects_foreign_host(client, session):
    session._install([])
    with pytest.raises(HandoffError):
        client.open_from_ticket("https://evil.example/esanchit?ticket=x")


def test_open_from_ticket_rejects_missing_upload_page(client, session):
    session._install([FakeResponse(200, text="<html>login</html>")])
    with pytest.raises(HandoffError):
        client.open_from_ticket("https://www.icegate.gov.in/esanchit?ticket=x")


def test_home_reports_idle_timeout(client, session):
    session._install([FakeResponse(500, text="error")])
    with pytest.raises(HandoffError):
        client.home()


def test_parse_request_cap_from_the_page_js():
    """The per-request cap exists only in the JS, in one branch per user class."""
    html = (
        "<html><body><script>"
        "if (upoadSize >\n52428800\n&& user === 'PGA') { return 4; }"
        "if (upoadSize >\n10485760\n&& user === 'OTHERS') { return 4; }"
        "</script></body></html>"
    )
    page = HomePage.parse(html)
    assert page.max_request_bytes == 10485760
    assert "10 MB per request" in page.describe_limits()


def test_parse_page_without_request_cap_leaves_it_unset():
    page = HomePage.parse(staged_html())
    assert page.max_request_bytes is None
    assert "per request" not in page.describe_limits()
