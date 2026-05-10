"""Per-backend smoke tests for S3Backend.

boto3's S3 client surface is mocked via unittest.mock — a tiny in-memory
``FakeS3`` (a dict[key, bytes]) backs the methods the backend actually
calls (head_bucket, head_object, list_objects_v2 paginator, put_object,
get_object, copy_object, delete_object, upload_file). The tests exercise
the real backend code paths against the offline fake; no network, no
``moto``, no real AWS credentials.

All tests stay <100ms and offline.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

boto3 = pytest.importorskip("boto3")
botocore = pytest.importorskip("botocore")

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
    ConnectTimeoutError,
)

from claude_mirror.backends import BackendError, ErrorClass
from claude_mirror.backends.s3 import S3Backend, _MULTIPART_THRESHOLD

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Fake S3 client (in-memory) ────────────────────────────────────────────────


class _FakeStreamBody:
    """Stand-in for the StreamingBody returned by get_object."""

    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n) if n != -1 else self._buf.read()


class FakeS3:
    """In-memory S3 mock — only the surface S3Backend touches."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.buckets: set[str] = {"mybucket"}
        self.region: str = "us-east-1"
        # Call recording (used by a few tests).
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Failure injection (per-method).
        self.head_bucket_exc: Optional[BaseException] = None
        self.list_exc: Optional[BaseException] = None
        self.put_exc: Optional[BaseException] = None
        self.get_exc: Optional[BaseException] = None
        self.upload_exc: Optional[BaseException] = None

    def head_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head_bucket", kwargs))
        if self.head_bucket_exc is not None:
            raise self.head_bucket_exc
        bucket = kwargs.get("Bucket", "")
        if bucket not in self.buckets:
            raise _make_client_error("NoSuchBucket", 404)
        return {
            "ResponseMetadata": {
                "HTTPStatusCode": 200,
                "HTTPHeaders": {"x-amz-bucket-region": self.region},
            },
            "BucketRegion": self.region,
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("head_object", kwargs))
        key = kwargs.get("Key", "")
        if key not in self.objects:
            raise _make_client_error("NoSuchKey", 404)
        data = self.objects[key]
        import hashlib
        etag = '"' + hashlib.md5(data).hexdigest() + '"'
        return {
            "ETag": etag,
            "ContentLength": len(data),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        if self.put_exc is not None:
            raise self.put_exc
        key = kwargs["Key"]
        body = kwargs.get("Body", b"")
        if hasattr(body, "read"):
            body = body.read()
        self.objects[key] = body
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        if self.get_exc is not None:
            raise self.get_exc
        key = kwargs["Key"]
        if key not in self.objects:
            raise _make_client_error("NoSuchKey", 404)
        data = self.objects[key]
        return {
            "Body": _FakeStreamBody(data),
            "ContentLength": len(data),
            "ResponseMetadata": {"HTTPStatusCode": 200},
        }

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("copy_object", kwargs))
        src = kwargs["CopySource"]
        if isinstance(src, dict):
            src_key = src["Key"]
        else:
            src_key = str(src).split("/", 1)[-1]
        if src_key not in self.objects:
            raise _make_client_error("NoSuchKey", 404)
        self.objects[kwargs["Key"]] = self.objects[src_key]
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs))
        self.objects.pop(kwargs["Key"], None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    def upload_file(
        self,
        Filename: str,
        Bucket: str,
        Key: str,
        Config: Any = None,
        Callback: Any = None,
    ) -> None:
        self.calls.append((
            "upload_file",
            {
                "Filename": Filename,
                "Bucket": Bucket,
                "Key": Key,
                "multipart_threshold": getattr(Config, "multipart_threshold", None),
            },
        ))
        if self.upload_exc is not None:
            raise self.upload_exc
        with open(Filename, "rb") as f:
            data = f.read()
        self.objects[Key] = data
        # Mirror boto3's Callback contract: invoke with delta bytes.
        if Callback is not None and data:
            Callback(len(data))

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", kwargs))
        if self.list_exc is not None:
            raise self.list_exc
        prefix = kwargs.get("Prefix", "")
        delimiter = kwargs.get("Delimiter", "")
        max_keys = int(kwargs.get("MaxKeys", 1000))
        contents = []
        common_prefixes: list[dict[str, str]] = []
        seen_prefixes: set[str] = set()
        import hashlib
        for key, data in sorted(self.objects.items()):
            if not key.startswith(prefix):
                continue
            if delimiter:
                rest = key[len(prefix):]
                if delimiter in rest:
                    sub = rest.split(delimiter, 1)[0]
                    sub_full = prefix + sub + delimiter
                    if sub_full not in seen_prefixes:
                        seen_prefixes.add(sub_full)
                        common_prefixes.append({"Prefix": sub_full})
                    continue
            etag = '"' + hashlib.md5(data).hexdigest() + '"'
            contents.append({
                "Key": key,
                "Size": len(data),
                "ETag": etag,
            })
            if len(contents) >= max_keys:
                break
        return {
            "Contents": contents,
            "CommonPrefixes": common_prefixes,
            "IsTruncated": False,
        }

    def get_paginator(self, op_name: str) -> "_FakePaginator":
        return _FakePaginator(self, op_name)


class _FakePaginator:
    def __init__(self, client: FakeS3, op_name: str) -> None:
        self._client = client
        self._op = op_name

    def paginate(self, **kwargs: Any) -> Any:
        # Paginate by issuing successive list_objects_v2 calls with
        # ContinuationToken cycles. Our FakeS3 doesn't implement pagination
        # natively — for the multi-page test the test patches
        # `paginate` directly to yield multiple pages.
        page = self._client.list_objects_v2(**kwargs)
        yield page


def _make_client_error(code: str, status: int) -> ClientError:
    """Construct a botocore ClientError with the given Code + HTTP status."""
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "FakeOp",
    )


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _make_backend(make_config, config_dir: Path, **overrides) -> S3Backend:
    cfg = make_config(
        backend="s3",
        s3_bucket=overrides.pop("s3_bucket", "mybucket"),
        s3_region=overrides.pop("s3_region", "us-east-1"),
        s3_access_key_id=overrides.pop("s3_access_key_id", "AKIA-FAKE"),
        s3_secret_access_key=overrides.pop("s3_secret_access_key", "secret-fake"),
        s3_endpoint_url=overrides.pop("s3_endpoint_url", ""),
        s3_prefix=overrides.pop("s3_prefix", "myproject"),
        s3_use_path_style=overrides.pop("s3_use_path_style", False),
        token_file=str(config_dir / "token.json"),
        **overrides,
    )
    return S3Backend(cfg)


def _wire_fake(backend: S3Backend, server: Optional[FakeS3] = None) -> FakeS3:
    """Bypass _get_client by stuffing a FakeS3 onto the backend."""
    if server is None:
        server = FakeS3()
    backend._client = server
    return server


# ─── 1. authenticate happy path writes token + verified marker ─────────────────


def test_authenticate_succeeds_and_writes_token(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)

    backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    data = json.loads(token_path.read_text())
    assert "verified_at" in data
    assert data["bucket"] == "mybucket"


# ─── 2. authenticate raises BackendError on auth-class ClientError ─────────────


def test_authenticate_raises_BackendError_on_invalid_credentials(
    make_config, config_dir,
):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.head_bucket_exc = _make_client_error("InvalidAccessKeyId", 403)

    with pytest.raises(BackendError) as exc_info:
        backend.authenticate()
    assert exc_info.value.error_class == ErrorClass.AUTH


# ─── 3. authenticate fails when bucket missing ─────────────────────────────────


def test_authenticate_raises_BackendError_when_bucket_missing(
    make_config, config_dir,
):
    backend = _make_backend(make_config, config_dir, s3_bucket="nope")
    server = _wire_fake(backend)
    # Bucket "nope" not in server.buckets, so head_bucket raises NoSuchBucket.

    with pytest.raises(BackendError) as exc_info:
        backend.authenticate()
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


# ─── 4. authenticate fails fast when bucket name is empty ──────────────────────


def test_authenticate_raises_when_bucket_unconfigured(
    make_config, config_dir,
):
    backend = _make_backend(make_config, config_dir, s3_bucket="")
    with pytest.raises(BackendError) as exc_info:
        backend.authenticate()
    assert exc_info.value.error_class == ErrorClass.AUTH


# ─── 5. get_credentials raises if no token file ────────────────────────────────


def test_get_credentials_raises_when_token_missing(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert not Path(backend.config.token_file).exists()
    with pytest.raises(RuntimeError, match="not authenticated"):
        backend.get_credentials()


# ─── 6. get_or_create_folder synthesizes prefix ────────────────────────────────


def test_get_or_create_folder_returns_synthesized_prefix(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    out = backend.get_or_create_folder("subdir", "myproject/")
    assert out == "myproject/subdir/"


# ─── 7. resolve_path walks parts ───────────────────────────────────────────────


def test_resolve_path_returns_parent_and_basename(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    parent, basename = backend.resolve_path("a/b/c.md", "myproject/")
    assert parent == "myproject/a/b/"
    assert basename == "c.md"


def test_resolve_path_root_level_file(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    parent, basename = backend.resolve_path("c.md", "myproject/")
    assert parent == "myproject/"
    assert basename == "c.md"


# ─── 8. upload_file → put_object via boto3 (small) ─────────────────────────────


def test_upload_file_round_trips_small_payload(
    make_config, config_dir, project_dir,
):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    local = project_dir / "note.md"
    local.write_text("hi")

    file_id = backend.upload_file(str(local), "note.md", "myproject/")
    assert file_id == "myproject/note.md"
    assert server.objects["myproject/note.md"] == b"hi"


# ─── 9. upload_file invokes multipart-threshold config ─────────────────────────


def test_upload_file_passes_multipart_threshold_config(
    make_config, config_dir, project_dir,
):
    """The TransferConfig handed to upload_file must carry our 5MiB threshold
    so multipart kicks in at the documented boundary."""
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    local = project_dir / "small.bin"
    local.write_bytes(b"x" * 1024)

    backend.upload_file(str(local), "small.bin", "myproject/")
    upload_calls = [c for c in server.calls if c[0] == "upload_file"]
    assert len(upload_calls) == 1
    assert upload_calls[0][1]["multipart_threshold"] == _MULTIPART_THRESHOLD


# ─── 10. upload_file multipart code path triggered above 5MiB ──────────────────


def test_upload_file_above_multipart_threshold_uses_same_api(
    make_config, config_dir, project_dir,
):
    """boto3's upload_file orchestrates multipart internally — the test
    proves we DO cross the documented threshold AND that the same
    upload_file API is invoked (the multipart-vs-single decision is
    boto3-internal). The TransferConfig threshold is set to our constant
    so a real boto3 against real S3 would multipart-upload here."""
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    local = project_dir / "big.bin"
    local.write_bytes(b"y" * (_MULTIPART_THRESHOLD + 1024))

    backend.upload_file(str(local), "big.bin", "myproject/")
    assert len(server.objects["myproject/big.bin"]) > _MULTIPART_THRESHOLD


# ─── 11. upload_bytes round-trip ───────────────────────────────────────────────


def test_upload_bytes_round_trip(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    fid = backend.upload_bytes(b'{"a":1}', "manifest.json", "myproject/")
    assert fid == "myproject/manifest.json"
    assert server.objects["myproject/manifest.json"] == b'{"a":1}'


# ─── 12. download_file streams payload ─────────────────────────────────────────


def test_download_file_returns_bytes(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/x.md"] = b"content-for-download"

    out = backend.download_file("myproject/x.md")
    assert out == b"content-for-download"


# ─── 13. download_file aborts past size cap ────────────────────────────────────


def test_download_file_aborts_on_size_cap(make_config, config_dir, monkeypatch):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    monkeypatch.setattr(S3Backend, "MAX_DOWNLOAD_BYTES", 1024)
    server.objects["myproject/huge.bin"] = b"z" * (4 * 1024)

    with pytest.raises(BackendError) as exc_info:
        backend.download_file("myproject/huge.bin")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


# ─── 14. download_file pre-flight rejects oversized Content-Length ─────────────


def test_download_file_aborts_on_content_length_preflight(
    make_config, config_dir, monkeypatch,
):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    # Populate with a small body but lie about the size in the response.
    server.objects["myproject/big.bin"] = b"abc"
    monkeypatch.setattr(S3Backend, "MAX_DOWNLOAD_BYTES", 10)

    real_get = server.get_object

    def lying_get(**kwargs):
        resp = real_get(**kwargs)
        resp["ContentLength"] = 99999
        return resp

    server.get_object = lying_get  # type: ignore[assignment]
    with pytest.raises(BackendError) as exc_info:
        backend.download_file("myproject/big.bin")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


# ─── 15. list_files_recursive returns key dicts ────────────────────────────────


def test_list_files_recursive_returns_key_dicts(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/a.md"] = b"alpha"
    server.objects["myproject/sub/b.md"] = b"beta"

    results = backend.list_files_recursive("myproject/")
    by_rel = {r["relative_path"]: r for r in results}
    assert "a.md" in by_rel
    assert "sub/b.md" in by_rel
    assert by_rel["a.md"]["id"] == "myproject/a.md"
    assert by_rel["a.md"]["size"] == 5


# ─── 16. list_files_recursive honours exclude_folder_names ─────────────────────


def test_list_files_recursive_honors_exclude_folder_names(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/keep.md"] = b"k"
    server.objects["myproject/_claude_mirror_snapshots/snap.md"] = b"s"

    results = backend.list_files_recursive(
        "myproject/", exclude_folder_names={"_claude_mirror_snapshots"},
    )
    rels = {r["relative_path"] for r in results}
    assert "keep.md" in rels
    assert not any("_claude_mirror_snapshots" in r for r in rels)


# ─── 17. list_files_recursive supports multi-page pagination ───────────────────


def test_list_files_recursive_handles_multiple_pages(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)

    page1 = {
        "Contents": [
            {"Key": "myproject/a.md", "Size": 1, "ETag": '"e1"'},
            {"Key": "myproject/b.md", "Size": 1, "ETag": '"e2"'},
        ],
    }
    page2 = {
        "Contents": [
            {"Key": "myproject/c.md", "Size": 1, "ETag": '"e3"'},
        ],
    }

    fake_paginator = MagicMock()
    fake_paginator.paginate = MagicMock(return_value=iter([page1, page2]))
    server.get_paginator = MagicMock(return_value=fake_paginator)  # type: ignore[assignment]

    results = backend.list_files_recursive("myproject/")
    rels = sorted(r["relative_path"] for r in results)
    assert rels == ["a.md", "b.md", "c.md"]


# ─── 18. list_files_recursive on empty bucket ──────────────────────────────────


def test_list_files_recursive_empty(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)
    assert backend.list_files_recursive("myproject/") == []


# ─── 19. list_folders surfaces CommonPrefixes ──────────────────────────────────


def test_list_folders_returns_common_prefixes(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/sub1/x.md"] = b"a"
    server.objects["myproject/sub2/y.md"] = b"b"

    out = backend.list_folders("myproject/")
    names = sorted(d["name"] for d in out)
    assert names == ["sub1", "sub2"]


# ─── 20. get_file_id returns key when present, None when missing ───────────────


def test_get_file_id_present(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/x.md"] = b"hi"
    assert backend.get_file_id("x.md", "myproject/") == "myproject/x.md"


def test_get_file_id_absent_returns_none(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)
    assert backend.get_file_id("missing.md", "myproject/") is None


# ─── 21. copy_file does server-side copy_object ────────────────────────────────


def test_copy_file_uses_copy_object(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/src.md"] = b"contents"

    out = backend.copy_file("myproject/src.md", "myproject/dest/", "dst.md")
    assert out == "myproject/dest/dst.md"
    assert server.objects["myproject/dest/dst.md"] == b"contents"
    assert any(c[0] == "copy_object" for c in server.calls)


# ─── 22. get_file_hash returns the ETag ────────────────────────────────────────


def test_get_file_hash_returns_etag(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/x.md"] = b"hello"

    h = backend.get_file_hash("myproject/x.md")
    import hashlib
    assert h == hashlib.md5(b"hello").hexdigest()


def test_get_file_hash_returns_none_for_missing(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    _wire_fake(backend)
    assert backend.get_file_hash("myproject/missing") is None


# ─── 23. delete_file calls delete_object ───────────────────────────────────────


def test_delete_file_removes_object(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    server = _wire_fake(backend)
    server.objects["myproject/x.md"] = b"x"
    backend.delete_file("myproject/x.md")
    assert "myproject/x.md" not in server.objects


# ─── 24. classify_error mapping table ──────────────────────────────────────────


def test_classify_error_no_credentials_is_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(NoCredentialsError()) == ErrorClass.AUTH


def test_classify_error_invalid_access_key_is_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("InvalidAccessKeyId", 403)
    assert backend.classify_error(err) == ErrorClass.AUTH


def test_classify_error_signature_does_not_match_is_AUTH(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("SignatureDoesNotMatch", 403)
    assert backend.classify_error(err) == ErrorClass.AUTH


def test_classify_error_no_such_bucket_is_FILE_REJECTED(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("NoSuchBucket", 404)
    assert backend.classify_error(err) == ErrorClass.FILE_REJECTED


def test_classify_error_slow_down_is_RATE_LIMIT_GLOBAL(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("SlowDown", 503)
    assert backend.classify_error(err) == ErrorClass.RATE_LIMIT_GLOBAL


def test_classify_error_429_is_RATE_LIMIT_GLOBAL(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("TooManyRequests", 429)
    assert backend.classify_error(err) == ErrorClass.RATE_LIMIT_GLOBAL


def test_classify_error_5xx_is_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("InternalError", 503)
    assert backend.classify_error(err) == ErrorClass.TRANSIENT


def test_classify_error_413_is_FILE_REJECTED(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = _make_client_error("EntityTooLarge", 413)
    assert backend.classify_error(err) == ErrorClass.FILE_REJECTED


def test_classify_error_endpoint_connection_is_TRANSIENT(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    err = EndpointConnectionError(endpoint_url="https://nowhere.example")
    assert backend.classify_error(err) == ErrorClass.TRANSIENT


def test_classify_error_default_is_UNKNOWN(make_config, config_dir):
    backend = _make_backend(make_config, config_dir)
    assert backend.classify_error(RuntimeError("???")) == ErrorClass.UNKNOWN


# ─── 25. path-style addressing flag flows into BotoConfig ──────────────────────


def test_path_style_flag_sets_addressing_style_path(make_config, config_dir):
    """Verify the path-style toggle propagates into the boto3 client's
    config — important for MinIO + several S3-compat services."""
    captured: dict[str, Any] = {}

    real_client = MagicMock()

    def fake_boto_client(service: str, **kwargs: Any) -> Any:
        captured["service"] = service
        captured["kwargs"] = kwargs
        return real_client

    backend = _make_backend(make_config, config_dir, s3_use_path_style=True)
    with patch.object(boto3, "client", side_effect=fake_boto_client):
        backend._get_client()

    assert captured["service"] == "s3"
    boto_cfg = captured["kwargs"]["config"]
    addr = getattr(boto_cfg, "s3", {}).get("addressing_style", "")
    assert addr == "path"


def test_no_path_style_default_addressing_is_auto(make_config, config_dir):
    captured: dict[str, Any] = {}

    real_client = MagicMock()

    def fake_boto_client(service: str, **kwargs: Any) -> Any:
        captured["service"] = service
        captured["kwargs"] = kwargs
        return real_client

    backend = _make_backend(make_config, config_dir, s3_use_path_style=False)
    with patch.object(boto3, "client", side_effect=fake_boto_client):
        backend._get_client()
    boto_cfg = captured["kwargs"]["config"]
    addr = getattr(boto_cfg, "s3", {}).get("addressing_style", "")
    assert addr == "auto"


# ─── Server-returned key validation (H6) ───────────────────────────────────────

# S3 has no path constraints — every key is just a string. A bucket may
# legitimately contain a key like ``../escape`` (a previous tenant's
# misconfigured uploader, or a deliberately hostile setup). The
# downstream sync engine relies on `_safe_join` to convert keys into
# local paths. To defend against a future caller that bypasses the
# safe-join, the backend rejects the suspicious key shapes at the
# listing boundary.


class _FakePaginatorWithKeys:
    """Like _FakePaginator but lets the test inject arbitrary key shapes."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys

    def paginate(self, **kwargs: Any) -> Any:
        contents = [
            {"Key": k, "Size": 1, "ETag": '"x"'} for k in self._keys
        ]
        yield {"Contents": contents, "CommonPrefixes": []}


def _wire_with_keys(
    backend: S3Backend, keys: list[str],
) -> None:
    """Bypass _get_client with a tiny mock that returns the given keys
    for any list_objects_v2 paginator call."""
    client = MagicMock()
    client.get_paginator.return_value = _FakePaginatorWithKeys(keys)
    backend._client = client


def test_list_rejects_parent_directory_traversal_key(make_config, config_dir):
    """A bucket key like `myproject/../../etc/passwd` MUST be rejected
    at the backend boundary."""
    backend = _make_backend(make_config, config_dir, s3_prefix="myproject")
    _wire_with_keys(backend, ["myproject/../../etc/passwd"])
    with pytest.raises(BackendError) as exc_info:
        backend.list_files_recursive("myproject/")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


def test_list_rejects_nul_byte_in_key(make_config, config_dir):
    """A NUL byte in an S3 key would let some downstream path code
    truncate the rest — reject at the boundary."""
    backend = _make_backend(make_config, config_dir, s3_prefix="myproject")
    _wire_with_keys(backend, ["myproject/foo\x00bar.md"])
    with pytest.raises(BackendError) as exc_info:
        backend.list_files_recursive("myproject/")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


def test_list_rejects_windows_drive_prefix(make_config, config_dir):
    """A Windows drive-letter prefix would slip past PurePath joins on
    that platform — reject server-returned keys carrying one."""
    backend = _make_backend(make_config, config_dir, s3_prefix="myproject")
    # Strip-prefix gives `C:Windows/system32/config` — the C: prefix
    # triggers the drive-letter rejection.
    _wire_with_keys(backend, ["myproject/C:Windows/system32/config"])
    with pytest.raises(BackendError) as exc_info:
        backend.list_files_recursive("myproject/")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


def test_list_accepts_normal_keys(make_config, config_dir):
    """Sanity check: a normal key shape is NOT rejected."""
    backend = _make_backend(make_config, config_dir, s3_prefix="myproject")
    _wire_with_keys(
        backend,
        ["myproject/CLAUDE.md", "myproject/memory/notes.md"],
    )
    files = backend.list_files_recursive("myproject/")
    rels = sorted(f["relative_path"] for f in files)
    assert rels == ["CLAUDE.md", "memory/notes.md"]
