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
