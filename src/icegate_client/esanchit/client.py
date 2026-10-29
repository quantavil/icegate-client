"""eSANCHIT handoff, uploads and the single POST gate.

The handoff ticket is a bearer credential in a query string, so it is never
logged. Redirects are followed manually because the portal answers with plain
``http://`` URLs while sending HSTS; every hop to an ``icegate.gov.in`` host is
upgraded to HTTPS before the request is made.

Posting is allowlisted per call site. The staging path can only ever reach
``uploadDocs``; ``submitDocs`` lives in ``submission.py`` with its own
allowlist and its own confirmation gate.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..shared.errors import HandoffError, StagingError
from ..shared.journal import Journal
from ..shared.redact import query_keys
from ..shared.session import IcegateSession
from .page import HomePage

# The staging client may only ever POST to this action.
FORM_ACTION_ALLOWLIST = frozenset({"uploadDocs"})

MAX_HOPS = 6
HOME_PATH = "/esanchit/home"


def assert_action_allowed(
    action: str,
    allowlist: frozenset[str] = FORM_ACTION_ALLOWLIST,
) -> str:
    """Return a normalised action name or raise.

    ``action`` may be a bare name (``uploadDocs``) or a path
    (``/esanchit/uploadDocs``). Anything outside ``allowlist`` is refused, so
    a call site has to opt in to a capability explicitly.
    """
    cleaned = action.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    name = cleaned.rsplit("/", 1)[-1]
    if not name:
        raise StagingError("refusing to POST to an empty action")
    if name not in allowlist:
        raise StagingError(f"refusing to POST to unexpected action {name!r}")
    return name


class EsanchitClient:
    def __init__(self, session: IcegateSession, journal: Journal) -> None:
        self.session = session
        self.journal = journal
        self.settings = session.settings
        self.base = self.settings.esanchit_base.rstrip("/")
        self.home_url = self.base + HOME_PATH

    # -------------------------------------------------------------- handoff
    def open_from_ticket(self, ticket_url: str) -> HomePage:
        current = ticket_url
        seen: list[str] = []
        for hop in range(MAX_HOPS):
            parts = urlsplit(current)
            host = (parts.hostname or "").lower()
            if not (host == "icegate.gov.in" or host.endswith(".icegate.gov.in")):
                raise HandoffError(f"handoff left the ICEGATE domain at hop {hop}")
            if parts.scheme == "http":
                current = parts._replace(scheme="https").geturl()
                parts = urlsplit(current)

            response = self.session.send(
                "GET",
                current,
                timeout=60,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": self.base + "/",
                },
            )
            location = response.headers.get("Location") or ""
            seen.append(f"{response.status_code} {parts.path}")
            self.journal.add(
                "esanchit_hop",
                hop=hop,
                status=response.status_code,
                host=parts.hostname,
                path=parts.path,
                query_keys=query_keys(current),
                location_path=urlsplit(urljoin(current, location)).path if location else None,
                bytes=len(response.content),
            )
            if response.status_code in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                continue
            if response.status_code != 200:
                raise HandoffError(
                    f"eSANCHIT returned HTTP {response.status_code} after "
                    f"{len(seen)} hop(s)"
                )
            page = HomePage.parse(response.text)
            if "Upload Document Details" not in response.text:
                raise HandoffError("eSANCHIT did not return the upload page")
            self.journal.add(
                "esanchit_open",
                hops=len(seen),
                chain=seen,
                rows=len(page.rows),
                limits=page.describe_limits(),
                tab_id_present=bool(page.tab_id),
            )
            return page
        raise HandoffError(f"eSANCHIT redirect chain exceeded {MAX_HOPS} hops")

    def home(self) -> HomePage:
        response = self.session.send(
            "GET",
            self.home_url,
            timeout=60,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": self.base + "/",
            },
        )
        if response.status_code != 200:
            raise HandoffError(
                f"eSANCHIT home returned HTTP {response.status_code}; the session "
                "may have idled out, run the handoff again"
            )
        page = HomePage.parse(response.text)
        if "Upload Document Details" not in response.text:
            raise HandoffError("eSANCHIT home did not return the upload page")
        self.journal.add("esanchit_home", rows=len(page.rows), limits=page.describe_limits())
        return page

    # --------------------------------------------------------------- upload
    def stage(
        self, paths: list[Path], *, page: HomePage | None = None,
        file_contents: dict[Path, bytes] | None = None,
    ) -> HomePage:
        page = page or self.home()
        if not page.tab_id:
            raise StagingError("upload form has no tabId; refusing to upload")

        files = [
            ("docs", (path.name, file_contents[path] if file_contents is not None
                      else path.read_bytes(), "application/pdf")) for path in paths
        ]
        data = {
            "tabId": page.tab_id,
            "uploadForm": "Upload",
            "finalSubmit": "",
        }
        response = self.post_form("uploadDocs", data=data, files=files)

        result = HomePage.parse(response.text)
        self.journal.add(
            "stage_response",
            status=response.status_code,
            bytes=len(response.content),
            rows=len(result.rows),
            has_error=bool(result.errors),
            tab_id_present=bool(result.tab_id),
        )
        if response.status_code != 200:
            raise StagingError(f"upload returned HTTP {response.status_code}")
        if result.errors:
            raise StagingError("; ".join(result.errors)[:500])
        return result

    # ----------------------------------------------------------------- posts
    def post_form(
        self,
        action: str,
        *,
        data=None,
        files=None,
        allowlist: frozenset[str] = FORM_ACTION_ALLOWLIST,
    ):
        """POST to an allowlisted action and follow redirects manually."""
        name = assert_action_allowed(action, allowlist)
        url = urljoin(self.home_url, name)
        response = self.session.send(
            "POST",
            url,
            data=data,
            files=files,
            timeout=max(self.settings.timeout, 120),
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": self.base,
                "Referer": self.home_url,
                "Upgrade-Insecure-Requests": "1",
            },
        )
        response = self._follow(response, url)
        self.journal.add(
            "form_post",
            action=name,
            status=response.status_code,
            bytes=len(response.content),
        )
        return response

    def _follow(self, response, url: str):
        """Follow redirects with the same HTTPS upgrade and host checks.

        A redirect after a POST is treated as a GET (what a browser does for
        301/302/303). A 307/308 would require replaying the multipart body, so
        it is refused instead of risking a duplicate upload.
        """
        for _ in range(MAX_HOPS):
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location") or ""
            if not location:
                return response
            target = urljoin(url, location)
            parts = urlsplit(target)
            host = (parts.hostname or "").lower()
            if not (host == "icegate.gov.in" or host.endswith(".icegate.gov.in")):
                raise StagingError("redirect left the ICEGATE domain")
            if parts.scheme == "http":
                target = parts._replace(scheme="https").geturl()
                parts = urlsplit(target)
            if response.status_code in (307, 308):
                raise StagingError(
                    "eSANCHIT asked to replay the upload body; refusing to avoid a "
                    "duplicate. Check the portal before trying again."
                )
            response = self.session.send(
                "GET",
                target,
                timeout=60,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": url,
                },
            )
            self.journal.add(
                "form_redirect",
                status=response.status_code,
                host=parts.hostname,
                path=parts.path,
            )
            url = target
        raise StagingError(f"form redirect chain exceeded {MAX_HOPS} hops")
