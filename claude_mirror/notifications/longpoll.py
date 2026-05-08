from __future__ import annotations

import json
import threading
from typing import Callable, Optional, Tuple

try:
    import dropbox
    from dropbox.files import FileMetadata
except ImportError:
    raise ImportError(
        "Dropbox SDK is required for the Dropbox notification backend.\n"
        "Install it with:  pipx install -e '.[dropbox]' --force"
    )

from ..config import Config
from ..events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from . import NotificationBackend


# See polling.py for design rationale: composite key is rotation-safe
# whereas a numeric watermark (`len(log.events)`) becomes a future index
# the moment the log is truncated or replaced, hiding new events forever.
_EventKey = Tuple[str, str, str, Tuple[str, ...]]


def _event_key(event: SyncEvent) -> _EventKey:
    return (event.timestamp, event.user, event.machine, tuple(event.files))


class DropboxLongPollNotifier(NotificationBackend):
    """Notification backend using Dropbox longpoll (files/list_folder/longpoll)."""

    def __init__(self, config: Config, dbx: dropbox.Dropbox) -> None:
        self.config = config
        self._dbx = dbx
        self._folder_path = config.dropbox_folder

    def ensure_topic(self) -> None:
        """No-op — Dropbox has no topic/channel concept."""

    def ensure_subscription(self) -> None:
        """No-op — longpoll needs no subscription setup."""

    def publish_event(self, event: SyncEvent) -> None:
        """Write event to the sync log on Dropbox (same format as Google Drive)."""
        logs_path = f"{self._folder_path}/{LOGS_FOLDER}"
        log_file_path = f"{logs_path}/{SYNC_LOG_NAME}"

        # Ensure logs folder exists
        try:
            self._dbx.files_create_folder_v2(logs_path)
        except Exception:
            pass  # already exists

        # Read existing log
        log = SyncLog()
        try:
            _, response = self._dbx.files_download(log_file_path)
            log = SyncLog.from_bytes(response.content)
        except Exception:
            pass

        log.append(event)
        self._dbx.files_upload(
            log.to_bytes(), log_file_path,
            mode=dropbox.files.WriteMode.overwrite,
        )

    def watch(self, callback: Callable[[SyncEvent], None], stop_event: threading.Event) -> None:
        """
        Block and long-poll for changes. When changes are detected, check the
        sync log for new events from other machines and dispatch them.
        """
        # Get initial cursor
        result = self._dbx.files_list_folder_get_latest_cursor(
            self._folder_path, recursive=True,
        )
        cursor = result.cursor
        last_seen_key = self._get_last_log_key()

        while not stop_event.is_set():
            try:
                longpoll = self._dbx.files_list_folder_longpoll(cursor, timeout=90)
            except Exception:
                if stop_event.is_set():
                    break
                stop_event.wait(10)
                continue

            if stop_event.is_set():
                break

            if longpoll.changes:
                # Advance the cursor
                try:
                    result = self._dbx.files_list_folder_continue(cursor)
                    cursor = result.cursor
                    while result.has_more:
                        result = self._dbx.files_list_folder_continue(cursor)
                        cursor = result.cursor
                except Exception:
                    pass

                # Check sync log for new events from other machines
                last_seen_key = self._dispatch_new_events(
                    callback, last_seen_key,
                )

            if longpoll.backoff:
                stop_event.wait(longpoll.backoff)

    def watch_once(self, callback: Callable[[SyncEvent], None]) -> None:
        """Run a single sync-log fetch + dispatch and return.

        Used by `claude-mirror watch --once` for cron-driven setups.
        Unlike the streaming `watch()` loop, this never opens a longpoll
        connection — the event log is the source of truth, and the
        watermark file in `watch_once_state/` lets successive runs only
        surface events that arrived since the previous invocation.

        First-run bootstrap: capture the current log tail and dispatch
        nothing, so the very first cron tick after install does not
        flood the user with weeks of historical events.
        """
        from .._watch_once_state import (
            load_watermark, save_watermark, FIRST_RUN_SENTINEL,
        )

        key = ("longpoll", self.config.machine_name, self.config.project_path)
        watermark = load_watermark(key)
        if watermark is FIRST_RUN_SENTINEL:
            current_tail = self._get_last_log_key()
            save_watermark(key, current_tail if current_tail is not None else ("", "", "", ()))
            return
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
        log_file_path = f"{self._folder_path}/{LOGS_FOLDER}/{SYNC_LOG_NAME}"
        try:
            _, response = self._dbx.files_download(log_file_path)
            log = SyncLog.from_bytes(response.content)
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
        log_file_path = f"{self._folder_path}/{LOGS_FOLDER}/{SYNC_LOG_NAME}"
        try:
            _, response = self._dbx.files_download(log_file_path)
            log = SyncLog.from_bytes(response.content)
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
