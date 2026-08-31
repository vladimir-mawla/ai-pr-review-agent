"""Maps a ``Finding``'s file/line to a real, postable GitHub review-comment anchor.

THE HARD PART THIS MILESTONE NAMES EXPLICITLY: an LLM-reported line number
is not guaranteed to land on a line GitHub will accept an inline comment
on. The reference implementation this project is measured against
(``ayush488-glitch/ai-pr-review-agent``) gave up on this entirely and posts
summary-only, because a single unmappable comment in a GitHub review's
``comments`` array makes the WHOLE review POST fail with 422 -- one bad
line number silently destroys every other, perfectly valid finding in the
same review. This module exists so that never happens here: every finding
is independently classified as mappable or not, and only the mappable ones
are ever attempted as inline comments (see
``backend.integrations.github_client.RealGitHubClient.post_review_comment``
for how the unmappable remainder is degraded into the summary body instead
of being dropped or allowed to 422 the whole review).

ANCHORING SCHEME: ``line`` + ``side``, not the legacy ``position`` offset.
GitHub's REST API has historically accepted two ways to anchor an inline
PR review comment:

- ``position``: an integer offset counted from the FIRST line of the
  unified diff for that file (counting every hunk header and context line,
  not just the changed ones) -- brittle by construction, since it silently
  shifts if GitHub ever changes hunk-splitting/context-line heuristics
  between the time a diff is fetched and the time a review is posted, and
  it is GitHub's own older, less-documented mechanism.
- ``line`` + ``side``: the actual line number in the file (new-file line
  number when ``side="RIGHT"``, old-file line number when
  ``side="LEFT"``), which is what GitHub's docs describe as the current,
  recommended way to create a single-line review comment, and what the
  web UI itself produces when a human leaves an inline comment. It is also
  a far more natural match for what a ``Finding`` already carries
  (``file_path`` + ``line_start``/``line_end``, meant to describe a real
  line in the file, not a diff-hunk offset) -- no extra bookkeeping is
  needed to translate "line 42 of db.py" into a diff-relative counter.

This project uses ``line`` + ``side`` for both reasons: it is the more
robust, currently-recommended scheme, and it is a direct match for the
domain model this codebase already has.

MAPPING RULE (see ``build_diff_index``/``map_finding_to_anchor`` below):
for a given file, a unified diff hunk (``@@ -old_start,old_count
+new_start,new_count @@``) is walked line by line, tracking the running
old-file and new-file line counters. Each hunk line becomes one entry in
one or both of two lookup tables for that file:

- An ADDED line (``+``) or a CONTEXT line (`` ``) is commentable on
  ``side="RIGHT"`` at its NEW-file line number (the file as it exists
  after this PR) -- this covers both "a specialist flagged a line that was
  actually changed" and "a specialist flagged a line that merely appears
  in the diff's context window".
- A REMOVED line (``-``) or a CONTEXT line is commentable on
  ``side="LEFT"`` at its OLD-file line number (the file as it existed
  before this PR) -- this is what lets a finding about code that was
  DELETED (a real, valid thing to comment on -- "this validation you just
  removed was load-bearing") still anchor somewhere.

``map_finding_to_anchor`` looks up RIGHT first, then LEFT, so an ordinary
added/context-line finding (the overwhelming common case) resolves without
even considering the old-file numbering; a finding that only matches on
the old side (a genuinely removed line) falls back to LEFT.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from backend.integrations.github_models import ChangedFile

# Matches a unified diff hunk header, e.g. "@@ -12,7 +12,9 @@ def foo():".
# The trailing "@@ <context>" suffix (a function/section name GitHub/git
# sometimes appends) is intentionally not captured -- this module has no
# use for it.
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class CommentAnchor:
    """Where a mappable finding should actually be anchored on GitHub.

    Attributes:
        path: The file path exactly as it appears in the PR's changed-files
            list (must match what GitHub itself expects in the comment
            payload).
        line: The line number in the file, interpreted per ``side``.
        side: ``"RIGHT"`` (new-file line numbering) or ``"LEFT"``
            (old-file line numbering) -- see module docstring.
    """

    path: str
    line: int
    side: str


@dataclass
class _FileDiffIndex:
    """Per-file lookup tables built by ``build_diff_index``."""

    right_lines: set[int] = field(default_factory=set)
    left_lines: set[int] = field(default_factory=set)


@dataclass
class DiffIndex:
    """The full per-PR index ``map_finding_to_anchor`` queries against.

    Built once per (fetch diff, changed files) pair and reused for every
    finding in the review -- parsing every file's patch text is done
    exactly once, not once per finding.
    """

    files: dict[str, _FileDiffIndex] = field(default_factory=dict)

    def has_file(self, path: str) -> bool:
        return path in self.files


def _parse_patch(patch: str) -> _FileDiffIndex:
    """Walk one file's unified-diff patch text, building its RIGHT/LEFT line-number sets."""
    index = _FileDiffIndex()
    old_line = 0
    new_line = 0
    in_hunk = False

    for raw_line in patch.splitlines():
        header_match = _HUNK_HEADER_RE.match(raw_line)
        if header_match is not None:
            old_line = int(header_match.group(1))
            new_line = int(header_match.group(3))
            in_hunk = True
            continue
        if not in_hunk or raw_line == "":
            continue

        marker = raw_line[0] if raw_line else ""
        if marker == "+":
            index.right_lines.add(new_line)
            new_line += 1
        elif marker == "-":
            index.left_lines.add(old_line)
            old_line += 1
        elif marker == " ":
            # A context line exists at both the old and new line numbers
            # simultaneously -- it is the SAME source line, unmoved by
            # this hunk, so it is commentable from either side.
            index.right_lines.add(new_line)
            index.left_lines.add(old_line)
            old_line += 1
            new_line += 1
        # A "\ No newline at end of file" marker (or any other non-+/-/
        # space-prefixed line git can emit inside a hunk) advances neither
        # counter -- it describes formatting, not a real source line.

    return index


def build_diff_index(changed_files: list[ChangedFile]) -> DiffIndex:
    """Build the per-PR lookup structure ``map_finding_to_anchor`` queries.

    A ``ChangedFile`` with ``patch is None`` (GitHub does not generate a
    text patch for binary files or diffs it judges too large to render)
    contributes an empty index for that file -- every finding against it
    will correctly fail to map (there is no diff content to anchor a
    comment against), never raise.
    """
    diff_index = DiffIndex()
    for changed_file in changed_files:
        if changed_file.patch is None:
            diff_index.files[changed_file.filename] = _FileDiffIndex()
            continue
        diff_index.files[changed_file.filename] = _parse_patch(changed_file.patch)
    return diff_index


def map_finding_to_anchor(
    *, file_path: str, line_start: int, diff_index: DiffIndex
) -> CommentAnchor | None:
    """Resolve one finding's ``(file_path, line_start)`` to a postable anchor, or ``None``.

    Returns ``None`` (never raises) for every way a finding can fail to
    map -- the file is not part of this PR's changed-files list at all, or
    the file IS in the PR but ``line_start`` does not correspond to any
    line the diff actually touches or shows as context. ``None`` is the
    caller's (``RealGitHubClient.post_review_comment``) signal to degrade
    this finding into the summary body instead of an inline comment -- see
    that method's docstring.
    """
    file_index = diff_index.files.get(file_path)
    if file_index is None:
        return None
    if line_start in file_index.right_lines:
        return CommentAnchor(path=file_path, line=line_start, side="RIGHT")
    if line_start in file_index.left_lines:
        return CommentAnchor(path=file_path, line=line_start, side="LEFT")
    return None
