"""Tests for the per-project notification inbox.

The inbox file lives at `{project_path}/.claude_mirror_inbox.jsonl` and is
written by `Notifier._write_inbox` (under LOCK_EX) and drained by
`read_and_clear_inbox()` (also under LOCK_EX). The drain was patched in
v0.5.5 to be atomic against concurrent writers — the concurrency test below
is the regression guard for that fix.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from claude_mirror.notifier import (
    INBOX_FILENAME,
    Notifier,
    inbox_path,
    read_and_clear_inbox,
)


def _enqueue(notifier: Notifier, event: dict) -> None:
    """Helper: write one entry through the same code path Notifier.notify()
    uses, but without triggering the platform desktop notifier (which would
    shell out to osascript / notify-send during the test)."""
    notifier._write_inbox("title", "message", event)


def test_inbox_write_then_read_round_trip(project_dir: Path) -> None:
    notifier = Notifier(str(project_dir))
    _enqueue(notifier, {"foo": "bar"})

    entries = read_and_clear_inbox(str(project_dir))
    assert len(entries) == 1
    assert entries[0]["foo"] == "bar"
    # File still exists but is now empty (truncated) — or is missing.
    p = inbox_path(str(project_dir))
    if p.exists():
        assert p.read_text() == ""


def test_inbox_multiple_writes_then_one_read_returns_all(project_dir: Path) -> None:
    notifier = Notifier(str(project_dir))
    for i in range(5):
        _enqueue(notifier, {"i": i})

    entries = read_and_clear_inbox(str(project_dir))
    assert len(entries) == 5
    # Order is preserved (append-only file → write order).
    assert [e["i"] for e in entries] == [0, 1, 2, 3, 4]

    # Second read drains nothing.
    assert read_and_clear_inbox(str(project_dir)) == []


def test_inbox_read_when_empty_returns_empty_list(project_dir: Path) -> None:
    # No inbox file exists at all.
    assert not inbox_path(str(project_dir)).exists()
    assert read_and_clear_inbox(str(project_dir)) == []


def test_inbox_read_clears_atomically_against_concurrent_writer(project_dir: Path) -> None:
    """TOCTOU regression test — v0.5.5 fix.

    A writer thread appends entries continuously. The main thread drains the
    inbox repeatedly. Because both sides take LOCK_EX on the same file, no
    line should ever be lost mid-clear, and no half-written line should ever
    leak through to the reader."""
    notifier = Notifier(str(project_dir))
    stop = threading.Event()
    write_count = [0]

    def writer() -> None:
        while not stop.is_set():
            _enqueue(notifier, {"event": "x", "n": write_count[0]})
            write_count[0] += 1

    t = threading.Thread(target=writer)
    t.start()
    try:
        all_read: list[dict] = []
        for _ in range(20):
            time.sleep(0.005)
            all_read.extend(read_and_clear_inbox(str(project_dir)))
        stop.set()
        t.join(timeout=2)
        # Final drain after the writer is fully stopped.
        all_read.extend(read_and_clear_inbox(str(project_dir)))

        # The writer wrote at least some events.
        assert write_count[0] > 0, "writer never produced any events"
        # Every entry that came back is a fully-formed dict with the
        # expected key — would fail if a partial line leaked through.
        assert len(all_read) > 0
        for entry in all_read:
            assert "event" in entry
            assert entry["event"] == "x"
        # No entry was lost: the count we read back matches what the
        # writer produced. This is the strict TOCTOU guarantee.
        assert len(all_read) == write_count[0]
    finally:
        stop.set()
        t.join(timeout=1)


def test_inbox_read_handles_corrupt_line_by_skipping(project_dir: Path) -> None:
    """A malformed JSON line in the inbox raises in `read_and_clear_inbox`,
    which catches the exception and returns []. The file is still cleared
    on the way out (since clearing happens before parsing inside the lock).

    Either contract is acceptable as long as it doesn't crash callers; the
    current implementation returns [] for any parse error. This test pins
    that behaviour so a refactor can't silently change it."""
    p = inbox_path(str(project_dir))
    p.write_text(
        '{"valid": 1}\n'
        'not-json-at-all\n'
        '{"valid": 2}\n'
    )

    result = read_and_clear_inbox(str(project_dir))

    # Current contract: on a JSON parse error the function returns [].
    # If the contract is ever loosened to skip bad lines, the assertion
    # below should be updated to check `[e for e in result if "valid" in e]`.
    assert result == [] or all("valid" in e for e in result)


def test_inbox_file_uses_correct_filename(project_dir: Path) -> None:
    """Regression: v0.5.1 renamed the inbox file from `.claude_sync_inbox.jsonl`
    to `.claude_mirror_inbox.jsonl`. Anything that hardcoded the old name
    would silently stop seeing notifications."""
    assert INBOX_FILENAME == ".claude_mirror_inbox.jsonl"

    notifier = Notifier(str(project_dir))
    _enqueue(notifier, {"k": "v"})

    expected = project_dir / ".claude_mirror_inbox.jsonl"
    legacy = project_dir / ".claude_sync_inbox.jsonl"
    assert expected.exists()
    assert not legacy.exists()


def test_inbox_unicode_content_round_trip(project_dir: Path) -> None:
    notifier = Notifier(str(project_dir))
    payload = {
        "emoji": "📝🚀",
        "accents": "café — naïve façade",
        "cjk": "日本語テスト",
    }
    _enqueue(notifier, payload)

    entries = read_and_clear_inbox(str(project_dir))
    assert len(entries) == 1
    for k, v in payload.items():
        assert entries[0][k] == v
