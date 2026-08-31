"""Tests for the golden dataset loader.

FREE -- no network, no credential. Proves the dataset actually loads, every
entry is well-formed, every referenced diff file exists, and the loader
fails loudly (not silently) on the failure modes that would otherwise let a
broken dataset entry slip through unnoticed -- the same discipline this
milestone's build report holds the CI workflow files to, applied here.
"""

from __future__ import annotations

import json

import pytest

from backend.evaluation.golden_dataset import (
    DEFAULT_GOLDEN_DATASET_PATH,
    GoldenDatasetError,
    load_golden_dataset,
)


class TestLoadGoldenDataset:
    def test_default_dataset_file_exists(self):
        assert DEFAULT_GOLDEN_DATASET_PATH.is_file()

    def test_loads_at_least_four_cases(self):
        cases = load_golden_dataset()
        assert len(cases) >= 4

    def test_every_case_id_is_unique(self):
        cases = load_golden_dataset()
        ids = [case.case_id for case in cases]
        assert len(ids) == len(set(ids))

    def test_every_case_has_nonempty_diff_text(self):
        cases = load_golden_dataset()
        for case in cases:
            assert case.diff_text.strip(), f"{case.case_id} resolved to empty diff text"

    def test_expected_clean_cases_have_no_expected_findings(self):
        cases = load_golden_dataset()
        for case in cases:
            if case.expected_clean:
                assert case.expected_findings == []

    def test_non_clean_cases_have_at_least_one_expected_finding(self):
        cases = load_golden_dataset()
        for case in cases:
            if not case.expected_clean:
                assert len(case.expected_findings) >= 1, f"{case.case_id} has no expected findings"

    def test_sqli_case_has_a_must_detect_critical_sql_injection_finding(self):
        cases = {case.case_id: case for case in load_golden_dataset()}
        sqli_case = cases["sqli-basic"]
        must_detect = sqli_case.must_detect_findings
        assert any(f.category == "sql_injection" and f.severity.value == "CRITICAL" for f in must_detect)


class TestLoadGoldenDatasetFailureModes:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(GoldenDatasetError, match="not found"):
            load_golden_dataset(tmp_path / "does-not-exist.json")

    def test_malformed_json_raises(self, tmp_path):
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(GoldenDatasetError, match="not valid JSON"):
            load_golden_dataset(bad_file)

    def test_empty_cases_list_raises(self, tmp_path):
        empty_file = tmp_path / "empty.json"
        empty_file.write_text(json.dumps({"cases": []}), encoding="utf-8")
        with pytest.raises(GoldenDatasetError, match="cases"):
            load_golden_dataset(empty_file)

    def test_missing_diff_file_raises(self, tmp_path):
        bad_case_file = tmp_path / "bad_case.json"
        bad_case_file.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "case_id": "ghost",
                            "description": "references a diff that does not exist",
                            "diff_path": "tests/fixtures/does_not_exist.patch",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(GoldenDatasetError, match="does not exist"):
            load_golden_dataset(bad_case_file)

    def test_duplicate_case_id_raises(self, tmp_path):
        dup_file = tmp_path / "dup.json"
        entry = {
            "case_id": "dup",
            "description": "d",
            "diff_path": "tests/fixtures/clean_diff.patch",
            "expected_clean": True,
        }
        dup_file.write_text(json.dumps({"cases": [entry, dict(entry)]}), encoding="utf-8")
        with pytest.raises(GoldenDatasetError, match="duplicate"):
            load_golden_dataset(dup_file)

    def test_invalid_entry_raises(self, tmp_path):
        invalid_file = tmp_path / "invalid.json"
        invalid_file.write_text(
            json.dumps({"cases": [{"case_id": "", "description": "d", "diff_path": "x"}]}),
            encoding="utf-8",
        )
        with pytest.raises(GoldenDatasetError, match="invalid golden dataset entry"):
            load_golden_dataset(invalid_file)
