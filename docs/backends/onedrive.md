← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# Microsoft OneDrive backend

OneDrive uses periodic polling for change notifications and a device-code OAuth flow for authentication (works on headless machines). Pick it when collaborators already have Microsoft 365 / personal Microsoft accounts and you want a single Azure app registration to cover everyone.

## Setup walkthrough

### Step 1: Register an Azure AD app

1. Go to [portal.azure.com](https://portal.azure.com) → **Azure Active Directory** → **App registrations** → **New registration**
2. Name: `claude-mirror` (or anything you like)
3. Supported account types: **Personal Microsoft accounts only** (or "Accounts in any organizational directory and personal Microsoft accounts" for mixed use)
4. Click **Register**
5. From the overview page, copy the **Application (client) ID** — you will need it during `claude-mirror init`

### Step 2: Configure platform and permissions

1. Go to **Authentication** → **Add a platform** → **Mobile and desktop applications**
2. Add the redirect URI: `https://login.microsoftonline.com/common/oauth2/nativeclient`
3. Save
4. Go to **API permissions** → **Add a permission** → **Microsoft Graph** → **Delegated permissions**
5. Add: `Files.ReadWrite` and `offline_access`
6. Click **Grant admin consent** (only relevant if you're using an organizational tenant)

> No client secret is needed — claude-mirror uses the device-code OAuth flow, which works on any machine including headless ones.

### Step 3: Decide on the OneDrive folder

Pick a path inside your OneDrive where the project will live, e.g. `claude-mirror/myproject`. The folder will be created on first sync if it doesn't exist.

### Step 4: Share with collaborators

For each collaborator, share the OneDrive folder via OneDrive's normal sharing UI (Editor permission). Each collaborator uses the same Azure app's client ID — there's no per-user secret involved.

## Config file

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

## Daily ops notes

- **Push notifications** — periodic polling, configurable `poll_interval` (default 30s). Typical latency from a collaborator's `push` to your `watch` notification is up to `poll_interval` seconds.
- **Authentication** — `claude-mirror auth` runs a device-code login flow: claude-mirror prints a short code and a URL. Open the URL in any browser, paste the code, and sign in with the Microsoft account that has access to the OneDrive folder. The token cache is saved for silent refresh.
- **Server-side snapshot copy** — `full`-format snapshots use the OneDrive async copy API with monitor polling; no file data passes through the client.
- **Auth troubleshooting** — see [`AADSTS50058` / "no cached accounts found"](../../README.md#onedrive-aadsts50058-or-no-cached-accounts-found).

## Diagnosing setup problems

Once a OneDrive backend is configured (or you suspect it's misconfigured), run:

```bash
claude-mirror doctor --backend onedrive
```

This runs the generic credentials/token/connectivity checks AND a series of OneDrive-specific deep checks:

1. Token cache integrity — the MSAL token cache (a JSON blob inside `token_file`) deserializes cleanly and contains at least one cached account.
2. Azure `client_id` format — the configured `onedrive_client_id` must be a GUID (`xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx`); doctor surfaces a malformed value here BEFORE attempting any MSAL operation, since MSAL's "invalid client" error is far less actionable.
3. Granted scopes — the cached account holds at least one of `Files.ReadWrite` (default) or `Files.ReadWrite.All` (shared OneDrive Business). Missing scopes ⇒ yellow warning.
4. Token still refreshable — `acquire_token_silent` against the cached account; None / `error` dict / raise ⇒ AUTH bucket fail with the canonical `claude-mirror auth` fix.
5. Drive item access — Microsoft Graph GET against `me/drive/root:/{onedrive_folder}`. 200 ⇒ folder exists. 404 ⇒ "create the folder via the OneDrive web UI, or run `claude-mirror push` to create it on first sync". 401 ⇒ AUTH bucket. 5xx ⇒ TRANSIENT (suggest retry; point at `status.office.com`).
6. Drive item type — confirms Graph returned a folder shape (not a file). Per-file `quickXorHash` detection happens at sync time, so this check stops at folder access.

Sample successful output:

```
$ claude-mirror doctor --backend onedrive
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking onedrive backend (/home/alice/.config/claude_mirror/myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/onedrive-myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)

OneDrive deep checks
  ✓ Token cache valid; 1 cached account
  ✓ Azure client_id format valid
  ✓ Scopes: Files.ReadWrite
  ✓ Token refreshable; access_token acquired
  ✓ OneDrive folder accessible: /claude-mirror/myproject
  ✓ Drive item type: folder
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✓ All checks passed.
```

Sample failure on the missing-folder case (the most common one):

```
  ✗ Drive item access: HTTP 404
      OneDrive folder doesn't exist at the configured path: /claude-mirror/myproject
      Fix: create /claude-mirror/myproject in the OneDrive web UI, or run claude-mirror push --config /home/alice/.config/claude_mirror/myproject.yaml which will create the folder on first sync.
```

If the silent token refresh fails (expired refresh token) or the Graph endpoint returns 401, doctor emits ONE bucketed `OneDrive auth failed` line and skips the remaining checks, so you don't get duplicate "re-run claude-mirror auth" lines for the same root cause. The fix is `claude-mirror auth --config PATH`.

If `onedrive_folder` is empty in the YAML, the deep section emits one yellow info line and skips. The generic checks still run; the user is presumably mid-wizard and will fill in the folder before attempting `push`.

See [admin.md#onedrive-deep-checks](../admin.md#onedrive-deep-checks) for the full deep-check reference table and the auth-bucketing semantics.

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
