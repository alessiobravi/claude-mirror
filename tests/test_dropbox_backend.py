"""Per-backend smoke tests for DropboxBackend.

Skipped entirely if the `dropbox` SDK is not installed (the file uses
`pytest.importorskip` at module level so collection itself is safe).

We mock the `dropbox.Dropbox` constructor so no SDK initialisation hits
the network. The backend stores its client in `self._dbx`, and the
`dbx` property lazily calls `get_credentials()` if `_dbx` is unset —
tests assign `_dbx` directly to a MagicMock to bypass that path.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

dropbox = pytest.importorskip("dropbox")

from claude_mirror.backends import ErrorClass
from claude_mirror.backends.dropbox import DropboxBackend


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_backend(make_config, config_dir: Path) -> DropboxBackend:
    cfg = make_config(
        backend="dropbox",
        dropbox_app_key="test-app-key",
        dropbox_folder="/claude-mirror/test",
        token_file=str(config_dir / "token.json"),
    )
    return DropboxBackend(cfg)


# ─── 1. authenticate completes PKCE flow ───────────────────────────────────────

def test_authenticate_completes_pkce_flow(
    make_config, config_dir, mock_oauth_dropbox, monkeypatch,
):
    """After authenticate(), the token file should hold app_key + refresh_token."""
    # Patch the dropbox.Dropbox client constructor so it doesn't try to talk
    # to the network when authenticate() builds the post-flow client.
    monkeypatch.setattr(
        "claude_mirror.backends.dropbox.dropbox.Dropbox",
        lambda *a, **kw: MagicMock(),
    )
    backend = _make_backend(make_config, config_dir)
    backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    data = json.loads(token_path.read_text())
    assert data["app_key"] == "test-app-key"
    assert data["refresh_token"] == "fake-dropbox-refresh-token"


# ─── 2. get_credentials returns a Dropbox-ish client ───────────────────────────

def test_get_credentials_returns_dbx_client(
    make_config, config_dir, monkeypatch,
):
    """A pre-written token file → get_credentials() returns a Dropbox object
    (we just assert the constructor was called with the saved refresh_token)."""
    backend = _make_backend(make_config, config_dir)
    Path(backend.config.token_file).write_text(json.dumps({
        "app_key": "saved-app-key",
        "refresh_token": "saved-refresh-token",
    }))

    seen_kwargs: dict = {}

    def fake_dropbox_ctor(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return MagicMock(name="DropboxClient")

    monkeypatch.setattr(
        "claude_mirror.backends.dropbox.dropbox.Dropbox", fake_dropbox_ctor,
    )

    client = backend.get_credentials()
    assert client is not None
    assert seen_kwargs.get("oauth2_refresh_token") == "saved-refresh-token"
    assert seen_kwargs.get("app_key") == "saved-app-key"


# ─── 3. upload_file calls files_upload ─────────────────────────────────────────

def test_upload_file_calls_files_upload(make_config, config_dir, project_dir):
    """A backend.upload_file() invocation should land exactly one
    `dbx.files_upload` call."""
    backend = _make_backend(make_config, config_dir)
    local = project_dir / "note.md"
    local.write_text("contents")

    fake_dbx = MagicMock()
    backend._dbx = fake_dbx

    file_id = backend.upload_file(str(local), "note.md", "/claude-mirror/test")
    assert fake_dbx.files_upload.call_count == 1
    # The returned 'id' is the Dropbox path.
    assert file_id == "/claude-mirror/test/note.md"


# ─── 4. download_file returns bytes ────────────────────────────────────────────

def test_download_file_returns_bytes(make_config, config_dir):
    """`dbx.files_download` returns `(metadata, response)`; the backend
    must extract `response.content` and round-trip it to the caller."""
    backend = _make_backend(make_config, config_dir)
    fake_meta = MagicMock(size=11)
    fake_response = MagicMock(content=b"hello world")
    fake_dbx = MagicMock()
    fake_dbx.files_download.return_value = (fake_meta, fake_response)
    backend._dbx = fake_dbx

    out = backend.download_file("/claude-mirror/test/note.md")
    assert out == b"hello world"


# ─── 5. classify_error: AuthError → AUTH ───────────────────────────────────────

def test_classify_error_AuthError_is_auth(make_config, config_dir):
    """A `dropbox.exceptions.AuthError` is the unambiguous re-auth signal."""
    from dropbox.exceptions import AuthError
    backend = _make_backend(make_config, config_dir)
    # AuthError(request_id, error) — error can be any object; we just need
    # the isinstance(exc, AuthError) check to fire.
    exc = AuthError("req-1", MagicMock())
    assert backend.classify_error(exc) == ErrorClass.AUTH
