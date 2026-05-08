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
- **Near-real-time collaboration.** Pub/Sub (Drive), long-poll (Dropbox), or polling (OneDrive / WebDAV) push remote changes to other machines within seconds. Optional per-project Slack webhooks pipe events to a team channel.
- **No-loss conflict resolution.** When both sides change a file, interactive choice: keep local, keep remote, open `$EDITOR` for manual merge, or skip. No silent overwrites.

**Supported backends:** Google Drive, Dropbox, Microsoft OneDrive, any WebDAV server (Nextcloud, OwnCloud, Apache mod_dav, Synology/QNAP NAS, Box.com, etc.), and any SFTP/SSH-accessible server (VPS, NAS, shared hosting, self-hosted Linux). Each project picks its own primary backend independently — different projects on the same machine can use different backends.

**Quality gates:** Every commit and pull request runs **361 automated tests** on Python 3.11, 3.12, 3.13, and 3.14 in parallel via GitHub Actions — covering the 3-way diff sync core, both snapshot formats, path-traversal safety, conflict resolution, auth flows, all five backends (with HTTP-level / SSH-level mocking), the notifier inbox under concurrent writers, and the watcher daemon's SIGHUP hot-reload. CI must be green before any PR can merge. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the test conventions and how to run them locally.

---

## How it works

- Files matching configured patterns (default: `**/*.md`) are synced to a shared cloud folder
- A local manifest tracks file hashes to detect what changed since the last sync
- When you push, collaborators are notified in near-real-time:
  - **Google Drive** — Cloud Pub/Sub streaming (sub-second latency)
  - **Dropbox** — `files/list_folder/longpoll` (seconds latency)
  - **OneDrive / WebDAV** — periodic polling (default 30s, configurable)
- Conflicts (both sides changed) are resolved interactively: keep local, keep remote, or open in `$EDITOR`
- A snapshot is saved after every push or sync, enabling point-in-time recovery. Two formats are selectable per-project: **`blobs`** (content-addressed, deduplicated — identical files across snapshots stored once) or **`full`** (server-side copy of every file per snapshot)
- **Multi-backend mirroring (Tier 2)** — push to multiple backends simultaneously (e.g. Drive + Dropbox), with automatic per-backend retry, classified error handling, and snapshot mirroring. Set `mirror_config_paths` in the project YAML and add a config file per mirror.
- Optional **Slack** notifications on push/pull/sync/delete (per-project, opt-in, webhook-based)

---

## Supported storage backends

### Google Drive

- Full Google Drive API integration
- Real-time push notifications via Google Cloud Pub/Sub (streaming gRPC, sub-second latency)
- Server-side file copy for snapshots (no data transferred through client)
- Requires a Google Cloud project with Drive API and Pub/Sub API enabled
- OAuth2 with refresh token — authenticate once, then silent refresh indefinitely
- No optional dependency — included by default (`pipx install claude-mirror`)

### Dropbox

- Full Dropbox API integration via the `dropbox` Python SDK
- Near-real-time notifications via `files/list_folder/longpoll` (HTTP long-polling, seconds of latency)
- Server-side file copy for snapshots
- OAuth2 with PKCE — no client secret needed, simpler setup than Google Drive
- Requires a Dropbox app registration (free, no billing)
- No extra install step — included in the base package

### Microsoft OneDrive

- Full Microsoft Graph API integration via the `msal` Python SDK
- Notifications via periodic polling (configurable, default 30s)
- Simple upload (< 4 MB) and chunked upload sessions (> 4 MB)
- Server-side copy with async monitor polling for snapshots
- Device-code OAuth2 flow via MSAL — no redirect URI server needed, works on any machine including headless ones
- `quickXorHash` change detection (falls back to `sha1Hash`)
- Token cache with silent refresh
- Requires an Azure AD app registration (free)
- No extra install step — included in the base package

### WebDAV

- Standard WebDAV over HTTP via PUT, GET, PROPFIND, MKCOL, COPY, DELETE — no vendor lock-in
- Compatible with Nextcloud, OwnCloud, Apache (`mod_dav`), Nginx (`nginx-dav-ext-module`), Synology, QNAP, Box.com, and any RFC 4918-compliant server
- Notifications via periodic polling (configurable, default 30s)
- Basic auth (username + app password)
- ETag-based change detection, with OwnCloud/Nextcloud `oc:checksums` support (MD5/SHA1) when available
- No cloud account required — works on LAN, no API quotas, full data ownership
- Uses `requests` (already an explicit dependency, no extras needed)

### SFTP

- Standard SSH file transfer — works against any OpenSSH server, NAS device with SSH (Synology, QNAP, TrueNAS), VPS, shared hosting account, or self-hosted Linux box
- SSH key authentication (preferred) or password fallback for LAN-only setups
- Host-fingerprint verification via `~/.ssh/known_hosts` — same trust model as the `ssh` CLI
- Polling-based change detection — same as WebDAV / OneDrive (configurable, default 30s; no native push events over SFTP)
- Server-side `sha256sum` + `cp -p` via SSH `exec_command` when available; auto-falls back to client-side hashing for SFTP-only / `internal-sftp`-jailed accounts that disallow shell commands
- No cloud account, no OAuth, no per-vendor app registration — if you can `ssh user@host`, you can `claude-mirror push`
- Uses `paramiko>=3.0` (included in the base package, no extras needed)

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

## Part 1 — Backend setup (done once by the project owner)

Choose the backend for your project and follow the corresponding section below.

### Option A: Google Drive setup

#### Step 1: Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project selector at the top → **New Project**
3. Name it (e.g. `claude-mirror`) and click **Create**
4. Note your **Project ID** — you will need it later

#### Step 2: Enable APIs

In your new project:

1. Go to **APIs & Services** → **Library**
2. Search for and enable:
   - **Google Drive API**
   - **Cloud Pub/Sub API**

#### Step 3: Create OAuth 2.0 credentials

1. Go to **APIs & Services** → **Credentials**
2. Click **Create Credentials** → **OAuth client ID**
3. If prompted, configure the OAuth consent screen first:
   - User type: **External** (or Internal if using a Google Workspace org)
   - Fill in app name (e.g. `claude-mirror`) and your email
   - Add scopes: `../auth/drive` and `../auth/pubsub`
   - Add your own email as a test user
   - Save
4. Back on Create OAuth client ID:
   - Application type: **Desktop app**
   - Name: `claude-mirror`
   - Click **Create**
5. Click **Download JSON** on the confirmation dialog
6. Save the file using a name that identifies this Google account or GCP project:
   ```bash
   mkdir -p ~/.config/claude_mirror
   # e.g. for a work account:
   mv ~/Downloads/client_secret_*.json ~/.config/claude_mirror/work-credentials.json
   # e.g. for a personal account:
   mv ~/Downloads/client_secret_*.json ~/.config/claude_mirror/personal-credentials.json
   ```
   Using a descriptive name (rather than the generic `credentials.json`) keeps multiple accounts clearly separated and avoids accidental overwrites.

> This credentials file is shared with all collaborators (it identifies the app, not any individual user). Do **not** share the token file — that is per-person and contains individual access tokens.

#### Step 4: Create a Pub/Sub topic

1. Go to **Pub/Sub** → **Topics**
2. Click **Create Topic**
3. Topic ID: choose a name that identifies your project, e.g. `claude-mirror-myproject`
4. Leave **Add a default subscription** unchecked (claude-mirror creates per-machine subscriptions automatically)
5. Click **Create**

#### Step 5: Grant collaborator access

For each collaborator's Google account:

1. Go to **IAM & Admin** → **IAM**
2. Click **Grant Access**
3. Enter their Google account email
4. Assign this role:
   - **Pub/Sub Editor** (to publish and subscribe)

> **Note:** no Drive-specific IAM role is needed — Drive access is managed by sharing the folder directly (see Step 6).

For Drive folder access: share the Drive folder directly with each collaborator's Google account (see Step 6).

#### Step 6: Create the shared Drive folder

1. Go to [drive.google.com](https://drive.google.com)
2. Create a new folder (e.g. `claude-mirror-myproject`)
3. Open the folder — the URL will look like:
   ```
   https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
   ```
4. Copy the folder ID — the long string after `/folders/`
5. Share the folder with each collaborator's Google account (Editor access)

### Option B: Dropbox setup

#### Step 1: Create a Dropbox app

1. Go to [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
2. Click **Create app**
3. Choose:
   - **Scoped access**
   - **Full Dropbox** (or App folder if you prefer isolation)
   - App name: e.g. `claude-mirror`
4. Click **Create app**

#### Step 2: Configure permissions

On the app's **Permissions** tab:

1. Enable:
   - `files.content.read`
   - `files.content.write`
2. Click **Submit** at the bottom

#### Step 3: Note your app key

On the app's **Settings** tab, copy the **App key**. You will need it during `claude-mirror init`.

No client secret is needed — claude-mirror uses OAuth2 with PKCE.

#### Step 4: Share with collaborators

Share the Dropbox folder (e.g. `/claude-mirror/myproject`) with collaborators via Dropbox's normal sharing. Each collaborator creates their own Dropbox app (Step 1) or you share the same app key.

### Option C: Microsoft OneDrive setup

#### Step 1: Register an Azure AD app

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory** → **App registrations** → **New registration**
2. Name: `claude-mirror` (or anything you like)
3. Supported account types: **Personal Microsoft accounts only** (or "Accounts in any organizational directory and personal Microsoft accounts" for mixed use)
4. Click **Register**
5. From the overview page, copy the **Application (client) ID** — you will need it during `claude-mirror init`

#### Step 2: Configure platform and permissions

1. Go to **Authentication** → **Add a platform** → **Mobile and desktop applications**
2. Add the redirect URI: `https://login.microsoftonline.com/common/oauth2/nativeclient`
3. Save
4. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**
5. Add: `Files.ReadWrite` and `offline_access`
6. Click **Grant admin consent** (only relevant if you're using an organizational tenant)

> No client secret is needed — claude-mirror uses the device-code OAuth flow, which works on any machine including headless ones.

#### Step 3: Decide on the OneDrive folder

Pick a path inside your OneDrive where the project will live, e.g. `claude-mirror/myproject`. The folder will be created on first sync if it doesn't exist.

#### Step 4: Share with collaborators

For each collaborator, share the OneDrive folder via OneDrive's normal sharing UI (Editor permission). Each collaborator uses the same Azure app's client ID — there's no per-user secret involved.

### Option D: WebDAV setup (Nextcloud, OwnCloud, NAS, etc.)

WebDAV requires no cloud account or app registration — any RFC 4918 server works.

#### Step 1: Identify the WebDAV URL

Examples:
- **Nextcloud** — `https://my-server.com/remote.php/dav/files/<username>/claude-mirror/`
- **OwnCloud** — `https://my-server.com/remote.php/webdav/claude-mirror/`
- **Apache `mod_dav`** — whatever URL the admin configured
- **Synology** — `https://<nas-host>:5006/<webdav-share>/claude-mirror/` (after enabling WebDAV in DSM Control Panel → File Services)
- **Box.com** — `https://dav.box.com/dav/claude-mirror/`

#### Step 2: Generate an app password

For services that support app passwords (Nextcloud, OwnCloud, FastMail, etc.), generate one specifically for claude-mirror rather than using your account password. The app password will be stored in `~/.config/claude_mirror/<project>-token.json` (chmod 0600).

#### Step 3: Pick the project folder

Decide on a folder name (e.g. `claude-mirror-myproject`) and create it on the WebDAV server (via the web UI or via `mkdir` over WebDAV — claude-mirror will also create it on first push if needed).

#### Step 4: Share with collaborators

Use the WebDAV server's native share/permissions UI to grant each collaborator read+write access to the project folder.

### Option E: SFTP setup (any server with SSH access)

SFTP requires no cloud account or app registration — any OpenSSH server works. If you can `ssh user@host` interactively, you can use it as a claude-mirror backend.

#### Step 1: Choose authentication (SSH key strongly recommended)

SSH key authentication is the supported default. Password authentication is supported as a LAN-only fallback for legacy / NAS setups that don't accept keys.

If you don't already have a key, generate one:

```bash
ssh-keygen -t ed25519 -C "claude-mirror@$(hostname)"
# Press enter to accept the default path (~/.ssh/id_ed25519)
# Use a passphrase or leave empty — claude-mirror runs non-interactively, so an empty passphrase or an ssh-agent-loaded key both work
```

#### Step 2: Add the public key to the server

The simplest path is `ssh-copy-id`, which appends your public key to the remote `~/.ssh/authorized_keys` over a single password-authenticated SSH connection:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub alice@files.example.com
```

If `ssh-copy-id` is not available (some NAS firmwares strip it), append the key manually:

```bash
cat ~/.ssh/id_ed25519.pub | ssh alice@files.example.com 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
```

#### Step 3: Verify interactive SSH works (and trust the host fingerprint)

This step is the prerequisite that lets `claude-mirror` connect non-interactively later — you must accept the server's host fingerprint into `~/.ssh/known_hosts` once, from the OpenSSH client, before claude-mirror can verify it on subsequent connects.

```bash
ssh alice@files.example.com
# First time: ssh prints the host's fingerprint and asks "Are you sure you want to continue connecting (yes/no)?"
# Type "yes" to add it to ~/.ssh/known_hosts, then exit.
```

If `claude-mirror` later refuses to connect with a host-key mismatch error, this is the file to inspect or update.

#### Step 4: Decide on the server-side folder

Pick an absolute path on the server where the project will live, e.g. `/home/alice/claude-mirror/myproject`. You don't need to pre-create it — `claude-mirror` will `mkdir -p` the folder on first connect if it doesn't exist.

If the account is chrooted to a smaller subtree (Step 5), use a path relative to that chroot — e.g. `/myproject` if the account is jailed to `/home/alice/sftp/`.

#### Step 5 (optional): Lock the account down to SFTP-only

For a dedicated mirror account, add the following to the server's `/etc/ssh/sshd_config`:

```
Match User alice
    ForceCommand internal-sftp
    ChrootDirectory /home/alice/sftp
    AllowTcpForwarding no
    X11Forwarding no
```

Trade-off: `internal-sftp` disables shell `exec_command`, so the server-side `sha256sum` + `cp -p` optimizations are unavailable. `claude-mirror` falls back automatically to client-side hashing + `get`/`put` for snapshots — slightly slower on large files, but otherwise functionally identical.

---

## Part 2 — Installation (every machine)

### Step 1: Clone or copy the repository

```bash
git clone https://github.com/alessiobravi/claude-mirror.git
cd claude-mirror
```

Or copy the `Claude_Sync/` directory to the machine.

### Step 2: Install

#### Recommended: pipx from PyPI (globally available, no activation needed)

[pipx](https://pipx.pypa.io) installs the package into an isolated environment and puts `claude-mirror` on your PATH permanently — no venv activation required. This is the recommended approach, especially if you use the Claude Code skill.

```bash
brew install pipx   # macOS; see https://pipx.pypa.io for other platforms
pipx ensurepath     # adds ~/.local/bin to PATH if not already there

pipx install claude-mirror
```

All backends (Google Drive, Dropbox, OneDrive, WebDAV, SFTP) ship in this single install — no per-backend extras needed. Pick which one to use later via `claude-mirror init --backend ...`.

Verify the install:

```bash
claude-mirror --version
```

#### Alternative: pip in a virtual environment

If you prefer not to use pipx, install via pip into a venv. Note that `claude-mirror` will only be available when the venv is active, so the Claude Code skill (which runs in a non-interactive shell) won't be able to call it — use pipx for that case.

```bash
python3 -m venv ~/.venvs/claude-mirror
source ~/.venvs/claude-mirror/bin/activate
pip install claude-mirror
```

You must activate the venv in every new shell:
```bash
source ~/.venvs/claude-mirror/bin/activate
```

#### Developer install (editable, from a clone)

If you want to hack on the code, clone the repo and install editably so your edits take effect without reinstalling:

```bash
git clone https://github.com/alessiobravi/claude-mirror.git
cd claude-mirror
pipx install -e .
```

### Step 3: Install components

Once `claude-mirror` is on your PATH, run the installer to set up the Claude Code skill, notification hook, and background watcher in one step:

```bash
claude-mirror-install
```

You will be prompted to confirm each component before anything is written or changed. To remove all components later:

```bash
claude-mirror-install --uninstall
```

See [Manual component installation](#manual-component-installation) if you prefer to configure each piece individually.

To verify desktop notifications are working, run `claude-mirror test-notify` after installation (see [Desktop notifications](#desktop-notifications) for platform-specific permission setup).

### Step 3.5: Shell tab-completion

`claude-mirror-install` automatically installs shell tab-completion as one of the components — it detects your shell (zsh / bash / fish), adds an `eval` line to your rc file (zsh / bash) or writes a completion file (fish), and prompts before any change. Marker comments wrap the addition so a future `claude-mirror-install --uninstall` removes it cleanly.

After install, the installer offers to replace the current shell with a fresh interactive shell so the new completion is live immediately. If you decline, you can activate it yourself by either opening a new terminal or running `source ~/.zshrc` (zsh), `source ~/.bash_profile` on macOS or `source ~/.bashrc` on Linux (bash), or simply opening a new fish shell (fish auto-loads completion files from `~/.config/fish/completions`).

Once active, `claude-mirror <TAB>` lists all commands, `claude-mirror push <TAB>` lists flags, and `claude-mirror init --backend <TAB>` shows the five valid backends `googledrive`, `dropbox`, `onedrive`, `webdav`, and `sftp`.

If you want to install completion manually instead (or for a different shell on the same machine):

```bash
# zsh — append to ~/.zshrc
eval "$(claude-mirror completion zsh)"

# bash — append to ~/.bashrc
eval "$(claude-mirror completion bash)"

# fish — write to the completions dir
claude-mirror completion fish > ~/.config/fish/completions/claude-mirror.fish
```

### Step 4: Update

For PyPI installs (the default — `pipx install claude-mirror`):

```bash
pipx upgrade claude-mirror
```

Or use the built-in self-upgrade command, which also auto-detects editable installs:

```bash
claude-mirror update --apply
```

For editable installs from a clone:

```bash
cd /path/to/claude-mirror
git pull
pipx install -e . --force
```

`--force` is required to make pipx pick up any new dependencies added to `pyproject.toml`. Because the install is editable, pure code changes (no new dependencies) take effect immediately — but running `--force` after every pull is safe and ensures nothing is missed.

If you also updated the skill file, re-run the installer:

```bash
claude-mirror-install
```

### Step 5: Copy credentials (Google Drive only)

Copy the credentials file (obtained from the project owner in Part 1, Option A, Step 3) to `~/.config/claude_mirror/`, keeping the same descriptive name used by the owner. The `~/.config/claude_mirror/` directory is created automatically by `claude-mirror init`, but you can also create it now:

```bash
mkdir -p ~/.config/claude_mirror
cp work-credentials.json ~/.config/claude_mirror/work-credentials.json
```

> Dropbox does not use a credentials file — authentication is handled entirely through the OAuth2 PKCE flow during `claude-mirror auth`.

---

## Part 3 — Project setup (run once per project, on every machine)

Each project gets its own config file, named after the project directory. Repeat these steps for every project you want to sync.

### Step 1: Initialize

The easiest way is the interactive wizard — run it from inside your project directory:

```bash
cd /path/to/your/claude/project
claude-mirror init --wizard
```

The wizard walks you through each value, shows sensible defaults, and prints a confirmation summary before saving. It asks which backend to use first, then prompts for backend-specific fields.

#### Google Drive wizard example

```
claude-mirror setup wizard

Press Enter to accept the default shown in brackets.

Storage backend [googledrive]:
Project directory [/home/user/work/myproject]:
Credentials file [~/.config/claude_mirror/credentials.json]: ~/.config/claude_mirror/work-credentials.json

Drive folder ID: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
GCP project ID: my-gcp-project
Pub/Sub topic ID [claude-mirror-myproject]:
Token file [~/.config/claude_mirror/work-token.json]:
Config file [~/.config/claude_mirror/myproject.yaml]:
File patterns [**/*.md]:

Summary
  Backend:       googledrive
  Project:       /home/user/work/myproject
  Config:        ~/.config/claude_mirror/myproject.yaml
  Token:         ~/.config/claude_mirror/work-token.json
  Credentials:   ~/.config/claude_mirror/work-credentials.json
  Drive folder:  1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
  GCP project:   my-gcp-project
  Pub/Sub topic: claude-mirror-myproject
  Patterns:      **/*.md
  Exclude:       (none)

Save this configuration? [Y/n]:
```

#### Dropbox wizard example

```
Storage backend [googledrive]: dropbox
Dropbox app key: your-app-key-here
Dropbox folder [/claude-mirror/myproject]:
Token file [~/.config/claude_mirror/dropbox-myproject-token.json]:
File patterns [**/*.md]:

Summary
  Backend:       dropbox
  Dropbox folder:/claude-mirror/myproject
  App key:       your-app-key-here
  ...
```

#### OneDrive wizard example

```
Storage backend [googledrive]: onedrive
OneDrive client ID (Azure app registration): 12345678-aaaa-bbbb-cccc-1234567890ab
OneDrive folder [claude-mirror/myproject]:
Token file [~/.config/claude_mirror/onedrive-myproject-token.json]:
Poll interval (seconds) [30]:
File patterns [**/*.md]:

Summary
  Backend:        onedrive
  Client ID:      12345678-aaaa-bbbb-cccc-1234567890ab
  OneDrive folder:claude-mirror/myproject
  Poll interval:  30s
  ...
```

#### WebDAV wizard example

```
Storage backend [googledrive]: webdav
WebDAV URL: https://nextcloud.example.com/remote.php/dav/files/alice/claude-mirror/
WebDAV username: alice
WebDAV password (or app password): ••••••••
Token file [~/.config/claude_mirror/webdav-myproject-token.json]:
Poll interval (seconds) [30]:
File patterns [**/*.md]:

Summary
  Backend:       webdav
  URL:           https://nextcloud.example.com/remote.php/dav/files/alice/claude-mirror/
  Username:      alice
  Poll interval: 30s
  ...
```

The WebDAV password is stored in `<token-file>` with `chmod 0600` (owner read/write only). Prefer an app password over your account password.

#### CLI flags (non-interactive)

**Google Drive:**

```bash
claude-mirror init \
  --project /path/to/your/claude/project \
  --backend googledrive \
  --drive-folder-id 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt \
  --gcp-project-id my-gcp-project-id \
  --pubsub-topic-id claude-mirror-myproject \
  --credentials-file ~/.config/claude_mirror/work-credentials.json
```

**Dropbox:**

```bash
claude-mirror init \
  --project /path/to/your/claude/project \
  --backend dropbox \
  --dropbox-app-key your-app-key \
  --dropbox-folder /claude-mirror/myproject
```

**OneDrive:**

```bash
claude-mirror init \
  --project /path/to/your/claude/project \
  --backend onedrive \
  --onedrive-client-id 12345678-aaaa-bbbb-cccc-1234567890ab \
  --onedrive-folder claude-mirror/myproject \
  --poll-interval 30
```

**WebDAV:**

```bash
claude-mirror init \
  --project /path/to/your/claude/project \
  --backend webdav \
  --webdav-url https://nextcloud.example.com/remote.php/dav/files/alice/claude-mirror/ \
  --webdav-username alice \
  --webdav-password 'app-password-here' \
  --poll-interval 30
```

`init` automatically:
- Creates `~/.config/claude_mirror/` if it does not exist
- Names the config file after the project directory: `~/.config/claude_mirror/myproject.yaml`
- Derives the token filename from the credentials filename (Google Drive) or project name (Dropbox)

Available flags:

| Flag | Default | Description |
|---|---|---|
| `--wizard` | — | Launch interactive setup wizard. |
| `--backend` | `googledrive` | Storage backend: `googledrive`, `dropbox`, `onedrive`, `webdav`, or `sftp`. |
| `--drive-folder-id ID` | — | Google Drive folder ID (Google Drive only). |
| `--gcp-project-id ID` | — | Google Cloud project ID (Google Drive only). |
| `--pubsub-topic-id ID` | — | Pub/Sub topic ID (Google Drive only). |
| `--credentials-file PATH` | `~/.config/claude_mirror/credentials.json` | OAuth2 credentials JSON (Google Drive only). |
| `--dropbox-app-key KEY` | — | Dropbox app key (Dropbox only). |
| `--dropbox-folder PATH` | — | Dropbox folder path, e.g. `/claude-mirror/myproject` (Dropbox only). |
| `--onedrive-client-id ID` | — | Azure AD app client ID (OneDrive only). |
| `--onedrive-folder PATH` | — | OneDrive folder path, e.g. `claude-mirror/myproject` (OneDrive only). |
| `--webdav-url URL` | — | WebDAV server URL including project folder (WebDAV only). |
| `--webdav-username USER` | — | WebDAV username (WebDAV only). |
| `--webdav-password PASS` | — | WebDAV password or app password (WebDAV only). Stored in token file with `chmod 0600`. |
| `--poll-interval SECS` | `30` | Polling interval in seconds (OneDrive, WebDAV only). |
| `--slack/--no-slack` | `--no-slack` | Enable optional Slack notifications. |
| `--slack-webhook-url URL` | — | Slack incoming-webhook URL (only if `--slack`). |
| `--slack-channel CHAN` | *(webhook default)* | Override the Slack channel. |
| `--token-file PATH` | *(auto-derived)* | Override the token file path. |
| `--patterns GLOB` | `**/*.md` | File glob patterns to sync. Can be repeated. |
| `--exclude GLOB` | *(none)* | Glob patterns to exclude from sync. Can be repeated. E.g. `--exclude 'archive/**'`. |
| `--config PATH` | `~/.config/claude_mirror/<project>.yaml` | Override the auto-generated config file path. |

### Step 2: Authenticate

```bash
claude-mirror auth
```

Run this from inside the project directory (config is auto-detected from cwd).

**Google Drive:** A browser window opens — log in with the Google account that has access to the Drive folder and GCP project. After login, the Pub/Sub topic and per-machine subscription are verified and created if needed.

**Dropbox:** An authorization URL is printed — visit it in your browser, authorize the app, and paste the authorization code back into the terminal. The refresh token is saved for silent refresh on subsequent runs.

**OneDrive:** A device-code login flow runs — claude-mirror prints a short code and a URL. Open the URL in any browser, paste the code, and sign in with the Microsoft account that has access to the OneDrive folder. The token cache is saved for silent refresh.

**WebDAV:** No interactive browser flow. The URL, username, and password you provided at `init` are validated against the server (a `PROPFIND` on the project folder) and written to the token file with `chmod 0600`.

> Each collaborator runs `claude-mirror auth` with their own account. Subscriptions and tokens are unique per machine.
>
> All token files (Google, Dropbox, OneDrive, WebDAV) are written with owner-only permissions (`chmod 0600`). The WebDAV token additionally stores the password in plaintext inside the `0600` file — for that reason, prefer an app password over your real account password whenever your server supports them (Nextcloud, OwnCloud, FastMail, etc.).

### Excluding files and directories

Use `exclude_patterns` in the config file (or `--exclude` on `init`) to prevent specific files or directories from ever being synced or appearing in `status`:

```yaml
# ~/.config/claude_mirror/myproject.yaml
file_patterns:
  - "**/*.md"
exclude_patterns:
  - "archive/**"        # entire directory
  - "drafts/**"         # another directory
  - "**/*_draft.md"     # any file ending in _draft.md
  - "private.md"        # a specific file
```

You can also set this at init time with the `--exclude` flag (repeatable):

```bash
claude-mirror init \
  --project /path/to/project \
  ... \
  --exclude 'archive/**' \
  --exclude '**/*_draft.md'
```

Or enter the patterns comma-separated when the wizard asks for them.

**How matching works:**

- Patterns follow Python `fnmatch` glob syntax (`*`, `**`, `?`, `[...]`)
- `archive/**` excludes everything inside `archive/` at any depth
- `**/*_draft.md` excludes any file ending in `_draft.md` anywhere in the tree
- `private.md` excludes only that exact file at the project root
- Excluded files are invisible to all commands: `status`, `push`, `pull`, `sync`, `delete`

> **Note:** files already on remote storage before adding an exclude pattern will remain there — they are just ignored by future syncs. To remove them, run `claude-mirror delete` before adding the exclusion.

### Multiple projects on the same machine

Simply repeat Steps 1 and 2 for each project. Every project gets its own config file:

```
~/.config/claude_mirror/
├── proj-a.yaml          # config for ~/work/proj-a (googledrive)
├── proj-b.yaml          # config for ~/personal/proj-b (dropbox)
└── proj-c.yaml          # config for ~/work/proj-c (googledrive)
```

Different projects can use different backends. All commands auto-detect the right config from the current working directory — no `--config` flag needed during daily use.

### Multiple accounts and mixed backends

The same machine can mix backends across projects — one project on Google Drive, another on Dropbox, a third on a Nextcloud-via-WebDAV server, all running side by side. Each project's config records its own backend, credentials, and token, and every command auto-detects the right config from the current working directory.

If some projects use a different Google account (different GCP project, different Drive), pass the matching `--credentials-file` to `init` for each project. The token file is derived automatically:

```bash
# Work projects — authenticated as work account
cd ~/work/proj-a
claude-mirror init --wizard   # enter ~/.config/claude_mirror/work-credentials.json when prompted
# → config:  ~/.config/claude_mirror/proj-a.yaml
# → token:   ~/.config/claude_mirror/work-token.json  (auto-derived)

# Personal projects — authenticated as personal account
cd ~/personal/proj-b
claude-mirror init --wizard   # enter ~/.config/claude_mirror/personal-credentials.json when prompted
# → config:  ~/.config/claude_mirror/proj-b.yaml
# → token:   ~/.config/claude_mirror/personal-token.json  (auto-derived)
```

Then run `claude-mirror auth` once inside each project directory. Each project stores its credentials and token paths in its own config — all subsequent commands pick them up automatically.

**Sharing an account across projects**: if several projects use the same Google account, enter the same `--credentials-file` when running the wizard for each. They will share the credentials and token files transparently. The same principle applies to OneDrive (same `--onedrive-client-id`) and WebDAV (same URL + username).

---

## Part 4 — Daily usage

All commands work identically regardless of which backend a project uses. The backend is transparent during daily use.

### Check sync status

Before pushing or pulling, see what has changed:

```bash
claude-mirror status
```

While `status` runs, two live progress lines (Local / Remote) show file-counting progress so you can see it has not stalled on a slow filesystem or remote.

Output includes a per-file table followed by a color-coded summary line:

```
  1 conflict  ·  2 local ahead  ·  1 remote ahead  ·  5 in sync
```

Or, when everything is clean:

```
All 5 file(s) in sync.
```

For a compact one-line view (no table), use `--short`:

```bash
claude-mirror status --short
```

This is used automatically by the Claude Code skill to avoid collapsed output in the terminal.

Output columns:

| Status | Meaning | Suggested action |
|---|---|---|
| `in sync` | No changes | Nothing to do |
| `local ahead` | You changed it, remote has not | `push` |
| `remote ahead` | Someone else pushed, you have not | `pull` |
| `conflict` | Both sides changed | `sync` (prompts for resolution) |
| `new local` | New file not yet on remote | `push` |
| `new on remote` | New file from collaborator | `pull` |
| `deleted local` | Removed locally, still on remote | `push` (deletes from remote) |

### Push your changes

```bash
claude-mirror push
```

Pushes all locally changed files to the remote storage, then:
- Creates a snapshot
- Publishes a notification to all collaborators

Push specific files only:

```bash
claude-mirror push CLAUDE.md memory/notes.md
```

### Pull remote changes

```bash
claude-mirror pull
```

Downloads all remote-ahead files to your machine, updating the local manifest.

Pull specific files:

```bash
claude-mirror pull memory/notes.md
```

#### Preview before pulling

Use `--output` to download remote versions to a separate directory without touching your local files or manifest. Useful for inspecting remote changes before deciding what to do:

```bash
claude-mirror pull --output ~/.local/tmp/claude-mirror/preview
claude-mirror pull memory/notes.md --output ~/.local/tmp/claude-mirror/preview
```

Files are written to `<output-dir>/<relative-path>`. Your local project is untouched and the manifest is not updated. The Claude Code skill uses this automatically when merging remote and session changes.

### Compare local vs remote for a single file

Before deciding to push, pull, or merge, you can see exactly what differs:

```bash
claude-mirror diff memory/CLAUDE.md
```

`diff` prints a colorized unified diff (remote → local). Green `+` lines are what would be pushed; red `-` lines are what would be pulled. Hunk headers (`@@`) and `--- remote` / `+++ local` headers match standard `git diff` style, so the output is usable with any tool that consumes unified diffs.

The path argument accepts either a project-relative path (`memory/CLAUDE.md`) or an absolute path inside the project root (`/Users/me/work/proj/memory/CLAUDE.md`); absolute paths outside the project are rejected up-front. Adjust context with `--context N` (default 3, max 200):

```bash
claude-mirror diff memory/CLAUDE.md --context 8
```

`diff` handles every state combination cleanly:

- **both sides differ** — full unified diff with `@@` hunk headers
- **only on local** — every line shown as added (would be pushed)
- **only on remote** — every line shown as deleted (would be pulled)
- **identical** — single "in sync" message, exit 0
- **binary file** — refused with a one-line note rather than rendering garbage
- **missing on both sides** — clear error, exit 1

`diff` is read-only — it never modifies local or remote state.

### Full bidirectional sync

```bash
claude-mirror sync
```

Handles everything in one command:
- Pushes local-ahead files
- Pulls remote-ahead files
- Prompts interactively for conflicts
- Creates a snapshot and notifies collaborators after completion

### Receive notifications from collaborators

Run the watcher in the background or in a dedicated terminal:

```bash
claude-mirror watch
```

When a collaborator pushes, you will receive a system notification:

```
claude-mirror
alice@workstation updated CLAUDE.md, memory/notes.md in 'myproject'.
Run `claude-mirror sync` to merge.
```

Press `Ctrl+C` to stop the watcher.

The notification channel depends on the backend, but the user-facing behaviour is identical:

| Backend | Mechanism | Typical latency |
|---|---|---|
| Google Drive | Cloud Pub/Sub streaming pull (persistent gRPC connection) | Sub-second |
| Dropbox | `files/list_folder/longpoll` HTTP long-polling | Seconds |
| OneDrive | Periodic polling, configurable `poll_interval` (default 30s) | Up to `poll_interval` |
| WebDAV | Periodic polling, configurable `poll_interval` (default 30s) | Up to `poll_interval` |

### View sync activity log

```bash
claude-mirror log
```

Shows who pushed what and when, across all machines. The log is stored on the remote storage and shared with all collaborators.

Each entry has an action label with distinct coloring:

| Action | Color | Meaning |
|---|---|---|
| `push` | cyan | Files uploaded |
| `sync` | blue | Bidirectional sync completed |
| `pull` | blue | Files downloaded |
| `delete` | red | Files deleted from remote |

---

## Conflict resolution

When both your local file and the remote version changed since the last sync, claude-mirror prompts you to resolve the conflict:

```
Conflict in: CLAUDE.md

┌─ LOCAL ─────────────────────────────────┐
│ # My Project                            │
│ local version of the file...            │
└─────────────────────────────────────────┘

┌─ DRIVE ─────────────────────────────────┐
│ # My Project                            │
│ collaborator's version of the file...   │
└─────────────────────────────────────────┘

[L] Keep local  [D] Keep drive  [E] Open in editor  [S] Skip
```

- **L** — discard the remote version, keep yours, push it
- **D** — discard your local version, keep the remote version
- **E** — open a temporary file in `$EDITOR` with conflict markers:
  ```
  <<<<<<< LOCAL
  your content here
  =======
  collaborator's content here
  >>>>>>> DRIVE
  ```
  Edit the file to the desired result, save and exit. The resolved version is pushed.
- **S** — skip this file for now (it stays unresolved)

Set your preferred editor:

```bash
export EDITOR=nano   # or vim, code, etc.
```

---

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

## Multi-backend mirroring (Tier 2)

A single project can be synced to multiple storage backends at the same time. Push uploads to all of them in parallel, snapshots are mirrored across all of them (configurable), and pull / status read from the primary. If a mirror fails transiently it is retried automatically on the next push; permanent failures are quarantined and surfaced via `claude-mirror status --pending` and the desktop / Slack notifiers.

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

Slack messages now include a per-backend status block, e.g.:

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

---

## Claude Code skill

claude-mirror ships a skill for [Claude Code](https://claude.ai/claude-code) that lets you run all sync operations directly from your AI conversation, and surfaces remote notifications inline without leaving the editor.

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

### Step 1: Install the skill

Run `claude-mirror-install` (if you have not done so already in Part 2):

```bash
claude-mirror-install
```

It installs the skill alongside the notification hook and watcher service. For the manual install command, see [Manual component installation](#manual-component-installation).

No further configuration needed — the skill auto-detects the active project.

### Step 2: Start the background watcher

If you ran `claude-mirror-install` in Part 2, the watcher is already running as a persistent service — skip this step.

Otherwise, start it manually in a dedicated terminal:

```bash
claude-mirror watch-all
```

Or set it up as a persistent service so it starts on login (see [Auto-start the watcher](#auto-start-the-watcher)).

### Step 3: Enable inline notifications in Claude Code (optional but recommended)

By default, notifications are only surfaced when you manually type `/claude-mirror`. You can make them appear automatically inside the Claude Code conversation by adding a `PreToolUse` hook to `~/.claude/settings.json`.

`claude-mirror-install` adds this hook automatically. If you set things up manually, see the hook JSON in [Manual component installation](#manual-component-installation).

How it works:
- The hook runs silently before every tool call Claude makes
- `claude-mirror inbox` auto-detects the project from cwd, prints any pending notifications, and clears them
- If nothing is pending it produces no output — completely invisible
- When a notification is present, Claude sees the output and surfaces it in the conversation immediately, without you having to type anything

Combined with the background watcher, the full notification flow becomes:

| You are... | How you get notified |
|---|---|
| Actively using Claude Code | Hook fires on next tool call — notification appears inline |
| Idle / away from Claude Code | Desktop notification from the watcher |

### Step 4: Use the skill in Claude Code

In any Claude Code conversation, type:

```
/claude-mirror
```

Claude will automatically:
1. Detect the config for your current working directory
2. Report any pending notifications from collaborators
3. Show the full sync status
4. Analyze and merge remote changes (see below)

#### Smart merge workflow

When the skill detects remote-ahead or conflict files it does **not** pull blindly. Instead it:

1. Reads the current local file content (capturing any in-session edits)
2. Downloads remote versions to `~/.local/tmp/claude-mirror/preview/` via `pull --output` — local files are untouched
3. Diffs local vs remote and reports what changed on each side
4. Produces an intelligent merged result that incorporates both the remote changes and any local session changes
5. Writes the merged file and offers to push immediately

If both sides edited the same region and the merge cannot be done cleanly, Claude shows the conflict and asks how to resolve it before writing anything.

Example session after a collaborator pushed:

```
You: /claude-mirror

Claude: Config: ~/.config/claude_mirror/myproject.yaml

  Pending notifications:
  [2026-03-05 11:30:00] alice@workstation pushed CLAUDE.md, memory/notes.md

  Status:
  ┌─────────────────┬─────────────┬────────┐
  │ File            │ Status      │ Action │
  ├─────────────────┼─────────────┼────────┤
  │ CLAUDE.md       │ drive ahead │ ← pull │
  │ memory/notes.md │ drive ahead │ ← pull │
  └─────────────────┴─────────────┴────────┘

  Downloading remote versions for analysis...

  CLAUDE.md — Alice added a "Deployment" section (lines 42-58).
  Your session added 3 lines to the "Architecture" section.
  Merged: both changes applied. Ready to push.

  memory/notes.md — Alice updated the "Next steps" list.
  No local session changes. Merged trivially.

  Both files updated locally. Push now?

You: yes

Claude: [runs claude-mirror push CLAUDE.md memory/notes.md --config ...]
  ↑ CLAUDE.md
  ↑ memory/notes.md
  Pushed 2 file(s).
```

### Skill commands

You can ask Claude to run any operation in natural language:

| You say | Claude runs |
|---|---|
| "push my changes" | `claude-mirror push --config ...` |
| "pull the latest" | preview via `pull --output ~/.local/tmp/claude-mirror/preview`, merge, then push |
| "sync everything" | `claude-mirror sync --config ...` |
| "what's different in this file?" | `claude-mirror diff <path> --config ...` |
| "show me the snapshots" | `claude-mirror snapshots --config ...` |
| "restore to 10:30 this morning" | `claude-mirror restore 2026-03-05T10-30-00Z --output ~/.local/tmp/claude-mirror/review --config ...` |
| "clean up old snapshots" | `claude-mirror prune --config ...` (dry-run, then `--delete --yes` to apply) |
| "what changed recently" | `claude-mirror log --config ...` |
| "check for notifications" | `claude-mirror inbox --config ...` |

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

### find-config

The skill uses `claude-mirror find-config` internally to detect the active project from the current working directory. You can also use it directly:

```bash
claude-mirror find-config                    # match current directory
claude-mirror find-config ~/projects/proj-a  # match a specific path
```

It searches all `~/.config/claude_mirror/*.yaml` files for one whose `project_path` matches, falling back to `default.yaml` if none match. A `default.yaml` is created when you run `claude-mirror init` without `--config` and the project directory name cannot be determined; in most setups you will have named config files (e.g. `myproject.yaml`) instead.

---

## Command reference

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
claude-mirror history           PATH [--config PATH]
claude-mirror retry             [--backend NAME] [--dry-run] [--config PATH]
claude-mirror seed-mirror       --backend NAME [--dry-run] [--config PATH]   # populate a freshly-added mirror with files already on the primary
claude-mirror restore           TIMESTAMP [PATH ...] [--backend NAME] [--output PATH] [--config PATH]
claude-mirror forget            TIMESTAMP... | --before DATE/DURATION | --keep-last N | --keep-days N
                              [--delete] [--yes] [--config PATH]   # dry-run by default; --delete to actually delete
claude-mirror prune             [--keep-last N] [--keep-daily N] [--keep-monthly N] [--keep-yearly N]
                              [--delete] [--yes] [--config PATH]   # dry-run by default; reads keep_* from config
claude-mirror gc                [--backend NAME] [--delete] [--yes] [--config PATH]   # dry-run by default; --delete to actually delete; --backend targets a specific mirror (Tier 2)
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

## Config file examples

### Google Drive

```yaml
backend: googledrive
project_path: /home/user/work/myproject
drive_folder_id: 1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
gcp_project_id: my-gcp-project
pubsub_topic_id: claude-mirror-myproject
credentials_file: /home/user/.config/claude_mirror/work-credentials.json
token_file: /home/user/.config/claude_mirror/work-token.json
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
# Optional: snapshot retention policy. Each field defaults to 0 (= disabled).
# Union of every selector's keep-set is retained; everything else is pruned
# automatically after each successful `claude-mirror push`.
keep_last:    7      # always keep the 7 newest snapshots
keep_daily:   14     # plus one snapshot per day for the last 14 days
keep_monthly: 12     # plus one snapshot per month for the last 12 months
keep_yearly:  5      # plus one snapshot per year for the last 5 years
```

### Dropbox

```yaml
backend: dropbox
project_path: /home/user/work/myproject
dropbox_app_key: your-app-key
dropbox_folder: /claude-mirror/myproject
token_file: /home/user/.config/claude_mirror/dropbox-myproject-token.json
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
```

### OneDrive

```yaml
backend: onedrive
project_path: /home/user/work/myproject
onedrive_client_id: <your-azure-client-id>
onedrive_folder: claude-mirror/myproject
token_file: /home/user/.config/claude_mirror/onedrive-myproject-token.json
poll_interval: 30
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
```

### WebDAV

```yaml
backend: webdav
project_path: /home/user/work/myproject
webdav_url: https://nextcloud.example.com/remote.php/dav/files/alice/claude-mirror/
webdav_username: alice
# password is NOT stored in this config — it lives only in the token file (chmod 0600)
token_file: /home/user/.config/claude_mirror/webdav-myproject-token.json
poll_interval: 30
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
```

### SFTP

```yaml
backend: sftp
project_path: /home/user/work/myproject
sftp_host: files.example.com           # hostname or IP of the SSH server
sftp_port: 22                          # default 22 — change if the server listens elsewhere
sftp_username: alice                   # the SSH user — same one you'd use with `ssh user@host`
sftp_key_file: ~/.ssh/id_ed25519       # PREFERRED — path to the private key file
sftp_password: ""                      # FALLBACK — leave empty when using a key; LAN-only when set
sftp_known_hosts_file: ~/.ssh/known_hosts   # where the host fingerprint is read from
sftp_strict_host_check: true           # default true — refuse to connect on host-key mismatch (set false ONLY for trusted LAN with rotating IPs)
sftp_folder: /home/alice/claude-mirror/myproject   # absolute path on the server (or relative to the chroot if `internal-sftp` is in use)
token_file: /home/user/.config/claude_mirror/sftp-myproject-token.json
poll_interval: 30                      # seconds between remote-state polls (no native push events on SFTP)
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
# Optional: snapshot retention policy. Each field defaults to 0 (= disabled).
# Union of every selector's keep-set is retained; everything else is pruned
# automatically after each successful `claude-mirror push`.
keep_last:    7      # always keep the 7 newest snapshots
keep_daily:   14     # plus one snapshot per day for the last 14 days
keep_monthly: 12     # plus one snapshot per month for the last 12 months
keep_yearly:  5      # plus one snapshot per year for the last 5 years
```

The password (when used) is stored only in the token file (chmod 0600), never in this YAML. SSH key files are read from disk on every connect; their permissions are your responsibility (OpenSSH refuses to use a key file that's group- or world-readable).

### With Slack notifications

Add the following fields to any of the configs above to post a message to Slack on every push, pull, sync, or delete. Slack notifications are independent of the backend.

```yaml
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T000.../B000.../xxxx
slack_channel: "#claude-mirror"      # optional — overrides the webhook's default channel
```

---

## Slack notifications

Slack notifications are **per-project** and **opt-in**. They are independent of any git/GitHub commit notifications you may have set up — claude-mirror posts on its own sync events (push, pull, sync, delete), not on commits.

### Step 1 — Create the Slack incoming webhook

You need an **Incoming Webhook URL** that points at a specific Slack channel. Each webhook is bound to one channel at creation time; if you want to post to two channels, generate two webhooks.

The walkthrough below assumes you are an admin of the Slack workspace (or have permission to install apps). If you're not, ask your workspace admin to install the app and share the webhook URL with you — it doesn't grant any other permissions.

1. **Open the Slack API page**
   Go to [api.slack.com/apps](https://api.slack.com/apps) → click **Create New App** (top right).

2. **Pick "From scratch"** when prompted.
   - **App Name:** `claude-mirror` (or anything you like — it appears as the message author in Slack).
   - **Pick a workspace:** the Slack workspace that contains the destination channel.
   - Click **Create App**.

3. **Enable Incoming Webhooks**
   - On the app's left sidebar, click **Incoming Webhooks**.
   - Toggle **Activate Incoming Webhooks** to **On**.

4. **Add a webhook to a specific channel**
   - Scroll to the bottom of the same page.
   - Click **Add New Webhook to Workspace**.
   - You'll be redirected to a Slack page asking which channel to post into. Pick the channel (e.g. `#claude-mirror`, `#dev-notes`, or a private channel/group you're a member of) and click **Allow**.
   - For a private channel that isn't in the dropdown: open the channel in Slack first, type `/invite @claude-mirror` (or the app name you chose) to add the app to the channel, then come back to this page and the channel will appear.

5. **Copy the webhook URL**
   You'll be returned to the **Incoming Webhooks** page with the new entry listed under **Webhook URLs for Your Workspace**. The URL looks like:
   ```
   https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx
   ```
   Click **Copy**. Treat this URL like a secret — anyone with it can post to that channel.

6. **(Optional) Customise the app icon and name**
   - On the left sidebar click **Basic Information** → scroll to **Display Information** → upload an icon and pick a colour. Slack will use these as the visual identity of the messages.

> If you ever need to revoke the webhook (e.g. it was leaked), come back to **Incoming Webhooks** in the app's settings and click **Remove** next to the entry. Generate a new one to replace it.

### Step 2 — Enable Slack in claude-mirror

During `claude-mirror init --wizard`, the wizard offers to enable Slack and asks for the webhook URL and an optional channel override. Or pass the flags non-interactively:

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

To enable Slack on an already-initialized project, edit `~/.config/claude_mirror/<project>.yaml` directly:

```yaml
slack_enabled: true
slack_webhook_url: https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx
slack_channel: "#claude-mirror"      # optional — see below
```

To disable later, set `slack_enabled: false`, or re-run `init` with `--no-slack`.

### Step 3 — Verify it works

Trigger any sync event from the project directory:

```bash
echo "test" >> CLAUDE.md
claude-mirror push CLAUDE.md
```

You should see a message in your Slack channel within a second or two. The notification renders as three Slack blocks:

**Header line**
```
🔼 user@machine pushed 1 file in myproject
```

**Files changed**
```
Files changed:
• CLAUDE.md
```

**Context line** (snapshot confirmation + project size)
```
📸 Snapshot: 2026-05-05T10-15-22Z (blobs)  ·  📚 1245 files in project
```

If a push or sync touched files but no snapshot was created (rare — usually because the snapshot creation itself errored), the context line shows `⚠️ No snapshot was created for this event` instead of the snapshot timestamp, so the recovery-point gap is visible.

The file list is capped at 10 entries; longer pushes show `… and N more` after the cap. Clients/notifications that don't render Slack blocks (mobile push previews, IRC bridges) fall back to the compact one-line summary.

If nothing arrives:
- Check the webhook URL is correct (no trailing whitespace, no extra quotes inside the YAML).
- Check `slack_enabled: true` in the YAML (`true` lowercase, not `True`).
- Verify the bot was invited to the channel if it's private.
- Re-test the webhook directly with `curl`:
  ```bash
  curl -X POST -H 'Content-Type: application/json' \
    -d '{"text":"hello from curl"}' \
    'https://hooks.slack.com/services/T01ABCDEF/B01GHIJKL/xxxxxxxxxxxxxxxxxxxxxxxx'
  ```
  If `curl` works but claude-mirror doesn't, it's almost certainly a YAML quoting issue — wrap the URL in `"…"`.

### About `slack_channel` (channel override)

The webhook is permanently bound to the channel you picked at step 4. The `slack_channel` config field can override it ONLY if your workspace allows webhook channel-overrides (a per-workspace setting, off by default for new workspaces since 2018). If you set `slack_channel` and posts still land in the original channel, the override is being ignored — generate a new webhook for the new channel instead. In short: **the webhook is the source of truth for the destination channel; the `slack_channel` field is a best-effort override.**

### Multiple Slack channels for the same project

To post the same events to two different channels, the simplest approach is two separate Slack apps (or two webhooks under one app), then configure two project YAMLs that point at the same `project_path` but with different webhook URLs. Or set up a Slack workflow that re-broadcasts.

### Different Slack channels for different projects

Each project YAML carries its own `slack_webhook_url`, so two projects on the same machine can post to entirely different channels (or different Slack workspaces). Just generate one webhook per project.

### Config fields

| Field | Type | Purpose |
|---|---|---|
| `slack_enabled` | bool | Master switch. `false` (default) disables all Slack posts. |
| `slack_webhook_url` | str | Incoming-webhook URL from Slack's Apps directory. |
| `slack_channel` | str (optional) | Override the channel the webhook posts to. Omit to use the webhook's default. |

### What gets posted

A Rich-formatted message with an action label and the list of affected files, on every successful `push`, `pull`, `sync`, or `delete`. The message includes the user, machine name, and project name so the same Slack channel can serve multiple projects.

### Reliability

- Best-effort: a Slack failure (network error, 4xx, 5xx, malformed webhook URL) is logged and silently swallowed. It will **never** block or fail a sync.
- No extra dependency: Slack posting uses Python's standard-library `urllib`, so the base `pipx install claude-mirror` is enough.

---

## Desktop notifications

Run the built-in test command to verify notifications are working and see platform-specific setup instructions:

```bash
claude-mirror test-notify
```

This sends a test notification and prints step-by-step permission instructions for your OS.

### macOS

Notifications use `osascript display notification`. macOS requires the calling application to have notification permission granted explicitly — no prompt appears automatically.

**Steps:**

1. Run `claude-mirror test-notify` from Terminal (or iTerm2)
2. Open **System Settings → Notifications**
3. Scroll down and find **Terminal** (or iTerm2)
   - If it is not listed yet, the test above should have triggered its first appearance — scroll again
4. Enable **Allow Notifications**
5. Set alert style to **Alerts** or **Banners** (not Off)

**Running as a launchd service:**

When the watcher runs as a launchd agent it has no app bundle, so the system cannot create a notification permission entry for it. Workaround: run `claude-mirror watch` once from a regular Terminal window, grant permission to Terminal in System Settings, then switch back to the launchd service. The notification will be delivered on behalf of Terminal.

### Linux

Notifications use `notify-send` (libnotify). Install it if missing:

```bash
# Debian / Ubuntu
sudo apt install libnotify-bin

# Fedora
sudo dnf install libnotify
```

A notification daemon must be running — most desktop environments (GNOME, KDE, XFCE) include one automatically.

**Running as a systemd service:** if the service has no access to the display session, add these to the service unit:

```ini
[Service]
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
```

Replace `1000` with your user ID (`id -u`).

---

## Update notifications

claude-mirror checks once per 24h whether a newer version exists on this project's GitHub mirror and tells you about it. The check is best-effort, offline-tolerant, and never blocks a command — it runs in a background daemon thread and only ever prints a single inline notice when the cached "latest version" is newer than the locally-installed one.

### What you'll see

On any command launch when an update is available:

```
🆕 claude-mirror 0.4.1 is available (you have 0.4.0).
Update: pipx install -e . --force from your repo dir, or set CLAUDE_MIRROR_NO_UPDATE_CHECK=1 to silence.
```

Inside the long-running watcher daemon, the same event also fires a desktop notification — but only ONCE per new version (tracked in cache, so re-launching the daemon repeatedly doesn't spam). Restarting the daemon after updating clears the "already notified" record automatically (because the local version now matches the cached latest).

### Manual check

```bash
claude-mirror check-update
```

Bypasses the 24h cache and queries GitHub immediately. Sample output:

```
Current version: 0.4.0
Fetching latest from GitHub…
Latest on GitHub: 0.4.1

🆕 Update available: 0.4.0 → 0.4.1
Update with: cd '/Users/you/claude-mirror' && git pull && pipx install -e . --force
```

Three exit branches:
- **Up to date** → green ✓ message, exit 0
- **Update available** → yellow notice, exit 0
- **Local ahead of GitHub** (you're developing claude-mirror itself) → blue ℹ message, exit 0
- **Network failure** → yellow "could not reach GitHub" message, exit 1

### One-shot upgrade

```bash
claude-mirror update            # dry-run: shows current → latest and the command that would run
claude-mirror update --apply    # actually runs git pull + pipx install -e . --force, with confirmation
claude-mirror update --apply --yes   # skip the confirmation prompt (cron / CI)
```

`update` auto-detects whether your install was made editable (the typical case for this project) or not. For editable installs it does:

1. `cd <auto-detected-repo-path>`
2. `git pull`  (fails clean if you have uncommitted local changes)
3. `pipx install -e . --force` (rebuilds the pipx venv with the new pyproject.toml)

For non-editable installs (e.g. `pipx install git+https://github.com/...`), it falls back to `pipx upgrade claude-mirror`.

When a `claude-mirror watch-all` daemon is running on the same machine, `update` warns you (with PIDs) that the daemon will keep the OLD code in memory until restarted. It does **not** auto-kill the daemon — you decide when to bounce it (`kill <pid>` then re-launch via your launchd / systemd service or `claude-mirror watch-all`).

Exit codes:
- 0 — update succeeded, or already up-to-date, or dry-run completed
- non-zero — `git pull` rejected (uncommitted changes), `pipx install` failed, network failure, or install path couldn't be auto-detected

### Cache and opt-out

| Item | Default | How to change |
|---|---|---|
| Cache location | `~/.config/claude_mirror/.update_check.json` | Currently hardcoded; the file is pure cache and safe to delete |
| TTL | 24h | Hardcoded — `claude-mirror check-update` bypasses |
| Opt out (per-shell) | (off) | `export CLAUDE_MIRROR_NO_UPDATE_CHECK=1` |
| Opt out (permanent) | (off) | Add the export to your `.zshrc` / `.bashrc` |

The check fetches the canonical `pyproject.toml` from `https://raw.githubusercontent.com/alessiobravi/claude-mirror/main/pyproject.toml`. Only the `version` line is parsed. The HTTP request includes a `User-Agent: claude-mirror/<version> update-check` header so the maintainer can correlate version-update lag with installed-base drift via standard server logs — no telemetry data is sent.

---

## Troubleshooting

### `RefreshError: Reauthentication is needed` (Google Drive)

**Symptom:** Any command fails with a traceback ending in:

```
google.auth.exceptions.RefreshError: Reauthentication is needed.
Please run `gcloud auth application-default login` to reauthenticate.
```

**Cause:** The OAuth refresh token has expired or been revoked by Google (this happens after extended inactivity or if access was revoked in the Google account settings). The error message mentioning `gcloud` is misleading — claude-mirror does not use gcloud credentials.

**Fix:** Re-run the auth command for your project config:

```bash
claude-mirror auth --config ~/.config/claude_mirror/default.yaml
```

Or for a named project config:

```bash
claude-mirror auth --config ~/.config/claude_mirror/<project>.yaml
```

This will open a browser window for a fresh OAuth login. The stale token is cleared automatically and replaced with a new one. All other config (project path, Drive folder, Pub/Sub topic) is preserved.

---

### `Not authenticated. Run claude-mirror auth first.`

**Cause:** No token file exists — either `auth` was never run on this machine, or the token file was deleted. Applies to all backends.

**Fix:** Run `claude-mirror auth`.

---

### Authentication expires every day or two (Google Drive)

**Symptom:** You are prompted to re-authenticate every ~24 hours even though nothing changed.

**First step — diagnose:**

```bash
claude-mirror auth --check
```

This non-destructive command inspects the saved token file, attempts a refresh, and reports whether the failure is local (refresh_token revoked, network blip, clock skew) or organisational. Set `CLAUDE_MIRROR_AUTH_VERBOSE=1` for extra detail.

**Most common causes (in order of likelihood):**

1. **The OAuth consent screen is in `Testing` mode (External user type).** Google enforces a 7-day refresh-token lifetime in this mode — and as of 2024+ has been tightening this aggressively for sensitive scopes like `drive`. Many users effectively see ~24h.

   **Fix:** [Google Cloud Console → APIs & Services → OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent) → click **PUBLISH APP** → **CONFIRM**. Do **not** click "Submit for verification" — you don't need it for personal use. Next time you run `claude-mirror auth`, click past the "Google hasn't verified this app" warning. Refresh tokens are then permanent.

2. **Workspace admin has set Google Cloud Session Control reauth interval.** Even with an `Internal` consent screen and a published app, a Workspace admin can force every refresh token in the org to die on a fixed interval.

   **Check:** [Admin Console → Security → Authentication → Google Cloud session control](https://admin.google.com/ac/security/cloud-session-controls). This is **separate** from "Web session control" — Cloud session control specifically governs OAuth tokens for Google Cloud APIs (including Drive). If "Reauthentication policy" is anything other than "Never", that's the cause.

   **Fix:** Ask the admin to set "Reauthentication policy" → "Never expire", or to whitelist the OAuth client under [API controls → Manage Third-party app access](https://admin.google.com/ac/owl).

3. **The OAuth client lives in a GCP project that isn't in your Workspace organisation.** "Internal" only takes effect when the project is owned by the same Workspace org as the user. Otherwise Google quietly treats the auth as External.

   **Check:** [Cloud Console → IAM & Admin → Settings](https://console.cloud.google.com/iam-admin/settings) — the **Organisation** field should match your Workspace domain.

4. **Refresh token genuinely revoked** — `claude-mirror auth` was run on too many machines (>50 active refresh tokens per OAuth client × user account), or a user manually revoked access at [myaccount.google.com → Security → Third-party apps](https://myaccount.google.com/permissions).

   **Fix:** Run `claude-mirror auth` once more. Avoid re-running it on machines where it's already working — `prompt=consent` forces a new refresh_token every time and pushes the oldest out of the cap.

5. **Clock skew > 3 minutes from real UTC.** Google rejects refresh requests with skewed JWT signatures.

   **Check:** `date -u` — compare to a real UTC clock. **Fix:** `sudo sntp -sS time.apple.com` (macOS) or your distro's NTP equivalent.

**What's already implemented to make refresh robust:**

- Proactive refresh: tokens are refreshed when they have less than 5 minutes left, not after expiry. Avoids the situation where many parallel API calls all hit 401 simultaneously and race.
- Retry with exponential backoff: a transient transport error triggers up to 3 retries (~2.4s total) before surfacing the failure.
- Distinguishes `invalid_grant` (refresh token genuinely dead — re-auth required) from transient errors (transport, 5xx, rate-limit — try again).
- `CLAUDE_MIRROR_AUTH_VERBOSE=1` env var logs every refresh attempt to stderr so you can confirm refresh is actually firing.

---

### Dropbox: "Authentication code expired" or "invalid_grant"

**Cause:** The one-shot authorization code printed during `claude-mirror auth` has a short lifetime; if you wait too long before pasting it back, or if the same code is reused, Dropbox rejects it.

**Fix:** Re-run `claude-mirror auth`, complete the flow promptly, and paste the code on first try.

---

### OneDrive: `AADSTS50058` or "no cached accounts found"

**Symptom:** OneDrive operations fail with a Microsoft authentication error such as `AADSTS50058: A silent sign-in request was sent but no user is signed in` or "no cached accounts were found in the token cache".

**Cause:** The MSAL token cache for this project is missing, corrupted, or its refresh token has been revoked.

**Fix:** Re-run `claude-mirror auth`. The device-code flow prints a short code and a URL — open the URL in any browser, paste the code, sign in with the Microsoft account that has access to the OneDrive folder, and the cache is rebuilt.

---

### WebDAV: `401 Unauthorized`

**Cause:** Wrong username, wrong password, or — for servers like Nextcloud and OwnCloud that require app passwords when 2FA is enabled — a real account password where an app password is required.

**Fix:** Confirm the username, generate an app password in the server's UI, and re-run `claude-mirror init` (or edit `webdav_username` in the config + re-run `claude-mirror auth` to refresh the token file).

---

### WebDAV: `405 Method Not Allowed` or `409 Conflict` from `MKCOL`

**Cause:** The server refused to create the project folder. This usually means the parent path does not exist, or the server's WebDAV implementation does not allow creating folders at the configured path (some shared-hosting WebDAV setups disable `MKCOL` entirely).

**Fix:** Create the project folder manually via the server's web UI or another WebDAV client, then re-run `claude-mirror push`. The folder only needs to be created once.

---

## File locations

All token files are written with `chmod 0600` (owner read/write only).

| File | Purpose |
|---|---|
| `~/.config/claude_mirror/<account>-credentials.json` | OAuth2 client credentials for one Google account (share with team) |
| `~/.config/claude_mirror/<account>-token.json` | Your personal access token for Google Drive (do not share) |
| `~/.config/claude_mirror/dropbox-<project>-token.json` | Your personal Dropbox refresh token (do not share) |
| `~/.config/claude_mirror/onedrive-<project>-token.json` | MSAL token cache for OneDrive (do not share) |
| `~/.config/claude_mirror/webdav-<project>-token.json` | WebDAV credentials — URL, username, and password in plaintext at `0600`. Prefer an app password. |
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

- **Configs without a `backend` field** (any project YAML created before multi-backend support) are loaded as `backend: googledrive`. No edit required; the field is filled in automatically the next time the config is written.
- **Manifests with legacy `drive_file_id` keys** are still understood. `Manifest.load` accepts both the legacy `drive_file_id` and the current `remote_file_id`, so existing projects keep their full sync state across the upgrade.

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

claude-mirror is free software released under the **GNU General Public License, version 3 or later** (GPL-3.0-or-later). The full text is in [LICENSE](./LICENSE).

In short:

- You may **use, modify, and redistribute** this software, including in commercial settings.
- If you distribute a modified version (or any work that incorporates claude-mirror's source), you must release your changes under the same GPL-3.0-or-later license and make the corresponding source available to recipients.
- claude-mirror comes with **NO WARRANTY**, to the extent permitted by applicable law (see the Disclaimer above and Sections 15–17 of the GPL).

For the formal terms, see [LICENSE](./LICENSE) or [gnu.org/licenses/gpl-3.0](https://www.gnu.org/licenses/gpl-3.0.html).
