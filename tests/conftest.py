"""Shared pytest fixtures for the claude-mirror test suite.

Conventions:
    * Tests must NOT touch the user's real ~/.config/claude_mirror/ tree —
      every test uses tmp_path or one of the fixtures below for isolation.
    * Tests must NOT make real network calls. Use the mock_backend fixture
      or monkeypatch the relevant requests/google-api modules.
    * Tests must NOT hit real cloud storage (Drive/Dropbox/OneDrive/WebDAV).
      Backend tests use a local in-memory fake.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from claude_mirror.config import Config


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Empty project directory inside the test's tmp_path."""
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Empty config dir inside the test's tmp_path. Use to host token /
    credentials files without touching ~/.config/claude_mirror/."""
    d = tmp_path / "config"
    d.mkdir()
    return d


@pytest.fixture
def make_config(project_dir: Path, config_dir: Path):
    """Factory: returns a callable that builds a Config dataclass with
    sensible defaults pointing at the temp project + config dirs.

    Usage:
        def test_something(make_config):
            cfg = make_config(file_patterns=["**/*.md"])
            assert cfg.project_path.endswith("/project")
    """
    def _make(**overrides: Any) -> Config:
        defaults: Dict[str, Any] = {
            "project_path": str(project_dir),
            "backend": "googledrive",
            "drive_folder_id": "test-folder-id",
            "credentials_file": str(config_dir / "credentials.json"),
            "token_file": str(config_dir / "token.json"),
            "file_patterns": ["**/*.md"],
            "exclude_patterns": [],
            "machine_name": "test-machine",
            "user": "test-user",
        }
        defaults.update(overrides)
        # Drop unknown keys gracefully — some fields only exist in newer Configs
        valid = {k: v for k, v in defaults.items() if k in Config.__dataclass_fields__}
        return Config(**valid)
    return _make


@pytest.fixture
def write_files(project_dir: Path):
    """Factory: write a dict of {rel_path: content} into project_dir.

    Returns the project_dir for chaining.
    """
    def _write(files: Dict[str, str]) -> Path:
        for rel, content in files.items():
            full = project_dir / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
        return project_dir
    return _write


class FakeStorageBackend:
    """In-memory storage for backend-shape tests. Tracks uploads/downloads/
    deletes so tests can assert on call patterns without hitting cloud."""

    backend_name = "fake"

    def __init__(self) -> None:
        self.files: Dict[str, bytes] = {}      # path → content
        self.folders: Dict[str, str] = {}      # name → fake-folder-id
        self.upload_calls: List[tuple] = []
        self.download_calls: List[str] = []
        self.delete_calls: List[str] = []

    def authenticate(self) -> Any:
        return self

    def get_credentials(self) -> Any:
        return self

    def get_or_create_folder(self, name: str, parent_id: str) -> str:
        key = f"{parent_id}/{name}"
        if key not in self.folders:
            self.folders[key] = f"fakeid-{len(self.folders)}"
        return self.folders[key]

    def upload_file(self, local_path: str, name: str, parent_id: str, **_kw) -> str:
        self.upload_calls.append((local_path, name, parent_id))
        with open(local_path, "rb") as f:
            self.files[f"{parent_id}/{name}"] = f.read()
        return f"fakeid-up-{len(self.upload_calls)}"

    def download_file(self, file_id: str) -> bytes:
        self.download_calls.append(file_id)
        return b""

    def delete_file(self, file_id: str) -> None:
        self.delete_calls.append(file_id)


@pytest.fixture
def fake_backend() -> FakeStorageBackend:
    """A reusable in-memory backend for tests that need a backend but don't
    care about real cloud semantics."""
    return FakeStorageBackend()


@pytest.fixture
def manifest_path(project_dir: Path) -> Path:
    """Path where a project's manifest would live, but doesn't exist yet."""
    return project_dir / ".claude_mirror_manifest.json"


@pytest.fixture
def write_manifest(manifest_path: Path):
    """Factory: write a manifest dict to disk."""
    def _write(data: Dict[str, Any]) -> Path:
        manifest_path.write_text(json.dumps(data))
        return manifest_path
    return _write
