from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


MANIFEST_FILE = ".claude_mirror_manifest.json"


# Per-backend file state inside FileState.remotes — each entry tracks
# claude-mirror's view of how the file lives on one specific backend.
#
# state field values:
#   "ok"            — file present on remote at the recorded synced_remote_hash
#   "pending_retry" — last push to this backend hit a transient failure;
#                     retry on next push (Tier 2 multi-backend)
#   "failed_perm"   — last push hit a permanent failure (auth, quota, perm).
#                     Backend is quarantined for this file until the user
#                     resolves the underlying issue.
#   "absent"        — file existed on this backend in the past but the last
#                     sync confirmed it's gone (deleted-on-remote case).
@dataclass
class RemoteState:
    remote_file_id: str = ""           # backend-specific file ID or path
    synced_remote_hash: str = ""       # remote-side hash at last successful sync
    state: str = "ok"                  # see comment above
    last_error: str = ""               # short string describing latest failure
    last_attempt: str = ""             # ISO timestamp of latest attempt
    intended_hash: str = ""            # hash we tried to push (for pending_retry)
    attempts: int = 0                  # cumulative push attempts on the current intended_hash


# Cap on how many pending_retry entries the orchestrator will pull off
# the queue per push. Without this, a long mirror outage misclassified as
# transient could grow the queue unboundedly and every push would read
# the entire manifest synchronously. The user can run `claude-mirror retry`
# explicitly to drain a backlog.
PENDING_RETRY_QUEUE_CAP = 200

# After this many cumulative attempts on the same intended_hash for one
# (file × backend) pair, flip the state from `pending_retry` to
# `failed_perm` so it stops auto-retrying. The user has to inspect it
# (via `status --pending`) and either clear it or fix the underlying
# issue. Prevents indefinite retry loops on backends that keep returning
# transient-looking errors that are actually permanent.
PENDING_MAX_ATTEMPTS = 10


@dataclass
class FileState:
    """Per-file state in the manifest.

    The manifest schema has evolved through three layers, all readable
    from disk for back-compat:

      v1 (legacy single-backend)
          Each entry had `drive_file_id` and `synced_hash`. Loaded by
          mapping `drive_file_id` -> `remote_file_id`, leaving `remotes`
          empty and `synced_remote_hash` defaulted to `synced_hash`.

      v2 (multi-backend abstraction, post-universal_abstraction merge)
          `remote_file_id` + `synced_remote_hash` flat fields; still one
          backend per project. The single backend's name is implicit
          from the project's config.

      v3 (Tier 2 multi-backend, this version)
          Adds `remotes: dict[backend_name, RemoteState]` so a single
          file can live on multiple backends simultaneously, each with
          its own file ID, sync state, and per-backend retry/quarantine
          status. The flat `remote_file_id` + `synced_remote_hash`
          fields are kept as the "primary backend's view" for
          back-compat with code paths that haven't been multi-backend-
          aware'd yet.

    On save, we always write the v3 shape (flat fields populated from
    primary, plus the full `remotes` map). On load, all three shapes are
    accepted; older formats are silently upgraded in memory and rewritten
    on next save.
    """

    synced_hash: str = ""              # local hash at last successful sync (any backend)
    remote_file_id: str = ""           # primary backend's file ID (back-compat surface)
    synced_at: str = ""                # ISO timestamp of last successful sync (any backend)
    synced_remote_hash: str = ""       # primary backend's hash (back-compat surface)
    remotes: dict[str, RemoteState] = field(default_factory=dict)  # per-backend states (v3)

    def get_remote(self, backend_name: str) -> Optional[RemoteState]:
        """Return the per-backend state for the given backend, or None
        if this file has never been seen on that backend."""
        return self.remotes.get(backend_name)

    def set_remote(self, backend_name: str, remote: RemoteState) -> None:
        self.remotes[backend_name] = remote


class Manifest:
    def __init__(self, project_path: str, manifest_filename: Optional[str] = None) -> None:
        """Open the manifest at <project_path>/<manifest_filename>.

        manifest_filename: optional override (e.g. for multi-config setups
        where two configs point at the same project_path and need
        independent manifests). Defaults to `.claude_mirror_manifest.json`.
        """
        self.project_path = Path(project_path)
        self._path = self.project_path / (manifest_filename or MANIFEST_FILE)
        self._data: dict[str, FileState] = {}
        self._lock = threading.Lock()
        self.load()

    def load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted or unreadable — back it up and start clean rather than crashing.
            try:
                backup = self._path.with_suffix(self._path.suffix + ".corrupt")
                self._path.replace(backup)
            except OSError:
                pass
            self._data = {}
            return
        # Reject manifest keys that would escape the project root or
        # name absolute paths. A malicious or corrupted manifest could
        # otherwise turn `retry_mirrors` / `_push_file` / `_resolve_conflict`
        # into a read-arbitrary-file primitive (each composes
        # `self._project / rel_path` and reads the result), letting an
        # attacker who can write to `.claude_mirror_manifest.json` exfiltrate
        # files like `~/.ssh/id_rsa` to a configured mirror.
        accepted: dict[str, FileState] = {}
        for k, v in raw.items():
            if not self._is_safe_relpath(k):
                # Skip silently rather than crashing — an old manifest
                # rewritten by a buggy version may have stray entries.
                continue
            accepted[k] = self._load_entry(v)
        self._data = accepted

    @staticmethod
    def _is_safe_relpath(rel_path: str) -> bool:
        """Reject manifest keys that look like path-traversal attempts.
        A safe key is a non-empty relative path with no `..` segments,
        no leading `/`, and no NUL bytes."""
        if not rel_path or not isinstance(rel_path, str):
            return False
        if "\x00" in rel_path:
            return False
        if rel_path.startswith("/") or rel_path.startswith("\\"):
            return False
        # Normalise once and reject if any component is ".."
        try:
            parts = Path(rel_path).parts
        except ValueError:
            # Path() can raise ValueError on embedded NUL on some platforms
            # (already filtered above, but keep a defensive narrow catch).
            # Programming bugs (TypeError, AttributeError) propagate.
            return False
        for part in parts:
            if part in ("..", "\\..", "/.."):
                return False
        # Rough additional defence: a resolved relative path that climbs
        # out of '.' will produce a leading '..' part, caught above.
        return True

    @staticmethod
    def _load_entry(v: dict) -> FileState:
        """Parse one manifest entry, accepting v1 / v2 / v3 shapes."""
        synced_hash = v.get("synced_hash", "")
        # v1: drive_file_id; v2/v3: remote_file_id
        remote_file_id = v.get("remote_file_id", v.get("drive_file_id", ""))
        synced_at = v.get("synced_at", "")
        # v1: only synced_hash existed; v2/v3 split out synced_remote_hash
        synced_remote_hash = v.get("synced_remote_hash", synced_hash)

        # v3 multi-backend remotes map. Older formats have no `remotes`
        # field; in that case the orchestrator will populate it on first
        # successful per-backend push.
        remotes_raw = v.get("remotes") or {}
        remotes: dict[str, RemoteState] = {}
        for backend_name, r in remotes_raw.items():
            if not isinstance(r, dict):
                continue
            remotes[backend_name] = RemoteState(
                remote_file_id=r.get("remote_file_id", ""),
                synced_remote_hash=r.get("synced_remote_hash", ""),
                state=r.get("state", "ok"),
                last_error=r.get("last_error", ""),
                last_attempt=r.get("last_attempt", ""),
                intended_hash=r.get("intended_hash", ""),
                attempts=int(r.get("attempts", 0) or 0),
            )

        return FileState(
            synced_hash=synced_hash,
            remote_file_id=remote_file_id,
            synced_at=synced_at,
            synced_remote_hash=synced_remote_hash,
            remotes=remotes,
        )

    def save(self) -> None:
        # Hold the lock around the ENTIRE write+replace sequence: two
        # concurrent save() calls share the same `<file>.json.tmp` path,
        # so without serialisation one os.replace can move a partially-
        # written file. Serialising under self._lock makes the tmp path
        # safe to reuse and keeps the on-disk manifest consistent.
        with self._lock:
            raw = {k: self._dump_entry(v) for k, v in self._data.items()}
            # Atomic write: tmp + os.replace so a crash mid-write can't corrupt the manifest.
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(raw, indent=2))
            os.replace(tmp, self._path)

    @staticmethod
    def _dump_entry(s: FileState) -> dict:
        """Serialise one entry in v3 shape (flat back-compat fields +
        per-backend `remotes` map). Empty `remotes` is omitted to keep
        single-backend manifests visually identical to v2."""
        out = {
            "synced_hash": s.synced_hash,
            "remote_file_id": s.remote_file_id,
            "synced_at": s.synced_at,
            "synced_remote_hash": s.synced_remote_hash,
        }
        if s.remotes:
            out["remotes"] = {
                name: {
                    "remote_file_id": r.remote_file_id,
                    "synced_remote_hash": r.synced_remote_hash,
                    "state": r.state,
                    "last_error": r.last_error,
                    "last_attempt": r.last_attempt,
                    "intended_hash": r.intended_hash,
                    "attempts": r.attempts,
                }
                for name, r in s.remotes.items()
            }
        return out

    def get(self, rel_path: str) -> Optional[FileState]:
        with self._lock:
            return self._data.get(rel_path)

    def update(
        self, rel_path: str, synced_hash: str, remote_file_id: str,
        synced_remote_hash: str = "",
        backend_name: Optional[str] = None,
    ) -> None:
        """Record a successful sync for one file.

        For single-backend (legacy) callers: pass `synced_hash` +
        `remote_file_id` + `synced_remote_hash`. The flat fields are
        updated and, if `backend_name` is provided, also the corresponding
        per-backend RemoteState entry.

        For multi-backend callers: prefer `update_remote(rel_path,
        backend_name, ...)` to update one backend's state without
        touching the others.
        """
        with self._lock:
            existing = self._data.get(rel_path)
            remotes = dict(existing.remotes) if existing else {}
            now = datetime.now(timezone.utc).isoformat()
            if backend_name:
                remotes[backend_name] = RemoteState(
                    remote_file_id=remote_file_id,
                    synced_remote_hash=synced_remote_hash or synced_hash,
                    state="ok",
                    last_attempt=now,
                )
            self._data[rel_path] = FileState(
                synced_hash=synced_hash,
                remote_file_id=remote_file_id,
                synced_at=now,
                synced_remote_hash=synced_remote_hash or synced_hash,
                remotes=remotes,
            )

    def update_remote(
        self,
        rel_path: str,
        backend_name: str,
        remote_file_id: str = "",
        synced_remote_hash: str = "",
        state: str = "ok",
        last_error: str = "",
        intended_hash: str = "",
    ) -> None:
        """Update one backend's per-file state without disturbing others.

        Use this from the multi-backend orchestrator when a single
        backend's push succeeds, fails transiently, or fails permanently
        — the other backends' states stay intact.

        IMPORTANT: This method NEVER writes the FLAT fields
        (`remote_file_id`, `synced_remote_hash`, `synced_hash`,
        `synced_at`). Flat fields represent the PRIMARY backend's view
        and are written exclusively by `Manifest.update()` (called from
        `_push_file` for the primary upload). A previous version of this
        method wrote flat fields when `state == "ok"`, which corrupted
        the primary's state whenever a mirror's push completed —
        because backends use different hash formats (Drive=md5,
        Dropbox=content_hash, OneDrive=quickXorHash, WebDAV=ETag), the
        flat `synced_remote_hash` would end up holding the last-completed
        mirror's hash, and the next status pass would falsely report
        every file as `drive_ahead`.
        """
        with self._lock:
            existing = self._data.get(rel_path) or FileState()
            remotes = dict(existing.remotes)
            now = datetime.now(timezone.utc).isoformat()
            # Preserve the existing per-backend remote_file_id when the
            # caller didn't supply a new one (e.g. transient/permanent
            # failure paths only update state, not the file ID).
            existing_rs = existing.remotes.get(backend_name)
            existing_id = existing_rs.remote_file_id if existing_rs else ""
            existing_attempts = existing_rs.attempts if existing_rs else 0
            existing_intended = existing_rs.intended_hash if existing_rs else ""

            # Attempt-counter logic: bumps on every non-ok update for the
            # same intended_hash. Resets to 0 on success or when the user
            # changes the local file (intended_hash differs).
            if state == "ok":
                attempts = 0
            else:
                if intended_hash and intended_hash != existing_intended:
                    # Local file changed — fresh attempt budget.
                    attempts = 1
                else:
                    attempts = existing_attempts + 1

            # Auto-flip pending_retry → failed_perm after too many
            # cumulative attempts on the same intended_hash. This stops
            # backends that keep returning transient-looking errors that
            # are actually permanent (e.g. a quota issue mis-classified
            # as 5xx) from looping forever.
            effective_state = state
            if state == "pending_retry" and attempts >= PENDING_MAX_ATTEMPTS:
                effective_state = "failed_perm"

            remotes[backend_name] = RemoteState(
                remote_file_id=remote_file_id or existing_id,
                synced_remote_hash=synced_remote_hash,
                state=effective_state,
                last_error=last_error,
                last_attempt=now,
                intended_hash=intended_hash,
                attempts=attempts,
            )
            # Flat fields are PRIMARY-only — preserve them verbatim.
            self._data[rel_path] = FileState(
                synced_hash=existing.synced_hash,
                remote_file_id=existing.remote_file_id,
                synced_at=existing.synced_at,
                synced_remote_hash=existing.synced_remote_hash,
                remotes=remotes,
            )

    def remove(self, rel_path: str) -> None:
        with self._lock:
            self._data.pop(rel_path, None)

    def all(self) -> dict[str, FileState]:
        with self._lock:
            return dict(self._data)

    def pending_for_backend(self, backend_name: str) -> dict[str, RemoteState]:
        """Return rel_path -> RemoteState for every file whose state on the
        given backend is `pending_retry`. Used by the multi-backend
        orchestrator at the start of every push to retry failures from
        previous runs."""
        with self._lock:
            return {
                p: s.remotes[backend_name]
                for p, s in self._data.items()
                if backend_name in s.remotes
                and s.remotes[backend_name].state == "pending_retry"
            }

    def unseeded_for_backend(self, backend_name: str) -> dict[str, FileState]:
        """Return rel_path -> FileState for every file that has NO recorded
        state at all for the named backend.

        This is the case when a mirror is added to a project after files
        already exist on the primary: the manifest carries each file's
        sync state for the primary but has nothing for the new mirror,
        so push has nothing to do (local hash already matches manifest)
        and the mirror folder stays empty. `claude-mirror seed-mirror
        --backend NAME` walks this set and uploads each file to the
        named mirror once, recording state="ok" so subsequent pushes
        track it normally.
        """
        with self._lock:
            return {
                p: s
                for p, s in self._data.items()
                if backend_name not in s.remotes
            }

    def prune_unknown_backends(self, active_backend_names: set[str]) -> int:
        """Drop entries from each FileState's `remotes` map whose
        backend_name is not in `active_backend_names` (i.e. mirrors the
        user has removed from `mirror_config_paths`). Returns the count
        of pruned per-backend entries.

        Without this, a removed mirror's `pending_retry` entries become
        orphan: invisible to status / push (no active backend asks about
        them) yet they keep growing the manifest forever. Caller is
        expected to re-save the manifest at the next normal write — we
        deliberately don't auto-save here so a dry-run-style `_load_engine`
        doesn't write to disk just to clean state.
        """
        pruned = 0
        with self._lock:
            for state in self._data.values():
                stale = [
                    name for name in state.remotes
                    if name not in active_backend_names
                ]
                for name in stale:
                    del state.remotes[name]
                    pruned += 1
        return pruned

    def quarantined_backends(self) -> dict[str, list[str]]:
        """Return backend_name -> list of paths currently marked
        `failed_perm` on that backend. Used to surface user-action-required
        notifications after a push completes."""
        out: dict[str, list[str]] = {}
        with self._lock:
            for path, state in self._data.items():
                for backend_name, r in state.remotes.items():
                    if r.state == "failed_perm":
                        out.setdefault(backend_name, []).append(path)
        return out

    @staticmethod
    def hash_file(path: str) -> str:
        # hashlib.file_digest (Python 3.11+) uses a C-level hot-path that
        # reads + digests with no Python-level chunk loop, releases the GIL
        # during reads on POSIX, and is meaningfully faster than the manual
        # 1 MiB chunked loop we previously used.
        with open(path, "rb") as f:
            return hashlib.file_digest(f, "md5").hexdigest()

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.md5(data).hexdigest()
