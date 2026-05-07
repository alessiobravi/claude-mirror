# Changelog

All notable changes to claude-mirror.

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
