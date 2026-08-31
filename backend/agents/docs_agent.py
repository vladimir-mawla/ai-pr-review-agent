"""The real, LLM-backed DOCS specialist -- M10's outcome, made concrete.

Owns ``DocsAgent``, built on the shared orchestration
``backend.agents.base_agent.run_specialist_analysis`` provides -- see
``backend.agents.quality_agent``'s module docstring for the full pattern
this and ``backend.agents.test_agent`` both follow.

REMIT (deliberately distinct from the other three specialists -- see
``backend/prompts/templates/docs/v1.md`` for the full instruction given to
the model): documentation the diff ITSELF should have updated -- a changed
signature/behavior with a stale docstring, a new public API with no
docstring, a prose doc file (e.g. README) describing behavior the diff just
changed without updating it, or an inline comment the diff's own change now
contradicts. DOCS is explicitly told NOT to comment on security, general
code quality, or test coverage -- those are
``backend.agents.security_agent``, ``backend.agents.quality_agent``, and
``backend.agents.test_agent``'s jobs respectively, and DOCS is told not to
flag pre-existing documentation gaps the diff itself did not introduce.

FAILURE POLICY: identical to SECURITY's/QUALITY's/TESTS' -- see
``backend.agents.security_agent``'s module docstring for the full
reasoning.
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


class DocsAgent(BaseAgent):
    """LLM-backed DOCS specialist: real diff in, schema-valid Findings out.

    See module docstring for DOCS' specific remit. Structurally identical
    to ``backend.agents.security_agent.SecurityAgent`` -- both delegate to
    ``backend.agents.base_agent.run_specialist_analysis``, differing only in
    ``agent_type``.
    """

    agent_type = AgentType.DOCS

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
        """Analyze ``diff`` for documentation gaps via a real (or fake) LLM call.

        See ``backend.agents.base_agent.run_specialist_analysis``'s
        docstring for the total-parse-failure fallback policy, and this
        module's docstring for why infrastructure/availability exceptions
        are deliberately NOT caught here (that is
        ``backend.orchestrator.nodes.docs_node``'s job).
        """
        return run_specialist_analysis(
            AgentType.DOCS,
            llm_client=self._llm_client,
            retriever=self._retriever,
            prompt_version=self._prompt_version,
            diff=diff,
            review_id=review_id,
        )


def infrastructure_failure_fallback_finding(exc: BaseException) -> Finding:
    """Build the synthetic, forced-HITL Finding an infrastructure failure returns.

    Thin wrapper around ``backend.agents.base_agent.
    infrastructure_failure_fallback_finding(AgentType.DOCS, exc)``.
    ``backend.orchestrator.nodes.docs_node`` is this function's one caller.
    """
    return _generic_infrastructure_failure_fallback_finding(AgentType.DOCS, exc)
