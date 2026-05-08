"""Bandwidth throttling for upload paths (v0.5.39+).

Implements a classic token-bucket rate-limiter that every backend's
upload path can consume from before sending bytes over the wire.
The bucket fills at a constant `rate_kbps * 1024 / 8` bytes per second
up to `capacity_bytes`; callers ask for N tokens via `consume(N)`,
which blocks just long enough for the bucket to accumulate them.

Why tokens-not-throttled-sleeps:
    A simple `sleep(N / rate)` model misbehaves around bursts and
    small files: a 4 KB file over a 1024 kbps cap would sleep 31 ms
    even when no other traffic is in flight. Token-bucket lets a
    single small file pass through without delay (the bucket starts
    full at construction) while still imposing the long-run rate.

Why per-bucket-not-per-process:
    Each backend gets its own bucket, so a Tier 2 mirror config can
    throttle Drive without throttling SFTP or vice versa. A NullBucket
    is returned when the user hasn't set a cap — callers don't need
    conditionals, they always call `bucket.consume(N)`.

Threadsafety:
    Drive's resumable upload, OneDrive's chunked PUT, and SFTP's
    parallel-worker fan-out all call `consume()` from worker threads
    in parallel. The shared state (level + last refill time) is
    guarded by `threading.Lock`. No re-entrancy is needed — `consume`
    never calls into user code while holding the lock.

Design constraint:
    Tests must NOT actually sleep. `consume()` calls a module-level
    `_now()` and `_sleep()` indirection so tests can monkey-patch a
    deterministic clock. Production code uses `time.monotonic` /
    `time.sleep` — no behaviour change for real callers.
"""
from __future__ import annotations

import threading
import time
from typing import Optional, Union


# Module-level indirection for tests. Production paths call these as
# `_now()` / `_sleep(...)`; the test suite monkey-patches them with a
# fake-clock helper to drive `consume()` deterministically without
# wall-clock waits.
def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)


class TokenBucket:
    """Token-bucket bandwidth limiter (bytes per second under the hood).

    Construction:
        TokenBucket(rate_kbps=512)            # 512 kilobits/sec, default capacity
        TokenBucket(rate_kbps=512, capacity_bytes=131072)  # 128 KiB burst window

    The constructor pre-fills the bucket to capacity so a single small
    file (or a single chunk smaller than capacity) passes through with
    zero wait — the throttle only kicks in after that initial burst is
    drained, which is the desired UX.

    Default capacity:
        max(64 KiB, rate_bytes_per_sec). 64 KiB is enough for a typical
        small-markdown-file resumable-upload chunk without delay; for
        higher caps the rate-per-sec floor lets a one-second worth of
        burst pass before throttling.

    `rate_kbps` semantics:
        Kilobits per second (1 kbps = 1024 bits/s = 128 bytes/s). Match
        the unit users see in their ISP / NAS contracts. 1024 kbps =
        128 KiB/sec ≈ 7.5 MiB/min.
    """

    def __init__(self, rate_kbps: float, capacity_bytes: Optional[int] = None) -> None:
        if rate_kbps <= 0:
            raise ValueError(
                "TokenBucket rate_kbps must be > 0 (use NullBucket for no-throttle)"
            )
        # Convert kbps -> bytes/sec. 1 kbps = 1024 bits/s = 128 bytes/s.
        self.rate_bytes_per_sec: float = float(rate_kbps) * 1024.0 / 8.0
        if capacity_bytes is None:
            capacity_bytes = max(64 * 1024, int(self.rate_bytes_per_sec))
        if capacity_bytes <= 0:
            raise ValueError("TokenBucket capacity_bytes must be > 0")
        self.capacity: int = int(capacity_bytes)
        # Start full so a single small request passes without waiting.
        self._level: float = float(self.capacity)
        self._last_refill: float = _now()
        self._lock = threading.Lock()

    def _refill_locked(self) -> None:
        """Move forward to the current time and accumulate tokens. Caller
        must hold `self._lock`."""
        now = _now()
        elapsed = now - self._last_refill
        if elapsed > 0:
            self._level = min(
                float(self.capacity),
                self._level + elapsed * self.rate_bytes_per_sec,
            )
            self._last_refill = now

    def consume(self, byte_count: int) -> None:
        """Block until `byte_count` tokens are available, then deduct them.

        Larger-than-capacity requests are handled in capacity-sized waves:
        the call internally drains the bucket, sleeps, and repeats. This
        keeps the long-run rate honest without forcing callers to chunk
        their writes (some SDK paths hand us a 5 MB chunk we cannot split).
        """
        if byte_count <= 0:
            return
        remaining = int(byte_count)
        while remaining > 0:
            with self._lock:
                self._refill_locked()
                # Take whichever is smaller: the remaining ask, or what
                # the bucket can ever hold in one drain.
                take = min(remaining, self.capacity)
                if self._level >= take:
                    self._level -= take
                    remaining -= take
                    sleep_for = 0.0
                else:
                    deficit = take - self._level
                    # How long until enough tokens accumulate? Compute
                    # under the lock so two threads don't race the rate.
                    sleep_for = deficit / self.rate_bytes_per_sec
            if sleep_for > 0:
                _sleep(sleep_for)


class NullBucket:
    """No-op bucket for the unconfigured case. `consume(N)` returns
    immediately; tests and callers can treat it as a TokenBucket without
    `if bucket is None` guards everywhere.
    """

    def consume(self, byte_count: int) -> None:  # noqa: D401 — interface match
        return None


def get_throttle(rate_kbps: Optional[float]) -> Union[TokenBucket, NullBucket]:
    """Build a throttle bucket from an optional rate.

    Returns:
        NullBucket if `rate_kbps` is None, 0, negative, or otherwise
        falsy — this is the "throttling disabled" case and the no-op
        bucket lets call sites skip the conditional.

        TokenBucket(rate_kbps) otherwise.
    """
    if rate_kbps is None:
        return NullBucket()
    try:
        rate = float(rate_kbps)
    except (TypeError, ValueError):
        return NullBucket()
    if rate <= 0:
        return NullBucket()
    return TokenBucket(rate)
