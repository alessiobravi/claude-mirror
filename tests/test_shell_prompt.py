"""Tests for SHELL-PROMPT — the network-free `claude-mirror prompt` subcommand.

The contract under test:

  * Output reflects local state ONLY (manifest + local file walk + hash cache).
    Never the network. Tests run with no backend wired up.
  * Exit code is ALWAYS 0 — even on corrupt-manifest, missing-config, or
    other error paths — so a user's shell prompt never breaks.
  * Format flags (`--format symbols / ascii / text / json`) all derive from
    the same internal PromptState so the four surfaces stay in lock-step.
  * `--quiet-when-clean` suppresses output entirely when the project is
    fully in sync.
  * The prompt cache file at `.claude_mirror_prompt_cache.json` short-circuits
    the file walk when the manifest hasn't changed and the file count is
    unchanged — verified by patching the walker and counting calls.
  * Cold-cache wall time stays well under the spec'd 50ms target on a
    500-file synthetic project (the assertion has CI-runner slack).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from claude_mirror import _prompt as prompt_mod
from claude_mirror.cli import cli


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _md5(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _write_yaml_config(config_path: Path, project_path: Path) -> None:
    config_path.write_text(
        f"project_path: {project_path}\n"
        "backend: googledrive\n"
        "drive_folder_id: test-folder-id\n"
        'file_patterns:\n  - "**/*.md"\n'
    )


def _write_manifest(project_dir: Path, entries: dict) -> None:
    (project_dir / ".claude_mirror_manifest.json").write_text(
        json.dumps(entries, indent=2)
    )


def _write_hash_cache(project_dir: Path, project_files: dict[str, str]) -> None:
    """Pre-populate the hash cache so the prompt path can resolve without
    rehashing — mirrors what a recent `claude-mirror status` run would
    have left behind."""
    cache: dict = {}
    for rel, content in project_files.items():
        full = project_dir / rel
        st = full.stat()
        cache[rel] = [st.st_size, st.st_mtime_ns, _md5(content)]
    (project_dir / ".claude_mirror_hash_cache.json").write_text(json.dumps(cache))


def test_prompt_in_sync_emits_check_symbol(project_dir, write_files):
    files = {"a.md": "alpha\n", "b.md": "bravo\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "id-a",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("alpha\n")},
        "b.md": {"synced_hash": _md5("bravo\n"), "remote_file_id": "id-b",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("bravo\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == prompt_mod.SYMBOL_OK


def test_prompt_local_ahead_emits_up_arrow(project_dir, write_files):
    files = {"a.md": "alpha-modified\n", "b.md": "bravo\n", "c.md": "charlie-modified\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha-old\n"), "remote_file_id": "id-a",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("alpha-old\n")},
        "b.md": {"synced_hash": _md5("bravo\n"), "remote_file_id": "id-b",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("bravo\n")},
        "c.md": {"synced_hash": _md5("charlie-old\n"), "remote_file_id": "id-c",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("charlie-old\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"{prompt_mod.SYMBOL_AHEAD}2"


def test_prompt_new_local_files_counted_with_local_ahead(project_dir, write_files):
    files = {"a.md": "alpha\n", "fresh1.md": "new1\n", "fresh2.md": "new2\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "id-a",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("alpha\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"{prompt_mod.SYMBOL_AHEAD}2"


def test_prompt_quiet_when_clean_emits_empty_string(project_dir, write_files):
    files = {"a.md": "alpha\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "id-a",
                 "synced_at": "2026-01-01T00:00:00Z",
                 "synced_remote_hash": _md5("alpha\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(
        cli, ["prompt", "--config", str(cfg_path), "--quiet-when-clean"]
    )
    assert result.exit_code == 0, result.output
    assert result.output == "\n"


def test_prompt_format_ascii_uses_plus_minus(project_dir, write_files):
    files = {"a.md": "alpha-mod\n", "b.md": "bravo\n", "c.md": "charlie-mod\n",
             "d.md": "delta-mod\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("a\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("a\n")},
        "b.md": {"synced_hash": _md5("bravo\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("bravo\n")},
        "c.md": {"synced_hash": _md5("c\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("c\n"),
                 "remotes": {"mirror1": {
                     "remote_file_id": "m1", "synced_remote_hash": "",
                     "state": "pending_retry", "last_error": "",
                     "last_attempt": "", "intended_hash": "", "attempts": 1,
                 }}},
        "d.md": {"synced_hash": _md5("d\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("d\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(
        cli, ["prompt", "--config", str(cfg_path), "--format", "ascii"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "+3 ~1"


def test_prompt_format_text_uses_words(project_dir, write_files):
    files = {"a.md": "modified-a\n", "b.md": "bravo\n",
             "c.md": "modified-c\n", "d.md": "modified-d\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("old-a\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("old-a\n")},
        "b.md": {"synced_hash": _md5("bravo\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("bravo\n"),
                 "remotes": {"m1": {
                     "remote_file_id": "m", "synced_remote_hash": "",
                     "state": "pending_retry", "last_error": "",
                     "last_attempt": "", "intended_hash": "", "attempts": 1,
                 }}},
        "c.md": {"synced_hash": _md5("old-c\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("old-c\n")},
        "d.md": {"synced_hash": _md5("old-d\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("old-d\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(
        cli, ["prompt", "--config", str(cfg_path), "--format", "text"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "+3 ahead, 1 conflict"


def test_prompt_format_json_emits_parseable_dict(project_dir, write_files):
    files = {"a.md": "modified\n", "b.md": "bravo\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("old\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("old\n")},
        "b.md": {"synced_hash": _md5("bravo\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("bravo\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(
        cli, ["prompt", "--config", str(cfg_path), "--format", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["in_sync"] is False
    assert payload["local_ahead"] == 1
    assert payload["remote_ahead"] == 0
    assert payload["conflicts"] == 0
    assert payload["no_manifest"] is False
    assert payload["error"] is False


def test_prompt_no_config_exits_silently(tmp_path, monkeypatch):
    """A directory that doesn't match any configured project should print
    nothing and exit 0 — embedding `claude-mirror prompt` in PS1 must
    NOT inject text in non-claude-mirror directories."""
    cfg_dir = tmp_path / "config_dir"
    cfg_dir.mkdir()
    monkeypatch.setattr("claude_mirror.cli.CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(
        "claude_mirror.cli.DEFAULT_CONFIG", str(cfg_dir / "default.yaml")
    )

    other_dir = tmp_path / "not_a_project"
    other_dir.mkdir()
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=other_dir):
        result = runner.invoke(cli, ["prompt"])

    assert result.exit_code == 0, result.output
    assert result.output == ""


def test_prompt_corrupt_manifest_exits_zero_with_warning(project_dir, write_files):
    write_files({"a.md": "alpha\n"})
    (project_dir / ".claude_mirror_manifest.json").write_text("not-valid-json{{{")

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert result.exit_code == 0, (result.output, result.stderr_bytes)
    assert result.output.strip() == prompt_mod.SYMBOL_ERROR


def test_prompt_cache_returns_same_value_when_manifest_unchanged(
    project_dir, write_files, monkeypatch,
):
    files = {"a.md": "alpha\n", "b.md": "bravo\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("alpha\n")},
        "b.md": {"synced_hash": _md5("bravo\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("bravo\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    real_compute = prompt_mod._compute_state_uncached
    call_count = {"n": 0}

    def counting(*args, **kwargs):
        call_count["n"] += 1
        return real_compute(*args, **kwargs)

    monkeypatch.setattr(prompt_mod, "_compute_state_uncached", counting)

    runner = CliRunner()
    r1 = runner.invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert r1.exit_code == 0
    assert call_count["n"] == 1
    first_output = r1.output

    r2 = runner.invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert r2.exit_code == 0
    assert r2.output == first_output
    assert call_count["n"] == 1


def test_prompt_cache_invalidates_when_manifest_rewritten(
    project_dir, write_files,
):
    files = {"a.md": "alpha\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("alpha\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    runner = CliRunner()
    r1 = runner.invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert r1.exit_code == 0
    assert r1.output.strip() == prompt_mod.SYMBOL_OK

    time.sleep(0.01)
    (project_dir / "a.md").write_text("alpha-modified\n", newline="")
    _write_hash_cache(project_dir, {"a.md": "alpha-modified\n"})
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("alpha\n")},
    })

    r2 = runner.invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert r2.exit_code == 0
    assert r2.output.strip() == f"{prompt_mod.SYMBOL_AHEAD}1"


def test_prompt_no_manifest_yet_emits_question_mark(project_dir, write_files):
    files = {"a.md": "alpha\n"}
    write_files(files)
    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == prompt_mod.SYMBOL_NO_MANIFEST


def test_prompt_prefix_and_suffix_wrap_output(project_dir, write_files):
    files = {"a.md": "modified\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("old\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("old\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(
        cli, ["prompt", "--config", str(cfg_path),
              "--prefix", "[", "--suffix", "]"]
    )
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"[{prompt_mod.SYMBOL_AHEAD}1]"


def test_prompt_quiet_when_clean_skips_prefix_and_suffix(project_dir, write_files):
    """When fully in sync and `--quiet-when-clean` is set, the wrapper
    must NOT emit the prefix/suffix — otherwise users embedding a
    leading space in the prefix would see a stray space in their PS1
    on every clean redraw."""
    files = {"a.md": "alpha\n"}
    write_files(files)
    _write_manifest(project_dir, {
        "a.md": {"synced_hash": _md5("alpha\n"), "remote_file_id": "x",
                 "synced_at": "t", "synced_remote_hash": _md5("alpha\n")},
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(
        cli, ["prompt", "--config", str(cfg_path),
              "--quiet-when-clean", "--prefix", " ", "--suffix", "!"]
    )
    assert result.exit_code == 0, result.output
    assert result.output == "\n"


def test_prompt_under_50ms_on_typical_project(project_dir, write_files):
    """Cold-cache wall time on a 500-file synthetic project must clear
    a comfortably loose envelope (real target: 50ms; CI slack: 500ms)."""
    files = {f"doc_{i:04d}.md": f"content-{i}\n" for i in range(500)}
    write_files(files)
    _write_manifest(project_dir, {
        rel: {
            "synced_hash": _md5(content),
            "remote_file_id": f"id-{rel}",
            "synced_at": "t",
            "synced_remote_hash": _md5(content),
        }
        for rel, content in files.items()
    })
    _write_hash_cache(project_dir, files)

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    runner = CliRunner()
    t0 = time.perf_counter()
    result = runner.invoke(cli, ["prompt", "--config", str(cfg_path)])
    cold = time.perf_counter() - t0
    assert result.exit_code == 0, result.output
    assert result.output.strip() == prompt_mod.SYMBOL_OK
    assert cold < 0.5, f"cold-cache prompt took {cold*1000:.0f}ms (>500ms)"


def test_prompt_compute_state_no_local_files_with_manifest(project_dir):
    """Edge case: manifest exists but no local files — should report
    in_sync (nothing to push) without crashing."""
    _write_manifest(project_dir, {})

    cfg_path = project_dir.parent / "cfg.yaml"
    _write_yaml_config(cfg_path, project_dir)

    result = CliRunner().invoke(cli, ["prompt", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == prompt_mod.SYMBOL_OK
