"""Per-backend smoke tests for SFTPBackend.

paramiko's SSHClient + SFTPClient are mocked via unittest.mock. A
tiny in-memory `FakeServer` (a dict[abs_path, bytes | "DIR"]) backs
the mocked SFTPClient methods — listdir_attr, stat, mkdir, put,
posix_rename, open, remove, rmdir — so the test exercises the real
backend code paths against an offline fake.

All tests must stay <100 ms and offline; no network, no filesystem
beyond tmp_path.
"""
from __future__ import annotations

import errno
import io
import os
import socket
import stat as stat_mod
from pathlib import Path
from typing import Any, Dict, Optional, Union
from unittest.mock import MagicMock, patch

import pytest

paramiko = pytest.importorskip("paramiko")

from claude_mirror.backends import BackendError, ErrorClass
from claude_mirror.backends.sftp import SFTPBackend

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Fake server (in-memory) ───────────────────────────────────────────────────

DIR = "DIR"  # sentinel for directory entries


class _FakeAttr:
    """Mimic paramiko.SFTPAttributes — only the fields the backend reads."""

    def __init__(self, filename: str, mode: int, size: int = 0, mtime: int = 0):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


class _FakeFile(io.BytesIO):
    """Stand-in for the file object returned by sftp.open(...).

    Wraps BytesIO so the backend's `f.read(N)` and `f.write(content)`
    code paths behave like real SFTP file handles. On context-exit we
    flush the contents back into the FakeServer's storage.
    """

    def __init__(self, server: "FakeServer", path: str, mode: str, initial: bytes = b""):
        super().__init__(initial)
        self._server = server
        self._path = path
        self._mode = mode

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if "w" in self._mode or "a" in self._mode:
            self._server.storage[self._path] = self.getvalue()
        self.close()
        return False


class FakeServer:
    """Minimal in-memory SFTP backend.

    storage maps absolute paths (no trailing slash) to either a `bytes`
    (file content) or the sentinel string "DIR" (directory). The
    methods below mirror the paramiko.SFTPClient surface that
    SFTPBackend actually calls.
    """

    def __init__(self):
        self.storage: Dict[str, Union[bytes, str]] = {"/": DIR}
        # Pre-create the typical project root so authenticate's stat()
        # doesn't trip; tests that exercise the missing-folder path
        # blow this entry away.
        self.storage["/srv"] = DIR
        self.storage["/srv/project"] = DIR

    # --- paramiko.SFTPClient surface -------------------------------

    def stat(self, path: str) -> _FakeAttr:
        path = self._norm(path)
        if path not in self.storage:
            raise IOError(errno.ENOENT, "No such file")
        entry = self.storage[path]
        if entry == DIR:
            return _FakeAttr(filename=path.rsplit("/", 1)[-1], mode=stat_mod.S_IFDIR | 0o755)
        return _FakeAttr(
            filename=path.rsplit("/", 1)[-1],
            mode=stat_mod.S_IFREG | 0o644,
            size=len(entry),
        )

    def listdir_attr(self, path: str):
        path = self._norm(path)
        if path not in self.storage or self.storage[path] != DIR:
            raise IOError(errno.ENOENT, "No such directory")
        prefix = path.rstrip("/") + "/"
        out = []
        seen = set()
        for p, v in list(self.storage.items()):
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if not rest or "/" in rest:
                continue
            if rest in seen:
                continue
            seen.add(rest)
            if v == DIR:
                out.append(_FakeAttr(filename=rest, mode=stat_mod.S_IFDIR | 0o755))
            else:
                out.append(_FakeAttr(
                    filename=rest,
                    mode=stat_mod.S_IFREG | 0o644,
                    size=len(v),
                ))
        return out

    def mkdir(self, path: str, mode: int = 511) -> None:
        path = self._norm(path)
        if path in self.storage:
            raise IOError(errno.EEXIST, "File exists")
        # Refuse if parent doesn't exist — the backend's _mkdir_p creates
        # parents top-down so this should never trip in practice.
        parent = path.rsplit("/", 1)[0] or "/"
        if parent not in self.storage:
            raise IOError(errno.ENOENT, "Parent missing")
        self.storage[path] = DIR

    def rmdir(self, path: str) -> None:
        path = self._norm(path)
        if path not in self.storage or self.storage[path] != DIR:
            raise IOError(errno.ENOENT, "No such directory")
        del self.storage[path]

    def remove(self, path: str) -> None:
        path = self._norm(path)
        if path not in self.storage:
            raise IOError(errno.ENOENT, "No such file")
        if self.storage[path] == DIR:
            raise IOError(errno.EISDIR, "Is a directory")
        del self.storage[path]

    def put(self, local_path: str, remote_path: str, callback=None) -> None:
        with open(local_path, "rb") as f:
            data = f.read()
        remote_path = self._norm(remote_path)
        self.storage[remote_path] = data

    def posix_rename(self, src: str, dst: str) -> None:
        src = self._norm(src)
        dst = self._norm(dst)
        if src not in self.storage:
            raise IOError(errno.ENOENT, f"No such file: {src}")
        self.storage[dst] = self.storage[src]
        del self.storage[src]

    def open(self, path: str, mode: str = "r"):
        path = self._norm(path)
        if "r" in mode and "+" not in mode:
            if path not in self.storage:
                raise IOError(errno.ENOENT, "No such file")
            content = self.storage[path]
            if content == DIR:
                raise IOError(errno.EISDIR, "Is a directory")
            return _FakeFile(self, path, mode, initial=content)
        # write mode
        return _FakeFile(self, path, mode, initial=b"")

    @staticmethod
    def _norm(path: str) -> str:
        # Treat "/a/b/" same as "/a/b". Don't collapse the root.
        if not path:
            return path
        if path == "/":
            return path
        return path.rstrip("/")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_backend(make_config, config_dir: Path, **overrides) -> SFTPBackend:
    cfg = make_config(
        backend="sftp",
        sftp_host=overrides.pop("sftp_host", "sftp.example.com"),
        sftp_port=overrides.pop("sftp_port", 22),
        sftp_username=overrides.pop("sftp_username", "alice"),
        sftp_key_file=overrides.pop("sftp_key_file", ""),
        sftp_password=overrides.pop("sftp_password", ""),
        sftp_known_hosts_file=overrides.pop(
            "sftp_known_hosts_file", str(config_dir / "known_hosts"),
        ),
        sftp_strict_host_check=overrides.pop("sftp_strict_host_check", False),
        sftp_folder=overrides.pop("sftp_folder", "/srv/project"),
        token_file=str(config_dir / "token.json"),
        **overrides,
    )
    return SFTPBackend(cfg)


def _wire_fake(backend: SFTPBackend, server: Optional[FakeServer] = None) -> FakeServer:
    """Bypass _connect by stuffing pre-built mocks onto the backend.

    Returns the FakeServer so tests can inspect/mutate `server.storage`."""
    if server is None:
        server = FakeServer()
    fake_ssh = MagicMock(name="SSHClient")
    fake_sftp = MagicMock(name="SFTPClient")
    # Wire the real FakeServer methods through the MagicMock so call-counting
    # still works while the side effects are real.
    fake_sftp.stat.side_effect = server.stat
    fake_sftp.listdir_attr.side_effect = server.listdir_attr
    fake_sftp.mkdir.side_effect = server.mkdir
    fake_sftp.rmdir.side_effect = server.rmdir
    fake_sftp.remove.side_effect = server.remove
    fake_sftp.put.side_effect = server.put
    fake_sftp.posix_rename.side_effect = server.posix_rename
    fake_sftp.open.side_effect = server.open
    backend._client = fake_ssh
    # Per-thread SFTP channel cache (post-v0.5.34 fix for paramiko
    # channel-multiplexing bottleneck). Tests still wire ONE shared mock
    # SFTPClient — no real threading happens in unit tests, so a single
    # cache slot for the calling thread suffices.
    backend._tls.sftp = fake_sftp
    # Also point the SSHClient mock at open_sftp so any code path that
    # calls `self._client.open_sftp()` (e.g. a fresh worker thread) gets
    # the same fake SFTPClient back rather than a generic MagicMock.
    fake_ssh.open_sftp.return_value = fake_sftp
    backend._server = server  # for tests
    return server


# ─── 1. authenticate with key succeeds ─────────────────────────────────────────

def test_authenticate_with_key_succeeds(make_config, config_dir, tmp_path, monkeypatch):
    """A configured key file + reachable host → token file written with
    a verified_at marker (no creds persisted)."""
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("dummy-key-bytes")
    backend = _make_backend(make_config, config_dir, sftp_key_file=str(key_path))

    captured: Dict[str, Any] = {}

    def fake_connect_method(self, **kwargs):
        captured.update(kwargs)
        return None

    server = FakeServer()
    fake_sftp = MagicMock()
    fake_sftp.stat.side_effect = server.stat
    fake_sftp.mkdir.side_effect = server.mkdir
    fake_sftp.listdir_attr.side_effect = server.listdir_attr

    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect_method)
    monkeypatch.setattr(paramiko.SSHClient, "open_sftp", lambda self: fake_sftp)
    monkeypatch.setattr(paramiko.SSHClient, "load_host_keys", lambda self, p: None)

    backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    import json
    data = json.loads(token_path.read_text())
    assert "verified_at" in data
    assert data["host"] == "sftp.example.com"
    # Key file resolved to the path on disk (not None).
    assert captured["key_filename"] == str(key_path)
    assert captured["password"] is None  # no password set


# ─── 2. password fallback when key missing ─────────────────────────────────────

def test_authenticate_with_password_falls_back_when_key_missing(
    make_config, config_dir, monkeypatch,
):
    """If sftp_key_file points at a non-existent path, the backend must
    fall back to password auth (key_filename=None)."""
    backend = _make_backend(
        make_config, config_dir,
        sftp_key_file="/nonexistent/nope/key",
        sftp_password="hunter2",
    )

    captured: Dict[str, Any] = {}

    def fake_connect_method(self, **kwargs):
        captured.update(kwargs)
        return None

    server = FakeServer()
    fake_sftp = MagicMock()
    fake_sftp.stat.side_effect = server.stat
    fake_sftp.mkdir.side_effect = server.mkdir

    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect_method)
    monkeypatch.setattr(paramiko.SSHClient, "open_sftp", lambda self: fake_sftp)
    monkeypatch.setattr(paramiko.SSHClient, "load_host_keys", lambda self, p: None)

    backend.authenticate()

    assert captured["key_filename"] is None
    assert captured["password"] == "hunter2"


# ─── 3. bad host key rejected ──────────────────────────────────────────────────

def test_authenticate_rejects_bad_host_key(make_config, config_dir, monkeypatch):
    """If paramiko raises BadHostKeyException, classify_error must
    return AUTH (re-auth required)."""
    backend = _make_backend(make_config, config_dir, sftp_strict_host_check=True)

    fake_key = MagicMock()
    fake_key.get_name.return_value = "ssh-rsa"

    def fake_connect_method(self, **kwargs):
        raise paramiko.ssh_exception.BadHostKeyException(
            "sftp.example.com", fake_key, fake_key,
        )

    monkeypatch.setattr(paramiko.SSHClient, "connect", fake_connect_method)
    monkeypatch.setattr(paramiko.SSHClient, "load_host_keys", lambda self, p: None)

    with pytest.raises(paramiko.ssh_exception.BadHostKeyException) as exc_info:
        backend.authenticate()
    assert backend.classify_error(exc_info.value) == ErrorClass.AUTH


# ─── 4. get_credentials raises if no token ─────────────────────────────────────

def test_get_credentials_raises_when_token_missing(make_config, config_dir):
    """No token file → RuntimeError prompting the user to run auth."""
    backend = _make_backend(make_config, config_dir)
    # Token file path is config_dir/token.json — not created.
    assert not Path(backend.config.token_file).exists()
    with pytest.raises(RuntimeError, match="not authenticated"):
        backend.get_credentials()


# ─── 5. get_or_create_folder creates missing ───────────────────────────────────

def test_get_or_create_folder_creates_missing(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)

    path = backend.get_or_create_folder("newdir", "/srv/project")
    assert path == "/srv/project/newdir"
    assert server.storage["/srv/project/newdir"] == DIR


# ─── 6. get_or_create_folder idempotent ────────────────────────────────────────

def test_get_or_create_folder_idempotent_when_present(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/already"] = DIR

    path = backend.get_or_create_folder("already", "/srv/project")
    assert path == "/srv/project/already"


# ─── 7. resolve_path walks components ──────────────────────────────────────────

def test_resolve_path_walks_components_and_creates_folders(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)

    parent, basename = backend.resolve_path("a/b/c.md", "/srv/project")
    assert parent == "/srv/project/a/b"
    assert basename == "c.md"
    assert server.storage["/srv/project/a"] == DIR
    assert server.storage["/srv/project/a/b"] == DIR


# ─── 8. list_files_recursive returns attr dicts ────────────────────────────────

def test_list_files_recursive_returns_attr_dicts(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/a.md"] = b"alpha"
    server.storage["/srv/project/sub"] = DIR
    server.storage["/srv/project/sub/b.md"] = b"beta-content"

    results = backend.list_files_recursive("/srv/project")
    by_rel = {r["relative_path"]: r for r in results}
    assert "a.md" in by_rel
    assert "sub/b.md" in by_rel
    assert by_rel["a.md"]["id"] == "/srv/project/a.md"
    assert by_rel["a.md"]["size"] == 5
    assert by_rel["a.md"]["md5Checksum"] is None
    assert by_rel["sub/b.md"]["size"] == len(b"beta-content")


# ─── 9. exclude_folder_names is honoured ───────────────────────────────────────

def test_list_files_recursive_honors_exclude_folder_names(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/keep.md"] = b"x"
    server.storage["/srv/project/_claude_mirror_snapshots"] = DIR
    server.storage["/srv/project/_claude_mirror_snapshots/snap.md"] = b"y"

    results = backend.list_files_recursive(
        "/srv/project",
        exclude_folder_names={"_claude_mirror_snapshots"},
    )
    rels = {r["relative_path"] for r in results}
    assert "keep.md" in rels
    assert not any("_claude_mirror_snapshots" in r for r in rels)


# ─── 10. upload_file uses .tmp + posix_rename ──────────────────────────────────

def test_upload_file_uses_atomic_tmp_then_rename(make_config, config_dir, project_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    local = project_dir / "note.md"
    local.write_text("hi-there")

    file_id = backend.upload_file(str(local), "note.md", "/srv/project")

    assert file_id == "/srv/project/note.md"
    assert server.storage["/srv/project/note.md"] == b"hi-there"
    # The .tmp must NOT survive the rename.
    assert "/srv/project/note.md.tmp" not in server.storage
    # posix_rename must have been called.
    backend._tls.sftp.posix_rename.assert_called_once()
    args, _ = backend._tls.sftp.posix_rename.call_args
    assert args[0].endswith(".tmp")
    assert args[1] == "/srv/project/note.md"


# ─── 11. upload_file rolls back .tmp on error ──────────────────────────────────

def test_upload_file_rolls_back_tmp_on_error(make_config, config_dir, project_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    local = project_dir / "note.md"
    local.write_text("hi")

    # Simulate posix_rename failing, the .tmp landed first.
    def boom(src, dst):
        raise IOError(errno.EIO, "I/O error during rename")
    backend._tls.sftp.posix_rename.side_effect = boom

    with pytest.raises(IOError):
        backend.upload_file(str(local), "note.md", "/srv/project")

    # The cleanup path called sftp.remove on the .tmp.
    backend._tls.sftp.remove.assert_called_once_with("/srv/project/note.md.tmp")


# ─── 12. upload_bytes round-trip ───────────────────────────────────────────────

def test_upload_bytes_round_trip(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)

    file_id = backend.upload_bytes(b'{"a":1}', "manifest.json", "/srv/project")
    assert file_id == "/srv/project/manifest.json"
    assert server.storage["/srv/project/manifest.json"] == b'{"a":1}'
    assert "/srv/project/manifest.json.tmp" not in server.storage


# ─── 13. download_file streaming ───────────────────────────────────────────────

def test_download_file_streaming_chunks(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    payload = b"x" * (200 * 1024)  # 200 KiB → triggers multiple 64 KiB chunks
    server.storage["/srv/project/big.bin"] = payload

    out = backend.download_file("/srv/project/big.bin")
    assert out == payload


# ─── 14. download_file aborts on size cap → BackendError ───────────────────────

def test_download_file_aborts_on_size_cap_via_BackendError(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    # Slam the cap down for the test so we don't have to allocate a GiB.
    monkeypatch.setattr(SFTPBackend, "MAX_DOWNLOAD_BYTES", 1024)
    server.storage["/srv/project/huge.bin"] = b"y" * (4 * 1024)

    with pytest.raises(BackendError) as exc_info:
        backend.download_file("/srv/project/huge.bin")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


# ─── 15. get_file_id present ───────────────────────────────────────────────────

def test_get_file_id_returns_path_when_present(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/note.md"] = b"hi"

    file_id = backend.get_file_id("note.md", "/srv/project")
    assert file_id == "/srv/project/note.md"


# ─── 16. get_file_id absent ────────────────────────────────────────────────────

def test_get_file_id_returns_none_when_absent(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)

    assert backend.get_file_id("missing.md", "/srv/project") is None


# ─── 17. copy_file uses server-side cp first ───────────────────────────────────

def test_copy_file_uses_server_side_cp_first(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/src.md"] = b"copyme"
    server.storage["/srv/project/dst"] = DIR

    # Mock exec_command to return an exit status of 0 and have the
    # server-side cp actually populate the destination — this is the
    # fast path that doesn't require a get/put round-trip.
    def fake_exec(cmd, timeout=None):
        # Naively parse `cp -p <src> <dst>` and copy the bytes.
        import shlex as _sh
        parts = _sh.split(cmd)
        if parts[:2] == ["cp", "-p"]:
            src, dst = parts[2], parts[3]
            server.storage[dst] = server.storage.get(src, b"")
        stdin = MagicMock()
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        stderr = MagicMock()
        return stdin, stdout, stderr

    backend._client.exec_command = fake_exec

    new_path = backend.copy_file("/srv/project/src.md", "/srv/project/dst", "src.md")
    assert new_path == "/srv/project/dst/src.md"
    assert server.storage["/srv/project/dst/src.md"] == b"copyme"


# ─── 18. copy_file falls back to get/put ───────────────────────────────────────

def test_copy_file_falls_back_to_get_put_when_exec_fails(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/src.md"] = b"copyme"
    server.storage["/srv/project/dst"] = DIR

    # exec_command returns a non-zero exit status → fallback path.
    def failing_exec(cmd, timeout=None):
        stdin = MagicMock()
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 127  # command not found
        stderr = MagicMock()
        return stdin, stdout, stderr

    backend._client.exec_command = failing_exec

    new_path = backend.copy_file("/srv/project/src.md", "/srv/project/dst", "src.md")
    assert new_path == "/srv/project/dst/src.md"
    # Fallback wrote via the .tmp + rename path.
    assert server.storage["/srv/project/dst/src.md"] == b"copyme"


# ─── 19. get_file_hash uses sha256sum ──────────────────────────────────────────

def test_get_file_hash_via_sha256sum_exec(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)
    digest = "0" * 64

    def fake_exec(cmd, timeout=None):
        stdin = MagicMock()
        stdout = MagicMock()
        stdout.channel.recv_exit_status.return_value = 0
        stdout.read.return_value = (digest + "  /srv/project/note.md\n").encode("utf-8")
        stderr = MagicMock()
        return stdin, stdout, stderr

    backend._client.exec_command = fake_exec

    h = backend.get_file_hash("/srv/project/note.md")
    assert h == digest


# ─── 20. get_file_hash returns None on exec failure ────────────────────────────

def test_get_file_hash_returns_none_when_exec_unavailable(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)

    def boom_exec(cmd, timeout=None):
        raise paramiko.ssh_exception.SSHException("no shell")

    backend._client.exec_command = boom_exec

    assert backend.get_file_hash("/srv/project/note.md") is None


# ─── 21. delete_file removes ───────────────────────────────────────────────────

def test_delete_file_removes(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.storage["/srv/project/note.md"] = b"x"

    backend.delete_file("/srv/project/note.md")
    assert "/srv/project/note.md" not in server.storage


# ─── 22-26. classify_error matrix ──────────────────────────────────────────────

def test_classify_error_AuthenticationException_is_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = paramiko.ssh_exception.AuthenticationException("nope")
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_classify_error_BadHostKey_is_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    fake_key = MagicMock()
    fake_key.get_name.return_value = "ssh-rsa"
    exc = paramiko.ssh_exception.BadHostKeyException("h", fake_key, fake_key)
    assert backend.classify_error(exc) == ErrorClass.AUTH


def test_classify_error_NoValidConnections_is_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = paramiko.ssh_exception.NoValidConnectionsError({("h", 22): OSError("x")})
    assert backend.classify_error(exc) == ErrorClass.TRANSIENT


def test_classify_error_sftp_no_such_file_is_NOT_FOUND(make_config, config_dir):
    """ENOENT (errno 2) maps to FILE_REJECTED — the closest base-class
    enum to 'not found' (skip this one file, push the rest)."""
    backend = _make_backend(make_config, config_dir)
    exc = IOError(2, "No such file")
    assert backend.classify_error(exc) == ErrorClass.FILE_REJECTED


def test_classify_error_quota_string_is_QUOTA(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    exc = IOError("Disk quota exceeded")
    assert backend.classify_error(exc) == ErrorClass.QUOTA
