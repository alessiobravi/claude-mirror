← Back to [README index](../README.md)

# CLI reference

Every `claude-mirror` subcommand, grouped by topic. For deeper walkthroughs see the linked pages.

## Full command list

```
claude-mirror init        [--wizard]
                        [--backend googledrive|dropbox|onedrive|webdav|sftp]
                        [--project PATH]
                        [--drive-folder-id ID] [--gcp-project-id ID] [--pubsub-topic-id ID]
                        [--credentials-file PATH]
                        [--dropbox-app-key KEY] [--dropbox-folder PATH]
                        [--onedrive-client-id ID] [--onedrive-folder PATH]
                        [--webdav-url URL] [--webdav-username USER] [--webdav-password PASS]
                        [--webdav-insecure-http]   # opt-in to plain http:// (NOT recommended — credentials in cleartext)
                        [--sftp-host HOST] [--sftp-port PORT] [--sftp-username USER]
                        [--sftp-key-file PATH] [--sftp-password PASS]
                        [--sftp-known-hosts-file PATH] [--sftp-strict-host-check/--no-sftp-strict-host-check]
                        [--sftp-folder PATH]
                        [--poll-interval SECS]
                        [--slack/--no-slack] [--slack-webhook-url URL] [--slack-channel CHAN]
                        [--token-file PATH] [--patterns GLOB ...] [--exclude GLOB ...] [--config PATH]
claude-mirror auth        [--check] [--config PATH]
claude-mirror status      [--short] [--config PATH]
claude-mirror status --pending                  [--config PATH]
claude-mirror status --by-backend               [--config PATH]   # Tier 2: per-file table with one column per backend
claude-mirror sync        [--config PATH]
claude-mirror push        [FILES...] [--force-local] [--config PATH]
claude-mirror pull        [FILES...] [--output PATH] [--config PATH]
claude-mirror diff        PATH [--context N] [--config PATH]
claude-mirror delete      FILES... [--local] [--config PATH]
claude-mirror watch       [--config PATH]
claude-mirror watch-all   [--config PATH ...]   (default: all configs in ~/.config/claude_mirror/)
claude-mirror reload
claude-mirror snapshots         [--config PATH]
claude-mirror inspect           TIMESTAMP [--paths GLOB] [--config PATH]
claude-mirror history           PATH [--since DATE/DURATION] [--until DATE/DURATION] [--config PATH]
claude-mirror snapshot-diff     TS1 TS2 [--all] [--paths GLOB] [--unified PATH] [--config PATH]
claude-mirror retry             [--backend NAME] [--dry-run] [--config PATH]
claude-mirror seed-mirror       --backend NAME [--dry-run] [--config PATH]   # populate a freshly-added mirror with files already on the primary
claude-mirror restore           TIMESTAMP [PATH ...] [--backend NAME] [--output PATH] [--dry-run/--no-dry-run] [--config PATH]
claude-mirror forget            TIMESTAMP... | --before DATE/DURATION | --keep-last N | --keep-days N
                              [--delete] [--yes] [--config PATH]   # dry-run by default; --delete to actually delete
claude-mirror prune             [--keep-last N] [--keep-daily N] [--keep-monthly N] [--keep-yearly N]
                              [--delete] [--yes] [--config PATH]   # dry-run by default; reads keep_* from config
claude-mirror gc                [--backend NAME] [--delete] [--yes] [--config PATH]   # dry-run by default; --delete to actually delete; --backend targets a specific mirror (Tier 2)
claude-mirror doctor            [--backend NAME] [--config PATH]   # end-to-end self-test: config + credentials + connectivity + project + manifest
claude-mirror migrate-snapshots --to {blobs|full} [--dry-run] [--keep-source] [--no-update-config] [--config PATH]
claude-mirror log               [--limit N] [--config PATH]
claude-mirror inbox       [--config PATH]
claude-mirror find-config [PATH]
claude-mirror test-notify
claude-mirror check-update
claude-mirror update            [--apply] [--yes]   # one-shot upgrade: dry-run by default, --apply to execute
claude-mirror completion        {bash|zsh|fish}   # emit shell tab-completion source — eval into your shell rc
claude-mirror status --pending  [--config PATH]   # Tier 2: show files with non-ok mirror state

claude-mirror-install     [--uninstall]
```

---

## Setup

### `init`

Create a new project config in `~/.config/claude_mirror/<project>.yaml`. Pass `--wizard` for an interactive walkthrough that prompts for the backend and its required fields, or pass `--backend NAME` plus the backend-specific flags for a non-interactive setup. Auto-derives the token file path from the credentials file (Google Drive) or project name (other backends).

See [README — Step 1: Initialize](../README.md#step-1-initialize) for the wizard transcripts and the full flag table, and the per-backend pages for what each backend needs:
- [backends/google-drive.md](backends/google-drive.md)
- [backends/dropbox.md](backends/dropbox.md)
- [backends/onedrive.md](backends/onedrive.md)
- [backends/webdav.md](backends/webdav.md)
- [backends/sftp.md](backends/sftp.md)

### `auth`

Authenticate the configured backend. Google Drive opens a browser; Dropbox prints an authorization URL and reads the code from stdin; OneDrive prints a device code; WebDAV validates the URL/username/password in-process; SFTP validates the SSH key/password against the server. Tokens are written to the project's `token_file` with `chmod 0600`. `--check` only verifies the existing token (does not start a fresh login).

### `claude-mirror-install`

Install (or `--uninstall`) the auto-start service for the watcher daemon. Detects platform automatically — writes a `launchd` plist on macOS or a `systemd --user` unit on Linux — and loads it immediately. After running this, `claude-mirror watch-all` runs in the background and restarts on login / on failure.

See [admin.md — Auto-start the watcher](admin.md#auto-start-the-watcher) for the manual setup recipes.

### `completion`

Emit shell tab-completion source for the named shell. `eval "$(claude-mirror completion zsh)"` (or `bash` / `fish`) in your shell rc enables tab-completion for all subcommands and their flags.

---

## Daily

### `status`

Show what has changed since the last sync — per-file table plus a color-coded summary. Use `--short` for a one-line view (no table) — what the Claude Code skill uses internally. Use `--pending` (Tier 2) to list files with non-ok state on any mirror. Use `--by-backend` (Tier 2) for a per-file table with one column per configured backend.

See [README — Check sync status](../README.md#check-sync-status).

### `push`

Upload locally-changed files to the remote, create a snapshot, and publish a notification to all collaborators. With Tier 2 mirrors, uploads to every backend in parallel; the run as a whole succeeds even if one mirror has transient errors. Pass file arguments to push only those specific files.

### `pull`

Download remote-ahead files and update the local manifest. Pass file arguments to pull only specific files. Use `--output PATH` to download to a separate directory without touching local files or the manifest — useful for previewing remote changes before deciding to merge.

### `sync`

Bidirectional in one command: pushes local-ahead files, pulls remote-ahead files, and prompts interactively for conflicts. Creates a snapshot and notifies collaborators after completion.

See [conflict-resolution.md](conflict-resolution.md) for what happens at the conflict prompt.

### `diff`

Print a colorized unified diff (remote → local) for one file, in standard `git diff` style. Green `+` lines are what would be pushed; red `-` lines are what would be pulled. Adjust context with `--context N` (default 3, max 200). Read-only — never modifies local or remote state.

### `delete`

Delete files from the remote (and locally with `--local`). Used to remove files that are no longer relevant or were committed by mistake.

### `watch`

Run the notification listener for one project in the foreground. Used for ad-hoc monitoring or debugging — for production use, prefer `watch-all` via `claude-mirror-install`.

### `watch-all`

Watch every project in a single process — auto-discovers all configs in `~/.config/claude_mirror/` and starts one notification thread per project, picking the right notifier per backend (Pub/Sub for Google Drive, long-polling for Dropbox, periodic polling for OneDrive / WebDAV / SFTP). Pass `--config PATH` repeatedly to watch only a subset.

### `reload`

Send `SIGHUP` to the running `watch-all` process so it re-scans `~/.config/claude_mirror/` and starts watcher threads for any new configs without restarting.

### `inbox`

Print pending notifications from collaborators (received by the background watcher). Used by the `PreToolUse` hook in Claude Code to surface remote activity inline before each tool invocation.

### `log`

Show the cross-machine sync activity log: who pushed/pulled/synced/deleted what and when. Stored on the remote and shared with all collaborators. `--limit N` caps the number of entries.

---

## Snapshots

### `snapshots`

List every available snapshot for the project, with a `Format` column distinguishing `blobs` from `full` snapshots. Both formats are listed together.

### `inspect`

Show the manifest of a snapshot — every path with its SHA-256 (blobs format) or size (full format) — without downloading any file bodies. Filter with `--paths GLOB`. Use this to confirm a file exists at the version you want before running `restore`.

### `history`

Scan every snapshot's manifest and report which ones contain `PATH`. For `blobs` snapshots, the SHA-256 lets it label distinct versions (v1, v2, ...) so you can spot when the file actually changed. The version timeline highlights transitions in bold green. Pass `--since DATE/DURATION` and/or `--until DATE/DURATION` (inclusive on both bounds, independent, both optional) to restrict the scan to a window — accepts ISO dates (`2026-04-15`), ISO datetimes (`2026-04-15T10:00:00Z`), or relative durations (`30d` / `2w` / `3m` / `1y`). A `--since` later than `--until` exits 1 with a red error. See [admin.md — Filtering history by date](admin.md#filtering-history-by-date).

### `snapshot-diff`

Show what changed between two snapshots. `TS1` is the "from" snapshot; `TS2` is the "to" snapshot — order matters. Pass the literal keyword `latest` for either side to use the most recent snapshot. Default output classifies each file as `added` / `removed` / `modified` (omitting `unchanged` rows; pass `--all` to include them). For `modified` rows the `Changes` column shows `+N -M` line counts via difflib (`binary` if either body is non-UTF-8). `--paths PATTERN` filters the table by an fnmatch glob. `--unified PATH` switches to standard `diff -u` output for one specific file (composes with `less` / `delta` / shell redirection). Both `blobs` and `full` snapshots accepted, including a mix of formats. See [admin.md — Comparing snapshots](admin.md#comparing-snapshots).

### `restore`

Restore one or more files (or the whole snapshot) from `TIMESTAMP`. With Tier 2, falls back to mirrors in `mirror_config_paths` order if the snapshot is missing on the primary; pass `--backend NAME` to force a specific source. Use `--output PATH` to restore to a safe inspection directory instead of overwriting the project. Pass `--dry-run` to preview every file the restore would write (Path / Action / Source backend / Size) without touching local disk; the summary ends with `Run without --dry-run to apply.`. Default is `--no-dry-run` (existing behaviour). Auto-detects the snapshot's format. See [admin.md — Previewing a restore](admin.md#previewing-a-restore).

### `forget`

Delete snapshots matching one selector: positional `TIMESTAMP...`, `--before DATE/DURATION` (`30d` / `2w` / `3m` / `1y` accepted), `--keep-last N`, or `--keep-days N`. Dry-run by default. Pass `--delete` plus a typed `YES` confirmation to actually delete (or `--yes` to skip the prompt for cron / CI).

### `prune`

Apply the YAML retention policy (`keep_last`, `keep_daily`, `keep_monthly`, `keep_yearly`) by hand. Same dry-run / `--delete` / `--yes` contract as `forget`. Any `--keep-*` flag overrides the corresponding config field for that one run only.

### `migrate-snapshots`

Convert every snapshot in-place to/from the `blobs` or `full` format. `--to blobs` or `--to full` is required. Idempotent and atomic per snapshot. By default the YAML is updated to the new format on success; pass `--no-update-config` to leave it untouched. Pass `--keep-source` to keep originals.

See [admin.md — Snapshots and disaster recovery](admin.md#snapshots-and-disaster-recovery) for the full snapshot lifecycle.

---

## Tier 2 (multi-backend mirroring)

### `status --pending`

Lists files with non-ok mirror state (File / Backend / State / Last error) AND any mirror with unseeded files. The trailing hint suggests `claude-mirror retry` or `claude-mirror seed-mirror` as appropriate.

### `status --by-backend`

Full per-file table with one column per configured backend (primary first, mirrors in `mirror_config_paths` order). Each cell shows that backend's state for the file (`✓ ok` / `⚠ pending` / `✗ failed` / `⊘ unseeded` / `· absent`) plus a footer summary line per backend.

### `retry`

Re-attempt mirrors stuck in `pending_retry`. Pass `--backend NAME` to retry one specific mirror, `--dry-run` to preview without uploading. Runs the same upload path as `push`, with the same error classification.

### `seed-mirror`

Populate a newly-added mirror with files that already exist on the primary. Walks the manifest, finds every file with no recorded state on `--backend NAME`, and uploads each one to that mirror only — the primary is never touched. Idempotent. Drift-safe: files whose local content has diverged from the manifest are skipped with a warning. Use `--dry-run` to preview.

See [README — Multi-backend mirroring (Tier 2)](../README.md#multi-backend-mirroring-tier-2) for the full Tier 2 walkthrough.

---

## Maintenance

### `gc`

Delete blobs no longer referenced by any manifest (only relevant for `blobs`-format snapshots). Dry-run by default. Pass `--delete` plus a typed `YES` confirmation (or `--yes` to skip the prompt) to actually delete. Pass `--backend NAME` to gc a specific mirror's blob store (Tier 2). Refuses to run if no manifests exist on remote.

### `doctor`

End-to-end self-test of a project's configuration: config file parses, credentials / token files present, backend connectivity, `project_path` exists, manifest is valid. Each check repeats per backend including Tier 2 mirrors. Exits 0 on all-pass, 1 on any failure. Pass `--config PATH` to point at a specific config (auto-detected from cwd otherwise) or `--backend NAME` to limit checks to one backend (`googledrive` / `dropbox` / `onedrive` / `webdav` / `sftp`).

See [admin.md#doctor](admin.md#doctor) for the full check matrix, sample output, and fix-hint interpretation.

### `find-config`

Print the config file path that matches the current working directory (or `PATH` if given). Searches all `~/.config/claude_mirror/*.yaml` files for one whose `project_path` matches, falling back to `default.yaml` if none match. The Claude Code skill uses this internally to detect the active project.

See [README — find-config](../README.md#find-config).

### `test-notify`

Send a test desktop notification (and a test Slack message, if Slack is configured) to verify the notification pipeline.

### `check-update`

Check whether a newer claude-mirror release is available on PyPI / GitHub. Uses the GitHub API as the primary source (with raw.githubusercontent.com as a fallback). Caches the result for a short window to avoid hammering the server.

### `update`

One-shot in-place upgrade. Dry-run by default — prints the pip command that would run and the version it would install. Pass `--apply` to execute the upgrade; `--yes` skips the confirmation prompt. Useful for keeping a long-running watcher up to date without leaving the terminal.

---

## Misc

### `claude-mirror-install`

(Standalone binary, not a subcommand.) Install or uninstall the auto-start service for `watch-all`. See the [Setup](#setup) section above and [admin.md — Auto-start the watcher](admin.md#auto-start-the-watcher).

---

## Config fields (selected)

Most config fields are documented inline at the points where they affect behaviour (per-backend setup pages, retention in [admin.md](admin.md#auto-pruning-by-retention-policy), etc.). The fields that don't fit any single command's scope live here:

### `max_upload_kbps`

Per-backend upload bandwidth cap, in **kilobits per second** (1 kbps = 128 bytes/sec). Default `null` (disabled — every upload runs uncapped). When set, every upload path on that backend (Drive resumable-chunk loop, Dropbox `files_upload`, OneDrive simple PUT and chunked upload session, WebDAV PUT, SFTP per-block writes) consumes from a token-bucket limiter before sending bytes.

```yaml
# in your project YAML
max_upload_kbps: 1024     # ≈ 128 KiB/sec, ≈ 7.5 MiB/min
```

In Tier 2 multi-backend setups, every mirror config has its own `max_upload_kbps` field — throttle Drive but leave SFTP unbounded, or vice versa, by setting the field on one config and leaving it `null` on the other. See [admin.md — Performance and bandwidth control](admin.md#performance-and-bandwidth-control) for the full design rationale and the per-backend resume-behaviour table.

### `webdav_streaming_threshold_bytes`

WebDAV-only field. Files at or above this size go through a streaming chunked-PUT path (request body is a generator yielding 1 MiB blocks; peak memory bounded to one block, NOT the whole file). Smaller files use the historic in-memory PUT path so the hot path for typical markdown content is unchanged. Default `4194304` (4 MiB).

```yaml
# in your project YAML — files >= this size stream
webdav_streaming_threshold_bytes: 4194304
```

Ignored by the four other backends (each has its own native chunking story documented in [admin.md — Upload resume behaviour by backend](admin.md#upload-resume-behaviour-by-backend)).

---

## See also

- [admin.md](admin.md) — snapshots, retention, watcher daemon.
- [conflict-resolution.md](conflict-resolution.md) — what `sync` does when both sides changed.
- [README — Daily usage](../README.md#part-4--daily-usage) — narrative walkthrough of the daily commands.
- [README — Slack notifications](../README.md#slack-notifications) — Slack-specific config and webhook setup.
