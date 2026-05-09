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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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
    creds: Any,
    folder_id: str,
    *,
    build_service: Optional[Callable[[Any], Any]] = None,
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

        def build_service(creds: Any) -> Any:
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


# ─────────────────────────────────────────────────────────────────────────────
# 4. Auto-create Pub/Sub topic + subscription + IAM grant (since v0.5.47)
# ─────────────────────────────────────────────────────────────────────────────
#
# Once the user has authenticated and granted both Drive AND Pub/Sub OAuth
# scopes, claude-mirror has the credentials to create Pub/Sub resources
# itself. The three steps are:
#
#   1. Create the topic at projects/{gcp_project_id}/topics/{pubsub_topic_id}
#   2. Create a per-machine subscription at the canonical
#      `{topic}-{machine_safe}` name pattern (lifted from
#      `Config.subscription_id`).
#   3. Grant `roles/pubsub.publisher` to Drive's push-notification
#      service account on the topic. THIS is the killer detail — about
#      70% of self-serve Drive setups silently miss this grant. The
#      service-account constant lives in `cli.py` as
#      `_DRIVE_PUBSUB_PUBLISHER_SA` and is imported here lazily so this
#      module stays import-light at the top of the file.
#
# All three steps are idempotent: AlreadyExists / a binding that's
# already in the policy is treated as success-already-converged. The
# function returns a structured `AutoSetupResult` so the wizard can emit
# a nicely-formatted Rich table without re-running the inspections.
#
# `pubsub_v1` is lazy-imported INSIDE the function so callers who never
# pass `--auto-pubsub-setup` pay no extra import cost.


# Pub/Sub OAuth scope identifier — duplicated from cli.py to keep this
# module import-light. The string is part of Google's stable public
# OAuth surface; it doesn't change.
_PUBSUB_OAUTH_SCOPE = "https://www.googleapis.com/auth/pubsub"


@dataclass
class AutoSetupResult:
    """Structured result of `auto_setup_pubsub`.

    Field semantics (load-bearing for the wizard's output formatter):

      * skipped       — True iff we declined to run any Pub/Sub admin
                        calls (e.g. the Pub/Sub OAuth scope was not
                        granted at auth time). When True, `reason` is
                        the user-facing yellow info line.
      * topic_created — True iff `create_topic` returned a fresh topic
                        on this run. False means the topic already
                        existed (AlreadyExists swallowed) — DO NOT
                        report it as "created" in that case.
      * subscription_created — same semantics for the per-machine
                        subscription.
      * iam_grant_added — True iff this run actually appended the
                        `roles/pubsub.publisher` binding for Drive's
                        service account to the topic policy. False
                        means the binding was already present.
      * failures      — list of `(step_name, error_message)` tuples for
                        any step that raised. The wizard prints these
                        as yellow warnings BUT does not abort — the
                        YAML still writes; the user can fix the
                        underlying cause and re-run later.
    """

    skipped: bool = False
    reason: str = ""
    topic_created: bool = False
    subscription_created: bool = False
    iam_grant_added: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)


def auto_setup_pubsub(
    creds: Any,
    gcp_project_id: str,
    pubsub_topic_id: str,
    machine_name: str,
) -> AutoSetupResult:
    """Idempotently create the Pub/Sub topic + per-machine subscription
    + IAM grant for Drive's service account.

    Parameters mirror the values the wizard already has in scope:
      * `creds`            — OAuth credentials returned by the Drive
                             backend's `authenticate()` call. Must have
                             the Pub/Sub scope; if missing, the function
                             returns `skipped=True` without making any
                             RPC.
      * `gcp_project_id`   — GCP project that owns the topic.
      * `pubsub_topic_id`  — Topic short ID (NOT the full path).
      * `machine_name`     — Used to build the subscription suffix via
                             the same dot/space → dash transform as
                             `Config.subscription_id`. Each machine
                             gets its own subscription so notifications
                             fan out independently per-host.

    Returns an `AutoSetupResult`; the caller is responsible for
    rendering it (the wizard prints a Rich table; tests inspect the
    fields directly).
    """
    result = AutoSetupResult()

    # ── Step 0: Verify the Pub/Sub OAuth scope was granted at auth time
    # The smoke test we just ran needed only the Drive scope; Pub/Sub
    # admin calls (create_topic / set_iam_policy) require the dedicated
    # Pub/Sub scope. A user who skipped that scope at the consent screen
    # sees ONE friendly skip-line here rather than a cascade of opaque
    # PermissionDenied / Unauthenticated errors deeper in the SDK.
    granted_scopes = list(getattr(creds, "scopes", None) or [])
    if _PUBSUB_OAUTH_SCOPE not in granted_scopes:
        result.skipped = True
        result.reason = (
            "Pub/Sub scope not granted; re-run claude-mirror auth with "
            "the Pub/Sub scope to enable auto-setup."
        )
        return result

    # ── Lazy-import the Pub/Sub admin SDK + the constants we need
    # Cost is paid only on the auto-setup path. The publisher service
    # account name is imported from cli.py to avoid duplication; if cli
    # is partially imported (e.g. very early in startup) we fall back
    # to the literal string so the wizard still works.
    from google.cloud import pubsub_v1  # noqa: PLC0415
    from google.api_core import exceptions as gax_exceptions  # noqa: PLC0415

    try:
        from claude_mirror.cli import _DRIVE_PUBSUB_PUBLISHER_SA  # noqa: PLC0415
    except ImportError:  # pragma: no cover — defensive only
        _DRIVE_PUBSUB_PUBLISHER_SA = "apps-storage-noreply@google.com"

    publisher = pubsub_v1.PublisherClient(credentials=creds)
    subscriber = pubsub_v1.SubscriberClient(credentials=creds)

    topic_path = f"projects/{gcp_project_id}/topics/{pubsub_topic_id}"

    # Subscription ID derivation MUST match `Config.subscription_id`
    # exactly so `doctor` and the watcher both find the subscription
    # this function created. The pattern is `{topic_id}-{machine_safe}`
    # where machine_safe = machine_name.replace(".", "-").replace(" ", "-").lower().
    safe_machine = (
        (machine_name or "").replace(".", "-").replace(" ", "-").lower()
    )
    subscription_id = f"{pubsub_topic_id}-{safe_machine}"
    subscription_path = (
        f"projects/{gcp_project_id}/subscriptions/{subscription_id}"
    )

    # ── Step 1: Create the topic (idempotent)
    topic_create_failed = False
    try:
        publisher.create_topic(name=topic_path)
        result.topic_created = True
    except gax_exceptions.AlreadyExists:
        # Pre-existing topic — converged state, not an error.
        result.topic_created = False
    except gax_exceptions.PermissionDenied as exc:
        topic_create_failed = True
        result.failures.append((
            "create_topic",
            f"Permission denied creating topic in project "
            f"{gcp_project_id}: {exc}",
        ))
    except gax_exceptions.GoogleAPICallError as exc:  # broader API errors
        topic_create_failed = True
        result.failures.append(("create_topic", str(exc)))

    # ── Step 2: Create the per-machine subscription (idempotent)
    # If the topic step failed outright, attempting the subscription
    # would just produce a confusing "topic does not exist" error on
    # top of the already-reported permission failure. Skip in that
    # case to keep the failure list focused on root causes.
    if not topic_create_failed:
        try:
            subscriber.create_subscription(
                name=subscription_path,
                topic=topic_path,
            )
            result.subscription_created = True
        except gax_exceptions.AlreadyExists:
            result.subscription_created = False
        except gax_exceptions.PermissionDenied as exc:
            result.failures.append((
                "create_subscription",
                f"Permission denied creating subscription "
                f"{subscription_id}: {exc}",
            ))
        except gax_exceptions.GoogleAPICallError as exc:
            result.failures.append(("create_subscription", str(exc)))

    # ── Step 3: IAM grant for Drive's service account on the topic
    # Read-modify-write on the topic IAM policy. Idempotent: if the
    # binding already includes our service account, leave the policy
    # alone (don't even call set_iam_policy — pointless RPC + risk of
    # etag conflict). On etag-conflict (the proto's `Aborted` exception)
    # retry ONCE with a fresh read; second failure surfaces as a clean
    # error in `result.failures` rather than a stack trace.
    if not topic_create_failed:
        expected_member = f"serviceAccount:{_DRIVE_PUBSUB_PUBLISHER_SA}"
        attempted = 0
        max_attempts = 2
        last_exc: Optional[BaseException] = None
        while attempted < max_attempts:
            attempted += 1
            try:
                policy = publisher.get_iam_policy(
                    request={"resource": topic_path}
                )
                if _binding_already_present(policy, expected_member):
                    # Converged state — no write needed.
                    result.iam_grant_added = False
                    last_exc = None
                    break
                _append_publisher_binding(policy, expected_member)
                publisher.set_iam_policy(
                    request={"resource": topic_path, "policy": policy}
                )
                result.iam_grant_added = True
                last_exc = None
                break
            except gax_exceptions.Aborted as exc:
                # etag mismatch — someone (us, on a parallel machine?)
                # mutated the policy between our get and set. Retry
                # once with a fresh read.
                last_exc = exc
                continue
            except gax_exceptions.PermissionDenied as exc:
                last_exc = exc
                result.failures.append((
                    "iam_grant",
                    f"Permission denied updating topic IAM policy: "
                    f"{exc}",
                ))
                last_exc = None  # already recorded
                break
            except gax_exceptions.GoogleAPICallError as exc:
                last_exc = exc
                result.failures.append(("iam_grant", str(exc)))
                last_exc = None  # already recorded
                break
        if last_exc is not None:
            # Exhausted retries on Aborted — surface it cleanly.
            result.failures.append((
                "iam_grant",
                f"Topic IAM policy update kept conflicting after "
                f"{max_attempts} attempts: {last_exc}",
            ))

    return result


def _binding_already_present(policy: Any, expected_member: str) -> bool:
    """Return True iff `policy` already contains a `roles/pubsub.publisher`
    binding whose `members` list includes `expected_member`. The proto's
    `bindings` field is repeated; each binding has `role` and `members`
    attributes."""
    for binding in getattr(policy, "bindings", []) or []:
        role = getattr(binding, "role", "")
        if role != "roles/pubsub.publisher":
            continue
        members = list(getattr(binding, "members", []) or [])
        if expected_member in members:
            return True
    return False


def _append_publisher_binding(policy: Any, expected_member: str) -> None:
    """Mutate `policy` in place: ensure a `roles/pubsub.publisher`
    binding exists and includes `expected_member`. If a binding for
    that role already exists (without our member), append the member
    to its `members` list. Otherwise, append a new binding object.

    The Pub/Sub IAM proto's `Policy.bindings` is a repeated message
    field — both list-style append (`policy.bindings.append(...)`) and
    raw `Binding(role=..., members=[...])` construction work. We use
    the SDK's `Binding` class when available so the appended object
    matches the proto schema exactly.
    """
    # Try to extend an existing binding for the same role first — that
    # keeps the policy compact and avoids a duplicate-role binding.
    for binding in getattr(policy, "bindings", []) or []:
        if getattr(binding, "role", "") == "roles/pubsub.publisher":
            members = getattr(binding, "members", None)
            if members is None:
                # Some proto shapes use a tuple/list — assign fresh.
                binding.members = [expected_member]
            elif expected_member not in list(members):
                # `members` is a RepeatedField — supports `.append`.
                try:
                    members.append(expected_member)
                except AttributeError:  # pragma: no cover — list shape
                    binding.members = list(members) + [expected_member]
            return

    # No existing binding for the role — add a fresh one. Two common
    # shapes are supported by the SDK: a `Binding` proto class and a
    # plain dict-like. We append a dict; the SDK accepts that and
    # converts it on serialization. Tests can inject a list bindings
    # field and assert the appended dict.
    new_binding = {
        "role": "roles/pubsub.publisher",
        "members": [expected_member],
    }
    bindings = getattr(policy, "bindings", None)
    if bindings is None:
        policy.bindings = [new_binding]
    else:
        try:
            bindings.append(new_binding)
        except AttributeError:  # pragma: no cover — read-only shape
            policy.bindings = list(bindings) + [new_binding]
