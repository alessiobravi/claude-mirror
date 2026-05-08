← Back to [README index](../../README.md) · [docs index](../README.md) · backends/

# SFTP backend (any server with SSH access)

SFTP is the "I already have an SSH server" option. If you can `ssh user@host` interactively, you can use it as a claude-mirror backend. No cloud accounts, no app registration, no public hostnames required.

## Setup walkthrough

SFTP requires no cloud account or app registration — any OpenSSH server works. If you can `ssh user@host` interactively, you can use it as a claude-mirror backend.

### Step 1: Choose authentication (SSH key strongly recommended)

SSH key authentication is the supported default. Password authentication is supported as a LAN-only fallback for legacy / NAS setups that don't accept keys.

If you don't already have a key, generate one:

```bash
ssh-keygen -t ed25519 -C "claude-mirror@$(hostname)"
# Press enter to accept the default path (~/.ssh/id_ed25519)
# Use a passphrase or leave empty — claude-mirror runs non-interactively, so an empty passphrase or an ssh-agent-loaded key both work
```

### Step 2: Add the public key to the server

The simplest path is `ssh-copy-id`, which appends your public key to the remote `~/.ssh/authorized_keys` over a single password-authenticated SSH connection:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub alice@files.example.com
```

If `ssh-copy-id` is not available (some NAS firmwares strip it), append the key manually:

```bash
cat ~/.ssh/id_ed25519.pub | ssh alice@files.example.com 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
```

### Step 3: Verify interactive SSH works (and trust the host fingerprint)

This step is the prerequisite that lets `claude-mirror` connect non-interactively later — you must accept the server's host fingerprint into `~/.ssh/known_hosts` once, from the OpenSSH client, before claude-mirror can verify it on subsequent connects.

```bash
ssh alice@files.example.com
# First time: ssh prints the host's fingerprint and asks "Are you sure you want to continue connecting (yes/no)?"
# Type "yes" to add it to ~/.ssh/known_hosts, then exit.
```

If `claude-mirror` later refuses to connect with a host-key mismatch error, this is the file to inspect or update.

### Step 4: Decide on the server-side folder

Pick an absolute path on the server where the project will live, e.g. `/home/alice/claude-mirror/myproject`. You don't need to pre-create it — `claude-mirror` will `mkdir -p` the folder on first connect if it doesn't exist.

If the account is chrooted to a smaller subtree (Step 5), use a path relative to that chroot — e.g. `/myproject` if the account is jailed to `/home/alice/sftp/`.

### Step 5 (optional): Lock the account down to SFTP-only

For a dedicated mirror account, add the following to the server's `/etc/ssh/sshd_config`:

```
Match User alice
    ForceCommand internal-sftp
    ChrootDirectory /home/alice/sftp
    AllowTcpForwarding no
    X11Forwarding no
```

Trade-off: `internal-sftp` disables shell `exec_command`, so the server-side `sha256sum` + `cp -p` optimizations are unavailable. `claude-mirror` falls back automatically to client-side hashing + `get`/`put` for snapshots — slightly slower on large files, but otherwise functionally identical.

## Config file

```yaml
backend: sftp
project_path: /home/user/work/myproject
sftp_host: files.example.com           # hostname or IP of the SSH server
sftp_port: 22                          # default 22 — change if the server listens elsewhere
sftp_username: alice                   # the SSH user — same one you'd use with `ssh user@host`
sftp_key_file: ~/.ssh/id_ed25519       # PREFERRED — path to the private key file
sftp_password: ""                      # FALLBACK — leave empty when using a key; LAN-only when set
sftp_known_hosts_file: ~/.ssh/known_hosts   # where the host fingerprint is read from
sftp_strict_host_check: true           # default true — refuse to connect on host-key mismatch (set false ONLY for trusted LAN with rotating IPs)
sftp_folder: /home/alice/claude-mirror/myproject   # absolute path on the server (or relative to the chroot if `internal-sftp` is in use)
token_file: /home/user/.config/claude_mirror/sftp-myproject-token.json
poll_interval: 30                      # seconds between remote-state polls (no native push events on SFTP)
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

The password (when used) is stored only in the token file (chmod 0600), never in this YAML. SSH key files are read from disk on every connect; their permissions are your responsibility (OpenSSH refuses to use a key file that's group- or world-readable).

## Daily ops notes

- **No native push notifications** — SFTP has no event channel. claude-mirror polls remote state every `poll_interval` seconds (default 30s). Typical latency from a collaborator's `push` to your `watch` notification is up to `poll_interval` seconds.
- **Authentication** — pure SSH. No browser flow, no token refresh — the SSH key (or password) is the credential. claude-mirror validates the key/password on `init` and stores the password (when used) in the token file with `chmod 0600`.
- **Server-side snapshot copy** — when shell `exec_command` is available, `full`-format snapshots use server-side `cp -p` and `sha256sum` for free hashing. With `ForceCommand internal-sftp` (chrooted accounts), claude-mirror falls back to client-side hashing + `get`/`put` — functionally identical, slightly slower on large files.
- **Strict host checking** — `sftp_strict_host_check: true` (default) means claude-mirror refuses to connect if the server's host key doesn't match `~/.ssh/known_hosts`. Set to `false` only for trusted LAN setups with rotating IPs.

## Diagnosing setup problems

Once an SFTP backend is configured (or you suspect it's misconfigured), run:

```bash
claude-mirror doctor --backend sftp
```

This runs the generic credentials/connectivity checks AND seven SFTP-specific deep checks:

1. **Host fingerprint matches `~/.ssh/known_hosts`** — paramiko's `HostKeys` looks up the configured `sftp_host` in your known_hosts; if found, doctor opens an unauthenticated SSH transport, pulls the server's live host key from the handshake, and compares fingerprints. A mismatch is a SECURITY INCIDENT (possible MITM) and causes doctor to refuse to continue. If the host isn't in known_hosts at all, doctor emits an info line — first connection will prompt to verify.
2. **SSH key file exists + readable** — checks `sftp_key_file` (after `~` expansion). Failures point at the YAML or `ssh-keygen` to generate a fresh key.
3. **SSH key file permissions are 0600** — uses `os.stat(...).st_mode & 0o077` to detect any group/world bits set. OpenSSH refuses keys with looser permissions; doctor surfaces the offending mode and tells you to run `chmod 600 PATH`. Doctor does NOT auto-fix — chmod is a deliberate human action.
4. **SSH key can decrypt** — encrypted keys (passphrase-protected) raise `PasswordRequiredException`, which doctor reports as an info line, not a failure. ssh-agent (or claude-mirror's `auth` flow) handles the passphrase at sync time.
5. **Connection + authenticate** — opens a paramiko `Transport` to `sftp_host:sftp_port` (5-second timeout), authenticates with the configured key (or password fallback). Catches: server unreachable, auth rejected, host-key mismatch detected at the auth layer.
6. **`exec_command` capability** — probes `echo claude-mirror-doctor-probe` over an SSH session. If it returns exit 0, claude-mirror will use server-side `sha256sum` + `cp -p` for snapshot operations. If the channel request is refused (typical of `ForceCommand internal-sftp` setups), claude-mirror falls back to client-side hashing — functionally identical, slightly slower on large files. Either branch is an info line, not a failure.
7. **Root path access** — `sftp.stat(sftp_folder)` against the live SFTP channel. NotFound is an info line ("claude-mirror creates it on first push"); PermissionDenied is an auth-bucket failure pointing at server-side ACLs.

Sample successful output:

```
$ claude-mirror doctor --backend sftp
claude-mirror doctor — /home/alice/.config/claude_mirror/myproject.yaml

  ✓ config file parses: /home/alice/.config/claude_mirror/myproject.yaml

── checking sftp backend (/home/alice/.config/claude_mirror/myproject.yaml)
  · credentials file: skipped (SFTP uses inline host/user/key in YAML)
  ✓ SFTP credentials present in config (host + username + folder + key/password)
  ✓ SFTP connectivity ok (session opened + stat(/home/alice/claude-mirror/myproject) succeeded)
  ✓ SSH key file readable: /home/alice/.ssh/id_ed25519
  ✓ known_hosts file present: /home/alice/.ssh/known_hosts
SFTP deep checks
  ✓ Host in known_hosts; fingerprint matches (SHA256:abcdef...)
  ✓ Key file readable: /home/alice/.ssh/id_ed25519
  ✓ Key file permissions: 0600
  ✓ Key decryptable (or ssh-agent will handle)
  ✓ Connection + auth succeeded
  ✓ exec_command available; server-side hashing will be used
  ✓ Root path: /home/alice/claude-mirror/myproject
  ✓ project_path exists: /home/alice/projects/myproject
  ✓ manifest parses: /home/alice/projects/myproject/.claude_mirror_manifest.json

✓ All checks passed.
```

Sample failure on the host-fingerprint-mismatch case (the security-critical one):

```
SFTP deep checks
  ✗ Host fingerprint mismatch in /home/alice/.ssh/known_hosts
           Stored fingerprint: SHA256:abcdef...
           Live fingerprint:   SHA256:000000...
           POSSIBLE MAN-IN-THE-MIDDLE — refusing to connect.
      Fix: investigate the mismatch. If the host genuinely changed, run
           ssh-keygen -R sftp.example.com and re-add the host (verify the
           new fingerprint out-of-band first).
```

The fix-hint mentions `ssh-keygen -R HOSTNAME` deliberately — fingerprint mismatches are not a token problem, they're a security incident. Do NOT just re-run `claude-mirror auth`; verify the new host fingerprint out-of-band (e.g. ask the server administrator) before adding it back to known_hosts.

If multiple auth-class checks would fail (fingerprint mismatch + permission denied at the same time), doctor emits ONE bucketed failure and short-circuits the rest — you don't get five copies of "your access is broken" rooted in the same problem.

For chrooted (`internal-sftp`) accounts, expect the `exec_command` check to surface as a yellow info line — that's fine. claude-mirror falls back to client-side hashing transparently.

See [admin.md#sftp-deep-checks](../admin.md#sftp-deep-checks) for the full deep-check reference table and the auth-bucketing semantics.

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [admin.md#sftp-deep-checks](../admin.md#sftp-deep-checks) for the full doctor deep-check matrix.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
