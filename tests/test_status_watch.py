"""Tests for `claude-mirror status --watch` live-updating mode.

Watch mode wraps the existing snapshot rendering in a `rich.live.Live`
context manager and refreshes the renderable every N seconds until
KeyboardInterrupt. These tests cover:

    * the snapshot path stays single-shot when --watch is omitted (regression),
    * --watch enters Live mode and calls Live.update at least once,
    * --watch loops until KeyboardInterrupt is raised by time.sleep,
    * click-level validation rejects watch intervals outside [1, 3600],
    * KeyboardInterrupt prints the friendly stop message rather than a stack trace,
    * the `_build_status_renderable` helper returns a Rich-renderable object.

All tests run offline against a FakeStorageBackend; no real cloud I/O.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from claude_mirror import cli as cli_module
from claude_mirror.cli import _build_status_renderable, cli
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine

# Click 8.3 emits a DeprecationWarning for Context.protected_args from inside
# CliRunner.invoke; pyproject's filterwarnings = "error" otherwise turns that
# into a test failure. Suppress for this module — same reasoning as
# tests/test_completion.py.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _make_engine(make_config, fake_backend, project_dir: Path) -> SyncEngine:
    """Build a SyncEngine wired to the in-memory fake_backend so tests can
    drive `status` without authenticating against a real cloud provider."""
    cfg = make_config()
    manifest = Manifest(cfg.project_path)
    return SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=manifest,
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=[],
    )


@pytest.fixture
def fake_engine(make_config, fake_backend, project_dir, write_files):
    """A SyncEngine pinned to fake_backend, with a single local file present
    so get_status() returns a non-empty list — exercises both the table and
    the summary line in the renderable."""
    write_files({"a.md": "hello"})
    return _make_engine(make_config, fake_backend, project_dir)


@pytest.fixture
def patch_load_engine(monkeypatch, fake_engine, make_config, fake_backend):
    """Replace cli._load_engine with one that returns a pre-built engine,
    bypassing _resolve_config + auth. Returns the engine for assertions."""
    cfg = make_config()
    monkeypatch.setattr(
        cli_module,
        "_load_engine",
        lambda config_path, with_pubsub=True: (fake_engine, cfg, fake_backend),
    )
    monkeypatch.setattr(
        cli_module, "_resolve_config", lambda p: p or "fake-config-path"
    )
    return fake_engine


# ---------------------------------------------------------------------------
# 1. Regression: snapshot mode (no --watch flag) does NOT enter Live mode
# ---------------------------------------------------------------------------

def test_status_without_watch_flag_runs_once_and_exits(patch_load_engine):
    """Without --watch, `status` prints a single snapshot and exits — the
    rich.live.Live context manager is never instantiated."""
    with patch.object(cli_module, "Live") as mock_live:
        result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    # Live should never be instantiated in snapshot mode
    mock_live.assert_not_called()


def test_status_snapshot_path_wires_phase_progress_callbacks(monkeypatch, patch_load_engine, fake_engine):
    """Regression test: the snapshot-mode status path MUST pass on_local
    and on_remote callbacks into engine.get_status() so the dual-row
    transient Progress (Local: 'hashing 42/120 files' / Remote:
    'explored 7 folder(s)…') updates live during the scan.

    Pre-v0.5.30, status() called engine.show_status() which did this
    wiring. The v0.5.30 --watch refactor extracted _build_status_renderable
    but initially dropped the callbacks — this caused a silent pause
    during the scan with the full table appearing all at once at the
    end. The fix re-wires the callbacks via with_progress=True; this
    test pins that contract so it can't regress again."""
    captured: dict[str, object] = {}
    real_get_status = fake_engine.get_status

    def spy(*args, **kwargs):
        # Record whether the snapshot path forwarded the phase callbacks.
        captured["on_local"]  = kwargs.get("on_local")
        captured["on_remote"] = kwargs.get("on_remote")
        return real_get_status(*args, **kwargs)

    monkeypatch.setattr(fake_engine, "get_status", spy)

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    assert callable(captured.get("on_local")),  "on_local callback NOT forwarded — phase progress would be silent"
    assert callable(captured.get("on_remote")), "on_remote callback NOT forwarded — phase progress would be silent"


# ---------------------------------------------------------------------------
# 2. --watch enters Live mode and calls Live.update at least once
# ---------------------------------------------------------------------------

def test_status_with_watch_flag_enters_live_mode(monkeypatch, patch_load_engine):
    """With --watch N, `status` enters a rich.live.Live context and calls
    Live.update() before each sleep. Patching the watch-loop sleep helper
    to raise KeyboardInterrupt on the first call exits the loop after one
    iteration. We patch `_status_watch_sleep` rather than `time.sleep`
    globally — a global patch can fire from unrelated stdlib code paths
    (urllib retries, subprocess internals, threading) and bypass the
    loop's try/except, surfacing as Click "Aborted!" exit_code 1."""
    sleep_calls = {"n": 0}

    def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_status_watch_sleep", fake_sleep)

    # Spy on Live so we can verify update() was invoked.
    fake_live = MagicMock()
    fake_live_ctx = MagicMock()
    fake_live_ctx.__enter__.return_value = fake_live
    fake_live_ctx.__exit__.return_value = False

    with patch.object(cli_module, "Live", return_value=fake_live_ctx) as live_cls:
        result = CliRunner().invoke(cli, ["status", "--watch", "1"])

    assert result.exit_code == 0, result.output
    live_cls.assert_called_once()
    assert fake_live.update.call_count >= 1
    assert sleep_calls["n"] == 1


# ---------------------------------------------------------------------------
# 3. --watch loops until KeyboardInterrupt — each cycle re-renders
# ---------------------------------------------------------------------------

def test_status_watch_iterates_until_keyboard_interrupt(monkeypatch, patch_load_engine):
    """Letting time.sleep run twice and then raising KeyboardInterrupt should
    drive Live.update() exactly three times before the loop exits cleanly
    (one update per iteration: 1, 2, then 3 followed by interrupt on sleep)."""
    sleep_calls = {"n": 0}

    def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 3:
            raise KeyboardInterrupt
        return None

    monkeypatch.setattr(cli_module, "_status_watch_sleep", fake_sleep)

    fake_live = MagicMock()
    fake_live_ctx = MagicMock()
    fake_live_ctx.__enter__.return_value = fake_live
    fake_live_ctx.__exit__.return_value = False

    with patch.object(cli_module, "Live", return_value=fake_live_ctx):
        result = CliRunner().invoke(cli, ["status", "--watch", "1"])

    assert result.exit_code == 0, result.output
    assert sleep_calls["n"] == 3
    assert fake_live.update.call_count == 3


# ---------------------------------------------------------------------------
# 4. & 5. click-level validation: --watch must be in [1, 3600]
# ---------------------------------------------------------------------------

def test_status_watch_interval_validation_rejects_zero(patch_load_engine):
    """`--watch 0` is rejected by click's IntRange(min=1) before any engine
    work happens."""
    result = CliRunner().invoke(cli, ["status", "--watch", "0"])
    assert result.exit_code != 0
    # Click renders an out-of-range message that mentions the bound
    assert "0" in result.output or "watch" in result.output.lower()


def test_status_watch_interval_validation_rejects_excessive(patch_load_engine):
    """`--watch 99999` is rejected by click's IntRange(max=3600) before any
    engine work happens."""
    result = CliRunner().invoke(cli, ["status", "--watch", "99999"])
    assert result.exit_code != 0
    assert "99999" in result.output or "watch" in result.output.lower()


# ---------------------------------------------------------------------------
# 6. KeyboardInterrupt prints "watch stopped" — no stack trace leaks through
# ---------------------------------------------------------------------------

def test_status_watch_keyboard_interrupt_prints_stop_message(
    monkeypatch, patch_load_engine
):
    """On Ctrl+C, the watch loop should exit with a friendly "watch stopped"
    message. A bare KeyboardInterrupt (caught by Click's runner) would
    surface as a non-zero exit; we want the user-facing message instead."""
    def fake_sleep(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_status_watch_sleep", fake_sleep)

    fake_live = MagicMock()
    fake_live_ctx = MagicMock()
    fake_live_ctx.__enter__.return_value = fake_live
    fake_live_ctx.__exit__.return_value = False

    with patch.object(cli_module, "Live", return_value=fake_live_ctx):
        result = CliRunner().invoke(cli, ["status", "--watch", "1"])

    assert result.exit_code == 0, result.output
    # The friendly stop message lands on stdout (Rich console) — CliRunner
    # captures both; "watch stopped" is the canonical phrase.
    assert "watch stopped" in result.output


# ---------------------------------------------------------------------------
# 7. _build_status_renderable returns a Rich-renderable object
# ---------------------------------------------------------------------------

def test_build_status_renderable_returns_rich_object(fake_engine):
    """The helper called directly returns something Rich can render — in
    practice a Group, Table, or Text. This is the contract the watch loop
    relies on when calling Live.update(renderable)."""
    result = _build_status_renderable(fake_engine, short=False, pending=False)
    assert isinstance(result, (Group, Table, Text))
    # Sanity: short=True should also yield a renderable (just no per-file table).
    short_result = _build_status_renderable(fake_engine, short=True, pending=False)
    assert isinstance(short_result, (Group, Table, Text))
    # And the pending path returns a renderable too — even for an engine
    # with no mirrors configured (it returns the "no mirrors" Text).
    pending_result = _build_status_renderable(fake_engine, short=False, pending=True)
    assert isinstance(pending_result, (Group, Table, Text))
