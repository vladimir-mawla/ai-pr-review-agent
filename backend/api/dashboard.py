"""The dashboard's JSON API: HITL queue, per-agent cost/latency, and trace reconstruction.

Owns: three read-only GET routes backing ``frontend/``'s three views (plus
a fourth, ``/api/reviews``, listing recent reviews for the trace view's
picker). A backend JSON API under ``backend/api/`` was chosen over direct
DB reads from Next.js server components for two reasons: (1) it keeps this
project's inward-only architecture intact -- the frontend is a new,
outward-facing consumer, and every existing consumer of
``backend.database``/``backend.observability`` is backend Python code, so
giving the frontend its own direct Postgres credentials would be the first
crack in that boundary; (2) ``backend.database.repository.EventRepository``
and ``backend.database.review_store.ReviewRepository`` already own "the
only code that runs SQL against agent_events/reviews" (their own
docstrings' words) -- a second SQL client embedded in the frontend would
duplicate that knowledge in TypeScript and risk drifting from the Python
schema.

HONESTY, NOT FABRICATION: every route below returns real data or an
honest empty state (``[]``/``null``/a zero-count summary) -- never
placeholder numbers. An unreachable database surfaces as a real HTTP error
(502), not a silently-empty response that could be mistaken for "there is
genuinely nothing to show yet".
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from backend.database.repository import AgentMetrics, EventRepository
from backend.database.review_store import PersistedReview, ReviewRepository
from backend.models import Finding

router = APIRouter(prefix="/api")


class FindingOut(BaseModel):
    agent_type: str
    severity: str
    category: str
    file_path: str
    line_start: int
    line_end: int
    confidence: Decimal
    rationale: str

    @classmethod
    def from_finding(cls, finding: Finding) -> FindingOut:
        return cls(
            agent_type=finding.agent_type.value,
            severity=finding.severity.value,
            category=finding.category,
            file_path=finding.file_path,
            line_start=finding.line_start,
            line_end=finding.line_end,
            confidence=finding.confidence,
            rationale=finding.rationale,
        )


class ReviewOut(BaseModel):
    review_id: str
    pr_number: int
    repository_owner: str
    repository_name: str
    head_sha: str
    status: str
    overall_confidence: Decimal
    reason: str
    findings: list[FindingOut]
    created_at: datetime
    posted_at: datetime | None

    @classmethod
    def from_persisted(cls, review: PersistedReview) -> ReviewOut:
        return cls(
            review_id=review.review_id,
            pr_number=review.pr_number,
            repository_owner=review.repository_owner,
            repository_name=review.repository_name,
            head_sha=review.head_sha,
            status=review.status.value,
            overall_confidence=review.overall_confidence,
            reason=review.reason,
            findings=[FindingOut.from_finding(f) for f in review.findings],
            created_at=review.created_at,
            posted_at=review.posted_at,
        )


class HitlQueueResponse(BaseModel):
    reviews: list[ReviewOut]
    count: int


class AgentMetricRow(BaseModel):
    agent: str
    model: str
    call_count: int
    total_cost_usd: Decimal
    avg_latency_ms: int
    total_tokens_in: int
    total_tokens_out: int

    @classmethod
    def from_metrics(cls, metrics: AgentMetrics) -> AgentMetricRow:
        return cls(
            agent=metrics.agent,
            model=metrics.model,
            call_count=metrics.call_count,
            total_cost_usd=metrics.total_cost_usd,
            avg_latency_ms=metrics.avg_latency_ms,
            total_tokens_in=metrics.total_tokens_in,
            total_tokens_out=metrics.total_tokens_out,
        )


class AgentMetricsResponse(BaseModel):
    metrics: list[AgentMetricRow]
    total_cost_usd: Decimal
    is_empty: bool


class TraceEventOut(BaseModel):
    id: int | None
    event_type: str
    ts: datetime
    agent: str | None
    model: str | None
    tokens_in: int | None
    tokens_out: int | None
    cost_usd: Decimal | None
    latency_ms: int | None
    outcome: str | None
    confidence: Decimal | None


class TraceResponse(BaseModel):
    review_id: str
    events: list[TraceEventOut]
    review: ReviewOut | None


class RecentReviewsResponse(BaseModel):
    reviews: list[ReviewOut]


def _event_repository(request: Request) -> EventRepository:
    repository = request.app.state.event_repository
    assert isinstance(repository, EventRepository)
    return repository


def _review_repository(request: Request) -> ReviewRepository:
    repository = request.app.state.review_repository
    assert isinstance(repository, ReviewRepository)
    return repository


def _db_unavailable(exc: Exception) -> HTTPException:
    # 502: the dashboard API itself is healthy, but the database it depends
    # on to answer this request is not -- distinct from a 500 (a bug in
    # this route) or a 404 (the resource genuinely does not exist).
    return HTTPException(status_code=502, detail=f"database unavailable: {exc}")


@router.get("/hitl-queue", response_model=HitlQueueResponse)
def get_hitl_queue(request: Request) -> HitlQueueResponse:
    """Every review currently awaiting human approval, with its findings/severities/confidence/reason.

    An empty ``reviews`` list is a real, honest answer (no reviews are
    currently queued for HITL) -- not distinguished from any other empty
    state in the response shape, since there is nothing more to say about
    it than "count: 0".
    """
    repository = _review_repository(request)
    try:
        pending = repository.list_pending_hitl()
    except (psycopg.Error, OSError) as exc:
        raise _db_unavailable(exc) from exc
    reviews = [ReviewOut.from_persisted(r) for r in pending]
    return HitlQueueResponse(reviews=reviews, count=len(reviews))


@router.get("/agent-metrics", response_model=AgentMetricsResponse)
def get_agent_metrics(request: Request) -> AgentMetricsResponse:
    """Per-(agent, model) cost/latency, aggregated from ``agent_events``' ``llm.call`` rows.

    See ``backend.database.repository.EventRepository.
    aggregate_llm_calls_by_agent``'s docstring for the M12
    continuous-aggregate adaptation this query stands in for.
    ``is_empty=True`` (with ``metrics: []`` and ``total_cost_usd: 0``) is
    the honest answer when no ``llm.call`` event has ever been recorded --
    the dashboard renders that state explicitly rather than a bare zero
    that could be mistaken for "spend is genuinely zero on an active
    system".
    """
    repository = _event_repository(request)
    try:
        metrics = repository.aggregate_llm_calls_by_agent()
    except (psycopg.Error, OSError) as exc:
        raise _db_unavailable(exc) from exc
    total = sum((m.total_cost_usd for m in metrics), start=Decimal("0"))
    return AgentMetricsResponse(
        metrics=[AgentMetricRow.from_metrics(m) for m in metrics],
        total_cost_usd=total,
        is_empty=len(metrics) == 0,
    )


@router.get("/trace/{review_id}", response_model=TraceResponse)
def get_trace(review_id: str, request: Request) -> TraceResponse:
    """Reconstruct one review end-to-end from ``agent_events``, by ``review_id`` alone.

    Includes the persisted ``Review`` (findings/status/reason) alongside
    the raw event timeline when one was written
    (``backend.database.review_store``); ``review: null`` if this
    ``review_id`` never completed the aggregator (e.g. still in flight, or
    predates M13's persistence). An empty ``events`` list with a
    ``review_id`` that matches no known review is still a 200 with an
    honest empty timeline, not a 404 -- ``review_id`` is caller-supplied
    free text (e.g. typed into the dashboard's search box), and "no events
    recorded for this id" is meaningfully different from "this route does
    not exist".
    """
    event_repository = _event_repository(request)
    review_repository = _review_repository(request)
    try:
        events = event_repository.fetch_events_for_review(review_id)
    except (psycopg.Error, OSError) as exc:
        raise _db_unavailable(exc) from exc
    try:
        persisted_review = review_repository.get_review(review_id)
    except (psycopg.Error, OSError) as exc:
        raise _db_unavailable(exc) from exc

    return TraceResponse(
        review_id=review_id,
        events=[
            TraceEventOut(
                id=e.id,
                event_type=e.event_type.value,
                ts=e.ts,
                agent=e.agent,
                model=e.model,
                tokens_in=e.tokens_in,
                tokens_out=e.tokens_out,
                cost_usd=e.cost_usd,
                latency_ms=e.latency_ms,
                outcome=e.outcome,
                confidence=e.confidence,
            )
            for e in events
        ],
        review=ReviewOut.from_persisted(persisted_review) if persisted_review is not None else None,
    )


@router.get("/reviews", response_model=RecentReviewsResponse)
def get_recent_reviews(
    request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> RecentReviewsResponse:
    """The most recently completed reviews, newest first -- feeds the trace view's picker."""
    repository = _review_repository(request)
    try:
        recent = repository.list_recent(limit)
    except (psycopg.Error, OSError) as exc:
        raise _db_unavailable(exc) from exc
    return RecentReviewsResponse(reviews=[ReviewOut.from_persisted(r) for r in recent])
