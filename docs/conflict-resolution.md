← Back to [README index](../README.md)

# Conflict resolution

When both your local file and the remote version changed since the last sync, claude-mirror prompts you to resolve the conflict:

```
Conflict in: CLAUDE.md

┌─ LOCAL ─────────────────────────────────┐
│ # My Project                            │
│ local version of the file...            │
└─────────────────────────────────────────┘

┌─ DRIVE ─────────────────────────────────┐
│ # My Project                            │
│ collaborator's version of the file...   │
└─────────────────────────────────────────┘

[L] Keep local  [D] Keep drive  [E] Open in editor  [S] Skip
```

- **L** — discard the remote version, keep yours, push it
- **D** — discard your local version, keep the remote version
- **E** — open a temporary file in `$EDITOR` with conflict markers:
  ```
  <<<<<<< LOCAL
  your content here
  =======
  collaborator's content here
  >>>>>>> DRIVE
  ```
  Edit the file to the desired result, save and exit. The resolved version is pushed.
- **S** — skip this file for now (it stays unresolved)

Set your preferred editor:

```bash
export EDITOR=nano   # or vim, code, etc.
```

## Non-interactive mode (cron / launchd / systemd)

For unattended sync runs (cron, launchd, systemd, CI) the interactive prompt would block forever — there is no TTY to answer it. Pass `--no-prompt --strategy` so every conflict resolves automatically:

```bash
claude-mirror sync --no-prompt --strategy keep-local    # local always wins
claude-mirror sync --no-prompt --strategy keep-remote   # remote always wins
```

Output is one yellow line per auto-resolved file plus a trailing one-line `Summary:` for grep-friendly cron mail. Every auto-resolution is logged to `_sync_log.json` with the strategy that won, so audits can spot every overwrite after the fact via `claude-mirror log`.

`--no-prompt` requires `--strategy` — without it the command exits 1 with a clean error message rather than silently falling back to the interactive flow under cron. `--strategy keep-local` overwriting the remote IS destructive in the operator's mind, but the flag combination IS the consent — no extra typed-YES gate. Full flag table and crontab samples in [cli-reference.md](cli-reference.md#sync) and [admin.md](admin.md#unattended-sync-via-cron).

## See also

- [faq.md](faq.md) — 30-second answers to common questions, including conflict-resolution recipes for cron / unattended use.
- [admin.md](admin.md) — restoring an older version from a snapshot if you resolved a conflict the wrong way.
- [admin.md — Unattended sync via cron](admin.md#unattended-sync-via-cron) — sample crontab entries for `--no-prompt --strategy`.
- [cli-reference.md](cli-reference.md#sync) — the `sync` command that triggers conflict resolution, including the `--no-prompt --strategy` flag table.
- [README — Compare local vs remote for a single file](../README.md#compare-local-vs-remote-for-a-single-file) — `claude-mirror diff` for previewing differences before resolving.
