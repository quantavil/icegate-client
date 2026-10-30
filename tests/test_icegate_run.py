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

SB_FILE = "HREC\x1dZZ\x1dINMBD6\r\n<TABLE>SB\r\nF\x1dinmbd6\x1d893\x1d20261030\x1d\r\n<END-SB>\r\n"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "STATE", tmp_path / "state")
    monkeypatch.setattr(mod, "copy_html", lambda text: True)
    monkeypatch.setattr(mod, "prepare_pdf", lambda path, say: None)  # the fixtures are not real PDFs


class FakeRun:
    """Stands in for the client; every argv must parse with the client's real parser."""

    def __init__(self, tmp_path, fail=None, fail_filing=False, rows=None):
        self.calls, self.fail, self.fail_filing, self.rows, self.tmp = [], fail, fail_filing, rows, tmp_path

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
        body = SB_FILE if ext == "sb" else name
        (folder / name).write_text(body, newline="")
        if signed:
            (folder / (stem.replace(" ", "_") + "_Signed." + ext)).write_text("signed " + body, newline="")
    return folder


def fake_signer(tmp_path, code=0):
    script = tmp_path / "signer.sh"
    script.write_text('#!/bin/bash\nfor f in "$1"/*; do case "$f" in *_Signed.*) ;; *) t="${f%.*}_Signed.${f##*.}"; '
                      f'cp "$f" "${{t// /_}}";; esac; done\nexit {code}\n')
    script.chmod(0o700)
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
            self.searches += 1
            self.rows = [SB_ROW] if self.searches >= 3 else []
        return super().__call__(argv)


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


def test_refused_pdf_is_flattened_signed_again_and_retried_once(tmp_path, monkeypatch):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    flattened = []
    monkeypatch.setattr(mod, "flatten_pdf", lambda src, dst: (flattened.append(src.name), dst.write_text("flat")))
    run, ui = FakeRun(tmp_path), FakeUI()
    refused = []
    inner = run.__call__

    def refusing(argv):
        out = inner(argv)
        if "--confirm" in argv and argv[:2] == ["esanchit", "submit"] and not refused:
            refused.append(1)
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="error: docs_error: Please upload PDF files only, Commercial_Invoice_1_Signed.pdf is not a valid PDF file.\n")
        return out
    assert go(folder, tmp_path, refusing, ui=ui, signer=fake_signer(tmp_path)) == 0
    assert flattened == ["Commercial Invoice 1.pdf"] and len(ui.confirms) == 3     # upload, flatten, upload again
    done = next((folder / "Done").iterdir())
    assert (done / "Commercial_Invoice_1_Signed.pdf").read_text() == "flat"        # signed from the flat copy
    assert (folder / ".original" / "Commercial Invoice 1.pdf").read_text() == "Commercial Invoice 1.pdf"


def test_refused_pdf_stays_refused_without_a_second_yes(tmp_path, monkeypatch):
    folder = make(tmp_path, "Commercial Invoice 1.pdf", signed=False)
    monkeypatch.setattr(mod, "flatten_pdf", lambda src, dst: dst.write_text("flat"))
    ui, answers = FakeUI(), iter([True, False])
    ui.confirm = lambda text: next(answers)
    run = FakeRun(tmp_path)
    with pytest.raises(mod.Stop, match="Already submitted in this run: nothing"):
        go(folder, tmp_path, lambda a: SimpleNamespace(
            returncode=1, stdout="", stderr="X_Signed.pdf is not a valid PDF file") if "--confirm" in a else run(a),
           ui=ui, signer=fake_signer(tmp_path))
    assert len([c for c in run.calls if "--confirm" in c]) == 0


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
