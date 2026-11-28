#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["questionary>=2.1", "rich>=13", "pymupdf>=1.24", "tzdata; sys_platform == 'win32'"]
# ///
"""One command: sign a folder, pick the document type per file, confirm, upload, draft, archive.

  scripts/icegate-run.py FOLDER [--wait SECONDS] [--dry-run] [--yes] [--no-sign]
  scripts/icegate-run.py --lookup RUN_DIR [RUN_DIR ...]   # one login, one JSON line per filed job
  scripts/icegate-run.py --refile RUN_DIR --yes          # file a "not filed" run again, once
  scripts/icegate-run.py FOLDER --yes --wait 0 --json     # upload; stdout is ONE JSON line (see README)
  scripts/icegate-run.py --drafts DIR [DIR ...]          # list the reply tables of job folders / runs (read only)
  scripts/icegate-run.py --token                         # is the DSC token plugged in? (JSON line, exit 0 / 1)

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
DRAFT_KINDS = ("esanchit", "sb", "be", "error")  # reply-table order; "error" is the rejection table
TOKEN_USB = os.environ.get("ICEGATE_TOKEN_USB") or "2ccf:080a"  # the HYP2003 DSC token (vendor:product)
USB_DEVICES = Path("/sys/bus/usb/devices")
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


class Unsure(Stop):
    """An upload may have reached the portal; the caller records it in Done before stopping."""


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
_ABBREVIATIONS = {"inv": "invoice", "ci": "commercial invoice", "pl": "packing list", "pack": "packing list"}
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


DEFAULT_CODE = "331000"  # commercial invoice including packing list: the code for a file whose name says nothing


def assign_codes(items: list[Item], ui: Prompts | None, known: dict[str, str], overrides: dict[str, str] | None = None) -> None:
    """Every PDF gets a code. With a terminal the user picks per file, the guess pre-selected;
    without one, only a code the catalogue pins to a single entry is used. When the name does not
    point to a code, DEFAULT_CODE is used (and pre-selected at a terminal)."""
    previous: dict[tuple[str, ...], str] = {}
    overrides = overrides or {}
    for item in items:
        code_override = (
            overrides.get(item.signed.name)
            or (overrides.get(item.original.name) if item.original else None)
            or overrides.get(item.label)
        )
        if code_override:
            if code_override not in known:
                raise Stop(f"{item.signed.name}: unknown document code {code_override!r}")
            item.code = code_override
            continue
        options, sure = candidates(item.label, known)
        fallback = DEFAULT_CODE if DEFAULT_CODE in known else None
        if ui is None:
            if not sure and not fallback:
                hint = f" (could be: {', '.join(options[:6])})" if options else ""
                raise Stop(f"cannot tell the document code for {item.signed.name}{hint}")
            item.code = options[0] if sure else fallback
            continue
        key = tuple(options)
        default = options[0] if sure else previous.get(key) or (options[0] if options else fallback)
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

    def choose_run(self, runs: list[Path]) -> Path | str:
        choices = [self.q.Choice("Show all completed runs combined", value="ALL")]
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
def write_json_atomic(path: Path, data: object) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, indent=1)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


class Ledger:
    """The folder's `Done` directory is the only memory of what was uploaded.

    Uploaded names and source hashes come from `Done/*/.uploaded.json`; an entry with
    `"status": "unknown"` is an upload whose outcome could not be confirmed (its files stay in the
    folder). The sign-then-upload resume marker is `Done/.pending.json`. Nothing lives anywhere else:
    delete a Done run to allow its files to be uploaded again, delete `Done` to forget everything.
    """

    def __init__(self, done_dir: Path) -> None:
        self.done_dir = done_dir
        self.pending_path = done_dir / ".pending.json"
        self.entries: dict[str, dict] = {}
        self._load_entries()
        try:
            self.pending: dict[str, dict] = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.pending = {}
        except (OSError, ValueError):
            raise Stop(f"cannot read {self.pending_path}") from None

    def _load_entries(self) -> None:
        """Read every `Done/<run>/.uploaded.json`; dot-folders (`Done/.deleted`) are not runs."""
        self.entries = {}
        done_dir = self.done_dir
        runs = sorted(p for p in done_dir.iterdir() if p.is_dir() and not p.is_symlink()
                      and not p.name.startswith(".")) if done_dir.is_dir() else []
        for run in runs:
            try:
                data = json.loads((run / ".uploaded.json").read_text(encoding="utf-8"))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, ValueError):
                raise Stop(f"cannot read {run / '.uploaded.json'}") from None
            for key, entry in data.items():
                previous = self.entries.get(key, {})
                hashes = [*previous.get("source_hashes", [])]
                hashes += [h for h in entry.get("source_hashes", []) if h not in hashes]
                self.entries[key] = {**entry, "source_hashes": hashes, "run": run.name}

    def reload(self) -> None:
        """Read the Done folder again (after a run was removed)."""
        self._load_entries()

    def remember_signed(self, folder: Path, paths: list[Path], source_hashes: dict[str, str]) -> None:
        for path in paths:
            key = signed_name(path).casefold()
            signed = folder / signed_name(path)
            self.pending[str(signed.resolve())] = {
                "original": str(path.resolve()), "source_hash": source_hashes[key],
                "prepared_hash": file_hash(path), "signed_hash": file_hash(signed),
            }
        self.pending_path.parent.mkdir(exist_ok=True)
        write_json_atomic(self.pending_path, self.pending)

    def forget_pending(self, signed: list[Path]) -> None:
        """Drop resume markers once the files are recorded, so no stale marker outlives its upload."""
        dropped = [self.pending.pop(str(path.resolve()), None) for path in signed]  # a list: drop every one
        if not any(dropped):
            return
        if self.pending:
            write_json_atomic(self.pending_path, self.pending)
        else:
            self.pending_path.unlink(missing_ok=True)

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

    def record(self, items: list[Item], plan_id: str, source_hashes: dict[str, str], archive: Path,
               status: str | None = None, baselines: dict[str, int | None] | None = None) -> None:
        """Write the items into `archive/.uploaded.json`; `status="unknown"` marks an unconfirmed upload.

        `baselines` maps a signed name (any case) to how many notices ICEGATE held for that filing just before
        the upload (None: they could not be read). Without it a later lookup cannot tell old notices from new.
        """
        now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
        archive.mkdir(parents=True, exist_ok=True)
        path = archive / ".uploaded.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        for item in items:
            key = item.signed.name.casefold()
            hashes = [source_hashes[key]] if source_hashes.get(key) else []
            data[key] = {"plan_id": plan_id, "at": now, "source_hashes": hashes, **({"status": status} if status else {})}
            if baselines and item.signed.name.casefold() in {k.casefold() for k in baselines}:
                count = next(v for k, v in baselines.items() if k.casefold() == key)
                data[key]["baseline"] = {"seen": count} if count is not None else None
            self.entries[key] = {**data[key], "run": archive.name}
        write_json_atomic(path, data)
        self.forget_pending([item.signed for item in items])


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
            what = ("upload outcome unknown; check the portal and mail first" if done.get("status") == "unknown"
                    else "already uploaded; do not upload it again")
            say(f"skipping {path.name}: {what} (plan {done['plan_id']}, {done['at']}, Done/{done['run']}); "
                "delete that Done run only to allow another upload")
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
        # The Done folder is this script's only duplicate guard (Ledger); the client's receipts in the
        # state directory must not block a file the folder says was not uploaded.
        done = run([*upload.prefix, "--allow-duplicate", "--confirm", upload.challenge])
    finally:
        signal.signal(signal.SIGINT, previous)
    say((done.stdout + done.stderr).rstrip())
    if done.returncode not in (0, 1):  # exit 1 is a refusal before anything was sent; the rest may have been
        raise Unsure(f"{upload.group} outcome is unknown (exit {done.returncode}); do NOT re-run. "
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


# ---------------------------------------------------------------- lookup: numbers and notices
POLL_SECONDS = 60  # each round is one client login (three requests); keep it gentle
RUN_NAME = re.compile(r"\d{8}-\d{6}-\d{6}")
PROBLEMS = ("not_filed", "failed")
NOT_RECEIVED_AFTER = timedelta(minutes=5)  # ICEGATE shows a file in Job Status within moments; later means it never came
IST = ZoneInfo("Asia/Kolkata")


def _portal_day(value: object) -> date | None:
    """A portal date (DD-MM-YYYY, or the UTC timestamps `portal_date` reads) as a date; None if unreadable."""
    try:
        return datetime.strptime(portal_date(value), "%d-%m-%Y").date()
    except (Stop, ValueError):
        return None


def _registered(row: dict, kind: str) -> bool:
    """The record carries its number; a BE may carry '-' as its date, an SB needs a real one."""
    has = lambda key: str(row.get(key) or "").strip() not in {"", "-", "0"}  # noqa: E731
    return has("docNumber") and (kind == "BE" or has("docDate"))


def match_rows(rows: list, kind: str, location: str, job: str, file_date: date | str,
               today: date | None = None) -> list[dict]:
    """The registered e-copy records of one filing: its location and job number, inside its date window.

    The portal's own job date is the day ICEGATE processed the file, not the date written in it (observed:
    file 20261113, e-copy 14-11-2026), so a record counts when that day lies between the file's date and
    `today`. The record's job number can be the filed one plus a two-digit year suffix (642, 64226); the
    plain number wins, the suffixed one is tried only when the plain one matched nothing.
    """
    start = file_date if isinstance(file_date, date) else date.fromisoformat(file_date)
    last = max(today or datetime.now(ZoneInfo("Asia/Kolkata")).date(), start)

    def in_window(row: dict) -> bool:
        if not row.get("jobDate"):
            return True
        day = _portal_day(row["jobDate"])
        return day is not None and start <= day <= last

    for candidate in (job, f"{job}{start:%y}"):
        found = [r for r in rows if isinstance(r, dict) and str(r.get("jobNumber")) == candidate
                 and (not location or str(r.get("location") or location).casefold() == location.casefold())
                 and in_window(r) and _registered(r, kind)]
        if found:
            return found
    return []


@dataclass
class Job:
    """One signed SB/BE filing to look up. `since` is the upload day: older notices are not about this upload."""
    kind: str
    location: str
    number: str
    date: str  # the date written in the file, ISO
    since: date
    archive: Path | None = None
    name: str = ""
    label: str = ""  # the run as the caller named it
    seen: int | None = None  # notices already there before the upload; when set they replace `since`
    refiled: bool = False
    baselined: bool = False  # the run's ledger holds that count; only then is a rejection safe to act on
    uploaded: datetime | None = None  # when this file was uploaded (the run's ledger); None for a fresh watch


def _created(event: dict) -> date | None:
    try:
        return datetime.strptime(str(event.get("created")), "%d.%m.%Y").date()
    except ValueError:
        return None


def job_events(events: list[dict], job: Job) -> list[dict]:
    """Every notice about this filing (same type, job number, file date, location), newest first."""
    return [e for e in events if e.get("doc") in (None, job.kind) and e.get("job") == job.number
            and e.get("date") in (None, job.date) and e.get("location") in (None, job.location.upper())]


def latest_event(events: list[dict] | None, job: Job) -> dict | None:
    """The newest notice that is about this upload.

    Notices carry a day, not a time, and job numbers repeat, so they are judged against what was already
    there before the upload (`seen`), or else against the upload day. A later acceptance is the newest and
    so supersedes an earlier rejection.
    """
    mine = job_events(events or [], job)
    if job.seen is not None:
        mine = mine[:max(0, len(mine) - job.seen)]
    else:
        mine = [e for e in mine if (day := _created(e)) is not None and day >= job.since]
    # "Header validation failed" is what a duplicate upload gets; it says nothing about the first upload
    mine = [e for e in mine if not str(e.get("detail") or "").lower().startswith("header validation")]
    return mine[0] if mine else None


@dataclass
class Verdict:
    job: Job
    status: str  # found, pending, not_filed, not_received, failed or multiple
    rows: list[dict]
    event: dict | None = None
    draft: Path | None = None
    note: str = ""

    def as_json(self) -> dict:
        row = self.rows[0] if self.status == "found" else {}
        shown = self.event if self.status in ("pending", *PROBLEMS) else None
        docdate = row.get("docDate")
        return {"run": self.job.label, "file": self.job.name, "kind": self.job.kind.lower(),
                "location": self.job.location, "job": self.job.number, "status": self.status,
                "number": str(row["docNumber"]) if row else None,
                "date": None if not row else ("-" if str(docdate or "").strip() in ("", "-") else _show_date(docdate)),
                "draft": str(self.draft) if self.draft else None, "note": self.note,
                "notice": {"message": shown["message"], "date": shown.get("created")} if shown else None,
                "errors": list(shown.get("errors") or []) if shown and self.status == "failed" else [],
                "refiled": self.job.refiled}


def _show_date(value: object) -> str:
    try:
        return portal_date(value)
    except Stop:
        return str(value)


def fetch(run: Runner, jobs: list[Job], *, events: bool = True) -> dict:
    """One client call, one login: a date-range search per kind plus the filing notices."""
    windows: dict[str, date] = {}
    for job in jobs:
        start = date.fromisoformat(job.date)
        windows[job.kind] = min(start, windows.get(job.kind, start))
    argv = ["ecopy", "lookup", *(a for kind, start in sorted(windows.items()) for a in ("--window", f"{kind}={start.isoformat()}"))]
    if events:
        argv.append("--events")
    for kind, location in sorted({(j.kind, j.location.upper()) for j in jobs if j.uploaded}):
        argv += ["--job-status", f"{kind}:{location}"]
    out = run(argv)
    if out.returncode:
        raise Stop(f"portal lookup failed: {(out.stderr or out.stdout).strip()[:200]}")
    try:
        data = json.loads(out.stdout)
    except ValueError:
        raise Stop("portal lookup returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("ecopy"), dict):
        raise Stop("portal lookup returned an unexpected response")
    return data


def received(rows: object, job: Job) -> bool | None:
    """Did ICEGATE's Job Status list this job as received at or after the upload? None: cannot tell."""
    if not isinstance(rows, list) or not job.uploaded:
        return None
    for row in rows:
        if not isinstance(row, dict) or str(row.get("jobNo")).strip() != job.number:
            continue
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                stamp = datetime.strptime(str(row.get("inboundRecievedTimestamp")), fmt).replace(tzinfo=IST)
            except ValueError:
                continue
            if stamp >= job.uploaded:
                return True
            break
    return False


def judge(jobs: list[Job], data: dict, now: datetime | None = None) -> list[Verdict]:
    try:
        today = date.fromisoformat(str(data.get("today")))
    except ValueError:
        today = None
    events = data.get("events") if isinstance(data.get("events"), list) else None
    unread = f" (notices unreadable: {data['events_error']})" if events is None and data.get("events_error") else ""
    out = []
    now = now or datetime.now(IST)
    job_status = data.get("job_status") if isinstance(data.get("job_status"), dict) else {}
    for job in jobs:
        rows = data["ecopy"].get(job.kind) or []
        found = match_rows(rows if isinstance(rows, list) else [], job.kind, job.location, job.number, job.date, today)
        event = latest_event(events, job)
        if len(found) > 1:
            out.append(Verdict(job, "multiple", found, note=f"{len(found)} {job.kind} records match; check ICEGATE"))
        elif found:
            out.append(Verdict(job, "found", found))
        elif event and event.get("problem") == "not_filed":
            out.append(Verdict(job, "not_filed", [], event,
                               note="ICEGATE did not file it" + (f"; file it again: icerun --refile {job.label} --yes"
                                                                 if job.baselined else "")))
        elif event and event.get("problem") == "failed":
            out.append(Verdict(job, "failed", [], event, note="ICEGATE rejected the file with errors; fix it"))
        elif (received(job_status.get(f"{job.kind}:{job.location.upper()}"), job) is False
              and now - job.uploaded >= NOT_RECEIVED_AFTER):
            out.append(Verdict(job, "not_received", [], event,
                               note="ICEGATE never received the file" + (f"; file it again: icerun --refile {job.label} --yes"
                                                                       if job.baselined else "")))
        else:
            out.append(Verdict(job, "pending", [], event, note=(
                "accepted for processing; the number is not registered yet" if event else "no number or notice yet") + unread))
    for verdict in out:
        if verdict.job.archive and not verdict.job.baselined:
            verdict.note = (verdict.note + "; " if verdict.note else "") + (
                "no baseline: the notices before the upload were not recorded, so an older notice of the same day may be "
                "counted; check the notices by hand" if verdict.status in PROBLEMS else "no baseline")
    return out


def baseline(run: Runner, jobs: list[Job], say: Callable[[str], None]) -> None:
    """Count the notices already there, before uploading, so later ones can be told apart."""
    why = ""
    try:
        data = fetch(run, [], events=True)
        events = data.get("events")
        why = str(data.get("events_error") or "")
    except Stop as exc:
        events, why = None, str(exc)
    if not isinstance(events, list):
        say(f"note: could not read ICEGATE notices ({why or 'no data'}); rejections will be judged by their date only")
    else:
        for job in jobs:
            job.seen = len(job_events(events, job))
            job.baselined = True


def poll(run: Runner, jobs: list[Job], wait: float, say: Callable[[str], None],
         pause: Callable[[str, float], None]) -> list[Verdict]:
    """Look up every job with ONE client call per round until none is pending or `wait` seconds are over."""
    deadline = time.monotonic() + wait
    verdicts: list[Verdict] = []
    try:
        while True:
            try:
                verdicts = judge(jobs, fetch(run, jobs))
            except Stop as exc:
                say(f"note: check failed: {exc}")
            if verdicts and not any(v.status == "pending" for v in verdicts):
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            delay = min(POLL_SECONDS, remaining)
            waiting = sum(v.status == "pending" for v in verdicts) or len(jobs)
            pause(f"{waiting} job(s) not registered yet; checking again in {delay:.0f}s (Ctrl-C to stop waiting)", delay)
    except KeyboardInterrupt:
        raise Stop("stopped waiting for the numbers") from None
    if not verdicts:
        raise Stop("the portal could not be checked")
    return verdicts


def error_html(failed: list[Verdict]) -> str:
    rows = [[v.job.number, v.job.name, error] for v in failed for error in (v.event or {}).get("errors") or [v.event["message"]]]
    return page("ICEGATE rejected the filing(s) below with these errors:", [table(["Job No", "File Name", "Error"], rows)])


def write_drafts(verdicts: list[Verdict]) -> None:
    """Write the draft tables a lookup makes possible; an existing draft is never overwritten.

    A kind's number table is written once every job of that kind in the run has its number, and an error
    table for the failed ones (rewritten when the errors change).
    """
    groups: dict[tuple[Path, str], list[Verdict]] = {}
    for verdict in verdicts:
        if verdict.job.archive:
            groups.setdefault((verdict.job.archive, verdict.job.kind), []).append(verdict)
    for (archive, kind), group in groups.items():
        path = archive / f"draft-{kind.lower()}.html"
        found = [v for v in group if v.status == "found"]
        try:
            if path.exists():
                for v in found:
                    v.draft, v.note = path, "draft already exists; left as it is"
            elif found and len(found) == len(group):
                path.write_text((be_html if kind == "BE" else sb_html)([v.rows[0] for v in group]), encoding="utf-8")
                for v in found:
                    v.draft = path
            else:
                for v in found:
                    v.note = f"{len(group) - len(found)} other {kind} job(s) of this run have no number yet; no table written"
        except (Stop, OSError, ValueError) as exc:
            for v in found:
                v.note = f"table not written: {exc}"
    for archive in {v.job.archive for v in verdicts if v.status == "failed" and v.job.archive}:
        failed = [v for v in verdicts if v.status == "failed" and v.job.archive == archive]
        path = archive / "draft-error.html"
        try:
            text = error_html(failed)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
            for v in failed:
                v.draft = path
        except OSError as exc:
            for v in failed:
                v.note += f" (error table not written: {exc.strerror})"


def read_entries(archive: Path) -> dict:
    try:
        data = json.loads((archive / ".uploaded.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, NotADirectoryError):
        return {}
    except (OSError, ValueError):
        raise Stop(f"cannot read {archive / '.uploaded.json'}") from None
    return data if isinstance(data, dict) else {}


def _uploaded_at(entry: dict) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(entry.get("at")))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=IST)


def run_jobs(archive: Path, label: str) -> tuple[list[Job], list[dict]]:
    """The signed SB/BE files archived in one completed run (Done/<timestamp>), as jobs to look up.

    Files whose location or job number cannot be read come back as ready-made `unreadable` results.
    """
    if not archive.is_dir() or (archive / "Done").is_dir() or not RUN_NAME.fullmatch(archive.name):
        raise Stop(f"{label}: not a completed run folder (expected Done/<timestamp>)")
    entries = read_entries(archive)
    days = []
    for entry in entries.values():
        try:
            days.append(date.fromisoformat(str(entry.get("at"))[:10]))
        except (ValueError, AttributeError):
            pass
    since = min(days) if days else datetime.strptime(archive.name, "%Y%m%d-%H%M%S-%f").date()
    jobs: list[Job] = []
    bad: list[dict] = []
    for path in sorted(archive.iterdir()):
        if (not path.is_file() or path.is_symlink() or path.suffix.lower() not in FILING_SUFFIXES
                or not path.stem.lower().endswith("_signed")):
            continue
        kind = path.suffix[1:].upper()
        location, job = sb_location(path, kind), sb_job(path, kind)
        if not location or not job:
            bad.append({"run": label, "file": path.name, "kind": kind.lower(), "location": location, "job": None,
                        "status": "unreadable", "number": None, "date": None, "draft": None, "refiled": False,
                        "note": "cannot read the location and job number from this file", "notice": None, "errors": []})
            continue
        entry = entries.get(path.name.casefold()) or {}
        refile, stored = entry.get("refile"), entry.get("baseline")
        stored = stored.get("seen") if isinstance(stored, dict) else None
        stored = stored if isinstance(stored, int) and not isinstance(stored, bool) else None
        seen = refile.get("seen") if isinstance(refile, dict) and isinstance(refile.get("seen"), int) else stored
        jobs.append(Job(kind, location, job[0], job[1], since, archive, path.name, label, seen, bool(refile),
                        baselined=stored is not None, uploaded=_uploaded_at(entry)))
    return jobs, bad


def lookup(runs: list[str], *, run: Runner, say: Callable[[str], None], emit: Callable[[str], None]) -> int:
    """`--lookup`: ONE login for every job of every given run; one JSON line per job on `emit`."""
    jobs: list[Job] = []
    results: list[dict] = []
    for text in runs:
        found, bad = run_jobs(Path(text).expanduser().resolve(), text)
        if not found and not bad:
            say(f"{text}: no signed .sb/.be files in this run")
        jobs += found
        results += bad
    verdicts: list[Verdict] = []
    if jobs:
        verdicts = judge(jobs, fetch(run, jobs))
        write_drafts(verdicts)
    for item in [*(v.as_json() for v in verdicts), *results]:
        emit(json.dumps(item, ensure_ascii=False))
    return 0


def token_present(devices: Path | None = None, usb_id: str | None = None) -> bool:
    """Is the DSC token plugged in? Read only: looks for its USB id under sysfs."""
    vendor, _, product = (usb_id or TOKEN_USB).partition(":")
    try:
        for dev in (devices or USB_DEVICES).glob("*"):
            try:
                if (dev / "idVendor").read_text().strip() == vendor and (dev / "idProduct").read_text().strip() == product:
                    return True
            except OSError:
                continue
    except OSError:
        pass
    return False


def token(emit: Callable[[str], None], devices: Path | None = None) -> int:
    """`--token`: one JSON line, exit 0 when the token is plugged in, 1 when not."""
    present = token_present(devices)
    emit(json.dumps({"token": "HYP2003", "usb": TOKEN_USB, "present": present}))
    return 0 if present else 1


def list_drafts(paths: list[str], emit: Callable[[str], None]) -> int:
    """`--drafts`: the reply tables of job folders (holding Done/) or of completed runs, one JSON line each:
    {"run", "kind": esanchit|sb|be|error, "draft"}. eSANCHIT first, then SB, then BE, then the error table."""
    found: list[tuple[int, str, str, str]] = []
    for text in paths:
        folder = Path(text).expanduser().resolve()
        if not folder.is_dir():
            raise Stop(f"{text}: not a folder")
        done = folder / "Done"
        runs = ([p for p in done.iterdir() if p.is_dir() and not p.is_symlink() and RUN_NAME.fullmatch(p.name)]
                if done.is_dir() else [folder])
        for run in runs:
            for kind in DRAFT_KINDS:
                path = run / f"draft-{kind}.html"
                if path.is_file():
                    found.append((DRAFT_KINDS.index(kind), str(path), str(run), kind))
    for _, path, run, kind in sorted(found):
        emit(json.dumps({"run": run, "kind": kind, "draft": path}, ensure_ascii=False))
    return 0


def mark_refile(archive: Path, seen: dict[str, int], plan_id: str) -> None:
    """Record, in the run's own ledger, that these files are being filed again (once per run)."""
    data = read_entries(archive)
    now = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")
    for name, count in seen.items():
        data.setdefault(name.casefold(), {})["refile"] = {"at": now, "plan_id": plan_id, "seen": count}
    write_json_atomic(archive / ".uploaded.json", data)


def clear_refile(archive: Path, names: list[str]) -> None:
    data = read_entries(archive)
    for name in names:
        if isinstance(data.get(name.casefold()), dict):
            data[name.casefold()].pop("refile", None)
    write_json_atomic(archive / ".uploaded.json", data)


def refile(archive: Path, *, run: Runner, say: Callable[[str], None], yes: bool = False, dry_run: bool = False) -> int:
    """File an archived run's SB/BE again, once, when ICEGATE says it was not filed or never received it.

    The signed file in the run folder is uploaded as it is (nothing is signed). The Done ledger refuses
    to upload a folder's files twice; this path is for one run and only for jobs whose newest notice is a
    bare "not filed" one, or that Job Status never listed. It is recorded in the run's `.uploaded.json` before sending, so it can happen
    once per run and a crash cannot make it happen twice.
    """
    jobs, _ = run_jobs(archive, str(archive))
    if not jobs:
        raise Stop("no readable signed .sb/.be files in this run; only SB/BE filings can be filed again")
    if (again := [j.name for j in jobs if j.refiled]):
        raise Stop(f"this run was already filed again ({', '.join(again)}); a second refile is refused. "
                   "Check the portal and your mail")
    if (blind := [j.name for j in jobs if not j.baselined]):
        raise Stop(f"no baseline for {', '.join(blind)}: the notices before the upload were not recorded, so a "
                   "rejection cannot be told from an older one; check the notices by hand. Nothing was filed")
    data = fetch(run, jobs)
    verdicts = judge(jobs, data)
    for v in verdicts:
        say(f"{v.job.kind} job {v.job.number}: {v.status}")
    targets = [v for v in verdicts if v.status in ("not_filed", "not_received")]
    if not targets:
        raise Stop("nothing to file again: ICEGATE did not report any job as not filed or not received"
                   + (f" (notices unreadable: {data['events_error']})" if data.get("events") is None else ""))
    items = [Item(archive / v.job.name) for v in targets]
    for item in items:
        if item.signed.is_symlink() or not item.signed.is_file():
            raise Stop(f"{item.signed.name} is missing from the run folder")
    for v in targets:
        say(f"would file again: {v.job.name}  ({(v.event or {}).get('message') or v.note})")
    if dry_run:
        say("dry run only; nothing uploaded")
        return 0
    if not yes:
        raise Stop("filing again uploads the signed file once more; check the portal, then pass --yes")
    upload = plan_upload(run, "filing", ["filing", "upload", *(str(i.signed) for i in items)], items, say)
    seen = {v.job.name: len(job_events(data["events"], v.job)) for v in targets}
    mark_refile(archive, seen, upload.plan_id)
    try:
        send_upload(run, upload, say)
    except Unsure:
        say("the refile stays recorded in .uploaded.json; do not run it again until the portal is checked")
        raise
    except Stop:
        clear_refile(archive, list(seen))  # refused before anything was sent
        raise
    say(f"filed again: {', '.join(seen)}. Check with: icerun --lookup {archive}")
    return 0


# ---------------------------------------------------------------- offering to redo an uploaded file
def run_contents(run: Path) -> list[str]:
    """Every file in a Done run (the bookkeeping `.uploaded.json` is left out; it goes with the run)."""
    return sorted(p.name + ("/" if p.is_dir() else "") for p in run.iterdir() if p.name != ".uploaded.json")


def run_numbers(run: Path) -> list[str]:
    """The SB/BE numbers already known from the run's saved reply tables (`draft-sb.html`, `draft-be.html`)."""
    found: list[str] = []
    for kind in ("sb", "be"):
        try:
            text = (run / f"draft-{kind}.html").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        for _, number, _ in re.findall(r"<tr><td>([^<]*)</td><td>([^<]*)</td><td>([^<]*)</td></tr>", text):
            if number.strip():
                found.append(f"{kind.upper()} {html.unescape(number.strip())}")
    return found


def retire_run(run: Path, done_dir: Path) -> str:
    """Take a Done run out of the ledger without destroying it; says where it went.

    The desktop trash (`gio trash`) when it works, otherwise `Done/.deleted/<run>`. Never a plain delete.
    """
    if run.parent != done_dir or run.is_symlink() or not run.is_dir() or run.name.startswith("."):
        raise Stop(f"refusing to remove {run}: not a run folder inside {done_dir}")
    gio = shutil.which("gio")
    if gio:
        try:
            result = subprocess.run([gio, "trash", str(run)], capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode == 0 and not run.exists():
            return f"moved Done/{run.name} to the trash"
    graveyard = done_dir / ".deleted"
    graveyard.mkdir(exist_ok=True)
    target = graveyard / run.name
    for n in range(1, 1000):
        if not target.exists():
            break
        target = graveyard / f"{run.name}.{n}"
    try:
        shutil.move(str(run), str(target))
    except OSError as exc:
        raise Stop(f"could not remove Done/{run.name}: {exc.strerror or exc}; nothing was uploaded") from None
    return f"moved Done/{run.name} to Done/.deleted/{target.name} (the trash was not available)"


def offer_redo(todo: list[Path], ledger: Ledger, source_hashes: dict[str, str], done_dir: Path,
               ui: Prompts | None, say: Callable[[str], None]) -> None:
    """Stop on files already uploaded (or of unknown outcome), unless the person agrees to remove that run.

    Every such file is collected first and each run is asked about once. Only an interactive terminal is
    asked, and the answer is always a fresh yes: `--yes` never agrees to a second filing.
    """
    for _ in range(1000):  # each round removes runs, so this ends; the bound is only a guard
        dups: dict[str, list[Path]] = {}
        for path in todo:
            previous = ledger.seen(Path(signed_name(path)))
            if previous and source_hashes[signed_name(path).casefold()] in previous.get("source_hashes", []):
                dups.setdefault(previous["run"], []).append(path)
        if not dups:
            return
        if ui is None:
            first_run, paths = next(iter(dups.items()))
            raise Stop(f"{paths[0].name} was already uploaded or its outcome is unknown (Done/{first_run}); "
                       "delete that Done run to upload it again (run icerun in a terminal to be offered that)")
        for name, paths in dups.items():
            run = done_dir / name
            recorded = read_entries(run)
            signed = [signed_name(p) for p in paths]
            unknown = any(recorded.get(s.casefold(), {}).get("status") == "unknown" for s in signed)
            detail = "outcome unknown" if unknown else "uploaded"
            if numbers := run_numbers(run):
                detail += "; " + ", ".join(numbers)
            if not ui.confirm(
                    f"Done/{name} already holds {', '.join(signed)} ({detail}).\n"
                    f"  Everything in that run would be removed: {', '.join(run_contents(run)) or '(nothing else)'}.\n"
                    f"  Uploading again files {'them' if len(signed) > 1 else 'it'} on ICEGATE a second time "
                    "(a duplicate filing).\n"
                    "  Delete that run and upload again?"):
                raise Stop(f"{paths[0].name} was already uploaded or its outcome is unknown (Done/{name}); "
                           "delete that Done run to upload it again")
        for name in dups:
            say(retire_run(done_dir / name, done_dir))
        ledger.reload()
    raise Stop("too many earlier runs hold these files; nothing was uploaded")


# ---------------------------------------------------------------- the run
def run_folder(folder: Path, *, run: Runner, say: Callable[[str], None],
               ui: Prompts | None, dry_run: bool = False, yes: bool = False,
               signer: Path | None = None, wait: float = 0,
               doc_type: str | None = None, doc_type_map: list[str] | None = None,
               report: dict | None = None) -> int:
    """`report`, when given, collects what was uploaded: report["runs"] gets one entry per upload group
    ({"dir", "kind": "filing"|"esanchit", "files", "draft"}), for `--json`."""
    done_dir = folder / "Done"
    if done_dir.exists() and not done_dir.is_dir():
        raise Stop("'Done' exists and is not a folder")
    ledger = Ledger(done_dir)
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
            offer_redo(todo, ledger, source_hashes, done_dir, ui, say)
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
    overrides: dict[str, str] = {}
    if doc_type:
        overrides.update({i.signed.name: doc_type for i in pdfs})
    for pair in doc_type_map or []:
        if "=" not in pair:
            raise Stop(f"--doc-type-map expects FILE=CODE, got {pair!r}")
        name, code = pair.split("=", 1)
        name, code = name.strip(), code.strip()
        matched = False
        for item in pdfs:
            if name in (item.signed.name, item.original.name if item.original else "", item.label):
                overrides[item.signed.name] = code
                matched = True
        if not matched:
            raise Stop(f"--doc-type-map file not found: {name}")

    if dry_run:
        for item in pdfs:
            code = overrides.get(item.signed.name)
            if code:
                say(f"eSANCHIT  {item.signed.name}  ->  {code}  {known.get(code, '')}")
            else:
                options, sure = candidates(item.label, known)
                say(f"eSANCHIT  {item.signed.name}  ->  " + (f"{options[0]}  {known[options[0]]}" if sure
                    else "you will choose" + (f" (suggested: {', '.join(options[:4])})" if options else "")))
        for item in filings:
            say(f"filing    {item.signed.name}")
        say("dry run only; nothing signed or uploaded")
        return 0
    if ui is None and not yes:
        raise Stop("not a terminal: pass --yes to upload without a prompt")
    assign_codes(pdfs, ui, known, overrides)
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
    today = stamp.date()
    watched = [Job(kind, location, job[0], job[1], today, archive, item.signed.name, str(archive))
               for kind, (group, _, location) in numbered.items() if location
               for item in group if (job := sb_job(item.signed, kind))]
    if watched and not dry_run:
        # Notices carry a day, not a time: count what the portal already says, whatever --wait is, and keep the
        # count in the run's ledger so a later --lookup or --refile can tell a new notice from an old one.
        baseline(run, watched, say)
    baselines = {job.name: job.seen for job in watched}

    def finish(upload: Upload) -> None:
        """Record the upload, archive its files, then try the draft; never raises for after-work."""
        submitted.append(f"{upload.group} (plan {upload.plan_id})")
        if report is not None:
            report.setdefault("runs", []).append({"dir": str(archive), "kind": "esanchit" if upload.group == "eSANCHIT" else "filing",
                                                  "files": [i.signed.name for i in upload.items], "draft": None})
        ledger.record(upload.items, upload.plan_id, source_hashes, archive, baselines=baselines)
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
        if archive.exists() and any(p.name != ".uploaded.json" for p in archive.iterdir()):
            say(f"files and drafts are in {archive}; delete that folder when you are done")

    try:
        for upload in uploads:
            try:
                send_upload(run, upload, say)
            except Unsure as exc:
                # Done is the only memory: mark these files so no later run sends them again unseen.
                try:
                    ledger.record(upload.items, upload.plan_id, source_hashes, archive, status="unknown", baselines=baselines)
                except OSError as err:
                    raise Stop(f"{exc}\nWARNING: could not record this in {archive} ({err.strerror}); "
                               "move the files out of this folder yourself") from None
                raise Stop(f"{exc}\nThe files stay in this folder and are marked in {archive}; "
                           "delete that Done run only after checking, to allow another upload") from None
            finish(upload)
            if upload.group == "eSANCHIT":
                draft("esanchit", esanchit_draft, upload.receipt or STATE / "receipts" / f"{upload.plan_id}.json", known)
                if report is not None and "esanchit" in drafts:
                    report["runs"][-1]["draft"] = str(drafts["esanchit"])
                share("esanchit")
            else:
                for kind, (group, jobs, location) in numbered.items():
                    if not jobs or not location or len(jobs) != len(group):
                        say(f"note: no {kind} number check for {kind} files without a readable location and job "
                            f"number in the {kind} record; look them up in ICEGATE")
                if not watched:
                    continue
                if wait <= 0:
                    say(f"Not waiting for SB/BE numbers (--wait 0). Look them up later with: icerun --lookup {archive}")
                    continue
                say(f"Checking for SB/BE numbers for job(s) {', '.join(j.number for j in watched)} for up to {wait:g} seconds. "
                    "Press Ctrl-C to skip the wait; the upload is already complete.")
                try:
                    verdicts = poll(run, watched, wait, say, ui.pause if ui else pause_plain(say))
                except Stop as exc:
                    say(f"note: SB/BE numbers not checked: {exc}")
                    continue
                write_drafts(verdicts)
                for kind in dict.fromkeys(j.kind for j in watched):
                    path = archive / f"draft-{kind.lower()}.html"
                    if path.is_file():
                        drafts[kind.lower()] = path
                        share(kind.lower())
                    else:
                        say(f"note: {kind} table not created: not every {kind} number is registered yet; "
                            f"run: icerun --recheck {archive}")
                for v in verdicts:
                    notice = (v.event or {}).get("message", "")
                    if v.status == "not_filed":
                        rejected.append(f"{v.job.kind} job {v.job.number}: NOT FILED by ICEGATE ({notice}). "
                                        + (f"File it again with: icerun --refile {archive} --yes" if v.job.baselined
                                         else "No baseline was recorded: check the notices by hand before filing again"))
                    elif v.status == "failed":
                        rejected.append(f"{v.job.kind} job {v.job.number}: FAILED with errors:\n      "
                                        + "\n      ".join((v.event or {}).get("errors") or [notice])
                                        + (f"\n    error table: {v.draft}" if v.draft else ""))
                    elif v.status in ("multiple", "pending"):
                        say(f"note: {v.job.kind} job {v.job.number}: {v.note}")
    except Stop as exc:
        wrap_up()
        raise Stop(f"{exc}\nAlready submitted in this run: {', '.join(submitted) or 'nothing'}") from None
    wrap_up()
    if rejected:
        raise Stop("ICEGATE reported a problem with the upload:\n  " + "\n  ".join(rejected))
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
    """Restore saved tables or look up missing SB/BE numbers without signing or uploading."""
    archive = folder
    if (archive / "Done").is_dir():
        raise Stop("provide the specific completed run folder inside Done")
    say(f"Completed run: {archive}")
    jobs, _ = run_jobs(archive, str(archive)) if RUN_NAME.fullmatch(archive.name) else ([], [])
    todo = [j for j in jobs if table_name in ("all", j.kind.lower()) and not (archive / f"draft-{j.kind.lower()}.html").exists()]
    if todo:
        say(f"Looking up {len(todo)} job(s); no files will be uploaded.")
        try:
            verdicts = poll(run, todo, wait, say, pause_plain(say))
        except Stop as exc:
            say(f"numbers unavailable: {exc}")
        else:
            write_drafts(verdicts)
            for v in verdicts:
                say(f"{v.job.kind} job {v.job.number}: {v.status}" + (f" ({v.note})" if v.note else ""))
                if v.status in PROBLEMS and v.event:
                    say("    " + "\n    ".join(v.event.get("errors") or [v.event["message"]]))
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


def extract_table_rows(html_text: str) -> tuple[list[str], list[list[str]]]:
    """Extract headers and data rows from an HTML table block."""
    headers: list[str] = []
    rows: list[list[str]] = []
    for block in re.findall(r"<table.*?</table>", html_text, re.S):
        for tr in re.findall(r"<tr>(.*?)</tr>", block, re.S):
            ths = [html.unescape(c.strip()) for c in re.findall(r"<th>(.*?)</th>", tr, re.S)]
            if ths and not headers:
                headers = ths
            tds = [html.unescape(c.strip()) for c in re.findall(r"<td>(.*?)</td>", tr, re.S)]
            if tds and tds not in rows:
                rows.append(tds)
    return headers, rows


def recheck_all(runs: list[Path], *, say: Callable[[str], None], table_name: str = "all") -> int:
    """Combine and show saved tables from all completed runs without selecting just one."""
    names = ("esanchit", "sb", "be") if table_name == "all" else (table_name,)
    copied_any = False
    for name in names:
        all_rows: list[list[str]] = []
        headers: list[str] = []
        for archive in runs:
            path = archive / f"draft-{name}.html"
            if not path.is_file():
                continue
            h, r = extract_table_rows(path.read_text(encoding="utf-8"))
            if h and not headers:
                headers = h
            for row in r:
                if row not in all_rows:
                    all_rows.append(row)
        if all_rows and headers:
            intro = "Find below the IRN and DRN details:" if name == "esanchit" else f"Find below {name.upper()} Numbers:"
            combined_html = page(intro, [table(headers, all_rows)])
            say(f"\nCombined {name.upper()} table ({len(all_rows)} row(s) from {len(runs)} completed run(s)):\n{plain_table(combined_html)}")
            if copy_html(combined_html):
                copied_any = True
                copied_last = True
            else:
                copied_any = True
                copied_last = False
    if not copied_any:
        raise Stop(f"no saved {table_name.upper()} tables found across completed runs")
    say("-" * 60)
    if copied_last:
        say("Combined table copied to clipboard.")
    else:
        say("Could not copy the combined table to clipboard.")
    if len(names) > 1:
        say("The clipboard holds the last table shown. Use --table esanchit, sb or be to copy one specifically.")
    return 0


def show_notifications(*, run: Runner, say: Callable[[str], None], days: int = 1,
                       table_name: str = "all", sb: bool = False, be: bool = False,
                       raw: bool = False, as_json: bool = False) -> int:
    if table_name == "esanchit":
        raise Stop("notifications only track SB and BE filings, not eSANCHIT")
    argv = ["notifications", "--days", str(days)]
    if not raw:
        argv.append("--events")
    doc = "SB" if sb or table_name == "sb" else ("BE" if be or table_name == "be" else None)
    if doc:
        argv.extend(["--doc", doc])
    if as_json:
        argv.append("--json")
    out = run(argv)
    if out.stdout:
        say(out.stdout.rstrip())
    if out.stderr:
        print(out.stderr.rstrip(), file=sys.stderr, flush=True)
    return out.returncode


def parse_days_value(val: str | int | None, default: int) -> int:
    """`--days N`: a whole number of days, 1 being today, 2 today and yesterday."""
    if val is None:
        return default
    try:
        n = int(str(val).strip())
    except ValueError:
        raise Stop(f"invalid --days value: {val!r}; expected a whole number of days (e.g. --days 2)") from None
    if n < 1:
        raise Stop("--days must be at least 1")
    return n


def filter_rows(rows: list, *, job: str | None = None, loc: str | None = None) -> list[dict]:
    """Narrow one e-copy fetch locally. A job number matches exactly; failing that, with a two-digit year suffix."""
    rows = [r for r in rows if isinstance(r, dict)]
    if loc:
        rows = [r for r in rows if str(r.get("location") or "").casefold() == loc.casefold()]
    if job:
        number = job.strip()
        exact = [r for r in rows if str(r.get("jobNumber")) == number]
        rows = exact or [r for r in rows if re.fullmatch(rf"{re.escape(number)}\d{{2}}", str(r.get("jobNumber")))]
    return rows


def listing_table(kind: str, rows: list[dict]) -> str:
    cell = lambda row, key: "-" if not str(row.get(key) or "").strip() or row.get(key) == "-" else _show_date(row[key])  # noqa: E731
    body = [[str(r.get("jobNumber", "")), str(r.get("docNumber") or "-"), cell(r, "docDate"), str(r.get("location") or ""),
             cell(r, "jobDate")] for r in rows]
    if not body:
        return f"no {kind} records match"
    return plain_table(table(["Job No", f"{kind} Number", f"{kind} Date", "Location", "Job Date"], body))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="icerun",
        description="Sign, upload, and track customs filings, review run history, and check ICEGATE portal status.",
        allow_abbrev=False,  # `--day` or `--not` must not silently mean `--days` or `--notices`
    )
    parser.add_argument("folder_or_command", nargs="?", default=None,
                        help="folder to process, or command ('check', 'history')")
    parser.add_argument("sub_target", nargs="?", default=None,
                        help="target for check ('notices', 'sb', 'be') or folder for 'history'")
    parser.add_argument("--dry-run", action="store_true", help="show files and guessed codes only; sign and upload nothing")
    parser.add_argument("--yes", action="store_true", help="skip the upload confirmation (required when not on a terminal)")
    parser.add_argument("--no-sign", action="store_true", help="do not run the signer; use only files already signed")

    history_mode = parser.add_mutually_exclusive_group()
    history_mode.add_argument("--history", action="store_true",
                              help="show completed runs and copy summary table to clipboard (default: combines all runs)")
    history_mode.add_argument("--select", "--ls", dest="select", action="store_true",
                              help="pick a completed run from history instead of combining all")
    history_mode.add_argument("--all", action="store_true",
                              help="combine and show tables from all completed runs (same as --history)")
    history_mode.add_argument("--recheck", action="store_true",
                              help="show tables from a specific completed run without uploading")
    history_mode.add_argument("--notices", action="store_true",
                              help="show ICEGATE notifications without uploading")
    history_mode.add_argument("--lookup", nargs="+", metavar="RUN_DIR",
                              help="ONE login for every job of these Done/<timestamp> runs: one JSON line per job "
                                   "(found / pending / not_filed / not_received / failed), missing draft tables written")
    history_mode.add_argument("--drafts", nargs="+", metavar="DIR",
                              help="read only: one JSON line per reply table (draft-*.html) of these job folders "
                                   "(holding Done/) or completed runs: {run, kind, draft}")
    history_mode.add_argument("--parse-response", metavar="FILE",
                              help="read only, offline: the errors or numbers in an .ack/.nak result file ICEGATE mailed; "
                                   "one JSON line {type, doc, jobs: [{location, job, job_date, number, date, errors}]}")
    history_mode.add_argument("--token", action="store_true",
                              help="read only: is the DSC token plugged in? One JSON line, exit 0 when present, 1 when not")
    history_mode.add_argument("--refile", metavar="RUN_DIR",
                              help="upload the archived signed SB/BE of this run again, once, only when ICEGATE's newest "
                                   "notice says it was not filed (needs --yes; --dry-run to preview)")

    parser.add_argument("--table", choices=("all", "esanchit", "sb", "be"), default="all",
                        help="table to show and copy with history, or document type filter")
    parser.add_argument("--days", type=str, default=None, metavar="N",
                        help="check window in days: --days 1 is today (the default), --days 2 today and yesterday")
    parser.add_argument("--raw", action="store_true",
                        help="show raw notification list instead of parsed filing events")
    parser.add_argument("--job", metavar="N", help="with --sb/--be: only this job number (filtered locally)")
    parser.add_argument("--loc", metavar="CODE", help="with --sb/--be: only this customs location (filtered locally)")
    parser.add_argument("--sb", action="store_true", help="check Shipping Bills")
    parser.add_argument("--be", action="store_true", help="check Bills of Entry")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output. Filing a folder: narration goes to stderr and stdout is one final "
                             "JSON line {ok, runs: [{dir, kind, files, draft}]} (needs --yes)")
    parser.add_argument("--wait", type=float, default=None, metavar="SECONDS",
                        help="seconds to wait for SB/BE numbers after uploading (default 600; 0 uploads and archives only, no check at all); "
                             "with --recheck, how long to keep looking (default 0: one look)")
    parser.add_argument("--type", "--doc-type", dest="doc_type", default=None,
                        help="document code applied to every file")
    parser.add_argument("--doc-type-map", action="append", default=[],
                        help="per-file document code (e.g. --doc-type-map FILE=CODE)")
    parser.add_argument("--signer", default=os.environ.get("ICEGATE_SIGN_SCRIPT") or Path(__file__).resolve().with_name("sign-all.py"))
    args = parser.parse_args(argv)
    say = lambda text: print(text, flush=True)  # noqa: E731
    try:
        first = (args.folder_or_command or "").strip()
        second = (args.sub_target or "").strip()
        cmd_lower = first.lower()

        # 0. LOOKUP / REFILE ROUTES (one run folder each; no folder to sign)
        if args.lookup:
            if first or second or args.dry_run or args.no_sign or args.yes:
                raise Stop("--lookup takes only run folders (and --json); nothing is signed or uploaded")
            return lookup(args.lookup, run=client_runner(), say=lambda text: print(text, file=sys.stderr, flush=True),
                          emit=lambda text: print(text, flush=True))
        if args.parse_response:
            if first or second or args.dry_run or args.no_sign or args.yes:
                raise Stop("--parse-response takes only the file")
            out = client_runner()(["parse-response", str(Path(args.parse_response).expanduser().resolve()), "--json"])
            if out.returncode:
                raise Stop((out.stderr or out.stdout).strip().removeprefix("error: ") or "cannot read the file")
            print(out.stdout.strip(), flush=True)
            return 0
        if args.token:
            if first or second or args.dry_run or args.no_sign or args.yes:
                raise Stop("--token takes no other arguments")
            return token(emit=lambda text: print(text, flush=True))
        if args.drafts:
            if first or second or args.dry_run or args.no_sign or args.yes:
                raise Stop("--drafts takes only folders (and --json); nothing is signed or uploaded")
            return list_drafts(args.drafts, emit=lambda text: print(text, flush=True))
        if args.refile:
            if first or second or args.no_sign:
                raise Stop("--refile takes one run folder, --yes and --dry-run only")
            return refile(Path(args.refile).expanduser().resolve(), run=client_runner(), say=say,
                          yes=args.yes, dry_run=args.dry_run)

        # 1. CHECK ROUTE
        is_notices_explicit = args.notices or (cmd_lower in ("notices", "notifications"))
        is_check_cmd = (cmd_lower == "check")
        first_is_folder = bool(first) and Path(first).expanduser().is_dir()
        is_portal_shortcut = (args.sb or args.be) and not is_notices_explicit and not first_is_folder

        is_check = is_check_cmd or is_notices_explicit or is_portal_shortcut
        if is_check:
            if args.dry_run or args.no_sign or args.yes:
                raise Stop("check commands cannot be combined with --dry-run, --no-sign or --yes")
            if (args.job or args.loc) and (args.notices or cmd_lower in ("notices", "notifications")):
                raise Stop("--job and --loc filter --sb/--be listings, not notices")

            # Only `check [TARGET]` and `notices` take words. Anything else left over is a typo (e.g. `--notices 2`),
            # not something to ignore; a folder beside --notices is the one tolerated legacy form.
            if is_check_cmd:
                leftover = []
            elif cmd_lower in ("notices", "notifications"):
                leftover = [second] if second else []
            else:
                leftover = [w for w in (first, second) if w and not (w == first and first_is_folder)]
            if leftover:
                raise Stop(f"unexpected argument: {leftover[0]!r}")

            target = "notifications"
            sub_lower = second.lower()
            if is_check_cmd:
                if sub_lower in ("sb", "shipping-bill", "shipping-bills"):
                    target = "sb"
                elif sub_lower in ("be", "bill-of-entry", "bills-of-entry"):
                    target = "be"
                elif sub_lower in ("notices", "notifications"):
                    target = "notifications"
                elif not sub_lower:
                    if args.sb and args.be:
                        raise Stop("cannot specify both --sb and --be")
                    target = "sb" if args.sb else ("be" if args.be else "notifications")
                else:
                    raise Stop(f"unknown check target: {second!r}; expected 'notices', 'sb', or 'be'")
            elif is_notices_explicit:
                if args.sb and args.be:
                    raise Stop("cannot specify both --sb and --be")
                target = "notifications"
            elif is_portal_shortcut:
                if args.sb and args.be:
                    raise Stop("cannot specify both --sb and --be")
                target = "sb" if args.sb else "be"

            days = parse_days_value(args.days, default=1)

            if target == "notifications":
                return show_notifications(
                    run=client_runner(),
                    say=say,
                    days=days,
                    table_name=args.table,
                    sb=args.sb,
                    be=args.be,
                    raw=args.raw,
                    as_json=args.json,
                )
            else:
                local = bool(args.job or args.loc)
                argv = ["ecopy", target]
                argv.extend(["--days", str(days)])
                if args.json or local:
                    argv.append("--json")
                out = client_runner()(argv)
                if local and not out.returncode:
                    try:
                        rows = filter_rows(json.loads(out.stdout), job=args.job, loc=args.loc)
                    except ValueError:
                        raise Stop("the portal search returned invalid JSON") from None
                    say(json.dumps(rows, indent=2, ensure_ascii=False) if args.json else listing_table(target.upper(), rows))
                    return 0
                if out.stdout:
                    say(out.stdout.rstrip())
                if out.stderr:
                    print(out.stderr.rstrip(), file=sys.stderr, flush=True)
                return out.returncode

        # 2. HISTORY ROUTE
        is_history = (cmd_lower == "history") or args.history or args.select or args.all or args.recheck
        if is_history:
            if args.dry_run or args.no_sign or args.yes:
                raise Stop("history commands cannot be combined with --dry-run, --no-sign or --yes")
            folder_str = second if cmd_lower == "history" else first
            if not folder_str:
                raise Stop("history requires a folder path (e.g. icerun history /path/to/folder)")
            folder = Path(folder_str).expanduser().resolve()
            if not folder.is_dir():
                raise Stop("folder not found")

            if args.select:
                if not sys.stdin.isatty():
                    raise Stop("--select/--ls needs an interactive terminal; use without --select to show all")
                chosen = Prompts().choose_run(completed_runs(folder))
                if chosen == "ALL":
                    return recheck_all(completed_runs(folder), say=say, table_name=args.table)
                folder = chosen
                return recheck(folder, run=client_runner(), say=say,
                               wait=args.wait if args.wait is not None else 0, table_name=args.table)

            is_direct_run = not (folder / "Done").is_dir() and (
                any(folder.glob("draft-*.html")) or bool(re.fullmatch(r"\d{8}-\d{6}-\d{6}", folder.name))
            )
            if is_direct_run or args.recheck:
                return recheck(folder, run=client_runner(), say=say,
                               wait=args.wait if args.wait is not None else 0, table_name=args.table)

            return recheck_all(completed_runs(folder), say=say, table_name=args.table)

        # 3. FILING / UPLOAD ROUTE
        folder_str = second if cmd_lower == "file" else first
        if not folder_str:
            parser.print_help()
            return 1
        if cmd_lower != "file" and second:
            raise Stop(f"unexpected argument: {second!r}")
        if (args.sb or args.be or args.notices or args.days is not None or args.raw
                or args.job or args.loc):
            raise Stop("check options (--sb, --be, --notices, --days, --raw, --job, --loc) "
                       "cannot be used when filing a folder")
        if args.wait is not None and (not math.isfinite(args.wait) or args.wait < 0):
            raise Stop("--wait must be a finite number of seconds, zero or greater")
        folder = Path(folder_str).expanduser().resolve()
        if not folder.is_dir():
            raise Stop("folder not found")
        if args.table != "all":
            raise Stop("--table requires check or history")
        signer = Path(args.signer).expanduser()
        report: dict | None = {"runs": []} if args.json else None  # --json: narration to stderr, one JSON line to stdout
        try:
            code = run_folder(folder, run=client_runner(),
                              say=(lambda text: print(text, file=sys.stderr, flush=True)) if args.json else say,
                              ui=Prompts() if sys.stdin.isatty() and not args.json else None,
                              dry_run=args.dry_run, yes=args.yes,
                              signer=None if args.no_sign or not signer.is_file() else signer,
                              wait=args.wait if args.wait is not None else 600,
                              doc_type=args.doc_type, doc_type_map=args.doc_type_map, report=report)
        except Stop as exc:
            if report is not None:  # what was already uploaded still matters to the caller
                print(json.dumps({"ok": False, "error": str(exc), "runs": report["runs"]}, ensure_ascii=False), flush=True)
            raise
        if report is not None:
            print(json.dumps({"ok": code == 0, "runs": report["runs"]}, ensure_ascii=False), flush=True)
        return code
    except Stop as exc:
        print(f"stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; nothing further was done", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
