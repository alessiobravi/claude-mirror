"""Tests for `claude-mirror delete --dry-run`.

Covers:
  * `_plan_delete()` classifies each requested path into the right
    bucket (to_delete_remote / to_delete_local / not_found / local_only).
  * `--local` flag composes with the planning correctly: present-locally
    paths land in to_delete_local only when --local is set.
  * Dry-run does NOT call backend.delete_file, does NOT unlink the local
    copy, does NOT mutate the manifest on disk.
  * CLI: `claude-mirror delete --dry-run` exits 0 with a summary.
  * CLI: a real `claude-mirror delete` still works (regression guard for
    the wiring change).

All offline (in-memory backend), <100ms each.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror.cli import _plan_delete, cli
from claude_mirror.manifest import MANIFEST_FILE, Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import DeletePlan, SyncEngine

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


def _seed_in_sync(cfg, backend: InMemoryBackend, rel: str, content: bytes) -> str:
    """Place `rel` on both sides with a matching manifest entry — the
    common pre-condition for a 'remove this file from the mirror' flow."""
    (Path(cfg.project_path) / rel).parent.mkdir(parents=True, exist_ok=True)
    (Path(cfg.project_path) / rel).write_bytes(content)
    fid = backend.seed(rel, content)
    h = _md5(content)
    m = Manifest(cfg.project_path)
    m.update(rel, h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()
    return fid


# ─── _plan_delete — engine layer ────────────────────────────────────────────────


def test_plan_delete_remote_only_path_lands_in_to_delete_remote(make_config):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")

    plan = _plan_delete(_build_engine(cfg, backend), cfg, ["a.md"], local=False)

    assert isinstance(plan, DeletePlan)
    assert plan.to_delete_remote == ["a.md"]
    assert plan.to_delete_local == []
    assert plan.not_found == []
    assert plan.local_only == []


def test_plan_delete_local_flag_adds_path_to_to_delete_local(make_config):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")

    plan = _plan_delete(_build_engine(cfg, backend), cfg, ["a.md"], local=True)

    assert plan.to_delete_remote == ["a.md"]
    assert plan.to_delete_local == ["a.md"]


def test_plan_delete_local_flag_skips_when_local_copy_missing(make_config):
    backend = InMemoryBackend()
    fid = backend.seed("a.md", b"X")
    cfg = make_config()
    h = _md5(b"X")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()

    plan = _plan_delete(_build_engine(cfg, backend), cfg, ["a.md"], local=True)

    assert plan.to_delete_remote == ["a.md"]
    assert plan.to_delete_local == []


def test_plan_delete_unknown_path_lands_in_not_found(make_config):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")

    plan = _plan_delete(_build_engine(cfg, backend), cfg, ["ghost.md"], local=False)

    assert plan.to_delete_remote == []
    assert plan.not_found == ["ghost.md"]


def test_plan_delete_local_only_without_local_flag_lands_in_local_only_bucket(
    make_config, write_files,
):
    write_files({"local-only.md": "X"})
    backend = InMemoryBackend()
    cfg = make_config()

    plan = _plan_delete(_build_engine(cfg, backend), cfg, ["local-only.md"], local=False)

    assert plan.to_delete_remote == []
    assert plan.to_delete_local == []
    assert plan.local_only == ["local-only.md"]


def test_plan_delete_local_only_with_local_flag_lands_in_to_delete_local(
    make_config, write_files,
):
    write_files({"local-only.md": "X"})
    backend = InMemoryBackend()
    cfg = make_config()

    plan = _plan_delete(_build_engine(cfg, backend), cfg, ["local-only.md"], local=True)

    assert plan.to_delete_remote == []
    assert plan.to_delete_local == ["local-only.md"]
    assert plan.local_only == []


def test_plan_delete_mixed_paths_split_correctly(make_config, write_files):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "remote.md", b"R")
    write_files({"local.md": "L"})

    plan = _plan_delete(
        _build_engine(cfg, backend),
        cfg,
        ["remote.md", "local.md", "ghost.md"],
        local=True,
    )

    assert plan.to_delete_remote == ["remote.md"]
    assert plan.to_delete_local == ["remote.md", "local.md"]
    assert plan.not_found == ["ghost.md"]
    assert plan.local_only == []


# ─── Side-effect absence ───────────────────────────────────────────────────────


def test_plan_delete_does_not_call_backend_delete(make_config):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")

    calls_before = list(getattr(backend, "calls", []))
    _plan_delete(_build_engine(cfg, backend), cfg, ["a.md"], local=False)
    calls_after = list(getattr(backend, "calls", []))

    assert all("delete" not in c for c in calls_after) or calls_after == calls_before


def test_plan_delete_does_not_unlink_local_files(make_config, write_files):
    write_files({"a.md": "X"})
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")
    local_path = Path(cfg.project_path) / "a.md"
    before_bytes = local_path.read_bytes()

    _plan_delete(_build_engine(cfg, backend), cfg, ["a.md"], local=True)

    assert local_path.exists()
    assert local_path.read_bytes() == before_bytes


def test_plan_delete_does_not_mutate_manifest_on_disk(make_config):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")
    manifest_path = Path(cfg.project_path) / MANIFEST_FILE
    before_bytes = manifest_path.read_bytes()
    before_mtime = manifest_path.stat().st_mtime_ns

    _plan_delete(_build_engine(cfg, backend), cfg, ["a.md"], local=True)

    assert manifest_path.read_bytes() == before_bytes
    assert manifest_path.stat().st_mtime_ns == before_mtime


# ─── CLI layer ─────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_setup(make_config, tmp_path, monkeypatch):
    backend = InMemoryBackend()
    cfg = make_config()
    _seed_in_sync(cfg, backend, "a.md", b"X")
    _seed_in_sync(cfg, backend, "b.md", b"Y")

    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(cli_module, "_create_storage_set", lambda c: (backend, []))
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: backend)
    monkeypatch.setattr(cli_module, "_create_notifier", lambda c, s: None)
    return str(cfg_path), Path(cfg.project_path), backend


def _flat(s: str) -> str:
    import re
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return re.sub(r"\s+", " ", no_ansi)


def test_cli_delete_dry_run_exits_zero_and_prints_summary(cli_setup):
    cfg_path, project_dir, backend = cli_setup
    result = CliRunner().invoke(
        cli, ["delete", "a.md", "--dry-run", "--config", cfg_path]
    )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Delete plan (dry-run)" in out
    assert "a.md" in out
    assert "Would delete 1 remote" in out
    assert "Run without --dry-run to apply." in out


def test_cli_delete_dry_run_does_not_invoke_backend_delete(cli_setup):
    cfg_path, project_dir, backend = cli_setup
    result = CliRunner().invoke(
        cli, ["delete", "a.md", "--dry-run", "--config", cfg_path]
    )
    assert result.exit_code == 0, result.output
    calls = list(getattr(backend, "calls", []))
    assert all("delete" not in c for c in calls)


def test_cli_delete_dry_run_with_local_flag_reports_both(cli_setup):
    cfg_path, project_dir, backend = cli_setup
    result = CliRunner().invoke(
        cli, ["delete", "a.md", "--local", "--dry-run", "--config", cfg_path]
    )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "Would delete 1 remote + 1 local" in out
    assert (project_dir / "a.md").exists()


def test_cli_delete_dry_run_unknown_path_reports_not_found(cli_setup):
    cfg_path, _, _ = cli_setup
    result = CliRunner().invoke(
        cli, ["delete", "ghost.md", "--dry-run", "--config", cfg_path]
    )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "ghost.md" in out
    assert "not found" in out


def test_cli_delete_dry_run_local_only_warns_when_local_flag_missing(
    cli_setup, write_files,
):
    cfg_path, project_dir, _ = cli_setup
    (project_dir / "drafts.md").write_text("draft")
    result = CliRunner().invoke(
        cli, ["delete", "drafts.md", "--dry-run", "--config", cfg_path]
    )
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "drafts.md" in out
    assert "--local" in out


def test_cli_delete_real_run_still_deletes_after_dry_run_wiring(cli_setup):
    """Regression guard: adding the dry-run branch must not have broken
    the real path."""
    cfg_path, project_dir, backend = cli_setup
    result = CliRunner().invoke(
        cli, ["delete", "a.md", "--config", cfg_path]
    )
    assert result.exit_code == 0, result.output
    remaining_paths = {meta["rel_path"] for meta in backend._files.values()}
    assert "a.md" not in remaining_paths
