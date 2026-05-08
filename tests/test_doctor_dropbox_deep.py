"""Tests for the deep Dropbox checks added to `claude-mirror doctor`.

The generic doctor checks (config/credentials/token/connectivity/project/
manifest) live in test_doctor.py. This module covers the DROPBOX-ONLY deep
checks layered on top:

  1. Token JSON shape — access_token or refresh_token present.
  2. App-key sanity — non-empty + matches the Dropbox short-alphanumeric
     format (10-20 lower-case alphanumeric chars).
  3. Account smoke test — `users_get_current_account` returns an Account
     with a populated `account_id`.
  4. Granted scopes — for PKCE tokens the configured operations
     (files.content.read / files.content.write) must be present; legacy
     tokens skip with an info line.
  5. Folder access — `files_list_folder(path=dropbox_folder, limit=1)`
     against the configured folder; classifies NotFound, permission
     denied, etc.
  6. Account type / team status — info line about team-admin policies
     when the authenticated account is a team member.

All Dropbox SDK calls are mocked — the tests are offline, deterministic,
and well under 100ms each. The single mock seam is `dropbox.Dropbox`,
which the deep checker constructs lazily inside the function so we patch
the import-site rather than wiring a parallel factory function.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
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


_DEFAULT_APP_KEY = "uao2pmhc0xgg2xj"  # 15 chars, lower-alnum — valid format


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    credentials_file: Path,
    dropbox_app_key: str = _DEFAULT_APP_KEY,
    dropbox_folder: str = "/claude-mirror/myproject",
    machine_name: str = "test-machine",
) -> Path:
    """Write a minimal Dropbox YAML config that exercises the deep checks."""
    data: dict = {
        "project_path": str(project_path),
        "backend": "dropbox",
        "dropbox_app_key": dropbox_app_key,
        "dropbox_folder": dropbox_folder,
        "credentials_file": str(credentials_file),
        "token_file": str(token_file),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": machine_name,
        "user": "test-user",
    }
    path.write_text(yaml.safe_dump(data))
    return path


def _write_token(
    path: Path,
    *,
    refresh_token: Optional[str] = "fake-refresh-token",
    access_token: Optional[str] = None,
    scope: Optional[str] = "files.content.read files.content.write account_info.read",
    app_key: Optional[str] = _DEFAULT_APP_KEY,
) -> None:
    """Drop a fake Dropbox token JSON. Default shape is the PKCE refresh-
    token format claude-mirror's auth flow writes; pass `refresh_token=None`
    + `access_token="..."` to simulate a legacy long-lived token, or
    `scope=None` to simulate a token with no scope field at all."""
    payload: Dict[str, Any] = {}
    if app_key is not None:
        payload["app_key"] = app_key
    if refresh_token is not None:
        payload["refresh_token"] = refresh_token
    if access_token is not None:
        payload["access_token"] = access_token
    if scope is not None:
        payload["scope"] = scope
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


def _make_account(
    *,
    account_id: str = "dbid:AAH123456",
    email: str = "alice@example.com",
    account_type: str = "basic",
    team: Any = None,
) -> MagicMock:
    """Build a fake FullAccount-shape: an object with `account_id`, `email`,
    `account_type` (a sub-object with `is_basic` / `is_pro` / `is_business`
    predicates), and `team` (None for personal accounts, non-None for team
    members)."""
    atype = MagicMock()
    atype.is_basic = MagicMock(return_value=(account_type == "basic"))
    atype.is_pro = MagicMock(return_value=(account_type == "pro"))
    atype.is_business = MagicMock(return_value=(account_type == "business"))

    account = MagicMock()
    account.account_id = account_id
    account.email = email
    account.account_type = atype
    account.team = team
    return account


def _patch_dropbox_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    account: Optional[Any] = None,
    account_exc: Optional[BaseException] = None,
    list_folder_exc: Optional[BaseException] = None,
) -> MagicMock:
    """Replace `dropbox.Dropbox` with a MagicMock factory so the deep
    check's lazy `import dropbox` picks up our stub. Returns the
    constructed CLIENT instance for inspection."""
    instance = MagicMock()
    if account_exc is not None:
        instance.users_get_current_account.side_effect = account_exc
    else:
        instance.users_get_current_account.return_value = (
            account if account is not None else _make_account()
        )
    if list_folder_exc is not None:
        instance.files_list_folder.side_effect = list_folder_exc
    else:
        # Empty folder is fine — we only assert no exception is raised.
        instance.files_list_folder.return_value = MagicMock(entries=[])

    fake_class = MagicMock(return_value=instance)
    import dropbox as _dropbox

    monkeypatch.setattr(_dropbox, "Dropbox", fake_class)
    return instance


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


def test_deep_all_pass_on_fully_configured_dropbox_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: refresh-token + valid app key + healthy account + scope
    set granted + folder accessible + personal account. Exit 0; output
    shows every deep-check line as ✓."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_dropbox_client(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Token JSON valid" in out
    assert "refresh_token present" in out
    assert "App key format valid" in out
    assert "Account: alice@example.com" in out
    assert "dbid:AAH123456" in out
    assert "Scopes: files.content.read, files.content.write" in out
    assert "Folder accessible: /claude-mirror/myproject" in out
    assert "Account type: personal" in out
    assert "All checks passed" in out


def test_deep_token_missing_both_access_and_refresh_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token JSON has neither `access_token` nor `refresh_token` ⇒ deep
    check 1 fails with a clear message and the auth fix; subsequent deep
    checks are short-circuited."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # Empty-ish token JSON: just an app_key, no auth material at all.
    token.write_text(json.dumps({"app_key": _DEFAULT_APP_KEY}))
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage_ok(monkeypatch)

    # Patch the SDK so a stray call would explode loudly — proves we
    # short-circuit before touching the network.
    instance = MagicMock()
    instance.users_get_current_account.side_effect = AssertionError(
        "users_get_current_account must NOT be called when token JSON "
        "lacks access_token AND refresh_token"
    )
    fake_class = MagicMock(return_value=instance)
    import dropbox as _dropbox
    monkeypatch.setattr(_dropbox, "Dropbox", fake_class)

    # Note: the generic check 3 already reports "token has no
    # refresh_token" first; we still expect the deep check to add its
    # OWN failure with a more specific message.
    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Token JSON missing both access_token and refresh_token" in out
    assert "claude-mirror auth" in out
    # Confirm we did NOT call into the SDK after the early bail.
    assert instance.users_get_current_account.call_count == 0


def test_deep_app_key_wrong_format_fails_at_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """App key has wrong format (contains uppercase or hyphens) ⇒ check 2
    fails with a fix pointing at the Dropbox app settings page; SDK is
    never invoked."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # Token has a valid refresh_token but with a deliberately broken
    # app_key field; the YAML's app_key is also broken so the deep
    # check's app_key resolution lands on the broken value.
    _write_token(token, app_key="UPPERCASE-AND-DASHES")
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
        dropbox_app_key="UPPERCASE-AND-DASHES",
    )

    _patch_storage_ok(monkeypatch)

    # Patch dropbox.Dropbox to explode if reached — short-circuit on
    # bad app_key MUST happen before any SDK call.
    instance = MagicMock()
    instance.users_get_current_account.side_effect = AssertionError(
        "users_get_current_account must NOT be called when app key is invalid"
    )
    fake_class = MagicMock(return_value=instance)
    import dropbox as _dropbox
    monkeypatch.setattr(_dropbox, "Dropbox", fake_class)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "dropbox_app_key" in out
    assert "format invalid" in out
    assert "https://www.dropbox.com/developers/apps" in out
    assert instance.users_get_current_account.call_count == 0


def test_deep_account_smoke_auth_error_buckets_into_one_action_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`users_get_current_account` raises AuthError ⇒ ONE bucketed
    "Dropbox auth failed" failure line; subsequent folder probe is
    skipped."""
    from dropbox.exceptions import AuthError

    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    # AuthError(request_id, error) — error can be any AuthError variant.
    auth_exc = AuthError("req-id-123", "invalid_access_token")
    instance = _patch_dropbox_client(
        monkeypatch,
        account_exc=auth_exc,
        # Folder probe MUST NOT be called after auth-bucket triggers.
        list_folder_exc=AssertionError(
            "files_list_folder must NOT be called after Dropbox "
            "auth-bucket triggers"
        ),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    # Exactly one bucket line — count occurrences of the bucket marker.
    bucket_count = out.count("Dropbox auth failed")
    assert bucket_count == 1, (
        f"expected exactly one auth-bucket line, got {bucket_count}\n\n"
        f"output:\n{out}"
    )
    assert "claude-mirror auth" in out
    # Folder probe must not have run.
    assert instance.files_list_folder.call_count == 0


def test_deep_folder_not_found_emits_create_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`files_list_folder` raises an ApiError whose underlying
    LookupError reports `is_not_found()` ⇒ deep check emits the
    "folder not found" hint pointing at creating the folder in
    Dropbox."""
    from dropbox.exceptions import ApiError

    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    # Build the typed-union mock the SDK exposes:
    #   ApiError.error → ListFolderError.is_path() == True
    #              → get_path() → LookupError.is_not_found() == True
    path_err = MagicMock()
    path_err.is_not_found = MagicMock(return_value=True)
    path_err.is_no_write_permission = MagicMock(return_value=False)

    list_err = MagicMock()
    list_err.is_path = MagicMock(return_value=True)
    list_err.get_path = MagicMock(return_value=path_err)

    api_exc = ApiError("req-id-1", list_err, "not found", "en")

    _patch_dropbox_client(monkeypatch, list_folder_exc=api_exc)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Folder not found in Dropbox" in out
    assert "/claude-mirror/myproject" in out
    assert "claude-mirror doctor" in out  # fix hint includes a doctor re-run


def test_deep_folder_permission_denied_emits_share_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`files_list_folder` raises an ApiError whose underlying
    LookupError reports `is_no_write_permission()` ⇒ deep check emits
    the "access denied" hint mentioning the authenticated account."""
    from dropbox.exceptions import ApiError

    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    path_err = MagicMock()
    path_err.is_not_found = MagicMock(return_value=False)
    path_err.is_no_write_permission = MagicMock(return_value=True)

    list_err = MagicMock()
    list_err.is_path = MagicMock(return_value=True)
    list_err.get_path = MagicMock(return_value=path_err)

    api_exc = ApiError("req-id-2", list_err, "access_denied", "en")

    _patch_dropbox_client(monkeypatch, list_folder_exc=api_exc)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Access denied on folder" in out
    assert "/claude-mirror/myproject" in out
    # Mentions the authenticated account so the user can match it
    # against their Dropbox sharing UI.
    assert "alice@example.com" in out
    assert "files.content.write" in out


def test_deep_legacy_token_no_scope_field_emits_info_line_no_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy long-lived token has `access_token` only (no scope, no
    refresh_token) ⇒ deep check 4 emits a yellow info line "Legacy
    token format; scope inspection skipped" but does NOT add a
    failure. Exit 0 if everything else is healthy."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # Legacy token shape: bare access_token, no refresh_token, no scope.
    _write_token(
        token,
        refresh_token=None,
        access_token="fake-legacy-access-token",
        scope=None,
    )
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage_ok(monkeypatch)
    _patch_dropbox_client(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    # The generic check 3 already reports "token has no refresh_token"
    # — that's a failure tracked outside the deep section. We assert
    # ONLY on what the deep section emits: an info line, NO additional
    # failure for the missing-scope case.
    out = _strip_ansi(result.output)
    assert "Legacy token format" in out
    assert "scope inspection skipped" in out
    # The deep-check 1 line must report the legacy access_token form.
    assert "access_token present (legacy long-lived token)" in out
    # Folder probe still runs and succeeds — confirms the deep section
    # did NOT abort just because the scope field was missing.
    assert "Folder accessible: /claude-mirror/myproject" in out


def test_deep_team_account_emits_admin_policy_info_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the authenticated account is a team member (FullAccount.team
    is non-None), the deep check emits a yellow info line about admin
    policies; this is informational, not a failure."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    # Build a team-account: FullAccount.team is a non-None team object.
    team_obj = MagicMock(name="DropboxTeam")
    account = _make_account(
        account_type="business",
        team=team_obj,
    )
    _patch_dropbox_client(monkeypatch, account=account)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Account type: business" in out
    assert "team member" in out
    assert "admin" in out


def test_deep_skipped_for_non_dropbox_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deep checks must NOT run for googledrive / onedrive / webdav
    / sftp — they are Dropbox-specific. We confirm by writing a
    googledrive config and patching `dropbox.Dropbox` with a stub that
    fails loudly if invoked."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # Drive-shaped token JSON.
    token.write_text(json.dumps({
        "token": "fake",
        "refresh_token": "fake-refresh",
        "client_id": "fake",
        "client_secret": "fake",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/drive"],
    }))
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_path = tmp_path / "config.yaml"
    data = {
        "project_path": str(project),
        "backend": "googledrive",
        "drive_folder_id": "fake-folder-id",
        # Empty Pub/Sub config so the Drive deep section bails with the
        # yellow info line — keeps the test's exit-code assertion clean.
        "gcp_project_id": "",
        "pubsub_topic_id": "",
        "credentials_file": str(creds),
        "token_file": str(token),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    cfg_path.write_text(yaml.safe_dump(data))

    _patch_storage_ok(monkeypatch)

    # If anyone constructs a Dropbox client during a googledrive doctor
    # run, fail loudly so the regression is unmissable.
    def _exploding_factory(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError(
            "dropbox.Dropbox must NOT be constructed for non-dropbox "
            "backends"
        )

    import dropbox as _dropbox
    monkeypatch.setattr(_dropbox, "Dropbox", _exploding_factory)

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output


def test_deep_token_corrupt_json_fails_with_auth_fix_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token file exists but isn't valid JSON ⇒ deep check 1 fails with
    a clear "token file unreadable / not JSON" message; SDK is never
    constructed."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # Garbage text, not JSON.
    token.write_text("this is not json at all {{{")
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage_ok(monkeypatch)

    # Patch dropbox.Dropbox so a stray construction would explode.
    import dropbox as _dropbox
    monkeypatch.setattr(_dropbox, "Dropbox", MagicMock(
        side_effect=AssertionError(
            "dropbox.Dropbox must NOT be constructed when token JSON "
            "is corrupt"
        )
    ))

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Token file unreadable / not JSON" in out
    assert "claude-mirror auth" in out


def test_deep_missing_required_scope_fails_at_check_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PKCE token's `scope` field grants only `files.content.read` —
    `files.content.write` is missing ⇒ deep check 4 fails with the
    specific missing-scope name and a fix pointing at the Permissions
    tab."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    # Only read scope granted; write scope is missing.
    _write_token(token, scope="files.content.read account_info.read")
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
    )

    _patch_storage_ok(monkeypatch)
    _patch_dropbox_client(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "dropbox"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "missing required scope" in out
    assert "files.content.write" in out
    # Fix hint points at the Permissions tab.
    assert "Permissions" in out
    assert "https://www.dropbox.com/developers/apps" in out
