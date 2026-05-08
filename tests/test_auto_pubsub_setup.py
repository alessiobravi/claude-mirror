"""Tests for the v0.5.47 `--auto-pubsub-setup` Drive BYO improvement.

Two surfaces under test:

  1. `claude_mirror._byo_wizard.auto_setup_pubsub` — the standalone
     idempotent helper that creates the Pub/Sub topic + per-machine
     subscription + IAM grant on the topic for Drive's push-notification
     service account. Covered: the all-fresh path, the all-already-exists
     path, mixed states, the Pub/Sub-scope-not-granted skip, the
     PermissionDenied error path, and the etag-conflict retry on the
     IAM read-modify-write.

  2. CLI flag wiring on `claude-mirror init --auto-pubsub-setup` — that
     the helper is invoked exactly once when the flag is set AND a
     Google Drive backend is configured AND the smoke test returned
     working credentials, and NOT invoked when the flag is omitted.

All tests are offline (the Pub/Sub admin SDK is monkey-patched at the
module-import seam), deterministic, and well under 100ms each. The
publisher / subscriber objects are MagicMocks so the test owns the
exact behaviour of every RPC call.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from claude_mirror import _byo_wizard
import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli, _DRIVE_PUBSUB_PUBLISHER_SA


# Click 8.3+ emits a DeprecationWarning from CliRunner; suppress here only
# (the project's pytest config promotes warnings to errors).
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _fake_creds(scopes=None) -> Any:
    """Build a minimal credentials stand-in. The auto-setup helper only
    inspects `creds.scopes` — anything else is opaque and forwarded into
    `pubsub_v1.PublisherClient(credentials=...)` (which we mock)."""
    creds = MagicMock(name="oauth-creds")
    creds.scopes = list(scopes) if scopes is not None else [
        _DRIVE_SCOPE,
        _PUBSUB_SCOPE,
    ]
    return creds


def _make_iam_policy(bindings=None) -> Any:
    """Build a fake IAM policy proto-shape: an object with a `bindings`
    attribute, each binding having `role` and `members` attributes.
    `bindings` defaults to an empty list — i.e. no Drive grant present.
    """
    binding_objs = []
    for b in bindings or []:
        item = MagicMock()
        item.role = b["role"]
        item.members = list(b["members"])
        binding_objs.append(item)
    policy = MagicMock()
    policy.bindings = binding_objs
    return policy


def _patch_pubsub_layer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    create_topic_exc: Optional[BaseException] = None,
    create_subscription_exc: Optional[BaseException] = None,
    initial_policy: Any = None,
    set_iam_policy_exc: Optional[BaseException] = None,
    second_policy: Any = None,
) -> dict:
    """Replace the Pub/Sub admin SDK with mocks. Each kwarg lets a test
    inject a behaviour for one RPC; defaults are "succeed cleanly".

    `initial_policy` is what `get_iam_policy` returns the first time;
    `second_policy` is what it returns on retry (if `set_iam_policy_exc`
    raises Aborted on the first call). Tests that don't exercise the
    retry path can ignore `second_policy`.

    Returns a dict of {publisher, subscriber, pubsub_v1} so tests can
    assert call counts / args.
    """
    publisher = MagicMock(name="publisher")
    subscriber = MagicMock(name="subscriber")

    # Configure topic creation
    if create_topic_exc is not None:
        publisher.create_topic.side_effect = create_topic_exc
    else:
        publisher.create_topic.return_value = MagicMock(name="topic")

    # Configure subscription creation
    if create_subscription_exc is not None:
        subscriber.create_subscription.side_effect = create_subscription_exc
    else:
        subscriber.create_subscription.return_value = MagicMock(
            name="subscription"
        )

    # Configure IAM policy read — sequence supports retry.
    if initial_policy is None:
        initial_policy = _make_iam_policy()
    policy_returns = [initial_policy]
    if second_policy is not None:
        policy_returns.append(second_policy)
    publisher.get_iam_policy.side_effect = list(policy_returns)

    # Configure IAM policy write
    if set_iam_policy_exc is not None:
        # Allow either a single exception (always raise) or a list (one
        # per call) so the etag-retry test can succeed on the second try.
        if isinstance(set_iam_policy_exc, list):
            publisher.set_iam_policy.side_effect = list(set_iam_policy_exc)
        else:
            publisher.set_iam_policy.side_effect = set_iam_policy_exc
    else:
        publisher.set_iam_policy.return_value = MagicMock(
            name="updated-policy"
        )

    fake_pubsub_v1 = MagicMock(name="pubsub_v1")
    fake_pubsub_v1.PublisherClient = MagicMock(return_value=publisher)
    fake_pubsub_v1.SubscriberClient = MagicMock(return_value=subscriber)

    # Patch the symbol on the SDK package so the lazy
    # `from google.cloud import pubsub_v1` line inside auto_setup_pubsub
    # resolves to our fake.
    from google.cloud import pubsub_v1 as _real_pv1

    monkeypatch.setattr(_real_pv1, "PublisherClient", fake_pubsub_v1.PublisherClient)
    monkeypatch.setattr(_real_pv1, "SubscriberClient", fake_pubsub_v1.SubscriberClient)

    return {
        "publisher": publisher,
        "subscriber": subscriber,
        "pubsub_v1": fake_pubsub_v1,
    }


# ───────────────────────────────────────────────────────────────────────────
# 1. auto_setup_pubsub helper — direct unit tests
# ───────────────────────────────────────────────────────────────────────────


class TestAutoSetupPubsub:
    def test_all_fresh_path_creates_topic_subscription_and_iam_grant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fresh project: topic does not exist, subscription does not
        exist, IAM policy has no Drive binding. Helper must create all
        three and report each as newly-added in the result."""
        mocks = _patch_pubsub_layer(
            monkeypatch,
            initial_policy=_make_iam_policy(bindings=[]),
        )

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert not result.skipped
        assert result.topic_created is True
        assert result.subscription_created is True
        assert result.iam_grant_added is True
        assert result.failures == []

        # Topic creation called with the canonical path.
        mocks["publisher"].create_topic.assert_called_once_with(
            name="projects/my-project-prod/topics/claude-mirror-myproject"
        )
        # Subscription creation uses the {topic}-{machine_safe} pattern
        # lifted from Config.subscription_id, NOT a fresh invention.
        mocks["subscriber"].create_subscription.assert_called_once()
        sub_kwargs = mocks["subscriber"].create_subscription.call_args.kwargs
        assert (
            sub_kwargs["name"]
            == "projects/my-project-prod/subscriptions/claude-mirror-myproject-laptop"
        )
        assert (
            sub_kwargs["topic"]
            == "projects/my-project-prod/topics/claude-mirror-myproject"
        )
        # set_iam_policy called exactly once with the new binding.
        mocks["publisher"].set_iam_policy.assert_called_once()
        write_kwargs = mocks["publisher"].set_iam_policy.call_args.kwargs[
            "request"
        ]
        assert (
            write_kwargs["resource"]
            == "projects/my-project-prod/topics/claude-mirror-myproject"
        )
        # The policy passed in must contain the publisher binding.
        written_policy = write_kwargs["policy"]
        seen = False
        for binding in written_policy.bindings:
            role = (
                binding["role"]
                if isinstance(binding, dict)
                else getattr(binding, "role", "")
            )
            members = (
                binding["members"]
                if isinstance(binding, dict)
                else list(getattr(binding, "members", []))
            )
            if (
                role == "roles/pubsub.publisher"
                and f"serviceAccount:{_DRIVE_PUBSUB_PUBLISHER_SA}" in members
            ):
                seen = True
        assert seen, "expected publisher binding for Drive's SA in the written policy"

    def test_all_already_exists_path_reports_no_creations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Re-running the helper after a successful first run must NOT
        report anything as newly-created — every step is idempotent and
        the binding-already-present check must avoid the redundant
        `set_iam_policy` write entirely."""
        from google.api_core.exceptions import AlreadyExists

        existing_policy = _make_iam_policy(
            bindings=[
                {
                    "role": "roles/pubsub.publisher",
                    "members": [
                        f"serviceAccount:{_DRIVE_PUBSUB_PUBLISHER_SA}",
                    ],
                }
            ]
        )
        mocks = _patch_pubsub_layer(
            monkeypatch,
            create_topic_exc=AlreadyExists("topic exists"),
            create_subscription_exc=AlreadyExists("subscription exists"),
            initial_policy=existing_policy,
        )

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert not result.skipped
        assert result.topic_created is False
        assert result.subscription_created is False
        assert result.iam_grant_added is False
        assert result.failures == []
        # The redundant write MUST NOT happen — surfacing it as
        # "added" would be a false positive in the wizard's output.
        mocks["publisher"].set_iam_policy.assert_not_called()

    def test_mixed_path_topic_exists_subscription_new_iam_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Topic was created earlier (perhaps via the doctor's hint) but
        the per-machine subscription is new and the IAM grant is new.
        Each flag in the result should reflect the actual transition."""
        from google.api_core.exceptions import AlreadyExists

        mocks = _patch_pubsub_layer(
            monkeypatch,
            create_topic_exc=AlreadyExists("topic exists"),
            initial_policy=_make_iam_policy(bindings=[]),
        )

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert not result.skipped
        assert result.topic_created is False  # AlreadyExists swallowed
        assert result.subscription_created is True
        assert result.iam_grant_added is True
        assert result.failures == []
        # Subscription create still attempted despite the topic AlreadyExists.
        mocks["subscriber"].create_subscription.assert_called_once()
        # IAM write happened once with the new binding.
        mocks["publisher"].set_iam_policy.assert_called_once()

    def test_pubsub_scope_not_granted_skips_without_any_calls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the user didn't grant the Pub/Sub scope at the consent
        screen, the helper must short-circuit BEFORE building any
        PublisherClient / SubscriberClient — both because the calls
        would fail anyway, and because we want the user to see ONE
        friendly skip-line rather than five opaque PermissionDenied
        errors. Also confirms no pubsub_v1 admin call is made."""
        # Patch the SDK seam so we can prove no constructors were called.
        from google.cloud import pubsub_v1 as _real_pv1

        publisher_ctor = MagicMock(name="PublisherClient")
        subscriber_ctor = MagicMock(name="SubscriberClient")
        monkeypatch.setattr(_real_pv1, "PublisherClient", publisher_ctor)
        monkeypatch.setattr(_real_pv1, "SubscriberClient", subscriber_ctor)

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(scopes=[_DRIVE_SCOPE]),  # NO Pub/Sub scope
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert result.skipped is True
        assert "Pub/Sub scope not granted" in result.reason
        assert "claude-mirror auth" in result.reason
        # Crucially, no Pub/Sub admin call was attempted.
        publisher_ctor.assert_not_called()
        subscriber_ctor.assert_not_called()

    def test_topic_creation_permission_denied_surfaces_in_failures(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the topic create step raises PermissionDenied, the
        failure must surface in `result.failures` with step name
        `create_topic` and a clear error message — and the subsequent
        subscription / IAM steps must be SKIPPED (they would just
        cascade off the same root cause)."""
        from google.api_core.exceptions import PermissionDenied

        mocks = _patch_pubsub_layer(
            monkeypatch,
            create_topic_exc=PermissionDenied(
                "user lacks pubsub.topics.create on project"
            ),
        )

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert not result.skipped
        # The failure list must call out the exact step + error.
        assert any(
            step == "create_topic" and "permission" in msg.lower()
            for step, msg in result.failures
        ), result.failures
        assert result.topic_created is False
        # Subscription + IAM steps must be skipped — surfacing them
        # would just spew duplicate auth-class noise.
        mocks["subscriber"].create_subscription.assert_not_called()
        mocks["publisher"].get_iam_policy.assert_not_called()
        mocks["publisher"].set_iam_policy.assert_not_called()

    def test_iam_policy_etag_conflict_retries_once_and_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Read-modify-write race: the first `set_iam_policy` call
        raises Aborted (etag mismatch), the second succeeds. Helper
        must retry once with a fresh `get_iam_policy` and succeed
        without surfacing the Aborted as a failure."""
        from google.api_core.exceptions import Aborted

        first_policy = _make_iam_policy(bindings=[])  # empty
        second_policy = _make_iam_policy(bindings=[])  # still empty
        mocks = _patch_pubsub_layer(
            monkeypatch,
            initial_policy=first_policy,
            second_policy=second_policy,
            set_iam_policy_exc=[
                Aborted("etag conflict"),
                None,  # not used; success on second call replaces side_effect
            ],
        )
        # Replace the second-call behaviour with a clean return value
        # AFTER the first Aborted side_effect is consumed. Mock chaining:
        # set side_effect to a list where the second item is "no exception
        # raised" by using a callable that just returns. We model this
        # by switching to return_value after the first exception via
        # `_normal_set_iam(call_count)`.
        call_log: list[int] = []

        def _set_iam_side_effect(*args, **kwargs):
            call_log.append(1)
            if len(call_log) == 1:
                raise Aborted("etag conflict")
            return MagicMock(name="set-iam-success")

        mocks["publisher"].set_iam_policy.side_effect = _set_iam_side_effect

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert not result.skipped
        assert result.iam_grant_added is True
        assert result.failures == [], (
            "Aborted retry that ultimately succeeds must NOT surface as "
            "a failure"
        )
        # Get-policy must have been called twice (initial + retry).
        assert mocks["publisher"].get_iam_policy.call_count == 2
        # Set-policy called twice — first raised, second succeeded.
        assert len(call_log) == 2

    def test_subscription_creation_already_exists_does_not_block_iam_step(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the subscription already exists (re-running auto-setup on
        the same machine), the IAM grant check still runs — that's
        exactly the use case where users run with --auto-pubsub-setup
        a second time to land the IAM grant they missed earlier."""
        from google.api_core.exceptions import AlreadyExists

        mocks = _patch_pubsub_layer(
            monkeypatch,
            create_topic_exc=AlreadyExists("topic exists"),
            create_subscription_exc=AlreadyExists("subscription exists"),
            initial_policy=_make_iam_policy(bindings=[]),  # NO grant yet
        )

        result = _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="laptop",
        )

        assert not result.skipped
        assert result.topic_created is False
        assert result.subscription_created is False
        # The killer: the IAM grant lands on this re-run.
        assert result.iam_grant_added is True
        mocks["publisher"].set_iam_policy.assert_called_once()

    def test_machine_name_with_dots_and_spaces_lower_cased_to_dashes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Subscription pattern transform must mirror
        `Config.subscription_id` exactly: dots and spaces become dashes,
        the result is lower-cased. Both `doctor` and the watcher rely
        on this exact transform to find the subscription this helper
        just created."""
        mocks = _patch_pubsub_layer(monkeypatch)

        _byo_wizard.auto_setup_pubsub(
            creds=_fake_creds(),
            gcp_project_id="my-project-prod",
            pubsub_topic_id="claude-mirror-myproject",
            machine_name="Alice Workstation.local",
        )

        sub_name = mocks["subscriber"].create_subscription.call_args.kwargs[
            "name"
        ]
        assert sub_name == (
            "projects/my-project-prod/subscriptions/"
            "claude-mirror-myproject-alice-workstation-local"
        )


# ───────────────────────────────────────────────────────────────────────────
# 2. CLI flag wiring — `claude-mirror init --auto-pubsub-setup ...`
# ───────────────────────────────────────────────────────────────────────────


def _stub_smoke_test_returns_creds(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace `_maybe_run_drive_smoke_test` so the CLI test doesn't
    need to drive the full OAuth flow — the function simulates a
    successful smoke-test pass and returns an opaque creds object."""
    creds = _fake_creds()

    def _fake_smoke(**kwargs):
        return creds

    monkeypatch.setattr(cli_mod, "_maybe_run_drive_smoke_test", _fake_smoke)
    return creds


def test_init_with_auto_pubsub_setup_flag_calls_helper_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag-driven (non-wizard) path: when `--auto-pubsub-setup`
    is passed alongside the standard required googledrive flags, the
    init command must invoke `_byo_wizard.auto_setup_pubsub` exactly
    once with the OAuth creds returned from the smoke test, and pass
    through gcp_project_id / pubsub_topic_id / machine_name."""
    project = tmp_path / "project"
    project.mkdir()
    cfg = tmp_path / "config.yaml"
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        '{"installed": {"client_id": "x"}}'
    )

    smoke_creds = _stub_smoke_test_returns_creds(monkeypatch)

    auto_calls: list[dict] = []

    def _fake_auto(*, creds, gcp_project_id, pubsub_topic_id, machine_name):
        auto_calls.append({
            "creds": creds,
            "gcp_project_id": gcp_project_id,
            "pubsub_topic_id": pubsub_topic_id,
            "machine_name": machine_name,
        })
        return _byo_wizard.AutoSetupResult(
            topic_created=True,
            subscription_created=True,
            iam_grant_added=True,
        )

    monkeypatch.setattr(_byo_wizard, "auto_setup_pubsub", _fake_auto)
    # Don't try to talk to a running watcher.
    monkeypatch.setattr(cli_mod, "_try_reload_watcher", lambda: None)

    result = CliRunner().invoke(
        cli,
        [
            "init",
            "--backend", "googledrive",
            "--project", str(project),
            "--drive-folder-id", "A" * 33,
            "--gcp-project-id", "my-project-prod",
            "--pubsub-topic-id", "claude-mirror-myproject",
            "--credentials-file", str(creds_file),
            "--config", str(cfg),
            "--auto-pubsub-setup",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(auto_calls) == 1, (
        f"auto_setup_pubsub must be invoked exactly once when the flag "
        f"is set; got {len(auto_calls)} calls"
    )
    call = auto_calls[0]
    assert call["creds"] is smoke_creds
    assert call["gcp_project_id"] == "my-project-prod"
    assert call["pubsub_topic_id"] == "claude-mirror-myproject"
    # machine_name comes from Config's __post_init__ default — gethostname
    # — and is non-empty.
    assert isinstance(call["machine_name"], str) and call["machine_name"]


def test_init_without_auto_pubsub_setup_flag_does_not_call_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inverse: omitting the flag must NOT invoke `auto_setup_pubsub`
    even if the user is otherwise on a Drive-with-Pub/Sub config.
    Default behaviour is unchanged from v0.5.46."""
    project = tmp_path / "project"
    project.mkdir()
    cfg = tmp_path / "config.yaml"
    creds_file = tmp_path / "credentials.json"
    creds_file.write_text(
        '{"installed": {"client_id": "x"}}'
    )

    # Ensure the smoke test isn't even called on this path (the flag-only
    # path triggers it only inside the `if auto_pubsub_setup` block).
    monkeypatch.setattr(
        cli_mod,
        "_maybe_run_drive_smoke_test",
        lambda **kw: pytest.fail(
            "smoke test must not run on the flag-less init path"
        ),
    )

    auto_calls: list[int] = []
    monkeypatch.setattr(
        _byo_wizard,
        "auto_setup_pubsub",
        lambda **_kw: auto_calls.append(1) or _byo_wizard.AutoSetupResult(),
    )
    monkeypatch.setattr(cli_mod, "_try_reload_watcher", lambda: None)

    result = CliRunner().invoke(
        cli,
        [
            "init",
            "--backend", "googledrive",
            "--project", str(project),
            "--drive-folder-id", "A" * 33,
            "--gcp-project-id", "my-project-prod",
            "--pubsub-topic-id", "claude-mirror-myproject",
            "--credentials-file", str(creds_file),
            "--config", str(cfg),
        ],
    )

    assert result.exit_code == 0, result.output
    assert auto_calls == [], (
        "auto_setup_pubsub must NOT be invoked when --auto-pubsub-setup "
        "is omitted — default behaviour from v0.5.46 must be preserved"
    )


def test_init_auto_pubsub_setup_silently_ignored_on_non_googledrive_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`init` walks every backend through the same flag list. Passing
    `--auto-pubsub-setup` on a non-googledrive backend (here: SFTP)
    must be silently ignored — the helper isn't called and the init
    completes normally."""
    project = tmp_path / "project"
    project.mkdir()
    cfg = tmp_path / "config.yaml"
    sftp_folder = "/srv/claude-mirror"

    monkeypatch.setattr(
        cli_mod,
        "_maybe_run_drive_smoke_test",
        lambda **kw: pytest.fail(
            "smoke test must not run on a non-googledrive backend"
        ),
    )

    auto_calls: list[int] = []
    monkeypatch.setattr(
        _byo_wizard,
        "auto_setup_pubsub",
        lambda **_kw: auto_calls.append(1) or _byo_wizard.AutoSetupResult(),
    )
    monkeypatch.setattr(cli_mod, "_try_reload_watcher", lambda: None)

    result = CliRunner().invoke(
        cli,
        [
            "init",
            "--backend", "sftp",
            "--project", str(project),
            "--sftp-host", "host.example.com",
            "--sftp-username", "user",
            "--sftp-folder", sftp_folder,
            "--sftp-password", "secretpw",
            "--config", str(cfg),
            "--auto-pubsub-setup",  # silently ignored
        ],
    )

    assert result.exit_code == 0, result.output
    assert auto_calls == [], (
        "auto_setup_pubsub must NOT run on a non-googledrive backend"
    )
