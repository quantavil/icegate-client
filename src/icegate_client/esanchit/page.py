"""eSANCHIT page parsing with lxml.

The portal returns server-rendered JSP with legacy markup: a multipart form,
a hidden ``tabId`` and a table whose cells hold read-only inputs and one
``<select>`` per staged row. Parsing lives here so the HTTP client in
``client.py`` only deals with requests and policy.

Nothing in this module performs I/O and nothing logs: the page HTML carries a
session-scoped ``tabId``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree
from lxml import html as lxml_html

# Portal UI values, parsed from the page itself; the defaults in ``pdf.py``
# are only used when a page does not advertise limits.
_MB_RE = re.compile(r"maximum\s+size\s+per\s+file\s+is\s+(\d+(?:\.\d+)?)\s*MB", re.IGNORECASE)
_FILES_RE = re.compile(r"maximum\s+files\s+are\s+(\d+)", re.IGNORECASE)
# The per-request cap only exists in the page's own JavaScript; the visible
# text never states it, and its error message claims 50 MB while the broker
# branch compares against 10 MB.
_REQUEST_BYTES_RE = re.compile(r"upoadSize\s*>\s*(\d{4,})")
_REQUIRED_HEADERS = {"file name", "drn", "irn", "document type"}


def parse_html(text: str) -> etree._Element:
    """Parse a portal page, dropping scripts and styles.

    ``no_network`` keeps the parser from resolving anything external; the
    pages are plain HTML and never need entities.
    """
    parser = lxml_html.HTMLParser(no_network=True, recover=True, huge_tree=False)
    source = re.sub(r"^\s*<\?xml[^?]*\?>", "", text, count=1).strip()
    doc = lxml_html.fromstring(source or "<html></html>", parser=parser)
    etree.strip_elements(doc, "script", "style", with_tail=False)
    return doc


def split_option(value: str, label: str = "") -> tuple[str, str]:
    """Split ``101000-Registration Document`` into code and label."""
    text = (value or "").strip()
    if not text or text.upper() == "NONE":
        return "", (label or text).strip()
    if "-" in text:
        code, rest = text.split("-", 1)
        return code.strip(), rest.strip()
    return text, (label or text).strip()


@dataclass
class StagedRow:
    index: int
    file_name: str
    drn: str = ""
    irn: str = ""
    start_date: str = ""
    sign_status: str = ""
    doc_type_label: str = ""
    doc_type_code: str = ""
    select_id: str | None = None
    select_name: str | None = None
    options: list[tuple[str, str]] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return " ".join(self.sign_status.lower().split()) in {
            "verified",
            "successfully verified",
        }

    @property
    def has_document_type(self) -> bool:
        return bool(self.doc_type_code)

    def option_for(self, code: str) -> tuple[str, str] | None:
        for value, label in self.options:
            if split_option(value, label)[0] == code:
                return value, label
        return None


@dataclass
class HomePage:
    html: str
    tab_id: str | None = None
    rows: list[StagedRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    max_file_bytes: int | None = None
    max_files: int | None = None
    max_request_bytes: int | None = None
    hidden_fields: dict[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, html: str) -> HomePage:
        doc = parse_html(html)
        page = cls(html=html)

        form = _first(doc, '//form[@id="form1"]')
        if form is None:
            form = _first(
                doc,
                '//form[contains(translate(@enctype,"ABCDEFGHIJKLMNOPQRSTUVWXYZ",'
                '"abcdefghijklmnopqrstuvwxyz"),"multipart/form-data")]',
            )
        if form is not None:
            tab = _first(form, './/input[@id="tabId"]')
            if tab is not None:
                page.tab_id = tab.get("value")
            for hidden in form.xpath('.//input[@type="hidden"]'):
                name = hidden.get("name")
                if name:
                    page.hidden_fields[name] = hidden.get("value") or ""

        for element_id in ("docs_error", "fileDiv", "errorMsgDiv"):
            node = _first(doc, f'//*[@id="{element_id}"]')
            if node is not None:
                text = _text(node)
                if text:
                    page.errors.append(f"{element_id}: {text}")

        body_text = _text(doc)
        page.max_file_bytes, page.max_files = _parse_limits(body_text)
        # The raw text is used on purpose: scripts are stripped from ``doc``
        # and the per-request cap exists only in the page's JavaScript.
        page.max_request_bytes = _parse_request_limit(html)
        page.rows = _parse_rows(doc)
        return page

    def describe_limits(self) -> str:
        parts = []
        if self.max_file_bytes:
            parts.append(f"{self.max_file_bytes / (1024 * 1024):g} MB per file")
        if self.max_files:
            parts.append(f"{self.max_files} files per DRN")
        if self.max_request_bytes:
            parts.append(f"{self.max_request_bytes / (1024 * 1024):g} MB per request")
        return ", ".join(parts) if parts else "not advertised"


def _first(node, xpath: str):
    found = node.xpath(xpath)
    return found[0] if found else None


def _text(node) -> str:
    return " ".join((node.text_content() or "").split())


def _parse_limits(text: str) -> tuple[int | None, int | None]:
    max_bytes = None
    max_files = None
    match = _MB_RE.search(text)
    if match:
        max_bytes = int(float(match.group(1)) * 1024 * 1024)
    match = _FILES_RE.search(text)
    if match:
        max_files = int(match.group(1))
    return max_bytes, max_files


def _parse_request_limit(html: str) -> int | None:
    """The per-request byte cap the page's own JS compares against.

    The JS carries one branch per user class (agency, broker) and the visible
    message says 50 MB while the broker branch compares against 10 MB. The
    tightest number is reported: warning too early costs a line of output,
    warning too late loses the upload.
    """
    values = [int(match.group(1)) for match in _REQUEST_BYTES_RE.finditer(html or "")]
    return min(values) if values else None


def _cell_value(cell) -> str:
    """Return the textarea text or the input value inside a table cell."""
    textarea = _first(cell, ".//textarea")
    if textarea is not None:
        return (textarea.text or "").strip() or _text(textarea)
    box = _first(cell, ".//input")
    if box is not None and (box.get("type") or "").lower() not in {"checkbox", "radio"}:
        return box.get("value") or ""
    return _text(cell)


def _parse_rows(doc) -> list[StagedRow]:
    for table in doc.xpath("//table"):
        headers = [_text(cell).lower() for cell in table.xpath(".//th")]
        if not _REQUIRED_HEADERS.issubset(set(headers)):
            continue

        rows: list[StagedRow] = []
        for tr in table.xpath(".//tr"):
            cells = tr.xpath("./td")
            if len(cells) < 6:
                continue
            select = _first(cells[5], ".//select")
            if select is None and not _cell_value(cells[0]):
                continue

            options: list[tuple[str, str]] = []
            selected_value = ""
            selected_label = ""
            if select is not None:
                for option in select.xpath(".//option"):
                    value = option.get("value") or ""
                    label = _text(option)
                    options.append((value, label))
                    if "selected" in option.attrib:
                        selected_value, selected_label = value, label
                if not selected_value and options:
                    selected_value, selected_label = options[0]

            code, label = split_option(selected_value, selected_label)
            rows.append(
                StagedRow(
                    index=_index_of(select.get("id") if select is not None else None, len(rows)),
                    file_name=_cell_value(cells[0]),
                    drn=_cell_value(cells[1]),
                    irn=_cell_value(cells[2]),
                    start_date=_cell_value(cells[3]),
                    sign_status=_cell_value(cells[4]),
                    doc_type_label=label,
                    doc_type_code=code,
                    select_id=select.get("id") if select is not None else None,
                    select_name=select.get("name") if select is not None else None,
                    options=options,
                )
            )
        return rows
    return []


def _index_of(select_id: str | None, fallback: int) -> int:
    if not select_id:
        return fallback
    match = re.search(r"(\d+)$", select_id)
    return int(match.group(1)) if match else fallback
