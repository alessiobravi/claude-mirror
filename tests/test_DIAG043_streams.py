"""v0.5.43 diagnostic test only — REMOVE in v0.5.44.

Runs `claude-mirror status --json` and FAILS unconditionally, dumping
result.exit_code / result.stdout / result.stderr / result.output to
the assertion message so CI logs it. Tells us, for the actually-broken
Linux environment, what the captured streams look like.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror.cli import cli

# Click 8.3 emits a DeprecationWarning for Context.protected_args from
# inside CliRunner.invoke; pyproject's filterwarnings = "error" otherwise
# turns that into a test failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_DIAG043_dump_status_json_streams(tmp_path, monkeypatch):
    """Dump everything CliRunner captured. Always fails so CI prints
    the assertion message which contains the captured streams."""
    # Build a minimal config so 'status --json' has something to load.
    cfg_path = tmp_path / "diag.yaml"
    cfg_path.write_text(
        f"backend: googledrive\n"
        f"project_path: {tmp_path}\n"
        f"file_patterns: ['**/*.md']\n"
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--json", "--config", str(cfg_path)])

    stderr_attr = getattr(result, "stderr", None)
    stderr_repr = repr(stderr_attr) if stderr_attr is not None else "<no .stderr attr>"
    stdout_repr = repr(result.stdout) if hasattr(result, "stdout") else "<no .stdout attr>"

    msg = (
        "\n=== v0.5.43 DIAGNOSTIC DUMP ===\n"
        f"exit_code={result.exit_code}\n"
        f"len(stdout)={len(result.stdout) if hasattr(result, 'stdout') else 'N/A'}\n"
        f"stdout={stdout_repr}\n"
        f"len(stderr)={len(stderr_attr) if isinstance(stderr_attr, str) else 'N/A'}\n"
        f"stderr={stderr_repr}\n"
        f"len(output)={len(result.output)}\n"
        f"output={result.output!r}\n"
        f"exception={result.exception!r}\n"
        "=== END DIAG ===\n"
    )
    raise AssertionError(msg)
