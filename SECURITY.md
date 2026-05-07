# Security policy — claude-mirror

Thanks for taking the time to investigate or report a security issue. This document describes how to report a vulnerability privately and what to expect afterwards.

## Reporting a vulnerability

Use GitHub's private security advisory feature rather than filing a public issue:

**https://github.com/alessiobravi/claude-mirror/security/advisories/new**

That URL opens a private form visible only to you and to the maintainer. The maintainer is notified immediately when a draft advisory is created and can respond there confidentially.

Please include in the report:

- The version of claude-mirror you tested against (`claude-mirror --version`)
- The operating system and Python version
- The backend in use (Google Drive, Dropbox, OneDrive, WebDAV, or none)
- A clear description of the issue and the impact
- A minimal reproduction (commands or steps that trigger it), redacted of any real credentials
- Any suggested mitigation if you have one

Do **not** include real OAuth tokens, real refresh tokens, real credentials.json contents, or any other secrets in the advisory. claude-mirror redacts most of these via `redact_error()` before they would land anywhere persistent, but the redactor is not perfect and you should not rely on it for the report itself.

## What to expect

- The maintainer will acknowledge the report within seven days.
- For confirmed vulnerabilities, the maintainer will work on a fix in a private branch and ship it as a patch release. The CHANGELOG entry will note that a security issue was fixed without disclosing the exploit details until after users have had a reasonable window to upgrade.
- You will be credited in the CHANGELOG for the report unless you ask to remain anonymous.
- claude-mirror does not run a bug bounty programme. Reports are appreciated but not financially compensated.

## Scope

In scope:

- Code in the `claude_mirror/` package and the helper scripts in `skills/`
- The `claude-mirror` and `claude-mirror-install` CLI commands
- The CI workflows in `.github/workflows/`
- Anything that ships in the wheel or sdist on PyPI

Out of scope:

- The third-party SDKs that claude-mirror depends on (`google-api-python-client`, `dropbox`, `msal`, `requests`, etc.). Report those upstream to their respective projects.
- Vulnerabilities in the user's own environment (operating system, shell, terminal emulator, network).
- Vulnerabilities in the cloud backends themselves (Google Drive, Dropbox, OneDrive, the user's WebDAV server). Report those to the respective vendors.
- The Claude Code agent platform itself. Report agent-platform issues to Anthropic.

## What claude-mirror's design protects against

- File contents never traverse infrastructure operated by claude-mirror's maintainer; they flow directly between the user's machine and the user's chosen cloud backend over the backend's standard HTTPS API.
- OAuth tokens are stored locally at chmod 0600 with an explicit `os.chmod` after write, so other local users cannot read them.
- Path-traversal guards (`_safe_join`) prevent remote-controlled paths from writing outside the configured project directory.
- Error messages are passed through `redact_error()` before being persisted, stripping bearer tokens, basic-auth credentials in URLs, query-string secrets, and home-directory paths.
- TLS with default certificate verification is used for every network call. There is no `verify=False` anywhere in the codebase.
- The only network destinations the binary ever talks to are the cloud-backend APIs the user has configured plus `api.github.com` and `raw.githubusercontent.com` for the version-check.

If you find a way around any of these, that is a security issue worth reporting.

## What this policy does NOT cover

- Functionality requests, performance issues, sync correctness bugs, and other non-security defects belong in regular issues at https://github.com/alessiobravi/claude-mirror/issues.
- Privacy policy questions belong in `PRIVACY.md`.
- Contributor questions belong in `CONTRIBUTING.md`.
