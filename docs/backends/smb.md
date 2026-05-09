← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# SMB/CIFS backend (Windows shares, Synology, QNAP, TrueNAS, macOS Sharing)

SMB is the "I have a NAS or a Windows file share" option. If you can mount `\\server\share` from File Explorer or `smb://server/share` from Finder, you can use it as a claude-mirror backend. No cloud accounts, no app registration, no WebDAV layer in front of it.

claude-mirror speaks **SMB2/3 only**. SMBv1 is end-of-life on every modern OS and re-introduces EternalBlue-class attack surface; the doctor's protocol-negotiation deep check rejects v1-only servers explicitly.

## Quick start

### Synology DSM

1. **Control Panel → Shared Folder → Create.** Name the share (e.g. `claude-mirror`), pick the volume, leave snapshots enabled if you want them.
2. **Edit the share → Permissions → grant your DSM user Read/Write.**
3. **Control Panel → File Services → SMB.** Confirm "Maximum SMB protocol" is `SMB3`. Set "Minimum SMB protocol" to `SMB2` to lock SMBv1 out — claude-mirror refuses it anyway, but the audit trail is cleaner if the NAS doesn't advertise it.
4. From your dev machine: `claude-mirror init --wizard --backend smb`. Server is the NAS hostname (e.g. `synology.local`); share is `claude-mirror`; folder is the project subpath (e.g. `claude-mirror/myproject`).

### QNAP QTS

1. **Control Panel → Privilege → Shared Folders → Create.** Same shape as Synology — pick a volume, give your user write access.
2. **Control Panel → Network & File Services → Win/Mac/NFS/WebDAV → Microsoft Networking.** Enable SMB2 + SMB3; disable SMB1.
3. Continue with the wizard as above.

### TrueNAS / TrueNAS Scale

1. **Shares → Windows Shares (SMB) → Add.** Pick the dataset, enable the share, set ACLs in the wizard.
2. **System Settings → Services → SMB → Configure → Protocols.** Confirm SMB3 is the default; uncheck SMB1.
3. The username can be a local user or a connected AD user — both work.

### Windows file share (Windows 10/11/Server)

1. Right-click the folder → **Properties → Sharing → Advanced Sharing → Share this folder.** Permissions → grant your user Full Control or Change.
2. Confirm the SMBv1 client and server features are uninstalled (`Optional Features → SMB 1.0/CIFS File Sharing Support`). claude-mirror won't use SMBv1 even if the server offers it, but disabling the SMBv1 server reduces attack surface.
3. From your dev machine: `claude-mirror init --wizard --backend smb`. Server is the Windows hostname or IP; share is the share name from step 1.

### macOS Sharing

1. **System Settings → General → Sharing → File Sharing.** Pick the folder, add yourself with Read & Write.
2. Click **Options** → enable "Share files and folders using SMB". Tick the user account whose password you'll authenticate with.
3. Server is the Mac's `.local` hostname (or IP); share is the folder name shown in the Sharing panel.

### Generic SMB2/3 server (Samba on Linux)

```
[claude-mirror]
   path = /srv/samba/claude-mirror
   browseable = yes
   read only = no
   valid users = alice
   create mask = 0644
   directory mask = 0755
   # Refuse SMBv1 — claude-mirror won't talk to it anyway, but the
   # audit trail is cleaner when the server doesn't advertise it.
   min protocol = SMB2
```

Restart `smbd`. Then run `claude-mirror init --wizard --backend smb` from the dev machine.

## Config file

```yaml
backend: smb
project_path: /home/user/work/myproject
smb_server: nas.local           # hostname or IP of the SMB/CIFS server
smb_port: 445                   # default 445; legacy NetBIOS-over-TCP uses 139
smb_share: claude-mirror        # share name — the segment after \\server\
smb_username: alice             # local user on the server, or domain user
smb_password: ""                # stored plain in YAML at chmod 0600
smb_domain: ""                  # AD/NTLM domain; empty for workgroup auth
smb_folder: claude-mirror/myproject   # path within the share
smb_encryption: true            # default true — SMB3 per-message AES
token_file: /home/user/.config/claude_mirror/smb-myproject-token.json
poll_interval: 30               # seconds between remote-state polls
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
```

The password is stored in the YAML at chmod 0600 — same posture as `sftp_password`. With `smb_encryption: true` (the default), every SMB message between client and server is encrypted with per-message AES, so the wire traffic stays confidential even on shared LANs and over VPN. SMB2-only servers negotiate down automatically; the doctor surfaces the actual negotiated state so you can see when encryption was downgraded.

## Permission model walkthrough

SMB has TWO permission layers and you need both to grant the right access:

1. **Share-level permissions** (Windows: Sharing tab → Permissions; Samba: `valid users` / `read only`). These gate "can this user see and connect to the share at all?". Default on Windows is "Everyone: Read" — that's enough to LIST but not to WRITE.
2. **File-level permissions** (NTFS ACLs on Windows / POSIX permissions + ACLs on Linux/macOS). These gate "what can this user do once connected?". The two layers AND together — the EFFECTIVE permission is the more restrictive of share-level and file-level.

If `claude-mirror doctor --backend smb` reports the share is accessible (Check 4 ✓) but folder write is denied (Check 5 ✗), the share-level layer is fine but file-level needs adjustment. Common case on Synology: you granted the user Read/Write on the share but the underlying volume folder is owned by a different user — fix in **Control Panel → Shared Folder → Edit → Permissions → ACL**.

## Daily ops notes

- **No native push notifications** — SMB has no event channel. claude-mirror polls remote state every `poll_interval` seconds (default 30s).
- **Authentication** — pure NTLM / Kerberos over SMB. No browser flow, no token refresh. The username + password (and optional domain) are the credential.
- **No server-side copy primitive** — `smbclient` doesn't expose the SMB2 `FSCTL_SRV_COPYCHUNK` ioctl, so `copy_file` round-trips through the client. Files under 50 MiB stream through memory; larger ones write to a temp file on the destination side, atomically renamed on success.
- **No native hash** — SMB has no in-protocol checksum (signing digests aren't content hashes). claude-mirror computes SHA-256 client-side by streaming the file. One extra read pass per `get_file_hash`, but the result is reliable across machines (size+mtime alone is flaky with clock skew).
- **SMBv1 is rejected** — both at the doctor's protocol-negotiation check AND by `smbprotocol` itself. If your server only speaks v1, enable v2/v3 (every modern OS supports it) before running claude-mirror.

## Diagnosing setup problems

```bash
claude-mirror doctor --backend smb
```

Six SMB-specific deep checks layer on top of the generic credentials/connectivity loop:

1. **Server reachable** — TCP connect to `smb_server:smb_port`. Connection refused / timeout points at firewall or wrong port (445 default; 139 only for legacy NetBIOS-over-TCP).
2. **SMB protocol negotiation** — connects at the protocol level without authenticating. SMB2/3 → ok; SMBv1-only → SECURITY GATE failure with a fix-hint pointing at the server's protocol settings (NOT a `claude-mirror auth` retry — v1 is refused for security).
3. **Authentication** — `register_session` with the configured credentials. Bad creds, account locked, and domain mismatch all bucket as ONE auth-bucket failure so the user sees a single "your access is broken" line.
4. **Share access** — `scandir` on the share root. Share-not-found points at `smb_share` in the YAML; permission-denied points at share-level vs file-level ACLs.
5. **Folder write** — writes a 1-byte sentinel `__claude_mirror_doctor_test`, then deletes. Permission-denied here (after share access succeeded) means file-level ACLs need adjustment.
6. **Encryption status** — info-only line reporting whether SMB3 encryption was negotiated successfully. A yellow warning fires when encryption was REQUESTED but the server downgraded to plaintext — acceptable for closed LAN, risky on the open internet.

Sample successful output:

```
$ claude-mirror doctor --backend smb
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking smb backend (/home/alice/.config/claude_mirror/myproject.yaml)
  · credentials file: skipped (SMB uses inline server/share/user/password in YAML)
  ✓ SMB credentials present in config (server + share + username + password + folder)
  ✓ SMB connectivity ok (session opened on share claude-mirror)
SMB deep checks
  ✓ Server reachable: nas.local:445
  ✓ SMB2/3 protocol negotiated
  ✓ Authentication succeeded as alice
  ✓ Share accessible: \\nas.local\claude-mirror
  ✓ Folder writable: \\nas.local\claude-mirror\myproject
  ✓ SMB3 encryption negotiated (per-message AES)
  ✓ project_path exists: /home/alice/projects/myproject

✓ All checks passed.
```

Sample SMBv1-only failure (security gate):

```
SMB deep checks
  ✓ Server reachable: legacy-nas.local:445
  ✗ Server only speaks SMBv1 — refusing to connect.
      SMBv1 is end-of-life and re-opens EternalBlue-class attack surface.
      Fix: enable SMB2 or SMB3 on the server (modern Windows / Samba / NAS
           firmware do this by default; check the server's SMB protocol settings).
```

## Troubleshooting

- **`Server unreachable: nas.local:445`** — port 445 is filtered. Check `ping nas.local`. ISPs commonly block 445 outbound; SMB to a public-internet host should go over a VPN. Alternative: try port 139 if the server is set up for NetBIOS-over-TCP.
- **`SMB authentication rejected`** — verify `smb_username` / `smb_password` / `smb_domain`. Domain users typically need `smb_domain: CORP` set; the backend folds it into the canonical `CORP\\alice` form before passing to `register_session`. Account-locked errors look the same shape — check the server's audit log.
- **`Share not found: claude-mirror`** — the share isn't advertised by the server. List shares with `smbclient -L //nas.local -U alice` (Linux/macOS) or `net view \\nas.local /all` (Windows) to confirm the spelling.
- **`Permission denied writing`** — share access succeeded but file-level ACLs deny write. On Synology / QNAP / TrueNAS that's the underlying volume folder's ACL panel. On Windows, check **Properties → Security** in addition to **Properties → Sharing**.
- **`SMB3 encryption requested but server negotiated down`** — the server only supports SMB2. Acceptable on a closed LAN; switch to a SMB3-capable server for any internet-reachable setup.

## See also

- [Documentation index](../README.md) — back to the docs sidebar.
- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [admin.md#smb-deep-checks](../admin.md#smb-deep-checks) for the full doctor deep-check matrix.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
- [backends/s3.md](s3.md) — sister v0.5.65 backend (object storage).
- [backends/sftp.md](sftp.md) — SFTP is the closest peer (path-as-id, inline creds, polling watcher).
