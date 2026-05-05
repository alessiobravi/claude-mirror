"""Backward-compatibility shim — PubSubClient is now in notifications.pubsub."""
from .notifications.pubsub import PubSubNotifier as PubSubClient

__all__ = ["PubSubClient"]
