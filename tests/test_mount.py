"""Tests for the read-only FUSE mount engine.

Engine-level only: every test exercises the FS classes directly without
going through ``fuse.FUSE()``. The fusepy module is NOT imported here;
``MirrorFS`` is built with a fallback ``Operations`` base when fusepy
isn't installed.
"""
from __future__ import annotations

import errno
import hashlib
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from claude_mirror._mount import (
    AllSnapshotsFS,
    AsOfDateFS,
    BlobCache,
    LiveFS,
    ManifestEntry,
    MirrorFS,
    PerMirrorFS,
    SnapshotFS,
    _parse_snapshot_timestamp,
    default_cache_root,
    describe_cache,
)


# ─── Helpers ───────────────────────────────────────────────────────────────

def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    p = tmp_path / "cache"
    p.mkdir()
    return p


@pytest.fixture
def cache(cache_dir: Path) -> BlobCache:
    return BlobCache(cache_dir=cache_dir, max_bytes=10 * 1024 * 1024)


class _StubBackend:
    """Minimal StorageBackend stand-in. We don't subclass StorageBackend
    here because we only need the few methods MirrorFS calls."""

    backend_name = "stub"

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}
        self.list_calls: int = 0
        self.list_listing: List[Dict[str, Any]] = []
        self.fail_next_download: bool = False

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Any] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> List[Dict[str, Any]]:
        self.list_calls += 1
        return list(self.list_listing)

    def download_file(self, file_id: str, progress_callback: Any = None) -> bytes:
        if self.fail_next_download:
            self.fail_next_download = False
            raise RuntimeError("injected download failure")
        if file_id not in self.files:
            raise FileNotFoundError(file_id)
        return self.files[file_id]


class _StubSnapshotManager:
    """Stub SnapshotManager exposing the methods the mount engine calls."""

    def __init__(
        self,
        backend: _StubBackend,
        snapshots: Dict[str, Dict[str, Any]],
    ) -> None:
        self.backend = backend
        self.snapshots = snapshots
        self.list_calls: int = 0

    def list(self, _external_progress: Any = None) -> List[Dict[str, Any]]:
        self.list_calls += 1
        items = []
        for ts, snap in self.snapshots.items():
            items.append({
                "timestamp": ts,
                "format": snap.get("format", "blobs"),
                "tag": snap.get("tag"),
            })
        items.sort(key=lambda s: s["timestamp"], reverse=True)
        return items

    def get_snapshot_manifest(
        self,
        timestamp: str,
        backend_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        snap = self.snapshots[timestamp]
        return {
            "format": snap.get("format", "blobs"),
            "timestamp": timestamp,
            "files": dict(snap.get("files", {})),
            "_backend": self.backend,
        }

    def inspect(
        self,
        timestamp: str,
        backend_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        snap = self.snapshots[timestamp]
        files = []
        for path, ident in snap.get("files", {}).items():
            size = snap.get("sizes", {}).get(path, 0)
            entry: Dict[str, Any] = {"path": path, "size": size}
            if snap.get("format", "blobs") == "blobs":
                entry["hash"] = ident
            else:
                entry["id"] = ident
            files.append(entry)
        return {
            "format": snap.get("format", "blobs"),
            "timestamp": timestamp,
            "files": files,
            "metadata": {},
        }

    def get_blob_content(
        self,
        identifier: str,
        backend: Optional[Any] = None,
        format_hint: str = "blobs",
    ) -> bytes:
        # In the stub, blobs are stored under sha256 in backend.files.
        return self.backend.files[identifier]


def _make_blob_snapshot(
    backend: _StubBackend,
    contents: Dict[str, bytes],
) -> Dict[str, Any]:
    files: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    for path, content in contents.items():
        sha = _sha(content)
        files[path] = sha
        sizes[path] = len(content)
        backend.files[sha] = content
    return {"format": "blobs", "files": files, "sizes": sizes}


# ─── BlobCache ────────────────────────────────────────────────────────────

class TestBlobCache:
    def test_get_returns_fetched_bytes_on_miss(self, cache: BlobCache) -> None:
        content = b"hello world"
        sha = _sha(content)
        result = cache.get(sha, fetcher=lambda: content)
        assert result == content

    def test_get_returns_cached_bytes_on_hit(self, cache: BlobCache) -> None:
        content = b"hello world"
        sha = _sha(content)
        cache.get(sha, fetcher=lambda: content)
        # Second call must NOT invoke the fetcher.
        def fetcher_should_not_run() -> bytes:
            raise AssertionError("fetcher invoked on hit")
        result = cache.get(sha, fetcher=fetcher_should_not_run)
        assert result == content

    def test_stats_reports_hits_and_misses(self, cache: BlobCache) -> None:
        content = b"x" * 100
        sha = _sha(content)
        cache.get(sha, fetcher=lambda: content)
        cache.get(sha, fetcher=lambda: content)
        stats = cache.stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["entries"] == 1
        assert stats["bytes"] == 100

    def test_evict_to_size_drops_oldest(self, cache_dir: Path) -> None:
        # Cache limit set well above the total bytes to populate so
        # auto-eviction in `get()` doesn't pre-empt the manual call.
        cache = BlobCache(cache_dir=cache_dir, max_bytes=1_000_000)
        big_a = b"a" * 4000
        big_b = b"b" * 4000
        big_c = b"c" * 4000
        sha_a, sha_b, sha_c = _sha(big_a), _sha(big_b), _sha(big_c)
        cache.get(sha_a, fetcher=lambda: big_a)
        cache.get(sha_b, fetcher=lambda: big_b)
        cache.get(sha_c, fetcher=lambda: big_c)
        # Backdate sha_a strictly to make it the LRU candidate.
        path_a = cache._path_for(sha_a)
        old_atime = time.time() - 10000
        os.utime(path_a, (old_atime, old_atime))
        removed = cache.evict_to_size(8_000)
        assert removed >= 1
        assert not path_a.exists()

    def test_evict_returns_zero_when_under_budget(self, cache: BlobCache) -> None:
        content = b"y" * 50
        cache.get(_sha(content), fetcher=lambda: content)
        assert cache.evict_to_size(10_000) == 0

    def test_corrupted_cache_file_is_refetched(self, cache: BlobCache) -> None:
        content = b"original"
        sha = _sha(content)
        cache.get(sha, fetcher=lambda: content)
        # Corrupt the on-disk blob.
        path = cache._path_for(sha)
        path.write_bytes(b"WRONG-BYTES")
        calls = {"n": 0}
        def refetch() -> bytes:
            calls["n"] += 1
            return content
        result = cache.get(sha, fetcher=refetch)
        assert result == content
        assert calls["n"] == 1

    def test_invalid_sha256_raises(self, cache: BlobCache) -> None:
        with pytest.raises(ValueError):
            cache.get("not-a-hex-digest", fetcher=lambda: b"x")
        with pytest.raises(ValueError):
            cache.get("../etc/passwd", fetcher=lambda: b"x")

    def test_hash_mismatch_raises(self, cache: BlobCache) -> None:
        # Fetcher returns bytes that don't match the declared sha256.
        bogus = "0" * 64
        with pytest.raises(ValueError):
            cache.get(bogus, fetcher=lambda: b"unrelated content")

    def test_default_cache_root_posix(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX-only path")
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        root = default_cache_root()
        assert root == tmp_path / ".cache" / "claude-mirror" / "blobs"

    def test_default_cache_root_xdg(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        if sys.platform == "win32":
            pytest.skip("POSIX-only path")
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        root = default_cache_root()
        assert root == tmp_path / "xdg" / "claude-mirror" / "blobs"

    def test_default_cache_root_windows(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        root = default_cache_root()
        assert root == tmp_path / "local" / "claude-mirror" / "Cache" / "blobs"

    def test_describe_cache_includes_cache_dir(self, cache: BlobCache) -> None:
        info = describe_cache(cache)
        assert info["cache_dir"] == str(cache.cache_dir)
        assert info["max_bytes"] == cache.max_bytes
        assert "entries" in info and "bytes" in info


# ─── SnapshotFS ───────────────────────────────────────────────────────────

class TestSnapshotFS:
    @pytest.fixture
    def fs(self, cache: BlobCache) -> SnapshotFS:
        backend = _StubBackend()
        snap = _make_blob_snapshot(backend, {
            "memory/foo.md": b"foo content here",
            "memory/sub/bar.md": b"bar content",
            "ROADMAP.md": b"# roadmap\n",
        })
        mgr = _StubSnapshotManager(backend, {"2026-05-09T18-04-23Z": snap})
        return SnapshotFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            snapshot_timestamp="2026-05-09T18-04-23Z",
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )

    def test_getattr_root_is_dir(self, fs: SnapshotFS) -> None:
        attrs = fs.getattr("/")
        assert attrs["st_mode"] == fs.DIR_MODE
        assert attrs["st_size"] == 0

    def test_getattr_file_returns_size(self, fs: SnapshotFS) -> None:
        attrs = fs.getattr("/ROADMAP.md")
        assert attrs["st_mode"] == fs.FILE_MODE
        assert attrs["st_size"] == len(b"# roadmap\n")
        assert attrs["st_uid"] == fs._uid
        assert attrs["st_gid"] == fs._gid

    def test_getattr_synthesized_dir(self, fs: SnapshotFS) -> None:
        attrs = fs.getattr("/memory")
        assert attrs["st_mode"] == fs.DIR_MODE

    def test_getattr_nested_synthesized_dir(self, fs: SnapshotFS) -> None:
        attrs = fs.getattr("/memory/sub")
        assert attrs["st_mode"] == fs.DIR_MODE

    def test_getattr_missing_path_raises_enoent(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.getattr("/missing/path.md")
        assert exc_info.value.errno == errno.ENOENT

    def test_readdir_lists_immediate_children(self, fs: SnapshotFS) -> None:
        entries = fs.readdir("/")
        assert "." in entries
        assert ".." in entries
        assert "ROADMAP.md" in entries
        assert "memory" in entries

    def test_readdir_subdir(self, fs: SnapshotFS) -> None:
        entries = fs.readdir("/memory")
        assert "foo.md" in entries
        assert "sub" in entries

    def test_readdir_on_file_raises_enotdir(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.readdir("/ROADMAP.md")
        assert exc_info.value.errno == errno.ENOTDIR

    def test_readdir_missing_raises_enoent(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.readdir("/missing")
        assert exc_info.value.errno == errno.ENOENT

    def test_open_and_read_full_file(self, fs: SnapshotFS) -> None:
        fh = fs.open("/ROADMAP.md", os.O_RDONLY)
        try:
            data = fs.read("/ROADMAP.md", 1024, 0, fh)
        finally:
            fs.release("/ROADMAP.md", fh)
        assert data == b"# roadmap\n"

    def test_read_partial_with_offset(self, fs: SnapshotFS) -> None:
        fh = fs.open("/memory/foo.md", os.O_RDONLY)
        try:
            data = fs.read("/memory/foo.md", 3, 4, fh)
        finally:
            fs.release("/memory/foo.md", fh)
        assert data == b"con"

    def test_read_past_eof_returns_empty(self, fs: SnapshotFS) -> None:
        fh = fs.open("/memory/foo.md", os.O_RDONLY)
        try:
            data = fs.read("/memory/foo.md", 100, 10000, fh)
        finally:
            fs.release("/memory/foo.md", fh)
        assert data == b""

    def test_open_missing_path_raises_enoent(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.open("/no/such.md", os.O_RDONLY)
        assert exc_info.value.errno == errno.ENOENT

    def test_open_directory_raises_eisdir(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.open("/memory", os.O_RDONLY)
        assert exc_info.value.errno == errno.EISDIR

    def test_write_attempt_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.write("/ROADMAP.md", b"x", 0, 0)
        assert exc_info.value.errno == errno.EROFS

    def test_open_for_write_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.open("/ROADMAP.md", os.O_WRONLY)
        assert exc_info.value.errno == errno.EROFS

    def test_create_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.create("/new.md", 0o644)
        assert exc_info.value.errno == errno.EROFS

    def test_unlink_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.unlink("/ROADMAP.md")
        assert exc_info.value.errno == errno.EROFS

    def test_mkdir_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.mkdir("/new-dir", 0o755)
        assert exc_info.value.errno == errno.EROFS

    def test_rename_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.rename("/ROADMAP.md", "/RENAMED.md")
        assert exc_info.value.errno == errno.EROFS

    def test_truncate_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.truncate("/ROADMAP.md", 0)
        assert exc_info.value.errno == errno.EROFS

    def test_chmod_returns_erofs(self, fs: SnapshotFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.chmod("/ROADMAP.md", 0o777)
        assert exc_info.value.errno == errno.EROFS

    def test_statfs_returns_plausible_numbers(self, fs: SnapshotFS) -> None:
        info = fs.statfs("/")
        assert info["f_bsize"] > 0
        assert info["f_blocks"] > 0
        assert info["f_bfree"] >= 0
        assert info["f_bavail"] >= 0
        assert info["f_namemax"] == 255

    def test_release_idempotent_on_unknown_fh(self, fs: SnapshotFS) -> None:
        # Releasing an unknown fh must not raise.
        assert fs.release("/ROADMAP.md", 99999) == 0

    def test_open_caches_blob_via_blobcache(self, fs: SnapshotFS, cache: BlobCache) -> None:
        # First open populates the BlobCache.
        fh1 = fs.open("/ROADMAP.md", os.O_RDONLY)
        fs.release("/ROADMAP.md", fh1)
        stats_after_first = cache.stats()
        assert stats_after_first["misses"] >= 1

        # Second open hits.
        fh2 = fs.open("/ROADMAP.md", os.O_RDONLY)
        fs.release("/ROADMAP.md", fh2)
        stats_after_second = cache.stats()
        assert stats_after_second["hits"] >= 1


def test_empty_snapshot_listed_correctly(cache: BlobCache) -> None:
    backend = _StubBackend()
    snap = _make_blob_snapshot(backend, {})
    mgr = _StubSnapshotManager(backend, {"2026-01-01T00-00-00Z": snap})
    fs = SnapshotFS(
        snapshot_manager=mgr,  # type: ignore[arg-type]
        snapshot_timestamp="2026-01-01T00-00-00Z",
        blob_cache=cache,
        backend=backend,  # type: ignore[arg-type]
    )
    entries = fs.readdir("/")
    assert sorted(entries) == [".", ".."]
    attrs = fs.getattr("/")
    assert attrs["st_size"] == 0


def test_snapshot_full_format_uses_file_id(cache: BlobCache) -> None:
    backend = _StubBackend()
    backend.files["file-id-1"] = b"full-format content"
    mgr = _StubSnapshotManager(backend, {
        "2026-02-02T00-00-00Z": {
            "format": "full",
            "files": {"foo.md": "file-id-1"},
            "sizes": {"foo.md": len(b"full-format content")},
        },
    })
    fs = SnapshotFS(
        snapshot_manager=mgr,  # type: ignore[arg-type]
        snapshot_timestamp="2026-02-02T00-00-00Z",
        blob_cache=cache,
        backend=backend,  # type: ignore[arg-type]
    )
    fh = fs.open("/foo.md", os.O_RDONLY)
    try:
        data = fs.read("/foo.md", 1024, 0, fh)
    finally:
        fs.release("/foo.md", fh)
    assert data == b"full-format content"


def test_parse_snapshot_timestamp_parses_iso_dashes() -> None:
    epoch = _parse_snapshot_timestamp("2026-05-09T18-04-23Z")
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    assert dt.year == 2026 and dt.month == 5 and dt.day == 9
    assert dt.hour == 18 and dt.minute == 4 and dt.second == 23


def test_parse_snapshot_timestamp_invalid_falls_back_to_now() -> None:
    epoch = _parse_snapshot_timestamp("not-a-timestamp")
    assert abs(epoch - time.time()) < 5


# ─── LiveFS ────────────────────────────────────────────────────────────────

class _StubConfig:
    def __init__(self, root_folder: str = "root-id") -> None:
        self.root_folder = root_folder
        self.backend = "stub"
        self.mirror_config_paths: List[str] = []


class TestLiveFS:
    def _make(
        self,
        cache: BlobCache,
        listing: List[Dict[str, Any]],
        ttl: float = 30.0,
    ) -> tuple[LiveFS, _StubBackend]:
        backend = _StubBackend()
        backend.list_listing = listing
        config = _StubConfig()
        fs = LiveFS(
            config=config,  # type: ignore[arg-type]
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
            ttl_seconds=ttl,
        )
        return fs, backend

    def test_initial_listing_loads_in_constructor(self, cache: BlobCache) -> None:
        fs, backend = self._make(cache, [
            {"id": "fid-1", "name": "foo.md", "relative_path": "foo.md", "size": 4},
        ])
        assert backend.list_calls == 1
        entries = fs.readdir("/")
        assert "foo.md" in entries

    def test_ttl_not_expired_uses_cache(self, cache: BlobCache) -> None:
        fs, backend = self._make(cache, [
            {"id": "fid-1", "name": "foo.md", "relative_path": "foo.md", "size": 4},
        ], ttl=60.0)
        baseline = backend.list_calls
        fs.readdir("/")
        fs.readdir("/")
        assert backend.list_calls == baseline

    def test_ttl_expired_refetches(self, cache: BlobCache, monkeypatch: pytest.MonkeyPatch) -> None:
        fs, backend = self._make(cache, [
            {"id": "fid-1", "name": "foo.md", "relative_path": "foo.md", "size": 4},
        ], ttl=0.001)
        baseline = backend.list_calls
        # Force "time" forward by patching the module's time.time call.
        import claude_mirror._mount as mount_module
        future = time.time() + 1000
        monkeypatch.setattr(mount_module.time, "time", lambda: future)
        fs.readdir("/")
        assert backend.list_calls > baseline

    def test_ttl_zero_always_refetches(self, cache: BlobCache) -> None:
        fs, backend = self._make(cache, [
            {"id": "fid-1", "name": "foo.md", "relative_path": "foo.md", "size": 4},
        ], ttl=0)
        baseline = backend.list_calls
        fs.readdir("/")
        fs.readdir("/")
        assert backend.list_calls >= baseline + 2

    def test_read_returns_backend_bytes(self, cache: BlobCache) -> None:
        fs, backend = self._make(cache, [
            {"id": "fid-1", "name": "foo.md", "relative_path": "foo.md", "size": 5},
        ])
        backend.files["fid-1"] = b"hello"
        fh = fs.open("/foo.md", os.O_RDONLY)
        try:
            data = fs.read("/foo.md", 100, 0, fh)
        finally:
            fs.release("/foo.md", fh)
        assert data == b"hello"

    def test_backend_download_failure_surfaces_eio(self, cache: BlobCache) -> None:
        fs, backend = self._make(cache, [
            {"id": "fid-1", "name": "foo.md", "relative_path": "foo.md", "size": 5},
        ])
        backend.files["fid-1"] = b"hello"
        backend.fail_next_download = True
        with pytest.raises(OSError) as exc_info:
            fs.open("/foo.md", os.O_RDONLY)
        assert exc_info.value.errno == errno.EIO

    def test_listing_failure_yields_empty_dir(self, cache: BlobCache) -> None:
        backend = _StubBackend()
        def boom(*a: Any, **kw: Any) -> List[Dict[str, Any]]:
            raise RuntimeError("network down")
        backend.list_files_recursive = boom  # type: ignore[assignment]
        config = _StubConfig()
        fs = LiveFS(
            config=config,  # type: ignore[arg-type]
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
            ttl_seconds=60,
        )
        # Listing failed → empty manifest, but the FS is still usable.
        assert fs.readdir("/") == [".", ".."]

    def test_excludes_snapshot_and_blob_folders(self, cache: BlobCache) -> None:
        # Confirm the mount excludes the snapshot housekeeping folders.
        captured: Dict[str, Any] = {}
        backend = _StubBackend()

        def fake_list(folder_id: str, prefix: str = "",
                      progress_cb: Any = None,
                      exclude_folder_names: Optional[set[str]] = None) -> List[Dict[str, Any]]:
            captured["exclude"] = exclude_folder_names
            return []

        backend.list_files_recursive = fake_list  # type: ignore[assignment]
        config = _StubConfig()
        LiveFS(
            config=config,  # type: ignore[arg-type]
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
            ttl_seconds=60,
        )
        assert "_claude_mirror_snapshots" in captured["exclude"]
        assert "_claude_mirror_blobs" in captured["exclude"]


# ─── PerMirrorFS ──────────────────────────────────────────────────────────

class TestPerMirrorFS:
    def test_unknown_mirror_raises_value_error(
        self, cache: BlobCache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        config = _StubConfig()
        config.mirror_config_paths = []
        with pytest.raises(ValueError) as exc_info:
            PerMirrorFS(
                config=config,  # type: ignore[arg-type]
                blob_cache=cache,
                mirror_backend_name="dropbox",
            )
        assert "dropbox" in str(exc_info.value)

    def test_known_mirror_routes_to_correct_backend(
        self, cache: BlobCache, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Patch Config.load and _construct_backend so we can construct
        # PerMirrorFS without touching any real backend.
        from claude_mirror import _mount as mount_module

        mirror_cfg = MagicMock()
        mirror_cfg.backend = "dropbox"
        mirror_cfg.root_folder = "/mirror-root"
        mirror_cfg.mirror_config_paths = []

        from claude_mirror.config import Config as RealConfig

        def fake_load(path: str, *args: Any, **kwargs: Any) -> Any:
            return mirror_cfg

        monkeypatch.setattr(RealConfig, "load", classmethod(lambda cls, path, **kw: mirror_cfg))

        mirror_backend = _StubBackend()
        mirror_backend.list_listing = [
            {"id": "fid-1", "name": "from-mirror.md", "relative_path": "from-mirror.md", "size": 7},
        ]
        monkeypatch.setattr(mount_module, "_construct_backend", lambda cfg: mirror_backend)

        config = _StubConfig()
        config.mirror_config_paths = [str(tmp_path / "mirror.yaml")]

        fs = PerMirrorFS(
            config=config,  # type: ignore[arg-type]
            blob_cache=cache,
            mirror_backend_name="dropbox",
            ttl_seconds=60,
        )
        assert fs.mirror_backend_name == "dropbox"
        assert fs.backend is mirror_backend
        entries = fs.readdir("/")
        assert "from-mirror.md" in entries


# ─── AllSnapshotsFS ───────────────────────────────────────────────────────

class TestAllSnapshotsFS:
    @pytest.fixture
    def fs(self, cache: BlobCache) -> AllSnapshotsFS:
        backend = _StubBackend()
        snap_old = _make_blob_snapshot(backend, {"foo.md": b"old foo"})
        snap_new = _make_blob_snapshot(backend, {"foo.md": b"new foo", "bar.md": b"bar"})
        mgr = _StubSnapshotManager(backend, {
            "2026-01-01T00-00-00Z": snap_old,
            "2026-05-09T18-04-23Z": snap_new,
        })
        return AllSnapshotsFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )

    def test_root_lists_all_timestamps(self, fs: AllSnapshotsFS) -> None:
        entries = fs.readdir("/")
        assert "2026-01-01T00-00-00Z" in entries
        assert "2026-05-09T18-04-23Z" in entries

    def test_subtree_readdir_delegates(self, fs: AllSnapshotsFS) -> None:
        entries = fs.readdir("/2026-05-09T18-04-23Z")
        assert "foo.md" in entries
        assert "bar.md" in entries

    def test_subtree_read_delegates(self, fs: AllSnapshotsFS) -> None:
        path = "/2026-05-09T18-04-23Z/foo.md"
        fh = fs.open(path, os.O_RDONLY)
        try:
            data = fs.read(path, 1024, 0, fh)
        finally:
            fs.release(path, fh)
        assert data == b"new foo"

    def test_unknown_timestamp_raises_enoent(self, fs: AllSnapshotsFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.readdir("/2030-12-31T00-00-00Z")
        assert exc_info.value.errno == errno.ENOENT

    def test_root_getattr_is_dir(self, fs: AllSnapshotsFS) -> None:
        attrs = fs.getattr("/")
        assert attrs["st_mode"] == fs.DIR_MODE

    def test_timestamp_subdir_getattr_is_dir(self, fs: AllSnapshotsFS) -> None:
        attrs = fs.getattr("/2026-05-09T18-04-23Z")
        assert attrs["st_mode"] == fs.DIR_MODE

    def test_write_returns_erofs(self, fs: AllSnapshotsFS) -> None:
        with pytest.raises(OSError) as exc_info:
            fs.write("/2026-05-09T18-04-23Z/foo.md", b"x", 0, 0)
        assert exc_info.value.errno == errno.EROFS

    def test_lru_evicts_old_subtrees(self, cache: BlobCache) -> None:
        backend = _StubBackend()
        ts_count = AllSnapshotsFS.MAX_OPEN_SUBTREES + 5
        snaps: Dict[str, Any] = {}
        for i in range(ts_count):
            ts = f"2026-{i:02d}-01T00-00-00Z"
            snaps[ts] = _make_blob_snapshot(backend, {f"file-{i}.md": f"content-{i}".encode()})
        mgr = _StubSnapshotManager(backend, snaps)
        fs = AllSnapshotsFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )
        # Touch every timestamp to force materialisation.
        for ts in list(snaps.keys()):
            fs.readdir(f"/{ts}")
        assert len(fs._subtrees) <= AllSnapshotsFS.MAX_OPEN_SUBTREES


# ─── AsOfDateFS ───────────────────────────────────────────────────────────

class TestAsOfDateFS:
    @pytest.fixture
    def setup(self, cache: BlobCache) -> tuple[_StubBackend, _StubSnapshotManager]:
        backend = _StubBackend()
        snap_a = _make_blob_snapshot(backend, {"foo.md": b"v1"})
        snap_b = _make_blob_snapshot(backend, {"foo.md": b"v2"})
        snap_c = _make_blob_snapshot(backend, {"foo.md": b"v3"})
        mgr = _StubSnapshotManager(backend, {
            "2026-01-15T00-00-00Z": snap_a,
            "2026-03-15T00-00-00Z": snap_b,
            "2026-05-15T00-00-00Z": snap_c,
        })
        return backend, mgr

    def test_resolves_to_latest_at_or_before(
        self,
        setup: tuple[_StubBackend, _StubSnapshotManager],
        cache: BlobCache,
    ) -> None:
        backend, mgr = setup
        target = datetime(2026, 4, 1, tzinfo=timezone.utc)
        fs = AsOfDateFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            target_datetime=target,
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )
        assert fs.snapshot_timestamp == "2026-03-15T00-00-00Z"

    def test_naive_datetime_is_treated_as_utc(
        self,
        setup: tuple[_StubBackend, _StubSnapshotManager],
        cache: BlobCache,
    ) -> None:
        backend, mgr = setup
        target = datetime(2026, 6, 1)  # naive
        fs = AsOfDateFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            target_datetime=target,
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )
        assert fs.snapshot_timestamp == "2026-05-15T00-00-00Z"

    def test_pre_history_date_raises_click_exception(
        self,
        setup: tuple[_StubBackend, _StubSnapshotManager],
        cache: BlobCache,
    ) -> None:
        from click import ClickException
        backend, mgr = setup
        target = datetime(2025, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(ClickException):
            AsOfDateFS(
                snapshot_manager=mgr,  # type: ignore[arg-type]
                target_datetime=target,
                blob_cache=cache,
                backend=backend,  # type: ignore[arg-type]
            )

    def test_resolved_fs_behaves_like_snapshot(
        self,
        setup: tuple[_StubBackend, _StubSnapshotManager],
        cache: BlobCache,
    ) -> None:
        backend, mgr = setup
        target = datetime(2026, 4, 1, tzinfo=timezone.utc)
        fs = AsOfDateFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            target_datetime=target,
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )
        fh = fs.open("/foo.md", os.O_RDONLY)
        try:
            data = fs.read("/foo.md", 100, 0, fh)
        finally:
            fs.release("/foo.md", fh)
        assert data == b"v2"

    def test_latest_date_resolves_to_newest_snapshot(
        self,
        setup: tuple[_StubBackend, _StubSnapshotManager],
        cache: BlobCache,
    ) -> None:
        backend, mgr = setup
        target = datetime(2030, 1, 1, tzinfo=timezone.utc)
        fs = AsOfDateFS(
            snapshot_manager=mgr,  # type: ignore[arg-type]
            target_datetime=target,
            blob_cache=cache,
            backend=backend,  # type: ignore[arg-type]
        )
        assert fs.snapshot_timestamp == "2026-05-15T00-00-00Z"


# ─── ManifestEntry ────────────────────────────────────────────────────────

def test_manifest_entry_is_immutable() -> None:
    entry = ManifestEntry(
        rel_path="foo.md",
        identifier="abc",
        size=10,
        mtime=1000.0,
    )
    with pytest.raises(Exception):  # frozen=True → FrozenInstanceError
        entry.rel_path = "bar.md"  # type: ignore[misc]


def test_manifest_entry_default_kind_is_sha256() -> None:
    entry = ManifestEntry(rel_path="foo.md", identifier="abc", size=1, mtime=0.0)
    assert entry.identifier_kind == "sha256"
