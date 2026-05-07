# Contributing to claude-mirror

Thanks for your interest. This is a small, single-maintainer project — so before you sink time into a substantial change, please open an issue first to confirm the change is wanted.

## Quick start

```bash
git clone https://github.com/alessiobravi/claude-mirror.git
cd claude-mirror
pipx install -e '.[dev]'    # editable install with test/dev tooling
pytest tests/ -v            # should print "N passed"
```

The dev install adds:
- `pytest` — test runner
- `pytest-mock` — mocker fixture for monkeypatching
- `responses` — HTTP-level mocking for backend tests

## Project layout

```
claude_mirror/        ← runtime code (this is what ships to PyPI)
├── backends/         ← StorageBackend implementations
├── notifications/    ← NotificationBackend implementations
└── …
tests/                ← pytest suite (NOT shipped to PyPI; see pyproject.toml)
├── conftest.py       ← shared fixtures: make_config, fake_backend, mock_oauth_*
├── test_smoke.py     ← package-level smoke tests
├── test_*.py         ← one file per feature
skills/               ← Claude Code skill source (installed via `claude-mirror-install`)
```

## Test conventions

- **No real network calls.** Every test must run offline. Use `fake_backend` (in-memory) or the `responses` library to stub HTTP at the transport layer.
- **No real cloud accounts.** Use `mock_oauth_google` / `mock_oauth_dropbox` / `mock_oauth_msal` / `mock_oauth_webdav` fixtures from `conftest.py` for auth-flow tests.
- **No `~/.config/claude_mirror/` writes.** Every test uses `tmp_path` via the `make_config` factory fixture, which builds a `Config` pointing at temp dirs.
- **Tests should be fast.** The full suite runs in well under a second today; keep that property. If you need a slow test, mark it with `@pytest.mark.slow` so it can be filtered out.
- **Warnings are errors.** `pyproject.toml` sets `filterwarnings = "error"` — a `DeprecationWarning` from upstream usually means a future-version breakage to flag. If a specific warning is genuinely unactionable, add it to the `pyproject.toml` filter list with a comment explaining why.

## Shell tab-completion code

Tab-completion has two surfaces in the codebase, with dedicated test files for each:

- **The `completion` Click command** in `claude_mirror/cli.py` emits the per-shell completion script via Click 8's `BashComplete`, `ZshComplete`, and `FishComplete` classes. Tests live in `tests/test_completion.py` and cover each shell's emitted script, the case-insensitive shell argument, the unsupported-shell rejection path, and discoverability via the top-level `--help`.
- **The `install_completion` and `uninstall_completion` functions** in `claude_mirror/install.py` handle the rc-file editing during `claude-mirror-install`. Tests live in `tests/test_install_completion.py` and cover shell detection from `$SHELL`, target-file resolution per platform, the marker-comment-wrapped install block, idempotent re-runs, the update path when the binary path changes, the uninstall path that preserves user content above and below the block, and the `_completion_activation_pending` module-level flag that drives the end-of-install activation banner.

If you are adding a new completion-emitting feature (such as a value-completer for a specific flag), update both the runtime code in `cli.py` and the corresponding tests in `test_completion.py`. If you are changing how the installer writes to rc files (such as supporting a new shell), update both `install.py` and `test_install_completion.py`. The `_completion_activation_pending` flag is module-level state; if you add a new install path that should also trigger the end-of-install banner, set the flag from that path.

## Style

- No `Co-Authored-By:` trailers in commit messages.
- No "Generated with X" footers.
- Subjects describe what changed in the source tree, not internal/process notes (`feat: …`, `fix: …`, `perf: …`, `refactor: …`, `docs: …`, `test: …`).
- Patch version (`0.5.X` → `0.5.X+1`) is bumped before every release; minor/major bumps only on explicit maintainer call.

## Running tests on a single file

```bash
pytest tests/test_load_paths_narrow.py -v       # one file
pytest tests/ -v -k "exclude"                   # name-pattern match
pytest tests/ -v -x                             # stop on first failure
pytest tests/ --collect-only                    # list tests without running
```

## CI

Every push and pull request triggers `.github/workflows/test.yml`, which runs the full suite on Python 3.11, 3.12, and 3.13 in parallel. Your PR is unmergeable until it's green.

## Release flow (maintainer)

Releases ship via **PyPI Trusted Publishing** — `.github/workflows/publish.yml` triggers on tag push, builds on a clean Ubuntu VM, generates a SLSA-3 build provenance attestation, and uploads to PyPI via OIDC (no API token anywhere). To cut a release:

```bash
# 1. Bump version in pyproject.toml + add CHANGELOG entry
git add -A
git commit -m "release: vX.Y.Z"
git push origin main
# → Triggers test workflow only. Wait for green.

# 2. Tag and push the tag (only after tests pass)
git tag vX.Y.Z
git push origin vX.Y.Z
# → Triggers publish workflow. ~90s later: PyPI live with verified URLs
#   and SLSA attestation; verify on the project page.
```

The publish workflow re-runs the full test suite as a final pre-flight before uploading — defense in depth, since PyPI uploads are immutable. Yanking is possible but never overwriting.

## Verifying a release's provenance

Anyone can verify any published release was actually built by this repo's CI:

```bash
gh attestation verify <wheel-path> \
    --owner alessiobravi --repo claude-mirror
```

Returns the workflow filename, the commit SHA, and the GitHub-hosted runner that produced the artifact, all chained back to GitHub's OIDC issuer via Sigstore's Fulcio CA.

## Submitting a change

1. Branch off `main`.
2. Make the change. **Add or update tests for it.** A code-only PR will get pushback — every behavioural change needs a regression test.
3. `pytest tests/ -v` locally — must be green.
4. Add a `CHANGELOG.md` entry under a new patch-version heading, following the existing format (Fixed / Added / Changed / Performance / Security / Refactored / Tests).
5. Bump `pyproject.toml` `version` to match the CHANGELOG heading.
6. Open a PR. CI will run automatically.

## Reporting a bug

Open an issue with:
- `claude-mirror --version`
- The exact command + flags that fail
- The full error output (the redactor strips secrets, but eyeball it before posting anyway)
- Operating system + Python version
- Which backend you're using

For security issues that should not be public: email the maintainer directly rather than filing a public issue.

## What's out of scope

- Adding a new storage backend that doesn't have a real-world user already lined up.
- Renaming public CLI commands or config field names without a deprecation cycle.
- Adding heavy dependencies for marginal feature wins (every dep is install weight + attack surface).
- Changing the on-disk file/folder names — they're contract with existing users.
