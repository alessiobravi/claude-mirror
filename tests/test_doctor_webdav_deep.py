"""Tests for the deep WebDAV checks added to `claude-mirror doctor`.

The generic doctor checks (config/credentials/token/connectivity/project/
manifest) live in test_doctor.py. This module covers the WEBDAV-ONLY
deep checks layered on top:

  1. URL well-formed (https:// + netloc + path).
  2. PROPFIND on the configured root returns HTTP 207.
  3. DAV class header detection.
  4. ETag header presence on the root resource.
  5. oc:checksums extension support detection (Nextcloud / OwnCloud).
  6. Account-level PROPFIND for Nextcloud / OwnCloud-shaped URLs.

All HTTP calls are mocked with the `responses` library — the tests are
offline, deterministic, and well under 100ms each.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import responses
import yaml
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force a very wide terminal so long absolute tmp_path strings render
    on a single line. Otherwise Rich wraps and breaks substring asserts."""
    from rich.console import Console

    monkeypatch.setattr(
        cli_mod, "console", Console(force_terminal=True, width=400)
    )


# Click 8.3+ emits a DeprecationWarning from inside CliRunner.invoke; the
# project's pytest config promotes warnings to errors. Suppress here only.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# Canonical Nextcloud URL the suite uses by default — chosen to match
# the URL pattern detected by the account-level smoke test (Check 6).
_NEXTCLOUD_BASE = "https://nextcloud.example.com/remote.php/dav/files/alice"
_PROJECT_URL = f"{_NEXTCLOUD_BASE}/myproject"


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    webdav_url: str = _PROJECT_URL,
    webdav_username: str = "alice",
    webdav_password: str = "secret",
    webdav_insecure_http: bool = False,
) -> Path:
    """Write a minimal WebDAV YAML config that exercises the deep checks."""
    data: dict = {
        "project_path": str(project_path),
        "backend": "webdav",
        "webdav_url": webdav_url,
        "webdav_username": webdav_username,
        "webdav_password": webdav_password,
        "webdav_insecure_http": webdav_insecure_http,
        "token_file": str(token_file),
        "credentials_file": "",
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    path.write_text(yaml.safe_dump(data))
    return path


def _build_healthy_config(
    tmp_path: Path,
    *,
    webdav_url: str = _PROJECT_URL,
    webdav_username: str = "alice",
    webdav_password: str = "secret",
    webdav_insecure_http: bool = False,
) -> Path:
    """Build a fully-healthy on-disk config (project dir, token file,
    YAML), so each test only has to vary the HTTP-level mocks."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text(json.dumps({
        "username": webdav_username,
        "password": webdav_password,
    }))
    return _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
        webdav_insecure_http=webdav_insecure_http,
    )


def _propfind_xml(
    *,
    href: str,
    etag: str = "abc123",
    checksums: str | None = "SHA1:abc MD5:def SHA256:ghi",
) -> str:
    """Build a 207 Multi-Status PROPFIND body with an optional
    `<oc:checksums>` element."""
    cks_block = ""
    if checksums:
        cks_block = (
            '<oc:checksums xmlns:oc="http://owncloud.org/ns">'
            f"{checksums}"
            "</oc:checksums>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:multistatus xmlns:d="DAV:">'
        "<d:response>"
        f"<d:href>{href}</d:href>"
        "<d:propstat>"
        "<d:prop>"
        "<d:resourcetype><d:collection/></d:resourcetype>"
        f"<d:getetag>&quot;{etag}&quot;</d:getetag>"
        "<d:getlastmodified>Tue, 05 May 2026 12:00:00 GMT</d:getlastmodified>"
        f"{cks_block}"
        "</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status>"
        "</d:propstat>"
        "</d:response>"
        "</d:multistatus>"
    )


def _add_propfind(
    url: str,
    *,
    status: int = 207,
    body: str | None = None,
    dav_header: str = "1, 2, 3",
    etag_header: str = '"abc123"',
    checksums: str | None = "SHA1:abc MD5:def SHA256:ghi",
) -> None:
    """Register a PROPFIND mock on `url` with conventional default
    headers (DAV class 1/2/3 + ETag + 207 multistatus body containing
    oc:checksums)."""
    if body is None:
        body = _propfind_xml(href=url, checksums=checksums)
    headers = {}
    if dav_header is not None:
        headers["DAV"] = dav_header
    if etag_header is not None:
        headers["ETag"] = etag_header
    responses.add(
        responses.Response(
            method="PROPFIND",
            url=url,
            status=status,
            body=body,
            headers=headers,
            content_type="application/xml; charset=utf-8",
        )
    )


def _patch_storage_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub `_create_storage` so the generic connectivity check never
    actually tries to reach the network. The WebDAV deep checks do
    their HTTP calls themselves."""
    class _OkStorage:
        backend_name = "webdav"

        def authenticate(self):  # noqa: D401
            return self

        def get_credentials(self):  # noqa: D401
            return self

        def list_folders(self, parent_id, name=None):  # noqa: D401
            return []

        def classify_error(self, exc):  # noqa: D401
            from claude_mirror.backends import ErrorClass
            return ErrorClass.UNKNOWN

    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: _OkStorage())


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


@responses.activate
def test_deep_all_pass_on_healthy_nextcloud_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: well-formed URL + 207 PROPFIND + DAV class 1/2/3 +
    ETag + oc:checksums + Nextcloud account base reachable.
    Exit 0; output shows every deep-check line as ✓."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL)
    # Account-level (Check 6) — different URL from the project.
    _add_propfind(_NEXTCLOUD_BASE + "/")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "WebDAV deep checks" in out
    assert "URL well-formed" in out
    assert _PROJECT_URL in out
    assert "PROPFIND succeeded; HTTP 207" in out
    assert "DAV class: 1, 2, 3" in out
    assert "ETag header present" in out
    assert "oc:checksums extension supported" in out
    assert "Account-level PROPFIND succeeded" in out
    assert "All checks passed" in out


@responses.activate
def test_deep_url_malformed_fails_at_validation_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage URL (no scheme, no netloc) must fail Check 1 immediately
    without making any network call — and the deep section must short-
    circuit the remaining checks."""
    cfg = _build_healthy_config(tmp_path, webdav_url="not-a-url")
    _patch_storage_ok(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "WebDAV URL malformed" in out
    # No PROPFIND was attempted — `responses` is strict and any unmatched
    # request would raise. Confirm no calls at all.
    assert len(responses.calls) == 0
    # Subsequent checks must not run.
    assert "PROPFIND succeeded" not in out
    assert "DAV class" not in out


@responses.activate
def test_deep_propfind_401_emits_auth_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 401 from the configured root's PROPFIND surfaces ONE auth-bucket
    failure line with the canonical fix command. Subsequent checks
    (DAV class / ETag / oc:checksums / account-level) are skipped."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL, status=401, body="Unauthorized")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "PROPFIND failed: HTTP 401" in out
    assert "Credentials rejected" in out
    assert "claude-mirror auth" in out
    # No further deep-check lines after the auth-bucket bail.
    assert "DAV class" not in out
    assert "ETag header present" not in out


@responses.activate
def test_deep_propfind_404_says_root_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """404 ⇒ "configured WebDAV root doesn't exist" with a fix that
    points at fixing the URL (or creating the folder server-side)."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL, status=404, body="Not Found")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "PROPFIND failed: HTTP 404" in out
    assert "Configured WebDAV root doesn't exist" in out
    # Subsequent checks must not run.
    assert "DAV class" not in out


@responses.activate
def test_deep_propfind_405_says_server_does_not_support_propfind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """405 ⇒ "server doesn't support PROPFIND" — typically a misconfigured
    WebDAV endpoint that's actually serving plain HTTP."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL, status=405, body="Method Not Allowed")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "PROPFIND failed: HTTP 405" in out
    assert "Server doesn't support PROPFIND" in out


@responses.activate
def test_deep_propfind_5xx_surfaces_transient_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """5xx ⇒ classified as transient with a "retry" hint, not a permanent
    failure. The deep section short-circuits to avoid cascading errors."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL, status=503, body="Service Unavailable")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "PROPFIND failed: HTTP 503 (transient)" in out
    assert "retry" in out


@responses.activate
def test_deep_dav_class_missing_emits_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `DAV:` response header ⇒ yellow warning info line, but NOT a
    failure (some servers don't advertise it; basic operations may
    still work)."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL, dav_header=None)
    _add_propfind(_NEXTCLOUD_BASE + "/", dav_header=None)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "no DAV class header reported by server" in out
    # Other checks must still pass — this is informational only.
    assert "PROPFIND succeeded; HTTP 207" in out


@responses.activate
def test_deep_dav_class_zero_only_emits_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DAV header lacking class 1 (e.g. just `0`) is suspicious —
    surface a yellow warning but don't fail the check."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    _add_propfind(_PROJECT_URL, dav_header="0")
    _add_propfind(_NEXTCLOUD_BASE + "/", dav_header="0")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "does NOT list class 1" in out


@responses.activate
def test_deep_etag_missing_emits_info_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ETag header AND no <d:getetag/> in the body ⇒ info line
    explaining the change-detection fallback. Not a failure."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    # Body without a getetag element.
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:multistatus xmlns:d="DAV:">'
        "<d:response>"
        f"<d:href>{_PROJECT_URL}</d:href>"
        "<d:propstat>"
        "<d:prop>"
        "<d:resourcetype><d:collection/></d:resourcetype>"
        "</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status>"
        "</d:propstat>"
        "</d:response>"
        "</d:multistatus>"
    )
    _add_propfind(
        _PROJECT_URL,
        body=body,
        etag_header=None,
    )
    _add_propfind(_NEXTCLOUD_BASE + "/")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "no ETag returned" in out
    assert "fall back to last-modified" in out
    # Final result must still be all-pass.
    assert "All checks passed" in out


@responses.activate
def test_deep_oc_checksums_absent_emits_info_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vanilla Apache mod_dav servers don't expose oc:checksums. Doctor
    surfaces a dim info line and continues. Not a failure."""
    # Use a non-Nextcloud-pattern URL so Check 6 is also skipped.
    cfg = _build_healthy_config(
        tmp_path,
        webdav_url="https://apache.example.com/dav/myproject",
    )
    _patch_storage_ok(monkeypatch)

    _add_propfind(
        "https://apache.example.com/dav/myproject",
        checksums=None,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "oc:checksums extension not advertised" in out
    # Account-level check (Check 6) is NOT triggered for this URL.
    assert "Account-level PROPFIND" not in out


@responses.activate
def test_deep_auth_bucket_groups_two_401s_into_one_action_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two separate 401-emitting PROPFIND calls (root + account-level)
    must produce exactly ONE auth-bucket line. The root-level 401 fires
    the bucket and short-circuits before the account-level call runs —
    confirming bucketing prevents cascading copies of the same fix."""
    cfg = _build_healthy_config(tmp_path)
    _patch_storage_ok(monkeypatch)

    # Configure responses to return 401 on the root PROPFIND.
    _add_propfind(_PROJECT_URL, status=401, body="Unauthorized")
    # If the deep check were buggy and tried the account-level URL after
    # the root 401, this 401 would either appear or `responses` would
    # raise on the unmatched call. We register it to be defensive about
    # the assertion shape, then verify only ONE bucket line exists.
    _add_propfind(_NEXTCLOUD_BASE + "/", status=401, body="Unauthorized")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    bucket_count = out.count("Credentials rejected")
    assert bucket_count == 1, (
        f"expected exactly one auth-bucket line, got {bucket_count}\n\n"
        f"output:\n{out}"
    )
    assert "claude-mirror auth" in out


@responses.activate
def test_deep_account_level_check_skipped_for_non_nextcloud_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare Apache mod_dav URL (no `/remote.php/dav/files/USER/`
    pattern) must NOT trigger the Check-6 account-level smoke test —
    that probe is Nextcloud / OwnCloud only."""
    cfg = _build_healthy_config(
        tmp_path,
        webdav_url="https://apache.example.com/dav/myproject",
    )
    _patch_storage_ok(monkeypatch)

    _add_propfind("https://apache.example.com/dav/myproject")

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "webdav"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Account-level PROPFIND" not in out
    # Exactly one PROPFIND call was made — the project root.
    assert len(responses.calls) == 1


@responses.activate
def test_deep_skipped_for_non_webdav_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dropbox config must NOT trigger the WebDAV deep section, even
    if the WebDAV check function would explode. Belt-and-braces."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"refresh_token": "r"}))
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = tmp_path / "config.yaml"
    data = {
        "project_path": str(project),
        "backend": "dropbox",
        "dropbox_folder": "/claude-mirror/test",
        "dropbox_app_key": "abc1234567",
        "credentials_file": str(creds),
        "token_file": str(token),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    cfg_path.write_text(yaml.safe_dump(data))

    # With DOC-DBX shipped alongside DOC-WD, a dropbox config would
    # otherwise trigger dropbox deep checks and need separate mocking.
    # This test only cares about the webdav skip invariant — stub the
    # dropbox deep helper so it doesn't reach for a real dropbox client.
    monkeypatch.setattr(cli_mod, "_run_dropbox_deep_checks", lambda *a, **k: [])

    _patch_storage_ok(monkeypatch)

    # A loud factory that would raise if invoked — proves we don't run
    # the WebDAV deep section for a Dropbox config.
    def _exploding_deep(*_args, **_kwargs):
        raise AssertionError(
            "_run_webdav_deep_checks must NOT be called for non-webdav"
        )

    monkeypatch.setattr(cli_mod, "_run_webdav_deep_checks", _exploding_deep)

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output
