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

## Agent-driven merge via the skill (AGENT-MERGE)

When `claude-mirror sync` finds a file changed on BOTH sides since the last sync, it ALSO writes a structured JSON envelope per conflicted file to `~/.local/state/claude-mirror/<project-slug>/conflicts/` BEFORE the interactive prompt fires. The envelope is intended for the LLM agent already running alongside you (Claude Code, Cursor, Codex, Antigravity, VSCode Copilot Chat, …) to read via the [skill](../skills/claude-mirror.md), propose a merge, show the proposal to you, and apply it on your confirmation.

claude-mirror itself binds to NO LLM API: no Anthropic SDK call, no Ollama HTTP call, no API key requirement. The CLI is purely file plumbing — the skill describes the agent contract, and your agent does the merge cognition.

The flow:

```bash
claude-mirror sync                                    # writes envelope + fires interactive prompt
claude-mirror conflict list                           # see what's pending
claude-mirror conflict show <path> --format markers   # fetch the file with conventional <<<<<<< / ======= / >>>>>>> markers
# (agent proposes a merge, shows it to you, asks for confirmation)
claude-mirror conflict apply <path> --merged-file <tmp>  # write merged content + clear envelope + push
```

Three subcommands cover the lifecycle:

- `conflict list [--json]` — pretty Rich table of pending envelopes (path, created-at, local hash prefix, remote hash prefix, backend). Empty case prints "No pending conflicts" and exits 0. `--json` emits a v1 envelope `{schema: "v1", command: "conflict-list", generated_at, conflicts: [...]}` for skill / script consumption.
- `conflict show <path> [--format envelope|markers] [--json]` — `envelope` (default) prints the full JSON envelope (every field, including the precomputed `unified_diff`); `markers` prints the file content wrapped in 3-way `<<<<<<< local / ||||||| base / ======= / >>>>>>> remote` markers — the legacy format every agent IDE knows. `--json` is shorthand for `--format envelope`.
- `conflict apply <path> --merged-file FILE | --merged-stdin [--push/--no-push]` — read the merged content, write it to the project file, clear the envelope, and (default `--push`) run `push --force-local <path>` to land it on the remote. `--no-push` lets the user batch multiple resolves before one push. Idempotent: re-running on a path whose envelope is already cleared prints "already resolved" and exits 0.

**Existing behaviour is unchanged for users without the skill.** The interactive `keep local / keep remote / open editor / skip` prompt still fires; the envelope is just additional information stored on disk. If you skip a conflict interactively, the envelope persists so the agent can still help later. If you resolve one (keep-local / keep-remote / editor), the envelope is cleared automatically.

**Opt out:** just don't use the skill — the existing interactive resolver works exactly as before, and you can also resolve conflicts manually by running `conflict apply` with a hand-written merged file. Binary-file conflicts skip envelope writing entirely (the agent can't usefully merge them) and fall through to the existing prompt.

The envelope schema is **version 1**. Future breaking changes bump the version; older CLIs that encounter a newer envelope refuse it cleanly with a "this CLI understands version N" error rather than misinterpreting the shape.

The conflicts directory itself is created with mode `0o700` (owner-only RWX) — same hygiene as `~/.ssh` — so a world-readable directory listing cannot expose project-internal rel-path filenames like `memory__keys__deploy.md.merge.json` to other local users; envelope files inside the dir are also `0o600`.

## See also

- [faq.md](faq.md) — 30-second answers to common questions, including conflict-resolution recipes for cron / unattended use.
- [admin.md](admin.md) — restoring an older version from a snapshot if you resolved a conflict the wrong way.
- [admin.md — Unattended sync via cron](admin.md#unattended-sync-via-cron) — sample crontab entries for `--no-prompt --strategy`.
- [cli-reference.md](cli-reference.md#sync) — the `sync` command that triggers conflict resolution, including the `--no-prompt --strategy` flag table.
- [cli-reference.md — `diff`](cli-reference.md#diff) — `claude-mirror diff <path>` for previewing local-vs-remote differences before resolving.
- [cli-reference.md — `conflict`](cli-reference.md#conflict) — full reference for the `conflict list / show / apply` subcommands, the envelope schema, and the markers format.
- [admin.md — Monitoring pending conflicts](admin.md#monitoring-pending-conflicts) — wiring `conflict list --json` into a dashboard or cron-driven nudge.
- [faq.md](faq.md#i-have-a-conflict-and-im-not-sure-how-to-merge-it-can-the-agent-help) — agent-help Q/A entry for AGENT-MERGE.
- [skills/claude-mirror.md](../skills/claude-mirror.md) — the agent-side contract: how the skill walks pending envelopes, asks for user confirmation, and applies merges.
