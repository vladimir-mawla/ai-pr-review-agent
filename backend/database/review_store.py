"""Persistence for ``Review`` objects: the durable HITL queue + dashboard read model.

Owns: all SQL against the ``reviews`` table (migration
``0002_reviews.sql``). Unlike ``agent_events``
(``backend.database.repository.EventRepository``), this table is
intentionally mutable -- it is a materialized "current state of every
review", not an audit log; the append-only invariant stays scoped to
``agent_events`` alone.

WHY THIS EXISTS (an M13 addition outside M13's literal freeze boundary,
disclosed in the milestone's build report): ``backend.hitl.queue.
InMemoryHitlQueue`` (M5) was explicitly documented as a process-local
stand-in, deferring "a real persistent queue (Postgres-backed, surfaced
through a dashboard)" as future work. M13's dashboard needs exactly that --
real findings/severities/confidence/routing-reason for reviews awaiting
human approval -- and ``agent_events`` alone cannot answer it: the
aggregator's ``decision`` event records only ``outcome``/``confidence``
(``backend.observability.events.emit_decision``), never the findings that
produced them or the human-readable reason string. Rather than fabricate
that data or bolt free-text/JSON columns onto the append-only audit table,
this module adds the narrowly-scoped durable store M5 already named as the
right future home for it.

Per ADR-002's inward-only rule, this module only depends on
``backend.core`` and ``backend.models`` (plus the stdlib/psycopg) --
exactly the same layering ``backend.database.repository`` already follows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from math import ceil

import psycopg
from psycopg.types.json import Jsonb

from backend.core.settings import get_settings
from backend.models import Finding, Review, ReviewStatus

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
    INSERT INTO reviews
        (review_id, pr_number, repository_owner, repository_name, head_sha,
         status, overall_confidence, reason, findings, created_at, posted_at)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (review_id) DO UPDATE SET
        status = EXCLUDED.status,
        overall_confidence = EXCLUDED.overall_confidence,
        reason = EXCLUDED.reason,
        findings = EXCLUDED.findings,
        posted_at = EXCLUDED.posted_at,
        updated_at = now()
"""

_SELECT_COLUMNS = """
    review_id, pr_number, repository_owner, repository_name, head_sha,
    status, overall_confidence, reason, findings, created_at, posted_at
"""

_SELECT_BY_STATUS_SQL = f"""
    SELECT {_SELECT_COLUMNS} FROM reviews WHERE status = %s ORDER BY created_at ASC
"""

_SELECT_ONE_SQL = f"""
    SELECT {_SELECT_COLUMNS} FROM reviews WHERE review_id = %s
"""

_SELECT_RECENT_SQL = f"""
    SELECT {_SELECT_COLUMNS} FROM reviews ORDER BY created_at DESC LIMIT %s
"""


@dataclass(frozen=True)
class PersistedReview:
    """One ``reviews`` row, typed. Everything the dashboard's HITL/trace views need."""

    review_id: str
    pr_number: int
    repository_owner: str
    repository_name: str
    head_sha: str
    status: ReviewStatus
    overall_confidence: Decimal
    reason: str
    findings: list[Finding]
    created_at: datetime
    posted_at: datetime | None


def _row_to_persisted_review(row: tuple[object, ...]) -> PersistedReview:
    (
        review_id,
        pr_number,
        repository_owner,
        repository_name,
        head_sha,
        status,
        overall_confidence,
        reason,
        findings_json,
        created_at,
        posted_at,
    ) = row
    assert isinstance(review_id, str)
    assert isinstance(pr_number, int)
    assert isinstance(repository_owner, str)
    assert isinstance(repository_name, str)
    assert isinstance(head_sha, str)
    assert isinstance(status, str)
    assert isinstance(overall_confidence, Decimal)
    assert isinstance(reason, str)
    assert isinstance(findings_json, list)
    assert isinstance(created_at, datetime)
    return PersistedReview(
        review_id=review_id,
        pr_number=pr_number,
        repository_owner=repository_owner,
        repository_name=repository_name,
        head_sha=head_sha,
        status=ReviewStatus(status),
        overall_confidence=overall_confidence,
        reason=reason,
        findings=[Finding(**item) for item in findings_json],
        created_at=created_at,
        posted_at=posted_at if isinstance(posted_at, datetime) else None,
    )


class ReviewRepository:
    """Thin wrapper around one Postgres connection string, upsert + read only.

    Each call opens and closes its own short-lived connection, mirroring
    ``EventRepository``'s pattern -- this is a low-QPS write path (one
    upsert per completed review, not per request) with no need for a
    long-lived pool.
    """

    def __init__(
        self, dsn: str, *, connect_timeout_seconds: float = 2.0, search_path: str | None = None
    ) -> None:
        self._dsn = dsn
        self._connect_timeout_seconds = max(1, ceil(connect_timeout_seconds))
        # Mirrors EventRepository's own `search_path` parameter exactly --
        # `None` (every production call site) leaves libpq's default
        # search_path untouched; tests pass a per-test-run schema name to
        # redirect every unqualified `reviews` reference in this file's SQL
        # into a disposable, isolated table instead of the shared
        # production one (see tests/integration/test_dashboard_api.py).
        self._connect_options = f"-c search_path={search_path}" if search_path is not None else ""

    def upsert_review(self, review: Review, *, reason: str) -> None:
        """Write (or overwrite) ``review``'s current state.

        Deliberately does not catch anything -- mirrors
        ``EventRepository.insert_event``'s "fail loud, let the caller
        decide" contract. The one production call site
        (``backend.orchestrator.nodes.aggregate_node``) wraps this the same
        way it already wraps ``emit_decision``'s failure modes: logged and
        swallowed, never allowed to fail the review itself.
        """
        findings_payload = [finding.model_dump(mode="json") for finding in review.findings]
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
        ) as conn:
            conn.execute(
                _UPSERT_SQL,
                (
                    review.review_id,
                    review.pr_number,
                    review.repository_owner,
                    review.repository_name,
                    review.head_sha,
                    review.status.value,
                    review.overall_confidence,
                    reason,
                    Jsonb(findings_payload),
                    review.created_at,
                    review.posted_at,
                ),
            )

    def list_pending_hitl(self) -> list[PersistedReview]:
        """Every review currently ``QUEUED_FOR_HITL``, oldest first.

        The dashboard's HITL queue view's whole query -- deliberately
        excludes ``POSTED``/``REJECTED``/``ERROR`` reviews, since those are
        no longer awaiting a human decision.
        """
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
        ) as conn:
            rows = conn.execute(_SELECT_BY_STATUS_SQL, (ReviewStatus.QUEUED_FOR_HITL.value,)).fetchall()
        return [_row_to_persisted_review(row) for row in rows]

    def get_review(self, review_id: str) -> PersistedReview | None:
        """One review's persisted state, or ``None`` if never written."""
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
        ) as conn:
            row = conn.execute(_SELECT_ONE_SQL, (review_id,)).fetchone()
        if row is None:
            return None
        return _row_to_persisted_review(row)

    def list_recent(self, limit: int = 50) -> list[PersistedReview]:
        """The most recently created reviews, newest first -- used by the trace view's picker."""
        with psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout_seconds,
            autocommit=True,
            options=self._connect_options,
        ) as conn:
            rows = conn.execute(_SELECT_RECENT_SQL, (limit,)).fetchall()
        return [_row_to_persisted_review(row) for row in rows]


def persist_review(repository: ReviewRepository, review: Review, *, reason: str) -> None:
    """``upsert_review``, with the same log-and-continue failure policy ``backend.observability.events`` uses.

    The one production call site (``backend.orchestrator.nodes.
    aggregate_node``) must never let a Postgres hiccup fail a review that
    otherwise completed successfully -- the dashboard read model is a
    convenience for operators, not a correctness dependency of the review
    pipeline itself. Catches exactly ``psycopg.Error``/``OSError``
    (database unavailable/rejected), mirroring
    ``backend.observability.events._emit``'s policy and rationale.
    """
    try:
        repository.upsert_review(review, reason=reason)
    except (psycopg.Error, OSError) as exc:
        logger.warning(
            "failed to persist review row for review_id=%r: %s -- continuing without it",
            review.review_id,
            exc,
        )


@lru_cache
def get_review_repository() -> ReviewRepository:
    """Process-wide ``ReviewRepository``, built once from ``Settings``.

    Mirrors ``backend.observability.workflow_context.get_event_repository``
    exactly -- same connection string (``Settings.database_url``, the
    ``reviews`` table lives in the same Postgres database as
    ``agent_events``), same "build once, share across call sites" reasoning.
    """
    settings = get_settings()
    return ReviewRepository(settings.database_url)
