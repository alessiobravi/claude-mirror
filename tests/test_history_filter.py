"""Tests for `claude-mirror history --since DATE --until DATE`.

Covers both the SnapshotManager.history(since=, until=) library helper
and the CLI surface (`history PATH --since ... --until ...`):

  * default (no flags) → every snapshot scanned, behaviour unchanged
  * --since narrows to recent half
  * --until narrows to early half
  * both flags compose to a window
  * inclusive on both bounds (a snapshot taken exactly on the boundary
    is included)
  * relative durations (Nd / Nw / Nm / Ny) accepted
  * malformed --since / --until → red error + exit 1
  * --since later than --until → red error + exit 1
  * empty filter result → "no snapshots contain" message + active filter

All tests are offline (in-memory backend), <100ms each.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror import snapshots as snap_mod
from claude_mirror.cli import cli
from claude_mirror.snapshots import SnapshotManager, parse_relative_or_iso_date

from tests.test_snapshots import InMemoryBackend, _make_manager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _flat(s: str) -> str:
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return re.sub(r"\s+", " ", no_ansi)


# ─── parse_relative_or_iso_date — shared with `forget --before` ──────────────


def test_parse_iso_date_returns_utc():
    dt = parse_relative_or_iso_date("2026-04-15", flag_label="--since")
    assert dt == datetime(2026, 4, 15, tzinfo=timezone.utc)


def test_parse_iso_datetime_with_z_suffix():
    dt = parse_relative_or_iso_date("2026-04-15T10:30:00Z", flag_label="--since")
    assert dt == datetime(2026, 4, 15, 10, 30, 0, tzinfo=timezone.utc)


def test_parse_relative_duration_days():
    dt = parse_relative_or_iso_date("30d", flag_label="--since")
    delta = datetime.now(timezone.utc) - dt
    # 30d ± 1s tolerance for clock skew between parse and assertion.
    assert 30 * 24 * 3600 - 5 < delta.total_seconds() < 30 * 24 * 3600 + 5


def test_parse_relative_duration_weeks_months_years():
    dt_w = parse_relative_or_iso_date("2w", flag_label="--since")
    dt_m = parse_relative_or_iso_date("3m", flag_label="--since")
    dt_y = parse_relative_or_iso_date("1y", flag_label="--since")
    now = datetime.now(timezone.utc)
    assert (now - dt_w).days == 14
    assert (now - dt_m).days == 90
    assert (now - dt_y).days == 365


def test_parse_invalid_value_raises_with_flag_label():
    with pytest.raises(ValueError, match="--until"):
        parse_relative_or_iso_date("not-a-date", flag_label="--until")


def test_parse_empty_value_raises():
    with pytest.raises(ValueError, match="empty value"):
        parse_relative_or_iso_date("", flag_label="--since")


# ─── SnapshotManager.history(since=, until=) ─────────────────────────────────


def _seed_snapshots_with_path(
    backend: InMemoryBackend, timestamps: list[str], path: str = "f.md",
) -> None:
    """Inject N blobs-format snapshots whose manifests all reference
    `path` (so history() finds the file in every snapshot). Uses the
    same SHA-256 across all snapshots → one distinct version."""
    import hashlib
    import json

    from claude_mirror.snapshots import (
        BLOBS_FOLDER, MANIFEST_SUFFIX, SNAPSHOTS_FOLDER,
    )

    snaps_id = backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    blobs_id = backend.get_or_create_folder(BLOBS_FOLDER, "ROOT")

    body = b"hello"
    sha = hashlib.sha256(body).hexdigest()
    # Place the blob once.
    parent_id, filename = backend.resolve_path(
        f"{sha[:2]}/{sha}", blobs_id,
    )
    backend.upload_bytes(body, filename, parent_id)

    for ts in timestamps:
        manifest = {
            "format": "v2",
            "timestamp": ts,
            "files": {path: sha},
            "total_files": 1,
        }
        backend.upload_bytes(
            json.dumps(manifest).encode(),
            f"{ts}{MANIFEST_SUFFIX}",
            snaps_id,
        )


def test_history_no_filter_scans_every_snapshot(make_config, project_dir):
    """No --since / --until → every snapshot containing the path is returned."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    _seed_snapshots_with_path(
        backend,
        [
            "2026-01-01T00-00-00Z",
            "2026-03-15T00-00-00Z",
            "2026-05-01T00-00-00Z",
        ],
    )
    mgr = _make_manager(cfg, backend)

    result = mgr.history("f.md")
    assert result["total_appearances"] == 3


def test_history_since_filters_to_after(make_config, project_dir):
    """--since 2026-04-01 → only snapshots on or after that date."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    _seed_snapshots_with_path(
        backend,
        [
            "2026-01-01T00-00-00Z",
            "2026-03-15T00-00-00Z",
            "2026-05-01T00-00-00Z",
        ],
    )
    mgr = _make_manager(cfg, backend)

    since = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = mgr.history("f.md", since=since)
    timestamps = [e["timestamp"] for e in result["entries"]]
    assert timestamps == ["2026-05-01T00-00-00Z"]


def test_history_until_filters_to_before(make_config, project_dir):
    """--until 2026-04-01 → only snapshots on or before that date."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    _seed_snapshots_with_path(
        backend,
        [
            "2026-01-01T00-00-00Z",
            "2026-03-15T00-00-00Z",
            "2026-05-01T00-00-00Z",
        ],
    )
    mgr = _make_manager(cfg, backend)

    until = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = mgr.history("f.md", until=until)
    timestamps = sorted(e["timestamp"] for e in result["entries"])
    assert timestamps == ["2026-01-01T00-00-00Z", "2026-03-15T00-00-00Z"]


def test_history_since_and_until_compose(make_config, project_dir):
    """Both bounds → an inclusive [since, until] window."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    _seed_snapshots_with_path(
        backend,
        [
            "2026-01-01T00-00-00Z",
            "2026-03-15T00-00-00Z",
            "2026-05-01T00-00-00Z",
        ],
    )
    mgr = _make_manager(cfg, backend)

    since = datetime(2026, 2, 1, tzinfo=timezone.utc)
    until = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = mgr.history("f.md", since=since, until=until)
    timestamps = [e["timestamp"] for e in result["entries"]]
    assert timestamps == ["2026-03-15T00-00-00Z"]


def test_history_inclusive_on_exact_boundary(make_config, project_dir):
    """A snapshot taken exactly at `since` is included (and same for `until`)."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    _seed_snapshots_with_path(
        backend, ["2026-04-01T00-00-00Z", "2026-05-01T00-00-00Z"],
    )
    mgr = _make_manager(cfg, backend)

    # since == 2026-04-01 must include the 2026-04-01 snapshot.
    boundary = datetime(2026, 4, 1, tzinfo=timezone.utc)
    result = mgr.history("f.md", since=boundary, until=boundary)
    timestamps = [e["timestamp"] for e in result["entries"]]
    assert timestamps == ["2026-04-01T00-00-00Z"]


# ─── CLI surface ─────────────────────────────────────────────────────────────


@pytest.fixture
def cli_history_setup(
    make_config, write_files, project_dir, tmp_path, monkeypatch,
):
    """Build a config + on-remote snapshots covering Jan, Mar, May 2026,
    each containing 'f.md'. Patch _create_storage so the CLI sees them."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    _seed_snapshots_with_path(
        backend,
        [
            "2026-01-01T00-00-00Z",
            "2026-03-15T00-00-00Z",
            "2026-05-01T00-00-00Z",
        ],
    )
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    return str(cfg_path)


def test_cli_history_no_filter_lists_all(cli_history_setup):
    """No --since / --until: all three snapshots show up."""
    result = CliRunner().invoke(
        cli, ["history", "f.md", "--config", cli_history_setup],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "2026-01-01T00-00-00Z" in flat
    assert "2026-03-15T00-00-00Z" in flat
    assert "2026-05-01T00-00-00Z" in flat


def test_cli_history_since_iso_date(cli_history_setup):
    """--since 2026-04-01 → only the May snapshot remains."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md", "--since", "2026-04-01",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "2026-05-01T00-00-00Z" in flat
    assert "2026-01-01T00-00-00Z" not in flat
    assert "2026-03-15T00-00-00Z" not in flat


def test_cli_history_until_iso_date(cli_history_setup):
    """--until 2026-04-01 → only the Jan + Mar snapshots remain."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md", "--until", "2026-04-01",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "2026-01-01T00-00-00Z" in flat
    assert "2026-03-15T00-00-00Z" in flat
    assert "2026-05-01T00-00-00Z" not in flat


def test_cli_history_since_until_window(cli_history_setup):
    """Both flags compose to an inclusive [since, until] window."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md",
         "--since", "2026-02-01", "--until", "2026-04-01",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "2026-03-15T00-00-00Z" in flat
    assert "2026-01-01T00-00-00Z" not in flat
    assert "2026-05-01T00-00-00Z" not in flat


def test_cli_history_invalid_since_exits_1(cli_history_setup):
    """Malformed --since → red error + exit 1, no traceback."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md", "--since", "tomorrow",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 1
    flat = _flat(result.output).lower()
    assert "--since" in flat
    assert "cannot parse" in flat


def test_cli_history_since_after_until_exits_1(cli_history_setup):
    """--since later than --until → red error + exit 1 with explanation."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md",
         "--since", "2026-05-01", "--until", "2026-01-01",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 1
    flat = _flat(result.output).lower()
    assert "later than" in flat


def test_cli_history_empty_filter_result_shows_filter_hint(cli_history_setup):
    """A filter that excludes every snapshot prints the active range so
    the user can debug their query."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md",
         "--since", "2099-01-01", "--until", "2099-12-31",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 0
    flat = _flat(result.output)
    assert "No snapshots contain" in flat
    assert "Active filter" in flat
    # The exact UTC timestamp echoes back so the user sees what was parsed.
    assert "2099-01-01" in flat
    assert "2099-12-31" in flat


def test_cli_history_relative_since_accepted(cli_history_setup):
    """Relative durations (Nd / Nw / Nm / Ny) are accepted by --since.
    A 100-year window picks up every snapshot on remote (current date
    minus 100y is well before 2026)."""
    result = CliRunner().invoke(
        cli,
        ["history", "f.md", "--since", "100y",
         "--config", cli_history_setup],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    # All three appear (the 100-year window is wide enough).
    assert "2026-01-01T00-00-00Z" in flat
    assert "2026-05-01T00-00-00Z" in flat
