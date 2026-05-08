from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

try:
    import msal
except ImportError:
    raise ImportError(
        "MSAL is required for the OneDrive backend.\n"
        "Install it with:  pipx install -e '.[onedrive]' --force"
    )

import requests

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SCOPES = ["Files.ReadWrite", "offline_access"]

# Allowlist of MSAL exception class names that DEFINITELY mean "the user
# must re-authenticate interactively". Substring matching ("Auth" / "Token"
# in cls_name) was too broad: any exception whose name happened to contain
# either substring (e.g. a hypothetical `TokenRateLimitError`, which is a
# transient rate-limit, not a credential failure) would be classified as
# AUTH and surface as a scary "re-authenticate" prompt, forcing the user
# into pointless interactive flows for what should auto-retry.
#
# Only names on this allowlist trigger an unconditional AUTH classification.
# `MsalServiceError` is intentionally excluded — it covers a broad range
# of server-side conditions, only some of which need re-auth; those are
# detected by the OAuth-error-code branch below (`invalid_grant`,
# `AADSTS50058`, etc.).
_MSAL_AUTH_CLASS_NAMES = frozenset({
    "MsalUiRequiredError",          # silent-token failed; needs interactive
    "InteractionRequiredAuthError", # MSAL Python alternative name
})

# OAuth / AAD error codes that unambiguously mean "refresh token is dead,
# user must re-auth". These appear in the exception args/description for
# MsalServiceError and similar broad exceptions; matching on the code
# string keeps the classification narrow.
_AUTH_ERROR_CODES = (
    "invalid_grant",
    "AADSTS50058",   # silent sign-in but no user signed in → re-auth
    "AADSTS70008",   # refresh token expired
    "AADSTS700082",  # refresh token expired due to inactivity
)


class OneDriveBackend(StorageBackend):
    """StorageBackend implementation for Microsoft OneDrive via Graph API."""

    backend_name = "onedrive"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: Optional[requests.Session] = None
        self._app: Optional[msal.PublicClientApplication] = None

    # ------------------------------------------------------------------
    # Error classification & retry
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map MSAL + Microsoft Graph HTTP exceptions to ErrorClass.

        Must not raise. JSON parsing of error bodies is wrapped defensively;
        any failure to introspect the exception falls through to UNKNOWN.
        """
        # MSAL-specific exceptions — lazily imported so the classifier
        # works even if the msal module fails an isinstance() check.
        try:
            import msal as _msal  # noqa: F401
            try:
                if isinstance(exc, _msal.exceptions.MsalServiceError):
                    return ErrorClass.TRANSIENT
            except Exception:
                pass
        except Exception:
            pass

        # Class-name allowlist — only specific MSAL exceptions count as AUTH.
        # See _MSAL_AUTH_CLASS_NAMES for rationale (replaces an over-broad
        # "Auth" / "Token" substring match that misclassified transient
        # token-rate-limit errors as auth failures).
        try:
            cls_name = type(exc).__name__
            if cls_name in _MSAL_AUTH_CLASS_NAMES:
                return ErrorClass.AUTH
        except Exception:
            cls_name = ""

        # For MsalServiceError and similar broad exceptions, inspect the
        # OAuth/AAD error code from the exception args. Only the codes
        # listed in _AUTH_ERROR_CODES mean "refresh token is dead, user
        # must re-auth"; everything else (rate-limit, transient service
        # errors) falls through to TRANSIENT or UNKNOWN below.
        try:
            err_text = " ".join(str(a) for a in getattr(exc, "args", ()))
        except Exception:
            err_text = ""
        if err_text and any(code in err_text for code in _AUTH_ERROR_CODES):
            return ErrorClass.AUTH

        # requests HTTPError — inspect status code + Graph error body.
        if isinstance(exc, requests.exceptions.HTTPError):
            status = None
            try:
                if exc.response is not None:
                    status = exc.response.status_code
            except Exception:
                status = None

            if status == 401:
                return ErrorClass.AUTH
            if status == 403:
                code = None
                try:
                    body = exc.response.json()
                    code = body.get("error", {}).get("code")
                except Exception:
                    code = None
                if code in ("quotaLimitReached", "insufficientStorage"):
                    return ErrorClass.QUOTA
                # "accessDenied" or anything else under 403 — permission.
                return ErrorClass.PERMISSION
            if status in (404, 413):
                return ErrorClass.FILE_REJECTED
            if status == 423:
                return ErrorClass.TRANSIENT
            if status == 429:
                return ErrorClass.QUOTA
            if status is not None and 500 <= status < 600:
                return ErrorClass.TRANSIENT
            if status is not None and 400 <= status < 500:
                return ErrorClass.FILE_REJECTED

        # Network / transport — all transient.
        if isinstance(exc, requests.exceptions.ConnectionError):
            return ErrorClass.TRANSIENT
        if isinstance(exc, requests.exceptions.Timeout):
            return ErrorClass.TRANSIENT
        if isinstance(exc, requests.exceptions.RequestException):
            return ErrorClass.TRANSIENT

        # Stdlib socket / OS-level transient errors.
        try:
            import socket as _socket
            if isinstance(exc, _socket.timeout):
                return ErrorClass.TRANSIENT
        except Exception:
            pass
        if isinstance(exc, TimeoutError):
            return ErrorClass.TRANSIENT
        if isinstance(exc, ConnectionError):
            return ErrorClass.TRANSIENT
        if isinstance(exc, OSError):
            return ErrorClass.TRANSIENT

        # RuntimeError raised by this backend's own auth helpers.
        if isinstance(exc, RuntimeError):
            try:
                msg = str(exc).lower()
            except Exception:
                msg = ""
            if (
                "not authenticated" in msg
                or "no cached" in msg
                or "no account" in msg
            ):
                return ErrorClass.AUTH

        return ErrorClass.UNKNOWN

    def _upload_with_retry(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        """Upload with exponential backoff on retryable errors.

        Wraps `upload_file` so the multi-backend orchestrator gets a
        BackendError with the proper classification on final failure.
        """
        max_attempts = getattr(self.config, "max_retry_attempts", 3)
        delays = [0.8, 1.6, 3.2]
        last_exc: Optional[BaseException] = None
        last_class = ErrorClass.UNKNOWN

        for attempt in range(max_attempts):
            try:
                return self.upload_file(local_path, rel_path, root_folder_id, file_id)
            except Exception as exc:
                last_exc = exc
                last_class = self.classify_error(exc)
                if not last_class.is_retryable:
                    raise BackendError(
                        last_class,
                        str(exc),
                        backend_name="onedrive",
                        cause=exc,
                    )
                # Retryable — backoff unless this was the final attempt.
                if attempt < max_attempts - 1:
                    delay = delays[attempt] if attempt < len(delays) else delays[-1]
                    time.sleep(delay)

        raise BackendError(
            last_class,
            str(last_exc) if last_exc is not None else "upload retries exhausted",
            backend_name="onedrive",
            cause=last_exc,
        )

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _get_app(self) -> msal.PublicClientApplication:
        if not self._app:
            self._app = msal.PublicClientApplication(
                self.config.onedrive_client_id,
                authority="https://login.microsoftonline.com/consumers",
            )
        return self._app

    def authenticate(self) -> requests.Session:
        """Interactive device-code OAuth2 flow."""
        app = self._get_app()
        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(f"Device flow failed: {flow.get('error_description', 'unknown error')}")

        print(f"\n1. Visit: {flow['verification_uri']}")
        print(f"2. Enter code: {flow['user_code']}\n")
        print("Waiting for authorization...")

        result = app.acquire_token_by_device_flow(flow)
        if "access_token" not in result:
            raise RuntimeError(
                f"Authentication failed: {result.get('error_description', result.get('error', 'unknown'))}"
            )

        # Save token cache
        token_path = Path(self.config.token_file)
        write_token_secure(token_path, json.dumps({
            "client_id": self.config.onedrive_client_id,
            "token_cache": app.token_cache.serialize(),
        }))

        self._session = self._make_session(result["access_token"])
        return self._session

    def get_credentials(self) -> requests.Session:
        """Load cached tokens and refresh silently.

        SECURITY: client_id is taken from config ONLY. Earlier versions
        read `client_id` from the token JSON for convenience, but a
        token file is supposed to hold opaque secrets — letting it carry
        configuration means a malicious actor with write access to the
        token path (or a malicious mirror config that points at one)
        could substitute the OAuth client_id used at refresh time and
        redirect to an attacker-controlled Azure app registration. The
        config-supplied client_id is the only trusted source.
        """
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            raise RuntimeError("Not authenticated. Run `claude-mirror auth` first.")

        data = json.loads(token_path.read_text())
        app = self._get_app()
        cache = msal.SerializableTokenCache()
        cache.deserialize(data.get("token_cache", "{}"))
        client_id = self.config.onedrive_client_id
        self._app = msal.PublicClientApplication(
            client_id,
            authority="https://login.microsoftonline.com/consumers",
            token_cache=cache,
        )

        accounts = self._app.get_accounts()
        if not accounts:
            raise RuntimeError(
                "No cached accounts found. Run `claude-mirror auth` again."
            )

        result = self._app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError(
                "Token refresh failed. Run `claude-mirror auth` again."
            )

        # Persist updated cache (refreshed tokens)
        write_token_secure(token_path, json.dumps({
            "client_id": self.config.onedrive_client_id,
            "token_cache": self._app.token_cache.serialize(),
        }))

        self._session = self._make_session(result["access_token"])
        return self._session

    @property
    def session(self) -> requests.Session:
        if not self._session:
            self.get_credentials()
        return self._session

    def _make_session(self, access_token: str) -> requests.Session:
        s = requests.Session()
        s.headers.update({"Authorization": f"Bearer {access_token}"})
        return s

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _item_url(self, rel_path: str) -> str:
        """Build a Graph API URL for an item by path relative to the OneDrive folder."""
        folder = self.config.onedrive_folder.strip("/")
        if rel_path:
            full = f"{folder}/{rel_path.strip('/')}"
        else:
            full = folder
        return f"{GRAPH_BASE}/me/drive/root:/{full}"

    def _item_by_id_url(self, item_id: str) -> str:
        """Build a Graph API URL for an item by its OneDrive item ID."""
        return f"{GRAPH_BASE}/me/drive/items/{item_id}"

    def _is_path_id(self, file_id: str) -> bool:
        """Check if file_id is a relative path (our convention) vs OneDrive item ID."""
        # OneDrive item IDs are alphanumeric strings like "01BYE5RZ..."
        # Our paths contain slashes or dots
        return "/" in file_id or "." in file_id or file_id == ""

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Create folder if needed. Returns relative path as 'ID'."""
        folder_path = f"{parent_id.rstrip('/')}/{name}" if parent_id else name
        url = f"{self._item_url(folder_path)}"

        # Check if exists
        resp = self.session.get(url)
        if resp.status_code == 200:
            return folder_path

        # Create via parent
        parent_url = self._item_url(parent_id) if parent_id else f"{GRAPH_BASE}/me/drive/root:/{self.config.onedrive_folder.strip('/')}"
        children_url = f"{parent_url}:/children"
        body = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "replace",
        }
        resp = self.session.post(children_url, json=body)
        resp.raise_for_status()
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

    def list_files_recursive(self, folder_id: str, prefix: str = "", progress_cb=None, exclude_folder_names=None) -> list[dict]:
        """List all files recursively under the OneDrive folder.

        `exclude_folder_names` prunes named subfolders at recursion time so
        we never issue Graph API calls for `_claude_mirror_snapshots/` etc.
        """
        results = []
        # Counters for progress_cb — folders explored, files seen so far.
        counters = [0, 0]
        self._list_recursive(
            folder_id or "", results,
            exclude_folder_names=exclude_folder_names or set(),
            progress_cb=progress_cb,
            counters=counters,
        )
        return results

    def _list_recursive(
        self,
        rel_folder: str,
        results: list[dict],
        exclude_folder_names: Optional[set] = None,
        progress_cb=None,
        counters: Optional[list] = None,
    ) -> None:
        """Recursively list children of a folder."""
        if rel_folder:
            url = f"{self._item_url(rel_folder)}:/children"
        else:
            folder = self.config.onedrive_folder.strip("/")
            url = f"{GRAPH_BASE}/me/drive/root:/{folder}:/children"

        excluded = exclude_folder_names or set()

        while url:
            resp = self.session.get(url, params={"$top": "200"})
            if resp.status_code == 404:
                return
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("value", []):
                name = item["name"]
                item_rel = f"{rel_folder}/{name}" if rel_folder else name

                if "folder" in item:
                    # Prune at recursion time — never API-call into the excluded folder.
                    if name in excluded or name.startswith("_claude_mirror"):
                        continue
                    if counters is not None:
                        counters[0] += 1
                        if progress_cb:
                            progress_cb(counters[0], counters[1])
                    self._list_recursive(
                        item_rel, results,
                        exclude_folder_names=exclude_folder_names,
                        progress_cb=progress_cb,
                        counters=counters,
                    )
                elif "file" in item:
                    if name.startswith("_"):
                        continue
                    # Extract hash — prefer quickXorHash
                    hashes = item.get("file", {}).get("hashes", {})
                    hash_value = hashes.get("quickXorHash", "") or hashes.get("sha1Hash", "")

                    results.append({
                        "id": item_rel,
                        "name": name,
                        "md5Checksum": hash_value,
                        "relative_path": item_rel,
                        "mimeType": item.get("file", {}).get("mimeType", ""),
                    })
                    if counters is not None:
                        counters[1] += 1
                        if progress_cb:
                            progress_cb(counters[0], counters[1])

            url = data.get("@odata.nextLink")

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        """List subfolders. Returns dicts with id, name, createdTime."""
        if parent_id:
            url = f"{self._item_url(parent_id)}:/children"
        else:
            folder = self.config.onedrive_folder.strip("/")
            url = f"{GRAPH_BASE}/me/drive/root:/{folder}:/children"

        results = []
        while url:
            resp = self.session.get(url, params={"$top": "200"})
            if resp.status_code == 404:
                return results
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("value", []):
                if "folder" not in item:
                    continue
                if name and item["name"] != name:
                    continue
                results.append({
                    "id": f"{parent_id}/{item['name']}" if parent_id else item["name"],
                    "name": item["name"],
                    "createdTime": item.get("createdDateTime", ""),
                })

            url = data.get("@odata.nextLink")
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
    ) -> str:
        """Upload a local file. Returns relative path as file 'ID'.

        Resume behaviour: the Microsoft Graph `createUploadSession`
        endpoint returns an `uploadUrl` that, per
        https://learn.microsoft.com/en-us/onedrive/developer/rest-api/api/driveitem_createuploadsession
        ,survives process restart for up to ~7 days. Re-using the URL
        from a persisted state file would let us resume mid-file after
        a crash — claude-mirror does not currently persist the upload
        session URL between runs, so a crashed upload re-creates the
        session on retry. In-process retries via `_upload_with_retry`
        cover the common transient-network case.
        """
        bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))
        if file_id and self._is_path_id(file_id):
            dest_rel = file_id
        elif not file_id:
            parent, filename = self.resolve_path(rel_path, root_folder_id)
            dest_rel = f"{parent.rstrip('/')}/{filename}" if parent else filename
        else:
            dest_rel = rel_path

        url = f"{self._item_url(dest_rel)}:/content"

        with open(local_path, "rb") as f:
            content = f.read()

        # Files < 4MB use simple upload; larger files need upload session
        if len(content) < 4 * 1024 * 1024:
            # Throttle BEFORE the PUT — the bucket pre-pays for the
            # whole body; for tiny markdown files this is essentially
            # free against a sensibly-sized bucket.
            bucket.consume(len(content))
            resp = self.session.put(url, data=content,
                                    headers={"Content-Type": "application/octet-stream"})
            resp.raise_for_status()
        else:
            self._upload_large(dest_rel, content, bucket=bucket)

        return dest_rel

    def _upload_large(self, dest_rel: str, content: bytes, bucket=None) -> None:
        """Upload a large file (>4MB) using a Microsoft Graph upload session.

        Per Graph API spec, the `uploadUrl` returned by `createUploadSession`
        is pre-authenticated — chunk PUTs must use a plain `requests.put`
        (NOT `self.session.put`) so the Bearer token is NOT sent. Adding the
        Authorization header to upload-session URLs is at best ignored and
        at worst rejected by the upload-host CDN.
        Ref: https://learn.microsoft.com/en-us/onedrive/developer/rest-api/api/driveitem_createuploadsession
        """
        if bucket is None:
            bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))
        url = f"{self._item_url(dest_rel)}:/createUploadSession"
        resp = self.session.post(url, json={
            "item": {"@microsoft.graph.conflictBehavior": "replace"},
        })
        resp.raise_for_status()
        upload_url = resp.json()["uploadUrl"]

        chunk_size = 10 * 1024 * 1024  # 10MB chunks
        total = len(content)
        for start in range(0, total, chunk_size):
            end = min(start + chunk_size, total)
            chunk = content[start:end]
            headers = {
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{end - 1}/{total}",
            }
            # Throttle per-chunk so the long-run rate stays honest
            # across multi-megabyte uploads.
            bucket.consume(len(chunk))
            # Intentionally `requests.put`, not `self.session.put` — see docstring.
            resp = requests.put(upload_url, data=chunk, headers=headers)
            resp.raise_for_status()

    def download_file(self, file_id: str) -> bytes:
        """Download file by relative path. Enforces MAX_DOWNLOAD_BYTES so
        a compromised remote or chunked-lying response can't OOM us."""
        url = f"{self._item_url(file_id)}:/content"
        resp = self.session.get(url, stream=True)
        resp.raise_for_status()
        # Pre-flight via Content-Length when present.
        size_hdr = resp.headers.get("Content-Length")
        if size_hdr is not None:
            try:
                size = int(size_hdr)
            except (TypeError, ValueError):
                size = -1
            if size > self.MAX_DOWNLOAD_BYTES:
                resp.close()
                raise RuntimeError(
                    f"Refusing OneDrive download of {file_id!r}: "
                    f"Content-Length {size} exceeds MAX_DOWNLOAD_BYTES "
                    f"({self.MAX_DOWNLOAD_BYTES})."
                )
        # Streaming accumulation with cap (handles chunked-encoding lies).
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > self.MAX_DOWNLOAD_BYTES:
                resp.close()
                raise RuntimeError(
                    f"OneDrive download of {file_id!r} streamed past "
                    f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting."
                )
        return bytes(buf)

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        if file_id and self._is_path_id(file_id):
            dest_rel = file_id
        else:
            dest_rel = f"{folder_id.rstrip('/')}/{name}" if folder_id else name

        url = f"{self._item_url(dest_rel)}:/content"
        resp = self.session.put(url, data=content,
                                headers={"Content-Type": "application/octet-stream"})
        resp.raise_for_status()
        return dest_rel

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Check if a file/folder exists. Returns relative path or None."""
        file_rel = f"{folder_id.rstrip('/')}/{name}" if folder_id else name
        url = self._item_url(file_rel)
        resp = self.session.get(url)
        if resp.status_code == 200:
            return file_rel
        return None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Server-side copy (async). Polls until complete."""
        # Get the OneDrive item ID of the destination folder
        dest_url = self._item_url(dest_folder_id)
        dest_resp = self.session.get(dest_url)
        dest_resp.raise_for_status()
        dest_item_id = dest_resp.json()["id"]

        # Initiate copy
        source_url = f"{self._item_url(source_file_id)}:/copy"
        body = {
            "parentReference": {"driveItemId": dest_item_id},
            "name": name,
        }
        resp = self.session.post(source_url, json=body)
        # Copy returns 202 Accepted with a monitor URL
        if resp.status_code == 202:
            monitor_url = resp.headers.get("Location")
            if monitor_url:
                self._wait_for_copy(monitor_url)
        elif resp.status_code >= 400:
            resp.raise_for_status()

        return f"{dest_folder_id.rstrip('/')}/{name}"

    def _wait_for_copy(self, monitor_url: str, timeout: int = 60) -> None:
        """Poll copy monitor URL until complete."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            resp = requests.get(monitor_url)
            if resp.status_code == 200:
                data = resp.json()
                status = data.get("status")
                if status == "completed":
                    return
                if status == "failed":
                    raise RuntimeError(f"Copy failed: {data.get('error', {}).get('message', 'unknown')}")
            elif resp.status_code == 303:
                return  # redirect to completed item
            time.sleep(1)

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Return the quickXorHash for a file."""
        url = self._item_url(file_id)
        resp = self.session.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        hashes = data.get("file", {}).get("hashes", {})
        return hashes.get("quickXorHash") or hashes.get("sha1Hash") or None

    def delete_file(self, file_id: str) -> None:
        """Delete a file/folder."""
        url = self._item_url(file_id)
        resp = self.session.delete(url)
        if resp.status_code != 404:  # already gone is fine
            resp.raise_for_status()
