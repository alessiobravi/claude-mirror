from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional, cast

try:
    import dropbox
    from dropbox.exceptions import ApiError, AuthError, HttpError, InternalServerError, RateLimitError
    from dropbox.files import (
        WriteMode,
        FolderMetadata,
        FileMetadata,
        CreateFolderError,
    )
except ImportError:
    raise ImportError(
        "Dropbox SDK is required for the Dropbox backend.\n"
        "Install it with:  pipx install -e '.[dropbox]' --force"
    )

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure


class DropboxBackend(StorageBackend):
    """StorageBackend implementation for Dropbox."""

    backend_name = "dropbox"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._dbx: Optional[dropbox.Dropbox] = None

    # ------------------------------------------------------------------
    # Error classification & retry
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw Dropbox SDK / HTTP / network exception to an ErrorClass.

        Defensive: any introspection failure falls through to UNKNOWN rather
        than raising — a classifier that itself raises would mask the real
        failure from the orchestrator.
        """
        try:
            # Auth-level failure: token revoked, invalid, expired.
            if isinstance(exc, AuthError):
                return ErrorClass.AUTH

            # Rate limiting — server-wide throttle. Routed through the
            # shared backoff coordinator so all in-flight uploads pause
            # together rather than each retrying independently and
            # compounding the rate-limit pressure.
            if isinstance(exc, RateLimitError):
                return ErrorClass.RATE_LIMIT_GLOBAL

            # Dropbox 5xx wrapped exception.
            if isinstance(exc, InternalServerError):
                return ErrorClass.TRANSIENT

            # ApiError carries a typed union in `.error` — inspect carefully.
            if isinstance(exc, ApiError):
                # First, the global-throttle path: Dropbox sometimes
                # surfaces rate-limit conditions via ApiError with an
                # `error_summary` containing `too_many_requests` /
                # `too_many_write_operations` rather than the dedicated
                # RateLimitError. Detect those before the typed-union
                # path so they route to the coordinator.
                try:
                    summary = str(getattr(exc, "error_summary", "") or "").lower()
                except Exception:
                    summary = ""
                if (
                    "too_many_requests" in summary
                    or "too_many_write_operations" in summary
                ):
                    return ErrorClass.RATE_LIMIT_GLOBAL
                try:
                    err = getattr(exc, "error", None)
                    if err is not None and hasattr(err, "is_path") and err.is_path():
                        try:
                            path_err = err.get_path()
                        except Exception:
                            path_err = None
                        if path_err is not None:
                            try:
                                if hasattr(path_err, "is_insufficient_space") and path_err.is_insufficient_space():
                                    return ErrorClass.QUOTA
                            except Exception:
                                pass
                            try:
                                if hasattr(path_err, "is_no_write_permission") and path_err.is_no_write_permission():
                                    return ErrorClass.PERMISSION
                            except Exception:
                                pass
                            try:
                                if hasattr(path_err, "is_disallowed_name") and path_err.is_disallowed_name():
                                    return ErrorClass.FILE_REJECTED
                            except Exception:
                                pass
                            try:
                                if hasattr(path_err, "is_too_long") and path_err.is_too_long():
                                    return ErrorClass.FILE_REJECTED
                            except Exception:
                                pass
                except Exception:
                    pass
                return ErrorClass.UNKNOWN

            # Generic Dropbox HttpError — inspect status_code.
            if isinstance(exc, HttpError):
                status = None
                try:
                    status = int(getattr(exc, "status_code", 0) or 0)
                except Exception:
                    status = None
                if status == 401:
                    return ErrorClass.AUTH
                if status == 403:
                    return ErrorClass.PERMISSION
                if status == 404:
                    return ErrorClass.FILE_REJECTED
                if status == 413:
                    return ErrorClass.FILE_REJECTED
                if status == 429:
                    # Account-wide throttle — coordinator pause, not
                    # per-file retry. Distinct from QUOTA (which means
                    # the storage cap is hit and only user action can
                    # resolve it).
                    return ErrorClass.RATE_LIMIT_GLOBAL
                if status is not None and 500 <= status < 600:
                    return ErrorClass.TRANSIENT
                if status is not None and 400 <= status < 500:
                    return ErrorClass.FILE_REJECTED
                return ErrorClass.UNKNOWN

            # Network-level failures. socket.timeout subclasses OSError on
            # 3.10+, so the OSError branch covers it.
            if isinstance(exc, (TimeoutError, ConnectionError)):
                return ErrorClass.TRANSIENT
            if isinstance(exc, OSError):
                return ErrorClass.TRANSIENT

            return ErrorClass.UNKNOWN
        except Exception:
            # The classifier itself must never raise.
            return ErrorClass.UNKNOWN

    def _upload_with_retry(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        """Upload with exponential backoff on TRANSIENT/UNKNOWN errors.

        Wraps `upload_file`. Retries up to `config.max_retry_attempts` times
        (default 3) with backoff 0.8s, 1.6s, 3.2s, ... Non-retryable errors
        are wrapped in BackendError and raised immediately. Exhausted retries
        raise BackendError with the last classification (TRANSIENT or UNKNOWN).
        Never propagates raw causes.
        """
        max_attempts = int(getattr(self.config, "max_retry_attempts", 3))
        if max_attempts < 1:
            max_attempts = 1
        base_delay = 0.8

        last_exc: Optional[BaseException] = None
        last_class: ErrorClass = ErrorClass.TRANSIENT
        for attempt in range(max_attempts):
            try:
                return self.upload_file(local_path, rel_path, root_folder_id, file_id=file_id)
            except Exception as exc:
                err_class = self.classify_error(exc)
                if err_class.is_retryable:
                    last_exc = exc
                    last_class = err_class
                    if attempt + 1 < max_attempts:
                        time.sleep(base_delay * (2 ** attempt))
                        continue
                    # Out of retries.
                    break
                # Non-retryable — raise immediately.
                raise BackendError(
                    err_class,
                    str(exc),
                    backend_name="dropbox",
                    cause=exc,
                ) from exc

        # Exhausted retries on a retryable error.
        raise BackendError(
            last_class,
            f"upload failed after {max_attempts} attempts: {last_exc}",
            backend_name="dropbox",
            cause=last_exc,
        ) from last_exc

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> dropbox.Dropbox:
        """OAuth2 PKCE flow — no client secret needed."""
        flow = dropbox.DropboxOAuth2FlowNoRedirect(
            self.config.dropbox_app_key,
            use_pkce=True,
            token_access_type="offline",
        )
        auth_url = flow.start()
        print(f"\n1. Visit this URL and authorize the app:\n   {auth_url}\n")
        auth_code = input("2. Paste the authorization code here: ").strip()
        result = flow.finish(auth_code)

        token_path = Path(self.config.token_file)
        write_token_secure(token_path, json.dumps({
            "app_key": self.config.dropbox_app_key,
            "refresh_token": result.refresh_token,
        }))

        self._dbx = dropbox.Dropbox(
            app_key=self.config.dropbox_app_key,
            oauth2_refresh_token=result.refresh_token,
        )
        return self._dbx

    def get_credentials(self) -> dropbox.Dropbox:
        """Load saved refresh token and return a Dropbox client."""
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            raise RuntimeError("Not authenticated. Run `claude-mirror auth` first.")
        data = json.loads(token_path.read_text())
        refresh_token = data.get("refresh_token")
        app_key = data.get("app_key", self.config.dropbox_app_key)
        if not refresh_token:
            raise RuntimeError(
                "Token file is missing refresh_token. "
                "Run `claude-mirror auth` again."
            )
        self._dbx = dropbox.Dropbox(
            app_key=app_key,
            oauth2_refresh_token=refresh_token,
        )
        return self._dbx

    @property
    def dbx(self) -> dropbox.Dropbox:
        if not self._dbx:
            self.get_credentials()
        return self._dbx

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _full_path(self, *parts: str) -> str:
        """Join path parts into a Dropbox-style absolute path."""
        joined = "/".join(p.strip("/") for p in parts if p)
        return f"/{joined}" if not joined.startswith("/") else joined

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Create folder if needed. Returns the full Dropbox path as the 'ID'."""
        folder_path = self._full_path(parent_id, name)
        try:
            self.dbx.files_create_folder_v2(folder_path)
        except ApiError as e:
            # Folder already exists — that's fine
            if not (hasattr(e.error, "is_path") and e.error.is_path()
                    and e.error.get_path().is_conflict()):
                raise
        return folder_path

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Return (parent_folder_path, filename), creating intermediate folders."""
        parts = Path(rel_path).parts
        if len(parts) == 1:
            return root_folder_id, parts[0]
        current = root_folder_id
        for part in parts[:-1]:
            current = self.get_or_create_folder(part, current)
        return current, parts[-1]

    # ------------------------------------------------------------------
    # File listing
    # ------------------------------------------------------------------

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        """List all files recursively. Returns dicts with id, name, md5Checksum, relative_path.

        Dropbox's `files/list_folder` returns the full subtree in a single
        recursive call, so we can't prune the server-side traversal — but we
        DO drop any entry whose relative path passes through an excluded
        folder name (e.g. `_claude_mirror_snapshots/`). This avoids returning
        snapshot copies of the project tree to the caller.
        """
        results: list[dict[str, Any]] = []
        excluded = exclude_folder_names or set()
        try:
            response = self.dbx.files_list_folder(folder_id, recursive=True)
        except ApiError:
            return results

        def _is_excluded_path(rel: str) -> bool:
            if not excluded:
                return False
            # Split on "/" and check whether any path component is in the set.
            for component in rel.split("/"):
                if component in excluded:
                    return True
            return False

        files_seen = 0

        def _process(entries: list[Any]) -> None:
            # `files_list_folder(recursive=True)` may return three metadata
            # types: FileMetadata, FolderMetadata, and DeletedMetadata. We
            # only emit live files — folders are implicit in the file paths,
            # and deletions are tracked via the manifest+remote diff in
            # SyncEngine.get_status (a file present in the manifest but
            # absent from this listing is treated as remotely-deleted).
            nonlocal files_seen
            for entry in entries:
                if isinstance(entry, FileMetadata):
                    full = entry.path_display
                    rel = full[len(folder_id):].lstrip("/")
                    if _is_excluded_path(rel):
                        continue
                    results.append({
                        "id": full,
                        "name": entry.name,
                        "md5Checksum": entry.content_hash or "",
                        "relative_path": rel,
                        "mimeType": "",
                    })
                    files_seen += 1
                # FolderMetadata + DeletedMetadata: intentionally skipped.
            if progress_cb:
                progress_cb(0, files_seen)

        _process(response.entries)
        while response.has_more:
            response = self.dbx.files_list_folder_continue(response.cursor)
            _process(response.entries)
        return results

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict[str, Any]]:
        """List subfolders. Returns dicts with id, name, createdTime."""
        results: list[dict[str, Any]] = []
        try:
            response = self.dbx.files_list_folder(parent_id)
        except ApiError:
            return results

        def _process(entries: list[Any]) -> None:
            for entry in entries:
                if isinstance(entry, FolderMetadata):
                    if name and entry.name != name:
                        continue
                    results.append({
                        "id": entry.path_display,
                        "name": entry.name,
                        "createdTime": "",
                    })

        _process(response.entries)
        while response.has_more:
            response = self.dbx.files_list_folder_continue(response.cursor)
            _process(response.entries)
        return results

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Upload a local file. Returns the full Dropbox path as the file 'ID'.

        Resume behaviour: Dropbox's `files_upload_session_*` family of
        APIs supports resuming a multi-part upload session, but the
        session ID does NOT survive process restart per the Dropbox SDK
        contract — a crashed process loses the session and re-uploads
        from scratch. claude-mirror is built around small markdown
        files, so we use the single-call `files_upload` path; if a
        future change moves to chunked upload sessions, the resume
        caveat still applies.

        progress_callback: optional `Callable[[int], None]`. Invoked once
        with the full payload size after the SDK call returns
        successfully — `files_upload` is single-shot, so we have no
        per-chunk granularity here. The callback contract is delta-
        based, but with a single emission a delta equals the total.
        """
        if file_id:
            dest_path = file_id
        else:
            parent, filename = self.resolve_path(rel_path, root_folder_id)
            dest_path = self._full_path(parent, filename)

        bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))
        with open(local_path, "rb") as f:
            body = f.read()
        # Throttle BEFORE the SDK call — the bucket pre-pays for the
        # bytes about to go on the wire. For larger-than-bucket files
        # `consume()` internally paces in capacity-sized waves, so the
        # long-run rate stays honest.
        bucket.consume(len(body))
        self.dbx.files_upload(
            body, dest_path, mode=WriteMode.overwrite,
        )
        if progress_callback is not None and body:
            progress_callback(len(body))
        return dest_path

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bytes:
        """Download file by Dropbox path. Enforces MAX_DOWNLOAD_BYTES so
        a compromised remote or runaway response can't OOM the client.

        progress_callback: optional `Callable[[int], None]`. Invoked with
        delta bytes per `iter_content` chunk so callers see a live
        download bar even on Dropbox (which doesn't expose a per-chunk
        progress hook on the upload side).
        """
        meta, response = self.dbx.files_download(file_id)
        # Pre-flight: Dropbox metadata exposes content size.
        size = getattr(meta, "size", None)
        if size is not None and size > self.MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"Refusing Dropbox download of {file_id!r}: size {size} "
                f"exceeds MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES})."
            )
        if progress_callback is None:
            content: bytes = response.content
            # Belt-and-braces: if metadata lied, abort here.
            if len(content) > self.MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"Dropbox download of {file_id!r} returned "
                    f"{len(content)} bytes — exceeds MAX_DOWNLOAD_BYTES."
                )
            return content

        # Streaming path with per-chunk progress emission.
        buf = bytearray()
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > self.MAX_DOWNLOAD_BYTES:
                raise RuntimeError(
                    f"Dropbox download of {file_id!r} streamed past "
                    f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting."
                )
            progress_callback(len(chunk))
        return bytes(buf)

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        dest_path = file_id if file_id else self._full_path(folder_id, name)
        self.dbx.files_upload(content, dest_path, mode=WriteMode.overwrite)
        return dest_path

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Check if a file exists by path. Returns path or None."""
        file_path = self._full_path(folder_id, name)
        try:
            meta = self.dbx.files_get_metadata(file_path)
            return cast(Optional[str], meta.path_display)
        except ApiError:
            return None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Server-side copy."""
        dest_path = self._full_path(dest_folder_id, name)
        result = self.dbx.files_copy_v2(source_file_id, dest_path)
        return cast(str, result.metadata.path_display)

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Return the Dropbox content hash for a file."""
        try:
            meta = self.dbx.files_get_metadata(file_id)
            if isinstance(meta, FileMetadata):
                return cast(Optional[str], meta.content_hash)
        except ApiError:
            pass
        return None

    def delete_file(self, file_id: str) -> None:
        self.dbx.files_delete_v2(file_id)
