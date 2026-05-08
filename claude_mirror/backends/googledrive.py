from __future__ import annotations

import io
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure as _write_token_secure


# Refresh proactively when the access token has less than this much life left.
# Default is 10s in the google-auth library, which is too tight for long-running
# operations like migrate-snapshots: many parallel API calls can simultaneously
# hit 401 and trigger concurrent refresh attempts, where a single transient
# network blip kills the whole operation. Refreshing 5 minutes before expiry
# gives us slack to retry once or twice if the network is flaky.
_PROACTIVE_REFRESH_THRESHOLD = timedelta(minutes=5)
_REFRESH_RETRY_ATTEMPTS = 3
_REFRESH_RETRY_BASE_DELAY = 0.8  # seconds; exponential backoff

_AUTH_VERBOSE = bool(os.environ.get("CLAUDE_MIRROR_AUTH_VERBOSE"))


def _verbose(msg: str) -> None:
    if _AUTH_VERBOSE:
        print(f"[claude-mirror auth] {msg}", file=sys.stderr, flush=True)


def _is_invalid_grant(exc: Exception) -> bool:
    """Detect the OAuth2 'invalid_grant' response — the only RefreshError
    that *definitely* means the user must re-authenticate. Everything else
    (transport, 5xx, rate-limit) is transient and should be retried."""
    # google.auth.exceptions.RefreshError typically has args = (message, body)
    # where body is the JSON dict from the OAuth endpoint. Check both.
    text = " ".join(str(a) for a in getattr(exc, "args", ()))
    text_lower = text.lower()
    if "invalid_grant" in text_lower:
        return True
    if "token has been expired or revoked" in text_lower:
        return True
    if "account has been deleted" in text_lower:
        return True
    return False


def _refresh_with_retry(creds: Credentials) -> None:
    """Refresh credentials with exponential backoff on transient errors.
    Re-raises immediately if the OAuth server says invalid_grant (which means
    the refresh token is genuinely dead and re-auth is required)."""
    last_exc: Optional[Exception] = None
    for attempt in range(_REFRESH_RETRY_ATTEMPTS):
        try:
            creds.refresh(Request())
            _verbose(
                f"refresh ok (attempt {attempt + 1}); "
                f"new expiry={creds.expiry.isoformat() if creds.expiry else 'unset'}"
            )
            return
        except RefreshError as e:
            if _is_invalid_grant(e):
                _verbose(f"refresh failed: invalid_grant — re-auth required ({e})")
                raise
            last_exc = e
            _verbose(
                f"refresh transient failure (attempt {attempt + 1}/"
                f"{_REFRESH_RETRY_ATTEMPTS}): {e}"
            )
        except Exception as e:
            # Transport, timeout, connection refused, etc.
            last_exc = e
            _verbose(
                f"refresh transport failure (attempt {attempt + 1}/"
                f"{_REFRESH_RETRY_ATTEMPTS}): {e}"
            )
        if attempt + 1 < _REFRESH_RETRY_ATTEMPTS:
            time.sleep(_REFRESH_RETRY_BASE_DELAY * (2 ** attempt))
    # Out of retries — re-raise the last transient error.
    assert last_exc is not None
    raise last_exc

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/pubsub",
]

LIST_WORKERS = 8


def _escape_q(value: str) -> str:
    """Escape a value for safe interpolation inside a Drive `q=` query string."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveBackend(StorageBackend):
    backend_name = "googledrive"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._creds: Optional[Credentials] = None
        # httplib2.Http (used inside the discovery service) is not thread-safe,
        # so each thread that calls into Drive gets its own service instance.
        self._thread_local = threading.local()
        self._creds_lock = threading.Lock()
        self._folder_cache: dict[tuple[str, str], str] = {}
        self._folder_cache_lock = threading.Lock()

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw Google client / HTTP exception to an ErrorClass.

        Defensive: if introspection fails for any reason, returns UNKNOWN
        rather than raising — a classifier that itself raises would mask
        the real failure from the orchestrator.
        """
        try:
            # OAuth refresh errors — message-based subclassification.
            if isinstance(exc, RefreshError):
                try:
                    if _is_invalid_grant(exc):
                        return ErrorClass.AUTH
                except Exception:
                    pass
                return ErrorClass.TRANSIENT

            if isinstance(exc, DefaultCredentialsError):
                return ErrorClass.AUTH

            if isinstance(exc, TransportError):
                return ErrorClass.TRANSIENT

            # googleapiclient HttpError — inspect status + reason.
            if isinstance(exc, HttpError):
                status = None
                try:
                    resp = getattr(exc, "resp", None)
                    if resp is not None:
                        status = int(getattr(resp, "status", 0) or 0)
                except Exception:
                    status = None

                # Pull the first error reason out of the JSON body if present.
                reason = ""
                try:
                    error_details = getattr(exc, "error_details", None)
                    if error_details and isinstance(error_details, list):
                        first = error_details[0]
                        if isinstance(first, dict):
                            reason = str(first.get("reason", "")).strip()
                    if not reason:
                        # Fallback: parse content bytes if available.
                        import json as _json
                        content = getattr(exc, "content", None)
                        if content:
                            if isinstance(content, bytes):
                                content = content.decode("utf-8", errors="replace")
                            try:
                                body = _json.loads(content)
                                errors = body.get("error", {}).get("errors") or []
                                if errors and isinstance(errors[0], dict):
                                    reason = str(errors[0].get("reason", "")).strip()
                            except Exception:
                                pass
                except Exception:
                    reason = ""

                if status == 401:
                    return ErrorClass.AUTH
                if status == 403:
                    if reason == "authError":
                        return ErrorClass.AUTH
                    # `userRateLimitExceeded` and `rateLimitExceeded` are
                    # account-wide throttle signals — every parallel
                    # worker should pause via the shared coordinator
                    # rather than retrying independently. `quotaExceeded`
                    # means the daily/storage quota is exhausted (user
                    # action required), so it stays QUOTA.
                    if reason in ("userRateLimitExceeded", "rateLimitExceeded"):
                        return ErrorClass.RATE_LIMIT_GLOBAL
                    if reason == "quotaExceeded":
                        return ErrorClass.QUOTA
                    if reason in ("forbidden", "domainPolicy", "insufficientPermissions"):
                        return ErrorClass.PERMISSION
                    # Unclassified 403 — treat as permission to be safe.
                    return ErrorClass.PERMISSION
                if status == 404:
                    return ErrorClass.FILE_REJECTED
                if status == 413:
                    return ErrorClass.FILE_REJECTED
                if status == 429:
                    # File-level rejections (`fileSizeLimitExceeded`)
                    # never come back as 429 in Drive's contract, but
                    # the per-file body reason is still inspected for
                    # forward-compat.
                    if reason in ("fileSizeLimitExceeded",):
                        return ErrorClass.FILE_REJECTED
                    # Plain 429 with no rate-limit reason still means
                    # the account is being throttled overall.
                    return ErrorClass.RATE_LIMIT_GLOBAL
                if status is not None and 500 <= status < 600:
                    return ErrorClass.TRANSIENT
                if status is not None and 400 <= status < 500:
                    return ErrorClass.FILE_REJECTED
                return ErrorClass.UNKNOWN

            # Network-level failures — these come from httplib2 / sockets,
            # not the Google libraries. socket.timeout is a subclass of
            # OSError on Python 3.10+, but we list it explicitly anyway.
            import socket as _socket
            if isinstance(exc, _socket.timeout):
                return ErrorClass.TRANSIENT
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
        file_id=None,
    ) -> str:
        """Upload with exponential backoff on TRANSIENT/UNKNOWN errors.

        Wraps `upload_file`. Retries up to `config.max_retry_attempts` times
        (default 3) with backoff 0.8s, 1.6s, 3.2s, ... Non-retryable errors
        are wrapped in BackendError and raised immediately. Exhausted retries
        raise BackendError(TRANSIENT, ...). Never propagates raw causes.
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
                    backend_name="googledrive",
                    cause=exc,
                ) from exc

        # Exhausted retries on a retryable error.
        raise BackendError(
            last_class,
            f"upload failed after {max_attempts} attempts: {last_exc}",
            backend_name="googledrive",
            cause=last_exc,
        ) from last_exc

    def _needs_refresh(self, creds: Credentials) -> bool:
        """True if the access token is expired OR within the proactive
        threshold of expiring. Refreshing proactively avoids the situation
        where many parallel API calls all hit 401 simultaneously and race."""
        if not creds.expiry:
            return False
        # creds.expiry is a naive UTC datetime per the google-auth library.
        now = datetime.utcnow()
        return creds.expiry - now < _PROACTIVE_REFRESH_THRESHOLD

    def authenticate(self) -> Credentials:
        token_path = Path(self.config.token_file)
        creds: Optional[Credentials] = None

        if token_path.exists():
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

        if not creds or not creds.valid:
            if creds and creds.refresh_token and self._needs_refresh(creds):
                try:
                    _refresh_with_retry(creds)
                except RefreshError as e:
                    if _is_invalid_grant(e):
                        # Refresh token is genuinely dead — fall through to
                        # fresh browser flow.
                        creds = None
                        token_path.unlink(missing_ok=True)
                    else:
                        # Transient error AFTER retries — let it bubble up.
                        # Tearing the token file down on a network blip means
                        # the user has to re-auth for transient infra issues.
                        raise
            if not creds or not creds.valid:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.config.credentials_file, SCOPES
                )
                # prompt='consent' forces Google to always return a refresh_token.
                # Without it, re-running auth returns only an access token (no
                # refresh_token), so the next day when the access token expires
                # the library cannot refresh silently and auth fails.
                creds = flow.run_local_server(port=0, prompt="consent")
            _write_token_secure(token_path, creds.to_json())

        self._creds = creds
        self._thread_local.service = build("drive", "v3", credentials=creds)
        return creds

    def get_credentials(self) -> Credentials:
        # Serialize refresh attempts so parallel workers don't race on token rotation.
        with self._creds_lock:
            if self._creds:
                if self._creds.refresh_token and self._needs_refresh(self._creds):
                    try:
                        _refresh_with_retry(self._creds)
                        _write_token_secure(
                            Path(self.config.token_file), self._creds.to_json()
                        )
                    except RefreshError as e:
                        if _is_invalid_grant(e):
                            raise RuntimeError(
                                "Google refresh token has been revoked or expired "
                                "(invalid_grant). This usually means: the OAuth "
                                "client was rotated, the user revoked access, the "
                                "Workspace admin invalidated tokens, or the "
                                "Cloud Session Control reauth interval elapsed. "
                                "Run `claude-mirror auth` to reauthenticate."
                            ) from e
                        raise RuntimeError(
                            f"Could not refresh Google token after "
                            f"{_REFRESH_RETRY_ATTEMPTS} attempts: {e}. "
                            "This is likely a transient network or Google-side "
                            "issue — try the command again. If it persists, "
                            "run `claude-mirror auth --check` to diagnose."
                        ) from e
                return self._creds

            token_path = Path(self.config.token_file)
            if not token_path.exists():
                raise RuntimeError("Not authenticated. Run `claude-mirror auth` first.")
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not creds.refresh_token:
                raise RuntimeError(
                    "Token file is missing a refresh_token (this happens when `claude-mirror auth` "
                    "was run a second time without forcing the consent screen). "
                    "Run `claude-mirror auth` again to get a fresh token with refresh_token."
                )
            if self._needs_refresh(creds):
                try:
                    _refresh_with_retry(creds)
                    _write_token_secure(token_path, creds.to_json())
                except RefreshError as e:
                    if _is_invalid_grant(e):
                        raise RuntimeError(
                            "Google refresh token has been revoked or expired "
                            "(invalid_grant). Run `claude-mirror auth` to "
                            "reauthenticate."
                        ) from e
                    raise RuntimeError(
                        f"Could not refresh Google token after "
                        f"{_REFRESH_RETRY_ATTEMPTS} attempts: {e}. "
                        "Try again, or run `claude-mirror auth --check` to diagnose."
                    ) from e
            self._creds = creds
            return creds

    @property
    def service(self):
        svc = getattr(self._thread_local, "service", None)
        if svc is None:
            creds = self.get_credentials()
            svc = build("drive", "v3", credentials=creds)
            self._thread_local.service = svc
        return svc

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        cache_key = (parent_id, name)
        with self._folder_cache_lock:
            cached = self._folder_cache.get(cache_key)
        if cached is not None:
            return cached

        query = (
            f"name='{_escape_q(name)}' and mimeType='application/vnd.google-apps.folder' "
            f"and '{_escape_q(parent_id)}' in parents and trashed=false"
        )
        result = self.service.files().list(q=query, fields="files(id)").execute()
        files = result.get("files", [])
        if files:
            folder_id = files[0]["id"]
        else:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            }
            folder = self.service.files().create(body=meta, fields="id").execute()
            folder_id = folder["id"]

        with self._folder_cache_lock:
            self._folder_cache[cache_key] = folder_id
        return folder_id

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Return (parent_folder_id, filename) for a relative path, creating folders as needed."""
        parts = Path(rel_path).parts
        if len(parts) == 1:
            return root_folder_id, parts[0]
        current = root_folder_id
        for part in parts[:-1]:
            current = self.get_or_create_folder(part, current)
        return current, parts[-1]

    def _list_folder_one_level(
        self,
        folder_id: str,
        prefix: str,
        exclude_folder_names: Optional[set] = None,
    ) -> tuple[list[dict], list[tuple[str, str]]]:
        """List a single folder (no recursion). Returns (files, subfolders_to_traverse).

        Subfolders whose name is in `exclude_folder_names` are dropped here so
        the BFS never issues an API call for them (e.g. `_claude_mirror_snapshots/`).
        """
        files: list[dict] = []
        subfolders: list[tuple[str, str]] = []
        page_token = None
        while True:
            params = {
                "q": f"'{_escape_q(folder_id)}' in parents and trashed=false",
                "fields": "nextPageToken, files(id, name, md5Checksum, mimeType)",
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            response = self.service.files().list(**params).execute()
            for item in response.get("files", []):
                if item["mimeType"] == "application/vnd.google-apps.folder":
                    if exclude_folder_names and item["name"] in exclude_folder_names:
                        continue  # don't descend into pruned folders
                    subfolders.append((item["id"], f"{prefix}{item['name']}/"))
                else:
                    item["relative_path"] = f"{prefix}{item['name']}"
                    files.append(item)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return files, subfolders

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb=None,
        exclude_folder_names: Optional[set] = None,
    ) -> list[dict]:
        """List all non-folder files recursively. Subfolder fetches run in parallel.

        If `progress_cb` is provided, it is called as `(folders_done, files_seen)`
        after each folder is fully listed — useful for rendering a live counter.

        If `exclude_folder_names` is provided, the BFS prunes those subfolders
        at discovery time and never issues an API call for them. Critical for
        skipping `_claude_mirror_snapshots/` (which holds a full project copy
        per snapshot) and `_claude_mirror_logs/` — without pruning, status
        explodes from N files to N × (snapshot_count + 1).
        """
        results: list[dict] = []
        pending: list[tuple[str, str]] = [(folder_id, prefix)]
        folders_done = 0
        if progress_cb:
            progress_cb(0, 0)
        while pending:
            workers = min(LIST_WORKERS, len(pending))
            next_pending: list[tuple[str, str]] = []
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = [
                    ex.submit(self._list_folder_one_level, fid, pfx, exclude_folder_names)
                    for fid, pfx in pending
                ]
                for future in as_completed(futures):
                    files, subfolders = future.result()
                    results.extend(files)
                    next_pending.extend(subfolders)
                    folders_done += 1
                    if progress_cb:
                        progress_cb(folders_done, len(results))
            pending = next_pending
        return results

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        """List subfolders of parent. If name given, filter by exact name."""
        query = (
            f"'{_escape_q(parent_id)}' in parents "
            "and mimeType='application/vnd.google-apps.folder' "
            "and trashed=false"
        )
        if name:
            query += f" and name='{_escape_q(name)}'"
        result = self.service.files().list(
            q=query,
            fields="files(id, name, createdTime)",
            orderBy="createdTime desc",
        ).execute()
        return result.get("files", [])

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Resumable upload with optional bandwidth throttling.

        Drive resumable upload semantics (per
        https://developers.google.com/drive/api/guides/manage-uploads
        #resumable): the client opens an upload session, the server
        returns a session URI, and the client streams 256 KiB-aligned
        chunks (default chunksize ≈ 5 MiB in the python client). This
        protocol natively SURVIVES PROCESS RESTART — the upload session
        URI can be reused to resume a partially-uploaded file. We rely
        on it implicitly via `resumable=True`; integration-level resume
        from disk after a crash is a future feature, not in v0.5.39.

        Bandwidth-cap path: when `max_upload_kbps` is set we drive the
        upload manually via `request.next_chunk()` so we can call
        `bucket.consume(chunk_size)` BEFORE each chunk goes on the wire.
        Without a cap we keep the legacy single-`.execute()` path so
        Drive's internal resumable loop runs at full speed.

        progress_callback: optional `Callable[[int], None]`. Invoked with
        the number of bytes-transferred-since-the-last-call (a delta,
        not a cumulative count) after each upload chunk completes. When
        unset, behaviour is identical to the legacy path — no extra
        SDK calls, no per-chunk overhead. When set we always drive the
        upload via the manual `next_chunk()` loop so each iteration can
        emit a callback even on the no-throttle path.
        """
        rate = getattr(self.config, "max_upload_kbps", None)
        media = MediaFileUpload(local_path, resumable=True)
        if file_id:
            request = self.service.files().update(
                fileId=file_id,
                media_body=media,
                fields="id, md5Checksum",
            )
        else:
            parent_id, name = self.resolve_path(rel_path, root_folder_id)
            meta = {"name": name, "parents": [parent_id]}
            request = self.service.files().create(
                body=meta,
                media_body=media,
                fields="id, md5Checksum",
            )
        if not rate and progress_callback is None:
            # Legacy fast path — Drive's SDK runs the resumable loop
            # internally with no per-chunk hook.
            file = request.execute()
            return file["id"]

        # Manual chunk loop — used when either bandwidth-cap is on or
        # the caller wants per-chunk progress callbacks.
        bucket = get_throttle(rate)
        try:
            file_size = int(os.path.getsize(local_path))
        except OSError:
            file_size = 0
        # `chunksize` is a property on MediaFileUpload; default ≈ 5 MiB.
        try:
            chunk_size = int(media.chunksize)
        except Exception:
            chunk_size = 5 * 1024 * 1024
        last_progress = 0
        response = None
        while response is None:
            reserve = max(chunk_size, 1)
            if file_size:
                remaining = max(0, file_size - last_progress)
                if remaining > 0:
                    reserve = min(reserve, remaining)
            bucket.consume(reserve)
            status, response = request.next_chunk()
            new_progress = last_progress
            if status is not None:
                new_progress = int(getattr(status, "resumable_progress", last_progress))
            elif response is not None and file_size:
                # Final chunk — `status` is None when the upload completes
                # in this iteration; treat the remainder of the file as
                # transferred so `progress_callback` covers the full file.
                new_progress = file_size
            if progress_callback is not None:
                delta = max(0, new_progress - last_progress)
                if delta:
                    progress_callback(delta)
            last_progress = new_progress
        return response["id"]

    # Hard cap on remote-file size we will load into memory. claude-mirror is
    # designed for small text/markdown files; nothing legitimate should ever
    # be larger. A malicious or accidental large file would otherwise OOM
    # the process, so we refuse before downloading instead of after.
    MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024  # 1 GiB

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bytes:
        """Download a Drive file by ID.

        progress_callback: optional `Callable[[int], None]`. Invoked with
        delta bytes (since the previous call) after each `next_chunk()`
        iteration. When unset, behaviour is unchanged.
        """
        # Pre-flight size check via metadata — cheap, avoids streaming a huge
        # file into memory only to discover it's too big at the end.
        try:
            meta = self.service.files().get(fileId=file_id, fields="size").execute()
            size = int(meta.get("size", 0)) if meta.get("size") else 0
        except Exception:
            size = 0  # fall through; downloader will fail naturally if broken
        if size and size > self.MAX_DOWNLOAD_BYTES:
            raise RuntimeError(
                f"Refusing to download Drive file {file_id}: size {size:,} bytes "
                f"exceeds MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES:,}). "
                "claude-mirror is intended for text/markdown files; if this is "
                "intentional, raise the limit in claude_mirror/backends/googledrive.py."
            )
        request = self.service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        last_pos = 0
        while not done:
            _, done = downloader.next_chunk()
            if progress_callback is not None:
                # `MediaIoBaseDownload` writes bytes into `buffer`; the
                # tell() position is the cumulative bytes downloaded.
                cur = buffer.tell()
                delta = max(0, cur - last_pos)
                if delta:
                    progress_callback(delta)
                last_pos = cur
        return buffer.getvalue()

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mimetype, resumable=True)
        if file_id:
            file = self.service.files().update(
                fileId=file_id, media_body=media, fields="id"
            ).execute()
        else:
            meta = {"name": name, "parents": [folder_id]}
            file = self.service.files().create(
                body=meta, media_body=media, fields="id"
            ).execute()
        return file["id"]

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        query = (
            f"name='{_escape_q(name)}' and "
            f"'{_escape_q(folder_id)}' in parents and trashed=false"
        )
        result = self.service.files().list(q=query, fields="files(id)").execute()
        files = result.get("files", [])
        return files[0]["id"] if files else None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Server-side copy — no data transfer through the client."""
        result = self.service.files().copy(
            fileId=source_file_id,
            body={"name": name, "parents": [dest_folder_id]},
            fields="id",
        ).execute()
        return result["id"]

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Fetch only the md5Checksum of a Drive file (no download)."""
        result = self.service.files().get(fileId=file_id, fields="md5Checksum").execute()
        return result.get("md5Checksum")

    def delete_file(self, file_id: str) -> None:
        self.service.files().delete(fileId=file_id).execute()
