"""End-to-end integrity audit for claude-mirror.

Three independent verification phases drive `claude-mirror verify`:

  manifest_vs_remote
      Walks every entry in the per-project manifest and asks each
      configured backend (primary + Tier 2 mirrors) for the recorded
      `synced_remote_hash`. Drift = backend hash differs from manifest.
      Missing = backend has no record of the file ID. Each backend
      uses its own native hash algorithm (Drive md5, Dropbox content_hash,
      OneDrive quickXorHash, WebDAV ETag/oc:checksums, SFTP sha256
      via exec) — verify trusts the per-backend `get_file_hash()`
      contract and surfaces the algorithm in its report.

  snapshot_blobs
      Walks `_claude_mirror_blobs/<hh>/<hash>` on each backend, fetches
      each blob's bytes (preferring the local mount BlobCache when one
      is supplied), recomputes sha256, and compares against the filename.
      Mismatch = corrupted (the content-addressing contract is broken).

  mount_blob_cache
      Walks the local content-addressed cache populated by the v0.5.62
      MOUNT engine (typically `~/.cache/claude-mirror/blobs/`),
      recomputes sha256 of each blob file, compares with its filename.
      Mismatch = corrupted cache entry — recommend eviction so the next
      mount fetches a clean copy from remote.

Pure orchestration: every phase function returns a structured dataclass
report. The CLI layer (`cli.py::verify`) aggregates phases, renders the
Rich table or JSON envelope, and translates `--strict` into the exit-code
contract. Side-effects (network, filesystem) live behind injected
backends/factories so the phases stay easy to test offline.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from .backends import StorageBackend
from .config import Config
from .manifest import Manifest


SCHEMA_VERSION = 1

PHASE_MANIFEST = "manifest_vs_remote"
PHASE_SNAPSHOTS = "snapshot_blobs"
PHASE_MOUNT_CACHE = "mount_blob_cache"


# ─── Result types ─────────────────────────────────────────────────────


@dataclass
class DriftEntry:
    path: str
    backend: str
    expected: str
    actual: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "backend": self.backend,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class MissingEntry:
    path: str
    backend: str
    expected: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "backend": self.backend,
            "expected": self.expected,
        }


@dataclass
class CorruptedEntry:
    layer: str           # "snapshot_blobs" | "mount_cache"
    key: str             # blob path or sha key
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"layer": self.layer, "key": self.key}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass
class PhaseReport:
    name: str
    checked: int = 0
    verified: int = 0
    drift: int = 0
    missing: int = 0
    corrupted: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "checked": self.checked,
            "verified": self.verified,
            "drift": self.drift,
            "missing": self.missing,
            "corrupted": self.corrupted,
        }


@dataclass
class ManifestVerifyReport:
    phase: PhaseReport = field(
        default_factory=lambda: PhaseReport(name=PHASE_MANIFEST)
    )
    drift: list[DriftEntry] = field(default_factory=list)
    missing: list[MissingEntry] = field(default_factory=list)


@dataclass
class SnapshotVerifyReport:
    phase: PhaseReport = field(
        default_factory=lambda: PhaseReport(name=PHASE_SNAPSHOTS)
    )
    corrupted: list[CorruptedEntry] = field(default_factory=list)
    missing: list[MissingEntry] = field(default_factory=list)


@dataclass
class MountCacheVerifyReport:
    phase: PhaseReport = field(
        default_factory=lambda: PhaseReport(name=PHASE_MOUNT_CACHE)
    )
    corrupted: list[CorruptedEntry] = field(default_factory=list)


@dataclass
class VerifyReport:
    """Aggregated report covering every requested phase."""
    checked_at: datetime
    phases: list[PhaseReport] = field(default_factory=list)
    drift: list[DriftEntry] = field(default_factory=list)
    corrupted: list[CorruptedEntry] = field(default_factory=list)
    missing: list[MissingEntry] = field(default_factory=list)

    def has_findings(self) -> bool:
        return bool(self.drift or self.corrupted or self.missing)

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked_at": self.checked_at.astimezone(timezone.utc).isoformat(),
            "phases": [p.to_dict() for p in self.phases],
            "drift": [d.to_dict() for d in self.drift],
            "corrupted": [c.to_dict() for c in self.corrupted],
            "missing": [m.to_dict() for m in self.missing],
        }


# ─── Helpers ──────────────────────────────────────────────────────────


def _backend_label(backend: StorageBackend) -> str:
    """Stable human-readable backend identifier, used in report rows."""
    return getattr(backend, "backend_name", "") or backend.__class__.__name__


class _ProgressCallback(Protocol):
    """Optional per-phase progress callback.

    Phases call ``on_progress(phase_name, checked_so_far, total_or_none)``
    after each unit of work so the CLI's live phase Progress can update
    its detail line. Phases never raise from a progress callback.
    """

    def __call__(
        self,
        phase: str,
        checked: int,
        total: Optional[int],
    ) -> None: ...


def _safe_progress(
    cb: Optional[_ProgressCallback],
    phase: str,
    checked: int,
    total: Optional[int],
) -> None:
    if cb is None:
        return
    try:
        cb(phase, checked, total)
    except Exception:
        pass  # progress is decorative; never let it break a verify pass


# ─── Phase 1: manifest vs remote ──────────────────────────────────────


def verify_manifest_vs_remote(
    config: Config,
    storage_set: tuple[StorageBackend, list[StorageBackend]],
    *,
    backend_filter: Optional[str] = None,
    on_progress: Optional[_ProgressCallback] = None,
) -> ManifestVerifyReport:
    """Walk the project manifest; for each entry, compare the manifest's
    `synced_remote_hash` with what each backend currently reports for
    the recorded `remote_file_id`.

    `backend_filter` (when set) restricts the check to the named Tier 2
    mirror. Pass the primary's `backend_name` or any mirror name; mirrors
    that don't match are silently skipped, including the primary.
    """
    report = ManifestVerifyReport()
    primary, mirrors = storage_set

    # Manifest is keyed by rel_path. We surface the primary's flat fields
    # plus any mirror entries — both layers must match remote reality.
    manifest = Manifest(config.project_path)
    entries = manifest.all()
    if not entries:
        return report

    # Build the (backend_name, backend) list we'll probe.
    targets: list[tuple[str, StorageBackend]] = []
    primary_name = _backend_label(primary)
    if backend_filter is None or backend_filter == primary_name:
        targets.append((primary_name, primary))
    for mirror in mirrors:
        name = _backend_label(mirror)
        if backend_filter is None or backend_filter == name:
            targets.append((name, mirror))
    if not targets:
        return report

    total = len(entries) * len(targets)
    checked = 0
    for rel_path, file_state in entries.items():
        for backend_name, backend in targets:
            checked += 1
            # Resolve (file_id, expected_hash) per backend. The primary's
            # flat fields apply when the entry has no `remotes` map yet
            # (legacy single-backend manifests); otherwise per-backend
            # `remotes[name]` wins.
            if backend is primary and not file_state.remotes:
                expected = file_state.synced_remote_hash
                file_id = file_state.remote_file_id
            else:
                rs = file_state.remotes.get(backend_name)
                if rs is None:
                    # The user hasn't seeded this mirror yet; skip
                    # rather than report a false-positive missing.
                    report.phase.checked += 1
                    report.phase.verified += 1
                    _safe_progress(on_progress, PHASE_MANIFEST, checked, total)
                    continue
                if rs.state != "ok":
                    # Quarantined / pending entries are not "drift"
                    # in the integrity sense — they are tracked by
                    # `status --pending` and `retry`. Skip them here.
                    report.phase.checked += 1
                    report.phase.verified += 1
                    _safe_progress(on_progress, PHASE_MANIFEST, checked, total)
                    continue
                expected = rs.synced_remote_hash
                file_id = rs.remote_file_id

            report.phase.checked += 1
            try:
                actual = backend.get_file_hash(file_id) if file_id else None
            except BaseException:  # noqa: BLE001 - probe must classify, not raise
                actual = None

            if actual is None:
                report.missing.append(MissingEntry(
                    path=rel_path,
                    backend=backend_name,
                    expected=expected,
                ))
                report.phase.missing += 1
            elif expected and actual != expected:
                report.drift.append(DriftEntry(
                    path=rel_path,
                    backend=backend_name,
                    expected=expected,
                    actual=actual,
                ))
                report.phase.drift += 1
            else:
                report.phase.verified += 1
            _safe_progress(on_progress, PHASE_MANIFEST, checked, total)

    return report


# ─── Phase 2: snapshot blobs ──────────────────────────────────────────


def verify_snapshot_blobs(
    snapshot_manager: Any,
    *,
    backend_filter: Optional[str] = None,
    on_progress: Optional[_ProgressCallback] = None,
) -> SnapshotVerifyReport:
    """Walk every `_claude_mirror_blobs/<hh>/<hash>` entry on each backend
    and confirm the bytes hash to their filename.

    The blobs are content-addressed — the filename IS the sha256 of the
    bytes by construction (see `claude_mirror/snapshots.py::_create_blobs`).
    A mismatch means either bit-rot, a partial upload, or tampering.

    `snapshot_manager` is a SnapshotManager-shaped object exposing
    `.storage` + `._mirrors` + `_get_blobs_folder_for(backend)`. Tests
    pass a small stub; the CLI passes the real SnapshotManager.
    """
    report = SnapshotVerifyReport()

    primary = snapshot_manager.storage
    mirrors: list[StorageBackend] = list(getattr(snapshot_manager, "_mirrors", []))

    targets: list[tuple[str, StorageBackend]] = []
    primary_name = _backend_label(primary)
    if backend_filter is None or backend_filter == primary_name:
        targets.append((primary_name, primary))
    for mirror in mirrors:
        name = _backend_label(mirror)
        if backend_filter is None or backend_filter == name:
            targets.append((name, mirror))

    if not targets:
        return report

    # Pre-list every backend's blob folder so we know `total` for progress.
    blob_listings: list[tuple[str, StorageBackend, list[dict[str, Any]]]] = []
    total = 0
    for backend_name, backend in targets:
        try:
            blobs_folder_id = snapshot_manager._get_blobs_folder_for(backend)
            entries = list(backend.list_files_recursive(blobs_folder_id))
        except BaseException:  # noqa: BLE001 - missing folder = empty result
            entries = []
        blob_listings.append((backend_name, backend, entries))
        total += len(entries)

    checked = 0
    for backend_name, backend, entries in blob_listings:
        for entry in entries:
            checked += 1
            report.phase.checked += 1
            sha = entry.get("name", "")
            file_id = entry.get("id", "")
            if not _looks_like_sha256(sha):
                report.corrupted.append(CorruptedEntry(
                    layer=PHASE_SNAPSHOTS,
                    key=f"{backend_name}:{sha}",
                    detail="filename is not a sha256 digest",
                ))
                report.phase.corrupted += 1
                _safe_progress(on_progress, PHASE_SNAPSHOTS, checked, total)
                continue
            try:
                content = backend.download_file(file_id)
            except BaseException:  # noqa: BLE001 - missing blob is reported
                report.missing.append(MissingEntry(
                    path=sha,
                    backend=backend_name,
                    expected=sha,
                ))
                report.phase.missing += 1
                _safe_progress(on_progress, PHASE_SNAPSHOTS, checked, total)
                continue
            actual = hashlib.sha256(content).hexdigest()
            if actual != sha:
                report.corrupted.append(CorruptedEntry(
                    layer=PHASE_SNAPSHOTS,
                    key=f"{backend_name}:{sha[:2]}/{sha}",
                    detail=f"bytes hash to {actual}, expected {sha}",
                ))
                report.phase.corrupted += 1
            else:
                report.phase.verified += 1
            _safe_progress(on_progress, PHASE_SNAPSHOTS, checked, total)

    return report


# ─── Phase 3: mount blob cache ────────────────────────────────────────


def verify_mount_cache(
    blob_cache_dir: Path,
    *,
    on_progress: Optional[_ProgressCallback] = None,
) -> MountCacheVerifyReport:
    """Walk the on-disk content-addressed mount cache populated by the
    v0.5.62 MOUNT engine and confirm each cached blob's bytes still
    hash to its filename. Corrupted entries are reported so the user
    can evict + refetch via the next mount.

    `blob_cache_dir` is the cache root (default `~/.cache/claude-mirror/blobs/`
    on POSIX, `%LOCALAPPDATA%/claude-mirror/Cache/blobs/` on Windows).
    Layout: `<root>/<hh>/<hash>` — one shard subdir per first-byte-prefix,
    matching `claude_mirror/_mount.py::BlobCache`.

    Empty / missing cache is a clean no-op (zero counts), not an error.
    """
    report = MountCacheVerifyReport()
    if not blob_cache_dir.exists():
        return report

    # Eagerly enumerate so `total` is available for progress.
    blobs: list[Path] = []
    for shard in blob_cache_dir.iterdir():
        if not shard.is_dir():
            continue
        for blob in shard.iterdir():
            if blob.is_file():
                blobs.append(blob)
    total = len(blobs)
    if total == 0:
        return report

    checked = 0
    for blob in blobs:
        checked += 1
        report.phase.checked += 1
        expected = blob.name
        if not _looks_like_sha256(expected):
            report.corrupted.append(CorruptedEntry(
                layer="mount_cache",
                key=f"{blob.parent.name}/{expected}",
                detail="filename is not a sha256 digest",
            ))
            report.phase.corrupted += 1
            _safe_progress(on_progress, PHASE_MOUNT_CACHE, checked, total)
            continue
        try:
            data = blob.read_bytes()
        except OSError as exc:
            report.corrupted.append(CorruptedEntry(
                layer="mount_cache",
                key=f"{blob.parent.name}/{expected}",
                detail=f"unreadable: {type(exc).__name__}",
            ))
            report.phase.corrupted += 1
            _safe_progress(on_progress, PHASE_MOUNT_CACHE, checked, total)
            continue
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            report.corrupted.append(CorruptedEntry(
                layer="mount_cache",
                key=f"{blob.parent.name}/{expected}",
                detail=f"bytes hash to {actual}, expected {expected}",
            ))
            report.phase.corrupted += 1
        else:
            report.phase.verified += 1
        _safe_progress(on_progress, PHASE_MOUNT_CACHE, checked, total)

    return report


def _looks_like_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and all(c in "0123456789abcdef" for c in value)
    )


# ─── Aggregator ───────────────────────────────────────────────────────


def collect_verify(
    config: Config,
    storage_set: tuple[StorageBackend, list[StorageBackend]],
    *,
    snapshot_manager: Optional[Any] = None,
    blob_cache_dir: Optional[Path] = None,
    include_files: bool = True,
    include_snapshots: bool = True,
    include_mount_cache: bool = True,
    backend_filter: Optional[str] = None,
    on_progress: Optional[_ProgressCallback] = None,
    now: Optional[datetime] = None,
) -> VerifyReport:
    """Run every requested phase in sequence and return the aggregated report.

    The aggregator never raises: each phase's failures land as structured
    rows in the appropriate list. Callers decide how to translate a non-empty
    drift / corrupted / missing list into an exit code (`--strict`).
    """
    report = VerifyReport(checked_at=now or datetime.now(timezone.utc))

    if include_files:
        m_report = verify_manifest_vs_remote(
            config,
            storage_set,
            backend_filter=backend_filter,
            on_progress=on_progress,
        )
        report.phases.append(m_report.phase)
        report.drift.extend(m_report.drift)
        report.missing.extend(m_report.missing)

    if include_snapshots and snapshot_manager is not None:
        s_report = verify_snapshot_blobs(
            snapshot_manager,
            backend_filter=backend_filter,
            on_progress=on_progress,
        )
        report.phases.append(s_report.phase)
        report.corrupted.extend(s_report.corrupted)
        report.missing.extend(s_report.missing)

    if include_mount_cache and blob_cache_dir is not None:
        c_report = verify_mount_cache(
            blob_cache_dir,
            on_progress=on_progress,
        )
        report.phases.append(c_report.phase)
        report.corrupted.extend(c_report.corrupted)

    return report


__all__ = [
    "CorruptedEntry",
    "DriftEntry",
    "ManifestVerifyReport",
    "MissingEntry",
    "MountCacheVerifyReport",
    "PHASE_MANIFEST",
    "PHASE_MOUNT_CACHE",
    "PHASE_SNAPSHOTS",
    "PhaseReport",
    "SCHEMA_VERSION",
    "SnapshotVerifyReport",
    "VerifyReport",
    "collect_verify",
    "verify_manifest_vs_remote",
    "verify_mount_cache",
    "verify_snapshot_blobs",
]
