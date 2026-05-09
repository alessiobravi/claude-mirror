"""Read-only FUSE mount engine for claude-mirror.

Five mount variants share one BlobCache and one MirrorFS abstract base.
Every variant returns ``EROFS`` for write attempts; reads go through the
content-addressed disk-LRU cache so repeat browses of the same snapshot
do not re-fetch from the backend.

The ``fusepy`` import is deferred so this module is importable without
fusepy installed — tests exercise the FS classes directly via their
public methods rather than going through ``fuse.FUSE()``.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterator,
    Optional,
)

if TYPE_CHECKING:
    from .backends import StorageBackend
    from .config import Config
    from .snapshots import SnapshotManager


# ---------------------------------------------------------------------------
# fusepy lazy-import shim
# ---------------------------------------------------------------------------

# fusepy is an optional dependency: shipped via the `mount` extra. Tests
# must not require it. We provide a minimal stand-in `Operations` base so
# `MirrorFS` is importable + subclassable + testable without fusepy.

_FUSE_LOAD_LOCK = threading.Lock()
_fuse_loaded: bool = False
_FuseOSError_cls: Optional[type[OSError]] = None
_Operations_cls: Optional[type[object]] = None


def _load_fuse() -> tuple[type[OSError], type[object]]:
    """Import fusepy on first use. Returns (FuseOSError, Operations)."""
    global _fuse_loaded, _FuseOSError_cls, _Operations_cls
    if _fuse_loaded and _FuseOSError_cls is not None and _Operations_cls is not None:
        return _FuseOSError_cls, _Operations_cls
    with _FUSE_LOAD_LOCK:
        if _fuse_loaded and _FuseOSError_cls is not None and _Operations_cls is not None:
            return _FuseOSError_cls, _Operations_cls
        try:
            from fuse import FuseOSError, Operations
        except ImportError as exc:
            raise ImportError(
                "fusepy is required for the mount engine. Install with "
                "`pipx install 'claude-mirror[mount]'` or "
                "`pip install fusepy`."
            ) from exc
        _FuseOSError_cls = FuseOSError
        _Operations_cls = Operations
        _fuse_loaded = True
        return FuseOSError, Operations


class _FallbackOperations:
    """Minimal stand-in for ``fuse.Operations`` when fusepy is absent.

    Tests subclass MirrorFS without requiring fusepy. Real mounts go
    through ``_load_fuse()`` and use the real ``Operations`` base.
    """


def _operations_base() -> type[object]:
    """Return the Operations base class — real if fusepy is available,
    else the in-process fallback. Resolved at class-definition time."""
    try:
        from fuse import Operations
    except ImportError:
        return _FallbackOperations
    base: type[object] = Operations
    return base


def _make_fuse_oserror(err_no: int) -> OSError:
    """Construct a ``FuseOSError(errno)`` if fusepy is loaded; otherwise
    fall back to a plain ``OSError(errno, ...)`` so test paths can assert
    on ``.errno`` without requiring fusepy."""
    if _FuseOSError_cls is not None:
        return _FuseOSError_cls(err_no)
    return OSError(err_no, os.strerror(err_no))


# ---------------------------------------------------------------------------
# Default cache root resolution (XDG-compliant, Windows-aware)
# ---------------------------------------------------------------------------

def default_cache_root() -> Path:
    """Resolve the default BlobCache root.

    POSIX: ``$XDG_CACHE_HOME/claude-mirror/blobs/`` if XDG_CACHE_HOME is
    set, else ``~/.cache/claude-mirror/blobs/``.
    Windows: ``%LOCALAPPDATA%\\claude-mirror\\Cache\\blobs\\`` if
    LOCALAPPDATA is set, else ``~/AppData/Local/claude-mirror/Cache/blobs/``.
    """
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "claude-mirror" / "Cache" / "blobs"
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "claude-mirror" / "blobs"


# ---------------------------------------------------------------------------
# BlobCache — content-addressed disk LRU
# ---------------------------------------------------------------------------

class BlobCache:
    """Disk-backed content-addressed LRU.

    Identity == ``sha256(content)``; once a blob is cached, its bytes are
    immutable. Survives mount/unmount cycles. Layout:
    ``<cache_root>/<hash[:2]>/<hash>`` — sharding by first byte keeps the
    directory fan-out tractable on filesystems with O(N) directory scans.
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        max_bytes: int = 500 * 1024 * 1024,
    ) -> None:
        self.cache_dir: Path = cache_dir if cache_dir is not None else default_cache_root()
        self.max_bytes: int = int(max_bytes)
        self._lock = threading.Lock()
        self._hits: int = 0
        self._misses: int = 0
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, sha256: str) -> Path:
        # Validation: hex digest is 64 lowercase chars; refuse anything
        # else to prevent path traversal via attacker-controlled hash.
        if len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256):
            raise ValueError(f"invalid sha256 digest: {sha256!r}")
        return self.cache_dir / sha256[:2] / sha256

    def get(self, sha256: str, fetcher: Callable[[], bytes]) -> bytes:
        """Return cached bytes for ``sha256``, fetching via ``fetcher()``
        on miss. After fetch, verifies the returned bytes hash to the
        declared sha256; mismatches raise ``ValueError`` rather than
        silently caching a corrupt blob.

        Updates the cached file's atime on hit so the LRU eviction order
        reflects access recency.
        """
        path = self._path_for(sha256)
        with self._lock:
            if path.exists():
                try:
                    data = path.read_bytes()
                except OSError:
                    data = b""
                # Defensive re-hash: a blob whose disk bytes don't match
                # its filename is corrupt (bit-rot, partial write,
                # tampering). Re-fetch rather than return bad bytes.
                if data and hashlib.sha256(data).hexdigest() == sha256:
                    self._hits += 1
                    now = time.time()
                    try:
                        os.utime(path, (now, os.path.getmtime(path)))
                    except OSError:
                        pass
                    return data
                # Fall through to re-fetch; remove the corrupt file.
                try:
                    path.unlink()
                except OSError:
                    pass

            self._misses += 1

        # Fetch outside the lock so concurrent fetches for distinct
        # blobs don't serialise on each other.
        data = fetcher()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256:
            raise ValueError(
                f"blob hash mismatch: expected {sha256[:12]}…, got {actual[:12]}…"
            )

        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            except OSError:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
                raise
            self._evict_locked(self.max_bytes)
        return data

    def evict_to_size(self, max_bytes: int) -> int:
        """Evict oldest-atime blobs until total cached bytes is at or
        below ``max_bytes``. Returns the number of files removed."""
        with self._lock:
            return self._evict_locked(max_bytes)

    def _evict_locked(self, max_bytes: int) -> int:
        entries: list[tuple[float, int, Path]] = []
        for shard in self.cache_dir.iterdir() if self.cache_dir.exists() else []:
            if not shard.is_dir():
                continue
            for blob in shard.iterdir():
                try:
                    st = blob.stat()
                except OSError:
                    continue
                entries.append((st.st_atime, st.st_size, blob))
        total = sum(e[1] for e in entries)
        if total <= max_bytes:
            return 0
        entries.sort(key=lambda e: e[0])
        removed = 0
        for atime, size, blob in entries:
            if total <= max_bytes:
                break
            try:
                blob.unlink()
                total -= size
                removed += 1
            except OSError:
                continue
        return removed

    def stats(self) -> dict[str, int]:
        entries = 0
        bytes_total = 0
        if self.cache_dir.exists():
            for shard in self.cache_dir.iterdir():
                if not shard.is_dir():
                    continue
                for blob in shard.iterdir():
                    try:
                        st = blob.stat()
                    except OSError:
                        continue
                    entries += 1
                    bytes_total += st.st_size
        return {
            "entries": entries,
            "bytes": bytes_total,
            "hits": self._hits,
            "misses": self._misses,
        }


# ---------------------------------------------------------------------------
# Manifest entry — internal value type used by every variant
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ManifestEntry:
    """One file in a frozen or live manifest.

    ``identifier`` is sha256 (blobs format / live-after-hashing) OR a
    backend-native file_id (full snapshots / live pre-hashing).
    ``identifier_kind`` distinguishes them so ``_fetch_blob`` knows
    whether to consult ``BlobCache`` or call ``download_file`` directly.
    """

    rel_path: str
    identifier: str
    size: int
    mtime: float
    identifier_kind: str = "sha256"   # "sha256" | "file_id"


# ---------------------------------------------------------------------------
# MirrorFS abstract base
# ---------------------------------------------------------------------------

_OperationsBase: type[object] = _operations_base()


class MirrorFS(_OperationsBase):    # type: ignore[misc,valid-type]
    """POSIX read-only Operations base for every mount variant.

    Subclasses implement ``_resolve_path`` (path → ManifestEntry or DIR)
    and ``_fetch_blob`` (entry → bytes). Common machinery: getattr,
    readdir, open, read, release, statfs, write-rejection.
    """

    FILE_MODE: int = 0o100444
    DIR_MODE: int = 0o040555

    def __init__(
        self,
        blob_cache: BlobCache,
        backend: "StorageBackend",
        default_mtime: Optional[float] = None,
    ) -> None:
        self.blob_cache: BlobCache = blob_cache
        self.backend: "StorageBackend" = backend
        self._default_mtime: float = (
            default_mtime if default_mtime is not None else time.time()
        )
        if os.name == "posix":
            self._uid: int = os.geteuid()
            self._gid: int = os.getegid()
        else:
            self._uid = 0
            self._gid = 0
        # Per-open file-handle table: fh → bytes. Simple integer counter
        # avoids handing fusepy a 0 fh (some kernels treat 0 as invalid).
        self._fh_lock = threading.Lock()
        self._next_fh: int = 1
        self._open_files: dict[int, bytes] = {}

    # ------------------------------------------------------------------
    # Methods subclasses MUST implement
    # ------------------------------------------------------------------

    def _resolve_path(self, path: str) -> Optional[ManifestEntry]:
        """Resolve ``path`` to a ManifestEntry. Return None if ``path``
        is a directory (synthesized or real). Raise FuseOSError(ENOENT)
        if the path does not exist at all."""
        raise NotImplementedError

    def _fetch_blob(self, entry: ManifestEntry) -> bytes:
        """Return the bytes for ``entry``. Subclasses route through
        ``self.blob_cache.get(...)`` for cache-friendliness."""
        raise NotImplementedError

    def _list_dir(self, path: str) -> list[str]:
        """Return immediate child names of ``path`` (no "." / "..").
        Raise FuseOSError(ENOENT) for non-existent paths."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Common path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _normalise(path: str) -> str:
        """Trim leading/trailing slashes, collapse "//", and yield a
        forward-slash relative path. ``"/"`` becomes ``""``."""
        if not path:
            return ""
        norm = path.replace("\\", "/").strip("/")
        # Collapse internal duplicate slashes.
        while "//" in norm:
            norm = norm.replace("//", "/")
        return norm

    def _all_paths(self) -> Iterator[str]:
        """Iterate over every file path the manifest knows about. Used
        by directory synthesis. Subclasses should override if they have
        a more efficient way to walk."""
        return iter([])

    def _is_dir(self, path: str) -> bool:
        """Synthesize directory existence from manifest paths.

        ``""`` (root) is always a directory. Any prefix of a manifest
        rel_path is a directory. The path itself being a manifest entry
        means it is a FILE, not a dir.
        """
        norm = self._normalise(path)
        if norm == "":
            return True
        prefix = norm + "/"
        for rel in self._all_paths():
            if rel == norm:
                return False
            if rel.startswith(prefix):
                return True
        return False

    def _children_of(self, path: str) -> list[str]:
        """Return immediate child names (single level) of dir ``path``."""
        norm = self._normalise(path)
        prefix = norm + "/" if norm else ""
        seen: set[str] = set()
        for rel in self._all_paths():
            if not rel.startswith(prefix) and prefix != "":
                continue
            tail = rel[len(prefix):]
            if not tail:
                continue
            head = tail.split("/", 1)[0]
            if head:
                seen.add(head)
        return sorted(seen)

    # ------------------------------------------------------------------
    # POSIX read methods
    # ------------------------------------------------------------------

    def getattr(self, path: str, fh: Optional[int] = None) -> dict[str, Any]:
        norm = self._normalise(path)
        if norm == "":
            return self._dir_attrs()
        try:
            entry = self._resolve_path("/" + norm)
        except OSError:
            raise
        if entry is None:
            if not self._is_dir(norm):
                raise _make_fuse_oserror(errno.ENOENT)
            return self._dir_attrs()
        return self._file_attrs(entry)

    def _file_attrs(self, entry: ManifestEntry) -> dict[str, Any]:
        return {
            "st_mode": self.FILE_MODE,
            "st_nlink": 1,
            "st_size": int(entry.size),
            "st_uid": self._uid,
            "st_gid": self._gid,
            "st_atime": entry.mtime,
            "st_mtime": entry.mtime,
            "st_ctime": entry.mtime,
        }

    def _dir_attrs(self) -> dict[str, Any]:
        return {
            "st_mode": self.DIR_MODE,
            "st_nlink": 2,
            "st_size": 0,
            "st_uid": self._uid,
            "st_gid": self._gid,
            "st_atime": self._default_mtime,
            "st_mtime": self._default_mtime,
            "st_ctime": self._default_mtime,
        }

    def readdir(self, path: str, fh: Optional[int] = None) -> list[str]:
        norm = self._normalise(path)
        # Reject readdir on a regular file.
        if norm != "":
            try:
                entry = self._resolve_path("/" + norm)
            except OSError:
                raise
            if entry is not None:
                raise _make_fuse_oserror(errno.ENOTDIR)
            if not self._is_dir(norm):
                raise _make_fuse_oserror(errno.ENOENT)
        return [".", ".."] + self._children_of(norm)

    def open(self, path: str, flags: int) -> int:
        # Reject any non-read open. O_RDONLY is the only acceptable mode;
        # O_WRONLY (1) and O_RDWR (2) MUST be denied with EROFS so
        # write-mode opens fail before any data is buffered.
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
        if flags & write_flags:
            raise _make_fuse_oserror(errno.EROFS)
        norm = self._normalise(path)
        if norm == "":
            raise _make_fuse_oserror(errno.EISDIR)
        entry = self._resolve_path("/" + norm)
        if entry is None:
            if self._is_dir(norm):
                raise _make_fuse_oserror(errno.EISDIR)
            raise _make_fuse_oserror(errno.ENOENT)
        try:
            data = self._fetch_blob(entry)
        except OSError:
            raise
        except Exception as exc:
            raise _make_fuse_oserror(errno.EIO) from exc
        with self._fh_lock:
            fh = self._next_fh
            self._next_fh += 1
            self._open_files[fh] = data
        return fh

    def read(self, path: str, size: int, offset: int, fh: int) -> bytes:
        with self._fh_lock:
            data = self._open_files.get(fh)
        if data is None:
            # Open didn't go through us — fall back to a fresh fetch so
            # callers like `cat` that issue read() on an fh of 0 still work.
            norm = self._normalise(path)
            entry = self._resolve_path("/" + norm)
            if entry is None:
                raise _make_fuse_oserror(errno.ENOENT)
            try:
                data = self._fetch_blob(entry)
            except OSError:
                raise
            except Exception as exc:
                raise _make_fuse_oserror(errno.EIO) from exc
        if offset < 0:
            offset = 0
        return data[offset:offset + max(0, int(size))]

    def release(self, path: str, fh: int) -> int:
        with self._fh_lock:
            self._open_files.pop(fh, None)
        return 0

    def statfs(self, path: str) -> dict[str, Any]:
        # Frozen / live manifests have no fixed disk capacity; report
        # the BlobCache budget so `df` shows usage that means something.
        used = sum(e.size for _, e in self._iter_entries())
        block_size = 4096
        total_blocks = max(1, self.blob_cache.max_bytes // block_size)
        used_blocks = max(0, used // block_size)
        free_blocks = max(0, total_blocks - used_blocks)
        return {
            "f_bsize": block_size,
            "f_frsize": block_size,
            "f_blocks": total_blocks,
            "f_bfree": free_blocks,
            "f_bavail": free_blocks,
            "f_files": 0,
            "f_ffree": 0,
            "f_namemax": 255,
        }

    def _iter_entries(self) -> Iterator[tuple[str, ManifestEntry]]:
        """Iterate ``(rel_path, entry)`` for every manifest entry. Used
        by ``statfs``. Default: empty. Subclasses override."""
        return iter(())

    # ------------------------------------------------------------------
    # POSIX write methods — every one returns EROFS
    # ------------------------------------------------------------------

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        raise _make_fuse_oserror(errno.EROFS)

    def create(self, path: str, mode: int, fi: Optional[Any] = None) -> int:
        raise _make_fuse_oserror(errno.EROFS)

    def truncate(self, path: str, length: int, fh: Optional[int] = None) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def unlink(self, path: str) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def rmdir(self, path: str) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def mkdir(self, path: str, mode: int) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def rename(self, old: str, new: str) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def chmod(self, path: str, mode: int) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def chown(self, path: str, uid: int, gid: int) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def symlink(self, target: str, source: str) -> None:
        raise _make_fuse_oserror(errno.EROFS)

    def link(self, target: str, source: str) -> None:
        raise _make_fuse_oserror(errno.EROFS)


# ---------------------------------------------------------------------------
# SnapshotFS — frozen snapshot at a single timestamp
# ---------------------------------------------------------------------------

def _parse_snapshot_timestamp(ts: str) -> float:
    """Best-effort parse of the project's ISO-ish timestamp form
    ``YYYY-MM-DDTHH-MM-SSZ`` to a unix epoch float. Falls back to the
    current time on parse failure — mtime is informational, never load-
    bearing for correctness."""
    try:
        normalised = ts.replace("T", " ").rstrip("Z")
        # The project's snapshot format uses dashes for time too:
        # "2026-05-09T18-04-23Z" → date "2026-05-09", time "18-04-23".
        date_part, _, time_part = normalised.partition(" ")
        time_part = time_part.replace("-", ":")
        if time_part:
            iso = f"{date_part}T{time_part}+00:00"
        else:
            iso = f"{date_part}T00:00:00+00:00"
        return datetime.fromisoformat(iso).timestamp()
    except (ValueError, IndexError):
        return time.time()


class SnapshotFS(MirrorFS):
    """Read-only view of one frozen snapshot. The manifest is loaded
    once at construction time and never re-fetched."""

    def __init__(
        self,
        snapshot_manager: "SnapshotManager",
        snapshot_timestamp: str,
        blob_cache: BlobCache,
        backend: "StorageBackend",
    ) -> None:
        self.snapshot_manager: "SnapshotManager" = snapshot_manager
        self.snapshot_timestamp: str = snapshot_timestamp
        snapshot_mtime = _parse_snapshot_timestamp(snapshot_timestamp)
        super().__init__(blob_cache=blob_cache, backend=backend, default_mtime=snapshot_mtime)

        self._format: str = "blobs"
        self._entries: dict[str, ManifestEntry] = {}
        self._serving_backend: "StorageBackend" = backend
        self._load_manifest()

    def _load_manifest(self) -> None:
        manifest = self.snapshot_manager.get_snapshot_manifest(self.snapshot_timestamp)
        self._format = str(manifest.get("format", "blobs"))
        served_by = manifest.get("_backend") or self.backend
        self._serving_backend = served_by

        files_dict = manifest.get("files") or {}
        # Per-file size is not in the manifest dict from
        # get_snapshot_manifest, so derive from `inspect()` once. Using
        # inspect avoids a download just to read size.
        try:
            view = self.snapshot_manager.inspect(self.snapshot_timestamp)
        except Exception:
            view = {"files": []}
        sizes: dict[str, int] = {}
        for f in view.get("files") or []:
            p = f.get("path")
            if isinstance(p, str):
                sizes[p] = int(f.get("size") or 0)

        kind = "sha256" if self._format == "blobs" else "file_id"
        for rel_path, identifier in files_dict.items():
            if not isinstance(rel_path, str) or not isinstance(identifier, str):
                continue
            self._entries[rel_path] = ManifestEntry(
                rel_path=rel_path,
                identifier=identifier,
                size=sizes.get(rel_path, 0),
                mtime=self._default_mtime,
                identifier_kind=kind,
            )

    def _all_paths(self) -> Iterator[str]:
        return iter(self._entries.keys())

    def _iter_entries(self) -> Iterator[tuple[str, ManifestEntry]]:
        return iter(self._entries.items())

    def _resolve_path(self, path: str) -> Optional[ManifestEntry]:
        norm = self._normalise(path)
        if norm == "":
            return None
        return self._entries.get(norm)

    def _fetch_blob(self, entry: ManifestEntry) -> bytes:
        if entry.identifier_kind == "sha256":
            return self.blob_cache.get(
                entry.identifier,
                fetcher=lambda: self.snapshot_manager.get_blob_content(
                    entry.identifier,
                    backend=self._serving_backend,
                    format_hint="blobs",
                ),
            )
        # Full-format snapshots: backend-native file_id, not content-
        # addressed. Cache under the freshly-computed sha256 of the body
        # so identical content across full snapshots dedups in-cache.
        raw = self._serving_backend.download_file(entry.identifier)
        sha = hashlib.sha256(raw).hexdigest()
        return self.blob_cache.get(sha, fetcher=lambda: raw)


# ---------------------------------------------------------------------------
# LiveFS — backend-as-of-now with TTL invalidation
# ---------------------------------------------------------------------------

@dataclass
class _LiveDirCache:
    listing: dict[str, ManifestEntry] = field(default_factory=dict)
    fetched_at: float = 0.0


class LiveFS(MirrorFS):
    """Live view of the backend with a TTL-invalidated directory cache.

    The constructor lists the backend once; subsequent reads consult the
    cached listing if it hasn't expired (``ttl_seconds``) and re-list
    otherwise. Per-blob bytes go through ``BlobCache`` keyed by the
    sha256 of the most recent body so that re-listing doesn't blow away
    the on-disk blob cache.
    """

    def __init__(
        self,
        config: "Config",
        blob_cache: BlobCache,
        backend: "StorageBackend",
        ttl_seconds: float = 30.0,
    ) -> None:
        super().__init__(blob_cache=blob_cache, backend=backend, default_mtime=time.time())
        self.config: "Config" = config
        self.ttl_seconds: float = float(ttl_seconds)
        self._dir_cache: _LiveDirCache = _LiveDirCache()
        self._dir_cache_lock = threading.Lock()
        # Map rel_path → last seen sha256, so we can re-use the BlobCache
        # entry without re-hashing on every fetch.
        self._content_hashes: dict[str, str] = {}
        self._refresh_listing()

    # ------------------------------------------------------------------
    # Directory listing with TTL
    # ------------------------------------------------------------------

    def _root_folder(self) -> str:
        rf = getattr(self.config, "root_folder", "")
        if isinstance(rf, str) and rf:
            return rf
        # Some Config builds (older test configs) expose only field-level
        # backend-specific attrs.
        for attr in ("drive_folder_id", "dropbox_folder", "onedrive_folder",
                     "webdav_url", "sftp_folder"):
            v = getattr(self.config, attr, "")
            if isinstance(v, str) and v:
                return v
        return ""

    def _refresh_listing(self) -> None:
        try:
            entries = self.backend.list_files_recursive(
                self._root_folder(),
                exclude_folder_names={
                    "_claude_mirror_snapshots",
                    "_claude_mirror_blobs",
                    "_claude_mirror_logs",
                },
            )
        except Exception:
            with self._dir_cache_lock:
                self._dir_cache = _LiveDirCache(
                    listing={},
                    fetched_at=time.time(),
                )
            return

        listing: dict[str, ManifestEntry] = {}
        for raw in entries:
            rel = raw.get("relative_path") or raw.get("name") or ""
            if not isinstance(rel, str) or not rel:
                continue
            file_id = raw.get("id")
            if not isinstance(file_id, str) or not file_id:
                continue
            size = int(raw.get("size") or 0)
            mtime_val = raw.get("modifiedTime") or raw.get("mtime") or self._default_mtime
            try:
                if isinstance(mtime_val, (int, float)):
                    mtime = float(mtime_val)
                else:
                    mtime = datetime.fromisoformat(
                        str(mtime_val).replace("Z", "+00:00")
                    ).timestamp()
            except (ValueError, TypeError):
                mtime = self._default_mtime
            listing[rel] = ManifestEntry(
                rel_path=rel,
                identifier=file_id,
                size=size,
                mtime=mtime,
                identifier_kind="file_id",
            )
        with self._dir_cache_lock:
            self._dir_cache = _LiveDirCache(
                listing=listing,
                fetched_at=time.time(),
            )

    def _maybe_refresh(self) -> None:
        with self._dir_cache_lock:
            age = time.time() - self._dir_cache.fetched_at
            stale = self.ttl_seconds <= 0 or age >= self.ttl_seconds
        if stale:
            self._refresh_listing()

    def _current_listing(self) -> dict[str, ManifestEntry]:
        self._maybe_refresh()
        with self._dir_cache_lock:
            return dict(self._dir_cache.listing)

    def _all_paths(self) -> Iterator[str]:
        return iter(self._current_listing().keys())

    def _iter_entries(self) -> Iterator[tuple[str, ManifestEntry]]:
        return iter(self._current_listing().items())

    def _resolve_path(self, path: str) -> Optional[ManifestEntry]:
        norm = self._normalise(path)
        if norm == "":
            return None
        return self._current_listing().get(norm)

    def _fetch_blob(self, entry: ManifestEntry) -> bytes:
        # Live entries are keyed by file_id, not content hash, because
        # the same file_id can change content over the file's lifetime.
        # Strategy: download once, compute sha256, then route through
        # BlobCache so the on-disk cache stays content-addressed (and
        # the previous hash for this path stays cached until LRU evicts
        # it — readers holding an open fh keep their old bytes).
        try:
            data = self.backend.download_file(entry.identifier)
        except OSError:
            raise
        except Exception as exc:
            raise _make_fuse_oserror(errno.EIO) from exc
        sha = hashlib.sha256(data).hexdigest()
        self.blob_cache.get(sha, fetcher=lambda: data)
        self._content_hashes[entry.rel_path] = sha
        return data


# ---------------------------------------------------------------------------
# PerMirrorFS — LiveFS scoped to a specific Tier 2 mirror
# ---------------------------------------------------------------------------

class PerMirrorFS(LiveFS):
    """Live view scoped to one named Tier 2 mirror backend.

    The mirror's backend instance is constructed from the matching
    config in ``config.mirror_config_paths``; if the named mirror is
    not configured for this project, the constructor raises
    ``ValueError`` with the available names.
    """

    def __init__(
        self,
        config: "Config",
        blob_cache: BlobCache,
        mirror_backend_name: str,
        ttl_seconds: float = 30.0,
    ) -> None:
        mirror_config, mirror_backend = self._resolve_mirror(config, mirror_backend_name)
        super().__init__(
            config=mirror_config,
            blob_cache=blob_cache,
            backend=mirror_backend,
            ttl_seconds=ttl_seconds,
        )
        self.mirror_backend_name: str = mirror_backend_name

    @staticmethod
    def _resolve_mirror(
        config: "Config",
        mirror_backend_name: str,
    ) -> tuple["Config", "StorageBackend"]:
        from .config import Config as _Config

        available: list[str] = []
        for path in getattr(config, "mirror_config_paths", []) or []:
            try:
                cfg = _Config.load(path)
            except Exception:
                continue
            name = getattr(cfg, "backend", "") or ""
            available.append(name)
            if name == mirror_backend_name:
                backend = _construct_backend(cfg)
                return cfg, backend
        raise ValueError(
            f"mirror backend {mirror_backend_name!r} not configured for "
            f"this project (available mirrors: {', '.join(available) or 'none'})"
        )


def _construct_backend(config: "Config") -> "StorageBackend":
    """Build a backend instance from a Config. Mirrors the dispatch
    table the CLI uses, but local to this module so MOUNT does not
    import from cli.py."""
    backend_name = getattr(config, "backend", "")
    if backend_name == "googledrive":
        from .backends.googledrive import GoogleDriveBackend
        return GoogleDriveBackend(config)
    if backend_name == "dropbox":
        from .backends.dropbox import DropboxBackend
        return DropboxBackend(config)
    if backend_name == "onedrive":
        from .backends.onedrive import OneDriveBackend
        return OneDriveBackend(config)
    if backend_name == "webdav":
        from .backends.webdav import WebDAVBackend
        return WebDAVBackend(config)
    if backend_name == "sftp":
        from .backends.sftp import SFTPBackend
        return SFTPBackend(config)
    raise ValueError(f"Unknown storage backend: {backend_name!r}")


# ---------------------------------------------------------------------------
# AllSnapshotsFS — every snapshot under /<TIMESTAMP>/...
# ---------------------------------------------------------------------------

class AllSnapshotsFS(MirrorFS):
    """Synthetic root listing every snapshot timestamp as a directory.

    Each subtree is a lazily-constructed ``SnapshotFS``. The class
    bounds memory by keeping at most ``MAX_OPEN_SUBTREES`` subtree FS
    instances live at any one time; older ones are evicted in LRU
    order. The frozen-snapshot manifest is small enough that re-loading
    on the next access is cheap.
    """

    MAX_OPEN_SUBTREES: int = 32

    def __init__(
        self,
        snapshot_manager: "SnapshotManager",
        blob_cache: BlobCache,
        backend: "StorageBackend",
    ) -> None:
        super().__init__(blob_cache=blob_cache, backend=backend, default_mtime=time.time())
        self.snapshot_manager: "SnapshotManager" = snapshot_manager
        self._timestamps: list[str] = []
        self._subtrees: "OrderedDict[str, SnapshotFS]" = OrderedDict()
        self._subtrees_lock = threading.Lock()
        self._load_index()

    def _load_index(self) -> None:
        try:
            snaps = self.snapshot_manager.list()
        except Exception:
            snaps = []
        seen: set[str] = set()
        ordered: list[str] = []
        for s in snaps:
            ts = s.get("timestamp", "")
            if isinstance(ts, str) and ts and ts not in seen:
                seen.add(ts)
                ordered.append(ts)
        self._timestamps = ordered

    def _split(self, path: str) -> tuple[str, str]:
        norm = self._normalise(path)
        if norm == "":
            return "", ""
        head, _, rest = norm.partition("/")
        return head, rest

    def _subtree_for(self, timestamp: str) -> "SnapshotFS":
        if timestamp not in self._timestamps:
            raise _make_fuse_oserror(errno.ENOENT)
        with self._subtrees_lock:
            existing = self._subtrees.get(timestamp)
            if existing is not None:
                self._subtrees.move_to_end(timestamp)
                return existing
        # Construct outside the lock — the manifest fetch may be slow.
        try:
            sub = SnapshotFS(
                snapshot_manager=self.snapshot_manager,
                snapshot_timestamp=timestamp,
                blob_cache=self.blob_cache,
                backend=self.backend,
            )
        except Exception as exc:
            raise _make_fuse_oserror(errno.EIO) from exc
        with self._subtrees_lock:
            self._subtrees[timestamp] = sub
            self._subtrees.move_to_end(timestamp)
            while len(self._subtrees) > self.MAX_OPEN_SUBTREES:
                self._subtrees.popitem(last=False)
        return sub

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _all_paths(self) -> Iterator[str]:
        # Two-level: root-level "directories" (one per timestamp). Real
        # files live under each subtree; we only emit timestamps here so
        # the synthesized-dir helpers in MirrorFS work at the root.
        return iter([f"{ts}/" for ts in self._timestamps])

    def _is_dir(self, path: str) -> bool:
        norm = self._normalise(path)
        if norm == "":
            return True
        head, rest = self._split(norm)
        if head not in self._timestamps:
            return False
        if rest == "":
            return True
        sub = self._subtree_for(head)
        return sub._is_dir(rest)

    def _children_of(self, path: str) -> list[str]:
        norm = self._normalise(path)
        if norm == "":
            return list(self._timestamps)
        head, rest = self._split(norm)
        if head not in self._timestamps:
            return []
        sub = self._subtree_for(head)
        return sub._children_of(rest)

    def _resolve_path(self, path: str) -> Optional[ManifestEntry]:
        norm = self._normalise(path)
        if norm == "":
            return None
        head, rest = self._split(norm)
        if head not in self._timestamps:
            raise _make_fuse_oserror(errno.ENOENT)
        if rest == "":
            return None
        sub = self._subtree_for(head)
        return sub._resolve_path("/" + rest)

    def _fetch_blob(self, entry: ManifestEntry) -> bytes:
        # The underlying SnapshotFS owns the fetch; AllSnapshotsFS does
        # not see a fetch unless _resolve_path returned an entry, which
        # is only possible after a subtree has materialised.
        for sub in list(self._subtrees.values()):
            if sub._entries.get(entry.rel_path) is entry:
                return sub._fetch_blob(entry)
        # Stale-entry fallback: the subtree backing this entry has been
        # LRU-evicted between path resolution and read. The caller will
        # retry (re-resolve), so EIO is the right signal.
        raise _make_fuse_oserror(errno.EIO)

    def _iter_entries(self) -> Iterator[tuple[str, ManifestEntry]]:
        # statfs traversal — sum across loaded subtrees only. Cold
        # subtrees aren't worth the round-trip here.
        for ts, sub in list(self._subtrees.items()):
            for rel, entry in sub._entries.items():
                yield f"{ts}/{rel}", entry


# ---------------------------------------------------------------------------
# AsOfDateFS — time-travel mount that resolves to a single snapshot
# ---------------------------------------------------------------------------

class AsOfDateFS(SnapshotFS):
    """Mount that resolves the user-supplied date to the latest
    snapshot ``<= target_datetime`` and then behaves like a
    ``SnapshotFS`` at that timestamp."""

    def __init__(
        self,
        snapshot_manager: "SnapshotManager",
        target_datetime: datetime,
        blob_cache: BlobCache,
        backend: "StorageBackend",
    ) -> None:
        from click import ClickException

        if target_datetime.tzinfo is None:
            target_datetime = target_datetime.replace(tzinfo=timezone.utc)
        self.target_datetime: datetime = target_datetime

        try:
            snaps = snapshot_manager.list()
        except Exception:
            snaps = []
        candidates: list[tuple[float, str]] = []
        for s in snaps:
            ts = s.get("timestamp", "")
            if not isinstance(ts, str) or not ts:
                continue
            ts_epoch = _parse_snapshot_timestamp(ts)
            ts_dt = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
            if ts_dt <= target_datetime:
                candidates.append((ts_epoch, ts))
        if not candidates:
            available = sorted({s.get("timestamp", "") for s in snaps if s.get("timestamp")})
            hint = (
                "Available snapshots: " + ", ".join(available)
                if available
                else "No snapshots exist on remote yet."
            )
            raise ClickException(
                f"No snapshot at or before {target_datetime.isoformat()}. {hint}"
            )
        candidates.sort()
        _, resolved_timestamp = candidates[-1]

        super().__init__(
            snapshot_manager=snapshot_manager,
            snapshot_timestamp=resolved_timestamp,
            blob_cache=blob_cache,
            backend=backend,
        )


# ---------------------------------------------------------------------------
# Convenience: dump cache directory location for `claude-mirror cache info`
# ---------------------------------------------------------------------------

def describe_cache(cache: BlobCache) -> dict[str, Any]:
    """Return a JSON-serialisable dict summarising a BlobCache instance."""
    info = cache.stats()
    info_out: dict[str, Any] = dict(info)
    info_out["cache_dir"] = str(cache.cache_dir)
    info_out["max_bytes"] = cache.max_bytes
    return info_out


__all__ = [
    "BlobCache",
    "ManifestEntry",
    "MirrorFS",
    "SnapshotFS",
    "LiveFS",
    "PerMirrorFS",
    "AllSnapshotsFS",
    "AsOfDateFS",
    "default_cache_root",
    "describe_cache",
]
