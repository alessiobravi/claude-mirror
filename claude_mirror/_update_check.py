"""Background update-availability check.

Once per 24 h (configurable via the cache TTL constant below), this module
fetches the `pyproject.toml` from the canonical claude-mirror GitHub repo
and compares its declared version against the locally-installed
`claude-mirror` package version. When the remote version is strictly
greater, an inline notice is printed at command launch (and, when used
from the watcher daemon, a non-disruptive desktop popup is fired).

Design constraints:
  * NEVER block the foreground command — the actual HTTP fetch runs in
    a daemon thread; the foreground reads whatever the cache currently
    holds and decides whether to print a notice.
  * NEVER fail noisily — every error path silently no-ops. Update-check
    is best-effort; it is never allowed to break a sync command.
  * NEVER notify twice for the same version — the cache tracks
    `last_notified_version` so the daemon popup fires exactly once per
    new release per machine, not on every wake-up.
  * Honor opt-out: the env var `CLAUDE_MIRROR_NO_UPDATE_CHECK` and a
    YAML config field `update_check: false` both fully disable the
    check (no fetch, no notice, no popup).
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Where the cache lives. ~/.config/claude_mirror/.update_check.json
_CACHE_FILE = Path.home() / ".config" / "claude_mirror" / ".update_check.json"
# How often the background thread re-fetches the upstream version.
_CHECK_INTERVAL_HOURS = 24
# Primary: GitHub API. Authoritative — the API hits the canonical Git
# blob store directly, bypassing the raw.githubusercontent.com CDN
# entirely. Subject to a 60 requests/hour rate limit per unauthenticated
# IP, which is generous for our 1/day cache TTL (and even for manual
# check-update calls). Returns JSON with base64-encoded content.
_API_URL = (
    "https://api.github.com/repos/alessiobravi/claude-mirror/contents/pyproject.toml"
)
# Fallback: raw CDN URL with cache-busting. Used only when the API is
# unavailable (rate-limited, 5xx, network filtering of api.github.com).
# Cache-busting via `?t=<unix_seconds>` + no-cache headers gives us the
# best chance of getting fresh content from the CDN, but isn't as
# authoritative as the API path — CDN edges can still serve stale
# content for a few minutes after a push.
_PYPROJECT_URL = (
    "https://raw.githubusercontent.com/alessiobravi/claude-mirror/main/pyproject.toml"
)
# Conservative timeout — the daemon thread never blocks the foreground,
# but we still don't want it lingering for minutes on a slow network.
_FETCH_TIMEOUT_SECONDS = 5

# Match `version = "X.Y.Z"` (or single quotes / extra whitespace).
_VERSION_RE = re.compile(
    r"""^\s*version\s*=\s*['"]([^'"]+)['"]""", re.MULTILINE
)


def _is_disabled() -> bool:
    """Honor the env-var opt-out. Config-field opt-out is checked at
    call sites (we don't have a Config in scope here)."""
    return bool(os.environ.get("CLAUDE_MIRROR_NO_UPDATE_CHECK"))


def _resolve_repo_root() -> Optional[Path]:
    """Resolve the editable-install repo root (the dir containing
    pyproject.toml), or None if not an editable install or detection
    fails. Used by `update --apply` to run git+pipx as list-form
    subprocess calls (no shell). Kept separate from
    suggested_update_command() so the shell-string and the structured
    path stay in sync without one parsing the other.
    """
    try:
        import claude_mirror
        pkg_file = getattr(claude_mirror, "__file__", None)
        if not pkg_file:
            return None
        pkg_dir = Path(pkg_file).resolve().parent
        if "site-packages" in pkg_dir.parts:
            return None  # non-editable install — use `pipx upgrade` path
        repo_root = pkg_dir.parent
        if not (repo_root / "pyproject.toml").exists():
            return None
        return repo_root
    except (ImportError, OSError):
        # ImportError: package not findable; OSError: resolve() failed
        # (broken symlink, missing dir). Coding bugs propagate.
        return None


def suggested_update_command() -> str:
    """Return a ready-to-paste shell command that updates the local
    claude-mirror install. Detects whether the install was made editable
    (`pipx install -e <path>`) and prints the absolute path so the user
    can copy-paste; falls back to the generic recipe otherwise.

    Cases handled:
      * Editable install (pipx install -e <repo>) — most common in this
        project's distribution model. Returns
            cd '<repo-root>' && git pull && pipx install -e . --force
        with the actual resolved repo path.
      * Non-editable pipx install (e.g. from a URL or eventual PyPI):
        returns `pipx upgrade claude-mirror`.
      * Detection failure (e.g. unusual install layout): returns the
        generic v0.3.x phrasing so users at least see something useful.
    """
    import shlex
    try:
        # Locate the installed package on disk. For editable installs
        # this is `<repo>/claude_mirror/`; for non-editable, it's
        # `<venv>/lib/python3.X/site-packages/claude_mirror/`.
        import claude_mirror
        pkg_file = getattr(claude_mirror, "__file__", None)
        if not pkg_file:
            return "pipx install -e . --force from your repo dir"
        pkg_dir = Path(pkg_file).resolve().parent
        # `site-packages` in the path means a real install, not editable.
        if "site-packages" in pkg_dir.parts:
            return "pipx upgrade claude-mirror"
        # Editable: parent dir of the package IS the repo root (where
        # pyproject.toml lives). Verify by checking for pyproject.toml.
        repo_root = pkg_dir.parent
        if not (repo_root / "pyproject.toml").exists():
            # Defensive fallback if the layout looks weird.
            return "pipx install -e . --force from your repo dir"
        repo_quoted = shlex.quote(str(repo_root))
        return (
            f"cd {repo_quoted} && git pull && pipx install -e . --force"
        )
    except (ImportError, OSError):
        # ImportError: package not findable; OSError: resolve() failed.
        # Coding bugs propagate.
        return "pipx install -e . --force from your repo dir"


def _get_current_version() -> str:
    """Return the locally-installed claude-mirror version, or '0.0.0' on
    failure. Used both for comparison and for the User-Agent header."""
    try:
        from importlib.metadata import version
        return version("claude-mirror")
    except (ImportError, LookupError):
        # ImportError: importlib.metadata missing (shouldn't on 3.8+);
        # LookupError: PackageNotFoundError subclass — package not
        # installed via metadata. Coding bugs propagate.
        return "0.0.0"


def _load_cache() -> dict:
    """Read the cache file. Returns {} on any failure (missing file,
    corrupted JSON, permission error, etc.)."""
    try:
        return json.loads(_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        # JSONDecodeError: malformed cache; OSError: missing/unreadable
        # (FileNotFoundError, PermissionError); ValueError: defensive
        # catch for unusual decode paths. Coding bugs propagate.
        return {}


def _save_cache(data: dict) -> None:
    """Atomic write via tmp + os.replace so a crash mid-write can't
    corrupt the cache. Best-effort: any failure is swallowed."""
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(_CACHE_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, _CACHE_FILE)
    except OSError:
        # mkdir/write_text/os.replace all raise OSError on filesystem
        # failure (permission, disk full, missing parent). Best-effort
        # cache; programming bugs (TypeError from non-serialisable data,
        # AttributeError) propagate so they're visible.
        pass


def _fetch_via_api() -> Optional[str]:
    """Authoritative fetch via the GitHub API.

    Hits api.github.com/repos/<owner>/<repo>/contents/pyproject.toml,
    base64-decodes the `content` field, and parses the version line.
    Bypasses raw.githubusercontent.com's CDN entirely, so it sees the
    canonical version the moment the push lands — no edge propagation
    delay.

    Subject to a 60 requests/hour rate limit per unauthenticated IP.
    With our 24h cache TTL this is genuinely a non-issue (a single
    user makes ~1 request/day from the daemon), and even hammering
    `claude-mirror check-update` 60 times in an hour stays under the cap.
    """
    import base64
    try:
        req = urllib.request.Request(
            _API_URL,
            headers={
                "User-Agent": f"claude-mirror/{_get_current_version()} update-check",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        encoded = data.get("content", "")
        if not encoded:
            return None
        # GitHub returns base64 with embedded newlines (RFC 2045 76-col
        # wrap). Python's b64decode handles that natively.
        content = base64.b64decode(encoded).decode("utf-8", errors="replace")
        m = _VERSION_RE.search(content)
        return m.group(1).strip() if m else None
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return None
    except Exception:
        return None


def _fetch_via_raw_with_busting() -> Optional[str]:
    """Fallback fetch via raw.githubusercontent.com with cache-busting.

    Used only when the API is unavailable (rate-limited, 5xx, network
    blocking of api.github.com but not raw). Less authoritative — the
    CDN can serve stale content for a few minutes after a push — but
    always available, even when the API quota is exhausted.
    """
    import time as _time
    try:
        # Append a cache-busting query so the CDN treats this as a
        # distinct URL on every call. The server ignores the param —
        # it's purely an edge-routing trick.
        url = f"{_PYPROJECT_URL}?t={int(_time.time())}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"claude-mirror/{_get_current_version()} update-check",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        m = _VERSION_RE.search(content)
        return m.group(1).strip() if m else None
    except (urllib.error.URLError, OSError, ValueError, UnicodeDecodeError):
        return None
    except Exception:
        return None


def _fetch_remote_version() -> Optional[str]:
    """Fetch the upstream version from GitHub.

    Tries the authoritative API path first; falls back to the raw URL
    with cache-busting if the API fails for any reason (rate limit,
    network issue, etc.). Returns None on any total failure.

    Sends a User-Agent identifying the running claude-mirror version so
    the maintainer can correlate version-update lag with installed-base
    drift via standard server logs (no telemetry sent — just the UA).
    """
    result = _fetch_via_api()
    if result is not None:
        return result
    return _fetch_via_raw_with_busting()


def _is_strictly_newer(remote: str, current: str) -> bool:
    """Compare two version strings. Returns True iff remote is strictly
    greater than current.

    Prefers `packaging.version.Version` for proper PEP-440 semantics
    (handles pre-releases, post-releases, dev tags). Falls back to a
    component-wise integer comparison of the form `X.Y.Z` when
    `packaging` is unavailable. The fallback intentionally rejects
    pre-release / post-release suffixes so `0.4.0.dev1` doesn't get
    misinterpreted as newer than `0.4.0`.
    """
    if not remote or remote == "0.0.0":
        return False  # error sentinel from _fetch_remote_version
    try:
        from packaging.version import Version
        return Version(remote) > Version(current)
    except Exception:
        # Component-wise int comparison fallback. Splits on '.',
        # pads to equal length, compares as integers. Refuses to
        # claim "newer" when either version has non-numeric segments
        # (e.g. '0.4.0.dev1' or '0.4.0a1') — better to under-report
        # than spuriously prompt the user to update.
        try:
            r_parts = [int(p) for p in remote.split(".")]
            c_parts = [int(p) for p in current.split(".")]
        except ValueError:
            return False  # non-numeric segment → don't risk a false positive
        # Pad shorter list with zeros so 1.2 vs 1.2.0 compares equal.
        n = max(len(r_parts), len(c_parts))
        r_parts += [0] * (n - len(r_parts))
        c_parts += [0] * (n - len(c_parts))
        return r_parts > c_parts


def _do_background_check() -> None:
    """The actual fetch + cache-write, run in a daemon thread.

    Reads the cache; if the last-checked timestamp is fresher than
    `_CHECK_INTERVAL_HOURS`, returns immediately. Otherwise fetches
    upstream pyproject.toml, parses the version, and writes back.
    Never raises — all failures are swallowed.
    """
    try:
        cache = _load_cache()
        last = cache.get("last_checked", "1970-01-01T00:00:00Z")
        try:
            last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
        except Exception:
            last_dt = datetime.fromtimestamp(0, tz=timezone.utc)
        now = datetime.now(timezone.utc)
        if (now - last_dt) < timedelta(hours=_CHECK_INTERVAL_HOURS):
            return  # cache fresh
        latest = _fetch_remote_version()
        if not latest:
            # Update last_checked anyway so we don't hammer GitHub when
            # offline — but keep the previously cached latest_version so
            # the inline notice still shows if applicable.
            cache["last_checked"] = now.isoformat().replace("+00:00", "Z")
            _save_cache(cache)
            return
        cache["last_checked"] = now.isoformat().replace("+00:00", "Z")
        cache["latest_version"] = latest
        _save_cache(cache)
    except Exception:
        # Daemon thread must never raise — would just print a noisy
        # traceback to stderr without context.
        pass


def check_for_update(notify_desktop: bool = False) -> None:
    """Public entry point. Spawn the background fetch (silent) and, based
    on whatever version is already cached, optionally print an inline
    notice or fire a desktop popup.

    notify_desktop: when True, also fire a non-disruptive desktop
    notification via `Notifier.notify(...)` — but only ONCE per new
    version (tracked in cache as `last_notified_version`). Used from
    the long-running watcher daemon (`watch-all`) so users who don't
    invoke claude-mirror interactively still hear about updates.

    On interactive command launches (foreground CLI), `notify_desktop`
    is left False — the inline notice is enough; a popup would just
    duplicate the stdout line.
    """
    if _is_disabled():
        return

    # Kick off the (best-effort) background fetch.
    threading.Thread(target=_do_background_check, daemon=True).start()

    # Decide whether to surface anything from the currently-cached state.
    cache = _load_cache()
    latest = cache.get("latest_version")
    if not latest:
        return  # nothing cached yet — first run will populate
    current = _get_current_version()
    if not _is_strictly_newer(latest, current):
        return

    # Inline notice — printed via Rich at every CLI invocation that
    # observes a newer cached version. Yellow header line + a ready-
    # to-paste update command (resolved against the actual install
    # location so the user doesn't have to remember their repo path).
    update_cmd = suggested_update_command()
    try:
        from rich.console import Console
        c = Console(stderr=False)
        c.print(
            f"[yellow]🆕 claude-mirror {latest} is available[/] "
            f"(you have {current}).\n"
            f"[dim]Run:[/] [bold]claude-mirror update --apply[/]   "
            f"[dim](or manually: {update_cmd})[/]\n"
            f"[dim](set CLAUDE_MIRROR_NO_UPDATE_CHECK=1 to silence)[/]"
        )
    except Exception:
        try:
            print(
                f"claude-mirror {latest} is available (you have {current}). "
                f"Update: {update_cmd}",
                file=sys.stderr,
            )
        except Exception:
            pass

    # Desktop popup — daemon-mode only. Fired once per new version so
    # users running watch-all over a long stretch get told without spam.
    if notify_desktop:
        last_notified = cache.get("last_notified_version", "")
        if last_notified == latest:
            return  # already popped for this version
        try:
            from .notifier import Notifier
            n = Notifier(str(Path.home()))
            # Informational only — NOT action_required, so on Linux it
            # uses normal urgency (not critical). Title is short so it
            # fits in the OS notification banner.
            # Daemon popup body uses the resolved update command too,
            # so the user can copy-paste from the notification's text
            # (where the OS supports that) without hunting for their repo path.
            n.notify(
                title=f"claude-mirror {latest} available",
                message=(
                    f"You have {current}. Update: {update_cmd}"
                ),
            )
            cache["last_notified_version"] = latest
            _save_cache(cache)
        except Exception:
            # Notifier missing or notification system not available —
            # silently swallow.
            pass


def force_check_now() -> Optional[str]:
    """Synchronous variant for testing / `claude-mirror --check-update`.
    Bypasses the cache TTL, fetches upstream, returns the version
    string (or None on failure). Updates the cache."""
    if _is_disabled():
        return None
    latest = _fetch_remote_version()
    if not latest:
        return None
    cache = _load_cache()
    cache["last_checked"] = datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    cache["latest_version"] = latest
    _save_cache(cache)
    return latest
