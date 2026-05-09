← Back to [README index](../README.md)

# Deployment scenarios

How to set up claude-mirror for the most common deployment topologies. Each scenario explains its purpose, how to implement it, what daily operations look like, and common pitfalls.

These are recipes, not policy. The same machine can host several projects in different scenarios at the same time — one project on a personal Drive, another on a team SFTP server, a third mirrored across both. Pick whichever scenario most closely matches each project and adapt from there.

For backend-specific setup details (OAuth flow, app registration, NAS configuration), follow the cross-links into [`docs/backends/`](./backends/) — this guide focuses on topology, not provider mechanics. For the conflict-resolution UX, see [`docs/conflict-resolution.md`](./conflict-resolution.md). For full command flag reference, see [`docs/cli-reference.md`](./cli-reference.md). For snapshot retention, gc, and doctor, see [`docs/admin.md`](./admin.md).

## Index
- [A. Standalone mirror](#a-standalone-mirror)
- [B. Personal multi-machine sync](#b-personal-multi-machine-sync)
- [C. Multi-user collaboration](#c-multi-user-collaboration)
- [D. Multi-backend redundancy (Tier 2)](#d-multi-backend-redundancy-tier-2)
- [F. Selective sync](#f-selective-sync)
- [G. Multi-user + multi-backend (production-realistic)](#g-multi-user--multi-backend-production-realistic)
- [H. Multi-project enterprise](#h-multi-project-enterprise)

---

## A. Standalone mirror

### Purpose

One user, one machine, one backend. The point is **off-machine durability**: an authoritative remote copy of the project's markdown that survives a stolen laptop, a wiped disk, or a botched local edit. You also get the snapshot timeline for free, so any file can be rolled back to any past push.

This is the right scenario when:

- You only ever work from one machine but want a disaster-recovery backup of the project.
- You want time-travel restore (`claude-mirror history`, `claude-mirror restore`) without setting up anything multi-user.
- You want to evaluate claude-mirror end-to-end before adopting a more complex topology.

If you sometimes work from a second machine, jump to [Scenario B](#b-personal-multi-machine-sync) instead — the daemon and notification setup are different. If durability matters enough to need two independent providers, jump to [Scenario D](#d-multi-backend-redundancy-tier-2).

### How to implement

Backend choice: **any backend works**. Pick whichever you already have credentials for. Google Drive and Dropbox are the lowest-friction (consumer accounts, free tier, no infrastructure). SFTP/WebDAV are the right choice if you already operate a NAS or VPS — see [`docs/backends/sftp.md`](./backends/sftp.md) and [`docs/backends/webdav.md`](./backends/webdav.md).

A complete config for a single Dropbox-backed project:

```yaml
# ~/.config/claude_mirror/myproject.yaml
backend: dropbox
project_path: /home/alice/projects/myproject
dropbox_app_key: uao2pmhc0xgg2xj
dropbox_folder: /claude-mirror/myproject
token_file: /home/alice/.config/claude_mirror/dropbox-myproject-token.json
file_patterns:
  - "**/*.md"
machine_name: laptop
user: alice
keep_last: 30        # always keep 30 newest snapshots
keep_daily: 14       # plus one per day for the last 2 weeks
```

Bring-up sequence from inside the project directory:

```bash
cd /home/alice/projects/myproject
claude-mirror init --wizard --backend dropbox
claude-mirror auth
claude-mirror push
```

The first `push` uploads every file matching `file_patterns` and creates the first snapshot. From here, the project is durable on Dropbox even if your laptop disappears tomorrow.

Optional but recommended: install the watcher service via `claude-mirror-install` so the watcher auto-starts on login. In a true single-machine scenario the watcher has nothing to listen for from collaborators, but it still keeps the inbox file fresh for the Claude Code skill and surfaces version-update notices.

### Daily ops behaviour

| Event | What happens |
|---|---|
| You edit a file locally | `status` reports `local ahead`. Nothing is sent until you `push`. |
| You run `claude-mirror push` | Changed files upload to Dropbox in parallel. A new snapshot is written. The local manifest records the new remote hashes. |
| You run `claude-mirror pull` | Downloads anything that is newer on the remote (rare in this scenario — only happens if you edited via the Dropbox web UI). |
| Network blip during push | Per-file retry with exponential backoff. If the file ultimately fails, it stays in `pending_retry` and is reattempted on the next push. |
| You delete a file locally | `status` shows `deleted local`. Next `push` deletes from Dropbox too. The previous version is still recoverable from the most recent snapshot. |
| You want to recover yesterday's CLAUDE.md | `claude-mirror history CLAUDE.md` then `claude-mirror restore <timestamp> CLAUDE.md`. |
| You delete a file via the Dropbox web UI | `status` shows `new on remote` for the rest of the project (because the remote drifted), and the deleted file shows as `local ahead`. Your next `push` will re-upload it. |

### Pitfalls and tips

- **Don't skip the first authenticated `push`.** Until the manifest is written, a `status` will look as if the entire project is local-ahead.
- **Snapshot retention is opt-in.** Without `keep_*` fields, snapshots accumulate forever. Set a reasonable retention up-front so you don't have to back-fill later. See [`docs/admin.md`](./admin.md).
- **Editing in the cloud UI works but is messy.** Dropbox's web editor will rewrite the file's metadata and may break the manifest's hash record. Prefer to edit locally and `push`.
- **The `.claude_mirror_*` files in your project root are local state.** Add them to `.gitignore` if the project is also a git repo.
- **Upgrading to multi-machine later is free.** When you do add a second machine, you keep the same config and just run `init`/`auth` on the new machine pointing at the same Dropbox folder. You're already in [Scenario B](#b-personal-multi-machine-sync).

### Automated nightly sync

Even on a single machine, you may want a cron-driven safety net that pushes anything you forgot to push manually. The `--no-prompt --strategy` flow makes that one-line:

```cron
# Nightly at 03:00 — push any local edits, pull anything edited via the
# cloud UI, and resolve any 2-way conflict by keeping the local copy.
0 3 * * * cd /home/alice/projects/myproject && /usr/local/bin/claude-mirror sync --no-prompt --strategy keep-local
```

In a true single-machine scenario, conflicts are vanishingly rare — they only happen if you also edited via the Dropbox / Drive web UI between cron ticks. `--strategy keep-local` is the right default: your local edits always win, the conflict is logged in `_sync_log.json` with the chosen strategy, and the next interactive `claude-mirror log` surfaces it so you know to investigate. See [`docs/admin.md` — Unattended sync via cron](./admin.md#unattended-sync-via-cron) for the full flag table and `docs/cli-reference.md#sync`.

---

## B. Personal multi-machine sync

### Purpose

One user, multiple machines (e.g. laptop + desktop + work machine), one backend. Always work from the latest state regardless of which machine you sit down at. Real-time notification when one of your other machines pushes lets you `pull` before you start editing — preventing accidental local-vs-remote forks.

This is the right scenario when:

- You frequently switch between two or more personal machines on the same project.
- You don't share the project with anyone else (yet).
- You want sub-30-second propagation when one machine pushes.

If a second person joins the project, jump to [Scenario C](#c-multi-user-collaboration) — the auth model changes (each person needs their own token) but the topology is identical. If you need to survive a backend outage, layer [Scenario D](#d-multi-backend-redundancy-tier-2) on top.

### How to implement

Backend choice: **Google Drive or Dropbox**. Both deliver near-real-time push notifications across machines (Pub/Sub for Drive, long-poll for Dropbox), so when your laptop pushes, your desktop's watcher fires within a second or two. OneDrive / WebDAV / SFTP work too but rely on polling — fine if you only switch machines a few times a day.

The config is identical on each machine **except** for `machine_name` (used in notifications) and the per-machine token file. Example for Google Drive on the laptop:

```yaml
# laptop:~/.config/claude_mirror/myproject.yaml
backend: googledrive
project_path: /Users/alice/projects/myproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
gcp_project_id: alice-personal-gcp
pubsub_topic_id: claude-mirror-myproject
credentials_file: /Users/alice/.config/claude_mirror/personal-credentials.json
token_file: /Users/alice/.config/claude_mirror/personal-token.json
file_patterns:
  - "**/*.md"
machine_name: laptop
user: alice
```

On the desktop, the only difference is `project_path` (if it lives at a different path) and `machine_name: desktop`. **Both machines authenticate as the same Google account** — the credentials file is shared, but each machine creates its own token file via its own `claude-mirror auth` run.

Bring-up sequence — run on **each** machine:

```bash
cd ~/projects/myproject
claude-mirror init --wizard --backend googledrive   # use the same drive_folder_id and gcp_project_id on every machine
claude-mirror auth                                   # opens a browser, log in as the same account, but creates a per-machine token
claude-mirror-install                                # installs the watcher as a launchd / systemd service
```

After both machines are set up, the laptop pushes once to seed the Drive folder, and the desktop pulls once to bring its working copy in line. From here on, the watcher does the heavy lifting.

See [`docs/backends/google-drive.md`](./backends/google-drive.md) for the GCP project / OAuth client / Pub/Sub topic creation walkthrough.

### Daily ops behaviour

| Event | What happens |
|---|---|
| Laptop pushes | Drive receives the new file bodies; Pub/Sub publishes a notification. Desktop's `watch-all` daemon receives it within a second and fires a desktop notification: "alice@laptop updated CLAUDE.md in 'myproject'." |
| Desktop's watcher fires the notification | Inbox file `.claude_mirror_inbox.jsonl` is appended. Next time you launch Claude Code or run `/claude-mirror`, the pending notification is surfaced inline. |
| You sit down at the desktop and start working | Run `claude-mirror status` first (or just `/claude-mirror` in Claude Code). `pull` anything `remote ahead`, then start editing. |
| You forget to pull and edit a file the laptop already changed | Next `push` from the desktop produces a `conflict` for that file. Interactive resolution: keep local, keep remote, or merge in `$EDITOR`. See [`docs/conflict-resolution.md`](./conflict-resolution.md). |
| Network blip | Push retries 3x in-process; persistent failure queues the file for the next push. The watcher on the other machine will fire whenever the eventual push succeeds. |
| Laptop offline for a week | When it comes back online, run `claude-mirror status` — files modified on either side show their state. `sync` resolves the lot in one command. |
| You delete a file on the laptop | Next push removes it from Drive. The desktop's watcher fires, and `pull` removes it from the desktop's working copy. The file is still in every existing snapshot. |

### Pitfalls and tips

- **Each machine needs its own `claude-mirror auth` run.** Tokens are per-machine. Copying a token file from machine A to machine B works but invalidates A's refresh token under Google's per-client-per-account refresh-token cap (50 active tokens by default). Just run auth on each machine — it's a one-time browser dance.
- **Set distinct `machine_name` values.** Otherwise notifications read "alice@laptop pushed" from both machines and you can't tell them apart.
- **Always `claude-mirror status` (or run `/claude-mirror`) before editing.** Even with sub-second notification delivery, if your machine was asleep or offline, you're working from stale state.
- **The watcher must actually be running.** If you skipped `claude-mirror-install`, run `claude-mirror watch-all` in a terminal — otherwise notifications pile up in the Pub/Sub backlog and you lose the real-time signal.
- **Same-account, different-Drive is a footgun.** If you point one machine at folder X and another at folder Y on the same Google account, they will each happily mirror their own copy and never see each other. Double-check `drive_folder_id` is identical across configs.
- **Skill auto-detection works in Claude Code.** Open the project in Claude Code on either machine and the skill picks the right config from `cwd`.

### Automated nightly sync

For machines that are awake-but-idle overnight (e.g. a desktop you don't power off), pair the watcher with a cron-driven safety-net `sync` that catches anything the watcher missed:

```cron
# Every 4 hours, push pending edits + pull remote edits + auto-resolve any
# conflicts in favour of local. The watcher already handles real-time
# pulls; this is the belt-and-braces guarantee that an asleep / offline /
# pub-sub-glitch window doesn't leave your machine stale.
0 */4 * * * cd /Users/alice/projects/myproject && /usr/local/bin/claude-mirror sync --no-prompt --strategy keep-local
```

`keep-local` is the safest default for a personal-multi-machine setup: when both your laptop AND your desktop edited the same file (rare but possible if both were offline at the same time), local wins on whichever machine the cron runs on. The conflict is logged in `_sync_log.json` so the next time you open `claude-mirror log` you see the auto-resolution and can manually re-merge if needed. Full flag table in [`docs/cli-reference.md#sync`](./cli-reference.md#sync); crontab samples in [`docs/admin.md`](./admin.md#unattended-sync-via-cron).

---

## C. Multi-user collaboration

### Purpose

Multiple users sharing one project's MD context. Everyone sees everyone's edits in near-real-time; conflicts are surfaced interactively and resolved without silent overwrites; the snapshot timeline is the team's shared point-in-time recovery surface.

This is the right scenario when:

- 2+ people share a project — typically a Claude Code memory directory, a team's running notebook, or a research wiki.
- You want chat-channel-level immediacy without the heavy merge ceremony of git.
- You need an audit trail (the `_sync_log.json` records who pushed what when).

If you also want redundancy across providers (Drive **and** Dropbox), jump to [Scenario G](#g-multi-user--multi-backend-production-realistic) — that's this scenario plus [Scenario D](#d-multi-backend-redundancy-tier-2) layered on. If only some files should be shared, layer [Scenario F](#f-selective-sync) on top.

### How to implement

Backend choice: **Google Drive** is the most ergonomic for teams — Pub/Sub notifications are sub-second, folder sharing is one-click in the Drive UI, and the OAuth model gives every collaborator their own identity (so the audit log shows real names, not a shared service account). **Dropbox** works similarly with its long-poll channel. **WebDAV** (Nextcloud) is the right pick when the team self-hosts and doesn't want any third-party cloud involved.

The project owner does the one-time backend setup (GCP project, OAuth client, Pub/Sub topic, shared Drive folder), then shares two things with each collaborator: the credentials JSON (it identifies the **app**, not any user) and the Drive folder ID. Each collaborator authenticates as themselves.

Owner's config:

```yaml
# alice@laptop:~/.config/claude_mirror/teamproject.yaml
backend: googledrive
project_path: /Users/alice/projects/teamproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
gcp_project_id: team-shared-gcp
pubsub_topic_id: claude-mirror-teamproject
credentials_file: /Users/alice/.config/claude_mirror/team-credentials.json
token_file: /Users/alice/.config/claude_mirror/team-token.json
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "drafts/**"
machine_name: alice-laptop
user: alice
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01/B01/xxxx
slack_channel: "#teamproject-sync"
```

Collaborator's config (Bob) is the same except for `project_path`, `machine_name`, `user`, and `token_file`:

```yaml
# bob@desktop:~/.config/claude_mirror/teamproject.yaml
backend: googledrive
project_path: /home/bob/work/teamproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt   # same folder!
gcp_project_id: team-shared-gcp
pubsub_topic_id: claude-mirror-teamproject
credentials_file: /home/bob/.config/claude_mirror/team-credentials.json
token_file: /home/bob/.config/claude_mirror/team-token.json
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "drafts/**"
machine_name: bob-desktop
user: bob
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01/B01/xxxx
slack_channel: "#teamproject-sync"
```

Bring-up:

1. Owner sets up the GCP project + Drive folder + Pub/Sub topic ([`docs/backends/google-drive.md`](./backends/google-drive.md)).
2. Owner shares the Drive folder with each collaborator's Google account (Editor permission) and grants Pub/Sub Editor in IAM.
3. Owner sends each collaborator the credentials JSON and the Drive folder ID over a secure channel (the credentials file identifies the app, not any user — it's safe to share within the team).
4. Each collaborator drops the credentials at `~/.config/claude_mirror/team-credentials.json`, runs `claude-mirror init --wizard` (entering the shared `drive_folder_id` / `gcp_project_id` / `pubsub_topic_id`), and runs `claude-mirror auth` to authenticate as themselves.
5. Each collaborator runs `claude-mirror-install` so the watcher starts on login.

### Daily ops behaviour

| Event | What happens |
|---|---|
| Alice pushes a CLAUDE.md edit | Drive accepts the upload, Pub/Sub publishes; Bob's watcher receives within a second; Bob's desktop notification reads "alice@alice-laptop updated CLAUDE.md in 'teamproject'." Slack posts a Rich message to `#teamproject-sync`. |
| Bob runs `/claude-mirror` in Claude Code | Inbox is shown inline; the skill diffs Bob's local copy against Alice's remote, produces a merged result, and offers to push. |
| Bob and Alice edit the same file simultaneously, both push | First push wins; the second hits a `conflict`. The losing side resolves interactively (keep local / keep remote / merge in `$EDITOR`). See [`docs/conflict-resolution.md`](./conflict-resolution.md). |
| Bob runs `claude-mirror log` | Sees a chronological list of every push, pull, sync, and delete by every collaborator across all machines. |
| Carol joins the team | Owner shares the Drive folder + grants Pub/Sub Editor + sends her the credentials. Carol does the 3 setup commands. Her first `pull` brings the project to her machine. |
| Bob accidentally deletes a file | His next push removes it from Drive. Alice's watcher fires; she sees the delete in `log`. The file is still in every snapshot — `claude-mirror history <path>` shows when it disappeared and `restore` brings it back. |
| Pub/Sub backlog fills (a watcher was offline for a week) | When the watcher comes back online it drains the backlog into the inbox. Notifications still arrive in order. |

### Pitfalls and tips

- **Pub/Sub IAM is a recurring stumbling block.** A collaborator whose `auth` succeeds but who can't `push` usually lacks Pub/Sub Editor. Have the owner double-check the IAM grant in the GCP project.
- **The credentials JSON is shared; the token file is not.** Sharing a token file gives someone else your Google identity for this app. The credentials file alone is harmless without an OAuth flow.
- **Conflicts are not bugs — they're features.** Don't reflexively pick "keep local" every time. Open the merge in `$EDITOR` if you can't trivially say which side is right.
- **Slack becomes the team's heartbeat.** A channel dedicated to the project's sync events tells everyone, at a glance, that the team is in sync (or not) without anyone running `status`.
- **One person being offline never blocks the team.** Pub/Sub holds undelivered messages for up to 7 days by default; OneDrive/WebDAV pollers catch up on next poll.
- **Add `drafts/**` (or similar) to `exclude_patterns`** so personal scratch notes never accidentally end up in the team folder. See [Scenario F](#f-selective-sync) for the full pattern reference.

---

## D. Multi-backend redundancy (Tier 2)

### Purpose

One user, primary backend + N mirrors. Belt-and-suspenders durability: a Drive outage, a Dropbox account suspension, a NAS power failure — none of these can cost you data, because every push fans out to every backend in parallel and snapshots can be mirrored too. `restore` falls back across backends automatically.

This is the right scenario when:

- The project's MD context is irreplaceable (e.g. years of accumulated memory) and a single-vendor outage is unacceptable.
- You want to migrate from one backend to another without a hard cutover (run as mirror, then promote).
- You're comfortable trading some setup complexity for genuine cross-vendor redundancy.

If you also collaborate with others, you want [Scenario G](#g-multi-user--multi-backend-production-realistic) — same shape, but every user replicates this redundancy on their own machine.

### How to implement

Backend choice: **two unrelated providers**. Drive + Dropbox is the canonical team choice (different companies, different infrastructure). Drive + a self-hosted SFTP/NAS gives "cloud + local-LAN" survivability. The combinations are open — pick any two (or more) of `googledrive`, `dropbox`, `onedrive`, `webdav`, `sftp`.

The model is **one primary config + one extra config per mirror**, all sharing the same `project_path`. The primary config gets a `mirror_config_paths` list pointing at each mirror's YAML.

Primary config (Google Drive):

```yaml
# ~/.config/claude_mirror/myproject.yaml
backend: googledrive
project_path: /home/alice/projects/myproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
gcp_project_id: alice-personal-gcp
pubsub_topic_id: claude-mirror-myproject
credentials_file: /home/alice/.config/claude_mirror/personal-credentials.json
token_file: /home/alice/.config/claude_mirror/personal-token.json
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
mirror_config_paths:
  - /home/alice/.config/claude_mirror/myproject-dropbox.yaml
snapshot_on: all          # snapshots fan out to every backend
retry_on_push: true
notify_failures: true
```

Mirror config (Dropbox) — note **same `project_path`**:

```yaml
# ~/.config/claude_mirror/myproject-dropbox.yaml
backend: dropbox
project_path: /home/alice/projects/myproject
dropbox_app_key: uao2pmhc0xgg2xj
dropbox_folder: /claude-mirror/myproject
token_file: /home/alice/.config/claude_mirror/dropbox-myproject-token.json
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
```

Bring-up:

```bash
cd ~/projects/myproject
claude-mirror init --wizard --backend googledrive
# add mirror_config_paths to the YAML by hand or via:
claude-mirror init --wizard --backend dropbox \
  --project ~/projects/myproject \
  --config ~/.config/claude_mirror/myproject-dropbox.yaml
# edit primary config to add the mirror_config_paths line, then:
claude-mirror auth --config ~/.config/claude_mirror/myproject.yaml
claude-mirror auth --config ~/.config/claude_mirror/myproject-dropbox.yaml
claude-mirror push
```

The first `push` uploads to **both** backends in parallel and creates one snapshot on each (because `snapshot_on: all`).

### Daily ops behaviour

| Event | What happens |
|---|---|
| You run `claude-mirror push` | Files upload to Drive **and** Dropbox in parallel. Output groups results per backend. Snapshot is mirrored. |
| Drive is healthy, Dropbox returns 503 mid-push | Drive uploads succeed; failed Dropbox files are retried 3x in-process, then queued for next-push retry. The push as a whole exits 0; a yellow `pending_retry` warning summarizes what's queued. |
| Dropbox refresh token revoked | Affected files marked `failed_perm`. Red `ACTION REQUIRED` block in stdout and Slack. Re-auth Dropbox (`claude-mirror auth --config ...-dropbox.yaml`) then `claude-mirror retry --backend dropbox`. |
| You run `claude-mirror pull` | Reads from the **primary** (Drive) only. Mirrors are write-only from claude-mirror's perspective. |
| You run `claude-mirror status --by-backend` | Per-file table with one column per configured backend; instantly shows whether the project is in sync on every mirror. |
| You run `claude-mirror restore <ts>` and Drive is down | claude-mirror tries the primary, fails, walks `mirror_config_paths` in order, finds the snapshot on Dropbox, recovers. A yellow warning identifies which backend supplied the recovery. |
| You add a third mirror after the project already has files | Run `claude-mirror seed-mirror --backend NAME` once. It walks the manifest and uploads only files the new mirror is missing; the primary is never touched. |

### Pitfalls and tips

- **`mirror_config_paths` lives only in the primary YAML.** Mirror configs themselves are ordinary single-backend configs. Putting `mirror_config_paths` in a mirror YAML is a no-op.
- **`snapshot_on: all` is the right default for the `blobs` snapshot format** (cheap, deduplicated). For `full` format, `snapshot_on: primary` saves a lot of storage — full snapshots aren't deduped.
- **Don't share a token file across backends even on the same provider.** Each mirror gets its own token file.
- **`seed-mirror` is required after adding a mirror to an existing project.** Plain `push` only uploads files whose local hash differs from the manifest, so a new mirror with no manifest entries gets nothing. `seed-mirror` is idempotent — safe to re-run.
- **Watch `notify_failures: true` on Slack.** Quiet mirror failures (a token expiring 6 months in) are exactly the kind of thing you want loud notifications for. Keep it on.
- **Promoting a mirror to primary** is just a YAML edit: swap which file lists `mirror_config_paths` and you're done. See the migration note in [`docs/admin.md`](./admin.md).

---

## F. Selective sync

### Purpose

Per-project `file_patterns` + `exclude_patterns` to scope what gets mirrored. Composes with every other scenario — selective sync is a layer, not a topology.

This is the right scenario when:

- The project tree contains files you don't want on the cloud (`drafts/`, `secrets.md`, generated artifacts).
- The project contains files of multiple types and you only want some synced (e.g. mirror `**/*.md` and `**/*.py` but skip `**/*.log` and `node_modules/`).
- Storage cost or compliance pushes you to keep the synced surface as small as possible.

This is **not** an access-control mechanism. Anyone with access to the remote folder sees whatever you push. Use exclude patterns for "don't replicate this", not for "hide this from a specific collaborator".

### How to implement

Set `file_patterns` (defaults to `["**/*.md"]`) and `exclude_patterns` (defaults to none) in the project YAML. Both accept Python `fnmatch` glob syntax: `*`, `**`, `?`, `[...]`. Patterns can be repeated. They're project-relative.

Examples:

```yaml
# Sync MD + Python source, but skip drafts and tests
file_patterns:
  - "**/*.md"
  - "**/*.py"
exclude_patterns:
  - "drafts/**"
  - "**/tests/**"
  - "**/__pycache__/**"
  - "**/*_draft.md"
  - "secrets.md"
```

```yaml
# Mirror the entire tree but skip generated and dependency dirs
file_patterns:
  - "**/*"
exclude_patterns:
  - "node_modules/**"
  - "dist/**"
  - "build/**"
  - ".venv/**"
  - "**/*.log"
```

You can set patterns at `init` time:

```bash
claude-mirror init \
  --project ~/projects/myproject \
  --backend googledrive \
  --patterns '**/*.md' --patterns '**/*.py' \
  --exclude 'drafts/**' --exclude '**/*_draft.md' \
  --exclude 'secrets.md'
```

Or at any time later by editing the YAML directly. Changes take effect on the next command — no restart of the watcher needed (it picks up YAML changes via `SIGHUP` reload).

### Daily ops behaviour

| Event | What happens |
|---|---|
| You add `drafts/**` to `exclude_patterns` | Files inside `drafts/` immediately become invisible to `status`, `push`, `pull`, `sync`, `delete`. |
| You add a new file under `drafts/` | It never appears in `status` and never uploads. |
| You add a new file matching `file_patterns` outside excluded dirs | Shows up as `new local` on next `status`; uploads on next `push`. |
| Files **already** on the remote when you add the exclude pattern | Stay on the remote — exclusion ignores them on the local side, but doesn't reach back to delete. To remove them, run `claude-mirror delete <path>` **before** adding the exclusion (or revert the exclusion temporarily, delete, then re-exclude). |
| A collaborator on a different machine has different `exclude_patterns` | Each machine evaluates its own patterns. A file excluded on machine A is invisible to A but happily synced by B. (This is usually a misconfiguration — keep `exclude_patterns` identical across team members for the same project.) |
| You change `file_patterns` to a more restrictive set | Files that were previously matched are now invisible — but they remain on the remote. Run `claude-mirror delete` to reach back, same as for `exclude_patterns`. |

### Pitfalls and tips

- **`fnmatch`, not `gitignore`.** `archive/**` excludes everything inside `archive/` at any depth, but a bare `archive` does not. Always use `**` for directory exclusion.
- **Excluded files are invisible to `status`.** If `claude-mirror status` shows a clean tree but you "know" you edited something, double-check it isn't excluded.
- **Exclusions don't delete remote leftovers.** This is the most common surprise. To purge the remote: temporarily remove the exclude, run `claude-mirror delete <files>`, then re-add the exclude.
- **Keep team patterns aligned.** If Alice excludes `drafts/**` and Bob doesn't, Bob will mirror `drafts/` from his machine, and Alice's clients see those files arrive on the remote — which she didn't expect.
- **Don't put credentials or .env in a watched tree.** Selective sync prevents accidental upload **only** if the pattern is set correctly. Belt-and-suspenders: keep secrets outside the project tree.
- **Composes with every other scenario.** Add patterns to a Tier 2 setup, a multi-user team, a single-machine standalone — same fields, same semantics.

### `.claude_mirror_ignore` (gitignore-style alternative)

For per-project exclusions that read like a `.gitignore`, drop a `.claude_mirror_ignore` file at the project root. Unlike YAML `exclude_patterns` (which uses `fnmatch`), the ignore file uses gitignore syntax — including `**` for any-depth directory matches, leading `/` for root-anchored rules, trailing `/` for directory-only rules, and `!` for re-includes. Both systems run side by side; a file is excluded if EITHER layer says so. The file itself is auto-excluded from sync, so its rules don't propagate to other machines unless you explicitly opt in. Full reference: [`docs/admin.md`](./admin.md#claude_mirror_ignore--project-tree-exclusions).

---

## G. Multi-user + multi-backend (production-realistic)

### Purpose

The most production-realistic scenario: a team of users (here, Alice and Bob) collaborating on a project, with every push fanned out to a primary cloud backend AND a redundant mirror, on every user's machine. This combines [Scenario C](#c-multi-user-collaboration) (multi-user) with [Scenario D](#d-multi-backend-redundancy-tier-2) (multi-backend) — neither one alone delivers what teams running anything important want.

Specifically: if Alice pushes, the file fans out to **both** Drive and the team's SFTP server. Bob's watcher receives the Drive notification within seconds and his next pull picks up the change. If Drive has an outage tomorrow, Bob can still read the latest from SFTP via `restore --backend sftp`. If the SFTP server's disk fills, Drive is untouched and the team keeps working.

This is the right scenario when:

- 2+ people share a project and you cannot afford a single-vendor outage.
- You have a team-controlled SFTP/WebDAV server (NAS, VPS) that can serve as the "we own this" backup of the cloud primary.
- The project's MD context is the team's institutional memory and you want it boringly durable.

This is overkill for solo / casual use — start with [Scenario C](#c-multi-user-collaboration) and add a mirror later when the team grows. It's also overkill if your team is fine with single-provider risk; in that case [Scenario C](#c-multi-user-collaboration) is enough.

### How to implement

Backend choice: **Google Drive as primary** (real-time Pub/Sub gives the team near-instant collaboration signal) and **SFTP as mirror** (team-owned, no third-party cloud cost, easy to back up to LAN tape). Other production-realistic combinations: Drive + Dropbox (two clouds), or Dropbox primary + WebDAV/Nextcloud mirror (consumer cloud + self-hosted).

The model: each user maintains **two** YAML files — a primary (Drive) and a mirror (SFTP) — with `mirror_config_paths` set on the primary. Both files share `project_path` but have user-specific `machine_name`, `user`, and per-machine token files.

#### Alice's primary config

```yaml
# alice@laptop:~/.config/claude_mirror/teamproject.yaml
backend: googledrive
project_path: /Users/alice/work/teamproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
gcp_project_id: team-shared-gcp
pubsub_topic_id: claude-mirror-teamproject
credentials_file: /Users/alice/.config/claude_mirror/team-credentials.json
token_file: /Users/alice/.config/claude_mirror/team-token.json
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "drafts/**"
machine_name: alice-laptop
user: alice
mirror_config_paths:
  - /Users/alice/.config/claude_mirror/teamproject-sftp.yaml
snapshot_on: all
retry_on_push: true
notify_failures: true
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01/B01/xxxx
slack_channel: "#teamproject-sync"
keep_last: 50
keep_daily: 14
keep_monthly: 12
```

#### Alice's SFTP mirror config

```yaml
# alice@laptop:~/.config/claude_mirror/teamproject-sftp.yaml
backend: sftp
project_path: /Users/alice/work/teamproject
sftp_host: files.team.example.com
sftp_port: 22
sftp_username: alice
sftp_key_file: /Users/alice/.ssh/id_ed25519
sftp_known_hosts_file: /Users/alice/.ssh/known_hosts
sftp_strict_host_check: true
sftp_folder: /srv/claude-mirror/teamproject
token_file: /Users/alice/.config/claude_mirror/sftp-teamproject-token.json
poll_interval: 30
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "drafts/**"
machine_name: alice-laptop
user: alice
```

#### Bob's primary config

Same `drive_folder_id`, `gcp_project_id`, `pubsub_topic_id`, and identical `file_patterns` / `exclude_patterns`. Different `project_path`, `machine_name`, `user`, and per-machine token files. Crucially, **Bob authenticates as Bob** — he has his own Google identity for the same Drive folder.

```yaml
# bob@desktop:~/.config/claude_mirror/teamproject.yaml
backend: googledrive
project_path: /home/bob/projects/teamproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
gcp_project_id: team-shared-gcp
pubsub_topic_id: claude-mirror-teamproject
credentials_file: /home/bob/.config/claude_mirror/team-credentials.json
token_file: /home/bob/.config/claude_mirror/team-token.json
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "drafts/**"
machine_name: bob-desktop
user: bob
mirror_config_paths:
  - /home/bob/.config/claude_mirror/teamproject-sftp.yaml
snapshot_on: all
retry_on_push: true
notify_failures: true
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01/B01/xxxx
slack_channel: "#teamproject-sync"
keep_last: 50
keep_daily: 14
keep_monthly: 12
```

#### Bob's SFTP mirror config

Same SFTP host and folder as Alice (the whole point is the team shares the mirror), but Bob's own SSH user, key, and token file:

```yaml
# bob@desktop:~/.config/claude_mirror/teamproject-sftp.yaml
backend: sftp
project_path: /home/bob/projects/teamproject
sftp_host: files.team.example.com
sftp_port: 22
sftp_username: bob
sftp_key_file: /home/bob/.ssh/id_ed25519
sftp_known_hosts_file: /home/bob/.ssh/known_hosts
sftp_strict_host_check: true
sftp_folder: /srv/claude-mirror/teamproject
token_file: /home/bob/.config/claude_mirror/sftp-teamproject-token.json
poll_interval: 30
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "drafts/**"
machine_name: bob-desktop
user: bob
```

What's shared across users: the Drive folder ID, GCP project, Pub/Sub topic, SFTP host + folder, file patterns, exclude patterns, Slack webhook, retention policy. What's per-user: `project_path`, `machine_name`, `user`, all token files, the SFTP username and SSH key, and the credentials file (each user holds their own copy of the OAuth client JSON locally).

### End-to-end transcript

This walks through one full collaboration loop. Alice initializes the project, both backends authenticate, Alice's first push fans out, Bob joins, Bob pulls, Bob edits, Bob pushes, Alice pulls. The notification choreography is the punchline.

#### 1. Alice initializes the project

```
alice@laptop:~/work/teamproject $ claude-mirror init --wizard --backend googledrive

claude-mirror setup wizard

Press Enter to accept the default shown in brackets.

Storage backend [googledrive]:
Project directory [/Users/alice/work/teamproject]:
Credentials file [~/.config/claude_mirror/credentials.json]: ~/.config/claude_mirror/team-credentials.json
Drive folder ID: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
GCP project ID: team-shared-gcp
Pub/Sub topic ID [claude-mirror-teamproject]:
Token file [~/.config/claude_mirror/team-token.json]:
File patterns [**/*.md]:
Exclude patterns []: drafts/**

Summary
  Backend:       googledrive
  Project:       /Users/alice/work/teamproject
  Drive folder:  1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
  GCP project:   team-shared-gcp
  Patterns:      **/*.md
  Exclude:       drafts/**

Save this configuration? [Y/n]: y
Wrote ~/.config/claude_mirror/teamproject.yaml
```

She then runs the wizard a second time for the SFTP mirror, pinning the config filename so it's obviously a mirror:

```
alice@laptop:~/work/teamproject $ claude-mirror init --wizard --backend sftp \
  --project /Users/alice/work/teamproject \
  --config ~/.config/claude_mirror/teamproject-sftp.yaml

Storage backend [googledrive]: sftp
SFTP host: files.team.example.com
SFTP port [22]:
SFTP username: alice
SFTP key file [~/.ssh/id_ed25519]:
SFTP folder: /srv/claude-mirror/teamproject
... (rest of the prompts) ...

Save this configuration? [Y/n]: y
Wrote ~/.config/claude_mirror/teamproject-sftp.yaml
```

She then edits the primary YAML to add the `mirror_config_paths` line pointing at the SFTP config, plus `snapshot_on: all` and the Slack webhook.

#### 2. Alice authenticates against both Drive and SFTP

```
alice@laptop:~/work/teamproject $ claude-mirror auth
Browser opening — log in with the Google account that has access to the Drive folder.
✓ Token saved to ~/.config/claude_mirror/team-token.json
✓ Pub/Sub topic verified, per-machine subscription created.

alice@laptop:~/work/teamproject $ claude-mirror auth --config ~/.config/claude_mirror/teamproject-sftp.yaml
Connecting to files.team.example.com:22 as alice...
✓ Host fingerprint matched ~/.ssh/known_hosts entry.
✓ Authenticated with key ~/.ssh/id_ed25519.
✓ Token saved to ~/.config/claude_mirror/sftp-teamproject-token.json (chmod 0600).
```

#### 3. Alice does the first push

```
alice@laptop:~/work/teamproject $ claude-mirror push
Scanning local files...      14 files match patterns
Scanning remote (drive)...    0 remote
Scanning remote (sftp)...     0 remote
Uploading to drive...        14/14 ✓
Uploading to sftp...         14/14 ✓
Snapshot 2026-05-08T09-12-30Z written to drive (blobs)
Snapshot 2026-05-08T09-12-30Z written to sftp  (blobs)
Per-backend status:
  • 🟢 drive — pushed 14, snapshot 2026-05-08T09-12-30Z
  • 🟢 sftp  — pushed 14, snapshot 2026-05-08T09-12-30Z
Slack: ✓ posted to #teamproject-sync
```

#### 4. Alice's watcher fires Pub/Sub notification on Drive

Alice's own watcher logs the Pub/Sub event for her own push. This is mostly a no-op for her (she pushed it; her local manifest already matches), but it's worth knowing the notification path is live.

#### 5. Bob clones the configs (or sets up his own)

The team's onboarding doc tells Bob to drop the credentials JSON at `~/.config/claude_mirror/team-credentials.json` and run the wizard with the same shared values. Alternatively, the team checks the **non-secret** parts of both YAMLs into a private internal repo as templates; Bob copies them, edits the per-user fields (`project_path`, `machine_name`, `user`, token paths, `sftp_username`, key path), and skips the wizard.

```
bob@desktop:~/projects/teamproject $ claude-mirror init --wizard --backend googledrive
... (same prompts as Alice, but Bob enters /home/bob/.config/... paths) ...

bob@desktop:~/projects/teamproject $ claude-mirror init --wizard --backend sftp \
  --project /home/bob/projects/teamproject \
  --config ~/.config/claude_mirror/teamproject-sftp.yaml
... (Bob enters his own SFTP username and key path) ...
```

Bob then edits his primary YAML to add `mirror_config_paths` pointing at his SFTP config.

#### 6. Bob authenticates as himself

```
bob@desktop:~/projects/teamproject $ claude-mirror auth
Browser opening — log in with the Google account that has access to the Drive folder.
# Bob logs in as bob@... — different Google identity from Alice
✓ Token saved to ~/.config/claude_mirror/team-token.json
✓ Pub/Sub topic verified, per-machine subscription created (different subscription from Alice's).

bob@desktop:~/projects/teamproject $ claude-mirror auth --config ~/.config/claude_mirror/teamproject-sftp.yaml
Connecting to files.team.example.com:22 as bob...
✓ Host fingerprint matched ~/.ssh/known_hosts entry.
✓ Authenticated with key ~/.ssh/id_ed25519.
✓ Token saved.
```

#### 7. Bob runs first pull — gets Alice's content

```
bob@desktop:~/projects/teamproject $ claude-mirror pull
Reading from primary (drive)...
Downloading 14 files...
✓ Pulled 14 files. Manifest updated.
```

Bob's working copy now matches what Alice pushed.

#### 8. Bob's watcher daemon starts

```
bob@desktop:~/projects/teamproject $ claude-mirror-install
✓ Claude Code skill installed.
✓ PreToolUse notification hook installed.
✓ systemd user service claude-mirror-watch enabled and started.
✓ Shell tab-completion installed for zsh.

bob@desktop:~/projects/teamproject $ systemctl --user status claude-mirror-watch
● claude-mirror-watch.service - Claude Sync watcher — real-time cloud storage notifications
     Active: active (running) since ...
```

The watcher discovers Bob's `~/.config/claude_mirror/teamproject.yaml` and starts a Pub/Sub listener thread for it. (For the SFTP mirror, the watcher doesn't poll — pulls always read from the primary.)

#### 9. Bob edits a file

Bob opens `memory/architecture.md` in his editor and adds a paragraph about a new subsystem.

```
bob@desktop:~/projects/teamproject $ claude-mirror status
                            
  File                       Status
  memory/architecture.md     local ahead

  1 local ahead  ·  13 in sync
```

#### 10. Bob pushes — fans out to Drive AND SFTP

```
bob@desktop:~/projects/teamproject $ claude-mirror push
Uploading to drive...        1/1 ✓
Uploading to sftp...         1/1 ✓
Snapshot 2026-05-08T11-04-18Z written to drive (blobs)
Snapshot 2026-05-08T11-04-18Z written to sftp  (blobs)
Per-backend status:
  • 🟢 drive — pushed 1, snapshot 2026-05-08T11-04-18Z
  • 🟢 sftp  — pushed 1, snapshot 2026-05-08T11-04-18Z
Slack: ✓ posted to #teamproject-sync
```

The Slack channel shows:

```
🔼 bob@bob-desktop pushed 1 file in teamproject
Files changed: • memory/architecture.md
Per-backend status:
  • 🟢 drive — pushed 1, snapshot 2026-05-08T11-04-18Z
  • 🟢 sftp — pushed 1, snapshot 2026-05-08T11-04-18Z
📚 14 files in project
```

#### 11. Alice's watcher fires (now from Bob's push)

Within a second of Bob's push completing, Alice's watcher receives the Pub/Sub notification:

```
[macOS desktop notification]
claude-mirror
bob@bob-desktop updated memory/architecture.md in 'teamproject'.
Run `claude-mirror sync` to merge.
```

The event is also written to `/Users/alice/work/teamproject/.claude_mirror_inbox.jsonl`.

#### 12. Alice pulls Bob's edits

```
alice@laptop:~/work/teamproject $ claude-mirror status
  File                       Status
  memory/architecture.md     drive ahead

  1 drive ahead  ·  13 in sync

alice@laptop:~/work/teamproject $ claude-mirror pull
Reading from primary (drive)...
Downloading 1 file...
✓ Pulled 1 file. Manifest updated.
```

Or, if Alice is in Claude Code:

```
alice: /claude-mirror

Claude: Pending notifications:
  [2026-05-08 11:04:18] bob@bob-desktop pushed memory/architecture.md

  Status: 1 drive ahead.

  Downloading remote version for analysis...
  memory/architecture.md — Bob added a "Subsystem X" section (lines 88-104).
  No local session changes. Merged trivially. Ready to apply.

alice: yes

Claude: [runs claude-mirror pull memory/architecture.md]
  ↓ memory/architecture.md
  Pulled 1 file.
```

The loop closes. Both Alice and Bob now have identical local copies, identical state on Drive, and identical state on SFTP.

### Daily ops behaviour

| Event | What happens |
|---|---|
| Alice pushes | Drive + SFTP receive the file in parallel. Snapshots fan out per `snapshot_on: all`. Pub/Sub notifies all subscribers (Bob's machine), Slack posts. |
| Bob's watcher offline during Alice's push | Pub/Sub holds the message; when Bob's watcher reconnects, the notification drains immediately. SFTP polling on the primary path isn't relevant (pull reads from Drive). |
| Drive returns 503 mid-push | Drive uploads retry 3x in-process; failed files queue for next-push retry. SFTP uploads succeed independently. The push exits 0 with a yellow `pending_retry` summary. |
| SFTP server is down for maintenance | Drive uploads succeed normally. SFTP files queue for next-push retry; `retry_on_push: true` reattempts them automatically when the server returns. Bob and Alice continue working uninterrupted. |
| Drive has a multi-hour outage | Pulls fall back to mirror via `claude-mirror restore <ts> --backend sftp` for any file rollback. New pushes accumulate `pending_retry` on Drive until it returns. The team is not blocked. |
| Bob pushes a conflict (he and Alice both edited) | First push wins; Bob's push hits `conflict` per-file. Interactive resolution; resolved file is then fanned out to Drive AND SFTP. |
| `claude-mirror status --by-backend` | Shows the per-file × per-backend grid — Alice can see at a glance whether SFTP fell behind. |
| New mirror added to the project (e.g. add Dropbox as third backend) | Owner adds the third config to `mirror_config_paths`, runs `claude-mirror auth --config ...-dropbox.yaml`, then `claude-mirror seed-mirror --backend dropbox`. Once seeded, every future push fans to all three. |

### Pitfalls and tips

- **Each user has their own mirror token.** Alice's SFTP key and Bob's SFTP key are separate; the SFTP server has both in `authorized_keys`. The mirror folder permissions on the server need both users to be able to write.
- **Both users must agree on `snapshot_on`.** If Alice has `all` and Bob has `primary`, Bob's pushes won't write a snapshot to SFTP, and the snapshot timeline diverges per-mirror. Keep `snapshot_on` aligned across all users' primary configs.
- **Slack channel is the team's heartbeat.** When the channel goes quiet for a working day, someone's watcher is probably down; check.
- **`status --by-backend` is the morning check-in command.** Run it once per day per project; if any cell shows `⚠ pending` or `✗ failed`, deal with it before it accumulates.
- **`seed-mirror` is required when adding mirrors to an established project.** Otherwise the new mirror sits empty until the next file is edited. Idempotent and drift-safe.
- **Don't share `credentials_file` outside the team.** It identifies the OAuth app, and although it's not "secret" in the per-user sense, leaking it lets an attacker stand up a fake "claude-mirror" against your GCP project.
- **`restore --backend NAME` is your friend during outages.** When Drive is down, `claude-mirror restore <ts> <path> --backend sftp` reads directly from the mirror.

---

## H. Multi-project enterprise

### Purpose

Same machine, multiple distinct projects, possibly with different backend choices per project. The user has, say, a work project on the company's Drive, a personal project on their own Drive (different Google account), a side hustle's notes on Dropbox, and a research wiki on a Nextcloud-via-WebDAV instance — all running side by side without interfering. One watcher process handles all of them.

This is the right scenario when:

- One operator manages many independent claude-mirror projects.
- Different projects have different backend constraints (compliance: work files on the company's Drive only; personal files anywhere; OSS files on Dropbox; etc).
- You want a single watcher service per machine and not N watchers fighting for resources.

This is **not** about cross-project synchronization — each project is its own island. If you want one project to appear under two paths, see [Scenario B](#b-personal-multi-machine-sync) (multi-machine, same project) or [Scenario D](#d-multi-backend-redundancy-tier-2) (one project, multiple backends).

### How to implement

Backend choice: **whatever each project requires**. claude-mirror auto-detects the right config from `cwd` for every command, so different projects can use entirely different backends without any per-command flags.

Layout:

```
~/.config/claude_mirror/
├── work-credentials.json         # OAuth client for the work GCP project
├── work-token.json               # work Google account token
├── personal-credentials.json     # OAuth client for the personal GCP project
├── personal-token.json           # personal Google account token
├── dropbox-sidehustle-token.json # Dropbox token for the side-hustle project
├── webdav-research-token.json    # WebDAV creds for the research wiki
│
├── work-design-doc.yaml          # backend: googledrive (work account)
├── work-runbooks.yaml            # backend: googledrive (work account)
├── personal-journal.yaml         # backend: googledrive (personal account)
├── sidehustle.yaml               # backend: dropbox
├── research-wiki.yaml            # backend: webdav
└── homelab-notes.yaml            # backend: sftp (NAS at home)
```

Two configs from this layout, showing different backends on the same machine:

```yaml
# ~/.config/claude_mirror/work-design-doc.yaml
backend: googledrive
project_path: /Users/alice/work/design-doc
drive_folder_id: 1AAA...
gcp_project_id: company-shared-gcp
pubsub_topic_id: claude-mirror-design-doc
credentials_file: /Users/alice/.config/claude_mirror/work-credentials.json
token_file: /Users/alice/.config/claude_mirror/work-token.json
file_patterns:
  - "**/*.md"
machine_name: alice-laptop
user: alice
keep_last: 30
keep_daily: 14
```

```yaml
# ~/.config/claude_mirror/sidehustle.yaml
backend: dropbox
project_path: /Users/alice/personal/sidehustle
dropbox_app_key: uao2pmhc0xgg2xj
dropbox_folder: /claude-mirror/sidehustle
token_file: /Users/alice/.config/claude_mirror/dropbox-sidehustle-token.json
file_patterns:
  - "**/*.md"
machine_name: alice-laptop
user: alice
```

Bring-up — repeat per project:

```bash
cd /Users/alice/work/design-doc
claude-mirror init --wizard   # picks up cwd as default project_path
claude-mirror auth

cd /Users/alice/personal/sidehustle
claude-mirror init --wizard --backend dropbox
claude-mirror auth

cd /Users/alice/research/wiki
claude-mirror init --wizard --backend webdav
claude-mirror auth
```

A single watcher handles all of them:

```bash
claude-mirror watch-all
# Auto-discovers every config in ~/.config/claude_mirror/, starts one
# notifier thread per project, each picking the right notifier for its backend
# (Pub/Sub for Drive, longpoll for Dropbox, polling for WebDAV/SFTP/OneDrive).
```

Or via `claude-mirror-install` for an auto-starting service.

### Daily ops behaviour

| Event | What happens |
|---|---|
| You `cd` into any project directory | Subsequent `claude-mirror` commands auto-detect the right config via `find-config`. No flags needed. |
| You run `claude-mirror push` from `~/work/design-doc` | Pushes only that project. Touches the work Drive, work token, work GCP project. Side hustle and research wiki are untouched. |
| You `cd ~/personal/sidehustle` and `push` | Same command, different project, different backend (Dropbox), different token. Transparent. |
| `watch-all` sees a Drive notification for the work project | Routes the event to the right project's inbox file (`/Users/alice/work/design-doc/.claude_mirror_inbox.jsonl`). The desktop notification names the project. |
| You add a new project (`init` from a new directory) | The running `watch-all` daemon receives a `SIGHUP` and picks up the new config without restart. (Or run `claude-mirror reload` to force the rescan.) |
| You retire a project | Delete its YAML and token files. The next `watch-all` reload drops the corresponding watcher thread. The remote folder is untouched (delete it manually if you want). |
| Dropbox token expires for the side hustle | Only the side-hustle project surfaces an `auth` warning. Work, research, and homelab projects continue working fine. |
| You upgrade claude-mirror | One install, all projects benefit. The watcher must be restarted to pick up the new code (the auto-start service script will do this on the next login or you can `kill` it and let the supervisor restart it). |

### Pitfalls and tips

- **`find-config` is your friend.** Run `claude-mirror find-config` from any directory to see which config the tool would pick. If it's wrong, your `project_path` field disagrees with where you actually are. The lookup walks parent directories.
- **Different Google accounts = different credential files.** The work account and the personal account each need their own `*-credentials.json`. The wizard prompts for the path on each `init`.
- **One watcher process is the right model.** Don't run multiple `claude-mirror watch` processes — they'll race on inbox files and duplicate desktop notifications. `watch-all` exists for exactly this scenario.
- **Snapshot retention is per-project.** Work might want `keep_last: 100`; the side hustle might want `keep_last: 7`. Set them independently.
- **Slack channels per project.** The whole point of separate projects is they don't interfere; give each one its own webhook (or its own channel-override) so the team chat for the work project doesn't get spammed by side-hustle pushes.
- **`watch-all --config <path> ...` lets you watch a subset.** If most projects should be watched but one is paused, list only the ones you want — the others are simply not subscribed-to until you re-include them.
- **Retire projects deliberately.** Just deleting the YAML doesn't delete the remote — that's a feature (you keep the snapshot history) but means storage costs persist until you `delete --local` the project or wipe the remote folder by hand.

---

## See also

- [faq.md](faq.md) — 30-second answers across auth, sync, snapshots, notifications, performance, and migration.
- [admin.md](admin.md) — snapshots, retention, watcher service, multi-backend Tier 2 reference.
- [cli-reference.md](cli-reference.md) — every command, every flag.
- [conflict-resolution.md](conflict-resolution.md) — interactive prompt + `--no-prompt --strategy` for unattended runs.
- [profiles.md](profiles.md) — credentials profiles for multi-project / multi-account setups.

---

← Back to [README index](../README.md)
