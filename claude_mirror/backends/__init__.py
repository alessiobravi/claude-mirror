from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional


class ErrorClass(enum.Enum):
    """Categorical classification of backend errors for retry / notification.

    The point: not all failures are equal. Tier 2 multi-backend pushes use
    this to decide whether to retry in-process, queue for next-push retry,
    surface as user-action-required, or skip just one file.

    TRANSIENT      — network blip, 5xx, rate-limit, brief Pub/Sub stream
                     reconnect. Worth in-process retry with backoff; if
                     still failing, queue for next-push retry. The backend
                     itself is healthy; the specific call timed out.

    AUTH           — refresh token revoked, OAuth scope rescinded, app
                     access removed by user/admin. Retrying is pointless;
                     user must run `claude-mirror auth --backend X`.

    QUOTA          — storage full, daily API quota exhausted, rate-cap
                     beyond what backoff fixes. User must free space or
                     wait out the quota window. No retry until the user
                     resolves it (e.g. via `claude-mirror forget` + `gc`).

    PERMISSION     — folder access revoked, share link broken, the project
                     was moved out of reach. User-action-required;
                     retrying without intervention won't succeed.

    FILE_REJECTED  — file too large, invalid path, illegal name, server
                     refused this specific file. Skip the one file; the
                     rest of the push proceeds normally.

    UNKNOWN        — unclassified. Treated like TRANSIENT (retry once)
                     but with a louder warning so the user can report it.
    """

    TRANSIENT = "transient"
    AUTH = "auth"
    QUOTA = "quota"
    PERMISSION = "permission"
    FILE_REJECTED = "file_rejected"
    UNKNOWN = "unknown"

    @property
    def is_retryable(self) -> bool:
        """True if a retry might succeed without user intervention."""
        return self in (ErrorClass.TRANSIENT, ErrorClass.UNKNOWN)

    @property
    def needs_user_action(self) -> bool:
        """True if a human must do something before this backend can be
        used again — credentials renewed, space freed, permission restored."""
        return self in (ErrorClass.AUTH, ErrorClass.QUOTA, ErrorClass.PERMISSION)


class BackendError(Exception):
    """Wraps a raw backend exception with a classification and original cause.

    Backends should raise this from their upload/download/delete/list paths
    when they want the multi-backend orchestrator to make routing decisions.
    The orchestrator inspects `error_class` to decide retry / quarantine /
    user-notification behaviour.

    SECURITY NOTE — we DO NOT keep the live `cause` exception instance.
    Holding it would retain `__traceback__` (and through it the locals of
    every frame: response bodies, session objects, OAuth credential tuples,
    raw token bytes) for the lifetime of the BackendError, which in a
    long-running watcher accumulates per quarantined manifest entry. We
    drop the traceback immediately and store only `repr(cause)[:200]` for
    diagnostics. Use `_cause_repr` for logging; never `raise ... from
    self.cause` — call sites should `raise ... from None`.
    """

    def __init__(
        self,
        error_class: ErrorClass,
        message: str,
        backend_name: str = "",
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.backend_name = backend_name
        if cause is not None:
            try:
                cause.__traceback__ = None  # drop frames + locals
            except Exception:
                pass
            # Keep a stringified copy for diagnostics; do not retain the live
            # exception instance (it might pin response bodies, sessions,
            # or credential tuples in memory for the lifetime of the
            # quarantined manifest entry).
            self._cause_repr = repr(cause)[:200]
            self.cause = None
        else:
            self._cause_repr = ""
            self.cause = None

    def __str__(self) -> str:
        prefix = f"[{self.backend_name}] " if self.backend_name else ""
        return f"{prefix}{self.error_class.value}: {super().__str__()}"


def redact_error(msg: str, *, max_chars: int = 160) -> str:
    """Strip credentials and PII from an exception message before it
    lands in the manifest, gets posted to Slack, or shows in `status
    --pending` output.

    Removes:
      * Bearer / OAuth tokens (`Bearer ABC123...`)
      * basic-auth in URLs (`https://user:pass@host/...`)
      * `?access_token=...`, `?key=...`, `?token=...` query strings
      * absolute home paths (`/Users/<name>/...` or `/home/<name>/...` → `$HOME/...`)
      * absolute paths starting with `/var/` `/private/` (macOS) and `/home/`
      * file paths with embedded NUL bytes (artefact of weird inputs)

    Final output is capped to `max_chars` so a single buggy backend
    doesn't dump a megabyte of stack into the user's manifest.
    """
    import os
    import re

    if not msg:
        return ""
    text = str(msg)
    # Bearer / token prefixes
    text = re.sub(
        r"(Bearer|Token)\s+[A-Za-z0-9._\-]+",
        r"\1 [redacted]",
        text,
        flags=re.IGNORECASE,
    )
    # basic-auth in URL: scheme://user:pass@host
    text = re.sub(
        r"(\b[a-z][a-z0-9+.\-]*://)[^/@\s]+:[^/@\s]+@",
        r"\1[redacted]@",
        text,
    )
    # ?access_token=, ?token=, ?key=, ?api_key= query params
    text = re.sub(
        r"([?&](?:access_token|token|api_key|key|secret|client_secret)=)[^&\s]+",
        r"\1[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    # Absolute home directory paths → /$HOME
    home = os.path.expanduser("~")
    if home and home != "/":
        text = text.replace(home, "/$HOME")
    # NUL bytes — artefact of weird inputs that confuse JSON serialisers
    text = text.replace("\x00", "")
    # Cap.
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


class StorageBackend(ABC):
    """Abstract interface for cloud/remote storage backends."""

    # Subclasses MUST set this to a stable identifier ("googledrive",
    # "dropbox", "onedrive", "webdav") so the orchestrator can name them
    # in logs / Slack / per-backend manifest entries.
    backend_name: str = ""

    # Maximum bytes any single download_file call may return. Enforced
    # by each backend in its download_file implementation. A compromised
    # remote (or bug returning a chunked-encoding body that lies about
    # size) could otherwise OOM the client by streaming arbitrary GB.
    # 1 GiB is well above the largest real-world claude-mirror content
    # (markdown, JSON state) and well below typical desktop RAM, so it
    # acts as a safety floor for runaway downloads. Backends MUST
    # check Content-Length pre-flight where the protocol exposes it,
    # and additionally short-circuit accumulation past the cap.
    MAX_DOWNLOAD_BYTES: int = 1 * 1024 * 1024 * 1024  # 1 GiB

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw backend exception to an ErrorClass. Default returns
        UNKNOWN — backends should override with their own classification.

        Called by the orchestrator after catching an exception from any
        backend method. Must not raise; an exception here would be a bug
        in the classifier itself.
        """
        return ErrorClass.UNKNOWN

    @abstractmethod
    def authenticate(self) -> Any:
        """Run interactive authentication flow. Returns credentials object."""

    @abstractmethod
    def get_credentials(self) -> Any:
        """Load existing credentials. Raises RuntimeError if not authenticated."""

    @abstractmethod
    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Get or create a folder by name under parent. Returns folder ID."""

    @abstractmethod
    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Resolve a relative path to (parent_folder_id, filename), creating intermediate folders."""

    @abstractmethod
    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> list[dict]:
        """List all non-folder files recursively. Each dict must have: id, name, md5Checksum, relative_path.

        If `progress_cb` is provided, backends should call it periodically with
        `(folders_explored, files_seen_so_far)` so the caller can render live progress.
        Backends may call it synchronously from worker threads.

        If `exclude_folder_names` is provided, backends MUST NOT descend into
        any subfolder whose name (the last path component) is in the set.
        This is used to prune the recursion at the source — e.g. to skip
        `_claude_mirror_snapshots/` (which contains a full copy of the project
        per snapshot) and `_claude_mirror_logs/` rather than walk them and
        filter afterwards. Backends that issue a single server-side recursive
        listing (Dropbox, OneDrive, WebDAV) may filter the result instead."""

    @abstractmethod
    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        """List subfolders of parent. If name given, filter by exact name. Each dict: id, name, createdTime."""

    @abstractmethod
    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        """Upload a local file. If file_id given, update existing. Returns file ID."""

    @abstractmethod
    def download_file(self, file_id: str) -> bytes:
        """Download file content by ID."""

    @abstractmethod
    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        """Upload raw bytes as a file. If file_id given, update existing. Returns file ID."""

    @abstractmethod
    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Find a file by name in a folder. Returns file ID or None."""

    @abstractmethod
    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Server-side copy of a file. Returns new file ID."""

    @abstractmethod
    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Get the hash/checksum of a remote file without downloading."""

    @abstractmethod
    def delete_file(self, file_id: str) -> None:
        """Delete a file by ID."""
