"""Tests for `claude_mirror.snapshots._safe_join` — the path-traversal guard
that is the last line of defence against backend-supplied metadata writing
files outside the destination directory.

Contract (read from the source, not assumed):
    * Returns a `Path` resolved under `base` when the join is safe.
    * Raises `ValueError` when the resolved path escapes `base`.
    * Embedded NUL bytes are rejected explicitly (since v0.5.55) — historically
      this relied on `Path.resolve()` raising `ValueError`, but Python 3.13+
      on Windows no longer does. Up-front rejection keeps the security
      contract uniform across platforms and Python versions.
    * Empty string and `"."` resolve to `base` itself — they are NOT
      traversal but are not useful as a write target either; the caller
      is responsible for not feeding such inputs. We pin the actual
      observed behaviour rather than aspirational behaviour.

A bug here is a CVE: this is what protects `sync.py` download writes from
backend metadata that says e.g. `rel_path="../../../etc/passwd"`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_mirror.snapshots import _safe_join


# ---------------------------------------------------------------------------
# Parametrized contract sweep
# ---------------------------------------------------------------------------
#
# `should_be_safe == True`  → call must return a Path under `base`.
# `should_be_safe == False` → call must raise ValueError.
#
# Notes on edge cases (verified against the actual function):
#   * `""` and `"."` both resolve to `base` itself. No traversal, but also
#     not a useful write target. The function's contract is "no traversal"
#     so we mark these as safe (current observed behaviour).
#   * `..foo`, `...`, `.hidden` are LEGAL POSIX filenames whose name merely
#     STARTS with `..` — must NOT be over-rejected.
#   * NUL byte in path is rejected explicitly by `_safe_join` (v0.5.55+).
#     Earlier behaviour relied on `Path.resolve()` raising ValueError, but
#     Python 3.13+ on Windows no longer does — explicit rejection is
#     the only platform-independent way to honour the security contract.
SAFE_JOIN_CASES = [
    # ---- Safe paths --------------------------------------------------------
    ("foo/bar.md", True),
    ("a/b/c/d/file.md", True),
    ("notes/2026-05-07.md", True),
    ("file.with.dots.md", True),
    (".hidden", True),
    ("..foo", True),                    # starts-with-.. but isn't a traversal segment
    ("...", True),                      # legal POSIX filename
    ("a/" * 100 + "deep.md", True),     # deep but legal
    ("docs/..notes.md", True),          # nested dotfile-ish, still legal
    # ---- Unsafe — traversal -----------------------------------------------
    ("../escape.md", False),
    ("foo/../../escape.md", False),
    ("../../../etc/passwd", False),
    ("a/b/c/../../../../escape.md", False),
    # ---- Unsafe — absolute paths ------------------------------------------
    ("/etc/passwd", False),
    ("/abs/path/file.md", False),
    # ---- Unsafe — embedded NUL byte ---------------------------------------
    ("foo\x00bar.md", False),
    # ---- Unsafe — bare traversal ------------------------------------------
    ("..", False),
]


@pytest.mark.parametrize("rel,should_be_safe", SAFE_JOIN_CASES)
def test_safe_join_classifies_correctly(
    rel: str, should_be_safe: bool, tmp_path: Path
) -> None:
    """The full classification sweep — every input is either resolved
    safely under base, or rejected with ValueError. Nothing in between."""
    base = tmp_path / "project"
    base.mkdir()
    base_resolved = base.resolve()

    if should_be_safe:
        result = _safe_join(base, rel)
        assert isinstance(result, Path)
        # The result must be inside (or equal to) base.
        result_resolved = result.resolve()
        # Path.is_relative_to was added in 3.9; we target 3.11+.
        assert result_resolved.is_relative_to(base_resolved), (
            f"_safe_join({rel!r}) returned {result_resolved!r} "
            f"which is NOT under {base_resolved!r}"
        )
    else:
        with pytest.raises(ValueError):
            _safe_join(base, rel)


# ---------------------------------------------------------------------------
# Targeted non-parametrized cases
# ---------------------------------------------------------------------------


def test_safe_join_resolves_normal_path_to_under_base(tmp_path: Path) -> None:
    """Happy-path: a normal relative path resolves to a Path strictly inside
    base, with the expected file name and parent."""
    base = tmp_path / "project"
    base.mkdir()
    result = _safe_join(base, "subdir/file.md")
    assert result.name == "file.md"
    assert result.parent.name == "subdir"
    assert result.resolve().is_relative_to(base.resolve())


def test_safe_join_handles_redundant_slashes(tmp_path: Path) -> None:
    """`foo//bar.md` is treated like `foo/bar.md` — a normal POSIX path
    quirk that must not be misread as escape."""
    base = tmp_path / "project"
    base.mkdir()
    result = _safe_join(base, "foo//bar.md")
    assert result.resolve().is_relative_to(base.resolve())
    assert result.name == "bar.md"


def test_safe_join_returns_path_object(tmp_path: Path) -> None:
    """The return type is `pathlib.Path`, not `str` — callers in `sync.py`
    rely on this (some wrap with `str()`, some use Path methods directly)."""
    base = tmp_path / "project"
    base.mkdir()
    result = _safe_join(base, "x.md")
    assert isinstance(result, Path)


def test_safe_join_rejects_traversal_even_when_target_does_not_exist(
    tmp_path: Path,
) -> None:
    """The check must work on PURELY logical paths — the file doesn't have
    to exist on disk for traversal to be detected. (Important: backend
    metadata describes files we're about to CREATE.)"""
    base = tmp_path / "project"
    base.mkdir()
    # Sibling directory that doesn't exist yet.
    with pytest.raises(ValueError):
        _safe_join(base, "../sibling/file.md")


def test_safe_join_rejects_double_traversal_above_root(tmp_path: Path) -> None:
    """A path with mid-string `..` that escapes ABOVE base must be rejected
    even if individual segments alone wouldn't."""
    base = tmp_path / "project"
    base.mkdir()
    with pytest.raises(ValueError):
        _safe_join(base, "a/../../b.md")


def test_safe_join_error_message_mentions_refused_path(tmp_path: Path) -> None:
    """The ValueError message includes the offending rel_path so an operator
    debugging a security alert can tell which backend entry caused it."""
    base = tmp_path / "project"
    base.mkdir()
    with pytest.raises(ValueError, match="../escape"):
        _safe_join(base, "../escape")
