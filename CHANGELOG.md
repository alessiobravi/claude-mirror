# Changelog

All notable changes to claude-mirror.

---

## [0.5.10] — 2026-05-05

Initial public release.

### Features

**Backends (storage)**
- Google Drive (Pub/Sub-driven real-time notifications)
- Dropbox (long-poll notifications)
- OneDrive (Graph API + polling)
- WebDAV (PROPFIND polling; OwnCloud / Nextcloud / Apache mod_dav / NAS / Box.com)
- Multi-backend mirroring (Tier 2): one primary backend + N secondary mirrors per project, with per-mirror manifest state and pending-retry queue.
- All backends ship in the base install — `pipx install claude-mirror` is the only command needed regardless of which backend you choose.

**Sync engine**
- 3-way diff manifest (`local hash` × `synced hash` × `remote hash`) with content-addressed change detection.
- Conflict resolver with `[L]ocal / [D]rive / [E]ditor / [S]kip` interactive choices.
- `push --force-local` for skill-side merges that should treat local as authoritative.
- File-pattern allowlist + glob exclude patterns, applied uniformly to status/push/pull/sync/delete.
- Per-machine Pub/Sub subscriptions with `SIGHUP` hot-reload of config tree (`claude-mirror reload`).
- Pending-retry queue with classified error types (TRANSIENT / AUTH / QUOTA / PERMISSION / FILE_REJECTED) for graceful degradation under transient failures.

**Snapshots & disaster recovery**
- Auto-snapshot after every push/sync, stored on the remote storage (`_claude_mirror_snapshots/`).
- Two on-disk formats: `full` (one folder per snapshot, server-side `files.copy`) and `blobs` (content-addressed dedup with `_claude_mirror_blobs/`).
- `claude-mirror history PATH` — shows every snapshot containing a file, version-grouped by SHA-256.
- `claude-mirror inspect TIMESTAMP` — list snapshot contents with `--paths` filtering.
- `claude-mirror restore TIMESTAMP [PATH...]` — restore a snapshot (non-destructive `--output`, in-place when omitted, per-backend `--backend NAME` override).
- `claude-mirror forget` and `claude-mirror gc` for safe pruning (dry-run by default; require `--delete` flag and typed `YES` confirmation; `--yes` only for non-interactive use).
- `claude-mirror migrate-snapshots --to {blobs|full}` for in-place format conversion.

**Notifications**
- Cross-platform desktop notifications (macOS `osascript`, Linux `libnotify` via `plyer`).
- Per-project notification inbox (`.claude_mirror_inbox.jsonl`) atomic against concurrent writers.
- Optional Slack webhooks (`slack_enabled` / `slack_webhook_url` / `slack_channel`) with best-effort delivery.

**CLI**
- 25 commands covering init/auth/status/sync/push/pull/delete/watch/snapshots/restore/log/inbox/find-config/migrate-state/migrate-snapshots/check-update/update.
- `claude-mirror init --wizard` for interactive setup; non-wizard path supports the same fields via flags.
- `claude-mirror find-config` walks up the directory tree like `git`'s `.git/` discovery; lists every available config on a total miss.
- `claude-mirror update --apply` one-shot self-upgrade (executes `git pull` + `pipx install -e . --force` as separate list-form `subprocess` calls — never `shell=True`).
- `claude-mirror check-update` queries the GitHub API for the latest version (CDN-bypass for instant visibility after a push); cache-busting headers fall back to raw GitHub when the API is rate-limited.
- Live-progress UI on every long-running command; silence is opt-in via a future `--quiet` flag, never default.

**Installer**
- `claude-mirror-install` — interactive installer for the Claude Code skill, the PreToolUse hook, and the background watcher daemon (launchd on macOS, systemd on Linux).
- Honours `CLAUDE_CONFIG_DIR` so a parallel Claude install is targeted correctly.

**Security & robustness**
- Path-traversal guards on all remote-driven local writes.
- Token files written with `0600` mode + post-`chmod` belt-and-braces.
- Error-message redaction (`redact_error()`) strips Bearer tokens, basic-auth credentials in URLs, query-string secrets, and home-directory paths before any error reaches a manifest, log, Slack message, or `status --pending` output.
- WebDAV backend refuses `http://` URLs by default (transmits credentials in cleartext); `--webdav-insecure-http` flag + `webdav_insecure_http: true` config field opt in.
- `BackendError` drops the live `cause` traceback to avoid pinning credential tuples / response bodies in long-running watcher processes.
- AppleScript notification text escaped against injection.
- All HTTP traffic to first-party endpoints (`api.github.com`, `raw.githubusercontent.com`) over TLS with default cert verification.

**Performance**
- O(1) state lookup on pending-retry queue expansion (was O(N×P)).
- `claude-mirror history` walks path components instead of full BFS per snapshot — ~30× fewer Drive API calls on a 30-snapshot project.
- Blob deduplication with `threading.Lock`-guarded existence check; uploads remain parallel via `ThreadPoolExecutor`.

### Notes
- Python 3.11+, Linux + macOS (Windows untested).
- Config directory: `~/.config/claude_mirror/`. Per-project state: `.claude_mirror_manifest.json`, `.claude_mirror_inbox.jsonl`, `.claude_mirror_hash_cache.json` inside each project.
- Single install command for all backends: `pipx install claude-mirror`.
- License: GPL-3.0-or-later.
