"""Per-backend smoke tests for SmbBackend.

`smbprotocol`'s wire-level surface is impractical to mock, so we mock at
`smbclient` (the high-level path-based API the backend actually calls)
via `unittest.mock`. A tiny in-memory `FakeShare` (a `dict[unc_path, bytes
| 'DIR']`) backs the mocked smbclient methods so the tests exercise the
real backend against an offline fake.

All tests stay <100 ms and offline; no network, no filesystem beyond
tmp_path.
"""
from __future__ import annotations

import errno
import io
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Union
from unittest.mock import MagicMock

import pytest

smbprotocol = pytest.importorskip("smbprotocol")
smbclient_mod = pytest.importorskip("smbclient")

from claude_mirror.backends import BackendError, ErrorClass
from claude_mirror.backends.smb import SmbBackend, _COPY_MEMORY_BUDGET

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Fake share ────────────────────────────────────────────────────────────

DIR = "DIR"


class _FakeStat:
    def __init__(self, size: int = 0, is_dir: bool = False, mtime: int = 0):
        self.st_size = size
        self.st_mtime = mtime
        # st_mode is consulted by some helpers; not strictly needed for
        # smbclient's surface (it exposes `is_dir()` / `is_file()`).
        self.st_mode = 0o040755 if is_dir else 0o100644


class _FakeEntry:
    def __init__(self, name: str, path: str, is_dir: bool, size: int = 0):
        self.name = name
        self.path = path
        self._is_dir = is_dir
        self._size = size

    def is_dir(self) -> bool:
        return self._is_dir

    def is_file(self) -> bool:
        return not self._is_dir

    def stat(self) -> _FakeStat:
        return _FakeStat(size=self._size, is_dir=self._is_dir)


class _FakeFile(io.BytesIO):
    """Stand-in for the file object returned by smbclient.open_file."""

    def __init__(self, share: "FakeShare", path: str, mode: str, initial: bytes = b""):
        super().__init__(initial)
        self._share = share
        self._path = path
        self._mode = mode

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if "w" in self._mode or "a" in self._mode:
            self._share.storage[self._path] = self.getvalue()
        self.close()
        return False


class FakeShare:
    """In-memory share keyed by UNC path."""

    def __init__(self) -> None:
        self.storage: Dict[str, Union[bytes, str]] = {
            "\\\\server\\share": DIR,
            "\\\\server\\share\\proj": DIR,
        }

    def stat(self, path: str) -> _FakeStat:
        if path not in self.storage:
            raise OSError(errno.ENOENT, f"No such file: {path}")
        v = self.storage[path]
        if v == DIR:
            return _FakeStat(is_dir=True)
        return _FakeStat(size=len(v), is_dir=False)

    def scandir(self, path: str):
        if path not in self.storage or self.storage[path] != DIR:
            raise OSError(errno.ENOENT, f"No such directory: {path}")
        prefix = path.rstrip("\\") + "\\"
        seen = set()
        for p, v in list(self.storage.items()):
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            if not rest or "\\" in rest:
                continue
            if rest in seen:
                continue
            seen.add(rest)
            yield _FakeEntry(
                name=rest,
                path=p,
                is_dir=(v == DIR),
                size=(0 if v == DIR else len(v)),  # type: ignore[arg-type]
            )

    def makedirs(self, path: str, exist_ok: bool = False) -> None:
        path = path.rstrip("\\")
        if path in self.storage:
            if exist_ok:
                return
            raise OSError(errno.EEXIST, f"Exists: {path}")
        # Create intermediates top-down. Skip the `\\\\server\\share`
        # prefix — we pre-register it in __init__.
        if not path.startswith("\\\\"):
            self.storage[path] = DIR
            return
        # Split into [server, share, segment, ...] (drop the leading \\).
        rest = path[2:].split("\\")
        if len(rest) < 2:
            self.storage[path] = DIR
            return
        cur = "\\\\" + rest[0] + "\\" + rest[1]
        # Pre-existing `\\server\share` is registered by __init__; create
        # any deeper segments.
        for seg in rest[2:]:
            cur = cur + "\\" + seg
            if cur not in self.storage:
                self.storage[cur] = DIR

    def open_file(self, path: str, mode: str = "r") -> _FakeFile:
        if "r" in mode and "+" not in mode:
            if path not in self.storage:
                raise OSError(errno.ENOENT, f"No such file: {path}")
            content = self.storage[path]
            if content == DIR:
                raise OSError(errno.EISDIR, "Is a directory")
            return _FakeFile(self, path, mode, initial=content)  # type: ignore[arg-type]
        return _FakeFile(self, path, mode, initial=b"")

    def remove(self, path: str) -> None:
        if path not in self.storage:
            raise OSError(errno.ENOENT, f"No such file: {path}")
        if self.storage[path] == DIR:
            raise OSError(errno.EISDIR, "Is a directory")
        del self.storage[path]

    def rmdir(self, path: str) -> None:
        if path not in self.storage:
            raise OSError(errno.ENOENT, f"No such directory: {path}")
        if self.storage[path] != DIR:
            raise OSError(errno.ENOTDIR, "Not a directory")
        del self.storage[path]

    def rename(self, src: str, dst: str) -> None:
        if src not in self.storage:
            raise OSError(errno.ENOENT, f"No such file: {src}")
        self.storage[dst] = self.storage[src]
        del self.storage[src]

    def replace(self, src: str, dst: str) -> None:
        # Atomic replace — emulate by removing dst (if any) then renaming.
        if src not in self.storage:
            raise OSError(errno.ENOENT, f"No such file: {src}")
        self.storage[dst] = self.storage[src]
        del self.storage[src]

    def register_session(
        self,
        server: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        port: int = 445,
        encrypt: bool = True,
    ) -> None:
        # Hook the test relies on; the FakeShare itself doesn't track auth state.
        self.last_session_kwargs = dict(
            server=server, username=username, password=password,
            port=port, encrypt=encrypt,
        )


# ─── Helpers ───────────────────────────────────────────────────────────────


def _make_backend(make_config, config_dir: Path, **overrides) -> SmbBackend:
    cfg = make_config(
        backend="smb",
        smb_server=overrides.pop("smb_server", "server"),
        smb_port=overrides.pop("smb_port", 445),
        smb_share=overrides.pop("smb_share", "share"),
        smb_username=overrides.pop("smb_username", "alice"),
        smb_password=overrides.pop("smb_password", "secret"),
        smb_domain=overrides.pop("smb_domain", ""),
        smb_folder=overrides.pop("smb_folder", "proj"),
        smb_encryption=overrides.pop("smb_encryption", True),
        token_file=str(config_dir / "token.json"),
        **overrides,
    )
    return SmbBackend(cfg)


def _wire_fake(backend: SmbBackend, share: Optional[FakeShare] = None,
               monkeypatch: Optional[pytest.MonkeyPatch] = None) -> FakeShare:
    """Patch the lazy-imported `smbclient` module surface that the
    backend uses. Returns the FakeShare so tests can mutate storage."""
    if share is None:
        share = FakeShare()
    fake = MagicMock(name="smbclient")
    fake.scandir.side_effect = share.scandir
    fake.stat.side_effect = share.stat
    fake.makedirs.side_effect = share.makedirs
    fake.open_file.side_effect = share.open_file
    fake.remove.side_effect = share.remove
    fake.rmdir.side_effect = share.rmdir
    fake.rename.side_effect = share.rename
    fake.replace.side_effect = share.replace
    fake.register_session.side_effect = share.register_session

    import claude_mirror.backends.smb as smb_mod
    if monkeypatch is None:
        # When called without monkeypatch, mutate the module directly —
        # tests using this path are responsible for restoration.
        smb_mod._smbclient = lambda: fake  # type: ignore[assignment]
    else:
        monkeypatch.setattr(smb_mod, "_smbclient", lambda: fake)
    backend._fake_smbclient = fake  # for tests
    backend._fake_share = share
    return share


# ─── 1. authenticate happy path ────────────────────────────────────────────

def test_authenticate_happy_path_writes_token(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)

    backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    import json
    data = json.loads(token_path.read_text())
    assert "verified_at" in data
    assert data["server"] == "server"
    assert data["share"] == "share"
    assert data["encryption_requested"] is True
    # register_session was called with the configured creds.
    assert share.last_session_kwargs["username"] == "alice"
    assert share.last_session_kwargs["password"] == "secret"
    assert share.last_session_kwargs["encrypt"] is True
    assert share.last_session_kwargs["port"] == 445


# ─── 2. authenticate folds domain into username ────────────────────────────

def test_authenticate_folds_domain_into_ntlm_username(make_config, config_dir, monkeypatch):
    backend = _make_backend(
        make_config, config_dir,
        smb_domain="CORP", smb_username="alice",
    )
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    backend.authenticate()
    assert share.last_session_kwargs["username"] == "CORP\\alice"


# ─── 3. authenticate bad credentials surfaces classify_error AUTH ──────────

def test_authenticate_logon_failure_classifies_AUTH(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    fake = MagicMock()
    fake.register_session.side_effect = smbprotocol.exceptions.LogonFailure()
    import claude_mirror.backends.smb as smb_mod
    monkeypatch.setattr(smb_mod, "_smbclient", lambda: fake)

    with pytest.raises(smbprotocol.exceptions.LogonFailure) as exc_info:
        backend.authenticate()
    assert backend.classify_error(exc_info.value) == ErrorClass.AUTH


# ─── 4. get_credentials raises if no token ─────────────────────────────────

def test_get_credentials_raises_when_token_missing(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert not Path(backend.config.token_file).exists()
    with pytest.raises(RuntimeError, match="not authenticated"):
        backend.get_credentials()


# ─── 5. get_or_create_folder creates ───────────────────────────────────────

def test_get_or_create_folder_creates_under_unc_root(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    path = backend.get_or_create_folder("newdir", "\\\\server\\share\\proj")
    assert path == "\\\\server\\share\\proj\\newdir"
    assert share.storage[path] == DIR


# ─── 6. resolve_path walks components ──────────────────────────────────────

def test_resolve_path_walks_components_and_creates_folders(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    parent, basename = backend.resolve_path("a/b/c.md", "\\\\server\\share\\proj")
    assert parent == "\\\\server\\share\\proj\\a\\b"
    assert basename == "c.md"
    assert share.storage["\\\\server\\share\\proj\\a"] == DIR
    assert share.storage["\\\\server\\share\\proj\\a\\b"] == DIR


# ─── 7. list_files_recursive ───────────────────────────────────────────────

def test_list_files_recursive_returns_attr_dicts(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    root = "\\\\server\\share\\proj"
    share.storage[f"{root}\\a.md"] = b"alpha"
    share.storage[f"{root}\\sub"] = DIR
    share.storage[f"{root}\\sub\\b.md"] = b"beta"

    results = backend.list_files_recursive(root)
    by_rel = {r["relative_path"]: r for r in results}
    assert "a.md" in by_rel
    assert "sub/b.md" in by_rel
    assert by_rel["a.md"]["id"] == f"{root}\\a.md"
    assert by_rel["a.md"]["size"] == 5
    assert by_rel["a.md"]["md5Checksum"] is None


# ─── 8. exclude_folder_names honoured ──────────────────────────────────────

def test_list_files_recursive_honors_exclude_folder_names(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    root = "\\\\server\\share\\proj"
    share.storage[f"{root}\\keep.md"] = b"x"
    share.storage[f"{root}\\_claude_mirror_snapshots"] = DIR
    share.storage[f"{root}\\_claude_mirror_snapshots\\snap.md"] = b"y"

    results = backend.list_files_recursive(
        root, exclude_folder_names={"_claude_mirror_snapshots"},
    )
    rels = {r["relative_path"] for r in results}
    assert "keep.md" in rels
    assert not any("_claude_mirror_snapshots" in r for r in rels)


# ─── 9. upload_file atomic .tmp + replace ──────────────────────────────────

def test_upload_file_uses_atomic_tmp_then_replace(make_config, config_dir, project_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    local = project_dir / "note.md"
    local.write_text("hi-there")

    file_id = backend.upload_file(str(local), "note.md", "\\\\server\\share\\proj")
    assert file_id == "\\\\server\\share\\proj\\note.md"
    assert share.storage[file_id] == b"hi-there"
    # tmp should not survive.
    assert f"{file_id}.tmp" not in share.storage


# ─── 10. upload_file rolls back on rename failure ──────────────────────────

def test_upload_file_rolls_back_tmp_on_rename_error(make_config, config_dir, project_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    local = project_dir / "note.md"
    local.write_text("hi")

    backend._fake_smbclient.replace.side_effect = OSError(errno.EIO, "I/O error")

    with pytest.raises(OSError):
        backend.upload_file(str(local), "note.md", "\\\\server\\share\\proj")
    backend._fake_smbclient.remove.assert_called_with(
        "\\\\server\\share\\proj\\note.md.tmp"
    )


# ─── 11. upload_bytes round-trip ───────────────────────────────────────────

def test_upload_bytes_round_trip(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    fid = backend.upload_bytes(b'{"a":1}', "manifest.json", "\\\\server\\share\\proj")
    assert fid == "\\\\server\\share\\proj\\manifest.json"
    assert share.storage[fid] == b'{"a":1}'


# ─── 12. download_file streaming ───────────────────────────────────────────

def test_download_file_streaming_chunks(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    payload = b"x" * (200 * 1024)
    share.storage["\\\\server\\share\\proj\\big.bin"] = payload

    out = backend.download_file("\\\\server\\share\\proj\\big.bin")
    assert out == payload


# ─── 13. download_file aborts on size cap ──────────────────────────────────

def test_download_file_aborts_on_size_cap_via_BackendError(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    monkeypatch.setattr(SmbBackend, "MAX_DOWNLOAD_BYTES", 1024)
    share.storage["\\\\server\\share\\proj\\huge.bin"] = b"y" * (4 * 1024)

    with pytest.raises(BackendError) as exc_info:
        backend.download_file("\\\\server\\share\\proj\\huge.bin")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


# ─── 14. get_file_id present / absent ──────────────────────────────────────

def test_get_file_id_returns_path_when_present(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    share.storage["\\\\server\\share\\proj\\note.md"] = b"hi"
    assert backend.get_file_id("note.md", "\\\\server\\share\\proj") \
        == "\\\\server\\share\\proj\\note.md"


def test_get_file_id_returns_none_when_absent(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend, monkeypatch=monkeypatch)
    assert backend.get_file_id("missing.md", "\\\\server\\share\\proj") is None


# ─── 15. copy_file in-memory fallback ──────────────────────────────────────

def test_copy_file_in_memory_for_small_files(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    src = "\\\\server\\share\\proj\\src.md"
    share.storage[src] = b"copyme"
    share.storage["\\\\server\\share\\proj\\dst"] = DIR

    new_path = backend.copy_file(src, "\\\\server\\share\\proj\\dst", "src.md")
    assert new_path == "\\\\server\\share\\proj\\dst\\src.md"
    assert share.storage[new_path] == b"copyme"


# ─── 16. copy_file streaming for large files ──────────────────────────────

def test_copy_file_streams_when_above_memory_budget(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    src = "\\\\server\\share\\proj\\big.bin"
    # Force the streaming branch: temporarily lower the threshold.
    monkeypatch.setattr(
        "claude_mirror.backends.smb._COPY_MEMORY_BUDGET", 16
    )
    payload = b"a" * 64
    share.storage[src] = payload
    share.storage["\\\\server\\share\\proj\\dst"] = DIR

    new_path = backend.copy_file(src, "\\\\server\\share\\proj\\dst", "big.bin")
    assert new_path == "\\\\server\\share\\proj\\dst\\big.bin"
    assert share.storage[new_path] == payload


# ─── 17. get_file_hash streams + returns sha256 hex ────────────────────────

def test_get_file_hash_streams_and_returns_sha256_hex(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    share.storage["\\\\server\\share\\proj\\note.md"] = b"hello"

    import hashlib
    expected = hashlib.sha256(b"hello").hexdigest()
    assert backend.get_file_hash("\\\\server\\share\\proj\\note.md") == expected


def test_get_file_hash_returns_none_on_open_failure(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend, monkeypatch=monkeypatch)
    backend._fake_smbclient.open_file.side_effect = OSError(errno.ENOENT, "missing")
    assert backend.get_file_hash("\\\\server\\share\\proj\\nope.md") is None


# ─── 18. delete_file ───────────────────────────────────────────────────────

def test_delete_file_removes(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    share.storage["\\\\server\\share\\proj\\note.md"] = b"x"
    backend.delete_file("\\\\server\\share\\proj\\note.md")
    assert "\\\\server\\share\\proj\\note.md" not in share.storage


# ─── 19. encryption flag honoured by register_session ──────────────────────

def test_encryption_flag_default_true_passed_to_register_session(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir, smb_encryption=True)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    backend.authenticate()
    assert share.last_session_kwargs["encrypt"] is True


def test_encryption_flag_false_passed_to_register_session(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir, smb_encryption=False)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    backend.authenticate()
    assert share.last_session_kwargs["encrypt"] is False


# ─── 20. classify_error matrix ─────────────────────────────────────────────

def test_classify_error_LogonFailure_is_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(smbprotocol.exceptions.LogonFailure()) == ErrorClass.AUTH


def test_classify_error_AccessDenied_is_PERMISSION(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(smbprotocol.exceptions.AccessDenied()) == ErrorClass.PERMISSION


def test_classify_error_ObjectNameNotFound_is_FILE_REJECTED(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(smbprotocol.exceptions.ObjectNameNotFound()) == ErrorClass.FILE_REJECTED


def test_classify_error_BadNetworkName_is_FILE_REJECTED(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(smbprotocol.exceptions.BadNetworkName()) == ErrorClass.FILE_REJECTED


def test_classify_error_DiskFull_is_QUOTA(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(smbprotocol.exceptions.DiskFull()) == ErrorClass.QUOTA


def test_classify_error_socket_timeout_is_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(socket.timeout("timed out")) == ErrorClass.TRANSIENT


def test_classify_error_connection_reset_is_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(ConnectionResetError("reset")) == ErrorClass.TRANSIENT


def test_classify_error_unknown_exception_is_UNKNOWN(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(ValueError("oops")) == ErrorClass.UNKNOWN


# ─── 21. forward-slash to backslash UNC translation ────────────────────────

def test_to_unc_translates_forward_slash_paths(make_config, config_dir):
    backend = _make_backend(make_config, config_dir, smb_folder="claude/myproject")
    # Internal helper: project root should be a clean backslash UNC.
    assert backend._project_root() == "\\\\server\\share\\claude\\myproject"


# ─── 22. list_folders filters by name ──────────────────────────────────────

def test_list_folders_filters_by_name(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    share = _wire_fake(backend, monkeypatch=monkeypatch)
    parent = "\\\\server\\share\\proj"
    share.storage[f"{parent}\\one"] = DIR
    share.storage[f"{parent}\\two"] = DIR

    results = backend.list_folders(parent, name="one")
    assert len(results) == 1
    assert results[0]["name"] == "one"
