from __future__ import annotations

import json
import stat

import pytest
from requests.exceptions import ReadTimeout

from icegate_client.filing.client import (
    FilingError,
    FilingOutcome,
    FilingPlan,
    FilingService,
)
from icegate_client.shared.errors import DuplicateSubmit

SIGNED = (
    b"CACHE01\n<START-SIGNATURE>AA==</START-SIGNATURE>\n"
    b"<START-CERTIFICATE>AA==</START-CERTIFICATE>\n"
    b"<SIGNER-VERSION>V-NCODE</SIGNER-VERSION>\n"
)


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.token = "token"
        self.session_id = "session"

    def url(self, path):
        return "https://foservices.icegate.gov.in" + path

    def auth_headers(self):
        return {"channel": "browser", "Authorization": self.token, "session_id": self.session_id}

    def send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.fixture
def signed_sb(tmp_path):
    path = tmp_path / "filing.sb"
    path.write_bytes(SIGNED)
    return path


def test_upload_acceptance_is_not_final_filing(signed_sb, journal, tmp_path):
    session = Session(Response(202, {"validationStatus": "SUCCESS", "errorMsg": "processing"}))
    plan = FilingPlan.build([signed_sb])
    service = FilingService(session, journal, tmp_path / "state")
    receipts = service.submit(plan, confirmation=plan.challenge)
    assert receipts[0].outcome is FilingOutcome.ACCEPTED_FOR_PROCESSING
    assert not receipts[0].final_customs_status_known
    method, url, request = session.calls[0]
    assert method == "POST" and url.endswith("/filing/file")
    assert request["files"]["messageFile"][1] == SIGNED
    assert request["headers"]["channel"] == "browser"
    receipt = service.receipt_path(plan.files[0].sha256)
    assert json.loads(receipt.read_text())["outcome"] == "accepted_for_processing"
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_unknown_outcome_is_persisted_and_never_retried(signed_sb, journal, tmp_path):
    session = Session(ReadTimeout("unexpected transport error"))
    plan = FilingPlan.build([signed_sb])
    service = FilingService(session, journal, tmp_path / "state")
    result = service.submit(plan, confirmation=plan.challenge)
    assert result[0].outcome is FilingOutcome.UNKNOWN
    assert len(session.calls) == 1
    with pytest.raises(DuplicateSubmit):
        service.submit(plan, confirmation=plan.challenge)
    assert len(session.calls) == 1


def test_interruption_leaves_unknown_receipt(signed_sb, journal, tmp_path):
    session = Session(KeyboardInterrupt())
    plan = FilingPlan.build([signed_sb])
    service = FilingService(session, journal, tmp_path / "state")
    with pytest.raises(KeyboardInterrupt):
        service.submit(plan, confirmation=plan.challenge)
    saved = json.loads(service.receipt_path(plan.files[0].sha256).read_text())
    assert saved["outcome"] == "unknown"
    assert "CACHE01" not in json.dumps(saved)


def test_portal_failure_is_not_customs_acknowledgement(signed_sb, journal, tmp_path):
    session = Session(Response(200, {"validationStatus": "FAILED", "errorMsg": "bad file"}))
    plan = FilingPlan.build([signed_sb])
    service = FilingService(session, journal, tmp_path / "state")
    result = service.submit(plan, confirmation=plan.challenge)
    assert result[0].outcome is FilingOutcome.PORTAL_REPORTED_FAILURE
    assert not result[0].final_customs_status_known


def test_unexpected_http_or_response_is_unknown(signed_sb, journal, tmp_path):
    session = Session(Response(403, {"validationStatus": "SUCCESS"}))
    plan = FilingPlan.build([signed_sb])
    result = FilingService(session, journal, tmp_path / "state").submit(
        plan, confirmation=plan.challenge
    )
    assert result[0].outcome is FilingOutcome.UNKNOWN


def test_rejects_unsigned_or_wrong_file_before_any_request(tmp_path):
    unsigned = tmp_path / "file.sb"
    unsigned.write_bytes(b"CACHE01\n")
    with pytest.raises(FilingError):
        FilingPlan.build([unsigned])
    wrong = tmp_path / "file.txt"
    wrong.write_bytes(SIGNED)
    with pytest.raises(FilingError):
        FilingPlan.build([wrong])


def test_accepts_signed_be_structure(tmp_path):
    be = tmp_path / "message.BE"
    be.write_bytes(SIGNED)
    assert FilingPlan.build([be]).files[0].name == "message.BE"


def test_uploads_snapshot_not_changed_file(signed_sb, journal, tmp_path):
    session = Session(Response(202, {"validationStatus": "SUCCESS"}))
    plan = FilingPlan.build([signed_sb])
    signed_sb.write_bytes(SIGNED + b"changed")
    result = FilingService(session, journal, tmp_path / "state").submit(
        plan, confirmation=plan.challenge
    )
    assert result[0].outcome is FilingOutcome.ACCEPTED_FOR_PROCESSING
    assert session.calls[0][2]["files"]["messageFile"][1] == SIGNED


def test_challenge_mismatch_sends_nothing(signed_sb, journal, tmp_path):
    session = Session(Response(202, {"validationStatus": "SUCCESS"}))
    plan = FilingPlan.build([signed_sb])
    with pytest.raises(FilingError):
        FilingService(session, journal, tmp_path / "state").submit(plan, confirmation="wrong")
    assert session.calls == []


def test_prior_manual_test_record_blocks_duplicate(signed_sb, journal, tmp_path):
    session = Session(Response(202, {"validationStatus": "SUCCESS"}))
    plan = FilingPlan.build([signed_sb])
    state = tmp_path / "state"
    previous = state / "filing-tests" / f"{plan.files[0].sha256}.json"
    previous.parent.mkdir(parents=True)
    previous.write_text('{}')
    with pytest.raises(DuplicateSubmit):
        FilingService(session, journal, state).submit(plan, confirmation=plan.challenge)
    assert not session.calls


def test_unrecognized_response_body_is_not_saved(signed_sb, journal, tmp_path):
    session = Session(Response(200, {"validationStatus": "FAILED", "other": "PRIVATE_ECHO"}))
    plan = FilingPlan.build([signed_sb])
    service = FilingService(session, journal, tmp_path / "state")
    service.submit(plan, confirmation=plan.challenge)
    assert "PRIVATE_ECHO" not in service.receipt_path(plan.files[0].sha256).read_text()


def test_batch_stops_after_uncertain_response(tmp_path, journal):
    files = []
    for i in range(2):
        path = tmp_path / f"message{i}.sb"
        path.write_bytes(SIGNED + str(i).encode())
        files.append(path)
    session = Session(ReadTimeout("uncertain"))
    plan = FilingPlan.build(files)
    service = FilingService(session, journal, tmp_path / "state")
    receipts = service.submit(plan, confirmation=plan.challenge)
    assert len(receipts) == 1
    assert receipts[0].outcome is FilingOutcome.UNKNOWN
    assert not service.receipt_path(plan.files[1].sha256).exists()


def test_duplicate_filing_requires_explicit_override(signed_sb, journal, tmp_path):
    session = Session(Response(200, {'validationStatus': 'FAILED'}))
    plan = FilingPlan.build([signed_sb])
    service = FilingService(session, journal, tmp_path / 'state')
    service.submit(plan, confirmation=plan.challenge)
    with pytest.raises(DuplicateSubmit):
        service.submit(plan, confirmation=plan.challenge)
    service.submit(plan, confirmation=plan.challenge, allow_duplicate=True)
    assert len(session.calls) == 2
