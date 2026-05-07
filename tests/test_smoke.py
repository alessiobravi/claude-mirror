"""Smoke tests — verify the test harness itself works, the package
imports cleanly, and the most fundamental invariants hold.

If any of these fail, the test infrastructure is broken before the
specific-feature tests even start running."""
from __future__ import annotations

import claude_mirror


def test_package_imports():
    """The top-level package imports without side-effects."""
    assert hasattr(claude_mirror, "__file__")


def test_version_is_set():
    """The package advertises a version via importlib.metadata."""
    from importlib.metadata import version
    v = version("claude-mirror")
    assert v
    # Validate it parses as a PEP 440 version (rough check)
    parts = v.split(".")
    assert len(parts) >= 2
    assert all(p.lstrip("0").isdigit() or p == "0" for p in parts[:3] if p)


def test_redact_error_strips_home_path():
    """The error redactor rewrites the running user's home dir to $HOME,
    so leaked paths in persisted manifests / Slack messages don't carry
    a real username."""
    import os
    from claude_mirror.backends import redact_error
    home = os.path.expanduser("~")
    msg = f"open failed: {home}/private/file.txt: permission denied"
    out = redact_error(msg)
    assert home not in out
    assert "$HOME" in out
    assert "permission denied" in out


def test_redact_error_strips_bearer_token():
    """The error redactor strips Bearer tokens.

    The fixture below uses a deliberately-non-real string so automated
    secret scanners (PyPI / GitHub Advanced Security / TruffleHog) don't
    mis-flag this test file as containing a leaked credential. The
    redactor's regex only cares about the `Bearer ` prefix + any
    [A-Za-z0-9._-]+ suffix, so any non-empty alphanumeric tail exercises
    the same code path that a real ya29.* token would.
    """
    from claude_mirror.backends import redact_error
    fake_token = "FAKE_TOKEN_NOT_A_REAL_CREDENTIAL_xxxxxxxxxxxxxxxxxxx"
    msg = f"401 Unauthorized: Bearer {fake_token}"
    out = redact_error(msg)
    assert fake_token not in out
    assert "redacted" in out.lower()


def test_make_config_fixture(make_config):
    """The make_config fixture produces a usable Config."""
    cfg = make_config()
    assert cfg.project_path
    assert cfg.backend == "googledrive"


def test_fake_backend_fixture(fake_backend, project_dir, write_files):
    """The fake_backend fixture supports the StorageBackend shape."""
    write_files({"a.md": "hello"})
    folder_id = fake_backend.get_or_create_folder("subfolder", "root")
    file_id = fake_backend.upload_file(
        str(project_dir / "a.md"), "a.md", folder_id
    )
    assert file_id
    upload_calls = [c for c in fake_backend.calls if c[0] == "upload_file"]
    assert len(upload_calls) == 1
    # Round-trip verification: download returns the same bytes we uploaded
    assert fake_backend.download_file(file_id) == b"hello"


def test_fake_notifier_fixture(fake_notifier):
    """The fake_notifier fixture supports the NotificationBackend shape
    and lets tests inject events into registered watch callbacks."""
    from claude_mirror.events import SyncEvent
    received = []
    import threading
    stop = threading.Event()
    stop.set()  # release immediately
    fake_notifier.watch(received.append, stop)
    fake_notifier.deliver(SyncEvent(
        timestamp="2026-05-07T10:00:00",
        user="u",
        machine="m",
        action="push",
        files=["a.md"],
        project="testproject",
    ))
    assert len(received) == 1
    assert received[0].files == ["a.md"]
