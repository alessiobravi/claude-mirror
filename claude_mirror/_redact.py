"""Pure regex-catalogue + scanner for `claude-mirror redact`.

The motivating scenario: a user accidentally pastes an API key into a
`CLAUDE.md` or memory file and is about to push it to Drive / S3 /
wherever. `claude-mirror redact PATH` scans markdown files for
likely-secret patterns and offers an interactive replace-with-placeholder
flow so the secret never leaves the machine.

This module is the pure layer: a frozen `Finding` dataclass, a regex
catalogue (`SECRET_PATTERNS`), and three pure functions:

    * `scan_text(text, *, path)` — string in, sorted findings out.
    * `scan_file(path)` — wraps `scan_text` with file I/O. Skips binary
      files (returns []).
    * `apply_replacements(text, findings, *, kept=())` — returns the text
      with each non-`kept` finding replaced by `<REDACTED:{kind}>`.
      Idempotent: already-redacted text is a no-op.

NO Click, NO Rich, NO interactive logic, NO file mutation — the CLI
layer in `cli.py::redact` handles those concerns. Keeping the regex +
scanning purely string-shaped means the test suite can drive every
pattern with a one-line ``scan_text("...", path=Path("x.md"))`` call.

The starting kind catalogue is:

    aws-access-key            AKIA-style 16-char access key IDs
    aws-secret-key            base64-y 40-char secrets bound to a label
    github-token              ghp_ / gho_ / ghs_ / ghu_ / ghr_ + 36 chars
    slack-webhook             https://hooks.slack.com/services/T*/B*/<token>
    slack-bot-token           xoxb- / xoxp- / xoxa- / xoxr- + suffix
    openai-api-key            sk- prefix + 20+ alnum
    anthropic-api-key         sk-ant- prefix + alnum (incl. dashes)
    google-api-key            AIza... 35-char Google API key
    gcp-service-account-key   private_key field inside a service-account
                              JSON pasted into markdown
    private-key-block         -----BEGIN [...] PRIVATE KEY----- block
    jwt                       eyJ-prefixed three-segment dotted token
    password-assignment       PASSWORD = "..." / api_key: "..." style
                              high-confidence assignments
    generic-high-entropy      40+ char base64-y / hex-y string assigned to
                              a key-shaped name (lowest confidence; gated
                              by the surrounding `=` or `:` token)

The catalogue is deliberately a high-confidence subset. Expanding to
private-config-file specific patterns, .env probes, certificate bodies,
or third-party SaaS tokens (Stripe, Square, Twilio …) is a follow-up
release — pick patterns that match cleanly without flooding the user
with false positives on every README and snippet block.

The replacement marker is always `<REDACTED:{kind}>`; the CLI layer
guarantees that a re-scan after `apply_replacements(...)` finds zero
new secrets (markers do not match any regex in the catalogue).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


# ─── Finding dataclass ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """One secret-shaped match inside a file.

    Frozen so a `set[Finding]` / `dict[Finding, ...]` is well-defined and
    so the CLI layer can hash a "kept" set against the live findings list.
    """

    path: Path
    line_no: int  # 1-based
    kind: str
    column_start: int  # 0-based inclusive
    column_end: int    # 0-based exclusive
    raw_line: str      # the full source line (no trailing newline)
    matched_text: str  # the secret-shaped slice of `raw_line`


# ─── Regex catalogue ────────────────────────────────────────────────────────
#
# Each entry is `(kind, compiled_pattern)`. Patterns are anchored by
# the secret shape itself, NOT by line boundaries — `scan_text` walks
# every line independently and surfaces every non-overlapping match
# from each pattern. Order matters only for ties: when two patterns
# match the same span, the first one wins (the catalogue lists
# higher-confidence kinds before lower-confidence ones).

# The "private-key-block" pattern works across multiple lines, so the
# scanner runs it on the whole `text` blob and reports the FIRST line
# where the BEGIN marker appears. Every other pattern is single-line.

SECRET_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
    # AWS access key ID — fixed 20-char shape starting AKIA / ASIA / AIDA
    # / AGPA / AROA / AIPA / ANPA / ANVA / ASCA / APKA. Most public docs
    # focus on AKIA + ASIA; the others are rare enough that we accept
    # the cleanest pattern and miss the long tail (`generic-high-entropy`
    # backstops the rare prefixes).
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),

    # AWS secret key — 40-char base64-ish, gated by an `aws_secret_access_key`
    # / `AWS_SECRET_ACCESS_KEY` style label so we don't flag every random
    # 40-char hash. Bare 40-char strings without the label fall through
    # to `generic-high-entropy` (which has its own gate).
    (
        "aws-secret-key",
        re.compile(
            r"(?i)\baws[_-]?secret[_-]?access[_-]?key\b"
            r"\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})\b"
        ),
    ),

    # GitHub fine-grained / classic / OAuth tokens. ghp/gho/ghu/ghs/ghr
    # prefix + 36 alphanumeric chars. The 36 figure is from GitHub's docs
    # for the v2 token format.
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),

    # Slack incoming-webhook URL — fully qualified shape. Highly specific
    # so false positives on this one are essentially zero.
    (
        "slack-webhook",
        re.compile(r"\bhttps://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+\b"),
    ),

    # Slack bot / user / app / refresh tokens. xoxb-/xoxp-/xoxa-/xoxr-
    # prefix + dash-separated digits and a hex-ish suffix.
    (
        "slack-bot-token",
        re.compile(r"\bxox[baprs]-[0-9]+-[0-9]+-[0-9]+-[A-Za-z0-9]+\b"),
    ),

    # Anthropic — must come BEFORE openai-api-key because both start
    # `sk-` and the openai pattern would otherwise greedy-match the
    # anthropic prefix. Anthropic keys are sk-ant-... .
    (
        "anthropic-api-key",
        re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b"),
    ),

    # OpenAI — sk- prefix + 20+ alphanumeric (legacy + project keys).
    # The catalogue accepts a permissive shape; OpenAI has rotated key
    # formats several times and a too-strict pattern would miss new ones.
    ("openai-api-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),

    # Google API key — AIza prefix + 35 alphanumeric chars (browser /
    # mobile / server keys all share this shape). Distinct from OAuth
    # client secrets, which we do NOT match (too generic).
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),

    # GCP service-account JSON: the giveaway is the literal
    # "private_key": "-----BEGIN PRIVATE KEY-----" field. Match the
    # JSON-key form so a service-account file pasted as a fenced code
    # block in markdown is caught even if the BEGIN marker has
    # `\n` -> literal `\n` escapes (single-line JSON form).
    (
        "gcp-service-account-key",
        re.compile(r'"private_key"\s*:\s*"-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    ),

    # PEM private key block — the literal BEGIN marker. The scanner
    # treats this as a multi-line pattern and reports the line of the
    # BEGIN marker.
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP |)PRIVATE KEY-----"),
    ),

    # JWT — three base64url segments separated by dots, header opens
    # with `eyJ` (the base64 of `{"`). 20+ chars per segment to keep
    # short examples like `a.b.c` from matching.
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
    ),

    # PASSWORD / api_key / secret / token style assignments — any name
    # whose CASE-INSENSITIVE form ENDS with `password`, `api_key`,
    # `apikey`, `secret`, `token`, or `auth`, followed by `=` or `:` and
    # a quoted non-empty string. A 6+ char body keeps the noise floor
    # down (`token: ""` is not a finding; `token: "hunter2"` is). The
    # leading `(?:^|[^A-Za-z0-9])` (rather than `\b`) lets us match
    # underscore-glued names like `DB_PASSWORD` where a Python `\b` would
    # not see a boundary between `_` and the keyword.
    (
        "password-assignment",
        re.compile(
            r"(?i)(?:^|[^A-Za-z0-9])"
            r"(?:[A-Za-z0-9_]*?)"
            r"(?:password|passwd|api[_-]?key|secret|token|auth)\b"
            r"\s*[:=]\s*['\"]([^'\"\n\r]{6,})['\"]"
        ),
    ),

    # Generic high-entropy fallback — 40+ char base64/hex string assigned
    # to a key-shaped name. We require the surrounding `key|secret|token`
    # name + `=`/`:` so a long markdown hash example doesn't trigger.
    # Lower confidence than every catalogue entry above; the CLI layer
    # surfaces the kind so a user can keep it if it's a false positive.
    # Leading `(?:^|[^A-Za-z0-9])` (rather than `\b`) so underscore-glued
    # names like `MY_API_KEY` are recognised — Python's `\b` does not
    # treat the `_` boundary as word-vs-nonword.
    (
        "generic-high-entropy",
        re.compile(
            r"(?i)(?:^|[^A-Za-z0-9])"
            r"[A-Za-z_][A-Za-z0-9_]*(?:key|secret|token|cred(?:ential)?s?)\b"
            r"\s*[:=]\s*['\"]?([A-Za-z0-9+/=_\-]{40,})['\"]?"
        ),
    ),
)


# Patterns whose match span covers the FULL match. The other patterns
# carry an inner capture group that is the actual secret body (the
# label / quote characters live outside the secret). For findings on
# capture-group patterns, we surface the capture span rather than the
# whole-match span so the prompt arrow points at the secret itself.
_PATTERNS_WITH_INNER_GROUP: frozenset[str] = frozenset({
    "aws-secret-key",
    "password-assignment",
    "generic-high-entropy",
})


# Maximum bytes inspected when sniffing a file for binary content.
# Identical to git's "is_binary" heuristic: if the first 8 KiB of the
# file contains a NUL byte, treat it as binary. Default raised from
# 1024 → 8192 to match git; the test suite uses 1024.
_BINARY_SNIFF_BYTES = 8192


# ─── Marker / re-scan idempotence ───────────────────────────────────────────

# Replacement marker shape. Kept in sync with the regex below so a
# re-scan after `apply_replacements(...)` is guaranteed to be a no-op:
# the marker uses `<` / `>` characters which none of the catalogue
# patterns accept inside a captured secret (every catalogue pattern
# constrains its body to base64 / hex / alphanumeric / underscores +
# dashes — never angle brackets).
_MARKER_RE = re.compile(r"<REDACTED:[a-z0-9\-]+>")


def _make_marker(kind: str) -> str:
    """Return the replacement string for a finding of the given kind."""
    return f"<REDACTED:{kind}>"


# ─── Public scanning API ────────────────────────────────────────────────────


def scan_text(text: str, *, path: Path) -> list[Finding]:
    """Scan a string for likely-secret matches.

    Walks each catalogue pattern over `text`; single-line patterns are
    applied per-line so the line number is exact. Multi-line patterns
    (currently only `private-key-block`) are matched against the whole
    blob and report the line of the BEGIN marker.

    Findings are de-duplicated on `(line_no, column_start, column_end,
    kind)` so a pattern that fires twice on the same span (e.g. via
    overlapping catalogue entries) only surfaces once.

    Returns findings sorted by `(line_no, column_start)` so the CLI's
    interactive prompt walks them in source order.
    """
    out: list[Finding] = []
    seen: set[tuple[int, int, int, str]] = set()
    # Span-level dedup: when two catalogue patterns hit the EXACT same
    # span, the higher-confidence kind (earlier in SECRET_PATTERNS) wins
    # and the duplicate is suppressed. Most common case: a label-gated
    # `aws-secret-key` body also matches the lower-confidence
    # `generic-high-entropy` regex; the user wants ONE finding, not two.
    span_taken: set[tuple[int, int, int]] = set()

    lines = text.splitlines()

    for kind, pattern in SECRET_PATTERNS:
        # `private-key-block` is the only multi-line catalogue entry —
        # match it on the whole blob, then map the BEGIN marker offset
        # back to a 1-based line number.
        if kind == "private-key-block":
            for m in pattern.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                # raw_line is the line containing the BEGIN marker.
                raw_line = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
                # column_start / end are 0-based offsets within raw_line.
                line_start_offset = (
                    sum(len(lines[i]) + 1 for i in range(line_no - 1))
                    if line_no - 1 < len(lines)
                    else m.start()
                )
                col_start = max(0, m.start() - line_start_offset)
                col_end = min(len(raw_line), col_start + (m.end() - m.start()))
                matched_text = raw_line[col_start:col_end]
                key = (line_no, col_start, col_end, kind)
                if key in seen:
                    continue
                span_key = (line_no, col_start, col_end)
                if span_key in span_taken:
                    continue
                seen.add(key)
                span_taken.add(span_key)
                out.append(Finding(
                    path=path,
                    line_no=line_no,
                    kind=kind,
                    column_start=col_start,
                    column_end=col_end,
                    raw_line=raw_line,
                    matched_text=matched_text,
                ))
            continue

        # Single-line patterns: walk lines one by one for exact line numbers.
        for idx, raw_line in enumerate(lines):
            for m in pattern.finditer(raw_line):
                if kind in _PATTERNS_WITH_INNER_GROUP and m.lastindex:
                    col_start = m.start(1)
                    col_end = m.end(1)
                    matched_text = m.group(1)
                else:
                    col_start = m.start()
                    col_end = m.end()
                    matched_text = m.group(0)
                # Skip if the matched text is already a redaction marker
                # (single-line marker rescan is a no-op).
                if _MARKER_RE.fullmatch(matched_text):
                    continue
                line_no = idx + 1
                key = (line_no, col_start, col_end, kind)
                if key in seen:
                    continue
                span_key = (line_no, col_start, col_end)
                if span_key in span_taken:
                    continue
                seen.add(key)
                span_taken.add(span_key)
                out.append(Finding(
                    path=path,
                    line_no=line_no,
                    kind=kind,
                    column_start=col_start,
                    column_end=col_end,
                    raw_line=raw_line,
                    matched_text=matched_text,
                ))

    out.sort(key=lambda f: (f.line_no, f.column_start, f.kind))
    return out


def scan_file(path: Path) -> list[Finding]:
    """Read `path` from disk and scan it for likely-secret matches.

    Skips binary files (returns `[]`) when the first `_BINARY_SNIFF_BYTES`
    of the file contain a NUL byte — same heuristic as git's
    "is_binary" check. Files that don't exist or can't be read raise
    `FileNotFoundError` / `PermissionError` etc. — the CLI layer wraps
    those into a friendly skip line.
    """
    raw = path.read_bytes()
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Treat as binary if it isn't UTF-8 either — markdown is utf-8
        # by convention; non-UTF-8 files are out of the scrubber's scope.
        return []
    return scan_text(text, path=path)


def apply_replacements(
    text: str,
    findings: Iterable[Finding],
    *,
    kept: Iterable[Finding] = (),
) -> str:
    """Return `text` with each non-`kept` finding's matched_text replaced.

    The replacement is `<REDACTED:{kind}>`. Findings are applied in
    reverse-source-order so each replacement does not shift the
    column offsets of later findings.

    If a finding's matched_text is already a redaction marker, the
    replacement is a no-op for that finding (idempotence guarantee:
    re-running the replacer on already-redacted text never grows the
    output).
    """
    kept_set = set(kept)
    actionable = [f for f in findings if f not in kept_set]
    # Sort descending by (line_no, column_start) so each replacement
    # does not invalidate the line/column offsets of later ones.
    actionable.sort(key=lambda f: (f.line_no, f.column_start), reverse=True)

    if not actionable:
        return text

    # Walk each line independently — the catalogue is per-line for every
    # entry except `private-key-block`, which we handle explicitly below.
    lines = text.splitlines(keepends=True)

    for f in actionable:
        if _MARKER_RE.fullmatch(f.matched_text):
            continue  # already-redacted: idempotence
        idx = f.line_no - 1
        if idx < 0 or idx >= len(lines):
            continue
        line = lines[idx]
        # The line in `lines` may include the trailing `\n`; the finding's
        # raw_line does not. Compute trailing-newline-aware slicing.
        trailing_nl = "\n" if line.endswith("\n") else ""
        line_body = line[:-1] if trailing_nl else line
        # Defensive: ensure we replace at the recorded span rather than
        # blindly substituting the matched_text via str.replace, which
        # would also rewrite incidental matches elsewhere on the line.
        if (
            f.column_start < 0
            or f.column_end > len(line_body)
            or f.column_start >= f.column_end
        ):
            # Span no longer fits the line (file mutated under us); skip.
            continue
        if line_body[f.column_start:f.column_end] != f.matched_text:
            # Span no longer carries the original secret. Skip rather than
            # corrupt the line.
            continue
        new_line_body = (
            line_body[:f.column_start]
            + _make_marker(f.kind)
            + line_body[f.column_end:]
        )
        lines[idx] = new_line_body + trailing_nl

    return "".join(lines)
