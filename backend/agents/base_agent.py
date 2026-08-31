"""Shared contract every specialist agent implements, plus their canonical order.

Owns three things that all belong on the "what is an agent" contract rather
than on any one consumer of it:

1. ``BaseAgent`` -- the abstract shape a specialist (SECURITY, QUALITY,
   TESTS, DOCS) implements: given a diff, produce a list of schema-valid
   ``Finding`` objects. As of M10, all four specialists in
   ``backend.orchestrator.nodes`` are real, LLM-backed implementations of
   this class (``backend.agents.{security_agent,quality_agent,test_agent,
   docs_agent}``) -- M5 built this interface before a real implementation
   existed; M8 built the first one (SECURITY); M10 builds the other three
   and, since all four now share the exact same shape (load a versioned
   prompt, optionally ground it with retrieved context, call an LLM, parse
   the response, fall back to a forced-HITL Finding on either kind of
   failure), pulls that shared plumbing into this module as
   ``run_specialist_analysis`` so each concrete agent module stays a thin,
   domain-specific shell (its own prompt content and any agent-specific
   post-processing) rather than four near-identical copies of the same
   orchestration code.

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

3. M10 additions -- the shared plumbing every real specialist needs:

   - ``RETRIEVAL_TOP_K`` / retrieval-query extraction / context formatting:
     grounds a specialist's prompt in retrieved repository context (M9's
     ``HybridRetriever``), not just the raw diff. See
     ``extract_retrieval_query`` and ``build_user_message`` below for the
     query strategy and context budget, and the module-level docstring on
     each concrete agent for why this matters per-domain.
   - ``parse_failure_fallback_finding`` / ``infrastructure_failure_fallback_
     finding``: the two forced-HITL fallback builders M8 wrote specifically
     for SECURITY (``backend.agents.security_agent``), generalized here to
     take an ``AgentType`` so all four agents -- and
     ``backend.orchestrator.nodes``'s four specialist nodes -- share
     exactly one implementation of "how do we signal this specialist could
     not do its job" instead of four (or five, counting the original)
     copies of the same ~20 lines. ``security_agent.py``'s own
     ``infrastructure_failure_fallback_finding`` is kept as a thin,
     backward-compatible wrapper around the generic version here (its
     existing caller, ``backend.orchestrator.nodes.security_node``, is
     unchanged), rather than being deleted -- see that module's docstring.
   - ``run_specialist_analysis``: the shared "load prompt, ground with
     retrieval, call the LLM, parse the response, fall back on a parse
     failure" sequence every concrete agent's ``analyze`` delegates to.
     Infrastructure failures (``BudgetExceededError``,
     ``LLMConfigurationError``, ``LLMCallFailedError``) are deliberately
     NOT caught here -- exactly like M8's original ``SecurityAgent.analyze``
     docstring explains, those mean "this specialist could not even attempt
     its analysis", a different, more serious situation than "the model
     answered but its answer was garbage" (a parse failure, which IS
     handled here). The caller (each specialist's own node function in
     ``backend.orchestrator.nodes``) is where that distinction is handled.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol

from backend.agents.response_parsing import ResponseParseError, parse_findings_from_llm_response
from backend.models import AgentType, Finding, Severity
from backend.prompts.registry import load_prompt
from backend.tools.llm_client import LLMClientProtocol

logger = logging.getLogger(__name__)

AGENT_PRECEDENCE: tuple[AgentType, ...] = (
    AgentType.SECURITY,
    AgentType.QUALITY,
    AgentType.TESTS,
    AgentType.DOCS,
)

# Which on-disk prompt directory (backend/prompts/templates/<name>/) and
# which "agent=" attribution string each AgentType uses. One place, so a
# concrete agent module and backend.orchestrator.nodes always agree on the
# name a given AgentType maps to.
AGENT_NAME_BY_TYPE: dict[AgentType, str] = {
    AgentType.SECURITY: "security",
    AgentType.QUALITY: "quality",
    AgentType.TESTS: "tests",
    AgentType.DOCS: "docs",
}


class BaseAgent(ABC):
    """Abstract specialist agent: analyzes a diff and returns Findings.

    Every concrete subclass as of M10 (``SecurityAgent``, ``QualityAgent``,
    ``TestsAgent``, ``DocsAgent``) is a real, LLM-backed implementation --
    see this module's docstring for the shared plumbing they all delegate
    to (``run_specialist_analysis``).
    """

    agent_type: AgentType

    @abstractmethod
    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        """Analyze a unified diff and return this specialist's findings.

        Real implementations call an LLM and validate its output against
        the ``Finding`` schema before returning; this method's contract
        does not change based on how a concrete subclass produces its
        findings.

        ``review_id``: an optional correlation id a real, LLM-backed
        implementation passes through to its LLM client so a resulting
        ``llm.call`` event (``backend.observability.emit_llm_call``) can be
        attributed to the review it belongs to. ``None`` for a caller with
        no review to correlate against (e.g. an ad hoc CLI invocation) --
        implementations must treat that as "don't emit a correlated
        event", not as an error.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# M10: retrieval grounding.
#
# QUERY STRATEGY (documented per this milestone's own instruction to decide
# and record it): prefer the names of symbols the diff actually DEFINES on
# an added line (a function/class/const/interface declaration) over the raw
# diff text. Rationale -- learned directly from M9's own investigation
# (`.genesis/checkpoints/CURRENT.md`'s M9 L2 DEBUG entry): a long, free-text
# query (the whole diff) dilutes a single distinctive identifier's vector
# weight below the corpus noise floor once the corpus has hundreds of
# chunks, exactly the failure mode M9 measured directly. A short query built
# from the actual changed symbol names is both a stronger full-text match
# (the two-token compound identifier as written) and a less-diluted vector
# query (fewer, more distinctive tokens). Falls back to a truncated slice of
# the raw diff text ONLY when no such symbol can be found (e.g. a
# config/data-only diff with no code-shaped declarations) -- some query is
# always better than skipping retrieval entirely for that diff.
_RETRIEVAL_TOP_K = 5

# Total budget (characters, a cheap proxy for tokens) of retrieved-chunk
# text injected into one specialist's prompt. Chosen so four specialists'
# retrieval blocks together stay a small fraction of one LLM call's
# _MAX_OUTPUT_TOKENS-sized budget (backend.tools.llm_client) -- this bounds
# both prompt cost and the risk of retrieved context crowding out the diff
# itself in a model's attention. A per-agent budget (not shared across
# agents) since each specialist's call is independent and billed
# separately.
_MAX_CONTEXT_CHARS = 6000

# Cap on how many distinct changed-symbol tokens feed the retrieval query --
# a diff touching dozens of functions should not turn into one enormous,
# unfocused query; the first few changed symbols are the ones most likely
# to be what the diff is actually "about".
_MAX_QUERY_SYMBOLS = 12

# Patterns that pull a declared symbol's name off an ADDED diff line ("+..."),
# covering the languages this project's own source (and a realistic PR
# fixture) is likely to touch. Deliberately simple regexes, not a real
# parser -- a false negative (missing a symbol) just means this diff falls
# back to raw-text query, never a hard failure.
_DEFINITION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*="),
    re.compile(r"\binterface\s+([A-Za-z_][A-Za-z0-9_]*)"),
)


def extract_retrieval_query(diff: str) -> str:
    """Build a retrieval query text from a unified diff. See module docstring.

    Returns the space-joined, order-preserving, deduplicated list of the
    first ``_MAX_QUERY_SYMBOLS`` distinct symbol names DEFINED on an added
    (``+``) line, or -- if none are found -- the first 500 characters of the
    raw diff text as a fallback query.
    """
    symbols: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pattern in _DEFINITION_PATTERNS:
            match = pattern.search(line)
            if match:
                name = match.group(1)
                if name not in symbols:
                    symbols.append(name)
                break
        if len(symbols) >= _MAX_QUERY_SYMBOLS:
            break
    if symbols:
        return " ".join(symbols[:_MAX_QUERY_SYMBOLS])
    return diff[:500]


class RetrievedChunkLike(Protocol):
    """The shape ``format_retrieved_context`` needs from one retrieved chunk.

    A ``Protocol`` (not a direct dependency on ``backend.memory.
    context_retriever.RetrievedChunk``) purely so a test can hand this a
    bare fake without constructing that dataclass -- ``RetrievedChunk``
    already satisfies this structurally. Declared via read-only
    ``@property`` methods, not plain mutable attribute annotations: a
    plain ``path: str`` annotation on a ``Protocol`` requires a SETTABLE
    attribute for structural conformance, which ``RetrievedChunk`` (a
    ``@dataclass(frozen=True)``) does not have -- mypy --strict correctly
    rejects that mismatch. A read-only property is satisfied by both a
    frozen dataclass field and a plain mutable attribute on any fake a
    test constructs, so this is the strictly more permissive (and
    correct) declaration for a value this module only ever reads.
    """

    @property
    def path(self) -> str: ...

    @property
    def content(self) -> str: ...


class RetrieverProtocol(Protocol):
    """The shape a specialist agent needs from a retriever.

    Matches ``backend.memory.context_retriever.HybridRetriever.
    hybrid_search`` exactly (structural typing) -- a test can inject a bare
    fake object with just this one method, no real pgvector connection or
    embedder required, mirroring how ``LLMClientProtocol`` already lets
    every test run with no ``ANTHROPIC_API_KEY``.
    """

    def hybrid_search(
        self, query_text: str, top_k: int | None = None
    ) -> Sequence[RetrievedChunkLike]: ...


def format_retrieved_context(
    chunks: Sequence[RetrievedChunkLike], *, max_chars: int = _MAX_CONTEXT_CHARS
) -> str:
    """Render retrieved chunks as a labeled context block, bounded by ``max_chars``.

    Returns ``""`` for an empty chunk list (nothing to inject) -- callers
    must treat that as "no retrieval context available", not as a formatting
    bug. Chunks are added in the order given (already ranked by the
    retriever's own fused RRF score) and truncated -- a whole chunk is
    either fully included or fully dropped, never cut mid-chunk, so nothing
    injected into the prompt is a truncated, potentially misleading
    fragment of a chunk.
    """
    if not chunks:
        return ""
    header = (
        "Relevant existing code from this repository, retrieved because it "
        "may relate to the diff below (it may or may not actually be "
        "relevant -- use your own judgment, and do not assume it is "
        "complete or authoritative):\n\n"
    )
    used = len(header)
    blocks: list[str] = [header]
    for chunk in chunks:
        block = f"--- {chunk.path} ---\n{chunk.content}\n\n"
        if used + len(block) > max_chars:
            continue
        blocks.append(block)
        used += len(block)
    if len(blocks) == 1:
        # Every chunk was too large to fit the budget individually --
        # nothing usable was actually injected.
        return ""
    return "".join(blocks)


def build_user_message(
    diff: str,
    retriever: RetrieverProtocol | None,
    *,
    top_k: int = _RETRIEVAL_TOP_K,
) -> str:
    """Build the LLM call's user-turn content: retrieved context (if any) + the diff.

    ``retriever=None`` (the default for every agent unless one is
    explicitly injected -- see each concrete agent's constructor) means
    "retrieval is not wired up for this call", and this returns the diff
    alone, unchanged -- the exact M8 behavior, preserved for any caller
    that doesn't opt into grounding.

    A retrieval failure (the pgvector store unreachable, a malformed query)
    is deliberately swallowed here, not propagated: retrieval is a
    best-effort GROUNDING enhancement to an LLM call that can still proceed
    perfectly well on the diff alone, and a specialist's whole analysis
    should not be forced into the same "infrastructure failure, force
    HITL" path (see ``infrastructure_failure_fallback_finding`` below) just
    because the grounding store happened to be briefly unavailable while
    the LLM provider itself is fine. This is a deliberate availability/
    thoroughness trade-off, not an oversight -- disclosed in this
    milestone's build report.
    """
    if retriever is None:
        return diff
    query = extract_retrieval_query(diff)
    try:
        chunks = retriever.hybrid_search(query, top_k=top_k)
    except Exception:  # noqa: BLE001 -- deliberate: see docstring above.
        logger.warning(
            "retrieval failed while grounding a specialist prompt (query=%r); "
            "continuing with the diff alone",
            query,
            exc_info=True,
        )
        return diff
    context_block = format_retrieved_context(chunks)
    if not context_block:
        return diff
    return f"{context_block}Diff to review:\n{diff}"


# ---------------------------------------------------------------------------
# M10: generic forced-HITL fallback builders, generalizing M8's
# SECURITY-only versions (backend.agents.security_agent) to take an
# AgentType. See module docstring for why these exist here now.
# ---------------------------------------------------------------------------

# Sentinel file/line the two fallback Findings below are attributed to --
# there is no real diff location responsible for either kind of failure
# (a parse failure means the MODEL's response, not the diff, failed to
# parse; an infrastructure failure means the specialist never got far
# enough to look at the diff at all).
_PARSE_FAILURE_FILE_PATH = "<llm-response-unparseable>"


def parse_failure_fallback_finding(agent_type: AgentType, exc: ResponseParseError) -> Finding:
    """Build the synthetic, forced-HITL Finding a total parse failure returns.

    CRITICAL + confidence 0.000 -- see ``infrastructure_failure_fallback_
    finding``'s docstring (and ``backend.agents.security_agent``'s module
    docstring, the original design for this mechanism) for why CRITICAL,
    not merely "very low confidence", is the deliberate choice: it is what
    makes ``backend.hitl.queue.has_critical_finding`` force human review of
    this run UNCONDITIONALLY, regardless of what the other three
    specialists reported.
    """
    agent_name = AGENT_NAME_BY_TYPE[agent_type]
    return Finding(
        agent_type=agent_type,
        severity=Severity.CRITICAL,
        category="llm_response_unparseable",
        file_path=_PARSE_FAILURE_FILE_PATH,
        line_start=1,
        line_end=1,
        confidence=Decimal("0.000"),
        rationale=(
            f"The {agent_name} specialist's LLM response could not be parsed "
            f"into any valid finding ({exc}). Flagging as CRITICAL, "
            "confidence 0.000, purely to force mandatory human review of "
            "this run -- this is NOT a claim that a real issue exists."
        ),
    )


def infrastructure_failure_fallback_finding(agent_type: AgentType, exc: BaseException) -> Finding:
    """Build the synthetic, forced-HITL Finding an infrastructure failure returns.

    CRITICAL + confidence 0.000, exactly like ``parse_failure_fallback_
    finding`` above -- used by each specialist node in
    ``backend.orchestrator.nodes`` when that specialist's ``analyze`` raises
    one of the handful of exceptions that mean "the analysis did not happen
    at all" (``backend.economics.budget.BudgetExceededError``,
    ``backend.tools.llm_client.LLMConfigurationError``/``LLMCallFailedError``)
    rather than "the model answered but its answer was garbage" (that is
    ``parse_failure_fallback_finding``'s case, handled inside
    ``run_specialist_analysis`` itself and never reaches a node as an
    exception at all).

    Generalizes M8's SECURITY-only ``backend.agents.security_agent.
    infrastructure_failure_fallback_finding`` to any ``AgentType`` -- this
    is the fix for the exact gap M8's own L4 VERIFY flagged as about to
    bite once M10 made the other three specialists real (see
    ``.genesis/checkpoints/CURRENT.md``'s M9 Deferred entry): with only
    SECURITY real, a budget block on a stub specialist was impossible: the
    stubs never call an LLM. Once all four are real, EVERY one of them can
    hit a BudgetGuard block/misconfigured key/unreachable provider, and
    every one of them must force HITL the same way, not just SECURITY.
    """
    agent_name = AGENT_NAME_BY_TYPE[agent_type]
    return Finding(
        agent_type=agent_type,
        severity=Severity.CRITICAL,
        category=f"{agent_name}_specialist_unavailable",
        file_path=f"<{agent_name}-specialist-unavailable>",
        line_start=1,
        line_end=1,
        confidence=Decimal("0.000"),
        rationale=(
            f"The {agent_name} specialist could not complete its analysis "
            f"({type(exc).__name__}: {exc}). Flagging as CRITICAL, "
            "confidence 0.000, purely to force mandatory human review of "
            "this run -- this is NOT a claim that a real issue exists."
        ),
    )


def run_specialist_analysis(
    agent_type: AgentType,
    *,
    llm_client: LLMClientProtocol,
    retriever: RetrieverProtocol | None,
    prompt_version: str,
    diff: str,
    review_id: str | None,
) -> list[Finding]:
    """Shared body every concrete specialist's ``analyze`` delegates to.

    Sequence: load this agent's versioned prompt
    (``backend.prompts.registry.load_prompt``), ground the user turn with
    retrieved context if a retriever was injected (``build_user_message``),
    make the LLM call, and parse its response
    (``backend.agents.response_parsing.parse_findings_from_llm_response``).
    A total parse failure becomes one forced-HITL fallback Finding rather
    than propagating or silently returning an empty list -- see
    ``parse_failure_fallback_finding``.

    Infrastructure/availability exceptions the LLM call can raise
    (``BudgetExceededError``, ``LLMConfigurationError``,
    ``LLMCallFailedError``) are deliberately NOT caught here -- they
    propagate to the caller (each specialist's node function in
    ``backend.orchestrator.nodes``), which is where that distinction is
    handled identically for all four agents. See this module's docstring
    and ``backend.agents.security_agent``'s original M8 docstring for the
    full reasoning.
    """
    agent_name = AGENT_NAME_BY_TYPE[agent_type]
    system_prompt = load_prompt(agent_name, version=prompt_version)
    user_message = build_user_message(diff, retriever)
    response = llm_client.complete(
        system=system_prompt,
        user=user_message,
        agent=agent_name,
        review_id=review_id,
    )
    try:
        return parse_findings_from_llm_response(response.text, default_agent_type=agent_type)
    except ResponseParseError as exc:
        logger.warning(
            "%s agent: LLM response could not be parsed into any valid "
            "Finding (%s) -- falling back to a forced-HITL CRITICAL "
            "finding rather than dropping this specialist's contribution "
            "silently",
            agent_name,
            exc,
        )
        return [parse_failure_fallback_finding(agent_type, exc)]


# Re-exported so callers that only need "any Any-typed protocol member" (a
# handful of internal type hints above) don't need a second import purely
# for annotation purposes.
__all__ = [
    "AGENT_NAME_BY_TYPE",
    "AGENT_PRECEDENCE",
    "BaseAgent",
    "RetrievedChunkLike",
    "RetrieverProtocol",
    "build_user_message",
    "extract_retrieval_query",
    "format_retrieved_context",
    "infrastructure_failure_fallback_finding",
    "parse_failure_fallback_finding",
    "run_specialist_analysis",
]
