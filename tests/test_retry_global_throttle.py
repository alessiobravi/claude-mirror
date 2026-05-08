"""Tests for the shared backoff coordinator that handles RATE_LIMIT_GLOBAL.

Background: when a backend (Drive, Dropbox, OneDrive, WebDAV) starts
returning 429 Too Many Requests, the historical per-file TRANSIENT retry
treated each failure as N independent transient errors. Each file
independently retried 3 times, the retries arrived in clustered bursts,
and the user saw a flurry of red warnings instead of a calm "we're being
throttled, slowing down for 30s".

The fix is `BackoffCoordinator` (in `claude_mirror/retry.py`): when ANY
upload reports `RATE_LIMIT_GLOBAL`, every in-flight upload pauses on the
same deadline. These tests cover:
  1. Coordinator semantics in isolation (waiting, signalling, escalation).
  2. Server-supplied Retry-After honouring.
  3. Per-backend `classify_error` 429 detection (Drive, Dropbox, OneDrive,
     WebDAV) returning RATE_LIMIT_GLOBAL.
  4. Per-backend per-file errors NOT classified as RATE_LIMIT_GLOBAL.
  5. SFTP NEVER returning RATE_LIMIT_GLOBAL.
  6. Engine integration: a mocked backend that raises 429 once then
     succeeds completes after the throttle clears.
  7. Calm-message smoke: ONE "Pausing 30s..." line and ONE "Throttle
     cleared" line, not N transient warnings.
  8. Non-rate-limit errors (TRANSIENT/AUTH/QUOTA/PERMISSION/FILE_REJECTED)
     are unaffected.

The clock is NEVER `time.sleep`-patched globally (that violates the
project's `feedback_no_global_time_sleep_patch.md` rule). Instead, tests
rebind the module-level `_now` / `_sleep` wrappers in
`claude_mirror.retry` to a controlled fake clock — only the
coordinator's view of time changes; every other caller of `time.sleep`
(if any) sees the real stdlib.
"""
from __future__ import annotations

import threading
from typing import List, Optional
from unittest.mock import MagicMock

import pytest
import requests

from claude_mirror import retry as retry_mod
from claude_mirror.backends import BackendError, ErrorClass


# ─── Fake clock helpers ────────────────────────────────────────────────────────

class FakeClock:
    """Manually-advanceable monotonic-clock substitute.

    Tests bind `retry_mod._now` to `clock.now` and `retry_mod._sleep` to
    `clock.sleep`. `clock.advance(seconds)` jumps the simulated clock
    forward and wakes every `cv.wait` blocked on a timeout — see
    `_install_fake_clock` for the wiring.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self._t = float(start)
        self._lock = threading.Lock()
        # Every Condition variable a coordinator might be waiting on; we
        # call `notify_all` on each when the clock advances so blocked
        # waiters re-check the deadline rather than sleeping for real.
        self._registered_cvs: List[threading.Condition] = []

    def now(self) -> float:
        with self._lock:
            return self._t

    def sleep(self, seconds: float) -> None:
        # Production code only calls `_sleep` from the (currently unused)
        # convenience helper; the coordinator uses `cv.wait(timeout=...)`,
        # which we intercept separately. Make sleep a no-op so tests
        # never actually block on the wall clock.
        return

    def register_cv(self, cv: threading.Condition) -> None:
        self._registered_cvs.append(cv)

    def advance(self, seconds: float) -> None:
        """Jump the simulated clock forward and wake every registered cv."""
        with self._lock:
            self._t += float(seconds)
        # Wake each registered cv. Each waiter's `cv.wait(timeout=...)`
        # returns; the waiter then re-checks `_now() - deadline` and
        # either exits the loop (deadline elapsed) or waits again
        # (deadline still in the future, e.g. after an extension).
        for cv in self._registered_cvs:
            with cv:
                cv.notify_all()


@pytest.fixture
def fake_clock(monkeypatch):
    """Install a fake monotonic clock for the retry module.

    Rebinds `retry_mod._now` and `retry_mod._sleep` so the coordinator
    sees a manually-advanceable clock. The real `time.monotonic` /
    `time.sleep` are untouched — every other caller in the process is
    unaffected, in line with the project's rule against global stdlib
    patches.
    """
    clock = FakeClock()
    monkeypatch.setattr(retry_mod, "_now", clock.now)
    monkeypatch.setattr(retry_mod, "_sleep", clock.sleep)
    return clock


def _make_coordinator(
    fake_clock: FakeClock,
    *,
    max_wait_seconds: float = 600.0,
    on_start=None,
    on_clear=None,
):
    """Build a coordinator and register its condition variable with the
    fake clock so `clock.advance()` wakes blocked waiters."""
    coord = retry_mod.BackoffCoordinator(
        max_wait_seconds=max_wait_seconds,
        on_throttle_start=on_start,
        on_throttle_clear=on_clear,
    )
    fake_clock.register_cv(coord._cv)
    return coord


# ─── Coordinator behaviour ─────────────────────────────────────────────────────

def test_wait_if_throttled_returns_immediately_when_no_signal(fake_clock):
    """Baseline: a coordinator that has never seen `signal_rate_limit`
    must let `wait_if_throttled` return immediately with no blocking."""
    coord = _make_coordinator(fake_clock)
    assert coord.is_throttled is False
    # Should not block — ran inline.
    coord.wait_if_throttled()
    assert coord.is_throttled is False


def test_signal_rate_limit_then_wait_blocks_until_deadline(fake_clock):
    """`signal_rate_limit(30)` followed by `wait_if_throttled` blocks
    until the simulated clock advances past the 30s deadline."""
    coord = _make_coordinator(fake_clock)
    coord.signal_rate_limit(retry_after_seconds=30.0)
    assert coord.is_throttled is True

    started = threading.Event()
    finished = threading.Event()

    def _waiter():
        started.set()
        coord.wait_if_throttled()
        finished.set()

    t = threading.Thread(target=_waiter, daemon=True)
    t.start()
    started.wait(timeout=1.0)

    # Before advancing, the waiter must still be blocked. Use a tiny
    # real wait to give the thread a chance to enter `cv.wait`.
    finished.wait(timeout=0.05)
    assert not finished.is_set()

    # Advance past the deadline.
    fake_clock.advance(31.0)
    finished.wait(timeout=1.0)
    assert finished.is_set()
    assert coord.is_throttled is False


def test_multiple_workers_unblock_at_same_deadline(fake_clock):
    """All waiters share one deadline: when the clock crosses it, every
    waiter wakes (one observes the clear, the rest see is_throttled
    False on their next check)."""
    coord = _make_coordinator(fake_clock)
    coord.signal_rate_limit(retry_after_seconds=30.0)

    n_workers = 8
    started = threading.Barrier(n_workers + 1)
    finished_events = [threading.Event() for _ in range(n_workers)]

    def _waiter(idx: int):
        started.wait(timeout=1.0)
        coord.wait_if_throttled()
        finished_events[idx].set()

    threads = [
        threading.Thread(target=_waiter, args=(i,), daemon=True)
        for i in range(n_workers)
    ]
    for t in threads:
        t.start()
    started.wait(timeout=1.0)
    # Give threads a moment to enter cv.wait().
    for ev in finished_events:
        ev.wait(timeout=0.02)
    assert not any(ev.is_set() for ev in finished_events)

    fake_clock.advance(31.0)
    for ev in finished_events:
        ev.wait(timeout=1.0)
    assert all(ev.is_set() for ev in finished_events)
    assert coord.is_throttled is False


def test_server_supplied_retry_after_honoured(fake_clock):
    """When `retry_after_seconds=45`, the deadline is exactly 45s out."""
    coord = _make_coordinator(fake_clock)
    t0 = fake_clock.now()
    coord.signal_rate_limit(retry_after_seconds=45.0)

    # Advance 44s — still throttled.
    fake_clock.advance(44.0)
    assert coord.is_throttled is True
    # Cross the threshold by 1s — wait_if_throttled should clear and return.
    fake_clock.advance(2.0)
    coord.wait_if_throttled()
    assert coord.is_throttled is False


def test_default_window_when_no_retry_after_supplied(fake_clock):
    """No server value supplied → fall back to DEFAULT_INITIAL_BACKOFF_SECONDS (30s)."""
    coord = _make_coordinator(fake_clock)
    coord.signal_rate_limit(retry_after_seconds=None)
    assert coord.is_throttled is True
    # Advance 29s — still throttled.
    fake_clock.advance(29.0)
    assert coord.is_throttled is True
    fake_clock.advance(2.0)
    assert coord.is_throttled is False


def test_escalation_extends_deadline(fake_clock):
    """Two `signal_rate_limit()` calls in the same throttled window
    extend the deadline. With no server value supplied:
       1st signal → 30s window
       2nd signal (within window) → 30 * 1.5 = 45s window from now."""
    coord = _make_coordinator(fake_clock)
    coord.signal_rate_limit()  # 30s default
    fake_clock.advance(10.0)   # 20s left
    coord.signal_rate_limit()  # extension: 30 * 1.5 = 45s NEW window
    # New window is 45s from advanced-to time — total elapsed from t0
    # is 10s, new deadline is 10 + 45 = 55s after t0.
    fake_clock.advance(40.0)   # now 50s after t0 — still throttled (45s window starts at 10s)
    assert coord.is_throttled is True
    fake_clock.advance(10.0)   # now 60s after t0 — past the 45s window
    assert coord.is_throttled is False


def test_escalation_capped_at_max_wait_seconds(fake_clock):
    """No matter how many escalations fire, the window cannot exceed
    `max_wait_seconds`. With cap=60 and default initial=30s, even
    repeated escalations stay <=60s."""
    coord = _make_coordinator(fake_clock, max_wait_seconds=60.0)
    coord.signal_rate_limit()           # 30s
    coord.signal_rate_limit()           # 30 * 1.5 = 45s (still under cap)
    coord.signal_rate_limit()           # 45 * 1.5 = 67.5 → capped at 60s
    coord.signal_rate_limit()           # 60 * 1.5 = 90 → still capped at 60s
    assert coord.is_throttled is True
    # Past 60s, even after multiple escalations.
    fake_clock.advance(61.0)
    assert coord.is_throttled is False


def test_default_max_wait_is_600_seconds(fake_clock):
    """Default cap is 600s (10 minutes) per the spec."""
    coord = _make_coordinator(fake_clock)
    # Force a runaway escalation by passing a huge server-supplied value.
    coord.signal_rate_limit(retry_after_seconds=10_000.0)
    # The window must be capped at 600s.
    fake_clock.advance(599.0)
    assert coord.is_throttled is True
    fake_clock.advance(2.0)
    assert coord.is_throttled is False


def test_throttle_clears_after_deadline_with_no_further_signals(fake_clock):
    """Once the deadline elapses with no further `signal_rate_limit`
    calls, the coordinator's internal state resets and is_throttled
    flips back to False."""
    coord = _make_coordinator(fake_clock)
    coord.signal_rate_limit(retry_after_seconds=10.0)
    assert coord.is_throttled is True
    fake_clock.advance(11.0)
    coord.wait_if_throttled()  # observes the elapsed deadline, clears state
    assert coord.is_throttled is False
    # A subsequent fresh signal starts a NEW window with the default
    # initial backoff, not an escalation off the old one.
    coord.signal_rate_limit()
    fake_clock.advance(29.0)
    assert coord.is_throttled is True
    fake_clock.advance(2.0)
    assert coord.is_throttled is False


def test_negative_retry_after_falls_back_to_default(fake_clock):
    """A bogus negative server value is treated as 'no value supplied'."""
    coord = _make_coordinator(fake_clock)
    coord.signal_rate_limit(retry_after_seconds=-5.0)
    # Should still be in a default 30s window, not 0s and not error.
    assert coord.is_throttled is True
    fake_clock.advance(29.0)
    assert coord.is_throttled is True
    fake_clock.advance(2.0)
    assert coord.is_throttled is False


# ─── Calm-message callbacks ────────────────────────────────────────────────────

def test_on_throttle_start_fires_once_per_window(fake_clock):
    """A flurry of `signal_rate_limit` calls within one window must
    produce exactly ONE `on_throttle_start` callback — the user sees
    one calm 'Pausing 30s...' line, not N copies, even though every
    extra signal still extends the deadline (escalation)."""
    starts: List[float] = []
    clears: List[None] = []

    coord = _make_coordinator(
        fake_clock,
        on_start=lambda secs: starts.append(secs),
        on_clear=lambda: clears.append(None),
        max_wait_seconds=600.0,
    )

    # Five rapid signals → one start callback. With server-supplied
    # retry_after=30 each time, the first signal opens a 30s window;
    # subsequent signals escalate (30 * 1.5 = 45s, then 45 * 1.5 = 67.5,
    # etc., capped at max_wait_seconds). Only the first fires the
    # start callback.
    for _ in range(5):
        coord.signal_rate_limit(retry_after_seconds=30.0)

    assert len(starts) == 1
    # The callback receives the wait length.
    assert starts[0] == 30.0
    assert clears == []

    # Advance past whatever the escalated window grew to. With a 600s
    # cap, five signals at base 30s land well below the cap (final
    # window is 30 * 1.5^4 = 151.875s); 200s is safely past it.
    fake_clock.advance(200.0)
    coord.wait_if_throttled()
    assert len(clears) == 1


def test_clear_callback_fires_once_even_with_many_waiters(fake_clock):
    """If multiple workers are blocked and the deadline passes, the
    on_throttle_clear callback fires exactly once — the first waiter to
    observe the elapsed deadline consumes the pending-clear flag."""
    clears: List[None] = []
    coord = _make_coordinator(fake_clock, on_clear=lambda: clears.append(None))
    coord.signal_rate_limit(retry_after_seconds=30.0)

    n = 6
    barrier = threading.Barrier(n + 1)
    done = [threading.Event() for _ in range(n)]

    def _waiter(idx: int):
        barrier.wait(timeout=1.0)
        coord.wait_if_throttled()
        done[idx].set()

    threads = [threading.Thread(target=_waiter, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    barrier.wait(timeout=1.0)
    fake_clock.advance(31.0)
    for ev in done:
        ev.wait(timeout=1.0)
    assert all(ev.is_set() for ev in done)
    # Exactly one clear despite N waiters.
    assert len(clears) == 1


# ─── Per-backend classify_error: 429 → RATE_LIMIT_GLOBAL ───────────────────────

@pytest.fixture
def googledrive_backend(make_config):
    from claude_mirror.backends.googledrive import GoogleDriveBackend
    cfg = make_config(backend="googledrive", drive_folder_id="test")
    return GoogleDriveBackend(cfg)


@pytest.fixture
def dropbox_backend(make_config):
    from claude_mirror.backends.dropbox import DropboxBackend
    cfg = make_config(backend="dropbox", dropbox_folder="/test")
    return DropboxBackend(cfg)


@pytest.fixture
def onedrive_backend(make_config):
    from claude_mirror.backends.onedrive import OneDriveBackend
    cfg = make_config(backend="onedrive", onedrive_client_id="x", onedrive_folder="/test")
    return OneDriveBackend(cfg)


@pytest.fixture
def webdav_backend(make_config):
    from claude_mirror.backends.webdav import WebDAVBackend
    cfg = make_config(
        backend="webdav",
        webdav_url="https://dav.example/remote.php/dav/",
        webdav_folder="claude_mirror",
    )
    return WebDAVBackend(cfg)


def _make_googleapi_http_error(status: int, reason: str = ""):
    """Synthesise a googleapiclient.errors.HttpError-like object with
    `resp.status` and an error_details list."""
    from googleapiclient.errors import HttpError
    resp = MagicMock()
    resp.status = status
    body = {"error": {"errors": [{"reason": reason}] if reason else []}}
    import json as _json
    content = _json.dumps(body).encode("utf-8")
    return HttpError(resp, content)


def test_googledrive_429_user_rate_limit_exceeded_is_global(googledrive_backend):
    """Drive's `userRateLimitExceeded` reason on a 403 means 'this user
    is making too many requests overall' — route through the
    coordinator, not as per-file QUOTA."""
    exc = _make_googleapi_http_error(403, reason="userRateLimitExceeded")
    assert googledrive_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_googledrive_429_rate_limit_exceeded_is_global(googledrive_backend):
    """`rateLimitExceeded` (the broader sibling reason) is also global."""
    exc = _make_googleapi_http_error(403, reason="rateLimitExceeded")
    assert googledrive_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_googledrive_plain_429_is_global(googledrive_backend):
    """A bare 429 with no body reason still routes through the
    coordinator — Drive is signalling overall throttling."""
    exc = _make_googleapi_http_error(429, reason="")
    assert googledrive_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_googledrive_quota_exceeded_stays_quota(googledrive_backend):
    """`quotaExceeded` means storage / daily quota is hit — user must
    free space; this is QUOTA, not RATE_LIMIT_GLOBAL (waiting won't
    help)."""
    exc = _make_googleapi_http_error(403, reason="quotaExceeded")
    assert googledrive_backend.classify_error(exc) == ErrorClass.QUOTA


def test_googledrive_413_payload_too_large_is_file_rejected(googledrive_backend):
    """Per-file rejections (file too large) must NOT classify as
    RATE_LIMIT_GLOBAL — they're not server-wide and waiting changes
    nothing for that file."""
    exc = _make_googleapi_http_error(413)
    assert googledrive_backend.classify_error(exc) == ErrorClass.FILE_REJECTED


def test_dropbox_too_many_requests_summary_is_global(dropbox_backend):
    """Dropbox surfaces account-wide throttling either via
    RateLimitError or via ApiError with `error_summary` containing
    `too_many_requests`. Both must classify as RATE_LIMIT_GLOBAL."""
    from dropbox.exceptions import ApiError
    # Synthesise an ApiError without going through its __init__ (which
    # demands typed-union internals); the classifier reads `error_summary`
    # and `.error` defensively.
    exc = ApiError.__new__(ApiError)
    exc.error_summary = "too_many_requests/.."
    exc.error = None
    assert dropbox_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_dropbox_too_many_write_operations_is_global(dropbox_backend):
    """The other Dropbox throttle string."""
    from dropbox.exceptions import ApiError
    exc = ApiError.__new__(ApiError)
    exc.error_summary = "too_many_write_operations/.."
    exc.error = None
    assert dropbox_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_dropbox_rate_limit_error_class_is_global(dropbox_backend):
    """Dropbox's dedicated `RateLimitError` exception type."""
    from dropbox.exceptions import RateLimitError
    exc = RateLimitError.__new__(RateLimitError)
    exc.error = None
    assert dropbox_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_dropbox_429_status_is_global(dropbox_backend):
    """A bare HttpError with 429 status (no typed union) is also global."""
    from dropbox.exceptions import HttpError
    exc = HttpError.__new__(HttpError)
    exc.status_code = 429
    assert dropbox_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_dropbox_413_payload_too_large_is_file_rejected(dropbox_backend):
    """Per-file rejection — must NOT route as global."""
    from dropbox.exceptions import HttpError
    exc = HttpError.__new__(HttpError)
    exc.status_code = 413
    assert dropbox_backend.classify_error(exc) == ErrorClass.FILE_REJECTED


def test_onedrive_429_is_global(onedrive_backend):
    """Microsoft Graph 429 with Retry-After is the canonical global-throttle case."""
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "47"
    exc = requests.exceptions.HTTPError(response=response)
    assert onedrive_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_onedrive_413_is_file_rejected(onedrive_backend):
    """413 means the single file is too large — per-file rejection, not global."""
    response = requests.Response()
    response.status_code = 413
    exc = requests.exceptions.HTTPError(response=response)
    assert onedrive_backend.classify_error(exc) == ErrorClass.FILE_REJECTED


def test_webdav_429_is_global(webdav_backend):
    """A 429 from a WebDAV server is unusual but must route through the
    coordinator the same as the cloud backends."""
    response = requests.Response()
    response.status_code = 429
    exc = requests.exceptions.HTTPError(response=response)
    assert webdav_backend.classify_error(exc) == ErrorClass.RATE_LIMIT_GLOBAL


def test_webdav_413_is_file_rejected(webdav_backend):
    """Per-file rejection — must NOT route as global."""
    response = requests.Response()
    response.status_code = 413
    exc = requests.exceptions.HTTPError(response=response)
    assert webdav_backend.classify_error(exc) == ErrorClass.FILE_REJECTED


def test_sftp_never_returns_rate_limit_global(make_config):
    """SFTP doesn't have a 429 equivalent. No exception type the SFTP
    backend's classify_error inspects should produce RATE_LIMIT_GLOBAL."""
    from claude_mirror.backends.sftp import SFTPBackend
    cfg = make_config(
        backend="sftp",
        sftp_host="host.example",
        sftp_user="user",
        sftp_folder="/path",
    )
    backend = SFTPBackend(cfg)

    # Cover a representative slice of the exception types SFTP sees.
    import paramiko
    import socket
    candidates = [
        paramiko.ssh_exception.AuthenticationException("bad creds"),
        paramiko.ssh_exception.NoValidConnectionsError({("h", 22): OSError("nope")}),
        socket.timeout("read timed out"),
        ConnectionResetError("reset"),
        ConnectionRefusedError("refused"),
        IOError(2, "no such file"),
        IOError("disk full"),
        RuntimeError("unexpected"),
    ]
    for exc in candidates:
        assert backend.classify_error(exc) != ErrorClass.RATE_LIMIT_GLOBAL


# ─── extract_retry_after_seconds ──────────────────────────────────────────────

def test_extract_retry_after_from_requests_response():
    """OneDrive's `requests.HTTPError.response.headers['Retry-After']`."""
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "42"
    exc = requests.exceptions.HTTPError(response=response)
    assert retry_mod.extract_retry_after_seconds(exc) == 42.0


def test_extract_retry_after_returns_none_when_missing():
    """No header → None; coordinator uses its default 30s."""
    response = requests.Response()
    response.status_code = 429
    exc = requests.exceptions.HTTPError(response=response)
    assert retry_mod.extract_retry_after_seconds(exc) is None


def test_extract_retry_after_handles_non_numeric_gracefully():
    """HTTP-date-form Retry-After (rare in API replies) → None, not crash."""
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "Wed, 21 Oct 2026 07:28:00 GMT"
    exc = requests.exceptions.HTTPError(response=response)
    assert retry_mod.extract_retry_after_seconds(exc) is None


# ─── Engine integration ──────────────────────────────────────────────────────

class _ThrottledOnceBackend:
    """Minimal backend stub: first `upload_file` raises RATE_LIMIT_GLOBAL,
    subsequent calls succeed. Used to verify the coordinator-driven
    re-attempt loop in `SyncEngine._upload_with_coordinator`."""

    backend_name = "throttled-once"

    def __init__(self):
        self.calls = 0

    def classify_error(self, exc):
        return ErrorClass.RATE_LIMIT_GLOBAL


def test_engine_upload_with_coordinator_retries_after_throttle(fake_clock, make_config):
    """When the upload callable raises RATE_LIMIT_GLOBAL once and then
    succeeds, `_upload_with_coordinator` must:
      1. Signal the coordinator with the server-supplied retry-after.
      2. Wait through the throttle window.
      3. Re-call the upload callable, which now returns a file ID.
    """
    from claude_mirror.sync import SyncEngine

    # We bypass __init__ to focus on the helper under test.
    eng = SyncEngine.__new__(SyncEngine)
    eng.config = make_config(max_retry_attempts=3, max_throttle_wait_seconds=60.0)
    eng._coordinator = _make_coordinator(fake_clock, max_wait_seconds=60.0)

    backend = _ThrottledOnceBackend()
    response = requests.Response()
    response.status_code = 429
    response.headers["Retry-After"] = "5"
    one_shot_exc = requests.exceptions.HTTPError(response=response)

    state = {"attempts": 0}

    def upload():
        state["attempts"] += 1
        if state["attempts"] == 1:
            raise one_shot_exc
        return "file-id-xyz"

    # Drive the coordinator's wait off the fake clock by advancing in a
    # parallel thread once the first attempt has signalled.
    def _advancer():
        # Wait until the coordinator becomes throttled, then jump past
        # the 5s retry-after window.
        for _ in range(200):
            if eng._coordinator.is_throttled:
                fake_clock.advance(6.0)
                return
            threading.Event().wait(0.001)

    advancer = threading.Thread(target=_advancer, daemon=True)
    advancer.start()

    result = eng._upload_with_coordinator(upload, backend)
    advancer.join(timeout=1.0)
    assert result == "file-id-xyz"
    assert state["attempts"] == 2


def test_engine_upload_with_coordinator_propagates_non_global_errors(fake_clock, make_config):
    """A non-rate-limit failure (e.g. AUTH) must NOT be swallowed by the
    coordinator loop — it propagates so the caller's per-class state
    machine (pending_retry / failed_perm) still works."""
    from claude_mirror.sync import SyncEngine

    eng = SyncEngine.__new__(SyncEngine)
    eng.config = make_config(max_retry_attempts=3, max_throttle_wait_seconds=60.0)
    eng._coordinator = _make_coordinator(fake_clock)

    class _AuthBackend:
        backend_name = "auth-fail"
        def classify_error(self, exc):
            return ErrorClass.AUTH

    auth_exc = RuntimeError("token revoked")
    def upload():
        raise auth_exc

    with pytest.raises(RuntimeError):
        eng._upload_with_coordinator(upload, _AuthBackend())


def test_engine_upload_with_coordinator_no_coordinator_falls_through(make_config):
    """Defensive path: when no coordinator is wired (legacy code path,
    direct unit tests), the helper falls through to a plain call."""
    from claude_mirror.sync import SyncEngine

    eng = SyncEngine.__new__(SyncEngine)
    eng.config = make_config()
    eng._coordinator = None

    def upload():
        return "id-direct"

    class _Anon:
        backend_name = "anon"
        def classify_error(self, exc):
            return ErrorClass.UNKNOWN

    assert eng._upload_with_coordinator(upload, _Anon()) == "id-direct"


def test_engine_calm_message_emitted_once(fake_clock, make_config, monkeypatch):
    """A burst of N RATE_LIMIT_GLOBAL signals across parallel workers
    yields exactly ONE 'Pausing Ns...' line and ONE 'Throttle cleared'
    line — not N transient warnings."""
    from claude_mirror.sync import SyncEngine

    printed: List[str] = []

    class _CapturingConsole:
        def print(self, msg, **kwargs):
            printed.append(str(msg))

    monkeypatch.setattr("claude_mirror.sync.console", _CapturingConsole())

    eng = SyncEngine.__new__(SyncEngine)
    eng.config = make_config(max_retry_attempts=1, max_throttle_wait_seconds=60.0)
    coord = eng._make_coordinator()
    fake_clock.register_cv(coord._cv)
    eng._coordinator = coord

    # Several signals; only one start message expected. Each subsequent
    # signal escalates the deadline (capped at 60s by the engine's
    # configured `max_throttle_wait_seconds`), but the calm 'Pausing...'
    # line still emits exactly once.
    for _ in range(10):
        coord.signal_rate_limit(retry_after_seconds=30.0)

    # One pause line.
    pause_lines = [m for m in printed if "Pausing" in m]
    assert len(pause_lines) == 1, f"expected 1 pause line, got: {printed!r}"

    # Advance past the cap so the throttle definitely clears.
    fake_clock.advance(61.0)
    coord.wait_if_throttled()
    cleared_lines = [m for m in printed if "Throttle cleared" in m]
    assert len(cleared_lines) == 1


def test_non_rate_limit_classes_unaffected_by_coordinator(fake_clock, make_config):
    """Sanity: calling `signal_rate_limit` does not change classification
    behaviour for other ErrorClass values. The coordinator is purely a
    timing primitive — TRANSIENT/AUTH/QUOTA/PERMISSION/FILE_REJECTED
    semantics on the per-backend `classify_error` are untouched."""
    from claude_mirror.backends.googledrive import GoogleDriveBackend
    cfg = make_config(backend="googledrive", drive_folder_id="t")
    gd = GoogleDriveBackend(cfg)

    # AUTH path
    exc_auth = _make_googleapi_http_error(401)
    assert gd.classify_error(exc_auth) == ErrorClass.AUTH

    # PERMISSION path
    exc_perm = _make_googleapi_http_error(403, reason="forbidden")
    assert gd.classify_error(exc_perm) == ErrorClass.PERMISSION

    # FILE_REJECTED path (404 in Drive's classify rules)
    exc_404 = _make_googleapi_http_error(404)
    assert gd.classify_error(exc_404) == ErrorClass.FILE_REJECTED

    # TRANSIENT path (5xx)
    exc_500 = _make_googleapi_http_error(503)
    assert gd.classify_error(exc_500) == ErrorClass.TRANSIENT
