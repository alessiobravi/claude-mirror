"""Tests for `SyncEngine.pull(dry_run=True)` and `claude-mirror pull --dry-run`.

Covers:
  * Engine: dry-run classifies DRIVE_AHEAD / NEW_DRIVE / LOCAL_AHEAD /
    NEW_LOCAL / IN_SYNC into the right buckets — pull only acts on the
    first two, everything else lands in `skipped`.
  * Engine: dry-run does NOT call download_file / upload_file / delete_file
    on the backend, and does NOT write the manifest file to disk.
  * Engine: `paths=` filter narrows the plan.
  * CLI: `claude-mirror pull --dry-run` exits 0, prints a summary, and
    leaves the manifest file unchanged on disk.

All tests are offline (in-memory backend), <100ms each.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli
from claude_mirror.manifest import MANIFEST_FILE, Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import PullPlan, SyncEngine

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


def test_pull_dry_run_classifies_drive_ahead_as_download(make_config, write_files):
    """A DRIVE_AHEAD file goes into to_download, with byte total."""
    write_files({"a.md": "v1"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v2-changed")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert isinstance(plan, PullPlan)
    assert plan.to_download == ["a.md"]
    assert plan.download_bytes == len(b"v2-changed")


def test_pull_dry_run_classifies_new_drive_as_download(make_config):
    """A NEW_DRIVE file (remote-only, no local, no manifest) → to_download."""
    backend = InMemoryBackend()
    backend.seed("new-remote.md", b"PAYLOAD")
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert plan.to_download == ["new-remote.md"]
    assert plan.download_bytes == 7


def test_pull_dry_run_local_ahead_is_skipped(make_config, write_files):
    """A LOCAL_AHEAD file is irrelevant to pull — it lands in skipped."""
    write_files({"a.md": "v2-local-newer"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v1")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert plan.to_download == []
    assert plan.skipped == ["a.md"]


def test_pull_dry_run_new_local_is_skipped(make_config, write_files):
    """A NEW_LOCAL file (local-only) is irrelevant to pull → skipped."""
    write_files({"local-only.md": "X"})
    backend = InMemoryBackend()
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert plan.to_download == []
    assert plan.skipped == ["local-only.md"]


def test_pull_dry_run_in_sync_is_skipped(make_config, write_files):
    """An IN_SYNC file → skipped."""
    write_files({"a.md": "v1"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v1")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert plan.to_download == []
    assert plan.skipped == ["a.md"]


def test_pull_dry_run_conflict_is_skipped_not_downloaded(make_config, write_files):
    """A CONFLICT — pull leaves alone (sync is the command for conflicts)."""
    write_files({"a.md": "L"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"R")
    cfg = make_config()
    h = _md5(b"baseline")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert plan.to_download == []
    assert plan.skipped == ["a.md"]


def test_pull_dry_run_mixed_states(make_config, write_files):
    """Mixed: one drive-ahead (download), one new-drive (download), one
    in-sync (skipped), one local-ahead (skipped)."""
    write_files({"in-sync.md": "S", "local-ahead.md": "L2", "drive-ahead.md": "DA1"})
    backend = InMemoryBackend()
    fid_s = backend.seed("in-sync.md", b"S")
    fid_la = backend.seed("local-ahead.md", b"L1")
    fid_da = backend.seed("drive-ahead.md", b"DA2")
    backend.seed("new-remote.md", b"NEW")
    cfg = make_config()

    h_s = _md5(b"S")
    m = Manifest(cfg.project_path)
    m.update("in-sync.md", h_s, fid_s, synced_remote_hash=h_s, backend_name="fake")
    h_la = _md5(b"L1")
    m.update("local-ahead.md", h_la, fid_la, synced_remote_hash=h_la,
             backend_name="fake")
    h_da_baseline = _md5(b"DA1")
    m.update("drive-ahead.md", h_da_baseline, fid_da,
             synced_remote_hash=h_da_baseline, backend_name="fake")
    m.save()

    eng = _build_engine(cfg, backend)
    plan = eng.pull(dry_run=True)

    assert sorted(plan.to_download) == ["drive-ahead.md", "new-remote.md"]
    assert "in-sync.md" in plan.skipped
    assert "local-ahead.md" in plan.skipped


def test_pull_dry_run_paths_filter_narrows_plan(make_config):
    """`paths=['a.md']` keeps only that remote file in the plan."""
    backend = InMemoryBackend()
    backend.seed("a.md", b"AA")
    backend.seed("b.md", b"BB")
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    plan = eng.pull(["a.md"], dry_run=True)

    assert plan.to_download == ["a.md"]


# ─── Engine layer — side-effect absence ────────────────────────────────────────


def test_pull_dry_run_does_not_call_download(make_config):
    """dry_run=True does NOT invoke any of the backend's read/write
    methods that would actually move bytes (download_file in particular)."""
    backend = InMemoryBackend()
    backend.seed("a.md", b"hi")
    backend.seed("b.md", b"bye")
    cfg = make_config()
    eng = _build_engine(cfg, backend)
    eng.pull(dry_run=True)

    method_names = [c[0] for c in backend.calls]
    assert "download_file" not in method_names
    assert "upload_file" not in method_names
    assert "upload_bytes" not in method_names
    assert "delete_file" not in method_names


def test_pull_dry_run_does_not_write_manifest_file(make_config):
    """dry_run=True does NOT create or modify the on-disk manifest."""
    backend = InMemoryBackend()
    backend.seed("a.md", b"hi")
    cfg = make_config()
    manifest_path = Path(cfg.project_path) / MANIFEST_FILE
    assert not manifest_path.exists()

    eng = _build_engine(cfg, backend)
    eng.pull(dry_run=True)

    assert not manifest_path.exists()


def test_pull_dry_run_leaves_existing_manifest_unchanged(make_config, write_files):
    """When a manifest already exists on disk, dry-run must not touch its
    bytes — even though `Manifest.save()` happens after a real pull."""
    write_files({"a.md": "v1"})
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"v2-newer")
    cfg = make_config()
    h = _md5(b"v1")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    manifest_path = Path(cfg.project_path) / MANIFEST_FILE
    before_bytes = manifest_path.read_bytes()
    before_mtime = manifest_path.stat().st_mtime_ns

    eng = _build_engine(cfg, backend)
    eng.pull(dry_run=True)

    assert manifest_path.read_bytes() == before_bytes
    assert manifest_path.stat().st_mtime_ns == before_mtime


def test_pull_dry_run_does_not_write_to_local_disk(make_config):
    """A NEW_DRIVE file remains absent locally after dry-run."""
    backend = InMemoryBackend()
    backend.seed("would-be-pulled.md", b"PAYLOAD")
    cfg = make_config()
    project_root = Path(cfg.project_path)
    assert not (project_root / "would-be-pulled.md").exists()

    eng = _build_engine(cfg, backend)
    eng.pull(dry_run=True)

    assert not (project_root / "would-be-pulled.md").exists()


def test_pull_dry_run_returns_plan_real_run_returns_none(make_config):
    """Same return-type contract as push: dry_run=True → PullPlan, real → None."""
    backend = InMemoryBackend()
    backend.seed("a.md", b"x")
    cfg = make_config()
    eng = _build_engine(cfg, backend)

    plan = eng.pull(dry_run=True)
    assert isinstance(plan, PullPlan)
    real_result = eng.pull()
    assert real_result is None


# ─── CLI layer ─────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_setup(make_config, write_files, tmp_path, monkeypatch):
    """Build a config + on-disk YAML so the CLI can load it, then patch
    `_create_storage_set` so it returns the in-memory backend.
    Returns (cfg_path, project_dir, backend)."""
    write_files({"local-only.md": "L", "in-sync.md": "S"})
    backend = InMemoryBackend()
    fid_s = backend.seed("in-sync.md", b"S")
    backend.seed("remote-only.md", b"R")
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


def test_cli_pull_dry_run_exits_zero_and_prints_summary(cli_setup):
    cfg_path, _project_dir, _backend = cli_setup
    result = CliRunner().invoke(cli, ["pull", "--dry-run", "--config", cfg_path])
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Would download" in flat
    assert "Run without --dry-run to apply." in flat
    assert "remote-only.md" in flat


def test_cli_pull_dry_run_does_not_modify_manifest_on_disk(cli_setup):
    """Manifest bytes on disk are byte-identical before vs after the
    --dry-run invocation."""
    cfg_path, project_dir, _backend = cli_setup
    manifest_path = project_dir / MANIFEST_FILE
    before = manifest_path.read_bytes()
    before_mtime = manifest_path.stat().st_mtime_ns

    result = CliRunner().invoke(cli, ["pull", "--dry-run", "--config", cfg_path])
    assert result.exit_code == 0, result.output

    assert manifest_path.read_bytes() == before
    assert manifest_path.stat().st_mtime_ns == before_mtime


def test_cli_pull_dry_run_does_not_call_backend_writes(cli_setup):
    cfg_path, _project_dir, backend = cli_setup
    CliRunner().invoke(cli, ["pull", "--dry-run", "--config", cfg_path])
    method_names = [c[0] for c in backend.calls]
    assert "download_file" not in method_names
    assert "upload_file" not in method_names


def test_cli_pull_dry_run_does_not_write_to_local_disk(cli_setup):
    cfg_path, project_dir, _backend = cli_setup
    target = project_dir / "remote-only.md"
    assert not target.exists()

    result = CliRunner().invoke(cli, ["pull", "--dry-run", "--config", cfg_path])
    assert result.exit_code == 0, result.output

    assert not target.exists()


def test_cli_pull_dry_run_nothing_to_pull(make_config, write_files, tmp_path,
                                          monkeypatch):
    """When nothing needs pulling, dry-run prints a 'nothing to pull' line."""
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
        cli, ["pull", "--dry-run", "--config", str(cfg_path)],
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "nothing to pull" in flat.lower()


def test_cli_pull_no_dry_run_default_still_runs_actual_pull(make_config,
                                                             tmp_path,
                                                             monkeypatch):
    """Without --dry-run the existing behaviour is preserved."""
    backend = InMemoryBackend()
    backend.seed("remote-only.md", b"PAYLOAD")
    cfg = make_config()
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (backend, []),
    )
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    monkeypatch.setattr(cli_module, "_create_notifier", lambda c, s: None)

    result = CliRunner().invoke(cli, ["pull", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    method_names = [c[0] for c in backend.calls]
    assert "download_file" in method_names
    assert (Path(cfg.project_path) / "remote-only.md").read_bytes() == b"PAYLOAD"
