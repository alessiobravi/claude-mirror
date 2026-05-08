← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# Dropbox backend

Dropbox uses HTTP long-polling for change notifications and OAuth2 with PKCE (no client secret) for authentication. Pick it when collaborators already have Dropbox accounts and you want a low-friction setup with no Google Cloud project to manage.

## Setup walkthrough

### Step 1: Create a Dropbox app

1. Go to [dropbox.com/developers/apps](https://www.dropbox.com/developers/apps)
2. Click **Create app**
3. Choose:
   - **Scoped access**
   - **Full Dropbox** (or App folder if you prefer isolation)
   - App name: e.g. `claude-mirror`
4. Click **Create app**

### Step 2: Configure permissions

On the app's **Permissions** tab:

1. Enable:
   - `files.content.read`
   - `files.content.write`
2. Click **Submit** at the bottom

### Step 3: Note your app key

On the app's **Settings** tab, copy the **App key**. You will need it during `claude-mirror init`.

No client secret is needed — claude-mirror uses OAuth2 with PKCE.

### Step 4: Share with collaborators

Share the Dropbox folder (e.g. `/claude-mirror/myproject`) with collaborators via Dropbox's normal sharing. Each collaborator creates their own Dropbox app (Step 1) or you share the same app key.

## Config file

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

## Daily ops notes

- **Push notifications** — Dropbox `files/list_folder/longpoll` HTTP long-polling. Typical latency from a collaborator's `push` to your `watch` notification is a few seconds.
- **Authentication** — `claude-mirror auth` prints an authorization URL. Visit it in your browser, authorize the app, and paste the authorization code back into the terminal. The refresh token is saved for silent refresh on subsequent runs.
- **Server-side snapshot copy** — `full`-format snapshots use `files/copy_v2`; no file data passes through the client.
- **Auth code expiry** — paste the authorization code promptly; Dropbox auth codes are very short-lived. See [troubleshooting](../../README.md#dropbox-authentication-code-expired-or-invalid_grant).

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
