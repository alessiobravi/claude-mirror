"""Tests for `claude-mirror sync --no-prompt --strategy {keep-local,keep-remote}`
— the unattended / cron-friendly conflict-resolution path shipped in v0.5.49.

Coverage:
    1. Auto-resolve keep-local: 2-way conflict → local content wins, remote
       overwritten with local bytes.
    2. Auto-resolve keep-remote: 2-way conflict → remote wins, local file
       overwritten with remote bytes.
    3. CLI: `--no-prompt` without `--strategy` exits 1 with the clean
       error message.
    4. CLI: `--strategy` without `--no-prompt` prints a yellow info line
       and proceeds with the interactive flow.
    5. Local-only file with `--strategy keep-remote`: file stays as
       NEW_LOCAL, gets pushed normally — no spurious "deleted" action.
    6. Remote-only file with `--strategy keep-local`: file stays as
       NEW_DRIVE, gets pulled normally — no spurious "delete" action.
    7. Audit-log SyncEvent carries `auto_resolved_files: [{path, strategy}]`
       so audits can spot every auto-resolution.
    8. Multiple conflicts in one run: all auto-resolved, summary count
       matches.
    9. Trailing summary line appears even with zero conflicts.
   10. Non-tty stdin without `--no-prompt`: fail-fast with a hint
       pointing at `--no-prompt --strategy`.
   11. Interactive path stays green: existing `MergeHandler()` no-arg
       construction continues to use `click.prompt`.

Every test is offline (in-memory backend + monkeypatched MergeHandler /
notifier), runs in <100ms, and never touches the user's
`~/.config/claude_mirror/` tree.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any, Callable, Optional

import pytest
from click.testing import CliRunner

# Click 8.3 emits a DeprecationWarning for Context.protected_args from
# inside CliRunner.invoke; pyproject's filterwarnings = "error" otherwise
# turns that into a test failure for any test that exercises the CLI.
# Same workaround as `test_DIAG043_streams.py`.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

from claude_mirror.backends import ErrorClass, StorageBackend
from claude_mirror.events import SyncEvent
from claude_mirror.manifest import Manifest
from claude_mirror.merge import (
    MergeHandler,
    NON_INTERACTIVE_STRATEGIES,
    STRATEGY_KEEP_LOCAL,
    STRATEGY_KEEP_REMOTE,
)
from claude_mirror.sync import FileSyncState, Status, SyncEngine


# ---------------------------------------------------------------------------
# In-memory backend (mirrors the one in test_sync_engine.py).
# ---------------------------------------------------------------------------

class _InMemoryBackend(StorageBackend):
    backend_name = "fake"

    def __init__(self) -> None:
        self._files: dict[str, dict] = {}
        self._next_id: int = 0
        self.calls: list[tuple] = []

    def seed(self, rel_path: str, content: bytes) -> str:
        fid = self._mint_id()
        name = rel_path.rsplit("/", 1)[-1]
        self._files[fid] = {"name": name, "rel_path": rel_path, "content": content}
        return fid

    def _mint_id(self) -> str:
        self._next_id += 1
        return f"fid-{self._next_id}"

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        return f"folder-{name}"

    def resolve_path(self, rel_path: str, root_folder_id: str) -> tuple[str, str]:
        return root_folder_id, rel_path.rsplit("/", 1)[-1]

    def list_files_recursive(
        self,
        folder_id: str,
        prefix: str = "",
        progress_cb: Optional[Callable[[int, int], None]] = None,
        exclude_folder_names: Optional[set[str]] = None,
    ) -> list[dict]:
        out = []
        for fid, meta in self._files.items():
            md5 = hashlib.md5(meta["content"]).hexdigest()
            out.append({
                "id": fid,
                "name": meta["name"],
                "md5Checksum": md5,
                "relative_path": meta["rel_path"],
                "size": len(meta["content"]),
            })
        return out

    def list_folders(self, parent_id: str, name: Optional[str] = None) -> list[dict]:
        return []

    def upload_file(
        self,
        local_path: str,
        rel_path: str,
        root_folder_id: str,
        file_id: Optional[str] = None,
    ) -> str:
        self.calls.append(("upload_file", rel_path, file_id))
        with open(local_path, "rb") as f:
            content = f.read()
        if file_id and file_id in self._files:
            self._files[file_id]["content"] = content
            self._files[file_id]["rel_path"] = rel_path
            return file_id
        fid = self._mint_id()
        name = rel_path.rsplit("/", 1)[-1]
        self._files[fid] = {"name": name, "rel_path": rel_path, "content": content}
        return fid

    def download_file(self, file_id: str) -> bytes:
        return self._files[file_id]["content"]

    def upload_bytes(
        self,
        content: bytes,
        name: str,
        folder_id: str,
        file_id: Optional[str] = None,
        mimetype: str = "application/json",
    ) -> str:
        if file_id and file_id in self._files:
            self._files[file_id]["content"] = content
            return file_id
        fid = self._mint_id()
        self._files[fid] = {"name": name, "rel_path": name, "content": content}
        return fid

    def get_file_id(self, name: str, folder_id: str) -> Optional[str]:
        for fid, meta in self._files.items():
            if meta["name"] == name:
                return fid
        return None

    def copy_file(self, source_file_id: str, dest_folder_id: str, name: str) -> str:
        return self._mint_id()

    def get_file_hash(self, file_id: str) -> Optional[str]:
        meta = self._files.get(file_id)
        if meta is None:
            return None
        return hashlib.md5(meta["content"]).hexdigest()

    def delete_file(self, file_id: str) -> None:
        self.calls.append(("delete_file", file_id))
        self._files.pop(file_id, None)

    def classify_error(self, exc: BaseException) -> ErrorClass:
        return ErrorClass.UNKNOWN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


def _build_engine(
    config,
    backend: _InMemoryBackend,
    merger: MergeHandler,
) -> SyncEngine:
    manifest = Manifest(config.project_path)
    return SyncEngine(
        config=config,
        storage=backend,
        manifest=manifest,
        merge=merger,
        notifier=None,
        snapshots=None,
        mirrors=[],
    )


def _setup_conflict(make_config, backend, write_files):
    """A 2-way conflict: both sides differ from the manifest baseline."""
    write_files({"a.md": "LOCAL"})
    fid = backend.seed("a.md", b"DRIVE")
    cfg = make_config()
    h = _md5(b"BASELINE")
    m = Manifest(cfg.project_path)
    m.update("a.md", h, fid, synced_remote_hash=h, backend_name="fake")
    m.save()
    return cfg, fid


# ===========================================================================
# 1. keep-local: 2-way conflict → local wins, remote overwritten.
# ===========================================================================

def test_no_prompt_keep_local_overwrites_remote(make_config, write_files):
    backend = _InMemoryBackend()
    cfg, fid = _setup_conflict(make_config, backend, write_files)

    handler = MergeHandler(non_interactive_strategy=STRATEGY_KEEP_LOCAL)
    eng = _build_engine(cfg, backend, handler)
    result = eng.sync(non_interactive_strategy=STRATEGY_KEEP_LOCAL)

    # Remote overwritten with the local bytes.
    assert backend._files[fid]["content"] == b"LOCAL"
    # The audit trail captures path + strategy.
    assert result["auto_resolved"] == [
        {"path": "a.md", "strategy": "keep-local"},
    ]
    # Local file untouched (it WAS the winner).
    assert (Path(cfg.project_path) / "a.md").read_text() == "LOCAL"


# ===========================================================================
# 2. keep-remote: 2-way conflict → remote wins, local overwritten.
# ===========================================================================

def test_no_prompt_keep_remote_overwrites_local(make_config, write_files):
    backend = _InMemoryBackend()
    cfg, fid = _setup_conflict(make_config, backend, write_files)

    handler = MergeHandler(non_interactive_strategy=STRATEGY_KEEP_REMOTE)
    eng = _build_engine(cfg, backend, handler)
    result = eng.sync(non_interactive_strategy=STRATEGY_KEEP_REMOTE)

    # Local overwritten with the remote bytes.
    assert (Path(cfg.project_path) / "a.md").read_bytes() == b"DRIVE"
    # Remote untouched.
    assert backend._files[fid]["content"] == b"DRIVE"
    # No upload happened — the remote was authoritative.
    uploads = [c for c in backend.calls if c[0] == "upload_file"]
    assert uploads == []
    # Audit log captures path + strategy.
    assert result["auto_resolved"] == [
        {"path": "a.md", "strategy": "keep-remote"},
    ]


# ===========================================================================
# 3. CLI: `--no-prompt` without `--strategy` exits 1 with a clean message.
# ===========================================================================

def test_cli_no_prompt_without_strategy_exits_1(tmp_path, monkeypatch):
    """Without --strategy, --no-prompt has no defined behaviour. Fail fast
    so the operator gets an actionable error message — not a silent
    fallback to interactive that would hang under cron."""
    from claude_mirror.cli import cli

    # _resolve_config will call cwd lookup; redirect to a place with no
    # config so the command fails BEFORE the engine even starts (we only
    # care that the validation runs first).
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["sync", "--no-prompt", "--config", str(tmp_path / "missing.yaml")])

    assert result.exit_code == 1, result.output
    # The error message names both flags so the operator can fix it.
    assert "--no-prompt requires --strategy" in result.output
    assert "keep-local" in result.output
    assert "keep-remote" in result.output


# ===========================================================================
# 4. CLI: `--strategy` without `--no-prompt` warns + falls back to
#    interactive flow (which fails fast on non-tty stdin).
# ===========================================================================

def test_cli_strategy_without_no_prompt_warns_and_continues(tmp_path, monkeypatch):
    """`--strategy keep-local` alone is meaningless — print the yellow
    warning then continue. Under CliRunner stdin is non-tty so the
    interactive flow fails-fast with the cron hint; the test asserts
    BOTH messages so a regression that drops either gets caught."""
    from claude_mirror.cli import cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["sync", "--strategy", "keep-local", "--config", str(tmp_path / "missing.yaml")],
    )

    # The yellow warning was emitted...
    assert "--strategy ignored without --no-prompt" in result.output
    # ...and then the non-tty fail-fast hint kicked in.
    assert "needs an interactive terminal" in result.output
    assert "--no-prompt --strategy" in result.output


# ===========================================================================
# 5. Local-only file with `--strategy keep-remote`: no spurious "deleted".
# ===========================================================================

def test_keep_remote_does_not_delete_local_only_file(make_config, write_files):
    """A NEW_LOCAL file (only on local, no remote counterpart, no manifest
    entry) is not a 2-way conflict — there's nothing to "resolve". The
    strategy must be ignored for this state and the file must be pushed
    normally; no delete_file call should fire."""
    backend = _InMemoryBackend()
    write_files({"local-only.md": "fresh local content"})
    cfg = make_config()

    handler = MergeHandler(non_interactive_strategy=STRATEGY_KEEP_REMOTE)
    eng = _build_engine(cfg, backend, handler)
    result = eng.sync(non_interactive_strategy=STRATEGY_KEEP_REMOTE)

    # The file was pushed (NEW_LOCAL → push), not deleted.
    assert "local-only.md" in result["pushed"]
    # No conflict ever entered the resolver.
    assert result["auto_resolved"] == []
    # Local file still present.
    assert (Path(cfg.project_path) / "local-only.md").exists()
    # No delete call against the remote.
    deletes = [c for c in backend.calls if c[0] == "delete_file"]
    assert deletes == []


# ===========================================================================
# 6. Remote-only file with `--strategy keep-local`: pulled normally.
# ===========================================================================

def test_keep_local_pulls_remote_only_file(make_config, write_files):
    """A NEW_DRIVE file (only on remote, no local file, no manifest
    entry) is not a 2-way conflict either. keep-local must NOT mean
    "delete it from the remote because there's no local"; the file
    must be pulled normally and the auto_resolved list must stay empty."""
    backend = _InMemoryBackend()
    backend.seed("remote-only.md", b"only on remote")
    # write_files needs at least one call to materialise project_dir; we
    # invoke it with an empty dict to get the project path back.
    write_files({})
    cfg = make_config()

    handler = MergeHandler(non_interactive_strategy=STRATEGY_KEEP_LOCAL)
    eng = _build_engine(cfg, backend, handler)
    result = eng.sync(non_interactive_strategy=STRATEGY_KEEP_LOCAL)

    # The file was pulled, not auto-resolved as a conflict.
    assert "remote-only.md" in result["pulled"]
    assert result["auto_resolved"] == []
    # Local copy now exists.
    assert (Path(cfg.project_path) / "remote-only.md").read_bytes() == b"only on remote"
    # The remote file is still there (no delete).
    deletes = [c for c in backend.calls if c[0] == "delete_file"]
    assert deletes == []


# ===========================================================================
# 7. Audit-log SyncEvent carries the auto_resolved_files audit trail.
# ===========================================================================

def test_sync_event_carries_auto_resolved_audit_trail():
    """The SyncEvent dataclass gained an `auto_resolved_files` field in
    v0.5.49 so cron-resolved conflicts can be spotted in `_sync_log.json`
    after the fact. The field round-trips through to_json / from_json
    intact, and is empty by default for backwards compatibility with
    every push/pull/delete event and with every interactive sync run."""
    # Default: empty list, never None — keeps log readers from having
    # to special-case an absent field.
    plain = SyncEvent.now(
        machine="m", user="u", files=["a.md"], action="sync", project="p",
    )
    assert plain.auto_resolved_files == []

    # With explicit audit trail: round-trips intact.
    rich = SyncEvent.now(
        machine="m", user="u", files=["a.md"], action="sync", project="p",
        auto_resolved_files=[
            {"path": "a.md", "strategy": "keep-local"},
            {"path": "b.md", "strategy": "keep-local"},
        ],
    )
    revived = SyncEvent.from_json(rich.to_json())
    assert revived.auto_resolved_files == [
        {"path": "a.md", "strategy": "keep-local"},
        {"path": "b.md", "strategy": "keep-local"},
    ]


# ===========================================================================
# 8. Multiple conflicts in one run: all auto-resolved.
# ===========================================================================

def test_multiple_conflicts_all_auto_resolved(make_config, write_files):
    backend = _InMemoryBackend()
    write_files({"a.md": "LOCAL_A", "b.md": "LOCAL_B", "c.md": "LOCAL_C"})
    fid_a = backend.seed("a.md", b"DRIVE_A")
    fid_b = backend.seed("b.md", b"DRIVE_B")
    fid_c = backend.seed("c.md", b"DRIVE_C")
    cfg = make_config()
    # Manifest pretends every file's baseline differs from BOTH sides.
    m = Manifest(cfg.project_path)
    h = _md5(b"BASELINE")
    m.update("a.md", h, fid_a, synced_remote_hash=h, backend_name="fake")
    m.update("b.md", h, fid_b, synced_remote_hash=h, backend_name="fake")
    m.update("c.md", h, fid_c, synced_remote_hash=h, backend_name="fake")
    m.save()

    handler = MergeHandler(non_interactive_strategy=STRATEGY_KEEP_LOCAL)
    eng = _build_engine(cfg, backend, handler)
    result = eng.sync(non_interactive_strategy=STRATEGY_KEEP_LOCAL)

    # Every conflict resolved, every file's audit trail captured.
    paths = sorted(e["path"] for e in result["auto_resolved"])
    assert paths == ["a.md", "b.md", "c.md"]
    assert all(e["strategy"] == "keep-local" for e in result["auto_resolved"])
    # Every remote bytes-string flipped to the local content.
    assert backend._files[fid_a]["content"] == b"LOCAL_A"
    assert backend._files[fid_b]["content"] == b"LOCAL_B"
    assert backend._files[fid_c]["content"] == b"LOCAL_C"


# ===========================================================================
# 9. Trailing summary line appears even with zero conflicts.
# ===========================================================================

def test_summary_line_with_zero_conflicts(make_config, write_files, monkeypatch):
    """The cron operator gets ONE grep-friendly summary line at the end
    of every `--no-prompt` run, even if the sync was a no-op. Without
    this guarantee, parsing cron mail to confirm "the sync succeeded"
    becomes "absence of error" — fragile."""
    from claude_mirror.cli import cli
    from click.testing import CliRunner

    # Build a config that points at a tmp project + no remote drift.
    backend = _InMemoryBackend()
    cfg = make_config()
    Path(cfg.project_path).mkdir(parents=True, exist_ok=True)

    # Patch _load_engine to skip real backend wiring and return our
    # in-memory engine. The CLI's mutual-exclusion validation runs
    # BEFORE _load_engine is called, which is what we're testing here.
    handler = MergeHandler(non_interactive_strategy=STRATEGY_KEEP_LOCAL)
    eng = _build_engine(cfg, backend, handler)

    import claude_mirror.cli as cli_mod

    def fake_load_engine(config_path, with_pubsub=True, *, non_interactive_strategy=None):
        # Re-build merge with the CLI-supplied strategy so the engine's
        # self.merge.non_interactive_strategy lines up with the kwarg
        # the CLI passes to engine.sync().
        eng.merge = MergeHandler(non_interactive_strategy=non_interactive_strategy)
        return eng, cfg, backend

    monkeypatch.setattr(cli_mod, "_load_engine", fake_load_engine)
    # Resolve_config is also called — bypass to avoid the cwd walk-up.
    monkeypatch.setattr(cli_mod, "_resolve_config", lambda _: "")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["sync", "--no-prompt", "--strategy", "keep-local"],
    )

    assert result.exit_code == 0, result.output
    # Strip ANSI markup so Rich's auto-cyan number styling doesn't make
    # naive substring matches brittle (the number lives in its own SGR
    # group, separated from "conflicts." by reset escapes).
    import re as _re
    plain = _re.sub(r"\x1b\[[0-9;]*m", "", result.output)
    # The summary line is present even though nothing happened.
    assert "Summary:" in plain
    assert "0 conflicts" in plain
    assert "0 in sync" in plain
    assert "0 pushed" in plain
    assert "0 pulled" in plain


# ===========================================================================
# 10. Non-tty stdin without `--no-prompt` fails fast with the cron hint.
# ===========================================================================

def test_non_tty_without_no_prompt_fails_fast_with_hint(tmp_path, monkeypatch):
    """The cron / launchd / systemd flow has no stdin to prompt against.
    If the user runs `sync` (no flags) under cron, the interactive
    prompt would hang forever — fail-fast at command entry instead and
    point the operator at the right flag combination."""
    from claude_mirror.cli import cli

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    # CliRunner's stdin is a StringIO, so `sys.stdin.isatty()` returns
    # False naturally — no extra patching needed.
    result = runner.invoke(cli, ["sync"])

    assert result.exit_code == 1, result.output
    assert "needs an interactive terminal" in result.output
    # The hint names BOTH flags + both choices so the operator can
    # immediately fix the cron entry.
    assert "--no-prompt --strategy" in result.output


# ===========================================================================
# 11. Interactive path stays green: existing MergeHandler() unchanged.
# ===========================================================================

def test_interactive_handler_default_still_prompts(monkeypatch):
    """Backward compatibility: `MergeHandler()` with no kwargs MUST
    continue to use `click.prompt`. This pins the contract so a future
    refactor that flips the default to non-interactive gets caught."""
    from claude_mirror import merge as merge_mod

    calls: list[str] = []

    def fake_prompt(*args, **kwargs):
        calls.append("prompt")
        return "L"

    monkeypatch.setattr(merge_mod.click, "prompt", fake_prompt)

    handler = MergeHandler()  # no kwargs — the contract under test
    assert handler.non_interactive_strategy is None
    result = handler.resolve_conflict("CLAUDE.md", "L_content", "D_content")
    assert calls == ["prompt"]
    assert result == ("L_content", "local")


# ===========================================================================
# 12. Defensive: invalid strategy at construction time → ValueError.
# ===========================================================================

def test_invalid_strategy_construction_fails_fast():
    """A typo in `non_interactive_strategy=` (e.g. "keep_local" with
    underscore instead of "keep-local") would silently fall through to
    the interactive path under cron and hang. Reject it at __init__ so
    the operator sees the real error immediately."""
    with pytest.raises(ValueError, match="non_interactive_strategy"):
        MergeHandler(non_interactive_strategy="keep_local")  # underscore typo

    # The valid choices are still accepted.
    assert MergeHandler(non_interactive_strategy="keep-local")
    assert MergeHandler(non_interactive_strategy="keep-remote")
    # And the constants list is not empty.
    assert set(NON_INTERACTIVE_STRATEGIES) == {"keep-local", "keep-remote"}
