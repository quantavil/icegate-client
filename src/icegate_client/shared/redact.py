"""Secret redaction for logs, journals, exceptions and console output.

Two layers:

1. Exact values registered at runtime (dashboard token, session id, tab id).
2. Pattern based scrubbing for anything that looks like a bearer value, an
   OTP, a signed handoff URL or a long opaque blob.

The pattern layer intentionally keeps all-hex strings of 64 characters or
fewer, because file SHA-256 digests are useful in the journal and are not
secrets.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

REDACTED = "<redacted>"

_URL_RE = re.compile(r"https?://[^\s\"'<>`]+", re.IGNORECASE)
_KEY_VALUE_RE = re.compile(
    r"(?i)\b(token|session[_-]?id|authorization|cookie|otp|ticket|dm_ticket|password|passwd)\b"
    r"(\s*[:=]\s*)"
    r"(?:(?:bearer|basic)\s+)?"
    r"(\"[^\"]*\"|'[^']*'|[^\s&\"'<>,;}\]]+)"
)
_OPAQUE_RE = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
_OTP_CONTEXT_RE = re.compile(
    r"(?i)\b(otp|one[-\s]?time\s+password)\b[^\d]{0,24}(\d{4,8})\b"
)


def safe_url(url: str) -> str:
    """Return ``scheme://host/path`` and drop the query and fragment.

    eSANCHIT handoff URLs carry a bearer ``DM_TICKET`` in the query string, so
    the query is never safe to keep.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return REDACTED
    if not parts.scheme or not parts.netloc:
        return REDACTED
    return f"{parts.scheme}://{parts.netloc}{parts.path or '/'}"


def query_keys(url: str) -> list[str]:
    """Return sorted query parameter names only, never their values."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return []
    if not parts.query:
        return []
    keys = []
    for pair in parts.query.split("&"):
        if pair:
            keys.append(pair.split("=", 1)[0])
    return sorted(set(keys))


def _opaque(match: re.Match[str]) -> str:
    value = match.group(0)
    # Keep short hex digests (SHA-256 of a file is 64 hex characters).
    if len(value) <= 64 and _HEX_RE.match(value):
        return value
    # Keep separator lines such as "------" from table rendering.
    if len(set(value)) == 1:
        return value
    # Keep filesystem paths and key=value fragments; they are not bearer
    # values and mangling them makes diagnostics unreadable.
    before = match.string[:match.start()]
    after = match.string[match.end():]
    # Recognize complete filesystem paths and filenames, not arbitrary blobs.
    if ("/" in value and (value.startswith("/") or before.endswith(("/", "~/", "./")))):
        return value
    if re.match(r"\.[A-Za-z0-9]{1,8}(?:\b|$)", after):
        return value
    if "=" in value.rstrip("="):
        return value
    return REDACTED


def _key_value(match: re.Match[str]) -> str:
    quoted = match.group(3)
    if quoted[:1] in {'"', "'"}:
        return f"{match.group(1)}{match.group(2)}{quoted[0]}{REDACTED}{quoted[0]}"
    return f"{match.group(1)}{match.group(2)}{REDACTED}"


class Redactor:
    """Stateful scrubber that also removes exact runtime secrets."""

    def __init__(self, minimum_exact_length: int = 8) -> None:
        self._exact: set[str] = set()
        self._minimum = minimum_exact_length

    def remember(self, *values: str | None) -> None:
        for value in values:
            if value and len(value) >= self._minimum:
                self._exact.add(value)

    def forget_all(self) -> None:
        self._exact.clear()

    def scrub(self, value: str) -> str:
        if not value:
            return value
        text = value
        for secret in sorted(self._exact, key=len, reverse=True):
            if secret in text:
                text = text.replace(secret, REDACTED)
        text = _URL_RE.sub(lambda m: safe_url(m.group(0)), text)
        text = _KEY_VALUE_RE.sub(_key_value, text)
        text = _OPAQUE_RE.sub(_opaque, text)
        text = _OTP_CONTEXT_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
        return text

    def scrub_obj(self, value):
        """Recursively scrub strings inside dicts, lists and tuples."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {self.scrub(str(k)): self.scrub_obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub_obj(v) for v in value]
        if isinstance(value, (bytes, bytearray)):
            return f"<{len(value)} bytes>"
        return value


# Process-wide redactor. Sessions register their live secrets here so that
# every module benefits without threading an object through every call.
REDACTOR = Redactor()


def scrub(value: str) -> str:
    return REDACTOR.scrub(value)


def scrub_obj(value):
    return REDACTOR.scrub_obj(value)


def remember(*values: str | None) -> None:
    REDACTOR.remember(*values)
