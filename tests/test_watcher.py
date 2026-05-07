"""Smoke tests for the `watch-all` daemon's process-management surface:

  * `_start_watcher` spawns one daemon thread per config and dedups
    already-watched configs by resolved path.
  * The SIGHUP handler installed by `watch-all` re-scans configs and
    spawns watchers for newly-added projects.
  * `claude-mirror reload` finds the running watch-all PID via pgrep
    and sends it SIGHUP.

These are smoke-level: the real watcher loop, the storage backend, the
pubsub subscription, and the desktop notifier are all mocked out. We
care about glue, not loop bodies.
"""
from __future__ import annotations

import signal
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_mod

# Click 8.3 emits a DeprecationWarning about `protected_args` from inside
# CliRunner.invoke; pyproject's `filterwarnings = ["error"]` would turn
# that into a test failure. Suppress at the module level — tests in this
# file only use CliRunner for smoke coverage of the CLI plumbing.
pytestmark = pytest.mark.filterwarnings(
    "ignore::DeprecationWarning:click",
)


def _patch_watcher_internals(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the heavy collaborators inside `_start_watcher` with mocks
    so `_start_watcher` returns a real Thread without doing any I/O.

    Returns the dict of mocks so the test can assert on them.
    """
    fake_config = MagicMock()
    fake_config.project_path = "/tmp/fake-project"
    fake_config.subscription_id = "sub-123"

    fake_notifier_obj = MagicMock()
    fake_notifier_obj.ensure_subscription = MagicMock()
    # `notifier.watch(callback, stop_event)` is the thread target — make it
    # return immediately so the daemon thread terminates cleanly.
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
    # Every spawned thread is a real Thread (daemon=True so it dies with
    # the test process even if the mocked watch() somehow blocked).
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
    This is what the SIGHUP reload relies on to avoid double-spawning
    threads for the configs that were running before the reload."""
    _patch_watcher_internals(monkeypatch)

    cp = tmp_path / "only.yaml"
    cp.write_text("dummy: true\n")

    stop = threading.Event()
    watched: set[str] = set()
    clients: list = []

    t1 = cli_mod._start_watcher(str(cp), stop, watched, clients)
    t2 = cli_mod._start_watcher(str(cp), stop, watched, clients)

    assert t1 is not None
    assert t2 is None  # dedup'd
    assert len(watched) == 1
    assert len(clients) == 1

    stop.set()
    t1.join(timeout=1)


def test_watch_all_reload_spawns_only_new_configs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mimic what `_handle_reload` does: pass the union of old + new configs
    through `_start_watcher`; only the newly-added one starts a thread."""
    _patch_watcher_internals(monkeypatch)

    initial = [tmp_path / f"old{i}.yaml" for i in range(2)]
    for cp in initial:
        cp.write_text("dummy: true\n")

    stop = threading.Event()
    watched: set[str] = set()
    clients: list = []

    initial_threads = []
    for cp in initial:
        t = cli_mod._start_watcher(str(cp), stop, watched, clients)
        assert t is not None
        initial_threads.append(t)
    assert len(watched) == 2

    # New config appears.
    new_cfg = tmp_path / "new.yaml"
    new_cfg.write_text("dummy: true\n")

    # Reload pass: walk every known config; only the new one yields a thread.
    new_threads = []
    for cp in initial + [new_cfg]:
        t = cli_mod._start_watcher(str(cp), stop, watched, clients)
        if t is not None:
            new_threads.append(t)

    assert len(new_threads) == 1
    assert len(watched) == 3

    stop.set()
    for t in initial_threads + new_threads:
        t.join(timeout=1)


def test_sighup_handler_registered_by_watch_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`watch-all` registers a SIGHUP handler whose job is to re-scan configs
    and start watchers for newly-added projects. We verify the handler is
    installed (not that it dispatches a real signal — that needs a child
    process and would slow the suite).

    Calls the underlying click callback directly to bypass CliRunner +
    Click's argv parsing (which trips a Click 8.3 deprecation warning that
    the project filterwarnings turn into errors)."""
    _patch_watcher_internals(monkeypatch)

    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    (config_dir / "a.yaml").write_text("dummy: true\n")

    monkeypatch.setattr(cli_mod, "CONFIG_DIR", config_dir)

    captured: dict[int, object] = {}

    def fake_signal(sig, handler):
        captured[sig] = handler
        return None

    monkeypatch.setattr(cli_mod.signal, "signal", fake_signal)

    # Periodic update-check thread does sleeps + an HTTP call; stub it.
    monkeypatch.setattr(
        "claude_mirror._update_check.check_for_update",
        lambda **kw: None,
    )

    # `watch-all` blocks on `stop_event.wait()` after registering signal
    # handlers. Patch Event.wait to return immediately so the function
    # exits cleanly. We can't patch `is_set` because Thread._started is
    # also an Event and Thread.start() reads it.
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: True)

    # `watch-all` is the click @cli.command, decorator stores the function
    # on `.callback`. The signature is `(config_paths: tuple)` — pass an
    # empty tuple to trigger the auto-discover branch and pick up our
    # temp CONFIG_DIR.
    try:
        cli_mod.watch_all.callback(config_paths=())
    except SystemExit:
        # `sys.exit(1)` fires if no configs / no watchers — we have a
        # config and a mocked notifier, so this shouldn't trigger, but
        # tolerate it just in case the loop exit path raised.
        pass

    # The SIGHUP handler MUST have been registered before the wait loop.
    assert signal.SIGHUP in captured, (
        f"watch-all did not register a SIGHUP handler. captured={captured!r}"
    )
    assert callable(captured[signal.SIGHUP])
    # And SIGINT/SIGTERM are wired up too — sanity check.
    assert signal.SIGINT in captured
    assert signal.SIGTERM in captured


def test_reload_command_sends_sighup(monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude-mirror reload` calls `pgrep -f "claude-mirror watch-all"` and
    sends SIGHUP to every PID it finds (excluding its own).

    We invoke the underlying callback directly rather than through
    `CliRunner` — Click 8.3 emits an internal DeprecationWarning during
    `invoke()` that the project's `filterwarnings = ["error"]` setting
    converts into a test failure. The callback is plain Python; calling it
    bypasses Click's argv parsing entirely."""
    fake_proc = MagicMock()
    fake_proc.stdout = "12345\n67890\n"

    def fake_run(cmd, **kw):
        assert "pgrep" in cmd[0]
        return fake_proc

    monkeypatch.setattr("subprocess.run", fake_run)

    killed: list[tuple[int, int]] = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr("os.kill", fake_kill)
    # Make the reload command's "skip our own PID" filter effectively
    # a no-op by giving it a PID that doesn't appear in the pgrep output.
    monkeypatch.setattr("os.getpid", lambda: 99999)

    # The Click @cli.command decorator stores the original function on
    # the Command's `.callback` attribute.
    cli_mod.reload.callback()

    assert (12345, signal.SIGHUP) in killed
    assert (67890, signal.SIGHUP) in killed


def test_reload_command_when_no_watcher_running(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """If pgrep returns no PIDs, `reload` prints a friendly notice and
    returns without raising."""
    fake_proc = MagicMock()
    fake_proc.stdout = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)

    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("os.getpid", lambda: 99999)

    cli_mod.reload.callback()

    assert killed == []
    out = capsys.readouterr().out.lower()
    assert "no running" in out or "not" in out
