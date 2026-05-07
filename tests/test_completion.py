"""Tests for `claude-mirror completion <shell>` — the tab-completion bootstrap.

Click 8 ships native shell completion: setting `_CLAUDE_MIRROR_COMPLETE=<shell>_source`
in the env makes the binary print a completion script when invoked. This is opaque
enough that nobody discovers it. The `completion` command exposes the same
script under a discoverable name:

    eval "$(claude-mirror completion zsh)"

These tests verify the command emits non-empty, shell-specific scripts for each
of the three shells we support, rejects unsupported shells, and that the script
contains the prog-name + complete-var so it'll function correctly when sourced.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli

# Click 8.3 emits a DeprecationWarning for `Context.protected_args` from inside
# CliRunner.invoke; pyproject's filterwarnings = "error" turns that into a test
# failure. Suppress for this module — the warning is unactionable from our side
# (it's internal to Click) and will go away when Click 9 ships.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_completion_zsh_emits_zsh_compsys_script():
    """zsh completion script registers a compdef for claude-mirror."""
    result = CliRunner().invoke(cli, ["completion", "zsh"])
    assert result.exit_code == 0
    out = result.output
    assert out.strip(), "expected non-empty script"
    # zsh completion scripts use compdef; verify the prog name is wired in
    assert "claude-mirror" in out
    assert "_CLAUDE_MIRROR_COMPLETE" in out


def test_completion_bash_emits_bash_complete_script():
    """bash completion script wires up the complete builtin for claude-mirror."""
    result = CliRunner().invoke(cli, ["completion", "bash"])
    assert result.exit_code == 0
    out = result.output
    assert out.strip()
    assert "claude-mirror" in out
    assert "_CLAUDE_MIRROR_COMPLETE" in out
    # bash complete uses `complete` builtin
    assert "complete" in out.lower()


def test_completion_fish_emits_fish_completer():
    """fish completion script defines completion functions for claude-mirror."""
    result = CliRunner().invoke(cli, ["completion", "fish"])
    assert result.exit_code == 0
    out = result.output
    assert out.strip()
    assert "claude-mirror" in out
    assert "_CLAUDE_MIRROR_COMPLETE" in out
    # fish uses its own `complete -c <prog>` syntax
    assert "complete" in out.lower()


def test_completion_uppercase_shell_argument_normalised():
    """SHELL choice is case-insensitive — `ZSH` should work like `zsh`."""
    result = CliRunner().invoke(cli, ["completion", "ZSH"])
    assert result.exit_code == 0
    assert "_CLAUDE_MIRROR_COMPLETE" in result.output


def test_completion_invalid_shell_rejected():
    """Unsupported shells (powershell, sh, csh, etc.) error cleanly with the choice list."""
    result = CliRunner().invoke(cli, ["completion", "powershell"])
    assert result.exit_code != 0
    # Click renders the available choices in the error message
    assert "bash" in result.output
    assert "zsh" in result.output
    assert "fish" in result.output


def test_completion_help_lists_shells():
    """`claude-mirror completion --help` includes example invocations for each shell."""
    result = CliRunner().invoke(cli, ["completion", "--help"])
    assert result.exit_code == 0
    out = result.output
    # The docstring documents zsh / bash / fish setup
    assert "zsh" in out
    assert "bash" in out
    assert "fish" in out
    # And the obvious eval form
    assert "eval" in out


def test_completion_command_appears_in_top_level_help():
    """The completion command is registered on the cli group and discoverable
    via `claude-mirror --help` (i.e. it's not hidden)."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "completion" in result.output
