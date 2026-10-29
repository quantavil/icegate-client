from __future__ import annotations

from icegate_client.shared.redact import Redactor, query_keys, safe_url, scrub


def test_safe_url_drops_query_and_fragment():
    url = "https://www.icegate.gov.in/esanchit/?token=DM_TICKET=abc123&x=1#frag"
    assert safe_url(url) == "https://www.icegate.gov.in/esanchit/"


def test_query_keys_never_returns_values():
    keys = query_keys("https://host/p?token=SECRET&id=2")
    assert keys == ["id", "token"]


def test_dm_ticket_is_removed():
    text = "handoff https://www.icegate.gov.in/esanchit?token=DM_TICKET=eyJhbGciOiJIUzI1NiJ9"
    cleaned = scrub(text)
    assert "DM_TICKET" not in cleaned
    assert "eyJhbGci" not in cleaned


def test_key_value_forms_are_scrubbed():
    for text in [
        "Authorization: Bearer abcdefghijklmnop",
        "session_id=0123456789abcdef0123456789abcdef",
        "otp=123456",
        "password=hunter2hunter2",
    ]:
        cleaned = scrub(text)
        assert "<redacted>" in cleaned


def test_sha256_digest_is_kept():
    digest = "a" * 64
    assert scrub(f"sha256 {digest}") == f"sha256 {digest}"


def test_long_opaque_blob_is_scrubbed():
    blob = "AbC" * 40
    assert blob not in scrub(blob)


def test_exact_runtime_secret_is_removed_everywhere():
    redactor = Redactor()
    redactor.remember("SESSIONID0123456789ABCDEF01234567")
    assert "SESSIONID" not in redactor.scrub("prefix SESSIONID0123456789ABCDEF01234567 suffix")


def test_scrub_obj_walks_containers():
    redactor = Redactor()
    redactor.remember("tokentokentokentoken")
    value = {"a": ["tokentokentokentoken", 1], "b": {"c": "tokentokentokentoken"}}
    cleaned = redactor.scrub_obj(value)
    assert "tokentokentokentoken" not in str(cleaned)
    assert cleaned["a"][1] == 1


def test_otp_context_is_scrubbed():
    assert "123456" not in scrub("Your OTP is 123456 for ICEGATE")


def test_bytes_are_replaced_by_length():
    assert scrub_obj_bytes() == "<5 bytes>"


def test_filesystem_paths_are_preserved():
    for path in [
        "/home/example/icegate-client/.env",
        "/tmp/opencode/icegate-client-state/ca-bundle.pem",
        "~/.config/example/.env",
    ]:
        assert scrub(path) == path


def scrub_obj_bytes():
    return Redactor().scrub_obj(b"hello")


def test_preserves_receipt_path_and_long_filename():
    from icegate_client.shared.redact import scrub
    path = '/home/test/.local/state/icegate-client/receipts/123456789abc.json'
    name = 'INVOICE_PACKING_LIST_CUSTOMER_REFERENCE_1234567890.pdf'
    assert scrub(path) == path
    assert scrub(name) == name
