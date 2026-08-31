"""The real, LLM-backed SECURITY specialist -- M8's outcome, made concrete.

Owns ``SecurityAgent``, the first ``backend.agents.base_agent.BaseAgent``
implementation that is not a canned stub: given a diff, it calls the driver
model (``backend.tools.llm_client``), parses the raw response through the
tolerant parser (``backend.agents.response_parsing``), and returns
schema-valid ``Finding`` objects -- "the first point real model behavior
enters the system", per PLAN.md's M8 outcome text.

REMIT (M10): SECURITY reviews the diff for real, exploitable vulnerability
classes ONLY -- injection, broken auth, hardcoded secrets, insecure
deserialization, missing input validation, cryptographic misuse. It is
explicitly told (see ``backend/prompts/templates/security/v1.md``) NOT to
comment on code quality, test coverage, or documentation -- those are the
other three specialists' distinct remits (see
``backend.agents.{quality_agent,test_agent,docs_agent}`` for theirs), which
is what keeps all four agents' findings from converging into interchangeable
generic commentary on the same lines.

FAILURE POLICY -- never silently drop, never crash the run: if the LLM's
response cannot be parsed into any valid ``Finding`` at all (see
``backend.agents.response_parsing.ResponseParseError`` for exactly which
cases raise that), this agent does not propagate the exception (which would
cost the whole review this specialist's contribution -- the same
"one bad specialist must not cost the other three their findings" principle
``backend.orchestrator.nodes.AgentExecutionError`` documents) and does not
return an empty list (which would silently and indistinguishably look like
"no security issues found"). Instead it returns exactly one synthetic
``Finding`` at CRITICAL severity, confidence 0.000, explaining that the
model's output could not be interpreted. CRITICAL is deliberate, not
decorative: ``backend.hitl.queue.has_critical_finding`` forces ANY review
containing a CRITICAL finding to human review UNCONDITIONALLY, regardless
of every other specialist's confidence -- which is exactly "forces HITL
review" in the most robust sense available (it does not depend on how the
other three real specialists happen to score that particular run; a
lower-confidence-but-non-CRITICAL fallback could, in principle, still
average out above the auto-post threshold if the other three specialists
were confident enough, which would defeat the point).

This module is also PLAN.md's named M8 demo entry point: running it as
``python -m backend.agents.security_agent --diff <path>`` reads a unified
diff from disk, runs it through a real ``SecurityAgent``, and prints every
resulting ``Finding``.

M8 L2 DEBUG addition (post-L4-VERIFY): this module also exposes
``infrastructure_failure_fallback_finding``, a SECOND forced-HITL fallback
builder for a DIFFERENT failure class than the parse-failure fallback
above. That one fires when the model answered but its answer was garbage
(a parse failure); this one is for when the security specialist could not
even attempt or complete its analysis at all -- a ``BudgetGuard`` block, a
misconfigured API key, or the provider unreachable after every retry.
``backend.orchestrator.nodes.security_node`` is the one caller: reusing
this mechanism (rather than inventing a second, differently-shaped
"infrastructure error" signal) is deliberate -- both failure classes share
the same real-world consequence (this run's security review did not
happen, so a human must look at it), and
``backend.hitl.queue.has_critical_finding``'s unconditional routing already
exists to guarantee exactly that.

M10 refactor: the actual orchestration logic (load prompt, ground with
retrieval, call the LLM, parse the response, fall back on a parse failure)
moved to ``backend.agents.base_agent.run_specialist_analysis`` once three
more specialists needed the exact same sequence -- see that module's
docstring. ``SecurityAgent`` itself is now a thin, domain-specific shell
around that shared function. The total-parse-failure fallback (a private
helper here in M8) is now handled inside ``run_specialist_analysis`` itself
via ``base_agent.parse_failure_fallback_finding`` and no longer has a
SECURITY-specific copy in this module. ``infrastructure_failure_fallback_
finding`` IS kept here, under its original name, as a thin wrapper around
``base_agent``'s generalized (any ``AgentType``) version -- for backward
compatibility with this module's existing public API and its one caller
(``backend.orchestrator.nodes.security_node``, which imports
``infrastructure_failure_fallback_finding`` from this module by name).
M10 also adds an optional ``retriever`` constructor argument -- SECURITY's
prompt is now grounded with retrieved repository context exactly like the
other three specialists, per this milestone's explicit instruction to wire
retrieval into "each specialist's prompt", SECURITY included.
"""

from __future__ import annotations

import argparse
import sys

from backend.agents.base_agent import (
    BaseAgent,
    RetrieverProtocol,
    run_specialist_analysis,
)
from backend.agents.base_agent import (
    infrastructure_failure_fallback_finding as _generic_infrastructure_failure_fallback_finding,
)
from backend.agents.response_parsing import ResponseParseError
from backend.models import AgentType, Finding
from backend.tools.llm_client import AnthropicLLMClient, LLMClientProtocol

_PROMPT_VERSION = "v1"


class SecurityAgent(BaseAgent):
    """LLM-backed SECURITY specialist: real diff in, schema-valid Findings out.

    Attributes:
        _llm_client: Anything satisfying ``LLMClientProtocol`` -- a real
            ``AnthropicLLMClient`` by default (constructed lazily by
            ``backend.orchestrator.nodes._get_security_agent``, never at
            import time), or a test-injected fake. This is exactly what
            lets every unit test in this project construct and exercise a
            real ``SecurityAgent`` with no ``ANTHROPIC_API_KEY`` set.
        _prompt_version: Which version of the security prompt template to
            load (``backend.prompts.registry.load_prompt``). Defaulted to
            "v1" rather than hardcoded inline so a future prompt revision
            is a one-argument change, not a new constructor signature.
        _retriever: M10 addition. Anything satisfying
            ``backend.agents.base_agent.RetrieverProtocol`` (a real
            ``HybridRetriever`` by default in production, wired by
            ``backend.orchestrator.nodes``, or a test-injected fake).
            ``None`` (the default) means "no retrieval grounding for this
            call" -- the exact pre-M10 behavior.
    """

    agent_type = AgentType.SECURITY

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
        """Analyze ``diff`` for security issues via a real (or fake) LLM call.

        See module docstring for the total-parse-failure fallback policy.
        Any other exception (e.g. ``backend.economics.budget.
        BudgetExceededError``, ``backend.tools.llm_client.
        LLMCallFailedError``/``LLMConfigurationError``) is deliberately NOT
        caught here -- those represent this agent being unable to even
        attempt an analysis (budget exhausted, misconfigured credentials,
        the provider unreachable after retries), which is a different, more
        serious situation than "the model answered but its answer was
        garbage". The caller (``backend.orchestrator.nodes.security_node``)
        is where that distinction is handled -- see that module's own
        isolation behavior for a specialist's failure.
        """
        return run_specialist_analysis(
            AgentType.SECURITY,
            llm_client=self._llm_client,
            retriever=self._retriever,
            prompt_version=self._prompt_version,
            diff=diff,
            review_id=review_id,
        )


def infrastructure_failure_fallback_finding(exc: BaseException) -> Finding:
    """Build the synthetic, forced-HITL Finding an infrastructure failure returns.

    Thin, backward-compatible wrapper around ``backend.agents.base_agent.
    infrastructure_failure_fallback_finding(AgentType.SECURITY, exc)`` -- see
    this module's docstring ("M10 refactor") for why the generic version now
    lives in ``base_agent`` (all four specialists need the identical
    mechanism) while this SECURITY-specific name is kept for
    ``backend.orchestrator.nodes.security_node``, its one existing caller.
    """
    return _generic_infrastructure_failure_fallback_finding(AgentType.SECURITY, exc)


def _read_diff(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def main(argv: list[str] | None = None) -> int:
    """PLAN.md's M8 demo entry point: ``python -m backend.agents.security_agent --diff <path>``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--diff",
        required=True,
        help="Path to a unified diff (patch) file to analyze.",
    )
    args = parser.parse_args(argv)

    diff = _read_diff(args.diff)
    agent = SecurityAgent()
    findings = agent.analyze(diff)

    if not findings:
        print("no findings")
        return 0
    for finding in findings:
        print(
            f"[{finding.severity.value}] {finding.category} "
            f"{finding.file_path}:{finding.line_start}-{finding.line_end} "
            f"(confidence={finding.confidence}) -- {finding.rationale}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


# Re-exported purely so a caller (or a docstring elsewhere) that imports
# ResponseParseError from this module for the total-parse-failure case
# still finds it here -- the actual handling now lives in
# backend.agents.base_agent.run_specialist_analysis.
__all__ = [
    "ResponseParseError",
    "SecurityAgent",
    "infrastructure_failure_fallback_finding",
    "main",
]
