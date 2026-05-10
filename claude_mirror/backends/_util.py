"""Shared backend utilities."""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path


# Windows drive-letter prefix detector (e.g. ``C:``, ``z:`` — single
# letter + colon at the start of a path component). We reject any
# server-returned path that includes one because every callsite composes
# the path against the project root via `_safe_join`, and a drive-letter
# prefix would slip past Path arithmetic on Windows.
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def validate_server_rel_path(rel_path: str, *, backend_name: str) -> str:
    """Reject server-returned relative paths that look like traversal.

    A trustworthy server returns paths like ``"memory/foo.md"`` or
    ``"CLAUDE.md"``. A hostile / buggy server can return ``"../../etc/
    passwd"`` (WebDAV PROPFIND ``<href>``), ``"/etc/passwd"`` (S3
    ``Key:``), ``"\\..\\.."`` (Windows-shaped traversal), or a
    NUL-byte-containing path that some downstream callers treat
    specially. Every rejection raises ``BackendError(FILE_REJECTED)``
    so the orchestrator skips the offending entry without aborting the
    rest of the listing.

    Returns the path unchanged on success — the helper is meant to be
    used as a "tag" right before unpacking the listing dict, so the
    final code reads naturally::

        results.append({
            "relative_path": validate_server_rel_path(rel, backend_name="webdav"),
            ...
        })

    Rejection criteria (all are treated as defence-in-depth):
      * empty string;
      * leading ``/`` or ``\\`` — would be an absolute path;
      * any ``..`` segment after splitting on both separators;
      * NUL byte (``\\x00``) anywhere in the path;
      * a Windows drive-letter prefix (``C:``, ``z:`` etc.).
    """
    # Local import keeps this module's global namespace clean and avoids
    # a circular dependency on the package init at import time.
    from . import BackendError, ErrorClass

    def _reject(reason: str) -> "BackendError":
        return BackendError(
            ErrorClass.FILE_REJECTED,
            f"{backend_name}: server returned suspicious path "
            f"{rel_path!r} ({reason})",
            backend_name=backend_name,
        )

    if not isinstance(rel_path, str) or not rel_path:
        raise _reject("empty or non-string")
    if "\x00" in rel_path:
        raise _reject("contains NUL byte")
    if rel_path.startswith("/") or rel_path.startswith("\\"):
        raise _reject("absolute path")
    if _WIN_DRIVE_RE.match(rel_path):
        raise _reject("Windows drive-letter prefix")
    # Split on BOTH separators so a Windows-shaped traversal segment
    # (``..\\..\\etc``) gets caught when the rest of the codebase
    # normalises on POSIX-style joins.
    for part in rel_path.replace("\\", "/").split("/"):
        if part == "..":
            raise _reject("contains parent-directory segment")
    return rel_path


def write_token_secure(path: Path, content: str) -> None:
    """Write a credentials/token file with 0600 permissions (owner only).

    Defends against the default umask (commonly 0022) creating world-readable
    token files. Used by every backend for token persistence.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
    except Exception:
        os.close(fd)
        raise
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
