"""Read the dashboard notification list (bell icon). Read-only: never calls ``getNotifications/read``."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .shared.errors import IcegateError
from .shared.session import IcegateSession


class NotificationClient:
    def __init__(self, session: IcegateSession) -> None:
        self.session = session

    def list(self, notification_type: str = "E") -> Any:
        """The portal's notification payload for this account; the shape is returned unchanged."""
        session = self.session
        if session.role_id is None:
            raise IcegateError("login did not provide a role for notifications")
        response = session.send(
            "POST", session.url("/registration/getNotifications/all"),
            json_body={"icegateId": session.settings.icegate_id, "notificationType": notification_type,
                       "roleId": session.role_id},
            headers=session.auth_headers(), timeout=60,
        )
        if response.status_code != 200:
            raise IcegateError(f"notifications request failed with HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise IcegateError("notifications returned invalid JSON") from None


_DATE = r"(\d{8}|\d{4}-\d{2}-\d{2})"
_FILING = re.compile(
    r"Your (shipping bill|bill of entry) filing with (job|sb|be) no\.? (\d+) dated " + _DATE
    + r" has been (successfully submitted|rejected)\.?(?: Error code: ([^\n\"]*))?", re.I)
_VALIDATION = re.compile(
    r"(Header|Field|DSC) validation failed for control no\.? (\d+), filing date " + _DATE
    + r", Receiver ID (\w+)", re.I)
_JOB_VALIDATION = re.compile(r"Field validation failed for Job no\.? (\d+)\s+(.*?)\s*(?:Please correct input data.*)?$", re.I | re.S)
_PROCESSING = re.compile(
    r"error in processing your file with control no\.? (\d+), filing date " + _DATE + r", Receiver ID (\w+)", re.I)


def _iso(text: str) -> str | None:
    """Portal dates in these messages are YYYYMMDD, DDMMYYYY or YYYY-MM-DD."""
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d%m%Y"):
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if parsed.year >= 2020:
            return parsed.isoformat()
    return None


def _tidy(message: str) -> str:
    """One line without the greeting, sign-off or stray quotes the portal wraps around every message."""
    text = " ".join(message.split()).strip('" ')
    text = re.sub(r"^Dear Sir/Madam,\s*", "", text)
    text = re.sub(r"\s*(Regards,?\s*ICEGATE Support Team|-CBIC)\s*\"?$", "", text)
    return text.strip('" ')


_ERROR_PATH = re.compile(r",\s+(?=\w+(?:\[\d+\])?(?:\.\w+(?:\[\d+\])?)*:)")


def _code_groups(codes: str) -> list[str]:
    """Split a portal error-code string at each section tag, keeping the text as given.

    ``349,708,,_MAIN,314_ITEMS`` is the codes of section MAIN followed by those of ITEMS.
    """
    groups, current = [], []
    for token in codes.split(","):
        current.append(token)
        if re.search(r"_[A-Za-z]+$", token):
            groups.append(",".join(current))
            current = []
    if any(t.strip() for t in current):
        groups.append(",".join(current))
    return [g.strip() for g in groups if g.strip()]


def _field_errors(message: str, tidy: str) -> list[str]:
    """Every field error of a validation notice (one per model path), or the notice itself when it lists none."""
    if "Errors:" in message:
        body = " ".join(message.split("Errors:", 1)[1].split())
        body = re.sub(r"\s*Regards,?\s*ICEGATE Support Team\s*$", "", body)
        return [e.strip() for e in _ERROR_PATH.split(body) if e.strip()]
    return [tidy]


def parse_notification(message: str) -> dict[str, Any] | None:
    """A filing outcome from one portal message, or None for anything else.

    ``outcome`` is ``submitted`` (accepted for processing, not final), ``rejected`` or ``failed``.
    ``job`` is the job number, or the control number for validation failures (they are the same file field).
    ``problem`` tells the two bad kinds apart (``None`` for ``submitted``): ``not_filed`` is a rejection that
    names no error (the portal did not file it) or a processing error that asks to try again later, so filing
    it again is the fix; ``failed`` carries errors in the file, listed in ``errors``.
    """
    text = _tidy(message)
    if match := _FILING.search(message):
        kind, label, number, day, verdict, codes = match.groups()
        by_job = label.lower() == "job"
        rejected = verdict.lower() == "rejected"
        has_codes = bool(rejected and codes and codes.strip())
        return {"outcome": "rejected" if rejected else "submitted", "doc": "SB" if kind.lower().startswith("s") else "BE",
                "job": number if by_job else None, "number": None if by_job else number,
                "date": _iso(day), "location": None,
                "detail": f"error code {codes.strip()}" if has_codes else "", "message": text,
                "problem": ("failed" if has_codes else "not_filed") if rejected else None,
                "errors": _code_groups(codes.strip()) if has_codes else []}
    if match := _VALIDATION.search(message):
        stage, job, day, location = match.groups()
        extra = message.split("Errors:", 1)[1].strip() if "Errors:" in message else ""
        doc = "SB" if "cache" in message.lower() or "shipping bill" in message.lower() else ("BE" if "cachi" in message.lower() or "bill of entry" in message.lower() else None)
        return {"outcome": "failed", "doc": doc, "job": job, "number": None, "date": _iso(day), "location": location.upper(),
                "detail": f"{stage.lower()} validation failed" + (f": {' '.join(extra.split())[:300]}" if extra else ""),
                "message": text, "problem": "failed", "errors": _field_errors(message, text)}
    if match := _JOB_VALIDATION.search(_tidy(message)):  # an older wording: no date or location, errors straight after the job
        errors = [e.strip() for e in _ERROR_PATH.split(" ".join(match.group(2).split())) if e.strip()]
        return {"outcome": "failed", "doc": None, "job": match.group(1), "number": None, "date": None, "location": None,
                "detail": "field validation failed", "message": text, "problem": "failed", "errors": errors or [text]}
    if match := _PROCESSING.search(message):
        job, day, location = match.groups()
        doc = "SB" if "cache" in message.lower() or "shipping bill" in message.lower() else ("BE" if "cachi" in message.lower() or "bill of entry" in message.lower() else None)
        return {"outcome": "failed", "doc": doc, "job": job, "number": None, "date": _iso(day), "location": location.upper(),
                "detail": "portal error while processing the file; check the portal before retrying", "message": text,
                "problem": "not_filed", "errors": []}
    return None


def filing_events(notifications: Any) -> list[dict[str, Any]]:
    """Parsed outcomes, newest first (the portal's order). Unrelated notifications are dropped."""
    if not isinstance(notifications, list):
        raise IcegateError("notifications returned an unexpected response")
    events = []
    for item in notifications:
        if isinstance(item, dict) and isinstance(item.get("message"), str) and (event := parse_notification(item["message"])):
            events.append({**event, "created": item.get("createdDate")})
    return events


def parse_date(val: Any) -> date | None:
    """Parse a date string in common ICEGATE or ISO formats."""
    if isinstance(val, date) and not isinstance(val, datetime):
        return val
    if isinstance(val, datetime):
        return val.date()
    if not isinstance(val, str) or not val.strip():
        return None
    val = val.strip()
    if "T" in val or " " in val:
        val = val.replace("T", " ").split()[0]
    for fmt in ("%d.%m.%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%Y%m%d", "%d%m%Y"):
        try:
            parsed = datetime.strptime(val, fmt).date()
            if parsed.year >= 2000:
                return parsed
        except ValueError:
            continue
    return None


def filter_by_days(
    items: list[dict[str, Any]],
    days: int = 30,
    *,
    is_events: bool = False,
    now: date | None = None,
    doc: str | None = None,
) -> list[dict[str, Any]]:
    """Filter notifications to those within the last N days (maximum 30 days window).

    `days=1` means today.
    `days=2` means today and yesterday.
    `days=7` means the last 7 calendar days.
    `days=30` (or default) covers up to 30 calendar days.
    `doc` filters by document type ('SB' or 'BE').
    Dates are evaluated in the Asia/Kolkata timezone unless `now` is provided.
    """
    if now is None:
        try:
            now = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        except Exception:
            now = datetime.now().date()

    requested_days = max(1, min(days, 30))
    cutoff = now - timedelta(days=requested_days - 1)
    target_doc = doc.upper().strip() if isinstance(doc, str) else None
    if target_doc in ("ALL", ""):
        target_doc = None

    filtered = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_date = None
        if is_events:
            item_date = parse_date(item.get("created")) or parse_date(item.get("date"))
        else:
            for key in ("createdDate", "created_date", "created", "date"):
                if key in item and (d := parse_date(item[key])):
                    item_date = d
                    break

        if item_date is not None:
            # Within window [cutoff, now + 1 day for clock skew tolerance]
            if not (cutoff <= item_date <= now + timedelta(days=1)):
                continue
        elif requested_days < 30:
            continue

        # Document type filter (SB or BE)
        if target_doc:
            if is_events:
                item_doc = item.get("doc")
                if not item_doc:
                    msg = str(item.get("message") or "").lower()
                    if "shipping bill" in msg or "cache" in msg or any(k in msg for k in ("sb no", "sb number", "sb#", "sb/", "sb:")):
                        item_doc = "SB"
                    elif "bill of entry" in msg or "cachi" in msg or any(k in msg for k in ("be no", "be number", "be#", "be/", "be:")):
                        item_doc = "BE"
                if item_doc != target_doc:
                    continue
            else:
                msg = str(item.get("message") or "").lower()
                if target_doc == "SB" and not any(k in msg for k in ("shipping bill", "cache", "sb no", "sb number", "sb#", "sb/", "sb:")):
                    continue
                if target_doc == "BE" and not any(k in msg for k in ("bill of entry", "cachi", "be no", "be number", "be#", "be/", "be:")):
                    continue

        filtered.append(item)

    return filtered

