← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# Google Drive backend

Google Drive is the original claude-mirror backend. Pick it when you want sub-second push notifications between collaborators (via Cloud Pub/Sub), free 15 GB of storage per Google account, and you're comfortable creating a Google Cloud project once.

## Setup walkthrough

### Step 1: Create a Google Cloud project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click the project selector at the top → **New Project**
3. Name it (e.g. `claude-mirror`) and click **Create**
4. Note your **Project ID** — you will need it later

### Step 2: Enable APIs

In your new project:

1. Go to **APIs & Services** → **Library**
2. Search for and enable:
   - **Google Drive API**
   - **Cloud Pub/Sub API**

### Step 3: Create OAuth 2.0 credentials

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

### Step 4: Create a Pub/Sub topic

1. Go to **Pub/Sub** → **Topics**
2. Click **Create Topic**
3. Topic ID: choose a name that identifies your project, e.g. `claude-mirror-myproject`
4. Leave **Add a default subscription** unchecked (claude-mirror creates per-machine subscriptions automatically)
5. Click **Create**

### Step 5: Grant collaborator access

For each collaborator's Google account:

1. Go to **IAM & Admin** → **IAM**
2. Click **Grant Access**
3. Enter their Google account email
4. Assign this role:
   - **Pub/Sub Editor** (to publish and subscribe)

> **Note:** no Drive-specific IAM role is needed — Drive access is managed by sharing the folder directly (see Step 6).

For Drive folder access: share the Drive folder directly with each collaborator's Google account (see Step 6).

### Step 6: Create the shared Drive folder

1. Go to [drive.google.com](https://drive.google.com)
2. Create a new folder (e.g. `claude-mirror-myproject`)
3. Open the folder — the URL will look like:
   ```
   https://drive.google.com/drive/folders/1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OhBlt
   ```
4. Copy the folder ID — the long string after `/folders/`
5. Share the folder with each collaborator's Google account (Editor access)

## Config file

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

## Daily ops notes

- **Push notifications** — Cloud Pub/Sub streaming pull (persistent gRPC connection). Typical end-to-end latency from a collaborator's `push` to your `watch` notification is sub-second.
- **Authentication** — `claude-mirror auth` opens a browser window. After login, the Pub/Sub topic and per-machine subscription are verified and created if needed.
- **Token refresh** — Google OAuth refresh tokens are issued for the lifetime of the consent. If the project is in "Testing" status on the OAuth consent screen, tokens expire after ~7 days; publish the app to "In Production" (no review required for internal use) to get long-lived refresh tokens. See [troubleshooting](../../README.md#authentication-expires-every-day-or-two-google-drive).
- **Server-side snapshot copy** — `full`-format snapshots use the `files.copy` API; no file data passes through the client.
- **Storage cost** — counts against the **Google account's** 15 GB free quota (or whatever the account has).

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
