← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# WebDAV backend (Nextcloud, OwnCloud, NAS, etc.)

WebDAV is the self-hosted option. Any RFC 4918 server works — Nextcloud, OwnCloud, Apache `mod_dav`, Synology, Box.com, FastMail, etc. Pick it when you want full control over where the files live and don't need cloud-hosted notifications.

## Setup walkthrough

WebDAV requires no cloud account or app registration — any RFC 4918 server works.

### Step 1: Identify the WebDAV URL

Examples:
- **Nextcloud** — `https://my-server.com/remote.php/dav/files/<username>/claude-mirror/`
- **OwnCloud** — `https://my-server.com/remote.php/webdav/claude-mirror/`
- **Apache `mod_dav`** — whatever URL the admin configured
- **Synology** — `https://<nas-host>:5006/<webdav-share>/claude-mirror/` (after enabling WebDAV in DSM Control Panel → File Services)
- **Box.com** — `https://dav.box.com/dav/claude-mirror/`

### Step 2: Generate an app password

For services that support app passwords (Nextcloud, OwnCloud, FastMail, etc.), generate one specifically for claude-mirror rather than using your account password. The app password will be stored in `~/.config/claude_mirror/<project>-token.json` (chmod 0600).

### Step 3: Pick the project folder

Decide on a folder name (e.g. `claude-mirror-myproject`) and create it on the WebDAV server (via the web UI or via `mkdir` over WebDAV — claude-mirror will also create it on first push if needed).

### Step 4: Share with collaborators

Use the WebDAV server's native share/permissions UI to grant each collaborator read+write access to the project folder.

## Config file

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

## Daily ops notes

- **Push notifications** — periodic polling, configurable `poll_interval` (default 30s). Typical latency from a collaborator's `push` to your `watch` notification is up to `poll_interval` seconds.
- **Authentication** — no interactive browser flow. The URL, username, and password you provided at `init` are validated against the server (a `PROPFIND` on the project folder) and written to the token file with `chmod 0600`.
- **Password storage** — the WebDAV token additionally stores the password in plaintext inside the `0600` file — for that reason, prefer an app password over your real account password whenever your server supports them.
- **HTTPS by default** — `claude-mirror init` rejects plain `http://` URLs. Pass `--webdav-insecure-http` to opt in to plain HTTP (NOT recommended — credentials cross the wire in cleartext).
- **Server-side snapshot copy** — `full`-format snapshots use the WebDAV `COPY` method; no file data passes through the client.
- **Common errors** — see [`401 Unauthorized`](../../README.md#webdav-401-unauthorized) and [`405 Method Not Allowed` / `409 Conflict` from `MKCOL`](../../README.md#webdav-405-method-not-allowed-or-409-conflict-from-mkcol).

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
