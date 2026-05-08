"""Tests for `.claude_mirror_ignore` — gitignore-style project-tree exclusions.

The ignore file is a v0.5.39 addition that complements the YAML
`exclude_patterns` config. It lives at `<project_path>/.claude_mirror_ignore`
and uses gitignore syntax (subset). These tests cover:

  * empty / comment-only files are OK (return None / never match)
  * basic patterns
  * negation (`!pattern`) re-includes a previously-excluded path
  * directory-only rules (`pattern/`) match only when used as a parent
  * `**` matches any number of path segments
  * `*` does NOT cross `/`
  * anchored (`/pattern`) vs unanchored matching
  * the file itself is auto-excluded from sync
  * pathologically long patterns are rejected at parse time (ReDoS guard)

All tests run offline and use the `IgnoreSet.from_lines` constructor
where possible; the file-path tests use tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_mirror.ignore import IGNORE_FILENAME, IgnoreSet, _MAX_PATTERN_LENGTH
from claude_mirror.sync import SyncEngine


# ── from_file: empty / comment-only / missing ─────────────────────────────────

def test_from_file_missing_returns_none(tmp_path):
    """No file → IgnoreSet.from_file returns None (cheap fast-path)."""
    assert IgnoreSet.from_file(tmp_path / "nope") is None


def test_from_file_empty_returns_none(tmp_path):
    """Empty file → None (no rules → no matching to do)."""
    p = tmp_path / IGNORE_FILENAME
    p.write_text("")
    assert IgnoreSet.from_file(p) is None


def test_from_file_comment_only_returns_none(tmp_path):
    """Only comments + blank lines → None."""
    p = tmp_path / IGNORE_FILENAME
    p.write_text("# this is a comment\n\n# another\n   \n")
    assert IgnoreSet.from_file(p) is None


def test_from_file_with_one_rule_returns_set(tmp_path):
    """One real rule → an IgnoreSet that excludes matching paths."""
    p = tmp_path / IGNORE_FILENAME
    p.write_text("# header\n*.tmp\n")
    igs = IgnoreSet.from_file(p)
    assert igs is not None
    assert igs.is_excluded("foo.tmp") is True
    assert igs.is_excluded("foo.md") is False


# ── basic patterns ────────────────────────────────────────────────────────────

def test_basic_glob_extension():
    igs = IgnoreSet.from_lines(["*.log"])
    assert igs is not None
    assert igs.is_excluded("server.log") is True
    assert igs.is_excluded("logs/server.log") is True
    assert igs.is_excluded("server.md") is False


def test_basic_literal_filename():
    igs = IgnoreSet.from_lines(["secret.env"])
    assert igs is not None
    assert igs.is_excluded("secret.env") is True
    assert igs.is_excluded("docs/secret.env") is True
    assert igs.is_excluded("secret.md") is False


# ── single * does not cross / ─────────────────────────────────────────────────

def test_single_star_does_not_cross_separator():
    """`docs/*.md` matches docs/foo.md but NOT docs/sub/foo.md."""
    igs = IgnoreSet.from_lines(["docs/*.md"])
    assert igs is not None
    assert igs.is_excluded("docs/foo.md") is True
    assert igs.is_excluded("docs/sub/foo.md") is False


# ── ** matches any number of segments ─────────────────────────────────────────

def test_double_star_matches_any_depth():
    """`**/*.tmp` matches files at any depth."""
    igs = IgnoreSet.from_lines(["**/*.tmp"])
    assert igs is not None
    assert igs.is_excluded("foo.tmp") is True
    assert igs.is_excluded("a/foo.tmp") is True
    assert igs.is_excluded("a/b/c/foo.tmp") is True


def test_double_star_in_middle():
    """`docs/**/secret.md` matches the leaf at any nested depth under docs/."""
    igs = IgnoreSet.from_lines(["docs/**/secret.md"])
    assert igs is not None
    assert igs.is_excluded("docs/secret.md") is True
    assert igs.is_excluded("docs/a/secret.md") is True
    assert igs.is_excluded("docs/a/b/secret.md") is True
    assert igs.is_excluded("other/secret.md") is False


# ── anchored (leading /) vs unanchored ────────────────────────────────────────

def test_anchored_pattern_matches_only_at_root():
    igs = IgnoreSet.from_lines(["/build"])
    assert igs is not None
    assert igs.is_excluded("build") is True
    assert igs.is_excluded("build/x.md") is True
    # Same name deeper in the tree is NOT excluded — anchored at root.
    assert igs.is_excluded("src/build") is False
    assert igs.is_excluded("src/build/x.md") is False


def test_unanchored_pattern_matches_anywhere():
    igs = IgnoreSet.from_lines(["build"])
    assert igs is not None
    assert igs.is_excluded("build") is True
    assert igs.is_excluded("build/x.md") is True
    assert igs.is_excluded("src/build") is True
    assert igs.is_excluded("src/build/x.md") is True
    # But NOT a path where `build` is just a substring of a name.
    assert igs.is_excluded("rebuild.md") is False


# ── directory-only rules (trailing /) ─────────────────────────────────────────

def test_directory_only_rule_matches_parent():
    """`logs/` excludes everything under logs/ but does NOT exclude a
    leaf file literally named `logs`."""
    igs = IgnoreSet.from_lines(["logs/"])
    assert igs is not None
    # As a parent — yes.
    assert igs.is_excluded("logs/server.log") is True
    assert igs.is_excluded("logs/sub/x.txt") is True
    # As a leaf file — no (file walker only yields file paths; a leaf
    # named `logs` is a file, not a dir).
    assert igs.is_excluded("logs") is False


def test_directory_only_anchored():
    """`/build/` matches only the root-level build/ dir."""
    igs = IgnoreSet.from_lines(["/build/"])
    assert igs is not None
    assert igs.is_excluded("build/x") is True
    assert igs.is_excluded("src/build/x") is False


# ── negation (!pattern) ───────────────────────────────────────────────────────

def test_negation_re_includes_after_exclude():
    """Last matching rule wins — a `!` rule re-includes."""
    igs = IgnoreSet.from_lines([
        "*.md",
        "!keep.md",
    ])
    assert igs is not None
    assert igs.is_excluded("foo.md") is True
    assert igs.is_excluded("keep.md") is False


def test_negation_only_re_includes_when_after_exclude():
    """A negation that doesn't follow an exclude is a no-op (path was
    already kept)."""
    igs = IgnoreSet.from_lines([
        "!keep.md",
        "*.tmp",
    ])
    assert igs is not None
    assert igs.is_excluded("keep.md") is False  # no rule excludes it
    assert igs.is_excluded("foo.tmp") is True


def test_negation_then_re_exclude():
    """Order matters — three rules in sequence flip the verdict each
    time the path matches."""
    igs = IgnoreSet.from_lines([
        "drafts/*",
        "!drafts/published.md",
        "drafts/published.md",
    ])
    assert igs is not None
    assert igs.is_excluded("drafts/foo.md") is True
    # Re-included by rule 2 then re-excluded by rule 3 → final = exclude.
    assert igs.is_excluded("drafts/published.md") is True


# ── auto-exclusion of `.claude_mirror_ignore` itself ─────────────────────────

def test_ignore_file_itself_auto_excluded_via_engine(tmp_path, make_config, write_files):
    """The `.claude_mirror_ignore` file is auto-excluded from the project
    walker even when no ignore rules apply to it. Verifies via SyncEngine
    so we exercise the full integration: walker → _is_excluded → result."""
    write_files({
        "a.md": "kept",
        IGNORE_FILENAME: "*.tmp\n",
        "foo.tmp": "should be excluded by rule",
    })
    cfg = make_config()
    eng = SyncEngine.__new__(SyncEngine)
    # Run the constructor's exclude/ignore initialisation via the real
    # __init__ — it needs storage/manifest etc., so we set just the
    # fields _is_excluded touches.
    SyncEngine.__init__(
        eng,
        config=cfg,
        storage=_FakeStorage(),
        manifest=_FakeManifest(cfg.project_path),
        merge=_FakeMerge(),
    )
    # The walker yields a.md, .claude_mirror_ignore, foo.tmp; after
    # exclusion only a.md should remain.
    found = eng._local_files()
    assert "a.md" in found
    assert IGNORE_FILENAME not in found, "ignore file must auto-exclude itself"
    assert "foo.tmp" not in found, "explicit rule should still apply"


def test_is_excluded_directly_excludes_ignore_filename(make_config):
    """Even without a `.claude_mirror_ignore` file present, the engine
    auto-excludes the filename so it never accidentally syncs."""
    cfg = make_config()
    eng = SyncEngine.__new__(SyncEngine)
    SyncEngine.__init__(
        eng,
        config=cfg,
        storage=_FakeStorage(),
        manifest=_FakeManifest(cfg.project_path),
        merge=_FakeMerge(),
    )
    assert eng._is_excluded(IGNORE_FILENAME) is True


# ── ReDoS / long-input safety ────────────────────────────────────────────────

def test_pathologically_long_pattern_rejected_at_parse_time():
    """Lines longer than _MAX_PATTERN_LENGTH are dropped silently — they
    never reach re.compile, so no malicious input can lead to a
    catastrophic backtracking blow-up."""
    long_pattern = "a" * (_MAX_PATTERN_LENGTH + 1)
    igs = IgnoreSet.from_lines([long_pattern, "*.md"])
    assert igs is not None  # the *.md rule survives
    # The long pattern was dropped, so its supposed match never fires.
    assert igs.is_excluded("a" * (_MAX_PATTERN_LENGTH + 1)) is False
    assert igs.is_excluded("foo.md") is True


def test_redos_style_input_does_not_blow_up_compile():
    """Patterns that LOOK like they could trigger ReDoS in a naive
    translator don't actually get translated to nested quantifiers
    here — translation emits only ``.*`` / ``[^/]*`` / fixed-length
    pieces, so the compiled regex is bounded and safe."""
    igs = IgnoreSet.from_lines([
        "**/**/**/**/**/foo.md",  # many segments, harmless
        "*?*?*?*?*?",              # alternating wildcards, harmless
    ])
    assert igs is not None
    # And the patterns still match what they should.
    assert igs.is_excluded("a/b/c/d/e/foo.md") is True


# ── end-of-line / whitespace handling ─────────────────────────────────────────

def test_trailing_whitespace_stripped():
    """Trailing whitespace on a rule line is stripped — `foo.md   ` and
    `foo.md` behave identically."""
    igs = IgnoreSet.from_lines(["foo.md   "])
    assert igs is not None
    assert igs.is_excluded("foo.md") is True


def test_blank_lines_skipped():
    """Empty and whitespace-only lines are not rules."""
    igs = IgnoreSet.from_lines(["", "   ", "\t", "*.md"])
    assert igs is not None
    assert igs.is_excluded("foo.md") is True


# ── helpers ───────────────────────────────────────────────────────────────────

class _FakeStorage:
    """Tiny stub of StorageBackend — SyncEngine.__init__ needs an object
    to bind to but doesn't call any of its methods until a sync command
    actually runs. _is_excluded is unit-testable without it."""


class _FakeManifest:
    """Stub Manifest object. SyncEngine.__init__ does not call into it
    during construction either."""

    def __init__(self, project_path: str) -> None:
        self.project_path = project_path


class _FakeMerge:
    """Stub MergeHandler — same rationale as _FakeManifest above."""
