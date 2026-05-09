"""Tests for the deep SMB checks added to `claude-mirror doctor`.

The generic doctor checks live in test_doctor.py; this module covers the
SMB-only deep checks layered on top:

  1. Server reachable (TCP).
  2. SMB protocol negotiation (SMB2/3 only — SMBv1 rejected).
  3. Authentication (register_session).
  4. Share access (scandir).
  5. Folder write (sentinel + delete).
  6. Encryption status (info-only).

All smbprotocol / smbclient calls are mocked. The two seams are
`claude_mirror.cli._smb_deep_check_factory` (returns the connection /
auth state the deep checker consumes) and `smbclient.scandir` /
`smbclient.open_file` / `smbclient.remove` (mocked separately to control
the share-access and folder-write checks).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner

smbprotocol = pytest.importorskip("smbprotocol")
smbclient_mod = pytest.importorskip("smbclient")

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a very wide terminal so long absolute tmp_path strings render
    on a single line."""
    from rich.console import Console
    monkeypatch.setattr(
        cli_mod, "console", Console(force_terminal=True, width=400)
    )


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    smb_server: str = "nas.local",
    smb_port: int = 445,
    smb_share: str = "claude-mirror",
    smb_username: str = "alice",
    smb_password: str = "secret",
    smb_domain: str = "",
    smb_folder: str = "myproject",
    smb_encryption: bool = True,
    machine_name: str = "test-machine",
) -> Path:
    data: dict = {
        "project_path": str(project_path),
        "backend": "smb",
        "smb_server": smb_server,
        "smb_port": smb_port,
        "smb_share": smb_share,
        "smb_username": smb_username,
        "smb_password": smb_password,
        "smb_domain": smb_domain,
        "smb_folder": smb_folder,
        "smb_encryption": smb_encryption,
        "token_file": str(token_file),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": machine_name,
        "user": "test-user",
    }
    path.write_text(yaml.safe_dump(data))
    return path


def _write_token(path: Path) -> None:
    path.write_text(
        '{"verified_at": "2026-05-09T00:00:00Z", '
        '"server": "nas.local", "share": "claude-mirror"}'
    )


# Stand-in storage for the generic connectivity probe.
class _OkStorage:
    backend_name = "smb"

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def list_folders(self, parent_id: str, name: Any = None) -> list:
        return []

    def classify_error(self, exc: BaseException) -> Any:
        from claude_mirror.backends import ErrorClass
        return ErrorClass.UNKNOWN

    def _project_root(self) -> str:
        return "\\\\nas.local\\claude-mirror\\myproject"


def _patch_storage_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: _OkStorage())


def _patch_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tcp_error: Optional[BaseException] = None,
    negotiate_error: Optional[BaseException] = None,
    auth_error: Optional[BaseException] = None,
    smbv1_only: bool = False,
    encryption_active: Optional[bool] = True,
) -> None:
    def _factory(config: Any) -> dict:
        return {
            "tcp_error": tcp_error,
            "negotiate_error": negotiate_error,
            "auth_error": auth_error,
            "smbv1_only": smbv1_only,
            "encryption_active": encryption_active,
        }

    monkeypatch.setattr(cli_mod, "_smb_deep_check_factory", _factory)


def _patch_smbclient(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scandir_raise: Optional[BaseException] = None,
    open_file_raise: Optional[BaseException] = None,
) -> MagicMock:
    """Patch the `smbclient` module to control share-access / folder-write
    test outcomes deterministically."""
    fake = MagicMock(name="smbclient")
    if scandir_raise is not None:
        fake.scandir.side_effect = scandir_raise
    else:
        fake.scandir.return_value = iter([])
    if open_file_raise is not None:
        fake.open_file.side_effect = open_file_raise
    else:
        # Default: open_file returns a context-manageable file-like.
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=MagicMock(write=MagicMock()))
        cm.__exit__ = MagicMock(return_value=False)
        fake.open_file.return_value = cm
    fake.makedirs.return_value = None
    fake.remove.return_value = None
    monkeypatch.setitem(sys.modules, "smbclient", fake)
    return fake


def _build_healthy_config(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
    )
    return cfg


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


def test_deep_all_pass_on_healthy_smb_setup(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch, encryption_active=True)
    _patch_smbclient(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "SMB deep checks" in out
    assert "Server reachable" in out
    assert "SMB2/3 protocol negotiated" in out
    assert "Authentication succeeded" in out
    assert "Share accessible" in out
    assert "Folder writable" in out
    assert "SMB3 encryption negotiated" in out


def test_deep_tcp_refused_emits_unreachable_failure(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        tcp_error=ConnectionRefusedError("Connection refused"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Server unreachable" in out or "unreachable" in out, out
    assert "nas.local" in out
    assert "445" in out


def test_deep_tcp_timeout_emits_timeout_failure(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        tcp_error=TimeoutError("timed out"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "timed out" in out.lower()
    assert "ping nas.local" in out


def test_deep_smbv1_only_server_is_rejected_security_gate(tmp_path, monkeypatch):
    """Security gate: the deep check MUST refuse SMBv1-only servers.

    Mirrors the SFTPv1 rejection in spirit — old SMBv1 re-opens
    EternalBlue-class attack surface and we never want claude-mirror to
    silently accept it."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        negotiate_error=RuntimeError("Server only supports SMBv1"),
        smbv1_only=True,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "SMBv1" in out
    assert "refusing to connect" in out
    assert "EternalBlue" in out
    # Subsequent checks (auth / share / folder write) MUST not fire.
    assert "Authentication succeeded" not in out
    assert "Share accessible" not in out


def test_deep_negotiate_error_non_v1_emits_failure(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        negotiate_error=RuntimeError("dialect handshake failure"),
        smbv1_only=False,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "negotiation failed" in out


def test_deep_logon_failure_buckets_into_one_auth_line(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        auth_error=smbprotocol.exceptions.LogonFailure(),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "SMB authentication rejected" in out
    # Single bucket — subsequent checks short-circuited.
    bucket_count = out.count("SMB authentication rejected")
    assert bucket_count == 1, out
    assert "Share accessible" not in out
    assert "Folder writable" not in out


def test_deep_share_not_found_emits_failure(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_smbclient(
        monkeypatch,
        scandir_raise=smbprotocol.exceptions.BadNetworkName(),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Share not found" in out
    assert "claude-mirror" in out


def test_deep_share_access_denied_buckets_as_auth(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_smbclient(
        monkeypatch,
        scandir_raise=smbprotocol.exceptions.AccessDenied(),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Permission denied" in out
    assert "alice" in out  # username surfaced in fix hint


def test_deep_folder_write_permission_denied(tmp_path, monkeypatch):
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_smbclient(
        monkeypatch,
        open_file_raise=smbprotocol.exceptions.AccessDenied(),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Permission denied writing" in out
    assert "alice" in out


def test_deep_encryption_downgraded_emits_warning(tmp_path, monkeypatch):
    """Server downgraded to plaintext — info-line warning, NOT a failure."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch, encryption_active=False)
    _patch_smbclient(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "smb"]
    )
    out = _strip_ansi(result.output)
    assert "encryption requested but server negotiated down" in out
    # The downgrade warning is NOT a failure on its own.
    assert "✗ SMB" not in out


def test_deep_skipped_for_non_smb_backend(tmp_path, monkeypatch):
    """Deep SMB checks must NOT run for any other backend."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    cfg_path = tmp_path / "config.yaml"
    data: dict = {
        "project_path": str(project),
        "backend": "webdav",
        "webdav_url": "https://dav.example.com/remote.php",
        "webdav_username": "alice",
        "webdav_password": "secret",
        "token_file": str(token),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    cfg_path.write_text(yaml.safe_dump(data))

    _patch_storage_ok(monkeypatch)

    def _exploding_factory(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError(
            "_smb_deep_check_factory must NOT be called for non-smb backends"
        )

    monkeypatch.setattr(cli_mod, "_smb_deep_check_factory", _exploding_factory)
    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])
    assert "SMB deep checks" not in _strip_ansi(result.output)
