"""Command line interface.

Every command logs in fresh. Dashboard tokens and eSANCHIT session cookies
are short-lived bearer values and are kept in memory only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import __version__
from .ecopy import ECopyCache, ECopyClient
from .enquiry import EnquiryClient
from .jobstatus import JobStatusClient
from .myiec import MyIecClient
from .esanchit.client import EsanchitClient
from .esanchit.doc_types import DocumentCatalog
from .esanchit.page import StagedRow, split_option
from .esanchit.pdf import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_FILES,
    Limits,
    limit_messages,
    preflight,
)
from .esanchit.submission import SubmissionService, SubmitOutcome, SubmitPlan
from .filing.client import FilingOutcome, FilingPlan, FilingService
from .shared.config import (
    Settings,
    default_env_file,
    mask_identifier,
    write_env_template,
)
from .shared.errors import ConfigError, IcegateError, PreflightError, StagingError
from .shared.files import write_private as _write_private
from .shared.journal import Journal
from .shared.redact import remember, scrub, scrub_obj
from .shared.session import IcegateSession
from .shared.tls import build_ca_bundle
from .shared.tls import describe as describe_tls


# --------------------------------------------------------------------- output
def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)).rstrip())
    return "\n".join(lines)


def _say(message: str = "", *, error: bool = False) -> None:
    print(scrub(message), file=sys.stderr if error else sys.stdout)


def _json(value, **kwargs) -> str:
    return json.dumps(scrub_obj(value), **kwargs)


def _row_cells(row: StagedRow, plan: dict[str, tuple[str, str]] | None = None) -> list[str]:
    cells = [row.file_name]
    if row.drn or row.irn:
        cells += [row.drn or "-", row.irn or "-"]
    cells += [
        row.start_date or "-",
        row.sign_status or "-",
        row.doc_type_label or "-",
    ]
    if plan is not None:
        planned = plan.get(row.file_name)
        cells.append(f"{planned[0]} {planned[1]}" if planned else "-")
    return cells


def _row_summary(
    row: StagedRow, plan: dict[str, tuple[str, str]] | None = None
) -> dict[str, object]:
    """Compact row for JSON output; the full option list is very long."""
    return {
        "file": row.file_name,
        "started": row.start_date,
        "drn": row.drn,
        "irn": row.irn,
        "signature": row.sign_status,
        "portal_type": row.doc_type_code,
        "portal_type_label": row.doc_type_label,
        "planned_type": (plan or {}).get(row.file_name, (None, None))[0],
    }


def _print_rows(rows: list[StagedRow], plan: dict[str, tuple[str, str]] | None = None) -> None:
    if not rows:
        _say("  (no staged rows)")
        return
    show_drn = any(row.drn or row.irn for row in rows)
    headers = ["file"]
    if show_drn:
        headers += ["DRN", "IRN"]
    headers += ["started", "signature", "portal type"]
    if plan is not None:
        headers.append("planned type (not saved)")
    _say(_table(headers, [_row_cells(row, plan) for row in rows]))


def _plan_doc_types(
    rows: list[StagedRow], mapping: dict[str, str]
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Check the requested codes against each row's own option list.

    The portal does not save a document type during staging: the select is a
    form field that is only read when ``submitDocs`` is posted. The plan is
    therefore recorded and shown, not written back.
    """
    plan: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for row in rows:
        code = mapping.get(row.file_name)
        if not code:
            continue
        option = row.option_for(code)
        if option is None:
            problems.append(
                f"{row.file_name}: code {code} is not offered for this row"
            )
            continue
        plan[row.file_name] = (code, split_option(*option)[1])
    return plan, problems


# ---------------------------------------------------------------------- setup
class Context:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.settings = Settings.load(
            env_file=args.env_file,
            state_dir=args.state_dir,
        )
        remember(self.settings.password)
        self.journal = Journal(
            self.settings.journal_path(),
            enabled=not args.no_journal,
            echo=args.verbose,
        )
        self.journal.add(
            "run",
            command=(getattr(args, "esanchit_command", None)
                     or getattr(args, "filing_command", None)
                     or getattr(args, "command", "unknown")),
            version=__version__,
            account=mask_identifier(self.settings.icegate_id),
        )
        self.session = IcegateSession(self.settings, self.journal)
        self.esanchit = EsanchitClient(self.session, self.journal)
        self.submission = SubmissionService(
            self.esanchit, self.journal, self.settings.state_dir
        )

    def authenticate(self) -> None:
        self.session.authenticate()

    def close(self) -> None:
        self.journal.add("run_end")


# -------------------------------------------------------------------- commands
def cmd_login(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    ctx.authenticate()
    probe = ctx.session.probe()
    if ctx.args.json:
        print(
            _json(
                {
                    "ok": True,
                    "account": mask_identifier(ctx.settings.icegate_id),
                    "role_id": ctx.session.role_id,
                    "role_name": ctx.session.role_name,
                    "user_type": ctx.session.user_type,
                    "token_expires": ctx.session.expires_at.isoformat()
                    if ctx.session.expires_at
                    else None,
                    "seconds_left": round(ctx.session.seconds_left() or 0),
                    "services": probe.service_count,
                },
                indent=2,
            )
        )
        return 0

    _say("ICEGATE login ok")
    _say(f"  account      {mask_identifier(ctx.settings.icegate_id)}")
    _say(f"  role         {ctx.session.role_id} {ctx.session.role_name}")
    _say(f"  user type    {ctx.session.user_type}")
    _say(f"  dashboard    {probe.service_count} services")
    left = int(ctx.session.seconds_left() or 0)
    _say(f"  token        {left // 60}m {left % 60:02d}s left")
    return 0


def cmd_status(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    ctx.authenticate()
    before = int(ctx.session.seconds_left() or 0)
    refreshed = ctx.session.refresh_if_expiring(margin=10_000)
    after = int(ctx.session.seconds_left() or 0)
    probe = ctx.session.probe()
    if ctx.args.json:
        print(
            _json(
                {
                    "ok": probe.ok,
                    "services": probe.service_count,
                    "refreshed": refreshed,
                    "seconds_before": before,
                    "seconds_after": after,
                },
                indent=2,
            )
        )
        return 0 if probe.ok else 1
    _say("session ok" if probe.ok else "session rejected")
    _say(f"  dashboard    {probe.service_count} services")
    _say(f"  token        {after // 60}m {after % 60:02d}s left")
    _say(f"  refresh      {'rotated' if refreshed else 'not needed'}")
    return 0 if probe.ok else 1


def cmd_handoff(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    ctx.authenticate()
    ctx.session.refresh_if_expiring(120)
    ticket = ctx.session.esanchit_ticket()
    page = ctx.esanchit.open_from_ticket(ticket)
    if ctx.args.json:
        print(
            _json(
                {
                    "ok": True,
                    "rows": [_row_summary(row) for row in page.rows],
                    "limits": page.describe_limits(),
                },
                indent=2,
            )
        )
        return 0
    _say("eSANCHIT session open")
    _say("  page          Upload Document Details")
    _say(f"  limits        {page.describe_limits()}")
    _say(f"  staged rows   {len(page.rows)}")
    _print_rows(page.rows)
    return 0


def _doc_type_mapping(args: argparse.Namespace, files: list[Path], catalog: DocumentCatalog) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if args.doc_type:
        mapping.update({path.name: args.doc_type for path in files})
    for pair in args.doc_type_map or []:
        if "=" not in pair:
            raise ConfigError(f"--doc-type-map expects FILE=CODE, got {pair!r}")
        name, code = pair.split("=", 1)
        name = name.strip()
        if name not in {path.name for path in files}:
            raise ConfigError(f"--doc-type-map file not found in input: {name}")
        mapping[name] = code.strip()
    for name, code in mapping.items():
        if catalog.get(code) is None:
            raise ConfigError(f"unknown document code {code!r} for {name}")
    return mapping


def _preflight_report(
    ctx: Context, files: list[Path], *, strict_signature: bool = False,
    file_contents: dict[Path, bytes] | None = None,
):
    return preflight(
        files,
        Limits(strict=ctx.args.strict_limits),
        allow_unsigned=ctx.args.allow_unsigned,
        allow_invalid=ctx.args.allow_invalid_signature,
        strict_signature=strict_signature,
        file_contents=file_contents,
    )


def _enforce_report(report) -> None:
    for warning in report.warnings:
        _say(f"warning: {warning}", error=True)
    for problem in report.problems:
        _say(f"error: {problem}", error=True)
    if not report.ok:
        raise PreflightError("preflight failed; nothing was sent")


def enforce_page_limits(page, report, *, strict: bool) -> None:
    """Re-apply the size and count rules with the live page's own numbers.

    Preflight runs before the login, so it can only use the documented
    defaults. The page is the authority, and one comparison is cheaper than an
    upload the portal will reject or truncate.
    """
    live = Limits(
        max_file_bytes=page.max_file_bytes or DEFAULT_MAX_FILE_BYTES,
        max_files=page.max_files or DEFAULT_MAX_FILES,
        total_bytes=page.max_request_bytes,
        strict=strict,
    )
    if (live.max_file_bytes, live.max_files, live.total_bytes) == (
        DEFAULT_MAX_FILE_BYTES,
        DEFAULT_MAX_FILES,
        None,
    ):
        return  # identical to what preflight already reported; do not repeat

    messages = limit_messages(report.items, live)
    for severity, message in messages:
        _say(f"{'error' if severity == 'problem' else 'warning'}: {message}", error=True)
    if strict and any(severity == "problem" for severity, _ in messages):
        raise PreflightError("the live portal limits are tighter; nothing was sent")


def cmd_stage(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    files = [Path(item).expanduser() for item in ctx.args.files]
    catalog = DocumentCatalog.load()
    mapping = _doc_type_mapping(ctx.args, files, catalog)

    snapshots = {}
    for path in files:
        try:
            snapshots[path] = path.read_bytes()
        except OSError as exc:
            raise PreflightError(f"{path.name}: could not read file") from exc
    report = _preflight_report(ctx, files, file_contents=snapshots)
    if not ctx.args.json:
        _say("preflight")
        _say(
            _table(
                ["file", "bytes", "pages", "signature", "sha256"],
                [
                    [
                        item.path.name,
                        f"{item.size:,}",
                        str(item.pages),
                        item.signature.describe(),
                        item.sha256[:12],
                    ]
                    for item in report.items
                ],
            )
        )
    _enforce_report(report)

    plan = {
        "files": [
            {
                "name": item.path.name,
                "bytes": item.size,
                "pages": item.pages,
                "signed": item.signed,
                "sha256": item.sha256,
                "document_code": mapping.get(item.path.name),
            }
            for item in report.items
        ],
        "dry_run": bool(ctx.args.dry_run),
        "limits": Limits(strict=ctx.args.strict_limits).describe(),
    }
    if ctx.args.dry_run:
        ctx.journal.add("stage_dry_run", **plan)
        if ctx.args.json:
            print(_json({"ok": True, "dry_run": True, **plan}, indent=2))
        else:
            _say("")
            _say("dry run: nothing was uploaded and nothing was submitted")
        return 0

    ctx.authenticate()
    ctx.session.refresh_if_expiring(120)
    ticket = ctx.session.esanchit_ticket()
    page = ctx.esanchit.open_from_ticket(ticket)

    if page.rows and not ctx.args.append:
        raise StagingError(
            f"eSANCHIT already shows {len(page.rows)} staged row(s); "
            "re-run with --append to add to them"
        )

    enforce_page_limits(page, report, strict=ctx.args.strict_limits)

    ctx.journal.add("stage_intent", **plan, existing_rows=len(page.rows))

    result = ctx.esanchit.stage(files, page=page, file_contents=snapshots)
    rows = result.rows
    planned: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    if mapping:
        planned, problems = _plan_doc_types(rows, mapping)
        for problem in problems:
            ctx.journal.add("doc_type_problem", detail=problem)

    if ctx.args.dump_html:
        _write_private(Path(ctx.args.dump_html).expanduser(), result.html)
        _say(f"html saved to {ctx.args.dump_html} (mode 0600, contains a session tabId)", error=True)

    ctx.journal.add(
        "stage_result",
        rows=[
            {
                "file": row.file_name,
                "drn": row.drn,
                "irn": row.irn,
                "signature": row.sign_status,
                "portal_type": row.doc_type_code,
                "planned_type": planned.get(row.file_name, (None, None))[0],
            }
            for row in rows
        ],
        problems=problems,
        session_scoped=True,
    )

    if ctx.args.json:
        print(
            _json(
                {
                    "ok": not problems,
                    "submitted": False,
                    "session_scoped": True,
                    "rows": [_row_summary(row, planned) for row in rows],
                    "planned_document_types": {
                        name: {"code": code, "label": label}
                        for name, (code, label) in planned.items()
                    },
                    "problems": problems,
                },
                indent=2,
            )
        )
        return 1 if problems else 0

    _say("")
    _say("staged in eSANCHIT (this session only)")
    _print_rows(rows, planned)
    _say("")
    _say("The portal creates DRN, IRN and the signature verdict only when it")
    _say("submits. The document type is a form field carried by that submit,")
    _say("so it is shown here as a plan and is not saved during staging.")
    _say("")
    _say(f"One submission covers all {len(rows)} row(s) and yields one DRN for the")
    _say("batch, with one IRN per document. Do not split a shipment across runs:")
    _say("each run would get its own DRN.")
    if problems:
        _say("")
        for problem in problems:
            _say(f"warning: {problem}")
    _say("")
    _say("The queue disappears when this eSANCHIT session ends.")
    _say("Nothing was submitted. Use `icegate submit` to finish this batch.")
    return 1 if problems else 0


def _plan_rows(plan: SubmitPlan, report) -> list[list[str]]:
    by_name = {item.path.name: item for item in report.items}
    rows = []
    for item in plan.files:
        info = by_name.get(item.name)
        rows.append(
            [
                item.name,
                f"{item.size:,}",
                str(info.pages) if info else "-",
                info.signature.describe() if info else "-",
                item.sha256[:12],
                item.document_code,
            ]
        )
    return rows


def cmd_submit(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    """Upload and submit in one process.

    Submission is the only irreversible action in the tool, so the plan is
    printed with a challenge string before anything is sent. The challenge
    covers the file hashes, sizes and document codes: re-signing or replacing
    a file between the two runs invalidates it.
    """
    files = [Path(item).expanduser() for item in ctx.args.files]
    catalog = DocumentCatalog.load()
    mapping = _doc_type_mapping(ctx.args, files, catalog)

    plan = SubmitPlan.build(files, mapping)
    report = _preflight_report(
        ctx, files, strict_signature=True,
        file_contents={item.path: item.contents for item in plan.files},
    )
    _enforce_report(report)

    challenge = plan.challenge
    confirmed = (ctx.args.confirm or "").strip() == challenge

    if ctx.args.dry_run or not confirmed:
        if ctx.args.json:
            print(
                _json(
                    {
                        "submitted": False,
                        "confirmed": False,
                        "dry_run": bool(ctx.args.dry_run),
                        "challenge": challenge,
                        "plan_id": plan.plan_id,
                        "files": [
                            {
                                "name": item.name,
                                "bytes": item.size,
                                "sha256": item.sha256,
                                "document_code": item.document_code,
                            }
                            for item in plan.files
                        ],
                    },
                    indent=2,
                )
            )
        else:
            _say("submit plan")
            _say(
                _table(
                    ["file", "bytes", "pages", "signature", "sha256", "code"],
                    _plan_rows(plan, report),
                )
            )
            _say("")
            _say(f"challenge   {challenge}")
            _say("nothing was sent. to submit, re-run the same command with:")
            _say(f"  --confirm '{challenge}'")
        return 0 if ctx.args.dry_run else 4

    ctx.authenticate()
    ctx.session.refresh_if_expiring(120)
    ticket = ctx.session.esanchit_ticket()
    page = ctx.esanchit.open_from_ticket(ticket)
    if page.rows:
        raise StagingError(
            f"eSANCHIT already shows {len(page.rows)} staged row(s) in this session; "
            "submit needs an empty queue. Run a fresh handoff and try again."
        )

    enforce_page_limits(page, report, strict=ctx.args.strict_limits)

    receipt = ctx.submission.submit(
        plan,
        confirmation=ctx.args.confirm,
        page=page,
        allow_duplicate=ctx.args.allow_duplicate,
        dump_html=Path(ctx.args.dump_html).expanduser() if ctx.args.dump_html else None,
    )
    receipt_file = ctx.submission.receipt_path(receipt.plan_id)

    if ctx.args.json:
        print(_json(receipt.as_dict(), indent=2))
    else:
        _say("")
        _say(f"outcome     {receipt.outcome.value}")
        if receipt.batch_drn:
            _say(
                f"DRN         {receipt.batch_drn}   "
                f"(one per batch, {len(receipt.rows)} document(s))"
            )
        for row in receipt.rows:
            _say(f"  {row['file']}  IRN {row['irn'] or '-'}")
        if receipt.detail:
            _say(f"detail      {receipt.detail}")
        _say(f"receipt     {receipt_file}")
        if receipt.outcome is SubmitOutcome.UNKNOWN:
            _say("")
            _say("The outcome is unknown. Check eSANCHIT before doing anything else:")
            _say("a retry could create a second DRN.")

    return {
        SubmitOutcome.SUBMITTED: 0,
        SubmitOutcome.UNKNOWN: 3,
    }[receipt.outcome]


def cmd_ecopy_search(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    args = ctx.args
    if args.relative_date:
        if args.start_date is not None or args.end_date is not None:
            raise IcegateError("relative date flags cannot be combined with --from or --to")
        selected = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        if args.relative_date == "yesterday":
            selected -= timedelta(days=1)
        args.start_date = args.end_date = selected
    date_search = args.start_date is not None or args.end_date is not None
    job_search = any(value is not None for value in (args.location, args.job_number, args.job_date))
    if date_search == job_search:
        raise IcegateError("provide either --from and --to, or --location, --job-number and --job-date")
    if date_search and (args.start_date is None or args.end_date is None):
        raise IcegateError("date search requires both --from and --to")
    if job_search and not all((args.location, args.job_number, args.job_date)):
        raise IcegateError("job search requires --location, --job-number and --job-date")
    if date_search and args.start_date > args.end_date:
        raise IcegateError("start date must not be after end date")
    ctx.authenticate()
    client = ECopyClient(ctx.session)
    if date_search:
        rows = client.search_by_date(args.kind, args.start_date, args.end_date)
    else:
        rows = client.search_by_job(args.kind, args.location, args.job_number, args.job_date)
    ECopyCache(ctx.settings.state_dir, ctx.settings.icegate_id).remember(rows)
    if args.json:
        print(_json(rows, indent=2))
    elif rows:
        keys = ["jobNumber", "jobDate", "docNumber", "docDate", "location"]
        _say(_table(["job", "job date", f"{args.kind} number", f"{args.kind} date", "location"],
                    [[str(row.get(key) or "") for key in keys] for row in rows]))
    else:
        _say("no records found")
    return 0


def cmd_report_job_status(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    ctx.authenticate()
    rows = JobStatusClient(ctx.session).detailed()
    if ctx.args.json:
        print(_json(rows, indent=2))
    elif rows:
        keys = list(rows[0])
        _say(_table(keys, [[str(row.get(key) or "") for key in keys] for row in rows]))
    else:
        _say("no records found")
    return 0


def cmd_my_iec(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    ctx.authenticate()
    rows = MyIecClient(ctx.session).list(ctx.args.iec, ctx.args.location)
    if ctx.args.json:
        print(_json(rows, indent=2))
    elif rows:
        keys = ["iecCode", "iecName", "locationCode"]
        _say(_table(["IEC", "name", "location"], [[str(row.get(key) or "") for key in keys] for row in rows]))
    else:
        _say("no records found")
    return 0


def cmd_enquiry_sb(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    args = ctx.args
    body = EnquiryClient(ctx.session).shipping_bill(args.location, args.sb_no, args.sb_date)
    if args.json:
        print(_json(body, indent=2))
    else:
        for section, rows in body.items():
            _say(f"{section}: {len(rows) if isinstance(rows, list) else rows}")
    return 0


def cmd_ecopy_download(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    args = ctx.args
    if args.copy_type == "ack":
        if not args.filename or args.irn or args.user_id:
            raise IcegateError("ack requires --filename, without --irn or --user-id")
    elif not args.irn or args.filename:
        raise IcegateError("signed download requires --irn, without --filename")
    if args.copy_type == "signed" and not args.user_id:
        args.user_id = ECopyCache(ctx.settings.state_dir, ctx.settings.icegate_id).user_id(args.irn)
        if not args.user_id:
            raise IcegateError("IRN not found in saved search results; run an ecopy search for it first")
    path = Path(args.output).expanduser()
    if path.exists():
        raise IcegateError(f"output file already exists: {path}")
    ctx.authenticate()
    client = ECopyClient(ctx.session)
    if args.copy_type == "ack":
        content = client.download_ack(args.filename)
    else:
        content = client.download_signed(args.irn, args.user_id)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(content)
    except FileExistsError as exc:
        raise IcegateError(f"output file already exists: {path}") from exc
    if args.json:
        print(_json({"saved": str(path), "bytes": len(content), "type": args.copy_type}, indent=2))
    else:
        _say(f"saved {len(content)} bytes to {path}")
    return 0


def cmd_filing_upload(ctx: Context, _args: argparse.Namespace | None = None) -> int:
    plan = FilingPlan.build(ctx.args.files)
    confirmed = (ctx.args.confirm or "").strip() == plan.challenge
    if ctx.args.dry_run or not confirmed:
        data = {
            "submitted": False,
            "signature_verified": False,
            "challenge": plan.challenge,
            "files": [
                {"name": item.name, "bytes": len(item.contents), "sha256": item.sha256}
                for item in plan.files
            ],
        }
        if ctx.args.json:
            print(_json(data, indent=2))
        else:
            _say("filing plan (signature structure checked; DSC not verified)")
            for item in plan.files:
                _say(f"  {item.name}  {len(item.contents):,} bytes  sha256 {item.sha256}")
            _say(f"challenge   {plan.challenge}")
            _say("nothing was sent; re-run with --confirm and this challenge to upload")
        return 0 if ctx.args.dry_run else 4

    ctx.authenticate()
    service = FilingService(ctx.session, ctx.journal, ctx.settings.state_dir)
    results = service.submit(plan, confirmation=ctx.args.confirm, allow_duplicate=ctx.args.allow_duplicate)
    if ctx.args.json:
        print(_json({"receipts": [item.as_dict() for item in results],
                          "not_attempted": len(plan.files) - len(results)}, indent=2))
    else:
        for item in results:
            _say(f"{item.filename}: {item.outcome.value} (HTTP {item.http_status or '-'})")
            _say(f"  receipt: {service.receipt_path(item.sha256)}")
        if len(results) < len(plan.files):
            _say(f"{len(plan.files) - len(results)} file(s) not attempted after an uncertain or failed response")
        _say("DSC signature was not verified locally; check the separate ICEGATE filing-status email.")
    if any(item.outcome is FilingOutcome.UNKNOWN for item in results):
        return 3
    if any(item.outcome is FilingOutcome.PORTAL_REPORTED_FAILURE for item in results):
        return 2
    return 0




def cmd_doc_types(ctx: Context | None, args: argparse.Namespace) -> int:
    catalog = DocumentCatalog.load()
    results = catalog.search(args.term or "", limit=args.limit)
    if args.json:
        print(_json([asdict(item) for item in results], indent=2))
        return 0
    if not results:
        _say("no matching document codes")
        return 1
    for item in results:
        _say(str(item))
    return 0


def cmd_doctor(ctx: Context | None, args: argparse.Namespace) -> int:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python", sys.version_info >= (3, 11), sys.version.split()[0]))

    settings = None
    try:
        settings = Settings.load(
            env_file=args.env_file,
            state_dir=args.state_dir,
        )
        checks.append(("config", True, settings.describe()))
    except ConfigError as exc:
        checks.append(
            (
                "config",
                False,
                f"{exc} (create {default_env_file()} with mode 0600)",
            )
        )

    try:
        bundle = build_ca_bundle(settings.state_dir if settings else Path.home() / ".local/state/icegate-client")
        checks.append(("ca bundle", bundle.is_file(), f"{bundle} ({describe_tls()['pinned']})"))
        checks.append(("tls verify", True, "certificate verification is always on"))
    except Exception as exc:  # noqa: BLE001 - unusual filesystem failure
        checks.append(("ca bundle", False, type(exc).__name__))

    if settings:
        writable = _can_write(settings.state_dir)
        checks.append(("state dir", writable, str(settings.state_dir)))

    if args.json:
        print(
            _json(
                [{"check": name, "ok": ok, "detail": detail} for name, ok, detail in checks],
                indent=2,
            )
        )
    else:
        for name, ok, detail in checks:
            _say(f"[{'ok' if ok else '!!'}] {name:<12} {detail}")

    return 0 if all(ok for _, ok, _ in checks) else 1


def _can_write(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def cmd_init(ctx: Context | None, args: argparse.Namespace) -> int:
    target = write_env_template(Path(args.path).expanduser() if args.path else None, force=args.force)
    _say(f"created {target}")
    _say("edit it and restrict access to your user account" if os.name == "nt"
         else "edit it, then run: chmod 600 " + str(target))
    return 0


# ---------------------------------------------------------------------- parser
def _date_arg(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from None


def _add_esanchit_commands(sub, common):
    sub.add_parser("handoff", parents=[common], help="open eSANCHIT and show staged rows").set_defaults(func=cmd_handoff)

    stage = sub.add_parser("stage", parents=[common], help="upload documents and stop before submit")
    stage.add_argument("files", nargs="+", help="already-signed PDF files")
    stage.add_argument("--type", "--doc-type", dest="doc_type", help="document code applied to every file")
    stage.add_argument("--doc-type-map", action="append", metavar="FILE=CODE", help="per-file document code")
    stage.add_argument("--dry-run", action="store_true", help="validate and stop before any network call")
    stage.add_argument("--append", action="store_true", help="allow uploading when rows are already staged")
    stage.add_argument("--allow-unsigned", action="store_true", help="do not fail on unsigned files")
    stage.add_argument(
        "--allow-invalid-signature",
        action="store_true",
        help="upload even when the signature does not verify",
    )
    stage.add_argument("--strict-limits", action="store_true", help="treat size and count limits as errors")
    stage.add_argument("--dump-html", metavar="PATH", help="save the post-upload page for inspection")
    stage.set_defaults(func=cmd_stage)

    submit = sub.add_parser(
        "submit",
        parents=[common],
        help="upload and submit in one run (irreversible; prints a challenge first)",
    )
    submit.add_argument("files", nargs="+", help="already-signed PDF files")
    submit.add_argument("--type", "--doc-type", dest="doc_type", help="document code applied to every file")
    submit.add_argument("--doc-type-map", action="append", metavar="FILE=CODE", help="per-file document code")
    submit.add_argument("--confirm", metavar="CHALLENGE", help="the exact challenge printed by the plan step")
    submit.add_argument("--dry-run", action="store_true", help="print the plan and challenge only")
    submit.add_argument("--allow-unsigned", action="store_true", help="do not fail on unsigned files")
    submit.add_argument(
        "--allow-invalid-signature",
        action="store_true",
        help="submit even when the signature does not verify",
    )
    submit.add_argument("--strict-limits", action="store_true", help="treat size and count limits as errors")
    submit.add_argument(
        "--allow-duplicate",
        action="store_true",
        help="submit even when a receipt for this exact plan exists",
    )
    submit.add_argument("--dump-html", metavar="PATH", help="save the post-upload page for inspection")
    submit.set_defaults(func=cmd_submit)

    codes = sub.add_parser("doc-types", parents=[common], help="search bundled eSANCHIT document codes")
    codes.add_argument("term", nargs="?", help="code or part of a name")
    codes.add_argument("--limit", type=int, default=20)
    codes.set_defaults(func=cmd_doc_types, no_context=True)



def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", help="path to a mode-0600 env file")
    common.add_argument("--state-dir", help="journal and CA cache directory")
    common.add_argument("--json", action="store_true", help="machine readable output")
    common.add_argument("--no-journal", action="store_true", help="do not write a journal")
    common.add_argument("-v", "--verbose", action="store_true", help="echo journal events to stderr")

    parser = argparse.ArgumentParser(
        prog="icegate",
        description="Unofficial browser-free ICEGATE client for eSANCHIT and electronic filing.",
    )
    parser.add_argument("--version", action="version", version=f"icegate-client {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", parents=[common], help="log in and verify the dashboard").set_defaults(func=cmd_login)
    sub.add_parser("status", parents=[common], help="log in, refresh and probe").set_defaults(func=cmd_status)
    esanchit = sub.add_parser("esanchit", help="eSANCHIT document operations")
    esanchit_sub = esanchit.add_subparsers(dest="esanchit_command", required=True)
    _add_esanchit_commands(esanchit_sub, common)

    filing = sub.add_parser("filing", help="electronic filing of signed SB/BE files")
    filing_sub = filing.add_subparsers(dest="filing_command", required=True)
    upload = filing_sub.add_parser("upload", parents=[common], help="upload signed filing files")
    upload.add_argument("files", nargs="+", help="already-signed .sb or .be files")
    upload.add_argument("--dry-run", action="store_true", help="inspect files without network access")
    upload.add_argument("--confirm", metavar="CHALLENGE", help="the exact challenge printed by the plan")
    upload.add_argument("--allow-duplicate", action="store_true", help="repeat an attempted file only after reconciliation")
    upload.set_defaults(func=cmd_filing_upload)

    report = sub.add_parser("report", help="read-only ICEGATE reports")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    job_status = report_sub.add_parser("job-status", parents=[common], help="Shipping Bill job status report (latest rows)")
    job_status.set_defaults(func=cmd_report_job_status)

    my_iec = sub.add_parser("my-iec", parents=[common], help="list IEC clients (read-only)")
    my_iec.add_argument("--iec", help="only this IEC code")
    my_iec.add_argument("--location", help="only this customs location code")
    my_iec.set_defaults(func=cmd_my_iec)

    enquiry = sub.add_parser("enquiry", help="public enquiries (no login)")
    enquiry_sub = enquiry.add_subparsers(dest="enquiry_command", required=True)
    enquiry_sb = enquiry_sub.add_parser("sb", parents=[common], help="Shipping Bill status, all sections")
    enquiry_sb.add_argument("--location", required=True, help="customs location code, e.g. INMBD6")
    enquiry_sb.add_argument("--sb-no", required=True, help="shipping bill number")
    enquiry_sb.add_argument("--sb-date", required=True, type=_date_arg, help="shipping bill date, YYYY-MM-DD")
    enquiry_sb.set_defaults(func=cmd_enquiry_sb)

    ecopy = sub.add_parser("ecopy", help="search Shipping Bill, Bill of Entry and gatepass records")
    ecopy_sub = ecopy.add_subparsers(dest="ecopy_command", required=True)
    def add_search_options(search):
        search.add_argument("--from", dest="start_date", type=_date_arg, help="start date, YYYY-MM-DD")
        search.add_argument("--to", dest="end_date", type=_date_arg, help="end date, YYYY-MM-DD")
        relative = search.add_mutually_exclusive_group()
        relative.add_argument("--today", dest="relative_date", action="store_const", const="today", help="search today's date in India")
        relative.add_argument("--yesterday", "--yestarday", dest="relative_date", action="store_const", const="yesterday", help="search yesterday's date in India")
        search.add_argument("--location", help="customs location code for a job search")
        search.add_argument("--job-number", help="job number for a job search")
        search.add_argument("--job-date", type=_date_arg, help="job date, YYYY-MM-DD")
        search.set_defaults(func=cmd_ecopy_search)

    search = ecopy_sub.add_parser("search", parents=[common], help="search by date range or job details")
    search.add_argument("kind", choices=["SB", "BE", "GATEPASS"], help="document type")
    add_search_options(search)
    for kind in ("SB", "BE", "GATEPASS"):
        shortcut = ecopy_sub.add_parser(kind.lower(), parents=[common], help=f"search {kind} records")
        add_search_options(shortcut)
        shortcut.set_defaults(kind=kind)

    download = ecopy_sub.add_parser("download", parents=[common], help="download a signed PDF or acknowledgement")
    download.add_argument("copy_type", nargs="?", choices=["signed", "ack"], default="signed")
    download.add_argument("--filename", help="downloadUrl filename for an acknowledgement")
    download.add_argument("--irn", help="IRN from an e-copy search")
    download.add_argument("--user-id", help="optional portal userId override; normally taken from search results")
    download.add_argument("-o", "--output", required=True, help="destination; existing files are not overwritten")
    download.set_defaults(func=cmd_ecopy_download)

    doctor = sub.add_parser("doctor", parents=[common], help="check configuration and TLS")
    doctor.set_defaults(func=lambda ctx, args: cmd_doctor(None, args))

    init = sub.add_parser("init", help="write a mode-0600 env file template")
    init.add_argument("path", nargs="?")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=lambda ctx, args: cmd_init(None, args))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx: Context | None = None
    try:
        if args.command not in {"doctor", "init"} and not getattr(args, "no_context", False):
            ctx = Context(args)
        code = args.func(ctx, args)
        return int(code or 0)
    except IcegateError as exc:
        _say(f"error: {exc}", error=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - avoid leaking request or response data in tracebacks
        _say(f"error: unexpected {type(exc).__name__}; check receipts before retrying", error=True)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001 - do not mask a completed operation or leak data
                _say("warning: could not close the run journal", error=True)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
