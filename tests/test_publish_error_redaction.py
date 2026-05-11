"""Regression: the three `console.print(f"...: {e}")` warning sites in
`sync.py::_publish_event` / `_flush_remote_log` / `_flush_publishes` must
route exception bodies through `redact_error()` so a googleapiclient
`HttpError` carrying Google's HTML 400 page can't dump that HTML wall
into the user's terminal.

Without `redact_error()` the raw HTML body lands verbatim (~3 KiB of
`<!DOCTYPE html>...` per warning). With `redact_error()` the message is
truncated to ~160 chars and known credential prefixes are stripped.
"""
from __future__ import annotations

from io import StringIO
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from claude_mirror import sync as sync_mod
from claude_mirror.events import SyncEvent


_GOOGLE_400_HTML = (
    "<!DOCTYPE html>\n<html lang=en>\n  <meta charset=utf-8>\n"
    + "<title>Error 400 (Bad Request)!!1</title>\n"
    + "<style>" + ("a" * 2000) + "</style>\n"
    + "<a href=//www.google.com/><span id=logo aria-label=Google></span></a>\n"
    + "<p><b>400.</b> <ins>That's an error.</ins>"
    + "<p>Your client has issued a malformed or illegal request."
    + "  <ins>That's all we know.</ins>\n"
)


def _make_engine_with_failing_notifier(exc: Exception) -> Any:
    """Build a minimal mock that has enough surface for `_publish_event`."""
    engine = MagicMock(spec=sync_mod.SyncEngine)
    engine.config = MagicMock()
    engine.config.machine_name = "test-machine"
    engine.config.user = "tester"
    engine._project = MagicMock()
    engine._project.name = "myproject"
    engine._mirrors = []
    engine._append_to_drive_log = MagicMock()
    engine._build_backend_status = MagicMock(return_value=None)
    engine._pending_publish_futures = []

    failing_notifier = MagicMock()
    failing_notifier.publish_event_async.side_effect = exc
    engine.notifier = failing_notifier

    return engine


def _captured_print(engine: Any, exc: Exception) -> str:
    """Run `_publish_event` against the engine, capturing console output."""
    buf = StringIO()
    saved_console = sync_mod.console
    sync_mod.console = Console(file=buf, force_terminal=False, no_color=True, width=400)
    try:
        sync_mod.SyncEngine._publish_event(engine, ["a.md"], "push")
    finally:
        sync_mod.console = saved_console
    return buf.getvalue()


def test_publish_event_truncates_html_response_body() -> None:
    """A raw Google 400 HTML body must be capped — without `redact_error()`
    the whole ~3 KiB HTML wall lands in the user's terminal. The first 14
    chars (`<!DOCTYPE html>`) survive the 160-char cap, but the full body
    must not. The load-bearing assertions are on total output size and on
    the absence of content that's past the truncation point.
    """
    output = _captured_print(
        _make_engine_with_failing_notifier(Exception(_GOOGLE_400_HTML)),
        exc=Exception(_GOOGLE_400_HTML),
    )
    assert "Warning: could not publish event:" in output
    # Tail-of-HTML markers (well past char 160) must NOT appear:
    assert "</html>" not in output, "trailing </html> leaked past truncation"
    assert "That's an error" not in output, "mid-body HTML leaked past truncation"
    assert "Your client has issued" not in output
    # Output length: warning prefix (~40) + redacted body (≤160) + Rich
    # newlines/styling = under ~400 chars. Without redact_error the output
    # is ~3 KiB. Setting a generous 600-char ceiling so cosmetic Rich
    # changes don't flake the test.
    assert len(output) < 600, (
        f"warning line exploded to {len(output)} chars; redact_error caps "
        f"the exception body at ~160"
    )


def test_publish_event_redacts_does_not_leak_aaaaa_filler() -> None:
    """The 2 KiB of inline-style `aaaa…` in the Google 400 page is the
    actual marker of a leak — assert it's gone."""
    output = _captured_print(
        _make_engine_with_failing_notifier(Exception(_GOOGLE_400_HTML)),
        exc=Exception(_GOOGLE_400_HTML),
    )
    assert "a" * 200 not in output, "long filler from HTML leaked"


def test_publish_event_short_error_still_visible() -> None:
    """Don't over-truncate — a short error message must still be readable."""
    output = _captured_print(
        _make_engine_with_failing_notifier(Exception("connection refused")),
        exc=Exception("connection refused"),
    )
    assert "Warning: could not publish event:" in output
    assert "connection refused" in output
