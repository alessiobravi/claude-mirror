"""Per-backend smoke tests for WebDAVBackend.

WebDAV uses `requests.Session` directly, so we mock at the HTTP layer
with the `responses` library. Construction-time URL validation
(http:// vs https://) is the v0.5.6 fix; tests 3 and 4 lock that
behaviour in.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses

from claude_mirror.backends.webdav import WebDAVBackend


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_backend(
    make_config, config_dir: Path,
    *, url: str = "https://dav.example.com/remote.php/dav/files/u/test",
    insecure: bool = False,
) -> WebDAVBackend:
    cfg = make_config(
        backend="webdav",
        webdav_url=url,
        webdav_username="alice",
        webdav_password="secret",
        webdav_insecure_http=insecure,
        token_file=str(config_dir / "token.json"),
    )
    return WebDAVBackend(cfg)


# ─── 1. authenticate via PROPFIND succeeds on 207 ──────────────────────────────

@responses.activate
def test_authenticate_via_propfind_succeeds_on_207(make_config, config_dir):
    """A real PROPFIND that comes back 207 Multi-Status → token written
    with username + password persisted."""
    url = "https://dav.example.com/remote.php/dav/files/u/test"
    backend = _make_backend(make_config, config_dir, url=url)
    responses.add(
        responses.Response(
            method="PROPFIND", url=url, status=207, body="<d:multistatus/>",
        ),
    )

    backend.authenticate()

    token_path = Path(backend.config.token_file)
    assert token_path.exists()
    data = json.loads(token_path.read_text())
    assert data["username"] == "alice"
    assert data["password"] == "secret"


# ─── 2. authenticate raises on 401 ─────────────────────────────────────────────

@responses.activate
def test_authenticate_raises_on_401(make_config, config_dir):
    """A 401 from the server during authenticate() is the explicit
    'wrong creds' signal — backend must surface RuntimeError."""
    url = "https://dav.example.com/remote.php/dav/files/u/test"
    backend = _make_backend(make_config, config_dir, url=url)
    responses.add(
        responses.Response(method="PROPFIND", url=url, status=401),
    )

    with pytest.raises(RuntimeError, match="Authentication failed"):
        backend.authenticate()


# ─── 3. https required unless insecure_http ────────────────────────────────────

def test_https_required_unless_insecure_http(make_config, config_dir):
    """Constructor must reject http:// URLs by default — this is the v0.5.6
    fix that prevents cleartext basic-auth on insecure transports."""
    with pytest.raises(ValueError, match="https"):
        _make_backend(
            make_config, config_dir,
            url="http://dav.example.com/remote.php/dav/files/u/test",
            insecure=False,
        )


# ─── 4. http allowed when insecure_http=True ───────────────────────────────────

def test_http_allowed_when_insecure_http_true(make_config, config_dir):
    """Same http:// URL with the explicit opt-in flag must construct OK."""
    backend = _make_backend(
        make_config, config_dir,
        url="http://dav.example.com/remote.php/dav/files/u/test",
        insecure=True,
    )
    assert backend.config.webdav_url.startswith("http://")


# ─── 5. upload_file uses PUT ───────────────────────────────────────────────────

@responses.activate
def test_upload_file_uses_PUT(make_config, config_dir, project_dir):
    """upload_file must issue exactly one PUT to the encoded target URL."""
    base = "https://dav.example.com/remote.php/dav/files/u/test"
    backend = _make_backend(make_config, config_dir, url=base)
    # Inject a session so we don't run through get_credentials().
    import requests
    backend._session = requests.Session()

    local = project_dir / "note.md"
    local.write_text("hi")

    expected_url = f"{base}/note.md"
    responses.add(responses.PUT, expected_url, status=201)

    file_id = backend.upload_file(str(local), "note.md", "")
    # The dest_rel for an empty parent is just "/note.md" — the backend
    # treats `root_folder_id=""` as the bare URL root.
    assert "note.md" in file_id
    # Exactly one request, and it was a PUT to the right URL.
    assert len(responses.calls) == 1
    assert responses.calls[0].request.method == "PUT"
    assert responses.calls[0].request.url == expected_url
