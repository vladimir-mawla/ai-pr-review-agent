"""The golden dataset: known PR diffs paired with what a correct review must catch.

Owns: ``GoldenCase``/``ExpectedFinding`` (the typed shape of one dataset
entry) and ``load_golden_dataset`` (reads
``tests/fixtures/golden_dataset.json``, validates it, and resolves every
``diff_path`` to real diff text -- failing loudly if a referenced fixture
file does not exist, rather than silently producing a case with no diff to
review). This is the same "every referenced file must actually exist"
discipline ``.genesis/DONE.html`` section 2's CI gate names for workflow
files, applied here to the dataset that feeds the regression gate.

Each case is a real, checkable diff (``tests/fixtures/*.patch``) plus the
findings a correct review is expected to surface. ``must_detect=True``
marks a finding the judge should treat as mandatory (missing it is a
serious miss -- e.g. the SQL injection in ``sqli-basic``);
``must_detect=False`` marks a nice-to-catch finding a judge should reward
but not require. ``expected_clean=True`` marks a case with no real issues,
so the judge should instead penalize a review that fabricates CRITICAL/HIGH
findings on it.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from backend.models.enums import Severity

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOLDEN_DATASET_PATH = _REPO_ROOT / "tests" / "fixtures" / "golden_dataset.json"


class GoldenDatasetError(Exception):
    """Raised when the golden dataset file is missing, malformed, or references a missing diff."""


class ExpectedFinding(BaseModel):
    """One finding a correct review is expected to surface for a ``GoldenCase``.

    Attributes:
        category: Free-text finding category (matches
            ``backend.models.findings.Finding.category``'s own free-text
            convention -- there is no fixed enum of categories).
        severity: The minimum severity tier a matching finding should carry.
        file_path: Which changed file the finding should be attributed to.
        must_detect: If ``True``, a review that misses this finding is a
            serious defect the judge should score harshly. If ``False``,
            it is a nice-to-catch the judge should reward but not require.
        notes: Human-readable context for whoever is reading the dataset
            (and fed to the judge as grounding for what "catching" this
            finding actually means for this specific case).
    """

    category: str = Field(min_length=1)
    severity: Severity
    file_path: str = Field(min_length=1)
    must_detect: bool = True
    notes: str = ""


class GoldenCase(BaseModel):
    """One golden-dataset entry: a real diff plus what a correct review must catch.

    Attributes:
        case_id: Stable, unique identifier for this case (used as the key
            the regression gate joins produced reviews against).
        description: Human-readable summary of what this diff contains and
            why it is in the dataset.
        diff_path: Path to the fixture diff, relative to the repo root.
        expected_clean: If ``True``, this diff has no real issues -- a
            correct review should have no (or only INFO-level) findings,
            and CRITICAL/HIGH findings here are hallucinations.
        expected_findings: What a correct review should surface. Empty for
            an ``expected_clean`` case.
        diff_text: The actual diff content, populated by
            ``load_golden_dataset`` (not read from the JSON file itself --
            see that function's docstring for why this is resolved eagerly
            rather than left for each caller to re-read).
    """

    case_id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    diff_path: str = Field(min_length=1)
    expected_clean: bool = False
    expected_findings: list[ExpectedFinding] = Field(default_factory=list)
    diff_text: str = Field(default="", repr=False)

    @property
    def must_detect_findings(self) -> list[ExpectedFinding]:
        """The subset of ``expected_findings`` a review must not miss."""
        return [f for f in self.expected_findings if f.must_detect]


def load_golden_dataset(path: Path | None = None) -> list[GoldenCase]:
    """Load and validate every case in the golden dataset, with real diff text attached.

    Diff text is resolved eagerly (not lazily, on first access) so a
    caller that just wants to sanity-check the dataset's shape (e.g.
    ``tests/eval/test_golden_dataset.py``) exercises the exact same
    file-existence check the regression gate itself depends on -- there is
    no code path that loads a ``GoldenCase`` successfully but only
    discovers a missing diff file later, at judge time.

    Raises:
        ``GoldenDatasetError``: the dataset file is missing/malformed, a
            case fails pydantic validation, or a case's ``diff_path`` does
            not exist on disk.
    """
    dataset_path = path if path is not None else DEFAULT_GOLDEN_DATASET_PATH
    if not dataset_path.is_file():
        raise GoldenDatasetError(f"golden dataset file not found: {dataset_path}")

    try:
        raw = json.loads(dataset_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GoldenDatasetError(f"golden dataset file is not valid JSON: {dataset_path}") from exc

    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise GoldenDatasetError(f"golden dataset file has no non-empty 'cases' list: {dataset_path}")

    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()
    for entry in raw_cases:
        try:
            case = GoldenCase.model_validate(entry)
        except Exception as exc:  # pydantic.ValidationError, re-raised as our own type
            raise GoldenDatasetError(f"invalid golden dataset entry: {entry!r}: {exc}") from exc

        if case.case_id in seen_ids:
            raise GoldenDatasetError(f"duplicate case_id in golden dataset: {case.case_id!r}")
        seen_ids.add(case.case_id)

        diff_file = _REPO_ROOT / case.diff_path
        if not diff_file.is_file():
            raise GoldenDatasetError(
                f"golden case {case.case_id!r} references a diff_path that does not exist: {diff_file}"
            )
        case = case.model_copy(update={"diff_text": diff_file.read_text(encoding="utf-8")})
        cases.append(case)

    return cases
