"""Tests for the retention-policy defaults written by `claude-mirror init`.

Coverage:
    * Newly initialised YAMLs land with sensible `keep_*` retention defaults
      (10 / 7 / 12 / 3) so the prune path has a policy to act on out of the
      box. Closes out the Scenario A pitfall in docs/scenarios.md.
    * Pre-existing YAMLs without these fields still load with `0` for every
      `keep_*` (back-compat — the dataclass defaults are unchanged).

Both tests are offline, sub-100ms, and run entirely under tmp_path. No real
home directory is touched: `cli.CONFIG_DIR` and the global `CONFIG_DIR` from
`claude_mirror.config` are both monkeypatched to point at tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror import config as config_module
from claude_mirror.cli import cli
from claude_mirror.config import Config

# Click 8.3 emits a Context.protected_args DeprecationWarning from CliRunner;
# pyproject's filterwarnings = "error" otherwise turns that into a failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# Expected retention defaults written into newly initialised YAMLs (v0.5.38+).
EXPECTED_KEEP_LAST = 10
EXPECTED_KEEP_DAILY = 7
EXPECTED_KEEP_MONTHLY = 12
EXPECTED_KEEP_YEARLY = 3


def _isolate_config_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect both `cli.CONFIG_DIR` and `config.CONFIG_DIR` to a tmp dir
    so `init` never touches the real ~/.config/claude_mirror/ tree."""
    fake_cfg_dir = tmp_path / "fake_config_home"
    fake_cfg_dir.mkdir()
    monkeypatch.setattr(cli_module, "CONFIG_DIR", fake_cfg_dir)
    monkeypatch.setattr(config_module, "CONFIG_DIR", fake_cfg_dir)
    return fake_cfg_dir


def test_init_writes_retention_defaults_into_new_yaml(tmp_path, monkeypatch):
    """`claude-mirror init` (non-wizard, flag-driven) must write the four
    `keep_*` retention defaults into the newly created YAML so a fresh
    project has an active retention policy from the very first push."""
    _isolate_config_dir(monkeypatch, tmp_path)

    project = tmp_path / "project"
    project.mkdir()
    config_path = tmp_path / "claude_mirror.yaml"
    key_file = tmp_path / "id_rsa"
    key_file.write_text("dummy-key")
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("")
    token_file = tmp_path / "sftp-token.json"

    # SFTP backend is the simplest happy-path init for tests: no OAuth flow
    # or cloud-side setup is touched at init time, the command just writes
    # YAML and prints a hint to run `auth` next.
    result = CliRunner().invoke(
        cli,
        [
            "init",
            "--backend", "sftp",
            "--project", str(project),
            "--sftp-host", "sftp.example.com",
            "--sftp-port", "22",
            "--sftp-username", "syncer",
            "--sftp-key-file", str(key_file),
            "--sftp-known-hosts-file", str(known_hosts),
            "--sftp-folder", "/srv/claude-mirror/proj",
            "--config", str(config_path),
            "--token-file", str(token_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert config_path.exists(), "init did not write the YAML"

    raw = yaml.safe_load(config_path.read_text())
    assert raw["keep_last"] == EXPECTED_KEEP_LAST, raw
    assert raw["keep_daily"] == EXPECTED_KEEP_DAILY, raw
    assert raw["keep_monthly"] == EXPECTED_KEEP_MONTHLY, raw
    assert raw["keep_yearly"] == EXPECTED_KEEP_YEARLY, raw

    # And the same values must round-trip through Config.load().
    loaded = Config.load(str(config_path))
    assert loaded.keep_last == EXPECTED_KEEP_LAST
    assert loaded.keep_daily == EXPECTED_KEEP_DAILY
    assert loaded.keep_monthly == EXPECTED_KEEP_MONTHLY
    assert loaded.keep_yearly == EXPECTED_KEEP_YEARLY


def test_existing_yaml_without_retention_fields_still_loads_as_disabled(tmp_path):
    """Back-compat: a YAML written before v0.5.38 (no `keep_*` fields)
    must still load with `0` for every retention bucket. The dataclass
    defaults are deliberately left at `0` so omitting these fields keeps
    meaning "no retention" for hand-rolled / pre-existing configs."""
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(
        # Minimum-viable legacy config: just enough fields to construct a
        # Config without errors, and explicitly NO keep_* keys.
        "project_path: /tmp/project\n"
        "backend: googledrive\n"
        "drive_folder_id: legacy-folder-id\n"
        "credentials_file: /tmp/credentials.json\n"
        "token_file: /tmp/token.json\n"
        "file_patterns:\n"
        "  - '**/*.md'\n"
        "exclude_patterns: []\n"
    )

    cfg = Config.load(str(legacy))
    assert cfg.keep_last == 0
    assert cfg.keep_daily == 0
    assert cfg.keep_monthly == 0
    assert cfg.keep_yearly == 0
