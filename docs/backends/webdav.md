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

## Diagnosing setup problems

Once a WebDAV backend is configured (or you suspect it's misconfigured), run:

```bash
claude-mirror doctor --backend webdav
```

This runs the generic credentials/token/connectivity checks AND six WebDAV-specific deep checks:

1. **URL well-formed** — the configured `webdav_url` must parse to `https://` (or `http://` with `webdav_insecure_http: true`) plus a netloc and a path. Empty / scheme-less / netloc-less URLs are rejected before any network call.
2. **PROPFIND on the configured root** — issues `PROPFIND` with `Depth: 0` on the configured `webdav_url`. Expects HTTP 207 Multi-Status. 401 → auth-bucket (verify `webdav_username` and `webdav_password`), 404 → "configured WebDAV root doesn't exist" (create the folder server-side or fix the URL), 405 → "server doesn't support PROPFIND" (typically a misconfigured endpoint serving plain HTTP, or a Nextcloud URL missing `/remote.php/dav/files/USER/`), 5xx → transient retry hint.
3. **DAV class detection** — parses the `DAV:` response header (e.g. `DAV: 1, 2, 3` from Nextcloud). claude-mirror requires class 1 minimum; class 2 (locking) and class 3 (range PUT) are informational. Missing header or sub-1 classes emit a yellow warning, not a failure.
4. **ETag header presence** — checked from both the `ETag:` response header and the PROPFIND XML's `<d:getetag/>` field. Missing ETag is informational only — claude-mirror falls back to last-modified / content-md5 for change detection (slower but still correct).
5. **oc:checksums extension support** — Nextcloud / OwnCloud servers expose `<oc:checksums>SHA1:abc MD5:def SHA256:ghi</oc:checksums>` in PROPFIND responses, which claude-mirror prefers over ETags for primary-backend parity. Absence is informational only — non-Nextcloud / non-OwnCloud servers don't advertise this namespace.
6. **Account-level smoke test** — for Nextcloud / OwnCloud-shaped URLs (`https?://HOST/remote.php/dav/files/USERNAME/...`), PROPFIND the `/remote.php/dav/files/USERNAME/` base separately to confirm the account itself is reachable. Skipped silently for non-Nextcloud-pattern URLs (Apache mod_dav, Synology, Box.com, etc.).

Sample successful output:

```
$ claude-mirror doctor --backend webdav
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking webdav backend (/home/alice/.config/claude_mirror/myproject.yaml)
  · credentials file: skipped (WebDAV uses inline username/password)
  ✓ WebDAV credentials present in config (username + password)
  ✓ backend connectivity ok (list_folders on root succeeded)
WebDAV deep checks
  ✓ URL well-formed: https://nextcloud.example.com/remote.php/dav/files/alice/myproject
  ✓ PROPFIND succeeded; HTTP 207
  ✓ DAV class: 1, 2, 3
  ✓ ETag header present
  ✓ oc:checksums extension supported (SHA1, MD5, SHA256)
  ✓ Account-level PROPFIND succeeded: https://nextcloud.example.com/remote.php/dav/files/alice/
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✓ All checks passed.
```

Sample failure on the most common case (wrong credentials):

```
WebDAV deep checks
  ✓ URL well-formed: https://nextcloud.example.com/remote.php/dav/files/alice/myproject
  ✗ PROPFIND failed: HTTP 401
       Credentials rejected. Verify webdav_username and webdav_password.
       Fix: run claude-mirror auth --config /home/alice/.config/claude_mirror/myproject.yaml
```

If the configured root doesn't exist (e.g. the project sub-folder hasn't been created on the server yet):

```
WebDAV deep checks
  ✓ URL well-formed: https://nextcloud.example.com/remote.php/dav/files/alice/myproject
  ✗ PROPFIND failed: HTTP 404
       Configured WebDAV root doesn't exist: https://nextcloud.example.com/remote.php/dav/files/alice/myproject
       Fix: create the folder on the server, or correct webdav_url in /home/alice/.config/claude_mirror/myproject.yaml.
```

Multiple 401 failures across the root and account-level PROPFIND calls are bucketed into ONE `Credentials rejected` line — re-run `claude-mirror auth --config PATH` to update the saved credentials.

See [admin.md#webdav-deep-checks](../admin.md#webdav-deep-checks) for the full deep-check reference table and the auth-bucketing semantics.

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
