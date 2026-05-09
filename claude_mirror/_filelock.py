"""Cross-platform exclusive file locking for the notification inbox.

POSIX uses `fcntl.flock(LOCK_EX)`, which is what the inbox code shipped with
through v0.5.58. On Windows `fcntl` doesn't exist; we use `msvcrt.locking`
with `LK_LOCK`, which blocks (retrying every ~1s up to ~10s) until the lock
is available. Behaviour is otherwise the same: callers see a context manager
that holds an exclusive lock for the duration of the `with` block and
releases it on exit, whether the block exits normally or via an exception.

Quirk worth knowing: `msvcrt.locking` locks N bytes from the current file
pointer position rather than the whole file. We seek to byte 0, lock a
sentinel range of `IO_LOCK_BYTES` bytes, then restore the previous file
position before yielding to the caller. The position is restored again
before unlocking on exit so the unlock targets the same byte range.
"""
from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import IO, Any, Iterator

# Maximum value documented for `nbytes` in `msvcrt.locking`. We lock a
# sentinel range starting at byte 0; this serializes any caller that uses
# the same helper, regardless of where the actual reads / writes happen.
IO_LOCK_BYTES = 0x7FFFFFFF


if sys.platform == "win32":  # pragma: no cover - platform-specific branch
    import msvcrt

    @contextmanager
    def exclusive_lock(file_obj: IO[Any]) -> Iterator[None]:
        fd = file_obj.fileno()
        prev_pos = file_obj.tell()
        file_obj.seek(0)
        msvcrt.locking(fd, msvcrt.LK_LOCK, IO_LOCK_BYTES)
        file_obj.seek(prev_pos)
        try:
            yield
        finally:
            file_obj.seek(0)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, IO_LOCK_BYTES)
            finally:
                file_obj.seek(prev_pos)
else:
    import fcntl

    @contextmanager
    def exclusive_lock(file_obj: IO[Any]) -> Iterator[None]:
        fd = file_obj.fileno()
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
