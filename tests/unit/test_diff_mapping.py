"""Unit tests for backend.integrations.diff_mapping.

Covers the exact matrix this milestone's build brief calls out explicitly:
a finding on an added line, a context line, a removed line, a line not in
the diff, and a file not in the PR at all.
"""

from __future__ import annotations

from backend.integrations.diff_mapping import build_diff_index, map_finding_to_anchor
from backend.integrations.github_models import ChangedFile

# A small, hand-constructed unified diff for one file:
#   old file: lines 1-5, unchanged except line 3 removed and a new line
#             inserted after it.
#   new file: line 3 is now the inserted line; old line 3 is gone.
_SAMPLE_PATCH = (
    "@@ -1,5 +1,5 @@\n"
    " line one (context)\n"
    " line two (context)\n"
    "-line three (removed)\n"
    "+line three REPLACED (added)\n"
    " line four (context)\n"
    " line five (context)\n"
)


def _index_for_one_file(filename: str = "src/app.py", patch: str = _SAMPLE_PATCH):
    changed_files = [ChangedFile(filename=filename, status="modified", patch=patch)]
    return build_diff_index(changed_files)


class TestMapFindingToAnchor:
    def test_added_line_maps_to_right_side_at_new_file_line_number(self) -> None:
        diff_index = _index_for_one_file()
        # "line three REPLACED (added)" is the 3rd line of the NEW file.
        anchor = map_finding_to_anchor(file_path="src/app.py", line_start=3, diff_index=diff_index)
        assert anchor is not None
        assert anchor.path == "src/app.py"
        assert anchor.line == 3
        assert anchor.side == "RIGHT"

    def test_context_line_maps_to_right_side(self) -> None:
        diff_index = _index_for_one_file()
        # "line four (context)" is line 4 in both old and new numbering;
        # RIGHT is checked first, so it resolves there.
        anchor = map_finding_to_anchor(file_path="src/app.py", line_start=4, diff_index=diff_index)
        assert anchor is not None
        assert anchor.side == "RIGHT"
        assert anchor.line == 4

    def test_removed_only_line_resolves_via_left_side_unambiguously(self) -> None:
        """A finding on a removed line anchors via side=LEFT at its OLD-file line number.

        The patch removes two consecutive lines with no replacement, so
        their old line numbers never reappear on the new/RIGHT side at
        all -- old numbering: keep one=1, removed A=2, removed B=3, keep
        two=4; new numbering: keep one=1, keep two=2. Old line 3
        ("removed B") has no RIGHT counterpart whatsoever, so resolving it
        proves the LEFT path independently of any RIGHT-side coincidence.
        """
        patch = "@@ -1,4 +1,3 @@\n keep one\n-removed A\n-removed B\n keep two\n"
        diff_index = _index_for_one_file(patch=patch)
        anchor = map_finding_to_anchor(file_path="src/app.py", line_start=3, diff_index=diff_index)
        assert anchor is not None
        assert anchor.side == "LEFT"
        assert anchor.line == 3

    def test_line_not_in_diff_is_unmappable(self) -> None:
        diff_index = _index_for_one_file()
        # Line 500 is nowhere near this small patch's hunk range.
        anchor = map_finding_to_anchor(file_path="src/app.py", line_start=500, diff_index=diff_index)
        assert anchor is None

    def test_file_not_in_pr_is_unmappable(self) -> None:
        diff_index = _index_for_one_file()
        anchor = map_finding_to_anchor(
            file_path="src/some_other_file.py", line_start=1, diff_index=diff_index
        )
        assert anchor is None

    def test_binary_file_with_no_patch_is_unmappable_not_an_error(self) -> None:
        changed_files = [ChangedFile(filename="assets/logo.png", status="modified", patch=None)]
        diff_index = build_diff_index(changed_files)
        anchor = map_finding_to_anchor(
            file_path="assets/logo.png", line_start=1, diff_index=diff_index
        )
        assert anchor is None

    def test_multi_hunk_file_maps_lines_from_the_second_hunk(self) -> None:
        patch = (
            "@@ -1,2 +1,2 @@\n"
            " top context\n"
            "+top added\n"
            "@@ -50,2 +51,3 @@\n"
            " bottom context\n"
            "+bottom added\n"
            " trailing context\n"
        )
        diff_index = _index_for_one_file(patch=patch)
        anchor = map_finding_to_anchor(file_path="src/app.py", line_start=52, diff_index=diff_index)
        assert anchor is not None
        assert anchor.side == "RIGHT"
        assert anchor.line == 52
