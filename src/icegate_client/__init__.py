"""Browser-free ICEGATE client.

The package automates authentication, token refresh and the dashboard to
eSANCHIT handoff, submits already-signed PDFs, and uploads signed electronic
filing messages after explicit per-plan confirmation.
"""

from .shared.errors import (
    AuthError,
    ConfigError,
    DuplicateSubmit,
    HandoffError,
    IcegateError,
    PreflightError,
    StagingError,
    SubmitAborted,
    SubmitBusy,
    SubmitError,
    TokenExpired,
    TransientError,
)

__all__ = [
    "AuthError",
    "ConfigError",
    "DuplicateSubmit",
    "HandoffError",
    "IcegateError",
    "PreflightError",
    "StagingError",
    "SubmitAborted",
    "SubmitBusy",
    "SubmitError",
    "TokenExpired",
    "TransientError",
]

__version__ = "0.2.0"
