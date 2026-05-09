"""Tests for `claude-mirror clone` — one-shot bootstrap from an existing remote.

`clone` chains init -> auth -> pull behind a single command. The tests below
pin the behaviour contract:

  * Happy path (Drive): YAML + token + 3 local files end up on disk.
  * `--no-pull`: YAML + token exist; project dir is empty (the user will
    pull / push later).
  * Auth failure: backend.authenticate() raises -> non-zero exit, YAML
    rolled back (config path does not exist on disk).
  * Wizard mode: `--wizard --backend googledrive` driven by stdin reaches
    the same successful state as the flag-driven path.
  * SFTP variant: a single happy-path SFTP clone proves the per-backend
    dispatch isn't Drive-specific.

All tests run offline against FakeStorageBackend; <100ms each.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import claude_mirror.cli as cli_mod
from claude_mirror import config as config_module
from claude_mirror.cli import cli

from tests.conftest import FakeStorageBackend


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _isolate_config_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect cli.CONFIG_DIR + config.CONFIG_DIR so init never touches the
    real ~/.config/claude_mirror/ tree."""
    fake_cfg_dir = tmp_path / "fake_config_home"
    fake_cfg_dir.mkdir()
    monkeypatch.setattr(cli_mod, "CONFIG_DIR", fake_cfg_dir)
    monkeypatch.setattr(config_module, "CONFIG_DIR", fake_cfg_dir)
    return fake_cfg_dir


def _patch_backend(monkeypatch, backend: Any) -> None:
    """Force every cli helper to use the supplied backend instance.

    `_create_storage` is what `_run_auth` and `_load_engine` (via
    `_create_storage_set`) consult; we replace both so the same
    FakeStorageBackend serves the auth phase and the pull phase."""
    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: backend)
    monkeypatch.setattr(
        cli_mod, "_create_storage_set", lambda config: (backend, []),
    )
    monkeypatch.setattr(cli_mod, "_create_notifier", lambda config, storage: None)


def _seed_three_files(backend: FakeStorageBackend) -> dict[str, bytes]:
    """Place three .md files on the fake remote. Returns a {rel_path:
    content} dict so the test can later assert on local-side contents."""
    payloads = {
        "a.md": b"alpha\n",
        "b.md": b"bravo\n",
        "notes/c.md": b"charlie\n",
    }
    for rel_path, content in payloads.items():
        parent_id, basename = backend.resolve_path(rel_path, backend.root_folder_id)
        backend._store_file(content, basename, parent_id, None)
    return payloads


def test_clone_drive_happy_path_writes_config_token_and_pulls_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end clone against a fake Drive remote with 3 seeded files.

    Asserts: YAML config exists with the expected backend + folder ID,
    token file exists (auth fake wrote one), and all 3 files were pulled
    to disk with the expected bytes."""
    fake_cfg_dir = _isolate_config_dir(monkeypatch, tmp_path)

    backend = FakeStorageBackend(root_folder_id="test-drive-folder")
    payloads = _seed_three_files(backend)

    def _authenticate_writes_token() -> Any:
        Path(cfg_token).write_text('{"access_token":"fake","refresh_token":"r"}')
        return backend
    backend.authenticate = _authenticate_writes_token  # type: ignore[method-assign]
    _patch_backend(monkeypatch, backend)

    project = tmp_path / "cloned-project"
    cfg_path = tmp_path / "myproject.yaml"
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_token = str(fake_cfg_dir / "drive-cloned-project-token.json")

    result = CliRunner().invoke(
        cli,
        [
            "clone",
            "--backend", "googledrive",
            "--project", str(project),
            "--drive-folder-id", "test-drive-folder",
            "--gcp-project-id", "test-gcp-project",
            "--pubsub-topic-id", "claude-mirror-test",
            "--credentials-file", str(creds),
            "--token-file", cfg_token,
            "--config", str(cfg_path),
        ],
    )

    assert result.exit_code == 0, result.output
    assert cfg_path.exists(), "clone did not write the YAML config"
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["backend"] == "googledrive"
    assert raw["drive_folder_id"] == "test-drive-folder"
    assert raw["project_path"] == str(project.resolve())

    assert Path(cfg_token).exists(), "clone did not write the token file"

    for rel_path, content in payloads.items():
        local = project / rel_path
        assert local.exists(), f"clone did not pull {rel_path}"
        assert local.read_bytes() == content


def test_clone_no_pull_writes_config_and_token_but_leaves_project_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--no-pull` halts after auth. YAML + token exist; project has no
    files synced down even though the remote has 3 of them."""
    fake_cfg_dir = _isolate_config_dir(monkeypatch, tmp_path)

    backend = FakeStorageBackend(root_folder_id="test-drive-folder")
    _seed_three_files(backend)

    def _authenticate_writes_token() -> Any:
        Path(cfg_token).write_text('{"access_token":"fake"}')
        return backend
    backend.authenticate = _authenticate_writes_token  # type: ignore[method-assign]
    _patch_backend(monkeypatch, backend)

    project = tmp_path / "no-pull-project"
    cfg_path = tmp_path / "no-pull.yaml"
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")
    cfg_token = str(fake_cfg_dir / "drive-no-pull-project-token.json")

    result = CliRunner().invoke(
        cli,
        [
            "clone",
            "--backend", "googledrive",
            "--project", str(project),
            "--drive-folder-id", "test-drive-folder",
            "--gcp-project-id", "test-gcp-project",
            "--pubsub-topic-id", "claude-mirror-test",
            "--credentials-file", str(creds),
            "--token-file", cfg_token,
            "--config", str(cfg_path),
            "--no-pull",
        ],
    )

    assert result.exit_code == 0, result.output
    assert cfg_path.exists()
    assert Path(cfg_token).exists()

    pulled = [p for p in project.rglob("*.md")]
    assert pulled == [], (
        f"--no-pull must not pull files, but found: {pulled}"
    )


def test_clone_rolls_back_config_on_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When auth fails, the YAML written by the init phase is removed —
    a partial setup must not be left on disk for the next run to trip on."""
    _isolate_config_dir(monkeypatch, tmp_path)

    backend = FakeStorageBackend(root_folder_id="test-drive-folder")

    def _authenticate_raises() -> Any:
        raise RuntimeError("simulated oauth failure")
    backend.authenticate = _authenticate_raises  # type: ignore[method-assign]
    _patch_backend(monkeypatch, backend)

    project = tmp_path / "rollback-project"
    cfg_path = tmp_path / "rollback.yaml"
    creds = tmp_path / "credentials.json"
    creds.write_text("{}")

    result = CliRunner().invoke(
        cli,
        [
            "clone",
            "--backend", "googledrive",
            "--project", str(project),
            "--drive-folder-id", "test-drive-folder",
            "--gcp-project-id", "test-gcp-project",
            "--pubsub-topic-id", "claude-mirror-test",
            "--credentials-file", str(creds),
            "--token-file", str(tmp_path / "token.json"),
            "--config", str(cfg_path),
        ],
    )

    assert result.exit_code != 0, (
        f"auth failure must surface as non-zero exit; got 0 with output: {result.output}"
    )
    assert not cfg_path.exists(), (
        f"clone left the YAML behind after auth failure: {cfg_path}"
    )
    assert "auth failed" in result.output.lower() or "auth failed" in str(result.exception or "").lower()


def test_clone_wizard_mode_drives_through_prompts_to_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--wizard --backend googledrive` walks through the prompt sequence
    and reaches the same end-state as the flag-driven happy path: YAML
    written, token written, files pulled."""
    fake_cfg_dir = _isolate_config_dir(monkeypatch, tmp_path)

    valid_folder_id = "AAAA1111BBBB2222CCCC"  # 20+ URL-safe chars (matches DRIVE_FOLDER_ID_RE)
    backend = FakeStorageBackend(root_folder_id=valid_folder_id)
    payloads = _seed_three_files(backend)

    cfg_token_path = fake_cfg_dir / "wizard-token.json"

    def _authenticate_writes_token() -> Any:
        Path(cfg_token_path).write_text('{"access_token":"fake"}')
        return backend
    backend.authenticate = _authenticate_writes_token  # type: ignore[method-assign]
    _patch_backend(monkeypatch, backend)

    # Cloud-Console URL helpers in the wizard call out to webbrowser.open
    # via _byo_wizard.try_open_browser; stub it so the wizard flow stays
    # fully offline regardless of the host.
    monkeypatch.setattr(cli_mod._byo_wizard, "try_open_browser", lambda url: True)

    # Drive smoke test would attempt a real OAuth flow — short-circuit it
    # at the wizard call site so the wizard returns without any network.
    monkeypatch.setattr(
        cli_mod, "_maybe_run_drive_smoke_test",
        lambda **_kw: None,
    )

    project = tmp_path / "wizard-project"
    project.mkdir()  # wizard validates that the project path exists
    creds = tmp_path / "credentials.json"
    creds.write_text(
        '{"installed":{"client_id":"fake-id-fake-id.apps.googleusercontent.com","client_secret":"x"}}'
    )
    cfg_path = tmp_path / "wizard.yaml"

    # The wizard prompts (in order, with defaults pre-typed where possible):
    #   1. Storage backend [googledrive]            -> Enter
    #   2. Project directory [cwd]                  -> project path
    #   3. GCP project ID                           -> "test-gcp-project"
    #   4. Open Cloud Console pages now? [Y]        -> n  (skip browser prompts)
    #   5. Credentials file [_DEFAULT_CREDENTIALS]  -> creds path
    #   6. Drive folder ID                          -> 20-char folder ID
    #   7. Pub/Sub topic ID [claude-mirror-<name>]  -> Enter (default)
    #   8. Token file [derived]                     -> cfg_token_path
    #   9. Config file [derived]                    -> cfg_path
    #  10. File patterns [**/*.md]                  -> Enter
    #  11. Enable Slack notifications? [N]          -> Enter (no)
    #  12. Exclude patterns []                      -> Enter
    #  13. Snapshot format [blobs]                  -> Enter
    #  14. Save this configuration? [Y]             -> Enter
    stdin = "\n".join([
        "",                              # 1 backend default
        str(project),                    # 2 project dir
        "test-gcp-project",              # 3 GCP project ID
        "n",                             # 4 do NOT open Cloud Console pages
        str(creds),                      # 5 credentials file
        valid_folder_id,                 # 6 Drive folder ID
        "",                              # 7 Pub/Sub topic ID default
        str(cfg_token_path),             # 8 token file
        str(cfg_path),                   # 9 config file
        "",                              # 10 file patterns
        "",                              # 11 Slack? (default N)
        "",                              # 12 exclude patterns
        "",                              # 13 snapshot format default
        "",                              # 14 confirm save
        "",                              # safety: trailing newline
    ]) + "\n"

    result = CliRunner().invoke(
        cli,
        ["clone", "--backend", "googledrive", "--project", str(project), "--wizard"],
        input=stdin,
    )

    # If the wizard gets out of step the test will exit non-zero; surface
    # the captured output to make the failure debuggable.
    assert result.exit_code == 0, (
        f"wizard clone failed: {result.output}\nException: {result.exception}"
    )
    assert cfg_path.exists()
    assert cfg_token_path.exists()
    for rel_path, content in payloads.items():
        local = project / rel_path
        assert local.exists(), f"wizard clone did not pull {rel_path}"
        assert local.read_bytes() == content


def test_clone_sftp_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-backend dispatch is generic — the SFTP path also runs through
    init -> auth -> pull cleanly when the backend cooperates."""
    fake_cfg_dir = _isolate_config_dir(monkeypatch, tmp_path)

    # SFTP requires a real-looking sftp_folder (absolute path) and either
    # key file or password. We use a key file so the YAML serializes
    # cleanly without password warnings.
    key_file = tmp_path / "id_rsa"
    key_file.write_text("dummy-private-key")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")

    backend = FakeStorageBackend(root_folder_id="/srv/claude-mirror/cloned")
    payloads = _seed_three_files(backend)

    cfg_token = fake_cfg_dir / "sftp-cloned-sftp-project-token.json"

    def _authenticate_writes_token() -> Any:
        cfg_token.write_text('{"sftp":"ok"}')
        return backend
    backend.authenticate = _authenticate_writes_token  # type: ignore[method-assign]
    _patch_backend(monkeypatch, backend)

    project = tmp_path / "cloned-sftp-project"
    cfg_path = tmp_path / "sftp.yaml"

    result = CliRunner().invoke(
        cli,
        [
            "clone",
            "--backend", "sftp",
            "--project", str(project),
            "--sftp-host", "sftp.example.com",
            "--sftp-port", "22",
            "--sftp-username", "syncer",
            "--sftp-key-file", str(key_file),
            "--sftp-known-hosts-file", str(known_hosts),
            "--sftp-folder", "/srv/claude-mirror/cloned",
            "--config", str(cfg_path),
            "--token-file", str(cfg_token),
        ],
    )

    assert result.exit_code == 0, result.output
    assert cfg_path.exists()
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw["backend"] == "sftp"
    assert raw["sftp_folder"] == "/srv/claude-mirror/cloned"

    assert cfg_token.exists()
    for rel_path, content in payloads.items():
        local = project / rel_path
        assert local.exists()
        assert local.read_bytes() == content
