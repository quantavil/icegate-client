"""Read an ICEGATE filing result file (.ack / .nak) that the portal mails after an SB or BE upload. Offline."""

from __future__ import annotations

from datetime import datetime

from .shared.errors import IcegateError

GS = "\x1d"


def _iso(text: str, fmt: str) -> str | None:
    try:
        return datetime.strptime(text.strip(), fmt).date().isoformat()
    except ValueError:
        return None


def _be_error(f: list[str]) -> str:
    """One BE error row (24 fields): its codes, the section tag, and the IGM / MBL / HBL it is about (IGMS rows)."""
    codes = [c.strip() for c in f[18].split(",") if c.strip() and c.strip().lower() != "null"]
    text = ", ".join(codes) + (f" ({f[22].strip()})" if f[22].strip() else "")
    refs = [f"{label} {f[i].strip()}" for label, i in (("IGM", 13), ("MBL", 15), ("HBL", 16))
            if f[22].strip() == "IGMS" and f[i].strip()]  # other sections lay these columns out differently
    return " ".join(filter(None, [text, ", ".join(refs)]))


def parse_response(data: bytes | str) -> dict:
    """{"type": "ack"|"nak", "doc": "SB"|"BE", "jobs": [{location, job, job_date, number, date, errors}]}.

    Only the text before ``<START-SIGNATURE>`` counts. Fields are separated by GS (0x1D), lines by LF.
    A SB nak carries ``;`` separated errors in one line; a BE nak has one ``F`` line per error.
    """
    text = data.decode("latin1") if isinstance(data, bytes) else data
    lines = [line.split(GS) for line in text.split("<START-SIGNATURE>")[0].split("\n") if line.strip()]
    if not lines or lines[0][0] != "HREC" or len(lines[0]) < 9 or lines[0][8] not in ("CHCAE02", "CHCAI02"):
        raise IcegateError("not an ICEGATE SB/BE result file")
    doc = "SB" if lines[0][8] == "CHCAE02" else "BE"
    body = [f for f in lines[1:] if f[0] != "TREC" and len(f) >= 3]
    jobs: dict[tuple[str, str, str | None], dict] = {}
    kind = "nak"
    for f in body:
        if doc == "SB":
            ack = len(f) >= 5 and f[3].strip().isdigit()
            errors = [] if ack else list(dict.fromkeys(e.strip() for e in f[3].split(";") if e.strip()))
            number, day = (f[3].strip(), _iso(f[4], "%Y%m%d")) if ack else (None, None)
            job = {"location": f[0], "job": f[1], "job_date": _iso(f[2], "%d%m%Y"), "number": number, "date": day,
                   "errors": errors}
            kind = "ack" if ack else "nak"
            jobs[(f[0], f[1], job["job_date"])] = job
        elif f[0] == "A" and len(f) >= 9:  # BE ack: the line carries the BE number and date, not the job
            kind = "ack"
            jobs[(f[1], f"A{len(jobs)}", None)] = {"location": f[1], "job": None, "job_date": None,
                                                    "number": f[7].strip() or None, "date": _iso(f[8], "%d%m%Y"), "errors": []}
        elif f[0] == "F":
            job = jobs.setdefault((f[1], f[2], _iso(f[3], "%d%m%Y")), {"location": f[1], "job": f[2], "job_date": _iso(f[3], "%d%m%Y"),
                                                                       "number": None, "date": None, "errors": []})
            if len(f) >= 23 and (error := _be_error(f)) and error not in job["errors"]:
                job["errors"].append(error)
    return {"type": kind, "doc": doc, "jobs": list(jobs.values())}
