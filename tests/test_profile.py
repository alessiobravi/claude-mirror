"""Tests for the credentials-profile feature (PROFILE, v0.5.49).

Covers:
    * `claude_mirror.profiles.load_profile` — read + validation + helpful
      error message when the named profile doesn't exist.
    * `claude_mirror.profiles.apply_profile` — merge precedence (project
      wins over profile when both define a field) + idempotence.
    * `Config.load(profile_override=...)` — flag-driven profile injection.
    * Project YAML with `profile: NAME` — auto-merge at config-load time.
    * `claude-mirror profile list / show / create / delete` subcommands.
    * `--profile NAME` global flag through `init / status` (smoke).

All tests run offline. Each test redirects `claude_mirror.config.CONFIG_DIR`
to a tmp directory so we never touch the user's real
~/.config/claude_mirror/ tree.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from claude_mirror import config as config_module
from claude_mirror import profiles as profiles_module
from claude_mirror.config import Config
from claude_mirror.cli import cli
from claude_mirror.profiles import apply_profile, load_profile, profile_path


# Click 8.3 emits a Context.protected_args DeprecationWarning from
# CliRunner.invoke(); pyproject's filterwarnings = "error" otherwise
# would convert that into a test failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_config_dir(tmp_path: Path, monkeypatch) -> Path:
    """Redirect CONFIG_DIR to tmp_path/cm so neither the profile module
    nor any subcommand touches the user's real ~/.config/claude_mirror/
    tree. Returns the redirected directory.

    The CLI group's invoke() handler also resets the global profile
    override at startup, so we explicitly clear it after each test to
    avoid cross-test bleed.
    """
    cm_dir = tmp_path / "cm"
    cm_dir.mkdir()
    monkeypatch.setattr(config_module, "CONFIG_DIR", cm_dir)
    # cli.py and install.py also import CONFIG_DIR by name from
    # claude_mirror.config — replace those bindings too so subcommand
    # paths derived from CONFIG_DIR (e.g. _derive_config_path,
    # _DEFAULT_CREDENTIALS, default.yaml fallback) all land inside the
    # tmp dir.
    from claude_mirror import cli as cli_module
    monkeypatch.setattr(cli_module, "CONFIG_DIR", cm_dir)
    # Clear any leftover global profile override from a previous test
    # run.
    config_module.set_global_profile_override("")
    yield cm_dir
    config_module.set_global_profile_override("")


@pytest.fixture
def write_profile(isolated_config_dir: Path):
    """Factory: write a profile YAML under <CONFIG_DIR>/profiles/<name>.yaml."""
    def _write(name: str, data: dict) -> Path:
        d = isolated_config_dir / "profiles"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.yaml"
        p.write_text(yaml.dump(data, default_flow_style=False))
        return p
    return _write


# ─── load_profile ──────────────────────────────────────────────────────────

def test_load_profile_reads_yaml(isolated_config_dir, write_profile):
    write_profile("work", {
        "backend": "googledrive",
        "credentials_file": "/Users/me/.config/cm/work-credentials.json",
        "token_file": "/Users/me/.config/cm/work-token.json",
        "gcp_project_id": "my-work-gcp",
        "description": "Work Google account (alice@example.com)",
    })
    data = load_profile("work")
    assert data["backend"] == "googledrive"
    assert data["credentials_file"] == "/Users/me/.config/cm/work-credentials.json"
    assert data["gcp_project_id"] == "my-work-gcp"
    assert data["description"] == "Work Google account (alice@example.com)"


def test_load_profile_missing_lists_available(isolated_config_dir, write_profile):
    """When the named profile doesn't exist BUT others do, the error
    message must list the available names so the user can recover from
    a typo without grepping the filesystem."""
    write_profile("work", {"backend": "googledrive"})
    write_profile("personal", {"backend": "dropbox"})
    with pytest.raises(FileNotFoundError) as excinfo:
        load_profile("wrk")
    msg = str(excinfo.value)
    assert "wrk" in msg
    # All available profile names appear in the error.
    assert "personal" in msg
    assert "work" in msg


def test_load_profile_missing_no_profiles_yet(isolated_config_dir):
    """When NO profiles exist at all, the error must point at
    `profile create` rather than dumping an empty list."""
    with pytest.raises(FileNotFoundError) as excinfo:
        load_profile("anything")
    msg = str(excinfo.value)
    assert "profile create" in msg
    assert "anything" in msg


# ─── apply_profile ─────────────────────────────────────────────────────────

def test_apply_profile_project_wins_over_profile():
    profile = {
        "backend": "googledrive",
        "credentials_file": "/profile/creds.json",
        "gcp_project_id": "profile-gcp",
        "token_file": "/profile/token.json",
    }
    project = {
        "project_path": "/Users/me/proj",
        # Project overrides credentials_file (the escape hatch case)
        "credentials_file": "/project/creds.json",
        # gcp_project_id NOT set on project; profile should win.
        "gcp_project_id": "",
        # token_file omitted entirely; profile should fill in.
    }
    merged = apply_profile(profile, project)
    assert merged["credentials_file"] == "/project/creds.json"
    assert merged["gcp_project_id"] == "profile-gcp"
    assert merged["token_file"] == "/profile/token.json"
    assert merged["project_path"] == "/Users/me/proj"
    assert merged["backend"] == "googledrive"


def test_apply_profile_is_idempotent():
    profile = {"backend": "dropbox", "dropbox_app_key": "abc123"}
    project = {"project_path": "/p", "dropbox_folder": "/cm/p"}
    once = apply_profile(profile, project)
    twice = apply_profile(profile, once)
    assert once == twice


def test_apply_profile_does_not_mutate_inputs():
    profile = {"a": 1}
    project = {"b": 2}
    profile_copy = dict(profile)
    project_copy = dict(project)
    _ = apply_profile(profile, project)
    assert profile == profile_copy
    assert project == project_copy


# ─── Config.load with profile override ─────────────────────────────────────

def test_config_load_with_profile_override(isolated_config_dir, write_profile, tmp_path):
    write_profile("work", {
        "backend": "googledrive",
        "credentials_file": str(tmp_path / "work-creds.json"),
        "token_file": str(tmp_path / "work-token.json"),
    })
    cfg_path = tmp_path / "project.yaml"
    cfg_path.write_text(yaml.dump({
        "project_path": str(tmp_path),
        "backend": "googledrive",
        "drive_folder_id": "ABC123",
    }))
    cfg = Config.load(str(cfg_path), profile_override="work")
    assert cfg.credentials_file == str(tmp_path / "work-creds.json")
    assert cfg.token_file == str(tmp_path / "work-token.json")
    assert cfg.drive_folder_id == "ABC123"


def test_config_load_with_profile_yaml_field(isolated_config_dir, write_profile, tmp_path):
    """A project YAML with `profile: work` at the top must auto-load
    the profile and merge it in — without the caller having to pass
    profile_override or set the global override."""
    write_profile("work", {
        "backend": "googledrive",
        "credentials_file": str(tmp_path / "work-creds.json"),
    })
    cfg_path = tmp_path / "project.yaml"
    cfg_path.write_text(yaml.dump({
        "profile": "work",
        "project_path": str(tmp_path),
        "backend": "googledrive",
        "drive_folder_id": "FOLDER",
    }))
    cfg = Config.load(str(cfg_path))
    assert cfg.credentials_file == str(tmp_path / "work-creds.json")
    assert cfg.drive_folder_id == "FOLDER"


def test_config_load_global_override_beats_yaml_field(
    isolated_config_dir, write_profile, tmp_path
):
    """The global --profile flag (set_global_profile_override) wins
    over a YAML's `profile: NAME` field — flag is the one-shot escape
    hatch."""
    write_profile("work", {
        "credentials_file": "/from-work.json",
    })
    write_profile("personal", {
        "credentials_file": "/from-personal.json",
    })
    cfg_path = tmp_path / "project.yaml"
    cfg_path.write_text(yaml.dump({
        "profile": "personal",
        "project_path": str(tmp_path),
        "backend": "googledrive",
    }))
    config_module.set_global_profile_override("work")
    try:
        cfg = Config.load(str(cfg_path))
    finally:
        config_module.set_global_profile_override("")
    assert cfg.credentials_file == "/from-work.json"


# ─── claude-mirror profile list / show ─────────────────────────────────────

def test_profile_list_outputs_names_and_descriptions(
    isolated_config_dir, write_profile
):
    write_profile("work", {
        "backend": "googledrive",
        "description": "Work account",
    })
    write_profile("personal", {
        "backend": "dropbox",
        "description": "Personal Dropbox",
    })
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "list"])
    assert result.exit_code == 0, result.output
    assert "work" in result.output
    assert "personal" in result.output
    assert "googledrive" in result.output
    assert "dropbox" in result.output
    assert "Work account" in result.output


def test_profile_list_empty_dir(isolated_config_dir):
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "list"])
    assert result.exit_code == 0
    assert "No profiles configured" in result.output


def test_profile_show_cats_yaml(isolated_config_dir, write_profile):
    write_profile("work", {
        "backend": "googledrive",
        "credentials_file": "/x/y.json",
    })
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "show", "work"])
    assert result.exit_code == 0
    assert "backend: googledrive" in result.output
    assert "credentials_file: /x/y.json" in result.output


def test_profile_show_missing_errors(isolated_config_dir, write_profile):
    write_profile("work", {"backend": "googledrive"})
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "show", "nope"])
    assert result.exit_code == 1
    assert "not found" in result.output
    assert "work" in result.output  # available profiles listed


# ─── claude-mirror profile create ──────────────────────────────────────────

def test_profile_create_googledrive_writes_yaml(isolated_config_dir, tmp_path):
    """`profile create work --backend googledrive` writes a YAML with
    backend, credentials_file, and token_file fields. We feed answers
    via stdin to drive the click prompts."""
    creds = tmp_path / "fake-creds.json"
    # The googledrive prompt validates the credentials file via
    # _byo_wizard.validate_credentials_file — write a stub that passes.
    import json
    creds.write_text(json.dumps({
        "installed": {
            "client_id": "fake-id",
            "client_secret": "fake-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }))
    token = tmp_path / "tok.json"
    runner = CliRunner()
    # Three prompts: credentials_file, token_file, gcp_project_id.
    stdin = f"{creds}\n{token}\n\n"
    result = runner.invoke(
        cli, ["profile", "create", "work", "--backend", "googledrive"],
        input=stdin,
    )
    assert result.exit_code == 0, result.output
    target = profile_path("work")
    assert target.exists()
    data = yaml.safe_load(target.read_text())
    assert data["backend"] == "googledrive"
    assert data["credentials_file"] == str(creds)
    assert data["token_file"] == str(token)


def test_profile_create_existing_without_force_errors(
    isolated_config_dir, write_profile
):
    write_profile("work", {"backend": "googledrive"})
    runner = CliRunner()
    result = runner.invoke(
        cli, ["profile", "create", "work", "--backend", "dropbox"],
        input="x\n",
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


# ─── claude-mirror profile delete ──────────────────────────────────────────

def test_profile_delete_dry_run_by_default(isolated_config_dir, write_profile):
    p = write_profile("work", {"backend": "googledrive"})
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "delete", "work"])
    assert result.exit_code == 0
    assert "DRY-RUN" in result.output
    # File still exists
    assert p.exists()


def test_profile_delete_with_yes_removes_file(isolated_config_dir, write_profile):
    p = write_profile("work", {"backend": "googledrive"})
    runner = CliRunner()
    result = runner.invoke(
        cli, ["profile", "delete", "work", "--delete", "--yes"],
    )
    assert result.exit_code == 0
    assert not p.exists()


def test_profile_delete_typed_yes_required(isolated_config_dir, write_profile):
    p = write_profile("work", {"backend": "googledrive"})
    runner = CliRunner()
    # User types "y" (lowercase) instead of "YES" — must abort.
    result = runner.invoke(
        cli, ["profile", "delete", "work", "--delete"],
        input="y\n",
    )
    assert result.exit_code == 1
    assert p.exists()
    assert "Aborted" in result.output


def test_profile_delete_missing_is_no_op(isolated_config_dir):
    runner = CliRunner()
    result = runner.invoke(cli, ["profile", "delete", "ghost"])
    assert result.exit_code == 0
    assert "does not exist" in result.output


# ─── --profile NAME flag through CLI ───────────────────────────────────────

def test_global_profile_flag_unknown_errors_with_list(
    isolated_config_dir, write_profile
):
    write_profile("work", {"backend": "googledrive"})
    runner = CliRunner()
    result = runner.invoke(cli, ["--profile", "nope", "status"])
    assert result.exit_code == 1
    assert "nope" in result.output
    assert "work" in result.output  # listed as available


def test_global_profile_flag_sets_global_override(
    isolated_config_dir, write_profile, tmp_path, monkeypatch
):
    """The global --profile flag must call set_global_profile_override
    so downstream Config.load picks the value up.

    We probe by patching `_resolve_config` to capture the override
    after the cli group has dispatched but before the subcommand runs.
    """
    write_profile("work", {"backend": "googledrive"})
    captured: dict = {}

    from claude_mirror import cli as cli_module

    def spy_resolve(config_path: str) -> str:
        captured["override"] = config_module.get_global_profile_override()
        # Stop further work by raising — we only care that the
        # override was set before dispatch reached this point.
        raise SystemExit(0)

    monkeypatch.setattr(cli_module, "_resolve_config", spy_resolve)

    runner = CliRunner()
    runner.invoke(cli, ["--profile", "work", "status"])
    assert captured.get("override") == "work"


# ─── init --profile NAME ───────────────────────────────────────────────────

def test_init_with_profile_skips_credentials_flag_validation(
    isolated_config_dir, write_profile, tmp_path, monkeypatch
):
    """`init --backend googledrive --profile work --project P --drive-folder-id F
    --pubsub-topic-id T` must succeed — the missing --credentials-file is
    supplied by the profile, NOT a missing-flag error."""
    write_profile("work", {
        "backend": "googledrive",
        "credentials_file": str(tmp_path / "work-creds.json"),
        "token_file": str(tmp_path / "work-token.json"),
        "gcp_project_id": "shared-gcp",
    })
    proj = tmp_path / "proj"
    proj.mkdir()
    # Avoid the watcher-reload subprocess call.
    monkeypatch.setattr(
        "claude_mirror.cli._try_reload_watcher", lambda: None,
    )
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--profile", "work",
        "init",
        "--backend", "googledrive",
        "--project", str(proj),
        "--drive-folder-id", "F123",
        "--pubsub-topic-id", "topic-x",
    ])
    assert result.exit_code == 0, result.output
    # The written YAML must reference the profile and NOT inline the
    # credentials_file (which the profile owns).
    cfg_path = isolated_config_dir / f"{proj.name}.yaml"
    raw = yaml.safe_load(cfg_path.read_text())
    assert raw.get("profile") == "work"
    assert "credentials_file" not in raw
    # On reload the credentials must come from the profile.
    cfg = Config.load(str(cfg_path))
    assert cfg.credentials_file == str(tmp_path / "work-creds.json")


def test_init_without_profile_still_requires_credentials_default(
    isolated_config_dir, tmp_path, monkeypatch
):
    """Sanity: dropping --profile does NOT exempt the user from
    --pubsub-topic-id (regression guard for the optional-flag logic)."""
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(
        "claude_mirror.cli._try_reload_watcher", lambda: None,
    )
    runner = CliRunner()
    result = runner.invoke(cli, [
        "init",
        "--backend", "googledrive",
        "--project", str(proj),
        "--drive-folder-id", "F123",
        # No --pubsub-topic-id, no --gcp-project-id
    ])
    assert result.exit_code == 1
    assert "Missing required options" in result.output
    assert "--pubsub-topic-id" in result.output


# ─── Wizard skip behaviour under --profile ─────────────────────────────────

def test_wizard_skips_credentials_prompt_when_profile_supplies_it(
    isolated_config_dir, write_profile, tmp_path, monkeypatch
):
    """`_run_wizard(profile_data={...})` must NOT prompt for the
    credentials_file when the profile already provides it. We patch
    click.prompt and assert the credentials prompt never appears."""
    from claude_mirror import cli as cli_module

    write_profile("work", {
        "backend": "googledrive",
        "credentials_file": str(tmp_path / "work-creds.json"),
        "token_file": str(tmp_path / "work-token.json"),
        "gcp_project_id": "shared-gcp",
    })
    profile_data = profiles_module.load_profile("work")

    seen_prompts: list[str] = []

    def fake_prompt(label, **kwargs):
        seen_prompts.append(str(label))
        # Fail loudly the moment a credential prompt appears.
        if "Credentials file" in str(label):
            raise AssertionError(
                "Credentials file prompt should be skipped under "
                "--profile work, but the wizard asked for it."
            )
        # Provide canned answers for every other prompt the wizard
        # might ask. We bail out at the first mandatory follow-up
        # prompt (Drive folder ID) so the test stays fast.
        if "Storage backend" in str(label):
            return "googledrive"
        if "Project directory" in str(label):
            return str(tmp_path)
        if "Drive folder ID" in str(label):
            raise _StopAfterDriveFolder()
        return kwargs.get("default", "")

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    # Disable the auto-open URLs branch (would hit the network).
    monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **k: False)

    with pytest.raises(_StopAfterDriveFolder):
        cli_module._run_wizard(
            backend_default="googledrive",
            profile_data=profile_data,
        )

    # Sanity: the wizard ran far enough to ask for the project + drive folder.
    assert any("Storage backend" in p for p in seen_prompts)
    # And the credentials prompt did NOT appear.
    assert not any("Credentials file" in p for p in seen_prompts)


class _StopAfterDriveFolder(Exception):
    """Sentinel — raised by the patched click.prompt to abort the wizard
    once the test has confirmed the credentials prompt was skipped."""
    pass


# ─── docs/profiles/agents-md.yaml sample (AGENTS-MD) ───────────────────────

def test_agents_md_sample_profile_loads_cleanly(
    isolated_config_dir, tmp_path, monkeypatch
):
    """The docs/profiles/agents-md.yaml sample is a maintainer-curated
    copy-paste source. Make sure it stays valid YAML that Config can
    apply without errors. A future schema change in Config that breaks
    the sample will fail here, so docs and code stay aligned."""
    sample = (
        Path(__file__).parent.parent / "docs" / "profiles" / "agents-md.yaml"
    )
    assert sample.exists(), f"Sample profile YAML missing at {sample}"

    raw = yaml.safe_load(sample.read_text())
    assert isinstance(raw, dict), "Sample must be a YAML mapping"

    assert "AGENTS.md" in raw["file_patterns"]
    assert "**/AGENTS.md" in raw["file_patterns"]
    assert ".AGENTS.md" in raw["file_patterns"]
    assert "**/.AGENTS.md" in raw["file_patterns"]

    assert "node_modules/**" in raw["exclude_patterns"]
    assert ".git/**" in raw["exclude_patterns"]

    profiles_dir = isolated_config_dir / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "agents-md.yaml").write_text(sample.read_text())

    loaded = load_profile("agents-md")
    assert loaded["file_patterns"] == raw["file_patterns"]
    assert loaded["exclude_patterns"] == raw["exclude_patterns"]

    cfg_path = tmp_path / "project.yaml"
    cfg_path.write_text(yaml.dump({
        "profile": "agents-md",
        "project_path": str(tmp_path),
        "backend": "googledrive",
        "drive_folder_id": "FOLDER",
        "credentials_file": str(tmp_path / "creds.json"),
        "token_file": str(tmp_path / "token.json"),
        "gcp_project_id": "test-gcp",
        "pubsub_topic_id": "test-topic",
    }))
    cfg = Config.load(str(cfg_path))
    assert "AGENTS.md" in cfg.file_patterns
    assert "**/AGENTS.md" in cfg.file_patterns
    assert "node_modules/**" in cfg.exclude_patterns
