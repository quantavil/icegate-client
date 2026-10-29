"""Search ICEGATE's Shipping Bill and Bill of Entry e-copy records."""

from __future__ import annotations

from datetime import date
from typing import Any

from .shared.errors import IcegateError
from .shared.session import IcegateSession


class ECopyClient:
    def __init__(self, session: IcegateSession) -> None:
        self.session = session

    def search_by_date(self, kind: str, start: date, end: date) -> list[dict[str, Any]]:
        if start > end:
            raise IcegateError("start date must not be after end date")
        return self._search("searchByDate", kind, {
            "fromDate": _portal_date(start), "toDate": _portal_date(end),
        })

    def search_by_job(self, kind: str, location: str, number: str, job_date: date) -> list[dict[str, Any]]:
        if not location.strip() or not number.strip():
            raise IcegateError("location and job number are required")
        return self._search("searchByJobNumber", kind, {
            "location": location.strip(), "jobNumber": number.strip(), "jobDate": _portal_date(job_date),
        })

    def _search(self, action: str, kind: str, filters: dict[str, str]) -> list[dict[str, Any]]:
        if kind not in {"SB", "BE"}:
            raise IcegateError("document type must be SB or BE")
        response = self.session.send(
            "POST", self.session.url("/dashboard-utility/download-utility/" + action),
            json_body={**filters, "documentType": kind, "icegateId": self.session.settings.icegate_id},
            headers=self.session.auth_headers(),
        )
        if response.status_code != 200:
            raise IcegateError(f"e-copy search failed with HTTP {response.status_code}")
        try:
            records = response.json()
        except ValueError:
            raise IcegateError("e-copy search returned invalid JSON") from None
        if not isinstance(records, list) or not all(isinstance(row, dict) for row in records):
            raise IcegateError("e-copy search returned an unexpected response")
        return records


def _portal_date(value: date) -> str:
    return value.isoformat() + "T00:00:00.000Z"
