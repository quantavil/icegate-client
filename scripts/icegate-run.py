#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["questionary>=2.1", "rich>=13", "pymupdf>=1.24", "tzdata; sys_platform == 'win32'"]
# ///
"""One command: sign a folder, pick the document type per file, confirm, upload, draft, archive.

  scripts/icegate-run.py FOLDER [--wait SECONDS] [--dry-run] [--yes] [--no-sign]

Boundaries. The private signer (scripts/sign-all.py) is run as a separate program and alone
handles the PIN and token; this script never sees them. The icegate client owns the portal and
its safety gates; this script only drives its plan and confirm. Every upload shows the plan and
waits for your yes; a failed or unknown step stops the run and nothing is uploaded twice.

Environment: ICEGATE_CLIENT_DIR (client checkout, default: the one holding this script),
ICEGATE_STATE_DIR, ICEGATE_SIGN_SCRIPT.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

CLIENT = Path(os.environ.get("ICEGATE_CLIENT_DIR") or Path(__file__).resolve().parents[1]).expanduser()
STATE_ENV = os.environ.get("ICEGATE_STATE_DIR")
STATE = Path(STATE_ENV or "~/.local/state/icegate-client").expanduser()
FILING_SUFFIXES = {".sb", ".be"}
_CHALLENGE = re.compile(r"^challenge\s+((?:SUBMIT|FILE) \d+ ([0-9a-f]{12}))\s*$", re.M)
_RECEIPT = re.compile(r"^receipt\s+(\S.*?)\s*$", re.M)
_SB_RECORD = re.compile(r"<TABLE>SB\r?\n\s*F\x1d([A-Za-z0-9]{4,10})\x1d")  # F, location, job number, date
_BAD_PDF = re.compile(r"(\S+) is not a valid PDF file")
_PDF_END = re.compile(rb"startxref\s+\d+\s+%%EOF")  # a real end of file, not a stray %%EOF in glued-on junk
FLATTEN_DPI = 120  # 150 dpi pushes a 14-page invoice past the portal's 2 MiB per-file cap once signed
PORTAL_FILE_CAP = 2 * 1024 * 1024
SIGN_OVERHEAD_PER_PAGE = 13_000  # observed: 4 pages, 4 signatures added about 48 kB

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class Stop(Exception):
    """The run stopped on purpose."""


class NotYet(Stop):
    """The portal has not registered the SB number yet."""


class BadPdf(Stop):
    """The portal refused a signed PDF as invalid; nothing was submitted."""

    def __init__(self, message: str, names: list[str]) -> None:
        super().__init__(message)
        self.names = names


POLL_SECONDS = 30  # each check is a fresh client login, so do not hammer the portal


@dataclass
class Item:
    signed: Path
    original: Path | None = None
    code: str = ""

    @property
    def label(self) -> str:
        """File name without extension or the _Signed marker; what the code guess reads."""
        return re.sub(r"_signed$", "", self.signed.stem, flags=re.I)


# ---------------------------------------------------------------- catalogue and codes
def catalog() -> dict[str, str]:
    path = CLIENT / "src/icegate_client/esanchit/data/document_codes.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise Stop(f"cannot read the document code list at {path}") from None
    out = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip().isalnum() and parts[1].strip():
            out[parts[0].strip()] = parts[1].strip()
    return out


# Abbreviations in file names, spelled out in the catalogue's own words.
_ABBREVIATIONS = {"inv": "invoice", "pl": "packing list", "pack": "packing list"}
_STOP_WORDS = {"of", "the", "and", "a", "an", "for", "by", "from", "or", "to", "in", "with", "which"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.casefold())) - _STOP_WORDS


def candidates(stem: str, known: dict[str, str]) -> tuple[list[str], bool]:
    """Codes a file name points to, judged only from the catalogue's own names.

    Evidence is the words of every multi-word catalogue name that appears whole in the file name, plus any
    other catalogue word in it. An entry made of exactly that evidence wins, otherwise every entry
    containing all of it. The bool is True only for a single code backed by such a whole name; an
    abbreviated leading word (INV) or a loose match only suggests, so the user decides.
    """
    name_words = re.findall(r"[a-z]+", stem.casefold())
    name_words += [w for token in name_words for w in _ABBREVIATIONS.get(token, "").split()]
    vocabulary = {w for name in known.values() for w in _words(name)}

    def pick(terms: set[str]) -> list[str]:
        exact = [c for c, n in known.items() if _words(n) == terms]
        return exact or [c for c, n in known.items() if terms <= _words(n)]

    phrase: set[str] = set()
    for name in known.values():
        entry = _words(name)
        if len(entry) >= 2 and entry <= set(name_words):
            phrase |= entry
    if phrase:
        best = pick(phrase | {w for w in name_words if len(w) >= 3 and w in vocabulary})
        return (best, len(best) == 1) if best else (pick(phrase), False)
    first = name_words[0] if name_words else ""  # an expanded abbreviation never leads
    if len(first) < 3:
        return [], False
    terms = {first} if first in vocabulary else {w for w in vocabulary if w.startswith(first)}
    return (pick(terms) if terms else []), False


def assign_codes(items: list[Item], ui: Prompts | None, known: dict[str, str]) -> None:
    """Every PDF gets a code. With a terminal the user picks per file, the guess pre-selected;
    without one, only a code the catalogue pins to a single entry is used, otherwise stop."""
    previous: dict[tuple[str, ...], str] = {}
    for item in items:
        options, sure = candidates(item.label, known)
        if ui is None:
            if not sure:
                hint = f" (could be: {', '.join(options[:6])})" if options else ""
                raise Stop(f"cannot tell the document code for {item.signed.name}{hint}")
            item.code = options[0]
            continue
        key = tuple(options)
        default = options[0] if sure else previous.get(key) or (options[0] if options else None)
        code = ui.choose_code(item.signed.name, options, known, default)
        if code not in known:
            raise Stop(f"{item.signed.name}: unknown or missing document code {code!r}")
        item.code = previous[key] = code


class Prompts:
    """Terminal prompts (questionary, imported only when a person is at the keyboard)."""

    def __init__(self) -> None:
        import questionary
        from rich.console import Console
        self.q, self.console = questionary, Console()

    def _answer(self, question):
        answer = question.ask()
        if answer is None:
            raise Stop("cancelled; nothing was uploaded")
        return answer

    def select_files(self, names: list[str]) -> list[str]:
        return self._answer(self.q.checkbox("Files to upload (space = tick, enter = go)", choices=[
            self.q.Choice(n, checked=True) for n in names]))

    def choose_code(self, name: str, options: list[str], known: dict[str, str], default: str | None) -> str:
        rest = [c for c in known if c not in options]
        choices = [self.q.Choice(f"{'* ' if c in options else '  '}{c}  {known[c]}", value=c) for c in [*options, *rest]]
        return self._answer(self.q.select(f"{name}: document type (type to search)", choices=choices,
                                          default=default, use_search_filter=True, use_jk_keys=False))

    def confirm(self, text: str) -> bool:
        return self._answer(self.q.confirm(text, default=False))

    def summary(self, rows: list[tuple[str, str, str]]) -> None:
        from rich.table import Table
        table = Table(title="eSANCHIT uploads", title_justify="left")
        for column in ("File", "Code", "Document type"):
            table.add_column(column)
        for row in rows:
            table.add_row(*row)
        self.console.print(table)

    def pause(self, message: str, seconds: float) -> None:
        with self.console.status(message):
            time.sleep(seconds)


# ---------------------------------------------------------------- ledger / scan / sign
class Ledger:
    """Signed files already uploaded, by name. A re-signed copy has new bytes, so the client's
    own hash-based duplicate check cannot see a repeat; the name can."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "icegate-run-ledger.json"
        try:
            self.entries: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.entries = {}
        except (OSError, ValueError):
            raise Stop(f"cannot read {self.path}") from None

    def seen(self, path: Path) -> dict | None:
        return self.entries.get(path.name.casefold())

    def record(self, paths: list[Path], plan_id: str) -> None:
        now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
        for path in paths:
            self.entries[path.name.casefold()] = {"plan_id": plan_id, "at": now}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ledger-")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(self.entries, out, indent=1)
        os.replace(tmp, self.path)


def portal_safe(name: str) -> str:
    """Letters, digits and . _ - # only: the portal rejected a name containing '&'."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9._#-]", "_", name))


def signed_name(path: Path) -> str:
    return portal_safe(path.stem + "_Signed" + path.suffix)


def tidy_names(folder: Path, say: Callable[[str], None], dry_run: bool = False) -> None:
    """Give signed files portal-safe names (the signer only turns spaces into underscores)."""
    for path in sorted(folder.iterdir()):
        safe = portal_safe(path.name)
        if (path.is_file() and not path.is_symlink() and path.stem.lower().endswith("_signed")
                and safe != path.name and not (folder / safe).exists()):
            if not dry_run:
                path.rename(folder / safe)
            say(f"{'would rename' if dry_run else 'renamed'} {path.name} -> {safe} (the portal rejects some characters in file names)")


def unsigned(folder: Path, ledger: Ledger) -> list[Path]:
    """Originals with no signed twin yet (and not already uploaded)."""
    names = {p.name for p in folder.iterdir()} | {portal_safe(p.name) for p in folder.iterdir()}
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
                  and p.suffix.lower() in {".pdf", *FILING_SUFFIXES} and not p.stem.lower().endswith("_signed")
                  and signed_name(p) not in names and ledger.seen(Path(signed_name(p))) is None)


def clean_env() -> dict[str, str]:
    """Without VIRTUAL_ENV: this script runs inside its own uv environment, which would make
    every child uv command warn that it is ignoring it."""
    return {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def sign_folder(folder: Path, signer: Path, say: Callable[[str], None]) -> None:
    say(f"signing with {signer.name} (the token must be plugged in)...")
    command = [sys.executable, str(signer)] if signer.suffix == ".py" else [str(signer)]
    try:
        done = subprocess.run([*command, str(folder)], env={**clean_env(), "SIGN_ALL_IN_TERMINAL": "1"}, check=False)
    except OSError as exc:
        raise Stop(f"cannot run the signer: {exc.strerror}") from None
    if done.returncode:
        raise Stop(f"signing failed (exit {done.returncode}); nothing was uploaded")


def scan(folder: Path, ledger: Ledger, say: Callable[[str], None]) -> tuple[list[Item], list[Item]]:
    files = sorted(p for p in folder.iterdir() if not p.name.startswith(".") and not p.is_dir())
    originals = {signed_name(p): p for p in files if p.suffix.lower() in {".pdf", *FILING_SUFFIXES}
                 and not p.stem.lower().endswith("_signed")}
    pdfs: list[Item] = []
    filings: list[Item] = []
    for path in files:
        suffix = path.suffix.lower()
        if suffix != ".pdf" and suffix not in FILING_SUFFIXES:
            say(f"skipping {path.name} (unsupported type)")
        elif path.is_symlink():
            say(f"skipping {path.name} (symlink)")
        elif not path.stem.lower().endswith("_signed"):
            if signed_name(path) not in {q.name for q in files} | {portal_safe(q.name) for q in files}:
                say(f"skipping {path.name} (not signed; sign it first)")
        elif (done := ledger.seen(path)) is not None:
            say(f"skipping {path.name}: already uploaded (plan {done['plan_id']}, {done['at']}); "
                "do not upload it again, move it out of this folder")
        else:
            (pdfs if suffix == ".pdf" else filings).append(Item(path, originals.get(path.name)))
    return pdfs, filings


# ---------------------------------------------------------------- upload via the client
def client_runner() -> Runner:
    extra = ["--state-dir", str(STATE)] if STATE_ENV else []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["uv", "run", "--project", str(CLIENT), "icegate", *argv, *extra],
                              capture_output=True, text=True, check=False, env=clean_env())
    return run


@dataclass
class Upload:
    group: str
    prefix: list[str]
    items: list[Item]
    plan: str = ""
    challenge: str = ""
    plan_id: str = ""
    receipt: Path | None = None


def plan_upload(run: Runner, group: str, prefix: list[str], items: list[Item]) -> Upload:
    plan = run([*prefix, "--dry-run"])
    if plan.returncode:
        raise Stop(f"{group} plan failed:\n{plan.stdout}{plan.stderr}".rstrip())
    match = _CHALLENGE.search(plan.stdout)
    if not match:
        raise Stop(f"{group} plan printed no challenge")
    return Upload(group, prefix, items, (plan.stdout + plan.stderr).rstrip(), match.group(1), match.group(2))


def send_upload(run: Runner, upload: Upload, say: Callable[[str], None]) -> None:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # a half-seen upload must not be interrupted
    try:
        done = run([*upload.prefix, "--confirm", upload.challenge])
    finally:
        signal.signal(signal.SIGINT, previous)
    say((done.stdout + done.stderr).rstrip())
    if done.returncode in (2, 3):
        raise Stop(f"{upload.group} outcome is unknown (exit {done.returncode}); do NOT re-run. "
                   f"Check the portal and mail. Plan id {upload.plan_id}")
    if done.returncode == 1 and (names := _BAD_PDF.findall(done.stdout + done.stderr)):
        raise BadPdf(f"{upload.group} upload refused: the portal calls {', '.join(names)} an invalid PDF; "
                     f"nothing was submitted. Plan id {upload.plan_id}", names)
    if done.returncode:
        raise Stop(f"{upload.group} upload stopped (exit {done.returncode}); read the message above "
                   f"before trying again. Plan id {upload.plan_id}")
    if (match := _RECEIPT.search(done.stdout)):
        upload.receipt = Path(match.group(1)).expanduser()


def sb_location(path: Path) -> str | None:
    """The customs location the SB file itself names (first field of its SB record)."""
    try:
        head = path.read_bytes()[:4096].decode("latin-1")
    except OSError:
        return None
    return match.group(1).upper() if (match := _SB_RECORD.search(head)) else None


def flatten_pdf(source: Path, target: Path) -> None:
    """Rasterise every page to a grayscale JPEG. The portal rejects some Excel exports however they
    are rewritten (pikepdf, Ghostscript and PyMuPDF rebuilds all failed); a flat copy is accepted."""
    import pymupdf
    with pymupdf.open(source) as doc, pymupdf.open() as flat:
        for page in doc:
            image = page.get_pixmap(dpi=FLATTEN_DPI, colorspace=pymupdf.csGRAY).tobytes("jpeg", jpg_quality=70)
            flat.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, stream=image)
        flat.save(target, garbage=4, deflate=True)


def keep_original(path: Path) -> Path:
    """Copy a file into FOLDER/.original/ before it is changed; an earlier, different copy of the same
    name (a previous run's file) is never overwritten."""
    backup = path.parent / ".original"
    backup.mkdir(exist_ok=True)
    data, kept, n = path.read_bytes(), backup / path.name, 1
    while kept.exists() and kept.read_bytes() != data:
        kept, n = backup / f"{path.stem}.{n}{path.suffix}", n + 1
    if not kept.exists():
        shutil.copy2(path, kept)
    return kept


def prepare_pdf(path: Path, say: Callable[[str], None]) -> None:
    """Before signing, on every PDF: it must open in PyMuPDF; cut bytes after the last startxref/%%EOF
    (a portal download came with an HTML page glued on), a plain cut so existing signatures stay
    intact; recompress and, if need be, flatten if signing would push it past the per-file cap.
    The untouched original goes to FOLDER/.original/ first."""
    data = path.read_bytes()
    ends = [m.end() for m in _PDF_END.finditer(data)]
    clean = data[:ends[-1]] + b"\n" if ends and data[ends[-1]:].strip() else data
    import pymupdf
    try:
        doc = pymupdf.open(stream=clean, filetype="pdf")
    except Exception as exc:
        raise Stop(f"{path.name} cannot be read as a PDF ({exc}); nothing was signed") from None
    with doc:
        pages = len(doc)
        fits = lambda size: size + SIGN_OVERHEAD_PER_PAGE * pages <= PORTAL_FILE_CAP  # noqa: E731
        if clean is data and fits(len(data)):
            return
        kept = keep_original(path)
        fixed = path.with_name(f".{path.name}.fixing")
        try:
            note = "trailing bytes removed"
            fixed.write_bytes(clean)
            if not fits(len(clean)):
                doc.save(fixed, garbage=4, deflate=True)
                note = "recompressed"
                if not fits(fixed.stat().st_size):
                    flatten_pdf(path, fixed)
                    note = ("flattened to images to stay under the portal's 2 MiB cap once signed" if fits(fixed.stat().st_size)
                            else "flattened to images, but it may still pass the portal's 2 MiB cap once signed")
            fixed.replace(path)
        finally:
            fixed.unlink(missing_ok=True)
    say(f"fixed {path.name}: {note} (original kept as {kept})")


# ---------------------------------------------------------------- draft text
def portal_date(value: object) -> str:
    """Portal timestamps are midnight UTC, or 18:30 UTC for IST midnight; some records already hold DD-MM-YYYY."""
    if re.fullmatch(r"\d{2}-\d{2}-\d{4}", str(value)):
        return str(value)
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2}):00(?:\.000)?Z", str(value))
    if not match:
        raise Stop("unexpected date format from the portal")
    day = date(int(match[1]), int(match[2]), int(match[3]))
    if match[4] == "18:30":
        day += timedelta(days=1)
    elif match[4] != "00:00":
        raise Stop("unexpected time of day in a portal date")
    return day.strftime("%d-%m-%Y")


def table(header: list[str], rows: list[list[str]]) -> str:
    cell = lambda tag, text: f"<{tag}>{html.escape(str(text), quote=True)}</{tag}>"  # noqa: E731
    head = "<tr>" + "".join(cell("th", h) for h in header) + "</tr>"
    body = "".join("<tr>" + "".join(cell("td", c) for c in row) + "</tr>" for row in rows)
    return f'<table border="1" cellpadding="6" style="border-collapse:collapse">{head}{body}</table>'


def page(intro: str, tables: list[str]) -> str:
    return f"<p>Dear Sir,</p><p>{html.escape(intro)}</p>" + "<br>".join(tables) + "\n"


def sb_html(rows: list[dict]) -> str:
    tables = [table(["Job No", "SB Number", "SB Date"],
                    [[r.get("jobNumber", ""), r.get("docNumber", ""), portal_date(r.get("docDate", ""))]])
              for r in rows]
    return page("Find below Shipping Bill Numbers:", tables)


def esanchit_html(receipt: dict, names: dict[str, str]) -> str:
    if receipt.get("outcome") != "submitted":
        raise Stop("receipt is not a confirmed submission; refusing to draft it")
    codes = {str(f.get("name")).casefold(): f.get("code") for f in receipt.get("files", [])}
    rows = []
    for row in receipt.get("rows", []):
        code = str(codes.get(str(row.get("file")).casefold()) or "")
        if code not in names or not row.get("drn") or not row.get("irn"):
            raise Stop(f"receipt row for {row.get('file')!r} is incomplete or has an unknown document code")
        rows.append([row["file"], row["drn"], row["irn"], f"{code}-{names[code]}"])
    if not rows or len(rows) != len(codes):
        raise Stop("receipt rows do not match its files")
    return page("Find below the IRN and DRN details:", [table(["File Name", "DRN", "IRN", "Document Type"], rows)])


def esanchit_draft(receipt_path: Path, names: dict[str, str]) -> str:
    return esanchit_html(json.loads(receipt_path.read_text(encoding="utf-8")), names)


def pause_plain(say: Callable[[str], None]) -> Callable[[str, float], None]:
    def pause(message: str, seconds: float) -> None:
        say(message)
        time.sleep(seconds)
    return pause


def sb_draft(run: Runner, location: str, jobs: list[str], today: date, wait: float,
             pause: Callable[[str, float], None]) -> str:
    """The SB number can take a couple of minutes to appear after a filing; check until it does."""
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                return sb_html(sb_rows(run, location, jobs, today))
            except NotYet as exc:
                if time.monotonic() >= deadline:
                    raise
                pause(f"{exc}; checking again in {POLL_SECONDS}s (Ctrl-C to skip the SB draft)", POLL_SECONDS)
    except KeyboardInterrupt:
        raise Stop("stopped waiting for the SB number") from None


def copy_html(text: str) -> bool:
    """Put HTML on the clipboard as rich text; False when no tool worked.

    Linux (wl-copy, xclip) is tested. macOS (osascript) and Windows (PowerShell) are not."""
    if sys.platform == "darwin":
        argv, data = ["osascript", "-e", f"set the clipboard to \u00abdata HTML{text.encode().hex()}\u00bb"], None
    elif sys.platform == "win32":
        argv, data = ["powershell", "-NoProfile", "-Command", "$input | Out-String | Set-Clipboard -AsHtml"], text.encode()
    else:
        argv = next(([t, *a] for t, a in (("wl-copy", ["--type", "text/html"]),
                                         ("xclip", ["-selection", "clipboard", "-t", "text/html"])) if shutil.which(t)), None)
        data = text.encode()
        if argv is None:
            return False
    try:
        return subprocess.run(argv, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sb_rows(run: Runner, location: str, jobs: list[str], today: date) -> list[dict]:
    out = run(["ecopy", "search", "SB", "--from", str(today - timedelta(days=7)), "--to", str(today), "--json"])
    if out.returncode:
        raise Stop(f"SB search failed: {(out.stderr or out.stdout).strip()[:200]}")
    try:
        found = json.loads(out.stdout)
    except ValueError:
        raise Stop("SB search returned invalid JSON") from None
    rows = []
    for number in jobs:
        match = [r for r in found if str(r.get("jobNumber")) == number
                 and str(r.get("location") or location).casefold() == location.casefold()]
        if not match:
            raise NotYet(f"no SB found yet for job {number}")
        rows += match
    return rows


# ---------------------------------------------------------------- the run
def run_folder(folder: Path, *, run: Runner, say: Callable[[str], None],
               ui: Prompts | None, dry_run: bool = False, yes: bool = False, state: Path = STATE,
               signer: Path | None = None, wait: float = 0) -> int:
    done_dir = folder / "Done"
    if done_dir.exists() and not done_dir.is_dir():
        raise Stop("'Done' exists and is not a folder")
    ledger = Ledger(state)
    tidy_names(folder, say, dry_run)
    if (todo := unsigned(folder, ledger)):
        if dry_run or signer is None:
            say(f"{len(todo)} unsigned file(s) would be skipped"
                + (": " + ", ".join(p.name for p in todo) if len(todo) <= 5 else "")
                + ("" if dry_run else "; no signer available, sign them first"))
        else:
            for path in todo:
                if path.suffix.lower() == ".pdf":
                    prepare_pdf(path, say)
            sign_folder(folder, signer, say)
            tidy_names(folder, say)
    pdfs, filings = scan(folder, ledger, say)
    if not pdfs and not filings:
        raise Stop("no new signed .pdf, .sb or .be files (names ending _Signed) to upload")
    if ui is not None and not dry_run and len(pdfs) + len(filings) > 1:
        names = ui.select_files([i.signed.name for i in pdfs + filings])
        pdfs, filings = [i for i in pdfs if i.signed.name in names], [i for i in filings if i.signed.name in names]
        if not pdfs and not filings:
            raise Stop("no files selected; nothing was uploaded")
    sbs = [i for i in filings if i.signed.suffix.lower() == ".sb"]
    location = None
    if sbs:
        found = {sb_location(i.signed) for i in sbs}
        if len(found) != 1 or None in found:
            raise Stop("cannot read one customs location from the SB file(s); each must name the same one "
                       "in its <TABLE>SB record")
        location = found.pop()
        say(f"location {location} (read from the SB file)")
    known = catalog()
    if dry_run:
        for item in pdfs:
            options, sure = candidates(item.label, known)
            say(f"eSANCHIT  {item.signed.name}  ->  " + (f"{options[0]}  {known[options[0]]}" if sure
                else "you will choose" + (f" (suggested: {', '.join(options[:4])})" if options else "")))
        for item in filings:
            say(f"filing    {item.signed.name}")
        say("dry run only; nothing signed or uploaded")
        return 0
    if ui is None and not yes:
        raise Stop("not a terminal: pass --yes to upload without a prompt")
    assign_codes(pdfs, ui, known)
    if ui is not None and pdfs:
        ui.summary([(i.signed.name, i.code, known[i.code]) for i in pdfs])
    else:
        for item in pdfs:
            say(f"eSANCHIT  {item.signed.name}  ->  {item.code}  {known[item.code]}")
    for item in filings:
        say(f"filing    {item.signed.name}")

    uploads: list[Upload] = []
    try:
        if pdfs:
            mapping = [f"--doc-type-map={i.signed.name}={i.code}" for i in pdfs]
            uploads.append(plan_upload(run, "eSANCHIT", ["esanchit", "submit", *mapping, *(str(i.signed) for i in pdfs)], pdfs))
        if filings:
            uploads.append(plan_upload(run, "filing", ["filing", "upload", *(str(i.signed) for i in filings)], filings))
        for upload in uploads:
            say(f"\n== {upload.group} plan ==\n{upload.plan}")
        if not yes and not ui.confirm(f"Upload {len(pdfs) + len(filings)} signed file(s) now? This cannot be undone."):
            raise Stop("not confirmed; nothing was uploaded")
    except KeyboardInterrupt:
        raise Stop("interrupted before uploading; nothing was uploaded") from None

    stamp = datetime.now(ZoneInfo("Asia/Kolkata"))
    archive = done_dir / stamp.strftime("%Y%m%d-%H%M%S")
    drafts: dict[str, Path] = {}
    submitted: list[str] = []

    def finish(upload: Upload) -> None:
        """Record the upload, archive its files, then try the draft; never raises for after-work."""
        submitted.append(f"{upload.group} (plan {upload.plan_id})")
        ledger.record([i.signed for i in upload.items], upload.plan_id)
        try:
            archive.mkdir(parents=True, exist_ok=True)
            for item in upload.items:
                for path in (item.original, item.signed):
                    if path is not None:
                        shutil.move(str(path), archive / path.name)
        except OSError as exc:
            say(f"warning: uploaded, but could not archive some files ({exc.strerror}); they are still in the folder")

    def reflatten(upload: Upload, bad: BadPdf) -> Upload:
        """One flattened retry for PDFs the portal refused; a fresh plan and a fresh yes, never automatic."""
        broken = [i for i in upload.items if i.signed.name in bad.names and i.original is not None]
        if not broken or ui is None or signer is None:
            raise bad
        if not ui.confirm(f"The portal refused {', '.join(i.signed.name for i in broken)} as an invalid PDF "
                          "(nothing was submitted). Flatten the pages to images, sign again and retry once?"):
            raise bad
        backup = folder / ".original"
        for item in broken:
            flatten_pdf(keep_original(item.original), item.original)
            item.signed.unlink()
        sign_folder(folder, signer, say)
        tidy_names(folder, say)
        again = plan_upload(run, upload.group, upload.prefix, upload.items)
        say(f"\n== {again.group} plan (flattened) ==\n{again.plan}\nunflattened originals are in {backup}")
        if not ui.confirm(f"Upload {len(again.items)} signed file(s) now? This cannot be undone."):
            raise Stop("not confirmed; nothing was uploaded")
        return again

    def draft(name: str, build: Callable[..., str], *args: object) -> None:
        try:
            text = build(*args)
            archive.mkdir(parents=True, exist_ok=True)
            drafts[name] = archive / f"draft-{name}.html"
            drafts[name].write_text(text, encoding="utf-8")
        except (Stop, OSError, ValueError) as exc:
            say(f"note: {name} draft not created: {exc}")

    def wrap_up() -> None:
        chosen = drafts.get("esanchit") or drafts.get("sb")
        if archive.exists():
            say(f"files and drafts are in {archive}; delete that folder when you are done")
        if chosen:
            say("copied to clipboard as rich text; paste it into the reply" if copy_html(chosen.read_text(encoding="utf-8"))
                else f"could not copy to clipboard; open {chosen} in a browser, copy and paste")
            if len(drafts) > 1:
                say("a second draft is in the same folder; only the eSANCHIT draft is on the clipboard")

    try:
        for upload in uploads:
            sent = upload
            try:
                send_upload(run, sent, say)
            except BadPdf as bad:
                sent = reflatten(upload, bad)
                send_upload(run, sent, say)
            finish(sent)
            if sent.group == "eSANCHIT":
                draft("esanchit", esanchit_draft, sent.receipt or STATE / "receipts" / f"{sent.plan_id}.json", known)
            else:
                jobs = [m.group(0) for i in sbs if (m := re.match(r"\d+", i.signed.stem))]
                if jobs:
                    draft("sb", sb_draft, run, location, jobs, stamp.date(), wait, ui.pause if ui else pause_plain(say))
                if len(jobs) != len(sbs) or any(i.signed.suffix.lower() == ".be" for i in filings):
                    say("note: no SB draft for .be files or SB files whose names do not start with the job number")
    except Stop as exc:
        wrap_up()
        raise Stop(f"{exc}\nAlready submitted in this run: {', '.join(submitted) or 'nothing'}") from None
    wrap_up()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign a folder, pick types, confirm, upload, draft and archive.")
    parser.add_argument("folder", help="folder holding the files to process")
    parser.add_argument("--dry-run", action="store_true", help="show files and guessed codes only; sign and upload nothing")
    parser.add_argument("--yes", action="store_true", help="skip the upload confirmation (required when not on a terminal)")
    parser.add_argument("--no-sign", action="store_true", help="do not run the signer; use only files already signed")
    parser.add_argument("--wait", type=float, default=240, metavar="SECONDS",
                        help="how long to wait for SB numbers to appear for the reply draft (default 240)")
    parser.add_argument("--signer", default=os.environ.get("ICEGATE_SIGN_SCRIPT") or Path(__file__).resolve().with_name("sign-all.py"))
    args = parser.parse_args(argv)
    say = lambda text: print(text, flush=True)  # noqa: E731
    try:
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            raise Stop("folder not found")
        signer = Path(args.signer).expanduser()
        return run_folder(folder, run=client_runner(), say=say,
                          ui=Prompts() if sys.stdin.isatty() else None, dry_run=args.dry_run, yes=args.yes,
                          signer=None if args.no_sign or not signer.is_file() else signer, wait=args.wait)
    except Stop as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; nothing further was done", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
