from __future__ import annotations

import pytest

from icegate_client.shared.config import Settings
from icegate_client.shared.journal import Journal
from icegate_client.shared.redact import REDACTOR


@pytest.fixture(autouse=True)
def _isolate_default_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr("icegate_client.shared.config.default_env_file", lambda: tmp_path / ".env")


@pytest.fixture(autouse=True)
def _clean_redactor():
    REDACTOR.forget_all()
    yield
    REDACTOR.forget_all()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        icegate_id="TESTID01",
        password="hunter2hunter2",
        state_dir=tmp_path / "state",
    )


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "journal.jsonl", enabled=True)


class FakeResponse:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body
        self.text = text
        self.headers = headers or {}
        self.content = text.encode() if text else b""
        self.cookies = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class ScriptedSessionMixin:
    """Replace transport with a scripted list of responses."""

    def _install(self, script):
        self.script = list(script)
        self.calls = []

    def _send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.script:
            raise AssertionError(f"unexpected request: {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


SIGNED_PDF = (
    b"%PDF-1.7\n"
    b"1 0 obj << /Type /Page /Parent 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Count 1 >> endobj\n"
    b"3 0 obj << /Type /Sig /SubFilter /adbe.pkcs7.detached "
    b"/ByteRange [0 100 200 300] >> endobj\n"
    b"trailer << /Root 1 0 R >>\n%%EOF\n"
)

UNSIGNED_PDF = (
    b"%PDF-1.7\n"
    b"1 0 obj << /Type /Page /Parent 2 0 R >> endobj\n"
    b"2 0 obj << /Type /Pages /Count 1 >> endobj\n"
    b"trailer << /Root 1 0 R >>\n%%EOF\n"
)


@pytest.fixture
def signed_pdf(tmp_path):
    path = tmp_path / "invoice.pdf"
    path.write_bytes(SIGNED_PDF)
    return path


@pytest.fixture
def unsigned_pdf(tmp_path):
    path = tmp_path / "unsigned.pdf"
    path.write_bytes(UNSIGNED_PDF)
    return path


EMPTY_HOME_HTML = """<!doctype html>
<html><body>
<div id="docs_error"></div>
<form id="form1" action="/esanchit/home" method="POST" enctype="multipart/form-data">
  <input id="tabId" name="tabId" type="hidden" value="TAB0123456789"/>
  <input type="file" name="docs" multiple="multiple" id="fileIn"/>
  <h4>Upload Document Details</h4>
  <ol>
    <li>Maximum size per file is 2 MB.</li>
    <li>The total number of maximum files are 5 per DRN.</li>
  </ol>
  <table>
    <thead><tr>
      <th>File Name</th><th>DRN</th><th>IRN</th><th>Start Date</th>
      <th>Digital Sign Validated</th><th>Document Type</th><th>Action</th>
    </tr></thead>
    <tr class="trBlankRow"><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
  </table>
</form>
</body></html>
"""

# Mirrors the markup the portal returned after a real upload on 2026-10-28:
# read-only inputs for DRN/IRN/start date/signature, and a plain select that
# is only read by submitDocs.
STAGED_HOME_HTML = """<!doctype html>
<html><body>
<div id="docs_error"></div>
<form id="form1" action="/esanchit/home" method="POST" enctype="multipart/form-data">
  <input id="tabId" name="tabId" type="hidden" value="TAB0123456789"/>
  <input type="file" name="docs" multiple="multiple" id="fileIn"/>
  <h4>Upload Document Details</h4>
  <ol>
    <li>Maximum size per file is 2 MB.</li>
    <li>The total number of maximum files are 5 per DRN.</li>
  </ol>
  <table>
    <thead><tr>
      <th>File Name</th><th>DRN</th><th>IRN</th><th>Start Date</th>
      <th>Digital Sign Validated</th><th>Document Type</th><th>Action</th>
    </tr></thead>
    <tr class="trBlankRow"><td></td><td></td><td></td><td></td><td></td><td></td><td></td></tr>
    <tr>
      <td><a href="viewUploadDocument_0"><textarea id="fileName_0" readonly="readonly">invoice.pdf</textarea></a></td>
      <td><input id="drn_0" type="text" readonly="readonly" value="{drn}"/></td>
      <td><input id="irn_0" type="text" readonly="readonly" value="{irn}"/></td>
      <td><input id="startDate_0" type="text" disabled="disabled" value="2026-10-28 21:39:15"/></td>
      <td><input id="digiSignValidated_0_0" type="text" disabled="disabled" value="{signature}"/></td>
      <td>
        <select id="docTypeSelect_0" name="detailBean[0].docTyp" class="dropdown1">
          <option value="NONE">--- Select ---</option>
          <option value="101000-Registration Document">101000-Registration Document</option>
          <option value="331000-Commercial invoice">331000-Commercial invoice</option>
        </select>
      </td>
      <td><input type="checkbox" name="deleteRecord_0"/></td>
    </tr>
  </table>
</form>
</body></html>
"""


def staged_html(drn: str = "", irn: str = "", signature: str = "") -> str:
    return STAGED_HOME_HTML.format(drn=drn, irn=irn, signature=signature)


def make_signed_pdf() -> bytes:
    """Build a one-page PDF with a real, verifiable self-signed signature.

    The fake ``SIGNED_PDF`` above only carries markers; this one is used to
    exercise the pyHanko path without shipping a fixture binary.
    """
    import datetime
    import io

    from asn1crypto import keys as asn1keys
    from asn1crypto import x509 as asn1x509
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from pyhanko.pdf_utils import generic, writer
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pyhanko.sign.fields import SigFieldSpec, append_signature_field
    from pyhanko_certvalidator.registry import SimpleCertificateStore

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TEST SIGNER")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    asn1cert = asn1x509.Certificate.load(cert.public_bytes(serialization.Encoding.DER))
    asn1key = asn1keys.PrivateKeyInfo.load(
        key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    blank = writer.PdfFileWriter()
    blank.insert_page(
        writer.PageObject(contents=generic.ArrayObject(), media_box=(0, 0, 595, 842))
    )
    buffer = io.BytesIO()
    blank.write(buffer)

    incremental = IncrementalPdfFileWriter(io.BytesIO(buffer.getvalue()))
    append_signature_field(incremental, SigFieldSpec("Sig1", box=(36, 36, 300, 100)))
    signer = signers.SimpleSigner(
        signing_cert=asn1cert,
        signing_key=asn1key,
        cert_registry=SimpleCertificateStore.from_certs([asn1cert]),
    )
    out = io.BytesIO()
    signers.sign_pdf(
        incremental,
        signers.PdfSignatureMetadata(field_name="Sig1"),
        signer=signer,
        output=out,
    )
    return out.getvalue()


@pytest.fixture(scope="session")
def real_signed_pdf_bytes():
    return make_signed_pdf()


@pytest.fixture
def real_signed_pdf(tmp_path, real_signed_pdf_bytes):
    path = tmp_path / "real-signed.pdf"
    path.write_bytes(real_signed_pdf_bytes)
    return path
