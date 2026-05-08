# Changelog

All notable changes to claude-mirror.

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
