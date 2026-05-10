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


# ─── 6. server-returned path validation (H6) ───────────────────────────────────

# A hostile or buggy WebDAV server can return `<href>` values that
# contain path traversal segments, absolute paths, NUL bytes, or
# similar shapes that would let a downstream caller read or write
# outside the project root. The backend rejects them at the boundary
# via `validate_server_rel_path` so a future caller that forgets to
# re-validate after `_safe_join` cannot be tricked. Each rejection
# raises BackendError(FILE_REJECTED) — bubbles up as the entire
# listing being unsafe, NOT as a silent skip.

import pytest as _pytest  # local alias to avoid clashing with the module-level import
from claude_mirror.backends import BackendError, ErrorClass


def _propfind_with_href(base: str, hrefs: list[str]) -> str:
    """Build a minimal multistatus PROPFIND response listing each href
    as a non-collection resource."""
    parts = []
    for href in hrefs:
        parts.append(
            f"<d:response>"
            f"  <d:href>{href}</d:href>"
            f"  <d:propstat>"
            f"    <d:prop>"
            f"      <d:resourcetype/>"
            f"      <d:getetag>\"abc\"</d:getetag>"
            f"    </d:prop>"
            f"    <d:status>HTTP/1.1 200 OK</d:status>"
            f"  </d:propstat>"
            f"</d:response>"
        )
    return (
        '<?xml version="1.0"?>'
        '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        + "".join(parts)
        + "</d:multistatus>"
    )


@responses.activate
def test_listing_rejects_parent_directory_traversal(make_config, config_dir):
    """Server returns `../../etc/passwd` → backend raises FILE_REJECTED."""
    base = "https://dav.example.com/remote.php/dav/files/u/test"
    backend = _make_backend(make_config, config_dir, url=base)
    import requests
    backend._session = requests.Session()

    body = _propfind_with_href(
        base, [f"{base}/../../etc/passwd"],
    )
    responses.add(
        responses.Response(method="PROPFIND", url=base, status=207, body=body),
    )
    with _pytest.raises(BackendError) as exc_info:
        backend.list_files_recursive("")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


@responses.activate
def test_listing_rejects_absolute_path(make_config, config_dir):
    """Server returns an href that decodes to a leading-slash absolute
    path → FILE_REJECTED."""
    base = "https://dav.example.com/remote.php/dav/files/u/test"
    backend = _make_backend(make_config, config_dir, url=base)
    import requests
    backend._session = requests.Session()

    # The href doesn't start with the configured base so _rel_from_url
    # falls through to `path.lstrip("/")`. We use a path that starts
    # with `/` and DOES NOT share the base prefix so the backend ends
    # up with `/etc/passwd` to validate, which then fails the absolute-
    # path check after lstrip... actually lstrip removes leading /, so
    # we need to use a path that explicitly contains `..` to hit the
    # absolute-shape rejection. Use a `\` (Windows-style) leader.
    body = _propfind_with_href(base, ["\\windows\\system32\\config"])
    responses.add(
        responses.Response(method="PROPFIND", url=base, status=207, body=body),
    )
    with _pytest.raises(BackendError) as exc_info:
        backend.list_files_recursive("")
    assert exc_info.value.error_class == ErrorClass.FILE_REJECTED


@responses.activate
def test_listing_rejects_normal_path_succeeds(make_config, config_dir):
    """Sanity check: a server returning a normal path is NOT rejected."""
    base = "https://dav.example.com/remote.php/dav/files/u/test"
    backend = _make_backend(make_config, config_dir, url=base)
    import requests
    backend._session = requests.Session()

    body = _propfind_with_href(base, [f"{base}/memory/notes.md"])
    responses.add(
        responses.Response(method="PROPFIND", url=base, status=207, body=body),
    )
    files = backend.list_files_recursive("")
    assert any(f["relative_path"] == "memory/notes.md" for f in files)
