"""Tests for the deep Google Drive checks added to `claude-mirror doctor`.

The generic doctor checks (config/credentials/token/connectivity/project/
manifest) live in test_doctor.py. This module covers the GOOGLE-DRIVE-ONLY
deep checks layered on top:

  1. OAuth scope inventory (Drive required, Pub/Sub optional).
  2. Drive API enabled in the GCP project.
  3. Pub/Sub API enabled.
  4. Pub/Sub topic exists.
  5. Per-machine subscription exists.
  6. IAM grant: Drive's service account has publish permission on the topic.

All Google Cloud SDK calls are mocked — the tests are offline, deterministic,
and well under 100ms each. The single mock seam is
`claude_mirror.cli._googledrive_deep_check_factory`, which returns the
publisher / subscriber / scopes / OAuth-creds bundle that the deep checker
consumes. The Drive-API probe is mocked separately by patching
`googleapiclient.discovery.build`.
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


_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    credentials_file: Path,
    gcp_project_id: str = "test-project",
    pubsub_topic_id: str = "test-topic",
    machine_name: str = "test-machine",
    drive_folder_id: str = "test-folder-id",
) -> Path:
    """Write a minimal Google Drive YAML config that exercises the deep
    Drive checks (gcp_project_id + pubsub_topic_id non-empty)."""
    data: dict = {
        "project_path": str(project_path),
        "backend": "googledrive",
        "drive_folder_id": drive_folder_id,
        "gcp_project_id": gcp_project_id,
        "pubsub_topic_id": pubsub_topic_id,
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
    scopes: Optional[List[str]] = None,
    with_refresh: bool = True,
) -> None:
    """Drop a fake token JSON. Default scopes = both Drive and Pub/Sub."""
    if scopes is None:
        scopes = [_DRIVE_SCOPE, _PUBSUB_SCOPE]
    payload: dict = {
        "token": "fake-access-token",
        "client_id": "fake-client",
        "client_secret": "fake-secret",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": scopes,
    }
    if with_refresh:
        payload["refresh_token"] = "fake-refresh-token"
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


def _make_iam_policy(
    bindings: Optional[List[dict]] = None,
) -> Any:
    """Build a fake IAM policy proto-shape: an object with a `bindings`
    attribute, each binding having `role` and `members` attributes."""
    binding_objs = []
    for b in bindings or []:
        m = MagicMock()
        m.role = b["role"]
        m.members = list(b["members"])
        binding_objs.append(m)
    policy = MagicMock()
    policy.bindings = binding_objs
    return policy


def _patch_factory(
    monkeypatch: pytest.MonkeyPatch,
    *,
    publisher: Optional[MagicMock] = None,
    scopes: Optional[List[str]] = None,
    auth_error: Optional[BaseException] = None,
    creds: Optional[Any] = None,
) -> MagicMock:
    """Replace `_googledrive_deep_check_factory` with a stub returning
    the supplied publisher / scopes / auth_error. Returns the publisher
    so tests can inspect call-count/args if they care."""
    if publisher is None:
        publisher = MagicMock()
    if scopes is None:
        scopes = [_DRIVE_SCOPE, _PUBSUB_SCOPE]
    if creds is None:
        creds = MagicMock(name="fake-creds")

    def _factory(config: Any, token_path: Any) -> dict:
        return {
            "publisher": publisher,
            "creds": creds,
            "scopes": scopes,
            "auth_error": auth_error,
        }

    monkeypatch.setattr(cli_mod, "_googledrive_deep_check_factory", _factory)
    return publisher


def _patch_drive_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_exc: Optional[BaseException] = None,
) -> MagicMock:
    """Replace `googleapiclient.discovery.build` so the Drive API probe
    either succeeds (default) or raises the supplied exception."""
    fake_service = MagicMock()
    if raise_exc is not None:
        fake_service.about.return_value.get.return_value.execute.side_effect = raise_exc
    else:
        fake_service.about.return_value.get.return_value.execute.return_value = {
            "user": {"emailAddress": "alice@example.com"}
        }
    fake_build = MagicMock(return_value=fake_service)
    # Patch on the module the deep-check imports from, not on the global —
    # the deep check uses `from googleapiclient.discovery import build`.
    import googleapiclient.discovery as _disc

    monkeypatch.setattr(_disc, "build", fake_build)
    return fake_build


def _patch_subscriber_get(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_exc: Optional[BaseException] = None,
) -> MagicMock:
    """Replace `pubsub_v1.SubscriberClient` so `get_subscription` either
    succeeds (default) or raises the supplied exception. Returns the
    constructed instance for inspection."""
    instance = MagicMock()
    if raise_exc is not None:
        instance.get_subscription.side_effect = raise_exc
    else:
        instance.get_subscription.return_value = MagicMock()
    fake_class = MagicMock(return_value=instance)
    from google.cloud import pubsub_v1 as _pv1

    monkeypatch.setattr(_pv1, "SubscriberClient", fake_class)
    return instance


# Builds the canonical full configuration on disk so each test only has
# to vary the mocks.
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


def test_deep_all_pass_on_fully_configured_drive_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: Drive scope + Pub/Sub scope, Drive API ok, topic exists,
    subscription exists, IAM grant present. Exit 0; output shows every
    deep-check line as ✓."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    publisher.get_topic.return_value = MagicMock()
    publisher.get_iam_policy.return_value = _make_iam_policy(
        bindings=[
            {
                "role": "roles/pubsub.publisher",
                "members": ["serviceAccount:apps-storage-noreply@google.com"],
            }
        ]
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)
    _patch_subscriber_get(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "OAuth scopes: Drive" in out
    assert "Pub/Sub" in out
    assert "Drive API enabled" in out
    assert "Pub/Sub API enabled" in out
    assert "Pub/Sub topic exists" in out
    assert "Pub/Sub subscription exists for this machine" in out
    assert "Drive service account has publish permission" in out
    assert "All checks passed" in out


def test_deep_pubsub_scope_not_granted_skips_pubsub_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive scope present but Pub/Sub scope absent ⇒ emit yellow info
    line, skip checks 2-6, do NOT add a failure (the user opted out of
    real-time notifications). Exit 0."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch, scopes=[_DRIVE_SCOPE])  # Pub/Sub missing

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    # The yellow info line must mention skipping + give the auth-rerun fix.
    assert "Pub/Sub" in out
    assert "not granted" in out
    assert "skipping Pub/Sub checks" in out
    assert "claude-mirror auth" in out
    # Pub/Sub topic / subscription / IAM lines must NOT appear — they
    # were skipped, not run.
    assert "Pub/Sub topic exists" not in out
    assert "Pub/Sub subscription exists" not in out
    assert "Drive service account has publish permission" not in out


def test_deep_drive_api_not_enabled_parses_googles_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive API probe raises the canonical Google "API has not been used
    in project X" string ⇒ doctor classifies as api_disabled and emits a
    fix that links to the API-enable URL with the correct project ID."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch)

    err = RuntimeError(
        "Google Drive API has not been used in project test-project "
        "before or it is disabled. Enable it by visiting "
        "https://console.cloud.google.com/apis/library/drive.googleapis.com"
        "?project=test-project"
    )
    _patch_drive_api(monkeypatch, raise_exc=err)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Drive API not enabled" in out
    assert "test-project" in out
    # Fix URL should be templated with the GCP project ID.
    assert "drive.googleapis.com?project=test-project" in out


def test_deep_topic_missing_emits_fix_with_topic_creation_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_topic raises NotFound ⇒ doctor emits ✗ "Pub/Sub topic does not
    exist" with a fix pointing at the topic-creation URL templated by
    GCP project ID."""
    from google.api_core.exceptions import NotFound

    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    publisher.get_topic.side_effect = NotFound(
        "Resource not found (resource=test-topic)."
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    # API check passed (probe got a response, just NotFound for the topic).
    assert "Pub/Sub API enabled" in out
    assert "Pub/Sub topic does not exist" in out
    assert "projects/test-project/topics/test-topic" in out
    # Fix URL templated with the GCP project ID.
    assert "cloudpubsub/topic/list?project=test-project" in out
    # Subsequent subscription/IAM checks must be skipped (no point
    # checking them when the topic itself is missing).
    assert "Pub/Sub subscription exists" not in out
    assert "Drive service account has publish permission" not in out


def test_deep_subscription_missing_emits_fix_with_auth_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Topic exists but the per-machine subscription doesn't ⇒ doctor
    suggests `claude-mirror auth` (which creates the subscription) and
    surfaces the canonical projects/PROJECT/subscriptions/TOPIC-MACHINE
    name pattern in the failure."""
    from google.api_core.exceptions import NotFound

    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    publisher.get_topic.return_value = MagicMock()
    # IAM check would pass — but we don't want to assert on it here, just
    # confirm subscription failure is reported correctly.
    publisher.get_iam_policy.return_value = _make_iam_policy(
        bindings=[
            {
                "role": "roles/pubsub.publisher",
                "members": ["serviceAccount:apps-storage-noreply@google.com"],
            }
        ]
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)
    _patch_subscriber_get(
        monkeypatch,
        raise_exc=NotFound("Subscription does not exist."),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Pub/Sub subscription does not exist for this machine" in out
    # config.subscription_id = f"{topic}-{machine_safe}" = "test-topic-test-machine"
    assert "projects/test-project/subscriptions/test-topic-test-machine" in out
    assert "claude-mirror auth" in out


def test_deep_iam_grant_missing_emits_exact_warning_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Topic exists, subscription exists, but the IAM policy lacks the
    `roles/pubsub.publisher` grant for Drive's service account ⇒ emit the
    exact "Push events from THIS machine won't notify others." message
    plus the reconfigure-pubsub fix command."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    publisher.get_topic.return_value = MagicMock()
    # Policy has bindings but NOT the required service-account grant —
    # an unrelated viewer role only.
    publisher.get_iam_policy.return_value = _make_iam_policy(
        bindings=[
            {
                "role": "roles/pubsub.viewer",
                "members": ["user:alice@example.com"],
            }
        ]
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)
    _patch_subscriber_get(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Drive service account missing publish permission" in out
    # The exact wording from the spec must appear verbatim.
    assert "Push events from THIS machine won't notify others." in out
    # Fix mentions the canonical reconfigure-pubsub command.
    assert "claude-mirror init --reconfigure-pubsub" in out
    # And names the service account principal explicitly so the user
    # can copy-paste into the Cloud Console if they prefer the manual fix.
    assert "serviceAccount:apps-storage-noreply@google.com" in out


def test_deep_iam_grant_with_publisher_role_but_wrong_member_still_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: a policy that has `roles/pubsub.publisher` but bound to
    a DIFFERENT principal (e.g. only the user themselves) must still
    fail — Drive's SA is the one that needs to publish."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    publisher.get_topic.return_value = MagicMock()
    publisher.get_iam_policy.return_value = _make_iam_policy(
        bindings=[
            {
                "role": "roles/pubsub.publisher",
                "members": ["user:alice@example.com"],  # not Drive's SA
            }
        ]
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)
    _patch_subscriber_get(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Drive service account missing publish permission" in out


def test_deep_auth_failure_buckets_into_one_line_not_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RefreshError on the FIRST Pub/Sub admin call must produce exactly
    ONE auth-bucket failure line; subsequent checks (topic / subscription
    / IAM) are skipped to avoid five identical "auth needed" messages."""
    from google.auth.exceptions import RefreshError

    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    # Auth failure surfaces on the very first publisher RPC.
    publisher.get_topic.side_effect = RefreshError(
        "invalid_grant: Token has been expired or revoked."
    )
    # If anything calls get_iam_policy after the bucket has been
    # emitted, the test should fail loudly so we know bucketing works.
    publisher.get_iam_policy.side_effect = AssertionError(
        "get_iam_policy must NOT be called after auth-bucket triggers"
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)
    # SubscriberClient must also not be called after auth-bucket.
    sub_instance = _patch_subscriber_get(
        monkeypatch,
        raise_exc=AssertionError(
            "subscriber.get_subscription must NOT be called after "
            "auth-bucket triggers"
        ),
    )

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    # Exactly one bucket line — count occurrences of the bucket marker.
    bucket_count = out.count("Pub/Sub admin auth failed")
    assert bucket_count == 1, (
        f"expected exactly one auth-bucket line, got {bucket_count}\n\n"
        f"output:\n{out}"
    )
    # The bucket fix must point at re-running auth.
    assert "claude-mirror auth" in out
    # And we must NOT have tried the subscriber call (asserts in side_effect
    # would have raised AssertionError — confirm we didn't call it).
    assert sub_instance.get_subscription.call_count == 0


def test_deep_pubsub_api_not_enabled_short_circuits_remaining_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_topic raises with the "API has not been used in project X"
    string ⇒ doctor classifies as api_disabled, surfaces the project ID
    in the fix URL, and skips checks 4-6 (no point asking about a topic
    when the API hosting it is off)."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)

    publisher = MagicMock()
    publisher.get_topic.side_effect = RuntimeError(
        "Cloud Pub/Sub API has not been used in project test-project "
        "before or it is disabled."
    )
    _patch_factory(monkeypatch, publisher=publisher)
    _patch_drive_api(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Pub/Sub API not enabled" in out
    assert "test-project" in out
    assert "pubsub.googleapis.com?project=test-project" in out
    # Subsequent checks must be skipped.
    assert "Pub/Sub topic exists" not in out
    assert "Pub/Sub subscription exists" not in out


def test_deep_skipped_when_pubsub_not_configured_in_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When gcp_project_id is empty in the YAML, the deep section emits a
    yellow info line and adds NO failures — Drive without Pub/Sub is a
    valid (degraded) configuration."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    _write_token(token)
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        credentials_file=creds,
        gcp_project_id="",  # empty → deep section bails with yellow info
        pubsub_topic_id="",
    )

    _patch_storage_ok(monkeypatch)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    # No failures from the deep section — only generic checks would
    # contribute, and they're all healthy.
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Pub/Sub not configured" in out
    assert "skipping deep Drive checks" in out


def test_deep_drive_scope_missing_short_circuits_with_single_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Drive scope on the saved token ⇒ ONE failure line, no further
    Drive/Pub/Sub probes (everything else would just cascade off the same
    root cause)."""
    cfg = _build_healthy_config(tmp_path)

    _patch_storage_ok(monkeypatch)
    _patch_factory(monkeypatch, scopes=[])  # neither scope granted

    # Wire patches that would EXPLODE if invoked — proves we short-circuit.
    publisher = MagicMock()
    publisher.get_topic.side_effect = AssertionError("must not be called")
    _patch_factory(monkeypatch, publisher=publisher, scopes=[])
    fake_build = _patch_drive_api(monkeypatch)
    # The Drive-API probe must NOT be called when Drive scope is missing.
    fake_build.assert_not_called()  # baseline — no calls yet

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "googledrive"]
    )

    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Drive scope not granted" in out
    assert "claude-mirror auth" in out
    # Confirm none of the deferred probes ran.
    assert publisher.get_topic.call_count == 0
    fake_build.assert_not_called()


def test_deep_skipped_for_non_googledrive_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deep checks must NOT run for dropbox / onedrive / webdav / sftp
    — they are Drive-specific. We confirm by writing a dropbox config and
    a factory-stub that would fail loudly if invoked."""
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

    # Factory stub that fails loudly if called — proves the Dropbox path
    # never invokes the Drive-deep section.
    def _exploding_factory(*_args: Any, **_kwargs: Any) -> dict:
        raise AssertionError(
            "_googledrive_deep_check_factory must NOT be called for "
            "non-googledrive backends"
        )

    monkeypatch.setattr(
        cli_mod, "_googledrive_deep_check_factory", _exploding_factory
    )

    result = CliRunner().invoke(cli, ["doctor", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.output
