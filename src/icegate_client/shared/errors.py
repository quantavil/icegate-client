"""Exception hierarchy.

Messages must never contain credentials, tokens, session ids, OTPs or
handoff URLs carrying ``DM_TICKET``. Callers pass text through
:func:`icegate_client.redact.scrub` before raising where that is possible.
"""

from __future__ import annotations


class IcegateError(Exception):
    """Base error for all expected failures."""


class ConfigError(IcegateError):
    """Missing or unsafe configuration."""


class AuthError(IcegateError):
    """Login or authentication was rejected."""


class TokenExpired(AuthError):
    """The dashboard token expired and cannot be refreshed.

    ICEGATE refuses ``refresh-token`` once the old token has expired, so the
    only recovery is a new ``ext-login``.
    """



class HandoffError(IcegateError):
    """The dashboard to eSANCHIT handoff did not produce a usable session."""


class StagingError(IcegateError):
    """eSANCHIT rejected or did not report a staged upload."""


class PreflightError(IcegateError):
    """A local file failed validation before any network call."""


class TransientError(IcegateError):
    """A network error that is worth retrying."""


class SubmitError(IcegateError):
    """Submission was refused locally or its outcome is unknown."""


class SubmitBusy(SubmitError):
    """Another submission holds the state lock."""


class DuplicateSubmit(SubmitError):
    """A receipt for this exact plan already exists."""


class SubmitAborted(SubmitError):
    """The staged rows did not match the plan, so submit was not attempted."""

