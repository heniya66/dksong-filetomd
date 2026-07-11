"""test_figure_renumber.py — M-3 회귀 가드.

extract_all_via_pdf.renumber_figures 가 청크/페이지 결합 후 `### Figure N` 헤딩을
문서 전역 1..K 로 유일하게 리넘버하는지 검증한다. 또한 기존 동작 보존(MISSING/
TRUNCATED 마커·코드펜스 내부·비-Figure 헤딩 무변경)을 고정한다.

실행:
    .venv/bin/python -m pytest tests/test_figure_renumber.py -v
"""
from __future__ import annotations

import os
import re
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import extract_all_via_pdf as e  # noqa: E402

_HEADING = re.compile(r"^###\s+Figure\s+(\d+)", re.IGNORECASE | re.MULTILINE)


def _figure_numbers(md: str) -> list[int]:
    return [int(n) for n in _HEADING.findall(md)]


class TestRenumberGlobalUnique(unittest.TestCase):
    """핵심: 결합 후 Figure 번호가 전역 유일·연속."""

    def test_duplicate_figure1_across_chunks_renumbered(self):
        """청크마다 1부터 재시작한 Figure 가 전역 1,2,3 으로 유일화."""
        md = (
            "### Figure 1: chunk1 fig\nbody\n"
            "\n\n---\n\n"
            "### Figure 1: chunk2 fig\nbody\n"
            "### Figure 2: chunk2 fig2\n"
        )
        out = e.renumber_figures(md)
        nums = _figure_numbers(out)
        self.assertEqual(nums, [1, 2, 3], out)
        # 유일성(중복 없음).
        self.assertEqual(len(nums), len(set(nums)), out)

    def test_titles_and_separators_preserved(self):
        """번호만 교체 — 제목·콜론·구분자는 원문 그대로."""
        md = "### Figure 1: DDR2 bank\n\n---\n\n### Figure 1: Power tree"
        out = e.renumber_figures(md)
        self.assertIn("### Figure 1: DDR2 bank", out)
        self.assertIn("### Figure 2: Power tree", out)

    def test_heading_level_preserved(self):
        """원래 `#` 개수(##/####) 보존."""
        md = "## Figure 1: a\n\n#### Figure 1: b"
        out = e.renumber_figures(md)
        self.assertIn("## Figure 1: a", out)
        self.assertIn("#### Figure 2: b", out)

    def test_many_figures_sequential(self):
        md = "\n\n---\n\n".join(f"### Figure 1: f{i}" for i in range(10))
        out = e.renumber_figures(md)
        self.assertEqual(_figure_numbers(out), list(range(1, 11)), out)


class TestRenumberPreservesContract(unittest.TestCase):
    """기존 동작 보존(H-5 마커, 코드펜스, 비-Figure)."""

    def test_missing_marker_preserved(self):
        md = (
            "### Figure 1: a\n"
            "<!-- MISSING pages 5-6: extraction failed -->\n"
            "### Figure 1: b"
        )
        out = e.renumber_figures(md)
        self.assertIn("<!-- MISSING pages 5-6: extraction failed -->", out)
        self.assertEqual(_figure_numbers(out), [1, 2], out)

    def test_truncated_marker_preserved(self):
        md = (
            "### Figure 1: a\n"
            "<!-- TRUNCATED: finish_reason=length -->\n"
            "### Figure 1: b"
        )
        out = e.renumber_figures(md)
        self.assertIn("<!-- TRUNCATED: finish_reason=length -->", out)
        self.assertEqual(_figure_numbers(out), [1, 2], out)

    def test_code_fence_content_not_renumbered(self):
        """코드펜스 내부의 `### Figure N` 유사 텍스트는 헤딩이 아니므로 무변경."""
        md = (
            "### Figure 1: real\n"
            "```\n"
            "### Figure 1: this is inside a fence, leave it\n"
            "```\n"
            "### Figure 1: real2"
        )
        out = e.renumber_figures(md)
        # 실제 헤딩 2개만 리넘버(1,2). 펜스 안의 "Figure 1" 은 그대로.
        self.assertIn("### Figure 1: real", out)
        self.assertIn("### Figure 2: real2", out)
        self.assertIn("### Figure 1: this is inside a fence, leave it", out)

    def test_tilde_fence_supported(self):
        md = (
            "### Figure 1: real\n"
            "~~~\n"
            "### Figure 1: inside tilde fence\n"
            "~~~\n"
            "### Figure 1: real2"
        )
        out = e.renumber_figures(md)
        self.assertIn("### Figure 2: real2", out)
        self.assertIn("### Figure 1: inside tilde fence", out)

    def test_non_figure_headings_untouched(self):
        md = "## Section 1\n### Figure 1: a\n## Section 2\n### Figure 1: b"
        out = e.renumber_figures(md)
        self.assertIn("## Section 1", out)
        self.assertIn("## Section 2", out)
        self.assertEqual(_figure_numbers(out), [1, 2], out)

    def test_no_figures_passthrough(self):
        md = "# Title\nsome text\n## sub\nmore"
        self.assertEqual(e.renumber_figures(md), md)

    def test_empty_passthrough(self):
        self.assertEqual(e.renumber_figures(""), "")

    def test_idempotent_on_already_sequential(self):
        """이미 전역 유일한 입력에 재적용해도 결과 동일(멱등)."""
        md = "### Figure 1: a\n\n---\n\n### Figure 2: b\n\n---\n\n### Figure 3: c"
        once = e.renumber_figures(md)
        twice = e.renumber_figures(once)
        self.assertEqual(once, twice)
        self.assertEqual(_figure_numbers(twice), [1, 2, 3])


class TestRenumberInProcessFileCombine(unittest.TestCase):
    """결합 단계 통합: combined 문자열에 renumber 가 실제 적용되는 경로 고정.

    process_file 의 chunk 결합 후 renumber_figures(combined) 호출 계약을 직접
    재현(파일 IO/네트워크 없이 결합·리넘버만)한다.
    """

    def test_combine_then_renumber(self):
        chunk_texts = [
            "### Figure 1: c1",
            "### Figure 1: c2",
            "<!-- MISSING pages 3-4: extraction failed -->",
            "### Figure 1: c3\n### Figure 2: c3b",
        ]
        combined = "\n\n---\n\n".join(chunk_texts)
        out = e.renumber_figures(combined)
        # MISSING 마커 보존 + 전역 1..4.
        self.assertIn("<!-- MISSING pages 3-4: extraction failed -->", out)
        self.assertEqual(_figure_numbers(out), [1, 2, 3, 4], out)
        # 결합 구분자(\n\n---\n\n) 보존.
        self.assertEqual(out.count("\n\n---\n\n"), 3, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
