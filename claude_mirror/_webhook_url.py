"""Scheme + host validation for outgoing notification webhook URLs.

Every notification backend (Slack, Discord, Microsoft Teams, the Generic
JSON envelope) accepts a free-form URL string from project YAML or the
CLI, and posts to it via stdlib ``urllib.request.urlopen``. urllib's
default opener honours every scheme it knows about — including ``file://``
which would attempt a local-file read, and ``http://169.254.169.254/...``
which would hit the AWS metadata endpoint from a misconfigured cron.

This module centralises the gate. A URL is rejected if its scheme is not
``https``; it's rejected further if a backend specifies a host gate
(Slack only talks to ``hooks.slack.com``, etc.) and the URL's host
doesn't match. The Generic webhook is intentionally https-only with no
host check — that's the entire point of "generic".

The check fires at TWO places:
  * config-load time (`Config.__post_init__`) so a typo / hostile YAML
    fails loudly during `claude-mirror init` rather than silently
    swallowing every notification later;
  * webhook-send time (defence in depth) so a programmatic caller that
    skipped Config.load still can't ship the request.
"""
from __future__ import annotations

from typing import Optional, Sequence
from urllib.parse import urlparse


# Per-backend host allow-lists. We accept exact hostnames and the special
# wildcard ``"*.suffix"`` for domains that issue per-tenant subdomains
# (Microsoft Teams ``{tenant}.webhook.office.com``).
_SLACK_HOSTS: tuple[str, ...] = ("hooks.slack.com",)
_DISCORD_HOSTS: tuple[str, ...] = ("discord.com", "discordapp.com")
# Microsoft Teams: legacy connector + per-tenant Workflows webhook.
_TEAMS_HOSTS: tuple[str, ...] = (
    "outlook.office.com",
    "*.webhook.office.com",
)


def _host_matches(host: str, allowed: Sequence[str]) -> bool:
    """Return True when ``host`` matches any pattern in ``allowed``.

    Patterns are either an exact hostname (``"hooks.slack.com"``) or a
    wildcard suffix (``"*.webhook.office.com"`` matches
    ``"contoso.webhook.office.com"`` but NOT ``"webhook.office.com"``
    itself — the dot before the suffix is required so ``*.foo.com``
    can't match ``evilfoo.com``).
    """
    h = host.lower()
    for pattern in allowed:
        p = pattern.lower()
        if p.startswith("*."):
            suffix = p[1:]  # ".webhook.office.com"
            if h.endswith(suffix) and len(h) > len(suffix):
                return True
        else:
            if h == p:
                return True
    return False


def validate_webhook_url(
    url: str,
    *,
    field_name: str = "webhook_url",
    allowed_hosts: Optional[Sequence[str]] = None,
) -> None:
    """Reject webhook URLs whose scheme is not ``https`` or whose host
    is not in ``allowed_hosts`` (when supplied).

    Empty / falsy URLs are accepted as a no-op so callers don't have to
    pre-filter — an unset webhook URL means "this channel isn't
    configured" everywhere else in the codebase.

    Raises :class:`ValueError` with a message naming ``field_name`` so
    a user with multiple webhook fields knows which one to fix.
    """
    if not url:
        return
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"{field_name}: could not parse URL {url!r} ({exc})"
        ) from exc
    scheme = (parsed.scheme or "").lower()
    if scheme != "https":
        raise ValueError(
            f"{field_name}: URL must use https:// scheme "
            f"(got {scheme or '<empty>'}://). Refusing to send to "
            f"non-https URL — file://, http://, and other schemes can "
            f"target local files or internal endpoints."
        )
    host = parsed.hostname or ""
    if not host:
        raise ValueError(
            f"{field_name}: URL is missing a hostname ({url!r})."
        )
    if allowed_hosts is not None and not _host_matches(host, allowed_hosts):
        raise ValueError(
            f"{field_name}: host {host!r} is not in the allow-list for "
            f"this webhook backend (expected any of {list(allowed_hosts)})."
        )


def validate_slack_webhook_url(url: str, *, field_name: str = "slack_webhook_url") -> None:
    """Slack incoming webhooks always live at ``hooks.slack.com``."""
    validate_webhook_url(url, field_name=field_name, allowed_hosts=_SLACK_HOSTS)


def validate_discord_webhook_url(
    url: str, *, field_name: str = "discord_webhook_url",
) -> None:
    """Discord webhooks live at ``discord.com`` (or the legacy
    ``discordapp.com`` redirect)."""
    validate_webhook_url(url, field_name=field_name, allowed_hosts=_DISCORD_HOSTS)


def validate_teams_webhook_url(
    url: str, *, field_name: str = "teams_webhook_url",
) -> None:
    """Microsoft Teams accepts the legacy connector at
    ``outlook.office.com`` AND any per-tenant ``*.webhook.office.com``
    subdomain."""
    validate_webhook_url(url, field_name=field_name, allowed_hosts=_TEAMS_HOSTS)


def validate_generic_webhook_url(
    url: str, *, field_name: str = "webhook_url",
) -> None:
    """The generic webhook accepts ANY https host — that's the point of
    "generic". We still reject non-https schemes to defeat ``file://``
    / ``http://localhost:6379/`` style abuse."""
    validate_webhook_url(url, field_name=field_name, allowed_hosts=None)
