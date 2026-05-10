"""Cross-process safety for `Manifest.save()` (H4).

The watcher daemon and a foreground `claude-mirror sync` can both write
the manifest concurrently. The in-process `threading.Lock` only
serialises threads in ONE process — across processes it's a no-op, so
without an OS-level lock the two writers race on the same `<file>.tmp`
path and one os.replace silently clobbers the other's bytes.

These tests validate the cross-process behaviour by spawning ACTUAL
subprocesses (multiprocessing with the `spawn` start method for
cross-platform compatibility — `fork` would inherit module state in a
way that masks the bug we're fixing). Each subprocess constructs a
fresh `Manifest`, mutates ONE rel-path, calls `save()`, and exits.
After both subprocesses exit, the on-disk manifest must contain BOTH
mutations — the OS-level lock + read-merge-write inside save() makes
disjoint writes from two processes both land.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from claude_mirror.manifest import Manifest


# Windows: msvcrt.locking blocks for ~10s on contention. Tests still
# run there but slower; skip when the multiprocessing forkserver isn't
# available (e.g. PyPy on Windows in some configurations).
_pytestmark_skip_windows = pytest.mark.skipif(
    sys.platform == "win32"
    and multiprocessing.get_start_method(allow_none=True) == "fork",
    reason="forking start method on Windows isn't supported",
)


def _child_writer(project_path: str, rel_path: str, hash_value: str) -> None:
    """Worker run inside the spawned child. Constructs a fresh Manifest,
    sets one disjoint entry, saves, exits."""
    m = Manifest(project_path)
    m.update(
        rel_path=rel_path,
        synced_hash=hash_value,
        remote_file_id=f"id-{rel_path}",
        synced_remote_hash=hash_value,
        backend_name="fake",
    )
    m.save()


def _child_writer_with_barrier(
    project_path: str, rel_path: str, hash_value: str, barrier_path: str,
) -> None:
    """Same as `_child_writer` but synchronises start-of-save with the
    sibling via a barrier file — both children try to save at the same
    time so the OS-level lock actually has contention."""
    m = Manifest(project_path)
    m.update(
        rel_path=rel_path,
        synced_hash=hash_value,
        remote_file_id=f"id-{rel_path}",
        synced_remote_hash=hash_value,
        backend_name="fake",
    )
    # Mark ourselves ready and wait for the sibling.
    Path(barrier_path).touch()
    deadline = time.time() + 5.0
    while time.time() < deadline:
        # Two .ready files == both children are at the barrier.
        ready = sum(
            1 for p in Path(barrier_path).parent.iterdir()
            if p.suffix == ".ready"
        )
        if ready >= 2:
            break
        time.sleep(0.01)
    m.save()


@_pytestmark_skip_windows
def test_two_processes_disjoint_entries_both_land(tmp_path: Path) -> None:
    """Two children each write a DISJOINT manifest entry. After both
    exit, the manifest on disk must contain BOTH entries — the
    cross-process file lock + read-merge-write inside save() makes
    disjoint writes both survive."""
    project = tmp_path / "project"
    project.mkdir()

    # `spawn` start method gives us a fresh interpreter per child
    # (matches how the watcher daemon vs CLI sync split processes in
    # production). `fork` would clone the parent's memory and could
    # mask the bug.
    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(target=_child_writer, args=(str(project), "a.md", "hash-a"))
    p2 = ctx.Process(target=_child_writer, args=(str(project), "b.md", "hash-b"))
    p1.start()
    p2.start()
    p1.join(timeout=10)
    p2.join(timeout=10)
    assert p1.exitcode == 0, f"child p1 exited {p1.exitcode}"
    assert p2.exitcode == 0, f"child p2 exited {p2.exitcode}"

    # Re-load and assert both entries survived.
    final = Manifest(str(project))
    all_entries = final.all()
    assert "a.md" in all_entries, (
        f"writer-1 entry lost; manifest = {list(all_entries)}"
    )
    assert "b.md" in all_entries, (
        f"writer-2 entry lost; manifest = {list(all_entries)}"
    )


@_pytestmark_skip_windows
def test_concurrent_writers_with_barrier_both_land(tmp_path: Path) -> None:
    """Tighter version: synchronise both writers at a barrier so they
    contend for the lock, then assert no entry is lost. Without the
    OS-level lock the os.replace race window opens and one entry
    drops; with the lock, both survive deterministically."""
    project = tmp_path / "project"
    project.mkdir()
    barrier_dir = tmp_path / "barriers"
    barrier_dir.mkdir()

    ctx = multiprocessing.get_context("spawn")
    p1 = ctx.Process(
        target=_child_writer_with_barrier,
        args=(str(project), "alpha.md", "h-alpha", str(barrier_dir / "p1.ready")),
    )
    p2 = ctx.Process(
        target=_child_writer_with_barrier,
        args=(str(project), "beta.md", "h-beta", str(barrier_dir / "p2.ready")),
    )
    p1.start()
    p2.start()
    p1.join(timeout=15)
    p2.join(timeout=15)
    assert p1.exitcode == 0, f"child p1 exited {p1.exitcode}"
    assert p2.exitcode == 0, f"child p2 exited {p2.exitcode}"

    final = Manifest(str(project))
    all_entries = final.all()
    assert "alpha.md" in all_entries
    assert "beta.md" in all_entries


def test_save_uses_per_process_tmp_suffix(tmp_path: Path) -> None:
    """Defence-in-depth: the tmp file used during save() carries this
    process's PID and thread ID in its suffix. Even if the OS-level
    lock somehow fails (hostile filesystem without working flock(2),
    bind-mount weirdness, …), two writers cannot share a single tmp
    path and stomp each other.

    Test by saving and inspecting the actual call signature — patch
    `Path.write_text` to capture the path used for the tmp write."""
    project = tmp_path / "project"
    project.mkdir()
    manifest = Manifest(str(project))
    manifest.update(
        rel_path="x.md",
        synced_hash="h",
        remote_file_id="id",
        synced_remote_hash="h",
    )

    captured_tmp_paths: list[str] = []
    original_write_text = Path.write_text

    def _spy_write_text(self: Path, *args: object, **kwargs: object) -> int:
        if str(self).endswith(".tmp"):
            captured_tmp_paths.append(str(self))
        return original_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    pid = os.getpid()
    tid = threading.get_ident()

    Path.write_text = _spy_write_text  # type: ignore[method-assign]
    try:
        manifest.save()
    finally:
        Path.write_text = original_write_text  # type: ignore[method-assign]

    assert captured_tmp_paths, "save() did not write any .tmp file"
    # Tmp suffix carries .{pid}.{tid}.tmp so two concurrent writers
    # can never collide on the same tmp path.
    last = captured_tmp_paths[-1]
    assert f".{pid}." in last, (
        f"tmp path {last} missing per-process PID suffix"
    )
    assert f".{tid}." in last, (
        f"tmp path {last} missing per-thread suffix"
    )


def test_remove_propagates_through_cross_process_save(tmp_path: Path) -> None:
    """When this process explicitly calls `remove(rel_path)` and saves,
    a stale on-disk entry from a prior writer MUST NOT resurrect the
    deleted key. Without this carve-out, the merge would silently undo
    every delete by re-reading the disk view as authoritative."""
    project = tmp_path / "project"
    project.mkdir()

    # Writer 1 puts file `victim.md` on disk.
    m1 = Manifest(str(project))
    m1.update(
        rel_path="victim.md",
        synced_hash="h1",
        remote_file_id="id1",
        synced_remote_hash="h1",
    )
    m1.save()
    assert "victim.md" in Manifest(str(project)).all()

    # Writer 2 (a fresh process simulation): load, remove, save.
    m2 = Manifest(str(project))
    m2.remove("victim.md")
    m2.save()

    final = Manifest(str(project)).all()
    assert "victim.md" not in final, (
        f"deletion was reverted by merge logic; final = {list(final)}"
    )
