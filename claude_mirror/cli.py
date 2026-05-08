from __future__ import annotations

import io
import json as _json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

# Suppress gRPC / abseil INFO noise on macOS before gRPC is imported
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GRPC_TRACE", "")

import click
import google.auth.exceptions
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from . import _byo_wizard
from .backends import StorageBackend
from .backends.googledrive import GoogleDriveBackend
from .config import Config, CONFIG_DIR
from ._diff import render_diff
from .events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from .manifest import Manifest
from .merge import MergeHandler
from .notifications import NotificationBackend
from .notifications.pubsub import PubSubNotifier
from .notifier import Notifier, read_and_clear_inbox
from .snapshots import SnapshotManager, _human_size
from .sync import Status, STATUS_LABELS, SyncEngine


def _get_version() -> str:
    """Return the installed package version."""
    try:
        from importlib.metadata import version
        return version("claude-mirror")
    except Exception:
        return "unknown"

console = Console(force_terminal=True)

DEFAULT_CONFIG = str(Path.home() / ".config" / "claude_mirror" / "default.yaml")


# ──────────────────────────────────────────────────────────────────────────
# JSON output mode (v0.5.39)
#
# Five read-only commands (status, history, inbox, log, snapshots) accept
# `--json`. When set, the command:
#   * suppresses ALL Rich output (tables, banners, progress lines) by
#     swapping the module-level `console` for a quiet console that writes
#     to /dev/null,
#   * emits a single flat JSON document to stdout shaped as
#         {"version": 1, "command": "<name>", "result": {...}}
#   * on error, writes a JSON error envelope to stderr shaped as
#         {"version": 1, "command": "<name>", "error": {"type": ..., "message": ...}}
#     and exits 1.
#
# Schema is v1. Future breaking changes bump to v2 with both shapes
# supported during transition. `result` is per-command (see the
# command bodies and docs/cli-reference.md "JSON output" section).
# ──────────────────────────────────────────────────────────────────────────

JSON_SCHEMA_VERSION = 1


class _JsonMode:
    """Context manager: swap the module-level `console` for a quiet one
    for the lifetime of a `--json` command, restore on exit.

    Rich-rendering helpers (`_build_status_renderable`, `SnapshotManager.show_*`,
    progress bars in `make_phase_progress`) all reach for the module-level
    `console`. Replacing it for the duration of a `--json` command is the
    least invasive way to silence them without rewriting every helper.

    The quiet console writes to an in-memory `io.StringIO` rather than
    to a real /dev/null file handle. This avoids leaking an open file
    descriptor to Python 3.14's unraisable-exception finalizer (which
    pytest treats as a test failure under `filterwarnings = "error"`)
    and keeps the silencing fully in-process.
    """

    def __init__(self) -> None:
        self._saved: Optional[Console] = None
        self._saved_snap: Optional[Console] = None
        self._saved_sync: Optional[Console] = None
        self._sink: Optional[io.StringIO] = None
        self._saved_stdout: Optional[Any] = None
        self._saved_stderr: Optional[Any] = None
        self._saved_progress: Optional[tuple] = None

    def __enter__(self) -> "_JsonMode":
        import claude_mirror.cli as _cli_mod
        from claude_mirror import snapshots as _snap_mod
        from claude_mirror import sync as _sync_mod
        from claude_mirror import _progress as _progress_mod
        self._saved = _cli_mod.console
        self._saved_snap = _snap_mod.console
        self._saved_sync = getattr(_sync_mod, "console", None)
        self._saved_stdout = sys.stdout
        self._saved_stderr = sys.stderr
        self._sink = io.StringIO()
        quiet = Console(file=self._sink, force_terminal=False, no_color=True, quiet=True)
        _cli_mod.console = quiet
        _snap_mod.console = quiet
        if self._saved_sync is not None:
            _sync_mod.console = quiet
        # Replace make_phase_progress with a no-op factory for the
        # duration. Rich's Progress(transient=True) wraps a Live region
        # that does redirect_stdout=True by default; under Click's
        # CliRunner on Linux that redirect leaves Click's stdout wiring
        # poisoned even after Live.__exit__ runs, so subsequent
        # click.echo writes never reach result.stdout. Suppressing the
        # Progress entirely sidesteps the issue. We patch the SOURCE
        # module plus every consuming module's local binding (sync,
        # snapshots both do `from ._progress import make_phase_progress`
        # at module load time, which creates per-module name bindings).
        self._saved_progress = (
            _progress_mod.make_phase_progress,
            getattr(_sync_mod, "make_phase_progress", None),
            getattr(_snap_mod, "make_phase_progress", None),
        )
        _progress_mod.make_phase_progress = _no_op_progress  # type: ignore[assignment]
        if self._saved_progress[1] is not None:
            _sync_mod.make_phase_progress = _no_op_progress  # type: ignore[assignment]
        if self._saved_progress[2] is not None:
            _snap_mod.make_phase_progress = _no_op_progress  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: Any) -> None:
        import claude_mirror.cli as _cli_mod
        from claude_mirror import snapshots as _snap_mod
        from claude_mirror import sync as _sync_mod
        from claude_mirror import _progress as _progress_mod
        if self._saved is not None:
            _cli_mod.console = self._saved
        if self._saved_snap is not None:
            _snap_mod.console = self._saved_snap
        if self._saved_sync is not None:
            _sync_mod.console = self._saved_sync
        if self._saved_progress is not None:
            _progress_mod.make_phase_progress = self._saved_progress[0]  # type: ignore[assignment]
            if self._saved_progress[1] is not None:
                _sync_mod.make_phase_progress = self._saved_progress[1]  # type: ignore[assignment]
            if self._saved_progress[2] is not None:
                _snap_mod.make_phase_progress = self._saved_progress[2]  # type: ignore[assignment]
        if self._saved_stdout is not None:
            sys.stdout = self._saved_stdout
        if self._saved_stderr is not None:
            sys.stderr = self._saved_stderr
        if self._sink is not None:
            try:
                self._sink.close()
            except Exception:
                pass
            self._sink = None


class _NoOpProgressCtx:
    """Stand-in for `rich.progress.Progress` used when --json mode is
    active. Mimics the small surface that callers use (`add_task`,
    `update`, `remove_task`, context-manager protocol) and does
    nothing. Avoids opening a Rich Live region, which on Linux under
    Click's CliRunner leaves Click's stdout wiring poisoned."""

    def __enter__(self) -> "_NoOpProgressCtx":
        return self

    def __exit__(self, *_exc: Any) -> None:
        pass

    def add_task(self, *_args: Any, **_kwargs: Any) -> int:
        return 0

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def remove_task(self, *_args: Any, **_kwargs: Any) -> None:
        pass


def _no_op_progress(_console: Any) -> _NoOpProgressCtx:
    """Drop-in replacement for `make_phase_progress` while --json mode
    is active. See `_NoOpProgressCtx`."""
    return _NoOpProgressCtx()


def _emit_json_success(command: str, result: Any) -> None:
    """Emit a v1 success envelope to stdout: {version, command, result}.

    Uses indent=2 + sort_keys=False + ensure_ascii=False so the output is
    human-readable, diff-friendly, and preserves UTF-8 paths verbatim.
    Uses click.echo so Click's CliRunner captures the output identically
    on macOS and Linux. (sys.stdout.write does not reliably reach the
    runner's captured buffer on Linux under Click 8.3.)
    """
    doc = {
        "version": JSON_SCHEMA_VERSION,
        "command": command,
        "result": result,
    }
    click.echo(_json.dumps(doc, indent=2, sort_keys=False, ensure_ascii=False))


def _emit_json_error(command: str, exc: BaseException) -> None:
    """Emit a v1 error envelope to stderr: {version, command, error},
    then exit 1. `exc.__class__.__name__` is the `error.type`. Uses
    click.echo(err=True) for the same Linux/CliRunner reason as
    _emit_json_success."""
    doc = {
        "version": JSON_SCHEMA_VERSION,
        "command": command,
        "error": {
            "type": exc.__class__.__name__,
            "message": str(exc),
        },
    }
    click.echo(
        _json.dumps(doc, indent=2, sort_keys=False, ensure_ascii=False),
        err=True,
    )
    sys.exit(1)


def _resolve_config(config_path: str) -> str:
    """
    If config_path is explicitly provided, use it.
    Otherwise, auto-detect by matching the current directory against known configs,
    falling back to default.yaml — same logic as `claude-mirror find-config`.
    """
    if config_path:
        return config_path
    target = Path.cwd().resolve()
    for config_file in sorted(CONFIG_DIR.glob("*.yaml")):
        try:
            cfg = Config.load(str(config_file))
            if Path(cfg.project_path).resolve() == target:
                return str(config_file)
        except Exception:
            continue
    return DEFAULT_CONFIG


def _try_reload_watcher() -> None:
    """Send SIGHUP to any running watch-all process so it picks up new configs."""
    import subprocess as _sp
    result = _sp.run(["pgrep", "-f", "claude-mirror watch-all"], capture_output=True, text=True)
    pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip() and p.strip() != str(os.getpid())]
    if pids:
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
        console.print("[dim]Watcher reloaded to pick up the new config.[/]")


# Commands that should NOT print the "watcher is not running" warning:
#   * commands that manage the watcher itself (watch, watch-all, reload)
#   * commands that set things up before the watcher would even make sense
#     (init, auth, find-config, test-notify)
#   * inbox — called silently by the Claude Code PreToolUse hook on every tool
#     call; printing here would flood the conversation
_NO_WATCHER_CHECK_CMDS = {
    "watch", "watch-all", "reload",
    "init", "auth",
    "find-config", "test-notify",
    "inbox",
    # `doctor` is diagnosing setup health — printing a "watcher not running"
    # warning on top of doctor's own checks would be redundant noise; doctor
    # also gets called specifically when the user already suspects something
    # is wrong, so the watcher hint isn't useful here.
    "doctor",
    # `prune` and `diff` are read-mostly housekeeping — the watcher hint
    # is unrelated and just adds noise to the rendered output.
    "prune", "diff",
    # `seed-mirror` is one-shot mirror initialization; the watcher hint
    # is unrelated and would distract from the seed summary.
    "seed-mirror",
    # `profile` (since v0.5.49) is a credentials-registry management
    # group — list/show/create/delete operate on profile YAMLs, never
    # on a project, so the watcher hint is unrelated noise.
    "profile",
}


def _check_watcher_running(cmd_name: str) -> None:
    """Warn the user if the background watcher isn't running.

    Real-time notifications (Pub/Sub for Drive, longpoll for Dropbox, polling
    for OneDrive/WebDAV) require `claude-mirror watch-all` to be running. If
    it's not, the user will only ever see remote changes when they manually
    run `status` / `sync` — they'll miss the live notification flow.

    Best-effort: if pgrep is missing (Windows, minimal containers, etc.) we
    silently skip the check rather than ever blocking a command.
    """
    if cmd_name in _NO_WATCHER_CHECK_CMDS:
        return
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-f", "claude-mirror watch-all"],
            capture_output=True, text=True, timeout=2,
        )
    except Exception:
        return
    pids = [p for p in result.stdout.strip().splitlines() if p.strip()]
    if pids:
        return
    console.print(
        "[yellow]⚠[/]  [dim]watcher not running — you won't get real-time "
        "notifications from collaborators.[/]\n"
        "   [dim]start it:[/] [bold]claude-mirror watch-all[/]  "
        "[dim](or run[/] [bold]claude-mirror-install[/] "
        "[dim]for auto-start at login)[/]"
    )


def _create_storage(config: Config) -> StorageBackend:
    """Factory: create the storage backend based on config.backend."""
    backend = config.backend
    if backend == "googledrive":
        return GoogleDriveBackend(config)
    if backend == "dropbox":
        from .backends.dropbox import DropboxBackend
        return DropboxBackend(config)
    if backend == "onedrive":
        from .backends.onedrive import OneDriveBackend
        return OneDriveBackend(config)
    if backend == "webdav":
        from .backends.webdav import WebDAVBackend
        return WebDAVBackend(config)
    if backend == "sftp":
        from .backends.sftp import SFTPBackend
        return SFTPBackend(config)
    raise ValueError(f"Unknown storage backend: {backend}")


def _create_storage_set(config: Config) -> tuple[StorageBackend, list[StorageBackend]]:
    """Tier 2 multi-backend factory.

    Returns (primary, mirrors). For single-backend projects (the v0.3.x
    behaviour), `mirrors` is empty and the caller treats `primary` as the
    only target — no behaviour change.

    For multi-backend projects, each entry in `mirror_config_paths` is a
    YAML config that:
      - shares the same `project_path` as the primary config
      - has its own `backend`, credentials, folder, and token files
      - represents one additional storage target

    Each mirror config is loaded independently so credentials / folder
    IDs / tokens stay isolated. The orchestrator (SyncEngine) iterates
    over `[primary] + mirrors` for push/sync/snapshot operations.

    Validation:
      - Every mirror must point at the same project_path as primary.
        We compare the resolved real path AND the inode number (st_ino)
        of the primary's project_path to defeat symlink-TOCTOU: a
        symlink can be swapped between Path.resolve() and the actual
        filesystem touch, so a string-only comparison can be tricked
        into running the mirror against a different tree. Inodes are
        stable for a given file across the lifetime of that file
        (atomic-rename of an entire directory does change inode, which
        is detectable here — and the user would notice a renamed
        project root anyway). We still keep the resolved-string check
        as a sanity layer in case the filesystem doesn't expose stable
        inodes (unlikely, but documented for future maintainers).
      - The (backend_name, credentials_file or token_file) tuple must
        be unique across primary + mirrors. Two same-backend entries
        ARE allowed when they point at different accounts (work +
        personal Google Drive, redundant Dropbox accounts, etc.) —
        what's NOT allowed is literal duplicates that would write to
        the same account, since that's just a config bug, not a mirror.
    """
    primary = _create_storage(config)
    mirrors: list[StorageBackend] = []
    if not config.mirror_config_paths:
        return primary, mirrors

    primary_resolved_path = Path(config.project_path).expanduser().resolve()
    primary_path = str(primary_resolved_path)
    # Inode of the resolved primary project_path. If the filesystem
    # doesn't support stat (extremely unusual) we fall back to None and
    # the inode check is skipped — string comparison still applies.
    try:
        primary_inode: int | None = os.stat(primary_resolved_path).st_ino
    except OSError:
        primary_inode = None

    primary_creds_key = (
        config.credentials_file or config.token_file or ""
    )
    seen_identity_keys: set[tuple[str, str]] = {
        (primary.backend_name or config.backend, primary_creds_key)
    }
    for mirror_path in config.mirror_config_paths:
        try:
            mirror_cfg = Config.load(_resolve_config(mirror_path) if not Path(mirror_path).is_absolute() else mirror_path)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"mirror_config_paths entry not found: {mirror_path} ({e})"
            ) from e
        # Same-project guard — string check (sanity)
        mirror_resolved_path = Path(mirror_cfg.project_path).expanduser().resolve()
        mirror_resolved = str(mirror_resolved_path)
        if mirror_resolved != primary_path:
            raise RuntimeError(
                f"mirror config {mirror_path!r} points at {mirror_resolved!r} "
                f"but primary config points at {primary_path!r}. "
                f"All mirror configs must share the same project_path."
            )
        # Same-project guard — inode check (defeats symlink TOCTOU,
        # where the symlink is swapped between resolve() and use).
        if primary_inode is not None:
            try:
                mirror_inode = os.stat(mirror_resolved_path).st_ino
            except OSError as e:
                raise RuntimeError(
                    f"mirror config {mirror_path!r} project_path "
                    f"{mirror_resolved!r} could not be stat()ed ({e}). "
                    f"Refusing to proceed — cannot verify it points at "
                    f"the same on-disk inode as the primary project."
                ) from e
            if mirror_inode != primary_inode:
                raise RuntimeError(
                    f"mirror config {mirror_path!r} resolves to inode "
                    f"{mirror_inode} but primary resolves to inode "
                    f"{primary_inode}. The string paths matched, but the "
                    f"underlying filesystem objects are different — "
                    f"likely a symlink was swapped or the path was "
                    f"replaced. Refusing to mirror to an unrelated tree."
                )
        # Identity guard: same-backend with different credentials is OK
        # (legitimate dual-account redundancy: e.g. two googledrive
        # mirrors targeting work + personal accounts). What we reject
        # is duplicates — same backend AND same credentials/token file
        # path — since that would write twice to the same account, a
        # wasted call rather than a real mirror.
        backend_name = mirror_cfg.backend
        creds_key = mirror_cfg.credentials_file or mirror_cfg.token_file or ""
        identity_key = (backend_name, creds_key)
        if identity_key in seen_identity_keys:
            raise RuntimeError(
                f"Two backends with backend_name={backend_name!r} AND "
                f"identical credentials_file/token_file ({creds_key!r}) "
                f"would write to the same account — that's not a mirror, "
                f"just a duplicate. To mirror to a second account of the "
                f"same backend, point its mirror config at a different "
                f"credentials_file / token_file."
            )
        seen_identity_keys.add(identity_key)
        # Sandbox each mirror's credential / token paths under the
        # claude-mirror config directory or the user's home. A malicious
        # mirror config that specifies `token_file: /etc/launchd.plist`
        # would otherwise become an arbitrary-write primitive on the
        # next auth/refresh — the backend's `write_token_secure` writes
        # 0600 token JSON to whatever path is configured. Restricting
        # to ~/.config/claude_mirror/ (the only place the wizard ever
        # writes) closes that door without affecting any legitimate
        # setup.
        _validate_mirror_paths(mirror_path, config, mirror_cfg)
        mirrors.append(_create_storage(mirror_cfg))
    return primary, mirrors


def _validate_mirror_paths(
    source_label: str, primary_cfg: Config, mirror_cfg: Config,
) -> None:
    """Refuse mirror configs whose token_file / credentials_file resolve
    outside the claude-mirror config directory. This prevents a malicious
    mirror YAML from silently turning future auth/refresh flows into
    an arbitrary-write primitive.

    Also reject mirror configs that themselves declare
    `mirror_config_paths` — chained mirrors aren't supported (and would
    risk infinite recursion during `_create_storage_set`).

    Finally, warn (don't reject) when the mirror's `file_patterns` or
    `exclude_patterns` differ from the primary's. The orchestrator only
    consults the primary's patterns when deciding what to fan out, so a
    mirror's divergent values would be silently ignored — surfacing the
    drift gives the user a chance to reconcile the YAMLs.

    `source_label` is the original config path string used by the user
    (for error messages); `primary_cfg` is the primary's Config;
    `mirror_cfg` is the loaded mirror Config dataclass.
    """
    config_root = CONFIG_DIR.resolve()
    for field_name in ("token_file", "credentials_file"):
        raw = getattr(mirror_cfg, field_name, "") or ""
        if not raw:
            continue
        resolved = Path(raw).expanduser().resolve()
        try:
            resolved.relative_to(config_root)
        except ValueError:
            raise RuntimeError(
                f"mirror config {source_label!r} declares {field_name}="
                f"{raw!r}, which resolves to {resolved!r} — outside the "
                f"claude-mirror config directory ({config_root}). For "
                f"safety, mirror configs may only place token / "
                f"credential files under {config_root}. Edit the mirror "
                f"YAML and move the file under that directory before "
                f"continuing."
            )

    # Chained-mirror guard: a mirror must not itself declare mirrors.
    if mirror_cfg.mirror_config_paths:
        raise RuntimeError(
            f"mirror config {source_label!r} has its own "
            f"mirror_config_paths; chained mirrors aren't supported."
        )

    # Pattern-drift warnings (non-fatal): only the primary's patterns
    # drive fan-out, so divergent mirror patterns would be silently
    # ignored.
    if list(mirror_cfg.file_patterns) != list(primary_cfg.file_patterns):
        console.print(
            f"[yellow]Warning:[/] mirror {source_label} has different "
            f"file_patterns than primary; mirror's are silently ignored."
        )
    if list(mirror_cfg.exclude_patterns) != list(primary_cfg.exclude_patterns):
        console.print(
            f"[yellow]Warning:[/] mirror {source_label} has different "
            f"exclude_patterns than primary; mirror's are silently ignored."
        )


def _create_notifier(config: Config, storage: StorageBackend) -> NotificationBackend | None:
    """Factory: create the notification backend based on config.backend."""
    backend = config.backend
    if backend == "googledrive":
        try:
            creds = storage.get_credentials()
            return PubSubNotifier(config, creds)
        except Exception:
            console.print("[yellow]Warning: Pub/Sub unavailable. Notifications disabled.[/]")
            return None
    if backend == "dropbox":
        from .notifications.longpoll import DropboxLongPollNotifier
        try:
            dbx = storage.get_credentials()
            return DropboxLongPollNotifier(config, dbx)
        except Exception:
            console.print("[yellow]Warning: Dropbox longpoll unavailable. Notifications disabled.[/]")
            return None
    if backend == "onedrive":
        from .notifications.polling import PollingNotifier
        return PollingNotifier(config, storage)
    if backend == "webdav":
        from .notifications.polling import PollingNotifier
        return PollingNotifier(config, storage)
    return None


def _load_engine(config_path: str, with_pubsub: bool = True) -> tuple[SyncEngine, Config, StorageBackend]:
    config = Config.load(config_path)
    storage, mirrors = _create_storage_set(config)
    manifest = Manifest(config.project_path)
    merge = MergeHandler()
    snap = SnapshotManager(config, storage, mirrors=mirrors)

    # Prune orphan per-backend manifest state for mirrors no longer in
    # this project's config. Without this, a removed mirror's
    # pending_retry entries linger forever, never retried, never
    # surfaced. We rely on the next normal manifest write to persist —
    # don't auto-save here.
    active_backends = {storage.backend_name} | {
        m.backend_name for m in mirrors
    }
    pruned = manifest.prune_unknown_backends(active_backends)
    if pruned > 0:
        console.print(
            f"[dim]Cleaned {pruned} orphan mirror state entry(ies) from "
            f"manifest (mirrors no longer in config).[/]"
        )

    notifier = None
    if with_pubsub:
        notifier = _create_notifier(config, storage)

    engine = SyncEngine(config, storage, manifest, merge, notifier, snap, mirrors=mirrors)
    return engine, config, storage


_AUTH_KEYWORDS = ("auth", "token", "credential", "authenticated", "refresh", "oauth")


def _sanitise_auth_msg(msg: str) -> str:
    """Strip misleading gcloud / ADC suggestions from a Google auth exception
    string before showing it to the user.

    Google's auth libraries sometimes recommend `gcloud auth application-default
    login` when a refresh fails — that command is for Application Default
    Credentials, which claude-mirror does NOT use (we authenticate via the
    OAuth2 flow against the credentials.json the user provided to
    `claude-mirror auth`). Showing it confuses users into running the wrong
    command.

    Replace any such suggestion with the correct claude-mirror command, and
    strip parenthetical follow-up reauth advice from the same library.
    """
    if not msg:
        return ""
    replacements = [
        ("gcloud auth application-default login", "claude-mirror auth"),
        ("gcloud auth login", "claude-mirror auth"),
        ("Please run `claude-mirror auth` to reauthenticate.",
         "Run `claude-mirror auth` to reauthenticate."),
    ]
    out = msg
    for src, dst in replacements:
        out = out.replace(src, dst)
    return out


class _CLIGroup(click.Group):
    """Click group that intercepts auth errors and shows a clean message."""

    def invoke(self, ctx: click.Context) -> object:
        # Health check: warn if the background watcher isn't running, so the
        # user knows they won't see live notifications until they start it.
        # `ctx.invoked_subcommand` is NOT yet populated at this point — Click
        # only assigns it inside Group.invoke (which is what super().invoke
        # calls below). So parse the subcommand from protected_args ourselves.
        args = list(ctx.protected_args) + list(ctx.args)
        sub_cmd = args[0] if args else ""
        # When --json is anywhere in argv, suppress the pre-subcommand
        # banners (watcher warning + update-check notice). Both write to
        # stdout via Rich; in --json mode stdout is reserved for the
        # JSON document, and any other content corrupts it for jq /
        # script consumers. The diagnostic dump from v0.5.43 confirmed
        # the watcher banner was leaking into stdout ahead of the JSON
        # envelope on Linux.
        json_mode = "--json" in args
        if not json_mode:
            _check_watcher_running(sub_cmd)
            # Update check (best-effort, silent on any failure). Skipped for
            # the silent inbox path (PreToolUse hook) so it doesn't print an
            # "update available" line into the Claude Code conversation.
            if sub_cmd not in _NO_WATCHER_CHECK_CMDS:
                try:
                    from ._update_check import check_for_update
                    check_for_update(notify_desktop=False)
                except Exception:
                    pass  # never let update-check break a command
        # When the user explicitly invokes `auth`, every "fix: run claude-mirror auth"
        # message below would be a logical infinite-loop ("you ran auth; to fix it,
        # run auth"). Detect that case once and route to a different diagnostic.
        is_auth_cmd = (sub_cmd == "auth")

        def _auth_fix_hint() -> str:
            return (
                "[yellow]Fix:[/] run [bold]claude-mirror auth[/] to reauthenticate."
                if not is_auth_cmd
                else "[yellow]The OAuth flow itself failed.[/] Things to check:\n"
                     "  • Network reachability of accounts.google.com / oauth2.googleapis.com\n"
                     "  • That your [bold]credentials_file[/] (OAuth client JSON) is valid and unrevoked\n"
                     "  • That the local OAuth callback can bind to a free port\n"
                     "  • Re-run with [bold]CLAUDE_MIRROR_AUTH_VERBOSE=1[/] for refresh-attempt logs\n"
                     "  • If running on a headless machine, set up a port-forwarded OAuth flow"
            )

        try:
            return super().invoke(ctx)
        except google.auth.exceptions.RefreshError as e:
            if is_auth_cmd:
                # User is already running `auth`. The auth command moves the old
                # token aside before calling authenticate(), so a RefreshError
                # here means the OAuth flow itself triggered a refresh somehow —
                # genuinely unusual. Surface the raw error.
                console.print(
                    "\n[red bold]OAuth flow failed during a refresh step:[/]\n"
                    f"[dim]{_sanitise_auth_msg(str(e))}[/]\n\n"
                    + _auth_fix_hint()
                )
            else:
                console.print(
                    "\n[red bold]Authentication error:[/] your Google token has "
                    "expired or been revoked.\n"
                    f"[dim]{_sanitise_auth_msg(str(e))}[/]\n\n"
                    + _auth_fix_hint()
                )
            sys.exit(1)
        except google.auth.exceptions.TransportError as e:
            console.print(
                "\n[red bold]Network error during authentication:[/]\n"
                f"[dim]{_sanitise_auth_msg(str(e))}[/]\n\n"
                "Check your internet connection and try again."
            )
            sys.exit(1)
        except google.auth.exceptions.DefaultCredentialsError as e:
            # ADC isn't what claude-mirror uses, but some library paths inside
            # google-cloud-pubsub / google-api-python-client occasionally
            # surface this when an explicit credential isn't visible to the
            # client constructor — it always means "auth setup is missing
            # or stale" from the user's point of view.
            console.print(
                "\n[red bold]Authentication setup is missing or stale.[/]\n"
                f"[dim]{_sanitise_auth_msg(str(e))}[/]\n\n"
                + (
                    "[yellow]Fix:[/] run [bold]claude-mirror auth[/] to set up or "
                    "refresh authentication for this project."
                    if not is_auth_cmd
                    else _auth_fix_hint()
                )
            )
            sys.exit(1)
        except google.auth.exceptions.GoogleAuthError as e:
            # Catch-all for any other google.auth subclass not handled above
            # (UserAccessTokenError, ReauthFailError, etc.).
            console.print(
                "\n[red bold]Authentication error:[/]\n"
                f"[dim]{_sanitise_auth_msg(str(e))}[/]\n\n"
                + _auth_fix_hint()
            )
            sys.exit(1)
        except RuntimeError as e:
            msg = str(e)
            if any(kw in msg.lower() for kw in _AUTH_KEYWORDS):
                if is_auth_cmd:
                    # Auth command itself raised an auth-keyword RuntimeError —
                    # likely an OAuth flow error (Dropbox flow.finish, OneDrive
                    # device flow, WebDAV 401 on test connection). Surface it
                    # verbatim with diagnostic hints, NOT the run-auth loop.
                    console.print(
                        f"\n[red bold]OAuth flow failed:[/] {_sanitise_auth_msg(msg)}\n\n"
                        + _auth_fix_hint()
                    )
                else:
                    console.print(
                        f"\n[red bold]Authentication error:[/] {_sanitise_auth_msg(msg)}\n\n"
                        + _auth_fix_hint()
                    )
                sys.exit(1)
            raise
        except FileNotFoundError as e:
            msg = str(e)
            filename = getattr(e, "filename", None) or ""
            # No claude-mirror config for this directory — guide the user toward init.
            if filename.endswith(".yaml"):
                cwd = Path.cwd()
                console.print(
                    f"\n[red bold]No claude-mirror config found for this directory.[/]\n"
                    f"[dim]Current directory:[/] {cwd}\n"
                    f"[dim]Looked for:[/]        {filename}\n\n"
                    "[yellow]Fix:[/] cd into a configured project, or run "
                    "[bold]claude-mirror init --wizard[/] to set one up here."
                )
                sys.exit(1)
            if any(kw in msg.lower() for kw in ("credentials", "token")):
                console.print(
                    f"\n[red bold]Credentials file not found:[/] {msg}\n\n"
                    + (
                        "[yellow]Fix:[/] run [bold]claude-mirror auth[/] to set up authentication."
                        if not is_auth_cmd
                        else "[yellow]Cause:[/] the [bold]credentials_file[/] in your config "
                             "points at a path that doesn't exist on disk. Verify the path "
                             "and re-run [bold]claude-mirror auth[/]."
                    )
                )
                sys.exit(1)
            raise
        except (OSError, ConnectionError) as e:
            # Catches socket.gaierror (DNS lookup failure),
            # ConnectionRefusedError, ConnectionResetError, socket.timeout
            # — all of these subclass OSError. The earlier
            # `except FileNotFoundError` clause caught file-not-found
            # cases (also OSError subclass) so by the time we get here,
            # any OSError is genuinely a network/IO failure rather than a
            # missing-file config issue. Convert the 100-line traceback
            # into a clean message + fix hint.
            import errno
            errno_str = errno.errorcode.get(getattr(e, "errno", 0), "")
            console.print(
                "\n[red bold]Could not reach the storage backend.[/]\n"
                f"[dim]Underlying error:[/] {type(e).__name__}"
                + (f" ({errno_str})" if errno_str else "")
                + f": {redact_error(str(e))}\n\n"
                "[yellow]Fix:[/] check your network connectivity, then retry. "
                "If the problem persists, run [bold]claude-mirror doctor[/] "
                "to diagnose the configured backend."
            )
            sys.exit(1)
        except Exception as e:
            # Last-resort handler for library-specific network errors that
            # do NOT subclass OSError. We match by type-name string so a
            # missing vendor package doesn't break the handler at import.
            #
            # Known cases to catch cleanly:
            #   * httplib2.error.ServerNotFoundError — Drive DNS-failed wrapper
            #   * requests.exceptions.ConnectionError — Dropbox/OneDrive/WebDAV
            #   * urllib3 connection errors
            #   * paramiko SSH connection errors
            type_name = type(e).__module__ + "." + type(e).__name__
            network_indicators = (
                "httplib2.error.ServerNotFoundError",
                "httplib2.ServerNotFoundError",
                "requests.exceptions.ConnectionError",
                "urllib3.exceptions.NewConnectionError",
                "urllib3.exceptions.MaxRetryError",
                "paramiko.ssh_exception.NoValidConnectionsError",
            )
            if any(ind in type_name for ind in network_indicators):
                console.print(
                    "\n[red bold]Could not reach the storage backend.[/]\n"
                    f"[dim]Underlying error:[/] {type_name}: {redact_error(str(e))}\n\n"
                    "[yellow]Fix:[/] check your network connectivity, then retry. "
                    "If the problem persists, run [bold]claude-mirror doctor[/] "
                    "to diagnose the configured backend."
                )
                sys.exit(1)
            # Non-network unknown exception — let Click handle it normally
            # (exit 1, traceback only if --traceback is set).
            raise


@click.group(cls=_CLIGroup)
@click.version_option()
@click.option(
    "--profile", "profile_name", default="", metavar="NAME",
    help=(
        "Apply credentials profile NAME (~/.config/claude_mirror/profiles/"
        "NAME.yaml) on top of the project config. Profile values supply "
        "credentials/identity fields the project YAML omits; project values "
        "win when both define the same field. See `claude-mirror profile --help`."
    ),
)
def cli(profile_name: str) -> None:
    """Sync Claude project MD files across machines via cloud storage."""
    # Stash the resolved profile name in a module-level slot so every
    # `Config.load` call downstream picks it up without each subcommand
    # having to thread the argument through. See
    # `claude_mirror.config.set_global_profile_override` for the contract.
    if profile_name:
        # Validate up-front: fail fast if the named profile doesn't
        # exist, with the same helpful list of available profiles that
        # `load_profile` produces. Doing this here means a typo on the
        # global flag aborts before the subcommand starts work.
        from .profiles import load_profile
        try:
            load_profile(profile_name)
        except FileNotFoundError as e:
            console.print(f"[red]✗ {e}[/]")
            sys.exit(1)
    from .config import set_global_profile_override
    set_global_profile_override(profile_name)


_DEFAULT_CREDENTIALS = str(CONFIG_DIR / "credentials.json")


def _derive_token_file(credentials_file: str) -> str:
    """Derive token filename from credentials filename.
    work-credentials.json → work-token.json
    credentials.json      → token.json
    myapp.json            → myapp-token.json
    """
    p = Path(credentials_file)
    stem = p.stem
    if stem.endswith("-credentials"):
        token_stem = stem[: -len("-credentials")] + "-token"
    elif stem == "credentials":
        token_stem = "token"
    else:
        token_stem = stem + "-token"
    return str(p.parent / f"{token_stem}.json")


def _derive_config_path(project_path: str) -> str:
    """Derive config filename from the project directory name."""
    project_name = Path(project_path).name
    return str(CONFIG_DIR / f"{project_name}.yaml")


def _maybe_run_drive_smoke_test(
    *,
    credentials_file: str,
    token_file: str,
    drive_folder_id: str,
) -> Optional[Any]:
    """Offer to authenticate now and run a `drive.files.list` smoke test
    against `drive_folder_id` BEFORE the wizard returns and the YAML is
    written. Default Yes; on No we skip silently — the user may be
    configuring offline.

    Returns the OAuth credentials object on smoke-test pass so a
    follow-up step (e.g. `--auto-pubsub-setup`, since v0.5.47) can
    reuse them without prompting the user for a second OAuth flow.
    Returns None on every other path (user declined, auth raised,
    smoke test failed and user declined retry).

    On smoke-test failure we print the classified reason and ask whether
    to retry the auth flow. Three failure paths:
      * user accepts retry  → re-run authenticate(), re-run smoke test
      * user declines retry → print yellow warning, return (YAML still writes)
      * the auth flow itself errors → print warning, return (YAML still writes)

    The token file is written by `authenticate()` as a side effect; on
    success the user can run `claude-mirror push` immediately. On the
    declined-retry path the token file may still hold valid creds
    (smoke-test failure does NOT invalidate auth), so the user can fix
    the underlying issue (enable Drive API, share the folder) without
    re-authenticating.
    """
    if not click.confirm(
        "\nAuthenticate and run a Drive smoke test now? "
        "(catches Drive-API-not-enabled, wrong-project credentials.json, "
        "and folder-not-shared errors)",
        default=True,
    ):
        return None

    # Build a minimal in-memory Config for the backend — we reuse the
    # full GoogleDriveBackend.authenticate() so the OAuth flow, scopes,
    # and token-file write match the production path exactly.
    smoke_config = Config(
        project_path=str(Path.cwd()),
        backend="googledrive",
        credentials_file=credentials_file,
        token_file=token_file,
        drive_folder_id=drive_folder_id,
        gcp_project_id="",
        pubsub_topic_id="",
        file_patterns=["**/*.md"],
    )

    while True:
        try:
            backend_instance = GoogleDriveBackend(smoke_config)
            console.print("\n[dim]Opening browser for Google sign-in…[/]")
            creds = backend_instance.authenticate()
        except Exception as e:
            console.print(
                f"[yellow]⚠ Authentication failed: {e}[/]\n"
                f"[yellow]Skipping smoke test. Run `claude-mirror auth` "
                f"after the YAML is saved to retry.[/]"
            )
            return None

        result = _byo_wizard.run_drive_smoke_test(creds, drive_folder_id)
        if result.ok:
            console.print(
                "[green]✓ Drive smoke test passed.[/] "
                "Auth complete; folder is reachable."
            )
            return creds

        console.print(
            f"\n[yellow]⚠ Drive smoke test failed.[/]\n"
            f"  Reason: {result.reason}"
        )
        if not click.confirm(
            "Retry authentication (e.g. after enabling the Drive API "
            "or sharing the folder)?",
            default=True,
        ):
            console.print(
                "[yellow]Skipping smoke test. The YAML will still be "
                "written. Fix the issue and run `claude-mirror auth` "
                "(or push) to verify.[/]"
            )
            return None
        # Loop: retry auth + smoke test.


def _maybe_auto_setup_pubsub(
    *,
    creds: Any,
    gcp_project_id: str,
    pubsub_topic_id: str,
    machine_name: str,
) -> None:
    """Run the v0.5.47 `--auto-pubsub-setup` step: idempotently create
    the Pub/Sub topic + per-machine subscription + IAM grant for Drive's
    push-notification service account, using the OAuth credentials we
    just acquired in the smoke-test phase.

    Renders a Rich-formatted summary of what changed (or was already in
    place) so the user sees exactly which resources the wizard touched.
    Failures are printed as yellow warnings — the wizard does NOT abort;
    the YAML still writes, and the user can either fix the underlying
    cause and re-run `init --auto-pubsub-setup`, or fix it via the GCP
    console and re-run `doctor` to verify.
    """
    console.print("\n[bold]Pub/Sub auto-setup:[/]")
    try:
        result = _byo_wizard.auto_setup_pubsub(
            creds=creds,
            gcp_project_id=gcp_project_id,
            pubsub_topic_id=pubsub_topic_id,
            machine_name=machine_name,
        )
    except Exception as exc:  # noqa: BLE001 — defensive: never abort wizard
        console.print(
            f"  [yellow]⚠ Pub/Sub auto-setup raised "
            f"({type(exc).__name__}): {exc}[/]\n"
            f"  [yellow]Skipping. The YAML will still be written; fix "
            f"the underlying cause and re-run "
            f"[bold]claude-mirror init --auto-pubsub-setup[/] or "
            f"configure the topic via the GCP console.[/]"
        )
        return

    if result.skipped:
        # Pub/Sub OAuth scope wasn't granted — print one yellow line
        # and stop. The smoke test already confirmed the Drive scope
        # works; the user just opted out of real-time notifications.
        console.print(f"  [yellow]⚠[/] {result.reason}")
        return

    topic_path = (
        f"projects/{gcp_project_id}/topics/{pubsub_topic_id}"
    )
    safe_machine = (
        (machine_name or "").replace(".", "-").replace(" ", "-").lower()
    )
    subscription_id = f"{pubsub_topic_id}-{safe_machine}"

    # Topic line
    if result.topic_created:
        console.print(
            f"  [green]✓[/] Topic created                       "
            f"[dim]{topic_path}[/]"
        )
    else:
        console.print(
            f"  [green]✓[/] Topic exists                        "
            f"[dim]{topic_path}[/]"
        )

    # Subscription line
    if result.subscription_created:
        console.print(
            f"  [green]✓[/] Subscription created for {machine_name}  "
            f"[dim]{subscription_id}[/]"
        )
    else:
        console.print(
            f"  [green]✓[/] Subscription exists for {machine_name}   "
            f"[dim]{subscription_id}[/]"
        )

    # IAM grant line
    if result.iam_grant_added:
        console.print(
            f"  [green]✓[/] IAM grant added                     "
            f"[dim]{_DRIVE_PUBSUB_PUBLISHER_SA} → "
            f"roles/pubsub.publisher[/]"
        )
    else:
        console.print(
            f"  [green]✓[/] IAM grant already present           "
            f"[dim]{_DRIVE_PUBSUB_PUBLISHER_SA} → "
            f"roles/pubsub.publisher[/]"
        )

    if result.failures:
        console.print(
            "\n  [yellow]⚠ Pub/Sub auto-setup had partial failures:[/]"
        )
        for step, msg in result.failures:
            console.print(f"    [yellow]•[/] [bold]{step}[/]: {msg}")
        console.print(
            "  [yellow]The YAML will still be written. Fix the "
            "underlying cause and re-run "
            "[bold]claude-mirror init --auto-pubsub-setup[/], or "
            "configure the missing piece via the GCP console and "
            "verify with [bold]claude-mirror doctor --backend "
            "googledrive[/].[/]"
        )


def _run_wizard(
    backend_default: str = "googledrive",
    *,
    auto_pubsub_setup: bool = False,
    profile_data: Optional[dict] = None,
) -> dict:
    """Interactive wizard that collects all init parameters. Returns a dict of values.

    `backend_default` is the storage backend pre-filled in the first prompt.
    The caller passes the value of the `--backend` CLI flag so that
    `claude-mirror init --wizard --backend sftp` shows `[sftp]` as the
    default rather than the unconditional `[googledrive]`.

    `auto_pubsub_setup` (since v0.5.47) — when True AND the wizard's
    smoke test passes AND the chosen backend is googledrive, the
    wizard chains the smoke test directly into the v0.5.47 auto-setup
    helper, creating the Pub/Sub topic, per-machine subscription, and
    Drive's IAM grant on the topic without a second OAuth flow. Off by
    default (additive behaviour); ignored on non-googledrive backends.

    `profile_data` (since v0.5.49) — when set, every credential-bearing
    field already supplied by the named profile (`credentials_file`,
    `token_file`, `dropbox_app_key`, etc.) is SKIPPED in the prompt
    sequence. The wizard only collects project-specific fields
    (`drive_folder_id`, `dropbox_folder`, `sftp_folder`, ...) and the
    returned dict carries the profile-supplied values verbatim so the
    caller can include them in the eventual Config (or, when writing
    the YAML with `profile: NAME` reference, ignore them and let the
    profile re-supply them at load time).
    """
    profile_data = dict(profile_data or {})
    console.print("\n[bold cyan]claude-mirror setup wizard[/]\n")
    console.print("Press Enter to accept the [dim]default[/] shown in brackets.\n")

    _SUPPORTED_BACKENDS = ("googledrive", "dropbox", "onedrive", "webdav", "sftp")

    # Backend
    console.print(
        f"[dim]Storage backend: {' | '.join(_SUPPORTED_BACKENDS)}[/]"
    )
    backend = click.prompt("Storage backend", default=backend_default)
    if backend not in _SUPPORTED_BACKENDS:
        console.print(f"[red]Backend '{backend}' is not supported.[/]")
        sys.exit(1)
    console.print()

    # Project path
    default_project = str(Path.cwd())
    raw_project = click.prompt("Project directory", default=default_project)
    project_path = str(Path(raw_project).expanduser().resolve())
    if not Path(project_path).exists():
        console.print(f"[red]Path does not exist: {project_path}[/]")
        sys.exit(1)

    project_name = Path(project_path).name

    # Backend-specific fields
    drive_folder_id = ""
    gcp_project_id = ""
    pubsub_topic_id = ""
    credentials_file = ""
    dropbox_app_key = ""
    dropbox_folder = ""
    onedrive_client_id = ""
    onedrive_folder = ""
    webdav_url = ""
    webdav_username = ""
    webdav_password = ""
    webdav_insecure_http = False
    sftp_host = ""
    sftp_port = 22
    sftp_username = ""
    sftp_key_file = ""
    sftp_password = ""
    sftp_known_hosts_file = "~/.ssh/known_hosts"
    sftp_strict_host_check = True
    sftp_folder = ""
    poll_interval = 30  # default; only meaningful for onedrive/webdav/sftp

    # ── Profile pre-fill banner ───────────────────────────────────────
    # When the wizard runs under `--profile NAME`, announce which fields
    # the profile is going to supply so the user knows why some prompts
    # are skipped further down.
    if profile_data:
        supplied = sorted(
            k for k in profile_data
            if k not in ("backend", "description") and profile_data[k]
        )
        if supplied:
            console.print(
                "[dim]Profile-supplied fields (will be skipped): "
                f"{', '.join(supplied)}[/]\n"
            )

    if backend == "googledrive":
        # ── GCP project ID FIRST ────────────────────────────────────────
        # Asked before everything else in the Drive flow so we can
        # template project-scoped Cloud Console URLs and offer to open
        # them. The validator reasserts the GCP project-ID rules
        # (lowercase letter start, 6-30 chars, hyphens/digits ok) at
        # the prompt — typos no longer cascade into a mysterious first-
        # sync failure later.
        if profile_data.get("gcp_project_id"):
            gcp_project_id = profile_data["gcp_project_id"]
            console.print(
                f"[dim]GCP project ID supplied by profile:[/] {gcp_project_id}\n"
            )
        else:
            console.print(
                "\n[dim]GCP project ID: found in Google Cloud Console → project selector "
                "(e.g. my-project-123). If you don't have one yet, hit Ctrl-C, create one "
                f"at {_byo_wizard.project_create_url()} and re-run the wizard.[/]\n"
            )
            gcp_project_id = click.prompt(
                "GCP project ID",
                value_proc=_byo_wizard.validate_gcp_project_id,
            )

        # ── Offer to auto-open the project-scoped Cloud Console URLs ───
        # Default Yes; on No (or webbrowser failure) we still print the
        # URLs so SSH / headless users can copy-paste into a local
        # browser. The print-fallback path is unconditional so the user
        # never has to ask "what was that URL?".
        urls = _byo_wizard.build_console_urls(gcp_project_id)
        console.print(
            "\n[dim]The wizard can open the relevant Google Cloud Console pages "
            "for you (Drive API, Pub/Sub API, OAuth client creation).[/]"
        )
        if click.confirm("Open Cloud Console pages now?", default=True):
            for label, url in urls:
                opened = _byo_wizard.try_open_browser(url)
                marker = "[green]opened[/]" if opened else "[yellow]could not open browser[/]"
                console.print(f"  {marker}  {label}: {url}")
        else:
            console.print(
                "[dim]Skipped auto-open. Open these URLs manually:[/]"
            )
            for label, url in urls:
                console.print(f"  {label}: {url}")

        # ── Credentials file (validated: exists + JSON + installed.client_id) ──
        if profile_data.get("credentials_file"):
            credentials_file = profile_data["credentials_file"]
            console.print(
                f"[dim]Credentials file supplied by profile:[/] {credentials_file}\n"
            )
        else:
            console.print(
                "\n[dim]Credentials file: the OAuth2 'Desktop app' client JSON "
                "downloaded from Google Cloud Console (NOT a service-account key).[/]"
            )
            credentials_file = click.prompt(
                "Credentials file",
                default=_DEFAULT_CREDENTIALS,
                value_proc=_byo_wizard.validate_credentials_file,
            )

        # ── Drive folder ID (validated: looks like a folder ID, not a URL) ──
        console.print(
            "\n[dim]Drive folder ID: open the target folder in Google Drive and copy "
            "the segment AFTER /folders/ in the URL[/]"
            "\n[dim]  https://drive.google.com/drive/folders/<FOLDER_ID>[/]\n"
        )
        drive_folder_id = click.prompt(
            "Drive folder ID",
            value_proc=_byo_wizard.validate_drive_folder_id,
        )

        # ── Pub/Sub topic ID (validated: 3-255 chars, allowed charset) ──
        console.print(
            f"\n[dim]Pub/Sub topic ID: a unique name for this project's notification channel.[/]\n"
        )
        pubsub_topic_id = click.prompt(
            "Pub/Sub topic ID",
            default=f"claude-mirror-{project_name}",
            value_proc=_byo_wizard.validate_pubsub_topic_id,
        )
    elif backend == "dropbox":
        # Dropbox app key
        if profile_data.get("dropbox_app_key"):
            dropbox_app_key = profile_data["dropbox_app_key"]
            console.print(
                f"[dim]Dropbox app key supplied by profile.[/]\n"
            )
        else:
            console.print(
                "\n[dim]Dropbox app key: create an app at dropbox.com/developers and copy the app key.[/]"
                "\n[dim]  Required scopes: files.content.read, files.content.write[/]\n"
            )
            dropbox_app_key = click.prompt("Dropbox app key")

        # Dropbox folder
        console.print(
            "\n[dim]Dropbox folder: the path inside Dropbox where project files are stored.[/]"
            f"\n[dim]  Example: /claude-mirror/{project_name}[/]\n"
        )
        dropbox_folder = click.prompt(
            "Dropbox folder", default=f"/claude-mirror/{project_name}"
        )
    elif backend == "onedrive":
        # OneDrive client ID
        if profile_data.get("onedrive_client_id"):
            onedrive_client_id = profile_data["onedrive_client_id"]
            console.print(
                f"[dim]Azure app client ID supplied by profile.[/]\n"
            )
        else:
            console.print(
                "\n[dim]Azure app client ID: register an app at portal.azure.com → App registrations.[/]"
                "\n[dim]  Platform: Mobile and desktop applications[/]"
                "\n[dim]  Redirect URI: https://login.microsoftonline.com/common/oauth2/nativeclient[/]"
                "\n[dim]  API permissions: Files.ReadWrite, offline_access[/]\n"
            )
            onedrive_client_id = click.prompt("Azure app client ID")

        # OneDrive folder
        console.print(
            "\n[dim]OneDrive folder: path inside OneDrive where project files are stored.[/]"
            f"\n[dim]  Example: claude-mirror/{project_name}[/]\n"
        )
        onedrive_folder = click.prompt(
            "OneDrive folder", default=f"claude-mirror/{project_name}"
        )
    elif backend == "webdav":
        # WebDAV URL
        if profile_data.get("webdav_url"):
            webdav_url = profile_data["webdav_url"]
            console.print(
                f"[dim]WebDAV URL supplied by profile:[/] {webdav_url}\n"
            )
        else:
            console.print(
                "\n[dim]WebDAV URL: the full URL to the sync folder on your server.[/]"
                "\n[dim]  Nextcloud example: https://cloud.example.com/remote.php/dav/files/USER/claude-mirror/[/]"
                "\n[dim]  Generic example:   https://my-server.com/dav/claude-mirror/[/]\n"
            )
            webdav_url = click.prompt("WebDAV URL")

        # Reject http:// unless the user explicitly opts in. Basic-auth
        # over http transmits credentials and file payloads in cleartext
        # on every request — refuse by default.
        _scheme = webdav_url.split(":", 1)[0].lower() if ":" in webdav_url else ""
        if _scheme == "http":
            console.print(
                "\n[red]⚠ http:// WebDAV is INSECURE.[/] "
                "Basic-auth credentials and every file payload travel "
                "in cleartext on every request."
            )
            if not click.confirm(
                "Use http:// anyway? (only safe on a closed LAN test setup)",
                default=False,
            ):
                console.print(
                    "[yellow]Aborted. Re-run with an https:// URL.[/]"
                )
                sys.exit(1)
            webdav_insecure_http = True
        else:
            webdav_insecure_http = False

        # Username
        if profile_data.get("webdav_username"):
            webdav_username = profile_data["webdav_username"]
            console.print(
                f"[dim]WebDAV username supplied by profile:[/] {webdav_username}\n"
            )
        else:
            console.print(
                "\n[dim]Username for WebDAV authentication (basic auth).[/]\n"
            )
            webdav_username = click.prompt("Username")

        # Password
        if profile_data.get("webdav_password"):
            webdav_password = profile_data["webdav_password"]
            console.print("[dim]WebDAV password supplied by profile.[/]\n")
        else:
            console.print(
                "\n[dim]Password or app password. Stored in the token file.[/]"
                "\n[dim]  Nextcloud: generate an app password in Settings → Security.[/]\n"
            )
            import getpass
            webdav_password = getpass.getpass("Password: ")
    elif backend == "sftp":
        # Host
        if profile_data.get("sftp_host"):
            sftp_host = profile_data["sftp_host"]
            console.print(
                f"[dim]SFTP host supplied by profile:[/] {sftp_host}\n"
            )
        else:
            console.print(
                "\n[dim]SFTP host: hostname or IP of the SSH/SFTP server.[/]"
                "\n[dim]  Example: storage.example.com  or  10.0.0.42[/]\n"
            )
            while True:
                sftp_host = click.prompt("SFTP host").strip()
                if sftp_host:
                    break
                console.print("[red]Host cannot be empty.[/]")

        # Port
        if profile_data.get("sftp_port"):
            sftp_port = int(profile_data["sftp_port"])
            console.print(
                f"[dim]SFTP port supplied by profile:[/] {sftp_port}\n"
            )
        else:
            console.print(
                "\n[dim]SFTP port: TCP port for SSH on the server (default 22).[/]\n"
            )
            while True:
                sftp_port = click.prompt("SFTP port", default=22, type=int)
                if 1 <= sftp_port <= 65535:
                    break
                console.print(
                    "[red]Port must be in range 1..65535.[/]"
                )

        # Username
        if profile_data.get("sftp_username"):
            sftp_username = profile_data["sftp_username"]
            console.print(
                f"[dim]SFTP username supplied by profile:[/] {sftp_username}\n"
            )
        else:
            console.print(
                "\n[dim]Username for SSH/SFTP login.[/]\n"
            )
            while True:
                sftp_username = click.prompt("SFTP username").strip()
                if sftp_username:
                    break
                console.print("[red]Username cannot be empty.[/]")

        # Auth (key or password) — skipped when profile supplies one.
        if profile_data.get("sftp_key_file") or profile_data.get("sftp_password"):
            sftp_key_file = profile_data.get("sftp_key_file", "")
            sftp_password = profile_data.get("sftp_password", "")
            console.print(
                "[dim]SFTP authentication credentials supplied by profile.[/]\n"
            )
        else:
            # Auth choice — key (default) or password
            console.print(
                "\n[dim]Authentication method:[/]"
                "\n[dim]  k = SSH private key (recommended)[/]"
                "\n[dim]  p = password (LAN/test only — stored plain in YAML)[/]\n"
            )
            auth_choice = click.prompt(
                "Authenticate with [k]ey or [p]assword?",
                default="k",
                type=click.Choice(["k", "p"], case_sensitive=False),
            ).lower()

            if auth_choice == "k":
                console.print(
                    "\n[dim]Path to your SSH private key. Tilde-expanded.[/]\n"
                )
                raw_key = click.prompt(
                    "SSH private key file", default="~/.ssh/id_ed25519"
                )
                sftp_key_file = str(Path(raw_key).expanduser())
                if not Path(sftp_key_file).exists():
                    console.print(
                        f"[yellow]⚠ Key file not found at "
                        f"{sftp_key_file} on this machine — accepting anyway "
                        f"(it may exist on the deployment host).[/]"
                    )
            else:
                console.print(
                    "\n[red]⚠ Password will be stored in plain text in the "
                    "YAML config.[/] Recommended only for closed-LAN setups; "
                    "switch to key-based auth for any internet-reachable server.\n"
                )
                import getpass
                sftp_password = getpass.getpass("SFTP password: ")

        # known_hosts file
        console.print(
            "\n[dim]known_hosts file: where paramiko looks up host fingerprints.[/]"
            "\n[dim]  Default ~/.ssh/known_hosts is fine for most users; "
            "paramiko creates the file on first connect if missing.[/]\n"
        )
        sftp_known_hosts_file = click.prompt(
            "known_hosts file", default="~/.ssh/known_hosts"
        )

        # Strict host-key checking
        sftp_strict_host_check = click.confirm(
            "Reject unknown host fingerprints? "
            "(disable only for one-shot LAN setups)",
            default=True,
        )

        # Remote folder
        console.print(
            "\n[dim]SFTP folder: absolute path on the server where project "
            "files live. Must start with '/'.[/]"
            f"\n[dim]  Example: /srv/claude-mirror/{project_name}[/]\n"
        )
        while True:
            raw_folder = click.prompt("SFTP folder").strip()
            if not raw_folder.startswith("/"):
                console.print(
                    "[red]Folder must be an absolute path "
                    "(start with '/').[/]"
                )
                continue
            sftp_folder = raw_folder.rstrip("/") or "/"
            break

    # Polling interval for backends without push notifications.
    if backend in ("onedrive", "webdav", "sftp"):
        console.print(
            "\n[dim]Poll interval (seconds): how often the watcher checks for "
            "remote changes. Lower = more responsive, higher = less network use.[/]\n"
        )
        poll_interval = click.prompt(
            "Poll interval (seconds)", default=30, type=int,
        )

    # Token file
    if profile_data.get("token_file"):
        token_file = profile_data["token_file"]
        console.print(
            f"[dim]Token file supplied by profile:[/] {token_file}\n"
        )
    else:
        if backend == "googledrive":
            derived_token = _derive_token_file(credentials_file)
        elif backend == "dropbox":
            derived_token = str(CONFIG_DIR / f"dropbox-{project_name}-token.json")
        elif backend == "onedrive":
            derived_token = str(CONFIG_DIR / f"onedrive-{project_name}-token.json")
        elif backend == "webdav":
            derived_token = str(CONFIG_DIR / f"webdav-{project_name}-token.json")
        elif backend == "sftp":
            derived_token = str(CONFIG_DIR / f"sftp-{project_name}-token.json")
        else:
            derived_token = str(CONFIG_DIR / f"{backend}-{project_name}-token.json")
        raw_token = click.prompt("Token file", default=derived_token)
        token_file = str(Path(raw_token).expanduser())

    # Config file path
    derived_config = str(CONFIG_DIR / f"{project_name}.yaml")
    raw_config = click.prompt("Config file", default=derived_config)
    config_path = str(Path(raw_config).expanduser())

    # File patterns
    console.print(
        "\n[dim]File patterns: glob patterns for files to sync. Separate multiple with commas.[/]\n"
    )
    raw_patterns = click.prompt("File patterns", default="**/*.md")
    patterns = [p.strip() for p in raw_patterns.split(",")]

    # Optional Slack notifications
    slack_enabled = False
    slack_webhook_url = ""
    slack_channel = ""
    console.print(
        "\n[dim]Slack notifications: optionally post sync events to a Slack channel.[/]\n"
    )
    if click.confirm("Enable Slack notifications?", default=False):
        slack_enabled = True
        console.print(
            "\n[dim]Webhook URL: create an incoming webhook at api.slack.com/apps → Incoming Webhooks.[/]\n"
        )
        slack_webhook_url = click.prompt("Slack webhook URL")
        console.print(
            "\n[dim]Channel override (optional): leave blank to use the webhook's default channel.[/]\n"
        )
        slack_channel = click.prompt("Slack channel", default="")

    # Summary
    console.print("\n[bold]Summary[/]")
    console.print(f"  Backend:       {backend}")
    console.print(f"  Project:       {project_path}")
    console.print(f"  Config:        {config_path}")
    console.print(f"  Token:         {token_file}")
    if backend == "googledrive":
        console.print(f"  Credentials:   {credentials_file}")
        console.print(f"  Drive folder:  {drive_folder_id}")
        console.print(f"  GCP project:   {gcp_project_id}")
        console.print(f"  Pub/Sub topic: {pubsub_topic_id}")
    elif backend == "dropbox":
        console.print(f"  App key:       {dropbox_app_key}")
        console.print(f"  Dropbox folder:{dropbox_folder}")
    elif backend == "onedrive":
        console.print(f"  Client ID:     {onedrive_client_id}")
        console.print(f"  OneDrive folder: {onedrive_folder}")
    elif backend == "webdav":
        console.print(f"  WebDAV URL:    {webdav_url}")
        console.print(f"  Username:      {webdav_username}")
        console.print(f"  Password:      {'*' * len(webdav_password)}")
    elif backend == "sftp":
        console.print(f"  SFTP host:     {sftp_host}:{sftp_port}")
        console.print(f"  Username:      {sftp_username}")
        if sftp_key_file:
            console.print(f"  Key file:      {sftp_key_file}")
        if sftp_password:
            console.print(f"  Password:      {'*' * len(sftp_password)}")
        console.print(f"  known_hosts:   {sftp_known_hosts_file}")
        console.print(f"  Strict host:   {sftp_strict_host_check}")
        console.print(f"  SFTP folder:   {sftp_folder}")
    if backend in ("onedrive", "webdav", "sftp"):
        console.print(f"  Poll interval: {poll_interval}s")
    console.print(f"  Patterns:      {', '.join(patterns)}")

    # Exclude patterns
    console.print(
        "\n[dim]Exclude patterns: glob patterns for files/directories to exclude. "
        "Leave blank for none. Separate multiple with commas.\n"
        "  Examples: archive/**, drafts/**, **/*_draft.md[/]\n"
    )
    raw_excludes = click.prompt("Exclude patterns", default="")
    exclude_patterns = [p.strip() for p in raw_excludes.split(",") if p.strip()]

    # Reprint exclude in summary
    console.print(f"  Exclude:       {', '.join(exclude_patterns) if exclude_patterns else '(none)'}")

    # Snapshot format
    console.print(
        "\n[dim]Snapshot format:[/]\n"
        "  [bold]blobs[/] — content-addressed, deduplicated, ~zero-cost per snapshot (recommended)\n"
        "  [bold]full[/]  — full server-side copy of every file per snapshot (legacy)\n"
        "[dim]Use `claude-mirror migrate-snapshots` later to convert between formats.[/]\n"
    )
    snapshot_format = click.prompt(
        "Snapshot format", default="blobs",
        type=click.Choice(["blobs", "full"], case_sensitive=False),
    ).lower()
    console.print(f"  Snapshots:     {snapshot_format}")

    if slack_enabled:
        console.print(f"  Slack:         enabled")
        console.print(f"  Slack webhook: {slack_webhook_url}")
        if slack_channel:
            console.print(f"  Slack channel: {slack_channel}")
    console.print()

    if not click.confirm("Save this configuration?", default=True):
        console.print("[yellow]Aborted.[/]")
        sys.exit(0)

    # ── Post-auth Drive smoke test (googledrive only, opt-in, default Yes) ──
    # Catches the three most common Drive setup failures BEFORE the user
    # walks away thinking everything is configured: Drive API not enabled
    # in the GCP project, credentials.json downloaded for the wrong
    # project, and folder ID typos / missing share. Failure here does
    # NOT block the YAML write — the user might be configuring offline,
    # or behind an eventual-consistency provider. We just print a warning.
    smoke_creds: Optional[Any] = None
    if backend == "googledrive":
        smoke_creds = _maybe_run_drive_smoke_test(
            credentials_file=credentials_file,
            token_file=token_file,
            drive_folder_id=drive_folder_id,
        )

    # ── Auto-create Pub/Sub topic + subscription + IAM grant (v0.5.47) ──
    # Only runs when the user passed `--auto-pubsub-setup` AND the
    # smoke test returned a working OAuth credential bundle AND the
    # YAML actually configures Pub/Sub (gcp_project_id + topic ID set).
    # Idempotent — safe to re-run on every `init`. Failures don't abort
    # the wizard; the YAML still writes.
    if (
        auto_pubsub_setup
        and backend == "googledrive"
        and smoke_creds is not None
        and gcp_project_id
        and pubsub_topic_id
    ):
        _maybe_auto_setup_pubsub(
            creds=smoke_creds,
            gcp_project_id=gcp_project_id,
            pubsub_topic_id=pubsub_topic_id,
            machine_name=Config(
                project_path=project_path,
                backend="googledrive",
                file_patterns=["**/*.md"],
            ).machine_name,
        )

    return dict(
        backend=backend,
        project_path=project_path,
        drive_folder_id=drive_folder_id,
        gcp_project_id=gcp_project_id,
        pubsub_topic_id=pubsub_topic_id,
        dropbox_app_key=dropbox_app_key,
        dropbox_folder=dropbox_folder,
        onedrive_client_id=onedrive_client_id,
        onedrive_folder=onedrive_folder,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
        webdav_insecure_http=webdav_insecure_http,
        sftp_host=sftp_host,
        sftp_port=sftp_port,
        sftp_username=sftp_username,
        sftp_key_file=sftp_key_file,
        sftp_password=sftp_password,
        sftp_known_hosts_file=sftp_known_hosts_file,
        sftp_strict_host_check=sftp_strict_host_check,
        sftp_folder=sftp_folder,
        poll_interval=poll_interval,
        slack_enabled=slack_enabled,
        slack_webhook_url=slack_webhook_url,
        slack_channel=slack_channel,
        snapshot_format=snapshot_format,
        patterns=patterns,
        exclude_patterns=exclude_patterns,
        credentials_file=credentials_file,
        token_file=token_file,
        config_path=config_path,
    )


@cli.command()
@click.option("--project", default="", help="Path to the Claude project directory.")
@click.option("--backend", "backend_opt", default="googledrive", show_default=True,
              help="Storage backend: googledrive | dropbox | onedrive | webdav | sftp.")
@click.option("--drive-folder-id", default="", help="Google Drive folder ID to sync into.")
@click.option("--gcp-project-id", default="", help="Google Cloud project ID.")
@click.option("--pubsub-topic-id", default="", help="Pub/Sub topic ID.")
@click.option("--dropbox-app-key", default="", help="Dropbox app key.")
@click.option("--dropbox-folder", default="", help="Dropbox folder path (e.g. /claude-mirror/myproject).")
@click.option("--onedrive-client-id", default="", help="Azure app registration client ID.")
@click.option("--onedrive-folder", default="", help="OneDrive folder path (e.g. claude-mirror/myproject).")
@click.option("--webdav-url", default="", help="WebDAV server URL.")
@click.option("--webdav-username", default="", help="WebDAV username.")
@click.option("--webdav-password", default="", help="WebDAV password or app password.")
@click.option("--webdav-insecure-http", "webdav_insecure_http", is_flag=True, default=False,
              help="Allow http:// WebDAV URLs (cleartext basic-auth). NOT recommended; only for closed LAN test setups.")
@click.option("--sftp-host", default="", help="SFTP server hostname or IP.")
@click.option("--sftp-port", default=22, show_default=True, type=int,
              help="SFTP server port (1..65535).")
@click.option("--sftp-username", default="", help="SFTP username.")
@click.option("--sftp-key-file", default="",
              help="Path to SSH private key for SFTP auth (tilde-expanded).")
@click.option("--sftp-password", default="",
              help="SFTP password (LAN-only fallback; stored plain in YAML).")
@click.option("--sftp-known-hosts-file", default="~/.ssh/known_hosts", show_default=True,
              help="Path to SSH known_hosts file used for host-key verification.")
@click.option("--sftp-strict-host-check/--no-sftp-strict-host-check",
              "sftp_strict_host_check", default=True, show_default=True,
              help="Reject unknown SFTP host fingerprints. Disable only for one-shot LAN setups.")
@click.option("--sftp-folder", default="",
              help="Absolute server-side folder path for SFTP storage (must start with '/').")
@click.option("--poll-interval", "poll_interval", default=30, show_default=True, type=int,
              help="Polling interval in seconds for backends without push notifications (OneDrive, WebDAV, SFTP).")
@click.option("--slack-webhook-url", default="", help="Slack incoming webhook URL for sync notifications.")
@click.option("--slack-channel", default="", help="Slack channel override (default: webhook's channel).")
@click.option("--slack/--no-slack", "slack_flag", default=False, help="Enable/disable Slack notifications.")
@click.option("--snapshot-format", "snapshot_format_opt",
              type=click.Choice(["blobs", "full"], case_sensitive=False),
              default="blobs", show_default=True,
              help="Snapshot format: blobs (content-addressed, deduplicated) or full (legacy server-side copy).")
@click.option("--patterns", multiple=True, default=["**/*.md"], show_default=True,
              help="File glob patterns to sync (can repeat).")
@click.option("--exclude", "exclude_patterns", multiple=True, default=[],
              help="Glob patterns to exclude from sync (can repeat). E.g. --exclude 'archive/**'.")
@click.option("--credentials-file", "credentials_file", default=_DEFAULT_CREDENTIALS, show_default=True,
              help="Path to Google OAuth2 credentials JSON for this project.")
@click.option("--token-file", "token_file", default="",
              help="Path to store the OAuth2 token. Auto-derived from credentials filename if omitted.")
@click.option("--config", "config_path", default="",
              help="Path to write the config file. Defaults to ~/.config/claude_mirror/<project>.yaml.")
@click.option("--wizard", is_flag=True, default=False,
              help="Launch interactive setup wizard instead of specifying flags.")
@click.option("--auto-pubsub-setup", "auto_pubsub_setup", is_flag=True, default=False,
              help="After Drive OAuth completes, automatically create the "
                   "Pub/Sub topic, per-machine subscription, and IAM grant "
                   "(apps-storage-noreply@google.com -> roles/pubsub.publisher) "
                   "on the topic. Requires the Pub/Sub OAuth scope to have "
                   "been granted at auth time. Skipped silently if scope was "
                   "not granted, or on a non-googledrive backend.")
def init(
    project: str,
    backend_opt: str,
    drive_folder_id: str,
    gcp_project_id: str,
    pubsub_topic_id: str,
    dropbox_app_key: str,
    dropbox_folder: str,
    onedrive_client_id: str,
    onedrive_folder: str,
    webdav_url: str,
    webdav_username: str,
    webdav_password: str,
    webdav_insecure_http: bool,
    sftp_host: str,
    sftp_port: int,
    sftp_username: str,
    sftp_key_file: str,
    sftp_password: str,
    sftp_known_hosts_file: str,
    sftp_strict_host_check: bool,
    sftp_folder: str,
    poll_interval: int,
    slack_webhook_url: str,
    slack_channel: str,
    slack_flag: bool,
    snapshot_format_opt: str,
    patterns: tuple,
    exclude_patterns: tuple,
    credentials_file: str,
    token_file: str,
    config_path: str,
    wizard: bool,
    auto_pubsub_setup: bool,
) -> None:
    """Initialize claude-mirror for a project.

    Run with --wizard for interactive setup, or pass all flags directly.

    Combine with the global --profile flag (e.g.
    `claude-mirror --profile work init --wizard --backend googledrive`)
    to inherit credential-bearing fields (credentials_file, token_file,
    dropbox_app_key, etc.) from a named profile so the wizard / flag set
    only collects the project-specific fields (drive_folder_id, dropbox_folder,
    sftp_folder, ...).
    """
    # Ensure config directory exists before anything else
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Pick up the global --profile flag, if any. When set, the credential-
    # bearing fields supplied by the profile are exempt from the wizard's
    # prompt set and from the flag-mode required-fields check; the resulting
    # project YAML is written with `profile: NAME` at the top so the same
    # inheritance applies on every subsequent load.
    from .config import get_global_profile_override
    from .profiles import load_profile as _load_profile_data
    profile_name = get_global_profile_override()
    profile_data: dict[str, Any] = {}
    if profile_name:
        try:
            profile_data = _load_profile_data(profile_name)
        except FileNotFoundError as e:
            # The CLI group's invoke() already validates --profile up
            # front, so this branch is only reached if someone calls
            # init() directly. Match the group-level error shape.
            console.print(f"[red]✗ {e}[/]")
            sys.exit(1)

    backend = backend_opt

    if wizard:
        values = _run_wizard(
            backend_default=backend_opt,
            auto_pubsub_setup=auto_pubsub_setup,
            profile_data=profile_data,
        )
        backend          = values["backend"]
        project_path     = values["project_path"]
        drive_folder_id  = values["drive_folder_id"]
        gcp_project_id   = values["gcp_project_id"]
        pubsub_topic_id  = values["pubsub_topic_id"]
        dropbox_app_key  = values["dropbox_app_key"]
        dropbox_folder   = values["dropbox_folder"]
        onedrive_client_id = values["onedrive_client_id"]
        onedrive_folder  = values["onedrive_folder"]
        webdav_url       = values["webdav_url"]
        webdav_username  = values["webdav_username"]
        webdav_password  = values["webdav_password"]
        webdav_insecure_http = values["webdav_insecure_http"]
        sftp_host        = values["sftp_host"]
        sftp_port        = values["sftp_port"]
        sftp_username    = values["sftp_username"]
        sftp_key_file    = values["sftp_key_file"]
        sftp_password    = values["sftp_password"]
        sftp_known_hosts_file = values["sftp_known_hosts_file"]
        sftp_strict_host_check = values["sftp_strict_host_check"]
        sftp_folder      = values["sftp_folder"]
        poll_interval    = values["poll_interval"]
        slack_enabled    = values["slack_enabled"]
        slack_webhook_url = values["slack_webhook_url"]
        slack_channel    = values["slack_channel"]
        snapshot_format  = values["snapshot_format"]
        patterns         = values["patterns"]
        exclude_patterns = values["exclude_patterns"]
        credentials_file = values["credentials_file"]
        token_file       = values["token_file"]
        config_path      = values["config_path"]
    else:
        # Helper: is this credential-bearing field already supplied by
        # the active profile? When True, the corresponding CLI flag is
        # NOT required.
        def _from_profile(key: str) -> bool:
            return bool(profile_data.get(key))

        # Validate required flags per backend
        if backend == "googledrive":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--drive-folder-id", drive_folder_id),
                    ("--gcp-project-id",
                     gcp_project_id or profile_data.get("gcp_project_id", "")),
                    ("--pubsub-topic-id", pubsub_topic_id),
                ] if not val
            ]
        elif backend == "dropbox":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--dropbox-app-key",
                     dropbox_app_key or profile_data.get("dropbox_app_key", "")),
                    ("--dropbox-folder", dropbox_folder),
                ] if not val
            ]
        elif backend == "onedrive":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--onedrive-client-id",
                     onedrive_client_id or profile_data.get("onedrive_client_id", "")),
                    ("--onedrive-folder", onedrive_folder),
                ] if not val
            ]
        elif backend == "webdav":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--webdav-url",
                     webdav_url or profile_data.get("webdav_url", "")),
                    ("--webdav-username",
                     webdav_username or profile_data.get("webdav_username", "")),
                    ("--webdav-password",
                     webdav_password or profile_data.get("webdav_password", "")),
                ] if not val
            ]
        elif backend == "sftp":
            # Either a key OR a password is acceptable for auth — require
            # at least one. Folder, host, and username are always required.
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--sftp-host",
                     sftp_host or profile_data.get("sftp_host", "")),
                    ("--sftp-username",
                     sftp_username or profile_data.get("sftp_username", "")),
                    ("--sftp-folder", sftp_folder),
                ] if not val
            ]
            if (
                not sftp_key_file
                and not sftp_password
                and not profile_data.get("sftp_key_file")
                and not profile_data.get("sftp_password")
            ):
                missing.append("--sftp-key-file or --sftp-password")
        else:
            console.print(f"[red]Unknown backend: {backend}[/]")
            sys.exit(1)

        if missing:
            console.print(
                f"[red]Missing required options: {', '.join(missing)}[/]\n"
                "Pass them as flags or use [bold]--wizard[/] for interactive setup."
            )
            sys.exit(1)

        # Reject http:// WebDAV URLs unless --webdav-insecure-http was
        # passed. Basic-auth over http leaks credentials and payloads in
        # cleartext on every request — refuse by default. The same check
        # also runs at backend construction time as a belt-and-braces
        # guard against hand-edited configs.
        if backend == "webdav":
            _scheme = (
                webdav_url.split(":", 1)[0].lower() if ":" in webdav_url else ""
            )
            if _scheme == "http" and not webdav_insecure_http:
                console.print(
                    "[red]✗ http:// WebDAV URLs are refused by default.[/] "
                    "Basic-auth credentials and file payloads transit in "
                    "cleartext on every request. To proceed anyway (only "
                    "safe on a closed LAN test setup), re-run with "
                    "[bold]--webdav-insecure-http[/]."
                )
                sys.exit(1)

        # SFTP-specific validation: folder must be absolute, port in range.
        if backend == "sftp":
            if not sftp_folder.startswith("/"):
                console.print(
                    "[red]✗ --sftp-folder must be an absolute path "
                    "(start with '/').[/]"
                )
                sys.exit(1)
            sftp_folder = sftp_folder.rstrip("/") or "/"
            if not (1 <= sftp_port <= 65535):
                console.print(
                    f"[red]✗ --sftp-port must be in range 1..65535 "
                    f"(got {sftp_port}).[/]"
                )
                sys.exit(1)
            if sftp_key_file:
                sftp_key_file = str(Path(sftp_key_file).expanduser())
                if not Path(sftp_key_file).exists():
                    console.print(
                        f"[yellow]⚠ Key file not found at "
                        f"{sftp_key_file} on this machine — accepting "
                        f"anyway (it may exist on the deployment host).[/]"
                    )
            if sftp_password and not sftp_key_file:
                console.print(
                    "[yellow]⚠ Password stored in plain text in YAML.[/] "
                    "Recommended only for closed-LAN setups; switch to "
                    "key-based auth for any internet-reachable server."
                )

        project_path = str(Path(project).expanduser().resolve())
        if not Path(project_path).exists():
            console.print(f"[red]Project path does not exist: {project_path}[/]")
            sys.exit(1)

        credentials_file = str(Path(credentials_file).expanduser())
        if not token_file:
            if backend == "googledrive":
                token_file = _derive_token_file(credentials_file)
            elif backend == "dropbox":
                project_name = Path(project_path).name
                token_file = str(CONFIG_DIR / f"dropbox-{project_name}-token.json")
            elif backend == "onedrive":
                project_name = Path(project_path).name
                token_file = str(CONFIG_DIR / f"onedrive-{project_name}-token.json")
            elif backend == "webdav":
                project_name = Path(project_path).name
                token_file = str(CONFIG_DIR / f"webdav-{project_name}-token.json")
            elif backend == "sftp":
                project_name = Path(project_path).name
                token_file = str(CONFIG_DIR / f"sftp-{project_name}-token.json")
            else:
                project_name = Path(project_path).name
                token_file = str(CONFIG_DIR / f"{backend}-{project_name}-token.json")
        else:
            token_file = str(Path(token_file).expanduser())

        if not config_path:
            config_path = _derive_config_path(project_path)

        patterns = list(patterns)
        exclude_patterns = list(exclude_patterns)
        slack_enabled = slack_flag
        snapshot_format = (snapshot_format_opt or "blobs").lower()

    # When a profile is in play, blank out the credential-bearing fields
    # the profile supplies so the project YAML stores only project-
    # specific values. On reload, `Config.load` re-merges the profile
    # (project blanks lose to profile values per `apply_profile`'s
    # truthy-wins rule), so the runtime Config has the right credentials.
    if profile_name:
        if profile_data.get("credentials_file"):
            credentials_file = ""
        if profile_data.get("token_file"):
            token_file = ""
        if profile_data.get("gcp_project_id"):
            gcp_project_id = profile_data.get("gcp_project_id", "")
            # Keep the gcp_project_id on the Config so runtime steps
            # (auto-pubsub-setup) work; we do NOT blank it because it's
            # not just a credential — many project YAMLs override it.
        if profile_data.get("dropbox_app_key"):
            dropbox_app_key = ""
        if profile_data.get("onedrive_client_id"):
            onedrive_client_id = ""
        if profile_data.get("webdav_url"):
            webdav_url = ""
        if profile_data.get("webdav_username"):
            webdav_username = ""
        if profile_data.get("webdav_password"):
            webdav_password = ""
        if profile_data.get("sftp_host"):
            sftp_host = ""
        if profile_data.get("sftp_username"):
            sftp_username = ""
        if profile_data.get("sftp_key_file"):
            sftp_key_file = ""
        if profile_data.get("sftp_password"):
            sftp_password = ""

    config = Config(
        project_path=project_path,
        drive_folder_id=drive_folder_id,
        gcp_project_id=gcp_project_id,
        pubsub_topic_id=pubsub_topic_id,
        dropbox_app_key=dropbox_app_key,
        dropbox_folder=dropbox_folder,
        onedrive_client_id=onedrive_client_id,
        onedrive_folder=onedrive_folder,
        webdav_url=webdav_url,
        webdav_username=webdav_username,
        webdav_password=webdav_password,
        webdav_insecure_http=webdav_insecure_http,
        sftp_host=sftp_host,
        sftp_port=sftp_port,
        sftp_username=sftp_username,
        sftp_key_file=sftp_key_file,
        sftp_password=sftp_password,
        sftp_known_hosts_file=sftp_known_hosts_file,
        sftp_strict_host_check=sftp_strict_host_check,
        sftp_folder=sftp_folder,
        poll_interval=poll_interval,
        slack_enabled=slack_enabled,
        slack_webhook_url=slack_webhook_url,
        slack_channel=slack_channel,
        snapshot_format=snapshot_format,
        file_patterns=patterns,
        exclude_patterns=exclude_patterns,
        credentials_file=credentials_file,
        token_file=token_file,
        backend=backend,
        # Sensible snapshot-retention defaults written into every newly
        # initialised YAML so the prune command has a policy to act on
        # the first time it runs. Pre-existing configs are unchanged —
        # the dataclass defaults remain 0 (= disabled) so omitting these
        # fields from a hand-written YAML still means "no retention".
        # Roughly: keep 10 newest + last week of dailies + last year of
        # monthlies + last 3 years of yearlies. Edit the YAML or pass
        # --keep-* to `claude-mirror prune` to override.
        keep_last=10,
        keep_daily=7,
        keep_monthly=12,
        keep_yearly=3,
    )
    if profile_name:
        # Strip any field the profile supplies from the on-disk YAML so
        # the project file stays slim and changes to the profile
        # propagate. Fields the profile doesn't set still serialise
        # normally — e.g. a Drive profile that omits gcp_project_id
        # leaves the project YAML's gcp_project_id intact.
        strip = tuple(
            k for k in (
                "credentials_file", "token_file",
                "gcp_project_id",
                "dropbox_app_key",
                "onedrive_client_id",
                "webdav_url", "webdav_username", "webdav_password",
                "webdav_insecure_http",
                "sftp_host", "sftp_port", "sftp_username",
                "sftp_key_file", "sftp_password",
                "sftp_known_hosts_file", "sftp_strict_host_check",
            ) if profile_data.get(k) not in (None, "")
        )
        config.save(config_path, profile=profile_name, strip_fields=strip)
    else:
        config.save(config_path)
    console.print(f"[green]Config saved to:[/]     {config_path}")
    console.print(f"[green]Token file:[/]          {token_file}")
    if backend == "googledrive":
        console.print(f"[green]Credentials file:[/]    {credentials_file}")
        console.print("\nRun [bold]claude-mirror auth[/] to authenticate with Google.")
    elif backend == "dropbox":
        console.print(f"[green]Dropbox folder:[/]      {dropbox_folder}")
        console.print("\nRun [bold]claude-mirror auth[/] to authenticate with Dropbox.")
    elif backend == "onedrive":
        console.print(f"[green]OneDrive folder:[/]     {onedrive_folder}")
        console.print("\nRun [bold]claude-mirror auth[/] to authenticate with Microsoft.")
    elif backend == "webdav":
        console.print(f"[green]WebDAV URL:[/]          {webdav_url}")
        console.print("\nRun [bold]claude-mirror auth[/] to authenticate with your WebDAV server.")
    elif backend == "sftp":
        console.print(f"[green]SFTP host:[/]           {sftp_host}:{sftp_port}")
        console.print(f"[green]SFTP folder:[/]         {sftp_folder}")
        console.print("\nRun [bold]claude-mirror auth[/] to verify the SFTP connection.")

    # ── Flag-driven (non-wizard) auto-pubsub-setup path (v0.5.47) ──
    # The wizard branch already ran the smoke test + auto-setup above
    # via _run_wizard. The flag-only path (no --wizard) skipped that
    # smoke test entirely; honour --auto-pubsub-setup here by running
    # it now. Skipped silently on non-googledrive backends so the same
    # CLI invocation works for callers walking every backend through
    # the same flag list. The smoke test itself runs the OAuth flow,
    # so the user does NOT need to run `claude-mirror auth` first.
    if (
        auto_pubsub_setup
        and not wizard
        and backend == "googledrive"
        and gcp_project_id
        and pubsub_topic_id
    ):
        smoke_creds = _maybe_run_drive_smoke_test(
            credentials_file=credentials_file,
            token_file=token_file,
            drive_folder_id=drive_folder_id,
        )
        if smoke_creds is not None:
            _maybe_auto_setup_pubsub(
                creds=smoke_creds,
                gcp_project_id=gcp_project_id,
                pubsub_topic_id=pubsub_topic_id,
                machine_name=config.machine_name,
            )

    # Auto-reload the watcher if it is running
    _try_reload_watcher()


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--check", is_flag=True, default=False,
              help="Diagnostic mode: don't re-auth, just inspect the saved token "
                   "and try a refresh, reporting expiry / scopes / refresh result. "
                   "Useful for diagnosing why tokens 'expire' more often than expected.")
@click.option("--keep-existing", is_flag=True, default=False,
              help="Try to refresh the existing token first; only run a fresh "
                   "OAuth flow if refresh fails. Default behaviour is to replace "
                   "any existing token with a brand-new OAuth flow — running "
                   "`claude-mirror auth` should always end with a working token, "
                   "regardless of the prior state.")
def auth(config_path: str, check: bool, keep_existing: bool) -> None:
    """Authenticate with the configured storage backend.

    Default behaviour (since 0.5.11): the existing token file is moved aside
    BEFORE the OAuth flow runs, so a stale / partially-revoked / corrupted
    token can never short-circuit a re-auth attempt. If the OAuth flow fails
    for any reason, the original token is restored and the error surfaces
    cleanly — running `claude-mirror auth` is therefore always a safe action.

    --check runs a non-destructive diagnostic on the existing token without
    starting a new flow.
    --keep-existing skips the move-aside step (refresh-then-fallback semantics
    of older versions) — useful for diagnosing whether refresh works.
    """
    import shutil as _shutil
    config = Config.load(_resolve_config(config_path))
    if check:
        _auth_check(config)
        return

    # Move existing token aside so the backend's authenticate() sees no
    # cached credential and goes straight to the interactive OAuth flow.
    # Restore on any failure — never leave the user worse off than before.
    token_path = Path(config.token_file)
    backup_path: Optional[Path] = None
    if token_path.exists() and not keep_existing:
        backup_path = token_path.with_suffix(token_path.suffix + ".pre-reauth.bak")
        _shutil.move(str(token_path), str(backup_path))

    try:
        storage = _create_storage(config)
        creds = storage.authenticate()
    except BaseException:
        # OAuth flow failed (Ctrl-C, network, browser error, bad credentials_file).
        # Restore the prior token state so the user can retry without losing
        # whatever (possibly broken but at least known) state they had.
        if backup_path and backup_path.exists() and not token_path.exists():
            _shutil.move(str(backup_path), str(token_path))
        raise

    # OAuth succeeded — backup is no longer needed.
    if backup_path and backup_path.exists():
        try:
            backup_path.unlink()
        except OSError:
            pass  # leave it; harmless

    console.print("[green]Authentication successful.[/]")

    # Set up notification backend
    try:
        notifier = _create_notifier(config, storage)
        if notifier:
            notifier.ensure_topic()
            notifier.ensure_subscription()
            if config.backend == "googledrive":
                console.print(f"[green]Pub/Sub topic ready:[/] {config.pubsub_topic_id}")
                console.print(f"[green]Subscription ready:[/] {config.subscription_id}")
            else:
                console.print("[green]Notification backend ready.[/]")
            notifier.close()
    except Exception as e:
        console.print(f"[yellow]Notification setup failed: {e}[/]")


def _auth_check(config: Config) -> None:
    """Diagnose the current authentication state without modifying it.

    Reports:
      * which backend is configured
      * whether the token file exists and contains a refresh_token
      * the access-token expiry (and time-until-expiry)
      * the granted scopes
      * the result of a fresh refresh attempt (and whether the refresh_token
        was rotated by the server)

    Designed to be safe to run repeatedly — the only side effect is writing
    the refreshed token back to the token file (same as a normal command run).
    """
    import json as _json
    from datetime import datetime, timezone
    from pathlib import Path

    backend = (config.backend or "googledrive").lower()
    console.print(f"[bold]auth --check[/] [dim]({backend})[/]\n")

    if backend != "googledrive":
        console.print(
            f"[yellow]auth --check is currently Google-Drive specific.[/]\n"
            f"For {backend}, run [bold]claude-mirror auth[/] to verify."
        )
        return

    token_path = Path(config.token_file)
    if not token_path.exists():
        console.print(
            f"[red]✗ Token file not found:[/] {token_path}\n"
            "[yellow]Fix:[/] run [bold]claude-mirror auth[/] to authenticate."
        )
        return
    console.print(f"[green]✓ Token file:[/] {token_path}")

    try:
        raw = _json.loads(token_path.read_text())
    except Exception as e:
        console.print(f"[red]✗ Token file is unreadable:[/] {e}")
        return

    has_rt = bool(raw.get("refresh_token"))
    rt_len = len(raw.get("refresh_token", "") or "")
    if not has_rt:
        console.print(
            "[red]✗ Token file has no refresh_token.[/]\n"
            "[yellow]Fix:[/] run [bold]claude-mirror auth[/] (the consent screen "
            "must be shown again to issue a new refresh_token)."
        )
        return
    console.print(f"[green]✓ refresh_token present[/] ({rt_len} chars)")

    expiry = raw.get("expiry")
    if expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = (exp_dt - now).total_seconds()
            sign = "+" if delta >= 0 else "-"
            console.print(
                f"  saved expiry: {expiry}  ({sign}{abs(delta):.0f}s "
                f"= {sign}{abs(delta)/3600:.2f}h from now)"
            )
        except Exception:
            console.print(f"  saved expiry: {expiry}  [dim](unparseable)[/]")

    scopes = raw.get("scopes") or []
    if scopes:
        console.print(f"  scopes: {', '.join(scopes)}")

    # Now try a real refresh to confirm the refresh_token is alive.
    console.print("\n[bold]Attempting refresh…[/]")
    from .backends.googledrive import (
        _refresh_with_retry,
        _is_invalid_grant,
        SCOPES,
    )
    from google.oauth2.credentials import Credentials
    from google.auth.exceptions import RefreshError

    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except Exception as e:
        console.print(f"[red]✗ Could not load credentials:[/] {e}")
        return

    pre_refresh_token = creds.refresh_token
    try:
        _refresh_with_retry(creds)
    except RefreshError as e:
        if _is_invalid_grant(e):
            console.print(
                f"[red]✗ Refresh failed: invalid_grant[/]\n"
                f"  Raw: {e}\n\n"
                "[bold]This means the refresh token has been revoked or expired.[/] "
                "Common causes:\n"
                "  · Workspace admin's [bold]Cloud Session Control[/] reauth\n"
                "    interval elapsed (Admin Console → Security → Authentication\n"
                "    → Google Cloud session control)\n"
                "  · OAuth client was rotated in Cloud Console\n"
                "  · User revoked third-party app access\n"
                "  · The 50-refresh-tokens-per-(client × user) cap was hit\n\n"
                "[yellow]Fix:[/] run [bold]claude-mirror auth[/] to reauthenticate."
            )
        else:
            console.print(
                f"[red]✗ Refresh failed (transient):[/] {e}\n\n"
                "[yellow]This is a network/transport error, NOT an expired token.[/] "
                "Try again in a moment. If it persists, check:\n"
                "  · Network connectivity to oauth2.googleapis.com\n"
                "  · System clock is within 3 minutes of real UTC\n"
                "  · No corporate proxy is intercepting the refresh"
            )
        return
    except Exception as e:
        console.print(f"[red]✗ Refresh failed (unexpected):[/] {e}")
        return

    rotated = creds.refresh_token != pre_refresh_token
    new_expiry = creds.expiry.isoformat() + "Z" if creds.expiry else "(none)"
    console.print(
        f"[green]✓ Refresh succeeded[/]\n"
        f"  new expiry:    {new_expiry}\n"
        f"  refresh_token rotated: {'yes — token file updated' if rotated else 'no'}\n"
    )

    # Persist the rotated/refreshed token so subsequent commands see it.
    from .backends._util import write_token_secure
    try:
        write_token_secure(token_path, creds.to_json())
        console.print(f"  token file written: {token_path}")
    except Exception as e:
        console.print(f"[yellow]  warning: failed to write refreshed token: {e}[/]")

    console.print(
        "\n[bold green]Auth state is healthy.[/] If you're still seeing daily "
        "expiries, the cause is likely organisational, not local. Check:\n"
        "  1. Admin Console → Security → Authentication → "
        "[bold]Google Cloud session control[/]\n"
        "     (NOT 'Web session control' — they're separate settings)\n"
        "  2. Admin Console → Security → API controls → Manage Third-party app access\n"
        "  3. Set [bold]CLAUDE_MIRROR_AUTH_VERBOSE=1[/] before commands to log\n"
        "     refresh attempts to stderr."
    )


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--short", is_flag=True, default=False,
              help="Compact one-line summary instead of the full per-file table.")
@click.option("--pending", "pending", is_flag=True, default=False,
              help="Show files with non-ok mirror state (Tier 2 only). Lists "
                   "entries that are pending_retry or failed_perm on any "
                   "configured mirror backend — i.e. what `claude-mirror retry` "
                   "would attempt.")
@click.option("--by-backend", "by_backend", is_flag=True, default=False,
              help="Tier 2: render the per-file table with one column "
                   "per configured backend (primary first, mirrors in "
                   "mirror_config_paths order). Each cell shows that "
                   "backend's recorded state for the file: ok, pending, "
                   "failed, unseeded, or absent. The 'is everything in "
                   "sync on every mirror?' view at a glance.")
@click.option("--watch", "watch_interval", type=click.IntRange(min=1, max=3600), default=None,
              help="Live-update the status display, refreshing every WATCH "
                   "seconds (1-3600). Press Ctrl+C to exit. Suggested interval: "
                   "5 to 30 seconds.")
@click.option("--json", "json_output", is_flag=True, default=False,
              help="Emit a single flat JSON document to stdout instead of "
                   "the Rich table. All Rich output is suppressed; on error, "
                   "a JSON error envelope is written to stderr and the "
                   "process exits 1. Schema: v1.")
def status(config_path: str, short: bool, pending: bool, by_backend: bool,
           watch_interval: Optional[int], json_output: bool) -> None:
    """Show sync status of all configured project files.

    By default, prints a single snapshot of sync state and exits. With
    --watch N, refreshes the display every N seconds in place using
    rich.live.Live until the user presses Ctrl+C. Each refresh re-runs
    the full status computation (local hashing + remote listing), so
    pick an interval that's high enough to keep that work cheap — 5 to
    30 seconds is the sweet spot.

    --pending and --by-backend are mutually exclusive views; passing
    both at once errors out cleanly.
    """
    if pending and by_backend:
        if json_output:
            _emit_json_error(
                "status",
                ValueError(
                    "--pending and --by-backend are mutually exclusive"
                ),
            )
        console.print(
            "[red]--pending and --by-backend are mutually exclusive.[/] "
            "Pick one: --pending shows ONLY non-OK / unseeded entries; "
            "--by-backend shows the FULL per-file table with one column "
            "per backend."
        )
        sys.exit(1)

    # JSON path: build a flat result dict and emit it. Suppresses ALL
    # Rich output (tables, banners, progress lines) via _JsonMode. Watch
    # mode is incompatible with --json (a streaming live region is not a
    # single JSON document); we ignore --watch when --json is set.
    if json_output:
        try:
            with _JsonMode():
                resolved = _resolve_config(config_path)
                engine, _, _ = _load_engine(resolved, with_pubsub=False)
                states = engine.get_status()
            result = _status_result_dict(resolved, engine, states)
            _emit_json_success("status", result)
            return
        except SystemExit:
            raise
        except BaseException as e:
            _emit_json_error("status", e)

    engine, _, _ = _load_engine(_resolve_config(config_path), with_pubsub=False)

    if watch_interval is None:
        # Snapshot path: render once and exit. The transient dual-row
        # Progress lives inside _build_status_renderable so the user
        # sees "Local: hashing 42/120 files" + "Remote: explored 7
        # folder(s), 312 file(s)" updating live during the scan, rather
        # than a silent pause followed by a finished table.
        console.print(_build_status_renderable(
            engine, short=short, pending=pending,
            by_backend=by_backend, with_progress=True,
        ))
        return

    # Watch mode: refresh in place every watch_interval seconds. Each
    # iteration re-runs engine.get_status() (local hashing + remote listing),
    # so the user pays N-seconds of compute per cycle for "live" output.
    # Exit cleanly on Ctrl+C with a friendly message instead of a stack trace.
    # The outer rich.live.Live owns the live region, so we suppress the
    # snapshot-path's inner Progress (would conflict with Live's render
    # loop and produce flicker / interleaved output).
    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while True:
                renderable = _build_status_renderable(
                    engine, short=short, pending=pending,
                    by_backend=by_backend, with_progress=False,
                )
                live.update(renderable)
                _status_watch_sleep(watch_interval)
    except KeyboardInterrupt:
        console.print("\n[dim]watch stopped[/]")


def _status_watch_sleep(interval: int) -> None:
    """Indirection over time.sleep for the --watch loop only.

    Exists so tests can monkeypatch THIS function to raise KeyboardInterrupt
    instead of patching the global time.sleep. The global patch was fragile —
    it would trigger from any unrelated time.sleep call along the request
    path (stdlib retry loops, threading internals, urllib backoff, etc.),
    which raised KeyboardInterrupt outside the watch loop's try/except and
    surfaced as "Aborted!" exit_code 1 in CI on Python 3.11/3.12/3.13.
    """
    time.sleep(interval)


def _status_result_dict(
    config_path: str,
    engine: SyncEngine,
    states: list,
) -> dict:
    """Build the v1 `status --json` result payload.

    Mirrors the Rich table content but in a flat JSON-serialisable form:
        result.config_path     — absolute path to the active config YAML
        result.summary         — counts per Status enum value (snake_case keys)
        result.files           — list of {path, status, local_hash, remote_hash, manifest_hash}

    Status keys in `summary` use the lowercased Status.value strings
    (`in_sync`, `local_ahead`, `drive_ahead`, `conflict`, `new_local`,
    `new_drive`, `deleted_local`) plus `remote_ahead`/`new_remote` aliases
    so the v1 schema example in docs matches what consumers actually get.
    """
    summary: dict[str, int] = {
        "in_sync": 0,
        "local_ahead": 0,
        "remote_ahead": 0,
        "conflict": 0,
        "new_local": 0,
        "new_remote": 0,
        "deleted_local": 0,
    }
    files: list[dict] = []
    for s in states:
        # Convert Status enum to the schema key. The internal Status enum
        # uses `drive_ahead` / `new_drive` (legacy from the Drive-only
        # era); the JSON schema exposes them under the storage-agnostic
        # `remote_ahead` / `new_remote` aliases per the v1 spec.
        status_value = s.status.value
        summary_key = {
            "drive_ahead": "remote_ahead",
            "new_drive": "new_remote",
        }.get(status_value, status_value)
        if summary_key in summary:
            summary[summary_key] += 1
        manifest_entry = engine.manifest.get(s.rel_path)
        manifest_hash = manifest_entry.synced_hash if manifest_entry else None
        files.append({
            "path": s.rel_path,
            "status": summary_key,
            "local_hash": s.local_hash,
            "remote_hash": s.drive_hash,
            "manifest_hash": manifest_hash or None,
        })
    return {
        "config_path": str(Path(config_path).resolve()) if config_path else "",
        "summary": summary,
        "files": files,
    }


def _build_status_renderable(
    engine: SyncEngine,
    *,
    short: bool,
    pending: bool,
    by_backend: bool = False,
    with_progress: bool = False,
) -> RenderableType:
    """Build a Rich renderable describing the engine's current sync state.

    Used by both the snapshot path of `claude-mirror status` and the
    watch-mode loop. Heavy work (filesystem walk, remote listing, hash
    computation) runs once per call; the watch loop pays this cost on
    every refresh interval, which is the intended trade-off for "live"
    output.

    `with_progress` controls whether the local-hashing + remote-listing
    phases render their own dual-row transient Progress while running.
    True for the one-shot snapshot path (so the user sees live updates
    during the scan); False for the watch-mode loop where the outer
    rich.live.Live already owns the live region.

    Returns a Rich object (Group / Table / Text) that callers can either
    `console.print(...)` or pass to `Live.update(...)`.
    """
    if pending:
        return _build_pending_renderable(engine)

    if by_backend:
        return _build_status_by_backend_renderable(engine)

    if with_progress:
        # Mirrors the original engine.show_status() phase progress: two
        # rows ("Local" / "Remote") that update independently as the
        # callbacks fire from inside engine.get_status(). The Progress
        # is transient so the rows clear once the scan completes and the
        # final table can render in their place.
        from rich.progress import Progress, SpinnerColumn, TextColumn
        from ._progress import _SharedElapsedColumn

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description:<8}"),
            TextColumn("{task.fields[detail]}", style="dim"),
            _SharedElapsedColumn(),
            console=console,
            transient=True,
        ) as progress:
            local_task  = progress.add_task("Local",  total=None, detail="starting…", show_time=True)
            remote_task = progress.add_task("Remote", total=None, detail="starting…", show_time=False)

            def _on_local(msg: str) -> None:
                progress.update(local_task, detail=msg)

            def _on_remote(msg: str) -> None:
                progress.update(remote_task, detail=msg)

            states = engine.get_status(on_local=_on_local, on_remote=_on_remote)
    else:
        states = engine.get_status()

    # Per-file table (omitted in --short mode).
    parts: list[RenderableType] = []
    if not short:
        table = Table(title="Sync Status", show_header=True)
        table.add_column("File", style="white")
        table.add_column("Status")
        table.add_column("Action", style="dim")
        for s in states:
            label, action = STATUS_LABELS[s.status]
            table.add_row(s.rel_path, label, action)
        parts.append(table)

    # Summary counts line.
    counts: dict[Status, int] = {}
    for s in states:
        counts[s.status] = counts.get(s.status, 0) + 1

    order = [
        Status.CONFLICT, Status.LOCAL_AHEAD, Status.DRIVE_AHEAD,
        Status.NEW_LOCAL, Status.NEW_DRIVE, Status.DELETED_LOCAL, Status.IN_SYNC,
    ]
    colors = {
        Status.CONFLICT:      "red",
        Status.LOCAL_AHEAD:   "cyan",
        Status.DRIVE_AHEAD:   "blue",
        Status.NEW_LOCAL:     "cyan",
        Status.NEW_DRIVE:     "blue",
        Status.DELETED_LOCAL: "yellow",
        Status.IN_SYNC:       "green",
    }
    labels = {
        Status.CONFLICT:      "conflict",
        Status.LOCAL_AHEAD:   "local ahead",
        Status.DRIVE_AHEAD:   "drive ahead",
        Status.NEW_LOCAL:     "new local",
        Status.NEW_DRIVE:     "new on drive",
        Status.DELETED_LOCAL: "deleted local",
        Status.IN_SYNC:       "in sync",
    }

    if not counts:
        parts.append(Text.from_markup("[dim]No files found.[/]"))
    elif all(s == Status.IN_SYNC for s in counts):
        parts.append(Text.from_markup(
            f"[green]✓ All {counts[Status.IN_SYNC]} file(s) in sync.[/]"
        ))
    else:
        summary_parts = []
        for status in order:
            if status in counts:
                summary_parts.append(
                    f"[{colors[status]}]{counts[status]} {labels[status]}[/]"
                )
        parts.append(Text.from_markup("  " + "  ·  ".join(summary_parts)))

    # Size report — total project size + per-action byte breakdowns.
    # Pushes count local bytes (what's about to upload), pulls count drive
    # bytes (what's about to download), and conflicts use whichever side
    # is bigger so the user sees the largest cost.
    total_local_bytes = sum(s.local_size or 0 for s in states if s.local_size)
    total_local_files = sum(1 for s in states if s.local_size is not None)
    push_bytes = sum(
        (s.local_size or 0) for s in states
        if s.status in (Status.LOCAL_AHEAD, Status.NEW_LOCAL)
    )
    pull_bytes = sum(
        (s.drive_size or 0) for s in states
        if s.status in (Status.DRIVE_AHEAD, Status.NEW_DRIVE)
    )
    conflict_bytes = sum(
        max(s.local_size or 0, s.drive_size or 0)
        for s in states if s.status == Status.CONFLICT
    )
    size_parts: list[str] = []
    if total_local_files:
        size_parts.append(
            f"[dim]project: {total_local_files} file(s), "
            f"{_human_size(total_local_bytes)}[/]"
        )
    if push_bytes:
        size_parts.append(f"[cyan]↑ {_human_size(push_bytes)}[/]")
    if pull_bytes:
        size_parts.append(f"[blue]↓ {_human_size(pull_bytes)}[/]")
    if conflict_bytes:
        size_parts.append(f"[red]⚠ {_human_size(conflict_bytes)} (conflict)[/]")
    if size_parts:
        parts.append(Text.from_markup("  " + "  ·  ".join(size_parts)))

    return Group(*parts)


def _build_pending_renderable(engine: SyncEngine) -> RenderableType:
    """Tier 2: render files with any non-OK mirror state — pending_retry,
    failed_perm, unseeded (no recorded state AND not present on the live
    mirror), or deleted-on-mirror (manifest claims ok but the mirror's
    live listing disagrees).

    Live-verifies every configured mirror by walking it once, then
    cross-references with the manifest. This is slower than a manifest-
    only check (one list_files_recursive call per mirror) but reflects
    actual remote state — important in multi-user setups where one
    machine's manifest doesn't see what another machine pushed.
    """
    from .snapshots import SNAPSHOTS_FOLDER, BLOBS_FOLDER

    if not engine._mirrors:
        return Text.from_markup(
            "[dim]No mirrors configured for this project; "
            "no pending state to report.[/]"
        )

    # Live walk every mirror so we can distinguish "not in manifest +
    # not on mirror" (truly unseeded) from "not in manifest + present
    # on mirror" (another machine pushed it; not really unseeded).
    excluded = {SNAPSHOTS_FOLDER, BLOBS_FOLDER, LOGS_FOLDER}
    mirror_names = [
        getattr(b, "backend_name", "") for b in engine._mirrors
        if getattr(b, "backend_name", "")
    ]
    live_files: dict[str, set[str]] = {}
    walk_errors: dict[str, str] = {}

    from rich.progress import Progress, SpinnerColumn, TextColumn
    from ._progress import _SharedElapsedColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<14}"),
        TextColumn("{task.fields[detail]}", style="dim"),
        _SharedElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        # Add every mirror's row up-front so the user sees all backends
        # progress simultaneously instead of one-at-a-time.
        mirror_tasks_p: dict[str, int] = {}
        for idx, name in enumerate(mirror_names):
            mirror_tasks_p[name] = progress.add_task(
                name, total=None, detail="queued",
                show_time=(idx == 0),
            )

        def _walk_mirror_pending(args: tuple) -> None:
            backend, name = args
            task = mirror_tasks_p[name]
            progress.update(task, detail="listing")
            try:
                folder_id = (
                    getattr(backend, "config", None)
                    and backend.config.root_folder
                )
                entries = backend.list_files_recursive(
                    folder_id, exclude_folder_names=excluded,
                )
                seen = {
                    f["relative_path"] for f in entries
                    if (f.get("relative_path", "")
                        and not f["relative_path"].startswith("_")
                        and not f["relative_path"].startswith(f"{SNAPSHOTS_FOLDER}/")
                        and not f["relative_path"].startswith(f"{BLOBS_FOLDER}/")
                        and not f["relative_path"].startswith(f"{LOGS_FOLDER}/")
                        and not engine._is_excluded(f["relative_path"]))
                }
                live_files[name] = seen
                progress.update(task, detail=f"{len(seen)} file(s)")
            except Exception as e:
                walk_errors[name] = redact_error(str(e))
                live_files[name] = set()
                progress.update(task, detail=f"[red]error: {walk_errors[name]}[/]")

        # Walk every mirror in parallel — pre-fix this loop was
        # sequential, leaving each mirror to wait for its predecessor.
        # Same fan-out approach as `_build_status_by_backend_renderable`.
        from concurrent.futures import ThreadPoolExecutor as _TPE

        if engine._mirrors:
            with _TPE(max_workers=len(engine._mirrors)) as ex:
                list(ex.map(_walk_mirror_pending,
                            zip(engine._mirrors, mirror_names)))

    pending_by_path: dict[str, list[tuple[str, str, str]]] = {}
    unseeded_by_backend: dict[str, int] = {}
    deleted_by_backend: dict[str, list[str]] = {}

    files = engine.manifest.all()
    # Universe of paths to consider: union of manifest entries + every
    # file actually present on any mirror. Files seen on a mirror but
    # not in our manifest are NOT pending/unseeded (another machine
    # uploaded them and we just haven't pulled yet) — silently skipped.
    for path in set(files.keys()):
        fs = files[path]
        for backend_name in mirror_names:
            rs = fs.remotes.get(backend_name)
            present = path in live_files.get(backend_name, set())
            if rs is None and not present:
                # No manifest entry AND not on mirror → truly unseeded.
                unseeded_by_backend[backend_name] = (
                    unseeded_by_backend.get(backend_name, 0) + 1
                )
                continue
            if rs is None and present:
                # Another machine uploaded — fine, nothing to do here.
                continue
            if rs is not None and rs.state in ("pending_retry", "failed_perm"):
                pending_by_path.setdefault(path, []).append(
                    (backend_name, rs.state, rs.last_error)
                )
                continue
            if rs is not None and rs.state == "ok" and not present:
                # Manifest claims ok but mirror's live listing disagrees:
                # someone removed the file out of band (SSH, web UI, etc.).
                deleted_by_backend.setdefault(backend_name, []).append(path)
                continue
            # rs.state == "ok" and present, OR state == "absent" — nothing to surface

    parts: list[RenderableType] = []

    if walk_errors:
        # Surface listing failures up-front so the user knows the rest of
        # the report is incomplete for those mirrors.
        err_lines = [
            f"  [red]✗[/] {name}: {err}"
            for name, err in walk_errors.items()
        ]
        parts.append(Text.from_markup(
            "[red]Could not list these mirrors — pending/unseeded state "
            "below may be incomplete:[/]\n" + "\n".join(err_lines)
        ))
        parts.append(Text(""))

    if pending_by_path:
        table = Table(show_header=True, header_style="bold",
                      title=f"Pending mirror state ({len(pending_by_path)} file(s))")
        table.add_column("File", style="white")
        table.add_column("Backend")
        table.add_column("State")
        table.add_column("Last error", style="dim")
        for path in sorted(pending_by_path.keys()):
            for backend_name, state, last_error in pending_by_path[path]:
                color = "yellow" if state == "pending_retry" else "red"
                table.add_row(
                    path, backend_name,
                    f"[{color}]{state}[/]",
                    (last_error or "")[:80],
                )
        parts.append(table)
        parts.append(Text.from_markup(
            "[dim]Run [bold]claude-mirror retry[/] to re-attempt "
            "the pending entries.[/]"
        ))

    if deleted_by_backend:
        if parts:
            parts.append(Text(""))
        del_table = Table(show_header=True, header_style="bold",
                          title="Deleted out-of-band on mirror")
        del_table.add_column("Backend")
        del_table.add_column("Files missing", justify="right")
        del_table.add_column("Sample", style="dim")
        for name in sorted(deleted_by_backend.keys()):
            paths = deleted_by_backend[name]
            sample = paths[0] + (f" (+{len(paths)-1} more)" if len(paths) > 1 else "")
            del_table.add_row(name, str(len(paths)), sample)
        parts.append(del_table)
        parts.append(Text.from_markup(
            "[dim]Manifest says these files were uploaded but the "
            "mirror's live listing disagrees — someone removed them via "
            "SSH / web UI / a different tool. Re-push to restore, or "
            "[bold]claude-mirror delete[/dim][dim] them locally if the "
            "removal was intentional.[/]"
        ))

    if unseeded_by_backend:
        if parts:
            parts.append(Text(""))  # blank line spacer
        seed_table = Table(show_header=True, header_style="bold",
                           title="Unseeded mirrors")
        seed_table.add_column("Backend")
        seed_table.add_column("Unseeded files", justify="right")
        seed_table.add_column("Fix", style="dim")
        for backend_name in sorted(unseeded_by_backend.keys()):
            count = unseeded_by_backend[backend_name]
            seed_table.add_row(
                backend_name,
                str(count),
                f"claude-mirror seed-mirror --backend {backend_name}",
            )
        parts.append(seed_table)
        parts.append(Text.from_markup(
            "[dim]A mirror is [yellow]unseeded[/yellow] when files exist "
            "on the primary but were never uploaded to that mirror — "
            "typically because the mirror was added to "
            "`mirror_config_paths` after the files were first pushed. "
            "Run the suggested command to upload them to the mirror only "
            "(primary is not touched).[/dim]"
        ))

    if not parts:
        return Text.from_markup(
            "[green]✓ All mirrors are caught up — nothing pending or unseeded.[/]"
        )
    return Group(*parts)


def _build_status_by_backend_renderable(engine: SyncEngine) -> RenderableType:
    """Tier 2: render the per-file table with one column per configured
    backend (primary first, mirrors in mirror_config_paths order). Each
    cell reflects the file's actual sync state on that backend, derived
    from `engine.get_status()` (which does local hashing + primary remote
    listing + 3-way diff) PLUS a separate live walk of every mirror.

    Why use get_status() for the universe instead of `manifest ∪ live`:
    plain `claude-mirror status` is the gold standard for sync state —
    it catches local-only files, locally-modified files, drive-ahead
    files, and conflicts via hash comparison. By-backend MUST give the
    same answer for the same files; otherwise users see "✓ ok" on a
    file they know they just edited locally.

    Cell rendering per backend column:
      Primary cell — derived from FileSyncState.status (Status enum):
        IN_SYNC        → green ✓ ok
        LOCAL_AHEAD    → cyan  ↑ local ahead   (local has unpushed changes)
        DRIVE_AHEAD    → blue  ↓ drive ahead   (need to pull)
        NEW_LOCAL      → cyan  + new local      (never pushed)
        NEW_DRIVE      → blue  + new drive      (only on primary, not local)
        DELETED_LOCAL  → yellow ✗ deleted local (was synced; gone locally)
        CONFLICT       → red   ⚠ conflict       (both sides changed)

      Mirror cells — derived from live presence + manifest state, but
      ALSO consider the primary's status: when the primary is non-IN_SYNC
      because LOCAL has unpushed/conflicting/new content (LOCAL_AHEAD,
      NEW_LOCAL, CONFLICT), the mirror inherits the same problem because
      mirrors are write-replicas of primary — propagate the primary's
      cell to the mirror so the user sees the issue uniformly across
      backends. When the primary status is IN_SYNC, fall back to per-
      mirror live+manifest logic for ok / pending / failed / unseeded /
      deleted-out-of-band / absent.
    """
    from .snapshots import SNAPSHOTS_FOLDER, BLOBS_FOLDER

    primary_name = getattr(engine.storage, "backend_name", "") or "primary"
    mirrors = list(engine._mirrors)
    mirror_names = [
        getattr(b, "backend_name", "") or "mirror" for b in mirrors
    ]
    backend_names = [primary_name] + mirror_names

    # Phase 1: get authoritative local + primary state via get_status,
    # PLUS list each mirror in its own progress row so the user can see
    # which backend is the bottleneck (especially SFTP, which can be
    # slower than Drive's batched listing).
    excluded = {SNAPSHOTS_FOLDER, BLOBS_FOLDER, LOGS_FOLDER}
    live_files: dict[str, set[str]] = {}
    walk_errors: dict[str, str] = {}

    from rich.progress import Progress, SpinnerColumn, TextColumn
    from ._progress import _SharedElapsedColumn

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<14}"),
        TextColumn("{task.fields[detail]}", style="dim"),
        _SharedElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        local_task = progress.add_task("Local", total=None,
                                       detail="starting…", show_time=True)
        primary_task = progress.add_task(f"{primary_name} (primary)",
                                         total=None, detail="starting…",
                                         show_time=False)

        # Add all mirror task rows up-front so the user sees every
        # backend's progress simultaneously instead of one-at-a-time.
        mirror_tasks: dict[str, int] = {}
        for idx, name in enumerate(mirror_names):
            mirror_tasks[name] = progress.add_task(
                name, total=None, detail="queued",
                show_time=False,  # Local already carries the shared timer
            )

        # Worker that walks one mirror's tree and stuffs results into
        # `live_files[name]` / `walk_errors[name]` under thread-safe
        # writes (dict assignment is GIL-atomic in CPython).
        def _walk_mirror(args: tuple) -> None:
            mirror, name = args
            task = mirror_tasks[name]
            progress.update(task, detail="listing")
            try:
                folder_id = (
                    getattr(mirror, "config", None)
                    and mirror.config.root_folder
                )
                entries = mirror.list_files_recursive(
                    folder_id, exclude_folder_names=excluded,
                )
                seen: set[str] = set()
                for f in entries:
                    rel = f.get("relative_path", "")
                    if not rel:
                        continue
                    if rel.startswith("_"):
                        continue
                    if (rel.startswith(f"{SNAPSHOTS_FOLDER}/")
                        or rel.startswith(f"{BLOBS_FOLDER}/")
                        or rel.startswith(f"{LOGS_FOLDER}/")):
                        continue
                    if engine._is_excluded(rel):
                        continue
                    seen.add(rel)
                live_files[name] = seen
                progress.update(task, detail=f"{len(seen)} file(s)")
            except Exception as e:
                walk_errors[name] = redact_error(str(e))
                live_files[name] = set()
                progress.update(task, detail=f"[red]error: {walk_errors[name]}[/]")

        # Run engine.get_status() (Local + primary listing) AND every
        # mirror's list_files_recursive concurrently via a single
        # ThreadPoolExecutor. Pre-fix the mirror loop ran AFTER
        # get_status returned, leaving SFTP listing as a tail-latency
        # bottleneck — user observed Local + GDrive completing in
        # parallel but SFTP only starting after. Now everything fans
        # out at once: total wall-clock = max(Local, primary, mirrors)
        # instead of get_status() + sum(mirror_walks).
        from concurrent.futures import ThreadPoolExecutor as _TPE

        def _run_get_status() -> list:
            return engine.get_status(
                on_local=lambda msg: progress.update(local_task, detail=msg),
                on_remote=lambda msg: progress.update(primary_task, detail=msg),
            )

        # +1 worker for engine.get_status, one per mirror.
        max_workers = 1 + max(1, len(mirrors))
        with _TPE(max_workers=max_workers) as ex:
            primary_fut = ex.submit(_run_get_status)
            mirror_futs = [
                ex.submit(_walk_mirror, (m, n))
                for m, n in zip(mirrors, mirror_names)
            ]
            states = primary_fut.result()
            for f in mirror_futs:
                f.result()  # propagate exceptions; _walk_mirror catches its own

        # Freeze the local + primary rows at their final state so they
        # stay visible while subsequent table-rendering happens. Mirror
        # rows are already at their final detail strings from inside
        # _walk_mirror.
        progress.update(local_task, total=1, completed=1)
        progress.update(primary_task, total=1, completed=1)
        for task in mirror_tasks.values():
            progress.update(task, total=1, completed=1)

    # Map FileSyncState's Status enum to (cell markup, tally key, propagates_to_mirror).
    # When propagates_to_mirror is True, the mirror cell shows the same
    # status because the mirror is necessarily in the same boat as the
    # primary (write-replica). When False, mirrors get their own per-
    # backend cell from live presence + manifest state.
    primary_cell_for_status: dict[Status, tuple[str, str, bool]] = {
        Status.IN_SYNC:        ("[green]✓ ok[/]",            "ok",            False),
        Status.LOCAL_AHEAD:    ("[cyan]↑ local ahead[/]",    "local_ahead",   True),
        Status.DRIVE_AHEAD:    ("[blue]↓ drive ahead[/]",    "drive_ahead",   True),
        Status.NEW_LOCAL:      ("[cyan]+ new local[/]",      "new_local",     True),
        Status.NEW_DRIVE:      ("[blue]+ new drive[/]",      "new_drive",     True),
        Status.DELETED_LOCAL:  ("[yellow]✗ deleted local[/]","deleted_local", True),
        Status.CONFLICT:       ("[red]⚠ conflict[/]",        "conflict",      True),
    }

    # Per-backend tally for the footer.
    tallies: dict[str, dict[str, int]] = {
        name: {} for name in backend_names
    }

    def _bump(name: str, key: str) -> None:
        tallies[name][key] = tallies[name].get(key, 0) + 1

    def _mirror_cell_when_primary_in_sync(name: str, fs, rel_path: str) -> tuple[str, str]:
        """Per-mirror cell when the primary says the file is in sync —
        the mirror's outcome depends on its own live presence + manifest
        recorded state."""
        present = rel_path in live_files.get(name, set())
        rs = fs.remotes.get(name) if fs else None
        manifest_state = rs.state if rs else None
        if present:
            if manifest_state == "pending_retry":
                return ("[yellow]⚠ pending[/]", "pending")
            if manifest_state == "failed_perm":
                return ("[red]✗ failed[/]", "failed")
            return ("[green]✓ ok[/]", "ok")
        # Not present on mirror.
        if manifest_state == "ok":
            return ("[red]✗ deleted[/]", "deleted")
        if manifest_state == "absent":
            return ("[dim]· absent[/]", "absent")
        if manifest_state == "pending_retry":
            return ("[yellow]⚠ pending[/]", "pending")
        if manifest_state == "failed_perm":
            return ("[red]✗ failed[/]", "failed")
        return ("[yellow]⊘ unseeded[/]", "unseeded")

    table = Table(
        show_header=True, header_style="bold",
        title="Sync Status (per backend, live-verified)",
    )
    table.add_column("File", style="white", overflow="fold")
    for name in backend_names:
        suffix = " [dim](primary)[/]" if name == primary_name else ""
        table.add_column(f"{name}{suffix}", justify="left")

    if not states and not any(live_files.values()):
        if walk_errors:
            error_lines = "\n".join(
                f"  [red]✗[/] {name}: {err}"
                for name, err in walk_errors.items()
            )
            return Text.from_markup(
                "[yellow]Could not list any backend.[/]\n" + error_lines
            )
        return Text.from_markup(
            "[dim]No files tracked yet — push something first.[/]"
        )

    manifest_files = engine.manifest.all()
    # Universe is the FileSyncState list (covers local + primary). Mirror-
    # only files (rare — would be a file present on mirror but missing
    # from primary AND from local) are tracked separately so they don't
    # get lost. We add them as extra rows below the get_status set.
    state_paths: set[str] = {s.rel_path for s in states}
    mirror_only: set[str] = set()
    for name in mirror_names:
        for path in live_files.get(name, set()):
            if path not in state_paths:
                mirror_only.add(path)

    for s in sorted(states, key=lambda x: x.rel_path):
        fs = manifest_files.get(s.rel_path)
        primary_cell, primary_key, propagates = primary_cell_for_status.get(
            s.status, (f"[dim]{s.status.name}[/]", "ok", False),
        )
        row_cells: list[str] = [s.rel_path, primary_cell]
        _bump(primary_name, primary_key)
        for name in mirror_names:
            if propagates:
                # Primary is non-IN_SYNC due to local divergence — the
                # mirror is necessarily in the same state because mirrors
                # follow the primary's content.
                row_cells.append(primary_cell)
                _bump(name, primary_key)
            else:
                cell, key = _mirror_cell_when_primary_in_sync(
                    name, fs, s.rel_path,
                )
                row_cells.append(cell)
                _bump(name, key)
        table.add_row(*row_cells)

    # Mirror-only orphan files: present on a mirror, not on primary, not
    # local. Render them as "✗ orphan" on each backend that has them.
    # These typically come from old restores or out-of-band uploads.
    for path in sorted(mirror_only):
        row_cells = [f"{path} [dim](mirror-only)[/]", "[dim]· absent[/]"]
        _bump(primary_name, "absent")
        for name in mirror_names:
            if path in live_files.get(name, set()):
                row_cells.append("[red]✗ orphan[/]")
                _bump(name, "orphan")
            else:
                row_cells.append("[dim]· absent[/]")
                _bump(name, "absent")
        table.add_row(*row_cells)

    # Per-backend health footer.
    health_priority = ("conflict", "failed", "deleted", "orphan",
                       "pending", "local_ahead", "drive_ahead",
                       "new_local", "new_drive", "deleted_local",
                       "unseeded", "absent", "ok")
    color_for_key = {
        "ok":            "green",
        "pending":       "yellow",
        "failed":        "red",
        "deleted":       "red",
        "orphan":        "red",
        "unseeded":      "yellow",
        "absent":        "dim",
        "local_ahead":   "cyan",
        "drive_ahead":   "blue",
        "new_local":     "cyan",
        "new_drive":     "blue",
        "deleted_local": "yellow",
        "conflict":      "red",
    }
    label_for_key = {
        "local_ahead":   "local ahead",
        "drive_ahead":   "drive ahead",
        "new_local":     "new local",
        "new_drive":     "new drive",
        "deleted_local": "deleted local",
    }
    summary_lines: list[str] = []
    for name in backend_names:
        if name in walk_errors:
            suffix = " [dim](primary)[/]" if name == primary_name else ""
            summary_lines.append(
                f"  [red]✗[/] [bold]{name}[/]{suffix} · "
                f"[red]listing failed: {walk_errors[name]}[/]"
            )
            continue
        t = tallies[name]
        bits: list[str] = []
        for key in health_priority:
            n = t.get(key, 0)
            if not n:
                continue
            color = color_for_key.get(key, "white")
            label = label_for_key.get(key, key)
            bits.append(f"[{color}]{n} {label}[/]")
        # Health emoji.
        if any(t.get(k) for k in ("conflict", "failed", "deleted", "orphan")):
            health = "[red]✗[/]"
        elif any(t.get(k) for k in ("pending", "unseeded", "local_ahead",
                                    "drive_ahead", "new_local", "new_drive",
                                    "deleted_local")):
            health = "[yellow]⚠[/]"
        else:
            health = "[green]✓[/]"
        suffix = " [dim](primary)[/]" if name == primary_name else ""
        summary_lines.append(
            f"  {health} [bold]{name}[/]{suffix} · "
            + " · ".join(bits or ["[dim]empty[/]"])
        )

    summary = Text.from_markup("\n".join(summary_lines))
    return Group(table, Text(""), summary)


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
def sync(config_path: str) -> None:
    """Bidirectional sync: push local changes, pull remote changes, prompt on conflicts."""
    engine, _, _ = _load_engine(_resolve_config(config_path))
    engine.sync()


@cli.command()
@click.argument("files", nargs=-1)
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--force-local", is_flag=True, default=False,
              help="Treat local content as authoritative: push all changed files without interactive conflict resolution.")
def push(files: tuple, config_path: str, force_local: bool) -> None:
    """Push local changes to Drive. Optionally specify FILES to push."""
    engine, cfg, _ = _load_engine(_resolve_config(config_path))
    engine.push(list(files) if files else None, force_local=force_local)
    _maybe_auto_prune(engine, cfg)


def _maybe_auto_prune(engine: SyncEngine, cfg: Config) -> None:
    """Run snapshot retention pruning if any keep_* policy field is set.

    Called after a successful push. Opt-in via config — every keep_*
    field defaults to 0, so projects without retention configured see
    zero behaviour change. When active, runs in non-dry-run mode (the
    user has consented by setting the config field) and logs the prune
    summary so deletions stay visible.
    """
    if not any((cfg.keep_last, cfg.keep_daily, cfg.keep_monthly, cfg.keep_yearly)):
        return
    if engine.snapshots is None:
        return
    try:
        engine.snapshots.prune_per_retention(
            keep_last=cfg.keep_last,
            keep_daily=cfg.keep_daily,
            keep_monthly=cfg.keep_monthly,
            keep_yearly=cfg.keep_yearly,
            dry_run=False,
        )
    except Exception as e:
        # Pruning is opportunistic housekeeping — never fail the push
        # because retention couldn't complete. The error is surfaced so
        # the user can investigate, but exit code stays 0.
        console.print(f"[yellow]auto-prune skipped:[/] {e}")


@cli.command()
@click.argument("path", type=str)
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--context", "context_lines", type=click.IntRange(min=0, max=200), default=3,
              show_default=True, help="Number of context lines around each hunk.")
def diff(path: str, config_path: str, context_lines: int) -> None:
    """Show a colorized line-diff of local vs remote for one file.

    PATH can be a project-relative path or an absolute path inside the
    project. The output is a unified diff (remote → local) with green
    additions, red deletions, and dim context lines — a quick way to
    decide whether to push, pull, or merge before doing either.

    \b
    Cases handled cleanly:
      - both sides differ — full unified diff
      - only on local       — every line shown as added (would be pushed)
      - only on remote      — every line shown as deleted (would be pulled)
      - in sync             — single "identical" line, exit code 0
      - binary file         — refused with a one-line note, exit code 0

    \b
    Examples:
      claude-mirror diff memory/CLAUDE.md
      claude-mirror diff /Users/me/proj/memory/CLAUDE.md
      claude-mirror diff CHANGELOG.md --context 8
    """
    engine, _, _ = _load_engine(_resolve_config(config_path), with_pubsub=False)

    project_root = Path(engine.config.project_path).resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            rel_path = str(candidate.resolve().relative_to(project_root))
        except ValueError:
            console.print(
                f"[red]Path is outside the project root.[/]\n"
                f"  path:    {candidate}\n"
                f"  project: {project_root}"
            )
            sys.exit(1)
    else:
        rel_path = str(candidate).replace("\\", "/").lstrip("./")

    # Find the file in engine state. We don't run a full status (slow on
    # large trees) — just resolve the local path + look up the remote
    # entry by relative path.
    local_path = project_root / rel_path
    local_bytes: Optional[bytes] = None
    if local_path.exists() and local_path.is_file():
        local_bytes = local_path.read_bytes()

    remote_bytes: Optional[bytes] = None
    try:
        remote_entries = engine.storage.list_files_recursive(engine._folder_id)
    except Exception as e:
        console.print(f"[red]Could not list remote files:[/] {e}")
        sys.exit(1)

    remote_match = next(
        (f for f in remote_entries if f.get("relative_path") == rel_path),
        None,
    )
    if remote_match is not None:
        try:
            remote_bytes = engine.storage.download_file(remote_match["id"])
        except Exception as e:
            console.print(f"[red]Could not download remote copy:[/] {e}")
            sys.exit(1)

    if local_bytes is None and remote_bytes is None:
        console.print(
            f"[red]No such file:[/] {rel_path}\n"
            f"[dim]Not present locally and not found on the remote.[/]"
        )
        sys.exit(1)

    console.print(render_diff(local_bytes, remote_bytes, rel_path, context_lines=context_lines))


@cli.command()
@click.argument("files", nargs=-1)
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--output", default="", help="Download files to this directory instead of the project path. Does not update local files or the manifest.")
def pull(files: tuple, config_path: str, output: str) -> None:
    """Pull remote changes from Drive. Optionally specify FILES to pull."""
    engine, _, _ = _load_engine(_resolve_config(config_path), with_pubsub=not output)
    engine.pull(list(files) if files else None, output_dir=output or None)


@cli.command()
@click.argument("files", nargs=-1, required=True)
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--local", is_flag=True, default=False, help="Also delete the local file(s).")
def delete(files: tuple, config_path: str, local: bool) -> None:
    """Delete FILES from remote storage (and optionally local).

    Removes the specified files from the configured storage backend and clears
    their manifest entries. Use --local to also delete the local copies.
    """
    engine, config, storage = _load_engine(_resolve_config(config_path), with_pubsub=True)

    deleted: list[str] = []

    with engine._make_phase_progress() as progress:
        # Status phase (Local + Remote rows)
        states = engine._run_status_phase(progress)
        state_map = {s.rel_path: s for s in states}

        # Deleting phase
        del_task = progress.add_task(
            "Deleting", total=len(files), detail=f"0/{len(files)}", show_time=True)
        done = 0
        for rel_path in files:
            state = state_map.get(rel_path)
            if not state:
                progress.console.print(
                    f"  [yellow]⚠[/] {rel_path} — not found in project or remote storage"
                )
                done += 1
                progress.update(del_task, advance=1, detail=f"{done}/{len(files)}")
                continue

            if not state.drive_file_id:
                progress.console.print(f"  [yellow]⚠[/] {rel_path} — not on remote storage")
                if local:
                    local_path = Path(config.project_path) / rel_path
                    if local_path.exists():
                        local_path.unlink()
                        engine.manifest.remove(rel_path)
                        progress.console.print(f"  [yellow]✗[/] {rel_path} (deleted locally)")
                        deleted.append(rel_path)
                done += 1
                progress.update(del_task, advance=1, detail=f"{done}/{len(files)}")
                continue

            # Route through SyncEngine._delete_drive_file so the
            # multi-backend fan-out runs (primary delete + each mirror's
            # delete via its recorded per-backend file_id). Calling
            # storage.delete_file directly would have orphaned the file
            # on every mirror — exactly the bug a Tier 2 user would
            # hit if they cleaned up a file on Drive but never told
            # the Dropbox/OneDrive/WebDAV mirrors about it.
            engine._delete_drive_file(state)

            if local:
                local_path = Path(config.project_path) / rel_path
                if local_path.exists():
                    local_path.unlink()
                    progress.console.print(f"  [yellow]✗[/] {rel_path} (deleted locally)")

            deleted.append(rel_path)
            done += 1
            progress.update(del_task, advance=1, detail=f"{done}/{len(files)} ({rel_path})")

        engine.manifest.save()

        # Notify phase
        if deleted and engine.notifier:
            notify_task = progress.add_task(
                "Notify", total=1, detail="publishing delete event…", show_time=True)
            engine._publish_event(deleted, "delete")
            engine._flush_publishes()
            progress.update(notify_task, advance=1, detail="completed")
        else:
            engine._flush_publishes()

    if deleted:
        console.print(f"[yellow]Deleted {len(deleted)} file(s).[/]")
    else:
        console.print("[dim]Nothing to delete.[/]")


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option(
    "--once/--no-once",
    default=False,
    help=(
        "Run a single polling cycle instead of the long-running watch loop. "
        "Useful for cron-driven setups: `*/5 * * * * claude-mirror watch --once --quiet`. "
        "Default --no-once preserves the existing foreground-daemon behaviour."
    ),
)
@click.option(
    "--quiet/--no-quiet",
    default=False,
    help=(
        "Suppress the 'Watching ...' banner and the 'Watcher stopped.' line. "
        "Per-event notification lines are still printed. Pairs with --once "
        "for cron jobs that should only emit output when there is news."
    ),
)
def watch(config_path: str, once: bool, quiet: bool) -> None:
    """
    Watch for remote changes via the configured notification backend.
    Sends a system notification when collaborators push updates.

    Default mode: foreground long-running daemon — press Ctrl+C to stop.

    With --once: run exactly one polling cycle, dispatch any events
    surfaced, exit 0. Pairs with --quiet for cron-driven setups.
    """
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    desktop_notifier = Notifier(config.project_path)
    stop_event = threading.Event()

    try:
        notifier = _create_notifier(config, storage)
        if not notifier:
            console.print("[red]No notification backend available for this storage type.[/]")
            sys.exit(1)
        notifier.ensure_subscription()
    except Exception as e:
        console.print(f"[red]Failed to connect to notification backend: {e}[/]")
        sys.exit(1)

    def on_event(event: SyncEvent) -> None:
        files_str = ", ".join(event.files) if event.files else "files"
        title = "claude-mirror"
        message = (
            f"{event.user}@{event.machine} updated {files_str} "
            f"in '{event.project}'. Run `claude-mirror sync` to merge."
        )
        console.print(f"\n[bold blue]Remote update:[/] {message}")
        desktop_notifier.notify(title, message, event={"user": event.user, "machine": event.machine,
                                               "files": event.files, "project": event.project,
                                               "action": event.action})

    def _handle_signal(sig, frame):
        console.print("\n[dim]Stopping watcher...[/]")
        stop_event.set()

    if not once:
        # Signal handlers only matter for the long-running daemon path —
        # `--once` returns of its own accord after a single cycle, and
        # installing a SIGINT handler in cron-driven runs would mask
        # the user's expected Ctrl+C-during-test behaviour for nothing.
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    if config.backend == "googledrive":
        sub_info = config.subscription_id
    elif config.backend == "dropbox":
        sub_info = "longpoll"
    elif config.backend == "onedrive":
        sub_info = f"polling every {config.poll_interval}s"
    elif config.backend == "webdav":
        sub_info = f"polling every {config.poll_interval}s"
    else:
        sub_info = config.backend

    if not quiet:
        console.print(f"\n[bold]claude-mirror v{_get_version()}[/]")
        if once:
            console.print(
                f"[green]Running one polling cycle[/] "
                f"(project: [bold]{config.project_path}[/])\n"
                f"Backend: [dim]{config.backend}[/] ({sub_info})"
            )
        else:
            console.print(
                f"[green]Watching for updates[/] (project: [bold]{config.project_path}[/])\n"
                f"Backend: [dim]{config.backend}[/] ({sub_info})\n"
                "Press [bold]Ctrl+C[/] to stop."
            )

    if once:
        notifier.watch_once(on_event)
    else:
        notifier.watch(on_event, stop_event)
    notifier.close()
    if not quiet and not once:
        console.print("[dim]Watcher stopped.[/]")


def _make_watch_callback(cfg: Config, n: Notifier) -> Callable:
    """Create a per-project callback for Pub/Sub watch events."""
    def on_event(event: SyncEvent) -> None:
        files_str = ", ".join(event.files) if event.files else "files"
        message = (
            f"{event.user}@{event.machine} updated {files_str} "
            f"in '{event.project}'. Run `claude-mirror sync` to merge."
        )
        console.print(
            f"\n[bold blue]Remote update[/] ([dim]{cfg.project_path}[/]): {message}"
        )
        n.notify("claude-mirror", message, event={
            "user": event.user, "machine": event.machine,
            "files": event.files, "project": event.project,
            "action": event.action,
        })
    return on_event


def _start_watcher(
    config_path: str,
    stop_event: threading.Event,
    watched: set[str],
    clients: list[NotificationBackend],
) -> threading.Thread | None:
    """Start a watcher thread for a single config. Returns the thread, or None on failure."""
    resolved = str(Path(config_path).resolve())
    if resolved in watched:
        return None

    try:
        config = Config.load(config_path)
        storage = _create_storage(config)
        desktop_notifier = Notifier(config.project_path)
        notifier = _create_notifier(config, storage)
        if not notifier:
            console.print(f"[yellow]Skipping {config_path}: no notification backend[/]")
            return None
        notifier.ensure_subscription()
    except Exception as e:
        console.print(f"[yellow]Skipping {config_path}: {e}[/]")
        return None

    clients.append(notifier)
    watched.add(resolved)

    t = threading.Thread(
        target=notifier.watch,
        args=(_make_watch_callback(config, desktop_notifier), stop_event),
        daemon=True,
    )
    t.start()
    console.print(
        f"[green]Watching[/] [bold]{config.project_path}[/] "
        f"([dim]{config.subscription_id}[/])"
    )
    return t


@cli.command("watch-all")
@click.option("--config", "config_paths", multiple=True,
              help="Config file(s) to watch. Repeatable. Defaults to all configs in ~/.config/claude_mirror/.")
def watch_all(config_paths: tuple) -> None:
    """
    Watch all projects simultaneously (one subscription per config).
    Discovers all configs in ~/.config/claude_mirror/ unless --config is given.
    Send SIGHUP to reload configs and pick up new projects without restarting.
    Press Ctrl+C to stop all watchers.
    """
    use_auto_discover = not config_paths

    if config_paths:
        paths = list(config_paths)
    else:
        paths = sorted(str(p) for p in CONFIG_DIR.glob("*.yaml"))
        if not paths:
            console.print("[red]No configs found in ~/.config/claude_mirror/[/]")
            sys.exit(1)

    stop_event = threading.Event()
    clients: list[NotificationBackend] = []
    watched: set[str] = set()            # resolved paths of configs already watched
    threads: list[threading.Thread] = []

    def _collect_mirror_paths(candidate_paths: list[str]) -> set[str]:
        """First pass: load each candidate config and gather every path
        referenced by some other config's `mirror_config_paths`. Those
        configs are mirrors of an already-watched primary, so they must
        be skipped — otherwise multi-backend projects get duplicate
        notification streams (one per backend)."""
        mirror_paths: set[str] = set()
        for cp in candidate_paths:
            try:
                cfg = Config.load(cp)
            except Exception:
                continue
            for mp in (cfg.mirror_config_paths or []):
                try:
                    if Path(mp).is_absolute():
                        resolved_mp = Path(mp).resolve()
                    else:
                        resolved_mp = Path(_resolve_config(mp)).resolve()
                except Exception:
                    try:
                        resolved_mp = Path(mp).resolve()
                    except Exception:
                        continue
                mirror_paths.add(str(resolved_mp))
        return mirror_paths

    def _start_with_mirror_skip(p: str, mirror_paths: set[str]) -> threading.Thread | None:
        resolved = str(Path(p).resolve())
        if resolved in mirror_paths:
            try:
                cfg = Config.load(p)
                project_label = cfg.project_path
            except Exception:
                project_label = "<unknown>"
            console.print(
                f"[dim]skipping mirror config {Path(p).name} for project "
                f"{project_label} — primary already watching[/]"
            )
            return None
        return _start_watcher(p, stop_event, watched, clients)

    mirror_paths = _collect_mirror_paths(paths)
    for p in paths:
        t = _start_with_mirror_skip(p, mirror_paths)
        if t:
            threads.append(t)

    if not threads:
        console.print("[red]No watchers started.[/]")
        sys.exit(1)

    def _handle_stop(sig, frame):
        console.print("\n[dim]Stopping all watchers...[/]")
        stop_event.set()

    def _handle_reload(sig, frame):
        """SIGHUP: re-scan configs and start watchers for any new projects."""
        if use_auto_discover:
            new_paths = sorted(str(p) for p in CONFIG_DIR.glob("*.yaml"))
        else:
            new_paths = list(config_paths)
        # Re-scan mirror paths in case primaries gained / lost mirrors
        # since startup. Mirror configs are skipped on reload too.
        new_mirror_paths = _collect_mirror_paths(new_paths)
        added = 0
        for p in new_paths:
            t = _start_with_mirror_skip(p, new_mirror_paths)
            if t:
                threads.append(t)
                added += 1
        if added:
            console.print(f"[dim]Reload: added {added} new project(s), now watching {len(watched)} total.[/]")
        else:
            console.print(f"[dim]Reload: no new configs found ({len(watched)} project(s) unchanged).[/]")

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGHUP, _handle_reload)

    console.print(f"\n[bold]claude-mirror v{_get_version()}[/]")
    console.print(f"[dim]Watching {len(threads)} project(s). Send SIGHUP to reload, Ctrl+C to stop.[/]")

    # Update-check daemon thread — once at startup, then every 24h while
    # watch-all is running. Fires a non-disruptive desktop notification
    # the first time a new version is observed (cache prevents re-notifying
    # for the same version on subsequent wake-ups). Best-effort; any
    # failure inside the timer thread is silently swallowed.
    def _periodic_update_check():
        import time as _time
        try:
            from ._update_check import check_for_update
            # Initial check shortly after startup so the user gets a
            # notice on launch when an update is already pending.
            _time.sleep(30)
            while not stop_event.is_set():
                try:
                    check_for_update(notify_desktop=True)
                except Exception:
                    pass
                # Sleep in 60s slices so stop_event is honoured promptly.
                for _ in range(24 * 60):  # 24 hours
                    if stop_event.is_set():
                        return
                    _time.sleep(60)
        except Exception:
            pass

    _update_thread = threading.Thread(target=_periodic_update_check, daemon=True)
    _update_thread.start()

    stop_event.wait()

    for c in clients:
        c.close()

    console.print("[dim]All watchers stopped.[/]")


@cli.command("check-update")
def check_update() -> None:
    """Check GitHub for a newer claude-mirror version.

    Bypasses the 24h cache and fetches the canonical pyproject.toml
    from the project's GitHub mirror. Prints the current and latest
    versions and an explicit "up to date" / "update available" message.

    Useful when you suspect the daily cache has missed a release, or
    when you want to confirm a specific version is out before updating.
    """
    from ._update_check import (
        force_check_now,
        _get_current_version,
        _is_strictly_newer,
        _is_disabled,
    )
    if _is_disabled():
        console.print(
            "[dim]Update check is disabled via "
            "[bold]CLAUDE_MIRROR_NO_UPDATE_CHECK[/]; unset that env var to "
            "use this command.[/]"
        )
        return
    current = _get_current_version()
    console.print(f"[bold]Current version:[/] {current}")
    console.print(f"[dim]Fetching latest from GitHub…[/]")
    latest = force_check_now()
    if latest is None:
        console.print(
            "[yellow]Could not reach GitHub.[/] Check your connection and "
            "try again — the daily background check will retry automatically."
        )
        sys.exit(1)
    console.print(f"[bold]Latest on GitHub:[/] {latest}")
    if _is_strictly_newer(latest, current):
        console.print(
            f"\n[yellow]🆕 Update available: {current} → {latest}[/]\n"
            f"[dim]Update with: [bold]pipx install -e . --force[/] from "
            f"your repo dir.[/]"
        )
    elif latest == current:
        console.print(f"\n[green]✓ You are on the latest version.[/]")
    else:
        # Local is ahead of upstream — typically because the user is
        # running a dev build from a not-yet-pushed commit.
        console.print(
            f"\n[blue]ℹ You are ahead of GitHub[/] (local {current} > "
            f"latest {latest}). This is normal if you're developing "
            f"claude-mirror itself."
        )


@cli.command()
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Actually run the update. Without this flag, just lists "
                   "what would happen (the safe default).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="With --apply, skip the confirmation prompt. Required "
                   "for non-interactive use (cron, CI).")
def update(do_apply: bool, skip_confirm: bool) -> None:
    """Update claude-mirror to the latest version on GitHub.

    Runs `git pull` in the install directory, then `pipx install -e .
    --force` to rebuild the venv. Both steps are auto-detected from the
    package's install location — works whether claude-mirror was installed
    editable (the typical case) or not.

    \b
    Safe by default: without --apply, this command only reports what
    would happen. To actually update:
        claude-mirror update --apply
    To skip the confirmation prompt (cron / CI):
        claude-mirror update --apply --yes

    \b
    Notes:
      * If `claude-mirror watch-all` is running, the daemon will keep the
        OLD code in memory until restarted. The command warns you and
        prints the PIDs to kill.
      * If your repo has uncommitted local changes, `git pull` will
        refuse to merge — fix the conflict manually first.
      * On network failure or git refusal, the command exits non-zero
        with the underlying tool's error output preserved.
    """
    from ._update_check import (
        force_check_now,
        _get_current_version,
        _is_strictly_newer,
        suggested_update_command,
    )
    current = _get_current_version()
    console.print(f"[bold]Current version:[/] {current}")
    console.print(f"[dim]Fetching latest from GitHub…[/]")
    latest = force_check_now()
    if not latest:
        console.print(
            "[yellow]Could not reach GitHub.[/] Check your connection and "
            "try again."
        )
        sys.exit(1)
    console.print(f"[bold]Latest on GitHub:[/] {latest}")

    if not _is_strictly_newer(latest, current):
        if latest == current:
            console.print("\n[green]✓ Already on the latest version.[/]")
        else:
            console.print(
                f"\n[blue]ℹ Local ({current}) is ahead of GitHub "
                f"({latest}).[/] Nothing to update."
            )
        return

    cmd = suggested_update_command()

    if not do_apply:
        console.print(
            f"\n[yellow]🆕 Update available: {current} → {latest}[/]\n"
            f"[dim]To apply automatically:[/] [bold]claude-mirror update --apply[/]\n"
            f"[dim]Or run manually:[/] {cmd}"
        )
        return

    # --apply path: detect running watcher first so the user can decide
    # whether to stop it. The daemon keeps the OLD code in memory after
    # an upgrade until restarted; we surface the PIDs so they can kill
    # them, but never auto-kill (would terminate active sync work).
    try:
        import subprocess as _sp
        result = _sp.run(
            ["pgrep", "-f", "claude-mirror watch-all"],
            capture_output=True, text=True, timeout=2,
        )
        own_pid = str(os.getpid())
        pids = [
            p.strip() for p in result.stdout.splitlines()
            if p.strip() and p.strip() != own_pid
        ]
    except Exception:
        pids = []

    if pids:
        console.print(
            f"\n[yellow]⚠ Watcher daemon is running[/] "
            f"(PID {', '.join(pids)}).\n"
            f"  After the update, the daemon will keep running the OLD "
            f"code in memory until restarted.\n"
            f"  To restart it cleanly:\n"
            f"    [dim]kill {' '.join(pids)}[/]   (then re-launch via "
            f"`claude-mirror watch-all` or your launchd / systemd service)"
        )

    if not skip_confirm:
        if not click.confirm(
            f"\nUpdate claude-mirror from {current} to {latest}?",
            default=True,
        ):
            console.print("[yellow]Aborted.[/]")
            return

    console.print(f"\n[bold]Running:[/] {cmd}\n")
    import subprocess as _sp
    try:
        # `cmd` is the user-displayed shell recipe from
        # suggested_update_command(); we DO NOT execute it via the shell.
        # Instead we resolve the repo path from the same source and run
        # `git pull` and `pipx install -e . --force` as list-form
        # subprocess calls with cwd=repo_path and shell=False. The
        # generic v0.3.x phrasing (no resolved repo path) is refused
        # outright — we cannot run it automatically.
        if cmd == "pipx install -e . --force from your repo dir":
            console.print(
                "[red]Could not auto-detect the install path.[/] "
                "Please run the update manually from your repo "
                "directory:\n  pipx install -e . --force"
            )
            sys.exit(1)
        if cmd == "pipx upgrade claude-mirror":
            # Non-editable install path — single command, no cwd needed.
            result = _sp.run(
                ["pipx", "upgrade", "claude-mirror"], check=False,
            )
            if result.returncode != 0:
                console.print(
                    f"\n[red]✗ Update failed (exit code {result.returncode}).[/] "
                    f"See output above for the underlying error (typically "
                    f"a network failure or pipx issue)."
                )
                sys.exit(result.returncode)
        else:
            # Editable install path — resolve the repo root from the
            # package location and run git+pipx as separate list-form
            # calls with cwd=repo_path. This avoids shell=True entirely.
            from ._update_check import _resolve_repo_root
            repo_path = _resolve_repo_root()
            if not repo_path:
                console.print(
                    "[red]Could not auto-detect the install path.[/] "
                    "Please run the update manually from your repo "
                    "directory:\n  pipx install -e . --force"
                )
                sys.exit(1)
            git_result = _sp.run(
                ["git", "pull"], cwd=repo_path, check=False,
            )
            if git_result.returncode != 0:
                console.print(
                    f"\n[red]✗ Update failed (git pull, exit code "
                    f"{git_result.returncode}).[/] See output above "
                    f"(typically a git conflict or network failure)."
                )
                sys.exit(git_result.returncode)
            pipx_result = _sp.run(
                ["pipx", "install", "-e", ".", "--force"],
                cwd=repo_path, check=False,
            )
            if pipx_result.returncode != 0:
                console.print(
                    f"\n[red]✗ Update failed (pipx install, exit code "
                    f"{pipx_result.returncode}).[/] See output above "
                    f"(typically a pipx or build issue)."
                )
                sys.exit(pipx_result.returncode)
        console.print(
            f"\n[green]✓ Update complete.[/] Verify with:\n"
            f"  [dim]claude-mirror --version[/]\n"
            f"  [dim]claude-mirror check-update[/]"
        )
        if pids:
            console.print(
                f"\n[yellow]Reminder:[/] watcher daemon (PID "
                f"{', '.join(pids)}) is still on the OLD code; restart it now."
            )
    except Exception as e:
        console.print(f"\n[red]✗ Update failed:[/] {e}")
        sys.exit(1)


@cli.command()
def reload() -> None:
    """Send SIGHUP to the running watch-all process to pick up new configs."""
    import subprocess
    result = subprocess.run(
        ["pgrep", "-f", "claude-mirror watch-all"],
        capture_output=True, text=True,
    )
    pids = result.stdout.strip().splitlines()
    # Filter out our own process
    own_pid = str(os.getpid())
    pids = [p.strip() for p in pids if p.strip() and p.strip() != own_pid]

    if not pids:
        console.print("[yellow]No running watch-all process found.[/]")
        return

    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGHUP)
            console.print(f"[green]Sent SIGHUP to watch-all process (PID {pid}).[/]")
        except ProcessLookupError:
            console.print(f"[yellow]Process {pid} no longer exists.[/]")
        except PermissionError:
            console.print(f"[red]Permission denied sending signal to PID {pid}.[/]")


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--json", "json_output", is_flag=True, default=False,
              help="Emit a single flat JSON document to stdout instead of "
                   "the Rich table. All Rich output is suppressed; on error, "
                   "a JSON error envelope is written to stderr and the "
                   "process exits 1. Schema: v1.")
def snapshots(config_path: str, json_output: bool) -> None:
    """List all snapshots stored on Drive."""
    if json_output:
        try:
            with _JsonMode():
                config = Config.load(_resolve_config(config_path))
                storage = _create_storage(config)
                snap = SnapshotManager(config, storage)
                snapshot_list = snap.list()
            result = [_snapshot_entry_to_json(s) for s in snapshot_list]
            _emit_json_success("snapshots", result)
            return
        except SystemExit:
            raise
        except BaseException as e:
            _emit_json_error("snapshots", e)
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)
    snap.show_list()


def _snapshot_entry_to_json(entry: dict) -> dict:
    """Project a SnapshotManager.list() dict into the v1 JSON schema.

    Schema: {timestamp, format, file_count, size_bytes_or_null, source_backend}
    `size_bytes` is null when not recorded by the backend (full-format
    snapshots and older blobs manifests don't track total bytes
    end-to-end). `source_backend` is the primary backend name as that's
    where `.list()` looks today.
    """
    files_changed = entry.get("files_changed", [])
    file_count = entry.get("total_files")
    if not isinstance(file_count, int):
        # Fallback: the metadata may have lost total_files; use the
        # length of files_changed as an upper bound only when nothing
        # better is available.
        file_count = len(files_changed) if isinstance(files_changed, list) else 0
    size_bytes = entry.get("size_bytes")
    if not isinstance(size_bytes, int):
        size_bytes = None
    return {
        "timestamp": entry.get("timestamp", ""),
        "format": entry.get("format", "?"),
        "file_count": file_count,
        "size_bytes": size_bytes,
        "source_backend": entry.get("source_backend", "primary"),
    }


@cli.command()
@click.argument("timestamp")
@click.argument("paths", nargs=-1)
@click.option("--output", default="", help="Directory to restore files into. Defaults to project path.")
@click.option("--backend", "backend_name", default="",
              help="Tier 2: restore SOLELY from the named backend (e.g. "
                   "'dropbox'), bypassing the primary-first fallback chain. "
                   "Useful when the primary is down or you know which "
                   "mirror has the version you want.")
@click.option("--dry-run/--no-dry-run", "dry_run", default=False,
              help="Preview every file the restore would write (Path / "
                   "Action / Source backend / Size) without touching local "
                   "disk. Exits 0 after printing the plan. Default: "
                   "--no-dry-run (the actual restore runs as before).")
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
def restore(timestamp: str, paths: tuple, output: str, backend_name: str,
            dry_run: bool, config_path: str) -> None:
    """
    Restore a snapshot to a local directory.

    TIMESTAMP is the snapshot name shown by `claude-mirror snapshots`
    (e.g. 2026-03-05T10-30-00Z).

    PATHS is an optional list of relative paths or fnmatch globs to
    restrict the restore to specific files only — by default the whole
    snapshot is restored. Examples:

    \b
      claude-mirror restore 2026-05-05T10-15-22Z
      claude-mirror restore 2026-05-05T10-15-22Z memory/MOC-Session.md
      claude-mirror restore 2026-05-05T10-15-22Z 'memory/**' --output ~/tmp/recovery
      claude-mirror restore 2026-05-05T10-15-22Z '*.md'
      claude-mirror restore 2026-05-05T10-15-22Z --backend dropbox
      claude-mirror restore 2026-05-05T10-15-22Z --dry-run

    By default, files are restored to the original project path (with
    a confirmation prompt). Use --output to restore to a separate
    directory instead — useful for inspecting before overwriting.

    Pass --dry-run to preview the file list without writing anything to
    local disk. The plan shows every file the restore would touch
    (Path / Action / Source backend / Size); files referencing blobs
    that are no longer present on remote are flagged as
    `missing-blob`. Re-run without --dry-run to apply.

    For blobs-format snapshots, only the requested files' blobs are
    downloaded (no whole-tree fetch). For full-format snapshots, only
    the matching files are downloaded.

    Tier 2 multi-backend: by default, restore tries the primary backend
    first; if the snapshot isn't there, it falls through to each
    configured mirror in order. Pass --backend NAME to force a specific
    target without the fallback chain (e.g. when the primary is
    unreachable or you know which mirror has the right version).
    """
    config = Config.load(_resolve_config(config_path))
    storage, mirrors = _create_storage_set(config)
    snap = SnapshotManager(config, storage, mirrors=mirrors)

    if dry_run:
        try:
            plan = snap.plan_restore(
                timestamp,
                paths=list(paths) if paths else None,
                backend_name=backend_name or None,
            )
        except ValueError as e:
            console.print(f"[red]{e}[/]")
            sys.exit(1)
        _render_restore_plan(plan, paths=list(paths) if paths else None)
        return

    target = output or config.project_path
    if target == config.project_path:
        scope = (
            f"{len(paths)} matching file(s)" if paths else "the entire snapshot"
        )
        click.confirm(
            f"This will overwrite {scope} in {config.project_path}. Continue?",
            abort=True,
        )

    # Install confirm hook so SnapshotManager can prompt the user before
    # falling back to a mirror when the primary is unreachable. The
    # snapshot module is library-grade and must not assume click/stdin
    # is available — the CLI plugs in real prompting here.
    from .snapshots import set_confirm_hook
    set_confirm_hook(lambda msg: click.confirm(msg, default=False))

    snap.restore(
        timestamp, target,
        paths=list(paths) if paths else None,
        backend_name=backend_name or None,
    )


def _render_restore_plan(plan: dict, paths: Optional[list]) -> None:
    """Print a Rich table for `claude-mirror restore --dry-run`. Columns:
    Path / Action / Source backend / Size. Ends with a one-line summary."""
    files = plan["files"]
    fmt = plan["format"]
    timestamp = plan["timestamp"]
    source = plan["source_backend"]

    if not files:
        if paths:
            console.print(
                f"[yellow]Dry-run:[/] no files in snapshot {timestamp} match "
                f"{', '.join(repr(p) for p in paths)}.\n"
                f"[dim]Total files in snapshot: {plan['total_in_snapshot']}.[/]"
            )
        else:
            console.print(
                f"[yellow]Dry-run:[/] snapshot {timestamp} is empty."
            )
        return

    table = Table(
        show_header=True, header_style="bold",
        title=f"Restore plan — snapshot {timestamp} (format={fmt})",
    )
    table.add_column("Path")
    table.add_column("Action")
    table.add_column("Source backend")
    table.add_column("Size", justify="right")

    for f in files:
        size = f.get("size") or 0
        size_str = _human_size(size) if size else "[dim]?[/]"
        action = f["action"]
        if action == "missing-blob":
            action_cell = "[red]missing-blob[/]"
        else:
            action_cell = "[green]restore[/]"
        table.add_row(f["path"], action_cell, source, size_str)

    console.print(table)
    n = len(files)
    console.print(
        f"[bold]Would restore {n} file(s) from snapshot {timestamp}. "
        f"Run without --dry-run to apply.[/]"
    )


@cli.command()
@click.option("--backend", "backend_filter", default="",
              help="Retry only on this one mirror backend (e.g. 'dropbox'). "
                   "Default: retry on every mirror with pending entries.")
@click.option("--dry-run", is_flag=True, default=False,
              help="List what would be retried without re-uploading anything.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def retry(backend_filter: str, dry_run: bool, config_path: str) -> None:
    """Re-attempt previously-failed mirror pushes (Tier 2 only).

    During Tier 2 multi-backend pushes, a transient mirror failure (rate-
    limit, network blip, brief 5xx) marks the file as `pending_retry` in
    the manifest's per-backend remotes map. The next regular push or sync
    automatically retries those entries — but if you don't push again
    soon, you can run `claude-mirror retry` to attempt them on demand.

    \b
    Examples:
      claude-mirror retry                    # retry on every mirror
      claude-mirror retry --backend dropbox  # only retry on dropbox
      claude-mirror retry --dry-run          # preview what would be retried

    Failures during retry are reclassified just as in a normal push:
    transient → still pending (next retry will try again), permanent
    (auth/quota/permission) → flipped to `failed_perm` (awaits user
    action; visible via `claude-mirror status --pending`).

    Primary backend is never touched by retry — its state was already
    `ok` at the time of the original push.
    """
    engine, config, _ = _load_engine(_resolve_config(config_path), with_pubsub=False)
    if not engine._mirrors:
        console.print(
            "[dim]No mirrors configured for this project. Tier 2 multi-backend "
            "is opt-in via `mirror_config_paths` in the project YAML.[/]"
        )
        return
    summary = engine.retry_mirrors(
        backend_filter=backend_filter or None,
        dry_run=dry_run,
    )
    if dry_run:
        return
    if summary["retried"] == 0:
        return
    console.print(
        f"\n[bold]retry complete:[/] "
        f"{summary['succeeded']} succeeded · "
        f"{summary['still_pending']} still pending · "
        f"{summary['permanent']} need user action"
    )
    if summary["permanent"] > 0:
        console.print(
            "[red]Some files have permanent failures (auth / quota / "
            "permission). Run `claude-mirror status --pending` to see "
            "which backends need attention.[/]"
        )


@cli.command("seed-mirror")
@click.option("--backend", "backend_name", default="",
              help="Mirror backend to seed (e.g. 'sftp'). Must match a "
                   "configured mirror's `backend` field. Optional: when "
                   "omitted, seed-mirror auto-detects the candidate if "
                   "exactly one configured mirror has unseeded files.")
@click.option("--dry-run", is_flag=True, default=False,
              help="List which files would be seeded without uploading anything.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def seed_mirror(backend_name: str, dry_run: bool, config_path: str) -> None:
    """Seed a newly-added mirror with files that already exist on the primary.

    \b
    Why this exists: when you add a backend to `mirror_config_paths` for
    a project where files already exist on the primary, regular `push`
    has nothing to do — every local hash matches its manifest record, so
    push uploads zero files and the new mirror's folder stays empty.
    `seed-mirror` walks the manifest, finds every file with no recorded
    state on the named mirror, and uploads each one to that mirror only.
    The primary is never touched.

    \b
    Examples:
      claude-mirror seed-mirror                           # auto-detect when only one mirror is unseeded
      claude-mirror seed-mirror --backend sftp
      claude-mirror seed-mirror --backend sftp --dry-run
      claude-mirror seed-mirror --backend dropbox --config ~/.config/claude_mirror/work.yaml

    Drift safety: a file whose local content has diverged from the
    manifest's recorded `synced_hash` is SKIPPED with a yellow warning
    (it would be wrong to seed mismatched content — the user should run
    `claude-mirror push` first to reconcile primary, which fans out to
    the mirror at the same time, then re-run seed-mirror to catch any
    leftovers).

    Idempotent: running twice in a row is safe — the second invocation
    finds zero unseeded files (everything has `state="ok"` from the
    first run) and exits with a "✓ already seeded" message.
    """
    engine, _, _ = _load_engine(_resolve_config(config_path), with_pubsub=False)
    if not engine._mirrors:
        console.print(
            "[yellow]No mirrors configured for this project.[/] Tier 2 "
            "multi-backend is opt-in via `mirror_config_paths` in the "
            "project YAML."
        )
        sys.exit(1)
    # Auto-detect the target backend when --backend is not supplied: if
    # exactly one configured mirror has unseeded files, infer it; zero
    # → clean exit; multiple → ask the user to disambiguate.
    if not backend_name:
        candidates = []
        for mirror in engine._mirrors:
            name = (getattr(mirror, "backend_name", "") or "")
            if not name:
                continue
            if engine.manifest.unseeded_for_backend(name):
                candidates.append(name)
        if not candidates:
            console.print(
                "[green]✓ No mirrors have unseeded files. Nothing to seed.[/]"
            )
            return
        if len(candidates) > 1:
            names_sorted = sorted(candidates)
            quoted = ", ".join(f"`{n}`" for n in names_sorted)
            console.print(
                f"[red]Multiple mirrors have unseeded files: {quoted}. "
                "Specify `--backend NAME` to choose.[/]"
            )
            sys.exit(1)
        backend_name = candidates[0]
        console.print(
            f"[dim]Auto-detected unseeded mirror: `{backend_name}`[/]"
        )
    try:
        summary = engine.seed_mirror(
            backend_name=backend_name,
            dry_run=dry_run,
        )
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)
    if dry_run or summary["total_unseeded"] == 0:
        return
    if summary["failed"]:
        console.print(
            f"\n[yellow]{summary['failed']} file(s) failed to seed.[/] "
            f"Re-run [bold]claude-mirror seed-mirror --backend {backend_name}[/] "
            "to retry transient failures, or [bold]claude-mirror doctor "
            f"--backend {backend_name}[/] to diagnose persistent ones."
        )


@cli.command()
@click.option("--delete", "do_delete", is_flag=True, default=False,
              help="Actually DELETE orphan blobs. WITHOUT this flag, gc "
                   "runs in dry-run mode and only reports what would be "
                   "deleted (the safe default).")
@click.option("--dry-run", is_flag=True, default=False,
              help="(Same as the default behavior — kept for explicitness "
                   "in scripts that pass it deliberately.)")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="With --delete, skip both confirmation prompts. Required "
                   "for non-interactive use (cron, CI).")
@click.option("--backend", "backend_name", default="",
              help="Tier 2: target a specific backend (primary or any "
                   "configured mirror by `backend_name`, e.g. 'sftp', "
                   "'dropbox'). Default: the primary backend. Use this "
                   "to clean up orphan blobs on a mirror without "
                   "touching the primary, or vice versa.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def gc(do_delete: bool, dry_run: bool, skip_confirm: bool,
       backend_name: str, config_path: str) -> None:
    """Delete blobs no longer referenced by any snapshot manifest.

    \b
    SAFE BY DEFAULT — running `claude-mirror gc` with no flags performs
    a dry-run scan only. To actually delete, you must:
      1. pass --delete explicitly, AND
      2. confirm twice (or pass --yes to skip the prompts).

    \b
    Tier 2: pass --backend NAME to gc a specific mirror. Without it,
    gc operates on the primary backend (matching pre-Tier-2 behaviour
    exactly). Each backend has its own _claude_mirror_blobs/ tree;
    gc on one backend does NOT touch any other.

    \b
    Examples:
      claude-mirror gc                          # primary, dry-run
      claude-mirror gc --delete                 # primary, real delete
      claude-mirror gc --backend sftp           # gc the SFTP mirror, dry-run
      claude-mirror gc --backend sftp --delete  # gc the SFTP mirror for real

    Refuses to run if no blobs-format manifests exist on the chosen
    backend (otherwise gc would wipe the entire blob store).

    Only meaningful when snapshot_format is 'blobs'.
    """
    config = Config.load(_resolve_config(config_path))
    storage, mirrors = _create_storage_set(config)
    snap = SnapshotManager(config, storage, mirrors=mirrors)
    if (config.snapshot_format or "full").lower() != "blobs":
        console.print(
            "[yellow]Note:[/] this project's snapshot_format is "
            f"'{config.snapshot_format}'. gc is only meaningful for the "
            "'blobs' format. Scanning anyway in case stray blobs exist."
        )

    # Up-front banner when running in dry-run mode (no --delete) so the
    # user knows BEFORE the scan starts that nothing will be deleted.
    target_label = backend_name or (
        getattr(storage, "backend_name", "") or config.backend or "primary"
    )
    if not do_delete:
        console.print(
            f"[bold yellow]🔍 DRY-RUN mode[/] — scanning for orphan blob(s) "
            f"on backend [bold]{target_label}[/]; "
            "no deletions will be performed."
        )

    # Phase 1: always run a dry-run scan first so we know the scope.
    try:
        result = snap.gc(dry_run=True, backend_name=backend_name or None)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    if not do_delete:
        if result.get("refused"):
            return
        orphans = result.get("orphans", 0)
        if orphans > 0:
            backend_arg = (
                f" --backend {backend_name}" if backend_name else ""
            )
            console.print(
                f"\n[bold yellow]Dry-run complete.[/] No deletions were performed.\n"
                f"To actually delete {orphans} orphan blob(s) on {target_label}:\n"
                f"  [bold cyan]claude-mirror gc{backend_arg} --delete[/]\n"
                f"[dim](you'll be asked to type YES to confirm before "
                f"anything is deleted)[/]"
            )
        else:
            console.print(
                f"\n[bold yellow]Dry-run complete.[/] Nothing to clean up "
                f"on {target_label} — no orphan blobs.\n"
                f"[dim]When orphans appear in future runs, use: "
                f"[bold]claude-mirror gc --delete[/][/]"
            )
        return

    # --delete path: nothing to do if scan refused or found nothing.
    if result.get("refused") or result.get("orphans", 0) == 0:
        return

    if not skip_confirm:
        orphans = result["orphans"]
        confirmation = click.prompt(
            f"\nThis will permanently delete {orphans} orphan blob(s) "
            f"from {target_label} remote storage.\n"
            f"This cannot be undone via claude-mirror.\n"
            f"Type YES (uppercase, exact) to confirm",
            default="",
            show_default=False,
        )
        if confirmation != "YES":
            console.print(
                "[yellow]Aborted — you typed something other than 'YES'.[/]"
            )
            sys.exit(1)

    # Phase 2: real delete (gc internally re-scans + deletes; the second
    # scan is the cost we pay for the safer default. On most projects
    # it's a few seconds; it never deletes anything not still orphaned
    # at the moment of the second scan, which is the safest semantics.)
    snap.gc(dry_run=False, backend_name=backend_name or None)


@cli.command()
@click.argument("path")
@click.option("--since", "since", default="",
              help="Filter to snapshots taken on or after this point in time. "
                   "Accepts an ISO date (2026-04-15), an ISO datetime "
                   "(2026-04-15T10:00:00Z), or a relative duration: "
                   "Nd / Nw / Nm / Ny  (e.g. 30d, 2w, 3m, 1y).")
@click.option("--until", "until", default="",
              help="Filter to snapshots taken on or before this point in time. "
                   "Same accepted forms as --since: ISO date, ISO datetime, "
                   "or Nd / Nw / Nm / Ny relative duration.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--json", "json_output", is_flag=True, default=False,
              help="Emit a single flat JSON document to stdout instead of "
                   "the Rich timeline table. All Rich output is suppressed; "
                   "on error, a JSON error envelope is written to stderr "
                   "and the process exits 1. Schema: v1.")
def history(path: str, since: str, until: str, config_path: str, json_output: bool) -> None:
    """Show every snapshot that contains PATH, grouped by version.

    Walks every snapshot's manifest and reports which ones contain the
    given file. For `blobs`-format snapshots, the file's SHA-256 lets us
    label distinct versions (v1, v2, ...) so you can spot when the file
    actually changed vs. snapshots taken while it was unchanged. For
    `full`-format snapshots, only presence is reported (no hash without
    downloading the file body).

    \b
    Examples:
      claude-mirror history memory/MOC-Session.md
      claude-mirror history CLAUDE.md
      claude-mirror history memory/notes.md --since 2026-04-15
      claude-mirror history memory/notes.md --since 30d
      claude-mirror history memory/notes.md --since 2026-04-01 --until 2026-04-30

    Pass --since DATE / --until DATE (independently optional) to scan
    only snapshots whose timestamp falls inside the inclusive
    [since, until] window. Both flags accept the same vocabulary as
    `forget --before` — an ISO date, an ISO datetime, or a relative
    duration of the form Nd / Nw / Nm / Ny.

    The output table is newest-first. Each version transition is shown
    in bold green so version changes are easy to spot. Use the timestamp
    of the version you want with `claude-mirror restore` to recover it:

    \b
      claude-mirror restore <timestamp> <path> --output ~/tmp/recovery
    """
    if json_output:
        try:
            with _JsonMode():
                from .snapshots import parse_relative_or_iso_date as _parse_date
                since_dt = _parse_date(since, flag_label="--since") if since else None
                until_dt = _parse_date(until, flag_label="--until") if until else None
                if since_dt is not None and until_dt is not None and since_dt > until_dt:
                    raise ValueError(
                        f"--since ({since}) is later than --until ({until}); "
                        "no snapshots can match an empty range."
                    )
                config = Config.load(_resolve_config(config_path))
                storage = _create_storage(config)
                snap = SnapshotManager(config, storage)
                history_data = snap.history(path, since=since_dt, until=until_dt)
            versions: list[dict] = []
            for entry in history_data.get("entries", []):
                versions.append({
                    "timestamp": entry.get("timestamp", ""),
                    "hash": entry.get("hash"),
                    "size": entry.get("size"),
                    "version": entry.get("version", "?"),
                    "format": entry.get("format", "?"),
                })
            result = {
                "path": history_data.get("path", path),
                "versions": versions,
                "distinct_versions": history_data.get("distinct_versions", 0),
                "total_appearances": history_data.get("total_appearances", 0),
            }
            _emit_json_success("history", result)
            return
        except SystemExit:
            raise
        except BaseException as e:
            _emit_json_error("history", e)
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)

    from .snapshots import parse_relative_or_iso_date

    since_dt = None
    until_dt = None
    try:
        if since:
            since_dt = parse_relative_or_iso_date(since, flag_label="--since")
        if until:
            until_dt = parse_relative_or_iso_date(until, flag_label="--until")
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    if since_dt is not None and until_dt is not None and since_dt > until_dt:
        console.print(
            f"[red]--since ({since}) is later than --until ({until}); "
            "no snapshots can match an empty range.[/]"
        )
        sys.exit(1)

    snap.show_history(path, since=since_dt, until=until_dt)


@cli.command()
@click.argument("timestamp")
@click.option("--paths", "path_filter", default="",
              help="Show only files matching this glob (e.g. 'memory/**', '*.md').")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--backend", "backend_name", default="",
              help="Inspect a specific backend (e.g. 'dropbox'). Default: "
                   "primary first, fall back to mirror(s).")
def inspect(timestamp: str, path_filter: str, config_path: str, backend_name: str) -> None:
    """Show the contents of a snapshot — every file path with its
    SHA-256 hash (blobs format) or size (full format).

    Use this to find a specific file inside a snapshot before recovering
    it. Example workflows:

    \b
      claude-mirror inspect 2026-05-05T10-15-22Z
      claude-mirror inspect 2026-05-05T10-15-22Z --paths 'memory/**'
      claude-mirror inspect 2026-05-05T10-15-22Z --paths '*.md'
      claude-mirror inspect 2026-05-05T10-15-22Z --backend dropbox

    For blobs-format snapshots, this is one cheap manifest download — no
    file bodies are fetched. For full-format snapshots, it's a recursive
    listing of the snapshot folder.

    With Tier 2 mirrors configured, inspect tries the primary backend
    first and transparently falls back to each mirror if the snapshot
    is not found there — consistent with `restore`.
    """
    config = Config.load(_resolve_config(config_path))
    storage, mirrors = _create_storage_set(config)
    snap = SnapshotManager(config, storage, mirrors=mirrors)
    try:
        snap.show_inspect(
            timestamp,
            path_filter=path_filter or None,
            backend_name=backend_name or None,
        )
    except ValueError:
        sys.exit(1)


@cli.command("snapshot-diff")
@click.argument("ts1", metavar="TS1")
@click.argument("ts2", metavar="TS2")
@click.option("--all", "show_all", is_flag=True, default=False,
              help="Include `unchanged` rows in the output. Default: omit "
                   "unchanged files (only added / removed / modified shown).")
@click.option("--paths", "path_filter", default="",
              help="Filter to files whose path matches this fnmatch glob "
                   "(e.g. 'memory/**', '*.md', 'CLAUDE.md').")
@click.option("--unified", "unified_path", default="",
              help="Print a standard unified diff (`diff -u` format) for "
                   "exactly ONE file at the given relative path. Composes "
                   "with shell tools — `claude-mirror snapshot-diff TS1 TS2 "
                   "--unified PATH | less`.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def snapshot_diff(ts1: str, ts2: str, show_all: bool, path_filter: str,
                  unified_path: str, config_path: str) -> None:
    """Show what changed between two snapshots.

    TS1 is the "from" snapshot and TS2 is the "to" snapshot — order
    matters. Pass the literal keyword `latest` for either to use the
    most recent snapshot on remote.

    \b
    Examples:
      claude-mirror snapshot-diff 2026-04-01T10-00-00Z 2026-05-01T10-00-00Z
      claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest
      claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --paths 'memory/**'
      claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --all
      claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --unified CLAUDE.md

    Each file is classified as one of:

    \b
      added       present in TS2, absent in TS1
      removed     present in TS1, absent in TS2
      modified    present in both, content differs (blobs: hash differs;
                  full: file body differs)
      unchanged   present in both, content identical (omitted unless --all)

    For modified rows, the `Changes` column shows `+N -M` line counts
    via difflib on the two file bodies. Files whose bytes are not valid
    UTF-8 are reported as `binary` (no line count) — both snapshots
    must be text for the count to apply.

    Pass --paths PATTERN to filter the table by an fnmatch glob, or
    --unified PATH to print a standard `diff -u` for one file
    (suppresses the table — designed to compose with `less`, `delta`,
    `vim -`, etc.).

    Both blobs-format and full-format snapshots are accepted, and the
    two snapshots may even be in different formats (the older one was
    full, the newer one is blobs after a migrate).
    """
    config = Config.load(_resolve_config(config_path))
    storage, mirrors = _create_storage_set(config)
    snap = SnapshotManager(config, storage, mirrors=mirrors)

    # Resolve `latest` against the actual snapshot list.
    def _resolve(ref: str) -> str:
        if ref != "latest":
            return ref
        listing = snap.list()
        if not listing:
            console.print(
                "[red]Cannot resolve 'latest': no snapshots found on remote.[/]"
            )
            sys.exit(1)
        return listing[0]["timestamp"]

    try:
        resolved_ts1 = _resolve(ts1)
        resolved_ts2 = _resolve(ts2)
        manifest1 = snap.get_snapshot_manifest(resolved_ts1)
        manifest2 = snap.get_snapshot_manifest(resolved_ts2)
    except ValueError as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)

    if unified_path:
        _emit_unified_diff(snap, manifest1, manifest2, unified_path)
        return

    _render_snapshot_diff(
        snap,
        manifest1, manifest2,
        resolved_ts1, resolved_ts2,
        show_all=show_all,
        path_filter=path_filter or None,
    )


def _classify_files(
    manifest1: dict, manifest2: dict,
) -> dict[str, list[str]]:
    """Bucket every path into added/removed/modified/unchanged based on
    the per-format identifier in the two manifest dicts."""
    f1 = manifest1["files"]
    f2 = manifest2["files"]
    all_paths = sorted(set(f1) | set(f2))
    buckets: dict[str, list[str]] = {
        "added": [], "removed": [], "modified": [], "unchanged": [],
    }
    for p in all_paths:
        in1, in2 = p in f1, p in f2
        if in1 and not in2:
            buckets["removed"].append(p)
        elif in2 and not in1:
            buckets["added"].append(p)
        elif f1[p] == f2[p]:
            # blobs: same hash. full: same file_id (rare — only after a
            # no-op snapshot). Treat as unchanged.
            buckets["unchanged"].append(p)
        else:
            buckets["modified"].append(p)
    return buckets


def _line_diff_counts(
    snap: SnapshotManager,
    manifest1: dict, manifest2: dict, path: str,
) -> tuple[Optional[int], Optional[int], bool]:
    """For a `modified` file, fetch both blob bodies and return
    `(plus_count, minus_count, is_binary)`. Returns `(None, None, True)`
    if either body fails UTF-8 decode."""
    import difflib

    ident1 = manifest1["files"][path]
    ident2 = manifest2["files"][path]
    try:
        body1 = snap.get_blob_content(
            ident1,
            backend=manifest1.get("_backend"),
            format_hint=manifest1["format"],
        )
        body2 = snap.get_blob_content(
            ident2,
            backend=manifest2.get("_backend"),
            format_hint=manifest2["format"],
        )
    except Exception:
        return None, None, True

    try:
        text1 = body1.decode("utf-8").splitlines()
        text2 = body2.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None, None, True

    plus = minus = 0
    for line in difflib.unified_diff(text1, text2, lineterm=""):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
    return plus, minus, False


def _render_snapshot_diff(
    snap: SnapshotManager,
    manifest1: dict, manifest2: dict,
    ts1: str, ts2: str,
    show_all: bool,
    path_filter: Optional[str],
) -> None:
    """Print the Path / Status / Changes table for `snapshot-diff`."""
    import fnmatch as _fnmatch

    buckets = _classify_files(manifest1, manifest2)

    def _filter(paths: list[str]) -> list[str]:
        if not path_filter:
            return paths
        return [p for p in paths if _fnmatch.fnmatch(p, path_filter)]

    added = _filter(buckets["added"])
    removed = _filter(buckets["removed"])
    modified = _filter(buckets["modified"])
    unchanged = _filter(buckets["unchanged"]) if show_all else []

    if not (added or removed or modified or unchanged):
        if path_filter:
            console.print(
                f"[yellow]No files differ between {ts1} and {ts2} "
                f"matching {path_filter!r}.[/]"
            )
        else:
            console.print(
                f"[green]Snapshots {ts1} and {ts2} are identical[/] "
                f"(every file's content hash matches)."
            )
        return

    table = Table(
        show_header=True, header_style="bold",
        title=f"snapshot-diff   {ts1}  →  {ts2}",
    )
    table.add_column("Path")
    table.add_column("Status")
    table.add_column("Changes")

    for p in added:
        table.add_row(p, "[green]added[/]", "[dim]—[/]")
    for p in removed:
        table.add_row(p, "[red]removed[/]", "[dim]—[/]")
    for p in modified:
        plus, minus, is_binary = _line_diff_counts(
            snap, manifest1, manifest2, p,
        )
        if is_binary:
            changes_cell = "[dim]binary[/]"
        else:
            changes_cell = f"[green]+{plus}[/] [red]-{minus}[/]"
        table.add_row(p, "[yellow]modified[/]", changes_cell)
    for p in unchanged:
        table.add_row(p, "[dim]unchanged[/]", "[dim]—[/]")

    console.print(table)
    summary_parts = []
    if added:
        summary_parts.append(f"[green]{len(added)} added[/]")
    if removed:
        summary_parts.append(f"[red]{len(removed)} removed[/]")
    if modified:
        summary_parts.append(f"[yellow]{len(modified)} modified[/]")
    if unchanged:
        summary_parts.append(f"[dim]{len(unchanged)} unchanged[/]")
    console.print("Summary: " + ", ".join(summary_parts))


def _emit_unified_diff(
    snap: SnapshotManager,
    manifest1: dict, manifest2: dict,
    path: str,
) -> None:
    """Print a standard `diff -u`-format unified diff for ONE file
    between two snapshots. Composes with shell tools — exits 1 if the
    file is not present in either snapshot or one body is binary."""
    import difflib

    f1 = manifest1["files"]
    f2 = manifest2["files"]
    in1, in2 = path in f1, path in f2
    if not in1 and not in2:
        console.print(
            f"[red]{path}: not present in either snapshot {manifest1['timestamp']} "
            f"or {manifest2['timestamp']}.[/]"
        )
        sys.exit(1)

    def _fetch(manifest: dict) -> bytes:
        if path not in manifest["files"]:
            return b""
        return snap.get_blob_content(
            manifest["files"][path],
            backend=manifest.get("_backend"),
            format_hint=manifest["format"],
        )

    try:
        body1 = _fetch(manifest1)
        body2 = _fetch(manifest2)
    except Exception as e:
        console.print(f"[red]Could not fetch {path}: {e}[/]")
        sys.exit(1)

    try:
        text1 = body1.decode("utf-8").splitlines(keepends=True)
        text2 = body2.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        console.print(
            f"[red]{path}: content is not UTF-8 text in one or both "
            f"snapshots; cannot produce a unified diff.[/]"
        )
        sys.exit(1)

    diff = difflib.unified_diff(
        text1, text2,
        fromfile=f"{path}@{manifest1['timestamp']}",
        tofile=f"{path}@{manifest2['timestamp']}",
        lineterm="",
    )
    # Use click.echo (NOT console.print) so we emit plain text to stdout
    # without Rich markup interpretation — composes with `less`, `delta`,
    # redirection, etc.
    for line in diff:
        click.echo(line)


@cli.command()
@click.argument("timestamps", nargs=-1)
@click.option("--before", default="",
              help="Delete snapshots older than this point in time. "
                   "Accepts an ISO date (2026-04-15), ISO datetime "
                   "(2026-04-15T10:00:00Z), or a relative duration: "
                   "Nd / Nw / Nm / Ny  (e.g. 30d, 2w, 3m).")
@click.option("--keep-last", type=int, default=None,
              help="Keep only the N newest snapshots; delete everything older.")
@click.option("--keep-days", type=int, default=None,
              help="Keep snapshots from the last N days; delete everything older.")
@click.option("--delete", "do_delete", is_flag=True, default=False,
              help="Actually DELETE the matching snapshots. WITHOUT this "
                   "flag, forget runs in dry-run mode (the safe default).")
@click.option("--dry-run", is_flag=True, default=False,
              help="(Same as the default behavior — kept for explicitness.)")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="With --delete, skip both confirmation prompts. Required "
                   "for non-interactive use.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def forget(
    timestamps: tuple,
    before: str,
    keep_last: Optional[int],
    keep_days: Optional[int],
    do_delete: bool,
    dry_run: bool,
    skip_confirm: bool,
    config_path: str,
) -> None:
    """Delete one or more snapshots from remote storage.

    \b
    SAFE BY DEFAULT — running `claude-mirror forget` with no --delete flag
    performs a dry-run only. To actually delete, you must:
      1. pass --delete explicitly, AND
      2. confirm twice (or pass --yes to skip the prompts).

    Selectors (use exactly one):

    \b
      claude-mirror forget TIMESTAMP [TIMESTAMP ...]   one or more explicit timestamps
      claude-mirror forget --before YYYY-MM-DD          everything older than the date
      claude-mirror forget --before 30d                 everything older than 30 days
      claude-mirror forget --keep-last 50               keep newest 50, delete the rest
      claude-mirror forget --keep-days 90               delete anything older than 90 days

    For `full`-format snapshots, the snapshot folder is deleted directly.
    For `blobs`-format snapshots, only the manifest JSON is removed — the
    underlying blobs in `_claude_mirror_blobs/` become orphaned and are
    reclaimable by `claude-mirror gc --delete`.
    """
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)

    selectors = sum(1 for x in (
        list(timestamps),
        before,
        keep_last is not None,
        keep_days is not None,
    ) if x)
    if selectors != 1:
        console.print(
            "[red]forget requires exactly one selector.[/]\n"
            "Pass one or more TIMESTAMP arguments, or one of "
            "--before / --keep-last / --keep-days. Run `claude-mirror forget "
            "--help` for examples."
        )
        sys.exit(1)

    selector_kwargs = dict(
        timestamps=list(timestamps) or None,
        before=before or None,
        keep_last=keep_last,
        keep_days=keep_days,
    )

    # Up-front banner when running in dry-run mode (no --delete) so the
    # user knows BEFORE the scan starts that nothing will be deleted.
    if not do_delete:
        console.print(
            "[bold yellow]🔍 DRY-RUN mode[/] — scanning for matching "
            "snapshot(s); no deletions will be performed."
        )

    # Phase 1: always run a dry-run first so we know the scope.
    preview = snap.forget(**selector_kwargs, dry_run=True)

    if not do_delete:
        selected = preview.get("selected", 0)
        if selected > 0:
            console.print(
                f"\n[bold yellow]Dry-run complete.[/] No deletions were performed.\n"
                f"To actually delete the {selected} matching snapshot(s) "
                f"(use the same selector flags):\n"
                f"  [bold cyan]claude-mirror forget ... --delete[/]\n"
                f"[dim](you'll be asked to type YES to confirm before "
                f"anything is deleted)[/]"
            )
        else:
            console.print(
                "\n[bold yellow]Dry-run complete.[/] Nothing matches the "
                "supplied selector — nothing to delete.\n"
                "[dim]When matches appear in future runs, use: "
                "[bold]claude-mirror forget ... --delete[/][/]"
            )
        return

    if preview.get("selected", 0) == 0:
        return

    if not skip_confirm:
        confirmation = click.prompt(
            f"\nThis will permanently delete {preview['selected']} snapshot(s) "
            f"from remote storage.\n"
            f"This cannot be undone via claude-mirror.\n"
            f"Type YES (uppercase, exact) to confirm",
            default="",
            show_default=False,
        )
        if confirmation != "YES":
            console.print(
                "[yellow]Aborted — you typed something other than 'YES'.[/]"
            )
            sys.exit(1)

    snap.forget(**selector_kwargs, dry_run=False)


@cli.command()
@click.option("--keep-last", "keep_last", type=click.IntRange(min=0, max=100000),
              default=None,
              help="Override config: keep the N newest snapshots regardless of age.")
@click.option("--keep-daily", "keep_daily", type=click.IntRange(min=0, max=10000),
              default=None,
              help="Override config: for the last N days, keep the newest snapshot in each day-bucket.")
@click.option("--keep-monthly", "keep_monthly", type=click.IntRange(min=0, max=10000),
              default=None,
              help="Override config: for the last N months, keep the newest snapshot in each month-bucket.")
@click.option("--keep-yearly", "keep_yearly", type=click.IntRange(min=0, max=10000),
              default=None,
              help="Override config: for the last N years, keep the newest snapshot in each year-bucket.")
@click.option("--delete", "do_delete", is_flag=True, default=False,
              help="Actually DELETE the snapshots outside the retention keep-set. "
                   "WITHOUT this flag, prune runs in dry-run mode (the safe default).")
@click.option("--yes", "skip_confirm", is_flag=True, default=False,
              help="With --delete, skip the typed-YES confirmation prompt. "
                   "Required for non-interactive use (cron, CI).")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def prune(
    keep_last: Optional[int],
    keep_daily: Optional[int],
    keep_monthly: Optional[int],
    keep_yearly: Optional[int],
    do_delete: bool,
    skip_confirm: bool,
    config_path: str,
) -> None:
    """Apply a multi-bucket retention policy to remote snapshots.

    \b
    SAFE BY DEFAULT — running `claude-mirror prune` with no --delete flag
    performs a dry-run only. To actually delete, you must:
      1. pass --delete explicitly, AND
      2. confirm by typing YES (or pass --yes for non-interactive use).

    \b
    Reads the four `keep_*` fields from the project YAML by default:
      keep_last     newest N regardless of age
      keep_daily    one per day for the last N days
      keep_monthly  one per month for the last N months
      keep_yearly   one per year for the last N years
    Each is independent — the union of their keep-sets is retained.
    Any --keep-* CLI flag overrides the corresponding config field for
    this run only (the YAML is not modified).

    \b
    Examples:
      claude-mirror prune                                  # dry-run with config
      claude-mirror prune --delete                         # apply config policy
      claude-mirror prune --keep-last 5 --delete           # ad-hoc one-off
      claude-mirror prune --keep-daily 7 --keep-monthly 12 --delete --yes
    """
    cfg_path = _resolve_config(config_path)
    config = Config.load(cfg_path)
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)

    eff_keep_last    = keep_last    if keep_last    is not None else config.keep_last
    eff_keep_daily   = keep_daily   if keep_daily   is not None else config.keep_daily
    eff_keep_monthly = keep_monthly if keep_monthly is not None else config.keep_monthly
    eff_keep_yearly  = keep_yearly  if keep_yearly  is not None else config.keep_yearly

    if not any((eff_keep_last, eff_keep_daily, eff_keep_monthly, eff_keep_yearly)):
        console.print(
            "[yellow]No retention policy set.[/] All four keep_* fields are 0 "
            "in your config and no override was passed.\n"
            "Either set [bold]keep_last[/] / [bold]keep_daily[/] / [bold]keep_monthly[/] / [bold]keep_yearly[/] "
            "in the project YAML, or pass one or more [bold]--keep-*[/] flags."
        )
        sys.exit(1)

    if not do_delete:
        console.print(
            "[bold yellow]🔍 DRY-RUN mode[/] — scanning for snapshots outside "
            "the retention keep-set; no deletions will be performed."
        )

    preview = snap.prune_per_retention(
        keep_last=eff_keep_last,
        keep_daily=eff_keep_daily,
        keep_monthly=eff_keep_monthly,
        keep_yearly=eff_keep_yearly,
        dry_run=True,
    )

    if not do_delete:
        selected = preview.get("selected", 0)
        if selected > 0:
            console.print(
                f"\n[bold yellow]Dry-run complete.[/] No deletions were performed.\n"
                f"To actually delete the {selected} snapshot(s) outside the keep-set:\n"
                f"  [bold cyan]claude-mirror prune --delete[/]\n"
                f"[dim](you'll be asked to type YES to confirm before anything is deleted)[/]"
            )
        else:
            console.print(
                "\n[bold yellow]Dry-run complete.[/] Every snapshot is inside "
                "the retention keep-set — nothing to delete."
            )
        return

    if preview.get("selected", 0) == 0:
        return

    if not skip_confirm:
        confirmation = click.prompt(
            f"\nThis will permanently delete {preview['selected']} snapshot(s) "
            f"from remote storage.\n"
            f"This cannot be undone via claude-mirror.\n"
            f"Type YES (uppercase, exact) to confirm",
            default="",
            show_default=False,
        )
        if confirmation != "YES":
            console.print(
                "[yellow]Aborted — you typed something other than 'YES'.[/]"
            )
            sys.exit(1)

    snap.prune_per_retention(
        keep_last=eff_keep_last,
        keep_daily=eff_keep_daily,
        keep_monthly=eff_keep_monthly,
        keep_yearly=eff_keep_yearly,
        dry_run=False,
    )


@cli.command("migrate-snapshots")
@click.option("--to", "target", required=True,
              type=click.Choice(["blobs", "full"], case_sensitive=False),
              help="Target snapshot format to convert all existing snapshots into.")
@click.option("--dry-run", is_flag=True, default=False,
              help="List which snapshots would be converted without touching remote storage.")
@click.option("--keep-source", is_flag=True, default=False,
              help="Don't delete source-format artifacts after conversion. Useful for cautious transitions; clean up manually later.")
@click.option("--update-config/--no-update-config", default=True, show_default=True,
              help="After successful migration, update snapshot_format in the project YAML to the target.")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def migrate_snapshots(
    target: str, dry_run: bool, keep_source: bool,
    update_config: bool, config_path: str,
) -> None:
    """Convert all snapshots between 'full' and 'blobs' formats.

    Idempotent and atomic per snapshot — interruptions are safe to retry.
    Both formats coexist throughout, so restore continues to work for any
    not-yet-converted snapshot during the run.
    """
    cfg_path = _resolve_config(config_path)
    config = Config.load(cfg_path)
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)
    target = target.lower()

    summary = snap.migrate(target=target, dry_run=dry_run, keep_source=keep_source)

    if dry_run or not update_config:
        return
    if summary.get("errors", 0):
        console.print(
            "[yellow]Not updating snapshot_format in config: at least one "
            "snapshot failed to convert. Re-run migrate-snapshots to retry.[/]"
        )
        return
    if (config.snapshot_format or "full").lower() != target:
        config.snapshot_format = target
        config.save(cfg_path)
        console.print(
            f"[green]Updated snapshot_format → {target} in[/] {cfg_path}"
        )


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--json", "json_output", is_flag=True, default=False,
              help="Emit a single flat JSON document to stdout instead of "
                   "the human-readable lines. The inbox is still cleared. "
                   "Schema: v1; result.events is the list of inbox event "
                   "dicts (empty list when no events are pending).")
def inbox(config_path: str, json_output: bool) -> None:
    """Show and clear pending notifications for this project."""
    if json_output:
        try:
            with _JsonMode():
                resolved = _resolve_config(config_path)
                config = Config.load(resolved)
                notifications = read_and_clear_inbox(config.project_path)
            _emit_json_success("inbox", {"events": list(notifications)})
            return
        except SystemExit:
            raise
        except BaseException as e:
            _emit_json_error("inbox", e)
    try:
        resolved = _resolve_config(config_path)
        config = Config.load(resolved)
    except Exception:
        # Not in a project directory — silently exit 0 so PreToolUse hooks don't error
        return
    notifications = read_and_clear_inbox(config.project_path)
    if not notifications:
        return  # silent when empty — avoid noise in hook context
    for n in notifications:
        console.print(
            f"[bold blue][{n.get('timestamp', '')[:19].replace('T', ' ')}][/] "
            f"[bold]{n.get('user', '?')}@{n.get('machine', '?')}[/] "
            f"[cyan]{n.get('action', 'updated')}[/] "
            f"{', '.join(n.get('files', []))} "
            f"in '{n.get('project', '')}'"
        )


@cli.command("find-config")
@click.argument("path", default=".", required=False)
def find_config(path: str) -> None:
    """
    Find the config file whose project_path matches PATH (default: current
    directory) or any of its ANCESTOR directories.

    Walks up the directory tree from PATH the way `git` walks up to find
    `.git/` — so `claude-mirror find-config` works from any subdirectory of
    a configured project (e.g. `myproject/memory/notes/` resolves to the
    `myproject.yaml` whose project_path is `myproject/`). Falls back to
    `default.yaml` if no ancestor matches and `default.yaml` exists. On
    total miss, lists every configured project (path + config) on stderr
    so the user knows which `--config <path>` to pass explicitly.

    Used by the Claude Code skill to auto-detect the active project
    without manual --config flags. Walking up parents means the skill
    works whether the user opened Claude Code at the project root or in
    any subdirectory.
    """
    target = Path(path).resolve()
    # Build the search path: target itself + every ancestor up to '/'.
    candidates = [target] + list(target.parents)

    # Pre-load every config once so we don't reparse for each candidate.
    available: list[tuple[Path, str]] = []  # (config_file, resolved_project_path)
    for config_file in sorted(CONFIG_DIR.glob("*.yaml")):
        try:
            cfg = Config.load(str(config_file))
            available.append((config_file, str(Path(cfg.project_path).resolve())))
        except Exception:
            continue

    # Walk up: first config whose resolved project_path matches any
    # ancestor of `target` wins. The closest match (deepest ancestor)
    # is preferred — that's the natural "innermost project" behaviour.
    for candidate in candidates:
        candidate_str = str(candidate)
        for config_file, project_path in available:
            if project_path == candidate_str:
                click.echo(str(config_file))
                return

    # Fall back to default.yaml if it exists.
    default = CONFIG_DIR / "default.yaml"
    if default.exists():
        click.echo(str(default))
        return

    # Total miss: list every available project so the user (or the skill)
    # can pick one. Output goes to stderr to keep stdout empty for any
    # caller that pipes the result, but `claude-mirror find-config` itself
    # exits non-zero so callers can detect the miss.
    click.echo(
        f"No config found for this directory or any parent.",
        err=True,
    )
    if available:
        click.echo(
            f"\n{len(available)} config(s) available — pass one with --config:",
            err=True,
        )
        for config_file, project_path in available:
            click.echo(
                f"  --config {config_file}    # project: {project_path}",
                err=True,
            )
    else:
        click.echo(
            "\nNo configs exist yet. Run `claude-mirror init --wizard` to "
            "create one.",
            err=True,
        )
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────
# `claude-mirror profile` subcommand group (since v0.5.49)
# ──────────────────────────────────────────────────────────────────────────
#
# A profile is a YAML under ~/.config/claude_mirror/profiles/<name>.yaml
# carrying the credential-bearing fields for one logical "account" — Google
# account / Dropbox app / Azure AD app / WebDAV server / SFTP host. Project
# YAMLs reference one by name (`profile: work`) and inherit those fields,
# so a user with five projects on the same Google account doesn't have to
# duplicate `credentials_file` / `token_file` / `gcp_project_id` five times.
#
# `profile create` reuses the same per-backend prompt logic as
# `init --wizard` but writes the result under profiles/<name>.yaml rather
# than as a project config; it only prompts for credential-bearing fields
# (project-specific fields like drive_folder_id are NOT collected here).

@cli.group("profile")
def profile_group() -> None:
    """Manage credentials profiles (~/.config/claude_mirror/profiles/).

    A profile bundles the credentials-bearing fields shared across several
    projects (e.g. one Google account used by five projects). Reference
    one by name with the global `--profile NAME` flag, or set
    `profile: NAME` at the top of a project YAML.
    """


@profile_group.command("list")
def profile_list() -> None:
    """List every profile under ~/.config/claude_mirror/profiles/."""
    from .profiles import list_profiles, profile_summary, _profiles_dir

    names = list_profiles()
    if not names:
        d = _profiles_dir()
        console.print(
            f"[yellow]No profiles configured.[/] Profiles directory: {d}\n"
            "Create one with [bold]claude-mirror profile create NAME --backend BACKEND[/]."
        )
        return

    table = Table(title="Profiles")
    table.add_column("Name", style="bold")
    table.add_column("Backend")
    table.add_column("Description", overflow="fold")
    table.add_column("Path", style="dim")
    for n in names:
        s = profile_summary(n)
        table.add_row(s["name"], s["backend"] or "[dim](unset)[/]",
                      s["description"] or "[dim](none)[/]", s["path"])
    console.print(table)


@profile_group.command("show")
@click.argument("name")
def profile_show(name: str) -> None:
    """Print the raw YAML of profile NAME to stdout."""
    from .profiles import profile_path

    path = profile_path(name)
    if not path.exists():
        from .profiles import list_profiles
        available = list_profiles()
        if available:
            avail_str = ", ".join(available)
            console.print(
                f"[red]✗ profile '{name}' not found at {path}.[/] "
                f"Available profiles: {avail_str}."
            )
        else:
            console.print(
                f"[red]✗ profile '{name}' not found at {path}.[/] "
                f"No profiles configured yet."
            )
        sys.exit(1)
    click.echo(path.read_text())


@profile_group.command("create")
@click.argument("name")
@click.option(
    "--backend", "backend_opt",
    type=click.Choice(
        ["googledrive", "dropbox", "onedrive", "webdav", "sftp"],
        case_sensitive=False,
    ),
    required=True,
    help="Storage backend the profile will hold credentials for.",
)
@click.option(
    "--description", "description", default="",
    help="Optional human-readable description shown by `profile list`.",
)
@click.option(
    "--force", is_flag=True, default=False,
    help="Overwrite an existing profile YAML at the target path.",
)
def profile_create(
    name: str, backend_opt: str, description: str, force: bool,
) -> None:
    """Interactively scaffold a new credentials profile.

    Prompts for the credential-bearing fields of the chosen backend
    (credentials_file / token_file / dropbox_app_key / onedrive_client_id
    / webdav_url+username+password / sftp_host+username+key+folder)
    and writes the result to
    ~/.config/claude_mirror/profiles/NAME.yaml. Project-specific fields
    (drive_folder_id, dropbox_folder, onedrive_folder, webdav-folder
    suffix, etc.) are NOT collected here — those belong on each project
    YAML.
    """
    from .profiles import profile_path, _profiles_dir

    backend = backend_opt.lower()
    target = profile_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() and not force:
        console.print(
            f"[red]✗ profile '{name}' already exists at {target}.[/]\n"
            f"Pass [bold]--force[/] to overwrite, or pick a different name."
        )
        sys.exit(1)

    console.print(
        f"\n[bold cyan]Creating profile '{name}' ({backend})[/]\n"
        f"[dim]Will be written to: {target}[/]\n"
    )

    profile_data: dict[str, Any] = {"backend": backend}
    if description:
        profile_data["description"] = description

    if backend == "googledrive":
        console.print(
            "[dim]Credentials file: OAuth2 'Desktop app' client JSON "
            "downloaded from Google Cloud Console.[/]\n"
        )
        creds = click.prompt(
            "Credentials file", default=_DEFAULT_CREDENTIALS,
            value_proc=_byo_wizard.validate_credentials_file,
        )
        token = click.prompt(
            "Token file", default=_derive_token_file(creds),
        )
        gcp = click.prompt(
            "GCP project ID (optional, for Pub/Sub)", default="",
        )
        profile_data["credentials_file"] = str(Path(creds).expanduser())
        profile_data["token_file"] = str(Path(token).expanduser())
        if gcp:
            profile_data["gcp_project_id"] = gcp
    elif backend == "dropbox":
        console.print(
            "\n[dim]Dropbox app key: from your app at dropbox.com/developers.[/]\n"
        )
        app_key = click.prompt("Dropbox app key")
        token = click.prompt(
            "Token file", default=str(CONFIG_DIR / f"dropbox-{name}-token.json"),
        )
        profile_data["dropbox_app_key"] = app_key
        profile_data["token_file"] = str(Path(token).expanduser())
    elif backend == "onedrive":
        console.print(
            "\n[dim]Azure app client ID: portal.azure.com → App registrations.[/]\n"
        )
        client_id = click.prompt("Azure app client ID")
        token = click.prompt(
            "Token file", default=str(CONFIG_DIR / f"onedrive-{name}-token.json"),
        )
        profile_data["onedrive_client_id"] = client_id
        profile_data["token_file"] = str(Path(token).expanduser())
    elif backend == "webdav":
        console.print(
            "\n[dim]WebDAV URL: full URL to the WebDAV root for this account.[/]\n"
        )
        url = click.prompt("WebDAV URL")
        username = click.prompt("Username")
        import getpass
        password = getpass.getpass("Password: ")
        token = click.prompt(
            "Token file", default=str(CONFIG_DIR / f"webdav-{name}-token.json"),
        )
        profile_data["webdav_url"] = url
        profile_data["webdav_username"] = username
        profile_data["webdav_password"] = password
        profile_data["token_file"] = str(Path(token).expanduser())
        if url.startswith("http://"):
            profile_data["webdav_insecure_http"] = True
    elif backend == "sftp":
        console.print(
            "\n[dim]SFTP host + auth credentials. The remote folder lives on the "
            "project YAML, NOT on the profile.[/]\n"
        )
        host = click.prompt("SFTP host")
        port = click.prompt("SFTP port", default=22, type=int)
        username = click.prompt("SFTP username")
        auth_choice = click.prompt(
            "Authenticate with [k]ey or [p]assword?",
            default="k",
            type=click.Choice(["k", "p"], case_sensitive=False),
        ).lower()
        profile_data["sftp_host"] = host
        profile_data["sftp_port"] = port
        profile_data["sftp_username"] = username
        if auth_choice == "k":
            key_file = click.prompt(
                "SSH private key file", default="~/.ssh/id_ed25519",
            )
            profile_data["sftp_key_file"] = str(Path(key_file).expanduser())
        else:
            import getpass
            profile_data["sftp_password"] = getpass.getpass("SFTP password: ")
        profile_data["sftp_known_hosts_file"] = "~/.ssh/known_hosts"
        token = click.prompt(
            "Token file", default=str(CONFIG_DIR / f"sftp-{name}-token.json"),
        )
        profile_data["token_file"] = str(Path(token).expanduser())

    import yaml as _yaml
    with open(target, "w") as f:
        _yaml.dump(profile_data, f, default_flow_style=False, sort_keys=False)
    # Lock down the profile YAML — token paths plus (for WebDAV) a
    # password may be written here. 0600 matches the chmod we apply to
    # token files themselves.
    try:
        target.chmod(0o600)
    except OSError:
        pass

    console.print(
        f"\n[green]✓ Profile '{name}' written to[/] {target}\n"
        f"[dim]Use it with[/] [bold]claude-mirror --profile {name} <command>[/]\n"
        f"[dim]or set[/] [bold]profile: {name}[/] [dim]at the top of a project YAML.[/]"
    )


@profile_group.command("delete")
@click.argument("name")
@click.option(
    "--delete", "do_delete", is_flag=True, default=False,
    help="Actually DELETE the profile YAML. WITHOUT this flag, runs in "
         "dry-run mode (the safe default).",
)
@click.option(
    "--yes", "skip_confirm", is_flag=True, default=False,
    help="With --delete, skip the typed-YES confirmation prompt. Required "
         "for non-interactive use.",
)
def profile_delete(name: str, do_delete: bool, skip_confirm: bool) -> None:
    """Delete a credentials profile.

    \b
    SAFE BY DEFAULT — running without --delete performs a dry-run only.
    To actually delete, you must:
      1. pass --delete explicitly, AND
      2. confirm by typing the literal word YES (or pass --yes).
    """
    from .profiles import profile_path

    target = profile_path(name)
    if not target.exists():
        console.print(
            f"[yellow]Profile '{name}' does not exist at {target} — nothing to delete.[/]"
        )
        return

    if not do_delete:
        console.print(
            f"[bold yellow]🔍 DRY-RUN mode[/] — profile '{name}' at {target} "
            f"WOULD be deleted.\n"
            f"To actually delete: [bold cyan]claude-mirror profile delete {name} "
            f"--delete[/]\n"
            f"[dim](you'll be asked to type YES to confirm)[/]"
        )
        return

    if not skip_confirm:
        confirmation = click.prompt(
            f"\nThis will permanently delete profile '{name}' "
            f"({target}).\n"
            f"This cannot be undone via claude-mirror.\n"
            f"Type YES (uppercase, exact) to confirm",
            default="",
            show_default=False,
        )
        if confirmation != "YES":
            console.print(
                "[yellow]Aborted — you typed something other than 'YES'.[/]"
            )
            sys.exit(1)

    target.unlink()
    console.print(f"[green]✓ Profile '{name}' deleted from {target}.[/]")


@cli.command("test-notify")
def test_notify() -> None:
    """Send a test desktop notification and print permission setup instructions."""
    import platform as _platform
    from .notifier import Notifier

    system = _platform.system()

    # Always print permission instructions first
    if system == "Darwin":
        console.print(
            "\n[bold]macOS notification permission setup[/]\n"
            "\n"
            "If the test notification does not appear, grant permission manually:\n"
            "\n"
            "  1. Open [bold]System Settings → Notifications[/]\n"
            "  2. Scroll down and find [bold]Terminal[/] (or iTerm2 / whichever app you use)\n"
            "     If it is not listed, this test will trigger its first appearance — scroll again after.\n"
            "  3. Enable [bold]Allow Notifications[/]\n"
            "  4. Set alert style to [bold]Alerts[/] or [bold]Banners[/] (not Off)\n"
            "\n"
            "  If you run claude-mirror as a launchd service, the notification is sent on\n"
            "  behalf of the launchd agent, which has no app bundle. In that case:\n"
            "  • Run claude-mirror watch once from a [bold]Terminal window[/] to get the\n"
            "    permission entry created, grant it, then switch to the launchd service.\n"
        )
    elif system == "Linux":
        console.print(
            "\n[bold]Linux notification setup[/]\n"
            "\n"
            "Notifications use [bold]notify-send[/] (libnotify). If nothing appears:\n"
            "\n"
            "  • Install libnotify:  [bold]sudo apt install libnotify-bin[/]  (Debian/Ubuntu)\n"
            "                        [bold]sudo dnf install libnotify[/]       (Fedora)\n"
            "  • A notification daemon must be running (most desktop environments\n"
            "    include one: GNOME, KDE, XFCE, etc.)\n"
            "  • If running as a systemd service with no display, set:\n"
            "    [bold]Environment=DISPLAY=:0[/] and [bold]Environment=DBUS_SESSION_BUS_ADDRESS=...[/]\n"
            "    in the service unit file.\n"
        )

    # Send the test notification
    notifier = Notifier(str(Path.home()))
    console.print("Sending test notification...\n")
    try:
        if system == "Darwin":
            notifier._notify_macos("claude-mirror", "Desktop notifications are working correctly.")
        elif system == "Linux":
            notifier._notify_linux("claude-mirror", "Desktop notifications are working correctly.")
        else:
            notifier._notify_windows("claude-mirror", "Desktop notifications are working correctly.")
        console.print("[green]✓ Notification sent.[/] Check your desktop — if it did not appear, follow the instructions above.")
    except Exception as e:
        console.print(f"[red]✗ Notification failed:[/] {e}")


@cli.command()
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--limit", default=20, show_default=True, help="Number of events to show.")
@click.option("--json", "json_output", is_flag=True, default=False,
              help="Emit a single flat JSON document to stdout instead of "
                   "the Rich table. result is a list of activity-log "
                   "entries newest-first; empty list when the log is empty "
                   "or absent. Schema: v1.")
def log(config_path: str, limit: int, json_output: bool) -> None:
    """Show recent sync activity from collaborators."""
    if json_output:
        try:
            with _JsonMode():
                config = Config.load(_resolve_config(config_path))
                storage = _create_storage(config)
                logs_folder_id = storage.get_file_id(LOGS_FOLDER, config.root_folder)
                log_file_id = (
                    storage.get_file_id(SYNC_LOG_NAME, logs_folder_id)
                    if logs_folder_id else None
                )
                if not log_file_id:
                    _emit_json_success("log", [])
                    return
                raw = storage.download_file(log_file_id)
                sync_log = SyncLog.from_bytes(raw)
            events = sync_log.events[-limit:]
            payload: list[dict] = []
            # Newest-first to match the Rich render.
            for event in reversed(events):
                payload.append({
                    "timestamp": event.timestamp,
                    "user": event.user,
                    "machine": event.machine,
                    "action": event.action,
                    "files": list(event.files),
                    "project": event.project,
                    # snapshot_timestamp is reserved by the v1 schema;
                    # SyncEvent does not record one today, so we always
                    # emit null. Future versions that thread snapshot
                    # timestamps through to the log will populate this.
                    "snapshot_timestamp": None,
                })
            _emit_json_success("log", payload)
            return
        except SystemExit:
            raise
        except BaseException as e:
            _emit_json_error("log", e)

    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)

    from ._progress import make_phase_progress
    with make_phase_progress(console) as progress:
        load_task = progress.add_task(
            "Log", total=None, detail="locating sync log…", show_time=True,
        )
        logs_folder_id = storage.get_file_id(LOGS_FOLDER, config.root_folder)
        log_file_id = (
            storage.get_file_id(SYNC_LOG_NAME, logs_folder_id)
            if logs_folder_id else None
        )
        if not log_file_id:
            progress.remove_task(load_task)
            console.print("[dim]No sync log found. Push some files first.[/]")
            return
        progress.update(load_task, detail="downloading sync log…")
        raw = storage.download_file(log_file_id)
        progress.update(load_task, detail=f"parsing {len(raw)} byte(s)…")
        sync_log = SyncLog.from_bytes(raw)
        progress.update(load_task, detail="completed")

    events = sync_log.events[-limit:]
    if not events:
        console.print("[dim]No events yet.[/]")
        return

    table = Table(title="Sync Log", show_header=True)
    table.add_column("Time", style="dim")
    table.add_column("User@Machine")
    table.add_column("Action")
    table.add_column("Files")

    for event in reversed(events):
        table.add_row(
            event.timestamp[:19].replace("T", " "),
            f"{event.user}@{event.machine}",
            (
                f"[cyan]{event.action}[/]" if event.action == "push"
                else f"[red]{event.action}[/]" if event.action == "delete"
                else f"[blue]{event.action}[/]"
            ),
            ", ".join(event.files),
        )

    console.print(table)




# ──────────────────────────────────────────────────────────────────────────
# completion — emit shell-completion source for the user to eval/source
#
# Click 8+ supports tab-completion natively. The traditional bootstrap is
#   eval "$(_CLAUDE_MIRROR_COMPLETE=zsh_source claude-mirror)"
# which is opaque enough that nobody discovers it. This command prints the
# same script with one obvious invocation:
#   eval "$(claude-mirror completion zsh)"
# ──────────────────────────────────────────────────────────────────────────

# PowerShell completion source template. Click 8.3 ships native completion
# classes for bash / zsh / fish but NOT PowerShell, so we define one here
# (subclassing click.shell_completion.ShellComplete) using the same
# `<NAME>_complete` env-var protocol the other shells use. The script
# registers an ArgumentCompleter via PowerShell's Register-ArgumentCompleter
# cmdlet — invoked once when the user dot-sources the script (or it lands
# in their `$PROFILE`), live for every subsequent claude-mirror tab-press.
_POWERSHELL_COMPLETION_SOURCE = """\
Register-ArgumentCompleter -Native -CommandName %(prog_name)s -ScriptBlock {
    param($wordToComplete, $commandAst, $cursorPosition)

    $env:%(complete_var)s = "powershell_complete"
    $env:COMP_WORDS = $commandAst.ToString()
    $env:COMP_CWORD = $cursorPosition

    & %(prog_name)s | ForEach-Object {
        $parts = $_ -split ',', 3
        $type = $parts[0]
        $value = $parts[1]
        $help = if ($parts.Length -ge 3) { $parts[2] } else { '' }

        if ($type -eq 'plain') {
            [System.Management.Automation.CompletionResult]::new(
                $value, $value, 'ParameterValue', $help
            )
        }
        elseif ($type -eq 'file' -or $type -eq 'dir') {
            [System.Management.Automation.CompletionResult]::new(
                $value, $value, 'ProviderItem', $value
            )
        }
    }

    Remove-Item Env:\\%(complete_var)s -ErrorAction SilentlyContinue
    Remove-Item Env:\\COMP_WORDS -ErrorAction SilentlyContinue
    Remove-Item Env:\\COMP_CWORD -ErrorAction SilentlyContinue
}
"""


def _build_powershell_complete_class():
    """Return a `PowerShellComplete` ShellComplete subclass.

    Defined as a function so Click's `ShellComplete` import only happens
    when `completion powershell` is actually invoked — keeping the
    `claude-mirror --help` import time unchanged for the common path.
    """
    from click.shell_completion import ShellComplete, CompletionItem

    class PowerShellComplete(ShellComplete):
        """Click shell-completion adapter for PowerShell.

        Click 8.3 does not ship a native PowerShell adapter, so we
        define one matching the same ``<COMPLETE_VAR>_source`` /
        ``<COMPLETE_VAR>_complete`` env-var protocol the bundled
        adapters use. Discoverable as `claude-mirror completion
        powershell`.
        """

        name = "powershell"
        source_template = _POWERSHELL_COMPLETION_SOURCE

        def get_completion_args(self) -> tuple[list[str], str]:
            """Pull the partial command line out of the env vars set by
            the source-template script. Mirrors the bash/zsh strategy:
            the shell hands us the full word vector + cursor position;
            we slice on cursor position to compute (args, incomplete).
            """
            import shlex

            words_str = os.environ.get("COMP_WORDS", "")
            try:
                cwords = shlex.split(words_str, posix=False)
            except ValueError:
                cwords = words_str.split()
            # Drop the program name itself from the front so `args` is
            # the list of completed args, matching the contract the
            # other ShellComplete adapters fulfil.
            args = cwords[1:] if len(cwords) > 1 else []
            # PowerShell hands us the cursor position — for the
            # purposes of completion, the incomplete is whatever sits
            # after the last separator; lift it off the args list.
            if args and not words_str.endswith(" "):
                incomplete = args.pop()
            else:
                incomplete = ""
            return args, incomplete

        def format_completion(self, item: "CompletionItem") -> str:
            """Format one completion item into the ``type,value,help``
            string the source-template script splits on.
            """
            return f"{item.type},{item.value},{item.help or ''}"

    return PowerShellComplete


@cli.command()
@click.argument(
    "shell",
    type=click.Choice(["bash", "zsh", "fish", "powershell"], case_sensitive=False),
)
def completion(shell: str) -> None:
    """Emit shell tab-completion source for claude-mirror.

    Add to your shell's startup file:

    \b
      # zsh — append to ~/.zshrc
      eval "$(claude-mirror completion zsh)"

    \b
      # bash — append to ~/.bashrc
      eval "$(claude-mirror completion bash)"

    \b
      # fish — write to the completions dir
      claude-mirror completion fish > ~/.config/fish/completions/claude-mirror.fish

    \b
      # PowerShell — append to your profile
      claude-mirror completion powershell | Out-File -Encoding utf8 -Append $PROFILE.CurrentUserAllHosts

    After restarting your shell, `claude-mirror <TAB>` completes commands
    and `claude-mirror push <TAB>` completes flag names. High-value flags
    (--config, --backend) also complete their values.
    """
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    shell_classes = {
        "bash": BashComplete,
        "zsh": ZshComplete,
        "fish": FishComplete,
        "powershell": _build_powershell_complete_class(),
    }
    cls = shell_classes[shell.lower()]
    comp = cls(
        cli=cli,
        ctx_args={},
        prog_name="claude-mirror",
        complete_var="_CLAUDE_MIRROR_COMPLETE",
    )
    # `.source()` returns the shell-specific script as a string
    click.echo(comp.source())


# ──────────────────────────────────────────────────────────────────────────
# `claude-mirror doctor` — one-shot configuration diagnosis
# ──────────────────────────────────────────────────────────────────────────
# Runs through every common configuration check and prints concrete fix
# commands when something is wrong. Exit code 0 if every check passes, 1
# if any fail — composes cleanly with shell scripts and CI.
#
# Replaces the "why isn't my sync working?" support-thread loop with a
# single command. Each check is independent and renders its result as it
# runs; later checks still execute even if earlier ones fail, so the user
# sees the FULL set of issues in one pass rather than playing whack-a-mole.
# ──────────────────────────────────────────────────────────────────────────


# ───────────────────────────────────────────────────────────────────────────
# Google Drive deep diagnostic constants + seams
# ───────────────────────────────────────────────────────────────────────────
#
# Service-account email that Google Drive's push-notification subsystem uses
# to publish change events into a user-owned Pub/Sub topic. When you call
# `drive.changes.watch(topicName=...)`, Drive's backend authenticates as
# THIS account when posting to the topic, so the topic's IAM policy MUST
# include this principal with role `roles/pubsub.publisher`.
#
# Source: Google Drive API Push Notifications guide,
# https://developers.google.com/drive/api/guides/push#initial-requirements
#
# About 70% of self-serve Drive setups miss this grant — Pub/Sub appears to
# work (subscribe / publish from the user's own credentials succeeds) but
# Drive itself silently fails to publish change events, so other machines
# never receive notifications. The deep doctor surfaces it explicitly.
_DRIVE_PUBSUB_PUBLISHER_SA = "apps-storage-noreply@google.com"

# OAuth scope identifiers we expect to find on a fully-configured token.
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
_PUBSUB_SCOPE = "https://www.googleapis.com/auth/pubsub"


def _googledrive_deep_check_factory(
    config: "Config", token_path: "Path"
) -> dict:
    """Build the OAuth credentials + Pub/Sub admin client used by the deep
    Google Drive doctor checks.

    Lazily imports the Google Cloud Pub/Sub SDK so the cost is only paid
    when `claude-mirror doctor` is actually inspecting a Drive backend.
    Most doctor invocations are quick health checks; we don't want them
    paying the multi-hundred-millisecond gRPC import cost.

    Returns a dict with keys:
      publisher    — google.cloud.pubsub_v1.PublisherClient instance
      creds        — google.oauth2.credentials.Credentials instance
      scopes       — list of granted scope URIs (from token JSON)
      auth_error   — exception or None; non-None means token is unusable
                     (corrupt / wrong shape / refresh failed). When set,
                     the deep checks emit ONE auth-bucket failure rather
                     than five identical "auth needed" lines.

    Tests monkeypatch this whole function to inject a stub publisher /
    scope list, which is why it's a module-level seam rather than inline.
    """
    import json as _json_local  # local alias avoids shadowing module-level import

    # Read scopes directly from the token JSON. We do this BEFORE building
    # OAuth `Credentials` so that even a token with a missing/malformed
    # scopes field doesn't crash the factory — we just report empty scopes
    # and let the calling check fail with a clear message.
    scopes: list[str] = []
    auth_error: Optional[BaseException] = None
    try:
        token_data = _json_local.loads(token_path.read_text())
        if isinstance(token_data, dict):
            raw_scopes = token_data.get("scopes")
            if isinstance(raw_scopes, list):
                scopes = [str(s) for s in raw_scopes]
            elif isinstance(raw_scopes, str):
                # Legacy token format: scopes as space-separated string.
                scopes = raw_scopes.split()
    except (OSError, _json_local.JSONDecodeError) as e:
        auth_error = e

    # Lazy-import — pays the cost only on the Drive deep path.
    creds = None
    publisher = None
    try:
        from google.oauth2.credentials import Credentials  # noqa: PLC0415
        from google.cloud import pubsub_v1  # noqa: PLC0415

        creds = Credentials.from_authorized_user_file(
            str(token_path),
            [_DRIVE_SCOPE, _PUBSUB_SCOPE],
        )
        publisher = pubsub_v1.PublisherClient(credentials=creds)
    except BaseException as e:  # noqa: BLE001 — diagnostic must not bubble
        if auth_error is None:
            auth_error = e

    return {
        "publisher": publisher,
        "creds": creds,
        "scopes": scopes,
        "auth_error": auth_error,
    }


def _run_googledrive_deep_checks(path: str, config: "Config") -> list[str]:
    """Drive-specific deep diagnostic checks.

    Runs AFTER the generic credentials/token/connectivity checks have
    already reported. Adds Drive-specific assertions that the generic
    pass cannot make:

      1. OAuth scopes granted (Drive required, Pub/Sub optional but
         needed for Pub/Sub-related checks 4-6).
      2. Drive API enabled in the GCP project.
      3. Pub/Sub API enabled.
      4. Pub/Sub topic exists at the configured projects/PROJECT/topics/TOPIC.
      5. Per-machine subscription exists at the canonical name pattern
         (config.subscription_id, defined as `{topic}-{machine_safe}`).
      6. Drive's service account has `roles/pubsub.publisher` on the
         topic — the killer "70% of users miss this" check.

    Returns the list of failure summary strings (empty ⇒ all-pass).
    Renders ✓ / ✗ / ⚠ lines to `console` as it goes, matching the
    surrounding doctor's output style.

    Auth failures are bucketed: if the saved token is unusable, ONE
    AUTH-class failure line is emitted and checks 2-6 are skipped, so
    the user sees a single "fix: re-run claude-mirror auth" rather than
    five identical lines for the same root cause.
    """
    failures: list[str] = []

    # Skip the entire deep section gracefully when the user hasn't even
    # configured a GCP project or Pub/Sub topic. The Drive backend itself
    # tolerates missing Pub/Sub config (it just disables real-time push);
    # we mirror that here with an info line rather than failing.
    gcp_project_id = (config.gcp_project_id or "").strip()
    pubsub_topic_id = (config.pubsub_topic_id or "").strip()
    if not gcp_project_id or not pubsub_topic_id:
        console.print(
            "  [yellow]⚠[/] Pub/Sub not configured "
            "([dim]gcp_project_id / pubsub_topic_id empty[/]) — "
            "skipping deep Drive checks. Real-time notifications "
            "won't work without these. "
            "[yellow]Fix:[/] run "
            f"[bold]claude-mirror init --wizard --config {path}[/] "
            "to add Pub/Sub settings."
        )
        return failures

    token_path = Path(config.token_file)
    if not token_path.exists():
        # The generic check above already emitted a failure for the
        # missing token file; emitting a second "deep checks skipped"
        # message would just be noise. Bail silently.
        return failures

    # ───── Build the publisher + scopes via the test seam ─────
    factory_result = _googledrive_deep_check_factory(config, token_path)
    publisher = factory_result["publisher"]
    scopes: list[str] = list(factory_result.get("scopes") or [])
    auth_error = factory_result.get("auth_error")

    # ───── Check 1: OAuth scopes granted ─────
    has_drive_scope = _DRIVE_SCOPE in scopes
    has_pubsub_scope = _PUBSUB_SCOPE in scopes

    if not has_drive_scope:
        console.print(
            "  [red]✗[/] OAuth Drive scope not granted "
            f"([dim]{_DRIVE_SCOPE}[/])\n"
            "      [yellow]Fix:[/] run "
            f"[bold]claude-mirror auth --config {path}[/] and approve "
            "the Drive scope on the consent screen."
        )
        failures.append("OAuth Drive scope not granted")
        # Without Drive scope nothing else works — bail before issuing
        # five more API errors that all root-cause to the same fix.
        return failures

    if has_pubsub_scope:
        console.print(
            "  [green]✓[/] OAuth scopes: "
            "Drive [green]✓[/], Pub/Sub [green]✓[/]"
        )
    else:
        console.print(
            "  [yellow]⚠[/] OAuth scopes: Drive [green]✓[/], "
            "Pub/Sub [yellow]not granted[/]; "
            "skipping Pub/Sub checks. Re-run "
            f"[bold]claude-mirror auth --config {path}[/] "
            "to add the scope if you want real-time notifications."
        )
        # No Pub/Sub scope ⇒ checks 2-6 cannot succeed anyway. Pretend
        # we ran them and return clean — the user opted out of Pub/Sub.
        return failures

    # If the credentials object itself couldn't be loaded (corrupt token,
    # OAuth refresh blew up at construction time), bucket it as ONE auth
    # failure — calling get_topic / get_iam_policy below would just spew
    # five copies of the same root cause.
    if auth_error is not None or publisher is None:
        err_text = str(auth_error) if auth_error is not None else "unknown"
        console.print(
            "  [red]✗[/] OAuth credentials cannot be used for Pub/Sub "
            f"admin calls: [dim]{err_text[:200]}[/]\n"
            "      [yellow]Fix:[/] re-run "
            f"[bold]claude-mirror auth --config {path}[/] to refresh "
            "the token; if the failure persists, the saved scopes may "
            "not include Pub/Sub admin permissions."
        )
        failures.append("OAuth credentials unusable for Pub/Sub admin")
        return failures

    # Lazy-import Pub/Sub error classes — same import-cost rationale as
    # the publisher itself, plus we need them to classify exceptions
    # raised by the publisher's RPC methods below.
    from google.api_core.exceptions import (  # noqa: PLC0415
        NotFound,
        PermissionDenied,
        Unauthenticated,
        FailedPrecondition,
        ServiceUnavailable,
        DeadlineExceeded,
        GoogleAPICallError,
    )
    from google.auth.exceptions import RefreshError  # noqa: PLC0415

    topic_path = (
        f"projects/{gcp_project_id}/topics/{pubsub_topic_id}"
    )
    subscription_path = (
        f"projects/{gcp_project_id}/subscriptions/"
        f"{config.subscription_id}"
    )

    def _classify(exc: BaseException) -> str:
        """Map an SDK exception to one of: api_disabled, auth, not_found,
        permission, transient, unknown."""
        text = str(exc)
        if isinstance(exc, RefreshError) or "invalid_grant" in text.lower():
            return "auth"
        if isinstance(exc, Unauthenticated):
            return "auth"
        # "API has not been used in project X before or it is disabled"
        # is the canonical Google Cloud "API not enabled" error string.
        if "has not been used in project" in text or "is disabled" in text:
            return "api_disabled"
        if isinstance(exc, FailedPrecondition) and (
            "API" in text or "enable" in text.lower()
        ):
            return "api_disabled"
        if isinstance(exc, NotFound):
            return "not_found"
        if isinstance(exc, PermissionDenied):
            return "permission"
        if isinstance(exc, (ServiceUnavailable, DeadlineExceeded)):
            return "transient"
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return "transient"
        return "unknown"

    # Track whether we've already emitted the AUTH bucket so a cascading
    # auth failure across checks 2-6 surfaces ONCE not five times.
    auth_bucket_reported = False

    def _maybe_auth_bucket(exc: BaseException) -> bool:
        """If `exc` looks like AUTH-class, emit ONE bucket line (only
        the first time) and return True. Caller skips its own per-check
        message. Returns False otherwise."""
        nonlocal auth_bucket_reported
        if _classify(exc) != "auth":
            return False
        if not auth_bucket_reported:
            auth_bucket_reported = True
            console.print(
                "  [red]✗[/] Pub/Sub admin auth failed "
                f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/])\n"
                "      [yellow]Fix:[/] re-run "
                f"[bold]claude-mirror auth --config {path}[/] — "
                "remaining Pub/Sub checks skipped to avoid duplicate "
                "failures from the same root cause."
            )
            failures.append("Pub/Sub admin auth failed")
        return True

    # ───── Check 2: Drive API enabled ─────
    # Cheap probe: drive.about.get(fields="user"). 403 with the canonical
    # "API has not been used in project X" string ⇒ Drive API not enabled
    # in the GCP project that owns the OAuth client.
    drive_api_ok = False
    try:
        from googleapiclient.discovery import build as _gapi_build  # noqa: PLC0415

        _drive_service = _gapi_build(
            "drive", "v3",
            credentials=factory_result["creds"],
            cache_discovery=False,
        )
        _drive_service.about().get(fields="user").execute()
        drive_api_ok = True
    except BaseException as exc:  # noqa: BLE001
        if _maybe_auth_bucket(exc):
            # Auth-bucket already emitted; skip remaining checks.
            return failures
        klass = _classify(exc)
        text = str(exc)
        if klass == "api_disabled":
            console.print(
                f"  [red]✗[/] Drive API not enabled in GCP project "
                f"[bold]{gcp_project_id}[/]\n"
                f"      [dim]{text[:200]}[/]\n"
                f"      [yellow]Fix:[/] enable the Drive API at "
                f"[bold]https://console.cloud.google.com/apis/library/"
                f"drive.googleapis.com?project={gcp_project_id}[/]"
            )
            failures.append(
                f"Drive API not enabled in {gcp_project_id}"
            )
        elif klass == "transient":
            console.print(
                "  [red]✗[/] Drive API probe failed (transient): "
                f"[dim]{type(exc).__name__}: {text[:140]}[/]\n"
                "      [yellow]Fix:[/] retry; if persistent, your "
                "credentials may have lost the relevant scope — re-run "
                f"[bold]claude-mirror auth --config {path}[/]."
            )
            failures.append("Drive API probe transient failure")
        else:
            console.print(
                f"  [red]✗[/] Drive API probe failed "
                f"([dim]{type(exc).__name__}[/]): "
                f"[dim]{text[:160]}[/]\n"
                f"      [yellow]Fix:[/] enable the Drive API at "
                f"[bold]https://console.cloud.google.com/apis/library/"
                f"drive.googleapis.com?project={gcp_project_id}[/] "
                f"or re-run [bold]claude-mirror auth --config {path}[/]."
            )
            failures.append(
                f"Drive API probe failed: {type(exc).__name__}"
            )

    if drive_api_ok:
        console.print(
            f"  [green]✓[/] Drive API enabled in project "
            f"[dim]{gcp_project_id}[/]"
        )

    # ───── Check 3: Pub/Sub API enabled (probe via get_topic) ─────
    # We piggy-back on the topic-existence check below: if get_topic raises
    # the "API not enabled" string, that's check 3 failing; if it raises
    # NotFound, that's check 3 PASSING and check 4 (topic exists) failing;
    # if it succeeds, both 3 and 4 pass. One RPC, two signals.
    topic_get_exc: Optional[BaseException] = None
    topic_exists = False
    try:
        publisher.get_topic(request={"topic": topic_path})
        topic_exists = True
    except BaseException as exc:  # noqa: BLE001
        topic_get_exc = exc

    if topic_get_exc is None:
        # Topic exists ⇒ Pub/Sub API definitionally enabled.
        console.print("  [green]✓[/] Pub/Sub API enabled")
        console.print(
            f"  [green]✓[/] Pub/Sub topic exists: [dim]{topic_path}[/]"
        )
    else:
        if _maybe_auth_bucket(topic_get_exc):
            return failures
        klass = _classify(topic_get_exc)
        text = str(topic_get_exc)
        if klass == "api_disabled":
            console.print(
                "  [red]✗[/] Pub/Sub API not enabled in GCP project "
                f"[bold]{gcp_project_id}[/]\n"
                f"      [dim]{text[:200]}[/]\n"
                f"      [yellow]Fix:[/] enable the Pub/Sub API at "
                f"[bold]https://console.cloud.google.com/apis/library/"
                f"pubsub.googleapis.com?project={gcp_project_id}[/]"
            )
            failures.append(
                f"Pub/Sub API not enabled in {gcp_project_id}"
            )
            # Without Pub/Sub API, none of checks 4-6 can run.
            return failures
        # API is presumed enabled if we got here — emit the API ✓ now and
        # then the per-failure line for the topic check.
        console.print("  [green]✓[/] Pub/Sub API enabled")
        if klass == "not_found":
            console.print(
                f"  [red]✗[/] Pub/Sub topic does not exist: "
                f"[bold]{topic_path}[/]\n"
                f"      [yellow]Fix:[/] create the topic at "
                f"[bold]https://console.cloud.google.com/cloudpubsub/"
                f"topic/list?project={gcp_project_id}[/] (topic ID: "
                f"[bold]{pubsub_topic_id}[/]), or re-run "
                f"[bold]claude-mirror init --wizard --config {path}[/]."
            )
            failures.append(f"Pub/Sub topic missing: {topic_path}")
            # Without a topic, the subscription + IAM checks can't pass.
            return failures
        if klass == "permission":
            console.print(
                f"  [red]✗[/] Pub/Sub topic check denied (permission): "
                f"[dim]{text[:140]}[/]\n"
                f"      [yellow]Fix:[/] grant your account the "
                f"[bold]Pub/Sub Editor[/] role at "
                f"[bold]https://console.cloud.google.com/iam-admin/iam"
                f"?project={gcp_project_id}[/]."
            )
            failures.append("Pub/Sub topic check permission denied")
            return failures
        if klass == "transient":
            console.print(
                "  [red]✗[/] Pub/Sub topic probe failed (transient): "
                f"[dim]{type(topic_get_exc).__name__}: {text[:140]}[/]\n"
                "      [yellow]Fix:[/] retry; if persistent, your "
                "credentials may have lost the relevant scope — re-run "
                f"[bold]claude-mirror auth --config {path}[/]."
            )
            failures.append("Pub/Sub topic probe transient failure")
            return failures
        console.print(
            f"  [red]✗[/] Pub/Sub topic probe failed "
            f"([dim]{type(topic_get_exc).__name__}[/]): "
            f"[dim]{text[:160]}[/]\n"
            f"      [yellow]Fix:[/] inspect the error above and verify "
            f"the topic at "
            f"[bold]https://console.cloud.google.com/cloudpubsub/topic/"
            f"list?project={gcp_project_id}[/]."
        )
        failures.append(
            f"Pub/Sub topic probe failed: {type(topic_get_exc).__name__}"
        )
        return failures

    if not topic_exists:
        # Defensive: the branches above should all have returned by now.
        return failures

    # ───── Check 5: Per-machine subscription exists ─────
    try:
        from google.cloud import pubsub_v1 as _pubsub_v1  # noqa: PLC0415

        # Build a SubscriberClient lazily — we only need it for get_subscription.
        # Reuse the OAuth credentials from the factory.
        _subscriber = _pubsub_v1.SubscriberClient(
            credentials=factory_result["creds"]
        )
        try:
            _subscriber.get_subscription(
                request={"subscription": subscription_path}
            )
        finally:
            try:
                _subscriber.close()
            except Exception:
                pass
        console.print(
            f"  [green]✓[/] Pub/Sub subscription exists for this machine: "
            f"[dim]{subscription_path}[/]"
        )
    except BaseException as exc:  # noqa: BLE001
        if _maybe_auth_bucket(exc):
            return failures
        klass = _classify(exc)
        text = str(exc)
        if klass == "not_found":
            console.print(
                f"  [red]✗[/] Pub/Sub subscription does not exist for "
                f"this machine: [bold]{subscription_path}[/]\n"
                f"      [yellow]Fix:[/] run "
                f"[bold]claude-mirror auth --config {path}[/] — auth "
                f"creates the per-machine subscription if it's missing."
            )
            failures.append(
                f"Pub/Sub subscription missing: {subscription_path}"
            )
        elif klass == "transient":
            console.print(
                "  [red]✗[/] Pub/Sub subscription probe failed "
                f"(transient): [dim]{type(exc).__name__}: "
                f"{text[:140]}[/]\n"
                "      [yellow]Fix:[/] retry; if persistent, your "
                "credentials may have lost the relevant scope — re-run "
                f"[bold]claude-mirror auth --config {path}[/]."
            )
            failures.append(
                "Pub/Sub subscription probe transient failure"
            )
        else:
            console.print(
                f"  [red]✗[/] Pub/Sub subscription probe failed "
                f"([dim]{type(exc).__name__}[/]): "
                f"[dim]{text[:160]}[/]\n"
                f"      [yellow]Fix:[/] re-run "
                f"[bold]claude-mirror auth --config {path}[/] to "
                f"recreate the subscription."
            )
            failures.append(
                f"Pub/Sub subscription probe failed: {type(exc).__name__}"
            )

    # ───── Check 6: IAM grant — Drive's service account on the topic ─────
    # Read the topic's IAM policy and look for a binding that grants
    # `roles/pubsub.publisher` to a member matching Drive's service account
    # (`serviceAccount:apps-storage-noreply@google.com`). This is the
    # killer check: ~70% of Drive setups miss this and silently lose
    # real-time notifications.
    try:
        policy = publisher.get_iam_policy(
            request={"resource": topic_path}
        )
    except BaseException as exc:  # noqa: BLE001
        if _maybe_auth_bucket(exc):
            return failures
        klass = _classify(exc)
        text = str(exc)
        if klass == "permission":
            console.print(
                f"  [red]✗[/] Cannot read topic IAM policy "
                f"(permission denied): [dim]{text[:140]}[/]\n"
                f"      [yellow]Fix:[/] grant your account the "
                f"[bold]Pub/Sub Admin[/] role at "
                f"[bold]https://console.cloud.google.com/iam-admin/iam"
                f"?project={gcp_project_id}[/] (Pub/Sub Editor cannot "
                f"read IAM)."
            )
            failures.append("Topic IAM policy read permission denied")
        elif klass == "transient":
            console.print(
                "  [red]✗[/] Topic IAM policy read failed (transient): "
                f"[dim]{type(exc).__name__}: {text[:140]}[/]\n"
                "      [yellow]Fix:[/] retry; if persistent, your "
                "credentials may have lost the relevant scope — re-run "
                f"[bold]claude-mirror auth --config {path}[/]."
            )
            failures.append("Topic IAM policy read transient failure")
        else:
            console.print(
                f"  [red]✗[/] Topic IAM policy read failed "
                f"([dim]{type(exc).__name__}[/]): "
                f"[dim]{text[:160]}[/]\n"
                f"      [yellow]Fix:[/] inspect the error above; verify "
                f"the topic exists and your account has IAM read "
                f"permission."
            )
            failures.append(
                f"Topic IAM policy read failed: {type(exc).__name__}"
            )
        return failures

    # Search the policy for the required binding. The proto's `bindings`
    # field is repeated and each binding's `members` is repeated; match
    # is exact (no wildcards) — `serviceAccount:` prefix included.
    expected_member = f"serviceAccount:{_DRIVE_PUBSUB_PUBLISHER_SA}"
    has_publisher_grant = False
    for binding in getattr(policy, "bindings", []) or []:
        role = getattr(binding, "role", "")
        if role != "roles/pubsub.publisher":
            continue
        members = list(getattr(binding, "members", []) or [])
        if expected_member in members:
            has_publisher_grant = True
            break

    if has_publisher_grant:
        console.print(
            f"  [green]✓[/] Drive service account has publish permission "
            f"on the topic ([dim]{_DRIVE_PUBSUB_PUBLISHER_SA}[/])"
        )
    else:
        console.print(
            f"  [red]✗[/] Drive service account missing publish "
            f"permission on the topic\n"
            f"      [dim]Push events from THIS machine won't notify "
            f"others.[/]\n"
            f"      [yellow]Fix:[/] run "
            f"[bold]claude-mirror init --reconfigure-pubsub --config "
            f"{path}[/], or grant [bold]roles/pubsub.publisher[/] to "
            f"[bold]{expected_member}[/] on topic "
            f"[bold]{topic_path}[/] in the Cloud Console."
        )
        failures.append(
            "Drive service account missing roles/pubsub.publisher on topic"
        )

    return failures


# ─────────────────────────────────────────────────────────────────────────
# Dropbox deep checks (v0.5.48)
#
# Layered on top of the generic doctor pass for `--backend dropbox`. Mirrors
# the v0.5.46 googledrive deep-check pattern: lazy SDK import, classified
# ✓ / ✗ / ⚠ output, single ACTION REQUIRED bucket for grouped auth failures.
# ─────────────────────────────────────────────────────────────────────────

# Dropbox app keys are short alphanumeric tokens issued by the developer
# console. Empirically they are 15 chars but we accept 10-20 to absorb any
# future format drift. Lower-case + digits only — no hyphens, no underscores.
_DROPBOX_APP_KEY_RE = re.compile(r"^[a-z0-9]{10,20}$")

# OAuth scopes claude-mirror needs to read + write the configured folder.
# Token JSON from a PKCE flow carries these in a `scope` field (space- or
# comma-separated). Legacy tokens (pre-2020 long-lived access_token) have
# no scope field at all — we surface that as an info line.
_DROPBOX_REQUIRED_SCOPES = ("files.content.read", "files.content.write")


def _run_dropbox_deep_checks(path: str, config: "Config") -> list[str]:
    """Dropbox-specific deep diagnostic checks.

    Runs AFTER the generic credentials/token/connectivity checks have
    already reported. Adds Dropbox-specific assertions that the generic
    pass cannot make:

      1. Token JSON shape — `access_token` or `refresh_token` present.
      2. App-key sanity — non-empty + matches the short-alphanumeric format.
      3. Account smoke test — `users_get_current_account` returns an
         Account with a populated `account_id`.
      4. Granted scopes inspection — for PKCE tokens, verify the operations
         claude-mirror needs (file read/write) are present. Legacy tokens
         skip this with an info line.
      5. Folder access — `files_list_folder(dropbox_folder, limit=1)`
         catches NotFound / permission denied / team-folder restrictions.
      6. Account type / team status — info line about admin policies that
         may affect sync if the account is a team member.

    Returns the list of failure summary strings (empty ⇒ all-pass).
    Renders ✓ / ✗ / ⚠ lines to `console` as it goes, matching the
    surrounding doctor's output style.

    Auth failures are bucketed: if `users_get_current_account` returns an
    AuthError, ONE auth-bucket failure line is emitted and checks 4-6 are
    skipped, so the user sees a single "fix: re-run claude-mirror auth"
    rather than four identical lines for the same root cause.
    """
    import json as _json_local

    failures: list[str] = []

    # ───── Check 1: Token JSON shape ─────
    # The generic check 3 already verified the file exists + has a
    # refresh_token (the format claude-mirror writes); the deep check
    # confirms either `access_token` (long-lived legacy) or `refresh_token`
    # (PKCE) is present so the SDK has SOMETHING to authenticate with.
    token_path = Path(config.token_file)
    if not token_path.exists():
        # Generic check already reported this; emitting again would just
        # be noise. Bail silently.
        return failures

    token_data: dict = {}
    try:
        raw = token_path.read_text()
        parsed = _json_local.loads(raw)
        if isinstance(parsed, dict):
            token_data = parsed
    except (OSError, _json_local.JSONDecodeError) as exc:
        console.print(
            f"  [red]✗[/] Token file unreadable / not JSON: "
            f"[bold]{token_path}[/]\n"
            f"      [dim]{exc}[/]\n"
            f"      [yellow]Fix:[/] run "
            f"[bold]claude-mirror auth --config {path}[/] to refresh "
            f"the token."
        )
        failures.append(f"Dropbox token file corrupt: {token_path}")
        return failures

    has_access = bool(token_data.get("access_token"))
    has_refresh = bool(token_data.get("refresh_token"))
    if not has_access and not has_refresh:
        console.print(
            f"  [red]✗[/] Token JSON missing both [bold]access_token[/] "
            f"and [bold]refresh_token[/]: [bold]{token_path}[/]\n"
            f"      [yellow]Fix:[/] run "
            f"[bold]claude-mirror auth --config {path}[/] to refresh "
            f"the token."
        )
        failures.append("Dropbox token JSON missing access_token/refresh_token")
        return failures

    if has_refresh:
        console.print(
            f"  [green]✓[/] Token JSON valid; "
            f"[dim]refresh_token present[/]"
        )
    else:
        console.print(
            f"  [green]✓[/] Token JSON valid; "
            f"[dim]access_token present (legacy long-lived token)[/]"
        )

    # ───── Check 2: App-key sanity ─────
    # The app key is required to construct a `dropbox.Dropbox(...)`
    # client; an empty / malformed key would just produce an opaque SDK
    # error several lines later. Surface it cleanly here.
    app_key_yaml = (config.dropbox_app_key or "").strip()
    # Token JSON also carries an app_key fallback when DropboxBackend writes
    # it — fall back to the YAML value if the token didn't store one.
    app_key_token = str(token_data.get("app_key", "") or "").strip()
    app_key = app_key_yaml or app_key_token

    if not app_key:
        console.print(
            f"  [red]✗[/] [bold]dropbox_app_key[/] is empty in "
            f"[bold]{path}[/]\n"
            f"      [yellow]Fix:[/] add the App key from your Dropbox "
            f"app's [bold]Settings[/] tab "
            f"([bold]https://www.dropbox.com/developers/apps[/]) to the "
            f"YAML, then re-run "
            f"[bold]claude-mirror auth --config {path}[/]."
        )
        failures.append("Dropbox app key empty")
        return failures

    if not _DROPBOX_APP_KEY_RE.match(app_key):
        console.print(
            f"  [red]✗[/] [bold]dropbox_app_key[/] format invalid: "
            f"[dim]{app_key!r}[/]\n"
            f"      [dim]Expected 10-20 lower-case alphanumeric "
            f"characters (e.g. [bold]uao2pmhc0xgg2xj[/]).[/]\n"
            f"      [yellow]Fix:[/] copy the App key from your Dropbox "
            f"app's [bold]Settings[/] tab at "
            f"[bold]https://www.dropbox.com/developers/apps[/] and update "
            f"[bold]{path}[/]."
        )
        failures.append(f"Dropbox app key format invalid: {app_key}")
        return failures

    console.print(
        f"  [green]✓[/] App key format valid: [dim]{app_key}[/]"
    )

    # ───── Lazy-import the SDK ─────
    # Pays the multi-tens-of-millisecond import cost only on this branch.
    # Generic doctor invocations on other backends remain quick.
    try:
        import dropbox as _dropbox  # noqa: PLC0415
        from dropbox.exceptions import (  # noqa: PLC0415
            ApiError,
            AuthError,
            HttpError,
        )
    except ImportError as exc:
        console.print(
            f"  [red]✗[/] Dropbox SDK not importable: [dim]{exc}[/]\n"
            f"      [yellow]Fix:[/] reinstall claude-mirror with the "
            f"Dropbox backend pulled in — "
            f"[bold]pipx install --force claude-mirror[/]."
        )
        failures.append("Dropbox SDK not importable")
        return failures

    # Track whether we've already emitted the AUTH bucket so a cascading
    # auth failure across checks 3-5 surfaces ONCE not three times.
    auth_bucket_reported = False

    def _maybe_auth_bucket(exc: BaseException) -> bool:
        """If `exc` is AuthError-class, emit ONE bucket line (only the
        first time) and return True. Caller skips its own per-check
        message. Returns False otherwise."""
        nonlocal auth_bucket_reported
        is_auth = isinstance(exc, AuthError)
        # Some HttpErrors with status 401 ALSO mean "token revoked";
        # treat them the same so a 401 short-circuits the cascade too.
        if not is_auth and isinstance(exc, HttpError):
            try:
                status = int(getattr(exc, "status_code", 0) or 0)
                if status == 401:
                    is_auth = True
            except (TypeError, ValueError):
                pass
        if not is_auth:
            return False
        if not auth_bucket_reported:
            auth_bucket_reported = True
            console.print(
                "  [red]✗[/] Dropbox auth failed "
                f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/])\n"
                "      [yellow]Fix:[/] re-run "
                f"[bold]claude-mirror auth --config {path}[/] — "
                "remaining Dropbox checks skipped to avoid duplicate "
                "failures from the same root cause."
            )
            failures.append("Dropbox auth failed")
        return True

    # ───── Build the Dropbox client ─────
    # Prefer refresh_token (PKCE flow) so the SDK silently refreshes the
    # access_token on the first RPC; fall back to a bare access_token for
    # legacy tokens. Both shapes are tolerated by `dropbox.Dropbox`.
    try:
        if has_refresh:
            dbx = _dropbox.Dropbox(
                app_key=app_key,
                oauth2_refresh_token=token_data.get("refresh_token"),
            )
        else:
            dbx = _dropbox.Dropbox(
                oauth2_access_token=token_data.get("access_token"),
            )
    except BaseException as exc:  # noqa: BLE001 — diagnostic, must not bubble
        console.print(
            f"  [red]✗[/] Could not construct Dropbox client: "
            f"[dim]{type(exc).__name__}: {str(exc)[:160]}[/]\n"
            f"      [yellow]Fix:[/] re-run "
            f"[bold]claude-mirror auth --config {path}[/]."
        )
        failures.append(
            f"Dropbox client construction failed: {type(exc).__name__}"
        )
        return failures

    # ───── Check 3: Account smoke test ─────
    # First network call after auth — surfaces revoked tokens cleanly.
    account: Any = None
    try:
        account = dbx.users_get_current_account()
    except BaseException as exc:  # noqa: BLE001 — diagnostic, must not bubble
        if _maybe_auth_bucket(exc):
            return failures
        console.print(
            f"  [red]✗[/] Account smoke test failed "
            f"([dim]{type(exc).__name__}[/]): "
            f"[dim]{str(exc)[:160]}[/]\n"
            f"      [yellow]Fix:[/] check internet connectivity and "
            f"re-run [bold]claude-mirror auth --config {path}[/] if the "
            f"failure persists."
        )
        failures.append(
            f"Dropbox account smoke test failed: {type(exc).__name__}"
        )
        return failures

    account_id = getattr(account, "account_id", None) if account is not None else None
    account_email = getattr(account, "email", None) if account is not None else None
    if not account_id:
        console.print(
            "  [red]✗[/] Account response missing [bold]account_id[/] "
            f"(got [dim]{type(account).__name__}[/])\n"
            f"      [yellow]Fix:[/] re-run "
            f"[bold]claude-mirror auth --config {path}[/]; if the issue "
            f"persists, the Dropbox SDK may be too old — upgrade with "
            f"[bold]pipx install --force claude-mirror[/]."
        )
        failures.append("Dropbox account response missing account_id")
        return failures

    if account_email:
        console.print(
            f"  [green]✓[/] Account: [bold]{account_email}[/] "
            f"([dim]account_id: {account_id}[/])"
        )
    else:
        console.print(
            f"  [green]✓[/] Account verified "
            f"([dim]account_id: {account_id}[/])"
        )

    # ───── Check 4: Granted scopes inspection ─────
    # PKCE tokens carry a `scope` field (space- or comma-separated list);
    # legacy tokens have no scope field at all. For PKCE, verify the
    # operations claude-mirror needs are present; for legacy, emit an
    # info line and move on (legacy tokens implicitly grant everything
    # the app was approved for at the time of issuance).
    raw_scope = token_data.get("scope") or token_data.get("scopes") or ""
    if isinstance(raw_scope, list):
        scope_set = {str(s).strip() for s in raw_scope if str(s).strip()}
    elif isinstance(raw_scope, str) and raw_scope.strip():
        # Accept both space- and comma-separated; Dropbox returns space-
        # separated but we tolerate either to absorb format drift.
        normalised = raw_scope.replace(",", " ")
        scope_set = {s for s in normalised.split() if s}
    else:
        scope_set = set()

    if scope_set:
        missing_scopes = [
            s for s in _DROPBOX_REQUIRED_SCOPES if s not in scope_set
        ]
        if missing_scopes:
            console.print(
                f"  [red]✗[/] Token missing required scope(s): "
                f"[bold]{', '.join(missing_scopes)}[/]\n"
                f"      [dim]Granted: "
                f"{', '.join(sorted(scope_set)) or '(none)'}[/]\n"
                f"      [yellow]Fix:[/] enable the missing scope(s) on "
                f"your Dropbox app's [bold]Permissions[/] tab at "
                f"[bold]https://www.dropbox.com/developers/apps[/], "
                f"click [bold]Submit[/], then re-run "
                f"[bold]claude-mirror auth --config {path}[/]."
            )
            failures.append(
                f"Dropbox token missing scope(s): {', '.join(missing_scopes)}"
            )
        else:
            console.print(
                f"  [green]✓[/] Scopes: "
                f"[dim]{', '.join(_DROPBOX_REQUIRED_SCOPES)}[/]"
            )
    else:
        console.print(
            "  [yellow]·[/] Legacy token format; scope inspection "
            "skipped — re-auth via PKCE flow when convenient "
            f"([bold]claude-mirror auth --config {path}[/])."
        )

    # ───── Check 5: Folder access ─────
    # `files_list_folder(path=dropbox_folder, limit=1)` catches: folder
    # doesn't exist (LookupError.is_not_found), permission denied
    # (HttpError 403), team-folder access not granted, etc. Each maps
    # to a specific fix-hint.
    folder = (config.dropbox_folder or "").strip()
    if not folder:
        console.print(
            f"  [red]✗[/] [bold]dropbox_folder[/] is empty in "
            f"[bold]{path}[/]\n"
            f"      [yellow]Fix:[/] set [bold]dropbox_folder[/] to the "
            f"absolute path of your sync folder (e.g. "
            f"[bold]/claude-mirror/myproject[/]) in the YAML."
        )
        failures.append("Dropbox folder path empty")
    else:
        try:
            dbx.files_list_folder(path=folder, limit=1)
            console.print(
                f"  [green]✓[/] Folder accessible: [dim]{folder}[/]"
            )
        except BaseException as exc:  # noqa: BLE001 — diagnostic, must not bubble
            if _maybe_auth_bucket(exc):
                # Don't return — the team-status info line below is
                # still useful and doesn't make any RPC calls.
                pass
            else:
                # Inspect ApiError → ListFolderError → LookupError for
                # the specific failure mode. Wrap every getattr in a
                # try/except because the SDK's typed-union accessors raise
                # AttributeError when the wrong tag is set.
                fix_hint: str = ""
                summary: str = ""
                handled = False
                if isinstance(exc, ApiError):
                    err = getattr(exc, "error", None)
                    try:
                        if err is not None and hasattr(err, "is_path") and err.is_path():
                            path_err = err.get_path()
                            if hasattr(path_err, "is_not_found") and path_err.is_not_found():
                                fix_hint = (
                                    f"create [bold]{folder}[/] in your "
                                    f"Dropbox account (web UI or "
                                    f"Dropbox client) and re-run "
                                    f"[bold]claude-mirror doctor "
                                    f"--backend dropbox --config "
                                    f"{path}[/]."
                                )
                                summary = "folder not found"
                                console.print(
                                    f"  [red]✗[/] Folder not found in "
                                    f"Dropbox: [bold]{folder}[/]\n"
                                    f"      [yellow]Fix:[/] {fix_hint}"
                                )
                                failures.append(
                                    f"Dropbox folder not found: {folder}"
                                )
                                handled = True
                            elif (
                                hasattr(path_err, "is_no_write_permission")
                                and path_err.is_no_write_permission()
                            ):
                                console.print(
                                    f"  [red]✗[/] Access denied on "
                                    f"folder: [bold]{folder}[/]\n"
                                    f"      [yellow]Fix:[/] check the "
                                    f"folder is shared with the "
                                    f"authenticated account "
                                    f"([bold]{account_email or account_id}[/]) "
                                    f"and that the Dropbox app has the "
                                    f"[bold]files.content.write[/] "
                                    f"scope."
                                )
                                failures.append(
                                    f"Dropbox folder access denied: {folder}"
                                )
                                handled = True
                    except Exception:
                        # Defensive: typed-union introspection failed.
                        # Fall through to the generic ApiError handler.
                        pass

                    if not handled:
                        # Generic ApiError fallback — surfaces any
                        # path-error variant we don't model explicitly.
                        text = str(exc)
                        denied_hit = (
                            "access_denied" in text.lower()
                            or "forbidden" in text.lower()
                        )
                        if denied_hit:
                            console.print(
                                f"  [red]✗[/] Access denied on folder: "
                                f"[bold]{folder}[/]\n"
                                f"      [dim]{text[:160]}[/]\n"
                                f"      [yellow]Fix:[/] verify the "
                                f"folder is shared with "
                                f"[bold]{account_email or account_id}[/] "
                                f"and that the Dropbox app has the "
                                f"[bold]files.content.read[/] + "
                                f"[bold]files.content.write[/] scopes."
                            )
                            failures.append(
                                f"Dropbox folder access denied: {folder}"
                            )
                            handled = True

                if not handled:
                    # Final catch-all — HTTP / network / unknown.
                    text = str(exc)
                    if isinstance(exc, HttpError):
                        try:
                            status = int(
                                getattr(exc, "status_code", 0) or 0
                            )
                        except (TypeError, ValueError):
                            status = 0
                        if status == 403:
                            console.print(
                                f"  [red]✗[/] Folder access denied "
                                f"(HTTP 403): [bold]{folder}[/]\n"
                                f"      [yellow]Fix:[/] verify the "
                                f"folder is shared with "
                                f"[bold]{account_email or account_id}[/]."
                            )
                            failures.append(
                                f"Dropbox folder access denied: {folder}"
                            )
                            handled = True
                        elif status == 404:
                            console.print(
                                f"  [red]✗[/] Folder not found "
                                f"(HTTP 404): [bold]{folder}[/]\n"
                                f"      [yellow]Fix:[/] create "
                                f"[bold]{folder}[/] in Dropbox and "
                                f"re-run."
                            )
                            failures.append(
                                f"Dropbox folder not found: {folder}"
                            )
                            handled = True

                if not handled:
                    console.print(
                        f"  [red]✗[/] Folder probe failed "
                        f"([dim]{type(exc).__name__}[/]): "
                        f"[dim]{str(exc)[:160]}[/]\n"
                        f"      [yellow]Fix:[/] verify "
                        f"[bold]dropbox_folder[/] in [bold]{path}[/] is "
                        f"correct and that the folder is shared with "
                        f"the authenticated account."
                    )
                    failures.append(
                        f"Dropbox folder probe failed: {type(exc).__name__}"
                    )

    # ───── Check 6: Account type / team status ─────
    # Read account.account_type from check 3's result. Dropbox SDK's
    # AccountType union has is_basic / is_pro / is_business predicates.
    # Separately, FullAccount.team is non-None when the user is a member
    # of a Dropbox Business team — surface that as an info line because
    # team admins can disable third-party app access at the team level,
    # which would silently break sync.
    account_type_label = "unknown"
    try:
        atype = getattr(account, "account_type", None)
        if atype is not None:
            for tag, label in (
                ("is_basic", "personal"),
                ("is_pro", "pro"),
                ("is_business", "business"),
            ):
                pred = getattr(atype, tag, None)
                if callable(pred) and pred():
                    account_type_label = label
                    break
    except Exception:
        # Defensive: any introspection failure falls through to "unknown".
        pass

    is_team_member = getattr(account, "team", None) is not None
    if is_team_member:
        console.print(
            f"  [yellow]·[/] Account type: "
            f"[bold]{account_type_label}[/] (team member) — "
            f"[dim]team admins may disable third-party app access; "
            f"if sync stops working unexpectedly, ask your Dropbox "
            f"admin to confirm claude-mirror is permitted.[/]"
        )
    else:
        console.print(
            f"  [green]✓[/] Account type: [dim]{account_type_label}[/]"
        )

    return failures

_ONEDRIVE_GRAPH_BASE = "https://graph.microsoft.com/v1.0"

_ONEDRIVE_REQUIRED_SCOPES = ("Files.ReadWrite", "Files.ReadWrite.All")

_AZURE_CLIENT_ID_RE = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

def _onedrive_deep_check_factory(
    config: "Config", token_path: "Path"
) -> dict:
    """Build the MSAL PublicClientApplication + cached account used by
    the deep OneDrive doctor checks.

    Lazily imports the MSAL SDK so the cost is only paid when
    `claude-mirror doctor` is actually inspecting a OneDrive backend.

    Returns a dict with keys:
      app           — msal.PublicClientApplication instance (or None on
                      construction failure — e.g. malformed client_id)
      app_error     — exception or None; non-None means MSAL refused to
                      construct the app (typically an invalid client_id)
      account       — the first cached account, or None if cache is empty
      cache_error   — exception or None; non-None means the token cache
                      file failed to read / deserialize
      cached_count  — number of cached accounts (informational)

    Tests monkeypatch this whole function to inject a stub app / account,
    which is why it's a module-level seam rather than inline.
    """
    import json as _json_local  # noqa: PLC0415 — local alias
    import re as _re_local  # noqa: PLC0415 — only used in the OneDrive deep path

    app = None
    app_error: Optional[BaseException] = None
    account = None
    cache_error: Optional[BaseException] = None
    cached_count = 0

    client_id = (config.onedrive_client_id or "").strip()

    # Validate client_id format BEFORE constructing the MSAL app — MSAL
    # will accept a non-GUID string and only fail later on token-acquire,
    # at which point the error message is far less actionable than
    # "invalid GUID format".
    if not _re_local.match(_AZURE_CLIENT_ID_RE, client_id):
        app_error = ValueError(
            f"onedrive_client_id has invalid format: {client_id!r}"
        )
        return {
            "app": None,
            "app_error": app_error,
            "account": None,
            "cache_error": None,
            "cached_count": 0,
        }

    # Lazy-import MSAL — pays the cost only on the OneDrive deep path.
    try:
        import msal as _msal  # noqa: PLC0415
    except ImportError as e:
        return {
            "app": None,
            "app_error": e,
            "account": None,
            "cache_error": None,
            "cached_count": 0,
        }

    # Read + deserialize the token cache. We do this BEFORE constructing
    # the MSAL app so a corrupt cache file surfaces as a separate
    # diagnostic from a malformed client_id.
    cache = _msal.SerializableTokenCache()
    try:
        token_data = _json_local.loads(token_path.read_text())
        if isinstance(token_data, dict):
            cache_blob = token_data.get("token_cache", "{}")
            cache.deserialize(cache_blob)
        else:
            cache_error = ValueError(
                "token file does not contain a JSON object"
            )
    except (OSError, _json_local.JSONDecodeError) as e:
        cache_error = e
    except BaseException as e:  # noqa: BLE001 — diagnostic must not bubble
        cache_error = e

    # Construct the MSAL PublicClientApplication. Wrapped because a
    # malformed client_id (rare, since we regex-validated above) or any
    # other constructor failure should surface as `app_error`, not
    # crash doctor.
    try:
        app = _msal.PublicClientApplication(
            client_id,
            authority="https://login.microsoftonline.com/consumers",
            token_cache=cache,
        )
    except BaseException as e:  # noqa: BLE001
        app_error = e
        return {
            "app": None,
            "app_error": app_error,
            "account": None,
            "cache_error": cache_error,
            "cached_count": 0,
        }

    # Inspect cached accounts. Empty cache ⇒ user has never authenticated
    # successfully on this machine; first cached account is the one our
    # `acquire_token_silent` call will use.
    try:
        accounts = app.get_accounts() or []
        cached_count = len(accounts)
        if accounts:
            account = accounts[0]
    except BaseException as e:  # noqa: BLE001
        cache_error = cache_error or e

    return {
        "app": app,
        "app_error": app_error,
        "account": account,
        "cache_error": cache_error,
        "cached_count": cached_count,
    }

def _run_onedrive_deep_checks(path: str, config: "Config") -> list[str]:
    """OneDrive-specific deep diagnostic checks.

    Runs AFTER the generic credentials/token/connectivity checks have
    already reported. Adds OneDrive-specific assertions that the generic
    pass cannot make:

      1. Token cache integrity — read the MSAL token cache, deserialize,
         confirm at least one cached account.
      2. Azure client_id format valid — Application (client) ID is a
         GUID; surface malformed values before MSAL spits a cryptic
         error from deeper down the stack.
      3. Granted scopes match config — cached account scopes include
         `Files.ReadWrite` (or `Files.ReadWrite.All` for shared business
         tenants). Missing scopes ⇒ info line "scopes missing: re-run
         auth".
      4. Token still refreshable — `acquire_token_silent` against the
         cached account. None / `error` in the result ⇒ AUTH bucket fail.
      5. Drive item access — Microsoft Graph GET against
         `me/drive/root:/{onedrive_folder}`. 200 ⇒ folder exists. 404 ⇒
         "create the folder, or push to create it on first sync". 401 ⇒
         AUTH bucket fail. 5xx ⇒ TRANSIENT classification.
      6. Drive item type — confirm Graph returned a drive item / folder
         shape; quickXorHash detection happens at sync time per-file
         (not verifiable here without listing the whole folder).

    Returns the list of failure summary strings (empty ⇒ all-pass).
    Renders ✓ / ✗ / ⚠ lines to `console` as it goes, matching the
    surrounding doctor's output style.

    Auth failures are bucketed: if the saved token is unusable, ONE
    AUTH-class failure line is emitted and remaining checks are skipped,
    so the user sees a single "fix: re-run claude-mirror auth" rather
    than three identical lines for the same root cause.
    """
    failures: list[str] = []

    # Skip the entire deep section gracefully when the user hasn't
    # configured the OneDrive folder. Without it, the drive-item probe
    # can't run and the rest of the section is meaningless.
    onedrive_folder = (config.onedrive_folder or "").strip()
    if not onedrive_folder:
        console.print(
            "  [yellow]⚠[/] OneDrive folder not configured "
            "([dim]onedrive_folder empty[/]) — skipping deep "
            "OneDrive checks. "
            "[yellow]Fix:[/] run "
            f"[bold]claude-mirror init --wizard --config {path}[/] "
            "to add OneDrive settings."
        )
        return failures

    token_path = Path(config.token_file)
    if not token_path.exists():
        # The generic check above already emitted a failure for the
        # missing token file; don't repeat.
        return failures

    # ───── Build the MSAL app + cached account via the test seam ─────
    factory_result = _onedrive_deep_check_factory(config, token_path)
    app = factory_result["app"]
    app_error = factory_result["app_error"]
    account = factory_result["account"]
    cache_error = factory_result["cache_error"]
    cached_count = int(factory_result["cached_count"])

    # ───── Check 2: Azure client_id format valid ─────
    # Run this BEFORE the cache check because a malformed client_id
    # short-circuits everything else (no point inspecting the cache when
    # the cache will never be usable with a bad client_id).
    if app_error is not None and isinstance(app_error, ValueError) and (
        "invalid format" in str(app_error)
    ):
        client_id_disp = (config.onedrive_client_id or "").strip()
        console.print(
            f"  [red]✗[/] Azure client_id has invalid format: "
            f"[bold]{client_id_disp!r}[/]\n"
            f"      [yellow]Fix:[/] edit [bold]{path}[/] and set "
            f"[bold]onedrive_client_id[/] to your Azure App "
            f"registration's Application (client) ID (GUID format: "
            f"[dim]xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx[/])."
        )
        failures.append(
            f"Azure client_id has invalid format: {client_id_disp!r}"
        )
        return failures

    # ───── Check 1: Token cache integrity ─────
    if cache_error is not None:
        console.print(
            f"  [red]✗[/] Token cache unreadable: "
            f"[dim]{type(cache_error).__name__}: "
            f"{str(cache_error)[:140]}[/]\n"
            f"      [yellow]Fix:[/] run "
            f"[bold]claude-mirror auth --config {path}[/] to "
            f"re-authenticate."
        )
        failures.append(f"OneDrive token cache unreadable: {token_path}")
        return failures

    if app is None:
        # Catch-all for any non-format MSAL construction failure that
        # made it past the regex validator above.
        err_text = str(app_error) if app_error is not None else "unknown"
        console.print(
            f"  [red]✗[/] MSAL PublicClientApplication construction "
            f"failed: [dim]{err_text[:160]}[/]\n"
            f"      [yellow]Fix:[/] verify [bold]onedrive_client_id[/] "
            f"in [bold]{path}[/] and re-run "
            f"[bold]claude-mirror auth --config {path}[/]."
        )
        failures.append("MSAL app construction failed")
        return failures

    if cached_count == 0 or account is None:
        console.print(
            "  [red]✗[/] Token cache has no cached accounts "
            f"([dim]{token_path}[/])\n"
            "      [yellow]Fix:[/] run "
            f"[bold]claude-mirror auth --config {path}[/] to "
            "complete the device-code login."
        )
        failures.append("OneDrive token cache has no cached accounts")
        return failures

    console.print(
        f"  [green]✓[/] Token cache valid; "
        f"[dim]{cached_count} cached account"
        f"{'s' if cached_count != 1 else ''}[/]"
    )

    # ───── Check 2 (positive): Azure client_id format valid ─────
    console.print("  [green]✓[/] Azure client_id format valid")

    # ───── Check 3: Granted scopes include configured ones ─────
    # MSAL's get_accounts() returns Account objects whose serialized form
    # holds a 'scopes' field if available. Fall back to introspecting
    # the cache directly when the account dict doesn't surface scopes.
    granted_scopes: list[str] = []
    try:
        if isinstance(account, dict):
            raw_scopes = account.get("scopes") or account.get("scope") or ""
            if isinstance(raw_scopes, str):
                granted_scopes = raw_scopes.split()
            elif isinstance(raw_scopes, list):
                granted_scopes = [str(s) for s in raw_scopes]
        # MSAL also stores per-token scopes in the cache's AccessToken
        # entries; if the per-account dict didn't have them, look there.
        if not granted_scopes:
            try:
                cache_obj = getattr(app, "token_cache", None)
                if cache_obj is not None and hasattr(cache_obj, "find"):
                    # CredentialType.ACCESS_TOKEN == "AccessToken" string.
                    found = cache_obj.find("AccessToken") or []
                    for entry in found:
                        target = entry.get("target") if isinstance(entry, dict) else None
                        if target:
                            granted_scopes = (
                                target.split() if isinstance(target, str)
                                else [str(s) for s in target]
                            )
                            if granted_scopes:
                                break
            except Exception:
                pass
    except Exception:
        granted_scopes = []

    has_required_scope = any(
        req in granted_scopes for req in _ONEDRIVE_REQUIRED_SCOPES
    )
    if has_required_scope:
        # Pick the first matching scope for display.
        match = next(
            (req for req in _ONEDRIVE_REQUIRED_SCOPES if req in granted_scopes),
            "Files.ReadWrite",
        )
        console.print(f"  [green]✓[/] Scopes: [dim]{match}[/]")
    elif granted_scopes:
        # Cache had scopes but none of ours — degraded but not fatal;
        # acquire_token_silent below will tell us definitively.
        console.print(
            f"  [yellow]⚠[/] Scopes missing from cache: expected one of "
            f"[bold]{', '.join(_ONEDRIVE_REQUIRED_SCOPES)}[/], "
            f"saw [dim]{', '.join(granted_scopes) or '(none)'}[/]. "
            f"Re-run [bold]claude-mirror auth --config {path}[/] "
            "to grant the scope."
        )
    else:
        # No scopes surfaced from the cache — could be a legit-but-old
        # cache shape; log info rather than fail and let the silent-token
        # call settle it.
        console.print(
            "  [dim]·[/] Scopes: cache shape doesn't expose granted "
            "scopes — silent-token call below will verify."
        )

    # ───── Check 4: Token still refreshable ─────
    # acquire_token_silent against the cached account. None or a result
    # dict carrying an 'error' key ⇒ refresh failed; user must re-auth.
    auth_bucket_reported = False

    def _emit_auth_bucket(reason: str) -> None:
        """Emit ONE bucketed AUTH-class failure line. Subsequent calls
        with `auth_bucket_reported=True` from the caller short-circuit."""
        nonlocal auth_bucket_reported
        if auth_bucket_reported:
            return
        auth_bucket_reported = True
        console.print(
            f"  [red]✗[/] OneDrive auth failed: [dim]{reason[:200]}[/]\n"
            f"      [yellow]Fix:[/] re-run "
            f"[bold]claude-mirror auth --config {path}[/] — "
            "remaining OneDrive checks skipped to avoid duplicate "
            "failures from the same root cause."
        )
        failures.append("OneDrive auth failed (token refresh)")

    access_token: Optional[str] = None
    try:
        # Use the broadest scope so a token granted with Files.ReadWrite.All
        # still satisfies a Files.ReadWrite request.
        scopes_for_silent = ["Files.ReadWrite"]
        result = app.acquire_token_silent(scopes_for_silent, account=account)
    except BaseException as exc:  # noqa: BLE001
        _emit_auth_bucket(f"{type(exc).__name__}: {exc}")
        return failures

    if result is None:
        _emit_auth_bucket(
            "acquire_token_silent returned None — refresh token expired "
            "or revoked"
        )
        return failures

    if isinstance(result, dict) and result.get("error"):
        err_code = result.get("error", "")
        err_desc = result.get("error_description", "")
        _emit_auth_bucket(f"{err_code}: {err_desc}")
        return failures

    if isinstance(result, dict):
        access_token = result.get("access_token")

    if not access_token:
        _emit_auth_bucket(
            "acquire_token_silent returned no access_token"
        )
        return failures

    console.print(
        "  [green]✓[/] Token refreshable; "
        "[dim]access_token acquired[/]"
    )

    # ───── Check 5: Drive item access ─────
    # GET https://graph.microsoft.com/v1.0/me/drive/root:/{folder}
    # Returns a DriveItem on 200, with the folder's metadata. 404 means
    # the folder doesn't exist (yet). 401 means the access_token we just
    # obtained is somehow not valid — likely a tenant / scope mismatch.
    import requests as _requests  # noqa: PLC0415 — already a top-level dep
    folder_for_url = onedrive_folder.lstrip("/")
    drive_item_url = (
        f"{_ONEDRIVE_GRAPH_BASE}/me/drive/root:/{folder_for_url}"
    )
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = _requests.get(drive_item_url, headers=headers, timeout=10)
    except _requests.exceptions.RequestException as exc:
        console.print(
            f"  [red]✗[/] Drive item access failed (network): "
            f"[dim]{type(exc).__name__}: {str(exc)[:140]}[/]\n"
            f"      [yellow]Fix:[/] check internet connectivity (and any "
            f"corporate proxy / VPN settings) and retry."
        )
        failures.append("OneDrive drive item access network failure")
        return failures

    status = resp.status_code
    if status == 200:
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        console.print(
            f"  [green]✓[/] OneDrive folder accessible: "
            f"[dim]/{folder_for_url}[/]"
        )

        # ───── Check 6: drive item shape ─────
        # quickXorHash detection happens at sync time (per-file). All we
        # can verify here is that Graph returned a drive item / folder
        # shape — the per-file hash is in `file.hashes.quickXorHash` and
        # only appears on individual files, not on folder metadata.
        is_folder = isinstance(payload, dict) and "folder" in payload
        is_file = isinstance(payload, dict) and "file" in payload
        if is_folder:
            console.print("  [green]✓[/] Drive item type: folder")
        elif is_file:
            console.print(
                "  [yellow]⚠[/] Drive item type: file ([dim]onedrive_folder "
                "points at a file, not a folder — sync will fail[/])"
            )
            failures.append(
                f"onedrive_folder points at a file, not a folder: "
                f"/{folder_for_url}"
            )
        else:
            # Unknown shape — Graph normally returns one of the two; treat
            # as a soft warning so the user knows the response was odd.
            console.print(
                "  [yellow]⚠[/] Drive item type: unknown ([dim]Graph "
                "returned a payload without `folder` or `file` "
                "keys; quickXorHash detection runs at sync time per-file[/])"
            )
        return failures

    if status == 401:
        _emit_auth_bucket(
            f"Microsoft Graph returned HTTP 401 for "
            f"{drive_item_url}"
        )
        return failures

    if status == 404:
        console.print(
            f"  [red]✗[/] Drive item access: HTTP 404\n"
            f"      OneDrive folder doesn't exist at the configured "
            f"path: [bold]/{folder_for_url}[/]\n"
            f"      [yellow]Fix:[/] create [bold]/{folder_for_url}[/] in "
            f"the OneDrive web UI, or run "
            f"[bold]claude-mirror push --config {path}[/] which will "
            f"create the folder on first sync."
        )
        failures.append(
            f"OneDrive folder does not exist: /{folder_for_url}"
        )
        return failures

    if status == 403:
        console.print(
            f"  [red]✗[/] Drive item access: HTTP 403 "
            f"([dim]forbidden[/])\n"
            f"      [yellow]Fix:[/] your account lacks permission for "
            f"[bold]/{folder_for_url}[/]. Check folder sharing in the "
            f"OneDrive web UI or re-run "
            f"[bold]claude-mirror auth --config {path}[/] with an "
            f"account that has access."
        )
        failures.append(
            f"OneDrive drive item access forbidden: /{folder_for_url}"
        )
        return failures

    if 500 <= status < 600:
        console.print(
            f"  [red]✗[/] Drive item access: HTTP {status} "
            f"([dim]Microsoft Graph transient[/])\n"
            f"      [yellow]Fix:[/] retry; if persistent, check "
            f"[bold]https://status.office.com[/] for service incidents."
        )
        failures.append(
            f"OneDrive drive item access transient HTTP {status}"
        )
        return failures

    # Catch-all for any other 4xx/3xx/etc. status.
    console.print(
        f"  [red]✗[/] Drive item access: HTTP {status} "
        f"([dim]unexpected[/])\n"
        f"      [yellow]Fix:[/] inspect the error above; verify "
        f"[bold]onedrive_folder[/] and [bold]onedrive_client_id[/] in "
        f"[bold]{path}[/]."
    )
    failures.append(
        f"OneDrive drive item access unexpected HTTP {status}"
    )
    return failures

def _run_webdav_deep_checks(path: str, config: "Config") -> list[str]:
    """WebDAV-specific deep diagnostic checks.

    Runs AFTER the generic credentials/token/connectivity checks have
    already reported. Adds WebDAV-specific assertions that the generic
    pass cannot make:

      1. Configured URL is well-formed (https:// + netloc + path).
      2. PROPFIND on the configured root returns 207 Multi-Status —
         the explicit "the configured WebDAV path actually exists and
         the credentials work" smoke-test that goes beyond list_folders.
      3. DAV class detection — parse the `DAV:` response header to
         report the server's RFC 4918 compliance level (claude-mirror
         needs class 1+ minimum).
      4. ETag header presence on the configured root — without ETags,
         change-detection falls back to last-modified or content-md5.
      5. oc:checksums extension support detection — Nextcloud / OwnCloud
         expose this XML namespace in PROPFIND responses with MD5 / SHA1
         / SHA256 hashes that claude-mirror prefers for primary-backend
         parity.
      6. Account-level smoke test — for Nextcloud / OwnCloud URLs,
         PROPFIND `/remote.php/dav/files/{user}/` to confirm the account
         itself is reachable separately from the project sub-folder.

    Returns the list of failure summary strings (empty ⇒ all-pass).
    Renders ✓ / ✗ / ⚠ lines to `console` as it goes, matching the
    surrounding doctor's output style.

    Auth failures are bucketed: a single ACTION REQUIRED auth-bucket
    line is emitted on the first 401 and remaining checks are skipped
    so the user sees one "fix your credentials" line rather than five
    cascading copies of the same root cause.

    Uses `requests` directly (already a dependency via the WebDAV
    backend) rather than instantiating a WebDAVBackend instance — the
    deep checks need lower-level header/status visibility than the
    backend's high-level `list_folders` exposes.
    """
    import requests as _requests  # noqa: PLC0415 — keep top-of-module clean
    from urllib.parse import urlparse  # noqa: PLC0415

    failures: list[str] = []

    # ───── Check 1: URL well-formed ─────
    # Reject empty, unparseable, or http:// (unless explicitly opted in).
    url = (config.webdav_url or "").strip()
    if not url:
        console.print(
            "  [red]✗[/] WebDAV URL is empty in config\n"
            "      [yellow]Fix:[/] set [bold]webdav_url[/] in "
            f"[bold]{path}[/], or run "
            f"[bold]claude-mirror init --wizard --config {path}[/]."
        )
        failures.append(f"WebDAV URL empty: {path}")
        return failures

    parsed = urlparse(url)
    insecure_http_ok = bool(getattr(config, "webdav_insecure_http", False))
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        console.print(
            f"  [red]✗[/] WebDAV URL malformed: [bold]{url}[/]\n"
            "      [yellow]Fix:[/] expected "
            "[bold]https://host/path[/]; edit "
            f"[bold]{path}[/] or re-run "
            f"[bold]claude-mirror init --wizard --config {path}[/]."
        )
        failures.append(f"WebDAV URL malformed: {url}")
        return failures
    if parsed.scheme == "http" and not insecure_http_ok:
        # The backend constructor itself rejects this, but the deep
        # check should still surface it explicitly so the failure
        # message is actionable rather than a cryptic ValueError.
        console.print(
            f"  [red]✗[/] WebDAV URL uses http:// (cleartext): "
            f"[bold]{url}[/]\n"
            f"      [yellow]Fix:[/] switch to https:// or set "
            f"[bold]webdav_insecure_http: true[/] in [bold]{path}[/] "
            f"(NOT recommended — basic-auth credentials cross the wire "
            f"in cleartext)."
        )
        failures.append(f"WebDAV URL uses cleartext http: {url}")
        return failures
    console.print(
        f"  [green]✓[/] URL well-formed: [dim]{url}[/]"
    )

    # If credentials are missing the connectivity check above already
    # emitted a failure for that — skip the deep section silently
    # rather than firing another redundant 401-class line.
    username = (config.webdav_username or "").strip()
    password = config.webdav_password or ""
    # The token file may carry the password if the user ran `auth`; fall
    # back to it so the deep checks work post-auth even when the YAML
    # only stores the username.
    if not password:
        try:
            token_path = Path(config.token_file)
            if token_path.exists():
                token_data = _json.loads(token_path.read_text())
                if isinstance(token_data, dict):
                    username = username or str(
                        token_data.get("username", "") or ""
                    )
                    password = str(token_data.get("password", "") or "")
        except (OSError, _json.JSONDecodeError):
            # Generic check 3 already surfaced credential problems.
            pass
    if not username or not password:
        # Generic check 3 already flagged this; bail without a duplicate
        # complaint and without making a real network call we can't auth.
        return failures

    auth = _requests.auth.HTTPBasicAuth(username, password)

    # Track AUTH-bucket emission so a 401 cascade across multiple
    # PROPFIND / HEAD calls surfaces ONCE not five times — same shape
    # as the Drive deep checks.
    auth_bucket_reported = False

    def _maybe_auth_bucket(status: int, where: str) -> bool:
        """If `status` is 401, emit ONE auth-bucket line (only the
        first time) and return True. Caller must skip its own per-check
        message and bail."""
        nonlocal auth_bucket_reported
        if status != 401:
            return False
        if not auth_bucket_reported:
            auth_bucket_reported = True
            console.print(
                f"  [red]✗[/] {where} failed: HTTP 401\n"
                "      Credentials rejected. Verify "
                "[bold]webdav_username[/] and [bold]webdav_password[/].\n"
                f"      [yellow]Fix:[/] run "
                f"[bold]claude-mirror auth --config {path}[/]"
            )
            failures.append(f"WebDAV auth failed (HTTP 401) at {where}")
        return True

    # ───── Check 2: PROPFIND on configured root (depth=0) ─────
    # The exact body the WebDAV backend uses internally — keep them in
    # sync so the deep check reproduces the real-world request shape.
    propfind_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        "<d:prop>"
        "<d:resourcetype/>"
        "<d:getcontentlength/>"
        "<d:getetag/>"
        "<d:getlastmodified/>"
        "<d:getcontenttype/>"
        "<oc:checksums/>"
        "</d:prop>"
        "</d:propfind>"
    )
    propfind_resp = None
    try:
        propfind_resp = _requests.request(
            "PROPFIND", url,
            auth=auth,
            headers={
                "Depth": "0",
                "Content-Type": "application/xml; charset=utf-8",
            },
            data=propfind_body.encode("utf-8"),
            timeout=15,
        )
    except _requests.exceptions.RequestException as exc:
        console.print(
            "  [red]✗[/] PROPFIND probe failed (network): "
            f"[dim]{type(exc).__name__}: {str(exc)[:160]}[/]\n"
            "      [yellow]Fix:[/] verify the server is reachable; "
            "retry once network is healthy."
        )
        failures.append(
            f"WebDAV PROPFIND network failure: {type(exc).__name__}"
        )
        return failures

    status = propfind_resp.status_code
    if _maybe_auth_bucket(status, "PROPFIND"):
        return failures
    if status == 207:
        console.print(
            f"  [green]✓[/] PROPFIND succeeded; HTTP 207"
        )
    elif status == 404:
        console.print(
            f"  [red]✗[/] PROPFIND failed: HTTP 404\n"
            f"      Configured WebDAV root doesn't exist: [bold]{url}[/]\n"
            f"      [yellow]Fix:[/] create the folder on the server, "
            f"or correct [bold]webdav_url[/] in [bold]{path}[/]."
        )
        failures.append(f"WebDAV root does not exist (404): {url}")
        return failures
    elif status == 405:
        console.print(
            f"  [red]✗[/] PROPFIND failed: HTTP 405\n"
            f"      Server doesn't support PROPFIND on this URL "
            f"([dim]{url}[/]).\n"
            f"      [yellow]Fix:[/] verify the URL points at a WebDAV "
            f"endpoint (not a plain HTTP folder). For Nextcloud / "
            f"OwnCloud, the URL must include "
            f"[bold]/remote.php/dav/files/USER/[/]."
        )
        failures.append(f"WebDAV server does not support PROPFIND: {url}")
        return failures
    elif 500 <= status < 600:
        console.print(
            f"  [red]✗[/] PROPFIND failed: HTTP {status} (transient)\n"
            f"      [yellow]Fix:[/] server-side error; retry. If it "
            f"persists, check the server's error log."
        )
        failures.append(f"WebDAV PROPFIND transient (HTTP {status})")
        return failures
    else:
        console.print(
            f"  [red]✗[/] PROPFIND failed: HTTP {status}\n"
            f"      [yellow]Fix:[/] inspect the server response and "
            f"verify [bold]webdav_url[/] in [bold]{path}[/]."
        )
        failures.append(f"WebDAV PROPFIND unexpected HTTP {status}")
        return failures

    # ───── Check 3: DAV class detection ─────
    # The DAV: header lists the RFC 4918 compliance levels, comma-
    # separated, e.g. `1, 2, 3` (Nextcloud), `1, 3` (OwnCloud), `1, 2`
    # (Apache mod_dav). Class 1 is the bare minimum for claude-mirror;
    # class 2 (locking) is unused but informative; class 3 (range PUT)
    # would let us optimize partial uploads in future.
    dav_header = propfind_resp.headers.get("DAV", "") or ""
    if not dav_header:
        console.print(
            "  [yellow]⚠[/] no DAV class header reported by server "
            "([dim]missing `DAV:` response header[/]); some WebDAV "
            "features may be unavailable but basic operations should "
            "still work."
        )
    else:
        # Normalize: strip whitespace around each class token.
        classes = [c.strip() for c in dav_header.split(",") if c.strip()]
        # claude-mirror only requires "1" to be present.
        if "1" in classes:
            console.print(
                f"  [green]✓[/] DAV class: [dim]{', '.join(classes)}[/]"
            )
        else:
            console.print(
                f"  [yellow]⚠[/] DAV header reported [dim]{dav_header}[/] "
                "but does NOT list class 1 — the server may not be a "
                "compliant WebDAV implementation; expect issues."
            )

    # ───── Check 4: ETag header presence on the root resource ─────
    # Two sources to check: the response's `ETag:` header AND the
    # PROPFIND XML's `<d:getetag/>` field. Either present is enough.
    etag_header = propfind_resp.headers.get("ETag", "") or ""
    has_etag_xml = False
    propfind_xml = None
    try:
        import xml.etree.ElementTree as _ET  # noqa: PLC0415
        propfind_xml = _ET.fromstring(propfind_resp.content)
        for getetag_elem in propfind_xml.iter("{DAV:}getetag"):
            if getetag_elem.text and getetag_elem.text.strip():
                has_etag_xml = True
                break
    except _ET.ParseError:
        # Server returned 207 but the body isn't valid XML — odd but
        # not fatal for the overall deep check.
        propfind_xml = None

    if etag_header or has_etag_xml:
        console.print(
            "  [green]✓[/] ETag header present"
        )
    else:
        console.print(
            "  [yellow]⚠[/] no ETag returned; claude-mirror will fall "
            "back to last-modified / content-md5 for change detection "
            "(slower but still correct)."
        )

    # ───── Check 5: oc:checksums extension support detection ─────
    # Nextcloud / OwnCloud emit `<oc:checksums>SHA1:abc MD5:def</oc:checksums>`
    # in PROPFIND responses when the server has the
    # files_checksums-style extension active. claude-mirror prefers
    # these over ETags for parity with primary backends. Their absence
    # is informational only — non-Nextcloud / non-OwnCloud servers
    # never expose this namespace.
    has_oc_checksums = False
    checksum_kinds: list[str] = []
    if propfind_xml is not None:
        for cks_elem in propfind_xml.iter(
            "{http://owncloud.org/ns}checksums"
        ):
            if cks_elem.text and cks_elem.text.strip():
                has_oc_checksums = True
                # Surface the kinds in the info line so the user knows
                # what their server advertises.
                for token in cks_elem.text.split():
                    kind = token.split(":", 1)[0].strip().upper()
                    if kind and kind not in checksum_kinds:
                        checksum_kinds.append(kind)
                break
        # Some servers include the element but with empty text — fall
        # back to namespace-only detection.
        if not has_oc_checksums:
            for _ in propfind_xml.iter(
                "{http://owncloud.org/ns}checksums"
            ):
                has_oc_checksums = True
                break
    if has_oc_checksums:
        if checksum_kinds:
            console.print(
                "  [green]✓[/] oc:checksums extension supported "
                f"([dim]{', '.join(checksum_kinds)}[/])"
            )
        else:
            console.print(
                "  [green]✓[/] oc:checksums extension supported"
            )
    else:
        console.print(
            "  [dim]·[/] oc:checksums extension not advertised "
            "([dim]Nextcloud / OwnCloud only[/]) — falling back to "
            "ETag for change detection (still correct)."
        )

    # ───── Check 6: account-level smoke test ─────
    # For Nextcloud / OwnCloud URLs of the form
    # `https://host/remote.php/dav/files/USERNAME/...`, PROPFIND the
    # account-level `/remote.php/dav/files/USERNAME/` to confirm the
    # account itself is reachable separately from the project folder.
    # Skipped silently for non-Nextcloud-pattern URLs.
    import re as _re  # noqa: PLC0415
    nc_match = _re.match(
        r"^(https?://[^/]+/remote\.php/dav/files/[^/]+/)",
        url,
    )
    if nc_match:
        account_url = nc_match.group(1)
        if account_url.rstrip("/") == url.rstrip("/"):
            # The configured root IS the account base — Check 2 already
            # exercised it; emitting a duplicate ✓ would be noise.
            pass
        else:
            try:
                acct_resp = _requests.request(
                    "PROPFIND", account_url,
                    auth=auth,
                    headers={
                        "Depth": "0",
                        "Content-Type": "application/xml; charset=utf-8",
                    },
                    data=propfind_body.encode("utf-8"),
                    timeout=15,
                )
            except _requests.exceptions.RequestException as exc:
                console.print(
                    "  [red]✗[/] Account-level PROPFIND failed "
                    f"(network): [dim]{type(exc).__name__}: "
                    f"{str(exc)[:140]}[/]\n"
                    "      [yellow]Fix:[/] verify the server is "
                    "reachable; retry once network is healthy."
                )
                failures.append(
                    "WebDAV account-level PROPFIND network failure"
                )
                return failures
            acct_status = acct_resp.status_code
            if _maybe_auth_bucket(acct_status, "Account-level PROPFIND"):
                return failures
            if acct_status == 207:
                console.print(
                    f"  [green]✓[/] Account-level PROPFIND succeeded: "
                    f"[dim]{account_url}[/]"
                )
            elif acct_status == 404:
                console.print(
                    f"  [red]✗[/] Account-level PROPFIND failed: HTTP "
                    f"404\n"
                    f"      Account base unreachable: "
                    f"[bold]{account_url}[/]\n"
                    f"      [yellow]Fix:[/] verify the username "
                    f"segment in [bold]webdav_url[/] of [bold]{path}[/]."
                )
                failures.append(
                    f"WebDAV account base 404: {account_url}"
                )
                return failures
            else:
                console.print(
                    f"  [yellow]⚠[/] Account-level PROPFIND returned "
                    f"HTTP {acct_status} ([dim]{account_url}[/]); the "
                    "project folder is reachable but the account base "
                    "isn't — server may have non-standard ACLs."
                )

    return failures

def _sftp_deep_check_factory(
    config: "Config",
) -> dict:
    """Build the live SSH key (post-handshake) used by the SFTP deep
    checks, plus the resolved key-file path on disk.

    Returns a dict with keys:
      live_host_key   — paramiko.PKey (the server's key from a real
                        Transport handshake), or None if the connection
                        couldn't be established yet.
      transport_error — exception or None; non-None means the host
                        wasn't reachable / TCP failed / SSH banner
                        timed out (i.e. transient).
      key_path        — resolved absolute path to sftp_key_file
                        (~ expanded), or "" if not configured.
      transport       — the open paramiko.Transport, or None. Caller
                        owns closing it.

    Tests monkeypatch this whole function to inject stubs. Keeping it as
    a module-level seam mirrors the Drive deep-check pattern and avoids
    having to mock half a dozen paramiko classes inside the test body.
    """
    import paramiko as _paramiko  # noqa: PLC0415 — lazy
    import socket as _socket  # noqa: PLC0415

    key_path = ""
    raw_key = (getattr(config, "sftp_key_file", "") or "").strip()
    if raw_key:
        key_path = str(Path(raw_key).expanduser())

    host = getattr(config, "sftp_host", "") or ""
    port = int(getattr(config, "sftp_port", 22) or 22)

    transport: Optional[Any] = None
    live_host_key: Optional[Any] = None
    transport_error: Optional[BaseException] = None
    try:
        # Open a raw TCP socket to the host:port with a short timeout, then
        # wrap it in a paramiko.Transport so we can pull the host key out
        # of the handshake WITHOUT authenticating yet. This separation lets
        # the fingerprint check fail BEFORE we send a key/password — which
        # is exactly what you want when the host has been swapped under you.
        sock = _socket.create_connection((host, port), timeout=5)
        transport = _paramiko.Transport(sock)
        transport.start_client(timeout=5)
        live_host_key = transport.get_remote_server_key()
    except BaseException as e:  # noqa: BLE001 — diagnostic must not bubble
        transport_error = e
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass
            transport = None

    return {
        "live_host_key": live_host_key,
        "transport_error": transport_error,
        "key_path": key_path,
        "transport": transport,
    }

def _run_sftp_deep_checks(path: str, config: "Config") -> list[str]:
    """SFTP-specific deep diagnostic checks.

    Runs AFTER the generic credentials/connectivity checks above. Adds
    SSH-specific assertions the generic loop can't make:

      1. Host fingerprint matches `~/.ssh/known_hosts`.
      2. SSH key file exists + readable.
      3. SSH key file permissions are 0600.
      4. SSH key can decrypt (or ssh-agent will handle).
      5. Connect + authenticate.
      6. `exec_command` capability.
      7. Root path access.

    Returns the list of failure summary strings (empty ⇒ all-pass).
    Renders ✓ / ✗ / ⚠ lines to `console` as it goes, matching the
    surrounding doctor's output style.

    Auth failures bucket: a single AUTH-class fail (host fingerprint
    mismatch, auth rejected, root-path permission denied) emits ONE
    auth-bucket line and short-circuits the rest of the chain — the
    user doesn't need five copies of "your access is broken".
    """
    failures: list[str] = []

    console.print("[bold]SFTP deep checks[/]")

    # Lazy-import paramiko + its exception module so generic doctor
    # invocations on other backends don't pay the import cost.
    import paramiko as _paramiko  # noqa: PLC0415

    sftp_host = (getattr(config, "sftp_host", "") or "").strip()
    sftp_port = int(getattr(config, "sftp_port", 22) or 22)
    sftp_username = (getattr(config, "sftp_username", "") or "").strip()
    sftp_folder = (getattr(config, "sftp_folder", "") or "").strip()
    sftp_password = getattr(config, "sftp_password", "") or None
    raw_key = (getattr(config, "sftp_key_file", "") or "").strip()
    kh_raw = (
        getattr(config, "sftp_known_hosts_file", "") or "~/.ssh/known_hosts"
    )
    kh_path = str(Path(kh_raw).expanduser())
    key_path = str(Path(raw_key).expanduser()) if raw_key else ""

    # ───── Auth-bucket plumbing (mirrors Drive deep-check pattern) ─────
    auth_bucket_reported = False

    def _emit_auth_bucket(headline: str, fix_hint: str, summary: str) -> None:
        """Emit ONE auth-bucket failure line; subsequent auth-class
        failures go silent so the user sees ONE root cause."""
        nonlocal auth_bucket_reported
        if auth_bucket_reported:
            return
        auth_bucket_reported = True
        console.print(
            f"  [red]✗[/] {headline}\n"
            f"      [yellow]Fix:[/] {fix_hint}"
        )
        failures.append(summary)

    # ───── Check 1: host fingerprint matches known_hosts ─────
    # Load known_hosts (if it exists) and look up the configured host.
    # If the host is absent → INFO line ("first connection will prompt
    # to verify"). If it's present, open a Transport without authenticating
    # and compare the live key's fingerprint to the stored entry. A
    # mismatch is a SECURITY INCIDENT — bucket it as AUTH and stop.
    stored_key = None
    kh_present = os.path.exists(kh_path)
    if kh_present:
        try:
            host_keys = _paramiko.HostKeys(filename=kh_path)
        except (IOError, OSError) as e:
            console.print(
                f"  [yellow]⚠[/] known_hosts file unreadable: "
                f"[bold]{kh_path}[/] ([dim]{e}[/]) — fingerprint check "
                f"skipped, first connection will prompt to verify."
            )
            host_keys = None
        if host_keys is not None:
            # paramiko's HostKeys.lookup understands "[host]:port" for non-22
            # ports; for the standard port we just look up the bare host.
            lookup_target = (
                f"[{sftp_host}]:{sftp_port}"
                if sftp_port != 22
                else sftp_host
            )
            entry = host_keys.lookup(lookup_target)
            if entry is None and sftp_port != 22:
                # Fall back to bare host — some ssh clients write the bare
                # form even for non-standard ports.
                entry = host_keys.lookup(sftp_host)
            if entry is not None:
                # `entry` is a dict {keytype: PKey}; any key in there is
                # a valid stored fingerprint.
                stored_keys = list(entry.values())
                if stored_keys:
                    stored_key = stored_keys[0]

    factory_result = _sftp_deep_check_factory(config)
    live_key = factory_result.get("live_host_key")
    transport_error = factory_result.get("transport_error")
    transport = factory_result.get("transport")

    try:
        if stored_key is None:
            if not kh_present:
                console.print(
                    f"  [yellow]⚠[/] known_hosts file missing: "
                    f"[bold]{kh_path}[/] — first connection will prompt "
                    f"to verify the host fingerprint."
                )
            else:
                console.print(
                    f"  [yellow]⚠[/] host [bold]{sftp_host}[/] not in "
                    f"[dim]{kh_path}[/]; first connection will prompt to "
                    f"verify the host fingerprint."
                )
        else:
            # Need a live key to compare. If the Transport handshake
            # itself failed, we can't run check 1 — surface a transient
            # error here and let check 5 emit a real failure.
            if live_key is None:
                exc = transport_error
                exc_name = type(exc).__name__ if exc is not None else "unknown"
                exc_text = str(exc) if exc is not None else "?"
                console.print(
                    f"  [yellow]⚠[/] could not fetch live host key from "
                    f"[bold]{sftp_host}:{sftp_port}[/] ([dim]{exc_name}: "
                    f"{exc_text[:120]}[/]) — fingerprint compare skipped; "
                    f"see connection check below."
                )
            else:
                stored_fp = getattr(stored_key, "fingerprint", None) or "?"
                live_fp = getattr(live_key, "fingerprint", None) or "?"
                if stored_fp == live_fp:
                    console.print(
                        f"  [green]✓[/] Host in known_hosts; fingerprint "
                        f"matches ([dim]{stored_fp}[/])"
                    )
                else:
                    # POSSIBLE MITM. Strong warning, dedicated fix hint
                    # (NOT `claude-mirror auth` — fingerprint mismatches
                    # are not a token problem, they're a security incident).
                    console.print(
                        f"  [red]✗[/] Host fingerprint mismatch in "
                        f"[bold]{kh_path}[/]\n"
                        f"           Stored fingerprint: [dim]{stored_fp}[/]\n"
                        f"           Live fingerprint:   [dim]{live_fp}[/]\n"
                        f"           [red bold]POSSIBLE MAN-IN-THE-MIDDLE — "
                        f"refusing to connect.[/]\n"
                        f"      [yellow]Fix:[/] investigate the mismatch. "
                        f"If the host genuinely changed, run "
                        f"[bold]ssh-keygen -R {sftp_host}[/] and re-add the "
                        f"host (verify the new fingerprint out-of-band first)."
                    )
                    failures.append(
                        f"SFTP host fingerprint mismatch: {sftp_host}"
                    )
                    auth_bucket_reported = True
                    # Stop here — refusing to connect is exactly the right
                    # response to a fingerprint mismatch. Don't try to
                    # auth or stat anything against a host we don't trust.
                    return failures

        # ───── Check 2: SSH key file exists + readable ─────
        if key_path:
            if not os.path.exists(key_path):
                console.print(
                    f"  [red]✗[/] SSH key file not found: "
                    f"[bold]{key_path}[/]\n"
                    f"      [yellow]Fix:[/] verify [bold]sftp_key_file[/] in "
                    f"[bold]{path}[/] points at an existing private key, "
                    f"or generate one with "
                    f"[bold]ssh-keygen -t ed25519[/]."
                )
                failures.append(f"SFTP key file not found: {key_path}")
                # Without a key file we can't run checks 3 and 4 — but
                # we still want to attempt connect+auth (paramiko may
                # have agent / default keys that work), so don't return.
            elif not os.access(key_path, os.R_OK):
                console.print(
                    f"  [red]✗[/] SSH key file not readable: "
                    f"[bold]{key_path}[/]\n"
                    f"      [yellow]Fix:[/] [bold]chmod 600 {key_path}[/] "
                    f"and ensure the current user owns it."
                )
                failures.append(
                    f"SFTP key file not readable: {key_path}"
                )
            else:
                console.print(
                    f"  [green]✓[/] Key file readable: [dim]{key_path}[/]"
                )

                # ───── Check 3: SSH key file permissions are 0600 ─────
                # OpenSSH refuses keys with any group/world bits set. We
                # use `st_mode & 0o077` to detect them — non-zero means
                # somebody other than the owner can read or write the key.
                # NOTE: we do NOT auto-fix; chmod 600 is one command and
                # the human needs to run it consciously.
                try:
                    perm_bits = os.stat(key_path).st_mode & 0o777
                except OSError as e:
                    console.print(
                        f"  [yellow]⚠[/] could not stat key file "
                        f"([dim]{e}[/]) — permission check skipped."
                    )
                else:
                    if perm_bits & 0o077:
                        console.print(
                            f"  [red]✗[/] Key file permissions too open: "
                            f"[bold]{oct(perm_bits)[2:]:>04}[/] on "
                            f"[bold]{key_path}[/]\n"
                            f"      [dim]OpenSSH refuses keys readable by "
                            f"group or world.[/]\n"
                            f"      [yellow]Fix:[/] [bold]chmod 600 "
                            f"{key_path}[/]"
                        )
                        failures.append(
                            f"SFTP key file permissions too open: "
                            f"{oct(perm_bits)[2:]:>04} on {key_path}"
                        )
                    else:
                        console.print(
                            f"  [green]✓[/] Key file permissions: "
                            f"[dim]{oct(perm_bits)[2:]:>04}[/]"
                        )

                # ───── Check 4: SSH key can decrypt ─────
                # paramiko 4.x exposes `PKey.from_private_key_file` (auto-
                # detects key type). On encrypted keys without a passphrase
                # it raises PasswordRequiredException — that's an INFO
                # line, not a failure: ssh-agent or claude-mirror's auth
                # flow handles the passphrase at sync time.
                #
                # paramiko has been known to raise unexpected exception
                # types on malformed/binary garbage masquerading as a key
                # (TypeError, ValueError, generic SSHException, etc.); a
                # broad catch here keeps the deep check from crashing on
                # a single bad file and lets the connect+auth check below
                # surface the real error.
                try:
                    _paramiko.PKey.from_private_key_file(key_path)
                    console.print(
                        "  [green]✓[/] Key decryptable "
                        "(or ssh-agent will handle)"
                    )
                except _paramiko.PasswordRequiredException:
                    console.print(
                        "  [yellow]⚠[/] Key is encrypted; ssh-agent or "
                        "claude-mirror's auth flow handles this at sync time."
                    )
                except _paramiko.SSHException as e:
                    # Malformed key, unsupported format, etc. Surface as
                    # a failure — paramiko's runtime will hit the same
                    # error during sync.
                    console.print(
                        f"  [red]✗[/] Key file unparseable: "
                        f"[bold]{key_path}[/] ([dim]{e}[/])\n"
                        f"      [yellow]Fix:[/] regenerate the key with "
                        f"[bold]ssh-keygen -t ed25519 -f {key_path}[/] or "
                        f"point [bold]sftp_key_file[/] at a valid key."
                    )
                    failures.append(
                        f"SFTP key file unparseable: {key_path}"
                    )
                except OSError:
                    # Already covered by the readable check above —
                    # silently ignore here.
                    pass
                except Exception as e:  # noqa: BLE001 — defensive
                    # Unexpected paramiko error shape (e.g. TypeError on
                    # binary garbage); report as unparseable and keep going.
                    console.print(
                        f"  [red]✗[/] Key file unparseable: "
                        f"[bold]{key_path}[/] "
                        f"([dim]{type(e).__name__}: {str(e)[:120]}[/])\n"
                        f"      [yellow]Fix:[/] regenerate the key with "
                        f"[bold]ssh-keygen -t ed25519 -f {key_path}[/] or "
                        f"point [bold]sftp_key_file[/] at a valid key."
                    )
                    failures.append(
                        f"SFTP key file unparseable: {key_path}"
                    )

        # ───── Check 5: connect + authenticate ─────
        # We already have an unauthenticated Transport from the factory
        # (used for fingerprint check 1). Authenticate it here. Time-
        # bounded by the 5s socket timeout we set when opening it.
        if transport is None:
            # The factory couldn't even open a Transport — classify the
            # underlying error and emit an appropriate line.
            exc = transport_error
            exc_name = type(exc).__name__ if exc is not None else "unknown"
            exc_text = str(exc) if exc is not None else "?"
            text_lower = exc_text.lower()
            if (
                isinstance(exc, (TimeoutError,))
                or "timeout" in text_lower
                or "timed out" in text_lower
            ):
                console.print(
                    f"  [red]✗[/] Connection to "
                    f"[bold]{sftp_host}:{sftp_port}[/] timed out\n"
                    f"      [yellow]Fix:[/] check that the server is "
                    f"reachable ([bold]ping {sftp_host}[/]) and that port "
                    f"[bold]{sftp_port}[/] is open from this machine."
                )
                failures.append(
                    f"SFTP connection timeout: {sftp_host}:{sftp_port}"
                )
            elif (
                isinstance(exc, ConnectionRefusedError)
                or "refused" in text_lower
                or "unreachable" in text_lower
            ):
                console.print(
                    f"  [red]✗[/] Server unreachable: "
                    f"[bold]{sftp_host}:{sftp_port}[/] "
                    f"([dim]{exc_name}: {exc_text[:120]}[/])\n"
                    f"      [yellow]Fix:[/] verify the server is up and "
                    f"port [bold]{sftp_port}[/] is open."
                )
                failures.append(
                    f"SFTP server unreachable: {sftp_host}:{sftp_port}"
                )
            else:
                console.print(
                    f"  [red]✗[/] Could not open SSH transport to "
                    f"[bold]{sftp_host}:{sftp_port}[/] "
                    f"([dim]{exc_name}: {exc_text[:120]}[/])\n"
                    f"      [yellow]Fix:[/] inspect the error above and "
                    f"verify the host/port in [bold]{path}[/]."
                )
                failures.append(
                    f"SFTP transport open failed: {exc_name}"
                )
            return failures

        # Transport is open — authenticate. Prefer the configured key
        # file; fall back to password if set; finally let paramiko try
        # ssh-agent / default keys via the auth_none/agent path.
        auth_ok = False
        try:
            if key_path and os.path.exists(key_path) and os.access(key_path, os.R_OK):
                try:
                    pkey = _paramiko.PKey.from_private_key_file(key_path)
                except _paramiko.PasswordRequiredException:
                    # Encrypted key without passphrase — paramiko's
                    # transport.auth_publickey would raise, so try
                    # ssh-agent / password instead. INFO already printed
                    # in check 4; just fall through to other auth methods.
                    pkey = None
                except Exception:
                    pkey = None

                if pkey is not None:
                    transport.auth_publickey(sftp_username, pkey)
                    auth_ok = transport.is_authenticated()
            if not auth_ok and sftp_password:
                transport.auth_password(sftp_username, sftp_password)
                auth_ok = transport.is_authenticated()
            if not auth_ok:
                # No usable creds — paramiko's high-level SSHClient would
                # try ssh-agent here. We surface this as an auth failure
                # since the deep check is supposed to verify configured
                # creds work end-to-end.
                raise _paramiko.AuthenticationException(
                    "no usable credentials (no key, no password, "
                    "agent-only auth not exercised here)"
                )
            console.print(
                "  [green]✓[/] Connection + auth succeeded"
            )
        except _paramiko.AuthenticationException as exc:
            _emit_auth_bucket(
                headline=(
                    f"SSH authentication rejected by [bold]{sftp_host}[/] "
                    f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/])"
                ),
                fix_hint=(
                    f"verify [bold]sftp_username[/] / "
                    f"[bold]sftp_key_file[/] / [bold]sftp_password[/] in "
                    f"[bold]{path}[/]. If using a key, confirm the public "
                    f"key is in the server's "
                    f"[bold]~/.ssh/authorized_keys[/]."
                ),
                summary=f"SFTP auth rejected: {sftp_host}",
            )
            return failures
        except _paramiko.BadHostKeyException as exc:
            # Paramiko's own host-key check fired — mirrors check 1's
            # detection but at the auth layer. Same security semantics.
            _emit_auth_bucket(
                headline=(
                    f"Host key changed for [bold]{sftp_host}[/] "
                    f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/]). "
                    f"[red bold]POSSIBLE MAN-IN-THE-MIDDLE.[/]"
                ),
                fix_hint=(
                    f"investigate the mismatch. If the host genuinely "
                    f"changed, run [bold]ssh-keygen -R {sftp_host}[/] and "
                    f"re-add the host."
                ),
                summary=f"SFTP host key changed: {sftp_host}",
            )
            return failures
        except (_paramiko.SSHException, OSError, EOFError) as exc:
            console.print(
                f"  [red]✗[/] SSH transport error during auth "
                f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/])\n"
                f"      [yellow]Fix:[/] retry; if the failure persists, "
                f"check the server's SSH service and the network path."
            )
            failures.append(
                f"SFTP transport error during auth: {type(exc).__name__}"
            )
            return failures

        # ───── Check 6: exec_command capability ─────
        # internal-sftp-jailed accounts disallow shell commands; we fall
        # back to client-side hashing in that case. INFO line either
        # branch — neither is a failure, just a perf signal.
        try:
            session = transport.open_session()
            try:
                session.settimeout(5)
                session.exec_command(
                    "echo claude-mirror-doctor-probe"
                )
                # Drain any response so the channel closes cleanly.
                try:
                    session.recv(64)
                except Exception:
                    pass
                # `recv_exit_status` blocks until the server sends the
                # close; capped by the 5s settimeout above.
                exit_status = session.recv_exit_status()
            finally:
                try:
                    session.close()
                except Exception:
                    pass
            if exit_status == 0:
                console.print(
                    "  [green]✓[/] exec_command available; server-side "
                    "hashing will be used"
                )
            else:
                console.print(
                    f"  [yellow]⚠[/] exec_command returned exit "
                    f"[bold]{exit_status}[/] — client-side hashing "
                    f"fallback active"
                )
        except Exception as exc:  # noqa: BLE001 — diagnostic only
            console.print(
                f"  [yellow]⚠[/] exec_command unavailable "
                f"([dim]{type(exc).__name__}: {str(exc)[:120]}[/]) — "
                f"client-side hashing fallback active"
            )

        # ───── Check 7: root path access ─────
        # Open an SFTP channel and stat the configured folder. NotFound is
        # an INFO line (claude-mirror creates it on first push); permission
        # denied is an AUTH-bucket failure.
        try:
            sftp_chan = _paramiko.SFTPClient.from_transport(transport)
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"  [red]✗[/] Could not open SFTP channel "
                f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/])\n"
                f"      [yellow]Fix:[/] confirm the server allows SFTP "
                f"subsystem access for user [bold]{sftp_username}[/]."
            )
            failures.append(
                f"SFTP channel open failed: {type(exc).__name__}"
            )
            return failures

        try:
            sftp_chan.stat(sftp_folder)
            console.print(
                f"  [green]✓[/] Root path: [dim]{sftp_folder}[/]"
            )
        except IOError as exc:
            code = getattr(exc, "errno", None)
            msg = str(exc).lower()
            if code == 2 or "no such" in msg or "not found" in msg:
                console.print(
                    f"  [yellow]⚠[/] Configured root doesn't exist: "
                    f"[bold]{sftp_folder}[/] — claude-mirror creates it "
                    f"on first push."
                )
            elif code == 13 or "permission" in msg or "denied" in msg:
                _emit_auth_bucket(
                    headline=(
                        f"Permission denied stat'ing root path "
                        f"[bold]{sftp_folder}[/]"
                    ),
                    fix_hint=(
                        f"user [bold]{sftp_username}[/] lacks access to "
                        f"[bold]{sftp_folder}[/]. Adjust server-side ACLs "
                        f"or change [bold]sftp_folder[/] in [bold]{path}[/]."
                    ),
                    summary=(
                        f"SFTP root path permission denied: {sftp_folder}"
                    ),
                )
            else:
                console.print(
                    f"  [red]✗[/] Could not stat root path "
                    f"[bold]{sftp_folder}[/] "
                    f"([dim]{type(exc).__name__}: {str(exc)[:140]}[/])\n"
                    f"      [yellow]Fix:[/] inspect the error above and "
                    f"verify [bold]sftp_folder[/] in [bold]{path}[/]."
                )
                failures.append(
                    f"SFTP root path stat failed: {type(exc).__name__}"
                )
        finally:
            try:
                sftp_chan.close()
            except Exception:
                pass
    finally:
        # Always close the Transport — it owns the underlying socket.
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    return failures



def _run_doctor_checks(cfg_path: str, backend_filter: str) -> list[str]:
    """Run the doctor check sequence for one config + its mirrors.

    Returns the list of failure summaries (empty list ⇒ everything passed).
    Renders each check's icon/result/fix-hint to `console` as it runs so
    the user sees live progress rather than a single end-of-run dump.

    Splitting this out from the CLI command itself keeps the wiring small
    and means the test suite can poke individual scenarios without
    having to mock click's progress / sys.exit machinery.
    """
    import json as _json

    failures: list[str] = []

    # ───── Check 1: primary config exists and parses ─────
    # If this fails, every later check is meaningless — bail with a single
    # actionable hint pointing at the wizard.
    try:
        primary_config = Config.load(cfg_path)
    except FileNotFoundError:
        console.print(
            f"  [red]✗[/] config file not found: [bold]{cfg_path}[/]\n"
            f"      [yellow]Fix:[/] run "
            f"[bold]claude-mirror init --wizard --config {cfg_path}[/] "
            f"to create a config."
        )
        failures.append(f"config file not found: {cfg_path}")
        return failures
    except Exception as e:
        console.print(
            f"  [red]✗[/] config file does not parse: [bold]{cfg_path}[/]\n"
            f"      [dim]{e}[/]\n"
            f"      [yellow]Fix:[/] run "
            f"[bold]claude-mirror init --wizard --config {cfg_path}[/] "
            f"to create a fresh config, or fix the YAML by hand."
        )
        failures.append(f"config file does not parse: {cfg_path}")
        return failures
    console.print(f"  [green]✓[/] config file parses: [bold]{cfg_path}[/]")

    # Build the list of (config_path, config) pairs to check. For Tier 2
    # multi-backend setups, the primary's `mirror_config_paths` references
    # additional configs — each gets the SAME check sequence applied.
    backends_to_check: list[tuple[str, Config]] = [(cfg_path, primary_config)]
    for mirror_path in primary_config.mirror_config_paths:
        try:
            mirror_resolved = (
                mirror_path
                if Path(mirror_path).is_absolute()
                else _resolve_config(mirror_path)
            )
            mirror_cfg = Config.load(mirror_resolved)
            backends_to_check.append((mirror_resolved, mirror_cfg))
        except Exception as e:
            console.print(
                f"  [red]✗[/] mirror config does not load: "
                f"[bold]{mirror_path}[/]\n"
                f"      [dim]{e}[/]\n"
                f"      [yellow]Fix:[/] verify the path in "
                f"`mirror_config_paths` of [bold]{cfg_path}[/], or "
                f"remove the entry."
            )
            failures.append(f"mirror config does not load: {mirror_path}")

    # ───── Per-backend checks ─────
    for path, config in backends_to_check:
        # Filter: skip backends not matching --backend NAME (case-insensitive).
        if backend_filter and (config.backend or "").lower() != backend_filter.lower():
            console.print(
                f"\n[dim]── skipped: {config.backend} "
                f"({path}) — does not match --backend {backend_filter}[/]"
            )
            continue

        console.print(
            f"\n[bold]── checking {config.backend} backend "
            f"({path})[/]"
        )

        # ───── Check 2: credentials file exists ─────
        # Required for googledrive / dropbox / onedrive (OAuth client JSON).
        # WebDAV doesn't use a credentials file — the WebDAV username +
        # password live in the YAML — so we skip this check there.
        # SFTP also stores host/user/key/password inline in the YAML.
        backend_name = (config.backend or "").lower()
        if backend_name == "webdav":
            console.print(
                "  [dim]·[/] credentials file: skipped (WebDAV uses inline "
                "username/password)"
            )
        elif backend_name == "sftp":
            console.print(
                "  [dim]·[/] credentials file: skipped (SFTP uses inline "
                "host/user/key in YAML)"
            )
        else:
            creds_path = Path(config.credentials_file)
            if not creds_path.exists():
                console.print(
                    f"  [red]✗[/] credentials file missing: "
                    f"[bold]{creds_path}[/]\n"
                    f"      [yellow]Fix:[/] re-download credentials.json "
                    f"from your cloud provider's developer console and "
                    f"place at [bold]{creds_path}[/]."
                )
                failures.append(f"credentials file missing: {creds_path}")
            else:
                console.print(
                    f"  [green]✓[/] credentials file exists: "
                    f"[dim]{creds_path}[/]"
                )

        # ───── Check 3: token file exists, parses, has refresh credentials ─────
        # WebDAV stores its credentials in the YAML rather than a token
        # file, so the test there is "username AND password are non-empty
        # in the config".
        if backend_name == "webdav":
            if not config.webdav_username or not config.webdav_password:
                console.print(
                    f"  [red]✗[/] WebDAV credentials missing in config: "
                    f"[bold]{path}[/]\n"
                    f"      [yellow]Fix:[/] run "
                    f"[bold]claude-mirror auth --config {path}[/] "
                    f"to authenticate."
                )
                failures.append(f"WebDAV credentials missing: {path}")
            else:
                console.print(
                    "  [green]✓[/] WebDAV credentials present in config "
                    "(username + password)"
                )
        elif backend_name == "sftp":
            # SFTP requires host + username + folder, plus AT LEAST ONE
            # auth material (key file path or password). All five fields
            # live in the YAML — there is no separate token file.
            sftp_host_v = getattr(config, "sftp_host", "") or ""
            sftp_user_v = getattr(config, "sftp_username", "") or ""
            sftp_folder_v = getattr(config, "sftp_folder", "") or ""
            sftp_key_v = getattr(config, "sftp_key_file", "") or ""
            sftp_pw_v = getattr(config, "sftp_password", "") or ""
            sftp_missing = []
            if not sftp_host_v:
                sftp_missing.append("sftp_host")
            if not sftp_user_v:
                sftp_missing.append("sftp_username")
            if not sftp_folder_v:
                sftp_missing.append("sftp_folder")
            if not sftp_key_v and not sftp_pw_v:
                sftp_missing.append("sftp_key_file or sftp_password")
            if sftp_missing:
                console.print(
                    f"  [red]✗[/] SFTP config incomplete: "
                    f"missing [bold]{', '.join(sftp_missing)}[/] in "
                    f"[bold]{path}[/]\n"
                    f"      [yellow]Fix:[/] run "
                    f"[bold]claude-mirror init --wizard --config {path}[/] "
                    f"or edit the YAML to add the missing fields."
                )
                failures.append(
                    f"SFTP config incomplete ({', '.join(sftp_missing)}): {path}"
                )
            else:
                console.print(
                    "  [green]✓[/] SFTP credentials present in config "
                    "(host + username + folder + key/password)"
                )
        else:
            token_path = Path(config.token_file)
            if not token_path.exists():
                console.print(
                    f"  [red]✗[/] token file missing: [bold]{token_path}[/]\n"
                    f"      [yellow]Fix:[/] run "
                    f"[bold]claude-mirror auth --config {path}[/] "
                    f"to authenticate."
                )
                failures.append(f"token file missing: {token_path}")
            else:
                # Parse and check for a refresh-capable credential.
                try:
                    token_data = _json.loads(token_path.read_text())
                except (OSError, _json.JSONDecodeError) as e:
                    console.print(
                        f"  [red]✗[/] token file unreadable / corrupt: "
                        f"[bold]{token_path}[/]\n"
                        f"      [dim]{e}[/]\n"
                        f"      [yellow]Fix:[/] run "
                        f"[bold]claude-mirror auth --config {path}[/] "
                        f"to re-authenticate."
                    )
                    failures.append(f"token file corrupt: {token_path}")
                else:
                    has_refresh = bool(
                        isinstance(token_data, dict)
                        and token_data.get("refresh_token")
                    )
                    if not has_refresh:
                        console.print(
                            f"  [red]✗[/] token file has no refresh_token: "
                            f"[bold]{token_path}[/]\n"
                            f"      [yellow]Fix:[/] run "
                            f"[bold]claude-mirror auth --config {path}[/] "
                            f"(consent screen must be shown to issue a new "
                            f"refresh_token)."
                        )
                        failures.append(f"token has no refresh_token: {token_path}")
                    else:
                        console.print(
                            f"  [green]✓[/] token file present with refresh_token: "
                            f"[dim]{token_path}[/]"
                        )

        # ───── Check 4: backend connectivity ─────
        # Instantiate the backend, fetch credentials, make ONE light read
        # call (list_folders on the root folder, or sftp.stat for SFTP).
        # On exception, branch on exception class to give a specific fix.
        connectivity_ok = False
        try:
            storage = _create_storage(config)
            if backend_name == "sftp":
                # SFTP exposes a paramiko.SFTPClient via get_credentials();
                # stat'ing the configured folder doubles as both a "session
                # opens" check AND a "folder exists / readable" check.
                sftp_client = storage.get_credentials()
                sftp_folder_v = getattr(config, "sftp_folder", "") or "/"
                _stat = sftp_client.stat(sftp_folder_v)
                # paramiko returns SFTPAttributes; mode bit S_IFDIR (0o040000)
                # tells us it's a directory.
                import stat as _stat_mod
                if not _stat_mod.S_ISDIR(_stat.st_mode):
                    raise RuntimeError(
                        f"sftp_folder is not a directory: {sftp_folder_v}"
                    )
            else:
                storage.get_credentials()
                storage.list_folders(config.root_folder, name=None)
            connectivity_ok = True
        except BaseException as exc:  # noqa: BLE001 — diagnostic, must not bubble
            # Classify via the backend's own classifier when possible — it
            # knows about HTTP status codes, OAuth `invalid_grant`, etc.
            exc_class_name = type(exc).__name__
            exc_text = str(exc)
            try:
                # Re-create a fresh backend just for classification — the
                # failed one may not be in a usable state. classify_error
                # is documented to never raise.
                klass = _create_storage(config).classify_error(exc)
            except Exception:
                klass = None

            from .backends import ErrorClass as _EC

            # AUTH-class → user must run `claude-mirror auth`.
            is_auth = (
                klass == _EC.AUTH
                or "RefreshError" in exc_class_name
                or "invalid_grant" in exc_text.lower()
                or "401" in exc_text
            )
            # Permission-class → 403 / forbidden / token revoked at server.
            is_permission = (
                klass == _EC.PERMISSION
                or "403" in exc_text
                or "permission" in exc_text.lower()
                or "forbidden" in exc_text.lower()
            )
            # 404 → folder ID wrong (Drive); user must check provider UI.
            is_not_found = (
                klass == _EC.FILE_REJECTED and "404" in exc_text
            ) or "404" in exc_text or "not found" in exc_text.lower()
            # Network-class → transient transport / DNS / timeout failures.
            is_network = (
                klass == _EC.TRANSIENT
                or isinstance(exc, (TimeoutError, ConnectionError))
                or "TransportError" in exc_class_name
                or "timed out" in exc_text.lower()
                or "connection" in exc_text.lower()
            )

            if backend_name == "sftp":
                # SFTP-specific fix hints — point at concrete server-side
                # actions (host-key trust, port reachability, server-side
                # mkdir, account ACLs) rather than generic OAuth / web-UI
                # remedies that don't apply.
                _sftp_host = getattr(config, "sftp_host", "") or "?"
                _sftp_port = getattr(config, "sftp_port", 22)
                _sftp_folder_v = getattr(config, "sftp_folder", "") or "?"
                if is_auth:
                    hint = (
                        f"[yellow]Fix:[/] SSH authentication failed. Run "
                        f"[bold]claude-mirror auth --config {path}[/] to "
                        f"re-verify host key + key/password."
                    )
                elif is_network:
                    hint = (
                        f"[yellow]Fix:[/] network reachability — check "
                        f"[bold]ping {_sftp_host}[/] and that port "
                        f"[bold]{_sftp_port}[/] is open."
                    )
                elif is_permission:
                    hint = (
                        f"[yellow]Fix:[/] your account lacks access to "
                        f"[bold]{_sftp_folder_v}[/] on the server."
                    )
                elif is_not_found:
                    hint = (
                        f"[yellow]Fix:[/] [bold]{_sftp_folder_v}[/] doesn't "
                        f"exist on the server. Create it (server-side "
                        f"`mkdir`) or change `sftp_folder` in [bold]{path}[/]."
                    )
                else:
                    hint = (
                        f"[yellow]Fix:[/] inspect the error above. Verify "
                        f"host/port/credentials in [bold]{path}[/] and "
                        f"re-run [bold]claude-mirror auth --config {path}[/]."
                    )
            elif is_auth:
                hint = (
                    f"[yellow]Fix:[/] token revoked or refresh failed. Run "
                    f"[bold]claude-mirror auth --config {path}[/] to "
                    f"re-authenticate."
                )
            elif is_permission:
                hint = (
                    f"[yellow]Fix:[/] insufficient permissions for the "
                    f"configured folder. Run "
                    f"[bold]claude-mirror auth --config {path}[/] or check "
                    f"folder sharing in the provider's web UI."
                )
            elif is_not_found:
                hint = (
                    f"[yellow]Fix:[/] folder ID "
                    f"[bold]{config.root_folder!r}[/] not found. Verify "
                    f"it in the cloud provider's web UI and update "
                    f"[bold]{path}[/]."
                )
            elif is_network:
                hint = (
                    "[yellow]Fix:[/] check internet connectivity (and any "
                    "corporate proxy / VPN settings) and retry."
                )
            else:
                hint = (
                    f"[yellow]Fix:[/] inspect the error above and re-run "
                    f"[bold]claude-mirror auth --config {path}[/] if it "
                    f"looks auth-related."
                )

            console.print(
                f"  [red]✗[/] backend connectivity failed "
                f"({exc_class_name}): [dim]{exc_text[:160]}[/]\n"
                f"      {hint}"
            )
            failures.append(f"connectivity failed for {config.backend}: {exc_class_name}")

        if connectivity_ok:
            if backend_name == "sftp":
                _sftp_folder_v = getattr(config, "sftp_folder", "") or "/"
                console.print(
                    f"  [green]✓[/] SFTP connectivity ok "
                    f"([dim]session opened + stat({_sftp_folder_v}) "
                    f"succeeded[/])"
                )
            else:
                console.print(
                    f"  [green]✓[/] backend connectivity ok "
                    f"([dim]list_folders on root succeeded[/])"
                )

        # ───── SFTP-specific auxiliary checks ─────
        # Local-filesystem checks for SFTP only — key file readability,
        # known_hosts presence (when strict-host-check is on), and a
        # plaintext-password advisory when the YAML stores a bare
        # password. These run regardless of connectivity outcome so the
        # user sees every fixable issue in one pass.
        if backend_name == "sftp":
            sftp_key_v = getattr(config, "sftp_key_file", "") or ""
            sftp_pw_v = getattr(config, "sftp_password", "") or ""
            sftp_kh_v = (
                getattr(config, "sftp_known_hosts_file", "")
                or "~/.ssh/known_hosts"
            )
            sftp_strict_v = bool(
                getattr(config, "sftp_strict_host_check", True)
            )

            # Key file readable.
            if sftp_key_v:
                key_expanded = str(Path(sftp_key_v).expanduser())
                if not os.access(key_expanded, os.R_OK):
                    console.print(
                        f"  [red]✗[/] SSH key file not readable: "
                        f"[bold]{key_expanded}[/]\n"
                        f"      [yellow]Fix:[/] key file at "
                        f"[bold]{key_expanded}[/] is not readable by "
                        f"the current user. Check permissions "
                        f"(typically 0600) — "
                        f"[bold]chmod 600 {key_expanded}[/]."
                    )
                    failures.append(
                        f"SFTP key file not readable: {key_expanded}"
                    )
                else:
                    console.print(
                        f"  [green]✓[/] SSH key file readable: "
                        f"[dim]{key_expanded}[/]"
                    )

            # known_hosts file present (only required when strict checking).
            if sftp_strict_v:
                kh_expanded = str(Path(sftp_kh_v).expanduser())
                if not os.path.exists(kh_expanded):
                    console.print(
                        f"  [red]✗[/] known_hosts file missing: "
                        f"[bold]{kh_expanded}[/]\n"
                        f"      [yellow]Fix:[/] first connect via "
                        f"[bold]ssh "
                        f"{getattr(config, 'sftp_username', 'user')}@"
                        f"{getattr(config, 'sftp_host', 'host')}[/] "
                        f"to populate it, or set "
                        f"[bold]sftp_strict_host_check: false[/] in "
                        f"[bold]{path}[/] for one-shot LAN setups."
                    )
                    failures.append(
                        f"SFTP known_hosts missing: {kh_expanded}"
                    )
                else:
                    console.print(
                        f"  [green]✓[/] known_hosts file present: "
                        f"[dim]{kh_expanded}[/]"
                    )
            else:
                console.print(
                    "  [yellow]⚠[/] SFTP strict host-key check is "
                    "disabled — host fingerprints will not be verified. "
                    "Acceptable for closed-LAN setups; risky on the "
                    "open internet."
                )

            # Plaintext password advisory (warning, not failure).
            if sftp_pw_v:
                console.print(
                    f"  [yellow]⚠[/] SFTP password is stored in plain "
                    f"text in [bold]{path}[/]. Recommended only for "
                    f"LAN/test setups. Switch to key-based auth for "
                    f"any internet-reachable server."
                )

        # ───── Deep Drive checks (googledrive only) ─────
        # Adds Drive-specific assertions the generic loop above can't
        # make: OAuth scope inventory, Drive-API-enabled probe, Pub/Sub
        # topic + per-machine subscription presence, and the IAM grant
        # for Drive's service account on the topic. Skipped silently
        # for other backends.
        if backend_name == "googledrive":
            failures.extend(_run_googledrive_deep_checks(path, config))

        # ───── Deep Dropbox checks (dropbox only) ─────
        # Adds Dropbox-specific assertions the generic loop above can't
        # make: token JSON shape, app-key format, account smoke test,
        # granted scope inspection, configured-folder access, and an
        # info line about team-account admin policies. Skipped silently
        # for other backends.
        if backend_name == "dropbox":
            failures.extend(_run_dropbox_deep_checks(path, config))

        # ───── OneDrive deep checks (DOC-ONE) ─────
        if backend_name == "onedrive":
            console.print("\n[bold]OneDrive deep checks[/]")
            failures.extend(_run_onedrive_deep_checks(path, config))

        # ───── WebDAV deep checks (DOC-WD) ─────
        if backend_name == "webdav":
            console.print("\n[bold]WebDAV deep checks[/]")
            failures.extend(_run_webdav_deep_checks(path, config))

        # ───── SFTP deep checks (DOC-SFTP) ─────
        if backend_name == "sftp":
            console.print("\n[bold]SFTP deep checks[/]")
            failures.extend(_run_sftp_deep_checks(path, config))

        # ───── Check 5: project_path exists locally ─────
        # Only check on the primary — every mirror config validated by
        # `_create_storage_set` must point at the SAME project_path, so
        # checking it once on the primary is sufficient. We still emit
        # an info line for mirror configs so the output is symmetric.
        proj = Path(config.project_path).expanduser()
        if not proj.exists():
            console.print(
                f"  [red]✗[/] project_path does not exist: [bold]{proj}[/]\n"
                f"      [yellow]Fix:[/] update `project_path` in "
                f"[bold]{path}[/] to point at the actual project directory."
            )
            failures.append(f"project_path missing: {proj}")
        elif not proj.is_dir():
            console.print(
                f"  [red]✗[/] project_path is not a directory: "
                f"[bold]{proj}[/]\n"
                f"      [yellow]Fix:[/] update `project_path` in "
                f"[bold]{path}[/] to point at the actual project directory."
            )
            failures.append(f"project_path not a directory: {proj}")
        else:
            console.print(
                f"  [green]✓[/] project_path exists: [dim]{proj}[/]"
            )

        # ───── Check 6: manifest integrity (if present) ─────
        # Manifest.load() auto-recovers from a corrupt manifest by moving
        # it aside, which would mask the issue from the user. So we read
        # the file ourselves first; if it fails to parse as JSON, we
        # report and suggest removing it.
        from .manifest import MANIFEST_FILE
        manifest_path = proj / MANIFEST_FILE
        if not manifest_path.exists():
            console.print(
                f"  [dim]·[/] manifest not present yet "
                f"([dim]{MANIFEST_FILE}[/]) — first sync will create it"
            )
        else:
            try:
                _json.loads(manifest_path.read_text())
                console.print(
                    f"  [green]✓[/] manifest parses: [dim]{manifest_path}[/]"
                )
            except (OSError, _json.JSONDecodeError) as e:
                console.print(
                    f"  [red]✗[/] manifest is corrupt: [bold]{manifest_path}[/]\n"
                    f"      [dim]{e}[/]\n"
                    f"      [yellow]Fix:[/] remove it and re-sync — "
                    f"[bold]rm {manifest_path} && "
                    f"claude-mirror sync --config {path}[/]"
                )
                failures.append(f"manifest corrupt: {manifest_path}")

    return failures


@cli.command()
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
@click.option("--backend", "backend_filter", default="",
              help="Limit checks to one backend by name "
                   "(googledrive, dropbox, onedrive, webdav, sftp). Default: "
                   "check all configured backends including Tier 2 mirrors.")
def doctor(config_path: str, backend_filter: str) -> None:
    """Diagnose claude-mirror configuration health.

    Runs through every common configuration check and reports what is
    wrong with concrete fix commands. Exit code 0 on all-pass, 1 on
    any failure — composes cleanly with shell scripts and CI.

    \b
    Checks performed (per backend, including Tier 2 mirrors):
      1. Config file exists and parses
      2. Credentials file exists (skipped for WebDAV / SFTP)
      3. Token file exists, parses, has refresh_token
         (or for WebDAV / SFTP: required fields present in config)
      4. Backend connectivity (list_folders on the configured root, or
         sftp.stat for SFTP)
      4a. SFTP only: key file readable, known_hosts present (if strict
          host-check is on), plaintext-password advisory
      4b. googledrive only: OAuth scope inventory (Drive required, Pub/Sub
          optional), Drive API enabled, Pub/Sub API enabled, topic exists,
          per-machine subscription exists, IAM grant for Drive's service
          account on the topic
      4c. dropbox only: token JSON shape, app-key format, account smoke
          test (users_get_current_account), scope inspection (PKCE only),
          folder access (files_list_folder), team-account info line
      5. project_path exists locally and is a directory
      6. Manifest integrity (if a manifest file is present)

    \b
    Examples:
      claude-mirror doctor
      claude-mirror doctor --config ~/.config/claude_mirror/work.yaml
      claude-mirror doctor --backend dropbox        # generic + deep Dropbox checks
      claude-mirror doctor --backend googledrive    # generic + deep Drive checks
    """
    cfg_path = _resolve_config(config_path)
    console.print(f"[bold]claude-mirror doctor[/] — {cfg_path}\n")

    failures = _run_doctor_checks(cfg_path, backend_filter)

    if failures:
        console.print(
            f"\n[red bold]✗ {len(failures)} issue(s) found.[/] "
            f"Fix the items above and re-run [bold]claude-mirror doctor[/]."
        )
        sys.exit(1)
    console.print("\n[green bold]✓ All checks passed.[/]")
