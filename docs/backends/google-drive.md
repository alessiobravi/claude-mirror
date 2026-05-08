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

## Wizard improvements as of v0.5.46

`claude-mirror init --wizard --backend googledrive` now front-loads everything that used to fail at first sync:

- **Auto-open Cloud Console pages.** After you type the GCP project ID, the wizard offers to open three project-scoped pages in your default browser: enable Drive API, enable Pub/Sub API, create OAuth client. Default Yes; on No (or on a headless / SSH session where `webbrowser.open` cannot launch a browser) the wizard prints the URLs for copy-paste instead.
- **Inline input validation.** The GCP project ID, Drive folder ID, Pub/Sub topic ID, and credentials JSON path are checked at the prompt rather than at first sync. Common mistakes get a specific error and the prompt re-asks:
  - Pasting the whole `https://drive.google.com/drive/folders/<FOLDER_ID>` URL into the folder-ID prompt is rejected with a hint to copy only the segment after `/folders/`.
  - Selecting a service-account key JSON instead of an OAuth Desktop client JSON is rejected with a hint pointing at the Cloud Console -> APIs & Services -> Credentials -> Create Credentials -> OAuth client ID -> Application type: Desktop app flow.
  - GCP project IDs that violate the 6-30-char / lowercase-letter-start rule are rejected up front with a link to the canonical Google docs.
- **Post-auth smoke test.** After OAuth completes (and BEFORE the YAML is written), the wizard runs a single `drive.files.list(pageSize=1, q="<folder_id>" in parents)` call. This catches:
  - Drive API not enabled in the GCP project (often: the credentials.json was for a project where it isn't enabled).
  - Folder ID typos (file not found in the Drive accessible to the authenticated account).
  - Folder not shared with the authenticating Google account.
  
  Failure prints a classified reason and asks whether to retry the auth flow. Decline retry and the YAML is still saved with a yellow warning, so you can fix the underlying issue (e.g. share the folder, enable the API) and run `claude-mirror auth` later.

If you prefer to script the wizard or are configuring offline, all three behaviours are skippable: decline the auto-open prompt, decline the smoke-test prompt, and the wizard collapses back to the pre-v0.5.46 question-only flow.

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
