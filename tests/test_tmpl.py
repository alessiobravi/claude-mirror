"""Tests for per-event message templating across all four webhook
notification backends (Slack, Discord, Microsoft Teams, Generic JSON).

The templating contract:

* Each backend has an optional per-action template dict in the project
  config (`slack_template_format`, `discord_template_format`,
  `teams_template_format`, `webhook_template_format`).
* Slack/Discord/Teams take str-format templates that REPLACE the
  message summary; the rich-blocks / embed / MessageCard structure
  around it is preserved.
* Generic webhook takes a dict-of-format-strings template; rendered
  values are MERGED on top of the schema-stable v1 envelope so
  template fields override same-name envelope keys.
* Unknown placeholders are NON-FATAL — the notifier emits a yellow
  info line and falls back to the built-in format. A bad template
  never crashes a sync.
* Action-not-in-template (e.g. only `push` configured but a `sync`
  event fires) falls through to the built-in format for that action.
* Empty-string templates are treated as "no template" and fall through.

All tests run offline (urlopen is mocked when transport is exercised)
and complete in well under 100ms each.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from claude_mirror.config import Config
from claude_mirror.events import SyncEvent
from claude_mirror.notifications.webhooks import (
    DiscordWebhookNotifier,
    GenericWebhookNotifier,
    TeamsWebhookNotifier,
    event_template_vars,
    _render_file_list_inline,
    _render_str_template,
)


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _make_event(
    *,
    action: str = "push",
    files: list[str] | None = None,
    user: str = "alice",
    machine: str = "laptop",
    project: str = "myproject",
    timestamp: str = "2026-05-09T12:00:00+00:00",
) -> SyncEvent:
    return SyncEvent(
        machine=machine,
        user=user,
        timestamp=timestamp,
        files=list(files) if files is not None else ["a.md", "b.md"],
        action=action,
        project=project,
    )


# ─── Default-format back-compat (no template configured) ──────────────────────

class TestNoTemplateBackCompat:
    """Every backend falls through to its built-in format when no
    template dict is configured. Round-trip the v0.5.49 payload shapes
    so a future template-machinery refactor can't silently regress
    backwards-compatibility."""

    def test_discord_default_when_no_templates(self) -> None:
        notifier = DiscordWebhookNotifier("https://discord.example/hook")
        payload = notifier._format_event(_make_event(files=["x.md"]))
        # Built-in Discord title.
        assert "alice@laptop" in payload["embeds"][0]["title"]
        assert "pushed 1 file" in payload["embeds"][0]["title"]

    def test_teams_default_when_no_templates(self) -> None:
        notifier = TeamsWebhookNotifier("https://teams.example/hook")
        payload = notifier._format_event(_make_event(files=["x.md"]))
        # Built-in Teams summary.
        assert "alice@laptop" in payload["summary"]
        assert "pushed 1 file" in payload["summary"]

    def test_generic_default_when_no_templates(self) -> None:
        notifier = GenericWebhookNotifier("https://n8n.example/hook")
        payload = notifier._format_event(_make_event())
        # Schema-stable v1 envelope, exact keyset.
        assert set(payload.keys()) == {
            "version", "event", "user", "machine", "project", "files",
            "timestamp",
        }
        assert payload["version"] == 1


# ─── Discord templates ────────────────────────────────────────────────────────

class TestDiscordTemplate:
    def test_push_template_replaces_embed_title(self) -> None:
        notifier = DiscordWebhookNotifier(
            "https://discord.example/hook",
            templates={
                "push": "**{user}** pushed {n_files} files to **{project}**",
            },
        )
        payload = notifier._format_event(_make_event(files=["x.md", "y.md"]))
        assert payload["embeds"][0]["title"] == (
            "**alice** pushed 2 files to **myproject**"
        )
        # Other surfaces preserved — colour, fields, file-list field.
        assert payload["embeds"][0]["color"] == 0x22C55E
        names = [f["name"] for f in payload["embeds"][0]["fields"]]
        assert names == ["Action", "User", "Machine", "Project", "Files"]

    def test_action_not_in_template_falls_back_to_default(self) -> None:
        # Only `push` templated; a `sync` event fires.
        notifier = DiscordWebhookNotifier(
            "https://discord.example/hook",
            templates={"push": "PUSH: {user}"},
        )
        payload = notifier._format_event(_make_event(action="sync"))
        # Default Discord title for sync — no template wording.
        assert "PUSH" not in payload["embeds"][0]["title"]
        assert "alice@laptop" in payload["embeds"][0]["title"]
        assert "synced" in payload["embeds"][0]["title"]

    def test_empty_string_template_falls_back_to_default(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        notifier = DiscordWebhookNotifier(
            "https://discord.example/hook",
            templates={"push": ""},
        )
        payload = notifier._format_event(_make_event())
        # Empty template is treated as "no template configured" → default.
        assert "alice@laptop" in payload["embeds"][0]["title"]
        assert "pushed" in payload["embeds"][0]["title"]

    def test_unknown_placeholder_falls_back_with_warning(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        notifier = DiscordWebhookNotifier(
            "https://discord.example/hook",
            templates={"push": "weird {nonexistent} field"},
        )
        payload = notifier._format_event(_make_event())
        # Default title surfaces because rendering raised KeyError.
        assert "alice@laptop" in payload["embeds"][0]["title"]
        # Yellow warning line printed somewhere (stdout via Rich).
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "template error" in combined.lower()
        assert "discord" in combined.lower()


# ─── Teams templates ──────────────────────────────────────────────────────────

class TestTeamsTemplate:
    def test_push_template_carries_through_to_activity_subtitle(self) -> None:
        notifier = TeamsWebhookNotifier(
            "https://teams.example/hook",
            templates={"push": "{user}@{machine} pushed {n_files} file(s)"},
        )
        payload = notifier._format_event(_make_event(files=["x.md", "y.md"]))
        # activitySubtitle gets the rendered template.
        assert payload["sections"][0]["activitySubtitle"] == (
            "alice@laptop pushed 2 file(s)"
        )
        # Top-level summary also carries the rendered template (so push
        # / mobile previews surface the team-specific wording).
        assert payload["summary"] == "alice@laptop pushed 2 file(s)"
        # activityTitle keeps the built-in default — gives readers the
        # full structured headline regardless of how short the template is.
        assert "alice@laptop" in payload["sections"][0]["activityTitle"]

    def test_template_does_not_change_theme_color_or_facts(self) -> None:
        notifier = TeamsWebhookNotifier(
            "https://teams.example/hook",
            templates={"delete": "{user} deleted {n_files} files"},
        )
        payload = notifier._format_event(_make_event(action="delete"))
        # delete still red.
        assert payload["themeColor"] == "ef4444"
        # Facts preserved.
        names = [f["name"] for f in payload["sections"][0]["facts"]]
        assert names == ["Action", "User", "Machine", "Project"]


# ─── Generic webhook (dict template) ──────────────────────────────────────────

class TestGenericTemplate:
    def test_dict_template_overrides_envelope_fields(self) -> None:
        notifier = GenericWebhookNotifier(
            "https://n8n.example/hook",
            templates={
                "push": {
                    "custom_field_1": "{user}@{machine}",
                    "custom_field_2": "{project}",
                    "file_count": "{n_files}",
                },
            },
        )
        payload = notifier._format_event(_make_event(files=["x.md", "y.md"]))
        # Original v1 keys still present (template doesn't override them).
        assert payload["version"] == 1
        assert payload["event"] == "push"
        assert payload["user"] == "alice"
        # Custom fields merged in.
        assert payload["custom_field_1"] == "alice@laptop"
        assert payload["custom_field_2"] == "myproject"
        # All template values are strings post-format — even {n_files}.
        assert payload["file_count"] == "2"

    def test_dict_template_can_override_existing_envelope_key(self) -> None:
        # User wants to ship `user` as `user@machine` instead of bare user.
        notifier = GenericWebhookNotifier(
            "https://n8n.example/hook",
            templates={
                "push": {"user": "{user}@{machine}"},
            },
        )
        payload = notifier._format_event(_make_event())
        # Template overrides — the user gets what they asked for.
        assert payload["user"] == "alice@laptop"
        # Other keys untouched.
        assert payload["machine"] == "laptop"
        assert payload["project"] == "myproject"

    def test_dict_template_with_unknown_placeholder_falls_back(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        notifier = GenericWebhookNotifier(
            "https://n8n.example/hook",
            templates={
                "push": {"custom": "weird {nonexistent}"},
            },
        )
        payload = notifier._format_event(_make_event())
        # Falls back to default v1 envelope — no `custom` key got merged.
        assert "custom" not in payload
        assert payload["version"] == 1
        # Yellow warning line emitted (Rich writes to stdout by default).
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "template error" in combined.lower()
        assert "generic" in combined.lower()

    def test_dict_template_non_string_values_pass_through_untouched(self) -> None:
        # YAML-authored bool / int / nested-dict values in a template
        # are legitimate — user shipping a literal flag without
        # wrapping in `"{value}"`.
        notifier = GenericWebhookNotifier(
            "https://n8n.example/hook",
            templates={
                "push": {
                    "is_test": True,
                    "count": 42,
                    "tags": ["a", "b"],
                },
            },
        )
        payload = notifier._format_event(_make_event())
        assert payload["is_test"] is True
        assert payload["count"] == 42
        assert payload["tags"] == ["a", "b"]


# ─── Slack template ───────────────────────────────────────────────────────────

class TestSlackTemplate:
    """Slack templating goes through `claude_mirror.slack.post_sync_event`,
    which posts via `_send_webhook`. We mock the underlying urlopen at
    that boundary so we can assert on the final payload."""

    def _capture_payload(self, config: Config, event: SyncEvent) -> dict:
        """Run post_sync_event with urlopen mocked; return the payload
        dict that would have been POSTed to Slack."""
        from claude_mirror import slack
        captured: dict = {}

        def fake_urlopen(req, timeout=None):  # type: ignore[no-untyped-def]
            import json
            captured["payload"] = json.loads(req.data.decode())

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return None

                def read(self):
                    return b""
            return _Resp()

        with patch("claude_mirror.slack.urlopen", side_effect=fake_urlopen):
            slack.post_sync_event(config, event)
        return captured.get("payload", {})

    def _make_config(
        self,
        tmp_path: Path,
        slack_template_format: dict | None = None,
    ) -> Config:
        return Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_enabled=True,
            slack_webhook_url="https://hooks.slack.example/services/aaa/bbb/ccc",
            slack_template_format=slack_template_format,
        )

    def test_no_template_uses_default_header(self, tmp_path: Path) -> None:
        cfg = self._make_config(tmp_path)
        payload = self._capture_payload(cfg, _make_event())
        # Default mrkdwn header includes the bold formatting + emoji.
        assert "alice@laptop" in payload["text"]
        assert ":arrow_up:" in payload["text"]

    def test_push_template_replaces_summary_and_fallback(
        self, tmp_path: Path,
    ) -> None:
        cfg = self._make_config(
            tmp_path,
            slack_template_format={
                "push": ":up: {user}@{machine} pushed {n_files} file(s) to {project}",
            },
        )
        payload = self._capture_payload(cfg, _make_event(files=["x.md", "y.md"]))
        # Fallback `text` field uses the rendered template.
        assert payload["text"] == (
            ":up: alice@laptop pushed 2 file(s) to myproject"
        )
        # First section block (the header) uses the rendered template too.
        first_section = next(
            b for b in payload["blocks"] if b.get("type") == "section"
        )
        assert first_section["text"]["text"] == (
            ":up: alice@laptop pushed 2 file(s) to myproject"
        )

    def test_action_not_in_template_falls_back(self, tmp_path: Path) -> None:
        cfg = self._make_config(
            tmp_path,
            slack_template_format={"push": "PUSH"},
        )
        # Sync event — the template only covers `push`.
        payload = self._capture_payload(cfg, _make_event(action="sync"))
        # Default Slack format kicks in.
        assert "PUSH" not in payload["text"]
        assert "alice@laptop" in payload["text"]


# ─── Placeholder vocabulary ───────────────────────────────────────────────────

class TestPlaceholderVocabulary:
    def test_n_files_first_file_file_list_all_render(self) -> None:
        notifier = DiscordWebhookNotifier(
            "https://discord.example/hook",
            templates={
                "push": "{n_files} | {first_file} | {file_list}",
            },
        )
        payload = notifier._format_event(_make_event(files=["a.md", "b.md", "c.md"]))
        assert payload["embeds"][0]["title"] == "3 | a.md | a.md, b.md, c.md"

    def test_first_file_empty_when_no_files(self) -> None:
        notifier = DiscordWebhookNotifier(
            "https://discord.example/hook",
            templates={"push": "first=[{first_file}] count={n_files}"},
        )
        payload = notifier._format_event(_make_event(files=[]))
        assert payload["embeds"][0]["title"] == "first=[] count=0"

    def test_file_list_caps_at_ten_with_and_n_more(self) -> None:
        files = [f"f{i}.md" for i in range(15)]
        # Direct test of the inline renderer used by {file_list}.
        rendered = _render_file_list_inline(files)
        # Caps at 10 + "and 5 more".
        for i in range(10):
            assert f"f{i}.md" in rendered
        for i in range(10, 15):
            assert f"f{i}.md" not in rendered
        assert "and 5 more" in rendered

    def test_snapshot_timestamp_defaults_to_unknown(self) -> None:
        # SyncEvent doesn't ship a snapshot_timestamp field on this
        # event, so the placeholder should resolve to the literal
        # string "unknown" rather than blowing up.
        vars_ = event_template_vars(_make_event())
        assert vars_["snapshot_timestamp"] == "unknown"

    def test_action_placeholder_resolves(self) -> None:
        notifier = TeamsWebhookNotifier(
            "https://teams.example/hook",
            templates={"delete": "[{action}] {user} -> {n_files} files"},
        )
        payload = notifier._format_event(
            _make_event(action="delete", files=["x.md"]),
        )
        assert payload["sections"][0]["activitySubtitle"] == (
            "[delete] alice -> 1 files"
        )


# ─── YAML round-trip ──────────────────────────────────────────────────────────

class TestConfigRoundTrip:
    def test_all_four_template_fields_round_trip(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "project.yml"
        original = Config(
            project_path=str(tmp_path / "proj"),
            drive_folder_id="folder",
            slack_template_format={
                "push": ":up: {user} pushed {n_files}",
                "delete": ":wastebasket: {user} deleted",
            },
            discord_template_format={
                "push": "**{user}** pushed {n_files}",
            },
            teams_template_format={
                "sync": "{user}@{machine} synced",
            },
            webhook_template_format={
                "push": {"custom": "{user}", "count": "{n_files}"},
            },
        )
        original.save(str(cfg_path))
        loaded = Config.load(str(cfg_path))

        assert loaded.slack_template_format == {
            "push": ":up: {user} pushed {n_files}",
            "delete": ":wastebasket: {user} deleted",
        }
        assert loaded.discord_template_format == {
            "push": "**{user}** pushed {n_files}",
        }
        assert loaded.teams_template_format == {
            "sync": "{user}@{machine} synced",
        }
        assert loaded.webhook_template_format == {
            "push": {"custom": "{user}", "count": "{n_files}"},
        }

    def test_old_yaml_without_template_fields_still_loads(
        self, tmp_path: Path,
    ) -> None:
        # Backward compatibility: every existing project YAML must keep
        # working unchanged. Defaults are None for all four fields.
        cfg_path = tmp_path / "old.yml"
        cfg_path.write_text(yaml.dump({
            "project_path": str(tmp_path / "p"),
            "backend": "googledrive",
            "drive_folder_id": "x",
            "slack_enabled": False,
        }))
        cfg = Config.load(str(cfg_path))
        assert cfg.slack_template_format is None
        assert cfg.discord_template_format is None
        assert cfg.teams_template_format is None
        assert cfg.webhook_template_format is None


# ─── Validation ───────────────────────────────────────────────────────────────

class TestTemplateValidation:
    def test_unknown_action_key_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown action key 'foo'"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                discord_template_format={"foo": "hi"},
            )

    def test_typo_in_action_name_caught(self, tmp_path: Path) -> None:
        # `delet` instead of `delete` — common typo, should NOT be silently ignored.
        with pytest.raises(ValueError, match="unknown action key 'delet'"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_template_format={"delet": "{user} deleted"},
            )

    def test_string_value_for_webhook_template_raises(self, tmp_path: Path) -> None:
        # Generic webhook templates MUST be dicts.
        with pytest.raises(ValueError, match="must be a dict"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                webhook_template_format={"push": "not a dict"},
            )

    def test_dict_value_for_slack_template_raises(self, tmp_path: Path) -> None:
        # Slack templates are str-format strings, not dicts.
        with pytest.raises(ValueError, match="must be a str"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_template_format={"push": {"a": "b"}},
            )

    def test_non_dict_top_level_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must be a dict"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                discord_template_format="not a dict",  # type: ignore[arg-type]
            )

    def test_all_four_action_keys_accepted(self, tmp_path: Path) -> None:
        # push / pull / sync / delete all valid.
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_template_format={
                "push":   "{user} pushed",
                "pull":   "{user} pulled",
                "sync":   "{user} synced",
                "delete": "{user} deleted",
            },
        )
        assert set(cfg.slack_template_format.keys()) == {
            "push", "pull", "sync", "delete",
        }


# ─── _render_str_template direct tests ─────────────────────────────────────

class TestRenderStrTemplate:
    """Direct tests of the template-rendering primitive — covers the
    edge cases that subclass-level tests can't conveniently surface."""

    def test_renders_all_known_placeholders(self) -> None:
        out = _render_str_template(
            "{user}|{machine}|{project}|{action}|{n_files}|{first_file}|"
            "{timestamp}|{snapshot_timestamp}",
            _make_event(files=["a.md"]),
        )
        assert out == (
            "alice|laptop|myproject|push|1|a.md|"
            "2026-05-09T12:00:00+00:00|unknown"
        )

    def test_unknown_placeholder_raises_keyerror(self) -> None:
        with pytest.raises(KeyError):
            _render_str_template("hi {nope}", _make_event())

    def test_empty_template_raises(self) -> None:
        # Empty templates are a control-flow signal — caller treats
        # them as "no template configured" and falls back.
        with pytest.raises(KeyError):
            _render_str_template("", _make_event())
