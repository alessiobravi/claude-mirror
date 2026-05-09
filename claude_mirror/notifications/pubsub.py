from __future__ import annotations

import threading
from typing import Any, Callable

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud.pubsub_v1 import PublisherClient, SubscriberClient
from google.oauth2.credentials import Credentials

from ..config import Config
from ..events import SyncEvent
from . import NotificationBackend


class PubSubNotifier(NotificationBackend):
    def __init__(self, config: Config, credentials: Credentials) -> None:
        self.config = config
        self._credentials = credentials
        self._publisher = PublisherClient(credentials=credentials)
        self._subscriber = SubscriberClient(credentials=credentials)
        self._topic_path = self._publisher.topic_path(
            config.gcp_project_id, config.pubsub_topic_id
        )
        self._subscription_path = self._subscriber.subscription_path(
            config.gcp_project_id, config.subscription_id
        )
        self._streaming_pull_future: Any = None

    def ensure_topic(self) -> None:
        try:
            self._publisher.create_topic(request={"name": self._topic_path})
        except AlreadyExists:
            pass

    def ensure_subscription(self) -> None:
        try:
            self._subscriber.create_subscription(
                request={
                    "name": self._subscription_path,
                    "topic": self._topic_path,
                    "ack_deadline_seconds": 60,
                }
            )
        except AlreadyExists:
            pass

    def publish_event(self, event: SyncEvent) -> None:
        """Synchronous publish: block until the broker has acknowledged."""
        future = self.publish_event_async(event)
        future.result()

    def publish_event_async(self, event: SyncEvent) -> Any:
        """Non-blocking publish. Returns a future the caller can `result()`
        later to confirm delivery. Used by SyncEngine to flush all publishes
        for a command in a single batch at the end of the operation."""
        data = event.to_json().encode("utf-8")
        return self._publisher.publish(
            self._topic_path,
            data=data,
            machine=event.machine,
            user=event.user,
            project=event.project,
        )

    def watch(self, callback: Callable[[SyncEvent], None], stop_event: threading.Event) -> None:
        """
        Start streaming subscription. Calls callback for each message
        not originating from this machine. Blocks until stop_event is set.
        """
        def _on_message(message: Any) -> None:
            try:
                event = SyncEvent.from_json(message.data.decode("utf-8"))
                message.ack()
                if event.machine == self.config.machine_name:
                    return  # ignore own messages
                callback(event)
            except Exception:
                message.nack()

        self._streaming_pull_future = self._subscriber.subscribe(
            self._subscription_path, callback=_on_message
        )

        try:
            stop_event.wait()
        finally:
            self._streaming_pull_future.cancel()
            try:
                self._streaming_pull_future.result(timeout=5)
            except Exception:
                pass

    def watch_once(self, callback: Callable[[SyncEvent], None]) -> None:
        """Run one synchronous Pub/Sub pull and dispatch the batch.

        Used by `claude-mirror watch --once` for cron-driven setups.
        Calls `subscriber.pull(...)` with `return_immediately=True`
        and a small `max_messages` cap — each cron tick processes at
        most that many pending messages and returns. Unprocessed
        messages stay on the subscription for the next tick.

        Unlike the polling and longpoll backends, Pub/Sub already
        provides a per-subscription cursor server-side (acks consume
        messages off the queue), so no separate watermark file is
        required.
        """
        max_messages = 100
        try:
            response = self._subscriber.pull(
                request={
                    "subscription": self._subscription_path,
                    "max_messages": max_messages,
                    "return_immediately": True,
                },
                timeout=10,
            )
        except Exception:
            # Network/auth blip — surface zero events; the next cron
            # tick will retry. We deliberately do not raise: cron-driven
            # operation should not turn a transient blip into a non-zero
            # exit and an emailed failure.
            return

        ack_ids: list[str] = []
        for received in response.received_messages:
            try:
                event = SyncEvent.from_json(received.message.data.decode("utf-8"))
                if event.machine != self.config.machine_name:
                    callback(event)
                ack_ids.append(received.ack_id)
            except Exception:
                # Malformed payload — skip, do not ack, let the broker
                # redeliver and dead-letter through its own policy.
                continue

        if ack_ids:
            try:
                self._subscriber.acknowledge(
                    request={
                        "subscription": self._subscription_path,
                        "ack_ids": ack_ids,
                    }
                )
            except Exception:
                # Same rationale as above — swallow on cron path.
                pass

    def close(self) -> None:
        self._subscriber.close()
