from __future__ import annotations

import threading
from typing import Callable

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
        self._streaming_pull_future = None

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

    def publish_event_async(self, event: SyncEvent):
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
        def _on_message(message):
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

    def close(self) -> None:
        self._subscriber.close()
