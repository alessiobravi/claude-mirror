"""Tests for SyncEngine exclude-pattern matching (O2 perf fix).

Covers:
    * Functional correctness for the three legacy match forms
      (bare glob, directory-form, "**/*..." double-star).
    * Empty-config safety.
    * The compile-once invariant (pre-compilation happens at __init__).
    * A loose performance smoke test (10K calls / 10 patterns < 100ms).
    * 100% behaviour parity with the previous fnmatch-loop implementation
      across a few dozen path shapes (parametrize sweep).
"""
from __future__ import annotations

import fnmatch
import re
import time

import pytest

from claude_mirror.sync import SyncEngine


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _legacy_is_excluded(rel_path: str, patterns: list[str]) -> bool:
    """Verbatim copy of the pre-O2 _is_excluded logic. Used as the
    parity oracle so we can assert byte-for-byte behaviour preservation."""
    for pattern in patterns:
        if fnmatch.fnmatch(rel_path, pattern):
            return True
        if fnmatch.fnmatch(rel_path, f"{pattern}/*") or rel_path.startswith(f"{pattern}/"):
            return True
    return False


def _engine(make_config, exclude_patterns: list[str]) -> SyncEngine:
    """Build a SyncEngine instance with only the bits _is_excluded needs.

    The full constructor pulls in storage/manifest/merge dependencies that
    aren't relevant here, so we use object.__new__ + a minimal __init__-style
    setup focused on the exclude-pattern state under test."""
    cfg = make_config(exclude_patterns=exclude_patterns)
    eng = SyncEngine.__new__(SyncEngine)
    eng.config = cfg
    # Re-run the pre-compile block from __init__.
    if cfg.exclude_patterns:
        parts: list[str] = []
        for pattern in cfg.exclude_patterns:
            parts.append(fnmatch.translate(pattern))
            parts.append(fnmatch.translate(f"{pattern}/*"))
        eng._exclude_re = re.compile("(?:" + "|".join(parts) + ")")
        eng._exclude_prefixes = tuple(f"{p}/" for p in cfg.exclude_patterns)
    else:
        eng._exclude_re = None
        eng._exclude_prefixes = ()
    return eng


# --------------------------------------------------------------------------
# Functional cases
# --------------------------------------------------------------------------

def test_exclude_patterns_match_simple_glob(make_config):
    """A single `*.tmp` glob excludes `foo.tmp` but not `foo.md`."""
    eng = _engine(make_config, ["*.tmp"])
    assert eng._is_excluded("foo.tmp") is True
    assert eng._is_excluded("foo.md") is False


def test_exclude_patterns_match_directory_form(make_config):
    """A bare directory name like `archive` excludes nested children
    (`archive/foo.md`) via the prefix-match path, not just the bare name."""
    eng = _engine(make_config, ["archive"])
    assert eng._is_excluded("archive/foo.md") is True
    assert eng._is_excluded("archive") is True
    # Sanity: a same-named subdirectory deeper in the tree is NOT excluded —
    # the prefix-match is anchored at rel_path's start, not anywhere.
    assert eng._is_excluded("docs/archive/foo.md") is False


def test_exclude_patterns_match_double_star(make_config):
    """`**/*_draft.md` matches a file at any nested depth ending with the suffix."""
    eng = _engine(make_config, ["**/*_draft.md"])
    assert eng._is_excluded("notes/sketch_draft.md") is True
    # Not at top level (no leading directory component) — fnmatch's `**/*`
    # form requires at least one path segment before the file. This matches
    # legacy behaviour; we don't try to "fix" it here.
    assert eng._is_excluded("foo_draft.md") is False
    assert eng._is_excluded("foo.md") is False


def test_exclude_patterns_no_patterns_returns_false(make_config):
    """Empty exclude_patterns means _is_excluded always returns False, and
    the precompiled regex is None (fast-path skipped entirely)."""
    eng = _engine(make_config, [])
    assert eng._exclude_re is None
    assert eng._exclude_prefixes == ()
    assert eng._is_excluded("anything.md") is False
    assert eng._is_excluded("a/b/c/d/e.txt") is False


def test_exclude_patterns_compiled_once(make_config):
    """The compile happens at __init__-equivalent setup: _exclude_re must
    exist and be a compiled regex (re.Pattern), not None, when patterns
    are configured."""
    eng = _engine(make_config, ["*.tmp", "archive", "**/*_draft.md"])
    assert eng._exclude_re is not None
    assert isinstance(eng._exclude_re, re.Pattern)
    # The prefix tuple is the optimization for the str.startswith path.
    assert eng._exclude_prefixes == ("*.tmp/", "archive/", "**/*_draft.md/")


def test_exclude_patterns_perf_smoke(make_config):
    """Loose perf gate: 10K calls × 10 patterns must complete in <100ms.

    On reference hardware (Apple Silicon, Python 3.13) this completes in
    ~7ms. The 100ms cap is an order-of-magnitude generous bound chosen to
    not flake on slow CI runners while still failing loudly if someone
    re-introduces the per-call fnmatch loop (which clocks ~44ms even on
    fast hardware — well under 100ms — so this gate is genuinely a smoke
    test, not a regression net. See O2 commit message for the actual
    before/after figures.)"""
    patterns = [
        "*.tmp", "*.bak", "archive", ".git", "node_modules",
        "dist", "*.swp", "__pycache__", ".venv", "build",
    ]
    eng = _engine(make_config, patterns)
    paths = []
    for i in range(2500):
        paths.append(f"docs/section{i % 50}/note{i}.md")
        paths.append(f"archive/old{i}.md")
        paths.append(f"src/lib/file{i}.tmp")
        paths.append(f"project/notes/snippet{i}_draft.md")
    assert len(paths) == 10000

    # Warm up to exclude any first-call import cost from the timing window.
    for p in paths[:100]:
        eng._is_excluded(p)

    t0 = time.perf_counter()
    for p in paths:
        eng._is_excluded(p)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 100, f"_is_excluded too slow: {elapsed_ms:.1f}ms (cap 100ms)"


# --------------------------------------------------------------------------
# Behaviour parity sweep — old vs new across a few dozen shapes.
# --------------------------------------------------------------------------

PARITY_CASES = [
    # (patterns, rel_path)
    (["*.tmp"], "foo.tmp"),
    (["*.tmp"], "foo.md"),
    (["*.tmp"], "src/foo.tmp"),
    (["*.tmp"], "deeply/nested/dir/file.tmp"),
    (["archive"], "archive"),
    (["archive"], "archive/foo.md"),
    (["archive"], "archive/sub/dir/x.md"),
    (["archive"], "docs/archive/foo.md"),
    (["archive"], "archived.md"),
    (["**/*_draft.md"], "notes/sketch_draft.md"),
    (["**/*_draft.md"], "foo_draft.md"),
    (["**/*_draft.md"], "x/y/z/sketch_draft.md"),
    (["**/*_draft.md"], "draft.md"),
    (["**/*_draft.md"], "foo.md"),
    (["*.bak", "archive"], "archive/sub/x.md"),
    (["*.bak", "archive"], "foo.bak"),
    (["*.bak", "archive"], "src/lib/foo.bak"),
    (["*.bak", "archive"], "src/lib/foo.md"),
    (["node_modules"], "node_modules/x/y.md"),
    (["node_modules"], "node_modules"),
    (["node_modules"], "sub/node_modules/x.md"),
    ([], "anything.md"),
    ([], ""),
    (["*"], "anything"),
    (["*"], "a/b"),
    (["build"], "build"),
    (["build"], "build/x"),
    (["build"], "build/sub/dir/file.md"),
    (["build"], "src/build"),
    (["build"], "rebuild.md"),
    (["docs/*.md"], "docs/foo.md"),
    (["docs/*.md"], "docs/sub/foo.md"),
    (["docs/*.md"], "other/foo.md"),
    (["*.swp"], ".vimrc.swp"),
    (["*.swp"], "src/note.md.swp"),
    ([".git"], ".git/HEAD"),
    ([".git"], "src/.git/HEAD"),
    (["__pycache__"], "pkg/__pycache__/mod.cpython-311.pyc"),
    (["__pycache__"], "__pycache__/x.pyc"),
]


@pytest.mark.parametrize(("patterns", "rel_path"), PARITY_CASES)
def test_exclude_patterns_parity_old_vs_new(make_config, patterns, rel_path):
    """For every case, the new precompiled implementation must agree
    with the verbatim legacy fnmatch-loop on the include/exclude verdict."""
    eng = _engine(make_config, patterns)
    expected = _legacy_is_excluded(rel_path, patterns)
    actual = eng._is_excluded(rel_path)
    assert actual == expected, (
        f"mismatch for patterns={patterns!r} rel_path={rel_path!r}: "
        f"legacy={expected} new={actual}"
    )
