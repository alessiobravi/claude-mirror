"""Tests for DYN-COMP — dynamic `--backend` shell tab-completion (v0.5.50).

The hidden `_list-backends` subcommand is the source-of-truth callback that
shell completion scripts invoke at tab-press time to enumerate the live set
of valid `--backend` values. Together with the module-level
`_AVAILABLE_BACKENDS` constant (consumed by both the storage-factory
dispatch and the shell-completion shim), it removes the need to re-source
completion scripts when a new backend is added.

These tests verify:

  * `_list-backends` prints exactly the five expected backends, one per
    line, exit 0.
  * The output stays in lock-step with the `_AVAILABLE_BACKENDS` constant
    (no drift between dispatch and completion).
  * The hidden subcommand never appears in `claude-mirror --help`.
  * Each emitted shell-completion script (zsh / bash / fish / powershell)
    references `_list-backends` so users can grep for the dynamic-callback
    pattern and rc-level overrides have a stable hook.
  * No emitted script contains a hardcoded
    `googledrive dropbox onedrive webdav sftp` substring — regression
    check that the static fallback is gone.
  * `_list-backends` rejects unexpected positional arguments cleanly.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli, _AVAILABLE_BACKENDS

# Click 8.3 emits a DeprecationWarning for `Context.protected_args` from
# inside CliRunner.invoke; pyproject's filterwarnings = "error" turns that
# into a test failure. Suppress for this module — same as the other
# completion test modules.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── _list-backends output ────────────────────────────────────────────────────

def test_list_backends_prints_expected_names_one_per_line():
    """The hidden command emits exactly the supported backends, each on
    its own line, exit 0 — the contract every shell shim depends on.
    Order matches the `_AVAILABLE_BACKENDS` tuple (append-only, so older
    shells that hard-coded a length keep working until they choose to
    update)."""
    result = CliRunner().invoke(cli, ["_list-backends"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert lines == [
        "googledrive", "dropbox", "onedrive", "webdav", "sftp", "ftp", "s3", "smb",
    ]


def test_list_backends_output_matches_available_backends_constant():
    """Source-of-truth check: the printed list MUST equal the module-level
    `_AVAILABLE_BACKENDS` tuple. If a future commit adds a backend to the
    dispatch but forgets to extend the constant (or vice versa) this test
    fires immediately."""
    result = CliRunner().invoke(cli, ["_list-backends"])
    assert result.exit_code == 0
    lines = tuple(
        line for line in result.output.splitlines() if line.strip()
    )
    assert lines == _AVAILABLE_BACKENDS


def test_list_backends_hidden_from_top_level_help():
    """The hidden subcommand exists for shell completion only — it must
    NOT appear in `claude-mirror --help`. Verifies Click's `hidden=True`
    decoration is in place."""
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "_list-backends" not in result.output


def test_list_backends_rejects_extra_positional_args():
    """The command takes no positional arguments. Passing one must error
    rather than silently ignore — keeps the contract tight for future
    refactors."""
    result = CliRunner().invoke(cli, ["_list-backends", "extra-arg"])
    assert result.exit_code != 0


# ── per-shell emitted-script content ─────────────────────────────────────────

@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_completion_script_contains_list_backends_invocation(shell):
    """Each emitted shell-completion script must mention the hidden
    `_list-backends` subcommand — the dynamic-invocation pattern that
    powers `--backend <TAB>` enumeration without re-sourcing."""
    result = CliRunner().invoke(cli, ["completion", shell])
    assert result.exit_code == 0, result.output
    assert "_list-backends" in result.output, (
        f"{shell} completion script missing dynamic _list-backends shim"
    )


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell"])
def test_completion_script_has_no_static_backend_list(shell):
    """Regression: no emitted script may contain the hardcoded
    `googledrive dropbox onedrive webdav sftp` literal — that's the
    pre-DYN-COMP static fallback we explicitly removed. If this fires
    after a Click upgrade, the upgrade is silently re-introducing the
    static list and DYN-COMP needs to be re-validated against the new
    Click version."""
    result = CliRunner().invoke(cli, ["completion", shell])
    assert result.exit_code == 0
    static_literal = "googledrive dropbox onedrive webdav sftp"
    assert static_literal not in result.output, (
        f"{shell} completion script contains a hardcoded backend list"
    )
