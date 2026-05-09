"""Cross-platform exclusive file lock contract tests.

These pin the behaviour the inbox code in `claude_mirror.notifier` relies on:
two threads contending for the same lock cannot interleave their critical
sections, the lock is released even if the protected block raises, the file
position is preserved across the lock cycle (a Windows-specific quirk that
must not regress on POSIX either), and the helper accepts both binary and
text-mode file handles.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from claude_mirror._filelock import exclusive_lock


def test_exclusive_lock_serializes_concurrent_threads(tmp_path: Path) -> None:
    target = tmp_path / "lock.bin"
    target.write_bytes(b"")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(marker: bytes) -> None:
        try:
            with target.open("r+b") as f:
                barrier.wait(timeout=2)
                with exclusive_lock(f):
                    f.seek(0, 2)
                    pos = f.tell()
                    f.write(marker)
                    f.flush()
                    f.seek(0, 2)
                    assert f.tell() == pos + len(marker)
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=(b"A",))
    t2 = threading.Thread(target=worker, args=(b"B",))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not errors, f"worker raised: {errors!r}"
    contents = target.read_bytes()
    assert sorted(contents) == [ord("A"), ord("B")]
    assert len(contents) == 2


def test_exclusive_lock_releases_on_exception(tmp_path: Path) -> None:
    target = tmp_path / "lock.bin"
    target.write_bytes(b"")

    with target.open("r+b") as f1:
        with pytest.raises(RuntimeError, match="boom"):
            with exclusive_lock(f1):
                raise RuntimeError("boom")

    acquired = threading.Event()

    def second_acquirer() -> None:
        with target.open("r+b") as f2:
            with exclusive_lock(f2):
                acquired.set()

    t = threading.Thread(target=second_acquirer)
    t.start()
    t.join(timeout=2)
    assert acquired.is_set(), "second acquirer never got the lock — release-on-exception is broken"


def test_exclusive_lock_restores_file_position_on_windows(tmp_path: Path) -> None:
    target = tmp_path / "lock.bin"
    target.write_bytes(b"0123456789")

    with target.open("r+b") as f:
        f.seek(5)
        assert f.tell() == 5
        with exclusive_lock(f):
            pass
        assert f.tell() == 5
        with exclusive_lock(f):
            pass
        assert f.tell() == 5


def test_exclusive_lock_works_on_text_mode_file(tmp_path: Path) -> None:
    target = tmp_path / "lock.txt"
    target.write_text("")

    with target.open("a") as f:
        with exclusive_lock(f):
            f.write("hello\n")
            f.flush()

    with target.open("r+") as f:
        with exclusive_lock(f):
            data = f.read()

    assert data == "hello\n"
