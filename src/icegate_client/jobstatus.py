"""Read the Shipping Bill Job Status report (Dashboard -> Shipping Bill - Job Status -> Detailed Status)."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from lxml import etree

from .shared.errors import IcegateError
from .shared.session import IcegateSession

_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


class JobStatusClient:
    def __init__(self, session: IcegateSession) -> None:
        self.session = session

    def detailed(self) -> list[dict[str, str]]:
        """The Detailed Status report: the portal's latest rows (about 200), every column kept."""
        response = self._post("/dashboard/jobStatus/sbExcel")
        if not response.content or "json" in response.headers.get("Content-Type", "").lower():
            raise IcegateError("job status report returned an error or empty response")
        return parse_xlsx(response.content)

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
        archive = zipfile.ZipFile(io.BytesIO(data))
        shared = _shared_strings(archive)
        sheet = etree.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError):
        raise IcegateError("job status report is not a readable xlsx file") from None
    table: list[list[str]] = []
    for row in sheet.iterfind(".//m:sheetData/m:row", _NS):
        cells: dict[int, str] = {}
        for cell in row.iterfind("m:c", _NS):
            cells[_column(cell.get("r", ""))] = _cell_text(cell, shared)
        width = max(cells, default=-1) + 1
        table.append([cells.get(i, "") for i in range(width)])
    if not table:
        return []
    header, *rows = table
    return [{name: (row[i] if i < len(row) else "") for i, name in enumerate(header)} for row in rows]


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = etree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return ["".join(item.itertext()) for item in root.iterfind("m:si", _NS)]


def _cell_text(cell, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(cell.find("m:is", _NS).itertext())
    value = cell.find("m:v", _NS)
    if value is None or value.text is None:
        return ""
    if kind == "s":
        return shared[int(value.text)]
    return value.text[:-2] if kind in (None, "n") and re.fullmatch(r"\d+\.0", value.text) else value.text


def _column(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference)
    if not letters:
        raise IcegateError("job status report has an unreadable cell reference")
    index = 0
    for char in letters.group():
        index = index * 26 + ord(char) - 64
    return index - 1
