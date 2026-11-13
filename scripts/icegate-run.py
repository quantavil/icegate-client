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
import hashlib
import html
import json
import math
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
# The SB and BE records both start: F, location, job number, date.
_RECORD = {kind: re.compile(rf"<TABLE>{kind}\r?\n\s*F\x1d([A-Za-z0-9]{{4,10}})\x1d") for kind in ("SB", "BE")}
_JOB = {kind: re.compile(rf"<TABLE>{kind}\r?\n\s*F\x1d[A-Za-z0-9]{{4,10}}\x1d(\d+)\x1d(\d{{8}})\x1d") for kind in ("SB", "BE")}
PORTAL_FILE_CAP = 2 * 1024 * 1024
SIGN_OVERHEAD_PER_PAGE = 13_000  # observed: ~11.5 - 12 kB added per page by DSC signing
SAFETY_MARGIN = 64 * 1024  # 64 kB safety buffer below 2 MiB cap
FLATTEN_DPI = 120  # default dpi for short documents
FLAT_MARK = "icegate-run flattened"  # a flattened file is not flattened again unless oversized
ORIGIN_TAG = "source-sha256:"  # in a flat file's keywords: the hash of the document it was made from

# Profiles in descending order of visual quality.
# Step down DPI and JPEG quality adaptively until the file fits within the safe cap.
COMPRESSION_PROFILES: list[tuple[int, int]] = [
    (120, 70),
    (110, 70),
    (100, 70),
    (100, 60),
    (90, 65),
    (90, 55),
    (85, 60),
    (80, 60),
    (80, 50),
    (75, 55),
    (72, 60),
    (72, 50),
    (72, 40),
    (65, 40),
    (60, 40),
    (50, 40),
]

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


class Stop(Exception):
    """The run stopped on purpose."""


class NotYet(Stop):
    """The portal has not registered the SB number yet."""


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

    def choose_run(self, runs: list[Path]) -> Path:
        choices = []
        for path in runs:
            files = sorted(p.name for p in path.iterdir() if p.is_file()
                           and p.suffix.lower() in {".pdf", *FILING_SUFFIXES}
                           and p.stem.lower().endswith("_signed"))
            summary = ", ".join(files[:2]) or "saved tables"
            if len(files) > 2:
                summary += f" (+{len(files) - 2} more)"
            choices.append(self.q.Choice(f"{path.name}  |  {summary}", value=path))
        return self._answer(self.q.select("Completed run to recheck (newest first)", choices=choices))

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
    """Remember uploaded names and source hashes to detect repeats after re-signing."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "icegate-run-ledger.json"
        self.pending_path = directory / "icegate-run-pending.json"
        try:
            self.entries: dict[str, dict] = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.entries = {}
        except (OSError, ValueError):
            raise Stop(f"cannot read {self.path}") from None
        try:
            self.pending: dict[str, dict] = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.pending = {}
        except (OSError, ValueError):
            raise Stop(f"cannot read {self.pending_path}") from None

    def remember_signed(self, folder: Path, paths: list[Path], source_hashes: dict[str, str]) -> None:
        for path in paths:
            key = signed_name(path).casefold()
            signed = folder / signed_name(path)
            self.pending[str(signed.resolve())] = {
                "original": str(path.resolve()), "source_hash": source_hashes[key],
                "prepared_hash": file_hash(path), "signed_hash": file_hash(signed),
            }
        self.pending_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=self.pending_path.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(self.pending, out, indent=1)
            os.replace(tmp, self.pending_path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def resumable(self, folder: Path, originals: dict[str, Path], source_hashes: dict[str, str]) -> set[str]:
        result: set[str] = set()
        for key, original in originals.items():
            signed = folder / signed_name(original)
            pending = self.pending.get(str(signed.resolve()))
            previous = self.seen(signed)
            if not pending or not signed.exists():
                continue
            if (signed.is_symlink() or not signed.is_file()
                    or pending.get("original") != str(original.resolve())
                    or pending.get("prepared_hash") != file_hash(original)
                    or pending.get("signed_hash") != file_hash(signed)):
                raise Stop(f"{original.name} or {signed.name} changed since signing; review the files before continuing")
            if pending.get("source_hash") not in (previous or {}).get("source_hashes", []):
                source_hashes[key] = pending["source_hash"]
                result.add(key)
        return result

    def seen(self, path: Path) -> dict | None:
        return self.entries.get(path.name.casefold())

    def record(self, items: list[Item], plan_id: str, source_hashes: dict[str, str]) -> None:
        now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
        for item in items:
            key = item.signed.name.casefold()
            previous = self.entries.get(key, {})
            hashes = previous.get("source_hashes", [])
            source_hash = source_hashes.get(key)
            if source_hash and source_hash not in hashes:
                hashes = [*hashes, source_hash]
            self.entries[key] = {"plan_id": plan_id, "at": now, "source_hashes": hashes}
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


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def originals_by_signed_name(folder: Path) -> dict[str, Path]:
    originals: dict[str, Path] = {}
    for path in folder.iterdir():
        if (path.name.startswith(".") or path.is_symlink() or not path.is_file()
                or path.suffix.lower() not in {".pdf", *FILING_SUFFIXES}
                or path.stem.lower().endswith("_signed")):
            continue
        key = signed_name(path).casefold()
        if key in originals:
            raise Stop(f"{path.name} and {originals[key].name} have the same signed filename; rename one")
        originals[key] = path
    return originals


def tidy_names(folder: Path, say: Callable[[str], None], dry_run: bool = False) -> None:
    """Give signed files portal-safe names (the signer only turns spaces into underscores)."""
    for path in sorted(folder.iterdir()):
        safe = portal_safe(path.name)
        if (path.is_file() and not path.is_symlink() and path.stem.lower().endswith("_signed")
                and safe != path.name and not (folder / safe).exists()):
            if not dry_run:
                path.rename(folder / safe)
            say(f"{'would rename' if dry_run else 'renamed'} {path.name} -> {safe} (the portal rejects some characters in file names)")


def unsigned(folder: Path) -> list[Path]:
    """Originals with no signed twin yet."""
    names = {p.name for p in folder.iterdir()} | {portal_safe(p.name) for p in folder.iterdir()}
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
                  and p.suffix.lower() in {".pdf", *FILING_SUFFIXES} and not p.stem.lower().endswith("_signed")
                  and signed_name(p) not in names)


def clean_env() -> dict[str, str]:
    """Without VIRTUAL_ENV: this script runs inside its own uv environment, which would make
    every child uv command warn that it is ignoring it."""
    return {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}


def sign_folder(folder: Path, signer: Path, say: Callable[[str], None]) -> None:
    say(f"signing with {signer.name} (the token must be plugged in)...")
    command = [sys.executable, str(signer)] if signer.suffix == ".py" else [str(signer)]
    try:
        done = subprocess.run([*command, str(folder)],
                              env={**clean_env(), "SIGN_ALL_IN_TERMINAL": "1", "PYTHONUTF8": "1"},
                              capture_output=True, text=True, errors="replace", check=False)
    except OSError as exc:
        raise Stop(f"cannot run the signer: {exc.strerror}") from None
    if done.returncode:
        if detail := (done.stderr or done.stdout).strip():
            say(detail.replace(str(folder), "[signing folder]"))
        raise Stop(f"signing failed (exit {done.returncode}); nothing was uploaded")
    say("Signing complete.")


def sign_batch(folder: Path, paths: list[Path], signer: Path, say: Callable[[str], None]) -> None:
    """Give the folder-based signer only the files that need signing."""
    with tempfile.TemporaryDirectory(prefix=".sign-batch-", dir=folder) as temp:
        staging = Path(temp)
        for path in paths:
            shutil.copy2(path, staging / path.name)
        sign_folder(staging, signer, say)
        for path in paths:
            produced = staging / (re.sub(r"\s+", "_", path.stem) + "_Signed" + path.suffix)
            destination = folder / signed_name(path)
            if not produced.is_file() or produced.is_symlink():
                raise Stop(f"signer did not produce {destination.name}; nothing was uploaded")
            if destination.exists():
                raise Stop(f"{destination.name} already exists; nothing was overwritten")
        for path in paths:
            produced = staging / (re.sub(r"\s+", "_", path.stem) + "_Signed" + path.suffix)
            shutil.move(str(produced), folder / signed_name(path))


def scan(folder: Path, ledger: Ledger, say: Callable[[str], None],
         newly_signed: set[str] | None = None) -> tuple[list[Item], list[Item]]:
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
        elif (done := ledger.seen(path)) is not None and path.name.casefold() not in (newly_signed or set()):
            say(f"skipping {path.name}: already uploaded (plan {done['plan_id']}, {done['at']}); "
                "do not upload it again, move it out of this folder")
        else:
            (pdfs if suffix == ".pdf" else filings).append(Item(path, originals.get(path.name)))
    return pdfs, filings


# ---------------------------------------------------------------- upload via the client
def client_runner() -> Runner:
    extra = ["--state-dir", str(STATE)] if STATE_ENV else []
    env = {**clean_env(), "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["uv", "run", "--project", str(CLIENT), "icegate", *argv, *extra],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              check=False, env=env)
    return run


@dataclass
class Upload:
    group: str
    prefix: list[str]
    items: list[Item]
    challenge: str = ""
    plan_id: str = ""
    receipt: Path | None = None


def plan_upload(run: Runner, group: str, prefix: list[str], items: list[Item],
                say: Callable[[str], None]) -> Upload:
    plan = run([*prefix, "--dry-run"])
    if plan.returncode:
        raise Stop(f"{group} plan failed:\n{plan.stdout}{plan.stderr}".rstrip())
    match = _CHALLENGE.search(plan.stdout)
    if not match:
        raise Stop(f"{group} plan printed no challenge")
    if plan.stderr.strip():
        say(plan.stderr.strip())
    return Upload(group, prefix, items, match.group(1), match.group(2))


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
    if done.returncode:
        raise Stop(f"{upload.group} upload stopped (exit {done.returncode}); read the message above "
                   f"before trying again. Plan id {upload.plan_id}")
    if (match := _RECEIPT.search(done.stdout)):
        upload.receipt = Path(match.group(1)).expanduser()


def sb_location(path: Path, kind: str = "SB") -> str | None:
    """The customs location the SB/BE file itself names (first field of its record)."""
    try:
        head = path.read_bytes()[:4096].decode("latin-1")
    except OSError:
        return None
    return match.group(1).upper() if (match := _RECORD[kind].search(head)) else None


def sb_job(path: Path, kind: str = "SB") -> tuple[str, str] | None:
    try:
        head = path.read_bytes()[:4096].decode("latin-1")
    except OSError:
        return None
    if not (match := _JOB[kind].search(head)):
        return None
    try:
        job_date = datetime.strptime(match.group(2), "%Y%m%d").date().isoformat()
    except ValueError:
        return None
    return match.group(1), job_date


def flatten_pdf(source: Path, target: Path, dpi: int = FLATTEN_DPI, quality: int = 70,
                origin: str | None = None) -> int:
    """Rasterise every page to a colour (RGB) JPEG. The portal rejects some Excel exports however they
    are rewritten (pikepdf, Ghostscript and PyMuPDF rebuilds all failed); a flat copy is accepted.
    The flat file records the SHA-256 of the document it came from (`origin`, default: `source`), so a
    later, tighter pass can find that exact original again instead of re-compressing the flat copy."""
    import pymupdf
    origin = origin or file_hash(source)
    with pymupdf.open(source) as doc, pymupdf.open() as flat:
        for page in doc:
            image = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB).tobytes("jpeg", jpg_quality=quality)
            flat.new_page(width=page.rect.width, height=page.rect.height).insert_image(page.rect, stream=image)
        flat.set_metadata({"producer": FLAT_MARK, "keywords": f"{ORIGIN_TAG}{origin}"})
        flat.save(target, garbage=4, deflate=True)
    return target.stat().st_size


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


def original_by_hash(backup: Path, keywords: str) -> Path | None:
    """The file in FOLDER/.original/ whose SHA-256 a flat file names as its origin, if any."""
    if not keywords.startswith(ORIGIN_TAG) or not backup.is_dir():
        return None
    wanted = keywords[len(ORIGIN_TAG):]
    for candidate in sorted(backup.iterdir()):
        if candidate.is_file() and not candidate.is_symlink() and file_hash(candidate) == wanted:
            return candidate
    return None


def prepare_pdf(path: Path, say: Callable[[str], None]) -> None:
    """Before signing, on every PDF: rasterise the pages (flatten_pdf). That makes any file the portal
    can parse, drops bytes glued on after %%EOF and turns existing signatures into plain page images.
    The untouched original goes to FOLDER/.original/ first; if the file would exceed the portal's
    2 MiB cap once signed, it is compressed adaptively (stepping down DPI/quality) until it fits safely."""
    import pymupdf
    try:
        with pymupdf.open(path) as doc:
            pages = len(doc)
            is_flat = doc.metadata.get("producer") == FLAT_MARK
            keywords = doc.metadata.get("keywords") or ""
    except Exception as exc:
        raise Stop(f"{path.name} cannot be read as a PDF ({exc}); nothing was signed") from None

    target_cap = PORTAL_FILE_CAP - (SIGN_OVERHEAD_PER_PAGE * pages) - SAFETY_MARGIN
    if is_flat and path.stat().st_size <= target_cap:
        return

    if SIGN_OVERHEAD_PER_PAGE * pages >= PORTAL_FILE_CAP:
        raise Stop(f"{path.name} has {pages} pages; signing overhead alone would exceed the portal's 2 MiB limit")

    # Backup names can belong to an earlier document; never infer a match by name. A flat file names its
    # origin by hash, so re-compress that exact original rather than the already-lossy flat copy.
    origin = None
    kept = original_by_hash(path.parent / ".original", keywords) if is_flat else None
    if kept is not None:
        source, origin = kept, file_hash(kept)
    else:
        kept = source = keep_original(path)
        if is_flat and keywords.startswith(ORIGIN_TAG):
            origin = keywords[len(ORIGIN_TAG):]  # keep pointing at the true original

    effective_cap = max(1, target_cap)
    flat = path.with_name(f".{path.name}.flat")
    best_size = None
    best_profile = None

    try:
        for dpi, quality in COMPRESSION_PROFILES:
            size = flatten_pdf(source, flat, dpi=dpi, quality=quality, origin=origin)
            best_size = size
            best_profile = (dpi, quality)
            if size <= effective_cap:
                break

        if best_size is None or best_size > effective_cap:
            raise Stop(f"cannot compress {path.name} to the estimated safe size for signing; nothing was signed")

        flat.replace(path)
    finally:
        flat.unlink(missing_ok=True)

    dpi, quality = best_profile or (FLATTEN_DPI, 70)
    if (dpi, quality) != (FLATTEN_DPI, 70):
        say(f"compressed and flattened {path.name} ({best_size // 1024} kB at {dpi} dpi/q{quality}, "
            f"{pages} pages, estimated signed {(best_size + SIGN_OVERHEAD_PER_PAGE * pages) // 1024} kB, "
            f"original kept as {kept})")
    else:
        say(f"flattened {path.name} ({best_size // 1024} kB, original kept as {kept})")


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


def be_html(rows: list[dict]) -> str:
    tables = [table(["Job No", "BE Number", "BE Date"],
                    [[r.get("jobNumber", ""), r.get("docNumber", ""),
                      "-" if r.get("docDate") in ("-", "") else portal_date(r["docDate"])]])
              for r in rows]
    return page("Find below Bill of Entry Numbers:", tables)


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


def plain_table(text: str) -> str:
    """The draft's tables as aligned plain text without borders, so a terminal selection pastes cleanly."""
    out = []
    for block in re.findall(r"<table.*?</table>", text, re.S):
        rows = [[html.unescape(c) for c in re.findall(r"<t[hd]>(.*?)</t[hd]>", r, re.S)]
                for r in re.findall(r"<tr>(.*?)</tr>", block, re.S)]
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        out.append("\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows))
    return "\n\n".join(out)


def pause_plain(say: Callable[[str], None]) -> Callable[[str, float], None]:
    def pause(message: str, seconds: float) -> None:
        say(message)
        time.sleep(seconds)
    return pause


Notices = Callable[[list[tuple[str, str]]], "dict[tuple[str, str], str]"]


def sb_draft(run: Runner, location: str, jobs: list[tuple[str, str]], wait: float,
             pause: Callable[[str, float], None], *, kind: str = "SB",
             notices: Notices | None = None, failures: dict[tuple[str, str], str] | None = None) -> str:
    """The SB/BE number can take a couple of minutes to appear after a filing; check until it does.

    `notices` maps jobs to a portal rejection message. A rejected job is dropped from the wait (it will
    never get a number) and recorded in `failures`; when every job is rejected the draft is impossible.
    """
    failures = {} if failures is None else failures
    build = be_html if kind == "BE" else sb_html
    deadline = time.monotonic() + wait
    try:
        while True:
            if notices:
                failures.update(notices([j for j in dict.fromkeys(jobs) if j not in failures]))
            live = [j for j in jobs if j not in failures]
            if not live:
                # Notices carry no time: an old rejection of the same job number can sit beside a later
                # success. The e-copy record is the evidence, so look once before giving up.
                try:
                    return build(sb_rows(run, location, list(dict.fromkeys(jobs)), kind))
                except (NotYet, Stop):
                    raise Stop(f"no {kind} number will be issued: ICEGATE rejected every job") from None
            try:
                return build(sb_rows(run, location, live, kind))
            except NotYet as exc:
                if time.monotonic() >= deadline:
                    raise
                remaining = max(0, deadline - time.monotonic())
                delay = min(POLL_SECONDS, remaining)
                pause(f"{exc}; checking again in {delay:.0f}s (Ctrl-C to skip the {kind} draft)", delay)
    except KeyboardInterrupt:
        raise Stop(f"stopped waiting for the {kind} number") from None


def portal_events(run: Runner) -> list[dict] | None:
    """Filing outcomes from the portal's notifications, newest first; None when they cannot be read."""
    out = run(["notifications", "--events"])
    if out.returncode:
        return None
    try:
        events = json.loads(out.stdout)
    except ValueError:
        return None
    return events if isinstance(events, list) else None


def job_events(events: list[dict], location: str, job: tuple[str, str]) -> list[dict]:
    number, job_date = job
    return [e for e in events if e.get("job") == number and e.get("date") == job_date
            and e.get("location") in (None, location.upper())]


def notice_watcher(run: Runner, location: str, jobs: list[tuple[str, str]],
                   say: Callable[[str], None], baseline: list[dict]) -> Notices:
    """Report new portal outcomes for these jobs.

    Notifications carry no time, so a job that was filed before (same number and date) is judged only by
    what is not in `baseline`, the events fetched before uploading (empty: judge by everything).
    """
    seen = {job: len(job_events(baseline, location, job)) for job in dict.fromkeys(jobs)}
    told: set[str] = set()

    def check(live: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        events = portal_events(run)
        failed: dict[tuple[str, str], str] = {}
        if events is None:  # unreadable now: say nothing rather than guess
            return failed
        for job in live:
            matching = job_events(events, location, job)
            for event in reversed(matching[:max(0, len(matching) - seen[job])]):  # new ones, oldest first
                text = f"ICEGATE, job {job[0]}: {event['message']}"
                if event["outcome"] == "submitted":
                    if text not in told:
                        told.add(text)
                        say(text)
                else:
                    failed[job] = event["message"] + (f" ({event['detail']})" if event.get("detail") else "")
        return failed
    return check


def clipboard_command(text: str) -> tuple[list[str], bytes | None] | None:
    """The program that puts `text` on the clipboard as rich text, and the stdin it needs."""
    if sys.platform == "darwin":
        return ["osascript", "-e", f"set the clipboard to \u00abdata HTML{text.encode().hex()}\u00bb"], None
    if sys.platform == "win32":
        # `$input` is pipeline input, not redirected stdin, so it pastes nothing. Read the bytes
        # ourselves and decode them as UTF-8; Set-Clipboard -AsHtml then writes CF_HTML.
        script = ("$stdin = [Console]::OpenStandardInput(); $buf = New-Object System.IO.MemoryStream; "
                  "$stdin.CopyTo($buf); $html = [Text.Encoding]::UTF8.GetString($buf.ToArray()); "
                  "Set-Clipboard -AsHtml -Value $html -ErrorAction Stop")
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], text.encode("utf-8")
    argv = next(([t, *a] for t, a in (("wl-copy", ["--type", "text/html"]),
                                     ("xclip", ["-selection", "clipboard", "-t", "text/html"])) if shutil.which(t)), None)
    return (argv, text.encode()) if argv else None


def copy_html(text: str) -> bool:
    """Put HTML on the clipboard as rich text; False when no tool worked."""
    command = clipboard_command(text)
    if command is None:
        return False
    argv, data = command
    try:
        return subprocess.run(argv, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def sb_rows(run: Runner, location: str, jobs: list[tuple[str, str]], kind: str = "SB") -> list[dict]:
    rows = []
    for number, job_date in dict.fromkeys(jobs):
        expected_date = date.fromisoformat(job_date).strftime("%d-%m-%Y")
        # The record's job number (what the portal's notices use) can be the e-copy job number minus a
        # two-digit year suffix (record 642, e-copy 64226), so try the plain number, then the suffixed one.
        candidates = [number, f"{number}{job_date[2:4]}"]
        match: list[dict] = []
        for candidate in candidates:
            out = run(["ecopy", "search", kind, "--location", location, "--job-number", candidate,
                       "--job-date", job_date, "--json"])
            if out.returncode:
                raise Stop(f"{kind} search failed: {(out.stderr or out.stdout).strip()[:200]}")
            try:
                found = json.loads(out.stdout)
            except ValueError:
                raise Stop(f"{kind} search returned invalid JSON") from None
            match = [r for r in found if str(r.get("jobNumber")) == candidate
                     and str(r.get("location") or location).casefold() == location.casefold()
                     and (not r.get("jobDate") or portal_date(r["jobDate"]) == expected_date)
                     and r.get("docNumber") and r.get("docDate")]
            if match:
                break
        if not match and kind == "BE":
            # A refiled BE keeps the date inside the file but the portal records the new filing date, so a
            # job-date search misses it. Search from the file's date to today and match location and job only.
            today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
            out = run(["ecopy", "search", kind, "--from", job_date, "--to", today, "--json"])
            if out.returncode:
                raise Stop(f"{kind} search failed: {(out.stderr or out.stdout).strip()[:200]}")
            try:
                found = json.loads(out.stdout)
            except ValueError:
                raise Stop(f"{kind} search returned invalid JSON") from None
            match = [r for r in found if str(r.get("jobNumber")) in candidates
                     and str(r.get("location") or "").casefold() == location.casefold()
                     and r.get("docNumber") and r.get("docDate")]
        if not match:
            raise NotYet(f"no {kind} found yet for job {number}")
        if len(match) != 1:
            raise Stop(f"multiple {kind} results for job {number}; check ICEGATE before drafting")
        rows.extend(match)
    return rows


# ---------------------------------------------------------------- the run
def run_folder(folder: Path, *, run: Runner, say: Callable[[str], None],
               ui: Prompts | None, dry_run: bool = False, yes: bool = False, state: Path = STATE,
               signer: Path | None = None, wait: float = 0) -> int:
    done_dir = folder / "Done"
    if done_dir.exists() and not done_dir.is_dir():
        raise Stop("'Done' exists and is not a folder")
    ledger = Ledger(state)
    originals = originals_by_signed_name(folder)
    tidy_names(folder, say, dry_run)

    source_hashes = {key: file_hash(path) for key, path in originals.items()}
    newly_signed = ledger.resumable(folder, originals, source_hashes)
    todo = unsigned(folder)
    if todo and not dry_run:
        if signer is None:
            say(f"{len(todo)} unsigned file(s) would be skipped"
                + (": " + ", ".join(p.name for p in todo) if len(todo) <= 5 else "")
                + "; no signer available, sign them first")
        else:
            for path in todo:
                key = signed_name(path).casefold()
                previous = ledger.seen(Path(signed_name(path)))
                if previous and source_hashes[key] in previous.get("source_hashes", []):
                    raise Stop(f"{path.name} was already uploaded; move it out of this folder")
                if previous and not previous.get("source_hashes"):
                    say(f"warning: {path.name} has a previously uploaded name; older ledger entries "
                        "cannot establish whether its contents were uploaded. Check the portal and mail.")
            for path in todo:
                if path.suffix.lower() == ".pdf":
                    prepare_pdf(path, say)
            sign_batch(folder, todo, signer, say)
            ledger.remember_signed(folder, todo, source_hashes)
            newly_signed.update(signed_name(p).casefold() for p in todo)
    pdfs, filings = scan(folder, ledger, (lambda message: None) if dry_run else say, newly_signed)
    if dry_run:
        for path in todo:
            (pdfs if path.suffix.lower() == ".pdf" else filings).append(Item(path, path))
    if not pdfs and not filings:
        raise Stop("no new signed .pdf, .sb or .be files (names ending _Signed) to upload")
    if ui is not None and not dry_run and len(pdfs) + len(filings) > 1:
        names = ui.select_files([i.signed.name for i in pdfs + filings])
        pdfs, filings = [i for i in pdfs if i.signed.name in names], [i for i in filings if i.signed.name in names]
        if not pdfs and not filings:
            raise Stop("no files selected; nothing was uploaded")
    # Per kind: the files, their (job, date) pairs and the one customs location they name.
    numbered: dict[str, tuple[list[Item], list[tuple[str, str]], str | None]] = {}
    for kind in ("SB", "BE"):
        group = [i for i in filings if i.signed.suffix.lower() == f".{kind.lower()}"]
        if not group:
            continue
        found = {sb_location(i.signed, kind) for i in group}
        if kind == "SB" and (len(found) != 1 or None in found):
            raise Stop("cannot read one customs location from the SB file(s); each must name the same one "
                       "in its <TABLE>SB record")
        location = found.pop() if len(found) == 1 and None not in found else None
        if location:
            say(f"location {location} (read from the {kind} file)")
        numbered[kind] = (group, [job for i in group if (job := sb_job(i.signed, kind))], location)
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
            uploads.append(plan_upload(run, "eSANCHIT", ["esanchit", "submit", *mapping, *(str(i.signed) for i in pdfs)], pdfs, say))
        if filings:
            uploads.append(plan_upload(run, "filing", ["filing", "upload", *(str(i.signed) for i in filings)], filings, say))
        for upload in uploads:
            say(f"{upload.group}: {len(upload.items)} file(s) validated and ready to upload.")
            if upload.group == "filing":
                say("Filing signatures were checked for structure; final DSC validation is done by ICEGATE.")
        if not yes and not ui.confirm(f"Upload {len(pdfs) + len(filings)} signed file(s) now? This cannot be undone."):
            raise Stop("not confirmed; nothing was uploaded")
    except KeyboardInterrupt:
        raise Stop("interrupted before uploading; nothing was uploaded") from None

    stamp = datetime.now(ZoneInfo("Asia/Kolkata"))
    archive = done_dir / stamp.strftime("%Y%m%d-%H%M%S-%f")
    drafts: dict[str, Path] = {}
    submitted: list[str] = []
    rejected: list[str] = []
    failures: dict[str, dict[tuple[str, str], str]] = {"SB": {}, "BE": {}}
    # Notifications carry no time, so remember what the portal already said about these jobs before uploading.
    checked = any(jobs and location for _, jobs, location in numbered.values())
    before = portal_events(run) if checked else None
    if checked and before is None:
        say("note: could not read ICEGATE notifications; rejections will not be reported here, so watch your mail")
    watchers: dict[str, Notices | None] = {kind: notice_watcher(run, location, jobs, say, before) if before is not None else None
                                           for kind, (_, jobs, location) in numbered.items() if jobs and location}

    def finish(upload: Upload) -> None:
        """Record the upload, archive its files, then try the draft; never raises for after-work."""
        submitted.append(f"{upload.group} (plan {upload.plan_id})")
        ledger.record(upload.items, upload.plan_id, source_hashes)
        try:
            archive.mkdir(parents=True, exist_ok=True)
            for item in upload.items:
                for path in (item.original, item.signed):
                    if path is not None:
                        shutil.move(str(path), archive / path.name)
        except OSError as exc:
            say(f"warning: uploaded, but could not archive some files ({exc.strerror}); they are still in the folder")

    def draft(name: str, build: Callable[..., str], *args: object, **kwargs: object) -> None:
        try:
            text = build(*args, **kwargs)
            archive.mkdir(parents=True, exist_ok=True)
            path = archive / f"draft-{name}.html"
            path.write_text(text, encoding="utf-8")
            drafts[name] = path
        except (Stop, OSError, ValueError) as exc:
            say(f"note: {name.upper()} table not created: {exc}")

    def share(name: str) -> None:
        """Put a finished draft on the clipboard now, so each table can be pasted as soon as it exists."""
        if name in drafts:
            body = drafts[name].read_text(encoding="utf-8")
            say(f"\n{plain_table(body)}\n")
            say("-" * 60)
            say(f"{name} table copied to clipboard as rich text; paste it into the reply"
                if copy_html(body)
                else f"could not copy the {name} table; open {drafts[name]} in a browser, copy and paste")

    def wrap_up() -> None:
        if archive.exists():
            say(f"files and drafts are in {archive}; delete that folder when you are done")

    try:
        for upload in uploads:
            send_upload(run, upload, say)
            finish(upload)
            if upload.group == "eSANCHIT":
                draft("esanchit", esanchit_draft, upload.receipt or STATE / "receipts" / f"{upload.plan_id}.json", known)
                share("esanchit")
            else:
                for kind, (group, jobs, location) in numbered.items():
                    if not jobs or not location or len(jobs) != len(group):
                        say(f"note: no {kind} number check for {kind} files without a readable location and job "
                            f"number in the {kind} record; look them up in ICEGATE")
                    if not jobs or not location:
                        continue
                    say(f"Checking for {kind} numbers for job(s) {', '.join(n for n, _ in jobs)} for up to {wait:g} seconds. "
                        "Press Ctrl-C to skip the wait; the upload is already complete.")
                    name = kind.lower()
                    draft(name, sb_draft, run, location, jobs, wait, ui.pause if ui else pause_plain(say),
                          kind=kind, notices=watchers[kind], failures=failures[kind])
                    share(name)
                    for (number, _), message in failures[kind].items():
                        rejected.append(f"{kind} job {number}: {message}")
    except Stop as exc:
        wrap_up()
        raise Stop(f"{exc}\nAlready submitted in this run: {', '.join(submitted) or 'nothing'}") from None
    wrap_up()
    if rejected:
        raise Stop("ICEGATE reported a problem with the upload; fix the file and file it again only after "
                   "checking the portal:\n  " + "\n  ".join(rejected))
    return 0


def completed_runs(folder: Path) -> list[Path]:
    history = folder / "Done" if (folder / "Done").is_dir() else folder
    runs = sorted((p for p in history.iterdir() if p.is_dir() and not p.is_symlink()
                   and re.fullmatch(r"\d{8}-\d{6}-\d{6}", p.name)), reverse=True)
    if not runs:
        raise Stop("no completed runs found; provide the working folder or its Done folder")
    return runs


def recheck(folder: Path, *, run: Runner, say: Callable[[str], None], wait: float = 0,
            table_name: str = "all") -> int:
    """Restore saved tables or look up a missing SB number without signing or uploading."""
    archive = folder
    if (archive / "Done").is_dir():
        raise Stop("provide the specific completed run folder inside Done")
    say(f"Completed run: {archive}")
    for kind in ("SB", "BE"):
        name = kind.lower()
        path = archive / f"draft-{name}.html"
        if table_name not in ("all", name) or path.exists():
            continue
        files = sorted(p for p in archive.iterdir() if p.is_file() and not p.is_symlink()
                       and p.suffix.lower() == f".{name}" and p.stem.lower().endswith("_signed"))
        if not files:
            continue
        locations = {sb_location(p, kind) for p in files}
        jobs = [sb_job(p, kind) for p in files]
        if len(locations) != 1 or None in locations or None in jobs:
            raise Stop(f"cannot read the location, job number and date from every archived {kind} file")
        location = locations.pop()
        say(f"Looking up {len(jobs)} {kind} job(s); no files will be uploaded.")
        failed: dict[tuple[str, str], str] = {}
        try:
            body = sb_draft(run, location, jobs, wait, pause_plain(say), kind=kind, failures=failed,
                            notices=notice_watcher(run, location, jobs, say, []))
            path.write_text(body, encoding="utf-8")
        except (Stop, OSError, ValueError) as exc:
            say(f"{kind} table unavailable: {exc}")
        for (number, _), message in failed.items():
            say(f"{kind} job {number}: {message}")
    names = ("esanchit", "sb", "be") if table_name == "all" else (table_name,)
    shown = []
    for name in names:
        path = archive / f"draft-{name}.html"
        if not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        say(f"\n{name.upper()} table:\n{plain_table(body)}")
        shown.append((name, path, body))
    if not shown:
        raise Stop("no saved table is available for this run")
    name, path, body = shown[-1]
    say("-" * 60)
    say(f"{name.upper()} table copied to clipboard." if copy_html(body)
        else f"Could not copy the {name.upper()} table; open {path} to copy it.")
    if len(shown) > 1:
        say("The clipboard holds the last table shown. Use --table esanchit, sb or be to copy one.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sign a folder, pick types, confirm, upload, draft and archive.")
    parser.add_argument("folder", help="folder holding the files to process")
    parser.add_argument("--dry-run", action="store_true", help="show files and guessed codes only; sign and upload nothing")
    parser.add_argument("--yes", action="store_true", help="skip the upload confirmation (required when not on a terminal)")
    parser.add_argument("--no-sign", action="store_true", help="do not run the signer; use only files already signed")
    history_mode = parser.add_mutually_exclusive_group()
    history_mode.add_argument("--recheck", action="store_true", help="show tables from the specified completed run without uploading")
    history_mode.add_argument("--ls", action="store_true", help="pick a completed run from this folder's Done history and recheck it")
    parser.add_argument("--table", choices=("all", "esanchit", "sb", "be"), default="all", help="table to show and copy with --recheck")
    parser.add_argument("--wait", type=float, default=None, metavar="SECONDS",
                        help="wait for SB numbers (default: 600 seconds for upload, 0 for recheck)")
    parser.add_argument("--signer", default=os.environ.get("ICEGATE_SIGN_SCRIPT") or Path(__file__).resolve().with_name("sign-all.py"))
    args = parser.parse_args(argv)
    say = lambda text: print(text, flush=True)  # noqa: E731
    try:
        if args.wait is not None and (not math.isfinite(args.wait) or args.wait < 0):
            raise Stop("--wait must be a finite number of seconds, zero or greater")
        folder = Path(args.folder).expanduser().resolve()
        if not folder.is_dir():
            raise Stop("folder not found")
        if args.recheck or args.ls:
            if args.dry_run or args.no_sign or args.yes:
                raise Stop("--recheck/--ls cannot be combined with --dry-run, --no-sign or --yes")
            if args.ls:
                if not sys.stdin.isatty():
                    raise Stop("--ls needs an interactive terminal; use FILE_PATH --recheck to select a run directly")
                folder = Prompts().choose_run(completed_runs(folder))
            return recheck(folder, run=client_runner(), say=say,
                           wait=args.wait if args.wait is not None else 0, table_name=args.table)
        if args.table != "all":
            raise Stop("--table requires --recheck or --ls")
        signer = Path(args.signer).expanduser()
        return run_folder(folder, run=client_runner(), say=say,
                          ui=Prompts() if sys.stdin.isatty() else None, dry_run=args.dry_run, yes=args.yes,
                          signer=None if args.no_sign or not signer.is_file() else signer,
                          wait=args.wait if args.wait is not None else 600)
    except Stop as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; nothing further was done", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
