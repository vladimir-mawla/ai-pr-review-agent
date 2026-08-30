"""Shared contract every specialist agent implements, plus their canonical order.

Owns two things that both belong on the "what is an agent" contract rather
than on any one consumer of it:

1. ``BaseAgent`` -- the abstract shape a specialist (SECURITY, QUALITY,
   TESTS, DOCS) implements: given a diff, produce a list of schema-valid
   ``Finding`` objects. M5 does not build a real agent (no LLM calls -- see
   M8); the four specialists in ``backend.orchestrator.nodes`` remain M4's
   canned stubs. This class exists now so M8's real agents have a stable
   interface to implement, and so M5's aggregator (below) has a canonical
   place to source agent ordering from rather than inventing its own.

2. ``AGENT_PRECEDENCE`` -- the single canonical ranking of specialist agents,
   most-authoritative first. This is a project-wide constant, not a private
   detail of the aggregator: anything that needs to break a tie between two
   agents (today: ``backend.agents.contracts.dedupe_findings``'s equal-
   confidence tie-break; potentially, in a later milestone, a dashboard
   sorting findings by agent) must use this one ordering so the answer is
   always the same regardless of which caller asks. Defining it here, next
   to ``BaseAgent``, keeps "what agents exist and how are they ranked" in
   one place instead of duplicated as a magic tuple inside the aggregator.

Why SECURITY > QUALITY > TESTS > DOCS: a missed security issue is the
costliest kind of finding to lose to a coin-flip tie-break, and a missed
docs issue is the cheapest -- so when two agents disagree at the exact same
confidence on the exact same line, the more consequential agent's finding is
the one that survives.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from backend.models import AgentType, Finding

AGENT_PRECEDENCE: tuple[AgentType, ...] = (
    AgentType.SECURITY,
    AgentType.QUALITY,
    AgentType.TESTS,
    AgentType.DOCS,
)


class BaseAgent(ABC):
    """Abstract specialist agent: analyzes a diff and returns Findings.

    Not instantiated or subclassed anywhere in M5 -- the four specialist
    nodes wired into the graph (``backend.orchestrator.nodes``) are still
    M4's canned stubs, and M5 builds no real LLM-backed agent (that is M8's
    scope). This class is the forward-looking contract M8's real agents will
    implement, defined now so the aggregator's tie-break logic has a stable,
    named ``AGENT_PRECEDENCE`` to import instead of a private magic tuple.
    """

    agent_type: AgentType

    @abstractmethod
    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        """Analyze a unified diff and return this specialist's findings.

        Real implementations (M8+) call an LLM and validate its output
        against the ``Finding`` schema before returning; this method's
        contract does not change based on how a concrete subclass produces
        its findings.

        ``review_id`` (M8 addition -- keyword-only, defaulted, so this is a
        backward-compatible extension of the contract M5 originally wrote):
        an optional correlation id a real, LLM-backed implementation passes
        through to its LLM client so a resulting ``llm.call`` event
        (``backend.observability.emit_llm_call``) can be attributed to the
        review it belongs to. ``None`` for a caller with no review to
        correlate against (e.g. an ad hoc CLI invocation) -- implementations
        must treat that as "don't emit a correlated event", not as an error.
        """
        raise NotImplementedError
