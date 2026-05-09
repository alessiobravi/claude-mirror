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
from typing import Any, Optional, Union
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen

from ..events import SyncEvent


logger = logging.getLogger(__name__)

# Sentinel returned from `_config_template_for` when the per-backend
# template dict has no entry for the event's action — distinct from
# `None` (template-config explicitly omitted) only at the call site,
# but kept readable here for grep-ability.
_NO_TEMPLATE: None = None


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


def _render_file_list_inline(files: list[str]) -> str:
    """Render the file list as a comma-separated inline string used by
    the {file_list} placeholder in templates. Cap at the same limit as
    the bullet renderer so users see consistent truncation regardless
    of which template they pick."""
    visible, dropped = _truncate_for_display(files)
    if not visible:
        return ""
    base = ", ".join(visible)
    if dropped:
        return f"{base}, and {dropped} more"
    return base


def event_template_vars(event: SyncEvent) -> dict[str, object]:
    """Build the placeholder dictionary passed to ``str.format`` when
    rendering a notification template.

    The set of keys is the documented contract — adding a new variable
    here means updating ``docs/admin.md`` so users know it's available.
    Removing a variable is a breaking change for any project that put
    it in their template, so don't.

    `snapshot_timestamp` defaults to the literal string ``"unknown"`` to
    keep the template path well-defined for projects that haven't
    plumbed it through (the field doesn't exist on every SyncEvent).
    """
    files = list(event.files or [])
    snapshot_ts = getattr(event, "snapshot_timestamp", None) or "unknown"
    return {
        "user":               event.user,
        "machine":            event.machine,
        "project":            event.project,
        "action":             event.action,
        "n_files":            len(files),
        "file_list":          _render_file_list_inline(files),
        "first_file":         files[0] if files else "",
        "timestamp":          event.timestamp,
        "snapshot_timestamp": snapshot_ts,
    }


def _render_str_template(template: str, event: SyncEvent) -> str:
    """Render a single ``str.format``-style template against an event.

    Raises ``KeyError`` if the template references a placeholder not in
    ``event_template_vars`` and ``ValueError`` for malformed template
    syntax. Both bubble up to the caller (`_format_event`) which catches
    them and falls back to the built-in format with a yellow info line.
    """
    if template == "":
        # Empty string is treated as "no template configured" — a user
        # that types `push: ""` clearly didn't mean to suppress the
        # default summary entirely. Fall through to built-in format.
        raise KeyError("__empty_template__")
    return template.format(**event_template_vars(event))


def _render_dict_template(
    template: dict[str, Any], event: SyncEvent,
) -> dict[str, Any]:
    """Render a dict-of-format-strings template (used by Generic webhook).

    Each value is run through ``str.format``; non-string values pass
    through unchanged so a user can ship a literal int / bool / nested
    dict in their template. Raises ``KeyError`` / ``ValueError`` on bad
    templates, same contract as ``_render_str_template``.
    """
    if not template:
        raise KeyError("__empty_template__")
    vars_ = event_template_vars(event)
    out: dict[str, Any] = {}
    for key, val in template.items():
        if isinstance(val, str):
            out[key] = val.format(**vars_)
        else:
            # Pass non-string values through untouched. A YAML-authored
            # int / bool / list is a legitimate way to ship a literal
            # value in the envelope without making the user wrap it in
            # `"{value}"`.
            out[key] = val
    return out


def _log_template_fallback(
    backend: str, action: str, exc: Exception,
) -> None:
    """Emit the user-facing yellow info line on template-render failure.

    Uses Rich's console if available so the colour matches the rest of
    claude-mirror's surface; falls back to plain stderr if Rich isn't
    importable (test environments / minimal installs)."""
    detail = str(exc) or type(exc).__name__
    # Strip the inner-only "__empty_template__" sentinel — that's a
    # control-flow signal, not something the user should see.
    if detail == "'__empty_template__'":
        detail = "empty template"
    msg = (
        f"Notification template error ({backend}, action={action!r}): "
        f"{detail} — falling back to default format."
    )
    try:
        from rich.console import Console
        Console().print(f"[yellow]warn[/]  {msg}")
    except Exception:  # pragma: no cover — keep going on rich failure
        import sys
        print(f"warn  {msg}", file=sys.stderr)


class WebhookNotifier(ABC):
    """Base class for HTTP-webhook notification backends.

    Subclasses implement :meth:`_format_event` to build the
    backend-specific JSON payload. Transport (POST + error handling) is
    centralized in :meth:`post_json`.
    """

    # Symbolic name surfaced in template-fallback log lines. Subclasses
    # override so the user sees "Discord" / "Teams" / "Generic" rather
    # than the Python class name. None means fall back to type(self).__name__.
    BACKEND_LABEL: Optional[str] = None

    def __init__(
        self,
        webhook_url: str,
        extra_headers: Optional[dict[str, str]] = None,
        *,
        templates: Optional[dict[str, Any]] = None,
    ) -> None:
        self.webhook_url = webhook_url
        # Defensive copy so a caller mutating the dict afterward can't
        # affect already-constructed notifiers.
        self.extra_headers: dict[str, str] = dict(extra_headers or {})
        # Per-action template dict (keys: push/pull/sync/delete; values:
        # str-format templates for Slack/Discord/Teams, dicts for
        # Generic). None means "no templates configured" — every
        # `_format_event` falls through to the built-in format. We
        # defensively copy so an upstream config object that gets
        # mutated after construction can't change a long-lived
        # notifier's template choice mid-flight.
        self.templates: Optional[dict[str, Any]] = (
            dict(templates) if templates else None
        )

    def _config_template_for(self, action: str) -> Optional[Union[str, dict[str, Any]]]:
        """Return the configured template for ``action``, or ``None``.

        Per-action lookup, NOT per-event: the same template applies to
        every event with that action. ``None`` means either templates
        weren't configured at all OR the user didn't supply one for
        this specific action — both cases route the caller to the
        built-in format.
        """
        if not self.templates:
            return None
        return self.templates.get(action)

    def _backend_label(self) -> str:
        """Friendly backend name for log lines."""
        return self.BACKEND_LABEL or type(self).__name__

    def post_json(self, payload: dict[str, Any], *, timeout_seconds: float = 5.0) -> bool:
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
    def _format_event(self, event: SyncEvent) -> dict[str, Any]:
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

    When a per-action template is configured (``discord_template_format``
    in the project YAML), the rendered template REPLACES the embed title;
    every other field — colour stripe, metadata fields, file list — is
    preserved so users always see the structured detail regardless of
    template wording.
    """

    BACKEND_LABEL = "Discord"

    def _format_event(self, event: SyncEvent) -> dict[str, Any]:
        _hex, decimal_color = _color_for(event.action)
        file_count = len(event.files)
        file_word = "file" if file_count == 1 else "files"
        files_block = _render_file_lines(event.files)

        # Template override — replaces the embed title only. The
        # built-in default kicks in when (a) no template is configured
        # for this action, (b) the template renders empty, or (c) the
        # template references an unknown placeholder.
        template = self._config_template_for(event.action)
        if isinstance(template, str):
            try:
                title = _render_str_template(template, event)
            except (KeyError, ValueError, IndexError) as e:
                _log_template_fallback(self._backend_label(), event.action, e)
                title = (
                    f"{event.user}@{event.machine} {event.action}ed "
                    f"{file_count} {file_word} in {event.project}"
                )
        else:
            title = (
                f"{event.user}@{event.machine} {event.action}ed "
                f"{file_count} {file_word} in {event.project}"
            )

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

    When a per-action template is configured (``teams_template_format``
    in the project YAML), the rendered template REPLACES the
    ``activitySubtitle`` line (the prominent secondary headline under
    the activity title) — the title, theme colour, facts list, and file
    body all stay built-in so the user keeps the structured surface.
    The top-level ``summary`` field also uses the rendered template so
    notification previews surface the templated wording.
    """

    BACKEND_LABEL = "Teams"

    def _format_event(self, event: SyncEvent) -> dict[str, Any]:
        hex_color, _decimal = _color_for(event.action)
        # MessageCard themeColor is a hex string without the `#` prefix.
        theme_color = hex_color.lstrip("#")

        file_count = len(event.files)
        file_word = "file" if file_count == 1 else "files"
        default_summary = (
            f"{event.user}@{event.machine} {event.action}ed "
            f"{file_count} {file_word} in {event.project}"
        )

        template = self._config_template_for(event.action)
        if isinstance(template, str):
            try:
                rendered = _render_str_template(template, event)
                summary = rendered
                activity_subtitle = rendered
            except (KeyError, ValueError, IndexError) as e:
                _log_template_fallback(self._backend_label(), event.action, e)
                summary = default_summary
                activity_subtitle = f"claude-mirror at {event.timestamp}"
        else:
            summary = default_summary
            activity_subtitle = f"claude-mirror at {event.timestamp}"

        files_block = _render_file_lines(event.files)

        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": theme_color,
            "summary": summary,
            "sections": [
                {
                    "activityTitle": default_summary,
                    "activitySubtitle": activity_subtitle,
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
    BACKEND_LABEL = "Generic"

    def _format_event(self, event: SyncEvent) -> dict[str, Any]:
        # Cap files so a 10k-file push doesn't ship a 5 MB envelope to a
        # tiny n8n webhook. We keep ALL files (no truncation marker) up
        # to MAX_FILES_PER_EVENT, since that cap is already enforced at
        # SyncEvent construction time.
        envelope = {
            "version": self.SCHEMA_VERSION,
            "event": event.action,
            "user": event.user,
            "machine": event.machine,
            "project": event.project,
            "files": list(event.files),
            "timestamp": event.timestamp,
        }
        # Generic-webhook templates are STRUCTURED — the configured
        # value is a dict mapping output-key → format string. Rendered
        # values are merged on top of the v1 envelope so the user can
        # add custom fields (`custom_field_1`) AND override existing
        # ones (`user`, `project`, etc.) when their downstream system
        # needs different wording. `version` is intentionally still
        # overridable: a user pinning a downstream consumer to "v2"
        # of their own schema gets to do so via the template.
        template = self._config_template_for(event.action)
        if isinstance(template, dict):
            try:
                rendered = _render_dict_template(template, event)
            except (KeyError, ValueError, IndexError) as e:
                _log_template_fallback(self._backend_label(), event.action, e)
            else:
                envelope.update(rendered)
        return envelope
