"""PLAN.md's named M8 integration test: a real call to the driver model.

Skipped -- not failed -- whenever ``ANTHROPIC_API_KEY`` is not configured,
per PLAN.md's own M8 credentials line ("the unit-test path is
credential-free"). This is the one test file in this milestone that is
allowed to (and does, when a key is present) make a real network call to
Anthropic's API; every other M8 test uses a fake LLM client and must pass
with no key at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.agents.security_agent import SecurityAgent
from backend.core.settings import get_settings
from backend.models import AgentType

_FIXTURE_DIFF_PATH = Path(__file__).parent.parent / "fixtures" / "sqli_diff.patch"

pytestmark = pytest.mark.skipif(
    not get_settings().anthropic_api_key,
    reason="ANTHROPIC_API_KEY is not configured -- skipping the live LLM call",
)


def test_security_agent_makes_a_real_call_and_returns_schema_valid_findings() -> None:
    diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
    agent = SecurityAgent()

    findings = agent.analyze(diff)

    # The model may or may not flag anything, but PLAN.md's success
    # criteria for the CLI demo command specifically requires at least one
    # finding for this fixture (a real, unparameterized SQL query) -- see
    # the demo command itself for the equivalent assertion on stdout.
    assert len(findings) >= 1
    for finding in findings:
        assert finding.agent_type == AgentType.SECURITY
        assert 0 <= finding.confidence <= 1
        assert finding.rationale
