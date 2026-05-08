"""Regression tests for OneDriveBackend.classify_error.

Background (bug O3): the original implementation classified any exception
whose class name contained the substring "Auth" or "Token" as AUTH. That
caught MsalUiRequiredError correctly but also misclassified hypothetical
transient errors like `TokenRateLimitError` — surfacing a scary
"re-authenticate" prompt for what should be an auto-retried rate limit.

The fix is an explicit allowlist of MSAL class names that DEFINITELY mean
"user must re-auth", plus an OAuth error-code check on the exception args
for broader MsalServiceError-shaped exceptions.
"""
from __future__ import annotations

import pytest
import requests

from claude_mirror.backends import ErrorClass
from claude_mirror.backends.onedrive import OneDriveBackend


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

@pytest.fixture
def backend(make_config) -> OneDriveBackend:
    """OneDriveBackend instance with a minimal in-memory Config.

    classify_error never touches the network or token cache, so we don't
    need to authenticate or stub MSAL — just construct the backend.
    """
    cfg = make_config(
        backend="onedrive",
        onedrive_client_id="test",
        onedrive_folder="/test",
    )
    return OneDriveBackend(cfg)


def _make_exc(name: str, *args) -> Exception:
    """Synthesise an exception class by `name` and instantiate it.

    Uses `type()` so the test does not need to import — or even have
    installed — the real MSAL exception classes; classify_error keys off
    the class name string, which is exactly what we want to exercise.
    """
    cls = type(name, (Exception,), {})
    return cls(*args)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_msal_ui_required_classified_as_auth(backend):
    """The canonical 'silent token failed → user must re-auth' exception."""
    exc = _make_exc("MsalUiRequiredError", "interaction required")
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_interaction_required_auth_error_classified_as_auth(backend):
    """MSAL Python's alternative name for the same condition is also AUTH."""
    exc = _make_exc("InteractionRequiredAuthError", "needs interactive flow")
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_token_rate_limit_not_classified_as_auth(backend):
    """A hypothetical transient rate-limit error must NOT trigger AUTH.

    Under the old substring rule this would have been AUTH simply because
    its name contains 'Token' — the exact misclassification this fix
    targets. With the allowlist, it falls through to UNKNOWN (or TRANSIENT
    if other branches catch it) but specifically MUST NOT be AUTH.
    """
    exc = _make_exc("TokenRateLimitError", "slow down")
    result = backend.classify_error(exc)
    assert result != ErrorClass.AUTH
    # In the current chain a bare exception with no requests/socket type
    # falls through to UNKNOWN; assert that explicitly so a future
    # behaviour change here is a deliberate decision.
    assert result == ErrorClass.UNKNOWN


def test_invalid_grant_in_args_classified_as_auth(backend):
    """A generic exception whose args mention 'invalid_grant' is AUTH —
    that's the OAuth signal for a dead refresh token."""
    exc = _make_exc(
        "MsalServiceError",
        "invalid_grant: The provided authorization grant has expired",
    )
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_aadsts50058_classified_as_auth(backend):
    """AADSTS50058 = silent sign-in attempted with no signed-in user → re-auth."""
    exc = _make_exc(
        "MsalServiceError",
        "AADSTS50058: Session information is not sufficient for single-sign-on",
    )
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_aadsts70008_classified_as_auth(backend):
    """AADSTS70008 = refresh token expired → re-auth."""
    exc = _make_exc("MsalServiceError", "AADSTS70008: refresh token has expired")
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_random_runtime_error_classified_as_unknown(backend):
    """A bare RuntimeError whose message doesn't match any auth heuristic
    is UNKNOWN — it must NOT be coerced into AUTH."""
    exc = RuntimeError("backend exploded")
    result = backend.classify_error(exc)
    assert result != ErrorClass.AUTH
    assert result == ErrorClass.UNKNOWN


def test_http_429_classified_as_rate_limit_global(backend):
    """A 429 from Microsoft Graph is an account-wide throttle signal, not a
    storage-quota exhaustion or a per-file transient blip. It must route
    through the shared backoff coordinator (RATE_LIMIT_GLOBAL) so every
    in-flight upload pauses on the same deadline rather than each
    retrying independently and compounding the rate-limit pressure."""
    response = requests.Response()
    response.status_code = 429
    exc = requests.exceptions.HTTPError(response=response)
    assert backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_http_500_still_transient(backend):
    """Regression guard: 5xx → TRANSIENT path is preserved."""
    response = requests.Response()
    response.status_code = 503
    exc = requests.exceptions.HTTPError(response=response)
    assert backend.classify_error(exc) == ErrorClass.TRANSIENT


def test_http_401_still_auth(backend):
    """Regression guard: 401 still maps to AUTH via the HTTPError branch,
    independent of the class-name allowlist."""
    response = requests.Response()
    response.status_code = 401
    exc = requests.exceptions.HTTPError(response=response)
    assert backend.classify_error(exc) == ErrorClass.AUTH
