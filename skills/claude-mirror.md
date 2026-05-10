---
name: claude-mirror
description: Manage Claude project MD file sync via claude-mirror. Use when the user wants to sync, push, pull, delete, check status, view notifications, manage snapshots, or restore files for their Claude project.
---

# claude-mirror skill

You are helping the user manage their Claude project MD file sync via claude-mirror.
Everything is scoped to the **current project** — never mix notifications or state across projects.

## Step 1: Detect the active project config

Run this as a **separate** Bash call (do not combine with other commands):

```bash
claude-mirror find-config
```

`find-config` walks UP from CWD through every ancestor directory looking for a config whose `project_path` matches one of those ancestors — like `git` finding `.git/`. So it works whether the user opened Claude Code at the project root or in any subdirectory.

Capture stdout as the config path. If the command fails (exit code 1):
- Stderr will list every available config with its project_path. Read that listing.
- If exactly ONE config exists, use it via `--config <path>` and proceed.
- If MULTIPLE configs exist and you can't infer which one the user means from the conversation, ask the user which project they're working with before running any other claude-mirror command.
- If NO configs exist, tell the user to run `claude-mirror init --wizard` first.

Use the returned path verbatim (e.g. `~/.config/claude_mirror/default.yaml`) on every subsequent command as `--config <path>`.

## Step 2: Check for pending notifications (project-scoped)

```bash
claude-mirror inbox --config <config-path>
```

If there are pending notifications, clearly report them to the user:
- Who pushed (user@machine)
- Which files changed
- Which project

Then proceed automatically with the remote-change analysis workflow below (Step 3) for all affected files — do not wait for the user to ask.

> **Note:** if the user has configured the `PreToolUse` hook in `~/.claude/settings.json`, `claude-mirror inbox` already ran before this skill was invoked and its output is visible in the conversation context. In that case, skip re-running inbox and use the already-shown notification data directly.

## Step 3: Show current sync status

Always use `--short` by default to avoid collapsed output in the Claude Code terminal:

```bash
claude-mirror status --short --config <config-path>
```

Report the summary line. If the user asks for the full file list, run without `--short`:

```bash
claude-mirror status --config <config-path>
```

## Step 4: Handle remote changes — analyze and merge

When the status shows remote-ahead or conflict files (either discovered via inbox notifications or via status), do **not** pull blindly. Instead, perform a full analysis and merge:

### 4a. Read current local versions

For each affected file, read its current local content. This captures any changes made during the current Claude session that are not yet pushed.

### 4b. Download remote versions to a temp directory

```bash
claude-mirror pull <file1> <file2> ... --output ~/.local/tmp/claude-mirror/preview --config <config-path>
```

This downloads only the remote versions to `~/.local/tmp/claude-mirror/preview/` without touching the local project files.

### 4c. Read and diff

Read each file from `~/.local/tmp/claude-mirror/preview/<file>` and compare it against the local version you captured in 4a.

Produce a clear diff summary for the user:
- What changed remotely (additions, deletions, modifications)
- What was changed locally in the current session (if anything)

### 4d. Intelligent merge

Produce a merged version that incorporates **both**:
- All remote changes (from the downloaded preview)
- All local session changes (identified in 4a)

Apply the merge directly to the local project file. If the changes touch the same region and cannot be auto-merged cleanly, show the conflict to the user and ask how to resolve it before writing.

### 4e. Confirm and push

After writing the merged result to the local file(s), report a summary of what was merged and offer to push immediately.

**Always use `--force-local` when pushing after a skill-side merge.** This bypasses the interactive conflict resolver (which cannot run in the skill's non-interactive shell) and treats the locally written merged content as authoritative:

```bash
claude-mirror push <file1> <file2> ... --force-local --config <config-path>
```

## Step 5: Ask what the user wants to do (no pending remote changes)

If status shows no remote changes:

- Everything in sync → report it and offer to push any local/session changes
- Local-ahead files → offer to push
- Deleted-local files → offer to delete from remote storage
- Conflicts → offer to sync (prompts for resolution)

## Available commands

Always include `--config <config-path>` on every command:

```bash
claude-mirror auth      --config <config-path>
claude-mirror status --short --config <config-path>
claude-mirror status         --config <config-path>
claude-mirror sync      --config <config-path>
claude-mirror push                       --config <config-path>
claude-mirror push <file> [<file> ...]   --config <config-path>
claude-mirror push <file> [<file> ...]   --force-local --config <config-path>   # after skill-side merge
claude-mirror pull      --config <config-path>
claude-mirror pull <file> [<file> ...]  --output ~/.local/tmp/claude-mirror/preview  --config <config-path>
claude-mirror delete <file> [<file> ...]              --config <config-path>   # delete from remote only
claude-mirror delete <file> [<file> ...] --local      --config <config-path>   # delete from remote and local
claude-mirror snapshots --config <config-path>
claude-mirror history  <path>                            --config <config-path>   # find every snapshot containing PATH, version-grouped
claude-mirror inspect  <timestamp>                       --config <config-path>   # list paths in a snapshot
claude-mirror inspect  <timestamp> --paths 'memory/**'   --config <config-path>   # filter by glob
claude-mirror inspect  <timestamp> --backend NAME        --config <config-path>   # force inspect from a specific backend
claude-mirror restore  <timestamp> --output ~/.local/tmp/claude-mirror/review  --config <config-path>   # whole snapshot, safe location
claude-mirror restore  <timestamp>                       --config <config-path>   # whole snapshot, overwrite project (prompts); auto-fallback across mirrors
claude-mirror restore  <timestamp> <path> [<path> ...]   --config <config-path>   # single-file or path-glob restore
claude-mirror restore  <timestamp> [<path> ...] --backend NAME --config <config-path>   # force restore from a specific backend (skip primary-first fallback)
claude-mirror retry                                      --config <config-path>   # re-attempt failed mirror pushes
claude-mirror retry    --backend NAME [--dry-run]        --config <config-path>   # retry one backend only / preview without sending
claude-mirror seed-mirror --backend NAME                 --config <config-path>   # one-shot: populate a freshly-added mirror with files already on primary
claude-mirror status   --pending                         --config <config-path>   # show files with non-ok mirror state
claude-mirror status   --by-backend                      --config <config-path>   # full per-file table with one column per backend (Tier 2)
claude-mirror forget   <timestamp> [<timestamp> ...]     --config <config-path>   # delete specific snapshots
claude-mirror forget   --keep-last 50                    --config <config-path>   # retention pruning (single-selector form)
claude-mirror prune                                      --config <config-path>   # apply YAML's keep_last/keep_daily/keep_monthly/keep_yearly policy (dry-run by default)
claude-mirror prune    --delete --yes                    --config <config-path>   # apply retention policy non-interactively (cron / CI)
claude-mirror diff     <path>                            --config <config-path>   # colorized line-diff of local vs remote for one file
claude-mirror gc                                         --config <config-path>   # reclaim orphan blobs on PRIMARY (default)
claude-mirror gc       --backend NAME                    --config <config-path>   # Tier 2: reclaim orphan blobs on a specific mirror
claude-mirror migrate-snapshots --to {blobs|full}        --config <config-path>   # convert snapshots between formats (admin-only; rarely skill-triggered)
claude-mirror log       --config <config-path>
claude-mirror inbox     --config <config-path>
claude-mirror redact   <project-path>                                        # pre-push secret scan (dry-run); --apply to scrub interactively, --apply --yes for non-interactive
claude-mirror check-update                                                   # check GitHub for a newer version (no --config needed)
claude-mirror update                                                         # dry-run: report what update would do
claude-mirror update --apply                                                 # actually upgrade (git pull + pipx install -e . --force, with confirmation)
```

If a user notice mentions "🆕 claude-mirror X.Y.Z is available", do NOT silently invoke `update --apply` — that runs `git pull` and rebuilds the pipx venv, which has user-visible side effects (and may fail on uncommitted local changes). Surface the version diff and offer `claude-mirror update --apply` (or the manual command); let the user run it. If they confirm, you may run `claude-mirror update --apply` (the command itself prompts for confirmation by default; the user is in the loop).

**Snapshot recovery workflow:** when a user asks to restore a previous version of a specific file, prefer this sequence — single-file restore is far cheaper than fetching the whole tree:

```bash
claude-mirror history <path>                               # find every snapshot containing the file, version-grouped
claude-mirror inspect <ts> --paths '<path-pattern>'        # (optional) confirm the file is there at the version wanted
claude-mirror restore <ts> <path> --output <safe-location> # recover only what's needed
```

`claude-mirror history` is the most direct way to find "the version of X from before I broke it" — it walks every snapshot's manifest and labels distinct versions (v1, v2, ...) by SHA-256, so version transitions are obvious without scanning manifests by hand.

`claude-mirror restore` auto-falls-back across mirrors: if the snapshot is missing on the primary backend, it walks each configured mirror in order until it finds one. The skill does not need to know which backend holds the snapshot — just call `restore` without `--backend`. Use `--backend NAME` only when the user explicitly requests a specific backend.

For conflict resolution, explain the options and let the user decide before writing.

## Multi-backend awareness

A project may be mirrored to multiple backends. Detect this by checking the project's YAML for a non-empty `mirror_config_paths` field. If present, the skill is operating on a multi-backend setup. Never run `init` or modify `mirror_config_paths` from the skill — mirror setup is user-driven.

Push behavior is unchanged: `claude-mirror push` (with or without `--force-local`) fans out to all mirrors internally. Run push **once** — never iterate per backend.

A push may begin with a line like `Retrying N file(s) pending on mirror(s) from previous run(s)`. This is normal — it means a prior push hit a transient mirror failure and the queue is catching up. Do not interpret it as an error.

If the push reports per-backend warnings (e.g. `⚠ memory/notes.md (mirror dropbox: transient — will retry next time)`), the file IS pushed to primary and the mirror will catch up automatically on the next push. Treat the push as successful and proceed.

For "ACTION REQUIRED" failures (auth, quota, permission), surface the failure to the user with the recommended fix and stop — do not attempt auto-recovery:
- auth → `claude-mirror auth --config <mirror-config-path>` (the backend is selected by which config file you point at)
- quota → `claude-mirror forget --keep-last N` then `claude-mirror gc` on the affected backend
- permission → user must fix backend-side ACLs

To inspect mirror health on demand: `claude-mirror status --pending --config <config-path>` lists files with non-ok mirror state. To force a retry pass: `claude-mirror retry --config <config-path>` (optionally `--backend NAME` to scope, `--dry-run` to preview).

Slack messages (when configured) include a per-backend status block and may carry an "ACTION REQUIRED" alert. The skill does not post to Slack — just be aware the user may already see these.

## Watcher (background notifications)

Notifications accumulate in `.claude_mirror_inbox.jsonl` inside the project directory while the watcher runs. To check if the watcher is running for the current project:

```bash
pgrep -af "claude-mirror watch"
```

To start it for the current project:

```bash
claude-mirror watch --config <config-path>
```

## Shell tab-completion

`claude-mirror-install` auto-installs shell tab-completion for the user's detected shell (zsh, bash, or fish). Pressing `<TAB>` after `claude-mirror` shows all commands; after a specific command, it shows the relevant flags; for `click.Choice` flags such as `--backend` it shows the valid choices (`googledrive`, `dropbox`, `onedrive`, `webdav`, `sftp`, `ftp`, `s3`, `smb`).

If the user reports that tab-completion is not working after running `claude-mirror-install`, the most common cause is that the current shell session was started before the rc file was modified by the installer. Two fixes:

```bash
# (zsh) re-source the rc file in the current shell
source ~/.zshrc

# OR open a new terminal — both work
```

To reinstall or inspect the completion script directly:

```bash
claude-mirror completion zsh         # emit zsh completion script to stdout
claude-mirror completion bash        # emit bash completion script to stdout
claude-mirror completion fish        # emit fish completion script to stdout
claude-mirror completion powershell  # emit PowerShell completion script to stdout
```

For users running `claude-mirror` inside Claude Code's own shell snapshot (rather than a real iTerm or Terminal session), tab-completion will not work; the snapshot does not re-source rc files dynamically. Direct the user to test tab-completion in their normal terminal.

## Error recovery

### Authentication error — browser-OAuth backends (Google Drive / Dropbox / OneDrive)

If a command on a browser-OAuth backend fails with `RefreshError` or `Not authenticated`, run:

```bash
claude-mirror auth --config <config-path>
```

This opens a browser tab on the user's machine for a fresh OAuth login. Once auth completes, retry the original command.

If the user is hitting daily auth expiries on Google Drive, the cause is almost always organisational (Workspace Cloud Session Control reauth interval, GCP project not in the user's Workspace org, or OAuth consent screen still in "Testing" mode). Run `claude-mirror auth --check` to diagnose without re-auth — it inspects the saved token, attempts a refresh, and classifies the failure as either `invalid_grant` (re-auth required) or transient (retry). The README "Authentication expires every day or two (Google Drive)" section has the full checklist; refer the user there rather than guessing.

### Authentication error — inline-credential backends (WebDAV / SFTP / FTP / S3 / SMB)

These backends do not have a browser-OAuth flow. An auth failure means the inline credentials in the YAML are wrong (or the host's policy changed). `claude-mirror auth` is a no-op on these backends — fix the YAML field and re-run the original command. Use `claude-mirror doctor --backend NAME --config <config-path>` for diagnosis: it surfaces the specific check that's failing (host fingerprint, server unreachable, bad access key, share permission denied, …) and its fix-hint.

## Project memory rules

These rules apply whenever the active project has a `memory/` directory or MEMORY.md files managed by Claude:

**MIRROR RULE** — After every file change in memory, immediately copy the changed file(s) to the project working directory. Both locations must always be in sync.

**PRE-SYNC REVIEW RULE** — When one or more project markdown files are updated in the working directory, before any claude-mirror action:
1. Re-read all changed files
2. Diff against current memory
3. Report a clear summary of what changed
4. Ask for explicit confirmation before writing to memory and pushing to remote

**POST-PULL EVALUATION RULE** — When claude-mirror pulls remote changes (inbox notification or status showing remote-ahead files), immediately:
1. Re-read all changed files
2. Diff against current memory
3. Produce a structured analysis of what changed and what should be updated in memory (new facts, stale entries, discrepancies across files)
4. Ask for confirmation before writing to memory or pushing to remote

## Backend recipes

### SFTP (self-hosted SSH server)

YAML config (excerpt):

```yaml
backend: sftp
sftp_host: storage.example.com
sftp_port: 22
sftp_username: alice
sftp_key_file: ~/.ssh/id_ed25519
sftp_folder: /srv/claude-mirror/myproject
poll_interval: 30
```

Bring it up:

```bash
claude-mirror init --wizard --backend sftp
claude-mirror auth   --config <config-path>
claude-mirror push   --config <config-path>
```

SFTP has no native push notifications — claude-mirror falls back to polling (see `poll_interval`).

### FTP / FTPS (legacy shared hosting — cPanel / DirectAdmin / NAS)

YAML config (excerpt):

```yaml
backend: ftp
ftp_host: ftp.example.com
ftp_port: 21
ftp_username: alice
ftp_password: <stored at chmod 0600>
ftp_tls_mode: explicit   # plain | explicit | implicit
ftp_folder: claude-mirror/myproject
ftp_passive: true
poll_interval: 30
```

Bring it up:

```bash
claude-mirror init --wizard --backend ftp
claude-mirror push --config <config-path>
```

Plain FTP transmits credentials in cleartext — prefer `ftp_tls_mode: explicit` (FTPS) or use SFTP. No native push notifications; polling only.

### S3-compatible (AWS S3, Cloudflare R2, Backblaze B2, Wasabi, MinIO, …)

YAML config (excerpt):

```yaml
backend: s3
s3_endpoint_url: ""              # blank for AWS; provider host otherwise
s3_bucket: my-claude-mirror-bucket
s3_region: us-east-1
s3_access_key_id: AKIA...        # or blank to use boto3 default credential chain
s3_secret_access_key: <secret>   # or blank
s3_prefix: myproject
s3_use_path_style: false         # true for MinIO / some S3-compat services
poll_interval: 30
```

Bring it up:

```bash
claude-mirror init --wizard --backend s3
claude-mirror push --config <config-path>
```

Leaving access key + secret blank tells boto3 to use its default credential chain (env vars, `~/.aws/credentials`, IAM role). For non-AWS providers set `s3_endpoint_url` (e.g. `https://<account>.r2.cloudflarestorage.com` for R2). No native push notifications; polling only.

### SMB / CIFS (Windows file shares, Synology / QNAP / TrueNAS NAS, macOS Sharing, generic Samba)

YAML config (excerpt):

```yaml
backend: smb
smb_server: nas.local            # hostname or IP
smb_port: 445                    # 139 for legacy NetBIOS-over-TCP
smb_share: claude-mirror
smb_username: alice
smb_password: <stored at chmod 0600>
smb_domain: ""                   # AD / NTLM domain; blank for workgroup
smb_folder: claude-mirror/myproject
smb_encryption: true             # SMB3 per-message encryption
poll_interval: 30
```

Bring it up:

```bash
claude-mirror init --wizard --backend smb
claude-mirror push --config <config-path>
```

SMB2/3 only — SMBv1 is rejected as a security gate. No native push notifications; polling only.

## Pre-push safety: scrub secrets before pushing

If the user mentions secrets, API keys, OAuth tokens, or auth material being involved in recent edits — or if the conversation has touched credentials at all — run `claude-mirror redact <project-path>` (no `--apply`) as a sanity check before any push. This is a dry-run scan that flags `AKIA…` AWS keys, `ghp_`-style GitHub tokens, OpenAI / Anthropic / Google API keys, Slack webhooks and bot tokens, JWTs, password assignments, and a few more. The dry-run never writes to disk; it just prints the findings table.

If findings appear, surface them to the user and offer to scrub via `claude-mirror redact <project-path> --apply` (interactive) or `claude-mirror redact <project-path> --apply --yes` (auto-replace every finding with a `<REDACTED:KIND>` marker). Never run `--apply` automatically without user confirmation — `--apply` rewrites the user's files in place.

## Important rules

- Always run `find-config` as a standalone Bash call first — capture its output as the config path, never use shell variable substitution `$()` inline
- Always pass `--config <config-path>` on every command — never omit it
- Never run `claude-mirror restore` to the project path without explicit user confirmation
- Never run destructive operations (restore, delete, redact --apply) without confirming with the user first
- The `delete` command requires explicit file arguments — it will not delete all files
- `claude-mirror redact` is dry-run by default; `--apply` rewrites files in place and requires user confirmation
- If a command fails, show the full error and suggest a fix
- Notifications are stored in `{project_path}/.claude_mirror_inbox.jsonl` — they are project-scoped and will not mix with other projects
