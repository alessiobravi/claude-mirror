"""Per-backend round-trip tests for GoogleDriveBackend.

These tests use unittest.mock to stub the `googleapiclient.discovery.build`
return value. We intentionally do NOT mock httplib2 (the transport
underneath) — that path is too deep and brittle. Instead we patch the
backend's per-thread `service` attribute directly with a MagicMock that
mirrors the chained-call shape `service.files().list().execute()`.

All tests are offline. Each test should run in <100ms.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httplib2
import pytest

from claude_mirror.backends import ErrorClass
from claude_mirror.backends.googledrive import GoogleDriveBackend


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_backend(make_config, config_dir: Path) -> GoogleDriveBackend:
    """Build a GoogleDriveBackend pointed at the per-test config dir."""
    cfg = make_config(
        backend="googledrive",
        drive_folder_id="root-folder-id",
        credentials_file=str(config_dir / "credentials.json"),
        token_file=str(config_dir / "token.json"),
    )
    return GoogleDriveBackend(cfg)


def _stub_service() -> MagicMock:
    """Return a MagicMock shaped like a Drive v3 service.

    Pattern: each `.files().<method>(...).execute()` chain returns whatever
    `mock.files.return_value.<method>.return_value.execute.return_value`
    is set to. Tests configure those return values directly.
    """
    return MagicMock()


def _attach_service(backend: GoogleDriveBackend, service: MagicMock) -> None:
    """Inject a stub service into the backend's thread-local cache so the
    `.service` property returns the stub without any auth path running."""
    backend._thread_local.service = service
    # Also stash a fake creds object so any incidental get_credentials() call
    # doesn't trip on the missing token file.
    backend._creds = MagicMock(valid=True, refresh_token="fake-refresh", expiry=None)


# ─── 1. authenticate writes a token file ───────────────────────────────────────

def test_authenticate_writes_token_file(make_config, config_dir, mock_oauth_google):
    """authenticate() must persist a JSON token file with refresh_token,
    client_id, and scopes after the OAuth flow completes."""
    backend = _make_backend(make_config, config_dir)
    with patch("claude_mirror.backends.googledrive.build") as mock_build:
        mock_build.return_value = MagicMock()
        backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    data = json.loads(token_path.read_text())
    assert data["refresh_token"] == "fake-refresh-token"
    assert data["client_id"] == "fake-client"
    assert "scopes" in data and data["scopes"] == [
        "https://www.googleapis.com/auth/drive"
    ]


# ─── 2. get_credentials loads existing token ───────────────────────────────────

def test_get_credentials_loads_existing_token(make_config, config_dir):
    """A pre-existing valid token file should yield Credentials without
    triggering a re-auth flow."""
    backend = _make_backend(make_config, config_dir)
    Path(backend.config.token_file).write_text(json.dumps({
        "token": "existing-access-token",
        "refresh_token": "existing-refresh-token",
        "client_id": "existing-client",
        "client_secret": "existing-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }))

    # `_needs_refresh` calls datetime.utcnow() (deprecated in 3.14); patch it
    # to a constant False so the load path doesn't trip the warning filter.
    with patch.object(GoogleDriveBackend, "_needs_refresh", return_value=False):
        creds = backend.get_credentials()
    assert creds.refresh_token == "existing-refresh-token"


# ─── 3. get_credentials raises on missing token ────────────────────────────────

def test_get_credentials_raises_on_missing_token(make_config, config_dir):
    """No token file → RuntimeError prompting the user to run `auth`."""
    backend = _make_backend(make_config, config_dir)
    # Token file deliberately not created.
    with pytest.raises(RuntimeError, match="Not authenticated"):
        backend.get_credentials()


# ─── 4. get_or_create_folder creates when missing ──────────────────────────────

def test_get_or_create_folder_creates_missing(make_config, config_dir):
    """When `files().list()` returns an empty match, create the folder and
    return the new id."""
    backend = _make_backend(make_config, config_dir)
    service = _stub_service()
    # First .list().execute() — no match.
    service.files.return_value.list.return_value.execute.return_value = {"files": []}
    # .create().execute() — returns new id.
    service.files.return_value.create.return_value.execute.return_value = {"id": "new-folder-id"}
    _attach_service(backend, service)

    folder_id = backend.get_or_create_folder("subdir", "parent-id")
    assert folder_id == "new-folder-id"
    # The create call should have happened.
    assert service.files.return_value.create.called


# ─── 5. get_or_create_folder returns existing ──────────────────────────────────

def test_get_or_create_folder_returns_existing(make_config, config_dir):
    """When `files().list()` returns a hit, no create call is issued."""
    backend = _make_backend(make_config, config_dir)
    service = _stub_service()
    service.files.return_value.list.return_value.execute.return_value = {
        "files": [{"id": "existing-folder-id"}]
    }
    _attach_service(backend, service)

    folder_id = backend.get_or_create_folder("subdir", "parent-id")
    assert folder_id == "existing-folder-id"
    assert not service.files.return_value.create.called


# ─── 6. resolve_path walks components ──────────────────────────────────────────

def test_resolve_path_walks_components_and_creates_folders(make_config, config_dir):
    """`a/b/c/file.md` → 3 folder lookups; final tuple is (final_parent_id, basename)."""
    backend = _make_backend(make_config, config_dir)
    # Stub get_or_create_folder so we can count invocations without
    # threading a complex MagicMock chain through three separate list/create
    # round-trips.
    calls: list[tuple[str, str]] = []
    def fake_get_or_create(name: str, parent_id: str) -> str:
        calls.append((name, parent_id))
        return f"id-{name}"
    backend.get_or_create_folder = fake_get_or_create  # type: ignore[method-assign]

    parent_id, basename = backend.resolve_path("a/b/c/file.md", "root-id")
    assert basename == "file.md"
    assert parent_id == "id-c"
    # Three intermediate folder creations: a, b, c.
    assert calls == [
        ("a", "root-id"),
        ("b", "id-a"),
        ("c", "id-b"),
    ]


# ─── 7. upload_file (simple, new file) ─────────────────────────────────────────

def test_upload_file_simple(make_config, config_dir, project_dir):
    """A small new file → `files().create().execute()` returns the new id."""
    backend = _make_backend(make_config, config_dir)
    local = project_dir / "hello.md"
    local.write_text("hello world")

    service = _stub_service()
    service.files.return_value.create.return_value.execute.return_value = {
        "id": "new-file-id",
        "md5Checksum": "abc",
    }
    _attach_service(backend, service)

    # Stub resolve_path so we don't go through get_or_create_folder.
    backend.resolve_path = lambda rel, root: (root, "hello.md")  # type: ignore[method-assign]

    file_id = backend.upload_file(str(local), "hello.md", "root-id")
    assert file_id == "new-file-id"
    assert service.files.return_value.create.called
    assert not service.files.return_value.update.called


# ─── 8. upload_file (update existing) ──────────────────────────────────────────

def test_upload_file_update_existing(make_config, config_dir, project_dir):
    """When file_id is provided, the backend uses `files().update()`,
    NOT `files().create()`."""
    backend = _make_backend(make_config, config_dir)
    local = project_dir / "hello.md"
    local.write_text("hello v2")

    service = _stub_service()
    service.files.return_value.update.return_value.execute.return_value = {
        "id": "existing-file-id",
        "md5Checksum": "def",
    }
    _attach_service(backend, service)

    file_id = backend.upload_file(
        str(local), "hello.md", "root-id", file_id="existing-file-id"
    )
    assert file_id == "existing-file-id"
    assert service.files.return_value.update.called
    assert not service.files.return_value.create.called


# ─── 9. download_file returns bytes ────────────────────────────────────────────

def test_download_file_returns_bytes(make_config, config_dir):
    """The backend must round-trip remote bytes faithfully.

    `MediaIoBaseDownload` writes the response content into the supplied
    BytesIO; we stub it at the module level so the real httplib2 path
    never runs.
    """
    backend = _make_backend(make_config, config_dir)
    service = _stub_service()
    # Pre-flight metadata fetch returns size = small.
    service.files.return_value.get.return_value.execute.return_value = {"size": "11"}
    # The .get_media() return value is an opaque request object that
    # MediaIoBaseDownload consumes — we'll stub the downloader instead.
    service.files.return_value.get_media.return_value = MagicMock()
    _attach_service(backend, service)

    payload = b"hello world"

    class FakeDownloader:
        def __init__(self, buffer, request):
            self.buffer = buffer
            self.done_yet = False

        def next_chunk(self):
            self.buffer.write(payload)
            self.done_yet = True
            return None, True

    with patch(
        "claude_mirror.backends.googledrive.MediaIoBaseDownload",
        FakeDownloader,
    ):
        out = backend.download_file("file-id-123")

    assert out == payload


# ─── 10. get_file_hash returns md5 ─────────────────────────────────────────────

def test_get_file_hash_returns_md5(make_config, config_dir):
    """`files().get(fields='md5Checksum').execute()` → returns the hash string."""
    backend = _make_backend(make_config, config_dir)
    service = _stub_service()
    service.files.return_value.get.return_value.execute.return_value = {
        "md5Checksum": "deadbeef"
    }
    _attach_service(backend, service)

    h = backend.get_file_hash("file-id-xyz")
    assert h == "deadbeef"


# ─── 11. classify_error: invalid_grant → AUTH ──────────────────────────────────

def test_classify_error_invalid_grant_is_auth(make_config, config_dir):
    """A RefreshError with 'invalid_grant' in args is unambiguously AUTH —
    only re-running `claude-mirror auth` will recover."""
    from google.auth.exceptions import RefreshError
    backend = _make_backend(make_config, config_dir)
    exc = RefreshError(
        "invalid_grant: Token has been expired or revoked.",
        {"error": "invalid_grant"},
    )
    assert backend.classify_error(exc) == ErrorClass.AUTH


# ─── 12. classify_error: 5xx → TRANSIENT ───────────────────────────────────────

def test_classify_error_5xx_is_transient(make_config, config_dir):
    """An HttpError with status 503 should map to TRANSIENT — server is
    unhappy, but a retry has a real chance of success."""
    from googleapiclient.errors import HttpError
    backend = _make_backend(make_config, config_dir)
    resp = httplib2.Response({"status": 503})
    exc = HttpError(resp, b'{"error": {"code": 503, "message": "service unavailable"}}')
    assert backend.classify_error(exc) == ErrorClass.TRANSIENT
