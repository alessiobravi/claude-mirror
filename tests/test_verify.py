"""Tests for `claude-mirror verify` — end-to-end integrity audit.

Covers both the pure phase orchestrators (`verify_manifest_vs_remote`,
`verify_snapshot_blobs`, `verify_mount_cache`, `collect_verify`) and the
CLI surface (`claude-mirror verify` / `claude-mirror verify --json`).

All tests are offline: backends are stubbed via FakeStorageBackend or a
small SnapshotManager-shaped stub; the mount-cache walker reads tmp
directories. Each test runs in well under 100 ms.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror import _verify
from claude_mirror._verify import (
    PHASE_MANIFEST,
    PHASE_MOUNT_CACHE,
    PHASE_SNAPSHOTS,
    VerifyReport,
    collect_verify,
    verify_manifest_vs_remote,
    verify_mount_cache,
    verify_snapshot_blobs,
)
from claude_mirror.cli import cli
from claude_mirror.manifest import Manifest, RemoteState


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _seed_manifest(project_dir: Path, entries: dict) -> Path:
    """Write a v3-shaped manifest to disk and return its path."""
    manifest_path = project_dir / ".claude_mirror_manifest.json"
    manifest_path.write_text(json.dumps(entries, indent=2))
    return manifest_path


def _put_file(backend, content: bytes, name: str = "doc.md", folder=None):
    """Helper: drop a file into the FakeStorageBackend, return file_id."""
    folder_id = folder or backend.root_folder_id
    return backend.upload_bytes(content, name, folder_id)


# ─── verify_manifest_vs_remote ────────────────────────────────────────


def test_manifest_all_clean_yields_empty_drift(
    project_dir, fake_backend, make_config,
):
    cfg = make_config(project_path=str(project_dir))
    content = b"hello\n"
    fid = _put_file(fake_backend, content, name="docs/notes.md")
    expected = hashlib.md5(content).hexdigest()
    _seed_manifest(project_dir, {
        "docs/notes.md": {
            "synced_hash": expected,
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": expected,
        }
    })
    report = verify_manifest_vs_remote(cfg, (fake_backend, []))
    assert report.phase.checked == 1
    assert report.phase.verified == 1
    assert report.phase.drift == 0
    assert report.phase.missing == 0
    assert report.drift == []
    assert report.missing == []


def test_manifest_drift_listed(project_dir, fake_backend, make_config):
    cfg = make_config(project_path=str(project_dir))
    fid = _put_file(fake_backend, b"current-bytes", name="notes.md")
    _seed_manifest(project_dir, {
        "notes.md": {
            "synced_hash": "abc123",
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": "abc123",
        }
    })
    report = verify_manifest_vs_remote(cfg, (fake_backend, []))
    assert report.phase.drift == 1
    assert report.phase.verified == 0
    assert len(report.drift) == 1
    drift = report.drift[0]
    assert drift.path == "notes.md"
    assert drift.expected == "abc123"
    assert drift.actual == hashlib.md5(b"current-bytes").hexdigest()
    assert drift.backend == fake_backend.backend_name


def test_manifest_missing_listed(project_dir, fake_backend, make_config):
    cfg = make_config(project_path=str(project_dir))
    _seed_manifest(project_dir, {
        "lost.md": {
            "synced_hash": "abc",
            "remote_file_id": "file-no-such-thing",
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": "abc",
        }
    })
    report = verify_manifest_vs_remote(cfg, (fake_backend, []))
    assert report.phase.missing == 1
    assert len(report.missing) == 1
    assert report.missing[0].path == "lost.md"
    assert report.missing[0].backend == fake_backend.backend_name


def test_manifest_drift_attributed_per_mirror(
    project_dir, fake_backend, make_config, monkeypatch,
):
    """Tier 2: drift on a mirror appears as a row attributed to that
    mirror, not the primary."""
    from tests.conftest import FakeStorageBackend
    mirror = FakeStorageBackend(root_folder_id="mirror-folder-id")
    mirror.backend_name = "mirror-backend"
    cfg = make_config(project_path=str(project_dir))

    primary_content = b"same-bytes\n"
    primary_fid = _put_file(fake_backend, primary_content, name="x.md")
    correct_hash = hashlib.md5(primary_content).hexdigest()
    mirror_fid = _put_file(mirror, b"different-bytes\n", name="x.md")
    drifted_hash_recorded_by_manifest = correct_hash

    _seed_manifest(project_dir, {
        "x.md": {
            "synced_hash": correct_hash,
            "remote_file_id": primary_fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": correct_hash,
            "remotes": {
                "fake": {
                    "remote_file_id": primary_fid,
                    "synced_remote_hash": correct_hash,
                    "state": "ok",
                },
                "mirror-backend": {
                    "remote_file_id": mirror_fid,
                    "synced_remote_hash": drifted_hash_recorded_by_manifest,
                    "state": "ok",
                },
            },
        }
    })
    report = verify_manifest_vs_remote(cfg, (fake_backend, [mirror]))
    assert report.phase.drift == 1
    assert len(report.drift) == 1
    assert report.drift[0].backend == "mirror-backend"


def test_manifest_skip_pending_retry_state(
    project_dir, fake_backend, make_config,
):
    """Entries quarantined to pending_retry / failed_perm aren't 'drift'
    in the integrity sense — they're tracked by `status --pending`."""
    cfg = make_config(project_path=str(project_dir))
    _seed_manifest(project_dir, {
        "queued.md": {
            "synced_hash": "abc",
            "remote_file_id": "primary-fid",
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": "abc",
            "remotes": {
                "fake": {
                    "remote_file_id": "primary-fid",
                    "synced_remote_hash": "abc",
                    "state": "pending_retry",
                    "intended_hash": "def",
                    "attempts": 1,
                },
            },
        }
    })
    report = verify_manifest_vs_remote(cfg, (fake_backend, []))
    assert report.phase.drift == 0
    assert report.phase.missing == 0
    assert report.phase.verified == 1


def test_manifest_empty_returns_empty_report(
    project_dir, fake_backend, make_config,
):
    cfg = make_config(project_path=str(project_dir))
    report = verify_manifest_vs_remote(cfg, (fake_backend, []))
    assert report.phase.checked == 0
    assert report.drift == []
    assert report.missing == []


def test_manifest_backend_filter_scopes_to_one(
    project_dir, fake_backend, make_config,
):
    from tests.conftest import FakeStorageBackend
    mirror = FakeStorageBackend(root_folder_id="mirror-folder-id")
    mirror.backend_name = "scope-target"
    cfg = make_config(project_path=str(project_dir))
    primary_fid = _put_file(fake_backend, b"a", name="f.md")
    mirror_fid = _put_file(mirror, b"a", name="f.md")
    correct = hashlib.md5(b"a").hexdigest()
    _seed_manifest(project_dir, {
        "f.md": {
            "synced_hash": correct,
            "remote_file_id": primary_fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": correct,
            "remotes": {
                "fake": {
                    "remote_file_id": primary_fid,
                    "synced_remote_hash": correct,
                    "state": "ok",
                },
                "scope-target": {
                    "remote_file_id": mirror_fid,
                    "synced_remote_hash": correct,
                    "state": "ok",
                },
            },
        }
    })
    report = verify_manifest_vs_remote(
        cfg, (fake_backend, [mirror]), backend_filter="scope-target",
    )
    assert report.phase.checked == 1
    assert report.phase.verified == 1


# ─── verify_snapshot_blobs ────────────────────────────────────────────


class _SnapshotManagerStub:
    """Minimal SnapshotManager surface used by `verify_snapshot_blobs`."""

    def __init__(self, storage, mirrors=None, blobs_folders=None):
        self.storage = storage
        self._mirrors = list(mirrors or [])
        self._folders = blobs_folders or {}

    def _get_blobs_folder_for(self, backend) -> str:
        # Resolve via dict, falling back to the backend's default.
        return self._folders.get(id(backend), "blobs-folder")


def _seed_blob(backend, blobs_folder_id: str, content: bytes) -> str:
    """Upload `content` under its sha256 filename — content-addressed."""
    sha = hashlib.sha256(content).hexdigest()
    backend.upload_bytes(content, sha, blobs_folder_id)
    return sha


def test_snapshot_blobs_all_clean(fake_backend):
    blobs_folder = fake_backend.get_or_create_folder(
        "_claude_mirror_blobs", fake_backend.root_folder_id,
    )
    _seed_blob(fake_backend, blobs_folder, b"alpha")
    _seed_blob(fake_backend, blobs_folder, b"beta")
    sm = _SnapshotManagerStub(
        fake_backend, blobs_folders={id(fake_backend): blobs_folder},
    )
    report = verify_snapshot_blobs(sm)
    assert report.phase.checked == 2
    assert report.phase.verified == 2
    assert report.phase.corrupted == 0
    assert report.corrupted == []


def test_snapshot_blobs_corrupted_listed(fake_backend):
    blobs_folder = fake_backend.get_or_create_folder(
        "_claude_mirror_blobs", fake_backend.root_folder_id,
    )
    # Honest blob: filename matches sha.
    _seed_blob(fake_backend, blobs_folder, b"truth")
    # Corrupted blob: filename is sha("expected") but bytes are "wrong".
    expected_sha = hashlib.sha256(b"expected").hexdigest()
    fake_backend.upload_bytes(b"wrong content", expected_sha, blobs_folder)

    sm = _SnapshotManagerStub(
        fake_backend, blobs_folders={id(fake_backend): blobs_folder},
    )
    report = verify_snapshot_blobs(sm)
    assert report.phase.checked == 2
    assert report.phase.verified == 1
    assert report.phase.corrupted == 1
    assert len(report.corrupted) == 1
    assert expected_sha in report.corrupted[0].key


def test_snapshot_blobs_missing_listed(fake_backend, monkeypatch):
    """A blob entry whose download_file raises lands in missing[]."""
    blobs_folder = fake_backend.get_or_create_folder(
        "_claude_mirror_blobs", fake_backend.root_folder_id,
    )
    sha = _seed_blob(fake_backend, blobs_folder, b"vanished")

    def boom(*_a, **_kw):
        raise FileNotFoundError("blob is gone")

    monkeypatch.setattr(fake_backend, "download_file", boom)

    sm = _SnapshotManagerStub(
        fake_backend, blobs_folders={id(fake_backend): blobs_folder},
    )
    report = verify_snapshot_blobs(sm)
    assert report.phase.checked == 1
    assert report.phase.missing == 1
    assert report.missing[0].path == sha


def test_snapshot_blobs_filename_not_sha_is_corrupted(fake_backend):
    blobs_folder = fake_backend.get_or_create_folder(
        "_claude_mirror_blobs", fake_backend.root_folder_id,
    )
    fake_backend.upload_bytes(b"data", "not-a-hash", blobs_folder)
    sm = _SnapshotManagerStub(
        fake_backend, blobs_folders={id(fake_backend): blobs_folder},
    )
    report = verify_snapshot_blobs(sm)
    assert report.phase.corrupted == 1
    assert report.corrupted[0].detail == "filename is not a sha256 digest"


# ─── verify_mount_cache ───────────────────────────────────────────────


def test_mount_cache_empty_dir_is_clean(tmp_path):
    cache = tmp_path / "blobs"
    cache.mkdir()
    report = verify_mount_cache(cache)
    assert report.phase.checked == 0
    assert report.phase.verified == 0
    assert report.corrupted == []


def test_mount_cache_missing_dir_is_clean(tmp_path):
    cache = tmp_path / "missing"
    report = verify_mount_cache(cache)
    assert report.phase.checked == 0
    assert report.corrupted == []


def test_mount_cache_clean_blob_verified(tmp_path):
    cache = tmp_path / "blobs"
    content = b"alpha"
    sha = hashlib.sha256(content).hexdigest()
    shard = cache / sha[:2]
    shard.mkdir(parents=True)
    (shard / sha).write_bytes(content)
    report = verify_mount_cache(cache)
    assert report.phase.checked == 1
    assert report.phase.verified == 1
    assert report.corrupted == []


def test_mount_cache_corrupted_blob_listed(tmp_path):
    cache = tmp_path / "blobs"
    expected = hashlib.sha256(b"expected").hexdigest()
    shard = cache / expected[:2]
    shard.mkdir(parents=True)
    (shard / expected).write_bytes(b"corrupted bytes here")
    report = verify_mount_cache(cache)
    assert report.phase.checked == 1
    assert report.phase.corrupted == 1
    assert report.phase.verified == 0
    assert expected in report.corrupted[0].key
    assert "hash" in report.corrupted[0].detail


def test_mount_cache_filename_not_sha_listed(tmp_path):
    cache = tmp_path / "blobs"
    shard = cache / "ab"
    shard.mkdir(parents=True)
    (shard / "not-a-sha").write_bytes(b"xx")
    report = verify_mount_cache(cache)
    assert report.phase.corrupted == 1
    assert report.corrupted[0].detail == "filename is not a sha256 digest"


# ─── collect_verify aggregator ────────────────────────────────────────


def test_collect_verify_runs_only_requested_phases(
    project_dir, fake_backend, make_config, tmp_path,
):
    cfg = make_config(project_path=str(project_dir))
    report = collect_verify(
        cfg, (fake_backend, []),
        snapshot_manager=None,
        blob_cache_dir=None,
        include_files=False,
        include_snapshots=False,
        include_mount_cache=False,
    )
    assert report.phases == []
    assert not report.has_findings()


def test_collect_verify_aggregates_findings(
    project_dir, fake_backend, make_config, tmp_path,
):
    cfg = make_config(project_path=str(project_dir))
    fid = _put_file(fake_backend, b"current", name="x.md")
    _seed_manifest(project_dir, {
        "x.md": {
            "synced_hash": "stale",
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": "stale",
        }
    })
    blobs_folder = fake_backend.get_or_create_folder(
        "_claude_mirror_blobs", fake_backend.root_folder_id,
    )
    expected_sha = hashlib.sha256(b"expected").hexdigest()
    fake_backend.upload_bytes(b"wrong content", expected_sha, blobs_folder)
    sm = _SnapshotManagerStub(
        fake_backend, blobs_folders={id(fake_backend): blobs_folder},
    )

    cache = tmp_path / "cache"
    bad_sha = hashlib.sha256(b"clean").hexdigest()
    shard = cache / bad_sha[:2]
    shard.mkdir(parents=True)
    (shard / bad_sha).write_bytes(b"corrupted")

    report = collect_verify(
        cfg, (fake_backend, []),
        snapshot_manager=sm,
        blob_cache_dir=cache,
    )
    assert {p.name for p in report.phases} == {
        PHASE_MANIFEST, PHASE_SNAPSHOTS, PHASE_MOUNT_CACHE,
    }
    assert report.drift  # one stale entry
    assert report.corrupted  # snapshot blob + mount cache
    assert report.has_findings()


# ─── CLI: argument handling + exit codes ──────────────────────────────


def _write_yaml_config(
    path: Path, *, project_path: Path, token_file: Path, credentials_file: Path,
):
    data = {
        "project_path": str(project_path),
        "backend": "googledrive",
        "drive_folder_id": "test-folder-id",
        "credentials_file": str(credentials_file),
        "token_file": str(token_file),
        "machine_name": "test-machine",
        "user": "test-user",
    }
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def verify_setup(tmp_path, project_dir, config_dir, fake_backend, monkeypatch):
    """A minimal CLI-ready config: token file present, fake backend wired
    in via _create_storage and _create_storage_set."""
    token = config_dir / "token.json"
    token.write_text(json.dumps({"token": "x", "refresh_token": "y"}))
    creds = config_dir / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "primary.yaml",
        project_path=project_dir,
        token_file=token,
        credentials_file=creds,
    )
    monkeypatch.setattr(cli_mod, "_create_storage", lambda c: fake_backend)
    monkeypatch.setattr(
        cli_mod, "_create_storage_set",
        lambda c: (fake_backend, []),
    )
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: p)
    return SimpleNamespace(
        cfg_path=str(cfg_path),
        project_dir=project_dir,
        backend=fake_backend,
    )


def test_cli_verify_default_exit_zero_with_drift(verify_setup, monkeypatch):
    """`verify` exits 0 by default even when drift is present."""
    fid = _put_file(verify_setup.backend, b"current", name="d.md")
    _seed_manifest(verify_setup.project_dir, {
        "d.md": {
            "synced_hash": "stale",
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": "stale",
        }
    })
    # Avoid touching the real ~/.cache/claude-mirror/blobs.
    monkeypatch.setattr(
        cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path,
    )
    result = CliRunner().invoke(
        cli, ["verify", "--no-mount-cache", "--no-snapshots",
              "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Drift detected" in out
    assert "d.md" in out


def test_cli_verify_strict_exits_one_on_drift(verify_setup, monkeypatch):
    fid = _put_file(verify_setup.backend, b"current", name="d.md")
    _seed_manifest(verify_setup.project_dir, {
        "d.md": {
            "synced_hash": "stale",
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": "stale",
        }
    })
    monkeypatch.setattr(
        cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path,
    )
    result = CliRunner().invoke(
        cli, ["verify", "--strict", "--no-mount-cache", "--no-snapshots",
              "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 1, result.output


def test_cli_verify_strict_exits_zero_when_clean(verify_setup, monkeypatch):
    """Clean run + --strict still exits 0 when there are zero findings."""
    monkeypatch.setattr(
        cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path,
    )
    result = CliRunner().invoke(
        cli, ["verify", "--strict", "--no-mount-cache", "--no-snapshots",
              "--no-files", "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 0, result.output


def test_cli_verify_json_envelope_v1(verify_setup, monkeypatch):
    monkeypatch.setattr(
        cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path,
    )
    result = CliRunner().invoke(
        cli, ["verify", "--json", "--no-mount-cache", "--no-snapshots",
              "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "verify"
    assert "result" in doc
    assert "phases" in doc["result"]
    assert "checked_at" in doc["result"]
    # No human-readable banner snuck onto stdout
    assert "claude-mirror verify" not in result.stdout


def test_cli_verify_no_phases_friendly_message(verify_setup, monkeypatch):
    monkeypatch.setattr(
        cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path,
    )
    result = CliRunner().invoke(
        cli, ["verify", "--no-files", "--no-snapshots", "--no-mount-cache",
              "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "No phases enabled" in out


def test_cli_verify_empty_manifest_friendly_message(verify_setup, monkeypatch):
    """Empty manifest → all phase counts are zero → 'No files to verify.'"""
    monkeypatch.setattr(
        cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path,
    )
    result = CliRunner().invoke(
        cli, ["verify", "--no-mount-cache", "--no-snapshots",
              "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "No files to verify" in out


def test_cli_verify_backend_filter_scopes(verify_setup, monkeypatch):
    """`verify --backend NAME` runs with backend_filter passed through.
    Patch collect_verify and assert it received the filter."""
    captured: dict = {}

    def fake_collect(*args, **kwargs):
        captured.update(kwargs)
        return VerifyReport(checked_at=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc,
        ))
    monkeypatch.setattr(_verify, "collect_verify", fake_collect)
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: verify_setup.cfg_path)
    result = CliRunner().invoke(
        cli, ["verify", "--backend", "fake", "--no-mount-cache",
              "--no-snapshots", "--config", verify_setup.cfg_path],
    )
    assert result.exit_code == 0, result.output
    assert captured.get("backend_filter") == "fake"


# ─── Report.to_dict() shape ───────────────────────────────────────────


def test_verify_report_to_dict_shape():
    from datetime import datetime, timezone
    from claude_mirror._verify import (
        DriftEntry, MissingEntry, CorruptedEntry, PhaseReport,
    )
    rep = VerifyReport(checked_at=datetime(2026, 5, 9, tzinfo=timezone.utc))
    rep.phases.append(PhaseReport(name=PHASE_MANIFEST, checked=2, verified=1, drift=1))
    rep.drift.append(DriftEntry(path="a.md", backend="x", expected="aa", actual="bb"))
    rep.corrupted.append(CorruptedEntry(layer="mount_cache", key="ab/abcd"))
    rep.missing.append(MissingEntry(path="m.md", backend="x", expected="cc"))
    doc = rep.to_dict()
    assert "checked_at" in doc
    assert isinstance(doc["phases"], list)
    assert doc["phases"][0]["name"] == PHASE_MANIFEST
    assert set(doc["phases"][0].keys()) == {
        "name", "checked", "verified", "drift", "missing", "corrupted",
    }
    assert doc["drift"][0]["path"] == "a.md"
    assert doc["corrupted"][0]["layer"] == "mount_cache"
    assert doc["missing"][0]["path"] == "m.md"


# ─── Progress callback ────────────────────────────────────────────────


def test_progress_callback_invoked(project_dir, fake_backend, make_config):
    cfg = make_config(project_path=str(project_dir))
    content = b"hello\n"
    fid = _put_file(fake_backend, content, name="p.md")
    expected = hashlib.md5(content).hexdigest()
    _seed_manifest(project_dir, {
        "p.md": {
            "synced_hash": expected,
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": expected,
        }
    })
    calls: list = []

    def cb(phase, checked, total):
        calls.append((phase, checked, total))

    verify_manifest_vs_remote(cfg, (fake_backend, []), on_progress=cb)
    assert calls
    assert calls[0][0] == PHASE_MANIFEST


def test_progress_callback_swallows_exceptions(
    project_dir, fake_backend, make_config,
):
    cfg = make_config(project_path=str(project_dir))
    content = b"hi"
    fid = _put_file(fake_backend, content, name="p.md")
    expected = hashlib.md5(content).hexdigest()
    _seed_manifest(project_dir, {
        "p.md": {
            "synced_hash": expected,
            "remote_file_id": fid,
            "synced_at": "2026-05-09T00:00:00Z",
            "synced_remote_hash": expected,
        }
    })

    def bad_cb(*_a, **_kw):
        raise RuntimeError("progress callback should not crash verify")

    report = verify_manifest_vs_remote(
        cfg, (fake_backend, []), on_progress=bad_cb,
    )
    assert report.phase.verified == 1
