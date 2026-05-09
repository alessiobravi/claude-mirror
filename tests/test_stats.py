"""Tests for the `stats` aggregated-usage subcommand (STATS).

Two layers:
    1. Pure-function tests for `aggregate_log` — offline, no fixtures
       beyond hand-built dicts and a frozen window.
    2. CLI-level tests that drive `claude-mirror stats` end-to-end against
       the FakeStorageBackend, asserting the JSON envelope shape, error
       paths, and the watcher-banner suppression in --json mode.

All tests run offline; no real cloud I/O, no real clocks.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror._stats import (
    StatsResult,
    StatsRow,
    StatsTotals,
    aggregate_log,
)
from claude_mirror.cli import cli
from claude_mirror.events import (
    LOGS_FOLDER,
    SYNC_LOG_NAME,
    SyncEvent,
    SyncLog,
)

# Same DeprecationWarning suppression as test_presence.py / test_status_watch.py:
# Click 8.3 emits one from inside CliRunner.invoke and pyproject's
# filterwarnings="error" otherwise turns it into a test failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


def _entry(
    user: str,
    machine: str,
    action: str,
    minutes_ago: int,
    files: list[str] | None = None,
    auto_resolved: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build one raw log dict pinned `minutes_ago` minutes before _NOW."""
    ts = (_NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "user": user,
        "machine": machine,
        "action": action,
        "timestamp": ts,
        "files": files or [],
        "project": "demo",
        "auto_resolved_files": auto_resolved or [],
    }


# ─── Pure-function tests for aggregate_log ─────────────────────────────────────


def test_aggregate_log_empty_input_returns_empty_result():
    out = aggregate_log([], group_by="user")
    assert out.rows == []
    assert out.totals == StatsTotals(events=0, files=0, conflicts=0)
    assert out.group_by == "user"


def test_aggregate_log_groups_by_user_with_correct_counts():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5, files=["a.md"]),
        _entry("alice", "laptop", "push", minutes_ago=10, files=["b.md", "c.md"]),
        _entry("bob", "desktop", "push", minutes_ago=15, files=["x.md"]),
    ]
    out = aggregate_log(entries, group_by="user")
    by_key = {r.key: r for r in out.rows}
    assert by_key["alice"].events == 2
    assert by_key["alice"].files == 3
    assert by_key["bob"].events == 1
    assert by_key["bob"].files == 1
    assert out.totals.events == 3
    assert out.totals.files == 4


def test_aggregate_log_groups_by_machine():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5),
        _entry("bob", "laptop", "push", minutes_ago=10),
        _entry("alice", "desktop", "push", minutes_ago=15),
    ]
    out = aggregate_log(entries, group_by="machine")
    by_key = {r.key: r.events for r in out.rows}
    assert by_key == {"laptop": 2, "desktop": 1}


def test_aggregate_log_groups_by_action():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5),
        _entry("alice", "laptop", "pull", minutes_ago=10),
        _entry("alice", "laptop", "sync", minutes_ago=15),
        _entry("alice", "laptop", "delete", minutes_ago=20),
        _entry("alice", "laptop", "push", minutes_ago=25),
    ]
    out = aggregate_log(entries, group_by="action")
    by_key = {r.key: r.events for r in out.rows}
    assert by_key == {"push": 2, "pull": 1, "sync": 1, "delete": 1}
    # First row is the most-events one (push, 2).
    assert out.rows[0].key == "push"


def test_aggregate_log_groups_by_day_returns_iso_dates_descending():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=60 * 24 * 0 + 30),
        _entry("alice", "laptop", "push", minutes_ago=60 * 24 * 1 + 30),
        _entry("alice", "laptop", "push", minutes_ago=60 * 24 * 2 + 30),
    ]
    out = aggregate_log(entries, group_by="day")
    keys = [r.key for r in out.rows]
    assert keys == sorted(keys, reverse=True)
    assert all(len(k) == 10 and k[4] == "-" and k[7] == "-" for k in keys)


def test_aggregate_log_groups_by_backend_uses_label():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5),
        _entry("bob", "desktop", "push", minutes_ago=10),
    ]
    out = aggregate_log(entries, group_by="backend", backend_label="dropbox")
    assert len(out.rows) == 1
    assert out.rows[0].key == "dropbox"
    assert out.rows[0].events == 2


def test_aggregate_log_filters_entries_outside_since_window():
    since = _NOW - timedelta(hours=1)
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=30),  # in window
        _entry("bob", "desktop", "push", minutes_ago=120),  # excluded
    ]
    out = aggregate_log(entries, since=since, group_by="user")
    keys = [r.key for r in out.rows]
    assert keys == ["alice"]


def test_aggregate_log_filters_entries_after_until_bound():
    until = _NOW - timedelta(minutes=15)
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=30),  # in window
        _entry("bob", "desktop", "push", minutes_ago=10),   # excluded (after until)
    ]
    out = aggregate_log(entries, until=until, group_by="user")
    keys = [r.key for r in out.rows]
    assert keys == ["alice"]


def test_aggregate_log_top_caps_rows():
    entries = [
        _entry(f"u{i}", "m", "push", minutes_ago=i + 1)
        for i in range(10)
    ]
    out = aggregate_log(entries, group_by="user", top=3)
    assert len(out.rows) == 3
    # Totals reflect ALL events, not just the rows kept after the cap.
    assert out.totals.events == 10


def test_aggregate_log_counts_conflicts_from_auto_resolved_files():
    entries = [
        _entry(
            "alice", "laptop", "sync", minutes_ago=5,
            files=["a.md", "b.md"],
            auto_resolved=[{"path": "a.md", "strategy": "keep-local"}],
        ),
        _entry(
            "alice", "laptop", "sync", minutes_ago=10,
            files=["c.md"],
            auto_resolved=[
                {"path": "c.md", "strategy": "keep-remote"},
                {"path": "d.md", "strategy": "keep-remote"},
            ],
        ),
    ]
    out = aggregate_log(entries, group_by="user")
    assert out.rows[0].conflicts == 3
    assert out.totals.conflicts == 3


def test_aggregate_log_skips_malformed_entries():
    entries = [
        "not-a-dict",
        {"timestamp": "garbage"},                                    # bad ts
        {"timestamp": _NOW.isoformat()},                             # missing user/machine
        {"user": "ok", "machine": "ok", "timestamp": "not-a-date"},  # bad ts
        _entry("alice", "laptop", "push", minutes_ago=5),
    ]
    out = aggregate_log(entries, group_by="user")  # type: ignore[arg-type]
    assert [r.key for r in out.rows] == ["alice"]
    assert out.totals.events == 1


def test_aggregate_log_unknown_group_by_raises():
    with pytest.raises(ValueError, match="unknown group_by axis"):
        aggregate_log([], group_by="bogus")


def test_aggregate_log_handles_z_suffix_timestamp():
    raw_ts = (_NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    entries = [{
        "user": "alice", "machine": "laptop", "action": "push",
        "timestamp": raw_ts, "files": ["a.md"], "project": "demo",
    }]
    out = aggregate_log(entries, group_by="user")
    assert len(out.rows) == 1
    assert out.rows[0].key == "alice"


def test_aggregate_log_returns_typed_dataclasses():
    entries = [_entry("alice", "laptop", "push", minutes_ago=5, files=["a.md"])]
    out = aggregate_log(entries, group_by="user")
    assert isinstance(out, StatsResult)
    assert isinstance(out.rows[0], StatsRow)
    assert isinstance(out.totals, StatsTotals)


# ─── CLI-level tests ───────────────────────────────────────────────────────────


def _seed_log(fake_backend, cfg, events: list[SyncEvent]) -> None:
    """Place a SyncLog containing `events` on the fake backend, with the
    same get_file_id shim used in tests/test_json_output.py."""
    log = SyncLog()
    for ev in events:
        log.append(ev)
    logs_folder_id = fake_backend.get_or_create_folder(LOGS_FOLDER, cfg.root_folder)
    fake_backend.upload_bytes(log.to_bytes(), SYNC_LOG_NAME, logs_folder_id)
    real_get_file_id = fake_backend.get_file_id

    def _get_file_or_folder_id(name, folder_id):
        fid = real_get_file_id(name, folder_id)
        if fid is not None:
            return fid
        return fake_backend.folders.get((folder_id, name))

    fake_backend.get_file_id = _get_file_or_folder_id  # type: ignore[method-assign]


@pytest.fixture
def patch_stats(monkeypatch, make_config, fake_backend):
    """Pin Config.load + _create_storage to offline doubles for the
    `stats` command, mirroring the patch_config_and_storage pattern."""
    cfg = make_config()
    monkeypatch.setattr(
        cli_module, "_resolve_config", lambda p: p or "fake-config-path"
    )
    monkeypatch.setattr(cli_module.Config, "load", lambda path: cfg)
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: fake_backend)
    return cfg, fake_backend


def test_stats_json_envelope_v1_shape(patch_stats):
    """`stats --json` returns a parseable JSON envelope under the v1
    schema with the v1.1 additive `result` shape."""
    cfg, fake_backend = patch_stats
    now_iso = datetime.now(timezone.utc)
    _seed_log(fake_backend, cfg, [
        SyncEvent(
            machine="laptop", user="alice",
            timestamp=(now_iso - timedelta(hours=1)).isoformat(),
            files=["a.md"], action="push", project="demo",
        ),
        SyncEvent(
            machine="desktop", user="bob",
            timestamp=(now_iso - timedelta(minutes=15)).isoformat(),
            files=["b.md", "c.md"], action="push", project="demo",
        ),
    ])

    result = CliRunner().invoke(cli, ["stats", "--json", "--by", "user"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "stats"
    assert "rows" in doc["result"]
    assert "totals" in doc["result"]
    assert "since" in doc["result"]
    assert "until" in doc["result"]
    assert doc["result"]["group_by"] == "user"
    keys = {row["key"] for row in doc["result"]["rows"]}
    assert keys == {"alice", "bob"}
    assert doc["result"]["totals"]["events"] == 2
    assert doc["result"]["totals"]["files"] == 3


def test_stats_json_empty_log_returns_empty_rows(patch_stats):
    """No log file on the backend → result has empty rows + zero totals."""
    result = CliRunner().invoke(cli, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["result"]["rows"] == []
    assert doc["result"]["totals"] == {"events": 0, "files": 0, "conflicts": 0}


def test_stats_accepts_relative_since_duration(patch_stats):
    """`--since 30d` parses without error (covers parse_relative_or_iso_date
    wiring)."""
    result = CliRunner().invoke(cli, ["stats", "--json", "--since", "30d"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["result"]["since"] is not None


def test_stats_invalid_since_exits_nonzero_with_clear_error(patch_stats):
    """`--since garbage` exits non-zero with a JSON error envelope (in
    --json mode) whose error.message names the offending flag."""
    result = CliRunner().invoke(cli, ["stats", "--json", "--since", "garbage"])
    assert result.exit_code != 0
    err_doc = json.loads(result.stderr) if result.stderr else json.loads(result.output)
    assert err_doc["command"] == "stats"
    assert "since" in err_doc["error"]["message"].lower()


def test_stats_json_stdout_only_no_banner_leak(patch_stats):
    """--json mode must keep stdout pure — the watcher banner suppression
    in `_CLIGroup.invoke` is the same pattern used by health/log/etc."""
    result = CliRunner().invoke(cli, ["stats", "--json"])
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # raises if anything else is on stdout
    assert "watcher not running" not in result.stdout


def test_stats_default_table_renders_when_log_has_events(patch_stats):
    """Without --json, stats renders the Rich table with totals."""
    cfg, fake_backend = patch_stats
    now_iso = datetime.now(timezone.utc)
    _seed_log(fake_backend, cfg, [
        SyncEvent(
            machine="laptop", user="alice",
            timestamp=(now_iso - timedelta(hours=1)).isoformat(),
            files=["a.md"], action="push", project="demo",
        ),
    ])
    result = CliRunner().invoke(cli, ["stats", "--by", "user"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "Totals" in result.output


def test_stats_empty_state_when_window_has_no_events(patch_stats):
    """Empty log + default 7d window → friendly empty-state message."""
    result = CliRunner().invoke(cli, ["stats"])
    assert result.exit_code == 0, result.output
    assert "No sync events in window" in result.output
