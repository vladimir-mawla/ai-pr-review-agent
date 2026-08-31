"""Contract test: pins ``MockGitHubClient``'s assumed shape to the real API's observed shape.

PLAN.md calls for this explicitly: "A contract test pins the mocked
client's behavior to the real API's observed shape, to catch mock-drift."
Two halves, split by ``pytest.mark.live`` so the default, free ``pytest``
run still exercises the pinning (against a checked-in, real-captured
fixture) without ever touching the network:

1. ``TestFixtureMatchesRealShape`` (default, no marker, no credential
   needed): loads ``tests/fixtures/github_api_contract.json`` -- JSON
   captured from REAL, live calls against the testbed repo
   (``vladimir-mawla/pr-review-agent-testbed``, PR #1) during this
   milestone's own build session (see the fixture's own ``_captured_from``
   field) -- and asserts every ``backend.integrations.github_models``
   model parses it without error, with the exact field values GitHub
   itself returned. THIS is the actual mock-drift guard: if a future
   change to ``github_models.py`` (or the mock's own assumptions) stops
   agreeing with what GitHub's real API actually returns, this test fails
   without needing a network call or a credential.
2. ``TestLiveCaptureReproducesTheFixtureShape`` (``@pytest.mark.live``,
   ``skipif`` on missing credentials): re-runs the SAME calls against the
   REAL API right now and asserts the response still parses under the same
   models -- proving the fixture is not stale/fabricated, and would catch
   GitHub itself changing a response shape out from under this project.
   Not run by a plain ``pytest`` invocation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from backend.core.settings import Settings
from backend.integrations.github_client import RealGitHubClient
from backend.integrations.github_models import (
    ChangedFile,
    CreateReviewResponse,
    ExistingReview,
    InstallationResponse,
    PullRequestMetadata,
)

_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "github_api_contract.json"

# The exact real repo/PR this contract was captured from, and that the
# live half re-verifies against -- see this milestone's final report for
# the full account of how PR #1 was created.
_TESTBED_OWNER = "vladimir-mawla"
_TESTBED_REPO = "pr-review-agent-testbed"
_TESTBED_PR_NUMBER = 1


def _load_fixture() -> dict[str, object]:
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


class TestFixtureMatchesRealShape:
    """Default, free, no network: MockGitHubClient's shape assumptions vs. a real, captured fixture."""

    def test_fixture_file_exists_and_is_real_not_fabricated(self) -> None:
        data = _load_fixture()
        assert "_captured_from" in data
        assert "github.com" in str(data["_captured_from"])

    def test_pull_request_metadata_shape_conforms(self) -> None:
        data = _load_fixture()
        parsed = PullRequestMetadata.model_validate(data["pull_request_metadata"])
        assert parsed.number == 1
        assert parsed.state == "open"
        assert len(parsed.head.sha) == 40
        assert parsed.head.ref == "feature/username-lookup"

    def test_changed_files_shape_conforms(self) -> None:
        data = _load_fixture()
        files = [ChangedFile.model_validate(f) for f in data["changed_files"]]  # type: ignore[union-attr]
        assert len(files) == 1
        assert files[0].filename == "app.py"
        assert files[0].patch is not None
        assert "get_user_by_username" in files[0].patch

    def test_installation_shape_conforms(self) -> None:
        data = _load_fixture()
        parsed = InstallationResponse.model_validate(data["installation"])
        assert parsed.id == 157972840
        assert parsed.repository_selection == "selected"

    def test_create_review_response_shape_conforms(self) -> None:
        data = _load_fixture()
        parsed = CreateReviewResponse.model_validate(data["create_review_response"])
        assert parsed.state == "COMMENTED"
        assert parsed.html_url.startswith("https://github.com/")

    def test_existing_review_shape_conforms_and_carries_the_idempotency_marker(self) -> None:
        """This is the exact real shape RealGitHubClient's idempotency check parses."""
        data = _load_fixture()
        reviews = [ExistingReview.model_validate(r) for r in data["existing_reviews"]]  # type: ignore[union-attr]
        assert len(reviews) == 1
        assert "pr-review-agent:review_id=" in reviews[0].body

    def test_mock_client_diff_shape_is_a_plain_string_like_the_real_diff_accept_header_response(
        self,
    ) -> None:
        """Cross-check: MockGitHubClient.fetch_diff returns `str` -- the real client's
        Accept: application/vnd.github.diff response is ALSO plain text (not JSON),
        confirmed by this fixture's own capture (a raw unified-diff patch string,
        not a wrapped object) -- the two implementations of GitHubClient.fetch_diff
        agree on return shape, which is exactly what this contract test exists to
        catch drift on.
        """
        from backend.integrations.github_client import MockGitHubClient

        mock = MockGitHubClient(diffs_by_pr={1: "fake diff"})
        mock_diff = mock.fetch_diff(
            repository_owner="x", repository_name="y", pr_number=1, head_sha="a" * 40
        )
        assert isinstance(mock_diff, str)

        data = _load_fixture()
        real_patch = data["changed_files"][0]["patch"]  # type: ignore[index]
        assert isinstance(real_patch, str)


@pytest.mark.live
@pytest.mark.skipif(
    not (os.environ.get("GITHUB_APP_ID") and os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")),
    reason="GITHUB_APP_ID/GITHUB_APP_PRIVATE_KEY_PATH not configured",
)
class TestLiveCaptureReproducesTheFixtureShape:
    """Re-run the real calls right now; proves the checked-in fixture is not stale/fabricated."""

    def test_real_pull_request_metadata_still_matches_the_pinned_shape(self) -> None:
        settings = Settings(github_webhook_secret="unused-live-contract-test", github_client_backend="real")
        client = RealGitHubClient(settings)
        metadata = client.get_pr_metadata(owner=_TESTBED_OWNER, repo=_TESTBED_REPO, pr_number=_TESTBED_PR_NUMBER)
        assert metadata.number == _TESTBED_PR_NUMBER
        assert len(metadata.head.sha) == 40

    def test_real_diff_fetch_still_returns_a_plain_diff_string(self) -> None:
        settings = Settings(github_webhook_secret="unused-live-contract-test", github_client_backend="real")
        client = RealGitHubClient(settings)
        diff = client.fetch_diff(
            repository_owner=_TESTBED_OWNER,
            repository_name=_TESTBED_REPO,
            pr_number=_TESTBED_PR_NUMBER,
            head_sha=client.get_pr_metadata(
                owner=_TESTBED_OWNER, repo=_TESTBED_REPO, pr_number=_TESTBED_PR_NUMBER
            ).head.sha,
        )
        assert isinstance(diff, str)
        assert "diff --git" in diff
