"""Tests for `claude-mirror ncdu` — the pure-data layer only.

The curses TUI (`run_curses_ui`) is NOT covered here: it requires a
real terminal and is validated by manual smoke-test (running
`claude-mirror ncdu` in a real shell).

Coverage:
    * `build_size_tree` — empty / single-file / nested / siblings /
      duplicate-rel-path / NUL-byte rejection
    * `SizeNode` aggregation — size + file_count bubble up correctly
    * `top_n_paths` — desc order, n bounds, ties broken by path
    * `format_non_interactive` — header / row / total / empty-remote
    * `entries_from_backend_listing` adapter — happy path + defensive
      skips
    * CLI dispatch (`--remote NAME`, `--non-interactive`,
      unknown-backend error, Windows-platform gating)
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

import claude_mirror.cli as cli_module
from claude_mirror import _ncdu
from claude_mirror._ncdu import (
    SizeNode,
    build_size_tree,
    entries_from_backend_listing,
    format_non_interactive,
    human_size,
    top_n_paths,
)
from claude_mirror.cli import cli

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── build_size_tree ──────────────────────────────────────────────────────


def test_build_size_tree_empty_listing_returns_empty_root() -> None:
    root = build_size_tree([])
    assert root.path == ""
    assert root.is_file is False
    assert root.size == 0
    assert root.file_count == 0
    assert root.children == {}


def test_build_size_tree_single_file_at_root() -> None:
    root = build_size_tree([("README.md", 100)])
    assert root.size == 100
    assert root.file_count == 1
    assert list(root.children.keys()) == ["README.md"]
    leaf = root.children["README.md"]
    assert leaf.is_file is True
    assert leaf.size == 100
    assert leaf.file_count == 1
    assert leaf.path == "README.md"


def test_build_size_tree_synthesises_intermediate_directories() -> None:
    root = build_size_tree([("docs/admin/setup.md", 500)])
    assert "docs" in root.children
    docs = root.children["docs"]
    assert docs.is_file is False
    assert docs.path == "docs"
    assert "admin" in docs.children
    admin = docs.children["admin"]
    assert admin.is_file is False
    assert admin.path == "docs/admin"
    assert "setup.md" in admin.children
    leaf = admin.children["setup.md"]
    assert leaf.is_file is True
    assert leaf.path == "docs/admin/setup.md"
    assert leaf.size == 500


def test_build_size_tree_siblings_at_same_depth() -> None:
    root = build_size_tree([
        ("docs/a.md", 10),
        ("docs/b.md", 20),
        ("docs/c.md", 30),
    ])
    docs = root.children["docs"]
    assert set(docs.children.keys()) == {"a.md", "b.md", "c.md"}
    assert docs.size == 60
    assert docs.file_count == 3


def test_build_size_tree_aggregates_size_up_the_tree() -> None:
    root = build_size_tree([
        ("docs/a.md", 100),
        ("docs/sub/b.md", 200),
        ("README.md", 50),
    ])
    assert root.size == 350
    assert root.file_count == 3
    assert root.children["docs"].size == 300
    assert root.children["docs"].file_count == 2
    assert root.children["docs"].children["sub"].size == 200
    assert root.children["docs"].children["sub"].file_count == 1
    assert root.children["README.md"].size == 50


def test_build_size_tree_skips_empty_rel_path() -> None:
    root = build_size_tree([("", 100), ("a.md", 50)])
    assert root.size == 50
    assert root.file_count == 1
    assert "a.md" in root.children


def test_build_size_tree_handles_doubled_slashes() -> None:
    root = build_size_tree([("docs//a.md", 100)])
    assert root.children["docs"].children["a.md"].size == 100


def test_build_size_tree_rejects_nul_byte() -> None:
    with pytest.raises(ValueError, match="NUL byte"):
        build_size_tree([("docs\x00bad.md", 100)])


def test_build_size_tree_duplicate_rel_path_last_wins() -> None:
    root = build_size_tree([
        ("a.md", 100),
        ("a.md", 50),
    ])
    assert root.children["a.md"].size == 50
    assert root.children["a.md"].file_count == 1
    assert root.size == 150
    assert root.file_count == 2


def test_build_size_tree_max_listing_entries_guard() -> None:
    original = _ncdu.MAX_LISTING_ENTRIES
    _ncdu.MAX_LISTING_ENTRIES = 3
    try:
        with pytest.raises(ValueError, match="MAX_LISTING_ENTRIES"):
            build_size_tree(
                (f"f{i}.md", 1) for i in range(10)
            )
    finally:
        _ncdu.MAX_LISTING_ENTRIES = original


# ─── SizeNode.sorted_children ─────────────────────────────────────────────


def test_sorted_children_size_desc_ties_by_name() -> None:
    root = build_size_tree([
        ("a.md", 100),
        ("b.md", 100),
        ("c.md", 200),
    ])
    sorted_kids = root.sorted_children()
    assert [c.name for c in sorted_kids] == ["c.md", "a.md", "b.md"]


# ─── top_n_paths ──────────────────────────────────────────────────────────


def test_top_n_paths_returns_largest_in_desc_order() -> None:
    root = build_size_tree([
        ("a.md", 10),
        ("docs/b.md", 100),
        ("docs/c.md", 200),
        ("README.md", 50),
    ])
    rows = top_n_paths(root, 3)
    sizes = [r.size for r in rows]
    assert sizes == sorted(sizes, reverse=True)
    assert len(rows) == 3
    assert rows[0].size == 300


def test_top_n_paths_includes_directories_and_files() -> None:
    """Both dir aggregates and individual files participate in the
    ranking — same as `ncdu -o` output."""
    root = build_size_tree([
        ("docs/a.md", 100),
        ("docs/b.md", 100),
    ])
    rows = top_n_paths(root, 5)
    paths = [r.path for r in rows]
    assert "docs" in paths
    assert "docs/a.md" in paths
    assert "docs/b.md" in paths


def test_top_n_paths_n_zero_returns_empty() -> None:
    root = build_size_tree([("a.md", 100)])
    assert top_n_paths(root, 0) == []
    assert top_n_paths(root, -1) == []


def test_top_n_paths_n_larger_than_tree_returns_all() -> None:
    root = build_size_tree([("a.md", 10), ("b.md", 20)])
    rows = top_n_paths(root, 100)
    assert len(rows) == 2


def test_top_n_paths_empty_tree() -> None:
    root = build_size_tree([])
    assert top_n_paths(root, 10) == []


# ─── human_size ───────────────────────────────────────────────────────────


def test_human_size_renders_bytes_kb_mb() -> None:
    assert human_size(0) == "0 B"
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(1024 * 1024 * 5) == "5.0 MB"


# ─── format_non_interactive ───────────────────────────────────────────────


def test_format_non_interactive_basic_shape() -> None:
    root = build_size_tree([
        ("docs/a.md", 1000),
        ("docs/b.md", 2000),
        ("README.md", 500),
    ])
    out = format_non_interactive(root, 5, backend_label="primary")
    assert "Top 5 largest paths in primary backend:" in out
    assert "size" in out and "count" in out and "path" in out
    assert "docs/" in out
    assert "README.md" in out
    assert "total: 3.4 KB across 3 files" in out


def test_format_non_interactive_directory_paths_get_trailing_slash() -> None:
    root = build_size_tree([("docs/a.md", 1000)])
    out = format_non_interactive(root, 5, backend_label="primary")
    assert "docs/\n" in out
    assert "docs/a.md\n" in out


def test_format_non_interactive_empty_tree() -> None:
    root = build_size_tree([])
    out = format_non_interactive(root, 10, backend_label="dropbox")
    assert "No files in dropbox backend." in out
    assert "total: 0 B across 0 files" in out


def test_format_non_interactive_respects_top_n() -> None:
    root = build_size_tree(
        (f"file{i}.md", 100 + i) for i in range(50)
    )
    out = format_non_interactive(root, 3, backend_label="primary")
    assert "Top 3 largest paths" in out


def test_format_non_interactive_uses_passed_backend_label() -> None:
    root = build_size_tree([("a.md", 10)])
    out = format_non_interactive(root, 1, backend_label="sftp")
    assert "sftp backend" in out


# ─── entries_from_backend_listing ─────────────────────────────────────────


def test_entries_from_backend_listing_happy_path() -> None:
    listing = [
        {"relative_path": "a.md", "size": 100, "id": "x"},
        {"relative_path": "b.md", "size": 200, "id": "y"},
    ]
    result = list(entries_from_backend_listing(listing))
    assert result == [("a.md", 100), ("b.md", 200)]


def test_entries_from_backend_listing_skips_missing_relative_path() -> None:
    listing: list[dict[str, Any]] = [
        {"relative_path": "", "size": 100},
        {"size": 200},
        {"relative_path": "a.md", "size": 50},
    ]
    result = list(entries_from_backend_listing(listing))
    assert result == [("a.md", 50)]


def test_entries_from_backend_listing_handles_missing_size() -> None:
    listing = [{"relative_path": "a.md"}]
    result = list(entries_from_backend_listing(listing))
    assert result == [("a.md", 0)]


def test_entries_from_backend_listing_skips_uncoercible_size() -> None:
    listing: list[dict[str, Any]] = [
        {"relative_path": "a.md", "size": "not-a-number"},
        {"relative_path": "b.md", "size": 50},
    ]
    result = list(entries_from_backend_listing(listing))
    assert result == [("b.md", 50)]


# ─── CLI dispatch ────────────────────────────────────────────────────────


def _patched_engine(monkeypatch: pytest.MonkeyPatch, listing: list[dict[str, Any]],
                    mirrors: list[Any] | None = None) -> Any:
    """Build a stub engine + primary backend that returns `listing`
    from `list_files_recursive`, monkey-patch `_load_engine` and
    `_resolve_config` so the command runs without real I/O."""
    primary = MagicMock()
    primary.backend_name = "googledrive"
    primary.list_files_recursive.return_value = listing
    primary.config = MagicMock(root_folder="root-id")

    engine = MagicMock()
    engine._mirrors = mirrors or []
    engine._folder_id = "root-id"

    config = MagicMock()
    config.project_path = "/tmp/myproject"
    config.backend = "googledrive"

    monkeypatch.setattr(
        cli_module, "_load_engine",
        lambda config_path, with_pubsub=True: (engine, config, primary),
    )
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")
    return primary


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="ncdu is POSIX-only (curses isn't in CPython's Windows stdlib); the "
           "CLI dispatch tests below exercise behaviour reachable only on POSIX. "
           "Windows-specific gating is verified by test_cli_windows_gated_with_friendly_message.",
)
def test_cli_non_interactive_prints_top_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = [
        {"relative_path": "docs/a.md", "size": 1000, "id": "x"},
        {"relative_path": "docs/b.md", "size": 2000, "id": "y"},
        {"relative_path": "README.md", "size": 500, "id": "z"},
    ]
    _patched_engine(monkeypatch, listing)
    result = CliRunner().invoke(cli, ["ncdu", "--non-interactive", "--top", "5"])
    assert result.exit_code == 0, result.output
    assert "Top 5 largest paths in googledrive backend" in result.output
    assert "docs/" in result.output
    assert "total: 3.4 KB across 3 files" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="ncdu CLI flow is POSIX-only")
def test_cli_non_interactive_default_top_is_20(monkeypatch: pytest.MonkeyPatch) -> None:
    listing = [{"relative_path": f"f{i}.md", "size": 10 + i, "id": str(i)}
               for i in range(30)]
    _patched_engine(monkeypatch, listing)
    result = CliRunner().invoke(cli, ["ncdu", "--non-interactive"])
    assert result.exit_code == 0, result.output
    assert "Top 20 largest paths" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="ncdu CLI flow is POSIX-only")
def test_cli_remote_dispatches_to_named_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    sftp_mirror = MagicMock()
    sftp_mirror.backend_name = "sftp"
    sftp_mirror.list_files_recursive.return_value = [
        {"relative_path": "from-sftp.md", "size": 999, "id": "s"},
    ]
    sftp_mirror.config = MagicMock(root_folder="/srv/myproject")

    primary_listing = [
        {"relative_path": "from-primary.md", "size": 111, "id": "p"},
    ]
    primary = _patched_engine(monkeypatch, primary_listing, mirrors=[sftp_mirror])

    result = CliRunner().invoke(
        cli, ["ncdu", "--non-interactive", "--remote", "sftp"]
    )
    assert result.exit_code == 0, result.output
    assert "from-sftp.md" in result.output
    assert "from-primary.md" not in result.output
    assert "sftp backend" in result.output
    primary.list_files_recursive.assert_not_called()
    sftp_mirror.list_files_recursive.assert_called_once()


@pytest.mark.skipif(sys.platform == "win32", reason="ncdu CLI flow is POSIX-only")
def test_cli_unknown_remote_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_engine(monkeypatch, [])
    result = CliRunner().invoke(
        cli, ["ncdu", "--non-interactive", "--remote", "doesnotexist"]
    )
    assert result.exit_code != 0
    assert "Unknown backend" in result.output
    assert "doesnotexist" in result.output


@pytest.mark.skipif(sys.platform == "win32", reason="ncdu CLI flow is POSIX-only")
def test_cli_top_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_engine(monkeypatch, [])
    result = CliRunner().invoke(
        cli, ["ncdu", "--non-interactive", "--top", "0"]
    )
    assert result.exit_code != 0
    assert "positive integer" in result.output


def test_cli_windows_gated_with_friendly_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    result = CliRunner().invoke(cli, ["ncdu", "--non-interactive"])
    assert result.exit_code != 0
    assert "POSIX-only" in result.output
    assert "claude-mirror tree" in result.output
