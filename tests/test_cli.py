import hashlib
import json

import pytest

from icegate_client import cli
from icegate_client.cli import build_parser
from icegate_client.esanchit.doc_types import DocumentCatalog


def test_unavailable_pdf_trust_option_is_not_exposed():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["submit", "a.pdf", "--require-trusted"])


def test_catalog_includes_alphanumeric_portal_codes():
    assert DocumentCatalog.load().get("101TO2") is not None


def test_nested_esanchit_commands_keep_existing_handlers():
    parser = build_parser()
    nested = parser.parse_args(["esanchit", "submit", "a.pdf", "--doc-type", "331000", "--dry-run"])
    old = parser.parse_args(["submit", "a.pdf", "--doc-type", "331000", "--dry-run"])
    assert nested.func is old.func


def test_filing_dry_run_plans_signed_file_without_network(tmp_path, monkeypatch, capsys):
    file = tmp_path / "message.sb"
    file.write_bytes(b"CACHE01\n<START-SIGNATURE>AA==</START-SIGNATURE>\n"
                     b"<START-CERTIFICATE>AA==</START-CERTIFICATE>\n")
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    monkeypatch.setattr(cli.Context, "authenticate", lambda self: pytest.fail("unexpected login"))
    result = cli.main(["filing", "upload", str(file), "--dry-run", "--json", "--state-dir", str(tmp_path / "state")])
    assert result == 0
    data = json.loads(capsys.readouterr().out)
    assert data["files"][0]["name"] == "message.sb"
    assert data["challenge"].startswith("FILE 1 ")
    assert data["signature_verified"] is False


def test_filing_upload_reports_processing_not_final_acceptance(tmp_path, monkeypatch, capsys):
    file = tmp_path / "message.sb"
    file.write_bytes(b"CACHE01\n<START-SIGNATURE>AA==</START-SIGNATURE>\n"
                     b"<START-CERTIFICATE>AA==</START-CERTIFICATE>\n")
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    def authenticate(ctx):
        ctx.session.token, ctx.session.session_id = "test-token", "test-session"
        ctx.session.send = lambda *args, **kwargs: type("Response", (), {
            "status_code": 202,
            "json": lambda self: {"validationStatus": "SUCCESS"},
        })()
    monkeypatch.setattr(cli.Context, "authenticate", authenticate)
    from icegate_client.filing.client import FilingPlan
    challenge = FilingPlan.build([file]).challenge
    result = cli.main(["filing", "upload", str(file), "--confirm", challenge, "--json",
                       "--state-dir", str(tmp_path / "state")])
    assert result == 0
    receipt = json.loads(capsys.readouterr().out)["receipts"][0]
    assert receipt["outcome"] == "accepted_for_processing"
    assert receipt["final_customs_status_known"] is False


def test_submit_plan_uses_same_bytes_as_preflight(real_signed_pdf, monkeypatch, tmp_path, capsys):
    original_hash = hashlib.sha256(real_signed_pdf.read_bytes()).hexdigest()
    original_preflight = cli._preflight_report

    def validate_then_modify(*args, **kwargs):
        report = original_preflight(*args, **kwargs)
        real_signed_pdf.write_bytes(real_signed_pdf.read_bytes() + b"\n% unsigned change")
        return report

    monkeypatch.setattr(cli, "_preflight_report", validate_then_modify)
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    result = cli.main([
        "submit", str(real_signed_pdf), "--doc-type", "101000", "--dry-run",
        "--state-dir", str(tmp_path / "state"), "--json",
    ])
    assert result == 0
    result_json = json.loads(capsys.readouterr().out)
    assert result_json["files"][0]["sha256"] == original_hash


@pytest.mark.parametrize("fixture, flag", [
    ("unsigned_pdf", "--allow-unsigned"),
    ("signed_pdf", "--allow-invalid-signature"),
])
def test_submit_dry_run_preserves_explicit_signature_override(request, fixture, flag, monkeypatch, tmp_path):
    file = request.getfixturevalue(fixture)
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    assert cli.main([
        "submit", str(file), "--doc-type", "101000", "--dry-run", flag,
        "--state-dir", str(tmp_path / "state"),
    ]) == 0


def test_missing_pdf_reports_error_instead_of_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    result = cli.main([
        "submit", str(tmp_path / "missing.pdf"), "--doc-type", "101000", "--dry-run",
        "--state-dir", str(tmp_path / "state"),
    ])
    assert result == 1
    assert "missing.pdf" in capsys.readouterr().out
