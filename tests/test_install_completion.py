"""Tests for the shell-tab-completion install + uninstall flow.

The user-facing entry is `claude-mirror-install` — the v0.5.27 release added
auto-install of tab-completion as a step in that flow. Without it, users had
to manually run `eval "$(claude-mirror completion zsh)"` to get tab-completion,
which most users never discovered.

These tests cover:
  - Detection of zsh / bash / fish from $SHELL
  - Idempotent install (re-running install doesn't double-write)
  - Update path (changes the eval line cleanly when re-installing after binary moves)
  - Uninstall removes the marker block but preserves user content above + below
  - Fish writes a separate completion file (no rc-file editing)
  - Unsupported shells (sh, dash) skip cleanly without error
"""
from __future__ import annotations

from pathlib import Path

import pytest

from claude_mirror.install import (
    _COMPLETION_MARK_BEGIN,
    _COMPLETION_MARK_END,
    _completion_target,
    _detect_shell,
    _replace_completion_block,
    install_completion,
    uninstall_completion,
)

# Click 8.3 protected_args deprecation — same pattern as other test modules.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── _detect_shell ──────────────────────────────────────────────────────────────

def test_detect_shell_zsh(monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/zsh")
    assert _detect_shell() == "zsh"


def test_detect_shell_bash(monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/bin/bash")
    assert _detect_shell() == "bash"


def test_detect_shell_fish(monkeypatch):
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    assert _detect_shell() == "fish"


def test_detect_shell_unsupported_returns_none(monkeypatch):
    """sh, dash, csh, tcsh, ksh — better to skip than write something broken."""
    for shell_path in ("/bin/sh", "/bin/dash", "/bin/csh", "/bin/tcsh", "/bin/ksh"):
        monkeypatch.setenv("SHELL", shell_path)
        assert _detect_shell() is None, f"expected None for {shell_path}"


def test_detect_shell_empty_falls_back_to_platform_default(monkeypatch):
    """If SHELL is unset, fall back to platform default (zsh on Mac, bash on Linux)."""
    monkeypatch.delenv("SHELL", raising=False)
    result = _detect_shell()
    assert result in ("zsh", "bash")  # exactly one of these depending on platform


# ── _completion_target ─────────────────────────────────────────────────────────

def test_completion_target_zsh_points_at_zshrc():
    target = _completion_target("zsh")
    assert target is not None
    assert target.name == ".zshrc"
    assert target.is_absolute()


def test_completion_target_fish_points_at_completions_dir():
    target = _completion_target("fish")
    assert target is not None
    assert target.name == "claude-mirror.fish"
    assert "fish/completions" in str(target).replace("\\", "/")


def test_completion_target_unknown_shell_returns_none():
    assert _completion_target("csh") is None
    assert _completion_target("ksh") is None
    assert _completion_target("") is None


# ── _replace_completion_block ──────────────────────────────────────────────────

def _make_block(eval_line: str = 'eval "$(claude-mirror completion zsh)"') -> str:
    return "\n".join([_COMPLETION_MARK_BEGIN, eval_line, _COMPLETION_MARK_END])


def test_replace_completion_block_strips_existing_when_replacement_empty():
    """uninstall path: pass replacement="" to remove the block."""
    rc = (
        "# user content above\n"
        "alias foo=bar\n\n"
        f"{_make_block()}\n"
        "\n# user content below\n"
        "export PATH=$PATH:/foo\n"
    )
    result = _replace_completion_block(rc, replacement="")
    assert _COMPLETION_MARK_BEGIN not in result
    assert _COMPLETION_MARK_END not in result
    assert "alias foo=bar" in result
    assert "export PATH=$PATH:/foo" in result


def test_replace_completion_block_swaps_block_when_replacement_given():
    """update path: pass new block to replace stale block."""
    rc = (
        "# user content\n"
        f"{_make_block(eval_line='eval $(old_path/claude-mirror completion zsh)')}\n"
    )
    new_block = _make_block(eval_line='eval "$(/new/path/claude-mirror completion zsh)"')
    result = _replace_completion_block(rc, replacement=new_block)
    assert "# user content" in result
    assert "/new/path/claude-mirror" in result
    assert "old_path" not in result


def test_replace_completion_block_handles_no_existing_block():
    """no marker → result is just `replacement` appended (or no-op if replacement empty)."""
    rc = "# my zshrc\nalias x=y\n"
    new_block = _make_block()
    result = _replace_completion_block(rc, replacement=new_block)
    assert "alias x=y" in result
    assert _COMPLETION_MARK_BEGIN in result


# ── install_completion ─────────────────────────────────────────────────────────

def test_install_completion_zsh_appends_block_to_zshrc(tmp_path, monkeypatch):
    """First-time install: marker block lands at the end of an existing rc, with
    the user's pre-existing content preserved verbatim above it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")

    rc = home / ".zshrc"
    rc.write_text("# user's existing content\nalias ll='ls -la'\n")

    # Auto-confirm the prompt
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    # Stub `_find_binary` so the eval line is deterministic
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    install_completion()

    after = rc.read_text()
    assert "alias ll='ls -la'" in after, "user content must be preserved"
    assert _COMPLETION_MARK_BEGIN in after
    assert _COMPLETION_MARK_END in after
    assert 'eval "$(claude-mirror completion zsh)"' in after


def test_install_completion_idempotent_when_already_current(tmp_path, monkeypatch):
    """Re-running install with an unchanged binary path is a no-op."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")

    rc = home / ".zshrc"
    rc.write_text(
        "# baseline\n"
        f"{_make_block()}\n"
    )
    before = rc.read_text()

    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    install_completion()
    after = rc.read_text()

    # Idempotent: file content unchanged when the existing eval line still matches.
    assert before == after


def test_install_completion_fish_writes_completion_file(tmp_path, monkeypatch):
    """Fish shell uses a dedicated file in the completions dir, not eval-of-script."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")

    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    # Stub the subprocess.run call that invokes `claude-mirror completion fish`
    import subprocess
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="# fake fish completion script\ncomplete -c claude-mirror -a foo\n",
            stderr="",
        )
    monkeypatch.setattr("claude_mirror.install.subprocess.run", fake_run)

    install_completion()

    target = home / ".config" / "fish" / "completions" / "claude-mirror.fish"
    assert target.exists()
    assert "complete -c claude-mirror" in target.read_text()
    assert captured["cmd"] == ["claude-mirror", "completion", "fish"]


def test_install_completion_unsupported_shell_skips(tmp_path, monkeypatch):
    """sh/dash/etc. — print a warning, do not write anything."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/sh")

    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)

    install_completion()

    # No rc files were created
    assert list(home.iterdir()) == []


# ── uninstall_completion ───────────────────────────────────────────────────────

def test_uninstall_completion_zsh_strips_block_preserves_user_content(tmp_path, monkeypatch):
    """Uninstall removes ONLY the marker block; user content above and below stays."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")

    rc = home / ".zshrc"
    rc.write_text(
        "# user's stuff above\n"
        "alias ll='ls -la'\n"
        "\n"
        f"{_make_block()}\n"
        "\n"
        "# user's stuff below\n"
        "export EDITOR=vim\n"
    )

    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)

    uninstall_completion()

    after = rc.read_text()
    assert _COMPLETION_MARK_BEGIN not in after
    assert _COMPLETION_MARK_END not in after
    assert "alias ll='ls -la'" in after
    assert "export EDITOR=vim" in after


def test_uninstall_completion_fish_removes_completion_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    completions = home / ".config" / "fish" / "completions"
    completions.mkdir(parents=True)
    target = completions / "claude-mirror.fish"
    target.write_text("# fish completion content")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)

    uninstall_completion()

    assert not target.exists()


def test_uninstall_completion_no_existing_block_skips_cleanly(tmp_path, monkeypatch):
    """If the user never installed completion, uninstall is a graceful no-op."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")

    rc = home / ".zshrc"
    rc.write_text("# fresh zshrc\nalias ll='ls -la'\n")

    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)

    # Should not raise; should leave file untouched.
    uninstall_completion()

    assert rc.read_text() == "# fresh zshrc\nalias ll='ls -la'\n"


# ── _completion_activation_pending flag (used by install_cli for the banner) ──

def test_install_completion_sets_activation_pending_on_zsh_install(tmp_path, monkeypatch):
    """First-time zsh install must flip the activation-pending flag so the
    end-of-run banner in install_cli fires."""
    import claude_mirror.install as install_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    install_mod._completion_activation_pending = False
    install_completion()
    assert install_mod._completion_activation_pending is True


def test_install_completion_sets_activation_pending_on_fish_install(tmp_path, monkeypatch):
    """First-time fish install must flip the same flag."""
    import claude_mirror.install as install_mod
    import subprocess

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/fish")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")
    monkeypatch.setattr(
        "claude_mirror.install.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            args=cmd, returncode=0,
            stdout="# fake fish completion\ncomplete -c claude-mirror -a foo\n",
            stderr="",
        ),
    )

    install_mod._completion_activation_pending = False
    install_completion()
    assert install_mod._completion_activation_pending is True


def test_install_completion_does_not_set_activation_pending_on_idempotent_skip(tmp_path, monkeypatch):
    """When the existing eval line already matches (true no-op path), the
    flag must NOT be flipped — the banner is irrelevant when nothing was
    written to the rc file."""
    import claude_mirror.install as install_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)
    monkeypatch.setattr("claude_mirror.install._find_binary", lambda: "claude-mirror")

    rc = home / ".zshrc"
    rc.write_text(
        "# baseline\n"
        f"{_make_block()}\n"
    )

    install_mod._completion_activation_pending = False
    install_completion()
    assert install_mod._completion_activation_pending is False, (
        "no-op idempotent install path must NOT set activation flag"
    )


def test_install_completion_does_not_set_activation_pending_on_unsupported_shell(tmp_path, monkeypatch):
    """Unsupported shells skip without writing anything; flag must stay False."""
    import claude_mirror.install as install_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr("claude_mirror.install._confirm", lambda *a, **kw: True)

    install_mod._completion_activation_pending = False
    install_completion()
    assert install_mod._completion_activation_pending is False
