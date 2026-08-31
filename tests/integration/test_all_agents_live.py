"""M10's key-gated live test: all four real specialists, four real LLM calls.

Skipped -- not failed -- whenever ``ANTHROPIC_API_KEY`` is not configured,
exactly mirroring ``tests/integration/test_security_agent_live.py``'s own
M8 pattern. Every other M10 test uses a fake LLM client; this is the one
file in this milestone allowed to (and, when a key is present, does) make
four real, billable calls to Anthropic's API -- one per specialist, against
the same fixture diff PLAN.md's own M10 demo command reviews.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.core.settings import get_settings
from backend.models import AgentType

_FIXTURE_DIFF_PATH = Path(__file__).parent.parent / "fixtures" / "sample_pr_diff.patch"

pytestmark = pytest.mark.skipif(
    not get_settings().anthropic_api_key,
    reason="ANTHROPIC_API_KEY is not configured -- skipping the 4 live LLM calls",
)


class TestAllFourAgentsMakeRealCallsAndReturnSchemaValidFindings:
    """One real call per specialist against the real M10 fixture diff."""

    def test_security_agent_live(self) -> None:
        diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
        findings = SecurityAgent().analyze(diff)
        for finding in findings:
            assert finding.agent_type == AgentType.SECURITY
            assert Decimal("0") <= finding.confidence <= Decimal("1")
            assert finding.rationale

    def test_quality_agent_live(self) -> None:
        diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
        findings = QualityAgent().analyze(diff)
        for finding in findings:
            assert finding.agent_type == AgentType.QUALITY
            assert finding.rationale

    def test_tests_agent_live(self) -> None:
        diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
        findings = TestsAgent().analyze(diff)
        for finding in findings:
            assert finding.agent_type == AgentType.TESTS
            assert finding.rationale

    def test_docs_agent_live(self) -> None:
        diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
        findings = DocsAgent().analyze(diff)
        for finding in findings:
            assert finding.agent_type == AgentType.DOCS
            assert finding.rationale
