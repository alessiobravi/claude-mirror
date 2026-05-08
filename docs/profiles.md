← Back to [README index](../README.md)

# Credentials profiles

A *profile* is a YAML file under `~/.config/claude_mirror/profiles/<name>.yaml` that bundles the credential-bearing fields for one logical "account" — a Google account, a Dropbox app, an Azure AD app, a WebDAV server, or an SFTP host. Project YAMLs reference a profile by name (`profile: work` at the top, or the global `--profile work` flag) and inherit the credential fields from it instead of duplicating `credentials_file` / `token_file` / `dropbox_app_key` / `onedrive_client_id` / WebDAV creds / SFTP host info across every project YAML.

Profiles are an optional convenience. A user with a single project never needs them. A user with five projects on the same Google account, or one work account + one personal account on the same laptop, gets a much simpler config tree by collapsing the duplicated fields into one place.

## Why profiles

Without profiles, every project YAML on the same Google account looks like this:

```yaml
# ~/.config/claude_mirror/research.yaml
backend: googledrive
project_path: /Users/alice/research
credentials_file: /Users/alice/.config/claude_mirror/work-credentials.json
token_file: /Users/alice/.config/claude_mirror/work-token.json
gcp_project_id: my-work-gcp
drive_folder_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz
pubsub_topic_id: claude-mirror-research
file_patterns: ['**/*.md']

# ~/.config/claude_mirror/strategy.yaml — same account, different folder
backend: googledrive
project_path: /Users/alice/strategy
credentials_file: /Users/alice/.config/claude_mirror/work-credentials.json   # duplicated
token_file: /Users/alice/.config/claude_mirror/work-token.json               # duplicated
gcp_project_id: my-work-gcp                                                  # duplicated
drive_folder_id: 1ZyXwVuTsRqPoNmLkJiHgFeDcBa
pubsub_topic_id: claude-mirror-strategy
file_patterns: ['**/*.md']
```

With a profile:

```yaml
# ~/.config/claude_mirror/profiles/work.yaml
backend: googledrive
credentials_file: /Users/alice/.config/claude_mirror/work-credentials.json
token_file: /Users/alice/.config/claude_mirror/work-token.json
gcp_project_id: my-work-gcp
description: "Work Google account (alice@example.com)"

# ~/.config/claude_mirror/research.yaml — slim
profile: work
backend: googledrive
project_path: /Users/alice/research
drive_folder_id: 1AbCdEfGhIjKlMnOpQrStUvWxYz
pubsub_topic_id: claude-mirror-research
file_patterns: ['**/*.md']

# ~/.config/claude_mirror/strategy.yaml — slim
profile: work
backend: googledrive
project_path: /Users/alice/strategy
drive_folder_id: 1ZyXwVuTsRqPoNmLkJiHgFeDcBa
pubsub_topic_id: claude-mirror-strategy
file_patterns: ['**/*.md']
```

Rotating the credentials JSON or moving the token file is a one-line change in `profiles/work.yaml` instead of a sweep across every project file.

## Merge precedence: PROJECT WINS

When both the profile and the project YAML define the same field, the **project value wins**. The profile is the *default*; the project YAML is the *escape hatch*. This keeps the mental model simple — a project YAML is always exactly what it says it is, and the profile only fills in the blanks.

Worked example:

```yaml
# ~/.config/claude_mirror/profiles/work.yaml
backend: googledrive
credentials_file: /Users/alice/.config/claude_mirror/work-credentials.json
gcp_project_id: my-work-gcp

# ~/.config/claude_mirror/oddproject.yaml
profile: work
backend: googledrive
project_path: /Users/alice/oddproject
credentials_file: /Users/alice/.config/claude_mirror/special-credentials.json   # <-- overrides
drive_folder_id: 1Special123
pubsub_topic_id: oddproject-topic
```

After `Config.load("oddproject.yaml")`:

| Field             | Value                                                                  | Source            |
|-------------------|------------------------------------------------------------------------|-------------------|
| `backend`         | `googledrive`                                                          | both (same value) |
| `credentials_file`| `/Users/alice/.config/claude_mirror/special-credentials.json`          | **project wins**  |
| `gcp_project_id`  | `my-work-gcp`                                                          | profile           |
| `project_path`    | `/Users/alice/oddproject`                                              | project (only)    |
| `drive_folder_id` | `1Special123`                                                          | project (only)    |
| `pubsub_topic_id` | `oddproject-topic`                                                     | project (only)    |

A "set" project value is one that's *truthy*: a non-empty string, a non-zero int, `True`, a non-empty list. Empty/default values (`""`, `0`, `False`, `[]`) count as "not set" and let the profile's value through. This matters because `Config.load -> asdict` always produces every field — the dataclass defaults can't be distinguished from explicit user values without this rule.

## Sample profile YAMLs by backend

### Google Drive

```yaml
# ~/.config/claude_mirror/profiles/work-google.yaml
backend: googledrive
credentials_file: ~/.config/claude_mirror/work-credentials.json
token_file: ~/.config/claude_mirror/work-token.json
gcp_project_id: my-work-gcp-12345
description: "Work Google account (alice@example.com)"
```

`drive_folder_id` and `pubsub_topic_id` are project-specific — leave them off the profile.

### Dropbox

```yaml
# ~/.config/claude_mirror/profiles/personal-dropbox.yaml
backend: dropbox
dropbox_app_key: uao2pmhc0xgg2xj
token_file: ~/.config/claude_mirror/personal-dropbox-token.json
description: "Personal Dropbox app"
```

`dropbox_folder` belongs on the project YAML.

### OneDrive

```yaml
# ~/.config/claude_mirror/profiles/work-onedrive.yaml
backend: onedrive
onedrive_client_id: 9d7d6034-3524-4dce-b0f0-2a67f9e7b409
token_file: ~/.config/claude_mirror/work-onedrive-token.json
description: "Work Azure AD app (Files.ReadWrite + offline_access)"
```

`onedrive_folder` belongs on the project YAML.

### WebDAV

```yaml
# ~/.config/claude_mirror/profiles/nas.yaml
backend: webdav
webdav_url: https://cloud.example.com/remote.php/dav/files/alice/claude-mirror/
webdav_username: alice
webdav_password: app-password-from-nextcloud
token_file: ~/.config/claude_mirror/nas-token.json
description: "Home Nextcloud (alice)"
```

A trailing project-specific path can be added on the project YAML by overriding `webdav_url` if the WebDAV server requires per-project paths in the URL itself.

### SFTP

```yaml
# ~/.config/claude_mirror/profiles/vps.yaml
backend: sftp
sftp_host: vps.example.com
sftp_port: 22
sftp_username: alice
sftp_key_file: ~/.ssh/id_ed25519
sftp_known_hosts_file: ~/.ssh/known_hosts
sftp_strict_host_check: true
description: "VPS SFTP storage"
```

`sftp_folder` belongs on the project YAML.

## Subcommand reference

The `claude-mirror profile` subcommand group manages profile YAMLs.

### `claude-mirror profile list`

List every profile under `~/.config/claude_mirror/profiles/` with backend + description + path. Empty when no profiles exist yet.

### `claude-mirror profile show NAME`

Print the raw YAML of profile `NAME` to stdout. Useful for piping into other tools or inspecting from a script.

### `claude-mirror profile create NAME --backend BACKEND [--description TEXT] [--force]`

Interactively scaffold a new profile under `~/.config/claude_mirror/profiles/NAME.yaml`. The wizard prompts only for the credential-bearing fields of the chosen backend — project-specific fields like `drive_folder_id` / `dropbox_folder` are NOT collected here.

`--description` adds a one-liner shown by `profile list`. `--force` overwrites an existing profile YAML at the target path.

The resulting file is `chmod 0600` so credential material is not world-readable.

### `claude-mirror profile delete NAME [--delete] [--yes]`

Remove the profile YAML. Follows claude-mirror's destructive-ops convention — see [admin.md — Destructive ops are dry-run by default](admin.md#destructive-ops-are-dry-run-by-default) for the same pattern in `forget` / `gc` / `prune`:

- **No flag**: dry-run. Prints the file that *would* be deleted; exits 0; nothing changes on disk.
- `--delete`: arms the actual deletion. Prompts for the literal word `YES` (uppercase, exact). Anything else aborts.
- `--delete --yes`: skips the typed-`YES` prompt; required for non-interactive scripts.

Project YAMLs that reference the deleted profile via `profile: NAME` will fail to load on next use until you either remove the `profile:` field or recreate the profile under the same name.

## How `--profile NAME` resolves at runtime

The global `--profile NAME` flag goes on the click group, BEFORE the subcommand:

```bash
claude-mirror --profile work push                    # correct
claude-mirror push --profile work                    # WRONG: subcommand sees --profile, errors
```

Resolution order at `Config.load(path)`:

1. If the global `--profile NAME` flag was passed at the CLI, that profile wins.
2. Else, if the project YAML has a top-level `profile: NAME` field, that profile is used.
3. Else, no profile — the project YAML is loaded as-is.

The flag form is the one-shot escape hatch — it overrides any `profile:` reference baked into the project YAML for that one command invocation.

## Common workflows

### One work account + 5 work projects

```bash
# 1. Create the shared profile.
claude-mirror profile create work-google --backend googledrive
#   prompts: credentials file, token file, GCP project ID

# 2. Initialise each project, inheriting work-google's credentials.
cd ~/projects/research
claude-mirror --profile work-google init --wizard --backend googledrive
#   wizard: skips credentials-file / token-file / gcp-project-id prompts;
#   asks only for drive-folder-id, pubsub-topic-id, file patterns, etc.

cd ~/projects/strategy
claude-mirror --profile work-google init --wizard --backend googledrive
# (same skipped prompts)
```

After init, the resulting project YAMLs carry `profile: work-google` so `claude-mirror push` / `pull` / `sync` from inside those project directories pick the profile up automatically without needing the flag again.

### One personal Google + one work Google on the same laptop

```bash
claude-mirror profile create work-google --backend googledrive --description "Work account"
claude-mirror profile create personal-google --backend googledrive --description "Personal account"

# Project on the work account
cd ~/work/notes
claude-mirror --profile work-google init --wizard

# Project on the personal account
cd ~/personal/journal
claude-mirror --profile personal-google init --wizard
```

`profile list` now shows both, with descriptions, so future-you remembers which profile is which.

### Override a single project's credentials

A consultant who normally syncs through `work-google` but has one client project that needs to use a different OAuth client:

```yaml
# ~/.config/claude_mirror/clientX.yaml
profile: work-google                                                      # inherits gcp_project_id, token_file
backend: googledrive
project_path: /Users/alice/clients/X
credentials_file: /Users/alice/.config/claude_mirror/clientX-creds.json   # overrides profile's
drive_folder_id: 1ClientXFolder...
pubsub_topic_id: claude-mirror-clientX
```

The project's `credentials_file` wins because it's truthy; the rest of the profile's fields still apply.

### Inspect what's set without running a sync

```bash
claude-mirror profile list                 # see all profiles + descriptions
claude-mirror profile show work-google     # see the raw YAML
claude-mirror find-config                  # see which project YAML applies in cwd
```

## See also

- [README — Multiple projects on the same machine](../README.md#multiple-projects-on-the-same-machine)
- [docs/cli-reference.md](cli-reference.md) — every command + flag, including the `profile` subcommand group entry
- [docs/admin.md](admin.md) — operational guidance, destructive-ops convention
- [docs/scenarios.md](scenarios.md) — H. Multi-project enterprise (the canonical use case for profiles)
