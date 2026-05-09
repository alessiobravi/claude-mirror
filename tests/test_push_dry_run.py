"""Tests for `SyncEngine.push(dry_run=True)` and `claude-mirror push --dry-run`.

Covers:
  * Engine: dry-run classifies LOCAL_AHEAD / NEW_LOCAL / DELETED_LOCAL /
    CONFLICT / DRIVE_AHEAD / NEW_DRIVE / IN_SYNC into the right buckets.
  * Engine: dry-run does NOT call upload_file / upload_bytes / delete_file
    on the backend, and does NOT write the manifest file to disk.
  * Engine: `paths=` filter narrows the plan.
  * CLI: `claude-mirror push --dry-run` exits 0, prints a summary, and
    leaves the manifest file unchanged on disk.

All tests are offline (in-memory backend), <100ms each.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli
from claude_mirror.manifest import MANIFEST_FILE, Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import PushPlan, Status, SyncEngine

from tests.test_sync_engine import InMemoryBackend


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def _build_engine(config, backend: InMemoryBackend) -> SyncEngine:
    manifest = Manifest(config.project_path)
    return SyncEngine(
        config=config, storage=backend, manifest=manifest,
        merge=MergeHandler(), notifier=None, snapshots=None, mirrors=[],
    )


# ─── Engine layer — happy path ─────────────────────────────────────────────────


def test_push_dry_run_classifies_local_ahead_as_upload(make_config, write_files):
    """A LOCAL_AHEAD file goes into to_upload, with byte total."""
    write_files({"a.md": "v2"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v1")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert isinstance(plan, PushPlan)
    assert plan.to_upload == ["a.md"]
    assert plan.to_delete == []
    assert plan.conflicts == []
    assert plan.upload_bytes == 2  # "v2"


def test_push_dry_run_classifies_new_local_as_upload(make_config, write_files):
    """A NEW_LOCAL file (local-only, no manifest) goes into to_upload."""
    write_files({"new.md": "hello"})
    backend = InMemoryBackend()
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert plan.to_upload == ["new.md"]
    assert plan.upload_bytes == 5


def test_push_dry_run_classifies_deleted_local_as_delete(make_config):
    """A locally-deleted file (still on remote, manifest entry exists) goes
    into to_delete, NOT to_upload."""
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v1")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert plan.to_delete == ["a.md"]
    assert plan.to_upload == []


def test_push_dry_run_classifies_conflict(make_config, write_files):
    """A CONFLICT (both sides changed) goes into conflicts, not to_upload."""
    write_files({"a.md": "local-version"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"remote-version")
    cfg = make_config()
    h = _md5(b"baseline")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert plan.conflicts == ["a.md"]
    assert plan.to_upload == []
    assert plan.to_delete == []


def test_push_dry_run_classifies_in_sync_as_skipped(make_config, write_files):
    """An IN_SYNC file goes into skipped — push leaves it alone."""
    write_files({"a.md": "v1"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v1")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert plan.skipped == ["a.md"]
    assert plan.to_upload == []


def test_push_dry_run_remote_only_files_classified_as_skipped(
    make_config, write_files,
):
    """A NEW_DRIVE file (remote-only) is NOT something push acts on — it
    should land in skipped, not in to_upload or conflicts."""
    backend = InMemoryBackend()
    backend.seed("remote-only.md", b"R")
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert plan.skipped == ["remote-only.md"]
    assert plan.to_upload == []
    assert plan.conflicts == []


def test_push_dry_run_mixed_states(make_config, write_files):
    """Mixed project: one upload, one delete, one conflict, one in-sync,
    one new-drive (skipped)."""
    write_files({"upload.md": "U2", "conflict.md": "L"})
    backend = InMemoryBackend()
    fid_u = backend.seed("upload.md", b"U1")
    fid_c = backend.seed("conflict.md", b"R")
    fid_d = backend.seed("delete.md", b"D")
    backend.seed("new-drive.md", b"X")
    cfg = make_config()

    m = Manifest(cfg.project_path)
    h_u = _md5(b"U1")
    m.update("upload.md", h_u, fid_u, synced_remote_hash=h_u, backend_name="fake")
    h_baseline = _md5(b"baseline")
    m.update("conflict.md", h_baseline, fid_c, synced_remote_hash=h_baseline,
             backend_name="fake")
    h_d = _md5(b"D")
    m.update("delete.md", h_d, fid_d, synced_remote_hash=h_d, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.push(dry_run=True)

    assert plan.to_upload == ["upload.md"]
    assert plan.to_delete == ["delete.md"]
    assert plan.conflicts == ["conflict.md"]
    assert "new-drive.md" in plan.skipped


def test_push_dry_run_paths_filter_narrows_plan(make_config, write_files):
    """`paths=['a.md']` keeps only that file in the plan."""
    write_files({"a.md": "AA", "b.md": "BB"})
    backend = InMemoryBackend()
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    plan = eng.push(["a.md"], dry_run=True)

    assert plan.to_upload == ["a.md"]


# ─── Engine layer — side-effect absence ────────────────────────────────────────


def test_push_dry_run_does_not_call_upload(make_config, write_files):
    """dry_run=True does NOT invoke any of the backend's write methods."""
    write_files({"a.md": "hi", "b.md": "bye"})
    backend = InMemoryBackend()
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    eng.push(dry_run=True)

    method_names = [c[0] for c in backend.calls]
    assert "upload_file" not in method_names
    assert "upload_bytes" not in method_names
    assert "delete_file" not in method_names
    assert "copy_file" not in method_names


def test_push_dry_run_does_not_write_manifest_file(make_config, write_files):
    """dry_run=True does NOT create or modify the on-disk manifest."""
    write_files({"a.md": "hi"})
    backend = InMemoryBackend()
    cfg = make_config()
    manifest_path = Path(cfg.project_path) / MANIFEST_FILE
    assert not manifest_path.exists()

    eng = _build_engine(cfg, backend)
    eng.push(dry_run=True)

    assert not manifest_path.exists()


def test_push_dry_run_leaves_existing_manifest_unchanged(make_config, write_files):
    """When a manifest already exists on disk, dry-run must not touch its
    bytes — even though `Manifest.save()` happens after a real push."""
    write_files({"a.md": "v2"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v1")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    manifest_path = Path(cfg.project_path) / MANIFEST_FILE
    before_bytes = manifest_path.read_bytes()
    before_mtime = manifest_path.stat().st_mtime_ns

    eng = _build_engine(cfg, backend)
    eng.push(dry_run=True)

    after_bytes = manifest_path.read_bytes()
    assert before_bytes == after_bytes
    # mtime check is the strongest signal — a save() that wrote identical
    # bytes would still bump it.
    assert manifest_path.stat().st_mtime_ns == before_mtime


def test_push_dry_run_returns_plan_real_run_returns_none(make_config, write_files):
    """The keyword-only `dry_run` toggle changes the return type — real
    runs still return None, preserving the existing public contract."""
    write_files({"a.md": "x"})
    backend = InMemoryBackend()
    cfg = make_config()
    eng = _build_engine(cfg, backend)

    plan = eng.push(dry_run=True)
    assert isinstance(plan, PushPlan)
    # Real run returns None (existing contract).
    real_result = eng.push()
    assert real_result is None


# ─── CLI layer ─────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_setup(make_config, write_files, tmp_path, monkeypatch):
    """Build a config + on-disk YAML so the CLI can load it, then patch
    `_create_storage_set` so it returns the in-memory backend.
    Returns (cfg_path, project_dir, backend)."""
    write_files({"upload.md": "U", "in-sync.md": "S"})
    backend = InMemoryBackend()
    fid_s = backend.seed("in-sync.md", b"S")
    cfg = make_config()

    h_s = _md5(b"S")
    m = Manifest(cfg.project_path)
    m.update("in-sync.md", h_s, fid_s, synced_remote_hash=h_s, backend_name="fake")
    m.save()

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    monkeypatch.setattr(cli_module, "_create_notifier", lambda c, s: None)
    return str(cfg_path), Path(cfg.project_path), backend


def _flat(s: str) -> str:
    """Strip ANSI + collapse whitespace so substring asserts survive Rich
    table wrapping at narrow terminal widths."""
    import re
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return re.sub(r"\s+", " ", no_ansi)


def test_cli_push_dry_run_exits_zero_and_prints_summary(cli_setup):
    cfg_path, _project_dir, _backend = cli_setup
    result = CliRunner().invoke(cli, ["push", "--dry-run", "--config", cfg_path])
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Would upload" in flat
    assert "Run without --dry-run to apply." in flat
    assert "upload.md" in flat


def test_cli_push_dry_run_does_not_modify_manifest_on_disk(cli_setup):
    """Manifest bytes on disk are byte-identical before vs after the
    --dry-run invocation."""
    cfg_path, project_dir, _backend = cli_setup
    manifest_path = project_dir / MANIFEST_FILE
    before = manifest_path.read_bytes()
    before_mtime = manifest_path.stat().st_mtime_ns

    result = CliRunner().invoke(cli, ["push", "--dry-run", "--config", cfg_path])
    assert result.exit_code == 0, result.output

    assert manifest_path.read_bytes() == before
    assert manifest_path.stat().st_mtime_ns == before_mtime


def test_cli_push_dry_run_does_not_call_backend_writes(cli_setup):
    cfg_path, _project_dir, backend = cli_setup
    CliRunner().invoke(cli, ["push", "--dry-run", "--config", cfg_path])
    method_names = [c[0] for c in backend.calls]
    assert "upload_file" not in method_names
    assert "upload_bytes" not in method_names
    assert "delete_file" not in method_names


def test_cli_push_dry_run_nothing_to_push(make_config, write_files, tmp_path,
                                          monkeypatch):
    """When everything is in sync, dry-run prints a 'nothing to push' line."""
    write_files({"a.md": "X"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"X")
    cfg = make_config()
    h = _md5(b"X")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    monkeypatch.setattr(cli_module, "_create_notifier", lambda c, s: None)

    result = CliRunner().invoke(
        cli, ["push", "--dry-run", "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "nothing to push" in flat.lower()


def test_cli_push_no_dry_run_default_still_runs_actual_push(make_config,
                                                             write_files,
                                                             tmp_path,
                                                             monkeypatch):
    """Without --dry-run the existing behaviour is preserved."""
    write_files({"new.md": "PAYLOAD"})
    backend = InMemoryBackend()
    cfg = make_config()
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    monkeypatch.setattr(cli_module, "_create_notifier", lambda c, s: None)

    result = CliRunner().invoke(cli, ["push", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    method_names = [c[0] for c in backend.calls]
    assert "upload_file" in method_names
