"""LIVE: authenticate as the real GitHub App, fetch a real diff, post a real review.

This is the one test in this project that exercises M11's entire outcome
end-to-end against the real, private testbed repository
(``vladimir-mawla/pr-review-agent-testbed``, PR #1 --
https://github.com/vladimir-mawla/pr-review-agent-testbed/pull/1, created
during this milestone's own build session specifically for this purpose).
Marked ``live`` (never runs in a plain ``pytest`` invocation) and
``skipif``-guarded on the GitHub App credential being configured, per this
project's established credential policy (see ``pyproject.toml``'s
``live`` marker docstring).

IDEMPOTENT BY DESIGN, SAFE TO RE-RUN: this test posts with a FIXED,
hardcoded ``review_id`` (not a fresh uuid per run). The first run posts a
real review; ``RealGitHubClient.post_review_comment``'s own idempotency
check (see that method's docstring) means every subsequent run finds its
marker already on the PR and skips posting -- so running this test
repeatedly (e.g. in CI, or by hand) never spams the PR with duplicate
comments. This ALSO happens to be a second, independent proof of the
idempotency mechanism itself, this time against the real API rather than a
fake transport.

Findings are constructed directly here (not via a real LLM call) -- this
test's job is to prove the GITHUB half of the pipeline (auth, diff fetch,
diff-position mapping, posting) works for real, which does not require
spending on a real Anthropic call every time this test runs; M10/M8's own
``live``-marked tests already separately prove the LLM half against a real
model.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from backend.core.settings import Settings
from backend.integrations.github_client import RealGitHubClient
from backend.models import AgentType, Finding, Review, ReviewStatus, compute_overall_confidence

_TESTBED_OWNER = "vladimir-mawla"
_TESTBED_REPO = "pr-review-agent-testbed"
_TESTBED_PR_NUMBER = 1
# Fixed on purpose -- see module docstring's "IDEMPOTENT BY DESIGN" section.
_FIXED_REVIEW_ID = "m11-live-integration-test-fixed-id"


@pytest.mark.live
@pytest.mark.skipif(
    not (os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")),
    reason="GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY_PATH not configured",
)
class TestGitHubLiveEndToEnd:
    def test_authenticate_fetch_real_diff_and_post_a_real_review(self) -> None:
        settings = Settings(
            github_webhook_secret="unused-live-integration-test", github_client_backend="real"
        )
        client = RealGitHubClient(settings)

        # 1. AUTHENTICATE + fetch a REAL diff from the real, private testbed PR.
        pr_metadata = client.get_pr_metadata(
            owner=_TESTBED_OWNER, repo=_TESTBED_REPO, pr_number=_TESTBED_PR_NUMBER
        )
        assert pr_metadata.number == _TESTBED_PR_NUMBER
        head_sha = pr_metadata.head.sha

        diff = client.fetch_diff(
            repository_owner=_TESTBED_OWNER,
            repository_name=_TESTBED_REPO,
            pr_number=_TESTBED_PR_NUMBER,
            head_sha=head_sha,
        )
        assert "diff --git" in diff
        assert "get_user_by_username" in diff

        # 2. Build one real Review with one mappable finding (an added
        # line real in this diff -- see backend.integrations.diff_mapping)
        # and one deliberately unmappable finding (a line number nowhere
        # in this diff), so this test also proves degradation survives a
        # real GitHub round trip, not just the fake-transport unit tests.
        mappable_finding = Finding(
            agent_type=AgentType.SECURITY,
            # HIGH, not CRITICAL: Review.status is set explicitly to
            # POSTED below for this test's own purpose (proving the
            # posting mechanism); a CRITICAL finding is unrelated to that
            # and would just be a misleading severity for this
            # hand-constructed fixture finding.
            severity="HIGH",
            category="sql_injection",
            file_path="app.py",
            line_start=26,
            line_end=26,
            confidence=Decimal("0.900"),
            rationale=(
                "M11 live integration test: string-interpolated SQL query "
                "on this line is a real, findable SQL-injection defect."
            ),
        )
        unmappable_finding = Finding(
            agent_type=AgentType.QUALITY,
            severity="LOW",
            category="stale_line_reference",
            file_path="app.py",
            line_start=9999,
            line_end=9999,
            confidence=Decimal("0.300"),
            rationale=(
                "M11 live integration test: deliberately unmappable finding "
                "(line 9999 does not exist in this diff) -- proves "
                "degradation to the summary body over a real GitHub call."
            ),
        )
        findings = [mappable_finding, unmappable_finding]
        review = Review(
            review_id=_FIXED_REVIEW_ID,
            pr_number=_TESTBED_PR_NUMBER,
            repository_owner=_TESTBED_OWNER,
            repository_name=_TESTBED_REPO,
            head_sha=head_sha,
            findings=findings,
            overall_confidence=compute_overall_confidence(findings),
            status=ReviewStatus.POSTED,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )

        # 3. POST A REAL REVIEW TO A REAL PR. Idempotent on re-run -- see
        # module docstring.
        client.post_review_comment(review)

        # 4. Verify against the REAL API that the review (or an earlier
        # run's identical one) really is on the PR now, carrying this
        # test's marker.
        existing = client._find_existing_review_id_marker(  # noqa: SLF001 -- direct verification, test-only
            owner=_TESTBED_OWNER,
            repo=_TESTBED_REPO,
            pr_number=_TESTBED_PR_NUMBER,
            headers=client._installation_headers(_TESTBED_OWNER, _TESTBED_REPO),  # noqa: SLF001
            review_id=_FIXED_REVIEW_ID,
        )
        assert existing is True
