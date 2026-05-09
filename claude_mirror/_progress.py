"""Shared Rich Progress helpers used across CLI commands.

The dual-line phase Progress (one row per phase, left-aligned label,
free-text detail, single shared elapsed timer) is the visual contract
established by push / pull / sync / delete / status. Every claude-mirror
command that does perceptible remote work should adopt it for visual
consistency — see feedback memory `feedback_progress_default.md`.

The transfer Progress (`make_transfer_progress`) extends that contract
with a real progress bar, "5.3/12 MB" download column, transfer rate,
and an ETA column for phases that move file bytes (push / pull / sync /
seed-mirror). Both factories coexist — non-byte-total phases (status
counting, snapshot creation) keep using `make_phase_progress`.

Thread-safety: Rich's Progress is documented as thread-safe — multiple
worker threads may call `progress.advance(...)` against the same task
concurrently. We deliberately do NOT add a redundant `threading.Lock`
around `advance()` calls in the parallel push/pull paths; doing so
would only re-establish a guarantee Rich already provides.
"""
from __future__ import annotations

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    Task,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text


class _SharedElapsedColumn(TimeElapsedColumn):
    """Renders the elapsed time only for tasks with ``show_time=True``.

    Use to give a multi-row Progress a single shared timer rather than
    redundant per-row timers — every row in our multi-phase displays
    started at the same moment, so per-row times would always be
    identical.
    """

    def render(self, task: "Task") -> Text:
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


def make_transfer_progress(console: Console) -> Progress:
    """Build a Progress that renders ETA + bytes/sec + cumulative bytes.

    Used by phases that actually transfer file bytes (Pushing, Pulling,
    Seeding) so the user sees an actual progress bar with running rate
    and time-remaining instead of just a spinner. Each task's `total`
    must be expressed in bytes; callers `advance(N)` with the number of
    bytes pushed since the last update.

    Layout::

        [bold]Pushing      [/]  [bar fills 30%][----]  5.3/12 MB  •  780 kB/s  •  0:00:11  •  0:00:42 remaining

    `transient=True` mirrors `make_phase_progress` — the live region
    cleans up on exit so the surrounding command output stays tidy.

    Rich's column set:

      * `BarColumn` — visible bar fill.
      * `DownloadColumn(binary_units=True)` — "5.3/12.0 MB" with binary
        prefixes so figures match what `ls -h` and friends report.
      * `TransferSpeedColumn` — "780 kB/s".
      * `_SharedElapsedColumn` — single shared elapsed timer (rendered
        only on the row carrying `show_time=True`), matching the
        phase-progress contract.
      * `TimeRemainingColumn` — "0:00:42 remaining".

    Non-tty mode: Rich auto-detects when stdout is not a tty and emits
    a plainer rendering — no ANSI cursor sequences, no live refresh.
    `--json` mode silences progress entirely via `_NoOpProgressCtx`
    (see cli.py); the new factory does not need a special-case there.
    """
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<14}"),
        BarColumn(),
        DownloadColumn(binary_units=True),
        TextColumn("•", style="dim"),
        TransferSpeedColumn(),
        TextColumn("•", style="dim"),
        _SharedElapsedColumn(),
        TextColumn("•", style="dim"),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    )
