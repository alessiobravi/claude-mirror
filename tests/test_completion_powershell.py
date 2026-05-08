"""Tests for `claude-mirror completion powershell` (v0.5.39).

Click 8.3 ships native completion adapters for bash / zsh / fish only;
the PowerShell adapter is implemented locally in `claude_mirror.cli`.
These tests verify:

  * `completion powershell` emits PowerShell-syntax script
    (Register-ArgumentCompleter cmdlet present)
  * The same env-var protocol the other shells use is honoured
    (`_CLAUDE_MIRROR_COMPLETE` referenced in the script)
  * `_detect_shell()` recognises pwsh / powershell as a shell name
  * `_detect_shell()` defaults to powershell on Windows when SHELL is unset
  * `_completion_target()` returns a sensible profile-script path on
    each platform (pwsh's `$PROFILE.CurrentUserAllHosts`)
  * `install_completion()` writes the marker block to the profile
    when the user runs `claude-mirror-install` from a pwsh session
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli
from claude_mirror.install import (
    _COMPLETION_MARK_BEGIN,
    _COMPLETION_MARK_END,
    _completion_target,
    _detect_shell,
    install_completion,
    uninstall_completion,
)

# Click 8.3 deprecation noise — same suppression as other test modules.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── completion powershell command output ──────────────────────────────────────

def test_completion_powershell_emits_register_argumentcompleter():
    """The PowerShell completion script must use the canonical
    `Register-ArgumentCompleter` cmdlet — that's the native PowerShell
    mechanism for hooking into tab-completion."""
    result = CliRunner().invoke(cli, ["completion", "powershell"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.strip(), "expected non-empty script"
    assert "Register-ArgumentCompleter" in out
    assert "claude-mirror" in out
    # Script must reference our COMPLETE env var so Click can dispatch
    # back into the binary on each tab-press.
    assert "_CLAUDE_MIRROR_COMPLETE" in out


def test_completion_powershell_emits_native_completer():
    """The script registers a -Native completer (the form for external
    binaries — claude-mirror is an external command from PowerShell's
    perspective)."""
    result = CliRunner().invoke(cli, ["completion", "powershell"])
    assert result.exit_code == 0
    assert "-Native" in result.output


def test_completion_powershell_uppercase_normalised():
    """Click choice is case-insensitive — `POWERSHELL` should work."""
    result = CliRunner().invoke(cli, ["completion", "POWERSHELL"])
    assert result.exit_code == 0
    assert "Register-ArgumentCompleter" in result.output


def test_completion_help_documents_powershell():
    """`claude-mirror completion --help` must include the PowerShell setup
    snippet so Windows / cross-platform users discover it."""
    result = CliRunner().invoke(cli, ["completion", "--help"])
    assert result.exit_code == 0
    assert "powershell" in result.output.lower()
    # The recommended invocation pipes into Out-File on $PROFILE.
    assert "$PROFILE" in result.output or "Out-File" in result.output


# ── _detect_shell on Unix-likes ───────────────────────────────────────────────

def test_detect_shell_pwsh_recognised(monkeypatch):
    """`SHELL=/usr/local/bin/pwsh` should resolve to 'powershell'."""
    monkeypatch.setenv("SHELL", "/usr/local/bin/pwsh")
    assert _detect_shell() == "powershell"


def test_detect_shell_powershell_basename_recognised(monkeypatch):
    """A `SHELL` ending in 'powershell' (Windows PowerShell 5.1) also
    resolves to 'powershell'."""
    monkeypatch.setenv("SHELL", "/c/Windows/System32/powershell")
    assert _detect_shell() == "powershell"


def test_detect_shell_pwsh_exe_basename_recognised(monkeypatch):
    """Some hybrid setups (Git Bash, WSL) put the .exe-suffixed binary
    on $SHELL — strip the suffix before matching."""
    monkeypatch.setenv("SHELL", "C:/Program Files/PowerShell/7/pwsh.exe")
    assert _detect_shell() == "powershell"


def test_detect_shell_unix_default_unchanged_when_pwsh_not_in_shell(monkeypatch):
    """Regression: a Unix user with $SHELL=/bin/zsh still gets zsh —
    PowerShell is lower priority than the native Unix shells."""
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _detect_shell() == "zsh"


# ── _detect_shell on Windows ──────────────────────────────────────────────────

def test_detect_shell_windows_default_to_powershell(monkeypatch):
    """When SHELL is unset on Windows, default to powershell — that's
    the canonical interactive shell for almost every Windows user."""
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Windows")
    assert _detect_shell() == "powershell"


# ── _completion_target ────────────────────────────────────────────────────────

def test_completion_target_powershell_unix_uses_xdg_path(monkeypatch):
    """On macOS/Linux the pwsh profile lives at ~/.config/powershell/profile.ps1."""
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Darwin")
    target = _completion_target("powershell")
    assert target is not None
    assert "powershell" in str(target).lower()
    assert target.name == "profile.ps1"


def test_completion_target_powershell_windows_uses_documents(monkeypatch):
    """On Windows the pwsh profile lives under Documents/PowerShell/."""
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Windows")
    target = _completion_target("powershell")
    assert target is not None
    parts = [p.lower() for p in target.parts]
    assert "documents" in parts or "powershell" in parts
    assert target.name == "profile.ps1"


# ── install_completion writes the marker block ────────────────────────────────

def test_install_completion_powershell_writes_block_to_profile(tmp_path, monkeypatch):
    """First-time pwsh install: marker block lands in the profile path
    with the PowerShell-style invoke-expression line."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Darwin")
    monkeypatch.setenv("SHELL", "/usr/local/bin/pwsh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    install_completion()

    target = home / ".config" / "powershell" / "profile.ps1"
    assert target.exists(), "PowerShell profile must be created on first install"
    content = target.read_text()
    assert _COMPLETION_MARK_BEGIN in content
    assert _COMPLETION_MARK_END in content
    # The PowerShell invocation pipes the live source through Invoke-Expression.
    assert "Invoke-Expression" in content
    assert "claude-mirror" in content


def test_install_completion_powershell_idempotent_when_current(tmp_path, monkeypatch):
    """Re-running install with the same binary path is a no-op for
    PowerShell, same as zsh/bash."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Darwin")
    monkeypatch.setenv("SHELL", "/usr/local/bin/pwsh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    install_completion()
    target = home / ".config" / "powershell" / "profile.ps1"
    before = target.read_text()

    install_completion()
    after = target.read_text()
    assert before == after, "second install with same binary path must be a no-op"


def test_install_completion_powershell_sets_activation_pending(tmp_path, monkeypatch):
    """Like the other shells, pwsh install must flip the activation
    flag so the end-of-run banner fires."""
    import claude_mirror.install as install_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Darwin")
    monkeypatch.setenv("SHELL", "/usr/local/bin/pwsh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    install_mod._completion_activation_pending = False
    install_completion()
    assert install_mod._completion_activation_pending is True


def test_uninstall_completion_powershell_removes_block(tmp_path, monkeypatch):
    """Uninstall on pwsh removes the marker block but preserves any
    user content above and below."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr("claude_mirror.install.platform.system", lambda: "Darwin")
    monkeypatch.setenv("SHELL", "/usr/local/bin/pwsh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    target = home / ".config" / "powershell" / "profile.ps1"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# user pwsh customisations above\n"
        "Set-Alias ll Get-ChildItem\n"
        "\n"
        f"{_COMPLETION_MARK_BEGIN}\n"
        "Invoke-Expression (& claude-mirror completion powershell | Out-String)\n"
        f"{_COMPLETION_MARK_END}\n"
        "\n"
        "# user content below\n"
    )

    uninstall_completion()

    after = target.read_text()
    assert _COMPLETION_MARK_BEGIN not in after
    assert _COMPLETION_MARK_END not in after
    assert "Set-Alias ll Get-ChildItem" in after
    assert "user content below" in after
