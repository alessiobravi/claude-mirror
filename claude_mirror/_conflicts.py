"""Conflict-envelope plumbing for AGENT-MERGE.

When `claude-mirror sync` finds a file changed on BOTH sides since the
last sync, it has historically prompted the user for `keep-local /
keep-remote / open-editor / skip` (see `merge.py::MergeHandler`). That
forces the user to do the merge cognition at the worst moment — alone,
in a terminal, without the help of the LLM agent already running in
their editor.

AGENT-MERGE adds a structured envelope on disk (one JSON file per
conflicted file) that the agent — Claude Code, Cursor, Codex, etc. —
reads via the skill, performs the merge, then applies via a new
`conflict apply` subcommand. The CLI itself binds to NO LLM API: it is
purely file plumbing. This module is the file plumbing.

The envelope is written for EVERY text-file conflict (cheap; just a
JSON file) but it sits inert if no agent picks it up. Existing sync
behaviour is unchanged — the interactive prompt still fires; the
envelope is information ALSO STORED, not a behaviour change.

Public API:
  - `ConflictEnvelope`      — frozen dataclass schema (version=1)
  - `envelope_dir`          — `~/.local/state/claude-mirror/<project>/conflicts/`
  - `envelope_path`         — flat-file path inside that dir for a rel-path
  - `write_envelope`        — atomic JSON write
  - `read_envelope`         — load + validate
  - `list_envelopes`        — alphabetical scan of pending envelopes
  - `clear_envelope`        — idempotent unlink
  - `is_eligible`           — text/text gate via `_diff.is_binary`
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import difflib
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import quote


from ._diff import is_binary


# Envelope schema version. Bump on breaking changes (consumers MUST
# reject envelopes whose `version` they don't understand — see
# `read_envelope`). Additive changes (new optional fields) keep v1.
ENVELOPE_VERSION = 1

# Filename suffix for an envelope on disk. Picked specifically so a
# `glob("*.merge.json")` excludes any other JSON files an operator may
# drop into the conflicts/ dir manually.
_ENVELOPE_SUFFIX = ".merge.json"


@dataclass(frozen=True)
class ConflictEnvelope:
    """Structured representation of a single sync conflict.

    Written by `write_envelope` and consumed by the skill via
    `claude-mirror conflict show <PATH>`. The agent merges the two text
    bodies and hands the result back via `conflict apply`. base_text is
    None when the manifest doesn't carry the last-synced content (we
    record the hash, not the bytes — the 3-way base is informational
    only and the agent can still merge from `local_text` + `remote_text`
    just fine).
    """

    path: str
    """Project-relative path with forward-slash separators on every
    platform — same shape as manifest keys + status JSON output."""

    local_text: str
    """Current local file content, decoded as UTF-8 (errors=replace).
    Binary files are NOT envelope-eligible — see `is_eligible`."""

    remote_text: str
    """Current remote file content, decoded as UTF-8."""

    local_hash: str
    """SHA-256 of `local_text.encode('utf-8')`, hex digest."""

    remote_hash: str
    """SHA-256 of `remote_text.encode('utf-8')`, hex digest."""

    created_at: str
    """ISO 8601 timestamp with `Z` suffix marking when the envelope was
    written. Stable across rewrites — the agent uses this to surface
    "envelope is N minutes old" context to the user."""

    project_path: str
    """Absolute path to the project root, for the agent's reference
    when it constructs absolute paths to read other project files."""

    backend: str
    """Backend identifier (`googledrive`, `dropbox`, `s3`, `smb`, …) the
    conflict was discovered on. Informational — the agent might use it
    to surface which backend's content the remote_text came from."""

    unified_diff: str
    """Precomputed unified diff `remote → local` with 3 lines of
    context. Saves the agent from recomputing it every time it inspects
    the envelope. Format matches `claude-mirror diff <path>` byte-for-
    byte modulo Rich styling."""

    base_text: Optional[str] = None
    """Last-synced content from the manifest, if available, else None.
    Currently the manifest stores hashes only, so `base_text` is None
    in practice — but the field is reserved for a future enhancement
    where we recover base content from snapshots."""

    base_hash: Optional[str] = None
    """SHA-256 of the base content from the manifest at last successful
    sync (the manifest's `synced_hash` field)."""

    version: int = ENVELOPE_VERSION
    """Envelope schema version — bump on breaking changes. Consumers
    MUST reject envelopes with a version they don't understand."""


def _state_root() -> Path:
    """Return the user's XDG state-home directory.

    Honors `XDG_STATE_HOME` when set; otherwise falls back to
    `~/.local/state` per the XDG Base Directory specification. We do
    NOT depend on the third-party `xdg` package — same convention as
    `_mount.py` and `cli.py` which already use the env-var lookup.
    """
    env = os.environ.get("XDG_STATE_HOME", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".local" / "state"


def _project_slug(project_path: Path) -> str:
    """URL-safe slug from an absolute project path.

    Two projects on the same machine can share a project name (e.g.
    `~/code/notes` and `~/work/notes`). Slugifying the absolute path
    keeps their envelope dirs disjoint. We use `urllib.parse.quote` with
    `safe=""` so every separator is percent-escaped — the result is a
    single filesystem-safe component.
    """
    return quote(str(project_path), safe="")


def envelope_dir(project_path: Path) -> Path:
    """Absolute directory holding the project's pending conflict envelopes.

    Layout: `<XDG_STATE_HOME>/claude-mirror/<project-slug>/conflicts/`.
    Created on demand (mkdir parents=True, exist_ok=True) so callers
    don't have to guard the first-use path. Returning the Path object
    rather than a string mirrors `manifest.py`'s convention.
    """
    target = _state_root() / "claude-mirror" / _project_slug(project_path) / "conflicts"
    target.mkdir(parents=True, exist_ok=True)
    return target


def envelope_path(project_path: Path, rel_path: str) -> Path:
    """Flat-file path for a single conflict's envelope.

    The project's conflict tree is flattened into one directory: each
    rel-path's `/` separators become `__` so e.g. `memory/foo/bar.md`
    lands at `memory__foo__bar.md.merge.json`. Flattening keeps the
    `clear_envelope` / `list_envelopes` paths trivial — no recursive
    walks, no empty-parent cleanup. Backslashes are normalised to `/`
    first so a Windows-shaped rel_path lands at the same envelope as
    its POSIX-shaped equivalent.
    """
    flat = rel_path.replace("\\", "/").replace("/", "__")
    return envelope_dir(project_path) / f"{flat}{_ENVELOPE_SUFFIX}"


def is_eligible(local_bytes: Optional[bytes], remote_bytes: Optional[bytes]) -> bool:
    """Both sides must be non-binary text for an envelope to be useful.

    Reuses `_diff.is_binary` rather than reimplementing the heuristic —
    same convention as DIFF and REDACT. None on either side means the
    file isn't really a 2-side conflict (it's a one-sided new file or
    a deleted file), which the engine handles via different code paths.
    """
    if local_bytes is None or remote_bytes is None:
        return False
    if is_binary(local_bytes):
        return False
    if is_binary(remote_bytes):
        return False
    return True


def _sha256_hex(text: str) -> str:
    """Hex SHA-256 of `text.encode('utf-8')`. Used for envelope hashes
    so two consumers on different platforms agree on the digest."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_unified_diff(remote_text: str, local_text: str, rel_path: str) -> str:
    """Precompute a `remote → local` unified diff with 3 lines of context.

    Same shape as `_diff.render_diff` produces, minus the Rich styling
    — this one is plain text for the envelope's `unified_diff` field
    and for `conflict show --format markers` consumers. Headers point
    at `remote/<rel>` → `local/<rel>` so a downstream tool can apply
    the diff via `patch -p1` if it wants to.
    """
    diff_iter = difflib.unified_diff(
        remote_text.splitlines(keepends=True),
        local_text.splitlines(keepends=True),
        fromfile=f"remote/{rel_path}",
        tofile=f"local/{rel_path}",
        n=3,
    )
    return "".join(diff_iter)


def make_envelope(
    *,
    rel_path: str,
    local_text: str,
    remote_text: str,
    base_text: Optional[str],
    base_hash: Optional[str],
    project_path: Path,
    backend: str,
    created_at: Optional[str] = None,
) -> ConflictEnvelope:
    """Construct a `ConflictEnvelope` from the engine's view of a conflict.

    `created_at` defaults to "now" in UTC with the `Z` suffix so the
    serialised envelope is unambiguously interpretable across timezones.
    Hashes are computed from the text bodies (NOT from the raw bytes
    the engine fetched) so two consumers re-deriving them after a
    round-trip through JSON agree.
    """
    if created_at is None:
        created_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return ConflictEnvelope(
        path=rel_path.replace("\\", "/"),
        local_text=local_text,
        remote_text=remote_text,
        base_text=base_text,
        local_hash=_sha256_hex(local_text),
        remote_hash=_sha256_hex(remote_text),
        base_hash=base_hash,
        created_at=created_at,
        project_path=str(project_path),
        backend=backend,
        unified_diff=build_unified_diff(remote_text, local_text, rel_path),
    )


def write_envelope(env: ConflictEnvelope, *, project_path: Path) -> Path:
    """Atomically write the envelope as JSON to its canonical path.

    Atomic = tempfile in the same directory + `os.replace`. A reader
    polling the conflicts/ directory never sees a half-written file.
    Returns the absolute path so the caller can surface it (e.g.
    `[envelope] <rel-path> → <path>` for the user's terminal).
    """
    target = envelope_path(project_path, env.path)
    payload = json.dumps(
        dataclasses.asdict(env),
        indent=2,
        sort_keys=False,
        ensure_ascii=False,
    ).encode("utf-8")

    fd, tmp_name = tempfile.mkstemp(
        prefix=".envelope.", suffix=_ENVELOPE_SUFFIX, dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
        os.replace(tmp_name, target)
    except Exception:
        # Best-effort cleanup — if os.replace failed we still want to
        # remove the temp file rather than leak it.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return target


def read_envelope(path: Path) -> ConflictEnvelope:
    """Load + validate a single envelope from disk.

    Raises `FileNotFoundError` if the path doesn't exist, `ValueError`
    if the envelope's `version` is anything other than `ENVELOPE_VERSION`
    (the project's compat policy: future versions ship a CLI that knows
    the new shape; older CLIs MUST refuse to misinterpret it).
    """
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    version = data.get("version")
    if version != ENVELOPE_VERSION:
        raise ValueError(
            f"Envelope at {path} has version {version!r}; "
            f"this CLI understands version {ENVELOPE_VERSION}. "
            f"Upgrade claude-mirror to read it."
        )
    # Filter to known dataclass fields so a future additive field on
    # disk doesn't crash an older CLI that supports the same major
    # version. (Today there's no skew, but the policy is cheap.)
    fields = {f.name for f in dataclasses.fields(ConflictEnvelope)}
    kwargs = {k: v for k, v in data.items() if k in fields}
    return ConflictEnvelope(**kwargs)


def list_envelopes(project_path: Path) -> list[ConflictEnvelope]:
    """Return every pending envelope under `envelope_dir`.

    Sorted alphabetically by file path so two callers see the same
    iteration order. Files that fail to parse (unknown version, bad
    JSON, truncated mid-write) are skipped silently — the caller would
    otherwise face a single bad envelope crashing the whole listing,
    which is the wrong failure mode for `conflict list`.
    """
    target = envelope_dir(project_path)
    out: list[ConflictEnvelope] = []
    for path in sorted(target.glob(f"*{_ENVELOPE_SUFFIX}")):
        try:
            out.append(read_envelope(path))
        except (ValueError, OSError, json.JSONDecodeError):
            # Skip rather than crash — a bad envelope is informational,
            # not fatal. The CLI surfaces the count of pending good
            # envelopes; bad ones get a separate diagnostic surface
            # (today: silence; future: a `conflict doctor` subcommand).
            continue
    return out


def clear_envelope(project_path: Path, rel_path: str) -> bool:
    """Delete the envelope file for a rel-path. Idempotent.

    Returns True if the file existed (and was removed), False if it was
    already gone. Callers that race on `apply` against an interactive
    `keep-local` resolution use the False return as the "already
    resolved" signal.
    """
    target = envelope_path(project_path, rel_path)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    return True
