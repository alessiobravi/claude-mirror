"""Tests for `claude-mirror init --wizard` argument-handling behaviour.

Coverage today:
    * Regression: when `--backend X` is passed alongside `--wizard`, the
      wizard's first prompt MUST default to X, not the unconditional
      'googledrive' default that shipped pre-fix.

The wizard collects many values from the user; testing the full prompt
sequence is brittle. These tests target the contract that's load-bearing
on each fix and leave the rest of the prompt sequence to manual QA.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from claude_mirror import cli as cli_module

# Click 8.3 emits a Context.protected_args DeprecationWarning from CliRunner.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_run_wizard_uses_backend_default_for_first_prompt(monkeypatch):
    """Regression: pre-fix the first prompt was hardcoded to default
    'googledrive'. With the fix, `_run_wizard(backend_default="sftp")`
    must pass `default="sftp"` into click.prompt for the backend question
    so the user sees `Storage backend [sftp]:` and can hit Enter.
    """
    captured: dict[str, object] = {}

    def fake_prompt(label, default=None, **kwargs):
        # First call is the Storage-backend prompt; capture and bail.
        if "Storage backend" in str(label):
            captured["default"] = default
            # Raise a sentinel so we don't have to mock the rest of the
            # wizard's many prompts.
            raise _StopAfterBackend()
        return default

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)

    with pytest.raises(_StopAfterBackend):
        cli_module._run_wizard(backend_default="sftp")

    assert captured["default"] == "sftp", (
        "wizard ignored backend_default — pre-fix behaviour: it would have "
        "passed 'googledrive' regardless of --backend on the CLI"
    )


def test_run_wizard_default_value_is_googledrive_when_omitted(monkeypatch):
    """When _run_wizard() is called with no argument (e.g. internal call
    where the caller hasn't read --backend), the historical default
    'googledrive' is preserved so existing call-sites don't break."""
    captured: dict[str, object] = {}

    def fake_prompt(label, default=None, **kwargs):
        if "Storage backend" in str(label):
            captured["default"] = default
            raise _StopAfterBackend()
        return default

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)

    with pytest.raises(_StopAfterBackend):
        cli_module._run_wizard()

    assert captured["default"] == "googledrive"


class _StopAfterBackend(Exception):
    """Sentinel — raised by the patched click.prompt to abort the wizard
    after capturing the first-prompt default. Lets the test stay focused
    on the backend prompt without having to script answers to every
    subsequent question."""
    pass
