"""Tests for the deep OneDrive checks added to `claude-mirror doctor`.

The generic doctor checks (config/credentials/token/connectivity/project/
manifest) live in test_doctor.py. This module covers the ONEDRIVE-ONLY
deep checks layered on top:

  1. Token cache integrity (MSAL deserialization + at least one cached
     account).
  2. Azure client_id format valid (GUID).
  3. Granted scopes match config (Files.ReadWrite or Files.ReadWrite.All).
  4. Token still refreshable (`acquire_token_silent`).
  5. Drive item access via Microsoft Graph (`me/drive/root:/{folder}`).
  6. Drive item type (folder vs file vs unknown).

All MSAL + Microsoft Graph calls are mocked — the tests are offline,
deterministic, and well under 100ms each. The single mock seam is
`claude_mirror.cli._onedrive_deep_check_factory`, which returns the
MSAL app + cached account bundle that the deep checker consumes. The
Graph drive-item probe is mocked separately by patching
`requests.get` (lazily imported inside the deep-check function).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
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

    monkeypatch.setattr(cli_mod, "console", Console(force_terminal=True, width=400))


# Click 8.3+ emits a DeprecationWarning from inside CliRunner.invoke; the
# project's pytest config promotes warnings to errors. Suppress here only.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


_VALID_CLIENT_ID = "9d7d6034-3524-4dce-b0f0-2a67f9e7b409"


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    credentials_file: Optional[Path] = None,
    onedrive_client_id: str = _VALID_CLIENT_ID,
    onedrive_folder: str = "claude-mirror/myproject",
    machine_name: str = "test-machine",
) -> Path:
    """Write a minimal OneDrive YAML config that exercises the deep checks.

    OneDrive doesn't actually use a separate credentials.json file (the
    Azure client_id is in the YAML itself), but the generic doctor's
    Check 2 still asserts presence of `credentials_file` for the
    googledrive/dropbox/onedrive triad. We point it at a tmp-path stub
    so the generic check passes and the deep section actually runs.
    """
    data: dict = {
        "project_path": str(project_path),
        "backend": "onedrive",
        "onedrive_client_id": onedrive_client_id,
        "onedrive_folder": onedrive_folder,
        "token_file": str(token_file),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": machine_name,
        "user": "test-user",
    }
    if credentials_file is not None:
        data["credentials_file"] = str(credentials_file)
    path.write_text(yaml.safe_dump(data))
    return path


def _write_token(path: Path) -> None:
    """Drop a fake OneDrive token cache JSON. The deep-check factory is
    mocked, so the contents don't have to be a real MSAL cache; we just
    need the file to exist + parse so the generic checks pass through."""
    payload: dict = {
        "client_id": "fake-client-id",
        "token_cache": "{}",
        "refresh_token": "fake-refresh-token",
    }
    path.write_text(json.dumps(payload))


# Stand-in storage for the generic connectivity probe — the deep tests
# all need this to "pass" so the deep section runs cleanly.
class _OkStorage:
    backend_name = "fake"

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def list_folders(self, parent_id: str, name: Any = None) -> list:
        return []

    def classify_error(self, exc: BaseException) -> Any:
        from claude_mirror.backends import ErrorClass

        return ErrorClass.UNKNOWN


def _patch_storage_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: _OkStorage())


_UNSET = object()  # sentinel — distinguish "not passed" from "passed None"


def _patch_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    app: Optional[MagicMock] = None,
    app_error: Optional[BaseException] = None,
    account: Any = _UNSET,
    cache_error: Optional[BaseException] = None,
    cached_count: int = 1,
    silent_result: Any = _UNSET,
    silent_raises: Optional[BaseException] = None,
    scopes: Optional[List[str]] = None,
) -> MagicMock:
    """Replace `_onedrive_deep_check_factory` with a stub returning the
    supplied app/account/cache_error. Returns the app so tests can
    inspect call-count/args.

    `silent_result` defaults to a fresh access_token-bearing dict.
    Pass `silent_result=None` explicitly to test the "refresh failed"
    bucket path. `silent_raises` overrides — when set,
    app.acquire_token_silent raises the supplied exception.
    `scopes` populates the cached-account dict's "scopes" field, so the
    granted-scopes check sees them.
    """
    if app is None:
        app = MagicMock(name="msal-app")
        if silent_raises is not None:
            app.acquire_token_silent.side_effect = silent_raises
        elif silent_result is _UNSET:
            app.acquire_token_silent.return_value = {
                "access_token": "fake-access-token",
                "expires_in": 3600,
            }
        else:
            # Explicit value — including None — for the refresh-failed path.
            app.acquire_token_silent.return_value = silent_result
        # Default token cache shape — empty list of access tokens.
        app.token_cache.find = MagicMock(return_value=[])

    if account is _UNSET:
        if cached_count > 0:
            account = {
                "username": "alice@example.com",
                "scopes": " ".join(
                    scopes if scopes is not None else ["Files.ReadWrite"]
                ),
            }
        else:
            account = None

    def _factory(config: Any, token_path: Any) -> dict:
        return {
            "app": app,
            "app_error": app_error,
            "account": account,
            "cache_error": cache_error,
            "cached_count": cached_count if account is not None else 0,
        }

    monkeypatch.setattr(cli_mod, "_onedrive_deep_check_factory", _factory)
    return app


def _patch_graph_get(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    json_payload: Optional[dict] = None,
    raises: Optional[BaseException] = None,
) -> MagicMock:
    """Replace `requests.get` (the function used by the deep-check
    Graph probe) so the drive-item endpoint either returns a stubbed
    Response or raises the supplied exception."""
    import requests as _requests

    if raises is not None:
        fake_get = MagicMock(side_effect=raises)
    else:
        if json_payload is None:
            json_payload = {"folder": {"childCount": 3}, "name": "myproject"}
        fake_resp = MagicMock()
        fake_resp.status_code = status_code
        fake_resp.json.return_value = json_payload
        fake_get = MagicMock(return_value=fake_resp)

    monkeypatch.setattr(_requests, "get", fake_get)
    return fake_get


def _build_healthy_config(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    return _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


def test_deep_all_pass_on_fully_configured_onedrive_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: valid client_id, cached account, scopes present,
    silent refresh succeeds, Graph returns 200 with a folder shape.
    Exit 0; output shows every deep-check line as ✓."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_graph_get(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "OneDrive deep checks" in out
    assert "Token cache valid" in out
    assert "1 cached account" in out
    assert "Azure client_id format valid" in out
    assert "Scopes: Files.ReadWrite" in out
    assert "Token refreshable" in out
    assert "OneDrive folder accessible" in out
    assert "/claude-mirror/myproject" in out
    assert "Drive item type: folder" in out
    assert "All checks passed" in out


def test_deep_invalid_client_id_format_short_circuits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-GUID `onedrive_client_id` ⇒ doctor surfaces the validation
    failure early and skips the rest of the deep section (no point
    attempting MSAL with a bad client_id)."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        onedrive_client_id="not-a-guid",
    )

    _patch_storage_ok(monkeypatch)

    # The factory needs to actually run for this case — we want the real
    # regex validation path to trip. So we DON'T patch the factory.
    # The factory will try to import msal but fail gracefully because
    # we hit the regex check first.

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Azure client_id has invalid format" in out
    assert "'not-a-guid'" in out
    # Subsequent deep-check lines must NOT appear.
    assert "Token refreshable" not in out
    assert "OneDrive folder accessible" not in out


def test_deep_token_cache_no_accounts_emits_auth_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty MSAL cache (no cached accounts) ⇒ doctor emits a clear
    "run claude-mirror auth" hint and skips remaining checks."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    # Wire factory: app constructs OK but get_accounts returns []
    app = MagicMock(name="msal-app")
    _patch_factory(
        monkeypatch,
        app=app,
        account=None,
        cached_count=0,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "no cached accounts" in out
    assert "claude-mirror auth" in out
    # Silent-token must NOT have been attempted.
    assert app.acquire_token_silent.call_count == 0


def test_deep_corrupt_token_cache_reports_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt token cache file ⇒ doctor reports "Token cache
    unreadable" with the parsed exception type, and skips remaining
    checks."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    # Surface a cache_error from the factory.
    cache_err = ValueError("bad cache shape")
    _patch_factory(
        monkeypatch,
        app=MagicMock(name="msal-app"),
        cache_error=cache_err,
        cached_count=0,
        account=None,
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Token cache unreadable" in out
    assert "ValueError" in out
    assert "claude-mirror auth" in out


def test_deep_silent_token_returns_none_buckets_as_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`acquire_token_silent` returning None ⇒ AUTH bucket fail; the
    drive-item probe must not run after the bucket fires."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    app = _patch_factory(monkeypatch, silent_result=None)
    # Drive-item probe must NOT be called once auth-bucket fires.
    fake_get = _patch_graph_get(
        monkeypatch,
        raises=AssertionError(
            "requests.get must NOT be called after auth-bucket triggers"
        ),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "OneDrive auth failed" in out
    assert "claude-mirror auth" in out
    # Confirm short-circuit — Graph probe never fired.
    assert fake_get.call_count == 0


def test_deep_silent_token_returns_error_dict_buckets_as_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`acquire_token_silent` returning a dict with `error` ⇒ AUTH bucket
    fail; the error code/description from MSAL surfaces in the message."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    err_result = {
        "error": "invalid_grant",
        "error_description": (
            "AADSTS70008: The provided authorization code or refresh "
            "token has expired."
        ),
    }
    _patch_factory(monkeypatch, silent_result=err_result)
    fake_get = _patch_graph_get(
        monkeypatch,
        raises=AssertionError(
            "requests.get must NOT be called after auth-bucket triggers"
        ),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "OneDrive auth failed" in out
    assert "invalid_grant" in out
    assert "AADSTS70008" in out
    assert fake_get.call_count == 0


def test_deep_graph_404_emits_create_folder_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Microsoft Graph returning 404 for the drive-item endpoint ⇒
    doctor emits ✗ "Drive item access: HTTP 404" with a fix that points
    at creating the folder via the OneDrive web UI or via push."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_graph_get(monkeypatch, status_code=404)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Drive item access: HTTP 404" in out
    assert "OneDrive folder doesn't exist" in out
    assert "/claude-mirror/myproject" in out
    assert "OneDrive web UI" in out
    assert "claude-mirror push" in out


def test_deep_graph_401_buckets_as_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Microsoft Graph returning 401 ⇒ AUTH bucket fail (the
    access_token we just acquired is somehow rejected — typically a
    tenant/scope mismatch)."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_graph_get(monkeypatch, status_code=401)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "OneDrive auth failed" in out
    assert "HTTP 401" in out
    assert "claude-mirror auth" in out


def test_deep_graph_500_classified_as_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Microsoft Graph returning 5xx ⇒ TRANSIENT classification; doctor
    suggests retry and points at the Office 365 status page."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)
    _patch_graph_get(monkeypatch, status_code=503)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Drive item access: HTTP 503" in out
    assert "transient" in out.lower()
    assert "retry" in out
    assert "status.office.com" in out


def test_deep_auth_bucketing_emits_only_one_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When silent-token returns None, the OneDrive auth bucket fires
    exactly once; we never reach the drive-item probe (which would
    contribute a second AUTH-class failure if reached)."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch, silent_result=None)
    # The graph probe is wired to ALSO return 401, which would trigger
    # a second auth-bucket call. The short-circuit on the silent-token
    # failure must prevent that — only ONE bucket line should appear.
    _patch_graph_get(monkeypatch, status_code=401)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    bucket_count = out.count("OneDrive auth failed")
    assert bucket_count == 1, (
        f"expected exactly one OneDrive auth bucket line, "
        f"got {bucket_count}\n\noutput:\n{out}"
    )


def test_deep_skipped_when_onedrive_folder_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When `onedrive_folder` is empty in the YAML, the deep section
    emits a yellow info line and adds NO failures — there's nothing
    to probe without a folder, but the user may simply have a malformed
    config that the wizard will fill in next."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        onedrive_folder="",  # empty → deep section bails with yellow info
    )

    _patch_storage_ok(monkeypatch)

    # Factory stub that fails loudly if invoked — proves the deep
    # section short-circuits before doing any MSAL work.
    def _exploding_factory(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError(
            "_onedrive_deep_check_factory must NOT be called when "
            "onedrive_folder is empty"
        )

    monkeypatch.setattr(
        cli_mod, "_onedrive_deep_check_factory", _exploding_factory
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "onedrive"]
    )

    # No deep-section failures — generic checks would still run, but
    # a missing token or backend connectivity might surface — those
    # are not the concern of this test.
    out = _strip_ansi(result.output)
    assert "OneDrive folder not configured" in out
    assert "skipping deep OneDrive checks" in out


def test_deep_skipped_for_non_onedrive_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deep checks must NOT run for googledrive / dropbox / webdav /
    sftp — they are OneDrive-specific. We confirm by writing a dropbox
    config and a factory-stub that would fail loudly if invoked."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = tmp_path / "config.yaml"
    data = {
        "project_path": str(project),
        "backend": "dropbox",
        "dropbox_folder": "/claude-mirror/test",
        "credentials_file": str(creds),
        "token_file": str(token),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    cfg_path.write_text(yaml.safe_dump(data))

    _patch_storage_ok(monkeypatch)

    def _exploding_factory(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError(
            "_onedrive_deep_check_factory must NOT be called for "
            "non-onedrive backends"
        )

    monkeypatch.setattr(
        cli_mod, "_onedrive_deep_check_factory", _exploding_factory
    )

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])

    # We don't assert exit code here — generic checks for the dropbox
    # backend may pass or fail depending on the test environment.
    # The point is that the OneDrive deep section never tried to run.
    out = _strip_ansi(result.output)
    assert "OneDrive deep checks" not in out
