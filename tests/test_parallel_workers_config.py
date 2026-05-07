"""Tests for the per-project `parallel_workers` Config field.

Power users on different machines have different appropriate concurrency
levels (slow CPUs prefer fewer workers, fat home connections prefer more,
rate-limited APIs prefer fewer). Before D3 the only way to tune
concurrency was to edit `claude_mirror._constants.PARALLEL_WORKERS`.
This module verifies that:

1. The new `Config.parallel_workers` field defaults to 5 (matching the
   `_constants.PARALLEL_WORKERS` fallback used by non-config-aware
   call sites).
2. YAML overrides are honoured.
3. Edge values (e.g. 0) round-trip through the dataclass unchanged —
   any safety clamping is the caller's responsibility, not the
   storage layer.
4. The constant itself is unchanged so it remains a safe fallback.
5. SyncEngine actually consults `config.parallel_workers` when
   building a ThreadPoolExecutor (not the legacy module constant).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor as _RealThreadPoolExecutor
from typing import Any

from claude_mirror import sync as sync_mod
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine


def test_parallel_workers_default_is_five(make_config) -> None:
    """Default matches the existing PARALLEL_WORKERS constant — installs
    without the field set behave identically to today."""
    cfg = make_config()
    assert cfg.parallel_workers == 5


def test_parallel_workers_override_respected(make_config) -> None:
    """YAML override flows through the dataclass."""
    cfg = make_config(parallel_workers=12)
    assert cfg.parallel_workers == 12


def test_parallel_workers_zero_is_accepted_at_config_level(make_config) -> None:
    """The dataclass itself stores whatever value is given; engine-level
    safety is enforced at the use site, not at storage."""
    cfg = make_config(parallel_workers=0)
    assert cfg.parallel_workers == 0


def test_parallel_workers_constant_unchanged_for_fallback() -> None:
    """The shared constant remains 5 and continues to be importable —
    any non-config-aware call site keeps using it as a safe fallback."""
    from claude_mirror._constants import PARALLEL_WORKERS

    assert PARALLEL_WORKERS == 5


def test_sync_engine_uses_config_parallel_workers_value(
    make_config, fake_backend, monkeypatch
) -> None:
    """End-to-end: instantiate a SyncEngine with parallel_workers=3,
    drive a method that dispatches a ThreadPoolExecutor, and assert the
    `max_workers` kwarg passed to ThreadPoolExecutor is 3 (NOT 5).

    Wraps `claude_mirror.sync.ThreadPoolExecutor` so we can capture
    construction kwargs without losing real executor behaviour."""
    captured: list[dict[str, Any]] = []

    def _wrapped_executor(*args: Any, **kwargs: Any) -> _RealThreadPoolExecutor:
        captured.append({"args": args, "kwargs": dict(kwargs)})
        return _RealThreadPoolExecutor(*args, **kwargs)

    monkeypatch.setattr(sync_mod, "ThreadPoolExecutor", _wrapped_executor)

    cfg = make_config(parallel_workers=3)
    engine = SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=Manifest(cfg.project_path),
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=[],
    )

    # `_parallel` constructs ThreadPoolExecutor(max_workers=workers) where
    # workers = min(self.config.parallel_workers, len(items)). With 4
    # items and parallel_workers=3, the cap is parallel_workers.
    items = ["a", "b", "c", "d"]
    succeeded, failed = engine._parallel(items, fn=lambda x: None)

    # `_parallel` collects results in `as_completed` order, which is
    # nondeterministic across threads; compare as sets.
    assert set(succeeded) == set(items)
    assert failed == []
    assert captured, "ThreadPoolExecutor was never instantiated"
    # The first executor built inside _parallel is the one we care about.
    first = captured[0]
    assert first["kwargs"].get("max_workers") == 3, (
        f"expected max_workers=3 (config.parallel_workers), "
        f"got {first['kwargs'].get('max_workers')}"
    )
