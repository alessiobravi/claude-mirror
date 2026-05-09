"""Tests for `claude-mirror tree` and the underlying `_tree.render_tree`.

Two layers of coverage:

  * Pure rendering (`_tree.render_tree`) — empty / single-file / deeply
    nested / depth-limited / ASCII vs Unicode / size+mtime toggles /
    sort order / subpath filtering.

  * CLI surface (`tree` command) — Tier 2 `--remote NAME` dispatch,
    unknown-name error, missing-PATH error.

All offline against `FakeStorageBackend` from conftest. No real
network, no real filesystem beyond `tmp_path`. <100ms each.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from claude_mirror._tree import render_tree
from claude_mirror.cli import cli


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _entry(rel_path: str, size: int = 0, mtime: str | None = None) -> dict[str, Any]:
    """Build the minimal listing-dict shape `render_tree` consumes —
    matches what every real backend emits from `list_files_recursive`."""
    d: dict[str, Any] = {
        "id": rel_path,
        "name": rel_path.rsplit("/", 1)[-1],
        "relative_path": rel_path,
        "md5Checksum": "",
        "size": size,
    }
    if mtime is not None:
        d["modifiedTime"] = mtime
    return d


# ─── Pure rendering layer ───────────────────────────────────────────────────────


def test_empty_listing_renders_root_plus_zero_footer() -> None:
    out = render_tree([])
    assert out.splitlines()[0] == "."
    assert "0 directories, 0 files" in out


def test_single_file_at_root_uses_last_connector() -> None:
    out = render_tree([_entry("CLAUDE.md", size=1234)])
    lines = out.splitlines()
    assert lines[0] == "."
    # Single child — must use the "last" connector, never the
    # mid-branch one.
    assert lines[1].startswith("└── ")
    assert "├──" not in out
    assert "CLAUDE.md" in lines[1]
    assert "1.2 KB" in lines[1]
    assert "1 directories, 1 files" in out or "0 directories, 1 files" in out


def test_deep_nesting_uses_pipe_continuation_for_siblings() -> None:
    entries = [
        _entry("a.md", size=10),
        _entry("dir/b.md", size=20),
        _entry("dir/c.md", size=30),
        _entry("z.md", size=40),
    ]
    out = render_tree(entries)
    lines = out.splitlines()
    # Sort: dir first, then files. dir/ has siblings below (a.md, z.md),
    # so the leading column for dir/'s children must be the pipe glyph.
    dir_line = next(i for i, ln in enumerate(lines) if "dir/" in ln)
    # The two children of dir/ live on the next two lines, prefixed with
    # `│   ` because dir/ is NOT the last entry under root.
    assert lines[dir_line + 1].startswith("│   ")
    assert lines[dir_line + 2].startswith("│   ")
    # The last-of-its-parent inside dir/ uses └──.
    assert "└── c.md" in lines[dir_line + 2]


def test_last_dir_blank_continuation_does_not_use_pipe() -> None:
    entries = [
        _entry("dir/a.md", size=5),
        _entry("dir/b.md", size=5),
    ]
    out = render_tree(entries)
    lines = out.splitlines()
    # dir/ is the only child of root → it's "last", so its sub-rows must
    # be prefixed with blanks, not with the pipe glyph.
    children_lines = [ln for ln in lines if ln.startswith("    ")]
    assert len(children_lines) >= 2
    assert not any(ln.startswith("│") for ln in children_lines)


def test_depth_one_hides_deeper_entries_and_summarises() -> None:
    entries = [
        _entry("top.md", size=5),
        _entry("dir/a.md", size=5),
        _entry("dir/sub/b.md", size=5),
        _entry("dir/sub/c.md", size=5),
    ]
    out = render_tree(entries, depth=1)
    # top-level entries visible (top.md and dir/), nested ones hidden.
    assert "top.md" in out
    assert "dir/" in out
    assert "a.md" not in out
    assert "b.md" not in out
    assert "more files in subtrees" in out


def test_ascii_mode_emits_no_unicode_box_chars() -> None:
    entries = [
        _entry("a.md", size=5),
        _entry("dir/b.md", size=5),
        _entry("dir/c.md", size=5),
    ]
    out = render_tree(entries, ascii_only=True)
    for ch in ("├", "└", "│", "─"):
        assert ch not in out, f"ASCII mode leaked unicode char {ch!r}"
    assert "+--" in out or "\\--" in out


def test_no_show_size_omits_size_column() -> None:
    out = render_tree(
        [_entry("CLAUDE.md", size=1234)],
        show_size=False,
    )
    assert "1.2 KB" not in out
    # Footer total bytes also drops out when sizes are off.
    assert "total" not in out


def test_show_mtime_appends_mtime_when_present() -> None:
    out = render_tree(
        [_entry("a.md", size=10, mtime="2026-05-09T10:00:00Z")],
        show_mtime=True,
    )
    assert "2026-05-09T10:00:00Z" in out


def test_sort_order_directories_before_files_alphabetical_within() -> None:
    entries = [
        _entry("zfile.md", size=1),
        _entry("afile.md", size=1),
        _entry("z_dir/leaf.md", size=1),
        _entry("a_dir/leaf.md", size=1),
    ]
    out = render_tree(entries)
    lines = out.splitlines()
    indices = {
        "a_dir/": next(i for i, ln in enumerate(lines) if "a_dir/" in ln),
        "z_dir/": next(i for i, ln in enumerate(lines) if "z_dir/" in ln),
        "afile.md": next(
            i for i, ln in enumerate(lines)
            if ln.endswith("afile.md") or "afile.md  " in ln
        ),
        "zfile.md": next(
            i for i, ln in enumerate(lines)
            if ln.endswith("zfile.md") or "zfile.md  " in ln
        ),
    }
    assert indices["a_dir/"] < indices["z_dir/"]
    assert indices["z_dir/"] < indices["afile.md"]
    assert indices["afile.md"] < indices["zfile.md"]


def test_subpath_renders_only_its_subtree() -> None:
    entries = [
        _entry("top.md", size=5),
        _entry("memory/notes.md", size=10),
        _entry("memory/refs/x.md", size=20),
        _entry("docs/architecture.md", size=15),
    ]
    out = render_tree(entries, sub_path="memory")
    assert "notes.md" in out
    assert "refs/" in out
    assert "x.md" in out
    # Files outside the subtree must NOT appear.
    assert "top.md" not in out
    assert "architecture.md" not in out


def test_subpath_missing_raises_filenotfound() -> None:
    with pytest.raises(FileNotFoundError):
        render_tree([_entry("a.md", size=5)], sub_path="ghost")


def test_footer_total_bytes_uses_human_size() -> None:
    out = render_tree([
        _entry("a.md", size=1024),
        _entry("b.md", size=1024),
    ])
    # 2048 bytes → "2.0 KB" via _human_size — assert the human suffix is
    # present rather than the raw number.
    assert "2.0 KB total" in out


# ─── CLI layer ──────────────────────────────────────────────────────────────────


def _save_cfg(make_config, tmp_path: Path, **overrides) -> tuple[str, Any]:
    cfg = make_config(**overrides)
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    return str(cfg_path), cfg


def test_cli_tree_unknown_remote_exits_one_with_listing(
    monkeypatch, make_config, tmp_path, fake_backend,
) -> None:
    cfg_path, _cfg = _save_cfg(make_config, tmp_path)

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (fake_backend, []),
    )
    monkeypatch.setattr(
        cli_module, "_create_storage", lambda c: fake_backend,
    )
    monkeypatch.setattr(
        cli_module, "_create_notifier", lambda c, s: None,
    )

    result = CliRunner().invoke(
        cli, ["tree", "--remote", "no-such-backend", "--config", cfg_path],
    )
    assert result.exit_code == 1
    out = _strip_ansi(result.output)
    assert "Unknown --remote" in out
    assert "fake" in out


def test_cli_tree_with_remote_dispatches_to_named_mirror(
    monkeypatch, make_config, tmp_path, fake_backend,
) -> None:
    """Tier 2: when --remote NAME matches a configured mirror, that
    mirror's listing wins. Verified by giving the mirror a unique file
    name the primary doesn't have, then asserting the rendered tree."""
    from tests.conftest import FakeStorageBackend

    primary = fake_backend
    mirror = FakeStorageBackend()
    mirror.backend_name = "mirror-b"  # type: ignore[misc]  # test-only override; the conftest fixture sets a class default
    # Seed each backend with a distinguishing file.
    primary._store_file(b"P", "primary-only.md", primary.root_folder_id, None)
    mirror._store_file(b"M", "mirror-only.md", mirror.root_folder_id, None)

    cfg_path, _cfg = _save_cfg(make_config, tmp_path)

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (primary, [mirror]),
    )
    monkeypatch.setattr(
        cli_module, "_create_storage", lambda c: primary,
    )
    monkeypatch.setattr(
        cli_module, "_create_notifier", lambda c, s: None,
    )

    result = CliRunner().invoke(
        cli, ["tree", "--remote", "mirror-b", "--config", cfg_path],
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "mirror-only.md" in out
    assert "primary-only.md" not in out


def test_cli_tree_subpath_missing_exits_one_cleanly(
    monkeypatch, make_config, tmp_path, fake_backend,
) -> None:
    fake_backend._store_file(b"X", "real.md", fake_backend.root_folder_id, None)
    cfg_path, _cfg = _save_cfg(make_config, tmp_path)

    from claude_mirror import cli as cli_module
    monkeypatch.setattr(
        cli_module, "_create_storage_set", lambda c: (fake_backend, []),
    )
    monkeypatch.setattr(
        cli_module, "_create_storage", lambda c: fake_backend,
    )
    monkeypatch.setattr(
        cli_module, "_create_notifier", lambda c, s: None,
    )

    result = CliRunner().invoke(
        cli, ["tree", "ghost-dir", "--config", cfg_path],
    )
    assert result.exit_code == 1
    out = _strip_ansi(result.output)
    assert "path not found" in out
    assert "ghost-dir" in out
