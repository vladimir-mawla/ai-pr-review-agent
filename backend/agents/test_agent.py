"""The real, LLM-backed TESTS specialist -- M10's outcome, made concrete.

Owns ``TestsAgent``, built on the shared orchestration
``backend.agents.base_agent.run_specialist_analysis`` provides -- see
``backend.agents.quality_agent``'s module docstring for the full pattern
this and ``backend.agents.docs_agent`` both follow.

Named ``test_agent.py`` (not ``tests_agent.py``), per PLAN.md's M10 literal
freeze-boundary file list.

REMIT (deliberately distinct from the other three specialists -- see
``backend/prompts/templates/tests/v1.md`` for the full instruction given to
the model): gaps in test coverage for the CHANGE ITSELF -- new/changed
production logic with no corresponding new/updated test in the diff, or a
new test that is present but inadequate (happy-path only, or asserts too
little to actually catch a regression). TESTS is explicitly told NOT to
comment on security, general code quality, or documentation -- those are
``backend.agents.security_agent``, ``backend.agents.quality_agent``, and
``backend.agents.docs_agent``'s jobs respectively.

FAILURE POLICY: identical to SECURITY's/QUALITY's -- see
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


class TestsAgent(BaseAgent):
    """LLM-backed TESTS specialist: real diff in, schema-valid Findings out.

    See module docstring for TESTS' specific remit. Structurally identical
    to ``backend.agents.security_agent.SecurityAgent`` -- both delegate to
    ``backend.agents.base_agent.run_specialist_analysis``, differing only in
    ``agent_type``.

    ``__test__ = False``: pytest's default class-collection pattern
    (``python_classes = ["Test*"]``, ``pyproject.toml``) matches this
    class's name (``TestsAgent`` starts with "Test"), which would
    otherwise make every test module that imports this class emit a
    ``PytestCollectionWarning`` ("cannot collect test class 'TestsAgent'
    because it has a __init__ constructor") -- harmless (pytest already
    correctly skips it), but noisy across every M10 test file that imports
    it. ``__test__ = False`` is pytest's own documented mechanism for
    telling it "this is not a test class" explicitly, silencing the
    warning without renaming the class away from its natural, domain-
    matching name (mirroring ``SecurityAgent``/``QualityAgent``/
    ``DocsAgent``).
    """

    __test__ = False
    agent_type = AgentType.TESTS

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
        """Analyze ``diff`` for test-coverage gaps via a real (or fake) LLM call.

        See ``backend.agents.base_agent.run_specialist_analysis``'s
        docstring for the total-parse-failure fallback policy, and this
        module's docstring for why infrastructure/availability exceptions
        are deliberately NOT caught here (that is
        ``backend.orchestrator.nodes.tests_node``'s job).
        """
        return run_specialist_analysis(
            AgentType.TESTS,
            llm_client=self._llm_client,
            retriever=self._retriever,
            prompt_version=self._prompt_version,
            diff=diff,
            review_id=review_id,
        )


def infrastructure_failure_fallback_finding(exc: BaseException) -> Finding:
    """Build the synthetic, forced-HITL Finding an infrastructure failure returns.

    Thin wrapper around ``backend.agents.base_agent.
    infrastructure_failure_fallback_finding(AgentType.TESTS, exc)``.
    ``backend.orchestrator.nodes.tests_node`` is this function's one caller.
    """
    return _generic_infrastructure_failure_fallback_finding(AgentType.TESTS, exc)
