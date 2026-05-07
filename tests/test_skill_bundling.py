"""Tests for `_find_skill_source` — covers both install layouts.

Why this matters:
    PyPI users install claude-mirror as a wheel, which puts the package
    under `<venv>/lib/pythonX.Y/site-packages/claude_mirror/`. There's no
    `skills/` directory next to that location. Pre-v0.5.22, the skill
    lookup only checked the repo-layout path (`<install.py>/../skills/`),
    so PyPI users running `claude-mirror-install` saw "Skill source file
    not found — skipping" and got the binary without the Claude Code skill.

    v0.5.22 fixed this by:
      1. Bundling skills/claude-mirror.md inside the wheel via Hatchling's
         force-include, mapped to `claude_mirror/_skill/claude-mirror.md`.
      2. Making `_find_skill_source` check the bundled location first,
         falling back to the repo location for editable installs.

    These tests pin both paths.
"""
from __future__ import annotations

from pathlib import Path

from claude_mirror.install import _find_skill_source


def test_find_skill_source_returns_existing_path_in_dev_layout():
    """Running pytest from a clone (the dev case): the skill source must
    be findable via the editable-install fallback."""
    src = _find_skill_source()
    assert src is not None, "skill source not found in editable install"
    assert src.exists()
    assert src.name == "claude-mirror.md"
    # Sanity-check the content — it's a markdown file with a frontmatter
    # and the skill name.
    content = src.read_text()
    assert "claude-mirror" in content


def test_find_skill_source_prefers_bundled_over_repo(tmp_path, monkeypatch):
    """If both the bundled location AND the repo location exist, the
    bundled copy wins (PyPI install path takes precedence). This mimics
    what happens for a user who has both an editable clone AND a
    site-packages install of an older version — they should get the
    bundled copy from whichever one is being imported, not the repo's."""
    # Build a fake `claude_mirror` package layout that has BOTH locations
    # populated, then redirect `install.__file__` at it.
    import claude_mirror.install as install_mod

    fake_pkg = tmp_path / "claude_mirror"
    fake_pkg.mkdir()
    (fake_pkg / "_skill").mkdir()
    bundled = fake_pkg / "_skill" / "claude-mirror.md"
    bundled.write_text("# bundled marker")

    repo_skills = tmp_path / "skills"
    repo_skills.mkdir()
    repo = repo_skills / "claude-mirror.md"
    repo.write_text("# repo marker")

    monkeypatch.setattr(install_mod, "__file__", str(fake_pkg / "install.py"))

    src = _find_skill_source()
    assert src is not None
    assert src.read_text() == "# bundled marker", (
        "_find_skill_source should prefer the bundled wheel location"
    )


def test_find_skill_source_falls_back_to_repo_when_bundled_missing(tmp_path, monkeypatch):
    """For editable installs (no force-include applied), the bundled
    location won't exist; the repo-layout fallback must succeed."""
    import claude_mirror.install as install_mod

    fake_pkg = tmp_path / "claude_mirror"
    fake_pkg.mkdir()
    # Deliberately do NOT create _skill/

    repo_skills = tmp_path / "skills"
    repo_skills.mkdir()
    repo = repo_skills / "claude-mirror.md"
    repo.write_text("# repo marker")

    monkeypatch.setattr(install_mod, "__file__", str(fake_pkg / "install.py"))

    src = _find_skill_source()
    assert src is not None
    assert src.read_text() == "# repo marker"


def test_find_skill_source_returns_none_when_neither_exists(tmp_path, monkeypatch):
    """Both layouts missing → return None so `install_skill()` can show
    its 'Skill source file not found' message and skip cleanly."""
    import claude_mirror.install as install_mod

    fake_pkg = tmp_path / "claude_mirror"
    fake_pkg.mkdir()
    monkeypatch.setattr(install_mod, "__file__", str(fake_pkg / "install.py"))

    src = _find_skill_source()
    assert src is None
