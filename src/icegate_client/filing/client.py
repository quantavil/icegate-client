"""Single-attempt upload of already-signed electronic filing messages.

Portal upload acceptance means only that the file was accepted for processing;
the subsequent ICEGATE/ICES outcome is separate. SB/BE signature tags are
checked for structure only: no local cryptographic signature or DSC trust check
is done.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from filelock import FileLock, Timeout
from requests.exceptions import RequestException

from ..shared.errors import DuplicateSubmit, IcegateError, SubmitBusy
from ..shared.journal import Journal, now_iso
from ..shared.session import IcegateSession


class FilingError(IcegateError):
    """The electronic filing plan cannot be used."""


class FilingOutcome(str, Enum):
    ACCEPTED_FOR_PROCESSING = "accepted_for_processing"
    PORTAL_REPORTED_FAILURE = "portal_reported_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FilingFile:
    path: Path
    name: str
    sha256: str
    contents: bytes = field(repr=False)


@dataclass(frozen=True)
class FilingPlan:
    files: tuple[FilingFile, ...]
    plan_id: str

    @classmethod
    def build(cls, paths: list[Path | str]) -> FilingPlan:
        if not paths:
            raise FilingError("no files supplied")
        files: list[FilingFile] = []
        names: set[str] = set()
        hashes: set[str] = set()
        for raw in paths:
            path = Path(raw).expanduser()
            if path.suffix.lower() not in {".sb", ".be"}:
                raise FilingError(f"{path.name}: expected an already-signed .sb or .be file")
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise FilingError(f"{path.name}: could not read file") from exc
            if not _has_signature_sections(data):
                raise FilingError(f"{path.name}: missing signature or certificate sections")
            digest = hashlib.sha256(data).hexdigest()
            if path.name.casefold() in names or digest in hashes:
                raise FilingError(f"{path.name}: duplicate filename or contents in this plan")
            names.add(path.name.casefold())
            hashes.add(digest)
            files.append(FilingFile(path=path, name=path.name, sha256=digest, contents=data))
        encoded = json.dumps([[f.name, f.sha256] for f in files], separators=(",", ":"))
        return cls(files=tuple(files), plan_id=hashlib.sha256(encoded.encode()).hexdigest()[:12])

    @property
    def challenge(self) -> str:
        return f"FILE {len(self.files)} {self.plan_id}"


def _has_signature_sections(data: bytes) -> bool:
    # Structural check only. Presence of tags does not establish a valid DSC.
    for tag in (b"START-SIGNATURE", b"START-CERTIFICATE"):
        pattern = rb"<" + tag + rb">\s*[^<\s][^<]*</" + tag + rb">"
        if not re.search(pattern, data):
            return False
    return True


@dataclass(frozen=True)
class FilingReceipt:
    filename: str
    sha256: str
    size: int
    outcome: FilingOutcome
    http_status: int | None = None
    at: str = ""

    @property
    def final_customs_status_known(self) -> bool:
        return False

    def as_dict(self) -> dict:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "bytes": self.size,
            "outcome": self.outcome.value,
            "http_status": self.http_status,
            "final_customs_status_known": False,
            "at": self.at,
        }


class FilingService:
    def __init__(self, session: IcegateSession, journal: Journal, state_dir: Path | str):
        self.session = session
        self.journal = journal
        self.state_dir = Path(state_dir).expanduser()
        self.receipts_dir = self.state_dir / "filing" / "receipts"

    def receipt_path(self, sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise FilingError("invalid file digest")
        return self.receipts_dir / f"{sha256}.json"

    def submit(self, plan: FilingPlan, *, confirmation: str) -> list[FilingReceipt]:
        if (confirmation or "").strip() != plan.challenge:
            raise FilingError("confirmation does not match this filing plan; nothing was sent")
        if not self.session.token or not self.session.session_id:
            raise FilingError("ICEGATE session is not authenticated")
        for item in plan.files:
            if hashlib.sha256(item.contents).hexdigest() != item.sha256:
                raise FilingError(f"{item.name}: file contents do not match the plan")

        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(self.state_dir / "filing.lock"), timeout=0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise SubmitBusy("another electronic filing upload is in progress") from exc
        try:
            for item in plan.files:
                # Previous one-off research uploads used this state directory.
                legacy = self.state_dir / "filing-tests" / f"{item.sha256}.json"
                if self.receipt_path(item.sha256).exists() or legacy.exists():
                    raise DuplicateSubmit(
                        f"{item.name}: an attempt record already exists; reconcile before repeating"
                    )
            results: list[FilingReceipt] = []
            for item in plan.files:
                receipt = self._upload(item)
                results.append(receipt)
                if receipt.outcome is not FilingOutcome.ACCEPTED_FOR_PROCESSING:
                    break
            return results
        finally:
            lock.release()

    def _upload(self, item: FilingFile) -> FilingReceipt:
        pending = self._receipt(item, FilingOutcome.UNKNOWN)
        self._persist(pending)
        try:
            response = self.session.send(
                "POST",
                self.session.url("/filing/file"),
                files={"messageFile": (item.name, item.contents, "application/octet-stream")},
                headers=self.session.auth_headers(),
                timeout=120,
            )
        except (IcegateError, RequestException, OSError):
            return pending

        status = getattr(response, "status_code", None)
        try:
            body = response.json()
        except (ValueError, AttributeError):
            body = None
        validation = body.get("validationStatus") if isinstance(body, dict) else None
        if status is not None and 200 <= status < 300 and validation == "SUCCESS":
            outcome = FilingOutcome.ACCEPTED_FOR_PROCESSING
        elif status is not None and 200 <= status < 300 and validation == "FAILED":
            outcome = FilingOutcome.PORTAL_REPORTED_FAILURE
        else:
            outcome = FilingOutcome.UNKNOWN
        result = self._receipt(item, outcome, status=status)
        self._persist(result)
        return result

    @staticmethod
    def _receipt(item: FilingFile, outcome: FilingOutcome, *, status: int | None = None) -> FilingReceipt:
        return FilingReceipt(
            filename=item.name, sha256=item.sha256, size=len(item.contents),
            outcome=outcome, http_status=status, at=now_iso(),
        )

    def _persist(self, receipt: FilingReceipt) -> None:
        self.receipts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.receipt_path(receipt.sha256)
        fd, temporary = tempfile.mkstemp(dir=self.receipts_dir, prefix=".receipt-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(receipt.as_dict(), handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            directory_fd = os.open(self.receipts_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.journal.add("filing_result", **receipt.as_dict())
