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

## Diagnosing setup problems

Once a Dropbox backend is configured (or you suspect it's misconfigured), run:

```bash
claude-mirror doctor --backend dropbox
```

This runs the generic credentials/token/connectivity checks AND six Dropbox-specific deep checks:

1. Token JSON shape — `access_token` (legacy long-lived) or `refresh_token` (PKCE) must be present in the token file. Both shapes work; PKCE refresh-token is the modern path.
2. App-key sanity — `dropbox_app_key` in the YAML is non-empty and matches `^[a-z0-9]{10,20}$` (Dropbox app keys are short alphanumeric strings, e.g. `uao2pmhc0xgg2xj`). Catches typos and pasted-with-extra-chars values before the SDK does.
3. Account smoke test — `users_get_current_account()` returns a `FullAccount` with a populated `account_id`. This is the first network call after auth — it surfaces revoked tokens cleanly with a clear AuthError rather than a confusing folder-listing failure later.
4. Granted scopes inspection — for PKCE tokens (which carry a `scope` field), the configured operations `files.content.read` and `files.content.write` must both appear. Missing scopes surface the exact name and point at the Dropbox app's Permissions tab. Legacy tokens (no `scope` field) emit a yellow info line and skip this check — the token implicitly grants whatever was approved when it was issued.
5. Folder access — `files_list_folder(path=dropbox_folder, limit=1)`. Catches: folder doesn't exist, permission denied, team-folder access not granted. Each maps to a specific fix-hint.
6. Account type / team status — the account is classified as `personal` / `pro` / `business`, and team-membership is reported separately. **If you're a team member, the deep check emits a yellow info line about admin policies.** Team admins can disable third-party app access at the team level, which silently breaks sync — if you start seeing unexpected auth failures on a team account, that's the first thing to ask your admin to confirm.

Sample successful output:

```
$ claude-mirror doctor --backend dropbox
claude-mirror doctor — /home/alice/.config/claude_mirror/dropbox-myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/dropbox-myproject.yaml

── checking dropbox backend (/home/alice/.config/claude_mirror/dropbox-myproject.yaml)
  ✓ credentials file exists: /home/alice/.config/claude_mirror/dropbox-credentials.json
  ✓ token file present with refresh_token: /home/alice/.config/claude_mirror/dropbox-myproject-token.json
  ✓ backend connectivity ok (list_folders on root succeeded)
  ✓ Token JSON valid; refresh_token present
  ✓ App key format valid: uao2pmhc0xgg2xj
  ✓ Account: alice@example.com (account_id: dbid:AAH123456)
  ✓ Scopes: files.content.read, files.content.write
  ✓ Folder accessible: /claude-mirror/myproject
  ✓ Account type: personal
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✓ All checks passed.
```

Sample failure on the missing-folder case (the most common one when collaborators set up a new project):

```
  ✗ Folder not found in Dropbox: /claude-mirror/myproject
      Fix: create /claude-mirror/myproject in your Dropbox account (web UI or Dropbox client) and re-run claude-mirror doctor --backend dropbox --config /home/alice/.config/claude_mirror/dropbox-myproject.yaml.
```

If multiple deep checks fail with the same auth error (e.g. an expired or revoked token), doctor emits ONE bucketed `Dropbox auth failed` line and skips the remaining checks, so you don't get three identical "re-run claude-mirror auth" lines for the same root cause. The fix is `claude-mirror auth --config PATH`.

See [admin.md#dropbox-deep-checks](../admin.md#dropbox-deep-checks) for the full deep-check reference table and the auth-bucketing semantics.

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
