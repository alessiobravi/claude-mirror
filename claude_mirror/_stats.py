"""Aggregate the project's `_sync_log.json` into a usage summary.

Pure-function counterpart to the CLI `stats` command. Given a flat list
of raw log dicts (the same shape `_sync_log.json` persists), produce one
row per group key with the totals the CLI renders or returns over JSON.

The function is pure — no I/O, no clocks, no console — so the surrounding
CLI layer can fetch the log and compose the rendering while tests drive
the aggregation directly with hand-built dicts.

The current `SyncEvent` schema does NOT carry per-event byte counts,
backend identity, or a latency measurement. The aggregator therefore
exposes only the fields that ARE derivable from the log: `events`,
`files`, and `conflicts` (where conflicts == len(auto_resolved_files)).
The CLI builds the BACKEND-axis row by attaching the configured primary
backend label out-of-band; this module only sees what the log records.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional


_VALID_GROUP_BY: tuple[str, ...] = ("backend", "user", "machine", "action", "day")


@dataclass
class StatsRow:
    """One aggregated row: ``key`` plus the three derivable counters."""

    key: str
    events: int
    files: int
    conflicts: int


@dataclass
class StatsTotals:
    """Sum of every row's counters, for the bottom-line summary."""

    events: int
    files: int
    conflicts: int


@dataclass
class StatsResult:
    """Output of `aggregate_log` — rows + totals + the resolved window."""

    since: Optional[datetime]
    until: Optional[datetime]
    group_by: str
    rows: list[StatsRow]
    totals: StatsTotals


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Mirrors ``_presence._parse_timestamp`` — kept duplicated rather than
    cross-imported so this module stays a self-contained pure-function
    unit (no reverse dep into a CLI-adjacent helper).
    """
    if not isinstance(value, str) or not value:
        return None
    raw = value.replace("Z", "+00:00") if value.endswith("Z") else value
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _row_key_for_axis(
    entry: dict[str, Any],
    axis: str,
    ts: datetime,
    backend_label: str,
) -> Optional[str]:
    """Resolve the group-by key for ``entry`` on ``axis``.

    Returns None when the entry can't contribute to the axis (e.g.
    missing user / action). The DAY axis uses the UTC ISO date so a
    single push at 23:59 UTC and another at 00:01 UTC the next day land
    in different buckets — ISO date is the standard split for "daily
    activity pattern" reporting.
    """
    if axis == "user":
        v = entry.get("user")
        return v if isinstance(v, str) and v else None
    if axis == "machine":
        v = entry.get("machine")
        return v if isinstance(v, str) and v else None
    if axis == "action":
        v = entry.get("action")
        return v if isinstance(v, str) and v else None
    if axis == "day":
        return ts.date().isoformat()
    if axis == "backend":
        return backend_label or "unknown"
    return None


def _conflict_count(entry: dict[str, Any]) -> int:
    """How many auto-resolved conflicts this event recorded.

    Older log producers (pre-v0.5.49) didn't ship `auto_resolved_files`
    at all — treat absent / non-list as zero.
    """
    raw = entry.get("auto_resolved_files")
    if not isinstance(raw, list):
        return 0
    return len(raw)


def _files_count(entry: dict[str, Any]) -> int:
    """How many files this event touched. Tolerant of malformed input."""
    raw = entry.get("files")
    if not isinstance(raw, list):
        return 0
    return sum(1 for f in raw if isinstance(f, str))


def aggregate_log(
    log_entries: list[dict[str, Any]],
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    group_by: str = "backend",
    top: Optional[int] = None,
    backend_label: str = "",
) -> StatsResult:
    """Reduce a flat sync-log list into one row per group key.

    `log_entries` is the raw list persisted in `_sync_log.json` —
    `{user, machine, action, timestamp, files, project, ...}` dicts.
    Unknown keys are tolerated. Entries missing or with an unparseable
    `timestamp` are skipped (a malformed line in the audit log must
    never poison the rest of the aggregation).

    Behaviour:
        * Entries outside ``[since, until]`` are excluded. Either or
          both bounds may be None for "no bound on that side".
        * Each row aggregates `events` (one per matching log entry),
          `files` (sum of `len(entry["files"])`), and `conflicts`
          (sum of `len(entry["auto_resolved_files"])`).
        * Rows are sorted descending by `events` for backend / user /
          machine / action axes, and descending by ISO date for the
          day axis (so the freshest day is first).
        * `top` caps the row count after sort.
        * `backend_label` is the static label attached to every entry
          when grouping by backend — the log itself does not record a
          backend per event.

    Raises ValueError for unknown ``group_by`` axes.
    """
    if group_by not in _VALID_GROUP_BY:
        raise ValueError(
            f"unknown group_by axis {group_by!r}; "
            f"choose one of {', '.join(_VALID_GROUP_BY)}"
        )

    by_key: dict[str, list[int]] = {}
    total_events = 0
    total_files = 0
    total_conflicts = 0

    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        ts = _parse_timestamp(entry.get("timestamp"))
        if ts is None:
            continue
        if since is not None and ts < since:
            continue
        if until is not None and ts > until:
            continue
        key = _row_key_for_axis(entry, group_by, ts, backend_label)
        if key is None:
            continue
        files = _files_count(entry)
        conflicts = _conflict_count(entry)
        bucket = by_key.setdefault(key, [0, 0, 0])
        bucket[0] += 1
        bucket[1] += files
        bucket[2] += conflicts
        total_events += 1
        total_files += files
        total_conflicts += conflicts

    rows = [
        StatsRow(key=k, events=v[0], files=v[1], conflicts=v[2])
        for k, v in by_key.items()
    ]
    if group_by == "day":
        rows.sort(key=lambda r: r.key, reverse=True)
    else:
        rows.sort(key=lambda r: (-r.events, r.key))
    if top is not None and top >= 0:
        rows = rows[:top]

    return StatsResult(
        since=since,
        until=until,
        group_by=group_by,
        rows=rows,
        totals=StatsTotals(
            events=total_events,
            files=total_files,
            conflicts=total_conflicts,
        ),
    )
