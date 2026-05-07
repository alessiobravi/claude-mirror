from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field, asdict
from pathlib import Path

import yaml

CONFIG_DIR = Path.home() / ".config" / "claude_mirror"


@dataclass
class Config:
    project_path: str
    drive_folder_id: str = ""
    gcp_project_id: str = ""
    pubsub_topic_id: str = ""
    file_patterns: list[str] = field(default_factory=lambda: ["**/*.md"])
    exclude_patterns: list[str] = field(default_factory=list)
    credentials_file: str = ""
    token_file: str = ""
    machine_name: str = ""
    user: str = ""
    backend: str = "googledrive"
    # Dropbox-specific
    dropbox_app_key: str = ""
    dropbox_folder: str = ""  # e.g. "/claude-mirror/myproject"
    # OneDrive-specific
    onedrive_client_id: str = ""  # Azure app registration client ID
    onedrive_folder: str = ""     # e.g. "claude-mirror/myproject"
    # WebDAV-specific
    webdav_url: str = ""       # e.g. "https://my-server.com/remote.php/dav/files/user/claude-mirror/"
    webdav_username: str = ""
    webdav_password: str = ""  # or app password
    # Allow http:// WebDAV URLs (cleartext basic-auth + payloads).
    # Default false: backend construction raises if the URL scheme is
    # `http` and this flag is not explicitly set. Intended only for
    # closed LAN test setups; never set this on a config that talks to
    # a public server.
    webdav_insecure_http: bool = False
    poll_interval: int = 30    # seconds between polling checks (WebDAV, OneDrive)
    # Slack notifications (optional, per-project)
    slack_enabled: bool = False
    slack_webhook_url: str = ""
    slack_channel: str = ""    # override channel (optional, webhook default used if empty)
    # Snapshot format:
    #   "full"  — every snapshot is a full server-side copy of the project tree
    #             into _claude_mirror_snapshots/{ts}/ (legacy default for existing
    #             projects, since YAMLs without this field load as "full").
    #   "blobs" — content-addressed: a manifest JSON at _claude_mirror_snapshots/
    #             {ts}.json plus deduplicated blobs at _claude_mirror_blobs/
    #             {hash[:2]}/{hash}. Identical content across snapshots is
    #             stored exactly once. Use `claude-mirror gc` to reclaim space
    #             from blobs no longer referenced by any snapshot.
    # Both formats coexist on the same remote — restore auto-detects per
    # snapshot, so switching format does not lose access to prior snapshots.
    snapshot_format: str = "full"

    # Tier 2 multi-backend mirroring (v0.4.0+)
    #
    # When mirrors are configured, every push, sync, and delete operates
    # on ALL backends (primary + mirrors) in parallel. Pull is primary-
    # only by design (write-replica model — mirrors receive pushes but
    # are not consulted as upstream).
    #
    # Each mirror needs its own credentials/folder fields under the same
    # config — those are scoped via separate per-backend config files
    # reachable from `mirror_config_paths` (one config file per mirror).
    # This keeps the credentials schema simple and isolates token files /
    # folder IDs per backend.
    #
    # mirror_config_paths: ordered list of paths to other YAML configs;
    # each MUST point at the same project_path as this config. The
    # backend named by each mirror config's `backend` field becomes one
    # of the mirror targets.
    mirror_config_paths: list[str] = field(default_factory=list)

    # snapshot_on: "primary" | "all". Controls whether snapshots are
    # mirrored to every backend or only created on the primary.
    # Default for blobs format is "all" (cheap, dedup'd); default for
    # full format is "primary" (server-side copy is per-backend
    # expensive). Resolved by `effective_snapshot_on()` below.
    #
    # When `mirror_config_paths` is empty, this setting has no effect —
    # only one backend exists, so "all" and "primary" are equivalent.
    snapshot_on: str = ""

    # retry_on_push: scan the manifest for files with state="pending_retry"
    # at the start of every push and retry them before processing newly-
    # changed files. True by default — the whole point of the retry queue.
    retry_on_push: bool = True

    # max_retry_attempts: in-process retry attempts per upload before
    # giving up and marking the file pending_retry for next-push pickup.
    # Clamped to [1, 8] in __post_init__ — values outside this range
    # would either disable retry entirely (0/negative) or pin a worker
    # thread for days on exponential-backoff schedules (large values),
    # so a hostile mirror config cannot DoS the watcher.
    max_retry_attempts: int = 3

    # notify_failures: surface per-backend failures to desktop +
    # configured Slack webhook. Independent of the existing
    # slack_enabled flag — a project can have Slack on for events but
    # opt out of failure-only notifications.
    notify_failures: bool = True

    # parallel_workers: maximum concurrent ThreadPoolExecutor workers
    # used for blob uploads, snapshot copies, recursive listings, and
    # other parallel operations. Tune up on fat connections, down on
    # rate-limited APIs / slow CPUs. Default 5 mirrors the
    # `claude_mirror._constants.PARALLEL_WORKERS` fallback used when
    # no Config object is available.
    parallel_workers: int = 5

    # Snapshot retention policy (v0.5.32+).
    #
    # When any of these is > 0, `claude-mirror push` automatically prunes
    # snapshots older than the policy's keep-set after a successful push,
    # AND `claude-mirror prune` (no selectors) reads the same fields to
    # know what to delete. Each is evaluated independently and the union
    # of their keep-sets is retained — so the user can compose
    # "newest 7 + last 30 days + last 12 months + last 5 years" cleanly.
    #
    #   keep_last     — keep the N newest snapshots regardless of age.
    #   keep_daily    — for the last N days, keep the newest snapshot
    #                   in each day-bucket (UTC). N=7 keeps up to 7
    #                   snapshots, one per day.
    #   keep_monthly  — for the last N months, keep the newest snapshot
    #                   in each month-bucket. Months are bucketed by
    #                   year+month (UTC). N=12 keeps up to 12 snapshots.
    #   keep_yearly   — for the last N years, keep the newest snapshot
    #                   in each year-bucket.
    #
    # All four default to 0 = disabled. With every field at 0, push runs
    # exactly as before and `prune` requires explicit CLI selectors.
    keep_last: int = 0
    keep_daily: int = 0
    keep_monthly: int = 0
    keep_yearly: int = 0

    def __post_init__(self) -> None:
        if not self.credentials_file:
            self.credentials_file = str(CONFIG_DIR / "credentials.json")
        if not self.token_file:
            self.token_file = str(CONFIG_DIR / "token.json")
        if not self.machine_name:
            self.machine_name = socket.gethostname()
        if not self.user:
            self.user = os.environ.get("USER", "unknown")
        # Clamp max_retry_attempts to [1, 8]. See field comment above.
        try:
            self.max_retry_attempts = min(max(int(self.max_retry_attempts), 1), 8)
        except (TypeError, ValueError):
            self.max_retry_attempts = 3

    def effective_snapshot_on(self) -> str:
        """Resolve snapshot_on with format-aware defaults.

        Explicit `snapshot_on` value wins. Otherwise, default depends on
        snapshot_format: blobs format defaults to `all` (cheap, dedup'd
        across snapshots), full format defaults to `primary` (full
        server-side copies per backend would be expensive).
        """
        if self.snapshot_on in ("all", "primary"):
            return self.snapshot_on
        fmt = (self.snapshot_format or "full").lower()
        return "all" if fmt == "blobs" else "primary"

    @property
    def root_folder(self) -> str:
        """Return the backend-appropriate root folder reference."""
        if self.backend == "dropbox":
            return self.dropbox_folder
        if self.backend == "onedrive":
            return ""  # OneDrive uses path-based Graph API; paths are relative to onedrive_folder
        if self.backend == "webdav":
            return ""  # WebDAV uses the base URL directly; paths are relative
        return self.drive_folder_id

    @property
    def subscription_id(self) -> str:
        safe = self.machine_name.replace(".", "-").replace(" ", "-").lower()
        return f"{self.pubsub_topic_id}-{safe}"

    @classmethod
    def load(cls, path: str) -> Config:
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)

    def save(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
