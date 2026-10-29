from __future__ import annotations

import pytest
from conftest import SIGNED_PDF

from icegate_client.cli import enforce_page_limits
from icegate_client.esanchit.page import HomePage
from icegate_client.esanchit.pdf import Limits, inspect_pdf, limit_messages, preflight
from icegate_client.shared.errors import PreflightError


def test_inspect_signed_pdf(signed_pdf):
    info = inspect_pdf(signed_pdf)
    assert info.signed
    assert info.pages == 1
    assert info.signature_filters == ("adbe.pkcs7.detached",)
    assert not info.encrypted
    assert len(info.sha256) == 64


def test_inspect_unsigned_pdf(unsigned_pdf):
    info = inspect_pdf(unsigned_pdf)
    assert not info.signed
    assert info.pages == 1


def test_missing_file(tmp_path):
    with pytest.raises(PreflightError):
        inspect_pdf(tmp_path / "nope.pdf")


def test_non_pdf(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(PreflightError):
        inspect_pdf(path)


def test_bad_header(tmp_path):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"not a pdf")
    with pytest.raises(PreflightError):
        inspect_pdf(path)


def test_encrypted_pdf(tmp_path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(SIGNED_PDF + b"trailer << /Encrypt 9 0 R >>\n")
    assert inspect_pdf(path).encrypted


def test_preflight_ok(signed_pdf):
    report = preflight([signed_pdf])
    assert report.ok
    assert len(report.items) == 1


def test_preflight_rejects_unsigned(unsigned_pdf):
    report = preflight([unsigned_pdf])
    assert not report.ok
    assert any("no digital signature" in problem for problem in report.problems)


def test_preflight_can_allow_unsigned(unsigned_pdf):
    report = preflight([unsigned_pdf], allow_unsigned=True)
    assert report.ok


def test_preflight_duplicate_names(tmp_path):
    first = tmp_path / "a" / "invoice.pdf"
    second = tmp_path / "b" / "invoice.pdf"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(SIGNED_PDF)
    report = preflight([first, second])
    assert not report.ok
    assert any("duplicate file name" in problem for problem in report.problems)


def test_size_limit_warns_by_default(tmp_path):
    path = tmp_path / "big.pdf"
    path.write_bytes(SIGNED_PDF + b"x" * (2 * 1024 * 1024))
    report = preflight([path])
    assert report.ok
    assert any("per-file limit" in warning for warning in report.warnings)


def test_size_limit_is_error_when_strict(tmp_path):
    path = tmp_path / "big.pdf"
    path.write_bytes(SIGNED_PDF + b"x" * (2 * 1024 * 1024))
    report = preflight([path], Limits(strict=True))
    assert not report.ok


def test_file_count_limit(tmp_path):
    paths = []
    for index in range(6):
        path = tmp_path / f"doc{index}.pdf"
        path.write_bytes(SIGNED_PDF.replace(b"DRN", b"DRN" + str(index).encode()))
        paths.append(path)
    report = preflight(paths)
    assert report.ok
    assert any("file limit" in warning for warning in report.warnings)


def test_no_files_given():
    report = preflight([])
    assert not report.ok


# ------------------------------------------------------- cryptographic checks
def test_real_signature_is_valid_but_untrusted(real_signed_pdf):
    info = inspect_pdf(real_signed_pdf)
    assert info.signed
    assert info.signature.ok
    assert info.signature.intact
    assert info.signature.crypto_valid
    assert not info.signature.trusted
    assert info.signature.signer == "TEST SIGNER"
    assert info.signature.describe() == "valid (chain not verified)"


def test_preflight_accepts_valid_untrusted_signature(real_signed_pdf):
    report = preflight([real_signed_pdf])
    assert report.ok, report.problems


def test_preflight_rejects_tampered_signature(tmp_path, real_signed_pdf_bytes):
    data = bytearray(real_signed_pdf_bytes)
    data[len(data) // 3] ^= 0xFF
    path = tmp_path / "tampered.pdf"
    path.write_bytes(bytes(data))

    info = inspect_pdf(path)
    assert info.signed
    assert not info.signature.ok

    # Staging only warns: the portal decides, and nothing is submitted.
    assert preflight([path]).ok
    # Submitting is irreversible, so the same file must not pass there.
    report = preflight([path], strict_signature=True)
    assert not report.ok
    assert any("could not be verified" in problem for problem in report.problems)


def test_preflight_can_allow_invalid_signature(tmp_path, real_signed_pdf_bytes):
    data = bytearray(real_signed_pdf_bytes)
    data[len(data) // 3] ^= 0xFF
    path = tmp_path / "tampered.pdf"
    path.write_bytes(bytes(data))
    assert preflight([path], allow_invalid=True).ok


def test_marker_only_pdf_cannot_be_verified(signed_pdf):
    info = inspect_pdf(signed_pdf)
    assert info.signed
    assert info.signature.error
    assert "unverified" in info.signature.describe()


def test_explicit_override_allows_unverifiable_signature_on_submit(signed_pdf):
    report = preflight([signed_pdf], strict_signature=True, allow_invalid=True)
    assert report.ok
    assert any("could not be verified" in message for message in report.warnings)


def test_unsigned_data_appended_after_signature_is_not_accepted(tmp_path, real_signed_pdf_bytes):
    path = tmp_path / "modified.pdf"
    path.write_bytes(real_signed_pdf_bytes + b"\n% unsigned trailing data\n")
    assert not inspect_pdf(path).signature.ok
    report = preflight([path], strict_signature=True)
    assert not report.ok
    assert any("cover the entire file" in problem for problem in report.problems)


# ------------------------------------------------------------- limit messages
def test_limit_messages_warn_by_default(real_signed_pdf):
    info = inspect_pdf(real_signed_pdf)
    live = Limits(max_file_bytes=info.size - 1, max_files=0)
    messages = limit_messages([info], live)
    assert [severity for severity, _ in messages] == ["warning", "warning"]
    assert "per-file limit" in messages[0][1]
    assert "file limit per DRN" in messages[1][1]


def test_limit_messages_become_problems_when_strict(real_signed_pdf):
    info = inspect_pdf(real_signed_pdf)
    live = Limits(max_file_bytes=info.size - 1, max_files=0, strict=True)
    assert all(severity == "problem" for severity, _ in limit_messages([info], live))


def test_limit_messages_are_silent_within_the_limit(real_signed_pdf):
    info = inspect_pdf(real_signed_pdf)
    live = Limits(max_file_bytes=info.size, max_files=1, total_bytes=info.size)
    assert limit_messages([info], live) == []


def test_live_page_limits_override_the_defaults(real_signed_pdf):
    """The page is the authority: a tighter live limit must not be ignored."""
    report = preflight([real_signed_pdf])
    assert report.ok
    page = HomePage.parse(
        "<html><body><p>Maximum size per file is 0.001 MB</p></body></html>"
    )
    assert page.max_file_bytes == 1048

    with pytest.raises(PreflightError):
        enforce_page_limits(page, report, strict=True)


def test_live_page_limits_do_not_repeat_the_defaults(real_signed_pdf, capsys):
    report = preflight([real_signed_pdf])
    page = HomePage.parse(
        "<html><body><p>Maximum size per file is 2 MB, maximum files are 5</p></body></html>"
    )
    enforce_page_limits(page, report, strict=True)
    assert capsys.readouterr().out == ""


def test_total_request_limit_is_reported(real_signed_pdf):
    info = inspect_pdf(real_signed_pdf)
    limits = Limits(max_file_bytes=10**9, max_files=99, total_bytes=info.size - 1)
    (severity, message), = limit_messages([info], limits)
    assert severity == "warning"
    assert "total limit" in message
