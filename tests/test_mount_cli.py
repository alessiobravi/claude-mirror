"""Tests for the CLI surface of `claude-mirror mount` and
`claude-mirror umount` (the read-only FUSE mount package).

These tests exercise the CLI's flag handling, error paths, dispatch
into the right variant class, and the cross-platform umount wrapper.
The engine itself (`claude_mirror._mount`) is mocked end-to-end so the
suite runs without fusepy installed and without a real FUSE kernel
layer present.

Conventions:
    * Offline.
    * <100ms each.
    * No real `claude_mirror._mount` import — `sys.modules` is
      pre-populated with a stand-in module per test, then removed in
      teardown.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any, List, Tuple
from unittest.mock import MagicMock

import click
import pytest
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Stand-ins for the engine module + fusepy.
# ---------------------------------------------------------------------------


class _RecordingFS:
    """Stand-in for any of the five variant classes.

    Records its constructor args + tracks whether `cleanup()` was
    called, so tests can assert the right variant was instantiated and
    that the try/finally cleanup hook ran on KeyboardInterrupt.
    """

    instances: List["_RecordingFS"] = []

    def __init__(self, kind: str, *args: Any, **kwargs: Any) -> None:
        self.kind = kind
        self.args = args
        self.kwargs = kwargs
        self.cleaned_up = False
        _RecordingFS.instances.append(self)

    def cleanup(self) -> None:
        self.cleaned_up = True


class _RecordingBlobCache:
    instances: List["_RecordingBlobCache"] = []

    def __init__(self, cache_dir: Path, max_bytes: int = 500 * 1024 * 1024) -> None:
        self.cache_dir = Path(cache_dir)
        self.max_bytes = max_bytes
        _RecordingBlobCache.instances.append(self)


def _build_engine_stub() -> types.ModuleType:
    """Build a fake `claude_mirror._mount` module exposing the API the
    CLI imports."""
    mod = types.ModuleType("claude_mirror._mount")
    mod.BlobCache = _RecordingBlobCache  # type: ignore[attr-defined]
    mod.SnapshotFS = lambda *a, **kw: _RecordingFS("SnapshotFS", *a, **kw)  # type: ignore[attr-defined]
    mod.LiveFS = lambda *a, **kw: _RecordingFS("LiveFS", *a, **kw)  # type: ignore[attr-defined]
    mod.PerMirrorFS = lambda *a, **kw: _RecordingFS("PerMirrorFS", *a, **kw)  # type: ignore[attr-defined]
    mod.AllSnapshotsFS = lambda *a, **kw: _RecordingFS("AllSnapshotsFS", *a, **kw)  # type: ignore[attr-defined]
    mod.AsOfDateFS = lambda *a, **kw: _RecordingFS("AsOfDateFS", *a, **kw)  # type: ignore[attr-defined]
    return mod


class _FakeFuseModule:
    """Stand-in for the `fuse` (fusepy) module.

    Records every FUSE() call. The first call returns normally; tests
    that want to simulate Ctrl+C set `raise_keyboard_interrupt = True`
    BEFORE invoking the command.
    """

    def __init__(self) -> None:
        self.calls: List[Tuple[Any, str, dict[str, Any]]] = []
        self.raise_keyboard_interrupt = False

    def FUSE(self, fs: Any, mountpoint: str, **kwargs: Any) -> None:
        self.calls.append((fs, mountpoint, dict(kwargs)))
        if self.raise_keyboard_interrupt:
            raise KeyboardInterrupt()


@pytest.fixture
def fake_fuse() -> _FakeFuseModule:
    return _FakeFuseModule()


@pytest.fixture(autouse=True)
def _reset_recorders() -> None:
    """Wipe shared recorders between tests so leftovers from one test
    don't leak into the next assertion."""
    _RecordingFS.instances.clear()
    _RecordingBlobCache.instances.clear()


@pytest.fixture
def patch_engine(monkeypatch: pytest.MonkeyPatch, fake_fuse: _FakeFuseModule) -> _FakeFuseModule:
    """Install fake fuse + fake engine module + bypass Config / storage
    creation so the mount command runs end-to-end against in-memory
    stand-ins."""
    monkeypatch.setattr(cli_mod, "_import_fuse", lambda: fake_fuse)

    engine_stub = _build_engine_stub()
    monkeypatch.setattr(cli_mod, "_import_mount_engine", lambda: engine_stub)

    fake_config = MagicMock(name="FakeConfig")
    fake_config.project_path = "/fake/project"
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: p or "fake-config")
    monkeypatch.setattr(cli_mod.Config, "load", staticmethod(lambda _p: fake_config))

    fake_primary = MagicMock(name="FakePrimary")
    monkeypatch.setattr(
        cli_mod, "_create_storage_set", lambda cfg: (fake_primary, []),
    )

    fake_snap = MagicMock(name="FakeSnapshotManager")
    fake_snap.resolve_tag_to_timestamp = MagicMock(
        return_value="2026-04-15T10-30-00Z",
    )
    monkeypatch.setattr(
        cli_mod, "SnapshotManager",
        lambda *a, **kw: fake_snap,
    )

    return fake_fuse


@pytest.fixture
def mountpoint(tmp_path: Path) -> str:
    """A real, existing directory the CLI will accept as a mount target."""
    p = tmp_path / "mnt"
    p.mkdir()
    return str(p)


# ---------------------------------------------------------------------------
# 1. Optional-dep guard
# ---------------------------------------------------------------------------


def test_mount_optional_dep_guard_prints_install_hint(
    monkeypatch: pytest.MonkeyPatch, mountpoint: str,
) -> None:
    """When fusepy is missing, `mount` exits non-zero and the output
    walks the user through the install command + the per-platform
    kernel-layer hint."""
    def _raise() -> Any:
        raise click.ClickException(cli_mod._MOUNT_INSTALL_HINT)
    monkeypatch.setattr(cli_mod, "_import_fuse", _raise)
    result = CliRunner().invoke(
        cli, ["mount", "--live", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "claude-mirror[mount]" in result.output
    assert "macfuse" in result.output
    assert "WinFsp" in result.output


# ---------------------------------------------------------------------------
# 2. Mutually-exclusive variant flags
# ---------------------------------------------------------------------------


def test_mount_no_variant_flag_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """Zero variant flags → clean error naming all five."""
    result = CliRunner().invoke(cli, ["mount", mountpoint])
    assert result.exit_code != 0, result.output
    assert "--tag" in result.output
    assert "--snapshot" in result.output
    assert "--live" in result.output
    assert "--as-of" in result.output
    assert "--all-snapshots" in result.output


def test_mount_two_variant_flags_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """--live and --all-snapshots together → clean error."""
    result = CliRunner().invoke(
        cli, ["mount", "--live", "--all-snapshots", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--live" in result.output
    assert "--all-snapshots" in result.output


def test_mount_exactly_one_variant_flag_succeeds(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """--all-snapshots alone (no other variant flags) → exit 0 and a
    FUSE() call recorded."""
    result = CliRunner().invoke(
        cli, ["mount", "--all-snapshots", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert len(patch_engine.calls) == 1
    assert _RecordingFS.instances[0].kind == "AllSnapshotsFS"


# ---------------------------------------------------------------------------
# 3. --backend / --ttl scope rules
# ---------------------------------------------------------------------------


def test_mount_backend_without_live_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """`--backend NAME` without `--live` is meaningless and rejected
    cleanly."""
    result = CliRunner().invoke(
        cli, ["mount", "--all-snapshots", "--backend", "dropbox", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--backend" in result.output
    assert "--live" in result.output


def test_mount_ttl_without_live_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """`--ttl SECONDS` without `--live` is meaningless and rejected
    cleanly. Click's ParameterSource machinery distinguishes an
    explicit pass from a defaulted value, so this fires regardless of
    the chosen TTL value."""
    result = CliRunner().invoke(
        cli,
        ["mount", "--all-snapshots", "--ttl", "60", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--ttl" in result.output
    assert "--live" in result.output


def test_mount_ttl_default_value_passed_explicitly_without_live_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """Even passing `--ttl 30` (the default value) explicitly without
    `--live` is rejected — the rule is "the flag is scoped to --live",
    not "this specific value is suspicious". Guards against a future
    refactor that swaps `ParameterSource` for a `value != default`
    check, which would silently let `--ttl 30 --all-snapshots` through.
    """
    result = CliRunner().invoke(
        cli,
        ["mount", "--all-snapshots", "--ttl", "30", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--ttl" in result.output


# ---------------------------------------------------------------------------
# 4. --cache-mb validation
# ---------------------------------------------------------------------------


def test_mount_cache_mb_zero_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    result = CliRunner().invoke(
        cli, ["mount", "--live", "--cache-mb", "0", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--cache-mb" in result.output


def test_mount_cache_mb_negative_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    result = CliRunner().invoke(
        cli, ["mount", "--live", "--cache-mb", "-5", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--cache-mb" in result.output


# ---------------------------------------------------------------------------
# 5. Dispatch into the right variant
# ---------------------------------------------------------------------------


def test_mount_tag_dispatches_into_snapshot_fs(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """--tag NAME resolves the tag to a timestamp via SnapshotManager
    then constructs a SnapshotFS with that timestamp."""
    result = CliRunner().invoke(
        cli, ["mount", "--tag", "pre-refactor", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert len(_RecordingFS.instances) == 1
    fs = _RecordingFS.instances[0]
    assert fs.kind == "SnapshotFS"
    # SnapshotFS(snapshot_manager, snapshot_timestamp, blob_cache, backend)
    assert fs.args[1] == "2026-04-15T10-30-00Z"


def test_mount_snapshot_timestamp_dispatches_into_snapshot_fs(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """--snapshot TIMESTAMP skips the tag resolver and goes straight
    into SnapshotFS with the literal timestamp."""
    result = CliRunner().invoke(
        cli,
        ["mount", "--snapshot", "2026-05-01T08-00-00Z", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert len(_RecordingFS.instances) == 1
    fs = _RecordingFS.instances[0]
    assert fs.kind == "SnapshotFS"
    assert fs.args[1] == "2026-05-01T08-00-00Z"


def test_mount_live_dispatches_into_live_fs_with_default_ttl(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    result = CliRunner().invoke(
        cli, ["mount", "--live", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert len(_RecordingFS.instances) == 1
    fs = _RecordingFS.instances[0]
    assert fs.kind == "LiveFS"
    assert fs.kwargs.get("ttl_seconds") == 30


def test_mount_live_with_backend_dispatches_into_per_mirror_fs(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """`--live --backend dropbox` selects the PerMirrorFS variant and
    threads the mirror name through."""
    result = CliRunner().invoke(
        cli,
        ["mount", "--live", "--backend", "dropbox", "--ttl", "60", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert len(_RecordingFS.instances) == 1
    fs = _RecordingFS.instances[0]
    assert fs.kind == "PerMirrorFS"
    assert "dropbox" in fs.args
    assert fs.kwargs.get("ttl_seconds") == 60


def test_mount_all_snapshots_dispatches_into_all_snapshots_fs(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    result = CliRunner().invoke(
        cli, ["mount", "--all-snapshots", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert _RecordingFS.instances[0].kind == "AllSnapshotsFS"


def test_mount_as_of_dispatches_into_as_of_date_fs(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """`--as-of 2026-04-15` parses to a datetime and constructs
    AsOfDateFS with it."""
    result = CliRunner().invoke(
        cli, ["mount", "--as-of", "2026-04-15", mountpoint],
    )
    assert result.exit_code == 0, result.output
    fs = _RecordingFS.instances[0]
    assert fs.kind == "AsOfDateFS"
    parsed = fs.args[1]
    assert isinstance(parsed, datetime)
    assert (parsed.year, parsed.month, parsed.day) == (2026, 4, 15)


def test_mount_as_of_garbage_rejected(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """A non-ISO --as-of value exits non-zero with a clear error
    message naming the flag."""
    result = CliRunner().invoke(
        cli, ["mount", "--as-of", "yesterday-ish", mountpoint],
    )
    assert result.exit_code != 0, result.output
    assert "--as-of" in result.output


# ---------------------------------------------------------------------------
# 6. Ctrl+C cleanup
# ---------------------------------------------------------------------------


def test_mount_keyboard_interrupt_runs_cleanup(
    patch_engine: _FakeFuseModule, mountpoint: str,
) -> None:
    """A KeyboardInterrupt out of fuse.FUSE() must hit the FS
    instance's cleanup() in the finally block."""
    patch_engine.raise_keyboard_interrupt = True
    result = CliRunner().invoke(
        cli, ["mount", "--all-snapshots", mountpoint],
    )
    assert result.exit_code == 0, result.output
    assert _RecordingFS.instances[0].cleaned_up is True


# ---------------------------------------------------------------------------
# 7. umount
# ---------------------------------------------------------------------------


def test_umount_macos_invokes_umount(
    monkeypatch: pytest.MonkeyPatch, mountpoint: str,
) -> None:
    """On darwin, umount shells out to `/usr/sbin/umount MOUNTPOINT`."""
    monkeypatch.setattr(sys, "platform", "darwin")
    captured: dict[str, Any] = {}

    class _FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeResult:
        captured["cmd"] = cmd
        return _FakeResult()

    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = CliRunner().invoke(cli, ["umount", mountpoint])
    assert result.exit_code == 0, result.output
    assert captured["cmd"] == ["umount", mountpoint]
    assert "Unmounted" in result.output


def test_umount_linux_invokes_fusermount(
    monkeypatch: pytest.MonkeyPatch, mountpoint: str,
) -> None:
    """On linux, umount shells out to `fusermount -u MOUNTPOINT`."""
    monkeypatch.setattr(sys, "platform", "linux")
    captured: dict[str, Any] = {}

    class _FakeResult:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd: List[str], **kwargs: Any) -> _FakeResult:
        captured["cmd"] = cmd
        return _FakeResult()

    import subprocess
    monkeypatch.setattr(subprocess, "run", _fake_run)

    result = CliRunner().invoke(cli, ["umount", mountpoint])
    assert result.exit_code == 0, result.output
    assert captured["cmd"] == ["fusermount", "-u", mountpoint]


def test_umount_windows_prints_hint(
    monkeypatch: pytest.MonkeyPatch, mountpoint: str,
) -> None:
    """On win32, umount prints a hint pointing at the foreground
    mount process and exits 0 (best-effort — no PID tracking yet)."""
    monkeypatch.setattr(sys, "platform", "win32")
    result = CliRunner().invoke(cli, ["umount", mountpoint])
    assert result.exit_code == 0, result.output
    assert "Ctrl+C" in result.output
    assert "claude-mirror mount" in result.output


def test_umount_failure_surfaces_stderr(
    monkeypatch: pytest.MonkeyPatch, mountpoint: str,
) -> None:
    """When the underlying tool returns non-zero, the CLI re-emits the
    captured stderr and exits non-zero."""
    monkeypatch.setattr(sys, "platform", "linux")

    class _FakeResult:
        returncode = 1
        stderr = "fusermount: entry not found in /etc/mtab\n"
        stdout = ""

    import subprocess
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kwargs: _FakeResult(),
    )

    result = CliRunner().invoke(cli, ["umount", mountpoint])
    assert result.exit_code != 0, result.output
    assert "fusermount" in result.output
    assert "entry not found" in result.output
