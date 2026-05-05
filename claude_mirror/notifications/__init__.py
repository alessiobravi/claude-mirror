from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable

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

    def publish_event_async(self, event: SyncEvent):
        """Publish without blocking. Default implementation falls back to the
        synchronous publish (returns None). Backends with native async support
        (e.g. Pub/Sub) override this and return a future."""
        self.publish_event(event)
        return None

    @abstractmethod
    def watch(self, callback: Callable[[SyncEvent], None], stop_event: threading.Event) -> None:
        """Block and listen for events, calling callback for each. Stop when stop_event is set."""

    @abstractmethod
    def close(self) -> None:
        """Release resources."""
