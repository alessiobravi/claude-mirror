from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

SYNC_LOG_NAME = "_sync_log.json"
LOGS_FOLDER = "_claude_mirror_logs"
MAX_LOG_ENTRIES = 200
# Cap on the number of file paths any single event records. Without this a
# malicious or buggy collaborator could publish an event with millions of
# entries that would explode memory in every consumer (notification text
# formatting, Slack post, inbox JSONL line, sync log on remote).
MAX_FILES_PER_EVENT = 100


def _truncate_files(files: list[str]) -> list[str]:
    """Cap a file list at MAX_FILES_PER_EVENT entries, with a sentinel marker
    indicating how many were dropped. Idempotent — applying twice is a no-op
    if the second list is already at-or-under the cap."""
    if files is None:
        return []
    if len(files) <= MAX_FILES_PER_EVENT:
        return list(files)
    kept = list(files[:MAX_FILES_PER_EVENT])
    kept.append(f"... and {len(files) - MAX_FILES_PER_EVENT} more")
    return kept


def _truncate_auto_resolved(
    entries: Optional[list[dict[str, str]]],
) -> list[dict[str, str]]:
    """Cap the auto-resolved-conflicts list to MAX_FILES_PER_EVENT, mirroring
    `_truncate_files`. Each entry is a {path, strategy} dict — the audit
    trail for `sync --no-prompt --strategy ...` runs. Idempotent."""
    if not entries:
        return []
    if len(entries) <= MAX_FILES_PER_EVENT:
        return [dict(e) for e in entries]
    kept = [dict(e) for e in entries[:MAX_FILES_PER_EVENT]]
    kept.append({
        "path": f"... and {len(entries) - MAX_FILES_PER_EVENT} more",
        "strategy": "",
    })
    return kept


@dataclass
class SyncEvent:
    machine: str
    user: str
    timestamp: str
    files: list[str]
    action: str   # "push" | "pull" | "sync" | "delete"
    project: str
    # Audit trail for non-interactive conflict resolution. Empty (default)
    # for interactive sync runs and every push/pull/delete event. Populated
    # only by `sync --no-prompt --strategy {keep-local,keep-remote}` so log
    # readers can spot which files were auto-overwritten and by which
    # strategy. Each entry is `{"path": str, "strategy": str}`. Older
    # consumers (pre-v0.5.49) ignore unknown fields when deserialising.
    auto_resolved_files: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def now(
        cls,
        machine: str,
        user: str,
        files: list[str],
        action: str,
        project: str,
        *,
        auto_resolved_files: Optional[list[dict[str, str]]] = None,
    ) -> SyncEvent:
        return cls(
            machine=machine,
            user=user,
            timestamp=datetime.now(timezone.utc).isoformat(),
            files=_truncate_files(files),
            action=action,
            project=project,
            auto_resolved_files=_truncate_auto_resolved(auto_resolved_files),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> SyncEvent:
        # Deserialise then re-cap files in case the producer was older / malicious.
        raw = json.loads(data)
        if isinstance(raw.get("files"), list):
            raw["files"] = _truncate_files(raw["files"])
        # Re-cap the auto_resolved_files list. Tolerant of older producers
        # that didn't ship the field at all (default factory takes over).
        if isinstance(raw.get("auto_resolved_files"), list):
            raw["auto_resolved_files"] = _truncate_auto_resolved(
                raw["auto_resolved_files"]
            )
        # Drop unknown fields so a future schema bump doesn't crash older readers.
        valid = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}
        return cls(**valid)

    def summary(self) -> str:
        files_str = ", ".join(self.files) if self.files else "no files"
        return (
            f"{self.user}@{self.machine} {self.action}ed [{files_str}] "
            f"in '{self.project}' at {self.timestamp}"
        )


class SyncLog:
    """Persistent audit log stored as JSON on Drive."""

    def __init__(self) -> None:
        self.events: list[SyncEvent] = []

    @classmethod
    def from_bytes(cls, data: bytes) -> SyncLog:
        log = cls()
        raw = json.loads(data.decode())
        events: list[SyncEvent] = []
        for e in raw.get("events", []):
            # Re-apply the per-event file cap. The sync log lives on remote
            # storage and could contain entries published by older versions
            # (no cap) or by a malicious actor — caller still gets bounded
            # SyncEvent objects.
            if isinstance(e.get("files"), list):
                e["files"] = _truncate_files(e["files"])
            # Same defensive re-cap on the auto-resolution audit trail.
            if isinstance(e.get("auto_resolved_files"), list):
                e["auto_resolved_files"] = _truncate_auto_resolved(
                    e["auto_resolved_files"]
                )
            # Strip unknown fields so a forward-compat schema bump on the
            # remote doesn't crash an older reader. The dataclass default
            # factory fills in any missing optional field.
            valid = {k: v for k, v in e.items() if k in SyncEvent.__dataclass_fields__}
            events.append(SyncEvent(**valid))
        log.events = events
        return log

    def append(self, event: SyncEvent) -> None:
        self.events.append(event)
        if len(self.events) > MAX_LOG_ENTRIES:
            self.events = self.events[-MAX_LOG_ENTRIES:]

    def to_bytes(self) -> bytes:
        raw = {"events": [asdict(e) for e in self.events]}
        return json.dumps(raw, indent=2).encode()
