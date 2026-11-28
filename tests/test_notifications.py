from datetime import date
import json
import pytest

from icegate_client import cli
from icegate_client.notifications import (
    NotificationClient,
    filing_events,
    filter_by_days,
    parse_date,
    parse_notification,
)


def msg(text):
    return parse_notification(text)


def test_submitted_and_rejected_by_job():
    ok = msg("Dear Sir/Madam,\nYour bill of entry filing with job no. 113 dated 20261105 has been successfully submitted.\nRegards")
    assert (ok["outcome"], ok["doc"], ok["job"], ok["date"]) == ("submitted", "BE", "113", "2026-11-05")
    bad = msg('"Your bill of entry filing with job no. 112 dated 05112026 has been rejected. Error code: 820_SUPPDOCS\n-CBIC "')
    assert (bad["outcome"], bad["job"], bad["date"], bad["detail"]) == ("rejected", "112", "2026-11-05", "error code 820_SUPPDOCS")
    assert msg('"Your shipping bill filing with job no. 952 dated 07112026 has been rejected.')["detail"] == ""


def test_a_document_number_is_not_a_job_number():
    event = msg("Your bill of entry filing with be no. 4836195 dated 20261103 has been successfully submitted.")
    assert event["job"] is None and event["number"] == "4836195"


@pytest.mark.parametrize("stage", ["Header", "Field", "DSC"])
def test_validation_failures_carry_the_control_number_as_job(stage):
    event = msg(f"Dear Sir/Madam,\n{stage} validation failed for control no. 113, filing date 20261105, "
                "Receiver ID INHDD6, message ID CACHI01. Please use a valid input file.")
    assert (event["outcome"], event["job"], event["date"], event["location"]) == ("failed", "113", "2026-11-05", "INHDD6")
    assert event["detail"].startswith(stage.lower())


def test_field_errors_are_included():
    event = msg("Field validation failed for control no. 721, filing date 20261103, Receiver ID INTKD6, message ID X. "
                "Please use a valid input file.\n\n\nErrors: finalInvoiceModel[0].buyerAddress1: too long")
    assert "buyerAddress1" in event["detail"]


def test_unrelated_messages_are_ignored_and_order_is_kept():
    rows = [{"message": "Your shipping bill filing has been successfully generated with SB number 8536843.\n-CBIC"},
            {"message": "Your bill of entry filing with job no. 1 dated 20261105 has been rejected.", "createdDate": "09.11.2026"},
            {"message": "You have successfully changed your ICEGATE password."}]
    events = filing_events(rows)
    assert [e["job"] for e in events] == ["1"] and events[0]["created"] == "09.11.2026"


def test_unexpected_payload_is_an_error():
    with pytest.raises(Exception):
        filing_events({"error": "x"})


def test_greeting_signoff_and_quotes_are_removed():
    assert msg("Dear Sir/Madam,\nYour bill of entry filing with job no. 1 dated 20261105 has been successfully submitted.\n"
               "Regards,\nICEGATE Support Team")["message"] == \
        "Your bill of entry filing with job no. 1 dated 20261105 has been successfully submitted."
    assert msg('"Your bill of entry filing with job no. 1 dated 05112026 has been rejected. Error code: 820_SUPPDOCS\n-CBIC "')["message"] == \
        "Your bill of entry filing with job no. 1 dated 05112026 has been rejected. Error code: 820_SUPPDOCS"


def test_parse_date_formats():
    assert parse_date("23.11.2026") == date(2026, 11, 23)
    assert parse_date("23-11-2026") == date(2026, 11, 23)
    assert parse_date("2026-11-23") == date(2026, 11, 23)
    assert parse_date("23/11/2026") == date(2026, 11, 23)
    assert parse_date("20261123") == date(2026, 11, 23)
    assert parse_date("23112026") == date(2026, 11, 23)
    assert parse_date("23.11.2026 15:30:00") == date(2026, 11, 23)
    assert parse_date("2026-11-23T15:30:00") == date(2026, 11, 23)
    assert parse_date(date(2026, 11, 23)) == date(2026, 11, 23)
    assert parse_date("") is None
    assert parse_date(None) is None
    assert parse_date("not-a-date") is None


def test_filter_by_days_raw():
    now = date(2026, 11, 23)
    items = [
        {"createdDate": "23.11.2026", "message": "today"},
        {"createdDate": "22.11.2026", "message": "yesterday"},
        {"createdDate": "18.11.2026", "message": "5 days ago"},
        {"createdDate": "10.11.2026", "message": "13 days ago"},
        {"createdDate": "15.10.2026", "message": "39 days ago"},
    ]

    # days=1: only today
    r1 = filter_by_days(items, days=1, is_events=False, now=now)
    assert [x["message"] for x in r1] == ["today"]

    # days=2: today and yesterday
    r2 = filter_by_days(items, days=2, is_events=False, now=now)
    assert [x["message"] for x in r2] == ["today", "yesterday"]

    # days=7: within last 7 days (2026-11-17 to 2026-11-23)
    r7 = filter_by_days(items, days=7, is_events=False, now=now)
    assert [x["message"] for x in r7] == ["today", "yesterday", "5 days ago"]

    # days=30: up to 30 days (15.10.2026 is 39 days ago, excluded)
    r30 = filter_by_days(items, days=30, is_events=False, now=now)
    assert [x["message"] for x in r30] == ["today", "yesterday", "5 days ago", "13 days ago"]


def test_filter_by_days_events():
    now = date(2026, 11, 23)
    events = [
        {"job": "1", "created": "23.11.2026", "outcome": "submitted"},
        {"job": "2", "created": "22.11.2026", "outcome": "rejected"},
        {"job": "3", "created": None, "date": "2026-11-20", "outcome": "submitted"},
        {"job": "4", "created": "01.11.2026", "outcome": "failed"},
    ]

    r1 = filter_by_days(events, days=1, is_events=True, now=now)
    assert [e["job"] for e in r1] == ["1"]

    r2 = filter_by_days(events, days=2, is_events=True, now=now)
    assert [e["job"] for e in r2] == ["1", "2"]

    r7 = filter_by_days(events, days=7, is_events=True, now=now)
    assert [e["job"] for e in r7] == ["1", "2", "3"]

    r30 = filter_by_days(events, days=30, is_events=True, now=now)
    assert [e["job"] for e in r30] == ["1", "2", "3", "4"]


def test_filter_by_days_caps_at_30():
    now = date(2026, 11, 23)
    items = [
        {"createdDate": "23.11.2026", "message": "today"},
        {"createdDate": "20.10.2026", "message": "34 days ago"},
    ]
    # Even if user asks for 60 days, it is capped at 30 days
    r60 = filter_by_days(items, days=60, is_events=False, now=now)
    assert [x["message"] for x in r60] == ["today"]


def test_cmd_notifications_table_and_json(tmp_path, monkeypatch, capsys):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from icegate_client import cli
    from icegate_client.notifications import NotificationClient

    today_str = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d.%m.%Y")
    sample = [
        {"createdDate": today_str, "message": "Your bill of entry filing with job no. 101 dated 20261123 has been successfully submitted."},
        {"createdDate": "15.10.2020", "message": "Old notification"},
    ]
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    monkeypatch.setattr(cli.Context, "authenticate", lambda self: None)
    monkeypatch.setattr(NotificationClient, "list", lambda self, ntype: sample)

    # 1. Default table output with --days 2
    res = cli.main(["notifications", "--days", "2", "--state-dir", str(tmp_path / "state")])
    assert res == 0
    out = capsys.readouterr().out
    assert "Date" in out and "Message" in out
    assert today_str in out
    assert "Old notification" not in out

    # 2. --json flag
    res = cli.main(["notifications", "--days", "2", "--json", "--state-dir", str(tmp_path / "state")])
    assert res == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert len(data) == 1
    assert data[0]["createdDate"] == today_str

    # 3. --events with table
    res = cli.main(["notifications", "--events", "--days", "2", "--state-dir", str(tmp_path / "state")])
    assert res == 0
    out = capsys.readouterr().out
    assert "Date" in out and "Doc" in out and "Outcome" in out and "Job/No" in out
    assert "101" in out and "submitted" in out

    # 4. No records found
    monkeypatch.setattr(NotificationClient, "list", lambda self, ntype: [])
    res = cli.main(["notifications", "--days", "1", "--state-dir", str(tmp_path / "state")])
    assert res == 0
    out = capsys.readouterr().out
    assert "no records found" in out

    # 5. Filter by --sb vs --be
    monkeypatch.setattr(NotificationClient, "list", lambda self, ntype: sample)
    res_sb = cli.main(["notifications", "--events", "--sb", "--state-dir", str(tmp_path / "state")])
    assert res_sb == 0
    assert "no records found" in capsys.readouterr().out

    res_be = cli.main(["notifications", "--events", "--be", "--state-dir", str(tmp_path / "state")])
    assert res_be == 0
    out_be = capsys.readouterr().out
    assert "101" in out_be and "BE" in out_be


def test_filter_by_days_doc():
    now = date(2026, 11, 23)
    events = [
        {"job": "1", "created": "23.11.2026", "doc": "SB", "outcome": "submitted"},
        {"job": "2", "created": "23.11.2026", "doc": "BE", "outcome": "submitted"},
        {"job": "3", "created": "23.11.2026", "doc": None, "message": "CACHE validation failed", "outcome": "failed"},
    ]
    sb_only = filter_by_days(events, days=1, is_events=True, now=now, doc="SB")
    assert [e["job"] for e in sb_only] == ["1", "3"]

    be_only = filter_by_days(events, days=1, is_events=True, now=now, doc="BE")
    assert [e["job"] for e in be_only] == ["2"]

    # Verify that raw notifications containing the English word 'be' are NOT falsely tagged as BE
    raw_notices = [
        {"createdDate": "23.11.2026", "message": "Your shipping bill filing has been accepted and will be processed."},
        {"createdDate": "23.11.2026", "message": "Scheduled maintenance: portal will be offline tonight."},
        {"createdDate": "23.11.2026", "message": "Your bill of entry job 113 cachi acknowledgement is ready."},
    ]
    raw_be = filter_by_days(raw_notices, days=1, is_events=False, now=now, doc="BE")
    assert len(raw_be) == 1
    assert "bill of entry" in raw_be[0]["message"]


def test_cli_notifications_invalid_days_and_conflicting_docs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ICEGATE_USERNAME", "test")
    monkeypatch.setenv("ICEGATE_PASSWORD", "test")
    monkeypatch.setattr(cli.Context, "authenticate", lambda self: None)
    monkeypatch.setattr(NotificationClient, "list", lambda self, ntype: [])

    # --days 0 must fail
    res = cli.main(["notifications", "--days", "0", "--state-dir", str(tmp_path / "state")])
    assert res == 1
    assert "--days must be at least 1" in capsys.readouterr().err

    # conflicting --sb and --be must fail
    res = cli.main(["notifications", "--sb", "--be", "--state-dir", str(tmp_path / "state")])
    assert res == 1
    assert "cannot specify both --sb and --be" in capsys.readouterr().err





def test_bare_rejection_is_not_filed_and_a_rejection_with_codes_failed():
    bare = msg('"Your shipping bill filing with job no. 952 dated 07112026 has been rejected.')
    assert (bare["outcome"], bare["problem"], bare["errors"]) == ("rejected", "not_filed", [])
    bad = msg('"Your bill of entry filing with job no. 112 dated 2026-11-05 has been rejected. '
              'Error code: 349,708,,_MAIN,314_ITEMS\n-CBIC "')
    assert bad["problem"] == "failed" and bad["errors"] == ["349,708,,_MAIN", "314_ITEMS"]
    assert msg('"Your bill of entry filing with job no. 1 dated 05112026 has been rejected. Error code: 610_IGMS\n-CBIC "')["errors"] == ["610_IGMS"]
    assert msg("Your bill of entry filing with job no. 1 dated 20261105 has been successfully submitted.")["problem"] is None


def test_processing_error_asks_to_try_again_so_it_is_not_filed():
    event = msg("Dear Sir/Madam,\nThere was an error in processing your file with control no. 9, filing date 20261105, "
                "Receiver ID INHPI6, message ID CACHI01_A. Please try after some time.\nRegards,\nICEGATE Support Team")
    assert (event["outcome"], event["problem"], event["errors"]) == ("failed", "not_filed", [])


def test_field_errors_are_listed_one_per_field_and_header_failure_keeps_its_text():
    event = msg("Dear Sir/Madam,\nField validation failed for control no. 721, filing date 20261103, Receiver ID INTKD6, "
                "message ID CACHE1. Please use a valid input file.\n\n\nErrors: finalInvoiceModel[0].buyerAddress1: Buyer "
                "Address 1 should not be more than length 50 and should only contain alphabet, numeric, special character, "
                "finalSBModel[0].consigneeAddress1: Consignee Address 1 too long\n\nRegards,\nICEGATE Support Team")
    assert event["problem"] == "failed" and event["errors"] == [
        "finalInvoiceModel[0].buyerAddress1: Buyer Address 1 should not be more than length 50 and should only contain "
        "alphabet, numeric, special character",
        "finalSBModel[0].consigneeAddress1: Consignee Address 1 too long"]
    head = msg("Dear Sir/Madam,\nHeader validation failed for control no. 5, filing date 20261103, Receiver ID INMBD6, "
               "message ID CACHE1. Please use a valid input file.\nRegards,\nICEGATE Support Team")
    assert head["errors"] == ["Header validation failed for control no. 5, filing date 20261103, Receiver ID INMBD6, "
                              "message ID CACHE1. Please use a valid input file."]


def test_older_field_validation_wording_without_date_is_parsed():
    event = msg('Field validation failed for Job no 642 finalInvoiceModel[0].buyerAddress1: Buyer Address 1 is too long, '
                'finalSBModel[0].portOfLoading: invalid port Please correct input data in the file & upload again.')
    assert (event["job"], event["date"], event["problem"]) == ("642", None, "failed")
    assert event["errors"] == ["finalInvoiceModel[0].buyerAddress1: Buyer Address 1 is too long",
                               "finalSBModel[0].portOfLoading: invalid port"]
