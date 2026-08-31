"""M10's named e2e test: PLAN.md's M10 demo command, exercised end to end.

Owns proving PLAN.md's exact M10 success criteria:

    "out/review.json validates against the Review schema; the mocked
    GitHub client records exactly one 'post' or one 'queue_for_hitl' call,
    never both; pytest tests/e2e/test_full_local_review.py -v passes."

Drives ``backend.cli.review_local.run_review_locally`` (the testable core
``main`` is a thin wrapper around -- see that module's docstring) directly
in-process against ``tests/fixtures/sample_pr_diff.patch`` with fake LLM
clients installed for all four specialists (no ``ANTHROPIC_API_KEY``
needed) and an injected ``MockGitHubClient``/isolated ``tmp_path``-backed
engine, so this test is fast and fully self-contained.

``TestCliMainWritesTheDemoCommandsOutputFile`` additionally drives the
real ``main()`` CLI entry point (argv parsing, file I/O) -- proving the
ACTUAL demo command PLAN.md names, not just the function it calls.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.cli.review_local import main, run_review_locally
from backend.integrations.github_client import MockGitHubClient
from backend.models import Review
from backend.orchestrator import nodes
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.tools.llm_client import LLMResponse

_FIXTURE_DIFF_PATH = Path(__file__).parent.parent / "fixtures" / "sample_pr_diff.patch"


def _findings_response(file_path: str, category: str) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "severity": "MEDIUM",
                    "category": category,
                    "file_path": file_path,
                    "line_start": 1,
                    "line_end": 1,
                    "confidence": "0.900",
                    "rationale": f"a fake {category} finding for e2e CLI testing",
                }
            ]
        }
    )


# A distinct file_path/category per agent -- so the four fake findings land
# at four different (file_path, line_start) keys and none of them collide
# in the real aggregator's dedupe step below. Colliding them on purpose is
# a DIFFERENT, deliberately-tested scenario
# (tests/integration/test_all_specialist_agents_in_graph.py); this file
# tests the ordinary "four independent findings" path.
_RESPONSES_BY_AGENT = {
    "security": _findings_response("app/api/user_routes.py", "sql_injection"),
    "quality": _findings_response("app/services/pricing.py", "excessive_complexity"),
    "tests": _findings_response("app/services/bulk_pricing.py", "missing_test_coverage"),
    "docs": _findings_response("app/services/pricing_docs.py", "stale_docstring"),
}


class _FakeLLMClient:
    def complete(
        self,
        *,
        system: str,
        user: str,
        agent: str,
        review_id: str | None = None,
    ) -> LLMResponse:
        return LLMResponse(
            text=_RESPONSES_BY_AGENT[agent],
            model="fake-model",
            tokens_in=10,
            tokens_out=10,
            cost_usd=Decimal("0.000100"),
            latency_ms=1,
        )


@pytest.fixture(autouse=True)
def _install_fake_agents_for_all_four_specialists() -> None:
    """No ANTHROPIC_API_KEY anywhere in this file -- every specialist uses a fake client."""
    fake_client = _FakeLLMClient()
    nodes.set_security_agent_for_testing(SecurityAgent(fake_client))
    nodes.set_quality_agent_for_testing(QualityAgent(fake_client))
    nodes.set_tests_agent_for_testing(TestsAgent(fake_client))
    nodes.set_docs_agent_for_testing(DocsAgent(fake_client))
    yield
    nodes.set_security_agent_for_testing(None)
    nodes.set_quality_agent_for_testing(None)
    nodes.set_tests_agent_for_testing(None)
    nodes.set_docs_agent_for_testing(None)


class TestRunReviewLocallyProducesASchemaValidReview:
    def test_review_is_produced_and_valid(self, tmp_path: Path) -> None:
        diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
        github_client = MockGitHubClient()
        engine = LangGraphWorkflowEngine(tmp_path / "checkpoints.sqlite3")
        try:
            review = run_review_locally(
                diff,
                review_id="e2e-test-1",
                github_client=github_client,
                engine=engine,
            )
        finally:
            engine.close()

        # A Review instance is, by construction, already schema-valid
        # (pydantic validates on construction) -- but re-validate a
        # round-trip through JSON too, exactly what the demo command's own
        # `jq '.findings | length'` step depends on being possible.
        round_tripped = Review.model_validate_json(review.model_dump_json())
        assert round_tripped.review_id == "e2e-test-1"
        assert len(round_tripped.findings) == 4  # one per specialist, four distinct file paths

    def test_the_mock_github_client_records_exactly_one_call_never_both(
        self, tmp_path: Path
    ) -> None:
        diff = _FIXTURE_DIFF_PATH.read_text(encoding="utf-8")
        github_client = MockGitHubClient()
        engine = LangGraphWorkflowEngine(tmp_path / "checkpoints.sqlite3")
        try:
            run_review_locally(
                diff,
                review_id="e2e-test-2",
                github_client=github_client,
                engine=engine,
            )
        finally:
            engine.close()

        assert github_client.total_calls() == 1, (
            f"expected exactly one post_review_comment or queue_for_hitl call, got "
            f"{len(github_client.posted_reviews)} posted + "
            f"{len(github_client.queued_reviews)} queued"
        )
        assert not (github_client.posted_reviews and github_client.queued_reviews)


class TestCliMainWritesTheDemoCommandsOutputFile:
    """Drives the real ``main()`` entry point -- argv parsing + file I/O -- not just the function it calls."""

    def test_main_writes_a_valid_review_json_file(self, tmp_path: Path) -> None:
        out_path = tmp_path / "out" / "review.json"
        exit_code = main(
            [
                "--diff",
                str(_FIXTURE_DIFF_PATH),
                "--out",
                str(out_path),
                "--review-id",
                "e2e-cli-main-1",
            ]
        )

        assert exit_code == 0
        assert out_path.is_file()

        raw = json.loads(out_path.read_text(encoding="utf-8"))
        # Validates against the Review schema, per PLAN.md's own success
        # criterion wording.
        review = Review.model_validate(raw)
        assert review.review_id == "e2e-cli-main-1"
        assert len(review.findings) == 4
        assert isinstance(raw["findings"], list)
        assert len(raw["findings"]) == 4
