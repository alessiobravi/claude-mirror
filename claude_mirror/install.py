"""claude-mirror-install: interactive installer/uninstaller for claude-mirror components.

Components managed:
  1. Claude Code skill  → ~/.claude/skills/claude-mirror/SKILL.md
  2. PreToolUse hook   → ~/.claude/settings.json
  3. Background watcher:
       macOS  → ~/Library/LaunchAgents/com.claude-mirror.watch.plist
       Linux  → ~/.config/systemd/user/claude-mirror-watch.service
"""
from __future__ import annotations

import json
import platform
import shutil
import os
import subprocess
from pathlib import Path

import click

# ── well-known paths ───────────────────────────────────────────────────────────

# Respect CLAUDE_CONFIG_DIR so users running a parallel Claude install
# (CLAUDE_CONFIG_DIR pointed at a non-default directory) get the skill,
# settings.json, hook, and legacy cleanup applied to their active install
# rather than always to ~/.claude. Falls back to ~/.claude when the env
# var is unset.
CLAUDE_BASE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()

SKILL_DEST        = CLAUDE_BASE_DIR / "skills" / "claude-mirror" / "SKILL.md"
LEGACY_SKILL_DIR  = CLAUDE_BASE_DIR / "skills" / "claude-sync"   # pre-v0.5.0
SETTINGS_PATH     = CLAUDE_BASE_DIR / "settings.json"
HOOK_COMMAND      = "claude-mirror inbox 2>/dev/null || true"

# Pre-v0.5.0 hook command — still appears in some users' settings.json after
# the rename. Removed automatically during install_hook so it doesn't keep
# firing against a binary that no longer exists.
LEGACY_HOOK_COMMAND_PREFIXES = ("claude-sync inbox",)

LAUNCHD_LABEL = "com.claude-mirror.watch"
LAUNCHD_PLIST = Path(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist").expanduser()

SYSTEMD_SERVICE      = Path("~/.config/systemd/user/claude-mirror-watch.service").expanduser()
SYSTEMD_SERVICE_NAME = "claude-mirror-watch"

# ── helpers ────────────────────────────────────────────────────────────────────

def _find_skill_source() -> Path | None:
    """Locate the skill source file. Resolution order:

    1. Bundled inside the wheel at `claude_mirror/_skill/claude-mirror.md`
       (the PyPI install path — see pyproject.toml `force-include`).
    2. Repo / editable-install layout at `<repo>/skills/claude-mirror.md`,
       which is where the source lives in git.

    Returning the bundled copy first means PyPI users get a working skill
    install via `claude-mirror-install` with no manual download step.
    Returning the repo copy as fallback keeps the dev workflow unchanged
    (editable installs continue to find the un-bundled source).
    """
    here = Path(__file__).parent
    # PyPI / wheel install — bundled by Hatchling's force-include.
    bundled = here / "_skill" / "claude-mirror.md"
    if bundled.exists():
        return bundled
    # Editable install from a clone — source still in repo's skills/ dir.
    repo = here.parent / "skills" / "claude-mirror.md"
    if repo.exists():
        return repo
    return None


def _find_binary() -> str:
    """Return the absolute path to the claude-mirror binary, or the bare name."""
    binary = shutil.which("claude-mirror")
    return binary if binary else "claude-mirror"


def _ok(msg: str) -> None:
    click.echo(click.style(f"  \u2713 {msg}", fg="green"))


def _skip(msg: str = "Skipped.") -> None:
    click.echo(f"  {msg}")


def _warn(msg: str) -> None:
    click.echo(click.style(f"  ! {msg}", fg="yellow"))


def _err(msg: str) -> None:
    click.echo(click.style(f"  ERROR: {msg}", fg="red"))


def _section(title: str) -> None:
    click.echo()
    click.echo(click.style(f"\u2500\u2500 {title} ", fg="blue") + click.style("\u2500" * max(0, 50 - len(title)), fg="blue"))


def _confirm(prompt: str, default: bool = True) -> bool:
    return click.confirm(f"  {prompt}", default=default)


# ── skill ──────────────────────────────────────────────────────────────────────

def install_skill() -> None:
    _section("Claude Code skill")
    click.echo(f"  Destination: {SKILL_DEST}")

    source = _find_skill_source()
    if source is None:
        _warn("Skill source file not found — skipping.")
        click.echo("  Expected: skills/claude-mirror.md next to this package.")
        return

    click.echo(f"  Source:      {source}")
    if not _confirm(f"Install Claude Code skill to {SKILL_DEST}?"):
        _skip()
        return

    SKILL_DEST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, SKILL_DEST)
    _ok("Skill installed.")

    # One-shot cleanup of the pre-v0.5.0 skill directory (claude-sync rename).
    # Idempotent — silently no-ops once the legacy dir has been removed.
    if LEGACY_SKILL_DIR.exists():
        click.echo(f"  Found legacy skill at {LEGACY_SKILL_DIR}")
        if _confirm(f"Remove legacy claude-sync skill directory?"):
            shutil.rmtree(LEGACY_SKILL_DIR)
            _ok("Legacy skill removed.")
        else:
            _skip("Left legacy skill in place — Claude Code may load both.")


def uninstall_skill() -> None:
    _section("Claude Code skill")

    if not SKILL_DEST.exists():
        _skip("Skill not installed — nothing to remove.")
        return

    click.echo(f"  Will remove: {SKILL_DEST}")
    if not _confirm("Remove Claude Code skill?"):
        _skip()
        return

    SKILL_DEST.unlink()
    try:
        SKILL_DEST.parent.rmdir()          # remove dir if now empty
    except OSError:
        pass
    _ok("Skill removed.")


# ── settings.json hook ─────────────────────────────────────────────────────────

def install_hook() -> None:
    _section("Claude Code PreToolUse hook")
    click.echo(f"  File:    {SETTINGS_PATH}")
    click.echo(f"  Command: {HOOK_COMMAND}")

    # Read and validate existing file before prompting
    data: dict = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError:
            _err("settings.json is not valid JSON — skipping.")
            return

    hooks = data.setdefault("hooks", {})
    pre   = hooks.setdefault("PreToolUse", [])

    # Strip legacy claude-sync hook entries first (rename cleanup).
    legacy_removed = 0
    for entry in pre:
        if not isinstance(entry, dict):
            continue
        kept = []
        for h in entry.get("hooks", []):
            if (
                isinstance(h, dict)
                and isinstance(h.get("command"), str)
                and any(h["command"].startswith(p) for p in LEGACY_HOOK_COMMAND_PREFIXES)
            ):
                legacy_removed += 1
            else:
                kept.append(h)
        entry["hooks"] = kept
    # Prune wrapper entries whose hooks list is now empty (orphans left
    # behind by the legacy strip above, or by hand-edited settings.json).
    pre[:] = [
        e for e in pre
        if not (isinstance(e, dict) and not e.get("hooks"))
    ]
    if legacy_removed:
        click.echo(f"  Removed {legacy_removed} legacy claude-sync hook entry(ies).")

    # Idempotency check for the current hook before prompting.
    for entry in pre:
        if isinstance(entry, dict):
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and h.get("command") == HOOK_COMMAND:
                    if legacy_removed:
                        # We modified the file (legacy cleanup) — write the changes
                        # even though the new hook was already there.
                        SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
                        _ok("Legacy hook(s) cleaned up; current hook already present.")
                    else:
                        _skip("Hook already present — nothing to do.")
                    return

    if not _confirm("Add PreToolUse hook to ~/.claude/settings.json?"):
        if legacy_removed:
            # User declined the new hook, but we still want to persist the
            # legacy cleanup we already did to the in-memory dict.
            SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
            _ok("Legacy hook(s) removed; new hook NOT installed (user declined).")
        else:
            _skip()
        return

    pre.append({
        "matcher": "",
        "hooks": [{"type": "command", "command": HOOK_COMMAND}],
    })

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    _ok("Hook added.")


def uninstall_hook() -> None:
    _section("Claude Code PreToolUse hook")

    if not SETTINGS_PATH.exists():
        _skip("settings.json not found — nothing to remove.")
        return

    try:
        data = json.loads(SETTINGS_PATH.read_text())
    except json.JSONDecodeError:
        _err("settings.json is not valid JSON — skipping.")
        return

    hooks = data.get("hooks", {})
    pre   = hooks.get("PreToolUse", [])

    new_pre: list = []
    removed = False
    for entry in pre:
        if isinstance(entry, dict):
            filtered = [
                h for h in entry.get("hooks", [])
                if not (isinstance(h, dict) and h.get("command") == HOOK_COMMAND)
            ]
            if len(filtered) < len(entry.get("hooks", [])):
                removed = True
                if filtered:
                    new_pre.append({**entry, "hooks": filtered})
                # else: drop entire entry — it only contained our hook
            else:
                new_pre.append(entry)
        else:
            new_pre.append(entry)

    if not removed:
        _skip("Hook not found — nothing to remove.")
        return

    click.echo(f"  Will edit: {SETTINGS_PATH}")
    if not _confirm("Remove PreToolUse hook?"):
        _skip()
        return

    if new_pre:
        hooks["PreToolUse"] = new_pre
    else:
        hooks.pop("PreToolUse", None)
    if not hooks:
        data.pop("hooks", None)

    SETTINGS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    _ok("Hook removed.")


# ── macOS launchd ──────────────────────────────────────────────────────────────

_LAUNCHD_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>watch-all</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir}/claude-mirror-watch.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/claude-mirror-watch.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:{home}/.local/bin</string>
        <key>GRPC_VERBOSITY</key>
        <string>ERROR</string>
    </dict>
</dict>
</plist>
"""


def install_launchd() -> None:
    _section("macOS launchd agent (background watcher)")

    binary  = _find_binary()
    log_dir = Path("~/Library/Logs").expanduser()
    home    = Path.home()

    click.echo(f"  Plist:  {LAUNCHD_PLIST}")
    click.echo(f"  Binary: {binary}")
    click.echo(f"  Log:    {log_dir}/claude-mirror-watch.log")

    if not _confirm("Install launchd agent (auto-starts watcher on login)?"):
        _skip()
        return

    content = _LAUNCHD_TEMPLATE.format(
        label=LAUNCHD_LABEL,
        binary=binary,
        log_dir=log_dir,
        home=home,
    )

    # Unload existing agent before overwriting (otherwise launchctl load fails)
    if LAUNCHD_PLIST.exists():
        subprocess.run(
            ["launchctl", "unload", "-w", str(LAUNCHD_PLIST)],
            capture_output=True,
        )

    LAUNCHD_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST.write_text(content)

    result = subprocess.run(
        ["launchctl", "load", "-w", str(LAUNCHD_PLIST)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _warn(f"launchctl load failed: {result.stderr.strip()}")
        click.echo(f"  Plist written. Load manually with:")
        click.echo(f"    launchctl load -w {LAUNCHD_PLIST}")
    else:
        _ok("Launchd agent installed and loaded.")


def uninstall_launchd() -> None:
    _section("macOS launchd agent (background watcher)")

    if not LAUNCHD_PLIST.exists():
        _skip("Plist not found — nothing to remove.")
        return

    click.echo(f"  Will unload and remove: {LAUNCHD_PLIST}")
    if not _confirm("Remove launchd agent?"):
        _skip()
        return

    subprocess.run(
        ["launchctl", "unload", "-w", str(LAUNCHD_PLIST)],
        capture_output=True,
    )
    LAUNCHD_PLIST.unlink()
    _ok("Launchd agent removed.")


# ── Linux systemd ──────────────────────────────────────────────────────────────

_SYSTEMD_TEMPLATE = """\
[Unit]
Description=Claude Sync watcher — real-time Drive notifications
After=network-online.target
Wants=network-online.target

[Service]
ExecStart={binary} watch-all
Restart=on-failure
RestartSec=10
Environment=GRPC_VERBOSITY=ERROR
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def install_systemd() -> None:
    _section("Linux systemd user service (background watcher)")

    binary = _find_binary()
    click.echo(f"  Service file: {SYSTEMD_SERVICE}")
    click.echo(f"  Binary:       {binary}")

    if not _confirm("Install systemd user service (auto-starts watcher on login)?"):
        _skip()
        return

    SYSTEMD_SERVICE.parent.mkdir(parents=True, exist_ok=True)
    SYSTEMD_SERVICE.write_text(_SYSTEMD_TEMPLATE.format(binary=binary))

    ok = True
    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE_NAME],
    ):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            _warn(f"{' '.join(cmd)} failed: {result.stderr.strip()}")
            ok = False

    if ok:
        _ok("systemd user service installed and started.")
    else:
        _warn("Service file written but some systemctl commands failed (see above).")
    click.echo("  View logs: journalctl --user -u claude-mirror-watch -f")


def uninstall_systemd() -> None:
    _section("Linux systemd user service (background watcher)")

    if not SYSTEMD_SERVICE.exists():
        _skip("Service file not found — nothing to remove.")
        return

    click.echo(f"  Will stop and remove: {SYSTEMD_SERVICE}")
    if not _confirm("Remove systemd user service?"):
        _skip()
        return

    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SYSTEMD_SERVICE_NAME],
        capture_output=True,
    )
    SYSTEMD_SERVICE.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    _ok("systemd user service removed.")


# ── entry point ────────────────────────────────────────────────────────────────

@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--uninstall", is_flag=True,
    help="Remove all claude-mirror components instead of installing them.",
)
def install_cli(uninstall: bool) -> None:
    """Interactively install or uninstall claude-mirror components.

    Each component is confirmed individually before any action is taken.
    Components:

    \b
      * Claude Code skill    → ~/.claude/skills/claude-mirror/SKILL.md
      * PreToolUse hook      → ~/.claude/settings.json
      * Background watcher   → launchd (macOS) or systemd (Linux)
    """
    system = platform.system()

    if uninstall:
        click.echo(click.style("\nclaude-mirror uninstaller", bold=True))
        click.echo("Each component will be confirmed before removal.")

        uninstall_skill()
        uninstall_hook()

        if system == "Darwin":
            uninstall_launchd()
        elif system == "Linux":
            uninstall_systemd()
        else:
            click.echo()
            _warn(f"Platform '{system}' — no service management to undo.")

        click.echo()
        click.echo(click.style("Uninstall complete.", bold=True))

    else:
        click.echo(click.style("\nclaude-mirror installer", bold=True))
        click.echo("Each component will be confirmed before installation.")

        install_skill()
        install_hook()

        if system == "Darwin":
            install_launchd()
        elif system == "Linux":
            install_systemd()
        else:
            click.echo()
            _warn(f"Platform '{system}' — skipping background watcher service.")

        click.echo()
        click.echo(click.style("Installation complete.", bold=True))
        click.echo()
        click.echo("Next steps:")
        click.echo("  1. claude-mirror init --wizard   (run in each project directory)")
        click.echo("  2. claude-mirror auth             (authenticate with Google)")
        click.echo("  3. /claude-mirror                 (invoke skill inside Claude Code)")
