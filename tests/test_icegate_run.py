import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
REJECTED = {"outcome": "rejected", "doc": "BE", "job": "113", "date": "2026-11-05", "location": None,
            "detail": "error code 820_SUPPDOCS", "message": "Your bill of entry filing with job no. 113 dated 05112026 has been rejected."}

SB_FILE = "HREC\x1dZZ\x1dINMBD6\r\n<TABLE>SB\r\nF\x1dinmbd6\x1d893\x1d20261030\x1d\r\n<END-SB>\r\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE", tmp_path / "state")
    monkeypatch.setattr(mod, "copy_html", lambda text: True)
    monkeypatch.setattr(mod, "prepare_pdf", lambda path, say: None)  # the fixtures are not real PDFs


class FakeRun:
    """Stands in for the client; every argv must parse with the client's real parser."""

    def __init__(self, tmp_path, fail=None, fail_filing=False, rows=None, before=(), after=(), unreadable=0):
        self.calls, self.fail, self.fail_filing, self.rows, self.tmp = [], fail, fail_filing, rows, tmp_path
        self.before, self.after, self.polled, self.unreadable = list(before), list(after), 0, unreadable

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
            return ok("", 1, "error: portal said no\n") if self.fail_filing else ok("accepted\n")
        if argv[0] == "notifications":
            self.polled += 1  # the first call is the baseline taken before uploading
            if self.polled <= self.unreadable:
                return ok("", 1, "boom")
            return ok(json.dumps(self.before if self.polled == 1 else self.after + self.before))
        return ok(json.dumps(self.rows if self.rows is not None else [SB_ROW]))


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
    return mod.run_folder(folder, run=run, say=kw.pop("said", lambda _: None), state=tmp_path / "state", **kw)


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
    done = next((folder / "Done").iterdir())
    assert sorted(p.name for p in done.iterdir()) == sorted([
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
    with pytest.raises(mod.Stop, match="cannot tell"):          # no terminal: ambiguous names cannot be decided
        go(make(tmp_path / "x", "INV 1.pdf"), tmp_path, FakeRun(tmp_path), ui=None, yes=True)
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
    assert any("no SB found yet" in s for s in said)
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
    with pytest.raises(mod.Stop, match="cannot tell"):
        mod.assign_codes([odd], None, mod.catalog())


def test_unsigned_originals_are_signed_first_then_uploaded(tmp_path):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", "Customs invoice 2.pdf", signed=False)
    run, said = FakeRun(tmp_path), []
    assert go(folder, tmp_path, run, signer=fake_signer(tmp_path), said=said.append) == 0
    assert any("signing with" in s for s in said)
    assert sorted(p.name for p in next((folder / "Done").iterdir()).iterdir()) == sorted([
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


def test_sb_lookup_uses_job_date_and_rejects_incomplete_results(tmp_path):
    run = FakeRun(tmp_path, rows=[{**SB_ROW, "docNumber": ""}])
    with pytest.raises(mod.NotYet):
        mod.sb_rows(run, "INMBD6", [("893", "2026-10-30")])
    assert "--job-date" in run.calls[0] and "2026-10-30" in run.calls[0]


def test_sb_lookup_rejects_wrong_job_date(tmp_path):
    run = FakeRun(tmp_path, rows=[{**SB_ROW, "jobDate": "29-10-2026"}])
    with pytest.raises(mod.NotYet):
        mod.sb_rows(run, "INMBD6", [("893", "2026-10-30")])


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
    assert all(call[:2] == ["ecopy", "search"] or call[0] == "notifications" for call in run.calls)


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
        if argv[:2] == ["ecopy", "search"]:
            if argv[argv.index("--job-number") + 1] != SB_ROW["jobNumber"]:  # year-suffixed fallback search
                self.rows = []
                return super().__call__(argv)
            self.searches += 1
            self.rows = [SB_ROW] if self.searches >= 3 else []
        return super().__call__(argv)


def test_sb_number_found_under_year_suffixed_job_number(tmp_path):
    """Record job 893 filed as e-copy job 89326 (record number + two-digit year)."""
    row = {**SB_ROW, "jobNumber": "89326", "jobDate": "30-10-2026"}

    class Suffixed(FakeRun):
        def __call__(self, argv):
            if argv[:2] == ["ecopy", "search"]:
                self.rows = [row] if argv[argv.index("--job-number") + 1] == "89326" else []
            return super().__call__(argv)

    folder = make(tmp_path, "893.sb")
    assert go(folder, tmp_path, Suffixed(tmp_path), wait=0) == 0
    assert "89326" in (next((folder / "Done").iterdir()) / "draft-sb.html").read_text()


def test_waits_for_the_sb_number_to_appear(tmp_path):
    folder = make(tmp_path, "893.sb")
    run, ui = LateSB(tmp_path), FakeUI()
    assert go(folder, tmp_path, run, ui=ui, wait=600) == 0
    assert run.searches == 3 and len(ui.pauses) == 2 and all("checking again" in m for m in ui.pauses)
    assert (next((folder / "Done").iterdir()) / "draft-sb.html").exists()


def test_waiting_without_a_terminal_uses_plain_sleep(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    run = LateSB(tmp_path)
    assert go(make(tmp_path, "893.sb"), tmp_path, run, ui=None, yes=True, wait=600) == 0
    assert sleeps == [mod.POLL_SECONDS] * 2


def test_gives_up_waiting_after_the_limit(tmp_path):
    folder = make(tmp_path, "893.sb")
    said = []
    assert go(folder, tmp_path, FakeRun(tmp_path, rows=[]), wait=0, said=said.append) == 0
    assert any("no SB found yet" in s for s in said)


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
    assert sorted(p.name for p in next((folder / "Done").iterdir()).iterdir()) == sorted([
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
        go(folder, tmp_path, run, signer=fake_signer(tmp_path))
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
    search = next(c for c in run.calls if c[:2] == ["ecopy", "search"])
    assert search[2:] == ["BE", "--location", "INHDD6", "--job-number", "113", "--job-date", "2026-11-05", "--json"]
    draft = (next((folder / "Done").iterdir()) / "draft-be.html").read_text()
    assert "4941305" in draft and "BE Number" in draft and "SB Number" not in draft


def test_a_rejection_notice_stops_the_wait_and_is_reported(tmp_path, monkeypatch):
    sleeps = []
    monkeypatch.setattr(mod.time, "sleep", sleeps.append)
    folder = make(tmp_path, "113.be")
    run, said = FakeRun(tmp_path, rows=[], after=[REJECTED]), []
    with pytest.raises(mod.Stop) as stopped:
        go(folder, tmp_path, run, ui=None, yes=True, wait=600, said=said.append)
    assert sleeps == [] and "BE job 113" in str(stopped.value) and "820_SUPPDOCS" in str(stopped.value)
    assert (next((folder / "Done").iterdir()) / "113_Signed.be").exists()  # the upload itself stays recorded


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


def test_unreadable_baseline_disables_rejection_reports_instead_of_guessing(tmp_path):
    folder, said = make(tmp_path, "113.be"), []
    run = FakeRun(tmp_path, rows=[BE_ROW], before=[REJECTED], unreadable=1)  # later reads would show an old rejection
    assert go(folder, tmp_path, run, said=said.append) == 0
    assert any("could not read ICEGATE notifications" in s for s in said)
