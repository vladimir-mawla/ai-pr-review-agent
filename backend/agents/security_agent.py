"""The real, LLM-backed SECURITY specialist -- M8's outcome, made concrete.

Owns ``SecurityAgent``, the first ``backend.agents.base_agent.BaseAgent``
implementation that is not a canned stub: given a diff, it calls the driver
model (``backend.tools.llm_client``), parses the raw response through the
tolerant parser (``backend.agents.response_parsing``), and returns
schema-valid ``Finding`` objects -- "the first point real model behavior
enters the system", per PLAN.md's M8 outcome text.

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
other three stub/real specialists happen to score that particular run; a
lower-confidence-but-non-CRITICAL fallback could, in principle, still
average out above the auto-post threshold if the other three specialists
were confident enough, which would defeat the point).

This module is also PLAN.md's named M8 demo entry point: running it as
``python -m backend.agents.security_agent --diff <path>`` reads a unified
diff from disk, runs it through a real ``SecurityAgent``, and prints every
resulting ``Finding``.

M8 L2 DEBUG addition (post-L4-VERIFY): this module also exposes
``infrastructure_failure_fallback_finding``, a SECOND forced-HITL fallback
builder for a DIFFERENT failure class than ``_parse_failure_fallback_finding``
above. That one fires when the model answered but its answer was garbage
(a parse failure); this one is for when the security specialist could not
even attempt or complete its analysis at all -- a ``BudgetGuard`` block, a
misconfigured API key, or the provider unreachable after every retry (see
this class's ``analyze`` docstring for exactly which exceptions those are).
``backend.orchestrator.nodes.security_node`` is the one caller: reusing
this module's existing CRITICAL/confidence-0.000/forced-HITL mechanism
(rather than inventing a second, differently-shaped "infrastructure error"
signal) is deliberate -- both failure classes share the same real-world
consequence (this run's security review did not happen, so a human must
look at it), and ``backend.hitl.queue.has_critical_finding``'s unconditional
routing already exists to guarantee exactly that.
"""

from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal

from backend.agents.base_agent import BaseAgent
from backend.agents.response_parsing import ResponseParseError, parse_findings_from_llm_response
from backend.models import AgentType, Finding, Severity
from backend.prompts.registry import load_prompt
from backend.tools.llm_client import AnthropicLLMClient, LLMClientProtocol

logger = logging.getLogger(__name__)

_AGENT_NAME = "security"
_PROMPT_VERSION = "v1"

# The file/line a total-parse-failure fallback Finding is attributed to.
# There is no real location to point at (the model's response, not the
# diff, is what failed to parse) -- a fixed sentinel makes that fact
# visible to a human reader rather than pointing at a plausible-looking but
# meaningless line number.
_PARSE_FAILURE_FILE_PATH = "<llm-response-unparseable>"

# Same idea, for the OTHER forced-HITL fallback (see
# infrastructure_failure_fallback_finding below): no real diff location
# caused this failure either -- the specialist never got far enough to look
# at one.
_INFRASTRUCTURE_FAILURE_FILE_PATH = "<security-specialist-unavailable>"


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
    """

    agent_type = AgentType.SECURITY

    def __init__(
        self,
        llm_client: LLMClientProtocol | None = None,
        *,
        prompt_version: str = _PROMPT_VERSION,
    ) -> None:
        self._llm_client: LLMClientProtocol = (
            llm_client if llm_client is not None else AnthropicLLMClient()
        )
        self._prompt_version = prompt_version

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
        system_prompt = load_prompt(_AGENT_NAME, version=self._prompt_version)
        response = self._llm_client.complete(
            system=system_prompt,
            user=diff,
            agent=_AGENT_NAME,
            review_id=review_id,
        )
        try:
            return parse_findings_from_llm_response(
                response.text, default_agent_type=AgentType.SECURITY
            )
        except ResponseParseError as exc:
            logger.warning(
                "security agent: LLM response could not be parsed into any "
                "valid Finding (%s) -- falling back to a forced-HITL CRITICAL "
                "finding rather than dropping this specialist's contribution "
                "silently",
                exc,
            )
            return [_parse_failure_fallback_finding(exc)]


def _parse_failure_fallback_finding(exc: ResponseParseError) -> Finding:
    """Build the synthetic, forced-HITL Finding a total parse failure returns.

    CRITICAL + confidence 0.000 -- see module docstring for why CRITICAL
    (not merely "very low confidence") is the deliberate choice here.
    """
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.CRITICAL,
        category="llm_response_unparseable",
        file_path=_PARSE_FAILURE_FILE_PATH,
        line_start=1,
        line_end=1,
        confidence=Decimal("0.000"),
        rationale=(
            "The security specialist's LLM response could not be parsed into "
            f"any valid finding ({exc}). Flagging as CRITICAL, confidence "
            "0.000, purely to force mandatory human review of this run -- "
            "this is NOT a claim that a real security issue exists."
        ),
    )


def infrastructure_failure_fallback_finding(exc: BaseException) -> Finding:
    """Build the synthetic, forced-HITL Finding an infrastructure failure returns.

    CRITICAL + confidence 0.000, exactly like
    ``_parse_failure_fallback_finding`` -- see this module's docstring for
    why this reuses that mechanism instead of inventing a second one. Used
    by ``backend.orchestrator.nodes.security_node`` when ``SecurityAgent.
    analyze`` raises one of the handful of exceptions that mean "the
    analysis did not happen at all" (``backend.economics.budget.
    BudgetExceededError``, ``backend.tools.llm_client.
    LLMConfigurationError``/``LLMCallFailedError``) rather than "the model
    answered but its answer was garbage" (that is
    ``_parse_failure_fallback_finding``'s case, handled inside ``analyze``
    itself and never reaches the node as an exception at all).

    Deliberately public (unlike ``_parse_failure_fallback_finding``): its
    one caller lives in a different module (``backend.orchestrator.nodes``),
    whereas the parse-failure fallback's only caller is ``analyze`` right
    here in the same class.
    """
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.CRITICAL,
        category="security_specialist_unavailable",
        file_path=_INFRASTRUCTURE_FAILURE_FILE_PATH,
        line_start=1,
        line_end=1,
        confidence=Decimal("0.000"),
        rationale=(
            "The security specialist could not complete its analysis "
            f"({type(exc).__name__}: {exc}). Flagging as CRITICAL, confidence "
            "0.000, purely to force mandatory human review of this run -- "
            "this is NOT a claim that a real security issue exists."
        ),
    )


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
