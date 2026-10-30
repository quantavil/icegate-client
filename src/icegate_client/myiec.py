"""Read the My IEC client list (Dashboard -> My IEC)."""

from __future__ import annotations

from typing import Any

from .shared.errors import IcegateError
from .shared.session import IcegateSession


class MyIecClient:
    def __init__(self, session: IcegateSession) -> None:
        self.session = session

    def list(self, iec: str | None = None, location: str | None = None) -> list[dict[str, Any]]:
        """Every IEC client row (locationCode, iecName, iecCode); one IEC can appear at several locations."""
        rows = self._post("/dashboard/myIec/getMyIecDetails/", {"iec": iec, "locationCode": location})
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise IcegateError("My IEC returned an unexpected response")
        return rows

    def _post(self, path: str, extra: dict[str, Any]) -> Any:
        session = self.session
        if session.role_id is None:
            raise IcegateError("login did not provide a role for My IEC")
        response = session.send(
            "POST", session.url(path),
            json_body={"icegateId": session.settings.icegate_id, "roleId": session.role_id, **extra},
            headers=session.auth_headers(), timeout=60,
        )
        if response.status_code != 200:
            raise IcegateError(f"My IEC request failed with HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise IcegateError("My IEC returned invalid JSON") from None
