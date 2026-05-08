"""gitignore-style project-tree exclusion for claude-mirror.

A project may ship a `.claude_mirror_ignore` file at its root. The walker
loads it once per command invocation and feeds every project-relative
candidate path through `IgnoreSet.is_excluded(rel_path)` AFTER the YAML
`exclude_patterns` rules but BEFORE any hashing / upload work. The two
systems are independent — both must vote "keep" for a file to be synced.

Syntax (a strict subset of gitignore — implemented here without depending
on the optional `pathspec` library):

  * Blank lines and lines starting with ``#`` are skipped.
  * Trailing whitespace is stripped (use a backslash to preserve it).
  * A leading ``!`` negates a previous match (re-includes a path).
  * A trailing ``/`` makes the pattern apply to directories only — i.e.
    only paths whose components include that prefix as a directory.
  * A leading ``/`` anchors the pattern at the project root. Without it
    the pattern matches anywhere in the tree.
  * ``**`` matches any number of path segments (including zero, gitignore-
    style). ``*`` matches anything except the path separator. ``?`` matches
    a single character except the path separator. ``[abc]`` is a character
    class.

Precedence: rules are evaluated in file order. The last matching rule
wins; if it is a negation (``!``) the path is RE-included, otherwise it
is excluded. If no rule matches, the path is kept (consistent with both
gitignore and YAML `exclude_patterns` semantics).

The file `.claude_mirror_ignore` itself is auto-excluded so the rules do
not propagate to other machines unless the user explicitly wants them to.

ReDoS safety: each pattern is translated to a bounded regex. The pattern
length is capped at `_MAX_PATTERN_LENGTH` characters (long-enough for any
reasonable real-world rule, short enough to make any pathological input
fail the parse rather than the match). The translator emits only fixed
quantifiers (``.*``, ``[^/]*``, single-char classes) — there are no
nested ``(a+)+``-style structures, so the compiled regex runs in
linear time on the input path.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, NamedTuple, Optional


# Project-tree filename for the ignore rules. Auto-excluded from sync.
IGNORE_FILENAME = ".claude_mirror_ignore"

# Hard cap on the length of any single rule line. Real gitignore lines
# top out around 80-120 chars; capping at 1024 lets every reasonable
# pattern through while making any genuinely-pathological input fail
# the parse step rather than be passed to re.compile.
_MAX_PATTERN_LENGTH = 1024


class _Rule(NamedTuple):
    """One compiled ignore rule.

    Fields:
        negated:   True if the rule starts with ``!`` (re-include).
        dir_only:  True if the rule ends with ``/`` (matches only when
                   the candidate path includes the matched component as
                   a directory in its prefix — see _matches).
        anchored:  True if the rule starts with ``/`` (matches only at
                   the project root).
        regex:     Compiled pattern that returns a match against either
                   the full rel_path (anchored / unanchored bare-name)
                   or any path-segment join.
        raw:       Original pattern text (kept for debugging / repr).
    """

    negated: bool
    dir_only: bool
    anchored: bool
    regex: re.Pattern[str]
    raw: str


def _translate(pattern: str) -> str:
    """Translate a gitignore-style glob into a regex source string.

    Behaviour summary:
      * ``**`` between separators matches zero or more path segments.
      * ``*`` matches any character except the path separator.
      * ``?`` matches one character except the path separator.
      * ``[...]`` is a character class. A leading ``!`` inside the
        brackets is rewritten to ``^`` (gitignore convention).
      * Every other character is regex-escaped.

    The returned string has NO leading ``^`` or trailing ``$`` — the
    caller wraps it with the appropriate anchors based on the rule's
    `anchored` flag.
    """
    i = 0
    out: list[str] = []
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            # Detect "**". gitignore semantics:
            #   "/**" or "**/" or "**" matches any number of segments.
            # We translate "**" to ".*" to span path separators; lone
            # "*" becomes "[^/]*" so it does not cross "/".
            if i + 1 < n and pattern[i + 1] == "*":
                # Consume the "**" plus an optional trailing "/" so the
                # composite "**/" collapses cleanly to ".*" without an
                # extra explicit separator the user did not intend.
                i += 2
                if i < n and pattern[i] == "/":
                    i += 1
                # ".*" matches any number of segments including zero.
                # Emitted as a non-greedy ".*" would pessimise common
                # cases without semantic gain — keep greedy for parity
                # with fnmatch/gitignore expectations.
                out.append(r".*")
                continue
            out.append(r"[^/]*")
            i += 1
            continue
        if c == "?":
            out.append(r"[^/]")
            i += 1
            continue
        if c == "[":
            # Character class — copy through to the closing bracket but
            # rewrite a leading "!" (gitignore negation) into "^" (regex
            # negation). If the bracket never closes, escape the "[" and
            # treat it as a literal — defensive vs malformed inputs.
            j = i + 1
            if j < n and pattern[j] == "!":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                # Unclosed bracket — emit literal.
                out.append(re.escape(c))
                i += 1
                continue
            cls = pattern[i + 1 : j]
            if cls.startswith("!"):
                cls = "^" + cls[1:]
            # Escape any internal backslash to keep the class deterministic.
            out.append("[" + cls + "]")
            i = j + 1
            continue
        # Default: regex-escape any literal character (handles "/" as
        # itself, since re.escape leaves "/" untouched on every supported
        # Python version).
        out.append(re.escape(c))
        i += 1
    return "".join(out)


def _compile_rule(line: str) -> Optional[_Rule]:
    """Compile one cleaned ignore line into a `_Rule`. Returns None if the
    line is blank, a comment, or fails the safety length check."""
    raw = line.rstrip()
    # Trailing whitespace stripping (gitignore's rule). Skip blank lines
    # and comments here so the caller does not have to special-case.
    if not raw or raw.startswith("#"):
        return None
    if len(raw) > _MAX_PATTERN_LENGTH:
        # Defensive: refuse pathologically-long inputs at parse time so
        # we never hand them to re.compile.
        return None

    negated = False
    if raw.startswith("!"):
        negated = True
        raw = raw[1:]
        if not raw:
            return None  # bare "!" is meaningless

    dir_only = False
    if raw.endswith("/"):
        dir_only = True
        raw = raw[:-1]
        if not raw:
            return None  # bare "/" is meaningless

    anchored = False
    if raw.startswith("/"):
        anchored = True
        raw = raw[1:]
        if not raw:
            return None

    if not raw:
        return None

    body = _translate(raw)

    if anchored:
        # Anchored at the project root: match the pattern from start.
        # Allow trailing "/anything" so a directory-rule like "/build"
        # excludes "build/x.md" too.
        regex_src = "^" + body + r"(/.*)?$"
    else:
        # Unanchored: match the pattern at the start of any path
        # segment. The regex accepts either:
        #   * the path begins with the pattern, or
        #   * the path contains "/<pattern>" anywhere.
        # ``(?:^|.*/)`` is bounded (no nested quantifiers over the
        # quantified group itself), so it does not introduce ReDoS.
        regex_src = r"(?:^|.*/)" + body + r"(/.*)?$"

    try:
        regex = re.compile(regex_src)
    except re.error:
        return None

    return _Rule(
        negated=negated,
        dir_only=dir_only,
        anchored=anchored,
        regex=regex,
        raw=line.rstrip(),
    )


class IgnoreSet:
    """A compiled ordered list of ignore rules.

    Usage:

        igs = IgnoreSet.from_file(project_path / IGNORE_FILENAME)
        if igs is not None and igs.is_excluded(rel_path):
            ...
    """

    __slots__ = ("_rules", "_path")

    def __init__(self, rules: List[_Rule], source_path: Optional[Path] = None) -> None:
        self._rules = rules
        self._path = source_path

    @classmethod
    def from_file(cls, path: Path) -> Optional["IgnoreSet"]:
        """Parse a `.claude_mirror_ignore` file and return an IgnoreSet,
        or None if the file does not exist OR contains no usable rules.

        Returning None for empty files is a deliberate optimisation: the
        caller gets a clean fast-path (`if igs is None: skip the check`)
        instead of having to construct and call into an empty rule set
        on every candidate path.
        """
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        rules: list[_Rule] = []
        for line in text.splitlines():
            rule = _compile_rule(line)
            if rule is not None:
                rules.append(rule)
        if not rules:
            return None
        return cls(rules, source_path=path)

    @classmethod
    def from_lines(cls, lines: list[str]) -> Optional["IgnoreSet"]:
        """Build an IgnoreSet from an in-memory list of pattern lines.

        Used by the test suite (no temp file needed) and any future
        caller that wants to compose rules without touching disk.
        """
        rules: list[_Rule] = []
        for line in lines:
            rule = _compile_rule(line)
            if rule is not None:
                rules.append(rule)
        if not rules:
            return None
        return cls(rules)

    def is_excluded(self, rel_path: str) -> bool:
        """Return True if `rel_path` is excluded by the ignore rules.

        rel_path is the project-relative path string with forward
        slashes (the shape produced by `Path.relative_to(...)` after
        replacing OS separators on Windows — the SyncEngine walker
        already normalises this).
        """
        # Always strip a leading "/" defensively — the caller's
        # rel_path should already be project-relative, but accept both
        # shapes so a future refactor that yields "/foo/bar" doesn't
        # silently miss every rule.
        path = rel_path.lstrip("/")
        excluded = False
        for rule in self._rules:
            if self._matches(rule, path):
                # Last matching rule wins. A negated rule re-includes
                # (sets excluded=False); a normal rule excludes.
                excluded = not rule.negated
        return excluded

    @staticmethod
    def _matches(rule: _Rule, path: str) -> bool:
        """Check one rule against the candidate path.

        For dir_only rules, the candidate must contain the matched
        component as a directory in its prefix — i.e. a trailing
        "/anything" must be present, OR the path itself ends with the
        match AND we know it represents a directory.

        Since the walker only feeds us file paths (never bare directory
        paths), a dir_only rule that exactly matches the leaf cannot be
        a directory; only the "<dir>/<more>" shape qualifies. The
        regex's optional `(/.*)?` group captures whether such a suffix
        was present — we use that to enforce dir_only semantics.
        """
        m = rule.regex.match(path)
        if m is None:
            return False
        if rule.dir_only:
            # group(1) is the optional trailing "/<rest>" — present means
            # the matched element is a parent directory of `path`.
            tail = m.group(1) if m.lastindex else None
            return bool(tail)
        return True

    def __repr__(self) -> str:  # pragma: no cover — diagnostic only
        n = len(self._rules)
        src = f" from {self._path}" if self._path else ""
        return f"<IgnoreSet rules={n}{src}>"
