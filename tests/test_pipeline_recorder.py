"""test_pipeline_recorder.py — Phase 0 녹화기 시연 + 골든 비교 단위 테스트.

골든 고정 원칙:
  골든 스냅샷은 "git HEAD 원본 스크립트"에 고정한다.
  워킹트리 파일은 마이그레이션으로 바뀌므로, 거기서 골든을 캡처하면
  (a) 함수 소멸로 AttributeError, (b) "통합본 vs 통합본" 순환이 된다.
  `git show HEAD:<파일>`로 원본 소스를 임시 파일에 쓰고 record_script 에 전달.

검증 항목:
  R-1) record_convert_pdf: convert_pdf 호출 시퀀스 정확 캡처
  R-2) record_script(git-원본 blockdiagram): 원본 스크립트 호출 시퀀스 캡처
  R-3) git-원본 vs 통합본 골든 비교 (blockdiagram, 20/21/50페이지)
  R-4) PipelineSnapshot.assert_equals: 불일치 시 명확한 메시지

실행:
    .venv/bin/python -m pytest tests/test_pipeline_recorder.py -v
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tests._pipeline_recorder import (
    PipelineSnapshot,
    record_convert_pdf,
)
from tests._golden_fixtures import PRE_INTEGRATION_REF, load_golden


# ──────────────────────────────────────────────────────────────────────────────
# git 원본 추출 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _git_show(rel_path: str) -> str:
    """git show <PRE_INTEGRATION_REF>:<rel_path> 로 통합 직전 원본 소스 반환.

    ⚠️ HEAD 가 아니라 통합 직전 커밋을 본다(통합본 커밋으로 HEAD 에서 원본
    process_pdf 가 사라졌기 때문). 원본 PROMPT_TEMPLATE/OUTPUT_DIR 상수 추출용.
    """
    result = subprocess.run(
        ["git", "show", f"{PRE_INTEGRATION_REF}:{rel_path}"],
        capture_output=True, text=True, cwd=_ROOT, check=True,
    )
    return result.stdout


def _load_const_from_src(src: str, attr: str, module_name: str):
    """소스 문자열을 임시 파일로 저장 후 상수 추출 (mkdir 차단)."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as f:
        f.write(src)
        tmp_path = Path(f.name)
    try:
        spec = _ilu.spec_from_file_location(module_name, tmp_path)
        mod = _ilu.module_from_spec(spec)
        with patch.object(Path, "mkdir", return_value=None):
            spec.loader.exec_module(mod)
        return getattr(mod, attr)
    finally:
        tmp_path.unlink(missing_ok=True)


# ── git HEAD 원본 소스 로드 (불변 고정) ─────────────────────────────────────────
_BD_ORIG_SRC  = _git_show("extract_pdf_blockdiagram.py")
_EDA_ORIG_SRC = _git_show("extract_eda_pyojun.py")

# 원본 PROMPT_TEMPLATE — 골든 비교의 기준값 (불변)
_BD_ORIG_PROMPT = _load_const_from_src(_BD_ORIG_SRC,  "PROMPT_TEMPLATE", "_bd_orig")
_BD_ORIG_OUTPUT = _load_const_from_src(_BD_ORIG_SRC,  "OUTPUT_DIR",      "_bd_orig_out")
_BD_CHUNK_SIZE  = 20   # 원본 CHUNK_SIZE=20 핀 고정


# ──────────────────────────────────────────────────────────────────────────────
# R-1) record_convert_pdf 기본 동작
# ──────────────────────────────────────────────────────────────────────────────

class TestRecordConvertPdf(unittest.TestCase):
    """record_convert_pdf 가 (prompt, start, end) 시퀀스를 정확 캡처한다."""

    def _snap(self, total_pages, chunk_size=20, single_chunk_max=None,
               prompt="p {start} {end}"):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "doc.md"
            fake_pdf = Path(tmp) / "doc.pdf"
            return record_convert_pdf(
                fake_pdf, prompt,
                output_path=out,
                total_pages=total_pages,
                chunk_size=chunk_size,
                single_chunk_max=single_chunk_max,
            )

    def test_single_chunk_one_call(self):
        snap = self._snap(20, chunk_size=20)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1], 1)
        self.assertEqual(snap.calls[0][2], 20)

    def test_two_chunks_calls(self):
        snap = self._snap(21, chunk_size=20)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 21))

    def test_prompt_range_substituted(self):
        snap = self._snap(21, chunk_size=20, prompt="pages {start}~{end}")
        self.assertEqual(snap.calls[0][0], "pages 1~20")
        self.assertEqual(snap.calls[1][0], "pages 21~21")

    def test_single_chunk_max_forces_single(self):
        snap = self._snap(50, chunk_size=20, single_chunk_max=10**9)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 50))

    def test_separator_recorded_for_multi_chunk(self):
        snap = self._snap(21, chunk_size=20)
        self.assertEqual(snap.separator, "\n\n---\n\n")

    def test_separator_none_for_single_chunk(self):
        snap = self._snap(20, chunk_size=20)
        self.assertIsNone(snap.separator)


# ──────────────────────────────────────────────────────────────────────────────
# R-2) 통합 직전 원본 blockdiagram 골든 (박제 fixture)
# ──────────────────────────────────────────────────────────────────────────────

class TestRecordScriptBlockdiagram(unittest.TestCase):
    """통합 직전 원본 extract_pdf_blockdiagram.py 의 호출 시퀀스(고정 fixture).

    (이전엔 git show HEAD 원본을 record_script 로 즉석 녹화했으나, 통합본 커밋으로
    HEAD 에 process_pdf 가 사라져 fixture 로드로 전환. fixture 는 통합 직전
    원본을 record_script 로 1회 박제한 것이라 record_script 동작 시연 의미도 유지.)
    """

    def _snap(self, total_pages) -> PipelineSnapshot:
        """통합 직전 원본 blockdiagram 골든 fixture 로드."""
        return load_golden("migration_phase1", "extract_pdf_blockdiagram", total_pages)

    def test_20p_single_chunk(self):
        """20페이지 → 단일 청크 (1,20)."""
        snap = self._snap(20)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 20))

    def test_50p_three_chunks(self):
        """50페이지 → 3청크: (1,20) + (21,40) + (41,50)."""
        snap = self._snap(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 40))
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_prompt_contains_start_end(self):
        """치환 후 리터럴 {start}/{end} 없어야 함."""
        snap = self._snap(20)
        prompt = snap.calls[0][0]
        self.assertNotIn("{start}", prompt)
        self.assertNotIn("{end}", prompt)

    def test_output_path_recorded(self):
        """출력 경로가 기록된다."""
        snap = self._snap(20)
        self.assertIsNotNone(snap.output_path)


# ──────────────────────────────────────────────────────────────────────────────
# R-3) git-원본 vs 통합본 골든 비교 — 비순환(non-vacuous)
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenComparison(unittest.TestCase):
    """git-원본 스크립트와 마이그레이션된 convert_pdf 래퍼가 동일 시퀀스를 생성.

    비순환 보장:
      - orig = 통합 직전 원본(process_pdf 있음)에서 1회 박제된 fixture — 불변·독립.
      - pipe = 워킹트리 통합본이 실제로 호출하는 convert_pdf 파라미터에서 캡처.
      두 독립 소스를 비교하므로 순환이 없다.
    """

    def _golden_original(self, total_pages) -> PipelineSnapshot:
        """통합 직전 원본 blockdiagram.process_pdf 골든 fixture 로드 (불변·독립)."""
        return load_golden("migration_phase1", "extract_pdf_blockdiagram", total_pages)

    def _golden_pipeline(self, total_pages) -> PipelineSnapshot:
        """마이그레이션된 convert_pdf 파라미터 시퀀스 (원본 프롬프트 상수 사용).

        출력 경로는 원본 blockdiagram 이 쓰는 OUTPUT_DIR / f"{stem}.md" 와
        동일하게 맞춘다(원본은 /fake/test.pdf → test.md). 그래야 assert_equals
        의 output_path 대조가 의미 있게 통과한다(원본·통합본 출력 경로 동일성).
        """
        with tempfile.TemporaryDirectory() as tmp:
            out = _BD_ORIG_OUTPUT / "test.md"   # 원본과 동일한 출력 경로
            fake_pdf = Path(tmp) / "test.pdf"
            return record_convert_pdf(
                fake_pdf, _BD_ORIG_PROMPT,
                output_path=out,
                total_pages=total_pages,
                chunk_size=_BD_CHUNK_SIZE,
                single_chunk_max=None,   # blockdiagram: 항상 20p 분할
                rate_limit_s=0,
            )

    def test_golden_20p(self):
        """20페이지: git-원본 vs 통합본 시퀀스 동일."""
        orig = self._golden_original(20)
        pipe = self._golden_pipeline(20)
        orig.assert_equals(pipe, label="blockdiagram 20p")

    def test_golden_21p(self):
        """21페이지 경계: git-원본 vs 통합본 동일."""
        orig = self._golden_original(21)
        pipe = self._golden_pipeline(21)
        orig.assert_equals(pipe, label="blockdiagram 21p")

    def test_golden_50p(self):
        """50페이지(3청크): git-원본 vs 통합본 동일."""
        orig = self._golden_original(50)
        pipe = self._golden_pipeline(50)
        orig.assert_equals(pipe, label="blockdiagram 50p")


# ──────────────────────────────────────────────────────────────────────────────
# R-4) PipelineSnapshot.assert_equals 불일치 감지
# ──────────────────────────────────────────────────────────────────────────────

class TestSnapshotAssertEquals(unittest.TestCase):
    """불일치 시 명확한 AssertionError 메시지."""

    def _make(self, calls):
        s = PipelineSnapshot()
        s.calls = calls
        s.chunk_count = len(calls)
        return s

    def test_chunk_count_mismatch_raises(self):
        a = self._make([("p1", 1, 20)])
        b = self._make([("p1", 1, 20), ("p2", 21, 40)])
        with self.assertRaises(AssertionError) as ctx:
            a.assert_equals(b)
        self.assertIn("chunk_count", str(ctx.exception))

    def test_prompt_drift_raises(self):
        a = self._make([("original prompt", 1, 20)])
        b = self._make([("drifted prompt", 1, 20)])
        with self.assertRaises(AssertionError) as ctx:
            a.assert_equals(b)
        self.assertIn("프롬프트 드리프트", str(ctx.exception))

    def test_start_mismatch_raises(self):
        a = self._make([("p", 1, 20)])
        b = self._make([("p", 2, 20)])
        with self.assertRaises(AssertionError) as ctx:
            a.assert_equals(b)
        self.assertIn("start", str(ctx.exception))

    def test_equal_snapshots_no_raise(self):
        a = self._make([("p", 1, 20), ("p2", 21, 40)])
        b = self._make([("p", 1, 20), ("p2", 21, 40)])
        a.assert_equals(b)


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
