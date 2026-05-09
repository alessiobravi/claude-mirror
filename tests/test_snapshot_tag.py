"""Tests for SNAP-TAG: named snapshot tags and messages.

Covers:
    * Tag-name validation regex (^[A-Za-z0-9._-]{1,64}$).
    * Per-project tag uniqueness on create.
    * `--message` without `--tag` and `--tag` without `--message`.
    * Manifest round-trip of `tag` + `message` for both formats.
    * `restore --tag NAME` resolves to the right timestamp.
    * `restore --tag NAME` for an unknown tag exits non-zero with the
      list of available tags.
    * `restore --tag NAME` mutually exclusive with positional TIMESTAMP.
    * Pre-existing snapshots without tag/message keys load cleanly.
    * `snapshots` table renders Tag + Message columns; truncation works.
    * `history PATH` enriched with the tag bracket.
    * `prune --include-tagged` includes tagged snapshots; default skips them.
    * `forget TIMESTAMP` (explicit) deletes a tagged snapshot — explicit
      positional path is not protected.
    * Migration preserves tag + message across full→blobs and blobs→full.

All tests use the in-memory backend from `tests/test_snapshots.py` so they
stay offline + fast.
"""
from __future__ import annotations

import json
from typing import Any, List, Optional
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.config import Config

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from claude_mirror.snapshots import (
    MANIFEST_SUFFIX,
    MAX_MESSAGE_LEN,
    SNAPSHOT_META_FILE,
    SNAPSHOTS_FOLDER,
    SnapshotManager,
    _TAG_NAME_RE,
    _truncate_message_for_table,
    _validate_message,
    _validate_tag_name,
)

# Reuse the in-memory backend defined for test_snapshots.py — it models a
# full StorageBackend so SnapshotManager can drive it transparently.
from tests.test_snapshots import InMemoryBackend, _make_manager


# ---------------------------------------------------------------------------
# 1. Tag-name validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "v1.0",
    "v1",
    "release-2026-05-09",
    "foo.bar_baz",
    "A",
    "0",
    "abc-XYZ_123.4",
    "x" * 64,
])
def test_validate_tag_name_accepts_valid(name: str) -> None:
    """Names matching ^[A-Za-z0-9._-]{1,64}$ are accepted."""
    _validate_tag_name(name)
    assert _TAG_NAME_RE.match(name)


@pytest.mark.parametrize("name", [
    "",
    " ",
    "with space",
    "with/slash",
    "trailing\n",
    "tab\there",
    "name@host",
    "naïve",
    "x" * 65,
    "!bang",
    "(parens)",
    ":colon",
])
def test_validate_tag_name_rejects_invalid(name: str) -> None:
    """Empty, whitespace, slashes, '@', non-ASCII, >64 chars all reject."""
    with pytest.raises(ValueError, match="Invalid tag name"):
        _validate_tag_name(name)


def test_validate_message_accepts_within_cap() -> None:
    """Messages up to MAX_MESSAGE_LEN characters are fine."""
    _validate_message("")
    _validate_message("hello")
    _validate_message("x" * MAX_MESSAGE_LEN)


def test_validate_message_rejects_over_cap() -> None:
    """Messages over MAX_MESSAGE_LEN raise."""
    with pytest.raises(ValueError, match="too long"):
        _validate_message("x" * (MAX_MESSAGE_LEN + 1))


# ---------------------------------------------------------------------------
# 2. Manifest round-trip — blobs format
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_backend() -> InMemoryBackend:
    return InMemoryBackend(name="primary", root_folder="ROOT")


@pytest.fixture
def stepped_clock(monkeypatch):
    """Patch `claude_mirror.snapshots.datetime` so each `now()` call
    returns 60s after the last — multiple `create()` calls in one test
    would otherwise collide in the same second and overwrite manifests."""
    import datetime as _dt
    from claude_mirror import snapshots as _snap_mod

    state = {"step": 0}
    base = _dt.datetime(2026, 1, 1, 12, 0, 0, tzinfo=_dt.timezone.utc)

    class _SteppedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            value = base + _dt.timedelta(minutes=state["step"])
            state["step"] += 1
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    monkeypatch.setattr(_snap_mod, "datetime", _SteppedDateTime)
    return state


def test_blobs_manifest_writes_tag_and_message(
    make_config, memory_backend, write_files,
) -> None:
    """A blobs-format snapshot's manifest JSON has tag + message keys."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    ts = mgr.create(
        action="manual", files_changed=[],
        tag="v1.0", message="first stable release",
    )

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert manifest["tag"] == "v1.0"
    assert manifest["message"] == "first stable release"


def test_full_manifest_writes_tag_and_message(
    make_config, memory_backend, write_files,
) -> None:
    """A full-format snapshot's meta sidecar has tag + message keys."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="full")
    write_files({"a.md": "AAA"})
    parent_id, fname = memory_backend.resolve_path("a.md", "ROOT")
    memory_backend.upload_bytes(b"AAA", fname, parent_id)
    mgr = _make_manager(cfg, memory_backend)

    ts = mgr.create(
        action="manual", files_changed=[],
        tag="full-v1", message="full snapshot tagged",
    )

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    folders = memory_backend.list_folders(snaps_id, name=ts)
    assert len(folders) == 1
    snap_folder_id = folders[0]["id"]
    meta_id = memory_backend.get_file_id(SNAPSHOT_META_FILE, snap_folder_id)
    meta = json.loads(memory_backend.download_file(meta_id))
    assert meta["tag"] == "full-v1"
    assert meta["message"] == "full snapshot tagged"


def test_create_message_only_works(
    make_config, memory_backend, write_files,
) -> None:
    """A messaged-but-untagged snapshot is fine — same as a git commit
    message without a `git tag` later."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    ts = mgr.create(
        action="manual", files_changed=[],
        tag=None, message="before the big refactor",
    )

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert manifest["tag"] is None
    assert manifest["message"] == "before the big refactor"


def test_create_tag_only_works(
    make_config, memory_backend, write_files,
) -> None:
    """A tagged snapshot without a message is fine."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    ts = mgr.create(action="manual", files_changed=[], tag="anchor", message=None)

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert manifest["tag"] == "anchor"
    assert manifest["message"] is None


def test_create_neither_tag_nor_message_is_back_compat(
    make_config, memory_backend, write_files,
) -> None:
    """The pre-SNAP-TAG behaviour: no tag, no message → both keys None."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    ts = mgr.create(action="push", files_changed=["a.md"])

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert manifest["tag"] is None
    assert manifest["message"] is None


# ---------------------------------------------------------------------------
# 3. Tag-name validation on create
# ---------------------------------------------------------------------------


def test_create_rejects_invalid_tag(
    make_config, memory_backend, write_files,
) -> None:
    """Invalid tag names are rejected before any remote write."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    with pytest.raises(ValueError, match="Invalid tag name"):
        mgr.create(action="manual", files_changed=[], tag="with space")
    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    files_now = memory_backend.list_files_recursive(snaps_id)
    assert files_now == [], "Validation must run before any remote write."


def test_create_rejects_too_long_message(
    make_config, memory_backend, write_files,
) -> None:
    """Over-length messages are rejected before any remote write."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    with pytest.raises(ValueError, match="too long"):
        mgr.create(
            action="manual", files_changed=[],
            message="x" * (MAX_MESSAGE_LEN + 1),
        )


# ---------------------------------------------------------------------------
# 4. Per-project uniqueness
# ---------------------------------------------------------------------------


def test_create_with_duplicate_tag_errors(
    make_config, memory_backend, write_files, stepped_clock,
) -> None:
    """A second snapshot --tag X errors clearly when X is already in use."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    first_ts = mgr.create(action="manual", files_changed=[], tag="v1.0")

    write_files({"a.md": "BBB"})  # change content so timestamp differs
    with pytest.raises(ValueError, match="already in use"):
        mgr.create(action="manual", files_changed=[], tag="v1.0")


def test_find_by_tag_returns_match(
    make_config, memory_backend, write_files,
) -> None:
    """find_by_tag returns the matching snapshot dict."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "AAA"})
    mgr = _make_manager(cfg, memory_backend)

    ts = mgr.create(action="manual", files_changed=[], tag="release")
    found = mgr.find_by_tag("release")
    assert found is not None
    assert found["timestamp"] == ts
    assert mgr.find_by_tag("does-not-exist") is None


def test_list_tags_returns_sorted_unique(
    make_config, memory_backend, write_files, stepped_clock,
) -> None:
    """list_tags returns the in-use tag set, sorted, deduplicated."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "A"})
    mgr = _make_manager(cfg, memory_backend)

    mgr.create(action="manual", files_changed=[], tag="zeta")
    write_files({"a.md": "B"})
    mgr.create(action="manual", files_changed=[], tag="alpha")
    write_files({"a.md": "C"})
    mgr.create(action="manual", files_changed=[])  # untagged

    assert mgr.list_tags() == ["alpha", "zeta"]


# ---------------------------------------------------------------------------
# 5. Restore by tag
# ---------------------------------------------------------------------------


def test_restore_by_tag_resolves_to_timestamp(
    make_config, memory_backend, write_files, project_dir,
) -> None:
    """resolve_tag_to_timestamp + restore round-trips a tagged snapshot."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "ORIGINAL"})
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(action="manual", files_changed=[], tag="anchor")

    resolved = mgr.resolve_tag_to_timestamp("anchor")
    assert resolved == ts

    (project_dir / "a.md").unlink()
    mgr.restore(timestamp=resolved, output_path=str(project_dir))
    assert (project_dir / "a.md").read_bytes() == b"ORIGINAL"


def test_resolve_tag_unknown_lists_available(
    make_config, memory_backend, write_files, stepped_clock,
) -> None:
    """An unknown tag raises ValueError listing the tags that DO exist."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "A"})
    mgr = _make_manager(cfg, memory_backend)
    mgr.create(action="manual", files_changed=[], tag="alpha")
    write_files({"a.md": "B"})
    mgr.create(action="manual", files_changed=[], tag="beta")

    with pytest.raises(ValueError) as excinfo:
        mgr.resolve_tag_to_timestamp("gamma")
    msg = str(excinfo.value)
    assert "not found" in msg
    assert "'alpha'" in msg
    assert "'beta'" in msg


def test_resolve_tag_no_tags_in_project_hints_creation(
    make_config, memory_backend,
) -> None:
    """When zero tags exist, the error message points at `snapshot --tag`."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    mgr = _make_manager(cfg, memory_backend)

    with pytest.raises(ValueError, match="No snapshots in this project"):
        mgr.resolve_tag_to_timestamp("anything")


# ---------------------------------------------------------------------------
# 6. CLI wiring (via stub manager)
# ---------------------------------------------------------------------------


class _StubMgr(SnapshotManager):
    """Minimal stub for CLI wiring tests — bypasses real storage but
    exposes enough to drive `snapshot --tag`, `restore --tag`, etc."""

    def __init__(self, snapshots: List[dict[str, Any]], config: Config) -> None:
        self.config = config
        self.storage = MagicMock()
        self._mirrors: List[Any] = []
        self._snapshots_to_return = snapshots
        self.created_with: List[dict[str, Any]] = []
        self.restored_ts: Optional[str] = None

    def list(self, _external_progress: Any = None) -> List[dict[str, Any]]:
        return list(self._snapshots_to_return)

    def create(
        self,
        action: str,
        files_changed: list[str],
        tag: Optional[str] = None,
        message: Optional[str] = None,
    ) -> str:
        if tag is not None:
            _validate_tag_name(tag)
            for s in self._snapshots_to_return:
                if s.get("tag") == tag:
                    raise ValueError(
                        f"Tag {tag!r} already in use by snapshot "
                        f"{s['timestamp']!r}."
                    )
        if message is not None:
            _validate_message(message)
        ts = "2026-05-09T12-34-56Z"
        self.created_with.append({"tag": tag, "message": message, "ts": ts})
        self._snapshots_to_return.append(
            {"timestamp": ts, "format": "blobs", "tag": tag,
             "message": message, "manifest_id": "m"}
        )
        return ts

    def restore(
        self, timestamp: str, output_path: str,
        paths: Optional[list[str]] = None,
        backend_name: Optional[str] = None,
    ) -> None:
        self.restored_ts = timestamp


@pytest.fixture
def yaml_config_path(tmp_path, project_dir, config_dir, make_config):
    cfg = make_config()
    cfg_path = tmp_path / "claude_mirror.yaml"
    cfg.save(str(cfg_path))
    return str(cfg_path)


@pytest.fixture
def stub_factory(monkeypatch):
    """Patch the CLI's SnapshotManager construction to return a stub."""
    instances: List[_StubMgr] = []
    snapshots_state: List[dict[str, Any]] = []

    def factory(config, storage, *_, **__):
        mgr = _StubMgr(snapshots_state, config)
        instances.append(mgr)
        return mgr

    monkeypatch.setattr(cli_module, "SnapshotManager", factory)
    monkeypatch.setattr(cli_module, "_create_storage", lambda cfg: MagicMock())
    monkeypatch.setattr(cli_module, "_create_storage_set",
                        lambda cfg: (MagicMock(), []))
    return instances


def test_cli_snapshot_with_tag_and_message(yaml_config_path, stub_factory):
    """`claude-mirror snapshot --tag v1.0 --message "first stable"`."""
    result = CliRunner().invoke(
        cli, [
            "snapshot", "--tag", "v1.0",
            "--message", "first stable release",
            "--config", yaml_config_path,
        ],
    )
    assert result.exit_code == 0, result.output
    assert stub_factory[-1].created_with == [
        {"tag": "v1.0", "message": "first stable release",
         "ts": "2026-05-09T12-34-56Z"}
    ]
    assert "Snapshot created" in result.output


def test_cli_snapshot_no_flags_creates_untagged(yaml_config_path, stub_factory):
    """`claude-mirror snapshot` with no flags is the legacy path."""
    result = CliRunner().invoke(
        cli, ["snapshot", "--config", yaml_config_path],
    )
    assert result.exit_code == 0, result.output
    last = stub_factory[-1].created_with[-1]
    assert last["tag"] is None
    assert last["message"] is None


def test_cli_snapshot_invalid_tag_exits_nonzero(yaml_config_path, stub_factory):
    """An invalid tag name aborts before any side-effect."""
    result = CliRunner().invoke(
        cli, [
            "snapshot", "--tag", "with space",
            "--config", yaml_config_path,
        ],
    )
    assert result.exit_code == 1
    assert "Invalid tag name" in result.output


def test_cli_snapshot_duplicate_tag_exits_nonzero(yaml_config_path, stub_factory):
    """A duplicate tag prints a clear error and exits 1."""
    runner = CliRunner()
    r1 = runner.invoke(
        cli, ["snapshot", "--tag", "v1.0", "--config", yaml_config_path],
    )
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(
        cli, ["snapshot", "--tag", "v1.0", "--config", yaml_config_path],
    )
    assert r2.exit_code == 1
    assert "already in use" in r2.output


def test_cli_restore_by_tag_uses_resolved_timestamp(
    yaml_config_path, stub_factory, monkeypatch,
):
    """`restore --tag NAME` resolves the tag and restores by timestamp."""
    runner = CliRunner()
    r1 = runner.invoke(
        cli, ["snapshot", "--tag", "anchor", "--config", yaml_config_path],
    )
    assert r1.exit_code == 0, r1.output

    # The restore CLI calls click.confirm for the in-place overwrite — bypass.
    monkeypatch.setattr("click.confirm", lambda *_, **__: True)
    r2 = runner.invoke(
        cli, [
            "restore", "--tag", "anchor",
            "--output", "/tmp/snap-tag-restore",
            "--config", yaml_config_path,
        ],
    )
    assert r2.exit_code == 0, r2.output
    # The stub records what timestamp was actually passed to restore().
    last_mgr = stub_factory[-1]
    assert last_mgr.restored_ts == "2026-05-09T12-34-56Z"
    assert "Resolved tag" in r2.output


def test_cli_restore_unknown_tag_exits_nonzero(yaml_config_path, stub_factory):
    """Unknown tag → exit 1, error names the unknown tag."""
    result = CliRunner().invoke(
        cli, [
            "restore", "--tag", "no-such",
            "--output", "/tmp/x",
            "--config", yaml_config_path,
        ],
    )
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cli_restore_tag_and_timestamp_mutually_exclusive(
    yaml_config_path, stub_factory,
):
    """Passing both TIMESTAMP and --tag is a clean error."""
    result = CliRunner().invoke(
        cli, [
            "restore", "2026-01-01T00-00-00Z", "--tag", "v1.0",
            "--config", yaml_config_path,
        ],
    )
    assert result.exit_code == 1
    assert "not both" in result.output.lower() or "either" in result.output.lower()


def test_cli_restore_missing_identifier_errors(yaml_config_path, stub_factory):
    """Neither TIMESTAMP nor --tag → friendly error."""
    result = CliRunner().invoke(
        cli, ["restore", "--config", yaml_config_path],
    )
    assert result.exit_code == 1
    assert "Missing" in result.output


# ---------------------------------------------------------------------------
# 7. Back-compat: pre-SNAP-TAG manifests load cleanly
# ---------------------------------------------------------------------------


def test_pre_snap_tag_blobs_manifest_loads_cleanly(
    make_config, memory_backend,
) -> None:
    """A blobs manifest written WITHOUT tag/message keys (the v0.5.59
    shape) loads via `list()` with both fields = None."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    legacy = {
        "format": "v2",
        "timestamp": "2025-12-01T00-00-00Z",
        "triggered_by": "old@machine",
        "action": "push",
        "files_changed": ["a.md"],
        "total_files": 1,
        "files": {"a.md": "deadbeef"},
    }
    memory_backend.upload_bytes(
        json.dumps(legacy).encode(),
        f"{legacy['timestamp']}{MANIFEST_SUFFIX}", snaps_id,
    )

    mgr = _make_manager(cfg, memory_backend)
    listing = mgr.list()
    assert len(listing) == 1
    assert listing[0]["timestamp"] == "2025-12-01T00-00-00Z"
    assert listing[0]["tag"] is None
    assert listing[0]["message"] is None


def test_pre_snap_tag_full_meta_loads_cleanly(
    make_config, memory_backend,
) -> None:
    """A full-format meta sidecar without tag/message keys loads with
    both fields = None."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="full")
    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    folder_id = memory_backend.get_or_create_folder(
        "2025-11-01T00-00-00Z", snaps_id,
    )
    memory_backend.upload_bytes(
        json.dumps({
            "timestamp": "2025-11-01T00-00-00Z",
            "triggered_by": "old@machine",
            "action": "push",
            "files_changed": [],
            "total_files": 0,
            "format": "full",
        }).encode(),
        SNAPSHOT_META_FILE, folder_id,
    )

    mgr = _make_manager(cfg, memory_backend)
    listing = mgr.list()
    assert len(listing) == 1
    assert listing[0]["tag"] is None
    assert listing[0]["message"] is None


# ---------------------------------------------------------------------------
# 8. Snapshots table + history rendering
# ---------------------------------------------------------------------------


def test_truncate_message_for_table_short_passthrough() -> None:
    """A short message renders as-is."""
    assert _truncate_message_for_table("hello") == "hello"


def test_truncate_message_for_table_long_truncated() -> None:
    """A long message is collapsed + truncated with an ellipsis."""
    long = "x" * 100
    out = _truncate_message_for_table(long)
    assert out.endswith("…")
    assert len(out) <= 50  # the configured width


def test_truncate_message_for_table_empty_renders_dim() -> None:
    """Empty / None messages render as a dim em-dash."""
    assert _truncate_message_for_table(None) == "[dim]—[/]"
    assert _truncate_message_for_table("") == "[dim]—[/]"


def test_show_list_includes_tag_and_message_columns(
    make_config, memory_backend, write_files, capsys,
) -> None:
    """`show_list()` renders Tag + Message columns. Drive the underlying
    `list()` directly to assert the data plumbing — Rich's terminal-width
    truncation is incidental to the SNAP-TAG contract."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "A"})
    mgr = _make_manager(cfg, memory_backend)
    mgr.create(
        action="manual", files_changed=[],
        tag="v1.0", message="first release ever",
    )

    listing = mgr.list()
    assert listing[0]["tag"] == "v1.0"
    assert listing[0]["message"] == "first release ever"

    # And the Rich path renders without raising (column header text reaches
    # stdout even when row content is width-truncated).
    mgr.show_list()
    out = capsys.readouterr().out
    assert "Tag" in out
    assert "Message" in out


def test_history_propagates_tag_field(
    make_config, memory_backend, write_files,
) -> None:
    """`history(PATH)` surfaces the `tag` field on each entry — that's
    what powers the dim `[tag]` label in show_history's table cell."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    write_files({"a.md": "A"})
    mgr = _make_manager(cfg, memory_backend)
    mgr.create(action="manual", files_changed=[], tag="v1.0")

    result = mgr.history("a.md")
    entries = result["entries"]
    assert len(entries) == 1
    assert entries[0].get("tag") == "v1.0"


# ---------------------------------------------------------------------------
# 9. Tag-protected pruning / forget
# ---------------------------------------------------------------------------


def test_prune_skips_tagged_by_default(
    make_config, memory_backend, write_files, stepped_clock,
) -> None:
    """`prune_per_retention(keep_last=1)` does NOT delete a tagged
    snapshot even when it falls outside the keep-set."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    mgr = _make_manager(cfg, memory_backend)

    write_files({"a.md": "A"})
    ts1 = mgr.create(action="manual", files_changed=[], tag="anchor")
    write_files({"a.md": "B"})
    ts2 = mgr.create(action="manual", files_changed=[])
    write_files({"a.md": "C"})
    ts3 = mgr.create(action="manual", files_changed=[])

    out = mgr.prune_per_retention(keep_last=1, dry_run=False)
    # Newest is ts3 → kept. ts2 (untagged) → deleted. ts1 (tagged) → SHIELDED.
    assert ts1 in out["skipped_tagged"]
    assert ts2 in out["to_delete"]
    assert ts1 not in out["to_delete"]


def test_prune_with_include_tagged_deletes_tagged(
    make_config, memory_backend, write_files, stepped_clock,
) -> None:
    """`include_tagged=True` opts in to deleting tagged snapshots too."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    mgr = _make_manager(cfg, memory_backend)

    write_files({"a.md": "A"})
    ts1 = mgr.create(action="manual", files_changed=[], tag="anchor")
    write_files({"a.md": "B"})
    ts2 = mgr.create(action="manual", files_changed=[])
    write_files({"a.md": "C"})
    ts3 = mgr.create(action="manual", files_changed=[])

    out = mgr.prune_per_retention(keep_last=1, dry_run=False, include_tagged=True)
    assert ts1 in out["to_delete"]
    assert ts2 in out["to_delete"]
    assert out["skipped_tagged"] == []


def test_forget_before_skips_tagged_by_default(
    make_config, memory_backend, write_files, monkeypatch,
) -> None:
    """`forget --before 1d` shields a tagged snapshot from rule-based
    deletion. The tagged-skip happens regardless of dry_run."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    mgr = _make_manager(cfg, memory_backend)

    write_files({"a.md": "A"})
    ts1 = mgr.create(action="manual", files_changed=[], tag="anchor")

    out = mgr.forget(before="0d", dry_run=True)
    assert ts1 in out["skipped_tagged"]


def test_forget_explicit_timestamp_deletes_tagged(
    make_config, memory_backend, write_files,
) -> None:
    """An explicit positional `forget TIMESTAMP` deletes a tagged
    snapshot — protection only applies to rule-based selectors."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="blobs")
    mgr = _make_manager(cfg, memory_backend)

    write_files({"a.md": "A"})
    ts1 = mgr.create(action="manual", files_changed=[], tag="anchor")

    out = mgr.forget(timestamps=[ts1], dry_run=False)
    assert out["selected"] == 1
    assert out["deleted"] == 1
    # After deletion the tag is no longer in the listing.
    assert mgr.find_by_tag("anchor") is None


# ---------------------------------------------------------------------------
# 10. Migration preserves tag + message
# ---------------------------------------------------------------------------


def test_migrate_full_to_blobs_preserves_tag_and_message(
    make_config, memory_backend, write_files,
) -> None:
    """Converting a tagged full-format snapshot into blobs format
    carries the tag + message into the new manifest."""
    cfg = make_config(drive_folder_id="ROOT", snapshot_format="full")
    write_files({"a.md": "AAA"})
    parent_id, fname = memory_backend.resolve_path("a.md", "ROOT")
    memory_backend.upload_bytes(b"AAA", fname, parent_id)
    mgr = _make_manager(cfg, memory_backend)
    ts = mgr.create(
        action="manual", files_changed=[],
        tag="archive", message="archive of v0",
    )

    mgr.migrate(target="blobs", dry_run=False)

    snaps_id = memory_backend.get_or_create_folder(SNAPSHOTS_FOLDER, "ROOT")
    manifest_id = memory_backend.get_file_id(f"{ts}{MANIFEST_SUFFIX}", snaps_id)
    assert manifest_id is not None
    manifest = json.loads(memory_backend.download_file(manifest_id))
    assert manifest["tag"] == "archive"
    assert manifest["message"] == "archive of v0"
