"""Smoke tests for the `watch-all` daemon's process-management surface:

  * `_start_watcher` spawns one daemon thread per config and dedups
    already-watched configs by resolved path.
  * `_rescan_configs` re-scans configs and spawns watchers for newly
    added projects without disturbing the existing ones.
  * The watch-all polling loop reacts to a fresh sentinel-file mtime
    by invoking the same rescan path.
  * On POSIX `watch-all` ALSO registers a SIGHUP handler as a
    back-compat path for `claude-mirror reload` clients still on the
    pre-WIN-WATCH wire-format.
  * `claude-mirror reload` writes the sentinel file (atomic) and surfaces
    a friendly notice when no daemon is running.

These are smoke-level: the real watcher loop, the storage backend, the
pubsub subscription, and the desktop notifier are all mocked out. We
care about glue, not loop bodies.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = [
    pytest.mark.filterwarnings("ignore::DeprecationWarning:click"),
]

from claude_mirror import cli as cli_mod  # noqa: E402


def _patch_watcher_internals(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the heavy collaborators inside `_start_watcher` with mocks
    so `_start_watcher` returns a real Thread without doing any I/O.

    Returns the dict of mocks so the test can assert on them.
    """
    fake_config = MagicMock()
    fake_config.project_path = "/tmp/fake-project"
    fake_config.subscription_id = "sub-123"
    fake_config.mirror_config_paths = []

    fake_notifier_obj = MagicMock()
    fake_notifier_obj.ensure_subscription = MagicMock()
    fake_notifier_obj.watch = MagicMock(return_value=None)

    fake_storage = MagicMock()

    monkeypatch.setattr(cli_mod.Config, "load", staticmethod(lambda p: fake_config))
    monkeypatch.setattr(cli_mod, "_create_storage", lambda cfg: fake_storage)
    monkeypatch.setattr(cli_mod, "_create_notifier", lambda cfg, st: fake_notifier_obj)
    monkeypatch.setattr(cli_mod, "Notifier", lambda project_path: MagicMock())
    monkeypatch.setattr(cli_mod, "_make_watch_callback", lambda cfg, dn: lambda *a, **kw: None)

    return {
        "config": fake_config,
        "notifier": fake_notifier_obj,
        "storage": fake_storage,
    }


def test_watch_all_spawns_one_thread_per_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given 3 configs, the start-watcher helper spawns 3 distinct threads
    (one per resolved config path)."""
    _patch_watcher_internals(monkeypatch)

    cfgs = [tmp_path / f"c{i}.yaml" for i in range(3)]
    for cp in cfgs:
        cp.write_text("dummy: true\n")

    stop = threading.Event()
    watched: set[str] = set()
    clients: list = []

    threads = []
    for cp in cfgs:
        t = cli_mod._start_watcher(str(cp), stop, watched, clients)
        assert t is not None
        threads.append(t)

    assert len(threads) == 3
    assert len(watched) == 3
    assert len(clients) == 3
    for t in threads:
        assert isinstance(t, threading.Thread)
        assert t.daemon is True

    stop.set()
    for t in threads:
        t.join(timeout=1)


def test_watch_all_dedups_already_watched_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling `_start_watcher` twice for the same resolved path returns
    None on the second call — the already-watched set short-circuits it.
    This is what hot-reload relies on to avoid double-spawning threads
    for the configs that were running before the reload."""
    _patch_watcher_internals(monkeypatch)

    cp = tmp_path / "only.yaml"
    cp.write_text("dummy: true\n")

    stop = threading.Event()
    watched: set[str] = set()
    clients: list = []

    t1 = cli_mod._start_watcher(str(cp), stop, watched, clients)
    t2 = cli_mod._start_watcher(str(cp), stop, watched, clients)

    assert t1 is not None
    assert t2 is None
    assert len(watched) == 1
    assert len(clients) == 1

    stop.set()
    t1.join(timeout=1)


def test_watch_all_reload_spawns_only_new_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive `_rescan_configs` directly: after seeding watchers for two
    configs, drop a third config into CONFIG_DIR and assert the rescan
    helper returns 1 (the count of newly-spawned threads) and leaves the
    pre-existing two threads untouched."""
    _patch_watcher_internals(monkeypatch)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    monkeypatch.setattr(cli_mod, "CONFIG_DIR", config_dir)

    initial = [config_dir / f"old{i}.yaml" for i in range(2)]
    for cp in initial:
        cp.write_text("dummy: true\n")

    stop = threading.Event()
    watched: set[str] = set()
    clients: list = []
    threads: list[threading.Thread] = []

    state = cli_mod._RescanState(
        config_paths=(),
        use_auto_discover=True,
        stop_event=stop,
        watched=watched,
        clients=clients,
        threads=threads,
    )

    added_first = cli_mod._rescan_configs(state)
    assert added_first == 2
    assert len(watched) == 2

    (config_dir / "new.yaml").write_text("dummy: true\n")

    added_second = cli_mod._rescan_configs(state)
    assert added_second == 1
    assert len(watched) == 3
    assert len(threads) == 3

    stop.set()
    for t in threads:
        t.join(timeout=1)


def test_sighup_handler_registered_by_watch_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`watch-all` registers SIGINT/SIGTERM unconditionally, plus SIGHUP
    on POSIX where the symbol is defined. SIGHUP is back-compat for
    pre-WIN-WATCH `claude-mirror reload` clients; the cross-platform
    hot-reload path is the sentinel-file polling check inside the loop.

    Calls the underlying click callback directly to bypass CliRunner +
    Click's argv parsing (which trips a Click 8.3 deprecation warning
    that the project filterwarnings turn into errors)."""
    _patch_watcher_internals(monkeypatch)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "a.yaml").write_text("dummy: true\n")

    monkeypatch.setattr(cli_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        cli_mod, "RELOAD_SIGNAL_FILE", config_dir / ".reload_signal"
    )

    captured: dict[int, object] = {}
    real_signal_signal = cli_mod.signal.signal

    def fake_signal(sig, handler):
        captured[sig] = handler
        return None

    monkeypatch.setattr(cli_mod.signal, "signal", fake_signal)

    monkeypatch.setattr(
        "claude_mirror._update_check.check_for_update",
        lambda **kw: None,
    )

    # `watch-all` blocks on a polling loop guarded by `stop_event.wait`.
    # Patch Event.wait to set the event and return True so the loop
    # exits on the first iteration. We can't patch `is_set` because
    # Thread._started is also an Event and Thread.start() reads it.
    original_wait = threading.Event.wait

    def fast_wait(self, timeout=None):  # type: ignore[no-untyped-def]
        self.set()
        return True

    monkeypatch.setattr(threading.Event, "wait", fast_wait)

    try:
        cli_mod.watch_all.callback(config_paths=())
    except SystemExit:
        pass

    import signal as _signal
    if hasattr(_signal, "SIGHUP"):
        assert _signal.SIGHUP in captured, (
            f"watch-all did not register a SIGHUP handler on POSIX. "
            f"captured={captured!r}"
        )
        assert callable(captured[_signal.SIGHUP])
    else:
        assert not hasattr(_signal, "SIGHUP")

    assert _signal.SIGINT in captured
    assert _signal.SIGTERM in captured

    # Restore real Event.wait so subsequent tests see normal semantics.
    monkeypatch.setattr(threading.Event, "wait", original_wait)


def test_watch_all_polling_loop_reacts_to_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end of the sentinel-file mechanism without involving
    `signal`: write the sentinel after seeding two configs + a third
    one, and assert the polling loop's `_should_reload` + rescan
    sequence picks the third config up.

    Drives the loop body manually rather than spinning the daemon — the
    real loop is `while not stop: if _should_reload(...): _rescan(...)`,
    so exercising those two helpers in sequence is the loop's full
    semantic surface.
    """
    _patch_watcher_internals(monkeypatch)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    monkeypatch.setattr(cli_mod, "CONFIG_DIR", config_dir)
    sentinel = config_dir / ".reload_signal"
    monkeypatch.setattr(cli_mod, "RELOAD_SIGNAL_FILE", sentinel)

    (config_dir / "a.yaml").write_text("dummy: true\n")
    (config_dir / "b.yaml").write_text("dummy: true\n")

    stop = threading.Event()
    watched: set[str] = set()
    clients: list = []
    threads: list[threading.Thread] = []

    state = cli_mod._RescanState(
        config_paths=(),
        use_auto_discover=True,
        stop_event=stop,
        watched=watched,
        clients=clients,
        threads=threads,
    )
    cli_mod._rescan_configs(state)
    assert len(watched) == 2

    # Fresh start: no sentinel file yet → no rescan triggered.
    last_seen = cli_mod._read_reload_mtime(sentinel)
    assert last_seen is None
    assert cli_mod._should_reload(sentinel, last_seen) is False

    (config_dir / "c.yaml").write_text("dummy: true\n")
    cli_mod._write_reload_signal(sentinel)

    assert cli_mod._should_reload(sentinel, last_seen) is True
    last_seen = cli_mod._read_reload_mtime(sentinel)
    added = cli_mod._rescan_configs(state)
    assert added == 1
    assert len(watched) == 3

    # Same mtime → no further reload until the next signal write.
    assert cli_mod._should_reload(sentinel, last_seen) is False

    # Second reload: bump the mtime forward (filesystems can have
    # 1-second mtime resolution on some platforms — set it explicitly
    # rather than racing the clock).
    import os as _os
    _os.utime(sentinel, (last_seen + 5.0, last_seen + 5.0))
    assert cli_mod._should_reload(sentinel, last_seen) is True

    stop.set()
    for t in threads:
        t.join(timeout=1)


def test_reload_command_writes_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`claude-mirror reload` writes the sentinel file with a fresh mtime
    and surfaces a one-line confirmation. We invoke the underlying
    callback directly rather than through `CliRunner` — Click 8.3 emits
    an internal DeprecationWarning during `invoke()` that the project's
    `filterwarnings = ["error"]` setting converts into a test failure.
    The callback is plain Python; calling it bypasses Click's argv
    parsing entirely."""
    sentinel = tmp_path / ".reload_signal"
    monkeypatch.setattr(cli_mod, "RELOAD_SIGNAL_FILE", sentinel)

    monkeypatch.setattr(
        cli_mod, "_detect_watcher_pids", lambda: (["12345"], None)
    )

    assert not sentinel.exists()
    cli_mod.reload.callback()
    assert sentinel.exists()

    payload = sentinel.read_text().strip()
    parsed = float(payload)
    assert abs(parsed - time.time()) < 5.0


def test_reload_command_when_no_watcher_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """If no watcher process is detected, `reload` still writes the
    sentinel (the watcher might not exist yet — when one starts later
    it will baseline against the fresh mtime and ignore the stale mark)
    BUT exits non-zero with a friendly notice so cron / scripts surface
    the misconfiguration rather than silently no-op."""
    sentinel = tmp_path / ".reload_signal"
    monkeypatch.setattr(cli_mod, "RELOAD_SIGNAL_FILE", sentinel)

    monkeypatch.setattr(cli_mod, "_detect_watcher_pids", lambda: ([], None))

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.reload.callback()
    assert exc_info.value.code == 1

    out = capsys.readouterr().out.lower()
    assert "no running" in out or "not running" in out
    # Sentinel still got written even though the warning was raised —
    # the fail-fast happens AFTER the write so a deferred-start watcher
    # picks up the request.
    assert sentinel.exists()


def test_reload_command_when_detection_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """If the running-process check can't run (e.g. pgrep absent on a
    minimal container, tasklist denied on a locked-down Windows host),
    reload still writes the sentinel and exits 0 with an informational
    notice — the check is best-effort, the write is the contract."""
    sentinel = tmp_path / ".reload_signal"
    monkeypatch.setattr(cli_mod, "RELOAD_SIGNAL_FILE", sentinel)

    monkeypatch.setattr(
        cli_mod, "_detect_watcher_pids", lambda: (None, "pgrep unavailable (test)")
    )

    cli_mod.reload.callback()
    assert sentinel.exists()
    out = capsys.readouterr().out.lower()
    assert "couldn't verify" in out or "could not verify" in out
