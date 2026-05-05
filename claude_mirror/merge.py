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


class MergeHandler:
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
        """
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
