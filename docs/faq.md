← Back to [README index](../README.md)

# Frequently asked questions

A wayfinding page. Each entry is a 30-second answer with a concrete command and a "See also" link to the depth-doc that owns the topic. If your question isn't here, the canonical references are [admin.md](admin.md), [cli-reference.md](cli-reference.md), [scenarios.md](scenarios.md), and the per-backend pages under [backends/](backends/).

---

## Contents

- [Getting started](#getting-started)
  - [Which backend should I pick?](#which-backend-should-i-pick)
  - [Do I need a GCP project for Google Drive?](#do-i-need-a-gcp-project-for-google-drive)
  - [What's the smallest setup that works?](#whats-the-smallest-setup-that-works)
- [Auth and credentials](#auth-and-credentials)
  - [Drive auth expires every 24 hours](#drive-auth-expires-every-24-hours)
  - [How do I share credentials across N projects?](#how-do-i-share-credentials-across-n-projects)
  - [I rotated my Drive password — what now?](#i-rotated-my-drive-password--what-now)
  - [Dropbox auth code expired before I pasted it](#dropbox-auth-code-expired-before-i-pasted-it)
  - [OneDrive AADSTS50058 / no cached accounts found](#onedrive-aadsts50058--no-cached-accounts-found)
- [Sync workflow](#sync-workflow)
  - [Why didn't my collaborator's push show up?](#why-didnt-my-collaborators-push-show-up)
  - [Status says 'conflict' — what do I do?](#status-says-conflict--what-do-i-do)
  - [How do I sync only some files?](#how-do-i-sync-only-some-files)
  - [How do I run sync on a schedule?](#how-do-i-run-sync-on-a-schedule)
  - [I accidentally pasted an API key into my notes. How do I scrub it before pushing?](#i-accidentally-pasted-an-api-key-into-my-notes-how-do-i-scrub-it-before-pushing)
- [Multi-machine and multi-user](#multi-machine-and-multi-user)
  - [I'm syncing the same project from two machines — what do I run?](#im-syncing-the-same-project-from-two-machines--what-do-i-run)
  - [We're a team of three sharing one Drive folder](#were-a-team-of-three-sharing-one-drive-folder)
  - [How do I add a backup mirror?](#how-do-i-add-a-backup-mirror)
- [Snapshots and recovery](#snapshots-and-recovery)
  - [I deleted a file — can I get it back?](#i-deleted-a-file--can-i-get-it-back)
  - [How do I prune old snapshots?](#how-do-i-prune-old-snapshots)
  - [My remote is full of orphan blobs after a failed migration](#my-remote-is-full-of-orphan-blobs-after-a-failed-migration)
- [Notifications](#notifications)
  - [I want notifications in Slack](#i-want-notifications-in-slack)
  - [...in Discord, Teams, or a custom webhook](#in-discord-teams-or-a-custom-webhook)
  - [Notifications fire too often](#notifications-fire-too-often)
  - [My watcher isn't firing notifications](#my-watcher-isnt-firing-notifications)
- [Performance and reliability](#performance-and-reliability)
  - [Push is slow on first sync of a 50-MB project](#push-is-slow-on-first-sync-of-a-50-mb-project)
  - [I want bandwidth-throttled uploads](#i-want-bandwidth-throttled-uploads)
  - [I keep getting HTTP 429](#i-keep-getting-http-429)
  - [Network drops mid-push — what happens?](#network-drops-mid-push--what-happens)
- [Common gotchas](#common-gotchas)
  - [`claude-mirror` command not found after pipx install](#claude-mirror-command-not-found-after-pipx-install)
  - [The skill doesn't surface notifications in Claude Code](#the-skill-doesnt-surface-notifications-in-claude-code)
  - [Tab completion isn't working](#tab-completion-isnt-working)
  - [Watcher not starting on login (macOS)](#watcher-not-starting-on-login-macos)
- [Migration and upgrade](#migration-and-upgrade)
  - [Upgrading from < 0.5.1 — what changes?](#upgrading-from--051--what-changes)
  - [Switching backends — can I?](#switching-backends--can-i)

---

## Getting started

tl;dr: pick a backend, install via pipx, run `init --wizard`, run `auth`, run `push`. Five minutes for SFTP / Dropbox; ten to fifteen for Drive (because of the GCP project).

### Which backend should I pick?

Decision tree:

- **Solo developer, want zero cloud setup, already have an SSH-accessible server (VPS / NAS / shared hosting)** → **SFTP**. Cheapest path. No browsers, no OAuth, no API quotas.
- **Solo developer with a Dropbox account, no servers** → **Dropbox**. The wizard handles everything except creating one Dropbox app at [dropbox.com/developers](https://www.dropbox.com/developers).
- **Team collaboration on Google Drive infrastructure (most teams)** → **Google Drive**. Best latency (sub-second Pub/Sub), best multi-user story. Costs an hour of GCP project setup the first time.
- **Microsoft 365 / Office shop** → **OneDrive**. Same multi-user story as Drive at slightly higher latency (polling, not push).
- **Self-hosted Nextcloud / OwnCloud / Synology / QNAP / Apache mod_dav** → **WebDAV**. Works against anything that speaks WebDAV.
- **Object-storage / pay-per-byte (Cloudflare R2, Backblaze B2, MinIO, AWS S3, Wasabi, Storj)** → **S3-compatible**. Single backend transparently supports every S3-API provider via the `s3_endpoint_url` config. boto3's default credential chain works for IAM-role machines.
- **Already have a NAS or Windows file share you mount via SMB** → **SMB/CIFS**. Synology, QNAP, TrueNAS, macOS Sharing, generic Samba — SMB2/3 only with optional per-message AES encryption.
- **Stuck on legacy shared hosting (cPanel / DirectAdmin) with no SFTP** → **FTP / FTPS**. Stdlib-only, no extra dependencies; use `ftp_tls: explicit` (FTPS) wherever the server supports it.
- **Belt-and-braces (paranoid about any one provider)** → **Tier 2 mirroring**: pick any primary, mirror to one or more others.

**See also:** [docs/scenarios.md](scenarios.md) for full topology walkthroughs (A through J, E omitted).

### Do I need a GCP project for Google Drive?

Yes. The Google Drive backend needs OAuth2 credentials and (for real-time collaborator notifications) a Pub/Sub topic — both are GCP-project-scoped resources. The free tier is sufficient for personal and small-team use; the wizard's `--auto-pubsub-setup` flag (since v0.5.47) creates the topic, the per-machine subscription, and the IAM grant for Drive's service account in one step.

If you want the simplest possible path with no GCP setup at all, pick **Dropbox** or **SFTP** instead.

**See also:** [docs/backends/google-drive.md](backends/google-drive.md) — full GCP project / OAuth / Pub/Sub walkthrough, including the `--auto-pubsub-setup` flag.

### What's the smallest setup that works?

Three commands, against Dropbox (one provider account, no servers):

```bash
# 1. Install
pipx install claude-mirror

# 2. Initialize this directory as a claude-mirror project
cd /path/to/your/project
claude-mirror init --wizard --backend dropbox

# 3. Authenticate (opens a browser)
claude-mirror auth

# 4. First push
claude-mirror push
```

That's it. The wizard prompts for one Dropbox app key (paste it in once, reuse across as many projects as you like — see profiles below).

For an SFTP setup the shape is identical except the `auth` step is skipped — the wizard validates the SSH connection and host fingerprint inline.

**See also:** [README — Your first project](../README.md#your-first-project) for the side-by-side Drive and SFTP walkthroughs.

---

## Auth and credentials

tl;dr: tokens regenerate on `claude-mirror auth`. The most common surprise is the Drive 7-day refresh-token lifetime when the OAuth consent screen sits in `Testing` mode.

### Drive auth expires every 24 hours

Symptom: every couple of days `claude-mirror push` fails with `RefreshError: Reauthentication is needed`, even though nothing changed about your account.

Root cause: Google's OAuth consent screen has two states — `Testing` and `In Production`. In `Testing` mode, refresh tokens expire after **7 days** (Google's safety net for unfinished apps that never got reviewed). The fix is to publish the app — this does NOT require a Google review for personal use, and gives you the normal long-lived refresh-token behaviour.

Steps:

1. Open `https://console.cloud.google.com/apis/credentials/consent` for your GCP project.
2. Click **Publish App**.
3. Re-run `claude-mirror auth --config ~/.config/claude_mirror/<project>.yaml`.

Workspace tenants with Cloud Session Control settings can also force token expiry — check with your IT admin before publishing. Same `auth` re-run resolves it once the policy is right.

**See also:** [docs/backends/google-drive.md — Diagnosing setup problems](backends/google-drive.md#diagnosing-setup-problems) for the full diagnosis flow.

### How do I share credentials across N projects?

Use a **credentials profile**. Define the credentials path / app keys / token-file path **once** at `~/.config/claude_mirror/profiles/<name>.yaml`, then attach every project YAML to it via `claude-mirror --profile <name>`:

```bash
# One-time scaffold
claude-mirror profile create work --backend googledrive

# Each project inherits — wizard skips credential prompts
cd ~/projects/research
claude-mirror --profile work init --wizard
cd ~/projects/strategy
claude-mirror --profile work init --wizard
```

The global `--profile NAME` flag (since v0.5.49) goes BEFORE the subcommand. After init, each project YAML carries `profile: work` at the top so subsequent commands pick it up automatically. Project YAML values override profile defaults on a per-field basis, so any one project can escape-hatch a single setting.

**See also:** [docs/profiles.md](profiles.md) — full walkthrough with per-backend sample profile YAMLs and the merge-precedence rule.

### I rotated my Drive password — what now?

Re-run `claude-mirror auth`. The OAuth refresh token issued before the password change is still valid (it's bound to the OAuth grant, not the password) — but if you also revoked the app's access from `myaccount.google.com/permissions`, you need a fresh `auth` to re-grant.

```bash
claude-mirror auth --config ~/.config/claude_mirror/<project>.yaml
```

The stale token file is replaced; the rest of the YAML (folder ID, GCP project ID, Pub/Sub topic ID) is preserved.

**See also:** [README — Troubleshooting](../README.md#refresherror-reauthentication-is-needed-google-drive).

### Dropbox auth code expired before I pasted it

Dropbox's one-shot authorization code has a short lifetime (typically 10 minutes). If the code expired, the flow fails with `invalid_grant` or "Authentication code expired".

Fix: re-run `claude-mirror auth` and paste the code on first try. Open the browser tab and the terminal side-by-side so the round trip is fast.

**See also:** [docs/backends/dropbox.md](backends/dropbox.md) for the full PKCE flow.

### OneDrive AADSTS50058 / no cached accounts found

Symptom: OneDrive operations fail with `AADSTS50058: A silent sign-in request was sent but no user is signed in` or `no cached accounts found`.

Root cause: the MSAL token cache file is missing, corrupted, or its refresh token has been revoked. The fix is to rebuild the cache via the device-code flow:

```bash
claude-mirror auth --config ~/.config/claude_mirror/<project>.yaml
```

Follow the device-code prompt in your browser. This re-populates `~/.config/claude_mirror/onedrive-<project>-token.json`.

**See also:** [docs/backends/onedrive.md](backends/onedrive.md).

---

## Sync workflow

tl;dr: `status` shows what changed, `push` / `pull` / `sync` apply the change. Conflicts get an interactive prompt by default; `--no-prompt --strategy` is the cron-friendly alternative.

### Why didn't my collaborator's push show up?

Two common causes:

1. **The watcher isn't running.** Real-time notifications need `claude-mirror watch` (or `watch-all`) running in the background. Confirm:

   ```bash
   pgrep -fl claude-mirror     # should list a watch process
   ```

   If nothing comes back, start one (or run the installer once with `claude-mirror-install` to wire up the launchd / systemd service).

2. **(Drive only) Pub/Sub IAM grant is missing.** Drive's push-notification service account (`apps-storage-noreply@google.com`) needs `roles/pubsub.publisher` on your topic. About 70% of self-serve Drive setups silently miss this — Pub/Sub locally appears to work, but Drive itself never publishes change events. Diagnose with:

   ```bash
   claude-mirror doctor --backend googledrive
   ```

   If the IAM grant check fails, fix it in one step:

   ```bash
   claude-mirror init --auto-pubsub-setup --config ~/.config/claude_mirror/<project>.yaml
   ```

While waiting on the fix, run `claude-mirror pull` manually to grab the missed changes.

**See also:** [docs/admin.md — Drive deep checks](admin.md#drive-deep-checks) for the full check matrix.

### Status says 'conflict' — what do I do?

`conflict` means both your local file AND the remote version changed since the last sync. Run:

```bash
claude-mirror sync
```

You get an interactive prompt per conflicted file: **L** keep local, **D** keep drive (or remote), **E** open `$EDITOR` with conflict markers, **S** skip.

For unattended runs (cron, launchd, systemd) the prompt would block forever — there's no TTY. Pass `--no-prompt --strategy` (since v0.5.49) to auto-resolve:

```bash
claude-mirror sync --no-prompt --strategy keep-local    # local always wins
claude-mirror sync --no-prompt --strategy keep-remote   # remote always wins
```

Every auto-resolution is logged to `_sync_log.json` with the strategy that won, so audits can spot every overwrite via `claude-mirror log`.

**See also:** [docs/conflict-resolution.md](conflict-resolution.md) — full prompt walkthrough and the `$EDITOR` merge flow.

### I have a conflict and I'm not sure how to merge it. Can the agent help?

Yes — the **AGENT-MERGE** flow is built for exactly that. When `claude-mirror sync` finds a file changed on BOTH sides since the last sync, it writes a structured JSON envelope to `~/.local/state/claude-mirror/<project-slug>/conflicts/` per conflicted file. The [skill](../skills/claude-mirror.md) running in your agent (Claude Code, Cursor, Codex, …) picks up the envelope, proposes a merged version, **shows you the proposal and asks for explicit confirmation**, and applies it on your "yes":

```bash
claude-mirror conflict list                                      # see what's pending
claude-mirror conflict show <path> --format markers              # 3-way <<<<<<< / ======= / >>>>>>> markers — every agent IDE knows this
claude-mirror conflict apply <path> --merged-file <tmp>          # write merged content + clear envelope + push
```

claude-mirror itself binds to NO LLM API — no Anthropic SDK call, no Ollama HTTP call, no API key requirement. The CLI is purely file plumbing; the skill describes the agent contract, and your agent does the merge cognition. **The user is always in the loop:** the skill is instructed to never apply a merge without showing the proposed content and getting explicit confirmation first.

Existing behaviour is unchanged for users without the skill — the interactive `[L]ocal / [D]rive / [E]ditor / [S]kip` prompt still fires on `sync`, and the envelope is just additional information stored on disk. If you skip a conflict interactively, the envelope persists so the agent can still help later.

**See also:** [docs/conflict-resolution.md — Agent-driven merge via the skill](conflict-resolution.md#agent-driven-merge-via-the-skill-agent-merge), [docs/cli-reference.md — `conflict`](cli-reference.md#conflict).

### How do I sync only some files?

Two complementary mechanisms:

1. **`file_patterns` in the project YAML** — positive list of globs to include. Default `["**/*.md"]` keeps just markdown; set to `["**/*"]` for everything, or `["**/*.py", "**/*.md"]` to scope to a subset.

2. **`.claude_mirror_ignore` at the project root** (since v0.5.45) — gitignore-style exclusions that complement YAML `exclude_patterns`. Supports `**` recursive globs, `/anchored` rules, `dir/` directory-only rules, and `!negation` re-include rules.

```
# .claude_mirror_ignore
node_modules/
**/*.tmp
/build
!build/keep-this.md
```

Both layers must vote "keep" for a file to sync. The `.claude_mirror_ignore` file is itself auto-excluded from sync (gitignore convention).

**See also:** [docs/scenarios.md — F. Selective sync](scenarios.md#f-selective-sync) and [docs/admin.md — `.claude_mirror_ignore`](admin.md#claude_mirror_ignore--project-tree-exclusions).

### How do I run sync on a schedule?

Two complementary patterns:

- **Real-time (preferred)** — `claude-mirror watch-all` running as a launchd / systemd service, started by `claude-mirror-install`. Sub-second latency on Drive (Pub/Sub), seconds on Dropbox (long-poll), `poll_interval` (default 30s) on OneDrive / WebDAV / SFTP / FTP / S3 / SMB.

- **Scheduled (belt-and-braces)** — cron with `--no-prompt --strategy`. Sample crontab:

  ```cron
  # Hourly: push pending edits, pull remote edits, auto-resolve conflicts in favour of local
  0 * * * *  cd /home/alice/myproject && /home/alice/.local/bin/claude-mirror sync --no-prompt --strategy keep-local >> /tmp/claude-mirror-cron.log 2>&1
  ```

  Switch to `--strategy keep-remote` if the cron host is a passive backup target. Both modes write to `_sync_log.json` so every auto-resolution is auditable.

If you also want a single one-shot poll cycle in cron (no bidirectional sync, no conflict logic), use `claude-mirror watch --once --quiet` instead.

**See also:** [docs/admin.md — Unattended sync via cron](admin.md#unattended-sync-via-cron) for additional crontab samples.

### I accidentally pasted an API key into my notes. How do I scrub it before pushing?

`claude-mirror redact PATH` is the pre-push safety net. Run it on the project root (or a specific file) to scan for likely-secret patterns — AWS access keys, GitHub tokens, OpenAI / Anthropic / Google API keys, Slack webhooks and bot tokens, JWTs, password assignments, and a few more.

```bash
claude-mirror redact .                    # dry-run scan (default: never writes)
claude-mirror redact . --apply            # interactive replace/keep/skip-file/quit prompt
claude-mirror redact . --apply --yes      # auto-replace every finding (non-interactive)
```

Dry-run by default — without `--apply`, the command lists every finding as `path:line [kind] match` and exits 0. No disk writes happen until you opt in with `--apply`. The replacement marker is `<REDACTED:KIND>` (e.g. `<REDACTED:aws-access-key>`); re-running `redact` on already-redacted text is a no-op.

For a permanent guard, wire `claude-mirror redact <project> --apply --yes` into your project's `.git/hooks/pre-commit` so secrets never even reach the staged tree — see [`docs/admin.md` — Pre-push secret scanning with redact](admin.md#pre-push-secret-scanning-with-redact). Full kind catalogue + sample interactive transcript: [`docs/cli-reference.md` — `redact`](cli-reference.md#redact).

---

## Multi-machine and multi-user

tl;dr: same `claude-mirror init --wizard --backend X` on every machine, against the **same** remote folder. Tokens are per-machine; credentials (the OAuth client / app key) can be shared.

### I'm syncing the same project from two machines — what do I run?

Once per machine:

```bash
# On laptop and desktop both:
cd /path/to/myproject
claude-mirror init --wizard --backend googledrive
# Use the SAME folder ID, GCP project ID, Pub/Sub topic ID on both machines.
# The Pub/Sub subscription ID will differ — the wizard derives it per machine.
claude-mirror auth
claude-mirror push     # first machine to push wins
# On the other machine:
claude-mirror pull     # picks up the first machine's content
```

Then leave `claude-mirror watch` (or `watch-all`) running on both machines. Each machine sees the other's pushes within sub-seconds (Drive Pub/Sub) or up to `poll_interval` (other backends).

**See also:** [docs/scenarios.md — B. Personal multi-machine sync](scenarios.md#b-personal-multi-machine-sync) for the full walkthrough.

### We're a team of three sharing one Drive folder

Same idea, scaled. Each teammate runs `init --wizard` on their own machine pointing at the **shared** Drive folder ID and the **shared** GCP Pub/Sub topic. Each authenticates as themselves (different Google identities) — `claude-mirror auth` writes a per-user token file at `~/.config/claude_mirror/<user>-token.json`.

Critical: each teammate needs a **per-machine Pub/Sub subscription** on the shared topic. The wizard auto-derives unique subscription IDs from the machine name; if you also pass `--auto-pubsub-setup`, the IAM grant for `apps-storage-noreply@google.com` is added once and re-used by every subscription.

```bash
# Each teammate runs, on their own machine:
claude-mirror init --wizard --backend googledrive --auto-pubsub-setup
claude-mirror auth
```

**See also:** [docs/scenarios.md — C. Multi-user collaboration](scenarios.md#c-multi-user-collaboration) and [docs/scenarios.md — G. Multi-user + multi-backend (production-realistic)](scenarios.md#g-multi-user--multi-backend-production-realistic) for end-to-end transcripts.

### How do I add a backup mirror?

**Tier 2 mirroring**: keep your primary backend (e.g. Drive) and add one or more additional backends in parallel — every push fans out to all of them, every snapshot is replicated.

Two-step setup:

1. Initialize the mirror as if it were a normal project (different `backend`, same `project_path`):

   ```bash
   claude-mirror init --wizard --backend sftp --project /path/to/myproject \
     --config ~/.config/claude_mirror/myproject-sftp.yaml
   ```

2. Edit the **primary** YAML and add `mirror_config_paths`:

   ```yaml
   mirror_config_paths:
     - ~/.config/claude_mirror/myproject-sftp.yaml
   ```

Then run `claude-mirror seed-mirror` once to backfill the new mirror. After that, every `push` writes to both backends in parallel; transient errors on one don't block the other (per-mirror retry queues).

**See also:** [docs/admin.md — Multi-backend mirroring (Tier 2)](admin.md#multi-backend-mirroring-tier-2) for the full configuration reference.

---

## Snapshots and recovery

tl;dr: every push and every sync auto-creates a snapshot. `history PATH` shows every version of any file; `restore TIMESTAMP PATH` rolls a single file (or the whole project) back to any past timestamp.

### I deleted a file — can I get it back?

Yes. Snapshots are per-push and per-sync; the file is in the most recent one taken before the deletion.

```bash
# Find the snapshot timestamps that contain the file
claude-mirror history path/to/file.md

# Restore a single file from a specific snapshot
claude-mirror restore 2026-05-08T10-15-22Z path/to/file.md
```

`restore` runs in dry-run mode by default — it shows the planned operation as a Rich table (Path / Action / Source backend / Size). Add `--apply` to actually write to local disk, or `--apply --yes` to skip the confirmation prompt for non-interactive use.

For a whole-project rollback, omit the `PATH` argument. The interactive prompt is `This will overwrite the entire snapshot in /your/project. Continue? [y/N]`.

**See also:** [docs/admin.md — Restore a snapshot](admin.md#restore-a-snapshot) and [docs/cli-reference.md — `restore`](cli-reference.md#restore).

### How do I prune old snapshots?

Two commands:

- **`claude-mirror forget TIMESTAMP`** — precise: delete one specific snapshot (or several by passing multiple timestamps). Useful for surgical cleanup.

- **`claude-mirror prune`** — retention-policy driven. Reads `keep_last` / `keep_within_days` / `keep_daily` / etc. from the project YAML and computes the union of every selector's keep-set; everything else is pruned. Auto-runs after each successful `push` if any retention field is non-zero.

Both are dry-run by default per the project's destructive-ops convention. Add `--apply --yes YES` to actually delete (literal string `YES` required).

```bash
claude-mirror prune                                # dry-run preview
claude-mirror prune --apply --yes YES              # apply
claude-mirror forget 2026-04-01T03-00-00Z          # dry-run preview
claude-mirror forget 2026-04-01T03-00-00Z --apply --yes YES   # apply
```

**See also:** [docs/admin.md — Auto-pruning by retention policy](admin.md#auto-pruning-by-retention-policy) and [docs/admin.md — Delete old snapshots](admin.md#delete-old-snapshots).

### My remote is full of orphan blobs after a failed migration

Symptom: the remote folder size grew, but `claude-mirror snapshots` doesn't show new entries. Some `blobs/` content is referenced by no manifest — orphans.

```bash
claude-mirror gc --backend googledrive               # dry-run preview
claude-mirror gc --backend googledrive --delete --yes YES   # apply
```

`gc` walks every manifest, builds the set of referenced blob IDs, and lists everything in `blobs/` that isn't referenced. Dry-run shows what would be deleted without touching the remote; `--delete --yes YES` (typed `YES` per the destructive-ops convention) actually deletes them.

In Tier 2 setups, run once per backend.

**See also:** [docs/cli-reference.md — `gc`](cli-reference.md#gc).

---

## Notifications

tl;dr: opt-in, per-project, webhook-based. Slack since v0.5.0; Discord / Teams / Generic-webhook since v0.5.47. Failures are logged and silently swallowed — they will never block a sync.

### I want notifications in Slack

```yaml
# in your project YAML
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx
slack_channel: "#claude-mirror"      # optional override
```

Then trigger any push / pull / sync — the message arrives within seconds.

**See also:** [admin.md — Slack](admin.md#slack) for the step-by-step Slack-app-creation walkthrough.

### ...in Discord, Teams, or a custom webhook

Same shape. Each backend is opt-in via its own config field, and multiple can be enabled simultaneously — they fire sequentially after Slack. Available since v0.5.47:

```yaml
# Discord
discord_enabled: true
discord_webhook_url: https://discord.com/api/webhooks/<id>/<token>

# Microsoft Teams
teams_enabled: true
teams_webhook_url: https://<tenant>.webhook.office.com/webhookb2/<token>

# Generic webhook (n8n, Make, Zapier, custom endpoints)
webhook_enabled: true
webhook_url: https://your-endpoint.example.com/claude-mirror
webhook_extra_headers:
  Authorization: Bearer <your-token>
  X-Tenant-ID: <your-tenant>
```

Discord uses action-coloured embeds (green push / blue pull / red delete). Teams uses Adaptive Cards. The Generic webhook posts a schema-stable v1 JSON envelope with `webhook_extra_headers` honoured for auth / routing.

**See also:** [docs/admin.md — Notifications](admin.md#notifications).

### Notifications fire too often

If every minor edit is producing noise, options:

- **Disable notifications for specific event types** via `slack_events` (or `discord_events` / `teams_events` / `webhook_events`) — list of allowed event types from `["push", "pull", "sync", "delete"]`. Default is all four.
- **Mirror to a less-noisy channel** by generating a second webhook and pointing it at a low-traffic project channel.
- **Throttle the watcher** by raising `poll_interval` (OneDrive / WebDAV / SFTP only — Drive / Dropbox are event-driven so they can't over-fire).

Templating support for fully custom message bodies is under active development (TMPL); until it ships, the four-event filter is the supported coarse control.

**See also:** [docs/admin.md — Notifications](admin.md#notifications) for the full config-field reference.

### My watcher isn't firing notifications

Three layers to check, top-down:

1. **Is the watcher running?** `pgrep -fl claude-mirror` should list at least one watch process. If not, `claude-mirror watch-all` (or restart the launchd / systemd service via `claude-mirror-install`).

2. **Is the per-backend channel healthy?** `claude-mirror doctor --backend NAME` runs deep, backend-specific checks for every backend — Drive (Pub/Sub topic + subscription + IAM grant), Dropbox (token shape + scopes + folder access), OneDrive (token cache + Azure GUID + scopes), WebDAV (PROPFIND + DAV class), SFTP (host fingerprint + key perms + auth), FTP (host reachable + TLS handshake + auth + folder write), S3 (bucket reachable + list + write permissions + region consistency), SMB (SMB2/3 negotiation + auth + share access + folder write).

3. **(Drive) Is the IAM grant present?** This is the single most common silent failure — about 70% of self-serve Drive setups miss it. The deep `doctor` check reports it explicitly; `claude-mirror init --auto-pubsub-setup --config <path>` adds it idempotently.

**See also:** [docs/admin.md — Drive deep checks](admin.md#drive-deep-checks) and [docs/admin.md — Doctor](admin.md#doctor).

---

## Performance and reliability

tl;dr: live transfer progress with ETA + bytes/sec on every push / pull / sync (since v0.5.49). Bandwidth throttle and global rate-limit handling are config-tunable.

### Push is slow on first sync of a 50-MB project

A few realistic causes, in descending order of likelihood:

1. **Network bandwidth.** A residential 10 Mbps upload pushes 50 MB in roughly 40 seconds. The progress bar (since v0.5.49) shows live ETA + bytes/sec — if the bytes/sec line matches your link's actual upload, this is just real-world throughput.

2. **Per-file API overhead.** Drive's REST API charges a few hundred milliseconds per file metadata write. A 50-MB project of 5,000 small markdown files spends most of its time on metadata, not bytes. The `parallel_workers` config field (default 4) raises concurrency:

   ```yaml
   parallel_workers: 8
   ```

3. **Throttling.** If you've set `max_upload_kbps`, the token bucket is doing exactly what you asked for. Verify with `claude-mirror status --short` that the throttle is intentional.

4. **Snapshot creation on first push.** The first push also creates the initial `blobs/` directory tree. Subsequent pushes only add new blobs.

**See also:** [docs/admin.md — Transfer progress](admin.md#transfer-progress-live-eta--bytessec) and [docs/admin.md — Performance and bandwidth control](admin.md#performance-and-bandwidth-control).

### I want bandwidth-throttled uploads

```yaml
# in your project YAML — null (default) disables throttling
max_upload_kbps: 500   # cap at 500 KB/s (~4 Mbps)
```

Token-bucket implementation, integrated across all eight backends (since v0.5.45). In Tier 2 setups each mirror has its own field — throttle Drive but not SFTP, or vice versa.

**See also:** [docs/admin.md — Bandwidth throttling: `max_upload_kbps`](admin.md#bandwidth-throttling-max_upload_kbps).

### I keep getting HTTP 429

Symptom: `HTTP 429 Too Many Requests` from Drive / Dropbox / OneDrive / WebDAV during a large push or sync.

Since v0.5.49, the global rate-limit `BackoffCoordinator` auto-paces every in-flight upload on a single shared deadline whenever any worker reports `RATE_LIMIT_GLOBAL`. You'll see exactly two lines:

```
Backend reports rate limit. Pausing 30s before retrying.
Throttle cleared. Resuming uploads.
```

instead of N TRANSIENT warnings. Drive's `userRateLimitExceeded` / `rateLimitExceeded` reasons (which come as 403 + reason, not 429), Dropbox's `RateLimitError`, Microsoft Graph's 429 + `Retry-After`, and WebDAV 429 are all classified uniformly as `RATE_LIMIT_GLOBAL`. SFTP has no 429 equivalent (SSH).

For cron jobs that should fail fast rather than wait out a long backoff:

```yaml
max_throttle_wait_seconds: 60   # default 600
```

**See also:** [docs/admin.md — Rate-limit handling](admin.md#rate-limit-handling).

### Network drops mid-push — what happens?

Per-file classified-error handling. On a network drop, the in-flight file is classified `TRANSIENT` and queued for next-push retry via the manifest's `pending_retry` state. Your next `claude-mirror push` (or the `--auto-retry-pending` config in Tier 2 setups, default `true`) re-attempts queued files first.

To inspect the queue without pushing:

```bash
claude-mirror status --pending             # files in pending_retry on any backend
claude-mirror retry --dry-run              # preview what next push would re-attempt
claude-mirror retry --backend googledrive  # re-attempt one mirror only
```

`AUTH` / `QUOTA` / `PERMISSION` failures move to `failed_perm` rather than `pending_retry` — those need operator action (re-auth, free quota, fix permissions) before retrying.

**See also:** [docs/admin.md — Multi-backend mirroring (Tier 2)](admin.md#multi-backend-mirroring-tier-2) for the full retry semantics, and [docs/cli-reference.md — `retry`](cli-reference.md#retry).

---

## Common gotchas

tl;dr: most "it doesn't work after install" reports are PATH issues, completion-source-not-loaded issues, or macOS notification-permission issues. None of them are bugs.

### `claude-mirror` command not found after pipx install

pipx installs into `~/.local/bin/`, which isn't on PATH by default on macOS / Linux. Fix:

```bash
pipx ensurepath          # adds ~/.local/bin to PATH
exec $SHELL              # restart shell to pick up the change
claude-mirror --version  # verify
```

If `pipx ensurepath` reports nothing changed but `claude-mirror` still isn't found, source your shell rc explicitly:

```bash
source ~/.zshrc          # or ~/.bashrc
```

### The skill doesn't surface notifications in Claude Code

Notifications surface inline in Claude Code via a `PreToolUse` hook in `~/.claude/settings.json` that runs `claude-mirror inbox` silently before every tool call. Two checks:

1. The installer wired it in. Run `claude-mirror-install` once if you haven't:

   ```bash
   claude-mirror-install
   ```

2. The hook block exists. Open `~/.claude/settings.json` and confirm there's a `hooks.PreToolUse` array containing a `command` of `claude-mirror inbox` (or similar). If not, re-run the installer.

If you ARE getting desktop notifications but they're not appearing inside Claude Code, the watcher is healthy but the hook is missing or malformed. Re-run `claude-mirror-install`.

**See also:** [README — Claude Code skill](../README.md#claude-code-skill) for the inline-vs-desktop notification model.

### Tab completion isn't working

The completion script needs to be sourced into your shell. The installer (`claude-mirror-install`) appends it to your shell rc; new shells pick it up automatically, but the current shell needs an explicit re-source:

```bash
exec $SHELL                           # easiest: restart shell
# or:
source ~/.zshrc                       # zsh
source ~/.bashrc                      # bash
```

If you skipped the installer, install completion manually:

```bash
echo 'eval "$(claude-mirror completion zsh)"' >> ~/.zshrc      # zsh
echo 'eval "$(claude-mirror completion bash)"' >> ~/.bashrc    # bash
claude-mirror completion fish | source                          # fish (current shell)
```

For PowerShell:

```powershell
claude-mirror completion powershell | Out-File -Encoding utf8 -Append $PROFILE.CurrentUserAllHosts
```

**See also:** [docs/cli-reference.md — `completion`](cli-reference.md#completion).

### Watcher not starting on login (macOS)

The launchd agent at `~/Library/LaunchAgents/com.claude-mirror.watch.plist` needs notification permission for desktop notifications to actually fire — but launchd-launched processes have no app bundle, so the system can't grant permission to "the watcher".

Workaround: run `claude-mirror watch` once from a regular Terminal (or iTerm2) window. macOS prompts for notification permission against Terminal itself; grant it, then switch back to the launchd service. The permission grant on the parent (Terminal) covers the launchd-spawned child for subsequent sessions.

If the watcher isn't even starting (no `pgrep -fl claude-mirror` hit), check:

```bash
launchctl list | grep claude-mirror
```

If the agent shows status `78` or similar non-zero, see the launchd troubleshooting in admin.md.

**See also:** [docs/admin.md — Auto-start the watcher](admin.md#auto-start-the-watcher).

---

## Migration and upgrade

tl;dr: claude-mirror reads older configs and manifests transparently; only the v0.5.0 → v0.5.1 on-disk rename needed an explicit migration step.

### Upgrading from < 0.5.1 — what changes?

v0.5.1 renamed the on-disk paths from `claude_sync` to `claude_mirror` (the project itself was renamed in v0.5.0). The config / manifest **schemas** are unchanged — only file and directory names shifted.

For projects created before v0.5.1, run once:

```bash
claude-mirror migrate-state --apply
```

This renames local files (`~/.config/claude_sync/` → `~/.config/claude_mirror/`, project-local `.claude_sync_*` → `.claude_mirror_*`), rewrites token-file paths inside YAMLs, and updates remote-folder predicates so WebDAV / OneDrive listings accept both old and new prefixes during the transition.

For projects created at v0.5.1 or later, `migrate-state` is a no-op; you can run it safely.

**See also:** [README — Migrating from older versions](../README.md#migrating-from-older-versions).

### Switching backends — can I?

Yes — and the safe path is **Tier 2 mirroring as a transition**. Add the new backend as a mirror, run `claude-mirror seed-mirror` to backfill, let it run alongside the primary for as long as you want to verify it, then promote the mirror to primary by swapping config paths.

```bash
# 1. Initialize the new backend as a mirror of the existing primary
claude-mirror init --wizard --backend dropbox --project /path/to/myproject \
  --config ~/.config/claude_mirror/myproject-dropbox.yaml

# 2. Add it to the primary's mirror_config_paths
# (edit ~/.config/claude_mirror/myproject.yaml by hand, or via the cli helper)

# 3. Backfill
claude-mirror seed-mirror --backend dropbox

# 4. Verify for a week or two — every push fans out to both backends.
claude-mirror status --by-backend

# 5. When ready to switch, swap which YAML is the primary and which is the mirror.
```

This is non-destructive: at no point do you lose the original primary's data. If the new backend turns out wrong for your workflow, just remove it from `mirror_config_paths` and continue with the original primary.

**See also:** [docs/admin.md — Multi-backend mirroring (Tier 2)](admin.md#multi-backend-mirroring-tier-2) and [docs/scenarios.md — D. Multi-backend redundancy (Tier 2)](scenarios.md#d-multi-backend-redundancy-tier-2).

---

← Back to [README index](../README.md)
