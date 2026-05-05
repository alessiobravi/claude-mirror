from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone

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


@dataclass
class SyncEvent:
    machine: str
    user: str
    timestamp: str
    files: list[str]
    action: str   # "push" | "pull" | "sync" | "delete"
    project: str

    @classmethod
    def now(cls, machine: str, user: str, files: list[str], action: str, project: str) -> SyncEvent:
        return cls(
            machine=machine,
            user=user,
            timestamp=datetime.now(timezone.utc).isoformat(),
            files=_truncate_files(files),
            action=action,
            project=project,
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, data: str) -> SyncEvent:
        # Deserialise then re-cap files in case the producer was older / malicious.
        raw = json.loads(data)
        if isinstance(raw.get("files"), list):
            raw["files"] = _truncate_files(raw["files"])
        return cls(**raw)

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
            events.append(SyncEvent(**e))
        log.events = events
        return log

    def append(self, event: SyncEvent) -> None:
        self.events.append(event)
        if len(self.events) > MAX_LOG_ENTRIES:
            self.events = self.events[-MAX_LOG_ENTRIES:]

    def to_bytes(self) -> bytes:
        raw = {"events": [asdict(e) for e in self.events]}
        return json.dumps(raw, indent=2).encode()
