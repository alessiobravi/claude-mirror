"""Meta-tests that re-run `mypy --strict claude_mirror/` from inside the
test suite.

The point: keep the strict-type-check gate honest at every commit, not
just inside CI. If a new file slips in with `def fn(x):` (no annotation)
and the developer pushes without running mypy locally, this test catches
the regression at the same step where pytest already runs.

Skipped when mypy is not installed — local dev environments may lack it
even though CI's mypy job will exercise the same gate.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy not installed")
def test_mypy_strict_passes_on_claude_mirror() -> None:
    """`mypy --strict claude_mirror/` reports zero issues.

    This is the same command CI runs in the dedicated `mypy` job; running
    it here too means a regression surfaces at `pytest` time on the
    contributor's machine before the CI feedback loop kicks in.
    """
    result = subprocess.run(
        ["mypy", "--strict", "claude_mirror/"],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, (
        "mypy --strict failed:\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy not installed")
def test_mypy_executable_runs() -> None:
    """`mypy --version` returns 0 — the binary is wired up correctly.

    Distinct from the strict check: this isolates the "is mypy itself
    installed and runnable in this Python environment" failure mode
    from the "claude_mirror has type errors" failure mode, so when CI
    breaks the cause is unambiguous.
    """
    result = subprocess.run(
        ["mypy", "--version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"mypy --version failed (returncode={result.returncode}):\n"
        f"{result.stdout}{result.stderr}"
    )
    # The output starts with `mypy` (followed by version + build metadata).
    assert result.stdout.lower().startswith("mypy"), (
        f"unexpected mypy --version output: {result.stdout!r}"
    )


@pytest.mark.skipif(shutil.which("mypy") is None, reason="mypy not installed")
def test_pyproject_has_strict_mypy_section() -> None:
    """`pyproject.toml` declares `[tool.mypy] strict = true`.

    Guards against an accidental config relaxation: if someone bumps a
    flag down (e.g. flips `disallow_untyped_defs` to false to silence
    mypy locally), this test fails before it lands in main.
    """
    text = (_REPO_ROOT / "pyproject.toml").read_text()
    assert "[tool.mypy]" in text, "pyproject.toml is missing [tool.mypy]"
    assert "strict = true" in text, (
        "pyproject.toml [tool.mypy] no longer declares `strict = true`"
    )
    assert "disallow_untyped_defs = true" in text, (
        "pyproject.toml [tool.mypy] no longer declares "
        "`disallow_untyped_defs = true`"
    )
