"""Generic webhook notification backends — Discord, Microsoft Teams, and a
schema-stable Generic JSON envelope.

These are siblings to the existing Slack integration in
``claude_mirror.slack``: each one is opt-in per project, formats a
:class:`~claude_mirror.events.SyncEvent` into a backend-specific JSON payload,
and POSTs it to a configured webhook URL via stdlib ``urllib.request``.

Design notes
------------
* All three notifiers extend :class:`WebhookNotifier`, which owns the actual
  network transport (``post_json``). Subclasses only have to implement
  ``_format_event`` — what the JSON body should look like for that backend.
* Best-effort delivery is the contract: any error during send returns
  ``False`` and is logged at ``DEBUG`` only. A misconfigured webhook must
  never raise out of ``notify`` — the sync command path is the caller and
  it cannot tolerate a notifier failure blocking a push.
* No new dependency: stdlib ``urllib.request`` is enough for a one-shot
  POST with custom headers.
* The Slack notifier in ``claude_mirror/slack.py`` predates this abstraction
  and is intentionally left untouched — its rich-block formatting +
  per-backend-status enrichment is Slack-specific and would be diluted by
  forcing it through the same shape.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Optional
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from ..events import SyncEvent


logger = logging.getLogger(__name__)


# Cap for the file list rendered into a webhook message. Beyond this, an
# "and N more" sentinel collapses the rest. Keeps Discord embeds and
# Teams cards under their respective per-message size budgets and matches
# the cap already used by the Slack notifier so users see consistent
# truncation across backends.
_FILES_DISPLAY_LIMIT = 10

# Color mapping shared by Discord (decimal int) and Teams (hex string,
# no `#` prefix per MessageCard schema). Picked for visual parity with
# Slack's emoji set: green for the "good push", blue for "synced" /
# "pulled" status updates, red for destructive deletes, grey fallback.
_ACTION_COLORS = {
    "push":   ("#22c55e", 0x22C55E),  # green-500
    "pull":   ("#3b82f6", 0x3B82F6),  # blue-500
    "sync":   ("#3b82f6", 0x3B82F6),  # blue-500 (same family as pull)
    "delete": ("#ef4444", 0xEF4444),  # red-500
}
_DEFAULT_COLOR = ("#6b7280", 0x6B7280)  # grey-500 fallback for unknown actions


def _color_for(action: str) -> tuple[str, int]:
    return _ACTION_COLORS.get(action, _DEFAULT_COLOR)


def _truncate_for_display(files: list[str]) -> tuple[list[str], int]:
    """Return (visible, dropped_count) capped at _FILES_DISPLAY_LIMIT."""
    if len(files) <= _FILES_DISPLAY_LIMIT:
        return list(files), 0
    return list(files[:_FILES_DISPLAY_LIMIT]), len(files) - _FILES_DISPLAY_LIMIT


def _render_file_lines(files: list[str]) -> str:
    """Render the file list as bullet-prefixed lines with a final
    "and N more" sentinel when truncated. Used by Discord and Teams which
    both want a single multi-line string field rather than a structured
    list."""
    visible, dropped = _truncate_for_display(files)
    lines = [f"- {f}" for f in visible]
    if dropped:
        lines.append(f"and {dropped} more")
    return "\n".join(lines) if lines else "(no files)"


class WebhookNotifier(ABC):
    """Base class for HTTP-webhook notification backends.

    Subclasses implement :meth:`_format_event` to build the
    backend-specific JSON payload. Transport (POST + error handling) is
    centralized in :meth:`post_json`.
    """

    def __init__(
        self,
        webhook_url: str,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> None:
        self.webhook_url = webhook_url
        # Defensive copy so a caller mutating the dict afterward can't
        # affect already-constructed notifiers.
        self.extra_headers: dict[str, str] = dict(extra_headers or {})

    def post_json(self, payload: dict, *, timeout_seconds: float = 5.0) -> bool:
        """POST ``payload`` as JSON to ``self.webhook_url``.

        Returns ``True`` on any 2xx HTTP response, ``False`` on network
        error, timeout, or non-2xx status. Errors are logged at DEBUG
        only — the contract is best-effort, and a noisy notifier would
        spam every sync command. No exception escapes this method.
        """
        if not self.webhook_url:
            return False
        try:
            data = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as e:
            logger.debug("webhook payload not JSON-serialisable: %s", e)
            return False

        headers = {"Content-Type": "application/json"}
        # User-supplied headers win over the default Content-Type, but we
        # still set Content-Type first so omitting it from extra_headers
        # does the right thing.
        headers.update(self.extra_headers)

        req = Request(self.webhook_url, data=data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout_seconds) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    # Older urllib.response objects expose getcode().
                    status = resp.getcode()
                if 200 <= int(status) < 300:
                    return True
                logger.debug(
                    "webhook POST to %s returned status %s", self.webhook_url, status
                )
                return False
        except HTTPError as e:
            # 4xx/5xx come back here on Python's urllib (HTTPError is a
            # subclass of URLError, so order matters in the except clause).
            logger.debug("webhook POST HTTPError %s on %s", e.code, self.webhook_url)
            return False
        except URLError as e:
            logger.debug("webhook POST URLError on %s: %s", self.webhook_url, e)
            return False
        except (OSError, ValueError) as e:
            # OSError covers socket-level failures, timeouts. ValueError
            # guards against malformed URLs reaching urlopen.
            logger.debug("webhook POST failure on %s: %s", self.webhook_url, e)
            return False

    @abstractmethod
    def _format_event(self, event: SyncEvent) -> dict:
        """Build the backend-specific JSON payload for ``event``."""

    def notify(self, event: SyncEvent) -> None:
        """Format + send. Best-effort: never raises."""
        try:
            payload = self._format_event(event)
        except Exception as e:  # noqa: BLE001 - never escape from notify()
            logger.debug("webhook _format_event failed: %s", e)
            return
        # post_json swallows its own errors; we don't need a second
        # try/except here. The wrapper exists purely so subclasses can
        # `notifier.notify(event)` without thinking about transport.
        self.post_json(payload)


class DiscordWebhookNotifier(WebhookNotifier):
    """POST to a Discord incoming webhook (``https://discord.com/api/webhooks/...``).

    Discord renders our payload as a single embed card per event:
    coloured stripe matching the action, title summarising who/what,
    one field per metadata item (Action / User / Machine / Project) and
    one field for the file list (capped at 10 + "and N more").
    """

    def _format_event(self, event: SyncEvent) -> dict:
        _hex, decimal_color = _color_for(event.action)
        file_count = len(event.files)
        file_word = "file" if file_count == 1 else "files"
        title = (
            f"{event.user}@{event.machine} {event.action}ed "
            f"{file_count} {file_word} in {event.project}"
        )
        files_block = _render_file_lines(event.files)

        return {
            "username": "claude-mirror",
            "embeds": [
                {
                    "title": title,
                    "description": f"Sync event at {event.timestamp}",
                    "color": decimal_color,
                    "fields": [
                        {"name": "Action",  "value": event.action,   "inline": True},
                        {"name": "User",    "value": event.user,     "inline": True},
                        {"name": "Machine", "value": event.machine,  "inline": True},
                        {"name": "Project", "value": event.project,  "inline": True},
                        {"name": "Files",   "value": files_block,    "inline": False},
                    ],
                    "timestamp": event.timestamp,
                }
            ],
        }


class TeamsWebhookNotifier(WebhookNotifier):
    """POST to a Microsoft Teams Incoming Webhook (legacy connector or the
    newer ``{tenant}.webhook.office.com`` URL form).

    Teams accepts the **MessageCard** schema. We render a single card
    with one section: the activity title carries the headline, the
    facts list breaks out per-attribute metadata, and the body text
    holds the file list.
    """

    def _format_event(self, event: SyncEvent) -> dict:
        hex_color, _decimal = _color_for(event.action)
        # MessageCard themeColor is a hex string without the `#` prefix.
        theme_color = hex_color.lstrip("#")

        file_count = len(event.files)
        file_word = "file" if file_count == 1 else "files"
        summary = (
            f"{event.user}@{event.machine} {event.action}ed "
            f"{file_count} {file_word} in {event.project}"
        )
        files_block = _render_file_lines(event.files)

        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": theme_color,
            "summary": summary,
            "sections": [
                {
                    "activityTitle": summary,
                    "activitySubtitle": f"claude-mirror at {event.timestamp}",
                    "facts": [
                        {"name": "Action",  "value": event.action},
                        {"name": "User",    "value": event.user},
                        {"name": "Machine", "value": event.machine},
                        {"name": "Project", "value": event.project},
                    ],
                    "text": f"**Files changed:**\n{files_block}",
                }
            ],
        }


class GenericWebhookNotifier(WebhookNotifier):
    """POST a schema-stable generic JSON envelope to an arbitrary URL.

    Designed for users wiring claude-mirror into automation platforms
    (n8n, Make, Zapier, internal Slack-replacements, custom dashboards).
    The envelope is **schema version 1** and will only ever be extended
    additively — fields are never renamed or removed, so a downstream
    consumer that pinned to v1 keeps working forever.

    Authentication / routing headers (``Authorization: Bearer ...``,
    ``X-Tenant-ID: ...``, etc.) are passed in via ``extra_headers`` at
    construction time and end up on the actual urllib request.
    """

    SCHEMA_VERSION = 1

    def _format_event(self, event: SyncEvent) -> dict:
        # Cap files so a 10k-file push doesn't ship a 5 MB envelope to a
        # tiny n8n webhook. We keep ALL files (no truncation marker) up
        # to MAX_FILES_PER_EVENT, since that cap is already enforced at
        # SyncEvent construction time.
        return {
            "version": self.SCHEMA_VERSION,
            "event": event.action,
            "user": event.user,
            "machine": event.machine,
            "project": event.project,
            "files": list(event.files),
            "timestamp": event.timestamp,
        }
