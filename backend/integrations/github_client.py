"""GitHub client interface, mock-backed for M10 -- the real REST wrapper is M11's job.

Owns the abstraction PLAN.md's M10 freeze boundary names explicitly
("``backend/integrations/github_client.py`` (mock-backed interface)"): the
shape ``backend.cli.review_local`` and ``backend.job_queue.arq_worker``
program against so a completed ``Review`` (and, before that, the diff to
review) is never fetched/posted through a hardcoded, un-swappable call --
exactly the same "code against the abstract interface, swap the concrete
implementation later" discipline ``backend.job_queue.interface.JobQueue``
already established for the queue at M2/M3, and
``backend.core.workflow_engine.WorkflowEngine`` established for the
orchestrator at M4.

WHY MOCKED, NOT REAL, AT M10: PLAN.md's M10 outcome is explicit --
"producing one structured review JSON on disk -- with GitHub posting
mocked out -- proving the whole cognitive pipeline end-to-end before
touching a real repository." M11 ("Real GitHub Integration") is where
``RealGitHubClient`` (a real REST/GraphQL wrapper, real GitHub App auth)
replaces ``MockGitHubClient`` behind this SAME ``GitHubClient`` protocol --
PLAN.md's own M11 freeze-boundary text says as much ("the mocked
``github_client`` from M10 is swapped for the real REST wrapper behind the
same interface"). Nothing in this module makes a real network call.

TWO RESPONSIBILITIES, both mocked here:
1. ``fetch_diff`` -- given a PR's identity, return the diff text to review.
   A real implementation (M11) would call the GitHub REST/GraphQL API for
   the PR's actual patch; ``MockGitHubClient`` returns a pre-configured
   diff (defaulting to a fixed fixture) instead, since M10 never talks to
   a real repository.
2. ``post_review_comment`` / ``queue_for_hitl`` -- given a completed
   ``Review``, either post it as a real PR comment (POSTED) or hand it to
   the human-approval queue (QUEUED_FOR_HITL). ``MockGitHubClient`` simply
   RECORDS each call instead of doing either -- exactly what M10's success
   criteria checks: "the mocked GitHub client records exactly one 'post' or
   one 'queue_for_hitl' call, never both." ``post_or_queue`` below is the
   one, single dispatcher every caller (the CLI, the ARQ worker) uses to
   guarantee that "never both" property structurally, rather than each
   caller re-implementing its own if/else and risking calling both by
   mistake.
"""

from __future__ import annotations

from typing import Protocol

from backend.models import Review, ReviewStatus

# The default diff MockGitHubClient.fetch_diff returns when no diff was
# pre-configured for a given PR number -- an empty diff is a safe,
# harmless default (every specialist analyzing "" simply has nothing to
# say), never a real repository's content.
_DEFAULT_MOCK_DIFF = ""


class GitHubClient(Protocol):
    """The shape a caller (CLI, ARQ worker) needs from a GitHub client.

    A ``Protocol`` (structural typing), not an ABC -- so a test can hand a
    caller a bare fake object satisfying just these methods, mirroring
    ``backend.tools.llm_client.LLMClientProtocol``'s and
    ``backend.agents.base_agent.RetrieverProtocol``'s existing pattern in
    this codebase.
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
    """Mock-backed ``GitHubClient``: never touches a real repository.

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
    both" (this function calls exactly one of the two methods, always),
    which is PLAN.md's own M10 success criterion. ``ReviewStatus.POSTED``
    posts; every other status (``QUEUED_FOR_HITL``, and the conservative
    default for ``REJECTED``/``ERROR``, neither of which
    ``backend.orchestrator.nodes.aggregate_node`` actually produces today)
    queues for human review rather than risking an unreviewed auto-post.
    """
    if review.status == ReviewStatus.POSTED:
        client.post_review_comment(review)
    else:
        client.queue_for_hitl(review)
