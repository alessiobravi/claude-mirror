"""Tests for the deep S3 checks added to `claude-mirror doctor`.

The generic doctor checks (config/credentials/token/connectivity/project/
manifest) live in test_doctor.py. This module covers the S3-ONLY deep
checks layered on top:

  1. Credentials shape — non-empty access key + secret (when set).
  2. Endpoint URL well-formed (when set).
  3. Bucket reachable (head_bucket).
  4. List permissions (list_objects_v2 MaxKeys=1).
  5. Write permissions (put_object + delete_object sentinel).
  6. Region consistency (warning, non-fatal).

All boto3 calls are mocked — the tests are offline, deterministic, and
well under 100ms each. The single mock seam is `S3Backend._get_client`,
which the deep checker invokes lazily.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner

boto3 = pytest.importorskip("boto3")
botocore = pytest.importorskip("botocore")

from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

import claude_mirror.cli as cli_mod
from claude_mirror.cli import cli

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    from rich.console import Console
    monkeypatch.setattr(cli_mod, "console", Console(force_terminal=True, width=400))


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _write_config(
    path: Path,
    *,
    project_path: Path,
    token_file: Path,
    s3_bucket: str = "mybucket",
    s3_endpoint_url: str = "",
    s3_region: str = "us-east-1",
    s3_access_key_id: str = "AKIA-FAKE",
    s3_secret_access_key: str = "secret-fake",
    s3_prefix: str = "myproject",
    s3_use_path_style: bool = False,
) -> Path:
    data: dict[str, Any] = {
        "project_path": str(project_path),
        "backend": "s3",
        "s3_bucket": s3_bucket,
        "s3_endpoint_url": s3_endpoint_url,
        "s3_region": s3_region,
        "s3_access_key_id": s3_access_key_id,
        "s3_secret_access_key": s3_secret_access_key,
        "s3_prefix": s3_prefix,
        "s3_use_path_style": s3_use_path_style,
        "token_file": str(token_file),
        "credentials_file": str(token_file.parent / "creds.json"),
        "file_patterns": ["**/*.md"],
        "exclude_patterns": [],
        "machine_name": "test-machine",
        "user": "test-user",
    }
    path.write_text(yaml.safe_dump(data))
    return path


def _make_client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "FakeOp",
    )


def _ok_client(region: str = "us-east-1") -> MagicMock:
    """Build a fake boto3 S3 client that succeeds on every call."""
    client = MagicMock()
    client.head_bucket.return_value = {
        "ResponseMetadata": {
            "HTTPStatusCode": 200,
            "HTTPHeaders": {"x-amz-bucket-region": region},
        },
    }
    client.list_objects_v2.return_value = {
        "Contents": [],
        "IsTruncated": False,
    }
    client.put_object.return_value = {
        "ResponseMetadata": {"HTTPStatusCode": 200},
    }
    client.delete_object.return_value = {
        "ResponseMetadata": {"HTTPStatusCode": 204},
    }
    paginator = MagicMock()
    paginator.paginate = MagicMock(return_value=iter([{"Contents": []}]))
    client.get_paginator = MagicMock(return_value=paginator)
    return client


def _patch_s3_client(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Replace S3Backend._get_client to return our fake."""
    from claude_mirror.backends import s3 as s3_mod

    monkeypatch.setattr(
        s3_mod.S3Backend,
        "_get_client",
        lambda self: client,
    )


def _patch_storage_ok(monkeypatch: pytest.MonkeyPatch, client: Any) -> None:
    """Make the generic connectivity probe pass — bypass _create_storage."""

    class _OkStorage:
        backend_name = "s3"

        def authenticate(self) -> Any:
            return self

        def get_credentials(self) -> Any:
            return client

        def list_folders(self, parent_id: str, name: Any = None) -> list:
            return []

        def classify_error(self, exc: BaseException) -> Any:
            from claude_mirror.backends import ErrorClass
            return ErrorClass.UNKNOWN

    monkeypatch.setattr(cli_mod, "_create_storage", lambda config: _OkStorage())


def _build_healthy_config(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text(json.dumps({"verified_at": "2026-05-09T00:00:00Z"}))
    return _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
    )


# ───────────────────────────────────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────────────────────────────────


def test_deep_all_pass_on_fully_configured_s3_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: bucket reachable, list works, write+delete work,
    region matches. Exit 0; output shows every deep-check line as ✓."""
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "S3 credentials present" in out
    assert "Bucket reachable" in out
    assert "List permissions ok" in out
    assert "Write permissions ok" in out
    assert "Region consistency ok" in out
    assert "All checks passed" in out


def test_deep_missing_secret_when_access_key_set_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        s3_access_key_id="AKIA-FAKE",
        s3_secret_access_key="",
    )
    client = _ok_client()
    _patch_storage_ok(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "s3_secret_access_key" in out
    assert "empty" in out


def test_deep_endpoint_url_malformed_fails_at_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        s3_endpoint_url="not-a-url-at-all",
    )
    client = _ok_client()
    _patch_storage_ok(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "s3_endpoint_url" in out
    assert "malformed" in out


def test_deep_bucket_not_found_fails_with_create_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    client.head_bucket.side_effect = _make_client_error("NoSuchBucket", 404)
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "does not exist" in out
    assert "aws s3 mb" in out


def test_deep_invalid_access_key_buckets_into_one_auth_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auth-class failures should bucket — head_bucket reports auth, then
    short-circuits checks 4-6 so the user sees ONE root cause."""
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    client.head_bucket.side_effect = _make_client_error("InvalidAccessKeyId", 403)
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "S3 auth failed" in out
    assert "InvalidAccessKeyId" in out
    # Checks 4-6 should NOT have run (no list/write lines).
    assert "List permissions ok" not in out
    assert "Write permissions ok" not in out


def test_deep_endpoint_unreachable_fails_with_dns_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    client.head_bucket.side_effect = EndpointConnectionError(
        endpoint_url="https://nowhere.example.com"
    )
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "Could not reach S3 endpoint" in out


def test_deep_5xx_on_head_bucket_is_transient_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    client.head_bucket.side_effect = _make_client_error("InternalError", 503)
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "transient" in out.lower()


def test_deep_list_permission_denied_emits_iam_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    client.list_objects_v2.side_effect = _make_client_error("AccessDenied", 403)
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "list permissions denied" in out.lower()
    assert "s3:ListBucket" in out


def test_deep_write_permission_denied_emits_iam_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _build_healthy_config(tmp_path)
    client = _ok_client()
    client.put_object.side_effect = _make_client_error("AccessDenied", 403)
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "write permissions denied" in out.lower()
    assert "s3:PutObject" in out


def test_deep_region_mismatch_is_warning_not_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A region mismatch is an advisory — exit code stays 0 if every
    other check passes."""
    cfg = _build_healthy_config(tmp_path)
    # Configured region is us-east-1 (in healthy config); make the
    # bucket actually live elsewhere.
    client = _ok_client(region="eu-central-1")
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.output)
    assert "Region mismatch" in out
    assert "us-east-1" in out
    assert "eu-central-1" in out


def test_deep_no_credentials_at_all_emits_chain_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both keys blank → check 1 reports "using boto3's default chain".
    If head_bucket then raises NoCredentialsError, the auth bucket fires."""
    project = tmp_path / "project"
    project.mkdir()
    token = tmp_path / "token.json"
    token.write_text("{}")
    cfg = _write_config(
        tmp_path / "config.yaml",
        project_path=project,
        token_file=token,
        s3_access_key_id="",
        s3_secret_access_key="",
    )
    client = _ok_client()
    client.head_bucket.side_effect = NoCredentialsError()
    _patch_storage_ok(monkeypatch, client)
    _patch_s3_client(monkeypatch, client)

    result = CliRunner().invoke(
        cli, ["doctor", "--config", str(cfg), "--backend", "s3"]
    )
    assert result.exit_code == 1, result.output
    out = _strip_ansi(result.output)
    assert "default" in out.lower()  # check 1 mentions default chain
    assert "credentials not found" in out.lower()
