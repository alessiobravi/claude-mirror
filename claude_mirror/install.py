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
from typing import Any, List, Optional

import click

# ── well-known paths ───────────────────────────────────────────────────────────

# Respect CLAUDE_CONFIG_DIR so users running a parallel Claude install
# (CLAUDE_CONFIG_DIR pointed at a non-default directory) get the skill,
# settings.json, hook, and legacy cleanup applied to their active install
# rather than always to ~/.claude. Falls back to ~/.claude when the env
# var is unset.
CLAUDE_BASE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))).expanduser()

SKILL_DEST        = CLAUDE_BASE_DIR / "skills" / "claude-mirror" / "SKILL.md"
SETTINGS_PATH     = CLAUDE_BASE_DIR / "settings.json"
HOOK_COMMAND      = "claude-mirror inbox 2>/dev/null || true"

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
    data: dict[str, Any] = {}
    if SETTINGS_PATH.exists():
        try:
            data = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError:
            _err("settings.json is not valid JSON — skipping.")
            return

    hooks = data.setdefault("hooks", {})
    pre   = hooks.setdefault("PreToolUse", [])

    # Idempotency check: if our hook is already present, no work to do.
    for entry in pre:
        if isinstance(entry, dict):
            for h in entry.get("hooks", []):
                if isinstance(h, dict) and h.get("command") == HOOK_COMMAND:
                    _skip("Hook already present — nothing to do.")
                    return

    if not _confirm("Add PreToolUse hook to ~/.claude/settings.json?"):
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

    new_pre: list[Any] = []
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


# ── shell tab-completion ───────────────────────────────────────────────────────

# Markers wrap the eval line we add to the user's shell rc, so we can find
# our addition unambiguously during uninstall (or replacement) without
# clobbering user-edited content nearby. The phrasing is deliberately
# verbose; users grepping their rc for "claude-mirror" should get hits
# that explain themselves.
_COMPLETION_MARK_BEGIN = "# >>> claude-mirror tab-completion (added by claude-mirror-install) >>>"
_COMPLETION_MARK_END   = "# <<< claude-mirror tab-completion <<<"

# Cross-call flag set by install_completion when it newly adds (or refreshes)
# the rc-file eval line. install_cli reads this at the end of the install run
# to decide whether to print the prominent activation banner + offer to
# replace the current shell. Module-level state because the install_*
# functions don't have a shared context object today; promotion to a
# proper `InstallReport` dataclass would be the right cleanup if more flags
# get added.
_completion_activation_pending: bool = False


def _detect_shell() -> Optional[str]:
    """Return 'zsh', 'bash', 'fish', 'powershell', or None.

    Resolution order:
      1. SHELL env var basename — pick zsh/bash/fish/pwsh as available.
      2. On Windows (or when no SHELL is set and PSModulePath is
         present), fall back to powershell — Windows users running
         claude-mirror-install from a PowerShell prompt should get
         PowerShell completion auto-installed.
      3. Platform default for Unix-likes (zsh on macOS, bash on Linux).

    Returns None for unsupported shells (sh/dash/csh/etc.) — better to
    skip the auto-install than write something that won't work.

    PowerShell is intentionally lower priority than the Unix shells on
    macOS / Linux: a user who runs zsh as their login shell but has
    pwsh installed should still get zsh completion, not pwsh.
    """
    shell_env = os.environ.get("SHELL", "")
    name = Path(shell_env).name if shell_env else ""
    # Strip a trailing ".exe" — Windows binaries land on $SHELL on a
    # few hybrid setups (Git Bash + WSL) with the executable suffix
    # still attached.
    if name.endswith(".exe"):
        name = name[: -len(".exe")]
    if name in ("zsh", "bash", "fish"):
        return name
    # `pwsh` is the canonical PowerShell 7+ binary name on every OS;
    # `powershell` is Windows PowerShell 5.1. Both map to the same
    # completion source.
    if name in ("pwsh", "powershell"):
        return "powershell"
    # If SHELL is unset, prefer Unix defaults on Unix and PowerShell on
    # Windows. Windows almost never sets SHELL, so checking platform
    # first is the right move.
    if not name:
        if platform.system() == "Windows":
            return "powershell"
        return "zsh" if platform.system() == "Darwin" else "bash"
    # Anything else (sh, dash, csh, tcsh, ksh) — punt.
    return None


def _completion_target(shell: str) -> Optional[Path]:
    """Where the eval line goes for each shell.

    For zsh + bash we append to the interactive rc file. For fish we use
    the dedicated completions directory (fish auto-loads from there).
    For powershell we target `$PROFILE.CurrentUserAllHosts` — the
    canonical "runs in every host" profile path on every OS pwsh
    supports.
    """
    home = Path.home()
    if shell == "zsh":
        return home / ".zshrc"
    if shell == "bash":
        # macOS Terminal.app starts bash as a login shell, so .bash_profile
        # is the canonical interactive rc. Linux gnome-terminal etc. use
        # .bashrc. Pick by platform.
        if platform.system() == "Darwin":
            return home / ".bash_profile"
        return home / ".bashrc"
    if shell == "fish":
        return home / ".config" / "fish" / "completions" / "claude-mirror.fish"
    if shell == "powershell":
        # PowerShell 7+ resolves $PROFILE.CurrentUserAllHosts to a
        # platform-specific path:
        #   * Windows: %USERPROFILE%/Documents/PowerShell/profile.ps1
        #   * macOS:   ~/.config/powershell/profile.ps1
        #   * Linux:   ~/.config/powershell/profile.ps1
        # On Windows we honour the Documents/PowerShell convention; on
        # Unix-like systems we use the XDG-style location pwsh itself
        # ships with. Either way, the file we target gets dot-sourced
        # automatically on every interactive shell start.
        if platform.system() == "Windows":
            return home / "Documents" / "PowerShell" / "profile.ps1"
        return home / ".config" / "powershell" / "profile.ps1"
    return None


def install_completion() -> None:
    # Declare module-level global once at the top of the function so the
    # subsequent assignments in the fish, refresh, and append branches all
    # write to the module attribute. Python's compiler requires the global
    # declaration to appear before any assignment to the name in the
    # function scope, even if those assignments live in different branches.
    global _completion_activation_pending

    _section("Shell tab-completion")

    shell = _detect_shell()
    if shell is None:
        _warn(
            f"Shell '{Path(os.environ.get('SHELL', '')).name or 'unknown'}' "
            "is not supported (only zsh / bash / fish / powershell). Skipping."
        )
        return

    rc = _completion_target(shell)
    if rc is None:
        _warn("Could not determine target rc file. Skipping.")
        return

    click.echo(f"  Shell:  {shell}")
    click.echo(f"  Target: {rc}")

    # fish gets a dedicated completion file — write the script directly,
    # no markers / no eval indirection. Idempotent: overwrite each time
    # to pick up new commands.
    if shell == "fish":
        if not _confirm(f"Install fish completion to {rc}?"):
            _skip()
            return
        rc.parent.mkdir(parents=True, exist_ok=True)
        completion_script = subprocess.run(
            [_find_binary(), "completion", "fish"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        rc.write_text(completion_script)
        _ok(f"Fish completion installed at {rc}.")
        _completion_activation_pending = True
        return

    # PowerShell uses `#` comment markers and an `Invoke-Expression` of
    # the live `claude-mirror completion powershell` output, mirroring
    # the eval-into-rc pattern used for zsh/bash but with PowerShell
    # idioms. The marker block is identical in shape — same begin/end
    # tokens — so `_replace_completion_block` reuses cleanly.
    if shell == "powershell":
        invoke_line = (
            f"Invoke-Expression (& {_find_binary()} completion powershell | Out-String)"
        )
        block = "\n".join([_COMPLETION_MARK_BEGIN, invoke_line, _COMPLETION_MARK_END])

        existing = rc.read_text() if rc.exists() else ""
        if _COMPLETION_MARK_BEGIN in existing:
            if invoke_line in existing:
                _skip("Tab-completion already installed and current.")
                return
            if not _confirm("Tab-completion is installed but stale — update?"):
                _skip()
                return
            new_content = _replace_completion_block(existing, block)
            rc.parent.mkdir(parents=True, exist_ok=True)
            rc.write_text(new_content)
            _ok(f"Tab-completion refreshed in {rc}.")
            _completion_activation_pending = True
            return

        if not _confirm(f"Add tab-completion to {rc}?"):
            _skip()
            return

        sep = "" if existing.endswith("\n") or not existing else "\n"
        rc.parent.mkdir(parents=True, exist_ok=True)
        with rc.open("a") as f:
            f.write(f"{sep}\n{block}\n")
        _ok(f"Tab-completion installed in {rc}.")
        _completion_activation_pending = True
        return

    # zsh / bash — eval the dynamic completion script from rc, wrapped
    # in markers so we can find / remove it cleanly.
    eval_line = f'eval "$({_find_binary()} completion {shell})"'
    block = "\n".join([_COMPLETION_MARK_BEGIN, eval_line, _COMPLETION_MARK_END])

    existing = rc.read_text() if rc.exists() else ""
    if _COMPLETION_MARK_BEGIN in existing:
        # Already installed. Compare just the eval line (ignoring surrounding
        # whitespace) — if unchanged, this is a true no-op. If the eval line
        # changed (e.g., binary path moved), offer to refresh.
        if eval_line in existing:
            _skip("Tab-completion already installed and current.")
            return
        if not _confirm("Tab-completion is installed but stale — update?"):
            _skip()
            return
        new_content = _replace_completion_block(existing, block)
        rc.write_text(new_content)
        _ok(f"Tab-completion refreshed in {rc}.")
        _completion_activation_pending = True
        return

    if not _confirm(f"Add tab-completion eval to {rc}?"):
        _skip()
        return

    # Append, with a leading newline only if the file does not already end with one.
    sep = "" if existing.endswith("\n") or not existing else "\n"
    if not rc.exists():
        rc.parent.mkdir(parents=True, exist_ok=True)
    with rc.open("a") as f:
        f.write(f"{sep}\n{block}\n")
    _ok(f"Tab-completion installed in {rc}.")
    # Set the activation flag so the end-of-run banner fires.
    _completion_activation_pending = True


def uninstall_completion() -> None:
    _section("Shell tab-completion")

    shell = _detect_shell()
    if shell is None:
        _skip("Shell unknown — nothing to remove.")
        return

    rc = _completion_target(shell)
    if rc is None or not rc.exists():
        _skip("No tab-completion installed at the expected location.")
        return

    if shell == "fish":
        click.echo(f"  Will remove: {rc}")
        if not _confirm("Remove fish completion file?"):
            _skip()
            return
        rc.unlink()
        _ok("Fish completion removed.")
        return

    existing = rc.read_text()
    if _COMPLETION_MARK_BEGIN not in existing:
        _skip(f"No claude-mirror completion block found in {rc}.")
        return

    click.echo(f"  Will strip the claude-mirror completion block from: {rc}")
    if not _confirm("Remove tab-completion block?"):
        _skip()
        return

    new_content = _replace_completion_block(existing, replacement="")
    rc.write_text(new_content)
    _ok(f"Tab-completion block removed from {rc}.")


def _replace_completion_block(content: str, replacement: str = "") -> str:
    """Replace the marker-wrapped block (and the surrounding blank lines) with `replacement`.

    Used for both update (replacement = new block) and uninstall (replacement = "").
    Tolerates the block being at the start, end, or middle of the file.
    """
    lines = content.splitlines(keepends=True)
    out: List[str] = []
    inside = False
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped == _COMPLETION_MARK_BEGIN:
            inside = True
            continue
        if stripped == _COMPLETION_MARK_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    result = "".join(out)
    if replacement:
        # Append the new block followed by a trailing newline.
        if result and not result.endswith("\n"):
            result += "\n"
        result += "\n" + replacement + "\n"
    # Collapse the resulting double-blank-lines that the strip-and-rejoin
    # creates around the removed block.
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result


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
      * Shell tab-completion → ~/.zshrc / ~/.bashrc / fish completions dir
      * Background watcher   → launchd (macOS) or systemd (Linux)
    """
    system = platform.system()

    if uninstall:
        click.echo(click.style("\nclaude-mirror uninstaller", bold=True))
        click.echo("Each component will be confirmed before removal.")

        uninstall_skill()
        uninstall_hook()
        uninstall_completion()

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
        install_completion()

        if system == "Darwin":
            install_launchd()
        elif system == "Linux":
            install_systemd()
        else:
            click.echo()
            _warn(f"Platform '{system}' — skipping background watcher service.")

        click.echo()
        click.echo(click.style("Installation complete.", bold=True))

        # Tab-completion activation — by far the most-missed post-install step
        # in older versions because the per-step "✓ installed" message was
        # easy to skim past. Surface it as a high-contrast banner at the very
        # end and offer to drop the user into a fresh shell so the eval line
        # is sourced immediately.
        if _completion_activation_pending:
            shell_name = _detect_shell() or "shell"
            rc = _completion_target(shell_name)
            click.echo()
            banner = "═" * 70
            click.echo(click.style(banner, fg="yellow"))
            click.echo(click.style(
                "  Tab-completion is installed but is NOT YET ACTIVE in this shell.",
                fg="yellow", bold=True,
            ))
            click.echo(click.style(
                "  Open a brand-new terminal, OR run the following command in",
                fg="yellow",
            ))
            click.echo(click.style(
                "  the current terminal to activate tab-completion immediately:",
                fg="yellow",
            ))
            click.echo()
            click.echo(click.style(f"      source {rc}", fg="cyan", bold=True))
            click.echo()
            click.echo(click.style(banner, fg="yellow"))

            # Offer to replace the current process with a fresh interactive shell
            # so .zshrc / .bashrc gets re-sourced and tab-completion is live
            # without the user typing anything else. Default is No because
            # `os.execvp` replaces the current process — losing any environment
            # variables the user set during this session, any pending shell
            # state, and the install command's calling context. For someone
            # mid-install that is usually fine (they have not accumulated
            # session state worth preserving) but we ask explicitly.
            if shell_name in ("zsh", "bash") and _confirm(
                "Replace the current shell with a fresh one now to activate "
                "tab-completion immediately? (Default: No — open a new terminal "
                "manually instead.)",
                default=False,
            ):
                shell_path = os.environ.get("SHELL", "/bin/" + shell_name)
                click.echo(click.style(
                    f"\nReplacing this shell with: {shell_path}",
                    fg="green",
                ))
                # os.execvp replaces the current process image with the named
                # binary. The new interactive shell will source the user's rc
                # file, picking up the completion eval line we just installed.
                os.execvp(shell_path, [shell_path])
                # Unreachable — execvp does not return on success.

        click.echo()
        click.echo("Next steps:")
        click.echo("  1. claude-mirror init --wizard   (run in each project directory)")
        click.echo("  2. claude-mirror auth             (authenticate with the configured backend)")
        click.echo("  3. /claude-mirror                 (invoke skill inside Claude Code)")
