"""Unit tests for backend.models domain contracts.

This module tests the core data types (Finding, Review, WebhookEvent, enums)
to ensure they validate correctly, reject invalid values, serialize/deserialize,
and maintain their invariants.

Why comprehensive tests here: The models are the system's contract boundary.
Any schema change breaks consumers (dashboard, API, agents). Tests catch breaking
changes at development time. Tests also serve as executable specifications of
what values are valid.
"""

import json
from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.models import (
    AgentType,
    Finding,
    Review,
    ReviewStatus,
    Severity,
    WebhookEvent,
)


class TestEnums:
    """Test enum validity and string representation."""

    def test_severity_enum_values(self) -> None:
        """Severity enum has all required values."""
        assert Severity.CRITICAL.value == "CRITICAL"
        assert Severity.HIGH.value == "HIGH"
        assert Severity.MEDIUM.value == "MEDIUM"
        assert Severity.LOW.value == "LOW"
        assert Severity.INFO.value == "INFO"

    def test_agent_type_enum_values(self) -> None:
        """AgentType enum has all required values."""
        assert AgentType.SECURITY.value == "SECURITY"
        assert AgentType.QUALITY.value == "QUALITY"
        assert AgentType.TESTS.value == "TESTS"
        assert AgentType.DOCS.value == "DOCS"

    def test_review_status_enum_values(self) -> None:
        """ReviewStatus enum has all required values."""
        assert ReviewStatus.POSTED.value == "POSTED"
        assert ReviewStatus.QUEUED_FOR_HITL.value == "QUEUED_FOR_HITL"
        assert ReviewStatus.REJECTED.value == "REJECTED"
        assert ReviewStatus.ERROR.value == "ERROR"

    def test_enum_json_serialization(self) -> None:
        """Enums serialize and deserialize via JSON."""
        severity = Severity.CRITICAL
        json_str = json.dumps({"severity": severity.value})
        data = json.loads(json_str)
        assert Severity(data["severity"]) == Severity.CRITICAL


class TestFinding:
    """Test Finding contract construction, validation, and serialization."""

    def test_finding_valid_construction(self) -> None:
        """Finding can be constructed with valid fields."""
        finding = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            category="sql_injection",
            file_path="src/db.py",
            line_start=42,
            line_end=42,
            confidence=Decimal("0.950"),
            rationale="User input directly interpolated into SQL query.",
        )
        assert finding.agent_type == AgentType.SECURITY
        assert finding.severity == Severity.CRITICAL
        assert finding.confidence == Decimal("0.950")
        assert finding.file_path == "src/db.py"
        assert finding.line_start == 42
        assert finding.line_end == 42

    def test_finding_confidence_bounds_enforcement(self) -> None:
        """Confidence must be in [0.000, 1.000]."""
        # Valid: exactly 0 and 1
        finding_min = Finding(
            agent_type=AgentType.QUALITY,
            severity=Severity.LOW,
            category="style",
            file_path="a.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.000"),
            rationale="test",
        )
        assert finding_min.confidence == Decimal("0.000")

        finding_max = Finding(
            agent_type=AgentType.QUALITY,
            severity=Severity.LOW,
            category="style",
            file_path="a.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("1.000"),
            rationale="test",
        )
        assert finding_max.confidence == Decimal("1.000")

        # Invalid: confidence > 1.0
        with pytest.raises(ValidationError) as exc_info:
            Finding(
                agent_type=AgentType.QUALITY,
                severity=Severity.LOW,
                category="style",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("1.500"),
                rationale="test",
            )
        assert "less than or equal to 1" in str(exc_info.value)

        # Invalid: confidence < 0.0
        with pytest.raises(ValidationError) as exc_info:
            Finding(
                agent_type=AgentType.QUALITY,
                severity=Severity.LOW,
                category="style",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("-0.100"),
                rationale="test",
            )
        assert "greater than or equal to 0" in str(exc_info.value)

    def test_finding_confidence_decimal_places(self) -> None:
        """Confidence supports 3-decimal precision."""
        finding = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.HIGH,
            category="xss",
            file_path="a.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.123"),
            rationale="test",
        )
        assert finding.confidence == Decimal("0.123")

        # Verify 3 decimal places are preserved in JSON
        finding_json = finding.model_dump_json()
        assert '"confidence":"0.123"' in finding_json

    def test_finding_line_numbers_valid(self) -> None:
        """Line numbers must be positive and line_end >= line_start."""
        # Valid: same line
        finding = Finding(
            agent_type=AgentType.TESTS,
            severity=Severity.MEDIUM,
            category="missing_test",
            file_path="a.py",
            line_start=10,
            line_end=10,
            confidence=Decimal("0.500"),
            rationale="test",
        )
        assert finding.line_start == 10
        assert finding.line_end == 10

        # Valid: range
        finding = Finding(
            agent_type=AgentType.TESTS,
            severity=Severity.MEDIUM,
            category="missing_test",
            file_path="a.py",
            line_start=10,
            line_end=20,
            confidence=Decimal("0.500"),
            rationale="test",
        )
        assert finding.line_start == 10
        assert finding.line_end == 20

        # Invalid: line_start = 0
        with pytest.raises(ValidationError):
            Finding(
                agent_type=AgentType.TESTS,
                severity=Severity.MEDIUM,
                category="missing_test",
                file_path="a.py",
                line_start=0,
                line_end=1,
                confidence=Decimal("0.500"),
                rationale="test",
            )

        # Invalid: line_end < line_start now raises via the model_validator that
        # enforces the invariant the docstring already promised.
        with pytest.raises(ValidationError):
            Finding(
                agent_type=AgentType.TESTS,
                severity=Severity.MEDIUM,
                category="missing_test",
                file_path="a.py",
                line_start=100,
                line_end=1,
                confidence=Decimal("0.500"),
                rationale="test",
            )

    def test_finding_category_validation(self) -> None:
        """Category must be non-empty and <= 100 chars."""
        # Valid: short
        finding = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.LOW,
            category="x",
            file_path="a.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.500"),
            rationale="test",
        )
        assert finding.category == "x"

        # Invalid: empty
        with pytest.raises(ValidationError):
            Finding(
                agent_type=AgentType.SECURITY,
                severity=Severity.LOW,
                category="",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("0.500"),
                rationale="test",
            )

    def test_finding_file_path_validation(self) -> None:
        """file_path must be non-empty and <= 1000 chars."""
        # Valid
        finding = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.LOW,
            category="cat",
            file_path="src/very/deep/path/to/file.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.500"),
            rationale="test",
        )
        assert finding.file_path == "src/very/deep/path/to/file.py"

        # Invalid: empty
        with pytest.raises(ValidationError):
            Finding(
                agent_type=AgentType.SECURITY,
                severity=Severity.LOW,
                category="cat",
                file_path="",
                line_start=1,
                line_end=1,
                confidence=Decimal("0.500"),
                rationale="test",
            )

    def test_finding_rationale_validation(self) -> None:
        """rationale must be non-empty and <= 5000 chars."""
        # Valid
        finding = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.LOW,
            category="cat",
            file_path="a.py",
            line_start=1,
            line_end=1,
            confidence=Decimal("0.500"),
            rationale="x",
        )
        assert finding.rationale == "x"

        # Invalid: empty
        with pytest.raises(ValidationError):
            Finding(
                agent_type=AgentType.SECURITY,
                severity=Severity.LOW,
                category="cat",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("0.500"),
                rationale="",
            )

    def test_finding_sorting(self) -> None:
        """Findings sort by severity (desc) then confidence (desc)."""
        findings = [
            Finding(
                agent_type=AgentType.SECURITY,
                severity=Severity.LOW,
                category="x",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("0.900"),
                rationale="low confidence",
            ),
            Finding(
                agent_type=AgentType.SECURITY,
                severity=Severity.CRITICAL,
                category="x",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("0.500"),
                rationale="critical confidence",
            ),
            Finding(
                agent_type=AgentType.SECURITY,
                severity=Severity.MEDIUM,
                category="x",
                file_path="a.py",
                line_start=1,
                line_end=1,
                confidence=Decimal("0.700"),
                rationale="medium confidence",
            ),
        ]
        sorted_findings = sorted(findings)
        assert sorted_findings[0].severity == Severity.CRITICAL
        assert sorted_findings[1].severity == Severity.MEDIUM
        assert sorted_findings[2].severity == Severity.LOW

    def test_finding_json_round_trip(self) -> None:
        """Finding serializes and deserializes via JSON."""
        original = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            category="sql_injection",
            file_path="src/db.py",
            line_start=42,
            line_end=45,
            confidence=Decimal("0.950"),
            rationale="User input interpolated directly into SQL.",
        )
        json_data = json.loads(original.model_dump_json())
        reconstructed = Finding(**json_data)
        assert reconstructed == original
        assert reconstructed.confidence == Decimal("0.950")


class TestReview:
    """Test Review contract construction, validation, and serialization."""

    def test_review_valid_construction(self) -> None:
        """Review can be constructed with valid fields."""
        now = datetime.now()
        review = Review(
            review_id="ghpr-12345-run-001",
            pr_number=12345,
            repository_owner="myorg",
            repository_name="myapp",
            head_sha="a" * 40,
            findings=[],
            overall_confidence=Decimal("0.750"),
            status=ReviewStatus.POSTED,
            created_at=now,
            posted_at=now,
            error_message=None,
        )
        assert review.review_id == "ghpr-12345-run-001"
        assert review.pr_number == 12345
        assert review.overall_confidence == Decimal("0.750")
        assert review.status == ReviewStatus.POSTED

    def test_review_with_findings(self) -> None:
        """Review can aggregate multiple findings."""
        finding1 = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            category="sql_injection",
            file_path="src/db.py",
            line_start=42,
            line_end=42,
            confidence=Decimal("0.950"),
            rationale="test",
        )
        finding2 = Finding(
            agent_type=AgentType.TESTS,
            severity=Severity.MEDIUM,
            category="missing_test",
            file_path="src/auth.py",
            line_start=10,
            line_end=20,
            confidence=Decimal("0.500"),
            rationale="test",
        )
        now = datetime.now()
        review = Review(
            review_id="ghpr-999-run-001",
            pr_number=999,
            repository_owner="myorg",
            repository_name="myapp",
            head_sha="b" * 40,
            findings=[finding1, finding2],
            overall_confidence=Decimal("0.725"),  # (0.95 + 0.50) / 2
            status=ReviewStatus.QUEUED_FOR_HITL,
            created_at=now,
        )
        assert len(review.findings) == 2
        assert review.findings[0].severity == Severity.CRITICAL

    def test_review_head_sha_validation(self) -> None:
        """head_sha must be exactly 40 hex characters."""
        now = datetime.now()

        # Valid: exactly 40 hex chars
        review = Review(
            review_id="test",
            pr_number=1,
            repository_owner="org",
            repository_name="repo",
            head_sha="a" * 40,
            findings=[],
            overall_confidence=Decimal("0.500"),
            status=ReviewStatus.POSTED,
            created_at=now,
        )
        assert len(review.head_sha) == 40

        # Invalid: too short
        with pytest.raises(ValidationError):
            Review(
                review_id="test",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 39,
                findings=[],
                overall_confidence=Decimal("0.500"),
                status=ReviewStatus.POSTED,
                created_at=now,
            )

        # Invalid: too long
        with pytest.raises(ValidationError):
            Review(
                review_id="test",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 41,
                findings=[],
                overall_confidence=Decimal("0.500"),
                status=ReviewStatus.POSTED,
                created_at=now,
            )

    def test_review_confidence_bounds(self) -> None:
        """Review.overall_confidence must be in [0.000, 1.000]."""
        now = datetime.now()

        # Valid: 0
        review = Review(
            review_id="test",
            pr_number=1,
            repository_owner="org",
            repository_name="repo",
            head_sha="a" * 40,
            findings=[],
            overall_confidence=Decimal("0.000"),
            status=ReviewStatus.POSTED,
            created_at=now,
        )
        assert review.overall_confidence == Decimal("0.000")

        # Valid: 1
        review = Review(
            review_id="test",
            pr_number=1,
            repository_owner="org",
            repository_name="repo",
            head_sha="a" * 40,
            findings=[],
            overall_confidence=Decimal("1.000"),
            status=ReviewStatus.POSTED,
            created_at=now,
        )
        assert review.overall_confidence == Decimal("1.000")

        # Invalid: > 1
        with pytest.raises(ValidationError):
            Review(
                review_id="test",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                findings=[],
                overall_confidence=Decimal("1.500"),
                status=ReviewStatus.POSTED,
                created_at=now,
            )

        # Invalid: < 0
        with pytest.raises(ValidationError):
            Review(
                review_id="test",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                findings=[],
                overall_confidence=Decimal("-0.100"),
                status=ReviewStatus.POSTED,
                created_at=now,
            )

    def test_review_json_round_trip(self) -> None:
        """Review serializes and deserializes via JSON."""
        now = datetime.now()
        original = Review(
            review_id="ghpr-12345-run-001",
            pr_number=12345,
            repository_owner="myorg",
            repository_name="myapp",
            head_sha="c" * 40,
            findings=[],
            overall_confidence=Decimal("0.750"),
            status=ReviewStatus.POSTED,
            created_at=now,
            error_message=None,
        )
        json_data = json.loads(original.model_dump_json())
        reconstructed = Review(**json_data)
        assert reconstructed.review_id == original.review_id
        assert reconstructed.overall_confidence == Decimal("0.750")


class TestWebhookEvent:
    """Test WebhookEvent contract construction, validation, and serialization."""

    def test_webhook_event_valid_construction(self) -> None:
        """WebhookEvent can be constructed with valid fields."""
        event = WebhookEvent(
            action="opened",
            pr_number=12345,
            repository_owner="myorg",
            repository_name="myapp",
            head_sha="d" * 40,
            delivery_id="12345678-1234-1234-1234-123456789012",
            received_at="2025-01-15T10:30:00Z",
        )
        assert event.action == "opened"
        assert event.pr_number == 12345
        assert event.delivery_id == "12345678-1234-1234-1234-123456789012"

    def test_webhook_event_head_sha_validation(self) -> None:
        """head_sha must be exactly 40 hex characters."""
        # Valid
        event = WebhookEvent(
            action="opened",
            pr_number=1,
            repository_owner="org",
            repository_name="repo",
            head_sha="a" * 40,
            delivery_id="12345678-1234-1234-1234-123456789012",
            received_at="2025-01-15T10:30:00Z",
        )
        assert len(event.head_sha) == 40

        # Invalid: non-hex
        with pytest.raises(ValidationError):
            WebhookEvent(
                action="opened",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="z" * 40,  # 'z' is not hex
                delivery_id="12345678-1234-1234-1234-123456789012",
                received_at="2025-01-15T10:30:00Z",
            )

    def test_webhook_event_delivery_id_validation(self) -> None:
        """delivery_id must be a UUID format, not just 36 characters of anything."""
        # Valid: a real, canonical lowercase UUID
        event = WebhookEvent(
            action="opened",
            pr_number=1,
            repository_owner="org",
            repository_name="repo",
            head_sha="a" * 40,
            delivery_id="12345678-1234-1234-1234-123456789012",
            received_at="2025-01-15T10:30:00Z",
        )
        assert event.delivery_id == "12345678-1234-1234-1234-123456789012"

        # Invalid: too short
        with pytest.raises(ValidationError):
            WebhookEvent(
                action="opened",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                delivery_id="12345678-1234-1234-1234-12345678901",  # too short
                received_at="2025-01-15T10:30:00Z",
            )

        # Invalid: 36 chars but not UUID-shaped (no hyphens in the right places,
        # not hex). Length alone used to be enough to pass; it must not be.
        with pytest.raises(ValidationError):
            WebhookEvent(
                action="opened",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                delivery_id="1" * 36,
                received_at="2025-01-15T10:30:00Z",
            )

    def test_webhook_event_action_validation(self) -> None:
        """action must be non-empty and <= 50 chars."""
        # Valid: common GitHub actions
        for action in ["opened", "synchronize", "reopened", "closed", "unlabeled"]:
            event = WebhookEvent(
                action=action,
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                delivery_id="12345678-1234-1234-1234-123456789012",
                received_at="2025-01-15T10:30:00Z",
            )
            assert event.action == action

        # Invalid: empty
        with pytest.raises(ValidationError):
            WebhookEvent(
                action="",
                pr_number=1,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                delivery_id="12345678-1234-1234-1234-123456789012",
                received_at="2025-01-15T10:30:00Z",
            )

    def test_webhook_event_pr_number_validation(self) -> None:
        """pr_number must be > 0."""
        # Valid
        event = WebhookEvent(
            action="opened",
            pr_number=1,
            repository_owner="org",
            repository_name="repo",
            head_sha="a" * 40,
            delivery_id="12345678-1234-1234-1234-123456789012",
            received_at="2025-01-15T10:30:00Z",
        )
        assert event.pr_number == 1

        # Invalid: 0
        with pytest.raises(ValidationError):
            WebhookEvent(
                action="opened",
                pr_number=0,
                repository_owner="org",
                repository_name="repo",
                head_sha="a" * 40,
                delivery_id="12345678-1234-1234-1234-123456789012",
                received_at="2025-01-15T10:30:00Z",
            )

    def test_webhook_event_json_round_trip(self) -> None:
        """WebhookEvent serializes and deserializes via JSON."""
        original = WebhookEvent(
            action="opened",
            pr_number=12345,
            repository_owner="myorg",
            repository_name="myapp",
            head_sha="e" * 40,
            delivery_id="12345678-1234-1234-1234-123456789012",
            received_at="2025-01-15T10:30:00Z",
        )
        json_data = json.loads(original.model_dump_json())
        reconstructed = WebhookEvent(**json_data)
        assert reconstructed == original
        assert reconstructed.pr_number == 12345
        assert reconstructed.delivery_id == "12345678-1234-1234-1234-123456789012"
