"""Tests for `log --follow` (TAIL).

Live `tail -f`-style streaming of the remote sync log. The follow path
prints the current tail first, then enters a poll loop that re-pulls the
remote `_sync_log.json` every `--interval` seconds, dedups against
already-seen entries, and prints only the new ones.

Coverage:
    * Happy path — a new entry shows up on poll N+1 and prints; old
      entries are not re-printed.
    * Dedup correctness — entries with identical timestamp but different
      (user, machine, action) tuples are both surfaced.
    * KeyboardInterrupt — Ctrl+C inside the poll loop exits 0 with
      "Stopped following." on its own line.
    * Transient-error resilience — backend raises a transient error on
      one poll, succeeds on the next; output contains the
      `[poll error: ...]` line AND the new entry.
    * `--interval` validation — non-positive values reject; passing
      `--interval` without `--follow` rejects.

All tests run offline against the FakeStorageBackend. They patch the
module-local `_log_follow_sleep` wrapper rather than `time.sleep`
itself; this is the same pattern used by `_status_watch_sleep` in
`test_status_watch.py` and matches the project rule
`feedback_no_global_time_sleep_patch.md`.
"""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror.cli import cli
from claude_mirror.events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _seed_log(fake_backend, cfg, events: list[SyncEvent]) -> None:
    """Place a SyncLog containing `events` at LOGS_FOLDER/SYNC_LOG_NAME
    on `fake_backend` and shim get_file_id so it also resolves folder
    names (the real backends conflate file + folder lookups; the fake
    separates them, so the production code's `get_file_id(LOGS_FOLDER,
    root)` call would otherwise miss the folder).
    """
    log = SyncLog()
    for ev in events:
        log.append(ev)
    logs_folder_id = fake_backend.get_or_create_folder(LOGS_FOLDER, cfg.root_folder)
    if existing := fake_backend.get_file_id(SYNC_LOG_NAME, logs_folder_id):
        fake_backend.upload_bytes(
            log.to_bytes(), SYNC_LOG_NAME, logs_folder_id, file_id=existing,
        )
    else:
        fake_backend.upload_bytes(log.to_bytes(), SYNC_LOG_NAME, logs_folder_id)


@pytest.fixture
def patched_log_env(monkeypatch, make_config, fake_backend):
    """Patch Config.load + _create_storage to return offline doubles, and
    shim get_file_id to also resolve folder names so LOGS_FOLDER works."""
    cfg = make_config()
    monkeypatch.setattr(
        cli_module, "_resolve_config", lambda p: p or "fake-config-path"
    )
    monkeypatch.setattr(cli_module.Config, "load", lambda path: cfg)
    monkeypatch.setattr(cli_module, "_create_storage", lambda c: fake_backend)

    real_get_file_id = fake_backend.get_file_id

    def _get_file_or_folder_id(name, folder_id):
        fid = real_get_file_id(name, folder_id)
        if fid is not None:
            return fid
        return fake_backend.folders.get((folder_id, name))
    monkeypatch.setattr(fake_backend, "get_file_id", _get_file_or_folder_id)
    return cfg


def _ev(timestamp: str, user: str, machine: str = "m1", action: str = "push",
        files: list[str] | None = None, project: str = "demo") -> SyncEvent:
    return SyncEvent(
        machine=machine, user=user, timestamp=timestamp,
        files=files or ["a.md"], action=action, project=project,
    )


def test_log_follow_streams_new_entries(patched_log_env, fake_backend, monkeypatch):
    """A new entry that appears between poll #1 and poll #2 prints once;
    old entries from the initial tail are not re-printed during the
    streaming phase."""
    cfg = patched_log_env
    initial = _ev("2026-05-07T10:00:00Z", "alice")
    _seed_log(fake_backend, cfg, [initial])

    poll_count = {"n": 0}

    def fake_sleep(_interval):
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            new = _ev("2026-05-07T11:00:00Z", "bob")
            _seed_log(fake_backend, cfg, [initial, new])
        elif poll_count["n"] >= 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli_module, "_log_follow_sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["log", "--follow", "--interval", "1"])
    assert result.exit_code == 0, result.output
    assert "alice" in result.output
    assert "bob" in result.output
    assert result.output.count("bob") == 1
    assert "Stopped following." in result.output


def test_log_follow_dedup_identity_tuple(patched_log_env, fake_backend, monkeypatch):
    """Two events sharing a timestamp but differing on (user, machine,
    action) are both surfaced — the dedup key is the full identity
    tuple, not the timestamp alone."""
    cfg = patched_log_env
    seed = _ev("2026-05-07T10:00:00Z", "alice", machine="laptop")
    _seed_log(fake_backend, cfg, [seed])

    poll_count = {"n": 0}

    def fake_sleep(_interval):
        poll_count["n"] += 1
        if poll_count["n"] == 1:
            same_ts_diff_user = _ev(
                "2026-05-07T11:00:00Z", "alice", machine="laptop", action="push",
            )
            same_ts_diff_machine = _ev(
                "2026-05-07T11:00:00Z", "alice", machine="desktop", action="push",
            )
            same_ts_diff_action = _ev(
                "2026-05-07T11:00:00Z", "alice", machine="laptop", action="pull",
            )
            _seed_log(fake_backend, cfg, [
                seed, same_ts_diff_user, same_ts_diff_machine, same_ts_diff_action,
            ])
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli_module, "_log_follow_sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["log", "--follow", "--interval", "1"])
    assert result.exit_code == 0, result.output
    assert "laptop" in result.output
    assert "desktop" in result.output
    assert "pull" in result.output
    assert "Stopped following." in result.output


def test_log_follow_keyboard_interrupt_exits_clean(patched_log_env, fake_backend, monkeypatch):
    """Ctrl+C inside the poll loop exits 0 with a tidy message rather
    than a stack trace."""
    cfg = patched_log_env
    _seed_log(fake_backend, cfg, [_ev("2026-05-07T10:00:00Z", "alice")])

    def fake_sleep(_interval):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli_module, "_log_follow_sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["log", "--follow", "--interval", "1"])
    assert result.exit_code == 0, result.output
    assert "Stopped following." in result.output


def test_log_follow_transient_error_keeps_polling(patched_log_env, fake_backend, monkeypatch):
    """A transient backend error on poll N must not kill the loop —
    the next successful poll still surfaces the new entry. Output
    contains the `[poll error: ...]` retry line AND the new entry."""
    cfg = patched_log_env
    seed = _ev("2026-05-07T10:00:00Z", "alice")
    _seed_log(fake_backend, cfg, [seed])

    poll_count = {"n": 0}

    real_download = fake_backend.download_file

    def flaky_download(file_id, progress_callback=None):
        poll_count["n"] += 1
        if poll_count["n"] == 2:
            raise RuntimeError("network blip")
        return real_download(file_id, progress_callback=progress_callback)
    monkeypatch.setattr(fake_backend, "download_file", flaky_download)

    sleep_count = {"n": 0}

    def fake_sleep(_interval):
        sleep_count["n"] += 1
        if sleep_count["n"] == 1:
            pass
        elif sleep_count["n"] == 2:
            new = _ev("2026-05-07T11:00:00Z", "bob")
            _seed_log(fake_backend, cfg, [seed, new])
        else:
            raise KeyboardInterrupt
    monkeypatch.setattr(cli_module, "_log_follow_sleep", fake_sleep)

    result = CliRunner().invoke(cli, ["log", "--follow", "--interval", "1"])
    assert result.exit_code == 0, result.output
    assert "[poll error:" in result.output
    assert "network blip" in result.output
    assert "bob" in result.output
    assert "Stopped following." in result.output


def test_log_follow_rejects_zero_interval(patched_log_env, fake_backend):
    """`--interval 0` exits non-zero with a message naming the flag."""
    result = CliRunner().invoke(cli, ["log", "--follow", "--interval", "0"])
    assert result.exit_code != 0
    assert "--interval" in result.output


def test_log_follow_rejects_negative_interval(patched_log_env, fake_backend):
    """`--interval -5` exits non-zero with a message naming the flag."""
    result = CliRunner().invoke(cli, ["log", "--follow", "--interval", "-5"])
    assert result.exit_code != 0
    assert "--interval" in result.output


def test_log_interval_without_follow_rejected(patched_log_env, fake_backend):
    """Passing `--interval` without `--follow` exits non-zero with a
    message naming both flags so the user knows what to fix."""
    result = CliRunner().invoke(cli, ["log", "--interval", "10"])
    assert result.exit_code != 0
    assert "--interval" in result.output
    assert "--follow" in result.output
