"""Single-attempt submission of staged documents.

The portal generates DRN and IRN only when ``submitDocs`` is posted, and it
reads the document type from that same request. There is no undo, so this
module is deliberately paranoid:

* the plan (file hashes, sizes, document codes) exists before any network I/O;
* the user must type ``SUBMIT <n> <plan_id>``, which proves they approved that
  exact plan and not a later state of the files;
* a lock file prevents two processes from submitting at once;
* the staged rows are re-checked against the plan before the POST;
* an ambiguous result (timeout, 5xx, HTTP 200 without a DRN) is reported as
  ``unknown`` and never retried.

The request and response parsing were verified for one two-PDF batch on
2026-10-29. Other response shapes can still yield an unknown outcome.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from filelock import FileLock, Timeout

from ..shared.errors import (
    DuplicateSubmit,
    SubmitAborted,
    SubmitBusy,
    SubmitError,
)
from ..shared.files import write_private as _write_private
from ..shared.files import write_receipt
from ..shared.journal import Journal, now_iso
from .client import EsanchitClient
from .page import HomePage

# Only ``submission.py`` may reach this action.
SUBMIT_ALLOWLIST = frozenset({"submitDocs"})


class SubmitOutcome(str, Enum):
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FileDigest:
    path: Path
    name: str
    size: int
    sha256: str
    contents: bytes = field(repr=False)
    document_code: str = ""


@dataclass(frozen=True)
class SubmitPlan:
    files: tuple[FileDigest, ...]
    plan_id: str

    @classmethod
    def build(cls, paths: list[Path], codes: dict[str, str]) -> SubmitPlan:
        digests: list[FileDigest] = []
        for raw in paths:
            path = Path(raw)
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise SubmitError(f"{path.name}: could not read file") from exc
            code = codes.get(path.name, "").strip()
            if not code:
                raise SubmitError(f"{path.name}: a document code is required to submit")
            digests.append(
                FileDigest(
                    path=path,
                    name=path.name,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    contents=data,
                    document_code=code,
                )
            )
        if not digests:
            raise SubmitError("no files given")
        payload = json.dumps(
            [[item.name, item.size, item.sha256, item.document_code] for item in digests],
            separators=(",", ":"),
        )
        return cls(tuple(digests), hashlib.sha256(payload.encode()).hexdigest()[:12])

    @property
    def challenge(self) -> str:
        return f"SUBMIT {len(self.files)} {self.plan_id}"


@dataclass
class Receipt:
    plan_id: str
    outcome: SubmitOutcome
    http_status: int | None = None
    detail: str = ""
    files: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    at: str = ""

    @property
    def submitted(self) -> bool:
        return self.outcome is SubmitOutcome.SUBMITTED

    @property
    def batch_drn(self) -> str:
        """The DRN shared by every row, or ``""`` when the portal disagrees.

        One submission produces one DRN for the whole batch; the IRNs are per
        document. A response whose rows carry different DRNs does not match
        that model, so it is reported separately instead of being averaged
        away.
        """
        drns = {row.get("drn", "") for row in self.rows if row.get("drn")}
        return drns.pop() if len(drns) == 1 else ""

    @property
    def irns(self) -> list[str]:
        return [row["irn"] for row in self.rows if row.get("irn")]

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "outcome": self.outcome.value,
            "submitted": self.submitted,
            "drn": self.batch_drn,
            "http_status": self.http_status,
            "detail": self.detail,
            "files": self.files,
            "rows": self.rows,
            "at": self.at,
        }


class SubmissionService:
    def __init__(self, client: EsanchitClient, journal: Journal, state_dir: Path | str) -> None:
        self.client = client
        self.journal = journal
        self.state_dir = Path(state_dir).expanduser()
        self.receipts_dir = self.state_dir / "receipts"
        self.lock_path = self.state_dir / "submit.lock"

    # ------------------------------------------------------------------ paths
    def receipt_path(self, plan_id: str) -> Path:
        return self.receipts_dir / f"{plan_id}.json"

    # ------------------------------------------------------------------ entry
    def submit(
        self,
        plan: SubmitPlan,
        *,
        confirmation: str,
        page: HomePage,
        allow_duplicate: bool = False,
        dump_html: Path | None = None,
    ) -> Receipt:
        if (confirmation or "").strip() != plan.challenge:
            raise SubmitError(
                "confirmation does not match this plan; nothing was sent. "
                f"Re-run with --confirm '{plan.challenge}'"
            )

        receipt_path = self.receipt_path(plan.plan_id)
        if receipt_path.exists() and not allow_duplicate:
            raise DuplicateSubmit(
                f"a receipt for plan {plan.plan_id} already exists at {receipt_path}; "
                "submitting again would create a second DRN (pass --allow-duplicate "
                "only if you are certain)"
            )

        lock = FileLock(str(self.lock_path), timeout=0)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            lock.acquire()
        except Timeout as exc:
            raise SubmitBusy("another submission is in progress") from exc
        try:
            if receipt_path.exists() and not allow_duplicate:
                raise DuplicateSubmit(f"a receipt for plan {plan.plan_id} already exists; refusing to submit again")
            return self._submit_locked(
                plan,
                page=page,
                dump_html=dump_html,
                allow_duplicate=allow_duplicate,
            )
        finally:
            lock.release()

    # ------------------------------------------------------------------- core
    def _submit_locked(
        self,
        plan: SubmitPlan,
        *,
        page: HomePage,
        dump_html: Path | None,
        allow_duplicate: bool,
    ) -> Receipt:
        self.journal.add(
            "submit_intent",
            plan_id=plan.plan_id,
            files=[
                {"name": f.name, "bytes": f.size, "sha256": f.sha256, "code": f.document_code}
                for f in plan.files
            ],
            allow_duplicate=allow_duplicate,
        )

        # Files are re-read here: never trust the plan object to match the disk.
        for item in plan.files:
            if len(item.contents) != item.size or hashlib.sha256(item.contents).hexdigest() != item.sha256:
                raise SubmitAborted(f"{item.name}: planned contents no longer match the approved hash")
            try:
                current_hash = hashlib.sha256(item.path.read_bytes()).hexdigest()
            except OSError as exc:
                raise SubmitAborted(f"{item.name}: could not re-read file") from exc
            if current_hash != item.sha256:
                raise SubmitAborted(
                    f"{item.name} changed after the plan was built; refusing to submit"
                )

        staged = self.client.stage(
            [item.path for item in plan.files], page=page,
            file_contents={item.path: item.contents for item in plan.files},
        )
        self._check_rows(plan, staged)

        fields = self._submit_fields(plan, staged)
        if dump_html is not None:
            _write_private(dump_html, staged.html)

        # Persist an uncertain outcome before dispatch: a crash at any point
        # after this must block an automatic re-run of the same plan.
        self._persist(self._receipt(
            plan, SubmitOutcome.UNKNOWN,
            detail="Submission may have been dispatched; reconcile with ICEGATE before retrying.",
        ), pending=True)
        try:
            response = self.client.post_form(
                "submitDocs", data=fields, allowlist=SUBMIT_ALLOWLIST
            )
        except Exception as exc:  # noqa: BLE001 - dispatch may have reached the portal
            receipt = self._receipt(
                plan,
                SubmitOutcome.UNKNOWN,
                detail=(
                    f"{type(exc).__name__} after the submit request was sent; the "
                    "outcome is unknown. Check the portal before doing anything else."
                ),
            )
            self._persist(receipt)
            return receipt

        try:
            receipt = self._interpret(plan, response)
        except Exception:  # noqa: BLE001 - response parsing failed after dispatch; do not retry
            receipt = self._receipt(
                plan, SubmitOutcome.UNKNOWN, status=getattr(response, "status_code", None),
                detail="Could not interpret submit response; reconcile before retrying.",
            )
        self._persist(receipt)
        return receipt

    def _check_rows(self, plan: SubmitPlan, staged: HomePage) -> None:
        expected = [item.name for item in plan.files]
        actual = [row.file_name for row in staged.rows]
        if len(actual) != len(expected) or sorted(map(str.lower, actual)) != sorted(
            map(str.lower, expected)
        ):
            raise SubmitAborted(
                "eSANCHIT does not show exactly the planned files "
                f"(planned {expected}, staged {actual}); nothing was submitted. "
                "Run a fresh handoff and try again."
            )
        for row in staged.rows:
            code = next(
                (f.document_code for f in plan.files if f.name.lower() == row.file_name.lower()),
                "",
            )
            if row.option_for(code) is None:
                raise SubmitAborted(
                    f"{row.file_name}: document code {code} is not offered for this row; "
                    "nothing was submitted"
                )

    def _submit_fields(self, plan: SubmitPlan, staged: HomePage) -> dict[str, str]:
        fields = dict(staged.hidden_fields)
        if staged.tab_id:
            fields["tabId"] = staged.tab_id
        fields.setdefault("finalSubmit", "Submit")
        used_names = set()
        for row in staged.rows:
            name = row.select_name or ""
            if not re.fullmatch(r"detailBean\[(?:0|[1-9][0-9]*)\]\.docTyp", name):
                raise SubmitAborted(f"{row.file_name}: missing or unsafe document type field; nothing submitted")
            if name in used_names:
                raise SubmitAborted(f"{row.file_name}: duplicate document type field; nothing submitted")
            used_names.add(name)
            code = next(
                (f.document_code for f in plan.files if f.name.lower() == row.file_name.lower()),
                "",
            )
            option = row.option_for(code)
            fields[name] = option[0] if option else ""
        return fields

    def _interpret(self, plan: SubmitPlan, response) -> Receipt:
        status = getattr(response, "status_code", None)
        page = HomePage.parse(getattr(response, "text", "") or "")

        rows = [
            {
                "file": row.file_name,
                "drn": row.drn,
                "irn": row.irn,
                "signature": row.sign_status,
                "document_type": row.doc_type_code,
            }
            for row in page.rows
        ]

        if status == 200:
            if any(row["drn"] or row["irn"] for row in rows):
                names = sorted(row["file"].lower() for row in rows)
                expected = sorted(item.name.lower() for item in plan.files)
                drns = {row["drn"] for row in rows}
                irns = [row["irn"] for row in rows]
                if (not page.errors and names == expected and len(drns) == 1
                        and all(re.fullmatch(r"[0-9]{16}", drn) for drn in drns)
                        and all(re.fullmatch(r"[0-9]{16}", irn) for irn in irns)
                        and len(set(irns)) == len(irns)
                        and all(row.verified for row in page.rows)):
                    return self._receipt(plan, SubmitOutcome.SUBMITTED, status=status, rows=rows)
                return self._receipt(
                    plan, SubmitOutcome.UNKNOWN, status=status, rows=rows,
                    detail="Incomplete or inconsistent submit response; reconcile before retrying.",
                )
            if page.errors:
                return self._receipt(
                    plan,
                    SubmitOutcome.UNKNOWN,
                    status=status,
                    rows=rows,
                    detail="; ".join(page.errors)[:400],
                )
            return self._receipt(
                plan,
                SubmitOutcome.UNKNOWN,
                status=status,
                rows=rows,
                detail=(
                    "HTTP 200 but no DRN or IRN was parsed. Check the portal "
                    "and confirmation email before retrying."
                ),
            )

        if status is not None and 400 <= status < 500:
            return self._receipt(
                plan,
                SubmitOutcome.UNKNOWN,
                status=status,
                detail=f"HTTP {status} after submit; reconcile before retrying",
            )

        return self._receipt(
            plan,
            SubmitOutcome.UNKNOWN,
            status=status,
            detail=f"HTTP {status} from submitDocs; the outcome is unknown",
        )

    # ---------------------------------------------------------------- receipts
    def _receipt(
        self,
        plan: SubmitPlan,
        outcome: SubmitOutcome,
        *,
        status: int | None = None,
        detail: str = "",
        rows: list[dict] | None = None,
    ) -> Receipt:
        return Receipt(
            plan_id=plan.plan_id,
            outcome=outcome,
            http_status=status,
            detail=detail,
            files=[
                {"name": f.name, "bytes": f.size, "sha256": f.sha256, "code": f.document_code}
                for f in plan.files
            ],
            rows=rows or [],
            at=now_iso(),
        )

    def _persist(self, receipt: Receipt, *, pending: bool = False) -> None:
        write_receipt(self.receipt_path(receipt.plan_id), receipt.as_dict())
        self.journal.add("submit_pending" if pending else "submit_result", **receipt.as_dict())
