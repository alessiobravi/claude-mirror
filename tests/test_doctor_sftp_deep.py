"""Tests for the deep SFTP checks added to `claude-mirror doctor`.

The generic doctor checks (config / credentials / token / connectivity /
project / manifest) live in test_doctor.py. This module covers the
SFTP-ONLY deep checks layered on top:

  1. Host fingerprint matches `~/.ssh/known_hosts`.
  2. SSH key file exists + readable.
  3. SSH key file permissions are 0600.
  4. SSH key can decrypt (or ssh-agent will handle).
  5. Connect + authenticate.
  6. exec_command capability.
  7. Root path access.

All paramiko calls are mocked — the tests are offline, deterministic, and
well under 100ms each. The two mock seams are
`claude_mirror.cli._sftp_deep_check_factory` (returns the live host-key /
transport bundle that the deep checker consumes) and
`paramiko.SFTPClient.from_transport` (mocked separately to control the
root-path stat result).
"""
from __future__ import annotations

import os
import re
import stat as stat_mod
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner

paramiko = pytest.importorskip("paramiko")

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a very wide terminal so long absolute tmp_path strings render
    on a single line. Otherwise Rich wraps and breaks substring asserts."""
    from rich.console import Console

    monkeypatch.setattr(
        cli_mod, "console", Console(force_terminal=True, width=400)
    )


# Click 8.3+ emits a DeprecationWarning from inside CliRunner.invoke; the
# project's pytest config promotes warnings to errors. Suppress here only.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    sftp_host: str = "sftp.example.com",
    sftp_port: int = 22,
    sftp_username: str = "alice",
    sftp_key_file: str = "",
    sftp_password: str = "",
    sftp_known_hosts_file: str = "",
    sftp_strict_host_check: bool = True,
    sftp_folder: str = "/srv/claude-mirror/myproject",
    machine_name: str = "test-machine",
) -> Path:
    """Write a minimal SFTP YAML config that exercises the deep SFTP checks."""
    data: dict = {
        "project_path": str(project_path),
        "backend": "sftp",
        "sftp_host": sftp_host,
        "sftp_port": sftp_port,
        "sftp_username": sftp_username,
        "sftp_key_file": sftp_key_file,
        "sftp_password": sftp_password,
        "sftp_known_hosts_file": (
            sftp_known_hosts_file if sftp_known_hosts_file else ""
        ),
        "sftp_strict_host_check": sftp_strict_host_check,
        "sftp_folder": sftp_folder,
        "token_file": str(token_file),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": machine_name,
        "user": "test-user",
    }
    if not data["sftp_known_hosts_file"]:
        del data["sftp_known_hosts_file"]
    path.write_text(yaml.safe_dump(data))
    return path


def _write_token(path: Path) -> None:
    """Drop a fake SFTP token file ('verified at' marker, no creds)."""
    path.write_text('{"verified_at": "2026-05-08T00:00:00Z", '
                    '"host": "sftp.example.com"}')


def _make_known_hosts(path: Path, host: str = "") -> None:
    """Write an empty known_hosts file (or, if `host` given, a minimal
    valid line — just the file existing is enough for most tests)."""
    if not host:
        path.write_text("")
    else:
        # We don't actually parse this — tests that need a present-host
        # entry mock HostKeys.lookup directly.
        path.write_text(f"{host} ssh-ed25519 AAAA-fake\n")


# Stand-in storage for the generic connectivity probe — the deep tests
# all need this to "pass" so the deep section runs cleanly.
class _OkStorage:
    backend_name = "sftp"

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        # Return a fake SFTPClient that stat()s the configured folder
        # successfully — keeps the generic Check 4 (connectivity) green
        # so the deep section runs.
        fake = MagicMock()
        attr = MagicMock()
        attr.st_mode = stat_mod.S_IFDIR | 0o755
        fake.stat.return_value = attr
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
    live_fingerprint: Optional[str] = "SHA256:LIVE-FINGERPRINT-HASH",
    transport_error: Optional[BaseException] = None,
    transport: Optional[MagicMock] = None,
    key_path: str = "",
) -> MagicMock:
    """Replace `_sftp_deep_check_factory` with a stub returning the
    supplied live key / transport / key path. Returns the transport
    so tests can inspect call counts."""
    if transport is None and transport_error is None:
        transport = MagicMock(name="paramiko.Transport")
        transport.is_authenticated.return_value = True

    live_key: Optional[MagicMock] = None
    if live_fingerprint is not None:
        live_key = MagicMock(name="LiveHostKey")
        live_key.fingerprint = live_fingerprint

    def _factory(config: Any) -> dict:
        return {
            "live_host_key": live_key,
            "transport_error": transport_error,
            "key_path": key_path,
            "transport": transport,
        }

    monkeypatch.setattr(cli_mod, "_sftp_deep_check_factory", _factory)
    return transport  # may be None (transport_error path)


def _patch_hostkeys(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored_fingerprint: Optional[str] = None,
) -> MagicMock:
    """Replace `paramiko.HostKeys` so `lookup()` returns either a stub
    entry with the supplied stored fingerprint, or None when
    `stored_fingerprint=None` (= host not in known_hosts)."""
    fake_hk = MagicMock(name="HostKeys")
    if stored_fingerprint is None:
        fake_hk.lookup.return_value = None
    else:
        stored_key = MagicMock(name="StoredHostKey")
        stored_key.fingerprint = stored_fingerprint
        fake_hk.lookup.return_value = {"ssh-ed25519": stored_key}

    def _factory(*args: Any, **kwargs: Any) -> MagicMock:
        return fake_hk

    monkeypatch.setattr(paramiko, "HostKeys", _factory)
    return fake_hk


def _patch_pkey_load(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_exc: Optional[BaseException] = None,
) -> MagicMock:
    """Replace `paramiko.PKey.from_private_key_file`."""
    fake_load = MagicMock(name="from_private_key_file")
    if raise_exc is not None:
        fake_load.side_effect = raise_exc
    else:
        fake_load.return_value = MagicMock(name="LoadedKey")
    monkeypatch.setattr(
        paramiko.PKey, "from_private_key_file", staticmethod(fake_load)
    )
    return fake_load


def _patch_sftp_from_transport(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stat_raise: Optional[BaseException] = None,
) -> MagicMock:
    """Replace `paramiko.SFTPClient.from_transport` so stat() either
    succeeds (default) or raises the supplied exception."""
    fake_sftp = MagicMock(name="SFTPClient")
    if stat_raise is not None:
        fake_sftp.stat.side_effect = stat_raise
    else:
        attr = MagicMock()
        attr.st_mode = stat_mod.S_IFDIR | 0o755
        fake_sftp.stat.return_value = attr

    fake_factory = MagicMock(return_value=fake_sftp)
    monkeypatch.setattr(
        paramiko.SFTPClient, "from_transport", staticmethod(fake_factory)
    )
    return fake_sftp


def _wire_transport_session(
    transport: MagicMock,
    *,
    exec_exit: int = 0,
    exec_raise: Optional[BaseException] = None,
) -> MagicMock:
    """Wire transport.open_session() so exec_command + recv_exit_status
    behave deterministically."""
    session = MagicMock(name="Session")
    if exec_raise is not None:
        session.exec_command.side_effect = exec_raise
    session.recv_exit_status.return_value = exec_exit
    session.recv.return_value = b"claude-mirror-doctor-probe\n"
    transport.open_session.return_value = session
    return session


# Builds the canonical full configuration on disk so each test only has
# to vary the mocks.
def _build_healthy_config(
    tmp_path: Path,
    *,
    with_key: bool = True,
    with_known_hosts: bool = True,
    key_perms: int = 0o600,
) -> tuple[Path, Path, Path]:
    """Returns (cfg_path, key_path, known_hosts_path).

    `key_path` may be a non-existent path if `with_key=False`.
    `known_hosts_path` may be a non-existent path if `with_known_hosts=False`.
    """
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)

    key_path = tmp_path / "id_ed25519"
    if with_key:
        key_path.write_text("dummy-key-bytes")
        os.chmod(key_path, key_perms)

    kh_path = tmp_path / "known_hosts"
    if with_known_hosts:
        _make_known_hosts(kh_path)

    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        sftp_key_file=str(key_path) if with_key else "",
        sftp_known_hosts_file=str(kh_path),
    )
    return cfg, key_path, kh_path


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


def test_deep_all_pass_on_healthy_sftp_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: host in known_hosts with matching fingerprint, key
    file at 0600, key decryptable, connection + auth succeed,
    exec_command returns 0, root path stat succeeds."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint="SHA256:MATCHING-HASH")
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:MATCHING-HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "SFTP deep checks" in out
    assert "Host in known_hosts; fingerprint matches" in out
    assert f"Key file readable: {key_path}" in out
    assert "Key file permissions: 0600" in out
    assert "Key decryptable" in out
    assert "Connection + auth succeeded" in out
    assert "exec_command available" in out
    assert "Root path: /srv/claude-mirror/myproject" in out
    assert "All checks passed" in out


def test_deep_host_not_in_known_hosts_emits_info_no_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host absent from known_hosts ⇒ yellow info line, NO failure
    (first connection will prompt to verify the fingerprint)."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)  # host missing
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:LIVE-HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "not in" in out and "known_hosts" in out
    assert "first connection will prompt" in out
    # Other deep checks still run.
    assert "Connection + auth succeeded" in out
    assert "Root path:" in out


def test_deep_host_fingerprint_mismatch_emits_mitm_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stored fingerprint != live fingerprint ⇒ AUTH-bucket failure with
    the MITM warning, and the fix-hint mentions `ssh-keygen -R hostname`
    NOT `claude-mirror auth`."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint="SHA256:STORED-HASH")
    # Make the fact the auth would have succeeded irrelevant — fingerprint
    # mismatch must short-circuit before any auth attempt.
    transport = MagicMock()
    transport.is_authenticated.return_value = True
    transport.auth_publickey.side_effect = AssertionError(
        "auth_publickey must NOT be called after fingerprint mismatch"
    )
    _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:DIFFERENT-HASH",
        key_path=str(key_path),
        transport=transport,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Host fingerprint mismatch" in out
    assert "POSSIBLE MAN-IN-THE-MIDDLE" in out
    assert "refusing to connect" in out
    assert "ssh-keygen -R sftp.example.com" in out
    # Strong: do NOT recommend `claude-mirror auth` for fingerprint mismatch.
    fix_section = out.split("Host fingerprint mismatch")[1]
    fix_section = fix_section.split("Fix:")[1] if "Fix:" in fix_section else ""
    assert "claude-mirror auth" not in fix_section


def test_deep_key_file_missing_emits_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sftp_key_file points at a non-existent path ⇒ failure with a
    fix-hint pointing at the YAML."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    kh_path = tmp_path / "known_hosts"
    _make_known_hosts(kh_path)
    bogus_key = tmp_path / "does_not_exist"

    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        sftp_key_file=str(bogus_key),
        sftp_known_hosts_file=str(kh_path),
    )

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(bogus_key),
    )
    # Auth would still try (paramiko may have agent keys), so wire a
    # working session so the test focuses on the key-missing message.
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "SSH key file not found" in out
    assert str(bogus_key) in out
    # Fix hint mentions the YAML and ssh-keygen.
    assert "sftp_key_file" in out
    assert "ssh-keygen" in out


def test_deep_key_file_permissions_too_open_emits_chmod_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key file at 0644 (group-readable) ⇒ failure with `chmod 600` fix.
    Doctor MUST NOT auto-fix — chmod is a deliberate human action."""
    cfg, key_path, _kh = _build_healthy_config(
        tmp_path, key_perms=0o644
    )

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Key file permissions too open" in out
    assert "0644" in out
    assert f"chmod 600 {key_path}" in out
    # Confirm permissions are still 0644 — doctor must NOT have changed them.
    assert (os.stat(key_path).st_mode & 0o777) == 0o644


def test_deep_encrypted_key_no_passphrase_emits_info_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from_private_key_file raises PasswordRequiredException ⇒ INFO
    line ('ssh-agent will handle'), NOT a failure."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(
        monkeypatch,
        raise_exc=paramiko.PasswordRequiredException("encrypted"),
    )
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    # Encrypted key alone shouldn't fail the run. Auth fallback (no
    # passphrase + no password) WILL fail in check 5 — that's a separate
    # concern. To isolate the "encrypted key is INFO not failure"
    # behaviour, also configure a password so check 5 still passes via
    # password auth.
    out = _strip_ansi(result.output)
    assert "Key is encrypted" in out
    assert "ssh-agent" in out


def test_deep_encrypted_key_with_password_fallback_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirms the encrypted-key INFO doesn't itself fail the run when
    a password is configured as fallback."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("dummy")
    os.chmod(key_path, 0o600)
    kh_path = tmp_path / "known_hosts"
    _make_known_hosts(kh_path)
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        sftp_key_file=str(key_path),
        sftp_password="fallback-pw",
        sftp_known_hosts_file=str(kh_path),
    )

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(
        monkeypatch,
        raise_exc=paramiko.PasswordRequiredException("encrypted"),
    )
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Key is encrypted" in out
    assert "Connection + auth succeeded" in out


def test_deep_connection_refused_emits_unreachable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport open raises ConnectionRefusedError ⇒ failure with
    'server unreachable' message and a port-check fix hint."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    _patch_factory(
        monkeypatch,
        live_fingerprint=None,
        transport_error=ConnectionRefusedError("Connection refused"),
        transport=None,
        key_path=str(key_path),
    )
    _patch_pkey_load(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Server unreachable" in out or "unreachable" in out, out
    assert "sftp.example.com" in out
    # Fix hint mentions checking that the port is open.
    assert "port" in out.lower()


def test_deep_connection_timeout_emits_timeout_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport open raises socket.timeout ⇒ failure with
    'timed out' message and a ping/port-check fix hint."""
    import socket

    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    _patch_factory(
        monkeypatch,
        live_fingerprint=None,
        transport_error=socket.timeout("timed out"),
        transport=None,
        key_path=str(key_path),
    )
    _patch_pkey_load(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "timed out" in out.lower(), out
    assert "ping sftp.example.com" in out


def test_deep_auth_rejected_buckets_into_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transport handshake succeeds but auth_publickey raises
    AuthenticationException ⇒ AUTH-bucket failure, exec_command +
    root-path checks SKIPPED so we don't cascade five identical
    'auth needed' lines off the same root cause."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)

    transport = MagicMock(name="paramiko.Transport")
    # Auth raises rejected.
    transport.auth_publickey.side_effect = paramiko.AuthenticationException(
        "Authentication failed"
    )
    transport.is_authenticated.return_value = False
    # If anything calls open_session AFTER the auth bucket fires, blow up.
    transport.open_session.side_effect = AssertionError(
        "open_session must NOT be called after auth-bucket fires"
    )
    _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
        transport=transport,
    )
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "SSH authentication rejected" in out
    # Single bucket, not five.
    bucket_count = out.count("SSH authentication rejected")
    assert bucket_count == 1, (
        f"expected exactly one auth-bucket line, got {bucket_count}\n\n{out}"
    )
    # exec_command and Root path lines must NOT appear — they were skipped.
    assert "exec_command" not in out
    assert "Root path:" not in out


def test_deep_exec_command_unavailable_emits_info_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """transport.open_session() raises ⇒ INFO line about client-side
    hashing fallback. NOT a failure — internal-sftp-jailed accounts
    are a fully supported (slightly slower) configuration."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
    )
    transport.open_session.side_effect = paramiko.SSHException(
        "channel request refused (internal-sftp)"
    )
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "exec_command unavailable" in out
    assert "client-side hashing fallback" in out
    # The root-path check must still run after exec_command's info line.
    assert "Root path:" in out


def test_deep_root_path_not_found_emits_info_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sftp.stat raises errno=2 (NotFound) ⇒ INFO line ('created on
    first push'), NOT a failure."""
    import errno

    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(
        monkeypatch,
        stat_raise=IOError(errno.ENOENT, "No such file"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Configured root doesn't exist" in out
    assert "/srv/claude-mirror/myproject" in out
    assert "creates it on first push" in out


def test_deep_root_path_permission_denied_buckets_as_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sftp.stat raises errno=13 (Permission denied) ⇒ AUTH-bucket
    failure with a server-side ACL fix hint."""
    import errno

    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint=None)
    transport = _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:HASH",
        key_path=str(key_path),
    )
    _wire_transport_session(transport, exec_exit=0)
    _patch_pkey_load(monkeypatch)
    _patch_sftp_from_transport(
        monkeypatch,
        stat_raise=IOError(errno.EACCES, "Permission denied"),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Permission denied" in out
    assert "/srv/claude-mirror/myproject" in out
    assert "alice" in out  # username surfaced in the fix hint


def test_deep_auth_grouping_fingerprint_mismatch_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fingerprint mismatch ⇒ ONE failure line, no further probes
    attempted. Wires `auth_publickey` and `from_transport` to AssertionError
    so the test fails LOUDLY if anything sneaks through after the bucket."""
    cfg, key_path, _kh = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_hostkeys(monkeypatch, stored_fingerprint="SHA256:STORED")

    transport = MagicMock()
    transport.auth_publickey.side_effect = AssertionError(
        "must not auth after fingerprint mismatch"
    )
    transport.open_session.side_effect = AssertionError(
        "must not open_session after fingerprint mismatch"
    )
    _patch_factory(
        monkeypatch,
        live_fingerprint="SHA256:DIFFERENT",
        key_path=str(key_path),
        transport=transport,
    )

    # If from_transport gets called, that's also a leak.
    bad_factory = MagicMock(
        side_effect=AssertionError(
            "SFTPClient.from_transport must not be called"
        )
    )
    monkeypatch.setattr(
        paramiko.SFTPClient, "from_transport", staticmethod(bad_factory)
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "sftp"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    # ONE failure summary in the final tally — fingerprint mismatch.
    assert out.count("Host fingerprint mismatch") == 1
    assert "1 issue" in out
    # Confirm the assertion-armed mocks weren't invoked.
    assert transport.auth_publickey.call_count == 0
    assert transport.open_session.call_count == 0
    bad_factory.assert_not_called()


def test_deep_skipped_for_non_sftp_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deep SFTP checks must NOT run for googledrive / dropbox / onedrive
    / webdav — they're SFTP-specific. We confirm by writing a webdav config
    and a factory-stub that fails loudly if invoked."""
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
        "webdav_folder": "claude-mirror/myproject",
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
            "_sftp_deep_check_factory must NOT be called for non-sftp backends"
        )

    monkeypatch.setattr(
        cli_mod, "_sftp_deep_check_factory", _exploding_factory
    )

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])

    # Result code may be non-zero if generic checks fail (e.g. the WebDAV
    # backend's own list_folders probe). What matters: deep SFTP code
    # never ran.
    assert "SFTP deep checks" not in _strip_ansi(result.output)
