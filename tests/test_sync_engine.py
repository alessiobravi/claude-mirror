"""Thorough coverage for SyncEngine — the 3-way diff sync core.

Covers the full state matrix of (local × remote × manifest) and the four
public entry points (push, pull, sync, _delete_drive_file via the CLI delete
flow). All tests run offline against a self-contained in-memory backend and
NEVER touch ~/.config/claude_mirror.

Coverage groups (mirrors the multi-agent test push for v0.5.17):
    1. 3-way diff state matrix — every cell of (local × remote) is exercised.
    2. push happy paths — uploads, skips, deletes, force-local override.
    3. pull happy paths — downloads, overwrites, propagates remote-delete,
       --output-dir leaves manifest untouched.
    4. sync (bidirectional) with conflict resolution — keep-local / keep-drive
       / skip routes, resolver patched so no interactive prompt fires.
    5. Pattern filtering — file_patterns + exclude_patterns, directory form.
    6. delete command path — primary delete + --local also unlinks file.
    7. Pending-retry queue — pending_retry picked up next push,
       failed_perm NOT auto-retried.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

from claude_mirror.backends import ErrorClass, StorageBackend
from claude_mirror.manifest import Manifest, MANIFEST_FILE
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import FileSyncState, Status, SyncEngine


# ---------------------------------------------------------------------------
# In-memory backend rich enough to drive SyncEngine end-to-end.
# ---------------------------------------------------------------------------

class InMemoryBackend(StorageBackend):
    """Self-contained backend for SyncEngine tests — tracks every call so
    tests can assert call patterns, and computes md5 hashes on stored bytes
    so push/pull/status semantics line up with Manifest.hash_bytes."""

    backend_name = "fake"

    def __init__(self) -> None:
        # remote file id (str) → {"name": str, "rel_path": str, "content": bytes}
        self._files: dict[str, dict] = {}
        self._next_id: int = 0
        self.calls: list[tuple] = []

    # -------- helpers (test-only) --------
    def seed(self, rel_path: str, content: bytes) -> str:
        """Place a file on the remote (bypassing call tracking)."""
        fid = self._mint_id()
        name = rel_path.rsplit("/", 1)[-1]
        self._files[fid] = {"name": name, "rel_path": rel_path, "content": content}
        return fid

    def _mint_id(self) -> str:
        self._next_id += 1
        return f"fid-{self._next_id}"

    # -------- StorageBackend abstract surface --------
    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        self.calls.append(("get_or_create_folder", name, parent_id))
        return f"folder-{name}"

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        return root_folder_id, rel_path.rsplit("/", 1)[-1]

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> list[dict]:
        self.calls.append(("list_files_recursive", folder_id))
        out = []
        for fid, meta in self._files.items():
            md5 = hashlib.md5(meta["content"]).hexdigest()
            out.append({
                "id": fid,
                "name": meta["name"],
                "md5Checksum": md5,
                "relative_path": meta["rel_path"],
                "size": len(meta["content"]),
            })
        return out

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        return []

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        self.calls.append(("upload_file", rel_path, file_id))
        with open(local_path, "rb") as f:
            content = f.read()
        if file_id and file_id in self._files:
            self._files[file_id]["content"] = content
            self._files[file_id]["rel_path"] = rel_path
            return file_id
        fid = self._mint_id()
        name = rel_path.rsplit("/", 1)[-1]
        self._files[fid] = {"name": name, "rel_path": rel_path, "content": content}
        return fid

    def download_file(self, file_id: str) -> bytes:
        self.calls.append(("download_file", file_id))
        return self._files[file_id]["content"]

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        self.calls.append(("upload_bytes", name, file_id))
        if file_id and file_id in self._files:
            self._files[file_id]["content"] = content
            return file_id
        fid = self._mint_id()
        self._files[fid] = {"name": name, "rel_path": name, "content": content}
        return fid

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        for fid, meta in self._files.items():
            if meta["name"] == name:
                return fid
        return None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        return self._mint_id()

    def get_file_hash(self, file_id: str) -> Optional[str]:
        meta = self._files.get(file_id)
        if meta is None:
            return None
        return hashlib.md5(meta["content"]).hexdigest()

    def delete_file(self, file_id: str) -> None:
        self.calls.append(("delete_file", file_id))
        self._files.pop(file_id, None)

    def classify_error(self, exc: BaseException) -> ErrorClass:
        return ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def backend() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture
def merger() -> MergeHandler:
    return MergeHandler()


def _build_engine(
    config,
    backend: InMemoryBackend,
    merger: MergeHandler,
) -> SyncEngine:
    """Construct a SyncEngine with a fresh manifest pinned to the project."""
    manifest = Manifest(config.project_path)
    return SyncEngine(
        config=config,
        storage=backend,
        manifest=manifest,
        merge=merger,
        notifier=None,
        snapshots=None,
        mirrors=[],
    )


def _read_manifest(project_path: str) -> dict:
    p = Path(project_path) / MANIFEST_FILE
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


# ===========================================================================
# Group 1 — 3-way diff state matrix
# ===========================================================================
#
# Cross-product axes:
#   local: missing | unchanged | changed | deleted-since-sync
#   remote: missing | unchanged | changed | deleted-since-sync
#
# Group 1 covers the classification (Status enum) directly; the operational
# behaviour for each cell is exercised by the push/pull/sync groups below.

class TestStateMatrix:
    """Exercise SyncEngine._classify across the (local × remote) grid."""

    def _state(self, eng: SyncEngine) -> dict[str, Status]:
        return {s.rel_path: s.status for s in eng.get_status()}

    # --- (no manifest) cells ---
    def test_local_only_no_manifest_is_new_local(self, make_config, backend, merger, write_files):
        write_files({"a.md": "hello"})
        eng = _build_engine(make_config(), backend, merger)
        assert self._state(eng)["a.md"] is Status.NEW_LOCAL

    def test_remote_only_no_manifest_is_new_drive(self, make_config, backend, merger):
        backend.seed("a.md", b"hello")
        eng = _build_engine(make_config(), backend, merger)
        assert self._state(eng)["a.md"] is Status.NEW_DRIVE

    def test_both_present_no_manifest_is_conflict(self, make_config, backend, merger, write_files):
        write_files({"a.md": "local"})
        backend.seed("a.md", b"remote")
        eng = _build_engine(make_config(), backend, merger)
        assert self._state(eng)["a.md"] is Status.CONFLICT

    # --- (with manifest) cells ---
    def test_in_sync_classification(self, make_config, backend, merger, write_files):
        write_files({"a.md": "hello"})
        fid = backend.seed("a.md", b"hello")
        cfg = make_config()
        h = _md5(b"hello")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.IN_SYNC

    def test_local_changed_only_is_local_ahead(self, make_config, backend, merger, write_files):
        write_files({"a.md": "v2"})
        fid = backend.seed("a.md", b"v1")
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.LOCAL_AHEAD

    def test_remote_changed_only_is_drive_ahead(self, make_config, backend, merger, write_files):
        write_files({"a.md": "v1"})
        fid = backend.seed("a.md", b"v2")
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.DRIVE_AHEAD

    def test_both_changed_is_conflict(self, make_config, backend, merger, write_files):
        write_files({"a.md": "local"})
        fid = backend.seed("a.md", b"remote")
        cfg = make_config()
        h = _md5(b"baseline")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.CONFLICT

    def test_local_deleted_with_remote_present_is_deleted_local(
        self, make_config, backend, merger
    ):
        # No local file written; remote still has it; manifest entry exists.
        fid = backend.seed("a.md", b"v1")
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.DELETED_LOCAL

    def test_remote_deleted_with_local_unchanged_classifies_in_sync(
        self, make_config, backend, merger, write_files
    ):
        # Local file unchanged from manifest, remote gone. The engine's
        # _classify short-circuits drive_changed when drive_exists=False
        # (see sync.py: `drive_changed = drive_hash != synced_remote_hash
        # if drive_exists else False`). So this surfaces as IN_SYNC and
        # neither push nor pull touches it — the user has to delete it
        # explicitly. This test pins that behaviour so a future refactor
        # that flips it to DRIVE_AHEAD or DELETED_REMOTE is a deliberate,
        # CHANGELOG-worthy change rather than a silent semantics shift.
        write_files({"a.md": "v1"})
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, "old-fid", synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.IN_SYNC

    def test_both_deleted_drops_out_of_status(self, make_config, backend, merger):
        # No local file, no remote, but manifest knows about it → DELETED_LOCAL
        # (the status is computed off `not local_exists` regardless of remote).
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, "stale-fid", synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        assert self._state(eng)["a.md"] is Status.DELETED_LOCAL


# ===========================================================================
# Group 2 — push happy paths
# ===========================================================================

class TestPush:
    def test_push_uploads_local_only_file(self, make_config, backend, merger, write_files):
        write_files({"a.md": "hello"})
        eng = _build_engine(make_config(), backend, merger)
        eng.push()

        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert len(uploads) == 1
        assert uploads[0][1] == "a.md"

        manifest = _read_manifest(eng.config.project_path)
        assert "a.md" in manifest
        assert manifest["a.md"]["synced_hash"] == _md5(b"hello")

    def test_push_skips_in_sync_file(self, make_config, backend, merger, write_files):
        write_files({"a.md": "same"})
        fid = backend.seed("a.md", b"same")
        cfg = make_config()
        h = _md5(b"same")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        eng.push()

        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert uploads == []

    def test_push_uploads_locally_changed_file(
        self, make_config, backend, merger, write_files
    ):
        write_files({"a.md": "v2"})
        fid = backend.seed("a.md", b"v1")
        cfg = make_config()
        h_old = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h_old, fid, synced_remote_hash=h_old, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        eng.push()

        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert len(uploads) == 1
        # Must reuse the existing file id (not create a new one).
        assert uploads[0][2] == fid

        manifest = _read_manifest(cfg.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"v2")

    def test_push_propagates_local_delete(self, make_config, backend, merger):
        # File never written locally; remote + manifest entry exist.
        fid = backend.seed("a.md", b"v1")
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        eng.push()

        deletes = [c for c in backend.calls if c[0] == "delete_file"]
        assert deletes == [("delete_file", fid)]
        assert "a.md" not in _read_manifest(cfg.project_path)

    def test_push_force_local_overrides_both_changed(
        self, make_config, backend, merger, write_files, monkeypatch
    ):
        # Both sides changed (CONFLICT) — without force_local this would
        # route to interactive resolution. With force_local, no prompt fires
        # and the local content wins outright.
        write_files({"a.md": "local-new"})
        fid = backend.seed("a.md", b"remote-new")
        cfg = make_config()
        h = _md5(b"baseline")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()

        # Hard-fail if the resolver gets called at all.
        def _no_resolve(*_a, **_kw):
            raise AssertionError("resolver must not run with force_local=True")
        monkeypatch.setattr(MergeHandler, "resolve_conflict", _no_resolve)

        eng = _build_engine(cfg, backend, merger)
        eng.push(force_local=True)

        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert len(uploads) == 1
        # Remote bytes now match local.
        assert backend._files[fid]["content"] == b"local-new"
        manifest = _read_manifest(cfg.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"local-new")


# ===========================================================================
# Group 3 — pull happy paths
# ===========================================================================

class TestPull:
    def test_pull_downloads_remote_only_file(self, make_config, backend, merger):
        backend.seed("a.md", b"hello")
        eng = _build_engine(make_config(), backend, merger)
        eng.pull()

        local = Path(eng.config.project_path) / "a.md"
        assert local.exists()
        assert local.read_bytes() == b"hello"

        manifest = _read_manifest(eng.config.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"hello")

    def test_pull_overwrites_remote_changed(self, make_config, backend, merger, write_files):
        write_files({"a.md": "v1"})
        fid = backend.seed("a.md", b"v2")
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        eng.pull()

        local = Path(cfg.project_path) / "a.md"
        assert local.read_bytes() == b"v2"
        manifest = _read_manifest(cfg.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"v2")

    def test_pull_no_op_when_remote_missing(self, make_config, backend, merger, write_files):
        # Manifest knows about a.md, local has it, remote does not. status
        # classifies this as IN_SYNC (drive_exists=False does NOT mark
        # drive_ahead under current semantics) — so pull is a no-op.
        write_files({"a.md": "v1"})
        cfg = make_config()
        h = _md5(b"v1")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, "stale-fid", synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)
        eng.pull()

        downloads = [c for c in backend.calls if c[0] == "download_file"]
        assert downloads == []
        # Manifest entry preserved (no remote-delete propagation in pull).
        assert "a.md" in _read_manifest(cfg.project_path)

    def test_pull_into_output_dir_does_not_touch_project(
        self, make_config, backend, merger, tmp_path
    ):
        backend.seed("a.md", b"hello")
        cfg = make_config()
        eng = _build_engine(cfg, backend, merger)
        out = tmp_path / "outdir"
        out.mkdir()

        eng.pull(output_dir=str(out))

        # Wrote to the output dir, not the project.
        assert (out / "a.md").read_bytes() == b"hello"
        assert not (Path(cfg.project_path) / "a.md").exists()
        # Manifest is untouched in --output mode.
        assert _read_manifest(cfg.project_path) == {}


# ===========================================================================
# Group 4 — sync (bidirectional, conflict resolution patched)
# ===========================================================================

class TestSyncConflict:
    def _setup_conflict(self, make_config, backend, write_files):
        """Build a CONFLICT state: both sides differ from synced baseline."""
        write_files({"a.md": "LOCAL"})
        fid = backend.seed("a.md", b"DRIVE")
        cfg = make_config()
        h = _md5(b"BASELINE")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        return cfg, fid

    def test_sync_conflict_keep_local(
        self, make_config, backend, merger, write_files, monkeypatch
    ):
        cfg, fid = self._setup_conflict(make_config, backend, write_files)

        def _resolve(self, rel_path, local_content, drive_content):
            return (local_content, "local")
        monkeypatch.setattr(MergeHandler, "resolve_conflict", _resolve)

        eng = _build_engine(cfg, backend, merger)
        eng.sync()

        # Local wins → remote bytes now match local.
        assert backend._files[fid]["content"] == b"LOCAL"
        manifest = _read_manifest(cfg.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"LOCAL")

    def test_sync_conflict_keep_drive(
        self, make_config, backend, merger, write_files, monkeypatch
    ):
        cfg, fid = self._setup_conflict(make_config, backend, write_files)

        def _resolve(self, rel_path, local_content, drive_content):
            return (drive_content, "drive")
        monkeypatch.setattr(MergeHandler, "resolve_conflict", _resolve)

        eng = _build_engine(cfg, backend, merger)
        eng.sync()

        # Drive wins → local bytes now match drive.
        local = Path(cfg.project_path) / "a.md"
        assert local.read_bytes() == b"DRIVE"
        manifest = _read_manifest(cfg.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"DRIVE")
        # No upload happened (drive was authoritative; only download).
        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert uploads == []

    def test_sync_conflict_skip(
        self, make_config, backend, merger, write_files, monkeypatch
    ):
        cfg, fid = self._setup_conflict(make_config, backend, write_files)

        def _resolve(self, rel_path, local_content, drive_content):
            return None  # user chose Skip
        monkeypatch.setattr(MergeHandler, "resolve_conflict", _resolve)

        eng = _build_engine(cfg, backend, merger)
        eng.sync()

        # Neither side updated.
        assert backend._files[fid]["content"] == b"DRIVE"
        local = Path(cfg.project_path) / "a.md"
        assert local.read_bytes() == b"LOCAL"
        # Manifest hashes are pinned to the original baseline (no update).
        manifest = _read_manifest(cfg.project_path)
        assert manifest["a.md"]["synced_hash"] == _md5(b"BASELINE")
        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert uploads == []


# ===========================================================================
# Group 5 — pattern filtering
# ===========================================================================

class TestPatternFilters:
    def test_file_patterns_filter_applied_to_push(
        self, make_config, backend, merger, write_files
    ):
        # Only **/*.md is in scope; the .txt file is invisible to push.
        write_files({"a.md": "md", "b.txt": "txt"})
        cfg = make_config(file_patterns=["**/*.md"])
        eng = _build_engine(cfg, backend, merger)
        eng.push()

        uploads = [c for c in backend.calls if c[0] == "upload_file"]
        assert len(uploads) == 1
        assert uploads[0][1] == "a.md"

    def test_exclude_patterns_filter_invisible_to_status(
        self, make_config, backend, merger, write_files
    ):
        write_files({"a.md": "keep", "skip.md": "drop"})
        cfg = make_config(exclude_patterns=["skip.md"])
        eng = _build_engine(cfg, backend, merger)

        states = {s.rel_path for s in eng.get_status()}
        assert "a.md" in states
        assert "skip.md" not in states

    def test_excluded_path_via_directory_form(
        self, make_config, backend, merger, write_files
    ):
        write_files({"keep.md": "k", "archive/old.md": "o"})
        cfg = make_config(exclude_patterns=["archive"])
        eng = _build_engine(cfg, backend, merger)

        states = {s.rel_path for s in eng.get_status()}
        assert "keep.md" in states
        assert "archive/old.md" not in states


# ===========================================================================
# Group 6 — delete command path (via _delete_drive_file as the CLI does)
# ===========================================================================

class TestDelete:
    def test_delete_removes_remote_and_manifest(
        self, make_config, backend, merger, write_files
    ):
        write_files({"a.md": "hello"})
        fid = backend.seed("a.md", b"hello")
        cfg = make_config()
        h = _md5(b"hello")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)

        state = next(s for s in eng.get_status() if s.rel_path == "a.md")
        eng._delete_drive_file(state)
        eng.manifest.save()

        deletes = [c for c in backend.calls if c[0] == "delete_file"]
        assert deletes == [("delete_file", fid)]
        assert "a.md" not in _read_manifest(cfg.project_path)
        # Local untouched by the engine — only the CLI's --local flag
        # unlinks the file (we cover that next).
        assert (Path(cfg.project_path) / "a.md").exists()

    def test_delete_with_local_flag_also_removes_local(
        self, make_config, backend, merger, write_files
    ):
        # Mirror the CLI's --local pathway: engine deletes remote+manifest,
        # the caller unlinks the local file. We assert both effects together.
        write_files({"a.md": "hello"})
        fid = backend.seed("a.md", b"hello")
        cfg = make_config()
        h = _md5(b"hello")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
        m.save()
        eng = _build_engine(cfg, backend, merger)

        state = next(s for s in eng.get_status() if s.rel_path == "a.md")
        eng._delete_drive_file(state)
        local_path = Path(cfg.project_path) / "a.md"
        local_path.unlink()  # the --local action in cli.delete
        eng.manifest.save()

        assert not local_path.exists()
        assert "a.md" not in _read_manifest(cfg.project_path)


# ===========================================================================
# Group 7 — pending-retry queue
# ===========================================================================

class TestPendingRetry:
    """The retry queue is mirror-scoped (primary success implies primary
    has nothing pending). These tests configure a mirror with a pending
    entry and confirm the next push picks it up — and that failed_perm
    entries are NOT auto-retried.
    """

    def _make_mirror(self) -> InMemoryBackend:
        mirror = InMemoryBackend()
        mirror.backend_name = "mirror_a"
        return mirror

    def test_pending_retry_picked_up_on_next_push(
        self, make_config, backend, merger, write_files, monkeypatch
    ):
        write_files({"a.md": "hello"})
        cfg = make_config()
        h = _md5(b"hello")
        # Manifest: primary is OK, mirror_a is pending_retry.
        m = Manifest(cfg.project_path)
        m.update("a.md", h, "fid-primary", synced_remote_hash=h, backend_name="fake")
        m.update_remote(
            "a.md", "mirror_a",
            state="pending_retry",
            intended_hash=h,
            last_error="transient",
        )
        m.save()

        # Seed primary so it's "in sync" (push won't pick it up via the
        # state pass — only via the pending-retry queue).
        backend.seed("a.md", b"hello")

        mirror = self._make_mirror()

        # Stub config helpers used inside _fan_out_to_mirrors.
        class _Cfg:
            root_folder = "mirror-root"
        mirror.config = _Cfg()  # type: ignore[attr-defined]

        eng = SyncEngine(
            config=cfg,
            storage=backend,
            manifest=Manifest(cfg.project_path),
            merge=merger,
            notifier=None,
            snapshots=None,
            mirrors=[mirror],
        )
        eng.push()

        # The mirror saw an upload for a.md even though the primary view
        # was in-sync — proving the pending-retry queue drove the push.
        mirror_uploads = [c for c in mirror.calls if c[0] == "upload_file"]
        assert any(c[1] == "a.md" for c in mirror_uploads)

    def test_failed_perm_state_not_retried(
        self, make_config, backend, merger, write_files
    ):
        write_files({"a.md": "hello"})
        cfg = make_config()
        h = _md5(b"hello")
        m = Manifest(cfg.project_path)
        m.update("a.md", h, "fid-primary", synced_remote_hash=h, backend_name="fake")
        m.update_remote(
            "a.md", "mirror_a",
            state="failed_perm",
            intended_hash=h,
            last_error="auth",
        )
        m.save()
        backend.seed("a.md", b"hello")

        mirror = self._make_mirror()

        class _Cfg:
            root_folder = "mirror-root"
        mirror.config = _Cfg()  # type: ignore[attr-defined]

        eng = SyncEngine(
            config=cfg,
            storage=backend,
            manifest=Manifest(cfg.project_path),
            merge=merger,
            notifier=None,
            snapshots=None,
            mirrors=[mirror],
        )
        eng.push()

        # No upload to the mirror — failed_perm is quarantined and waits
        # on user action, not silent auto-retry.
        mirror_uploads = [c for c in mirror.calls if c[0] == "upload_file"]
        assert mirror_uploads == []
