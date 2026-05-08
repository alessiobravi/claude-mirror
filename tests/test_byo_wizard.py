"""Tests for the Google Drive BYO wizard helpers introduced in v0.5.46.

Three feature areas, each with its own test class:

    1. ConsoleURLs   — `build_console_urls` / `try_open_browser` /
                       `project_create_url` build the right strings and
                       fail safely on headless environments.
    2. Validators    — `validate_*` functions accept canonical good
                       values and reject realistic bad ones with error
                       messages that mention the right hint.
    3. SmokeTest     — `run_drive_smoke_test` shapes the right Drive
                       API call, classifies the common failure modes,
                       and the wizard's retry loop in
                       `_maybe_run_drive_smoke_test` re-runs auth on
                       Yes and stops on No.

All tests are offline (no network, no real Google APIs) and complete in
well under 100 ms each — the smoke-test ones inject a fake build-service
callable, the URL ones don't reach the network at all.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from claude_mirror import _byo_wizard


# Click 8.3 emits a Context.protected_args DeprecationWarning from CliRunner
# in upstream tests; mirror the existing test_init_wizard.py filter so this
# file matches the project pattern.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Console URL templating + auto-open
# ─────────────────────────────────────────────────────────────────────────────


class TestConsoleURLs:
    def test_build_console_urls_returns_three_pages_in_setup_order(self):
        urls = _byo_wizard.build_console_urls("my-project-123")
        labels = [label for label, _ in urls]
        assert labels == [
            "Enable Drive API",
            "Enable Pub/Sub API",
            "Create OAuth client (Desktop app)",
        ]

    def test_build_console_urls_templates_project_id_into_each_url(self):
        urls = dict(_byo_wizard.build_console_urls("my-project-123"))
        assert urls["Enable Drive API"] == (
            "https://console.cloud.google.com/apis/library/"
            "drive.googleapis.com?project=my-project-123"
        )
        assert urls["Enable Pub/Sub API"] == (
            "https://console.cloud.google.com/apis/library/"
            "pubsub.googleapis.com?project=my-project-123"
        )
        assert urls["Create OAuth client (Desktop app)"] == (
            "https://console.cloud.google.com/apis/credentials/"
            "oauthclient?project=my-project-123"
        )

    def test_build_console_urls_handles_dashes_and_digits(self):
        # GCP project IDs commonly include hyphens + digits; nothing
        # in the templating layer should mangle them.
        urls = dict(_byo_wizard.build_console_urls("acme-prod-2"))
        assert "project=acme-prod-2" in urls["Enable Drive API"]

    def test_project_create_url_is_global(self):
        # No project ID can be templated when the user hasn't created
        # one yet — must return the global project-create URL.
        assert (
            _byo_wizard.project_create_url()
            == "https://console.cloud.google.com/projectcreate"
        )

    def test_try_open_browser_returns_true_on_success(self):
        with patch.object(
            _byo_wizard.webbrowser, "open", return_value=True
        ) as mock_open:
            assert _byo_wizard.try_open_browser("https://example.com") is True
            mock_open.assert_called_once_with("https://example.com", new=2)

    def test_try_open_browser_returns_false_when_webbrowser_returns_false(self):
        # webbrowser.open returns False on most platforms when no
        # browser is registered (typical for a headless Linux box).
        with patch.object(_byo_wizard.webbrowser, "open", return_value=False):
            assert _byo_wizard.try_open_browser("https://example.com") is False

    def test_try_open_browser_swallows_webbrowser_error(self):
        # Some webbrowser handlers raise webbrowser.Error rather than
        # returning False — we MUST catch it so the wizard doesn't
        # crash on a misconfigured DESKTOP_SESSION env var or similar.
        with patch.object(
            _byo_wizard.webbrowser,
            "open",
            side_effect=_byo_wizard.webbrowser.Error("no display"),
        ):
            assert _byo_wizard.try_open_browser("https://example.com") is False

    def test_try_open_browser_swallows_unexpected_exception(self):
        # Defensive belt-and-braces: a third-party browser handler can
        # raise anything. The wizard must not abort on this path.
        with patch.object(
            _byo_wizard.webbrowser,
            "open",
            side_effect=RuntimeError("buggy registered handler"),
        ):
            assert _byo_wizard.try_open_browser("https://example.com") is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. Inline input validation
# ─────────────────────────────────────────────────────────────────────────────


class TestValidateGCPProjectID:
    @pytest.mark.parametrize("value", [
        "my-project",            # 10 chars, hyphens — canonical
        "abcdef",                # 6 chars (minimum)
        "claude-mirror-prod",    # multi-hyphen
        "a-project-with-30-char1",  # 22 chars w/ hyphens + digits
        "my-project-123",        # ends with digit (allowed)
        "myproject1",            # no hyphens, ends with digit
    ])
    def test_accepts_valid_project_ids(self, value):
        assert _byo_wizard.validate_gcp_project_id(value) == value

    @pytest.mark.parametrize("value, why", [
        ("My-Project", "uppercase"),
        ("12345-abc", "starts with digit"),
        ("abc", "too short (3 chars; min is 6)"),
        ("-myproject", "starts with hyphen"),
        ("myproject-", "ends with hyphen"),
        ("my_project", "underscores not allowed"),
        ("my project", "space not allowed"),
        ("a" * 31, "too long (31 chars; max is 30)"),
        ("", "empty"),
    ])
    def test_rejects_invalid_project_ids(self, value, why):
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_gcp_project_id(value)
        # The error message MUST point the user at the rules + the
        # canonical Google docs URL — that's the whole UX win of inline
        # validation over "fail at first sync".
        msg = str(excinfo.value.message)
        assert "lowercase" in msg or "6-30" in msg, why
        assert "cloud.google.com" in msg, why

    def test_strips_surrounding_whitespace(self):
        # Users commonly paste with trailing whitespace; accept silently.
        assert _byo_wizard.validate_gcp_project_id("  my-project  ") == "my-project"


class TestValidateDriveFolderID:
    @pytest.mark.parametrize("value", [
        "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt",  # canonical 40-char Drive ID
        "A" * 33,                                    # typical 33-char ID
        "abc-DEF_ghi-JKL_mno-PQR",                   # 23 chars w/ -_ (boundary)
    ])
    def test_accepts_valid_folder_ids(self, value):
        assert _byo_wizard.validate_drive_folder_id(value) == value

    @pytest.mark.parametrize("value", [
        "https://drive.google.com/drive/folders/1Bxi",  # whole URL pasted
        "1Bxi",                                          # too short (4 chars)
        "A" * 19,                                        # one short of min
        "1Bxi/MVs0",                                     # `/` not allowed
        "",                                              # empty
    ])
    def test_rejects_invalid_folder_ids(self, value):
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_drive_folder_id(value)
        # Hint MUST mention `/folders/` because pasting the whole URL
        # is the #1 user mistake here.
        assert "/folders/" in str(excinfo.value.message)


class TestValidatePubSubTopicID:
    @pytest.mark.parametrize("value", [
        "claude-mirror-myproject",
        "abc",                          # 3-char minimum
        "topic.with.dots",              # `.` allowed
        "topic_with_underscore",
        "T" + "x" * 254,                # 255 chars (maximum)
    ])
    def test_accepts_valid_topic_ids(self, value):
        assert _byo_wizard.validate_pubsub_topic_id(value) == value

    @pytest.mark.parametrize("value", [
        "1topic",                       # starts with digit
        "ab",                           # too short (2 chars)
        "topic with space",             # space not allowed
        "T" + "x" * 255,                # 256 chars (one over max)
        "",                             # empty
    ])
    def test_rejects_invalid_topic_ids(self, value):
        with pytest.raises(click.BadParameter):
            _byo_wizard.validate_pubsub_topic_id(value)


class TestValidateCredentialsFile:
    def _write_oauth_json(self, tmp_path: Path) -> Path:
        """Helper: write a valid OAuth Desktop-app client JSON."""
        path = tmp_path / "credentials.json"
        path.write_text(json.dumps({
            "installed": {
                "client_id": "12345.apps.googleusercontent.com",
                "client_secret": "secret",
                "redirect_uris": ["http://localhost"],
            }
        }))
        return path

    def test_accepts_valid_oauth_client_json(self, tmp_path):
        path = self._write_oauth_json(tmp_path)
        assert (
            _byo_wizard.validate_credentials_file(str(path))
            == str(path)
        )

    def test_rejects_missing_file(self, tmp_path):
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_credentials_file(
                str(tmp_path / "does-not-exist.json")
            )
        # Hint MUST point at where to download the file.
        assert "Cloud Console" in str(excinfo.value.message)

    def test_rejects_empty_path(self):
        with pytest.raises(click.BadParameter):
            _byo_wizard.validate_credentials_file("")

    def test_rejects_directory(self, tmp_path):
        with pytest.raises(click.BadParameter):
            _byo_wizard.validate_credentials_file(str(tmp_path))

    def test_rejects_invalid_json(self, tmp_path):
        path = tmp_path / "not-json.json"
        path.write_text("not actually json {{{")
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_credentials_file(str(path))
        assert "not valid JSON" in str(excinfo.value.message)

    def test_rejects_service_account_key_with_explicit_hint(self, tmp_path):
        # COMMON USER MISTAKE: downloading a service-account key from
        # the same Credentials page. The error message must call this
        # out by name so the user knows what to download instead.
        path = tmp_path / "service-account.json"
        path.write_text(json.dumps({
            "type": "service_account",
            "project_id": "my-project",
            "private_key_id": "abc",
            "private_key": "-----BEGIN PRIVATE KEY-----\n...",
            "client_email": "sa@my-project.iam.gserviceaccount.com",
        }))
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_credentials_file(str(path))
        msg = str(excinfo.value.message)
        assert "SERVICE ACCOUNT" in msg
        assert "Desktop app" in msg

    def test_rejects_oauth_json_missing_installed_block(self, tmp_path):
        # A "Web application" OAuth client has `web` instead of
        # `installed` — claude-mirror's flow needs the installed-app
        # variant. Reject with a hint about the right Application type.
        path = tmp_path / "web-client.json"
        path.write_text(json.dumps({
            "web": {"client_id": "abc", "client_secret": "xyz"}
        }))
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_credentials_file(str(path))
        assert "installed" in str(excinfo.value.message)
        assert "Desktop app" in str(excinfo.value.message)

    def test_rejects_installed_block_without_client_id(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text(json.dumps({"installed": {"client_secret": "x"}}))
        with pytest.raises(click.BadParameter) as excinfo:
            _byo_wizard.validate_credentials_file(str(path))
        assert "client_id" in str(excinfo.value.message)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Post-auth Drive smoke test
# ─────────────────────────────────────────────────────────────────────────────


def _fake_service(execute_side_effect=None):
    """Build a fake `drive.files().list(...).execute()` chain whose
    `execute()` either returns a dict or raises whatever side effect
    the test specifies. Lets each smoke-test test focus on the one
    behaviour it cares about without touching googleapiclient.
    """
    service = MagicMock(name="service")
    list_request = MagicMock(name="list_request")
    if execute_side_effect is None:
        list_request.execute.return_value = {"files": []}
    elif isinstance(execute_side_effect, Exception):
        list_request.execute.side_effect = execute_side_effect
    else:
        list_request.execute.return_value = execute_side_effect
    service.files.return_value.list.return_value = list_request
    return service, list_request


class TestSmokeTest:
    def test_smoke_test_calls_files_list_with_expected_query(self):
        service, list_request = _fake_service()
        creds = object()  # opaque — never inspected
        result = _byo_wizard.run_drive_smoke_test(
            creds,
            "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt",
            build_service=lambda c: service,
        )
        assert result.ok
        # The call shape is load-bearing — `pageSize=1` keeps the smoke
        # test cheap, and the `q` clause restricts the listing to the
        # configured folder so we don't accidentally hit a folder the
        # user didn't intend.
        list_kwargs = service.files.return_value.list.call_args.kwargs
        assert list_kwargs["pageSize"] == 1
        assert (
            "'1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt' in parents"
            in list_kwargs["q"]
        )

    def test_smoke_test_classifies_drive_api_disabled(self):
        service, _ = _fake_service(
            Exception(
                "Google Drive API has not been used in project 12345 before "
                "or it is disabled. Enable it by visiting ..."
            )
        )
        result = _byo_wizard.run_drive_smoke_test(
            object(),
            "1Bxi" + "X" * 30,
            build_service=lambda c: service,
        )
        assert not result.ok
        assert "Drive API is not enabled" in result.reason

    def test_smoke_test_classifies_folder_not_found(self):
        service, _ = _fake_service(Exception("404 File not found: <folder>"))
        result = _byo_wizard.run_drive_smoke_test(
            object(),
            "1Bxi" + "X" * 30,
            build_service=lambda c: service,
        )
        assert not result.ok
        assert "not found" in result.reason.lower()
        # Echo the folder ID in the message so the user can sanity-check
        # what was actually queried.
        assert "1Bxi" in result.reason

    def test_smoke_test_classifies_permission_denied(self):
        service, _ = _fake_service(
            Exception("403 The user does not have sufficient permissions")
        )
        result = _byo_wizard.run_drive_smoke_test(
            object(),
            "1Bxi" + "X" * 30,
            build_service=lambda c: service,
        )
        assert not result.ok
        assert "access" in result.reason.lower() or "permission" in result.reason.lower()

    def test_smoke_test_falls_through_on_unknown_error(self):
        # Network glitch — preserve the raw exception text so the user
        # has SOMETHING to grep, even if we can't classify it.
        service, _ = _fake_service(Exception("Connection reset by peer"))
        result = _byo_wizard.run_drive_smoke_test(
            object(),
            "1Bxi" + "X" * 30,
            build_service=lambda c: service,
        )
        assert not result.ok
        assert "Connection reset by peer" in result.reason


class TestWizardSmokeTestLoop:
    """Integration tests for `_maybe_run_drive_smoke_test`'s retry loop
    in cli.py — uses monkeypatching to drive Click prompts and stub the
    backend's authenticate() so no real OAuth flow happens."""

    def test_user_declines_smoke_test_returns_immediately(self, monkeypatch):
        from claude_mirror import cli as cli_module

        monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **kw: False)
        # Backend should never be constructed if user said No.
        monkeypatch.setattr(
            cli_module,
            "GoogleDriveBackend",
            lambda *_a, **_kw: pytest.fail(
                "GoogleDriveBackend instantiated despite user declining"
            ),
        )
        cli_module._maybe_run_drive_smoke_test(
            credentials_file="/tmp/c.json",
            token_file="/tmp/t.json",
            drive_folder_id="1Bxi" + "X" * 30,
        )

    def test_smoke_test_passes_first_try_returns_after_one_auth(
        self, monkeypatch
    ):
        from claude_mirror import cli as cli_module

        # Always Yes on confirm() — we should never re-prompt because
        # the smoke test passes on the first attempt.
        monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **kw: True)

        auth_calls: list[int] = []

        class FakeBackend:
            def __init__(self, _config):
                pass

            def authenticate(self):
                auth_calls.append(1)
                return object()  # creds — opaque

        monkeypatch.setattr(cli_module, "GoogleDriveBackend", FakeBackend)
        monkeypatch.setattr(
            cli_module._byo_wizard,
            "run_drive_smoke_test",
            lambda *a, **kw: _byo_wizard.SmokeTestResult(ok=True),
        )

        cli_module._maybe_run_drive_smoke_test(
            credentials_file="/tmp/c.json",
            token_file="/tmp/t.json",
            drive_folder_id="1Bxi" + "X" * 30,
        )
        assert len(auth_calls) == 1, (
            "auth should run exactly once on a passing smoke test — "
            "running it twice would double the OAuth browser pop-ups"
        )

    def test_smoke_test_retries_on_failure_when_user_says_yes(
        self, monkeypatch
    ):
        from claude_mirror import cli as cli_module

        # Sequence of confirm() answers:
        #   1. "Authenticate and run a Drive smoke test now?"  → Yes
        #   2. "Retry authentication?" (after first failure)   → Yes
        #   3. "Retry authentication?" (after second failure)  → No
        confirm_answers = iter([True, True, False])
        monkeypatch.setattr(
            cli_module.click,
            "confirm",
            lambda *a, **kw: next(confirm_answers),
        )

        auth_calls: list[int] = []

        class FakeBackend:
            def __init__(self, _config):
                pass

            def authenticate(self):
                auth_calls.append(1)
                return object()

        monkeypatch.setattr(cli_module, "GoogleDriveBackend", FakeBackend)
        # Smoke test always fails — the loop should exit only when the
        # user declines retry, not on its own.
        monkeypatch.setattr(
            cli_module._byo_wizard,
            "run_drive_smoke_test",
            lambda *a, **kw: _byo_wizard.SmokeTestResult(
                ok=False, reason="Drive API is not enabled in this GCP project."
            ),
        )

        cli_module._maybe_run_drive_smoke_test(
            credentials_file="/tmp/c.json",
            token_file="/tmp/t.json",
            drive_folder_id="1Bxi" + "X" * 30,
        )
        # Two auth calls: initial attempt + one retry. User declined the
        # second retry, so we stopped at 2 (NOT 3) — the YAML should
        # still write afterwards (caller's responsibility).
        assert len(auth_calls) == 2

    def test_smoke_test_skips_if_authentication_raises(self, monkeypatch):
        from claude_mirror import cli as cli_module

        monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **kw: True)

        class FakeBackend:
            def __init__(self, _config):
                pass

            def authenticate(self):
                raise RuntimeError("user closed the browser")

        monkeypatch.setattr(cli_module, "GoogleDriveBackend", FakeBackend)
        # The wizard MUST swallow auth errors — failing the smoke test
        # here would block the user from saving the YAML and retrying
        # `claude-mirror auth` later.
        smoke_called = []
        monkeypatch.setattr(
            cli_module._byo_wizard,
            "run_drive_smoke_test",
            lambda *a, **kw: smoke_called.append(1),
        )
        cli_module._maybe_run_drive_smoke_test(
            credentials_file="/tmp/c.json",
            token_file="/tmp/t.json",
            drive_folder_id="1Bxi" + "X" * 30,
        )
        assert smoke_called == [], (
            "smoke test must not run if authenticate() raised; the loop "
            "should print a warning and return"
        )
