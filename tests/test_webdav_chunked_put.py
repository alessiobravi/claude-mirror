"""Tests for v0.5.39 WebDAV chunked / streaming PUT for large files.

Goal: verify that files at or above `webdav_streaming_threshold_bytes`
go through a generator-based PUT (peak memory bounded to one block,
NOT the whole file) while small files keep using the simple in-memory
PUT — no regression for typical markdown content.

We rely on `responses` for HTTP-layer mocking. The streaming path
sends a generator as the request body; `responses` reads the
generator in full to compute the captured request body, so we can
assert the round-trip integrity hash matches the source file. The
"peak memory bounded" property is verified structurally — we monkey-
patch `Session.put` to inspect the `data=` argument and confirm it's
an iterator/generator (NOT bytes) for files above the threshold.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import GeneratorType

import pytest
import requests
import responses

from claude_mirror.backends.webdav import WebDAVBackend


def _make_backend(
    make_config, config_dir,
    *, threshold: int = 4 * 1024 * 1024,
    max_kbps=None,
):
    cfg = make_config(
        backend="webdav",
        webdav_url="https://dav.example.com/remote.php/dav/files/u/test",
        webdav_username="alice",
        webdav_password="secret",
        token_file=str(config_dir / "token.json"),
        webdav_streaming_threshold_bytes=threshold,
        max_upload_kbps=max_kbps,
    )
    backend = WebDAVBackend(cfg)
    # Inject a Session so we don't go through authenticate().
    backend._session = requests.Session()
    return backend


# ─── 1. Small file → simple PUT (bytes body) ──────────────────────────────────


@responses.activate
def test_small_file_uses_simple_put_with_bytes_body(make_config, config_dir, project_dir):
    """A 1 KiB file (well under 4 MiB threshold) must continue to use
    the simple in-memory PUT path — single bytes body, no generator."""
    backend = _make_backend(make_config, config_dir)
    base = backend.config.webdav_url
    expected_url = f"{base}/note.md"

    # Capture the data argument passed to session.put.
    seen_bodies: list[object] = []
    real_put = backend._session.put

    def spy_put(url, data=None, **kwargs):
        seen_bodies.append(data)
        return real_put(url, data=data, **kwargs)

    backend._session.put = spy_put
    responses.add(responses.PUT, expected_url, status=201)

    local = project_dir / "note.md"
    local.write_bytes(b"x" * 1024)
    backend.upload_file(str(local), "note.md", "")

    assert len(seen_bodies) == 1
    # Small path passes raw bytes (NOT a generator).
    assert isinstance(seen_bodies[0], (bytes, bytearray))


# ─── 2. Large file → streaming PUT (generator body) ───────────────────────────


def test_large_file_uses_streaming_put_with_generator(make_config, config_dir, project_dir):
    """A file at or above the threshold must use a generator body so
    peak memory stays bounded. Verified by inspecting the `data=`
    argument type — must NOT be `bytes`/`bytearray` (which would mean
    we read the whole file at once)."""
    threshold = 64 * 1024  # 64 KiB so the test stays fast
    backend = _make_backend(make_config, config_dir, threshold=threshold)
    base = backend.config.webdav_url
    expected_url = f"{base}/big.bin"

    sent_chunks: list[bytes] = []
    seen_data_type = []

    class _FakeResp:
        status_code = 201

        def raise_for_status(self):
            pass

    def fake_put(url, data=None, headers=None, **kwargs):
        seen_data_type.append(type(data).__name__)
        # Drain the generator to simulate the wire send. We capture
        # each chunk so we can rebuild the body and verify integrity.
        if hasattr(data, "__iter__") and not isinstance(data, (bytes, bytearray, str)):
            for chunk in data:
                sent_chunks.append(chunk)
        else:
            sent_chunks.append(data)
        return _FakeResp()

    backend._session.put = fake_put

    local = project_dir / "big.bin"
    payload = b"".join(bytes([i % 256]) * 4096 for i in range(40))  # ≈ 160 KiB
    local.write_bytes(payload)
    backend.upload_file(str(local), "big.bin", "")

    # The data argument must NOT be bytes/bytearray on the streaming
    # path — it must be a generator/iterator.
    assert seen_data_type == ["generator"] or "generator" in seen_data_type[0].lower()
    # Round-trip integrity: concatenated chunks equal the original file.
    assembled = b"".join(sent_chunks)
    assert assembled == payload
    src_hash = hashlib.sha256(payload).hexdigest()
    sent_hash = hashlib.sha256(assembled).hexdigest()
    assert src_hash == sent_hash


# ─── 3. Streaming PUT sends explicit Content-Length ───────────────────────────


def test_streaming_put_sets_content_length_header(make_config, config_dir, project_dir):
    """The streaming PUT path must set `Content-Length` explicitly —
    without it, `requests` falls back to chunked transfer-encoding,
    which some WebDAV servers (Apache mod_dav with default config)
    reject."""
    threshold = 32 * 1024
    backend = _make_backend(make_config, config_dir, threshold=threshold)
    base = backend.config.webdav_url

    captured_headers: list[dict] = []

    class _FakeResp:
        status_code = 201

        def raise_for_status(self):
            pass

    def fake_put(url, data=None, headers=None, **kwargs):
        captured_headers.append(headers or {})
        # Drain generator so the test mirrors a real send.
        if hasattr(data, "__iter__") and not isinstance(data, (bytes, bytearray, str)):
            for _ in data:
                pass
        return _FakeResp()

    backend._session.put = fake_put

    local = project_dir / "big.bin"
    local.write_bytes(b"y" * (threshold + 1024))
    backend.upload_file(str(local), "big.bin", "")

    assert captured_headers
    cl = captured_headers[0].get("Content-Length")
    assert cl == str(threshold + 1024)


# ─── 4. Threshold field overrides default ─────────────────────────────────────


def test_threshold_field_routes_path_at_boundary(make_config, config_dir, project_dir):
    """File size == threshold → streaming path. File size < threshold →
    simple path. The boundary check is `>=` so a file exactly at the
    cap streams."""
    threshold = 16 * 1024
    backend = _make_backend(make_config, config_dir, threshold=threshold)

    seen_types: list[str] = []

    class _FakeResp:
        status_code = 201

        def raise_for_status(self):
            pass

    def fake_put(url, data=None, headers=None, **kwargs):
        seen_types.append(type(data).__name__)
        if hasattr(data, "__iter__") and not isinstance(data, (bytes, bytearray, str)):
            for _ in data:
                pass
        return _FakeResp()

    backend._session.put = fake_put

    # Below threshold.
    small = project_dir / "small.bin"
    small.write_bytes(b"a" * (threshold - 1))
    backend.upload_file(str(small), "small.bin", "")

    # At threshold.
    boundary = project_dir / "boundary.bin"
    boundary.write_bytes(b"b" * threshold)
    backend.upload_file(str(boundary), "boundary.bin", "")

    assert seen_types[0] == "bytes"            # small → simple
    assert seen_types[1] == "generator"        # boundary → streaming


# ─── 5. Streaming path consults the throttle bucket per chunk ─────────────────


def test_streaming_put_throttles_each_chunk(make_config, config_dir, project_dir, monkeypatch):
    """When `max_upload_kbps` is set, the streaming generator must call
    `bucket.consume(len(block))` for every block it yields — verified
    with a stub bucket that records calls."""

    class _StubBucket:
        def __init__(self):
            self.calls: list[int] = []

        def consume(self, n: int) -> None:
            self.calls.append(int(n))

    stub = _StubBucket()
    monkeypatch.setattr(
        "claude_mirror.backends.webdav.get_throttle",
        lambda _kbps: stub,
    )

    threshold = 16 * 1024
    backend = _make_backend(make_config, config_dir, threshold=threshold, max_kbps=1024)

    class _FakeResp:
        status_code = 201

        def raise_for_status(self):
            pass

    def fake_put(url, data=None, headers=None, **kwargs):
        if hasattr(data, "__iter__") and not isinstance(data, (bytes, bytearray, str)):
            for _ in data:
                pass
        return _FakeResp()

    backend._session.put = fake_put

    # 3 MiB file with 1 MiB chunks → expect 3 throttle calls of 1 MiB,
    # except the last may be smaller. WebDAV chunk size is 1 MiB.
    local = project_dir / "big.bin"
    payload = b"z" * (3 * 1024 * 1024)
    local.write_bytes(payload)
    backend.upload_file(str(local), "big.bin", "")

    assert sum(stub.calls) == len(payload)
    # Each call <= 1 MiB block size.
    assert all(c <= 1024 * 1024 for c in stub.calls)
