"""Tests for `claude-mirror diff <path>` and the underlying _diff module.

Coverage:
    * is_binary heuristic on text vs binary content
    * render_diff for every state combo:
        - both sides differ
        - only-on-local
        - only-on-remote
        - identical (in-sync)
        - one side binary
        - neither side has the file
    * The CLI command: path resolution (relative + absolute),
      out-of-project rejection, missing-file exit code, happy-path
      identical / differing / new-local / new-remote scenarios.

All tests run offline against FakeStorageBackend; no real cloud I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror._diff import is_binary, render_diff
from claude_mirror.cli import cli
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine

# Click 8.3's CliRunner triggers a Context.protected_args DeprecationWarning
# that pyproject's filterwarnings = "error" turns into a test failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── is_binary heuristic ───────────────────────────────────────────────────────

def test_is_binary_text_returns_false():
    assert is_binary(b"hello world\n") is False
    assert is_binary("# heading\n\nsome utf-8 ☕\n".encode("utf-8")) is False


def test_is_binary_empty_returns_false():
    assert is_binary(b"") is False


def test_is_binary_nul_byte_returns_true():
    assert is_binary(b"hello\x00world") is True


def test_is_binary_non_utf8_returns_true():
    # latin-1 byte sequence that is not valid utf-8
    assert is_binary(b"\xff\xfe\xfd\xfc") is True


# ─── render_diff scenarios ─────────────────────────────────────────────────────

def test_render_diff_identical_content_says_in_sync():
    out = render_diff(b"a\nb\nc\n", b"a\nb\nc\n", "x.md")
    assert "in sync" in out.plain.lower()


def test_render_diff_only_local_shows_pushed_hint():
    out = render_diff(b"new\n", None, "new.md")
    assert "only on local" in out.plain.lower()
    # difflib emits +new under the local file header
    assert "+new" in out.plain


def test_render_diff_only_remote_shows_pulled_hint():
    out = render_diff(None, b"old\n", "old.md")
    assert "only on remote" in out.plain.lower()
    assert "-old" in out.plain


def test_render_diff_both_sides_differ_emits_unified_diff():
    # Diff direction is remote -> local: lines unique to remote get "-",
    # lines unique to local get "+".
    out = render_diff(b"a\nb\nc\n", b"a\nB\nc\n", "f.md")
    plain = out.plain
    assert "both sides differ" in plain.lower()
    assert "@@" in plain  # hunk header
    assert "-B" in plain  # remote had uppercase B
    assert "+b" in plain  # local has lowercase b


def test_render_diff_local_binary_refused():
    out = render_diff(b"\x00\x01\x02binary", b"text\n", "blob.bin")
    assert "binary" in out.plain.lower()
    assert "refusing" in out.plain.lower()


def test_render_diff_remote_binary_refused():
    out = render_diff(b"text\n", b"\x00\x01\x02binary", "blob.bin")
    assert "binary" in out.plain.lower()
    assert "refusing" in out.plain.lower()


def test_render_diff_neither_side_present_says_so():
    out = render_diff(None, None, "ghost.md")
    assert "not present" in out.plain.lower()


def test_render_diff_context_lines_param_respected():
    # With 0 context lines, only the changed line surrounds the hunk header.
    local = b"a\nb\nc\nd\nE\nf\ng\nh\n"
    remote = b"a\nb\nc\nd\ne\nf\ng\nh\n"
    out0 = render_diff(local, remote, "x.md", context_lines=0)
    out3 = render_diff(local, remote, "x.md", context_lines=3)
    assert len(out3.plain) > len(out0.plain)


# ─── CLI command tests ─────────────────────────────────────────────────────────

def _build_engine(make_config, fake_backend, project_dir: Path) -> SyncEngine:
    """SyncEngine wired to FakeStorageBackend so the diff command can
    list_files_recursive + download_file without auth."""
    cfg = make_config()
    return SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=Manifest(cfg.project_path),
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=[],
    )


@pytest.fixture
def patch_load_engine(monkeypatch, make_config, fake_backend, project_dir):
    """Replace cli._load_engine with one returning a pre-built engine
    backed by FakeStorageBackend, bypassing _resolve_config + auth."""
    cfg = make_config()
    engine = SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=Manifest(cfg.project_path),
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=[],
    )
    monkeypatch.setattr(
        cli_module,
        "_load_engine",
        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend),
    )
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")
    return engine


def _add_remote_file(fake_backend, root_folder_id: str, rel_path: str, content: bytes) -> str:
    """Helper: place a file at rel_path in the fake backend's tree.
    Uses upload_bytes (in-memory) rather than upload_file (which expects
    a real path on disk)."""
    parent_id, basename = fake_backend.resolve_path(rel_path, root_folder_id)
    return fake_backend.upload_bytes(content, basename, parent_id)


def test_diff_in_sync_prints_identical_message(patch_load_engine, write_files, fake_backend, project_dir):
    write_files({"a.md": "hello\n"})
    _add_remote_file(fake_backend, "test-folder-id", "a.md", b"hello\n")
    result = CliRunner().invoke(cli, ["diff", "a.md"])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.output.lower()


def test_diff_local_only_shows_pushed_hint(patch_load_engine, write_files):
    write_files({"new.md": "freshly written\n"})
    result = CliRunner().invoke(cli, ["diff", "new.md"])
    assert result.exit_code == 0, result.output
    assert "only on local" in result.output.lower()


def test_diff_remote_only_shows_pulled_hint(patch_load_engine, fake_backend):
    _add_remote_file(fake_backend, "test-folder-id", "remote.md", b"from cloud\n")
    result = CliRunner().invoke(cli, ["diff", "remote.md"])
    assert result.exit_code == 0, result.output
    assert "only on remote" in result.output.lower()


def test_diff_both_differ_emits_unified_diff(patch_load_engine, write_files, fake_backend):
    write_files({"f.md": "alpha\nbeta-LOCAL\ngamma\n"})
    _add_remote_file(fake_backend, "test-folder-id", "f.md", b"alpha\nbeta-REMOTE\ngamma\n")
    result = CliRunner().invoke(cli, ["diff", "f.md"])
    assert result.exit_code == 0, result.output
    assert "@@" in result.output  # hunk header
    assert "-beta-REMOTE" in result.output
    assert "+beta-LOCAL" in result.output


def test_diff_missing_everywhere_exits_one(patch_load_engine):
    result = CliRunner().invoke(cli, ["diff", "ghost.md"])
    assert result.exit_code == 1
    assert "no such file" in result.output.lower()


def test_diff_absolute_path_inside_project_resolved_to_relative(
    patch_load_engine, write_files, fake_backend, project_dir
):
    write_files({"docs/x.md": "abs\n"})
    _add_remote_file(fake_backend, "test-folder-id", "docs/x.md", b"abs\n")
    abs_path = str(project_dir / "docs" / "x.md")
    result = CliRunner().invoke(cli, ["diff", abs_path])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.output.lower()


def test_diff_absolute_path_outside_project_rejected(patch_load_engine, tmp_path):
    outside = tmp_path / "elsewhere" / "x.md"
    outside.parent.mkdir()
    outside.write_text("doesn't matter\n")
    result = CliRunner().invoke(cli, ["diff", str(outside)])
    assert result.exit_code == 1
    assert "outside" in result.output.lower()


def test_diff_context_flag_respected_at_cli(patch_load_engine, write_files, fake_backend):
    write_files({"f.md": "a\nb\nc\nd\nE\nf\ng\nh\n"})
    _add_remote_file(fake_backend, "test-folder-id", "f.md", b"a\nb\nc\nd\ne\nf\ng\nh\n")
    r0 = CliRunner().invoke(cli, ["diff", "f.md", "--context", "0"])
    r5 = CliRunner().invoke(cli, ["diff", "f.md", "--context", "5"])
    assert r0.exit_code == 0
    assert r5.exit_code == 0
    assert len(r5.output) > len(r0.output)
