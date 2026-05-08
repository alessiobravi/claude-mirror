"""Persistent watermark storage for `claude-mirror watch --once`.

Each backend's `watch_once()` implementation needs to remember the
last-dispatched event so that successive cron-driven runs only surface
events that arrived since the previous invocation. The state is keyed
by a tuple `(backend_kind, machine_name, project_path)` and lives at:

    ~/.config/claude_mirror/watch_once_state/<sanitised_key>.json

We deliberately do NOT use the system-temp dir or `/var/run` — cron
jobs frequently run as the same user and need the watermark to survive
reboots. The XDG-style config dir already serves that purpose for every
other piece of claude-mirror state.

A `FIRST_RUN_SENTINEL` is returned when no watermark file exists, so
the caller can distinguish "first run ever — capture the current log
tail and dispatch nothing" from "we have a stored watermark — dispatch
events newer than it".
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional, Tuple


# Type alias matching the `_EventKey` shapes in the notification backends.
# All backends currently use the same composite key:
#     (timestamp, user, machine, files-tuple)
WatermarkKey = Tuple[str, str, str, Tuple[str, ...]]

# Routing key for the on-disk file: (backend_kind, machine_name, project_path).
RoutingKey = Tuple[str, str, str]


# Returned by `load_watermark` when no file exists — a singleton object
# the caller can `is`-compare against. Distinct from None and from any
# real (or empty-marker) tuple value.
class _FirstRunSentinel:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        return "<FIRST_RUN_SENTINEL>"


FIRST_RUN_SENTINEL: Any = _FirstRunSentinel()


def _state_dir() -> Path:
    """Return the directory where watch-once watermarks live.

    Honours `CLAUDE_MIRROR_CONFIG_DIR` (test injection point) before
    falling back to `~/.config/claude_mirror/watch_once_state/`.
    """
    base = os.environ.get("CLAUDE_MIRROR_CONFIG_DIR")
    if base:
        return Path(base).expanduser() / "watch_once_state"
    return Path.home() / ".config" / "claude_mirror" / "watch_once_state"


def _filename_for(key: RoutingKey) -> Path:
    """Build the on-disk filename for a routing key.

    Hashes the components into the filename to avoid path-traversal
    issues with project paths that contain "/" or other characters
    that would either break the filesystem mapping or escape the state
    directory. The hash is stable across runs on the same machine.
    """
    raw = "\x1f".join(key).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()[:32]
    # Include a short readable prefix so a human inspecting the state
    # dir can guess which file belongs to which project.
    safe_kind = "".join(ch if ch.isalnum() else "_" for ch in key[0])[:16]
    return _state_dir() / f"{safe_kind}_{digest}.json"


def load_watermark(key: RoutingKey) -> Any:
    """Return the stored watermark for `key`, or `FIRST_RUN_SENTINEL` if
    no file exists / cannot be read.

    Reading errors (permission denied, malformed JSON) are treated as
    "no watermark" so a corrupt state file does not wedge cron-driven
    operation — the next run will re-bootstrap from the current log
    tail.
    """
    path = _filename_for(key)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
        return FIRST_RUN_SENTINEL
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return FIRST_RUN_SENTINEL
    if not isinstance(data, dict):
        return FIRST_RUN_SENTINEL
    try:
        return (
            str(data["timestamp"]),
            str(data["user"]),
            str(data["machine"]),
            tuple(str(f) for f in data.get("files", [])),
        )
    except (KeyError, TypeError):
        return FIRST_RUN_SENTINEL


def save_watermark(key: RoutingKey, watermark: WatermarkKey) -> None:
    """Persist `watermark` for `key`.

    Best-effort: write failures are swallowed. The semantic cost of a
    skipped write is "the next --once run might re-dispatch a few
    already-seen events"; the cost of a raised exception during cron
    is a non-zero exit, which the cron daemon would email as a failure.
    The trade-off favours silent best-effort here.
    """
    state_dir = _state_dir()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    path = _filename_for(key)
    payload = {
        "timestamp": watermark[0],
        "user": watermark[1],
        "machine": watermark[2],
        "files": list(watermark[3]),
    }
    try:
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        return


def clear_watermark(key: RoutingKey) -> None:
    """Remove the watermark file for `key`, if it exists.

    Used by tests; not currently surfaced in any CLI command. Failures
    are swallowed for the same reason as `save_watermark`.
    """
    path = _filename_for(key)
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return
