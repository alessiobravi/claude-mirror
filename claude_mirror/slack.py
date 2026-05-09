"""Optional Slack webhook integration for sync event notifications."""
from __future__ import annotations

import json
import re
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from .config import Config
from .events import SyncEvent


# Characters that have special meaning in Slack mrkdwn. We strip them from
# remote-controlled fields before interpolation so a malicious collaborator
# can't inject formatting or break out of the message structure. Also strip
# ASCII control chars that could affect rendering or downstream parsers.
_SLACK_MRKDWN_SPECIAL = set("*_~`<>&|")

# Unicode bidi / zero-width / format codepoints that DO NOT render visibly
# but can flip the apparent direction of surrounding text or hide content.
# Stripping these defends against the "🔴 ACTION REQUIRED" framing being
# subverted by an attacker-controlled string that visually rearranges into
# a phishing prompt — e.g. U+202E (RIGHT-TO-LEFT OVERRIDE) reverses runs;
# U+2066-U+2069 (isolates) and U+200E/U+200F (LRM/RLM) have similar effect.
# Zero-width chars (U+200B-U+200D) and BOM (U+FEFF) can hide tokens in URLs.
_UNICODE_BIDI_CONTROLS = (
    set(range(0x200B, 0x2010))      # 200B-200F: ZWSP/ZWNJ/ZWJ/LRM/RLM
    | set(range(0x202A, 0x202F))    # 202A-202E: bidi embeddings + override
    | set(range(0x2066, 0x206A))    # 2066-2069: isolates
    | {0xFEFF}                      # BOM
)

# Bare URLs that Slack would auto-link in mrkdwn (or that any reader could
# click). Used ONLY for the `action` field of failure alerts — phrasing like
# "click here to recover" combined with the 🔴 ACTION REQUIRED framing makes
# this the single high-value phishing vector. Other fields legitimately
# contain filenames / timestamps that aren't URLs.
_BARE_URL_RE = re.compile(
    r"(?i)\b(?:https?|ftp|file|mailto|tel)://\S+"
)

# How many filenames to show in the Slack message body before collapsing
# the rest into a "+N more" sentinel. Keep small enough to avoid blowing
# past Slack's 3000-char block-text limit on big pushes.
_FILES_DISPLAY_LIMIT = 10


def _sanitise_slack(s: str) -> str:
    """Drop Slack mrkdwn metacharacters, ASCII control chars, and Unicode
    bidi/zero-width controls from a user-supplied string before
    interpolating into a Slack message body. Slack's mrkdwn doesn't
    support a portable backslash escape, so removal is the only option
    that doesn't break across clients."""
    if not s:
        return ""
    return "".join(
        ch for ch in s
        if ord(ch) >= 0x20
        and ord(ch) != 0x7f
        and ord(ch) not in _UNICODE_BIDI_CONTROLS
        and ch not in _SLACK_MRKDWN_SPECIAL
    )


def _slack_template_for_action(
    config: Config, action: str,
) -> Optional[str]:
    """Return the Slack template configured for ``action``, or ``None``.

    Reads ``config.slack_template_format`` (a per-action dict added in
    v0.5.50). ``None`` when the user didn't configure templates at all,
    OR when they configured templates but not for this specific action
    (e.g. only ``push`` is templated and a ``sync`` event fires). Both
    cases route the caller to the built-in Slack format.
    """
    templates = getattr(config, "slack_template_format", None)
    if not templates:
        return None
    value = templates.get(action)
    if not isinstance(value, str):
        return None
    return value


def _sanitise_action_text(s: str) -> str:
    """Sanitise the `action` field of a failure_alert.

    Builds on `_sanitise_slack` (mrkdwn + control chars + bidi) and
    additionally strips bare URLs. Slack auto-links bare URLs in
    plain-text fallbacks and in some `mrkdwn` contexts, and the failure
    alert's "ACTION REQUIRED" framing makes any clickable link there
    a high-value phishing vector. Other fields (filenames, snapshot
    timestamps, backend names) don't contain URLs in normal use, so
    this stripping is scoped to `action` only.
    """
    cleaned = _sanitise_slack(s)
    return _BARE_URL_RE.sub("[link removed]", cleaned)


def post_sync_event(
    config: Config,
    event: SyncEvent,
    *,
    snapshot_ts: Optional[str] = None,
    snapshot_format: Optional[str] = None,
    total_project_files: Optional[int] = None,
    backend_status: Optional[dict[str, dict[str, Any]]] = None,
    failure_alert: Optional[dict[str, str]] = None,
) -> None:
    """Post a sync event to the project's Slack webhook with rich formatting.

    No-op if Slack is not enabled for this project.

    Optional kwargs enrich the message:
      snapshot_ts:          if provided, surfaces "Snapshot: <ts>" so the
                            collaborator sees the snapshot was actually
                            captured (and can `claude-mirror restore <ts>`).
      snapshot_format:      `blobs` or `full` — appended to the snapshot line.
      total_project_files:  total tracked files in the project after this
                            event — gives a sense of project size.
      backend_status:       per-backend outcome map for Tier 2 multi-backend
                            pushes. Shape:
                              {backend_name: {
                                  "state": "ok"|"pending"|"failed",
                                  "files_pushed": int,
                                  "files_pending": int,
                                  "snapshot_ts": Optional[str],
                                  "error": Optional[str],
                              }}
                            A "Per-backend status:" section is added when
                            there is more than one entry OR any non-ok state.
      failure_alert:        action-required dict with keys `backend`,
                            `reason`, `action`. Prepends an
                            "ACTION REQUIRED" header block, sets a red
                            attachment sidebar, and replaces the text fallback
                            so push/mobile previews surface the alert.

    When all enrichments are None and `event.files` is empty, falls back to
    the previous single-line text format. Otherwise uses Slack `blocks` for
    rich formatting (header, file list, context line) with a `text`
    fallback for clients/notifications that don't render blocks.

    Uses urllib so there's no extra dependency. Best-effort: any failure
    is silently swallowed — Slack must never break a sync.
    """
    if not config.slack_enabled or not config.slack_webhook_url:
        return

    user    = _sanitise_slack(event.user)
    machine = _sanitise_slack(event.machine)
    project = _sanitise_slack(event.project)
    action  = _sanitise_slack(event.action)

    action_emoji = {
        "push": ":arrow_up:",
        "pull": ":arrow_down:",
        "sync": ":arrows_counterclockwise:",
        "delete": ":wastebasket:",
    }
    emoji = action_emoji.get(event.action, ":bell:")

    file_count = len(event.files)
    file_word  = "file" if file_count == 1 else "files"

    # Plain-text fallback used both as the `text` field (for desktop /
    # mobile push notification previews + non-blocks clients) and as the
    # body when there's no enrichment to show. When a per-action
    # template is configured (`slack_template_format` in project YAML),
    # the rendered template REPLACES the fallback line and the header
    # block's text — every other surface (file list, per-backend status,
    # snapshot context line) keeps its built-in structure.
    rendered_template_text: Optional[str] = None
    template_for_action = _slack_template_for_action(config, event.action)
    if template_for_action is not None and template_for_action != "":
        try:
            from .notifications.webhooks import (
                event_template_vars,
                _log_template_fallback,
            )
            try:
                # Sanitise EACH placeholder value individually so a
                # collaborator-controlled `event.user = "*haxx*"`
                # can't break out of the user's template formatting,
                # while preserving any mrkdwn the user themselves
                # wrote into the template (e.g. `*{user}*`).
                vars_ = event_template_vars(event)
                safe_vars = {
                    k: (_sanitise_slack(v) if isinstance(v, str) else v)
                    for k, v in vars_.items()
                }
                rendered_template_text = template_for_action.format(**safe_vars)
            except (KeyError, ValueError, IndexError) as e:
                _log_template_fallback("Slack", event.action, e)
                rendered_template_text = None
        except Exception:
            # Defensive — if the webhooks module fails to import for
            # any reason, we silently fall back to the built-in format.
            rendered_template_text = None

    if rendered_template_text:
        fallback_text = rendered_template_text
    else:
        fallback_text = (
            f"{emoji} {user}@{machine} {action}ed "
            f"{file_count} {file_word} in {project}"
        )

    blocks: list[dict[str, Any]] = []

    # Action-required alert — prepended at the top so it dominates the
    # message regardless of what else is going on. Kept as a `header`
    # block so Slack renders it prominently.
    if failure_alert:
        alert_blocks, alert_text = _build_failure_alert_blocks(
            failure_alert, machine, backend_status
        )
        blocks.extend(alert_blocks)
        # Replace fallback so mobile / desktop previews lead with the alert
        # instead of the routine "user pushed N files" line.
        fallback_text = alert_text

    # Header — always present. When a template is in play, the rendered
    # plain text replaces the bolded mrkdwn default; the user picks the
    # wording, including any emoji / formatting they want. Mrkdwn
    # metacharacters that came from the user's OWN template string are
    # preserved (the template is THEIR config, not a remote-controlled
    # field), but interpolated event values were sanitised at template
    # render time.
    if rendered_template_text:
        header_md = rendered_template_text
    else:
        header_md = (
            f"{emoji} *{user}@{machine}* {action}ed "
            f"*{file_count} {file_word}* in *{project}*"
        )
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": header_md}}
    )

    # File list — when we have any to show.
    if event.files:
        files_to_show = event.files[:_FILES_DISPLAY_LIMIT]
        lines = "\n".join(
            f"• `{_sanitise_slack(f)}`" for f in files_to_show
        )
        if len(event.files) > _FILES_DISPLAY_LIMIT:
            lines += (
                f"\n_… and {len(event.files) - _FILES_DISPLAY_LIMIT} more_"
            )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Files changed:*\n{lines}"},
        })

    # Per-backend status block — Tier 2 multi-backend visibility. Inserted
    # between the file list and the context line so a reader scans:
    # what changed → which backends accepted it → snapshot/size.
    if backend_status and _should_render_backend_status(backend_status):
        blocks.append(_build_backend_status_block(backend_status))

    # Context line — snapshot confirmation + project size.
    context_elements: list[dict[str, Any]] = []
    if snapshot_ts:
        fmt_label = f" ({_sanitise_slack(snapshot_format)})" if snapshot_format else ""
        context_elements.append({
            "type": "mrkdwn",
            "text": f":camera: Snapshot: `{_sanitise_slack(snapshot_ts)}`{fmt_label}",
        })
    elif event.action in ("push", "sync") and event.files:
        # Push or sync that touched files but didn't snapshot — surface this
        # so the collaborator notices a missing recovery point.
        context_elements.append({
            "type": "mrkdwn",
            "text": ":warning: No snapshot was created for this event",
        })
    if total_project_files is not None:
        context_elements.append({
            "type": "mrkdwn",
            "text": f":books: {total_project_files} files in project",
        })
    if context_elements:
        blocks.append({"type": "context", "elements": context_elements})

    payload: dict[str, Any] = {"text": fallback_text, "blocks": blocks}
    if config.slack_channel:
        payload["channel"] = config.slack_channel

    # Red sidebar via attachments when something needs human attention.
    # Slack still renders top-level `blocks` first; the attachment just
    # paints a vertical danger-coloured bar on the left.
    if failure_alert:
        payload["attachments"] = [{"color": "danger", "blocks": []}]

    _send_webhook(config.slack_webhook_url, payload)


def _should_render_backend_status(backend_status: dict[str, dict[str, Any]]) -> bool:
    """Render the per-backend section if there are multiple backends OR
    any backend reported a non-ok state. A single ok backend looks like
    Tier 1 and adds no signal, so we suppress the block in that case."""
    if len(backend_status) > 1:
        return True
    return any(
        (info or {}).get("state") != "ok" for info in backend_status.values()
    )


def _build_backend_status_block(backend_status: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Render the per-backend status section. State → symbol mapping:
       ok → green, pending → yellow, failed → red.
    Sanitises every backend-controlled string before interpolation."""
    state_symbol = {"ok": "🟢", "pending": "🟡", "failed": "🔴"}
    lines: list[str] = ["*Per-backend status:*"]
    for name, info in backend_status.items():
        info = info or {}
        state = (info.get("state") or "").lower()
        symbol = state_symbol.get(state, "⚪")
        name_s = _sanitise_slack(name)
        if state == "ok":
            pushed = int(info.get("files_pushed") or 0)
            ts = info.get("snapshot_ts")
            ts_part = (
                f", snapshot {_sanitise_slack(ts)}" if ts else ""
            )
            lines.append(f"• {symbol} {name_s} — pushed {pushed}{ts_part}")
        else:
            err = _sanitise_slack(info.get("error") or "")
            pending = int(info.get("files_pending") or 0)
            file_word = "file" if pending == 1 else "files"
            detail_bits: list[str] = []
            if err:
                detail_bits.append(err)
            if pending:
                detail_bits.append(f"{pending} {file_word} pending retry")
            detail = " (".join(detail_bits[:1])
            if len(detail_bits) > 1:
                detail = f"{detail_bits[0]} ({detail_bits[1]})"
            elif not detail:
                detail = state or "unknown"
            lines.append(f"• {symbol} {name_s} — {detail}")
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }


def _build_failure_alert_blocks(
    failure_alert: dict[str, str],
    machine: str,
    backend_status: Optional[dict[str, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], str]:
    """Construct the prepended ACTION REQUIRED header + detail blocks.

    Returns (blocks, text_fallback). The fallback replaces the default
    "user pushed N files" text so mobile previews lead with the alert.
    All backend-controlled fields (`reason`, `action`, `backend`) are
    sanitised — we never trust them to be free of Slack mrkdwn syntax.
    """
    backend = _sanitise_slack(failure_alert.get("backend") or "")
    reason  = _sanitise_slack(failure_alert.get("reason") or "")
    # `action` gets URL-stripping on top of the standard sanitiser — see
    # _sanitise_action_text docstring. The other two fields don't need it.
    action  = _sanitise_action_text(failure_alert.get("action") or "")

    # Count files pending re-push for this backend so the user knows the
    # blast radius before they go fix auth/quota.
    pending_count = 0
    if backend_status and backend in backend_status:
        info = backend_status[backend] or {}
        try:
            pending_count = int(info.get("files_pending") or 0)
        except (TypeError, ValueError):
            pending_count = 0

    pending_line = ""
    if pending_count:
        file_word = "file" if pending_count == 1 else "files"
        pending_line = (
            f"\n{pending_count} {file_word} pending re-push once auth is restored."
        )

    body_md = (
        f"claude-mirror push to *{backend}* failed: `{reason}`\n"
        f"{action} on `{machine}` to recover."
        f"{pending_line}"
    )

    header_block = {
        "type": "header",
        "text": {"type": "plain_text", "text": "🔴 ACTION REQUIRED", "emoji": True},
    }
    detail_block = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": body_md},
    }

    text_fallback = (
        f"🔴 ACTION REQUIRED — claude-mirror push to {backend} failed: "
        f"{reason}. {action}"
    )
    return [header_block, detail_block], text_fallback


def _send_webhook(url: str, payload: dict[str, Any]) -> None:
    """Fire-and-forget POST to a Slack incoming webhook URL."""
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        urlopen(req, timeout=5)
    except (URLError, OSError):
        pass  # best-effort — don't break sync on Slack failure
