"""Tests for the `status --presence` collaborator visibility feature.

Two layers:
    1. Pure-function tests for `aggregate_presence` (offline, no fixtures
       beyond hand-built dicts and a frozen `now` argument).
    2. CLI-level tests that drive `status --presence` end-to-end against
       the FakeStorageBackend, asserting both the Rich render and the
       v1.1 JSON envelope shape.

All tests run offline against the in-memory fake_backend; no real cloud
I/O, no real clocks (every aggregate call passes a frozen `now`).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from typing import Any

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror._presence import (
    PresenceEntry,
    RECENT_FILES_CAP,
    aggregate_presence,
    humanize_age,
)
from claude_mirror.cli import cli
from claude_mirror.events import (
    LOGS_FOLDER,
    SYNC_LOG_NAME,
    SyncEvent,
    SyncLog,
)
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.sync import SyncEngine

# Click 8.3 emits a DeprecationWarning for Context.protected_args from inside
# CliRunner.invoke; pyproject's filterwarnings = "error" otherwise turns that
# into a test failure. Suppress for this module — same as test_status_watch.py.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_NOW = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)


def _entry(
    user: str,
    machine: str,
    action: str,
    minutes_ago: int,
    files: list[str] | None = None,
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
    }


# ─── Pure-function tests for aggregate_presence ────────────────────────────────


def test_aggregate_presence_empty_log_returns_empty_list():
    assert aggregate_presence([], now=_NOW) == []


def test_aggregate_presence_single_entry_returns_one_presence_entry():
    entries = [_entry("alice", "laptop", "push", minutes_ago=5, files=["a.md"])]
    out = aggregate_presence(entries, ignore_self=False, now=_NOW)
    assert len(out) == 1
    p = out[0]
    assert isinstance(p, PresenceEntry)
    assert p.user == "alice"
    assert p.machine == "laptop"
    assert p.last_action == "push"
    assert p.recent_files == ["a.md"]


def test_aggregate_presence_collapses_same_user_machine_pair():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=30, files=["a.md"]),
        _entry("alice", "laptop", "sync", minutes_ago=10, files=["b.md"]),
        _entry("alice", "laptop", "pull", minutes_ago=5, files=["c.md"]),
    ]
    out = aggregate_presence(entries, ignore_self=False, now=_NOW)
    assert len(out) == 1
    p = out[0]
    assert p.last_action == "pull"
    # Newest event drives last_timestamp.
    assert (p.last_timestamp - (_NOW - timedelta(minutes=5))) == timedelta(0)
    # Files are aggregated newest-first.
    assert p.recent_files[0] == "c.md"
    assert set(p.recent_files) == {"a.md", "b.md", "c.md"}


def test_aggregate_presence_distinct_pairs_sorted_newest_first():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=60),
        _entry("bob", "desktop", "push", minutes_ago=10),
        _entry("carol", "tablet", "push", minutes_ago=30),
    ]
    out = aggregate_presence(entries, ignore_self=False, now=_NOW)
    assert [p.user for p in out] == ["bob", "carol", "alice"]


def test_aggregate_presence_ignore_self_filters_calling_machine():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5),
        _entry("bob", "desktop", "push", minutes_ago=10),
    ]
    out = aggregate_presence(
        entries,
        ignore_self=True,
        self_user="alice",
        self_machine="laptop",
        now=_NOW,
    )
    assert [p.user for p in out] == ["bob"]


def test_aggregate_presence_ignore_self_only_matches_full_tuple():
    # Same user on a different machine is NOT self → keep it.
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5),
        _entry("alice", "desktop", "push", minutes_ago=10),
    ]
    out = aggregate_presence(
        entries,
        ignore_self=True,
        self_user="alice",
        self_machine="laptop",
        now=_NOW,
    )
    assert [(p.user, p.machine) for p in out] == [("alice", "desktop")]


def test_aggregate_presence_excludes_entries_older_than_window():
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=10),
        _entry("bob", "desktop", "push", minutes_ago=60 * 25),
    ]
    out = aggregate_presence(
        entries, ignore_self=False, max_age_hours=24, now=_NOW,
    )
    assert [p.user for p in out] == ["alice"]


def test_aggregate_presence_larger_window_includes_older_entries():
    entries = [
        _entry("bob", "desktop", "push", minutes_ago=60 * 50),
    ]
    out = aggregate_presence(
        entries, ignore_self=False, max_age_hours=72, now=_NOW,
    )
    assert [p.user for p in out] == ["bob"]


def test_aggregate_presence_recent_files_capped_at_five():
    files = [f"file_{i}.md" for i in range(20)]
    entries = [
        _entry("alice", "laptop", "push", minutes_ago=5, files=files),
    ]
    out = aggregate_presence(entries, ignore_self=False, now=_NOW)
    assert len(out[0].recent_files) == RECENT_FILES_CAP == 5
    assert out[0].recent_files == files[:5]


def test_aggregate_presence_skips_malformed_entries():
    entries = [
        {"user": "", "machine": "x", "timestamp": _NOW.isoformat(), "action": "push", "files": []},
        {"user": "alice", "machine": "", "timestamp": _NOW.isoformat(), "action": "push", "files": []},
        {"user": "alice", "machine": "laptop", "timestamp": "not-a-date", "action": "push", "files": []},
        {"user": "alice", "machine": "laptop", "action": "push", "files": []},
        _entry("bob", "desktop", "push", minutes_ago=5),
    ]
    out = aggregate_presence(entries, ignore_self=False, now=_NOW)
    assert [(p.user, p.machine) for p in out] == [("bob", "desktop")]


def test_aggregate_presence_handles_z_suffix_timestamp():
    # Older producers / hand-edited fixtures sometimes use the Z suffix.
    raw_ts = (_NOW - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    entries = [{
        "user": "alice", "machine": "laptop", "action": "push",
        "timestamp": raw_ts, "files": ["a.md"], "project": "demo",
    }]
    out = aggregate_presence(entries, ignore_self=False, now=_NOW)
    assert len(out) == 1
    assert out[0].user == "alice"


def test_humanize_age_renders_expected_buckets():
    assert humanize_age(_NOW, now=_NOW) == "just now"
    assert humanize_age(_NOW - timedelta(seconds=30), now=_NOW) == "just now"
    assert humanize_age(_NOW - timedelta(minutes=3), now=_NOW) == "3m ago"
    assert humanize_age(_NOW - timedelta(hours=2), now=_NOW) == "2h ago"
    assert humanize_age(_NOW - timedelta(days=5), now=_NOW) == "5d ago"


# ─── CLI-level tests for `status --presence` ───────────────────────────────────


def _make_engine_for_cli(make_config, fake_backend):
    cfg = make_config()
    manifest = Manifest(cfg.project_path)
    engine = SyncEngine(
        config=cfg,
        storage=fake_backend,
        manifest=manifest,
        merge=MergeHandler(),
        notifier=None,
        snapshots=None,
        mirrors=[],
    )
    return engine, cfg


def _seed_log(fake_backend, cfg, events: list[SyncEvent]) -> None:
    """Place a SyncLog containing `events` on the fake backend, with the
    same get_file_id shim used in tests/test_json_output.py so the
    folder→file lookup works against our in-memory fake."""
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
def patch_status_with_presence(
    monkeypatch, make_config, fake_backend, project_dir, write_files,
):
    """Wire `status` against an engine pinned to the fake backend, and
    expose the underlying cfg + fake_backend so individual tests can
    seed the sync log."""
    write_files({"a.md": "hello"})
    engine, cfg = _make_engine_for_cli(make_config, fake_backend)
    monkeypatch.setattr(
        cli_module, "_load_engine",
        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend),
    )
    monkeypatch.setattr(
        cli_module, "_resolve_config", lambda p: p or "fake-config-path",
    )
    return engine, cfg, fake_backend


def test_status_presence_renders_other_collaborators(patch_status_with_presence):
    """`status --presence` shows two other collaborators, in newest-first
    order, with their last action surfaced in the table."""
    _, cfg, fake_backend = patch_status_with_presence
    now_iso = datetime.now(timezone.utc)
    _seed_log(fake_backend, cfg, [
        SyncEvent(
            machine="laptop", user="alice",
            timestamp=(now_iso - timedelta(hours=2)).isoformat(),
            files=["a.md"], action="push", project="demo",
        ),
        SyncEvent(
            machine="desktop", user="bob",
            timestamp=(now_iso - timedelta(minutes=10)).isoformat(),
            files=["b.md", "c.md"], action="sync", project="demo",
        ),
        SyncEvent(
            machine="test-machine", user="test-user",
            timestamp=(now_iso - timedelta(minutes=5)).isoformat(),
            files=["self.md"], action="push", project="demo",
        ),
    ])

    result = CliRunner().invoke(cli, ["status", "--presence"])
    assert result.exit_code == 0, result.output
    # Both collaborators appear; the calling machine's own entry is filtered.
    assert "alice" in result.output
    assert "bob" in result.output
    assert "self.md" not in result.output
    assert "Recent collaborator activity" in result.output
    # Newest-first: bob (10m ago) is listed before alice (2h ago).
    assert result.output.find("bob") < result.output.find("alice")


def test_status_presence_empty_state_when_no_other_collaborators(
    patch_status_with_presence,
):
    """`status --presence` against a project where the only entries are
    the calling machine's own (or no entries at all) renders the dim
    empty-state line, NOT an empty table."""
    _, cfg, fake_backend = patch_status_with_presence
    _seed_log(fake_backend, cfg, [
        SyncEvent(
            machine="test-machine", user="test-user",
            timestamp=datetime.now(timezone.utc).isoformat(),
            files=["self.md"], action="push", project="demo",
        ),
    ])
    result = CliRunner().invoke(cli, ["status", "--presence"])
    assert result.exit_code == 0, result.output
    assert "No other collaborators active in the last 24 hours." in result.output


def test_status_presence_json_emits_v11_envelope(patch_status_with_presence):
    """`status --presence --json` emits the v1 envelope with an additive
    `presence` key under `result`. Existing v1 consumers ignore the
    new key; v1.1 consumers can read it."""
    _, cfg, fake_backend = patch_status_with_presence
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
            files=["b.md"], action="push", project="demo",
        ),
    ])

    result = CliRunner().invoke(cli, ["status", "--presence", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert "presence" in doc["result"]
    rows = doc["result"]["presence"]
    assert [r["user"] for r in rows] == ["bob", "alice"]
    for row in rows:
        for key in ("user", "machine", "last_action", "last_timestamp", "recent_files"):
            assert key in row


def test_status_presence_default_off_does_not_fetch_log(patch_status_with_presence):
    """Without --presence, the status command must NOT touch the sync
    log on the backend — the presence fetch is opt-in. Asserts via the
    fake backend's call recorder."""
    _, _, fake_backend = patch_status_with_presence
    fake_backend.calls.clear()
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0, result.output
    log_lookups = [
        c for c in fake_backend.calls
        if c[0] == "get_file_id" and SYNC_LOG_NAME in c
    ]
    assert log_lookups == []


def test_status_presence_json_without_flag_omits_presence_key(
    patch_status_with_presence,
):
    """`status --json` without `--presence` keeps the v1 envelope shape
    untouched — no `presence` key, so v1 consumers see exactly what
    they always saw."""
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert "presence" not in doc["result"]
