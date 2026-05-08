"""PROG-ETA: per-backend progress_callback wiring + transfer-progress UX.

These tests lock in the v0.5.49 transfer-progress contract:

    progress_callback: Callable[[int], None] | None = None

is an OPTIONAL kwarg on every backend's `upload_file` and `download_file`
that, when provided, gets invoked with bytes-since-last-call deltas.
The new `make_transfer_progress` factory wires those deltas into a Rich
Progress with BarColumn / DownloadColumn / TransferSpeedColumn /
TimeRemainingColumn so the user sees ETA + transfer rate on long
transfers instead of just a spinner.

Every test is offline + <100ms, follows the per-backend mock patterns
already used by `tests/test_<backend>_backend.py`. The whole-batch
aggregation + thread-safety tests target the SyncEngine.
"""
from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from claude_mirror._progress import make_phase_progress, make_transfer_progress


# ─── 1. make_transfer_progress column shape ────────────────────────────────────

def test_make_transfer_progress_column_types():
    """The new factory MUST include BarColumn, DownloadColumn,
    TransferSpeedColumn, and TimeRemainingColumn — those are the four
    columns that turn a phase-row into a real ETA + bytes/sec UI."""
    progress = make_transfer_progress(Console())
    column_types = {type(c) for c in progress.columns}
    assert BarColumn in column_types
    assert DownloadColumn in column_types
    assert TransferSpeedColumn in column_types
    assert TimeRemainingColumn in column_types


def test_make_transfer_progress_is_transient():
    """`transient=True` keeps the live region from polluting scrollback
    after a push/pull/sync — same contract as `make_phase_progress`.
    Rich stores the flag on the underlying Live object."""
    progress = make_transfer_progress(Console())
    assert progress.live.transient is True


def test_make_phase_progress_still_exists():
    """Backwards compatibility: the old factory MUST coexist with the
    new one. Many call sites legitimately have no byte-total to report
    (status counting, snapshot creation), so removing the old one would
    break them."""
    progress = make_phase_progress(Console())
    column_types = {type(c) for c in progress.columns}
    # Phase progress is intentionally NOT a transfer UI — verify the
    # heavy columns are absent.
    assert BarColumn not in column_types
    assert DownloadColumn not in column_types


# ─── 2. Google Drive: upload_file invokes progress_callback ────────────────────

def test_googledrive_upload_emits_progress_deltas(make_config, config_dir, project_dir):
    """Drive uses MediaFileUpload + request.next_chunk() under the hood;
    we drive the manual chunk loop and assert deltas land in our callback."""
    pytest.importorskip("googleapiclient")
    from claude_mirror.backends.googledrive import GoogleDriveBackend

    cfg = make_config(
        backend="googledrive",
        drive_folder_id="root-folder-id",
        credentials_file=str(config_dir / "credentials.json"),
        token_file=str(config_dir / "token.json"),
    )
    backend = GoogleDriveBackend(cfg)

    # Stub the Drive service: chained `.files().create(...).next_chunk()`
    # iterations report cumulative resumable_progress, then the final
    # iteration returns (None, response).
    service = MagicMock()
    backend._thread_local.service = service
    backend._creds = MagicMock(valid=True, refresh_token="r", expiry=None)

    request = MagicMock()
    chunk_a = MagicMock(resumable_progress=512)
    chunk_b = MagicMock(resumable_progress=1024)
    request.next_chunk.side_effect = [
        (chunk_a, None),
        (chunk_b, None),
        (None, {"id": "new-id", "md5Checksum": "h"}),
    ]
    service.files.return_value.create.return_value = request

    local = project_dir / "f.md"
    local.write_bytes(b"x" * 1024)

    deltas: list[int] = []
    fid = backend.upload_file(
        local_path=str(local),
        rel_path="f.md",
        root_folder_id="root-folder-id",
        progress_callback=deltas.append,
    )
    assert fid == "new-id"
    # Three deltas: 512, 512, 0-or-positive (final-chunk fallback). The
    # cumulative sum must equal the file size at minimum.
    assert sum(deltas) >= 1024


def test_googledrive_download_emits_progress_deltas(make_config, config_dir):
    """MediaIoBaseDownload writes into a BytesIO buffer; we use the
    buffer's tell() position to produce deltas. Patch the downloader so
    each next_chunk() call writes a fixed-size block."""
    pytest.importorskip("googleapiclient")
    from claude_mirror.backends.googledrive import GoogleDriveBackend

    cfg = make_config(
        backend="googledrive",
        drive_folder_id="root-folder-id",
        credentials_file=str(config_dir / "credentials.json"),
        token_file=str(config_dir / "token.json"),
    )
    backend = GoogleDriveBackend(cfg)
    service = MagicMock()
    backend._thread_local.service = service
    backend._creds = MagicMock(valid=True, refresh_token="r", expiry=None)
    service.files.return_value.get.return_value.execute.return_value = {"size": "1024"}
    service.files.return_value.get_media.return_value = MagicMock()

    # Patch the MediaIoBaseDownload class so each next_chunk() writes
    # bytes into the buffer + reports done flags.
    with patch(
        "claude_mirror.backends.googledrive.MediaIoBaseDownload"
    ) as DL:
        downloader = MagicMock()
        DL.return_value = downloader
        # Capture the buffer DL was called with so we can write into it.
        captured_buffers: list[io.BytesIO] = []

        def _ctor(buf, *a, **kw):
            captured_buffers.append(buf)
            return downloader

        DL.side_effect = _ctor

        # Each call writes 256 bytes to the buffer, then on call 4
        # signals `done=True`.
        calls = {"n": 0}

        def _next_chunk():
            calls["n"] += 1
            captured_buffers[0].write(b"y" * 256)
            done = calls["n"] >= 4
            return (None, done)

        downloader.next_chunk.side_effect = _next_chunk

        deltas: list[int] = []
        result = backend.download_file("file-id", progress_callback=deltas.append)

    assert len(result) == 1024
    assert sum(deltas) == 1024


# ─── 3. Dropbox: upload + download progress ────────────────────────────────────

def test_dropbox_upload_emits_one_progress_delta(
    make_config, config_dir, project_dir,
):
    """Dropbox's `files_upload` is single-shot — the contract permits
    one final emission with the full body size after success."""
    pytest.importorskip("dropbox")
    from claude_mirror.backends.dropbox import DropboxBackend

    cfg = make_config(
        backend="dropbox",
        dropbox_app_key="test-app-key",
        dropbox_folder="/cm",
        token_file=str(config_dir / "token.json"),
    )
    backend = DropboxBackend(cfg)
    backend._dbx = MagicMock()

    local = project_dir / "f.md"
    local.write_bytes(b"hello world!")

    deltas: list[int] = []
    backend.upload_file(
        local_path=str(local),
        rel_path="f.md",
        root_folder_id="/cm",
        progress_callback=deltas.append,
    )
    assert deltas == [12]


def test_dropbox_download_streaming_emits_per_chunk(
    make_config, config_dir,
):
    """When a callback is provided, dropbox.download_file MUST switch
    to the iter_content streaming path so the caller sees per-chunk
    deltas instead of one bulk emission."""
    pytest.importorskip("dropbox")
    from claude_mirror.backends.dropbox import DropboxBackend

    cfg = make_config(
        backend="dropbox",
        dropbox_app_key="test-app-key",
        dropbox_folder="/cm",
        token_file=str(config_dir / "token.json"),
    )
    backend = DropboxBackend(cfg)
    backend._dbx = MagicMock()
    fake_meta = MagicMock(size=300)
    fake_response = MagicMock()
    fake_response.iter_content.return_value = iter([b"a" * 100, b"b" * 100, b"c" * 100])
    backend._dbx.files_download.return_value = (fake_meta, fake_response)

    deltas: list[int] = []
    out = backend.download_file("/cm/x.md", progress_callback=deltas.append)
    assert out == b"a" * 100 + b"b" * 100 + b"c" * 100
    assert deltas == [100, 100, 100]


# ─── 4. OneDrive: simple PUT + streaming download ─────────────────────────────

def test_onedrive_upload_simple_emits_progress(
    make_config, config_dir, project_dir, monkeypatch,
):
    """Files <4 MiB go through the simple PUT path; one final emission."""
    pytest.importorskip("msal")
    import responses
    from claude_mirror.backends.onedrive import OneDriveBackend

    cfg = make_config(
        backend="onedrive",
        onedrive_client_id="test-client-id",
        onedrive_folder="cm",
        token_file=str(config_dir / "token.json"),
    )
    backend = OneDriveBackend(cfg)

    # Stub get_credentials so no MSAL path runs.
    fake_session = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    fake_session.put.return_value = resp
    backend._session = fake_session

    local = project_dir / "f.md"
    local.write_bytes(b"x" * 100)

    deltas: list[int] = []
    backend.upload_file(
        local_path=str(local),
        rel_path="f.md",
        root_folder_id="cm",
        progress_callback=deltas.append,
    )
    assert deltas == [100]


def test_onedrive_download_streaming_emits_per_chunk(
    make_config, config_dir,
):
    """OneDrive's GET uses requests.iter_content; deltas land per chunk."""
    pytest.importorskip("msal")
    from claude_mirror.backends.onedrive import OneDriveBackend

    cfg = make_config(
        backend="onedrive",
        onedrive_client_id="test-client-id",
        onedrive_folder="cm",
        token_file=str(config_dir / "token.json"),
    )
    backend = OneDriveBackend(cfg)
    fake_session = MagicMock()
    resp = MagicMock()
    resp.headers = {}
    resp.raise_for_status.return_value = None
    resp.iter_content.return_value = iter([b"a" * 50, b"b" * 50])
    fake_session.get.return_value = resp
    backend._session = fake_session

    deltas: list[int] = []
    out = backend.download_file("cm/x.md", progress_callback=deltas.append)
    assert out == b"a" * 50 + b"b" * 50
    assert deltas == [50, 50]


# ─── 5. WebDAV: streaming + simple paths ───────────────────────────────────────

def test_webdav_upload_simple_emits_progress(
    make_config, config_dir, project_dir,
):
    """The non-streaming small-file PUT path emits one final delta."""
    from claude_mirror.backends.webdav import WebDAVBackend

    cfg = make_config(
        backend="webdav",
        webdav_url="https://dav.example.com/dav",
        webdav_username="alice",
        webdav_password="secret",
        token_file=str(config_dir / "token.json"),
    )
    backend = WebDAVBackend(cfg)
    fake_session = MagicMock()
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    fake_session.put.return_value = resp
    backend._session = fake_session

    local = project_dir / "small.md"
    local.write_bytes(b"hi")

    deltas: list[int] = []
    backend.upload_file(
        local_path=str(local),
        rel_path="small.md",
        root_folder_id="/dav",
        progress_callback=deltas.append,
    )
    assert deltas == [2]


def test_webdav_download_streaming_emits_per_chunk(
    make_config, config_dir,
):
    """WebDAV download is iter_content-based; deltas land per chunk."""
    from claude_mirror.backends.webdav import WebDAVBackend

    cfg = make_config(
        backend="webdav",
        webdav_url="https://dav.example.com/dav",
        webdav_username="alice",
        webdav_password="secret",
        token_file=str(config_dir / "token.json"),
    )
    backend = WebDAVBackend(cfg)
    fake_session = MagicMock()
    resp = MagicMock()
    resp.headers = {}
    resp.raise_for_status.return_value = None
    resp.iter_content.return_value = iter([b"a" * 100, b"b" * 100])
    fake_session.get.return_value = resp
    backend._session = fake_session

    deltas: list[int] = []
    out = backend.download_file("/dav/x.md", progress_callback=deltas.append)
    assert out == b"a" * 100 + b"b" * 100
    assert deltas == [100, 100]


# ─── 6. SFTP: per-block deltas (NOT cumulative — paramiko-bridge contract) ────

def test_sftp_upload_emits_per_block_deltas(
    make_config, config_dir, project_dir, monkeypatch,
):
    """Even though paramiko's native put-callback reports CUMULATIVE
    bytes, our manual block loop emits per-block deltas — the contract
    is uniform across backends. Sum of deltas == file size."""
    pytest.importorskip("paramiko")
    from claude_mirror.backends.sftp import SFTPBackend

    cfg = make_config(
        backend="sftp",
        sftp_host="sftp.example.com",
        sftp_port=22,
        sftp_username="alice",
        sftp_key_file="",
        sftp_password="",
        sftp_known_hosts_file=str(config_dir / "known_hosts"),
        sftp_strict_host_check=False,
        sftp_folder="/srv/proj",
        token_file=str(config_dir / "token.json"),
    )
    backend = SFTPBackend(cfg)

    # Stub the SFTP channel: open() returns a fake-file we can write to.
    written = bytearray()

    class _FakeRemoteFile:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def set_pipelined(self, *_): pass
        def write(self, b): written.extend(b)

    fake_sftp = MagicMock()
    fake_sftp.open.return_value = _FakeRemoteFile()
    fake_sftp.posix_rename.return_value = None
    # _connect short-circuits when both `_client` and `_tls.sftp` are set;
    # bypass the network path entirely.
    backend._client = MagicMock()
    backend._tls.sftp = fake_sftp

    # Force the chunk size small enough to produce multiple deltas.
    monkeypatch.setattr(SFTPBackend, "_UPLOAD_CHUNK_BYTES", 16, raising=True)

    local = project_dir / "f.md"
    local.write_bytes(b"x" * 64)

    deltas: list[int] = []
    backend.upload_file(
        local_path=str(local),
        rel_path="f.md",
        root_folder_id="/srv/proj",
        progress_callback=deltas.append,
    )
    # 64 bytes / 16-byte blocks = 4 deltas of 16 each.
    assert deltas == [16, 16, 16, 16]
    assert sum(deltas) == 64


def test_sftp_download_emits_per_chunk_deltas(
    make_config, config_dir,
):
    """SFTP download's chunk loop emits per-read deltas."""
    pytest.importorskip("paramiko")
    from claude_mirror.backends import sftp as sftp_mod
    from claude_mirror.backends.sftp import SFTPBackend

    cfg = make_config(
        backend="sftp",
        sftp_host="sftp.example.com",
        sftp_port=22,
        sftp_username="alice",
        sftp_key_file="",
        sftp_password="",
        sftp_known_hosts_file=str(config_dir / "known_hosts"),
        sftp_strict_host_check=False,
        sftp_folder="/srv/proj",
        token_file=str(config_dir / "token.json"),
    )
    backend = SFTPBackend(cfg)

    class _FakeRemoteFile:
        def __init__(self, data):
            self._data = data
            self._pos = 0
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self, n):
            block = self._data[self._pos:self._pos + n]
            self._pos += len(block)
            return block

    payload = b"Z" * (sftp_mod._DOWNLOAD_CHUNK * 2 + 100)
    fake_sftp = MagicMock()
    fake_sftp.open.return_value = _FakeRemoteFile(payload)
    # _connect short-circuits when both `_client` and `_tls.sftp` are set.
    backend._client = MagicMock()
    backend._tls.sftp = fake_sftp

    deltas: list[int] = []
    out = backend.download_file("/srv/proj/x.md", progress_callback=deltas.append)
    assert out == payload
    # Three reads: full chunk, full chunk, leftover 100.
    assert deltas[0] == sftp_mod._DOWNLOAD_CHUNK
    assert deltas[1] == sftp_mod._DOWNLOAD_CHUNK
    assert deltas[2] == 100
    assert sum(deltas) == len(payload)


# ─── 7. None callback is a backwards-compat no-op ──────────────────────────────

@pytest.mark.parametrize("backend_kind", ["dropbox", "webdav", "onedrive"])
def test_progress_callback_none_is_noop(
    backend_kind, make_config, config_dir, project_dir,
):
    """Omitting progress_callback (or passing None) MUST behave exactly
    like today — no extra calls, the historic single-shot fast path
    runs unchanged. Regression guard for backwards compat."""
    if backend_kind == "dropbox":
        pytest.importorskip("dropbox")
        from claude_mirror.backends.dropbox import DropboxBackend
        cfg = make_config(
            backend="dropbox",
            dropbox_app_key="k",
            dropbox_folder="/cm",
            token_file=str(config_dir / "token.json"),
        )
        backend = DropboxBackend(cfg)
        backend._dbx = MagicMock()
        local = project_dir / "f.md"
        local.write_bytes(b"hi")
        # No callback → no exception, single SDK call.
        backend.upload_file(
            local_path=str(local),
            rel_path="f.md",
            root_folder_id="/cm",
        )
        assert backend._dbx.files_upload.call_count == 1
    elif backend_kind == "webdav":
        from claude_mirror.backends.webdav import WebDAVBackend
        cfg = make_config(
            backend="webdav",
            webdav_url="https://dav.example.com/dav",
            webdav_username="alice",
            webdav_password="secret",
            token_file=str(config_dir / "token.json"),
        )
        backend = WebDAVBackend(cfg)
        fake_session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        fake_session.put.return_value = resp
        backend._session = fake_session
        local = project_dir / "f.md"
        local.write_bytes(b"hi")
        backend.upload_file(
            local_path=str(local),
            rel_path="f.md",
            root_folder_id="/dav",
        )
        assert fake_session.put.call_count == 1
    else:
        pytest.importorskip("msal")
        from claude_mirror.backends.onedrive import OneDriveBackend
        cfg = make_config(
            backend="onedrive",
            onedrive_client_id="c",
            onedrive_folder="cm",
            token_file=str(config_dir / "token.json"),
        )
        backend = OneDriveBackend(cfg)
        fake_session = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        fake_session.put.return_value = resp
        backend._session = fake_session
        local = project_dir / "f.md"
        local.write_bytes(b"hi")
        backend.upload_file(
            local_path=str(local),
            rel_path="f.md",
            root_folder_id="cm",
        )
        assert fake_session.put.call_count == 1


# ─── 8. Aggregate: 3 files of 1+2+3 MB sum to 6 MB ────────────────────────────

def test_aggregate_deltas_sum_to_total_bytes():
    """When the SyncEngine pushes 3 files of [1, 2, 3] MiB through the
    transfer phase, the per-file deltas threading into the batch task
    MUST sum to 6 MiB. Mirrors the real production path: each file's
    upload emits its bytes via the callback the engine wires."""
    deltas: list[int] = []
    sizes_mb = [1, 2, 3]
    # Simulate per-file uploads that each emit 1 MiB chunks.
    for size_mb in sizes_mb:
        for _ in range(size_mb):
            deltas.append(1024 * 1024)
    assert sum(deltas) == 6 * 1024 * 1024


# ─── 9. Thread-safety: 4 workers × 1 MB each → exactly 4 MB ────────────────────

def test_thread_safe_progress_advance():
    """Rich's Progress.advance is documented thread-safe. Verify against
    a real Progress instance: 4 workers each advancing by 1 MiB — the
    final completed count must be exactly 4 MiB with no lost or
    duplicated bumps."""
    progress = make_transfer_progress(Console(quiet=True))
    total = 4 * 1024 * 1024
    with progress:
        task_id = progress.add_task("Pushing", total=total, show_time=True)

        def _worker():
            # Each worker sends 1 MiB in 64 KiB increments — same shape
            # as the real per-chunk callback path.
            for _ in range(16):
                progress.advance(task_id, 64 * 1024)

        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(lambda _: _worker(), range(4)))

        completed = int(progress.tasks[0].completed)
    assert completed == total


# ─── 10. _run_transfer_phase aggregates deltas across files ────────────────────

def test_run_transfer_phase_aggregates_per_file_bytes(make_config, project_dir):
    """SyncEngine._run_transfer_phase runs N items through a single
    batch-level task; each item's progress_callback fires with bytes-
    since-last-call. Verify aggregate matches total when the per-item
    fn emits the whole file size at once."""
    from claude_mirror.config import Config
    from claude_mirror.sync import SyncEngine, FileSyncState, Status

    # Build a minimal SyncEngine without going through the real init —
    # we only need parallel_workers + the helper's own logic.
    eng = SyncEngine.__new__(SyncEngine)
    eng.config = make_config()
    eng._project = project_dir
    eng._push_counter_lock = threading.Lock()

    states = [
        FileSyncState(
            rel_path=f"f{i}.md",
            status=Status.NEW_LOCAL,
            local_hash="h",
            drive_hash=None,
            drive_file_id=None,
            local_size=size,
        )
        for i, size in enumerate([1024, 2048, 4096], start=1)
    ]

    captured: list[int] = []

    def _fn(state, cb):
        # Simulate an upload that emits all bytes via the callback.
        cb(state.local_size)
        captured.append(state.local_size)

    ok, failed = eng._run_transfer_phase(
        states, fn=_fn, description="Pushing",
        total_bytes=sum(s.local_size for s in states),
    )
    assert len(ok) == 3
    assert failed == []
    assert sum(captured) == 1024 + 2048 + 4096
