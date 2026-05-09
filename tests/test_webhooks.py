"""Tests for the Discord / Teams / Generic webhook notification backends.

Coverage matrix:
    * Per-backend payload shape (push, single-file fixture).
    * File-list cap at 10 with "and N more" sentinel for over-limit pushes.
    * Network failure path: urlopen raises URLError → post_json returns
      False, no exception escapes, no WARN/ERROR log emitted.
    * 4xx response path: HTTPError(403) from urlopen → False, no escape.
    * YAML round-trip of all six new Config fields plus webhook_extra_headers.
    * Engine dispatch site:
        - all three flags false → no webhook notifier instantiated.
        - one flag true → only that notifier runs.
        - one notifier raising does not stop the others (independence).
    * Generic-only:
        - extra_headers (Authorization: Bearer ...) appear on the urllib request.
        - envelope schema is exactly v1 with the documented field names.

Network is fully mocked — no test ever touches a real socket. Each test
runs in well under 100ms; the whole module finishes in single-digit ms.
"""
from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from urllib.error import HTTPError, URLError

from claude_mirror.config import Config
from claude_mirror.events import SyncEvent
from claude_mirror.notifications.webhooks import (
    DiscordWebhookNotifier,
    GenericWebhookNotifier,
    TeamsWebhookNotifier,
    WebhookNotifier,
    _FILES_DISPLAY_LIMIT,
)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    *,
    action: str = "push",
    files: list[str] | None = None,
    user: str = "alice",
    machine: str = "laptop",
    project: str = "myproject",
    timestamp: str = "2026-05-08T12:00:00+00:00",
) -> SyncEvent:
    """Build a SyncEvent without going through .now() so the timestamp
    is deterministic across test runs."""
    return SyncEvent(
        machine=machine,
        user=user,
        timestamp=timestamp,
        files=list(files) if files is not None else ["memory/notes.md", "CLAUDE.md"],
        action=action,
        project=project,
    )


class _FakeResponse:
    """Minimal stand-in for the context-manager object urlopen returns.
    Mirrors the .status / .getcode() / .read() surface our notifier uses."""

    def __init__(self, status: int = 200, body: bytes = b"") -> None:
        self.status = status
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self._body


# ─── Discord ───────────────────────────────────────────────────────────────────

class TestDiscordPayload:
    def test_push_two_files_yields_embed_with_expected_fields(self) -> None:
        event = _make_event(action="push", files=["a.md", "b.md"])
        notifier = DiscordWebhookNotifier("https://discord.com/api/webhooks/x/y")
        payload = notifier._format_event(event)

        assert payload["username"] == "claude-mirror"
        assert isinstance(payload["embeds"], list) and len(payload["embeds"]) == 1
        embed = payload["embeds"][0]
        assert "alice@laptop" in embed["title"]
        assert "pushed 2 files" in embed["title"]
        assert "myproject" in embed["title"]
        # Green colour for push (0x22C55E in decimal).
        assert embed["color"] == 0x22C55E
        # Five fields: Action / User / Machine / Project / Files.
        names = [f["name"] for f in embed["fields"]]
        assert names == ["Action", "User", "Machine", "Project", "Files"]
        files_field = embed["fields"][-1]["value"]
        assert "a.md" in files_field
        assert "b.md" in files_field

    def test_delete_uses_red_color(self) -> None:
        event = _make_event(action="delete", files=["x.md"])
        notifier = DiscordWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(event)
        assert payload["embeds"][0]["color"] == 0xEF4444

    def test_pull_and_sync_use_blue_color(self) -> None:
        for action in ("pull", "sync"):
            notifier = DiscordWebhookNotifier("https://example.com/hook")
            payload = notifier._format_event(_make_event(action=action))
            assert payload["embeds"][0]["color"] == 0x3B82F6

    def test_file_list_cap_at_ten_with_and_n_more(self) -> None:
        files = [f"file{i}.md" for i in range(12)]
        notifier = DiscordWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(_make_event(files=files))
        files_block = payload["embeds"][0]["fields"][-1]["value"]
        # First ten visible.
        for i in range(10):
            assert f"file{i}.md" in files_block
        # 11th and 12th hidden behind the sentinel.
        assert "file10.md" not in files_block
        assert "file11.md" not in files_block
        assert "and 2 more" in files_block

    def test_empty_files_renders_no_files_marker(self) -> None:
        notifier = DiscordWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(_make_event(files=[]))
        assert payload["embeds"][0]["fields"][-1]["value"] == "(no files)"


# ─── Teams ─────────────────────────────────────────────────────────────────────

class TestTeamsPayload:
    def test_push_yields_messagecard_with_expected_shape(self) -> None:
        event = _make_event(action="push", files=["a.md", "b.md"])
        notifier = TeamsWebhookNotifier("https://outlook.office.com/webhook/x")
        payload = notifier._format_event(event)

        assert payload["@type"] == "MessageCard"
        assert payload["@context"] == "https://schema.org/extensions"
        # themeColor is a hex string WITHOUT the leading '#' per MessageCard schema.
        assert payload["themeColor"] == "22c55e"
        assert "alice@laptop" in payload["summary"]
        assert "pushed 2 files" in payload["summary"]

        sections = payload["sections"]
        assert isinstance(sections, list) and len(sections) == 1
        section = sections[0]
        assert "alice@laptop" in section["activityTitle"]
        # Facts list: Action / User / Machine / Project.
        fact_names = [f["name"] for f in section["facts"]]
        assert fact_names == ["Action", "User", "Machine", "Project"]
        assert "a.md" in section["text"]
        assert "b.md" in section["text"]

    def test_file_list_cap_at_ten_with_and_n_more(self) -> None:
        files = [f"deep/path/file{i}.md" for i in range(12)]
        notifier = TeamsWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(_make_event(files=files))
        text = payload["sections"][0]["text"]
        for i in range(10):
            assert f"deep/path/file{i}.md" in text
        assert "deep/path/file10.md" not in text
        assert "and 2 more" in text

    def test_delete_uses_red_theme_color(self) -> None:
        notifier = TeamsWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(_make_event(action="delete"))
        assert payload["themeColor"] == "ef4444"


# ─── Generic ───────────────────────────────────────────────────────────────────

class TestGenericPayload:
    def test_envelope_schema_is_v1_with_documented_fields(self) -> None:
        event = _make_event(
            action="push",
            files=["a.md", "b.md"],
            user="alice",
            machine="laptop",
            project="myproject",
            timestamp="2026-05-08T12:00:00+00:00",
        )
        notifier = GenericWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(event)

        # Schema-stable keyset — no more, no less. If this assertion ever
        # needs to grow, bump SCHEMA_VERSION first.
        assert set(payload.keys()) == {
            "version", "event", "user", "machine", "project", "files", "timestamp",
        }
        assert payload["version"] == 1
        assert payload["event"] == "push"
        assert payload["user"] == "alice"
        assert payload["machine"] == "laptop"
        assert payload["project"] == "myproject"
        assert payload["files"] == ["a.md", "b.md"]
        assert payload["timestamp"] == "2026-05-08T12:00:00+00:00"

    def test_files_list_is_passed_through_uncapped_at_envelope_level(self) -> None:
        # The envelope includes ALL files (up to SyncEvent's MAX cap of
        # 100). Display-time truncation happens only in human-rendered
        # backends (Discord / Teams), not in the machine-readable envelope.
        files = [f"file{i}.md" for i in range(12)]
        notifier = GenericWebhookNotifier("https://example.com/hook")
        payload = notifier._format_event(_make_event(files=files))
        assert payload["files"] == files

    def test_extra_headers_set_authorization_on_request(self) -> None:
        notifier = GenericWebhookNotifier(
            "https://example.com/hook",
            extra_headers={"Authorization": "Bearer secret-token-123"},
        )
        captured = {}

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            # urllib lowercases header names internally — check both forms.
            captured["headers"] = dict(req.header_items())
            captured["url"] = req.full_url
            captured["data"] = req.data
            return _FakeResponse(status=200)

        with patch("claude_mirror.notifications.webhooks.urlopen", side_effect=fake_urlopen):
            ok = notifier.post_json({"hello": "world"})
        assert ok is True
        # Header keys are title-cased by urllib.request.Request.
        header_keys = {k.lower() for k in captured["headers"]}
        assert "authorization" in header_keys
        # Find the value regardless of case.
        auth_value = next(
            v for k, v in captured["headers"].items() if k.lower() == "authorization"
        )
        assert auth_value == "Bearer secret-token-123"
        # Content-Type still set.
        assert any(k.lower() == "content-type" for k in captured["headers"])

    def test_constructor_defensive_copies_extra_headers(self) -> None:
        # Mutating the dict the caller passed in MUST NOT affect the
        # notifier's view — otherwise a long-lived notifier could pick
        # up a token rotation half-way through a publish loop in
        # weird ways.
        headers = {"Authorization": "Bearer one"}
        notifier = GenericWebhookNotifier("https://example.com/hook", extra_headers=headers)
        headers["Authorization"] = "Bearer two"
        assert notifier.extra_headers["Authorization"] == "Bearer one"


# ─── Network behaviour (shared transport) ──────────────────────────────────────

class TestPostJsonTransport:
    def test_2xx_returns_true(self) -> None:
        notifier = GenericWebhookNotifier("https://example.com/hook")
        with patch(
            "claude_mirror.notifications.webhooks.urlopen",
            return_value=_FakeResponse(status=204),
        ):
            assert notifier.post_json({"k": "v"}) is True

    def test_url_error_returns_false_no_exception_no_warn_log(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        notifier = DiscordWebhookNotifier("https://example.com/hook")
        with caplog.at_level(logging.DEBUG, logger="claude_mirror.notifications.webhooks"):
            with patch(
                "claude_mirror.notifications.webhooks.urlopen",
                side_effect=URLError("network is down"),
            ):
                ok = notifier.post_json({"k": "v"})
        assert ok is False
        # Best-effort contract: nothing at WARN+ from the notifier.
        for rec in caplog.records:
            if rec.name == "claude_mirror.notifications.webhooks":
                assert rec.levelno < logging.WARNING

    def test_403_response_returns_false_no_exception(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        notifier = TeamsWebhookNotifier("https://example.com/hook")
        # HTTPError is what urllib raises for non-2xx. Wrap construction +
        # close in a try/finally so the underlying tempfile (fp) doesn't
        # leak a ResourceWarning if pytest sweeps unraisable exceptions.
        err = HTTPError(
            url="https://example.com/hook",
            code=403,
            msg="Forbidden",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )
        try:
            with caplog.at_level(logging.DEBUG, logger="claude_mirror.notifications.webhooks"):
                with patch(
                    "claude_mirror.notifications.webhooks.urlopen",
                    side_effect=err,
                ):
                    ok = notifier.post_json({"k": "v"})
            assert ok is False
            for rec in caplog.records:
                if rec.name == "claude_mirror.notifications.webhooks":
                    assert rec.levelno < logging.WARNING
        finally:
            err.close()

    def test_500_response_returns_false(self) -> None:
        notifier = TeamsWebhookNotifier("https://example.com/hook")
        err = HTTPError(
            url="https://example.com/hook",
            code=500,
            msg="Server Error",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b""),
        )
        try:
            with patch(
                "claude_mirror.notifications.webhooks.urlopen",
                side_effect=err,
            ):
                assert notifier.post_json({"k": "v"}) is False
        finally:
            err.close()

    def test_empty_url_returns_false_without_calling_urlopen(self) -> None:
        notifier = DiscordWebhookNotifier("")
        with patch(
            "claude_mirror.notifications.webhooks.urlopen",
        ) as mock_urlopen:
            ok = notifier.post_json({"k": "v"})
        assert ok is False
        mock_urlopen.assert_not_called()

    def test_non_serialisable_payload_returns_false(self) -> None:
        notifier = DiscordWebhookNotifier("https://example.com/hook")

        class NotJsonable:
            pass

        with patch("claude_mirror.notifications.webhooks.urlopen") as mock_urlopen:
            ok = notifier.post_json({"bad": NotJsonable()})
        assert ok is False
        mock_urlopen.assert_not_called()

    def test_notify_swallows_format_error(self) -> None:
        # If a subclass's _format_event blows up, notify() must still
        # return None silently — sync.py is the caller and cannot tolerate
        # a notifier raising.
        class BadNotifier(WebhookNotifier):
            def _format_event(self, event: SyncEvent) -> dict:
                raise RuntimeError("boom")

        notifier = BadNotifier("https://example.com/hook")
        with patch(
            "claude_mirror.notifications.webhooks.urlopen",
        ) as mock_urlopen:
            notifier.notify(_make_event())  # must not raise
        mock_urlopen.assert_not_called()


# ─── Config round-trip ─────────────────────────────────────────────────────────

class TestConfigYamlRoundTrip:
    def test_all_six_new_fields_plus_extra_headers_round_trip(
        self, tmp_path: Path,
    ) -> None:
        cfg_path = tmp_path / "project.yml"
        original = Config(
            project_path=str(tmp_path / "proj"),
            backend="googledrive",
            drive_folder_id="folder",
            discord_enabled=True,
            discord_webhook_url="https://discord.com/api/webhooks/123/abc",
            teams_enabled=True,
            teams_webhook_url="https://outlook.office.com/webhook/xyz",
            webhook_enabled=True,
            webhook_url="https://n8n.example.com/webhook/sync-event",
            webhook_extra_headers={
                "Authorization": "Bearer my-token",
                "X-Tenant-ID": "tenant-42",
            },
        )
        original.save(str(cfg_path))

        loaded = Config.load(str(cfg_path))
        assert loaded.discord_enabled is True
        assert loaded.discord_webhook_url == "https://discord.com/api/webhooks/123/abc"
        assert loaded.teams_enabled is True
        assert loaded.teams_webhook_url == "https://outlook.office.com/webhook/xyz"
        assert loaded.webhook_enabled is True
        assert loaded.webhook_url == "https://n8n.example.com/webhook/sync-event"
        assert loaded.webhook_extra_headers == {
            "Authorization": "Bearer my-token",
            "X-Tenant-ID": "tenant-42",
        }

    def test_defaults_are_disabled_and_empty(self, tmp_path: Path) -> None:
        cfg = Config(project_path=str(tmp_path), drive_folder_id="x")
        assert cfg.discord_enabled is False
        assert cfg.discord_webhook_url == ""
        assert cfg.teams_enabled is False
        assert cfg.teams_webhook_url == ""
        assert cfg.webhook_enabled is False
        assert cfg.webhook_url == ""
        assert cfg.webhook_extra_headers is None

    def test_load_from_yaml_string_with_no_new_fields_still_works(
        self, tmp_path: Path,
    ) -> None:
        # Backward compatibility — older project YAMLs predating v0.5.47
        # MUST still load. The dataclass defaults supply the new fields.
        cfg_path = tmp_path / "old.yml"
        cfg_path.write_text(yaml.dump({
            "project_path": str(tmp_path / "p"),
            "backend": "googledrive",
            "drive_folder_id": "x",
            "slack_enabled": False,
        }))
        cfg = Config.load(str(cfg_path))
        assert cfg.discord_enabled is False
        assert cfg.teams_enabled is False
        assert cfg.webhook_enabled is False
        assert cfg.webhook_extra_headers is None


# ─── Engine dispatch wiring ────────────────────────────────────────────────────

class TestEngineDispatch:
    """Spot-check the dispatch site in claude_mirror.sync without spinning
    up a full SyncEngine. We exercise `_dispatch_extra_webhooks` directly
    with a stub `self` since the method only reads `self.config`."""

    def _make_stub(self, config: Config) -> Any:
        from claude_mirror.sync import SyncEngine
        stub = MagicMock()
        stub.config = config
        # Bind the unbound method so `self` is the stub.
        stub._dispatch_extra_webhooks = (
            SyncEngine._dispatch_extra_webhooks.__get__(stub)
        )
        return stub

    def test_all_disabled_no_notifier_constructed(self, tmp_path: Path) -> None:
        cfg = Config(project_path=str(tmp_path), drive_folder_id="x")
        stub = self._make_stub(cfg)

        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord, patch(
            "claude_mirror.notifications.webhooks.TeamsWebhookNotifier"
        ) as MockTeams, patch(
            "claude_mirror.notifications.webhooks.GenericWebhookNotifier"
        ) as MockGeneric:
            stub._dispatch_extra_webhooks(_make_event())
        MockDiscord.assert_not_called()
        MockTeams.assert_not_called()
        MockGeneric.assert_not_called()

    def test_only_discord_enabled_dispatches_only_discord(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_enabled=True,
            discord_webhook_url="https://discord.com/api/webhooks/x/y",
        )
        stub = self._make_stub(cfg)

        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord, patch(
            "claude_mirror.notifications.webhooks.TeamsWebhookNotifier"
        ) as MockTeams, patch(
            "claude_mirror.notifications.webhooks.GenericWebhookNotifier"
        ) as MockGeneric:
            stub._dispatch_extra_webhooks(_make_event())
        # Templates kwarg is passed through unconditionally (None when
        # the user hasn't configured templates) so the dispatch site
        # never has to special-case the unset state.
        MockDiscord.assert_called_once_with(
            "https://discord.com/api/webhooks/x/y", templates=None,
        )
        MockDiscord.return_value.notify.assert_called_once()
        MockTeams.assert_not_called()
        MockGeneric.assert_not_called()

    def test_enabled_but_url_empty_does_not_dispatch(self, tmp_path: Path) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_enabled=True,
            discord_webhook_url="",
        )
        stub = self._make_stub(cfg)
        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord:
            stub._dispatch_extra_webhooks(_make_event())
        MockDiscord.assert_not_called()

    def test_one_failing_does_not_block_the_others(self, tmp_path: Path) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_enabled=True,
            discord_webhook_url="https://discord.example/hook",
            teams_enabled=True,
            teams_webhook_url="https://teams.example/hook",
            webhook_enabled=True,
            webhook_url="https://n8n.example/hook",
        )
        stub = self._make_stub(cfg)

        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord, patch(
            "claude_mirror.notifications.webhooks.TeamsWebhookNotifier"
        ) as MockTeams, patch(
            "claude_mirror.notifications.webhooks.GenericWebhookNotifier"
        ) as MockGeneric:
            # Discord blows up at construction time — Teams + Generic must
            # still fire, and the engine must not propagate the error.
            MockDiscord.side_effect = RuntimeError("discord down")
            stub._dispatch_extra_webhooks(_make_event())
        MockTeams.assert_called_once()
        MockTeams.return_value.notify.assert_called_once()
        MockGeneric.assert_called_once()
        MockGeneric.return_value.notify.assert_called_once()

    def test_generic_dispatches_with_extra_headers(self, tmp_path: Path) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            webhook_enabled=True,
            webhook_url="https://n8n.example/hook",
            webhook_extra_headers={"Authorization": "Bearer abc"},
        )
        stub = self._make_stub(cfg)
        with patch(
            "claude_mirror.notifications.webhooks.GenericWebhookNotifier"
        ) as MockGeneric:
            stub._dispatch_extra_webhooks(_make_event())
        MockGeneric.assert_called_once_with(
            "https://n8n.example/hook",
            extra_headers={"Authorization": "Bearer abc"},
            templates=None,
        )
