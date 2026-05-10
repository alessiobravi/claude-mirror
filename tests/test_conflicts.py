"""Tests for the AGENT-MERGE conflict-envelope subsystem.

Three layers:
    1. Pure-function tests for `claude_mirror._conflicts` — envelope
       round-trip, version validation, slug + path canonicalisation,
       eligibility gate, list/clear semantics.
    2. CLI-level tests for `claude-mirror conflict {list,show,apply}`
       via Click's CliRunner: empty / populated list, JSON envelope
       shape, `--format markers` ordering, `apply` writeback +
       `--push` integration + idempotence + mutual exclusion of
       `--merged-file` / `--merged-stdin`.
    3. Engine integration tests: a text-file conflict during sync
       writes an envelope; a binary-file conflict does not.

All tests run offline; no network. Each one is <100 ms.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, List

import pytest
from click.testing import CliRunner

from claude_mirror import _conflicts
from claude_mirror import cli as cli_module
from claude_mirror import sync as sync_mod
from claude_mirror._conflicts import (
    ConflictEnvelope,
    ENVELOPE_VERSION,
    build_unified_diff,
    clear_envelope,
    envelope_dir,
    envelope_path,
    is_eligible,
    list_envelopes,
    make_envelope,
    read_envelope,
    write_envelope,
)
from claude_mirror.cli import cli
from claude_mirror.config import Config
from claude_mirror.manifest import Manifest


# Click 8.3 emits a DeprecationWarning from inside CliRunner.invoke.
# pyproject.toml's filterwarnings="error" turns it into a failure.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolated_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Redirect `XDG_STATE_HOME` into the test's tmp_path so envelopes
    never escape into the user's real `~/.local/state/`. The autouse
    decorator means every test in this module gets the isolation for
    free."""
    state = tmp_path / "_state_home"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state


def _make_envelope(
    *,
    rel_path: str = "memory/foo.md",
    local: str = "local content\n",
    remote: str = "remote content\n",
    base: str | None = None,
    base_hash: str | None = None,
    project: Path | None = None,
    backend: str = "fake",
) -> ConflictEnvelope:
    project_path = project or Path("/tmp/test-project")
    return make_envelope(
        rel_path=rel_path,
        local_text=local,
        remote_text=remote,
        base_text=base,
        base_hash=base_hash,
        project_path=project_path,
        backend=backend,
    )


# ─── Pure-function tests: envelope round-trip ────────────────────────────────


def test_make_envelope_populates_every_field(tmp_path: Path):
    project = tmp_path / "project"
    env = make_envelope(
        rel_path="notes/idea.md",
        local_text="local\n",
        remote_text="remote\n",
        base_text=None,
        base_hash="deadbeef",
        project_path=project,
        backend="dropbox",
    )
    assert env.path == "notes/idea.md"
    assert env.local_text == "local\n"
    assert env.remote_text == "remote\n"
    assert env.base_text is None
    assert env.base_hash == "deadbeef"
    assert env.local_hash != env.remote_hash
    # SHA-256 hex is 64 chars.
    assert len(env.local_hash) == 64
    assert env.project_path == str(project)
    assert env.backend == "dropbox"
    assert env.version == ENVELOPE_VERSION
    # created_at is an ISO 8601 string ending with Z.
    assert env.created_at.endswith("Z")
    # unified_diff includes both bodies' content lines.
    assert "remote/notes/idea.md" in env.unified_diff
    assert "local/notes/idea.md" in env.unified_diff


def test_envelope_round_trip_preserves_every_field(tmp_path: Path):
    project = tmp_path / "project"
    env = make_envelope(
        rel_path="memory/CLAUDE.md",
        local_text="hello local\n",
        remote_text="hello remote\n",
        base_text="hello base\n",
        base_hash="0" * 64,
        project_path=project,
        backend="googledrive",
    )
    target = write_envelope(env, project_path=project)
    assert target.exists()
    loaded = read_envelope(target)
    assert loaded == env


def test_envelope_atomic_write_uses_replace(tmp_path: Path):
    """The temp file must be cleaned up after success — no orphan
    `.envelope.*.merge.json` files left behind."""
    project = tmp_path / "project"
    env = _make_envelope(project=project)
    target = write_envelope(env, project_path=project)
    siblings = list(target.parent.iterdir())
    assert siblings == [target]


def test_read_envelope_rejects_unknown_version(tmp_path: Path):
    project = tmp_path / "project"
    target = envelope_path(project, "memory/foo.md")
    target.write_text(json.dumps({"version": 999, "path": "memory/foo.md"}), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        read_envelope(target)


def test_read_envelope_missing_file_raises_filenotfound(tmp_path: Path):
    project = tmp_path / "project"
    target = envelope_path(project, "memory/missing.md")
    # envelope_path creates the parent dir; remove the file just in case.
    if target.exists():
        target.unlink()
    with pytest.raises(FileNotFoundError):
        read_envelope(target)


def test_read_envelope_ignores_extra_fields(tmp_path: Path):
    """Forward-compat: a future additive field on disk must not crash
    today's CLI. read_envelope filters to known dataclass fields."""
    project = tmp_path / "project"
    env = _make_envelope(project=project)
    target = write_envelope(env, project_path=project)
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["future_field"] = "ignored"
    target.write_text(json.dumps(raw), encoding="utf-8")
    loaded = read_envelope(target)
    assert loaded.path == env.path


# ─── is_eligible ─────────────────────────────────────────────────────────────


def test_is_eligible_text_text_returns_true():
    assert is_eligible(b"hello\n", b"world\n") is True


def test_is_eligible_local_binary_returns_false():
    assert is_eligible(b"\x00\x01binary\x00data", b"world\n") is False


def test_is_eligible_remote_binary_returns_false():
    assert is_eligible(b"hello\n", b"\x00binary remote") is False


def test_is_eligible_local_none_returns_false():
    assert is_eligible(None, b"world\n") is False


def test_is_eligible_remote_none_returns_false():
    assert is_eligible(b"hello\n", None) is False


def test_is_eligible_both_none_returns_false():
    assert is_eligible(None, None) is False


def test_is_eligible_utf8_with_high_bytes_is_text():
    # Café — UTF-8 multi-byte but not binary.
    assert is_eligible("café\n".encode("utf-8"), b"hello\n") is True


# ─── envelope_path / envelope_dir ────────────────────────────────────────────


def test_envelope_path_flattens_slashes(tmp_path: Path):
    project = tmp_path / "project"
    p = envelope_path(project, "memory/foo/bar.md")
    assert p.name == "memory__foo__bar.md.merge.json"


def test_envelope_path_no_subdirs_in_rel_path(tmp_path: Path):
    project = tmp_path / "project"
    p = envelope_path(project, "single.md")
    assert p.name == "single.md.merge.json"


def test_envelope_path_normalises_backslashes(tmp_path: Path):
    """Windows-shaped rel-paths land at the same envelope as their
    POSIX-shaped equivalents."""
    project = tmp_path / "project"
    win = envelope_path(project, "memory\\foo.md")
    posix = envelope_path(project, "memory/foo.md")
    assert win == posix


def test_envelope_dir_creates_on_demand(tmp_path: Path):
    project = tmp_path / "project-fresh"
    d = envelope_dir(project)
    assert d.exists()
    assert d.is_dir()


def test_envelope_dir_uses_xdg_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    custom = tmp_path / "custom_state"
    custom.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(custom))
    project = tmp_path / "project-xdg"
    d = envelope_dir(project)
    assert str(d).startswith(str(custom))


def test_envelope_dir_falls_back_to_home_local_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
):
    """Without XDG_STATE_HOME, the dir lives under `~/.local/state`."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "fake_home")
    project = tmp_path / "project-fallback"
    d = envelope_dir(project)
    assert ".local/state/claude-mirror" in str(d).replace("\\", "/")


def test_two_projects_have_disjoint_envelope_dirs(tmp_path: Path):
    """Same rel-path on two different project paths must NOT collide."""
    p1 = tmp_path / "proj-a"
    p2 = tmp_path / "proj-b"
    e1 = envelope_path(p1, "memory/note.md")
    e2 = envelope_path(p2, "memory/note.md")
    assert e1 != e2


# ─── M2: envelope dir mode is 0o700 ────────────────────────────────────────────


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="Windows chmod is a no-op for non-readonly bits — same skip "
    "convention as the SFTP-deep test suite. The mkdir(mode=0o700) call "
    "still happens; we just can't verify the bits via stat().",
)
def test_envelope_dir_is_chmod_0o700_on_creation(tmp_path: Path):
    """The envelope dir holds project-internal rel-paths in its
    filenames (e.g. `memory__keys__deploy.md.merge.json`). A
    world-readable directory listing would expose those paths to any
    other local user. Same hygiene as ~/.ssh."""
    project = tmp_path / "project-mode"
    d = envelope_dir(project)
    actual_mode = d.stat().st_mode & 0o777
    assert actual_mode == 0o700, (
        f"envelope_dir created with mode {oct(actual_mode)}; expected 0o700"
    )


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="POSIX-only mode test (see test above).",
)
def test_envelope_dir_chmod_tightens_permissive_existing_dir(tmp_path: Path):
    """If the dir was created earlier (e.g. by a prior version of
    claude-mirror with the default umask 0o022 → mode 0o755),
    `envelope_dir(...)` MUST tighten it to 0o700 on next call."""
    import os as _os

    # Construct the path manually with mode 0o755 to simulate an old
    # version having created it before the fix.
    state = tmp_path / "_state"
    state.mkdir()
    monkeypatch_env = state
    import os
    os.environ["XDG_STATE_HOME"] = str(monkeypatch_env)
    try:
        target = state / "claude-mirror" / "_proj_slug" / "conflicts"
        target.mkdir(parents=True)
        _os.chmod(target, 0o755)
        assert (target.stat().st_mode & 0o777) == 0o755

        # Now invoke envelope_dir with a project path whose slug matches
        # — easier to bypass: just call envelope_dir on a fresh path
        # and verify post-call mode is 0o700.
        project = tmp_path / "fresh-project"
        d = envelope_dir(project)
        # Verify the call's own dir is 0o700.
        assert (d.stat().st_mode & 0o777) == 0o700

        # Manually pre-create the EXACT path envelope_dir would target
        # for `project` and chmod it permissive, then re-call.
        _os.chmod(d, 0o755)
        assert (d.stat().st_mode & 0o777) == 0o755
        # Re-call must tighten.
        d2 = envelope_dir(project)
        assert d == d2
        assert (d2.stat().st_mode & 0o777) == 0o700
    finally:
        os.environ.pop("XDG_STATE_HOME", None)


# ─── list_envelopes ──────────────────────────────────────────────────────────


def test_list_envelopes_empty_returns_empty_list(tmp_path: Path):
    project = tmp_path / "project"
    assert list_envelopes(project) == []


def test_list_envelopes_returns_alphabetical_order(tmp_path: Path):
    project = tmp_path / "project"
    write_envelope(_make_envelope(rel_path="zeta.md", project=project), project_path=project)
    write_envelope(_make_envelope(rel_path="alpha.md", project=project), project_path=project)
    write_envelope(_make_envelope(rel_path="middle.md", project=project), project_path=project)
    paths = [e.path for e in list_envelopes(project)]
    assert paths == sorted(paths)


def test_list_envelopes_ignores_non_merge_json(tmp_path: Path):
    project = tmp_path / "project"
    write_envelope(_make_envelope(rel_path="real.md", project=project), project_path=project)
    # Drop a stray file in the same dir — must not appear in the listing.
    d = envelope_dir(project)
    (d / "stray.txt").write_text("not an envelope")
    (d / "stray.json").write_text("{}")
    listed = [e.path for e in list_envelopes(project)]
    assert listed == ["real.md"]


def test_list_envelopes_skips_unparseable_silently(tmp_path: Path):
    """A bad envelope (invalid JSON / wrong version) must not crash the
    listing — the user's good envelopes are still surfaced."""
    project = tmp_path / "project"
    write_envelope(_make_envelope(rel_path="good.md", project=project), project_path=project)
    bad = envelope_dir(project) / "bad.merge.json"
    bad.write_text("not valid json {")
    listed = [e.path for e in list_envelopes(project)]
    assert listed == ["good.md"]


# ─── clear_envelope ──────────────────────────────────────────────────────────


def test_clear_envelope_removes_existing(tmp_path: Path):
    project = tmp_path / "project"
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    assert clear_envelope(project, "x.md") is True
    assert list_envelopes(project) == []


def test_clear_envelope_idempotent_returns_false_when_absent(tmp_path: Path):
    project = tmp_path / "project"
    # Pre-create the envelope dir but no file.
    envelope_dir(project)
    assert clear_envelope(project, "never-existed.md") is False


# ─── build_unified_diff ──────────────────────────────────────────────────────


def test_build_unified_diff_includes_remote_and_local_headers():
    diff = build_unified_diff("alpha\n", "beta\n", "rel.md")
    assert "remote/rel.md" in diff
    assert "local/rel.md" in diff
    # alpha is the "from" (remote), beta is the "to" (local) — alpha
    # gets a `-` line, beta a `+` line.
    assert "-alpha" in diff
    assert "+beta" in diff


def test_build_unified_diff_empty_when_identical():
    """No diff lines when remote == local (degenerate case — sync would
    not call this with identical inputs but the helper must be robust)."""
    diff = build_unified_diff("same\n", "same\n", "rel.md")
    assert diff == ""


# ─── CLI: conflict list ──────────────────────────────────────────────────────


def _make_config_yaml(tmp_path: Path, project_dir: Path) -> Path:
    """Write a minimal valid config YAML pointing at project_dir."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir(exist_ok=True)
    cfg_path = cfg_dir / "test.yaml"
    cfg_path.write_text(
        f"backend: googledrive\n"
        f"project_path: {project_dir}\n"
        f"drive_folder_id: test-folder-id\n"
        f"credentials_file: {cfg_dir}/credentials.json\n"
        f"token_file: {cfg_dir}/token.json\n"
        f"file_patterns: ['**/*.md']\n",
        encoding="utf-8",
    )
    return cfg_path


def test_cli_conflict_list_empty_prints_no_pending(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    runner = CliRunner()
    result = runner.invoke(cli, ["conflict", "list", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "No pending conflicts" in result.output


def test_cli_conflict_list_table_shows_rel_paths(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(
        _make_envelope(rel_path="memory/note.md", project=project),
        project_path=project,
    )
    write_envelope(
        _make_envelope(rel_path="CLAUDE.md", project=project),
        project_path=project,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["conflict", "list", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "memory/note.md" in result.output
    assert "CLAUDE.md" in result.output


def test_cli_conflict_list_json_shape(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(
        _make_envelope(rel_path="alpha.md", project=project),
        project_path=project,
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["conflict", "list", "--config", str(cfg), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schema"] == "v1"
    assert payload["command"] == "conflict-list"
    assert "generated_at" in payload
    assert isinstance(payload["conflicts"], list)
    assert len(payload["conflicts"]) == 1
    assert payload["conflicts"][0]["path"] == "alpha.md"


def test_cli_conflict_list_json_empty(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    runner = CliRunner()
    result = runner.invoke(cli, ["conflict", "list", "--config", str(cfg), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["conflicts"] == []


# ─── CLI: conflict show ──────────────────────────────────────────────────────


def test_cli_conflict_show_envelope_format_returns_json(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    env = _make_envelope(
        rel_path="memory/x.md",
        local="my local edit\n",
        remote="their remote edit\n",
        project=project,
    )
    write_envelope(env, project_path=project)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "show", "memory/x.md", "--config", str(cfg)],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["path"] == "memory/x.md"
    assert parsed["local_text"] == "my local edit\n"
    assert parsed["remote_text"] == "their remote edit\n"
    assert parsed["version"] == ENVELOPE_VERSION


def test_cli_conflict_show_markers_format(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    env = _make_envelope(
        rel_path="readme.md",
        local="LOCAL VERSION\n",
        remote="REMOTE VERSION\n",
        project=project,
    )
    write_envelope(env, project_path=project)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "show", "readme.md", "--config", str(cfg),
              "--format", "markers"],
    )
    assert result.exit_code == 0
    out = result.output
    # Markers in correct order: local first, then ======= separator,
    # then remote.
    assert "<<<<<<< local" in out
    assert ">>>>>>> remote" in out
    assert "=======" in out
    assert "LOCAL VERSION" in out
    assert "REMOTE VERSION" in out
    # local appears before remote.
    assert out.index("LOCAL VERSION") < out.index("REMOTE VERSION")


def test_cli_conflict_show_markers_with_base_includes_base_marker(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    env = make_envelope(
        rel_path="three.md",
        local_text="LOCAL\n",
        remote_text="REMOTE\n",
        base_text="BASE\n",
        base_hash="abc",
        project_path=project,
        backend="fake",
    )
    write_envelope(env, project_path=project)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "show", "three.md", "--config", str(cfg),
              "--format", "markers"],
    )
    assert result.exit_code == 0
    out = result.output
    assert "||||||| base" in out
    assert "BASE" in out


def test_cli_conflict_show_json_alias(tmp_path: Path):
    """`--json` should be a shorthand for `--format envelope`."""
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="a.md", project=project), project_path=project)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "show", "a.md", "--config", str(cfg), "--json"],
    )
    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["path"] == "a.md"


def test_cli_conflict_show_missing_envelope_exits_one(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "show", "never-conflicted.md", "--config", str(cfg)],
    )
    assert result.exit_code == 1
    assert "No pending conflict" in result.output


def test_cli_conflict_show_json_with_format_markers_errors(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "show", "x.md", "--config", str(cfg),
              "--json", "--format", "markers"],
    )
    assert result.exit_code == 1
    assert "conflicts" in result.output.lower() or "json" in result.output.lower()


# ─── CLI: conflict apply ─────────────────────────────────────────────────────


class _FakeEngine:
    """Drop-in replacement for SyncEngine just for `conflict apply`'s
    `engine.push([path], force_local=True)` call. Records the args."""

    def __init__(self) -> None:
        self.push_calls: list[tuple[list[str], bool]] = []

    def push(self, paths: Any, force_local: bool = False) -> None:
        self.push_calls.append((list(paths or []), force_local))


def _patch_load_engine(monkeypatch: pytest.MonkeyPatch) -> _FakeEngine:
    fake = _FakeEngine()

    def fake_load_engine(*_args: Any, **_kwargs: Any) -> Any:
        return fake, None, None

    monkeypatch.setattr(cli_module, "_load_engine", fake_load_engine)
    return fake


def test_cli_conflict_apply_writes_merged_file_and_clears_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    project.mkdir()
    # Binary write to avoid Windows text-mode \n -> \r\n translation; the
    # engine reads local content via `read_bytes()` so the on-disk bytes
    # land in the envelope verbatim and the assertions must compare to
    # the literal LF-only payload regardless of platform.
    (project / "x.md").write_bytes(b"OLD LOCAL\n")
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    fake = _patch_load_engine(monkeypatch)

    merged = tmp_path / "merged.md"
    merged.write_bytes(b"MERGED CONTENT\n")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "apply", "x.md",
              "--config", str(cfg),
              "--merged-file", str(merged)],
    )
    assert result.exit_code == 0, result.output
    # File written.
    assert (project / "x.md").read_text(encoding="utf-8") == "MERGED CONTENT\n"
    # Envelope cleared.
    assert list_envelopes(project) == []
    # Push fired with --force-local.
    assert fake.push_calls == [(["x.md"], True)]


def test_cli_conflict_apply_no_push_skips_engine_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "x.md").write_text("OLD\n", encoding="utf-8")
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    fake = _patch_load_engine(monkeypatch)

    merged = tmp_path / "merged.md"
    merged.write_text("MERGED\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "apply", "x.md",
              "--config", str(cfg),
              "--merged-file", str(merged),
              "--no-push"],
    )
    assert result.exit_code == 0
    # No push call recorded.
    assert fake.push_calls == []
    # Envelope still cleared.
    assert list_envelopes(project) == []


def test_cli_conflict_apply_idempotent_when_envelope_already_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    # No envelope written.
    fake = _patch_load_engine(monkeypatch)
    merged = tmp_path / "merged.md"
    merged.write_text("DOES NOT MATTER\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "apply", "missing.md",
              "--config", str(cfg),
              "--merged-file", str(merged)],
    )
    assert result.exit_code == 0
    assert "already resolved" in result.output
    assert fake.push_calls == []


def test_cli_conflict_apply_requires_one_input_source(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    runner = CliRunner()
    # Neither --merged-file nor --merged-stdin → error.
    result_neither = runner.invoke(
        cli, ["conflict", "apply", "x.md", "--config", str(cfg)],
    )
    assert result_neither.exit_code == 1
    assert "exactly one" in result_neither.output


def test_cli_conflict_apply_rejects_both_input_sources(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    merged = tmp_path / "merged.md"
    merged.write_text("X\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "apply", "x.md",
              "--config", str(cfg),
              "--merged-file", str(merged),
              "--merged-stdin"],
    )
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_cli_conflict_apply_reads_from_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    project = tmp_path / "project"
    project.mkdir()
    (project / "x.md").write_text("OLD\n", encoding="utf-8")
    cfg = _make_config_yaml(tmp_path, project)
    write_envelope(_make_envelope(rel_path="x.md", project=project), project_path=project)
    _patch_load_engine(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        cli, ["conflict", "apply", "x.md",
              "--config", str(cfg),
              "--merged-stdin",
              "--no-push"],
        input="STDIN MERGE\n",
    )
    assert result.exit_code == 0, result.output
    assert (project / "x.md").read_text(encoding="utf-8") == "STDIN MERGE\n"


# ─── CLI: --help documentation ───────────────────────────────────────────────


def test_cli_conflict_help_lists_all_three_subcommands():
    runner = CliRunner()
    result = runner.invoke(cli, ["conflict", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "show" in result.output
    assert "apply" in result.output


# ─── Engine integration ─────────────────────────────────────────────────────


def test_engine_writes_envelope_for_text_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A text-file conflict must produce an envelope at the canonical path."""
    project = tmp_path / "project"
    project.mkdir()
    rel = "memory/note.md"
    (project / "memory").mkdir()
    # Binary write to avoid Windows text-mode \n -> \r\n translation.
    (project / "memory" / "note.md").write_bytes(b"LOCAL\n")

    # Build a minimal FileSyncState shape — only the fields _resolve_conflict
    # actually reads.
    from claude_mirror.sync import FileSyncState, Status

    class _MockStorage:
        backend_name = "fake"

        def download_file(self, file_id: str) -> bytes:
            return b"REMOTE\n"

    class _MockMerge:
        def resolve_conflict(self, *_args: Any, **_kwargs: Any) -> Any:
            # Simulate user picking "skip" — returning None.
            return None

    class _MockEngine:
        merge = _MockMerge()
        storage = _MockStorage()
        manifest = Manifest(str(project))
        _project = project
        _mirrors: list[Any] = []

    state = FileSyncState(
        rel_path=rel,
        status=Status.CONFLICT,
        local_hash="x",
        drive_hash="y",
        drive_file_id="fake-id",
    )
    # Bind the real method to our mock.
    sync_mod.SyncEngine._resolve_conflict(_MockEngine(), state)
    # Envelope appears at the canonical path.
    expected = envelope_path(project, rel)
    assert expected.exists()
    loaded = read_envelope(expected)
    assert loaded.path == rel
    assert loaded.local_text == "LOCAL\n"
    assert loaded.remote_text == "REMOTE\n"


def test_engine_skips_envelope_for_binary_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """A binary conflict (NUL byte on either side) must NOT produce an envelope."""
    project = tmp_path / "project"
    project.mkdir()
    rel = "image.bin"
    (project / "image.bin").write_bytes(b"\x00binary local\x00data")

    from claude_mirror.sync import FileSyncState, Status

    class _MockStorage:
        backend_name = "fake"

        def download_file(self, file_id: str) -> bytes:
            return b"\x00binary remote\x00content"

    class _MockMerge:
        def resolve_conflict(self, *_args: Any, **_kwargs: Any) -> Any:
            return None

    class _MockEngine:
        merge = _MockMerge()
        storage = _MockStorage()
        manifest = Manifest(str(project))
        _project = project
        _mirrors: list[Any] = []

    state = FileSyncState(
        rel_path=rel,
        status=Status.CONFLICT,
        local_hash="x",
        drive_hash="y",
        drive_file_id="fake-id",
    )
    sync_mod.SyncEngine._resolve_conflict(_MockEngine(), state)
    # No envelope should be written.
    expected = envelope_path(project, rel)
    assert not expected.exists()


def test_engine_clears_envelope_on_keep_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Picking keep-local resolves the conflict — envelope must be cleared."""
    project = tmp_path / "project"
    project.mkdir()
    rel = "x.md"
    # Binary write to avoid Windows text-mode \n -> \r\n translation.
    (project / "x.md").write_bytes(b"LOCAL\n")

    from claude_mirror.sync import FileSyncState, Status

    class _MockStorage:
        backend_name = "fake"

        def download_file(self, file_id: str) -> bytes:
            return b"REMOTE\n"

    class _MockMerge:
        def resolve_conflict(self, rel_path: str, local: str, remote: str) -> Any:
            return local, "local"

    pushed: list[Any] = []

    class _MockEngine:
        merge = _MockMerge()
        storage = _MockStorage()
        manifest = Manifest(str(project))
        _project = project
        _mirrors: list[Any] = []

        def _push_file(self, state: Any) -> None:
            pushed.append(state.rel_path)

    state = FileSyncState(
        rel_path=rel,
        status=Status.CONFLICT,
        local_hash="x",
        drive_hash="y",
        drive_file_id="fake-id",
    )
    result = sync_mod.SyncEngine._resolve_conflict(_MockEngine(), state)
    assert result == "pushed"
    # Envelope cleared.
    assert not envelope_path(project, rel).exists()
