"""Scheme + host validation for outgoing notification webhook URLs (H2).

A misconfigured project YAML or `--slack-webhook-url` flag must NOT be
able to point claude-mirror at a non-https URL — file://, http://, or
internal endpoints (169.254.169.254 metadata, localhost:6379 Redis…)
turn the notifier into a local-file read / SSRF primitive. This test
module locks in the validator's per-backend scheme/host rules at three
levels:

  * the pure helper functions in `_webhook_url`;
  * the WebhookNotifier subclasses' `post_json` boundary;
  * the `Config` constructor (every value of every webhook field gets
    rechecked at YAML-load time so a typo fails LOUDLY at `init`,
    NOT silently at first event-fire).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from claude_mirror._webhook_url import (
    validate_discord_webhook_url,
    validate_generic_webhook_url,
    validate_slack_webhook_url,
    validate_teams_webhook_url,
    validate_webhook_url,
)
from claude_mirror.config import Config
from claude_mirror.notifications.webhooks import (
    DiscordWebhookNotifier,
    GenericWebhookNotifier,
    TeamsWebhookNotifier,
)


# ─── Pure helper-function tests ────────────────────────────────────────────────


class TestSchemeRules:
    def test_file_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_webhook_url("file:///etc/passwd")

    def test_http_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_webhook_url("http://internal/secret")

    def test_gopher_scheme_rejected(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_webhook_url("gopher://example.com/")

    def test_empty_url_is_noop(self) -> None:
        # Empty string / None are accepted as "field not configured" so
        # callers don't need to pre-filter at every site.
        validate_webhook_url("")  # no raise
        validate_webhook_url(None)  # type: ignore[arg-type]

    def test_https_with_no_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="hostname"):
            validate_webhook_url("https:///nopath")


class TestSlackHostGate:
    def test_correct_slack_host_accepted(self) -> None:
        validate_slack_webhook_url("https://hooks.slack.com/services/T/B/X")

    def test_wrong_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="allow-list"):
            validate_slack_webhook_url("https://attacker.com/services/T/B/X")

    def test_subdomain_of_slack_rejected(self) -> None:
        # `xx.hooks.slack.com` would not be a real Slack incoming
        # webhook host. Reject anything that's not the exact match.
        with pytest.raises(ValueError, match="allow-list"):
            validate_slack_webhook_url("https://xx.hooks.slack.com/services/T/B/X")

    def test_http_slack_rejected(self) -> None:
        with pytest.raises(ValueError, match="https"):
            validate_slack_webhook_url("http://hooks.slack.com/services/T/B/X")


class TestDiscordHostGate:
    def test_discord_com_accepted(self) -> None:
        validate_discord_webhook_url("https://discord.com/api/webhooks/123/abc")

    def test_legacy_discordapp_com_accepted(self) -> None:
        validate_discord_webhook_url(
            "https://discordapp.com/api/webhooks/123/abc"
        )

    def test_wrong_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="allow-list"):
            validate_discord_webhook_url("https://attacker.com/hook")


class TestTeamsHostGate:
    def test_per_tenant_subdomain_accepted(self) -> None:
        validate_teams_webhook_url(
            "https://contoso.webhook.office.com/abc/def"
        )

    def test_legacy_outlook_office_accepted(self) -> None:
        validate_teams_webhook_url("https://outlook.office.com/webhook/abc")

    def test_wildcard_does_not_match_bare_suffix(self) -> None:
        # `*.webhook.office.com` MUST require a non-empty subdomain so
        # `webhook.office.com` itself doesn't quietly pass.
        with pytest.raises(ValueError, match="allow-list"):
            validate_teams_webhook_url("https://webhook.office.com/abc")

    def test_wrong_host_rejected(self) -> None:
        with pytest.raises(ValueError, match="allow-list"):
            validate_teams_webhook_url("https://evilwebhook.office.com.attacker/abc")


class TestGenericRule:
    def test_any_https_host_accepted(self) -> None:
        # The whole point of the generic webhook is supporting arbitrary
        # endpoints (n8n / Make / Zapier / custom dashboards).
        validate_generic_webhook_url("https://n8n.acme.example/webhook/x")
        validate_generic_webhook_url("https://internal.dashboard.org/in")

    def test_http_still_rejected(self) -> None:
        # No host gate, but the scheme rule still fires.
        with pytest.raises(ValueError, match="https"):
            validate_generic_webhook_url("http://n8n.acme.example/webhook/x")


# ─── Notifier-level integration ────────────────────────────────────────────────


class TestNotifierBoundary:
    def test_discord_post_json_drops_off_host_url(self) -> None:
        # When the URL is off-host, post_json returns False without
        # invoking urlopen — defence-in-depth for callers that bypass
        # Config.load.
        n = DiscordWebhookNotifier("https://attacker.com/x")
        with patch(
            "claude_mirror.notifications.webhooks.urlopen"
        ) as mock_open:
            assert n.post_json({"a": 1}) is False
            assert mock_open.call_count == 0

    def test_teams_post_json_drops_file_scheme(self) -> None:
        n = TeamsWebhookNotifier("file:///etc/passwd")
        with patch(
            "claude_mirror.notifications.webhooks.urlopen"
        ) as mock_open:
            assert n.post_json({"a": 1}) is False
            assert mock_open.call_count == 0

    def test_generic_drops_http_scheme(self) -> None:
        n = GenericWebhookNotifier("http://internal/redis")
        with patch(
            "claude_mirror.notifications.webhooks.urlopen"
        ) as mock_open:
            assert n.post_json({"a": 1}) is False
            assert mock_open.call_count == 0


# ─── Config-load-time validation ───────────────────────────────────────────────


class TestConfigConstruction:
    def test_slack_file_url_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="slack_webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_enabled=True,
                slack_webhook_url="file:///etc/passwd",
            )

    def test_slack_http_url_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="slack_webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_enabled=True,
                slack_webhook_url="http://hooks.slack.com/services/T/B/X",
            )

    def test_slack_off_host_url_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="slack_webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_enabled=True,
                slack_webhook_url="https://attacker.com/services/T/B/X",
            )

    def test_slack_correct_url_accepted(self, tmp_path: Path) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_enabled=True,
            slack_webhook_url="https://hooks.slack.com/services/T/B/X",
        )
        assert cfg.slack_webhook_url == "https://hooks.slack.com/services/T/B/X"

    def test_discord_off_host_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="discord_webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                discord_enabled=True,
                discord_webhook_url="https://attacker.com/api/webhooks/x",
            )

    def test_teams_off_host_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="teams_webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                teams_enabled=True,
                teams_webhook_url="https://attacker.com/webhook",
            )

    def test_routes_url_validated_at_load(self, tmp_path: Path) -> None:
        """Per-route URL gate fires at Config construction, naming the
        offending field + 1-indexed position so users with a 12-route
        list can find the typo."""
        with pytest.raises(ValueError, match="slack_routes"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_routes=[{
                    "webhook_url": "file:///etc/passwd",
                    "on": ["push"],
                }],
            )

    def test_generic_webhook_url_accepts_any_https(self, tmp_path: Path) -> None:
        # Generic accepts arbitrary https hosts — that's the contract.
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            webhook_enabled=True,
            webhook_url="https://anything.example.org/in",
        )
        assert cfg.webhook_url == "https://anything.example.org/in"

    def test_generic_webhook_url_still_rejects_http(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                webhook_enabled=True,
                webhook_url="http://internal-redis:6379/",
            )
