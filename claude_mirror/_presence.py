"""Aggregate the project's `_sync_log.json` into per-collaborator presence.

The shared sync log records one entry per push / pull / sync / delete made
by any collaborator. The CLI's `status --presence` view answers a single
question: "who else is editing this project right now?". This module
reduces a flat list of log dicts into one `PresenceEntry` per
``(user, machine)`` tuple, dropping anything older than the activity
window and (by default) the caller's own activity.

The function is pure — no I/O, no clocks, no console — so the surrounding
CLI layer can fetch the log and compose presence with the rest of the
status renderable while tests drive the aggregation directly with hand-
built dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Optional


# Cap on how many recently-touched files we surface per (user, machine).
# Keeps the rendered table tidy even when a collaborator has just landed
# a 200-file refactor; the full list is still in `claude-mirror log`.
RECENT_FILES_CAP = 5


@dataclass
class PresenceEntry:
    """One collaborator's recent activity on this project."""

    user: str
    machine: str
    last_action: str
    last_timestamp: datetime
    recent_files: list[str] = field(default_factory=list)


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    The sync log writes `datetime.now(timezone.utc).isoformat()`, so the
    common shape is `2026-05-09T10:00:00+00:00`. We also tolerate the
    older `Z` suffix (some pre-v0.4 producers, hand-edited test fixtures).
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


def aggregate_presence(
    log_entries: list[dict[str, Any]],
    *,
    ignore_self: bool = True,
    self_user: Optional[str] = None,
    self_machine: Optional[str] = None,
    max_age_hours: int = 24,
    now: Optional[datetime] = None,
) -> list[PresenceEntry]:
    """Reduce a flat sync-log list into per-(user, machine) presence rows.

    Each input dict is the raw shape persisted in `_sync_log.json` —
    `{user, machine, action, timestamp, files, project, ...}`. Unknown
    keys are tolerated. Entries missing a `user`, `machine`, or
    parseable `timestamp` are skipped (a malformed line in the audit
    log must never poison the rest of the aggregation).

    Behaviour:
        * Entries older than `max_age_hours` are excluded entirely.
        * `ignore_self=True` (the default) drops every entry whose
          `(user, machine)` matches the calling machine. Pass False
          to include the caller — useful for the v1.1 JSON envelope
          when a script wants the full picture.
        * Each output entry's `last_action` and `last_timestamp` come
          from the *most recent* event for that pair; `recent_files`
          aggregates files across ALL events for the pair (newest
          first), capped at RECENT_FILES_CAP and de-duplicated.
        * The result is sorted by `last_timestamp` descending — most
          recent collaborator first.

    `now` is injectable so tests can pin the activity window without
    monkey-patching `datetime.utcnow()`. Defaults to wall-clock UTC.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    by_pair: dict[tuple[str, str], list[tuple[datetime, str, list[str]]]] = {}
    for entry in log_entries:
        if not isinstance(entry, dict):
            continue
        user = entry.get("user")
        machine = entry.get("machine")
        if not isinstance(user, str) or not user:
            continue
        if not isinstance(machine, str) or not machine:
            continue
        ts = _parse_timestamp(entry.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        if ignore_self and user == self_user and machine == self_machine:
            continue
        action = entry.get("action") or ""
        if not isinstance(action, str):
            action = ""
        files_raw = entry.get("files") or []
        files = [f for f in files_raw if isinstance(f, str)] if isinstance(files_raw, list) else []
        by_pair.setdefault((user, machine), []).append((ts, action, files))

    presence: list[PresenceEntry] = []
    for (user, machine), events in by_pair.items():
        # Sort newest-first; the head wins for last_action / last_timestamp,
        # and `recent_files` walks the same order so the surfaced list is
        # also newest-first.
        events.sort(key=lambda e: e[0], reverse=True)
        head_ts, head_action, _ = events[0]

        seen: set[str] = set()
        recent: list[str] = []
        for _, _, files in events:
            for f in files:
                if f in seen:
                    continue
                seen.add(f)
                recent.append(f)
                if len(recent) >= RECENT_FILES_CAP:
                    break
            if len(recent) >= RECENT_FILES_CAP:
                break

        presence.append(PresenceEntry(
            user=user,
            machine=machine,
            last_action=head_action,
            last_timestamp=head_ts,
            recent_files=recent,
        ))

    presence.sort(key=lambda p: p.last_timestamp, reverse=True)
    return presence


def humanize_age(ts: datetime, *, now: Optional[datetime] = None) -> str:
    """Render `ts` as a human-friendly delta from `now`.

    Examples: ``just now``, ``3m ago``, ``2h ago``, ``5d ago``. Used by
    the CLI's presence table; lives here so the tests can assert the
    exact string output without touching the Rich layer.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = now - ts
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"
