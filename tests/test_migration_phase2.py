"""test_migration_phase2.py — Phase 2 마이그레이션 골든 비교 테스트.

대상(중위험 3개): extract_ava2, extract_eda_pangdan, extract_pdf_image_analysis.
세 스크립트 모두 "≤25페이지 → 단일 청크 / 초과 → 20페이지 분할" 정책 +
sleep 15초 + on_failure=abort + 구분자 \\n\\n---\\n\\n 를 공유한다.

비순환(non-vacuous) 골든 원칙 (고정 fixture 방식 — Phase 1 과 동일):
  골든(원본 기준) = 통합 *직전* 원본 스크립트를 1회 박제한 JSON fixture
                    (tests/golden/migration_phase2/*.json).
  검증 대상      = 마이그레이션된 워킹트리 스크립트가 호출하는 convert_pdf 파라미터.
  두 독립 소스를 비교하므로 순환이 없다.

  ⚠️ 통합본이 HEAD 에 커밋된 뒤 HEAD 에는 process_pdf 가 없어 "git show HEAD →
     record_script" 가 불가하다. 통합 직전 원본을 박제한 fixture 로 전환했다
     (tests/_golden_fixtures.py 가 PRE_INTEGRATION_REF 원본에서 생성).

  fixture 생성 시 원본 진입점은 스크립트마다 달랐다:
    - ava2          : process_pdf 가 없고 청크 루프가 main() 안에 있다 →
                      record_script(entry_fn="main") (하드코딩 PDF_PATH 직접 사용).
    - eda_pangdan   : process_pdf(pdf_path) 보유 → record_script(entry_fn="process_pdf").
    - image_analysis: process_pdf(pdf_path) 보유 → record_script(entry_fn="process_pdf").
  세 원본 모두 청크 루프 내부에서 extract_chunk() 헬퍼를 호출했다.

  구체적으로:
    _golden_orig_*   : load_golden(...) → 박제 fixture(통합 직전 원본 시퀀스) 로드
    _golden_migrated_*: 워킹트리 통합본 스크립트의 main() 을 실제 실행해, 호출되는
                        convert_pdf 파라미터(프롬프트·청크정책·출력경로)를 캡처
                        → record_migrated_script (청크정책을 소스에서 직접 읽어 teeth 강화)
  → "워킹트리 vs 워킹트리" 순환이 완전히 제거된다.

검증 항목 (스크립트별):
  ava2          : 25(단일)/26(분할)/40페이지 — ≤25 경계 + main() 진입(하드코딩 PDF)
  eda_pangdan   : 25(단일)/26(분할)/40페이지 — ≤25 경계 + process_pdf 진입
  image_analysis: 25(단일)/26(분할)/50페이지 — ≤25 경계 + CLI 인자 PDF
  공통          : 프롬프트 byte-동일, import 크래시 없음, convert_pdf 사용 확인,
                  단일/다중 청크 구분자(separator) + 출력 경로(output_path) 대조,
                  sleep=15 보존

실행:
    .venv/bin/python -m pytest tests/test_migration_phase2.py -v
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
    PipelineSnapshot, record_convert_pdf, record_migrated_script,
)
from tests._golden_fixtures import PRE_INTEGRATION_REF, SCRIPT_ARGV, load_golden

# ── 대상 스크립트 경로 ──────────────────────────────────────────────────────────
_AVA2_SCRIPT = Path(_ROOT) / "extract_ava2.py"
_PANGDAN_SCRIPT = Path(_ROOT) / "extract_eda_pangdan.py"
_IMG_SCRIPT = Path(_ROOT) / "extract_pdf_image_analysis.py"


# ──────────────────────────────────────────────────────────────────────────────
# git 원본 로드 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _git_show(rel_path: str) -> str:
    """git show <PRE_INTEGRATION_REF>:<rel_path> 로 통합 직전 원본 소스를 반환.

    ⚠️ HEAD 가 아니라 통합 *직전* 커밋(PRE_INTEGRATION_REF)을 본다. 통합본이
    HEAD 에 커밋된 뒤로는 HEAD==워킹트리라 HEAD 기준 byte-동일 비교가 vacuous 가
    되므로, 통합 직전 원본 상수를 기준으로 삼아 프롬프트 드리프트 teeth 를 유지한다.

    R11(2026-07-15): PRE_INTEGRATION_REF 가 현 저장소 이력에 없으면(이력 재작성으로
    객체 소실 — 재생성 불가) None 반환. 소비 테스트만 skipIf 로 명시 skip 하고,
    fixture(JSON) 기반 골든 비교는 git 무관하게 계속 수행한다.
    """
    r = subprocess.run(
        ["git", "show", f"{PRE_INTEGRATION_REF}:{rel_path}"],
        capture_output=True, text=True, cwd=_ROOT, check=False,
    )
    if r.returncode != 0:
        return None  # R11: 원본 커밋 소실 — 소비 테스트는 skipIf 처리
    return r.stdout


# R11: 원본 커밋 소실 시 skip 사유(소비 테스트 공통).
_R11_ORIG_SKIP = ("PRE_INTEGRATION_REF 소실(저장소 이력 재작성) — 통합 직전 원본 "
                  "대비 byte-비교 재현 불가(통합 시점 1회 완료된 역사적 게이트; "
                  "fixture 기반 골든 비교는 계속 수행) R11 2026-07-15")


def _load_const_from_src(src: str, attr: str, tag: str):
    """소스 문자열을 임시 파일로 저장 후 상수 추출 (mkdir 차단)."""
    if src is None:
        return None  # R11: 원본 커밋 소실(위 _git_show 참조)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as f:
        f.write(src)
        tmp = Path(f.name)
    try:
        spec = _ilu.spec_from_file_location(tag, tmp)
        mod = _ilu.module_from_spec(spec)
        with patch.object(Path, "mkdir", return_value=None):
            spec.loader.exec_module(mod)
        return getattr(mod, attr)
    finally:
        tmp.unlink(missing_ok=True)


def _load_const_from_file(script: Path, attr: str):
    """워킹트리 스크립트에서 상수 추출 (mkdir 차단)."""
    spec = _ilu.spec_from_file_location(f"_wt2_{script.stem}", script)
    mod = _ilu.module_from_spec(spec)
    with patch.object(Path, "mkdir", return_value=None):
        spec.loader.exec_module(mod)
    return getattr(mod, attr)


# ── git HEAD 원본 소스 (불변 골든 기준) ─────────────────────────────────────────
_AVA2_ORIG_SRC = _git_show("extract_ava2.py")
_PANGDAN_ORIG_SRC = _git_show("extract_eda_pangdan.py")
_IMG_ORIG_SRC = _git_show("extract_pdf_image_analysis.py")

# 원본 상수 (골든 캡처에 사용)
_AVA2_ORIG_PROMPT = _load_const_from_src(_AVA2_ORIG_SRC, "PROMPT_TEMPLATE", "_ava2_orig")
_PANGDAN_ORIG_PROMPT = _load_const_from_src(_PANGDAN_ORIG_SRC, "PROMPT_TEMPLATE", "_pangdan_orig")
_IMG_ORIG_PROMPT = _load_const_from_src(_IMG_ORIG_SRC, "PROMPT_TEMPLATE", "_img_orig")

# 워킹트리(마이그레이션된) 상수 — 프롬프트 byte-동일 비교용.
# (출력경로는 _golden_migrated_* 가 통합본 main() 을 실제 실행해 직접 캡처한다.
#  _PANGDAN_WT_OUTDIR 만 output_path 변이 teeth 테스트에서 사용.)
_AVA2_WT_PROMPT = _load_const_from_file(_AVA2_SCRIPT, "PROMPT_TEMPLATE")
_PANGDAN_WT_PROMPT = _load_const_from_file(_PANGDAN_SCRIPT, "PROMPT_TEMPLATE")
_PANGDAN_WT_OUTDIR = _load_const_from_file(_PANGDAN_SCRIPT, "OUTPUT_DIR")
_IMG_WT_PROMPT = _load_const_from_file(_IMG_SCRIPT, "PROMPT_TEMPLATE")


# ──────────────────────────────────────────────────────────────────────────────
# 골든 캡처 헬퍼 — 원본(통합 직전 fixture 로드)
#   (이전엔 git show HEAD 로 동적 녹화했으나, 통합본 커밋으로 HEAD 에서
#    process_pdf/extract_chunk 가 사라져 박제 fixture 로 전환.)
# ──────────────────────────────────────────────────────────────────────────────

def _golden_orig_ava2(total_pages: int) -> PipelineSnapshot:
    """통합 직전 원본 ava2.main() 골든 — 고정 fixture 로드.

    원본 ava2 는 process_pdf 가 없고 청크 루프가 main() 안에 있다(fixture 생성 시
    entry_fn="main" 으로 녹화됨). 출력은 하드코딩 AVA_2.md.
    """
    return load_golden("migration_phase2", "extract_ava2", total_pages)


def _golden_orig_pangdan(total_pages: int) -> PipelineSnapshot:
    """통합 직전 원본 eda_pangdan.process_pdf 골든 — 고정 fixture 로드."""
    return load_golden("migration_phase2", "extract_eda_pangdan", total_pages)


def _golden_orig_img(total_pages: int) -> PipelineSnapshot:
    """통합 직전 원본 image_analysis.process_pdf 골든 — 고정 fixture 로드."""
    return load_golden("migration_phase2", "extract_pdf_image_analysis", total_pages)


# ──────────────────────────────────────────────────────────────────────────────
# 골든 캡처 헬퍼 — 마이그레이션본 (통합본 main() 실행 → 실제 convert_pdf 인자)
#   파라미터를 하드코딩하지 않고 스크립트 소스에서 직접 읽으므로, 프롬프트/청크정책/
#   출력경로/rate_limit 이 원본과 어긋나면 골든 비교가 실패한다(teeth 강화).
# ──────────────────────────────────────────────────────────────────────────────

def _run_migrated(script: Path, stem: str, total_pages: int) -> PipelineSnapshot:
    """통합본 스크립트 main() 을 실제 실행해 convert_pdf 호출 인자를 녹화."""
    argv = SCRIPT_ARGV.get(stem)
    saved = sys.argv
    try:
        if argv is not None:
            sys.argv = list(argv)
        return record_migrated_script(script, "main", (), total_pages=total_pages)
    finally:
        sys.argv = saved


def _golden_migrated_ava2(total_pages: int) -> PipelineSnapshot:
    """마이그레이션된 ava2 실제 convert_pdf 호출 시퀀스 (소스에서 직접)."""
    return _run_migrated(_AVA2_SCRIPT, "extract_ava2", total_pages)


def _golden_migrated_pangdan(total_pages: int) -> PipelineSnapshot:
    """마이그레이션된 eda_pangdan 실제 convert_pdf 호출 시퀀스 (소스에서 직접)."""
    return _run_migrated(_PANGDAN_SCRIPT, "extract_eda_pangdan", total_pages)


def _golden_migrated_img(total_pages: int) -> PipelineSnapshot:
    """마이그레이션된 image_analysis 실제 convert_pdf 호출 시퀀스 (소스에서 직접)."""
    return _run_migrated(_IMG_SCRIPT, "extract_pdf_image_analysis", total_pages)


# ──────────────────────────────────────────────────────────────────────────────
# ava2 — git-원본 vs 통합본 골든 비교 (≤25 경계 필수)
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenAva2(unittest.TestCase):
    """git-원본 ava2 main() 시퀀스 == 마이그레이션된 single_chunk_max=25."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_ava2(total_pages)
        migr = _golden_migrated_ava2(total_pages)
        orig.assert_equals(migr, label=f"ava2 {total_pages}p")

    def test_golden_25p_single(self):
        """≤25페이지: 단일 청크 시퀀스 동일."""
        self._compare(25)

    def test_golden_26p_split(self):
        """26페이지: 분할 시퀀스 동일."""
        self._compare(26)

    def test_golden_40p(self):
        """40페이지: (1,20)+(21,40) 동일."""
        self._compare(40)

    def test_orig_25p_single_chunk(self):
        """원본: ≤25 → 단일 청크 (1,25)."""
        snap = _golden_orig_ava2(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_orig_26p_split(self):
        """원본: 26 → (1,20)+(21,26)."""
        snap = _golden_orig_ava2(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_migrated_25p_single_chunk(self):
        """통합본: ≤25 → 단일 청크 (1,25)."""
        snap = _golden_migrated_ava2(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_migrated_26p_split(self):
        """통합본: 26 → (1,20)+(21,26)."""
        snap = _golden_migrated_ava2(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_prompt_no_placeholders(self):
        """치환 후 리터럴 {start}/{end} 없어야 함."""
        snap = _golden_migrated_ava2(26)
        for prompt, _, _ in snap.calls:
            self.assertNotIn("{start}", prompt)
            self.assertNotIn("{end}", prompt)

    def test_prompt_verbatim_content(self):
        snap = _golden_migrated_ava2(25)
        self.assertIn("OCR", snap.calls[0][0])
        self.assertIn("GFM pipe", snap.calls[0][0])

    def test_separator_25p_none(self):
        snap = _golden_migrated_ava2(25)
        self.assertIsNone(snap.separator)

    def test_separator_26p_present(self):
        snap = _golden_migrated_ava2(26)
        self.assertEqual(snap.separator, "\n\n---\n\n")


# ──────────────────────────────────────────────────────────────────────────────
# eda_pangdan — git-원본 vs 통합본 골든 비교 (≤25 경계 필수)
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenEdaPangdan(unittest.TestCase):
    """git-원본 eda_pangdan process_pdf 시퀀스 == 마이그레이션된 single_chunk_max=25."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_pangdan(total_pages)
        migr = _golden_migrated_pangdan(total_pages)
        orig.assert_equals(migr, label=f"eda_pangdan {total_pages}p")

    def test_golden_25p_single(self):
        self._compare(25)

    def test_golden_26p_split(self):
        self._compare(26)

    def test_golden_40p(self):
        self._compare(40)

    def test_orig_25p_single_chunk(self):
        snap = _golden_orig_pangdan(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_orig_26p_split(self):
        snap = _golden_orig_pangdan(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_migrated_25p_single_chunk(self):
        snap = _golden_migrated_pangdan(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_migrated_26p_split(self):
        snap = _golden_migrated_pangdan(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_prompt_verbatim_content(self):
        snap = _golden_migrated_pangdan(25)
        self.assertIn("OCR", snap.calls[0][0])
        self.assertIn("리스크", snap.calls[0][0])

    def test_separator_25p_none(self):
        snap = _golden_migrated_pangdan(25)
        self.assertIsNone(snap.separator)

    def test_separator_26p_present(self):
        snap = _golden_migrated_pangdan(26)
        self.assertEqual(snap.separator, "\n\n---\n\n")


# ──────────────────────────────────────────────────────────────────────────────
# image_analysis — git-원본 vs 통합본 골든 비교 (≤25 경계 필수)
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenImageAnalysis(unittest.TestCase):
    """git-원본 image_analysis process_pdf 시퀀스 == 마이그레이션된 single_chunk_max=25."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_img(total_pages)
        migr = _golden_migrated_img(total_pages)
        orig.assert_equals(migr, label=f"image_analysis {total_pages}p")

    def test_golden_25p_single(self):
        self._compare(25)

    def test_golden_26p_split(self):
        self._compare(26)

    def test_golden_50p(self):
        """50페이지: (1,20)+(21,40)+(41,50) 동일."""
        self._compare(50)

    def test_orig_25p_single_chunk(self):
        snap = _golden_orig_img(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_orig_26p_split(self):
        snap = _golden_orig_img(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_orig_50p_three_chunks(self):
        snap = _golden_orig_img(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 40))
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_migrated_25p_single_chunk(self):
        snap = _golden_migrated_img(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_migrated_26p_split(self):
        snap = _golden_migrated_img(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_migrated_50p_three_chunks(self):
        snap = _golden_migrated_img(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_prompt_verbatim_content(self):
        snap = _golden_migrated_img(25)
        self.assertIn("OCR", snap.calls[0][0])
        self.assertIn("UML", snap.calls[0][0])

    def test_separator_25p_none(self):
        snap = _golden_migrated_img(25)
        self.assertIsNone(snap.separator)

    def test_separator_26p_present(self):
        snap = _golden_migrated_img(26)
        self.assertEqual(snap.separator, "\n\n---\n\n")


# ──────────────────────────────────────────────────────────────────────────────
# 프롬프트 byte-동일 — 원본 git 상수 vs 워킹트리 상수 직접 비교
# ──────────────────────────────────────────────────────────────────────────────

@unittest.skipIf(_AVA2_ORIG_SRC is None or _PANGDAN_ORIG_SRC is None
                 or _IMG_ORIG_SRC is None, _R11_ORIG_SKIP)
class TestPromptByteIdentical(unittest.TestCase):
    """마이그레이션 후 워킹트리 PROMPT_TEMPLATE 이 git-원본과 byte-동일해야 한다."""

    def test_ava2_prompt_identical(self):
        self.assertEqual(
            _AVA2_WT_PROMPT, _AVA2_ORIG_PROMPT,
            "ava2 PROMPT_TEMPLATE 이 git-원본과 다름 (프롬프트 드리프트)"
        )

    def test_pangdan_prompt_identical(self):
        self.assertEqual(
            _PANGDAN_WT_PROMPT, _PANGDAN_ORIG_PROMPT,
            "eda_pangdan PROMPT_TEMPLATE 이 git-원본과 다름 (프롬프트 드리프트)"
        )

    def test_image_analysis_prompt_identical(self):
        self.assertEqual(
            _IMG_WT_PROMPT, _IMG_ORIG_PROMPT,
            "image_analysis PROMPT_TEMPLATE 이 git-원본과 다름 (프롬프트 드리프트)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# sleep(rate_limit) 값 보존 — 원본 15초 == 통합본 15초
# ──────────────────────────────────────────────────────────────────────────────

class TestRateLimitPreserved(unittest.TestCase):
    """원본 스크립트 sleep(15) 가 마이그레이션 호출의 rate_limit_s=15 로 보존됨.

    원본 소스에 `time.sleep(15)` 리터럴이, 통합본 소스에 `rate_limit_s=15` 가
    존재함을 정적으로 확인한다 (convert_pdf 내부 sleep 은 recorder 가 mock).
    """

    @unittest.skipIf(_AVA2_ORIG_SRC is None, _R11_ORIG_SKIP)
    def test_ava2_orig_sleep_15(self):
        self.assertIn("time.sleep(15)", _AVA2_ORIG_SRC)

    @unittest.skipIf(_PANGDAN_ORIG_SRC is None, _R11_ORIG_SKIP)
    def test_pangdan_orig_sleep_15(self):
        self.assertIn("time.sleep(15)", _PANGDAN_ORIG_SRC)

    @unittest.skipIf(_IMG_ORIG_SRC is None, _R11_ORIG_SKIP)
    def test_image_analysis_orig_sleep_15(self):
        self.assertIn("time.sleep(15)", _IMG_ORIG_SRC)

    def test_ava2_migrated_rate_limit_15(self):
        src = _AVA2_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("rate_limit_s=15", src)

    def test_pangdan_migrated_rate_limit_15(self):
        src = _PANGDAN_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("rate_limit_s=15", src)

    def test_image_analysis_migrated_rate_limit_15(self):
        src = _IMG_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("rate_limit_s=15", src)


# ──────────────────────────────────────────────────────────────────────────────
# import 크래시 없음 + convert_pdf 사용 확인 + abort 정책 보존
# ──────────────────────────────────────────────────────────────────────────────

class TestMigratedScriptHealth(unittest.TestCase):
    """통합본 스크립트 기본 건강 검사."""

    def _load(self, script: Path):
        spec = _ilu.spec_from_file_location(f"_health2_{script.stem}", script)
        mod = _ilu.module_from_spec(spec)
        with patch.object(Path, "mkdir", return_value=None):
            spec.loader.exec_module(mod)
        return mod

    def test_ava2_import_no_crash(self):
        mod = self._load(_AVA2_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "PDF_PATH"))
        self.assertTrue(hasattr(mod, "OUTPUT_DIR"))
        self.assertTrue(hasattr(mod, "OUTPUT_PATH"))
        self.assertTrue(hasattr(mod, "main"))

    def test_pangdan_import_no_crash(self):
        mod = self._load(_PANGDAN_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "PDF_PATH"))
        self.assertTrue(hasattr(mod, "OUTPUT_DIR"))
        self.assertTrue(hasattr(mod, "main"))

    def test_image_analysis_import_no_crash(self):
        mod = self._load(_IMG_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "OUTPUT_DIR"))
        self.assertTrue(hasattr(mod, "main"))

    def test_ava2_uses_convert_pdf(self):
        src = _AVA2_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src, "convert_pdf 미사용")
        self.assertNotIn("def process_pdf", src, "원본 process_pdf 잔존")
        self.assertNotIn("def extract_chunk", src, "원본 extract_chunk 잔존")
        self.assertIn('on_failure="abort"', src, "abort 정책 미보존")

    def test_pangdan_uses_convert_pdf(self):
        src = _PANGDAN_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src, "convert_pdf 미사용")
        self.assertNotIn("def process_pdf", src, "원본 process_pdf 잔존")
        self.assertNotIn("def extract_chunk", src, "원본 extract_chunk 잔존")
        self.assertIn('on_failure="abort"', src, "abort 정책 미보존")

    def test_image_analysis_uses_convert_pdf(self):
        src = _IMG_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src, "convert_pdf 미사용")
        self.assertNotIn("def process_pdf", src, "원본 process_pdf 잔존")
        self.assertNotIn("def extract_chunk", src, "원본 extract_chunk 잔존")
        self.assertIn('on_failure="abort"', src, "abort 정책 미보존")

    def test_ava2_output_path_literal_preserved(self):
        """ava2 출력 파일명은 원본과 동일하게 AVA_2.md (literal) 유지."""
        mod = self._load(_AVA2_SCRIPT)
        self.assertEqual(mod.OUTPUT_PATH.name, "AVA_2.md")


# ──────────────────────────────────────────────────────────────────────────────
# 출력 경로(output_path)·구분자(separator) teeth — 원본 vs 통합본 직접 대조
#   (assert_equals 가 내부 비교하지만, teeth 가 "물린다"는 것을 명시 테스트로 고정)
# ──────────────────────────────────────────────────────────────────────────────

def _norm_out(p) -> str:
    """R11(2026-07-15): 머신 무관 output 경로 정규화 — 'output' 컴포넌트부터 상대화.

    골든 fixture 는 캡처 머신의 절대 경로(/Users/heni/workspace/filestomdwgem/...)
    를 박제하고 있어 다른 머신/레포명에서는 워크스페이스 접두만 달라 순수 환경
    차이로 실패했다(이식 부채). 드리프트 teeth 의 실체 = output/ 이하 구조·파일명.
    _pipeline_recorder.PipelineSnapshot.assert_equals 의 R11 정규화와 동일 규칙."""
    parts = Path(p).parts
    return str(Path(*parts[parts.index("output"):])) if "output" in parts else str(p)


class TestOutputPathSeparatorTeeth(unittest.TestCase):
    """원본 record_script 가 쓴 경로 == 통합본 convert_pdf 가 쓴 경로.

    원본은 각자의 OUTPUT_DIR / 파일명 으로, 통합본은 호출자가 넘긴 output_path 로
    저장한다. 두 경로가 (output/ 이하) 동일해야 출력 위치 드리프트가 없음을 보장.
    또한 단일/다중 청크 구분자(None vs '\\n\\n---\\n\\n')도 일치해야 한다.
    (R11: byte-동일 → 머신 무관 정규화 비교 — _norm_out 참조.)
    """

    def test_ava2_output_path_matches_orig(self):
        orig = _golden_orig_ava2(25)
        migr = _golden_migrated_ava2(25)
        self.assertIsNotNone(orig.output_path)
        self.assertIsNotNone(migr.output_path)
        self.assertEqual(_norm_out(orig.output_path), _norm_out(migr.output_path))
        self.assertEqual(orig.output_path.name, "AVA_2.md")

    def test_pangdan_output_path_matches_orig(self):
        orig = _golden_orig_pangdan(25)
        migr = _golden_migrated_pangdan(25)
        # 양쪽 모두 하드코딩 PDF_PATH(판단서) 의 stem 으로 저장 → 동일.
        self.assertEqual(_norm_out(orig.output_path), _norm_out(migr.output_path))
        self.assertTrue(orig.output_path.name.endswith("판단서.md"),
                        f"예상치 못한 출력명: {orig.output_path.name}")

    def test_image_analysis_output_path_matches_orig(self):
        orig = _golden_orig_img(25)
        migr = _golden_migrated_img(25)
        self.assertEqual(_norm_out(orig.output_path), _norm_out(migr.output_path))
        self.assertEqual(orig.output_path.name, "img.md")

    def test_separator_matches_orig_single_and_multi(self):
        # 단일 청크(25p): 양쪽 separator None
        self.assertEqual(
            _golden_orig_pangdan(25).separator,
            _golden_migrated_pangdan(25).separator,
        )
        self.assertIsNone(_golden_orig_pangdan(25).separator)
        # 다중 청크(26p): 양쪽 separator '\n\n---\n\n'
        self.assertEqual(
            _golden_orig_img(26).separator,
            _golden_migrated_img(26).separator,
        )
        self.assertEqual(_golden_orig_img(26).separator, "\n\n---\n\n")

    def test_teeth_bite_on_output_path_mutation(self):
        """output_path 가 달라지면 assert_equals 가 실패해야 한다(teeth 자가검증)."""
        orig = _golden_orig_pangdan(25)
        with tempfile.TemporaryDirectory() as tmp:
            fake_pdf = Path(tmp) / "pangdan.pdf"
            bad_out = _PANGDAN_WT_OUTDIR / "WRONG_NAME.md"  # 의도적 변이
            migr_bad = record_convert_pdf(
                fake_pdf, _PANGDAN_WT_PROMPT, output_path=bad_out,
                total_pages=25, chunk_size=20, single_chunk_max=25,
                rate_limit_s=15.0,
            )
        with self.assertRaises(AssertionError):
            orig.assert_equals(migr_bad, label="mutation output_path")

    def test_teeth_bite_on_separator_mutation(self):
        """separator 가 달라지면 assert_equals 가 실패해야 한다(teeth 자가검증)."""
        orig = _golden_orig_img(26)
        migr = _golden_migrated_img(26)
        migr.separator = "\n\nXXX\n\n"  # 의도적 변이
        with self.assertRaises(AssertionError):
            orig.assert_equals(migr, label="mutation separator")


# ──────────────────────────────────────────────────────────────────────────────
# Info-1 — 0페이지(degenerate) PDF 동작 발산 특성화(characterization)
#   원본 스크립트: chunk_size=0 → range(1,1,0) → ValueError
#   통합본 _build_chunks(0,20,25): single_chunk_max 경로로 [(1,0)] 반환
#   실무 영향 없음(count_pdf_pages 는 실제 PDF 에서 0 을 반환하지 않음).
#   shared 모듈 동작을 바꾸지 않고, 알려진 발산을 "의도된 것"으로 고정한다.
# ──────────────────────────────────────────────────────────────────────────────

class TestZeroPageDivergenceCharacterization(unittest.TestCase):
    """0페이지 입력의 알려진 동작 발산을 명시적으로 고정(문서화 테스트).

    이는 회귀 알림용이다: 만약 향후 한쪽 동작이 바뀌면 이 테스트가 깨져
    "발산이 바뀌었다"는 사실을 드러낸다. 실무 PDF 에는 도달 불가한 경로다.
    """

    def test_unified_build_chunks_zero_pages_returns_single_degenerate(self):
        from fmdw.pdf_pipeline import _build_chunks
        # single_chunk_max=25 경로: 0 <= 25 → [(1, 0)] (degenerate, 빈 범위)
        self.assertEqual(_build_chunks(0, 20, 25), [(1, 0)])

    def test_orig_zero_pages_raises_valueerror(self):
        """원본 패턴 range(1, total+1, chunk_size) 은 chunk_size=0 에서 ValueError."""
        total_pages = 0
        chunk_size = total_pages if total_pages <= 25 else 20  # 원본 ava2/pangdan/img 식
        self.assertEqual(chunk_size, 0)
        with self.assertRaises(ValueError):
            list(range(1, total_pages + 1, chunk_size))


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
