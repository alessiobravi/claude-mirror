"""Tests for `claude-mirror watch --once` — single-cycle cron mode.

Default `watch` runs forever (foreground daemon). The `--once` flag
added in v0.5.39 changes the loop semantics to "do one polling cycle,
print any inbox events, exit 0" — useful for cron-driven setups that
do not want a long-lived daemon process.

Coverage:
  * --once invokes notifier.watch_once exactly once and exits 0
  * --once does NOT enter the long-running watch loop
  * --quiet suppresses the "Watching ..." banner
  * --quiet still allows per-event notification lines
  * default (--no-once) behaviour is unchanged — regression net
  * polling backend's watch_once dispatches new events via the watermark
  * polling backend's watch_once does NOT flood the user on first run
"""
from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.events import SyncEvent
from claude_mirror.notifications.polling import PollingNotifier

# Click 8.3 deprecation noise — same suppression as other test modules.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def patch_watch_dependencies(monkeypatch, fake_backend, make_config, fake_notifier):
    """Replace the cli.watch helpers with deterministic test stubs.

    `_resolve_config` returns a fixed string (no FS scan).
    `Config.load`     returns a Config built by `make_config`.
    `_create_storage` returns the in-memory FakeStorageBackend.
    `_create_notifier` returns a FakeNotificationBackend whose
                       `watch_once` and `watch` methods are recorded.
    `Notifier`        is patched out to a MagicMock so desktop
                       notifications never fire during the test.

    Returns the fake notifier so tests can assert on it.
    """
    cfg = make_config()
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: p or "fake-config")
    monkeypatch.setattr(cli_module.Config, "load", classmethod(lambda cls, path: cfg))
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: fake_backend)
    monkeypatch.setattr(cli_module, "_create_notifier", lambda c, s: fake_notifier)
    # Replace desktop notifier so the test never touches plyer.
    monkeypatch.setattr(cli_module, "Notifier", lambda *a, **kw: MagicMock())
    return fake_notifier


# ── --once invokes watch_once exactly once and exits 0 ────────────────────────

def test_watch_once_invokes_watch_once_method(patch_watch_dependencies):
    """`claude-mirror watch --once` should call notifier.watch_once
    exactly once and never enter notifier.watch (the long-running loop)."""
    notifier = patch_watch_dependencies
    notifier.watch_once = MagicMock()
    notifier.watch = MagicMock()

    result = CliRunner().invoke(cli, ["watch", "--once"])
    assert result.exit_code == 0, result.output
    assert notifier.watch_once.call_count == 1
    assert notifier.watch.call_count == 0


def test_watch_default_uses_long_running_watch_method(patch_watch_dependencies):
    """Regression: without --once, watch() must invoke the long-running
    `notifier.watch` method (the default daemon mode)."""
    notifier = patch_watch_dependencies
    notifier.watch_once = MagicMock()
    notifier.watch = MagicMock()

    # The default fake_notifier.watch returns immediately (it just registers
    # the callback and times out the wait), so this exits cleanly.
    result = CliRunner().invoke(cli, ["watch", "--no-once"])
    assert result.exit_code == 0, result.output
    assert notifier.watch.call_count == 1
    assert notifier.watch_once.call_count == 0


# ── --quiet suppresses the banner but lets event lines through ─────────────────

def test_watch_once_quiet_suppresses_banner(patch_watch_dependencies):
    """`--once --quiet` exits silently when no events are pending — perfect
    for cron jobs that should only emit output when there is news."""
    notifier = patch_watch_dependencies
    notifier.watch_once = MagicMock()  # dispatches no events

    result = CliRunner().invoke(cli, ["watch", "--once", "--quiet"])
    assert result.exit_code == 0, result.output
    # The banner contains the "Watching" / "Running one polling cycle" text.
    assert "Watching" not in result.output
    assert "Running one polling cycle" not in result.output


def test_watch_once_loud_prints_banner(patch_watch_dependencies):
    """Without --quiet, the once-mode banner is printed so the user sees
    what just ran."""
    notifier = patch_watch_dependencies
    notifier.watch_once = MagicMock()

    result = CliRunner().invoke(cli, ["watch", "--once"])
    assert result.exit_code == 0, result.output
    assert "Running one polling cycle" in result.output


def test_watch_once_event_lines_still_print_in_quiet_mode(patch_watch_dependencies):
    """`--quiet` suppresses the banner but NOT the per-event notifications.
    A cron job that finds new events should still emit them."""
    notifier = patch_watch_dependencies

    def fake_watch_once(callback):
        # Simulate one event surfacing during the cycle.
        ev = SyncEvent(
            user="alice", machine="alice-laptop", project="proj",
            files=["a.md"], action="push", timestamp="2026-05-08T00:00:00Z",
        )
        callback(ev)

    notifier.watch_once = fake_watch_once

    result = CliRunner().invoke(cli, ["watch", "--once", "--quiet"])
    assert result.exit_code == 0, result.output
    assert "Remote update" in result.output
    assert "alice" in result.output


# ── PollingNotifier.watch_once integration ───────────────────────────────────

def _patch_folder_lookup(monkeypatch, fake_backend):
    """Make `FakeStorageBackend.get_file_id` also resolve folder names.

    The real Drive/Dropbox/etc. APIs treat a folder ID as a special
    kind of file ID (Drive specifically), and PollingNotifier relies
    on `get_file_id(LOGS_FOLDER, root)` returning the folder ID it
    just created via `get_or_create_folder`. The FakeStorageBackend in
    conftest.py doesn't conflate the two, so we extend it locally for
    these tests rather than mutating shared fixture behaviour."""
    real_get_file_id = fake_backend.get_file_id

    def patched(name, folder_id):
        # File hits short-circuit; only fall through to folder lookup
        # when the file lookup misses.
        result = real_get_file_id(name, folder_id)
        if result is not None:
            return result
        for (parent, n), fid in fake_backend.folders.items():
            if parent == folder_id and n == name:
                return fid
        return None

    monkeypatch.setattr(fake_backend, "get_file_id", patched)


def test_polling_watch_once_first_run_does_not_flood(
    monkeypatch, tmp_path, make_config, fake_backend
):
    """First-run bootstrap: the very first watch_once invocation must
    capture the current log tail as the watermark and NOT dispatch any
    historical events. Otherwise a fresh cron install would flood the
    user with weeks of past collaborator events on the first tick."""
    # Redirect watermark storage into tmp_path so the test does not
    # touch the user's real ~/.config/claude_mirror/.
    monkeypatch.setenv("CLAUDE_MIRROR_CONFIG_DIR", str(tmp_path / "cm"))

    cfg = make_config(machine_name="my-laptop")
    _patch_folder_lookup(monkeypatch, fake_backend)
    notifier = PollingNotifier(cfg, fake_backend)

    # Pre-populate the log via the notifier's own publish_event so the
    # logs folder is created with the same `get_file_id`-resolvable id
    # the consumer side uses.
    from claude_mirror.events import SyncEvent
    notifier.publish_event(SyncEvent(
        user="alice", machine="other-laptop", project="proj",
        files=["x.md"], action="push", timestamp="2026-01-01T00:00:00Z",
    ))

    received: list[SyncEvent] = []
    notifier.watch_once(received.append)
    assert received == [], (
        "first --once run must NOT flood the user with historical events"
    )


def test_polling_watch_once_dispatches_new_events_after_bootstrap(
    monkeypatch, tmp_path, make_config, fake_backend
):
    """After the first run captures the current tail, a subsequent run
    should surface anything new since then — the canonical cron flow."""
    monkeypatch.setenv("CLAUDE_MIRROR_CONFIG_DIR", str(tmp_path / "cm"))

    cfg = make_config(machine_name="my-laptop")
    _patch_folder_lookup(monkeypatch, fake_backend)
    notifier = PollingNotifier(cfg, fake_backend)

    # First run: empty log, captures empty watermark.
    notifier.watch_once(lambda e: None)

    # Now another machine pushes an event — publish through the
    # notifier so the storage folder layout matches.
    from claude_mirror.events import SyncEvent
    notifier.publish_event(SyncEvent(
        user="alice", machine="other-laptop", project="proj",
        files=["x.md"], action="push", timestamp="2026-05-08T00:00:00Z",
    ))

    # Second run: should surface the new event.
    received: list[SyncEvent] = []
    notifier.watch_once(received.append)
    assert len(received) == 1
    assert received[0].user == "alice"


def test_polling_watch_once_filters_own_events(
    monkeypatch, tmp_path, make_config, fake_backend
):
    """Polling backend must continue to filter out events whose `machine`
    matches the local machine — same rule as the long-running watch loop."""
    monkeypatch.setenv("CLAUDE_MIRROR_CONFIG_DIR", str(tmp_path / "cm"))

    cfg = make_config(machine_name="my-laptop")
    _patch_folder_lookup(monkeypatch, fake_backend)
    notifier = PollingNotifier(cfg, fake_backend)
    notifier.watch_once(lambda e: None)  # bootstrap

    from claude_mirror.events import SyncEvent
    notifier.publish_event(SyncEvent(
        user="me", machine="my-laptop", project="proj",
        files=["self.md"], action="push", timestamp="2026-05-08T00:00:00Z",
    ))
    notifier.publish_event(SyncEvent(
        user="alice", machine="other-laptop", project="proj",
        files=["other.md"], action="push", timestamp="2026-05-08T00:01:00Z",
    ))

    received: list[SyncEvent] = []
    notifier.watch_once(received.append)
    # Only the other-laptop event surfaces; the self-event is filtered.
    assert len(received) == 1
    assert received[0].machine == "other-laptop"


# ── help text + flag presence ────────────────────────────────────────────────

def test_watch_help_documents_once_and_quiet_flags():
    """`claude-mirror watch --help` should mention both new flags so
    users discover them without reading the changelog."""
    result = CliRunner().invoke(cli, ["watch", "--help"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "--once" in out
    assert "--quiet" in out
    assert "cron" in out  # the help text steers users toward the use case
