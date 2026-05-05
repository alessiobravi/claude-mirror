"""Persistent local file-hash cache keyed by (size, mtime_ns).

Avoids re-hashing files whose size and mtime are unchanged since the last
status run — turns hashing into a stat-only check for the common case where
most files haven't been touched.

Each entry holds the local MD5 (used for sync diffing) and, optionally, the
SHA-256 (used by the content-addressed snapshot blob store). SHA-256 is
filled lazily on first snapshot creation; existing 3-element entries from
older versions keep working unchanged.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

CACHE_FILE = ".claude_mirror_hash_cache.json"


class HashCache:
    def __init__(self, project_path: str) -> None:
        self._path = Path(project_path) / CACHE_FILE
        # Entries are stored as one of:
        #   [size, mtime_ns, md5_hex]                   (legacy)
        #   [size, mtime_ns, md5_hex, sha256_hex]       (current)
        self._data: dict[str, list] = {}
        self._dirty = False
        # `set()` is called concurrently from the hashing thread pool while
        # the main thread iterates and calls `save()` — protect both the
        # dict mutation and the dirty flag.
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text())
        except Exception:
            self._data = {}

    def get(self, rel_path: str, size: int, mtime_ns: int) -> str | None:
        """Return the cached MD5 for this file, or None if stale/missing."""
        with self._lock:
            entry = self._data.get(rel_path)
        if not entry or len(entry) < 3:
            return None
        cached_size, cached_mtime, cached_md5 = entry[0], entry[1], entry[2]
        if cached_size == size and cached_mtime == mtime_ns:
            return cached_md5
        return None

    def get_sha256(self, rel_path: str, size: int, mtime_ns: int) -> str | None:
        """Return the cached SHA-256 for this file, or None if missing/stale."""
        with self._lock:
            entry = self._data.get(rel_path)
        if not entry or len(entry) < 4:
            return None
        cached_size, cached_mtime = entry[0], entry[1]
        cached_sha = entry[3]
        if cached_size == size and cached_mtime == mtime_ns and cached_sha:
            return cached_sha
        return None

    def set(self, rel_path: str, size: int, mtime_ns: int, hash_hex: str) -> None:
        """Store the MD5 hash. Preserves any existing SHA-256 entry if the
        file's size+mtime are unchanged; clears it otherwise.
        """
        with self._lock:
            existing = self._data.get(rel_path)
            sha = ""
            if (
                existing
                and len(existing) >= 4
                and existing[0] == size
                and existing[1] == mtime_ns
            ):
                sha = existing[3]
            self._data[rel_path] = [size, mtime_ns, hash_hex, sha] if sha else [size, mtime_ns, hash_hex]
            self._dirty = True

    def set_sha256(
        self, rel_path: str, size: int, mtime_ns: int, sha256_hex: str
    ) -> None:
        """Attach the SHA-256 to an existing entry, or create a new entry
        with an empty MD5 placeholder if none exists yet."""
        with self._lock:
            existing = self._data.get(rel_path)
            if (
                existing
                and len(existing) >= 3
                and existing[0] == size
                and existing[1] == mtime_ns
            ):
                md5 = existing[2]
            else:
                md5 = ""
            self._data[rel_path] = [size, mtime_ns, md5, sha256_hex]
            self._dirty = True

    def prune(self, keep: set[str]) -> None:
        with self._lock:
            before = len(self._data)
            self._data = {k: v for k, v in self._data.items() if k in keep}
            if len(self._data) != before:
                self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = dict(self._data)
            self._dirty = False
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(snapshot))
        os.replace(tmp, self._path)
