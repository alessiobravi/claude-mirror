# Changelog

All notable changes to claude-mirror.

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

This release is a coordinated multi-agent test-coverage push. **210 tests** now pass in <1 s; coverage of every major feature surface jumped from ~10% to ~70%. No runtime behaviour change beyond a single import fix in `snapshots.py`.

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
