"""Local PDF preflight.

The client expects files that were already signed with ``icegate-sign``. It
does not invoke the USB token. Signature validation is delegated to pyHanko:
the marker scan only decides whether it is worth parsing, and the verdict
comes from checking the CMS signature against the embedded signer certificate.

Trust is reported separately from integrity. No PDF trust roots are configured,
so signer trust is not established.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ..shared.errors import PreflightError

# Portal UI values, used only as a fallback when the live page does not
# advertise its own limits. They are not treated as server rules.
DEFAULT_MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_FILES = 5

_PAGE_RE = re.compile(rb"/Type\s*/Page(?![A-Za-z])")
_SIG_FILTER_RE = re.compile(rb"/SubFilter\s*/([A-Za-z0-9._#-]+)")
_ENCRYPT_RE = re.compile(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)")
_BYTERANGE_RE = re.compile(rb"/ByteRange\s*\[")


@dataclass(frozen=True)
class SignatureReport:
    """What the file claims, and what the cryptography says."""

    present: bool = False
    count: int = 0
    intact: bool = True
    crypto_valid: bool = True
    trusted: bool = False
    coverage: str = ""
    signer: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        """The signatures verify and the latest one covers the whole file."""
        return (self.present and self.intact and self.crypto_valid
                and self.coverage == "ENTIRE_FILE" and not self.error)

    def describe(self) -> str:
        if not self.present:
            return "unsigned"
        if self.error:
            return f"unverified ({self.error})"
        if not self.intact:
            return "modified after signing"
        if not self.crypto_valid:
            return "invalid signature"
        if self.coverage != "ENTIRE_FILE":
            return "signature does not cover the entire file"
        return "valid" if self.trusted else "valid (chain not verified)"


@dataclass(frozen=True)
class PdfInfo:
    path: Path
    size: int
    sha256: str
    pages: int
    encrypted: bool
    has_byte_range: bool
    signature_filters: tuple[str, ...] = ()
    signature: SignatureReport = field(default_factory=SignatureReport)

    @property
    def signed(self) -> bool:
        return self.signature.present or (
            self.has_byte_range and bool(self.signature_filters)
        )


@dataclass
class Limits:
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES
    total_bytes: int | None = None
    strict: bool = False

    def describe(self) -> str:
        total = f", total {self.total_bytes} bytes" if self.total_bytes else ""
        return f"{self.max_file_bytes} bytes per file, {self.max_files} files{total}"


@dataclass
class PreflightReport:
    items: list[PdfInfo] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


@contextmanager
def _quiet_pyhanko():
    """Keep chain-building tracebacks out of CLI output.

    A document signed by an Indian DSC does not chain to the system trust
    store, so pyHanko logs a path-building error per signature. That is
    expected; the status object already carries the verdict.
    """
    names = ("pyhanko", "pyhanko_certvalidator")
    previous = {name: logging.getLogger(name).level for name in names}
    for name in names:
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


def inspect_signatures(data: bytes) -> SignatureReport:
    """Validate embedded PDF signatures with pyHanko.

    A marker scan runs first: an unsigned file is never parsed, which keeps
    the common case fast and avoids feeding arbitrary bytes to the parser.
    """
    if not _BYTERANGE_RE.search(data):
        return SignatureReport(present=False, trusted=False)

    try:
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.sign.validation import validate_pdf_signature
        from pyhanko_certvalidator import ValidationContext
    except ImportError as exc:  # pragma: no cover - dependency is declared
        return SignatureReport(present=True, error=f"pyhanko unavailable: {exc}")

    try:
        with _quiet_pyhanko():
            reader = PdfFileReader(io.BytesIO(data))
            signatures = list(reader.embedded_signatures)
            if not signatures:
                return SignatureReport(present=False, error="markers but no signature field")

            context = ValidationContext(
                trust_roots=[],
                allow_fetching=False,
                revocation_mode="soft-fail",
            )
            statuses = [
                validate_pdf_signature(sig, signer_validation_context=context, skip_diff=True)
                for sig in signatures
            ]
    except Exception as exc:  # noqa: BLE001 - malformed PDFs raise many types
        return SignatureReport(
            present=True,
            count=0,
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )

    last = statuses[-1]
    signer = ""
    try:
        signer = last.signing_cert.subject.native.get("common_name", "") or ""
    except Exception:  # noqa: BLE001 - exotic certificates raise many types
        signer = ""

    return SignatureReport(
        present=True,
        count=len(statuses),
        intact=all(status.intact for status in statuses),
        crypto_valid=all(status.valid for status in statuses),
        trusted=all(status.trusted for status in statuses),
        coverage=str(getattr(last.coverage, "name", last.coverage)),
        signer=signer,
    )


def inspect_pdf(path: Path | str, *, contents: bytes | None = None) -> PdfInfo:
    target = Path(path)
    if not target.is_file():
        raise PreflightError(f"{target.name}: file not found")
    if target.suffix.lower() != ".pdf":
        raise PreflightError(f"{target.name}: only PDF files are accepted")

    data = target.read_bytes() if contents is None else contents
    if not data:
        raise PreflightError(f"{target.name}: file is empty")
    if not data.startswith(b"%PDF-"):
        raise PreflightError(f"{target.name}: missing %PDF- header")

    filters = tuple(
        sorted({m.group(1).decode("ascii", "replace") for m in _SIG_FILTER_RE.finditer(data)})
    )
    return PdfInfo(
        path=target,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        pages=len(_PAGE_RE.findall(data)),
        encrypted=bool(_ENCRYPT_RE.search(data)),
        has_byte_range=bool(_BYTERANGE_RE.search(data)),
        signature_filters=filters,
        signature=inspect_signatures(data),
    )


def preflight(
    paths: list[Path | str],
    limits: Limits | None = None,
    *,
    allow_unsigned: bool = False,
    allow_invalid: bool = False,
    strict_signature: bool = False,
    file_contents: dict[Path, bytes] | None = None,
) -> PreflightReport:
    limits = limits or Limits()
    report = PreflightReport()

    if not paths:
        report.problems.append("no files given")
        return report

    seen_names: dict[str, str] = {}

    for raw in paths:
        target = Path(raw)
        try:
            info = inspect_pdf(target, contents=file_contents[target] if file_contents is not None else None)
        except PreflightError as exc:
            report.problems.append(str(exc))
            continue

        report.items.append(info)

        key = info.path.name.lower()
        if key in seen_names:
            report.problems.append(
                f"{info.path.name}: duplicate file name (also {seen_names[key]})"
            )
        else:
            seen_names[key] = str(info.path)

        if info.encrypted:
            report.problems.append(f"{info.path.name}: PDF is encrypted")
        if info.pages == 0:
            report.problems.append(f"{info.path.name}: no pages found")

        signature = info.signature
        if not info.signed and not allow_unsigned:
            report.problems.append(
                f"{info.path.name}: no digital signature; sign it first "
                "(or pass --allow-unsigned)"
            )
        elif info.signed and signature.error:
            message = (
                f"{info.path.name}: signature could not be verified "
                f"({signature.error})"
            )
            (report.problems if strict_signature and not allow_invalid else report.warnings).append(message)
        elif info.signed and not allow_invalid and not signature.ok and not signature.error:
            if not signature.intact:
                reason = "the file was modified after signing"
            elif not signature.crypto_valid:
                reason = "the signature value is invalid"
            else:
                reason = "the last signature does not cover the entire file"
            report.problems.append(
                f"{info.path.name}: signature does not verify - {reason}. Re-sign it "
                "(or pass --allow-invalid-signature)"
            )
    for severity, message in limit_messages(report.items, limits):
        (report.problems if severity == "problem" else report.warnings).append(message)

    digests = [info.sha256 for info in report.items]
    if len(set(digests)) != len(digests):
        report.warnings.append("two or more files have identical content")

    return report


def limit_messages(items: list[PdfInfo], limits: Limits) -> list[tuple[str, str]]:
    """Size and count messages, tagged ``problem`` or ``warning``.

    Split out because the portal only reveals its real numbers once a session
    is open, and preflight runs before the login: the same rules are applied
    twice, first with the documented defaults and then with the live page.
    """
    severity = "problem" if limits.strict else "warning"
    messages: list[tuple[str, str]] = []
    for item in items:
        if item.size > limits.max_file_bytes:
            message = (
                f"{item.path.name}: {item.size} bytes exceeds the "
                f"{limits.max_file_bytes} byte per-file limit"
            )
            messages.append((severity, message))
    if len(items) > limits.max_files:
        message = (
            f"{len(items)} files exceeds the portal's stated {limits.max_files} "
            "file limit per DRN. Do not split the shipment into smaller runs: "
            "every submission creates its own DRN, and the portal has no merge"
        )
        messages.append((severity, message))
    total = sum(item.size for item in items)
    if limits.total_bytes and total > limits.total_bytes:
        message = f"{total} bytes exceeds the {limits.total_bytes} byte total limit"
        messages.append((severity, message))
    return messages
