"""Append-only JSONL journal of non-secret intent and observations."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from .redact import scrub_obj


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Journal:
    """Writes one JSON object per line, mode 0600, with every string scrubbed."""

    def __init__(self, path: Path | str, *, enabled: bool = True, echo: bool = False) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.echo = echo
        if enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            os.close(fd)

    def add(self, event: str, **fields) -> dict:
        record = {"ts": now_iso(), "epoch": round(time.time(), 3), "event": event}
        record.update(scrub_obj(fields))
        if self.enabled:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        if self.echo:
            import sys

            print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stderr)
        return record
