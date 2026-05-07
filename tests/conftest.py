"""Shared pytest fixtures for the claude-mirror test suite.

Conventions:
    * Tests must NOT touch the user's real ~/.config/claude_mirror/ tree —
      every test uses tmp_path or one of the fixtures below for isolation.
    * Tests must NOT make real network calls. Use the fake_backend fixture
      or the `responses` fixture for HTTP-level mocking.
    * Tests must NOT hit real cloud storage (Drive/Dropbox/OneDrive/WebDAV).
      Backend tests use FakeStorageBackend (in-memory) or `responses` to
      stub the HTTP layer.

Fixture overview:
    project_dir         — tmp_path/project, empty.
    config_dir          — tmp_path/config, empty.
    make_config         — factory: build a Config with sensible defaults.
    write_files         — factory: drop a {rel_path: content} dict into project_dir.
    write_manifest      — factory: write a manifest dict to disk.
    manifest_path       — Path where the manifest WILL live (may not exist yet).
    fake_backend        — FakeStorageBackend, full ABC implementation.
    fake_notifier       — FakeNotificationBackend, in-memory pub/sub.
    mock_oauth_google   — patch google_auth_oauthlib so authenticate() doesn't open a browser.
    mock_oauth_dropbox  — patch dropbox.DropboxOAuth2FlowNoRedirect.
    mock_oauth_msal     — patch msal device-flow / token-acquire.
    mock_oauth_webdav   — patch the WebDAV PROPFIND test connection.
"""
from __future__ import annotations

import hashlib
import io
import json
import threading
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from claude_mirror.backends import StorageBackend, ErrorClass
from claude_mirror.config import Config
from claude_mirror.events import SyncEvent
from claude_mirror.notifications import NotificationBackend


# ─── Filesystem fixtures ───────────────────────────────────────────────────────

@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Empty project directory inside the test's tmp_path."""
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Empty config dir inside the test's tmp_path. Use to host token /
    credentials files without touching ~/.config/claude_mirror/."""
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def make_config(project_dir: Path, config_dir: Path):
    """Factory: returns a callable that builds a Config dataclass with
    sensible defaults pointing at the temp project + config dirs.

    Usage:
        def test_something(make_config):
            cfg = make_config(file_patterns=["**/*.md"])
            assert cfg.project_path.endswith("/project")
    """
    def _make(**overrides: Any) -> Config:
        defaults: Dict[str, Any] = {
            "project_path": str(project_dir),
            "backend": "googledrive",
            "drive_folder_id": "test-folder-id",
            "credentials_file": str(config_dir / "credentials.json"),
            "token_file": str(config_dir / "token.json"),
            "file_patterns": ["**/*.md"],
            "exclude_patterns": [],
            "machine_name": "test-machine",
            "user": "test-user",
        }
        defaults.update(overrides)
        # Drop unknown keys gracefully — some fields only exist in newer Configs
        valid = {k: v for k, v in defaults.items() if k in Config.__dataclass_fields__}
        return Config(**valid)
    return _make


@pytest.fixture
def write_files(project_dir: Path):
    """Factory: write a dict of {rel_path: content} into project_dir.

    Returns the project_dir for chaining.
    """
    def _write(files: Dict[str, str]) -> Path:
        for rel, content in files.items():
            full = project_dir / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
        return project_dir
    return _write


@pytest.fixture
def manifest_path(project_dir: Path) -> Path:
    """Path where a project's manifest would live, but doesn't exist yet."""
    return project_dir / ".claude_mirror_manifest.json"


@pytest.fixture
def write_manifest(manifest_path: Path):
    """Factory: write a manifest dict to disk."""
    def _write(data: Dict[str, Any]) -> Path:
        manifest_path.write_text(json.dumps(data))
        return manifest_path
    return _write


# ─── Fake storage backend (full ABC implementation) ────────────────────────────

class FakeStorageBackend(StorageBackend):
    """Full StorageBackend implementation backed by in-memory dicts.

    Mimics the contract of GoogleDriveBackend / DropboxBackend / etc. closely
    enough that SyncEngine and SnapshotManager can drive it transparently.

    Storage model:
        - Folders are tracked by `(parent_id, name) → folder_id`.
        - Files are tracked by `file_id → {name, parent_id, content, created_at}`.
        - File IDs are uuid strings.
        - The "root folder" is the one passed in via config.drive_folder_id;
          the fake backend pre-registers it so resolve_path() works without
          first calling get_or_create_folder().

    Call recording:
        Every public method appends to `self.calls`, a list of
        `(method_name, *args)` tuples. Tests can assert call patterns.

    Failure injection:
        Set `self.fail_next_upload = True` to make the next `upload_file`
        / `upload_bytes` raise. Set `self.fail_next_download = True` for
        downloads. Set `self.classify_as = ErrorClass.X` to override the
        classify_error return for any raised exception.
    """

    backend_name = "fake"

    def __init__(self, root_folder_id: str = "test-folder-id") -> None:
        self.root_folder_id = root_folder_id
        # folders: (parent_id, name) → folder_id
        self.folders: Dict[Tuple[str, str], str] = {}
        # files: file_id → {name, parent_id, content, created_at}
        self.files: Dict[str, Dict[str, Any]] = {}
        # call recording
        self.calls: List[Tuple[str, ...]] = []
        # failure injection
        self.fail_next_upload: bool = False
        self.fail_next_download: bool = False
        self.classify_as: Optional[ErrorClass] = None

    def _record(self, *args: Any) -> None:
        self.calls.append(tuple(args))

    @staticmethod
    def _md5(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()

    def authenticate(self) -> Any:
        self._record("authenticate")
        return self

    def get_credentials(self) -> Any:
        self._record("get_credentials")
        return self

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        self._record("get_or_create_folder", name, parent_id)
        key = (parent_id, name)
        if key not in self.folders:
            self.folders[key] = f"folder-{uuid.uuid4().hex[:8]}"
        return self.folders[key]

    def resolve_path(self, rel_path: str, root_folder_id: str) -> Tuple[str, str]:
        """Walk path components, creating intermediate folders. Returns
        (final_parent_id, basename)."""
        self._record("resolve_path", rel_path, root_folder_id)
        parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
        if not parts:
            return root_folder_id, ""
        *parents, basename = parts
        parent_id = root_folder_id
        for part in parents:
            parent_id = self.get_or_create_folder(part, parent_id)
        return parent_id, basename

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set] = None,
    ) -> List[Dict[str, Any]]:
        self._record("list_files_recursive", folder_id, prefix)
        excluded = exclude_folder_names or set()
        results: List[Dict[str, Any]] = []
        # Files directly in this folder
        for fid, meta in self.files.items():
            if meta["parent_id"] == folder_id:
                rel = f"{prefix}{meta['name']}" if prefix else meta["name"]
                results.append({
                    "id": fid,
                    "name": meta["name"],
                    "md5Checksum": self._md5(meta["content"]),
                    "relative_path": rel,
                    "size": len(meta["content"]),
                })
        # Recurse into subfolders
        for (parent, name), sub_id in self.folders.items():
            if parent == folder_id and name not in excluded:
                sub_prefix = f"{prefix}{name}/" if prefix else f"{name}/"
                results.extend(
                    self.list_files_recursive(sub_id, sub_prefix, progress_cb, exclude_folder_names)
                )
        if progress_cb:
            progress_cb(len(self.folders), len(results))
        return results

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> List[Dict[str, Any]]:
        self._record("list_folders", parent_id, name)
        out = []
        for (parent, n), fid in self.folders.items():
            if parent == parent_id and (name is None or n == name):
                out.append({"id": fid, "name": n, "createdTime": "2026-01-01T00:00:00Z"})
        return out

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        self._record("upload_file", local_path, rel_path, root_folder_id, file_id)
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise RuntimeError("fake_backend: injected upload failure")
        with open(local_path, "rb") as f:
            content = f.read()
        parent_id, basename = self.resolve_path(rel_path, root_folder_id)
        return self._store_file(content, basename, parent_id, file_id)

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        self._record("upload_bytes", name, folder_id, file_id, mimetype)
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise RuntimeError("fake_backend: injected upload failure")
        return self._store_file(content, name, folder_id, file_id)

    def _store_file(
        self,
        content: bytes,
        name: str,
        parent_id: str,
        file_id: Optional[str],
    ) -> str:
        if file_id and file_id in self.files:
            self.files[file_id]["content"] = content
            self.files[file_id]["name"] = name
            self.files[file_id]["parent_id"] = parent_id
            return file_id
        new_id = f"file-{uuid.uuid4().hex[:12]}"
        self.files[new_id] = {
            "name": name,
            "parent_id": parent_id,
            "content": content,
            "created_at": "2026-01-01T00:00:00Z",
        }
        return new_id

    def download_file(self, file_id: str) -> bytes:
        self._record("download_file", file_id)
        if self.fail_next_download:
            self.fail_next_download = False
            raise RuntimeError("fake_backend: injected download failure")
        if file_id not in self.files:
            raise FileNotFoundError(f"fake_backend: no file with id {file_id}")
        return self.files[file_id]["content"]

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        self._record("get_file_id", name, folder_id)
        for fid, meta in self.files.items():
            if meta["name"] == name and meta["parent_id"] == folder_id:
                return fid
        return None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        self._record("copy_file", source_file_id, dest_folder_id, name)
        if source_file_id not in self.files:
            raise FileNotFoundError(f"fake_backend: cannot copy missing {source_file_id}")
        src = self.files[source_file_id]
        return self._store_file(src["content"], name, dest_folder_id, None)

    def get_file_hash(self, file_id: str) -> Optional[str]:
        self._record("get_file_hash", file_id)
        if file_id not in self.files:
            return None
        return self._md5(self.files[file_id]["content"])

    def delete_file(self, file_id: str) -> None:
        self._record("delete_file", file_id)
        self.files.pop(file_id, None)

    def classify_error(self, exc: BaseException) -> ErrorClass:
        if self.classify_as is not None:
            return self.classify_as
        return super().classify_error(exc)


@pytest.fixture
def fake_backend() -> FakeStorageBackend:
    """A reusable in-memory backend that fully implements StorageBackend.
    Use this for SyncEngine / SnapshotManager tests that need a backend
    but don't care about real cloud semantics."""
    return FakeStorageBackend()


# ─── Fake notification backend ─────────────────────────────────────────────────

class FakeNotificationBackend(NotificationBackend):
    """In-memory pub/sub for sync-engine tests. Any event published is
    appended to `self.published`; tests can manually invoke the registered
    callback via `self.deliver(event)` to simulate a remote-machine push."""

    backend_name = "fake-notify"

    def __init__(self) -> None:
        self.published: List[SyncEvent] = []
        self.callbacks: List[Callable[[SyncEvent], None]] = []
        self.topic_ensured: bool = False
        self.subscription_ensured: bool = False
        self.closed: bool = False

    def ensure_topic(self) -> None:
        self.topic_ensured = True

    def ensure_subscription(self) -> None:
        self.subscription_ensured = True

    def publish_event(self, event: SyncEvent) -> None:
        self.published.append(event)

    def watch(
        self,
        callback: Callable[[SyncEvent], None],
        stop_event: threading.Event,
    ) -> None:
        # Register the callback so tests can manually fire events into it.
        self.callbacks.append(callback)
        # Block until stop_event — but in tests, just return after registering.
        stop_event.wait(timeout=0.001)

    def close(self) -> None:
        self.closed = True

    def deliver(self, event: SyncEvent) -> None:
        """Test helper: simulate a remote-machine event arriving."""
        for cb in self.callbacks:
            cb(event)


@pytest.fixture
def fake_notifier() -> FakeNotificationBackend:
    """A reusable in-memory NotificationBackend."""
    return FakeNotificationBackend()


# ─── OAuth flow mocks (per-backend) ────────────────────────────────────────────

@pytest.fixture
def mock_oauth_google(monkeypatch, config_dir: Path):
    """Patch google_auth_oauthlib so GoogleDriveBackend.authenticate() doesn't
    open a browser. The patch returns a stub Credentials object whose .to_json()
    produces a writeable token."""
    fake_creds = MagicMock()
    fake_creds.to_json.return_value = json.dumps({
        "token": "fake-access-token",
        "refresh_token": "fake-refresh-token",
        "client_id": "fake-client",
        "client_secret": "fake-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    })
    fake_creds.valid = True
    fake_creds.expired = False
    fake_creds.refresh_token = "fake-refresh-token"

    fake_flow = MagicMock()
    fake_flow.run_local_server.return_value = fake_creds

    def fake_from_client_secrets_file(*args, **kwargs):
        return fake_flow

    # Drop a fake credentials.json so InstalledAppFlow.from_client_secrets_file
    # has something to read (even though we're patching it out).
    creds_path = config_dir / "credentials.json"
    creds_path.write_text(json.dumps({
        "installed": {
            "client_id": "fake-client",
            "client_secret": "fake-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }))

    monkeypatch.setattr(
        "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
        fake_from_client_secrets_file,
    )
    return fake_creds


@pytest.fixture
def mock_oauth_dropbox(monkeypatch):
    """Patch dropbox.DropboxOAuth2FlowNoRedirect so DropboxBackend.authenticate()
    doesn't prompt for a paste-back code or hit Dropbox servers."""
    fake_result = MagicMock()
    fake_result.refresh_token = "fake-dropbox-refresh-token"

    fake_flow = MagicMock()
    fake_flow.start.return_value = "https://www.dropbox.com/fake-auth-url"
    fake_flow.finish.return_value = fake_result

    def fake_flow_ctor(*args, **kwargs):
        return fake_flow

    monkeypatch.setattr("dropbox.DropboxOAuth2FlowNoRedirect", fake_flow_ctor)
    # Also patch input() so the prompt doesn't block
    monkeypatch.setattr("builtins.input", lambda *_: "fake-auth-code")
    return fake_result


@pytest.fixture
def mock_oauth_msal(monkeypatch):
    """Patch msal so OneDriveBackend.authenticate() doesn't perform a real
    device-code flow."""
    fake_app = MagicMock()
    fake_app.initiate_device_flow.return_value = {
        "user_code": "FAKECODE",
        "verification_uri": "https://microsoft.com/devicelogin",
        "device_code": "fake-device",
        "expires_in": 900,
        "interval": 5,
        "message": "Visit URL and enter code",
    }
    fake_app.acquire_token_by_device_flow.return_value = {
        "access_token": "fake-onedrive-access",
        "refresh_token": "fake-onedrive-refresh",
        "id_token_claims": {"oid": "fake-user-id"},
    }
    fake_app.token_cache = MagicMock()
    fake_app.token_cache.serialize.return_value = '{"AccessToken": {}}'

    def fake_pca(*args, **kwargs):
        return fake_app

    monkeypatch.setattr("msal.PublicClientApplication", fake_pca)
    return fake_app


@pytest.fixture
def mock_oauth_webdav(monkeypatch):
    """Patch the WebDAV PROPFIND test connection in WebDAVBackend.authenticate()
    so it accepts whatever credentials the test provides."""
    fake_response = MagicMock()
    fake_response.status_code = 207
    fake_response.raise_for_status = MagicMock()

    fake_session = MagicMock()
    fake_session.request.return_value = fake_response

    # Patch _make_session on WebDAVBackend (the helper that builds a Session)
    monkeypatch.setattr(
        "claude_mirror.backends.webdav.WebDAVBackend._make_session",
        lambda self, username, password: fake_session,
    )
    # Patch getpass so the password prompt doesn't block
    monkeypatch.setattr("getpass.getpass", lambda *_: "fake-webdav-password")
    monkeypatch.setattr("builtins.input", lambda *_: "fake-webdav-user")
    return fake_session
