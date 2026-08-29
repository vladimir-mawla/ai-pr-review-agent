"""Finding domain contract.

This module defines the Finding model: a typed report of a single code issue
discovered by an agent. Findings are the atomic unit of review output.

Why this shape: Findings must be independently scorable (confidence), routeable
(severity), attributed (agent_type, category), and locatable (file_path, line_start/end),
while supporting custom reasoning. The confidence bound (0..1) is enforced at
validation time to prevent impossible values.
"""

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from backend.models.enums import AgentType, Severity


class Finding(BaseModel):
    """A single code issue discovered during review.

    Attributes:
        agent_type: Which specialist discovered this (SECURITY, QUALITY, TESTS, DOCS).
        severity: Impact level of the issue (CRITICAL to INFO).
        category: Human-readable category (e.g., "sql_injection", "missing_test_coverage").
        file_path: Relative path to the file (e.g., "src/auth/login.py").
        line_start: Starting line number (1-indexed).
        line_end: Ending line number, inclusive. Must be >= line_start.
        confidence: Confidence that this finding is valid, in [0, 1] with 3-decimal precision.
                   Semantics: 1.0 = definitely correct, 0.5 = uncertain, 0.0 = unknown.
        rationale: Explanation of the finding and why it matters.
    """

    agent_type: AgentType
    severity: Severity
    category: str = Field(
        min_length=1,
        max_length=100,
        description="Category/type of finding (e.g., sql_injection, missing_test)",
    )
    file_path: str = Field(
        min_length=1,
        max_length=1000,
        description="Relative path to file (e.g., src/auth/login.py)",
    )
    line_start: int = Field(gt=0, description="Starting line number (1-indexed)")
    line_end: int = Field(gt=0, description="Ending line number (1-indexed, >= line_start)")
    confidence: Decimal = Field(
        ge=Decimal("0.000"),
        le=Decimal("1.000"),
        decimal_places=3,
        description="Confidence in [0.000, 1.000] with 3-decimal precision",
    )
    rationale: str = Field(
        min_length=1,
        max_length=5000,
        description="Explanation of the finding",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "agent_type": "SECURITY",
                    "severity": "CRITICAL",
                    "category": "sql_injection",
                    "file_path": "src/db.py",
                    "line_start": 42,
                    "line_end": 42,
                    "confidence": "0.950",
                    "rationale": "User input directly interpolated into SQL query without parameterization.",
                }
            ]
        }
    }

    @model_validator(mode="after")
    def _check_line_range(self) -> "Finding":
        # The docstring and field description both promise line_end >= line_start;
        # a range that ends before it starts is not a smaller range, it's a
        # contradiction that would silently corrupt any diff/snippet rendering
        # downstream, so it must be rejected at construction time rather than
        # trusted on the docstring's word alone.
        if self.line_end < self.line_start:
            raise ValueError(
                f"line_end ({self.line_end}) must be >= line_start ({self.line_start})"
            )
        return self

    def __lt__(self, other: object) -> bool:
        """Sort findings by severity (descending) then confidence (descending)."""
        if not isinstance(other, Finding):
            return NotImplemented
        if self.severity.value != other.severity.value:
            severity_order = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3, "INFO": 4}
            return severity_order.get(self.severity.value, 5) < severity_order.get(
                other.severity.value, 5
            )
        return self.confidence > other.confidence
