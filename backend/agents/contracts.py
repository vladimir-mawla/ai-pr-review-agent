"""The aggregation contract: merges multiple specialists' Finding lists into one.

Owns: ``dedupe_findings``, the pure-Python core of M5's aggregator outcome
("merges the four specialists' finding lists into one Review... dedupe
findings that multiple agents raised on the same (file, line), keeping the
one that matters most"). No LLM call, no I/O, fully deterministic --
testable with plain fixtures (``tests/unit/test_aggregator.py``), exactly
as M5's outcome requires ("all testable without any LLM").

Dedup key: ``(file_path, line_start)`` -- an exact match on both. Two
findings on the same file at *different* lines, or at the same line number
in *different* files, are never considered duplicates; only an agent raising
essentially the same issue at the identical location is.

KNOWN DEFERRED GAP (L4 VERIFY, M5 REJECT round): a wide-span finding (e.g.
``line_start=42, line_end=80``) collapses into any other finding that
merely shares ``line_start=42``, even one with an unrelated, much narrower
span. Widening the key to ``(file_path, line_start, line_end)`` was
considered and deliberately rejected for this fix -- it does not solve the
general "do these two findings actually overlap" problem (two spans can
overlap without sharing either endpoint) and was explicitly called out as
out of scope by the user. Tracked in ``.genesis/checkpoints/CURRENT.md``'s
Deferred section; not fixed here.

Deterministic tie-break, in order (SEVERITY FIRST -- see the L4 VERIFY
REJECT this ordering fixes, below):
1. Higher ``severity`` wins, ranked by ``backend.models.enums.SEVERITY_RANK``
   (CRITICAL > HIGH > MEDIUM > LOW > INFO) -- **not** confidence, and
   **not** the two findings' arrival order. This is the fix for a real
   safety bug an independent L4 VERIFY session caught and rejected M5 for:
   the previous rule ("highest confidence wins", severity ignored) let a
   SECURITY agent's CRITICAL finding (confidence 0.751) be discarded in
   favor of a DOCS agent's unrelated INFO finding at the same file/line
   that merely had a marginally higher confidence (0.752) -- and because
   ``backend.hitl.queue.has_critical_finding`` only ever sees the
   *post-dedupe* list, the CRITICAL finding vanished silently and the
   review auto-posted instead of requiring human review. A CRITICAL
   finding must never lose a dedup collision to a lower-severity one,
   regardless of either finding's confidence.
2. Within the *same* severity: higher ``confidence`` wins (the previous
   rule, now demoted to the second tie-break level instead of the first).
3. On an exact confidence tie too: the agent earlier in
   ``backend.agents.base_agent.AGENT_PRECEDENCE`` wins (SECURITY beats
   QUALITY beats TESTS beats DOCS) -- see that module's docstring for why.
4. On an agent-type tie as well (two findings from the *same* specialist at
   the same key -- not expected from M4/M5's one-finding-per-agent stubs,
   but not ruled out for a future real agent that could emit more than one
   finding per node): lexicographically smaller ``category`` wins, then
   lexicographically smaller ``rationale`` wins. These last two steps exist
   purely so the result never depends on the *input list's* order (which,
   coming out of a parallel LangGraph fan-out, is not itself guaranteed) --
   every comparison is over fields intrinsic to the findings being compared,
   never over "which one arrived first". This is verified for the whole
   chain, not just the old confidence-only levels: dedup output is
   permutation-invariant even when a CRITICAL and a higher-confidence
   lower-severity finding collide (see
   ``tests/unit/test_aggregator.py::TestSeverityBeforeConfidenceDedupe``).
"""

from __future__ import annotations

from backend.agents.base_agent import AGENT_PRECEDENCE
from backend.models import Finding
from backend.models.enums import SEVERITY_RANK


def _agent_rank(finding: Finding) -> int:
    """Position of ``finding``'s agent in AGENT_PRECEDENCE (lower = wins ties)."""
    try:
        return AGENT_PRECEDENCE.index(finding.agent_type)
    except ValueError:
        # Every AgentType is listed in AGENT_PRECEDENCE today; this branch
        # only guards against the two constants silently drifting apart in
        # the future (e.g. a new AgentType added to the enum but not to the
        # tuple), by ranking an unranked agent last rather than crashing.
        return len(AGENT_PRECEDENCE)


def _severity_rank(finding: Finding) -> int:
    """Rank of ``finding``'s severity via ``SEVERITY_RANK`` (lower = more severe = wins).

    Deliberately goes through the explicit rank map rather than the enum's
    declaration order or member name -- see ``Severity``'s docstring in
    ``backend.models.enums`` for why relying on either of those would be
    an accident waiting to break.
    """
    return SEVERITY_RANK[finding.severity]


def _tie_break_key(finding: Finding) -> tuple[int, str, str]:
    """Deterministic ranking used once two same-severity findings' confidences are equal.

    Lower tuple wins (used as "is finding strictly better than current").
    """
    return (_agent_rank(finding), finding.category, finding.rationale)


def _is_better(candidate: Finding, current: Finding) -> bool:
    """True if ``candidate`` should replace ``current`` at the same dedup key.

    Severity is compared FIRST, ahead of confidence: a higher-severity
    finding always wins the dedup collision, even against a
    lower-severity finding with higher confidence. This is the exact fix
    for the M5 L4 VERIFY REJECT (see module docstring) -- confidence and
    the rest of the tie-break chain only ever run to break a tie *within*
    the same severity, never to override it.
    """
    if candidate.severity != current.severity:
        return _severity_rank(candidate) < _severity_rank(current)
    if candidate.confidence != current.confidence:
        return candidate.confidence > current.confidence
    return _tie_break_key(candidate) < _tie_break_key(current)


def dedupe_findings(findings: list[Finding]) -> list[Finding]:
    """Collapse findings sharing a ``(file_path, line_start)`` key to one each.

    Returns the surviving findings sorted by ``(file_path, line_start)`` for
    a stable, reproducible order -- the aggregator's output must not depend
    on the order specialists happened to finish in during the parallel
    fan-out (see ``backend.orchestrator.graph``).

    An empty input returns an empty list (handled the same way as any other
    input -- no special-casing needed since the loop below simply does
    nothing).
    """
    best: dict[tuple[str, int], Finding] = {}
    for finding in findings:
        key = (finding.file_path, finding.line_start)
        current = best.get(key)
        if current is None or _is_better(finding, current):
            best[key] = finding
    return [best[key] for key in sorted(best)]
