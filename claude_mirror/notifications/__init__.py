from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Any, Callable

from ..events import SyncEvent


class NotificationBackend(ABC):
    """Abstract interface for real-time sync notification backends."""

    @abstractmethod
    def ensure_topic(self) -> None:
        """Create the notification channel/topic if it doesn't exist."""

    @abstractmethod
    def ensure_subscription(self) -> None:
        """Create the subscription/listener endpoint if it doesn't exist."""

    @abstractmethod
    def publish_event(self, event: SyncEvent) -> None:
        """Publish a sync event to the notification channel."""

    def publish_event_async(self, event: SyncEvent) -> Any:
        """Publish without blocking. Default implementation falls back to the
        synchronous publish (returns None). Backends with native async support
        (e.g. Pub/Sub) override this and return a future."""
        self.publish_event(event)
        return None

    @abstractmethod
    def watch(self, callback: Callable[[SyncEvent], None], stop_event: threading.Event) -> None:
        """Block and listen for events, calling callback for each. Stop when stop_event is set."""

    def watch_once(self, callback: Callable[[SyncEvent], None]) -> None:
        """Run exactly one polling/listen cycle and return.

        Used by `claude-mirror watch --once` for cron-driven setups where
        a long-lived watcher daemon is undesirable. Each backend
        overrides this with the cheapest single-cycle equivalent of its
        normal watch loop:

          * Polling backend → one log fetch, dispatch new events, return.
          * Dropbox longpoll → one short longpoll, dispatch, return.
          * Pub/Sub → one synchronous pull, dispatch, return.

        The default implementation falls back to `watch()` with a stop
        event already set so backends that don't override it still
        terminate quickly (they may emit zero events, which is the
        correct result for "no changes since last run").
        """
        stop = threading.Event()
        stop.set()
        self.watch(callback, stop)

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
