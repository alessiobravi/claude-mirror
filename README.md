# claude-mirror

[![Tests](https://github.com/alessiobravi/claude-mirror/actions/workflows/test.yml/badge.svg)](https://github.com/alessiobravi/claude-mirror/actions/workflows/test.yml)
[![PyPI](https://img.shields.io/pypi/v/claude-mirror)](https://pypi.org/project/claude-mirror/)
[![Python](https://img.shields.io/pypi/pyversions/claude-mirror)](https://pypi.org/project/claude-mirror/)
[![License](https://img.shields.io/pypi/l/claude-mirror)](https://github.com/alessiobravi/claude-mirror/blob/main/LICENSE)
[![Discussions](https://img.shields.io/github/discussions/alessiobravi/claude-mirror)](https://github.com/alessiobravi/claude-mirror/discussions)

**Mirror your project files across machines and cloud backends — with multi-cloud redundancy, time-travel disaster recovery, and real-time collaboration signals.**

Built originally for Claude Code projects (where most context lives in markdown), but the file-pattern glob is configurable to sync any file type. The default `file_patterns: ["**/*.md"]` keeps just Claude context files in sync; set it to `["**/*"]` to mirror the entire project tree, or any other glob (e.g. `["**/*.py", "**/*.md"]`) to scope what gets synced.

### Why use it

- **Multi-cloud redundancy.** Push to multiple backends in parallel (e.g. Google Drive + Dropbox + OneDrive). Any single provider's outage, account suspension, or quota cap never costs you data. Per-mirror retry queues mean a transient failure on one backend never blocks the rest.
- **Time-travel disaster recovery.** Every push and sync auto-creates a snapshot. `claude-mirror history PATH` shows every version of any file across snapshots; `claude-mirror restore` rolls a single file or the whole project back to any past timestamp. Two storage formats: content-addressed blobs (identical content across snapshots stored once) or full per-snapshot copies.
- **Near-real-time collaboration.** Pub/Sub (Drive), long-poll (Dropbox), or polling (OneDrive / WebDAV / SFTP) push remote changes to other machines within seconds. Optional per-project Slack webhooks pipe events to a team channel.
- **No-loss conflict resolution.** When both sides change a file, interactive choice: keep local, keep remote, open `$EDITOR` for manual merge, or skip. No silent overwrites.

**Supported backends:** Google Drive, Dropbox, Microsoft OneDrive, any WebDAV server (Nextcloud, OwnCloud, Apache mod_dav, Synology/QNAP NAS, Box.com, etc.), and any SFTP/SSH-accessible server (VPS, NAS, shared hosting, self-hosted Linux). Each project picks its own primary backend independently — different projects on the same machine can use different backends.

**Quality gates:** Every commit and pull request runs **834 automated tests** on Linux and Windows on Python 3.11, 3.12, 3.13, and 3.14 in parallel via GitHub Actions, plus a separate `mypy --strict` static-type-checking job on every commit and PR — covering the 3-way diff sync core, both snapshot formats, path-traversal safety, conflict resolution, auth flows, all five backends (with HTTP-level / SSH-level mocking), the notifier inbox under concurrent writers, the Discord / Teams / Generic webhook notifiers, the watcher daemon's SIGHUP hot-reload, the `--json` output mode for `status / history / inbox / log / snapshots`, the `.claude_mirror_ignore` parser, the bandwidth-throttle token-bucket integration across every backend, the credentials-profile resolution + the merge-precedence rule (project YAML wins over profile defaults), the transfer-progress callback wiring across all 5 backends (live ETA + bytes/sec during push / pull / sync / seed-mirror), the global rate-limit `BackoffCoordinator` (HTTP 429 from any backend pauses every parallel upload on a single shared deadline rather than each retrying independently), the non-interactive `sync --no-prompt --strategy` flow for cron / unattended use, the dynamic `--backend` shell completion (zsh / bash / fish / powershell) via the hidden `_list-backends` subcommand, the multi-channel notification routing per project (Slack/Discord/Teams/Generic with event + path filters), the per-event webhook templating across all 4 backends (Slack/Discord/Teams/Generic with `str.format` placeholder vocabulary), the Drive BYO wizard's URL templating + input validation + post-auth smoke test, the Drive Pub/Sub auto-setup logic (topic + per-machine subscription + IAM grant via `--auto-pubsub-setup`), the deep `doctor --backend` checks for **all five backends** — googledrive (Drive API / Pub/Sub topic / subscription / IAM grant), dropbox (token shape / app-key / account smoke / scopes / folder access), onedrive (token cache / Azure GUID / scopes / Graph drive-item probe), webdav (PROPFIND / DAV class / ETag / oc:checksums), and sftp (host fingerprint / key perms / exec_command / auth) — and the `seed-mirror` auto-detect logic. CI must be green before any PR can merge. See [`CONTRIBUTING.md`](https://github.com/alessiobravi/claude-mirror/blob/main/CONTRIBUTING.md) for the test conventions and how to run them locally.)

---

## How it works

- Files matching configured patterns (default: `**/*.md`) are synced to a shared cloud folder
- A local manifest tracks file hashes to detect what changed since the last sync
- When you push, collaborators are notified in near-real-time:
  - **Google Drive** — Cloud Pub/Sub streaming (sub-second latency)
  - **Dropbox** — `files/list_folder/longpoll` (seconds latency)
  - **OneDrive / WebDAV / SFTP** — periodic polling (default 30s, configurable)
- Conflicts (both sides changed) are resolved interactively: keep local, keep remote, or open in `$EDITOR` — see [docs/conflict-resolution.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/conflict-resolution.md)
- A snapshot is saved after every push or sync, enabling point-in-time recovery — see [docs/admin.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md#snapshots-and-disaster-recovery)
- **Multi-backend mirroring (Tier 2)** — push to multiple backends simultaneously (e.g. Drive + SFTP), with per-backend retry, classified error handling, and snapshot mirroring — see [docs/admin.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md#multi-backend-mirroring-tier-2) and [docs/scenarios.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/scenarios.md#d-multi-backend-redundancy-tier-2)
- Optional **Slack** notifications on push/pull/sync/delete (per-project, opt-in, webhook-based)

---

## Supported storage backends

Each backend ships in the base install — `pipx install claude-mirror` enables all five. Per-backend setup walkthroughs live under [docs/backends/](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/):

| Backend | Latency | Setup | Reference |
|---|---|---|---|
| **Google Drive** | sub-second (Pub/Sub gRPC) | OAuth2 + GCP project (Drive API + Pub/Sub API) | [docs/backends/google-drive.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/google-drive.md) |
| **Dropbox** | seconds (long-poll) | OAuth2 PKCE + Dropbox app | [docs/backends/dropbox.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/dropbox.md) |
| **OneDrive** | up to `poll_interval` (default 30s) | Device-code OAuth2 + Azure AD app | [docs/backends/onedrive.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/onedrive.md) |
| **WebDAV** | up to `poll_interval` (default 30s) | URL + username + app password (Nextcloud, OwnCloud, NAS, Apache mod_dav, Box, Synology, QNAP, ...) | [docs/backends/webdav.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/webdav.md) |
| **SFTP** | up to `poll_interval` (default 30s) | SSH key (preferred) or password — any OpenSSH-accessible server | [docs/backends/sftp.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/sftp.md) |

---

## Prerequisites

- Python 3.11 or later

Plus, depending on the backend you choose:

| Backend | Requires |
|---|---|
| Google Drive | A Google account and a Google Cloud project (free tier is fine) |
| Dropbox | A Dropbox account and a Dropbox app (free, created at [dropbox.com/developers](https://www.dropbox.com/developers)) |
| OneDrive | A Microsoft account and an Azure AD app registration (free, created at [portal.azure.com](https://portal.azure.com)) |
| WebDAV | A WebDAV server URL + username + app password (e.g. Nextcloud / OwnCloud / NAS / Apache mod_dav) |
| SFTP | An SSH-accessible server (VPS / NAS / shared hosting / self-hosted Linux) — SSH key recommended, password fallback OK on LAN |

---

## Install

### Recommended: pipx from PyPI

[pipx](https://pipx.pypa.io) installs into an isolated environment and puts `claude-mirror` on your PATH permanently — no venv activation required:

```bash
brew install pipx   # macOS; see https://pipx.pypa.io for other platforms
pipx ensurepath     # adds ~/.local/bin to PATH if not already there

pipx install claude-mirror
```

All five backends ship in this single install — no per-backend extras needed.

Verify:

```bash
claude-mirror --version
```

### Alternative: pip in a venv

```bash
python3 -m venv ~/.venvs/claude-mirror
source ~/.venvs/claude-mirror/bin/activate
pip install claude-mirror
```

You must activate the venv in every new shell. The Claude Code skill (which runs in a non-interactive shell) won't be able to call `claude-mirror` from a venv — use pipx for that case.

### Developer install (editable)

```bash
git clone https://github.com/alessiobravi/claude-mirror.git
cd claude-mirror
pipx install -e .
```

### Install components (skill, hook, watcher, completion)

Once `claude-mirror` is on your PATH, run the installer to set up the Claude Code skill, notification hook, background watcher, and shell tab-completion in one step:

```bash
claude-mirror-install
```

You will be prompted to confirm each component before anything is written. To remove later:

```bash
claude-mirror-install --uninstall
```

After install, `claude-mirror <TAB>` lists all commands; `claude-mirror init --backend <TAB>` shows the five valid backends. Tab-completion is supported on zsh, bash, fish, and PowerShell — the installer auto-detects your shell from `$SHELL` (or defaults to PowerShell on Windows). To install PowerShell completion manually:

```powershell
claude-mirror completion powershell | Out-File -Encoding utf8 -Append $PROFILE.CurrentUserAllHosts
```

Verify desktop notifications with `claude-mirror test-notify`.

### Updating

```bash
pipx upgrade claude-mirror              # PyPI installs
claude-mirror update --apply            # auto-detects PyPI vs editable; uses pipx upgrade or git pull + pipx install -e . --force
```

---

## Documentation index

The trimmed README covers install, your first project, daily-usage cheatsheet, notifications, and troubleshooting. Everything else lives under [`docs/`](https://github.com/alessiobravi/claude-mirror/blob/main/docs/):

**Backends** (per-backend setup, config fields, troubleshooting):
- [docs/backends/google-drive.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/google-drive.md) — Google Cloud project, Drive API, Pub/Sub, OAuth2 setup
- [docs/backends/dropbox.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/dropbox.md) — Dropbox app registration, OAuth2 PKCE
- [docs/backends/onedrive.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/onedrive.md) — Azure AD app, device-code login
- [docs/backends/webdav.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/webdav.md) — Nextcloud / OwnCloud / NAS / Apache mod_dav
- [docs/backends/sftp.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/sftp.md) — SSH keys, host fingerprints, OpenSSH

**Operations & admin**:
- [docs/admin.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md) — snapshots, retention, `gc` / `prune` / `forget`, doctor, watcher service, multi-backend Tier 2 setup, auto-start
- [docs/cli-reference.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/cli-reference.md) — every command, every flag
- [docs/conflict-resolution.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/conflict-resolution.md) — interactive conflict prompts, `$EDITOR` merge, three-way diff
- [docs/faq.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/faq.md) — frequently asked questions across auth, sync, snapshots, notifications, performance, and migration; 30-second answers with links into the depth-docs
- [docs/profiles.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/profiles.md) — credentials profiles: factor `credentials_file` / `token_file` / app keys out of every project YAML

**Topology guides** (pick the one that matches your situation):
- [docs/scenarios.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/scenarios.md) — seven deployment topologies, end to end:
  - **A. Standalone** — local ↔ 1 backend
  - **B. Personal multi-machine** — local ⇄ 1 backend ⇄ local'
  - **C. Multi-user collaboration** — Alice ⇄ shared backend ⇄ Bob
  - **D. Multi-backend redundancy** — local → primary + N mirrors
  - **F. Selective sync** — custom `file_patterns` + exclusions
  - **G. Multi-user + multi-backend (production-realistic)** — shared primary + shared mirror, full Alice/Bob YAMLs and command-by-command transcript
  - **H. Multi-project enterprise** — many configs in `~/.config/claude_mirror/`

---

## Your first project

The two examples below cover the most common path (Google Drive — most users) and the simplest path (SFTP — zero cloud setup, works against any SSH-accessible server). Dropbox / OneDrive / WebDAV walk-throughs follow the same shape; see the per-backend docs above.

### Option 1 — Google Drive

```bash
cd /path/to/your/claude/project
claude-mirror init --wizard --backend googledrive
```

The wizard asks for the Drive folder ID, GCP project ID, Pub/Sub topic ID, and credentials file path. Press Enter to accept defaults. Sample run:

```
Storage backend [googledrive]:
Project directory [/Users/alice/work/myproject]:
Credentials file [~/.config/claude_mirror/credentials.json]: ~/.config/claude_mirror/work-credentials.json

Drive folder ID: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
GCP project ID: my-gcp-project
Pub/Sub topic ID [claude-mirror-myproject]:
Token file [~/.config/claude_mirror/work-token.json]:
File patterns [**/*.md]:

Save this configuration? [Y/n]:
```

Then authenticate (opens a browser window):

```bash
claude-mirror auth
```

Detailed setup of the GCP project, OAuth consent screen, Drive folder, and Pub/Sub topic — including how to invite collaborators — lives in [docs/backends/google-drive.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/google-drive.md).

### Option 2 — SFTP

```bash
cd /path/to/your/claude/project
claude-mirror init --wizard --backend sftp
```

The wizard asks for hostname, username, root path, and SSH key (or password). Sample run:

```
Storage backend [sftp]:
Project directory [/Users/alice/work/myproject]:

SFTP host: backup.example.com
SFTP username [alice]:
Root path on server: /home/alice/claude-mirror/myproject
SSH key file [~/.ssh/id_ed25519]:
Poll interval (seconds) [30]:
File patterns [**/*.md]:

Save this configuration? [Y/n]:
```

No interactive auth step — the wizard validates the SSH connection and host fingerprint immediately. Set up the remote root path and SSH key permissions ahead of time; full walkthrough at [docs/backends/sftp.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/sftp.md).

### After init: push your first batch

```bash
claude-mirror status     # see what's local-ahead vs in-sync
claude-mirror push       # upload the local-ahead files; creates a snapshot
```

> Each collaborator runs `claude-mirror auth` (or `init --wizard` for SFTP-key setups) on their own machine with their own credentials. Tokens are unique per machine, never shared.

### Multiple projects on the same machine

Repeat `init --wizard` once per project. Every project gets its own config file at `~/.config/claude_mirror/<project>.yaml`. Different projects on the same machine can use different backends — Drive for one, SFTP for another, WebDAV for a third. All commands auto-detect the right config from the current working directory.

### Multiple accounts and mixed backends

When several projects share the same account (one Google account → 5 projects, one Dropbox app → 3 projects), use **credentials profiles** to factor the duplicated credential fields out of the per-project YAMLs:

```bash
claude-mirror profile create work --backend googledrive    # one-time scaffold
cd ~/projects/research
claude-mirror --profile work init --wizard                  # inherits credentials, prompts only for project-specific fields
cd ~/projects/strategy
claude-mirror --profile work init --wizard                  # same profile, different folder/topic
```

The global `--profile NAME` flag (since v0.5.49) goes BEFORE the subcommand. After init, each project YAML carries `profile: work` at the top so subsequent `push`/`pull`/`sync` commands pick the profile up automatically. Project YAML values still win over profile defaults, so any one project can override a single field as a per-project escape hatch. See [docs/profiles.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/profiles.md) for the full walkthrough.

For team-shared / multi-backend / multi-user setups, see [docs/scenarios.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/scenarios.md).

---

## Daily usage cheatsheet

All commands work identically regardless of backend. Run from inside the project directory; the right config is auto-detected.

```bash
claude-mirror status                    # 3-way diff: local / remote / manifest
claude-mirror status --short            # one-line summary, no table
claude-mirror status --by-backend       # per-file × per-backend live state (Tier 2 setups)
claude-mirror push                      # upload local-ahead files; snapshot
claude-mirror push file1.md file2.md    # push specific files only
claude-mirror pull                      # download remote-ahead files
claude-mirror pull --output DIR         # preview to DIR without touching local files
claude-mirror diff path/to/file.md      # colourised unified diff (remote → local)
claude-mirror sync                      # full bidirectional with conflict prompts
claude-mirror sync --no-prompt --strategy keep-local   # cron-friendly: auto-resolve all conflicts
claude-mirror watch                     # foreground watcher; system notification on remote pushes
claude-mirror watch --once --quiet      # single polling cycle (cron-friendly: */5 * * * * ...)
claude-mirror watch-all                 # watch every config in ~/.config/claude_mirror/
claude-mirror log                       # who pushed what, when, across machines
claude-mirror inbox                     # show + clear pending notifications
claude-mirror status --json | jq '.result.summary'   # script-friendly output
```

Per-project gitignore-style exclusions: drop a `.claude_mirror_ignore` at the project root for `**`/`!negation`/`/anchored`/`dir/` rules that complement YAML `exclude_patterns`. Full syntax in [docs/admin.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md#claude_mirror_ignore--project-tree-exclusions).

Snapshot, restore, and admin commands (`history`, `snapshots`, `restore`, `prune`, `gc`, `forget`, `doctor`, `seed-mirror`, `migrate-state`) live in [docs/cli-reference.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/cli-reference.md), with operational guidance in [docs/admin.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md).

Conflict resolution flow (interactive `keep local / keep remote / merge / skip` prompt) is documented in [docs/conflict-resolution.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/conflict-resolution.md).

---

## Claude Code skill

claude-mirror ships a skill for [Claude Code](https://claude.ai/claude-code) that lets you run sync operations directly from your AI conversation, and surfaces remote notifications inline without leaving the editor.

### How it works

```
Collaborator runs claude-mirror push on Machine B
  → Notification fires on the backend's channel (Pub/Sub, longpoll, or polling)
  → claude-mirror watch (running in background) receives it
  → System notification sent
  → Event written to {project}/.claude_mirror_inbox.jsonl

You type /claude-mirror in Claude Code
  → Skill auto-detects your active project via find-config
  → Reads and clears the inbox — pending notifications shown in conversation
  → Runs status — full sync state shown in conversation
  → You can push / pull / sync / restore by talking to Claude
```

Notifications are **project-scoped** — each project has its own `.claude_mirror_inbox.jsonl` inside the project directory. Switching projects is as simple as `cd`-ing to a different directory.

### Setup

`claude-mirror-install` (run once, see [Install](#install)) handles the skill, the `PreToolUse` hook in `~/.claude/settings.json`, and the background watcher service. No further configuration needed — the skill auto-detects the active project.

The hook makes notifications appear automatically inside the Claude Code conversation: it runs silently before every tool call, calls `claude-mirror inbox` to print + clear any pending notifications, and stays invisible when nothing is pending. Combined with the watcher:

| You are... | How you get notified |
|---|---|
| Actively using Claude Code | Hook fires on next tool call — notification appears inline |
| Idle / away from Claude Code | Desktop notification from the watcher |

### Use it

In any Claude Code conversation, type:

```
/claude-mirror
```

Claude will detect the config for your current working directory, report any pending notifications, show the full sync status, and offer a smart-merge for any remote-ahead or conflict files (downloads remote versions to a preview dir, diffs against local + in-session edits, produces an intelligent merged result, asks before writing).

You can also ask in natural language: "push my changes", "pull the latest", "sync everything", "what's different in this file?", "show me the snapshots", "restore to 10:30 this morning", "clean up old snapshots", "what changed recently".

For background and persistent-service setup of `claude-mirror watch-all`, see [docs/admin.md#auto-start-the-watcher](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md#auto-start-the-watcher).

### Recommended project memory directives

For best results, add the following directives to every project's `MEMORY.md` (or `CLAUDE.md`). They instruct Claude to keep memory and working files in sync and to review changes carefully before pushing.

```markdown
- **Project home:** the current working directory (wherever this project is opened from)
  — all MD files mirrored here (copy of memory)

- **MIRROR RULE:** After every file change in memory, immediately copy the changed
  file(s) to the project working directory. Both locations must always be in sync.

- **PRE-SYNC REVIEW RULE:** When one or more project markdown files are updated in
  the working directory, before any claude-mirror action:
  (1) re-read all changed files, (2) diff against current memory,
  (3) report a clear summary of what changed,
  (4) ask for explicit confirmation before writing to memory and pushing.

- **POST-PULL EVALUATION RULE:** When claude-mirror pulls remote changes (inbox
  notification or status showing drive-ahead files), immediately re-read all changed
  files, diff against current memory, produce a structured analysis of what changed
  and what should be updated in memory (new facts, stale entries, discrepancies
  across files), then ask for confirmation before writing to memory or pushing.
```

These rules are also built into the skill itself and apply automatically during every `/claude-mirror` invocation.

---

## Slack notifications

Slack notifications are **per-project** and **opt-in**. They fire on claude-mirror sync events (push, pull, sync, delete) — independent of any git/GitHub commit notifications you may have set up.

> **Other chat platforms?** Discord, Microsoft Teams, and a generic JSON-envelope webhook (for n8n / Make / Zapier / custom endpoints) are also supported. Same opt-in / best-effort contract as Slack. See [docs/admin.md — Notifications](docs/admin.md#notifications) for the setup walkthroughs and the full config-field reference.

### Step 1 — Create the Slack incoming webhook

You need an **Incoming Webhook URL** that points at a specific Slack channel. Each webhook is bound to one channel at creation time; if you want to post to two channels, generate two webhooks.

1. **Open the Slack API page** at [api.slack.com/apps](https://api.slack.com/apps) → click **Create New App** → **From scratch**. Name it `claude-mirror` (or anything; it appears as the message author in Slack), pick the workspace, click **Create App**.
2. **Enable Incoming Webhooks** — left sidebar → **Incoming Webhooks** → toggle **Activate Incoming Webhooks** to **On**.
3. **Add a webhook to a specific channel** — scroll to the bottom → **Add New Webhook to Workspace** → pick the channel → **Allow**. For a private channel: invite the app first (`/invite @claude-mirror`).
4. **Copy the webhook URL.** It looks like `https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx`. Treat it as a secret.

### Step 2 — Enable Slack in claude-mirror

The `init --wizard` flow offers to enable Slack and asks for the webhook. Or pass the flags non-interactively:

```bash
claude-mirror init \
  --project /path/to/project \
  --backend googledrive \
  ... \
  --slack \
  --slack-webhook-url 'https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx' \
  --slack-channel '#claude-mirror'
```

> **Quote the webhook URL** in single quotes — the `&`/`/` characters in the URL must not be interpreted by your shell.

To enable Slack on an already-initialized project, edit the project YAML directly:

```yaml
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx
slack_channel: "#claude-mirror"      # optional override
```

### Step 3 — Verify

Trigger any sync event from the project directory (`echo test >> CLAUDE.md && claude-mirror push CLAUDE.md`). You should see a Rich-formatted message in the channel within seconds:

```
🔼 alice@workstation pushed 1 file in myproject

Files changed:
• CLAUDE.md

📸 Snapshot: 2026-05-05T10-15-22Z (blobs)  ·  📚 1245 files in project
```

The file list is capped at 10 entries; longer pushes show `… and N more` after the cap. If a push or sync touched files but no snapshot was created (rare), the context line shows `⚠️ No snapshot was created for this event` so the recovery-point gap is visible.

Slack failures (network error, 4xx, 5xx, malformed webhook) are logged and silently swallowed — they will **never** block or fail a sync.

### Config fields

| Field | Type | Purpose |
|---|---|---|
| `slack_enabled` | bool | Master switch. `false` (default) disables all Slack posts. |
| `slack_webhook_url` | str | Incoming-webhook URL from Slack's Apps directory. |
| `slack_channel` | str (optional) | Override the channel the webhook posts to. Honoured only if your workspace allows webhook channel-overrides (per-workspace setting, off by default since 2018). |

Two channels for the same project? Generate two webhooks under the same app and configure two project YAMLs that point at the same `project_path`. Two projects on the same machine post to different channels naturally — each YAML carries its own `slack_webhook_url`.

---

## Desktop notifications

Run the built-in test command to verify and see platform-specific setup instructions:

```bash
claude-mirror test-notify
```

### macOS

Notifications use `osascript display notification`. macOS requires the calling application to have notification permission granted explicitly.

**Steps:** run `claude-mirror test-notify` from Terminal (or iTerm2) → open **System Settings → Notifications** → find **Terminal** → enable **Allow Notifications** → set alert style to **Alerts** or **Banners**.

**Running as a launchd service:** when the watcher runs as a launchd agent it has no app bundle, so the system cannot create a notification permission entry for it. Workaround: run `claude-mirror watch` once from a regular Terminal window, grant permission to Terminal, then switch back to the launchd service.

### Linux

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

---

## Update notifications

claude-mirror checks once per 24h whether a newer version exists on this project's GitHub mirror and tells you about it. The check is best-effort, offline-tolerant, and never blocks a command — it runs in a background daemon thread and only ever prints a single inline notice when the cached "latest version" is newer than the locally-installed one.

On any command launch when an update is available:

```
🆕 claude-mirror 0.4.1 is available (you have 0.4.0).
Update: pipx install -e . --force from your repo dir, or set CLAUDE_MIRROR_NO_UPDATE_CHECK=1 to silence.
```

Inside the long-running watcher daemon, the same event also fires a desktop notification — but only ONCE per new version (tracked in cache).

### Manual check

```bash
claude-mirror check-update     # bypasses the 24h cache
claude-mirror update           # dry-run: shows current → latest and the command that would run
claude-mirror update --apply   # runs git pull + pipx install -e . --force (editable) or pipx upgrade (PyPI)
claude-mirror update --apply --yes   # skip the confirmation prompt
```

### Cache and opt-out

| Item | Default | How to change |
|---|---|---|
| Cache location | `~/.config/claude_mirror/.update_check.json` | Hardcoded; safe to delete |
| TTL | 24h | `claude-mirror check-update` bypasses |
| Opt out (per-shell) | (off) | `export CLAUDE_MIRROR_NO_UPDATE_CHECK=1` |
| Opt out (permanent) | (off) | Add the export to your `.zshrc` / `.bashrc` |

The check looks at PyPI first (`https://pypi.org/pypi/claude-mirror/json`, the most authoritative source for installability — a release isn't real until the wheel is on PyPI), falls back to the GitHub Contents API (`https://api.github.com/repos/alessiobravi/claude-mirror/contents/pyproject.toml`) when PyPI is unreachable, and finally to the raw CDN (`https://raw.githubusercontent.com/alessiobravi/claude-mirror/main/pyproject.toml`) when both are unavailable. Each fallback only runs if the prior fails. Every request includes a `User-Agent: claude-mirror/<version> update-check` header — no telemetry data is sent.

---

## Troubleshooting

The most common gotchas are listed here. For backend-specific errors (Drive auth, Dropbox/OneDrive token cache, WebDAV `MKCOL`, SFTP host fingerprint), see the per-backend docs under [docs/backends/](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/).

### `RefreshError: Reauthentication is needed` (Google Drive)

The OAuth refresh token has expired or been revoked. The error mentions `gcloud` — that's misleading; claude-mirror does not use gcloud credentials.

**Fix:** `claude-mirror auth --config ~/.config/claude_mirror/<project>.yaml` — opens a browser for fresh OAuth login. The stale token is replaced; the rest of the config is preserved.

For "auth expires every day or two" symptoms (caused by the OAuth consent screen sitting in `Testing` mode, or Workspace Cloud Session Control settings), see the in-depth diagnosis in [docs/backends/google-drive.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/google-drive.md).

### `Not authenticated. Run claude-mirror auth first.`

No token file exists — `auth` was never run on this machine, or the token file was deleted. Fix: run `claude-mirror auth`.

### Dropbox: "Authentication code expired" or "invalid_grant"

The one-shot authorization code printed during `claude-mirror auth` has a short lifetime. Re-run `claude-mirror auth`, complete the flow promptly, and paste the code on first try.

### OneDrive: `AADSTS50058` or "no cached accounts found"

The MSAL token cache is missing, corrupted, or its refresh token has been revoked. Re-run `claude-mirror auth` — the device-code flow rebuilds the cache.

### WebDAV: `401 Unauthorized` or `405/409 from MKCOL`

`401` — wrong credentials, or a real account password where an app password is required (Nextcloud / OwnCloud with 2FA). Generate an app password and re-run `claude-mirror init` (or edit `webdav_username` + re-run `claude-mirror auth`).

`405/409 from MKCOL` — the server refused to create the project folder. Create it manually via the server's web UI or another WebDAV client, then re-run `claude-mirror push`.

### SFTP: host fingerprint or permission errors

See [docs/backends/sftp.md](https://github.com/alessiobravi/claude-mirror/blob/main/docs/backends/sftp.md) for the full troubleshooting flow (`~/.ssh/known_hosts` updates, `internal-sftp`-jailed accounts, server-side `sha256sum` fallback).

### `claude-mirror doctor`

For end-to-end self-test (config + creds + connectivity + backends + manifest sanity), run:

```bash
claude-mirror doctor
```

See [docs/admin.md#doctor](https://github.com/alessiobravi/claude-mirror/blob/main/docs/admin.md#doctor) for the full check matrix and output interpretation.

---

## File locations

All token files are written with `chmod 0600` (owner read/write only).

| File | Purpose |
|---|---|
| `~/.config/claude_mirror/<account>-credentials.json` | OAuth2 client credentials for one Google account (share with team) |
| `~/.config/claude_mirror/<account>-token.json` | Personal access token for Google Drive (do not share) |
| `~/.config/claude_mirror/dropbox-<project>-token.json` | Personal Dropbox refresh token (do not share) |
| `~/.config/claude_mirror/onedrive-<project>-token.json` | MSAL token cache for OneDrive (do not share) |
| `~/.config/claude_mirror/webdav-<project>-token.json` | WebDAV credentials — URL, username, password in plaintext at `0600`. Prefer an app password. |
| `~/.config/claude_mirror/sftp-<project>-token.json` | SFTP host-fingerprint cache (key auth uses your `~/.ssh/`; password fallback is plaintext at `0600`) |
| `~/.config/claude_mirror/<project>.yaml` | Per-project config (auto-named from project directory) |
| `{project}/.claude_mirror_manifest.json` | Local sync state (do not edit manually) |
| `{project}/.claude_mirror_hash_cache.json` | Per-project hash cache to skip re-hashing unchanged files. Safe to delete; rebuilt on next run. Add to `.gitignore`. |
| `{project}/.claude_mirror_inbox.jsonl` | Pending notifications for Claude Code skill (auto-cleared on read) |
| `~/.claude/skills/claude-mirror/SKILL.md` | Claude Code skill definition |
| `~/.claude/settings.json` | Claude Code settings (PreToolUse hook lives here) |
| `~/Library/LaunchAgents/com.claude-mirror.watch.plist` | macOS: launchd agent for background watcher |
| `~/.config/systemd/user/claude-mirror-watch.service` | Linux: systemd user service for background watcher |

---

## Migrating from older versions

claude-mirror reads older configs and manifests transparently — there is nothing you need to do by hand.

- **Configs without a `backend` field** (any project YAML created before multi-backend support) are loaded as `backend: googledrive`. The field is filled in automatically the next time the config is written.
- **Manifests with legacy `drive_file_id` keys** are still understood. `Manifest.load` accepts both the legacy `drive_file_id` and the current `remote_file_id`, so existing projects keep their full sync state across the upgrade.
- **Pre-v0.5.1 on-disk paths** (`claude_sync` everywhere) — run `claude-mirror migrate-state --apply` once to rename local files and rewrite token paths. WebDAV/OneDrive listing predicates accept both old and new prefixes during the transition.

---

## Disclaimer — use at your own risk

claude-mirror is provided **as is, without warranty of any kind**, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose, and noninfringement. By downloading, installing, or running this software you accept full and exclusive responsibility for any consequences of its use, including without limitation:

- **Data loss, corruption, or accidental deletion** of your local files, your remote storage (Google Drive, Dropbox, OneDrive, WebDAV server, SFTP server), or any backups thereof — whether caused by a bug, a misconfiguration, an interrupted sync, a network failure, a backend API change, an authentication problem, or otherwise.
- **Unintended overwrites** during conflict resolution, `pull`, `push`, or `restore` operations.
- **Disclosure of file contents** to anyone who has access to the configured remote folder, the Pub/Sub topic, the Slack channel, or the local machine. claude-mirror syncs whatever matches the configured `file_patterns` — review your patterns and `exclude_patterns` carefully before pushing.
- **Charges or quota consumption** on Google Cloud, Microsoft Azure, Dropbox, or any other third-party service used as a backend.
- **Compliance with the terms of service** of every backend, notification channel, and third-party API you point claude-mirror at.

You — the operator — are solely responsible for evaluating the suitability of claude-mirror for your use case, for keeping independent backups of any data you care about, and for testing the tool against non-critical data before relying on it in production. The authors and contributors are **not liable** for any direct, indirect, incidental, special, exemplary, or consequential damages arising from the use of this software, even if advised of the possibility of such damage.

If you do not accept these terms, do not download or run claude-mirror.

---

## License

claude-mirror is free software released under the **GNU General Public License, version 3 or later** (GPL-3.0-or-later). The full text is in [LICENSE](https://github.com/alessiobravi/claude-mirror/blob/main/LICENSE).

In short:

- You may **use, modify, and redistribute** this software, including in commercial settings.
- If you distribute a modified version (or any work that incorporates claude-mirror's source), you must release your changes under the same GPL-3.0-or-later license and make the corresponding source available to recipients.
- claude-mirror comes with **NO WARRANTY**, to the extent permitted by applicable law (see the Disclaimer above and Sections 15–17 of the GPL).

For the formal terms, see [LICENSE](https://github.com/alessiobravi/claude-mirror/blob/main/LICENSE) or [gnu.org/licenses/gpl-3.0](https://www.gnu.org/licenses/gpl-3.0.html).
