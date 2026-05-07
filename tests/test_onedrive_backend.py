"""Per-backend smoke tests for OneDriveBackend.

Skipped if `msal` is not installed. The backend uses `requests.Session`
under the hood so we can mock at the HTTP layer with `responses`.

Auth tests stub `msal.PublicClientApplication` via the `mock_oauth_msal`
fixture from conftest, so the device-code flow doesn't actually sit and
poll Microsoft's servers.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import responses

msal = pytest.importorskip("msal")

from claude_mirror.backends.onedrive import OneDriveBackend, GRAPH_BASE


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_backend(make_config, config_dir: Path) -> OneDriveBackend:
    cfg = make_config(
        backend="onedrive",
        onedrive_client_id="test-client-id",
        onedrive_folder="claude-mirror/test",
        token_file=str(config_dir / "token.json"),
    )
    return OneDriveBackend(cfg)


# ─── 1. authenticate completes device flow ─────────────────────────────────────

def test_authenticate_completes_device_flow(
    make_config, config_dir, mock_oauth_msal,
):
    """After authenticate(), token file should contain client_id + token_cache."""
    backend = _make_backend(make_config, config_dir)
    backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    data = json.loads(token_path.read_text())
    assert data["client_id"] == "test-client-id"
    assert "token_cache" in data


# ─── 2. get_credentials loads token cache ──────────────────────────────────────

def test_get_credentials_loads_token_cache(
    make_config, config_dir, monkeypatch,
):
    """A pre-written token file → get_credentials() yields a usable session.

    We can't easily build a real msal SerializableTokenCache that
    acquire_token_silent is happy with, so we patch the PCA + cache
    behaviour to short-circuit it.
    """
    backend = _make_backend(make_config, config_dir)
    Path(backend.config.token_file).write_text(json.dumps({
        "client_id": "test-client-id",
        "token_cache": "{}",
    }))

    fake_app = MagicMock()
    fake_app.get_accounts.return_value = [MagicMock()]
    fake_app.acquire_token_silent.return_value = {"access_token": "cached-access"}
    fake_app.token_cache.serialize.return_value = "{}"
    monkeypatch.setattr("msal.PublicClientApplication", lambda *a, **kw: fake_app)

    session = backend.get_credentials()
    assert session.headers["Authorization"] == "Bearer cached-access"


# ─── 3. upload_file under 4MB uses simple PUT ──────────────────────────────────

@responses.activate
def test_upload_file_simple_under_4mb(make_config, config_dir, project_dir):
    """A small file → single PUT to /me/drive/root:/<path>:/content."""
    backend = _make_backend(make_config, config_dir)
    # Inject a session that already carries an Authorization header so we
    # don't go through the auth path.
    import requests
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer test"})
    backend._session = s

    local = project_dir / "note.md"
    local.write_text("hello")  # 5 bytes — well under 4MB

    expected_url = (
        f"{GRAPH_BASE}/me/drive/root:/claude-mirror/test/note.md:/content"
    )
    responses.add(
        responses.PUT, expected_url,
        json={"id": "new-onedrive-id"}, status=201,
    )

    file_id = backend.upload_file(str(local), "note.md", "")
    assert file_id == "note.md"
    assert len(responses.calls) == 1
    assert responses.calls[0].request.url == expected_url


# ─── 4. upload_file over 4MB uses upload session ───────────────────────────────

@responses.activate
def test_upload_file_chunked_over_4mb(make_config, config_dir, project_dir):
    """Files >= 4MB use createUploadSession + chunked PUT with Content-Range."""
    backend = _make_backend(make_config, config_dir)
    import requests
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer test"})
    backend._session = s

    # 5MB of content — forces the upload-session code path.
    big = b"A" * (5 * 1024 * 1024)
    local = project_dir / "big.bin"
    local.write_bytes(big)

    create_session_url = (
        f"{GRAPH_BASE}/me/drive/root:/claude-mirror/test/big.bin:/createUploadSession"
    )
    upload_url = "https://example.com/upload-session/abc"

    responses.add(
        responses.POST, create_session_url,
        json={"uploadUrl": upload_url}, status=200,
    )
    # Chunk PUT (single chunk since file is < 10MB).
    responses.add(
        responses.PUT, upload_url,
        json={"id": "big-id"}, status=201,
    )

    file_id = backend.upload_file(str(local), "big.bin", "")
    assert file_id == "big.bin"
    # Two HTTP calls: createUploadSession + chunk PUT.
    assert len(responses.calls) == 2
    chunk_request = responses.calls[1].request
    assert chunk_request.url == upload_url
    assert "Content-Range" in chunk_request.headers


# ─── 5. download_file returns content bytes ────────────────────────────────────

@responses.activate
def test_download_file_returns_content(make_config, config_dir):
    """GET /me/drive/root:/<path>:/content → content bytes round-tripped."""
    backend = _make_backend(make_config, config_dir)
    import requests
    s = requests.Session()
    s.headers.update({"Authorization": "Bearer test"})
    backend._session = s

    rel = "note.md"
    expected_url = f"{GRAPH_BASE}/me/drive/root:/claude-mirror/test/{rel}:/content"
    payload = b"downloaded content"
    responses.add(
        responses.GET, expected_url,
        body=payload, status=200,
        content_type="application/octet-stream",
    )

    out = backend.download_file(rel)
    assert out == payload
