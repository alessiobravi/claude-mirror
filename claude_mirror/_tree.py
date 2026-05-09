"""Pure rendering layer for `claude-mirror tree`.

Builds an `rclone tree` / `tree(1)` style ASCII or Unicode view of a
flat backend listing. The rendering is computed entirely from the
`list[dict]` payload that `StorageBackend.list_files_recursive(...)`
already returns — we never call the network here.

Kept in a dedicated module so the rendering can be unit-tested without
spinning up the CLI: every test feeds a hand-built file list into
`render_tree(...)` and asserts on the resulting string.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .snapshots import _human_size


# Box-drawing characters. The ASCII variants are typed as the same
# `str` so the rendering loop can swap whole sets without `Union[...]`
# noise — a missing-glyph terminal can still display the shape.
@dataclass(frozen=True)
class _Glyphs:
    branch: str
    last: str
    pipe: str
    blank: str


_UNICODE = _Glyphs(branch="├── ", last="└── ", pipe="│   ", blank="    ")
_ASCII = _Glyphs(branch="+-- ", last="\\-- ", pipe="|   ", blank="    ")


@dataclass
class _Node:
    name: str
    is_dir: bool
    size: int = 0
    mtime: Optional[str] = None
    children: dict[str, "_Node"] = field(default_factory=dict)


def _build_tree(entries: list[dict[str, Any]]) -> _Node:
    """Synthesise a directory tree from a flat list of files.

    Each entry is the raw `list_files_recursive` dict; we only consult
    `relative_path`, `size`, and `modifiedTime`. Directories have no
    independent listing call — they are inferred from path components,
    which matches how `list_files_recursive` already enumerates.
    """
    root = _Node(name="", is_dir=True)
    for entry in entries:
        rel = entry.get("relative_path", "")
        if not rel:
            continue
        parts = rel.split("/")
        cur = root
        for part in parts[:-1]:
            child = cur.children.get(part)
            if child is None:
                child = _Node(name=part, is_dir=True)
                cur.children[part] = child
            cur = child
        leaf_name = parts[-1]
        size_val = entry.get("size")
        size_int = int(size_val) if isinstance(size_val, (int, float)) else 0
        mtime_val = entry.get("modifiedTime")
        mtime_str = mtime_val if isinstance(mtime_val, str) and mtime_val else None
        cur.children[leaf_name] = _Node(
            name=leaf_name,
            is_dir=False,
            size=size_int,
            mtime=mtime_str,
        )
    return root


def _sort_children(node: _Node) -> list[_Node]:
    """Directories first, then files; alphabetical within each group.

    Matches the `tree(1)` default and what most users expect when
    eyeballing a project listing.
    """
    dirs = sorted(
        (c for c in node.children.values() if c.is_dir),
        key=lambda n: n.name.lower(),
    )
    files = sorted(
        (c for c in node.children.values() if not c.is_dir),
        key=lambda n: n.name.lower(),
    )
    return dirs + files


def _walk_for_subtree(root: _Node, sub_path: str) -> Optional[_Node]:
    """Resolve `sub_path` (slash-separated, relative to `root`).

    Returns the matching node, or None if any segment is missing.
    Empty / "." / "/" inputs return root unchanged so callers can pass
    user input verbatim.
    """
    if not sub_path or sub_path in (".", "/"):
        return root
    parts = [p for p in sub_path.replace("\\", "/").split("/") if p and p != "."]
    cur = root
    for part in parts:
        child = cur.children.get(part)
        if child is None:
            return None
        cur = child
    return cur


def _count_descendant_files(node: _Node) -> tuple[int, int, int]:
    """Return (dir_count, file_count, total_bytes) for `node`'s subtree.

    `node` itself is not counted as a directory — only its descendants.
    """
    dirs = 0
    files = 0
    total = 0
    for child in node.children.values():
        if child.is_dir:
            dirs += 1
            sub_d, sub_f, sub_b = _count_descendant_files(child)
            dirs += sub_d
            files += sub_f
            total += sub_b
        else:
            files += 1
            total += child.size
    return dirs, files, total


def _format_row(
    node: _Node,
    *,
    show_size: bool,
    show_mtime: bool,
) -> str:
    """One node's display string — name first, then optional size/mtime
    columns. Directories get a trailing `/` so they're easy to spot at a
    glance, and they never carry a size column even when one is enabled
    (sizes are aggregated in the footer instead)."""
    label = f"{node.name}/" if node.is_dir else node.name
    parts = [label]
    if show_size and not node.is_dir:
        parts.append(_human_size(node.size))
    if show_mtime and not node.is_dir and node.mtime:
        parts.append(node.mtime)
    return "  ".join(parts) if len(parts) > 1 else parts[0]


def render_tree(
    entries: list[dict[str, Any]],
    *,
    sub_path: str = "",
    depth: Optional[int] = None,
    show_size: bool = True,
    show_mtime: bool = False,
    ascii_only: bool = False,
    root_label: str = ".",
) -> str:
    """Render `entries` as a `tree(1)`-style multi-line string.

    Returns the full output INCLUDING the trailing footer. Caller is
    responsible for printing it.

    Raises `FileNotFoundError` when `sub_path` does not resolve under
    the synthesised tree — the CLI surfaces that as an ENOENT-style
    clean error.
    """
    glyphs = _ASCII if ascii_only else _UNICODE

    full_root = _build_tree(entries)
    target = _walk_for_subtree(full_root, sub_path)
    if target is None:
        raise FileNotFoundError(
            f"path not found in remote listing: {sub_path!r}"
        )

    lines: list[str] = [root_label]
    hidden_files = 0

    def _walk(
        node: _Node,
        prefix: str,
        cur_depth: int,
    ) -> None:
        nonlocal hidden_files
        children = _sort_children(node)
        for idx, child in enumerate(children):
            is_last = idx == len(children) - 1
            connector = glyphs.last if is_last else glyphs.branch
            row = _format_row(
                child, show_size=show_size, show_mtime=show_mtime,
            )
            lines.append(f"{prefix}{connector}{row}")
            if child.is_dir:
                if depth is not None and cur_depth + 1 >= depth:
                    _, sub_files, _ = _count_descendant_files(child)
                    hidden_files += sub_files
                    continue
                next_prefix = prefix + (glyphs.blank if is_last else glyphs.pipe)
                _walk(child, next_prefix, cur_depth + 1)

    _walk(target, prefix="", cur_depth=0)

    if hidden_files > 0:
        lines.append(f"... ({hidden_files} more files in subtrees)")

    dirs, files, total = _count_descendant_files(target)
    footer = f"{dirs} directories, {files} files"
    if files > 0 and show_size:
        footer += f" ({_human_size(total)} total)"
    lines.append("")
    lines.append(footer)
    return "\n".join(lines)
