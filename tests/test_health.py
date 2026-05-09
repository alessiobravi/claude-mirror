"""Tests for `claude-mirror health` — machine-readable monitoring probe.

Covers both the pure aggregator (`collect_health`) and the CLI surface
(`claude-mirror health` / `claude-mirror health --json`).

All tests are offline: backends are stubbed via FakeStorageBackend,
the per-backend probe takes no network round-trips, and the watcher
liveness check is monkeypatched to a deterministic value where the
test cares about the outcome. Each test runs in well under 100 ms on
the in-tree macOS dev box.
"""
from __future__ import annotations

import json
import re
import signal
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror import _health
from claude_mirror._health import (
    HealthCheck,
    HealthReport,
    LAST_SYNC_FAIL_HOURS,
    LAST_SYNC_WARN_HOURS,
    _aggregate_overall,
    collect_health,
)
from claude_mirror.cli import cli
from claude_mirror.events import LOGS_FOLDER, SYNC_LOG_NAME, SyncEvent, SyncLog


# Click 8.3+ emits a DeprecationWarning for `Context.protected_args` from
# CliRunner.invoke; pyproject's filterwarnings = "error" otherwise turns
# that into a test failure. Mirrors the suppression used in
# tests/test_doctor.py and tests/test_json_output.py.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# ─── Helpers ───────────────────────────────────────────────────────────────────


def _write_yaml_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    credentials_file: Path,
    drive_folder_id: str = "test-folder-id",
    backend: str = "googledrive",
    extra: dict | None = None,
) -> Path:
    data: dict = {
        "project_path": str(project_path),
        "backend": backend,
        "drive_folder_id": drive_folder_id,
        "credentials_file": str(credentials_file),
        "token_file": str(token_file),
        "machine_name": "test-machine",
        "user": "test-user",
    }
    if extra:
        data.update(extra)
    path.write_text(yaml.safe_dump(data))
    return path


def _write_token(path: Path) -> Path:
    """Write a minimally well-formed token JSON file."""
    path.write_text(json.dumps({
        "token": "fake-access",
        "refresh_token": "fake-refresh",
        "client_id": "fake-client",
    }))
    return path


def _seed_sync_log(monkeypatch, age_hours: float, machine: str = "remote") -> None:
    """Patch `_fetch_sync_log` to return a SyncLog with one event aged
    `age_hours` in the past. The FakeStorageBackend distinguishes
    folders from files (real Drive doesn't), so seeding through the
    backend layer would force every test to mimic Drive's "folders are
    files" idiom. Patching the fetch helper is simpler and keeps the
    tests focused on what the aggregator does with the data."""
    when = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    event = SyncEvent(
        machine=machine,
        user="someone",
        timestamp=when.isoformat(),
        files=["a.md"],
        action="push",
        project="test-project",
    )
    log = SyncLog()
    log.append(event)
    monkeypatch.setattr(_health, "_fetch_sync_log", lambda *_a, **_kw: log)


@pytest.fixture
def healthy_config(tmp_path, project_dir, config_dir, fake_backend, monkeypatch):
    """A fully-healthy setup: config parses, token exists, fake backend
    is reachable, no mirrors. Returns a SimpleNamespace with cfg_path,
    backend, and the loaded Config."""
    token = _write_token(config_dir / "token.json")
    creds = config_dir / "credentials.json"
    creds.write_text("{}")
    cfg_path = _write_yaml_config(
        tmp_path / "primary.yaml",
        project_path=project_dir,
        token_file=token,
        credentials_file=creds,
    )

    def _factory(_cfg):
        return fake_backend

    return SimpleNamespace(
        cfg_path=str(cfg_path),
        config_dir=config_dir,
        token_file=token,
        backend=fake_backend,
        factory=_factory,
    )


# ─── _aggregate_overall: worst-rung-wins, unsupported is ignored ───────────────


def test_aggregate_all_ok():
    checks = [
        HealthCheck("a", "ok", "x"),
        HealthCheck("b", "ok", "y"),
    ]
    assert _aggregate_overall(checks) == "ok"


def test_aggregate_warn_and_ok_yields_warn():
    checks = [
        HealthCheck("a", "ok", "x"),
        HealthCheck("b", "warn", "y"),
    ]
    assert _aggregate_overall(checks) == "warn"


def test_aggregate_fail_and_warn_yields_fail():
    checks = [
        HealthCheck("a", "warn", "x"),
        HealthCheck("b", "fail", "y"),
    ]
    assert _aggregate_overall(checks) == "fail"


def test_aggregate_unsupported_does_not_affect_overall():
    """Windows watcher path: an `unsupported` check must not poison a
    green dashboard. Only ok / warn / fail rungs feed the aggregate."""
    checks = [
        HealthCheck("a", "ok", "x"),
        HealthCheck("b", "unsupported", "windows"),
    ]
    assert _aggregate_overall(checks) == "ok"


def test_aggregate_only_unsupported_yields_ok():
    checks = [HealthCheck("a", "unsupported", "n/a")]
    assert _aggregate_overall(checks) == "ok"


# ─── collect_health: the happy path ────────────────────────────────────────────


def test_collect_health_all_ok(healthy_config, monkeypatch):
    """Every check passes: config parses, token exists, fake backend
    list_folders succeeds, no sync history yet (counts as ok), watcher
    pgrep is stubbed to "running"."""
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    assert report.overall == "ok"
    names = [c.name for c in report.checks]
    assert "config_yaml" in names
    assert "token_present" in names
    assert "backend_reachable" in names
    assert "watcher_running" in names
    assert "last_sync_age" in names
    # Latency populated on the timed checks
    backend_check = next(c for c in report.checks if c.name == "backend_reachable")
    assert backend_check.latency_ms is not None
    assert backend_check.latency_ms >= 0


# ─── token_present check ───────────────────────────────────────────────────────


def test_token_missing_fails_overall(healthy_config, monkeypatch):
    healthy_config.token_file.unlink()
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    token_check = next(c for c in report.checks if c.name == "token_present")
    assert token_check.status == "fail"
    assert "token" in token_check.detail.lower()
    assert report.overall == "fail"


# ─── backend_reachable check ───────────────────────────────────────────────────


def test_backend_unreachable_fails_overall(healthy_config, monkeypatch):
    """Make list_folders raise; backend_reachable must come back as
    `fail` and the overall must be `fail`."""
    def boom(*_a, **_kw):
        raise RuntimeError("simulated network error")
    monkeypatch.setattr(healthy_config.backend, "list_folders", boom)
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    backend_check = next(c for c in report.checks if c.name == "backend_reachable")
    assert backend_check.status == "fail"
    assert "RuntimeError" in backend_check.detail
    assert report.overall == "fail"


# ─── mirrors_reachable check ───────────────────────────────────────────────────


def test_mirror_unreachable_while_primary_fine(
    tmp_path, project_dir, config_dir, fake_backend, monkeypatch,
):
    """A Tier 2 mirror config that fails the probe shows up as one
    `mirror_<backend>` row with `fail` status, while the primary's
    `backend_reachable` stays `ok`. Overall is `fail` because mirrors
    contribute to the aggregate."""
    primary_token = _write_token(config_dir / "primary-token.json")
    mirror_token = _write_token(config_dir / "mirror-token.json")
    creds = config_dir / "credentials.json"
    creds.write_text("{}")

    mirror_path = _write_yaml_config(
        tmp_path / "mirror.yaml",
        project_path=project_dir,
        token_file=mirror_token,
        credentials_file=creds,
        drive_folder_id="mirror-folder-id",
        extra={"machine_name": "test-machine-mirror"},
    )
    primary_path = _write_yaml_config(
        tmp_path / "primary.yaml",
        project_path=project_dir,
        token_file=primary_token,
        credentials_file=creds,
        extra={"mirror_config_paths": [str(mirror_path)]},
    )

    class _MirrorBackend:
        backend_name = "mirror"

        def get_credentials(self):
            return self

        def list_folders(self, _root, name=None):
            raise RuntimeError("mirror is down")

        def get_file_id(self, *_a, **_kw):
            return None

        def download_file(self, *_a, **_kw):
            raise RuntimeError("mirror is down")

    def _factory(cfg):
        if Path(cfg.token_file).name.startswith("mirror-"):
            return _MirrorBackend()
        return fake_backend

    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))

    report = collect_health(
        str(primary_path),
        storage_factory=_factory,
    )
    backend_check = next(c for c in report.checks if c.name == "backend_reachable")
    assert backend_check.status == "ok"
    mirror_rows = [c for c in report.checks if c.name.startswith("mirror_")]
    assert len(mirror_rows) == 1
    assert mirror_rows[0].status == "fail"
    assert "mirror is down" in mirror_rows[0].detail
    assert report.overall == "fail"


# ─── last_sync_age check ───────────────────────────────────────────────────────


def test_last_sync_age_30h_yields_warn(healthy_config, monkeypatch):
    """30h since last successful push is over the 24h warn threshold but
    under the 72h fail threshold."""
    _seed_sync_log(monkeypatch, age_hours=30)
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    age_check = next(c for c in report.checks if c.name == "last_sync_age")
    assert age_check.status == "warn"
    assert "30" in age_check.detail
    assert report.overall == "warn"


def test_last_sync_age_100h_yields_fail(healthy_config, monkeypatch):
    """100h is well past the 72h fail threshold."""
    _seed_sync_log(monkeypatch, age_hours=100)
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    age_check = next(c for c in report.checks if c.name == "last_sync_age")
    assert age_check.status == "fail"
    assert report.overall == "fail"


def test_last_sync_age_no_history_is_ok(healthy_config, monkeypatch):
    """A fresh project that has never been pushed has no sync log on
    remote yet — this is `ok` (a new install isn't unhealthy), with
    detail `no sync history yet`."""
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    age_check = next(c for c in report.checks if c.name == "last_sync_age")
    assert age_check.status == "ok"
    assert "no sync history" in age_check.detail


# ─── --no-backends opt-out ─────────────────────────────────────────────────────


def test_no_backends_skips_reachability_and_age(healthy_config, monkeypatch):
    """`include_backends=False` must omit backend_reachable, mirror_*,
    and last_sync_age — the network-touching checks. config_yaml,
    token_present, watcher_running stay."""
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        include_backends=False,
        storage_factory=healthy_config.factory,
    )
    names = [c.name for c in report.checks]
    assert "backend_reachable" not in names
    assert "last_sync_age" not in names
    assert not any(n.startswith("mirror_") for n in names)
    assert "config_yaml" in names
    assert "token_present" in names
    assert "watcher_running" in names
    assert report.overall == "ok"


# ─── unsupported watcher path (Windows) ────────────────────────────────────────


def test_watcher_unsupported_does_not_affect_overall(
    healthy_config, monkeypatch,
):
    """Simulate the Windows code path: watcher_running comes back as
    `unsupported`. The overall must still be `ok` because unsupported
    rungs never feed the aggregate."""
    monkeypatch.setattr(
        _health, "_check_watcher_running",
        lambda: HealthCheck("watcher_running", "unsupported",
                            "watch-all is POSIX-only"),
    )
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    watcher_check = next(c for c in report.checks if c.name == "watcher_running")
    assert watcher_check.status == "unsupported"
    assert report.overall == "ok"


# ─── _check_watcher_running: direct unit tests ─────────────────────────────────


def test_watcher_running_unsupported_when_no_sighup(monkeypatch):
    """On Windows, signal.SIGHUP is undefined. The function must return
    `unsupported` rather than ever attempting subprocess work."""
    fake_signal = SimpleNamespace()
    monkeypatch.setattr(_health, "signal", fake_signal)
    check = _health._check_watcher_running()
    assert check.status == "unsupported"
    assert "POSIX-only" in check.detail


# ─── Report.to_dict() shape ────────────────────────────────────────────────────


def test_health_report_to_dict_shape(healthy_config, monkeypatch):
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    report = collect_health(
        healthy_config.cfg_path,
        storage_factory=healthy_config.factory,
    )
    doc = report.to_dict()
    assert doc["schema"] == "v1"
    assert doc["command"] == "health"
    assert doc["overall"] == "ok"
    assert isinstance(doc["checks"], list) and doc["checks"]
    # Every check dict has exactly the four documented keys
    for entry in doc["checks"]:
        assert set(entry.keys()) == {"name", "status", "detail", "latency_ms"}
    # generated_at is an ISO-8601 string we can re-parse
    datetime.fromisoformat(doc["generated_at"].replace("Z", "+00:00"))


# ─── CLI: human-readable default ───────────────────────────────────────────────


@pytest.fixture
def patch_cli_storage(monkeypatch, fake_backend):
    """Patch cli._create_storage so the CLI command uses the fake backend
    without hitting any cloud APIs."""
    monkeypatch.setattr(cli_mod, "_create_storage", lambda c: fake_backend)
    return fake_backend


def test_cli_health_default_table_exit_zero(
    healthy_config, patch_cli_storage, monkeypatch,
):
    """`claude-mirror health` with a healthy config exits 0 and prints
    a table containing every check name plus the Overall footer."""
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: p)
    result = CliRunner().invoke(
        cli, ["health", "--config", healthy_config.cfg_path],
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "config_yaml" in out
    assert "token_present" in out
    assert "backend_reachable" in out
    assert "watcher_running" in out
    assert "last_sync_age" in out
    assert "OK" in out


# ─── CLI: --json envelope on healthy config ────────────────────────────────────


def test_cli_health_json_emits_v1_envelope(
    healthy_config, patch_cli_storage, monkeypatch,
):
    """`--json` emits a parseable JSON envelope with the spec'd keys
    (schema, command, generated_at, overall, checks). Stdout must be
    JSON-only — no banner leaks."""
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: p)
    result = CliRunner().invoke(
        cli, ["health", "--json", "--config", healthy_config.cfg_path],
    )
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["schema"] == "v1"
    assert doc["command"] == "health"
    assert doc["overall"] == "ok"
    assert isinstance(doc["checks"], list)
    assert "generated_at" in doc
    # No human-readable banner snuck onto stdout
    assert "claude-mirror health" not in result.stdout
    assert "Health checks" not in result.stdout


def test_cli_health_json_force_fail_exits_two(
    healthy_config, patch_cli_storage, monkeypatch,
):
    """A backend probe that throws makes `overall=fail` → exit 2."""
    def boom(*_a, **_kw):
        raise RuntimeError("forced backend down")
    monkeypatch.setattr(healthy_config.backend, "list_folders", boom)
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: p)
    result = CliRunner().invoke(
        cli, ["health", "--json", "--config", healthy_config.cfg_path],
    )
    assert result.exit_code == 2
    doc = json.loads(result.stdout)
    assert doc["overall"] == "fail"
    statuses = {c["name"]: c["status"] for c in doc["checks"]}
    assert statuses["backend_reachable"] == "fail"


def test_cli_health_json_30h_log_exits_one(
    healthy_config, patch_cli_storage, monkeypatch,
):
    """A 30h-old log makes `overall=warn` → exit 1."""
    _seed_sync_log(monkeypatch, age_hours=30)
    monkeypatch.setattr(_health, "_check_watcher_running",
                        lambda: HealthCheck("watcher_running", "ok", "stubbed"))
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: p)
    result = CliRunner().invoke(
        cli, ["health", "--json", "--config", healthy_config.cfg_path],
    )
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["overall"] == "warn"
    age = next(c for c in doc["checks"] if c["name"] == "last_sync_age")
    assert age["status"] == "warn"


# ─── CLI: --timeout validation ─────────────────────────────────────────────────


def test_cli_health_zero_timeout_rejected(monkeypatch):
    """`--timeout 0` is invalid — exit non-zero with a message naming
    the flag, before any check runs."""
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: "fake-config")
    result = CliRunner().invoke(cli, ["health", "--timeout", "0"])
    assert result.exit_code != 0
    assert "--timeout" in result.output


def test_cli_health_negative_timeout_rejected(monkeypatch):
    """`--timeout -5` is invalid — exit non-zero with a message naming
    the flag."""
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda p: "fake-config")
    result = CliRunner().invoke(cli, ["health", "--timeout", "-5"])
    assert result.exit_code != 0
    assert "--timeout" in result.output
