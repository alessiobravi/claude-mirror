"""Tests for `SyncEngine.seed_mirror` and the `claude-mirror seed-mirror` CLI.

The bug this addresses: when a mirror is added to `mirror_config_paths`
on a project where files already exist on the primary, the manifest
records each file's sync state for the primary but has no entry at all
for the new mirror. Regular `push` has nothing to do (local hashes match
manifest), so the mirror folder stays empty and `status --pending`
reports "nothing pending" — a silent footgun.

Coverage:
    * Manifest helper `unseeded_for_backend` returns the expected set.
    * Engine `seed_mirror` happy-path: every unseeded file uploads.
    * Idempotent: re-running on a fully-seeded mirror is a no-op.
    * Drift safety: files where local hash != manifest hash are skipped
      with a warning rather than uploading mismatched content.
    * Dry-run lists without uploading.
    * Unknown backend name raises ValueError.
    * No mirrors configured: clean no-op (early return).
    * `status --pending` surfaces unseeded files (the visibility fix
      that exposes this gap to users).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.manifest import Manifest, RemoteState
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Manifest helper ───────────────────────────────────────────────────────────

def test_unseeded_for_backend_returns_files_with_no_recorded_state(make_config, project_dir, write_files):
    write_files({"a.md": "alpha", "b.md": "beta", "c.md": "gamma"})
    cfg = make_config()
    m = Manifest(cfg.project_path)

    # All three files have state on the primary but only one has SFTP state.
    m.update_remote("a.md", "googledrive", remote_file_id="ga", synced_remote_hash="ha", state="ok")
    m.update_remote("b.md", "googledrive", remote_file_id="gb", synced_remote_hash="hb", state="ok")
    m.update_remote("c.md", "googledrive", remote_file_id="gc", synced_remote_hash="hc", state="ok")
    m.update_remote("a.md", "sftp", remote_file_id="/srv/a.md", synced_remote_hash="ha", state="ok")

    unseeded = m.unseeded_for_backend("sftp")
    assert sorted(unseeded.keys()) == ["b.md", "c.md"]


def test_unseeded_for_backend_empty_when_fully_seeded(make_config, project_dir, write_files):
    write_files({"a.md": "alpha"})
    cfg = make_config()
    m = Manifest(cfg.project_path)
    m.update_remote("a.md", "sftp", remote_file_id="/srv/a.md", synced_remote_hash="ha", state="ok")
    assert m.unseeded_for_backend("sftp") == {}


# ─── Engine seed_mirror ────────────────────────────────────────────────────────

def _build_engine_with_mirror(make_config, fake_backend, project_dir, mirror_backend) -> SyncEngine:
    """Engine where fake_backend is the primary and `mirror_backend` is
    a single configured mirror. Both are FakeStorageBackend instances —
    the second carries `backend_name="sftp"` so the engine can find it
    by name."""
    cfg = make_config()
    return SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=Manifest(cfg.project_path),
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=[mirror_backend],
    )


@pytest.fixture
def mirror_backend(make_config):
    """A second FakeStorageBackend masquerading as the SFTP mirror.

    Carries its own Config (with `root_folder` resolving to the mirror's
    root folder ID) because the engine reads `target.config.root_folder`
    when uploading — same convention real backends follow.
    """
    from tests.conftest import FakeStorageBackend
    cfg = make_config(backend="sftp", drive_folder_id="sftp-root")
    m = FakeStorageBackend(root_folder_id="sftp-root")
    m.backend_name = "sftp"
    m.config = cfg
    return m


def test_seed_mirror_happy_path_uploads_every_unseeded_file(
    make_config, fake_backend, mirror_backend, project_dir, write_files,
):
    write_files({"a.md": "alpha\n", "b.md": "beta\n", "c.md": "gamma\n"})
    engine = _build_engine_with_mirror(make_config, fake_backend, project_dir, mirror_backend)

    # Pretend the primary already has all three files (state=ok on googledrive).
    # Use the actual hashes from the local files so the drift check passes.
    for rel, _ in [("a.md", "alpha\n"), ("b.md", "beta\n"), ("c.md", "gamma\n")]:
        local_hash = Manifest.hash_file(str(project_dir / rel))
        engine.manifest.update_remote(
            rel, "googledrive", remote_file_id=f"g-{rel}",
            synced_remote_hash=local_hash, state="ok",
        )
        # Also pin the FileState's synced_hash field — that's what the
        # drift check compares against.
        existing = engine.manifest.get(rel)
        existing.synced_hash = local_hash
        engine.manifest._data[rel] = existing

    # Pre-condition: nothing on the SFTP mirror.
    assert mirror_backend.files == {}
    assert engine.manifest.unseeded_for_backend("sftp") == {
        rel: engine.manifest.get(rel) for rel in ("a.md", "b.md", "c.md")
    }

    summary = engine.seed_mirror(backend_name="sftp")

    assert summary == {
        "total_unseeded": 3, "seeded": 3, "skipped_drift": 0, "failed": 0,
    }
    # Every file now has state=ok on the SFTP mirror in the manifest.
    for rel in ("a.md", "b.md", "c.md"):
        rs = engine.manifest.get(rel).get_remote("sftp")
        assert rs is not None and rs.state == "ok"
        assert rs.remote_file_id  # non-empty
    # And the mirror's in-memory storage actually received the bytes.
    assert len(mirror_backend.files) == 3


def test_seed_mirror_idempotent_when_already_seeded(
    make_config, fake_backend, mirror_backend, project_dir, write_files,
):
    write_files({"a.md": "alpha\n"})
    engine = _build_engine_with_mirror(make_config, fake_backend, project_dir, mirror_backend)
    local_hash = Manifest.hash_file(str(project_dir / "a.md"))
    engine.manifest.update_remote("a.md", "googledrive",
                                  remote_file_id="g-a", synced_remote_hash=local_hash, state="ok")
    engine.manifest.update_remote("a.md", "sftp",
                                  remote_file_id="/srv/a.md", synced_remote_hash=local_hash, state="ok")

    summary = engine.seed_mirror(backend_name="sftp")
    assert summary == {"total_unseeded": 0, "seeded": 0, "skipped_drift": 0, "failed": 0}
    # Nothing was uploaded to the mirror this run.
    assert mirror_backend.files == {}


def test_seed_mirror_skips_files_with_local_drift(
    make_config, fake_backend, mirror_backend, project_dir, write_files,
):
    write_files({"a.md": "current local content\n"})
    engine = _build_engine_with_mirror(make_config, fake_backend, project_dir, mirror_backend)

    # Manifest claims the file was last synced with a DIFFERENT hash —
    # i.e. local content has drifted since that sync. seed_mirror MUST
    # skip rather than upload mismatched content.
    fake_old_hash = "0" * 64
    engine.manifest.update_remote("a.md", "googledrive",
                                  remote_file_id="g-a", synced_remote_hash=fake_old_hash, state="ok")
    existing = engine.manifest.get("a.md")
    existing.synced_hash = fake_old_hash
    engine.manifest._data["a.md"] = existing

    summary = engine.seed_mirror(backend_name="sftp")
    assert summary == {
        "total_unseeded": 1, "seeded": 0, "skipped_drift": 1, "failed": 0,
    }
    # File stays unseeded on the mirror — user must reconcile via push first.
    assert engine.manifest.get("a.md").get_remote("sftp") is None
    assert mirror_backend.files == {}


def test_seed_mirror_dry_run_lists_without_uploading(
    make_config, fake_backend, mirror_backend, project_dir, write_files,
):
    write_files({"a.md": "alpha\n", "b.md": "beta\n"})
    engine = _build_engine_with_mirror(make_config, fake_backend, project_dir, mirror_backend)
    for rel, _ in [("a.md", "alpha\n"), ("b.md", "beta\n")]:
        local_hash = Manifest.hash_file(str(project_dir / rel))
        engine.manifest.update_remote(rel, "googledrive",
                                      remote_file_id=f"g-{rel}", synced_remote_hash=local_hash, state="ok")
        existing = engine.manifest.get(rel)
        existing.synced_hash = local_hash
        engine.manifest._data[rel] = existing

    summary = engine.seed_mirror(backend_name="sftp", dry_run=True)

    assert summary["total_unseeded"] == 2
    assert summary["seeded"] == 0
    assert mirror_backend.files == {}
    # Mirror state in manifest unchanged.
    for rel in ("a.md", "b.md"):
        assert engine.manifest.get(rel).get_remote("sftp") is None


def test_seed_mirror_unknown_backend_raises(
    make_config, fake_backend, mirror_backend, project_dir,
):
    engine = _build_engine_with_mirror(make_config, fake_backend, project_dir, mirror_backend)
    with pytest.raises(ValueError) as excinfo:
        engine.seed_mirror(backend_name="not-a-real-backend")
    assert "not-a-real-backend" in str(excinfo.value)


def test_seed_mirror_no_mirrors_configured_is_noop(
    make_config, fake_backend, project_dir,
):
    """When no mirrors are configured at all, seed-mirror exits cleanly
    with an explanatory message rather than raising."""
    cfg = make_config()
    engine = SyncEngine(
        config=cfg, storage=fake_backend, manifest=Manifest(cfg.project_path),
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[],
    )
    summary = engine.seed_mirror(backend_name="sftp")
    assert summary == {"total_unseeded": 0, "seeded": 0, "skipped_drift": 0, "failed": 0}


# ─── status --pending integration ──────────────────────────────────────────────

@pytest.fixture
def patch_load_engine_with_mirror(monkeypatch, make_config, fake_backend, mirror_backend, project_dir, write_files):
    """Replace cli._load_engine so status --pending exercises the
    real renderer against a manifest that has unseeded SFTP state."""
    write_files({"a.md": "alpha\n", "b.md": "beta\n"})
    cfg = make_config()
    engine = SyncEngine(
        config=cfg, storage=fake_backend, manifest=Manifest(cfg.project_path),
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[mirror_backend],
    )
    # Both files: in-sync on primary, unseeded on sftp mirror.
    for rel in ("a.md", "b.md"):
        h = Manifest.hash_file(str(project_dir / rel))
        engine.manifest.update_remote(rel, "googledrive", remote_file_id=f"g-{rel}",
                                      synced_remote_hash=h, state="ok")
        existing = engine.manifest.get(rel)
        existing.synced_hash = h
        engine.manifest._data[rel] = existing

    monkeypatch.setattr(cli_module, "_load_engine",
                        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend))
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")
    return engine


def test_status_pending_surfaces_unseeded_files(patch_load_engine_with_mirror):
    """Regression for the bug: pre-fix `status --pending` said
    'All mirrors are caught up' even when an entire mirror was empty.
    With the fix it must show an Unseeded-mirrors table with the
    suggested seed-mirror command."""
    result = CliRunner().invoke(cli, ["status", "--pending"])
    assert result.exit_code == 0, result.output
    assert "Unseeded mirrors" in result.output
    assert "sftp" in result.output
    assert "seed-mirror --backend sftp" in result.output
    # The pre-fix happy-path message must NOT appear when something is unseeded.
    assert "All mirrors are caught up" not in result.output


def test_status_pending_clean_when_everything_seeded(monkeypatch, make_config, fake_backend, mirror_backend, project_dir, write_files):
    write_files({"a.md": "alpha\n"})
    cfg = make_config()
    engine = SyncEngine(
        config=cfg, storage=fake_backend, manifest=Manifest(cfg.project_path),
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[mirror_backend],
    )
    h = Manifest.hash_file(str(project_dir / "a.md"))
    engine.manifest.update_remote("a.md", "googledrive", remote_file_id="g-a", synced_remote_hash=h, state="ok")
    engine.manifest.update_remote("a.md", "sftp", remote_file_id="/srv/a.md", synced_remote_hash=h, state="ok")

    monkeypatch.setattr(cli_module, "_load_engine",
                        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend))
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")

    result = CliRunner().invoke(cli, ["status", "--pending"])
    assert result.exit_code == 0, result.output
    assert "All mirrors are caught up" in result.output
    assert "Unseeded" not in result.output
