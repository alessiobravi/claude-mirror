"""Standard coverage for `claude_mirror.snapshots.SnapshotManager`.

Covers create/list/restore/forget/gc/migrate/history/inspect across both
on-disk formats (`full` per-folder copies and `blobs` content-addressed
manifests). Drives a custom in-memory `InMemoryBackend` rather than the
thin conftest fake — SnapshotManager exercises the full StorageBackend
surface (resolve_path, list_files_recursive, list_folders, copy_file,
get_file_id, etc.), so tests need a backend that models a real folder
tree with stable IDs.

Hot-path semantics (verified against the four production backends):

* Folders and files share a single ID space; folder IDs are returned by
  `get_or_create_folder`/`resolve_path`, file IDs by uploads.
* `list_files_recursive(folder_id)` walks descendants by traversing
  parent-id pointers; the returned `relative_path` is relative to the
  passed-in folder_id.
* `delete_file(folder_id)` cascades to descendants (matches Drive,
  Dropbox, OneDrive, WebDAV semantics — see `_forget_one`).

Tests target SnapshotManager methods directly; CLI is covered by smoke."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from claude_mirror import snapshots as snap_mod
from claude_mirror.snapshots import (
    BLOBS_FOLDER,
    MANIFEST_SUFFIX,
    SNAPSHOT_META_FILE,
    SNAPSHOTS_FOLDER,
    SnapshotManager,
)


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class _Node:
    """A single tree node. is_folder tracks whether this is a directory."""

    __slots__ = ("node_id", "name", "parent_id", "is_folder", "content")

    def __init__(
        self,
        node_id: str,
        name: str,
        parent_id: Optional[str],
        is_folder: bool,
        content: bytes = b"",
    ) -> None:
        self.node_id = node_id
        self.name = name
        self.parent_id = parent_id
        self.is_folder = is_folder
        self.content = content


class InMemoryBackend:
    """Fully-functional in-memory backend that models a folder tree.

    Designed to be drop-in for `StorageBackend` so SnapshotManager runs
    without any cloud calls. Tracks every operation for assertion."""

    backend_name = "memory"
    MAX_DOWNLOAD_BYTES = 1 << 30

    def __init__(self, name: str = "memory", root_folder: str = "ROOT") -> None:
        self.backend_name = name
        # Pre-create the root folder; SnapshotManager calls
        # get_or_create_folder under the configured root_folder ID so the
        # caller is responsible for that being valid.
        self._root_id = root_folder
        self._nodes: dict[str, _Node] = {
            root_folder: _Node(root_folder, "", None, True),
        }
        self._next_id = 1
        # Per-call counters / fault injection.
        self.upload_calls: list[tuple] = []
        self.download_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.copy_calls: list[tuple] = []
        # Optional fault hook: a callable that, when set, is consulted
        # before download_file. Returning a string raises with that msg.
        self.download_fault: Optional[Callable[[str], Optional[str]]] = None

    # Helpers -----------------------------------------------------------

    def _new_id(self, prefix: str = "id") -> str:
        nid = f"{prefix}-{self._next_id}"
        self._next_id += 1
        return nid

    def _children(self, parent_id: str) -> list[_Node]:
        return [n for n in self._nodes.values() if n.parent_id == parent_id]

    def _find_child(self, parent_id: str, name: str) -> Optional[_Node]:
        for n in self._children(parent_id):
            if n.name == name:
                return n
        return None

    def _walk(self, parent_id: str) -> list[_Node]:
        """All descendant IDs, depth-first."""
        out: list[_Node] = []
        for child in self._children(parent_id):
            out.append(child)
            if child.is_folder:
                out.extend(self._walk(child.node_id))
        return out

    # StorageBackend interface -----------------------------------------

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        existing = self._find_child(parent_id, name)
        if existing and existing.is_folder:
            return existing.node_id
        nid = self._new_id("folder")
        self._nodes[nid] = _Node(nid, name, parent_id, is_folder=True)
        return nid

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
        if not parts:
            raise ValueError("empty rel_path")
        parent_id = root_folder_id
        for component in parts[:-1]:
            parent_id = self.get_or_create_folder(component, parent_id)
        return parent_id, parts[-1]

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> list[dict]:
        if folder_id not in self._nodes:
            return []
        excludes = exclude_folder_names or set()
        results: list[dict] = []

        def _recurse(parent_id: str, rel_prefix: str) -> None:
            for child in self._children(parent_id):
                if child.is_folder:
                    if child.name in excludes:
                        continue
                    sub_prefix = (
                        f"{rel_prefix}/{child.name}" if rel_prefix else child.name
                    )
                    _recurse(child.node_id, sub_prefix)
                else:
                    rel = (
                        f"{rel_prefix}/{child.name}" if rel_prefix else child.name
                    )
                    results.append(
                        {
                            "id": child.node_id,
                            "name": child.name,
                            "md5Checksum": "",
                            "relative_path": rel,
                            "size": len(child.content),
                        }
                    )

        _recurse(folder_id, "")
        if progress_cb is not None:
            progress_cb(1, len(results))
        return results

    def list_folders(
        self, parent_id: str, name: Optional[str] = None
    ) -> list[dict]:
        if parent_id not in self._nodes:
            return []
        out = []
        for child in self._children(parent_id):
            if not child.is_folder:
                continue
            if name is not None and child.name != name:
                continue
            out.append(
                {
                    "id": child.node_id,
                    "name": child.name,
                    "createdTime": "2026-01-01T00:00:00Z",
                }
            )
        return out

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        with open(local_path, "rb") as f:
            content = f.read()
        parent_id, filename = self.resolve_path(rel_path, root_folder_id)
        return self._write(parent_id, filename, content, file_id)

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        return self._write(folder_id, name, content, file_id)

    def _write(
        self,
        parent_id: str,
        name: str,
        content: bytes,
        file_id: Optional[str],
    ) -> str:
        if file_id and file_id in self._nodes:
            n = self._nodes[file_id]
            n.content = content
            self.upload_calls.append((parent_id, name, len(content)))
            return file_id
        # Replace existing same-name file under the same parent.
        existing = self._find_child(parent_id, name)
        if existing and not existing.is_folder:
            existing.content = content
            self.upload_calls.append((parent_id, name, len(content)))
            return existing.node_id
        nid = self._new_id("file")
        self._nodes[nid] = _Node(nid, name, parent_id, False, content)
        self.upload_calls.append((parent_id, name, len(content)))
        return nid

    def download_file(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        if self.download_fault is not None:
            err = self.download_fault(file_id)
            if err is not None:
                raise RuntimeError(err)
        if file_id not in self._nodes:
            raise RuntimeError(f"file not found: {file_id}")
        return self._nodes[file_id].content

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        n = self._find_child(folder_id, name)
        if n is None or n.is_folder:
            return None
        return n.node_id

    def copy_file(
        self, source_file_id: str, dest_folder_id: str, name: str
    ) -> str:
        src = self._nodes[source_file_id]
        self.copy_calls.append((source_file_id, dest_folder_id, name))
        nid = self._new_id("file")
        self._nodes[nid] = _Node(nid, name, dest_folder_id, False, src.content)
        return nid

    def get_file_hash(self, file_id: str) -> Optional[str]:
        if file_id in self._nodes:
            return hashlib.md5(self._nodes[file_id].content).hexdigest()
        return None

    def delete_file(self, file_id: str) -> None:
        self.delete_calls.append(file_id)
        if file_id not in self._nodes:
            return
        # Cascade delete: remove this node and all descendants.
        if self._nodes[file_id].is_folder:
            for d in self._walk(file_id):
                self._nodes.pop(d.node_id, None)
        self._nodes.pop(file_id, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_backend() -> InMemoryBackend:
    return InMemoryBackend(name="primary", root_folder="ROOT")


@pytest.fixture
def stepped_clock(monkeypatch):
    """Monkeypatch `snap_mod.datetime` so each call to `now()` returns a
    timestamp 60s after the previous one. Tests that snapshot multiple
    times in a row need this — without it, the wall clock can resolve
    to the same second and SnapshotManager would overwrite manifests.

    Returns the underlying counter so tests can read the current step."""
    import datetime as _dt

    state = {"step": 0}
    base = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)

    class _SteppedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            value = base + _dt.timedelta(minutes=state["step"])
            state["step"] += 1
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    monkeypatch.setattr(snap_mod, "datetime", _SteppedDateTime)
    return state


@pytest.fixture
def cfg(make_config):
    """Default config wired for the in-memory backend's root folder."""
    return make_config(drive_folder_id="ROOT", snapshot_format="full")


def _make_manager(cfg, backend, mirrors=None) -> SnapshotManager:
    return SnapshotManager(cfg, backend, mirrors=mirrors)


# Regression test for the SFTP-mirror-snapshot bug: pre-fix,
# _root_folder_for(mirror) checked only `getattr(mirror, "root_folder",
# None)` and fell through to `self.config.root_folder` (i.e. the primary's
# root). For backends that carry their root via `config.root_folder`
# (SFTP, all the real backends) this meant snapshot fan-out used the
# PRIMARY's folder ID as the parent path on the mirror — silently
# breaking blob/snapshot dir creation on every mirror.

class _MirrorConfigStub:
    """Minimal Config-like stub that exposes a `root_folder` property —
    matching the production backends' shape where the embedded Config
    knows the backend-specific field to return."""
    def __init__(self, root_folder: str) -> None:
        self.root_folder = root_folder


def test_root_folder_for_mirror_prefers_mirror_config_over_primary(make_config):
    """When the mirror exposes its own config with a `root_folder`
    property (SFTP / Drive / Dropbox / OneDrive / WebDAV all do this),
    SnapshotManager._root_folder_for(mirror) MUST return that mirror's
    root, not the primary's. Pre-fix it returned the primary's."""
    cfg = make_config(drive_folder_id="PRIMARY_ROOT")
    primary = InMemoryBackend(name="primary", root_folder="PRIMARY_ROOT")
    mirror = InMemoryBackend(name="sftp", root_folder="MIRROR_ROOT")
    # Attach a config to the mirror — same contract as real backends:
    # mirror.config.root_folder dispatches on backend type to return
    # the correct field (sftp_folder, dropbox_folder, etc.).
    mirror.config = _MirrorConfigStub("MIRROR_ROOT")

    mgr = SnapshotManager(cfg, primary, mirrors=[mirror])

    assert mgr._root_folder_for(primary) == "PRIMARY_ROOT"
    assert mgr._root_folder_for(mirror) == "MIRROR_ROOT", (
        "Mirror's root_folder must come from mirror.config.root_folder, "
        "not fall through to the primary's root — pre-fix bug caused "
        "every mirror's snapshot fan-out to write to a wrong path."
    )


def test_root_folder_for_mirror_falls_back_to_attribute_for_legacy_backends(make_config):
    """Backwards compat: a mirror that doesn't carry an embedded Config
    but DOES expose `.root_folder` directly (test fakes, custom
    subclasses) still resolves correctly."""
    cfg = make_config(drive_folder_id="PRIMARY_ROOT")
    primary = InMemoryBackend(name="primary", root_folder="PRIMARY_ROOT")
    mirror = InMemoryBackend(name="legacy", root_folder="LEGACY_MIRROR_ROOT")
    # Explicitly NO mirror.config attribute — exercise the legacy path.
    if hasattr(mirror, "config"):
        del mirror.config
    mirror.root_folder = "LEGACY_MIRROR_ROOT"

    mgr = SnapshotManager(cfg, primary, mirrors=[mirror])
    assert mgr._root_folder_for(mirror) == "LEGACY_MIRROR_ROOT"


# ---------------------------------------------------------------------------
# 1. Create — both formats
# ---------------------------------------------------------------------------


def test_create_full_format_copies_every_file(
    cfg, memory_backend, write_files, project_dir,
):
    """Full-format create: each project file becomes a server-side copy
    inside `_claude_mirror_snapshots/{ts}/`, plus a meta sidecar."""
    write_files({"a.md": "AAA", "b.md": "BBB", "sub/c.md": "CCC"})
    # Pre-populate remote with the same files so copy_file has sources to
    # copy from (real backends already have these from the prior push).
    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    for rel, content in [("a.md", b"AAA"), ("b.md", b"BBB"), ("sub/c.md", b"CCC")]:
        parent_id, filename = memory_backend.resolve_path(rel, "ROOT")
        memory_backend.upload_bytes(content, filename, parent_id)

    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=["a.md"])
    assert ts

    snap_folders = memory_backend.list_folders(snaps_id, name=ts)
    assert len(snap_folders) == 1
    snap_id = snap_folders[0]["id"]

    files = memory_backend.list_files_recursive(snap_id)
    names = {f["relative_path"] for f in files}
    assert SNAPSHOT_META_FILE in names
    assert "a.md" in names
    assert "b.md" in names
    assert "sub/c.md" in names

    # Meta sidecar reflects format + total.
    meta_id = memory_backend.get_file_id(SNAPSHOT_META_FILE, snap_id)
    meta = json.loads(memory_backend.download_file(meta_id))
    assert meta["format"] == "full"
    assert meta["total_files"] == 3
    assert meta["action"] == "push"


def test_create_blob_format_dedups_identical_content(
    make_config, memory_backend, write_files,
):
    """Two files with identical bytes share one blob upload + two manifest entries."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"x.md": "DUP", "y.md": "DUP"})

    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])
    assert ts

    blobs_id = memory_backend.get_or_create_folder(BLOBS_FOLDER, "ROOT")
    blobs = memory_backend.list_files_recursive(blobs_id)
    # Exactly one blob upload for two identical-content files.
    assert len(blobs) == 1
    blob_hash = blobs[0]["name"]

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    manifest = json.loads(memory_backend.download_file(manifest_id))
    files = manifest["files"]
    assert files == {"x.md": blob_hash, "y.md": blob_hash}


def test_create_blob_format_writes_manifest_json(
    make_config, memory_backend, write_files,
):
    """Manifest at `_claude_mirror_snapshots/{ts}.json` maps rel_path → sha256."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"foo.md": "FOO", "bar/baz.md": "BAZ"})

    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=["foo.md"])

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    assert manifest_id is not None
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert set(manifest["files"].keys()) == {"foo.md", "bar/baz.md"}
    assert manifest["files"]["foo.md"] == hashlib.sha256(b"FOO").hexdigest()
    assert manifest["files"]["bar/baz.md"] == hashlib.sha256(b"BAZ").hexdigest()
    assert manifest["timestamp"] == ts
    assert manifest["total_files"] == 2


def test_create_skips_excluded_files(
    make_config, memory_backend, write_files,
):
    """Files matching `exclude_patterns` never reach the snapshot manifest."""
    cfg = make_config(
        drive_folder_id="ROOT",
        snapshot_format="blobs",
        exclude_patterns=["secrets/*"],
    )
    write_files({"keep.md": "K", "secrets/leak.md": "BAD"})

    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert "keep.md" in manifest["files"]
    assert "secrets/leak.md" not in manifest["files"]


# ---------------------------------------------------------------------------
# 2. List
# ---------------------------------------------------------------------------


def test_list_returns_both_formats_sorted(
    make_config, memory_backend, write_files,
):
    """Mix of full + blob snapshots → returns descending by timestamp."""
    cfg_full = make_config(drive_folder_id="ROOT", snapshot_format="full")
    cfg_blobs = make_config(drive_folder_id="ROOT", snapshot_format="blobs")

    # Pre-populate remote project files (needed for full-format copy).
    write_files({"a.md": "A"})
    parent_id, filename = memory_backend.resolve_path("a.md", "ROOT")
    memory_backend.upload_bytes(b"A", filename, parent_id)

    # Inject snapshots with controlled timestamps.
    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    blobs_id = memory_backend.get_or_create_folder(BLOBS_FOLDER, "ROOT")

    # Full-format snapshot @ T1
    folder_id = memory_backend.get_or_create_folder(
        "2026-01-01T00-00-00Z", snaps_id
    )
    memory_backend.upload_bytes(
        json.dumps({"timestamp": "2026-01-01T00-00-00Z", "format": "full"}).encode(),
        SNAPSHOT_META_FILE, folder_id,
    )
    # Blobs-format snapshot @ T2 (newer)
    memory_backend.upload_bytes(
        json.dumps({"timestamp": "2026-02-01T00-00-00Z", "files": {}}).encode(),
        f"2026-02-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id,
    )

    mgr = _make_manager(cfg_full, memory_backend)
    listing = mgr.list()
    timestamps = [s["timestamp"] for s in listing]
    formats = {s["timestamp"]: s["format"] for s in listing}
    # Newest first
    assert timestamps == sorted(timestamps, reverse=True)
    assert formats["2026-02-01T00-00-00Z"] == "blobs"
    assert formats["2026-01-01T00-00-00Z"] == "full"


def test_list_empty_returns_empty(cfg, memory_backend):
    """No snapshots on remote → empty list."""
    mgr = _make_manager(cfg, memory_backend)
    assert mgr.list() == []


# ---------------------------------------------------------------------------
# 3. Restore
# ---------------------------------------------------------------------------


def test_restore_full_writes_files_to_project(
    cfg, memory_backend, write_files, project_dir, tmp_path,
):
    """Full-format restore writes each file from the snapshot folder."""
    write_files({"a.md": "A", "b.md": "B"})
    parent_id, _ = memory_backend.resolve_path("a.md", "ROOT")
    memory_backend.upload_bytes(b"A", "a.md", "ROOT")
    memory_backend.upload_bytes(b"B", "b.md", "ROOT")

    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    # Wipe local copies so we can verify restore re-creates them.
    (project_dir / "a.md").unlink()
    (project_dir / "b.md").unlink()

    mgr.restore(timestamp=ts, output_path=str(project_dir))
    assert (project_dir / "a.md").read_bytes() == b"A"
    assert (project_dir / "b.md").read_bytes() == b"B"


def test_restore_blob_resolves_blobs_back_to_files(
    make_config, memory_backend, write_files, project_dir,
):
    """Blob-format restore reads manifest, downloads blobs, materialises files."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "ALPHA", "nested/b.md": "BETA"})
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    # Wipe local; restore must reconstruct from blobs.
    (project_dir / "a.md").unlink()
    (project_dir / "nested" / "b.md").unlink()

    mgr.restore(timestamp=ts, output_path=str(project_dir))
    assert (project_dir / "a.md").read_bytes() == b"ALPHA"
    assert (project_dir / "nested" / "b.md").read_bytes() == b"BETA"


def test_restore_with_output_path_does_not_touch_project(
    make_config, memory_backend, write_files, project_dir, tmp_path,
):
    """`output_path=tmp` writes there and leaves the project untouched."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "ORIGINAL"})
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    # Mutate local AFTER snapshot.
    (project_dir / "a.md").write_text("CHANGED")

    out_dir = tmp_path / "recovery"
    mgr.restore(timestamp=ts, output_path=str(out_dir))

    assert (out_dir / "a.md").read_bytes() == b"ORIGINAL"
    # Project untouched.
    assert (project_dir / "a.md").read_text() == "CHANGED"


def test_restore_specific_paths_only(
    make_config, memory_backend, write_files, project_dir, tmp_path,
):
    """`paths=["foo.md"]` restores only the named file."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"foo.md": "F", "bar.md": "B", "baz.md": "Z"})
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    out_dir = tmp_path / "out"
    mgr.restore(timestamp=ts, output_path=str(out_dir), paths=["foo.md"])
    assert (out_dir / "foo.md").exists()
    assert not (out_dir / "bar.md").exists()
    assert not (out_dir / "baz.md").exists()


def test_restore_falls_back_to_secondary_backend(
    make_config, memory_backend, write_files, project_dir, tmp_path,
):
    """Primary lacks the snapshot; mirror has it → fallback restores from mirror."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    mirror = InMemoryBackend(name="mirror", root_folder="ROOT")
    write_files({"only.md": "FROM_MIRROR"})

    # Snapshot only on mirror (run create against a mirror-only manager).
    mirror_mgr = _make_manager(cfg, mirror)
    ts = mirror_mgr.create(action="push", files_changed=[])

    # Restore-time manager: primary is the empty memory_backend, mirror has snapshot.
    mgr = SnapshotManager(cfg, memory_backend, mirrors=[mirror])
    out_dir = tmp_path / "recovered"
    mgr.restore(timestamp=ts, output_path=str(out_dir))
    assert (out_dir / "only.md").read_bytes() == b"FROM_MIRROR"


# ---------------------------------------------------------------------------
# 4. Forget
# ---------------------------------------------------------------------------


def _seed_snapshots(backend, timestamps, fmt="full"):
    """Inject N snapshot artifacts on `backend` directly. Each timestamp
    becomes either a folder+meta (full) or a manifest JSON (blobs)."""
    snaps_id = backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    out = []
    for ts in timestamps:
        if fmt == "full":
            folder_id = backend.get_or_create_folder(ts, snaps_id)
            backend.upload_bytes(
                json.dumps({"timestamp": ts, "format": "full"}).encode(),
                SNAPSHOT_META_FILE, folder_id,
            )
            out.append(folder_id)
        else:
            mid = backend.upload_bytes(
                json.dumps({"timestamp": ts, "files": {}}).encode(),
                f"{ts}{MANIFEST_SUFFIX}", snaps_id,
            )
            out.append(mid)
    return out


def test_forget_specific_timestamps_removes_them_dry_run(
    cfg, memory_backend,
):
    """Dry-run reports what would go but doesn't delete anything."""
    _seed_snapshots(memory_backend, [
        "2026-01-01T00-00-00Z",
        "2026-02-01T00-00-00Z",
    ])
    mgr = _make_manager(cfg, memory_backend)

    pre_delete = list(memory_backend.delete_calls)
    result = mgr.forget(
        timestamps=["2026-01-01T00-00-00Z"], dry_run=True,
    )
    assert result["selected"] == 1
    assert result["deleted"] == 0
    # No deletes happened.
    assert memory_backend.delete_calls == pre_delete


def test_forget_with_delete_flag_actually_removes(cfg, memory_backend):
    """`dry_run=False` actually deletes the targeted snapshot folder."""
    folder_ids = _seed_snapshots(memory_backend, [
        "2026-01-01T00-00-00Z",
        "2026-02-01T00-00-00Z",
    ])
    mgr = _make_manager(cfg, memory_backend)
    result = mgr.forget(
        timestamps=["2026-01-01T00-00-00Z"], dry_run=False,
    )
    assert result["deleted"] == 1
    # The targeted folder is gone from the in-memory tree.
    assert folder_ids[0] not in memory_backend._nodes
    # The other one stays.
    assert folder_ids[1] in memory_backend._nodes


def test_forget_keep_last_n(cfg, memory_backend):
    """`keep_last=2` keeps the 2 newest, deletes the rest."""
    timestamps = [
        "2026-01-01T00-00-00Z",
        "2026-02-01T00-00-00Z",
        "2026-03-01T00-00-00Z",
        "2026-04-01T00-00-00Z",
    ]
    _seed_snapshots(memory_backend, timestamps)
    mgr = _make_manager(cfg, memory_backend)
    result = mgr.forget(keep_last=2, dry_run=False)
    assert result["selected"] == 2
    assert result["deleted"] == 2

    remaining = mgr.list()
    remaining_ts = {s["timestamp"] for s in remaining}
    assert remaining_ts == {
        "2026-04-01T00-00-00Z",
        "2026-03-01T00-00-00Z",
    }


def test_forget_keep_days_n(cfg, memory_backend, monkeypatch):
    """`keep_days=N` keeps anything within last N days."""
    from datetime import datetime, timezone

    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 4, 1, tzinfo=tz or timezone.utc)

    monkeypatch.setattr(snap_mod, "datetime", _FixedDateTime)

    timestamps = [
        "2026-01-01T00-00-00Z",  # 90 days old
        "2026-03-25T00-00-00Z",  # 7 days old
        "2026-03-30T00-00-00Z",  # 2 days old
    ]
    _seed_snapshots(memory_backend, timestamps)
    mgr = _make_manager(cfg, memory_backend)

    result = mgr.forget(keep_days=10, dry_run=False)
    # Only the 90-day-old one is older than the 10-day cutoff.
    assert result["selected"] == 1
    remaining = {s["timestamp"] for s in mgr.list()}
    assert remaining == {
        "2026-03-25T00-00-00Z",
        "2026-03-30T00-00-00Z",
    }


def test_forget_before_date(cfg, memory_backend):
    """ISO-date `before` removes everything strictly older than the given date."""
    timestamps = [
        "2026-01-01T00-00-00Z",
        "2026-02-15T00-00-00Z",
        "2026-04-01T00-00-00Z",
    ]
    _seed_snapshots(memory_backend, timestamps)
    mgr = _make_manager(cfg, memory_backend)

    result = mgr.forget(before="2026-03-01", dry_run=False)
    assert result["selected"] == 2  # Jan + Feb older than Mar-01
    remaining = {s["timestamp"] for s in mgr.list()}
    assert remaining == {"2026-04-01T00-00-00Z"}


# ---------------------------------------------------------------------------
# 5. GC
# ---------------------------------------------------------------------------


def _seed_blob(backend, content: bytes) -> tuple[str, str]:
    """Add a content-addressed blob to the in-memory backend's blobs folder.
    Returns (sha256, file_id)."""
    sha = hashlib.sha256(content).hexdigest()
    blobs_id = backend.get_or_create_folder(BLOBS_FOLDER, "ROOT")
    prefix_id = backend.get_or_create_folder(sha[:2], blobs_id)
    fid = backend.upload_bytes(content, sha, prefix_id)
    return sha, fid


def _seed_blob_on(backend, content: bytes) -> tuple[str, str]:
    """Like _seed_blob but uses the backend's own configured root folder
    (e.g. 'MIRROR_ROOT' for a mirror in the per-backend gc tests below)."""
    sha = hashlib.sha256(content).hexdigest()
    blobs_id = backend.get_or_create_folder(BLOBS_FOLDER, backend._root_id)
    prefix_id = backend.get_or_create_folder(sha[:2], blobs_id)
    fid = backend.upload_bytes(content, sha, prefix_id)
    return sha, fid


def test_gc_lists_orphan_blobs_dry_run(cfg, memory_backend):
    """Blobs not referenced by any manifest are listed in dry-run."""
    referenced_sha, _ = _seed_blob(memory_backend, b"keep")
    orphan_sha, _ = _seed_blob(memory_backend, b"orphan")

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    memory_backend.upload_bytes(
        json.dumps({
            "timestamp": "2026-01-01T00-00-00Z",
            "files": {"a.md": referenced_sha},
        }).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id,
    )

    mgr = _make_manager(cfg, memory_backend)
    pre_delete = list(memory_backend.delete_calls)
    result = mgr.gc(dry_run=True)
    assert result["orphans"] == 1
    assert result["deleted"] == 0
    assert memory_backend.delete_calls == pre_delete


def test_gc_with_delete_actually_removes_orphans(cfg, memory_backend):
    """Non-dry-run gc deletes orphan blobs."""
    referenced_sha, _ = _seed_blob(memory_backend, b"keep")
    orphan_sha, orphan_fid = _seed_blob(memory_backend, b"orphan")

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    memory_backend.upload_bytes(
        json.dumps({
            "timestamp": "2026-01-01T00-00-00Z",
            "files": {"a.md": referenced_sha},
        }).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id,
    )

    mgr = _make_manager(cfg, memory_backend)
    result = mgr.gc(dry_run=False)
    assert result["deleted"] == 1
    assert orphan_fid not in memory_backend._nodes


def test_gc_preserves_blobs_referenced_by_at_least_one_manifest(
    cfg, memory_backend,
):
    """A blob referenced by ANY manifest is kept."""
    sha_a, fid_a = _seed_blob(memory_backend, b"shared")
    sha_b, fid_b = _seed_blob(memory_backend, b"unique")
    sha_c, fid_c = _seed_blob(memory_backend, b"orphan-content")

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    memory_backend.upload_bytes(
        json.dumps({
            "timestamp": "2026-01-01T00-00-00Z",
            "files": {"a.md": sha_a},
        }).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id,
    )
    memory_backend.upload_bytes(
        json.dumps({
            "timestamp": "2026-02-01T00-00-00Z",
            "files": {"b.md": sha_a, "c.md": sha_b},
        }).encode(),
        f"2026-02-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id,
    )

    mgr = _make_manager(cfg, memory_backend)
    result = mgr.gc(dry_run=False)
    assert result["deleted"] == 1
    assert fid_a in memory_backend._nodes  # referenced by both manifests
    assert fid_b in memory_backend._nodes  # referenced by manifest 2
    assert fid_c not in memory_backend._nodes  # orphan


def test_gc_default_targets_primary_backend(cfg, make_config, memory_backend):
    """Without --backend, gc operates on the primary (back-compat with
    pre-v0.5.35 behaviour). A configured mirror with its own orphan is
    NOT touched when gc runs against the primary."""
    # Seed orphan on primary.
    primary_orphan_sha, primary_orphan_fid = _seed_blob(memory_backend, b"primary-orphan")
    primary_ref_sha, _ = _seed_blob(memory_backend, b"primary-keep")
    snaps_p = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    memory_backend.upload_bytes(
        json.dumps({"timestamp": "2026-01-01T00-00-00Z",
                    "files": {"a.md": primary_ref_sha}}).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_p,
    )
    # Build a mirror with its own orphan; mgr should NOT touch it.
    mirror = InMemoryBackend(name="sftp", root_folder="MIRROR_ROOT")
    mirror.config = _MirrorConfigStub("MIRROR_ROOT")
    mirror_orphan_sha, mirror_orphan_fid = _seed_blob_on(mirror, b"mirror-orphan")

    mgr = _make_manager(cfg, memory_backend, mirrors=[mirror])
    result = mgr.gc(dry_run=False)  # no backend_name → primary
    assert result["deleted"] == 1
    assert primary_orphan_fid not in memory_backend._nodes
    # Mirror's orphan is intentionally untouched.
    assert mirror_orphan_fid in mirror._nodes


def test_gc_targets_named_mirror(cfg, memory_backend):
    """gc(backend_name='sftp') operates on the SFTP mirror's blob store
    + manifests, leaving the primary alone."""
    # Primary has an orphan but mgr should NOT touch it when --backend sftp.
    primary_orphan_sha, primary_orphan_fid = _seed_blob(memory_backend, b"primary-orphan")
    primary_ref_sha, _ = _seed_blob(memory_backend, b"primary-keep")
    snaps_p = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    memory_backend.upload_bytes(
        json.dumps({"timestamp": "2026-01-01T00-00-00Z",
                    "files": {"a.md": primary_ref_sha}}).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_p,
    )
    # Mirror has its own orphan + own manifest.
    mirror = InMemoryBackend(name="sftp", root_folder="MIRROR_ROOT")
    mirror.config = _MirrorConfigStub("MIRROR_ROOT")
    m_orphan_sha, m_orphan_fid = _seed_blob_on(mirror, b"mirror-orphan")
    m_ref_sha, m_ref_fid = _seed_blob_on(mirror, b"mirror-keep")
    snaps_m = mirror.get_or_create_folder(SNAPSHOTS_FOLDER, "MIRROR_ROOT")
    mirror.upload_bytes(
        json.dumps({"timestamp": "2026-01-01T00-00-00Z",
                    "files": {"a.md": m_ref_sha}}).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_m,
    )

    mgr = _make_manager(cfg, memory_backend, mirrors=[mirror])
    result = mgr.gc(dry_run=False, backend_name="sftp")
    assert result["deleted"] == 1
    assert m_orphan_fid not in mirror._nodes      # mirror orphan deleted
    assert m_ref_fid in mirror._nodes              # mirror referenced kept
    assert primary_orphan_fid in memory_backend._nodes  # primary untouched


def test_gc_with_unknown_backend_name_raises(cfg, memory_backend):
    """gc(backend_name='nonexistent') raises ValueError naming the
    available backends — clean error rather than silently operating
    on the wrong target."""
    mgr = _make_manager(cfg, memory_backend)
    with pytest.raises(ValueError) as excinfo:
        mgr.gc(dry_run=True, backend_name="nonexistent")
    assert "nonexistent" in str(excinfo.value)


def test_gc_backend_name_matching_primary_works(cfg, memory_backend):
    """Explicitly passing the primary's backend_name should target the
    primary — same outcome as the default no-arg call."""
    orphan_sha, orphan_fid = _seed_blob(memory_backend, b"primary-orphan")
    ref_sha, _ = _seed_blob(memory_backend, b"primary-keep")
    snaps = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    memory_backend.upload_bytes(
        json.dumps({"timestamp": "2026-01-01T00-00-00Z",
                    "files": {"a.md": ref_sha}}).encode(),
        f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps,
    )
    mgr = _make_manager(cfg, memory_backend)
    result = mgr.gc(dry_run=False, backend_name="primary")  # the InMemoryBackend's name
    assert result["deleted"] == 1
    assert orphan_fid not in memory_backend._nodes


# ---------------------------------------------------------------------------
# 6. Migrate
# ---------------------------------------------------------------------------


def test_migrate_full_to_blobs_creates_manifest_per_snapshot(
    cfg, memory_backend, write_files,
):
    """Convert full snapshots to blobs format; each gets a manifest."""
    write_files({"a.md": "ALPHA"})
    # Seed two full-format snapshots.
    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    for ts in ("2026-01-01T00-00-00Z", "2026-02-01T00-00-00Z"):
        folder_id = memory_backend.get_or_create_folder(ts, snaps_id)
        memory_backend.upload_bytes(b"ALPHA", "a.md", folder_id)
        memory_backend.upload_bytes(
            json.dumps({"timestamp": ts, "format": "full"}).encode(),
            SNAPSHOT_META_FILE, folder_id,
        )

    mgr = _make_manager(cfg, memory_backend)
    result = mgr.migrate(target="blobs", dry_run=False)
    assert result["converted"] == 2
    assert result["errors"] == 0

    # Both timestamps now have a manifest at top-level of snapshots.
    assert (
        memory_backend.get_file_id(
            f"2026-01-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id
        )
        is not None
    )
    assert (
        memory_backend.get_file_id(
            f"2026-02-01T00-00-00Z{MANIFEST_SUFFIX}", snaps_id
        )
        is not None
    )


def test_migrate_blobs_to_full_reconstructs_full_folders(
    make_config, memory_backend, write_files,
):
    """Convert blobs snapshots to full format; each becomes a folder of files."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "ALPHA", "b.md": "BETA"})
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    # Now migrate to full.
    result = mgr.migrate(target="full", dry_run=False)
    assert result["converted"] == 1
    assert result["errors"] == 0

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    full_folders = memory_backend.list_folders(snaps_id, name=ts)
    assert len(full_folders) == 1
    folder_id = full_folders[0]["id"]
    contents = memory_backend.list_files_recursive(folder_id)
    rels = {c["relative_path"] for c in contents}
    assert "a.md" in rels
    assert "b.md" in rels
    assert SNAPSHOT_META_FILE in rels


def test_migrate_idempotent_when_already_target_format(
    make_config, memory_backend, write_files,
):
    """Re-running migrate against the same target is a no-op (zero converts)."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"x.md": "X"})
    mgr = _make_manager(cfg, memory_backend)
    mgr.create(action="push", files_changed=[])

    # First migrate (already in target format from create).
    r1 = mgr.migrate(target="blobs", dry_run=False)
    assert r1["converted"] == 0

    # Second migrate — still nothing to do.
    r2 = mgr.migrate(target="blobs", dry_run=False)
    assert r2["converted"] == 0
    assert r2["errors"] == 0


# ---------------------------------------------------------------------------
# 7. History + Inspect
# ---------------------------------------------------------------------------


def test_history_lists_snapshots_containing_path(
    make_config, memory_backend, write_files, project_dir, stepped_clock,
):
    """`history(path)` returns every snapshot where the path appears."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "v1", "b.md": "B"})
    mgr = _make_manager(cfg, memory_backend)
    ts1 = mgr.create(action="push", files_changed=[])

    # Mutate a.md and snapshot again.
    (project_dir / "a.md").write_text("v2")
    ts2 = mgr.create(action="push", files_changed=["a.md"])
    assert ts1 != ts2

    result = mgr.history("a.md")
    assert result["total_appearances"] == 2
    timestamps = {e["timestamp"] for e in result["entries"]}
    assert timestamps == {ts1, ts2}


def test_history_groups_by_sha_to_show_versions(
    make_config, memory_backend, write_files, project_dir, stepped_clock,
):
    """Same content across N snapshots groups under one version label."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "STABLE"})
    mgr = _make_manager(cfg, memory_backend)
    mgr.create(action="push", files_changed=[])
    mgr.create(action="push", files_changed=[])
    (project_dir / "a.md").write_text("CHANGED")
    mgr.create(action="push", files_changed=["a.md"])

    result = mgr.history("a.md")
    # Two distinct SHAs ⇒ two version labels.
    assert result["distinct_versions"] == 2
    assert result["total_appearances"] == 3
    versions = {e["version"] for e in result["entries"]}
    assert versions == {"v1", "v2"}


def test_inspect_with_paths_filter(
    make_config, memory_backend, write_files,
):
    """show_inspect's path_filter narrows the displayed list. fnmatch
    glob `memory/*` matches every path under `memory/` (fnmatch is
    greedy across `/` — that's the documented contract)."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({
        "memory/note.md": "N1",
        "memory/sub/deep.md": "N2",
        "other/skip.md": "S",
    })
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="push", files_changed=[])

    full = mgr.inspect(ts)
    all_paths = {f["path"] for f in full["files"]}
    assert all_paths == {"memory/note.md", "memory/sub/deep.md", "other/skip.md"}

    # Apply the path filter (replicates show_inspect's filtering logic).
    import fnmatch
    filtered = [f for f in full["files"] if fnmatch.fnmatch(f["path"], "memory/*")]
    filtered_paths = {f["path"] for f in filtered}
    # `memory/*` matches everything under memory/; `other/skip.md` is excluded.
    assert filtered_paths == {"memory/note.md", "memory/sub/deep.md"}
    assert "other/skip.md" not in filtered_paths
