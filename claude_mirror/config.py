from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

# Closed set of event-action strings accepted by `*_routes[*].on`. Anything
# outside this set is a typo or a forward-compat schema drift; we reject at
# `Config.__post_init__` time rather than waiting for the first event-fire.
_VALID_ROUTE_ACTIONS: frozenset[str] = frozenset(
    {"push", "pull", "sync", "delete"}
)
_DEFAULT_ROUTE_ACTIONS: tuple[str, ...] = ("push", "pull", "sync", "delete")
_DEFAULT_ROUTE_PATHS: tuple[str, ...] = ("**/*",)

CONFIG_DIR = Path.home() / ".config" / "claude_mirror"

# Module-level override used by `Config.load` when no explicit
# `profile_override` argument is passed. The CLI's global `--profile NAME`
# flag (since v0.5.49) sets this once, before any subcommand runs, so
# every downstream Config.load() picks the profile up without each
# command having to thread it through. Tests should set/restore this
# via monkeypatch rather than touching the global directly.
_GLOBAL_PROFILE_OVERRIDE: str = ""


def set_global_profile_override(name: str) -> None:
    """Set the process-wide profile override consulted by `Config.load`.

    Empty string clears the override. Idempotent: callable multiple
    times. The CLI calls this from the global click-group invoke()
    handler before dispatching to a subcommand.
    """
    global _GLOBAL_PROFILE_OVERRIDE
    _GLOBAL_PROFILE_OVERRIDE = name or ""


def get_global_profile_override() -> str:
    """Return the current process-wide profile override (or empty string)."""
    return _GLOBAL_PROFILE_OVERRIDE


def _normalise_routes(
    raw: Optional[list[dict[str, Any]]], field_name: str
) -> Optional[list[dict[str, Any]]]:
    """Validate + fill defaults on a `*_routes` list.

    Returns ``None`` when ``raw`` is None or empty so the legacy
    single-channel dispatch path is taken untouched. When ``raw`` is a
    non-empty list, every entry is normalised to a dict containing
    ``webhook_url`` (required, non-empty string), ``on`` (default
    ``["push","pull","sync","delete"]``), and ``paths`` (default
    ``["**/*"]``).

    Raises ``ValueError`` on:
      * non-list value
      * non-dict element
      * missing / empty / non-string ``webhook_url``
      * ``on`` entry outside the closed action set
      * ``paths`` entry that isn't a non-empty string

    The error message names the offending field (`field_name`) and the
    1-indexed position so a user with a 12-route list can find the
    typo without binary search.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError(
            f"{field_name} must be a list of route dicts, got {type(raw).__name__}"
        )
    if not raw:
        # Treat empty list as "not configured" so iter_routes still
        # falls back to the legacy single-channel form.
        return None
    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(
                f"{field_name}[{idx}] must be a dict, got {type(entry).__name__}"
            )
        url = entry.get("webhook_url")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"{field_name}[{idx}] is missing required string field "
                f"'webhook_url' (got {url!r})"
            )
        on_raw = entry.get("on")
        if on_raw is None:
            on_list: list[str] = list(_DEFAULT_ROUTE_ACTIONS)
        else:
            if not isinstance(on_raw, list) or not on_raw:
                raise ValueError(
                    f"{field_name}[{idx}].on must be a non-empty list of "
                    f"action strings (got {on_raw!r})"
                )
            for action in on_raw:
                if action not in _VALID_ROUTE_ACTIONS:
                    raise ValueError(
                        f"{field_name}[{idx}].on contains unknown action "
                        f"{action!r}; expected any of "
                        f"{sorted(_VALID_ROUTE_ACTIONS)}"
                    )
            on_list = [str(a) for a in on_raw]
        paths_raw = entry.get("paths")
        if paths_raw is None:
            paths_list: list[str] = list(_DEFAULT_ROUTE_PATHS)
        else:
            if not isinstance(paths_raw, list) or not paths_raw:
                raise ValueError(
                    f"{field_name}[{idx}].paths must be a non-empty list "
                    f"of glob strings (got {paths_raw!r})"
                )
            for p in paths_raw:
                if not isinstance(p, str) or not p:
                    raise ValueError(
                        f"{field_name}[{idx}].paths entry must be a "
                        f"non-empty string (got {p!r})"
                    )
            paths_list = list(paths_raw)
        normalised: dict[str, Any] = {
            "webhook_url": url.strip(),
            "on": on_list,
            "paths": paths_list,
        }
        # Forward any extra keys verbatim (e.g. per-route extra_headers
        # for the generic webhook). The dispatcher reads what it needs
        # and ignores the rest.
        for k, v in entry.items():
            if k not in ("webhook_url", "on", "paths"):
                normalised[k] = v
        out.append(normalised)
    return out
# Whitelist of sync-action names that may key a notification template
# dict. A typo here would otherwise mean "your `delet:` template is
# silently skipped" — surfacing it at config-load time is the cheapest
# place to catch the mistake. Order matches the action ordering used
# elsewhere (push / pull / sync / delete); the set form is what
# validation actually checks.
_VALID_TEMPLATE_ACTIONS: frozenset[str] = frozenset({
    "push", "pull", "sync", "delete",
})


def _validate_template_dict(
    value: object, field_name: str, value_type: type,
) -> None:
    """Validate a per-backend × per-action template dict at config load.

    Skips entirely when ``value`` is ``None`` (the unset default — the
    backend will use its built-in format). When set, every key MUST be
    one of ``_VALID_TEMPLATE_ACTIONS`` and every value MUST be an
    instance of ``value_type`` (``str`` for Slack/Discord/Teams,
    ``dict`` for the Generic webhook). A bad config raises
    :class:`ValueError` with a message naming the offending field +
    key + expected type so the user can fix the YAML quickly.
    """
    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError(
            f"{field_name} must be a dict mapping action name -> "
            f"{value_type.__name__}, got {type(value).__name__}"
        )
    for action, template in value.items():
        if action not in _VALID_TEMPLATE_ACTIONS:
            valid = ", ".join(sorted(_VALID_TEMPLATE_ACTIONS))
            raise ValueError(
                f"{field_name}: unknown action key {action!r} "
                f"(valid: {valid})"
            )
        if not isinstance(template, value_type):
            raise ValueError(
                f"{field_name}[{action!r}] must be a "
                f"{value_type.__name__}, got {type(template).__name__}"
            )


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
    # SFTP-specific (v0.5.33+)
    sftp_host: str = ""
    sftp_port: int = 22
    sftp_username: str = ""
    # Authentication: prefer sftp_key_file (path to private key, ~ expanded);
    # sftp_password is fallback only and should be reserved for closed LAN
    # test setups — claude-mirror doctor warns when it's set in YAML.
    sftp_key_file: str = ""
    sftp_password: str = ""
    sftp_known_hosts_file: str = "~/.ssh/known_hosts"
    # Set False only for one-shot test setups; leaving this True (default)
    # means an unrecognised host fingerprint aborts the connection with
    # ErrorClass.AUTH rather than silently trusting a possible MITM.
    sftp_strict_host_check: bool = True
    sftp_folder: str = ""  # absolute path on server, e.g. "/home/alice/claude-mirror/myproject"
    # FTP / FTPS-specific (BACKEND-FTP). Targets the legacy shared-hosting
    # market (cPanel / DirectAdmin / old WordPress hosts) and NAS devices
    # that gate on plain FTP. SFTP remains the canonical secure-transfer
    # backend for internet-reachable servers; FTPS (`ftp_tls=explicit`) is
    # accepted as a middle-ground, and `ftp_tls=off` is gated behind a loud
    # warning emitted at every authenticate() because credentials travel
    # in cleartext on every connection.
    ftp_host: str = ""
    ftp_port: int = 21
    ftp_username: str = ""
    ftp_password: str = ""
    ftp_folder: str = ""
    # `explicit` = AUTH TLS on the standard control port (default + recommended).
    # `implicit` = legacy FTPS-on-990 (entire control channel in TLS from the
    # first byte). `off` = cleartext FTP (LAN/test only — see warning above).
    ftp_tls: str = "explicit"
    ftp_passive: bool = True
    # S3-compatible (BACKEND-S3): one shape works for AWS S3, Cloudflare
    # R2, Backblaze B2 (S3 API), Wasabi, MinIO, Tigris, IDrive E2, Linode
    # Object Storage, DigitalOcean Spaces, Storj, Hetzner Storage Box, and
    # any other S3-compatible service. ``s3_endpoint_url`` selects the
    # provider; leave empty for AWS proper.
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_region: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_prefix: str = ""        # within-bucket path prefix; empty = use project name
    # Path-style addressing (https://endpoint/bucket/key) is required by
    # MinIO and a few S3-compat services that don't terminate TLS for
    # virtual-hosted-style URLs. AWS proper accepts both; default to
    # virtual-hosted-style.
    s3_use_path_style: bool = False
    poll_interval: int = 30    # seconds between polling checks (WebDAV, OneDrive)
    # Slack notifications (optional, per-project)
    slack_enabled: bool = False
    slack_webhook_url: str = ""
    slack_channel: str = ""    # override channel (optional, webhook default used if empty)
    # Discord notifications (optional, per-project) — POSTs an embed card
    # to a Discord incoming webhook (https://discord.com/api/webhooks/{id}/{token}).
    # Same opt-in / best-effort contract as Slack: failures never block sync.
    discord_enabled: bool = False
    discord_webhook_url: str = ""
    # Microsoft Teams notifications (optional, per-project) — POSTs a
    # MessageCard payload to a Teams incoming webhook (legacy connector at
    # outlook.office.com/webhook/... OR the newer {tenant}.webhook.office.com/...
    # form). Same opt-in / best-effort contract as Slack and Discord.
    teams_enabled: bool = False
    teams_webhook_url: str = ""
    # Generic webhook (optional, per-project) — POSTs a schema-stable JSON
    # envelope (version 1: event/user/machine/project/files/timestamp) to
    # any URL. Designed for n8n / Make / Zapier / internal endpoints that
    # need a stable structured payload. `webhook_extra_headers` carries
    # auth tokens (e.g. {"Authorization": "Bearer abc"}) onto the request.
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_extra_headers: Optional[dict[str, str]] = None

    # Per-project multi-channel notification routing (v0.5.50+).
    #
    # Each backend's existing single-channel field (`slack_webhook_url`,
    # `discord_webhook_url`, `teams_webhook_url`, `webhook_url`) gains an
    # optional list-form alternative. Each list entry has shape:
    #
    #   {
    #     "webhook_url": "https://...",         # required, non-empty string
    #     "on":     ["push", "delete"],         # subset of {push,pull,sync,delete}
    #     "paths":  ["memory/**", "**/CLAUDE.md"],  # fnmatch globs (Python fnmatch)
    #   }
    #
    # Defaults applied in `__post_init__` when a key is omitted:
    #   on    -> ["push","pull","sync","delete"]   (every action)
    #   paths -> ["**/*"]                          (every file)
    #
    # Precedence rule: when BOTH the legacy single-channel field
    # (`slack_webhook_url`) and the list-form (`slack_routes`) are set on
    # the same Config, the list-form wins. The legacy field is silently
    # dropped from dispatch and a one-shot info line is emitted at engine
    # startup ("ignoring slack_webhook_url because slack_routes is set").
    # We don't fail because the user may be in a transition.
    #
    # When a route's `paths` filter matches NO files in an event, the
    # route is skipped entirely (no payload sent). When SOME files match,
    # a route-scoped event is constructed with `event.files` trimmed to
    # the matching subset before the notifier is invoked.
    #
    # Each route gets its own notifier instance with its own webhook URL —
    # different URLs CANNOT share a notifier because each construction
    # captures the URL. Routes within a single backend fire sequentially.
    # Cross-backend dispatch is independent (Slack's routes never affect
    # Discord's), and each backend's list is evaluated on its own.
    slack_routes: Optional[list[dict[str, Any]]] = None
    discord_routes: Optional[list[dict[str, Any]]] = None
    teams_routes: Optional[list[dict[str, Any]]] = None
    webhook_routes: Optional[list[dict[str, Any]]] = None

    # Per-event message templating (v0.5.50+).
    #
    # All four fields are optional and default to None. When unset (the
    # historical case), each backend uses its built-in payload format —
    # so every existing project YAML keeps working with zero changes.
    #
    # When set, the dict's keys are sync action names ("push", "pull",
    # "sync", "delete") and the values are `str.format`-style template
    # strings (Slack / Discord / Teams) or nested dicts of format strings
    # (Generic — see below). Only the listed actions get templated; other
    # actions still use the built-in format. An action's template overrides
    # ONLY the message summary line; the rest of the rich-blocks / embed /
    # MessageCard structure (file list, context, facts) is preserved.
    #
    # Available placeholders (documented in docs/admin.md):
    #   {user} {machine} {project} {action}
    #   {n_files}  — len(event.files)
    #   {file_list} — comma-joined, capped at 10 with "and N more"
    #   {first_file} — event.files[0] if any, else empty string
    #   {timestamp} {snapshot_timestamp}
    #
    # If a template references an unknown placeholder, claude-mirror logs
    # a yellow info line and falls back to the built-in format for that
    # one event — a bad template never crashes a sync.
    #
    # Generic webhook templates are STRUCTURED: each value is itself a
    # dict mapping output-key to format-string. The rendered dict is
    # merged on top of the v1 envelope, so template fields override the
    # same-name envelope keys (use this to add `custom_field_1`, etc.).
    slack_template_format: Optional[dict[str, str]] = None
    discord_template_format: Optional[dict[str, str]] = None
    teams_template_format: Optional[dict[str, str]] = None
    webhook_template_format: Optional[dict[str, dict[str, str]]] = None
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

    # max_throttle_wait_seconds: hard cap on the shared backoff
    # coordinator's pause window when a backend signals
    # RATE_LIMIT_GLOBAL (HTTP 429 from Drive `userRateLimitExceeded`,
    # Dropbox `too_many_requests`, OneDrive 429, etc.). When any worker
    # hits a global throttle, every in-flight upload pauses for an
    # exponentially-growing window (initially 30s or the server-supplied
    # Retry-After value, multiplied by 1.5× on each escalation) capped
    # at this value.
    #
    # Default 600 (10 minutes) — long enough for the heaviest server
    # throttles to clear, short enough that a cron job which
    # accidentally hits a hard quota won't sit blocked all day. Lower
    # to e.g. 60 for cron-style runs that should fail fast and let the
    # next tick try again rather than holding open a long pause.
    max_throttle_wait_seconds: float = 600.0

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

    # Bandwidth throttling (v0.5.39+).
    #
    # When set, every upload path on this backend (Drive resumable-upload
    # chunk loop; Dropbox files_upload; OneDrive simple PUT and chunked
    # upload session; WebDAV PUT; SFTP put-file loop) consumes from a
    # token bucket sized at this rate before sending bytes. Files
    # smaller than the bucket capacity pass through with zero added
    # latency; bursts above the cap are paced down to the long-run rate.
    #
    # `null` (default) disables throttling — the no-op `NullBucket` is
    # used and callers see no overhead.
    #
    # In Tier 2 multi-backend setups, each mirror config carries its
    # own field, so a user can throttle Drive but leave SFTP unbounded
    # (or vice versa). Cap is per-backend, NOT per-process.
    #
    # Units are KILOBITS PER SECOND (1024 bits/s = 128 bytes/s) so the
    # value matches what users see in their ISP / NAS contracts.
    # 1024 kbps ≈ 128 KiB/sec ≈ 7.5 MiB/min.
    max_upload_kbps: Optional[int] = None

    # WebDAV streaming-upload threshold (v0.5.39+).
    #
    # Files at or above this size go through the chunked-PUT path
    # (request body is a generator yielding fixed-size blocks; peak
    # memory bounded to one block, not the whole file). Files below
    # the threshold use the simple in-memory PUT — keeps the hot path
    # for typical small markdown files unchanged.
    #
    # Default 4 MiB. WebDAV-only field; ignored by the other four
    # backends (each has its own native chunking story documented in
    # `docs/admin.md#upload-resume-behaviour-by-backend`).
    webdav_streaming_threshold_bytes: int = 4 * 1024 * 1024

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
        # Clamp max_throttle_wait_seconds to [0, 86400] — 0 disables the
        # coordinator's pause entirely (failures still classify as
        # RATE_LIMIT_GLOBAL but workers don't actually wait); 86400 (1
        # day) is an absurdly high upper bound to defeat hostile configs
        # that would otherwise pin workers indefinitely. Default 600s.
        try:
            self.max_throttle_wait_seconds = float(self.max_throttle_wait_seconds)
            if self.max_throttle_wait_seconds < 0:
                self.max_throttle_wait_seconds = 0.0
            if self.max_throttle_wait_seconds > 86400.0:
                self.max_throttle_wait_seconds = 86400.0
        except (TypeError, ValueError):
            self.max_throttle_wait_seconds = 600.0
        # Validate per-backend × per-action template dicts. Each dict's
        # keys MUST be sync-action names; for str-template backends the
        # values MUST be strings, for the Generic webhook the values MUST
        # be dicts of format strings. A typo'd action key surfaces here
        # with a clean error rather than silently being ignored later.
        _validate_template_dict(
            self.slack_template_format, "slack_template_format", str,
        )
        _validate_template_dict(
            self.discord_template_format, "discord_template_format", str,
        )
        _validate_template_dict(
            self.teams_template_format, "teams_template_format", str,
        )
        _validate_template_dict(
            self.webhook_template_format, "webhook_template_format", dict,
        )

        # Normalise + validate each *_routes field. Done up-front rather
        # than at first-event-fire so a typo in the YAML surfaces the
        # moment the project is loaded (init / status / push) instead
        # of silently swallowing every notification at runtime.
        self.slack_routes = _normalise_routes(self.slack_routes, "slack_routes")
        self.discord_routes = _normalise_routes(self.discord_routes, "discord_routes")
        self.teams_routes = _normalise_routes(self.teams_routes, "teams_routes")
        self.webhook_routes = _normalise_routes(self.webhook_routes, "webhook_routes")

    def iter_routes(self, backend: str) -> Iterable[dict[str, Any]]:
        """Yield the resolved route list for ``backend``.

        ``backend`` is one of ``"slack" | "discord" | "teams" | "webhook"``.

        Resolution rule:
          * If the explicit ``{backend}_routes`` list is set, yield each
            entry verbatim (already normalised to carry ``webhook_url``,
            ``on``, ``paths``). The legacy single-channel field is then
            ignored.
          * Otherwise, if the backend's legacy single-channel form is
            enabled (``{backend}_enabled`` and the URL non-empty), yield
            ONE pseudo-route with the default `on` (all four actions)
            and `paths` (`["**/*"]`). For the generic webhook this
            single pseudo-route also carries `extra_headers` so the
            dispatcher can attach them to the request.
          * Otherwise yield nothing.

        The legacy/list precedence is enforced at *dispatch* time by the
        caller — this helper just expresses the resolved routing tree.
        """
        b = backend.lower()
        if b == "slack":
            routes = self.slack_routes
            enabled = self.slack_enabled
            url = self.slack_webhook_url
        elif b == "discord":
            routes = self.discord_routes
            enabled = self.discord_enabled
            url = self.discord_webhook_url
        elif b == "teams":
            routes = self.teams_routes
            enabled = self.teams_enabled
            url = self.teams_webhook_url
        elif b == "webhook":
            routes = self.webhook_routes
            enabled = self.webhook_enabled
            url = self.webhook_url
        else:
            raise ValueError(f"unknown notification backend: {backend!r}")

        if routes:
            for r in routes:
                # Defensive: yield a fresh dict so caller mutation can't
                # leak back into the Config.
                yield {
                    "webhook_url": r["webhook_url"],
                    "on": list(r.get("on") or _DEFAULT_ROUTE_ACTIONS),
                    "paths": list(r.get("paths") or _DEFAULT_ROUTE_PATHS),
                }
            return

        if enabled and url:
            pseudo: dict[str, Any] = {
                "webhook_url": url,
                "on": list(_DEFAULT_ROUTE_ACTIONS),
                "paths": list(_DEFAULT_ROUTE_PATHS),
            }
            if b == "webhook" and self.webhook_extra_headers:
                # Generic envelope carries auth headers; preserve via the
                # pseudo-route so the dispatcher gets to set them on the
                # outgoing request without a special legacy code path.
                pseudo["extra_headers"] = dict(self.webhook_extra_headers)
            yield pseudo

    def has_legacy_routes_conflict(self, backend: str) -> bool:
        """Return True if BOTH legacy single-channel form AND list-form
        are configured for ``backend``. Used to emit a one-shot info
        line at engine startup ("ignoring slack_webhook_url because
        slack_routes is set") so a user mid-transition sees the override.
        """
        b = backend.lower()
        if b == "slack":
            return bool(self.slack_routes) and bool(self.slack_enabled and self.slack_webhook_url)
        if b == "discord":
            return bool(self.discord_routes) and bool(self.discord_enabled and self.discord_webhook_url)
        if b == "teams":
            return bool(self.teams_routes) and bool(self.teams_enabled and self.teams_webhook_url)
        if b == "webhook":
            return bool(self.webhook_routes) and bool(self.webhook_enabled and self.webhook_url)
        return False

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
        if self.backend == "sftp":
            return self.sftp_folder
        if self.backend == "ftp":
            return self.ftp_folder
        if self.backend == "s3":
            # S3 has no real folders; the project root is the configured
            # prefix (or the project name when the prefix is left blank).
            # Always returned with a trailing slash so prefix arithmetic
            # stays consistent.
            raw = (self.s3_prefix or "").strip().strip("/")
            if not raw:
                from pathlib import Path as _Path
                raw = _Path(self.project_path).name or "claude-mirror"
            return raw + "/"
        return self.drive_folder_id

    @property
    def subscription_id(self) -> str:
        safe = self.machine_name.replace(".", "-").replace(" ", "-").lower()
        return f"{self.pubsub_topic_id}-{safe}"

    @classmethod
    def load(cls, path: str, *, profile_override: str = "") -> Config:
        """Load a project config from `path`, applying any referenced profile.

        Profile resolution rules (since v0.5.49):

          1. If `profile_override` is set (the global `--profile NAME` flag
             on the CLI), load that profile and merge it in regardless of
             whether the YAML carries its own `profile:` field — the flag
             wins and acts as a one-shot override.
          2. Otherwise, if the module-level `_GLOBAL_PROFILE_OVERRIDE` was
             set by `set_global_profile_override` (the CLI does this from
             the global click-group invoke() handler before dispatching
             a subcommand), use that profile.
          3. Otherwise, if the YAML has a top-level `profile: NAME` field,
             load that profile and merge it in.
          4. Otherwise, no merging — the YAML loads as-is.

        Project YAML values override profile values for any field both
        define (see `apply_profile` for the precedence rule).
        """
        with open(path) as f:
            data = yaml.safe_load(f) or {}

        # Pull the per-YAML profile reference out before anything else so
        # `Config(**data)` doesn't trip on the unknown `profile` key.
        yaml_profile_name = data.pop("profile", None) or ""
        profile_name = (
            profile_override
            or _GLOBAL_PROFILE_OVERRIDE
            or yaml_profile_name
        )

        if profile_name:
            # Local import to avoid a config<->profiles cycle at import time.
            from .profiles import load_profile, apply_profile
            profile_data = load_profile(profile_name)
            # Drop description: it's comment-only, not a Config field.
            profile_data = {
                k: v for k, v in profile_data.items() if k != "description"
            }
            data = apply_profile(profile_data, data)
            # When a profile is in play, drop any keys the dataclass
            # doesn't know about — profile YAMLs may carry metadata
            # the Config dataclass intentionally doesn't model. Without
            # a profile, we keep the historical behaviour (TypeError on
            # unknown YAML keys) so hand-typo'd configs still surface.
            data = {
                k: v for k, v in data.items() if k in cls.__dataclass_fields__
            }

        return cls(**data)

    def save(
        self,
        path: str,
        *,
        profile: str = "",
        strip_fields: tuple[str, ...] = (),
    ) -> None:
        """Persist the config to `path` as YAML.

        Args:
            profile:      If non-empty, a top-level `profile: NAME` key is
                          written before the rest of the data. `Config.load`
                          will then merge the named profile in on every load.
            strip_fields: Tuple of field names to OMIT from the written
                          YAML. Used together with `profile` to keep
                          credential-bearing fields out of the project
                          YAML — the profile re-supplies them at load
                          time. Without strip_fields, the dataclass's
                          `__post_init__`-filled defaults would be
                          serialised and re-win over the profile (per
                          `apply_profile`'s truthy-wins rule), defeating
                          the whole point of `profile:` references.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        for k in strip_fields:
            data.pop(k, None)
        if profile:
            data = {"profile": profile, **data}
        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False)
