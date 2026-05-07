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


def _setup_in_sync(engine, primary_backend, mirror_backend, project_dir, files: dict):
    """Set up `files` (rel_path -> str/bytes content) as fully in-sync:

      * write the file locally,
      * upload identical content to both primary and mirror,
      * record state=ok in the manifest with hashes that match what
        engine.get_status() will compute.

    Required because the new --by-backend renderer routes through
    engine.get_status(), which does a real 3-way diff (local hash vs
    primary remote hash vs manifest hash). Without this setup, files
    classify as NEW_LOCAL / CONFLICT / DRIVE_AHEAD — not the IN_SYNC
    baseline most cell-rendering tests want to start from.

    primary_backend's `backend_name` defaults to 'fake' (FakeStorageBackend's
    class default); mirror_backend's is 'sftp' (set by the fixture).
    """
    from pathlib import Path
    primary_name = getattr(primary_backend, "backend_name", "") or "fake"
    mirror_name = getattr(mirror_backend, "backend_name", "") or "sftp"
    for rel_path, content in files.items():
        if isinstance(content, str):
            content = content.encode("utf-8")
        local_path = Path(engine.config.project_path) / rel_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        sha = Manifest.hash_file(str(local_path))
        for backend, name in (
            (primary_backend, primary_name),
            (mirror_backend, mirror_name),
        ):
            parent_id, basename = backend.resolve_path(
                rel_path, backend.root_folder_id,
            )
            fid = backend.upload_bytes(content, basename, parent_id)
            md5 = backend._md5(content)
            engine.manifest.update_remote(
                rel_path, name,
                remote_file_id=fid,
                synced_remote_hash=md5,
                state="ok",
            )
        # Pin the FileState.synced_hash so engine.get_status's local-hash
        # comparison sees the file as not-locally-changed.
        fs = engine.manifest.get(rel_path)
        if fs:
            fs.synced_hash = sha
            engine.manifest._data[rel_path] = fs


def test_by_backend_renders_one_column_per_configured_backend(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """Header row should include both the primary and every mirror name,
    with the primary marked '(primary)' so users can tell which side
    drives pull/status."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # Both backend names appear in the header.
    assert "fake" in result.output
    assert "sftp" in result.output
    # Primary is labelled.
    assert "(primary)" in result.output


# ─── Cell rendering across each state ──────────────────────────────────────────

def test_by_backend_cell_renders_ok_when_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """File fully in-sync (local hash == primary's drive_hash == manifest's
    synced_hash, AND mirror has the file) → ✓ ok in BOTH backend columns."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # Both cells should render ✓ ok.
    assert result.output.count("ok") >= 2


def test_by_backend_cell_renders_pending_on_mirror(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """File in-sync on primary; mirror's manifest says pending_retry —
    the pending marker wins on the mirror cell."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    # Override sftp's manifest state to pending_retry post-setup.
    fs = patch_load_engine.manifest.get("a.md")
    fs.remotes["sftp"].state = "pending_retry"
    fs.remotes["sftp"].last_error = "429 rate limit"
    patch_load_engine.manifest._data["a.md"] = fs
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "pending" in result.output


def test_by_backend_cell_renders_failed_on_mirror(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """File in-sync on primary; mirror's manifest says failed_perm."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    fs = patch_load_engine.manifest.get("a.md")
    fs.remotes["sftp"].state = "failed_perm"
    fs.remotes["sftp"].last_error = "auth revoked"
    patch_load_engine.manifest._data["a.md"] = fs
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "failed" in result.output


def test_by_backend_cell_renders_unseeded_for_mirror_with_no_state(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """Primary in-sync; mirror has no manifest entry AND no live presence
    → '⊘ unseeded' on the mirror cell. Setup uploads to primary only,
    not mirror, then strips the mirror's manifest entry."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    # Strip sftp from mirror_backend's storage AND from manifest's remotes
    # — pretend we never seeded it.
    mirror_backend.files.clear()
    fs = patch_load_engine.manifest.get("a.md")
    fs.remotes.pop("sftp", None)
    patch_load_engine.manifest._data["a.md"] = fs
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "unseeded" in result.output


def test_by_backend_cell_renders_absent(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """Mirror manifest says state=absent (deliberate deletion);
    mirror's live listing confirms it's missing → '· absent'."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    mirror_backend.files.clear()
    fs = patch_load_engine.manifest.get("a.md")
    fs.remotes["sftp"].state = "absent"
    patch_load_engine.manifest._data["a.md"] = fs
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "absent" in result.output


def test_by_backend_cell_renders_deleted_for_out_of_band_removal(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """Manifest claims sftp=ok but the mirror's live listing shows the
    file is missing → '✗ deleted' (someone removed it via SSH/web UI)."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    # Wipe mirror storage but keep manifest's state=ok intact.
    mirror_backend.files.clear()
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert "deleted" in result.output


def test_by_backend_propagates_local_ahead_to_mirror_cells(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """Regression for the user-found bug: when local has unpushed changes
    (LOCAL_AHEAD on primary), mirror cells must reflect the same status —
    mirrors are write-replicas, so they trail primary identically."""
    from pathlib import Path
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n"})
    # Modify the local file so local_hash != manifest.synced_hash → LOCAL_AHEAD.
    (Path(patch_load_engine.config.project_path) / "a.md").write_text("alpha (modified locally)\n")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # Both primary and mirror columns should show 'local ahead'
    assert result.output.count("local ahead") >= 2


def test_by_backend_propagates_new_local_to_mirror_cells(patch_load_engine, fake_backend, mirror_backend, project_dir, write_files):
    """Files that exist locally but were never pushed to ANY backend
    show as '+ new local' on every column — they're not on any backend
    and not in the manifest."""
    write_files({"never-pushed.md": "fresh\n"})
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # Both backend columns reflect the same 'new local' status.
    assert result.output.count("new local") >= 2


# ─── Footer per-backend health summary ─────────────────────────────────────────

def test_by_backend_footer_shows_per_backend_counts(patch_load_engine, fake_backend, mirror_backend, project_dir):
    """Footer shows per-backend counts: primary has 2 ok files; mirror
    has 1 ok + 1 unseeded (b.md was synced on primary but never seeded on
    sftp)."""
    _setup_in_sync(patch_load_engine, fake_backend, mirror_backend, project_dir,
                   {"a.md": "alpha\n", "b.md": "beta\n"})
    # Strip sftp from b.md (live + manifest) so it shows as unseeded there.
    parent_id, _ = mirror_backend.resolve_path("b.md", mirror_backend.root_folder_id)
    mirror_backend.files = {
        fid: f for fid, f in mirror_backend.files.items()
        if not (f["name"] == "b.md" and f["parent_id"] == parent_id)
    }
    fs = patch_load_engine.manifest.get("b.md")
    fs.remotes.pop("sftp", None)
    patch_load_engine.manifest._data["b.md"] = fs
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

def test_by_backend_honors_exclude_patterns_on_live_listings(monkeypatch, make_config, fake_backend, mirror_backend, project_dir, write_files):
    """Regression for the user-found bug: --by-backend was showing files
    matched by exclude_patterns as 'unseeded' on mirrors, even though the
    user had explicitly excluded them. The engine's get_status() applies
    `self._is_excluded()` to its remote listing; the by-backend renderer
    must do the same so excluded files don't pollute the table.

    Setup: file_patterns includes `**/*.md` and `git/**`, exclude_patterns
    excludes `git/cortex-demo/.git/**`. A `.git/objects/abc123` file is
    seeded on the primary (simulating historical pushes) but should NOT
    appear in the per-backend table because it's explicitly excluded.
    """
    write_files({"a.md": "alpha\n"})
    cfg = make_config(
        file_patterns=["**/*.md", "git/**"],
        exclude_patterns=["git/cortex-demo/.git/**"],
    )
    engine = SyncEngine(
        config=cfg, storage=fake_backend, manifest=Manifest(cfg.project_path),
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[mirror_backend],
    )
    # Seed the excluded path on the primary (simulating an old push from
    # before exclusion was tightened, or a push from a different machine).
    parent_id, basename = fake_backend.resolve_path(
        "git/cortex-demo/.git/objects/abc123", fake_backend.root_folder_id,
    )
    fake_backend.upload_bytes(b"git-object-content", basename, parent_id)
    # Seed an ordinary tracked file too.
    parent_id, basename = fake_backend.resolve_path("a.md", fake_backend.root_folder_id)
    fake_backend.upload_bytes(b"alpha\n", basename, parent_id)
    h = Manifest.hash_file(str(project_dir / "a.md"))
    engine.manifest.update_remote("a.md", "fake", remote_file_id="f-a",
                                  synced_remote_hash=h, state="ok")

    monkeypatch.setattr(cli_module, "_load_engine",
                        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend))
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")

    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    # The excluded path MUST NOT appear in the rendered output.
    assert "git/cortex-demo/.git/objects/abc123" not in result.output
    # The legitimate tracked file SHOULD appear.
    assert "a.md" in result.output


def test_by_backend_empty_project_prints_helpful_message(monkeypatch, make_config, fake_backend, mirror_backend, project_dir):
    """Brand-new project with no local files AND nothing on any
    backend — the renderer surfaces a 'push something first' hint
    rather than an empty table.

    This is a different setup from `patch_load_engine` (which writes
    a file locally) because we want the genuinely-empty case here."""
    # Fresh empty project — no local files at all.
    cfg = make_config()
    engine = SyncEngine(
        config=cfg, storage=fake_backend, manifest=Manifest(cfg.project_path),
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[mirror_backend],
    )
    monkeypatch.setattr(cli_module, "_load_engine",
                        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend))
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")
    result = CliRunner().invoke(cli, ["status", "--by-backend"])
    assert result.exit_code == 0, result.output
    assert ("no files tracked" in result.output.lower()
            or "push something" in result.output.lower())
