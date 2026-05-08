"""Tests for the v0.5.39 token-bucket bandwidth throttle.

All tests use a mocked clock — never sleep in real time. The throttle
module exposes `_now()` and `_sleep()` indirections precisely so the
suite can drive `consume()` deterministically.

Coverage:
    * NullBucket no-ops on consume.
    * `get_throttle()` returns NullBucket for None / 0 / negative /
      malformed inputs and TokenBucket for valid rates.
    * TokenBucket math: instantaneous burst when a request fits in the
      starting capacity; the call sleeps for exactly the deficit /
      rate when the request exceeds available tokens.
    * Refill behaviour: tokens accumulate at rate * elapsed up to the
      capacity ceiling.
    * Threadsafe under contention — N threads each consume(K) and
      together pay rate * elapsed seconds, never less.
    * Config wiring: `max_upload_kbps` field reads through to
      get_throttle and produces the right bucket type.
    * Backend integration smoke test: a fake backend that calls
      `bucket.consume(N)` per byte-sized payload sees the expected
      total reservations.
"""
from __future__ import annotations

import threading

import pytest

from claude_mirror import throttle as throttle_mod
from claude_mirror.config import Config
from claude_mirror.throttle import NullBucket, TokenBucket, get_throttle


# ─── Fake clock helper ─────────────────────────────────────────────────────────


class FakeClock:
    """Deterministic monotonic clock + sleep stub.

    Tests inject this via monkeypatch on `throttle._now` and
    `throttle._sleep`. `_sleep(d)` advances the clock by exactly `d`
    seconds — so the bucket sees `_now()` return the post-sleep time
    on its next refill, and the test can assert that sleep was called
    with the expected duration.
    """

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        # Record the request, then advance time. We accept zero-second
        # sleeps to mirror production behaviour (no-op when bucket has
        # the tokens already), but we record them for visibility.
        self.sleeps.append(float(seconds))
        if seconds > 0:
            self.t += float(seconds)

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    """Patch `throttle._now` and `throttle._sleep` with a fake clock."""
    fc = FakeClock()
    monkeypatch.setattr(throttle_mod, "_now", fc.now)
    monkeypatch.setattr(throttle_mod, "_sleep", fc.sleep)
    return fc


# ─── 1. NullBucket no-ops ──────────────────────────────────────────────────────


def test_null_bucket_consume_is_noop():
    """NullBucket.consume must return immediately for any size — it's
    the "throttling disabled" sentinel."""
    nb = NullBucket()
    assert nb.consume(0) is None
    assert nb.consume(1) is None
    assert nb.consume(10 * 1024 * 1024) is None  # 10 MiB ask


# ─── 2. get_throttle dispatch ──────────────────────────────────────────────────


@pytest.mark.parametrize("rate", [None, 0, -1, "", "not-a-number"])
def test_get_throttle_returns_null_bucket_for_disabled_inputs(rate):
    """None / 0 / negative / unparseable → NullBucket. Callers shouldn't
    have to gate on `rate is None`."""
    bucket = get_throttle(rate)
    assert isinstance(bucket, NullBucket)


def test_get_throttle_returns_token_bucket_for_valid_rate():
    """A positive rate produces a real TokenBucket with the expected
    bytes-per-second conversion (1 kbps = 128 bytes/s)."""
    bucket = get_throttle(1024)  # 1024 kbps == 128 KiB/s
    assert isinstance(bucket, TokenBucket)
    assert bucket.rate_bytes_per_sec == pytest.approx(1024 * 1024 / 8)


# ─── 3. TokenBucket: small request fits in initial capacity ────────────────────


def test_consume_fits_in_initial_capacity_no_sleep(clock):
    """A request smaller than the starting capacity must complete with
    zero `_sleep` calls — the bucket starts full."""
    # 1024 kbps -> 128 KiB/s; default capacity = max(64 KiB, 128 KiB) = 128 KiB.
    bucket = TokenBucket(rate_kbps=1024)
    bucket.consume(4 * 1024)  # 4 KiB ask — well under 128 KiB capacity
    # Either no sleeps recorded, or only zero-length ones.
    assert all(s == 0 for s in clock.sleeps)


# ─── 4. TokenBucket: deficit triggers exact sleep ──────────────────────────────


def test_consume_more_than_capacity_sleeps_for_expected_duration(clock):
    """Asking for 2 capacity-worth of bytes from a freshly-built bucket
    must drain the initial fill and then sleep ≈ capacity / rate."""
    # rate_kbps=8 -> 1 KiB/s. Capacity defaults to max(64 KiB, 1024) = 64 KiB.
    bucket = TokenBucket(rate_kbps=8)
    rate_bps = bucket.rate_bytes_per_sec  # 1024 bytes/sec
    cap = bucket.capacity                  # 64 KiB
    # First wave: drains the initial cap in one shot, no sleep.
    # Second wave: bucket empty, must wait cap / rate seconds for refill.
    bucket.consume(2 * cap)
    expected = cap / rate_bps
    # Production code emits exactly one non-zero sleep equal to expected
    # (refill time for the second wave). Allow rounding slack.
    nonzero = [s for s in clock.sleeps if s > 0]
    assert len(nonzero) == 1
    assert nonzero[0] == pytest.approx(expected, rel=1e-6)


# ─── 5. TokenBucket: refill respects elapsed time ──────────────────────────────


def test_refill_caps_at_capacity(clock):
    """Letting the clock run forward longer than the time to fill must
    NOT overflow capacity — `min(capacity, ...)` must hold."""
    bucket = TokenBucket(rate_kbps=8, capacity_bytes=4096)  # 1 KiB/s, 4 KiB cap
    bucket.consume(4096)  # drain
    # Advance the clock far past the refill horizon.
    clock.advance(60)
    # Next consume of 4 KiB must succeed without sleeping (bucket has
    # been topped off to capacity).
    sleeps_before = len([s for s in clock.sleeps if s > 0])
    bucket.consume(4096)
    sleeps_after = len([s for s in clock.sleeps if s > 0])
    assert sleeps_after == sleeps_before


def test_refill_partial_after_advance(clock):
    """After draining, advance the clock for half the refill time — the
    next consume of (rate * 0.5 * elapsed) bytes should pass without
    sleep, but a full-capacity ask must sleep for the remainder."""
    bucket = TokenBucket(rate_kbps=8, capacity_bytes=4096)
    bucket.consume(4096)  # drain
    # Advance 2 seconds: bucket gains 2 KiB (1 KiB/s * 2 s).
    clock.advance(2)
    # 2 KiB ask → must NOT sleep.
    pre = len([s for s in clock.sleeps if s > 0])
    bucket.consume(2048)
    assert len([s for s in clock.sleeps if s > 0]) == pre
    # Now drain again → empty.
    # Ask for 1024 → must sleep ≈ 1 second.
    bucket.consume(1024)
    expected = 1024 / bucket.rate_bytes_per_sec  # 1.0 s
    assert clock.sleeps[-1] == pytest.approx(expected, rel=1e-6)


# ─── 6. TokenBucket: threadsafe under contention ───────────────────────────────


def test_threadsafe_under_contention(clock):
    """Eight threads each consume(K) where K = capacity. Total bytes =
    8 * capacity; with the bucket starting full, the long-run cost is
    7 * capacity bytes' worth of refill waits. The aggregate sleep
    time must be ≥ (7 * cap / rate) — never less, even if individual
    threads race."""
    bucket = TokenBucket(rate_kbps=8, capacity_bytes=4096)  # 1 KiB/s, 4 KiB cap
    threads = []

    def worker():
        bucket.consume(bucket.capacity)

    for _ in range(8):
        t = threading.Thread(target=worker)
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total_slept = sum(s for s in clock.sleeps if s > 0)
    # 8 waves of `capacity` bytes; the first comes from the initial
    # fill (no sleep), the remaining 7 each cost cap / rate. Allow a
    # small margin because the bucket may serve partial refills mid-
    # wave depending on inter-thread interleaving — the LOWER BOUND is
    # the only safe assertion.
    expected_min = 7 * (bucket.capacity / bucket.rate_bytes_per_sec)
    assert total_slept >= expected_min - 1e-6


# ─── 7. TokenBucket: zero / negative consume is a no-op ────────────────────────


def test_consume_zero_or_negative_is_noop(clock):
    """consume(0) and consume(-N) must return immediately — they're a
    common edge case (empty file, hash-only lookup)."""
    bucket = TokenBucket(rate_kbps=1024)
    bucket.consume(0)
    bucket.consume(-1)
    assert all(s == 0 for s in clock.sleeps)


# ─── 8. TokenBucket: invalid construction args ─────────────────────────────────


def test_token_bucket_rejects_non_positive_rate():
    """rate_kbps <= 0 must raise — callers should use NullBucket via
    get_throttle instead. This guards against accidental zero-rate
    construction that would deadlock in `consume`."""
    with pytest.raises(ValueError, match="rate_kbps"):
        TokenBucket(rate_kbps=0)
    with pytest.raises(ValueError, match="rate_kbps"):
        TokenBucket(rate_kbps=-5)


def test_token_bucket_rejects_zero_capacity():
    """capacity_bytes <= 0 must raise — a zero-capacity bucket would
    never satisfy any consume call."""
    with pytest.raises(ValueError, match="capacity"):
        TokenBucket(rate_kbps=1024, capacity_bytes=0)


# ─── 9. Config field defaults and override ─────────────────────────────────────


def test_config_max_upload_kbps_default_is_none(tmp_path):
    """Default `max_upload_kbps` is None — backwards-compat with every
    pre-v0.5.39 YAML."""
    cfg = Config(project_path=str(tmp_path))
    assert cfg.max_upload_kbps is None


def test_config_max_upload_kbps_round_trips_through_yaml(tmp_path):
    """Value set in the dataclass survives save → load."""
    cfg = Config(project_path=str(tmp_path), max_upload_kbps=2048)
    yaml_path = tmp_path / "config.yaml"
    cfg.save(str(yaml_path))
    loaded = Config.load(str(yaml_path))
    assert loaded.max_upload_kbps == 2048


def test_config_webdav_streaming_threshold_default(tmp_path):
    """Default WebDAV streaming threshold is 4 MiB, documented per the
    field comment."""
    cfg = Config(project_path=str(tmp_path))
    assert cfg.webdav_streaming_threshold_bytes == 4 * 1024 * 1024


# ─── 10. Backend integration smoke test ────────────────────────────────────────


class _StubBucket:
    """Records every `consume()` call with the byte count the backend
    asked for. Drop-in compatible with TokenBucket / NullBucket for
    integration smoke tests."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    def consume(self, n: int) -> None:
        self.calls.append(int(n))


def test_dropbox_upload_calls_consume_with_payload_size(monkeypatch, tmp_path):
    """DropboxBackend.upload_file must call `bucket.consume(len(body))`
    before handing the bytes to the SDK. We patch get_throttle to
    return a stub bucket and the SDK client to a no-op."""
    stub = _StubBucket()

    monkeypatch.setattr(
        "claude_mirror.backends.dropbox.get_throttle",
        lambda _kbps: stub,
    )

    # Build the backend with a stub Dropbox client.
    from claude_mirror.backends.dropbox import DropboxBackend

    cfg = Config(
        project_path=str(tmp_path),
        backend="dropbox",
        dropbox_folder="/test",
        max_upload_kbps=1024,
    )
    backend = DropboxBackend(cfg)

    class _FakeDbx:
        def files_upload(self, body, dest_path, mode):
            self.last = (len(body), dest_path)

    fake = _FakeDbx()
    backend._dbx = fake  # bypass authenticate()

    local = tmp_path / "note.md"
    payload = b"hello world\n" * 100  # 1200 bytes
    local.write_bytes(payload)

    # Direct path with file_id avoids resolve_path overhead.
    backend.upload_file(str(local), "note.md", "/test", file_id="/test/note.md")

    assert stub.calls == [len(payload)]
    assert fake.last == (len(payload), "/test/note.md")


def test_dropbox_upload_with_null_bucket_does_not_crash(monkeypatch, tmp_path):
    """When max_upload_kbps is None, the backend must still upload
    without error — the NullBucket path is the unconfigured default."""
    from claude_mirror.backends.dropbox import DropboxBackend

    cfg = Config(
        project_path=str(tmp_path),
        backend="dropbox",
        dropbox_folder="/test",
        max_upload_kbps=None,
    )
    backend = DropboxBackend(cfg)

    class _FakeDbx:
        def files_upload(self, body, dest_path, mode):
            self.last = (len(body), dest_path)

    fake = _FakeDbx()
    backend._dbx = fake

    local = tmp_path / "note.md"
    local.write_bytes(b"x" * 50)
    backend.upload_file(str(local), "note.md", "/test", file_id="/test/note.md")
    assert fake.last == (50, "/test/note.md")
