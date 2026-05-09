← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# FTP / FTPS backend (cPanel / DirectAdmin / shared hosting / NAS)

The FTP backend targets the legacy shared-hosting market — cPanel, DirectAdmin, old WordPress hosting, budget VPS providers — plus consumer NAS appliances on a LAN. Anywhere SFTP isn't an option but `ftp://` is, this backend works.

**Use the SFTP backend wherever possible.** SFTP is the canonical answer for "secure file transfer" in 2026; FTPS (`ftp_tls=explicit`) is acceptable; plain FTP (`ftp_tls=off`) is gated behind a loud warning at every connection because credentials cross the wire UNENCRYPTED.

## Quick start — cPanel hosting

cPanel-flavoured shared hosting always exposes FTP, normally with the cPanel account name as the FTP user and the project hosted under `/public_html/` or a custom subdirectory.

```bash
claude-mirror init --wizard --backend ftp
# FTP host: yourname.cpanel-host.com
# TLS mode: explicit
# FTP port: 21
# FTP username: cpaneluser
# FTP password: ...
# FTP folder: /public_html/claude-mirror/myproject
# Passive mode: yes (default)
```

The wizard runs `claude-mirror auth` for you immediately — it opens a probe connection, mkdir-p's the configured folder, and writes a verified-at marker to the token file. No browser flow.

## Quick start — DirectAdmin hosting

DirectAdmin uses the same shape as cPanel — FTPS-on-21 with the panel username:

```bash
claude-mirror init --wizard --backend ftp
# FTP host: server.directadmin-host.net
# TLS mode: explicit
# FTP port: 21
# FTP username: dauser
# FTP password: ...
# FTP folder: claude-mirror/myproject
```

## Quick start — NAS / home FTP (cleartext on LAN)

NAS appliances (Synology / QNAP / TrueNAS) often expose plain FTP on the LAN with no TLS option. Cleartext FTP is acceptable here because the connection never leaves the local network — but the wizard makes you confirm:

```bash
claude-mirror init --wizard --backend ftp
# FTP host: 192.168.1.42
# TLS mode: off
# (warning emitted: "Cleartext FTP selected — username + password
#  travel UNENCRYPTED on every connection. Acceptable ONLY for trusted
#  local-network use.")
# FTP port: 21
# FTP username: nasadmin
# FTP password: ...
# FTP folder: /shares/claude-mirror/myproject
```

## Config field reference

```yaml
backend: ftp
project_path: /home/user/work/myproject
ftp_host: ftp.example.com           # hostname or IP of the FTP/FTPS server
ftp_port: 21                        # default 21 (or 990 for implicit FTPS)
ftp_username: alice                 # FTP username — often the panel account name
ftp_password: ...                   # FTP password (chmod-0600 in YAML)
ftp_folder: /public_html/claude-mirror/myproject  # server-side path
ftp_tls: explicit                   # explicit | implicit | off
ftp_passive: true                   # passive mode — works through more firewalls
token_file: /home/user/.config/claude_mirror/ftp-myproject-token.json
poll_interval: 30                   # seconds between remote-state polls (no native push events)
file_patterns:
  - "**/*.md"
machine_name: workstation
user: alice
```

## Cleartext-FTP security note

`ftp_tls: off` means every byte of the FTP control channel — including the username and password sent in the `USER` and `PASS` commands — travels in cleartext. Anyone with passive access to the network path between you and the server can capture them.

The backend emits a stderr warning at every `authenticate()` call when cleartext mode is active, regardless of the configured host. The doctor's deep-checks layer adds an additional advisory line when the configured host doesn't resolve to a loopback (`127.0.0.0/8`) or RFC1918 (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) address — i.e. when cleartext FTP is being used against an internet-reachable server.

**Recommendation:** if the server supports FTPS at all, use `ftp_tls: explicit` (default). If the server is a NAS on a closed LAN with no FTPS option, cleartext is acceptable. For any other configuration, switch to the SFTP backend.

## FTPS modes

| Mode | Port | Behaviour |
|---|---|---|
| `explicit` (default) | 21 | The standard RFC 4217 negotiation: connect plain, run `AUTH TLS`, then upgrade. The data channel is also encrypted via `PROT P`. Works with the vast majority of modern hosting servers. |
| `implicit` | 990 | Legacy mode: TLS wraps the control channel from the first byte. Some NAS firmwares and older servers require this — claude-mirror handles the socket-wrapping itself since `ftplib.FTP_TLS` doesn't speak implicit out of the box. |
| `off` | 21 | Cleartext FTP. Credentials and payloads are NOT encrypted. LAN-only. |

## Daily ops notes

- **No native push notifications** — FTP has no event channel. claude-mirror polls remote state every `poll_interval` seconds (default 30s).
- **No server-side copy** — FTP has no server-side copy primitive. `copy_file` falls back to download-then-upload (via memory or a temp file for large content). Snapshot operations on `full`-format snapshots can be slower than on backends with native copy.
- **No native checksum** — FTP has no in-protocol checksum command in the base spec. The backend tries `XSHA256`, `HASH`, `XSHA1`, `XMD5` (some servers implement one or more) and falls back to streaming the bytes and computing sha256 client-side. Same posture as SFTP without `sha256sum` exec.
- **Atomicity** — uploads are direct STORs, not `.tmp` + rename. Many shared-hosting FTP servers don't implement `RNFR/RNTO` reliably, so the SFTP-style POSIX rename guarantee doesn't exist. For atomic-upload guarantees, prefer SFTP or WebDAV.
- **No new dependencies** — the backend is built on Python's stdlib `ftplib`. `pipx install claude-mirror` ships everything needed.

## Diagnosing setup problems

Once an FTP backend is configured, run:

```bash
claude-mirror doctor --backend ftp
```

This runs the generic credentials/connectivity checks AND six FTP-specific deep checks:

1. **Host reachable** — TCP connect to `ftp_host:ftp_port`. Connection refused / timeout → failure with a port-check fix hint.
2. **Server greeting + protocol banner** — surfaces the server's 220-line so you can confirm the backend is talking to the expected box.
3. **TLS handshake** (when `ftp_tls != "off"`) — verifies TLS negotiation completes; surfaces cipher + protocol version (TLSv1.2 / TLSv1.3) as info.
4. **Authentication** — login with the configured creds. Bad password (530) → AUTH-bucket failure pointing at the YAML.
5. **Folder access** — `cwd` to `ftp_folder`. Doesn't exist → failure with a hint to create it server-side; permission denied → AUTH-bucket failure pointing at server-side ACLs.
6. **Folder write** — `STOR` a 1-byte sentinel `__claude_mirror_doctor_test`, then `DELE` it. Permission denied → AUTH-bucket failure; 552 (storage exceeded) → quota failure.

Plus the cleartext-mode advisory when `ftp_tls=off` and the configured host is non-loopback / non-RFC1918.

## Troubleshooting

- **Passive vs active mode** — most hosting servers and NATs require passive mode (the default). If transfers hang on the first byte, double-check `ftp_passive: true`. Some firewalls only allow active; switch to `ftp_passive: false` only when you control both ends.
- **Implicit FTPS port-990 connections refused** — a few NAS firmwares advertise implicit-FTPS but accept it on a non-standard port. Set `ftp_port` to whatever the NAS exposes (often 990, sometimes 992 or 9899).
- **TLS handshake failures** — if your server's certificate is self-signed or expired, the explicit-FTPS handshake will fail with an `ssl.SSLError`. Either install a real certificate (LetsEncrypt is free), or as a last resort run cleartext on a closed LAN. claude-mirror does NOT support disabling certificate validation.
- **MLSD vs LIST** — modern servers (vsftpd, ProFTPD, Pure-FTPd) implement MLSD for structured directory listings. Older servers / restrictive shared-hosting providers only support LIST. The backend tries MLSD first and falls back to parsing LIST automatically — no config knob needed.
- **`SIZE` command not supported** — some servers reject `SIZE` against text-mode files; the backend retries with a parent-directory listing.

## See also

- [Documentation index](../README.md) — back to the docs sidebar.
- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [admin.md#ftp-deep-checks](../admin.md#ftp-deep-checks) for the full doctor deep-check matrix.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
- [backends/sftp.md](sftp.md) — strongly preferred over FTP wherever both are available.
- [backends/s3.md](s3.md), [backends/smb.md](smb.md) — alternative non-cloud-native backends added in v0.5.65.
