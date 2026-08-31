"""Typed shapes for the GitHub REST API surface this project depends on.

Owns: the Pydantic models ``backend.integrations.github_client.RealGitHubClient``
parses real API responses into, and that
``tests/contract/test_github_client_contract.py`` uses to pin the real API's
observed shape (catching mock-drift -- see that test's own docstring).

WHY A SEPARATE MODULE FROM ``github_client.py``: PLAN.md's own M11
freeze-boundary text names both files explicitly
("``backend/integrations/{github_client,github_models}.py`` (real
implementation)"), and keeping the wire-shape models separate from the
client logic that uses them is what lets the contract test import just the
models (to validate a captured JSON fixture against them) without pulling
in the whole client (auth, retry/breaker/timeout, diff mapping).

Deliberately narrow: each model extracts only the fields this project
actually reads, mirroring ``backend.models.webhook.WebhookEvent``'s own
"GitHub payloads are large and deeply nested; we model only what we depend
on" precedent. ``model_config = {"extra": "ignore"}`` on every model here
for the same reason: GitHub's real responses carry dozens of fields we
never touch, and pinning to only the ones we need means an unrelated field
GitHub adds later can never break parsing.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class InstallationTokenResponse(BaseModel):
    """Response shape of ``POST /app/installations/{id}/access_tokens``.

    Attributes:
        token: The installation access token itself (``ghs_...``). Never
            logged -- see ``backend.integrations.github_auth``'s module
            docstring for the "never log the private key or a minted
            token" policy.
        expires_at: ISO 8601 timestamp; GitHub installation tokens are
            valid for exactly one hour from issuance.
    """

    token: str = Field(min_length=1)
    expires_at: str

    model_config = {"extra": "ignore"}


class InstallationResponse(BaseModel):
    """Response shape of ``GET /repos/{owner}/{repo}/installation``.

    Used by ``backend.security.rbac`` to both discover the numeric
    installation id for a given repository (so it is never hardcoded, per
    this milestone's explicit instruction) and, structurally, as the
    authorization check itself: a 404 here means this App is not installed
    on the repository at all (see ``RepositoryAuthorizer``).
    """

    id: int
    repository_selection: str | None = None

    model_config = {"extra": "ignore"}


class _CommitRef(BaseModel):
    sha: str = Field(min_length=1)
    ref: str = Field(min_length=1)

    model_config = {"extra": "ignore"}


class PullRequestMetadata(BaseModel):
    """Response shape of ``GET /repos/{owner}/{repo}/pulls/{pull_number}``.

    Only the fields this project actually reads: the head commit's SHA
    (must match the ``Review.head_sha`` a caller is posting against -- a
    mismatch means the PR moved since the diff was fetched) and the PR's
    open/closed state.
    """

    number: int
    state: str
    head: _CommitRef
    base: _CommitRef

    model_config = {"extra": "ignore"}


class ChangedFile(BaseModel):
    """One entry from ``GET /repos/{owner}/{repo}/pulls/{pull_number}/files`` (paginated).

    Attributes:
        filename: Path relative to the repository root -- matches
            ``Finding.file_path``'s own convention.
        status: ``"added"``, ``"modified"``, ``"removed"``, ``"renamed"``, etc.
        patch: The unified-diff hunk text for this file, or ``None`` for a
            file GitHub does not generate a text patch for (e.g. a binary
            file, or a diff too large to render) -- ``backend.integrations.
            diff_mapping`` treats a missing patch as "nothing in this file
            is commentable", not an error.
    """

    filename: str = Field(min_length=1)
    status: str
    patch: str | None = None
    additions: int = 0
    deletions: int = 0

    model_config = {"extra": "ignore"}


class ReviewCommentInput(BaseModel):
    """One inline comment in the ``comments`` array of a POST review request body.

    ``line`` + ``side`` is this project's chosen anchoring scheme -- see
    ``backend.integrations.diff_mapping``'s module docstring for why this
    was chosen over the legacy ``position`` (diff-hunk-relative offset)
    scheme.
    """

    path: str = Field(min_length=1)
    line: int = Field(gt=0)
    side: str = Field(pattern="^(LEFT|RIGHT)$")
    body: str = Field(min_length=1)


class CreateReviewRequest(BaseModel):
    """Request body for ``POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews``.

    ``event`` is always ``"COMMENT"`` in this project -- see
    ``backend.integrations.github_client``'s module docstring for why this
    system never submits ``APPROVE``/``REQUEST_CHANGES`` (it is not making
    a merge-gating decision, only reporting findings).
    """

    commit_id: str = Field(min_length=40, max_length=40)
    body: str
    event: str = "COMMENT"
    comments: list[ReviewCommentInput] = Field(default_factory=list)


class CreateReviewResponse(BaseModel):
    """Response shape of a successful ``POST .../reviews`` call.

    Attributes:
        id: The review's own id on GitHub -- not currently persisted
            anywhere in this project, but part of the real API's observed
            shape the contract test pins.
        html_url: Deep link to the posted review, useful for a human
            operator/log line.
        state: GitHub's own echo of what was submitted (e.g.
            ``"COMMENTED"``).
    """

    id: int
    html_url: str
    state: str

    model_config = {"extra": "ignore"}


class ExistingReview(BaseModel):
    """One entry from ``GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews``.

    Used only for the idempotency check in ``RealGitHubClient.
    post_review_comment`` -- see that method's docstring for why a
    duplicate post must be detected here rather than relying solely on
    ARQ's job-level retry never firing (a gap
    ``.genesis/checkpoints/CURRENT.md`` explicitly flagged after M10).
    """

    id: int
    body: str

    model_config = {"extra": "ignore"}
