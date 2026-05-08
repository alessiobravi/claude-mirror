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

### Auto-create Pub/Sub topic + subscription + IAM grant (`--auto-pubsub-setup`, since v0.5.47)

Pass `--auto-pubsub-setup` on `claude-mirror init` and the wizard will, after the smoke test passes, idempotently create three Pub/Sub resources using the OAuth credentials you just acquired:

1. The Pub/Sub **topic** at `projects/<gcp_project_id>/topics/<pubsub_topic_id>`.
2. A **per-machine subscription** named `<pubsub_topic_id>-<machine_safe>` (the canonical pattern from `Config.subscription_id` — `machine_safe` lower-cases the hostname and rewrites dots and spaces to dashes).
3. The **IAM grant** that lets Drive's push-notification service account (`apps-storage-noreply@google.com`) publish change events to your topic — `roles/pubsub.publisher`. This is the highest-value piece of the flag: about 70% of self-serve Drive setups silently miss this grant, see Pub/Sub appearing to work locally, and never receive notifications from collaborators.

Most users want this flag. It eliminates the entire "why aren't notifications arriving" failure mode without requiring the user to click through the GCP console for the IAM binding.

Sample output on a fresh project:

```
Pub/Sub auto-setup:
  ✓ Topic created                       projects/myproject-prod/topics/claude-mirror-myproject
  ✓ Subscription created for laptop     claude-mirror-myproject-laptop
  ✓ IAM grant added                     apps-storage-noreply@google.com -> roles/pubsub.publisher
```

Sample output when re-running on the same machine (idempotent — every step short-circuits cleanly):

```
Pub/Sub auto-setup:
  ✓ Topic exists                        projects/myproject-prod/topics/claude-mirror-myproject
  ✓ Subscription exists for laptop      claude-mirror-myproject-laptop
  ✓ IAM grant already present           apps-storage-noreply@google.com -> roles/pubsub.publisher
```

Requirements and edge cases:

- **The Pub/Sub OAuth scope must have been granted** at auth time (the Google sign-in screen lists Drive AND Pub/Sub — leave both checked). If the scope is missing, the helper prints one yellow info line (`Pub/Sub scope not granted; re-run claude-mirror auth with the Pub/Sub scope to enable auto-setup.`) and skips. The YAML still writes; the user can re-run `claude-mirror auth` to add the scope and then `claude-mirror init --auto-pubsub-setup --config <path>` to land the resources.
- **Failures don't abort the wizard.** If the OAuth credentials lack `pubsub.topics.create` (the user is on a GCP project they don't own), each step records its own line in `result.failures`; the wizard prints those as yellow warnings, but the YAML still writes. The user can either fix the underlying cause and re-run, or finish the missing step in the GCP console and verify with `claude-mirror doctor --backend googledrive`.
- **Silent on non-Drive backends.** Passing `--auto-pubsub-setup` on `--backend dropbox` (or any other) is a no-op — `init` walks every backend through the same flag list and the flag only takes effect on Drive.
- **Off by default.** Existing scripts and CI invocations that don't pass the flag see the v0.5.46 behaviour unchanged.

## Daily ops notes

- **Push notifications** — Cloud Pub/Sub streaming pull (persistent gRPC connection). Typical end-to-end latency from a collaborator's `push` to your `watch` notification is sub-second.
- **Authentication** — `claude-mirror auth` opens a browser window. After login, the Pub/Sub topic and per-machine subscription are verified and created if needed.
- **Token refresh** — Google OAuth refresh tokens are issued for the lifetime of the consent. If the project is in "Testing" status on the OAuth consent screen, tokens expire after ~7 days; publish the app to "In Production" (no review required for internal use) to get long-lived refresh tokens. See [troubleshooting](../../README.md#authentication-expires-every-day-or-two-google-drive).
- **Server-side snapshot copy** — `full`-format snapshots use the `files.copy` API; no file data passes through the client.
- **Storage cost** — counts against the **Google account's** 15 GB free quota (or whatever the account has).

## Diagnosing setup problems

Once a Drive backend is configured (or you suspect it's misconfigured), run:

```bash
claude-mirror doctor --backend googledrive
```

This runs the generic credentials/token/connectivity checks AND six Drive-specific deep checks:

1. OAuth scope inventory — both `https://www.googleapis.com/auth/drive` and `https://www.googleapis.com/auth/pubsub` must be on the saved token. Drive scope is required; Pub/Sub scope is optional but needed for real-time notifications.
2. Drive API enabled in the GCP project — parsed from Google's canonical "API has not been used in project X" error string. The fix URL is templated with your project ID.
3. Pub/Sub API enabled — same error-string parsing as Drive.
4. Pub/Sub topic exists at `projects/PROJECT/topics/TOPIC` — `gcp_project_id` and `pubsub_topic_id` from the YAML.
5. Per-machine subscription exists at `projects/PROJECT/subscriptions/TOPIC-MACHINE` — the `MACHINE` suffix is the value of `machine_name` in the YAML, lower-cased and dot/space-normalised to dashes.
6. IAM grant: Drive's service account (`apps-storage-noreply@google.com`) has `roles/pubsub.publisher` on the topic. **This is the highest-value check** — about 70% of self-serve Drive setups miss this grant. Pub/Sub appears to work (subscribe + publish from your own credentials succeeds), but Drive itself silently fails to publish change events, so other machines never receive notifications.

Sample successful output:

```
$ claude-mirror doctor --backend googledrive
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

✓ All checks passed.
```

Sample failure on the missing-IAM-grant case (the most common one):

```
  ✗ Drive service account missing publish permission on the topic
      Push events from THIS machine won't notify others.
      Fix: run claude-mirror init --reconfigure-pubsub --config /home/alice/.config/claude_mirror/myproject.yaml, or grant roles/pubsub.publisher to serviceAccount:apps-storage-noreply@google.com on topic projects/myproject-prod/topics/claude-mirror-myproject in the Cloud Console.
```

If multiple Pub/Sub admin calls fail with the same auth error (e.g. an expired token), doctor emits ONE bucketed `Pub/Sub admin auth failed` line and skips the remaining Pub/Sub checks, so you don't get five identical "re-run claude-mirror auth" lines for the same root cause. The fix is `claude-mirror auth --config PATH`.

If you're using Drive without Pub/Sub real-time notifications (a valid degraded mode — `gcp_project_id` and `pubsub_topic_id` empty in the YAML), the deep section emits one yellow info line and skips. The generic checks still run; everything works for `push` / `pull` / `sync`, you just miss the sub-second push notifications.

See [admin.md#drive-deep-checks](../admin.md#drive-deep-checks) for the full deep-check reference table and the auth-bucketing semantics.

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
