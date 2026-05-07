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

def _seed_on_backend(backend, rel_path: str, content: bytes = b"x"):
    """Upload a file directly to a FakeStorageBackend so its
    list_files_recursive picks it up — required now that --by-backend
    live-verifies remote presence rather than reading the manifest only."""
    parent_id, basename = backend.resolve_path(rel_path, backend.root_folder_id)
    return backend.upload_bytes(content, basename, parent_id)


def test_by_backend_renders_one_column_per_configured_backend(patch_load_engine, fake_backend):
    """Header row should include both the primary and every mirror name,
    with the primary marked '(primary)' so users can tell which side
    drives pull/status."""
    _seed_on_backend(fake_backend, "a.md")
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

def test_by_backend_cell_renders_ok_when_state_is_ok(patch_load_engine, fake_backend, mirror_backend):
    """File present on BOTH backends (live-verified) AND manifest says
    ok → renders as ✓ ok in both columns."""
    _seed_on_backend(fake_backend, "a.md")
    _seed_on_backend(mirror_backend, "a.md")
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="ok")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # ok cells render the ✓ ok marker for both backends — at least 2
    # occurrences (one per backend cell on the row).
    assert result.output.count("ok") >= 2


def test_by_backend_cell_renders_pending(patch_load_engine, fake_backend, mirror_backend):
    """File present on backends BUT manifest says pending_retry — the
    pending marker wins over ok because the manifest's recorded state
    captures intent that live presence alone can't (we tried, last
    attempt failed transiently, retry queued)."""
    _seed_on_backend(fake_backend, "a.md")
    _seed_on_backend(mirror_backend, "a.md")
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="pending_retry",
                                             last_error="429 rate limit")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "pending" in result.output


def test_by_backend_cell_renders_failed(patch_load_engine, fake_backend, mirror_backend):
    """File present on backends BUT manifest says failed_perm — failed
    marker wins."""
    _seed_on_backend(fake_backend, "a.md")
    _seed_on_backend(mirror_backend, "a.md")
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="failed_perm",
                                             last_error="auth revoked")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "failed" in result.output


def test_by_backend_cell_renders_unseeded_for_mirror_with_no_state(patch_load_engine, fake_backend):
    """The whole point of --by-backend with live verification: a file
    that's on the primary AND in the manifest BUT not on the mirror
    AND with no manifest entry for the mirror → 'unseeded' on the mirror.
    Note: mirror_backend is intentionally NOT seeded."""
    _seed_on_backend(fake_backend, "a.md")
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    # Note: NO update_remote for "sftp" AND no upload to mirror_backend
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "unseeded" in result.output


def test_by_backend_cell_renders_absent(patch_load_engine, fake_backend):
    """Manifest says state=absent on the mirror (claude-mirror knows the
    file was deliberately deleted there). The mirror's live listing
    confirms it isn't present. → '· absent' (not 'deleted', because we
    intended the absence)."""
    _seed_on_backend(fake_backend, "a.md")
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="absent")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "absent" in result.output


def test_by_backend_cell_renders_deleted_for_out_of_band_removal(patch_load_engine, fake_backend):
    """NEW STATE in the live-verified renderer: manifest says state=ok
    on the mirror, but the mirror's live listing shows the file is
    missing. Surfaced as '✗ deleted' to flag the divergence — this
    catches "someone removed the file directly via SSH/web-UI"."""
    _seed_on_backend(fake_backend, "a.md")
    # mirror_backend NOT seeded (file is "missing" on it)
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    # But manifest insists the file IS on sftp:
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="ok")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output


def test_by_backend_legacy_manifest_no_remotes_dict_renders_primary_as_ok(patch_load_engine, fake_backend):
    """v1/v2 manifest entries had `synced_hash` set but no `remotes`
    dict. With live verification, the primary cell renders as ✓ ok
    when the file is actually present on the backend, regardless of
    whether the manifest has per-backend remotes recorded for it."""
    _seed_on_backend(fake_backend, "a.md")
    fs = patch_load_engine.manifest.get("a.md")
    if fs is None:
        from claude_mirror.manifest import FileState
        fs = FileState()
    fs.synced_hash = "deadbeef"
    fs.remotes = {}  # explicitly no per-backend entries (legacy v1/v2 shape)
    patch_load_engine.manifest._data["a.md"] = fs
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "ok" in result.output


# ─── Footer per-backend health summary ─────────────────────────────────────────

def test_by_backend_footer_shows_per_backend_counts(patch_load_engine, fake_backend, mirror_backend):
    """Footer shows one summary line per backend with state counts.
    With live verification: fake primary has both files (live + manifest),
    sftp has a.md (live + manifest=ok) but not b.md (no manifest, no live)
    → primary 2 ok, sftp 1 ok + 1 unseeded."""
    from pathlib import Path
    (Path(patch_load_engine.config.project_path) / "b.md").write_text("beta\n")
    _seed_on_backend(fake_backend, "a.md")
    _seed_on_backend(fake_backend, "b.md")
    _seed_on_backend(mirror_backend, "a.md")
    # b.md NOT uploaded to mirror_backend → live shows missing
    patch_load_engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                             synced_remote_hash="ha", state="ok")
    patch_load_engine.manifest.update_remote("b.md", "fake", remote_file_id="f-b",
                                             synced_remote_hash="hb", state="ok")
    patch_load_engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a",
                                             synced_remote_hash="ha", state="ok")
    # b.md has NO manifest entry for sftp AND no live presence → unseeded
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
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
