from __future__ import annotations

import hashlib
import json
import socket
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote as urlquote, unquote as urlunquote

import requests
from requests.auth import HTTPBasicAuth

from ..config import Config
from ..throttle import get_throttle
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure

# WebDAV XML namespaces
DAV_NS = "DAV:"
OC_NS = "http://owncloud.org/ns"


def _dav_tag(local: str) -> str:
    return f"{{{DAV_NS}}}{local}"


def _oc_tag(local: str) -> str:
    return f"{{{OC_NS}}}{local}"


class WebDAVBackend(StorageBackend):
    """StorageBackend implementation for WebDAV servers (Nextcloud, OwnCloud, Apache, etc.)."""

    backend_name = "webdav"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._session: Optional[requests.Session] = None
        self._validate_url_scheme()

    def _validate_url_scheme(self) -> None:
        """Reject http:// WebDAV URLs unless explicitly opted-in.

        WebDAV uses HTTP basic authentication, which transmits the
        username and password (base64-encoded but NOT encrypted) on
        every PROPFIND/PUT/GET. On http:// any network observer between
        the client and the server can read both the credentials and the
        file payloads in cleartext.

        Raises ValueError at construction time if the configured URL
        is http:// and `webdav_insecure_http` is not set to true.
        Schemes other than http/https are passed through to the rest
        of the backend, which will reject them via the existing URL
        parsing path.
        """
        url = (self.config.webdav_url or "").strip()
        if not url:
            return  # empty URL: a different code path will surface this
        # Cheap scheme check — urlparse is overkill and we only need
        # to discriminate http:// from https://.
        scheme = url.split(":", 1)[0].lower() if ":" in url else ""
        if scheme == "http" and not getattr(
            self.config, "webdav_insecure_http", False,
        ):
            raise ValueError(
                "WebDAV URL must use https:// — set "
                "webdav_insecure_http: true in config to allow http "
                "(NOT recommended)"
            )

    # ------------------------------------------------------------------
    # Error classification & retry
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map a raw WebDAV / requests / network exception to an ErrorClass.

        WebDAV uses basic auth: a 401 always means credentials are wrong (no
        notion of "token expired"), so 401 -> AUTH prompts a re-auth. Other
        HTTP statuses map per RFC 4918 + common server practice. The
        classifier MUST NOT raise; any unexpected failure inside it falls
        back to UNKNOWN.
        """
        try:
            # HTTPError carries a response with a status code (most cases).
            if isinstance(exc, requests.exceptions.HTTPError):
                status: Optional[int] = None
                try:
                    resp = getattr(exc, "response", None)
                    if resp is not None:
                        status = getattr(resp, "status_code", None)
                except Exception:
                    status = None

                if status is None:
                    return ErrorClass.TRANSIENT

                if status == 401:
                    return ErrorClass.AUTH
                if status == 403:
                    return ErrorClass.PERMISSION
                if status == 404:
                    return ErrorClass.FILE_REJECTED
                if status == 405:
                    return ErrorClass.FILE_REJECTED
                if status == 409:
                    return ErrorClass.FILE_REJECTED
                if status == 412:
                    return ErrorClass.TRANSIENT
                if status == 413:
                    return ErrorClass.FILE_REJECTED
                if status == 423:
                    return ErrorClass.TRANSIENT
                if status == 429:
                    # Most WebDAV servers don't send 429 (they use 503
                    # under load), but a server that does is signalling
                    # account-wide throttling — route through the shared
                    # backoff coordinator the same as Drive / Dropbox /
                    # OneDrive.
                    return ErrorClass.RATE_LIMIT_GLOBAL
                if status == 507:
                    return ErrorClass.QUOTA
                if status in (502, 503, 504):
                    return ErrorClass.TRANSIENT
                if 500 <= status < 600:
                    return ErrorClass.TRANSIENT
                if 400 <= status < 500:
                    return ErrorClass.FILE_REJECTED
                return ErrorClass.UNKNOWN

            # Specific timeout/connection subclasses before the generic base.
            if isinstance(exc, (
                requests.exceptions.ConnectTimeout,
                requests.exceptions.ReadTimeout,
                requests.exceptions.Timeout,
            )):
                return ErrorClass.TRANSIENT
            if isinstance(exc, requests.exceptions.SSLError):
                return ErrorClass.TRANSIENT
            if isinstance(exc, requests.exceptions.ConnectionError):
                return ErrorClass.TRANSIENT
            if isinstance(exc, requests.exceptions.RequestException):
                return ErrorClass.TRANSIENT

            # Low-level network/OS errors.
            if isinstance(exc, socket.timeout):
                return ErrorClass.TRANSIENT
            if isinstance(exc, TimeoutError):
                return ErrorClass.TRANSIENT
            if isinstance(exc, ConnectionError):
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
                    backend_name="webdav",
                    cause=exc,
                ) from exc

        # Exhausted retries on a retryable error.
        raise BackendError(
            last_class,
            f"upload failed after {max_attempts} attempts: {last_exc}",
            backend_name="webdav",
            cause=last_exc,
        ) from last_exc

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> requests.Session:
        """Prompt for username/password and save to token file."""
        print("\nWebDAV authentication\n")
        print(f"Server URL: {self.config.webdav_url}")

        if self.config.webdav_username:
            username = self.config.webdav_username
            print(f"Username: {username}")
        else:
            username = input("Username: ").strip()

        if self.config.webdav_password:
            password = self.config.webdav_password
            print("Password: (from config)")
        else:
            import getpass
            password = getpass.getpass("Password (or app password): ")

        # Test connection
        session = self._make_session(username, password)
        resp = session.request("PROPFIND", self.config.webdav_url, headers={"Depth": "0"})
        if resp.status_code == 401:
            raise RuntimeError("Authentication failed — check username and password.")
        resp.raise_for_status()

        # Save credentials — token file holds the WebDAV password in plaintext,
        # so chmod 0600 is critical to prevent other local users from reading it.
        token_path = Path(self.config.token_file)
        write_token_secure(token_path, json.dumps({
            "username": username,
            "password": password,
        }))

        self._session = session
        return session

    def get_credentials(self) -> requests.Session:
        """Load saved credentials and return a configured session."""
        # Check token file first, then fall back to config fields
        token_path = Path(self.config.token_file)
        if token_path.exists():
            data = json.loads(token_path.read_text())
            username = data.get("username", "")
            password = data.get("password", "")
        elif self.config.webdav_username and self.config.webdav_password:
            username = self.config.webdav_username
            password = self.config.webdav_password
        else:
            raise RuntimeError("Not authenticated. Run `claude-mirror auth` first.")

        self._session = self._make_session(username, password)
        return self._session

    @property
    def session(self) -> requests.Session:
        if not self._session:
            self.get_credentials()
        return self._session

    def _make_session(self, username: str, password: str) -> requests.Session:
        s = requests.Session()
        s.auth = HTTPBasicAuth(username, password)
        return s

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        """Return the base WebDAV URL (normalized, no trailing slash)."""
        return self.config.webdav_url.rstrip("/")

    def _url(self, *parts: str) -> str:
        """Build a full URL from path parts, encoding each segment."""
        base = self._base_url()
        for part in parts:
            if not part:
                continue
            # Encode each path segment individually
            segments = part.strip("/").split("/")
            encoded = "/".join(urlquote(s, safe="") for s in segments)
            base = f"{base}/{encoded}"
        return base

    def _rel_from_url(self, url: str) -> str:
        """Extract relative path from a full URL or href."""
        base = self._base_url()
        # href may be URL-encoded path without host
        from urllib.parse import urlparse
        parsed_base = urlparse(base)
        base_path = parsed_base.path.rstrip("/")

        if url.startswith("http"):
            parsed = urlparse(url)
            path = parsed.path
        else:
            path = url

        path = urlunquote(path.rstrip("/"))
        base_decoded = urlunquote(base_path)

        if path.startswith(base_decoded):
            rel = path[len(base_decoded):].lstrip("/")
            return rel
        return path.lstrip("/")

    # ------------------------------------------------------------------
    # PROPFIND helpers
    # ------------------------------------------------------------------

    def _propfind(self, url: str, depth: str = "1", extra_props: list[str] | None = None) -> ET.Element:
        """Execute a PROPFIND request and return the parsed XML response."""
        # Build property request body
        props = [
            f"<d:resourcetype/>",
            f"<d:getcontentlength/>",
            f"<d:getetag/>",
            f"<d:getlastmodified/>",
            f"<d:getcontenttype/>",
        ]
        if extra_props:
            props.extend(extra_props)

        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:prop>"
            + "".join(props)
            + "</d:prop>"
            "</d:propfind>"
        )

        resp = self.session.request(
            "PROPFIND", url,
            headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            data=body.encode("utf-8"),
        )
        resp.raise_for_status()
        return ET.fromstring(resp.content)

    def _is_collection(self, response_elem: ET.Element) -> bool:
        """Check if a PROPFIND response element is a collection (folder)."""
        rt = response_elem.find(f".//{_dav_tag('resourcetype')}")
        if rt is not None:
            return rt.find(_dav_tag("collection")) is not None
        return False

    def _get_etag(self, response_elem: ET.Element) -> str:
        """Extract etag from a PROPFIND response element."""
        etag = response_elem.find(f".//{_dav_tag('getetag')}")
        if etag is not None and etag.text:
            return etag.text.strip('"')
        return ""

    def _get_href(self, response_elem: ET.Element) -> str:
        """Extract href from a PROPFIND response element."""
        href = response_elem.find(_dav_tag("href"))
        return href.text if href is not None and href.text else ""

    def _get_last_modified(self, response_elem: ET.Element) -> str:
        """Extract getlastmodified from a PROPFIND response element."""
        lm = response_elem.find(f".//{_dav_tag('getlastmodified')}")
        if lm is not None and lm.text:
            return lm.text
        return ""

    # ------------------------------------------------------------------
    # Folder operations
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Create folder if needed. Returns the path as the 'ID'."""
        folder_path = f"{parent_id.rstrip('/')}/{name}"
        url = self._url(folder_path)
        resp = self.session.request("MKCOL", url)
        if resp.status_code == 405:
            # Already exists
            pass
        elif resp.status_code >= 400:
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
        """List all files recursively using PROPFIND with Depth: infinity.

        Falls back to recursive Depth: 1 calls if the server rejects infinity.
        `exclude_folder_names` drops any entry whose path passes through a
        named excluded folder — used to skip `_claude_mirror_snapshots/` etc.
        """
        url = self._url(folder_id)
        try:
            root = self._propfind(url, depth="infinity", extra_props=["<oc:checksums/>"])
            results = self._parse_file_list(root, folder_id, exclude_folder_names)
        except requests.HTTPError:
            # Server doesn't support Depth: infinity — recurse manually.
            results = self._list_recursive_manual(folder_id, exclude_folder_names=exclude_folder_names)
        if progress_cb:
            progress_cb(0, len(results))
        return results

    def _parse_file_list(
        self,
        root: ET.Element,
        base_folder: str,
        exclude_folder_names: Optional[set] = None,
    ) -> list[dict]:
        """Parse PROPFIND multistat response into file dicts."""
        results = []
        excluded = exclude_folder_names or set()
        for response in root.findall(_dav_tag("response")):
            if self._is_collection(response):
                continue
            href = self._get_href(response)
            rel = self._rel_from_url(href)
            if not rel:
                continue
            # Strip the base folder prefix to get relative path within project
            base_stripped = base_folder.strip("/")
            if rel.startswith(base_stripped + "/"):
                rel = rel[len(base_stripped) + 1:]
            elif rel.startswith(base_stripped):
                rel = rel[len(base_stripped):]

            if not rel or rel.startswith("_claude_mirror"):
                continue
            # Drop entries whose path passes through a named excluded folder.
            if excluded and any(c in excluded for c in rel.split("/")):
                continue

            etag = self._get_etag(response)
            # Try to get OwnCloud/Nextcloud checksum
            checksum = self._get_oc_checksum(response)
            hash_value = checksum or etag

            results.append({
                "id": rel,
                "name": Path(rel).name,
                "md5Checksum": hash_value,
                "relative_path": rel,
                "mimeType": "",
            })
        return results

    def _list_recursive_manual(self, folder_id: str, exclude_folder_names: Optional[set] = None) -> list[dict]:
        """Recursively list files using Depth: 1 (fallback). Prunes excluded
        folders at recursion time so we never PROPFIND into them."""
        results = []
        excluded = exclude_folder_names or set()
        url = self._url(folder_id)
        try:
            root = self._propfind(url, depth="1", extra_props=["<oc:checksums/>"])
        except requests.HTTPError:
            return results

        for response in root.findall(_dav_tag("response")):
            href = self._get_href(response)
            rel = self._rel_from_url(href)
            base_stripped = folder_id.strip("/")

            if rel.rstrip("/") == base_stripped:
                continue  # skip self

            if self._is_collection(response):
                # Recurse into subfolder
                sub_path = rel
                if sub_path.startswith(base_stripped + "/"):
                    sub_path_rel = sub_path[len(base_stripped) + 1:]
                else:
                    sub_path_rel = sub_path
                if sub_path_rel.startswith("_claude_mirror"):
                    continue
                # Prune named excluded folders (e.g. _claude_sync_snapshots).
                last_component = sub_path_rel.rstrip("/").split("/")[-1]
                if excluded and last_component in excluded:
                    continue
                sub_results = self._list_recursive_manual(rel, exclude_folder_names=excluded)
                results.extend(sub_results)
            else:
                if rel.startswith(base_stripped + "/"):
                    file_rel = rel[len(base_stripped) + 1:]
                else:
                    file_rel = rel

                if not file_rel or file_rel.startswith("_claude_mirror"):
                    continue

                etag = self._get_etag(response)
                checksum = self._get_oc_checksum(response)
                hash_value = checksum or etag

                results.append({
                    "id": file_rel,
                    "name": Path(file_rel).name,
                    "md5Checksum": hash_value,
                    "relative_path": file_rel,
                    "mimeType": "",
                })
        return results

    def _get_oc_checksum(self, response_elem: ET.Element) -> str:
        """Try to extract OwnCloud/Nextcloud checksum (SHA1 or MD5)."""
        checksums = response_elem.find(f".//{_oc_tag('checksums')}")
        if checksums is not None and checksums.text:
            # Format: "SHA1:abc123 MD5:def456" or just "SHA1:abc123"
            for part in checksums.text.split():
                if part.startswith("MD5:"):
                    return part[4:]
                if part.startswith("SHA1:"):
                    return part[5:]
            return checksums.text.strip()
        return ""

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        """List subfolders. Returns dicts with id, name, createdTime."""
        url = self._url(parent_id)
        results = []
        try:
            root = self._propfind(url, depth="1")
        except requests.HTTPError:
            return results

        base_stripped = parent_id.strip("/")

        for response in root.findall(_dav_tag("response")):
            href = self._get_href(response)
            rel = self._rel_from_url(href)
            if rel.rstrip("/") == base_stripped:
                continue  # skip self
            if not self._is_collection(response):
                continue

            folder_name = urlunquote(rel.rstrip("/").split("/")[-1])
            if name and folder_name != name:
                continue

            results.append({
                "id": rel.rstrip("/"),
                "name": folder_name,
                "createdTime": self._get_last_modified(response),
            })
        return results

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    # Streaming-PUT block size. 1 MiB is small enough to keep peak
    # memory bounded for arbitrarily large files, large enough to
    # amortise per-block syscall + TLS-frame overhead.
    _STREAM_CHUNK_BYTES: int = 1 * 1024 * 1024

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> str:
        """Upload a local file via PUT. Returns the relative path as file 'ID'.

        Two paths are selected by `webdav_streaming_threshold_bytes`
        (default 4 MiB):

          * Small files use a simple in-memory PUT (the historic path)
            — minimal overhead for typical markdown content.
          * Large files use a streaming chunk-iterator PUT with explicit
            `Content-Length` so the request body never fully resides in
            memory; peak memory is bounded to one `_STREAM_CHUNK_BYTES`
            block (1 MiB) regardless of file size.

        Resume behaviour: WebDAV has no native resume protocol. A
        crashed upload re-uploads from scratch on retry; in-process
        retries are covered by `_upload_with_retry`.

        progress_callback: optional `Callable[[int], None]`. For the
        streaming path, the chunk generator emits a delta per block;
        for the small-file path, one final emission with the full body
        size after the PUT succeeds. The callback contract is delta-
        based (bytes-since-last-call).
        """
        if file_id:
            dest_rel = file_id
        else:
            parent, filename = self.resolve_path(rel_path, root_folder_id)
            dest_rel = f"{parent.rstrip('/')}/{filename}"

        url = self._url(dest_rel)
        bucket = get_throttle(getattr(self.config, "max_upload_kbps", None))
        threshold = int(getattr(
            self.config, "webdav_streaming_threshold_bytes", 4 * 1024 * 1024,
        ))
        try:
            file_size = Path(local_path).stat().st_size
        except OSError:
            file_size = 0

        if file_size and file_size >= threshold:
            # Streaming PUT — read the file lazily one block at a time.
            chunk_size = self._STREAM_CHUNK_BYTES
            with open(local_path, "rb") as f:
                def _gen():
                    while True:
                        block = f.read(chunk_size)
                        if not block:
                            break
                        # Pace the wire per-block so the long-run rate
                        # stays honest across multi-megabyte uploads.
                        bucket.consume(len(block))
                        if progress_callback is not None:
                            progress_callback(len(block))
                        yield block
                resp = self.session.put(
                    url,
                    data=_gen(),
                    headers={"Content-Length": str(file_size)},
                )
        else:
            # Simple path — small files, single PUT, single throttle.
            with open(local_path, "rb") as f:
                body = f.read()
            bucket.consume(len(body))
            resp = self.session.put(url, data=body)
            if progress_callback is not None and body:
                progress_callback(len(body))
        resp.raise_for_status()
        return dest_rel

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bytes:
        """Download file by relative path via GET. Enforces
        MAX_DOWNLOAD_BYTES so a compromised remote or chunked-lying
        response can't OOM us.

        progress_callback: optional `Callable[[int], None]`. Invoked with
        delta bytes per `iter_content` chunk for live progress.
        """
        url = self._url(file_id)
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
                    f"Refusing WebDAV download of {file_id!r}: "
                    f"Content-Length {size} exceeds MAX_DOWNLOAD_BYTES "
                    f"({self.MAX_DOWNLOAD_BYTES})."
                )
        # Streaming accumulation with cap.
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > self.MAX_DOWNLOAD_BYTES:
                resp.close()
                raise RuntimeError(
                    f"WebDAV download of {file_id!r} streamed past "
                    f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting."
                )
            if progress_callback is not None:
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
        if file_id:
            dest_rel = file_id
        else:
            dest_rel = f"{folder_id.rstrip('/')}/{name}"

        url = self._url(dest_rel)
        resp = self.session.put(url, data=content, headers={"Content-Type": mimetype})
        resp.raise_for_status()
        return dest_rel

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Check if a file/folder exists. Returns relative path or None."""
        file_rel = f"{folder_id.rstrip('/')}/{name}"
        url = self._url(file_rel)
        try:
            resp = self.session.request("PROPFIND", url, headers={"Depth": "0"})
            if resp.status_code < 400:
                return file_rel
        except requests.RequestException:
            pass
        return None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        """Server-side COPY."""
        source_url = self._url(source_file_id)
        dest_rel = f"{dest_folder_id.rstrip('/')}/{name}"
        dest_url = self._url(dest_rel)

        resp = self.session.request(
            "COPY", source_url,
            headers={"Destination": dest_url, "Overwrite": "T"},
        )
        resp.raise_for_status()
        return dest_rel

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Return the etag (or OwnCloud checksum) for a file."""
        url = self._url(file_id)
        try:
            root = self._propfind(url, depth="0", extra_props=["<oc:checksums/>"])
            for response in root.findall(_dav_tag("response")):
                checksum = self._get_oc_checksum(response)
                if checksum:
                    return checksum
                etag = self._get_etag(response)
                if etag:
                    return etag
        except requests.HTTPError:
            pass
        return None

    def delete_file(self, file_id: str) -> None:
        """Delete a file/folder via DELETE."""
        url = self._url(file_id)
        resp = self.session.delete(url)
        resp.raise_for_status()
