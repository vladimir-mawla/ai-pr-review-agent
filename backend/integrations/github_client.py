"""GitHub client: mock-backed (M10, unchanged as the default) and real (M11) implementations.

Owns the abstraction PLAN.md's M10 freeze boundary established and M11's
own text names explicitly ("the mocked ``github_client`` from M10 is
swapped for the real REST wrapper behind the same interface"): the shape
``backend.cli.review_local`` and ``backend.job_queue.arq_worker`` program
against so a completed ``Review`` (and, before that, the diff to review) is
never fetched/posted through a hardcoded, un-swappable call. ``GitHubClient``
and ``MockGitHubClient`` are UNCHANGED from M10 (see their own docstrings
below) -- this milestone adds ``RealGitHubClient`` behind the same
``Protocol`` and ``build_github_client``, the settings-driven factory that
picks between them (``Settings.github_client_backend``, default ``"mock"``
so a keyless checkout still works -- see ``backend.core.settings``).

REAL CLIENT DESIGN, top to bottom:

1. AUTH (``backend.integrations.github_auth`` /
   ``backend.security.rbac``). Every real call first resolves the
   installation id authorized for the target ``(owner, repo)``
   (``RepositoryAuthorizer``, which also refuses to act on a repo this App
   is not installed on) and then a cached, auto-refreshed installation
   access token (``InstallationTokenCache``). See both modules' docstrings
   for the full two-step JWT -> installation-token flow and why caching
   matters.

2. RELIABILITY (reused, not hand-rolled). Every real outbound GitHub REST
   call -- including the auth handshake's own two calls (installation
   discovery, token exchange) -- goes through the exact same composition
   ``backend.job_queue.redis_arq.RedisJobQueue`` and
   ``backend.tools.llm_client.AnthropicLLMClient`` use for their own
   outbound calls:

       retry (backend.reliability.retry.call_with_retry)
         -> circuit breaker (backend.reliability.circuit_breaker.CircuitBreaker)
           -> httpx's own native per-request timeout (httpx.Timeout, set
              once on the shared httpx.Client -- unlike Redis's
              synchronous client, httpx has a real socket-level timeout of
              its own, so this leg does not need the thread-pool-based
              ``run_with_timeout`` the M6/M7 call sites use for a client
              with no native timeout support)

   A brand-new unprotected outbound call here would violate the exact
   DONE.html gate ("every outbound call has a timeout / circuit-breaker")
   this project has been held to since M6.

   GitHub-specific response classification (``_raise_for_status``): a 401
   is ``GitHubUnauthorizedError`` (non-retryable -- the credential itself
   is bad, not transiently unavailable); a 403/429 that GitHub's own
   ``x-ratelimit-remaining: 0`` header (or a bare 429) identifies as rate
   limiting is ``GitHubRateLimitedError`` (retryable -- this project's
   generic exponential-backoff retry, not a bespoke
   sleep-until-x-ratelimit-reset scheduler, since the existing retry
   policy's bounded attempts/backoff is judged sufficient for this
   milestone's scope); any other 403 is ``GitHubForbiddenError``
   (non-retryable -- a real permission problem); a 422 is
   ``GitHubValidationError`` and is NEVER caught/swallowed anywhere in this
   module -- it propagates all the way to the caller, per this milestone's
   explicit "a 422 must be surfaced clearly, not swallowed" requirement.

3. DIFF-POSITION MAPPING + DEGRADATION (``post_review_comment``, using
   ``backend.integrations.diff_mapping``). Every finding is independently
   classified as mappable (becomes a real inline ``line``+``side`` comment)
   or not (its text is folded into the review's summary body instead of
   being dropped or attempted as a comment that would 422 the WHOLE
   review). See ``post_review_comment``'s own docstring for the exact
   mechanics.

4. IDEMPOTENCY (``post_review_comment``). ``.genesis/checkpoints/
   CURRENT.md``'s M10 Deferred list flagged a real risk this milestone
   must close: ARQ's default job-level retry could call
   ``post_review_comment`` a SECOND time for the same review (e.g. the job
   function raises after a successful post but before returning). Before
   posting, this method checks the PR's existing reviews for this
   project's own hidden marker comment (embedding ``review.review_id``)
   and skips posting entirely if a matching review is already there --
   turning "the whole job function is retried from the top" into a safe
   no-op on its second execution, structurally, not by trusting the caller
   to dedupe.

5. ``queue_for_hitl`` IS DELIBERATELY A NO-OP (LOG ONLY) FOR THE REAL
   CLIENT: there is still no durable, dashboard-visible HITL queue in this
   codebase (``backend.hitl.queue.InMemoryHitlQueue`` has no live call site
   as of M10 either -- see that milestone's own Deferred notes) -- M13's
   dashboard is the natural place to build one. A ``QUEUED_FOR_HITL``
   review today means "do not post to GitHub", which this method already
   guarantees by simply not calling GitHub at all; it does not yet mean
   "and a human can see it queued somewhere".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

import httpx

from backend.core.settings import Settings, get_settings
from backend.integrations.diff_mapping import build_diff_index, map_finding_to_anchor
from backend.integrations.github_auth import (
    GitHubAuthError,
    InstallationTokenCache,
    mint_app_jwt,
)
from backend.integrations.github_models import (
    ChangedFile,
    CreateReviewRequest,
    CreateReviewResponse,
    ExistingReview,
    PullRequestMetadata,
    ReviewCommentInput,
)
from backend.models import Finding, Review, ReviewStatus
from backend.reliability.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitOpenError,
    register,
)
from backend.reliability.retry import RetryExhaustedError, RetryPolicy, call_with_retry
from backend.security.rbac import RepositoryAuthorizer, RepositoryNotAuthorizedError

logger = logging.getLogger(__name__)

# The default diff MockGitHubClient.fetch_diff returns when no diff was
# pre-configured for a given PR number -- an empty diff is a safe,
# harmless default (every specialist analyzing "" simply has nothing to
# say), never a real repository's content.
_DEFAULT_MOCK_DIFF = ""

# Hidden HTML-comment marker embedded in every posted review body, keyed by
# review_id -- see module docstring's "IDEMPOTENCY" section. An HTML
# comment renders as nothing in GitHub's markdown view, so it costs a
# reviewer nothing visually while giving post_review_comment a reliable,
# review_id-scoped fingerprint to search for on retry.
_IDEMPOTENCY_MARKER_PREFIX = "<!-- pr-review-agent:review_id="
_IDEMPOTENCY_MARKER_SUFFIX = " -->"


class GitHubClient(Protocol):
    """The shape a caller (CLI, ARQ worker) needs from a GitHub client.

    A ``Protocol`` (structural typing), not an ABC -- so a test can hand a
    caller a bare fake object satisfying just these methods, mirroring
    ``backend.tools.llm_client.LLMClientProtocol``'s and
    ``backend.agents.base_agent.RetrieverProtocol``'s existing pattern in
    this codebase. UNCHANGED from M10.
    """

    def fetch_diff(
        self,
        *,
        repository_owner: str,
        repository_name: str,
        pr_number: int,
        head_sha: str,
    ) -> str:
        """Return the unified diff text for this PR at ``head_sha``."""
        ...

    def post_review_comment(self, review: Review) -> None:
        """Post ``review`` as a real PR comment. Called only for ``ReviewStatus.POSTED``."""
        ...

    def queue_for_hitl(self, review: Review) -> None:
        """Hand ``review`` to the human-approval queue instead of posting it."""
        ...


class MockGitHubClient:
    """Mock-backed ``GitHubClient``: never touches a real repository. UNCHANGED from M10.

    Attributes (test/inspection surface):
        posted_reviews: Every ``Review`` passed to ``post_review_comment``,
            in call order.
        queued_reviews: Every ``Review`` passed to ``queue_for_hitl``, in
            call order.

    Construction:
        diffs_by_pr: Optional ``{pr_number: diff_text}`` map so a test/demo
            can control exactly what ``fetch_diff`` returns for a given PR
            number, without needing a real GitHub API.
        default_diff: Returned by ``fetch_diff`` for any PR number not in
            ``diffs_by_pr``. Defaults to an empty diff.
    """

    def __init__(
        self,
        *,
        diffs_by_pr: dict[int, str] | None = None,
        default_diff: str = _DEFAULT_MOCK_DIFF,
    ) -> None:
        self._diffs_by_pr = dict(diffs_by_pr) if diffs_by_pr is not None else {}
        self._default_diff = default_diff
        self.posted_reviews: list[Review] = []
        self.queued_reviews: list[Review] = []

    def fetch_diff(
        self,
        *,
        repository_owner: str,
        repository_name: str,
        pr_number: int,
        head_sha: str,
    ) -> str:
        """Return the pre-configured diff for ``pr_number``, or ``default_diff``."""
        return self._diffs_by_pr.get(pr_number, self._default_diff)

    def post_review_comment(self, review: Review) -> None:
        """Record ``review`` as posted. Never makes a real network call."""
        self.posted_reviews.append(review)

    def queue_for_hitl(self, review: Review) -> None:
        """Record ``review`` as queued for human review. Never makes a real network call."""
        self.queued_reviews.append(review)

    def total_calls(self) -> int:
        """Total ``post_review_comment`` + ``queue_for_hitl`` calls recorded so far.

        Used by callers/tests to assert the "exactly one post or one
        queue_for_hitl call, never both" success criterion directly as a
        single number, rather than checking two list lengths separately.
        """
        return len(self.posted_reviews) + len(self.queued_reviews)


def post_or_queue(client: GitHubClient, review: Review) -> None:
    """Dispatch ``review`` to exactly one of ``post_review_comment``/``queue_for_hitl``.

    The single, shared decision point every caller (``backend.cli.
    review_local``, ``backend.job_queue.arq_worker``) uses instead of each
    re-implementing its own status check -- structurally guarantees "never
    both" (this function calls exactly one of the two methods, always).
    ``ReviewStatus.POSTED`` posts; every other status (``QUEUED_FOR_HITL``,
    and the conservative default for ``REJECTED``/``ERROR``, neither of
    which ``backend.orchestrator.nodes.aggregate_node`` actually produces
    today) queues for human review rather than risking an unreviewed
    auto-post. UNCHANGED from M10.
    """
    if review.status == ReviewStatus.POSTED:
        client.post_review_comment(review)
    else:
        client.queue_for_hitl(review)


# ---------------------------------------------------------------------------
# M11: the real REST client.
# ---------------------------------------------------------------------------


class GitHubAPIError(Exception):
    """Base class for a real, non-2xx response from the GitHub REST API."""


class GitHubUnauthorizedError(GitHubAPIError):
    """401 -- the installation token is invalid/revoked. Never retried."""


class GitHubForbiddenError(GitHubAPIError):
    """403 that is NOT a rate limit (``x-ratelimit-remaining`` was not ``"0"``). Never retried."""


class GitHubRateLimitedError(GitHubAPIError):
    """403 with ``x-ratelimit-remaining: 0``, or 429. Transient -- retried with backoff."""


class GitHubValidationError(GitHubAPIError):
    """422 -- GitHub rejected the request body. Never retried, never swallowed by this module."""


class GitHubUnavailableError(GitHubAPIError):
    """Wraps ``RetryExhaustedError``/``CircuitOpenError`` from the reliability layer.

    Mirrors ``backend.job_queue.interface.QueueUnavailableError`` and
    ``backend.tools.llm_client.LLMCallFailedError``'s role: one exception
    type a caller can catch regardless of which of the two underlying
    reliability failures actually happened.
    """


# Exceptions that mean "this call itself can never succeed by trying
# again", extending retry's own default set (TypeError/ValueError/KeyError/
# AttributeError) with everything above that is a genuine, non-transient
# rejection rather than a struggling dependency -- mirrors
# backend.job_queue.redis_arq's and backend.tools.llm_client's own
# extension pattern. GitHubValidationError is included here (never
# retried) for the same reason a malformed Anthropic request body is never
# retried in AnthropicLLMClient: the request that produced the 422 does
# not change on a second attempt.
_NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TypeError,
    ValueError,
    KeyError,
    AttributeError,
    CircuitOpenError,
    GitHubUnauthorizedError,
    GitHubForbiddenError,
    GitHubValidationError,
    GitHubAuthError,
    RepositoryNotAuthorizedError,
)

_CIRCUIT_BREAKER_NAME = "github_api"
_ACCEPT_JSON = "application/vnd.github+json"
_ACCEPT_DIFF = "application/vnd.github.diff"
_API_VERSION_HEADER = "2022-11-28"
_FILES_PAGE_SIZE = 100
_RESPONSE_BODY_PREVIEW_CHARS = 500


def _response_preview(response: httpx.Response) -> str:
    """A short, safe preview of a response body for error messages -- never key/token material."""
    text = response.text
    if len(text) > _RESPONSE_BODY_PREVIEW_CHARS:
        return text[:_RESPONSE_BODY_PREVIEW_CHARS] + "...(truncated)"
    return text


def _raise_for_status(response: httpx.Response, *, context: str) -> None:
    """Classify a GitHub REST response, raising the matching typed exception for any non-2xx.

    See module docstring's "RELIABILITY" section for the classification
    rules and why each status maps to retryable vs not.
    """
    if response.status_code < 300:
        return
    if response.status_code == 401:
        raise GitHubUnauthorizedError(f"{context}: HTTP 401 -- {_response_preview(response)}")
    if response.status_code == 429 or (
        response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
    ):
        reset = response.headers.get("x-ratelimit-reset", "unknown")
        raise GitHubRateLimitedError(
            f"{context}: rate limited (HTTP {response.status_code}, resets at epoch {reset})"
        )
    if response.status_code == 403:
        raise GitHubForbiddenError(f"{context}: HTTP 403 -- {_response_preview(response)}")
    if response.status_code == 422:
        raise GitHubValidationError(f"{context}: HTTP 422 -- {_response_preview(response)}")
    raise GitHubAPIError(f"{context}: HTTP {response.status_code} -- {_response_preview(response)}")


def _severity_emoji(severity: str) -> str:
    return {
        "CRITICAL": "\U0001f6a8",
        "HIGH": "⚠️",
        "MEDIUM": "\U0001f4dd",
        "LOW": "ℹ️",
        "INFO": "\U0001f4ac",
    }.get(severity, "•")


def _format_finding_body(finding: Finding) -> str:
    """Render one ``Finding`` as inline-comment markdown."""
    return (
        f"{_severity_emoji(finding.severity.value)} **{finding.severity.value}** "
        f"({finding.agent_type.value} / {finding.category}, "
        f"confidence {finding.confidence})\n\n{finding.rationale}"
    )


def _format_summary_body(
    review: Review, degraded_findings: list[Finding], *, mapped_count: int
) -> str:
    """Render the top-level review comment: overview + any degraded findings.

    A finding that could not be mapped to a real diff line (see
    ``backend.integrations.diff_mapping``'s module docstring) is rendered
    here, in full, rather than silently dropped -- see this module's
    docstring's "DIFF-POSITION MAPPING + DEGRADATION" section.
    """
    lines = [
        "## pr-review-agent review",
        "",
        f"**{len(review.findings)}** finding(s), overall confidence "
        f"**{review.overall_confidence}** -- {mapped_count} posted inline, "
        f"{len(degraded_findings)} below.",
    ]
    if degraded_findings:
        lines.append("")
        lines.append(
            "### Additional findings (could not be anchored to a specific diff line)"
        )
        lines.append(
            "_These findings' reported line numbers do not correspond to a line "
            "this diff actually touches or shows as context (a stale line number, "
            "or a file outside this PR) -- shown here instead of being dropped._"
        )
        for finding in degraded_findings:
            lines.append("")
            lines.append(f"- `{finding.file_path}:{finding.line_start}` -- {_format_finding_body(finding)}")
    lines.append("")
    lines.append(f"{_IDEMPOTENCY_MARKER_PREFIX}{review.review_id}{_IDEMPOTENCY_MARKER_SUFFIX}")
    return "\n".join(lines)


class RealGitHubClient:
    """Real ``GitHubClient``: authenticates as a real GitHub App and makes real REST calls.

    See module docstring for the full design (auth, reliability, diff
    mapping, idempotency). Construction reads the App's private key from
    disk once (``settings.github_app_private_key_path``) and never again;
    the key's bytes are held only in ``self._private_key_pem`` and are
    never logged, printed, or included in any exception message this class
    raises.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: httpx.Client | None = None,
    ) -> None:
        self._settings = settings if settings is not None else get_settings()
        if not self._settings.github_app_id or not self._settings.github_app_private_key_path:
            raise GitHubAuthError(
                "github_client_backend='real' requires both github_app_id and "
                "github_app_private_key_path to be configured"
            )
        self._app_id = self._settings.github_app_id
        with open(self._settings.github_app_private_key_path, encoding="utf-8") as key_file:
            self._private_key_pem = key_file.read()

        self._http_client = http_client if http_client is not None else httpx.Client(
            base_url=self._settings.github_api_base_url,
            timeout=httpx.Timeout(self._settings.github_timeout_seconds),
        )
        self._token_cache = InstallationTokenCache(
            app_id=self._app_id,
            private_key_pem=self._private_key_pem,
            http_client=self._http_client,
        )
        self._authorizer = RepositoryAuthorizer(app_id=self._app_id, http_client=self._http_client)

        self._retry_policy = RetryPolicy(
            max_attempts=self._settings.github_retry_max_attempts,
            base_delay_seconds=self._settings.github_retry_base_delay_seconds,
            max_delay_seconds=self._settings.github_retry_max_delay_seconds,
        )
        self._breaker = register(
            CircuitBreaker(
                CircuitBreakerConfig(
                    failure_threshold=self._settings.github_circuit_breaker_failure_threshold,
                    reset_timeout_seconds=self._settings.github_circuit_breaker_reset_timeout_seconds,
                ),
                name=_CIRCUIT_BREAKER_NAME,
            )
        )

        # (owner, repo, pr_number, head_sha) -> (diff_text, changed_files).
        # Populated by fetch_diff and/or post_review_comment, whichever
        # runs first for a given key -- so a caller that invokes both for
        # the same review (backend.job_queue.arq_worker does exactly this:
        # fetch_diff to build the orchestrator's input, then
        # post_review_comment once the review is produced) only ever pays
        # for the PR-metadata/diff/changed-files calls once.
        self._diff_cache: dict[tuple[str, str, int, str], tuple[str, list[ChangedFile]]] = {}

    def _call_reliably[T](self, func: Callable[[], T]) -> T:
        """Run one real GitHub REST call through retry -> circuit breaker.

        Mirrors ``backend.job_queue.redis_arq.RedisJobQueue._call_reliably``
        exactly (see that method's docstring for the retry-outermost/
        breaker-inner reasoning) -- the one difference is that the timeout
        leg is not this method's concern here: it is enforced natively by
        ``httpx.Timeout`` on ``self._http_client``, applied to every
        request that client makes, rather than a thread-pool wrapper.
        """
        try:
            return call_with_retry(
                lambda: self._breaker.call(func),
                policy=self._retry_policy,
                non_retryable_exceptions=_NON_RETRYABLE_EXCEPTIONS,
            )
        except (RetryExhaustedError, CircuitOpenError) as exc:
            raise GitHubUnavailableError(
                f"GitHub API temporarily unavailable (retries exhausted or circuit breaker open): {exc}"
            ) from exc

    def _installation_headers(self, owner: str, repo: str) -> dict[str, str]:
        """Resolve (and authorize) an installation token for ``owner/repo``, as request headers.

        Every step here is cheap on the common path: ``mint_app_jwt`` is
        pure local RSA signing (no network); ``RepositoryAuthorizer.
        authorize`` and ``InstallationTokenCache.get_token`` are both
        internally cached (per-repo installation id indefinitely, per-
        installation token until ~5 minutes before its real GitHub-
        reported expiry) so only the FIRST call for a given repo, and then
        roughly once an hour thereafter, actually reaches the network --
        see ``backend.integrations.github_auth``/``backend.security.rbac``
        for the caching mechanics this method builds on.
        """
        app_jwt = mint_app_jwt(app_id=self._app_id, private_key_pem=self._private_key_pem)
        installation_id = self._call_reliably(
            lambda: self._authorizer.authorize(owner=owner, repo=repo, app_jwt=app_jwt)
        )
        token = self._call_reliably(lambda: self._token_cache.get_token(installation_id))
        return {
            "Authorization": f"Bearer {token}",
            "Accept": _ACCEPT_JSON,
            "X-GitHub-Api-Version": _API_VERSION_HEADER,
        }

    def get_pr_metadata(self, *, owner: str, repo: str, pr_number: int) -> PullRequestMetadata:
        """``GET /repos/{owner}/{repo}/pulls/{pr_number}`` -- the PR's current metadata."""
        headers = self._installation_headers(owner, repo)

        def do() -> httpx.Response:
            response = self._http_client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}", headers=headers)
            _raise_for_status(response, context=f"GET pulls/{pr_number}")
            return response

        response = self._call_reliably(do)
        return PullRequestMetadata.model_validate(response.json())

    def _get_changed_files(self, *, owner: str, repo: str, pr_number: int, headers: dict[str, str]) -> list[ChangedFile]:
        """``GET /repos/{owner}/{repo}/pulls/{pr_number}/files``, fully paginated."""
        files: list[ChangedFile] = []
        page = 1
        while True:

            def do(page: int = page) -> httpx.Response:
                response = self._http_client.get(
                    f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                    headers=headers,
                    params={"per_page": _FILES_PAGE_SIZE, "page": page},
                )
                _raise_for_status(response, context=f"GET pulls/{pr_number}/files page={page}")
                return response

            response = self._call_reliably(do)
            batch = response.json()
            if not batch:
                break
            files.extend(ChangedFile.model_validate(entry) for entry in batch)
            if len(batch) < _FILES_PAGE_SIZE:
                break
            page += 1
        return files

    def _get_diff_and_files(
        self, *, owner: str, repo: str, pr_number: int, head_sha: str
    ) -> tuple[str, list[ChangedFile]]:
        cache_key = (owner, repo, pr_number, head_sha)
        cached = self._diff_cache.get(cache_key)
        if cached is not None:
            return cached

        headers = self._installation_headers(owner, repo)
        diff_headers = {**headers, "Accept": _ACCEPT_DIFF}

        def fetch_diff_call() -> httpx.Response:
            response = self._http_client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}", headers=diff_headers)
            _raise_for_status(response, context=f"GET diff for pulls/{pr_number}")
            return response

        diff_response = self._call_reliably(fetch_diff_call)
        diff_text = diff_response.text
        changed_files = self._get_changed_files(owner=owner, repo=repo, pr_number=pr_number, headers=headers)

        result = (diff_text, changed_files)
        self._diff_cache[cache_key] = result
        return result

    def fetch_diff(
        self,
        *,
        repository_owner: str,
        repository_name: str,
        pr_number: int,
        head_sha: str,
    ) -> str:
        """Real implementation: ``GET`` the PR's unified diff via ``Accept: application/vnd.github.diff``."""
        diff_text, _changed_files = self._get_diff_and_files(
            owner=repository_owner, repo=repository_name, pr_number=pr_number, head_sha=head_sha
        )
        return diff_text

    def _find_existing_review_id_marker(self, *, owner: str, repo: str, pr_number: int, headers: dict[str, str], review_id: str) -> bool:
        """Idempotency check: has ``review_id`` already been posted to this PR? See module docstring."""
        marker = f"{_IDEMPOTENCY_MARKER_PREFIX}{review_id}{_IDEMPOTENCY_MARKER_SUFFIX}"

        def do() -> httpx.Response:
            response = self._http_client.get(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                headers=headers,
                params={"per_page": _FILES_PAGE_SIZE},
            )
            _raise_for_status(response, context=f"GET pulls/{pr_number}/reviews")
            return response

        response = self._call_reliably(do)
        existing = [ExistingReview.model_validate(entry) for entry in response.json()]
        return any(marker in review.body for review in existing)

    def post_review_comment(self, review: Review) -> None:
        """Post ``review`` as a real GitHub PR review.

        1. Fetch (or reuse the cached) diff + changed files for this exact
           ``head_sha``.
        2. Build a per-PR ``DiffIndex`` (``backend.integrations.
           diff_mapping``) and independently map every finding to a real
           inline-comment anchor or ``None``.
        3. Every mappable finding becomes one entry in the review's
           ``comments`` array; every unmappable finding is rendered into
           the review's summary ``body`` instead -- see
           ``_format_summary_body``. This is what makes ONE bad line
           number never take down the whole review (see module docstring's
           "DIFF-POSITION MAPPING + DEGRADATION" section) -- the exact
           failure mode the reference implementation gave up in front of.
        4. Idempotency: if a review carrying this ``review.review_id``'s
           marker is already on the PR, skip posting entirely (logs and
           returns) -- closes the ARQ-retry double-post risk flagged in
           ``.genesis/checkpoints/CURRENT.md``'s M10 Deferred list.
        5. ``event="COMMENT"`` always -- this system reports findings, it
           never approves or requests changes (that remains a human
           decision).

        A 422 (GitHub rejects the request body -- e.g. ``commit_id`` no
        longer matches the PR's current head because it moved after this
        diff was fetched) propagates as ``GitHubValidationError``,
        unmodified and uncaught by this method -- per this milestone's
        explicit "surfaced clearly, not swallowed" requirement.
        """
        owner, repo, pr_number, head_sha = (
            review.repository_owner,
            review.repository_name,
            review.pr_number,
            review.head_sha,
        )
        headers = self._installation_headers(owner, repo)

        if self._find_existing_review_id_marker(
            owner=owner, repo=repo, pr_number=pr_number, headers=headers, review_id=review.review_id
        ):
            logger.info(
                "review_id=%s already posted to %s/%s#%d -- skipping duplicate post",
                review.review_id,
                owner,
                repo,
                pr_number,
            )
            return

        _diff_text, changed_files = self._get_diff_and_files(
            owner=owner, repo=repo, pr_number=pr_number, head_sha=head_sha
        )
        diff_index = build_diff_index(changed_files)

        mappable_comments: list[ReviewCommentInput] = []
        degraded_findings: list[Finding] = []
        for finding in review.findings:
            anchor = map_finding_to_anchor(
                file_path=finding.file_path, line_start=finding.line_start, diff_index=diff_index
            )
            if anchor is None:
                degraded_findings.append(finding)
            else:
                mappable_comments.append(
                    ReviewCommentInput(
                        path=anchor.path,
                        line=anchor.line,
                        side=anchor.side,
                        body=_format_finding_body(finding),
                    )
                )

        body = _format_summary_body(review, degraded_findings, mapped_count=len(mappable_comments))
        request_body = CreateReviewRequest(
            commit_id=head_sha, body=body, event="COMMENT", comments=mappable_comments
        )

        def do() -> httpx.Response:
            response = self._http_client.post(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                headers=headers,
                json=request_body.model_dump(),
            )
            _raise_for_status(response, context=f"POST pulls/{pr_number}/reviews")
            return response

        response = self._call_reliably(do)
        posted = CreateReviewResponse.model_validate(response.json())
        logger.info(
            "posted review_id=%s to %s/%s#%d: %d inline, %d degraded to summary -- %s",
            review.review_id,
            owner,
            repo,
            pr_number,
            len(mappable_comments),
            len(degraded_findings),
            posted.html_url,
        )

    def queue_for_hitl(self, review: Review) -> None:
        """No-op (log only) for the real client -- see module docstring's item 5."""
        logger.info(
            "review_id=%s for %s/%s#%d routed to QUEUED_FOR_HITL -- not posted to GitHub "
            "(no durable HITL queue backing this yet; see M13)",
            review.review_id,
            review.repository_owner,
            review.repository_name,
            review.pr_number,
        )


def build_github_client(settings: Settings | None = None) -> GitHubClient:
    """Settings-driven factory: ``MockGitHubClient`` (default) or ``RealGitHubClient``.

    Mirrors ``backend.memory.embedder.get_embedder``'s and
    ``backend.job_queue``'s own "read one Settings field, construct the
    matching implementation" pattern. Defaults to the mock so a keyless
    checkout of this repo -- and every test that does not explicitly ask
    for the real backend -- never attempts a real network call.
    """
    resolved_settings = settings if settings is not None else get_settings()
    if resolved_settings.github_client_backend == "real":
        return RealGitHubClient(resolved_settings)
    return MockGitHubClient()
