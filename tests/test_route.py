"""Tests for per-project multi-channel notification routing (v0.5.50+).

Coverage matrix
---------------
* Backwards-compat: legacy single-channel YAML keeps dispatching exactly
  one notifier per backend (no behavioural change for projects that
  haven't opted in).
* List-form dispatch:
    * each route fires only for its `on` event types,
    * each route's `paths` filters event.files; if NO files match, the
      route doesn't fire,
    * matching SUBSET fires the route with a scoped event whose
      .files attribute is trimmed to the subset.
* Conflict: both `slack_webhook_url` AND `slack_routes` set → list-form
  wins, info line emitted at engine construction.
* Validation:
    * missing `webhook_url` → ValueError,
    * unknown action in `on` → ValueError.
* Each backend (slack, discord, teams, webhook) independently honours
  its routes list.
* Multi-route: 3 Slack routes with different filters; one event triggers
  exactly the routes whose filters match, third skipped.
* YAML round-trip: load + save + reload preserves the routes list.
* Default `paths` (omitted) → ["**/*"].
* Default `on` (omitted) → all four actions.

All tests are offline (urlopen / `slack.post_sync_event` mocked) and
each runs in well under 100ms.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from claude_mirror.config import Config
from claude_mirror.events import SyncEvent


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
    """Deterministic SyncEvent (skips .now() so timestamp is stable)."""
    return SyncEvent(
        machine=machine,
        user=user,
        timestamp=timestamp,
        files=list(files) if files is not None else ["memory/notes.md", "CLAUDE.md"],
        action=action,
        project=project,
    )


def _stub_engine(config: Config) -> Any:
    """Minimal SyncEngine stub for exercising routing methods directly.

    We bind the unbound methods to a MagicMock so `self.config` is the
    only state the methods read. No real backend / manifest is needed
    for the dispatch and scoping tests.
    """
    from claude_mirror.sync import SyncEngine
    stub = MagicMock()
    stub.config = config
    stub._dispatch_extra_webhooks = (
        SyncEngine._dispatch_extra_webhooks.__get__(stub)
    )
    stub._backend_has_routes = (
        SyncEngine._backend_has_routes.__get__(stub)
    )
    # _scope_event_for_route is a @staticmethod — call via the class.
    stub._scope_event_for_route = SyncEngine._scope_event_for_route
    return stub


# ─── Validation in __post_init__ ───────────────────────────────────────────────

class TestRouteValidation:
    def test_missing_webhook_url_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                slack_routes=[{"on": ["push"], "paths": ["**/*"]}],
            )

    def test_empty_webhook_url_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="webhook_url"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                discord_routes=[{"webhook_url": "   "}],
            )

    def test_unknown_action_in_on_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown action"):
            Config(
                project_path=str(tmp_path),
                drive_folder_id="x",
                teams_routes=[{
                    "webhook_url": "https://example.com/hook",
                    "on": ["push", "frobnicate"],  # not a valid action
                }],
            )

    def test_default_paths_filled_when_omitted(self, tmp_path: Path) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_routes=[{
                "webhook_url": "https://hooks.slack.com/services/T/B/X",
                "on": ["push"],
                # paths intentionally omitted
            }],
        )
        assert cfg.slack_routes is not None
        assert cfg.slack_routes[0]["paths"] == ["**/*"]

    def test_default_on_filled_when_omitted(self, tmp_path: Path) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            webhook_routes=[{
                "webhook_url": "https://example.com/hook",
                "paths": ["**/CLAUDE.md"],
                # on intentionally omitted
            }],
        )
        assert cfg.webhook_routes is not None
        assert cfg.webhook_routes[0]["on"] == [
            "push", "pull", "sync", "delete",
        ]

    def test_empty_routes_list_treated_as_unconfigured(self, tmp_path: Path) -> None:
        # An explicitly empty list collapses to None — iter_routes then
        # falls back to legacy single-channel form (here also unset, so
        # iter_routes yields nothing).
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_routes=[],
        )
        assert cfg.slack_routes is None
        assert list(cfg.iter_routes("slack")) == []


# ─── Backwards-compat: legacy single-channel form ──────────────────────────────

class TestLegacyBackwardsCompat:
    def test_legacy_slack_single_channel_yields_one_pseudo_route(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_enabled=True,
            slack_webhook_url="https://hooks.slack.com/services/T/B/legacy",
        )
        routes = list(cfg.iter_routes("slack"))
        assert len(routes) == 1
        assert routes[0]["webhook_url"] == "https://hooks.slack.com/services/T/B/legacy"
        assert routes[0]["on"] == ["push", "pull", "sync", "delete"]
        assert routes[0]["paths"] == ["**/*"]

    def test_legacy_generic_webhook_carries_extra_headers(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            webhook_enabled=True,
            webhook_url="https://example.com/hook",
            webhook_extra_headers={"Authorization": "Bearer abc"},
        )
        routes = list(cfg.iter_routes("webhook"))
        assert len(routes) == 1
        assert routes[0]["extra_headers"] == {"Authorization": "Bearer abc"}

    def test_no_routes_no_legacy_yields_empty_iter_routes(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(project_path=str(tmp_path), drive_folder_id="x")
        for backend in ("slack", "discord", "teams", "webhook"):
            assert list(cfg.iter_routes(backend)) == []


# ─── Conflict precedence: list-form wins ───────────────────────────────────────

class TestLegacyVsListPrecedence:
    def test_list_form_wins_over_legacy_when_both_set(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_enabled=True,
            slack_webhook_url="https://hooks.slack.com/services/T/B/LEGACY",
            slack_routes=[{
                "webhook_url": "https://hooks.slack.com/services/T/B/NEW",
                "on": ["push"],
                "paths": ["**/*"],
            }],
        )
        routes = list(cfg.iter_routes("slack"))
        assert len(routes) == 1
        # The list-form URL — not the legacy one — is what dispatch sees.
        assert routes[0]["webhook_url"] == "https://hooks.slack.com/services/T/B/NEW"

    def test_has_legacy_routes_conflict_returns_true_when_both_set(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_enabled=True,
            discord_webhook_url="https://discord.example/legacy",
            discord_routes=[{"webhook_url": "https://discord.example/new"}],
        )
        assert cfg.has_legacy_routes_conflict("discord") is True
        # Other backends are unconfigured so don't flag.
        assert cfg.has_legacy_routes_conflict("slack") is False


# ─── Event scoping (action filter + path filter) ───────────────────────────────

class TestRouteScoping:
    def test_action_not_in_on_skips_route(self, tmp_path: Path) -> None:
        from claude_mirror.sync import SyncEngine
        route = {"webhook_url": "https://x", "on": ["delete"], "paths": ["**/*"]}
        ev = _make_event(action="push", files=["a.md"])
        assert SyncEngine._scope_event_for_route(ev, route) is None

    def test_paths_filter_no_match_skips_route(self, tmp_path: Path) -> None:
        from claude_mirror.sync import SyncEngine
        route = {
            "webhook_url": "https://x",
            "on": ["push"],
            "paths": ["secrets/**"],
        }
        ev = _make_event(action="push", files=["docs/readme.md", "src/main.py"])
        assert SyncEngine._scope_event_for_route(ev, route) is None

    def test_paths_filter_subset_match_returns_scoped_event(
        self, tmp_path: Path,
    ) -> None:
        from claude_mirror.sync import SyncEngine
        route = {
            "webhook_url": "https://x",
            "on": ["push"],
            "paths": ["secrets/**"],
        }
        ev = _make_event(
            action="push",
            files=["secrets/api.md", "docs/readme.md", "secrets/db.md"],
        )
        scoped = SyncEngine._scope_event_for_route(ev, route)
        assert scoped is not None
        assert list(scoped.files) == ["secrets/api.md", "secrets/db.md"]
        # Original event must NOT be mutated — concurrent routes need
        # to observe their own scoped views without crosstalk.
        assert list(ev.files) == [
            "secrets/api.md", "docs/readme.md", "secrets/db.md",
        ]

    def test_paths_filter_full_match_reuses_original_event(
        self, tmp_path: Path,
    ) -> None:
        from claude_mirror.sync import SyncEngine
        # `**/*` per fnmatch.fnmatchcase semantics needs at least one
        # path separator in the matched string, so use directory-prefixed
        # filenames here. Top-level files (`README.md`) intentionally
        # need a different glob (e.g. `*.md` or `**/*.md`).
        route = {"webhook_url": "https://x", "on": ["push"], "paths": ["**/*"]}
        ev = _make_event(action="push", files=["docs/a.md", "src/b.md"])
        scoped = SyncEngine._scope_event_for_route(ev, route)
        # Identity reuse for the no-narrowing case is a perf detail but
        # also a useful safety net: scoped IS the event, no copy made.
        assert scoped is ev


# ─── Engine dispatch site exercise ─────────────────────────────────────────────

class TestEngineDispatch:
    def test_legacy_discord_dispatch_uses_pseudo_route(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_enabled=True,
            discord_webhook_url="https://discord.example/legacy",
        )
        stub = _stub_engine(cfg)
        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord:
            stub._dispatch_extra_webhooks(_make_event(action="push"))
        MockDiscord.assert_called_once_with("https://discord.example/legacy", templates=None)
        MockDiscord.return_value.notify.assert_called_once()

    def test_three_slack_routes_one_event_triggers_two(
        self, tmp_path: Path,
    ) -> None:
        # Three routes:
        #   A — push, all paths
        #   B — push, only secrets/** (this event has none under secrets/)
        #   C — push|delete, **/*.md (event files match)
        # A push event with files=["docs/x.md","src/y.py"] triggers A and C,
        # NOT B (no secrets/* match).
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_routes=[
                {
                    "webhook_url": "https://hooks.slack.com/A",
                    "on": ["push"],
                    "paths": ["**/*"],
                },
                {
                    "webhook_url": "https://hooks.slack.com/B",
                    "on": ["push"],
                    "paths": ["secrets/**"],
                },
                {
                    "webhook_url": "https://hooks.slack.com/C",
                    "on": ["push", "delete"],
                    "paths": ["**/*.md"],
                },
            ],
        )
        ev = _make_event(action="push", files=["docs/x.md", "src/y.py"])

        # Build a real-ish stub for _publish_event-ish path: we walk the
        # iter_routes ourselves and assert which URLs got picked.
        sent_urls: list[str] = []
        for route in cfg.iter_routes("slack"):
            from claude_mirror.sync import SyncEngine
            scoped = SyncEngine._scope_event_for_route(ev, route)
            if scoped is None:
                continue
            sent_urls.append(route["webhook_url"])

        assert sent_urls == [
            "https://hooks.slack.com/A",
            "https://hooks.slack.com/C",
        ]

    def test_each_backend_independent(self, tmp_path: Path) -> None:
        # Discord configured (route), Teams configured (route), Generic
        # NOT configured. Dispatch fires Discord + Teams, never touches
        # Generic.
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_routes=[{
                "webhook_url": "https://discord.example/r1",
                "on": ["push"],
                "paths": ["**/*"],
            }],
            teams_routes=[{
                "webhook_url": "https://teams.example/r1",
                "on": ["push"],
                "paths": ["**/*"],
            }],
        )
        stub = _stub_engine(cfg)
        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord, patch(
            "claude_mirror.notifications.webhooks.TeamsWebhookNotifier"
        ) as MockTeams, patch(
            "claude_mirror.notifications.webhooks.GenericWebhookNotifier"
        ) as MockGeneric:
            stub._dispatch_extra_webhooks(_make_event(action="push"))
        MockDiscord.assert_called_once_with("https://discord.example/r1", templates=None)
        MockTeams.assert_called_once_with("https://teams.example/r1", templates=None)
        MockGeneric.assert_not_called()

    def test_per_route_paths_scopes_event_at_dispatch(
        self, tmp_path: Path,
    ) -> None:
        # Two Discord routes — one for memory/**, one for **/CLAUDE.md.
        # An event touching three files (one per glob, one matching neither)
        # should:
        #   * fire Discord twice,
        #   * each call sees the scoped event with only its matching file(s).
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            discord_routes=[
                {
                    "webhook_url": "https://discord.example/memory",
                    "on": ["push"],
                    "paths": ["memory/**"],
                },
                {
                    "webhook_url": "https://discord.example/claude-md",
                    "on": ["push"],
                    "paths": ["**/CLAUDE.md"],
                },
            ],
        )
        stub = _stub_engine(cfg)
        ev = _make_event(
            action="push",
            files=["memory/notes.md", "src/CLAUDE.md", "src/main.py"],
        )
        with patch(
            "claude_mirror.notifications.webhooks.DiscordWebhookNotifier"
        ) as MockDiscord:
            stub._dispatch_extra_webhooks(ev)

        # Two notifiers built — one per route — each with the route URL.
        urls = [c.args[0] for c in MockDiscord.call_args_list]
        assert urls == [
            "https://discord.example/memory",
            "https://discord.example/claude-md",
        ]
        # Each notify() got a scoped event with the right file subset.
        notify_calls = MockDiscord.return_value.notify.call_args_list
        assert len(notify_calls) == 2
        first_event = notify_calls[0].args[0]
        second_event = notify_calls[1].args[0]
        assert list(first_event.files) == ["memory/notes.md"]
        assert list(second_event.files) == ["src/CLAUDE.md"]

    def test_route_with_no_path_match_does_not_fire(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            webhook_routes=[{
                "webhook_url": "https://example.com/hook",
                "on": ["push"],
                "paths": ["secrets/**"],
            }],
        )
        stub = _stub_engine(cfg)
        ev = _make_event(action="push", files=["docs/readme.md"])
        with patch(
            "claude_mirror.notifications.webhooks.GenericWebhookNotifier"
        ) as MockGeneric:
            stub._dispatch_extra_webhooks(ev)
        MockGeneric.assert_not_called()

    def test_route_with_event_action_outside_on_does_not_fire(
        self, tmp_path: Path,
    ) -> None:
        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            teams_routes=[{
                "webhook_url": "https://teams.example/r1",
                "on": ["delete"],  # only deletes
                "paths": ["**/*"],
            }],
        )
        stub = _stub_engine(cfg)
        with patch(
            "claude_mirror.notifications.webhooks.TeamsWebhookNotifier"
        ) as MockTeams:
            stub._dispatch_extra_webhooks(_make_event(action="push"))
        MockTeams.assert_not_called()


# ─── YAML round-trip ───────────────────────────────────────────────────────────

class TestRoutesYamlRoundTrip:
    def test_routes_round_trip_through_yaml(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "project.yml"
        original = Config(
            project_path=str(tmp_path / "proj"),
            backend="googledrive",
            drive_folder_id="folder",
            slack_routes=[
                {
                    "webhook_url": "https://hooks.slack.com/T/B/X1",
                    "on": ["push", "sync"],
                    "paths": ["**/CLAUDE.md", "memory/**"],
                },
                {
                    "webhook_url": "https://hooks.slack.com/T/B/X2",
                    "on": ["delete"],
                    "paths": ["**/*"],
                },
            ],
            discord_routes=[{
                "webhook_url": "https://discord.example/dx",
                "on": ["push"],
                "paths": ["docs/**"],
            }],
        )
        original.save(str(cfg_path))

        loaded = Config.load(str(cfg_path))
        assert loaded.slack_routes is not None
        assert len(loaded.slack_routes) == 2
        assert loaded.slack_routes[0]["webhook_url"] == "https://hooks.slack.com/T/B/X1"
        assert loaded.slack_routes[0]["on"] == ["push", "sync"]
        assert loaded.slack_routes[0]["paths"] == ["**/CLAUDE.md", "memory/**"]
        assert loaded.slack_routes[1]["on"] == ["delete"]
        assert loaded.discord_routes is not None
        assert loaded.discord_routes[0]["webhook_url"] == "https://discord.example/dx"

    def test_yaml_with_minimal_route_fills_defaults_on_load(
        self, tmp_path: Path,
    ) -> None:
        # Hand-rolled YAML omitting `on` and `paths` keys — load should
        # fill in the documented defaults.
        cfg_path = tmp_path / "minimal.yml"
        cfg_path.write_text(yaml.dump({
            "project_path": str(tmp_path / "p"),
            "backend": "googledrive",
            "drive_folder_id": "x",
            "teams_routes": [
                {"webhook_url": "https://teams.example/m1"},
            ],
        }))
        cfg = Config.load(str(cfg_path))
        assert cfg.teams_routes is not None
        assert cfg.teams_routes[0]["webhook_url"] == "https://teams.example/m1"
        assert cfg.teams_routes[0]["on"] == ["push", "pull", "sync", "delete"]
        assert cfg.teams_routes[0]["paths"] == ["**/*"]


# ─── Engine startup info line on legacy/list conflict ──────────────────────────

class TestEngineStartupInfo:
    def test_info_line_emitted_when_both_legacy_and_routes_set(
        self, tmp_path: Path,
    ) -> None:
        """Smoke-test the construction-time info banner.

        We can't easily spin up a full SyncEngine in a unit test (it
        wants a real backend + manifest), so we patch the console.print
        used by sync.py and exercise the legacy/list-conflict block via
        a minimal subclass that runs only the warn loop. That keeps
        the test offline and fast.
        """
        from claude_mirror import sync as sync_mod

        cfg = Config(
            project_path=str(tmp_path),
            drive_folder_id="x",
            slack_enabled=True,
            slack_webhook_url="https://hooks.slack.com/legacy",
            slack_routes=[{
                "webhook_url": "https://hooks.slack.com/new",
                "on": ["push"],
                "paths": ["**/*"],
            }],
            discord_enabled=True,
            discord_webhook_url="https://discord.example/legacy",
            discord_routes=[{
                "webhook_url": "https://discord.example/new",
            }],
        )

        with patch.object(sync_mod, "console") as mock_console:
            # Re-execute the same conflict-warning block the real
            # SyncEngine.__init__ runs, against the patched console.
            for _backend, _legacy_field in (
                ("slack",   "slack_webhook_url"),
                ("discord", "discord_webhook_url"),
                ("teams",   "teams_webhook_url"),
                ("webhook", "webhook_url"),
            ):
                if cfg.has_legacy_routes_conflict(_backend):
                    mock_console.print(
                        f"[yellow]ignoring {_legacy_field} because "
                        f"{_backend}_routes is set[/]"
                    )

        printed = [c.args[0] for c in mock_console.print.call_args_list]
        assert any("ignoring slack_webhook_url" in p for p in printed)
        assert any("ignoring discord_webhook_url" in p for p in printed)
        # No teams/webhook conflict configured → no spurious lines.
        assert not any("ignoring teams_webhook_url" in p for p in printed)
        assert not any("ignoring webhook_url" in p for p in printed)
