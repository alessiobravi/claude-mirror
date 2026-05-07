---
name: Bug report
about: Something broke or behaved unexpectedly
title: '[bug] '
labels: bug
assignees: ''
---

<!--
Before filing: please skim the open issues at
https://github.com/alessiobravi/claude-mirror/issues to check whether
the same problem has already been reported. If it has, comment on the
existing issue instead of opening a duplicate.

For SECURITY issues (vulnerabilities, credential leaks, path traversal,
etc.) please DO NOT file a public bug report. File a private security
advisory instead at:
  https://github.com/alessiobravi/claude-mirror/security/advisories/new
-->

## Environment

- **claude-mirror version:** (output of `claude-mirror --version`)
- **Operating system and version:** (e.g. macOS 14.5, Ubuntu 24.04)
- **Python version:** (output of `python3 --version`)
- **Backend in use:** (Google Drive / Dropbox / OneDrive / WebDAV / multiple)
- **Install method:** (`pipx install claude-mirror` from PyPI / `pipx install -e .` from a clone / other)

## What happened

Describe the actual behaviour you saw.

## What you expected to happen

Describe what you expected instead.

## Steps to reproduce

1. The exact command you ran (with all flags spelled out).
2. The exact files or state on disk that the command operated on.
3. Any environment variables that were set (`CLAUDE_MIRROR_AUTH_VERBOSE`, `CLAUDE_MIRROR_NO_UPDATE_CHECK`, etc.).
4. Any error output, copied verbatim.

If you can attach a minimal test project (a few small markdown files plus a config) that reproduces the issue, please do — it dramatically speeds up the diagnosis.

## Error output

```
Paste the full output here. The redactor in claude_mirror/backends/__init__.py
strips bearer tokens, basic-auth credentials in URLs, query-string secrets,
and home-directory paths automatically — but eyeball the output before
posting and redact anything else that should not be public.
```

## What you have already tried

(Optional) Things you tested before filing — running with `CLAUDE_MIRROR_AUTH_VERBOSE=1`, running `claude-mirror auth --check`, deleting the manifest, reinstalling the package, etc.

## Additional context

(Optional) Anything else that might be relevant — your project size, whether the issue is intermittent, whether it started happening after upgrading from a specific version, etc.
