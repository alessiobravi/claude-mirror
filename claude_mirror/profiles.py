"""Credentials-profile registry (since v0.5.49).

A *profile* is a YAML file under `~/.config/claude_mirror/profiles/<name>.yaml`
that holds the identity-bearing fields for one logical "account" — e.g. one
Google account, one Dropbox app, one Azure AD app — so multiple project
configs can reference it by name instead of duplicating
`credentials_file` / `token_file` / `dropbox_app_key` / `onedrive_client_id` /
WebDAV credentials / SFTP host info across every project YAML.

Two pure functions live here so they're easy to unit test in isolation
from the Click layer:

    load_profile(name)                     -> dict
    apply_profile(profile, project_config) -> dict

Resolution rule:
    PROJECT WINS over PROFILE. Any field set in the project YAML overrides
    the profile's value. The profile is the *default*; the project YAML is
    the *escape hatch*. This keeps the mental model simple:

        merged[k] = project[k] if k in project and project[k] truthy
                    else profile.get(k, project.get(k))

    "Truthy" is the right test rather than "key present" because the
    config dataclass's defaults (empty string, 0, False, None, []) always
    appear in the dict that comes out of `Config.load -> asdict`. A
    project YAML that omits `credentials_file` materialises as `""`
    after dataclass `__post_init__` runs — we want the profile to win
    in that case.

The `PROFILES_DIR` constant points at `<CONFIG_DIR>/profiles/` so the
test suite can monkeypatch a single anchor (`CONFIG_DIR`) and have both
the profile loader and the project-config loader follow the redirect.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_DIR


# Subdirectory under CONFIG_DIR that holds one YAML per profile.
# Resolved lazily inside `_profiles_dir()` so test fixtures that
# monkeypatch `claude_mirror.config.CONFIG_DIR` are picked up at call
# time rather than at import time.
PROFILES_SUBDIR = "profiles"


def _profiles_dir() -> Path:
    """Return the directory holding profile YAMLs.

    Resolved at call time so a test monkeypatching `CONFIG_DIR` is honoured.
    """
    # Re-import each call: monkeypatch.setattr replaces the module
    # attribute, but the local symbol imported above was bound once at
    # import time. Reading via the module ensures we follow the patch.
    from . import config as _config_module
    return Path(_config_module.CONFIG_DIR) / PROFILES_SUBDIR


def profile_path(name: str) -> Path:
    """Return the on-disk path for a profile by name.

    Does NOT verify existence. Use `load_profile` for the read-and-validate
    path; this helper is for callers that need the path to write or list.
    """
    return _profiles_dir() / f"{name}.yaml"


def list_profiles() -> list[str]:
    """Return the sorted list of profile names available on disk.

    A "profile name" is the stem of a `.yaml` file under PROFILES_DIR.
    Returns an empty list when the directory does not exist.
    """
    d = _profiles_dir()
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


def load_profile(name: str) -> dict[str, Any]:
    """Read `<PROFILES_DIR>/<name>.yaml` and return its contents as a dict.

    Raises FileNotFoundError with a helpful message listing available
    profile names when the named profile doesn't exist. Raises
    ValueError if the YAML is not a mapping (e.g. a scalar / list).
    """
    path = profile_path(name)
    if not path.exists():
        available = list_profiles()
        if available:
            avail_str = ", ".join(available)
            msg = (
                f"profile '{name}' not found at {path}. "
                f"Available profiles: {avail_str}."
            )
        else:
            msg = (
                f"profile '{name}' not found at {path}. "
                f"No profiles configured yet — create one with "
                f"`claude-mirror profile create {name} --backend <backend>`."
            )
        raise FileNotFoundError(msg)

    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"profile '{name}' at {path} is not a YAML mapping; "
            f"got {type(data).__name__}."
        )
    return data


def apply_profile(
    profile: dict[str, Any],
    project_config: dict[str, Any],
) -> dict[str, Any]:
    """Merge `profile` into `project_config` and return a new dict.

    Project values override profile values for any field both define. A
    project field counts as "set" when its value is truthy (non-empty
    string, non-zero int, True, non-empty list, non-None). The dataclass
    defaults (empty string, 0, False, [], None) always appear in the
    project dict after `Config.load -> asdict`, so falsy = unset.

    The `description` and `backend` keys on the profile are passed
    through. `description` is a comment-only field for `profile list`
    and is harmless on the merged config (Config.load drops unknown
    keys at the dataclass boundary anyway).

    Pure: never mutates either argument. Idempotent: applying the same
    profile twice produces the same dict.
    """
    merged: dict[str, Any] = dict(profile)
    for key, value in project_config.items():
        # Project value wins when it's truthy. Falsy project values
        # (empty string, 0, False, [], None) are treated as "unset" so
        # the profile's default takes effect.
        if value:
            merged[key] = value
        else:
            # Fall back to profile's value if it has one; otherwise
            # keep the project's falsy value (so we don't drop fields
            # the profile doesn't know about).
            if key not in merged:
                merged[key] = value
    return merged


# ─── Profile metadata helpers (used by `claude-mirror profile list`) ──────

def profile_summary(name: str) -> dict[str, Any]:
    """Return a one-line summary dict for a profile.

    Shape: {"name": str, "backend": str, "description": str, "path": str}.
    Backend / description default to "" if the profile YAML doesn't
    set them. Used by `claude-mirror profile list` to render a table
    without re-reading every file at the call site.
    """
    try:
        data = load_profile(name)
    except FileNotFoundError:
        return {
            "name": name,
            "backend": "",
            "description": "",
            "path": str(profile_path(name)),
        }
    return {
        "name": name,
        "backend": str(data.get("backend", "") or ""),
        "description": str(data.get("description", "") or ""),
        "path": str(profile_path(name)),
    }
