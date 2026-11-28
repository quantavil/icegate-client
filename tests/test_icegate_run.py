import importlib.machinery
import importlib.util
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from icegate_client.cli import build_parser

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "icegate-run.py"
loader = importlib.machinery.SourceFileLoader("icegate_run", str(SCRIPT))
mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("icegate_run", loader))
sys.modules["icegate_run"] = mod
loader.exec_module(mod)

RECEIPT = {"outcome": "submitted", "files": [{"name": "A&B<x>.pdf", "code": "331000"}],
           "rows": [{"file": "a&b<x>.PDF", "drn": "2026103000018356", "irn": "2026103000058483"}]}
NAMES = {"331000": "Commercial invoice which includes a packing list"}
SB_ROW = {"jobNumber": "893", "location": "INMBD6", "docNumber": "8176055", "docDate": "2026-10-28T00:00:00.000Z"}

BE_FILE = "HREC\x1dZZ\x1dINHDD6\r\n<TABLE>BE\r\nF\x1dINHDD6\x1d113\x1d20261105\x1d\x1d\x1dH\x1d\r\n<END-BE>\r\n"
BE_ROW = {"jobNumber": "113", "jobDate": "05-11-2026", "location": "INHDD6", "docNumber": "4941305", "docDate": "-"}
NOW = datetime.now(ZoneInfo("Asia/Kolkata"))
CREATED = NOW.strftime("%d.%m.%Y")  # notices carry a day, in this format
# A rejection with error codes ("failed") and a bare rejection ("not_filed"), as parse_notification gives them.
REJECTED = {"outcome": "rejected", "doc": "BE", "job": "113", "date": "2026-11-05", "location": None, "created": CREATED,
            "detail": "error code 820_SUPPDOCS", "problem": "failed", "errors": ["820_SUPPDOCS"],
            "message": "Your bill of entry filing with job no. 113 dated 05112026 has been rejected. Error code: 820_SUPPDOCS"}
NOT_FILED = {**REJECTED, "detail": "", "problem": "not_filed", "errors": [],
             "message": "Your bill of entry filing with job no. 113 dated 05112026 has been rejected."}
ACCEPTED = {"outcome": "submitted", "doc": "BE", "job": "113", "date": "2026-11-05", "location": None, "created": CREATED,
            "detail": "", "problem": None, "errors": [], "message": "Your bill of entry filing with job no 113 dated 20261105 has been successfully submitted."}

SB_FILE = "HREC\x1dZZ\x1dINMBD6\r\n<TABLE>SB\r\nF\x1dinmbd6\x1d893\x1d20261030\x1d\r\n<END-SB>\r\n"


sleeps: list[float] = []


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    clock = [0.0]  # waiting costs no real time: a sleep only moves this clock
    def sleep(seconds):
        clock[0] += seconds
        sleeps.append(seconds)
    sleeps.clear()
    monkeypatch.setattr(mod, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(mod, "STATE", tmp_path / "state")
    monkeypatch.setattr(mod, "copy_html", lambda text: True)
    monkeypatch.setattr(mod, "prepare_pdf", lambda path, say: None)  # the fixtures are not real PDFs


class FakeRun:
    """Stands in for the client; every argv must parse with the client's real parser."""

    def __init__(self, tmp_path, fail=None, fail_filing=False, rows=None, before=(), after=(), unreadable=0,
                 filing_exit=1, job_status=None):
        self.job_status = job_status  # {"SB:LOC": rows | None}; a requested location missing here has no rows
        self.calls, self.fail, self.fail_filing, self.rows, self.tmp = [], fail, fail_filing, rows, tmp_path
        self.filing_exit = filing_exit
        self.before, self.after, self.polled, self.unreadable = list(before), list(after), 0, unreadable
        self.lookups: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        build_parser().parse_args(argv)
        ok = lambda out="", code=0, err="": SimpleNamespace(returncode=code, stdout=out, stderr=err)  # noqa: E731
        if argv[:2] == ["esanchit", "submit"]:
            if "--confirm" not in argv:
                return ok("challenge   SUBMIT 2 0123456789ab\n")
            receipt = self.tmp / "receipt.json"
            receipt.write_text(json.dumps(RECEIPT))
            return ok(f"submitted\nreceipt     {receipt}\n", self.fail or 0)
        if argv[:2] == ["filing", "upload"]:
            if "--confirm" not in argv:
                return ok("challenge   FILE 1 abcdefabcdef\n")
            return ok("", self.filing_exit, "error: portal said no\n") if self.fail_filing else ok("accepted\n")
        if argv[:2] == ["ecopy", "lookup"]:  # ONE client call: the searches and the notices
            self.lookups.append(argv)
            kinds = [argv[i + 1].split("=")[0] for i, a in enumerate(argv) if a == "--window"]
            rows = self.rows if self.rows is not None else [SB_ROW]
            out = {"today": NOW.date().isoformat(), "ecopy": {kind: rows for kind in kinds}, "events": None, "events_error": None}
            if "--events" in argv:
                self.polled += 1  # with the default wait the first one is the baseline taken before uploading
                if self.polled <= self.unreadable:
                    out["events_error"] = "boom"
                else:
                    out["events"] = self.before if self.polled == 1 else self.after + self.before
            wanted = [argv[i + 1] for i, a in enumerate(argv) if a == "--job-status"]
            if wanted:
                out["job_status"] = {key: (self.job_status or {}).get(key, []) for key in wanted}
            return ok(json.dumps(out))
        return ok(json.dumps([]))


class FakeUI:
    """Stands in for the terminal prompts; records what was shown."""

    def __init__(self, code="331000", confirm=True, pick=None):
        self.code, self.yes, self.pick, self.codes, self.defaults, self.confirms = code, confirm, pick, [], [], []
        self.summaries, self.pauses = [], []

    def select_files(self, names):
        return [n for n in names if self.pick is None or n in self.pick]

    def choose_code(self, name, options, known, default):
        self.codes.append(name)
        self.defaults.append(default)
        return self.code

    def confirm(self, text):
        self.confirms.append(text)
        return self.yes

    def summary(self, rows):
        self.summaries.append(rows)

    def pause(self, message, seconds):
        self.pauses.append(message)
        mod.time.sleep(seconds)


def make(tmp_path, *names, signed=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    """Each name is an original; its signed twin is created as the signer would (spaces -> _)."""
    folder = tmp_path / "in"
    folder.mkdir()
    for name in names:
        stem, dot, ext = name.rpartition(".")
        body = SB_FILE if ext == "sb" else BE_FILE if ext == "be" else name
        (folder / name).write_text(body, newline="")
        if signed:
            (folder / (stem.replace(" ", "_") + "_Signed." + ext)).write_text("signed " + body, newline="")
    return folder


def fake_signer(tmp_path, code=0):
    script = tmp_path / "signer.py"
    script.write_text("import shutil, sys\nfrom pathlib import Path\n"
                      "for f in Path(sys.argv[1]).iterdir():\n"
                      "    if f.is_file() and '_Signed.' not in f.name:\n"
                      "        shutil.copy(f, f.with_name(f.stem.replace(' ', '_') + '_Signed' + f.suffix))\n"
                      f"sys.exit({code})\n")
    return script


def go(folder, tmp_path, run, **kw):
    kw.setdefault("ui", FakeUI())
    kw.setdefault("wait", 600)
    return mod.run_folder(folder, run=run, say=kw.pop("said", lambda _: None), **kw)


def test_full_run(tmp_path):
    folder = make(tmp_path, "Packing list invoice 1.pdf", "inv two.pdf", "893.sb")
    run, ui = FakeRun(tmp_path), FakeUI()
    assert go(folder, tmp_path, run, ui=ui) == 0
    assert sorted(ui.codes) == ["Packing_list_invoice_1_Signed.pdf", "inv_two_Signed.pdf"]   # asked for every PDF
    assert ui.defaults[ui.codes.index("Packing_list_invoice_1_Signed.pdf")] == "331000"      # guess pre-selected
    assert len(ui.confirms) == 1 and "Upload 3 signed" in ui.confirms[0]
    submit = next(c for c in run.calls if c[:2] == ["esanchit", "submit"] and "--confirm" in c)
    assert "--doc-type-map=Packing_list_invoice_1_Signed.pdf=331000" in submit
    assert submit[submit.index("--confirm") + 1] == "SUBMIT 2 0123456789ab"
    # Done/ is the only duplicate guard; the client's state-dir receipts must not block a confirmed upload.
    assert all("--allow-duplicate" in c for c in run.calls if "--confirm" in c and c[0] in ("filing", "esanchit"))
    assert not any("--allow-duplicate" in c for c in run.calls if "--dry-run" in c)
    done = next((folder / "Done").iterdir())
    assert sorted(p.name for p in done.iterdir() if p.name != ".uploaded.json") == sorted([
        "893.sb", "893_Signed.sb", "Packing list invoice 1.pdf", "Packing_list_invoice_1_Signed.pdf",
        "inv two.pdf", "inv_two_Signed.pdf", "draft-esanchit.html", "draft-sb.html"])
    assert "A&amp;B&lt;x&gt;.pdf" not in (done / "draft-esanchit.html").read_text()  # receipt row name is lower-case
    assert [p.name for p in folder.iterdir()] == ["Done"]


def test_second_run_skips_already_uploaded_signed_names(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf")
    go(folder, tmp_path, FakeRun(tmp_path))
    done = next((folder / "Done").iterdir())
    (folder / "Commercial_Invoice_1_Signed.pdf").write_text("signed again, different bytes")
    said = []
    with pytest.raises(mod.Stop, match="no new"):
        go(folder, tmp_path, FakeRun(tmp_path), said=said.append)
    assert any("already uploaded" in s for s in said) and done.exists()


def test_unknown_outcome_stops_and_says_do_not_rerun(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf")
    with pytest.raises(mod.Stop, match="do NOT re-run"):
        go(folder, tmp_path, FakeRun(tmp_path, fail=3))


@pytest.mark.parametrize("kind,exit_code", [("esanchit", 3), ("filing", 3), ("filing", 2), ("esanchit", 130)])
def test_unknown_outcome_is_remembered_in_done_and_blocks_a_rerun(tmp_path, kind, exit_code):
    name = "Commercial Invoice 1.pdf" if kind == "esanchit" else "893.sb"
    folder = make(tmp_path, name)
    run = FakeRun(tmp_path, fail=exit_code) if kind == "esanchit" else FakeRun(tmp_path, fail_filing=True, filing_exit=exit_code)
    with pytest.raises(mod.Stop, match="marked in"):
        go(folder, tmp_path, run)
    signed = next(p for p in folder.iterdir() if p.name.endswith(("_Signed.pdf", "_Signed.sb")))
    (record,) = (folder / "Done").iterdir()
    entry = json.loads((record / ".uploaded.json").read_text())[signed.name.casefold()]
    assert entry["status"] == "unknown" and signed.exists()                  # the file stays where it is
    said, again = [], FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="no new"):
        go(folder, tmp_path, again, said=said.append)
    assert any("outcome unknown" in s and record.name in s for s in said)
    assert not any("--confirm" in c for c in again.calls)                    # nothing was sent
    import shutil
    shutil.rmtree(folder / "Done")                                           # deleting Done wipes the memory
    assert go(folder, tmp_path, FakeRun(tmp_path)) == 0


def test_refusal_before_dispatch_is_not_remembered(tmp_path):
    folder = make(tmp_path, "893.sb")
    with pytest.raises(mod.Stop, match="upload stopped"):
        go(folder, tmp_path, FakeRun(tmp_path, fail_filing=True, filing_exit=1))
    assert not (folder / "Done").exists()
    assert go(folder, tmp_path, FakeRun(tmp_path)) == 0                      # no false positive: it can be retried


def test_unknown_filing_after_good_esanchit_keeps_both_records_apart(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "893.sb")
    with pytest.raises(mod.Stop, match="Already submitted in this run: eSANCHIT"):
        go(folder, tmp_path, FakeRun(tmp_path, fail_filing=True, filing_exit=3))
    (record,) = (folder / "Done").iterdir()
    entries = json.loads((record / ".uploaded.json").read_text())
    assert entries["commercial_invoice_1_signed.pdf"].get("status") is None
    assert entries["893_signed.sb"]["status"] == "unknown"
    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == ["893.sb", "893_Signed.sb"]


def test_pending_marker_lives_in_done_and_is_gone_after_upload(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=FakeUI(confirm=False))
    assert (folder / "Done" / ".pending.json").exists()
    assert not (tmp_path / "state").exists()                                 # nothing outside the folder
    assert go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path)) == 0
    assert not (folder / "Done" / ".pending.json").exists()
    assert [p.is_dir() for p in (folder / "Done").iterdir()] == [True]


def test_deleting_done_also_forgets_the_pending_marker(tmp_path):
    import shutil
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=FakeUI(confirm=False))
    shutil.rmtree(folder / "Done")
    (folder / "Commercial_Invoice_1_Signed.pdf").write_text("signed by hand, different bytes")
    run = FakeRun(tmp_path)
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path)) == 0      # no stale "changed since signing"
    assert any("--confirm" in call for call in run.calls)


def test_filing_failure_after_esanchit_success_keeps_state_straight(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "893.sb")
    said = []
    with pytest.raises(mod.Stop, match=r"Already submitted in this run: eSANCHIT") as err:
        go(folder, tmp_path, FakeRun(tmp_path, fail_filing=True), said=said.append)
    assert "portal said no" in "\n".join(said)           # stderr is shown
    done = next((folder / "Done").iterdir())
    assert (done / "draft-esanchit.html").exists()
    assert sorted(p.name for p in folder.iterdir() if p.is_file()) == ["893.sb", "893_Signed.sb"]
    assert not (folder / "Commercial_Invoice_1_Signed.pdf").exists()
    assert "stopped" in str(err.value) or "upload stopped" in str(err.value)


def test_declining_uploads_nothing_and_changes_nothing(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf")
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, run, ui=FakeUI(confirm=False))
    assert sorted(p.name for p in folder.iterdir()) == ["Commercial Invoice 1.pdf", "Commercial_Invoice_1_Signed.pdf"]
    assert not any("--confirm" in c for c in run.calls)


def test_no_terminal_needs_yes(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf")
    with pytest.raises(mod.Stop, match="--yes"):
        go(folder, tmp_path, FakeRun(tmp_path), ui=None)
    assert len(list(folder.iterdir())) == 2
    # no terminal and a name that does not pin one code: the default code is used, never a stop
    assert go(make(tmp_path / "x", "INV 1.pdf"), tmp_path, FakeRun(tmp_path), ui=None, yes=True) == 0
    folder2 = make(tmp_path / "y", "Commercial Invoice 2.pdf")
    assert go(folder2, tmp_path, FakeRun(tmp_path), ui=None, yes=True) == 0


def test_unsigned_files_are_never_uploaded(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "Commercial Invoice 1.pdf").write_text("x")
    said = []
    with pytest.raises(mod.Stop, match="no new signed"):
        go(folder, tmp_path, FakeRun(tmp_path), said=said.append)
    assert any("not signed" in line for line in said)


def test_sb_not_registered_yet_is_a_note_not_a_failure(tmp_path):
    folder = make(tmp_path, "893.sb")
    said = []
    assert go(folder, tmp_path, FakeRun(tmp_path, rows=[]), said=said.append) == 0
    assert any("is registered yet" in s for s in said)
    assert not any(p.name.startswith("draft-") for p in next((folder / "Done").iterdir()).iterdir())


def test_pdfs_only_never_ask_for_a_location(tmp_path):
    ui = FakeUI()
    go(make(tmp_path, "Commercial Invoice 1.pdf"), tmp_path, FakeRun(tmp_path), ui=ui)
    assert len(ui.summaries) == 1


@pytest.mark.parametrize("name,codes,sure", [
    ("Invoice - Packing list 02 PLT 27E3100129", ["331000"], True),   # the one entry holding both
    ("Packing list", ["271000"], True),
    ("Commercial Invoice 5", ["380000"], True),
    ("Customs invoice 3", ["935000"], True),
    ("INV&PL 9230023771", ["331000"], True),                          # INV + PL spell out invoice + packing list
    ("inv pl kps", ["331000"], True),
    ("PL 5", ["271000"], True),
    ("INV & PACK CUSTOM KB078", ["331000"], True),                  # the file that was just uploaded
    ("PARAMOUNT 114", [], False),                                    # client name: no evidence
    ("KPS AGRO FOOD", [], False),
])
def test_guess_from_catalogue(name, codes, sure):
    assert mod.candidates(name, mod.catalog()) == (codes, sure)


@pytest.mark.parametrize("name", ["AUS 893", "Bond Exim 893", "Basmati 893", "Water 893", "Container list",
                                  "Phyto", "Gas", "INV#9230023770", "Annexure 893"])
def test_leading_word_only_suggests_never_decides(name):
    assert mod.candidates(name, mod.catalog())[1] is False


def test_codes_are_chosen_per_file_with_the_last_answer_as_default(tmp_path):
    items = [mod.Item(tmp_path / f"INV#{n}_Signed.pdf") for n in (1, 2)]
    ui = FakeUI("380000")
    mod.assign_codes(items, ui, mod.catalog())
    assert len(ui.codes) == 2 and [i.code for i in items] == ["380000", "380000"]
    assert ui.defaults[1] == "380000"                           # second INV offers the first answer
    odd = mod.Item(tmp_path / "PARAMOUNT_114_Signed.pdf")
    with pytest.raises(mod.Stop, match="unknown"):
        mod.assign_codes([odd], FakeUI("ZZZ"), mod.catalog())
    mod.assign_codes([odd], None, mod.catalog())              # no terminal, name says nothing: default code
    assert odd.code == mod.DEFAULT_CODE == "331000"
    seen = FakeUI("331000")
    mod.assign_codes([mod.Item(tmp_path / "PARAMOUNT_115_Signed.pdf")], seen, mod.catalog())
    assert seen.defaults[0] == "331000"                       # at a terminal it is pre-selected


def test_doc_type_map_override(tmp_path):
    folder = make(tmp_path, "CustomDoc.pdf", signed=False)
    run, said = FakeRun(tmp_path), []
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path), said=said.append,
              doc_type_map=["CustomDoc.pdf=022CO1"], yes=True) == 0
    assert any("--doc-type-map=CustomDoc_Signed.pdf=022CO1" in part for call in run.calls for part in call)


def test_unsigned_originals_are_signed_first_then_uploaded(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "Customs invoice 2.pdf", signed=False)
    run, said = FakeRun(tmp_path), []
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path), said=said.append) == 0
    assert any("signing with" in s for s in said)
    assert sorted(p.name for p in next((folder / "Done").iterdir()).iterdir() if p.name != ".uploaded.json") == sorted([
        "Commercial Invoice 1.pdf", "Commercial_Invoice_1_Signed.pdf",
        "Customs invoice 2.pdf", "Customs_invoice_2_Signed.pdf", "draft-esanchit.html"])


def test_signer_failure_stops_before_any_upload(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="signing failed"):
        go(folder, tmp_path, run, signer=fake_signer(tmp_path, code=7))
    assert run.calls == []


def test_only_ticked_files_go(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "Customs invoice 2.pdf")
    run = FakeRun(tmp_path)
    assert go(folder, tmp_path, run, ui=FakeUI(pick={"Customs_invoice_2_Signed.pdf"})) == 0
    submit = next(c for c in run.calls if c[:2] == ["esanchit", "submit"] and "--confirm" in c)
    assert [a for a in submit if a.startswith("--doc-type-map")] == ["--doc-type-map=Customs_invoice_2_Signed.pdf=331000"]
    assert (folder / "Commercial_Invoice_1_Signed.pdf").exists()    # unticked file stays put


def test_dry_run_prompts_for_nothing(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "INV 2.pdf")
    ui, said = FakeUI(), []
    assert go(folder, tmp_path, FakeRun(tmp_path), ui=ui, dry_run=True, said=said.append) == 0
    assert not ui.codes and not ui.confirms and any("you will choose" in s for s in said)


def test_dry_run_previews_unsigned_files_without_signing(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "EXP-2627-762.sb", signed=False)
    said, run = [], FakeRun(tmp_path)
    assert go(folder, tmp_path, run, dry_run=True, said=said.append) == 0
    output = "\n".join(said)
    assert "Commercial Invoice 1.pdf" in output and "EXP-2627-762.sb" in output
    assert "331000" not in output and "380000" in output
    assert "nothing signed or uploaded" in output and not run.calls
    assert not list(folder.glob("*_Signed.*"))


def test_sb_job_is_read_from_file_when_filename_is_not_job_number(tmp_path):
    folder = make(tmp_path, "EXP-2627-762.sb")
    run, ui = LateSB(tmp_path), FakeUI()
    assert go(folder, tmp_path, run, ui=ui, wait=600) == 0
    assert run.searches == 3
    assert (next((folder / "Done").iterdir()) / "draft-sb.html").exists()


def test_match_rows_wants_a_registered_number_and_date(tmp_path):
    match = lambda rows, **kw: mod.match_rows(rows, kw.get("kind", "SB"), "INMBD6", "893", "2026-10-30", date(2026, 11, 2))  # noqa: E731
    assert match([SB_ROW]) == [SB_ROW]
    assert match([{**SB_ROW, "docNumber": ""}]) == []
    for dash in ({"docDate": "-"}, {"docDate": ""}, {"docNumber": "-"}):
        assert match([{**SB_ROW, **dash}]) == []


def test_match_rows_rejects_a_job_dated_before_the_file():
    assert mod.match_rows([{**SB_ROW, "jobDate": "29-10-2026"}], "SB", "INMBD6", "893", "2026-10-30", date(2026, 11, 2)) == []


def test_match_rows_finds_a_job_the_portal_dated_after_the_file():
    """File says 2026-11-13; ICEGATE processed it on the 14th and dates the e-copy job that day."""
    row = {**SB_ROW, "jobNumber": "788", "jobDate": "14-11-2026", "docDate": "14-11-2026"}
    assert mod.match_rows([row, {**row, "jobNumber": "789"}], "SB", "INMBD6", "788", date(2026, 11, 13), date(2026, 11, 16)) == [row]


def test_match_rows_ignores_other_locations_and_future_dates():
    other = {**SB_ROW, "location": "INSTT6"}
    later = {**SB_ROW, "jobDate": "20-11-2026"}
    assert mod.match_rows([other, later], "SB", "INMBD6", "893", "2026-11-10", date(2026, 11, 16)) == []


def test_match_rows_accepts_a_be_with_a_dash_date_and_a_later_job_date():
    row = {**BE_ROW, "jobDate": "09-11-2026"}
    assert mod.match_rows([row], "BE", "INHDD6", "113", "2026-11-05", date(2026, 11, 16)) == [row]
    assert mod.match_rows([row], "SB", "INHDD6", "113", "2026-11-05", date(2026, 11, 16)) == []  # an SB needs a real date


def test_match_rows_two_matches_are_both_returned_and_the_plain_number_beats_the_suffixed():
    twin = {**SB_ROW, "docNumber": "8176056"}
    assert mod.match_rows([SB_ROW, twin], "SB", "INMBD6", "893", "2026-10-28", date(2026, 10, 28)) == [SB_ROW, twin]
    suffixed = {**SB_ROW, "jobNumber": "89326"}
    assert mod.match_rows([suffixed], "SB", "INMBD6", "893", "2026-10-28", date(2026, 10, 28)) == [suffixed]
    assert mod.match_rows([suffixed, SB_ROW], "SB", "INMBD6", "893", "2026-10-28", date(2026, 10, 28)) == [SB_ROW]


def test_recheck_recovers_sb_table_from_archived_file_without_upload(tmp_path, monkeypatch):
    folder = make(tmp_path, "EXP-2627-762.sb")
    run = FakeRun(tmp_path)
    go(folder, tmp_path, run)
    archive = next((folder / "Done").iterdir())
    (archive / "draft-sb.html").unlink()
    copied, said = [], []
    monkeypatch.setattr(mod, "copy_html", lambda body: copied.append(body) or True)
    run.calls.clear()
    assert mod.recheck(archive, run=run, say=said.append, wait=0) == 0
    assert "8176055" in "\n".join(said) and "8176055" in copied[0]
    assert (archive / "draft-sb.html").exists()
    assert [c[:2] for c in run.calls] == [["ecopy", "lookup"]]  # one client call, not a search plus notices


def test_recheck_can_copy_either_saved_table(tmp_path, monkeypatch):
    archive = tmp_path / "Done" / "20261106-210750-330102"
    archive.mkdir(parents=True)
    (archive / "draft-esanchit.html").write_text(mod.esanchit_html(RECEIPT, NAMES))
    (archive / "draft-sb.html").write_text(mod.sb_html([SB_ROW]))
    copied, said = [], []
    monkeypatch.setattr(mod, "copy_html", lambda body: copied.append(body) or True)
    run = FakeRun(tmp_path)
    assert mod.recheck(archive, run=run, say=said.append, wait=0, table_name="esanchit") == 0
    assert "2026103000058483" in "\n".join(said) and len(copied) == 1
    assert not run.calls


def test_recheck_requires_specific_completed_run(tmp_path):
    archive = tmp_path / "Done" / "20261106-210750-330102"
    archive.mkdir(parents=True)
    (archive / "draft-esanchit.html").write_text(mod.esanchit_html(RECEIPT, NAMES))
    with pytest.raises(mod.Stop, match="completed run"):
        mod.recheck(tmp_path, run=FakeRun(tmp_path), say=lambda _: None)


def test_ls_picks_completed_run_and_only_rechecks_it(tmp_path, monkeypatch):
    older = tmp_path / "Done" / "20261106-210414-972337"
    newer = tmp_path / "Done" / "20261106-210750-330102"
    for path in (older, newer):
        path.mkdir(parents=True)
        (path / "draft-sb.html").write_text(mod.sb_html([SB_ROW]))
    (tmp_path / "Done" / "unrelated").mkdir()
    class Picker:
        def choose_run(self, runs):
            assert runs == [newer, older]
            return older
    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(mod, "Prompts", Picker)
    observed = []
    monkeypatch.setattr(mod, "recheck", lambda path, **kw: observed.append(path) or 0)
    assert mod.main([str(tmp_path), "--ls"]) == 0
    assert observed == [older]


def test_ls_accepts_done_directory_and_handles_empty_history(tmp_path):
    done = tmp_path / "Done"
    done.mkdir()
    with pytest.raises(mod.Stop, match="completed runs"):
        mod.completed_runs(done)
    archive = done / "20261106-210750-330102"
    archive.mkdir()
    (archive / "sample_Signed.sb").write_text(SB_FILE)
    assert mod.completed_runs(done) == [archive]


def test_recheck_rejects_dry_run_option(tmp_path, capsys):
    assert mod.main([str(tmp_path), "--recheck", "--dry-run"]) != 0
    assert "--dry-run" in capsys.readouterr().err


def test_failed_draft_write_does_not_crash_after_upload(tmp_path, monkeypatch):
    folder = make(tmp_path, "Commercial Invoice 1.pdf")
    write = Path.write_text
    def fail_draft(path, *args, **kwargs):
        if path.name == "draft-esanchit.html":
            raise OSError("disk full")
        return write(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", fail_draft)
    said = []
    assert go(folder, tmp_path, FakeRun(tmp_path), said=said.append) == 0
    assert any("disk full" in message for message in said)


def test_recheck_honors_explicit_wait(tmp_path, monkeypatch):
    observed = []
    monkeypatch.setattr(mod, "recheck", lambda folder, **kw: observed.append(kw["wait"]) or 0)
    assert mod.main([str(tmp_path), "--recheck", "--wait", "45"]) == 0
    assert observed == [45]


@pytest.mark.parametrize("wait", ["-1", "nan", "inf"])
def test_invalid_wait_stops_before_processing(tmp_path, wait, capsys):
    assert mod.main([str(tmp_path), "--wait", wait]) == 1
    assert "--wait" in capsys.readouterr().err


def test_icerun_check_command(tmp_path, monkeypatch, capsys):
    calls = []
    def fake_client_runner():
        def run(argv):
            calls.append(argv)
            return type("Res", (), {"returncode": 0, "stdout": "NOTICES_TABLE\n", "stderr": ""})()
        return run
    monkeypatch.setattr(mod, "client_runner", fake_client_runner)

    # 1. icerun check (defaults to notifications 1 day)
    res = mod.main(["check"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "1", "--events"]
    assert "NOTICES_TABLE" in capsys.readouterr().out

    # 2. icerun check --days 2
    res = mod.main(["check", "--days", "2"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "2", "--events"]

    # 3. icerun check notices --days 2
    res = mod.main(["check", "notices", "--days", "2"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "2", "--events"]

    # 4. icerun check sb (defaults to today)
    res = mod.main(["check", "sb"])
    assert res == 0
    assert calls[-1] == ["ecopy", "sb", "--days", "1"]

    # 5. icerun check sb --days 7
    res = mod.main(["check", "sb", "--days", "7"])
    assert res == 0
    assert calls[-1] == ["ecopy", "sb", "--days", "7"]

    # 6. icerun check be --days 30
    res = mod.main(["check", "be", "--days", "30"])
    assert res == 0
    assert calls[-1] == ["ecopy", "be", "--days", "30"]

    # 7. icerun --notices (defaults to 1 day)
    res = mod.main(["--notices"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "1", "--events"]

    # 8. icerun --notices --table sb --days 2
    res = mod.main(["--notices", "--table", "sb", "--days", "2"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "2", "--events", "--doc", "SB"]

    # 9. icerun FOLDER --notices --sb (defaults to 1 day)
    res = mod.main([str(tmp_path), "--notices", "--sb"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "1", "--events", "--doc", "SB"]

    # 10. icerun --sb --days 7
    res = mod.main(["--sb", "--days", "7"])
    assert res == 0
    assert calls[-1] == ["ecopy", "sb", "--days", "7"]

    # 11. icerun check notices --sb (defaults to 1 day)
    res = mod.main(["check", "notices", "--sb"])
    assert res == 0
    assert calls[-1] == ["notifications", "--days", "1", "--events", "--doc", "SB"]

    # 12. --notification and --notifications are removed (unrecognized arguments)
    with pytest.raises(SystemExit):
        mod.main(["--notification"])
    with pytest.raises(SystemExit):
        mod.main(["--notifications"])

    # 13. Conflicting --sb and --be (GOLDFISH-6 fix)
    res = mod.main(["check", "--sb", "--be"])
    assert res == 1
    assert "cannot specify both --sb and --be" in capsys.readouterr().err

    # 14. Safety: check flags with folder must NOT trigger upload (GOLDFISH-1 fix)
    folder = tmp_path / "job_folder"
    folder.mkdir()
    res = mod.main([str(folder), "--sb"])
    assert res == 1
    assert "check options" in capsys.readouterr().err

    # 15. Safety: unexpected positional argument with folder (GOLDFISH-1 fix)
    res = mod.main([str(folder), "typo_arg"])
    assert res == 1
    assert "unexpected argument: 'typo_arg'" in capsys.readouterr().err


def test_icerun_days_flag_and_stray_words(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_client_runner():
        def run(argv):
            calls.append(argv)
            return type("Res", (), {"returncode": 0, "stdout": "OUT\n", "stderr": ""})()
        return run
    monkeypatch.setattr(mod, "client_runner", fake_client_runner)

    assert mod.main(["--notices", "--days", "2"]) == 0
    assert calls[-1] == ["notifications", "--days", "2", "--events"]
    assert mod.main(["--notices"]) == 0
    assert calls[-1] == ["notifications", "--days", "1", "--events"]
    assert mod.main(["--sb", "--days", "3"]) == 0
    assert calls[-1] == ["ecopy", "sb", "--days", "3"]
    assert mod.main(["check", "be", "--days", "2"]) == 0
    assert calls[-1] == ["ecopy", "be", "--days", "2"]
    assert mod.main(["notices", "--days", "2"]) == 0
    assert calls[-1] == ["notifications", "--days", "2", "--events"]

    n = len(calls)
    # --day and the --today/--week/--month presets are gone; no flag may be abbreviated (--not is not --notices)
    for bad in (["--notices", "--day", "2"], ["--notices", "--day"], ["--not"], ["--sb", "--da", "2"],
                ["--notices", "--today"], ["--sb", "--week"], ["--be", "--month"]):
        with pytest.raises(SystemExit) as exc:
            mod.main(bad)
        assert exc.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err
    # --days takes a positive whole number only
    for bad in ("week", "today", "0", "-1", "1.5"):
        assert mod.main(["--notices", f"--days={bad}"]) == 1
        assert "--days" in capsys.readouterr().err
    # a stray word beside a check route is refused, never ignored
    for bad, word in ((["--notices", "--days", "1", "2"], "'2'"), (["--sb", "--days", "1", "2"], "'2'"),
                      (["--be", "--days", "2", "x"], "'x'"), (["notices", "2"], "'2'"), (["--notices", "a", "b"], "'a'")):
        assert mod.main(bad) == 1
        assert f"unexpected argument: {word}" in capsys.readouterr().err
    assert len(calls) == n  # nothing reached the client


def test_icerun_history_combines_multiple_runs(tmp_path, capsys):
    done = tmp_path / "Done"
    run1 = done / "20261106-210414-972337"
    run2 = done / "20261106-210750-330102"
    for r, job, num in ((run1, "101", "80001"), (run2, "102", "80002")):
        r.mkdir(parents=True)
        html_content = mod.sb_html([{"jobNumber": job, "docNumber": num, "docDate": "2026-11-23T00:00:00.000Z"}])
        (r / "draft-sb.html").write_text(html_content)

    # 1. icerun history FOLDER
    res = mod.main(["history", str(tmp_path)])
    assert res == 0
    out = capsys.readouterr().out
    assert "101" in out and "80001" in out
    assert "102" in out and "80002" in out
    assert "2 row(s)" in out

    # 2. icerun FOLDER --history
    res = mod.main([str(tmp_path), "--history"])
    assert res == 0
    out = capsys.readouterr().out
    assert "101" in out and "80001" in out
    assert "2 row(s)" in out

    # 3. Legacy: upload FOLDER --all
    res = mod.main([str(tmp_path), "--all"])
    assert res == 0
    out = capsys.readouterr().out
    assert "101" in out and "80001" in out



def test_validation_warnings_are_shown_before_upload_confirmation(tmp_path):
    class WarningRun(FakeRun):
        def __call__(self, argv):
            result = super().__call__(argv)
            if "--dry-run" in argv:
                result.stderr = "warning: signer trust is unverified\n"
            return result
    said = []
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(make(tmp_path, "Commercial Invoice.pdf"), tmp_path, WarningRun(tmp_path),
           ui=FakeUI(confirm=False), said=said.append)
    assert any("signer trust is unverified" in message for message in said)


def test_html_dates_and_refusals():
    out = mod.esanchit_html(RECEIPT, NAMES)
    assert "a&amp;b&lt;x&gt;.PDF" in out and "331000-Commercial" in out
    for bad in ({**RECEIPT, "outcome": "unknown"}, {**RECEIPT, "files": [{"name": "A&B<x>.pdf", "code": "NOPE"}]}):
        with pytest.raises(mod.Stop):
            mod.esanchit_html(bad, NAMES)
    assert mod.portal_date("2026-10-27T18:30:00.000Z") == "28-10-2026"
    with pytest.raises(mod.Stop):
        mod.portal_date("2026-10-28T05:00:00.000Z")
    assert mod.sb_html([{**SB_ROW, "jobNumber": "8<b>"}]).count("<table") == 1 and "8&lt;b&gt;" in mod.sb_html([{**SB_ROW, "jobNumber": "8<b>"}])


class LateSB(FakeRun):
    """The SB number appears on the third e-copy search."""

    def __init__(self, tmp_path):
        super().__init__(tmp_path)
        self.searches = 0

    def __call__(self, argv):
        if argv[:2] == ["ecopy", "lookup"] and "--window" in argv:
            self.searches += 1
            self.rows = [SB_ROW] if self.searches >= 3 else []
        return super().__call__(argv)


def test_sb_number_found_under_year_suffixed_job_number(tmp_path):
    """Record job 893 filed as e-copy job 89326 (record number + two-digit year)."""
    row = {**SB_ROW, "jobNumber": "89326", "jobDate": "30-10-2026"}

    class Suffixed(FakeRun):
        def __call__(self, argv):
            if argv[:2] == ["ecopy", "lookup"]:
                self.rows = [row]
            return super().__call__(argv)

    folder = make(tmp_path, "893.sb")
    assert go(folder, tmp_path, Suffixed(tmp_path)) == 0
    assert "89326" in (next((folder / "Done").iterdir()) / "draft-sb.html").read_text()


def test_waits_for_the_sb_number_to_appear(tmp_path):
    folder = make(tmp_path, "893.sb")
    run, ui = LateSB(tmp_path), FakeUI()
    assert go(folder, tmp_path, run, ui=ui, wait=600) == 0
    assert run.searches == 3 and len(ui.pauses) == 2 and all("checking again" in m for m in ui.pauses)
    assert (next((folder / "Done").iterdir()) / "draft-sb.html").exists()


def test_waiting_without_a_terminal_uses_plain_sleep(tmp_path):
    run = LateSB(tmp_path)
    assert go(make(tmp_path, "893.sb"), tmp_path, run, ui=None, yes=True, wait=600) == 0
    assert sleeps == [mod.POLL_SECONDS] * 2


def test_gives_up_waiting_after_the_limit_with_one_lookup_per_round(tmp_path):
    folder = make(tmp_path, "893.sb")
    said, run = [], FakeRun(tmp_path, rows=[])
    assert go(folder, tmp_path, run, wait=150, said=said.append) == 0
    rounds = [c for c in run.lookups if "--window" in c]
    assert len(rounds) == 4 and sleeps == [60, 60, 30]  # one client login per round, covering SB and BE together
    assert any("is registered yet" in s and "icerun --recheck" in s for s in said)


def test_wait_zero_uploads_and_archives_only(tmp_path):
    folder, said = make(tmp_path, "893.sb", "113.be"), []
    run = FakeRun(tmp_path)
    assert go(folder, tmp_path, run, wait=0, ui=None, yes=True, said=said.append) == 0
    assert run.lookups == [["ecopy", "lookup", "--events"]] and sleeps == []  # only the baseline, taken before the upload
    archive = next((folder / "Done").iterdir())
    assert any("--wait 0" in s and f"icerun --lookup {archive}" in s for s in said)
    assert (archive / "893_Signed.sb").exists() and not list(archive.glob("draft-*"))


def test_sb_and_be_are_checked_together_in_one_call_per_round(tmp_path):
    folder = make(tmp_path, "893.sb", "113.be")
    run = FakeRun(tmp_path, rows=[SB_ROW, BE_ROW])
    assert go(folder, tmp_path, run) == 0
    window_calls = [c for c in run.lookups if "--window" in c]
    assert len(window_calls) == 1 and window_calls[0][2:] == ["--window", "BE=2026-11-05", "--window", "SB=2026-10-30", "--events"]
    assert len(run.lookups) == 2  # the baseline (notices only) and the one round


def test_portal_safe_names():
    assert mod.portal_safe("INV_&_PACK_CUSTOM_KB078_Signed.pdf") == "INV_PACK_CUSTOM_KB078_Signed.pdf"
    assert mod.portal_safe("INV#9230023771_Signed.pdf") == "INV#9230023771_Signed.pdf"
    assert mod.signed_name(Path("INV & PACK CUSTOM KB078.pdf")) == "INV_PACK_CUSTOM_KB078_Signed.pdf"


def test_signed_file_with_an_unsafe_name_is_renamed_not_resigned_and_uploaded_under_the_safe_name(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "INV & PACK CUSTOM KB078.pdf").write_text("original")
    (folder / "INV_&_PACK_CUSTOM_KB078_Signed.pdf").write_text("signed by the signer")   # as the signer names it
    run, said = FakeRun(tmp_path), []
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path), said=said.append) == 0
    assert any("renamed INV_&_PACK" in m for m in said) and not any("signing with" in m for m in said)
    submit = next(c for c in run.calls if c[:2] == ["esanchit", "submit"] and "--confirm" in c)
    assert "--doc-type-map=INV_PACK_CUSTOM_KB078_Signed.pdf=331000" in submit
    assert not any("&" in a for a in submit)
    assert sorted(p.name for p in next((folder / "Done").iterdir()).iterdir() if p.name != ".uploaded.json") == sorted([
        "INV & PACK CUSTOM KB078.pdf", "INV_PACK_CUSTOM_KB078_Signed.pdf", "draft-esanchit.html"])


def test_children_do_not_inherit_virtual_env(monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere")
    assert "VIRTUAL_ENV" not in mod.clean_env()


def test_dry_run_reports_a_rename_but_changes_nothing(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "INV & PACK.pdf").write_text("o")
    (folder / "INV_&_PACK_Signed.pdf").write_text("s")
    said = []
    assert go(folder, tmp_path, FakeRun(tmp_path), dry_run=True, said=said.append) == 0
    assert any("would rename" in m for m in said) and not any("would be skipped" in m for m in said)
    assert sorted(p.name for p in folder.iterdir()) == ["INV & PACK.pdf", "INV_&_PACK_Signed.pdf"]


def test_sb_files_must_name_one_location(tmp_path):
    folder = make(tmp_path, "893.sb", "894.sb")
    (folder / "894_Signed.sb").write_text(SB_FILE.replace("inmbd6", "INDEL4"), newline="")
    with pytest.raises(mod.Stop, match="one customs location"):
        go(folder, tmp_path, FakeRun(tmp_path))


def test_sb_without_a_readable_location_stops_before_anything_is_sent(tmp_path):
    folder = make(tmp_path, "893.sb")
    (folder / "893_Signed.sb").write_text("no record here")
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="one customs location"):
        go(folder, tmp_path, run)
    assert run.calls == []


def test_portal_date_keeps_a_ready_made_date():
    assert mod.portal_date("30-10-2026") == "30-10-2026"
    assert mod.portal_date("2026-10-27T18:30:00.000Z") == "28-10-2026"


def real_pdf(pages=1) -> bytes:
    import pymupdf
    with pymupdf.open() as doc:
        for n in range(pages):
            doc.new_page().insert_text((72, 72), f"page {n}")
        return doc.tobytes()


def test_prepare_pdf_flattens_drops_trailing_junk_and_keeps_the_original(tmp_path, monkeypatch):
    import pymupdf
    monkeypatch.undo()
    path = tmp_path / "a.pdf"
    path.write_bytes(original := real_pdf(2) + b"%%EOF <html>junk</html>\r\n")
    said = []
    mod.prepare_pdf(path, said.append)
    with pymupdf.open(path) as doc:
        assert len(doc) == 2 and doc.metadata["producer"] == mod.FLAT_MARK and not doc[0].get_text().strip()
    assert (tmp_path / ".original" / "a.pdf").read_bytes() == original and "flattened" in said[0]
    assert not list(tmp_path.glob(".*.flat"))


def test_prepare_pdf_does_not_flatten_twice(tmp_path, monkeypatch):
    monkeypatch.undo()
    path = tmp_path / "a.pdf"
    path.write_bytes(real_pdf())
    mod.prepare_pdf(path, lambda _: None)
    flat = path.read_bytes()
    mod.prepare_pdf(path, lambda _: pytest.fail("flattened again"))
    assert path.read_bytes() == flat


def test_prepare_pdf_never_overwrites_an_older_backup(tmp_path, monkeypatch):
    monkeypatch.undo()
    (tmp_path / ".original").mkdir()
    (tmp_path / ".original" / "a.pdf").write_bytes(b"older, different file")
    path = tmp_path / "a.pdf"
    path.write_bytes(new := real_pdf())
    mod.prepare_pdf(path, lambda _: None)
    assert (tmp_path / ".original" / "a.pdf").read_bytes() == b"older, different file"
    assert (tmp_path / ".original" / "a.1.pdf").read_bytes() == new


def test_prepare_pdf_refuses_a_non_pdf(tmp_path, monkeypatch):
    monkeypatch.undo()
    path = tmp_path / "a.pdf"
    path.write_bytes(b"not a pdf")
    with pytest.raises(mod.Stop, match="cannot be read"):
        mod.prepare_pdf(path, lambda _: None)


def test_each_table_is_copied_as_soon_as_it_exists(tmp_path, monkeypatch):
    copied = []
    monkeypatch.setattr(mod, "copy_html", lambda text: copied.append(text) or True)
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "893.sb")
    assert go(folder, tmp_path, FakeRun(tmp_path)) == 0
    assert len(copied) == 2
    assert "IRN and DRN" in copied[0] and "Shipping Bill Numbers" in copied[1]


def test_new_original_with_previously_uploaded_name_is_signed(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path))
    (folder / "Commercial Invoice 1.pdf").write_text("a different invoice")
    run = FakeRun(tmp_path)
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path)) == 0
    assert any("--confirm" in call for call in run.calls)


def test_same_original_is_not_signed_again_after_upload(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path))
    (folder / "Commercial Invoice 1.pdf").write_text("Commercial Invoice 1.pdf")
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="already uploaded"):
        go(folder, tmp_path, run, ui=FakeUI(confirm=False), signer=fake_signer(tmp_path))  # the offer is declined
    assert run.calls == []


def test_new_same_name_can_resume_after_cancellation(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path))
    (folder / "Commercial Invoice 1.pdf").write_text("a different invoice")
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=FakeUI(confirm=False))
    run = FakeRun(tmp_path)
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path)) == 0
    assert any("--confirm" in call for call in run.calls)


def test_changed_signed_file_cannot_use_pending_association(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path))
    (folder / "Commercial Invoice 1.pdf").write_text("a different invoice")
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=FakeUI(confirm=False))
    (folder / "Commercial_Invoice_1_Signed.pdf").write_text("different signed file")
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="changed since signing"):
        go(folder, tmp_path, run, signer=fake_signer(tmp_path))
    assert run.calls == []


def test_changed_original_after_first_cancel_cannot_upload_old_signature(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=FakeUI(confirm=False))
    (folder / "Commercial Invoice 1.pdf").write_text("changed after signing")
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="changed since signing"):
        go(folder, tmp_path, run, signer=fake_signer(tmp_path))
    assert run.calls == []


def test_colliding_original_names_stop_before_signing(tmp_path):
    folder = make(tmp_path, "Commercial Invoice.pdf", "Commercial_Invoice.pdf", signed=False)
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="same signed filename"):
        go(folder, tmp_path, run, signer=fake_signer(tmp_path))
    assert run.calls == []
    assert not list(folder.glob("*_Signed.pdf"))


def test_signing_another_file_does_not_replace_existing_signed_twin(tmp_path):
    folder = make(tmp_path, "Commercial Invoice.pdf")
    signed = folder / "Commercial_Invoice_Signed.pdf"
    signed.write_text("existing signature")
    (folder / "Customs invoice.pdf").write_text("new")
    with pytest.raises(mod.Stop, match="not confirmed"):
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=FakeUI(confirm=False))
    assert signed.read_text() == "existing signature"


def test_windows_clipboard_reads_redirected_stdin(monkeypatch):
    # `$input` is pipeline input, not stdin: the PowerShell branch pasted nothing before.
    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="win32"))
    argv, data = mod.clipboard_command("<b>ümlaut & co</b>")
    assert argv[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "OpenStandardInput" in argv[-1] and "Set-Clipboard -AsHtml" in argv[-1]
    assert data == "<b>ümlaut & co</b>".encode("utf-8")


def test_copy_html_passes_the_draft_on_stdin(monkeypatch):
    calls = []

    class Done:
        returncode = 0

    monkeypatch.undo()  # the fixture replaces copy_html itself
    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: calls.append(kw) or Done())
    assert mod.copy_html("<p>hi</p>") is True
    assert calls[0]["input"] == b"<p>hi</p>"


def test_copy_html_is_false_without_a_clipboard_tool(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(mod, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    assert mod.clipboard_command("<b>x</b>") is None
    assert mod.copy_html("<b>x</b>") is False


def test_client_runner_forces_utf8_output(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod, "STATE_ENV", None)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod.client_runner()(["esanchit", "doc-types", "invoice"])
    assert seen["encoding"] == "utf-8" and seen["errors"] == "replace"
    assert seen["env"]["PYTHONUTF8"] == "1" and seen["env"]["PYTHONIOENCODING"] == "utf-8"


def test_prepare_pdf_compresses_when_default_dpi_exceeds_cap(tmp_path, monkeypatch):
    import pymupdf
    monkeypatch.undo()
    path = tmp_path / "large.pdf"
    with pymupdf.open() as doc:
        for i in range(20):
            page = doc.new_page(width=612, height=792)
            for y in range(50, 750, 25):
                page.insert_text((50, y), f"Sample text line {y} for page {i} of commercial document")
        doc.save(path)
    said = []
    mod.prepare_pdf(path, said.append)
    assert any("compressed and flattened" in s for s in said)
    with pymupdf.open(path) as doc:
        assert doc.metadata["producer"] == mod.FLAT_MARK
        pages = len(doc)
    assert path.stat().st_size + mod.SIGN_OVERHEAD_PER_PAGE * pages <= mod.PORTAL_FILE_CAP


def test_prepare_pdf_recompresses_oversized_flat_file(tmp_path, monkeypatch):
    import pymupdf
    monkeypatch.undo()
    path = tmp_path / "invoice.pdf"
    with pymupdf.open() as doc:
        for i in range(2):
            page = doc.new_page(width=612, height=792)
            page.insert_text((50, 50), f"Pristine original page {i}")
        doc.save(path)
    (tmp_path / ".original").mkdir()
    original_bytes = path.read_bytes()
    (tmp_path / ".original" / "invoice.pdf").write_bytes(original_bytes)
    # Make path flat at 120 dpi
    flat = tmp_path / "flat.pdf"
    mod.flatten_pdf(path, flat, dpi=120, quality=70)
    flat.replace(path)  # replace only after the source document has closed (Windows)
    # Set cap so 120 dpi is over cap, triggering recompression
    monkeypatch.setattr(mod, "PORTAL_FILE_CAP", 40 * 1024)
    monkeypatch.setattr(mod, "SIGN_OVERHEAD_PER_PAGE", 10 * 1024)
    monkeypatch.setattr(mod, "SAFETY_MARGIN", 5 * 1024)
    said = []
    mod.prepare_pdf(path, said.append)
    assert any("compressed and flattened" in s for s in said)
    assert (tmp_path / ".original" / "invoice.pdf").read_bytes() == original_bytes


def test_oversized_flat_file_is_recompressed_from_its_true_original(tmp_path, monkeypatch):
    import pymupdf
    monkeypatch.undo()
    path = tmp_path / "invoice.pdf"
    with pymupdf.open() as doc:
        for i in range(3):
            doc.new_page(width=612, height=792).insert_text((50, 50), f"Original page {i}")
        doc.save(path)
    original_bytes = path.read_bytes()
    mod.prepare_pdf(path, lambda _: None)  # first pass: flatten, back up the original
    monkeypatch.setattr(mod, "PORTAL_FILE_CAP", 40 * 1024)  # a tighter cap makes the flat file oversized
    monkeypatch.setattr(mod, "SIGN_OVERHEAD_PER_PAGE", 5 * 1024)
    monkeypatch.setattr(mod, "SAFETY_MARGIN", 5 * 1024)
    seen = []
    real = mod.flatten_pdf
    monkeypatch.setattr(mod, "flatten_pdf", lambda src, *a, **k: seen.append(src.read_bytes()) or real(src, *a, **k))
    mod.prepare_pdf(path, lambda _: None)
    assert seen and all(data == original_bytes for data in seen)  # never the lossy flat copy
    assert [p.name for p in (tmp_path / ".original").iterdir()] == ["invoice.pdf"]  # no extra backup
    with pymupdf.open(path) as doc:  # still points at the true original for a further pass
        assert doc.metadata["keywords"] == f"{mod.ORIGIN_TAG}{mod.file_hash(tmp_path / '.original' / 'invoice.pdf')}"


def test_oversized_signed_file_is_not_deleted(tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    (folder / "invoice.pdf").write_text("unsigned original")
    signed = folder / "invoice_Signed.pdf"
    content = b"x" * (mod.PORTAL_FILE_CAP + 100)
    signed.write_bytes(content)
    try:
        go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path), ui=None)
    except mod.Stop:
        pass
    assert signed.read_bytes() == content


def test_prepare_pdf_stops_if_no_profile_fits(tmp_path, monkeypatch):
    import pymupdf
    monkeypatch.undo()
    path = tmp_path / "invoice.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.save(path)
    original = path.read_bytes()
    monkeypatch.setattr(mod, "PORTAL_FILE_CAP", 1000)
    monkeypatch.setattr(mod, "SIGN_OVERHEAD_PER_PAGE", 100)
    monkeypatch.setattr(mod, "SAFETY_MARGIN", 100)
    monkeypatch.setattr(mod, "COMPRESSION_PROFILES", [(72, 40)])
    with pytest.raises(mod.Stop, match="cannot compress"):
        mod.prepare_pdf(path, lambda _: None)
    assert path.read_bytes() == original


def test_prepare_flat_pdf_does_not_use_unrelated_backup(tmp_path, monkeypatch):
    import pymupdf
    monkeypatch.undo()
    path = tmp_path / "invoice.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.set_metadata({"producer": mod.FLAT_MARK})
        doc.save(path)
    backup = tmp_path / ".original"
    backup.mkdir()
    (backup / path.name).write_bytes(b"unrelated old file")
    monkeypatch.setattr(mod, "PORTAL_FILE_CAP", 100000)
    monkeypatch.setattr(mod, "SIGN_OVERHEAD_PER_PAGE", 1000)
    monkeypatch.setattr(mod, "SAFETY_MARGIN", 1000)
    # Force the flattened input over the estimated cap, while its rendered copy fits.
    with path.open("ab") as out:
        out.write(b"x" * 100000)
    mod.prepare_pdf(path, lambda _: None)
    with pymupdf.open(path) as doc:
        assert len(doc) == 1


def test_be_number_is_looked_up_after_a_be_upload(tmp_path):
    folder = make(tmp_path, "113.be")
    run, said = FakeRun(tmp_path, rows=[BE_ROW]), []
    assert go(folder, tmp_path, run, said=said.append) == 0
    search = next(c for c in run.lookups if "--window" in c)
    assert search[2:] == ["--window", "BE=2026-11-05", "--events"]
    draft = (next((folder / "Done").iterdir()) / "draft-be.html").read_text()
    assert "4941305" in draft and "BE Number" in draft and "SB Number" not in draft


def test_a_rejection_notice_stops_the_wait_and_is_reported(tmp_path):
    folder = make(tmp_path, "113.be")
    run, said = FakeRun(tmp_path, rows=[], after=[REJECTED]), []
    with pytest.raises(mod.Stop) as stopped:
        go(folder, tmp_path, run, ui=None, yes=True, wait=600, said=said.append)
    assert sleeps == [] and "BE job 113" in str(stopped.value) and "820_SUPPDOCS" in str(stopped.value)
    assert "FAILED" in str(stopped.value)
    archive = next((folder / "Done").iterdir())
    assert (archive / "113_Signed.be").exists()  # the upload itself stays recorded
    assert "820_SUPPDOCS" in (archive / "draft-error.html").read_text()


def test_a_notice_from_before_the_upload_is_not_blamed_on_it(tmp_path):
    folder = make(tmp_path, "113.be")
    run = FakeRun(tmp_path, rows=[BE_ROW], before=[REJECTED])
    assert go(folder, tmp_path, run) == 0


def test_one_rejected_job_does_not_hide_the_others(tmp_path):
    folder = make(tmp_path, "893.sb", "113.be")
    run = FakeRun(tmp_path, rows=[SB_ROW], after=[REJECTED])
    with pytest.raises(mod.Stop) as stopped:
        go(folder, tmp_path, run)
    assert "BE job 113" in str(stopped.value)
    assert "8176055" in (next((folder / "Done").iterdir()) / "draft-sb.html").read_text()


def test_unreadable_baseline_falls_back_to_the_upload_day_and_a_number_still_wins(tmp_path):
    folder, said = make(tmp_path, "113.be"), []
    run = FakeRun(tmp_path, rows=[BE_ROW], before=[REJECTED], unreadable=1)  # later reads would show an old rejection
    assert go(folder, tmp_path, run, said=said.append) == 0
    assert any("could not read ICEGATE notices" in s for s in said)


# ------------------------------------------------------------------ --lookup, --refile, listing filters
def archived(tmp_path, *names, sub="in", fake=None):
    """A completed run (Done/<timestamp>) made by a wait-0 upload (which takes only the notices baseline)."""
    folder = make(tmp_path / sub, *names)
    assert go(folder, tmp_path, fake or FakeRun(tmp_path), wait=0, ui=None, yes=True) == 0
    return next((folder / "Done").iterdir())


def looked_up(runs, fake, **kw):
    said, lines = [], []
    code = mod.lookup([str(r) for r in runs], run=fake, say=said.append, emit=lines.append, **kw)
    return code, [json.loads(line) for line in lines], said


def test_lookup_reports_found_writes_the_draft_and_never_overwrites_it(tmp_path):
    archive = archived(tmp_path, "893.sb")
    run = FakeRun(tmp_path, rows=[SB_ROW])
    code, out, _ = looked_up([archive], run)
    assert code == 0 and len(run.lookups) == 1
    [row] = out
    assert row == {"run": str(archive), "file": "893_Signed.sb", "kind": "sb", "location": "INMBD6", "job": "893",
                   "status": "found", "number": "8176055", "date": "28-10-2026", "draft": str(archive / "draft-sb.html"),
                   "note": "", "notice": None, "errors": [], "refiled": False}
    draft = archive / "draft-sb.html"
    assert "8176055" in draft.read_text()
    draft.write_text("hand edited")
    _, out, _ = looked_up([archive], FakeRun(tmp_path, rows=[SB_ROW]))
    assert draft.read_text() == "hand edited" and out[0]["status"] == "found" and "left as it is" in out[0]["note"]


def test_lookup_pending_when_nothing_is_there_yet(tmp_path):
    archive = archived(tmp_path, "893.sb")
    code, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[]))
    assert code == 0 and row["status"] == "pending" and row["number"] is None and row["draft"] is None
    assert not list(archive.glob("draft-*"))


def test_lookup_of_several_runs_is_one_client_call(tmp_path):
    a, b = archived(tmp_path, "893.sb", sub="a"), archived(tmp_path, "113.be", sub="b")
    run = FakeRun(tmp_path, rows=[SB_ROW, BE_ROW])
    code, out, _ = looked_up([a, b], run)
    assert code == 0 and len(run.lookups) == 1
    assert run.lookups[0][2:] == ["--window", "BE=2026-11-05", "--window", "SB=2026-10-30", "--events",
                                  "--job-status", "BE:INHDD6", "--job-status", "SB:INMBD6"]
    assert {(r["kind"], r["status"]) for r in out} == {("sb", "found"), ("be", "found")}
    assert {r["run"] for r in out} == {str(a), str(b)}


def age_upload(archive, minutes):
    path = archive / ".uploaded.json"
    data = json.loads(path.read_text())
    for entry in data.values():
        entry["at"] = (datetime.now(mod.IST) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    path.write_text(json.dumps(data))


def status_row(job, minutes_ago, desc="Message CHCAI02 for positive acknowledgment generated"):
    stamp = (datetime.now(mod.IST) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S.123")
    return {"jobNo": job, "inboundRecievedTimestamp": stamp, "statusDesc": desc}


def test_lookup_not_received_when_job_status_has_nothing_since_the_upload(tmp_path):
    archive = archived(tmp_path, "113.be")
    age_upload(archive, 10)
    old = {"BE:INHDD6": [status_row("113", 600), status_row("999", 1)]}  # an earlier upload, and another job
    run = FakeRun(tmp_path, rows=[], job_status=old)
    _, [row], _ = looked_up([archive], run)
    assert row["status"] == "not_received" and "never received" in row["note"] and row["number"] is None


@pytest.mark.parametrize("minutes_old, rows", [
    (2, {}),                                                         # too soon to say
    (10, {"BE:INHDD6": [status_row("113", 8)]}),                     # received after the upload
    (10, {"BE:INHDD6": None}),                                       # Job Status unreadable: stay pending
])
def test_lookup_stays_pending_unless_job_status_proves_it_never_arrived(tmp_path, minutes_old, rows):
    archive = archived(tmp_path, "113.be")
    age_upload(archive, minutes_old)
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], job_status=rows))
    assert row["status"] == "pending"


def test_lookup_ignores_header_validation_notices_of_a_duplicate_upload(tmp_path):
    archive = archived(tmp_path, "113.be")
    duplicate = {**REJECTED, "detail": "header validation failed", "problem": "failed", "errors": ["duplicate"]}
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[duplicate]))
    assert row["status"] == "pending" and row["notice"] is None
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[duplicate, REJECTED]))
    assert row["status"] == "failed" and row["errors"] == ["820_SUPPDOCS"]


def test_lookup_not_filed_carries_the_notice(tmp_path):
    archive = archived(tmp_path, "113.be")
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[NOT_FILED]))
    assert row["status"] == "not_filed" and row["errors"] == [] and row["draft"] is None
    assert row["notice"] == {"message": NOT_FILED["message"], "date": CREATED}
    assert not (archive / "draft-error.html").exists()


def test_lookup_failed_carries_every_error_and_writes_the_error_table(tmp_path):
    archive = archived(tmp_path, "113.be")
    failed = {**REJECTED, "errors": ["820_SUPPDOCS", "131_IGMS <x>"]}
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[failed]))
    assert row["status"] == "failed" and row["errors"] == ["820_SUPPDOCS", "131_IGMS <x>"]
    assert row["notice"]["message"] == failed["message"] and row["draft"] == str(archive / "draft-error.html")
    text = (archive / "draft-error.html").read_text()
    assert "Job No" in text and "113_Signed.be" in text and "820_SUPPDOCS" in text and "131_IGMS &lt;x&gt;" in text


def test_a_number_beats_an_old_rejection_and_a_later_acceptance_beats_it_too(tmp_path):
    archive = archived(tmp_path, "113.be")
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[BE_ROW], before=[REJECTED]))
    assert row["status"] == "found"
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[ACCEPTED, REJECTED]))  # newest first
    assert row["status"] == "pending"


def test_lookup_partial_run_writes_no_table_and_says_so(tmp_path):
    archive = archived(tmp_path, "893.sb", "894.sb")
    other = archive / "894_Signed.sb"
    other.write_text(other.read_text().replace("\x1d893\x1d", "\x1d894\x1d"), newline="")  # only 893 has a number
    _, out, _ = looked_up([archive], FakeRun(tmp_path, rows=[SB_ROW]))
    assert {r["job"]: r["status"] for r in out} == {"893": "found", "894": "pending"}
    assert not (archive / "draft-sb.html").exists()
    assert "no number yet" in next(r for r in out if r["job"] == "893")["note"]


def test_lookup_flags_a_file_it_cannot_read_and_still_exits_zero(tmp_path):
    archive = archived(tmp_path, "893.sb")
    (archive / "893_Signed.sb").write_text("garbage")
    code, [row], _ = looked_up([archive], FakeRun(tmp_path))
    assert code == 0 and row["status"] == "unreadable" and row["job"] is None


def test_lookup_refuses_a_folder_that_is_not_a_run(tmp_path):
    folder = make(tmp_path, "893.sb")
    with pytest.raises(mod.Stop, match="not a completed run"):
        looked_up([folder], FakeRun(tmp_path))


def test_lookup_fails_loudly_when_the_portal_cannot_be_reached(tmp_path):
    archive = archived(tmp_path, "893.sb")

    class Down(FakeRun):
        def __call__(self, argv):
            if argv[:2] == ["ecopy", "lookup"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="login failed")
            return super().__call__(argv)

    with pytest.raises(mod.Stop, match="login failed"):
        looked_up([archive], Down(tmp_path))


def test_main_lookup_prints_json_lines_and_exit_codes(tmp_path, monkeypatch, capsys):
    archive = archived(tmp_path, "893.sb")
    fake = FakeRun(tmp_path, rows=[SB_ROW])
    monkeypatch.setattr(mod, "client_runner", lambda: fake)
    assert mod.main(["--lookup", str(archive), "--json"]) == 0
    [row] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert row["status"] == "found"
    for bad in (["--lookup", str(archive), "--yes"], ["--lookup", str(archive), "--dry-run"]):
        assert mod.main(bad) == 1
    monkeypatch.setattr(mod, "client_runner", lambda: (lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="net down")))
    assert mod.main(["--lookup", str(archive)]) == 1
    assert "net down" in capsys.readouterr().err


def test_main_parse_response_prints_the_clients_json(tmp_path, monkeypatch, capsys):
    calls = []

    def client(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='{"type": "nak"}\n', stderr="")
    monkeypatch.setattr(mod, "client_runner", lambda: client)
    assert mod.main(["--parse-response", "x.nak"]) == 0
    assert capsys.readouterr().out == '{"type": "nak"}\n' and calls[0][0] == "parse-response" and calls[0][2] == "--json"
    assert mod.main(["--parse-response", "x.nak", "--yes"]) == 1
    monkeypatch.setattr(mod, "client_runner", lambda: (lambda argv: SimpleNamespace(returncode=1, stdout="", stderr="error: not an ICEGATE SB/BE result file")))
    assert mod.main(["--parse-response", "x.nak"]) == 1
    assert "not an ICEGATE" in capsys.readouterr().err


def test_recheck_uses_one_lookup_and_writes_the_missing_table(tmp_path):
    archive = archived(tmp_path, "893.sb")
    run, said = FakeRun(tmp_path, rows=[SB_ROW]), []
    mod.recheck(archive, run=run, say=said.append)
    assert len([c for c in run.lookups if "--window" in c]) == 1
    assert "8176055" in (archive / "draft-sb.html").read_text()


def refiled(archive, fake, **kw):
    said = []
    code = mod.refile(archive, run=fake, say=said.append, **kw)
    return code, said


def uploads(fake):
    return [c for c in fake.calls if c[:2] == ["filing", "upload"]]


def test_refile_dry_run_sends_nothing_and_records_nothing(tmp_path):
    archive = archived(tmp_path, "113.be")
    before = (archive / ".uploaded.json").read_text()
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED])
    code, said = refiled(archive, fake, dry_run=True)
    assert code == 0 and uploads(fake) == [] and any("would file again: 113_Signed.be" in s for s in said)
    assert (archive / ".uploaded.json").read_text() == before


def test_refile_needs_yes(tmp_path):
    archive = archived(tmp_path, "113.be")
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED])
    with pytest.raises(mod.Stop, match="--yes"):
        refiled(archive, fake)
    assert uploads(fake) == []


@pytest.mark.parametrize("rows, before", [([], [REJECTED]), ([], []), ([BE_ROW], [NOT_FILED]), ([], [ACCEPTED, NOT_FILED])])
def test_refile_only_when_the_newest_notice_says_not_filed(tmp_path, rows, before):
    archive = archived(tmp_path, "113.be")
    fake = FakeRun(tmp_path, rows=rows, before=before)
    with pytest.raises(mod.Stop, match="nothing to file again"):
        refiled(archive, fake, yes=True)
    assert uploads(fake) == []


def test_refile_also_accepts_a_file_icegate_never_received(tmp_path):
    archive = archived(tmp_path, "113.be")
    age_upload(archive, 10)
    fake = FakeRun(tmp_path, rows=[], job_status={})
    code, _ = refiled(archive, fake, yes=True)
    assert code == 0 and len(uploads(fake)) == 2  # plan + confirm
    with pytest.raises(mod.Stop, match="already filed again"):
        refiled(archive, FakeRun(tmp_path, rows=[], job_status={}), yes=True)


def test_refile_uploads_the_archived_signed_file_once_and_records_it_first(tmp_path):
    archive = archived(tmp_path, "113.be")
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED])
    seen_at_send = []

    class Spy(FakeRun):
        def __call__(self, argv):
            if "--confirm" in argv and argv[:2] == ["filing", "upload"]:  # the real send
                seen_at_send.append(json.loads((archive / ".uploaded.json").read_text())["113_signed.be"].get("refile"))
            return fake(argv)

    code, said = refiled(archive, Spy(tmp_path), yes=True)
    assert code == 0
    plan, send = uploads(fake)
    assert plan[2:] == [str(archive / "113_Signed.be"), "--dry-run"]
    assert send[2:] == [str(archive / "113_Signed.be"), "--allow-duplicate", "--confirm", "FILE 1 abcdefabcdef"]
    assert not any(c[0] in ("sign",) for c in fake.calls)
    assert seen_at_send[0]["seen"] == 1  # recorded BEFORE the send, with the notices already there
    entry = json.loads((archive / ".uploaded.json").read_text())["113_signed.be"]
    assert entry["refile"]["plan_id"] == "abcdefabcdef"
    assert any("icerun --lookup" in s for s in said)


def test_a_second_refile_of_the_same_run_is_refused(tmp_path):
    archive = archived(tmp_path, "113.be")
    refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED]), yes=True)
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED, NOT_FILED])
    with pytest.raises(mod.Stop, match="already filed again"):
        refiled(archive, fake, yes=True)
    assert fake.calls == []  # refused before even logging in


def test_refile_refused_before_sending_is_not_counted(tmp_path):
    archive = archived(tmp_path, "113.be")
    with pytest.raises(mod.Stop):
        refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED], fail_filing=True, filing_exit=1), yes=True)
    assert "refile" not in json.loads((archive / ".uploaded.json").read_text())["113_signed.be"]
    code, _ = refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED]), yes=True)  # may be tried again
    assert code == 0


def test_refile_with_an_unknown_outcome_stays_recorded(tmp_path):
    archive = archived(tmp_path, "113.be")
    with pytest.raises(mod.Unsure):
        refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED], fail_filing=True, filing_exit=2), yes=True)
    assert "refile" in json.loads((archive / ".uploaded.json").read_text())["113_signed.be"]
    with pytest.raises(mod.Stop, match="already filed again"):
        refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED]), yes=True)


def test_lookup_after_a_refile_ignores_the_notices_from_before_it(tmp_path):
    archive = archived(tmp_path, "113.be")
    refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED]), yes=True)
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[NOT_FILED]))
    assert row["status"] == "pending" and row["refiled"] is True
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[REJECTED, NOT_FILED]))  # a new one after the refile
    assert row["status"] == "failed"


def test_main_refile_honours_yes_and_dry_run(tmp_path, monkeypatch, capsys):
    archive = archived(tmp_path, "113.be")
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED])
    monkeypatch.setattr(mod, "client_runner", lambda: fake)
    assert mod.main(["--refile", str(archive), "--dry-run"]) == 0 and uploads(fake) == []
    assert mod.main(["--refile", str(archive)]) == 1 and uploads(fake) == []  # no --yes


def test_filter_rows_matches_job_exactly_or_with_a_year_suffix_and_location():
    rows = [{"jobNumber": "642", "location": "INMBD6"}, {"jobNumber": "64226", "location": "INMBD6"},
            {"jobNumber": "700", "location": "INHDD6"}, {"jobNumber": "70026", "location": "INHDD6"}]
    assert mod.filter_rows(rows, job="642") == [rows[0]]
    assert mod.filter_rows(rows, job="700", loc="inhdd6") == [rows[2]]
    assert mod.filter_rows([rows[1], rows[3]], job="642") == [rows[1]]
    assert mod.filter_rows(rows, loc="INHDD6") == rows[2:]
    assert mod.filter_rows(rows, job="64") == []


def test_icerun_listing_filters_locally_from_one_fetch(tmp_path, monkeypatch, capsys):
    calls = []
    rows = [{**SB_ROW, "jobDate": "28-10-2026"}, {**SB_ROW, "jobNumber": "900", "docNumber": "5", "location": "INHDD6"}]

    def runner(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(mod, "client_runner", lambda: runner)
    assert mod.main(["--sb", "--days", "7", "--job", "893", "--loc", "inmbd6"]) == 0
    out = capsys.readouterr().out
    assert calls == [["ecopy", "sb", "--days", "7", "--json"]] and "8176055" in out and "900" not in out
    assert mod.main(["--sb", "--loc", "INHDD6", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [rows[1]]
    assert mod.main(["--notices", "--job", "893"]) == 1


# ------------------------------------------------------------------ the notices baseline taken before every upload
def ledger_entry(archive, name):
    return json.loads((archive / ".uploaded.json").read_text())[name]


def test_the_baseline_is_taken_before_the_upload_and_stored_even_with_wait_zero(tmp_path):
    order = []

    class Spy(FakeRun):
        def __call__(self, argv):
            order.append(["baseline"] if argv[:2] == ["ecopy", "lookup"] else ["send"] if "--confirm" in argv else argv[:2])
            return super().__call__(argv)

    archive = archived(tmp_path, "113.be", fake=Spy(tmp_path, before=[REJECTED, ACCEPTED]))
    assert order.index(["baseline"]) < order.index(["send"])  # the plan (--dry-run) may come first
    assert ledger_entry(archive, "113_signed.be")["baseline"] == {"seen": 2}


def test_an_old_same_day_rejection_is_not_counted_after_a_wait_zero_upload(tmp_path):
    archive = archived(tmp_path, "113.be", fake=FakeRun(tmp_path, before=[NOT_FILED]))  # already there at upload time
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[NOT_FILED]))
    assert row["status"] == "pending" and row["notice"] is None and "no baseline" not in row["note"]
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED])
    with pytest.raises(mod.Stop, match="nothing to file again"):  # never refiled on the strength of the old notice
        refiled(archive, fake, yes=True)
    assert uploads(fake) == []
    # a notice that appeared after the upload does count
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[NOT_FILED, NOT_FILED]))
    assert row["status"] == "not_filed"
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED, NOT_FILED])
    assert refiled(archive, fake, yes=True)[0] == 0 and len(uploads(fake)) == 2


def test_an_unreadable_baseline_still_uploads_and_stores_null_and_blocks_refile(tmp_path):
    archive = archived(tmp_path, "113.be", fake=FakeRun(tmp_path, unreadable=1))
    assert (archive / "113_Signed.be").exists() and ledger_entry(archive, "113_signed.be")["baseline"] is None
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[NOT_FILED]))
    assert row["status"] == "not_filed" and "no baseline" in row["note"] and "--refile" not in row["note"]
    fake = FakeRun(tmp_path, rows=[], before=[NOT_FILED])
    with pytest.raises(mod.Stop, match="no baseline"):
        refiled(archive, fake, yes=True)
    assert fake.calls == []  # refused before logging in


def test_a_run_from_before_baselines_existed_cannot_be_refiled(tmp_path):
    archive = archived(tmp_path, "113.be")
    data = json.loads((archive / ".uploaded.json").read_text())
    data["113_signed.be"].pop("baseline")
    (archive / ".uploaded.json").write_text(json.dumps(data))
    with pytest.raises(mod.Stop, match="no baseline"):
        refiled(archive, FakeRun(tmp_path, rows=[], before=[NOT_FILED]), yes=True, dry_run=True)
    _, [row], _ = looked_up([archive], FakeRun(tmp_path, rows=[], before=[NOT_FILED]))
    assert row["status"] == "not_filed" and "no baseline" in row["note"]


def test_a_failed_baseline_in_a_waiting_run_is_stored_too(tmp_path):
    folder = make(tmp_path, "113.be")
    assert go(folder, tmp_path, FakeRun(tmp_path, rows=[BE_ROW], unreadable=1), said=lambda _: None) == 0
    archive = next((folder / "Done").iterdir())
    assert ledger_entry(archive, "113_signed.be")["baseline"] is None


# ---------------------------------------------------------------- machine interface for the mail monitor
def test_signer_runs_with_sign_all_in_terminal_set_so_no_window_opens(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["env"] = kw["env"]
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    monkeypatch.delenv("SIGN_ALL_IN_TERMINAL", raising=False)
    mod.sign_folder(tmp_path, tmp_path / "signer", lambda _: None)
    assert seen["env"]["SIGN_ALL_IN_TERMINAL"] == "1"


def usb(tmp_path, vendor, product):
    dev = tmp_path / "usb" / f"1-{vendor}"
    dev.mkdir(parents=True)
    (dev / "idVendor").write_text(vendor + "\n")
    (dev / "idProduct").write_text(product + "\n")
    return tmp_path / "usb"


def test_token_reports_presence_as_json_and_exit_code(tmp_path):
    lines: list[str] = []
    assert mod.token(lines.append, usb(tmp_path, "2ccf", "080a")) == 0
    assert json.loads(lines[-1]) == {"token": "HYP2003", "usb": "2ccf:080a", "present": True}
    assert mod.token(lines.append, usb(tmp_path / "other", "1234", "5678")) == 1
    assert json.loads(lines[-1])["present"] is False
    assert mod.token(lines.append, tmp_path / "no-such-sysfs") == 1


def test_main_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "USB_DEVICES", usb(tmp_path, "2ccf", "080a"))
    assert mod.main(["--token"]) == 0
    assert json.loads(capsys.readouterr().out)["present"] is True
    monkeypatch.setattr(mod, "USB_DEVICES", tmp_path / "empty")
    assert mod.main(["--token"]) == 1
    assert mod.main(["--token", "--yes"]) == 1  # takes no other arguments


def test_drafts_lists_tables_of_job_folders_and_runs_in_reply_order(tmp_path, capsys):
    job = tmp_path / "job"
    run = job / "Done" / "20261128-120000-000001"
    run.mkdir(parents=True)
    (job / "Done" / "not-a-run").mkdir()
    (job / "Done" / "not-a-run" / "draft-sb.html").write_text("x")
    for kind in ("error", "be", "sb", "esanchit"):
        (run / f"draft-{kind}.html").write_text(kind)
    lines: list[str] = []
    assert mod.list_drafts([str(job)], lines.append) == 0
    rows = [json.loads(line) for line in lines]
    assert [r["kind"] for r in rows] == ["esanchit", "sb", "be", "error"]
    assert all(r["run"] == str(run) and r["draft"] == str(run / f"draft-{r['kind']}.html") for r in rows)
    again: list[str] = []
    mod.list_drafts([str(run)], again.append)  # a run folder itself works too
    assert again == lines
    with pytest.raises(mod.Stop, match="not a folder"):
        mod.list_drafts([str(tmp_path / "missing")], lines.append)
    assert mod.main(["--drafts", str(run)]) == 0
    assert capsys.readouterr().out.splitlines() == lines
    assert mod.main(["--drafts", str(run), "--dry-run"]) == 1


def test_json_upload_prints_one_result_line_and_keeps_narration_on_stderr(tmp_path, monkeypatch, capsys):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "893.sb")
    fake = FakeRun(tmp_path)
    monkeypatch.setattr(mod, "client_runner", lambda: fake)
    assert mod.main([str(folder), "--yes", "--wait", "0", "--json", "--no-sign", "--type", "331000"]) == 0
    out, err = capsys.readouterr()
    [result] = [json.loads(line) for line in out.splitlines()]  # stdout: nothing but the one JSON line
    archive = next((folder / "Done").iterdir())
    assert result == {"ok": True, "runs": [
        {"dir": str(archive), "kind": "esanchit", "files": ["Commercial_Invoice_1_Signed.pdf"],
         "draft": str(archive / "draft-esanchit.html")},
        {"dir": str(archive), "kind": "filing", "files": ["893_Signed.sb"], "draft": None}]}
    assert (archive / "draft-esanchit.html").is_file()
    assert "filing    893_Signed.sb" in err  # the narration is still there, on stderr


def test_json_upload_failure_reports_what_was_already_uploaded(tmp_path, monkeypatch, capsys):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "893.sb")
    monkeypatch.setattr(mod, "client_runner", lambda: FakeRun(tmp_path, fail_filing=True))
    assert mod.main([str(folder), "--yes", "--wait", "0", "--json", "--no-sign", "--type", "331000"]) == 1
    out, err = capsys.readouterr()
    result = json.loads(out.splitlines()[-1])
    assert result["ok"] is False and "filing upload stopped" in result["error"]
    assert [r["kind"] for r in result["runs"]] == ["esanchit"]  # eSANCHIT went in before the filing was refused
    assert "stopped:" in err


def test_json_upload_without_yes_is_refused_even_on_a_terminal(tmp_path, monkeypatch, capsys):
    folder = make(tmp_path, "893.sb")
    monkeypatch.setattr(mod, "client_runner", lambda: FakeRun(tmp_path))
    monkeypatch.setattr(mod.sys.stdin, "isatty", lambda: True)
    assert mod.main([str(folder), "--wait", "0", "--json", "--no-sign"]) == 1
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["ok"] is False and result["runs"] == [] and not (folder / "Done").exists()


# ---- offering to delete an already-uploaded Done run so the file can be filed again
class RedoUI(FakeUI):
    """Answers the 'Delete that run' question with `redo` and every other confirmation with `confirm`."""

    def __init__(self, redo=True, **kw):
        super().__init__(**kw)
        self.redo, self.redo_prompts = redo, []

    def confirm(self, text):
        if "Delete that run" in text:
            self.redo_prompts.append(text)
            return self.redo
        return super().confirm(text)


def uploaded_once(tmp_path, *names):
    """Upload `names` (unsigned originals) once, then put the same originals back in the folder."""
    folder = make(tmp_path, *names, signed=False)
    contents = {n: (folder / n).read_text(newline="") for n in names}
    assert go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path)) == 0
    for n, body in contents.items():
        (folder / n).write_text(body, newline="")
    (done,) = [p for p in (folder / "Done").iterdir() if p.is_dir()]
    return folder, done


@pytest.fixture
def no_gio(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)


def test_interactive_yes_removes_the_run_forgets_it_and_uploads_again(tmp_path, no_gio):
    folder, old = uploaded_once(tmp_path, "893.sb")
    ui, run, said = RedoUI(redo=True), FakeRun(tmp_path), []
    assert go(folder, tmp_path, run, ui=ui, yes=True, signer=fake_signer(tmp_path), said=said.append) == 0
    (prompt,) = ui.redo_prompts
    assert "Done/" + old.name in prompt and "893_Signed.sb" in prompt
    assert "893.sb, 893_Signed.sb, draft-sb.html" in prompt            # everything in the run is listed
    assert "SB 8176055" in prompt                                       # number already known from the draft
    assert "second time" in prompt and "duplicate filing" in prompt
    assert not old.exists() and (folder / "Done" / ".deleted" / old.name / "893_Signed.sb").is_file()
    assert any(f"Done/.deleted/{old.name}" in s for s in said)
    assert any("--confirm" in c for c in run.calls)                     # signed and uploaded in the same invocation
    # the new run is the only one the ledger sees; the removed one is ignored (dot-folder) everywhere
    (new,) = [p for p in (folder / "Done").iterdir() if p.is_dir() and not p.name.startswith(".")]
    assert new != old and mod.Ledger(folder / "Done").seen(Path("893_Signed.sb"))["run"] == new.name
    assert mod.completed_runs(folder) == [new]


def test_ledger_forgets_a_removed_run_and_ignores_dot_folders(tmp_path, no_gio):
    folder, old = uploaded_once(tmp_path, "893.sb")
    done = folder / "Done"
    ledger = mod.Ledger(done)
    assert ledger.seen(Path("893_Signed.sb"))["run"] == old.name
    mod.retire_run(old, done)
    assert ledger.seen(Path("893_Signed.sb")) is not None               # in memory until reloaded
    ledger.reload()
    assert ledger.seen(Path("893_Signed.sb")) is None
    assert mod.Ledger(done).seen(Path("893_Signed.sb")) is None         # a fresh process agrees


def test_declining_the_offer_stops_with_the_same_message_and_changes_nothing(tmp_path, no_gio):
    folder, old = uploaded_once(tmp_path, "893.sb")
    ui, run = RedoUI(redo=False), FakeRun(tmp_path)
    with pytest.raises(mod.Stop) as exc:
        go(folder, tmp_path, run, ui=ui, signer=fake_signer(tmp_path))
    assert str(exc.value) == (f"893.sb was already uploaded or its outcome is unknown (Done/{old.name}); "
                              "delete that Done run to upload it again")
    assert len(ui.redo_prompts) == 1 and run.calls == []
    assert (old / "893_Signed.sb").is_file() and not (folder / "Done" / ".deleted").exists()


def test_without_a_terminal_the_run_is_never_offered_for_deletion(tmp_path, no_gio):
    folder, old = uploaded_once(tmp_path, "893.sb")
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop) as exc:
        go(folder, tmp_path, run, ui=None, yes=True, signer=fake_signer(tmp_path))
    assert str(exc.value).startswith(f"893.sb was already uploaded or its outcome is unknown (Done/{old.name}); "
                                     "delete that Done run to upload it again")
    assert run.calls == [] and old.exists() and not (folder / "Done" / ".deleted").exists()


def test_yes_flag_never_agrees_to_a_second_filing(tmp_path, no_gio):
    folder, old = uploaded_once(tmp_path, "893.sb")
    ui, run = RedoUI(redo=False), FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="already uploaded"):
        go(folder, tmp_path, run, ui=ui, yes=True, signer=fake_signer(tmp_path))
    assert len(ui.redo_prompts) == 1                                    # asked despite --yes, default answer is no
    assert run.calls == [] and old.exists()


def test_the_real_prompt_defaults_to_no(monkeypatch):
    seen = {}

    class Q:
        @staticmethod
        def confirm(text, default):
            seen["default"] = default
            return SimpleNamespace(ask=lambda: False)

    ui = mod.Prompts.__new__(mod.Prompts)
    ui.q = Q
    assert ui.confirm("Delete that run and upload again?") is False and seen["default"] is False


def test_several_files_of_one_run_are_asked_about_once(tmp_path, no_gio):
    folder, old = uploaded_once(tmp_path, "Commercial Invoice 1.pdf", "Customs invoice 2.pdf")
    ui, run = RedoUI(redo=True), FakeRun(tmp_path)
    assert go(folder, tmp_path, run, ui=ui, signer=fake_signer(tmp_path)) == 0
    (prompt,) = ui.redo_prompts
    assert "Commercial_Invoice_1_Signed.pdf" in prompt and "Customs_invoice_2_Signed.pdf" in prompt
    assert "files them on ICEGATE a second time" in prompt
    assert not old.exists()


def test_two_runs_are_each_asked_and_one_no_removes_neither(tmp_path, no_gio):
    folder, _first = uploaded_once(tmp_path, "Commercial Invoice 1.pdf")
    (folder / "Commercial Invoice 1.pdf").unlink()
    (folder / "Customs invoice 2.pdf").write_text("Customs invoice 2.pdf")
    assert go(folder, tmp_path, FakeRun(tmp_path), signer=fake_signer(tmp_path)) == 0
    (folder / "Commercial Invoice 1.pdf").write_text("Commercial Invoice 1.pdf")
    (folder / "Customs invoice 2.pdf").write_text("Customs invoice 2.pdf")
    runs = sorted(p for p in (folder / "Done").iterdir() if p.is_dir())
    assert len(runs) == 2

    class FirstYesThenNo(RedoUI):
        def confirm(self, text):
            answer = super().confirm(text) if "Delete that run" not in text else not self.redo_prompts
            if "Delete that run" in text:
                self.redo_prompts.append(text)
            return answer

    ui, run = FirstYesThenNo(), FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="already uploaded"):
        go(folder, tmp_path, run, ui=ui, signer=fake_signer(tmp_path))
    assert len(ui.redo_prompts) == 2 and all(r.exists() for r in runs) and run.calls == []


def test_retire_run_uses_gio_trash_when_it_works(tmp_path, monkeypatch):
    folder, old = uploaded_once(tmp_path, "893.sb")
    trash = tmp_path / "trash"
    trash.mkdir()
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        Path(argv[2]).rename(trash / Path(argv[2]).name)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/gio")
    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert "trash" in mod.retire_run(old, folder / "Done")
    assert calls == [["/usr/bin/gio", "trash", str(old)]] and (trash / old.name).is_dir()
    assert not (folder / "Done" / ".deleted").exists()


def test_retire_run_falls_back_to_done_deleted_when_gio_fails_and_never_overwrites(tmp_path, monkeypatch):
    folder, old = uploaded_once(tmp_path, "893.sb")
    done = folder / "Done"
    (done / ".deleted" / old.name).mkdir(parents=True)                  # an earlier removal of the same name
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/gio")
    monkeypatch.setattr(mod.subprocess, "run", lambda argv, **kw: SimpleNamespace(returncode=1, stdout="", stderr="no"))
    said = mod.retire_run(old, done)
    assert f"Done/.deleted/{old.name}.1" in said and (done / ".deleted" / f"{old.name}.1" / "893_Signed.sb").is_file()
    assert not old.exists()


def test_retire_run_refuses_anything_that_is_not_a_run_in_done(tmp_path, no_gio):
    folder, _old = uploaded_once(tmp_path, "893.sb")
    with pytest.raises(mod.Stop, match="refusing"):
        mod.retire_run(folder, folder / "Done")
    with pytest.raises(mod.Stop, match="refusing"):
        mod.retire_run(folder / "Done", folder / "Done")
