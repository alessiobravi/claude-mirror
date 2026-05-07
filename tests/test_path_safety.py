"""Tests for `Manifest._is_safe_relpath` — the path-traversal guard for
manifest keys. Verifies the simplification of the dead `startswith("..")`
branch (O4) didn't regress acceptance/rejection semantics."""
from __future__ import annotations

import pytest

from claude_mirror.manifest import Manifest


def test_manifest_rejects_double_dot_path_component() -> None:
    """A path with a literal `..` segment is rejected as traversal."""
    assert Manifest._is_safe_relpath("../escape.md") is False
    assert Manifest._is_safe_relpath("foo/../bar.md") is False
    assert Manifest._is_safe_relpath("..") is False


def test_manifest_accepts_normal_path() -> None:
    """A plain relative path with no `..` components passes."""
    assert Manifest._is_safe_relpath("CLAUDE.md") is True
    assert Manifest._is_safe_relpath("docs/notes.md") is True
    assert Manifest._is_safe_relpath("a/b/c/d.md") is True


def test_manifest_accepts_dotfile_not_double_dot() -> None:
    """Components that merely START with `..` but aren't literally `..`
    are NOT path traversal — verify the simplification didn't over-reject."""
    # `.hidden` is a normal dotfile.
    assert Manifest._is_safe_relpath(".hidden") is True
    # `..foo` starts with `..` but is a legal POSIX filename.
    assert Manifest._is_safe_relpath("..foo") is True
    # `...trailing` is also a legal filename.
    assert Manifest._is_safe_relpath("...trailing") is True
    # Nested dotfile-ish names also fine.
    assert Manifest._is_safe_relpath("docs/..notes.md") is True


def test_manifest_rejects_empty_and_absolute() -> None:
    """Sanity: the other rejection rules still hold after the cleanup."""
    assert Manifest._is_safe_relpath("") is False
    assert Manifest._is_safe_relpath("/abs/path.md") is False
    assert Manifest._is_safe_relpath("\\abs\\path.md") is False
    assert Manifest._is_safe_relpath("foo\x00bar") is False
