"""Per-backend smoke tests for FtpBackend (BACKEND-FTP).

The Python stdlib `ftplib.FTP` and `ftplib.FTP_TLS` clients are mocked
via unittest.mock — every test wires a stub FTP client onto
`backend._ftp` (the cached connection) and exercises the real backend
code paths against the stub.

All tests must stay <100ms and offline; no network, no sockets, no
real ftplib instances.
"""
from __future__ import annotations

import ftplib
import io
import socket
import ssl
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from claude_mirror.backends import BackendError, ErrorClass
from claude_mirror.backends.ftp import (
    CLEARTEXT_WARNING,
    FtpBackend,
    _is_loopback_or_rfc1918,
    _parse_hash_response,
    _parse_list_line,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _make_backend(make_config, config_dir: Path, **overrides) -> FtpBackend:
    cfg = make_config(
        backend="ftp",
        ftp_host=overrides.pop("ftp_host", "ftp.example.com"),
        ftp_port=overrides.pop("ftp_port", 21),
        ftp_username=overrides.pop("ftp_username", "alice"),
        ftp_password=overrides.pop("ftp_password", "hunter2"),
        ftp_folder=overrides.pop("ftp_folder", "claude-mirror/myproject"),
        ftp_tls=overrides.pop("ftp_tls", "explicit"),
        ftp_passive=overrides.pop("ftp_passive", True),
        token_file=str(config_dir / "token.json"),
        **overrides,
    )
    return FtpBackend(cfg)


def _wire_fake_ftp(backend: FtpBackend) -> MagicMock:
    """Attach a stub ftplib.FTP onto the backend; bypass _connect."""
    fake = MagicMock(name="ftplib.FTP")
    fake.voidcmd.return_value = "200 OK"
    backend._ftp = fake
    return fake


# ─── 1. authenticate happy path ───────────────────────────────────────────────


def test_authenticate_writes_token_and_cwds_into_folder(
    make_config, config_dir, monkeypatch,
):
    """A reachable server + valid creds → token file written; cwd
    succeeded against the configured folder."""
    backend = _make_backend(make_config, config_dir)

    fake = MagicMock()
    fake.voidcmd.return_value = "200 OK"

    def fake_open() -> Any:
        return fake

    monkeypatch.setattr(backend, "_open_connection", fake_open)
    backend.authenticate()

    fake.cwd.assert_called_once_with("claude-mirror/myproject")
    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    import json
    data = json.loads(token_path.read_text())
    assert data["host"] == "ftp.example.com"
    assert data["tls"] == "explicit"


def test_authenticate_creates_missing_folder_then_cwds(
    make_config, config_dir, monkeypatch,
):
    """When the configured ftp_folder doesn't exist, authenticate
    should mkd-p the path and then cwd into it."""
    backend = _make_backend(
        make_config, config_dir, ftp_folder="claude-mirror/myproject",
    )

    fake = MagicMock()
    fake.voidcmd.return_value = "200 OK"
    cwd_calls = {"count": 0}

    def fake_cwd(path: str) -> None:
        cwd_calls["count"] += 1
        if cwd_calls["count"] == 1:
            raise ftplib.error_perm("550 Folder not found")

    fake.cwd.side_effect = fake_cwd
    monkeypatch.setattr(backend, "_open_connection", lambda: fake)

    backend.authenticate()
    assert fake.mkd.call_count >= 1


def test_authenticate_bad_credentials_classified_as_AUTH(
    make_config, config_dir, monkeypatch,
):
    """A 530 from the server during login → ErrorClass.AUTH."""
    backend = _make_backend(make_config, config_dir)

    def boom() -> Any:
        raise ftplib.error_perm("530 Login authentication failed")

    monkeypatch.setattr(backend, "_open_connection", boom)
    with pytest.raises(ftplib.error_perm) as exc_info:
        backend.authenticate()
    assert backend.classify_error(exc_info.value) == ErrorClass.AUTH


def test_authenticate_connection_refused_classified_as_TRANSIENT(
    make_config, config_dir, monkeypatch,
):
    """Connection refused → ErrorClass.TRANSIENT."""
    backend = _make_backend(make_config, config_dir)

    def boom() -> Any:
        raise ConnectionRefusedError("Connection refused")

    monkeypatch.setattr(backend, "_open_connection", boom)
    with pytest.raises(ConnectionRefusedError) as exc_info:
        backend.authenticate()
    assert backend.classify_error(exc_info.value) == ErrorClass.TRANSIENT


# ─── 2. get_credentials raises if no token ─────────────────────────────────────


def test_get_credentials_raises_when_token_missing(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert not Path(backend.config.token_file).exists()
    with pytest.raises(RuntimeError, match="not authenticated"):
        backend.get_credentials()


# ─── 3. resolve_path / get_or_create_folder ────────────────────────────────────


def test_get_or_create_folder_creates_missing(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    path = backend.get_or_create_folder("subdir", "claude-mirror/myproject")
    assert path == "claude-mirror/myproject/subdir"
    fake.mkd.assert_called_once_with("claude-mirror/myproject/subdir")


def test_get_or_create_folder_idempotent_when_present(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.mkd.side_effect = ftplib.error_perm("550 File exists")

    path = backend.get_or_create_folder("already", "claude-mirror/myproject")
    assert path == "claude-mirror/myproject/already"


def test_resolve_path_walks_components(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    parent, basename = backend.resolve_path(
        "a/b/c.md", "claude-mirror/myproject"
    )
    assert parent == "claude-mirror/myproject/a/b"
    assert basename == "c.md"
    assert fake.mkd.call_count == 2  # a, then a/b


# ─── 4. upload + download ──────────────────────────────────────────────────────


def test_upload_file_calls_storbinary(
    make_config, config_dir, project_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    local = project_dir / "note.md"
    local.write_text("hello-ftp")

    file_id = backend.upload_file(
        str(local), "note.md", "claude-mirror/myproject",
    )
    assert file_id == "claude-mirror/myproject/note.md"
    assert fake.storbinary.call_count == 1
    cmd_arg = fake.storbinary.call_args[0][0]
    assert cmd_arg.startswith("STOR ")
    assert cmd_arg.endswith("/myproject/note.md")


def test_upload_bytes_round_trip(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    file_id = backend.upload_bytes(
        b'{"a":1}', "manifest.json", "claude-mirror/myproject",
    )
    assert file_id == "claude-mirror/myproject/manifest.json"
    fake.storbinary.assert_called_once()
    args = fake.storbinary.call_args
    assert args[0][0] == "STOR claude-mirror/myproject/manifest.json"


def test_download_file_streams_via_retrbinary(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    payload = b"x" * (200 * 1024)

    def fake_retr(cmd: str, callback: Any, blocksize: int = 0) -> None:
        for i in range(0, len(payload), 8192):
            callback(payload[i:i + 8192])

    fake.retrbinary.side_effect = fake_retr

    out = backend.download_file("claude-mirror/myproject/big.bin")
    assert out == payload


def test_download_file_aborts_on_size_cap(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    def fake_retr(cmd: str, callback: Any, blocksize: int = 0) -> None:
        for _ in range(64):
            callback(b"y" * 8192)

    fake.retrbinary.side_effect = fake_retr

    with pytest.raises(BackendError) as exc_info:
        backend.download_file(
            "claude-mirror/myproject/huge.bin", max_bytes=4096,
        )
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


# ─── 5. get_file_id present / absent ───────────────────────────────────────────


def test_get_file_id_returns_path_when_size_succeeds(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.size.return_value = 42

    fid = backend.get_file_id("note.md", "claude-mirror/myproject")
    assert fid == "claude-mirror/myproject/note.md"


def test_get_file_id_returns_none_when_absent(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.size.side_effect = ftplib.error_perm("550 No such file")

    def fake_mlsd(path: str) -> Any:
        return iter([])

    fake.mlsd.side_effect = fake_mlsd
    assert backend.get_file_id(
        "missing.md", "claude-mirror/myproject"
    ) is None


# ─── 6. copy_file falls back to download+upload ────────────────────────────────


def test_copy_file_round_trips_via_download_upload(
    make_config, config_dir,
):
    """FTP has no server-side copy; the implementation downloads then
    re-uploads to the destination."""
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.size.return_value = 100  # under the 50MB temp-file threshold

    payload = b"copyme"

    def fake_retr(cmd: str, callback: Any, blocksize: int = 0) -> None:
        callback(payload)

    fake.retrbinary.side_effect = fake_retr

    new_path = backend.copy_file(
        "claude-mirror/myproject/src.md",
        "claude-mirror/myproject/dst",
        "src.md",
    )
    assert new_path == "claude-mirror/myproject/dst/src.md"
    assert fake.storbinary.call_count == 1


# ─── 7. get_file_hash via XSHA256 happy path + fallback ───────────────────────


def test_get_file_hash_via_XSHA256_command(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    digest = "0" * 64
    fake.sendcmd.return_value = f"213 {digest}"

    h = backend.get_file_hash("claude-mirror/myproject/note.md")
    assert h == digest


def test_get_file_hash_falls_back_to_streaming_when_HASH_unsupported(
    make_config, config_dir,
):
    """When XSHA256 / HASH / XSHA1 / XMD5 all fail, the backend falls
    back to streaming the bytes and computing sha256 client-side."""
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.sendcmd.side_effect = ftplib.error_perm("502 Command unsupported")

    payload = b"hash-me-please"

    def fake_retr(cmd: str, callback: Any, blocksize: int = 0) -> None:
        callback(payload)

    fake.retrbinary.side_effect = fake_retr

    import hashlib
    expected = hashlib.sha256(payload).hexdigest()

    h = backend.get_file_hash("claude-mirror/myproject/note.md")
    assert h == expected


# ─── 8. delete_file ────────────────────────────────────────────────────────────


def test_delete_file_removes(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    backend.delete_file("claude-mirror/myproject/note.md")
    fake.delete.assert_called_once_with("claude-mirror/myproject/note.md")


def test_delete_file_falls_back_to_rmd_when_path_is_directory(
    make_config, config_dir,
):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.delete.side_effect = ftplib.error_perm(
        "550 Is a directory"
    )

    backend.delete_file("claude-mirror/myproject/subdir")
    fake.rmd.assert_called_once_with("claude-mirror/myproject/subdir")


# ─── 9. classify_error matrix ──────────────────────────────────────────────────


def test_classify_error_530_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = ftplib.error_perm("530 Login incorrect")
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_classify_error_550_no_such_FILE_REJECTED(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = ftplib.error_perm("550 No such file or directory")
    assert backend.classify_error(exc) == ErrorClass.FILE_REJECTED


def test_classify_error_550_permission_PERMISSION(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = ftplib.error_perm("550 Permission denied")
    assert backend.classify_error(exc) == ErrorClass.PERMISSION


def test_classify_error_552_QUOTA(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = ftplib.error_perm("552 Storage exceeded")
    assert backend.classify_error(exc) == ErrorClass.QUOTA


def test_classify_error_socket_timeout_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = socket.timeout("timed out")
    assert backend.classify_error(exc) == ErrorClass.TRANSIENT


def test_classify_error_BrokenPipe_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = BrokenPipeError("broken")
    assert backend.classify_error(exc) == ErrorClass.TRANSIENT


def test_classify_error_SSLError_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = ssl.SSLError("handshake failed")
    assert backend.classify_error(exc) == ErrorClass.AUTH


# ─── 10. TLS modes pick the right ftplib class ─────────────────────────────────


def test_open_connection_explicit_uses_FTP_TLS(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir, ftp_tls="explicit")

    fake_tls = MagicMock(name="FTP_TLS")
    monkeypatch.setattr(ftplib, "FTP_TLS", lambda: fake_tls)
    monkeypatch.setattr(ftplib, "FTP", lambda: pytest.fail("FTP must not be used in explicit mode"))

    backend._open_connection()
    fake_tls.connect.assert_called_once()
    fake_tls.auth.assert_called_once()
    fake_tls.prot_p.assert_called_once()
    fake_tls.set_pasv.assert_called_with(True)


def test_open_connection_off_uses_FTP_and_warns(
    make_config, config_dir, monkeypatch, capsys,
):
    backend = _make_backend(make_config, config_dir, ftp_tls="off")

    fake_plain = MagicMock(name="FTP")
    monkeypatch.setattr(ftplib, "FTP", lambda: fake_plain)
    monkeypatch.setattr(
        ftplib, "FTP_TLS",
        lambda: pytest.fail("FTP_TLS must not be used in off mode"),
    )

    backend._open_connection()
    fake_plain.connect.assert_called_once()
    fake_plain.login.assert_called_once()
    captured = capsys.readouterr()
    assert "cleartext" in captured.err.lower()
    assert "UNENCRYPTED" in captured.err


def test_open_connection_implicit_wraps_socket_with_ssl(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(
        make_config, config_dir, ftp_tls="implicit", ftp_port=990,
    )

    fake_sock = MagicMock(name="raw_sock")
    fake_wrapped = MagicMock(name="wrapped_sock")
    fake_wrapped.makefile.return_value = MagicMock(name="file")

    fake_ctx = MagicMock(name="ssl_ctx")
    fake_ctx.wrap_socket.return_value = fake_wrapped

    fake_tls = MagicMock(name="FTP_TLS")
    fake_tls.getresp.return_value = "220 implicit greeting"

    monkeypatch.setattr(ssl, "create_default_context", lambda: fake_ctx)
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: fake_sock)
    monkeypatch.setattr(ftplib, "FTP_TLS", lambda: fake_tls)

    backend._open_connection()
    fake_ctx.wrap_socket.assert_called_once()
    fake_tls.login.assert_called_once()


# ─── 11. passive vs active mode honoured ───────────────────────────────────────


def test_open_connection_honours_passive_false(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(
        make_config, config_dir, ftp_tls="off", ftp_passive=False,
    )
    fake_plain = MagicMock(name="FTP")
    monkeypatch.setattr(ftplib, "FTP", lambda: fake_plain)
    backend._open_connection()
    fake_plain.set_pasv.assert_called_with(False)


# ─── 12. MLSD vs LIST fallback ─────────────────────────────────────────────────


def test_list_files_recursive_uses_MLSD_when_supported(
    make_config, config_dir,
):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    def fake_mlsd(path: str) -> Any:
        if path == "claude-mirror/myproject":
            yield "note.md", {"type": "file", "size": "42"}
            yield "sub", {"type": "dir", "size": "0"}
        elif path == "claude-mirror/myproject/sub":
            yield "deep.md", {"type": "file", "size": "10"}

    fake.mlsd.side_effect = fake_mlsd

    results = backend.list_files_recursive("claude-mirror/myproject")
    rels = {r["relative_path"] for r in results}
    assert "note.md" in rels
    assert "sub/deep.md" in rels
    assert fake.retrlines.called is False  # LIST never invoked


def test_list_files_recursive_falls_back_to_LIST_when_MLSD_unsupported(
    make_config, config_dir,
):
    """Servers that don't speak MLSD (legacy hosting) must fall back
    to parsing LIST output."""
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)
    fake.mlsd.side_effect = ftplib.error_perm("502 MLSD not implemented")

    def fake_list(cmd: str, callback: Any) -> None:
        if "claude-mirror/myproject" in cmd and "/sub" not in cmd:
            callback(
                "-rw-r--r--   1 alice  users      42 May 09 12:00 note.md"
            )
            callback(
                "drwxr-xr-x   1 alice  users       0 May 09 12:00 sub"
            )
        elif cmd.endswith("/sub"):
            callback(
                "-rw-r--r--   1 alice  users      10 May 09 12:00 deep.md"
            )

    fake.retrlines.side_effect = fake_list

    results = backend.list_files_recursive("claude-mirror/myproject")
    rels = {r["relative_path"] for r in results}
    assert "note.md" in rels
    assert "sub/deep.md" in rels


def test_list_files_recursive_excludes_named_folders(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake = _wire_fake_ftp(backend)

    def fake_mlsd(path: str) -> Any:
        if path == "claude-mirror/myproject":
            yield "keep.md", {"type": "file", "size": "1"}
            yield "_claude_mirror_snapshots", {"type": "dir", "size": "0"}

    fake.mlsd.side_effect = fake_mlsd

    results = backend.list_files_recursive(
        "claude-mirror/myproject",
        exclude_folder_names={"_claude_mirror_snapshots"},
    )
    rels = {r["relative_path"] for r in results}
    assert "keep.md" in rels
    assert not any("_claude_mirror_snapshots" in r for r in rels)


# ─── 13. parse helpers ─────────────────────────────────────────────────────────


def test_parse_list_line_unix_file():
    line = "-rw-r--r--   1 alice  users      42 May 09 12:00 note.md"
    parsed = _parse_list_line(line)
    assert parsed == ("note.md", "file", 42)


def test_parse_list_line_unix_dir():
    line = "drwxr-xr-x   1 alice  users       0 May 09 12:00 sub"
    parsed = _parse_list_line(line)
    assert parsed == ("sub", "dir", 0)


def test_parse_list_line_iis_dir():
    line = "05-09-26  10:15AM   <DIR>   sub"
    parsed = _parse_list_line(line)
    assert parsed == ("sub", "dir", 0)


def test_parse_hash_response_extracts_hex_digest():
    resp = "213 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    digest = _parse_hash_response(resp)
    assert digest == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def test_parse_hash_response_returns_none_on_no_hex():
    resp = "550 not supported"
    assert _parse_hash_response(resp) is None


# ─── 14. cleartext warning content ─────────────────────────────────────────────


def test_cleartext_warning_string_warns_about_unencrypted():
    """The exported warning constant must mention UNENCRYPTED so the
    user sees the security implication, not just a generic 'cleartext'
    label."""
    assert "UNENCRYPTED" in CLEARTEXT_WARNING


# ─── 15. RFC1918 / loopback helper ─────────────────────────────────────────────


def test_is_loopback_or_rfc1918_recognises_loopback():
    assert _is_loopback_or_rfc1918("127.0.0.1") is True


def test_is_loopback_or_rfc1918_recognises_rfc1918_10():
    assert _is_loopback_or_rfc1918("10.0.0.1") is True


def test_is_loopback_or_rfc1918_recognises_rfc1918_192_168():
    assert _is_loopback_or_rfc1918("192.168.1.1") is True
