# Privacy Policy — claude-mirror

**Last updated:** 2026-05-07
**Version covered:** 0.6.0 and later (the public-app OAuth release).

This document describes what data claude-mirror handles, where it goes, and what the maintainer of claude-mirror has access to. It is written in plain language for the user; legalese is avoided deliberately.

---

## TL;DR

claude-mirror is **client-only software** — a command-line tool that runs entirely on your computer. It does not operate any servers, does not collect telemetry, does not relay your file contents through any service the maintainer controls.

Your files, file contents, file names, and OAuth tokens stay on (1) your computer and (2) the cloud backend you choose (Google Drive, Dropbox, OneDrive, or your WebDAV server). The maintainer of claude-mirror has no access to any of that data.

---

## What claude-mirror is, technically

claude-mirror is a Python package distributed via PyPI. After installation, it provides a CLI binary (`claude-mirror`) and a few helper commands. All execution happens locally on the user's machine.

When the user authenticates a backend, the OAuth flow happens directly between the user's browser and the cloud provider's servers (e.g. `dropbox.com`, `accounts.google.com`, `login.microsoftonline.com`). The resulting refresh token is written to the user's local filesystem with permission `0600` (owner-readable only). When the user runs `push`, `pull`, `sync`, etc., claude-mirror reads that token and makes API calls directly from the user's machine to the cloud provider.

**There is no server in between.** There is no claude-mirror infrastructure of any kind beyond the source code on GitHub and the package distribution on PyPI.

---

## What data flows where

### Stays on the user's computer

- Project files being synced
- The local `manifest`, `inbox`, and `hash_cache` JSON files (used to detect changes)
- OAuth tokens (refresh tokens for whichever backends the user has authorised)
- All configuration files in `~/.config/claude_mirror/`

### Flows between the user's computer and their chosen cloud backend

- Project files being uploaded / downloaded (over the cloud provider's HTTPS API)
- File metadata (names, sizes, modification times, content hashes)
- OAuth authentication exchanges (initial consent flow + periodic token refreshes)

**This traffic uses the cloud provider's standard APIs and TLS.** It does NOT pass through any claude-mirror infrastructure. The cloud provider's privacy policy applies to data on their side; the user's relationship with the cloud provider is unchanged by claude-mirror.

### Flows between the user's computer and GitHub

- One-line version-check requests to `api.github.com/repos/alessiobravi/claude-mirror/contents/pyproject.toml`, run at most once per 24 hours, used solely to compare the installed version against the latest published version.
- Optional: if the user runs `claude-mirror update --apply`, the tool fetches the new release from PyPI via `pip` / `pipx`.

The version-check can be disabled by setting `CLAUDE_MIRROR_NO_UPDATE_CHECK=1` in the environment.

### Does NOT flow anywhere claude-mirror's maintainer can see

- File contents
- File names or paths
- User identities (the maintainer does not know who the users are)
- Cloud account identifiers
- Usage frequency, error rates, or any other telemetry
- IP addresses, machine identifiers, or geographic information

---

## What the maintainer can see

The maintainer of claude-mirror is the entity who registered the public Dropbox and Azure AD apps used by v0.6.0+. By virtue of owning those registrations, the maintainer can see, on the respective developer dashboards:

- An aggregate count of users who have consented to the claude-mirror app
- Aggregate API call volumes and rate-limit status
- No individual identities, files, content, or metadata

This is the same information any developer of a published OAuth app sees — it is the cloud provider showing the developer "your app is in use, here is the usage scale" so the developer can plan rate limits and capacity.

The maintainer additionally has access to:

- Public PyPI download counts for the `claude-mirror` package
- GitHub repository stars, forks, issues, and pull requests
- Any information voluntarily submitted in GitHub issues or pull requests by users themselves

The maintainer does NOT have access to the user's cloud account, files, tokens, configuration, or any data on the user's computer.

---

## Token security

When the user authenticates a backend, claude-mirror stores the resulting refresh token in `~/.config/claude_mirror/<project>-token.json`. Specifically:

- The file is written with permission `0600` (only the file's owner — the user — can read it).
- An additional `os.chmod(0o600)` is performed after write as a belt-and-braces guard against the system umask.
- The file format is a JSON object containing the refresh token and the minimum metadata needed to refresh it. No file contents are stored alongside.

The user can revoke claude-mirror's access at any time from the cloud provider's account settings:

- **Google Drive:** https://myaccount.google.com/permissions
- **Dropbox:** https://www.dropbox.com/account/connected_apps
- **OneDrive:** https://account.microsoft.com/consent/Manage
- **WebDAV:** disable the user account on the WebDAV server, or change the password.

After revocation, claude-mirror's stored token is unusable; the user can also delete the local token file with `rm ~/.config/claude_mirror/<project>-token.json`.

---

## Bring-your-own (BYO) cloud app — advanced option

For users who prefer not to share an OAuth app registration with other claude-mirror users (organisation-internal deployments, audited environments, custom rate-limit needs), claude-mirror supports BYO mode: you register your own Dropbox or Azure AD app and pass its credentials via flag or YAML config. In BYO mode, the maintainer of claude-mirror has no visibility whatsoever into your usage — not even the aggregate-count metric described above.

This is an advanced option, not promoted in the main install flow. The setup walkthrough is at `docs/advanced/byo-app-registration.md` in the project repository.

---

## Children's data, sensitive personal data, regulated data

claude-mirror is a developer-facing CLI tool for syncing project files. It is not directed at children, not designed to handle sensitive personal data, not certified for regulated data (HIPAA, PCI-DSS, FedRAMP, etc.), and the maintainer makes no compliance claims.

The user is responsible for ensuring that the project files they sync are appropriate for the cloud backend they have chosen and for their own compliance environment.

---

## Changes to this policy

Material changes to how claude-mirror handles data are described in `CHANGELOG.md` under the version that introduces them, and in this file. The "Last updated" date at the top of this document reflects the most recent revision.

Users tracking the project can subscribe to GitHub release notifications for the repository to receive automatic emails when new versions ship.

---

## Contact

For questions about this policy, security disclosures, or abuse reports, open an issue at https://github.com/alessiobravi/claude-mirror/issues.

For sensitive security disclosures that should not be public, email the maintainer at the address listed on their GitHub profile.

---

## Source code transparency

claude-mirror is open-source under the GPL-3.0-or-later license. The complete source code is available at https://github.com/alessiobravi/claude-mirror. Any user who wants to verify the claims in this policy can read the source — the relevant entry points are:

- `claude_mirror/backends/` — implementations of each cloud backend (data path)
- `claude_mirror/_update_check.py` — the GitHub version-check
- `claude_mirror/notifier.py` — local desktop notifications (no network)
- `claude_mirror/slack.py` — optional, opt-in, user-configured Slack webhooks (data path)

A `grep -rn 'https://' claude_mirror/` produces a complete inventory of every URL the code can ever talk to. There are no hidden destinations.
