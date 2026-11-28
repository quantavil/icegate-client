"""ICEGATE .ack / .nak result files: small fixtures with the signature blocks stripped (ids made up)."""
import json
from pathlib import Path

import pytest

from icegate_client import cli
from icegate_client.response import parse_response
from icegate_client.shared.errors import IcegateError

FIXTURES = Path(__file__).parent / "fixtures"


def parsed(name):
    return parse_response((FIXTURES / name).read_bytes())


def test_sb_nak_lists_each_distinct_error_once():
    result = parsed("sb_nak_1037.nak")
    assert (result["type"], result["doc"]) == ("nak", "SB")
    [job] = result["jobs"]
    assert (job["location"], job["job"], job["job_date"], job["number"]) == ("INHPI6", "1037", "2026-11-26", None)
    assert job["errors"] == [f"license and Item UQC mismatch for Inv./Item No/Regn No:1/{i}/0511040858" for i in (1, 2, 3)]


def test_sb_nak_with_one_error():
    [job] = parsed("sb_nak_665.nak")["jobs"]
    assert (job["location"], job["job"], job["job_date"]) == ("INCPL6", "665", "2026-11-27")
    assert job["errors"] == ["Taxable value/IGST amount payable is missing  For Inv./Item No:1/3"]


def test_sb_nak_keeps_every_error_of_a_long_list():
    [job] = parsed("sb_nak_623.nak")["jobs"]
    assert job["job"] == "623" and len(job["errors"]) > 3 and len(set(job["errors"])) == len(job["errors"])


def test_be_nak_names_the_igm_mbl_hbl_it_is_about():
    result = parsed("be_nak_191.nak")
    assert (result["type"], result["doc"]) == ("nak", "BE")
    [job] = result["jobs"]
    assert (job["location"], job["job"], job["job_date"]) == ("INPPG6", "191", "2026-11-27")
    assert job["errors"] == ["610 (IGMS) IGM 4413602, MBL CHN4132800, HBL EURFL26T07270ICD"]


def test_be_nak_with_main_and_item_rows_is_one_job_with_one_error_per_row():
    [job] = parsed("be_nak_main_items.nak")["jobs"]
    assert (job["location"], job["job"]) == ("INHPI6", "100")
    assert job["errors"][:3] == ["708 (MAIN)", "485, 486, 484 (ITEMS)", "872 (ITEMS)"]  # "null" codes are dropped
    assert len(job["errors"]) == 5


def test_sb_ack_has_the_number_and_date():
    result = parsed("sb_ack.ack")
    assert (result["type"], result["doc"]) == ("ack", "SB")
    assert result["jobs"] == [{"location": "INMBD6", "job": "1047", "job_date": "2026-11-27", "number": "9058142",
                               "date": "2026-11-28", "errors": []}]


def test_be_ack_has_the_number_but_no_job():
    result = parsed("be_ack.ack")
    assert (result["type"], result["doc"]) == ("ack", "BE")
    assert result["jobs"] == [{"location": "INHPI6", "job": None, "job_date": None, "number": "7399807",
                               "date": "2026-11-25", "errors": []}]


def test_text_input_works_as_well_as_bytes():
    assert parse_response((FIXTURES / "sb_ack.ack").read_bytes().decode("latin1"))["type"] == "ack"


@pytest.mark.parametrize("data", [b"", b"just some text\n", b"HREC\x1da\x1db\n", b"HREC\x1d1\x1d2\x1d3\x1d4\x1d5\x1d6\x1d7\x1dOTHER\n"])
def test_anything_else_is_refused(data):
    with pytest.raises(IcegateError, match="not an ICEGATE SB/BE result file"):
        parse_response(data)


def test_cli_prints_one_json_line(capsys):
    assert cli.main(["parse-response", str(FIXTURES / "sb_nak_665.nak"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"][0]["job"] == "665"


def test_cli_text_output_and_bad_file(capsys, tmp_path):
    assert cli.main(["parse-response", str(FIXTURES / "be_nak_191.nak")]) == 0
    assert "BE nak INPPG6 job 191" in capsys.readouterr().out
    bad = tmp_path / "x.nak"
    bad.write_text("junk")
    assert cli.main(["parse-response", str(bad)]) == 1
    assert cli.main(["parse-response", str(tmp_path / "missing.nak")]) == 1
