"""Tests for `claude-mirror status --by-backend`.

The per-file table with one column per configured backend (primary first,
mirrors in mirror_config_paths order). Each cell shows that backend's
recorded state for the file: ok / pending / failed / unseeded / absent.

Coverage:
    * One column per backend, primary first.
    * Cell rendering for each state (ok / pending / failed / unseeded / absent).
    * Legacy v1/v2 manifest entries (no remotes dict) render primary as ok.
    * Footer per-backend health summary with correct counts.
    * --by-backend and --pending mutually exclusive.
    * Empty manifest gives a helpful message instead of an empty table.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Fixture: engine with primary + one mirror, manifest pre-populated ─────────

@pytest.fixture
def mirror_backend(make_config):
    from tests.conftest import FakeStorageBackend
    cfg = make_config(backend="sftp", drive_folder_id="sftp-root")
    m = FakeStorageBackend(root_folder_id="sftp-root")
    m.backend_name = "sftp"
    m.config = cfg
    return m


@pytest.fixture
def patch_load_engine(monkeypatch, make_config, fake_backend, mirror_backend, project_dir, write_files):
    """Engine wired with primary (fake_backend, name='fake') + one
    mirror (mirror_backend, name='sftp'). Manifest is empty by default;
    individual tests populate it with the per-file state combinations
    they want to render."""
    write_files({"a.md": "alpha\n"})  # at least one local file to pin project layout
    cfg = make_config()
    engine = SyncEngine(
        config=cfg, storage=fake_backend, manifest=Manifest(cfg.project_path),
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[mirror_backend],
    )
    monkeypatch.setattr(cli_module, "_load_engine",
                        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend))
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")
    return engine


# ─── Header / column structure ─────────────────────────────────────────────────

def test_by_backend_renders_one_column_per_configured_backend(patch_load_engine):
    """Header row should include both the primary and every mirror name,
    with the primary marked '(primary)' so users can tell which side
    drives pull/status."""
    patch_load_engine.manifest.update_remote(
        "a.md", "fake", remote_file_id="f-a", synced_remote_hash="ha", state="ok",
    )
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # Both backend names appear in the header.
    assert "fake" in result.output
    assert "sftp" in result.output
    # Primary is labelled.
    assert "(primary)" in result.output


# ─── Cell rendering across each state ──────────────────────────────────────────

def test_by_backend_cell_renders_ok_when_state_is_ok(patch_load_engine):
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="ok")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # ok cells render the ✓ ok marker for both backends — there should
    # be at least 2 occurrences (one per backend cell on the row).
    assert result.output.count("ok") >= 2


def test_by_backend_cell_renders_pending(patch_load_engine):
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="pending_retry",
                                             last_error="429 rate limit")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "pending" in result.output


def test_by_backend_cell_renders_failed(patch_load_engine):
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="failed_perm",
                                             last_error="auth revoked")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "failed" in result.output


def test_by_backend_cell_renders_unseeded_for_mirror_with_no_state(patch_load_engine):
    """The whole point of --by-backend: a file with primary state but
    no recorded state on a mirror renders as 'unseeded' on that mirror."""
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    # Note: NO update_remote for "sftp"
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "unseeded" in result.output


def test_by_backend_cell_renders_absent(patch_load_engine):
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="absent")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "absent" in result.output


def test_by_backend_legacy_manifest_no_remotes_dict_renders_primary_as_ok(patch_load_engine):
    """v1/v2 manifest entries had `synced_hash` set but no `remotes`
    dict. The renderer should still mark the primary as ok rather than
    reporting it as unseeded — the file IS present, just tracked via
    the legacy flat fields."""
    # Set synced_hash directly without going through update_remote (the
    # manifest helper would populate remotes for us).
    fs = patch_load_engine.manifest.get("a.md")
    if fs is None:
        from claude_mirror.manifest import FileState
        fs = FileState()
    fs.synced_hash = "deadbeef"
    fs.remotes = {}  # explicitly no per-backend entries
    patch_load_engine.manifest._data["a.md"] = fs
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


# ─── Footer per-backend health summary ─────────────────────────────────────────

def test_by_backend_footer_shows_per_backend_counts(patch_load_engine):
    """Footer should show one summary line per backend with state counts."""
    # 2 files, both ok on primary, one ok and one unseeded on sftp.
    fs1_hash = "ha"
    fs2_hash = "hb"
    from pathlib import Path
    (Path(patch_load_engine.config.project_path) / "b.md").write_text("beta\n")
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash=fs1_hash, state="ok")
    patch_load_engine.manifest.update_remote("b.md", "fake", remote_file_id="f-b",
                                             synced_remote_hash=fs2_hash, state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash=fs1_hash, state="ok")
    # b.md NOT seeded on sftp
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # The fake primary should report 2 ok; sftp should report 1 ok + 1 unseeded.
    assert "2 ok" in result.output
    assert "1 unseeded" in result.output


# ─── Mutually-exclusive flags ──────────────────────────────────────────────────

def test_pending_and_by_backend_are_mutually_exclusive(patch_load_engine):
    result = CliRunner().invoke(cli, ["status", "--pending", "--by-backend"])
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output.lower()


# ─── Empty manifest ────────────────────────────────────────────────────────────

def test_by_backend_empty_manifest_prints_helpful_message(patch_load_engine):
    """Brand-new project with nothing pushed yet — the manifest is
    empty. The renderer should NOT print an empty table; instead,
    surface a clear hint."""
    # patch_load_engine left the manifest empty (we never called update_remote).
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "no files tracked" in result.output.lower() or "push something" in result.output.lower()
