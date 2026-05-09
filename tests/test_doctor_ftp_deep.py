"""Tests for the deep FTP checks in `claude-mirror doctor` (BACKEND-FTP).

Six deep checks layered on top of the generic doctor sequence:

  1. Host reachable — TCP connect.
  2. Server greeting + protocol banner.
  3. TLS handshake (when ftp_tls != "off").
  4. Authentication.
  5. Folder access (cwd into ftp_folder).
  6. Folder write (STOR + DELE sentinel).

Plus the cleartext-mode advisory when ftp_tls=off against a non-RFC1918
host. All tests mock the connection layer through the
`_ftp_deep_check_factory` seam — no sockets, no real ftplib.
"""
from __future__ import annotations

import ftplib
import re
import socket
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console
    monkeypatch.setattr(
        cli_mod, "console", Console(force_terminal=True, width=400)
    )


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    ftp_host: str = "ftp.example.com",
    ftp_port: int = 21,
    ftp_username: str = "alice",
    ftp_password: str = "hunter2",
    ftp_folder: str = "claude-mirror/myproject",
    ftp_tls: str = "explicit",
    ftp_passive: bool = True,
    machine_name: str = "test-machine",
) -> Path:
    data: dict = {
        "project_path": str(project_path),
        "backend": "ftp",
        "ftp_host": ftp_host,
        "ftp_port": ftp_port,
        "ftp_username": ftp_username,
        "ftp_password": ftp_password,
        "ftp_folder": ftp_folder,
        "ftp_tls": ftp_tls,
        "ftp_passive": ftp_passive,
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
        '"host": "ftp.example.com", "tls": "explicit"}'
    )


class _OkStorage:
    backend_name = "ftp"

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        fake = MagicMock()
        return fake

    def list_folders(self, parent_id: str, name: Any = None) -> list:
        return []

    def classify_error(self, exc: BaseException) -> Any:
        from claude_mirror.backends import ErrorClass
        return ErrorClass.UNKNOWN


def _patch_storage_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: _OkStorage())


def _patch_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ftp_obj: Any = None,
    tls_info: Any = ("AES256-GCM-SHA384", "TLSv1.3"),
    banner: str = "220 Welcome to ProFTPD",
    transport_error: BaseException | None = None,
) -> Any:
    if ftp_obj is None and transport_error is None:
        ftp_obj = MagicMock(name="ftplib.FTP")
    if transport_error is not None:
        ftp_obj = None

    def _factory(config: Any) -> dict:
        return {
            "ftp": ftp_obj,
            "tls_info": tls_info,
            "banner": banner,
            "transport_error": transport_error,
        }

    monkeypatch.setattr(cli_mod, "_ftp_deep_check_factory", _factory)
    return ftp_obj


def _patch_socket_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make socket.create_connection return a closable stub so the
    Check 1 reachability probe always passes when wanted."""
    fake_sock = MagicMock()
    monkeypatch.setattr(
        cli_mod.socket if hasattr(cli_mod, "socket") else socket,
        "create_connection",
        lambda *a, **kw: fake_sock,
    )
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: fake_sock)


def _build_healthy_config(tmp_path: Path, **kwargs: Any) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    return _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        **kwargs,
    )


# ─── Tests ─────────────────────────────────────────────────────────────────────


def test_deep_all_pass_on_healthy_ftps_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    ftp_obj = _patch_factory(monkeypatch)
    ftp_obj.cwd.return_value = None
    ftp_obj.storbinary.return_value = None
    ftp_obj.delete.return_value = None

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "FTP deep checks" in out
    assert "Host reachable" in out
    assert "Server banner" in out
    assert "TLS handshake" in out
    assert "Authentication succeeded" in out
    assert "Folder accessible" in out
    assert "Folder writable" in out


def test_deep_connection_refused_emits_unreachable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(socket, "create_connection", boom)
    _patch_factory(
        monkeypatch,
        ftp_obj=None,
        transport_error=ConnectionRefusedError("refused"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Server unreachable" in out or "unreachable" in out.lower()


def test_deep_connection_timeout_emits_timeout_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("timed out")

    monkeypatch.setattr(socket, "create_connection", boom)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "timed out" in out.lower() or "timeout" in out.lower()


def test_deep_auth_rejected_530_buckets_into_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        transport_error=ftplib.error_perm("530 Login incorrect"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "FTP authentication rejected" in out
    assert out.count("FTP authentication rejected") == 1


def test_deep_tls_handshake_failure_buckets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ssl as _ssl
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    _patch_factory(
        monkeypatch,
        transport_error=_ssl.SSLError("handshake failed"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "TLS handshake failed" in out


def test_deep_folder_not_found_emits_failure_with_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    ftp_obj = _patch_factory(monkeypatch)
    ftp_obj.cwd.side_effect = ftplib.error_perm("550 No such directory")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Configured folder doesn't exist" in out
    assert "claude-mirror/myproject" in out


def test_deep_folder_permission_denied_buckets_as_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    ftp_obj = _patch_factory(monkeypatch)
    ftp_obj.cwd.side_effect = ftplib.error_perm(
        "550 Permission denied"
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Permission denied" in out
    assert "alice" in out  # username surfaced in fix hint


def test_deep_folder_write_quota_emits_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    ftp_obj = _patch_factory(monkeypatch)
    ftp_obj.cwd.return_value = None
    ftp_obj.storbinary.side_effect = ftplib.error_perm(
        "552 Storage allocation exceeded"
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "quota" in out.lower() or "storage" in out.lower()


def test_deep_cleartext_warning_emitted_on_public_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ftp_tls=off against a non-loopback / non-RFC1918 host emits the
    cleartext advisory line."""
    cfg = _build_healthy_config(tmp_path, ftp_tls="off")
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    ftp_obj = _patch_factory(monkeypatch, tls_info=None, banner="220 plain")
    ftp_obj.cwd.return_value = None
    ftp_obj.storbinary.return_value = None
    ftp_obj.delete.return_value = None
    monkeypatch.setattr(
        cli_mod, "_is_loopback_or_rfc1918_for_doctor",
        lambda host: False,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    out = _strip_ansi(result.output)
    assert "Cleartext FTP enabled" in out
    assert "UNENCRYPTED" in out


def test_deep_cleartext_advisory_softer_on_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ftp_tls=off + loopback host → softer advisory line that does NOT
    say UNENCRYPTED, since LAN-only use is the documented contract."""
    cfg = _build_healthy_config(tmp_path, ftp_tls="off", ftp_host="127.0.0.1")
    _patch_storage_ok(monkeypatch)
    _patch_socket_ok(monkeypatch)
    ftp_obj = _patch_factory(monkeypatch, tls_info=None, banner="220 plain")
    ftp_obj.cwd.return_value = None
    ftp_obj.storbinary.return_value = None
    ftp_obj.delete.return_value = None

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "ftp"]
    )
    out = _strip_ansi(result.output)
    assert "Cleartext FTP enabled" in out
    assert "loopback or RFC1918 range" in out


def test_deep_skipped_for_non_ftp_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deep FTP checks must NOT run for googledrive / dropbox / etc."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "project_path": str(project),
        "backend": "webdav",
        "webdav_url": "https://dav.example.com/remote.php",
        "webdav_username": "alice",
        "webdav_password": "secret",
        "token_file": str(token),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test",
        "user": "test",
    }))

    _patch_storage_ok(monkeypatch)

    def _exploding_factory(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError(
            "_ftp_deep_check_factory must NOT be called for non-ftp backends"
        )

    monkeypatch.setattr(
        cli_mod, "_ftp_deep_check_factory", _exploding_factory
    )

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])
    assert "FTP deep checks" not in _strip_ansi(result.output)
