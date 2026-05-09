"""Network-free, sub-50ms shell-prompt status snippet (SHELL-PROMPT).

This module powers `claude-mirror prompt`, the shell-prompt-segment surface
modelled on git's ``__git_ps1``. Every PS1 redraw runs the command, so the
contract is strict:

  * NO network calls. Ever. Output derives only from local state — the
    manifest at ``<project>/.claude_mirror_manifest.json``, the local file
    tree, and the persistent hash cache at
    ``<project>/.claude_mirror_hash_cache.json``.
  * NO live progress / spinners / banners. The CLI's project-wide
    "every command shows progress" rule has an explicit exception here:
    silence is mandatory. A spinner in PS1 would tear every shell line.
  * Errors NEVER raise to the user's shell. We exit 0 and emit a single
    ``warning`` symbol on stdout (plus a one-line stderr message); a
    non-zero exit would break the user's prompt rendering for every
    subsequent command.
  * Sub-50ms target on a typical project (~500 files). The hash cache
    lets us answer the "did the file change since last sync?" question
    by ``stat()`` alone for unchanged files; only files whose
    ``(size, mtime_ns)`` no longer match a cached entry need any further
    work, and even then we treat the change pessimistically as
    "locally ahead" without re-hashing — re-hashing 500 files exceeds
    budget and the prompt is allowed to be a one-tick-stale signal.

Symbol vocabulary (kept in sync with docs/cli-reference.md):

  symbols (default)::
      OK                  in sync (every local file matches manifest)
      up-arrow N          N local files ahead of manifest (modified or new)
      down-arrow N        N remote-ahead files (cached from last status)
      tilde N             N files with pending_retry state on any backend
      question mark       no manifest yet (first sync hasn't happened)
      warning             error reading state (corrupt manifest etc.)

  ascii::
      OK / +N / -N / ~N / ? / !

  text::
      "in sync" / "+N ahead" / "N conflict(s)" / "no manifest" / "error"

  json::
      {"in_sync": bool, "local_ahead": int, "remote_ahead": int,
       "conflicts": int, "no_manifest": bool, "error": bool}

The cache file ``<project>/.claude_mirror_prompt_cache.json`` keys on
the manifest's mtime_ns plus the live local-file count; if both match
the cached snapshot, we return the cached payload without walking the
project tree at all. The cache invalidates on every manifest rewrite
(push / pull / sync all rewrite the manifest) and on file additions
or removals.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .config import Config
from .hash_cache import CACHE_FILE as HASH_CACHE_FILE
from .manifest import MANIFEST_FILE, Manifest


PROMPT_CACHE_FILE = ".claude_mirror_prompt_cache.json"

# Above this file count we skip the live walk entirely and either return
# the cached prompt string (if present) or the "computing" ellipsis. The
# prompt MUST NOT block the user's shell while a giant project is
# inspected.
LARGE_PROJECT_THRESHOLD = 5000

ELLIPSIS_FALLBACK = "…"

# Output symbols (UTF-8) — see module docstring for the full vocabulary.
SYMBOL_OK = "✓"           # CHECK MARK
SYMBOL_AHEAD = "↑"        # UPWARDS ARROW
SYMBOL_BEHIND = "↓"       # DOWNWARDS ARROW
SYMBOL_CONFLICT = "~"
SYMBOL_NO_MANIFEST = "?"
SYMBOL_ERROR = "⚠"        # WARNING SIGN


@dataclass
class PromptState:
    in_sync: bool
    local_ahead: int
    remote_ahead: int
    conflicts: int
    no_manifest: bool
    error: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "in_sync": self.in_sync,
            "local_ahead": self.local_ahead,
            "remote_ahead": self.remote_ahead,
            "conflicts": self.conflicts,
            "no_manifest": self.no_manifest,
            "error": self.error,
        }


def _format_state(
    state: PromptState,
    fmt: str,
    quiet_when_clean: bool,
) -> str:
    if fmt == "json":
        return json.dumps(state.to_dict(), separators=(",", ":"), sort_keys=True)

    if state.error:
        if fmt == "ascii":
            return "!"
        if fmt == "text":
            return "error"
        return SYMBOL_ERROR

    if state.no_manifest:
        if fmt == "ascii":
            return "?"
        if fmt == "text":
            return "no manifest"
        return SYMBOL_NO_MANIFEST

    parts: list[str] = []
    if state.local_ahead:
        if fmt == "ascii":
            parts.append(f"+{state.local_ahead}")
        elif fmt == "text":
            parts.append(f"+{state.local_ahead} ahead")
        else:
            parts.append(f"{SYMBOL_AHEAD}{state.local_ahead}")
    if state.remote_ahead:
        if fmt == "ascii":
            parts.append(f"-{state.remote_ahead}")
        elif fmt == "text":
            parts.append(f"-{state.remote_ahead} behind")
        else:
            parts.append(f"{SYMBOL_BEHIND}{state.remote_ahead}")
    if state.conflicts:
        if fmt == "text":
            label = "conflict" if state.conflicts == 1 else "conflicts"
            parts.append(f"{state.conflicts} {label}")
        else:
            parts.append(f"{SYMBOL_CONFLICT}{state.conflicts}")

    if not parts:
        if quiet_when_clean:
            return ""
        if fmt == "ascii":
            return "OK"
        if fmt == "text":
            return "in sync"
        return SYMBOL_OK

    if fmt == "text":
        return ", ".join(parts)
    return " ".join(parts)


def _wrap(body: str, prefix: str, suffix: str, quiet_when_clean: bool) -> str:
    if quiet_when_clean and body == "":
        return ""
    return f"{prefix}{body}{suffix}"


def _read_hash_cache(project_path: Path) -> dict[str, list[Any]]:
    """Read the persistent local-hash cache without instantiating HashCache.

    HashCache is lock-protected and writes back on dirty saves; we only
    need a read-only snapshot here, and avoiding the constructor saves
    the threading-Lock allocation on every prompt redraw."""
    path = project_path / HASH_CACHE_FILE
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _local_files_paths(project_path: Path, file_patterns: list[str]) -> list[str]:
    """Return relative paths of local files matching ``file_patterns``.

    Mirrors the public-but-private ``SyncEngine._local_files`` walk minus
    the IgnoreSet hookup. The prompt deliberately does not parse
    ``.claude_mirror_ignore`` here — that file's grammar requires loading
    a non-trivial chunk of the ``ignore`` module, and an ignored file
    showing up as ``new_local`` once in the prompt is a much smaller
    failure than a 200ms grammar-load on every PS1 redraw.
    """
    found: set[str] = set()
    for pattern in file_patterns:
        for path in project_path.glob(pattern):
            if not path.is_file():
                continue
            name = path.name
            if name in (
                MANIFEST_FILE,
                HASH_CACHE_FILE,
                PROMPT_CACHE_FILE,
            ):
                continue
            rel = path.relative_to(project_path).as_posix()
            found.add(rel)
    return sorted(found)


def _load_manifest_raw(manifest_path: Path) -> dict[str, Any]:
    with open(manifest_path, "rb") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("manifest root is not a JSON object")
    return data


def _entry_has_pending_retry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    remotes = entry.get("remotes")
    if not isinstance(remotes, dict):
        return False
    for r in remotes.values():
        if isinstance(r, dict) and r.get("state") == "pending_retry":
            return True
    return False


def _compute_state_uncached(
    project_path: Path,
    file_patterns: list[str],
    local_paths: list[str],
) -> PromptState:
    manifest_path = project_path / MANIFEST_FILE
    if not manifest_path.exists():
        return PromptState(
            in_sync=False,
            local_ahead=0,
            remote_ahead=0,
            conflicts=0,
            no_manifest=True,
            error=False,
        )

    try:
        manifest_raw = _load_manifest_raw(manifest_path)
    except (OSError, json.JSONDecodeError, ValueError):
        return PromptState(
            in_sync=False,
            local_ahead=0,
            remote_ahead=0,
            conflicts=0,
            no_manifest=False,
            error=True,
        )

    hash_cache = _read_hash_cache(project_path)

    local_ahead = 0
    for rel in local_paths:
        entry = manifest_raw.get(rel)
        synced_hash = ""
        if isinstance(entry, dict):
            raw = entry.get("synced_hash", "")
            if isinstance(raw, str):
                synced_hash = raw
        if not synced_hash:
            local_ahead += 1
            continue
        try:
            st = (project_path / rel).stat()
        except OSError:
            continue
        cached = hash_cache.get(rel)
        cached_hash: Optional[str] = None
        if (
            isinstance(cached, list)
            and len(cached) >= 3
            and cached[0] == st.st_size
            and cached[1] == st.st_mtime_ns
            and isinstance(cached[2], str)
        ):
            cached_hash = cached[2]
        if cached_hash is None:
            local_ahead += 1
            continue
        if cached_hash != synced_hash:
            local_ahead += 1

    conflicts = sum(
        1 for entry in manifest_raw.values()
        if _entry_has_pending_retry(entry)
    )

    in_sync = local_ahead == 0 and conflicts == 0

    return PromptState(
        in_sync=in_sync,
        local_ahead=local_ahead,
        remote_ahead=0,
        conflicts=conflicts,
        no_manifest=False,
        error=False,
    )


def _read_prompt_cache(cache_path: Path) -> Optional[dict[str, Any]]:
    try:
        with open(cache_path, "rb") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_prompt_cache(cache_path: Path, payload: dict[str, Any]) -> None:
    try:
        tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(payload, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, cache_path)
    except OSError:
        return


def _state_from_cache_payload(payload: dict[str, Any]) -> Optional[PromptState]:
    state = payload.get("state")
    if not isinstance(state, dict):
        return None
    try:
        return PromptState(
            in_sync=bool(state.get("in_sync", False)),
            local_ahead=int(state.get("local_ahead", 0)),
            remote_ahead=int(state.get("remote_ahead", 0)),
            conflicts=int(state.get("conflicts", 0)),
            no_manifest=bool(state.get("no_manifest", False)),
            error=bool(state.get("error", False)),
        )
    except (TypeError, ValueError):
        return None


def compute_prompt(
    config: Config,
    *,
    fmt: str = "symbols",
    prefix: str = "",
    suffix: str = "",
    quiet_when_clean: bool = False,
) -> str:
    """Return the prompt-segment string for ``config``'s project.

    Pure function: no I/O beyond reading the project's manifest, hash
    cache, and prompt cache files. Always returns a string; never
    raises. The caller (CLI) writes stdout + decides exit code (always 0).
    """
    project_path = Path(config.project_path)
    if not project_path.is_dir():
        return _wrap(
            _format_state(
                PromptState(
                    in_sync=False,
                    local_ahead=0,
                    remote_ahead=0,
                    conflicts=0,
                    no_manifest=False,
                    error=True,
                ),
                fmt,
                quiet_when_clean,
            ),
            prefix,
            suffix,
            quiet_when_clean,
        )

    manifest_path = project_path / MANIFEST_FILE
    cache_path = project_path / PROMPT_CACHE_FILE

    try:
        manifest_mtime_ns = manifest_path.stat().st_mtime_ns
    except (FileNotFoundError, OSError):
        manifest_mtime_ns = 0

    cached_payload = _read_prompt_cache(cache_path)

    local_paths: Optional[list[str]] = None
    file_count: Optional[int] = None

    if cached_payload is not None:
        cached_mtime = cached_payload.get("manifest_mtime_ns")
        cached_count = cached_payload.get("file_count")
        if (
            isinstance(cached_mtime, int)
            and isinstance(cached_count, int)
            and cached_mtime == manifest_mtime_ns
        ):
            local_paths = _local_files_paths(project_path, list(config.file_patterns))
            file_count = len(local_paths)
            if file_count == cached_count:
                state = _state_from_cache_payload(cached_payload)
                if state is not None:
                    return _wrap(
                        _format_state(state, fmt, quiet_when_clean),
                        prefix,
                        suffix,
                        quiet_when_clean,
                    )

    if local_paths is None:
        if cached_payload is not None:
            try:
                preview_count = sum(
                    1 for pattern in config.file_patterns
                    for p in project_path.glob(pattern)
                    if p.is_file()
                )
            except OSError:
                preview_count = 0
            if preview_count > LARGE_PROJECT_THRESHOLD:
                state = _state_from_cache_payload(cached_payload)
                if state is not None:
                    return _wrap(
                        _format_state(state, fmt, quiet_when_clean),
                        prefix,
                        suffix,
                        quiet_when_clean,
                    )
                return _wrap(ELLIPSIS_FALLBACK, prefix, suffix, quiet_when_clean)

        local_paths = _local_files_paths(project_path, list(config.file_patterns))
        file_count = len(local_paths)

        if file_count > LARGE_PROJECT_THRESHOLD and cached_payload is None:
            return _wrap(ELLIPSIS_FALLBACK, prefix, suffix, quiet_when_clean)

    state = _compute_state_uncached(project_path, list(config.file_patterns), local_paths)

    payload: dict[str, Any] = {
        "manifest_mtime_ns": manifest_mtime_ns,
        "file_count": file_count if file_count is not None else len(local_paths),
        "state": state.to_dict(),
        "computed_at": time.time(),
    }
    _write_prompt_cache(cache_path, payload)

    return _wrap(
        _format_state(state, fmt, quiet_when_clean),
        prefix,
        suffix,
        quiet_when_clean,
    )
