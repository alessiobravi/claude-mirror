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

## See also

- [Scenario A — Standalone](../scenarios.md#a-standalone-mirror) for end-to-end usage patterns with this backend.
- [admin.md](../admin.md) for snapshots, retention policies, and the watcher daemon.
- [conflict-resolution.md](../conflict-resolution.md) for handling `sync` conflicts.
- [cli-reference.md](../cli-reference.md) for the full command list.
