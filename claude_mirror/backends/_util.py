"""Shared backend utilities."""
from __future__ import annotations

import os
import stat
from pathlib import Path


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
