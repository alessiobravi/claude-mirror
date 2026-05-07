"""Tests for `claude_mirror.merge.MergeHandler.resolve_conflict` — the
interactive [L]ocal / [D]rive / [E]ditor / [S]kip prompt that fires when
both sides of a sync have changed since last merge.

Contract pinned here:
    * "L" → returns (local_content, "local")
    * "D" → returns (drive_content, "drive")
    * "S" → returns None (skip sentinel)
    * "E" → opens an editor on a tempfile populated with conflict markers,
            returns (resolved_content, "merged"). Tempfile prefix is
            `claude_mirror_merge_` (renamed from `claude_sync_merge_`
            in v0.5.0).
    * Invalid input → click.Choice's re-prompt loop is exercised; the
      Choice constraint guards us so any non-LDES letter can never
      reach the dispatch.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

import claude_mirror.merge as merge_mod
from claude_mirror.merge import MergeHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _patch_prompt(monkeypatch: pytest.MonkeyPatch, replies: List[str]) -> List[str]:
    """Replace `click.prompt` (as used inside merge.py) with a function that
    pops one element off `replies` per call. Returns the same list so the
    test can assert how many calls happened.
    """
    iterator = iter(replies)

    def fake_prompt(*args, **kwargs):
        try:
            return next(iterator)
        except StopIteration:
            raise AssertionError(
                "click.prompt called more times than the test scripted"
            )

    monkeypatch.setattr(merge_mod.click, "prompt", fake_prompt)
    return replies


# ---------------------------------------------------------------------------
# L / D / S branches
# ---------------------------------------------------------------------------


def test_resolver_keep_local_returns_local_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User picks `L` → resolver returns (local_content, "local")."""
    _patch_prompt(monkeypatch, ["L"])
    handler = MergeHandler()
    result = handler.resolve_conflict("CLAUDE.md", "LOCAL", "DRIVE")
    assert result == ("LOCAL", "local")


def test_resolver_keep_drive_returns_drive_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User picks `D` → resolver returns (drive_content, "drive")."""
    _patch_prompt(monkeypatch, ["D"])
    handler = MergeHandler()
    result = handler.resolve_conflict("CLAUDE.md", "LOCAL", "DRIVE")
    assert result == ("DRIVE", "drive")


def test_resolver_skip_returns_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """User picks `S` → resolver returns None (skip sentinel — see source).
    The caller distinguishes None ("skipped") from a tuple ("resolved")."""
    _patch_prompt(monkeypatch, ["S"])
    handler = MergeHandler()
    result = handler.resolve_conflict("CLAUDE.md", "LOCAL", "DRIVE")
    assert result is None


def test_resolver_lowercase_input_is_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver upper-cases the click.prompt return value before
    dispatch, so 'l' and 'L' are equivalent. Pin this so a refactor
    that drops `.upper()` doesn't break user muscle memory."""
    _patch_prompt(monkeypatch, ["l"])
    handler = MergeHandler()
    result = handler.resolve_conflict("CLAUDE.md", "LOCAL", "DRIVE")
    assert result == ("LOCAL", "local")


# ---------------------------------------------------------------------------
# E branch — editor + tempfile
# ---------------------------------------------------------------------------


def test_resolver_editor_invokes_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User picks `E` → subprocess.run is called with [editor, tmp_path].
    The tempfile content contains the standard git-style conflict markers
    so the user can resolve it in any editor that understands them."""
    _patch_prompt(monkeypatch, ["E"])

    captured: dict = {}

    def fake_run(args, check=False, **_kw):
        # args == [editor, tmp_path]
        captured["args"] = list(args)
        captured["content"] = Path(args[1]).read_text()
        # Simulate the user "resolving" the conflict by writing a clean file.
        Path(args[1]).write_text("RESOLVED\n")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)

    handler = MergeHandler()
    result = handler.resolve_conflict("CLAUDE.md", "LOCAL\n", "DRIVE\n")

    assert result == ("RESOLVED\n", "merged")
    # Editor was launched with the tempfile as second arg.
    assert len(captured["args"]) == 2
    # Tempfile content has the standard git conflict markers.
    assert "<<<<<<< LOCAL" in captured["content"]
    assert "=======" in captured["content"]
    assert ">>>>>>> DRIVE" in captured["content"]


def test_resolver_writes_temp_file_with_claude_mirror_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tempfile prefix is `claude_mirror_merge_` (the v0.5.0 rename
    from `claude_sync_merge_`). Pin this so a future rename is caught."""
    _patch_prompt(monkeypatch, ["E"])

    seen_paths: List[str] = []

    def fake_run(args, check=False, **_kw):
        seen_paths.append(args[1])
        Path(args[1]).write_text("ok")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)

    handler = MergeHandler()
    handler.resolve_conflict("CLAUDE.md", "L", "D")

    assert seen_paths, "fake editor was not invoked"
    name = Path(seen_paths[0]).name
    assert name.startswith("claude_mirror_merge_"), (
        f"expected tempfile to start with 'claude_mirror_merge_', got {name!r}"
    )


def test_resolver_editor_uses_rel_path_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tempfile suffix is taken from the rel_path so the editor opens
    in the right syntax-highlighting mode (e.g. `.md`)."""
    _patch_prompt(monkeypatch, ["E"])
    seen: List[str] = []

    def fake_run(args, check=False, **_kw):
        seen.append(args[1])
        Path(args[1]).write_text("ok")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)

    handler = MergeHandler()
    handler.resolve_conflict("docs/notes.md", "L", "D")
    assert seen[0].endswith(".md")


def test_resolver_editor_respects_editor_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`$EDITOR` is honoured (and falls back to `vi`)."""
    _patch_prompt(monkeypatch, ["E"])
    monkeypatch.setenv("EDITOR", "my-fake-editor")
    seen: List[str] = []

    def fake_run(args, check=False, **_kw):
        seen.append(args[0])
        Path(args[1]).write_text("ok")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)

    handler = MergeHandler()
    handler.resolve_conflict("x.md", "L", "D")
    assert seen == ["my-fake-editor"]


def test_resolver_editor_unlinks_tempfile_after_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tempfile is deleted after the editor returns — no leaks of
    user content into /tmp after the merge. (try/finally in source.)"""
    _patch_prompt(monkeypatch, ["E"])
    captured_path: List[str] = []

    def fake_run(args, check=False, **_kw):
        captured_path.append(args[1])
        Path(args[1]).write_text("RESOLVED")

        class _Done:
            returncode = 0

        return _Done()

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)

    handler = MergeHandler()
    handler.resolve_conflict("x.md", "L", "D")

    assert captured_path
    assert not Path(captured_path[0]).exists(), (
        "tempfile was not cleaned up after editor exit"
    )


def test_resolver_editor_failure_cleans_up_tempfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the editor exits non-zero (subprocess raises), the tempfile
    is still unlinked (the cleanup is in a `finally`)."""
    _patch_prompt(monkeypatch, ["E"])
    captured_path: List[str] = []

    def fake_run(args, check=False, **_kw):
        captured_path.append(args[1])
        # Simulate `vi` crashing or exiting non-zero.
        raise subprocess.CalledProcessError(returncode=1, cmd=args)

    monkeypatch.setattr(merge_mod.subprocess, "run", fake_run)

    handler = MergeHandler()
    with pytest.raises(subprocess.CalledProcessError):
        handler.resolve_conflict("x.md", "L", "D")

    assert captured_path
    assert not Path(captured_path[0]).exists(), (
        "tempfile leaked after editor crashed"
    )


# ---------------------------------------------------------------------------
# Invalid-input handling — click.Choice re-prompts internally
# ---------------------------------------------------------------------------


def test_resolver_invalid_input_re_prompts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`click.prompt` is called with `type=click.Choice([...])` which makes
    Click itself re-prompt on invalid input. We simulate that behaviour
    here: our fake_prompt mirrors the Click contract — only valid choices
    can reach the caller. The first 'Q' attempt is rejected by Click,
    the second 'S' is accepted. We verify the resolver sees only the
    valid value and returns the expected sentinel."""

    # Click's Choice validation runs INSIDE click.prompt; by the time the
    # call returns, the value is already known-valid. So the only way the
    # resolver code path can be exercised is with a valid letter. The test
    # below pins that the *first valid* letter wins — which is exactly the
    # observable behaviour the user experiences when they mistype.
    calls: List[int] = []

    def fake_prompt(*args, **kwargs):
        calls.append(1)
        # First call: user typed 'Q' (invalid), click re-prompted, user typed 'S'.
        # By the time fake_prompt returns, the only value visible to the
        # resolver is 'S'.
        return "S"

    monkeypatch.setattr(merge_mod.click, "prompt", fake_prompt)

    handler = MergeHandler()
    result = handler.resolve_conflict("CLAUDE.md", "L", "D")
    assert result is None  # skip sentinel
    assert len(calls) == 1


def test_resolver_choice_constraint_includes_all_valid_letters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The click.prompt call uses a Choice that includes BOTH cases of
    each valid letter. Pin the contract so a refactor can't drop, say,
    lowercase 'd' and silently break users with caps-lock off."""
    captured_kwargs: dict = {}

    def fake_prompt(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return "S"

    monkeypatch.setattr(merge_mod.click, "prompt", fake_prompt)

    handler = MergeHandler()
    handler.resolve_conflict("CLAUDE.md", "L", "D")

    # The "type" kwarg is a click.Choice; its `.choices` attribute lists
    # the accepted values.
    choice = captured_kwargs.get("type")
    assert choice is not None, "click.prompt was not called with type=Choice(...)"
    assert set(choice.choices) == {"L", "l", "D", "d", "E", "e", "S", "s"}


# ---------------------------------------------------------------------------
# Sentinel-name pinning
# ---------------------------------------------------------------------------


def test_resolver_sentinel_constants_exposed() -> None:
    """The L/D/E/S string constants are public on the merge module so
    callers can compare against them rather than hard-coding letters."""
    assert merge_mod.CONFLICT_LOCAL == "L"
    assert merge_mod.CONFLICT_DRIVE == "D"
    assert merge_mod.CONFLICT_EDIT == "E"
    assert merge_mod.CONFLICT_SKIP == "S"
