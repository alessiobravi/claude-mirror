"""Tests for snapshot retention policies (`prune_per_retention` + CLI prune).

Covers:
    * Config: four new keep_* fields default to 0 (disabled).
    * The `prune_per_retention` algorithm:
        - all-zero policy is a no-op
        - keep_last keeps the N newest
        - keep_daily picks newest-per-day for the last N days
        - keep_monthly picks newest-per-month for the last N months
        - keep_yearly picks newest-per-year for the last N years
        - composing all four unions correctly (no double-deletes)
        - dry_run=True writes nothing
        - dry_run=False deletes via _forget_one
    * CLI `claude-mirror prune`:
        - dry-run by default (no --delete = no writes)
        - --delete + --yes deletes
        - per-flag override of config fields
        - empty config + no flags = clear error message

Tests use a stub-friendly subclass of SnapshotManager that mocks `list()`
and `_forget_one()` so the algorithm is exercised in isolation from the
storage backend.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.config import Config
from claude_mirror.snapshots import SnapshotManager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Config defaults ───────────────────────────────────────────────────────────

def test_retention_fields_default_to_zero(make_config):
    cfg = make_config()
    assert cfg.keep_last == 0
    assert cfg.keep_daily == 0
    assert cfg.keep_monthly == 0
    assert cfg.keep_yearly == 0


def test_retention_fields_round_trip_via_yaml(tmp_path, make_config):
    cfg = make_config(keep_last=7, keep_daily=14, keep_monthly=12, keep_yearly=5)
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    loaded = Config.load(str(cfg_path))
    assert loaded.keep_last == 7
    assert loaded.keep_daily == 14
    assert loaded.keep_monthly == 12
    assert loaded.keep_yearly == 5


# ─── Algorithm tests via a stub SnapshotManager ────────────────────────────────

class _StubSnapshotManager(SnapshotManager):
    """Subclass that lets tests drive the retention algorithm directly
    without going through real storage. Override `list()` to return
    test snapshots; `_forget_one()` records calls instead of actually
    deleting."""

    def __init__(self, snapshots: List[dict], config: Config):
        # Bypass SnapshotManager.__init__ entirely — its real backend
        # plumbing is irrelevant to retention algorithm tests. Set just
        # the attributes the prune path touches.
        self.config = config
        self.storage = MagicMock()
        self._snapshots_to_return = snapshots
        self.deleted_timestamps: List[str] = []

    def list(self, _external_progress=None) -> List[dict]:
        return list(self._snapshots_to_return)

    def _forget_one(self, snap: dict) -> None:
        self.deleted_timestamps.append(snap["timestamp"])


def _ts(year: int, month: int = 1, day: int = 1, hour: int = 0) -> str:
    """Build a snapshot timestamp string like '2026-04-07T15-22-50Z'."""
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}-00-00Z"


def _snap(ts: str, fmt: str = "blobs") -> dict:
    return {"timestamp": ts, "format": fmt, "manifest_id": f"m-{ts}", "total_files": 1}


def test_prune_all_zero_policy_is_noop(make_config):
    snaps = [_snap(_ts(2026, 5, 7))]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(dry_run=False)
    assert out == {
        "selected": 0, "deleted": 0, "errors": 0,
        "to_keep": [], "to_delete": [],
    }
    assert mgr.deleted_timestamps == []


def test_prune_keep_last_keeps_newest_n(make_config):
    # Newest-first ordering matches what list() returns.
    snaps = [
        _snap(_ts(2026, 5, 7)),
        _snap(_ts(2026, 5, 6)),
        _snap(_ts(2026, 5, 5)),
        _snap(_ts(2026, 5, 4)),
        _snap(_ts(2026, 5, 3)),
    ]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(keep_last=3, dry_run=False)
    assert out["selected"] == 2
    assert out["deleted"] == 2
    assert mgr.deleted_timestamps == [_ts(2026, 5, 4), _ts(2026, 5, 3)]


def test_prune_keep_daily_one_per_day(make_config):
    # Three snapshots on day 5, two on day 4, one on day 3. With
    # keep_daily=2 we should keep newest-of-day-5 + newest-of-day-4 = 2
    # snapshots; everything else (4 snapshots) deleted.
    snaps = [
        _snap(_ts(2026, 5, 5, hour=12)),
        _snap(_ts(2026, 5, 5, hour=10)),
        _snap(_ts(2026, 5, 5, hour=8)),
        _snap(_ts(2026, 5, 4, hour=14)),
        _snap(_ts(2026, 5, 4, hour=9)),
        _snap(_ts(2026, 5, 3, hour=11)),
    ]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(keep_daily=2, dry_run=False)
    assert out["selected"] == 4
    # Kept: newest of day 5 (hour 12), newest of day 4 (hour 14).
    assert sorted(out["to_keep"]) == sorted([
        _ts(2026, 5, 5, hour=12),
        _ts(2026, 5, 4, hour=14),
    ])


def test_prune_keep_monthly_one_per_month(make_config):
    snaps = [
        _snap(_ts(2026, 5, 7)),
        _snap(_ts(2026, 5, 1)),
        _snap(_ts(2026, 4, 30)),
        _snap(_ts(2026, 4, 15)),
        _snap(_ts(2026, 3, 1)),
        _snap(_ts(2026, 2, 1)),
    ]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(keep_monthly=2, dry_run=False)
    # Keep newest-of-may + newest-of-april = 2 entries.
    assert sorted(out["to_keep"]) == sorted([
        _ts(2026, 5, 7),
        _ts(2026, 4, 30),
    ])
    assert out["deleted"] == 4


def test_prune_keep_yearly_one_per_year(make_config):
    snaps = [
        _snap(_ts(2026, 5, 7)),
        _snap(_ts(2026, 1, 1)),
        _snap(_ts(2025, 12, 31)),
        _snap(_ts(2025, 1, 1)),
        _snap(_ts(2024, 6, 1)),
    ]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(keep_yearly=2, dry_run=False)
    # Newest of 2026 + newest of 2025.
    assert sorted(out["to_keep"]) == sorted([
        _ts(2026, 5, 7),
        _ts(2025, 12, 31),
    ])


def test_prune_composes_all_four_buckets_via_union(make_config):
    # Compose with policy: keep_last=1, keep_daily=2, keep_monthly=2,
    # keep_yearly=2. Each selector picks newest-in-each-bucket walking
    # snapshots newest-first. The kept set is the union of every
    # selector's picks.
    snaps = [
        _snap(_ts(2026, 5, 7, hour=15)),  # keep_last #1, keep_daily (2026-5-7), keep_monthly (2026-5), keep_yearly (2026)
        _snap(_ts(2026, 5, 7, hour=10)),  # NOT picked: same daily bucket as the previous one
        _snap(_ts(2026, 5, 6, hour=12)),  # keep_daily #2 (newest of 2026-5-6)
        _snap(_ts(2026, 4, 30, hour=12)), # keep_monthly #2 (newest of 2026-4)
        _snap(_ts(2025, 12, 31, hour=12)),# keep_yearly #2 (newest of 2025)
        _snap(_ts(2024, 6, 1, hour=12)),  # NOT in any bucket — outside keep_yearly=2 and all others
    ]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(
        keep_last=1, keep_daily=2, keep_monthly=2, keep_yearly=2,
        dry_run=False,
    )
    # Union keeps: 2026-05-07@15, 2026-05-06@12, 2026-04-30, 2025-12-31.
    # The two left out: 2026-05-07@10 (same daily bucket as the kept @15)
    # and 2024-06-01 (outside every bucket).
    assert sorted(mgr.deleted_timestamps) == sorted([
        _ts(2026, 5, 7, hour=10),
        _ts(2024, 6, 1, hour=12),
    ])
    assert out["deleted"] == 2


def test_prune_dry_run_writes_nothing(make_config):
    snaps = [_snap(_ts(2026, 5, 7)), _snap(_ts(2026, 5, 1))]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(keep_last=1, dry_run=True)
    assert out["selected"] == 1
    assert out["deleted"] == 0
    assert mgr.deleted_timestamps == []


def test_prune_no_snapshots_is_clean_noop(make_config):
    mgr = _StubSnapshotManager([], make_config())
    out = mgr.prune_per_retention(keep_last=10, dry_run=False)
    assert out == {
        "selected": 0, "deleted": 0, "errors": 0,
        "to_keep": [], "to_delete": [],
    }


def test_prune_when_everything_inside_keep_set_returns_empty_delete(make_config):
    snaps = [_snap(_ts(2026, 5, 7)), _snap(_ts(2026, 5, 6))]
    mgr = _StubSnapshotManager(snaps, make_config())
    out = mgr.prune_per_retention(keep_last=10, dry_run=False)
    # 2 snapshots, keep_last=10 → keep all, delete none.
    assert out["selected"] == 0
    assert out["deleted"] == 0
    assert sorted(out["to_keep"]) == sorted([s["timestamp"] for s in snaps])


# ─── CLI prune command ─────────────────────────────────────────────────────────

@pytest.fixture
def yaml_config(tmp_path, project_dir, config_dir, make_config):
    """A real YAML config file on disk with retention fields set."""
    cfg = make_config(keep_last=2)
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    return str(cfg_path)


@pytest.fixture
def stub_snapshot_manager(monkeypatch):
    """Replace SnapshotManager construction in cli.py so the prune
    command exercises the algorithm without real storage."""
    instances: List[_StubSnapshotManager] = []
    snaps = [
        _snap(_ts(2026, 5, 7)),
        _snap(_ts(2026, 5, 6)),
        _snap(_ts(2026, 5, 5)),
        _snap(_ts(2026, 5, 4)),
    ]

    def factory(config, storage, *_, **__):
        mgr = _StubSnapshotManager(snaps, config)
        instances.append(mgr)
        return mgr

    monkeypatch.setattr(cli_module, "SnapshotManager", factory)
    monkeypatch.setattr(cli_module, "_create_storage", lambda cfg: MagicMock())
    return instances


def test_cli_prune_dry_run_by_default(yaml_config, stub_snapshot_manager):
    result = CliRunner().invoke(cli, ["prune", "--config", yaml_config])
    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output
    # No deletions happened
    assert all(m.deleted_timestamps == [] for m in stub_snapshot_manager)


def test_cli_prune_with_delete_and_yes_actually_deletes(yaml_config, stub_snapshot_manager):
    result = CliRunner().invoke(
        cli, ["prune", "--delete", "--yes", "--config", yaml_config]
    )
    assert result.exit_code == 0, result.output
    # keep_last=2 from config + 4 snapshots = delete 2
    assert sum(len(m.deleted_timestamps) for m in stub_snapshot_manager) == 2


def test_cli_prune_flag_overrides_config(yaml_config, stub_snapshot_manager):
    # Config has keep_last=2; pass --keep-last 1 → should delete 3, not 2
    result = CliRunner().invoke(
        cli,
        ["prune", "--keep-last", "1", "--delete", "--yes", "--config", yaml_config],
    )
    assert result.exit_code == 0, result.output
    assert sum(len(m.deleted_timestamps) for m in stub_snapshot_manager) == 3


def test_cli_prune_no_policy_set_errors_clearly(tmp_path, project_dir, config_dir, make_config, stub_snapshot_manager):
    # Build a config with all four keep_* still at 0
    cfg = make_config()
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    result = CliRunner().invoke(cli, ["prune", "--config", str(cfg_path)])
    assert result.exit_code == 1
    assert "no retention policy" in result.output.lower()


def test_cli_prune_typed_yes_required_without_yes_flag(yaml_config, stub_snapshot_manager):
    # Without --yes, the typed-YES prompt fires; piping "no" must abort.
    result = CliRunner().invoke(
        cli, ["prune", "--delete", "--config", yaml_config], input="no\n"
    )
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    # No snapshots should have been deleted
    assert all(m.deleted_timestamps == [] for m in stub_snapshot_manager)


def test_cli_prune_keep_last_zero_explicit_treated_as_off(tmp_path, make_config, stub_snapshot_manager):
    # Config sets keep_last=2 in YAML, but passing --keep-last 0 explicitly
    # disables that bucket. With no other keep_* set, no policy is active.
    cfg = make_config(keep_last=2)
    cfg_path = tmp_path / "cfg.yaml"
    cfg.save(str(cfg_path))
    result = CliRunner().invoke(
        cli,
        ["prune", "--keep-last", "0", "--config", str(cfg_path)],
    )
    assert result.exit_code == 1
    assert "no retention policy" in result.output.lower()
