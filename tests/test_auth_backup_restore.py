"""Regression tests for the v0.5.11 `claude-mirror auth` backup-and-restore
contract.

Background:
    Before v0.5.11, running `claude-mirror auth` against a stale or partially-
    revoked token would short-circuit the OAuth flow because the backend's
    cached credentials looked "fine" until first use. Users ended up with
    no way to fully replace their token without manually deleting the file.

The fix:
    Default behaviour is to MOVE the existing token aside to
    `<token_file>.pre-reauth.bak` BEFORE invoking the OAuth flow, restore
    it on any exception, and unlink the backup on success. `--keep-existing`
    skips this dance for users who explicitly want refresh-then-fallback.

The tests below pin every transition of that contract so a future refactor
of the auth command can't silently regress it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, List

import pytest
import yaml
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

# Click 8.3+ emits internal DeprecationWarnings about its own `protected_args`
# attribute being removed in 9.0. The project's pytest config sets
# `filterwarnings = ["error"]` to catch our own warnings — we don't want
# Click's internal deprecations to fail every CliRunner-based test. Apply
# the suppression only in this module so the strict policy stays in force
# everywhere else.
pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_yaml_config(
    path: Path, project_path: Path, token_file: Path, credentials_file: Path
) -> Path:
    """Write a minimal YAML config that Config.load can parse.
    Returns the path for chaining."""
    data = {
        "project_path": str(project_path),
        "backend": "googledrive",
        "drive_folder_id": "test-folder-id",
        "credentials_file": str(credentials_file),
        "token_file": str(token_file),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    path.write_text(yaml.safe_dump(data))
    return path


class _FakeStorage:
    """Stand-in for the real GoogleDriveBackend during auth-flow tests.

    `behavior` controls what `.authenticate()` does:
        "ok"          → returns self, marks the token file written
        "raise"       → raises RuntimeError
        "ki"          → raises KeyboardInterrupt (user hit Ctrl-C mid-flow)
    """

    backend_name = "fake"

    def __init__(self, token_file: Path, behavior: str = "ok") -> None:
        self._token_file = token_file
        self._behavior = behavior
        # Snapshot whether the backup file existed at the moment .authenticate()
        # was called — the test asserts on this to verify the move-aside
        # actually happened BEFORE the OAuth flow.
        self.backup_existed_at_authenticate: Any = None

    def authenticate(self) -> Any:
        backup = Path(str(self._token_file) + ".pre-reauth.bak")
        self.backup_existed_at_authenticate = backup.exists()
        if self._behavior == "ok":
            # Simulate writing a fresh token file.
            self._token_file.write_text('{"new":"token"}')
            return self
        if self._behavior == "raise":
            raise RuntimeError("oauth blew up")
        if self._behavior == "ki":
            raise KeyboardInterrupt()
        raise AssertionError(f"unknown behavior: {self._behavior}")


def _patch_backend(monkeypatch: pytest.MonkeyPatch, fake: _FakeStorage) -> None:
    """Replace `_create_storage` and the notifier hook so the auth command
    touches no real cloud APIs."""
    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: fake)
    # The notifier setup runs after a successful auth. Make it a no-op so
    # tests can focus on the backup/restore semantics.
    monkeypatch.setattr(cli_mod, "_create_notifier", lambda config, storage: None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_auth_moves_token_aside_to_pre_reauth_bak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing token at config.token_file is moved to
    `<token>.pre-reauth.bak` BEFORE OAuth runs."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text('{"old":"token"}')
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "config.yaml", project, token, creds
    )

    fake = _FakeStorage(token, behavior="ok")
    _patch_backend(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output
    # The crucial assertion: the backup existed at the moment authenticate()
    # was invoked — i.e. the move-aside happened first.
    assert fake.backup_existed_at_authenticate is True


def test_auth_deletes_backup_on_oauth_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a successful OAuth flow, the backup file is gone (cleaned up)
    and the new token is in place."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text('{"old":"token"}')
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "config.yaml", project, token, creds
    )

    fake = _FakeStorage(token, behavior="ok")
    _patch_backend(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output
    backup = Path(str(token) + ".pre-reauth.bak")
    assert not backup.exists(), "backup should be unlinked on success"
    # The fresh token written by the fake is what's on disk.
    assert token.read_text() == '{"new":"token"}'


def test_auth_restores_backup_on_oauth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If OAuth raises, the backup is moved back to `<token>` and is
    intact — the user is never left worse off than before."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    original_content = '{"old":"token","keep":"me"}'
    token.write_text(original_content)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "config.yaml", project, token, creds
    )

    fake = _FakeStorage(token, behavior="raise")
    _patch_backend(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "--config", str(cfg_path)])

    # Auth failed — non-zero exit. The original RuntimeError ("oauth blew
    # up") matches the cli.py top-level handler's auth-keyword filter and
    # is converted to a friendly message + SystemExit(1). Either way the
    # important thing is that the OAuth flow failed and the on-disk
    # token-restore contract held.
    assert result.exit_code != 0
    # Token file is back at its original location with original contents.
    assert token.exists(), "token file must be restored after failure"
    assert token.read_text() == original_content
    # Backup is gone (it was moved BACK, not copied).
    backup = Path(str(token) + ".pre-reauth.bak")
    assert not backup.exists()


def test_auth_keep_existing_does_not_move_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--keep-existing` skips the move-aside step entirely — the original
    token file stays untouched at its location throughout the OAuth
    invocation."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    original_content = '{"keep":"me"}'
    token.write_text(original_content)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "config.yaml", project, token, creds
    )

    # Build a fake whose authenticate() does NOT touch the token file —
    # it only checks that the backup did NOT exist when called.
    class _NoTouchStorage(_FakeStorage):
        def authenticate(self) -> Any:
            backup = Path(str(self._token_file) + ".pre-reauth.bak")
            self.backup_existed_at_authenticate = backup.exists()
            # Don't write anything; emulate refresh-only path.
            return self

    fake = _NoTouchStorage(token, behavior="ok")
    _patch_backend(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["auth", "--keep-existing", "--config", str(cfg_path)]
    )

    assert result.exit_code == 0, result.output
    # No backup was created at any point.
    assert fake.backup_existed_at_authenticate is False
    backup = Path(str(token) + ".pre-reauth.bak")
    assert not backup.exists()
    # Token unchanged on disk.
    assert token.read_text() == original_content


def test_auth_handles_no_existing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If there is no existing token file, the auth command runs OAuth
    without trying to back anything up — it should not crash with
    FileNotFoundError or create an empty backup file."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"  # NOT created
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "config.yaml", project, token, creds
    )

    fake = _FakeStorage(token, behavior="ok")
    _patch_backend(monkeypatch, fake)

    runner = CliRunner()
    result = runner.invoke(cli, ["auth", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output
    # No backup was ever created, even transiently.
    assert fake.backup_existed_at_authenticate is False
    assert not Path(str(token) + ".pre-reauth.bak").exists()
    # Fresh token was written by the fake.
    assert token.read_text() == '{"new":"token"}'


def test_auth_failure_modes_dont_corrupt_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """For a variety of OAuth-failure exception types (RuntimeError,
    KeyboardInterrupt — the latter is caught by the BaseException handler
    in the source), the token file on disk afterward is the OLD content,
    bit-for-bit. No intermediate / partially-written state ever survives.

    Whether Click's top-level handler converts the underlying error to a
    SystemExit (auth-keyword RuntimeError → friendly message + exit 1) or
    re-raises it is a UI concern; what we pin here is the disk-state
    contract: the user's token is never lost or corrupted."""
    project = tmp_path / "project"
    project.mkdir()
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")

    # Each behavior corresponds to a different failure mode the auth flow
    # might encounter. We don't pin which exception type the CliRunner sees
    # at the top level — that's up to cli.py's _CLIGroup error router.
    for behavior in ("raise", "ki"):
        token = tmp_path / f"token_{behavior}.json"
        original = f'{{"flavour":"{behavior}"}}'
        token.write_text(original)
        cfg_path = _write_yaml_config(
            tmp_path / f"config_{behavior}.yaml", project, token, creds
        )

        fake = _FakeStorage(token, behavior=behavior)
        _patch_backend(monkeypatch, fake)

        runner = CliRunner()
        result = runner.invoke(cli, ["auth", "--config", str(cfg_path)])

        # Auth must have failed in some way — either non-zero exit or an
        # exception bubbled into result. The exact shape depends on Click
        # and the error router, neither of which we're testing here.
        assert result.exit_code != 0 or result.exception is not None, (
            f"behavior={behavior}: expected failure"
        )

        # ----- The actual contract -----
        # Token must still exist with the original content — never an
        # intermediate / empty / partially-written state.
        assert token.exists(), f"behavior={behavior}: token missing after failure"
        assert token.read_text() == original, (
            f"behavior={behavior}: token contents corrupted: {token.read_text()!r}"
        )
        # Backup is fully cleaned up — no `.pre-reauth.bak` litter.
        backup = Path(str(token) + ".pre-reauth.bak")
        assert not backup.exists()
