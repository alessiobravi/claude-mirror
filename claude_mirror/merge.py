from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import click
from rich.console import Console
from rich.syntax import Syntax
from rich.panel import Panel

console = Console(force_terminal=True)

CONFLICT_LOCAL = "L"
CONFLICT_DRIVE = "D"
CONFLICT_EDIT = "E"
CONFLICT_SKIP = "S"

# Strategy values accepted by `MergeHandler(non_interactive_strategy=...)`
# and surfaced on the CLI as `claude-mirror sync --strategy ...`.
# Centralising the literals here means the CLI flag, the engine wiring,
# the auto-resolution audit log, and the test suite cannot drift.
STRATEGY_KEEP_LOCAL = "keep-local"
STRATEGY_KEEP_REMOTE = "keep-remote"
NON_INTERACTIVE_STRATEGIES = (STRATEGY_KEEP_LOCAL, STRATEGY_KEEP_REMOTE)


class MergeHandler:
    """Resolve sync conflicts.

    Default (interactive) mode prompts the user with [L]ocal / [D]rive /
    [E]ditor / [S]kip exactly as before — backward compatible with every
    existing caller that constructs `MergeHandler()` with no arguments.

    When `non_interactive_strategy` is set (one of NON_INTERACTIVE_STRATEGIES),
    `resolve_conflict` returns the canned answer without ever calling
    `click.prompt` or rendering the diff panels. This is the cron-friendly
    path that lets `claude-mirror sync --no-prompt --strategy keep-local`
    run unattended under launchd / systemd / cron without blocking on a
    TTY that doesn't exist.
    """

    def __init__(self, *, non_interactive_strategy: str | None = None) -> None:
        if non_interactive_strategy is not None and non_interactive_strategy not in NON_INTERACTIVE_STRATEGIES:
            # Defensive: the CLI uses click.Choice so this branch is never
            # hit from the command line, but a programmatic caller could
            # pass a typo. Fail fast rather than silently fall through to
            # the interactive path under cron (which would hang forever).
            raise ValueError(
                f"non_interactive_strategy must be one of "
                f"{NON_INTERACTIVE_STRATEGIES}, got {non_interactive_strategy!r}"
            )
        self.non_interactive_strategy = non_interactive_strategy

    def show_diff(self, rel_path: str, local_content: str, drive_content: str) -> None:
        console.print(f"\n[bold yellow]Conflict in:[/] {rel_path}\n")
        console.print(Panel(
            Syntax(local_content, "markdown", theme="monokai"),
            title="[green]LOCAL[/]",
            border_style="green",
        ))
        console.print(Panel(
            Syntax(drive_content, "markdown", theme="monokai"),
            title="[blue]DRIVE[/]",
            border_style="blue",
        ))

    def resolve_conflict(
        self, rel_path: str, local_content: str, drive_content: str
    ) -> tuple[str, str] | None:
        """
        Returns (resolved_content, winner) or None if skipped.
        winner is 'local', 'drive', or 'merged'.

        When `non_interactive_strategy` is set on the handler, the canned
        policy resolution is returned and no diff/prompt is rendered —
        designed for cron / unattended sync. The interactive path is
        otherwise unchanged.
        """
        if self.non_interactive_strategy is not None:
            return self._policy_resolution(local_content, drive_content)

        self.show_diff(rel_path, local_content, drive_content)
        console.print(
            "\n[bold]How do you want to resolve this conflict?[/]\n"
            "  [green][L][/] Keep local\n"
            "  [blue][D][/] Keep drive\n"
            "  [yellow][E][/] Open in editor (merge manually)\n"
            "  [dim][S][/] Skip\n"
        )
        choice = click.prompt(
            "Choice", type=click.Choice(["L", "l", "D", "d", "E", "e", "S", "s"]),
            show_choices=False,
        ).upper()

        if choice == CONFLICT_LOCAL:
            return local_content, "local"
        elif choice == CONFLICT_DRIVE:
            return drive_content, "drive"
        elif choice == CONFLICT_EDIT:
            return self._open_in_editor(rel_path, local_content, drive_content), "merged"
        else:
            return None

    def _policy_resolution(
        self, local_content: str, drive_content: str
    ) -> tuple[str, str]:
        """Map a non-interactive strategy to (content, winner).

        keep-local  → local content wins, gets pushed to remote.
        keep-remote → remote content wins, overwrites local.

        Future strategies (`merge-via-git`, `keep-newer`) plug in here.
        """
        if self.non_interactive_strategy == STRATEGY_KEEP_LOCAL:
            return local_content, "local"
        if self.non_interactive_strategy == STRATEGY_KEEP_REMOTE:
            return drive_content, "drive"
        # Defensive — __init__ already rejects unknown strategies.
        raise ValueError(
            f"Unknown non_interactive_strategy: {self.non_interactive_strategy!r}"
        )

    def _open_in_editor(
        self, rel_path: str, local_content: str, drive_content: str
    ) -> str:
        conflict_content = (
            f"<<<<<<< LOCAL\n"
            f"{local_content.rstrip()}\n"
            f"=======\n"
            f"{drive_content.rstrip()}\n"
            f">>>>>>> DRIVE\n"
        )
        suffix = Path(rel_path).suffix or ".md"
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=suffix, prefix="claude_mirror_merge_", delete=False
        ) as tmp:
            tmp.write(conflict_content)
            tmp_path = tmp.name

        editor = os.environ.get("EDITOR", os.environ.get("VISUAL", "vi"))
        try:
            subprocess.run([editor, tmp_path], check=True)
            resolved = Path(tmp_path).read_text()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if "<<<<<<< LOCAL" in resolved:
            console.print(
                "[yellow]Warning: conflict markers still present. Saving as-is.[/]"
            )
        return resolved
