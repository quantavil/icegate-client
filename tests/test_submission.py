from __future__ import annotations

import hashlib
import json
import stat

import pytest
from conftest import (
    EMPTY_HOME_HTML,
    SIGNED_PDF,
    FakeResponse,
    ScriptedSessionMixin,
    staged_html,
)
from filelock import FileLock

from icegate_client.esanchit.client import EsanchitClient
from icegate_client.esanchit.page import HomePage, StagedRow
from icegate_client.esanchit.submission import (
    SUBMIT_ALLOWLIST,
    SubmissionService,
    SubmitOutcome,
    SubmitPlan,
    _write_private,
)
from icegate_client.shared.errors import (
    DuplicateSubmit,
    SubmitAborted,
    SubmitBusy,
    SubmitError,
)
from icegate_client.shared.journal import Journal
from icegate_client.shared.session import IcegateSession


class FakeSession(ScriptedSessionMixin, IcegateSession):
    pass


@pytest.fixture
def session(settings, journal):
    return FakeSession(settings, journal)


@pytest.fixture
def client(session, journal):
    return EsanchitClient(session, journal)


@pytest.fixture
def service(client, journal, settings):
    return SubmissionService(client, journal, settings.state_dir)


@pytest.fixture
def page():
    return HomePage.parse(EMPTY_HOME_HTML)


def _plan(paths, code="101000"):
    return SubmitPlan.build(list(paths), {path.name: code for path in paths})


# ---------------------------------------------------------------------- plans
def test_plan_id_is_stable(signed_pdf):
    first = _plan([signed_pdf])
    second = _plan([signed_pdf])
    assert first.plan_id == second.plan_id
    assert first.challenge == f"SUBMIT 1 {first.plan_id}"


def test_plan_id_changes_with_code(signed_pdf):
    assert _plan([signed_pdf], "101000").plan_id != _plan([signed_pdf], "331000").plan_id


def test_plan_requires_a_document_code(signed_pdf):
    with pytest.raises(SubmitError):
        SubmitPlan.build([signed_pdf], {})


def test_plan_requires_at_least_one_file():
    with pytest.raises(SubmitError):
        SubmitPlan.build([], {})


def test_dump_does_not_change_existing_directory_permissions(tmp_path):
    tmp_path.chmod(0o755)
    _write_private(tmp_path / "page.html", "test")
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "page.html").stat().st_mode) == 0o600


# -------------------------------------------------------------------- gates
def test_wrong_confirmation_sends_nothing(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([])
    with pytest.raises(SubmitError):
        service.submit(plan, confirmation="SUBMIT 1 deadbeef", page=page)
    assert session.calls == []


def test_missing_confirmation_sends_nothing(service, session, signed_pdf, page):
    session._install([])
    with pytest.raises(SubmitError):
        service.submit(_plan([signed_pdf]), confirmation="", page=page)
    assert session.calls == []


def test_lock_is_exclusive(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([])
    service.state_dir.mkdir(parents=True, exist_ok=True)
    held = FileLock(str(service.lock_path), timeout=0)
    held.acquire()
    try:
        with pytest.raises(SubmitBusy):
            service.submit(plan, confirmation=plan.challenge, page=page)
    finally:
        held.release()
    assert session.calls == []


# ------------------------------------------------------------------- happy path
def test_submit_returns_drn_and_irn(service, session, signed_pdf, page, journal):
    plan = _plan([signed_pdf])
    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(200, text=staged_html(drn="2026102900000096", irn="2026102900000321", signature="Verified")),
        ]
    )

    receipt = service.submit(plan, confirmation=plan.challenge, page=page)

    assert receipt.outcome is SubmitOutcome.SUBMITTED
    assert receipt.rows[0]["drn"] == "2026102900000096"
    assert receipt.rows[0]["irn"] == "2026102900000321"
    assert receipt.submitted

    receipt_file = service.receipt_path(plan.plan_id)
    assert receipt_file.is_file()
    assert stat.S_IMODE(receipt_file.stat().st_mode) == 0o600
    saved = json.loads(receipt_file.read_text(encoding="utf-8"))
    assert saved["outcome"] == "submitted"

    events = [json.loads(line)["event"] for line in journal.path.read_text().splitlines()]
    assert "submit_intent" in events
    assert "submit_result" in events


def test_submit_posts_document_types(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(200, text=staged_html(drn="DRN123", irn="IRN456")),
        ]
    )
    service.submit(plan, confirmation=plan.challenge, page=page)

    method, url, kwargs = session.calls[1]
    assert method == "POST"
    assert url.endswith("/esanchit/submitDocs")
    assert kwargs["data"]["detailBean[0].docTyp"] == "101000-Registration Document"
    assert kwargs["data"]["tabId"] == "TAB0123456789"


def test_submit_never_retries(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(500, text="boom"),
        ]
    )
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN
    assert len(session.calls) == 2  # upload + one submit, no retry


@pytest.mark.parametrize("signature", ["", "Signature Failed"])
def test_result_without_verified_signature_is_unknown(service, session, signed_pdf, page, signature):
    plan = _plan([signed_pdf])
    session._install([
        FakeResponse(200, text=staged_html()),
        FakeResponse(200, text=staged_html(
            drn="2026102900000096", irn="2026102900000321", signature=signature
        )),
    ])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN


@pytest.mark.parametrize("drn,irn", [
    ("DRN123", "2026102900000321"),
    ("2026102900000096", "IRN456"),
    ("202610290000009", "2026102900000321"),
])
def test_invalid_id_format_is_unknown(service, session, signed_pdf, page, drn, irn):
    plan = _plan([signed_pdf])
    session._install([
        FakeResponse(200, text=staged_html()),
        FakeResponse(200, text=staged_html(drn=drn, irn=irn, signature="Successfully Verified")),
    ])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN


def test_403_after_submit_redirect_is_unknown(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([
        FakeResponse(200, text=staged_html()),
        FakeResponse(302, headers={"Location": "/esanchit/home"}),
        FakeResponse(403, text="session expired"),
    ])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN
    assert len(session.calls) == 3


def test_success_fields_with_portal_error_are_unknown(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    result = staged_html(drn="2026102900000096", irn="2026102900000321", signature="Verified")
    result = result.replace('<div id="docs_error"></div>', '<div id="docs_error">Error</div>')
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(200, text=result)])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN


def test_uses_portal_select_name_for_document_type(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    html = staged_html().replace('detailBean[0].docTyp', 'detailBean[7].docTyp')
    session._install([FakeResponse(200, text=html), FakeResponse(500, text="unknown")])
    service.submit(plan, confirmation=plan.challenge, page=page)
    assert session.calls[1][2]["data"]["detailBean[7].docTyp"] == "101000-Registration Document"


@pytest.mark.parametrize("bad_name", ["", "detailBean[0].docTyp.extra", "tabId"])
def test_unsafe_or_missing_select_name_aborts_before_submit(service, session, signed_pdf, page, bad_name):
    plan = _plan([signed_pdf])
    html = staged_html().replace('name="detailBean[0].docTyp"', f'name="{bad_name}"')
    session._install([FakeResponse(200, text=html)])
    with pytest.raises(SubmitAborted):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert len(session.calls) == 1


def test_duplicate_select_names_abort_before_submit(service, signed_pdf, tmp_path):
    second = tmp_path / "other.pdf"
    second.write_bytes(signed_pdf.read_bytes())
    plan = _plan([signed_pdf, second])
    def row(name):
        return StagedRow(index=0, file_name=name, select_name="detailBean[0].docTyp",
                         options=[("101000-Registration Document", "Registration Document")])
    staged = HomePage(html="", rows=[row(signed_pdf.name), row(second.name)])
    with pytest.raises(SubmitAborted, match="duplicate document type field"):
        service._submit_fields(plan, staged)


def test_interrupted_submit_leaves_receipt_blocking_replay(service, session, signed_pdf, page, monkeypatch):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html())])
    original = service.client.post_form

    def interrupted(action, **kwargs):
        if action == "submitDocs":
            raise KeyboardInterrupt()
        return original(action, **kwargs)

    monkeypatch.setattr(service.client, "post_form", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert json.loads(service.receipt_path(plan.plan_id).read_text())["outcome"] == "unknown"
    with pytest.raises(DuplicateSubmit):
        service.submit(plan, confirmation=plan.challenge, page=page)


def test_interrupted_submit_receipt_identifies_plan_without_journal(
    service, session, signed_pdf, page, monkeypatch, tmp_path
):
    plan = _plan([signed_pdf])
    service.journal = Journal(tmp_path / "disabled.jsonl", enabled=False)
    session._install([FakeResponse(200, text=staged_html())])
    original = service.client.post_form

    def interrupted(action, **kwargs):
        if action == "submitDocs":
            raise KeyboardInterrupt()
        return original(action, **kwargs)

    monkeypatch.setattr(service.client, "post_form", interrupted)
    with pytest.raises(KeyboardInterrupt):
        service.submit(plan, confirmation=plan.challenge, page=page)
    stored = service.receipt_path(plan.plan_id).read_text()
    receipt = json.loads(stored)
    assert receipt["outcome"] == "unknown"
    assert receipt["files"] == [{
        "name": signed_pdf.name,
        "bytes": len(signed_pdf.read_bytes()),
        "sha256": plan.files[0].sha256,
        "code": "101000",
    }]
    assert "%PDF-" not in stored
    assert not (tmp_path / "disabled.jsonl").exists()


def test_receipt_rechecked_inside_lock(service, session, signed_pdf, page, monkeypatch):
    plan = _plan([signed_pdf])
    original_acquire = FileLock.acquire

    def acquire_and_insert(lock, *args, **kwargs):
        result = original_acquire(lock, *args, **kwargs)
        service.receipts_dir.mkdir(parents=True, exist_ok=True)
        service.receipt_path(plan.plan_id).write_text('{}')
        return result

    monkeypatch.setattr(FileLock, "acquire", acquire_and_insert)
    session._install([])
    with pytest.raises(DuplicateSubmit):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert session.calls == []


# --------------------------------------------------------------------- refusals
def test_row_mismatch_aborts_before_submit(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html().replace("invoice.pdf", "other.pdf"))])
    with pytest.raises(SubmitAborted):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert len(session.calls) == 1
    assert not any("submitDocs" in call[1] for call in session.calls)


def test_missing_option_aborts_before_submit(service, session, signed_pdf, page):
    plan = _plan([signed_pdf], code="999999")
    session._install([FakeResponse(200, text=staged_html())])
    with pytest.raises(SubmitAborted):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert len(session.calls) == 1


def test_http_400_is_unknown(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(400, text="bad")])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN


def test_200_without_drn_is_unknown(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(200, text=staged_html())])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN
    assert "no DRN" in receipt.detail


def test_duplicate_plan_is_refused(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(200, text=staged_html(drn="DRN123", irn="IRN456")),
        ]
    )
    service.submit(plan, confirmation=plan.challenge, page=page)

    session._install([])
    with pytest.raises(DuplicateSubmit):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert session.calls == []


def test_duplicate_can_be_forced(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(200, text=staged_html(drn="DRN123", irn="IRN456")),
        ]
    )
    service.submit(plan, confirmation=plan.challenge, page=page)

    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(200, text=staged_html(drn="DRN999", irn="IRN999")),
        ]
    )
    receipt = service.submit(
        plan, confirmation=plan.challenge, page=page, allow_duplicate=True
    )
    assert receipt.rows[0]["drn"] == "DRN999"


def test_file_changed_after_plan_is_refused(service, session, signed_pdf, page, tmp_path):
    plan = _plan([signed_pdf])
    signed_pdf.write_bytes(signed_pdf.read_bytes() + b"\n% changed\n")
    session._install([])
    with pytest.raises(SubmitAborted):
        service.submit(plan, confirmation=plan.challenge, page=page)
    assert session.calls == []


def test_upload_uses_approved_bytes_if_file_changes_at_stage(service, session, signed_pdf, page, monkeypatch):
    plan = _plan([signed_pdf])
    approved_digest = plan.files[0].sha256
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(500, text="unknown")])
    original_stage = service.client.stage

    def mutate_then_stage(paths, **kwargs):
        signed_pdf.write_bytes(signed_pdf.read_bytes() + b"\n% unsigned change")
        return original_stage(paths, **kwargs)

    monkeypatch.setattr(service.client, "stage", mutate_then_stage)
    service.submit(plan, confirmation=plan.challenge, page=page)
    uploaded = session.calls[0][2]["files"][0][1][1]
    assert hashlib.sha256(uploaded).hexdigest() == approved_digest


def test_allowlist_is_submit_only():
    assert SUBMIT_ALLOWLIST == {"submitDocs"}


# ------------------------------------------------- batch / one DRN invariants
def test_receipt_batch_drn_is_shared_by_all_rows(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install(
        [
            FakeResponse(200, text=staged_html()),
            FakeResponse(
                200, text=staged_html(drn="2026102900000096", irn="2026102900000321", signature="Verified")
            ),
        ]
    )
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.batch_drn == "2026102900000096"
    assert receipt.irns == ["2026102900000321"]
    assert receipt.detail == ""
    assert json.loads(service.receipt_path(plan.plan_id).read_text())["drn"] == "2026102900000096"


def test_conflicting_drns_are_flagged_not_averaged(service, session, signed_pdf, page):
    """The portal model is one DRN per batch; a split reply must be surfaced."""
    plan = _plan([signed_pdf])
    extra = (
        "<tr><td>b.pdf</td><td><input value='DRN222'/></td>"
        "<td><input value='IRN222'/></td><td></td><td></td><td></td><td></td></tr>"
    )
    two_rows = staged_html(drn="DRN111", irn="IRN111").replace("</table>", extra + "</table>")
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(200, text=two_rows)])

    receipt = service.submit(plan, confirmation=plan.challenge, page=page)

    assert receipt.batch_drn == ""
    assert receipt.outcome is SubmitOutcome.UNKNOWN


def test_partial_result_is_unknown(service, session, signed_pdf, page):
    plan = _plan([signed_pdf])
    session._install([
        FakeResponse(200, text=staged_html()),
        FakeResponse(200, text=staged_html(drn="DRN123", irn="")),
    ])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN


def test_stage_uploads_a_batch_in_one_request(session, journal, tmp_path):
    """No per-file loop: the portal issues one DRN for the whole submission."""
    first = tmp_path / "a.pdf"
    second = tmp_path / "b.pdf"
    first.write_bytes(SIGNED_PDF)
    second.write_bytes(SIGNED_PDF + b"\n% b\n")

    session._install([FakeResponse(200, text=staged_html())])
    client = EsanchitClient(session, journal)
    client.stage([first, second], page=HomePage.parse(EMPTY_HOME_HTML))

    assert len(session.calls) == 1
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url.endswith("/esanchit/uploadDocs")
    assert [name for name, _ in kwargs["files"]] == ["docs", "docs"]
    assert [part[1][0] for part in kwargs["files"]] == ["a.pdf", "b.pdf"]

@pytest.mark.parametrize('body', ['   ', '<?xml version="1.0" encoding="UTF-8"?><html></html>'])
def test_unusual_submit_response_is_unknown(service, session, signed_pdf, page, body):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(200, text=body)])
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN
    assert json.loads(service.receipt_path(plan.plan_id).read_text())['outcome'] == 'unknown'
    assert len(session.calls) == 2


def test_submit_interpret_exception_remains_unknown(service, session, signed_pdf, page, monkeypatch):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(200, text='ok')])
    def fail(*args):
        raise ValueError('private response content')
    monkeypatch.setattr(service, '_interpret', fail)
    receipt = service.submit(plan, confirmation=plan.challenge, page=page)
    assert receipt.outcome is SubmitOutcome.UNKNOWN
    assert 'private response content' not in receipt.detail
    assert len(session.calls) == 2


def test_unexpected_post_exception_leaves_unknown_receipt(service, session, signed_pdf, page, monkeypatch):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html())])
    original = service.client.post_form
    def fail(action, **kwargs):
        if action == 'submitDocs':
            raise RuntimeError('secret response')
        return original(action, **kwargs)
    monkeypatch.setattr(service.client, 'post_form', fail)
    result = service.submit(plan, confirmation=plan.challenge, page=page)
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert 'secret response' not in result.detail
    assert json.loads(service.receipt_path(plan.plan_id).read_text())['outcome'] == 'unknown'


def test_pending_journal_event_is_not_a_result(service, session, signed_pdf, page, journal):
    plan = _plan([signed_pdf])
    session._install([FakeResponse(200, text=staged_html()), FakeResponse(500, text='unknown')])
    service.submit(plan, confirmation=plan.challenge, page=page)
    events = [json.loads(line)['event'] for line in journal.path.read_text().splitlines()]
    assert events.count('submit_pending') == 1
    assert events.count('submit_result') == 1
