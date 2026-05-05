from __future__ import annotations

import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl  # POSIX file locking — used to serialize inbox appends
except ImportError:  # pragma: no cover — Windows fallback
    fcntl = None  # type: ignore

INBOX_FILENAME = ".claude_mirror_inbox.jsonl"


def _escape_applescript(s: str) -> str:
    """Escape a Python string for safe interpolation inside an AppleScript
    double-quoted literal. AppleScript treats backslash as an escape char and
    double-quote as the string delimiter; everything else is literal.
    """
    return s.replace("\\", "\\\\").replace('"', '\\"')


def inbox_path(project_path: str) -> Path:
    """Return the per-project inbox file path."""
    return Path(project_path) / INBOX_FILENAME


class Notifier:
    def __init__(self, project_path: str) -> None:
        self._system = platform.system()
        self._inbox = inbox_path(project_path)

    def notify(self, title: str, message: str, event: dict | None = None) -> None:
        self._write_inbox(title, message, event)
        try:
            if self._system == "Darwin":
                self._notify_macos(title, message)
            elif self._system == "Linux":
                self._notify_linux(title, message)
            elif self._system == "Windows":
                self._notify_windows(title, message)
        except Exception as e:
            # Non-fatal but surface the reason so the user can fix it
            print(f"[claude-mirror] desktop notification failed: {e}")

    def notify_failure(
        self,
        title: str,
        body: str,
        *,
        action_required: bool = False,
    ) -> None:
        """Cross-platform desktop notification for sync failures.

        Used for Tier 2 per-backend failures. When action_required=True,
        the notification is given a more attention-grabbing prefix
        ("🔴 ACTION REQUIRED:") so the user notices auth/quota issues.

        Best-effort: failure to send is silently swallowed (a notification
        failure must NEVER block the sync command from completing).
        """
        # Prefix only the body; keep title clean so notification centres
        # group failures by app rather than by alert variant.
        display_body = (
            f"🔴 ACTION REQUIRED — {body}" if action_required else body
        )
        try:
            if self._system == "Darwin":
                # Reuse the macOS path; AppleScript escaping happens inside.
                self._notify_macos(title, display_body)
            elif self._system == "Linux":
                # urgency=critical bypasses notification centre auto-dismiss
                # on most desktops, so AUTH/QUOTA alerts stick around until
                # the user acknowledges them.
                args = ["notify-send", title, display_body, "--icon=dialog-warning"]
                if action_required:
                    args.append("--urgency=critical")
                subprocess.run(args, check=False, capture_output=True)
            elif self._system == "Windows":
                # Best-effort plyer path — same as `notify`. plyer doesn't
                # expose an urgency knob; the prefix in body is what the
                # user sees.
                self._notify_windows(title, display_body)
        except Exception:
            # Never let a notification failure escape — the sync must
            # finish regardless of whether the desktop notifier worked.
            pass

    def _write_inbox(self, title: str, message: str, event: dict | None) -> None:
        """Append notification to the project-scoped inbox file under an exclusive
        flock so concurrent watcher threads can't interleave their JSON lines."""
        try:
            entry = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "title": title,
                "message": message,
            }
            if event:
                entry.update(event)
            line = json.dumps(entry) + "\n"
            with self._inbox.open("a") as f:
                if fcntl is not None:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    except OSError:
                        pass
                try:
                    f.write(line)
                    f.flush()
                finally:
                    if fcntl is not None:
                        try:
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        except OSError:
                            pass
        except Exception:
            pass

    def _notify_macos(self, title: str, message: str) -> None:
        # Uses osascript. If notifications don't appear, grant permission to the
        # calling app (Terminal, iTerm2, or the launchd agent) in:
        #   System Settings → Notifications → [your terminal app] → Allow Notifications
        # title/message arrive from collaborator-controlled events, so they MUST
        # be escaped before being interpolated into the AppleScript source.
        script = (
            f'display notification "{_escape_applescript(message)}" '
            f'with title "{_escape_applescript(title)}" '
            f'sound name "Ping"'
        )
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"osascript failed ({result.stderr.strip()}). "
                "To fix: open System Settings → Notifications → find your terminal app "
                "(Terminal or iTerm2) → enable Allow Notifications."
            )

    def _notify_linux(self, title: str, message: str) -> None:
        subprocess.run(
            ["notify-send", title, message, "--icon=dialog-information"],
            check=True,
            capture_output=True,
        )

    def _notify_windows(self, title: str, message: str) -> None:
        try:
            from plyer import notification
            notification.notify(title=title, message=message, app_name="claude-mirror")
        except ImportError:
            pass


def read_and_clear_inbox(project_path: str) -> list[dict]:
    """Read all pending notifications for a project and clear the inbox.

    Atomic against concurrent writers: opens the inbox r+, takes LOCK_EX,
    reads the full contents, truncates the file to 0 bytes, fsyncs, then
    releases the lock. Concurrent _write_inbox() calls (which take the
    same LOCK_EX before appending) therefore can't have their lines lost
    between read and clear. Idempotent if the inbox doesn't exist.
    """
    path = inbox_path(project_path)
    if not path.exists():
        return []
    try:
        with path.open("r+") as f:
            if fcntl is not None:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            try:
                data = f.read()
                f.seek(0)
                f.truncate(0)
                f.flush()
                try:
                    import os as _os
                    _os.fsync(f.fileno())
                except OSError:
                    pass
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
        lines = data.strip().splitlines()
        return [json.loads(line) for line in lines if line.strip()]
    except FileNotFoundError:
        return []
    except Exception:
        return []
