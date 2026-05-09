"""Pure-data layer + thin curses wrapper for `claude-mirror ncdu`.

The pure-data layer (`SizeNode`, `build_size_tree`, `top_n_paths`,
`format_non_interactive`) has no curses, no I/O, no rendering — it
turns a flat backend listing into a directory-aggregate tree and
answers "what's the biggest thing in here?" questions. It is the
unit-tested surface; ~95% of the feature's logic lives here.

`run_curses_ui(root)` is the thin wrapper that takes a built tree and
runs the interactive event loop. Validation path: manual smoke-test
in a real terminal (`claude-mirror ncdu`). It is NOT unit-tested
because curses requires a real TTY — headless CI runners that import
this module must NOT instantiate curses, only the data layer.

Windows note: `curses` is not in the CPython stdlib on Windows. The
CLI gates `claude-mirror ncdu` to POSIX only and prints a friendly
hint pointing Windows users at `claude-mirror tree --depth N` (the
read-only tree view).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


# Cap on input listing length. A pathologically large remote (millions
# of files) would still build a tree in linear time, but the curses UI
# doesn't degrade gracefully past tens of thousands of children at one
# level. The cap applies to the FLAT input listing — it's a soft
# upper-bound on the work the data layer does, not a hard limit on
# legal input. Callers above the cap should consider --top N first.
MAX_LISTING_ENTRIES = 1_000_000


@dataclass
class SizeNode:
    """One node in the directory-aggregate tree.

    `size` and `file_count` are AGGREGATES — for a directory node they
    sum every descendant file. For a file node, `size` is the file's
    own size and `file_count` is 1.

    `children` is keyed by the child's `name` (the last path segment),
    which keeps the tree topology stable across re-walks even if the
    backend's listing order varies.

    `path` is the rel-path from the tree root, with `/` separators and
    no leading slash. The root itself has `path == ""`.
    """

    name: str
    path: str
    is_file: bool
    size: int = 0
    file_count: int = 0
    children: dict[str, "SizeNode"] = field(default_factory=dict)

    def add_file(self, parts: list[str], size: int) -> None:
        """Insert a file at `parts` (path components from this node's
        perspective) carrying `size` bytes. Synthesises intermediate
        directory nodes as needed and bubbles size + file_count up to
        every ancestor (this node included)."""
        if not parts:
            return
        self.size += size
        self.file_count += 1
        head = parts[0]
        rest = parts[1:]
        child_path = f"{self.path}/{head}" if self.path else head
        existing = self.children.get(head)
        if not rest:
            if existing is None:
                self.children[head] = SizeNode(
                    name=head,
                    path=child_path,
                    is_file=True,
                    size=size,
                    file_count=1,
                )
            else:
                # Same rel_path appearing twice in the listing — last
                # one wins on size (matches what users would see if a
                # file mutated mid-walk).
                existing.size = size
                existing.file_count = 1
                existing.is_file = True
            return
        if existing is None:
            existing = SizeNode(
                name=head,
                path=child_path,
                is_file=False,
            )
            self.children[head] = existing
        existing.add_file(rest, size)

    def sorted_children(self) -> list["SizeNode"]:
        """Children in size-desc order, ties broken by name asc.
        Used by both the curses UI (per-level rendering) and the
        non-interactive top-N formatter (whole-tree flattening)."""
        return sorted(
            self.children.values(),
            key=lambda c: (-c.size, c.name),
        )


def _split_rel_path(rel_path: str) -> list[str]:
    """Split a rel_path the way the backends report it: forward
    slashes, no leading/trailing slash. Strip empties from doubled
    slashes; reject NUL bytes outright (the listing is supposed to be
    sanitised upstream — if a NUL slips through, fail loud rather
    than silently coerce).

    Backslashes are NOT treated as separators: backends serialise as
    `/`, and a literal backslash in a name is a legal name character
    on POSIX filesystems.
    """
    if "\x00" in rel_path:
        raise ValueError("rel_path contains NUL byte")
    parts = [p for p in rel_path.split("/") if p]
    return parts


def build_size_tree(
    entries: Iterable[tuple[str, int]],
    *,
    root_name: str = "",
) -> SizeNode:
    """Build a `SizeNode` tree from a flat iterable of `(rel_path,
    size)` tuples.

    The shape is the one the backends already return — list_files_recursive
    yields dicts with `relative_path` and `size` keys, and the CLI
    layer adapts those into `(rel, size)` tuples before calling here.
    Pure-data tests pass tuples directly.

    Empty listing → a root with zero children, zero size, zero count.
    Files with the same rel_path appearing twice — the second wins
    (defensive, matches a mid-walk mutation; in practice backends
    don't return duplicates).
    """
    root = SizeNode(name=root_name, path="", is_file=False)
    count = 0
    for rel_path, size in entries:
        count += 1
        if count > MAX_LISTING_ENTRIES:
            raise ValueError(
                f"listing exceeds MAX_LISTING_ENTRIES "
                f"({MAX_LISTING_ENTRIES}); try `--non-interactive --top N`"
            )
        if not rel_path:
            continue
        parts = _split_rel_path(rel_path)
        if not parts:
            continue
        root.add_file(parts, int(size))
    return root


def _walk_descendants(node: SizeNode) -> Iterable[SizeNode]:
    """Yield every descendant of `node` (NOT including `node` itself)
    in pre-order. Used by `top_n_paths` to flatten the tree."""
    for child in node.children.values():
        yield child
        if not child.is_file:
            yield from _walk_descendants(child)


def top_n_paths(root: SizeNode, n: int) -> list[SizeNode]:
    """Return the `n` largest aggregate paths under `root`, in
    size-desc order (ties broken by path asc).

    "Aggregate" matters: a directory node ranks by the sum of every
    descendant file. So `docs/` with 100 small files outranks one
    big binary at root if their summed sizes call it that way. This
    matches what `du -sh` does and what users expect from `ncdu` /
    `rclone ncdu` non-interactive top-N reports.

    Both directory nodes AND file nodes participate in the ranking,
    same as `ncdu`'s `-o` output. A directory with one big file in
    it will appear adjacent to that file in the listing — that's
    the expected duplication; the directory's row carries the
    file_count > 1 cue when there are siblings.

    `n <= 0` returns `[]`.
    """
    if n <= 0:
        return []
    flat = list(_walk_descendants(root))
    flat.sort(key=lambda node: (-node.size, node.path))
    return flat[:n]


# ──────────────────────────────────────────────────────────────────────────
# Human-readable formatting
# ──────────────────────────────────────────────────────────────────────────


def human_size(n: int) -> str:
    """Render a byte count as `<num> B/KB/MB/GB/TB`. Mirrors
    snapshots._human_size; kept local so the data layer has zero
    intra-package imports beyond stdlib."""
    if n < 1024:
        return f"{n} B"
    size: float = float(n)
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
    return f"{size:.1f} TB"


def format_non_interactive(
    root: SizeNode,
    n: int,
    *,
    backend_label: str = "primary",
) -> str:
    """Format the top-N report as plain text for stdout.

    Output shape is fixed (so cron / CI scripts can string-grep on
    it):

        Top N largest paths in BACKEND_LABEL backend:

          size      count   path
           45.2 MB    127   docs/
           ...
           total: 67.4 MB across 245 files

    A directory path is suffixed with `/`; a file path has no suffix.
    `count` is `1` for files. The total line aggregates the WHOLE
    tree, not just the displayed top-N — so a small project's total
    matches `du -sh` on the local copy.
    """
    rows = top_n_paths(root, n)
    if not rows:
        # Empty remote — say so explicitly rather than print a header
        # with zero rows under it.
        return (
            f"No files in {backend_label} backend.\n"
            f"  total: 0 B across 0 files\n"
        )
    header = f"Top {n} largest paths in {backend_label} backend:\n"
    lines = [
        header,
        "  size      count   path",
    ]
    for node in rows:
        size_str = human_size(node.size)
        path = node.path + ("/" if not node.is_file else "")
        lines.append(
            f"  {size_str:>9} {node.file_count:>6}   {path}"
        )
    lines.append(
        f"  total: {human_size(root.size)} across {root.file_count} files"
    )
    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────────────────────────────────
# Curses TUI — thin wrapper, manual smoke-test only.
# ──────────────────────────────────────────────────────────────────────────


def run_curses_ui(
    root: SizeNode,
    *,
    project_label: str = "",
    backend_label: str = "primary",
) -> None:
    """Run the interactive curses event loop against an already-built
    tree. Returns when the user presses `q`.

    This function is NOT unit-tested — curses needs a real terminal.
    Validation: manually run `claude-mirror ncdu` and exercise every
    keybinding (arrows, Enter, Backspace, h, q, terminal resize).

    Keybindings:
        ↑ / ↓        — move cursor
        Enter / →    — descend into selected directory
        ← / Backspace / h — ascend to parent
        q            — quit

    Layout:
        top bar : `--- claude-mirror: PROJECT (BACKEND) ---`
        body    : children sorted by size-desc, format
                  `<size>  <bar>  <name>` with the cursor-row
                  reverse-highlighted
        bot bar : aggregate size + child count + current path
    """
    import curses

    def _ui(stdscr: Any) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stack: list[SizeNode] = [root]
        cursor: list[int] = [0]

        def current() -> SizeNode:
            return stack[-1]

        def descend() -> None:
            kids = current().sorted_children()
            if not kids:
                return
            target = kids[cursor[-1]]
            if target.is_file:
                return
            stack.append(target)
            cursor.append(0)

        def ascend() -> None:
            if len(stack) > 1:
                stack.pop()
                cursor.pop()

        while True:
            stdscr.erase()
            try:
                rows, cols = stdscr.getmaxyx()
            except curses.error:
                rows, cols = 24, 80
            node = current()
            kids = node.sorted_children()
            top = (
                f"--- claude-mirror: "
                f"{project_label or '(no project)'} "
                f"({backend_label}) ---"
            )
            try:
                stdscr.addnstr(0, 0, top.ljust(cols)[:cols], cols, curses.A_REVERSE)
            except curses.error:
                pass

            body_rows = max(rows - 2, 1)
            if cursor[-1] >= len(kids):
                cursor[-1] = max(len(kids) - 1, 0)
            offset = 0
            if cursor[-1] >= body_rows:
                offset = cursor[-1] - body_rows + 1
            largest = kids[0].size if kids and kids[0].size > 0 else 0
            bar_width = max(min(20, cols // 4), 4)

            for i, child in enumerate(kids[offset:offset + body_rows]):
                idx = i + offset
                size_str = human_size(child.size).rjust(9)
                if largest > 0:
                    fill = max(int(round(bar_width * child.size / largest)), 0)
                else:
                    fill = 0
                bar = "*" * fill + " " * (bar_width - fill)
                suffix = "/" if not child.is_file else ""
                line = f"{size_str}  [{bar}]  {child.name}{suffix}"
                attr = curses.A_REVERSE if idx == cursor[-1] else curses.A_NORMAL
                try:
                    stdscr.addnstr(i + 1, 0, line.ljust(cols)[:cols], cols, attr)
                except curses.error:
                    pass

            if not kids:
                empty_msg = "(empty directory — press ← to go back, q to quit)"
                try:
                    stdscr.addnstr(1, 0, empty_msg[:cols], cols)
                except curses.error:
                    pass

            crumb = "/" + node.path if node.path else "/"
            bot = (
                f" {human_size(node.size)}  "
                f"{node.file_count} file(s)  {crumb}"
            )
            try:
                stdscr.addnstr(rows - 1, 0, bot.ljust(cols)[:cols], cols, curses.A_REVERSE)
            except curses.error:
                pass

            stdscr.refresh()
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                return
            if key in (ord("q"), ord("Q")):
                return
            if key == curses.KEY_UP and cursor[-1] > 0:
                cursor[-1] -= 1
            elif key == curses.KEY_DOWN and cursor[-1] < len(kids) - 1:
                cursor[-1] += 1
            elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
                descend()
            elif key in (curses.KEY_LEFT, curses.KEY_BACKSPACE, 127, 8, ord("h")):
                ascend()
            elif key == curses.KEY_RESIZE:
                continue

    import curses as _curses
    _curses.wrapper(_ui)


# ──────────────────────────────────────────────────────────────────────────
# CLI adapter — turns a backend listing into the data-layer's input
# shape. Kept here rather than in cli.py so cli.py stays focused on
# Click wiring.
# ──────────────────────────────────────────────────────────────────────────


def entries_from_backend_listing(
    listing: Iterable[dict[str, Any]],
) -> Iterable[tuple[str, int]]:
    """Adapt the backend listing's native shape (`{"relative_path":
    str, "size": int, ...}`) to the data layer's `(rel, size)` input.

    Files without a `relative_path` are skipped (defensive — the
    backends always set it, but a future backend bug shouldn't take
    down the ncdu UI). Files without a `size` key are treated as 0.
    Non-int sizes are coerced via `int()`; if that fails, the entry
    is skipped.
    """
    for entry in listing:
        rel = entry.get("relative_path", "")
        if not rel:
            continue
        raw_size = entry.get("size", 0)
        try:
            size = int(raw_size) if raw_size is not None else 0
        except (TypeError, ValueError):
            continue
        yield rel, size
