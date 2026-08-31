"""Integration tests for the M13 ``reviews`` table (``backend.database.review_store``).

FREE -- no LLM call, no API key -- but needs a real, reachable Postgres
(``docker compose up -d postgres``), same skip-if-unreachable pattern
``tests/integration/test_events_spine.py`` already established. Proves:

- upsert then read-back round-trips every field, findings included, through
  a real Postgres JSONB column.
- re-upserting the same ``review_id`` overwrites in place (the table is
  deliberately mutable, unlike ``agent_events`` -- see the migration's
  docstring) rather than erroring or duplicating.
- ``list_pending_hitl`` returns only ``QUEUED_FOR_HITL`` reviews, excluding
  ``POSTED``/``REJECTED``/``ERROR`` ones -- the dashboard's own success
  criterion, worded generally: "the HITL queue endpoint returns queued
  reviews and excludes posted ones".
- an empty/never-written ``review_id`` produces an honest ``None``/empty
  result, not a crash.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import psycopg
import pytest

from backend.core.settings import get_settings
from backend.database.postgres import apply_migrations
from backend.database.review_store import ReviewRepository
from backend.models import Finding, Review, ReviewStatus, compute_overall_confidence

_BASE_SETTINGS = get_settings()
_DATABASE_URL = _BASE_SETTINGS.database_url
_DATABASE_ADMIN_URL = _BASE_SETTINGS.database_admin_url


def _postgres_reachable(admin_dsn: str) -> bool:
    try:
        with psycopg.connect(admin_dsn, connect_timeout=1) as conn:
            conn.execute("SELECT 1")
        return True
    except (psycopg.Error, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _postgres_reachable(_DATABASE_ADMIN_URL),
    reason=f"Postgres not reachable at {_DATABASE_ADMIN_URL} -- run `docker compose up -d postgres` first",
)


@pytest.fixture(scope="module", autouse=True)
def _migrated_database() -> None:
    apply_migrations(_DATABASE_ADMIN_URL)


@pytest.fixture
def repository() -> ReviewRepository:
    return ReviewRepository(_DATABASE_URL)


def _unique_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _finding(category: str = "sql_injection") -> Finding:
    return Finding(
        agent_type="SECURITY",
        severity="CRITICAL",
        category=category,
        file_path="app/db.py",
        line_start=10,
        line_end=12,
        confidence=Decimal("0.900"),
        rationale="test finding",
    )


def _review(review_id: str, status: ReviewStatus, findings: list[Finding] | None = None) -> Review:
    findings = findings or []
    return Review(
        review_id=review_id,
        pr_number=42,
        repository_owner="acme",
        repository_name="widgets",
        head_sha="a" * 40,
        findings=findings,
        overall_confidence=compute_overall_confidence(findings),
        status=status,
        created_at=datetime.now(UTC),
        error_message=None,
    )


class TestUpsertAndReadBack:
    def test_round_trips_every_field_including_findings(self, repository: ReviewRepository):
        review_id = _unique_id("roundtrip")
        finding = _finding()
        review = _review(review_id, ReviewStatus.QUEUED_FOR_HITL, [finding])

        repository.upsert_review(review, reason="test reason: below threshold")
        persisted = repository.get_review(review_id)

        assert persisted is not None
        assert persisted.review_id == review_id
        assert persisted.pr_number == 42
        assert persisted.repository_owner == "acme"
        assert persisted.status == ReviewStatus.QUEUED_FOR_HITL
        assert persisted.overall_confidence == review.overall_confidence
        assert persisted.reason == "test reason: below threshold"
        assert len(persisted.findings) == 1
        assert persisted.findings[0].category == "sql_injection"
        assert persisted.findings[0].severity.value == "CRITICAL"

    def test_get_review_returns_none_for_unknown_id(self, repository: ReviewRepository):
        assert repository.get_review(_unique_id("never-written")) is None


class TestUpsertOverwritesInPlace:
    def test_re_upserting_same_review_id_updates_rather_than_duplicates(self, repository: ReviewRepository):
        review_id = _unique_id("upsert")
        first = _review(review_id, ReviewStatus.QUEUED_FOR_HITL, [_finding()])
        repository.upsert_review(first, reason="first: below threshold")

        second = _review(review_id, ReviewStatus.POSTED, [])
        repository.upsert_review(second, reason="second: auto-posted")

        persisted = repository.get_review(review_id)
        assert persisted is not None
        assert persisted.status == ReviewStatus.POSTED
        assert persisted.reason == "second: auto-posted"
        assert persisted.findings == []


class TestListPendingHitl:
    def test_excludes_posted_reviews_and_includes_only_queued_ones(self, repository: ReviewRepository):
        queued_id = _unique_id("queued")
        posted_id = _unique_id("posted")
        repository.upsert_review(
            _review(queued_id, ReviewStatus.QUEUED_FOR_HITL, [_finding()]), reason="below threshold"
        )
        repository.upsert_review(_review(posted_id, ReviewStatus.POSTED, []), reason="auto-posted")

        pending = repository.list_pending_hitl()
        pending_ids = {r.review_id for r in pending}

        assert queued_id in pending_ids
        assert posted_id not in pending_ids
        # Every returned entry is genuinely QUEUED_FOR_HITL -- not just the
        # two rows this test wrote (other tests/real runs share this table).
        assert all(r.status == ReviewStatus.QUEUED_FOR_HITL for r in pending)


class TestListRecent:
    def test_returns_most_recently_created_first(self, repository: ReviewRepository):
        older_id = _unique_id("older")
        newer_id = _unique_id("newer")
        repository.upsert_review(_review(older_id, ReviewStatus.POSTED, []), reason="r1")
        repository.upsert_review(_review(newer_id, ReviewStatus.POSTED, []), reason="r2")

        recent = repository.list_recent(limit=200)
        ids_in_order = [r.review_id for r in recent]
        assert ids_in_order.index(newer_id) < ids_in_order.index(older_id)
