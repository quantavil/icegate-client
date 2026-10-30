"""Public Shipping Bill enquiry (Public Enquiries -> Document Status -> Shipping Bill)."""

from __future__ import annotations

from datetime import date
from typing import Any

from .shared.errors import IcegateError
from .shared.session import IcegateSession


class EnquiryClient:
    """The enquiry is public: no login, no auth headers."""

    def __init__(self, session: IcegateSession) -> None:
        self.session = session

    def shipping_bill(self, location: str, sb_no: str, sb_date: date) -> dict[str, Any]:
        """Every section the portal returns, empty ones included (sbDetailsModel, currentStatusModel, ...)."""
        if not location.strip() or not sb_no.strip():
            raise IcegateError("location and shipping bill number are required")
        response = self.session.send(
            "POST", self.session.url("/enquiry/publicEnquiries/SBTrack_Ices_action_Public"),
            json_body={"location": location.strip(), "sbNo": sb_no.strip(), "sbDate": sb_date.strftime("%Y%m%d")},
            timeout=60,
        )
        if response.status_code != 200:
            raise IcegateError(f"shipping bill enquiry failed with HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise IcegateError("shipping bill enquiry returned invalid JSON") from None
        if not isinstance(body, dict):
            raise IcegateError("shipping bill enquiry returned an unexpected response")
        return body
