"""Bundled eSANCHIT document-type catalogue."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources


@dataclass(frozen=True)
class DocType:
    code: str
    name: str

    def __str__(self) -> str:
        return f"{self.code}  {self.name}"


class DocumentCatalog:
    def __init__(self, entries: list[DocType]) -> None:
        self.entries = entries
        self._by_code = {entry.code: entry for entry in entries}

    @classmethod
    def load(cls) -> DocumentCatalog:
        text = resources.files("icegate_client.esanchit.data").joinpath("document_codes.txt").read_text(
            encoding="utf-8"
        )
        entries: list[DocType] = []
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code, name = parts[0].strip(), parts[1].strip()
            if not code.isalnum() or not name:
                continue
            entries.append(DocType(code, name))
        return cls(entries)

    def get(self, code: str) -> DocType | None:
        return self._by_code.get(code)

    def search(self, term: str, limit: int = 20) -> list[DocType]:
        needle = term.strip().lower()
        if not needle:
            return self.entries[:limit]
        exact = [e for e in self.entries if e.code.lower() == needle]
        if exact:
            return exact
        return [e for e in self.entries if needle in e.name.lower() or needle in e.code][:limit]
