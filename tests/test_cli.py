import hashlib
import json

import pytest

from icegate_client import cli
from icegate_client.cli import build_parser
from icegate_client.esanchit.doc_types import DocumentCatalog


def test_unavailable_pdf_trust_option_is_not_exposed():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["esanchit", "submit", "a.pdf", "--require-trusted"])


def test_catalog_includes_alphanumeric_portal_codes():
    assert DocumentCatalog.load().get("101TO2") is not None


def test_short_search_and_download_commands():
    parser = build_parser()
    assert parser.parse_args(["ecopy", "sb", "--yesterday"]).kind == "SB"
    assert parser.parse_args(["ecopy", "be", "--today"]).kind == "BE"
    args = parser.parse_args(["ecopy", "download", "--irn", "123", "--user-id", "id", "-o", "a.pdf"])
    assert args.copy_type == "signed" and args.output == "a.pdf"
    assert parser.parse_args(["esanchit", "submit", "a.pdf", "--type", "331000"]).doc_type == "331000"


def test_nested_esanchit_commands_keep_existing_handlers():
    parser = build_parser()
    nested = parser.parse_args(["esanchit", "submit", "a.pdf", "--doc-type", "331000", "--dry-run"])
    assert nested.func is cli.cmd_submit


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
        "esanchit", "submit", str(real_signed_pdf), "--doc-type", "101000", "--dry-run",
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
        "esanchit", "submit", str(file), "--doc-type", "101000", "--dry-run", flag,
        "--state-dir", str(tmp_path / "state"),
    ]) == 0


def test_missing_pdf_reports_error_instead_of_traceback(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    result = cli.main([
        "esanchit", "submit", str(tmp_path / "missing.pdf"), "--doc-type", "101000", "--dry-run",
        "--state-dir", str(tmp_path / "state"),
    ])
    assert result == 1
    assert "missing.pdf" in capsys.readouterr().err


def test_json_warnings_go_to_stderr(unsigned_pdf, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    assert cli.main(['esanchit', 'stage', str(unsigned_pdf), str(unsigned_pdf.parent / 'copy.pdf'),
                     '--dry-run', '--allow-unsigned', '--json', '--state-dir', str(tmp_path / 'state')]) == 1
    output = capsys.readouterr()
    assert not output.out
    assert 'error' in output.err


def test_unmatched_document_map_is_rejected(unsigned_pdf, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    result = cli.main(['esanchit', 'stage', str(unsigned_pdf), '--dry-run', '--allow-unsigned',
                       '--doc-type', '331000', '--doc-type-map', 'typo.pdf=101000',
                       '--state-dir', str(tmp_path / 'state')])
    assert result == 1
    assert 'typo.pdf' in (capsys.readouterr().err)


def test_structured_json_scrubs_escaped_secret():
    from icegate_client.shared.redact import remember
    secret = 'a-secret"with\\escaped123'
    remember(secret)
    output = cli._json({'value': secret})
    assert json.loads(output)['value'] == '<redacted>'


def test_json_warning_does_not_contaminate_output(unsigned_pdf, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    other = tmp_path / 'copy.pdf'
    other.write_bytes(unsigned_pdf.read_bytes())
    assert cli.main(['esanchit', 'stage', str(unsigned_pdf), str(other), '--dry-run', '--allow-unsigned',
                     '--json', '--state-dir', str(tmp_path / 'state')]) == 0
    result = capsys.readouterr()
    assert json.loads(result.out)['ok']
    assert 'identical' in result.err


def test_stage_uploads_preflight_snapshot(unsigned_pdf, monkeypatch, tmp_path):
    from conftest import EMPTY_HOME_HTML

    from icegate_client.esanchit.page import HomePage
    original = unsigned_pdf.read_bytes()
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    monkeypatch.setattr(cli.Context, 'authenticate', lambda self: None)
    monkeypatch.setattr(cli.IcegateSession, 'refresh_if_expiring', lambda *args: False)
    monkeypatch.setattr(cli.IcegateSession, 'esanchit_ticket', lambda *args: 'ticket')
    monkeypatch.setattr(cli.EsanchitClient, 'open_from_ticket', lambda *args: HomePage.parse(EMPTY_HOME_HTML))
    def stage(self, files, *, page, file_contents):
        assert file_contents[files[0]] == original
        return page
    monkeypatch.setattr(cli.EsanchitClient, 'stage', stage)
    real_preflight = cli._preflight_report
    def change_after_validation(*args, **kwargs):
        report = real_preflight(*args, **kwargs)
        unsigned_pdf.write_bytes(b'replacement')
        return report
    monkeypatch.setattr(cli, '_preflight_report', change_after_validation)
    assert cli.main(['esanchit', 'stage', str(unsigned_pdf), '--allow-unsigned', '--state-dir', str(tmp_path / 'state')]) == 0


def test_close_failure_does_not_leak_traceback(unsigned_pdf, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv('ICEGATE_USERNAME', 'test')
    monkeypatch.setenv('ICEGATE_PASSWORD', 'test')
    def fail_close(self):
        raise OSError('sensitive detail')
    monkeypatch.setattr(cli.Context, 'close', fail_close)
    assert cli.main(['esanchit', 'stage', str(unsigned_pdf), '--allow-unsigned', '--dry-run', '--json',
                     '--state-dir', str(tmp_path / 'state')]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['ok']
    assert 'sensitive detail' not in captured.err
    assert 'journal' in captured.err
