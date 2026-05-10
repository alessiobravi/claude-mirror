"""Tests for the `claude-mirror redact` pre-push secret scrubber (REDACT).

Two layers:
    1. Pure-function tests for `claude_mirror._redact` — the regex
       catalogue, scanner, replacement function, binary-file detection,
       and idempotence guarantee. No CLI, no Click, no I/O wrappers
       beyond `tmp_path` for `scan_file`.
    2. CLI-level tests that drive `claude-mirror redact` end-to-end via
       Click's CliRunner: dry-run vs --apply, --yes auto-replace,
       interactive prompt routing, [s]kip-file / [q]uit semantics, and
       the non-TTY-without-yes refusal.

All tests run offline; no real network, no real cloud I/O. Per
`feedback_no_global_time_sleep_patch.md`, the interactive path is
tested by patching `cli_module.click.prompt` to a fake callable
rather than mocking global stdin.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest
from click.testing import CliRunner

from claude_mirror import cli as cli_module
from claude_mirror import _redact
from claude_mirror._redact import (
    Finding,
    SECRET_PATTERNS,
    apply_replacements,
    scan_file,
    scan_text,
)
from claude_mirror.cli import cli

# Click 8.3 emits a DeprecationWarning from inside CliRunner.invoke that
# pyproject's filterwarnings="error" would otherwise turn into a test
# failure. Same suppression as test_stats.py / test_presence.py etc.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ─── Pure-function tests: every catalogue entry, positive + negative ──────────


def _scan(text: str) -> list[Finding]:
    """Helper: scan_text against a fixed dummy path."""
    return scan_text(text, path=Path("dummy.md"))


def test_aws_access_key_positive_matches():
    findings = _scan("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE\n")
    assert len(findings) == 1
    assert findings[0].kind == "aws-access-key"
    assert findings[0].matched_text == "AKIAIOSFODNN7EXAMPLE"
    assert findings[0].line_no == 1


def test_aws_access_key_negative_too_short():
    # 18 chars after AKIA instead of 16 — wrong shape.
    findings = _scan("AWS_ACCESS_KEY_ID=AKIAxxxxxxxxxxx\n")
    aws = [f for f in findings if f.kind == "aws-access-key"]
    assert aws == []


def test_aws_secret_key_positive_with_label():
    text = (
        'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n'
    )
    findings = _scan(text)
    secret_kinds = [f.kind for f in findings]
    assert "aws-secret-key" in secret_kinds
    aws_secret = next(f for f in findings if f.kind == "aws-secret-key")
    assert aws_secret.matched_text == "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


def test_aws_secret_key_negative_no_label():
    # 40-char body without the AWS label gate — should NOT match
    # aws-secret-key. (Generic-high-entropy may still flag it if the
    # key-shaped name is present, but aws-secret-key requires the label.)
    findings = _scan("just a 40 char string: abcdefghijabcdefghijabcdefghijabcdefghij\n")
    aws_secret = [f for f in findings if f.kind == "aws-secret-key"]
    assert aws_secret == []


def test_github_token_positive_matches():
    text = "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyzABCD\n"
    findings = _scan(text)
    gh = [f for f in findings if f.kind == "github-token"]
    assert len(gh) == 1
    assert gh[0].matched_text.startswith("ghp_")


def test_github_token_negative_too_short():
    # 35 chars after ghp_ instead of 36 — too short.
    findings = _scan("GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxy\n")
    gh = [f for f in findings if f.kind == "github-token"]
    assert gh == []


def test_slack_webhook_positive_matches():
    text = "https://hooks.slack.com/services/T01ABCDE/B01FGHIJK/abc123def456\n"
    findings = _scan(text)
    sw = [f for f in findings if f.kind == "slack-webhook"]
    assert len(sw) == 1


def test_slack_webhook_negative_wrong_host():
    # hooks.evil.com instead of hooks.slack.com
    text = "https://hooks.evil.com/services/T01ABCDE/B01FGHIJK/abc123def456\n"
    findings = _scan(text)
    sw = [f for f in findings if f.kind == "slack-webhook"]
    assert sw == []


def test_slack_bot_token_positive_matches():
    # Real Slack token shape: xoxb-T-B-S-suffix (3 dash-separated digit groups + alnum suffix).
    text = "SLACK_BOT_TOKEN=xoxb-123456789012-1234567890123-12345-aBcDeFgHiJkLmNoPqRs\n"
    findings = _scan(text)
    sb = [f for f in findings if f.kind == "slack-bot-token"]
    assert len(sb) == 1


def test_anthropic_key_takes_precedence_over_openai():
    # sk-ant- prefixed token: must register as anthropic, not openai
    # (the regex order in SECRET_PATTERNS guarantees this).
    text = "ANTHROPIC=sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz1234567890\n"
    findings = _scan(text)
    kinds = [f.kind for f in findings]
    assert "anthropic-api-key" in kinds
    # The same span must NOT also surface as openai-api-key (the span
    # dedup at scan time suppresses the duplicate).
    span_set = {(f.line_no, f.column_start, f.column_end) for f in findings}
    ant = next(f for f in findings if f.kind == "anthropic-api-key")
    openai_findings = [
        f for f in findings
        if f.kind == "openai-api-key"
        and (f.line_no, f.column_start, f.column_end) == (ant.line_no, ant.column_start, ant.column_end)
    ]
    assert openai_findings == []
    assert (ant.line_no, ant.column_start, ant.column_end) in span_set


def test_openai_key_positive_matches():
    text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz0123456789\n"
    findings = _scan(text)
    oa = [f for f in findings if f.kind == "openai-api-key"]
    assert len(oa) == 1


def test_google_api_key_positive_matches():
    # AIza + exactly 35 chars body.
    text = "GOOGLE=AIzaSyD1234567890abcdefghijklmnopqrstuv\n"
    findings = _scan(text)
    g = [f for f in findings if f.kind == "google-api-key"]
    assert len(g) == 1


def test_google_api_key_negative_wrong_length():
    # 33-char body instead of 35 — too short for the strict pattern.
    text = "GOOGLE=AIzaSyD1234567890abcdefghijklmnopqr\n"
    findings = _scan(text)
    g = [f for f in findings if f.kind == "google-api-key"]
    assert g == []


def test_gcp_service_account_key_positive_matches():
    text = (
        '{"type":"service_account","private_key":'
        '"-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhki..."}\n'
    )
    findings = _scan(text)
    g = [f for f in findings if f.kind == "gcp-service-account-key"]
    assert len(g) == 1


def test_private_key_block_positive_multiline():
    text = (
        "Some preamble.\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC...\n"
        "-----END RSA PRIVATE KEY-----\n"
    )
    findings = _scan(text)
    pk = [f for f in findings if f.kind == "private-key-block"]
    assert len(pk) == 1
    assert pk[0].line_no == 2  # the BEGIN line


def test_jwt_positive_matches():
    text = (
        "JWT=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U\n"
    )
    findings = _scan(text)
    j = [f for f in findings if f.kind == "jwt"]
    assert len(j) == 1


def test_password_assignment_positive_matches():
    text = 'DB_PASSWORD = "hunter2-very-secret"\n'
    findings = _scan(text)
    p = [f for f in findings if f.kind == "password-assignment"]
    assert len(p) == 1
    # The matched_text is the inner-group capture (the secret body), not
    # the whole assignment.
    assert p[0].matched_text == "hunter2-very-secret"


def test_password_assignment_negative_empty_value():
    # password = "" is not a finding (body must be 6+ chars).
    text = 'PASSWORD = ""\n'
    findings = _scan(text)
    p = [f for f in findings if f.kind == "password-assignment"]
    assert p == []


def test_generic_high_entropy_negative_short_body():
    # A 30-char body assigned to a key-shaped name does NOT match
    # generic-high-entropy (we require 40+).
    text = 'API_KEY = "abcdef1234567890abcdef1234567890"\n'  # 30 chars
    findings = _scan(text)
    g = [f for f in findings if f.kind == "generic-high-entropy"]
    assert g == []


# ─── Multi-finding + sort-order tests ─────────────────────────────────────────


def test_scan_text_returns_findings_in_source_order():
    text = (
        "line1: ghp_111111111111111111111111111111111111\n"
        "line2: AKIAIOSFODNN7EXAMPLE\n"
        "line3: AIzaSyD1234567890abcdefghijklmnopqrstu0\n"
    )
    findings = _scan(text)
    line_numbers = [f.line_no for f in findings]
    assert line_numbers == sorted(line_numbers)


def test_scan_text_multiple_findings_per_file():
    text = (
        "first AKIAIOSFODNN7EXAMPLE second AKIAABCDEFGHIJKLMNOP\n"
        "third ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    )
    findings = _scan(text)
    aws = [f for f in findings if f.kind == "aws-access-key"]
    assert len(aws) == 2
    gh = [f for f in findings if f.kind == "github-token"]
    assert len(gh) == 1


def test_scan_text_no_findings_on_clean_text():
    text = "# Hello\n\nJust a normal markdown file with prose.\n"
    findings = _scan(text)
    assert findings == []


# ─── apply_replacements: happy + idempotent + kept ─────────────────────────────


def test_apply_replacements_happy_path_replaces_secret():
    text = "key=AKIAIOSFODNN7EXAMPLE\n"
    findings = _scan(text)
    out = apply_replacements(text, findings)
    assert out == "key=<REDACTED:aws-access-key>\n"


def test_apply_replacements_kept_finding_is_left_alone():
    text = "key=AKIAIOSFODNN7EXAMPLE other=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
    findings = _scan(text)
    aws_finding = next(f for f in findings if f.kind == "aws-access-key")
    out = apply_replacements(text, findings, kept=[aws_finding])
    # AWS key stays, GitHub token gets redacted.
    assert "AKIAIOSFODNN7EXAMPLE" in out
    assert "<REDACTED:github-token>" in out
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in out


def test_apply_replacements_idempotent_on_already_redacted():
    text = "key=<REDACTED:aws-access-key>\n"
    findings = _scan(text)
    # Markers are excluded from findings, so apply is a no-op.
    out = apply_replacements(text, findings)
    assert out == text


def test_apply_replacements_no_findings_returns_input_unchanged():
    text = "# Just prose\n"
    out = apply_replacements(text, [])
    assert out == text


def test_apply_replacements_multiple_findings_in_one_line():
    text = "A=AKIAIOSFODNN7EXAMPLE B=AKIAABCDEFGHIJKLMNOP\n"
    findings = _scan(text)
    out = apply_replacements(text, findings)
    assert out.count("<REDACTED:aws-access-key>") == 2
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "AKIAABCDEFGHIJKLMNOP" not in out


def test_rescan_after_apply_yields_zero_findings():
    """End-to-end idempotence guarantee: applying replacements then
    re-scanning the result must surface zero findings."""
    text = (
        "AKIAIOSFODNN7EXAMPLE\n"
        "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "AIzaSyD1234567890abcdefghijklmnopqrstu0\n"
    )
    findings = _scan(text)
    assert len(findings) >= 3
    out = apply_replacements(text, findings)
    out_findings = scan_text(out, path=Path("dummy.md"))
    assert out_findings == []


# ─── scan_file: I/O + binary-file detection ───────────────────────────────────


def test_scan_file_reads_real_file(tmp_path: Path):
    p = tmp_path / "memory.md"
    p.write_text("token: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    findings = scan_file(p)
    assert len(findings) == 1
    assert findings[0].path == p
    assert findings[0].kind == "github-token"


def test_scan_file_returns_empty_for_binary_file(tmp_path: Path):
    p = tmp_path / "image.bin"
    # NUL byte in the first 8 KiB triggers the binary heuristic.
    p.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + b"\x00" * 100)
    assert scan_file(p) == []


def test_scan_file_returns_empty_for_non_utf8(tmp_path: Path):
    p = tmp_path / "weird.md"
    # Latin-1-only bytes that are NOT valid UTF-8.
    p.write_bytes(b"caf\xe9 menu\n")
    assert scan_file(p) == []


# ─── CLI tests ────────────────────────────────────────────────────────────────


def test_cli_redact_no_findings_prints_clean_message(tmp_path: Path):
    p = tmp_path / "clean.md"
    p.write_text("# Just prose\n\nNo secrets here.\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p)])
    assert result.exit_code == 0
    assert "no secrets detected" in result.output


def test_cli_redact_dry_run_prints_findings_table(tmp_path: Path):
    p = tmp_path / "dirty.md"
    p.write_text(
        "key=AKIAIOSFODNN7EXAMPLE\n"
        "token=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p)])
    assert result.exit_code == 0
    # Dry run does NOT modify the file.
    assert "AKIAIOSFODNN7EXAMPLE" in p.read_text()
    # Output includes both kinds and the "Run with --apply" hint.
    assert "aws-access-key" in result.output
    assert "github-token" in result.output
    assert "--apply" in result.output


def test_cli_redact_apply_yes_writes_back(tmp_path: Path):
    p = tmp_path / "dirty.md"
    p.write_text(
        "key=AKIAIOSFODNN7EXAMPLE\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p), "--apply", "--yes"])
    assert result.exit_code == 0
    after = p.read_text()
    assert "AKIAIOSFODNN7EXAMPLE" not in after
    assert "<REDACTED:aws-access-key>" in after


def test_cli_redact_apply_without_yes_on_non_tty_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = tmp_path / "dirty.md"
    p.write_text("key=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "_redact_stdin_isatty", lambda: False)
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p), "--apply"])
    assert result.exit_code != 0
    # Fix-hint must mention --yes (the resolution).
    assert "--yes" in result.output


def test_cli_redact_apply_with_tty_routes_through_click_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With --apply on a TTY, every finding hits click.prompt and the
    user's answer is honoured."""
    p = tmp_path / "dirty.md"
    p.write_text(
        "key=AKIAIOSFODNN7EXAMPLE\n"
        "token=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "_redact_stdin_isatty", lambda: True)
    answers = iter(["r", "k"])  # replace first finding, keep second

    def fake_prompt(*args: object, **kwargs: object) -> str:
        return next(answers)

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p), "--apply"])
    assert result.exit_code == 0
    after = p.read_text()
    # First finding (aws-access-key) replaced, second (github-token) kept.
    assert "AKIAIOSFODNN7EXAMPLE" not in after
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in after
    assert "<REDACTED:aws-access-key>" in after


def test_cli_redact_skip_file_advances_to_next_file_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p1 = tmp_path / "first.md"
    p1.write_text(
        "a=AKIAIOSFODNN7EXAMPLE\n"
        "b=AKIAABCDEFGHIJKLMNOP\n",
        encoding="utf-8",
    )
    p2 = tmp_path / "second.md"
    p2.write_text(
        "c=AIzaSyD1234567890abcdefghijklmnopqrstu0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "_redact_stdin_isatty", lambda: True)

    # On the FIRST finding of first.md, return [s]kip-file. Then on the
    # only finding of second.md, return [r]eplace.
    answers = iter(["s", "r"])

    def fake_prompt(*args: object, **kwargs: object) -> str:
        return next(answers)

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    runner = CliRunner()
    # Sort-order: scan walks paths in sorted order, so first.md is
    # before second.md alphabetically.
    result = runner.invoke(cli, ["redact", str(p1), str(p2), "--apply"])
    assert result.exit_code == 0
    # first.md left untouched (skip-file).
    assert p1.read_text() == (
        "a=AKIAIOSFODNN7EXAMPLE\n"
        "b=AKIAABCDEFGHIJKLMNOP\n"
    )
    # second.md scrubbed.
    assert "AIzaSyD1234567890abcdefghijklmnopqrstu0" not in p2.read_text()
    assert "<REDACTED:google-api-key>" in p2.read_text()


def test_cli_redact_quit_mid_loop_keeps_already_applied_changes_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    p1 = tmp_path / "first.md"
    p1.write_text("a=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    p2 = tmp_path / "second.md"
    p2.write_text("b=ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "_redact_stdin_isatty", lambda: True)
    # Replace first.md's finding, then quit before second.md.
    answers = iter(["r", "q"])

    def fake_prompt(*args: object, **kwargs: object) -> str:
        return next(answers)

    monkeypatch.setattr(cli_module.click, "prompt", fake_prompt)
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p1), str(p2), "--apply"])
    # Quit must exit non-zero so a CI / hook caller knows the user did
    # NOT clear the full slate.
    assert result.exit_code == 1
    # First file's change was already written to disk before quit.
    assert "AKIAIOSFODNN7EXAMPLE" not in p1.read_text()
    # Second file untouched.
    assert "ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in p2.read_text()


def test_cli_redact_directory_recurses_md_files(tmp_path: Path):
    sub = tmp_path / "memory"
    sub.mkdir()
    (sub / "notes.md").write_text(
        "secret=ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
        encoding="utf-8",
    )
    (tmp_path / "top.md").write_text("# Top\nNothing to see.\n", encoding="utf-8")
    # A non-md file with a secret in it must be ignored — directory
    # recursion only walks *.md.
    (tmp_path / "ignored.txt").write_text(
        "key=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(tmp_path)])
    assert result.exit_code == 0
    assert "github-token" in result.output
    # The .txt file's AKIA key must NOT show up.
    assert "aws-access-key" not in result.output


def test_cli_redact_multiple_paths_in_one_invocation(tmp_path: Path):
    p1 = tmp_path / "a.md"
    p1.write_text("k=AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    p2 = tmp_path / "b.md"
    p2.write_text("k=ghp_cccccccccccccccccccccccccccccccccccc\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", str(p1), str(p2)])
    assert result.exit_code == 0
    # Both paths' findings appear in the table.
    assert "aws-access-key" in result.output
    assert "github-token" in result.output


def test_cli_redact_help_documents_dry_run_default():
    runner = CliRunner()
    result = runner.invoke(cli, ["redact", "--help"])
    assert result.exit_code == 0
    # --apply flag present + dry-run mention.
    assert "--apply" in result.output
    assert "dry-run" in result.output.lower() or "safe default" in result.output.lower()
    # Kind catalogue surfaced in --help so the user can grok it without
    # opening docs.
    assert "aws-access-key" in result.output
    assert "github-token" in result.output


def test_secret_patterns_have_unique_kinds():
    """Sanity: every entry's kind must be unique. A duplicate would
    silently shadow the second pattern at scan time (the catalogue
    dedup is span-based, not kind-based)."""
    kinds = [k for k, _ in SECRET_PATTERNS]
    assert len(kinds) == len(set(kinds))


def test_finding_dataclass_is_frozen_and_hashable():
    """Finding must be frozen + hashable so the kept-set logic in the
    CLI prompt loop works (`set[Finding]` is constructed there)."""
    f = Finding(
        path=Path("a.md"),
        line_no=1,
        kind="aws-access-key",
        column_start=0,
        column_end=20,
        raw_line="key=AKIAIOSFODNN7EXAMPLE",
        matched_text="AKIAIOSFODNN7EXAMPLE",
    )
    # Must hash without raising — frozen dataclass gives __hash__ for free.
    assert hash(f) == hash(f)
    with pytest.raises(Exception):
        f.kind = "other"  # type: ignore[misc]
