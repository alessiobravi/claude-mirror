"""StorageBackend implementation for S3-compatible object storage.

One implementation transparently supports AWS S3, Cloudflare R2, Backblaze B2
(via S3 API), Wasabi, MinIO, Tigris, IDrive E2, Linode Object Storage,
DigitalOcean Spaces, Storj, Hetzner Storage Box, and any other service
exposing the S3 API. The differentiator between providers is just the
``s3_endpoint_url`` field on Config — leave it ``None`` for AWS proper, set
it to the provider's S3 endpoint otherwise.

S3 has no concept of folders: every key is a flat string. We synthesize
folder semantics by treating the configured ``s3_prefix`` (defaulting to
the project name) as the root, and using ``/``-delimited keys underneath.
``get_or_create_folder`` returns a prefix with a trailing slash; we don't
PUT a zero-byte placeholder because every keyspace listing uses
``Prefix=`` + ``Delimiter='/'`` semantics anyway.

boto3 is lazy-imported (function-local) per the v0.5.61 fusepy precedent —
the SDK's import dance walks tens of submodules and probes its own data
files, which would otherwise lengthen import latency for users who never
touch S3. Importing it inside ``_get_client`` (and inside
``classify_error``) means non-S3 invocations of ``claude-mirror`` pay zero
cost. Once a single S3Backend instance has been constructed and
authenticated, the boto3 client is cached on the instance.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import Config
from . import BackendError, ErrorClass, StorageBackend
from ._util import write_token_secure


# Multipart-upload threshold. boto3's TransferConfig defaults to 8 MiB; we
# pick 5 MiB so the threshold matches S3's smallest legal multipart-part
# size and the upload code path is exercised at a predictable boundary.
_MULTIPART_THRESHOLD: int = 5 * 1024 * 1024

# Streaming download chunk size. Large enough to amortise per-call
# overhead, small enough that the size cap fires before a runaway response
# can blow out client memory.
_DOWNLOAD_CHUNK: int = 64 * 1024

# Sentinel key used by the doctor's deep checks to verify write
# permissions. The full key is built as ``prefix + _DOCTOR_TEST_KEY`` and
# removed immediately after a successful PUT.
_DOCTOR_TEST_KEY: str = "__claude_mirror_doctor_test"

# AWS S3 bucket names: 3-63 chars, lower-case letters / digits / hyphens,
# must start + end with letter or digit. Many S3-compat services accept
# wider names, so we use this only as a hint in the wizard, not as a hard
# rejection in the backend.
_BUCKET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")


class S3Backend(StorageBackend):
    """StorageBackend over the S3 API. Works with any S3-compatible service."""

    backend_name = "s3"

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: Optional[Any] = None
        # Resolve the prefix once: configured value wins, else project name.
        # Always normalised to end with exactly one trailing "/" so prefix
        # arithmetic ("prefix + rel_path") never double-slashes or skips a
        # separator.
        raw_prefix = (getattr(config, "s3_prefix", "") or "").strip().strip("/")
        if not raw_prefix:
            raw_prefix = Path(config.project_path).name or "claude-mirror"
        self._prefix: str = raw_prefix + "/"

    # ------------------------------------------------------------------
    # Boto3 client construction
    # ------------------------------------------------------------------

    def _get_client(self) -> Any:
        """Build (or return the cached) boto3 S3 client.

        The client is constructed against the configured endpoint /
        region / access keys. When the access key fields are blank,
        boto3's default credential chain (env vars, ~/.aws/credentials,
        instance metadata) is used — letting users opt into IAM roles /
        env-var creds without rewriting the project YAML.
        """
        if self._client is not None:
            return self._client

        import boto3  # noqa: PLC0415
        from botocore.config import Config as BotoConfig  # noqa: PLC0415

        endpoint = getattr(self.config, "s3_endpoint_url", "") or None
        region = getattr(self.config, "s3_region", "") or None
        access_key = getattr(self.config, "s3_access_key_id", "") or None
        secret_key = getattr(self.config, "s3_secret_access_key", "") or None
        path_style = bool(getattr(self.config, "s3_use_path_style", False))

        boto_cfg = BotoConfig(
            signature_version="s3v4",
            s3={"addressing_style": "path" if path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        )

        kwargs: dict[str, Any] = {"config": boto_cfg}
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        if region:
            kwargs["region_name"] = region
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key

        self._client = boto3.client("s3", **kwargs)
        return self._client

    # ------------------------------------------------------------------
    # Error classification
    # ------------------------------------------------------------------

    def classify_error(self, exc: BaseException) -> ErrorClass:
        """Map botocore + boto3 exceptions onto the project's ErrorClass.

        botocore wraps every API failure in ClientError (a generic shape
        carrying the wire-level error code + HTTP status); we branch on
        those. Network-layer / credential-resolution failures surface as
        their own typed exceptions.
        """
        try:
            from botocore.exceptions import (  # noqa: PLC0415
                ClientError,
                ConnectTimeoutError,
                EndpointConnectionError,
                NoCredentialsError,
                ReadTimeoutError,
            )
        except ImportError:
            return ErrorClass.UNKNOWN

        if isinstance(exc, NoCredentialsError):
            return ErrorClass.AUTH

        if isinstance(
            exc,
            (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError),
        ):
            return ErrorClass.TRANSIENT

        if isinstance(exc, ClientError):
            err = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
            code = str(err.get("Code", "") or "")
            status = 0
            try:
                status = int(
                    exc.response.get("ResponseMetadata", {}).get(
                        "HTTPStatusCode", 0
                    )
                )
            except (TypeError, ValueError, AttributeError):
                status = 0

            if code in (
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "AccessDenied",
                "ExpiredToken",
                "InvalidToken",
            ):
                return ErrorClass.AUTH
            if code in ("NoSuchBucket", "NoSuchKey"):
                return ErrorClass.FILE_REJECTED
            if code in ("SlowDown", "RequestLimitExceeded") or status == 429:
                return ErrorClass.RATE_LIMIT_GLOBAL
            if status == 413 or code == "EntityTooLarge":
                return ErrorClass.FILE_REJECTED
            if status >= 500:
                return ErrorClass.TRANSIENT
            if status == 403:
                return ErrorClass.PERMISSION
            if status == 404:
                return ErrorClass.FILE_REJECTED

        return ErrorClass.UNKNOWN

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def authenticate(self) -> Any:
        """Verify credentials by HEAD'ing the configured bucket.

        S3 has no interactive auth flow; "authenticate" here means "prove
        the static creds work end-to-end." A successful ``head_bucket``
        confirms the keys reach the bucket, the bucket exists, and the
        principal has at least minimal read access.

        Token file persistence is informational only: it records that
        we've verified creds against this bucket (no secrets written),
        mirroring SFTP / WebDAV's "verified_at" marker.
        """
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        if not bucket:
            raise BackendError(
                ErrorClass.AUTH,
                "s3_bucket is not configured",
                backend_name=self.backend_name,
            )

        client = self._get_client()
        try:
            client.head_bucket(Bucket=bucket)
        except BaseException as exc:  # noqa: BLE001 — re-classified
            klass = self.classify_error(exc)
            raise BackendError(
                klass,
                f"S3 head_bucket on {bucket!r} failed: {exc}",
                backend_name=self.backend_name,
                cause=exc,
            ) from None

        token_path = Path(self.config.token_file)
        write_token_secure(
            token_path,
            json.dumps({
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "bucket": bucket,
                "endpoint_url": getattr(self.config, "s3_endpoint_url", "") or "",
            }),
        )
        return client

    def get_credentials(self) -> Any:
        """Return the live boto3 S3 client.

        Unlike OAuth backends there is no cached refresh token to load —
        the static keys live on Config. We still gate on the token file
        so ``claude-mirror auth`` is required at least once before any
        sync operation, matching the user-visible flow of every other
        backend.
        """
        token_path = Path(self.config.token_file)
        if not token_path.exists():
            raise RuntimeError(
                "S3 backend not authenticated — run claude-mirror auth"
            )
        return self._get_client()

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _full_key(self, rel: str) -> str:
        """Resolve a relative path under the configured prefix.

        Keeps the leading-prefix invariant: the returned key always starts
        with ``self._prefix`` (no leading slash). Empty / ``/`` rel-paths
        return the prefix itself.
        """
        clean = (rel or "").lstrip("/")
        return self._prefix + clean

    @staticmethod
    def _strip_prefix(key: str, prefix: str) -> str:
        if key.startswith(prefix):
            return key[len(prefix):]
        return key

    # ------------------------------------------------------------------
    # Folder operations (synthesized over key prefixes)
    # ------------------------------------------------------------------

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        """Synthesize a folder by appending ``name/`` to ``parent_id``.

        S3 has no real directories; every "folder" is just the longest
        common prefix of the keys underneath it. We don't write a
        zero-byte marker because the recursive listing path uses prefix
        + delimiter semantics directly. Returned value is the new prefix
        (always trailing-slashed).
        """
        parent = (parent_id or self._prefix).rstrip("/") + "/" if parent_id else self._prefix
        if not parent.endswith("/"):
            parent = parent + "/"
        return f"{parent}{name}/"

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        """Split ``rel_path`` into ``(parent_prefix, basename)``.

        Caller passes the project root prefix as ``root_folder_id``. We
        walk ``rel_path``'s parts and join all but the last under the
        root, returning the parent-prefix plus the final filename.
        """
        root = (root_folder_id or self._prefix)
        if not root.endswith("/"):
            root = root + "/"
        parts = [p for p in (rel_path or "").replace("\\", "/").split("/") if p]
        if not parts:
            return root, ""
        if len(parts) == 1:
            return root, parts[0]
        return root + "/".join(parts[:-1]) + "/", parts[-1]

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
        """List every key under ``folder_id`` via paginated ``list_objects_v2``.

        S3 returns up to 1000 keys per page; we use a paginator so a
        bucket with many thousands of keys still produces a complete
        result. ``exclude_folder_names`` is applied client-side because
        the API has no native "exclude this prefix" parameter.
        """
        excluded = exclude_folder_names or set()
        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()

        listing_prefix = (folder_id or self._prefix)
        if not listing_prefix.endswith("/"):
            listing_prefix = listing_prefix + "/"

        paginator = client.get_paginator("list_objects_v2")
        results: list[dict[str, Any]] = []
        files_seen = 0
        pages_seen = 0
        for page in paginator.paginate(Bucket=bucket, Prefix=listing_prefix):
            pages_seen += 1
            for obj in page.get("Contents", []) or []:
                key = obj.get("Key", "") or ""
                if not key or key.endswith("/"):
                    continue
                rel = self._strip_prefix(key, listing_prefix)
                if not rel:
                    continue
                if excluded and any(c in excluded for c in rel.split("/")):
                    continue
                etag = (obj.get("ETag", "") or "").strip('"')
                size = int(obj.get("Size", 0) or 0)
                results.append({
                    "id": key,
                    "name": rel.rsplit("/", 1)[-1],
                    "relative_path": rel,
                    "size": size,
                    "md5Checksum": etag or None,
                })
                files_seen += 1
            if progress_cb:
                progress_cb(pages_seen, files_seen)
        return results

    def list_folders(
        self,
        parent_id: str,
        name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List "subfolders" of ``parent_id`` via the ``CommonPrefixes`` shape.

        Each ``list_objects_v2`` page with ``Delimiter='/'`` returns a
        ``CommonPrefixes`` array whose entries are the unique sub-prefix
        segments — that's S3's only directory abstraction.
        """
        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        listing_prefix = (parent_id or self._prefix)
        if not listing_prefix.endswith("/"):
            listing_prefix = listing_prefix + "/"

        paginator = client.get_paginator("list_objects_v2")
        out: list[dict[str, Any]] = []
        for page in paginator.paginate(
            Bucket=bucket, Prefix=listing_prefix, Delimiter="/",
        ):
            for cp in page.get("CommonPrefixes", []) or []:
                full = cp.get("Prefix", "") or ""
                if not full:
                    continue
                rel = self._strip_prefix(full, listing_prefix).rstrip("/")
                if not rel:
                    continue
                if name is not None and rel != name:
                    continue
                out.append({
                    "id": full,
                    "name": rel,
                    "createdTime": "",
                })
        return out

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
        """Upload a local file. Switches to multipart for files at or
        above ``_MULTIPART_THRESHOLD`` (5 MiB).

        boto3's ``upload_file`` handles the multipart orchestration when
        the source size exceeds the configured threshold; below that, it
        single-PUTs. The progress callback is wired through boto3's
        Callback parameter.
        """
        from boto3.s3.transfer import TransferConfig  # noqa: PLC0415

        if file_id:
            key = file_id
        else:
            parent, basename = self.resolve_path(rel_path, root_folder_id)
            key = parent + basename

        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()

        transfer_cfg = TransferConfig(
            multipart_threshold=_MULTIPART_THRESHOLD,
            multipart_chunksize=_MULTIPART_THRESHOLD,
            use_threads=False,
        )

        # boto3's Callback contract is delta-bytes-per-call (matches the
        # claude-mirror progress contract directly — no bridge needed).
        cb = progress_callback if progress_callback is not None else None
        client.upload_file(
            Filename=local_path,
            Bucket=bucket,
            Key=key,
            Config=transfer_cfg,
            Callback=cb,
        )
        return key

    def download_file(
        self,
        file_id: str,
        progress_callback: Optional[Callable[[int], None]] = None,
    ) -> bytes:
        """Stream-download via ``get_object`` honouring MAX_DOWNLOAD_BYTES.

        We don't trust the Content-Length header alone — a buggy /
        hostile server can lie about size and stream past the cap, so we
        also check accumulated bytes per chunk.
        """
        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        resp = client.get_object(Bucket=bucket, Key=file_id)
        body = resp.get("Body")
        if body is None:
            return b""

        # Pre-flight: if the server advertises a length above the cap,
        # bail before allocating anything.
        try:
            content_length = int(resp.get("ContentLength", 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        if content_length and content_length > self.MAX_DOWNLOAD_BYTES:
            raise BackendError(
                ErrorClass.FILE_REJECTED,
                f"S3 download of {file_id!r} declared "
                f"Content-Length {content_length} > "
                f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting.",
                backend_name=self.backend_name,
            )

        buf = bytearray()
        while True:
            chunk = body.read(_DOWNLOAD_CHUNK)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > self.MAX_DOWNLOAD_BYTES:
                raise BackendError(
                    ErrorClass.FILE_REJECTED,
                    f"S3 download of {file_id!r} streamed past "
                    f"MAX_DOWNLOAD_BYTES ({self.MAX_DOWNLOAD_BYTES}); aborting.",
                    backend_name=self.backend_name,
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
        """PUT raw bytes as a single object."""
        if file_id:
            key = file_id
        else:
            parent = (folder_id or self._prefix)
            if not parent.endswith("/"):
                parent = parent + "/"
            key = parent + name

        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=content,
            ContentType=mimetype,
        )
        return key

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        """Return the full S3 key when ``name`` exists under ``folder_id``.

        We use ``head_object`` rather than ``list_objects`` because the
        cost is one round-trip with a tiny response, and a 404 is a
        first-class signal here (not an error). Auth / network errors
        bubble; only ``NoSuchKey`` / 404 returns None.
        """
        from botocore.exceptions import ClientError  # noqa: PLC0415

        parent = (folder_id or self._prefix)
        if not parent.endswith("/"):
            parent = parent + "/"
        key = parent + name

        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            err = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
            code = str(err.get("Code", "") or "")
            status = 0
            try:
                status = int(
                    exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
                )
            except (TypeError, ValueError, AttributeError):
                status = 0
            if code in ("NoSuchKey", "404", "NotFound") or status == 404:
                return None
            raise
        return key

    def copy_file(
        self,
        source_file_id: str,
        dest_folder_id: str,
        name: str,
    ) -> str:
        """Server-side ``copy_object`` — no bytes move through the client."""
        parent = (dest_folder_id or self._prefix)
        if not parent.endswith("/"):
            parent = parent + "/"
        dest_key = parent + name

        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        client.copy_object(
            Bucket=bucket,
            Key=dest_key,
            CopySource={"Bucket": bucket, "Key": source_file_id},
        )
        return dest_key

    def get_file_hash(self, file_id: str) -> Optional[str]:
        """Return the object's ETag.

        For single-PUT objects, the ETag is the MD5 of the content. For
        multipart-uploaded objects, the ETag is a synthetic hash with a
        ``-N`` suffix (where N = number of parts) — still a stable
        identifier, but NOT comparable to a local MD5. Callers wanting
        cross-format comparison must fall back to size + mtime.
        """
        from botocore.exceptions import ClientError  # noqa: PLC0415

        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        try:
            resp = client.head_object(Bucket=bucket, Key=file_id)
        except ClientError:
            return None
        etag = (resp.get("ETag", "") or "").strip('"')
        return etag or None

    def delete_file(self, file_id: str) -> None:
        """Remove the object at ``file_id`` from the bucket."""
        client = self._get_client()
        bucket = (getattr(self.config, "s3_bucket", "") or "").strip()
        client.delete_object(Bucket=bucket, Key=file_id)
