from __future__ import annotations

import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

# Suppress gRPC / abseil INFO noise on macOS before gRPC is imported
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GRPC_TRACE", "")

import click
import google.auth.exceptions
from rich.console import Console
from rich.table import Table

from .backends import StorageBackend
from .backends.googledrive import GoogleDriveBackend
from .config import Config, CONFIG_DIR
from .events import SyncEvent, SyncLog, SYNC_LOG_NAME, LOGS_FOLDER
from .manifest import Manifest
from .merge import MergeHandler
from .notifications import NotificationBackend
from .notifications.pubsub import PubSubNotifier
from .notifier import Notifier, read_and_clear_inbox
from .snapshots import SnapshotManager
from .sync import SyncEngine


def _get_version() -> str:
    """Return the installed package version."""
    try:
        from importlib.metadata import version
        return version("claude-mirror")
    except Exception:
        return "unknown"

console = Console(force_terminal=True)

DEFAULT_CONFIG = str(Path.home() / ".config" / "claude_mirror" / "default.yaml")


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


@click.group(cls=_CLIGroup)
@click.version_option()
def cli() -> None:
    """Sync Claude project MD files across machines via cloud storage."""


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


def _run_wizard() -> dict:
    """Interactive wizard that collects all init parameters. Returns a dict of values."""
    console.print("\n[bold cyan]claude-mirror setup wizard[/]\n")
    console.print("Press Enter to accept the [dim]default[/] shown in brackets.\n")

    _SUPPORTED_BACKENDS = ("googledrive", "dropbox", "onedrive", "webdav")

    # Backend
    console.print(
        f"[dim]Storage backend: {' | '.join(_SUPPORTED_BACKENDS)}[/]"
    )
    backend = click.prompt("Storage backend", default="googledrive")
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
    poll_interval = 30  # default; only meaningful for onedrive/webdav

    if backend == "googledrive":
        # Credentials file
        console.print(
            "\n[dim]Credentials file: the OAuth2 JSON downloaded from Google Cloud Console.[/]"
        )
        raw_creds = click.prompt("Credentials file", default=_DEFAULT_CREDENTIALS)
        credentials_file = str(Path(raw_creds).expanduser())

        console.print()

        # Drive folder ID
        console.print(
            "[dim]Drive folder ID: open the target folder in Google Drive and copy the ID from the URL[/]"
            "\n[dim]  https://drive.google.com/drive/folders/<FOLDER_ID>[/]\n"
        )
        drive_folder_id = click.prompt("Drive folder ID")

        # GCP project ID
        console.print(
            "\n[dim]GCP project ID: found in Google Cloud Console → project selector (e.g. my-project-123)[/]\n"
        )
        gcp_project_id = click.prompt("GCP project ID")

        # Pub/Sub topic ID
        console.print(
            f"\n[dim]Pub/Sub topic ID: a unique name for this project's notification channel.[/]\n"
        )
        pubsub_topic_id = click.prompt(
            "Pub/Sub topic ID", default=f"claude-mirror-{project_name}"
        )
    elif backend == "dropbox":
        # Dropbox app key
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
        console.print(
            "\n[dim]Username for WebDAV authentication (basic auth).[/]\n"
        )
        webdav_username = click.prompt("Username")

        # Password
        console.print(
            "\n[dim]Password or app password. Stored in the token file.[/]"
            "\n[dim]  Nextcloud: generate an app password in Settings → Security.[/]\n"
        )
        import getpass
        webdav_password = getpass.getpass("Password: ")

    # Polling interval for backends without push notifications.
    if backend in ("onedrive", "webdav"):
        console.print(
            "\n[dim]Poll interval (seconds): how often the watcher checks for "
            "remote changes. Lower = more responsive, higher = less network use.[/]\n"
        )
        poll_interval = click.prompt(
            "Poll interval (seconds)", default=30, type=int,
        )

    # Token file
    if backend == "googledrive":
        derived_token = _derive_token_file(credentials_file)
    elif backend == "dropbox":
        derived_token = str(CONFIG_DIR / f"dropbox-{project_name}-token.json")
    elif backend == "onedrive":
        derived_token = str(CONFIG_DIR / f"onedrive-{project_name}-token.json")
    elif backend == "webdav":
        derived_token = str(CONFIG_DIR / f"webdav-{project_name}-token.json")
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
    if backend in ("onedrive", "webdav"):
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
              help="Storage backend: googledrive | dropbox | onedrive | webdav.")
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
@click.option("--poll-interval", "poll_interval", default=30, show_default=True, type=int,
              help="Polling interval in seconds for backends without push notifications (OneDrive, WebDAV).")
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
) -> None:
    """Initialize claude-mirror for a project.

    Run with --wizard for interactive setup, or pass all flags directly.
    """
    # Ensure config directory exists before anything else
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    backend = backend_opt

    if wizard:
        values = _run_wizard()
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
        # Validate required flags per backend
        if backend == "googledrive":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--drive-folder-id", drive_folder_id),
                    ("--gcp-project-id", gcp_project_id),
                    ("--pubsub-topic-id", pubsub_topic_id),
                ] if not val
            ]
        elif backend == "dropbox":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--dropbox-app-key", dropbox_app_key),
                    ("--dropbox-folder", dropbox_folder),
                ] if not val
            ]
        elif backend == "onedrive":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--onedrive-client-id", onedrive_client_id),
                    ("--onedrive-folder", onedrive_folder),
                ] if not val
            ]
        elif backend == "webdav":
            missing = [
                name for name, val in [
                    ("--project", project),
                    ("--webdav-url", webdav_url),
                    ("--webdav-username", webdav_username),
                    ("--webdav-password", webdav_password),
                ] if not val
            ]
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
    )
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
@click.option("--short", is_flag=True, default=False, help="Show summary line only, no file table.")
@click.option("--pending", "pending_only", is_flag=True, default=False,
              help="Tier 2: show only files that have a non-ok state on any "
                   "configured mirror backend (pending_retry or failed_perm). "
                   "Useful for quickly seeing what claude-mirror retry would attempt.")
def status(config_path: str, short: bool, pending_only: bool) -> None:
    """Show sync status for all project files."""
    engine, _, _ = _load_engine(_resolve_config(config_path), with_pubsub=False)
    if pending_only:
        _show_pending_status(engine)
        return
    engine.show_status(short=short)


def _show_pending_status(engine) -> None:
    """Tier 2: render only files with non-ok mirror state. Helps users
    see what's queued for retry without doing a full status pass."""
    if not engine._mirrors:
        console.print("[dim]No mirrors configured for this project; "
                      "no pending state to report.[/]")
        return
    pending_by_path: dict[str, list[tuple[str, str, str]]] = {}
    for path, fs in engine.manifest.all().items():
        for backend_name, rs in fs.remotes.items():
            if rs.state in ("pending_retry", "failed_perm"):
                pending_by_path.setdefault(path, []).append(
                    (backend_name, rs.state, rs.last_error)
                )
    if not pending_by_path:
        console.print("[green]✓ All mirrors are caught up — nothing pending.[/]")
        return
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
    console.print(table)
    console.print(
        "\n[dim]Run [bold]claude-mirror retry[/] to re-attempt the pending entries.[/]"
    )


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
    engine, _, _ = _load_engine(_resolve_config(config_path))
    engine.push(list(files) if files else None, force_local=force_local)


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
def watch(config_path: str) -> None:
    """
    Watch for remote changes via Pub/Sub streaming subscription.
    Sends a system notification when collaborators push updates.
    Press Ctrl+C to stop.
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
    console.print(f"\n[bold]claude-mirror v{_get_version()}[/]")
    console.print(
        f"[green]Watching for updates[/] (project: [bold]{config.project_path}[/])\n"
        f"Backend: [dim]{config.backend}[/] ({sub_info})\n"
        "Press [bold]Ctrl+C[/] to stop."
    )

    notifier.watch(on_event, stop_event)
    notifier.close()
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
def snapshots(config_path: str) -> None:
    """List all snapshots stored on Drive."""
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)
    snap.show_list()


@cli.command()
@click.argument("timestamp")
@click.argument("paths", nargs=-1)
@click.option("--output", default="", help="Directory to restore files into. Defaults to project path.")
@click.option("--backend", "backend_name", default="",
              help="Tier 2: restore SOLELY from the named backend (e.g. "
                   "'dropbox'), bypassing the primary-first fallback chain. "
                   "Useful when the primary is down or you know which "
                   "mirror has the version you want.")
@click.option("--config", "config_path", default="", help="Config file path. Auto-detected from cwd if omitted.")
def restore(timestamp: str, paths: tuple, output: str, backend_name: str, config_path: str) -> None:
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

    By default, files are restored to the original project path (with
    a confirmation prompt). Use --output to restore to a separate
    directory instead — useful for inspecting before overwriting.

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
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def gc(do_delete: bool, dry_run: bool, skip_confirm: bool, config_path: str) -> None:
    """Delete blobs no longer referenced by any snapshot manifest.

    \b
    SAFE BY DEFAULT — running `claude-mirror gc` with no flags performs
    a dry-run scan only. To actually delete, you must:
      1. pass --delete explicitly, AND
      2. confirm twice (or pass --yes to skip the prompts).

    Refuses to run if no blobs-format manifests exist on remote
    (otherwise gc would wipe the entire blob store).

    Only meaningful when snapshot_format is 'blobs'.
    """
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)
    if (config.snapshot_format or "full").lower() != "blobs":
        console.print(
            "[yellow]Note:[/] this project's snapshot_format is "
            f"'{config.snapshot_format}'. gc is only meaningful for the "
            "'blobs' format. Scanning anyway in case stray blobs exist."
        )

    # Up-front banner when running in dry-run mode (no --delete) so the
    # user knows BEFORE the scan starts that nothing will be deleted.
    if not do_delete:
        console.print(
            "[bold yellow]🔍 DRY-RUN mode[/] — scanning for orphan blob(s); "
            "no deletions will be performed."
        )

    # Phase 1: always run a dry-run scan first so we know the scope.
    result = snap.gc(dry_run=True)

    if not do_delete:
        if result.get("refused"):
            return
        orphans = result.get("orphans", 0)
        if orphans > 0:
            console.print(
                f"\n[bold yellow]Dry-run complete.[/] No deletions were performed.\n"
                f"To actually delete {orphans} orphan blob(s):\n"
                f"  [bold cyan]claude-mirror gc --delete[/]\n"
                f"[dim](you'll be asked to type YES to confirm before "
                f"anything is deleted)[/]"
            )
        else:
            console.print(
                "\n[bold yellow]Dry-run complete.[/] Nothing to clean up — "
                "no orphan blobs.\n"
                "[dim]When orphans appear in future runs, use: "
                "[bold]claude-mirror gc --delete[/][/]"
            )
        return

    # --delete path: nothing to do if scan refused or found nothing.
    if result.get("refused") or result.get("orphans", 0) == 0:
        return

    if not skip_confirm:
        orphans = result["orphans"]
        confirmation = click.prompt(
            f"\nThis will permanently delete {orphans} orphan blob(s) "
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

    # Phase 2: real delete (gc internally re-scans + deletes; the second
    # scan is the cost we pay for the safer default. On most projects
    # it's a few seconds; it never deletes anything not still orphaned
    # at the moment of the second scan, which is the safest semantics.)
    snap.gc(dry_run=False)


@cli.command()
@click.argument("path")
@click.option("--config", "config_path", default="",
              help="Config file path. Auto-detected from cwd if omitted.")
def history(path: str, config_path: str) -> None:
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

    The output table is newest-first. Each version transition is shown
    in bold green so version changes are easy to spot. Use the timestamp
    of the version you want with `claude-mirror restore` to recover it:

    \b
      claude-mirror restore <timestamp> <path> --output ~/tmp/recovery
    """
    config = Config.load(_resolve_config(config_path))
    storage = _create_storage(config)
    snap = SnapshotManager(config, storage)
    snap.show_history(path)


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
def inbox(config_path: str) -> None:
    """Show and clear pending notifications for this project."""
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
def log(config_path: str, limit: int) -> None:
    """Show recent sync activity from collaborators."""
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

@cli.command()
@click.argument(
    "shell",
    type=click.Choice(["bash", "zsh", "fish"], case_sensitive=False),
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

    After restarting your shell, `claude-mirror <TAB>` completes commands
    and `claude-mirror push <TAB>` completes flag names. High-value flags
    (--config, --backend) also complete their values.
    """
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    shell_classes = {
        "bash": BashComplete,
        "zsh": ZshComplete,
        "fish": FishComplete,
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
