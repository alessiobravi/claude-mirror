from __future__ import annotations

import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from rich.text import Text

from ._progress import _SharedElapsedColumn, make_phase_progress
from ._constants import PARALLEL_WORKERS

from .backends import StorageBackend, redact_error
from .config import Config
from .events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from .hash_cache import HashCache
from .manifest import Manifest
from .merge import MergeHandler
from .notifications import NotificationBackend
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
        self._pending_publish_futures: list = []

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def _is_excluded(self, rel_path: str) -> bool:
        """Return True if rel_path matches any exclude pattern."""
        for pattern in self.config.exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            # Also match against each path component so directory patterns work.
            # e.g. "archive/**" or "archive/*" should exclude "archive/foo.md"
            if fnmatch.fnmatch(rel_path, f"{pattern}/*") or rel_path.startswith(f"{pattern}/"):
                return True
        return False

    def _local_files(self) -> list[str]:
        """Return relative paths of all local files matching configured patterns."""
        found = set()
        for pattern in self.config.file_patterns:
            for path in self._project.glob(pattern):
                if path.is_file() and path.name != ".claude_mirror_manifest.json":
                    rel = str(path.relative_to(self._project))
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

        def _list_remote() -> dict:
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
            workers = min(PARALLEL_WORKERS, len(misses))
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
        manifest_entry,
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

    def sync(self) -> None:
        """Bidirectional sync: auto-resolve non-conflicts in parallel, prompt for conflicts."""
        pushed, pulled, skipped, deleted = [], [], [], []

        with self._make_phase_progress() as progress:
            states = self._run_status_phase(progress)

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
            pull_task = progress.add_task("Pulling", total=None, detail="0 file(s)", show_time=True)
            ok, _ = self._parallel(
                pulls, self._pull_file, description="Pulling",
                progress=progress, task_id=pull_task,
            )
            pulled.extend(s.rel_path for s in ok)

            # Guard checks
            guard_task = progress.add_task("Guard checks", total=None, detail="0 file(s)", show_time=True)
            safe_pushes, new_conflicts = self._parallel_guard_checks(
                local_pushes, progress=progress, task_id=guard_task,
            )

            # Pushing
            push_task = progress.add_task("Pushing", total=None, detail="0 file(s)", show_time=True)
            ok, _ = self._parallel(
                safe_pushes, self._push_file, description="Pushing",
                progress=progress, task_id=push_task,
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

            # Conflicts (interactive — pause the live region while prompting).
            all_conflicts = existing_conflicts + new_conflicts
            conflict_task = progress.add_task(
                "Conflicts", total=max(len(all_conflicts), 1),
                detail="no conflicts" if not all_conflicts else f"0/{len(all_conflicts)}", show_time=True)
            if all_conflicts:
                progress.stop()
                try:
                    for i, state in enumerate(all_conflicts, 1):
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
                        else:
                            skipped.append(state.rel_path)
                finally:
                    progress.start()
                    progress.update(
                        conflict_task,
                        completed=len(all_conflicts),
                        detail=f"{len(all_conflicts)}/{len(all_conflicts)} resolved",
                    )
            else:
                progress.update(conflict_task, completed=1)

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
                    self._publish_event(pushed, "sync", snapshot_ts=snapshot_ts)
                    progress.update(notify_task, advance=1, detail="sync event sent")
                if deleted and self.notifier:
                    self._publish_event(deleted, "delete")
                    progress.update(notify_task, advance=1, detail="delete event sent")
                self._flush_publishes()
                progress.update(notify_task, detail="completed")
            else:
                self._flush_publishes()

            self.manifest.save()

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

    def push(self, paths: Optional[list[str]] = None, force_local: bool = False) -> None:
        pushed: list[str] = []
        pulled: list[str] = []
        deleted: list[str] = []

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
                push_task = progress.add_task("Pushing", total=None, detail="0 file(s)", show_time=True)
                ok, _ = self._parallel(
                    to_push, self._push_file, description="Pushing",
                    progress=progress, task_id=push_task,
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
                push_task = progress.add_task("Pushing", total=None, detail="0 file(s)", show_time=True)
                ok, _ = self._parallel(
                    safe_pushes, self._push_file, description="Pushing",
                    progress=progress, task_id=push_task,
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

    def pull(self, paths: Optional[list[str]] = None, output_dir: Optional[str] = None) -> None:
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
                action = lambda s: self._pull_file_to(s, output_path)
            else:
                action = self._pull_file

            # Pulling
            pull_task = progress.add_task("Pulling", total=None, detail="0 file(s)", show_time=True)
            ok, _ = self._parallel(
                to_pull, action, description="Pulling",
                progress=progress, task_id=pull_task,
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
        items: list,
        fn: Callable,
        description: str = "Working",
        progress: Optional[Progress] = None,
        task_id: Optional[int] = None,
    ) -> tuple[list, list]:
        """Run fn(item) for each item in parallel with a live progress bar.
        Returns (succeeded, failed) item lists.

        If `progress` is provided, the work is rendered as a row in that
        existing Progress (used by push/pull/sync to compose multi-phase
        live displays). Otherwise a transient Progress is created locally.
        If `task_id` is also provided, that pre-existing task is reused
        and updated rather than creating a new one.
        """
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
                    progress.update(task_id, advance=1, detail="completed")
                return [item], []
            except Exception as e:
                label = getattr(item, "rel_path", str(item))
                out = progress.console if progress is not None else console
                out.print(f"  [red]✗ {label}: {e}[/]")
                if progress is not None and task_id is not None:
                    progress.update(task_id, advance=1, detail="failed")
                return [], [item]

        succeeded, failed = [], []
        workers = min(PARALLEL_WORKERS, len(items))

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
                        detail=f"{done}/{len(items)}",
                    )
            if owns_task:
                progress.remove_task(task_id)
            else:
                # Caller owns the row; mark it completed so the final state is
                # readable rather than a stale per-item count.
                progress.update(task_id, detail="completed")
        finally:
            if owns_progress:
                progress.stop()
        return succeeded, failed

    def _parallel_guard_checks(
        self,
        states: list[FileSyncState],
        progress: Optional[Progress] = None,
        task_id: Optional[int] = None,
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
        safe, conflicts = [], []
        workers = min(PARALLEL_WORKERS, len(states))
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

    def _push_file(self, state: FileSyncState) -> None:
        # _safe_join refuses manifest keys that try to escape the project
        # root — a defence against a corrupted or malicious manifest.
        local_path = str(_safe_join(self._project, state.rel_path))
        # Primary push first — its success is the gate for "push succeeded".
        # If it fails, propagate the exception (caller treats this as a
        # failed push, no manifest update). If it succeeds, fan out to
        # mirrors using their classified retry adapter.
        file_id = self.storage.upload_file(
            local_path=local_path,
            rel_path=state.rel_path,
            root_folder_id=self._folder_id,
            file_id=state.drive_file_id,
        )
        remote_hash = self.storage.get_file_hash(file_id)
        primary_name = (
            getattr(self.storage, "backend_name", "") or self.config.backend
        )
        # Update manifest's flat fields + record primary as ok in remotes map.
        self.manifest.update(
            state.rel_path, state.local_hash, file_id,
            synced_remote_hash=remote_hash or state.local_hash,
            backend_name=primary_name,
        )
        console.print(f"  [cyan]↑[/] {state.rel_path}")

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

        def _push_one(backend) -> tuple[str, bool, Optional[str], Optional[ErrorClass]]:
            name = getattr(backend, "backend_name", "") or "unknown"
            # Look up the file ID this mirror has used previously, if any.
            existing = self.manifest.get(rel_path)
            mirror_file_id: Optional[str] = None
            if existing and name in existing.remotes:
                mirror_file_id = existing.remotes[name].remote_file_id or None
            try:
                # Use the classified retry adapter when available; fall back
                # to plain upload_file for backends that haven't shipped
                # the retry helper yet.
                upload_fn = getattr(backend, "_upload_with_retry", None)
                if upload_fn:
                    new_id = upload_fn(
                        local_path=local_path,
                        rel_path=rel_path,
                        root_folder_id=backend.config.root_folder,
                        file_id=mirror_file_id,
                    )
                else:
                    new_id = backend.upload_file(
                        local_path=local_path,
                        rel_path=rel_path,
                        root_folder_id=backend.config.root_folder,
                        file_id=mirror_file_id,
                    )
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
        workers = min(PARALLEL_WORKERS, len(self._mirrors))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_push_one, b) for b in self._mirrors]
            for fut in as_completed(futs):
                name, ok, err, cls = fut.result()
                if ok:
                    console.print(f"  [cyan]↑[/] {rel_path} [dim](mirror: {name})[/]")
                else:
                    color = "yellow" if (cls and cls.is_retryable) else "red"
                    console.print(
                        f"  [{color}]⚠[/] {rel_path} [dim](mirror {name}: "
                        f"{cls.value if cls else 'error'} — {err[:80] if err else ''})[/]"
                    )

    def _pull_file(self, state: FileSyncState) -> None:
        # Refuse to write outside the project root, even if the remote metadata
        # claims a relative_path with `..` segments.
        local_path = _safe_join(self._project, state.rel_path)
        content = self.storage.download_file(state.drive_file_id)
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

    def _pull_file_to(self, state: FileSyncState, output_dir: Path) -> None:
        """Download a remote file to output_dir without touching the project or manifest."""
        dest = _safe_join(output_dir, state.rel_path)
        content = self.storage.download_file(state.drive_file_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        console.print(f"  [blue]↓[/] {state.rel_path} → {dest}")

    def _resolve_conflict(self, state: FileSyncState) -> Optional[str]:
        """
        Interactively resolve a conflict.
        Returns 'pushed', 'pulled', or None if skipped.
        """
        local_content = _safe_join(self._project, state.rel_path).read_text(errors="replace")
        drive_bytes = self.storage.download_file(state.drive_file_id)
        drive_content = drive_bytes.decode(errors="replace")

        result = self.merge.resolve_conflict(state.rel_path, local_content, drive_content)
        if result is None:
            console.print(f"  [dim]Skipped {state.rel_path}[/]")
            return None

        resolved_content, winner = result
        local_path = _safe_join(self._project, state.rel_path)
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
            return "pulled"
        else:
            # Local or merged — push the resolved content to Drive
            state.local_hash = Manifest.hash_file(str(local_path))
            self._push_file(state)
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
    ) -> dict:
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
                        new_id = upload_fn(
                            local_path=local_path,
                            rel_path=rel_path,
                            root_folder_id=backend.config.root_folder,
                            file_id=mirror_file_id,
                        )
                    else:
                        new_id = backend.upload_file(
                            local_path=local_path,
                            rel_path=rel_path,
                            root_folder_id=backend.config.root_folder,
                            file_id=mirror_file_id,
                        )
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

    def _print_summary(self, pushed: list, pulled: list, skipped: list, deleted: list = []) -> None:
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
    ) -> dict[str, dict]:
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
        result: dict[str, dict] = {}
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
    ) -> None:
        """Queue a sync event: append it to the in-memory log and fire the publish
        without blocking on broker ack. Log upload + ack waits are flushed once at
        the end of the user-facing command via _flush_publishes().

        snapshot_ts: if a snapshot was created for this event, the timestamp
        flows through to Slack so the recipient sees the recovery point
        explicitly. Pub/Sub events are unchanged (the SyncEvent wire format
        stays stable so older subscribers keep working).
        """
        event = SyncEvent.now(
            machine=self.config.machine_name,
            user=self.config.user,
            files=files,
            action=action,
            project=self._project.name,
        )
        try:
            future = self.notifier.publish_event_async(event)
            if future is not None:
                self._pending_publish_futures.append(future)
            self._append_to_drive_log(event)
        except Exception as e:
            console.print(f"[yellow]Warning: could not publish event: {e}[/]")

        # Tier 2 multi-backend Slack enrichment — build the per-backend
        # status block + (when applicable) the ACTION REQUIRED alert
        # for permanent failures, and pass them into post_sync_event so
        # the surfaces shipped in Phase 4 actually fire.
        backend_status: Optional[dict[str, dict]] = None
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
        # project size when available.
        if self.config.slack_enabled:
            try:
                from .slack import post_sync_event
                post_sync_event(
                    self.config, event,
                    snapshot_ts=snapshot_ts,
                    snapshot_format=(
                        (self.config.snapshot_format or "full").lower()
                        if snapshot_ts else None
                    ),
                    total_project_files=len(self.manifest.all()),
                    backend_status=backend_status,
                    failure_alert=failure_alert,
                )
            except Exception:
                pass  # best-effort

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
            console.print(f"[yellow]Warning: could not write sync log: {e}[/]")

    def _flush_publishes(self) -> None:
        """Wait for any deferred Pub/Sub publishes to be acknowledged, and
        write the deferred sync log. Called once per user-facing command."""
        for future in self._pending_publish_futures:
            try:
                future.result(timeout=10)
            except Exception as e:
                console.print(f"[yellow]Warning: publish ack failed: {e}[/]")
        self._pending_publish_futures.clear()
        self._flush_remote_log()

