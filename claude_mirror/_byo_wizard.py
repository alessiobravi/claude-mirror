"""Helpers used by the Google Drive BYO setup wizard (since v0.5.46).

Three concerns live here:

1. **Cloud Console URL templating + auto-open.** Build the project-scoped
   GCP Console URLs (Drive API enable / Pub/Sub API enable / OAuth client
   creation, plus the global project-creation URL) from a project ID, and
   offer to open them in the user's default browser — falling back cleanly
   to print-only on headless / browserless environments.

2. **Inline input validation.** Reject malformed GCP project IDs,
   Drive folder IDs, Pub/Sub topic IDs, and OAuth-credential JSON files
   AT THE PROMPT, with a single clear error message that points at where
   the user can find the right value. All regexes use bounded quantifiers
   only (no nested unbounded repetition) — ReDoS-safe by construction.

3. **Post-auth Drive smoke test.** After OAuth completes, run a
   `drive.files.list(pageSize=1, q="<folder_id>" in parents)` call to
   surface common misconfigurations (Drive API not enabled, credentials
   for a different GCP project, folder not shared with the authenticating
   account) at setup time rather than at first sync — and offer a retry
   loop without aborting the wizard.

Everything in this module is import-light and side-effect-free at import
time; the heavy `googleapiclient` imports happen inside functions so the
test suite can stub them and the rest of the CLI keeps a snappy startup.
"""
from __future__ import annotations

import json as _json
import re
import webbrowser
from pathlib import Path
from typing import Callable, Optional

import click


# ─────────────────────────────────────────────────────────────────────────────
# 1. Cloud Console URL templating + auto-open
# ─────────────────────────────────────────────────────────────────────────────

# Bare URL constants — exposed so tests can assert exact templating without
# round-tripping through `build_console_urls`. The `{project_id}` placeholder
# is replaced with the user-supplied GCP project ID at build time.
PROJECT_CREATE_URL = "https://console.cloud.google.com/projectcreate"
DRIVE_API_ENABLE_URL_TEMPLATE = (
    "https://console.cloud.google.com/apis/library/"
    "drive.googleapis.com?project={project_id}"
)
PUBSUB_API_ENABLE_URL_TEMPLATE = (
    "https://console.cloud.google.com/apis/library/"
    "pubsub.googleapis.com?project={project_id}"
)
OAUTH_CLIENT_CREATE_URL_TEMPLATE = (
    "https://console.cloud.google.com/apis/credentials/"
    "oauthclient?project={project_id}"
)


def build_console_urls(project_id: str) -> list[tuple[str, str]]:
    """Return a list of `(label, url)` tuples for the GCP Console pages the
    BYO wizard wants to open after capturing the project ID.

    Order matches the order the wizard will guide the user through them:
    enable Drive API, enable Pub/Sub API, create OAuth client. The user
    has typically already created the project (otherwise they wouldn't
    have a project ID to type), so the project-creation URL is *not*
    included here — see `project_create_url()` for that.
    """
    return [
        ("Enable Drive API",
         DRIVE_API_ENABLE_URL_TEMPLATE.format(project_id=project_id)),
        ("Enable Pub/Sub API",
         PUBSUB_API_ENABLE_URL_TEMPLATE.format(project_id=project_id)),
        ("Create OAuth client (Desktop app)",
         OAUTH_CLIENT_CREATE_URL_TEMPLATE.format(project_id=project_id)),
    ]


def project_create_url() -> str:
    """Return the GCP project-creation URL — used when the user does NOT
    yet have a project ID (very first run)."""
    return PROJECT_CREATE_URL


def try_open_browser(url: str) -> bool:
    """Open `url` in the user's default browser. Return True on success,
    False on any failure (headless box, missing display, raise from the
    `webbrowser` module).

    `webbrowser.open` returns False rather than raising on most platforms
    when no browser is available, but it can also raise `webbrowser.Error`
    in some configurations — we treat both as "failed; print URL instead"
    and never let an exception escape this helper.
    """
    try:
        return bool(webbrowser.open(url, new=2))
    except webbrowser.Error:
        return False
    except Exception:
        # Defensive: any unexpected failure inside the browser-open path
        # (e.g. a buggy registered handler) should fall through to
        # print-the-URL, never propagate up into the wizard.
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 2. Inline input validation
# ─────────────────────────────────────────────────────────────────────────────
#
# Each regex below is intentionally constructed with bounded quantifiers
# only. Patterns like `[a-z]{4,28}` cap the work the regex engine can do
# on adversarial input — there is no way for a hand-typed prompt value
# to make any of these regexes degrade catastrophically.

# GCP project IDs: start with a lowercase letter, 6-30 chars total, end
# with a letter or digit (no trailing hyphen). The middle 4-28 chars can
# be lowercase a-z, 0-9, or hyphen. Reference:
# https://cloud.google.com/resource-manager/docs/creating-managing-projects#identifying_projects
GCP_PROJECT_ID_RE = re.compile(r"^[a-z][-a-z0-9]{4,28}[a-z0-9]$")

# Drive folder IDs are URL-safe base64-ish — letters, digits, hyphen,
# underscore — typically 33 chars but Drive doesn't publish a hard upper
# bound. We require at least 20 chars to reject obvious typos like
# pasting just `1BxiMVs0`. No upper bound (still ReDoS-safe: each char
# is a single class, bounded by input length).
DRIVE_FOLDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")

# Pub/Sub topic IDs: start with a letter; 3-255 chars total; allow
# letters, digits, and `_.~+%-` (Pub/Sub's documented character set).
# Reference: https://cloud.google.com/pubsub/docs/admin#resource_names
PUBSUB_TOPIC_ID_RE = re.compile(r"^[a-zA-Z][\w.~+%-]{2,254}$")


def validate_gcp_project_id(value: str) -> str:
    """Click `value_proc` callback: validate a GCP project ID. Raises
    `click.BadParameter` on rejection so Click re-prompts; otherwise
    returns the stripped value."""
    value = (value or "").strip()
    if not GCP_PROJECT_ID_RE.match(value):
        raise click.BadParameter(
            "GCP project IDs are 6-30 chars, must start with a lowercase "
            "letter, end with a letter or digit, and may contain only "
            "lowercase a-z, digits 0-9, and hyphens. "
            "See: https://cloud.google.com/resource-manager/docs/"
            "creating-managing-projects#identifying_projects"
        )
    return value


def validate_drive_folder_id(value: str) -> str:
    """Click `value_proc` callback: validate a Drive folder ID. The user
    most often pastes the WHOLE URL by mistake — call out the right
    fragment to copy."""
    value = (value or "").strip()
    if not DRIVE_FOLDER_ID_RE.match(value):
        raise click.BadParameter(
            "Drive folder IDs are at least 20 URL-safe characters "
            "(A-Z, a-z, 0-9, hyphen, underscore). Open the target folder "
            "in Google Drive and copy the segment AFTER `/folders/` in "
            "the URL — for example, in "
            "`https://drive.google.com/drive/folders/<FOLDER_ID>` the "
            "folder ID is just the `<FOLDER_ID>` part."
        )
    return value


def validate_pubsub_topic_id(value: str) -> str:
    """Click `value_proc` callback: validate a Pub/Sub topic ID."""
    value = (value or "").strip()
    if not PUBSUB_TOPIC_ID_RE.match(value):
        raise click.BadParameter(
            "Pub/Sub topic IDs are 3-255 chars, must start with a letter, "
            "and may contain only letters, digits, and the symbols "
            "`._~+%-`. Pick something descriptive like "
            "`claude-mirror-<projectname>`."
        )
    return value


def validate_credentials_file(value: str) -> str:
    """Click `value_proc` callback: validate that `value` points at an
    OAuth-client (Desktop / Installed-app) credentials JSON.

    Rejects with a hint about the COMMON CONFUSION between a Desktop
    OAuth client (what claude-mirror needs) and a service-account key
    (what users sometimes download by mistake — they're both
    `*.json` files from the same Cloud Console area)."""
    value = (value or "").strip()
    if not value:
        raise click.BadParameter("Credentials path cannot be empty.")
    expanded = Path(value).expanduser()
    if not expanded.exists():
        raise click.BadParameter(
            f"No file at {expanded}. Download the OAuth client JSON from "
            f"Google Cloud Console -> APIs & Services -> Credentials -> "
            f"the Desktop OAuth client you created -> Download JSON, then "
            f"point this prompt at that file."
        )
    if not expanded.is_file():
        raise click.BadParameter(
            f"{expanded} exists but is not a regular file."
        )
    try:
        with expanded.open("r", encoding="utf-8") as fh:
            data = _json.load(fh)
    except OSError as e:
        raise click.BadParameter(
            f"Could not open {expanded}: {e}"
        ) from e
    except _json.JSONDecodeError as e:
        raise click.BadParameter(
            f"{expanded} is not valid JSON: {e.msg} (line {e.lineno})."
        ) from e
    if not isinstance(data, dict):
        raise click.BadParameter(
            f"{expanded} does not look like an OAuth client JSON "
            f"(top-level is not an object)."
        )
    # Service-account keys have top-level `type: "service_account"` and a
    # `private_key` field — flag those explicitly because the user
    # downloaded the wrong artifact, not a corrupted right one.
    if data.get("type") == "service_account" or "private_key" in data:
        raise click.BadParameter(
            f"{expanded} looks like a SERVICE ACCOUNT key, not an OAuth "
            f"client credential. claude-mirror authenticates AS YOU "
            f"(an OAuth Desktop app), not as a service account. In Cloud "
            f"Console, go to APIs & Services -> Credentials -> Create "
            f"Credentials -> OAuth client ID -> Application type: "
            f"Desktop app, then download THAT JSON."
        )
    if "installed" not in data or not isinstance(data["installed"], dict):
        raise click.BadParameter(
            f"{expanded} is missing the top-level `installed` block — "
            f"this is required for OAuth Desktop-app credentials. The "
            f"file may be a Web-application client (not supported); "
            f"create a fresh OAuth client with Application type "
            f"`Desktop app` and download that JSON instead."
        )
    if not data["installed"].get("client_id"):
        raise click.BadParameter(
            f"{expanded} is missing `installed.client_id` — the OAuth "
            f"client JSON is malformed. Re-download it from Cloud Console."
        )
    return str(expanded)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Post-auth Drive smoke test
# ─────────────────────────────────────────────────────────────────────────────


class SmokeTestResult:
    """Result of `run_drive_smoke_test`. `ok` is the only field most
    callers need; the rest carry detail for the wizard's failure path."""

    __slots__ = ("ok", "reason", "raw_exception")

    def __init__(
        self,
        ok: bool,
        reason: str = "",
        raw_exception: Optional[BaseException] = None,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.raw_exception = raw_exception

    def __repr__(self) -> str:  # pragma: no cover — debug aid only
        return (
            f"SmokeTestResult(ok={self.ok!r}, reason={self.reason!r})"
        )


def run_drive_smoke_test(
    creds,
    folder_id: str,
    *,
    build_service: Optional[Callable] = None,
) -> SmokeTestResult:
    """Run a single `drive.files.list(pageSize=1)` call against
    `folder_id`. Return a `SmokeTestResult` with a human-readable reason
    on failure.

    `build_service` is an injection seam for tests so they don't have to
    reach into `googleapiclient.discovery` to stub a service object.
    Production callers leave it as None.

    Failure modes this surfaces:
      * Drive API not enabled in the GCP project (HttpError with
        "has not been used in project")
      * Folder ID does not exist or the authenticating account cannot
        see it (HttpError 404, or empty result with no permission entry)
      * Network / transport error (TransportError, OSError) — counted as
        a smoke-test failure so the user can decide whether to retry now
        or write the YAML and debug later
    """
    if build_service is None:
        # Local import keeps `googleapiclient` off the import-time path
        # for callers that never run the smoke test (tests, dry-runs).
        from googleapiclient.discovery import build as _build

        def build_service(creds):  # type: ignore[no-redef]
            return _build("drive", "v3", credentials=creds)

    try:
        service = build_service(creds)
        # Wrap folder_id in an escape-safe single-quoted q clause. The
        # folder ID validator already rejects characters that need
        # escaping, but belt-and-braces.
        safe_folder = folder_id.replace("\\", "\\\\").replace("'", "\\'")
        request = service.files().list(
            pageSize=1,
            q=f"'{safe_folder}' in parents and trashed=false",
            fields="files(id,name)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        request.execute()
    except Exception as e:  # broad: classify by message / status below
        reason = _classify_smoke_failure(e, folder_id)
        return SmokeTestResult(ok=False, reason=reason, raw_exception=e)
    return SmokeTestResult(ok=True)


def _classify_smoke_failure(exc: BaseException, folder_id: str) -> str:
    """Translate a smoke-test exception into a one-sentence diagnosis
    pointing at the most likely fix."""
    text = str(exc)
    text_lower = text.lower()

    if (
        "has not been used in project" in text_lower
        or "drive.googleapis.com" in text_lower
        and "disabled" in text_lower
    ):
        return (
            "Drive API is not enabled in this GCP project. Open the "
            "'Enable Drive API' link the wizard offered earlier and click "
            "ENABLE, then retry."
        )
    if "accessnotconfigured" in text_lower or "service_disabled" in text_lower:
        return (
            "Drive API is not enabled in this GCP project (or the "
            "credentials.json is for a project where it isn't enabled). "
            "Enable the Drive API on the project tied to credentials.json."
        )
    if "file not found" in text_lower or "notfound" in text_lower:
        return (
            f"Drive folder ID `{folder_id}` was not found. The ID may be "
            f"wrong, the folder may have been deleted, or the folder is "
            f"not shared with the Google account you just authenticated."
        )
    if (
        "insufficientpermissions" in text_lower
        or "insufficient_permission" in text_lower
        or "forbidden" in text_lower
        or "permission" in text_lower
    ):
        return (
            f"The authenticated Google account does not have access to "
            f"folder `{folder_id}`. Share the folder with that account "
            f"(Editor access) in the Drive web UI, then retry."
        )
    # Network / transport / unknown — return the exception text so the
    # user can see what went wrong and decide whether to retry.
    return f"Smoke test failed: {text}"
