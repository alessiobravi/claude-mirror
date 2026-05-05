"""Shared Rich Progress helpers used across CLI commands.

The dual-line phase Progress (one row per phase, left-aligned label,
free-text detail, single shared elapsed timer) is the visual contract
established by push / pull / sync / delete / status. Every claude-mirror
command that does perceptible remote work should adopt it for visual
consistency — see feedback memory `feedback_progress_default.md`.
"""
from __future__ import annotations

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text


class _SharedElapsedColumn(TimeElapsedColumn):
    """Renders the elapsed time only for tasks with ``show_time=True``.

    Use to give a multi-row Progress a single shared timer rather than
    redundant per-row timers — every row in our multi-phase displays
    started at the same moment, so per-row times would always be
    identical.
    """

    def render(self, task):
        if not task.fields.get("show_time"):
            return Text("")
        return super().render(task)


def make_phase_progress(console: Console) -> Progress:
    """Build the multi-phase live Progress used by all top-level commands.

    Each phase is rendered as one row: spinner · description (left-aligned
    fixed width) · live detail · shared elapsed time. ``transient=True``
    so the live region disappears once the ``with`` block exits, leaving
    only the per-line print output above it.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<14}"),
        TextColumn("{task.fields[detail]}", style="dim"),
        _SharedElapsedColumn(),
        console=console,
        transient=True,
    )
