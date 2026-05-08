← Back to [project README](../README.md)

# claude-mirror documentation

This directory holds the per-area docs that the project README links into. Use it as a sidebar when browsing on GitHub.

## Backends

Per-backend setup, config fields, and troubleshooting:

- [Google Drive](backends/google-drive.md) — Google Cloud project, Drive API, Pub/Sub, OAuth2 setup
- [Dropbox](backends/dropbox.md) — Dropbox app registration, OAuth2 PKCE
- [OneDrive](backends/onedrive.md) — Azure AD app, device-code login
- [WebDAV](backends/webdav.md) — Nextcloud / OwnCloud / NAS / Apache mod_dav
- [SFTP](backends/sftp.md) — SSH keys, host fingerprints, OpenSSH

## Operations and admin

- [admin.md](admin.md) — snapshots, retention, `gc` / `prune` / `forget`, doctor, watcher service, multi-backend Tier 2 setup, auto-start
- [conflict-resolution.md](conflict-resolution.md) — interactive conflict prompts, `$EDITOR` merge, three-way diff
- [cli-reference.md](cli-reference.md) — every command, every flag

## Topology guides

- [scenarios.md](scenarios.md) — seven deployment topologies, end to end:
  - **A. Standalone** — local ↔ 1 backend
  - **B. Personal multi-machine** — local ⇄ 1 backend ⇄ local'
  - **C. Multi-user collaboration** — Alice ⇄ shared backend ⇄ Bob
  - **D. Multi-backend redundancy** — local → primary + N mirrors
  - **F. Selective sync** — custom `file_patterns` + exclusions
  - **G. Multi-user + multi-backend (production-realistic)** — shared primary + shared mirror, full Alice/Bob YAMLs
  - **H. Multi-project enterprise** — many configs in `~/.config/claude_mirror/`

## Convention for contributors

Whenever you add a feature, the docs in this tree get updated in the same change-set — not as a follow-up. The project README's "Documentation index" gets a link if you add a new file, rename one, or remove one. See the project's [CONTRIBUTING.md](../CONTRIBUTING.md) for test conventions and PR expectations.
