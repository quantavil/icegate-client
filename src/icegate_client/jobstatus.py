"""Read the Shipping Bill Job Status report (Dashboard -> Shipping Bill - Job Status -> Detailed Status)."""

from __future__ import annotations

import io

from python_calamine import CalamineError, CalamineWorkbook

from .shared.errors import IcegateError
from .shared.session import IcegateSession

class JobStatusClient:
    def __init__(self, session: IcegateSession) -> None:
        self.session = session

    def detailed(self) -> list[dict[str, str]]:
        """The Detailed Status report: the portal's latest rows (about 200), every column kept."""
        response = self._post("/dashboard/jobStatus/sbExcel")
        if not response.content or "json" in response.headers.get("Content-Type", "").lower():
            raise IcegateError("job status report returned an error or empty response")
        return parse_xlsx(response.content)

    def enquiry(self, kind: str, location: str) -> list[dict]:
        """Enquiries > Job Status for one customs location: one row per file ICEGATE received there.

        `kind` is "SB" or "BE". The portal answers HTTP 400 "No Record Found" for an empty list.
        """
        session = self.session
        if session.role_id is None:
            raise IcegateError("login did not provide a role for the job status enquiry")
        response = session.send(
            "POST", session.url(f"/enquiry/docenquiry/jobstatus/{kind.lower()}"),
            json_body={"icegateId": session.settings.icegate_id, "roleId": session.role_id, "receiverId": location},
            headers=session.auth_headers(), timeout=60,
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code == 400 and "no record" in str(body).lower():
            return []
        if response.status_code != 200 or not isinstance(body, list):
            raise IcegateError(f"job status enquiry failed with HTTP {response.status_code}")
        return [row for row in body if isinstance(row, dict)]

    def _post(self, path: str):
        session = self.session
        if session.role_id is None or not session.user_type:
            raise IcegateError("login did not provide a role for the job status report")
        response = session.send(
            "POST", session.url(path),
            json_body={"icegateId": session.settings.icegate_id, "roleId": session.role_id, "userType": session.user_type},
            headers=session.auth_headers(), timeout=120,
        )
        if response.status_code != 200:
            raise IcegateError(f"job status request failed with HTTP {response.status_code}")
        return response


def parse_xlsx(data: bytes) -> list[dict[str, str]]:
    """First sheet as dicts keyed by the header row; empty cells stay as empty strings."""
    try:
        table = CalamineWorkbook.from_filelike(io.BytesIO(data)).get_sheet_by_index(0).to_python()
    except (CalamineError, IndexError):
        raise IcegateError("job status report is not a readable xlsx file") from None
    if not table:
        return []
    header, *rows = ([_text(cell) for cell in row] for row in table)
    return [{name: (row[i] if i < len(row) else "") for i, name in enumerate(header)} for row in rows]


def _text(cell: object) -> str:
    if isinstance(cell, float) and cell.is_integer():
        return str(int(cell))
    return "" if cell is None else str(cell)
