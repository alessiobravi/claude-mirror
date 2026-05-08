"""Regression test for the v0.5.44 fix.

When --json is in argv, the pre-subcommand banners (watcher-not-running
warning, update-check notice) that `_CLIGroup.invoke()` would normally
print MUST NOT leak into stdout. Otherwise stdout starts with ANSI
escape codes and `json.loads(result.stdout)` fails with the literal
same error string `JSONDecodeError: Expecting value: line 1 column 1
(char 0)` that an empty stdout produces — which made the v0.5.39
failure mode look like "stdout is empty" when it actually was "stdout
starts with ANSI gunk". See CHANGELOG [0.5.44].

Originally a self-failing diagnostic (`test_DIAG043_dump_status_json_streams`);
v0.5.45 promoted it to a regression that passes only when the fix is
in place.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli

# Click 8.3 emits a DeprecationWarning for Context.protected_args from
# inside CliRunner.invoke; pyproject's filterwarnings = "error" otherwise
# turns that into a test failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_json_mode_does_not_leak_pre_subcommand_banners(tmp_path):
    """`status --json` against an unauthed config: stdout MUST be empty
    (no watcher-banner / no update-check notice). The JSON error envelope
    goes to stderr via _emit_json_error. exit_code 1.

    Pre-fix (v0.5.39 → v0.5.43): stdout was 271 bytes of ANSI watcher
    banner because _CLIGroup.invoke() called _check_watcher_running()
    before the subcommand could check the --json flag. Post-fix
    (v0.5.44+): _CLIGroup.invoke() skips both _check_watcher_running()
    and the update-check fetch when --json is in argv, so stdout is
    reserved exclusively for the JSON document.
    """
    cfg_path = tmp_path / "noauth.yaml"
    cfg_path.write_text(
        f"backend: googledrive\n"
        f"project_path: {tmp_path}\n"
        f"file_patterns: ['**/*.md']\n"
    )
    result = CliRunner().invoke(cli, ["status", "--json", "--config", str(cfg_path)])

    assert result.exit_code == 1, (
        f"Expected exit_code=1 (auth error path), got {result.exit_code}.\n"
        f"stdout={result.stdout!r}\n"
        f"stderr={getattr(result, 'stderr', None)!r}"
    )

    # Critical: NO banner pollution in stdout.
    assert result.stdout == "", (
        "stdout must be empty when --json is used in error path "
        "(no watcher banner, no update-check notice). "
        f"Found {len(result.stdout)}-byte leak: {result.stdout!r}"
    )

    # Sanity check: the JSON error envelope should be in stderr.
    stderr = getattr(result, "stderr", None) or result.output
    assert '"command": "status"' in stderr, (
        f"Expected JSON error envelope on stderr, got: {stderr!r}"
    )
    assert '"error":' in stderr
