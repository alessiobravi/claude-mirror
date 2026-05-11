from __future__ import annotations

import dataclasses
import fnmatch
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional, cast

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from ._progress import (
    _SharedElapsedColumn,
    make_phase_progress,
    make_transfer_progress,
)
from ._constants import PARALLEL_WORKERS

from .backends import StorageBackend, redact_error
from .config import (
    Config,
    _DEFAULT_ROUTE_ACTIONS,
    _DEFAULT_ROUTE_PATHS,
)
from ._conflicts import (
    clear_envelope,
    is_eligible as _envelope_is_eligible,
    make_envelope,
    write_envelope,
)
from .events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from .hash_cache import HashCache
from .ignore import IgnoreSet, IGNORE_FILENAME
from .manifest import FileState, Manifest
from .merge import MergeHandler
from .notifications import NotificationBackend
from .retry import BackoffCoordinator, extract_retry_after_seconds
from .snapshots import SnapshotManager, SNAPSHOTS_FOLDER, BLOBS_FOLDER, _safe_join, _human_size

console = Console(force_terminal=True)


class Status(Enum):
    IN_SYNC = "in_sync"
    LOCAL_AHEAD = "local_ahead"   # only local changed → push
    DRIVE_AHEAD = "drive_ahead"   # only drive changed → pull
    CONFLICT = "conflict"          # both changed
    NEW_LOCAL = "new_local"        # new file only on local → push
    NEW_DRIVE = "new_drive"        # new file only on drive → pull
    DELETED_LOCAL = "deleted_local"  # removed locally, still on drive


STATUS_LABELS = {
    Status.IN_SYNC: ("[green]in sync[/]", ""),
    Status.LOCAL_AHEAD: ("[cyan]local ahead[/]", "→ push"),
    Status.DRIVE_AHEAD: ("[blue]drive ahead[/]", "← pull"),
    Status.CONFLICT: ("[red]conflict[/]", "! merge"),
    Status.NEW_LOCAL: ("[cyan]new local[/]", "→ push"),
    Status.NEW_DRIVE: ("[blue]new on drive[/]", "← pull"),
    Status.DELETED_LOCAL: ("[yellow]deleted local[/]", "→ delete on drive"),
}


@dataclass
class FileSyncState:
    rel_path: str
    status: Status
    local_hash: Optional[str]
    drive_hash: Optional[str]
    drive_file_id: Optional[str]
    local_size: Optional[int] = None    # bytes; None if file is remote-only or unstat-able
    drive_size: Optional[int] = None    # bytes; None if backend didn't expose size for this entry


@dataclass
class PushPlan:
    """Read-only preview of what `push()` would do, returned by
    `SyncEngine.push(dry_run=True)`. No backend writes, no manifest
    mutations, no notifications. The CLI renders this; the engine
    populates it from the same `get_status()` pass a real run uses.

    `to_upload` covers LOCAL_AHEAD + NEW_LOCAL (what a real push uploads
    cleanly). `to_delete` covers DELETED_LOCAL (mirrored deletion on the
    remote). `conflicts` covers CONFLICT — a real push prompts the user
    interactively, so dry-run only reports the count + paths and doesn't
    speculate which side would win. `skipped` covers DRIVE_AHEAD +
    NEW_DRIVE + IN_SYNC — files a push leaves alone.
    """
    to_upload: list[str]
    to_delete: list[str]
    conflicts: list[str]
    skipped: list[str]
    upload_bytes: int
    delete_count: int
    total_files: int


@dataclass
class PullPlan:
    """Read-only preview of what `pull()` would do, returned by
    `SyncEngine.pull(dry_run=True)`. No backend reads beyond listing,
    no local writes, no manifest mutations.

    `to_download` covers DRIVE_AHEAD + NEW_DRIVE (what a real pull writes
    locally). `skipped` covers everything else (LOCAL_AHEAD, NEW_LOCAL,
    DELETED_LOCAL, CONFLICT, IN_SYNC) — a real pull never touches those.
    """
    to_download: list[str]
    skipped: list[str]
    download_bytes: int
    total_files: int


@dataclass
class DeletePlan:
    """Read-only preview of what `delete FILES...` would do. No backend
    writes, no local unlinks, no manifest mutations, no notifications.

    `to_delete_remote` lists files that would be removed from the
    primary backend (and every Tier 2 mirror via the recorded per-backend
    file ids). `to_delete_local` lists files where `--local` was set AND
    the local copy exists on disk. `not_found` covers paths that don't
    exist on either side. `local_only` covers paths absent from the
    remote but present locally — they only get touched if `--local` is
    passed (otherwise they're silent skips by design).
    """
    to_delete_remote: list[str]
    to_delete_local: list[str]
    not_found: list[str]
    local_only: list[str]


class SyncEngine:
    def __init__(
        self,
        config: Config,
        storage: StorageBackend,
        manifest: Manifest,
        merge: MergeHandler,
        notifier: Optional[NotificationBackend] = None,
        snapshots: Optional[SnapshotManager] = None,
        mirrors: Optional[list[StorageBackend]] = None,
    ) -> None:
        self.config = config
        self.storage = storage    # primary backend (status / pull / authoritative remote state)
        self.manifest = manifest
        self.merge = merge
        self.notifier = notifier
        self.snapshots = snapshots
        # Tier 2: write-replica backends. Push/sync/delete fan out to all
        # mirrors after the primary call succeeds. Status, pull, and
        # conflict resolution remain primary-only by design (mirrors are
        # write-replicas, not authoritative sources). Empty list = single-
        # backend behaviour, fully back-compatible with v0.3.x.
        self._mirrors: list[StorageBackend] = list(mirrors or [])
        self._project = Path(config.project_path)
        self._folder_id = config.root_folder
        self._remote_log_cache: Optional[SyncLog] = None
        self._remote_log_loaded: bool = False
        self._remote_log_dirty: bool = False
        self._remote_log_file_id: Optional[str] = None
        self._remote_log_folder_id: Optional[str] = None
        self._hash_cache = HashCache(config.project_path)
        # Pub/Sub publish futures collected during a command and resolved at the end —
        # avoids inline blocking on broker ack for each event we publish.
        self._pending_publish_futures: list[Any] = []
        # Per-backend push-progress counters. _push_file and
        # _fan_out_to_mirrors increment these as each backend completes
        # its upload for a given file; the running total is rendered in
        # the "Pushing X/N" Progress row's detail string so the user can
        # see at a glance whether SFTP is the bottleneck or whether the
        # primary is also slow. Reset by `push()` before each invocation;
        # protected by a lock because uploads run in a ThreadPoolExecutor.
        self._push_counter_lock = threading.Lock()
        self._push_counters: dict[str, int] = {}
        self._push_counter_total: int = 0
        # Buffered per-file "↑ {path}" messages. Worker threads append
        # here during the Pushing phase instead of calling console.print
        # directly — printing into the live Progress region from many
        # threads triggered Rich-cursor drift that visibly duplicated
        # the Guard/Pushing rows on some terminals. The buffer is
        # flushed AFTER the Progress block exits in push()/sync() so
        # the user still sees every ↑ line, just below the cleaned-up
        # final summary instead of interleaved with the live region.
        # list.append is GIL-atomic in CPython — no lock needed.
        self._push_log_buffer: list[str] = []
        # Pre-compile exclude patterns into a single regex + a directory-prefix
        # tuple. _is_excluded is a hot path: status/push/pull each invoke it
        # once per local file, so on a 5K-file project with 10 exclude patterns
        # the old per-pattern fnmatch.fnmatch loop translated into ~150K regex
        # compilations per command. Doing the compile once here keeps the
        # per-call cost down to one regex.match plus a startswith-tuple check.
        # Behaviour matches the legacy three-form match (bare pattern,
        # pattern + "/*", and rel_path.startswith(pattern + "/")).
        if config.exclude_patterns:
            parts: list[str] = []
            for pattern in config.exclude_patterns:
                parts.append(fnmatch.translate(pattern))
                parts.append(fnmatch.translate(f"{pattern}/*"))
            self._exclude_re: Optional[re.Pattern[str]] = re.compile(
                "(?:" + "|".join(parts) + ")"
            )
            self._exclude_prefixes: tuple[str, ...] = tuple(
                f"{p}/" for p in config.exclude_patterns
            )
        else:
            self._exclude_re = None
            self._exclude_prefixes = ()

        # Project-tree gitignore-style rules from `.claude_mirror_ignore`.
        # Loaded once per command invocation (engine instance lifetime).
        # Independent of `exclude_patterns`: both layers must vote "keep"
        # for a file to be eligible. Returns None if the file is absent
        # or contains no usable rules — keeping the hot path branch-free.
        self._ignore_set: Optional[IgnoreSet] = IgnoreSet.from_file(
            self._project / IGNORE_FILENAME
        )

        # Shared backoff coordinator. Reset by push() / sync() / pull()
        # at the start of every command invocation; remains None when no
        # uploading command is running. When a backend signals
        # RATE_LIMIT_GLOBAL (server-wide 429), every in-flight upload
        # pauses on the same deadline rather than each retrying
        # independently and compounding the rate-limit pressure.
        self._coordinator: Optional[BackoffCoordinator] = None

        # Multi-channel notification routing precedence info (v0.5.50+).
        # When BOTH the legacy single-channel form AND the list-form
        # are configured for the same backend, the list-form wins and
        # the legacy field is silently dropped from dispatch. Surface
        # the override exactly once at engine construction so a user
        # mid-transition notices.
        for _backend, _legacy_field in (
            ("slack",   "slack_webhook_url"),
            ("discord", "discord_webhook_url"),
            ("teams",   "teams_webhook_url"),
            ("webhook", "webhook_url"),
        ):
            if self.config.has_legacy_routes_conflict(_backend):
                console.print(
                    f"[yellow]ignoring {_legacy_field} because "
                    f"{_backend}_routes is set[/]"
                )

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _is_excluded(self, rel_path: str) -> bool:
        """Return True if rel_path matches any exclude pattern.

        Combines two independent layers (both must vote "keep" for the
        path to pass):

          1. YAML `exclude_patterns` — the legacy fnmatch-based
             exclusion list, pre-compiled in __init__:
              * self._exclude_re — a single regex union of every exclude
                pattern in both `pattern` and `pattern/*` forms.
              * self._exclude_prefixes — `tuple(f"{p}/" for p in patterns)`,
                used with str.startswith to mirror the legacy
                `rel_path.startswith(f"{pattern}/")` check.
          2. `.claude_mirror_ignore` — gitignore-style rules loaded once
             per engine instance from the project root. The file itself
             is auto-excluded so the rules do not propagate to other
             machines unless the user explicitly wants them to.
        """
        # Layer 2a: auto-exclude the ignore-rules file itself, even when
        # no .claude_mirror_ignore file exists at the moment of the call —
        # this keeps the auto-exclusion semantics consistent regardless
        # of whether the user has added the file yet.
        if rel_path == IGNORE_FILENAME:
            return True

        # Layer 1: YAML exclude_patterns.
        if self._exclude_re is not None:
            if self._exclude_re.match(rel_path):
                return True
            if rel_path.startswith(self._exclude_prefixes):
                return True

        # Layer 2b: .claude_mirror_ignore rules. Defensive `getattr`
        # so engines built via the bare `SyncEngine.__new__` path used
        # by the perf-smoke and parity tests (which skip the regular
        # __init__ to focus on a single concern) don't blow up — for
        # those engines, no ignore set is configured, which is exactly
        # the right behaviour anyway.
        ignore_set = getattr(self, "_ignore_set", None)
        if ignore_set is not None and ignore_set.is_excluded(rel_path):
            return True

        return False

    def _local_files(self) -> list[str]:
        """Return relative paths of all local files matching configured patterns.

        Symlink policy (current behaviour, intentional — do not change here):
        ``Path.glob()`` follows symbolic links transparently. A symlinked file
        therefore shows up in the discovery set under its symlink path, and
        a symlinked directory has its contents traversed exactly like any
        other directory. Cycles are not detected; pathological symlink loops
        inside the project tree will deadlock this loop. If we ever switch
        to non-following semantics (e.g. via ``os.walk(..., followlinks=False)``
        or an explicit `Path.is_symlink` check), that's a behaviour change
        and must ship in its own version with a clear migration note —
        existing users may rely on symlinks resolving today.
        """
        found = set()
        for pattern in self.config.file_patterns:
            for path in self._project.glob(pattern):
                if path.is_file() and path.name != ".claude_mirror_manifest.json":
                    # `.as_posix()` (NOT `str()`) so manifest keys use
                    # forward slashes on Windows too — keys flow into
                    # remote storage paths and have to be cross-platform
                    # stable. A Windows machine writing `a\b.md` to the
                    # manifest would break sync against a Linux machine
                    # reading the same remote.
                    rel = path.relative_to(self._project).as_posix()
                    if not self._is_excluded(rel):
                        found.add(rel)
        return sorted(found)

    # ------------------------------------------------------------------
    # Status computation
    # ------------------------------------------------------------------

    def get_status(
        self,
        on_local: Optional[Callable[[str], None]] = None,
        on_remote: Optional[Callable[[str], None]] = None,
    ) -> list[FileSyncState]:
        """Compute per-file sync state. Remote listing and local hashing run concurrently;
        local hashing is backed by an mtime+size cache so unchanged files skip rehashing.

        `on_local` and `on_remote` are independent callbacks invoked with short status
        strings as each side progresses — used by show_status to render two live progress
        lines so the user can see which phase is the actual bottleneck.
        """
        _local  = on_local  or (lambda _msg: None)
        _remote = on_remote or (lambda _msg: None)

        _local("scanning project tree")
        local_files = set(self._local_files())
        _local(f"found {len(local_files)} local file(s)")

        def _list_remote() -> dict[str, Any]:
            _remote("connecting")

            def _cb(folders_done: int, files_seen: int) -> None:
                _remote(f"explored {folders_done} folder(s), {files_seen} file(s)")

            # Prune `_claude_mirror_snapshots/`, `_claude_mirror_blobs/`, and
            # `_claude_mirror_logs/` at the source — without this the BFS
            # walks every snapshot folder (full format) or every blob
            # (blobs format), and remote listing explodes.
            entries = self.storage.list_files_recursive(
                self._folder_id,
                progress_cb=_cb,
                exclude_folder_names={SNAPSHOTS_FOLDER, BLOBS_FOLDER, LOGS_FOLDER},
            )
            _remote(f"received {len(entries)} entries, filtering")
            filtered = {
                f["relative_path"]: f for f in entries
                if not f["name"].startswith("_")
                and not f["relative_path"].startswith(f"{SNAPSHOTS_FOLDER}/")
                and not f["relative_path"].startswith(f"{BLOBS_FOLDER}/")
                and not f["relative_path"].startswith(f"{LOGS_FOLDER}/")
                and not self._is_excluded(f["relative_path"])
            }
            _remote(f"done — {len(filtered)} file(s) in scope")
            return filtered

        def _hash_locals() -> dict[str, str]:
            paths = sorted(local_files)
            results: dict[str, str] = {}
            if not paths:
                _local("no local files to hash")
                return results

            _local(f"checking cache (0/{len(paths)})")
            misses: list[tuple[str, int, int]] = []
            for i, p in enumerate(paths, 1):
                try:
                    st = (self._project / p).stat()
                except OSError:
                    continue
                cached = self._hash_cache.get(p, st.st_size, st.st_mtime_ns)
                if cached is not None:
                    results[p] = cached
                else:
                    misses.append((p, st.st_size, st.st_mtime_ns))
                if i % 250 == 0:
                    _local(f"checking cache ({i}/{len(paths)})")

            cached_count = len(paths) - len(misses)
            if not misses:
                _local(f"all {cached_count} file(s) cached — done")
                return results

            _local(f"hashing {len(misses)} new file(s) (0/{len(misses)}, {cached_count} cached)")
            workers = min(self.config.parallel_workers, len(misses))
            done = 0

            def _hash(rel: str) -> str:
                return Manifest.hash_file(str(self._project / rel))

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_hash, rel): (rel, size, mtime_ns)
                        for rel, size, mtime_ns in misses}
                for fut in as_completed(futs):
                    rel, size, mtime_ns = futs[fut]
                    h = fut.result()
                    results[rel] = h
                    self._hash_cache.set(rel, size, mtime_ns, h)
                    done += 1
                    _local(f"hashing new file(s) ({done}/{len(misses)}, {cached_count} cached)")
            _local(f"done — {len(misses)} hashed, {cached_count} cached")
            return results

        with ThreadPoolExecutor(max_workers=2) as ex:
            remote_future = ex.submit(_list_remote)
            hash_future = ex.submit(_hash_locals)
            drive_files = remote_future.result()
            local_hashes = hash_future.result()

        manifest_entries = self.manifest.all()
        all_paths = local_files | set(drive_files) | set(manifest_entries)

        states = []
        for rel_path in sorted(all_paths):
            local_exists = rel_path in local_files
            drive_entry = drive_files.get(rel_path)
            manifest_entry = manifest_entries.get(rel_path)

            local_hash = local_hashes.get(rel_path) if local_exists else None
            drive_hash = drive_entry.get("md5Checksum") if drive_entry else None
            drive_file_id = drive_entry["id"] if drive_entry else (
                manifest_entry.remote_file_id if manifest_entry else None
            )

            # Sizes — used to render byte totals in show_status. Local size
            # comes from a stat (cheap; we already stat'd during hash check).
            # Remote size is only present for backends that expose it on
            # listing (Drive does; some others may return 0/missing).
            local_size: Optional[int] = None
            if local_exists:
                try:
                    local_size = (self._project / rel_path).stat().st_size
                except OSError:
                    local_size = None
            drive_size: Optional[int] = None
            if drive_entry:
                raw_size = drive_entry.get("size")
                if raw_size is not None:
                    try:
                        drive_size = int(raw_size)
                    except (TypeError, ValueError):
                        drive_size = None

            status = self._classify(
                local_hash, drive_hash, manifest_entry, local_exists, drive_entry is not None
            )
            states.append(FileSyncState(
                rel_path, status, local_hash, drive_hash, drive_file_id,
                local_size=local_size, drive_size=drive_size,
            ))

        self._hash_cache.prune(local_files)
        try:
            self._hash_cache.save()
        except OSError:
            pass

        return states

    def _classify(
        self,
        local_hash: Optional[str],
        drive_hash: Optional[str],
        manifest_entry: Optional[FileState],
        local_exists: bool,
        drive_exists: bool,
    ) -> Status:
        if not manifest_entry:
            if local_exists and not drive_exists:
                return Status.NEW_LOCAL
            if drive_exists and not local_exists:
                return Status.NEW_DRIVE
            if local_exists and drive_exists:
                # Both exist but never synced — treat as conflict
                return Status.CONFLICT
        else:
            synced = manifest_entry.synced_hash
            synced_remote = manifest_entry.synced_remote_hash or synced
            local_changed = local_hash != synced if local_exists else False
            drive_changed = drive_hash != synced_remote if drive_exists else False

            if not local_exists:
                return Status.DELETED_LOCAL
            if local_changed and drive_changed:
                return Status.CONFLICT
            if local_changed:
                return Status.LOCAL_AHEAD
            if drive_changed:
                return Status.DRIVE_AHEAD
        return Status.IN_SYNC

    # ------------------------------------------------------------------
    # Sync operations
    # ------------------------------------------------------------------

    def _make_phase_progress(self) -> Progress:
        """Build the multi-phase live Progress used by push/pull/sync/delete.

        Thin wrapper around the shared `_progress.make_phase_progress` so
        every command in the CLI renders progress identically — see
        feedback memory `feedback_progress_default.md`.
        """
        return make_phase_progress(console)

    def _sum_local_bytes(self, states: list[FileSyncState]) -> int:
        """Sum local-side bytes across the given states. Used to size the
        Pushing transfer-progress bar. Falls back to ``os.path.getsize``
        when ``state.local_size`` is None (a state path that the status
        pass couldn't stat — rare but possible if a worker raced an mtime
        change between hash and bar setup). Errors yield 0 — the bar
        will still render, it just won't be perfectly sized."""
        total = 0
        for s in states:
            if s.local_size is not None:
                total += s.local_size
                continue
            try:
                total += int(_safe_join(self._project, s.rel_path).stat().st_size)
            except (OSError, ValueError):
                continue
        return total

    def _sum_drive_bytes(self, states: list[FileSyncState]) -> int:
        """Sum remote-side bytes across the given states. Used to size
        the Pulling transfer-progress bar. ``state.drive_size`` is set
        when the backend exposed a `size` field on its listing entry
        (Drive does, OneDrive does, Dropbox does, WebDAV does, SFTP via
        stat). Missing entries fall through silently."""
        total = 0
        for s in states:
            if s.drive_size is not None:
                total += s.drive_size
        return total

    def _run_transfer_phase(
        self,
        items: list[Any],
        fn: Callable[..., Any],
        description: str,
        total_bytes: int,
        outer_progress: Optional[Progress] = None,
        extra_detail: Optional[Callable[[], str]] = None,
    ) -> tuple[list[Any], list[Any]]:
        """Run a byte-transfer phase under a Rich transfer-progress UI.

        ``fn(item, progress_callback)`` does the actual transfer for one
        item (``progress_callback(N)`` reports N bytes transferred since
        the last call). The callback is wired to a single batch-level
        task whose ``total`` is ``total_bytes`` — the user sees one
        aggregated bar with ETA + bytes/sec for the whole phase.

        ``outer_progress`` (when provided) is the surrounding multi-
        phase Progress that runs Local/Remote/Snapshot rows. Rich does
        not allow two simultaneous Live regions, so we ``stop()`` it
        before opening the transfer Progress and ``start()`` it again
        afterwards. Pattern mirrors the existing conflict-resolution
        block in ``push()`` / ``sync()``.

        ``total_bytes == 0`` (or no items) short-circuits to a no-op
        return — opening a Progress with an empty bar would just create
        visual noise.

        ``extra_detail`` is a callable returning a short status string
        (e.g. the per-backend "googledrive: 5/5 · sftp: 2/5" breakdown
        used by push). The string is appended to the row description
        after each completion so a slow mirror is visible while the
        primary bar fills.

        Thread-safety: ``progress.advance()`` is documented as
        thread-safe by Rich, so the parallel workers update the bar
        directly without an additional ``threading.Lock`` wrapper —
        see module docstring in ``_progress.py``.

        Returns ``(succeeded, failed)`` item lists, mirroring
        ``_parallel``'s contract so call sites can swap between the
        two with minimal change.
        """
        if not items or total_bytes <= 0:
            # Defer to the per-file _parallel path so we still record
            # one row for "nothing to do" / file-count progress when
            # there's no byte total to report (e.g. a manifest-only
            # state that needs no upload).
            if outer_progress is not None:
                task = outer_progress.add_task(
                    description, total=None,
                    detail="0 file(s)", show_time=True,
                )
                ok_inner, failed_inner = self._parallel(
                    items, fn, description=description,
                    progress=outer_progress, task_id=task,
                    extra_detail=extra_detail,
                )
                return ok_inner, failed_inner
            return self._parallel(
                items, fn, description=description,
                extra_detail=extra_detail,
            )

        was_running = False
        if outer_progress is not None:
            try:
                # Rich's Progress exposes `live.is_started`; fall back
                # to a defensive stop() either way — calling stop on a
                # not-started Progress is a no-op.
                was_running = bool(getattr(outer_progress, "live", None) and outer_progress.live.is_started)
            except Exception:
                was_running = True
            if was_running:
                outer_progress.stop()

        succeeded: list[Any] = []
        failed: list[Any] = []
        with make_transfer_progress(console) as tprog:
            task_id = tprog.add_task(description, total=total_bytes, show_time=True)

            def _advance(n: int) -> None:
                if n <= 0:
                    return
                tprog.advance(task_id, n)

            workers = max(1, min(self.config.parallel_workers, len(items)))
            if workers == 1 or len(items) == 1:
                for item in items:
                    label = getattr(item, "rel_path", str(item))
                    try:
                        fn(item, _advance)
                        succeeded.append(item)
                    except Exception as e:
                        tprog.console.print(f"  [red]✗ {label}: {e}[/]")
                        failed.append(item)
            else:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {ex.submit(fn, item, _advance): item for item in items}
                    for future in as_completed(futures):
                        item = futures[future]
                        label = getattr(item, "rel_path", str(item))
                        try:
                            future.result()
                            succeeded.append(item)
                        except Exception as e:
                            tprog.console.print(f"  [red]✗ {label}: {e}[/]")
                            failed.append(item)

            # Defensive: ensure the bar reads as complete on success even
            # if a backend's progress_callback under-reported (e.g. a
            # final chunk's delta got lost to a SDK quirk). Without this
            # the user sees "5.7/6.0 MB" frozen at the end.
            try:
                completed_now = int(tprog.tasks[0].completed) if tprog.tasks else 0
            except Exception:
                completed_now = 0
            if completed_now < total_bytes and not failed:
                tprog.update(task_id, completed=total_bytes)

        if outer_progress is not None and was_running:
            outer_progress.start()
        return succeeded, failed

    def _run_status_phase(self, progress: Progress) -> list[FileSyncState]:
        """Run get_status() inside an outer Progress, rendering Local/Remote
        as two rows that update independently. Rows are removed once the
        status pass completes so subsequent phases own the live region."""
        # Only the first row carries the shared timer; the second is blank
        # so we don't display two identical "0:01:28" timers side by side.
        local_task  = progress.add_task("Local",  total=None, detail="starting…", show_time=True)
        remote_task = progress.add_task("Remote", total=None, detail="starting…", show_time=False)

        def _on_local(msg: str) -> None:
            progress.update(local_task, detail=msg)

        def _on_remote(msg: str) -> None:
            progress.update(remote_task, detail=msg)

        try:
            return self.get_status(on_local=_on_local, on_remote=_on_remote)
        finally:
            progress.remove_task(local_task)
            progress.remove_task(remote_task)

    def show_status(self, short: bool = False) -> list[FileSyncState]:
        # Two simultaneous progress lines — one for local hashing, one for remote
        # listing — so the user can see which phase is actually slow rather than
        # guessing based on whichever happened to update its spinner last.
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description:<8}"),
            TextColumn("{task.fields[detail]}", style="dim"),
            _SharedElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            # Only the first row carries the shared timer.
            local_task  = progress.add_task("Local",  total=None, detail="starting…", show_time=True)
            remote_task = progress.add_task("Remote", total=None, detail="starting…", show_time=False)

            def _on_local(msg: str) -> None:
                progress.update(local_task, detail=msg)

            def _on_remote(msg: str) -> None:
                progress.update(remote_task, detail=msg)

            states = self.get_status(on_local=_on_local, on_remote=_on_remote)

        # Summary counts
        counts: dict[Status, int] = {}
        for s in states:
            counts[s.status] = counts.get(s.status, 0) + 1

        order = [
            Status.CONFLICT, Status.LOCAL_AHEAD, Status.DRIVE_AHEAD,
            Status.NEW_LOCAL, Status.NEW_DRIVE, Status.DELETED_LOCAL, Status.IN_SYNC,
        ]
        colors = {
            Status.CONFLICT:      "red",
            Status.LOCAL_AHEAD:   "cyan",
            Status.DRIVE_AHEAD:   "blue",
            Status.NEW_LOCAL:     "cyan",
            Status.NEW_DRIVE:     "blue",
            Status.DELETED_LOCAL: "yellow",
            Status.IN_SYNC:       "green",
        }
        labels = {
            Status.CONFLICT:      "conflict",
            Status.LOCAL_AHEAD:   "local ahead",
            Status.DRIVE_AHEAD:   "drive ahead",
            Status.NEW_LOCAL:     "new local",
            Status.NEW_DRIVE:     "new on drive",
            Status.DELETED_LOCAL: "deleted local",
            Status.IN_SYNC:       "in sync",
        }

        if not short:
            table = Table(title="Sync Status", show_header=True)
            table.add_column("File", style="white")
            table.add_column("Status")
            table.add_column("Action", style="dim")
            for s in states:
                label, action = STATUS_LABELS[s.status]
                table.add_row(s.rel_path, label, action)
            console.print(table)

        if not counts:
            console.print("[dim]No files found.[/]")
        elif all(s == Status.IN_SYNC for s in counts):
            console.print(f"[green]✓ All {counts[Status.IN_SYNC]} file(s) in sync.[/]")
        else:
            parts = []
            for status in order:
                if status in counts:
                    parts.append(f"[{colors[status]}]{counts[status]} {labels[status]}[/]")
            console.print("  " + "  ·  ".join(parts))

        # Size report — total project size + breakdown of pending bytes per
        # action category. Local size is from stat; drive size is from the
        # backend listing where available. We pick the most-relevant size
        # per state: pushes count local bytes (what's about to upload),
        # pulls count drive bytes (what's about to download), and conflicts
        # count whichever side is bigger so the user sees the largest cost.
        total_local_bytes = sum(s.local_size or 0 for s in states if s.local_size)
        total_local_files = sum(1 for s in states if s.local_size is not None)
        push_bytes = sum(
            (s.local_size or 0) for s in states
            if s.status in (Status.LOCAL_AHEAD, Status.NEW_LOCAL)
        )
        pull_bytes = sum(
            (s.drive_size or 0) for s in states
            if s.status in (Status.DRIVE_AHEAD, Status.NEW_DRIVE)
        )
        conflict_bytes = sum(
            max(s.local_size or 0, s.drive_size or 0)
            for s in states if s.status == Status.CONFLICT
        )
        size_parts: list[str] = []
        if total_local_files:
            size_parts.append(
                f"[dim]project: {total_local_files} file(s), "
                f"{_human_size(total_local_bytes)}[/]"
            )
        if push_bytes:
            size_parts.append(f"[cyan]↑ {_human_size(push_bytes)}[/]")
        if pull_bytes:
            size_parts.append(f"[blue]↓ {_human_size(pull_bytes)}[/]")
        if conflict_bytes:
            size_parts.append(f"[red]⚠ {_human_size(conflict_bytes)} (conflict)[/]")
        if size_parts:
            console.print("  " + "  ·  ".join(size_parts))

        return states

    def sync(
        self,
        *,
        non_interactive_strategy: Optional[str] = None,
    ) -> dict[str, Any]:
        """Bidirectional sync: auto-resolve non-conflicts in parallel, prompt for conflicts.

        non_interactive_strategy: when set (one of "keep-local" / "keep-remote"),
        every conflict is auto-resolved by the supplied policy and a one-line
        Rich note is printed per file. The interactive diff/prompt path is
        skipped entirely. Designed for `claude-mirror sync --no-prompt
        --strategy ...` running under cron / launchd / systemd. The handler
        passed to the engine MUST already be configured with the same
        strategy — the CLI does this in `_load_engine`.

        Returns a dict with the per-category counts the CLI uses to render
        the trailing one-line cron summary:
            {
                "in_sync": int,         # files that did not need any action
                "pushed": [str, ...],
                "pulled": [str, ...],
                "skipped": [str, ...],
                "deleted": [str, ...],
                "auto_resolved": [{"path": str, "strategy": str}, ...],
            }
        """
        pushed: list[str] = []
        pulled: list[str] = []
        skipped: list[str] = []
        deleted: list[str] = []
        # Audit trail for `--no-prompt --strategy ...`. Each entry is
        # `{"path": str, "strategy": str}`. Empty in the interactive flow.
        auto_resolved: list[dict[str, Any]] = []

        # Fresh coordinator per sync() so a previous run's throttle state
        # never leaks into the next invocation.
        self._coordinator = self._make_coordinator()

        with self._make_phase_progress() as progress:
            states = self._run_status_phase(progress)

            in_sync_count = sum(1 for s in states if s.status is Status.IN_SYNC)
            local_pushes = [s for s in states if s.status in (Status.LOCAL_AHEAD, Status.NEW_LOCAL)]
            pulls        = [s for s in states if s.status in (Status.DRIVE_AHEAD, Status.NEW_DRIVE)]
            existing_conflicts = [s for s in states if s.status == Status.CONFLICT]
            deletes      = [s for s in states if s.status == Status.DELETED_LOCAL]

            # Tier 2: expand pushes with any files pending on a mirror
            # from a previous run. (See identical comment in push().)
            mirror_pending = self._retry_pending_for_mirrors()
            if mirror_pending:
                console.print(
                    f"[dim]Retrying {len(mirror_pending)} file(s) "
                    f"pending on mirror(s) from previous run(s).[/]"
                )
                already_in_push = {s.rel_path for s in local_pushes}
                # O(1) lookup by rel_path — replaces a per-iteration linear
                # scan over `states` (was O(N×P) for N files × P pending,
                # P capped at PENDING_RETRY_QUEUE_CAP=200).
                states_by_path = {s.rel_path: s for s in states}
                for rel in mirror_pending:
                    if rel in already_in_push:
                        continue
                    state = states_by_path.get(rel)
                    if state is not None:
                        local_pushes.append(state)

            # Pulling
            ok, _ = self._run_transfer_phase(
                pulls,
                fn=lambda s, cb: self._pull_file(s, progress_callback=cb),
                description="Pulling",
                total_bytes=self._sum_drive_bytes(pulls),
                outer_progress=progress,
            )
            pulled.extend(s.rel_path for s in ok)

            # Guard checks
            guard_task = progress.add_task("Guard checks", total=None, detail="0 file(s)", show_time=True)
            safe_pushes, new_conflicts = self._parallel_guard_checks(
                local_pushes, progress=progress, task_id=guard_task,
            )

            # Pushing
            self._reset_push_counters(safe_pushes)
            ok, _ = self._run_transfer_phase(
                safe_pushes,
                fn=lambda s, cb: self._push_file(s, progress_callback=cb),
                description="Pushing",
                total_bytes=self._sum_local_bytes(safe_pushes),
                outer_progress=progress,
                extra_detail=self._format_push_breakdown,
            )
            pushed.extend(s.rel_path for s in ok)

            # Deletes
            if deletes:
                del_task = progress.add_task("Deletes", total=None, detail="0 file(s)", show_time=True)
                ok, _ = self._parallel(
                    deletes, self._delete_drive_file, description="Deletes",
                    progress=progress, task_id=del_task,
                )
                deleted.extend(s.rel_path for s in ok)

            # Conflicts. Two paths:
            #   * Interactive (default): pause the live region while
            #     `_resolve_conflict` prompts the user.
            #   * Non-interactive (`--no-prompt --strategy ...`): the
            #     MergeHandler is preconfigured with a policy, so every
            #     `_resolve_conflict` call returns instantly without
            #     touching stdin. We print a one-line yellow note per file
            #     so cron emails and journal logs show what was overwritten.
            all_conflicts = existing_conflicts + new_conflicts
            conflict_task = progress.add_task(
                "Conflicts", total=max(len(all_conflicts), 1),
                detail="no conflicts" if not all_conflicts else f"0/{len(all_conflicts)}", show_time=True)
            if all_conflicts:
                progress.stop()
                try:
                    for i, state in enumerate(all_conflicts, 1):
                        if state in new_conflicts and non_interactive_strategy is None:
                            entry = self.manifest.get(state.rel_path)
                            context = self._remote_change_context(
                                state.rel_path, since=entry.synced_at if entry else ""
                            )
                            console.print(
                                f"\n[yellow]⚠  Remote change detected:[/] [bold]{state.rel_path}[/]{context}\n"
                                "   Drive was updated since your last sync — merging before push."
                            )
                        action = self._resolve_conflict(state)
                        if action == "pushed":
                            pushed.append(state.rel_path)
                        elif action == "pulled":
                            pulled.append(state.rel_path)
                        else:
                            skipped.append(state.rel_path)
                        if non_interactive_strategy is not None:
                            # Per-file audit print + structured record.
                            # The flag combination (--no-prompt + --strategy)
                            # IS the user's consent for the destructive
                            # overwrite (see `feedback_destructive_safe_default`),
                            # so no extra typed-YES prompt — but we DO log
                            # unambiguously what just happened.
                            console.print(
                                f"[yellow]⚠[/]  {state.rel_path}: "
                                f"auto-resolved ({non_interactive_strategy})"
                            )
                            auto_resolved.append({
                                "path": state.rel_path,
                                "strategy": non_interactive_strategy,
                            })
                finally:
                    progress.start()
                    progress.update(
                        conflict_task,
                        completed=len(all_conflicts),
                        detail=f"{len(all_conflicts)}/{len(all_conflicts)} resolved",
                    )
            else:
                progress.update(conflict_task, completed=1)

            # Persist manifest before snapshot — see identical comment in
            # `push()` for the rationale (Ctrl+C during snapshot must not
            # lose the file-upload bookkeeping).
            self.manifest.save()

            # Snapshot
            snapshot_ts: Optional[str] = None
            snapshot_error: Optional[str] = None
            if pushed and self.snapshots:
                snap_task = progress.add_task("Snapshot", total=None, detail="creating snapshot…", show_time=True)
                try:
                    snapshot_ts = self.snapshots.create(action="sync", files_changed=pushed)
                    progress.update(snap_task, total=1, completed=1,
                                    detail=f"created {snapshot_ts}")
                except Exception as e:
                    snapshot_error = str(e)
                    progress.update(snap_task, total=1, completed=1,
                                    detail=f"FAILED: {snapshot_error[:60]}")

            # Notify
            notify_count = (1 if pushed and self.notifier else 0) + (1 if deleted and self.notifier else 0)
            if notify_count:
                notify_task = progress.add_task(
                    "Notify", total=notify_count,
                    detail=f"publishing {notify_count} event(s)…", show_time=True)
                if pushed and self.notifier:
                    # Carry the auto-resolution audit list on the same
                    # SyncEvent that already records the pushed files.
                    # Keeps the audit trail in lock-step with what was
                    # actually written to the remote in this run, in the
                    # SAME `_sync_log.json` interactive runs use — no
                    # separate auto-resolution-only log file.
                    self._publish_event(
                        pushed, "sync",
                        snapshot_ts=snapshot_ts,
                        auto_resolved_files=auto_resolved,
                    )
                    progress.update(notify_task, advance=1, detail="sync event sent")
                if deleted and self.notifier:
                    self._publish_event(deleted, "delete")
                    progress.update(notify_task, advance=1, detail="delete event sent")
                self._flush_publishes()
                progress.update(notify_task, detail="completed")
            else:
                self._flush_publishes()

            self.manifest.save()

        # See identical comment in `push()` — flush buffered ↑ lines
        # AFTER the Progress region has been torn down.
        self._flush_push_log()

        self._print_summary(pushed, pulled, skipped, deleted)
        # Surface the snapshot result outside the transient progress region.
        if snapshot_ts:
            console.print(f"[green]Snapshot created:[/] {snapshot_ts}")
        elif snapshot_error:
            console.print(
                f"[red]Snapshot creation failed:[/] {snapshot_error}\n"
                "[yellow]Sync succeeded; the failed snapshot will be retried "
                "on the next push/sync.[/]"
            )

        return {
            "in_sync": in_sync_count,
            "pushed": list(pushed),
            "pulled": list(pulled),
            "skipped": list(skipped),
            "deleted": list(deleted),
            "auto_resolved": list(auto_resolved),
        }

    def _plan_push(self, paths: Optional[list[str]] = None) -> PushPlan:
        """Compute the plan a real push() would execute, without uploading,
        deleting, snapshotting, or notifying.

        Runs the same status pipeline (`_run_status_phase`) so the user sees
        the live Local/Remote progress while the engine classifies each file.
        Returns a PushPlan the CLI renders. Mirrors the dry-run contract of
        `SnapshotManager.plan_restore()`: callers must treat the engine state
        (manifest, hash cache, backend) as read-only for the duration of the
        call. `prune()` on the hash-cache still happens via `get_status()` —
        that's a local-only optimisation, not a backend write.
        """
        with self._make_phase_progress() as progress:
            states = self._run_status_phase(progress)

        to_upload: list[str] = []
        to_delete: list[str] = []
        conflicts: list[str] = []
        skipped: list[str] = []
        upload_bytes = 0

        for s in states:
            if paths and s.rel_path not in paths:
                continue
            if s.status in (Status.LOCAL_AHEAD, Status.NEW_LOCAL):
                to_upload.append(s.rel_path)
                if s.local_size is not None:
                    upload_bytes += s.local_size
            elif s.status == Status.DELETED_LOCAL:
                to_delete.append(s.rel_path)
            elif s.status == Status.CONFLICT:
                conflicts.append(s.rel_path)
            else:
                skipped.append(s.rel_path)

        return PushPlan(
            to_upload=sorted(to_upload),
            to_delete=sorted(to_delete),
            conflicts=sorted(conflicts),
            skipped=sorted(skipped),
            upload_bytes=upload_bytes,
            delete_count=len(to_delete),
            total_files=len(states),
        )

    def _plan_pull(self, paths: Optional[list[str]] = None) -> PullPlan:
        """Compute the plan a real pull() would execute, without downloading
        anything to disk or mutating the manifest.

        Same contract as `_plan_push`: the status phase still runs (so the
        user sees live progress during classification), but every transfer
        / write side-effect is skipped.
        """
        with self._make_phase_progress() as progress:
            states = self._run_status_phase(progress)

        to_download: list[str] = []
        skipped: list[str] = []
        download_bytes = 0

        for s in states:
            if paths and s.rel_path not in paths:
                continue
            if s.status in (Status.DRIVE_AHEAD, Status.NEW_DRIVE):
                to_download.append(s.rel_path)
                if s.drive_size is not None:
                    download_bytes += s.drive_size
            else:
                skipped.append(s.rel_path)

        return PullPlan(
            to_download=sorted(to_download),
            skipped=sorted(skipped),
            download_bytes=download_bytes,
            total_files=len(states),
        )

    def push(
        self,
        paths: Optional[list[str]] = None,
        force_local: bool = False,
        *,
        dry_run: bool = False,
    ) -> Optional[PushPlan]:
        if dry_run:
            return self._plan_push(paths=paths)

        pushed: list[str] = []
        pulled: list[str] = []
        deleted: list[str] = []

        # Fresh coordinator per push() so a previous run's throttle state
        # never leaks into the next invocation.
        self._coordinator = self._make_coordinator()

        with self._make_phase_progress() as progress:
            states = self._run_status_phase(progress)

            to_push = [
                s for s in states
                if s.status in (Status.LOCAL_AHEAD, Status.NEW_LOCAL, Status.CONFLICT)
                and (not paths or s.rel_path in paths)
            ]
            to_delete = [
                s for s in states
                if s.status == Status.DELETED_LOCAL
                and (not paths or s.rel_path in paths)
            ]

            # Tier 2: expand the push queue with any files that previously
            # failed on a mirror (pending_retry). They get re-pushed on
            # this command even if they're locally unchanged, so mirrors
            # eventually catch up. The primary's manifest already reflects
            # the file as `ok` since the primary's previous push succeeded.
            mirror_pending = self._retry_pending_for_mirrors()
            if mirror_pending:
                console.print(
                    f"[dim]Retrying {len(mirror_pending)} file(s) "
                    f"pending on mirror(s) from previous run(s).[/]"
                )
                already_in_push = {s.rel_path for s in to_push}
                # O(1) lookup by rel_path — replaces a per-iteration linear
                # scan over `states` (was O(N×P) for N files × P pending,
                # P capped at PENDING_RETRY_QUEUE_CAP=200).
                states_by_path = {s.rel_path: s for s in states}
                for rel in mirror_pending:
                    if rel in already_in_push:
                        continue
                    # Synthesize a FileSyncState matching the current
                    # manifest entry so _push_file can proceed without a
                    # full status re-pass. The local hash is whatever the
                    # status phase already computed (or recomputed below).
                    state = states_by_path.get(rel)
                    if state is not None:
                        to_push.append(state)

            new_conflicts: list[FileSyncState] = []
            existing_conflicts: list[FileSyncState] = []

            if force_local:
                # Treat local as authoritative — no guard, no conflict prompts.
                self._reset_push_counters(to_push)
                ok, _ = self._run_transfer_phase(
                    to_push,
                    fn=lambda s, cb: self._push_file(s, progress_callback=cb),
                    description="Pushing",
                    total_bytes=self._sum_local_bytes(to_push),
                    outer_progress=progress,
                    extra_detail=self._format_push_breakdown,
                )
                pushed.extend(s.rel_path for s in ok)
            else:
                existing_conflicts = [s for s in to_push if s.status == Status.CONFLICT]
                local_pushes       = [s for s in to_push if s.status != Status.CONFLICT]

                # Guard checks
                guard_task = progress.add_task("Guard checks", total=None, detail="0 file(s)", show_time=True)
                safe_pushes, new_conflicts = self._parallel_guard_checks(
                    local_pushes, progress=progress, task_id=guard_task,
                )

                # Pushing
                self._reset_push_counters(safe_pushes)
                ok, _ = self._run_transfer_phase(
                    safe_pushes,
                    fn=lambda s, cb: self._push_file(s, progress_callback=cb),
                    description="Pushing",
                    total_bytes=self._sum_local_bytes(safe_pushes),
                    outer_progress=progress,
                    extra_detail=self._format_push_breakdown,
                )
                pushed.extend(s.rel_path for s in ok)

            # Deletes
            if to_delete:
                del_task = progress.add_task("Deletes", total=None, detail="0 file(s)", show_time=True)
                ok, _ = self._parallel(
                    to_delete, self._delete_drive_file, description="Deletes",
                    progress=progress, task_id=del_task,
                )
                deleted = [s.rel_path for s in ok]

            # Conflicts (interactive — pause the live region while prompting).
            all_conflicts = existing_conflicts + new_conflicts
            conflict_task = progress.add_task(
                "Conflicts", total=max(len(all_conflicts), 1),
                detail="no conflicts" if not all_conflicts else f"0/{len(all_conflicts)}", show_time=True)
            if all_conflicts:
                progress.stop()
                try:
                    for state in all_conflicts:
                        if state in new_conflicts:
                            entry = self.manifest.get(state.rel_path)
                            context = self._remote_change_context(
                                state.rel_path, since=entry.synced_at if entry else ""
                            )
                            console.print(
                                f"\n[yellow]⚠  Remote change detected:[/] [bold]{state.rel_path}[/]{context}\n"
                                "   Drive was updated since your last sync — merging before push."
                            )
                        action = self._resolve_conflict(state)
                        if action == "pushed":
                            pushed.append(state.rel_path)
                        elif action == "pulled":
                            pulled.append(state.rel_path)
                finally:
                    progress.start()
                    progress.update(
                        conflict_task,
                        completed=len(all_conflicts),
                        detail=f"{len(all_conflicts)}/{len(all_conflicts)} resolved",
                    )
            else:
                progress.update(conflict_task, completed=1)

            # Persist the manifest to disk NOW — before the snapshot
            # phase, which can take a long time on a fresh mirror with
            # thousands of unique blobs and is the most likely point of
            # interruption (Ctrl+C, network drop). Without this, the
            # in-memory per-file state recorded by _push_file/_fan_out
            # would be lost on interrupt and the same files would be
            # re-pushed on the next run, wasting bandwidth and breaking
            # the user's mental model. The file uploads are durable;
            # only the snapshot create is meaningfully retryable.
            self.manifest.save()

            # Snapshot
            snapshot_ts: Optional[str] = None
            snapshot_error: Optional[str] = None
            if pushed and self.snapshots:
                snap_task = progress.add_task("Snapshot", total=None, detail="creating snapshot…", show_time=True)
                try:
                    snapshot_ts = self.snapshots.create(action="push", files_changed=pushed)
                    progress.update(snap_task, total=1, completed=1,
                                    detail=f"created {snapshot_ts}")
                except Exception as e:
                    snapshot_error = str(e)
                    progress.update(snap_task, total=1, completed=1,
                                    detail=f"FAILED: {snapshot_error[:60]}")

            # Notify
            notify_count = (1 if pushed and self.notifier else 0) + (1 if deleted and self.notifier else 0)
            if notify_count:
                notify_task = progress.add_task(
                    "Notify", total=notify_count,
                    detail=f"publishing {notify_count} event(s)…", show_time=True)
                if pushed and self.notifier:
                    self._publish_event(pushed, "push", snapshot_ts=snapshot_ts)
                    progress.update(notify_task, advance=1, detail="push event sent")
                if deleted and self.notifier:
                    self._publish_event(deleted, "delete")
                    progress.update(notify_task, advance=1, detail="delete event sent")
                self._flush_publishes()
                progress.update(notify_task, detail="completed")
            else:
                self._flush_publishes()

            self.manifest.save()

        # Flush per-file ↑ messages buffered during the Pushing phase —
        # printed AFTER the Progress live region cleared, so they don't
        # interleave with Rich cursor management and produce duplicated
        # phase rows on certain terminals.
        self._flush_push_log()

        if pushed:
            console.print(f"[green]Pushed {len(pushed)} file(s).[/]")
        if pulled:
            console.print(f"[blue]Pulled {len(pulled)} file(s).[/]")
        if deleted:
            console.print(f"[yellow]Deleted {len(deleted)} file(s) from Drive.[/]")
        if not pushed and not pulled and not deleted:
            console.print("[dim]Nothing to push.[/]")
        # Surface the snapshot result OUTSIDE the transient progress region so
        # the user always sees explicit confirmation (or failure) — the live
        # phase row is wiped on exit and is too easy to miss.
        if snapshot_ts:
            console.print(f"[green]Snapshot created:[/] {snapshot_ts}")
        elif snapshot_error:
            console.print(
                f"[red]Snapshot creation failed:[/] {snapshot_error}\n"
                "[yellow]Push succeeded; the failed snapshot will be retried "
                "on the next push.[/]"
            )
        return None

    def pull(
        self,
        paths: Optional[list[str]] = None,
        output_dir: Optional[str] = None,
        *,
        dry_run: bool = False,
    ) -> Optional[PullPlan]:
        if dry_run:
            return self._plan_pull(paths=paths)

        pulled: list[str] = []

        with self._make_phase_progress() as progress:
            states = self._run_status_phase(progress)

            to_pull = [
                s for s in states
                if s.status in (Status.DRIVE_AHEAD, Status.NEW_DRIVE)
                and (not paths or s.rel_path in paths)
            ]

            if output_dir:
                output_path = Path(output_dir)
                action = lambda s, cb: self._pull_file_to(
                    s, output_path, progress_callback=cb,
                )
            else:
                action = lambda s, cb: self._pull_file(s, progress_callback=cb)

            # Pulling
            ok, _ = self._run_transfer_phase(
                to_pull, fn=action, description="Pulling",
                total_bytes=self._sum_drive_bytes(to_pull),
                outer_progress=progress,
            )
            pulled = [s.rel_path for s in ok]

            # Notify (only when pulling into the project, not --output)
            if pulled and not output_dir and self.notifier:
                notify_task = progress.add_task(
                    "Notify", total=1, detail="publishing pull event…", show_time=True)
                self._publish_event(pulled, "pull")
                self._flush_publishes()
                progress.update(notify_task, advance=1, detail="completed")
            else:
                self._flush_publishes()

            if not output_dir:
                self.manifest.save()

        if pulled:
            dest = output_dir or self.config.project_path
            console.print(f"[blue]Pulled {len(pulled)} file(s) → {dest}[/]")
        else:
            console.print("[dim]Nothing to pull.[/]")
        return None

    # ------------------------------------------------------------------
    # Internal operations
    # ------------------------------------------------------------------

    def _load_remote_log(self) -> Optional[SyncLog]:
        """Fetch the remote sync log once per operation and cache it.

        Auth/transport errors are NOT swallowed silently — they bubble up to
        `_CLIGroup` for a clean user-facing message. Only "log file is missing"
        (which is normal on a fresh project) is handled by returning None.
        """
        if self._remote_log_loaded:
            return self._remote_log_cache
        self._remote_log_loaded = True
        self._remote_log_folder_id = self.storage.get_file_id(LOGS_FOLDER, self._folder_id)
        if self._remote_log_folder_id:
            self._remote_log_file_id = self.storage.get_file_id(
                SYNC_LOG_NAME, self._remote_log_folder_id
            )
            if self._remote_log_file_id:
                self._remote_log_cache = SyncLog.from_bytes(
                    self.storage.download_file(self._remote_log_file_id)
                )
        return self._remote_log_cache

    def _remote_change_context(self, rel_path: str, since: str = "") -> str:
        """Return a Rich-formatted string describing who last changed rel_path on another machine."""
        log = self._load_remote_log()
        if not log:
            return ""
        for event in reversed(log.events):
            if rel_path not in event.files:
                continue
            if event.machine == self.config.machine_name:
                continue
            if since and event.timestamp <= since:
                break
            ts = event.timestamp[:19].replace("T", " ")
            return f" — last changed by [bold]{event.user}@{event.machine}[/] at {ts}"
        return ""

    def _guard_check(self, state: FileSyncState) -> bool:
        """
        Read-only pre-push check: verify the remote hash matches our last-synced
        baseline. Returns False if the remote has changed since our last sync
        (state is downgraded to CONFLICT in place); True if it is safe to push.

        Optimization: we already fetched the remote hash during get_status, so
        prefer state.drive_hash and only fall back to a fresh API call if it
        is missing.
        """
        manifest_entry = self.manifest.get(state.rel_path)
        if state.drive_file_id and manifest_entry:
            current_hash = state.drive_hash
            if current_hash is None:
                current_hash = self.storage.get_file_hash(state.drive_file_id)
            synced_remote = manifest_entry.synced_remote_hash or manifest_entry.synced_hash
            if current_hash and current_hash != synced_remote:
                state.drive_hash = current_hash
                state.status = Status.CONFLICT
                return False
        return True

    def _guard_push(self, state: FileSyncState) -> bool:
        """
        Re-verify the remote file hash immediately before uploading.
        If the remote changed since our last sync, intercept and route to
        interactive conflict resolution instead of overwriting.
        Returns True if the file was pushed (directly or after merge), False if skipped.
        """
        if not self._guard_check(state):
            entry = self.manifest.get(state.rel_path)
            since = entry.synced_at if entry else ""
            context = self._remote_change_context(state.rel_path, since=since)
            console.print(
                f"\n[yellow]⚠  Remote change detected:[/] [bold]{state.rel_path}[/]{context}\n"
                "   Drive was updated since your last sync — merging before push."
            )
            return self._resolve_conflict(state) is not None
        self._push_file(state)
        return True

    def _parallel(
        self,
        items: list[Any],
        fn: Callable[..., Any],
        description: str = "Working",
        progress: Optional[Progress] = None,
        task_id: Optional[TaskID] = None,
        extra_detail: Optional[Callable[[], str]] = None,
    ) -> tuple[list[Any], list[Any]]:
        """Run fn(item) for each item in parallel with a live progress bar.
        Returns (succeeded, failed) item lists.

        If `progress` is provided, the work is rendered as a row in that
        existing Progress (used by push/pull/sync to compose multi-phase
        live displays). Otherwise a transient Progress is created locally.
        If `task_id` is also provided, that pre-existing task is reused
        and updated rather than creating a new one.

        `extra_detail` (optional callable) returns a short string that
        gets appended to the row's detail after each completion — used
        by the push pipeline to render per-backend breakdowns like
        "(googledrive: 5/5 · sftp: 1/5)" so the user can see which
        backend is the bottleneck during a multi-backend fan-out.
        """
        def _decorate(detail: str) -> str:
            """Append the extra_detail breakdown when the caller provided one."""
            if extra_detail is None:
                return detail
            try:
                extra = extra_detail()
            except Exception:
                extra = ""
            if not extra:
                return detail
            return f"{detail}  ({extra})"

        if not items:
            if progress is not None and task_id is not None:
                progress.update(task_id, total=0, completed=0,
                                detail="nothing to do")
            return [], []
        if len(items) == 1:
            item = items[0]
            if progress is not None and task_id is not None:
                progress.update(task_id, total=1, completed=0,
                                detail=getattr(item, "rel_path", str(item)))
            try:
                fn(item)
                if progress is not None and task_id is not None:
                    progress.update(task_id, advance=1, detail=_decorate("completed"))
                return [item], []
            except Exception as e:
                label = getattr(item, "rel_path", str(item))
                out = progress.console if progress is not None else console
                out.print(f"  [red]✗ {label}: {e}[/]")
                if progress is not None and task_id is not None:
                    progress.update(task_id, advance=1, detail=_decorate("failed"))
                return [], [item]

        succeeded: list[Any] = []
        failed: list[Any] = []
        workers = min(self.config.parallel_workers, len(items))

        # Render either inside a caller-supplied Progress (composed dual-line
        # view) or in our own transient Progress.
        owns_progress = progress is None
        owns_task = False
        if owns_progress:
            progress = Progress(
                SpinnerColumn(),
                TextColumn("[bold]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
                transient=True,
            )
            progress.start()
        assert progress is not None
        try:
            if task_id is None:
                task_id = progress.add_task(description, total=len(items), show_time=True)
                owns_task = True
            else:
                progress.update(task_id, total=len(items), completed=0,
                                detail=f"0/{len(items)}")
            done = 0
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(fn, item): item for item in items}
                for future in as_completed(futures):
                    item = futures[future]
                    label = getattr(item, "rel_path", str(item))
                    try:
                        future.result()
                        succeeded.append(item)
                    except Exception as e:
                        progress.console.print(f"  [red]✗ {label}: {e}[/]")
                        failed.append(item)
                    done += 1
                    # Show the running count only — the per-file path is already
                    # printed above the live region by _push_file/_pull_file/etc,
                    # and including it here causes the description column to be
                    # truncated when the path is long (`Guard chec…`).
                    progress.update(
                        task_id,
                        advance=1,
                        detail=_decorate(f"{done}/{len(items)}"),
                    )
            if owns_task:
                progress.remove_task(task_id)
            else:
                # Caller owns the row; mark it completed so the final state is
                # readable rather than a stale per-item count.
                progress.update(task_id, detail=_decorate("completed"))
        finally:
            if owns_progress:
                progress.stop()
        return succeeded, failed

    def _parallel_guard_checks(
        self,
        states: list[FileSyncState],
        progress: Optional[Progress] = None,
        task_id: Optional[TaskID] = None,
    ) -> tuple[list[FileSyncState], list[FileSyncState]]:
        """Run guard checks in parallel. Returns (safe_pushes, new_conflicts).

        If `progress` and `task_id` are provided, the row is updated live with
        progress counts; otherwise the call is silent (legacy behavior).
        """
        if not states:
            if progress is not None and task_id is not None:
                progress.update(task_id, total=0, completed=0,
                                detail="nothing to check")
            return [], []
        safe: list[Any] = []
        conflicts: list[Any] = []
        workers = min(self.config.parallel_workers, len(states))
        if progress is not None and task_id is not None:
            progress.update(task_id, total=len(states), completed=0,
                            detail=f"0/{len(states)}")
        done = 0
        out = progress.console if progress is not None else console
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(self._guard_check, s): s for s in states}
            for future in as_completed(futures):
                state = futures[future]
                try:
                    (safe if future.result() else conflicts).append(state)
                except Exception as e:
                    out.print(f"  [red]✗ guard check {state.rel_path}: {e}[/]")
                    conflicts.append(state)
                done += 1
                if progress is not None and task_id is not None:
                    progress.update(
                        task_id,
                        advance=1,
                        detail=f"{done}/{len(states)}",
                    )
        if progress is not None and task_id is not None:
            progress.update(task_id, detail="completed")
        return safe, conflicts

    def _reset_push_counters(self, items: list[Any]) -> None:
        """Initialise the per-backend completion counters before a push
        run. Total is the file count; one counter per configured backend
        (primary + every mirror). Initial values are 0 so the first
        rendered detail reads "googledrive: 0/N · sftp: 0/N". Also
        clears the per-file ↑ message buffer (flushed by the caller
        after the Progress block exits)."""
        primary_name = (
            getattr(self.storage, "backend_name", "") or self.config.backend
        )
        names = [primary_name] + [
            (getattr(b, "backend_name", "") or "mirror") for b in self._mirrors
        ]
        with self._push_counter_lock:
            self._push_counter_total = len(items)
            self._push_counters = {name: 0 for name in names}
        self._push_log_buffer = []

    def _flush_push_log(self) -> None:
        """Emit every buffered ↑ line as console output. Called by
        push()/sync() AFTER the live Progress region exits so the
        prints don't fight with Rich's cursor management."""
        for line in self._push_log_buffer:
            console.print(line)
        self._push_log_buffer = []

    def _bump_push_counter(self, backend_name: str) -> None:
        """Increment the per-backend completion counter and refresh the
        active push Progress row's detail string. Thread-safe — called
        from worker threads."""
        with self._push_counter_lock:
            self._push_counters[backend_name] = (
                self._push_counters.get(backend_name, 0) + 1
            )

    def _format_push_breakdown(self) -> str:
        """Build the per-backend "name: X/N" breakdown string consumed
        by `_parallel`'s extra_detail callback. Read under lock so a
        concurrent _bump can't produce a partial dict view."""
        with self._push_counter_lock:
            total = self._push_counter_total
            return " · ".join(
                f"{name}: {self._push_counters.get(name, 0)}/{total}"
                for name in self._push_counters
            )

    # ------------------------------------------------------------------
    # Global rate-limit coordination (RATE_LIMIT_GLOBAL)
    # ------------------------------------------------------------------

    def _make_coordinator(self) -> BackoffCoordinator:
        """Build a fresh BackoffCoordinator for the current command.

        Wires the user-facing "Backend reports rate limit. Pausing Ns..."
        and "Throttle cleared. Resuming uploads." messages so a flurry of
        per-file 429s collapses to ONE calm pair of lines. Honours
        `config.max_throttle_wait_seconds` for the hard cap (default 600s,
        cron jobs can lower it for fail-fast behaviour).
        """
        cap = float(getattr(self.config, "max_throttle_wait_seconds", 600.0))

        def _on_start(wait_seconds: float) -> None:
            console.print(
                f"  [yellow]⚠[/]  Backend reports rate limit. "
                f"Pausing {int(wait_seconds)}s before retrying."
            )

        def _on_clear() -> None:
            console.print("  [dim]Throttle cleared. Resuming uploads.[/]")

        return BackoffCoordinator(
            max_wait_seconds=cap,
            on_throttle_start=_on_start,
            on_throttle_clear=_on_clear,
        )

    def _upload_with_coordinator(
        self,
        upload_callable: Callable[[], str],
        backend: StorageBackend,
    ) -> str:
        """Run an upload through the shared backoff coordinator.

        `upload_callable` performs ONE upload attempt and returns the
        resulting file ID. The wrapper waits if a global throttle is
        active, invokes the callable, and on RATE_LIMIT_GLOBAL signals
        the coordinator + loops up to `config.max_retry_attempts` times.
        Other classifications and successful uploads are returned /
        re-raised unchanged.
        """
        from .backends import BackendError, ErrorClass

        coord = self._coordinator
        if coord is None:
            return upload_callable()

        max_attempts = int(getattr(self.config, "max_retry_attempts", 3))
        if max_attempts < 1:
            max_attempts = 1

        last_exc: Optional[BaseException] = None
        for attempt in range(max_attempts):
            coord.wait_if_throttled()
            try:
                return upload_callable()
            except BackendError as be:
                if be.error_class is ErrorClass.RATE_LIMIT_GLOBAL:
                    coord.signal_rate_limit(extract_retry_after_seconds(be))
                    last_exc = be
                    continue
                raise
            except Exception as exc:
                cls = backend.classify_error(exc)
                if cls is ErrorClass.RATE_LIMIT_GLOBAL:
                    coord.signal_rate_limit(extract_retry_after_seconds(exc))
                    last_exc = exc
                    continue
                raise

        raise BackendError(
            ErrorClass.RATE_LIMIT_GLOBAL,
            f"upload deferred by global rate limit after {max_attempts} attempts: {last_exc}",
            backend_name=getattr(backend, "backend_name", "") or "",
            cause=last_exc,
        ) from None

    def _push_file(
        self,
        state: FileSyncState,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        # _safe_join refuses manifest keys that try to escape the project
        # root — a defence against a corrupted or malicious manifest.
        local_path = str(_safe_join(self._project, state.rel_path))
        # Primary push first — its success is the gate for "push succeeded".
        # If it fails, propagate the exception (caller treats this as a
        # failed push, no manifest update). If it succeeds, fan out to
        # mirrors using their classified retry adapter.
        # progress_callback (when provided) flows down to the primary
        # backend's upload_file so the transfer-progress bar advances
        # with bytes-since-last-call deltas. Mirrors don't get a
        # callback — the bar is sized by primary bytes only so the
        # "X/Y MB" figure matches what `status` reports as the push
        # cost and never overshoots 100%.
        #
        # The upload is wrapped in the shared backoff coordinator so a
        # global 429 (Drive's `userRateLimitExceeded`, Dropbox's
        # `too_many_requests`, OneDrive's 429+Retry-After, etc.) pauses
        # every other in-flight upload via the same deadline rather than
        # each retrying independently.
        def _do_primary_upload() -> str:
            return self.storage.upload_file(
                local_path=local_path,
                rel_path=state.rel_path,
                root_folder_id=self._folder_id,
                file_id=state.drive_file_id,
                progress_callback=progress_callback,
            )

        file_id = self._upload_with_coordinator(_do_primary_upload, self.storage)
        remote_hash = self.storage.get_file_hash(file_id)
        primary_name = (
            getattr(self.storage, "backend_name", "") or self.config.backend
        )
        # `_push_file` is only called when the local file exists, so
        # local_hash is guaranteed populated by the status pipeline.
        assert state.local_hash is not None
        # Update manifest's flat fields + record primary as ok in remotes map.
        self.manifest.update(
            state.rel_path, state.local_hash, file_id,
            synced_remote_hash=remote_hash or state.local_hash,
            backend_name=primary_name,
        )
        self._bump_push_counter(primary_name)
        # Buffer the ↑ line; flushed after the Progress block exits.
        self._push_log_buffer.append(f"  [cyan]↑[/] {state.rel_path}")

        # Tier 2: fan out to write-replica mirrors. Failures are recorded
        # per-backend in the manifest as `pending_retry` (transient/UNKNOWN)
        # or `failed_perm` (auth/quota/permission); they do NOT abort the
        # primary success. The next push retries pending entries; permanent
        # failures await user action (e.g. `claude-mirror auth --backend X`).
        if self._mirrors:
            self._fan_out_to_mirrors(
                state.rel_path, local_path, state.local_hash,
            )

    def _fan_out_to_mirrors(
        self,
        rel_path: str,
        local_path: str,
        local_hash: str,
    ) -> None:
        """Push one file to every mirror backend in parallel. Failures are
        recorded per-backend in the manifest, never raised — the primary
        push has already succeeded by the time we get here."""
        from .backends import BackendError, ErrorClass

        def _push_one(backend: StorageBackend) -> tuple[str, bool, Optional[str], Optional[ErrorClass]]:
            name = getattr(backend, "backend_name", "") or "unknown"
            # Look up the file ID this mirror has used previously, if any.
            existing = self.manifest.get(rel_path)
            mirror_file_id: Optional[str] = None
            if existing and name in existing.remotes:
                mirror_file_id = existing.remotes[name].remote_file_id or None
            try:
                # Use the classified retry adapter when available; fall back
                # to plain upload_file for backends that haven't shipped
                # the retry helper yet. Wrap in the shared backoff
                # coordinator so a global 429 on this mirror pauses every
                # other in-flight upload (primary + sibling mirrors)
                # rather than each retrying independently.
                upload_fn = getattr(backend, "_upload_with_retry", None)
                if upload_fn:
                    def _do_mirror_upload() -> str:
                        return cast(str, upload_fn(
                            local_path=local_path,
                            rel_path=rel_path,
                            root_folder_id=backend.config.root_folder,  # type: ignore[attr-defined]  # backends carry a `config` attribute by convention; not on the abstract base
                            file_id=mirror_file_id,
                        ))
                else:
                    def _do_mirror_upload() -> str:
                        return backend.upload_file(
                            local_path=local_path,
                            rel_path=rel_path,
                            root_folder_id=backend.config.root_folder,  # type: ignore[attr-defined]  # backends carry a `config` attribute by convention; not on the abstract base
                            file_id=mirror_file_id,
                        )
                new_id = self._upload_with_coordinator(_do_mirror_upload, backend)
                # Record success in manifest.
                try:
                    remote_hash = backend.get_file_hash(new_id) or local_hash
                except Exception:
                    remote_hash = local_hash
                self.manifest.update_remote(
                    rel_path, name,
                    remote_file_id=new_id,
                    synced_remote_hash=remote_hash,
                    state="ok",
                )
                self._bump_push_counter(name)
                return (name, True, None, None)
            except BackendError as be:
                # Already classified — record per-class state.
                cls = be.error_class
                state_str = (
                    "pending_retry" if cls.is_retryable else "failed_perm"
                )
                self.manifest.update_remote(
                    rel_path, name,
                    state=state_str,
                    last_error=redact_error(f"{cls.value}: {be}"),
                    intended_hash=local_hash,
                )
                return (name, False, str(be), cls)
            except Exception as exc:
                # Unclassified raw exception — let the backend classify it.
                cls = backend.classify_error(exc)
                state_str = (
                    "pending_retry" if cls.is_retryable else "failed_perm"
                )
                self.manifest.update_remote(
                    rel_path, name,
                    state=state_str,
                    last_error=redact_error(f"{cls.value}: {exc}"),
                    intended_hash=local_hash,
                )
                return (name, False, str(exc), cls)

        if not self._mirrors:
            return
        workers = min(self.config.parallel_workers, len(self._mirrors))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_push_one, b) for b in self._mirrors]
            for fut in as_completed(futs):
                name, ok, err, cls = fut.result()
                if ok:
                    # Buffer alongside the primary's ↑; flushed after the
                    # Progress region exits to avoid Rich-cursor drift.
                    self._push_log_buffer.append(
                        f"  [cyan]↑[/] {rel_path} [dim](mirror: {name})[/]"
                    )
                else:
                    color = "yellow" if (cls and cls.is_retryable) else "red"
                    console.print(
                        f"  [{color}]⚠[/] {rel_path} [dim](mirror {name}: "
                        f"{cls.value if cls else 'error'} — {err[:80] if err else ''})[/]"
                    )

    def _pull_file(
        self,
        state: FileSyncState,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        # Refuse to write outside the project root, even if the remote metadata
        # claims a relative_path with `..` segments.
        local_path = _safe_join(self._project, state.rel_path)
        # `_pull_file` is only called for states the status pipeline
        # classified as REMOTE_NEWER / NEW_DRIVE / DELETED_LOCAL — all
        # paths require drive_file_id to be populated.
        assert state.drive_file_id is not None
        content = self.storage.download_file(
            state.drive_file_id, progress_callback=progress_callback,
        )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(content)
        synced_hash = Manifest.hash_bytes(content)
        self.manifest.update(state.rel_path, synced_hash, state.drive_file_id,
                             synced_remote_hash=state.drive_hash or synced_hash)
        console.print(f"  [blue]↓[/] {state.rel_path}")
        # Mirrors now hold the OLD bytes. Without flipping them to
        # pending_retry, the next push sees mirrors-look-ok (their
        # synced_remote_hash still matches their stored content) and
        # skips fan-out — mirrors keep the OLD bytes forever. Marking
        # each mirror as pending_retry with the new local hash makes
        # the next push fan out the pulled content to catch them up.
        for backend in self._mirrors:
            name = getattr(backend, "backend_name", "") or ""
            if name:
                self.manifest.update_remote(
                    state.rel_path, name,
                    state="pending_retry",
                    intended_hash=synced_hash,
                    last_error="local file changed via pull; mirror needs catch-up",
                )

    def _pull_file_to(
        self,
        state: FileSyncState,
        output_dir: Path,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> None:
        """Download a remote file to output_dir without touching the project or manifest."""
        dest = _safe_join(output_dir, state.rel_path)
        # Same precondition as `_pull_file`: pull-target states always
        # carry a populated drive_file_id by construction.
        assert state.drive_file_id is not None
        content = self.storage.download_file(
            state.drive_file_id, progress_callback=progress_callback,
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        console.print(f"  [blue]↓[/] {state.rel_path} → {dest}")

    def _resolve_conflict(self, state: FileSyncState) -> Optional[str]:
        """
        Interactively resolve a conflict.
        Returns 'pushed', 'pulled', or None if skipped.

        AGENT-MERGE: BEFORE the interactive prompt fires, every
        text-file conflict gets a structured envelope written to
        `~/.local/state/claude-mirror/<project-slug>/conflicts/`. The
        envelope is information ALSO STORED for the running LLM agent
        (Claude Code, Cursor, Codex, …) to pick up via the skill —
        existing interactive behaviour is unchanged. The envelope is
        cleared if the user picks `keep-local` / `keep-remote` /
        `editor` (the conflict is resolved by the engine), and persists
        on `skip` so the agent can still help later. Binary-file
        conflicts skip the envelope write entirely; the prompt fires
        unchanged.
        """
        local_path = _safe_join(self._project, state.rel_path)
        local_bytes = local_path.read_bytes()
        # Conflict path implies the remote already has a record of the file.
        assert state.drive_file_id is not None
        drive_bytes = self.storage.download_file(state.drive_file_id)

        # AGENT-MERGE envelope write — additive, never blocks the prompt.
        # Failures here are logged and swallowed: the user's interactive
        # path must keep working even if the state directory is somehow
        # unwritable (read-only home, full disk, etc.).
        if _envelope_is_eligible(local_bytes, drive_bytes):
            try:
                local_text_for_env = local_bytes.decode("utf-8", errors="replace")
                drive_text_for_env = drive_bytes.decode("utf-8", errors="replace")
                manifest_entry = self.manifest.get(state.rel_path)
                base_hash = manifest_entry.synced_hash if manifest_entry else None
                env = make_envelope(
                    rel_path=state.rel_path,
                    local_text=local_text_for_env,
                    remote_text=drive_text_for_env,
                    base_text=None,
                    base_hash=base_hash or None,
                    project_path=self._project,
                    backend=self.storage.backend_name,
                )
                env_path = write_envelope(env, project_path=self._project)
                console.print(
                    f"  [dim][envelope][/] {state.rel_path} → {env_path}"
                )
            except Exception as exc:
                # Never fail the conflict resolver because envelope
                # plumbing failed. Surface a one-line warning so the
                # user knows the agent-handoff didn't get written.
                console.print(
                    f"  [yellow]⚠[/] {state.rel_path}: envelope write "
                    f"failed ({exc}); continuing with interactive prompt."
                )

        local_content = local_bytes.decode(errors="replace")
        drive_content = drive_bytes.decode(errors="replace")

        result = self.merge.resolve_conflict(state.rel_path, local_content, drive_content)
        if result is None:
            console.print(f"  [dim]Skipped {state.rel_path}[/]")
            # AGENT-MERGE: leave the envelope on disk on skip so the
            # agent can still pick it up later via `conflict list`.
            return None

        resolved_content, winner = result
        local_path.write_text(resolved_content)

        if winner == "drive":
            # Remote version is authoritative — update local file and manifest, no push needed
            synced_hash = Manifest.hash_file(str(local_path))
            self.manifest.update(state.rel_path, synced_hash, state.drive_file_id,
                                 synced_remote_hash=state.drive_hash or synced_hash)
            console.print(f"  [blue]↓[/] {state.rel_path} (kept drive version)")
            # Fan out the drive-winner content to mirrors so they don't
            # silently keep the OLD bytes. Without this, the next push
            # sees primary as in-sync and skips fan-out, leaving mirrors
            # divergent. Mirror failures are recorded per-backend by the
            # fan-out helper (pending_retry / failed_perm) and never
            # abort conflict resolution.
            if self._mirrors:
                self._fan_out_to_mirrors(
                    state.rel_path, str(local_path), synced_hash,
                )
            # AGENT-MERGE: conflict resolved — clear the envelope so
            # `conflict list` doesn't keep showing it as pending.
            clear_envelope(self._project, state.rel_path)
            return "pulled"
        else:
            # Local or merged — push the resolved content to Drive
            state.local_hash = Manifest.hash_file(str(local_path))
            self._push_file(state)
            # AGENT-MERGE: conflict resolved — clear the envelope.
            clear_envelope(self._project, state.rel_path)
            return "pushed"

    def _delete_drive_file(self, state: FileSyncState) -> None:
        # Primary delete first.
        if state.drive_file_id:
            self.storage.delete_file(state.drive_file_id)
        # Mirror deletes — best-effort, classified, never abort.
        if self._mirrors:
            existing = self.manifest.get(state.rel_path)
            if existing and existing.remotes:
                for backend in self._mirrors:
                    name = getattr(backend, "backend_name", "") or "unknown"
                    rs = existing.remotes.get(name)
                    if not rs or not rs.remote_file_id:
                        continue
                    try:
                        backend.delete_file(rs.remote_file_id)
                    except Exception as exc:
                        cls = backend.classify_error(exc)
                        console.print(
                            f"  [yellow]⚠[/] {state.rel_path} delete on "
                            f"mirror {name} failed ({cls.value}: {exc})"
                        )
                        # The manifest entry is about to be removed entirely
                        # below, so a per-mirror leftover here gets cleaned
                        # by the next gc / orphan sweep on that backend.
        self.manifest.remove(state.rel_path)
        console.print(f"  [yellow]✗[/] {state.rel_path} (deleted on drive)")

    def retry_mirrors(
        self,
        backend_filter: Optional[str] = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Re-attempt previously-failed mirror pushes (state="pending_retry").

        Walks the manifest, finds files marked pending_retry on any mirror,
        and re-pushes them to ONLY the failing mirror(s) — the primary is
        not touched. Per-file failures are recorded just as in a normal
        push (still pending → keep state, permanent → flip to failed_perm).

        backend_filter: when set, only retry on that one mirror name.
        dry_run: list what would be retried without uploading.

        Returns a summary dict:
          { "retried": N, "succeeded": N, "still_pending": N, "permanent": N }
        """
        from .backends import BackendError, ErrorClass

        result = {
            "retried": 0,
            "succeeded": 0,
            "still_pending": 0,
            "permanent": 0,
        }

        if not self._mirrors:
            console.print("[dim]No mirrors configured; nothing to retry.[/]")
            return result

        # Fresh coordinator per retry_mirrors() so a previous run's
        # throttle state never leaks in.
        self._coordinator = self._make_coordinator()

        # Collect (rel_path, [mirrors_to_retry_for_that_path]).
        # Single-pass over manifest avoids N×M scans.
        plan: dict[str, list[StorageBackend]] = {}
        for backend in self._mirrors:
            name = getattr(backend, "backend_name", "") or ""
            if not name:
                continue
            if backend_filter and name != backend_filter:
                continue
            for path in self.manifest.pending_for_backend(name).keys():
                plan.setdefault(path, []).append(backend)

        if not plan:
            scope = (
                f"backend {backend_filter!r}" if backend_filter else "any mirror"
            )
            console.print(f"[green]✓ No pending retries on {scope}.[/]")
            return result

        if dry_run:
            console.print(
                f"[bold]Would retry {len(plan)} file(s) across "
                f"{sum(len(v) for v in plan.values())} (file × mirror) pair(s):[/]"
            )
            for rel_path, mirrors in sorted(plan.items()):
                names = ", ".join(
                    getattr(b, "backend_name", "?") for b in mirrors
                )
                console.print(f"  [yellow]→[/] {rel_path}  [dim](mirrors: {names})[/]")
            return result

        # Real retry — for each (file × mirror) pair, attempt the upload
        # via the backend's classified retry adapter and update manifest
        # per outcome.
        for rel_path, mirrors in sorted(plan.items()):
            try:
                local_path = str(_safe_join(self._project, rel_path))
            except ValueError as e:
                console.print(
                    f"  [red]✗[/] {rel_path}: refusing unsafe path ({e})"
                )
                continue
            try:
                local_hash = Manifest.hash_file(local_path)
            except OSError as e:
                console.print(
                    f"  [red]✗[/] {rel_path}: cannot read local file ({e})"
                )
                continue

            for backend in mirrors:
                name = getattr(backend, "backend_name", "") or "unknown"
                result["retried"] += 1
                # Look up the previously-tried mirror file_id (if any).
                existing = self.manifest.get(rel_path)
                mirror_file_id: Optional[str] = None
                if existing and name in existing.remotes:
                    mirror_file_id = existing.remotes[name].remote_file_id or None
                try:
                    upload_fn = getattr(backend, "_upload_with_retry", None)
                    if upload_fn:
                        def _do_retry_upload() -> str:
                            return cast(str, upload_fn(
                                local_path=local_path,
                                rel_path=rel_path,
                                root_folder_id=backend.config.root_folder,  # type: ignore[attr-defined]  # backends carry a `config` attribute by convention; not on the abstract base
                                file_id=mirror_file_id,
                            ))
                    else:
                        def _do_retry_upload() -> str:
                            return backend.upload_file(
                                local_path=local_path,
                                rel_path=rel_path,
                                root_folder_id=backend.config.root_folder,  # type: ignore[attr-defined]  # backends carry a `config` attribute by convention; not on the abstract base
                                file_id=mirror_file_id,
                            )
                    new_id = self._upload_with_coordinator(_do_retry_upload, backend)
                    try:
                        remote_hash = backend.get_file_hash(new_id) or local_hash
                    except Exception:
                        remote_hash = local_hash
                    self.manifest.update_remote(
                        rel_path, name,
                        remote_file_id=new_id,
                        synced_remote_hash=remote_hash,
                        state="ok",
                    )
                    result["succeeded"] += 1
                    console.print(
                        f"  [green]✓[/] {rel_path} [dim](mirror: {name})[/]"
                    )
                except BackendError as be:
                    cls = be.error_class
                    state_str = (
                        "pending_retry" if cls.is_retryable else "failed_perm"
                    )
                    self.manifest.update_remote(
                        rel_path, name,
                        state=state_str,
                        last_error=redact_error(f"{cls.value}: {be}"),
                        intended_hash=local_hash,
                    )
                    if cls.is_retryable:
                        result["still_pending"] += 1
                        console.print(
                            f"  [yellow]⚠[/] {rel_path} [dim](mirror {name}: "
                            f"{cls.value}, will retry next time)[/]"
                        )
                    else:
                        result["permanent"] += 1
                        console.print(
                            f"  [red]✗[/] {rel_path} [dim](mirror {name}: "
                            f"{cls.value} — needs user action)[/]"
                        )
                except Exception as exc:
                    cls = backend.classify_error(exc)
                    state_str = (
                        "pending_retry" if cls.is_retryable else "failed_perm"
                    )
                    self.manifest.update_remote(
                        rel_path, name,
                        state=state_str,
                        last_error=redact_error(f"{cls.value}: {exc}"),
                        intended_hash=local_hash,
                    )
                    if cls.is_retryable:
                        result["still_pending"] += 1
                    else:
                        result["permanent"] += 1
                    console.print(
                        f"  [red]✗[/] {rel_path} [dim](mirror {name}: {exc})[/]"
                    )

        self.manifest.save()
        return result

    def seed_mirror(
        self,
        backend_name: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Upload every manifest-tracked file to a newly-added mirror that
        has no recorded state for them yet.

        This closes the "fresh mirror seeding gap": when a user adds a
        mirror to `mirror_config_paths` for a project where files already
        exist on the primary, push has nothing to do (local hashes match
        the manifest's primary-side hashes) and the mirror folder stays
        empty forever. seed_mirror walks `manifest.unseeded_for_backend
        (backend_name)` and uploads each file to the named mirror only —
        the primary is never touched.

        For each file in the unseeded set:
          * Local file is hashed.
          * If the local hash differs from the manifest's `synced_hash`,
            the file is SKIPPED with a warning. Drift means the local
            content has diverged from what was last pushed; the user
            should resolve via a normal push (which will fan-out to the
            mirror at the same time) before seeding. Blindly uploading
            mismatched content here would silently desync local from
            primary on the seeded mirror.
          * Otherwise: upload to the named mirror, record state="ok"
            via `manifest.update_remote(...)`.

        backend_name: must match one of the configured mirror backends'
            `backend_name` attribute (typically "sftp", "dropbox",
            "onedrive", "webdav", "googledrive"). Raises ValueError if
            no mirror with that name is configured.
        dry_run: list what would be seeded without uploading.

        Returns a summary dict:
          { "total_unseeded": N, "seeded": N, "skipped_drift": N,
            "failed": N }
        """
        from .backends import BackendError

        if not self._mirrors:
            console.print(
                "[yellow]No mirrors configured for this project; "
                "seed-mirror has nothing to do.[/]"
            )
            return {"total_unseeded": 0, "seeded": 0, "skipped_drift": 0, "failed": 0}

        target = next(
            (b for b in self._mirrors
             if (getattr(b, "backend_name", "") or "") == backend_name),
            None,
        )
        if target is None:
            available = ", ".join(
                (getattr(b, "backend_name", "?") or "?") for b in self._mirrors
            )
            raise ValueError(
                f"No mirror named {backend_name!r} configured for this "
                f"project. Available mirrors: {available}"
            )

        unseeded = self.manifest.unseeded_for_backend(backend_name)
        result = {
            "total_unseeded": len(unseeded),
            "seeded": 0,
            "skipped_drift": 0,
            "failed": 0,
        }

        if not unseeded:
            console.print(
                f"[green]✓ Mirror {backend_name!r} is already seeded — "
                "every manifest-tracked file has recorded state on it.[/]"
            )
            return result

        if dry_run:
            console.print(
                f"[bold]Would seed {len(unseeded)} file(s) to mirror "
                f"{backend_name!r}:[/]"
            )
            for rel_path in sorted(unseeded.keys()):
                console.print(f"  [yellow]→[/] {rel_path}")
            return result

        console.print(
            f"[bold]Seeding {len(unseeded)} file(s) to mirror {backend_name!r}…[/]"
        )

        # Fresh coordinator per seed_mirror() so a previous run's
        # global-throttle state never leaks in. Wired into the per-file
        # upload via _upload_with_coordinator below.
        self._coordinator = self._make_coordinator()

        # Pre-compute per-file sizes + drift state so the transfer-
        # progress bar's `total` reflects the bytes we will actually
        # ship. Skipped (drift / missing) files do not contribute.
        seed_plan: list[tuple[str, str, int]] = []
        for rel_path in sorted(unseeded.keys()):
            try:
                local_path = str(_safe_join(self._project, rel_path))
            except ValueError as e:
                result["failed"] += 1
                console.print(
                    f"  [red]✗[/] {rel_path}: refusing unsafe path ({e})"
                )
                continue
            try:
                local_hash = Manifest.hash_file(local_path)
            except OSError as e:
                result["failed"] += 1
                console.print(
                    f"  [red]✗[/] {rel_path}: cannot read local file ({e})"
                )
                continue
            manifest_hash = unseeded[rel_path].synced_hash
            if manifest_hash and local_hash != manifest_hash:
                result["skipped_drift"] += 1
                console.print(
                    f"  [yellow]⚠[/] {rel_path}: local hash differs "
                    "from manifest — run [bold]claude-mirror push[/] "
                    "to reconcile, then re-run seed-mirror."
                )
                continue
            try:
                size = int(Path(local_path).stat().st_size)
            except OSError:
                size = 0
            seed_plan.append((rel_path, local_hash, size))

        total_bytes = sum(sz for _, _, sz in seed_plan)

        if not seed_plan:
            self.manifest.save()
            return self._finish_seed_mirror(result, backend_name)

        with make_transfer_progress(console) as tprog:
            task = tprog.add_task(
                "Seeding", total=max(total_bytes, 1), show_time=True,
            )

            def _advance(n: int) -> None:
                if n > 0:
                    tprog.advance(task, n)

            for rel_path, local_hash, size in seed_plan:
                local_path = str(_safe_join(self._project, rel_path))
                try:
                    upload_fn = getattr(target, "_upload_with_retry", None)
                    if upload_fn:
                        # `_upload_with_retry` does not yet thread the
                        # progress callback (it predates PROG-ETA);
                        # the retry adapter is a thin wrapper anyway.
                        # Wrap in the coordinator so a global throttle
                        # pauses every parallel uploader on one
                        # shared deadline.
                        def _do_seed_upload() -> str:
                            return cast(str, upload_fn(
                                local_path=local_path,
                                rel_path=rel_path,
                                root_folder_id=target.config.root_folder,  # type: ignore[attr-defined]  # backends carry a `config` attribute by convention; not on the abstract base
                                file_id=None,
                            ))
                        new_id = self._upload_with_coordinator(_do_seed_upload, target)
                        # Retry adapter has no progress hook — emit a
                        # single "transferred N bytes" delta after the
                        # call returns so the bar still moves.
                        if size:
                            _advance(size)
                    else:
                        def _do_seed_upload_direct() -> str:
                            return target.upload_file(
                                local_path=local_path,
                                rel_path=rel_path,
                                root_folder_id=target.config.root_folder,  # type: ignore[attr-defined]  # backends carry a `config` attribute by convention; not on the abstract base
                                file_id=None,
                                progress_callback=_advance,
                            )
                        new_id = self._upload_with_coordinator(_do_seed_upload_direct, target)
                    try:
                        remote_hash = target.get_file_hash(new_id) or local_hash
                    except Exception:
                        remote_hash = local_hash
                    self.manifest.update_remote(
                        rel_path, backend_name,
                        remote_file_id=new_id,
                        synced_remote_hash=remote_hash,
                        state="ok",
                    )
                    result["seeded"] += 1
                except (BackendError, Exception) as exc:
                    result["failed"] += 1
                    tprog.console.print(
                        f"  [red]✗[/] {rel_path}: {redact_error(str(exc))}"
                    )

            # Defensive cap — if a backend's progress_callback under-
            # reported (Dropbox single-shot path emits one delta of
            # body length; should match exactly, but a stat/read race
            # could leave a tiny gap), force the bar to 100% on success.
            if not result["failed"] and total_bytes > 0:
                tprog.update(task, completed=total_bytes)

        self.manifest.save()
        return self._finish_seed_mirror(result, backend_name)

    def _finish_seed_mirror(self, result: dict[str, Any], backend_name: str) -> dict[str, Any]:
        """Print the final seed-mirror summary line + the failure hint
        when applicable. Extracted from ``seed_mirror`` so the success
        path and the empty-plan early-return share the same epilogue."""
        console.print(
            f"[bold]seed-mirror complete:[/] "
            f"seeded {result['seeded']}, "
            f"skipped (drift) {result['skipped_drift']}, "
            f"failed {result['failed']}."
        )
        if result["failed"]:
            console.print(
                "[dim]Failed entries: their next normal push will retry "
                "transient failures automatically; permanent failures need "
                "user action via [bold]claude-mirror doctor --backend "
                f"{backend_name}[/].[/]"
            )
        return result

    def _retry_pending_for_mirrors(self, cap: Optional[int] = None) -> list[str]:
        """Scan the manifest for files marked `pending_retry` on any mirror
        and return their relative paths. Called at the start of every push;
        the returned paths are added to the push queue so transient failures
        from previous runs are retried automatically.

        Permanent failures (`failed_perm`) are skipped here — they require
        user action and are surfaced via notification, not silent retry.

        cap: maximum number of paths to return. Defaults to
        PENDING_RETRY_QUEUE_CAP from manifest.py — prevents an unbounded
        backlog from making every push read+process tens of thousands
        of files. The user can drain a larger backlog explicitly via
        `claude-mirror retry`.
        """
        if not self._mirrors or not self.config.retry_on_push:
            return []
        pending: set[str] = set()
        for backend in self._mirrors:
            name = getattr(backend, "backend_name", "") or ""
            if not name:
                continue
            for path in self.manifest.pending_for_backend(name).keys():
                pending.add(path)
        ordered = sorted(pending)
        from .manifest import PENDING_RETRY_QUEUE_CAP
        effective_cap = cap if cap is not None else PENDING_RETRY_QUEUE_CAP
        if len(ordered) > effective_cap:
            console.print(
                f"[dim]Pending retry queue has {len(ordered)} entries; "
                f"taking the oldest {effective_cap}. Run "
                f"[bold]claude-mirror retry[/] to drain the backlog explicitly.[/]"
            )
            ordered = ordered[:effective_cap]
        return ordered

    def _print_summary(
        self,
        pushed: list[str],
        pulled: list[str],
        skipped: list[str],
        deleted: Optional[list[str]] = None,
    ) -> None:
        if deleted is None:
            deleted = []
        if not pushed and not pulled and not skipped and not deleted:
            console.print("[green]Everything is in sync.[/]")
            return
        if pushed:
            console.print(f"[cyan]Pushed:[/] {', '.join(pushed)}")
        if pulled:
            console.print(f"[blue]Pulled:[/] {', '.join(pulled)}")
        if deleted:
            console.print(f"[yellow]Deleted from Drive:[/] {', '.join(deleted)}")
        if skipped:
            console.print(f"[dim]Skipped:[/] {', '.join(skipped)}")

    def _build_backend_status(
        self,
        primary_pushed: list[str],
        snapshot_ts: Optional[str],
    ) -> dict[str, dict[str, Any]]:
        """Build the backend_status dict for the Slack rich block from
        the manifest's per-backend RemoteState plus the primary's push
        outcome we already know in-process.

        Shape (consumed by slack.post_sync_event):
          {backend_name: {
              "state": "ok"|"pending"|"failed",
              "files_pushed": int,
              "files_pending": int,
              "snapshot_ts": Optional[str],
              "error": Optional[str],
          }}
        """
        result: dict[str, dict[str, Any]] = {}
        primary_name = (
            getattr(self.storage, "backend_name", "") or self.config.backend
        )
        # Primary is always "ok" by the time _publish_event fires for a
        # successful push — _push_file would have raised otherwise.
        result[primary_name] = {
            "state": "ok",
            "files_pushed": len(primary_pushed),
            "files_pending": 0,
            "snapshot_ts": snapshot_ts,
            "error": None,
        }
        # Mirrors: walk the just-completed manifest entries for the
        # pushed paths, summing per-backend outcomes.
        for backend in self._mirrors:
            name = getattr(backend, "backend_name", "") or "unknown"
            ok_count = 0
            pending_count = 0
            failed_count = 0
            last_error: Optional[str] = None
            for path in primary_pushed:
                fs = self.manifest.get(path)
                if not fs or name not in fs.remotes:
                    continue
                rs = fs.remotes[name]
                if rs.state == "ok":
                    ok_count += 1
                elif rs.state == "pending_retry":
                    pending_count += 1
                    last_error = rs.last_error or last_error
                elif rs.state == "failed_perm":
                    failed_count += 1
                    last_error = rs.last_error or last_error
            if failed_count > 0:
                state = "failed"
            elif pending_count > 0:
                state = "pending"
            else:
                state = "ok"
            result[name] = {
                "state": state,
                "files_pushed": ok_count,
                "files_pending": pending_count + failed_count,
                "snapshot_ts": snapshot_ts if state == "ok" else None,
                "error": last_error,
            }
        return result

    def _dispatch_extra_webhooks(self, event: SyncEvent) -> None:
        """Fire Discord, Teams, and Generic webhook notifiers for ``event``.

        Each backend is checked independently — one being misconfigured
        cannot suppress the others. Lazy import keeps the cold-start
        cost off code paths that never publish events. Every call is
        wrapped so a notifier raising (which the abstraction tries hard
        not to do) cannot break sync.

        Per-project multi-channel routing (v0.5.50+): each backend's
        configured routes (``discord_routes`` / ``teams_routes`` /
        ``webhook_routes``) are walked sequentially. Each route gets
        its own notifier instance with its own webhook URL, fires only
        when ``event.action`` is in the route's ``on`` set, and only
        when at least one of ``event.files`` matches at least one of
        the route's ``paths`` globs (in which case event.files is
        trimmed to the matching subset before the notifier sees it).
        Backwards-compat: the legacy single-channel form is
        transparently surfaced by ``Config.iter_routes`` as one
        pseudo-route with default `on` (all four actions) and `paths`
        (`["**/*"]`).
        """
        cfg = self.config
        # Cheap pre-check: if no backend has either routes or the
        # legacy single-channel form, skip the import entirely.
        has_any_dispatch = any(
            self._backend_has_routes(b)
            for b in ("discord", "teams", "webhook")
        )
        if not has_any_dispatch:
            return
        try:
            from .notifications.webhooks import (
                DiscordWebhookNotifier,
                GenericWebhookNotifier,
                TeamsWebhookNotifier,
            )
        except Exception:
            return  # best-effort — never let an import error break sync

        # Discord — one notifier per route. Templates (when configured)
        # apply across all routes for the backend.
        discord_templates = getattr(cfg, "discord_template_format", None)
        for route in cfg.iter_routes("discord"):
            scoped = self._scope_event_for_route(event, route)
            if scoped is None:
                continue
            try:
                DiscordWebhookNotifier(
                    route["webhook_url"],
                    templates=discord_templates,
                ).notify(scoped)
            except Exception:
                pass  # best-effort

        # Microsoft Teams — same shape, different notifier.
        teams_templates = getattr(cfg, "teams_template_format", None)
        for route in cfg.iter_routes("teams"):
            scoped = self._scope_event_for_route(event, route)
            if scoped is None:
                continue
            try:
                TeamsWebhookNotifier(
                    route["webhook_url"],
                    templates=teams_templates,
                ).notify(scoped)
            except Exception:
                pass  # best-effort

        # Generic webhook — also carries optional per-route extra_headers
        # (or the legacy `webhook_extra_headers` surfaced by iter_routes).
        webhook_templates = getattr(cfg, "webhook_template_format", None)
        for route in cfg.iter_routes("webhook"):
            scoped = self._scope_event_for_route(event, route)
            if scoped is None:
                continue
            try:
                GenericWebhookNotifier(
                    route["webhook_url"],
                    extra_headers=route.get("extra_headers", cfg.webhook_extra_headers),
                    templates=webhook_templates,
                ).notify(scoped)
            except Exception:
                pass  # best-effort

    def _backend_has_routes(self, backend: str) -> bool:
        """Return True if ``backend`` would yield at least one route from
        ``iter_routes``. Used as the cheap-prefilter for the import gate
        in ``_dispatch_extra_webhooks`` — we don't want to import the
        webhooks module just to discover there's nothing to do."""
        cfg = self.config
        b = backend.lower()
        if b == "slack":
            return bool(cfg.slack_routes) or bool(
                cfg.slack_enabled and cfg.slack_webhook_url
            )
        if b == "discord":
            return bool(cfg.discord_routes) or bool(
                cfg.discord_enabled and cfg.discord_webhook_url
            )
        if b == "teams":
            return bool(cfg.teams_routes) or bool(
                cfg.teams_enabled and cfg.teams_webhook_url
            )
        if b == "webhook":
            return bool(cfg.webhook_routes) or bool(
                cfg.webhook_enabled and cfg.webhook_url
            )
        return False

    @staticmethod
    def _scope_event_for_route(
        event: SyncEvent, route: dict[str, Any]
    ) -> Optional[SyncEvent]:
        """Match ``event`` against a single route's filters.

        Returns ``None`` (skip the route) when:
          * ``event.action`` is not in the route's ``on`` list, OR
          * none of ``event.files`` matches any of the route's ``paths``
            globs (using ``fnmatch.fnmatchcase`` — same engine as
            ``file_patterns`` and ``exclude_patterns``).

        Returns a scoped copy of ``event`` (via dataclasses.replace) with
        ``files`` trimmed to the matching subset otherwise. The original
        event is never mutated — concurrent backends can each derive
        their own scoped view.

        Special-case: when the original ``event.files`` is empty (no-op
        sync), the route fires with the empty list so the user still
        sees the heartbeat. Without this, a `sync` event with nothing
        to do would never reach Slack, defeating the routing layer for
        a useful surface.
        """
        action = getattr(event, "action", "")
        on_list = route.get("on") or _DEFAULT_ROUTE_ACTIONS
        if action not in on_list:
            return None
        paths_globs = route.get("paths") or _DEFAULT_ROUTE_PATHS
        files = list(getattr(event, "files", None) or [])
        if not files:
            # Heartbeat / no-files event — let it through. The default
            # `["**/*"]` glob would skip these on a strict-match reading,
            # but routing should not silently drop "I synced nothing"
            # status surfaces.
            return event
        matching = [
            f for f in files
            if any(fnmatch.fnmatchcase(f, glob) for glob in paths_globs)
        ]
        if not matching:
            return None
        if len(matching) == len(files):
            return event  # no narrowing — reuse original
        return dataclasses.replace(event, files=matching)

    def _surface_quarantine(self, files_in_event: list[str]) -> None:
        """Surface permanent-failure backends to desktop + Slack.

        After a push completes, walk Manifest.quarantined_backends() and
        produce one notification per quarantined backend (not one per
        file — the user just needs to know the backend needs attention).
        """
        if not self.config.notify_failures:
            return
        quar = self.manifest.quarantined_backends()
        if not quar:
            return
        # Lazy import to avoid pulling rich/notifier into commands that
        # never publish events (e.g. inbox).
        try:
            from .notifier import Notifier
        except Exception:
            return
        notifier = Notifier(self.config.project_path)
        for backend_name, paths in quar.items():
            # Try to figure out the most recent error for a useful body.
            last_err = ""
            for p in paths:
                fs = self.manifest.get(p)
                if fs and backend_name in fs.remotes:
                    last_err = fs.remotes[backend_name].last_error or last_err
            title = "claude-mirror — action required"
            body = (
                f"{backend_name}: {len(paths)} file(s) cannot be synced. "
                f"Run: claude-mirror auth --backend {backend_name}\n"
                f"({last_err[:120]})"
            )
            try:
                notifier.notify_failure(title, body, action_required=True)
            except Exception:
                pass  # best-effort

    def _publish_event(
        self,
        files: list[str],
        action: str,
        *,
        snapshot_ts: Optional[str] = None,
        auto_resolved_files: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Queue a sync event: append it to the in-memory log and fire the publish
        without blocking on broker ack. Log upload + ack waits are flushed once at
        the end of the user-facing command via _flush_publishes().

        snapshot_ts: if a snapshot was created for this event, the timestamp
        flows through to Slack so the recipient sees the recovery point
        explicitly. Pub/Sub events are unchanged (the SyncEvent wire format
        stays stable so older subscribers keep working).

        auto_resolved_files: per-file audit trail of conflicts auto-resolved
        by `sync --no-prompt --strategy ...`. Each entry is
        `{"path": str, "strategy": str}`. Empty for every interactive run
        and for every push/pull/delete event. Travels on the same SyncEvent
        and lands in the same `_sync_log.json` so audits can spot every
        auto-resolution in one place.
        """
        event = SyncEvent.now(
            machine=self.config.machine_name,
            user=self.config.user,
            files=files,
            action=action,
            project=self._project.name,
            auto_resolved_files=auto_resolved_files,
        )
        try:
            if self.notifier is not None:
                future = self.notifier.publish_event_async(event)
                if future is not None:
                    self._pending_publish_futures.append(future)
            self._append_to_drive_log(event)
        except Exception as e:
            console.print(
                f"[yellow]Warning: could not publish event: "
                f"{redact_error(str(e))}[/]"
            )

        # Tier 2 multi-backend Slack enrichment — build the per-backend
        # status block + (when applicable) the ACTION REQUIRED alert
        # for permanent failures, and pass them into post_sync_event so
        # the surfaces shipped in Phase 4 actually fire.
        backend_status: Optional[dict[str, dict[str, Any]]] = None
        failure_alert: Optional[dict[str, str]] = None
        if action in ("push", "sync") and self._mirrors:
            backend_status = self._build_backend_status(files, snapshot_ts)
            # Promote the first failed backend (if any) to a failure_alert.
            for name, info in backend_status.items():
                if info["state"] == "failed":
                    failure_alert = {
                        "backend": name,
                        "reason": "auth/quota/permission",
                        "action": f"Run claude-mirror auth --backend {name}",
                    }
                    break

        # Optional Slack notification — enriched with snapshot info and
        # project size when available. v0.5.50+: routes-aware. Each route
        # gets its own per-Config view (with `slack_webhook_url` swapped
        # to the route URL) so `slack.post_sync_event` keeps its current
        # surface unchanged. Legacy single-channel mode falls through
        # iter_routes as one pseudo-route.
        if self._backend_has_routes("slack"):
            try:
                from .slack import post_sync_event
                snap_fmt = (
                    (self.config.snapshot_format or "full").lower()
                    if snapshot_ts else None
                )
                total_files = len(self.manifest.all())
                for route in self.config.iter_routes("slack"):
                    scoped = self._scope_event_for_route(event, route)
                    if scoped is None:
                        continue
                    try:
                        # Build a per-route Config view: same object, but
                        # with the slack URL swapped. dataclasses.replace
                        # gives us an isolated copy so concurrent routes
                        # never observe each other's URL mid-flight.
                        route_cfg = dataclasses.replace(
                            self.config,
                            slack_enabled=True,
                            slack_webhook_url=route["webhook_url"],
                            # Drop list-form on the per-route view so
                            # post_sync_event reads the route URL
                            # directly without re-routing recursively.
                            slack_routes=None,
                        )
                        post_sync_event(
                            route_cfg, scoped,
                            snapshot_ts=snapshot_ts,
                            snapshot_format=snap_fmt,
                            total_project_files=total_files,
                            backend_status=backend_status,
                            failure_alert=failure_alert,
                        )
                    except Exception:
                        pass  # best-effort, per-route
            except Exception:
                pass  # best-effort

        # Additional webhook notifiers — Discord, Microsoft Teams, and a
        # generic JSON-envelope endpoint. Each runs sequentially and
        # independently: a failure on one does not block any of the
        # others, and none of them can ever raise out into the sync
        # path. Sequential is fine because each call is best-effort
        # with a short timeout (5s) and we already accept Slack's
        # latency cost on this path. Webhooks fire only when their
        # respective `*_enabled` flag is set AND the URL is non-empty.
        self._dispatch_extra_webhooks(event)

        # Desktop notification on permanent failure — independent of
        # Slack so the user sees it regardless of channel config.
        if action in ("push", "sync") and self._mirrors:
            self._surface_quarantine(files)

    def _append_to_drive_log(self, event: SyncEvent) -> None:
        """Append to the in-memory log and mark it dirty. Upload is deferred to
        _flush_remote_log() so a single command publishing N events results in
        exactly one log upload, not N download+upload cycles."""
        if not self._remote_log_loaded:
            try:
                self._load_remote_log()
            except Exception:
                self._remote_log_cache = None
        if self._remote_log_cache is None:
            self._remote_log_cache = SyncLog()
        self._remote_log_cache.append(event)
        self._remote_log_dirty = True

    def _flush_remote_log(self) -> None:
        """Upload the cached sync log if it has been mutated."""
        if not self._remote_log_dirty or self._remote_log_cache is None:
            return
        try:
            if self._remote_log_folder_id is None:
                self._remote_log_folder_id = self.storage.get_or_create_folder(
                    LOGS_FOLDER, self._folder_id
                )
            self._remote_log_file_id = self.storage.upload_bytes(
                self._remote_log_cache.to_bytes(),
                SYNC_LOG_NAME,
                self._remote_log_folder_id,
                file_id=self._remote_log_file_id,
            )
            self._remote_log_dirty = False
        except Exception as e:
            console.print(
                f"[yellow]Warning: could not write sync log: "
                f"{redact_error(str(e))}[/]"
            )

    def _flush_publishes(self) -> None:
        """Wait for any deferred Pub/Sub publishes to be acknowledged, and
        write the deferred sync log. Called once per user-facing command."""
        for future in self._pending_publish_futures:
            try:
                future.result(timeout=10)
            except Exception as e:
                console.print(
                    f"[yellow]Warning: publish ack failed: "
                    f"{redact_error(str(e))}[/]"
                )
        self._pending_publish_futures.clear()
        self._flush_remote_log()

