"""Tests for `claude-mirror doctor` — one-shot configuration diagnosis.

Doctor walks every common configuration check (config parse, credentials
file, token, backend connectivity, project path, manifest integrity) and
prints a concrete fix command for each failure. Exit code 0 if every check
passes, 1 if any fail — so it composes with shell scripts and CI.

These tests use the same CliRunner pattern as `test_completion.py` and
`test_auth_backup_restore.py`. The backend connectivity check is mocked
via monkeypatching `claude_mirror.cli._create_storage` so no real cloud
APIs are touched.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional

import pytest
import yaml
from click.testing import CliRunner
from google.auth.exceptions import RefreshError

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

# Rich's Console wraps long lines at the detected terminal width, which
# splits long absolute paths and substrings like "token file unreadable /
# corrupt" across newlines. For substring assertions to be stable across
# the variety of widths CliRunner can inherit (CI vs local terminal), we
# (a) replace the cli module's console with a very-wide one in a fixture
# and (b) strip ANSI escapes before matching.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from Rich's output so substring
    assertions can match the raw text content."""
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace `cli_mod.console` with a Rich Console set to a very large
    width so absolute tmp_path paths render on a single line. Otherwise
    Rich's auto-detected width splits paths across newlines and breaks
    substring assertions."""
    from rich.console import Console

    monkeypatch.setattr(cli_mod, "console", Console(force_terminal=True, width=400))

# Click 8.3+ emits a DeprecationWarning for `Context.protected_args` from
# inside CliRunner.invoke; pyproject's filterwarnings = "error" turns that
# into a test failure. Suppress only in this module — the warning is
# unactionable from our side and will go away when Click 9 ships.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    credentials_file: Path,
    backend: str = "googledrive",
    drive_folder_id: str = "test-folder-id",
    extra: Optional[dict] = None,
) -> Path:
    """Write a minimal YAML config that Config.load can parse."""
    data: dict = {
        "project_path": str(project_path),
        "backend": backend,
        "drive_folder_id": drive_folder_id,
        "credentials_file": str(credentials_file),
        "token_file": str(token_file),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    if extra:
        data.update(extra)
    path.write_text(yaml.safe_dump(data))
    return path


def _write_token(path: Path, *, with_refresh: bool = True) -> None:
    """Drop a fake token file at `path`. `with_refresh=False` produces a
    file that parses but has no refresh_token field."""
    payload = {
        "token": "fake-access-token",
        "client_id": "fake-client",
        "client_secret": "fake-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }
    if with_refresh:
        payload["refresh_token"] = "fake-refresh-token"
    path.write_text(json.dumps(payload))


class _DoctorFakeStorage:
    """Stand-in storage backend used by doctor's connectivity check.

    `behavior` controls how `list_folders` reacts:
      "ok"           → returns []
      "refresh"      → raises RefreshError("invalid_grant: revoked")
      "permission"   → raises RuntimeError("403 forbidden")
      "not_found"    → raises RuntimeError("404 not found")
      "network"      → raises ConnectionError("connection timed out")
      "unknown"      → raises RuntimeError("something else exploded")
    """

    backend_name = "fake"

    def __init__(self, behavior: str = "ok") -> None:
        self._behavior = behavior

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def list_folders(self, parent_id: str, name: Any = None) -> list:
        if self._behavior == "ok":
            return []
        if self._behavior == "refresh":
            raise RefreshError("invalid_grant: token revoked")
        if self._behavior == "permission":
            raise RuntimeError("HTTP 403 forbidden — permission denied")
        if self._behavior == "not_found":
            raise RuntimeError("HTTP 404 not found")
        if self._behavior == "network":
            raise ConnectionError("connection timed out")
        if self._behavior == "unknown":
            raise RuntimeError("mystery failure")
        raise AssertionError(f"unknown behavior: {self._behavior}")

    def classify_error(self, exc: BaseException) -> Any:
        # Doctor uses this defensively — return UNKNOWN so the message-
        # based heuristics in the doctor itself drive the fix-hint choice.
        from claude_mirror.backends import ErrorClass

        return ErrorClass.UNKNOWN


def _patch_storage(
    monkeypatch: pytest.MonkeyPatch, behavior: str = "ok"
) -> List[_DoctorFakeStorage]:
    """Patch `_create_storage` to return a `_DoctorFakeStorage` with the
    requested behavior. Returns the list of created instances so tests can
    inspect call count if they care."""
    created: List[_DoctorFakeStorage] = []

    def _factory(config: Any) -> _DoctorFakeStorage:
        s = _DoctorFakeStorage(behavior=behavior)
        created.append(s)
        return s

    monkeypatch.setattr(cli_mod, "_create_storage", _factory)
    return created


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


def test_doctor_all_checks_pass_with_healthy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy-path: every check passes; command exits 0; output contains
    the all-passed marker."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "All checks passed" in out


def test_doctor_missing_credentials_file_reports_with_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """credentials_file points at a nonexistent path → doctor reports ✗
    with a re-download hint and exits 1."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    # credentials.json is intentionally NOT written.
    creds = tmp_path / "credentials.json"
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "credentials file missing" in out
    assert str(creds) in out
    # Concrete fix mentions re-downloading credentials.json.
    assert "credentials.json" in out
    assert "developer console" in out


def test_doctor_missing_token_reports_with_auth_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """token_file does not exist → doctor reports ✗ and suggests running
    `claude-mirror auth --config <path>`. Exit 1."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"  # NOT created
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "token file missing" in out
    assert "claude-mirror auth" in out
    assert str(cfg) in out


def test_doctor_corrupt_token_reports_with_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token file exists but is malformed JSON → doctor reports ✗ and
    suggests `claude-mirror auth`. Exit 1."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text("{not valid json at all")
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "token file unreadable" in out
    assert "corrupt" in out
    assert "claude-mirror auth" in out


def test_doctor_backend_connectivity_failure_reports_with_specific_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend `list_folders` raises RefreshError(invalid_grant) → doctor
    reports the AUTH-class fix (re-auth command). Exit 1."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "refresh")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "backend connectivity failed" in out
    # AUTH-class hint mentions re-authentication and the auth command.
    assert "claude-mirror auth" in out
    assert "revoked" in out.lower() or "refresh" in out.lower()


def test_doctor_invalid_project_path_reports_with_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """project_path points at a nonexistent dir → doctor reports ✗ and
    suggests editing the config. Exit 1."""
    missing_project = tmp_path / "no_such_project"  # NOT created
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=missing_project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "project_path does not exist" in out
    assert str(missing_project) in out
    # Fix hint references project_path / config file.
    assert "project_path" in out
    assert str(cfg) in out


def test_doctor_corrupt_manifest_reports_with_rm_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Manifest exists but is malformed JSON → doctor reports ✗ and
    suggests removing it. Exit 1."""
    project = tmp_path / "project"
    project.mkdir()
    # Drop a malformed manifest at the canonical location.
    manifest_path = project / ".claude_mirror_manifest.json"
    manifest_path.write_text("{garbage:::not::json")
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "manifest is corrupt" in out
    # Concrete fix references `rm` of the manifest path.
    assert "rm " in out
    assert ".claude_mirror_manifest.json" in out


def test_doctor_backend_filter_limits_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--backend dropbox` skips checks for any non-Dropbox backend in the
    config. Relevant for Tier 2 multi-mirror setups: the user can isolate
    one mirror's diagnosis without doctor blowing up on the others.

    We construct a primary googledrive config + one dropbox mirror config
    that share the same project_path. With `--backend dropbox`, the
    googledrive primary is skipped and the dropbox mirror is checked.
    """
    project = tmp_path / "project"
    project.mkdir()

    # Primary: googledrive
    primary_token = tmp_path / "primary_token.json"
    _write_token(primary_token)
    primary_creds = tmp_path / "primary_credentials.json"
    primary_creds.write_text("{}")

    # Mirror: dropbox
    mirror_token = tmp_path / "mirror_token.json"
    _write_token(mirror_token)
    mirror_creds = tmp_path / "mirror_credentials.json"
    mirror_creds.write_text("{}")
    mirror_cfg_path = _write_config(
        tmp_path / "mirror.yaml",
        project_path=project,
        token_file=mirror_token,
        credentials_file=mirror_creds,
        backend="dropbox",
        extra={"dropbox_folder": "/claude-mirror/test"},
    )

    primary_cfg_path = _write_config(
        tmp_path / "primary.yaml",
        project_path=project,
        token_file=primary_token,
        credentials_file=primary_creds,
        backend="googledrive",
        extra={"mirror_config_paths": [str(mirror_cfg_path)]},
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(primary_cfg_path), "--backend", "dropbox"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    # Primary googledrive backend should have been skipped.
    assert "skipped" in out
    assert "googledrive" in out
    # Dropbox mirror should have been checked (per-backend header line).
    assert "dropbox" in out


def test_doctor_exit_code_zero_on_all_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify result.exit_code == 0 in the all-pass scenario. Pinned
    explicitly so `claude-mirror doctor && deploy` works in scripts."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 0


def test_doctor_exit_code_one_on_any_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify result.exit_code == 1 when even one check fails — pinned
    so CI integrations don't drift to a different non-zero exit code."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # NOT writing the token → token-missing failure → must exit 1.
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage(monkeypatch, "ok")

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg)])

    assert result.exit_code == 1
