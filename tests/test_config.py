"""Config-level invariants beyond webhook URL validation (H1).

Locks in the credential-masking contract on `repr(Config)` — every
secret-bearing field uses `field(repr=False)` so a stray
`console.print(f"... {config}")`, exception with `config` in locals
dumped to logs / Slack, or `logger.debug(config)` cannot leak the
value. Non-sensitive identifiers (project_path, backend, s3_bucket,
s3_access_key_id) MUST stay visible — over-masking them would make
debug output useless.
"""
from __future__ import annotations

from pathlib import Path

from claude_mirror.config import Config


# Sentinels are unique strings that any of them appearing in repr(c)
# would be a leak. Each per-field sentinel is distinguishable so a
# failure message points directly at the offending field.
_SENTINEL_WEBDAV_PASSWORD = "WEBDAV-PWD-SENTINEL-7K1"
_SENTINEL_SFTP_PASSWORD = "SFTP-PWD-SENTINEL-7K2"
_SENTINEL_FTP_PASSWORD = "FTP-PWD-SENTINEL-7K3"
_SENTINEL_S3_SECRET = "S3-SECRET-SENTINEL-7K4"
_SENTINEL_SMB_PASSWORD = "SMB-PWD-SENTINEL-7K5"
_SENTINEL_SLACK_URL = "https://hooks.slack.com/services/T/B/SLACK-SENTINEL-7K6"
_SENTINEL_DISCORD_URL = "https://discord.com/api/webhooks/123/DISCORD-SENTINEL-7K7"
_SENTINEL_TEAMS_URL = "https://contoso.webhook.office.com/TEAMS-SENTINEL-7K8"
_SENTINEL_GENERIC_URL = "https://my.private.webhook/GENERIC-SENTINEL-7K9"
_SENTINEL_BEARER = "Bearer-SENTINEL-7KA"


def _make_secret_config(tmp_path: Path) -> Config:
    """Build a Config with every credential-bearing field set to a
    distinguishable sentinel so a leak in repr() points at the field."""
    return Config(
        project_path=str(tmp_path),
        backend="webdav",
        drive_folder_id="folder-123-PUBLIC",
        s3_bucket="bucket-PUBLIC",
        s3_access_key_id="AKIA-PUBLIC",
        webdav_url="https://dav.example.com/x",
        webdav_username="alice-PUBLIC",
        webdav_password=_SENTINEL_WEBDAV_PASSWORD,
        sftp_password=_SENTINEL_SFTP_PASSWORD,
        ftp_password=_SENTINEL_FTP_PASSWORD,
        s3_secret_access_key=_SENTINEL_S3_SECRET,
        smb_password=_SENTINEL_SMB_PASSWORD,
        slack_enabled=True,
        slack_webhook_url=_SENTINEL_SLACK_URL,
        discord_enabled=True,
        discord_webhook_url=_SENTINEL_DISCORD_URL,
        teams_enabled=True,
        teams_webhook_url=_SENTINEL_TEAMS_URL,
        webhook_enabled=True,
        webhook_url=_SENTINEL_GENERIC_URL,
        webhook_extra_headers={"Authorization": _SENTINEL_BEARER},
    )


def test_repr_does_not_leak_any_credential_sentinel(tmp_path: Path) -> None:
    """Every credential-bearing field MUST be repr-masked. A stray
    `console.print(f"... {config}")`, exception trace dumping `config`
    in locals to a log, or a `logger.debug(config)` line — none of
    those must leak the secret."""
    cfg = _make_secret_config(tmp_path)
    rendered = repr(cfg)
    leaks = [
        ("webdav_password", _SENTINEL_WEBDAV_PASSWORD),
        ("sftp_password", _SENTINEL_SFTP_PASSWORD),
        ("ftp_password", _SENTINEL_FTP_PASSWORD),
        ("s3_secret_access_key", _SENTINEL_S3_SECRET),
        ("smb_password", _SENTINEL_SMB_PASSWORD),
        ("slack_webhook_url", _SENTINEL_SLACK_URL),
        ("discord_webhook_url", _SENTINEL_DISCORD_URL),
        ("teams_webhook_url", _SENTINEL_TEAMS_URL),
        ("webhook_url", _SENTINEL_GENERIC_URL),
        ("webhook_extra_headers", _SENTINEL_BEARER),
    ]
    for field_name, sentinel in leaks:
        assert sentinel not in rendered, (
            f"repr(Config) leaks {field_name}: sentinel {sentinel!r} "
            f"appeared in {rendered!r}"
        )


def test_repr_keeps_non_sensitive_identifiers_visible(tmp_path: Path) -> None:
    """Over-masking is wrong too — debug output must still show the
    project path, backend name, bucket, access-key ID. The secret
    half is the only thing we hide."""
    cfg = _make_secret_config(tmp_path)
    rendered = repr(cfg)
    visible_expected = [
        str(tmp_path),       # project_path
        "webdav",            # backend
        "bucket-PUBLIC",     # s3_bucket (non-sensitive identifier)
        "AKIA-PUBLIC",       # s3_access_key_id (public half of the AWS pair)
        "alice-PUBLIC",      # webdav_username (non-sensitive identifier)
        "folder-123-PUBLIC", # drive_folder_id
    ]
    for value in visible_expected:
        assert value in rendered, (
            f"repr(Config) over-masks: expected {value!r} to remain visible "
            f"in {rendered!r}"
        )


def test_repr_does_not_show_field_names_of_masked_fields(tmp_path: Path) -> None:
    """`field(repr=False)` removes both the value AND the
    `name=value` framing from repr output. Confirming the field NAMES
    (not just the sentinels) are absent prevents a future refactor
    from accidentally swapping `repr=False` for a custom mask string
    that still leaks the field's existence."""
    cfg = _make_secret_config(tmp_path)
    rendered = repr(cfg)
    # If a future change replaced `repr=False` with something like
    # `repr=lambda v: '***'`, the field name would still appear and
    # this assertion would catch it.
    for masked_field in (
        "webdav_password=",
        "sftp_password=",
        "ftp_password=",
        "s3_secret_access_key=",
        "smb_password=",
        "slack_webhook_url=",
        "discord_webhook_url=",
        "teams_webhook_url=",
        "webhook_url=",
        "webhook_extra_headers=",
    ):
        assert masked_field not in rendered, (
            f"repr(Config) still includes the masked field's name "
            f"{masked_field!r}; should be omitted entirely"
        )
