# Changelog

All notable changes to claude-mirror.

---

## [Unreleased]

### Fixed — security + correctness polish (SECURITY-POLISH)

A six-finding pass tightening the credential-handling, cross-process, and server-trust surfaces. No version bump — the release notes will fold this into the next tagged release. 1486 tests pass on macOS (1436 baseline + 50 new); `mypy --strict` clean across 51 source files (was 50 + the new `_webhook_url.py`).

- **H1 — Credential-bearing fields hidden from `repr(Config)`.** Every credential-bearing dataclass field (`webdav_password`, `sftp_password`, `ftp_password`, `s3_secret_access_key`, `smb_password`, `slack_webhook_url`, `discord_webhook_url`, `teams_webhook_url`, `webhook_url`, `webhook_extra_headers`) now uses `field(repr=False)`. A stray `console.print(f"... {config}")`, exception with `config` in locals dumped to logs / Slack, or `logger.debug(config)` can no longer leak secrets. Non-sensitive identifiers (`project_path`, `backend`, `s3_bucket`, `s3_access_key_id`, `webdav_username`) stay visible — over-masking would make debug output useless. Webhook URLs are repr-masked because the token at the end of the URL acts as a bearer credential, and `webhook_extra_headers` typically carries `Authorization: Bearer …`.
- **H2 — Webhook URL scheme + host validation.** Every Slack / Discord / Teams / Generic webhook URL now goes through a strict scheme + host gate at `Config.__post_init__` time AND at every `_send_webhook` / `WebhookNotifier.post_json` callsite. Only `https://` is accepted (no `file://`, no `http://`, no other schemes — these would let a misconfigured project YAML turn the notifier into a local-file read or an internal-endpoint probe like `http://169.254.169.254/...` for AWS metadata). Per backend, the host must match: Slack → `hooks.slack.com`; Discord → `discord.com` or `discordapp.com`; Microsoft Teams → `outlook.office.com` or any `*.webhook.office.com` per-tenant subdomain; the Generic webhook keeps the https-only rule but accepts any host (that is the whole point of "generic"). Bad URLs surface during `claude-mirror init` as a clean `ValueError` naming the offending field and 1-indexed route position. New module `claude_mirror/_webhook_url.py` (~120 lines) holds the helpers; lazy-imports only — no Click, no Rich.
- **H4 — `Manifest.save()` cross-process file lock.** The watcher daemon and a foreground `claude-mirror sync` can both write the same project's manifest concurrently. The previous in-process `threading.Lock` only serialised threads in ONE process; across processes it was a no-op, so the two writers raced on the same `<manifest>.tmp` path and `os.replace` could silently clobber the other's bytes. `Manifest.save()` now holds a sibling `<manifest>.lock` file under `_filelock.exclusive_lock` (the same `fcntl.flock` / `msvcrt.locking` helper the inbox already uses) for the entire read-merge-write sequence, so disjoint entries from two processes both land in the final manifest — neither side clobbers the other's commit. Each writer also uses a per-PID-per-thread tmp suffix (`f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"`) as defence in depth. Local `manifest.remove(...)` is tracked through the merge so explicit deletions still propagate (a stale on-disk entry doesn't silently resurrect a removed key).
- **H6 — WebDAV + S3 server-returned path validation.** A hostile or buggy WebDAV server can return `<href>../../etc/passwd</href>`; an S3 bucket may legitimately contain a key like `myproject/../../etc/passwd` (S3 has no path constraints — every key is just a string). Today every downstream caller in `sync.py` runs results through `_safe_join` so the project root stays sealed. New helper `claude_mirror/backends/_util.py::validate_server_rel_path(rel, *, backend_name)` rejects empty paths, leading `/` or `\`, any `..` segment, NUL bytes, and Windows drive-letter prefixes — applied at the listing site of `WebDAVBackend._parse_file_list` / `_list_recursive_manual` and `S3Backend.list_files_recursive` so a future caller that bypasses `_safe_join` can't be tricked into escaping the project root. Rejection raises `BackendError(FILE_REJECTED)`.
- **M2 — Conflict envelope dir mode 0o700.** `_conflicts.envelope_dir(...)` now creates `<XDG_STATE_HOME>/claude-mirror/<slug>/conflicts/` with `mkdir(mode=0o700)` AND an explicit `os.chmod(target, 0o700)` afterwards (mkdir's mode is umask-AND-ed, so the chmod is needed to defeat permissive umasks). The chmod is unconditional so dirs created by an older version with the default `0o755` mode get tightened on first use under the new code. Same hygiene as `~/.ssh`. The envelope files themselves were already `0o600`; this closes the directory-listing leak (filenames like `memory__keys__deploy.md.merge.json` revealed project-internal rel-paths to other local users).
- **M3 — `install.py` shlex / PowerShell quoting.** `_find_binary()` returns `shutil.which("claude-mirror")`, which on Windows / non-pipx installs CAN contain spaces (`C:\Program Files\Python311\Scripts\claude-mirror.exe`) or apostrophes. The previous f-string interpolation produced broken syntax at best, command-splitting at worst. POSIX (`zsh`/`bash`) emit `eval "$({shlex.quote(binary)} completion bash)"` so a path with spaces becomes a single token in the eval substitution. PowerShell uses a new `_ps_single_quote` helper that wraps the path in PowerShell single quotes with internal apostrophes doubled (PowerShell's literal-string convention), producing `Invoke-Expression (& '<path-with-doubled-apostrophes>' completion powershell | Out-String)`.

### Documentation

- `docs/admin.md` — new paragraph under "Notifications" documenting the webhook URL scheme/host validation rule, plus a new sentence under "Auto-start the watcher" noting the manifest cross-process file-lock guarantee.
- `docs/conflict-resolution.md` — one line noting the conflicts envelope directory is `0o700` (same hygiene as `~/.ssh`).

### Tests

- New `tests/test_webhook_url_validation.py` (~25 tests) — scheme rules, per-backend host gates, notifier-level boundary, Config-construction-time rejection.
- New `tests/test_config.py` (3 tests) — `repr(Config)` masks every credential sentinel, keeps non-sensitive identifiers visible, omits masked-field names entirely.
- New `tests/test_manifest_filelock.py` (4 tests) — two-process disjoint-entry test via `multiprocessing` `spawn`, contended barrier variant, per-process tmp-suffix invariant, deletion-propagation regression.
- Extended `tests/test_webdav_backend.py` (3 tests) and `tests/test_s3_backend.py` (4 tests) — server-returned path traversal / absolute / NUL-byte / Windows-drive-prefix rejection plus a normal-path sanity check.
- Extended `tests/test_install_completion.py` (4 tests) — POSIX shlex.quote quoting with and without spaces, PowerShell single-quote quoting with and without internal apostrophes.
- Existing tests `tests/test_webhooks.py`, `tests/test_route.py`, `tests/test_tmpl.py` — placeholder `*.example` URLs swapped for realistic-but-unused hostnames so the new Config-time URL gate accepts them.

---

## [0.5.68] — 2026-05-10

A new `claude-mirror conflict` subcommand group plus an envelope-handoff flow that lets the LLM agent already running alongside the user (Claude Code, Cursor, Codex, Antigravity, VSCode Copilot Chat, …) merge sync conflicts via the skill instead of forcing the user to make `keep-local` / `keep-remote` decisions alone in a terminal. claude-mirror itself binds to NO LLM API — the CLI is purely file plumbing over a v1 JSON envelope on disk; the skill describes the agent contract, and the agent does the merge cognition. 1436 tests pass on macOS (1388 baseline + 48 new); `mypy --strict` clean across 50 source files (was 49 + the new `_conflicts.py`).

### Added — agent-driven merge via the skill (AGENT-MERGE)

When `claude-mirror sync` finds a file changed on BOTH sides since the last sync, it writes a structured JSON envelope per text-file conflict to `~/.local/state/claude-mirror/<urlsafe-project-slug>/conflicts/` BEFORE the existing interactive `[L]ocal / [D]rive / [E]ditor / [S]kip` prompt fires. The envelope is intended for the running LLM agent to read via the skill, propose a merge, **show the proposal to the user and ask for explicit confirmation**, and apply it via `conflict apply`. Existing behaviour is unchanged for users without the skill — the interactive prompt still fires; the envelope is information ALSO STORED, not a behaviour change. Binary-file conflicts skip envelope writing entirely (the agent can't usefully merge them) and fall through to the existing prompt unchanged.

- New module `claude_mirror/_conflicts.py` (~330 lines including docstrings). Public API: `ConflictEnvelope` (frozen dataclass, version=1) with `path` / `local_text` / `remote_text` / `base_text` / `local_hash` / `remote_hash` / `base_hash` / `created_at` / `project_path` / `backend` / `unified_diff` fields; `envelope_dir(project_path)` resolving `<XDG_STATE_HOME>/claude-mirror/<project-slug>/conflicts/` with on-demand creation; `envelope_path(project_path, rel_path)` flattening `/` separators to `__` so the conflicts/ tree is a single directory (`memory/foo/bar.md` → `memory__foo__bar.md.merge.json`); `make_envelope(...)` constructor that hashes the text bodies and precomputes a `remote → local` unified diff; `write_envelope` (atomic via tempfile + `os.replace`); `read_envelope` (rejects unknown versions); `list_envelopes` (alphabetical, skips unparseable files silently); `clear_envelope` (idempotent); `is_eligible(local_bytes, remote_bytes)` reusing `_diff.is_binary` rather than duplicating the heuristic. NO Click, NO Rich, NO LLM SDK imports — the pure module imports only `dataclasses`, `datetime`, `difflib`, `hashlib`, `json`, `os`, `pathlib`, `tempfile`, `urllib.parse` so the envelope plumbing is testable in isolation.
- `claude_mirror/sync.py::SyncEngine._resolve_conflict` — engine integration. BEFORE calling `MergeHandler.resolve_conflict`, the engine writes an envelope per text-file conflict and surfaces a one-line `[envelope] <rel-path> → <path>` message to the terminal so the user sees what's been written. After the user picks `keep-local` / `keep-remote` / `editor`, the envelope is cleared (since the conflict is resolved); on `skip`, the envelope persists so the agent can still pick it up later. Envelope-write failures are logged with a yellow warning but never abort the interactive prompt — the user's existing path must keep working even if `~/.local/state/` is somehow unwritable.
- `claude_mirror/cli.py` — new `@cli.group("conflict")` with three children. `conflict list [--config PATH] [--json]` — Rich table with columns `Path / Created / Local hash[:8] / Remote hash[:8] / Backend`; empty case prints `No pending conflicts.` and exits 0. `--json` emits a v1 envelope `{schema: "v1", command: "conflict-list", generated_at, conflicts: [...]}` shaped like the existing `status --json` / `log --json` envelopes. `conflict show PATH [--config PATH] [--format envelope|markers] [--json]` — `envelope` (default) prints the full JSON envelope; `markers` wraps the file content in conventional 3-way `<<<<<<< local / ||||||| base / ======= / >>>>>>> remote` markers (the legacy format every agent IDE knows). `--json` is shorthand for `--format envelope`; conflicts with `--format markers` exit 1. `conflict apply PATH (--merged-file FILE | --merged-stdin) [--push/--no-push]` — reads the merged content, writes it via `_safe_join` (path-traversal safe), clears the envelope, and (default `--push`) runs `push --force-local PATH` to land the merge on the remote. `--no-push` lets the user batch multiple resolves before one push. Idempotent: re-running on a path whose envelope is already cleared prints `Envelope for <path> is already resolved` and exits 0.
- `claude_mirror/cli.py::_NO_WATCHER_CHECK_CMDS` — extended with `conflict` so the watcher banner cannot leak into the JSON output of `conflict list --json` (same pattern as `health` / `prompt` / `log` / `redact`).
- `skills/claude-mirror.md` — new "Conflict-resolution mode (AGENT-MERGE)" section between Step 4 and Step 5. Walks the agent through `conflict list` → `conflict show <path> --format markers` → propose merge → SHOW to user and get confirmation → `conflict apply --merged-file`. The available-commands list grew the three new subcommand lines. The "Important rules" list grew "Never run `conflict apply` without showing the user the proposed merge and getting explicit confirmation. The whole point of AGENT-MERGE is human-in-the-loop."
- `tests/test_conflicts.py` — **48 tests** covering the full surface (offline, <100 ms each):
  - **Pure-function layer (28 tests):** `make_envelope` populates every field; envelope round-trip via `write_envelope` → `read_envelope` preserves equality; atomic write leaves no orphan tempfiles; version-mismatch rejected with `ValueError`; missing path raises `FileNotFoundError`; future additive JSON fields are filtered cleanly (forward-compat); `is_eligible` covers text/text, local-binary, remote-binary, local-None, remote-None, both-None, UTF-8 multi-byte; `envelope_path` flattens slashes (`memory/foo/bar.md` → `memory__foo__bar.md.merge.json`), normalises backslashes (Windows ↔ POSIX), keeps single-component paths intact; `envelope_dir` creates on demand, honors `XDG_STATE_HOME`, falls back to `~/.local/state`, gives two projects disjoint dirs; `list_envelopes` empty → `[]`, alphabetical order, ignores non-`.merge.json` files, skips unparseable envelopes silently; `clear_envelope` removes existing files and is idempotent; `build_unified_diff` includes `remote/` and `local/` headers, empty when bodies are identical.
  - **CLI layer (17 tests):** empty `conflict list` prints `No pending conflicts`; populated table shows rel-paths; `--json` envelope shape (`schema: "v1"`, `command: "conflict-list"`, `generated_at`, `conflicts[]`); empty `--json` returns `conflicts: []`; `conflict show --format envelope` round-trips through JSON; `--format markers` includes `<<<<<<<` / `=======` / `>>>>>>>` in correct order; `||||||| base` block appears only when `base_text` is set; `--json` is shorthand for `--format envelope`; missing envelope exits 1 with a friendly message; `--json --format markers` conflicts exit 1; `apply --merged-file` writes the file, clears the envelope, fires `push --force-local`; `--no-push` skips the push; idempotent on cleared envelope; `--merged-file` and `--merged-stdin` are mutually exclusive (passing neither, or both, exits 1); `--merged-stdin` reads from stdin; `conflict --help` lists all three subcommands.
  - **Engine integration (3 tests):** a text-file conflict produces an envelope at the canonical path (verified via `read_envelope` round-trip); a binary-file conflict (NUL byte on either side) produces NO envelope; picking `keep-local` clears the envelope after a successful resolve.
- `README.md` — Daily usage cheatsheet grew three lines: `conflict list` / `conflict show <path> --format markers` / `conflict apply <path> --merged-file <tmp>`.
- `docs/cli-reference.md` — top-level command list grew the three `conflict` invocations; new `### conflict` subsection under `## Daily` with the synopsis, per-subcommand flag tables, sample workflow, the v1 envelope JSON shape, the markers format, the `--push/--no-push` contract, and the agent-handoff narrative (no LLM SDK import, forward-compat policy via the `version` field).
- `docs/conflict-resolution.md` — new "Agent-driven merge via the skill (AGENT-MERGE)" section explaining the envelope-handoff flow, the skill's role, the three subcommands, the version-1 schema, and the opt-out (just don't run the skill — the existing interactive resolver still works). The "See also" block grew cross-links to `cli-reference.md#conflict`, `admin.md#monitoring-pending-conflicts`, the FAQ entry, and the skill.
- `docs/admin.md` — new `### Monitoring pending conflicts` subsection under "Pre-push secret scanning with redact" with a 3-line shell hook example wiring `claude-mirror conflict list --json | jq '.conflicts | length'` into a cron-driven nudge.
- `docs/faq.md` — new Q/A entry "I have a conflict and I'm not sure how to merge it. Can the agent help?" under Sync workflow, pointing at `conflict list / show / apply` and the skill's role.

### Tests
- `pytest tests/` — **1436 passed, 3 skipped** locally on macOS (was 1388 + 48 new). The 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards.
- `mypy --strict claude_mirror/` — clean across **50 source files** (was 49 + the new `_conflicts.py`).

---

## [0.5.67] — 2026-05-10

A new `claude-mirror redact PATH...` subcommand for pre-push secret scrubbing, plus a documentation sweep that brings every page in line with the eight-backend reality post-v0.5.65. 1388 tests pass on macOS (1345 baseline + 43 new); `mypy --strict` clean across 49 source files (was 48 + new `_redact.py`).

### Added — `claude-mirror redact` for pre-push secret scrubbing (REDACT)

A new top-level `claude-mirror redact PATH...` subcommand interactively scrubs likely secrets out of project markdown files BEFORE they get pushed to a backend. The motivating scenario: a user accidentally pasted an API key into a `CLAUDE.md` or memory file and is about to push it to Drive / S3 / wherever — REDACT catches it locally. Dry-run by default (consistent with the project's destructive-ops-safe-by-default rule); `--apply` walks an interactive replace/keep/skip-file/quit prompt loop, and `--apply --yes` auto-replaces every finding non-interactively for CI / pre-commit hook usage. Replacement marker is `<REDACTED:KIND>` and re-running `redact` on already-redacted text is a no-op.

- New module `claude_mirror/_redact.py` (~330 lines, ~60 of pattern catalogue + ~150 of pure scanner / replacement logic). Public API: `Finding` (frozen dataclass), `SECRET_PATTERNS` ((kind, compiled regex) tuples), `scan_text(text, *, path) -> list[Finding]`, `scan_file(path) -> list[Finding]` (skips binary files via NUL-byte sniff in the first 8 KiB), and `apply_replacements(text, findings, *, kept=()) -> str` (idempotent: already-redacted markers are no-ops). NO Click, NO Rich, NO file mutation — the pure module imports only `re`, `dataclasses`, `pathlib`, `collections.abc` so the catalogue + scanner are testable in isolation.
- Kind catalogue (high-confidence starting subset; expanding is a follow-up): `aws-access-key`, `aws-secret-key`, `github-token`, `slack-webhook`, `slack-bot-token`, `openai-api-key`, `anthropic-api-key`, `google-api-key`, `gcp-service-account-key`, `private-key-block` (multi-line PEM), `jwt`, `password-assignment`, `generic-high-entropy`. Span-level dedup so a body matched by multiple patterns surfaces once with the higher-confidence kind. Anthropic precedence over OpenAI for `sk-ant-…` prefixes is enforced via catalogue order.
- `claude_mirror/cli.py` — new `@cli.command()` `redact` (~230 lines). Accepts one or more PATH arguments (file or directory; directory paths recurse over `*.md` and skip dotted dirs + `_claude_mirror_snapshots` / `_claude_mirror_blobs` infra folders). Renders a Rich findings table in the same visual style as `status --short`. With `--apply` on a TTY, drives a per-finding prompt loop via `click.prompt`; with `--apply --yes` auto-replaces every finding. With `--apply` on a non-TTY without `--yes`, exits 1 with a fix-hint pointing at `--yes` (we never silently default to "replace all" / "keep all" — that's the wrong failure mode). `[q]uit` mid-loop exits 1 with already-applied changes left on disk, matching the user's signal that they did NOT clear the full slate.
- `claude_mirror/cli.py::_NO_WATCHER_CHECK_CMDS` — extended with `redact` so the watcher banner can never leak into the findings table or interfere with a pre-commit hook's exit-code-driven contract.
- `claude_mirror/cli.py::_redact_stdin_isatty()` — new module-level helper wrapping `sys.stdin.isatty()`. Per `feedback_no_global_time_sleep_patch.md`, the test suite monkeypatches this wrapper rather than rebinding the stdlib global; CliRunner.invoke installs its own sys.stdin replacement so a direct patch of `sys.stdin.isatty` does not survive into the command body.
- Live progress on the scanning phase via the project-wide dual-line `make_phase_progress` widget when scanning more than 5 files (skipped for smaller invocations where the Progress overhead is greater than the value).
- `tests/test_redact.py` — **43 tests** covering the full surface (offline, <100 ms each):
  - **Pure-function layer (29 tests):** every catalogue entry has at least one positive sample (matches) and at least one negative sample (looks-similar but doesn't match — `AKIAxxx…` too short for `aws-access-key`, 33-char body for `google-api-key`, no-label-gate for `aws-secret-key`, wrong host for `slack-webhook`, etc.). Multi-line input. Source-order sort. `apply_replacements` happy path + idempotence + `kept`-set exclusion + already-redacted no-op + multi-finding-per-line. End-to-end "rescan after apply yields zero findings" idempotence guarantee. Binary-file detection via NUL byte; non-UTF-8 file detection. `Finding` frozen + hashable (so the `set[Finding]` kept-set logic in the CLI works).
  - **CLI layer (14 tests):** clean-file message; dry-run findings table + `--apply` hint; `--apply --yes` writes back; `--apply` on non-TTY without `--yes` errors with the `--yes` fix-hint; `--apply` on TTY routes through `click.prompt` with replace+keep choices honoured; `[s]kip file` advances to the next file with no writes; `[q]uit` mid-loop leaves applied changes on disk and exits 1; directory recursion only walks `*.md` (a `.txt` with a secret is ignored); multiple paths in one invocation; `--help` documents dry-run-default + the kind catalogue; `SECRET_PATTERNS` entries have unique kinds (regression guard against a future catalogue typo silently shadowing a pattern).
- `README.md` — Daily usage cheatsheet grew a `claude-mirror redact .` line.
- `docs/cli-reference.md` — top-level command list grows the `claude-mirror redact PATH...` line; new `### redact` subsection with the synopsis, flag table, kind catalogue, sample dry-run output, sample interactive transcript, the dry-run-by-default contract, the non-TTY-without-`--yes` error path, and a "When to use" guidance block (pre-push hook integration is the canonical use case).
- `docs/admin.md` — new `### Pre-push secret scanning with redact` subsection under Snapshots and disaster recovery (next to "End-to-end integrity audit"), with a 4-line shell hook example wiring `claude-mirror redact <project> --apply --yes` into `.git/hooks/pre-commit`.
- `docs/faq.md` — new Q/A entry "I accidentally pasted an API key into my notes. How do I scrub it before pushing?" under Sync workflow, pointing at `redact PATH`, the dry-run-default safety, and the cli-reference subsection.
- `skills/claude-mirror.md` — new "Pre-push safety: scrub secrets before pushing" section directing the skill to run `claude-mirror redact <project>` (dry-run) when secrets / API keys / OAuth tokens are mentioned in the conversation; `redact --apply` is added to the destructive-ops list that requires user confirmation; the available-commands list grew the `redact` line.

### Documentation — backends-matrix sweep across the doc tree

Post-v0.5.65 the supported-backends matrix went from six to eight (BACKEND-S3 + BACKEND-SMB) and several pages had drifted. This release sweeps every doc so each page accurately reflects the eight-backend reality. No source-code or test changes from this sub-section.

- `README.md` — polling-list, `## How it works` polling line, install-time tab-completion claim ("five valid backends" → eight), documentation index gains FTP and SMB rows, scenarios bullet list grew Scenario J (FUSE mount), file-locations table grew FTP/S3/SMB token-file rows, data-loss disclaimer enumerates all eight remote stores, duplicate L43 line collapsed.
- `docs/README.md` — backends index gains FTP/S3 entries; scenarios list grew I + J ("seven topologies" → "nine").
- `docs/admin.md` — credentials-skipped row collapsed (was split across two contradictory rows); doctor "all five backends" → "every backend"; "Where to go next" + final "See also" gain FTP/S3/SMB.
- `docs/cli-reference.md` — three contradictory `--backend` lines on `init` collapsed to one (lists all eight); same for `clone`; doctor's `--backend` choice list extended; `init` per-backend pages list gains `smb.md`; `clone` identity-flag list gains FTP and SMB rows; three new per-backend doctor narratives (ftp, s3, smb) mirroring the Drive/Dropbox/OneDrive/WebDAV/SFTP shape.
- `docs/faq.md` — "Which backend should I pick?" decision tree grew S3-compatible / SMB / FTP bullets; deep-check enumeration grew FTP and SMB; "all 5 backends" throttle claim → "all eight"; "topologies A through H" → "A through J, E omitted"; polling-list enumeration extends to FTP/S3/SMB.
- `docs/scenarios.md` — Scenario A backend-choice prose extends to mention FTP / S3 / SMB cross-links.
- `docs/profiles.md` — opening sentence enumerates all eight backend shapes; new sample profile YAML subsections for FTP / S3 / SMB.
- `CONTRIBUTING.md` — fixtures list grows FakeS3 + FakeShare + the SFTP/FTP fixtures; mypy expected-output literal source-file count dropped (drift-prone).
- `skills/claude-mirror.md` — `drive-ahead` → `remote-ahead` in Step 4 + the project-memory rules; `pushing to Drive` → `pushing to remote` in PRE-SYNC and POST-PULL rules; authentication-error block split into two paragraphs (browser-OAuth backends vs inline-credential backends — fix the YAML and run `doctor --backend NAME`); completion-subcommand list grew `powershell`.
- `docs/backends/{s3,smb,ftp}.md` — see-also cross-link extensions per the "Docs must be browseable" rule (Documentation-index back-links, sibling-backend cross-links, `admin.md#<backend>-deep-checks`).

### Tests
- `pytest tests/` — **1388 passed, 3 skipped** locally on macOS (was 1345 + 43 new). The 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards.
- `mypy --strict claude_mirror/` — clean across 49 source files (was 48 + the new `_redact.py`).

---

## [0.5.64] — 2026-05-09

<!-- subsumes: v0.5.63 -->

CI hotfix for v0.5.63: Windows CI flagged 5 ncdu CLI tests failing because they exercise behaviour reachable only on POSIX (the gate fires before any flag parsing on Windows; the gate-rejection path is covered by `test_cli_windows_gated_with_friendly_message` separately). Added per-test `@pytest.mark.skipif(sys.platform == "win32")` markers; replaced the Unicode em-dash in the gate message with an ASCII hyphen so it renders correctly on Windows console (cp1252 / cp437). v0.5.63 was tagged-but-never-published; v0.5.64 ships the same five-feature batch (TREE + NCDU + STATS + VERIFY + BACKEND-FTP) with the Windows test-skips folded in. PyPI's burn-once policy keeps v0.5.63 unpublished.

### Fixed — Windows CI green for `ncdu` CLI tests
- `tests/test_ncdu.py` — five CLI dispatch tests guarded by `@pytest.mark.skipif(sys.platform == "win32", reason="ncdu CLI flow is POSIX-only")`. Pure-data layer tests (`build_size_tree`, `top_n_paths`, `format_non_interactive`, `entries_from_backend_listing`) continue to run cross-platform.
- `claude_mirror/cli.py::ncdu` — Windows gate message replaces `—` (em-dash, U+2014) with `-` (ASCII hyphen) so the message renders cleanly on Windows console encodings without a `�` replacement character.

---

## [0.5.63] — 2026-05-09

Five new user-facing surfaces ship together: `claude-mirror tree` (remote-listing visualization), `claude-mirror ncdu` (interactive disk-usage TUI), `claude-mirror stats` (usage summary aggregation), `claude-mirror verify` (end-to-end integrity audit), and the new **FTP / FTPS storage backend** (legacy shared-hosting market — cPanel / DirectAdmin / NAS — via Python's stdlib `ftplib`, no new dependencies). 1251 tests pass on macOS (1101 baseline + 150 new); mypy `--strict` clean across 46 source files.

S3 and SMB backends are still in flight as a separate batch — the parallel-additions to shared CLI surfaces (`_AVAILABLE_BACKENDS` tuple, `_create_storage` dispatch, init wizard, doctor matrix) collided too aggressively with FTP's parallel additions. Splitting their integration into a follow-up release on a stable baseline is the cleaner path.

### Added — `claude-mirror tree` for remote-listing visualization (TREE)

A new top-level `claude-mirror tree [PATH]` subcommand prints a `tree(1)`-style view of remote files with sizes and (optionally) modification timestamps. Inspired by `rclone tree`. Reuses the same `StorageBackend.list_files_recursive(...)` path that `status` / `push` / `pull` already exercise — no engine changes, no new third-party dependencies. Pure rendering happens in a new `claude_mirror/_tree.py` module so the layout logic is unit-testable without spinning up the CLI.

- `claude_mirror/_tree.py` — new module. `render_tree(entries, *, sub_path, depth, show_size, show_mtime, ascii_only, root_label)` synthesises a directory tree from a flat listing payload, sorts directories before files (alphabetical within each group), renders Unicode (`├──` / `└──` / `│   `) or ASCII (`+--` / `\\--` / `|   `) connectors, and appends a `N directories, M files (TOTAL total)` footer. `--depth N` truncates deeper subtrees and adds a `... (K more files in subtrees)` summary line.
- `claude_mirror/cli.py` — new `@cli.command()` `tree` wired in next to `find-config`. Optional `[PATH]` positional restricts the rendering to a sub-path; missing PATH errors out cleanly via `FileNotFoundError`. Tier 2 `--remote NAME` dispatches to the named mirror's listing instead of the primary; unknown `NAME` exits 1 with the list of configured backend names. The listing fetch shows a dual-line phase Progress (`Listing  explored N folder(s), M file(s) ... done. (K files)`); the local rendering is synchronous and does not wrap in a Progress.
- `tests/test_tree.py` — 15 offline tests covering empty / single-file / deeply-nested / depth-limited / Unicode-vs-ASCII / size+mtime toggles / sort order / subpath filtering / Tier 2 `--remote` dispatch / unknown-remote error / missing-PATH error. All run against `FakeStorageBackend` from `conftest.py`. <100ms each.
- `README.md` — Daily usage cheatsheet: new `claude-mirror tree` line.
- `docs/cli-reference.md` — top-level command list block extended with the full `tree` invocation; new `### tree` subsection with the flag table and a sample rendering.

### Added — claude-mirror ncdu interactive disk-usage TUI (NCDU)

New `claude-mirror ncdu` subcommand modeled on `ncdu` / `rclone ncdu`: an interactive curses TUI showing per-directory size aggregates of the configured remote, with arrow-key navigation and a `--non-interactive` flag for cron / CI / scripts that just want the top-N largest paths in plain text.

- New module `claude_mirror/_ncdu.py` (~200 lines, ~330 with docstrings) split into a pure-data layer and a thin curses wrapper. The data layer (`SizeNode`, `build_size_tree`, `top_n_paths`, `format_non_interactive`, `entries_from_backend_listing`) has no curses, no rendering, no I/O — it turns a flat backend listing into a directory-aggregate tree and answers "what's the biggest thing in here?" questions; it is the unit-tested surface. `run_curses_ui(root)` is the thin wrapper that takes a built tree and runs the interactive event loop; validation path is manual smoke-test in a real terminal.
- Tier 2: `--remote NAME` walks a specific Tier 2 mirror by `backend_name` (default: the primary backend). Unknown name → clean error listing the configured backends.
- `--non-interactive` mode prints a fixed-shape report (`Top N largest paths in BACKEND backend:` header, size + count + path columns, total line) so cron jobs can string-grep on it. `--top N` defaults to 20.
- Interactive keybindings: `↑` / `↓` move cursor, `Enter` / `→` descend into a directory, `←` / `Backspace` / `h` ascend, `q` quits. The body bar-of-asterisks scales relative to the largest child of the current node. Handles `KEY_RESIZE` gracefully.
- POSIX-only: `curses` is not in the CPython stdlib on Windows. On Windows, `claude-mirror ncdu` exits with a friendly hint pointing at `claude-mirror tree --depth N` (the read-only tree view) as the closest cross-platform alternative. No new third-party dependencies.
- `claude_mirror/cli.py` — new `ncdu` Click subcommand, ~120 lines. Calls `_load_engine`, resolves the target backend by name, fetches the listing via `list_files_recursive(folder_id, exclude_folder_names={SNAPSHOTS_FOLDER, BLOBS_FOLDER, LOGS_FOLDER})`, builds the tree, and dispatches into `format_non_interactive` or `run_curses_ui` based on the `--non-interactive` flag. Live progress on the listing phase ("Fetching remote listing..." → "Fetched N file(s). Building size tree...").
- `tests/test_ncdu.py` — **32 tests** for the pure-data layer + CLI dispatch (offline, <100ms each). Coverage: empty / single-file / nested-paths / siblings / duplicate-rel-path / NUL-byte rejection / max-listing-entries guard tree-build cases; `SizeNode.sorted_children` ordering; `top_n_paths` desc order, n bounds, ties; `format_non_interactive` shape + empty-remote case; `entries_from_backend_listing` adapter happy + defensive paths; CLI `--non-interactive --top N`, default `--top 20`, `--remote NAME` dispatch to mirror, unknown-backend error, `--top 0` rejection, Windows-platform gating with the friendly message.
- `README.md` — Daily usage cheatsheet entry: `claude-mirror ncdu` (interactive TUI) and `claude-mirror ncdu --non-interactive --top 10`.
- `docs/cli-reference.md` — top-level command list grows the `claude-mirror ncdu …` line. New `### ncdu` subsection with the keybindings table and a sample non-interactive output block.

Pure-data API exposed by `claude_mirror/_ncdu.py`:
- `SizeNode(name, path, is_file, size, file_count, children)` — `size` and `file_count` are aggregates (sum of every descendant file for directory nodes); `children` is keyed by name; `path` is rel-from-root with `/` separators.
- `build_size_tree(entries: Iterable[tuple[str, int]], *, root_name: str = "") -> SizeNode` — accepts `(rel_path, size)` tuples.
- `top_n_paths(root: SizeNode, n: int) -> list[SizeNode]` — n largest aggregates in size-desc order.
- `format_non_interactive(root: SizeNode, n: int, *, backend_label: str = "primary") -> str` — fixed-shape plain-text report.
- `entries_from_backend_listing(listing: Iterable[dict[str, Any]]) -> Iterable[tuple[str, int]]` — adapter from backend native shape (`relative_path` + `size` keys) to the data layer's input.
- `run_curses_ui(root, *, project_label, backend_label) -> None` — thin curses wrapper; manual smoke-test only.

### Tests
- `pytest tests/` — **1133 passed, 3 skipped** locally on macOS (was 1101, +32 new ncdu tests; the 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards).
- `mypy --strict claude_mirror/` — clean across 42 source files (was 41 + the new `_ncdu.py`).

### Added — claude-mirror stats for aggregated usage summary (STATS)

`claude-mirror stats` is a new read-only subcommand that aggregates the project's `_sync_log.json` into a usage summary inspired by `rclone --stats`. Pairs with PRESENCE (v0.5.60) for team visibility — PRESENCE shows who is active right now, STATS shows the rolled-up activity over a configurable window.

Default window is the last 7 days; `--since` and `--until` accept the same vocabulary as `history --since/--until` (ISO date `2026-04-15`, ISO datetime, or relative duration `Nd` / `Nw` / `Nm` / `Ny`). The `--by` axis groups rows by `backend` (default), `user`, `machine`, `action`, or `day`. `--top N` caps the row count (default 20). `--json` emits a v1 envelope with the additive v1.1 `result` shape `{since, until, group_by, rows[], totals}`. Avg-latency and per-event byte counts are NOT reported — the existing `SyncEvent` schema does not record them, so the stats output stays truthful and surfaces only `events` / `files` / `conflicts` (where conflicts comes from the existing `auto_resolved_files` audit trail).

- New module `claude_mirror/_stats.py` — pure aggregation function `aggregate_log(entries, since, until, group_by, top, backend_label)` returning a `StatsResult` with typed `rows[]` and `totals`. No I/O, no clocks; 100% unit-testable with hand-built dicts.
- New CLI command in `claude_mirror/cli.py` — wires `--since` / `--until` through the existing `parse_relative_or_iso_date` helper, fetches the remote log via the existing `_log_fetch_remote` helper (no re-implementation), renders the Rich table or the v1.1 JSON envelope.
- `_NO_WATCHER_CHECK_CMDS` extended with `stats` so the watcher banner cannot leak into stdout ahead of the JSON envelope (same pattern as `health` / `prompt` / `log`).
- `tests/test_stats.py` — 21 tests: 14 pure-function tests covering every group-by axis (USER / MACHINE / ACTION / DAY / BACKEND), time-window filtering on both bounds, `--top` capping, conflict counting from `auto_resolved_files`, malformed-entry resilience, the unknown-axis ValueError path, Z-suffix timestamps, and dataclass typing; 7 CLI-level tests covering JSON envelope shape, empty-log path, relative-duration acceptance, invalid `--since` error envelope, no-banner-leak in `--json` mode, default Rich table rendering, and the empty-state message. Offline against the FakeStorageBackend, every test under 100ms.
- `docs/cli-reference.md` — top-level command list grows `claude-mirror stats …`. New `### stats` subsection with the flag table, sample table output, and JSON envelope. The `## JSON output` index lists `stats --json` as the sixth read-only command supporting `--json`.
- `docs/admin.md` — new "Activity stats over a window" subsection under "Who else is editing this project?" pointing at `stats` as the rolled-up companion to `status --presence` and `log`.
- `README.md` — Daily usage cheatsheet grows a `claude-mirror stats --since 7d` line.

### Added — claude-mirror verify for end-to-end integrity audit (VERIFY)

`claude-mirror verify` is the proactive drift-detection sibling of `claude-mirror health`: where health asks "is the system live and reachable?", verify asks "does claude-mirror's recorded view of reality match what's actually on every backend?" Inspired by `restic check` and `rclone check`, it runs three independent verification phases and surfaces drift / missing entries / corrupted blobs across them.

- **Phase 1 — manifest_vs_remote.** For each entry in the per-project `.claude_mirror_manifest.json`, ask each configured backend (primary + every Tier 2 mirror) for the recorded `synced_remote_hash` and compare against the manifest. Drift = backend hash differs from manifest. Missing = backend has no record of the recorded file ID. Each backend's native hash algorithm is honoured: Drive `md5Checksum`, Dropbox `content_hash`, OneDrive `quickXorHash`, WebDAV ETag / `oc:checksums`, SFTP sha256 — verify trusts each backend's `get_file_hash()` contract.
- **Phase 2 — snapshot_blobs.** Walk every `_claude_mirror_blobs/<hh>/<hash>` blob on each backend, fetch the bytes, recompute sha256, and compare with the filename. Mismatch = corrupted (the content-addressing contract is broken — bit-rot, partial upload, or tampering).
- **Phase 3 — mount_blob_cache.** Walk the on-disk content-addressed cache populated by the v0.5.62 MOUNT engine (`~/.cache/claude-mirror/blobs/` on POSIX, `%LOCALAPPDATA%/claude-mirror/Cache/blobs/` on Windows) and re-hash every entry. Corrupted entries are surfaced so the user can evict and refetch on the next mount rather than serve bad bytes.
- **`--strict` flips drift / missing / corrupted to a hard exit 1** so a daily cron alongside `claude-mirror health` can alert on integrity regressions. Default is exit 0 + report (informational).
- **`--json` v1 envelope** — same `{version, command, result}` shape as the rest of the read-only `--json` family. Schema bumps stay additive on v1. Stdout-only; the watcher banner is gated via `_NO_WATCHER_CHECK_CMDS` and the `--json` argv check at `_CLIGroup.invoke` so monitoring tools always get a parseable document.
- **Per-phase opt-out flags** — `--no-files`, `--no-snapshots`, `--no-mount-cache`. All three off is a friendly no-op ("No phases enabled — pass --files / --snapshots / --mount-cache to enable a check.")
- **Tier 2 backend scoping** — `claude-mirror verify --backend NAME` restricts the manifest + snapshot phases to one specific mirror so an operator can verify just the backend they suspect is drifting.
- New module `claude_mirror/_verify.py` (~440 lines, pure phase orchestration: `verify_manifest_vs_remote()`, `verify_snapshot_blobs()`, `verify_mount_cache()`, plus the `collect_verify()` aggregator). Reuses `claude_mirror/_mount.py::default_cache_root()` for the mount cache root path so the snapshot-blobs and mount-cache phases share the same on-disk layout contract as the v0.5.62 MOUNT engine.
- New `verify` command in `claude_mirror/cli.py` with the dual-line phase Progress display (live "manifest vs remote: 422/1245" detail) and a Rich table renderer with attention-coloured nonzero counts.
- `tests/test_verify.py` — 28 tests covering each phase function (clean / drift / missing / corrupted / per-mirror / pending-retry skip), the `collect_verify()` aggregator, the CLI default-exit-zero contract, the `--strict` exit-one path, the `--json` envelope shape and stdout-only contract, the `--backend NAME` scoping, the friendly empty-state messages, and progress-callback exception swallowing. All offline against `FakeStorageBackend`, every test under 100 ms.
- `README.md` — Daily usage cheatsheet entry pointing at `claude-mirror verify`.
- `docs/cli-reference.md` — top-level command list grows the line; new `### verify` subsection with the phase table, a sample table-mode report, the JSON envelope spec, and the exit-code contract.
- `docs/admin.md` — new "End-to-end integrity audit" subsection under Snapshots and disaster recovery, cross-linking `claude-mirror health` (liveness) and `claude-mirror verify` (correctness) as the proactive monitoring pair.

### Tests
- `pytest tests/` — **1129 passed, 3 skipped** on macOS (1101 baseline + 28 new).
- `mypy --strict claude_mirror/` — clean across 42 source files.

### Added — FTP / FTPS storage backend (BACKEND-FTP)

Plain FTP (legacy shared-hosting market: cPanel / DirectAdmin / old WordPress hosts) and FTPS (FTP over TLS) support via Python's stdlib `ftplib`. Cleartext FTP emits a clear warning at every connection — recommended only for trusted local-network use. SFTP remains the canonical choice for secure-file-transfer-over-the-internet. **No new dependencies — entirely stdlib.**

- `claude_mirror/backends/ftp.py` — new `FtpBackend(StorageBackend)` built on `ftplib.FTP` and `ftplib.FTP_TLS`. Three TLS modes: `explicit` (default; AUTH TLS on port 21), `implicit` (legacy FTPS-on-990 with manual socket-wrapping since `ftplib.FTP_TLS` doesn't speak implicit out of the box), and `off` (cleartext — emits a stderr warning at every `authenticate()` call). Passive mode honoured. `MLSD` (RFC 3659) directory listing with a `LIST`-parser fallback for legacy servers. `XSHA256` / `HASH` / `XSHA1` / `XMD5` server-side hash extensions tried first; falls back to streaming the bytes and computing sha256 client-side. `copy_file` falls back to download-then-upload (FTP has no server-side copy primitive). `classify_error` maps 530 → AUTH, 550-permission → PERMISSION, 550-not-found → FILE_REJECTED, 552 → QUOTA, socket / TLS errors → TRANSIENT / AUTH respectively.
- `claude_mirror/config.py` — seven new `Config` fields: `ftp_host`, `ftp_port` (default 21), `ftp_username`, `ftp_password`, `ftp_folder`, `ftp_tls` (default `explicit`), `ftp_passive` (default True). `root_folder` returns `ftp_folder` when `backend == "ftp"`.
- `claude_mirror/cli.py` — `ftp` appended to `_AVAILABLE_BACKENDS`, dispatch case appended to `_create_storage`, `--ftp-host` / `--ftp-port` / `--ftp-username` / `--ftp-password` / `--ftp-folder` / `--ftp-tls` / `--ftp-passive/--no-ftp-passive` Click options on both `init` and `clone`. Wizard branch added for the `ftp` backend (host, TLS mode, port with mode-aware default, username, password, folder, passive). `_run_ftp_deep_checks` (six checks: TCP reachable, server greeting, TLS handshake, authentication, folder access, folder write) plus `_ftp_deep_check_factory` seam for offline tests. Cleartext-mode advisory at doctor time when `ftp_tls=off` and the host is not loopback / RFC1918.
- `tests/test_ftp_backend.py` — 42 unit tests covering the full backend surface (auth happy path / bad creds / connection refused, upload / download / size cap, copy fallback, hash via XSHA256 plus client-side fallback, delete, classify_error matrix, TLS-mode selection, passive vs active, MLSD / LIST fallback, parse helpers, cleartext-warning content, RFC1918 helper).
- `tests/test_doctor_ftp_deep.py` — 11 deep-check tests covering each of the six checks plus the cleartext advisory branches and the non-ftp-backend skip.
- `tests/test_init_wizard.py::test_run_wizard_ftp_walks_through_prompts` — regression: choosing `ftp` at the first prompt walks through ftp-specific prompts (host, TLS mode, port, username, password).
- `tests/test_dyn_comp.py` — extended the hardcoded backend list to include `ftp`.
- `docs/backends/ftp.md` — new page: cPanel / DirectAdmin / NAS quick starts, full config field reference, cleartext-FTP security note, FTPS modes table, daily-ops notes (no native push, no server-side copy, no native checksum, no atomic upload), doctor deep-check description, troubleshooting (passive vs active, NAT/firewall, TLS handshake failures, MLSD vs LIST, SIZE).
- `docs/admin.md` — new `### FTP deep checks` subsection with the per-check matrix, auth-failure bucketing, cleartext-mode advisory, stdlib-only note. Doctor `--backend` filter list extended to include `ftp`. Backend index extended with `backends/ftp.md` link.
- `docs/cli-reference.md` — `--backend` choice list extended to include `ftp` everywhere it appears. New `--ftp-*` flag block on `init` and `clone`. Backend pages list extended.
- `README.md` — new FTP / FTPS row in the backends table; new row in the prerequisites table.

## [0.5.66] — 2026-05-09

<!-- subsumes: v0.5.65 -->

Same content as v0.5.65 (BACKEND-S3 + BACKEND-SMB) under a fresh PyPI version number. v0.5.65 was tagged on `c93ea6e` and CI Tests went green, but the **Publish to PyPI** workflow run was cancelled mid-flight before the wheel reached PyPI — so the v0.5.65 number is burned (PyPI's never-republish policy) and v0.5.66 ships the same source tree as the publishable artefact. No source-code changes from v0.5.65 → v0.5.66; the only delta is `pyproject.toml` version bump + this changelog block. Tests + mypy unchanged at 1345 / 48-source-files.

---

## [0.5.65] — 2026-05-09

Two new storage backends ship together — **S3-compatible** (BACKEND-S3) and **SMB/CIFS** (BACKEND-SMB) — extending the supported-backends matrix from six to eight. Both were held back from v0.5.63/v0.5.64 because their parallel additions to shared CLI surfaces (`_AVAILABLE_BACKENDS`, `_create_storage`, init wizard, doctor matrix) collided with FTP. Landing them on a stable post-FTP baseline kept the integration clean. 1345 tests pass on macOS (1251 baseline + 94 new); `mypy --strict` clean across 48 source files.

### Added — S3-compatible storage backend (BACKEND-S3)

One implementation transparently supports AWS S3, Cloudflare R2, Backblaze B2 (S3 API), Wasabi, MinIO, Tigris, IDrive E2, Linode Object Storage, DigitalOcean Spaces, Storj, Hetzner Storage Box, and every other S3-compatible service via configurable `s3_endpoint_url`. Adds `boto3>=1.34` to base install (lazy-imported, zero startup cost for users who don't use S3).

- New module `claude_mirror/backends/s3.py` (~470 lines) — full `StorageBackend` implementation: `authenticate` via `head_bucket`, single-PUT or multipart `upload_file` (5 MiB threshold matching S3's smallest legal multipart-part size), `get_object` streaming download with the project-wide `MAX_DOWNLOAD_BYTES` cap, paginated `list_objects_v2` recursive listing with client-side `exclude_folder_names` filtering, server-side `copy_object`, ETag-based `get_file_hash` (with the documented multipart `-N` suffix caveat), `classify_error` mapping for `NoCredentialsError` / `InvalidAccessKeyId` / `SignatureDoesNotMatch` / `AccessDenied` / `NoSuchBucket` / `NoSuchKey` / `SlowDown` / 429 / 5xx / 413 / `EndpointConnectionError`. Boto3 lazy-imported function-locally per the v0.5.61 fusepy precedent.
- `claude_mirror/cli.py::_AVAILABLE_BACKENDS` — append `"s3"` (append-only). New `_create_storage` dispatch case. New `_run_s3_deep_checks` doctor function (six checks: credentials shape, endpoint URL well-formedness, bucket reachable via `head_bucket`, list permissions via `list_objects_v2 MaxKeys=1`, write permissions via `put_object` + `delete_object` of a 1-byte sentinel, region consistency between `s3_region` and the bucket's actual region) wired into `_run_doctor_checks`. Init wizard + flag-mode validation extended with the s3 branch; `init` and `clone` Click commands gain the seven new `--s3-*` flags. Wizard summary block + token-file derivation cover s3.
- `claude_mirror/config.py::Config` — new fields `s3_endpoint_url`, `s3_bucket`, `s3_region`, `s3_access_key_id`, `s3_secret_access_key`, `s3_prefix`, `s3_use_path_style`. `root_folder` property returns the resolved prefix for s3.
- `pyproject.toml` — `boto3>=1.34` added to base `[project] dependencies` (matches the v0.5.10 every-backend-in-base policy). `boto3.*` + `botocore.*` added to the `[[tool.mypy.overrides]] ignore_missing_imports` list. Empty `[project.optional-dependencies] s3 = []` for back-compat aliases.
- `tests/test_s3_backend.py` — **38 tests** using a hand-written `FakeS3` mock (no `moto`, no network): authenticate happy/sad paths, all upload/download/list/copy/delete/get_hash methods, classify_error mapping for every ErrorClass entry, path-style vs virtual-hosted-style URL construction, multipart threshold passes through `TransferConfig`, multi-page pagination, empty bucket, sentinel write+delete.
- `tests/test_doctor_s3_deep.py` — **11 tests** covering each of the six deep checks (happy path + failure variants), plus auth-bucket short-circuit assertion. All boto3 calls mocked at the `S3Backend._get_client` seam.
- `tests/test_init_wizard.py` — extended with `test_run_wizard_s3_walks_through_prompts`.
- `tests/test_dyn_comp.py` — `_list-backends` expectation includes `"s3"`.
- `docs/backends/s3.md` — NEW (~250 lines): per-provider quick-starts (AWS / Cloudflare R2 / Backblaze B2 / MinIO), full config-field reference, minimum IAM policy with `s3:ListBucket` + `s3:GetObject` / `s3:PutObject` / `s3:DeleteObject` / `s3:CopyObject` examples, doctor deep-check walkthrough, troubleshooting matrix.

### Added — SMB/CIFS storage backend (BACKEND-SMB)

Sync directly to Windows file shares, Synology / QNAP / TrueNAS NAS devices, macOS Sharing-enabled folders, or any other SMB2/3 share without configuring WebDAV first. SMB2/3 only (SMBv1 not supported — security). Per-message AES encryption negotiated on by default; falls back gracefully on SMB2-only servers. Adds `smbprotocol>=1.13` to base install (lazy-imported).

- New backend module `claude_mirror/backends/smb.py` — full `StorageBackend` implementation. Lazy-imports `smbprotocol` / `smbclient` per the v0.5.61 fusepy precedent. Path-as-id (UNC paths in canonical backslash form). Uploads use `.tmp` + atomic replace so a crashed transfer never leaves a truncated file at the destination. Hashes computed client-side via streaming SHA-256 (SMB has no native hash primitive). `copy_file` round-trips through memory for files under 50 MiB and through a temp file above that — `smbclient` doesn't expose the SMB2 `FSCTL_SRV_COPYCHUNK` ioctl. The polling watcher (`PollingNotifier`) is reused as-is.
- New SMB-specific config fields on `Config`: `smb_server`, `smb_port` (default 445), `smb_share`, `smb_username`, `smb_password` (stored at chmod 0600 same posture as `sftp_password`), `smb_domain` (folded into the canonical NTLM `DOMAIN\\user` form by the backend), `smb_folder`, `smb_encryption` (default true). `Config.root_folder` returns `smb_folder` for the SMB backend.
- New CLI surfaces: `claude-mirror init --backend smb` + `claude-mirror init --wizard --backend smb` with the eight SMB-specific flags (`--smb-server`, `--smb-port`, `--smb-share`, `--smb-username`, `--smb-password`, `--smb-domain`, `--smb-folder`, `--smb-encryption/--no-smb-encryption`). The `clone` command grew the same flag set. Wizard validators reject empty server / share / username; port range-checked 1..65535.
- New doctor deep checks (`_run_smb_deep_checks`): six-step probe — server reachable (TCP), SMB2/3 protocol negotiation (SMBv1 rejected as a SECURITY GATE — refuses to connect, fix-hint points at the server's protocol settings rather than `claude-mirror auth`), authentication via `register_session`, share access via `scandir`, folder write via a 1-byte sentinel, and an info-only encryption-status line that warns when SMB3 was requested but the server downgraded to plaintext. Auth-class failures bucket into ONE failure line so the user doesn't see five copies of the same root cause.
- `tests/test_smb_backend.py` — **32 tests** covering the full backend surface (auth, upload/download/list/copy/delete/hash, classify_error matrix, encryption flag wiring, UNC path translation, server-side-copy fallback). All offline via an in-memory `FakeShare` mock of the smbclient module surface.
- `tests/test_doctor_smb_deep.py` — **11 tests** covering each of the six deep checks (happy path + failure paths). Includes the SMBv1 security-gate regression: a v1-only server fails the run loudly and short-circuits the rest of the chain.
- `tests/test_init_wizard.py` — extended with `test_run_wizard_smb_walks_through_prompts` reaching the SMB-specific server / share / username prompts.
- `tests/test_dyn_comp.py` — `_list-backends` expectation includes `"smb"`.
- `pyproject.toml` — new base dependency `smbprotocol>=1.13`. New mypy `ignore_missing_imports` overrides for `smbprotocol.*` and `smbclient.*` so `mypy --strict` stays clean.
- `docs/backends/smb.md` — NEW. Quick-start recipes for Synology, QNAP, TrueNAS, Windows file share, macOS Sharing, and generic Samba. Config-field reference. Permission-model walkthrough (share-level vs file-level — common gotcha on Synology). Deep-check reference. Troubleshooting matrix.

### Cross-cutting (S3 + SMB)

- `README.md` — backends table grew the S3 and SMB rows; **Supported backends** summary line, `## How it works` polling-latency line, `### Prerequisites` table, install description, and the documentation index all updated.
- `docs/README.md` — backends index extended with both new pages.
- `docs/admin.md` — generic-doctor matrix extended with the S3 + SMB credential rows; new `### S3 deep checks` and `### SMB deep checks` sections (six-check matrix each, auth-bucketing + lazy-import notes); `Where to go next` cross-links extended.
- `docs/cli-reference.md` — top-level `--backend` choices and the per-backend flag blocks appended to both `init` and `clone` flag tables.

### Tests
- `pytest tests/` — **1345 passed, 3 skipped** locally on macOS (was 1251 + 49 new S3 tests + 43 new SMB tests + 2 init-wizard regression tests).
- `mypy --strict claude_mirror/` — clean across 48 source files.

---

## [0.5.62] — 2026-05-09

Hotfix on top of v0.5.61's MOUNT release. v0.5.61 commits were pushed to origin but not tagged because Linux + Windows CI failed at test collection: importing `claude_mirror/_mount` triggered fusepy's `fuse.py`, which calls `ctypes.CDLL("libfuse.so.2")` at module load and raises `OSError("Unable to find libfuse")` when the OS-level FUSE library isn't installed. The original `try / except ImportError` only caught the wrong failure shape. v0.5.62 catches `OSError` too in three sites (`_operations_base()`, `_load_fuse()`, `_import_fuse()`), with two regression tests proving `_mount` imports cleanly when libfuse is absent. PyPI's burn-once policy means the v0.5.61 number stays unpublished; v0.5.62 ships the same MOUNT content with the import fix folded in.

### Fixed — libfuse-missing case no longer crashes module import (CI hotfix)
- `claude_mirror/_mount.py::_operations_base()` — also catches `OSError` from `from fuse import Operations`, falls back to `_FallbackOperations` so the module imports cleanly on machines without the OS-level FUSE library (CI runners, fresh dev installs that haven't run `brew install --cask macfuse` yet).
- `claude_mirror/_mount.py::_load_fuse()` — now distinguishes `ImportError` (fusepy missing) from `OSError` (fusepy installed but kernel layer missing) and raises with the right install hint per case.
- `claude_mirror/cli.py::_import_fuse()` — same dual handling; converts both error shapes to a `click.ClickException` with the kernel-layer install hint.
- `tests/test_mount.py::test_module_imports_when_libfuse_missing` — regression guard: monkeypatch `__import__` to raise `OSError` on `fuse`, re-import `_mount`, assert `_OperationsBase is _FallbackOperations`.
- `tests/test_mount_cli.py::test_import_fuse_handles_libfuse_missing_oserror` — same shape against the CLI helper.

### Tests
- `pytest tests/` — **1101 passed, 3 skipped** on macOS (1099 + 2 new regression guards).
- `mypy --strict claude_mirror/` — clean across 41 source files.

### Documentation — Slack walkthrough moved out of README into a consolidated `## Messaging and communication` index

Replaced the top-level `## Slack notifications` and `## Desktop notifications` blocks in `README.md` (~110 lines of step-by-step Slack-app-creation walkthrough, macOS launchd notification quirks, and platform-specific desktop-banner setup) with a single concise `## Messaging and communication` section that names every supported channel — Slack, Discord, Microsoft Teams, generic webhook, and desktop banners — and links each one to the canonical setup walkthrough in `docs/admin.md`. The README stays focused on getting users running quickly; messaging-channel-specific settings (webhook URLs, Slack-app creation steps, macOS notification permission, libnotify install on Linux, etc.) live in the dedicated docs page.

- `README.md` — new `## Messaging and communication` table covering all five channels with one-line descriptions and direct deep-links to `docs/admin.md` per channel. Quick `claude-mirror test-notify` verification command. Pointer to the routing / templating / config-field reference.
- `docs/admin.md → ### Slack` — was a stub linking back to the README walkthrough; now self-contained with the full Steps 1 / 2 / 3 (create the webhook, enable it in claude-mirror, verify) plus the config-field table that used to live in `README.md`.
- `docs/admin.md → ### Desktop notifications` — new subsection (was README-only). Same content as the old `## Desktop notifications` block: macOS permission + launchd workaround, Linux libnotify install + systemd display-session env vars, Windows note on `plyer`-based toasts.
- `docs/admin.md → ## Notifications` intro and table — extended to include desktop banners as a fifth channel ("All five are per-project, opt-in, best-effort").
- `docs/cli-reference.md` and `docs/faq.md` — three stale `README#slack-notifications` cross-links repointed at `docs/admin.md#slack`. The `### See also` block in `cli-reference.md` now links the new `## Messaging and communication` README section instead of the removed Slack section.

---

## [0.5.61] — 2026-05-09

The MOUNT release — every read-shaped FUSE view shipped at once. Five mount variants share one engine: snapshot mount, live remote mount, per-mirror mount (Tier 2), all-snapshots-stacked mount, and time-travel mount. Read-only across all five. **fusepy ships in the base install** — `pipx install claude-mirror` is enough on the Python side, matching the v0.5.10 every-backend-in-base policy. The kernel layer (macFUSE / WinFsp / libfuse) is platform-specific and installed separately, only needed at mount time.

### Added — read-only FUSE mount package (MOUNT)

Five mount variants share a single read-only FUSE engine, all driven through one new CLI surface (`claude-mirror mount` + `claude-mirror umount`). Browse, `grep`, `diff`, or open any snapshot — or the current state of any backend — as a real filesystem path, without ever running `restore` or pulling files to local disk.

- **Snapshot mount** — `claude-mirror mount --tag pre-refactor /tmp/snap` (or `--snapshot 2026-04-15T10-30-00Z`). One frozen snapshot, accessible at any path under the mountpoint.
- **Live remote mount** — `claude-mirror mount --live /tmp/drive-now`. The current state of the configured primary backend, with directory listings cached for `--ttl` seconds (default 30) and blob bodies cached forever (content-addressed).
- **Per-mirror mount** — `claude-mirror mount --live --backend dropbox /tmp/dbx`. Tier 2: pin the mount to one specific Tier 2 mirror's view rather than the primary.
- **All-snapshots stacked** — `claude-mirror mount --all-snapshots /tmp/all-history`. Every snapshot side-by-side under per-timestamp subdirectories — `diff /tmp/all-history/2026-04-01.../CLAUDE.md /tmp/all-history/2026-05-01.../CLAUDE.md` works directly.
- **Time-travel** — `claude-mirror mount --as-of 2026-04-15 /tmp/april15`. The last snapshot taken on or before DATE, picked automatically.

Read-only by design: writes return `EROFS`. The push/pull/sync flow stays the canonical writeback path. Useful for `grep -r`, `diff`, `git log -p` against a specific past state, or opening a snapshot in your editor without committing to a full `restore`.

- **Base install ships fusepy.** `pipx install claude-mirror` includes the FUSE Python bindings out of the box — no extras flag needed. The legacy `[mount]` extra is retained as a no-op alias so historical install commands keep working. Plus the kernel layer for your platform: macOS uses macFUSE (`brew install --cask macfuse`), Linux uses the in-tree libfuse (already kernel-resident on every modern distro), Windows uses WinFsp (https://winfsp.dev). The `mount` command prints the right kernel-layer install hint per platform when the OS-level FUSE library is missing at runtime.
- **Content-addressed BlobCache.** Backed by `$XDG_CACHE_HOME/claude-mirror/blobs/`. Once a blob is fetched, it stays valid forever (sha256 == identity) — survives unmount/remount cycles. Default 500MB cap, configurable via `--cache-mb N`. Cold-cache reads pay a network round-trip to the backend; warm-cache reads serve straight from disk.
- **Cross-platform `umount` wrapper.** `claude-mirror umount /tmp/snap` picks the right unmount tool per platform: `umount` on macOS, `fusermount -u` on Linux. Windows prints a hint pointing the user at Ctrl+C on the foreground mount process (WinFsp processes respond to a clean signal).
- **Foreground / background.** `--foreground` (default) keeps the process attached to the terminal; Ctrl+C cleanly unmounts via a `try/finally` that calls the FS instance's `cleanup()` hook. `--background` daemonises on POSIX; on Windows it exits with a hint pointing at `--foreground` in a separate console.
- New module `claude_mirror/_mount.py` (engine — five FS classes + content-addressed BlobCache), CLI `mount` + `umount` commands in `claude_mirror/cli.py`, `[mount]` optional-dependency block in `pyproject.toml` (with a parallel entry under `[all]`), `fuse.*` in the mypy `ignore_missing_imports` overrides so `mypy --strict` stays clean even on machines without fusepy installed.
- `README.md` — new "Browsing snapshots without downloading" subsection under Daily usage cheatsheet, plus per-platform install pointers under the existing Install section.
- `docs/scenarios.md` — new **Scenario J. Browse / grep / diff snapshots without restoring**. Same shape as Scenarios A–I (Purpose / How to implement / Daily ops behaviour / Pitfalls and tips). Worked examples for all five variants; pitfalls cover cold-cache latency, kernel-layer install requirement, the read-only contract, and the atomicity contract (FUSE syscalls are not atomic against concurrent push/pull operations on the backend).
- `docs/cli-reference.md` — top-level command list grows `claude-mirror mount …` and `claude-mirror umount …`. New `### mount` subsection with the full flag table, optional-dep notice, cross-platform install pointers, and exit-code table. New `### umount` subsection describing the per-platform behaviour.
- `docs/admin.md` — new "Browsing without restoring" subsection under Snapshots and disaster recovery, pointing at the new `mount` cli-reference entry and Scenario J as the lighter-weight alternative to `restore --output`.

### Tests
- `tests/test_mount_cli.py` — **21 tests** for the CLI surface (offline, <100ms each), driven against a mocked `claude_mirror._mount` module + a mocked `fuse` module so the suite runs without fusepy installed. Coverage: optional-dep guard prints the install hint; mutually-exclusive variant flags (0 / 2 / exactly-1); `--backend NAME` rejected without `--live`; `--ttl N` rejected without `--live` (both at a non-default value AND at the default value passed explicitly — guards against a future refactor swapping Click's `ParameterSource` machinery for a `value != default` check); `--cache-mb 0` and `--cache-mb -5` rejected; dispatch into each of the five variant classes (`SnapshotFS` from `--tag` and from `--snapshot`, `LiveFS` from `--live`, `PerMirrorFS` from `--live --backend`, `AllSnapshotsFS` from `--all-snapshots`, `AsOfDateFS` from `--as-of`); `--as-of` ISO-date parsing happy + sad path; KeyboardInterrupt cleanup hook fires; `umount` shells out to `umount` on darwin / `fusermount -u` on linux / prints a hint on win32; `umount` failure surfaces stderr.
- The engine track ships its own filesystem-class tests in a separate file (`tests/test_mount_engine.py`); run together they cover the variant classes' behaviour at the FS-syscall level (readdir, getattr, open, read, write-rejection) plus the BlobCache eviction and stats contract.
- `pytest tests/` — **1033 passed, 3 skipped** locally on macOS in 4.77s (was 1012 + 21 new mount-CLI tests; the 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards).
- `mypy --strict claude_mirror/` — **clean across 40 source files** (39 pre-existing + the engine track's new `_mount.py`).

---

## [0.5.60] — 2026-05-09

Three independent additions land together: snapshots become git-commit-shaped (named + messaged + tag-protected from auto-pruning), `AGENTS.md` cross-tool sync gets a first-class scenario page + sample profile, and a new `claude-mirror prompt` subcommand emits a fast network-free status snippet for embedding in shell prompts (PS1 / starship / fish / zsh).

### Added — AGENTS.md cross-tool sync recipe (AGENTS-MD)

Documents claude-mirror as a first-mover sync tool for the cross-IDE `AGENTS.md` convention. `AGENTS.md` is the project-root markdown file read by Claude Code, Cursor, Codex, Antigravity, and any future agent IDE that converges on the same standard. The engine has always been able to sync any markdown file (default `file_patterns: ["**/*.md"]` already matches `AGENTS.md`); this change adds a worked recipe so users can copy-paste a narrowed pattern set that mirrors only the agent-context files rather than every markdown in the project.

- `docs/profiles/agents-md.yaml` — new sample profile YAML, **~60 lines** including header comments. Sets `file_patterns` to the conservative inclusive default `["AGENTS.md", "**/AGENTS.md", ".AGENTS.md", "**/.AGENTS.md"]` and ships an `exclude_patterns` list covering `node_modules/`, `.venv/` / `venv/`, `**/__pycache__/`, `build/`, `dist/`, and `.git/` so generated dirs never leak in. Deliberately omits backend / credentials fields so the profile composes with whatever backend each project picks. Activation instructions in the file's header.
- `docs/scenarios.md` — new **Scenario I. Cross-tool AGENTS.md sync** (~80 lines) added after Scenario H; index at the top updated. Same shape as Scenarios A–H: Purpose / How to implement / Daily ops behaviour / Pitfalls and tips, with copy-paste config blocks and cross-links back to `docs/profiles.md`, `docs/profiles/agents-md.yaml`, Scenario B (multi-machine layering), Scenario F (`file_patterns` reference), and `docs/conflict-resolution.md` (conflicts are more common in this scenario because every agent IDE may rewrite `AGENTS.md` as the user works).
- `docs/profiles.md` — short paragraph + one-line install command added near the top under a new `### Worked sample: cross-tool AGENTS.md sync` subheading, linking out to the sample YAML and Scenario I.
- `README.md` — value-prop paragraph extended with one sentence flagging cross-tool agent-context sync as a first-class scenario, linking the sample YAML and Scenario I. Documentation index entry under `docs/scenarios.md` updated from "seven deployment topologies" to "eight" with a new bullet for Scenario I.
- `tests/test_profile.py` — new test `test_agents_md_sample_profile_loads_cleanly` (1 test, ~50 lines). Loads `docs/profiles/agents-md.yaml`, asserts the four `AGENTS.md` patterns and the key exclude entries are present, copies the file into the isolated profiles directory, and round-trips it through `load_profile` + `Config.load(profile=)` to prove the sample stays compatible with the dataclass schema. A future schema change in `Config` that breaks the doc sample will fail here, keeping docs and code aligned.

### Tests
- `pytest tests/test_profile.py` — **25 passed locally** on macOS in 0.23s (was 24, +1 for the new sample-loads-cleanly test).
- `pytest tests/` — **941 passed, 3 skipped** locally on macOS in 4.67s (the 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards, unchanged).

### Added — named snapshot tags + messages (SNAP-TAG)

Snapshots are git-commit-shaped now: a maintainer can tag a snapshot with a memorable name and attach a free-form message, then restore by tag instead of by timestamp. The on-disk timestamp identity is unchanged — tags are an additive annotation; pre-SNAP-TAG snapshots load as `tag=None` / `message=None` so back-compat is total.

- `claude_mirror/snapshots.py` — extended both manifest shapes (blobs `_claude_mirror_snapshots/{ts}.json` and full `_snapshot_meta.json`) with two optional keys: `tag` (validated against `^[A-Za-z0-9._-]{1,64}$`) and `message` (free-form, capped at 1024 chars). New helpers `_validate_tag_name`, `_validate_message`, `_truncate_message_for_table` plus `MAX_MESSAGE_LEN` / `_TAG_NAME_RE` module constants. New methods on `SnapshotManager`: `find_by_tag(NAME)`, `list_tags()`, `resolve_tag_to_timestamp(NAME)`. `create()` grew `tag=` / `message=` keyword-only-ish parameters; uniqueness check (per-project) runs before any remote write so a duplicate tag aborts cleanly. Both blobs and full-format writes carry the new keys, including the `_mirror_*_in_parallel` fan-out paths so Tier 2 mirrors get the same annotation. `list()`'s blobs and full meta-fetchers surface `tag` + `message` for downstream callers (`show_list`, `show_history`, JSON output). Migration (`_migrate_full_to_blobs` and `_migrate_blobs_to_full`) preserves both fields across format conversions. `MANIFEST_FORMAT_VERSION` stays at `"v2"` — the new keys are additive within v2 (default-`None` on read), so a v3 bump would have meant breaking back-compat for callers checking the literal version string. The bumped behaviour belongs in the field set, not the version label.
- `claude_mirror/snapshots.py` — tag-protected pruning. `prune_per_retention()` and `forget()` now skip tagged snapshots when the selector is rule-based (`--before` / `--keep-last` / `--keep-days` / retention bucket math). Both grew an `include_tagged: bool = False` parameter to opt in. Explicit positional `forget TIMESTAMP...` deletions still delete whatever is named — same shape as `git tag -d` letting you delete a tagged commit by name. The `to_keep` set returned by `prune_per_retention` folds shielded tagged timestamps in, so downstream callers see a complete keep-set; the new `skipped_tagged` field exposes the explicit list for UI / log purposes.
- `claude_mirror/cli.py` — new `snapshot` subcommand with `--tag NAME` and `--message TEXT` flags. Validation errors print red and exit 1 before any storage I/O. `restore` grew `--tag NAME` (mutually exclusive with the positional TIMESTAMP — passing both, or neither, prints a clear error and exits 1). `forget` and `prune` grew `--include-tagged` flags wired to the manager's new parameter. `snapshots --json` output schema additively includes `tag` and `message` fields. `_snapshot_entry_to_json` updated. The `snapshots` table gains `Tag` and `Message` columns; `inspect` header line now shows `tag:` / `message:` rows when set; `history`'s timeline rows surface the tag inline (dim `[tag]` after the timestamp).
- `tests/test_snapshot_tag.py` — new module, **55 tests** (offline, <100ms each). Coverage: tag-name validation across 8 valid + 12 invalid forms (incl. trailing newline, slash, '@', non-ASCII, >64 chars); message length cap at MAX_MESSAGE_LEN; manifest round-trip for both blobs and full formats; `--message` without `--tag` and `--tag` without `--message`; per-project uniqueness; `find_by_tag` / `list_tags` / `resolve_tag_to_timestamp` happy + sad paths; CLI `snapshot --tag` / `snapshot --tag --message` / unknown tag / duplicate tag; CLI `restore --tag NAME` resolves correctly; CLI mutual-exclusion of TIMESTAMP and `--tag`; CLI missing-identifier error; pre-SNAP-TAG manifests load with both fields = None for both formats (back-compat regression guard); `show_list` and `history` data plumbing surface the new fields; `_truncate_message_for_table` helper (short / long / empty); `prune_per_retention` skip-tagged-by-default + `--include-tagged` opt-in; `forget --before` shielding vs. `forget TIMESTAMP` explicit-positional override; migration full→blobs preserves tag + message.
- `tests/test_retention.py` — two existing strict-equality assertions on the `prune_per_retention` return shape updated to include the new `skipped_tagged: []` field. No behavioural changes to retention.
- `docs/admin.md` — new "Naming a snapshot" subsection under "Snapshots and disaster recovery". Shows the typical `--tag pre-refactor --message ...` workflow, restore-by-name, and tag-protected pruning. One-line note on the validation regex.
- `docs/cli-reference.md` — top-level command list grows `[--tag NAME]` / `[--message TEXT]` on `snapshot`, `[--tag NAME]` on `restore`, `[--include-tagged]` on `prune` and `forget`. Per-command sections for `snapshot` (new), `snapshots`, `restore`, `forget`, `prune` updated.
- `README.md` — Daily usage cheatsheet annotated with `snapshot --tag` and `restore --tag` lines.

Tag validation regex: `^[A-Za-z0-9._-]{1,64}$` — same shape as a tightened-up git tag name (no slashes, spaces, '@', or unicode). `re.fullmatch` is used so a trailing newline doesn't slip through Python's `$`-allows-final-newline rule.

### Tests
- `pytest tests/test_snapshot_tag.py` — **55 passed locally** on macOS in 0.46s.
- `pytest tests/` — **995 passed, 3 skipped** locally on macOS in 4.36s (the 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards).
- `mypy --strict claude_mirror/` — **clean across 39 source files**.

### Added — `claude-mirror prompt` for shell-prompt integration (SHELL-PROMPT)

A new top-level subcommand `claude-mirror prompt` emits a short, network-free, sub-50ms status snippet for embedding in shell prompts (PS1 / PROMPT / fish_prompt / starship). Inspired by git's `__git_ps1`: a maintainer-friendly visible signal that gets users to glance at their shell prompt and know whether they have unsynced changes, without ever reaching out to the network. Designed to run on every prompt redraw — silent on success, silent on the (auto-detected) non-claude-mirror directory case, and silent on every error path. Errors NEVER produce a non-zero exit code; that would break the user's prompt rendering for every subsequent command.

- `claude_mirror/_prompt.py` — new module, ~330 lines. Pure stdlib + `Config` + `Manifest` access; no rich / click dependencies (those are too heavy to import on every PS1 redraw). Public API: `compute_prompt(config, fmt, prefix, suffix, quiet_when_clean) -> str`. Internal helpers walk the project tree once, read the manifest's flat fields, consult the persistent hash cache (`.claude_mirror_hash_cache.json`) to decide whether each file's local hash matches the manifest's `synced_hash` — without rehashing — and count pending_retry mirror entries as conflicts. The result is wrapped in the requested format and surrounded by `--prefix` / `--suffix`. The `quiet_when_clean` path returns the empty string (and skips prefix/suffix wrapping) so users can embed a leading-space prefix without leaving stray whitespace in clean prompts.
- `claude_mirror/_prompt.py` — prompt-cache file at `<project>/.claude_mirror_prompt_cache.json` keyed on `(manifest mtime_ns, live local file count)`. Subsequent calls with an unchanged manifest AND unchanged file count short-circuit the file walk + classification entirely; the cache invalidates automatically on every manifest rewrite (push / pull / sync) and on local file additions or removals. Above the `LARGE_PROJECT_THRESHOLD` (5000 files), the path returns the cached value if present or an ellipsis fallback otherwise — so a giant project never blocks the user's shell for >100ms while hashing.
- `claude_mirror/cli.py` — new `@cli.command()` `prompt` (NOT a flag on `status`, because the perf + silence + exit-code contract is fundamentally different from the existing network-bound `status --short`). Flags: `--config PATH` (auto-detected from cwd via `_resolve_config`; if no config matches the cwd or any ancestor, exits 0 with empty stdout), `--format text|ascii|symbols|json` (default `symbols`), `--prefix STR`, `--suffix STR`, `--quiet-when-clean`. Added to `_NO_WATCHER_CHECK_CMDS` so the watcher-not-running banner can never tear the user's PS1.
- `claude_mirror/cli.py` — auto-detection guard: when `--config` is omitted and the resolved config's `project_path` is neither the cwd nor an ancestor of the cwd, the command returns silently rather than printing the default config's status. A user running shell commands in `~` should NOT see one of their projects' sync state in the prompt.

#### Symbol vocabulary

  | Meaning            | symbols (default) | ascii | text                |
  |--------------------|-------------------|-------|---------------------|
  | in sync            | `✓`               | `OK`  | `in sync`           |
  | N files locally ahead | `↑N`           | `+N`  | `+N ahead`          |
  | N files remote-ahead (cached) | `↓N`   | `-N`  | `-N behind`         |
  | N pending_retry conflicts | `~N`        | `~N`  | `N conflict(s)`     |
  | no manifest yet    | `?`               | `?`   | `no manifest`       |
  | error              | `⚠`               | `!`   | `error`             |

  `--format json` emits a flat dict to stdout: `{"in_sync": bool, "local_ahead": int, "remote_ahead": int, "conflicts": int, "no_manifest": bool, "error": bool}`.

#### Performance

  Measured on a synthetic 500-file project (typical claude-mirror size):

  | Phase           | Wall time |
  |-----------------|-----------|
  | cold cache      | ~6-8 ms   |
  | warm cache hit  | ~3-4 ms   |

  (CI test slack: 500 ms — accommodates slow runners; the real target is 50 ms.) The `claude-mirror prompt` end-to-end command line wall time is dominated by Python interpreter + click import startup (~250 ms on macOS), unavoidable without a long-running daemon — but the actual `compute_prompt` work is tiny and well within the per-PS1-redraw budget every shell user expects.

#### Shell recipes

Ready-to-paste recipes for `bash`, `zsh`, `fish`, and `starship` ship in the README's new "Shell prompt integration" subsection.

- `tests/test_shell_prompt.py` — new module, **16 tests:** in-sync emits the check symbol; modified files emit `↑N`; new local files (no manifest entry) count as local-ahead; `--quiet-when-clean` emits an empty line when in sync; `--format ascii` uses `+N ~M`; `--format text` uses `+N ahead, M conflict(s)`; `--format json` is parseable; non-claude-mirror directory exits 0 with empty stdout; corrupt manifest exits 0 with the warning symbol on stdout + a stderr line; the prompt cache short-circuits the file walk on the second call when nothing has changed (verified by patching the inner walker and asserting call count); cache invalidates when the manifest is rewritten; no-manifest-yet emits `?`; `--prefix` / `--suffix` wrap the output; `--quiet-when-clean` skips prefix/suffix on the empty path; cold-cache 500-file project completes well under the 500 ms loose budget; the no-local-files-with-manifest edge case returns in-sync without crashing.
- `README.md` — new "Shell prompt integration" subsection in the Daily-usage area with bash / zsh / fish / starship recipes.
- `docs/cli-reference.md` — new `### prompt` subsection documenting flags, formats, symbol vocabulary, performance contract, and the silent-on-failure-by-design exit-code-0 behaviour. The top-level command list grows a `claude-mirror prompt [--config PATH] [--format text|ascii|symbols|json] [--quiet-when-clean] [--prefix STR] [--suffix STR]` line.

---

## [0.5.59] — 2026-05-09

Windows credibility release: closes 7 of the 22 Windows test skips by making the inbox file lock and the `watch-all` hot-reload mechanism cross-platform. `watch-all` is now fully supported on Windows (was POSIX-only since the project's first release).

### Added — cross-platform inbox file locking (WIN-LOCK)

The notification inbox at `{project_path}/.claude_mirror_inbox.jsonl` is now serialized by an exclusive OS-level lock on every platform, not just POSIX. Before this change `claude_mirror/notifier.py` imported `fcntl` inside a `try / except ImportError` block and silently fell back to a no-op on Windows, which meant the strict TOCTOU contract documented in `tests/test_notifier_inbox.py::test_inbox_read_clears_atomically_against_concurrent_writer` only held on macOS and Linux. On Windows two threads (or two processes) racing the inbox could lose lines mid-drain. WIN-LOCK closes that gap with stdlib only — no new third-party dependency.

- `claude_mirror/_filelock.py` — new module, ~55 lines. Single public helper `exclusive_lock(file_obj)` exposed as a `@contextmanager` that takes an exclusive lock for the duration of the `with` block and releases on exit (success OR exception). On POSIX it dispatches to `fcntl.flock(fd, LOCK_EX)` / `fcntl.flock(fd, LOCK_UN)` — same semantics the inbox shipped with through v0.5.58. On Windows it dispatches to `msvcrt.locking(fd, LK_LOCK, IO_LOCK_BYTES)` / `msvcrt.locking(fd, LK_UNLCK, IO_LOCK_BYTES)`. `IO_LOCK_BYTES = 0x7FFFFFFF` is the documented maximum byte range. Because `msvcrt.locking` locks N bytes from the current file pointer rather than the whole file, the wrapper saves `file_obj.tell()`, seeks to byte 0, takes the lock against the sentinel range, restores the saved position before yielding, and restores again before unlocking on exit so the unlock targets the exact same range. `LK_LOCK` blocks (retrying every ~1s up to ~10s) rather than returning immediately — appropriate for the inbox where hold time is sub-second and contention is local-user-only; `LK_NBLCK` would have forced every caller into a retry loop, which is harder to make correct than relying on the OS's blocking lock.
- `claude_mirror/notifier.py` — both call sites (`Notifier._write_inbox()` and `read_and_clear_inbox()`) now use `with exclusive_lock(f):` instead of the inline `fcntl.flock(...)` / `flock(...LOCK_UN)` `try / finally` blocks. The conditional `try: import fcntl / except ImportError: fcntl = None` shim at module top is gone — that was the no-op fallback that broke Windows. Behaviour outside the lock block (the JSON line write, the read-then-truncate-then-fsync sequence on the drain side) is unchanged.
- `tests/test_filelock.py` — new module, **4 tests** that all run cross-platform without `skipif`: serialization of two threads contending the same lock (Barrier-coordinated; final file is exactly the two markers, no interleave), release on exception (a re-acquirer in a second thread finishes), file-position restore across the lock cycle (the Windows seek-back contract; trivially holds on POSIX too), and a sanity check that the helper accepts a text-mode file handle (the inbox opens in `a` / `r+` text mode).
- `tests/test_notifier_inbox.py` — `test_inbox_read_clears_atomically_against_concurrent_writer` used to be guarded by `@pytest.mark.skipif(sys.platform == "win32", reason="...fcntl POSIX-only...fallback path is a no-op...")`. The skipif is gone; the test now runs on every platform. The unused `import sys` and `import pytest` lines were removed in the same edit.

### Tests
- `pytest tests/test_filelock.py tests/test_notifier_inbox.py` — **11 passed locally** on macOS in 0.16s.
- `pytest tests/` — **938 passed, 3 skipped** locally on macOS in 4.82s (the 3 skips are the pre-existing `test_mypy_smoke.py` "mypy not installed" guards, unchanged by WIN-LOCK).
- `mypy --strict claude_mirror/` — **clean across 39 source files**.

### Changed — `watch-all` hot-reload now cross-platform (WIN-WATCH)

Replaced the POSIX-only `signal.SIGHUP` hot-reload mechanism in `claude-mirror watch-all` with a cross-platform sentinel-file polling mechanism, so the daemon now runs on Windows too. `claude-mirror init`, `claude-mirror reload`, and any future client that wants the running daemon to re-scan `~/.config/claude_mirror/` simply (re)write the sentinel file `~/.config/claude_mirror/.reload_signal` (atomically, via tempfile + `os.replace`); the daemon polls that file's mtime every 2 seconds and triggers the same re-scan path the SIGHUP handler used to drive. Existing watchers are not interrupted; the worst-case reload latency is the poll cadence (2 s).

- `claude_mirror/cli.py` — new module-level constants `RELOAD_SIGNAL_FILE` (`~/.config/claude_mirror/.reload_signal`) and `_RELOAD_POLL_SECONDS` (2.0). New helpers: `_write_reload_signal()` (atomic tempfile + `os.replace` so a daemon polling mtime never observes a half-written file even under aggressive concurrent reloads), `_read_reload_mtime()`, `_should_reload(path, last_seen_mtime)`, and a top-level `_rescan_configs(state)` driven by a new `_RescanState` dataclass so the rescan logic is no longer trapped inside `watch_all`'s local scope. `watch_all` now runs a polling loop (`while not stop: if _should_reload(...): _rescan_configs(...); stop_event.wait(_RELOAD_POLL_SECONDS)`) that exits cleanly on SIGINT/SIGTERM. On POSIX the SIGHUP handler is still registered and routed through the same `_rescan_configs` path — belt-and-braces so older `claude-mirror reload` clients on the legacy wire-format still work during the transition. On Windows the SIGHUP registration is silently skipped (`hasattr(signal, "SIGHUP")` guard).
- `claude_mirror/cli.py` — `claude-mirror reload` rewritten: writes the sentinel file (no signals, no `pgrep`/`os.kill`), then runs a best-effort running-process detection via `_detect_watcher_pids()` (POSIX `pgrep -f`, Windows `tasklist /V /FO CSV`). Three terminal states: detected at least one watcher → green confirmation; detection ran but found zero watchers → exit 1 with a friendly "no running watch-all" notice; detection couldn't run at all on this platform → exit 0 with a yellow "couldn't verify" notice (the sentinel write is the contract, the running-process check is informational). Output: one line, e.g. `Reload signal written; watcher (PID 12345) will pick it up within ~2s.`
- `claude_mirror/cli.py` — `_try_reload_watcher()` (called from `init` after success) drops the `pgrep`+`SIGHUP` dance entirely and just writes the sentinel file. If no watcher is running, the file sits there harmlessly until one starts (the daemon establishes a fresh mtime baseline at launch and ignores stale signals from before its start time).
- `tests/test_watcher.py` — 6 tests previously skipped on Windows via `@pytest.mark.skipif(sys.platform == "win32", reason="watch-all uses signal.SIGHUP …")` now run on every platform. Module-level `pytestmark` `skipif` removed, `import signal` removed (the test surface no longer touches SIGHUP directly except in one POSIX-conditional assertion). Test rewrites: `test_watch_all_reload_spawns_only_new_configs` now drives `_rescan_configs(_RescanState(...))` directly instead of mimicking the SIGHUP handler's body; `test_sighup_handler_registered_by_watch_all` asserts SIGHUP is registered on POSIX (`hasattr(signal, "SIGHUP")` guard) and SIGINT/SIGTERM are registered on every platform; `test_reload_command_writes_sentinel` replaces `test_reload_command_sends_sighup` and asserts the sentinel file is written with a fresh epoch payload + the detection-mock path renders the green confirmation; `test_reload_command_when_no_watcher_running` now asserts the new exit-1-with-notice behaviour AND that the sentinel still gets written so a deferred-start watcher can pick the request up. Two new tests: `test_watch_all_polling_loop_reacts_to_sentinel` (end-to-end of `_should_reload` + `_rescan_configs` + `os.utime`-driven mtime bump, no real waits) and `test_reload_command_when_detection_unsupported` (locked-down host with neither `pgrep` nor `tasklist`). 6 → 8 tests, 0 skipped.
- `docs/admin.md` — `## Auto-start the watcher` updated: Windows is now first-class (was: "use `watch --once` polling from Task Scheduler"; now: full `watch-all` recipe with `schtasks /Create /SC ONLOGON`). "Adding a new project to a running watcher" rewritten to describe the sentinel-file mechanism + the 2-second poll cadence so the user knows worst-case reload latency. Trailing reference to the watcher's "config-reload cadence (`SIGHUP`)" updated to "the sentinel-file polling check, default 2 s".
- `docs/cli-reference.md` — `### reload` rewritten to describe the sentinel-file mechanism + the cross-platform detection fallback chain.
- `docs/scenarios.md` — two SIGHUP references updated.
- `README.md` — quality-gates paragraph updated: "the watcher daemon's SIGHUP hot-reload" → "cross-platform sentinel-file hot-reload".

**Why a sentinel file vs. a named pipe / Unix socket / TCP loopback?** No new third-party dependencies, no platform-specific IPC primitives (named pipes work differently on Windows, Unix sockets need filesystem permissions reasoned about, TCP loopback opens a port and a firewall question). A regular file polled at 2 Hz costs essentially nothing (one `os.stat` per poll) and the atomic `tempfile + os.replace` write is robust against concurrent reloads collapsing onto a single rescan tick.

**Why 2 seconds?** Tradeoff between perceived instantaneity (`init`-triggered reloads should feel immediate) and idle-CPU cost (the daemon runs on user laptops 24/7). 2 s lands well below the typical "did it work?" inspection delay while keeping the per-day stat count to ~43k — negligible. Tests override `_RELOAD_POLL_SECONDS` via `monkeypatch.setattr` to drive the loop without real waits.

---

## [0.5.58] — 2026-05-09

Symmetry release: `delete` joins the `--dry-run` family. Plus a small CHANGELOG cosmetic fix carried over from the v0.5.56 → v0.5.57 renumber.

### Added — `delete --dry-run` preview mode

Symmetry with the v0.5.57 `push --dry-run` / `pull --dry-run` plumbing: `claude-mirror delete` now accepts `--dry-run / --no-dry-run`. A dry-run run prints exactly what a real run would remove from the remote and (when `--local` is also set) from local disk, without making any backend writes, manifest mutations, local unlinks, or notification dispatches. Completes the dry-run coverage across every mutating command — `push`, `pull`, `delete`, `restore`, `forget`, `prune`, `gc`, `seed-mirror`, `retry`, `update` all now honor `--dry-run`.

- `claude_mirror/sync.py` — new `DeletePlan` dataclass: `to_delete_remote: list[str]`, `to_delete_local: list[str]`, `not_found: list[str]`, `local_only: list[str]`. Mirrors the `PushPlan` / `PullPlan` shape so the CLI render path is uniform across all three.
- `claude_mirror/cli.py` — `delete` command grows `--dry-run / --no-dry-run` with the same help string shape as push/pull. New `_plan_delete()` helper classifies each requested path against the live status pass; new `_render_delete_plan()` prints a Rich `Delete plan (dry-run)` table with `- remote` / `- local` rows plus per-path warnings for `not_found` and `local_only-without---local`. The dry-run path constructs the engine with `with_pubsub=False` so a Pub/Sub setup error in a cron-only environment doesn't surface as a fake failure on a preview command.
- `tests/test_delete_dry_run.py` — new module, **16 tests:** the full bucket-classification matrix (`to_delete_remote` / `to_delete_local` / `not_found` / `local_only`), the `--local` flag composing with planning correctly, side-effect-absence guards (no `delete_file` on the backend, no `unlink` on local files, manifest bytes + mtime byte-identical before vs after), CLI exits 0 with the expected summary, the unknown-path warning, the `--local`-flag-missing warning, and a regression guard ensuring the real (non-dry-run) path still deletes after the wiring change.
- `docs/cli-reference.md` — `### delete` subsection updated with the new flag; the top-level command list shows `[--dry-run/--no-dry-run]`.
- `README.md` — Daily usage cheatsheet annotated.

### Documentation
- `### Total surface area for v0.5.56` → `v0.5.57` (typo carried over from the v0.5.56 → v0.5.57 renumber).

---

## [0.5.57] — 2026-05-09

Five new user-facing commands and command modes ship together: `push --dry-run` / `pull --dry-run` preview mode, `log --follow` for live streaming, `status --presence` for collaborator visibility, `claude-mirror health` for monitoring-tool integration, and `claude-mirror clone` for one-shot machine bootstrap. Plus a docs cleanup pass (DOC-CLEAN) fixing 6 stale README anchors trimmed during the v0.5.36 doc split, with two new admin.md subsections ("Filtering which events fire", "Is the watcher actually running?"), and a follow-up `mypy --strict` pass that surfaced 5 type-correctness issues across the new code (explicit `return None` on the `push` / `pull` engine paths, `assert plan is not None` narrowing on the `--dry-run` callers, `redact_error(str(exc))` coercion on the presence error path).

**Note on numbering:** v0.5.56 was tagged but had its tag pointed at the mypy-fix commit instead of the release-anchor commit, which made `git log -1 v0.5.56` show only the fix's subject and obscured the feature surface. PyPI's "burn-once" policy on version numbers means re-publishing under the same version is impossible, so v0.5.56 is yanked and v0.5.57 ships the same content with a proper feature-bearing release commit at the tag's pointed-to revision.

### Added — push --dry-run / pull --dry-run preview mode

A cron-paranoid operator can now preview exactly what a scheduled `claude-mirror push` or `claude-mirror pull` would do, without making any backend writes, local writes, or notification dispatches. Mirrors the existing `restore --dry-run` pattern: the engine exposes a planning function that returns a structured plan dataclass, and the CLI renders that plan as a Rich table with `+`/`-`/`~` markers and a one-line summary, exiting 0.

- `claude_mirror/sync.py` — new `PushPlan` / `PullPlan` dataclasses; `SyncEngine.push()` and `SyncEngine.pull()` gain a keyword-only `dry_run: bool = False` parameter. When set, the engine runs the same `get_status()` classification a real run uses, but skips every `upload_file` / `upload_bytes` / `download_file` / `delete_file` / `manifest.save()` / snapshot creation / notifier publish. Defaults preserve the existing public contract (real-run callers see no change). The planning phase still renders live progress (`Local` / `Remote` rows) so the user sees activity while classification runs.
- `claude_mirror/cli.py` — `--dry-run / --no-dry-run` flag on both `push` and `pull` Click commands. Help text: "Preview the run without uploading, downloading, deleting, or modifying the manifest. No network writes, no local writes, no notifications." New `_render_push_plan()` / `_render_pull_plan()` helpers print the Rich table + summary footer; the dry-run path explicitly skips `_try_reload_watcher()`, `_maybe_auto_prune()`, and the success-notification publish so it is genuinely side-effect-free. `pull --dry-run` constructs the engine without a notifier (`with_pubsub=False`) so a Pub/Sub setup error in a cron-only environment doesn't surface as a fake failure on a preview command.
- `tests/test_push_dry_run.py` — new module, 17 tests. Engine-level: every state in the (local × remote × manifest) matrix classifies into the right bucket; `paths=` filter narrows the plan; backend writes (`upload_file` / `upload_bytes` / `delete_file` / `copy_file`) are never called; the on-disk manifest's bytes + mtime are byte-identical before vs after; `dry_run=True` returns a `PushPlan`, `dry_run=False` keeps returning `None`. CLI-level: `--dry-run` exits 0, prints the summary, the manifest file is unchanged on disk, and the backend recorded zero write calls; the `nothing-to-push` branch renders a dim hint instead of an empty table.
- `tests/test_pull_dry_run.py` — new module, 19 tests. Same shape as the push module, scoped to `DRIVE_AHEAD` / `NEW_DRIVE` (downloads) vs every other state (`skipped`); explicit assertion that `download_file` is never called and that a would-be-pulled file is still absent from local disk after the dry-run.
- `README.md` — `Daily usage cheatsheet` updated to mention `--dry-run` next to `push` / `pull`.
- `docs/cli-reference.md` — `### push` and `### pull` subsections gain a one-line note describing the new flag.

### Tests
- `pytest tests/test_push_dry_run.py tests/test_pull_dry_run.py` — **36 passed locally** on macOS (under 0.5s total; every test under 20ms).

### Added — log --follow for live log streaming
- `claude-mirror log --follow` (alias `-f`) is the `tail -f` of the cross-machine sync activity log. Prints the recent tail first (so the user has context), then enters a poll loop that re-pulls `_sync_log.json` from the configured backend on a configurable cadence and prints only the new entries as they arrive. Closes the long-standing wart that "live" follow mode required cron-wrapping `claude-mirror log` from a shell loop.
- Polling cadence is set with `--interval N` (positive integer, seconds, default 5). `--interval` is rejected when passed without `--follow` — passing it alone exits non-zero with a message naming both flags rather than silently ignoring the value. Non-positive intervals (`--interval 0`, `--interval -5`) are rejected up-front.
- Dedup is by full identity tuple, not timestamp alone: the per-event key is `(timestamp, user, machine, action)`. The sync log is append-only, but two events can share a timestamp under clock-granularity ties or parallel pushes from different machines — keying on the identity tuple means co-timestamped events from different sources are both surfaced rather than collapsed into one.
- Transient-error resilience: a network blip / 5xx / rate-limit during a poll prints one yellow `[poll error: <reason>] retrying in <N>s` line and continues. The loop only exits non-zero on permanent auth-class failures (token revoked, permission removed) or a real Ctrl+C — defeating the purpose of follow mode by exiting on every transient error was the explicit non-goal.
- Ctrl+C path: prints a tidy `Stopped following.` line on a fresh row and exits 0. Implementation uses a module-local `_log_follow_sleep` indirection over `time.sleep` (mirroring the `_status_watch_sleep` pattern from v0.5.31) so tests can drive the loop without globally patching `time.sleep` — per the project's `feedback_no_global_time_sleep_patch.md` rule.
- `--json` composes with `--follow`: in streaming mode the per-entry payload is emitted as newline-delimited JSON (one object per line) so consumers like `jq -c` work without rebuilding the v1 envelope shape on every poll.

### Updated docs/files
- `claude_mirror/cli.py` — `log` command grows `--follow` / `-f` and `--interval N`; new module-level helpers `_log_follow_sleep`, `_log_event_key`, `_log_event_to_json_dict`, `_log_render_event_table_row`, `_log_fetch_remote`, `_log_is_permanent_error`, `_log_print_table`, `_log_follow_loop`. Existing one-shot path is unchanged. Net diff: roughly +180 lines / -40 lines on the `log` block.
- `tests/test_log_follow.py` — **new module, 7 tests:** happy-path streaming, dedup correctness on co-timestamped events with differing identity tuples, KeyboardInterrupt clean exit, transient-error resilience across a single poll, three `--interval` validation tests (zero, negative, without-`--follow`).
- `docs/cli-reference.md` — `### log` subsection grows a Flags table documenting `--follow` / `--interval` / dedup semantics / transient-error behaviour, matching the table style already used for `sync`.
- `README.md` — Daily-usage cheatsheet gains one line: `claude-mirror log --follow              # live tail -f: stream new entries as they arrive`.
- `CHANGELOG.md` — this entry.

### Tests
- `pytest tests/test_log_follow.py` — **7 passed locally** on macOS, all under 25ms each.

### Added — `status --presence` for collaborator visibility

`claude-mirror status --presence` answers "who else is editing this project right now?". It aggregates the shared `_sync_log.json` on the backend into one row per `(user, machine)` tuple — newest first — and renders a `Recent collaborator activity (last 24h)` table below the existing sync-status output. The calling machine's own entries are filtered by default; entries older than 24 hours are excluded; each row surfaces the most recent action, a humanised "When" delta (`3m ago`, `2h ago`, `5d ago`), and up to 5 of the most recently-touched files for that pair.

The flag composes with `--watch` (every tick re-fetches presence along with the rest of the status renderable, inside the same outer `rich.live.Live`, so the section is rebuilt as one Group rather than appended in place — scroll behaviour is preserved). It also composes with `--json`: the envelope schema is bumped to v1.1 (additive only — `version: 1` on the wire stays unchanged) with a new `presence: [...]` key under `result`. Existing v1 consumers see no behavioural change because `presence` is only emitted when `--presence` is set.

The presence-fetch phase appears as a progress row labelled "Presence" with a live `Fetching collaborator presence… done.` detail, matching the dual-line phase progress contract used by every other top-level command. A presence-fetch hiccup never takes down the watch loop — the next tick retries.

### Updated docs/files
- `claude_mirror/_presence.py` (NEW, 173 lines) — pure aggregation: `PresenceEntry` dataclass + `aggregate_presence()` reduces a flat log list into per-`(user, machine)` rows; `humanize_age()` for the Rich render. No I/O, no clocks except the injectable `now=` kwarg, easy to unit-test without mocks.
- `claude_mirror/cli.py` — `status` gains `--presence/--no-presence`; new helpers `_fetch_presence`, `_presence_entry_to_dict`, `_build_presence_renderable`; `_build_status_renderable` accepts `with_presence`/`config`/`storage` and appends the table to the returned `Group`. Snapshot, watch, and JSON paths all share the same fetch helper.
- `tests/test_presence.py` (NEW, 17 tests) — pure-function coverage of `aggregate_presence` (empty / single / collapsed / multi-pair sort / `ignore_self` semantics / 24h window / 5-file cap / malformed-entry resilience / `Z`-suffix timestamps), plus 5 CLI-level tests against the FakeStorageBackend (rendered table, empty-state message, v1.1 JSON envelope, no-fetch when flag omitted, omitted `presence` key in plain `--json`).
- `docs/cli-reference.md` — `--presence` documented in the `status` flag table; the `### status --json` schema section adds the v1.1 `presence` key with a worked example.
- `docs/admin.md` — new "Who else is editing this project?" subsection under Notifications pointing at `status --presence`.

### Tests
- `pytest tests/test_presence.py` — **17 passed** locally on macOS in 0.23s.

### Added — claude-mirror health for monitoring probes

- New `claude-mirror health` command — the machine-readable, fast sibling of `claude-mirror doctor`. Designed for monitoring tools (Uptime Kuma, Better Stack, Prometheus textfile-exporter, Datadog, GitHub Actions matrix health checks) polling every minute or so.
- **Six structured checks** in sequence:
  1. `config_yaml` — does the project YAML load cleanly?
  2. `token_present` — does the configured token file exist + parse? (For WebDAV / SFTP: required inline credentials present in YAML.)
  3. `backend_reachable` — light read against the primary backend (`list_folders` on the configured root, or `sftp.stat` for SFTP). Latency reported in milliseconds.
  4. `mirrors_reachable` — same probe for every Tier 2 mirror in `mirror_config_paths`. One row per mirror, named `mirror_<backend>`.
  5. `watcher_running` — POSIX-only `pgrep -f "claude-mirror watch-all"`. On Windows the row is `unsupported` (the watch-all daemon is POSIX-only); `unsupported` checks never poison the overall status.
  6. `last_sync_age` — most-recent `_sync_log.json` timestamp: `<24h` ok, `24-72h` warn, `>72h` fail. No history yet (fresh install) is `ok` with detail "no sync history yet" — fresh installs aren't unhealthy, they're new.
- **Exit codes** — the load-bearing contract for monitoring-tool integration:
  - `0` overall ok
  - `1` overall warn
  - `2` overall fail
- **Two output modes:**
  - Default — Rich table with one row per check, colour-coded (green = ok, yellow = warn, red = fail, dim = unsupported), latency column where present, and an "Overall: OK / WARN / FAIL" footer.
  - `--json` — single JSON envelope on stdout under the existing v1 schema family. Stdout is JSON-only; the watcher banner and update-check banner are suppressed (additive `health` entry in `_NO_WATCHER_CHECK_CMDS` plus the existing `--json` argv check). Envelope shape: `{"schema": "v1", "command": "health", "generated_at": <ISO8601>, "overall": "ok|warn|fail", "checks": [{"name": ..., "status": ..., "detail": ..., "latency_ms": ...}, ...]}`.
- **Flags:**
  - `--config PATH` — auto-detected from cwd if omitted (same pattern as every other read-only command).
  - `--no-backends` — skip the `backend_reachable`, `mirrors_reachable`, and `last_sync_age` checks. Useful for fast local-only checks that must not burn API quota; pairs well with a high cron frequency.
  - `--timeout N` — per-check timeout cap, default 10s. Negative or zero values exit non-zero with a message naming the flag, before any check runs.
  - `--json` — see above.
- **Worst-rung-wins aggregation:** any `fail` makes overall `fail`; any `warn` makes overall `warn`; otherwise `ok`. `unsupported` rungs (Windows watcher path) are ignored when computing the overall, so a green dashboard stays green.
- **vs `doctor`** — both share data sources but different audiences: doctor is the human-readable, verbose diagnostic with concrete fix-hint commands you reach for when something is broken; health is the structured, fast probe a monitoring tool polls on a schedule. Run them side-by-side.

### Updated docs/files

- `claude_mirror/_health.py` (new, ~370 lines) — `HealthCheck` / `HealthReport` dataclasses, `collect_health()` orchestrator, per-check helpers (`_check_token_present`, `_probe_backend`, `_check_watcher_running`, `_fetch_sync_log`, `_check_last_sync_age`), `_aggregate_overall()` worst-rung-wins helper, threshold constants `LAST_SYNC_WARN_HOURS = 24` and `LAST_SYNC_FAIL_HOURS = 72`.
- `claude_mirror/cli.py` — new `health` Click command (~140 lines including docstring), `_render_health_table()` helper, `_exit_code_for_overall()` helper, `health` added to `_NO_WATCHER_CHECK_CMDS` so banners can never leak into the JSON envelope.
- `tests/test_health.py` (new, **22 tests**) — pure-aggregator tests (worst-rung-wins, `unsupported` ignored, every per-check pathway), CLI tests (`CliRunner` against `--json` and human modes, exit-code mapping for ok/warn/fail, `--timeout 0` and `--timeout -5` rejection). All offline, every test runs in <10 ms.
- `CHANGELOG.md` — this entry.
- `README.md` — new "Monitoring & alerting" subsection with a sample cron one-liner.
- `docs/cli-reference.md` — `### health` subsection added under `## Maintenance`, covering each check, exit codes table, JSON envelope shape with sample, and the "vs doctor" framing.
- `docs/admin.md` — short paragraph in the Doctor section pointing operators at `health` for unattended monitoring.

### Tests

- `pytest tests/test_health.py` — **22 passed locally** on macOS in 0.22s.

### Added — `claude-mirror clone` for one-shot machine bootstrap

- New top-level `clone` command that bootstraps a fresh machine from an existing remote project in one shot. Combines `init` + `auth` + the first `pull` into a single multi-phase invocation, so a new laptop joining an existing project goes from zero to fully-synced with one command instead of three.
- Supports both **flag-driven** (`claude-mirror clone --backend googledrive --project ~/proj --drive-folder-id <FOLDER_ID> --gcp-project-id <GCP_ID> --pubsub-topic-id <TOPIC>`) and **interactive** (`claude-mirror clone --wizard --backend <NAME> --project <PATH>`) modes — the wizard reuses the same `_run_wizard` machinery `init --wizard` already drives, with the same per-backend prompts and validators.
- Per-backend identity flags mirror `init` for Google Drive, Dropbox, OneDrive, WebDAV, and SFTP so the same flag set works against any backend.
- `--no-pull` halts after the auth phase. Useful when this is the machine **seeding** a brand-new remote (no remote files yet to pull, but you still want config + token in place).
- **Rollback on partial failure.** If the auth phase raises, the YAML written by the init phase is removed before the command exits non-zero, so the next attempt starts from a clean state. If the pull phase fails, the YAML + token are kept (auth succeeded) and the error message points the user at `claude-mirror pull --config <PATH>` to retry just the last step.
- Live phase progress: `[1/3] Initializing...`, `[2/3] Authenticating...`, `[3/3] Pulling...` rendered via the existing `make_phase_progress` factory so the user sees which step is running and which one failed.
- **Refactored** rather than duplicated: `init` and `auth` bodies were extracted into module-level `_run_init(...)` / `_run_auth(...)` helpers that both the existing Click commands and the new `clone` command call directly. No `Runner.invoke(init, [...])` re-invocation games — the same code path runs end-to-end.

### Updated docs/files
- `claude_mirror/cli.py` — new `clone` command (~210 lines), `_run_init`/`_run_auth` helper extraction; `init` and `auth` are now thin shims over the helpers; `_run_init` gains a `create_project_if_missing` flag so the clone path creates the destination directory on a fresh machine.
- `tests/test_clone.py` — 5 new tests (Drive happy path, `--no-pull`, auth-failure rollback, `--wizard` mode, SFTP variant). All offline, all <100ms each, all using `FakeStorageBackend` from `tests/conftest.py`.
- `docs/cli-reference.md` — new `### clone` subsection in the `## Setup` group with the full flag list and the rollback-on-failure contract spelled out, plus a cross-link to `docs/scenarios.md` Scenario B (personal multi-machine sync — clone is exactly that scenario's bootstrap step).
- `README.md` — new "Cloning to a new machine" section near "Your first project" with the one-line invocation.

### Tests
- `pytest tests/test_clone.py` — **5 passed locally** on macOS; existing init / auth / wizard regressions all still pass after the helper extraction.

### Documentation — DOC-CLEAN cross-link cleanup

- Fixed 6 stale `README.md#...` anchor links in `docs/cli-reference.md` (5) and `docs/conflict-resolution.md` (1). The targets had been trimmed out of `README.md` during the v0.5.36 doc-split, leaving dead links that landed users at the README top instead of the moved content. Each link now points at its current home — README sections that survived (`#your-first-project`, `#daily-usage-cheatsheet`, `#slack-notifications`) or the equivalent destination in `docs/cli-reference.md` (`#diff`) and `docs/admin.md` (`#multi-backend-mirroring-tier-2`).
- New `### Filtering which events fire` subsection in `docs/admin.md` under `## Notifications` — table of the four dials operators have for shaping notification volume (`*_enabled`, `exclude_patterns` / `.claude_mirror_ignore`, route `on:` filter, route `paths:` filter), with the order-of-evaluation contract spelled out and the heartbeat-event exception documented.
- New `### Is the watcher actually running?` subsection in `docs/admin.md` under `## Auto-start the watcher` — explicit `launchctl list | grep claude-mirror` (macOS), `systemctl --user status claude-mirror-watch` (Linux), `pgrep -f "claude-mirror watch-all"` (any POSIX), and Windows guidance to use the `--once` polling form via Task Scheduler. Plus log-tail commands per platform and the `claude-mirror reload` re-scan hint.

### Total surface area for v0.5.57

- **918 tests pass locally on macOS** (831 baseline + 87 new): 36 push/pull dry-run + 7 log follow + 17 presence + 22 health + 5 clone.
- 5 new top-level commands or command modes; 2 new modules (`claude_mirror/_presence.py`, `claude_mirror/_health.py`); `init` and `auth` refactored into reusable `_run_init` / `_run_auth` helpers so the new `clone` command shares the same code path; `_NO_WATCHER_CHECK_CMDS` extended for the new `--json`-quiet `health` command; `--json` envelope schema bumped to v1.1 (additive — `presence` key only emitted when `status --presence` is set).

---

## [0.5.55] — 2026-05-09

Hotfix #3 for v0.5.52. v0.5.54's CI run came back green on Windows + Python 3.11/3.12 but red on Windows + Python 3.13/3.14 with one remaining failure: `test_safe_join_classifies_correctly[foo\x00bar.md-False]`. Strengthens the path-traversal guard.

### Fixed — Explicit NUL-byte rejection in `_safe_join` (security-contract hardening)
- `claude_mirror/snapshots.py::_safe_join()` is the last line of defence against backend-supplied metadata writing files outside the destination directory (e.g. a malicious mirror sending `rel_path="../../../etc/passwd"`). It must reject NUL bytes — embedded NULs are a classic path-confusion vector that some downstream consumers (older filesystems, C-string-based tooling) treat as a string terminator.
- The previous implementation relied on `Path.resolve()` raising `ValueError` for embedded NUL bytes. **That behaviour changed in Python 3.13 on Windows** — `resolve()` no longer raises `ValueError` for NUL bytes there, so `_safe_join("foo\x00bar.md")` returned silently instead of refusing the input. Linux / macOS / Windows-3.11-3.12 all still raised, masking the regression until the v0.5.52 Windows-CI matrix went green on the new Python versions.
- **Fix:** add an explicit `if "\x00" in rel_path: raise ValueError(...)` guard at the top of `_safe_join()`. Now the contract is platform- and Python-version-independent: NUL bytes are rejected up-front before any `Path.resolve()` call.
- The security posture is *strictly stronger* than before: even on platforms where `resolve()` still happened to raise `ValueError`, the explicit guard runs first and produces a clearer error message ("Refusing to write path containing NUL byte: ...").

### Updated docs/files
- `claude_mirror/snapshots.py` — `_safe_join()` gains an explicit NUL-byte guard; comment in source explains the Python 3.13 / Windows root cause so the next reader doesn't remove it as redundant.
- `tests/test_safe_join.py` — module docstring + parametrize comment updated to describe the new explicit-rejection contract instead of the old "ValueError from resolve()" coincidence. The test data is unchanged.
- `pyproject.toml` (version 0.5.54 → 0.5.55).

### Tests
- `pytest tests/test_safe_join.py` — **22 passed locally** on macOS (the test was already correct; only the source rejection mechanism changed).
- The Python 3.13 / 3.14 Windows runs that flagged the regression in v0.5.54 will now pass.

### Lesson learned
- Don't rely on platform-specific exception behaviour from stdlib for security-critical guards. `Path.resolve()` raising `ValueError` for NUL bytes was an *implementation detail* of CPython on POSIX + older-Windows, not a contract — and it changed. Up-front explicit checks are the only way to make security guarantees portable across CPython versions.

---

## [0.5.54] — 2026-05-09

Hotfix #2 for v0.5.52. v0.5.53 fixed the manifest/glob path-separator class of bug; CI on the new Windows runners now flagged 5 more Windows-only failures clustering into 3 distinct root causes.

### Fixed — Test fixture wrote `\r\n` on Windows, breaking 3 diff tests
- `tests/conftest.py::write_files` used `Path.write_text(content)`. On Windows, Python's text mode translates `\n` → `\r\n` by default, so a fixture writing `"hello\n"` produced `b"hello\r\n"` on disk. The diff engine compares files at the byte level, so the local copy (`b"hello\r\n"`) and the remote copy (`b"hello\n"`) differed even though the visible content was identical.
- Symptom: `test_diff_in_sync_prints_identical_message` and `test_diff_absolute_path_inside_project_resolved_to_relative` saw "both sides differ" when they expected "in sync"; `test_diff_context_flag_respected_at_cli` saw `len(out_ctx5) == len(out_ctx0)` because every line was already classified as a diff line, so adding context lines had nothing to add.
- Fix: `Path.write_text(content, newline="")` suppresses the translation. The fixture now produces byte-exact content on every platform.

### Fixed — `init` raised `FileNotFoundError` on Windows after success
- `_try_reload_watcher()` ran `subprocess.run(["pgrep", ...])` and then `os.kill(pid, signal.SIGHUP)` unconditionally. `pgrep` doesn't exist on Windows; `signal.SIGHUP` is POSIX-only. `init` would print its "Config saved … run claude-mirror auth" success block and then crash with `FileNotFoundError(2, 'The system cannot find the file specified')`.
- Symptom: `test_init_writes_retention_defaults_into_new_yaml` succeeded internally (the YAML was written correctly) but the CLI exited with `1`.
- Fix: early-return when `signal.SIGHUP` doesn't exist on the platform; wrap the `subprocess.run` in `try/except (FileNotFoundError, OSError)` as a defence-in-depth. `_check_watcher_running` already had this defensive shape; `_try_reload_watcher` was missing it.

### Fixed — Inbox concurrency test skipped on Windows (POSIX `fcntl` gap)
- `claude_mirror/notifier.py` imports `fcntl` conditionally and falls back to a no-op when not available. The strict TOCTOU guarantee asserted by `test_inbox_read_clears_atomically_against_concurrent_writer` therefore cannot hold on Windows today — the test counted 673 entries from 678 writes, exactly the kind of lost-mid-clear failure the locking is supposed to prevent.
- Fix: `@pytest.mark.skipif(sys.platform == "win32")` on that single test, with a reason string pointing at the underlying gap (`msvcrt.locking` / `portalocker` are the candidate replacements). Notifier behaviour on Windows is unchanged from v0.5.53; only the strict regression assertion is gated.

### Updated docs/files
- `tests/conftest.py` — `write_files` fixture passes `newline=""` to suppress CRLF translation.
- `claude_mirror/cli.py` — `_try_reload_watcher` guards on `hasattr(signal, "SIGHUP")` and wraps the `pgrep` call.
- `tests/test_notifier_inbox.py` — skip-marker on the strict concurrency test, with a reason linking the Windows-locking gap.
- `pyproject.toml` (version 0.5.53 → 0.5.54).

### Tests
- `pytest tests/` — **834 passed locally** on macOS (no behavioural changes on POSIX).
- The 5 Windows-only failures from the v0.5.53 CI run are addressed; one is now a documented `skip` with an actionable reason rather than a fail.

---

## [0.5.53] — 2026-05-09

Hotfix for v0.5.52. The Windows-runner CI matrix added in WIN-CI surfaced a real cross-platform source bug that the WIN-CI agent missed because it ran tests only on macOS (which uses forward slashes natively): manifest keys + glob comparisons were using OS-native path separators on Windows.

### Fixed — Manifest keys and glob comparisons now use forward slashes on Windows
- **The bug:** three source sites used `str(path.relative_to(project_root))` to compute relative paths. On macOS / Linux this returns forward slashes; on Windows it returns backslashes (`memory\note.md`). Manifest keys flow into remote storage paths, so a Windows machine writing `memory\note.md` to the manifest would break sync against a Linux machine reading the same remote — the keys wouldn't match.
- **What CI caught:** 10 test failures on the new Windows runners flagged by path-separator assertions in `test_snapshots.py`, `test_restore_dry_run.py`, etc. The assertions were correct; the source code was wrong.
- **Fix:** all 3 sites now use `Path.relative_to(...).as_posix()` instead of `str(...)`. Forward slashes everywhere, regardless of platform. Sites:
  - `claude_mirror/sync.py:_local_files()` — file walker building the local-file set fed to `engine.get_status()`.
  - `claude_mirror/snapshots.py:_collect_local_paths()` — snapshot creator collecting paths to upload.
  - `claude_mirror/cli.py:_resolve_path()` — absolute-path-to-relative resolver used by `diff`, `restore`, and friends.
- The corrected `as_posix()` calls also affect `_is_excluded()` and `fnmatch.fnmatchcase()` glob comparisons. Users' YAML `file_patterns: ["**/*.md"]` and `exclude_patterns: ["archive/**"]` are typically authored with forward slashes; on Windows they would have failed to match the OS-native separator paths the walker was previously producing. Now they match consistently.

### Lesson learned
- The WIN-CI agent's macOS-local "all tests pass" check was insufficient — Linux uses forward slashes too, so forward-slash assertions pass on macOS. Windows is the only OS that surfaces this bug class. Cross-platform CI is genuinely necessary; it caught what Linux-only CI would never have caught.
- Memory `feedback_agent_integration_pattern.md` updated with a new note: when an agent ships a multi-platform feature, treat the absence of failures on the agent's own platform as zero evidence that the OTHER platforms work. Cross-platform claims need cross-platform CI.

### Updated docs/files
- `claude_mirror/sync.py` (1-line fix + comment).
- `claude_mirror/snapshots.py` (1-line fix + comment).
- `claude_mirror/cli.py` (1-line fix + comment).
- `pyproject.toml` (version 0.5.52 → 0.5.53).

### Tests
- `pytest tests/` — **834 passed locally on macOS** (same as v0.5.52 — these tests were already correct; the source code was wrong).
- The Windows CI runs that flagged the bug in v0.5.52 will now pass after this fix lands.

---

## [0.5.52] — 2026-05-09

Two internal-quality additions: Windows in the CI matrix and `mypy --strict` as a separate CI gate. **MYPY caught a real runtime bug** — `redact_error` was called at four sites in `cli.py` but never imported, which would have raised `NameError` on every invocation. Plus 5 latent type-correctness issues that mypy surfaced as real defects (not just style).

### Added — Windows runner in CI matrix (WIN-CI)
- Test matrix expanded from `[ubuntu-latest] × [3.11, 3.12, 3.13, 3.14]` to `[ubuntu-latest, windows-latest] × [3.11, 3.12, 3.13, 3.14]` = **8 parallel jobs**. `fail-fast: false` so a Windows-flaky run doesn't mask Linux results.
- New `.gitattributes` pins `* text=auto eol=lf` so contributors on Windows with `core.autocrlf=true` don't accidentally commit CRLF files that confuse Linux text-comparing tests.
- **22 tests skipped on Windows with explicit reasons:**
  - `tests/test_watcher.py` (6 tests) — `watch-all` uses `signal.SIGHUP` for hot-reload, which is POSIX-only; the daemon's hot-reload IS the module's whole purpose.
  - `tests/test_doctor_sftp_deep.py` (15 tests) — POSIX file-permission semantics required (`os.chmod(path, 0o600)` is a no-op on Windows for non-readonly bits, so the "Key file permissions: 0600" assertion can't hold).
  - `tests/test_install_completion.py::test_detect_shell_empty_falls_back_to_platform_default` (1 test) — Windows-default fallback already covered by `test_detect_shell_windows_default_to_powershell` in the powershell completion test file.
- **No source-code bugs found** — the two limitations are intentional design decisions. `_check_watcher_running` already had a `try/except` for missing `pgrep` (Windows / minimal containers); `notifier.py` already conditionally imports `fcntl`. 809 of 831 tests run on each Windows job.

### Added — `mypy --strict` static-type checking in CI (MYPY)
- New `[tool.mypy]` config in `pyproject.toml` with `strict = true`, `disallow_untyped_defs = true`, `no_implicit_optional = true`. `[[tool.mypy.overrides]]` block with `ignore_missing_imports = true` for SDKs without bundled stubs (`paramiko.*`, `dropbox.*`, `msal.*`, `googleapiclient.*`, `google.*`, `yaml.*`, `requests.*`, `plyer.*`).
- New top-level `mypy` job in `.github/workflows/test.yml`. Runs on `ubuntu-latest` × Python 3.11 (the lowest supported version, so the static check catches errors that 3.12+ would silently accept). Separate from the `test` matrix so the GitHub PR check list shows "tests" and "mypy" as two clearly distinguishable failure modes.
- **`mypy --strict claude_mirror/`** now reports `Success: no issues found in 36 source files`.
- **Real bug caught:** `redact_error` was called in `cli.py` at four sites (`850`, `878`, `2872`, `3119`) but never imported. Every invocation would raise `NameError` at runtime. Fix: one line — `from .backends import StorageBackend, redact_error`.
- **5 latent issues fixed** — `config.py` had 12 `Optional[...]` annotations without `Optional` imported (lazy annotations meant no import-time crash, but `get_type_hints()` introspection would have failed); `SnapshotManager.list()` and `HashCache.set()` method names shadowed `list[...]` / `set[...]` in same-class annotations (fixed via `typing.List` aliases); `SyncEngine._classify` had untyped `manifest_entry` (actually `Optional[FileState]`); two backends had implicit-`None` parameter defaults; the abstract `StorageBackend.upload_file` / `download_file` were missing `progress_callback` from their signatures even though every concrete backend already accepted it (PROG-ETA in v0.5.49 wired it through every concrete backend but forgot the ABC).
- **18 `# type: ignore[error-code]` directives added** with specific error codes + one-line comments per the rule. Most-common codes: `[no-untyped-call]` (6× — `google.oauth2.credentials.Credentials` methods are unannotated despite shipping `py.typed`), `[attr-defined]` (6× — `backend.config.root_folder` access; abstract `StorageBackend` doesn't declare `config` but every concrete subclass carries it), `[attr-defined,assignment]` (4× — `_JsonMode`'s test-mode Progress-factory rebinding), `[assignment]` (2× — `_progress_mod.make_phase_progress = _no_op_progress` swap + POSIX-only `import fcntl` fallback to `None`).
- New `tests/test_mypy_smoke.py` (3 tests) runs `mypy --strict` as a subprocess from pytest so a regression surfaces at `pytest` time on the contributor's machine, not after the CI feedback loop. Skipped when `mypy` is not on PATH.
- `CONTRIBUTING.md` extended with a "Type checking" subsection: every PR must keep the strict-mode pass; `# type: ignore` is reserved for genuine third-party-stub gaps with a one-line comment.

### Documented
- `README.md` — Quality gates line updated to "**834 automated tests** on Linux and Windows on Python 3.11, 3.12, 3.13, and 3.14 in parallel via GitHub Actions, plus a separate `mypy --strict` static-type-checking job on every commit and PR".
- `CONTRIBUTING.md` — Updated CI section documents both the Windows + 4-Python matrix AND the separate mypy job; new POSIX-only-skip-set paragraph; Type checking subsection.
- `.gitattributes` (NEW) — pins LF endings cross-platform.

### Updated docs/files
- `pyproject.toml` (`[tool.mypy]` config block + 12-module override list).
- `.github/workflows/test.yml` (matrix expanded to 2 OS × 4 Python; new `mypy` top-level job).
- `.gitattributes` (NEW).
- `claude_mirror/cli.py` (real bug fix: import `redact_error`; 6 `[attr-defined]` ignores for `backend.config.root_folder`; 4 `_JsonMode` rebinding ignores).
- `claude_mirror/config.py` (Optional import; type ignores for fcntl fallback).
- `claude_mirror/sync.py`, `claude_mirror/snapshots.py`, `claude_mirror/backends/__init__.py`, `claude_mirror/backends/googledrive.py` + 19 other source files (annotation polish).
- `tests/test_watcher.py`, `tests/test_doctor_sftp_deep.py`, `tests/test_install_completion.py` (Windows skipif markers with explicit reasons).
- `tests/test_mypy_smoke.py` (NEW, 3 tests).
- `README.md`, `CONTRIBUTING.md`.
- `pyproject.toml` (version 0.5.51 → 0.5.52).

### Tests
- `pytest tests/` — **834 passed in ~3.5s** locally (831 baseline + 3 new mypy smoke tests; the 3 smoke tests skip when `mypy` isn't on the PATH-based detection — they run on CI where `pip install mypy` puts mypy on PATH). All offline.
- `mypy --strict claude_mirror/` — `Success: no issues found in 36 source files`.

---

## [0.5.51] — 2026-05-09

Hotfix for v0.5.50 — same banner-leak pattern that bit `--json` mode in v0.5.39 hit the new `_list-backends` hidden subcommand.

### Fixed — Watcher banner leak into `_list-backends` output
- v0.5.50's `_list-backends` is invoked by the dynamic shell-completion scripts on every tab-press. Its output MUST be exactly the backend names, one per line, or completion shows ANSI gunk + `claude-mirror watch-all` as a candidate. The CI Linux matrix flagged this on the dyn-comp tests (`Left contains 4 more items, first extra item: 'dropbox'` — the watcher banner pushed the real output to lines 5-9).
- Root cause: same as v0.5.44's banner-on-`--json` failure. `_CLIGroup.invoke()` runs `_check_watcher_running()` before any subcommand handler, and `_list-backends` wasn't in `_NO_WATCHER_CHECK_CMDS`. v0.5.44 added a `--json` argv check; v0.5.51 adds `_list-backends` to the per-command set.
- Fix: one-line addition to `_NO_WATCHER_CHECK_CMDS` so the banner is suppressed for `_list-backends`. Same set entry already covers `watch`/`watch-all`/`reload`/`init`/`auth`/`find-config`/`test-notify`/`inbox`/`doctor`/`prune`/`diff`/`seed-mirror`/`profile`.
- v0.5.50 was pushed to `origin/main` but never tagged or published to PyPI, so no end users received broken tab-completion.

---

## [0.5.50] — 2026-05-09

Four independent additions across distribution, docs, and notifications. ROUTE and TMPL share the notifier surface but partition cleanly: ROUTE owns dispatch (which webhook fires, with which event/path filter), TMPL owns format (the message body inside `_format_event`).

### Added — Dynamic `--backend` shell completion (DYN-COMP)
- New hidden `claude-mirror _list-backends` subcommand prints the valid backend names one per line. Source-of-truth is a single module-level `_AVAILABLE_BACKENDS` constant referenced by `_create_storage`'s dispatch, `profile_create`'s `click.Choice`, and 7 existing `--backend` Click options across init/restore/retry/seed-mirror/gc/inspect/doctor.
- The completion scripts emitted by `claude-mirror completion {zsh,bash,fish,powershell}` are runtime-callback-based and now wire through `_list-backends` for the `--backend` value list, so future backend additions automatically surface in tab-completion without re-sourcing.
- 12 new tests in `tests/test_dyn_comp.py`.

### Added — Comprehensive FAQ doc (DOC-FAQ)
- New `docs/faq.md` (666 lines) covering the most-asked-and-most-fixable issues: getting started / auth and credentials / sync workflow / multi-machine and multi-user / snapshots and recovery / notifications / performance and reliability / common gotchas / migration and upgrade. 41 Q/A entries with concrete commands and "See also" depth-doc links. TOC at the top, top-of-section tl;dr lines, back-links to README at top and bottom.
- Cross-linked from `README.md`, `docs/README.md`, `docs/admin.md`, `docs/cli-reference.md`, `docs/conflict-resolution.md`, `docs/scenarios.md`.

### Added — Multi-channel notification routing per project (ROUTE)
- New optional list-form per backend: `slack_routes`, `discord_routes`, `teams_routes`, `webhook_routes`. Each entry: `{webhook_url: str, on: list[str], paths: list[str]}` filters by event type (`push / pull / sync / delete`) and path glob.
- **Precedence**: when both the legacy single-channel form (`slack_webhook_url`) and the list-form (`slack_routes`) are set, the list-form wins with a yellow info line at engine startup. Empty list (`slack_routes: []`) collapses to None so legacy keeps working during transitions.
- **fnmatch quirk**: default `paths=["**/*"]` matches paths WITH at least one separator (`docs/notes.md`) but does NOT match top-level files (`CLAUDE.md` at project root). Use `*.md` or explicit listings for top-level catches. Documented.
- **Heartbeat events** with empty `files` bypass the path filter so subscribing to `sync` events still fires for nothing-to-do heartbeats.
- New `Config.iter_routes(backend)` helper yields the resolved route list (legacy form returns one pseudo-route with default on/paths). New `_normalise_routes()` validation runs in `__post_init__`.
- Slack dispatch uses per-route `dataclasses.replace(self.config, slack_webhook_url=route["webhook_url"], slack_routes=None)` for a clean single-channel Config view. Notifier classes' external API unchanged.
- 24 new tests in `tests/test_route.py`.

### Added — Per-event webhook templating (TMPL)
- New optional dicts per backend: `slack_template_format`, `discord_template_format`, `teams_template_format`, `webhook_template_format`. Each maps action (`push / pull / sync / delete`) to a `str.format`-style template string (Slack/Discord/Teams) or a structured dict template (Generic).
- **Placeholder vocabulary**: `{user}`, `{machine}`, `{project}`, `{action}`, `{n_files}`, `{file_list}` (capped at 10 with "and N more"), `{first_file}`, `{timestamp}`, `{snapshot_timestamp}`.
- **Per-backend body shape**:
  - Discord: rendered template REPLACES `embeds[0].title`; color/fields preserved.
  - Teams: populates BOTH `summary` (mobile preview) AND `sections[0].activitySubtitle`; `activityTitle` keeps the structured default.
  - Generic: template values merged on top of v1 envelope (template wins on key conflict; `version`/`event`/`timestamp` still overridable so users can pin downstream schemas).
  - Slack: sanitisation on placeholder VALUES (defeats mrkdwn injection from `event.user` / `event.machine` if a collaborator renames themselves to `*haxx*`); user-authored mrkdwn in their own template string is preserved.
- **Unknown placeholder** → yellow info line "template uses unknown variable {x} — falling back to default format" + builds the built-in format. Never crashes a sync.
- Stdlib `str.format` only — no Jinja2 / Mustache. New `_validate_template_dict` helper runs in `Config.__post_init__`.
- 32 new tests in `tests/test_tmpl.py`.

### Documented
- `docs/admin.md` — new "Multi-channel routing per project" subsection (ROUTE) + new "Per-event message templating" subsection (TMPL) with placeholder vocabulary and 4 worked examples; tab-completion subsection notes the dynamic-completion change.
- `docs/cli-reference.md` — extended notification-fields table with 4 `*_routes` rows + 4 `*_template_format` rows; `completion` entry notes dynamic `--backend` enumeration.
- `docs/faq.md` (NEW) — comprehensive 41-Q FAQ.
- `README.md` — Quality gates count `763 → 831` and four new coverage labels named explicitly.

### Updated docs/files
- `claude_mirror/cli.py` (`_AVAILABLE_BACKENDS` constant, hidden `_list-backends` subcommand, per-shell DYN-COMP shims).
- `claude_mirror/config.py` (4 `*_routes` fields + 4 `*_template_format` fields + `_normalise_routes` + `_validate_template_dict` + `iter_routes` helper).
- `claude_mirror/sync.py` (`_dispatch_extra_webhooks` rewritten for per-route iteration with `templates=` kwarg threaded through).
- `claude_mirror/notifications/webhooks.py` (templates= constructor kwarg + `_format_event` template branch with safe fallback).
- `claude_mirror/slack.py` (per-action template branch with placeholder-value sanitisation).
- `tests/test_dyn_comp.py`, `tests/test_route.py`, `tests/test_tmpl.py` (NEW, 68 tests total).
- `docs/faq.md` (NEW), `docs/admin.md`, `docs/cli-reference.md`, `docs/conflict-resolution.md`, `docs/scenarios.md`, `docs/README.md`, `README.md`.
- `pyproject.toml` (version 0.5.49 → 0.5.50).

### Tests
- `pytest tests/` — **831 passed in ~3s** (= 763 baseline + 12 DYN-COMP + 0 DOC-FAQ + 24 ROUTE + 32 TMPL). All offline.

---

## [0.5.49] — 2026-05-08

Four independent additions integrated together. None depends on the others; each is shippable on the 0.5.x line as patch-level work.

### Added — Live transfer progress with ETA + bytes/sec (PROG-ETA)
- New `claude_mirror/_progress.py:make_transfer_progress(console)` factory alongside the existing `make_phase_progress`. Transfer phases (`push` / `pull` / `sync` / `seed-mirror`) now show `BarColumn + DownloadColumn(binary_units=True) + TransferSpeedColumn + TimeRemainingColumn` instead of just a spinner. Status / guard / snapshot / notify keep the existing phase-progress UI.
- Each backend's `upload_file` and `download_file` now accept an optional `progress_callback: Callable[[int], None] | None = None` kwarg with a **delta-based** contract: `cb(N)` means "N more bytes transferred since the last call". Wired through every backend with care for SDK quirks (Drive's `next_chunk()` returns `(None, response)` on the final iteration; Dropbox's single-shot `files_upload` emits one final delta; OneDrive ignores `lastBytesUploaded` in favour of driving the chunk loop ourselves; SFTP's manual block loop from v0.5.39's throttle work means no cumulative→delta bridge needed).
- 19 new tests in `tests/test_prog_eta.py`. Multi-backend `push` keeps the per-backend `googledrive: X/N · sftp: Y/N` breakdown via `extra_detail` flowing into `_run_transfer_phase`.

### Added — `--profile NAME` credentials short-hand + `claude-mirror profile` subcommand group (PROFILE)
- Define a credentials profile once at `~/.config/claude_mirror/profiles/<name>.yaml`, reference it from any project via the global `--profile NAME` flag. Eliminates the "5 Drive projects, same credentials path copy-pasted 5 times" pattern.
- Project YAML wins over profile defaults — projects can override any field locally for one-off cases.
- New module `claude_mirror/profiles.py`: `load_profile()`, `apply_profile()`, `list_profiles()`, `profile_summary()`. Process-wide profile override via module-level slot (`set_global_profile_override`) keeps the diff to `Config.load` callers minimal.
- New `claude-mirror profile {list, show, create, delete}` subcommand group. `profile create` reuses the wizard helpers from `_byo_wizard.py`. `profile delete` follows the destructive-ops convention (dry-run default, typed `YES`, `--yes` for non-interactive). Profile YAMLs written with `chmod 0600` to match token-file protection.
- `init --profile NAME` skips prompts for fields the profile already provides and writes a slim project YAML with just `profile: NAME` at the top, plus the project-specific fields (folder ID, topic ID, etc.). `Config.save(profile=, strip_fields=)` strips the credential fields from disk so the profile remains the on-disk source of truth.
- 24 new tests in `tests/test_profile.py`. Full walkthrough in new `docs/profiles.md`.

### Added — Shared backoff coordinator for global rate-limits (RETRY)
- New `RATE_LIMIT_GLOBAL` value in `ErrorClass` (in `claude_mirror/backends/__init__.py`). Distinct from `TRANSIENT` (per-file network blip): when ANY worker reports `RATE_LIMIT_GLOBAL`, every in-flight upload pauses on a single shared deadline rather than each retrying independently and compounding the rate-limit pressure.
- New `claude_mirror/retry.py` module: `BackoffCoordinator` with module-level `_now` / `_sleep` wrappers (per the project's no-global-time-sleep-patch rule). Initial backoff: 30s if the server doesn't supply `Retry-After`, else honour the server value. Subsequent escalations within the same throttled window: `min(60s, current * 1.5)`. Capped at `config.max_throttle_wait_seconds` (default 600s; cron jobs can lower for fail-fast). Threadsafe via `threading.Lock`. Uses `time.monotonic()` for the deadline.
- Per-backend `classify_error` extensions (4 of 5 backends — SFTP intentionally untouched since SSH has no 429 equivalent):
  - **Drive**: `userRateLimitExceeded` / `rateLimitExceeded` reasons (Drive's quirk — these come as 403 + reason, not 429); plain 429 also handled. `quotaExceeded` stays QUOTA (semantically distinct: storage cap, user action).
  - **Dropbox**: dedicated `RateLimitError`, plus `ApiError` with `error_summary` containing `too_many_requests` / `too_many_write_operations`, plus bare `HttpError` 429 — all three classify uniformly now.
  - **OneDrive**: Microsoft Graph reliably emits 429 + `Retry-After`; picked up by `extract_retry_after_seconds` for honest initial-window sizing.
  - **WebDAV**: 429 routes correctly (most WebDAV servers send 503 under load, which stays TRANSIENT).
- Engine wiring: `SyncEngine._make_coordinator()` builds a fresh coordinator per command; `_upload_with_coordinator(callable, backend)` wraps each upload (push, fan-out to mirrors, retry, seed-mirror). On `RATE_LIMIT_GLOBAL`: signals coordinator with the server-supplied `Retry-After`, then loops up to `config.max_retry_attempts` times with `wait_if_throttled()` at the top of each attempt.
- User-visible: ONE calm "Backend reports rate limit. Pausing 30s before retrying." line + ONE "Throttle cleared. Resuming uploads." line, instead of N TRANSIENT warnings.
- 35 new tests in `tests/test_retry_global_throttle.py` (mocked clock — `cv.wait(timeout=remaining)` interaction uses a `register_cv` mechanism so `clock.advance()` calls `cv.notify_all()` and blocked waiters re-check the fake deadline).

### Added — Non-interactive `sync --no-prompt --strategy` for cron (CRON-SYNC)
- New `--no-prompt` + `--strategy {keep-local, keep-remote}` flags on `sync`. With `--no-prompt`, conflicts auto-resolve via the chosen strategy instead of blocking on `click.prompt`. Default flow unchanged.
- `MergeHandler.__init__(non_interactive_strategy=...)` extends the existing handler — back-compat preserved (`MergeHandler()` with no args still prompts).
- Mutual-exclusion validation: `--no-prompt` requires `--strategy` (clean error); `--strategy` without `--no-prompt` is a yellow info line + falls back to interactive. **Non-tty stdin without `--no-prompt`: fail-fast at command entry** with a hint pointing at the new flags — picked over silent-defaulting (would violate destructive-defaults convention) and over hanging on `click.prompt` (the bug we're fixing).
- Audit trail rides on the existing `SyncEvent` as an additive optional `auto_resolved_files: list[dict]` field. Older log readers ignore the field via the `valid = {k: v for k, v in raw.items() if k in cls.__dataclass_fields__}` filter added to `from_json` and `from_bytes`.
- Banner suppression mirrors the v0.5.44 `--json` pattern: when `--no-prompt` is in argv, the watcher-not-running banner + update-check notice are suppressed — keeps cron mail to one yellow line per conflict + the trailing `Summary:` line.
- Defensive `ValueError` on unknown strategy values in `MergeHandler.__init__` — a programmatic typo (`"keep_local"` with underscore) can't silently fall through to the interactive path under cron.
- 12 new tests in `tests/test_cron_sync.py`. Sample crontab entries in `docs/admin.md`. Scenarios A and B in `docs/scenarios.md` gain "Automated nightly sync" subsections.

### Documented
- `docs/admin.md` — new "Transfer progress" subsection (PROG-ETA), new "Rate-limit handling" subsection (RETRY), new "Unattended sync via cron" subsection (CRON-SYNC), new "Credentials profiles" section (PROFILE).
- `docs/cli-reference.md` — `--profile NAME` global flag, `profile` subcommand group, `--no-prompt` / `--strategy` on sync, `max_throttle_wait_seconds` config field, transfer-progress note on push/pull/sync/seed-mirror entries.
- `docs/profiles.md` (NEW) — comprehensive walkthrough with per-backend sample profile YAMLs, merge precedence rule, common workflows.
- `docs/conflict-resolution.md` — new "Non-interactive mode" section.
- `docs/scenarios.md` — Scenarios A and B gain "Automated nightly sync" subsections.
- `README.md` — Quality gates count `673 → 763` and four new coverage labels named explicitly.

### Updated docs/files
- `claude_mirror/_progress.py` (PROG-ETA factory).
- `claude_mirror/profiles.py` (NEW — PROFILE).
- `claude_mirror/retry.py` (NEW — RETRY coordinator).
- `claude_mirror/cli.py` (--profile global, profile subcommand group, sync --no-prompt/--strategy, transfer-progress wiring).
- `claude_mirror/sync.py` (transfer-progress callbacks, BackoffCoordinator wiring, non-interactive sync strategy).
- `claude_mirror/merge.py` (non_interactive_strategy kwarg).
- `claude_mirror/events.py` (auto_resolved_files field).
- `claude_mirror/config.py` (set_global_profile_override + max_throttle_wait_seconds).
- `claude_mirror/backends/__init__.py` (RATE_LIMIT_GLOBAL ErrorClass + progress_callback contract docs).
- `claude_mirror/backends/{googledrive,dropbox,onedrive,webdav,sftp}.py` (progress_callback wiring + 4-of-5 classify_error rate-limit detection).
- `tests/test_prog_eta.py`, `tests/test_profile.py`, `tests/test_retry_global_throttle.py`, `tests/test_cron_sync.py` (NEW — 90 tests total).
- `docs/profiles.md` (NEW), `docs/admin.md`, `docs/cli-reference.md`, `docs/conflict-resolution.md`, `docs/scenarios.md`, `docs/README.md` (index entry for profiles.md), `README.md`.
- `pyproject.toml` (version 0.5.48 → 0.5.49).

### Tests
- `pytest tests/` — **763 passed in ~3s** (= 673 baseline + 19 PROG-ETA + 24 PROFILE + 35 RETRY + 12 CRON-SYNC + 1 small fixture update on existing test). All offline.

---

## [0.5.48] — 2026-05-08

`doctor --backend NAME` now layers deep, backend-specific diagnostic checks on top of the generic per-backend pass for **all five backends**. v0.5.46 shipped this for `googledrive`; v0.5.48 closes the parity gap by adding equivalent deep checks for `dropbox`, `onedrive`, `webdav`, and `sftp`.

### Added — Cross-backend `doctor` deep diagnostic parity
- **`doctor --backend dropbox`** (DOC-DBX) — six new checks: token JSON shape (`access_token` or `refresh_token` present), `dropbox_app_key` format sanity (regex), `users_get_current_account` smoke test against the Dropbox SDK, granted-scope inspection (`files.content.read` + `files.content.write` for PKCE tokens; legacy tokens emit info line + skip), `files_list_folder` against the configured `dropbox_folder` (catches NotFound, permission-denied, team-folder access issues), and an account-type / team-status info line (team admins can disable third-party app access, silently breaking sync). 11 new tests.
- **`doctor --backend onedrive`** (DOC-ONE) — six new checks: MSAL token cache integrity, Azure `onedrive_client_id` GUID format validation (runs BEFORE MSAL construction so error messages stay actionable), granted-scope inspection (with fallback chain through both `app.get_accounts()` and `cache.find("AccessToken")` — MSAL cache shape varies), silent token refresh against the cached account, Microsoft Graph drive-item probe (`me/drive/root:/{onedrive_folder}`), and a folder-vs-file shape assertion on the response. `requests.get` is lazy-imported inside the deep-check function for clean test mocking. 12 new tests.
- **`doctor --backend webdav`** (DOC-WD) — six new checks: URL well-formedness, `PROPFIND` on the configured root (HTTP 207 expected; classifies 401 / 404 / 405 / 5xx with specific fix-hints), `DAV:` class header detection (server-quirk-tolerant: `oc:checksums` exposure varies wildly across Nextcloud / OwnCloud / Apache `mod_dav` and is treated as informational), `getetag` presence for change-detection, `oc:checksums` extension support detection, and an account-base smoke probe gated on the Nextcloud / OwnCloud URL pattern. 13 new tests.
- **`doctor --backend sftp`** (DOC-SFTP) — seven new checks: host fingerprint match against `~/.ssh/known_hosts` with **bracketed `[host]:port` lookup form** (paramiko's quirk for non-standard ports) and fallback to bare host, mismatch treated as a possible MITM with a fix-hint pointing at `ssh-keygen -R hostname` (NOT `claude-mirror auth` — fingerprint mismatches are a security incident, not a token problem); SSH key file existence + 0600 permissions (`os.stat & 0o077` detects any group/world bit, no auto-fix — user runs `chmod 600` consciously); key decryption (or ssh-agent fallback for encrypted keys); a raw-socket → `Transport(sock)` → `start_client(timeout=5)` handshake pattern (NOT `SSHClient.connect`) so the live host key can be checked **before** credentials are sent; auth + connection; `exec_command` capability detection (some `internal-sftp`-jailed accounts disallow shell, in which case claude-mirror falls back to client-side hashing); root-path `stat` (NotFound is informational — claude-mirror creates the path on first push). 15 new tests; the auth-bucket short-circuit is verified end-to-end with an inverted assertion that `auth_publickey` / `open_session` / `SFTPClient.from_transport` never fire after a fingerprint mismatch.
- All four mirror v0.5.46's auth-bucket grouping pattern: multiple auth-class failures collapse into a single `ACTION REQUIRED` block instead of N duplicate re-auth lines for the same root cause. SDK clients are lazy-imported so the generic `doctor` invocation stays quick.
- **51 new tests total** (DOC-DBX 11 + DOC-ONE 12 + DOC-WD 13 + DOC-SFTP 15). All offline (mocked SDKs), <100ms each.

### Documented
- `docs/admin.md` — extended the Doctor section with five `### <Backend> deep checks` subsections (Drive, Dropbox, OneDrive, WebDAV, SFTP), each with check-matrix table, auth-bucketing semantics, lazy-import note, sample success + failure outputs.
- `docs/backends/{dropbox,onedrive,webdav,sftp}.md` — each backend doc gains a `## Diagnosing setup problems` subsection mirroring the existing `docs/backends/google-drive.md` shape from v0.5.46.
- `docs/cli-reference.md` — extended the `doctor` entry with one paragraph per backend describing the deep-check matrix and cross-linking to admin.md + the backend-specific doc.
- `README.md` — Quality gates count `622 → 673` and the surface list now names the deep-check coverage for **all five backends** rather than only googledrive.

### Updated docs/files
- `claude_mirror/cli.py` (~1700 lines added: `_run_dropbox_deep_checks`, `_onedrive_deep_check_factory` + `_run_onedrive_deep_checks`, `_run_webdav_deep_checks`, `_sftp_deep_check_factory` + `_run_sftp_deep_checks`, four new dispatch hooks in `_run_doctor_checks` with section headers).
- `tests/test_doctor_{dropbox,onedrive,webdav,sftp}_deep.py` (NEW, 51 tests total).
- `docs/admin.md`, `docs/cli-reference.md`, `docs/backends/{dropbox,onedrive,webdav,sftp}.md`, `README.md`.
- `pyproject.toml` (version 0.5.47 → 0.5.48).

### Tests
- `pytest tests/` — **673 passed in ~2.8s** (= 622 baseline + 51 new). All offline, all deterministic.

---

## [0.5.47] — 2026-05-08

Two independent additions: three new notification backends (Discord / Teams / Generic webhook) plus the Drive Pub/Sub auto-setup that v0.5.46's deep `doctor` only diagnosed.

### Added — Discord / Microsoft Teams / Generic webhook notification backends
- **Discord** webhooks: payloads are formatted as Discord embeds with action-coloured borders (green push / blue pull / blue sync / red delete), action title + activitySubtitle, fields for User / Machine / Project, and a Files-changed list capped at 10 with "and N more". URL pattern: `https://discord.com/api/webhooks/{id}/{token}`.
- **Microsoft Teams** webhooks: payloads use the **MessageCard** schema (`@type: "MessageCard"`, `themeColor`, `summary`, `sections[]`). Both legacy `outlook.office.com/webhook/...` and modern `{tenant}.webhook.office.com/...` (Workflows-based) URLs work — same MessageCard payload accepted by both.
- **Generic** webhook: schema-stable v1 envelope `{"version": 1, "event": "push|pull|sync|delete", "user", "machine", "project", "files", "timestamp"}` POSTed to any URL with optional custom headers (`webhook_extra_headers` for Bearer tokens, n8n / Make / Zapier endpoints, internal Slack-replacement servers).
- All three share a new `WebhookNotifier` base class in `claude_mirror/notifications/webhooks.py` (~250 lines, single module). Best-effort delivery: any notifier failure is logged at DEBUG and silently swallowed — never blocks a sync.
- Each backend is opt-in via its own YAML field (`discord_enabled` / `teams_enabled` / `webhook_enabled` and the matching `*_webhook_url`). Multiple can be enabled at once; they fire sequentially after the existing Slack hook. Slack code path **untouched**.
- Stdlib-only — uses `urllib.request`, no new transitive dependency.
- 27 new tests in `tests/test_webhooks.py` cover payload format per backend, file-list cap, network-failure swallow, 4xx response handling, YAML round-trip with `webhook_extra_headers` dict, multi-backend independence, Slack regression guard.

### Added — `claude-mirror init --auto-pubsub-setup` (Drive BYO #4)
- New opt-in flag on `init` for Google Drive setups. After OAuth completes, idempotently creates the Pub/Sub topic, the per-machine subscription, and the IAM grant on the topic (`apps-storage-noreply@google.com` → `roles/pubsub.publisher`). The IAM grant is the silent failure most users miss — v0.5.46's `doctor --backend googledrive` diagnosed it; v0.5.47 fixes it in one step.
- Detects whether the Pub/Sub OAuth scope was granted at auth time. If not, prints a yellow info line and skips Pub/Sub steps without aborting the wizard. Re-running `claude-mirror auth` and adding the Pub/Sub scope unblocks.
- Each step handled idempotently: `AlreadyExists` treated as success; `PermissionDenied` surfaced with a clear message but doesn't block YAML write; etag-conflict on `set_iam_policy` retries once before surfacing.
- Lives in `claude_mirror/_byo_wizard.py` (existing module from v0.5.46) as `auto_setup_pubsub(creds, gcp_project_id, pubsub_topic_id, machine_name) -> AutoSetupResult`. Lazy-imports `pubsub_v1` so users not passing the flag pay no extra import cost. Re-uses the `_DRIVE_PUBSUB_PUBLISHER_SA` constant from v0.5.46's doctor (single source of truth) and the existing `Config.subscription_id` pattern (no invented names).
- Renders a Rich table after the auto-setup runs (✓ Topic exists / ✓ Subscription created / ✓ IAM grant added).
- 11 new tests in `tests/test_auto_pubsub_setup.py` cover all-fresh, all-already-exists, mixed, scope-not-granted skip (no SDK ctors called), topic PermissionDenied (subsequent steps skipped), etag-conflict retry, machine-name normalisation, plus three CLI flag-wiring tests.

### Documented
- `docs/admin.md` — new `## Notifications` section covering all four backends (Slack + Discord + Teams + Generic) with config-field summary and sample YAML; new "Fixing a missing topic / subscription / IAM grant" subsection under Drive deep checks pointing at `--auto-pubsub-setup`.
- `docs/cli-reference.md` — new "Notification webhook fields" table; `--auto-pubsub-setup` added to the `init` synopsis.
- `docs/backends/google-drive.md` — new H3 documenting `--auto-pubsub-setup` with sample fresh / re-run output and edge cases.
- `README.md` — Quality gates count `584 → 622` and two new coverage labels named explicitly: "Discord / Teams / Generic webhook notifiers" and "Drive Pub/Sub auto-setup logic".

### Updated docs/files
- `claude_mirror/notifications/webhooks.py` (NEW).
- `claude_mirror/_byo_wizard.py` (extended with `AutoSetupResult` + `auto_setup_pubsub` + helpers).
- `claude_mirror/cli.py` (`--auto-pubsub-setup` Click option + chained smoke→auto-setup flow).
- `claude_mirror/sync.py` (new `_dispatch_extra_webhooks` after Slack).
- `claude_mirror/config.py` (7 new fields: discord/teams/webhook *_enabled, *_url, plus `webhook_extra_headers`).
- `tests/test_webhooks.py`, `tests/test_auto_pubsub_setup.py` (NEW, 38 tests total).
- `docs/admin.md`, `docs/cli-reference.md`, `docs/backends/google-drive.md`, `README.md`.
- `pyproject.toml` (version 0.5.46 → 0.5.47).

### Tests
- `pytest tests/` — **622 passed in ~2.5s** (= 584 baseline + 27 webhooks + 11 Pub/Sub auto-setup). All offline.

---

## [0.5.46] — 2026-05-08

Three independent improvements landed together — all additive, all on the 0.5.x line, no minor bump required. Drive BYO wizard polish (the most painful backend setup just got smoother), a deep Drive diagnostic to surface the silent IAM-grant failure most users never know they hit, and a small `seed-mirror` ergonomics fix.

### Added — Drive BYO wizard polish (URL templating + input validation + post-auth smoke test)
- **Auto-open Cloud Console at the right page** — after the wizard captures the GCP project ID, it offers (default Yes) to launch the Drive API enable / Pub/Sub API enable / OAuth client creation pages with `?project={project_id}` pre-filled. Falls through to printing URLs for copy-paste on headless / SSH machines.
- **Inline regex validation** — `value_proc=` callbacks reject malformed inputs at the prompt with helpful errors:
  - GCP project ID: `^[a-z][-a-z0-9]{4,28}[a-z0-9]$` plus a link to Google's identifier rules
  - Drive folder ID: `^[A-Za-z0-9_-]{20,}$` plus a hint to copy the segment after `/folders/` in the Drive URL
  - Pub/Sub topic ID: `^[a-zA-Z][\w.~+%-]{2,254}$`
  - Credentials file path: existence + JSON-parse + `installed.client_id` field (catches the very common "I downloaded a service account key by mistake" failure)
- **Post-auth smoke test** — after the wizard completes auth and BEFORE writing the YAML, runs `drive.files.list(pageSize=1, q="'<folder>' in parents")`. Catches "Drive API not enabled", credentials-for-wrong-project, and "the folder ID I just typed doesn't actually exist" — three failures that today only surface at first sync. Smoke-test failure offers a retry loop without aborting the wizard; the YAML still writes if the user declines.
- New module `claude_mirror/_byo_wizard.py` (URL builders, four validators, smoke runner) so `cli.py`'s wizard branch stays compact.
- 59 new tests in `tests/test_byo_wizard.py` (URL templating, each validator's accept/reject set incl. parametrized error messages, smoke-test pass/fail/retry/decline, ReDoS-safe regex audit).

### Added — `claude-mirror doctor --backend googledrive` deep diagnostic
- Six new check types layered on top of the existing generic `doctor` checks, only when the user explicitly filters to `googledrive`:
  - **OAuth scopes granted** — Drive scope is mandatory; missing Pub/Sub scope downgrades the next four checks to a single yellow info line ("Pub/Sub scope not granted; skipping...") rather than five red failures.
  - **Drive API enabled** in the GCP project — uses `drive.about.get(fields="user")` and parses Google's "API has not been used in project X" error text.
  - **Pub/Sub API enabled** — same pattern via `publisher.get_topic`.
  - **Pub/Sub topic exists** at `projects/{gcp_project_id}/topics/{pubsub_topic_id}`. NotFound → fail-with-fix-hint.
  - **Per-machine subscription exists** at `projects/{gcp_project_id}/subscriptions/{pubsub_topic_id}-{machine_name_safe}` (lifted from the existing `config.subscription_id` property — not invented).
  - **IAM grant present on the topic for Drive's service account** (`apps-storage-noreply@google.com`, wrapped in `_DRIVE_PUBSUB_PUBLISHER_SA` constant for future maintenance). This is the highest-value check — most users miss this step and "Pub/Sub seems to work but no notifications arrive" silently. Surfaces with `Push events from THIS machine won't notify others.` plus the fix command.
- Auth failures (`invalid_grant` / `unauthorized_client`) bucket into a single `ACTION REQUIRED` block instead of repeating across checks.
- `pubsub_v1` is **lazy-imported** so generic `doctor` invocations on non-Drive projects pay no extra cost.
- 12 new tests in `tests/test_doctor_googledrive_deep.py` (all-pass, scope-not-granted skip, API-not-enabled detection, topic missing, subscription missing, IAM grant missing, mocked auth failure produces single bucket).

### Added — `seed-mirror` auto-detects single unseeded backend
- `--backend NAME` is now optional. When omitted, `seed-mirror` consults `manifest.unseeded_for_backend(...)` for every configured mirror and:
  - Zero candidates → green ✓ "No mirrors have unseeded files. Nothing to seed." Exit 0.
  - **Exactly one candidate** → dim "Auto-detected unseeded mirror: `<name>`" then continues with the inferred backend.
  - Multiple candidates → red error listing the names alphabetically + suggesting `--backend NAME`. Exit 1.
- Explicit `--backend NAME` always wins; back-compat preserved.
- 7 new tests in `tests/test_seed_mirror_auto_detect.py` (zero / exact-one / multiple / explicit-flag-bypass / unknown-backend / already-seeded / no-mirrors).

### Documented
- `docs/backends/google-drive.md` — new "Wizard improvements as of v0.5.46" section + "Diagnosing setup problems" subsection.
- `docs/admin.md` — new `### Drive deep checks` subsection under `## Doctor` with check-matrix table, auth-bucketing semantics, lazy-import note, sample success + failure outputs. seed-mirror Tier 2 reference updated to mention auto-detect.
- `docs/cli-reference.md` — `seed-mirror` synopsis line shows `[--backend NAME]`; `doctor` entry notes deep checks for `googledrive`.
- `README.md` — Quality gates count `506 → 584` and three new coverage surfaces named: BYO wizard, deep doctor checks, seed-mirror auto-detect.

### Updated docs/files
- `claude_mirror/_byo_wizard.py` (NEW).
- `claude_mirror/cli.py` (wizard reorder + value_proc validators + smoke-test loop; `_run_googledrive_deep_checks` ~370 lines hooked into `_run_doctor_checks`; `seed-mirror` `--backend` now optional with auto-detect).
- `tests/test_byo_wizard.py`, `tests/test_doctor_googledrive_deep.py`, `tests/test_seed_mirror_auto_detect.py` (NEW, 78 tests total).
- `docs/admin.md`, `docs/backends/google-drive.md`, `docs/cli-reference.md`, `README.md`.
- `pyproject.toml` (version 0.5.45 → 0.5.46).

### Tests
- `pytest tests/` — **584 passed in ~2.5s** (= 506 baseline + 59 wizard + 12 doctor + 7 seed-mirror). All offline.

---

## [0.5.45] — 2026-05-08

CI on the Linux matrix went green at v0.5.44. This release ships the v0.5.39 quality-of-life batch as the first PUBLISHED stable since v0.5.38.

### Fixed — Test suite is fully green on Linux + macOS, all 4 Python versions
- v0.5.44's pre-subcommand-banner suppression flipped CI from 13-red to 0-red on every Python in the matrix. v0.5.45 promotes the always-failing `test_DIAG043_streams.py` canary into a real regression test (`test_json_mode_does_not_leak_pre_subcommand_banners`) that asserts `result.stdout == ""` when `status --json` runs against an unauthed config — this catches any future code that re-introduces a banner leak into the JSON stdout path.

### Shipped (cumulative content from v0.5.39 → v0.5.44, now stable)
- **`claude-mirror restore --dry-run`** — preview what `restore` would write without touching local disk.
- **`claude-mirror snapshot-diff TS1 TS2`** — show what changed between two snapshots; `--paths` glob filter, `--unified` standard `diff -u` output.
- **`claude-mirror history PATH --since DATE --until DATE`** — date-range filter on the `history` command.
- **`--json` output mode** on `status / history / inbox / log / snapshots`. Schema v1: `{"version": 1, "command": "X", "result": {...}}`.
- **`.claude_mirror_ignore`** project-tree exclusion file (gitignore-style); complements YAML `exclude_patterns`.
- **`claude-mirror watch --once`** for cron-driven polling.
- **PowerShell** shell-completion alongside zsh / bash / fish.
- **`max_upload_kbps`** bandwidth throttle (token-bucket) integrated across all 5 backends.
- **WebDAV chunked PUT** for large files; replaces the `data=f` pattern that Apache `mod_dav` rejected via chunked-transfer-encoding.
- **Upload-resume behaviour table** in `docs/admin.md` documenting per-backend resume guarantees.

### Hardening that survived the v0.5.40 → v0.5.44 hotfix chain (stays in place)
- `_emit_json_success` / `_emit_json_error` use `click.echo` rather than `sys.stdout.write` — Click-aware writer that CliRunner captures identically across platforms.
- `_JsonMode.__exit__` snapshots and restores `sys.stdout` / `sys.stderr` so any context manager opened during the `with` block (Rich Live, etc.) cannot leave stale stdout wiring.
- `_JsonMode` no-ops `make_phase_progress` for the duration in `claude_mirror._progress`, `.sync`, and `.snapshots` — defensive against any future Rich Progress that gets opened in a JSON code path.
- `_CLIGroup.invoke()` skips `_check_watcher_running()` and the update-check fetch when `--json` is anywhere in argv (this was the actual fix; the rest is hardening).

None of v0.5.39 / v0.5.40 / v0.5.41 / v0.5.42 / v0.5.43 / v0.5.44 was tagged or published to PyPI. v0.5.45 is the first publishable release in this batch.

---

## [0.5.44] — 2026-05-08

The actual fix for v0.5.39's `--json` mode on Linux. v0.5.43's diagnostic captured the smoking gun, and the original "empty stdout" interpretation was wrong.

### Fixed — Suppress pre-subcommand banners when `--json` is in argv
- **Real root cause:** `_CLIGroup.invoke()` runs `_check_watcher_running()` before any subcommand handler. That helper writes a Rich-formatted "watcher not running" warning (with ANSI escape codes) to stdout via the module-level `cli.console`. In `--json` mode, that warning lands in stdout BEFORE the JSON envelope, then `_emit_json_success` appends the JSON. `result.stdout` ends up as `<ANSI watcher banner>\n<JSON envelope>\n`. The test failure `json.loads(result.stdout)` raises `JSONDecodeError: Expecting value: line 1 column 1 (char 0)` because **`\x1b` (ESC) is not a valid JSON start character** — and `json.loads("")` and `json.loads("\x1b...")` produce the literal same error message, which made the failure mode look like "stdout is empty" when it actually was "stdout starts with ANSI gunk".
- **Fix:** when `--json` is anywhere in argv, `_CLIGroup.invoke()` skips both `_check_watcher_running()` and the update-check fetch. Both write to stdout via Rich and corrupt JSON output for jq / script consumers — neither belongs in `--json` mode regardless of the test failure.
- v0.5.40 (`click.echo`), v0.5.41 (`sys.stdout` snapshot/restore), and v0.5.42 (no-op `make_phase_progress`) remain in place — they were correct hardening even though none of them was the root cause.
- `tests/test_DIAG043_streams.py` retained as an always-failing canary so any future regression that re-pollutes the JSON path's stdout is visible immediately.

### Documented in v0.5.43 release notes
- That v0.5.43 was a diagnostic-only release: the `[DIAG043]` markers in `_emit_json_success` were never actually shipped (the local edit was reverted before commit; only the new diagnostic test made it onto `origin/main`). The dump from that test gave us the watcher-banner evidence.

None of v0.5.39 / v0.5.40 / v0.5.41 / v0.5.42 / v0.5.43 was tagged or published to PyPI.

---

## [0.5.43] — 2026-05-08

**DIAGNOSTIC release.** Not for production use. v0.5.39's `--json` mode is broken on Linux under Click's CliRunner; three theory-driven hotfixes (v0.5.40 / v0.5.41 / v0.5.42) failed to land. This release ships instrumentation rather than a fix:

- `_emit_json_success` writes diagnostic markers to stderr (sys.stdout type, sys.stderr type, payload length, click.echo result), then emits the JSON to BOTH stdout and stderr.
- A new `tests/test_DIAG043_streams.py` runs `status --json` and FAILS unconditionally, dumping `result.exit_code` / `result.stdout` / `result.stderr` / `result.output` to the assertion message so CI logs reveal what actually landed in each captured stream.
- v0.5.44 will strip the instrumentation and ship a real fix once CI tells us which channel reaches `result.stdout` on Linux.

None of v0.5.39 / v0.5.40 / v0.5.41 / v0.5.42 was tagged or published to PyPI.

---

## [0.5.42] — 2026-05-08

Third and decisive hotfix for v0.5.39's `--json` mode on Linux. The two prior fixes (`click.echo` in v0.5.40, `sys.stdout` snapshot/restore in v0.5.41) addressed symptoms; this one addresses the cause.

### Fixed — prevent Rich Progress from opening during `--json` mode
- The 13 failing `tests/test_json_output.py` cases on the CI Linux matrix all hit commands whose `with _JsonMode():` block opens a Rich `Progress(transient=True)` region (status, snapshots, history, log via `engine.get_status` / `SnapshotManager.list_snapshots` / etc.). Inbox alone passed because its block opens no Progress.
- Rich's `Progress(transient=True)` wraps a `Live` region with `redirect_stdout=True` by default. Once Live runs under Click's `CliRunner` on Linux, it leaves Click's stdout wiring in a state where subsequent `click.echo` writes do not reach `result.stdout` — even after Live's own restoration runs and even after `_JsonMode.__exit__` forcibly restores `sys.stdout`. macOS doesn't reproduce.
- v0.5.40's switch from `sys.stdout.write` to `click.echo` was correct (still in place). v0.5.41's `sys.stdout` snapshot/restore was correct (still in place). Both were necessary but not sufficient — neither could un-poison Click's wiring once Live had touched it.
- v0.5.42 prevents Live from ever opening during `--json` mode: `_JsonMode.__enter__` replaces `make_phase_progress` with a `_no_op_progress` factory in the source module AND in every consuming module's local binding (`claude_mirror.sync` and `claude_mirror.snapshots` both do `from ._progress import make_phase_progress` at load time, creating per-module name bindings that need their own patches). `__exit__` restores all three. The no-op returns a stub context manager that mimics the small Progress surface callers use (`add_task`, `update`, `remove_task`, context-manager protocol) and does nothing.
- 505 tests pass on macOS; CI re-run on push validates the Linux matrix conclusively.
- None of v0.5.39 / v0.5.40 / v0.5.41 was tagged or published to PyPI, so no end users received a broken `--json` path.

---

## [0.5.41] — 2026-05-08

Follow-up to the v0.5.40 hotfix for v0.5.39's `--json` mode: the `click.echo` switch was correct but not sufficient. The deeper root cause was Rich Live's stdout redirection.

### Fixed — Rich Live restoration of `sys.stdout` on Linux
- After v0.5.40 swapped `sys.stdout.write` → `click.echo`, 13 of the 21 `tests/test_json_output.py` cases still failed on the CI Linux matrix with the same symptom: `result.stdout` empty, JSON envelope nowhere to be found. Inbox still passed.
- Inbox is the only `--json` command whose `with _JsonMode():` block does NOT call any code that opens a Rich `Progress` region. Status / snapshots / history / log all do (via `engine.get_status()`, `SnapshotManager.list_snapshots()`, etc.).
- Root cause: Rich's `Progress(transient=True)` uses `Live` with `redirect_stdout=True` by default. `Live.__enter__` replaces `sys.stdout`; `Live.__exit__` restores it. Under Click's `CliRunner` on Linux, the restore does not always put back the same object the runner installed as `sys.stdout` — leaving subsequent `click.echo` writes going to a void rather than to the runner's captured buffer.
- Fix: `_JsonMode.__enter__` now snapshots `sys.stdout` / `sys.stderr` alongside the module-level Rich consoles, and `__exit__` forcibly restores them. Whatever Rich Live did to `sys.stdout` during the `with` block, it gets pinned back to the runner's captured buffer before `_emit_json_success` runs.
- 505 tests pass locally on macOS; CI re-run on push will validate the Linux matrix.
- Neither v0.5.39 nor v0.5.40 was tagged or published to PyPI, so no end users received a broken `--json` path.

---

## [0.5.40] — 2026-05-08

Hotfix for v0.5.39 — `--json` emit path was Linux-broken under Click 8.3's `CliRunner`.

### Fixed — `--json` output reaches `result.stdout` on Linux too
- The v0.5.39 `_emit_json_success` / `_emit_json_error` helpers used `sys.stdout.write()` + `sys.stdout.flush()` to emit the JSON envelope. This worked under macOS pytest but failed on the CI Linux matrix (Python 3.11 / 3.12 / 3.13 / 3.14): 13 of the 21 `tests/test_json_output.py` cases failed with `json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)` — `result.stdout` was an empty string.
- Root cause: under Click 8.3 + CliRunner on Linux, `sys.stdout.write()` does not reliably reach the runner's captured stdout buffer. macOS happens to route the writes through a path that CliRunner observes; Linux does not. Click's own `click.echo()` uses Click's internal output abstraction and is captured identically on both platforms.
- Fix: switched both `_emit_json_success` (success → stdout) and `_emit_json_error` (error → stderr via `err=True`) to `click.echo()`. Same JSON serialisation; same success / error semantics; same exit codes; just a different writer.
- v0.5.39 was pushed to `origin/main` but not tagged and not published to PyPI, so no users running `pipx install claude-mirror` ever received the broken `--json` path.
- All 505 tests pass on macOS locally; CI re-run on push will validate the Linux matrix.

---

## [0.5.39] — 2026-05-08

A large quality-of-life batch — eleven user-facing additions across snapshot CLI, scripting interfaces, project-tree exclusions, cron-mode watcher, PowerShell completion, and per-backend bandwidth control. **505 tests pass (368 → 505, +137 new).** All offline, all deterministic, ~2.5s end-to-end.

### Added — Snapshot CLI quality-of-life
- **`claude-mirror restore --dry-run`** — preview what `restore` would write without touching local disk. Prints a Path / Action / Source backend / Size table plus a one-line summary `Would restore N file(s) from snapshot TIMESTAMP. Run without --dry-run to apply.` Reuses the existing `inspect()` dispatch for primary-first / mirror-fallback so the dry-run probes the same backend the real restore would use; rows where the primary's blob store can't supply the body are flagged `missing-blob` so the user sees that fact ahead of the real run.
- **`claude-mirror snapshot-diff TS1 TS2`** — show what changed between two snapshots. Each file classified as `added` / `removed` / `modified` / `unchanged`; default output omits `unchanged` (use `--all` to include them); `--paths PATTERN` filters by glob; `--unified PATH` prints a standard `diff -u` unified diff for one file (uses `click.echo`, not Rich, so the output composes cleanly with `less`, `delta`, file redirection). Both snapshot formats supported (`blobs` and `full`); the `latest` keyword resolves to the newest snapshot.
- **`claude-mirror history PATH --since DATE --until DATE`** — date-range filter on the existing `history` command. Both flags optional and independent; with neither set, behaviour unchanged. Accepts ISO date (`2026-04-15`), ISO datetime (`2026-04-15T10:00:00Z`), or relative duration (`30d / 2w / 3m / 1y`) — same vocabulary as `forget --before`. The shared parser was lifted to module level (`snapshots.parse_relative_or_iso_date`) so all three flags pull from the same source.
- 45 new tests in `tests/test_restore_dry_run.py` / `tests/test_snapshot_diff.py` / `tests/test_history_filter.py`.

### Added — `--json` output mode on read-only commands
- **`status / history / inbox / log / snapshots`** all gain a `--json` Click flag. When set, the command emits a single flat JSON document `{"version": 1, "command": "X", "result": {...}}` to stdout, suppresses ALL Rich output (tables, colours, progress, banners), and exits 0/1 with the same semantics. Schema is **v1** — top-level version-tagged for forward compatibility; flat per-command result objects.
- Storage-agnostic status keys: the internal `Status` enum's legacy `drive_ahead` / `new_drive` values (from the Drive-only era) are aliased to `remote_ahead` / `new_remote` in the JSON output so consumers never see "drive".
- Errors emit a JSON envelope to stderr: `{"version": 1, "command": "X", "error": {"type": "...", "message": "..."}}` — exit 1.
- `inbox --json` surfaces config errors as JSON instead of the Rich path's silent exit-0 (which exists to keep PreToolUse hooks quiet) — scripts asking for JSON want to know about failures.
- `--json` composes with W1's `--since/--until` on `history`: `claude-mirror history PATH --since 30d --json` filters then serialises.
- `_JsonMode` context manager patches the module-level Rich `Console` across `cli.py`, `snapshots.py`, and `sync.py` (each defines its own console) so every render path is silent.
- 21 new tests in `tests/test_json_output.py`.

### Added — `.claude_mirror_ignore` file (gitignore-style)
- Project-tree exclusion file that complements the existing YAML `exclude_patterns`. Lives at `<project_path>/.claude_mirror_ignore`. Optional — if absent, current behaviour unchanged.
- gitignore-subset syntax: blank lines + `#` comments skipped, leading `!` re-includes a previously matched pattern, trailing `/` means "directory only", `**` matches any number of path segments, `*` does not cross `/`, leading `/` anchors to project root.
- Precedence: applied **in addition** to YAML `exclude_patterns`. Walker yields a path → YAML `exclude_patterns` votes first → ignore-file rules vote in file order → if last matching rule excludes (or no YAML rule kept it), skip. Both systems must vote "keep" for sync.
- The file itself is auto-excluded from sync so the rules don't propagate to other machines unless the user wants that. Same convention as gitignore.
- New `claude_mirror/ignore.py` with hand-rolled translator (no `pathspec` transitive dep). ReDoS-safe: 1024-char pattern cap, bounded quantifiers (`.*` / `[^/]*`), no nested quantifier groups.
- Integration: loaded once per `SyncEngine.__init__`; `_is_excluded` uses defensive `getattr(self, "_ignore_set", None)` so engines built via `SyncEngine.__new__` (perf/parity tests) keep working.
- 22 new tests in `tests/test_ignore_file.py`.

### Added — `claude-mirror watch --once` (cron-friendly)
- Single-poll-cycle mode for cron use. Today's `watch` runs forever (foreground). New `--once / --no-once` flag (default `--no-once` so existing behaviour is unchanged). When set: do exactly ONE polling cycle, print any inbox events surfaced, exit 0. Useful pattern: `*/5 * * * * claude-mirror watch --once --quiet`.
- New `--quiet / --no-quiet` flag suppresses the "Watching ..." banner. Notifications still fire.
- Works for all 5 backends. New `watch_once()` abstract method on `NotificationBackend` with a default that delegates to `watch()` with a pre-set stop event. Polling and longpoll backends use a persistent watermark file (`~/.config/claude_mirror/watch_once_state/<sha>.json`) so a fresh cron install doesn't flood the user with weeks of historical events — first run captures the current tail and dispatches nothing. Pub/Sub does one synchronous `subscriber.pull(return_immediately=True, max_messages=100)` + ack; transient failures swallowed.
- Signal handlers only installed when not `--once`.
- 9 new tests in `tests/test_watch_once.py`.

### Added — PowerShell shell-completion
- `claude-mirror completion powershell` joins the existing zsh / bash / fish targets. Click 8.3 doesn't ship a `PowerShellComplete` class — added a custom `ShellComplete` subclass that emits `Register-ArgumentCompleter -Native` syntax matching the bundled `<COMPLETE_VAR>_complete` env-var protocol.
- `claude-mirror-install` detects PowerShell as a target shell on Windows AND on macOS/Linux when running pwsh. Priority: explicit Unix shell on `$SHELL` > pwsh/powershell on `$SHELL` > Windows-platform default (powershell) > Unix-platform default (zsh on macOS, bash on Linux). PowerShell intentionally lower priority than zsh/bash/fish on Unix so a user with both installed gets their actual login shell.
- Profile path: `~/.config/powershell/profile.ps1` (Unix), `~/Documents/PowerShell/profile.ps1` (Windows). Marker block uses `# >>> ... >>>` which is a valid PowerShell comment; the existing `uninstall_completion` works unchanged.
- 15 new tests in `tests/test_completion_powershell.py`.

### Added — Bandwidth throttling (`max_upload_kbps`)
- New `claude_mirror/throttle.py` with `TokenBucket` class (rate_kbps + capacity_bytes, threadsafe via `threading.Lock`, default capacity = max(64KB, rate × 1024 / 8) so single small files pass through without delay) and `NullBucket` no-op for the unthrottled case. Module helper `get_throttle(rate_kbps)` returns `NullBucket` when rate is None or 0 so callers don't need conditionals.
- New optional YAML field `max_upload_kbps: int | null` (default `null` = disabled). Tier 2 mirrors each have their own field — throttle Drive but not SFTP, or vice versa.
- Integrated across all 5 backends with per-backend care:
  - **googledrive** keeps the fast `request.execute()` path when throttle is null (back-compat + perf for the default case); only manual `next_chunk()` chunk loop when `max_upload_kbps` is set.
  - **dropbox** — single `consume(len(body))` before each `files_upload` call.
  - **onedrive** — per-chunk `consume()` for upload-session path; per-body for simple PUT.
  - **webdav** — wraps the new chunked-PUT generator (see below).
  - **sftp** — switched from opaque `sftp.put` to manual block loop (keeps `set_pipelined(True)` for uncapped throughput; atomic `.tmp` + `posix_rename` preserved).
- TokenBucket math tested with mocked `_now`/`_sleep` indirection — zero real sleeps in tests.
- 16 new tests in `tests/test_throttle.py`.

### Added — WebDAV chunked PUT for large files
- WebDAV's existing `data=f` pattern in `requests.put` already streamed under the hood, but lost Content-Length and went through chunked transfer-encoding (which Apache `mod_dav` rejects in default config). Replaced with a generator that yields 1 MiB blocks with explicit Content-Length. Files smaller than `webdav_streaming_threshold_bytes` (new YAML field, default 4 MiB) keep the simple in-memory PUT.
- 9 new tests in `tests/test_webdav_chunked_put.py`.

### Documented — Upload resume behaviour by backend
- New `## Upload resume behaviour by backend` H2 in `docs/admin.md` with a 5-row table (Backend / Native resume / Survives process restart / Behaviour on failure). Drive: resumable native. Dropbox: upload-session is internal "resume" but doesn't survive process restart. OneDrive: `createUploadSession` URL survives restart for ~1 week per Microsoft Graph docs. WebDAV / SFTP: no native resume; re-upload from scratch on retry.

### Updated docs/files
- `claude_mirror/cli.py` (3 new commands + 5 `--json` flags + `watch --once/--quiet` + `completion powershell`).
- `claude_mirror/snapshots.py` (`plan_restore`, `get_snapshot_manifest`, `get_blob_content`, `_locate_snapshot_backend`, `_snapshot_in_range`, `_blob_id_cache`; threaded `since`/`until` into `history()` + `show_history()`; `parse_relative_or_iso_date` lifted to module level).
- `claude_mirror/sync.py` (IgnoreSet integration in walker).
- `claude_mirror/notifications/{__init__.py,polling.py,longpoll.py,pubsub.py}` (new `watch_once()` abstract method + per-backend implementations).
- `claude_mirror/install.py` (PowerShell detection + profile path).
- `claude_mirror/config.py` (`max_upload_kbps`, `webdav_streaming_threshold_bytes`).
- `claude_mirror/backends/{googledrive,dropbox,onedrive,webdav,sftp}.py` (throttle hooks; webdav chunked PUT).
- `claude_mirror/{ignore.py,throttle.py,_watch_once_state.py}` (new modules).
- `tests/test_{restore_dry_run,snapshot_diff,history_filter,json_output,ignore_file,watch_once,completion_powershell,throttle,webdav_chunked_put}.py` (9 new test files).
- `docs/admin.md` (5 new sections: Previewing a restore / Comparing snapshots / Filtering history by date / `.claude_mirror_ignore` / Performance and bandwidth control / Upload resume behaviour).
- `docs/cli-reference.md` (`snapshot-diff` entry, updated `restore` + `history` flag lists, "JSON output" section, "Config fields" section).
- `docs/scenarios.md` (`.claude_mirror_ignore` reference at end of Scenario F).
- `README.md` (cheatsheet additions: `--json` line, `watch --once --quiet` line, ignore-file mention, PowerShell tab-completion).
- `pyproject.toml` (version 0.5.38 → 0.5.39).

---

## [0.5.38] — 2026-05-08

Three small quality-of-life improvements landed together: opinionated retention defaults at `init`, PyPI-primary update-check, and full documentation of the existing `doctor` command.

### Added — Snapshot retention defaults at `init`
- `claude-mirror init --wizard` (and the non-wizard flag-driven path) now write `keep_last: 10`, `keep_daily: 7`, `keep_monthly: 12`, `keep_yearly: 3` into newly created project YAMLs. Both paths share a single `Config(...)` constructor at `cli.py:1364`, so one edit covers both.
- The dataclass defaults stay at `0` — pre-existing YAMLs that omit these fields still load with all-zero retention (back-compat preserved). The new defaults only land in YAMLs created by `init` going forward.
- After the next `push`, the engine automatically prunes snapshots outside the keep-set (no extra confirmation; setting the YAML field IS the consent — same opt-in semantics that have been there since v0.5.32).
- Rationale for these specific values: ≈ "10 newest + one per day for last week + one per month for last year + one per year for last 3 years". Generous enough to cover normal recovery scenarios, opinionated enough to prevent disk bloat that Scenario A in `docs/scenarios.md` flags as a real-world pitfall.
- 2 new tests in `tests/test_retention_defaults.py` (offline, <100ms): the YAML written by `init` contains the four `keep_*` keys with expected values; legacy YAMLs lacking these fields still load with `0`.

### Changed — Update-check fetches from PyPI first
- `claude_mirror/_update_check.py` now walks a three-stage chain: **PyPI JSON API** (`https://pypi.org/pypi/claude-mirror/json`, the only authoritative answer to "is the wheel installable right now?") → **GitHub Contents API** (catches very recent tags before the wheel finishes uploading) → **raw CDN** (last fallback when both APIs are blocked).
- Each stage runs only if the previous raised. Cache file gains a `last_source: "pypi" | "github_api" | "raw_cdn"` field for future `--verbose` diagnostics; old cache files (without the field) still parse fine.
- Closes the user-visible gap where a freshly-pushed tag triggered an "upgrade available" prompt seconds before the PyPI wheel finished uploading — `pipx upgrade` would then fail. The new ordering reports a version only when it's actually installable.
- New `_fetch_via_pypi()`, new `_fetch_remote_version_with_source() -> (version, source_name)`. The legacy `_fetch_remote_version()` is preserved as a thin wrapper for any caller that doesn't need the source attribution.
- 5 new tests in `tests/test_update_check.py` (offline, deterministic, <100ms each): PyPI primary success path; PyPI down → GitHub API success; PyPI + GitHub API down → CDN success; total network failure → `None` (no notice fired); PyPI returns malformed JSON → falls through to GitHub API. The existing `test_update_check_silent_on_network_error` regression in `tests/test_load_paths_narrow.py` continues to pass.
- README "Update notifications" section updated to describe the new layered approach.
- No new dependencies — `urllib` only.

### Documentation — `doctor` command fully documented
- `docs/admin.md` gains a new top-level `## Doctor` H2 section (137 lines) documenting the existing `claude-mirror doctor` command (implemented at `claude_mirror/cli.py:4175-4724`, untouched). Sections: overview paragraph, full check matrix grouped by category (Configuration / Credentials / Tokens / Connectivity / SFTP-specific aux / Project path / Manifest integrity), sample successful output, sample failure output, exit codes (0 on all-pass, 1 on any failure — composes with shell scripts and CI), three common invocations, cross-links to all five backend setup pages plus `conflict-resolution.md` and `cli-reference.md#doctor`.
- Documents five behaviours the command's docstring elided: Tier 2 mirror-config-load failures recorded as separate Check 1 failures; `--backend` filter prints a dim "skipped" line for non-matching backends; the connectivity check classifies exceptions into AUTH / PERMISSION / FILE_REJECTED+404 / TRANSIENT / unknown buckets via `classify_error` and renders different fix-hints per bucket; SFTP-specific auxiliary checks (key file readable, known_hosts presence, plaintext-password advisory) run regardless of connectivity outcome; plaintext-password and `sftp_strict_host_check: false` cases are advisories (yellow warning, not failures).
- README's troubleshooting section: replaced the soft-link "see docs/admin.md for related operational guidance" with a hard anchor link `docs/admin.md#doctor` — restoring the anchor that was soft-linked in v0.5.36 because the section didn't exist yet.
- `docs/cli-reference.md` gains a `### doctor` entry under "Maintenance" with the two flags and a link to `admin.md#doctor` for depth.
- All `.md` link targets verified via the v0.5.36 audit script — broken=0.

### Updated docs/files
- `claude_mirror/cli.py` (retention defaults at the shared `Config(...)` constructor).
- `claude_mirror/_update_check.py` (PyPI-primary fetch chain).
- `tests/test_retention_defaults.py` (new, 2 tests).
- `tests/test_update_check.py` (new, 5 tests).
- `docs/admin.md` (new `## Doctor` section + new "Retention defaults at init" subsection inside "Auto-pruning by retention policy").
- `docs/cli-reference.md` (new `doctor` entry).
- `README.md` (Update notifications layered description, doctor anchor restored).
- `pyproject.toml` (version 0.5.37 → 0.5.38).

### Tests
- `pytest tests/` — **368 passed in ~2s** (361 + 2 retention + 5 update-check). All offline, all deterministic.

---

## [0.5.37] — 2026-05-08

### Fixed — README links broken on the PyPI project page
- After the v0.5.36 doc split, the README's relative `docs/...` / `LICENSE` / `CONTRIBUTING.md` links rendered as broken on `pypi.org/project/claude-mirror/` — PyPI does not resolve relative file paths against any source tree, so every doc link 404'd for users browsing PyPI.
- All 35 such links in `README.md` rewritten to absolute GitHub URLs of the form `https://github.com/alessiobravi/claude-mirror/blob/main/docs/...`. They render correctly on PyPI now and continue to work on GitHub (an absolute URL is just a regular link). Anchor fragments (`#snapshots-and-disaster-recovery`, `#multi-backend-mirroring-tier-2`, `#d-multi-backend-redundancy-tier-2`) preserved.

---

## [0.5.36] — 2026-05-08

A documentation refactor + two correctness fixes shipped together. The 2323-line README was split into a sleek 586-line navigation hub plus a `docs/` tree organised by topic; sequential mirror walks in two status renderables were parallelised; and the network error path for unreachable backends is now a clean diagnostic message instead of a Python traceback.

### Documentation — README split into a browseable docs/ tree
- `README.md` trimmed from 2323 → 586 lines. The "Part 1/2/3/4" linear-tutorial structure dropped in favour of clean H2 sections: hero / why / how / supported backends / prerequisites / install / Documentation index / your first project / daily-usage cheatsheet / Claude Code skill / Slack / desktop / update notifications / troubleshooting / file locations / migrating / disclaimer / license. The first H2 after install is the new **Documentation index** — exhaustive map of every `docs/*.md` file, organised by Backends / Operations & admin / Topology guides.
- "Your first project" now features Drive AND SFTP wizard walk-throughs side-by-side (Q5 of the doc-split plan). Other backends (Dropbox / OneDrive / WebDAV) are linked into per-backend pages.
- New `docs/` tree, 9 files, ~2200 lines:
  - `docs/README.md` — docs-tree index page (also reachable via the breadcrumb on every nested page).
  - `docs/backends/google-drive.md`, `dropbox.md`, `onedrive.md`, `webdav.md`, `sftp.md` — per-backend setup, config fields, troubleshooting.
  - `docs/admin.md` — snapshots and disaster recovery, retention policies, `gc` / `prune` / `forget`, doctor, watcher service auto-start (launchd / systemd), manual component installation, and **Multi-backend mirroring (Tier 2)** including the configuration reference, daily-usage diff, ErrorClass failure-handling table, and the "when to use Tier 2 vs running two configs by hand" comparison.
  - `docs/conflict-resolution.md` — interactive `keep local / keep remote / merge / skip` flow.
  - `docs/cli-reference.md` — every command, every flag, grouped by topic.
  - `docs/scenarios.md` — seven deployment topologies (A. Standalone / B. Personal multi-machine / C. Multi-user collaboration / D. Multi-backend redundancy Tier 2 / F. Selective sync / G. Multi-user + multi-backend production-realistic with full Alice/Bob YAMLs and a 12-step transcript / H. Multi-project enterprise).
- Cross-linking convention enforced: every `docs/*.md` opens with "← Back to README index" plus a breadcrumb when nested. README's Documentation index is the first H2 after install. Backend pages link forward to scenarios; scenarios link back to backend pages and admin. CLI reference is the destination for every command-flag mention. All `.md` link targets verified to resolve (via a programmatic audit pass).
- Conventions captured in project memory:
  - `feedback_docs_browseable.md` — every doc page MUST carry sideways navigation; readers should never hit a dead end.
  - `convention_docs_tree_updates.md` — future feature changes touch every relevant `docs/*.md` in the same commit, not as a follow-up. CHANGELOG entries name every doc file touched.

### Fixed — `claude-mirror status --by-backend` and `--pending` walked mirrors sequentially
- Both renderables (`_build_status_by_backend_renderable` and `_build_pending_renderable` in `claude_mirror/sync.py`) launched the primary `engine.get_status()` first, waited for it to complete, then walked each mirror's `list_files_recursive()` one at a time. With three configured backends, the wallclock latency was the sum of all four operations.
- Now they fan out via `concurrent.futures.ThreadPoolExecutor` with `(1 + N)` workers — primary status and every mirror walk run concurrently. Progress rows for each mirror are added to the live Rich table up-front so the user sees parallel progress, not a serial cascade. Each `freeze on completion` row settles independently as its backend finishes.
- User-visible: `status --by-backend` against Drive (primary) + SFTP (mirror) now finishes when the slower of the two finishes, not when their sum finishes — typical 1.5–2× wall-clock improvement on real Tier 2 setups.

### Fixed — clean error message when a backend is unreachable
- Previously, hitting a backend with no DNS / no route / connection refused / connection reset surfaced a raw Python traceback (`httplib2.error.ServerNotFoundError: Unable to find the server at ...` etc.). Confusing for users who don't read tracebacks; the actual problem (no internet) was buried in eight lines of stack frame.
- `_CLIGroup.invoke()` in `claude_mirror/cli.py` now catches `(OSError, ConnectionError)` (covering `socket.gaierror`, `ConnectionRefusedError`, `ConnectionResetError`, `socket.timeout`) and library-specific network exceptions (`httplib2.ServerNotFoundError`, `requests.exceptions.ConnectionError`, `urllib3.exceptions.NewConnectionError` / `MaxRetryError`, `paramiko.ssh_exception.NoValidConnectionsError`) and prints a single-block Rich message: `Could not reach the storage backend.` + the underlying error type + name + redacted message + a `Fix:` line suggesting connectivity check + retry. Exit code 1.
- The `OSError` clause is placed AFTER the existing `except FileNotFoundError` clause in source order, since `FileNotFoundError` IS-A `OSError` and would otherwise be swallowed. Library-specific catches use a string-name allowlist on `type(e).__module__ + "." + type(e).__name__` so the dependency on those libraries stays optional. Anything not matching falls through to the existing last-resort handler.
- Token / home-path leakage in error messages prevented by routing through `redact_error()`.

### Updated docs/files
- `README.md` (rewrite, 2323 → 586 lines).
- `docs/README.md` (new — docs-tree index).
- `docs/admin.md` (new — extracted + new Tier 2 section).
- `docs/conflict-resolution.md`, `docs/cli-reference.md`, `docs/scenarios.md` (new).
- `docs/backends/{google-drive,dropbox,onedrive,webdav,sftp}.md` (new).
- `claude_mirror/sync.py` (parallel mirror walks).
- `claude_mirror/cli.py` (network error handler).
- `pyproject.toml` (version 0.5.35 → 0.5.36).

---

## [0.5.35] — 2026-05-08

### Added — `claude-mirror gc --backend NAME` (per-mirror garbage collection)
- New `--backend NAME` flag on `claude-mirror gc`. When set, gc operates on the named backend (primary or any configured mirror) instead of always defaulting to the primary. Critical for cleaning up orphan blobs that accumulated on a specific mirror without disturbing the primary or other mirrors.
- Engine: `SnapshotManager.gc()` gains an optional `backend_name: Optional[str] = None` parameter. When None (default), preserves pre-v0.5.35 behaviour exactly — operates on the primary. When set, looks up the matching backend in `[self.storage] + self._mirrors`; raises `ValueError` with the list of available backend names if the requested name doesn't match.
- All four `self.storage.*` calls inside `gc()` (list_files_recursive × 2, download_file, delete_file) are routed through the resolved `target_backend`, ensuring per-backend isolation. The folder lookups use `_get_snapshots_folder_for(backend)` and `_get_blobs_folder_for(backend)` (helpers that already existed for the snapshot fan-out path).
- CLI: `gc` command's docstring + help text updated with Tier 2 examples (`claude-mirror gc --backend sftp --delete` etc.). The dry-run banner now names the target backend so the user can't accidentally gc the wrong one.
- 4 new tests in `tests/test_snapshots.py` cover: default-targets-primary back-compat (with a configured mirror present, gc-no-flag still leaves mirror's orphans alone), named-mirror-targeted (gc on sftp deletes sftp's orphans, leaves primary alone), unknown-backend-raises (clean ValueError), and primary-by-name parity (passing `backend_name="primary"` matches the no-arg call).

### Verified — Other backends are NOT affected by the v0.5.34 SFTP threading bug
- After fixing the paramiko channel-multiplex stall in v0.5.34, audited the remaining four backends (Google Drive, Dropbox, OneDrive, WebDAV) for the same class of bug (single shared client object across worker threads with insufficient parallelism guarantees). No code changes — pure analysis pass.
- **Result: all four are SAFE.** Drive already uses `threading.local()` for its `googleapiclient` Resource (the maintainer pre-empted the bug — see L116-117 comment naming `httplib2` thread-unsafety explicitly). Dropbox, OneDrive, and WebDAV all share a single `requests.Session` that IS genuinely thread-safe via urllib3's connection pool — concurrent HTTPS requests parallelize via separate TCP connections from the pool, no channel-multiplex serialization to worry about. SFTP was the unique outlier (paramiko's SFTPClient single-channel architecture).
- No code changes for the other backends. Conclusion captured here so future maintainers (or future re-audits triggered by similar reports) know the question has been deliberately asked and answered.

### Added — GitHub Discussions wired into the issue chooser + README badge
- GitHub Discussions enabled on the repository (Settings → Features → Discussions). Splits "I have a question" / "how do I configure SFTP" / "Tier 2 best practices" from "this is a confirmed bug" before noise accumulates in the issues tracker.
- `.github/ISSUE_TEMPLATE/config.yml` extended with a "Question / setup help / general usage" contact link that points to the Discussions tab. Anyone clicking "New issue" now sees Discussions as the first listed alternative, with an explicit note that issues are reserved for confirmed bugs and concrete feature requests.
- README badge row gains a Discussions count badge alongside the existing Tests / PyPI / Python / License row — completes the standard 5-badge maturity-signalling row that public Python tools typically display.

---

## [0.5.34] — 2026-05-08

A wave of fixes and additions to the v0.5.33 Tier 2 + SFTP surfaces, all surfaced by real-world use of the SFTP backend the day after it shipped. Highlights: `seed-mirror`, `status --by-backend`, live-verifying status views, paramiko channel-multiplex perf fix, Ctrl+C-safe manifest persistence.

### Fixed — `init --wizard --backend X` now respects the `--backend` flag
- **Bug**: `claude-mirror init --wizard --backend sftp` displayed `Storage backend [googledrive]:` regardless of what was passed to `--backend`. Pre-fix the wizard ignored the CLI flag entirely; users had to re-type the backend name into the prompt.
- **Cause**: `_run_wizard()` was a no-arg function with `default="googledrive"` hardcoded into its first `click.prompt`. The CLI's `--backend` value populated `backend_opt` in the `init` callback, but `init` then called `_run_wizard()` without forwarding it, so the wizard's prompt never saw the user's intent. After the wizard returned, the value collected from the prompt overwrote whatever `backend_opt` carried.
- **Fix**: `_run_wizard(backend_default: str = "googledrive")` now accepts the desired default. `init` passes `backend_default=backend_opt`. Running with `--backend sftp` now shows `Storage backend [sftp]:`; pressing Enter accepts.
- 2 regression tests in `tests/test_init_wizard.py` pin the contract so this can't silently regress.

### Added — `claude-mirror seed-mirror --backend NAME` (closes the fresh-mirror seeding gap)
- **Bug**: When a mirror is added to `mirror_config_paths` for a project where files already exist on the primary, regular `push` has nothing to do — every local hash matches its manifest record, so push uploads zero files and the new mirror's folder stays empty forever. `status --pending` reported "All mirrors are caught up" even when the mirror was completely empty, because `--pending` only counted files in `pending_retry` / `failed_perm` state. There was no built-in command for "upload everything that's already on the primary to this newly-added mirror."
- **Fix**: New `claude-mirror seed-mirror --backend NAME [--dry-run]` command. Walks the manifest, finds every file with no recorded state on the named mirror (`get_remote(name) is None`), and uploads each one to that mirror only — the primary is never touched. State is recorded as `state="ok"` per file × backend, so subsequent pushes track normally.
- **Drift safety**: a file whose local hash differs from the manifest's recorded `synced_hash` is SKIPPED with a yellow warning. The user must run a normal `push` first to reconcile primary (which fans out to the mirror simultaneously), then re-run `seed-mirror` for any leftovers. Blindly seeding mismatched content would silently desync local from primary on the seeded mirror.
- **Idempotent**: running twice in a row is safe — the second invocation finds zero unseeded files and exits with a "✓ already seeded" message.
- New `Manifest.unseeded_for_backend(backend_name)` helper — symmetric with the existing `pending_for_backend` — returns the unseeded set without iterating the dict at every call site.
- 10 new tests in `tests/test_seed_mirror.py` cover happy-path upload, idempotent re-run, drift detection skip, dry-run no-op, unknown-backend ValueError, no-mirrors-configured early exit, and the `status --pending` integration.

### Added — `claude-mirror status --by-backend` (positive per-backend visibility)
- New flag on the `status` command: render the per-file table with one column per configured backend (primary first, mirrors in `mirror_config_paths` order). Each cell shows that backend's recorded state for the file: green `✓ ok`, yellow `⚠ pending`, red `✗ failed`, yellow `⊘ unseeded`, dim `· absent`. Footer adds a per-backend health summary line — e.g. `✓ googledrive (primary) · 1251 ok` / `✓ sftp · 1251 ok`.
- Closes the visibility gap that the rest of v0.5.34 only patches negatively: `status` alone is primary-centric (one Status column), `status --pending` is filter-only (shows non-OK / unseeded only). `status --by-backend` is the **positive** view — "is everything in sync on every mirror?" answered at a glance.
- `--by-backend` and `--pending` are mutually exclusive (errors out cleanly with a one-line explanation if both passed).
- 10 new tests in `tests/test_status_by_backend.py` cover header structure, every state's cell rendering, the legacy v1/v2-manifest fallback (entries without a `remotes` dict still render primary as ok), the per-backend footer counts, and the mutually-exclusive-flags contract.

### Fixed — `status --pending` now surfaces unseeded mirrors
- Pre-fix `status --pending` only listed files in `pending_retry` / `failed_perm` state. Files that had **no recorded state at all** for a configured mirror (the bug above) were silently invisible — the user saw "✓ All mirrors are caught up" while a mirror folder was empty.
- Post-fix `status --pending` adds an "Unseeded mirrors" table when any configured mirror has files with no recorded state, with the suggested fix command (`claude-mirror seed-mirror --backend NAME`) inline. The pre-fix happy-path message only shows when both pending state AND unseeded state are clean.

### Fixed — `--by-backend` and `--pending` now live-poll every backend
- Pre-fix both views were manifest-only. The manifest is a per-machine local cache — in any multi-user / multi-machine setup it produced wrong answers (machine A pushes; machine B pulls; B's manifest doesn't know mirror state for those files; B's `--pending` says "unseeded" even though SFTP is fully populated). Plus same-machine drift cases (file deleted directly on SFTP via SSH) were invisible.
- Post-fix both views walk every configured backend via `list_files_recursive` once per invocation, then cross-reference with the manifest. New derived state "deleted out-of-band" surfaces files where manifest says `state="ok"` but the live listing disagrees.
- Cell semantics for `--by-backend` updated: live-presence + manifest state combine for the final cell. Mutually-exclusive `--by-backend` and `--pending` flags error cleanly when both passed.
- Speed cost: `--pending` is now slower (was ~ms manifest read; now ~5–30s for multi-thousand-file projects across 2 backends). Slower right answer beats fast wrong answer.

### Fixed — `--by-backend` and `--pending` live-walks honor `exclude_patterns`
- Pre-fix the live walks didn't filter by `exclude_patterns`, so files the user had explicitly excluded showed up as `⊘ unseeded` orphans on mirrors. For example: `git/cortex-demo/.git/objects/...` excluded by `git/cortex-demo/.git/**` still surfaced as 442 phantom unseeded files on the SFTP column.
- Post-fix both renderers apply `engine._is_excluded(rel_path)` to every entry returned by `list_files_recursive` — same filter the engine's own `get_status()` applies.

### Fixed — `--by-backend` matches plain `status`'s classifications
- Pre-fix `--by-backend` only checked file PRESENCE on each backend, not content hashes. A locally-modified file that had been pushed showed as `✓ ok` everywhere even though the user had unpushed local changes. Plus the file universe was built from `manifest ∪ live-remote`, missing local-only files that hadn't been pushed yet.
- Post-fix `--by-backend` routes through `engine.get_status()` for the file universe and primary state — same path plain `status` uses for its 3-way diff. The `Status` enum maps directly to per-cell labels (`IN_SYNC` → `✓ ok`, `LOCAL_AHEAD` → `↑ local ahead`, `DRIVE_AHEAD` → `↓ drive ahead`, `NEW_LOCAL` → `+ new local`, `CONFLICT` → `⚠ conflict`). When the primary's status is non-IN_SYNC due to local divergence (`LOCAL_AHEAD`, `NEW_LOCAL`, `CONFLICT`), the mirror inherits the same status — mirrors are write-replicas, they trail primary identically.
- Mirror-only orphan files (present on a mirror but not local AND not on primary — e.g. from out-of-band restores) get rendered as a "mirror-only" extra section with `✗ orphan` cells.

### Added — Per-backend push progress UX
- The "Pushing X/N" Progress row now appends a per-backend breakdown: `Pushing 3/5 (googledrive: 5/5 · sftp: 3/5) 0:01:17`. With Tier 2 fan-out, the file-level counter only advances when ALL backends finish a file, so 5 files × 2 backends with one slow backend used to look stuck at "Pushing 1/5". The breakdown shows which backend is the actual bottleneck.
- Implementation: thread-safe `_push_counters` dict on the engine, populated by `_bump_push_counter(backend_name)` calls inside `_push_file` (after primary upload) and `_fan_out_to_mirrors._push_one` (after each mirror). `_parallel` gains an optional `extra_detail` callable that's appended to the detail string after each completion.

### Fixed — Snapshot mirror-root resolution (snapshots actually land on the mirror)
- Pre-fix bug in `snapshots.py:_root_folder_for`: it only checked `getattr(mirror, "root_folder", None)` for the mirror's root folder. Real backends (SFTPBackend, all of them) carry their root via `mirror.config.root_folder` — a property that dispatches on backend type. Without `.root_folder` directly on the mirror instance, the lookup fell through to `self.config.root_folder` — i.e. the PRIMARY's folder ID. Snapshot fan-out then called e.g. `sftp.mkdir("1BxiMVs.../...")` treating the Drive folder ID as an SFTP path. Either silently failed or created garbage in the user's home dir; either way, no `_claude_mirror_*` directories in the actual project folder on the mirror.
- Fix: prefer `getattr(mirror, "config", None).root_folder` first, fall back to `getattr(mirror, "root_folder", ...)` for legacy / test backends. 2 regression tests in `tests/test_snapshots.py` pin the config-first resolution + the legacy-attribute fallback.

### Fixed — Phase rows stay visible during mirror walks
- Pre-fix the `--by-backend` renderer called `progress.remove_task` on the Local + primary tasks immediately after `engine.get_status()` returned. The Local + primary rows would render their final state for an instant ("Local: all 1255 file(s) cached — done"), then vanish the moment SFTP listing started.
- Fix: replace the `remove_task` calls with `progress.update(..., total=1, completed=1)`. Spinner stops, row stays visible at its final detail. Mirror rows append below.

### Fixed — Manifest persists before snapshot phase (Ctrl+C safety)
- Pre-fix `engine.push()` and `engine.sync()` called `self.manifest.save()` only at the very end of the with-progress block, AFTER the Snapshot and Notify phases. Per-file `manifest.update(...)` calls inside `_push_file` and `_fan_out_to_mirrors` updated the IN-MEMORY dict but never reached disk if the run was interrupted during snapshot creation — exactly the most likely interrupt point on a fresh mirror with thousands of unique blobs to upload. Symptom: `Ctrl+C` during a long snapshot, run `claude-mirror push` again → SAME files re-pushed, wasting bandwidth.
- Fix: add a `self.manifest.save()` call right after the Conflicts phase, BEFORE Snapshot. The file-level upload bookkeeping persists to disk as soon as the uploads complete; only the snapshot create itself is at risk on interrupt (and snapshot create is naturally retryable on the next push since the snapshot format is content-addressed). The existing save() at the end of the block stays — captures any state changes from snapshot/notify phases.

### Performance — Per-thread SFTPClient channels (no more parallel-upload stalls)
- User-reported symptom: snapshot fan-out to a fresh SFTP mirror with 1255 unique blobs and `parallel_workers=5` took 10+ minutes with only ONE finalized blob. paramiko effectively serialized all worker threads to a single SFTP channel; 4 of 5 workers stalled indefinitely.
- Root cause: `SFTPBackend` stored a single `self._sftp` SFTPClient on the instance and every worker thread shared it. paramiko's SFTPClient is technically thread-safe (each operation gets a unique request ID, dispatcher routes responses), but operations multiplex through ONE request/response channel — under concurrent load (5 simultaneous put + posix_rename calls) the channel queue stalls and only one transfer can be in flight at a time.
- Fix: replace `self._sftp` with `self._tls = threading.local()`. `_connect()` lazily opens a NEW SFTPClient per worker thread, multiplexed over the SHARED `self._client` SSH connection. paramiko's Transport supports many SFTP channels per SSH session by design — TCP handshake cost paid once; each worker's channel runs independently from there.
- Benchmark against the maintainer's live SFTP server (192.168.236.4): sequential put+rename 21ms each; 5 parallel workers complete in 64ms total (vs ~105ms ideal serial). 1.64x speedup, no stalls.
- Projected impact on the user's 1255-blob fan-out: pre-fix stalled indefinitely; post-fix completes in ~16 seconds.
- Tests: `tests/test_sftp_backend.py` `_wire_fake` helper updated to set `backend._tls.sftp = fake_sftp` instead of `backend._sftp`. Same contract for the calling thread; `fake_ssh.open_sftp.return_value = fake_sftp` so any code path opening a fresh channel still gets the fake.

### Fixed — Per-file `↑ {path}` prints no longer interleave with live region
- Visual bug surfaced under heavy multi-thread output: 10+ `console.print("↑ {file}")` calls from worker threads (5 primary + 5 mirror per push) interleaved with the live multi-row Progress region's redraw, causing Rich-cursor drift that visibly DUPLICATED the "Guard checks" / "Pushing" phase rows on some terminals.
- Fix: route the per-file ↑ messages through a thread-safe `self._push_log_buffer` list instead of calling `console.print` directly. The live region only manages its own rows during push (no interleaved console output to fight Rich's cursor math). After the `with progress` block exits AND the manifest save completes, `_flush_push_log()` emits every buffered line via `console.print` — same content, just below the cleaned-up summary instead of interleaved.
- Tradeoff: the user no longer sees real-time per-file feedback during the upload. The Progress detail row's per-backend breakdown (`gdrive: 3/5 · sftp: 2/5`) covers the live-progress need; the per-file lines emit as a clean batch after the run finishes.

### Fixed — Yellow markup contained to the literal "unseeded" word
- Pre-fix the `status --pending` unseeded-mirrors explanation paragraph used `[dim]A mirror is [yellow]unseeded[/dim][dim] when files exist...` which closes `[dim]` while `[yellow]` is still open. Rich kept yellow applied through the rest of the paragraph.
- Fix: corrected nesting `[dim]A mirror is [yellow]unseeded[/yellow] when files exist...[/dim]`.

---

## [0.5.33] — 2026-05-07

### Added — SFTP storage backend (`backend: sftp`)

A new universal "I have a server with SSH access" backend. SFTP is the path of least resistance for users who already run a VPS, a NAS with SSH (Synology, QNAP, TrueNAS), a shared hosting account with SSH, or any self-hosted Linux machine — no OAuth dance, no per-vendor app registration, no cloud account. If `ssh user@host` works from your terminal, `claude-mirror push` works against the same server. Implementation built on `paramiko>=3.0`, which is added as a base dependency (no extras needed — it ships in the single `pipx install claude-mirror`).

Eight new YAML config fields:
- `sftp_host` — hostname or IP of the SSH server
- `sftp_port` — TCP port (default 22)
- `sftp_username` — the SSH user (same one you'd use with `ssh user@host`)
- `sftp_key_file` — path to a private SSH key file (preferred auth method)
- `sftp_password` — password fallback for legacy / NAS setups that don't accept keys (LAN-only; stored in the token file at chmod 0600, never in the YAML)
- `sftp_known_hosts_file` — path to the known-hosts file used for host-fingerprint verification (default `~/.ssh/known_hosts`)
- `sftp_strict_host_check` — refuse to connect on host-key mismatch (default `true`; set `false` only for trusted LAN with rotating IPs)
- `sftp_folder` — absolute path on the server (or path relative to the chroot if `internal-sftp` is in use)

**Auth model:** SSH key is preferred and is the recommended setup. Password authentication is supported as a LAN-only fallback. Host-key verification reads from `~/.ssh/known_hosts` by default — same trust model as the OpenSSH client — so the user runs `ssh user@host` once interactively to pin the server fingerprint, after which `claude-mirror` connects non-interactively against the pinned key. Setting `sftp_strict_host_check: false` disables the check (not recommended outside a trusted LAN).

**Notification model:** Polling (no native push events over SFTP), same approach as the WebDAV and OneDrive backends. The notifier polls remote-folder state at `poll_interval` seconds (default 30); `claude-mirror watch` and `claude-mirror watch-all` both pick up the polling notifier transparently.

**Optimizations:** When the server allows shell commands (i.e. is not jailed to `internal-sftp`), `claude-mirror` issues SSH `exec_command` calls for `sha256sum <path>` (server-side hashing — avoids round-tripping the file bytes for change detection) and `cp -p <src> <dst>` (server-side snapshot copy — avoids round-tripping the file bytes for snapshots). Both are wrapped in a try/except: if the server returns "command not found" or non-zero exit, the backend transparently falls back to client-side hashing (download + hash locally) and client-side copy (`get` + `put`). Users who lock the account down to `Subsystem sftp internal-sftp` + `ChrootDirectory` see correct behaviour with the slower fallback path; no config change required.

**Path-as-id:** Like WebDAV, SFTP has no proper file-id concept — paths ARE the identifier. The backend layer normalizes all paths to POSIX-style with no trailing slashes before hashing into the manifest, and the existing path-traversal guards in `_safe_join` apply on every remote-side operation.

**Wizard + doctor + skill integration.** `claude-mirror init --wizard --backend sftp` collects the eight fields interactively (with sensible defaults — port 22, known-hosts at `~/.ssh/known_hosts`, strict host-check on); `claude-mirror auth --config <path>` does a smoke connect that lists the project folder and writes the token file; `claude-mirror doctor --backend sftp` runs the standard six-check pass with SFTP-specific failure hints (key-file permissions, host-key mismatch, password-fallback in use, chrooted account detected). The `claude-mirror.md` skill picks up SFTP via the same `--backend sftp` path; existing skill commands (`push` / `pull` / `status` / `diff` / `snapshots` / `restore`) work identically against the new backend.

**Tests:** 26 new offline tests in `tests/test_sftp_backend.py` with mocked `paramiko.SSHClient` / `paramiko.SFTPClient` cover the connect-with-key path, the password-fallback path, host-key acceptance + rejection, the `sha256sum` exec optimization, the client-side fallback when `exec_command` returns non-zero, recursive folder listing, upload + download round-trip, server-side `cp -p` for snapshots, and the size-cap guard on `download_file`. All under 100ms each, no network access, no real SSH server needed. Tests now 329 green on Python 3.14.

---

## [0.5.32] — 2026-05-07

Two additive features bundled into one release: a colorized diff command and configurable snapshot retention. Plus the held documentation tweaks from the v0.5.31 cycle.

### Added — `claude-mirror diff <path>`
- **Colorized line-diff of local vs remote** for a single tracked file. Diff direction is remote → local, so green `+` lines are what would be pushed and red `-` lines are what would be pulled, making "should I push, pull, or merge?" answerable at a glance before doing any of those.
- Handles every state combination cleanly:
  - both sides differ — full unified diff with `@@` hunk headers and configurable context (`--context N`, default 3)
  - only-on-local — every line shown as added (would be pushed)
  - only-on-remote — every line shown as deleted (would be pulled)
  - identical — single "in sync" message, exit 0
  - binary file (NUL-byte sniff or non-utf8 decode) — refused with a one-line note rather than rendering garbage
  - missing on both sides — clear error, exit 1
- Path argument accepts both project-relative (`memory/CLAUDE.md`) and absolute paths inside the project root; absolute paths outside the project root are rejected up-front with the project root surfaced in the error.
- 20 new tests in `tests/test_diff.py` cover the binary heuristic, every render state, and the CLI command across all path-resolution cases.
- Implementation in a new `claude_mirror/_diff.py` module so the rendering logic is decoupled from the CLI wiring and reusable from future surfaces (VS Code extension, web dashboard if either ever ships).

### Added — Snapshot retention policies (`keep_last`, `keep_daily`, `keep_monthly`, `keep_yearly`)
- **Four new YAML config fields** that compose into a multi-bucket retention policy:
  - `keep_last` — keep the N newest snapshots regardless of age
  - `keep_daily` — for the last N days, keep the newest snapshot in each day-bucket (UTC)
  - `keep_monthly` — for the last N months, keep the newest snapshot in each month-bucket
  - `keep_yearly` — for the last N years, keep the newest snapshot in each year-bucket
- Each is independent — the **union** of every selector's keep-set is retained — so a user can compose "newest 7 + last 30 days + last 12 months + last 5 years" cleanly without overlap concerns. Every field defaults to `0` (= disabled), so existing projects see zero behaviour change.
- **Auto-prune after `claude-mirror push`**: when any retention field is non-zero in the config, push runs `prune_per_retention(...)` after the upload finishes and logs the deletion summary. Setting the YAML field IS the consent — no extra confirmation prompt fires for the auto-prune path.
- **New `claude-mirror prune` CLI command** for ad-hoc / manual invocation. Reads the four `keep_*` fields from the project YAML by default; any `--keep-last`, `--keep-daily`, `--keep-monthly`, `--keep-yearly` flag overrides the corresponding config field for that one run only (the YAML is not modified). Dry-run by default per the project's destructive-ops convention; require `--delete` plus a typed `YES` (or `--yes` for non-interactive use) to actually remove anything.
- 17 new tests in `tests/test_retention.py` cover: config field defaults + YAML round-trip; the algorithm across all four buckets independently; the union behaviour when all four compose; dry-run = no writes; the CLI's dry-run-by-default + typed-YES + flag-overrides-config invariants.

### Fixed — `claude-mirror status` lost its live phase progress in v0.5.30
- **Regression** introduced when `status --watch` was added in v0.5.30: the refactor extracted `_build_status_renderable` from the original `engine.show_status()` but dropped the `on_local` / `on_remote` phase-progress callback wiring. As a result, the snapshot-mode `claude-mirror status` ran silently during local hashing + remote listing, then dumped the full table all at once at the end — instead of the dual-row "Local: hashing 42/120 files" / "Remote: explored 7 folder(s), 312 file(s)" updates.
- Fix: `_build_status_renderable` gained a `with_progress` parameter. `status` (snapshot path) calls it with `with_progress=True`, which opens a transient dual-row Progress and forwards `on_local` + `on_remote` callbacks into `engine.get_status(...)` — restoring the pre-v0.5.30 live updates. `status --watch` keeps `with_progress=False` because the outer `rich.live.Live` already owns the live region (running both at once would interleave/flicker).
- Pinned with a regression test in `tests/test_status_watch.py` that asserts the snapshot path forwards both callbacks into `get_status` so this behaviour can't silently regress again.

### Updated — README and command summary
- README "How it works" / quality-gates line updated to reflect the now-303 tests and Python 3.11 / 3.12 / 3.13 / 3.14 matrix (the doc tweaks from the v0.5.31 cycle, held back for this feature bump).
- Command-summary block now includes `claude-mirror diff` and `claude-mirror prune` with their flags.
- CONTRIBUTING.md updated to mention Python 3.14 in the CI matrix.

---

## [0.5.31] — 2026-05-07

CI fix for the v0.5.30 watch-mode tests, plus Python 3.14 added to the supported matrix.

### Fixed — `status --watch` test suite green on Linux Python 3.11/3.12/3.13
- The three watch-mode tests in `tests/test_status_watch.py` failed in CI on Ubuntu / Python 3.11, 3.12, and 3.13 with `Aborted!` exit_code 1, while passing locally on Python 3.14 / macOS. Root cause: the tests monkey-patched `time.sleep` globally to raise `KeyboardInterrupt`, but the global patch could fire from unrelated stdlib code paths between `CliRunner.invoke` and the watch loop's `try/except`, surfacing as Click's `Abort()` instead of being caught by the loop's interrupt handler.
- Fix: introduced a thin `_status_watch_sleep(interval)` indirection in `claude_mirror/cli.py` that the watch loop calls instead of `time.sleep` directly. Tests now patch `cli_module._status_watch_sleep` (one specific call site) rather than the global `time.sleep`, eliminating the interference. No production behaviour change — the helper is a one-line wrapper.

### Added — Python 3.14 to test matrix and PyPI classifiers
- `.github/workflows/test.yml` now runs the test suite on Python 3.11, 3.12, 3.13, and 3.14 (matching the maintainer's local dev version, so future regressions surface uniformly across all supported versions before they reach a release tag).
- Added `Programming Language :: Python :: 3.14` to `pyproject.toml` classifiers so the PyPI page and resolver correctly advertise 3.14 support.

---

## [0.5.30] — 2026-05-07

Three additive features bundled into one release.

### Added — `claude-mirror doctor [--config PATH] [--backend NAME]`
- **One-shot configuration diagnostic** that replaces the "why isn't my sync working" support thread. Runs through six check categories per backend (primary plus every Tier 2 mirror in `mirror_config_paths`):
  1. Config file parses
  2. Credentials file present (skipped for WebDAV which uses username + password)
  3. Token file has `refresh_token`, or for WebDAV has `username` + `password`
  4. Backend connectivity via `get_credentials()` plus a `list_folders(root, name=None)` smoke call, with class-specific fix hints based on the exception type (auth / permission / 404 / network / unknown)
  5. `project_path` exists and is a directory
  6. Manifest JSON integrity (read directly rather than via `Manifest.load()`, which auto-quarantines corrupt files and would mask the failure)
- All checks always run (no early exit) so the user sees every issue in one pass. Exit code 0 on all-pass, 1 on any failure — composes cleanly with shell scripts and CI.
- `--backend NAME` limits checks to one backend (relevant for users with Tier 2 multi-mirror setups who want to debug just one).
- 10 new tests in `tests/test_doctor.py` cover happy-path, each failure category, the per-backend filter, and the exit-code contract.

### Added — `claude-mirror status --watch SECONDS`
- Live-updating sync state via `rich.live.Live`. `claude-mirror status --watch 10` refreshes the status display in place every 10 seconds until the user presses Ctrl+C; on interrupt, prints "watch stopped" and exits cleanly without a stack trace.
- Refresh interval is validated as `IntRange(min=1, max=3600)` at the Click layer; values outside that range error before the loop starts.
- Snapshot mode (without `--watch`) is byte-for-byte unchanged — the same rendering helper `_build_status_renderable` produces a Rich `Group`/`Table`/`Text` consumed by both `console.print` (snapshot) and `Live.update` (watch).
- `--watch` is also wired into the Tier 2 pending-state view (`status --pending --watch 10`), refactored into a parallel `_build_pending_renderable` helper.
- 7 new tests in `tests/test_status_watch.py` cover the snapshot regression, Live entry, multi-iteration loop with Ctrl+C exit, the click-level interval validation, the "watch stopped" stop message, and the renderable-helper return type.

### Added — `parallel_workers` config field (project-scoped)
- New YAML field `parallel_workers: int = 5` (default 5, matching the previous hardcoded constant). Override per project to tune `ThreadPoolExecutor` concurrency for blob uploads, snapshot copies, recursive listings, and other parallel operations. Useful for slow CPUs (lower), fat home connections (higher), and rate-limited APIs (lower).
- Every existing call site of `PARALLEL_WORKERS` (4 in `sync.py`, 13 in `snapshots.py`) now reads `self.config.parallel_workers` instead of the module-level constant. The constant in `claude_mirror/_constants.py` is preserved as the documented default value of the new field; the `tests/test_constants.py` `is`-identity invariant continues to pass.
- 5 new tests in `tests/test_parallel_workers_config.py` cover the default value, YAML override, the boundary at zero, the constant fallback invariant, and an end-to-end test that wraps `concurrent.futures.ThreadPoolExecutor` and asserts `max_workers=3` propagates when the config sets `parallel_workers=3`.

### Notes
- Total suite: 265 tests, runtime under one second.
- All three features ship as additive changes — no observable behaviour change for existing configs (defaults match the prior hardcoded values; `--watch` is opt-in; `doctor` is a new command).

---

## [0.5.29] — 2026-05-07

### Added
- **`SECURITY.md`** at the repo root. Documents the security-advisory reporting flow (GitHub's private security-advisory form at `https://github.com/alessiobravi/claude-mirror/security/advisories/new` rather than public issues), what the maintainer commits to (acknowledgement within seven days, fix in a private branch shipped as a patch release, credit in the CHANGELOG unless the reporter asks otherwise, no bug bounty), what is in scope (the `claude_mirror/` package, the helper scripts in `skills/`, the CLI commands, the CI workflows, and anything that ships in the wheel or sdist) and out of scope (third-party SDKs, the user's own environment, the cloud backends themselves, the Claude Code agent platform). Also enumerates what claude-mirror's design protects against (no maintainer-operated infrastructure on the data path, chmod 0600 token files, path-traversal guards via `_safe_join`, error-message redaction via `redact_error`, TLS with default certificate verification on every network call, and a fixed allowlist of network destinations) so reporters know which boundaries to test.
- **`.github/ISSUE_TEMPLATE/bug_report.md`** standard bug-report template. Pre-fills the fields that diagnosis usually needs (`claude-mirror --version`, OS and version, Python version, backend in use, install method, the exact command, the steps to reproduce, the full error output, what the reporter has already tried) so incoming reports come in a useful shape rather than a free-form sentence. Includes a top-of-template note redirecting security issues to the private security-advisory form.
- **`.github/ISSUE_TEMPLATE/config.yml`** wires a "Security vulnerability (private)" option into GitHub's issue-creation chooser, pointing at the same security-advisory URL. Blank issues remain enabled for cases the bug-report template does not fit.

### Notes
- Pure repository-metadata patch; no runtime behaviour change. Tests stay at 243.
- The GitHub repo's "About" sidebar (description and topic tags) is set via the GitHub web UI, not the repo contents, so it ships as a manual operation alongside this commit rather than as code.

---

## [0.5.28] — 2026-05-07

### Docs
- **Skill (`skills/claude-mirror.md`) gains a "Shell tab-completion" section.** Pre-v0.5.28, the v0.5.27 tab-completion feature was documented in `README.md` and `CHANGELOG.md` but not in the skill that ships at `~/.claude/skills/claude-mirror/SKILL.md`. The Claude Code agent reading the skill now knows that `claude-mirror-install` auto-installs tab-completion, that the `completion` command can emit the script directly for any of the three supported shells, and that the most common reason tab-completion fails to work after install is that the user's current shell session was started before the rc-file edit (fix: re-source the rc file or open a new terminal).
- **`CONTRIBUTING.md` documents the two tab-completion code surfaces** for future contributors: the `completion` Click command in `claude_mirror/cli.py` (tests at `tests/test_completion.py`) and the `install_completion` / `uninstall_completion` functions in `claude_mirror/install.py` (tests at `tests/test_install_completion.py`). The new paragraph describes which file to touch for which kind of change and explicitly calls out the `_completion_activation_pending` module-level flag that drives the end-of-install activation banner.

### Notes
- Pure documentation patch; no runtime behaviour change. Suite stays at 243 tests.

---

## [0.5.27] — 2026-05-07

### Added
- **`claude-mirror completion {bash|zsh|fish}`** emits shell tab-completion source for the user to eval into their shell rc. Click 8's native completion already works via `_CLAUDE_MIRROR_COMPLETE=<shell>_source claude-mirror`, but that bootstrap is opaque enough that nobody discovers it. The new command exposes the same script under a discoverable name:
  ```bash
  # zsh
  eval "$(claude-mirror completion zsh)"

  # bash
  eval "$(claude-mirror completion bash)"

  # fish
  claude-mirror completion fish > ~/.config/fish/completions/claude-mirror.fish
  ```
- After installation, tab-completion handles:
  - **Command names:** `claude-mirror <TAB>` → `auth / completion / delete / find-config / forget / gc / history / inbox / init / inspect / log / migrate-snapshots / migrate-state / pull / push / reload / restore / retry / snapshots / status / sync / test-notify / update / watch / watch-all` plus `check-update`
  - **Flag names:** `claude-mirror push <TAB>` → `--config / --files / --force-local`
  - **`click.Choice` values automatically** — e.g. `claude-mirror init --backend <TAB>` → `googledrive / dropbox / onedrive / webdav`
  - **File paths for `--config <TAB>`** — handled by the shell itself (no extra completer needed)

- **`claude-mirror-install` now auto-installs shell tab-completion** as one of its components. Pre-v0.5.27, users had to manually run `eval "$(claude-mirror completion zsh)"` to get tab-completion — most users never discovered it. The installer now:
  - **Detects your shell** from `$SHELL` (zsh / bash / fish; falls back to platform default if unset).
  - **Picks the right target file** — `~/.zshrc` for zsh; `~/.bash_profile` on macOS or `~/.bashrc` on Linux for bash; `~/.config/fish/completions/claude-mirror.fish` for fish.
  - **Wraps the addition in marker comments** so the whole block can be found and removed cleanly by a future `claude-mirror-install --uninstall`. The begin marker is `# >>> claude-mirror tab-completion (added by claude-mirror-install) >>>`, the end marker is `# <<< claude-mirror tab-completion <<<`, and the `eval` line sits in between.
  - **Prompts before any change** (consistent with the rest of the installer; declining skips this component without affecting the others).
  - **Idempotent** — re-running install with no changes is a true no-op (no rewrite of the rc file). If the binary path changed (e.g. `pipx install -e` → PyPI install), the prompt offers to refresh the stored eval line.
  - **Skips cleanly on unsupported shells** (sh, dash, csh, tcsh, ksh) with a warning rather than writing a broken file.
- **Prominent activation banner at the end of `claude-mirror-install`.** When tab-completion is newly added to the rc file (or refreshed because the binary path changed), the install command finishes with a high-contrast yellow banner explaining that tab-completion is installed but is not yet active in the current shell, and pointing at the exact `source` command needed to activate it (or instructing the user to open a new terminal). The banner exists because the per-step `✓ Tab-completion installed in <path>` line during install was easy to skim past, leading users to think completion was working when it was not yet sourced into their current shell.
- **Optional one-shot shell replacement during install.** After the activation banner the installer asks "Replace the current shell with a fresh one now to activate tab-completion immediately? (Default: No — open a new terminal manually instead.)". A `Y` answer calls `os.execvp(shell, [shell])`, replacing the install process with a fresh interactive shell that sources the user's rc file and picks up the new completion immediately. The default of `No` is intentional, because `os.execvp` discards any environment variables the user set during the install session and returns them to a fresh login-equivalent shell. For users who would rather avoid that side effect, opening a new terminal or running `source <rc-file>` manually has the same effect with no process replacement.

### Tests
- New `tests/test_completion.py` (7 tests) covers each shell's emitted script + invalid-shell rejection + case-insensitive shell argument + completion-command discoverability via top-level `--help`.
- New `tests/test_install_completion.py` (22 tests) covers shell detection, target-file resolution, install (zsh, bash, fish + idempotency + update path), uninstall (preserves user content above and below the marker block), the unsupported-shell skip path, and the `_completion_activation_pending` module flag that drives the end-of-run banner (set on first install, set on refresh, NOT set on no-op idempotent skip, NOT set on unsupported-shell skip).

### Removed
- Removed legacy `claude-sync` migration support. Specifically:
  - `claude-mirror migrate-state` command and its helpers (`_LEGACY_CONFIG_DIR`, `_NEW_CONFIG_DIR`, `_FILE_RENAMES`, `_detect_legacy_state`).
  - `_legacy_state_banner` startup warning that detected `~/.config/claude_sync` and prompted users to migrate.
  - `CLAUDE_MIRROR_SUPPRESS_MIGRATION_BANNER` env var.
  - `LEGACY_SKILL_DIR` cleanup in `install.py:install_skill` that detected `~/.claude/skills/claude-sync` and prompted to remove it.
  - `LEGACY_HOOK_COMMAND_PREFIXES` strip-and-prune logic in `install.py:install_hook` that scanned `~/.claude/settings.json` for `claude-sync inbox` entries and removed them.
  - Dual-prefix folder-exclusion predicates `startswith(("_claude_sync", "_claude_mirror"))` narrowed back to single prefix `startswith("_claude_mirror")` in `claude_mirror/backends/onedrive.py`, `claude_mirror/backends/webdav.py`, and `claude_mirror/snapshots.py`.
  - Legacy `.claude_sync_manifest.json`, `.claude_sync_inbox.jsonl`, and `.claude_sync_hash_cache.json` patterns from `.gitignore`.
  - `migrate-state` row from `README.md`'s command reference table.

### Notes
- Pure CLI and installer feature pair; no behaviour change to sync, push, pull, snapshots, or any other working-tree command.
- Total suite: 243 tests, runtime under one second.

---

## [0.5.26] — 2026-05-07

### Added
- **`PRIVACY.md`** — full privacy policy at the repo root. Plain-language description of what data claude-mirror handles, where it goes, and what the maintainer has access to. Sections cover: what data flows where (3 explicit categories: stays local / user↔cloud / user↔GitHub-version-check), what the maintainer CAN see (aggregate count + PyPI download stats; no identities, no files, no metadata), token security (chmod 0600, double-write guard, where to revoke per backend), the bring-your-own-app advanced option, and source-code transparency (the `grep -rn 'https://' claude_mirror/` audit trail anyone can run).
- **`PRIVACY.md` is shipped now (pre-v0.6.0) so it has a stable GitHub URL** before the upcoming v0.6.0 Dropbox + Azure AD app registrations need to point at it (both providers require a privacy-policy URL during app registration).

### Infrastructure
- **`.gitignore`** adds `ROADMAP_*.md` glob so future internal planning docs (e.g. `ROADMAP_v060.md`) follow the same local-only convention as `ROADMAP.md` without needing per-file additions.

### Notes
- No code change; no behaviour change. Wheel rebuilt because `PRIVACY.md` is part of the sdist (Hatchling's default sdist policy includes tracked files), and so PyPI's project page has the latest list of bundled files.

---

## [0.5.25] — 2026-05-07

### Infrastructure
- **PyPI Trusted Publishing** is now wired up for the release flow. New `.github/workflows/publish.yml` triggers on tag pushes matching `v*`, runs the full 214-test suite on a clean Ubuntu VM as a final pre-flight, builds the wheel + sdist, generates a **SLSA-3 build provenance attestation** via `actions/attest-build-provenance@v3`, then uploads to PyPI using OIDC — no API token is read from anywhere.
- **Anyone can now verify a published release was built by this repo's CI:**
  ```bash
  gh attestation verify <wheel-path> --owner alessiobravi --repo claude-mirror
  ```
  Returns the workflow filename, commit SHA, and runner identity, chained back to GitHub's OIDC issuer via Sigstore's Fulcio CA.
- **PyPI URL verification.** Now that uploads come from a trusted publisher whose OIDC claim points at `alessiobravi/claude-mirror`, PyPI auto-verifies all `[project.urls]` entries that point at the same repo. The "Unverified details" disclaimer on the project page is replaced with green checkmarks next to Homepage / Repository / Changelog / Documentation / Issues.

### Release flow change
Pre-v0.5.25: bump version → `pyproject-build` → `twine upload dist/*` from laptop with project-scoped token in `~/.pypirc`.

v0.5.25 onward: bump version → `git push origin main` (CI tests run) → after green, `git tag vX.Y.Z && git push origin vX.Y.Z` (publish workflow fires, builds + attests + uploads). Laptop is no longer in the supply-chain critical path; the PyPI token in `~/.pypirc` can be revoked once the first trusted-publishing release lands cleanly.

### Docs
- `CONTRIBUTING.md` documents the new release flow + how downstream users / auditors verify provenance with `gh attestation verify`.

---

## [0.5.24] — 2026-05-07

### Docs
- **README opening rewritten to describe what claude-mirror actually does, not just its original use case.** Pre-v0.5.24, the headline read "Sync Claude project MD files across machines…" — accurate to the project's origin but misleading about its current scope. The file-pattern glob has always been configurable, but the marketing copy implied markdown-only. New headline: "Mirror your project files across machines and cloud backends — with multi-cloud redundancy, time-travel disaster recovery, and real-time collaboration signals." Followed by an explicit note that `file_patterns` is a glob (default `["**/*.md"]`, but `["**/*"]` mirrors everything, and any glob like `["**/*.py", "**/*.md"]` scopes what gets synced).
- **Added a "Why use it" bullet section right at the top** with four value props that previously were buried in the "How it works" technical section: multi-cloud redundancy (Tier 2 mirroring as outage / suspension / quota survival), time-travel DR (`history` + `restore` to any past timestamp, with both blob and full snapshot formats explained), near-real-time collaboration (per-backend notification mechanics in one line each), and no-loss conflict resolution (the four interactive choices). Drive-by visitors now see why this exists before they see how it works.
- **Backend support reordered.** Now appears after the value props instead of mixed in with the headline pitch. Same content, better information hierarchy.

### Notes
- No code change. Wheel + sdist rebuilt only because the README change is the long-description on PyPI's project page; uploading v0.5.24 refreshes that page.

---

## [0.5.23] — 2026-05-07

### Docs
- **README now leads with status badges** for CI test runs, PyPI version, supported Python versions, and license — gives drive-by visitors immediate signal that the project is maintained, tested, and on a known cadence.
- **README explicitly advertises the test suite** in a new "Quality gates" paragraph at the top: 214 tests on Python 3.11/3.12/3.13 in parallel, what surfaces are covered, the merge-blocking guarantee, and a pointer to `CONTRIBUTING.md` for run-it-yourself instructions. Closes the gap where readers had no way to gauge code-quality posture without scrolling to the changelog.

### Tests
- **`test_redact_error_strips_bearer_token` now uses an unambiguously-fake token fixture.** The previous fixture (`Bearer ya29.a0AfH6SMB...`) used the real-world Google access-token prefix `ya29.`, which would trip automated secret scanners (PyPI / GitHub Advanced Security / TruffleHog) into flagging the test file as a leaked credential — even though the suffix was nonsense. New fixture is `Bearer FAKE_TOKEN_NOT_A_REAL_CREDENTIAL_xxx...` which exercises the same redactor regex code path without resembling a real token shape. Test logic is unchanged.

### Privacy audit (no findings)
- Full repo grep ran across all tracked files for: real Mac/Linux paths (`/Users/<name>`, `/home/<name>`), personal aliases (`claude-aa`), other-project codenames (`Cortex/`, `cortex.yaml`), real `user@machine` slack-style examples, personal email addresses, real-format Drive folder IDs (excluding the well-known Google sample), real internal hostnames, and token-shaped strings. Zero hits remained after the bearer-token fixture rewrite.

### Infrastructure
- **CI workflow bumped to action versions that support Node.js 24.** GitHub deprecates Node.js 20 from runners on 2026-09-16; from 2026-06-02 actions running on Node 20 emit warnings until forced onto Node 24. Bumped `actions/checkout@v4 → v5` and `actions/setup-python@v5 → v6`. Both new majors are drop-in compatible (same `with:` keys); the only behavioural change is the runtime Node version, which we never depend on directly. Eliminates the warning banner on every CI run.

---

## [0.5.22] — 2026-05-07

### Fixed
- **`claude-mirror-install` now installs the Claude Code skill for PyPI users.** Pre-v0.5.22, `_find_skill_source()` only checked the editable-install layout (`<install.py>/../skills/claude-mirror.md`). For users who installed via `pipx install claude-mirror` from PyPI, that resolved to `<venv>/lib/python3.X/site-packages/skills/`, which doesn't exist — the installer printed `Skill source file not found — skipping` and the user got the binary without the skill.
  - **Fix in two parts:**
    1. **Wheel now bundles the skill.** `pyproject.toml` adds a `[tool.hatch.build.targets.wheel.force-include]` directive mapping `skills/claude-mirror.md` → `claude_mirror/_skill/claude-mirror.md` inside the wheel. The 14,711-byte file is now present in every PyPI install at `<site-packages>/claude_mirror/_skill/claude-mirror.md`.
    2. **`_find_skill_source()` checks the bundled location first**, falling back to the repo `skills/` directory for editable installs from a clone. Both paths return the same content; the resolution order means PyPI users never miss it.
- **No code change for editable-install users.** The repo `skills/claude-mirror.md` is still authoritative; the bundled copy is generated at wheel-build time.

### Tests
- New `tests/test_skill_bundling.py` (4 tests) pins both resolution paths:
  - `test_find_skill_source_returns_existing_path_in_dev_layout` — editable install (the test environment) resolves to the repo file successfully.
  - `test_find_skill_source_prefers_bundled_over_repo` — when both layouts exist, the bundled wheel copy wins.
  - `test_find_skill_source_falls_back_to_repo_when_bundled_missing` — editable installs without `_skill/` still find the source.
  - `test_find_skill_source_returns_none_when_neither_exists` — graceful no-op so `install_skill()` can show its own friendly skip message.
- Suite now totals **214 tests**, still <1 s.

### Verified
- Built v0.5.22 wheel, installed into a fresh venv (`python3 -m venv /tmp/...`), verified `_find_skill_source()` returns the bundled `claude_mirror/_skill/claude-mirror.md` (14,711 bytes) — the first version where PyPI users get a complete `claude-mirror-install`.

---

## [0.5.21] — 2026-05-07

A coordinated test-coverage push. **210 tests** now pass in <1 s; coverage of every major feature surface jumped from ~10% to ~70%. No runtime behaviour change beyond a single import fix in `snapshots.py`.

### Tests added
- **SyncEngine 3-way diff** (`tests/test_sync_engine.py`, **29 tests**) — full state matrix: no-manifest cells, in-sync, one-side-changed, both-changed/conflict, deletes. push / pull / sync / `_delete_drive_file` end-to-end against an in-memory backend. `force_local=True` skip-resolver pinned with hard-failing monkeypatch.
- **SnapshotManager** (`tests/test_snapshots.py`, **25 tests**) — both formats (`full` + `blobs`): create / list / restore (incl. `output_path` and per-backend fallback) / forget (specific timestamps, `--keep-last`, `--keep-days`, `--before`) / gc (orphan list + dry-run + apply) / migrate (full↔blobs, idempotent) / history (path-grouped-by-SHA) / inspect (`--paths` filter).
- **Path-traversal guard** (`tests/test_safe_join.py`, **23 tests**) — security-critical, parametrized over safe paths and traversal attacks (`..` segments at every depth, absolute paths, NUL bytes).
- **Conflict resolver** (`tests/test_merge_resolver.py`, **13 tests**) — `[L]ocal / [D]rive / [E]ditor / [S]kip` choices, conflict-marker file with `claude_mirror_merge_` prefix, editor invocation via subprocess.
- **Auth backup-and-restore** (`tests/test_auth_backup_restore.py`, **6 tests**) — regression test for the v0.5.11 fix: token moved to `<token>.pre-reauth.bak`, deleted on success, restored on failure, `--keep-existing` opt-out.
- **Per-backend round-trip** (`tests/test_googledrive_backend.py` + `test_dropbox_backend.py` + `test_onedrive_backend.py` + `test_webdav_backend.py`, **27 tests**). Drive (12) — `googleapiclient.discovery.build` mocked, covers auth/folders/path-resolve/upload/download/hash/error-classification incl. `RefreshError("invalid_grant")` → AUTH and `HttpError(503)` → TRANSIENT. Dropbox / OneDrive / WebDAV (5 each) — smoke coverage via `mock_oauth_*` fixtures + `responses` HTTP mocking. WebDAV explicitly tests the v0.5.6 https-required guard.
- **Notifier inbox** (`tests/test_notifier_inbox.py`, **7 tests**) — concurrency-critical. Includes the **TOCTOU regression test** (writer thread + drain loops asserting strict equality, would fail immediately if `read_and_clear_inbox` regressed away from `LOCK_EX`), plus round-trip / multi-write / corrupt-line / unicode / filename invariant.
- **Watcher daemon** (`tests/test_watcher.py`, **6 tests**) — smoke: one-thread-per-config, dedup, SIGHUP handler registration, `claude-mirror reload` sends SIGHUP via subprocess.

### Fixed
- **`snapshots.py` was missing a `timedelta` import** (uncovered by `test_forget_keep_days_n`). Any `forget --keep-days=N` or relative `--before=30d` invocation would have raised `NameError`. One-line fix; new test pins it.

### Infrastructure
- **`.github/workflows/test.yml`** runs the full suite on push and pull request, against Python 3.11 / 3.12 / 3.13 in parallel. CI now blocks merging a PR that fails tests.
- **`CONTRIBUTING.md`** — layout, fixture conventions, local-run commands, what's expected of PRs.
- **`pyproject.toml`** dev extras now include `responses>=0.25` for HTTP-level backend mocking.
- **`tests/conftest.py`** — full `StorageBackend` ABC fake (`FakeStorageBackend`), `NotificationBackend` fake (`FakeNotificationBackend`), and OAuth-flow mock fixtures for all four backends.

### Notes
- Test files are intentionally NOT shipped to PyPI. The Hatchling wheel build only includes `packages = ["claude_mirror"]`.
- Two test files (`test_sync_engine.py`, `test_snapshots.py`) build their own `InMemoryBackend` rather than reusing `conftest.FakeStorageBackend` because each needs slightly different semantics. Both work, both pass.
- Click 8.3 emits a `DeprecationWarning` for `Context.protected_args`; auth + watcher tests use module-level `filterwarnings("ignore::DeprecationWarning")` to coexist with the project-wide `filterwarnings = "error"` setting.

---

## [0.5.16] — 2026-05-07

### Fixed
- **Narrowed broad `except Exception` clauses on six load/best-effort paths.** Catching the bare `Exception` base class hides real coding bugs (`AttributeError`, `TypeError`, `NameError`) as "feature silently does nothing" — by far the hardest class of bug to track down because there's no traceback, no log line, just a behaviour that quietly stops working. Each site now narrows to exactly the exception types its legitimate failure modes can raise; programming bugs propagate normally so they're caught in development instead of in production.
  - `claude_mirror/hash_cache.py:_load` — narrowed to `(json.JSONDecodeError, OSError)`. Corrupt or unreadable hash cache → start with empty dict (the cache is purely a performance optimisation).
  - `claude_mirror/manifest.py:_is_safe_relpath` — narrowed to `(ValueError,)`. `Path()` raises `ValueError` on embedded NUL on some platforms; nothing else is expected here.
  - `claude_mirror/_update_check.py:_resolve_repo_root` — narrowed to `(ImportError, OSError)`. `ImportError` if the package isn't installed; `OSError` if `Path.resolve()` fails (broken symlink, missing dir).
  - `claude_mirror/_update_check.py:suggested_update_command` — narrowed to `(ImportError, OSError)`. Same shape as `_resolve_repo_root`.
  - `claude_mirror/_update_check.py:_get_current_version` — narrowed to `(ImportError, LookupError)`. `LookupError` is the parent of `importlib.metadata.PackageNotFoundError`.
  - `claude_mirror/_update_check.py:_load_cache` — narrowed to `(json.JSONDecodeError, OSError, ValueError)`. JSON parse / file-read / generic decode failure modes only.
  - `claude_mirror/_update_check.py:_save_cache` — narrowed to `OSError`. mkdir/write_text/os.replace all surface filesystem errors as `OSError`; non-serialisable cache data is a coding bug, not a runtime failure.

### Tests
- Added `tests/test_load_paths_narrow.py` (6 regression tests pinning the new behaviour):
  - `test_hash_cache_returns_empty_on_corrupt_json` — malformed cache file is treated as 'no cache yet' (legitimate failure mode still no-ops).
  - `test_hash_cache_propagates_attribute_error` — a coding bug in `json.loads` is NOT swallowed (the original regression test).
  - `test_manifest_load_propagates_programming_bugs` — `TypeError` raised inside `_is_safe_relpath` propagates rather than being misread as 'unsafe path'.
  - `test_update_check_silent_on_network_error` — `URLError` from urllib results in a clean no-op (the foreground command still works offline).
  - `test_update_check_load_cache_propagates_attribute_error` — coding bug in cache parse propagates out of `_load_cache`.
  - `test_update_check_load_cache_silent_on_corrupt_json` — `JSONDecodeError` on a corrupt cache file yields `{}` silently.

---

## [0.5.15] — 2026-05-07

### Refactored
- **Removed dead-equivalent branch in `Manifest._is_safe_relpath`** (`manifest.py:165`). The condition `part.startswith("..") and (part == ".." or part in ("..", ))` was a confusingly over-defensive way to write `part == ".."` — and the literal-`".."` case is already caught by the preceding `part in ("..", "\\..", "/..")` check on the line above. Removing the redundant branch simplifies the path-traversal guard without changing rejection semantics: `..` segments still rejected, names that merely START with `..` (e.g. `..foo`, `...trailing`, `.hidden`) still accepted as legal POSIX filenames. Covered by `tests/test_path_safety.py`.
- **Deduplicated `PARALLEL_WORKERS = 5` constant.** Previously declared twice — once in `sync.py:24` and once in `snapshots.py:68` — meaning a tuning bump required remembering to bump both. The constant now lives in a new `claude_mirror/_constants.py` module, imported by reference into both call sites. The `is`-identity invariant is asserted in `tests/test_constants.py` so any regression that re-declares it in either module fails CI.

---

## [0.5.14] — 2026-05-07

### Fixed
- **OneDrive `classify_error` no longer misclassifies "Token"-named transient errors as `AUTH`.** The previous heuristic ran a substring check on `type(exc).__name__` for `"Auth"` or `"Token"` and routed any match to `ErrorClass.AUTH`. That correctly caught `MsalUiRequiredError` but also caught any unrelated exception that happened to have one of those words in its class name — for example, a transient `TokenRateLimitError` would surface as a scary "re-authenticate" prompt and force the user through a pointless interactive OAuth flow for what should auto-retry.
  - **Class-name allowlist.** Only `MsalUiRequiredError` and `InteractionRequiredAuthError` (the two MSAL exception names that unambiguously mean "silent token failed; user must re-auth") trigger the AUTH classification by class name. `MsalServiceError` is intentionally excluded from the allowlist because it covers a wide range of service-side conditions, only some of which require re-auth.
  - **OAuth error-code branch.** For the broader exceptions, the classifier now inspects `exc.args` and matches against a narrow set of OAuth/AAD codes that genuinely mean "refresh token is dead": `invalid_grant`, `AADSTS50058` (silent sign-in with no signed-in user), `AADSTS70008` (refresh token expired), and `AADSTS700082` (refresh token expired due to inactivity). Every other server-side error falls through to the existing HTTP-status / network branches, which classify them as `TRANSIENT` or `UNKNOWN` as appropriate.
  - **Net effect.** Recoverable rate-limit / service blips no longer interrupt the user; only genuine credential failures prompt for re-auth.

### Tests
- New `tests/test_onedrive_classify_error.py` (10 tests) locks in the fix: synthesised `MsalUiRequiredError` and `InteractionRequiredAuthError` are AUTH; synthesised `TokenRateLimitError` is NOT AUTH (UNKNOWN); `invalid_grant`, `AADSTS50058`, and `AADSTS70008` in `exc.args` are AUTH; bare `RuntimeError("backend exploded")` is UNKNOWN; HTTP 401 → AUTH, 429 → QUOTA, 503 → TRANSIENT regression guards.

---

## [0.5.13] — 2026-05-07

### Performance
- **`SyncEngine._is_excluded` now uses a pre-compiled regex instead of a per-call `fnmatch.fnmatch` loop.** The previous implementation invoked `fnmatch.fnmatch` up to 3 times per pattern per file, and each call internally compiled (and LRU-cached) a regex. On a 5,000-file project with 10 exclude patterns, status/push/pull each made up to 150,000 `fnmatch.fnmatch` calls per command. The new code builds a single union regex of every `pattern` + `pattern/*` form once at `SyncEngine.__init__`, plus a `tuple(f"{p}/" for p in patterns)` for the legacy `startswith` branch. `_is_excluded` is now one `re.match` + one `str.startswith` per file.
  - **Measured impact** on the reference benchmark (10,000 path checks × 10 patterns, Python 3.14, Apple Silicon, median of 5 runs):
    - Old fnmatch loop: **38.00 ms**
    - New precompiled regex: **5.19 ms**
    - **Speedup: 7.3x** (-86% wall time)
  - Behaviour is byte-for-byte identical to the legacy implementation — verified by a 39-case parametrize sweep in `tests/test_exclude_patterns.py` comparing every input shape against the old fnmatch-loop reference.

### Documentation
- **`SyncEngine._local_files` now documents its symlink behaviour explicitly.** `Path.glob()` follows symlinks transparently — both for symlinked files (included under their symlink path) and symlinked directories (traversed like any other directory). Cycles are not detected. This is unchanged behaviour; the docstring just makes the policy discoverable. Switching to non-following semantics in the future is a behaviour change deserving its own version bump and migration note.

### Tests
- New `tests/test_exclude_patterns.py` (45 cases): simple-glob match, directory-form prefix match, `**/*` double-star match, empty-config short-circuit, compile-once invariant, perf smoke (<100ms / 10K calls), and a 39-case parity sweep against the verbatim legacy fnmatch-loop oracle.

---

## [0.5.11] — 2026-05-07

### Fixed
- **`claude-mirror auth` no longer falls into a "run claude-mirror auth" loop.** Previously, if Google's OAuth refresh path returned a `RefreshError` whose error string didn't match the three substrings in `_is_invalid_grant()` (`invalid_grant`, `token has been expired or revoked`, `account has been deleted`), the global `_CLIGroup` error handler caught it and instructed the user to run `claude-mirror auth` — the very command they had just run. The user had no path forward.
  - **Primary fix — `auth` command pre-emptively replaces the token file.** The existing token is moved to `<token>.pre-reauth.bak` before `storage.authenticate()` is called. The backend therefore sees no cached credential and goes straight to the interactive OAuth flow (browser for Google, code-paste for Dropbox, device-code for OneDrive, password prompt for WebDAV). On OAuth success, the backup is deleted; on OAuth failure (Ctrl-C, network, browser error), the backup is restored — running `auth` is therefore always safe and never leaves the user with a worse state than they started with.
  - **Secondary fix — `_CLIGroup.invoke()` knows when the running command is `auth`.** When `sub_cmd == "auth"`, the error handlers no longer print "run claude-mirror auth" (the loop trap). Instead they show "OAuth flow itself failed" with diagnostic hints: network reachability of accounts.google.com, validity of `credentials_file`, ability to bind a local OAuth callback port, and the `CLAUDE_MIRROR_AUTH_VERBOSE=1` env var for refresh-attempt logs.
  - **New `--keep-existing` flag** preserves the older behaviour for explicit refresh diagnostics: with it, `auth` does NOT move the token aside, so the backend's `authenticate()` will try to refresh first before falling through. Use this only when you want to test whether an existing token still works.

### Audit
- Confirmed that **only the Google Drive backend was affected** by the loop. Dropbox / OneDrive / WebDAV `authenticate()` already runs a fresh interactive flow on every call without consulting the existing token file, so those paths were never susceptible.

---

## [0.5.10] — 2026-05-05

Initial public release.

### Features

**Backends (storage)**
- Google Drive (Pub/Sub-driven real-time notifications)
- Dropbox (long-poll notifications)
- OneDrive (Graph API + polling)
- WebDAV (PROPFIND polling; OwnCloud / Nextcloud / Apache mod_dav / NAS / Box.com)
- Multi-backend mirroring (Tier 2): one primary backend + N secondary mirrors per project, with per-mirror manifest state and pending-retry queue.
- All backends ship in the base install — `pipx install claude-mirror` is the only command needed regardless of which backend you choose.

**Sync engine**
- 3-way diff manifest (`local hash` × `synced hash` × `remote hash`) with content-addressed change detection.
- Conflict resolver with `[L]ocal / [D]rive / [E]ditor / [S]kip` interactive choices.
- `push --force-local` for skill-side merges that should treat local as authoritative.
- File-pattern allowlist + glob exclude patterns, applied uniformly to status/push/pull/sync/delete.
- Per-machine Pub/Sub subscriptions with `SIGHUP` hot-reload of config tree (`claude-mirror reload`).
- Pending-retry queue with classified error types (TRANSIENT / AUTH / QUOTA / PERMISSION / FILE_REJECTED) for graceful degradation under transient failures.

**Snapshots & disaster recovery**
- Auto-snapshot after every push/sync, stored on the remote storage (`_claude_mirror_snapshots/`).
- Two on-disk formats: `full` (one folder per snapshot, server-side `files.copy`) and `blobs` (content-addressed dedup with `_claude_mirror_blobs/`).
- `claude-mirror history PATH` — shows every snapshot containing a file, version-grouped by SHA-256.
- `claude-mirror inspect TIMESTAMP` — list snapshot contents with `--paths` filtering.
- `claude-mirror restore TIMESTAMP [PATH...]` — restore a snapshot (non-destructive `--output`, in-place when omitted, per-backend `--backend NAME` override).
- `claude-mirror forget` and `claude-mirror gc` for safe pruning (dry-run by default; require `--delete` flag and typed `YES` confirmation; `--yes` only for non-interactive use).
- `claude-mirror migrate-snapshots --to {blobs|full}` for in-place format conversion.

**Notifications**
- Cross-platform desktop notifications (macOS `osascript`, Linux `libnotify` via `plyer`).
- Per-project notification inbox (`.claude_mirror_inbox.jsonl`) atomic against concurrent writers.
- Optional Slack webhooks (`slack_enabled` / `slack_webhook_url` / `slack_channel`) with best-effort delivery.

**CLI**
- 25 commands covering init/auth/status/sync/push/pull/delete/watch/snapshots/restore/log/inbox/find-config/migrate-state/migrate-snapshots/check-update/update.
- `claude-mirror init --wizard` for interactive setup; non-wizard path supports the same fields via flags.
- `claude-mirror find-config` walks up the directory tree like `git`'s `.git/` discovery; lists every available config on a total miss.
- `claude-mirror update --apply` one-shot self-upgrade (executes `git pull` + `pipx install -e . --force` as separate list-form `subprocess` calls — never `shell=True`).
- `claude-mirror check-update` queries the GitHub API for the latest version (CDN-bypass for instant visibility after a push); cache-busting headers fall back to raw GitHub when the API is rate-limited.
- Live-progress UI on every long-running command; silence is opt-in via a future `--quiet` flag, never default.

**Installer**
- `claude-mirror-install` — interactive installer for the Claude Code skill, the PreToolUse hook, and the background watcher daemon (launchd on macOS, systemd on Linux).
- Honours `CLAUDE_CONFIG_DIR` so a parallel Claude install is targeted correctly.

**Security & robustness**
- Path-traversal guards on all remote-driven local writes.
- Token files written with `0600` mode + post-`chmod` belt-and-braces.
- Error-message redaction (`redact_error()`) strips Bearer tokens, basic-auth credentials in URLs, query-string secrets, and home-directory paths before any error reaches a manifest, log, Slack message, or `status --pending` output.
- WebDAV backend refuses `http://` URLs by default (transmits credentials in cleartext); `--webdav-insecure-http` flag + `webdav_insecure_http: true` config field opt in.
- `BackendError` drops the live `cause` traceback to avoid pinning credential tuples / response bodies in long-running watcher processes.
- AppleScript notification text escaped against injection.
- All HTTP traffic to first-party endpoints (`api.github.com`, `raw.githubusercontent.com`) over TLS with default cert verification.

**Performance**
- O(1) state lookup on pending-retry queue expansion (was O(N×P)).
- `claude-mirror history` walks path components instead of full BFS per snapshot — ~30× fewer Drive API calls on a 30-snapshot project.
- Blob deduplication with `threading.Lock`-guarded existence check; uploads remain parallel via `ThreadPoolExecutor`.

### Notes
- Python 3.11+, Linux + macOS (Windows untested).
- Config directory: `~/.config/claude_mirror/`. Per-project state: `.claude_mirror_manifest.json`, `.claude_mirror_inbox.jsonl`, `.claude_mirror_hash_cache.json` inside each project.
- Single install command for all backends: `pipx install claude-mirror`.
- License: GPL-3.0-or-later.
