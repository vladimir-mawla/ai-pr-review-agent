"""The real, LLM-backed QUALITY specialist -- M10's outcome, made concrete.

Owns ``QualityAgent``, the second ``backend.agents.base_agent.BaseAgent``
implementation (after M8's SECURITY) -- built on the shared orchestration
``backend.agents.base_agent.run_specialist_analysis`` provides, so this
module's own job is narrow: which prompt to load, and this agent's own
constructor/failure-fallback identity.

REMIT (deliberately distinct from the other three specialists -- see
``backend/prompts/templates/quality/v1.md`` for the full instruction given
to the model): excessive complexity, duplication, poor/missing error
handling, and misleading naming. QUALITY is explicitly told NOT to comment
on security, test coverage, or documentation -- SECURITY
(``backend.agents.security_agent``), TESTS (``backend.agents.test_agent``),
and DOCS (``backend.agents.docs_agent``) own those, respectively. This
non-overlapping remit is what keeps four specialists' findings from
converging into four copies of the same generic commentary on the same
lines -- a genuinely worthless outcome this milestone's own instructions
warn against.

FAILURE POLICY: identical to SECURITY's (see
``backend.agents.security_agent``'s module docstring for the full
reasoning) -- a total LLM-response parse failure becomes one synthetic
CRITICAL/confidence-0.000 forced-HITL Finding
(``backend.agents.base_agent.parse_failure_fallback_finding``), never a
crash and never a silently-empty findings list. An infrastructure/
availability failure (``BudgetExceededError``,
``LLMConfigurationError``/``LLMCallFailedError``) is NOT caught here --
``backend.orchestrator.nodes.quality_node`` handles that, via
``infrastructure_failure_fallback_finding`` below, using the exact same
mechanism SECURITY's node already established at M8.
"""

from __future__ import annotations

from backend.agents.base_agent import (
    BaseAgent,
    RetrieverProtocol,
    run_specialist_analysis,
)
from backend.agents.base_agent import (
    infrastructure_failure_fallback_finding as _generic_infrastructure_failure_fallback_finding,
)
from backend.models import AgentType, Finding
from backend.tools.llm_client import AnthropicLLMClient, LLMClientProtocol

_PROMPT_VERSION = "v1"


class QualityAgent(BaseAgent):
    """LLM-backed QUALITY specialist: real diff in, schema-valid Findings out.

    See module docstring for QUALITY's specific remit. Structurally
    identical to ``backend.agents.security_agent.SecurityAgent`` --
    both delegate to ``backend.agents.base_agent.run_specialist_analysis``,
    differing only in ``agent_type`` (which prompt/attribution name that
    resolves to).
    """

    agent_type = AgentType.QUALITY

    def __init__(
        self,
        llm_client: LLMClientProtocol | None = None,
        *,
        prompt_version: str = _PROMPT_VERSION,
        retriever: RetrieverProtocol | None = None,
    ) -> None:
        self._llm_client: LLMClientProtocol = (
            llm_client if llm_client is not None else AnthropicLLMClient()
        )
        self._prompt_version = prompt_version
        self._retriever = retriever

    def analyze(self, diff: str, *, review_id: str | None = None) -> list[Finding]:
        """Analyze ``diff`` for quality issues via a real (or fake) LLM call.

        See ``backend.agents.base_agent.run_specialist_analysis``'s
        docstring for the total-parse-failure fallback policy, and this
        module's docstring for why infrastructure/availability exceptions
        are deliberately NOT caught here (that is
        ``backend.orchestrator.nodes.quality_node``'s job).
        """
        return run_specialist_analysis(
            AgentType.QUALITY,
            llm_client=self._llm_client,
            retriever=self._retriever,
            prompt_version=self._prompt_version,
            diff=diff,
            review_id=review_id,
        )


def infrastructure_failure_fallback_finding(exc: BaseException) -> Finding:
    """Build the synthetic, forced-HITL Finding an infrastructure failure returns.

    Thin wrapper around ``backend.agents.base_agent.
    infrastructure_failure_fallback_finding(AgentType.QUALITY, exc)`` --
    mirrors ``backend.agents.security_agent.
    infrastructure_failure_fallback_finding`` exactly, parameterized for
    QUALITY. ``backend.orchestrator.nodes.quality_node`` is this function's
    one caller.
    """
    return _generic_infrastructure_failure_fallback_finding(AgentType.QUALITY, exc)
