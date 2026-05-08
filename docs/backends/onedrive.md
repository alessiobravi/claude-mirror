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

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
