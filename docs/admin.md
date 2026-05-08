← Back to [README index](../README.md)

# Administration: snapshots, retention, and the watcher daemon

This page covers everything that lives between "I push and pull files" and "I pick a backend": snapshot formats, listing / deleting / restoring snapshots, garbage collection, automatic retention pruning, and the background watcher service that delivers real-time notifications from collaborators.

## Snapshots and disaster recovery

A snapshot of all project files is saved automatically after every successful `push` or `sync`. Two on-remote formats are supported — pick one **per project** in your config:

| Format | When to pick it | Storage cost per snapshot | Snapshot create cost |
|---|---|---|---|
| `blobs` (default for new projects) | You snapshot often, files change incrementally, you want disaster-recovery without paying full-tree storage every time | ~size of changed files (deduplicated across all snapshots) | Upload only the unique blobs not yet stored |
| `full` (default for older projects without the field) | You want a self-contained folder per snapshot, simpler model, willing to pay full-tree cost for each | full project size, every snapshot | Server-side copy of every file (no download/upload) |

Configure per project via the YAML field `snapshot_format: blobs` or `snapshot_format: full`. The `init --wizard` flow prompts; the non-wizard flow accepts `--snapshot-format`. Both formats coexist on the same remote — `restore` and `snapshots` work for any snapshot regardless of which format the project is currently set to.

### `blobs` format — content-addressed, deduplicated

Remote layout:

```
[Project Folder]/
├── CLAUDE.md
├── memory/notes.md
├── _claude_mirror_logs/
│   └── _sync_log.json
├── _claude_mirror_blobs/
│   ├── ab/
│   │   └── ab1c2d3e...   ← raw file body, named by SHA-256 of its content
│   └── ef/
│       └── ef9a0b1c...
└── _claude_mirror_snapshots/
    ├── 2026-03-05T10-30-00Z.json   ← manifest: {path: hash}
    └── 2026-03-05T11-45-00Z.json
```

Each unique file body is uploaded **exactly once**. The manifest is a small JSON listing every project file's path and the SHA-256 of its body. Two snapshots that differ in only one file share every other blob — the second snapshot costs ~one upload.

Run `claude-mirror gc` periodically to delete blobs no longer referenced by any manifest. **Safe by default** — running without flags is a dry-run scan only:

```bash
claude-mirror gc                              # primary backend, dry-run
claude-mirror gc --delete                     # primary backend, actually delete
claude-mirror gc --delete --yes               # primary, delete, skip typed prompt
claude-mirror gc --backend sftp               # gc the SFTP mirror, dry-run (Tier 2)
claude-mirror gc --backend sftp --delete      # gc the SFTP mirror, real delete
```

With `--delete` the command asks you to **type the literal word `YES`** (uppercase, exact). A `y`/`yes`/`Y`/anything-else aborts the deletion. `--yes` is the only way to skip the prompt and is explicitly required for non-interactive use. `gc` also refuses to run if no manifests exist on remote (which would otherwise wipe the entire blob store).

### `full` format — full server-side copy per snapshot

Remote layout:

```
_claude_mirror_snapshots/
├── 2026-03-05T10-30-00Z/
│   ├── _snapshot_meta.json
│   ├── CLAUDE.md
│   └── memory/notes.md
└── 2026-03-05T11-45-00Z/
    └── ...
```

Each snapshot folder is a complete server-side copy via the backend's native copy API — Google Drive (`files.copy`), Dropbox (`files/copy_v2`), OneDrive (async copy with monitor polling), WebDAV (`COPY` method). No file data passes through the client during snapshot creation, even for very large folders.

### Switching between formats

`claude-mirror migrate-snapshots --to blobs` (or `--to full`) converts every existing snapshot in-place. Idempotent and atomic per snapshot, so an interrupted run is safe to retry. Each successful conversion deletes its source-format artifact as the final step (unless `--keep-source` is passed). If a deletion fails (network blip, rate limit), the next migrate run automatically detects the leftover source as an "orphan" and cleans it up before processing anything else — no duplicate manifests, no manual cleanup.

```bash
claude-mirror migrate-snapshots --to blobs --dry-run    # preview
claude-mirror migrate-snapshots --to blobs              # do it
claude-mirror migrate-snapshots --to blobs --keep-source  # keep originals
```

By default the project's YAML is updated to the new format on success. Pass `--no-update-config` to leave the YAML untouched (useful if you want to test the new format on existing snapshots before flipping the default).

### List available snapshots

```bash
claude-mirror snapshots
```

Both formats are listed together with a `Format` column.

### Delete old snapshots

After migrating to `blobs` format, you may want to prune old snapshots to reclaim storage. Use `claude-mirror forget` with one of four selectors:

```bash
# Delete a specific snapshot (or several)
claude-mirror forget 2026-04-07T15-22-50Z 2026-04-07T13-06-53Z

# Delete everything older than a date (or relative duration)
claude-mirror forget --before 2026-04-15
claude-mirror forget --before 30d        # 30d / 2w / 3m / 1y are accepted
claude-mirror forget --before 2026-04-15T10:00:00Z

# Keep only the N newest snapshots
claude-mirror forget --keep-last 50

# Keep snapshots from the last N days
claude-mirror forget --keep-days 90
```

**Safe by default** — `forget` is dry-run unless you pass `--delete`:

```bash
claude-mirror forget --keep-last 50                # dry-run — shows matches, deletes nothing
claude-mirror forget --keep-last 50 --delete       # actually delete (must type YES)
claude-mirror forget --keep-last 50 --delete --yes # delete, skip the prompt (cron / CI)
```

With `--delete` the command asks you to **type the literal word `YES`** (uppercase, exact). Anything else aborts.

For `full`-format snapshots, the snapshot folder is deleted directly. For `blobs`-format snapshots, the manifest JSON is deleted and blobs no longer referenced by any remaining manifest become orphaned. After a `forget --delete` run that touched any `blobs` snapshots, run:

```bash
claude-mirror gc --delete
```

to reclaim the orphaned blob space.

### Auto-pruning by retention policy

`forget` is the precise, single-selector tool. For ongoing housekeeping you can declare a **retention policy** in the project YAML and let `claude-mirror push` keep the snapshot set trimmed automatically:

```yaml
# in your project YAML — every field defaults to 0 (= disabled)
keep_last:    7          # always keep the 7 newest snapshots
keep_daily:   14         # plus one snapshot per day for the last 14 days
keep_monthly: 12         # plus one snapshot per month for the last 12 months
keep_yearly:  5          # plus one snapshot per year for the last 5 years
```

Each field is independent — the **union** of every selector's keep-set is retained. The example above keeps "newest 7 + one per day for 2 weeks + one per month for a year + one per year for 5 years"; everything outside that union is pruned. Within each bucket the **newest** snapshot wins (e.g. with three snapshots on 2026-05-07, only the latest counts toward `keep_daily`).

Behaviour with retention enabled:

- After every successful `claude-mirror push`, the engine runs the prune automatically and prints a deletion summary.
- Setting the YAML field IS the consent — no extra confirmation prompt fires for the auto-prune path. (Each field is opt-in and defaults to 0.)
- For `blobs`-format snapshots, follow up with `claude-mirror gc --delete` to reclaim the orphaned blob space (the auto-prune doesn't run gc itself; see "Delete old snapshots" above for why).

You can also run the same policy by hand without waiting for a push, or one-off without changing the YAML:

```bash
# dry-run with the YAML's policy — shows what would be deleted
claude-mirror prune

# apply the YAML's policy
claude-mirror prune --delete

# non-interactive — for cron / CI
claude-mirror prune --delete --yes

# one-off override — does NOT modify the YAML
claude-mirror prune --keep-last 5 --keep-monthly 12 --delete --yes
```

`prune` is dry-run by default and requires both `--delete` AND a typed `YES` confirmation (or `--yes` for non-interactive use) — same safety contract as `forget` and `gc`. Any `--keep-*` flag overrides the corresponding config field for that one run only.

#### Retention defaults at init

Since v0.5.38, `claude-mirror init` writes a sensible retention policy into every newly created YAML so the prune path has something to act on out of the box:

```yaml
keep_last:    10         # 10 newest snapshots, regardless of age
keep_daily:   7          # plus one per day for the last week
keep_monthly: 12         # plus one per month for the last year
keep_yearly:  3          # plus one per year for the last 3 years
```

These kick in on the next successful `claude-mirror push` (or whenever `prune` is run). To use a different policy, edit the YAML directly or pass `--keep-*` flags to `prune` for a one-off override. To disable retention entirely, set every field to `0`. Pre-existing project YAMLs are not modified — configs without these fields continue to mean "no retention" (the dataclass defaults remain `0`), which closes out the Scenario A pitfall in [`docs/scenarios.md`](scenarios.md).

### Search the archive for a file's version history

When you want to find the right snapshot to restore from, `claude-mirror history PATH` scans every snapshot's manifest and reports which ones contain the file. For `blobs` snapshots, the SHA-256 lets it label distinct versions (v1, v2, ...) so you can spot when the file actually changed:

```bash
claude-mirror history MEMORY.md
```

```
History of MEMORY.md
  distinct versions:    13  (by SHA-256)
  total appearances:    47

    Snapshot timeline (newest first)
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Snapshot             ┃ Version ┃ Format ┃ SHA-256 (12) ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━┩
│ 2026-05-05T10-03-06Z │ v13     │ blobs  │ cf5b4d78fb59 │
│ 2026-04-29T14-22-10Z │ v12     │ blobs  │ fec0e6d5c7ee │
│ 2026-04-15T09-00-00Z │ v11     │ blobs  │ 932c1e4a12fb │
│ ...                  │ ...     │ ...    │ ...          │
└──────────────────────┴─────────┴────────┴──────────────┘
```

Version transitions render bold green; consecutive identical-hash rows render dim, so the eye picks up the change boundaries. Once you've found the version you want, restore it with the corresponding timestamp:

```bash
claude-mirror restore <timestamp> MEMORY.md --output ~/tmp/recovery
```

#### Filtering history by date

When the snapshot set is large, scan a narrower window with `--since` and `--until`. Both flags are independent and inclusive on both bounds, and accept the same vocabulary as `forget --before`: an ISO date (`2026-04-15`), an ISO datetime (`2026-04-15T10:00:00Z`), or a relative duration (`30d` / `2w` / `3m` / `1y`):

```bash
# Everything since April 15:
claude-mirror history MEMORY.md --since 2026-04-15

# Last 30 days only:
claude-mirror history MEMORY.md --since 30d

# A specific April-2026 window:
claude-mirror history MEMORY.md --since 2026-04-01 --until 2026-04-30
```

A `--since` later than `--until` is rejected up-front (red error, exit 1). With the filter active, an empty result echoes the parsed window back so you can see what range you queried:

```
No snapshots contain MEMORY.md
Active filter: since=2099-01-01T00:00:00Z, until=2099-12-31T00:00:00Z.
```

The filter applies BEFORE the per-snapshot manifest scan, so it's a meaningful speed-up on large remotes — only the snapshots inside the window are downloaded.

### Inspect a snapshot's contents

Before recovering, you can view exactly what's in a snapshot — every path with its SHA-256 (blobs format) or size (full format) — without downloading any file bodies:

```bash
claude-mirror inspect 2026-05-05T10-15-22Z

# Filter to a subdirectory:
claude-mirror inspect 2026-05-05T10-15-22Z --paths 'memory/**'

# Find one specific file:
claude-mirror inspect 2026-05-05T10-15-22Z --paths 'CLAUDE.md'
```

For blobs snapshots, this is one cheap manifest download. For full snapshots, it's a recursive listing of the snapshot folder. Use it to confirm a file exists at the version you want before running `restore`.

### Restore a snapshot

**Whole snapshot** — restore to a safe inspection directory first:

```bash
claude-mirror restore 2026-03-05T10-30-00Z --output ~/.local/tmp/claude-mirror/recovery
```

Review the files, then restore over your project if satisfied:

```bash
claude-mirror restore 2026-03-05T10-30-00Z
# Prompts: "This will overwrite the entire snapshot in /your/project. Continue? [y/N]"
```

**Single file** — pass the path as a positional argument:

```bash
claude-mirror restore 2026-03-05T10-30-00Z memory/MOC-Session.md
# Prompts: "This will overwrite 1 matching file(s) in /your/project. Continue? [y/N]"
```

**Multiple files / glob** — pass any number of paths or fnmatch globs:

```bash
claude-mirror restore 2026-03-05T10-30-00Z 'memory/**' --output ~/tmp/recovery
claude-mirror restore 2026-03-05T10-30-00Z '*.md'
claude-mirror restore 2026-03-05T10-30-00Z CLAUDE.md memory/notes.md
```

For blobs-format snapshots, single-file restore only downloads the one blob it needs — cheap regardless of snapshot size. Use `claude-mirror inspect TIMESTAMP --paths PATTERN` first to confirm a file exists at the version you want before recovering.

Restore auto-detects each snapshot's format — you don't have to know whether it was a `full` or `blobs` snapshot.

#### Previewing a restore

Pass `--dry-run` to see exactly what `restore` would write before committing to it. The plan is a Rich table (Path / Action / Source backend / Size) followed by a one-line summary; nothing is written to local disk:

```bash
claude-mirror restore 2026-03-05T10-30-00Z --dry-run
claude-mirror restore 2026-03-05T10-30-00Z 'memory/**' --dry-run
claude-mirror restore 2026-03-05T10-30-00Z --backend dropbox --dry-run
```

Each row's `Action` column is one of:

- `restore` — the file would be written (every file in a healthy snapshot).
- `missing-blob` — the manifest references a blob that's no longer on remote (typically because `claude-mirror gc --delete` ran after the snapshot was taken). The real `restore` would print a yellow warning and skip the file.

The summary line ends with `Run without --dry-run to apply.` so the next step is obvious. The exit code is 0 even when every row is `missing-blob` — `--dry-run` only fails on truly fatal errors (snapshot not found on any backend, malformed timestamp). Compose with shell tools for richer review:

```bash
# Spot every file that would be touched, paginated:
claude-mirror restore 2026-05-05T10-15-22Z --dry-run | less

# Diff the plan across two competing recovery candidates:
claude-mirror restore 2026-04-01T00-00-00Z --dry-run > /tmp/plan-april.txt
claude-mirror restore 2026-05-01T00-00-00Z --dry-run > /tmp/plan-may.txt
diff /tmp/plan-april.txt /tmp/plan-may.txt
```

### Comparing snapshots

When you want to know what changed between two recovery points before deciding which to restore, `claude-mirror snapshot-diff TS1 TS2` shows the per-file delta. Order matters — TS1 is the "from" snapshot, TS2 is the "to" snapshot. Pass the literal keyword `latest` for either side to use the most recent snapshot:

```bash
claude-mirror snapshot-diff 2026-04-01T10-00-00Z 2026-05-01T10-00-00Z
claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest
claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --paths 'memory/**'
claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --all
claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --unified CLAUDE.md
```

Each file is classified as one of:

| Status | Meaning |
|---|---|
| `added` | present in TS2, absent in TS1 |
| `removed` | present in TS1, absent in TS2 |
| `modified` | present in both, content differs |
| `unchanged` | present in both, content identical (omitted unless `--all`) |

For `modified` rows, the `Changes` column shows `+N -M` line counts computed via `difflib` on the two file bodies. Files whose bytes are not valid UTF-8 are reported as `binary` — both snapshots' contents must decode as text for the line count to apply.

`--paths PATTERN` filters the table by an fnmatch glob (e.g. `'memory/**'`, `'*.md'`, `'CLAUDE.md'`).

`--unified PATH` switches to a standard `diff -u`-format unified diff for one specific file — composes with shell tools (`less`, `delta`, `vim -`):

```bash
claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --unified CLAUDE.md | delta
claude-mirror snapshot-diff 2026-04-01T10-00-00Z latest --unified memory/notes.md | less -R
```

Both `blobs` and `full` snapshots are accepted, and the two snapshots may even be in different formats (the older one was `full`, the newer one is `blobs` after a `migrate-snapshots` run). For full-format snapshots, identical files between the two snapshots may show as `modified` (the per-snapshot file_id differs even when bytes match) — convert to `blobs` with `migrate-snapshots --to blobs` for content-equality classification.

---

## Performance and bandwidth control

claude-mirror is built around small markdown / JSON files, but the same upload paths handle snapshot blobs and (in Tier 2) parallel mirror writes. v0.5.39 adds two performance levers: a per-backend bandwidth cap and WebDAV chunked-PUT for large files.

### Bandwidth throttling: `max_upload_kbps`

Set `max_upload_kbps` in the project YAML to cap upload bandwidth on that backend. The value is in **kilobits per second** (1 kbps = 128 bytes/sec) so it matches the units users see in their ISP / NAS contracts.

```yaml
# in your project YAML — null (default) disables throttling
max_upload_kbps: 1024     # ≈ 128 KiB/sec, ≈ 7.5 MiB/min
```

How it works under the hood:

- A token-bucket limiter (in `claude_mirror/throttle.py`) lives inside each backend instance. The bucket fills at `max_upload_kbps * 1024 / 8` bytes/sec, up to a default capacity of `max(64 KiB, rate-bytes-per-sec)`.
- Every upload path consumes from the bucket BEFORE handing bytes to the SDK / wire: Drive's resumable-chunk loop, Dropbox `files_upload`, OneDrive simple-PUT and chunked upload session, WebDAV PUT (both simple and chunked), SFTP per-block writes.
- A small file (smaller than the bucket capacity) passes through with **zero added latency** — the bucket starts full at construction.
- A larger-than-bucket file paces in capacity-sized waves, so the long-run rate stays exactly at the cap regardless of how big the file is.
- When `max_upload_kbps` is `null` (the default), every backend uses a no-op `NullBucket` — no overhead, no behaviour change vs older versions.

In **Tier 2** multi-backend setups, every mirror config has its own `max_upload_kbps` field. So you can throttle Google Drive but leave SFTP unbounded, or vice versa, by setting the field on one config and leaving it `null` on the other.

When NOT to set it:

- On a fast home connection where upload bandwidth is plentiful — leaving it null keeps the hot path uncapped.
- On a process that only writes the manifest + a handful of small markdown files per push (the bucket is essentially free in that case, but also delivers no benefit).

When to set it:

- On a metered / capped link (mobile hotspot, 4G failover) where saturating upload affects voice / video calls.
- When pushing large `_claude_mirror_blobs/` payloads on a freshly-seeded mirror — the seed-mirror operation moves the most data and benefits most from rate-limiting.
- When sharing an upstream link with latency-sensitive workloads (gaming, VoIP) and you want claude-mirror to defer to those.

### WebDAV chunked PUT for large files

WebDAV's `upload_file` selects between two paths based on `webdav_streaming_threshold_bytes` (default `4194304`, i.e. 4 MiB):

- **Below threshold** (typical markdown content): single in-memory PUT — minimal overhead.
- **At or above threshold**: streaming PUT — the request body is a generator yielding fixed 1 MiB blocks read lazily from disk. Peak memory is bounded to one block (1 MiB) regardless of file size, and an explicit `Content-Length` header is sent so servers that reject chunked transfer-encoding (Apache mod_dav with default config) accept the upload.

Configure via:

```yaml
# in your project YAML — files >= this size stream; smaller use simple PUT
webdav_streaming_threshold_bytes: 4194304   # default 4 MiB
```

The simple-PUT path is preserved for small markdown files so the hot path is unchanged. The streaming path matters when a `_claude_mirror_blobs/` payload, an attachment, or a non-markdown asset crosses 4 MiB — without the chunked path, the whole file would land in memory before the PUT starts.

The throttle bucket integrates with both paths: every block yielded by the streaming generator is pre-paid via `bucket.consume(len(block))` so the long-run rate stays honest across multi-megabyte transfers.

### Upload resume behaviour by backend

Resume semantics differ across backends. Document the matrix here so users know what to expect when a watcher / push process crashes mid-upload:

| Backend     | Native resume protocol                          | Survives process restart | Behaviour on failure                                              |
|-------------|-------------------------------------------------|--------------------------|-------------------------------------------------------------------|
| googledrive | Drive resumable upload (session URI + offset)   | No (session URI not persisted across runs) | In-process retries via `_upload_with_retry` resume the session; a crashed process re-uploads from scratch on next push. |
| dropbox     | `files_upload_session_*` (chunked + commit)     | No (session ID not persisted across runs)  | In-process retry of `files_upload`; crashed process re-uploads from scratch.                                            |
| onedrive    | Microsoft Graph `createUploadSession`           | Up to ~7 days per Graph spec (URL not persisted by claude-mirror) | In-process retry resumes within the session; crashed process re-creates the session and re-uploads.                     |
| webdav      | None (HTTP PUT is single-shot)                  | No                       | Re-upload from scratch on retry. Streaming PUT (v0.5.39+) keeps peak memory bounded but the wire still re-sends.        |
| sftp        | None (paramiko's `SFTPClient` does not resume)  | No                       | Re-upload from scratch on retry. The `.tmp + posix_rename` dance keeps the destination atomic — never half-written.     |

claude-mirror does NOT currently persist upload-session URLs / IDs to disk between runs, so even backends with native resume protocols restart from byte zero after a process crash. This is by design for v0.5.39: claude-mirror's working set is small markdown files, where re-uploading is cheap and the complexity of cross-restart resume isn't justified. If you push a large blob payload often enough to hit this, raise the issue and we can revisit.

---

## Auto-start the watcher

Use `claude-mirror watch-all` to watch every project in a single process. It auto-discovers all configs in `~/.config/claude_mirror/` and starts one notification listener per project, each in its own thread. Projects using different backends are handled transparently — each thread picks the right notifier for its backend (Pub/Sub for Google Drive, long-polling for Dropbox, periodic polling for OneDrive, WebDAV, and SFTP):

```bash
claude-mirror watch-all
```

To watch a specific subset:

```bash
claude-mirror watch-all --config ~/.config/claude_mirror/work-a.yaml \
                      --config ~/.config/claude_mirror/personal-b.yaml
```

### Adding a new project to a running watcher

When you create a new project with `claude-mirror init`, the running watcher is notified automatically via `SIGHUP` and picks up the new config without restarting. You can also trigger a reload manually:

```bash
claude-mirror reload
```

This sends `SIGHUP` to the running `watch-all` process, which re-scans `~/.config/claude_mirror/` for new config files and starts watcher threads for any it doesn't already have. Existing watchers are not interrupted.

### Recommended: `claude-mirror-install`

If you already ran `claude-mirror-install` in Part 2 Step 3, the watcher service is set up and running — nothing else to do. Otherwise:

```bash
claude-mirror-install
```

It detects your platform automatically, creates the appropriate service file, and loads it immediately. The watcher will restart on login and on failure. To remove the service:

```bash
claude-mirror-install --uninstall
```

### Manual component installation

If you prefer to set up the service by hand, or need to customize the generated file, follow the steps for your platform below.

#### macOS (launchd)

Create `~/Library/LaunchAgents/com.claude-mirror.watch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.claude-mirror.watch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOUR_USERNAME/.local/bin/claude-mirror</string>
        <string>watch-all</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/YOUR_USERNAME/Library/Logs/claude-mirror-watch.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USERNAME/Library/Logs/claude-mirror-watch.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/Users/YOUR_USERNAME/.local/bin</string>
        <key>GRPC_VERBOSITY</key>
        <string>ERROR</string>
    </dict>
</dict>
</plist>
```

Replace `YOUR_USERNAME` with your macOS username. Load the agent:

```bash
launchctl load -w ~/Library/LaunchAgents/com.claude-mirror.watch.plist
```

Unload and remove it:

```bash
launchctl unload -w ~/Library/LaunchAgents/com.claude-mirror.watch.plist
rm ~/Library/LaunchAgents/com.claude-mirror.watch.plist
```

#### Linux (systemd user service)

Create `~/.config/systemd/user/claude-mirror-watch.service`:

```ini
[Unit]
Description=Claude Sync watcher — real-time cloud storage notifications
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/home/YOUR_USERNAME/.local/bin/claude-mirror watch-all
Restart=on-failure
RestartSec=10
Environment=GRPC_VERBOSITY=ERROR
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Replace `/home/YOUR_USERNAME` with your home directory path. Enable and start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now claude-mirror-watch
```

View logs:

```bash
journalctl --user -u claude-mirror-watch -f
```

Stop and remove:

```bash
systemctl --user disable --now claude-mirror-watch
rm ~/.config/systemd/user/claude-mirror-watch.service
systemctl --user daemon-reload
```

#### Claude Code skill

```bash
mkdir -p ~/.claude/skills/claude-mirror
cp /path/to/Claude_Sync/skills/claude-mirror.md ~/.claude/skills/claude-mirror/SKILL.md
```

#### PreToolUse notification hook

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "claude-mirror inbox 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

## Multi-backend mirroring (Tier 2)

A single project can be synced to multiple storage backends at the same time. Push uploads to all of them in parallel, snapshots are mirrored across all of them (configurable), and pull / status read from the primary. If a mirror fails transiently it is retried automatically on the next push; permanent failures are quarantined and surfaced via `claude-mirror status --pending` and the desktop / Slack notifiers.

For deployment topologies that combine mirroring with multi-user collaboration, see [scenarios.md](scenarios.md) — Scenario D (multi-backend redundancy) and Scenario G (multi-user + multi-backend, production-realistic).

### Why mirror?

- **Redundancy** — if one provider has an outage, the other backends still hold a current copy of every file plus a fresh snapshot. Disaster recovery does not depend on a single vendor.
- **Cross-platform collaboration** — one collaborator can run the project on Google Drive while another only has access to Dropbox or a self-hosted WebDAV server. The primary owner mirrors to whichever backends the team needs.
- **Backend portability** — mirroring is the safe, non-destructive way to move a project between backends. Run it as a mirror for as long as you like, then promote the mirror to primary by swapping config paths when you're ready.

### Setup walkthrough

The model is: **one primary config + one extra config per mirror**, all sharing the same `project_path`. The primary config gets a `mirror_config_paths` list pointing at the mirrors.

1. **Initialize the primary config** (whichever backend you want as primary — this example uses Google Drive):

   ```bash
   claude-mirror init --wizard \
     --backend googledrive \
     --project ~/projects/myproject
   # Writes ~/.config/claude_mirror/myproject.yaml
   ```

2. **Initialize one config per mirror**, sharing the same `--project` path but using a different backend, folder, and token file. Use `--config` to pin the file name so it is obviously a mirror:

   ```bash
   claude-mirror init --wizard \
     --backend dropbox \
     --project ~/projects/myproject \
     --config ~/.config/claude_mirror/myproject-dropbox.yaml

   claude-mirror init --wizard \
     --backend onedrive \
     --project ~/projects/myproject \
     --config ~/.config/claude_mirror/myproject-onedrive.yaml
   ```

3. **Edit the primary config** and add the `mirror_config_paths` field, listing each mirror's YAML file:

   ```yaml
   # ~/.config/claude_mirror/myproject.yaml
   backend: googledrive
   project_path: ~/projects/myproject
   # ... drive_folder_id, gcp_project_id, etc ...
   mirror_config_paths:
     - ~/.config/claude_mirror/myproject-dropbox.yaml
     - ~/.config/claude_mirror/myproject-onedrive.yaml
   ```

4. **Authenticate each backend** (each mirror has its own token file, so each needs its own auth):

   ```bash
   claude-mirror auth --config ~/.config/claude_mirror/myproject.yaml
   claude-mirror auth --config ~/.config/claude_mirror/myproject-dropbox.yaml
   claude-mirror auth --config ~/.config/claude_mirror/myproject-onedrive.yaml
   ```

5. **Push** — the primary config is enough; mirrors are picked up automatically:

   ```bash
   claude-mirror push
   # Uploads to Google Drive, Dropbox, and OneDrive in parallel.
   # Snapshots are mirrored to each backend per snapshot_on policy.
   ```

### Configuration reference

The primary config gains the following optional fields. Mirror configs are ordinary single-backend configs — they don't carry any mirror-specific fields themselves.

```yaml
# Primary config — ~/.config/claude_mirror/myproject.yaml
backend: googledrive
project_path: ~/projects/myproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
# ... rest of the primary backend's normal fields ...

# Mirrors — each is a full claude-mirror config in its own file,
# sharing the same project_path as the primary.
mirror_config_paths:
  - ~/.config/claude_mirror/myproject-dropbox.yaml
  - ~/.config/claude_mirror/myproject-onedrive.yaml

# Snapshot mirroring policy.
#   "primary" — snapshots only go to the primary backend
#   "all"     — snapshots are written to every backend
# When omitted, the default depends on snapshot_format:
#   blobs format → "all"     (cheap, deduplicated, mirror-friendly)
#   full  format → "primary" (one full copy per snapshot is enough)
snapshot_on: all

# Automatically re-attempt mirrors that previously ended up in
# pending_retry state. Runs at the start of every push / sync.
retry_on_push: true

# In-process retry attempts per upload before giving up and queuing
# the file for next-push retry. Exponential backoff: 0.8s, 1.6s, 3.2s.
max_retry_attempts: 3

# Surface mirror failures via desktop notification and Slack
# (in addition to the per-backend status block always shown on success).
notify_failures: true
```

### Daily usage

What changes once mirroring is set up:

- **`claude-mirror push`** — uploads to every backend in parallel. Output groups results by backend; the run as a whole succeeds even if one mirror has transient errors (those files end up in `pending_retry` for the next push).
- **`claude-mirror sync`** — same conflict-resolution flow as before; the resolved file is then pushed to every backend.
- **`claude-mirror pull`** — reads from the **primary** backend. Mirrors are write-only from claude-mirror's perspective.
- **`claude-mirror status`** — reads from the primary. Add `--pending` for a separate table listing files with non-ok state on any mirror (File / Backend / State / Last error) AND any mirror that has files unseeded on it (typically because the mirror was added to `mirror_config_paths` after files were already pushed to the primary — see seed-mirror below). When the table is non-empty the trailing hint suggests `claude-mirror retry` or `claude-mirror seed-mirror` as appropriate. Add `--by-backend` for the **full per-file table with one column per configured backend** (primary first, mirrors in `mirror_config_paths` order) — each cell shows that backend's state for the file (`✓ ok` / `⚠ pending` / `✗ failed` / `⊘ unseeded` / `· absent`) plus a footer summary line per backend. The "is everything in sync on every mirror?" view at a glance.
- **`claude-mirror retry`** — re-attempts mirrors stuck in `pending_retry`. Optional `--backend NAME` to retry one mirror, `--dry-run` to preview without uploading. Runs the same upload path as push, with the same error classification.
- **`claude-mirror seed-mirror --backend NAME`** — populates a newly-added mirror with files that already exist on the primary. When you add a backend to `mirror_config_paths` for a project where files already exist, regular `push` has nothing to do (every local hash matches its manifest record), so push uploads zero files and the new mirror's folder stays empty. `seed-mirror` walks the manifest, finds every file with no recorded state on the named mirror, and uploads each one to that mirror only — the primary is never touched. Idempotent: safe to re-run; the second invocation is a no-op. Drift-safe: files whose local content has diverged from the manifest are skipped with a warning rather than seeded with mismatched content (run `push` first to reconcile primary, then re-run seed-mirror). Use `--dry-run` to preview which files would be seeded.
- **`claude-mirror restore TIMESTAMP`** — tries the primary first, then walks `mirror_config_paths` in order until it finds the snapshot. When the snapshot is recovered from a mirror, claude-mirror prints a yellow warning identifying which backend supplied it. To force a specific backend (e.g. when the primary is down or you know which mirror has the version you want), use `claude-mirror restore TIMESTAMP --backend dropbox`.

`retry_on_push: true` means most transient failures heal themselves: a brief Dropbox outage during one push gets retried automatically on the next push without you doing anything. `claude-mirror retry` is only needed when you want to force a retry without making a new push. `claude-mirror seed-mirror` is only needed once per (mirror × project) pair, the first time you add a mirror to a project that already has files on the primary.

### Failure handling

Each backend classifies its raw exceptions into one of six `ErrorClass` values. The class determines what claude-mirror does and what you see:

| Class | What it means | What claude-mirror does | What you see |
|---|---|---|---|
| `TRANSIENT` | Network blip, 5xx, brief rate limit | Retries 3x in-process with exponential backoff (0.8s / 1.6s / 3.2s), then queues for next-push retry | Yellow warning; Slack `🟡 backend — N file(s) pending retry` |
| `AUTH` | Refresh token revoked or expired | Marks affected files `failed_perm` — no further auto-retry | Red `ACTION REQUIRED` block. Run `claude-mirror auth --config <mirror config>` (or plain `claude-mirror auth` for the primary) |
| `QUOTA` | Storage full or sustained rate limit | Marks affected files `failed_perm` | Red `ACTION REQUIRED` block. Free space on that backend or wait for quota reset, then `claude-mirror retry --backend NAME` |
| `PERMISSION` | Folder access revoked | Marks affected files `failed_perm` | Red `ACTION REQUIRED` block. Restore folder permissions, then `claude-mirror retry --backend NAME` |
| `FILE_REJECTED` | File too large or invalid path for this backend | Skips just that file; other files continue | Per-file warning in the per-backend status block; not retried |
| `UNKNOWN` | Unrecognized exception | Treated like `TRANSIENT` but with a louder warning | Yellow warning + raw exception text |

Slack messages include a per-backend status block, e.g.:

```
🔼 user@machine pushed 1 file in myproject
Files changed: • memory/notes.md
Per-backend status:
  • 🟢 drive — pushed 1, snapshot 2026-05-05T10-15-22Z
  • 🟡 dropbox — rate-limited (1 file pending retry)
📚 1245 files in project
```

For permanent failures (`AUTH`, `QUOTA`, `PERMISSION`), a separate `🔴 ACTION REQUIRED` header block is prepended with a red sidebar so it stands out in the channel. Desktop notifications follow the same rule when `notify_failures: true`.

### When to use Tier 2 vs running two configs by hand

Tier 2 is the supported way to mirror a project. There is also an unsupported workaround — keep two completely independent configs for the same project path and run `claude-mirror push --config A` followed by `claude-mirror push --config B` yourself. That works, but:

- Each push is two commands, with no shared error handling or pending-retry queue.
- Snapshot timestamps drift between backends (each push creates its own snapshot independently).
- `restore` cannot fall back across backends — you have to know which config to use.
- Failures are silent unless you read both command outputs.

Use Tier 2 (`mirror_config_paths`) for any real mirroring use case. Reach for the two-config workaround only if you specifically want each backend to be 100% independent (different file patterns, different exclude lists, manually triggered) and you accept the bookkeeping.

---

## Doctor

`claude-mirror doctor` is the end-to-end self-test for a project's configuration. It runs every common check that could explain a failed `push`, `pull`, `sync`, or `auth`, and reports each result with a concrete fix command pointing at the right next action — `claude-mirror auth --config ...`, `chmod 600 KEY`, "verify the folder ID in the provider's web UI", and so on. With Tier 2 mirroring configured, every mirror in `mirror_config_paths` gets the same check sequence applied automatically, so one `doctor` invocation diagnoses the whole multi-backend setup.

### Check matrix

The implementation runs the following checks in order. Every per-backend check repeats for each entry in `mirror_config_paths`. The `--backend NAME` flag filters the per-backend loop to one backend; the primary-config parse (Check 1) always runs.

#### Configuration

| Check | Backends | Failure looks like |
|---|---|---|
| Primary config file exists and parses as YAML | all | `config file not found: PATH` or `config file does not parse: PATH` — exits early; later checks are meaningless without a config |
| Each Tier 2 mirror config in `mirror_config_paths` loads and parses | all (Tier 2 only) | `mirror config does not load: PATH` — recorded as a failure but the run continues so the primary's other checks still report |

#### Credentials

| Check | Backends | Failure looks like |
|---|---|---|
| OAuth credentials file referenced by `credentials_file` exists on disk | googledrive, dropbox, onedrive | `credentials file missing: PATH` — fix is to re-download `credentials.json` from the provider's developer console |
| Credentials check skipped (inline in YAML) | webdav, sftp | info-only line, never a failure |

#### Tokens / inline auth material

| Check | Backends | Failure looks like |
|---|---|---|
| Token file exists, parses as JSON, and contains a `refresh_token` | googledrive, dropbox, onedrive | `token file missing` / `token file corrupt` / `token has no refresh_token` — fix is `claude-mirror auth --config PATH` (consent screen must be shown to issue a new refresh token) |
| `webdav_username` and `webdav_password` are non-empty in the YAML | webdav | `WebDAV credentials missing in config: PATH` |
| `sftp_host`, `sftp_username`, `sftp_folder`, plus at least one of `sftp_key_file` or `sftp_password` are set | sftp | `SFTP config incomplete (missing FIELDS): PATH` |

#### Connectivity

A single light read call is made against the configured root: `list_folders` for cloud backends, `sftp.stat(sftp_folder)` for SFTP. Exceptions are classified through the backend's own `classify_error` so the fix-hint matches what actually went wrong rather than dumping a raw stack trace.

| Failure class | What triggers it | Fix-hint shown |
|---|---|---|
| AUTH | OAuth `invalid_grant`, HTTP 401, `RefreshError`, SSH auth failure | `claude-mirror auth --config PATH` to re-authenticate |
| PERMISSION | HTTP 403, `forbidden`, server-side ACL denial | re-auth or check folder sharing in the provider's web UI; for SFTP, your account lacks access to `sftp_folder` |
| FILE_REJECTED + 404 / "not found" | Folder ID is wrong (Drive, Dropbox, OneDrive); `sftp_folder` does not exist on the server | verify the folder in the provider's web UI and update the YAML; for SFTP, server-side `mkdir` or change `sftp_folder` |
| TRANSIENT | `TimeoutError`, `ConnectionError`, `TransportError`, "timed out" / "connection" in the message | check internet connectivity (and any corporate proxy / VPN); for SFTP, check `ping HOST` and that the configured port is open |
| anything else | unrecognised exception | inspect the error and re-run `auth` if it looks auth-related |

#### SFTP-specific auxiliary checks

These run regardless of connectivity outcome so every fixable issue is surfaced in one pass.

| Check | Failure looks like |
|---|---|
| SSH key file is readable by the current user | `SSH key file not readable: PATH` — fix is `chmod 600 PATH` |
| `known_hosts` file exists when `sftp_strict_host_check: true` (default) | `known_hosts file missing: PATH` — fix is `ssh USER@HOST` once to populate it, or set `sftp_strict_host_check: false` for closed-LAN setups |
| `sftp_strict_host_check: false` advisory | yellow warning (not a failure) — host fingerprints will not be verified |
| Plaintext password stored in YAML advisory | yellow warning (not a failure) — recommend switching to key-based auth for any internet-reachable server |

#### Project path

| Check | Backends | Failure looks like |
|---|---|---|
| `project_path` exists locally | all | `project_path does not exist: PATH` — fix is to update the YAML |
| `project_path` is a directory (not a regular file or symlink to a non-dir) | all | `project_path is not a directory: PATH` |

#### Manifest integrity

The manifest auto-recovers from a corrupt file by moving it aside, which would otherwise mask the issue from the user. Doctor reads the file directly and reports parse failures.

| Check | Failure looks like |
|---|---|
| `.claude_mirror_manifest.json` parses as JSON (if the file exists at all) | `manifest is corrupt: PATH` — fix is `rm PATH && claude-mirror sync --config PATH` |
| Manifest absent | info-only line — first sync will create it |

### Sample successful output

```
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking googledrive backend (/home/alice/.config/claude_mirror/myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

── checking dropbox backend (/home/alice/.config/claude_mirror/myproject-dropbox.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/dropbox-credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/dropbox-myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✓ All checks passed.
```

### Sample failure output

```
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking googledrive backend (/home/alice/.config/claude_mirror/myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/credentials.json
  ✗ token file has no refresh_token: /home/alice/.config/claude_mirror/myproject-token.json
      Fix: run claude-mirror auth --config /home/alice/.config/claude_mirror/myproject.yaml (consent screen must be shown to issue a new refresh_token).
  ✗ backend connectivity failed (RefreshError): invalid_grant: Token has been expired or revoked.
      Fix: token revoked or refresh failed. Run claude-mirror auth --config /home/alice/.config/claude_mirror/myproject.yaml to re-authenticate.
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✗ 2 issue(s) found. Fix the items above and re-run claude-mirror doctor.
```

### Exit codes

- `0` — every check passed.
- `1` — at least one check failed.

This composes cleanly with shell scripts and CI: `claude-mirror doctor && claude-mirror push` will only push if the configuration is healthy, and a CI job that runs `claude-mirror doctor` on each agent surfaces broken setups before they cause noisy push / sync failures downstream.

### Common invocations

```bash
claude-mirror doctor                                              # auto-detect config from cwd
claude-mirror doctor --config ~/.config/claude_mirror/work.yaml   # specific config
claude-mirror doctor --backend dropbox                            # only check the dropbox backend (Tier 2)
```

The `--backend` filter is case-insensitive and accepts `googledrive`, `dropbox`, `onedrive`, `webdav`, or `sftp`. The primary config is always parsed; only the per-backend loop is filtered. Skipped backends print a dim `── skipped: NAME (PATH) — does not match --backend FILTER` line so the output stays self-explanatory.

### Where to go next

- Credentials issues (missing `credentials.json`, OAuth client setup) — see the backend setup pages: [backends/google-drive.md](backends/google-drive.md), [backends/dropbox.md](backends/dropbox.md), [backends/onedrive.md](backends/onedrive.md), [backends/webdav.md](backends/webdav.md), [backends/sftp.md](backends/sftp.md).
- Manifest corruption or surprising sync state — see [conflict-resolution.md](conflict-resolution.md) for how the manifest interacts with the conflict-detection flow.
- Full flag list — [cli-reference.md#doctor](cli-reference.md#doctor).

---

## See also

- [conflict-resolution.md](conflict-resolution.md) for resolving `sync` conflicts.
- [cli-reference.md](cli-reference.md) for the full command list (snapshot, retention, watcher commands grouped under "Snapshots" and "Maintenance").
- [scenarios.md](scenarios.md) for end-to-end deployment topology guides (standalone, multi-machine, multi-user, multi-backend).
- Backend pages — backend-specific notes about how `full`-format snapshots and the watcher behave on each backend:
  - [backends/google-drive.md](backends/google-drive.md)
  - [backends/dropbox.md](backends/dropbox.md)
  - [backends/onedrive.md](backends/onedrive.md)
  - [backends/webdav.md](backends/webdav.md)
  - [backends/sftp.md](backends/sftp.md)
