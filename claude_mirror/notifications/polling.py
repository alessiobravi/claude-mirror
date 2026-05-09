from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Callable, Optional, Tuple

from ..config import Config
from ..events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from . import NotificationBackend

if TYPE_CHECKING:
    from ..backends import StorageBackend


# Stable identity for an event: SyncEvent has no `id` field, so we
# synthesise one from fields that uniquely identify a publish. Using a
# composite key (rather than `len(log.events)`) makes the watermark
# survive log rotation/truncation: if the previously-seen event vanishes
# from the log, we fall back to dispatching the entire current log.
_EventKey = Tuple[str, str, str, Tuple[str, ...]]


def _event_key(event: SyncEvent) -> _EventKey:
    return (event.timestamp, event.user, event.machine, tuple(event.files))


class PollingNotifier(NotificationBackend):
    """Notification backend using periodic polling of the sync log.

    Works with any storage backend (WebDAV, OneDrive, etc.) that doesn't
    have a native push/longpoll mechanism.
    """

    def __init__(self, config: Config, storage: "StorageBackend") -> None:
        self.config = config
        self._storage: StorageBackend = storage
        self._poll_interval: int = getattr(config, "poll_interval", 30)

    def ensure_topic(self) -> None:
        """No-op — polling has no topic/channel concept."""

    def ensure_subscription(self) -> None:
        """No-op — polling needs no subscription setup."""

    def publish_event(self, event: SyncEvent) -> None:
        """Write event to the sync log on the remote storage."""
        root = self.config.root_folder
        logs_folder = self._storage.get_file_id(LOGS_FOLDER, root)
        if not logs_folder:
            logs_folder = self._storage.get_or_create_folder(LOGS_FOLDER, root)

        log_file_id = self._storage.get_file_id(SYNC_LOG_NAME, logs_folder)

        # Read existing log
        log = SyncLog()
        if log_file_id:
            try:
                raw = self._storage.download_file(log_file_id)
                log = SyncLog.from_bytes(raw)
            except Exception:
                pass

        log.append(event)
        self._storage.upload_bytes(
            log.to_bytes(), SYNC_LOG_NAME, logs_folder,
            file_id=log_file_id,
        )

    def watch(self, callback: Callable[[SyncEvent], None], stop_event: threading.Event) -> None:
        """Poll the sync log periodically for new events from other machines."""
        last_seen_key = self._get_last_log_key()

        while not stop_event.is_set():
            stop_event.wait(self._poll_interval)
            if stop_event.is_set():
                break

            last_seen_key = self._dispatch_new_events(
                callback, last_seen_key,
            )

    def watch_once(self, callback: Callable[[SyncEvent], None]) -> None:
        """Run exactly one polling cycle and return.

        Used by `claude-mirror watch --once` for cron-driven setups.
        Reads a persistent watermark (last-seen event key) from
        `~/.config/claude_mirror/watch_once_state/...json` so successive
        runs only dispatch events that arrived since the previous
        invocation.

        Bootstrap rule: on the very first `--once` run for a project we
        do NOT surface every historical event from the log (that would
        flood the user with weeks of past events the moment they first
        wire up cron). Instead we capture the current log tail as the
        initial watermark and dispatch nothing — subsequent runs then
        flow normally.
        """
        from .._watch_once_state import (
            load_watermark, save_watermark, FIRST_RUN_SENTINEL,
        )

        key = ("poll", self.config.machine_name, self.config.project_path)
        watermark = load_watermark(key)
        if watermark is FIRST_RUN_SENTINEL:
            current_tail = self._get_last_log_key()
            # Record current tail (or an empty marker if log is empty).
            save_watermark(key, current_tail if current_tail is not None else ("", "", "", ()))
            return
        # watermark is either a real event-key tuple or the empty marker
        # ("", "", "", ()) which `_dispatch_new_events` will treat as
        # "key not found → re-dispatch from top". Translate the empty
        # marker back to None to opt into the rotation-safe branch.
        last_seen = None if watermark == ("", "", "", ()) else watermark
        new_last = self._dispatch_new_events(callback, last_seen)
        if new_last is not None and new_last != last_seen:
            save_watermark(key, new_last)

    def close(self) -> None:
        """No persistent resources to release."""

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_last_log_key(self) -> Optional[_EventKey]:
        """Return the composite key of the last event in the remote sync log,
        or None if the log doesn't exist / is empty."""
        root = self.config.root_folder
        logs_folder = self._storage.get_file_id(LOGS_FOLDER, root)
        if not logs_folder:
            return None
        log_file_id = self._storage.get_file_id(SYNC_LOG_NAME, logs_folder)
        if not log_file_id:
            return None
        try:
            raw = self._storage.download_file(log_file_id)
            log = SyncLog.from_bytes(raw)
            if not log.events:
                return None
            return _event_key(log.events[-1])
        except Exception:
            return None

    def _dispatch_new_events(
        self,
        callback: Callable[[SyncEvent], None],
        last_seen_key: Optional[_EventKey],
    ) -> Optional[_EventKey]:
        """Read the sync log, dispatch events added since last_seen_key,
        return the new last-seen key.

        Survives log rotation: if the previously-seen key isn't in the
        current log, we treat every event as new (the log was truncated
        or replaced and we have no other way to resync).
        """
        root = self.config.root_folder
        logs_folder = self._storage.get_file_id(LOGS_FOLDER, root)
        if not logs_folder:
            return last_seen_key
        log_file_id = self._storage.get_file_id(SYNC_LOG_NAME, logs_folder)
        if not log_file_id:
            return last_seen_key
        try:
            raw = self._storage.download_file(log_file_id)
            log = SyncLog.from_bytes(raw)
        except Exception:
            return last_seen_key

        if not log.events:
            return last_seen_key

        # Find the index AFTER the last-seen key. If the key is missing
        # (rotation/truncation), start from the beginning.
        start_idx = 0
        if last_seen_key is not None:
            found = False
            for idx, ev in enumerate(log.events):
                if _event_key(ev) == last_seen_key:
                    start_idx = idx + 1
                    found = True
                    break
            if not found:
                # Log was rotated/truncated — re-dispatch from the top.
                start_idx = 0

        for event in log.events[start_idx:]:
            if event.machine == self.config.machine_name:
                continue  # ignore own events
            callback(event)
        return _event_key(log.events[-1])
