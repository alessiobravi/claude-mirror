"""Tests for the layered update-check source chain.

claude-mirror queries three sources, in order, when looking up the
latest released version:

    1. PyPI JSON API     (most authoritative — the only source that
                          knows whether a release is *installable*)
    2. GitHub Contents API
    3. raw.githubusercontent.com (CDN fallback)

These tests pin the chain ordering: each fallback only runs when the
prior raised. The successful source name is recorded in the cache as
`last_source` for diagnostic visibility.

All tests are offline (urllib.request.urlopen is patched) and must be
deterministic — no real network, no real filesystem outside tmp_path.
"""
from __future__ import annotations

import base64
import io
import json
import urllib.error
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pypi_response_body(version: str) -> bytes:
    """Build a minimal PyPI JSON response with the given `info.version`."""
    return json.dumps({"info": {"version": version}}).encode("utf-8")


def _github_api_response_body(pyproject_version: str) -> bytes:
    """Build a minimal GitHub Contents API response embedding a pyproject.toml
    whose `version = "..."` line matches the supplied version."""
    pyproject = (
        "[project]\n"
        'name = "claude-mirror"\n'
        f'version = "{pyproject_version}"\n'
    )
    encoded = base64.b64encode(pyproject.encode("utf-8")).decode("ascii")
    return json.dumps({"content": encoded, "encoding": "base64"}).encode("utf-8")


def _raw_cdn_response_body(pyproject_version: str) -> bytes:
    """Build a raw pyproject.toml body matching the supplied version."""
    return (
        "[project]\n"
        'name = "claude-mirror"\n'
        f'version = "{pyproject_version}"\n'
    ).encode("utf-8")


class _FakeResp:
    """Minimal stand-in for the object returned by `urllib.request.urlopen()`.

    Supports the context-manager protocol and `.read()` — the only
    surface area `_update_check` actually uses.
    """

    def __init__(self, body: bytes) -> None:
        self._buf = io.BytesIO(body)

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_exc) -> None:
        self._buf.close()

    def read(self) -> bytes:
        return self._buf.read()


def _make_router(routes: dict) -> "callable":
    """Build a fake `urlopen(req, timeout=...)` whose behaviour depends on
    the request URL. `routes` maps URL-prefix → either bytes (success body)
    or an exception instance (raised on call).

    The first prefix that matches `req.full_url` wins. Unmatched URLs
    raise URLError so a forgotten route surfaces clearly in the test.
    """
    def fake_urlopen(req, timeout=None):  # noqa: ARG001 — signature matches stdlib
        url = getattr(req, "full_url", None) or req.get_full_url()
        for prefix, outcome in routes.items():
            if url.startswith(prefix):
                if isinstance(outcome, BaseException):
                    raise outcome
                return _FakeResp(outcome)
        raise urllib.error.URLError(f"no route for {url}")
    return fake_urlopen


# ---------------------------------------------------------------------------
# Source chain ordering
# ---------------------------------------------------------------------------

def test_pypi_primary_returns_version_and_records_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PyPI is reachable, its version wins and the cache records
    `last_source: pypi` — neither GitHub source is even consulted."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("CLAUDE_MIRROR_NO_UPDATE_CHECK", raising=False)

    routes = {
        "https://pypi.org/pypi/claude-mirror/json": _pypi_response_body("0.5.38"),
        # Defensive: if we somehow fall through, both fallbacks return
        # a different version so the assertion below would fail loudly.
        "https://api.github.com/": _github_api_response_body("9.9.9"),
        "https://raw.githubusercontent.com/": _raw_cdn_response_body("9.9.9"),
    }
    with patch.object(uc.urllib.request, "urlopen", _make_router(routes)):
        result = uc.force_check_now()

    assert result == "0.5.38"
    cache = json.loads(cache_file.read_text())
    assert cache["latest_version"] == "0.5.38"
    assert cache["last_source"] == "pypi"


def test_pypi_failure_falls_back_to_github_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When PyPI raises, the GitHub Contents API is queried next; its
    version is reported and the source is recorded as `github_api`."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("CLAUDE_MIRROR_NO_UPDATE_CHECK", raising=False)

    routes = {
        "https://pypi.org/": urllib.error.URLError("pypi unreachable"),
        "https://api.github.com/": _github_api_response_body("0.5.38"),
        "https://raw.githubusercontent.com/": _raw_cdn_response_body("9.9.9"),
    }
    with patch.object(uc.urllib.request, "urlopen", _make_router(routes)):
        result = uc.force_check_now()

    assert result == "0.5.38"
    cache = json.loads(cache_file.read_text())
    assert cache["latest_version"] == "0.5.38"
    assert cache["last_source"] == "github_api"


def test_pypi_and_api_failure_falls_back_to_raw_cdn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both PyPI and the GitHub API raise, the raw CDN is the last
    line of defence; its version is reported and `last_source` is
    `raw_cdn`."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("CLAUDE_MIRROR_NO_UPDATE_CHECK", raising=False)

    routes = {
        "https://pypi.org/": urllib.error.URLError("pypi unreachable"),
        "https://api.github.com/": urllib.error.URLError("api unreachable"),
        "https://raw.githubusercontent.com/": _raw_cdn_response_body("0.5.38"),
    }
    with patch.object(uc.urllib.request, "urlopen", _make_router(routes)):
        result = uc.force_check_now()

    assert result == "0.5.38"
    cache = json.loads(cache_file.read_text())
    assert cache["latest_version"] == "0.5.38"
    assert cache["last_source"] == "raw_cdn"


def test_all_sources_fail_returns_none_and_does_not_cache_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If every source raises, force_check_now returns None and
    latest_version is NOT written into the cache."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("CLAUDE_MIRROR_NO_UPDATE_CHECK", raising=False)

    def always_fail(*_a, **_kw):
        raise urllib.error.URLError("offline")

    with patch.object(uc.urllib.request, "urlopen", always_fail):
        result = uc.force_check_now()

    assert result is None
    # Cache file may not exist at all; if it does, no version was written.
    if cache_file.exists():
        cache = json.loads(cache_file.read_text())
        assert "latest_version" not in cache
        assert "last_source" not in cache


def test_pypi_malformed_json_falls_through_to_next_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defensive: malformed PyPI JSON shouldn't crash — the chain should
    move on to the next source."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)
    monkeypatch.delenv("CLAUDE_MIRROR_NO_UPDATE_CHECK", raising=False)

    routes = {
        "https://pypi.org/": b"{not valid json",
        "https://api.github.com/": _github_api_response_body("0.5.38"),
        "https://raw.githubusercontent.com/": _raw_cdn_response_body("9.9.9"),
    }
    with patch.object(uc.urllib.request, "urlopen", _make_router(routes)):
        result = uc.force_check_now()

    assert result == "0.5.38"
    cache = json.loads(cache_file.read_text())
    assert cache["last_source"] == "github_api"
