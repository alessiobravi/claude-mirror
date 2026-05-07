"""Tests for the shared internal constants module (`_constants.py`).

Verifies that `PARALLEL_WORKERS` is defined ONCE in `_constants` and
re-exported by reference (not re-declared) from `sync` and `snapshots`.
A regression that re-declares the value in either module would break the
`is` identity assertion below."""
from __future__ import annotations

from claude_mirror import _constants, snapshots, sync


def test_parallel_workers_imported_from_constants() -> None:
    """`sync.PARALLEL_WORKERS` and `snapshots.PARALLEL_WORKERS` must be the
    same object as `_constants.PARALLEL_WORKERS` — i.e. imported by
    reference, not re-declared. Use `is`, not `==`."""
    assert sync.PARALLEL_WORKERS is _constants.PARALLEL_WORKERS
    assert snapshots.PARALLEL_WORKERS is _constants.PARALLEL_WORKERS
    assert sync.PARALLEL_WORKERS is snapshots.PARALLEL_WORKERS


def test_parallel_workers_value_sane() -> None:
    """Sanity: the constant is a positive int. If someone bumps it to a
    huge number or sets it to 0, ThreadPoolExecutor will misbehave."""
    assert isinstance(_constants.PARALLEL_WORKERS, int)
    assert _constants.PARALLEL_WORKERS >= 1
