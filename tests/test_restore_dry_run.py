"""Tests for `claude-mirror restore --dry-run`.

Covers both the `SnapshotManager.plan_restore()` library helper and the
CLI surface (`claude-mirror restore TIMESTAMP --dry-run`):

  * blobs format — every file shows up with its hash + size
  * full format  — every file shows up with its size + file_id surrogate
  * `paths=` filter narrows the plan to matching files only
  * `paths=` filter with no matches returns an empty file list
  * blobs with a missing blob on remote → row marked `missing-blob`
  * unknown timestamp → ValueError (CLI prints + exit 1)
  * CLI: --dry-run prints summary + does NOT touch local disk
  * CLI: --dry-run respects --paths filter

All tests are offline (in-memory backend), <100ms each.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli
from claude_mirror.snapshots import (
    BLOBS_FOLDER,
    SnapshotManager,
)

# Reuse the fully-functional in-memory backend from the snapshots test
# module rather than re-implementing it. This is the same model real
# backends present (folders + files share an ID space, list_files_recursive
# walks descendants, delete_file cascades to children).
from tests.test_snapshots import InMemoryBackend, _make_manager

# Click 9's CliRunner surfaces a `protected_args` DeprecationWarning when
# the `_CLIGroup.invoke` peeks at ctx.protected_args. The harness treats
# that as a non-zero exit; silence it the same way test_retention.py does.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── plan_restore() — blobs format ────────────────────────────────────────────


def test_plan_restore_blobs_lists_every_file(
    make_config, write_files, project_dir,
):
    """Every file in the snapshot shows up in the plan with hash + size."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "alpha", "nested/b.md": "beta"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    plan = mgr.plan_restore(ts)
    assert plan["timestamp"] == ts
    assert plan["format"] == "blobs"
    assert plan["source_backend"] == "primary"
    assert plan["total_in_snapshot"] == 2
    assert plan["matched"] == 2

    paths_in_plan = {f["path"] for f in plan["files"]}
    assert paths_in_plan == {"a.md", "nested/b.md"}
    for f in plan["files"]:
        assert f["action"] == "restore"
        # Blobs format: hash is a 64-char SHA-256 hex digest.
        assert isinstance(f["hash"], str) and len(f["hash"]) == 64
        # `inspect()` for blobs returns only path + hash (no size in the
        # manifest), so size is left as None in the plan — verify the
        # contract stays explicit rather than silently surfacing 0.
        assert f["size"] is None


def test_plan_restore_blobs_filters_by_paths(
    make_config, write_files, project_dir,
):
    """Passing `paths=['a.md']` narrows the plan to one matching file."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "AAA", "b.md": "BBB", "c.md": "CCC"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    plan = mgr.plan_restore(ts, paths=["a.md"])
    assert plan["total_in_snapshot"] == 3
    assert plan["matched"] == 1
    assert [f["path"] for f in plan["files"]] == ["a.md"]


def test_plan_restore_blobs_glob_filter_matches_subdir(
    make_config, write_files, project_dir,
):
    """Glob `'memory/**'` matches every file under memory/."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({
        "memory/x.md": "X", "memory/y.md": "Y",
        "other.md": "Z",
    })

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    plan = mgr.plan_restore(ts, paths=["memory/**"])
    paths = {f["path"] for f in plan["files"]}
    assert paths == {"memory/x.md", "memory/y.md"}
    assert plan["matched"] == 2


def test_plan_restore_blobs_paths_with_no_matches(
    make_config, write_files, project_dir,
):
    """A filter that matches nothing returns an empty `files` list."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "A"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    plan = mgr.plan_restore(ts, paths=["nonexistent/**"])
    assert plan["matched"] == 0
    assert plan["files"] == []
    assert plan["total_in_snapshot"] == 1


def test_plan_restore_blobs_flags_missing_blob(
    make_config, write_files, project_dir,
):
    """When a blob referenced by the manifest is no longer on remote, the
    plan flags that row as `missing-blob` rather than `restore`."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "A", "b.md": "B"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    # Wipe one blob from the backend to simulate a `gc --delete` that
    # ran AFTER the snapshot was taken (blobs orphaned by `forget`).
    blobs_id = backend.get_or_create_folder(BLOBS_FOLDER, "ROOT")
    blobs = backend.list_files_recursive(blobs_id)
    backend.delete_file(blobs[0]["id"])

    plan = mgr.plan_restore(ts)
    actions = {f["path"]: f["action"] for f in plan["files"]}
    # Exactly one path is now flagged missing-blob; the other still
    # resolves to a present blob.
    assert sorted(actions.values()) == ["missing-blob", "restore"]


# ─── plan_restore() — full format ─────────────────────────────────────────────


def test_plan_restore_full_lists_every_file(
    make_config, write_files, project_dir,
):
    """Full-format snapshots → every file in the plan, size from the
    snapshot folder listing, action=restore."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="full")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    # Pre-populate remote with the same files so `copy_file` has sources.
    write_files({"a.md": "ALPHA", "b.md": "BETA"})
    backend.upload_bytes(b"ALPHA", "a.md", "ROOT")
    backend.upload_bytes(b"BETA", "b.md", "ROOT")

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    plan = mgr.plan_restore(ts)
    assert plan["format"] == "full"
    paths = {f["path"] for f in plan["files"]}
    assert paths == {"a.md", "b.md"}
    for f in plan["files"]:
        assert f["action"] == "restore"
        # Full-format inspect carries size + id (not hash).
        assert f["hash"] in (None, "")
        # Sizes match the bodies above.
        assert f["size"] in (5, 4)


# ─── Error path ───────────────────────────────────────────────────────────────


def test_plan_restore_unknown_timestamp_raises_value_error(
    make_config, write_files, project_dir,
):
    """An unknown timestamp falls through both v2 + v1 probes → ValueError.
    (Same exception path as `restore()`.)"""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "A"})

    mgr = _make_manager(cfg, backend)
    mgr.create(action="push", files_changed=[])

    with pytest.raises(ValueError, match="not found"):
        mgr.plan_restore("2099-01-01T00-00-00Z")


# ─── CLI surface ──────────────────────────────────────────────────────────────


@pytest.fixture
def cli_setup(make_config, write_files, project_dir, tmp_path, monkeypatch):
    """Build a SnapshotManager + on-disk YAML so the CLI can load it,
    then patch `_create_storage_set` so it returns the in-memory backend.
    Returns (timestamp, yaml_path)."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    backend = InMemoryBackend(name="primary", root_folder="ROOT")
    write_files({"a.md": "AAA", "memory/notes.md": "NOTES"})

    mgr = _make_manager(cfg, backend)
    ts = mgr.create(action="push", files_changed=[])

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    return ts, str(cfg_path), project_dir


def _strip_ansi_and_newlines(s: str) -> str:
    """Collapse ANSI escapes + all whitespace so substring assertions are
    robust against Rich's hard wrapping at terminal width."""
    import re
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return re.sub(r"\s+", " ", no_ansi)


def test_cli_restore_dry_run_prints_summary_and_skips_writes(cli_setup):
    """--dry-run prints the plan + leaves local disk untouched."""
    ts, cfg_path, project_dir = cli_setup

    # Wipe local and run dry-run — local must NOT be re-created.
    (project_dir / "a.md").unlink()

    result = CliRunner().invoke(
        cli, ["restore", ts, "--dry-run", "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    flat = _strip_ansi_and_newlines(result.output)
    assert "Would restore" in flat
    assert "Run without --dry-run to apply." in flat
    # File is still gone — dry-run wrote nothing.
    assert not (project_dir / "a.md").exists()


def test_cli_restore_dry_run_respects_paths_filter(cli_setup):
    """A path argument narrows the dry-run plan."""
    ts, cfg_path, _ = cli_setup
    result = CliRunner().invoke(
        cli, ["restore", ts, "memory/notes.md", "--dry-run", "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    flat = _strip_ansi_and_newlines(result.output)
    # Only the matching file appears in the table.
    assert "memory/notes.md" in flat
    # The unrelated file's name doesn't appear in the body. Some Rich
    # wrappers might hyphenate path components — keep the substring
    # specific (`a.md │` only renders for an actual table row, not
    # inside the timestamp / summary text).
    assert "│ a.md " not in flat and "| a.md " not in flat
    assert "Would restore 1 file" in flat


def test_cli_restore_dry_run_unknown_timestamp_exits_1(cli_setup):
    """Unknown TIMESTAMP → red error + exit 1, no traceback."""
    _, cfg_path, _ = cli_setup
    result = CliRunner().invoke(
        cli,
        ["restore", "2099-01-01T00-00-00Z", "--dry-run", "--config", cfg_path],
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_cli_restore_no_dry_run_default_still_runs_actual_restore(
    cli_setup, tmp_path,
):
    """Without --dry-run the existing behaviour stands: files get written
    (here we point at --output to avoid the typed-confirm prompt that
    fires when target == project_path)."""
    ts, cfg_path, _ = cli_setup
    out_dir = tmp_path / "recovery"
    result = CliRunner().invoke(
        cli, ["restore", ts, "--output", str(out_dir), "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "a.md").read_bytes() == b"AAA"
    assert (out_dir / "memory" / "notes.md").read_bytes() == b"NOTES"
