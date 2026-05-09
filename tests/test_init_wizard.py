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


class _StopAfterAuth(Exception):
    """Sentinel — raised once the wizard reaches a known
    authentication-stage prompt for the FTP wizard branch."""
    pass


def test_run_wizard_ftp_walks_through_prompts(monkeypatch, tmp_path):
    """BACKEND-FTP regression: choosing the ftp backend at the first
    prompt must lead the wizard through ftp-specific prompts (host,
    TLS mode, port, username, password). This test scripts answers and
    halts at the password (auth) stage."""
    answers: list[object] = [
        "ftp",                # Storage backend
        str(tmp_path),        # Project directory
        "ftp.example.com",    # FTP host
        "explicit",           # TLS mode
        21,                   # FTP port
        "alice",              # FTP username
    ]
    seen_prompts: list[str] = []

    def fake_prompt(label, default=None, **kwargs):
        seen_prompts.append(str(label))
        if "password" in str(label).lower():
            raise _StopAfterAuth()
        if not answers:
            raise _StopAfterAuth()
        return answers.pop(0)

    def fake_confirm(label, default=True):
        return True

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    monkeypatch.setattr(cli_module.click, "confirm", fake_confirm)

    with pytest.raises(_StopAfterAuth):
        cli_module._run_wizard(backend_default="ftp")

    joined = "\n".join(seen_prompts)
    assert "FTP host" in joined
    assert "TLS mode" in joined
    assert "FTP port" in joined
    assert "FTP username" in joined
    assert "FTP password" in joined


class _ReachedSummary(Exception):
    """Sentinel — raised once the wizard has consumed all the s3-specific
    answers and would otherwise prompt for token-file / config-file /
    patterns / the final save-config confirmation."""
    pass


def test_run_wizard_s3_walks_through_prompts(monkeypatch):
    """Regression: the s3 branch of `_run_wizard` must reach the auth /
    confirmation step when fed bucket + region + access key + secret +
    prefix + path-style answers."""
    from pathlib import Path as _Path

    answers: list[object] = [
        "s3",                                  # Storage backend
        str(_Path.cwd()),                      # Project directory
        "",                                    # S3 endpoint URL (AWS default)
        "mybucket",                            # S3 bucket
        "us-east-1",                           # S3 region
        "AKIA-FAKE",                           # Access key ID
        "secret-fake",                         # Secret access key
        "myproject",                           # Prefix
        "30",                                  # Poll interval
    ]

    def fake_prompt(label, default=None, **kwargs):
        if not answers:
            raise _ReachedSummary()
        return answers.pop(0)

    def fake_confirm(label, default=False):
        if "path-style" in str(label).lower():
            return False
        if "save this configuration" in str(label).lower():
            raise _ReachedSummary()
        return default

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    monkeypatch.setattr(cli_module.click, "confirm", fake_confirm)

    with pytest.raises(_ReachedSummary):
        cli_module._run_wizard(backend_default="s3")
def test_run_wizard_smb_walks_through_prompts(monkeypatch):
    """Reach the SMB-specific auth-step prompts.

    Confirms `_run_wizard(backend_default="smb")` lands in the SMB block
    and exposes the `SMB server` / `SMB share` / `SMB username` prompts
    in that order. We capture the LABELS of every click.prompt call up
    to the first password prompt and assert the SMB-specific ones appear.
    """
    seen_labels: list[str] = []

    answers = {
        "Storage backend": "smb",
        "Project directory": ".",
        "SMB server": "nas.local",
        "SMB port": 445,
        "SMB share": "claude-mirror",
        "SMB username": "alice",
    }

    class _StopHere(Exception):
        pass

    def fake_prompt(label, default=None, **kwargs):
        text = str(label)
        seen_labels.append(text)
        for key, value in answers.items():
            if key in text:
                return value
        # Anything else: stop the wizard so the test stays focused.
        raise _StopHere()

    def fake_getpass(*args, **kwargs):
        # The SMB block calls getpass for the password.
        seen_labels.append("SMB password (getpass)")
        raise _StopHere()

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    import getpass as _getpass
    monkeypatch.setattr(_getpass, "getpass", fake_getpass)

    with pytest.raises(_StopHere):
        cli_module._run_wizard(backend_default="smb")

    # The SMB block was reached and asked for server / port / share /
    # username before getpass.
    assert any("SMB server" in s for s in seen_labels), seen_labels
    assert any("SMB share" in s for s in seen_labels), seen_labels
    assert any("SMB username" in s for s in seen_labels), seen_labels
