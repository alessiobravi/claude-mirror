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

## See also

- [admin.md](admin.md) — restoring an older version from a snapshot if you resolved a conflict the wrong way.
- [cli-reference.md](cli-reference.md#sync) — the `sync` command that triggers conflict resolution.
- [README — Compare local vs remote for a single file](../README.md#compare-local-vs-remote-for-a-single-file) — `claude-mirror diff` for previewing differences before resolving.
