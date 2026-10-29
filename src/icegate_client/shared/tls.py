"""TLS trust configuration.

``www.icegate.gov.in`` sends only its leaf certificate and omits the issuing
intermediate, so a stock ``certifi`` bundle cannot verify it. The fix is to
append the missing public intermediate to ``certifi``. Certificate
verification is never disabled.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

import certifi

CERT_DIR = Path(__file__).resolve().parent.parent / "certs"
BUNDLE_NAME = "ca-bundle.pem"


def bundled_certificates() -> list[Path]:
    if not CERT_DIR.is_dir():
        return []
    return sorted(CERT_DIR.glob("*.pem"))


def _compose() -> str:
    parts = [Path(certifi.where()).read_text(encoding="utf-8").strip()]
    for path in bundled_certificates():
        parts.append(path.read_text(encoding="utf-8").strip())
    return "\n".join(parts) + "\n"


def _write_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".ca-", suffix=".pem")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def build_ca_bundle(state_dir: Path | str) -> Path:
    """Return a verified CA bundle: certifi plus the eSANCHIT intermediate.

    The bundle is rebuilt on every call so that a ``certifi`` upgrade cannot
    leave a stale copy behind.
    """
    content = _compose()
    try:
        target = Path(state_dir).expanduser() / BUNDLE_NAME
        _write_atomic(target, content)
        return target
    except OSError:
        # Read-only state directory: fall back to a per-process temp file.
        fd, temp_name = tempfile.mkstemp(prefix="icegate-ca-", suffix=".pem")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        return Path(temp_name)


def describe() -> dict[str, object]:
    return {
        "certifi": certifi.where(),
        "certifi_version": getattr(certifi, "__version__", "unknown"),
        "pinned": [p.name for p in bundled_certificates()],
    }
