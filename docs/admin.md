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

Both formats are listed together with a `Format` column. Two further columns surface tag-snapshot metadata: `Tag` (the named tag, if any) and `Message` (a free-form annotation truncated to ~50 chars in the table).

### Naming a snapshot

Pushes auto-create snapshots already; sometimes a maintainer wants an explicit, memorable rollback target. `claude-mirror snapshot` takes a snapshot on demand and lets you attach an optional `--tag NAME` (a short identifier) and/or `--message TEXT` (a free-form annotation) so you can find it by name later instead of having to remember a UTC timestamp:

```bash
# Take a named snapshot before a risky change
claude-mirror snapshot --tag pre-refactor --message "before the big sync-engine refactor"

# Roll back to it later by name (no need to remember the timestamp)
claude-mirror restore --tag pre-refactor

# Tagged snapshots are skipped by `prune` (`--include-tagged` to override)
claude-mirror prune --keep-last 5 --delete                    # tagged snapshots are shielded
claude-mirror prune --keep-last 5 --include-tagged --delete   # opt in to deleting them
```

Tag rules: names must match the regex `^[A-Za-z0-9._-]{1,64}$` — 1 to 64 ASCII characters, alphanumeric or `.`, `_`, `-` only. No spaces, no slashes, no `@`, no unicode. Tags are unique per project; trying to reuse a tag exits 1 with a hint to either pick a different name or `forget` the existing snapshot first. Both flags are optional and compose freely:

```bash
claude-mirror snapshot                                   # untagged, no message — same as before
claude-mirror snapshot --tag v1.0
claude-mirror snapshot --message "before the big refactor"
claude-mirror snapshot --tag v1.0 --message "first stable release"
```

Restore by tag composes with the existing flags (`--output PATH`, `[PATHS...]`, `--dry-run`, `--backend NAME`):

```bash
claude-mirror restore --tag v1.0 --output ~/tmp/recovery
claude-mirror restore --tag v1.0 'memory/**'
claude-mirror restore --tag v1.0 --dry-run
```

If the tag doesn't exist in this project, the command exits 1 with the available tags listed so you can pick the right one. Mutual-exclusion: pass `--tag NAME` OR a positional TIMESTAMP, not both.

The `_claude_mirror_snapshots/{TIMESTAMP}.json` (blobs) and `_claude_mirror_snapshots/{TIMESTAMP}/` (full) on-disk identities are unchanged — tags are an additive annotation on top of the existing manifest. Pre-SNAP-TAG snapshots (taken before this release) load cleanly with both fields blank.

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

### Browsing without restoring

When you only want to inspect a snapshot — `grep -r`, `diff`, open a file in your editor — and not actually overwrite the project, `claude-mirror mount` is the lighter-weight alternative to `restore --output`:

```bash
mkdir /tmp/snap
claude-mirror mount --tag pre-refactor /tmp/snap
grep -r 'TODO' /tmp/snap
diff /tmp/snap/CLAUDE.md /your/project/CLAUDE.md
claude-mirror umount /tmp/snap
```

Five variants share one engine: a single tagged or timestamped snapshot (`--tag` / `--snapshot`), the live current state of the primary backend or a Tier 2 mirror (`--live` / `--live --backend NAME`), every snapshot stacked under per-timestamp subdirectories (`--all-snapshots`), or the last snapshot taken on or before a date (`--as-of DATE`). All variants are read-only — writes return `EROFS`. Blob bodies are content-addressed and cached forever; the first read pays a network round-trip, subsequent reads serve from `$XDG_CACHE_HOME/claude-mirror/blobs/`.

fusepy ships in the base install (since v0.5.61) — `pipx install claude-mirror` is enough on the Python side. The platform's kernel layer (macFUSE / WinFsp / libfuse) is installed separately, only required at mount time. Full reference: [`docs/cli-reference.md` — `mount`](cli-reference.md#mount). End-to-end recipe with pitfalls: [Scenario J. Browse / grep / diff snapshots without restoring](scenarios.md#j-browse--grep--diff-snapshots-without-restoring).

### End-to-end integrity audit

`claude-mirror verify` is the proactive drift-detection tool: it confirms claude-mirror's recorded view of reality matches what's actually stored on every backend and in the local mount cache. Pairs with `claude-mirror health` for the full monitoring story — health checks **liveness** (is the system live and reachable?), verify checks **correctness** (do the bytes match the manifest, and do the content-addressed blobs still hash to their filenames?). Inspired by `restic check` and `rclone check`.

Three independent verification phases:

| Phase | What it verifies |
|---|---|
| `manifest_vs_remote` | For each entry in `.claude_mirror_manifest.json`, compare the manifest's `synced_remote_hash` against what each configured backend returns from `get_file_hash()`. Drift = backend hash differs. Missing = backend has no record of the file ID. Honours each backend's native hash algorithm (Drive `md5Checksum`, Dropbox `content_hash`, OneDrive `quickXorHash`, WebDAV ETag / `oc:checksums`, SFTP `sha256`). |
| `snapshot_blobs` | Re-hash every `_claude_mirror_blobs/<hh>/<hash>` blob on each backend; mismatch = corrupted (the content-addressing contract is broken — bit-rot, partial upload, or tampering). |
| `mount_blob_cache` | Re-hash every entry in the on-disk content-addressed cache populated by [`mount`](#browsing-without-restoring). Corrupted entries are surfaced so the user can evict and refetch on the next mount rather than serve bad bytes. |

Default mode is informational — the report prints, the exit code is `0` whether or not findings were surfaced. `--strict` flips the contract: any drift / missing / corrupted entry exits `1`, which is the signal a daily cron uses to alert on integrity regressions:

```bash
claude-mirror verify                                    # default: report + exit 0
claude-mirror verify --strict                           # exit 1 on any finding
claude-mirror verify --json                             # parseable v1 envelope
claude-mirror verify --backend dropbox --no-mount-cache # Tier 2: scope to one mirror
```

Per-phase opt-out flags (`--no-files`, `--no-snapshots`, `--no-mount-cache`) let you focus on one layer at a time. Full reference, sample report, and the JSON envelope spec: [`docs/cli-reference.md` — `verify`](cli-reference.md#verify).

A daily cron alongside `health` covers both halves of the monitoring story:

```cron
*/5 * * * * /usr/local/bin/claude-mirror health --json --no-backends || /usr/local/bin/notify-monitor
0 3 * * *   /usr/local/bin/claude-mirror verify --strict --json    || /usr/local/bin/notify-monitor
```

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

### Transfer progress: live ETA + bytes/sec

`push`, `pull`, `sync`, and `seed-mirror` render a Rich Progress bar with a real fill, "X.Y / Z.Z MB" cumulative byte counter, transfer rate, and ETA on every long byte-transfer phase. Sample output during a fresh-mirror seed of a 50 MB notes project:

```
Seeding         ━━━━━━━━━━━━━━━━━━━━━━━╸━━━━━━━━  31.4/50.0 MB  •  4.2 MB/s  •  0:00:07  •  0:00:04 remaining
```

The bar is sized once at the start of the phase by summing local file sizes (for upload) or remote file sizes (for download). Each backend's `upload_file` / `download_file` accepts an optional `progress_callback: Callable[[int], None]` that the engine wires to the bar; the callback is invoked with bytes-since-the-last-call deltas as each chunk completes. All five backends ship a per-chunk hook on the streaming/chunked paths, plus a single final emission for single-shot uploads (Dropbox `files_upload`, the simple PUT path on OneDrive / WebDAV).

When does it show up:

- **Above ~1 MB transfers** the rate + ETA columns become useful — Rich refreshes ~10 Hz, so transfers shorter than that complete before the first refresh and the user just sees the final completed state.
- **Non-tty mode** (e.g. when redirected to a file or run under cron) — Rich auto-detects and renders a plainer, non-animated form. No flag needed.
- **`--json` mode** silences progress entirely (the `_NoOpProgressCtx` shim swallows every Progress call) so structured-output consumers never see a progress carriage-return in their JSON stream.

What still uses the simpler spinner-style phase progress (no bytes-bar):

- `status` — counts files, no transfer phase.
- Snapshot creation — copies blobs server-side or uploads many tiny manifest writes.
- Notification publish — single small JSON event.
- The Local / Remote rows during the status pass that precedes every push / pull / sync.

This is intentional — sites without a known byte total would render an empty / pulsing bar, which adds visual noise without conveying information. The two factories (`make_phase_progress` for spinner-style phases, `make_transfer_progress` for byte-transfer phases) coexist by design.

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

### Rate-limit handling

claude-mirror's retry path distinguishes two failure modes that look superficially similar but need very different responses:

- **TRANSIENT** — a per-file network blip. One file's upload timed out / saw a 502 / had a TLS hiccup. The right response is to retry **just that file** with the per-backend exponential backoff (0.8s, 1.6s, 3.2s, ...) embedded in `_upload_with_retry`. Other files' uploads continue normally — the backend itself is healthy.
- **RATE_LIMIT_GLOBAL** — the SERVER is throttling this client/account overall. HTTP 429 from Google Drive (`userRateLimitExceeded` / `rateLimitExceeded`), Dropbox (`too_many_requests` / `too_many_write_operations`), Microsoft Graph (429 + `Retry-After` header), or any WebDAV server returning 429. The right response is to pause **every** in-flight upload on the same shared deadline. Per-file retries here just compound the rate-limit pressure: N parallel workers each retrying 3× sends 3N more requests into a server that's already pushing back.

#### Shared backoff coordinator

When any worker classifies a failure as `RATE_LIMIT_GLOBAL`, it signals a process-wide `BackoffCoordinator` (`claude_mirror/retry.py`). Every other worker checks the coordinator at the top of its next upload attempt — if a window is active, the worker blocks on a shared `threading.Condition` until the deadline elapses. The user sees one calm message instead of N transient warnings:

```
  ⚠  Backend reports rate limit. Pausing 30s before retrying.
  ...
  Throttle cleared. Resuming uploads.
```

The window is sized from the server's `Retry-After` header (OneDrive consistently sends one; Drive and Dropbox sometimes do; WebDAV varies). When no value is supplied, the coordinator uses a **30s** default. If a second `RATE_LIMIT_GLOBAL` fires while a window is still active, the deadline escalates by 1.5× (30s → 45s → 67.5s → ...), capped at `max_throttle_wait_seconds` (default **600s** = 10 minutes). Once the deadline passes with no further signals, the throttled state clears and uploads resume.

#### Lowering the cap for cron jobs

A cron-driven push that hits a hard quota would otherwise sit blocked for the full 10-minute cap. For fail-fast cron behaviour, set `max_throttle_wait_seconds` low in the project YAML — the next cron tick will retry naturally:

```yaml
# in your project YAML
max_throttle_wait_seconds: 60     # default 600
```

#### What the coordinator does NOT do

The coordinator is purely a **timing primitive**. It does not decide whether a particular failure should be retried — that's still the per-class state machine in `manifest.update_remote(...)` (`pending_retry` for retryable classes, `failed_perm` for AUTH / QUOTA / PERMISSION). It does not change classification semantics for any other ErrorClass. TRANSIENT, AUTH, QUOTA, PERMISSION, FILE_REJECTED all behave exactly as before — the coordinator only affects the WHEN of a retry, never the WHAT.

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

## Credentials profiles

For users running multiple projects through the same account (one Google account → 5 projects, one Dropbox app → 3 projects, etc.), the `--profile NAME` flag and the `claude-mirror profile` subcommand group let credential-bearing fields (`credentials_file`, `token_file`, `dropbox_app_key`, `onedrive_client_id`, WebDAV creds, SFTP host/key) live in one shared YAML at `~/.config/claude_mirror/profiles/<name>.yaml`. Project YAMLs reference the profile by name and inherit those fields.

See [docs/profiles.md](profiles.md) for the full walkthrough — sample profile YAMLs for every backend, the project-wins-over-profile precedence rule, the `profile create` / `list` / `show` / `delete` subcommands, and common workflows (one work account + 5 projects; one personal Google + one work Google sharing a single laptop). Profiles are optional — a single-project setup never needs them.

### Destructive ops are dry-run by default

Six commands can permanently delete data: `forget`, `prune`, `gc`, `delete`, `migrate-snapshots --no-keep-source`, and `profile delete`. All six follow the same convention:

1. **No flag → dry-run.** The command prints what would be deleted, exits 0, and changes nothing on disk or remote.
2. **`--delete` → arms the action**. The command asks you to type the literal word `YES` (uppercase, exact). Anything else aborts.
3. **`--delete --yes` → skips the typed-`YES` prompt.** Required for non-interactive scripts and CI.

This keeps a careless `claude-mirror forget --keep-last 5` (without `--delete`) safe by default, and the typed-`YES` gate prevents a stuck-shell autocomplete from triggering a real delete. Same convention applies to `claude-mirror profile delete NAME` — no flag is a dry-run, `--delete` plus typed `YES` actually removes the profile YAML.

---

## Auto-start the watcher

Use `claude-mirror watch-all` to watch every project in a single process. It auto-discovers all configs in `~/.config/claude_mirror/` and starts one notification listener per project, each in its own thread. Projects using different backends are handled transparently — each thread picks the right notifier for its backend (Pub/Sub for Google Drive, long-polling for Dropbox, periodic polling for OneDrive, WebDAV, and SFTP):

```bash
claude-mirror watch-all
```

For cron-driven setups that prefer a polling tick over a long-running daemon, the single-project `watch` command supports `--once`. One cron line, no service to manage:

```cron
*/5 * * * * /usr/local/bin/claude-mirror watch --once --quiet --config ~/.config/claude_mirror/myproject.yaml
```

Each `--once` run does exactly one polling cycle, dispatches any inbox events, then exits 0. A persistent watermark in `~/.config/claude_mirror/watch_once_state/` ensures successive runs only surface events that arrived since the previous tick — the very first run after install captures the current log tail and emits nothing, so a fresh cron install does not flood you with weeks of historical events. `--quiet` suppresses the startup banner so cron emails only contain real news. See `claude-mirror watch --help` for the full flag list.

### Is the watcher actually running?

If notifications stop arriving, the first thing to check is whether `watch-all` is still up. claude-mirror prints a yellow `⚠ watcher not running` banner before most subcommands when it isn't, but the explicit checks below are useful in scripts, in remote sessions, or when the banner has been suppressed (`--json`, `--quiet`):

**macOS (launchd, after `claude-mirror-install`):**
```bash
launchctl list | grep claude-mirror     # exit 0 with a numeric PID = running
tail -n 50 ~/Library/Logs/claude-mirror-watch.log  # what it did most recently
launchctl kickstart -k gui/$(id -u)/com.claude-mirror.watch  # restart it
```

**Linux (systemd user unit, after `claude-mirror-install`):**
```bash
systemctl --user status claude-mirror-watch
journalctl --user -u claude-mirror-watch -n 50 --no-pager
systemctl --user restart claude-mirror-watch
```

**Any platform — manual / one-shot:**
```bash
pgrep -f "claude-mirror watch-all"      # PID(s) if running, exit 1 if not
claude-mirror watch-all                 # foreground run; Ctrl+C to stop
```

**Windows:** `watch-all` is fully supported on Windows since v0.5.59 — same one-process-per-config model as POSIX. Hot-reload uses a sentinel file (`%USERPROFILE%\.config\claude_mirror\.reload_signal`) the daemon polls every 2 seconds, so `claude-mirror reload` works the same way it does on macOS / Linux. The inbox file lock that serializes concurrent watcher threads is also cross-platform now (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX), so the previous v0.5.54 caveat about Windows watchers losing lines under concurrent drains is closed — the strict TOCTOU regression test runs on every platform. To start it on login, use Task Scheduler:

```powershell
schtasks /Create /SC ONLOGON /TN "claude-mirror watch-all" /TR "claude-mirror watch-all" /RL HIGHEST /F
schtasks /Run /TN "claude-mirror watch-all"          # start it now
tasklist /V /FO CSV /NH | findstr "claude-mirror"   # verify it's running
```

If the process is alive but a specific project still isn't getting events, run `claude-mirror reload` to ask the running watcher to re-scan `~/.config/claude_mirror/` for new configs (the request lands within ~2 seconds via the sentinel-file poll). If that doesn't help, [`claude-mirror doctor`](#doctor) will tell you whether the project's backend itself is reachable and whether the notification channel (Drive Pub/Sub, Dropbox cursor, OneDrive/WebDAV/SFTP poll loop) is configured correctly.

### Unattended sync via cron

`claude-mirror watch --once` only PULLS remote changes — it never pushes local edits and never resolves conflicts. For a fully bidirectional cron-driven flow (push local edits, pull remote edits, auto-resolve any conflicts) use `claude-mirror sync --no-prompt --strategy ...`:

```cron
# Hourly cron-driven sync. Local always wins on conflict — fits a workflow
# where the cron host is the authoritative editing machine. Every auto-
# resolution is logged to `_sync_log.json` on the remote with the strategy
# that won, so you can audit overwrites later.
0 * * * * cd /Users/alice/projects/myproject && /usr/local/bin/claude-mirror sync --no-prompt --strategy keep-local

# Every 15 minutes, remote always wins — fits a workflow where the cron
# host is a passive backup target and the canonical edits happen
# elsewhere (collaborator's laptop, web UI, etc.).
*/15 * * * * cd /Users/alice/projects/myproject && /usr/local/bin/claude-mirror sync --no-prompt --strategy keep-remote
```

Output is one yellow line per auto-resolved file plus a trailing one-line `Summary:` so cron mail / `journalctl` is grep-friendly:

```
⚠  CLAUDE.md: auto-resolved (keep-local)
Summary: 47 in sync, 2 pushed, 1 pulled, 1 conflict auto-resolved (keep-local).
```

`--strategy keep-local` overwriting remote IS destructive in the operator's mind (the same way `--force-local` is). The flag combination IS the consent — there is no extra typed-`YES` gate the way there is on `forget` / `prune` / `gc`. But every auto-resolved file is logged in `_sync_log.json` with its winning strategy, so an audit pass can spot every overwrite after the fact via `claude-mirror log --limit 100`.

If you accidentally run `claude-mirror sync` (no flags) under cron, the command detects the non-TTY stdin and fails fast with a hint pointing at the right flag combination, rather than hanging on a prompt that will never be answered. See [`docs/cli-reference.md#sync`](cli-reference.md#sync) for the full flag table.

To watch a specific subset:

```bash
claude-mirror watch-all --config ~/.config/claude_mirror/work-a.yaml \
                      --config ~/.config/claude_mirror/personal-b.yaml
```

### Adding a new project to a running watcher

When you create a new project with `claude-mirror init`, the running watcher is notified automatically and picks up the new config without restarting. You can also trigger a reload manually:

```bash
claude-mirror reload
```

This writes a sentinel file (`~/.config/claude_mirror/.reload_signal`) that the running `watch-all` daemon polls every 2 seconds. On the next tick the daemon re-scans `~/.config/claude_mirror/` for new config files and starts watcher threads for any it doesn't already have. Existing watchers are not interrupted, and worst-case reload latency is the poll cadence (2 s). The mechanism is cross-platform — same behaviour on macOS, Linux, and Windows; no signals or `pgrep` involved.

### Recommended: `claude-mirror-install`

If you already ran `claude-mirror-install` in Part 2 Step 3, the watcher service is set up and running — nothing else to do. Otherwise:

```bash
claude-mirror-install
```

It detects your platform automatically, creates the appropriate service file, and loads it immediately. The watcher will restart on login and on failure. To remove the service:

```bash
claude-mirror-install --uninstall
```

### Shell tab-completion

`claude-mirror-install` also installs tab-completion for your shell (auto-detected from `$SHELL`, with PowerShell as the Windows default). After install, `claude-mirror <TAB>` enumerates subcommands and `claude-mirror init --backend <TAB>` enumerates the supported storage backends.

Since v0.5.50 the `--backend` value list is **dynamic**: the completion script calls back into `claude-mirror _list-backends` (a hidden subcommand) at every tab-press, so when a future release adds a new backend, it appears automatically without re-sourcing the completion. The cold-start cost of the callback is roughly 50 ms (Python + Click startup); warm-cache around 10 ms — fast enough that there is no static-fallback flag.

To regenerate the completion script manually for one shell:

```bash
# zsh — append to ~/.zshrc
eval "$(claude-mirror completion zsh)"

# bash — append to ~/.bashrc
eval "$(claude-mirror completion bash)"

# fish — write to the completions directory
claude-mirror completion fish > ~/.config/fish/completions/claude-mirror.fish

# PowerShell — append to your profile
claude-mirror completion powershell | Out-File -Encoding utf8 -Append $PROFILE.CurrentUserAllHosts
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

## `.claude_mirror_ignore` — project-tree exclusions

Drop a `.claude_mirror_ignore` file at the project root for gitignore-style per-project exclusions that complement the YAML `exclude_patterns` list. The file is optional — if absent, behaviour is unchanged.

```text
# Lines beginning with `#` are comments. Blank lines are skipped.

# Glob patterns (gitignore subset)
*.log
secret.env
**/*.bak

# Anchored at project root (leading `/`)
/build
/dist/

# Directory-only rules (trailing `/`)
node_modules/
__pycache__/

# Re-include with `!` (last matching rule wins)
docs/drafts/*.md
!docs/drafts/published.md
```

Syntax summary:

- `*` matches any characters except `/`.
- `**` matches any number of path segments (gitignore-style, matches zero or more).
- `?` matches a single character except `/`.
- `[abc]` is a character class; `[!abc]` negates the class (gitignore convention, rewritten internally to `[^abc]`).
- A trailing `/` makes the rule directory-only — it matches only when the rule resolves to a parent directory of the candidate path.
- A leading `/` anchors the rule at the project root; without it the rule matches anywhere in the tree.
- A leading `!` is a re-include — the last matching rule wins, so a `!` rule re-includes a path that an earlier rule excluded.

Precedence: rules from `.claude_mirror_ignore` apply IN ADDITION to YAML `exclude_patterns` — both layers must vote "keep" for a file to be eligible. A path excluded by either system is filtered out before hashing or upload.

Reload: the file is parsed once per command invocation. Edit it, run any subsequent `claude-mirror status` / `push` / `sync` and the new rules apply. The background watcher re-reads it on its existing config-reload cadence (the sentinel-file polling check, default 2 s), so you do not need to restart `watch-all`.

The `.claude_mirror_ignore` file itself is auto-excluded from sync — the rules do not propagate to other machines unless you explicitly add the file to `file_patterns` (which you almost certainly should not). This mirrors the gitignore convention.

For broader selective-sync guidance — picking what to mirror in the first place — see [Scenario F in scenarios.md](scenarios.md#f-selective-sync).

## Notifications

claude-mirror posts every sync event (push / pull / sync / delete) to one or more **chat / automation backends** AND surfaces native **desktop banners** on the running watcher's machine. All channels are **per-project**, **opt-in**, and **best-effort**: a notification failure (network error, bad URL, 4xx, 5xx, missing notification daemon) never blocks or fails a sync. The Slack integration is the most feature-rich (rich blocks, per-backend Tier 2 status, ACTION REQUIRED alerts on permanent failures); the other webhooks are simpler one-shot posts.

Multiple channels can be enabled simultaneously on the same project — every enabled channel fires on every event, in sequence. One channel's failure does not stop the others.

| Channel | When to pick it | URL / setup |
|---|---|---|
| **Slack** | Team chat in Slack; want rich-block formatting + per-mirror status + permanent-failure alerts | `https://hooks.slack.com/services/T.../B.../...` |
| **Discord** | Team chat in Discord; want a coloured embed card per event | `https://discord.com/api/webhooks/{id}/{token}` |
| **Microsoft Teams** | Team chat in Teams; legacy connector or modern Workflows webhook | `https://outlook.office.com/webhook/...` or `https://{tenant}.webhook.office.com/...` |
| **Generic webhook** | Wiring claude-mirror into n8n / Make / Zapier / a custom dashboard / an internal Slack-replacement | Any URL — claude-mirror POSTs a schema-stable JSON envelope and lets you add custom auth headers |
| **Desktop banners** | Native macOS / Linux / Windows notifications on the local machine that runs `claude-mirror watch-all` | No URL — `claude-mirror test-notify` verifies; setup details below |

### Desktop notifications

Run the built-in test command to verify the channel and see platform-specific setup instructions:

```bash
claude-mirror test-notify
```

#### macOS

Notifications use `osascript display notification`. macOS requires the calling application to have notification permission granted explicitly.

**Steps:** run `claude-mirror test-notify` from Terminal (or iTerm2) → open **System Settings → Notifications** → find **Terminal** → enable **Allow Notifications** → set alert style to **Alerts** or **Banners**.

**Running as a launchd service:** when the watcher runs as a launchd agent it has no app bundle, so the system cannot create a notification permission entry for it. Workaround: run `claude-mirror watch` once from a regular Terminal window, grant permission to Terminal, then switch back to the launchd service.

#### Linux

Notifications use `notify-send` (libnotify). Install if missing:

```bash
sudo apt install libnotify-bin   # Debian / Ubuntu
sudo dnf install libnotify       # Fedora
```

A notification daemon must be running — most desktop environments (GNOME, KDE, XFCE) include one automatically.

**Running as a systemd service:** if the service has no access to the display session, add to the unit:

```ini
[Service]
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
```

Replace `1000` with your user ID (`id -u`).

#### Windows

Notifications use the platform's native toast API via `plyer`. No additional setup is usually needed; if `claude-mirror test-notify` reports a missing module on Python 3.11+, reinstall claude-mirror with `pipx install --force claude-mirror`.

### Filtering which events fire

claude-mirror exposes four independent dials so you can tune notifications from "everything everywhere" down to "only deletes under `secrets/**`, and only on the security channel". Apply them in this order; later filters operate on whatever the earlier ones let through.

| Dial | Scope | Where it lives | What it controls |
|---|---|---|---|
| `*_enabled: false` | Whole backend | Top-level config field (e.g. `slack_enabled`, `discord_enabled`) | Turns the entire integration off — no events of any kind reach that backend |
| `exclude_patterns` / `.claude_mirror_ignore` | Whole sync | Project-tree exclusions (see [`.claude_mirror_ignore`](#claude_mirror_ignore--project-tree-exclusions)) | Files matching here never enter sync at all, so they never appear in any event payload either |
| Route `on:` list | Per route | Inside a `*_routes` entry, e.g. `on: [push, delete]` | Drops the entire event if `event.action` isn't in the list — useful for "deletes go to a quieter channel" |
| Route `paths:` list | Per route | Inside a `*_routes` entry, e.g. `paths: ["secrets/**"]` | Filters `event.files` to those matching at least one glob; if zero files match, the route is skipped — useful for "security channel only sees the security subtree" |

Heartbeat events (`sync` runs that found nothing to do) carry no files, so they bypass `paths:` and always fire on a route whose `on:` list includes `sync` — that is intentional, so a watching channel still gets a periodic "I'm alive" beat.

The legacy single-channel form (`slack_webhook_url` / `discord_webhook_url` / etc. without a `*_routes` block) only honours `*_enabled` and `exclude_patterns` — `on:` and `paths:` are list-form-only. See [Multi-channel routing per project](#multi-channel-routing-per-project) for the full schema.

### Slack

Slack notifications are **per-project** and **opt-in**. They fire on claude-mirror sync events (push / pull / sync / delete) — independent of any git/GitHub commit notifications you may have set up. Slack's payload is the richest of the four webhook backends: it includes the per-backend status block (Tier 2 multi-backend) and an `ACTION REQUIRED` header on permanent failures.

#### Step 1 — Create the Slack incoming webhook

You need an **Incoming Webhook URL** that points at a specific Slack channel. Each webhook is bound to one channel at creation time; if you want to post to two channels, generate two webhooks.

1. **Open the Slack API page** at [api.slack.com/apps](https://api.slack.com/apps) → click **Create New App** → **From scratch**. Name it `claude-mirror` (or anything; it appears as the message author in Slack), pick the workspace, click **Create App**.
2. **Enable Incoming Webhooks** — left sidebar → **Incoming Webhooks** → toggle **Activate Incoming Webhooks** to **On**.
3. **Add a webhook to a specific channel** — scroll to the bottom → **Add New Webhook to Workspace** → pick the channel → **Allow**. For a private channel: invite the app first (`/invite @claude-mirror`).
4. **Copy the webhook URL.** It looks like `https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx`. Treat it as a secret.

#### Step 2 — Enable Slack in claude-mirror

The `init --wizard` flow offers to enable Slack and asks for the webhook. Or pass the flags non-interactively:

```bash
claude-mirror init \
  --project /path/to/project \
  --backend googledrive \
  --slack \
  --slack-webhook-url 'https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx' \
  --slack-channel '#claude-mirror'
```

Quote the webhook URL in single quotes — the `&` / `/` characters in the URL must not be interpreted by your shell.

To enable Slack on an already-initialized project, edit the project YAML directly:

```yaml
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx
slack_channel: "#claude-mirror"      # optional override
```

#### Step 3 — Verify

Trigger any sync event from the project directory (`echo test >> CLAUDE.md && claude-mirror push CLAUDE.md`). You should see a Rich-formatted message in the channel within seconds, naming who pushed what, the snapshot timestamp + format, and the file count for the project. The file list is capped at 10 entries; longer pushes show `… and N more` after the cap. If a push or sync touched files but no snapshot was created (rare), the context line shows a no-snapshot warning so the recovery-point gap is visible.

Slack failures (network error, 4xx, 5xx, malformed webhook) are logged and silently swallowed — they will **never** block or fail a sync.

#### Slack config fields

| Field | Type | Purpose |
|---|---|---|
| `slack_enabled` | bool | Master switch. `false` (default) disables all Slack posts. |
| `slack_webhook_url` | str | Incoming-webhook URL from Slack's Apps directory. |
| `slack_channel` | str (optional) | Override the channel the webhook posts to. Honoured only if your workspace allows webhook channel-overrides (per-workspace setting, off by default since 2018). |

Two channels for the same project? Generate two webhooks under the same app and configure two project YAMLs that point at the same `project_path`. Two projects on the same machine post to different channels naturally — each YAML carries its own `slack_webhook_url`. For richer multi-channel routing (per-event-type, per-path globs), see [Multi-channel routing per project](#multi-channel-routing-per-project).

### Discord

1. In your Discord server: **Server Settings → Integrations → Webhooks → New Webhook**, pick the target channel, copy the **Webhook URL**.
2. Add to your project YAML:

```yaml
discord_enabled: true
discord_webhook_url: https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz1234567890
```

Each event renders as a single embed card: green stripe for `push`, blue for `pull` / `sync`, red for `delete`. The card carries Action / User / Machine / Project / Files fields; the file list is capped at 10 entries with an `and N more` sentinel for larger pushes.

### Microsoft Teams

Two URL forms work — both accept the **MessageCard** schema:

- **Legacy Office 365 connector** (`https://outlook.office.com/webhook/...`) — set up via the channel's `...` menu → **Connectors → Incoming Webhook**. Microsoft has been deprecating connectors; new tenants may not be able to create them.
- **Workflows-based webhook** (`https://{tenant}.webhook.office.com/...`) — the recommended modern path. Use Power Automate's "Post to a channel when a webhook request is received" template and copy the resulting URL.

Add to your project YAML:

```yaml
teams_enabled: true
teams_webhook_url: https://outlook.office.com/webhook/abcd1234-5678-90ab-cdef-1234567890ab/IncomingWebhook/0123456789abcdef0123456789abcdef/abcd1234-5678-90ab-cdef-1234567890ab
```

Each event renders as a single MessageCard. The `themeColor` matches Discord's colour logic (green / blue / red); the activity title carries the headline; facts list breaks out Action / User / Machine / Project; the body holds the file list (capped at 10 with `and N more`).

### Generic

Use this for any HTTP endpoint that accepts a JSON `POST` body — n8n, Make, Zapier, a custom internal service, etc. claude-mirror sends a **schema-stable v1 envelope**:

```json
{
  "version": 1,
  "event": "push",
  "user": "alice",
  "machine": "laptop",
  "project": "myproject",
  "files": ["memory/notes.md", "CLAUDE.md"],
  "timestamp": "2026-05-08T12:00:00+00:00"
}
```

The schema is additive-only: future versions will add fields, never rename or remove the ones above, so a downstream consumer pinned to v1 keeps working. Add to your project YAML:

```yaml
webhook_enabled: true
webhook_url: https://n8n.example.com/webhook/claude-mirror-sync
webhook_extra_headers:
  Authorization: Bearer your-static-token-here
  X-Tenant-ID: tenant-42
```

Every header in `webhook_extra_headers` is set on the outgoing request, so this is also how you attach a Bearer token, a custom routing header, or anything else your endpoint requires.

### Config-field summary

| Field | Type | Default | Purpose |
|---|---|---|---|
| `slack_enabled` | bool | `false` | Master switch for Slack posts. |
| `slack_webhook_url` | str | `""` | Slack incoming-webhook URL. |
| `slack_channel` | str | `""` | Optional Slack channel override. |
| `slack_template_format` | dict[str,str] / null | `null` | Per-action message templates for Slack (see [Per-event message templating](#per-event-message-templating)). |
| `discord_enabled` | bool | `false` | Master switch for Discord posts. |
| `discord_webhook_url` | str | `""` | Discord incoming-webhook URL. |
| `discord_template_format` | dict[str,str] / null | `null` | Per-action message templates for Discord. |
| `teams_enabled` | bool | `false` | Master switch for Teams posts. |
| `teams_webhook_url` | str | `""` | Teams incoming-webhook URL (legacy connector or Workflows). |
| `teams_template_format` | dict[str,str] / null | `null` | Per-action message templates for Teams. |
| `webhook_enabled` | bool | `false` | Master switch for the generic JSON webhook. |
| `webhook_url` | str | `""` | Arbitrary `POST` target for the generic envelope. |
| `webhook_extra_headers` | dict / null | `null` | Extra HTTP headers for the generic webhook (auth tokens, tenant IDs). |
| `webhook_template_format` | dict[str,dict] / null | `null` | Per-action **structured** templates for the generic webhook — values merged on top of the v1 envelope (see [Per-event message templating](#per-event-message-templating)). |

All four are independent — enable Slack and Discord and Generic together if you want; each runs on every event.

### Multi-channel routing per project

Since v0.5.50, every backend supports an optional **list-form** that replaces the single-channel `*_webhook_url` field with a list of routes. Each route names its own webhook URL, an event-type filter (`on`), and a path-glob filter (`paths`). Routes within a backend fire sequentially; matched routes get a notifier instance built from their own URL — different routes never share a notifier.

The list-form is optional and additive — every project YAML written before v0.5.50 keeps working with zero changes. The legacy single-channel field stays the easy on-ramp for "send all events to one channel". The list-form is the right tool the moment you want push notifications and delete notifications going to different channels, or want to scope a route to files under a particular subtree.

#### Schema

Each `*_routes` field is a list of dicts with three keys:

```yaml
slack_routes:
  - webhook_url: https://hooks.slack.com/services/T1/B1/abcd...   # required, non-empty string
    on: [push, sync]                                              # default: [push, pull, sync, delete]
    paths: ["**/CLAUDE.md", "memory/**"]                          # default: ["**/*"]
```

Same shape applies to `discord_routes`, `teams_routes`, and `webhook_routes`.

- `webhook_url` is required. Missing or empty → `ValueError` at config load (not at first-event-fire — typos surface immediately on `claude-mirror init` / `status` / `push`).
- `on` is a list of action strings from the closed set `{"push", "pull", "sync", "delete"}`. An unknown action → `ValueError`. Defaults to all four actions.
- `paths` is a list of glob strings parsed by Python's `fnmatch.fnmatchcase` — the same engine as `file_patterns` and `exclude_patterns`. Defaults to `["**/*"]`. **Note**: `**/*` matches any file with at least one path separator (e.g. `docs/notes.md`) but NOT a top-level file (e.g. `CLAUDE.md`). To catch top-level files, use `*.md` or list `CLAUDE.md` explicitly.

#### Precedence: list-form wins over legacy

If a project sets BOTH `slack_webhook_url` (legacy) AND `slack_routes` (list-form), the list-form wins. The legacy field is silently dropped from dispatch and a yellow info line is printed at engine startup:

```
ignoring slack_webhook_url because slack_routes is set
```

This is intentional rather than a hard error — the user may be in a transition. Same precedence rule applies to `discord_*`, `teams_*`, and the generic `webhook_*` pair.

#### Filter semantics

For each route, on every event, claude-mirror does:

1. **Action filter.** If `event.action` is not in the route's `on` list, skip the route.
2. **Path filter.** Filter `event.files` to those matching at least one of the route's `paths` globs (via `fnmatch.fnmatchcase`). If NO files match, skip the route entirely. If SOME match, construct a route-scoped event (`event.files` trimmed to the matching subset) and fire the notifier.
3. **Heartbeat events** (no files at all) bypass the path filter and always fire — a `sync` that found nothing to do still surfaces as a status heartbeat for routes that subscribe to `sync`.

The original event is never mutated. Concurrent backends each derive their own scoped view, so a Slack route narrowing files to `secrets/**` does not affect what Discord sees.

#### Worked examples

**Per-event-type routing — push to a busy channel, delete to a quieter one:**

```yaml
slack_routes:
  - webhook_url: https://hooks.slack.com/services/T/B/firehose
    on: [push, sync, pull]
  - webhook_url: https://hooks.slack.com/services/T/B/deletes
    on: [delete]
```

**Per-path routing — send `secrets/**` and `infra/**` events to a security channel, everything else to general:**

```yaml
slack_routes:
  - webhook_url: https://hooks.slack.com/services/T/B/security
    paths: ["secrets/**", "infra/**"]
  - webhook_url: https://hooks.slack.com/services/T/B/general
    paths: ["**/*"]
```

(A push touching files in both subtrees fires both routes — each with its own scoped event listing only the files that matched its filter.)

**Combined — security channel sees only deletes under `secrets/**`, everything else goes to general:**

```yaml
discord_routes:
  - webhook_url: https://discord.com/api/webhooks/sec
    on: [delete]
    paths: ["secrets/**"]
  - webhook_url: https://discord.com/api/webhooks/general
    on: [push, pull, sync, delete]
    paths: ["**/*"]
```

#### Config-field summary (multi-channel routing)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `slack_routes` | list[dict] / null | `null` | Multi-channel Slack routing. Wins over `slack_webhook_url` when both set. |
| `discord_routes` | list[dict] / null | `null` | Multi-channel Discord routing. Wins over `discord_webhook_url` when both set. |
| `teams_routes` | list[dict] / null | `null` | Multi-channel Teams routing. Wins over `teams_webhook_url` when both set. |
| `webhook_routes` | list[dict] / null | `null` | Multi-channel generic-webhook routing. Wins over `webhook_url` when both set. Per-route `extra_headers` is supported as an additive key. |

Each list element shape: `{webhook_url: str, on: list[str], paths: list[str]}`.
### Per-event message templating

Every backend has a built-in payload format (Slack rich blocks, Discord embed, Teams MessageCard, Generic v1 JSON envelope). When the defaults don't match your team's wording — locale-specific phrasing, internal jargon, custom emoji conventions, per-tenant routing fields — you can override the **summary line** of each backend on a **per-action** basis without forking claude-mirror.

Add one or more of these fields to your project YAML:

```yaml
slack_template_format:
  push:   ":up: {user}@{machine} pushed {n_files} file(s) to {project}"
  sync:   ":arrows_counterclockwise: {user}@{machine} synced {project}"
  delete: ":wastebasket: {user}@{machine} deleted {n_files} file(s) from {project}"

discord_template_format:
  push:   "**{user}** pushed {n_files} files to **{project}**"
  delete: "{user} deleted: {file_list}"

teams_template_format:
  push:   "{user}@{machine} pushed {n_files} file(s)"

# Generic webhook templates are STRUCTURED — each value is a dict of
# format strings, merged on top of the v1 envelope.
webhook_template_format:
  push:
    custom_field_1: "{user}@{machine}"
    project_alias:  "{project}"
    file_count:     "{n_files}"
```

All four fields are **optional**, **per-action**, and **opt-in**. Every existing project YAML keeps working with zero changes — only actions you list in the dict get templated; everything else uses the built-in format. The rendered template overrides only the prominent summary line; the file list, per-backend status block, snapshot context, and metadata fields all stay built-in so users keep the structured detail.

#### Available placeholder variables

claude-mirror uses Python's `str.format` (no Jinja2 / Mako / Mustache dependency — the standard library covers 95% of use cases). The following placeholders are available in every template:

| Placeholder | Source | Example |
|---|---|---|
| `{user}` | `event.user` | `alice` |
| `{machine}` | `event.machine` | `laptop` |
| `{project}` | `event.project` | `my-product-docs` |
| `{action}` | `event.action` | `push` / `pull` / `sync` / `delete` |
| `{n_files}` | `len(event.files)` | `3` |
| `{file_list}` | comma-separated, capped at 10 with `and N more` | `notes.md, CLAUDE.md, README.md` |
| `{first_file}` | `event.files[0]` if any, else empty string | `notes.md` |
| `{timestamp}` | `event.timestamp` (ISO 8601 UTC) | `2026-05-09T12:00:00+00:00` |
| `{snapshot_timestamp}` | snapshot ID if known, else literal `"unknown"` | `2026-05-09T11-58-31Z` |

#### Worked examples

**Per-team channel naming.** Two teams share one Slack workspace; they want the team initials in front of every message:

```yaml
slack_template_format:
  push: "[DOCS] :up: {user} pushed {n_files} files to {project}"
  sync: "[DOCS] :arrows_counterclockwise: {user} synced {project}"
```

**Locale-aware wording.** A Spanish-speaking team prefers Spanish notifications:

```yaml
slack_template_format:
  push:   ":up: {user} subió {n_files} archivos a {project}"
  pull:   ":down: {user} descargó {n_files} archivos de {project}"
  sync:   ":arrows_counterclockwise: {user} sincronizó {project}"
  delete: ":wastebasket: {user} eliminó {n_files} archivos de {project}"
```

**Internal jargon — first-file emphasis.** A team where most pushes touch a single doc wants that file front-and-centre:

```yaml
discord_template_format:
  push: "{user} updated **{first_file}** ({n_files} total)"
```

**Generic webhook with per-tenant routing key.** An n8n workflow demultiplexes incoming events by a custom `tenant_id` field that the v1 envelope doesn't ship:

```yaml
webhook_template_format:
  push:
    tenant_id:    "tenant-acme"
    pushed_by:    "{user}@{machine}"
    project_path: "{project}"
    file_count:   "{n_files}"
```

The rendered template values are merged on top of the v1 envelope — `pushed_by` becomes a new field, `tenant_id` is a new literal, and the schema-stable `version` / `event` / `timestamp` keys keep their default values (your downstream consumer pinned to v1 keeps working).

#### What happens on a bad template

If a template references an unknown placeholder (`{nonexistent}`), or if the format string itself is malformed, claude-mirror **does not crash the sync**. It logs a yellow info line —

```
warn  Notification template error (Discord, action='push'): 'nonexistent' — falling back to default format.
```

— and uses the built-in format for that one event. The next event with a valid template renders normally. This matches claude-mirror's broader "notification failures are best-effort, never block sync" contract.

Action keys outside `{push, pull, sync, delete}` are caught at config-load time with a clean `ValueError` — typing `delet:` instead of `delete:` surfaces immediately rather than silently being skipped at notify time.

### Who else is editing this project?

When more than one collaborator is actively syncing the same project, you often want a quick read on who has touched it recently — without piping `claude-mirror log` through `awk`. Run:

```bash
claude-mirror status --presence
```

This appends a `Recent collaborator activity (last 24h)` table below the usual sync-status output. The table aggregates the shared `_sync_log.json` on the backend into one row per `(user, machine)` tuple — newest first — with columns for the user, the machine, the last action (`push` / `pull` / `sync` / `delete`), a humanised "When" delta (`3m ago`, `2h ago`, `5d ago`), and up to five of the most recently-touched files for that pair.

Your own machine's entries are filtered by default; the section answers "who else is here?", not "what have I done?". When nobody else has been active in the last 24 hours, the table is replaced by a dim `No other collaborators active in the last 24 hours.` line.

`--presence` composes with `--watch N`: every refresh tick re-fetches the sync log and rebuilds the presence table along with the rest of the status renderable, so a long-running `status --watch 10 --presence` shows live "they just pushed" updates inline. It also composes with `--json`: the v1 envelope grows an additive `presence: [...]` key under `result` (schema v1.1; see [cli-reference.md — `status --presence --json`](cli-reference.md#status---presence---json-schema-v11-additive)). Existing `--json` consumers that don't ask for `--presence` see the unchanged v1 shape.

For the full chronological audit log (every push from every collaborator over the lifetime of the project, not just the last 24h), use [`claude-mirror log`](cli-reference.md#log).

### Activity stats over a window

Where `status --presence` answers "who is here right now?" and `log` is the raw chronological feed, [`claude-mirror stats`](cli-reference.md#stats) is the rolled-up companion: it aggregates the same shared `_sync_log.json` into a small table with one row per group key — by `user`, `machine`, `action`, `day`, or `backend` — over a window the caller picks (`--since 7d`, `--since 2026-04-01 --until 2026-04-30`, etc.).

```bash
claude-mirror stats --since 30d --by user           # top contributors over the last 30 days
claude-mirror stats --since 2w --by day --top 14    # daily activity pattern, two weeks
claude-mirror stats --by action                     # push vs. pull vs. sync vs. delete mix (default 7d window)
```

Default window is the last 7 days; default group-by axis is `backend`. The `--json` flag emits the same v1 envelope shape used by `status` / `log` / `snapshots` (additive v1.1 `result` shape — see [`stats --json`](cli-reference.md#stats---json-schema-v11-additive)).

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
- **`claude-mirror seed-mirror --backend NAME`** — populates a newly-added mirror with files that already exist on the primary. When you add a backend to `mirror_config_paths` for a project where files already exist, regular `push` has nothing to do (every local hash matches its manifest record), so push uploads zero files and the new mirror's folder stays empty. `seed-mirror` walks the manifest, finds every file with no recorded state on the named mirror, and uploads each one to that mirror only — the primary is never touched. Idempotent: safe to re-run; the second invocation is a no-op. Drift-safe: files whose local content has diverged from the manifest are skipped with a warning rather than seeded with mismatched content (run `push` first to reconcile primary, then re-run seed-mirror). Use `--dry-run` to preview which files would be seeded. Without `--backend`, seed-mirror auto-detects the candidate when exactly one mirror has unseeded files; ambiguous cases (zero or multiple) print a clear message and require `--backend NAME` explicitly.
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

For unattended monitoring (Uptime Kuma, Better Stack, Prometheus, Datadog, GitHub Actions matrix health checks), reach for [`claude-mirror health`](cli-reference.md#health) instead — it's the structured, fast sibling of doctor that emits a JSON envelope (`schema: v1`) and uses exit codes `0`/`1`/`2` for ok/warn/fail. Doctor is for humans diagnosing a problem; health is for monitoring tools polling every minute. Both share the same data sources (config, token, backend reachability, sync log) but the surfaces are tuned for different audiences — run them side-by-side.

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
| Credentials check skipped (inline in YAML or default chain) | webdav, sftp, s3 | info-only line, never a failure |
| Credentials check skipped (inline in YAML) | webdav, sftp, smb | info-only line, never a failure |

#### Tokens / inline auth material

| Check | Backends | Failure looks like |
|---|---|---|
| Token file exists, parses as JSON, and contains a `refresh_token` | googledrive, dropbox, onedrive | `token file missing` / `token file corrupt` / `token has no refresh_token` — fix is `claude-mirror auth --config PATH` (consent screen must be shown to issue a new refresh token) |
| `webdav_username` and `webdav_password` are non-empty in the YAML | webdav | `WebDAV credentials missing in config: PATH` |
| `sftp_host`, `sftp_username`, `sftp_folder`, plus at least one of `sftp_key_file` or `sftp_password` are set | sftp | `SFTP config incomplete (missing FIELDS): PATH` |
| `s3_bucket` is non-empty in the YAML; access key + secret are optional (boto3's default credential chain handles env vars / `~/.aws/credentials` / IAM role when blank) | s3 | `S3 config incomplete (s3_bucket): PATH` |
| `smb_server`, `smb_share`, `smb_username`, `smb_password`, and `smb_folder` are non-empty in the YAML | smb | `SMB config incomplete (missing FIELDS): PATH` |

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

### Drive deep checks

When `--backend googledrive` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `googledrive`), the doctor runs an additional six checks targeting failure modes that only show up on Google Drive. These complement the generic credentials/token/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| OAuth Drive scope granted | `OAuth Drive scope not granted` — fix is `claude-mirror auth --config PATH` and approve the Drive scope on the consent screen. Drive scope is required; without it, the rest of the deep section is short-circuited (no point cascading failures off a missing root scope) |
| OAuth Pub/Sub scope granted | If absent, doctor emits a yellow `⚠` info line and skips the remaining four Pub/Sub checks — Drive-only setups (no real-time notifications) are a valid degraded mode, so this is informational rather than a failure |
| Drive API enabled in the GCP project | `Drive API not enabled in GCP project PROJECT_ID` — parsed from Google's canonical "API has not been used in project X before or it is disabled" error string. Fix URL is templated with the project ID so the user clicks straight to the right enable page |
| Pub/Sub API enabled | `Pub/Sub API not enabled in GCP project PROJECT_ID` — same error-string parsing as Drive. Probed via `publisher.get_topic` so one RPC double-duties as both the API-enabled probe AND the topic-existence probe (check 4) |
| Pub/Sub topic exists at `projects/PROJECT/topics/TOPIC` | `Pub/Sub topic does not exist: PATH` — fix points at the topic-creation URL templated with the project ID |
| Per-machine subscription exists at `projects/PROJECT/subscriptions/TOPIC-MACHINE` | `Pub/Sub subscription does not exist for this machine: PATH` — fix is `claude-mirror auth --config PATH`, which creates the per-machine subscription if it's missing. The machine-name suffix is the value of `machine_name` in the YAML, lower-cased and dot/space-normalised to dashes |
| IAM grant: Drive's service account has `roles/pubsub.publisher` on the topic | `Drive service account missing publish permission on the topic` plus the explanatory line `Push events from THIS machine won't notify others.` and the fix `claude-mirror init --reconfigure-pubsub --config PATH`. This is the highest-value check — about 70% of self-serve Drive setups miss this grant. Pub/Sub appears to work (subscribe + publish from the user's own credentials succeeds), but Drive itself silently fails to publish change events, so other machines never receive notifications. The expected member is `serviceAccount:apps-storage-noreply@google.com` — Google Drive's push-notification service account |

#### Auth-failure bucketing

If the very first Pub/Sub admin call (`get_topic` or earlier) fails with `RefreshError`, `invalid_grant`, or `Unauthenticated`, the deep section emits ONE auth-bucket failure line (`Pub/Sub admin auth failed`) and skips the remaining checks. This avoids five identical "auth needed" lines for what is always the same root cause and the same fix (`claude-mirror auth --config PATH`).

#### Lazy import

The Pub/Sub admin SDK (`google.cloud.pubsub_v1`) is lazy-imported inside the deep-check function so the multi-hundred-millisecond gRPC import cost is only paid when `--backend googledrive` is actually exercising these checks. Generic `claude-mirror doctor` invocations on other backends remain fast.

#### When the deep section is skipped

- `gcp_project_id` or `pubsub_topic_id` empty in the YAML — doctor emits a yellow info line ("Pub/Sub not configured … real-time notifications won't work") and stops the deep section there. The user is using Drive without push notifications, which is a valid degraded mode.
- Token file missing — the generic Check 3 above already emitted a failure for this; doctor doesn't repeat it in the deep section.
- Pub/Sub OAuth scope not granted — see the second row of the table above.

#### Fixing a missing topic / subscription / IAM grant

If `doctor` reports a missing Pub/Sub topic, missing per-machine subscription, or missing IAM grant for Drive's service account, re-run `claude-mirror init --auto-pubsub-setup --config <path>` to fix all three in one step. The auto-setup helper (added in v0.5.47, documented in [backends/google-drive.md](backends/google-drive.md#auto-create-pubsub-topic--subscription--iam-grant---auto-pubsub-setup-since-v0547)) is idempotent: anything that already exists is left in place, anything missing is created using the OAuth credentials acquired by the wizard's smoke test. Re-running `doctor` afterwards should now report all six checks green.

### OneDrive deep checks

When `--backend onedrive` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `onedrive`), the doctor runs an additional set of checks targeting failure modes that only show up on OneDrive. These complement the generic credentials/token/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| Token cache integrity | `Token cache unreadable` (corrupt JSON / wrong shape) or `Token cache has no cached accounts` (cache exists but is empty). Fix is `claude-mirror auth --config PATH` to (re-)complete the device-code login. The MSAL token cache is a JSON document inside the `token_file`; we deserialize it via `msal.SerializableTokenCache` and confirm at least one cached account |
| Azure client_id format valid | `Azure client_id has invalid format: 'STRING'` — Azure Application (client) IDs are GUIDs in `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` format. Fix is to edit the YAML and set `onedrive_client_id` to your Azure App registration's Application (client) ID. Doctor surfaces this BEFORE attempting MSAL so the user doesn't see a cryptic "invalid client" error from deeper in the stack |
| Granted scopes match config | claude-mirror's OneDrive backend requests `Files.ReadWrite` (or `Files.ReadWrite.All` for shared OneDrive Business tenants). If the cached account's scopes don't include either, doctor emits a yellow warning "Scopes missing from cache: expected one of Files.ReadWrite, Files.ReadWrite.All" and suggests re-running `claude-mirror auth`. This is informational rather than fatal — the silent-token call below will settle it definitively |
| Token still refreshable | `acquire_token_silent(scopes, account)` is called against the cached account. If the result is `None`, contains an `error` key, or raises, the refresh token has expired or been revoked — AUTH bucket fail with `claude-mirror auth --config PATH` as the fix. The error code (`invalid_grant`, `AADSTS70008`, etc.) is surfaced verbatim so the user can match it against Microsoft's documentation |
| Drive item access | Microsoft Graph GET against `me/drive/root:/{onedrive_folder}`. 200 ⇒ folder exists and is reachable. 404 ⇒ "OneDrive folder doesn't exist; create it via the OneDrive web UI or run `claude-mirror push` to create it on first sync". 401 ⇒ AUTH bucket fail. 403 ⇒ permission failure (account lacks access to the folder). 5xx ⇒ TRANSIENT classification, "retry; check status.office.com for service incidents". Network failure ⇒ same TRANSIENT treatment |
| Drive item type | Confirms Graph returned a `folder` shape (not a `file`). If the configured `onedrive_folder` points at a file rather than a folder, sync would fail; doctor catches this up front. Per-file `quickXorHash` detection happens at sync time (the hash field appears on individual `DriveItem`s, not on folder metadata), so we don't probe individual files here — folder access alone is sufficient evidence the configuration is workable |

#### Auth-failure bucketing

If `acquire_token_silent` fails (returns None / error dict / raises), or if the Graph drive-item probe returns 401, the deep section emits ONE auth-bucket failure line (`OneDrive auth failed`) and skips the remaining checks. This avoids two-or-three identical "auth needed" lines for what is always the same root cause and the same fix (`claude-mirror auth --config PATH`).

#### Lazy import

The MSAL SDK (`msal`) is lazy-imported inside the deep-check function so the multi-hundred-millisecond import cost is only paid when `--backend onedrive` is actually exercising these checks. Generic `claude-mirror doctor` invocations on other backends remain fast.

#### When the deep section is skipped

- `onedrive_folder` empty in the YAML — doctor emits a yellow info line ("OneDrive folder not configured … skipping deep OneDrive checks") and stops the deep section there. The generic checks still run; the user is presumably mid-wizard.
- Token file missing — the generic Check 3 above already emitted a failure for this; doctor doesn't repeat it in the deep section.

### Sample successful output

```
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking googledrive backend (/home/alice/.config/claude_mirror/myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ OAuth scopes: Drive ✓, Pub/Sub ✓
  ✓ Drive API enabled in project myproject-prod
  ✓ Pub/Sub API enabled
  ✓ Pub/Sub topic exists: projects/myproject-prod/topics/claude-mirror-myproject
  ✓ Pub/Sub subscription exists for this machine: projects/myproject-prod/subscriptions/claude-mirror-myproject-workstation
  ✓ Drive service account has publish permission on the topic (apps-storage-noreply@google.com)
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

### Sample Drive deep-check failure output

```
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking googledrive backend (/home/alice/.config/claude_mirror/myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ OAuth scopes: Drive ✓, Pub/Sub ✓
  ✓ Drive API enabled in project myproject-prod
  ✓ Pub/Sub API enabled
  ✓ Pub/Sub topic exists: projects/myproject-prod/topics/claude-mirror-myproject
  ✓ Pub/Sub subscription exists for this machine: projects/myproject-prod/subscriptions/claude-mirror-myproject-workstation
  ✗ Drive service account missing publish permission on the topic
      Push events from THIS machine won't notify others.
      Fix: run claude-mirror init --reconfigure-pubsub --config /home/alice/.config/claude_mirror/myproject.yaml, or grant roles/pubsub.publisher to serviceAccount:apps-storage-noreply@google.com on topic projects/myproject-prod/topics/claude-mirror-myproject in the Cloud Console.
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✗ 1 issue(s) found. Fix the items above and re-run claude-mirror doctor.
```

### Dropbox deep checks

When `--backend dropbox` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `dropbox`), the doctor runs an additional six checks targeting failure modes that only show up on Dropbox. These complement the generic credentials/token/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| Token JSON shape — `access_token` (legacy long-lived) or `refresh_token` (PKCE) present | `Token JSON missing both access_token and refresh_token` — fix is `claude-mirror auth --config PATH` to refresh the token |
| App-key sanity — `dropbox_app_key` non-empty and matches `^[a-z0-9]{10,20}$` (Dropbox app keys are short alphanumeric strings) | `dropbox_app_key is empty in PATH` or `dropbox_app_key format invalid` — fix is to copy the App key from the Dropbox app's Settings tab at https://www.dropbox.com/developers/apps and update the YAML |
| Account smoke test — `users_get_current_account()` returns an Account with a populated `account_id` | `Account smoke test failed: REASON` — surfaces revoked tokens cleanly, since this is the first network call after auth. Fix is `claude-mirror auth --config PATH` |
| Granted scopes inspection — for PKCE tokens, `files.content.read` and `files.content.write` must both appear on the granted scope list | `Token missing required scope(s): files.content.write` — fix is to enable the missing scope on the Dropbox app's Permissions tab, click Submit, then re-run `claude-mirror auth --config PATH`. Legacy tokens (no `scope` field) emit a yellow info line "Legacy token format; scope inspection skipped" and skip this check rather than failing |
| Folder access — `files_list_folder(path=dropbox_folder, limit=1)` succeeds | `Folder not found in Dropbox: PATH` (create the folder via the Dropbox web UI / desktop client) or `Access denied on folder: PATH` (verify the folder is shared with the authenticated account and that the app has the read + write scopes) |
| Account type / team status — read from check 3's `FullAccount.account_type` (basic / pro / business) and `FullAccount.team` (None for personal accounts, non-None for team members) | Personal accounts: green ✓ info line "Account type: personal/pro/business". Team members: yellow info line about admin policies — team admins can disable third-party app access at the team level, which silently breaks sync |

#### Dropbox auth-failure bucketing

If `users_get_current_account` fails with `AuthError` (or an HTTP 401 from a generic `HttpError`), the deep section emits ONE auth-bucket failure line (`Dropbox auth failed`) and skips the remaining Dropbox checks (folder access, etc.). This avoids three identical "auth needed" lines for what is always the same root cause and the same fix (`claude-mirror auth --config PATH`).

#### Lazy import

The Dropbox SDK (`dropbox`) is lazy-imported inside the deep-check function so its tens-of-milliseconds import cost is only paid when `--backend dropbox` is actually exercising these checks. Generic `claude-mirror doctor` invocations on other backends remain fast.

### WebDAV deep checks

When `--backend webdav` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `webdav`), the doctor runs an additional six checks targeting failure modes that only show up on WebDAV servers. These complement the generic credentials/token/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| URL well-formed | `WebDAV URL malformed: URL` — fix is `https://host/path`-shaped URL in the YAML, or re-run `claude-mirror init --wizard --config PATH`. Empty `webdav_url` and bare `http://` (without `webdav_insecure_http: true`) are both rejected here, before any network call |
| PROPFIND on the configured root returns HTTP 207 | `PROPFIND failed: HTTP STATUS` — branch hints by status code: 401 → auth-bucket (verify `webdav_username` / `webdav_password`), 404 → "configured WebDAV root doesn't exist" (create the folder server-side or fix the URL), 405 → "server doesn't support PROPFIND" (typically a misconfigured endpoint serving plain HTTP, or a Nextcloud URL missing `/remote.php/dav/files/USER/`), 5xx → transient retry hint |
| DAV class detection | `no DAV class header reported by server` (info, not failure) — server may still work for basic ops. A header that lacks class 1 emits a yellow warning ("does NOT list class 1"); class 1, 2, 3 from Nextcloud is the canonical green case |
| ETag header presence | `no ETag returned` (info, not failure) — claude-mirror falls back to last-modified / content-md5 for change detection. Detected from either the `ETag:` response header OR the PROPFIND XML's `<d:getetag/>` field |
| oc:checksums extension support | `oc:checksums extension not advertised` (info, not failure) — Nextcloud / OwnCloud only. When advertised, the kinds (`MD5`, `SHA1`, `SHA256`, etc.) are listed inline so the user knows what their server exposes |
| Account-level PROPFIND for Nextcloud / OwnCloud-shaped URLs | `Account-level PROPFIND failed: HTTP 404` ⇒ "Account base unreachable" — the username segment in `webdav_url` is wrong. Skipped silently for non-Nextcloud-pattern URLs (Apache mod_dav, Synology, Box.com, etc.). Triggered only when the URL matches `https?://HOST/remote.php/dav/files/USER/...` |

#### Auth-failure bucketing

If the very first PROPFIND returns 401 (or the account-level PROPFIND does, but the root succeeded somehow), the deep section emits ONE auth-bucket failure line (`Credentials rejected. Verify webdav_username and webdav_password.`) and skips the remaining checks. This avoids duplicate "credentials rejected" copies for what is always the same root cause and the same fix (`claude-mirror auth --config PATH`).

#### Lazy import

The `requests` and `urllib.parse` modules are top-level imports already in the WebDAV backend, so the deep section adds no additional import cost. The XML parser and regex used for the Nextcloud-pattern URL detection are stdlib-only.

#### When the deep section is skipped

- Backend is not `webdav` — the deep section is gated on `backend_name == "webdav"`.
- `webdav_url` empty in the YAML — Check 1 surfaces this and bails.
- Token file absent AND no `webdav_password` in the YAML — generic Check 3 already flagged it; the deep section bails silently rather than emitting a duplicate complaint and a network call it can't authenticate.

### SFTP deep checks

When `--backend sftp` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `sftp`), the doctor runs an additional seven checks targeting failure modes that only show up on SFTP/SSH backends. These complement the generic credentials/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| Host fingerprint matches `~/.ssh/known_hosts` | `Host fingerprint mismatch in PATH` plus the explanatory line `POSSIBLE MAN-IN-THE-MIDDLE — refusing to connect.` and the fix `ssh-keygen -R HOSTNAME` (verify the new fingerprint out-of-band first). The fix-hint deliberately does NOT mention `claude-mirror auth` — fingerprint mismatches are not a token problem, they're a security incident. If the host isn't in known_hosts at all, doctor emits a yellow info line ("first connection will prompt to verify") and runs the rest of the checks |
| SSH key file exists + readable | `SSH key file not found: PATH` (fix: regenerate with `ssh-keygen` or fix the YAML) or `SSH key file not readable: PATH` (fix: `chmod 600 PATH`) |
| SSH key file permissions are 0600 | `Key file permissions too open: NNNN on PATH` plus the explanatory line `OpenSSH refuses keys readable by group or world.` and the fix `chmod 600 PATH`. Doctor uses `os.stat(...).st_mode & 0o077` to detect any group/world bits and does NOT auto-fix — chmod is a deliberate human action |
| SSH key can decrypt | If the key is encrypted, doctor emits a yellow info line ("ssh-agent or claude-mirror's auth flow handles this at sync time"), NOT a failure. Malformed/garbage key files surface as `Key file unparseable: PATH` with a regenerate fix-hint |
| Connection + auth succeeds | TCP connect failures classify as `Connection timed out` (fix: `ping HOST`, check port) or `Server unreachable` (fix: verify server is up and port is open). Auth rejections classify as `SSH authentication rejected` (fix: verify key/password and `~/.ssh/authorized_keys` on the server). Both auth-class failures bucket into ONE failure line — no cascading copies of the same root cause |
| `exec_command` capability | If `transport.open_session()` succeeds and the probe returns exit 0, doctor emits `exec_command available; server-side hashing will be used`. If it fails (typical of `internal-sftp`-jailed accounts), doctor emits a yellow info line `exec_command unavailable — client-side hashing fallback active`. Neither branch is a failure — both modes are fully supported, just with slightly different snapshot performance on large files |
| Root path access | `sftp.stat(sftp_folder)` succeeds → green ✓. NotFound → yellow info line ("claude-mirror creates it on first push"), NOT a failure. PermissionDenied → AUTH-bucket failure with a server-side ACL fix-hint mentioning the configured `sftp_username` |

#### Auth-failure bucketing

Auth-class failures (host fingerprint mismatch, auth rejected, root-path permission denied) all funnel through ONE auth-bucket — at most one of these fires per run, and the remaining checks are short-circuited so the user doesn't see five copies of "your access is broken" rooted in the same problem. The bucket fix-hint is contextual: fingerprint mismatch points at `ssh-keygen -R HOSTNAME` (NOT `claude-mirror auth` — fingerprint mismatches aren't a token problem), auth rejection points at the YAML + server-side `authorized_keys`, and permission-denied points at server-side ACLs.

#### Lazy import

`paramiko` is lazy-imported inside the deep-check function so the import cost is only paid when `--backend sftp` is actually exercising these checks. Generic `claude-mirror doctor` invocations on other backends remain fast.

#### When the deep section is skipped

- Backend is not `sftp` — the deep section is gated on `backend_name == "sftp"` and silently skipped for everything else.
- Generic Check 3 (SFTP credentials present in YAML) failed — the deep section still runs, but most checks degrade to "missing credentials" failures pointing back at the YAML.

### FTP deep checks

When `--backend ftp` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `ftp`), the doctor runs an additional six checks targeting failure modes that only show up on FTP / FTPS backends. These complement the generic credentials/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| Host reachable | `Connection to HOST:PORT timed out` (fix: `ping HOST`, check port) or `Server unreachable: HOST:PORT` (fix: verify the server is up and the control port is open). Cleartext-mode advisory: when `ftp_tls=off` and the configured host is not loopback / RFC1918, doctor emits a yellow info line warning that credentials travel UNENCRYPTED. When the host IS loopback / RFC1918 the line is softer (LAN-only use is the documented contract). |
| Server greeting + protocol banner | The 220-line greeting from the server is surfaced as info so you can confirm the backend is talking to the expected box (some shared-hosting providers run idiosyncratic banners that double as the first diagnostic signal). |
| TLS handshake | Active when `ftp_tls != "off"`. Surfaces the negotiated cipher + protocol version (TLSv1.2 / TLSv1.3) as info. Failures bucket as `TLS handshake failed against HOST:PORT` (fix: verify server certificate, or change `ftp_tls` mode). |
| Authentication | 530 from the server → AUTH-bucket failure `FTP authentication rejected` (fix: verify `ftp_username` / `ftp_password` in the YAML). Other transport errors during auth (transient, server bug) surface as `FTP transport error during auth`. |
| Folder access | `cwd ftp_folder` succeeded → green ✓. 550 with "no such" / "not found" → failure `Configured folder doesn't exist on the server` (fix: create it via the host's file manager / shell, or `claude-mirror auth` will mkdir it on first connect). 550 with "permission denied" → AUTH-bucket failure pointing at server-side ACLs. |
| Folder write | STOR a 1-byte sentinel file `__claude_mirror_doctor_test`, then DELE it. 550-permission → AUTH-bucket failure. 552 (storage exceeded) → `FTP server reported quota / storage limit` failure. Any successful path emits `Folder writable (STOR + DELE sentinel succeeded)`. |

#### Auth-failure bucketing

Auth-class failures (auth rejected, folder permission denied, write permission denied) all funnel through ONE auth-bucket — at most one of these fires per run, and the remaining checks are short-circuited so the user doesn't see three copies of "your access is broken" rooted in the same problem.

#### Cleartext-mode advisory

`ftp_tls: off` is supported but actively discouraged. The backend itself emits a stderr warning at every `authenticate()` call. The doctor adds an additional advisory: a loud warning when the configured host doesn't resolve to a loopback or RFC1918 address (i.e. cleartext FTP against an internet-reachable server), and a softer "host appears local" line when it does.

#### Stdlib-only

The FTP backend uses Python's stdlib `ftplib` — no third-party dependency. The deep-check function lazy-imports `ftplib`, `socket`, and `ssl` to keep generic doctor invocations on other backends fast.

#### When the deep section is skipped

- Backend is not `ftp` — the deep section is gated on `backend_name == "ftp"` and silently skipped for everything else.
- Generic Check 3 (FTP credentials present in YAML) failed — the deep section still runs, but most checks degrade to "missing credentials" failures pointing back at the YAML.
### S3 deep checks

When `--backend s3` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `s3`), the doctor runs an additional six checks targeting failure modes that show up on S3-compatible storage. These complement the generic credentials/connectivity loop above; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| Credentials shape | `s3_access_key_id set but s3_secret_access_key empty` (or vice versa) — fix is to add the missing field, OR blank both to fall back to boto3's default credential chain (env vars / `~/.aws/credentials` / IAM role) |
| Endpoint URL well-formed (when set) | `s3_endpoint_url is malformed` — fix is to use the form `https://<host>` (e.g. `https://s3.eu-central-003.backblazeb2.com` for Backblaze B2; `https://<account>.r2.cloudflarestorage.com` for Cloudflare R2) |
| Bucket reachable (`head_bucket`) | `Bucket NAME does not exist` (404 — fix is `aws s3 mb s3://NAME` or create the bucket in the provider's web UI), `S3 auth failed` (auth-bucketed; fix is to verify keys + re-run `claude-mirror auth`), `Could not reach S3 endpoint` (DNS / firewall — fix is to verify reachability), or `transient server error` (5xx — retry in a moment) |
| List permissions (`list_objects_v2 MaxKeys=1`) | `S3 list permissions denied (AccessDenied)` plus the IAM hint `grant the IAM principal s3:ListBucket on arn:aws:s3:::BUCKET` |
| Write permissions (`put_object` + `delete_object` of a 1-byte sentinel `__claude_mirror_doctor_test`) | `S3 write permissions denied (AccessDenied)` plus the IAM hint `grant s3:PutObject + s3:DeleteObject on arn:aws:s3:::BUCKET/PREFIX/*`. The sentinel is cleaned up immediately on success |
| Region consistency (when `s3_region` is set) | yellow `⚠ Region mismatch: configured X but bucket is in Y` — non-fatal warning. AWS may redirect or reject some operations on a mismatch; many S3-compat services tolerate it |

#### Auth-failure bucketing

Auth-class failures (`InvalidAccessKeyId`, `SignatureDoesNotMatch`, `AccessDenied`, `NoCredentialsError`, HTTP 401) all funnel through ONE auth-bucket — at most one of these fires per run, and the remaining checks (4-6) are short-circuited so the user doesn't see five copies of "your credentials are broken" rooted in the same problem. The bucket fix-hint points at the YAML's `s3_access_key_id` + `s3_secret_access_key` fields and at re-running `claude-mirror auth --config PATH`.

#### Lazy import

`boto3` and `botocore.exceptions` are lazy-imported inside the deep-check function (and inside `S3Backend` itself, function-locally) so the multi-tens-of-millisecond import cost is only paid when `--backend s3` is actually exercising these checks. Generic `claude-mirror doctor` invocations on other backends remain fast — the same v0.5.61 fusepy precedent.

#### When the deep section is skipped

- Backend is not `s3` — the deep section is gated on `backend_name == "s3"` and silently skipped for everything else.
- Generic Check 3 (`s3_bucket` present in YAML) failed — the deep section still runs, but most checks degrade to "missing bucket" failures pointing back at the YAML.
### SMB deep checks

When `--backend smb` is in effect (explicitly via the flag, or because the primary / a Tier 2 mirror is `smb`), the doctor runs an additional six checks targeting SMB/CIFS-specific failure modes. These complement the generic credentials/connectivity loop; they don't replace it. Skipped silently for every other backend.

| Check | Failure looks like |
|---|---|
| Server reachable (TCP connect to `smb_server:smb_port`) | `Server unreachable: HOST:PORT` (fix: verify the SMB service is up; default port is 445, legacy NetBIOS-over-TCP uses 139) or `Connection timed out` (fix: `ping HOST`, check firewall) |
| SMB protocol negotiation (SMB2/3 only — SMBv1 rejected) | `Server only speaks SMBv1 — refusing to connect.` plus the explanatory line `SMBv1 is end-of-life and re-opens EternalBlue-class attack surface.` and a fix-hint pointing at the server's protocol settings. Non-v1 negotiation failures (handshake interrupted, dialect mismatch) surface as `SMB protocol negotiation failed` with a firewall / antivirus pointer |
| Authentication via `register_session` | `SMB authentication rejected by HOST` — bad credentials, account locked, and domain mismatch all bucket as ONE auth-bucket failure with a fix-hint pointing at `smb_username` / `smb_password` / `smb_domain` in the YAML |
| Share access (`scandir` on the share root) | `Share not found: \\\\HOST\\SHARE` (fix: `smb_share` in the YAML — list shares with `smbclient -L HOST -U USER`) or `Permission denied listing share` (auth-bucket failure pointing at share-level vs file-level ACLs) |
| Folder write (sentinel `__claude_mirror_doctor_test`) | `Permission denied writing to PATH` — share access succeeded but file-level ACLs deny write; fix points at the underlying NTFS / POSIX permissions on `smb_folder` |
| Encryption status (info-only) | `SMB3 encryption negotiated (per-message AES)` when requested+active. `SMB3 encryption requested but server negotiated down — wire traffic is NOT encrypted` (yellow warning, NOT a failure) when the server only supports SMB2. `SMB encryption disabled in config` when the user opted out via `smb_encryption: false` |

#### Security gate: SMBv1 rejection

The SMBv1 check is a hard refuse, not a warning. SMBv1 has been end-of-life since 2017 and re-introduces EternalBlue-class attack surface. The deep check fails immediately with the `POSSIBLE…` analogue ("refusing to connect") and the fix-hint points at the server's protocol settings rather than `claude-mirror auth` (re-authenticating won't help — we won't talk to v1 servers regardless). This mirrors the SFTP fingerprint-mismatch security gate in spirit.

#### Auth-failure bucketing

Auth-class failures (bad creds, share access denied, folder write denied) all funnel through ONE auth-bucket — at most one of these fires per run, and the remaining checks are short-circuited so the user doesn't see five copies of "your access is broken" rooted in the same problem.

#### Lazy import

`smbprotocol` and `smbclient` are lazy-imported inside the deep-check function so the import cost is only paid when `--backend smb` is actually exercising these checks. Generic `claude-mirror doctor` invocations on other backends remain fast.

#### When the deep section is skipped

- Backend is not `smb` — the deep section is gated on `backend_name == "smb"` and silently skipped for everything else.
- Generic Check 3 (SMB credentials present in YAML) failed — the deep section still runs, but most checks degrade to "missing credentials" failures pointing back at the YAML.

### Sample Dropbox deep-check successful output

```
── checking dropbox backend (/home/alice/.config/claude_mirror/dropbox-myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/dropbox-credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/dropbox-myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ Token JSON valid; refresh_token present
  ✓ App key format valid: uao2pmhc0xgg2xj
  ✓ Account: alice@example.com (account_id: dbid:AAH123456)
  ✓ Scopes: files.content.read, files.content.write
  ✓ Folder accessible: /claude-mirror/myproject
  ✓ Account type: personal
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json
```

### Sample Dropbox deep-check failure output

```
── checking dropbox backend (/home/alice/.config/claude_mirror/dropbox-myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/dropbox-credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/dropbox-myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ Token JSON valid; refresh_token present
  ✓ App key format valid: uao2pmhc0xgg2xj
  ✓ Account: alice@example.com (account_id: dbid:AAH123456)
  ✓ Scopes: files.content.read, files.content.write
  ✗ Folder not found in Dropbox: /claude-mirror/myproject
      Fix: create /claude-mirror/myproject in your Dropbox account (web UI or Dropbox client) and re-run claude-mirror doctor --backend dropbox --config /home/alice/.config/claude_mirror/dropbox-myproject.yaml.
  ✓ Account type: personal

✗ 1 issue(s) found. Fix the items above and re-run claude-mirror doctor.
```

### Exit codes

- `0` — every check passed.
- `1` — at least one check failed.

This composes cleanly with shell scripts and CI: `claude-mirror doctor && claude-mirror push` will only push if the configuration is healthy, and a CI job that runs `claude-mirror doctor` on each runner surfaces broken setups before they cause noisy push / sync failures downstream.

### Common invocations

```bash
claude-mirror doctor                                              # auto-detect config from cwd
claude-mirror doctor --config ~/.config/claude_mirror/work.yaml   # specific config
claude-mirror doctor --backend dropbox                            # only check the dropbox backend (Tier 2)
claude-mirror doctor --backend googledrive                        # generic checks PLUS Drive deep checks (scopes, APIs, topic, subscription, IAM grant)
claude-mirror doctor --backend onedrive                           # generic checks PLUS OneDrive deep checks (token cache, client_id, scopes, refresh, Graph drive-item probe)
```

The `--backend` filter is case-insensitive and accepts `googledrive`, `dropbox`, `onedrive`, `webdav`, `sftp`, or `ftp`. The primary config is always parsed; only the per-backend loop is filtered. Skipped backends print a dim `── skipped: NAME (PATH) — does not match --backend FILTER` line so the output stays self-explanatory.
The `--backend` filter is case-insensitive and accepts `googledrive`, `dropbox`, `onedrive`, `webdav`, `sftp`, or `smb`. The primary config is always parsed; only the per-backend loop is filtered. Skipped backends print a dim `── skipped: NAME (PATH) — does not match --backend FILTER` line so the output stays self-explanatory.

### Where to go next

- Credentials issues (missing `credentials.json`, OAuth client setup) — see the backend setup pages: [backends/google-drive.md](backends/google-drive.md), [backends/dropbox.md](backends/dropbox.md), [backends/onedrive.md](backends/onedrive.md), [backends/webdav.md](backends/webdav.md), [backends/sftp.md](backends/sftp.md), [backends/smb.md](backends/smb.md).
- Manifest corruption or surprising sync state — see [conflict-resolution.md](conflict-resolution.md) for how the manifest interacts with the conflict-detection flow.
- Full flag list — [cli-reference.md#doctor](cli-reference.md#doctor).

---

## See also

- [faq.md](faq.md) — 30-second answers to common questions about auth, sync, snapshots, notifications, performance, and migration.
- [conflict-resolution.md](conflict-resolution.md) for resolving `sync` conflicts.
- [cli-reference.md](cli-reference.md) for the full command list (snapshot, retention, watcher commands grouped under "Snapshots" and "Maintenance").
- [scenarios.md](scenarios.md) for end-to-end deployment topology guides (standalone, multi-machine, multi-user, multi-backend).
- Backend pages — backend-specific notes about how `full`-format snapshots and the watcher behave on each backend:
  - [backends/google-drive.md](backends/google-drive.md)
  - [backends/dropbox.md](backends/dropbox.md)
  - [backends/onedrive.md](backends/onedrive.md)
  - [backends/webdav.md](backends/webdav.md)
  - [backends/sftp.md](backends/sftp.md)
  - [backends/ftp.md](backends/ftp.md)
