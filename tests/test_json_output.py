"""Tests for `--json` output mode (v0.5.39).

Five read-only commands accept `--json`:
    status, history, inbox, log, snapshots

Per the v1 schema:
    success → stdout contains a single flat JSON document shaped as
        {"version": 1, "command": "<name>", "result": ...}
      with all Rich output (tables, banners, progress) suppressed and
      exit code 0.
    error   → stderr contains
        {"version": 1, "command": "<name>", "error": {"type": ..., "message": ...}}
      and exit code 1.

These tests cover, per command:
    * schema shape (top-level keys, version=1, command name),
    * empty result path (no snapshots, no events, no inbox),
    * populated result path (at least one entry shows up correctly),
    * error path (config not found / load failure → JSON error envelope to stderr),
    * stdout JSON parses as valid JSON (sanity).

All tests run offline against the FakeStorageBackend / monkeypatched
helpers in cli.py — no real cloud I/O.
"""
from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from claude_mirror.manifest import Manifest
from claude_mirror.merge import MergeHandler
from claude_mirror.notifier import inbox_path
from claude_mirror.sync import SyncEngine

# Click 8.3 emits a DeprecationWarning for Context.protected_args from
# inside CliRunner.invoke; pyproject's filterwarnings = "error" otherwise
# turns that into a test failure. Suppress for this module — same as
# tests/test_status_watch.py.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _parse_stdout_json(result) -> dict:
    """Parse the CliRunner result's stdout as JSON. CliRunner.invoke with
    mix_stderr=False (default since Click 8.x kept separate streams) puts
    success output on `result.stdout`. We use `result.output` here because
    Click's runner aggregates stdout+stderr by default; tests filter by
    looking for the JSON document boundaries when needed."""
    return json.loads(result.stdout if hasattr(result, "stdout") else result.output)


def _make_engine(make_config, fake_backend) -> SyncEngine:
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
def patch_load_engine(monkeypatch, make_config, fake_backend, project_dir, write_files):
    """Replace cli._load_engine with one that returns a pre-built engine
    pinned to fake_backend. Resolves ./_resolve_config to a stable string."""
    write_files({"a.md": "hello\n"})
    cfg = make_config()
    engine = _make_engine(make_config, fake_backend)
    monkeypatch.setattr(
        cli_module, "_load_engine",
        lambda config_path, with_pubsub=True: (engine, cfg, fake_backend),
    )
    monkeypatch.setattr(
        cli_module, "_resolve_config", lambda p: p or "fake-config-path"
    )
    return engine


@pytest.fixture
def patch_config_and_storage(monkeypatch, make_config, fake_backend):
    """For commands that don't load a SyncEngine — `snapshots`, `history`,
    `log` — patch Config.load + _create_storage to return offline doubles."""
    cfg = make_config()
    monkeypatch.setattr(
        cli_module, "_resolve_config", lambda p: p or "fake-config-path"
    )
    monkeypatch.setattr(cli_module.Config, "load", lambda path: cfg)
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: fake_backend)
    return cfg


# ─── status --json ─────────────────────────────────────────────────────────────

def test_status_json_emits_v1_envelope(patch_load_engine):
    """status --json prints a single JSON document with the v1 envelope:
    {version, command, result.{config_path, summary, files}}."""
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "status"
    assert "config_path" in doc["result"]
    assert "summary" in doc["result"]
    assert "files" in doc["result"]
    # summary has the seven canonical status counts
    expected_summary_keys = {
        "in_sync", "local_ahead", "remote_ahead", "conflict",
        "new_local", "new_remote", "deleted_local",
    }
    assert set(doc["result"]["summary"].keys()) == expected_summary_keys


def test_status_json_with_local_file_lists_it(patch_load_engine):
    """A single local-only file shows up in result.files with status
    `new_local` (the engine reports it as not-yet-pushed)."""
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    paths = [f["path"] for f in doc["result"]["files"]]
    assert "a.md" in paths
    a_md = next(f for f in doc["result"]["files"] if f["path"] == "a.md")
    assert a_md["status"] == "new_local"
    # Hashes are present (local computed; remote/manifest may be null)
    assert a_md["local_hash"] is not None
    # All hash fields are either str or None — never missing
    for key in ("local_hash", "remote_hash", "manifest_hash"):
        assert key in a_md


def test_status_json_suppresses_rich_output(patch_load_engine):
    """--json suppresses ALL Rich output. Stdout is JSON only — no banner
    text, no Rich markup escape codes leaking through, no progress lines."""
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    # Stdout should parse cleanly as JSON; nothing else mixed in.
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    # No "Sync Status" table title leaking through
    assert "Sync Status" not in result.stdout


def test_status_json_mutually_exclusive_flags_error_envelope(patch_load_engine):
    """--pending and --by-backend together emit a JSON error envelope to
    stderr (not the Rich error message) and exit 1."""
    result = CliRunner().invoke(
        cli, ["status", "--json", "--pending", "--by-backend"]
    )
    assert result.exit_code == 1
    # The error envelope lands on stderr — separate stream in Click 8.x
    err_doc = json.loads(result.stderr)
    assert err_doc["version"] == 1
    assert err_doc["command"] == "status"
    assert "error" in err_doc
    assert "type" in err_doc["error"]
    assert "message" in err_doc["error"]


# ─── snapshots --json ──────────────────────────────────────────────────────────

class _FakeSnapshotManager:
    """Lightweight stand-in for SnapshotManager. Only implements the
    methods exercised by the --json paths under test."""

    def __init__(self, snapshot_list=None, history_payload=None):
        self._snapshots = snapshot_list or []
        self._history = history_payload

    def list(self, _external_progress=None):
        return self._snapshots

    def show_list(self):
        return self._snapshots

    def history(self, path):
        if self._history is not None:
            return self._history
        return {"path": path, "entries": [], "distinct_versions": 0, "total_appearances": 0}

    def show_history(self, path):
        return self.history(path)


def test_snapshots_json_empty(patch_config_and_storage, monkeypatch):
    """No snapshots → result is an empty list, exit 0."""
    monkeypatch.setattr(
        cli_module, "SnapshotManager",
        lambda cfg, storage, **kw: _FakeSnapshotManager(snapshot_list=[]),
    )
    result = CliRunner().invoke(cli, ["snapshots", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "snapshots"
    assert doc["result"] == []


def test_snapshots_json_populated(patch_config_and_storage, monkeypatch):
    """Two snapshots → result is a list of two dicts with the v1 schema
    fields {timestamp, format, file_count, size_bytes, source_backend}."""
    snaps = [
        {
            "timestamp": "2026-05-08T10-00-00Z",
            "format": "blobs",
            "total_files": 12,
            "files_changed": ["a.md", "b.md"],
            "triggered_by": "user@machine",
            "action": "push",
        },
        {
            "timestamp": "2026-05-07T09-00-00Z",
            "format": "full",
            "total_files": 10,
            "files_changed": [],
        },
    ]
    monkeypatch.setattr(
        cli_module, "SnapshotManager",
        lambda cfg, storage, **kw: _FakeSnapshotManager(snapshot_list=snaps),
    )
    result = CliRunner().invoke(cli, ["snapshots", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert len(doc["result"]) == 2
    first = doc["result"][0]
    for key in ("timestamp", "format", "file_count", "size_bytes", "source_backend"):
        assert key in first
    assert first["timestamp"] == "2026-05-08T10-00-00Z"
    assert first["format"] == "blobs"
    assert first["file_count"] == 12
    assert first["size_bytes"] is None  # not recorded by the test fixture


def test_snapshots_json_error_envelope_on_config_failure(monkeypatch):
    """A failed Config.load (e.g. file missing) emits a JSON error
    envelope to stderr with exit 1."""
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "/nonexistent.yaml")

    def boom(_path):
        raise FileNotFoundError("config not found: /nonexistent.yaml")
    monkeypatch.setattr(cli_module.Config, "load", boom)

    result = CliRunner().invoke(cli, ["snapshots", "--json"])
    assert result.exit_code == 1
    err_doc = json.loads(result.stderr)
    assert err_doc["version"] == 1
    assert err_doc["command"] == "snapshots"
    assert err_doc["error"]["type"] == "FileNotFoundError"
    assert "config not found" in err_doc["error"]["message"]


# ─── history --json ────────────────────────────────────────────────────────────

def test_history_json_empty(patch_config_and_storage, monkeypatch):
    """No matching versions → result.versions = [], exit 0."""
    monkeypatch.setattr(
        cli_module, "SnapshotManager",
        lambda cfg, storage, **kw: _FakeSnapshotManager(history_payload={
            "path": "memory/notes.md",
            "entries": [],
            "distinct_versions": 0,
            "total_appearances": 0,
        }),
    )
    result = CliRunner().invoke(cli, ["history", "memory/notes.md", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "history"
    assert doc["result"]["path"] == "memory/notes.md"
    assert doc["result"]["versions"] == []


def test_history_json_populated(patch_config_and_storage, monkeypatch):
    """Three appearances of the same file → versions list contains three
    entries with timestamp + hash."""
    payload = {
        "path": "memory/notes.md",
        "entries": [
            {"timestamp": "2026-05-08T10-00-00Z", "format": "blobs", "hash": "deadbeef" * 8, "version": "v2"},
            {"timestamp": "2026-05-07T10-00-00Z", "format": "blobs", "hash": "cafef00d" * 8, "version": "v1"},
            {"timestamp": "2026-05-06T10-00-00Z", "format": "blobs", "hash": "cafef00d" * 8, "version": "v1"},
        ],
        "distinct_versions": 2,
        "total_appearances": 3,
    }
    monkeypatch.setattr(
        cli_module, "SnapshotManager",
        lambda cfg, storage, **kw: _FakeSnapshotManager(history_payload=payload),
    )
    result = CliRunner().invoke(cli, ["history", "memory/notes.md", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert len(doc["result"]["versions"]) == 3
    first = doc["result"]["versions"][0]
    assert first["timestamp"] == "2026-05-08T10-00-00Z"
    assert first["hash"].startswith("deadbeef")
    # version label preserved
    assert first["version"] == "v2"


def test_history_json_error_envelope_on_config_failure(monkeypatch):
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "/nonexistent.yaml")

    def boom(_path):
        raise FileNotFoundError("config not found")
    monkeypatch.setattr(cli_module.Config, "load", boom)

    result = CliRunner().invoke(cli, ["history", "x.md", "--json"])
    assert result.exit_code == 1
    err_doc = json.loads(result.stderr)
    assert err_doc["command"] == "history"
    assert err_doc["error"]["type"] == "FileNotFoundError"


# ─── inbox --json ──────────────────────────────────────────────────────────────

def test_inbox_json_empty(monkeypatch, make_config, project_dir):
    """An empty inbox → result.events = [], exit 0. Verifies the spec's
    'On empty inbox: result.events: []' contract."""
    cfg = make_config()
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "fake-config")
    monkeypatch.setattr(cli_module.Config, "load", lambda path: cfg)
    # No inbox file exists yet under the temp project_dir
    result = CliRunner().invoke(cli, ["inbox", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "inbox"
    assert doc["result"] == {"events": []}


def test_inbox_json_populated_and_clears_inbox(monkeypatch, make_config, project_dir):
    """Inbox with events → result.events lists each event AND the inbox
    is cleared afterward (same semantics as the Rich path)."""
    cfg = make_config()
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "fake-config")
    monkeypatch.setattr(cli_module.Config, "load", lambda path: cfg)
    # Drop two inbox events directly into the project's inbox file
    path = inbox_path(str(project_dir))
    path.write_text(
        json.dumps({
            "timestamp": "2026-05-08T10:00:00Z",
            "user": "alice", "machine": "laptop",
            "action": "push", "files": ["a.md"], "project": "demo",
        }) + "\n" +
        json.dumps({
            "timestamp": "2026-05-08T10:05:00Z",
            "user": "bob", "machine": "desktop",
            "action": "push", "files": ["b.md"], "project": "demo",
        }) + "\n"
    )
    result = CliRunner().invoke(cli, ["inbox", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert len(doc["result"]["events"]) == 2
    assert doc["result"]["events"][0]["user"] == "alice"
    assert doc["result"]["events"][1]["user"] == "bob"
    # Inbox cleared by the read+truncate path
    assert path.read_text() == ""


def test_inbox_json_error_envelope_on_config_failure(monkeypatch):
    """Unlike the Rich path (which silently exits 0 to keep PreToolUse
    hooks quiet), the --json path surfaces config errors as JSON so
    scripts can act on them."""
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "/nonexistent.yaml")

    def boom(_path):
        raise FileNotFoundError("config not found")
    monkeypatch.setattr(cli_module.Config, "load", boom)

    result = CliRunner().invoke(cli, ["inbox", "--json"])
    assert result.exit_code == 1
    err_doc = json.loads(result.stderr)
    assert err_doc["command"] == "inbox"
    assert err_doc["error"]["type"] == "FileNotFoundError"


# ─── log --json ────────────────────────────────────────────────────────────────

def test_log_json_empty_when_no_log_exists(patch_config_and_storage, fake_backend):
    """No log file on the backend → result is an empty list."""
    # fake_backend has no log file by default; get_file_id returns None
    result = CliRunner().invoke(cli, ["log", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "log"
    assert doc["result"] == []


def test_log_json_populated(patch_config_and_storage, fake_backend, monkeypatch):
    """A log file with two events → result is a list of two entries
    newest-first with timestamp/user/machine/action/files/project keys."""
    cfg = patch_config_and_storage
    # Build a SyncLog with two events and store it on the fake backend
    log = SyncLog()
    log.append(SyncEvent(
        machine="laptop", user="alice",
        timestamp="2026-05-07T10:00:00Z",
        files=["a.md"], action="push", project="demo",
    ))
    log.append(SyncEvent(
        machine="desktop", user="bob",
        timestamp="2026-05-08T11:00:00Z",
        files=["b.md", "c.md"], action="push", project="demo",
    ))
    # Place the log file at LOGS_FOLDER / SYNC_LOG_NAME under root.
    # Real backends' get_file_id resolves both files and folders; the
    # FakeStorageBackend only resolves files, so we shim get_file_id to
    # also check the folder map (LOGS_FOLDER lives in the folder map).
    logs_folder_id = fake_backend.get_or_create_folder(LOGS_FOLDER, cfg.root_folder)
    fake_backend.upload_bytes(log.to_bytes(), SYNC_LOG_NAME, logs_folder_id)
    real_get_file_id = fake_backend.get_file_id

    def _get_file_or_folder_id(name, folder_id):
        fid = real_get_file_id(name, folder_id)
        if fid is not None:
            return fid
        # Fall back to the folder map — real backends conflate file +
        # folder lookups under one method, our fake separates them.
        return fake_backend.folders.get((folder_id, name))
    monkeypatch.setattr(fake_backend, "get_file_id", _get_file_or_folder_id)

    result = CliRunner().invoke(cli, ["log", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert len(doc["result"]) == 2
    # Newest-first
    assert doc["result"][0]["user"] == "bob"
    assert doc["result"][0]["timestamp"] == "2026-05-08T11:00:00Z"
    # Required keys
    for key in ("timestamp", "user", "machine", "action", "files", "project", "snapshot_timestamp"):
        assert key in doc["result"][0]
    # snapshot_timestamp is reserved + always null in v1
    assert doc["result"][0]["snapshot_timestamp"] is None


def test_log_json_error_envelope_on_config_failure(monkeypatch):
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "/nonexistent.yaml")

    def boom(_path):
        raise FileNotFoundError("config not found")
    monkeypatch.setattr(cli_module.Config, "load", boom)

    result = CliRunner().invoke(cli, ["log", "--json"])
    assert result.exit_code == 1
    err_doc = json.loads(result.stderr)
    assert err_doc["command"] == "log"
    assert err_doc["error"]["type"] == "FileNotFoundError"


# ─── Cross-cutting: every --json document is valid JSON ────────────────────────

@pytest.mark.parametrize("argv", [
    ["snapshots", "--json"],
    ["history", "memory/notes.md", "--json"],
    ["log", "--json"],
])
def test_json_stdout_is_parseable_for_each_command(
    argv, patch_config_and_storage, monkeypatch
):
    """Every --json command's stdout is exactly one parseable JSON
    document — no surrounding text, no leading banner, nothing trailing."""
    # Stub SnapshotManager for snapshots/history paths
    monkeypatch.setattr(
        cli_module, "SnapshotManager",
        lambda cfg, storage, **kw: _FakeSnapshotManager(
            snapshot_list=[],
            history_payload={"path": "memory/notes.md", "entries": [],
                             "distinct_versions": 0, "total_appearances": 0},
        ),
    )
    result = CliRunner().invoke(cli, argv)
    assert result.exit_code == 0, result.output
    # The full stdout parses as JSON in one shot.
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == argv[0]


def test_json_status_stdout_is_parseable(patch_load_engine):
    """Same parseability check for `status --json` (separate fixture)."""
    result = CliRunner().invoke(cli, ["status", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "status"


def test_json_inbox_stdout_is_parseable(monkeypatch, make_config, project_dir):
    cfg = make_config()
    monkeypatch.setattr(cli_module, "_resolve_config", lambda p: "fake-config")
    monkeypatch.setattr(cli_module.Config, "load", lambda path: cfg)
    result = CliRunner().invoke(cli, ["inbox", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["version"] == 1
    assert doc["command"] == "inbox"
