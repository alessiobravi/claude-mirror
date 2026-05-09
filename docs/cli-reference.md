← Back to [README index](../README.md)

# CLI reference

Every `claude-mirror` subcommand, grouped by topic. For deeper walkthroughs see the linked pages.

## Full command list

The global `--profile NAME` flag (since v0.5.49) goes on the `claude-mirror` command itself, BEFORE the subcommand: `claude-mirror --profile work push`. It applies the named credentials profile from `~/.config/claude_mirror/profiles/NAME.yaml` to the project config at load time. See [profiles.md](profiles.md) for the full walkthrough.

```
claude-mirror [--profile NAME] <subcommand> ...   # global flag (since v0.5.49)

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
                        [--auto-pubsub-setup]      # googledrive only: auto-create Pub/Sub topic + per-machine subscription + IAM grant after auth
claude-mirror auth        [--check] [--config PATH]
claude-mirror status      [--short] [--config PATH]
claude-mirror status --pending                  [--config PATH]
claude-mirror status --by-backend               [--config PATH]   # Tier 2: per-file table with one column per backend
claude-mirror sync        [--no-prompt --strategy {keep-local|keep-remote}] [--config PATH]
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
claude-mirror seed-mirror       [--backend NAME] [--dry-run] [--config PATH]   # populate a freshly-added mirror with files already on the primary; auto-detects when exactly one mirror is unseeded
claude-mirror restore           TIMESTAMP [PATH ...] [--backend NAME] [--output PATH] [--dry-run/--no-dry-run] [--config PATH]
claude-mirror forget            TIMESTAMP... | --before DATE/DURATION | --keep-last N | --keep-days N
                              [--delete] [--yes] [--config PATH]   # dry-run by default; --delete to actually delete
claude-mirror prune             [--keep-last N] [--keep-daily N] [--keep-monthly N] [--keep-yearly N]
                              [--delete] [--yes] [--config PATH]   # dry-run by default; reads keep_* from config
claude-mirror gc                [--backend NAME] [--delete] [--yes] [--config PATH]   # dry-run by default; --delete to actually delete; --backend targets a specific mirror (Tier 2)
claude-mirror doctor            [--backend NAME] [--config PATH]   # end-to-end self-test: config + credentials + connectivity + project + manifest (+ deep checks under --backend googledrive or --backend dropbox)
claude-mirror migrate-snapshots --to {blobs|full} [--dry-run] [--keep-source] [--no-update-config] [--config PATH]
claude-mirror log               [--limit N] [--config PATH]
claude-mirror inbox       [--config PATH]
claude-mirror find-config [PATH]
claude-mirror profile list
claude-mirror profile show       NAME
claude-mirror profile create     NAME --backend BACKEND [--description TEXT] [--force]
claude-mirror profile delete     NAME [--delete] [--yes]   # dry-run by default; --delete to actually delete
claude-mirror test-notify
claude-mirror check-update
claude-mirror update            [--apply] [--yes]   # one-shot upgrade: dry-run by default, --apply to execute
claude-mirror completion        {bash|zsh|fish}   # emit shell tab-completion source — eval into your shell rc
claude-mirror status --pending  [--config PATH]   # Tier 2: show files with non-ok mirror state

claude-mirror-install     [--uninstall]
```

---

## JSON output (`--json`)

Five read-only commands accept `--json`, which makes them emit a single flat JSON document to stdout instead of the Rich table and suppress every banner / progress / colour. Designed for piping into `jq`, scripting from Claude Code skills, and automation that needs structured state without screen-scraping ANSI output.

Supported commands:
- `claude-mirror status --json`
- `claude-mirror history PATH --json`
- `claude-mirror inbox --json`
- `claude-mirror log --json`
- `claude-mirror snapshots --json`

Exit codes match the non-JSON path: `0` on success (an empty inbox is a success and produces `{... "result": {"events": []}}`), `1` on any actual error (config not found, network failure, malformed snapshot). On error, a JSON error envelope is written to **stderr** rather than the success document on stdout, so a script can `2>/dev/null` the failure stream without losing structured info.

### Top-level envelope (v1 schema)

Every successful response is a single JSON object shaped like:

```json
{
  "version": 1,
  "command": "<subcommand>",
  "result": { /* command-specific payload */ }
}
```

- `version` is an integer, currently `1`. A future breaking change will bump to `2`; both shapes will be supported during the transition.
- `command` matches the CLI subcommand name (`status`, `history`, `inbox`, `log`, `snapshots`).
- `result` is the per-command payload (see below). The schema is flat — version-tagged at the top level rather than per-field, so consumers gate on `doc["version"] == 1` once.

Errors are written to stderr in the same envelope shape:

```json
{
  "version": 1,
  "command": "<subcommand>",
  "error": {
    "type": "FileNotFoundError",
    "message": "config not found: /home/user/.config/claude_mirror/foo.yaml"
  }
}
```

`error.type` is the Python exception class name; `error.message` is the human-readable detail.

Output is formatted with `indent=2`, key order preserved, UTF-8 in strings (no `«` escaping for unicode home paths). Pipe through `python3 -c 'import json,sys; json.load(sys.stdin)'` to sanity-check that any output is a valid JSON document.

### `status --json`

```json
{
  "version": 1,
  "command": "status",
  "result": {
    "config_path": "/home/alice/.config/claude_mirror/notes.yaml",
    "summary": {
      "in_sync": 5,
      "local_ahead": 1,
      "remote_ahead": 0,
      "conflict": 0,
      "new_local": 0,
      "new_remote": 0,
      "deleted_local": 0
    },
    "files": [
      {
        "path": "CLAUDE.md",
        "status": "in_sync",
        "local_hash": "8b1a9953c4611296a827abf8c47804d7",
        "remote_hash": "8b1a9953c4611296a827abf8c47804d7",
        "manifest_hash": "8b1a9953c4611296a827abf8c47804d7"
      },
      {
        "path": "memory/notes.md",
        "status": "local_ahead",
        "local_hash": "ad0234829205b9033196ba818f7a872b",
        "remote_hash": "5d41402abc4b2a76b9719d911017c592",
        "manifest_hash": "5d41402abc4b2a76b9719d911017c592"
      }
    ]
  }
}
```

`status` values are the storage-agnostic aliases: `in_sync`, `local_ahead`, `remote_ahead`, `conflict`, `new_local`, `new_remote`, `deleted_local`. The internal Status enum's `drive_ahead`/`new_drive` legacy values are NOT exposed in the JSON; consumers see `remote_ahead` / `new_remote`.

`--watch` is incompatible with `--json` (a streaming live region is not a single JSON document) and is ignored when `--json` is set. `--pending` and `--by-backend` are mutually exclusive; passing both with `--json` produces a JSON error envelope on stderr and exits 1.

Hash fields are nullable: `local_hash` is null when the file is remote-only, `remote_hash` is null when the file hasn't been pushed yet, `manifest_hash` is null when the manifest has no record of the file.

### `history PATH --json`

```json
{
  "version": 1,
  "command": "history",
  "result": {
    "path": "memory/notes.md",
    "versions": [
      {
        "timestamp": "2026-05-08T10-00-00Z",
        "hash": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "size": null,
        "version": "v2",
        "format": "blobs"
      },
      {
        "timestamp": "2026-05-07T10-00-00Z",
        "hash": "cafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00dcafef00d",
        "size": null,
        "version": "v1",
        "format": "blobs"
      }
    ],
    "distinct_versions": 2,
    "total_appearances": 2
  }
}
```

`versions` is newest-first. Each entry's `version` label is a stable `v1`, `v2`, ... assigned by SHA-256 — consecutive identical hashes share the same label. For `full`-format snapshots the hash is `null` (we'd have to download every file body to compute it) and the `version` field is `"?"`.

### `inbox --json`

```json
{
  "version": 1,
  "command": "inbox",
  "result": {
    "events": [
      {
        "timestamp": "2026-05-08T10:00:00Z",
        "user": "alice",
        "machine": "laptop",
        "action": "push",
        "files": ["memory/notes.md"],
        "project": "research"
      }
    ]
  }
}
```

The whole document is one JSON object — NOT JSONL — even though the inbox is stored as JSONL on disk. The inbox is cleared after a successful read (same semantics as the Rich path). On an empty inbox the result is `{"events": []}` with exit 0 — empty is success, not an error. Unlike the Rich path (which silently exits 0 if the config can't be loaded so PreToolUse hooks stay quiet), the `--json` path surfaces config errors on stderr so scripts can act on them.

### `log --json`

```json
{
  "version": 1,
  "command": "log",
  "result": [
    {
      "timestamp": "2026-05-08T11:00:00Z",
      "user": "bob",
      "machine": "desktop",
      "action": "push",
      "files": ["b.md", "c.md"],
      "project": "research",
      "snapshot_timestamp": null
    },
    {
      "timestamp": "2026-05-07T10:00:00Z",
      "user": "alice",
      "machine": "laptop",
      "action": "push",
      "files": ["a.md"],
      "project": "research",
      "snapshot_timestamp": null
    }
  ]
}
```

Newest-first, capped by `--limit` (default 20). `snapshot_timestamp` is reserved by the v1 schema; current `SyncEvent` records don't track which snapshot a push generated, so it's always `null`. A future version that threads snapshot timestamps through to the activity log will populate this field — consumers should treat `null` as "unknown" rather than "no snapshot".

### `snapshots --json`

```json
{
  "version": 1,
  "command": "snapshots",
  "result": [
    {
      "timestamp": "2026-05-08T10-00-00Z",
      "format": "blobs",
      "file_count": 12,
      "size_bytes": null,
      "source_backend": "primary"
    },
    {
      "timestamp": "2026-05-07T09-00-00Z",
      "format": "full",
      "file_count": 10,
      "size_bytes": null,
      "source_backend": "primary"
    }
  ]
}
```

Newest-first. `size_bytes` is `null` when not recorded (full-format snapshots and older blobs manifests don't track end-to-end byte totals). `source_backend` is `"primary"` today — `snapshots` lists from the primary backend; in a future release it may report the actual backend name when listing per-mirror.

### Common piping recipes

```bash
# What's out of sync?
claude-mirror status --json | jq '.result.files[] | select(.status != "in_sync")'

# Count snapshots
claude-mirror snapshots --json | jq '.result | length'

# Latest pushed file per user
claude-mirror log --json | jq '.result | group_by(.user) | map({user: .[0].user, latest: .[0].timestamp})'

# All distinct versions of a file
claude-mirror history memory/notes.md --json | jq '.result.versions[] | {version, timestamp}'

# Drain inbox into a script
claude-mirror inbox --json | jq -r '.result.events[] | "[\(.timestamp)] \(.user)@\(.machine): \(.action) \(.files | join(","))"'
```

---

## Setup

### `init`

Create a new project config in `~/.config/claude_mirror/<project>.yaml`. Pass `--wizard` for an interactive walkthrough that prompts for the backend and its required fields, or pass `--backend NAME` plus the backend-specific flags for a non-interactive setup. Auto-derives the token file path from the credentials file (Google Drive) or project name (other backends).

Combine with the global `--profile NAME` flag (since v0.5.49) — `claude-mirror --profile work init --wizard --backend googledrive` — to inherit credential-bearing fields from a named profile. The wizard skips every credentials prompt the profile already supplies, and the resulting project YAML is written with `profile: work` at the top so the same inheritance applies on every later command. See [profiles.md](profiles.md).

`--auto-pubsub-setup` (Drive only, since v0.5.47): after the post-auth smoke test passes, idempotently create the Pub/Sub topic, the per-machine subscription, and the IAM grant for Drive's push-notification service account (`apps-storage-noreply@google.com` -> `roles/pubsub.publisher`) on the topic. Skipped silently if the Pub/Sub OAuth scope wasn't granted at auth time, and on every non-googledrive backend. See [backends/google-drive.md](backends/google-drive.md#auto-create-pubsub-topic--subscription--iam-grant---auto-pubsub-setup-since-v0547) for sample output and edge cases.

See [README — Step 1: Initialize](../README.md#step-1-initialize) for the wizard transcripts and the full flag table, and the per-backend pages for what each backend needs:
- [backends/google-drive.md](backends/google-drive.md)
- [backends/dropbox.md](backends/dropbox.md)
- [backends/onedrive.md](backends/onedrive.md)
- [backends/webdav.md](backends/webdav.md)
- [backends/sftp.md](backends/sftp.md)

### `auth`

Authenticate the configured backend. Google Drive opens a browser; Dropbox prints an authorization URL and reads the code from stdin; OneDrive prints a device code; WebDAV validates the URL/username/password in-process; SFTP validates the SSH key/password against the server. Tokens are written to the project's `token_file` with `chmod 0600`. `--check` only verifies the existing token (does not start a fresh login).

Combines with the global `--profile NAME` flag — `claude-mirror --profile work auth` writes the OAuth token to the path declared on the profile rather than the project YAML, which is exactly what you want when several projects share the same profile (one OAuth flow → one token reused everywhere).

### `claude-mirror-install`

Install (or `--uninstall`) the auto-start service for the watcher daemon. Detects platform automatically — writes a `launchd` plist on macOS or a `systemd --user` unit on Linux — and loads it immediately. After running this, `claude-mirror watch-all` runs in the background and restarts on login / on failure.

See [admin.md — Auto-start the watcher](admin.md#auto-start-the-watcher) for the manual setup recipes.

### `completion`

Emit shell tab-completion source for the named shell. `eval "$(claude-mirror completion zsh)"` (or `bash` / `fish`) in your shell rc enables tab-completion for all subcommands and their flags.

Since v0.5.50: `--backend` value list is enumerated dynamically at completion time via the hidden `_list-backends` command, so future backend additions automatically appear without re-sourcing the completion.

---

## Daily

### `status`

Show what has changed since the last sync — per-file table plus a color-coded summary. Use `--short` for a one-line view (no table) — what the Claude Code skill uses internally. Use `--pending` (Tier 2) to list files with non-ok state on any mirror. Use `--by-backend` (Tier 2) for a per-file table with one column per configured backend.

See [README — Check sync status](../README.md#check-sync-status).

### `push`

Upload locally-changed files to the remote, create a snapshot, and publish a notification to all collaborators. With Tier 2 mirrors, uploads to every backend in parallel; the run as a whole succeeds even if one mirror has transient errors. Pass file arguments to push only those specific files. Shows live ETA + transfer rate during the upload phase — see [admin.md "Transfer progress"](admin.md#transfer-progress-live-eta--bytessec).

### `pull`

Download remote-ahead files and update the local manifest. Pass file arguments to pull only specific files. Use `--output PATH` to download to a separate directory without touching local files or the manifest — useful for previewing remote changes before deciding to merge. Shows live ETA + transfer rate during the download phase — see [admin.md "Transfer progress"](admin.md#transfer-progress-live-eta--bytessec).

### `sync`

Bidirectional in one command: pushes local-ahead files, pulls remote-ahead files, and prompts interactively for conflicts. Creates a snapshot and notifies collaborators after completion. Shows live ETA + transfer rate during the upload and download phases — see [admin.md "Transfer progress"](admin.md#transfer-progress-live-eta--bytessec).

See [conflict-resolution.md](conflict-resolution.md) for what happens at the conflict prompt.

Flags:

| Flag | Default | Effect |
|---|---|---|
| `--config PATH` | auto-detected from cwd | Path to a specific config YAML when more than one project lives under `~/.config/claude_mirror/`. |
| `--no-prompt` | off | Resolve conflicts non-interactively. **Requires `--strategy`.** Designed for cron / launchd / systemd unattended runs where no TTY is available. |
| `--strategy {keep-local,keep-remote}` | none | Conflict-resolution strategy when `--no-prompt` is set. `keep-local` overwrites the remote with the local content; `keep-remote` overwrites the local file with the remote content. Required when `--no-prompt` is set; ignored otherwise (with a yellow info line). |

#### Cron / unattended use

The interactive flow blocks on `click.prompt` when both sides of a file have changed since the last sync. Under cron / launchd / systemd this hangs forever (or fails immediately, depending on stdin handling), so `claude-mirror sync` running unattended needs an explicit conflict-resolution strategy:

```bash
claude-mirror sync --no-prompt --strategy keep-local    # cron-friendly: local always wins
claude-mirror sync --no-prompt --strategy keep-remote   # cron-friendly: remote always wins
```

Output is one yellow line per auto-resolved conflict plus one trailing summary line — designed for cron mail and `journalctl` consumers:

```
⚠  CLAUDE.md: auto-resolved (keep-local)
⚠  notes/todo.md: auto-resolved (keep-local)
Summary: 12 in sync, 3 pushed, 2 pulled, 2 conflicts auto-resolved (keep-local).
```

Every auto-resolution is appended to `_sync_log.json` on the remote with a `auto_resolved_files: [{path, strategy}]` audit trail, so a later interactive operator can spot exactly which files were overwritten by the cron flow.

`--no-prompt` without `--strategy` exits 1 immediately with the message:

```
--no-prompt requires --strategy. Choices: keep-local, keep-remote.
```

`--strategy` without `--no-prompt` prints `--strategy ignored without --no-prompt` and falls back to the interactive flow.

If you run `claude-mirror sync` (no flags) and stdin is not a TTY (cron / launchd / systemd), the command fails fast at entry with a hint pointing at `--no-prompt --strategy` rather than hanging on a prompt nobody can answer.

Sample crontab entries are in [admin.md](admin.md#unattended-sync-via-cron). For the interactive prompt menu, see [conflict-resolution.md](conflict-resolution.md).

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

Populate a newly-added mirror with files that already exist on the primary. Walks the manifest, finds every file with no recorded state on `--backend NAME`, and uploads each one to that mirror only — the primary is never touched. Idempotent. Drift-safe: files whose local content has diverged from the manifest are skipped with a warning. Use `--dry-run` to preview. Shows live ETA + transfer rate during the seed upload — see [admin.md "Transfer progress"](admin.md#transfer-progress-live-eta--bytessec).

`--backend` is optional: when omitted, seed-mirror auto-detects the candidate when exactly one configured mirror has unseeded files. If zero mirrors are unseeded it exits cleanly with "Nothing to seed"; if more than one is unseeded it prints the candidate names and asks you to specify `--backend NAME` explicitly.

See [README — Multi-backend mirroring (Tier 2)](../README.md#multi-backend-mirroring-tier-2) for the full Tier 2 walkthrough.

---

## Maintenance

### `gc`

Delete blobs no longer referenced by any manifest (only relevant for `blobs`-format snapshots). Dry-run by default. Pass `--delete` plus a typed `YES` confirmation (or `--yes` to skip the prompt) to actually delete. Pass `--backend NAME` to gc a specific mirror's blob store (Tier 2). Refuses to run if no manifests exist on remote.

### `doctor`

End-to-end self-test of a project's configuration: config file parses, credentials / token files present, backend connectivity, `project_path` exists, manifest is valid. Each check repeats per backend including Tier 2 mirrors. Exits 0 on all-pass, 1 on any failure. Pass `--config PATH` to point at a specific config (auto-detected from cwd otherwise) or `--backend NAME` to limit checks to one backend (`googledrive` / `dropbox` / `onedrive` / `webdav` / `sftp`).

`--backend googledrive` additionally runs six Drive-specific deep checks beyond the generic per-backend loop: OAuth scope inventory (Drive required, Pub/Sub optional), Drive API enabled, Pub/Sub API enabled, Pub/Sub topic exists, per-machine subscription exists, and the IAM grant for Drive's service account on the topic. The IAM grant is the highest-value check — about 70% of self-serve Drive setups miss it, which silently breaks real-time notifications across machines. See [admin.md#drive-deep-checks](admin.md#drive-deep-checks) for the full deep-check matrix and [backends/google-drive.md#diagnosing-setup-problems](backends/google-drive.md#diagnosing-setup-problems) for sample output.

`--backend dropbox` additionally runs six Dropbox-specific deep checks beyond the generic per-backend loop: token JSON shape (`access_token` or `refresh_token` present), `dropbox_app_key` format sanity, account smoke test (`users_get_current_account`), granted-scope inspection (`files.content.read` + `files.content.write` for PKCE tokens; legacy tokens skip with an info line), folder access (`files_list_folder` against the configured `dropbox_folder`), and an account-type / team-status info line (team admins can disable third-party app access, silently breaking sync). Auth failures bucket into a single `Dropbox auth failed` line. See [admin.md#dropbox-deep-checks](admin.md#dropbox-deep-checks) for the full deep-check matrix and [backends/dropbox.md#diagnosing-setup-problems](backends/dropbox.md#diagnosing-setup-problems) for sample output.

`--backend onedrive` additionally runs OneDrive-specific deep checks beyond the generic per-backend loop: MSAL token cache integrity, Azure `onedrive_client_id` GUID format, granted scopes (`Files.ReadWrite` or `Files.ReadWrite.All`), silent token refresh against the cached account, Microsoft Graph drive-item probe (`me/drive/root:/{onedrive_folder}`), and a folder-vs-file shape assertion on the response. Auth-class failures (refresh failed, Graph 401) are bucketed into one `OneDrive auth failed` line so you don't get duplicate re-auth hints for the same root cause. See [admin.md#onedrive-deep-checks](admin.md#onedrive-deep-checks) for the full deep-check matrix and [backends/onedrive.md#diagnosing-setup-problems](backends/onedrive.md#diagnosing-setup-problems) for sample output.

`--backend webdav` additionally runs WebDAV-specific deep checks beyond the generic per-backend loop: URL well-formedness, `PROPFIND` on the configured root (HTTP 207 expected), `DAV:` class header detection (class 1+ required for sync), `getetag` presence (used for change detection), `oc:checksums` extension support detection (Nextcloud / OwnCloud advertise MD5/SHA1/SHA256 hashes), and an account-base smoke probe for Nextcloud / OwnCloud URL patterns. Authentication failures (401) bucket into a single `WebDAV auth failed` line. See [admin.md#webdav-deep-checks](admin.md#webdav-deep-checks) for the full deep-check matrix and [backends/webdav.md#diagnosing-setup-problems](backends/webdav.md#diagnosing-setup-problems) for sample output.

`--backend sftp` additionally runs SFTP-specific deep checks beyond the generic per-backend loop: host fingerprint match against `~/.ssh/known_hosts` (a mismatch is treated as a possible MITM and refuses to connect), SSH key file existence and 0600 permissions, key decryption (or ssh-agent fallback), connection + auth, `exec_command` capability (some `internal-sftp`-jailed accounts disallow shell, in which case claude-mirror falls back to client-side hashing), and root-path `stat`. Auth-class failures bucket into one `SFTP auth failed` line; the fingerprint-mismatch fix-hint deliberately points at `ssh-keygen -R hostname`, not `claude-mirror auth` — fingerprint mismatches are a security incident, not a token problem. See [admin.md#sftp-deep-checks](admin.md#sftp-deep-checks) for the full deep-check matrix and [backends/sftp.md#diagnosing-setup-problems](backends/sftp.md#diagnosing-setup-problems) for sample output.

See [admin.md#doctor](admin.md#doctor) for the full check matrix, sample output, and fix-hint interpretation.

### `find-config`

Print the config file path that matches the current working directory (or `PATH` if given). Searches all `~/.config/claude_mirror/*.yaml` files for one whose `project_path` matches, falling back to `default.yaml` if none match. The Claude Code skill uses this internally to detect the active project.

See [README — find-config](../README.md#find-config).

### `profile` (since v0.5.49)

Manage the credentials-profile registry under `~/.config/claude_mirror/profiles/`. A profile bundles credential-bearing fields (`credentials_file`, `token_file`, `dropbox_app_key`, `onedrive_client_id`, WebDAV creds, SFTP host info) for one logical account so multiple project YAMLs can share them via `profile: NAME` references or the global `--profile NAME` flag.

```
claude-mirror profile list
claude-mirror profile show NAME
claude-mirror profile create NAME --backend BACKEND [--description TEXT] [--force]
claude-mirror profile delete NAME [--delete] [--yes]
```

- `list` — table of every profile with backend + description + on-disk path.
- `show NAME` — print the raw YAML to stdout.
- `create NAME --backend ...` — interactive scaffold; only collects credential-bearing fields, NOT project-specific ones (`drive_folder_id`, `dropbox_folder`, etc.). `--force` overwrites an existing profile YAML.
- `delete NAME` — remove the profile YAML. Dry-run by default; `--delete` arms the action and prompts for typed `YES`; `--yes` skips the prompt.

Profile resolution at `Config.load`: the global `--profile NAME` flag wins over the YAML's `profile: NAME` field which wins over no-profile. When both a profile and the project YAML define the same field, **the project value wins** — the profile is the default, the project is the escape hatch.

See [profiles.md](profiles.md) for sample profile YAMLs per backend, the precedence rule worked through with examples, and common workflows.

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

### `max_throttle_wait_seconds`

Hard cap on the shared backoff coordinator's pause window when a backend signals a server-wide rate limit (HTTP 429 from Drive `userRateLimitExceeded`, Dropbox `too_many_requests`, OneDrive 429, etc.). When any worker hits a global throttle, every in-flight upload pauses for an exponentially-growing window (initially 30s or the server-supplied `Retry-After` value, multiplied by 1.5× on each escalation) — capped at this value. Default `600.0` (10 minutes).

```yaml
# in your project YAML — lower for cron jobs that should fail fast
max_throttle_wait_seconds: 60
```

Lower it for cron-driven runs that should fail fast and let the next tick retry, rather than holding open a long pause. Leave at the default for interactive `push` / `sync` / `watch` where the calm pause-and-resume pattern is the desired behaviour. See [admin.md — Rate-limit handling](admin.md#rate-limit-handling) for the full design rationale, the per-backend 429 detection matrix, and the user-facing message contract.

### Notification webhook fields

claude-mirror can post sync events to Slack, Discord, Microsoft Teams, and any generic JSON-receiving URL. All four are independent and opt-in; failures never block a sync. Full setup walkthroughs in [admin.md — Notifications](admin.md#notifications).

| Field | Type | Default | Purpose |
|---|---|---|---|
| `discord_enabled` | bool | `false` | Master switch for Discord webhook posts. |
| `discord_webhook_url` | str | `""` | Discord incoming-webhook URL — `https://discord.com/api/webhooks/{id}/{token}`. |
| `teams_enabled` | bool | `false` | Master switch for Microsoft Teams webhook posts. |
| `teams_webhook_url` | str | `""` | Teams incoming-webhook URL — legacy `outlook.office.com/webhook/...` form OR the modern `{tenant}.webhook.office.com/...` form. |
| `webhook_enabled` | bool | `false` | Master switch for the generic JSON webhook (n8n / Make / Zapier / custom endpoints). |
| `webhook_url` | str | `""` | Arbitrary URL that receives the schema-stable v1 JSON envelope on every event. |
| `webhook_extra_headers` | dict[str,str] / null | `null` | Extra HTTP headers attached to every generic-webhook request — typically auth tokens (`Authorization: Bearer ...`) or routing headers (`X-Tenant-ID: ...`). |

Slack-specific fields (`slack_enabled`, `slack_webhook_url`, `slack_channel`) are covered in [README — Slack notifications](../README.md#slack-notifications).

---

## See also

- [faq.md](faq.md) — 30-second answers to the most common questions across auth, sync, snapshots, notifications, performance, and migration.
- [admin.md](admin.md) — snapshots, retention, watcher daemon, notifications, credentials profiles.
- [profiles.md](profiles.md) — credentials profiles in depth: sample profile YAMLs per backend, precedence rule, common multi-project workflows.
- [conflict-resolution.md](conflict-resolution.md) — what `sync` does when both sides changed.
- [README — Daily usage](../README.md#part-4--daily-usage) — narrative walkthrough of the daily commands.
- [README — Slack notifications](../README.md#slack-notifications) — Slack-specific config and webhook setup.
- [admin.md — Notifications](admin.md#notifications) — Discord, Teams, and Generic webhook setup.
