"""Process-wide retry coordination for global rate-limit (429) responses.

When a backend signals that the SERVER is throttling this client/account
(HTTP 429 with `userRateLimitExceeded`/`rateLimitExceeded`, Dropbox's
`too_many_requests`/`too_many_write_operations`, or Microsoft Graph's 429
with a `Retry-After` header), every in-flight upload should pause for a
shared backoff window rather than each retrying independently — N
independent per-file retries arriving in clustered bursts only ratchet up
the rate-limit pressure and produce a flurry of red warnings instead of a
calm "we're being throttled, slowing down for 30 seconds".

`BackoffCoordinator` provides that shared state. One instance per
`engine.push()` call (or sync / pull); workers call `wait_if_throttled()`
at the top of every upload attempt, and call `signal_rate_limit()` when
they catch a `RATE_LIMIT_GLOBAL` failure.

Threadsafety: all mutation goes through `self._lock`; the deadline is a
`time.monotonic()` value so it survives wallclock changes (NTP step,
DST). Time is read through a module-level `_now()` / `_sleep()` pair so
tests can swap them out without patching the `time` module globally
(per the project's `feedback_no_global_time_sleep_patch.md` rule).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional


# Default initial backoff window when the server didn't supply a Retry-After.
# 30s is long enough to give a Drive / Dropbox / Graph rate-limiter time to
# drain its own bucket without making the user feel the CLI has frozen.
DEFAULT_INITIAL_BACKOFF_SECONDS: float = 30.0

# Hard cap on any single throttled window. With escalations multiplying
# the backoff by 1.5×, the coordinator would otherwise grow unbounded if
# a backend keeps signalling. 600s = 10 minutes — long enough for the
# heaviest throttles to clear, short enough that a cron job which
# accidentally hits a hard quota doesn't sit blocked all day.
MAX_WAIT_SECONDS: float = 600.0

# Per-escalation growth factor. Each fresh signal_rate_limit() within an
# already-throttled window extends the deadline by `current_backoff *
# ESCALATION_MULTIPLIER`, capped at MAX_WAIT_SECONDS.
ESCALATION_MULTIPLIER: float = 1.5


# Module-level wrappers around `time.monotonic()` and `time.sleep()` so
# tests can substitute them via `monkeypatch.setattr` on the module
# attribute, without patching the stdlib `time` module globally — the
# global patch caused the regression documented in
# `feedback_no_global_time_sleep_patch.md`. Production code calls these
# wrappers; tests rebind them to a controlled mock clock.
def _now() -> float:
    """Monotonic clock. Wrapped for test injection."""
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """Sleep wrapper. Wrapped for test injection."""
    if seconds > 0:
        time.sleep(seconds)


def extract_retry_after_seconds(exc: BaseException) -> Optional[float]:
    """Best-effort extraction of a server-supplied `Retry-After` window
    from a backend exception, in seconds.

    Microsoft Graph (OneDrive) is the most consistent — its 429s carry
    a `Retry-After` header on `exc.response.headers`. Google Drive
    sometimes includes one on `HttpError.resp`. Dropbox's
    `RateLimitError` exposes `error.retry_after`. WebDAV depends on the
    server. SFTP doesn't apply.

    Returns None if no value can be extracted; the coordinator falls
    back to `DEFAULT_INITIAL_BACKOFF_SECONDS` in that case.
    """
    # `requests` HTTPError shape (OneDrive, WebDAV).
    try:
        resp = getattr(exc, "response", None)
        if resp is not None:
            headers = getattr(resp, "headers", None)
            if headers is not None:
                ra = headers.get("Retry-After") or headers.get("retry-after")
                if ra:
                    try:
                        return max(0.0, float(ra))
                    except (TypeError, ValueError):
                        # HTTP-date form (e.g. "Wed, 21 Oct 2026 07:28:00 GMT")
                        # is allowed by the spec but rare in API replies; we
                        # don't bother parsing it — the coordinator's
                        # default backoff handles it.
                        pass
    except Exception:
        pass

    # googleapiclient `HttpError` shape — `exc.resp` is a
    # `httplib2.Response` with header fields accessible via mapping.
    try:
        resp2 = getattr(exc, "resp", None)
        if resp2 is not None:
            try:
                # httplib2.Response is dict-like.
                ra2 = resp2.get("retry-after") if hasattr(resp2, "get") else None
            except Exception:
                ra2 = None
            if ra2:
                try:
                    return max(0.0, float(ra2))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    # Dropbox SDK's `RateLimitError` exposes `error.retry_after` (an
    # integer count of seconds).
    try:
        err = getattr(exc, "error", None)
        if err is not None:
            ra3 = getattr(err, "retry_after", None)
            if ra3 is not None:
                try:
                    return max(0.0, float(ra3))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    return None


class BackoffCoordinator:
    """Shared backoff state across parallel uploaders.

    When any worker signals `RATE_LIMIT_GLOBAL` via
    `signal_rate_limit(retry_after_seconds)`, every other worker that
    calls `wait_if_throttled()` blocks until the deadline elapses.

    A single `BackoffCoordinator` instance is created per
    `engine.push()` call and passed to all workers via shared closure.
    Multiple coordinators do not interfere with each other; each push
    has its own throttle state, which is appropriate because the
    rate-limit budgets are per-backend and a different push (different
    set of mirrors) may be hitting a completely independent quota.

    The coordinator does NOT decide WHETHER to retry — that's the
    caller's job. It only coordinates the timing so retries don't
    cluster.

    Parameters
    ----------
    max_wait_seconds:
        Hard cap on any single throttled window in seconds. Defaults
        to `MAX_WAIT_SECONDS` (600s). User can lower it for cron jobs
        that should fail fast rather than sit blocked.
    on_throttle_start:
        Optional callback fired once when the coordinator first enters
        the throttled state for a given window. Used by the engine to
        emit a single calm "Backend reports rate limit. Pausing 30s..."
        message instead of N transient warnings. Receives the wait
        seconds as its only argument. Must not block.
    on_throttle_clear:
        Optional callback fired once when the coordinator transitions
        back to un-throttled (the deadline has elapsed and a worker
        observes it). Used to emit "Throttle cleared. Resuming uploads."
    """

    def __init__(
        self,
        max_wait_seconds: float = MAX_WAIT_SECONDS,
        on_throttle_start: Optional[Callable[[float], None]] = None,
        on_throttle_clear: Optional[Callable[[], None]] = None,
    ) -> None:
        self._lock = threading.Lock()
        # Wakes every worker waiting on `wait_if_throttled` when the
        # deadline shifts (extension OR clear).
        self._cv = threading.Condition(self._lock)
        # `_deadline` is a monotonic-clock value: workers should remain
        # blocked until `_now() >= _deadline`. None = not throttled.
        self._deadline: Optional[float] = None
        # Current backoff window length — informational, used to compute
        # the next escalation. Reset to 0 when throttle clears.
        self._current_backoff: float = 0.0
        self._max_wait_seconds: float = max(0.0, float(max_wait_seconds))
        self._on_start = on_throttle_start
        self._on_clear = on_throttle_clear
        # Tracks whether the on_throttle_start callback has fired for
        # the currently-active window, so a flurry of signal_rate_limit
        # calls from concurrent workers produces a single "Pausing..."
        # message rather than N copies.
        self._start_emitted_for_window: bool = False
        # Tracks whether on_throttle_clear should fire — set when the
        # window starts, consumed by the first worker to observe the
        # clear.
        self._clear_pending: bool = False

    @property
    def is_throttled(self) -> bool:
        """True if the coordinator is currently within an active throttled
        window. Reads under lock; the value is a snapshot — by the time
        the caller acts on it, another worker may have signalled an
        extension or the window may have elapsed."""
        with self._lock:
            return self._is_throttled_locked()

    def _is_throttled_locked(self) -> bool:
        """Caller must hold `self._lock`."""
        if self._deadline is None:
            return False
        return _now() < self._deadline

    def signal_rate_limit(self, retry_after_seconds: Optional[float] = None) -> None:
        """Mark the backend as globally rate-limited and (re)set the
        backoff window.

        If no window is currently active, starts one of length
        `retry_after_seconds` (or `DEFAULT_INITIAL_BACKOFF_SECONDS` if
        the server didn't supply a value). If a window IS active, this
        is treated as an escalation: the new window length is
        `min(self._max_wait_seconds, current_backoff * ESCALATION_MULTIPLIER)`,
        OR `retry_after_seconds` if the server supplied one larger than
        what we'd compute. Either way, the deadline is shifted forward
        from `_now()` by that length — never beyond
        `_max_wait_seconds`.

        Threadsafe; safe to call from any worker thread.
        """
        # Clamp negative / NaN values to defaults.
        try:
            supplied = float(retry_after_seconds) if retry_after_seconds is not None else None
            if supplied is not None and (supplied != supplied or supplied < 0):
                # NaN or negative — treat as "no value supplied".
                supplied = None
        except (TypeError, ValueError):
            supplied = None

        with self._lock:
            now = _now()
            already_active = self._deadline is not None and now < self._deadline
            if not already_active:
                # Fresh throttled window.
                base = supplied if supplied is not None else DEFAULT_INITIAL_BACKOFF_SECONDS
                wait = min(self._max_wait_seconds, max(0.0, base))
                self._current_backoff = wait
                self._deadline = now + wait
                self._start_emitted_for_window = False
                self._clear_pending = True
                # Fire the start callback exactly once per window.
                self._maybe_fire_start_locked(wait)
            else:
                # Escalation: extend by max(server-supplied, current * 1.5).
                escalation = self._current_backoff * ESCALATION_MULTIPLIER
                if supplied is not None and supplied > escalation:
                    new_window = supplied
                else:
                    new_window = escalation
                new_window = min(self._max_wait_seconds, max(0.0, new_window))
                self._current_backoff = new_window
                self._deadline = now + new_window
                # Window length grew — the original "Pausing 30s..."
                # message is still valid for the user; we don't fire a
                # second start callback, but we do refresh the deadline
                # so all waiters re-check at the new time.
            # Wake every waiter so they re-evaluate the deadline.
            self._cv.notify_all()

    def _maybe_fire_start_locked(self, wait_seconds: float) -> None:
        """Caller must hold `self._lock`."""
        if self._start_emitted_for_window:
            return
        self._start_emitted_for_window = True
        if self._on_start is None:
            return
        # Drop the lock to avoid pinning workers if the callback
        # accidentally blocks (Rich console writes generally don't, but
        # we're defensive here).
        cb = self._on_start
        self._lock.release()
        try:
            try:
                cb(wait_seconds)
            except Exception:
                # Callbacks must never break coordination.
                pass
        finally:
            self._lock.acquire()

    def wait_if_throttled(self) -> None:
        """Block until the active throttled window elapses, or return
        immediately if no window is active.

        Called by every worker BEFORE issuing each new upload attempt
        so a fresh wave of retries respects the active throttle. If a
        worker is already waiting and another worker signals an
        extension, the waiter wakes up, observes the new (later)
        deadline, and waits again. Once the deadline passes with no
        further signals, the throttle clears, the on_throttle_clear
        callback fires (exactly once per window), and all waiters
        return.

        Threadsafe; safe to call from any worker thread.
        """
        with self._lock:
            while True:
                if self._deadline is None:
                    return
                now = _now()
                remaining = self._deadline - now
                if remaining <= 0:
                    # Deadline elapsed — clear state, fire clear callback,
                    # and return.
                    self._deadline = None
                    self._current_backoff = 0.0
                    self._start_emitted_for_window = False
                    fire_clear = self._clear_pending
                    self._clear_pending = False
                    if fire_clear and self._on_clear is not None:
                        cb = self._on_clear
                        self._lock.release()
                        try:
                            try:
                                cb()
                            except Exception:
                                pass
                        finally:
                            self._lock.acquire()
                    # Wake any other waiters so they observe the cleared
                    # state — even though `wait()` would have returned
                    # them via `cv.notify_all()`, an extra notify is
                    # harmless and protects against races.
                    self._cv.notify_all()
                    return
                # Use the condition variable so a concurrent
                # signal_rate_limit (extension) wakes us early to
                # observe the new deadline, OR a concurrent worker
                # whose deadline-clear path runs first wakes us so
                # we don't oversleep.
                self._cv.wait(timeout=remaining)
