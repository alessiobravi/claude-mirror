"""Local-vs-remote line diff for a single tracked file.

Used by the `claude-mirror diff <path>` command to answer "what changed?"
before deciding whether to push, pull, or merge. Produces a Rich
renderable so the caller can `console.print(...)` it directly.
"""
from __future__ import annotations

import difflib
from typing import Optional

from rich.text import Text


# Bytes are considered binary if they contain a NUL byte in their first
# 8 KiB OR if utf-8 decoding fails. Matches git's heuristic closely
# enough for our purposes — we only diff text files here.
_BINARY_SNIFF_BYTES = 8 * 1024


def is_binary(blob: bytes) -> bool:
    """Heuristic: does this byte sequence look like binary content?

    We only ever diff text files. A NUL byte in the first 8 KiB is the
    classic indicator (text formats don't contain NULs); falling back
    to a utf-8 decode attempt catches latin-1 / utf-16 / etc. as binary
    which is the safe choice — refusing to diff non-utf8 is better than
    rendering garbage.
    """
    if not blob:
        return False
    if b"\x00" in blob[:_BINARY_SNIFF_BYTES]:
        return True
    try:
        blob[:_BINARY_SNIFF_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def render_diff(
    local_bytes: Optional[bytes],
    remote_bytes: Optional[bytes],
    rel_path: str,
    *,
    context_lines: int = 3,
) -> Text:
    """Compute and render a unified diff between local and remote content.

    Either side can be None (file missing on that side). The returned
    Text uses Rich styling: green for additions, red for deletions, dim
    for context, bold cyan for hunk headers, bold for the file header.

    Either / both bytes blobs being None is treated as empty content for
    the diff — the file header makes the situation explicit ("only on
    local" / "only on remote" / "both sides").
    """
    if local_bytes is not None and is_binary(local_bytes):
        return Text(
            f"{rel_path}: local file is binary — refusing to diff "
            f"({len(local_bytes)} bytes).",
            style="yellow",
        )
    if remote_bytes is not None and is_binary(remote_bytes):
        return Text(
            f"{rel_path}: remote file is binary — refusing to diff "
            f"({len(remote_bytes)} bytes).",
            style="yellow",
        )

    if local_bytes is None and remote_bytes is None:
        return Text(f"{rel_path}: not present on either side.", style="yellow")

    local_text  = (local_bytes  or b"").decode("utf-8", errors="replace")
    remote_text = (remote_bytes or b"").decode("utf-8", errors="replace")

    if local_text == remote_text and local_bytes is not None and remote_bytes is not None:
        return Text(
            f"{rel_path}: in sync — local and remote content are identical.",
            style="green",
        )

    # Header line summarises the situation in a single visible row.
    if local_bytes is None:
        header_summary = "only on remote (would be pulled)"
    elif remote_bytes is None:
        header_summary = "only on local (would be pushed)"
    else:
        header_summary = "both sides differ"

    # difflib produces a unified diff with --- / +++ / @@ headers and
    # leading + / - / space markers per content line. We re-style each
    # line for terminal readability rather than emitting raw text.
    local_lines  = local_text.splitlines(keepends=True)
    remote_lines = remote_text.splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        remote_lines,
        local_lines,
        fromfile=f"remote/{rel_path}",
        tofile=f"local/{rel_path}",
        n=context_lines,
    )

    out = Text()
    out.append(f"{rel_path}  ", style="bold")
    out.append(f"({header_summary})\n", style="dim")

    for raw in diff_iter:
        # difflib's lines retain their trailing newline when present;
        # normalise so we always end each rendered row with a single \n.
        line = raw.rstrip("\n")
        if line.startswith("+++") or line.startswith("---"):
            out.append(line + "\n", style="bold")
        elif line.startswith("@@"):
            out.append(line + "\n", style="bold cyan")
        elif line.startswith("+"):
            out.append(line + "\n", style="green")
        elif line.startswith("-"):
            out.append(line + "\n", style="red")
        else:
            out.append(line + "\n", style="dim")

    return out
