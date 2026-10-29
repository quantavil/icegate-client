from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import FakeResponse, ScriptedSessionMixin

from icegate_client.shared.errors import AuthError, TokenExpired
from icegate_client.shared.redact import scrub
from icegate_client.shared.session import IcegateSession, parse_expiry

TOKEN = "T" * 328
SESSION_ID = "S" * 32

LOGIN_BODY = {
    "token": TOKEN,
    "sessionId": SESSION_ID,
    "tokenExpiresIn": "2099.01.01 09:30:05 PM",
    "approvedRoles": {"userRoles": [{"roleId": 3, "roleName": "Customs Broker"}]},
    "userType": "ORGANIZATION",
    "email": "broker@example.test",
    "mobile": "9999999999",
    "isEmailSelected": True,
    "lastLoginAt": "2026.10.28 08:30:22 PM",
    "loginCount": 7,
}


class FakeSession(ScriptedSessionMixin, IcegateSession):
    pass


def services(count=3):
    return [{"serviceId": index} for index in range(count)]


def test_parse_expiry_formats():
    assert parse_expiry("2026.10.28 09:30:05 PM") == datetime(2026, 10, 28, 21, 30, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert parse_expiry("2026.10.28 09:30:05") == datetime(2026, 10, 28, 9, 30, 5, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert parse_expiry(None) is None
    assert parse_expiry("not a date") is None


def test_parse_expiry_accepts_seconds():
    value = parse_expiry(600)
    assert value is not None
    assert 590 < (value - datetime.now(value.tzinfo)).total_seconds() <= 600


def test_login_populates_session(settings, journal):
    session = FakeSession(settings, journal)
    session._install([FakeResponse(200, LOGIN_BODY)])
    session.login()

    assert session.token == TOKEN
    assert session.session_id == SESSION_ID
    assert session.role_id == 3
    assert session.role_name == "Customs Broker"
    assert session.user_type == "organization"
    assert session.expires_at == datetime(2099, 1, 1, 21, 30, 5, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_token_is_redacted_after_login(settings, journal):
    session = FakeSession(settings, journal)
    session._install([FakeResponse(200, LOGIN_BODY)])
    session.login()
    assert TOKEN not in scrub(f"value {TOKEN}")
    assert SESSION_ID not in scrub(f"value {SESSION_ID}")


def test_login_without_token_fails(settings, journal):
    session = FakeSession(settings, journal)
    session._install([FakeResponse(200, {"errors": ["bad password"]})])
    with pytest.raises(AuthError):
        session.login()


def test_authenticate_without_otp(settings, journal):
    session = FakeSession(settings, journal)
    session._install(
        [
            FakeResponse(200, text="<html>"),
            FakeResponse(200, LOGIN_BODY),
            FakeResponse(200, services(9)),
        ]
    )
    session.authenticate()
    assert [call[0] for call in session.calls] == ["GET", "POST", "POST"]
    assert session.calls[2][1].endswith("/dashboard/services/get")


def test_rejected_dashboard_stops_without_additional_requests(settings, journal):
    session = FakeSession(settings, journal)
    session._install([
        FakeResponse(200, text="<html>"),
        FakeResponse(200, LOGIN_BODY),
        FakeResponse(403, {"errors": ["access rejected"]}),
    ])
    with pytest.raises(AuthError, match="dashboard rejected"):
        session.authenticate()
    assert len(session.calls) == 3


def test_refresh_before_expiry(settings, journal):
    session = FakeSession(settings, journal)
    session._install(
        [FakeResponse(200, {"token": "N" * 328, "tokenExpiresIn": "2099.01.01 10:00:00 PM"})]
    )
    session.token = TOKEN
    session.session_id = SESSION_ID
    session.expires_at = datetime.now() + timedelta(minutes=5)
    session.refresh()
    assert session.token == "N" * 328


def test_refresh_after_expiry_makes_no_request(settings, journal):
    session = FakeSession(settings, journal)
    session._install([])
    session.token = TOKEN
    session.session_id = SESSION_ID
    session.expires_at = datetime.now() - timedelta(seconds=1)
    with pytest.raises(TokenExpired):
        session.refresh()
    assert session.calls == []


def test_refresh_rejected_with_409(settings, journal):
    session = FakeSession(settings, journal)
    session._install([FakeResponse(409, {"errors": ["auth error..!!!"]})])
    session.token = TOKEN
    session.session_id = SESSION_ID
    session.expires_at = datetime.now() + timedelta(minutes=5)
    with pytest.raises(TokenExpired):
        session.refresh()


def test_handoff_ticket_never_journals_query(settings, journal):
    session = FakeSession(settings, journal)
    ticket = "https://foservices.icegate.gov.in/esanchit?token=DM_TICKET=SUPERSECRETVALUE"
    session._install([FakeResponse(200, {"url": ticket})])
    session.token = TOKEN
    session.session_id = SESSION_ID
    session.role_id = 3
    session.user_type = "organization"
    assert session.esanchit_ticket() == ticket
    written = journal.path.read_text(encoding="utf-8")
    assert "SUPERSECRETVALUE" not in written
    assert "token" in written


def test_probe_counts_list_payload(settings, journal):
    session = FakeSession(settings, journal)
    session._install([FakeResponse(200, services(9))])
    session.token = TOKEN
    session.session_id = SESSION_ID
    probe = session.probe()
    assert probe.ok
    assert probe.service_count == 9


def test_probe_409_is_expiry(settings, journal):
    session = FakeSession(settings, journal)
    session._install([FakeResponse(409, {"errors": ["auth error..!!!"]})])
    session.token = TOKEN
    session.session_id = SESSION_ID
    probe = session.probe()
    assert not probe.ok
    assert probe.expired


def test_portal_expiry_is_india_time_independent_of_host(settings, journal, monkeypatch):
    import time
    from zoneinfo import ZoneInfo
    monkeypatch.setenv('TZ', 'UTC')
    time.tzset()
    try:
        expiry = parse_expiry('2026.10.28 09:30:05 PM')
        assert expiry.tzinfo == ZoneInfo('Asia/Kolkata')
        assert expiry.astimezone(UTC).hour == 16
    finally:
        monkeypatch.delenv('TZ')
        time.tzset()
