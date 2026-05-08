"""Tests for `claude-mirror snapshot-diff TS1 TS2`.

Covers both the SnapshotManager helpers (`get_snapshot_manifest`,
`get_blob_content`) and the CLI surface (`snapshot-diff TS1 TS2 [...]`):

  * blobs vs blobs:
      - added / removed / modified / unchanged classification
      - line counts via difflib (+N -M)
      - --all toggles inclusion of `unchanged` rows
      - --paths filters by glob
      - --unified emits `diff -u` for one file
  * full vs full and blobs vs full are both accepted
  * binary file in `modified` row → marked `binary`, no line count
  * `latest` keyword → resolves to most recent snapshot
  * unknown timestamp → red error + exit 1
  * identical snapshots → "snapshots are identical" message

All tests are offline (in-memory backend), <100ms each.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror import snapshots as snap_mod
from claude_mirror.cli import cli
from claude_mirror.snapshots import SnapshotManager

from tests.test_snapshots import InMemoryBackend, _make_manager

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def stepped_clock(monkeypatch):
    """Local copy of the same fixture in test_snapshots.py — patches
    snapshots.datetime so each call to .now() returns a value 1 minute
    after the previous one. Without this, two snapshots created back to
    back resolve to the same wall-clock second and the second one
    overwrites the first."""
    import datetime as _dt

    state = {"step": 0}
    base = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)

    class _SteppedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = base + _dt.timedelta(minutes=state["step"])
            state["step"] += 1
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    monkeypatch.setattr(snap_mod, "datetime", _SteppedDateTime)
    return state


def _flat(s: str) -> str:
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return re.sub(r"\s+", " ", no_ansi)


# ─── Library helpers (`get_snapshot_manifest`, `get_blob_content`) ───────────


def test_get_snapshot_manifest_blobs_format(
    make_config, write_files, project_dir,
):
    """Blobs snapshot → manifest dict has format=blobs and {path: sha256}."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "AAA", "b.md": "BBB"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    manifest = mgr.get_snapshot_manifest(ts)
    assert manifest["format"] == "blobs"
    assert manifest["timestamp"] == ts
    assert set(manifest["files"]) == {"a.md", "b.md"}
    # Every value is a 64-char SHA-256 hex digest.
    for h in manifest["files"].values():
        assert len(h) == 64
    # _backend should be the in-memory backend the manifest was served from.
    assert manifest["_backend"] is backend


def test_get_snapshot_manifest_full_format_carries_file_ids(
    make_config, write_files, project_dir,
):
    """Full snapshot → manifest dict has format=full and {path: file_id}."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="full")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "ALPHA"})
    backend.upload_bytes(b"ALPHA", "a.md", "ROOT")

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    manifest = mgr.get_snapshot_manifest(ts)
    assert manifest["format"] == "full"
    assert "a.md" in manifest["files"]
    file_id = manifest["files"]["a.md"]
    # In InMemoryBackend, file_ids start with "file-".
    assert file_id.startswith("file-")
    # We can fetch the body via get_blob_content(format_hint="full").
    body = mgr.get_blob_content(file_id, format_hint="full")
    assert body == b"ALPHA"


def test_get_blob_content_blobs_format_resolves_by_hash(
    make_config, write_files, project_dir,
):
    """get_blob_content(hash) downloads the body for a blobs snapshot."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "ALPHA-CONTENT"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])
    manifest = mgr.get_snapshot_manifest(ts)
    h = manifest["files"]["a.md"]

    body = mgr.get_blob_content(h)
    assert body == b"ALPHA-CONTENT"


def test_get_blob_content_blobs_format_unknown_hash_raises(
    make_config, write_files, project_dir,
):
    """An unknown hash → ValueError with a helpful prefix."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "A"})
    mgr = _make_manager(cfg, backend)
    mgr.create(action="push", files_changed=[])

    with pytest.raises(ValueError, match="not present"):
        mgr.get_blob_content("0" * 64)


# ─── CLI: blobs vs blobs ─────────────────────────────────────────────────────


@pytest.fixture
def two_blob_snapshots(make_config, write_files, project_dir, tmp_path,
                       monkeypatch, stepped_clock):
    """Make two blobs-format snapshots with controlled differences:
        ts1: a.md=A, b.md=B, delete-me.md=D
        ts2: a.md=AA (modified), b.md=B (unchanged), new.md=N (added)
        (delete-me.md is removed in ts2)

    Returns (ts1, ts2, cfg_path).
    """
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "A\nLINE2\n", "b.md": "BBB", "delete-me.md": "DEL"})

    mgr = _make_manager(cfg, backend)
    ts1 = mgr.create(action="push", files_changed=[])

    # Mutate the project for the second snapshot.
    (project_dir / "a.md").write_text("AA\nLINE2\nLINE3\n")
    (project_dir / "delete-me.md").unlink()
    (project_dir / "new.md").write_text("NEW")

    ts2 = mgr.create(action="push", files_changed=[])

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    return ts1, ts2, str(cfg_path), backend, mgr


def test_snapshot_diff_classifies_added_removed_modified(two_blob_snapshots):
    """Default output (no --all) shows added / removed / modified, not unchanged."""
    ts1, ts2, cfg_path, _backend, _mgr = two_blob_snapshots

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts1, ts2, "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    # added: new.md
    assert "new.md" in flat and "added" in flat
    # removed: delete-me.md
    assert "delete-me.md" in flat and "removed" in flat
    # modified: a.md
    assert "modified" in flat
    # unchanged b.md should NOT be in the table without --all.
    # The file name "b.md" might still appear in summary text? It does
    # not — only added/removed/modified rows render. Check for the row
    # marker:
    assert "│ b.md " not in flat and "| b.md " not in flat


def test_snapshot_diff_modified_shows_line_counts(two_blob_snapshots):
    """A modified row's `Changes` cell shows `+N -M` line counts."""
    ts1, ts2, cfg_path, _, _ = two_blob_snapshots

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts1, ts2, "--config", cfg_path],
    )
    flat = _flat(result.output)
    # a.md: A\nLINE2\n  →  AA\nLINE2\nLINE3\n
    # difflib.unified_diff sees: -A, +AA, +LINE3 (LINE2 is context)
    # So +2 -1.
    assert "+2" in flat
    assert "-1" in flat


def test_snapshot_diff_all_includes_unchanged(two_blob_snapshots):
    """--all adds `unchanged` rows for files identical between snapshots."""
    ts1, ts2, cfg_path, _, _ = two_blob_snapshots

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts1, ts2, "--all", "--config", cfg_path],
    )
    flat = _flat(result.output)
    assert "unchanged" in flat
    # b.md is the unchanged file — its row appears now.
    assert "│ b.md " in flat or "| b.md " in flat


def test_snapshot_diff_paths_filter(two_blob_snapshots):
    """--paths PATTERN restricts the table to matching files only."""
    ts1, ts2, cfg_path, _, _ = two_blob_snapshots

    result = CliRunner().invoke(
        cli,
        ["snapshot-diff", ts1, ts2, "--paths", "a.md", "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "a.md" in flat
    # Nothing else passes the filter.
    assert "delete-me.md" not in flat
    assert "new.md" not in flat


def test_snapshot_diff_unified_emits_diff_format(two_blob_snapshots):
    """--unified PATH emits a standard `diff -u`-format diff to stdout."""
    ts1, ts2, cfg_path, _, _ = two_blob_snapshots

    result = CliRunner().invoke(
        cli,
        ["snapshot-diff", ts1, ts2, "--unified", "a.md", "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    # Expect the `--- path@ts` / `+++ path@ts` headers + hunk markers.
    assert f"--- a.md@{ts1}" in result.output
    assert f"+++ a.md@{ts2}" in result.output
    assert "@@" in result.output  # hunk header
    # The unified-diff path uses click.echo, so no Rich markup leaks in.
    # Reasonable: exactly one `-A` line and one `+AA` line.
    lines = result.output.splitlines()
    assert any(line == "-A" for line in lines), result.output
    assert any(line == "+AA" for line in lines), result.output


def test_snapshot_diff_unified_missing_path_exits_1(two_blob_snapshots):
    """--unified for a path absent in BOTH snapshots → red error + exit 1."""
    ts1, ts2, cfg_path, _, _ = two_blob_snapshots
    result = CliRunner().invoke(
        cli,
        ["snapshot-diff", ts1, ts2, "--unified", "no-such.md",
         "--config", cfg_path],
    )
    assert result.exit_code == 1
    assert "not present" in _flat(result.output).lower()


def test_snapshot_diff_latest_keyword_resolves(two_blob_snapshots):
    """The keyword `latest` resolves to the most-recent snapshot."""
    ts1, ts2, cfg_path, _, _ = two_blob_snapshots

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts1, "latest", "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    # Same expected diff as the explicit-ts2 version — the ts2 string
    # appears in the table title as the "to" snapshot.
    assert ts2 in flat
    # No reference to literal "latest" in the rendered timestamp.
    assert "latest" not in flat


def test_snapshot_diff_unknown_timestamp_exits_1(two_blob_snapshots):
    """An unknown TIMESTAMP → exit 1 with a helpful red error message."""
    ts1, _, cfg_path, _, _ = two_blob_snapshots
    result = CliRunner().invoke(
        cli,
        ["snapshot-diff", ts1, "2099-01-01T00-00-00Z", "--config", cfg_path],
    )
    assert result.exit_code == 1
    assert "not found" in _flat(result.output).lower()


def test_snapshot_diff_identical_snapshots_prints_identical_message(
    make_config, write_files, project_dir, tmp_path, monkeypatch, stepped_clock,
):
    """Diffing a snapshot against itself reports they're identical."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "A"})
    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts, ts, "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    assert "identical" in _flat(result.output).lower()


# ─── full vs full + binary handling ──────────────────────────────────────────


def test_snapshot_diff_full_format_classifies_correctly(
    make_config, write_files, project_dir, tmp_path, monkeypatch, stepped_clock,
):
    """full-vs-full snapshots also classify added/removed/modified.
    full-format identifiers are file_ids (different across snapshots even
    when the content is byte-identical) so every file present in BOTH
    full snapshots shows as `modified` — that's a documented limitation
    of comparing two full snapshots without downloading bodies. Added
    and removed still classify correctly."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="full")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"keep.md": "KEEP", "drop-me.md": "DROP"})
    backend.upload_bytes(b"KEEP", "keep.md", "ROOT")
    backend.upload_bytes(b"DROP", "drop-me.md", "ROOT")

    mgr = _make_manager(cfg, backend)
    ts1 = mgr.create(action="push", files_changed=[])

    # Modify project + remote for ts2: full-format snapshots are
    # server-side copies of the remote tree, so the file must be removed
    # from remote (not just local) before the second create runs.
    (project_dir / "drop-me.md").unlink()
    drop_id = backend.get_file_id("drop-me.md", "ROOT")
    backend.delete_file(drop_id)
    (project_dir / "added.md").write_text("ADDED")
    backend.upload_bytes(b"ADDED", "added.md", "ROOT")

    ts2 = mgr.create(action="push", files_changed=[])

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts1, ts2, "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "added.md" in flat and "added" in flat
    assert "drop-me.md" in flat and "removed" in flat


def test_snapshot_diff_binary_modified_marked_binary(
    make_config, write_files, project_dir, tmp_path, monkeypatch, stepped_clock,
):
    """A file containing non-UTF-8 bytes in `modified` shows as `binary`
    in the Changes column — no line-count attempted."""
    cfg = make_config(
        drive_folder_id="ROOT", snapshot_format="blobs",
        file_patterns=["**/*"],   # include the binary file
    )
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    # Write the file directly via raw bytes so it isn't valid UTF-8.
    (project_dir / "data.bin").write_bytes(b"\xff\xfe\x00\x01ABC")

    mgr = _make_manager(cfg, backend)
    ts1 = mgr.create(action="push", files_changed=[])

    # Mutate the bytes so the second snapshot's hash differs.
    (project_dir / "data.bin").write_bytes(b"\xff\xfe\x00\x02DIFF")
    ts2 = mgr.create(action="push", files_changed=[])

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )

    result = CliRunner().invoke(
        cli, ["snapshot-diff", ts1, ts2, "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "data.bin" in flat
    assert "binary" in flat
