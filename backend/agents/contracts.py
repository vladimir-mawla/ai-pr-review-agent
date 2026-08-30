"""The aggregation contract: merges multiple specialists' Finding lists into one.

Owns: ``dedupe_findings``, the pure-Python core of M5's aggregator outcome
("merges the four specialists' finding lists into one Review... dedupe
findings that multiple agents raised on the same (file, line), keeping the
highest confidence"). No LLM call, no I/O, fully deterministic -- testable
with plain fixtures (``tests/unit/test_aggregator.py``), exactly as M5's
outcome requires ("all testable without any LLM").

Dedup key: ``(file_path, line_start)`` -- an exact match on both. Two
findings on the same file at *different* lines, or at the same line number
in *different* files, are never considered duplicates; only an agent raising
essentially the same issue at the identical location is.

Deterministic tie-break, in order:
1. Highest ``confidence`` wins (the spec's explicit rule).
2. On an exact confidence tie: the agent earlier in
   ``backend.agents.base_agent.AGENT_PRECEDENCE`` wins (SECURITY beats
   QUALITY beats TESTS beats DOCS) -- see that module's docstring for why.
3. On an agent-type tie too (two findings from the *same* specialist at the
   same key -- not expected from M4/M5's one-finding-per-agent stubs, but
   not ruled out for a future real agent that could emit more than one
   finding per node): lexicographically smaller ``category`` wins, then
   lexicographically smaller ``rationale`` wins. These last two steps exist
   purely so the result never depends on the *input list's* order (which,
   coming out of a parallel LangGraph fan-out, is not itself guaranteed) --
   every comparison is over fields intrinsic to the findings being compared,
   never over "which one arrived first".
"""

from __future__ import annotations

from backend.agents.base_agent import AGENT_PRECEDENCE
from backend.models import Finding


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


def _tie_break_key(finding: Finding) -> tuple[int, str, str]:
    """Deterministic ranking used once two findings' confidences are equal.

    Lower tuple wins (used as "is finding strictly better than current").
    """
    return (_agent_rank(finding), finding.category, finding.rationale)


def _is_better(candidate: Finding, current: Finding) -> bool:
    """True if ``candidate`` should replace ``current`` at the same dedup key."""
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
