"""Snapshot subsystem.

Two on-remote formats coexist; each project picks one via `config.snapshot_format`:

  full
    Every snapshot is a server-side copy of the entire project tree into
    `_claude_mirror_snapshots/{timestamp}/`, plus a `_snapshot_meta.json`
    sidecar inside that folder. Simple, but each snapshot costs O(files)
    storage even when nothing changed.

  blobs
    Content-addressed: every unique file content is uploaded exactly once
    to `_claude_mirror_blobs/{hash[:2]}/{hash}` (SHA-256 of the file body).
    Each snapshot is a single JSON manifest at
    `_claude_mirror_snapshots/{timestamp}.json` mapping rel_path -> hash.
    Identical files across snapshots share the same blob, so a snapshot
    after a small change costs ~the size of that change. Run
    `claude-mirror gc` to reclaim space from blobs no longer referenced by
    any snapshot.

`restore()` and `list()` accept BOTH formats simultaneously, so switching
the per-project format never makes older snapshots inaccessible. Use
`claude-mirror migrate-snapshots --to {blobs,full}` to convert in-place.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.table import Table

from ._progress import make_phase_progress
from .backends import StorageBackend


# Confirm hook for security-sensitive prompts during restore (e.g. when
# the primary backend is unreachable and a mirror is about to serve the
# snapshot — the mirror's contents cannot be cross-checked against the
# primary, so blindly trusting it lets a malicious mirror substitute a
# different blob set under the same timestamp). The CLI installs a hook
# that calls click.confirm; library callers (tests, programmatic use)
# get the default no-op-True so they aren't blocked on stdin.
_CONFIRM_HOOK: Callable[[str], bool] = lambda msg: True


def set_confirm_hook(fn: Callable[[str], bool]) -> None:
    """Install a confirm-callback used by `SnapshotManager.restore` for
    high-trust prompts. Pass a function taking a message string and
    returning bool. Default no-op accepts everything."""
    global _CONFIRM_HOOK
    _CONFIRM_HOOK = fn
from .config import Config
from .hash_cache import HashCache

console = Console(force_terminal=True)

from ._constants import PARALLEL_WORKERS

SNAPSHOTS_FOLDER = "_claude_mirror_snapshots"
BLOBS_FOLDER = "_claude_mirror_blobs"
SNAPSHOT_META_FILE = "_snapshot_meta.json"          # per-folder sidecar (full format)

MANIFEST_FORMAT_VERSION = "v2"
MANIFEST_SUFFIX = ".json"


def _safe_join(base: Path, rel_path: str) -> Path:
    """Resolve `base / rel_path` and assert the result is contained in `base`.
    Defends against path-traversal in untrusted backend metadata.
    Raises ValueError if rel_path attempts to escape the destination root.
    """
    base_resolved = base.resolve()
    target = (base / rel_path).resolve()
    try:
        target.relative_to(base_resolved)
    except ValueError:
        raise ValueError(
            f"Refusing to write outside destination directory: {rel_path!r}"
        )
    return target


def _sha256_file(path: str) -> str:
    """Streaming SHA-256 of a local file (Python 3.11+ C-level hot-path)."""
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _blob_rel_path(sha256_hex: str) -> str:
    """Storage-relative path inside the blobs folder for a given SHA-256."""
    return f"{sha256_hex[:2]}/{sha256_hex}"


def _human_size(n: int) -> str:
    """Render a byte count as a short human-readable string (e.g. '4.2K', '7.1M').
    Backends that don't expose a size for a file pass 0 — the caller is
    expected to check for that before calling."""
    if n < 1024:
        return f"{n} B"
    for unit in ("K", "M", "G", "T"):
        n /= 1024.0
        if n < 1024 or unit == "T":
            return f"{n:.1f} {unit}B"
    return f"{n:.1f} TB"


@dataclass
class SnapshotMeta:
    timestamp: str
    triggered_by: str   # "user@machine"
    action: str         # "push" | "sync"
    files_changed: list[str]
    total_files: int


class SnapshotManager:
    def __init__(
        self,
        config: Config,
        storage: StorageBackend,
        mirrors: Optional[list[StorageBackend]] = None,
    ) -> None:
        self.config = config
        self.storage = storage          # primary
        self._mirrors: list[StorageBackend] = list(mirrors or [])
        self._snapshots_folder_id: Optional[str] = None
        self._blobs_folder_id: Optional[str] = None
        # Tier 2: per-mirror folder ID caches so we don't re-resolve every call.
        self._mirror_snapshots_folder_ids: dict[str, str] = {}
        self._mirror_blobs_folder_ids: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Folder helpers
    # ------------------------------------------------------------------

    def _backend_key(self, backend: StorageBackend) -> str:
        """Stable per-backend cache key. Prefers the class-level
        `backend_name` attribute (Tier 2 contract); falls back to id()."""
        return getattr(backend, "backend_name", None) or f"id:{id(backend)}"

    def _root_folder_for(self, backend: StorageBackend) -> str:
        """Resolve the root folder reference for `backend`. The primary
        uses `self.config.root_folder`; for a mirror we prefer the
        mirror's OWN config (mirror.config.root_folder, which dispatches
        on backend type to return drive_folder_id / sftp_folder /
        dropbox_folder / etc.). Falls back to legacy attribute lookups
        for mirrors constructed without an embedded config.

        Bug history: pre-fix, this method only checked `getattr(backend,
        "root_folder", ...)` and fell through to `self.config.root_folder`
        when missing. SFTPBackend (and anything else not exposing
        `.root_folder` as a top-level attribute) silently inherited the
        PRIMARY's root_folder — a Drive folder ID — as its parent path.
        Snapshot fan-out then called e.g. sftp.mkdir("1BxiMVs.../...")
        which either silently failed or created garbage paths under the
        user's home directory, leaving the mirror's actual project
        folder without any `_claude_mirror_snapshots/` or
        `_claude_mirror_blobs/` directories.
        """
        if backend is self.storage:
            return self.config.root_folder
        # Preferred path — the mirror carries its own Config and that
        # Config's `root_folder` property knows the backend-specific
        # field to read (sftp_folder, drive_folder_id, etc.).
        backend_config = getattr(backend, "config", None)
        if backend_config is not None:
            rf = getattr(backend_config, "root_folder", None)
            if isinstance(rf, str) and rf:
                return rf
        # Legacy fallback for backends constructed without an embedded
        # config (test fakes, custom subclasses).
        rf = getattr(backend, "root_folder", None)
        if callable(rf):
            try:
                return rf()
            except Exception:
                pass
        elif isinstance(rf, str) and rf:
            return rf
        # Last resort — primary's root_folder. Reaching here means the
        # mirror has no usable config AND no `root_folder` attribute,
        # which is an unusual setup; the snapshot fan-out will likely
        # write to a wrong path on this backend.
        return self.config.root_folder

    def _get_snapshots_folder_for(self, backend: StorageBackend) -> str:
        """Resolve (and cache) the `_claude_mirror_snapshots` folder ID for
        a given backend — primary or mirror."""
        if backend is self.storage:
            if not self._snapshots_folder_id:
                self._snapshots_folder_id = self.storage.get_or_create_folder(
                    SNAPSHOTS_FOLDER, self.config.root_folder
                )
            return self._snapshots_folder_id
        key = self._backend_key(backend)
        cached = self._mirror_snapshots_folder_ids.get(key)
        if cached:
            return cached
        folder_id = backend.get_or_create_folder(
            SNAPSHOTS_FOLDER, self._root_folder_for(backend)
        )
        self._mirror_snapshots_folder_ids[key] = folder_id
        return folder_id

    def _get_blobs_folder_for(self, backend: StorageBackend) -> str:
        """Resolve (and cache) the `_claude_mirror_blobs` folder ID for
        a given backend — primary or mirror."""
        if backend is self.storage:
            if not self._blobs_folder_id:
                self._blobs_folder_id = self.storage.get_or_create_folder(
                    BLOBS_FOLDER, self.config.root_folder
                )
            return self._blobs_folder_id
        key = self._backend_key(backend)
        cached = self._mirror_blobs_folder_ids.get(key)
        if cached:
            return cached
        folder_id = backend.get_or_create_folder(
            BLOBS_FOLDER, self._root_folder_for(backend)
        )
        self._mirror_blobs_folder_ids[key] = folder_id
        return folder_id

    def _get_snapshots_folder(self) -> str:
        # Back-compat shim: defaults to primary.
        return self._get_snapshots_folder_for(self.storage)

    def _get_blobs_folder(self) -> str:
        # Back-compat shim: defaults to primary.
        return self._get_blobs_folder_for(self.storage)

    # ------------------------------------------------------------------
    # Local-file discovery (replicated from SyncEngine to avoid a cycle)
    # ------------------------------------------------------------------

    def _is_excluded(self, rel_path: str) -> bool:
        for pattern in self.config.exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            if fnmatch.fnmatch(rel_path, f"{pattern}/*") or rel_path.startswith(f"{pattern}/"):
                return True
        return False

    def _iter_local_files(self) -> list[tuple[str, Path]]:
        """Return sorted list of (rel_path, abs_path) for all local files
        matching configured patterns, excluding the manifest and any
        excluded patterns."""
        project = Path(self.config.project_path)
        found: dict[str, Path] = {}
        for pattern in self.config.file_patterns:
            for path in project.glob(pattern):
                if path.is_file() and path.name != ".claude_mirror_manifest.json":
                    rel = str(path.relative_to(project))
                    if not self._is_excluded(rel):
                        found[rel] = path
        return sorted(found.items())

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def create(self, action: str, files_changed: list[str]) -> str:
        """Create a snapshot in the project's configured format. When
        Tier 2 mirrors are configured AND `effective_snapshot_on()` is
        `"all"`, the same snapshot is also created on every mirror in
        parallel; mirror failures surface as warnings but never abort
        the primary snapshot.

        Returns the snapshot timestamp string (always sourced from the
        primary's snapshot)."""
        fmt = (self.config.snapshot_format or "full").lower()
        mirror_mode = self.config.effective_snapshot_on()
        mirrors_active = bool(self._mirrors) and mirror_mode == "all"

        if fmt == "blobs":
            return self._create_blobs(
                action, files_changed, mirrors_active=mirrors_active
            )
        return self._create_full(
            action, files_changed, mirrors_active=mirrors_active
        )

    # ------------------------------------------------------------------
    # FULL format (legacy, server-side copy)
    # ------------------------------------------------------------------

    def _create_full(
        self, action: str, files_changed: list[str],
        mirrors_active: bool = False,
    ) -> str:
        """Create a full server-side-copy snapshot. No data passes through
        the client. When `mirrors_active`, the same snapshot is also
        materialised on every mirror in parallel (each mirror does its
        own server-side copies, since copy_file is per-backend).
        Mirror failures are reported but do not raise.

        Returns the snapshot timestamp string."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")

        # Always do the primary first (synchronously) so we can return
        # its timestamp even if every mirror fails.
        try:
            self._create_full_on(self.storage, timestamp, action, files_changed)
        except Exception as e:
            # Primary failure DOES propagate — caller (sync engine) needs
            # to know the snapshot didn't take.
            raise

        if mirrors_active and self._mirrors:
            self._mirror_full_in_parallel(timestamp, action, files_changed)

        return timestamp

    def _create_full_on(
        self, backend: StorageBackend, timestamp: str,
        action: str, files_changed: list[str],
    ) -> None:
        """Internal: create the full-format snapshot on one specific
        backend. Each backend has to do its own server-side copies
        because `copy_file` is per-backend."""
        snapshots_folder_id = self._get_snapshots_folder_for(backend)
        snapshot_folder_id = backend.get_or_create_folder(timestamp, snapshots_folder_id)

        from .events import LOGS_FOLDER  # local import to avoid cycle
        all_files = backend.list_files_recursive(
            self._root_folder_for(backend),
            exclude_folder_names={SNAPSHOTS_FOLDER, BLOBS_FOLDER, LOGS_FOLDER},
        )
        project_files = [
            f for f in all_files
            if not f["name"].startswith("_")
            and not f["relative_path"].startswith(f"{SNAPSHOTS_FOLDER}/")
            and not f["relative_path"].startswith(f"{BLOBS_FOLDER}/")
            and not f["name"].startswith("_claude_mirror_")
        ]

        def _copy_one(file_info: dict) -> None:
            rel_path = file_info["relative_path"]
            parent_id, filename = backend.resolve_path(rel_path, snapshot_folder_id)
            backend.copy_file(
                source_file_id=file_info["id"],
                dest_folder_id=parent_id,
                name=filename,
            )

        if project_files:
            workers = min(self.config.parallel_workers, len(project_files))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_copy_one, project_files))

        from .events import _truncate_files
        meta = SnapshotMeta(
            timestamp=timestamp,
            triggered_by=f"{self.config.user}@{self.config.machine_name}",
            action=action,
            files_changed=_truncate_files(files_changed),
            total_files=len(project_files),
        )
        meta_bytes = json.dumps(
            {
                "timestamp": meta.timestamp,
                "triggered_by": meta.triggered_by,
                "action": meta.action,
                "files_changed": meta.files_changed,
                "total_files": meta.total_files,
                "format": "full",
            },
            indent=2,
        ).encode()
        backend.upload_bytes(meta_bytes, SNAPSHOT_META_FILE, snapshot_folder_id)

        backend_label = self._backend_key(backend)
        target = "primary" if backend is self.storage else f"mirror {backend_label}"
        console.print(
            f"  [dim]Snapshot created (full, {target}):[/] {timestamp} "
            f"({len(project_files)} file(s))"
        )

    def _mirror_full_in_parallel(
        self, timestamp: str, action: str, files_changed: list[str],
    ) -> None:
        """Fan out full-format snapshot creation to every mirror in
        parallel. Per-mirror failures are reported, never raised."""
        def _one(backend: StorageBackend) -> tuple[StorageBackend, Optional[Exception]]:
            try:
                self._create_full_on(backend, timestamp, action, files_changed)
                return backend, None
            except Exception as e:
                return backend, e

        workers = min(self.config.parallel_workers, len(self._mirrors))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, b) for b in self._mirrors]
            for fut in as_completed(futs):
                backend, err = fut.result()
                if err is not None:
                    label = self._backend_key(backend)
                    console.print(
                        f"  [yellow]Snapshot mirror failed:[/] {label} — {err}"
                    )

    # ------------------------------------------------------------------
    # BLOBS format (content-addressed, deduplicated)
    # ------------------------------------------------------------------

    def _create_blobs(
        self, action: str, files_changed: list[str],
        mirrors_active: bool = False,
    ) -> str:
        """Create a content-addressed snapshot. Local files are SHA-256'd
        (with HashCache for repeat speed), unique blobs are uploaded once
        to `_claude_mirror_blobs/{prefix}/{hash}`, and a JSON manifest is
        written to `_claude_mirror_snapshots/{timestamp}.json`.

        When `mirrors_active`, the SAME path/hash mapping is replayed
        against every mirror in parallel (its own blob store is checked,
        only missing blobs uploaded, its own manifest written). Mirror
        failures surface as warnings but never abort the primary."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        local_files = self._iter_local_files()

        # Hash every local file (cached by size+mtime). Computed ONCE,
        # reused across the primary and every mirror — SHA-256 is
        # backend-agnostic.
        cache = HashCache(self.config.project_path)
        path_to_hash: dict[str, str] = {}        # rel_path -> sha256
        hash_to_local: dict[str, Path] = {}      # sha256 -> abs_path (one rep)

        def _hash_one(rel_and_abs: tuple[str, Path]) -> tuple[str, str, Path]:
            rel, abs_path = rel_and_abs
            try:
                st = abs_path.stat()
            except OSError as e:
                raise RuntimeError(f"Cannot stat {rel}: {e}")
            cached = cache.get_sha256(rel, st.st_size, st.st_mtime_ns)
            if cached:
                return rel, cached, abs_path
            digest = _sha256_file(str(abs_path))
            cache.set_sha256(rel, st.st_size, st.st_mtime_ns, digest)
            return rel, digest, abs_path

        if local_files:
            workers = min(8, len(local_files))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for rel, digest, abs_path in ex.map(_hash_one, local_files):
                    path_to_hash[rel] = digest
                    hash_to_local.setdefault(digest, abs_path)
        cache.save()

        # Primary first (synchronously) so we can return the timestamp
        # even if every mirror fails.
        primary_snapshots_folder_id = self._get_snapshots_folder_for(self.storage)
        primary_blobs_folder_id = self._get_blobs_folder_for(self.storage)
        self._create_blobs_on(
            backend=self.storage,
            snapshots_folder_id=primary_snapshots_folder_id,
            blobs_folder_id=primary_blobs_folder_id,
            timestamp=timestamp,
            action=action,
            files_changed=files_changed,
            path_to_hash=path_to_hash,
            hash_to_local=hash_to_local,
        )

        if mirrors_active and self._mirrors:
            self._mirror_blobs_in_parallel(
                timestamp=timestamp,
                action=action,
                files_changed=files_changed,
                path_to_hash=path_to_hash,
                hash_to_local=hash_to_local,
            )

        return timestamp

    def _create_blobs_on(
        self,
        backend: StorageBackend,
        snapshots_folder_id: str,
        blobs_folder_id: str,
        timestamp: str,
        action: str,
        files_changed: list[str],
        path_to_hash: dict[str, str],
        hash_to_local: dict[str, Path],
    ) -> str:
        """Internal: create the blobs-format snapshot on one specific backend.

        Caller already computed `path_to_hash` + `hash_to_local` from local
        files (those are SHA-256, identical across backends). This method
        lists THIS backend's existing blobs, uploads any missing ones, and
        writes `{timestamp}.json` to its own snapshots folder.
        """
        # Find which blobs already exist on remote — one recursive list of
        # the blobs folder. Skip the upload entirely for hashes already
        # present (the whole point of content addressing).
        existing_blobs: set[str] = set()
        try:
            for entry in backend.list_files_recursive(blobs_folder_id):
                # The filename is the hash; relative_path is "{prefix}/{hash}".
                existing_blobs.add(entry["name"])
        except Exception:
            # Empty or transient — proceed; uploads will create as needed.
            pass

        to_upload = [
            (h, p) for h, p in hash_to_local.items() if h not in existing_blobs
        ]

        def _upload_blob(args: tuple[str, Path]) -> str:
            digest, abs_path = args
            # upload_file creates intermediate folders (the {prefix} subfolder).
            backend.upload_file(
                local_path=str(abs_path),
                rel_path=_blob_rel_path(digest),
                root_folder_id=blobs_folder_id,
            )
            return digest

        if to_upload:
            workers = min(self.config.parallel_workers, len(to_upload))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_upload_blob, to_upload))

        # Write the manifest JSON.
        from .events import _truncate_files
        manifest = {
            "format": MANIFEST_FORMAT_VERSION,
            "timestamp": timestamp,
            "triggered_by": f"{self.config.user}@{self.config.machine_name}",
            "action": action,
            "files_changed": _truncate_files(files_changed),
            "total_files": len(path_to_hash),
            "files": path_to_hash,
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        backend.upload_bytes(
            manifest_bytes,
            f"{timestamp}{MANIFEST_SUFFIX}",
            snapshots_folder_id,
        )

        reused = len(hash_to_local) - len(to_upload)
        backend_label = self._backend_key(backend)
        target = "primary" if backend is self.storage else f"mirror {backend_label}"
        console.print(
            f"  [dim]Snapshot created (blobs, {target}):[/] {timestamp} "
            f"({len(path_to_hash)} file(s), {len(to_upload)} new blob(s), "
            f"{reused} dedup'd)"
        )
        return timestamp

    def _mirror_blobs_in_parallel(
        self,
        timestamp: str,
        action: str,
        files_changed: list[str],
        path_to_hash: dict[str, str],
        hash_to_local: dict[str, Path],
    ) -> None:
        """Fan out blobs-format snapshot creation to every mirror in
        parallel. Per-mirror failures are reported, never raised."""
        def _one(backend: StorageBackend) -> tuple[StorageBackend, Optional[Exception]]:
            try:
                snapshots_folder_id = self._get_snapshots_folder_for(backend)
                blobs_folder_id = self._get_blobs_folder_for(backend)
                self._create_blobs_on(
                    backend=backend,
                    snapshots_folder_id=snapshots_folder_id,
                    blobs_folder_id=blobs_folder_id,
                    timestamp=timestamp,
                    action=action,
                    files_changed=files_changed,
                    path_to_hash=path_to_hash,
                    hash_to_local=hash_to_local,
                )
                return backend, None
            except Exception as e:
                return backend, e

        workers = min(self.config.parallel_workers, len(self._mirrors))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_one, b) for b in self._mirrors]
            for fut in as_completed(futs):
                backend, err = fut.result()
                if err is not None:
                    label = self._backend_key(backend)
                    console.print(
                        f"  [yellow]Snapshot mirror failed:[/] {label} — {err}"
                    )

    # ------------------------------------------------------------------
    # Listing — both formats combined
    # ------------------------------------------------------------------

    def list(self, _external_progress=None) -> list[dict]:
        """Return snapshots sorted newest-first, mixing both formats.
        Each entry has a `format` field of "full" or "blobs".

        Renders a live phase progress display by default. Pass
        `_external_progress` to embed inside an outer Progress (used by
        gc / migrate so the whole command shares one live region).
        """
        snapshots_folder_id = self._get_snapshots_folder()
        results: list[dict] = []

        progress = _external_progress
        own_progress = progress is None
        ctx = make_phase_progress(console) if own_progress else None
        if own_progress:
            progress = ctx.__enter__()
        try:
            scan_task = progress.add_task(
                "Scanning", total=None,
                detail="listing snapshot folder…", show_time=True,
            )

            # v1 (full): subfolders of _claude_mirror_snapshots/
            folders = self.storage.list_folders(snapshots_folder_id)
            progress.update(
                scan_task,
                detail=f"found {len(folders)} full-format folder(s)",
            )

            # v2 (blobs): files in _claude_mirror_snapshots/ ending in .json
            try:
                files = self.storage.list_files_recursive(snapshots_folder_id)
            except Exception:
                files = []
            manifest_files = [
                f for f in files
                if f.get("relative_path", "").endswith(MANIFEST_SUFFIX)
                and "/" not in f.get("relative_path", "")  # top-level only
            ]
            progress.update(
                scan_task,
                detail=(
                    f"found {len(folders)} full-format folder(s), "
                    f"{len(manifest_files)} blobs-format manifest(s)"
                ),
            )
            progress.remove_task(scan_task)

            # Per-format meta fetch: render each as a row with a counter.
            full_done = 0
            blobs_done = 0
            full_task = progress.add_task(
                "Full meta", total=len(folders) or None,
                detail=f"0/{len(folders)}",
                show_time=False,
            )
            blobs_task = progress.add_task(
                "Blobs meta", total=len(manifest_files) or None,
                detail=f"0/{len(manifest_files)}",
                show_time=False,
            )

            def _fetch_full_meta(item: dict) -> dict:
                meta: dict = {}
                try:
                    meta_id = self.storage.get_file_id(SNAPSHOT_META_FILE, item["id"])
                    if meta_id:
                        raw = self.storage.download_file(meta_id)
                        meta = json.loads(raw)
                except Exception:
                    pass
                return {
                    "timestamp": item["name"],
                    "folder_id": item["id"],
                    "created": item.get("createdTime", ""),
                    "format": "full",
                    **meta,
                }

            def _fetch_blob_meta(item: dict) -> dict:
                meta: dict = {}
                try:
                    raw = self.storage.download_file(item["id"])
                    meta = json.loads(raw)
                except Exception:
                    pass
                ts = (
                    item["name"][: -len(MANIFEST_SUFFIX)]
                    if item["name"].endswith(MANIFEST_SUFFIX)
                    else item["name"]
                )
                return {
                    "timestamp": meta.get("timestamp", ts),
                    "manifest_id": item["id"],
                    "format": "blobs",
                    "triggered_by": meta.get("triggered_by", ""),
                    "action": meta.get("action", ""),
                    "files_changed": meta.get("files_changed", []),
                    "total_files": meta.get("total_files", 0),
                }

            if folders:
                workers = min(self.config.parallel_workers, len(folders))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_fetch_full_meta, f) for f in folders]
                    for fut in as_completed(futs):
                        results.append(fut.result())
                        full_done += 1
                        progress.update(
                            full_task, advance=1,
                            detail=f"{full_done}/{len(folders)}",
                        )
            progress.update(full_task, detail="completed")

            if manifest_files:
                workers = min(self.config.parallel_workers, len(manifest_files))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futs = [ex.submit(_fetch_blob_meta, f) for f in manifest_files]
                    for fut in as_completed(futs):
                        results.append(fut.result())
                        blobs_done += 1
                        progress.update(
                            blobs_task, advance=1,
                            detail=f"{blobs_done}/{len(manifest_files)}",
                        )
            progress.update(blobs_task, detail="completed")
        finally:
            if own_progress:
                ctx.__exit__(None, None, None)

        # Newest first by timestamp string (ISO-ish, sorts correctly).
        results.sort(key=lambda s: s.get("timestamp", ""), reverse=True)
        return results

    # ------------------------------------------------------------------
    # Inspect — view a snapshot's contents (path/hash/size)
    # ------------------------------------------------------------------

    def inspect(self, timestamp: str, backend_name: Optional[str] = None) -> dict:
        """Return a structured view of a snapshot's contents. Auto-detects
        format. Returns a dict with `format`, `timestamp`, `metadata`,
        and `files`. For `blobs`, each file dict has `path` + `hash`.
        For `full`, each file dict has `path` + `size` + `id`.
        Raises ValueError if the snapshot doesn't exist on any backend.

        Tier 2: by default tries the primary backend first; if the snapshot
        is not present (or primary is unreachable), falls through to each
        mirror in order. The user is warned when a mirror is consulted —
        consistent with `restore`'s fallback semantics.

        backend_name: when provided (e.g. "dropbox"), inspect SOLELY from
        that specific backend. No fallback.
        """
        # Explicit backend override.
        if backend_name:
            target = self._find_backend(backend_name)
            if target is None:
                available = [self._backend_key(self.storage)] + [
                    self._backend_key(m) for m in self._mirrors
                ]
                raise ValueError(
                    f"backend {backend_name!r} not configured for this "
                    f"project (available: {', '.join(available)})"
                )
            result = self._try_inspect_on(target, timestamp)
            if result is not None:
                return result
            raise ValueError(
                f"Snapshot {timestamp!r} not found on backend {backend_name!r}"
            )

        # Default: primary first, fall through to mirrors on miss/error.
        primary_label = self._backend_key(self.storage)
        try:
            result = self._try_inspect_on(self.storage, timestamp)
            if result is not None:
                return result
        except Exception as e:
            console.print(
                f"[yellow]Primary {primary_label} unavailable[/] ({e}); "
                "trying mirror(s)..."
            )

        for mirror in self._mirrors:
            mirror_label = self._backend_key(mirror)
            try:
                result = self._try_inspect_on(mirror, timestamp)
                if result is not None:
                    console.print(
                        f"[yellow]Inspecting from mirror[/] {mirror_label} "
                        f"(primary {primary_label} does not have snapshot "
                        f"{timestamp})"
                    )
                    return result
            except Exception as e:
                console.print(
                    f"[yellow]Mirror inspect failed:[/] {mirror_label} — {e}"
                )
                continue

        raise ValueError(f"Snapshot not found: {timestamp}")

    def _try_inspect_on(
        self, backend: StorageBackend, timestamp: str,
    ) -> Optional[dict]:
        """Probe `backend` for `timestamp`. Returns the inspect dict if
        found, None if not. Re-raises unexpected backend errors so the
        caller can decide whether to fall through to the next mirror."""
        snapshots_folder_id = self._get_snapshots_folder_for(backend)

        # Try v2 (blobs) first.
        manifest_id = backend.get_file_id(
            f"{timestamp}{MANIFEST_SUFFIX}", snapshots_folder_id
        )
        if manifest_id:
            return self._inspect_blobs_on(backend, timestamp, manifest_id)

        # Fall back to v1 (full).
        folders = backend.list_folders(snapshots_folder_id, name=timestamp)
        if folders:
            return self._inspect_full_on(backend, timestamp, folders[0]["id"])

        return None

    def _inspect_blobs(self, timestamp: str, manifest_id: str) -> dict:
        return self._inspect_blobs_on(self.storage, timestamp, manifest_id)

    def _inspect_blobs_on(
        self, backend: StorageBackend, timestamp: str, manifest_id: str,
    ) -> dict:
        raw = backend.download_file(manifest_id)
        manifest = json.loads(raw)
        files = sorted(
            (
                {"path": p, "hash": h}
                for p, h in (manifest.get("files") or {}).items()
            ),
            key=lambda x: x["path"],
        )
        return {
            "format": "blobs",
            "timestamp": timestamp,
            "metadata": {
                "triggered_by": manifest.get("triggered_by", ""),
                "action": manifest.get("action", ""),
                "files_changed": manifest.get("files_changed", []),
                "total_files": manifest.get("total_files", len(files)),
            },
            "files": list(files),
        }

    def _inspect_full(self, timestamp: str, folder_id: str) -> dict:
        return self._inspect_full_on(self.storage, timestamp, folder_id)

    def _inspect_full_on(
        self, backend: StorageBackend, timestamp: str, folder_id: str,
    ) -> dict:
        entries = backend.list_files_recursive(folder_id)
        entries = [f for f in entries if f["name"] != SNAPSHOT_META_FILE]
        files = sorted(
            (
                {
                    "path": f["relative_path"],
                    "id": f["id"],
                    "size": f.get("size") or 0,
                }
                for f in entries
            ),
            key=lambda x: x["path"],
        )
        meta: dict = {}
        try:
            meta_id = backend.get_file_id(SNAPSHOT_META_FILE, folder_id)
            if meta_id:
                raw = backend.download_file(meta_id)
                meta = json.loads(raw)
        except Exception:
            pass
        return {
            "format": "full",
            "timestamp": timestamp,
            "metadata": {
                "triggered_by": meta.get("triggered_by", ""),
                "action": meta.get("action", ""),
                "files_changed": meta.get("files_changed", []),
                "total_files": meta.get("total_files", len(files)),
            },
            "files": list(files),
        }

    # ------------------------------------------------------------------
    # History — version timeline of a single path across all snapshots
    # ------------------------------------------------------------------

    def history(self, path: str) -> dict:
        """Return the path's version timeline across every snapshot on
        remote. For `blobs` snapshots the SHA-256 hash gives true version
        identity (consecutive identical hashes = file unchanged). For
        `full` snapshots we can only confirm presence/absence without
        downloading each file body, so they're listed without hash.

        Returns a dict with `path`, `entries` (newest-first), and counts
        `distinct_versions` (across blobs snapshots only) and
        `total_appearances`.
        """
        snapshots = self.list()  # already sorted newest-first

        blobs_snaps = [s for s in snapshots if s.get("format") == "blobs"]
        full_snaps = [s for s in snapshots if s.get("format") == "full"]

        def _check_blobs(snap: dict) -> Optional[dict]:
            try:
                raw = self.storage.download_file(snap["manifest_id"])
                manifest = json.loads(raw)
                files = manifest.get("files", {})
                if path in files:
                    return {
                        "timestamp": snap["timestamp"],
                        "format": "blobs",
                        "hash": files[path],
                    }
            except Exception:
                pass
            return None

        def _check_full(snap: dict) -> Optional[dict]:
            # Walk path components from the snapshot root using cheap
            # per-component lookups (one API call per component) instead
            # of a full recursive BFS over the snapshot folder. For a
            # path of depth D this is D API calls vs. listing every file
            # under the snapshot — typically 10²–10³× fewer calls per
            # snapshot, and orders of magnitude less data transferred.
            try:
                parent_id = snap["folder_id"]
                components = [c for c in path.split("/") if c]
                if not components:
                    return None
                for component in components:
                    next_id = self.storage.get_file_id(component, parent_id)
                    if not next_id:
                        return None
                    parent_id = next_id
                return {
                    "timestamp": snap["timestamp"],
                    "format": "full",
                    "hash": None,  # would need download to compute
                }
            except Exception:
                pass
            return None

        entries: list[dict] = []
        if blobs_snaps:
            workers = min(self.config.parallel_workers, len(blobs_snaps))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for result in ex.map(_check_blobs, blobs_snaps):
                    if result:
                        entries.append(result)
        if full_snaps:
            workers = min(self.config.parallel_workers, len(full_snaps))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for result in ex.map(_check_full, full_snaps):
                    if result:
                        entries.append(result)

        # Sort newest-first for display.
        entries.sort(key=lambda e: e["timestamp"], reverse=True)

        # Assign version labels by walking oldest-first and giving each
        # NEW hash a sequential v1, v2, ... label. Consecutive identical
        # hashes get the same label (file was unchanged across snapshots).
        chronological = sorted(entries, key=lambda e: e["timestamp"])
        version_for_hash: dict[str, str] = {}
        next_version = 1
        for entry in chronological:
            h = entry.get("hash")
            if not h:
                entry["version"] = "?"
                continue
            if h in version_for_hash:
                entry["version"] = version_for_hash[h]
            else:
                label = f"v{next_version}"
                version_for_hash[h] = label
                next_version += 1
                entry["version"] = label

        return {
            "path": path,
            "entries": entries,
            "distinct_versions": len(version_for_hash),
            "total_appearances": len(entries),
        }

    def show_history(self, path: str) -> dict:
        """Render the path's version timeline as a table. Returns the
        history() dict so callers can reuse it."""
        with make_phase_progress(console) as progress:
            task = progress.add_task(
                "History", total=None,
                detail=f"searching snapshots for {path}…", show_time=True,
            )
            result = self.history(path)
            progress.update(task, detail="completed")

        entries = result["entries"]
        if not entries:
            console.print(
                f"[yellow]No snapshots contain[/] [cyan]{path}[/]\n"
                f"[dim]Run [bold]claude-mirror snapshots[/] to see what's "
                f"available, or [bold]claude-mirror inspect <ts>[/] to "
                f"browse a specific snapshot.[/]"
            )
            return result

        console.print(
            f"\n[bold]History of[/] [cyan]{path}[/]\n"
            f"  distinct versions:    {result['distinct_versions']}  "
            f"[dim](by SHA-256, blobs-format snapshots only)[/]\n"
            f"  total appearances:    {result['total_appearances']}"
        )

        table = Table(show_header=True, header_style="bold", title="Snapshot timeline (newest first)")
        table.add_column("Snapshot", style="bold")
        table.add_column("Version")
        table.add_column("Format")
        table.add_column("SHA-256 (12)")

        prev_version: Optional[str] = None
        for e in entries:
            h = e.get("hash") or ""
            ver = e.get("version", "?")
            # Highlight version transitions with bold; identical-to-previous
            # rows render dim so the eye picks up the change boundaries.
            if ver != prev_version and ver != "?":
                version_cell = f"[bold green]{ver}[/]"
            elif ver == "?":
                version_cell = "[dim]?[/]"
            else:
                version_cell = f"[dim]{ver}[/]"
            prev_version = ver
            table.add_row(
                e["timestamp"],
                version_cell,
                e["format"],
                h[:12] if h else "[dim]?[/]",
            )

        console.print(table)
        console.print(
            f"\n[dim]Restore any version with:[/]\n"
            f"  [bold cyan]claude-mirror restore <timestamp> {path} "
            f"--output <recovery-dir>[/]"
        )
        return result

    def show_inspect(
        self,
        timestamp: str,
        path_filter: Optional[str] = None,
        backend_name: Optional[str] = None,
    ) -> dict:
        """Print the snapshot contents as a metadata header + file table.
        path_filter: optional fnmatch glob (e.g. `memory/**`) to filter
        the displayed file list — the underlying manifest is unchanged.
        backend_name: when provided, inspect a specific backend only;
        otherwise primary first then mirrors (consistent with restore).
        Returns the inspect() dict so callers can reuse it.
        """
        with make_phase_progress(console) as progress:
            load_task = progress.add_task(
                "Inspect", total=None,
                detail="locating snapshot…", show_time=True,
            )
            try:
                progress.update(load_task, detail="downloading manifest…")
                result = self.inspect(timestamp, backend_name=backend_name)
                progress.update(load_task, detail="completed")
            except ValueError as e:
                progress.remove_task(load_task)
                console.print(f"[red]{e}[/]")
                raise

        meta = result["metadata"]
        fmt = result["format"]
        files = result["files"]

        if path_filter:
            shown = [f for f in files if fnmatch.fnmatch(f["path"], path_filter)]
        else:
            shown = files

        # Header
        console.print(
            f"\n[bold]Snapshot[/] [cyan]{timestamp}[/]  "
            f"[dim]format=[/]{fmt}\n"
            f"  triggered_by:  {meta.get('triggered_by', '?')}\n"
            f"  action:        {meta.get('action', '?')}\n"
            f"  total_files:   {meta.get('total_files', '?')}"
        )
        if path_filter:
            console.print(
                f"  filter:        [yellow]{path_filter}[/]  "
                f"({len(shown)} of {len(files)} files match)"
            )

        if not shown:
            console.print("[dim]No files to display.[/]")
            return result

        table = Table(show_header=True, header_style="bold")
        table.add_column("Path")
        if fmt == "blobs":
            table.add_column("SHA-256 (12)")
        else:
            table.add_column("Size", justify="right")

        for f in shown:
            if fmt == "blobs":
                h = f.get("hash", "")
                table.add_row(f["path"], h[:12] if h else "[dim]?[/]")
            else:
                size = f.get("size") or 0
                size_str = _human_size(size) if size else "[dim]?[/]"
                table.add_row(f["path"], size_str)

        console.print(table)
        return result

    def show_list(self) -> list[dict]:
        snapshots = self.list()
        if not snapshots:
            console.print("[dim]No snapshots found.[/]")
            return []

        table = Table(title="Snapshots", show_header=True)
        table.add_column("Timestamp", style="bold")
        table.add_column("Format")
        table.add_column("By")
        table.add_column("Action")
        table.add_column("Changed")
        table.add_column("Total files")

        for s in snapshots:
            changed = s.get("files_changed", [])
            changed_str = ", ".join(changed[:3])
            if len(changed) > 3:
                changed_str += f" +{len(changed) - 3} more"
            table.add_row(
                s["timestamp"],
                s.get("format", "?"),
                s.get("triggered_by", "unknown"),
                s.get("action", ""),
                changed_str or "[dim]—[/]",
                str(s.get("total_files", "?")),
            )

        console.print(table)
        return snapshots

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def restore(
        self, timestamp: str, output_path: str,
        paths: Optional[list[str]] = None,
        backend_name: Optional[str] = None,
    ) -> None:
        """Restore the snapshot at `timestamp` into `output_path`. Auto-detects
        which format was used (a manifest file takes precedence over a folder
        of the same name). Both formats can be restored regardless of the
        project's current `snapshot_format` setting.

        Tier 2: by default tries the primary backend first; if the snapshot
        is not found there, falls through to each mirror in order. The user
        is warned when a mirror is used. If no backend has the snapshot,
        raises ValueError.

        backend_name: when provided (e.g. "dropbox"), restore SOLELY from
        that specific backend. No fallback. Useful when the user knows
        which mirror has the version they want, or when primary is down
        and they want to bypass the probe round-trip.

        paths: optional list of relative paths (or fnmatch globs like
        `memory/**`, `*.md`) to restrict the restore to a subset of the
        snapshot's files. Empty/None restores the whole snapshot.
        """
        # Explicit backend override — restore from that one only, no fallback.
        if backend_name:
            target = self._find_backend(backend_name)
            if target is None:
                available = [self._backend_key(self.storage)] + [
                    self._backend_key(m) for m in self._mirrors
                ]
                raise ValueError(
                    f"backend {backend_name!r} not configured for this "
                    f"project (available: {', '.join(available)})"
                )
            if self._try_restore_on(target, timestamp, output_path, paths):
                return
            raise ValueError(
                f"Snapshot {timestamp!r} not found on backend {backend_name!r}"
            )

        # Default path: primary first, then mirrors in order.
        # IMPORTANT: any backend exception (auth, transport, 5xx) on the
        # primary call must NOT abort — it must fall through to mirrors,
        # because the whole point of multi-backend fallback is "primary
        # is unreachable, restore from a mirror." Previously this call
        # was bare; now it's wrapped so primary failures behave the same
        # as primary not having the snapshot.
        #
        # SECURITY: mirror fallback is split into two cases:
        #   - primary reachable but no snapshot → mirror is just being
        #     consulted as the next-best source; safe to use silently
        #     (the user's expectation is "find the snapshot anywhere
        #     in my configured backends").
        #   - primary errored (auth/network/5xx) → we cannot verify
        #     that the mirror's snapshot blobs match what the primary
        #     would have served. A malicious mirror can serve different
        #     content under the same timestamp. We REQUIRE explicit
        #     confirmation via _CONFIRM_HOOK before trusting the mirror,
        #     and we colour the warning [red] (not [yellow]) so it's
        #     hard to miss in the terminal.
        primary_label = self._backend_key(self.storage)
        primary_errored = False
        try:
            if self._try_restore_on(self.storage, timestamp, output_path, paths):
                return
        except Exception as e:
            primary_errored = True
            console.print(
                f"[red]Primary {primary_label} unavailable[/] ({e}); "
                "trying mirror(s)..."
            )

        for mirror in self._mirrors:
            mirror_label = self._backend_key(mirror)
            # Gate: when primary errored, we cannot verify mirror
            # integrity against it. Ask the confirm hook before each
            # mirror. The user can also pass `backend_name=` upstream
            # to bypass this entirely (explicit consent).
            if primary_errored:
                prompt_msg = (
                    f"Primary {primary_label} unreachable. Restore from "
                    f"{mirror_label} (cannot verify integrity against "
                    f"primary)?"
                )
                if not _CONFIRM_HOOK(prompt_msg):
                    console.print(
                        f"[red]Skipping mirror[/] {mirror_label} "
                        "(user declined unverifiable restore)"
                    )
                    continue
            try:
                # Probe + restore in one call. _try_restore_on returns
                # False if the snapshot doesn't exist on this backend.
                if self._try_restore_on(mirror, timestamp, output_path, paths):
                    if primary_errored:
                        console.print(
                            f"[red]Restoring from mirror[/] {mirror_label} "
                            f"(primary {primary_label} unreachable — "
                            f"contents NOT cross-verified)"
                        )
                    else:
                        console.print(
                            f"[yellow]Restoring from mirror[/] {mirror_label} "
                            f"(primary {primary_label} does not have snapshot "
                            f"{timestamp})"
                        )
                    return
            except Exception as e:
                # Per-mirror restore failure: log + continue trying.
                console.print(
                    f"[yellow]Mirror restore failed:[/] {mirror_label} — {e}"
                )
                continue

        raise ValueError(f"Snapshot not found: {timestamp}")

    def _find_backend(self, backend_name: str) -> Optional[StorageBackend]:
        """Look up a backend by name across primary + mirrors. Returns None
        if no match. Used by `restore --backend NAME` to force a specific
        target without going through the primary-first fallback chain."""
        if self._backend_key(self.storage) == backend_name:
            return self.storage
        for m in self._mirrors:
            if self._backend_key(m) == backend_name:
                return m
        return None

    def _try_restore_on(
        self,
        backend: StorageBackend,
        timestamp: str,
        output_path: str,
        paths: Optional[list[str]],
    ) -> bool:
        """Attempt to restore `timestamp` from `backend`. Returns True if
        the snapshot was found (and restored), False if it doesn't exist
        on this backend. Re-raises unexpected backend errors so the caller
        can decide whether to fall through to the next mirror."""
        snapshots_folder_id = self._get_snapshots_folder_for(backend)

        # Check for a v2 manifest first.
        manifest_id = backend.get_file_id(
            f"{timestamp}{MANIFEST_SUFFIX}", snapshots_folder_id
        )
        if manifest_id:
            self._restore_blobs(
                backend, timestamp, output_path, manifest_id, paths=paths
            )
            return True

        # Fall back to v1 folder.
        folders = backend.list_folders(snapshots_folder_id, name=timestamp)
        if folders:
            self._restore_full(
                backend, timestamp, output_path, folders[0]["id"], paths=paths
            )
            return True

        return False

    def _matches_paths(self, rel_path: str, paths: list[str]) -> bool:
        """True if rel_path matches any entry in `paths` — either an exact
        match or an fnmatch glob (e.g. `memory/**`, `*.md`)."""
        for p in paths:
            if rel_path == p or fnmatch.fnmatch(rel_path, p):
                return True
        return False

    def _restore_full(
        self, backend: StorageBackend, timestamp: str, output_path: str,
        snapshot_folder_id: str, paths: Optional[list[str]] = None,
    ) -> None:
        """Restore a full-format snapshot from the given `backend`."""
        all_files = backend.list_files_recursive(snapshot_folder_id)
        all_files = [f for f in all_files if f["name"] != SNAPSHOT_META_FILE]

        if paths:
            files = [f for f in all_files if self._matches_paths(f["relative_path"], paths)]
            if not files:
                console.print(
                    f"[yellow]No files in snapshot[/] {timestamp} match: "
                    f"{', '.join(repr(p) for p in paths)}\n"
                    f"[dim]Total files in snapshot: {len(all_files)}. "
                    f"Run `claude-mirror inspect {timestamp}` to see what's there.[/]"
                )
                return
        else:
            files = all_files

        dest = Path(output_path)
        dest.mkdir(parents=True, exist_ok=True)

        scope = (
            f"{len(files)} of {len(all_files)} file(s)"
            if paths else f"{len(files)} file(s)"
        )
        console.print(
            f"[blue]Restoring snapshot[/] {timestamp} (full) → {output_path} "
            f"({scope})"
        )

        def _download_one(file_info: dict) -> str:
            rel_path = file_info["relative_path"]
            target = _safe_join(dest, rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = backend.download_file(file_info["id"])
            target.write_bytes(content)
            return rel_path

        if files:
            workers = min(self.config.parallel_workers, len(files))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_download_one, f): f for f in files}
                for fut in as_completed(futs):
                    try:
                        rel = fut.result()
                        console.print(f"  [blue]↓[/] {rel}")
                    except ValueError as e:
                        console.print(f"  [red]✗ {e}[/]")

        console.print(
            f"[green]Restore complete.[/] {len(files)} file(s) written to {output_path}"
        )

    def _restore_blobs(
        self, backend: StorageBackend, timestamp: str, output_path: str,
        manifest_id: str, paths: Optional[list[str]] = None,
    ) -> None:
        """Restore a blobs-format snapshot from the given `backend`."""
        manifest_raw = backend.download_file(manifest_id)
        manifest = json.loads(manifest_raw)
        full_path_to_hash: dict[str, str] = manifest.get("files", {})

        if paths:
            path_to_hash = {
                p: h for p, h in full_path_to_hash.items()
                if self._matches_paths(p, paths)
            }
            if not path_to_hash:
                console.print(
                    f"[yellow]No files in snapshot[/] {timestamp} match: "
                    f"{', '.join(repr(p) for p in paths)}\n"
                    f"[dim]Total files in snapshot: {len(full_path_to_hash)}. "
                    f"Run `claude-mirror inspect {timestamp}` to see what's there.[/]"
                )
                return
        else:
            path_to_hash = full_path_to_hash

        dest = Path(output_path)
        dest.mkdir(parents=True, exist_ok=True)

        scope = (
            f"{len(path_to_hash)} of {len(full_path_to_hash)} file(s)"
            if paths else f"{len(path_to_hash)} file(s)"
        )
        console.print(
            f"[blue]Restoring snapshot[/] {timestamp} (blobs) → {output_path} "
            f"({scope})"
        )

        # Build hash -> file_id map by listing this backend's blobs folder.
        blobs_folder_id = self._get_blobs_folder_for(backend)
        hash_to_id: dict[str, str] = {}
        for entry in backend.list_files_recursive(blobs_folder_id):
            hash_to_id[entry["name"]] = entry["id"]

        missing = [(p, h) for p, h in path_to_hash.items() if h not in hash_to_id]
        if missing:
            console.print(
                f"[yellow]Warning:[/] {len(missing)} file(s) reference blobs that "
                "are no longer on remote. Did `gc` run after this snapshot was "
                "created? These files will be skipped."
            )

        def _download_one(item: tuple[str, str]) -> str:
            rel_path, sha = item
            target = _safe_join(dest, rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            content = backend.download_file(hash_to_id[sha])
            # Verify the blob hasn't been corrupted in transit.
            actual = hashlib.sha256(content).hexdigest()
            if actual != sha:
                raise RuntimeError(
                    f"Blob hash mismatch for {rel_path}: expected {sha}, got {actual}"
                )
            target.write_bytes(content)
            return rel_path

        downloadable = [(p, h) for p, h in path_to_hash.items() if h in hash_to_id]
        ok = 0
        if downloadable:
            workers = min(self.config.parallel_workers, len(downloadable))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_download_one, item): item for item in downloadable}
                for fut in as_completed(futs):
                    try:
                        rel = fut.result()
                        console.print(f"  [blue]↓[/] {rel}")
                        ok += 1
                    except (ValueError, RuntimeError) as e:
                        console.print(f"  [red]✗ {e}[/]")

        console.print(
            f"[green]Restore complete.[/] {ok}/{len(path_to_hash)} file(s) "
            f"written to {output_path}"
        )

    # ------------------------------------------------------------------
    # Garbage collection (blobs format only)
    # ------------------------------------------------------------------

    def gc(self, dry_run: bool = False, backend_name: Optional[str] = None) -> dict:
        """Delete blobs no longer referenced by any v2 manifest.

        backend_name: when None (default), gc operates on the primary
        backend (`self.storage`). When set to one of the configured
        mirror's `backend_name` values, gc operates on THAT mirror's
        blob store + manifest folder instead — useful for cleaning
        up orphans on a specific mirror without touching the primary.
        Raises ValueError if the name doesn't match the primary or
        any configured mirror.

        Safety:
          * Refuses to run if there are zero v2 manifests on the chosen
            backend — this would otherwise delete the entire blob store.
          * Reads ALL manifests first; only then lists blobs; only then
            deletes orphans. A blob written between the manifest read and
            the blob list is safe (it must be referenced by a manifest
            written after our read, but we don't see that manifest, so we
            also don't see — wait, we DO list blobs after manifests, so we
            could see a blob whose referencing manifest we missed.

            Mitigation: we read manifests strictly AFTER listing blobs, so
            any blob we list is one whose existence predates the manifest
            scan; if that blob is referenced, we'll see the reference. New
            blobs written after we listed are not in our list and won't be
            touched. This ordering is reversed below for that reason.

        Returns: summary dict with counts.
        """
        # Resolve which backend to gc against.
        target_backend = self.storage  # default: primary
        if backend_name is not None:
            primary_name = (
                getattr(self.storage, "backend_name", "") or "primary"
            )
            if backend_name == primary_name:
                target_backend = self.storage
            else:
                target_backend = next(
                    (b for b in self._mirrors
                     if (getattr(b, "backend_name", "") or "") == backend_name),
                    None,
                )
                if target_backend is None:
                    available = [primary_name] + [
                        getattr(b, "backend_name", "?") or "?"
                        for b in self._mirrors
                    ]
                    raise ValueError(
                        f"No backend named {backend_name!r} configured for "
                        f"this project. Available: {', '.join(available)}"
                    )
        snapshots_folder_id = self._get_snapshots_folder_for(target_backend)
        blobs_folder_id = self._get_blobs_folder_for(target_backend)

        with make_phase_progress(console) as progress:
            # 1) List blobs first.
            blobs_task = progress.add_task(
                "Blobs", total=None,
                detail="listing remote blob store…", show_time=True,
            )
            blob_folders_seen = 0
            blob_files_seen = 0

            def _blob_cb(folders_done, files_seen):
                nonlocal blob_folders_seen, blob_files_seen
                blob_folders_seen = folders_done
                blob_files_seen = files_seen
                progress.update(
                    blobs_task,
                    detail=f"explored {folders_done} folder(s), {files_seen} blob(s)",
                )

            try:
                blob_entries = target_backend.list_files_recursive(
                    blobs_folder_id, progress_cb=_blob_cb,
                )
            except Exception:
                blob_entries = []
            progress.update(blobs_task, detail=f"found {len(blob_entries)} blob(s)")

            # 2) Then list and read all manifests (so any blob written after
            #    blob-listing but referenced by a new manifest is invisible
            #    to us; that blob can't be in our delete set, so it's safe).
            scan_task = progress.add_task(
                "Manifests", total=None,
                detail="listing manifests…", show_time=False,
            )
            try:
                files = target_backend.list_files_recursive(snapshots_folder_id)
            except Exception:
                files = []
            manifest_files = [
                f for f in files
                if f.get("relative_path", "").endswith(MANIFEST_SUFFIX)
                and "/" not in f.get("relative_path", "")
            ]
            progress.update(
                scan_task,
                total=len(manifest_files) or None,
                detail=f"0/{len(manifest_files)} read",
            )

            if not manifest_files:
                progress.remove_task(scan_task)
                console.print(
                    "[red]Refusing to gc:[/] no blobs-format manifests found on "
                    "remote. Running gc with no manifests would delete every blob. "
                    "If this project never used blobs format, there's nothing "
                    "to do here."
                )
                return {"refused": True, "manifests": 0, "blobs": len(blob_entries)}

            referenced: set[str] = set()
            for i, mf in enumerate(manifest_files, 1):
                try:
                    raw = target_backend.download_file(mf["id"])
                    manifest = json.loads(raw)
                    referenced.update(manifest.get("files", {}).values())
                except Exception:
                    pass
                progress.update(
                    scan_task, advance=1,
                    detail=f"{i}/{len(manifest_files)} read",
                )
            progress.update(scan_task, detail="completed")

            orphans = [b for b in blob_entries if b["name"] not in referenced]

            console.print(
                f"[bold]gc summary[/]\n"
                f"  manifests scanned:      {len(manifest_files)}\n"
                f"  blobs on remote:        {len(blob_entries)}\n"
                f"  referenced (kept):      {len(blob_entries) - len(orphans)}\n"
                f"  orphans (will delete):  {len(orphans)}"
            )

            if dry_run:
                # CLI handles up-front + trailing dry-run framing; no
                # mid-flow line here (would just duplicate the banner).
                return {
                    "refused": False,
                    "manifests": len(manifest_files),
                    "blobs": len(blob_entries),
                    "orphans": len(orphans),
                    "deleted": 0,
                }

            if not orphans:
                return {
                    "refused": False,
                    "manifests": len(manifest_files),
                    "blobs": len(blob_entries),
                    "orphans": 0,
                    "deleted": 0,
                }

            sweep_task = progress.add_task(
                "Sweep", total=len(orphans),
                detail=f"0/{len(orphans)} deleted", show_time=False,
            )
            deleted = 0
            done = 0

            def _delete_one(entry: dict) -> bool:
                try:
                    target_backend.delete_file(entry["id"])
                    return True
                except Exception:
                    return False

            workers = min(self.config.parallel_workers, len(orphans))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(_delete_one, o) for o in orphans]
                for fut in as_completed(futs):
                    if fut.result():
                        deleted += 1
                    done += 1
                    progress.update(
                        sweep_task, advance=1,
                        detail=f"{deleted}/{len(orphans)} deleted",
                    )
            progress.update(sweep_task, detail="completed")

        console.print(f"[green]Deleted {deleted} orphan blob(s).[/]")
        return {
            "refused": False,
            "manifests": len(manifest_files),
            "blobs": len(blob_entries),
            "orphans": len(orphans),
            "deleted": deleted,
        }

    # ------------------------------------------------------------------
    # Migration: full <-> blobs
    # ------------------------------------------------------------------

    def migrate(self, target: str, dry_run: bool = False, keep_source: bool = False) -> dict:
        """Convert every snapshot on remote to `target` format ("blobs" or
        "full"). Idempotent: re-running skips snapshots already in the
        target format. Atomic per snapshot: the source artifact is only
        deleted after the target artifact has been fully written.

        keep_source: when True, leaves the source artifacts in place after
        conversion. Useful for a cautious dry transition where the user
        wants both formats present and will clean up manually later.
        """
        target = target.lower()
        if target not in ("blobs", "full"):
            raise ValueError(f"Unknown target format: {target!r}")

        with make_phase_progress(console) as progress:
            # Phase 1: scan all existing snapshots (delegates to list()
            # with our outer progress so the user sees the listing live
            # rather than waiting in silence).
            existing = self.list(_external_progress=progress)

            # Detect orphans: same timestamp present in BOTH formats. Means
            # a previous migrate wrote the target manifest/folder but its
            # source-deletion step failed (network blip, rate limit,
            # interrupted run, etc.). Without this cleanup, the next
            # migrate would re-process the source and create a duplicate
            # target — wasteful and corrupting. So before converting,
            # delete the source side of any timestamp that already has the
            # target side written.
            target_timestamps = {
                s["timestamp"] for s in existing if s.get("format") == target
            }
            source_format = "full" if target == "blobs" else "blobs"
            orphans = [
                s for s in existing
                if s.get("format") == source_format
                and s["timestamp"] in target_timestamps
            ]

            # Real conversion targets: snapshots in the source format that
            # do NOT already have a target-side counterpart.
            to_convert = [
                s for s in existing
                if s.get("format") == source_format
                and s["timestamp"] not in target_timestamps
            ]

            # Process in chronological order (oldest first). list() returns
            # newest-first for the user-facing snapshots table, but for
            # migration the oldest-first order is preferable:
            #   - safer mental model: oldest snapshots are the least
            #     valuable to keep restore-tested; if anything goes wrong
            #     with the new format mid-migration, the most recent (most
            #     important) snapshots remain in their proven format the
            #     longest.
            #   - the new-blob upload count trends down visibly as we go,
            #     because each subsequent snapshot dedupes against blobs
            #     accumulated from earlier ones — clearer progress signal.
            to_convert.sort(key=lambda s: s["timestamp"])
            orphans.sort(key=lambda s: s["timestamp"])

            if not to_convert and not orphans:
                console.print(
                    f"[dim]All {len(existing)} snapshot(s) are already in "
                    f"`{target}` format — nothing to migrate.[/]"
                )
                return {
                    "converted": 0,
                    "skipped": len(existing),
                    "orphans_cleaned": 0,
                    "errors": 0,
                }

            already_done = len([
                s for s in existing
                if s.get("format") == target
                and s["timestamp"] not in {o["timestamp"] for o in orphans}
            ])
            console.print(
                f"[bold]migrate-snapshots →[/] [cyan]{target}[/]\n"
                f"  total snapshots:    {len(existing)}\n"
                f"  already in target:  {already_done}\n"
                f"  orphans to clean:   {len(orphans)}  "
                f"[dim](source-side leftovers from a prior migrate)[/]\n"
                f"  to convert:         {len(to_convert)}"
                + ("\n  [dim](dry run — no writes)[/]" if dry_run else "")
            )

            if dry_run:
                for s in orphans:
                    console.print(
                        f"  [yellow]⌫[/] {s['timestamp']}  "
                        f"(orphan {s['format']} folder — would be deleted)"
                    )
                for s in to_convert:
                    console.print(
                        f"  [yellow]→[/] {s['timestamp']}  "
                        f"({s['format']} → {target})"
                    )
                return {
                    "converted": 0,
                    "skipped": already_done,
                    "orphans_cleaned": 0,
                    "errors": 0,
                }

            # Phase: clean orphans first. Each orphan is one delete; after
            # this phase, every remaining timestamp exists in exactly one
            # format and the convert phase can proceed without ambiguity.
            orphans_cleaned = 0
            orphan_errors = 0
            if orphans:
                orphan_task = progress.add_task(
                    "Cleanup", total=len(orphans),
                    detail=f"0/{len(orphans)} orphan(s) cleaned", show_time=True,
                )
                for i, s in enumerate(orphans, 1):
                    try:
                        self._forget_one(s)
                        orphans_cleaned += 1
                        console.print(
                            f"  [green]⌫[/] cleaned orphan "
                            f"{s['format']} side of {s['timestamp']}"
                        )
                    except Exception as e:
                        orphan_errors += 1
                        console.print(
                            f"  [red]✗[/] could not clean orphan {s['format']} "
                            f"side of {s['timestamp']}: {e}"
                        )
                    progress.update(
                        orphan_task, advance=1,
                        detail=f"{orphans_cleaned}/{len(orphans)} cleaned",
                    )
                progress.update(orphan_task, detail="completed")

            if not to_convert:
                console.print(
                    f"[bold]migrate complete:[/] "
                    f"orphans_cleaned={orphans_cleaned} "
                    f"errors={orphan_errors} "
                    f"(no snapshots needed conversion)"
                )
                return {
                    "converted": 0,
                    "skipped": already_done,
                    "orphans_cleaned": orphans_cleaned,
                    "errors": orphan_errors,
                }

            convert_task = progress.add_task(
                "Convert", total=len(to_convert),
                detail=f"0/{len(to_convert)}", show_time=True,
            )

            converted = 0
            errors = 0
            for i, s in enumerate(to_convert, 1):
                progress.update(
                    convert_task,
                    detail=f"{i}/{len(to_convert)} — {s['timestamp']} ({s['format']} → {target})",
                )
                try:
                    if target == "blobs":
                        self._migrate_full_to_blobs(s, keep_source=keep_source)
                    else:
                        self._migrate_blobs_to_full(s, keep_source=keep_source)
                    converted += 1
                    console.print(
                        f"  [green]✓[/] {s['timestamp']}  ({s['format']} → {target})"
                    )
                except Exception as e:
                    errors += 1
                    console.print(f"  [red]✗[/] {s['timestamp']}: {e}")
                progress.update(convert_task, advance=1)
            progress.update(convert_task, detail="completed")

        console.print(
            f"[bold]migrate complete:[/] converted {converted}, "
            f"orphans_cleaned {orphans_cleaned}, "
            f"skipped {already_done}, "
            f"errors {errors + orphan_errors}"
        )
        return {
            "converted": converted,
            "skipped": already_done,
            "orphans_cleaned": orphans_cleaned,
            "errors": errors + orphan_errors,
        }

    # ------------------------------------------------------------------
    # Forget — delete individual snapshots
    # ------------------------------------------------------------------

    def forget(
        self,
        timestamps: Optional[list[str]] = None,
        before: Optional[str] = None,
        keep_last: Optional[int] = None,
        keep_days: Optional[int] = None,
        dry_run: bool = False,
    ) -> dict:
        """Delete snapshots matching one of the four selectors.

        timestamps: explicit list — delete snapshots whose timestamp string
            matches exactly. Use `claude-mirror snapshots` to find timestamps.
        before: ISO date ("2026-04-15") or relative ("30d", "2w", "3m") —
            delete every snapshot strictly older than that point in time.
        keep_last: keep the N newest snapshots, delete the rest.
        keep_days: keep snapshots from the last N days, delete older.

        For `full`-format snapshots, the snapshot folder is deleted via the
        backend. For `blobs`-format, the manifest JSON is deleted; blobs in
        `_claude_mirror_blobs/` referenced only by the deleted manifest(s)
        become orphaned and are reclaimable by `claude-mirror gc` afterwards.

        Always prints a summary of what will be deleted; with `dry_run=True`
        prints the summary and exits without touching remote storage.
        """
        # Validate exactly one selector
        provided = [
            ("timestamps", timestamps),
            ("before", before),
            ("keep_last", keep_last),
            ("keep_days", keep_days),
        ]
        active = [name for name, val in provided if val not in (None, [], 0)]
        if len(active) != 1:
            raise ValueError(
                "forget requires exactly one selector "
                "(timestamps | before | keep_last | keep_days); "
                f"got {len(active)}: {active}"
            )

        all_snapshots = self.list()
        if not all_snapshots:
            console.print("[dim]No snapshots on remote — nothing to forget.[/]")
            return {"selected": 0, "deleted": 0, "errors": 0}

        targets = self._select_to_forget(
            all_snapshots, timestamps, before, keep_last, keep_days
        )

        if not targets:
            console.print("[dim]No snapshots matched the selector — nothing to forget.[/]")
            return {"selected": 0, "deleted": 0, "errors": 0}

        kept = len(all_snapshots) - len(targets)
        console.print(
            f"[bold]forget[/]\n"
            f"  total snapshots:  {len(all_snapshots)}\n"
            f"  to delete:        {len(targets)}\n"
            f"  to keep:          {kept}"
            + ("\n  [dim](dry run — no writes)[/]" if dry_run else "")
        )
        for s in targets:
            console.print(
                f"  [yellow]→[/] {s['timestamp']}  ({s.get('format', '?')}, "
                f"{s.get('total_files', '?')} file(s))"
            )

        if dry_run:
            return {"selected": len(targets), "deleted": 0, "errors": 0}

        with make_phase_progress(console) as progress:
            del_task = progress.add_task(
                "Forget", total=len(targets),
                detail=f"0/{len(targets)} deleted", show_time=True,
            )
            deleted = 0
            errors = 0
            for i, snap in enumerate(targets, 1):
                try:
                    self._forget_one(snap)
                    deleted += 1
                    console.print(
                        f"  [green]✓[/] {snap['timestamp']}  "
                        f"({snap.get('format', '?')})"
                    )
                except Exception as e:
                    errors += 1
                    console.print(f"  [red]✗[/] {snap['timestamp']}: {e}")
                progress.update(
                    del_task, advance=1,
                    detail=f"{deleted}/{len(targets)} deleted",
                )
            progress.update(del_task, detail="completed")

        console.print(
            f"[bold]forget complete:[/] deleted {deleted}, errors {errors}"
        )
        if deleted and any(s.get("format") == "blobs" for s in targets):
            console.print(
                "[dim]Tip: run [bold]claude-mirror gc[/] to reclaim blob space "
                "from blobs no longer referenced by any remaining manifest.[/]"
            )
        return {"selected": len(targets), "deleted": deleted, "errors": errors}

    def prune_per_retention(
        self,
        *,
        keep_last: int = 0,
        keep_daily: int = 0,
        keep_monthly: int = 0,
        keep_yearly: int = 0,
        dry_run: bool = True,
    ) -> dict:
        """Apply a multi-bucket retention policy to the snapshot set.

        Each non-zero parameter contributes timestamps to a "keep" set;
        the union is retained, the rest is deleted. With every parameter
        at 0 the policy is disabled and the call is a no-op (returns
        zero deletions).

        Returns a dict with `selected`, `deleted`, `errors`, plus
        `to_keep` and `to_delete` lists of timestamp strings — handy for
        callers like `claude-mirror push` that want to log a one-line
        prune summary after running.
        """
        if not any((keep_last, keep_daily, keep_monthly, keep_yearly)):
            return {
                "selected": 0, "deleted": 0, "errors": 0,
                "to_keep": [], "to_delete": [],
            }

        all_snapshots = self.list()
        if not all_snapshots:
            return {
                "selected": 0, "deleted": 0, "errors": 0,
                "to_keep": [], "to_delete": [],
            }

        keep_set = self._compute_retention_keep_set(
            all_snapshots,
            keep_last=keep_last,
            keep_daily=keep_daily,
            keep_monthly=keep_monthly,
            keep_yearly=keep_yearly,
        )
        to_delete = [s for s in all_snapshots if s["timestamp"] not in keep_set]
        to_keep_ts = [s["timestamp"] for s in all_snapshots if s["timestamp"] in keep_set]

        if not to_delete:
            return {
                "selected": 0, "deleted": 0, "errors": 0,
                "to_keep": to_keep_ts, "to_delete": [],
            }

        policy_summary = ", ".join(
            f"{name}={val}"
            for name, val in (
                ("keep_last", keep_last),
                ("keep_daily", keep_daily),
                ("keep_monthly", keep_monthly),
                ("keep_yearly", keep_yearly),
            )
            if val
        )
        console.print(
            f"[bold]prune[/]  [dim]({policy_summary})[/]\n"
            f"  total snapshots:  {len(all_snapshots)}\n"
            f"  to delete:        {len(to_delete)}\n"
            f"  to keep:          {len(to_keep_ts)}"
            + ("\n  [dim](dry run — no writes)[/]" if dry_run else "")
        )
        for s in to_delete:
            console.print(
                f"  [yellow]→[/] {s['timestamp']}  ({s.get('format', '?')}, "
                f"{s.get('total_files', '?')} file(s))"
            )

        if dry_run:
            return {
                "selected": len(to_delete), "deleted": 0, "errors": 0,
                "to_keep": to_keep_ts,
                "to_delete": [s["timestamp"] for s in to_delete],
            }

        deleted = 0
        errors = 0
        for snap in to_delete:
            try:
                self._forget_one(snap)
                deleted += 1
                console.print(
                    f"  [green]✓[/] {snap['timestamp']}  "
                    f"({snap.get('format', '?')})"
                )
            except Exception as e:
                errors += 1
                console.print(f"  [red]✗[/] {snap['timestamp']}: {e}")

        console.print(
            f"[bold]prune complete:[/] deleted {deleted}, errors {errors}"
        )
        if deleted and any(s.get("format") == "blobs" for s in to_delete):
            console.print(
                "[dim]Tip: run [bold]claude-mirror gc[/] to reclaim blob space "
                "from blobs no longer referenced by any remaining manifest.[/]"
            )
        return {
            "selected": len(to_delete), "deleted": deleted, "errors": errors,
            "to_keep": to_keep_ts,
            "to_delete": [s["timestamp"] for s in to_delete],
        }

    def _compute_retention_keep_set(
        self,
        snapshots: list[dict],
        *,
        keep_last: int,
        keep_daily: int,
        keep_monthly: int,
        keep_yearly: int,
    ) -> set[str]:
        """Compute the union of timestamps to retain across four buckets.

        snapshots is newest-first (matches `list()` ordering). For each
        time-bucket selector, walk newest-first and pick the first
        snapshot whose bucket-key hasn't been seen yet — that's the
        "newest in the bucket". Stop once the configured count is reached.
        """
        keep: set[str] = set()

        if keep_last > 0:
            for s in snapshots[:keep_last]:
                keep.add(s["timestamp"])

        def _bucket_pick(key_fn, n: int) -> None:
            seen: set = set()
            for s in snapshots:
                k = key_fn(self._parse_snapshot_ts(s["timestamp"]))
                if k in seen:
                    continue
                seen.add(k)
                keep.add(s["timestamp"])
                if len(seen) >= n:
                    return

        if keep_daily > 0:
            _bucket_pick(lambda dt: (dt.year, dt.month, dt.day), keep_daily)
        if keep_monthly > 0:
            _bucket_pick(lambda dt: (dt.year, dt.month), keep_monthly)
        if keep_yearly > 0:
            _bucket_pick(lambda dt: dt.year, keep_yearly)

        return keep

    def _select_to_forget(
        self,
        snapshots: list[dict],
        timestamps: Optional[list[str]],
        before: Optional[str],
        keep_last: Optional[int],
        keep_days: Optional[int],
    ) -> list[dict]:
        """Compute the list of snapshots to delete from one of four selectors.
        snapshots is already sorted newest-first by `list()`."""
        if timestamps:
            wanted = set(timestamps)
            return [s for s in snapshots if s["timestamp"] in wanted]

        if keep_last is not None:
            if keep_last < 0:
                raise ValueError(f"keep_last must be >= 0; got {keep_last}")
            # snapshots is newest-first, so [keep_last:] is everything older.
            return list(snapshots[keep_last:])

        if keep_days is not None:
            if keep_days < 0:
                raise ValueError(f"keep_days must be >= 0; got {keep_days}")
            cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
            return [
                s for s in snapshots
                if self._parse_snapshot_ts(s["timestamp"]) < cutoff
            ]

        if before:
            cutoff = self._parse_before(before)
            return [
                s for s in snapshots
                if self._parse_snapshot_ts(s["timestamp"]) < cutoff
            ]

        return []

    @staticmethod
    def _parse_snapshot_ts(ts: str) -> datetime:
        """Parse a snapshot timestamp string like '2026-04-07T15-22-50Z' into
        a timezone-aware UTC datetime. Fall back to a very old date so a
        bad timestamp is treated as 'older than anything' (gets deleted by
        retention policies, which is the safer behaviour)."""
        try:
            # Snapshot timestamps use '-' between time fields, not ':'
            return datetime.strptime(ts, "%Y-%m-%dT%H-%M-%SZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            try:
                return datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                return datetime(1970, 1, 1, tzinfo=timezone.utc)

    @staticmethod
    def _parse_before(value: str) -> datetime:
        """Parse `--before` as either an ISO date ('2026-04-15'), an ISO
        timestamp ('2026-04-15T10:00:00Z'), or a relative duration
        ('30d', '2w', '3m', '1y'). Returns a timezone-aware UTC datetime."""
        v = value.strip().lower()
        if not v:
            raise ValueError("before: empty value")
        # Relative form: <N><unit>  with unit in d/w/m/y
        if v[-1] in "dwmy" and v[:-1].isdigit():
            n = int(v[:-1])
            unit = v[-1]
            days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * n
            return datetime.now(timezone.utc) - timedelta(days=days)
        # Absolute ISO date or datetime
        try:
            if "t" in v.lower() or " " in v:
                return datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            return datetime.strptime(v, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError as e:
            raise ValueError(
                f"--before: cannot parse {value!r} as date or duration. "
                "Use 'YYYY-MM-DD' or 'Nd' / 'Nw' / 'Nm' / 'Ny' (e.g. '30d')."
            ) from e

    def _forget_one(self, snap: dict) -> None:
        """Delete a single snapshot's on-remote artifacts. Format-specific:
        full → delete the folder (cascades to all its files). blobs →
        delete only the manifest JSON; blobs are reclaimed later by gc."""
        fmt = (snap.get("format") or "").lower()
        if fmt == "blobs":
            manifest_id = snap.get("manifest_id")
            if not manifest_id:
                raise RuntimeError("blobs snapshot has no manifest_id")
            self.storage.delete_file(manifest_id)
            return
        if fmt == "full":
            folder_id = snap.get("folder_id")
            if not folder_id:
                raise RuntimeError("full snapshot has no folder_id")
            # On Drive/Dropbox/OneDrive/WebDAV, deleting a folder removes
            # everything under it. For backends where that isn't true we'd
            # need to walk + delete files first; all current backends
            # cascade, so this is sufficient.
            self.storage.delete_file(folder_id)
            return
        raise RuntimeError(f"unknown snapshot format: {fmt!r}")

    def _migrate_full_to_blobs(self, snap: dict, keep_source: bool) -> None:
        """Convert one full-format snapshot folder into a v2 manifest +
        blobs. Files in the snapshot are downloaded once each (we have no
        choice; SHA-256 is not stored remotely), uploaded as blobs if not
        already present, and a manifest JSON is written. The source folder
        is deleted last (unless keep_source)."""
        timestamp = snap["timestamp"]
        snapshots_folder_id = self._get_snapshots_folder()
        blobs_folder_id = self._get_blobs_folder()
        snapshot_folder_id = snap["folder_id"]

        files = self.storage.list_files_recursive(snapshot_folder_id)
        files = [f for f in files if f["name"] != SNAPSHOT_META_FILE]

        # Build the existing-blobs set once.
        try:
            existing_blobs = {b["name"] for b in self.storage.list_files_recursive(blobs_folder_id)}
        except Exception:
            existing_blobs = set()

        # Workers in the ThreadPoolExecutor below mutate `existing_blobs`
        # concurrently. CPython sets are NOT thread-safe under contended
        # add(), and a lost update would let two workers each upload the
        # same blob (wasting bandwidth + space). The lock covers ONLY the
        # check-then-set decision; the actual upload happens outside it
        # so different blobs can still upload in parallel.
        _existing_blobs_lock = threading.Lock()

        path_to_hash: dict[str, str] = {}

        def _convert_one(file_info: dict) -> tuple[str, str]:
            rel = file_info["relative_path"]
            content = self.storage.download_file(file_info["id"])
            sha = hashlib.sha256(content).hexdigest()
            with _existing_blobs_lock:
                need_upload = sha not in existing_blobs
                if need_upload:
                    # Reserve the slot now so a concurrent worker on the
                    # same content doesn't double-upload while we're
                    # uploading. If the upload below raises we'll roll
                    # back the reservation in the except branch.
                    existing_blobs.add(sha)
            if need_upload:
                try:
                    # upload via upload_bytes (no temp file needed).
                    self.storage.upload_bytes(
                        content=content,
                        name=sha,
                        folder_id=self.storage.get_or_create_folder(
                            sha[:2], blobs_folder_id
                        ),
                        mimetype="application/octet-stream",
                    )
                except Exception:
                    with _existing_blobs_lock:
                        existing_blobs.discard(sha)
                    raise
            return rel, sha

        if files:
            workers = min(self.config.parallel_workers, len(files))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for rel, sha in ex.map(_convert_one, files):
                    path_to_hash[rel] = sha

        # Read original meta (for triggered_by/action/files_changed).
        original_meta: dict = {}
        meta_id: Optional[str] = None
        try:
            meta_id = self.storage.get_file_id(SNAPSHOT_META_FILE, snapshot_folder_id)
            if meta_id:
                original_meta = json.loads(self.storage.download_file(meta_id))
        except Exception:
            pass

        manifest = {
            "format": MANIFEST_FORMAT_VERSION,
            "timestamp": timestamp,
            "triggered_by": original_meta.get(
                "triggered_by", f"{self.config.user}@{self.config.machine_name}"
            ),
            "action": original_meta.get("action", "migrated"),
            "files_changed": original_meta.get("files_changed", []),
            "total_files": len(path_to_hash),
            "files": path_to_hash,
            "migrated_from": "full",
        }
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode()
        self.storage.upload_bytes(
            manifest_bytes,
            f"{timestamp}{MANIFEST_SUFFIX}",
            snapshots_folder_id,
        )

        if not keep_source:
            # All four backends (Drive, Dropbox, OneDrive, WebDAV) cascade
            # folder delete to contents — one call removes the snapshot's
            # full-format folder and every file inside. We DO NOT swallow
            # errors here: a silent failure would leave the snapshot
            # existing in BOTH formats on remote, which then either
            # confuses the user or causes a future migrate run to
            # re-migrate the same timestamp and produce duplicate blobs
            # manifests. Better to surface the error and let the user
            # re-run; the cleanup-orphan phase at the start of migrate()
            # will retry these on the next pass.
            self.storage.delete_file(snapshot_folder_id)

    def _migrate_blobs_to_full(self, snap: dict, keep_source: bool) -> None:
        """Convert one v2 manifest into a full-format snapshot folder.
        Each referenced blob is downloaded and uploaded into the new
        folder under its original relative path. The manifest JSON is
        deleted last (unless keep_source). Blobs are NOT deleted — run
        `claude-mirror gc` after migrating all manifests if desired (it
        will see the v2 manifests are gone and refuse, so plan ahead)."""
        timestamp = snap["timestamp"]
        manifest_id = snap["manifest_id"]
        snapshots_folder_id = self._get_snapshots_folder()
        blobs_folder_id = self._get_blobs_folder()

        manifest = json.loads(self.storage.download_file(manifest_id))
        path_to_hash: dict[str, str] = manifest.get("files", {})

        # hash -> file_id
        hash_to_id = {b["name"]: b["id"] for b in self.storage.list_files_recursive(blobs_folder_id)}

        snapshot_folder_id = self.storage.get_or_create_folder(timestamp, snapshots_folder_id)

        def _materialise_one(item: tuple[str, str]) -> str:
            rel, sha = item
            if sha not in hash_to_id:
                raise RuntimeError(f"missing blob {sha} for {rel}")
            content = self.storage.download_file(hash_to_id[sha])
            parent_id, filename = self.storage.resolve_path(rel, snapshot_folder_id)
            self.storage.upload_bytes(
                content=content,
                name=filename,
                folder_id=parent_id,
                mimetype="application/octet-stream",
            )
            return rel

        items = list(path_to_hash.items())
        if items:
            workers = min(self.config.parallel_workers, len(items))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(_materialise_one, items))

        # Write meta sidecar.
        from .events import _truncate_files
        meta_bytes = json.dumps(
            {
                "timestamp": timestamp,
                "triggered_by": manifest.get("triggered_by", ""),
                "action": manifest.get("action", "migrated"),
                "files_changed": _truncate_files(manifest.get("files_changed", [])),
                "total_files": len(path_to_hash),
                "format": "full",
                "migrated_from": "blobs",
            },
            indent=2,
        ).encode()
        self.storage.upload_bytes(meta_bytes, SNAPSHOT_META_FILE, snapshot_folder_id)

        if not keep_source:
            # Surface deletion errors — see comment in _migrate_full_to_blobs.
            self.storage.delete_file(manifest_id)
