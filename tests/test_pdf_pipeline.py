"""test_pdf_pipeline.py — lib/pdf_pipeline.py 단위 테스트.

ox.extract_pdf_pages / ox.count_pdf_pages 를 stub 으로 교체하여
실제 PDF / 네트워크 호출 없이 검증한다.

테스트 항목:
  A) _build_chunks 청크 윈도우 매트릭스
     - chunk_size=20 + count=20/21/50
     - single_chunk_max=25: count=25(단일) / count=26(분할)
     - single_chunk_max=10**9 (ava1식): 항상 단일
  B) convert_pdf 정상 경로
     - 병합: \n\n---\n\n / {stem}.md 저장
     - 프롬프트 verbatim vs {start}/{end} 포맷
     - post_process 적용
  C) on_failure='abort': 청크 실패 → None 반환, 파일 미생성
  D) on_failure='partial': 실패 청크 → MISSING 마커 + .partial.md
  E) rate_limit_s: 청크 간 sleep 호출 / 마지막 청크 뒤 sleep 없음
  F) 모든 청크 실패(partial) → None 반환

실행:
    .venv/bin/python -m pytest tests/test_pdf_pipeline.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import pdf_pipeline as pp
from fmdw.pdf_pipeline import _build_chunks, _missing_marker


# ──────────────────────────────────────────────────────────────────────────────
# A) _build_chunks 청크 윈도우 매트릭스
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildChunks(unittest.TestCase):
    """_build_chunks: 청크 (start, end) 시퀀스 정확성."""

    # ── chunk_size=20, single_chunk_max=None ──────────────────────────────────

    def test_exact_fit_20p(self):
        """count=20 → 단일 청크 (1,20)."""
        chunks = _build_chunks(20, 20, None)
        self.assertEqual(chunks, [(1, 20)])

    def test_one_over_20p(self):
        """count=21 → 두 청크 (1,20) + (21,21)."""
        chunks = _build_chunks(21, 20, None)
        self.assertEqual(chunks, [(1, 20), (21, 21)])

    def test_50p(self):
        """count=50 → (1,20) + (21,40) + (41,50)."""
        chunks = _build_chunks(50, 20, None)
        self.assertEqual(chunks, [(1, 20), (21, 40), (41, 50)])

    def test_full_coverage_no_gap(self):
        """청크가 빠짐없이 전체 페이지를 커버."""
        for total in [1, 19, 20, 21, 40, 41, 100]:
            chunks = _build_chunks(total, 20, None)
            # 시작은 1, 끝은 total
            self.assertEqual(chunks[0][0], 1, f"total={total}")
            self.assertEqual(chunks[-1][1], total, f"total={total}")
            # 인접 청크 연속성
            for j in range(len(chunks) - 1):
                self.assertEqual(chunks[j][1] + 1, chunks[j+1][0],
                                 f"total={total} gap at chunk {j}")

    # ── single_chunk_max=25 ───────────────────────────────────────────────────

    def test_single_chunk_max_at_boundary(self):
        """count=25 <= single_chunk_max=25 → 단일 청크."""
        chunks = _build_chunks(25, 20, single_chunk_max=25)
        self.assertEqual(chunks, [(1, 25)])

    def test_single_chunk_max_over_boundary(self):
        """count=26 > single_chunk_max=25 → chunk_size=20 분할."""
        chunks = _build_chunks(26, 20, single_chunk_max=25)
        self.assertEqual(chunks, [(1, 20), (21, 26)])

    def test_single_chunk_max_1_page(self):
        """count=1 → 항상 단일 청크."""
        chunks = _build_chunks(1, 20, single_chunk_max=25)
        self.assertEqual(chunks, [(1, 1)])

    # ── single_chunk_max=10**9 (ava1식) ──────────────────────────────────────

    def test_always_single_chunk_large_max(self):
        """single_chunk_max=10**9 → 어떤 count든 단일 청크."""
        for total in [1, 25, 100, 500, 1000]:
            chunks = _build_chunks(total, 20, single_chunk_max=10**9)
            self.assertEqual(chunks, [(1, total)],
                             f"total={total} should be single chunk")


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼: convert_pdf 를 위한 공통 mock 설정
# ──────────────────────────────────────────────────────────────────────────────

def _mock_ox(total_pages: int, responses: list):
    """ox.count_pdf_pages / ox.extract_pdf_pages mock 반환.

    responses: 청크 순서대로의 반환값. Exception 인스턴스면 raise, str이면 반환.
    """
    call_idx = [0]

    def fake_extract(prompt, pdf_path, start, end):
        idx = call_idx[0]
        call_idx[0] += 1
        r = responses[idx] if idx < len(responses) else "chunk text"
        if isinstance(r, Exception):
            raise r
        return r

    mock_count = MagicMock(return_value=total_pages)
    mock_extract = MagicMock(side_effect=fake_extract)
    return mock_count, mock_extract


# ──────────────────────────────────────────────────────────────────────────────
# B) convert_pdf 정상 경로
# ──────────────────────────────────────────────────────────────────────────────

class TestConvertPdfSuccess(unittest.TestCase):
    """정상 추출 경로 검증."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run(self, total_pages, responses, prompt_template,
             chunk_size=20, single_chunk_max=None,
             post_process=None, rate_limit_s=0.0):
        mock_count, mock_extract = _mock_ox(total_pages, responses)
        out = self._tmp / "test_doc.md"
        fake_pdf = self._tmp / "test_doc.pdf"
        fake_pdf.touch()
        with patch.object(pp.ox, "count_pdf_pages", mock_count), \
             patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
             patch.object(pp, "time") as mock_time:
            result = pp.convert_pdf(
                fake_pdf, prompt_template,
                output_path=out,
                chunk_size=chunk_size,
                single_chunk_max=single_chunk_max,
                rate_limit_s=rate_limit_s,
                post_process=post_process,
            )
        return result, out, mock_extract, mock_time

    def test_single_chunk_creates_md(self):
        """단일 청크 성공 → {stem}.md 생성."""
        result, out, _, _ = self._run(20, ["chunk1"], "prompt no range")
        self.assertIsNotNone(result)
        self.assertTrue(out.exists())
        self.assertEqual(out.read_text(encoding="utf-8"), "chunk1")

    def test_two_chunks_joined_with_sep(self):
        """2청크 성공 → \\n\\n---\\n\\n 병합."""
        result, out, _, _ = self._run(
            21, ["chunk1", "chunk2"], "prompt {start} {end}",
            chunk_size=20
        )
        content = out.read_text(encoding="utf-8")
        self.assertIn("\n\n---\n\n", content)
        self.assertTrue(content.startswith("chunk1"))
        self.assertTrue(content.endswith("chunk2"))

    def test_prompt_range_substitution(self):
        """프롬프트에 {start}/{end} 있으면 청크별 치환."""
        mock_count, mock_extract = _mock_ox(21, ["a", "b"])
        prompts_seen = []

        def capturing_extract(prompt, pdf_path, start, end):
            prompts_seen.append(prompt)
            return f"text {start}-{end}"

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", MagicMock(side_effect=capturing_extract)), \
                 patch.object(pp, "time"):
                pp.convert_pdf(fake_pdf, "pages {start}~{end}", output_path=out,
                               chunk_size=20, rate_limit_s=0)

        self.assertEqual(prompts_seen[0], "pages 1~20")
        self.assertEqual(prompts_seen[1], "pages 21~21")

    def test_prompt_verbatim_no_substitution(self):
        """프롬프트에 {start}/{end} 없으면 verbatim."""
        mock_count, mock_extract = _mock_ox(21, ["a", "b"])
        prompts_seen = []

        def capturing_extract(prompt, pdf_path, start, end):
            prompts_seen.append(prompt)
            return "ok"

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", MagicMock(side_effect=capturing_extract)), \
                 patch.object(pp, "time"):
                pp.convert_pdf(fake_pdf, "VERBATIM_PROMPT", output_path=out,
                               chunk_size=20, rate_limit_s=0)

        self.assertTrue(all(p == "VERBATIM_PROMPT" for p in prompts_seen),
                        f"verbatim 아님: {prompts_seen}")

    def test_post_process_applied(self):
        """post_process callable 이 병합 결과에 적용된다."""
        called_with = []

        def my_post(text):
            called_with.append(text)
            return text.upper()

        result, out, _, _ = self._run(
            20, ["hello"], "prompt",
            post_process=my_post
        )
        self.assertTrue(len(called_with) == 1)
        self.assertEqual(out.read_text(encoding="utf-8"), "HELLO")

    def test_single_chunk_max_forces_single(self):
        """single_chunk_max=10**9 → count=50 이어도 단일 청크."""
        mock_count, mock_extract = _mock_ox(50, ["bigchunk"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
                 patch.object(pp, "time"):
                pp.convert_pdf(fake_pdf, "p {start}-{end}", output_path=out,
                               chunk_size=20, single_chunk_max=10**9,
                               rate_limit_s=0)
        # extract_pdf_pages 가 1번만 호출됐어야 함
        self.assertEqual(mock_extract.call_count, 1)
        args = mock_extract.call_args
        # start=1, end=50 (total_pages)
        self.assertEqual(args[0][2], 1)   # start
        self.assertEqual(args[0][3], 50)  # end


# ──────────────────────────────────────────────────────────────────────────────
# C) on_failure='abort'
# ──────────────────────────────────────────────────────────────────────────────

class TestOnFailureAbort(unittest.TestCase):
    """청크 실패 시 abort 정책 검증."""

    def test_abort_returns_none(self):
        """첫 청크 실패 → None 반환."""
        mock_count, mock_extract = _mock_ox(20, [RuntimeError("boom")])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
                 patch.object(pp, "time"):
                result = pp.convert_pdf(fake_pdf, "prompt", output_path=out,
                                        chunk_size=20, on_failure="abort",
                                        rate_limit_s=0)
        self.assertIsNone(result)

    def test_abort_no_file_created(self):
        """abort 시 .md / .partial.md 모두 미생성."""
        mock_count, mock_extract = _mock_ox(21,
                                             ["ok", RuntimeError("boom")])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            partial = Path(tmp) / "doc.partial.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
                 patch.object(pp, "time"):
                result = pp.convert_pdf(fake_pdf, "prompt", output_path=out,
                                        chunk_size=20, on_failure="abort",
                                        rate_limit_s=0)
        self.assertIsNone(result)
        self.assertFalse(out.exists(), ".md 가 생성되면 안 됨")
        self.assertFalse(partial.exists(), ".partial.md 가 생성되면 안 됨")

    def test_abort_mid_chunk_stops_remaining(self):
        """2/3 번째 청크 실패 시 이후 청크 호출 없음."""
        mock_count, mock_extract = _mock_ox(60,
                                             ["ok", RuntimeError("fail"), "never"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
                 patch.object(pp, "time"):
                pp.convert_pdf(fake_pdf, "p", output_path=out,
                               chunk_size=20, on_failure="abort",
                               rate_limit_s=0)
        # 2번 호출 후 중단 (3번째 "never" 에 도달하면 안 됨)
        self.assertEqual(mock_extract.call_count, 2)


# ──────────────────────────────────────────────────────────────────────────────
# D) on_failure='partial'
# ──────────────────────────────────────────────────────────────────────────────

class TestOnFailurePartial(unittest.TestCase):
    """partial 정책 검증."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _make_paths(self, stem="doc"):
        out = self._tmp / f"{stem}.md"
        partial = self._tmp / f"{stem}.partial.md"
        fake_pdf = self._tmp / f"{stem}.pdf"
        fake_pdf.touch()
        return out, partial, fake_pdf

    def test_partial_creates_partial_md(self):
        """청크 실패 → .partial.md 생성."""
        out, partial, fake_pdf = self._make_paths()
        mock_count, mock_extract = _mock_ox(21, ["chunk1", RuntimeError("fail")])
        with patch.object(pp.ox, "count_pdf_pages", mock_count), \
             patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
             patch.object(pp, "time"):
            result = pp.convert_pdf(fake_pdf, "p {start} {end}",
                                    output_path=out,
                                    chunk_size=20, on_failure="partial",
                                    rate_limit_s=0)
        self.assertIsNotNone(result)
        self.assertFalse(out.exists(), ".md(완성본) 가 생성되면 안 됨")
        self.assertTrue(partial.exists(), ".partial.md 가 생성되어야 함")

    def test_partial_contains_missing_marker(self):
        """실패 청크 위치에 MISSING 마커 포함."""
        out, partial, fake_pdf = self._make_paths()
        mock_count, mock_extract = _mock_ox(21, ["chunk1", RuntimeError("fail")])
        with patch.object(pp.ox, "count_pdf_pages", mock_count), \
             patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
             patch.object(pp, "time"):
            pp.convert_pdf(fake_pdf, "p {start} {end}",
                           output_path=out,
                           chunk_size=20, on_failure="partial",
                           rate_limit_s=0)
        content = partial.read_text(encoding="utf-8")
        self.assertIn("MISSING", content.upper())
        self.assertIn("chunk1", content)

    def test_partial_continues_after_failure(self):
        """실패 청크 이후 남은 청크도 계속 처리(middle failure)."""
        out, _, fake_pdf = self._make_paths()
        mock_count, mock_extract = _mock_ox(60,
                                             ["chunk1", RuntimeError("fail"), "chunk3"])
        with patch.object(pp.ox, "count_pdf_pages", mock_count), \
             patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
             patch.object(pp, "time"):
            pp.convert_pdf(fake_pdf, "p {start} {end}",
                           output_path=out,
                           chunk_size=20, on_failure="partial",
                           rate_limit_s=0)
        self.assertEqual(mock_extract.call_count, 3)

    def test_all_chunks_fail_returns_none(self):
        """모든 청크 실패(partial) → None 반환."""
        out, _, fake_pdf = self._make_paths()
        mock_count, mock_extract = _mock_ox(40,
                                             [RuntimeError("f1"), RuntimeError("f2")])
        with patch.object(pp.ox, "count_pdf_pages", mock_count), \
             patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
             patch.object(pp, "time"):
            result = pp.convert_pdf(fake_pdf, "p",
                                    output_path=out,
                                    chunk_size=20, on_failure="partial",
                                    rate_limit_s=0)
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────────────
# E) rate_limit_s — sleep 위치 검증
# ──────────────────────────────────────────────────────────────────────────────

class TestRateLimit(unittest.TestCase):
    """청크 간 sleep 호출 / 마지막 뒤 sleep 없음."""

    def _run_with_sleep_capture(self, total_pages, chunk_size, rate_limit_s):
        mock_count = MagicMock(return_value=total_pages)
        mock_extract = MagicMock(return_value="text")
        sleep_calls = []

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            fake_pdf.touch()
            with patch.object(pp.ox, "count_pdf_pages", mock_count), \
                 patch.object(pp.ox, "extract_pdf_pages", mock_extract), \
                 patch.object(pp, "time") as mock_time:
                mock_time.sleep.side_effect = lambda s: sleep_calls.append(s)
                pp.convert_pdf(fake_pdf, "p", output_path=out,
                               chunk_size=chunk_size, rate_limit_s=rate_limit_s)
        return sleep_calls

    def test_single_chunk_no_sleep(self):
        """청크 1개 → sleep 0회."""
        calls = self._run_with_sleep_capture(20, 20, rate_limit_s=10.0)
        self.assertEqual(len(calls), 0, f"단일 청크 뒤 sleep 없어야 함: {calls}")

    def test_two_chunks_one_sleep(self):
        """청크 2개 → 중간 sleep 1회, 마지막 뒤 0회."""
        calls = self._run_with_sleep_capture(21, 20, rate_limit_s=15.0)
        self.assertEqual(len(calls), 1, f"2청크 → sleep 1회: {calls}")
        self.assertAlmostEqual(calls[0], 15.0)

    def test_three_chunks_two_sleeps(self):
        """청크 3개 → sleep 2회."""
        calls = self._run_with_sleep_capture(50, 20, rate_limit_s=10.0)
        self.assertEqual(len(calls), 2, f"3청크 → sleep 2회: {calls}")


# ──────────────────────────────────────────────────────────────────────────────
# F) _missing_marker 형식
# ──────────────────────────────────────────────────────────────────────────────

class TestMissingMarker(unittest.TestCase):
    def test_marker_format(self):
        m = _missing_marker(3, 5)
        self.assertIn("MISSING", m.upper())
        self.assertIn("3", m)
        self.assertIn("5", m)
        self.assertTrue(m.startswith("<!--"))
        self.assertTrue(m.endswith("-->"))


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
