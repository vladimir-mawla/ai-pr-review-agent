"""Unit tests for backend.integrations.github_client.RealGitHubClient.

Every test drives ``RealGitHubClient`` against a fake ``httpx.MockTransport``
-- no real network access, no real GitHub App. This is what proves the
composition (auth -> diff fetch -> diff-position mapping -> degradation ->
idempotency -> post) actually works end-to-end, the "test the COMPOSITION"
lesson this project's own checkpoint explicitly calls back to from M5.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.core.settings import Settings
from backend.integrations.github_auth import GitHubAuthError
from backend.integrations.github_client import (
    GitHubForbiddenError,
    GitHubUnauthorizedError,
    GitHubValidationError,
    RealGitHubClient,
    build_github_client,
)
from backend.models import AgentType, Finding, Review, ReviewStatus, compute_overall_confidence

_SAMPLE_PATCH = (
    "@@ -1,3 +1,4 @@\n"
    " def handler(request):\n"
    "+    execute(f\"SELECT * FROM users WHERE id={request.id}\")\n"
    "     return render(request)\n"
    " \n"
)


@pytest.fixture(scope="module")
def private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


@pytest.fixture
def key_path(tmp_path, private_key_pem: str) -> str:  # type: ignore[no-untyped-def]
    path = tmp_path / "test-github-app.pem"
    path.write_text(private_key_pem, encoding="utf-8")
    return str(path)


@pytest.fixture
def real_settings(key_path: str) -> Settings:
    return Settings(
        github_webhook_secret="unused-in-these-tests",
        github_client_backend="real",
        github_app_id="4781442",
        github_app_private_key_path=key_path,
        github_retry_max_attempts=3,
        github_retry_base_delay_seconds=0.001,
        github_retry_max_delay_seconds=0.002,
    )


def _one_finding(**overrides: object) -> Finding:
    defaults: dict[str, object] = {
        "agent_type": AgentType.SECURITY,
        "severity": "CRITICAL",
        "category": "sql_injection",
        "file_path": "src/app.py",
        "line_start": 2,
        "line_end": 2,
        "confidence": Decimal("0.950"),
        "rationale": "User input interpolated directly into a SQL query.",
    }
    defaults.update(overrides)
    if "line_start" in overrides and "line_end" not in overrides:
        defaults["line_end"] = overrides["line_start"]
    return Finding(**defaults)  # type: ignore[arg-type]


def _review(findings: list[Finding], *, review_id: str = "test-review-1") -> Review:
    return Review(
        review_id=review_id,
        pr_number=42,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        findings=findings,
        overall_confidence=compute_overall_confidence(findings),
        status=ReviewStatus.POSTED,
        created_at=datetime(2026, 8, 31, 12, 0, 0),
    )


class _Router:
    """Small request router for building a fake GitHub API out of an httpx.MockTransport.

    Records every request it handles (test/inspection surface) so tests
    can assert things like "no POST was ever issued" without a separate
    spy object.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.installation_response = httpx.Response(200, json={"id": 157972840, "repository_selection": "selected"})
        self.token_response = httpx.Response(
            201, json={"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z"}
        )
        self.pr_diff_text = _SAMPLE_PATCH
        self.changed_files_json: list[dict[str, object]] = [
            {"filename": "src/app.py", "status": "modified", "patch": _SAMPLE_PATCH}
        ]
        self.existing_reviews_json: list[dict[str, object]] = []
        self.post_review_response = httpx.Response(
            201, json={"id": 999, "html_url": "https://github.com/acme/widgets/pull/42#pullrequestreview-999", "state": "COMMENTED"}
        )
        self.post_review_calls: list[dict[str, object]] = []
        # Per-path status-code override queues, for rate-limit/auth tests:
        # e.g. {"/repos/acme/widgets/pulls/42/reviews_POST": [403, 403, 201]}
        self._status_queue: dict[str, list[int]] = {}
        self._rate_limit_headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}

    def queue_statuses(self, key: str, statuses: list[int]) -> None:
        self._status_queue[key] = list(statuses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        key = f"{path}_{method}"

        if key in self._status_queue and self._status_queue[key]:
            forced_status = self._status_queue[key].pop(0)
            if forced_status >= 400:
                headers = self._rate_limit_headers if forced_status in (403, 429) else {}
                return httpx.Response(forced_status, json={"message": "forced"}, headers=headers)
            # fall through to normal handling for a forced-success status

        if path.endswith("/installation"):
            return self.installation_response
        if path.endswith("/access_tokens"):
            return self.token_response
        if path.endswith("/reviews") and method == "GET":
            return httpx.Response(200, json=self.existing_reviews_json)
        if path.endswith("/reviews") and method == "POST":
            self.post_review_calls.append(json.loads(request.content))
            return self.post_review_response
        if path.endswith("/files"):
            return httpx.Response(200, json=self.changed_files_json)
        if path.endswith("/pulls/42") and request.headers.get("accept") == "application/vnd.github.diff":
            return httpx.Response(200, text=self.pr_diff_text)
        if path.endswith("/pulls/42"):
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "state": "open",
                    "head": {"sha": "a" * 40, "ref": "feature"},
                    "base": {"sha": "b" * 40, "ref": "main"},
                },
            )
        raise AssertionError(f"unexpected request: {method} {path}")


def _client_with_router(real_settings: Settings, router: _Router) -> RealGitHubClient:
    http_client = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(router)
    )
    return RealGitHubClient(real_settings, http_client=http_client)


class TestFetchDiff:
    def test_fetch_diff_returns_the_real_diff_text(self, real_settings: Settings) -> None:
        router = _Router()
        client = _client_with_router(real_settings, router)
        diff = client.fetch_diff(
            repository_owner="acme", repository_name="widgets", pr_number=42, head_sha="a" * 40
        )
        assert diff == _SAMPLE_PATCH


class TestPostReviewComment:
    def test_mappable_and_unmappable_findings_both_survive_the_post(self, real_settings: Settings) -> None:
        """One unmappable finding must not lose the other findings -- the core M11 requirement."""
        mappable = _one_finding(file_path="src/app.py", line_start=2)  # an added line
        unmappable = _one_finding(
            file_path="src/app.py", line_start=999, category="missing_test", severity="LOW", confidence=Decimal("0.500")
        )
        review = _review([mappable, unmappable])

        router = _Router()
        client = _client_with_router(real_settings, router)
        client.post_review_comment(review)

        assert len(router.post_review_calls) == 1
        posted_body = router.post_review_calls[0]
        # The mappable finding became exactly one inline comment.
        assert len(posted_body["comments"]) == 1
        assert posted_body["comments"][0]["path"] == "src/app.py"
        assert posted_body["comments"][0]["line"] == 2
        assert posted_body["comments"][0]["side"] == "RIGHT"
        # The unmappable finding's content survives in the summary body.
        assert "missing_test" in posted_body["body"]
        assert "999" in posted_body["body"]
        # event is always COMMENT -- this system never approves/blocks.
        assert posted_body["event"] == "COMMENT"

    def test_all_findings_unmappable_still_posts_summary_only_review(self, real_settings: Settings) -> None:
        unmappable = _one_finding(file_path="src/app.py", line_start=999)
        review = _review([unmappable])

        router = _Router()
        client = _client_with_router(real_settings, router)
        client.post_review_comment(review)

        posted_body = router.post_review_calls[0]
        assert posted_body["comments"] == []
        assert "sql_injection" in posted_body["body"]

    def test_idempotent_post_is_skipped_when_review_id_marker_already_present(
        self, real_settings: Settings
    ) -> None:
        review = _review([_one_finding()], review_id="already-posted-1")
        router = _Router()
        router.existing_reviews_json = [
            {"id": 1, "body": "some earlier review <!-- pr-review-agent:review_id=already-posted-1 -->"}
        ]
        client = _client_with_router(real_settings, router)

        client.post_review_comment(review)

        assert router.post_review_calls == []

    def test_a_different_review_id_on_the_pr_does_not_block_a_new_post(self, real_settings: Settings) -> None:
        review = _review([_one_finding()], review_id="a-new-review")
        router = _Router()
        router.existing_reviews_json = [
            {"id": 1, "body": "<!-- pr-review-agent:review_id=some-other-review -->"}
        ]
        client = _client_with_router(real_settings, router)

        client.post_review_comment(review)

        assert len(router.post_review_calls) == 1

    def test_422_from_github_is_surfaced_not_swallowed(self, real_settings: Settings) -> None:
        review = _review([_one_finding()])
        router = _Router()
        router.queue_statuses("/repos/acme/widgets/pulls/42/reviews_POST", [422])
        client = _client_with_router(real_settings, router)

        with pytest.raises(GitHubValidationError):
            client.post_review_comment(review)


class TestRetryClassification:
    """Rate-limit 403 is treated as retryable, a 401 is not."""

    def test_rate_limited_403_is_retried_and_can_eventually_succeed(self, real_settings: Settings) -> None:
        review = _review([_one_finding()])
        router = _Router()
        # Fail twice with a rate-limit 403, then succeed on the 3rd attempt
        # (github_retry_max_attempts=3 in real_settings fixture).
        router.queue_statuses("/repos/acme/widgets/pulls/42/reviews_POST", [403, 403])
        client = _client_with_router(real_settings, router)

        client.post_review_comment(review)

        assert len(router.post_review_calls) == 1  # only the final, successful attempt has a body

    def test_401_is_not_retried_and_raises_immediately(self, real_settings: Settings) -> None:
        review = _review([_one_finding()])
        router = _Router()
        router.queue_statuses("/repos/acme/widgets/pulls/42/reviews_POST", [401, 401, 401])
        client = _client_with_router(real_settings, router)

        with pytest.raises(GitHubUnauthorizedError):
            client.post_review_comment(review)

        # Non-retryable: exactly one POST attempt was made, not up to
        # github_retry_max_attempts of them.
        post_attempts = [
            r for r in router.requests if r.method == "POST" and r.url.path.endswith("/reviews")
        ]
        assert len(post_attempts) == 1

    def test_generic_403_without_rate_limit_headers_is_forbidden_not_retried(
        self, real_settings: Settings
    ) -> None:
        review = _review([_one_finding()])
        router = _Router()

        def handler(request: httpx.Request) -> httpx.Response:
            router.requests.append(request)
            if request.url.path.endswith("/installation"):
                return router.installation_response
            if request.url.path.endswith("/access_tokens"):
                return router.token_response
            if request.url.path.endswith("/reviews") and request.method == "GET":
                return httpx.Response(200, json=[])
            if request.url.path.endswith("/reviews") and request.method == "POST":
                # A plain permission-denied 403 -- no rate-limit headers.
                return httpx.Response(403, json={"message": "Resource not accessible"})
            if request.url.path.endswith("/files"):
                return httpx.Response(200, json=router.changed_files_json)
            if request.url.path.endswith("/pulls/42"):
                return httpx.Response(200, text=router.pr_diff_text)
            raise AssertionError(f"unexpected: {request.method} {request.url.path}")

        http_client = httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        )
        client = RealGitHubClient(real_settings, http_client=http_client)

        with pytest.raises(GitHubForbiddenError):
            client.post_review_comment(review)

        post_attempts = [
            r for r in router.requests if r.method == "POST" and r.url.path.endswith("/reviews")
        ]
        assert len(post_attempts) == 1


class TestQueueForHitl:
    def test_queue_for_hitl_makes_no_network_call(self, real_settings: Settings) -> None:
        review = _review([_one_finding()])
        review = review.model_copy(update={"status": ReviewStatus.QUEUED_FOR_HITL})
        router = _Router()
        client = _client_with_router(real_settings, router)

        client.queue_for_hitl(review)

        assert router.requests == []


class TestBuildGitHubClient:
    def test_defaults_to_mock_client(self) -> None:
        settings = Settings(github_webhook_secret="x")
        client = build_github_client(settings)
        assert type(client).__name__ == "MockGitHubClient"

    def test_real_backend_requires_credentials(self) -> None:
        # Explicit None overrides here matter: this repo's own .env (real
        # dev credentials) would otherwise supply github_app_id/
        # github_app_private_key_path via pydantic-settings' env-file
        # loading, masking exactly the "missing credential" case this test
        # means to cover.
        settings = Settings(
            github_webhook_secret="x",
            github_client_backend="real",
            github_app_id=None,
            github_app_private_key_path=None,
        )
        with pytest.raises(GitHubAuthError):
            build_github_client(settings)
