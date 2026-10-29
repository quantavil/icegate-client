"""Browser-free ICEGATE authentication and token lifecycle.

Protocol, as observed on 2026-10-28:

* ``POST /identity/ext-login`` returns ``sessionId`` and a 10-minute ``token``.
* Authenticated calls send ``Authorization: <token>``, ``session_id`` and
  ``channel: browser``.
* ``POST /identity/refresh-token`` issues a new 10-minute token, but only
  while the current token is still valid. After expiry it returns HTTP 409
  ``auth error..!!!`` and the session must be created again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests

from .config import Settings
from .crypto import encrypt_password
from .errors import AuthError, TokenExpired, TransientError
from .journal import Journal
from .redact import remember, scrub
from .tls import build_ca_bundle

EXPIRY_FORMATS = ("%Y.%m.%d %I:%M:%S %p", "%Y.%m.%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")


def parse_expiry(value: Any) -> datetime | None:
    """Parse ``tokenExpiresIn`` into a naive local datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(time.time() + float(value))
    text = str(value).strip()
    for fmt in EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _error_text(body: Any) -> str:
    if isinstance(body, dict):
        for key in ("errors", "error", "message"):
            value = body.get(key)
            if isinstance(value, list) and value:
                return scrub(str(value[0]))[:300]
            if isinstance(value, str) and value:
                return scrub(value)[:300]
    return ""


@dataclass
class ProbeResult:
    ok: bool
    status: int | None
    service_count: int | None = None
    error: str = ""

    @property
    def expired(self) -> bool:
        return self.status == 409


class IcegateSession:
    def __init__(self, settings: Settings, journal: Journal) -> None:
        self.settings = settings
        self.journal = journal
        self.verify = build_ca_bundle(settings.state_dir)
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": settings.base_url,
                "Referer": settings.base_url + "/",
            }
        )
        self.token: str | None = None
        self.session_id: str | None = None
        self.expires_at: datetime | None = None
        self.role_id: int | None = None
        self.role_name: str | None = None
        self.user_type: str | None = None

    # ------------------------------------------------------------------ http
    def _send(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        data: Any = None,
        files: Any = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        allow_redirects: bool = False,
    ) -> requests.Response:
        return self.http.request(
            method,
            url,
            json=json_body,
            data=data,
            files=files,
            headers=headers or {},
            timeout=timeout or self.settings.timeout,
            allow_redirects=allow_redirects,
            verify=str(self.verify),
        )

    def send(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send with one retry for idempotent requests only.

        POSTs are never retried automatically: a retried login could create
        another session and a retried upload could duplicate a document.
        """
        attempts = 2 if method.upper() in {"GET", "HEAD"} else 1
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self._send(method, url, **kwargs)
            except requests.RequestException as exc:
                last = exc
                self.journal.add(
                    "request_error",
                    method=method,
                    host=_host(url),
                    path=_path(url),
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                if attempt < attempts:
                    time.sleep(1.5)
        raise TransientError(
            f"{method} {_host(url)}{_path(url)} failed: {type(last).__name__}"
        )

    # ---------------------------------------------------------------- helpers
    def url(self, path: str) -> str:
        return self.settings.base_url.rstrip("/") + path

    def auth_headers(self) -> dict[str, str]:
        headers = {"channel": "browser"}
        if self.token:
            headers["Authorization"] = self.token
        if self.session_id:
            headers["session_id"] = self.session_id
        return headers

    def seconds_left(self) -> float | None:
        if self.expires_at is None:
            return None
        return (self.expires_at - datetime.now()).total_seconds()

    # ------------------------------------------------------------------ login
    def bootstrap(self) -> None:
        response = self.send("GET", self.url("/"), timeout=30)
        self.journal.add(
            "bootstrap",
            status=response.status_code,
            cookie_names=sorted(c.name for c in self.http.cookies),
        )

    def login(self) -> dict[str, Any]:
        response = self.send(
            "POST",
            self.url("/identity/ext-login"),
            json_body={
                "icegateId": self.settings.icegate_id,
                "password": encrypt_password(self.settings.password),
                "usertype": "external",
            },
            timeout=60,
        )
        body = _json(response)
        if response.status_code != 200:
            raise AuthError(
                f"login failed with HTTP {response.status_code}"
                + (f": {_error_text(body)}" if _error_text(body) else "")
            )

        token = body.get("token") if isinstance(body, dict) else None
        session_id = body.get("sessionId") if isinstance(body, dict) else None
        if not token or not session_id:
            raise AuthError(
                "login response did not include a token and session id"
                + (f": {_error_text(body)}" if _error_text(body) else "")
            )

        roles = ((body.get("approvedRoles") or {}).get("userRoles") or [])
        if not roles:
            raise AuthError("account has no approved roles")

        role = roles[0]
        self.token = token
        self.session_id = session_id
        self.expires_at = parse_expiry(body.get("tokenExpiresIn"))
        self.role_id = role.get("roleId")
        self.role_name = role.get("roleName")
        self.user_type = (body.get("userType") or "ORGANIZATION").lower()
        remember(token, session_id)

        self.journal.add(
            "login",
            status=response.status_code,
            role_id=self.role_id,
            role_name=self.role_name,
            user_type=self.user_type,
            token_expires=body.get("tokenExpiresIn"),
            seconds_left=round(self.seconds_left() or 0),
            email_selected=body.get("isEmailSelected"),
            last_login=body.get("lastLoginAt"),
            login_count=body.get("loginCount"),
            response_keys=sorted(body.keys()),
        )
        return body

    # ------------------------------------------------------------------ probe
    def probe(self) -> ProbeResult:
        if not self.token or not self.session_id:
            raise AuthError("not logged in")
        response = self.send(
            "POST",
            self.url("/dashboard/services/get"),
            json_body={"icegateId": self.settings.icegate_id, "roleId": self.role_id},
            headers=self.auth_headers(),
        )
        body = _json(response)
        count = None
        if isinstance(body, list):
            count = len(body)
        elif isinstance(body, dict):
            services = body.get("services")
            if isinstance(services, list):
                count = len(services)
            elif isinstance(body.get("data"), list):
                count = len(body["data"])
        result = ProbeResult(
            ok=response.status_code == 200 and bool(count),
            status=response.status_code,
            service_count=count,
            error=_error_text(body),
        )
        self.journal.add(
            "dashboard_probe",
            status=result.status,
            ok=result.ok,
            service_count=result.service_count,
            error=result.error or None,
        )
        return result

    # ---------------------------------------------------------------- refresh
    def refresh(self) -> None:
        if not self.token or not self.session_id:
            raise AuthError("not logged in")
        left = self.seconds_left()
        if left is not None and left <= 0:
            raise TokenExpired(
                "dashboard token already expired; ICEGATE rejects refresh after "
                "expiry, so run a fresh login"
            )
        response = self.send(
            "POST",
            self.url("/identity/refresh-token"),
            json_body={
                "icegateId": self.settings.icegate_id,
                "sessionId": self.session_id,
            },
            headers={"Authorization": self.token},
        )
        body = _json(response)
        new_token = body.get("token") if isinstance(body, dict) else None
        if response.status_code == 409 or not new_token:
            self.journal.add(
                "refresh_failed",
                status=response.status_code,
                error=_error_text(body) or None,
            )
            if response.status_code == 409:
                raise TokenExpired("refresh rejected: token expired")
            raise AuthError(
                f"refresh failed with HTTP {response.status_code}"
                + (f": {_error_text(body)}" if _error_text(body) else "")
            )
        self.token = new_token
        self.expires_at = parse_expiry(body.get("tokenExpiresIn"))
        remember(new_token)
        self.journal.add(
            "refresh",
            status=response.status_code,
            token_expires=body.get("tokenExpiresIn"),
            seconds_left=round(self.seconds_left() or 0),
        )

    def refresh_if_expiring(self, margin: float = 120.0) -> bool:
        left = self.seconds_left()
        if left is None or left > margin:
            return False
        self.refresh()
        return True

    # ------------------------------------------------------------ top level
    def authenticate(self) -> None:
        """Log in and confirm the dashboard accepts the session."""
        self.bootstrap()
        self.login()
        probe = self.probe()
        if not probe.ok:
            raise AuthError(f"dashboard rejected the session (HTTP {probe.status})")
        self.journal.add("authenticated")

    # --------------------------------------------------------------- eSANCHIT
    def esanchit_ticket(self) -> str:
        if not self.token:
            raise AuthError("not logged in")
        response = self.send(
            "POST",
            self.url("/esanchit/esanchitDynamicUrl"),
            json_body={
                "icegateId": self.settings.icegate_id,
                "roleId": self.role_id,
                "userType": self.user_type,
            },
            headers=self.auth_headers(),
            timeout=60,
        )
        body = _json(response)
        url = body.get("url") if isinstance(body, dict) else None
        if response.status_code != 200 or not url:
            raise AuthError(
                f"eSANCHIT handoff failed with HTTP {response.status_code}"
                + (f": {_error_text(body)}" if _error_text(body) else "")
            )
        from urllib.parse import urlsplit

        from .redact import query_keys

        parts = urlsplit(url)
        self.journal.add(
            "handoff_ticket",
            status=response.status_code,
            host=parts.hostname,
            path=parts.path,
            query_keys=query_keys(url),
            url_length=len(url),
        )
        return url


def _json(response: requests.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"data": body}


def _host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def _path(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path or "/"
