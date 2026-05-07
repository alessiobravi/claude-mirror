"""Regression tests for narrowed `except Exception` clauses on load paths.

The audit (Task O1) found six sites that swallowed `Exception` while
reading state files or making best-effort calls. That mask hid real
coding bugs (AttributeError, TypeError) as "feature silently does
nothing". These tests pin the narrowed behaviour: legitimate failure
modes (corrupt JSON, network errors, missing files) still no-op, but
programming bugs propagate.
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# hash_cache.HashCache._load
# ---------------------------------------------------------------------------

def test_hash_cache_returns_empty_on_corrupt_json(tmp_path: Path) -> None:
    """A malformed cache file should be treated as 'no cache yet'."""
    from claude_mirror.hash_cache import HashCache, CACHE_FILE
    (tmp_path / CACHE_FILE).write_text("{not valid json")
    cache = HashCache(str(tmp_path))
    # Empty dict in, empty dict out — and no exception raised.
    assert cache.get("anything", 0, 0) is None


def test_hash_cache_propagates_attribute_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coding bug surfaced as AttributeError in json.loads must NOT
    be silently swallowed by HashCache._load. This is the core
    regression for the original bare `except Exception` problem."""
    from claude_mirror import hash_cache
    (tmp_path / hash_cache.CACHE_FILE).write_text('{"a": [1, 2, "abc"]}')

    def boom(_text):
        raise AttributeError("simulated coding bug in json parse")

    monkeypatch.setattr(hash_cache.json, "loads", boom)

    with pytest.raises(AttributeError, match="simulated coding bug"):
        hash_cache.HashCache(str(tmp_path))


# ---------------------------------------------------------------------------
# manifest.Manifest._is_safe_relpath
# ---------------------------------------------------------------------------

def test_manifest_load_propagates_programming_bugs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TypeError raised inside the path-validation try block must
    propagate, not be swallowed as 'unsafe path → False'."""
    from claude_mirror import manifest as manifest_mod

    real_path = manifest_mod.Path

    class ExplodingPath(real_path):  # type: ignore[misc,valid-type]
        # Path is a meta-class fest; subclassing for a TypeError sim
        # requires we override __new__ to raise on construction.
        def __new__(cls, *a, **kw):
            raise TypeError("simulated coding bug in Path()")

    monkeypatch.setattr(manifest_mod, "Path", ExplodingPath)

    with pytest.raises(TypeError, match="simulated coding bug"):
        manifest_mod.Manifest._is_safe_relpath("some/legit/path.md")


# ---------------------------------------------------------------------------
# _update_check.check_for_update / _load_cache
# ---------------------------------------------------------------------------

def test_update_check_silent_on_network_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A URLError from the urllib request must result in a clean no-op:
    the foreground command must not see a traceback."""
    from claude_mirror import _update_check as uc

    # Redirect cache file into tmp_path so we don't touch the real one.
    monkeypatch.setattr(uc, "_CACHE_FILE", tmp_path / ".update_check.json")
    # Force the disabled-flag off so check actually runs.
    monkeypatch.delenv("CLAUDE_MIRROR_NO_UPDATE_CHECK", raising=False)

    def raise_urlerror(*_a, **_kw):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(uc.urllib.request, "urlopen", raise_urlerror)

    # _fetch_via_api / _fetch_via_raw_with_busting both catch URLError
    # and return None; force_check_now should observe None and return
    # None without raising.
    result = uc.force_check_now()
    assert result is None


def test_update_check_load_cache_propagates_attribute_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A coding bug in the cache-load path (AttributeError) must
    propagate rather than masquerade as 'no cache available'."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    cache_file.write_text('{"latest_version": "9.9.9"}')
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)

    def boom(_text):
        raise AttributeError("simulated coding bug in cache parse")

    monkeypatch.setattr(uc.json, "loads", boom)

    with pytest.raises(AttributeError, match="simulated coding bug"):
        uc._load_cache()


def test_update_check_load_cache_silent_on_corrupt_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSONDecodeError on a corrupt cache file is a legitimate failure
    mode and must yield {} silently."""
    from claude_mirror import _update_check as uc

    cache_file = tmp_path / ".update_check.json"
    cache_file.write_text("{not json")
    monkeypatch.setattr(uc, "_CACHE_FILE", cache_file)

    assert uc._load_cache() == {}
