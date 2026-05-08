"""Tests for `claude-mirror seed-mirror` auto-detect behaviour.

When --backend is omitted, the CLI inspects every configured mirror via
`manifest.unseeded_for_backend(NAME)`. Behaviour:

    * Zero candidates → green check, exit 0, "Nothing to seed".
    * Exactly one candidate → dim "Auto-detected" line, then run as if
      the user had passed --backend NAME explicitly.
    * Multiple candidates → red error listing candidate names
      alphabetically, exit 1.

When --backend is supplied explicitly the auto-detect logic is bypassed
entirely — explicit value wins. The pre-existing error paths (unknown
backend, no mirrors configured at all) are preserved.

All tests run offline against an in-memory FakeStorageBackend.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine
from tests.conftest import FakeStorageBackend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_named_mirror(make_config, backend_name: str, root_id: str | None = None) -> FakeStorageBackend:
    """Build a FakeStorageBackend masquerading as a named mirror.

    Mirrors are looked up via `getattr(mirror, "backend_name", "")` and
    they need a `.config` whose `root_folder` matches; the engine reads
    `target.config.root_folder` during the upload path.
    """
    root_id = root_id or f"{backend_name}-root"
    cfg = make_config(
        backend="sftp" if backend_name == "sftp" else "googledrive",
        drive_folder_id=root_id,
        sftp_folder=root_id,
    )
    m = FakeStorageBackend(root_folder_id=root_id)
    m.backend_name = backend_name
    m.config = cfg
    return m


def _build_engine(make_config, fake_backend, mirrors):
    cfg = make_config()
    return SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=Manifest(cfg.project_path),
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=list(mirrors),
    )


def _patch_load_engine(monkeypatch, engine, cfg, fake_backend):
    monkeypatch.setattr(
        cli_module, "_load_engine",
        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend),
    )
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")


def _seed_primary(engine, project_dir, rel_paths):
    """Mark each rel_path as state=ok on the primary (googledrive) and
    pin synced_hash to the actual local-file hash so the engine's drift
    check passes during the upload path."""
    for rel in rel_paths:
        h = Manifest.hash_file(str(project_dir / rel))
        engine.manifest.update_remote(
            rel, "googledrive",
            remote_file_id=f"g-{rel}", synced_remote_hash=h, state="ok",
        )
        existing = engine.manifest.get(rel)
        existing.synced_hash = h
        engine.manifest._data[rel] = existing


# ─── 1. Zero unseeded mirrors ──────────────────────────────────────────────────

def test_auto_detect_zero_unseeded_mirrors_exits_zero(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """One configured mirror, fully seeded → 'Nothing to seed', exit 0."""
    write_files({"a.md": "alpha\n"})
    mirror = _make_named_mirror(make_config, "sftp")
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [mirror])
    _seed_primary(engine, project_dir, ["a.md"])
    h = Manifest.hash_file(str(project_dir / "a.md"))
    # Mirror is fully seeded already.
    engine.manifest.update_remote(
        "a.md", "sftp",
        remote_file_id="/srv/a.md", synced_remote_hash=h, state="ok",
    )
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(cli, ["seed-mirror"])

    assert result.exit_code == 0, result.output
    assert "No mirrors have unseeded files" in result.output
    # No upload happened — the mirror's storage stays empty.
    assert mirror.files == {}


# ─── 2. Exactly one unseeded mirror ───────────────────────────────────────────

def test_auto_detect_single_unseeded_mirror_proceeds(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """Two configured mirrors, only one has unseeded files → auto-pick
    that one. Output includes the dim 'Auto-detected' line and the
    upload path runs to completion against the inferred backend."""
    write_files({"a.md": "alpha\n"})
    mirror_sftp = _make_named_mirror(make_config, "sftp")
    mirror_dropbox = _make_named_mirror(make_config, "dropbox")
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [mirror_sftp, mirror_dropbox])
    _seed_primary(engine, project_dir, ["a.md"])
    h = Manifest.hash_file(str(project_dir / "a.md"))
    # dropbox is fully seeded; sftp is NOT.
    engine.manifest.update_remote(
        "a.md", "dropbox",
        remote_file_id="db-a", synced_remote_hash=h, state="ok",
    )
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(cli, ["seed-mirror"])

    assert result.exit_code == 0, result.output
    assert "Auto-detected unseeded mirror: `sftp`" in result.output
    # Upload actually happened on sftp, not dropbox.
    assert len(mirror_sftp.files) == 1
    assert mirror_dropbox.files == {}
    # Manifest now records state=ok for sftp.
    rs = engine.manifest.get("a.md").get_remote("sftp")
    assert rs is not None and rs.state == "ok"


# ─── 3. Two unseeded mirrors → ambiguous ───────────────────────────────────────

def test_auto_detect_multiple_unseeded_mirrors_errors(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """Two configured mirrors, both unseeded → error listing candidates
    alphabetically, exit 1, no uploads."""
    write_files({"a.md": "alpha\n"})
    mirror_sftp = _make_named_mirror(make_config, "sftp")
    mirror_dropbox = _make_named_mirror(make_config, "dropbox")
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [mirror_sftp, mirror_dropbox])
    _seed_primary(engine, project_dir, ["a.md"])
    # Neither mirror has any state recorded → both are unseeded.
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(cli, ["seed-mirror"])

    # Rich may inject ANSI color escapes and soft-wrap lines mid-message;
    # strip both before asserting on message contents so the test
    # doesn't depend on terminal width.
    import re
    no_ansi = re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    flat = re.sub(r"\s+", " ", no_ansi)

    assert result.exit_code == 1, result.output
    assert "Multiple mirrors have unseeded files" in flat
    # Alphabetical listing → dropbox before sftp.
    dropbox_pos = flat.find("`dropbox`")
    sftp_pos = flat.find("`sftp`")
    assert dropbox_pos != -1 and sftp_pos != -1
    assert dropbox_pos < sftp_pos
    assert "--backend NAME" in flat
    # No uploads happened.
    assert mirror_sftp.files == {}
    assert mirror_dropbox.files == {}


# ─── 4. Explicit --backend bypasses auto-detect ────────────────────────────────

def test_explicit_backend_bypasses_auto_detect(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """When the user passes --backend sftp, the auto-detect branch is
    skipped entirely even if more than one mirror would be a candidate.
    The dim 'Auto-detected' line MUST NOT appear."""
    write_files({"a.md": "alpha\n"})
    mirror_sftp = _make_named_mirror(make_config, "sftp")
    mirror_dropbox = _make_named_mirror(make_config, "dropbox")
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [mirror_sftp, mirror_dropbox])
    _seed_primary(engine, project_dir, ["a.md"])
    # Both mirrors unseeded — would be ambiguous if auto-detect ran.
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(cli, ["seed-mirror", "--backend", "sftp"])

    assert result.exit_code == 0, result.output
    assert "Auto-detected" not in result.output
    assert "Multiple mirrors have unseeded files" not in result.output
    # Explicit --backend sftp uploaded to sftp only.
    assert len(mirror_sftp.files) == 1
    assert mirror_dropbox.files == {}


# ─── 5. Explicit --backend with unknown name ───────────────────────────────────

def test_explicit_backend_unknown_name_errors(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """Explicit --backend pointing at a mirror that isn't configured
    keeps the existing ValueError-→-red-message-→-exit-1 path."""
    write_files({"a.md": "alpha\n"})
    mirror_sftp = _make_named_mirror(make_config, "sftp")
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [mirror_sftp])
    _seed_primary(engine, project_dir, ["a.md"])
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(
        cli, ["seed-mirror", "--backend", "not-a-real-backend"],
    )

    assert result.exit_code == 1, result.output
    assert "not-a-real-backend" in result.output


# ─── 6. Explicit --backend on already-seeded mirror ────────────────────────────

def test_explicit_backend_on_seeded_mirror_is_noop(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """Explicit --backend pointing at a configured mirror that's already
    fully seeded → exits 0, no upload, prints the engine's
    'already seeded' message. Pre-feature behaviour preserved."""
    write_files({"a.md": "alpha\n"})
    mirror_sftp = _make_named_mirror(make_config, "sftp")
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [mirror_sftp])
    _seed_primary(engine, project_dir, ["a.md"])
    h = Manifest.hash_file(str(project_dir / "a.md"))
    engine.manifest.update_remote(
        "a.md", "sftp",
        remote_file_id="/srv/a.md", synced_remote_hash=h, state="ok",
    )
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(cli, ["seed-mirror", "--backend", "sftp"])

    assert result.exit_code == 0, result.output
    assert "already seeded" in result.output
    assert mirror_sftp.files == {}


# ─── 7. No mirrors configured at all ───────────────────────────────────────────

def test_no_mirrors_configured_exits_with_existing_message(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """Project with zero mirrors keeps the pre-feature behaviour: yellow
    warning + exit 1, regardless of whether --backend was supplied."""
    write_files({"a.md": "alpha\n"})
    cfg = make_config()
    engine = _build_engine(make_config, fake_backend, [])
    _patch_load_engine(monkeypatch, engine, cfg, fake_backend)

    result = CliRunner().invoke(cli, ["seed-mirror"])

    assert result.exit_code == 1, result.output
    assert "No mirrors configured" in result.output
