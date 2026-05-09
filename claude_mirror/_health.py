"""Machine-readable health probe for monitoring tools.

The `claude-mirror health` CLI is the fast, machine-readable sibling of
`claude-mirror doctor`: doctor is for humans diagnosing a problem with
verbose output and concrete fix hints, health is for monitoring tools
(Uptime Kuma, Better Stack, Prometheus, Datadog, GitHub Actions matrix
checks) polling every minute or so and keying off exit codes plus a
small JSON envelope.

Both share the same data sources (config YAML, token files, backend
connectivity probe, sync log timestamps) but the surfaces are tuned for
different audiences.

This module is a pure aggregator: side-effects (network calls, file
reads, subprocess invocations) live behind small per-check helpers and
the orchestrator (`collect_health`) calls them in sequence within a
per-check timeout cap. Rendering / exit-code translation lives in the
CLI layer (`cli.py::health`).
"""
from __future__ import annotations

import json as _json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from .config import Config
from .events import LOGS_FOLDER, SYNC_LOG_NAME, SyncLog


# Status rungs. `unsupported` is for checks that don't apply on this
# platform (e.g. watcher liveness on Windows) — they appear in the
# report for transparency but never affect the overall status.
HealthStatus = Literal["ok", "warn", "fail", "unsupported"]
OverallStatus = Literal["ok", "warn", "fail"]


# Threshold constants for the last-sync-age check. Hardcoded for the
# v1 schema — every monitoring integration agrees on the same numbers
# so dashboards across machines stay comparable. If a future user
# wants per-config overrides we'd add them as Config fields, but the
# current contract is "claude-mirror's opinion of healthy".
LAST_SYNC_WARN_HOURS = 24
LAST_SYNC_FAIL_HOURS = 72


SCHEMA_VERSION = "v1"


@dataclass
class HealthCheck:
    name: str
    status: HealthStatus
    detail: str
    latency_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
        }


@dataclass
class HealthReport:
    overall: OverallStatus
    checks: list[HealthCheck]
    generated_at: datetime
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema_version,
            "command": "health",
            "generated_at": self.generated_at.astimezone(timezone.utc).isoformat(),
            "overall": self.overall,
            "checks": [c.to_dict() for c in self.checks],
        }


def _aggregate_overall(checks: list[HealthCheck]) -> OverallStatus:
    """Worst-rung-wins, ignoring `unsupported`. Empty list is `ok`."""
    rungs = {c.status for c in checks if c.status != "unsupported"}
    if "fail" in rungs:
        return "fail"
    if "warn" in rungs:
        return "warn"
    return "ok"


# ─── Per-check helpers ────────────────────────────────────────────────


def _check_config_yaml(cfg_path: str) -> tuple[HealthCheck, Optional[Config]]:
    """Load the primary config YAML. Returns (check, config-or-None).

    A failure here short-circuits later checks that need a Config object,
    so the orchestrator passes the loaded Config back through to the next
    step rather than re-loading.
    """
    t0 = time.monotonic()
    try:
        cfg = Config.load(cfg_path)
    except FileNotFoundError as e:
        return (
            HealthCheck(
                name="config_yaml",
                status="fail",
                detail=f"config file not found: {cfg_path}",
                latency_ms=_elapsed_ms(t0),
            ),
            None,
        )
    except Exception as e:  # noqa: BLE001 - any parse error is a fail rung
        return (
            HealthCheck(
                name="config_yaml",
                status="fail",
                detail=f"config does not parse: {type(e).__name__}: {e}",
                latency_ms=_elapsed_ms(t0),
            ),
            None,
        )
    return (
        HealthCheck(
            name="config_yaml",
            status="ok",
            detail=cfg_path,
            latency_ms=_elapsed_ms(t0),
        ),
        cfg,
    )


def _check_token_present(cfg: Config) -> HealthCheck:
    """Token file exists and parses. WebDAV / SFTP store credentials in
    the YAML so the equivalent test there is "required inline fields are
    set". No actual auth call — that's `backend_reachable`'s job.
    """
    t0 = time.monotonic()
    backend = (cfg.backend or "").lower()
    if backend == "webdav":
        if cfg.webdav_username and cfg.webdav_password:
            return HealthCheck(
                name="token_present",
                status="ok",
                detail="WebDAV username + password present in config",
                latency_ms=_elapsed_ms(t0),
            )
        return HealthCheck(
            name="token_present",
            status="fail",
            detail="WebDAV username or password missing in config",
            latency_ms=_elapsed_ms(t0),
        )
    if backend == "sftp":
        host = getattr(cfg, "sftp_host", "") or ""
        user = getattr(cfg, "sftp_username", "") or ""
        folder = getattr(cfg, "sftp_folder", "") or ""
        key = getattr(cfg, "sftp_key_file", "") or ""
        pw = getattr(cfg, "sftp_password", "") or ""
        missing = []
        if not host:
            missing.append("sftp_host")
        if not user:
            missing.append("sftp_username")
        if not folder:
            missing.append("sftp_folder")
        if not key and not pw:
            missing.append("sftp_key_file or sftp_password")
        if missing:
            return HealthCheck(
                name="token_present",
                status="fail",
                detail=f"SFTP fields missing: {', '.join(missing)}",
                latency_ms=_elapsed_ms(t0),
            )
        return HealthCheck(
            name="token_present",
            status="ok",
            detail="SFTP credentials present in config",
            latency_ms=_elapsed_ms(t0),
        )
    token_path = Path(cfg.token_file)
    if not token_path.exists():
        return HealthCheck(
            name="token_present",
            status="fail",
            detail=f"token file missing: {token_path}",
            latency_ms=_elapsed_ms(t0),
        )
    try:
        _json.loads(token_path.read_text())
    except (OSError, _json.JSONDecodeError) as e:
        return HealthCheck(
            name="token_present",
            status="fail",
            detail=f"token file unreadable / corrupt: {type(e).__name__}",
            latency_ms=_elapsed_ms(t0),
        )
    return HealthCheck(
        name="token_present",
        status="ok",
        detail=str(token_path),
        latency_ms=_elapsed_ms(t0),
    )


def _probe_backend(
    cfg: Config,
    *,
    storage_factory: Callable[[Config], Any],
    timeout_seconds: int,
) -> tuple[HealthStatus, str, int]:
    """One light read against the configured backend. Returns
    (status, detail, latency_ms).

    Used by both `backend_reachable` (primary) and `mirrors_reachable`
    (each Tier 2 mirror). The shim here is intentionally NOT a re-use of
    doctor's connectivity check — doctor's helper is tightly coupled to
    Rich rendering and granular fix-hint emission. We pay a small bit of
    duplication so this module stays free of Rich imports and easy to
    test in isolation.
    """
    t0 = time.monotonic()
    backend = (cfg.backend or "").lower()
    try:
        storage = storage_factory(cfg)
        if backend == "sftp":
            sftp_client = storage.get_credentials()
            folder = getattr(cfg, "sftp_folder", "") or "/"
            sftp_client.stat(folder)
        else:
            storage.get_credentials()
            storage.list_folders(cfg.root_folder, name=None)
    except BaseException as exc:  # noqa: BLE001 - probe must classify, not raise
        return (
            "fail",
            f"{type(exc).__name__}: {exc}"[:200],
            _elapsed_ms(t0),
        )
    return ("ok", f"reachable ({backend})", _elapsed_ms(t0))


def _check_watcher_running() -> HealthCheck:
    """POSIX-only `pgrep -f "claude-mirror watch-all"`. On Windows the
    watch-all daemon doesn't exist (no SIGHUP, no pgrep) so the check
    returns `unsupported` rather than fail — monitoring tools will see
    a stable, non-affecting row instead of a perpetual red.
    """
    t0 = time.monotonic()
    if not hasattr(signal, "SIGHUP"):
        return HealthCheck(
            name="watcher_running",
            status="unsupported",
            detail="watch-all is POSIX-only",
            latency_ms=_elapsed_ms(t0),
        )
    try:
        result = subprocess.run(
            ["pgrep", "-f", "claude-mirror watch-all"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, OSError):
        return HealthCheck(
            name="watcher_running",
            status="unsupported",
            detail="pgrep unavailable on this system",
            latency_ms=_elapsed_ms(t0),
        )
    except subprocess.TimeoutExpired:
        return HealthCheck(
            name="watcher_running",
            status="warn",
            detail="pgrep timed out after 2s",
            latency_ms=_elapsed_ms(t0),
        )
    pids = [
        p for p in result.stdout.strip().splitlines()
        if p.strip() and p.strip() != str(os.getpid())
    ]
    if not pids:
        return HealthCheck(
            name="watcher_running",
            status="warn",
            detail="watch-all process not running",
            latency_ms=_elapsed_ms(t0),
        )
    return HealthCheck(
        name="watcher_running",
        status="ok",
        detail=f"watch-all running (pid {pids[0]})",
        latency_ms=_elapsed_ms(t0),
    )


def _fetch_sync_log(cfg: Config, storage_factory: Callable[[Config], Any]) -> Optional[SyncLog]:
    """Fetch the per-backend `_sync_log.json`. Returns None when no log
    file exists yet (first-run state) or when any error blocks the
    fetch. The caller decides what status to assign in each case.
    """
    try:
        storage = storage_factory(cfg)
        logs_folder_id = storage.get_file_id(LOGS_FOLDER, cfg.root_folder)
        if not logs_folder_id:
            return None
        log_file_id = storage.get_file_id(SYNC_LOG_NAME, logs_folder_id)
        if not log_file_id:
            return None
        raw = storage.download_file(log_file_id)
        return SyncLog.from_bytes(raw)
    except BaseException:  # noqa: BLE001 - fetch failure is a soft "no history" signal
        return None


def _check_last_sync_age(
    cfg: Config,
    *,
    storage_factory: Callable[[Config], Any],
    now: Optional[datetime] = None,
) -> HealthCheck:
    """Read the most-recent timestamp out of `_sync_log.json` and grade:
        < 24h    → ok
        24h-72h  → warn
        > 72h    → fail
    No log file (first-run, never pushed) is `ok` with detail
    "no sync history yet" — fresh installs aren't unhealthy, they're
    new.
    """
    t0 = time.monotonic()
    sync_log = _fetch_sync_log(cfg, storage_factory)
    if sync_log is None or not sync_log.events:
        return HealthCheck(
            name="last_sync_age",
            status="ok",
            detail="no sync history yet",
            latency_ms=_elapsed_ms(t0),
        )
    latest = sync_log.events[-1]
    try:
        ts = datetime.fromisoformat(latest.timestamp.replace("Z", "+00:00"))
    except ValueError:
        return HealthCheck(
            name="last_sync_age",
            status="warn",
            detail=f"could not parse latest log timestamp: {latest.timestamp!r}",
            latency_ms=_elapsed_ms(t0),
        )
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = current - ts.astimezone(timezone.utc)
    age_hours = age.total_seconds() / 3600.0
    if age_hours > LAST_SYNC_FAIL_HOURS:
        status: HealthStatus = "fail"
    elif age_hours > LAST_SYNC_WARN_HOURS:
        status = "warn"
    else:
        status = "ok"
    return HealthCheck(
        name="last_sync_age",
        status=status,
        detail=f"last sync {age_hours:.1f}h ago ({latest.timestamp})",
        latency_ms=_elapsed_ms(t0),
    )


def _elapsed_ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)


# ─── Orchestrator ─────────────────────────────────────────────────────


def collect_health(
    config_path: str,
    *,
    include_backends: bool = True,
    timeout_seconds: int = 10,
    storage_factory: Optional[Callable[[Config], Any]] = None,
    config_loader: Optional[Callable[[str], Config]] = None,
    now: Optional[datetime] = None,
) -> HealthReport:
    """Run every health check in sequence and return the aggregated report.

    Args:
        config_path: Path to the primary config YAML.
        include_backends: Skip the `backend_reachable` and
            `mirrors_reachable` checks when False — useful for fast
            local-only probes that don't burn API quota.
        timeout_seconds: Per-check timeout cap. Currently advisory
            (passed to per-check helpers); the watcher pgrep check has
            its own hardcoded 2s ceiling.
        storage_factory: Injected `Config -> StorageBackend` factory.
            Tests pass a fake; the CLI passes `_create_storage`.
        config_loader: Injected `path -> Config` loader. Tests pass a
            stub when they want to bypass YAML reads; the CLI passes
            `Config.load`.
        now: Override the current time, for deterministic age tests.

    Returns:
        A populated HealthReport. The orchestrator never raises — every
        failure path lands as a `fail` HealthCheck so callers always
        get back a parseable envelope.
    """
    if storage_factory is None:  # default-arg pattern keeps the function pure-ish
        from .cli import _create_storage as default_factory
        storage_factory = default_factory
    if config_loader is None:
        config_loader = Config.load

    checks: list[HealthCheck] = []

    # config_yaml
    t0 = time.monotonic()
    try:
        cfg = config_loader(config_path)
        checks.append(HealthCheck(
            name="config_yaml",
            status="ok",
            detail=config_path,
            latency_ms=_elapsed_ms(t0),
        ))
    except FileNotFoundError:
        checks.append(HealthCheck(
            name="config_yaml",
            status="fail",
            detail=f"config file not found: {config_path}",
            latency_ms=_elapsed_ms(t0),
        ))
        return HealthReport(
            overall=_aggregate_overall(checks),
            checks=checks,
            generated_at=now or datetime.now(timezone.utc),
        )
    except Exception as e:  # noqa: BLE001 - any parse error short-circuits
        checks.append(HealthCheck(
            name="config_yaml",
            status="fail",
            detail=f"config does not parse: {type(e).__name__}: {e}",
            latency_ms=_elapsed_ms(t0),
        ))
        return HealthReport(
            overall=_aggregate_overall(checks),
            checks=checks,
            generated_at=now or datetime.now(timezone.utc),
        )

    # token_present
    checks.append(_check_token_present(cfg))

    # backend_reachable + mirrors_reachable (skipped under --no-backends)
    if include_backends:
        status, detail, latency = _probe_backend(
            cfg,
            storage_factory=storage_factory,
            timeout_seconds=timeout_seconds,
        )
        checks.append(HealthCheck(
            name="backend_reachable",
            status=status,
            detail=detail,
            latency_ms=latency,
        ))
        for mirror_path in cfg.mirror_config_paths:
            t1 = time.monotonic()
            try:
                mirror_cfg = config_loader(mirror_path)
            except Exception as e:  # noqa: BLE001 - failed mirror config is a fail rung
                checks.append(HealthCheck(
                    name=f"mirror_{Path(mirror_path).stem}",
                    status="fail",
                    detail=f"mirror config does not load: {type(e).__name__}: {e}",
                    latency_ms=_elapsed_ms(t1),
                ))
                continue
            mstatus, mdetail, mlatency = _probe_backend(
                mirror_cfg,
                storage_factory=storage_factory,
                timeout_seconds=timeout_seconds,
            )
            checks.append(HealthCheck(
                name=f"mirror_{mirror_cfg.backend or Path(mirror_path).stem}",
                status=mstatus,
                detail=mdetail,
                latency_ms=mlatency,
            ))

    # watcher_running (POSIX-only; unsupported on Windows)
    checks.append(_check_watcher_running())

    # last_sync_age (skip the network fetch when --no-backends so a
    # local-only probe stays local-only — the threshold check is
    # pointless without a fresh log fetch anyway)
    if include_backends:
        checks.append(_check_last_sync_age(
            cfg,
            storage_factory=storage_factory,
            now=now,
        ))

    return HealthReport(
        overall=_aggregate_overall(checks),
        checks=checks,
        generated_at=now or datetime.now(timezone.utc),
    )
