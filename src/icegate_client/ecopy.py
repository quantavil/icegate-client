"""Search and download ICEGATE Shipping Bill and Bill of Entry e-copies."""

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

    def download_ack(self, filename: str) -> bytes:
        if not filename or filename in {".", ".."} or any(c in filename for c in "/\\\x00"):
            raise IcegateError("invalid acknowledgement filename")
        return self._download(
            "/dashboard-utility/download-utility/downloadAckFile",
            {"icegateId": self.session.settings.icegate_id, "fileName": filename},
            signed=False,
        )

    def download_signed(self, irn: str, user_id: str) -> bytes:
        if not irn.strip() or not user_id.strip():
            raise IcegateError("IRN and user ID are required for a signed copy")
        return self._download(
            "/esanchit/getDocument", {"userId": user_id, "irn": irn}, signed=True
        )

    def _download(self, path: str, payload: dict[str, str], *, signed: bool) -> bytes:
        response = self.session.send(
            "POST", self.session.url(path), json_body=payload,
            headers=self.session.auth_headers(), timeout=120,
        )
        if response.status_code != 200:
            raise IcegateError(f"e-copy download failed with HTTP {response.status_code}")
        content = response.content
        content_type = response.headers.get("Content-Type", "").lower()
        if not content or "json" in content_type or "html" in content_type:
            raise IcegateError("e-copy download returned an error or empty response")
        if signed and not content.startswith(b"%PDF-"):
            raise IcegateError("signed e-copy response is not a PDF")
        return content

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
