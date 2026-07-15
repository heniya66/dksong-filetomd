"""test_migration_phase1.py — Phase 1 마이그레이션 골든 비교 테스트.

비순환(non-vacuous) 골든 원칙 (고정 fixture 방식):
  골든(원본 기준) = 통합 *직전* 원본 스크립트(process_pdf 있음)를 1회 박제한
                    JSON fixture (tests/golden/migration_phase1/*.json).
  검증 대상      = 마이그레이션된 워킹트리 스크립트가 호출하는 convert_pdf 파라미터.
  두 독립 소스를 비교하므로 순환이 없다.

  ⚠️ 통합본이 HEAD 에 커밋된 뒤 HEAD 의 extract_*.py 에는 process_pdf 가 없다.
     그래서 "git show HEAD → record_script" 방식은 더 이상 불가하며, 통합 직전
     원본을 박제한 fixture 로 전환했다(tests/_golden_fixtures.py).

  구체적으로:
    _golden_orig_*   : load_golden(...) → 박제 fixture(통합 직전 원본 시퀀스) 로드
    _golden_migrated_*: 워킹트리 스크립트의 PROMPT_TEMPLATE + convert_pdf 파라미터
                        → record_convert_pdf

  이로써 "워킹트리 vs 워킹트리" 순환이 완전히 제거된다.

검증 항목:
  blockdiagram: 20/21/50페이지 — 청크 시퀀스·프롬프트·구분자
  eda_pyojun  : 25(단일)/26(분할)/40페이지 — ≤25 경계 필수
  공통        : import 크래시 없음, convert_pdf 사용 확인

실행:
    .venv/bin/python -m pytest tests/test_migration_phase1.py -v
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
_BD_SCRIPT  = Path(_ROOT) / "extract_pdf_blockdiagram.py"
_EDA_SCRIPT = Path(_ROOT) / "extract_eda_pyojun.py"


# ──────────────────────────────────────────────────────────────────────────────
# git 원본 로드 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _git_show(rel_path: str) -> str:
    """git show <PRE_INTEGRATION_REF>:<rel_path> 로 통합 직전 원본 소스를 반환.

    ⚠️ HEAD 가 아니라 통합 *직전* 커밋(PRE_INTEGRATION_REF)을 본다.
    Phase 1/2 통합본이 HEAD 에 커밋되면서 원본 PROMPT_TEMPLATE 가 HEAD==워킹트리가
    되어버려, HEAD 기준 byte-동일 비교는 vacuous(자기 비교)가 된다. 통합 직전
    원본 상수를 기준으로 삼아야 프롬프트 드리프트 검출 teeth 가 유지된다.

    R11(2026-07-15): PRE_INTEGRATION_REF 가 현 저장소 이력에 없으면(이력 재작성으로
    객체 소실 — 재생성 불가) None 을 반환한다. 구(舊)구현은 check=True 라 수집
    (collection) 단계에서 CalledProcessError 로 파일 전체가 죽었다 → 소비 테스트만
    skipIf 로 명시 skip 하고, fixture(JSON) 기반 골든 비교는 git 무관하게 계속 수행.
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
        mod  = _ilu.module_from_spec(spec)
        with patch.object(Path, "mkdir", return_value=None):
            spec.loader.exec_module(mod)
        return getattr(mod, attr)
    finally:
        tmp.unlink(missing_ok=True)


def _load_const_from_file(script: Path, attr: str):
    """워킹트리 스크립트에서 상수 추출 (mkdir 차단)."""
    spec = _ilu.spec_from_file_location(f"_wt_{script.stem}", script)
    mod  = _ilu.module_from_spec(spec)
    with patch.object(Path, "mkdir", return_value=None):
        spec.loader.exec_module(mod)
    return getattr(mod, attr)


# ── git HEAD 원본 소스 (불변 골든 기준) ─────────────────────────────────────────
_BD_ORIG_SRC   = _git_show("extract_pdf_blockdiagram.py")
_EDA_ORIG_SRC  = _git_show("extract_eda_pyojun.py")

# 원본 상수 (골든 캡처에 사용)
_BD_ORIG_PROMPT    = _load_const_from_src(_BD_ORIG_SRC,  "PROMPT_TEMPLATE", "_bd_orig")
_BD_ORIG_OUTPUT    = _load_const_from_src(_BD_ORIG_SRC,  "OUTPUT_DIR",      "_bd_orig2")
_EDA_ORIG_PROMPT   = _load_const_from_src(_EDA_ORIG_SRC, "PROMPT_TEMPLATE", "_eda_orig")
_EDA_ORIG_OUTPUT   = _load_const_from_src(_EDA_ORIG_SRC, "OUTPUT_DIR",      "_eda_orig2")

# 워킹트리(마이그레이션된) 상수 — 프롬프트 byte-동일 비교용.
# (출력경로는 _golden_migrated_* 가 통합본 main() 을 실제 실행해 직접 캡처하므로
#  여기서 OUTPUT_DIR 상수를 따로 로드하지 않는다.)
_BD_WT_PROMPT  = _load_const_from_file(_BD_SCRIPT,  "PROMPT_TEMPLATE")
_EDA_WT_PROMPT = _load_const_from_file(_EDA_SCRIPT, "PROMPT_TEMPLATE")


# ──────────────────────────────────────────────────────────────────────────────
# 골든 캡처 헬퍼
# ──────────────────────────────────────────────────────────────────────────────

def _golden_orig_bd(total_pages: int) -> PipelineSnapshot:
    """통합 직전 원본 blockdiagram.process_pdf 골든 — 고정 fixture 로드.

    (이전엔 git show HEAD 로 동적 녹화했으나, 통합본 커밋으로 HEAD 에
    process_pdf 가 사라져 박제 fixture 로 전환.)
    """
    return load_golden("migration_phase1", "extract_pdf_blockdiagram", total_pages)


def _golden_orig_eda(total_pages: int) -> PipelineSnapshot:
    """통합 직전 원본 eda_pyojun.process_pdf 골든 — 고정 fixture 로드."""
    return load_golden("migration_phase1", "extract_eda_pyojun", total_pages)


def _run_migrated(script: Path, stem: str, total_pages: int) -> PipelineSnapshot:
    """통합본 스크립트 main() 을 실제 실행해 convert_pdf 호출 인자를 녹화.

    프롬프트·청크정책(chunk_size/single_chunk_max)·출력경로·rate_limit 을
    스크립트 소스에서 직접 읽으므로(하드코딩 아님), 스크립트가 원본과 어긋나면
    골든 비교가 실패한다(teeth 강화). CLI(argv) 스크립트는 fixture 와 동일 argv 주입.
    """
    argv = SCRIPT_ARGV.get(stem)
    saved = sys.argv
    try:
        if argv is not None:
            sys.argv = list(argv)
        return record_migrated_script(script, "main", (), total_pages=total_pages)
    finally:
        sys.argv = saved


def _golden_migrated_bd(total_pages: int) -> PipelineSnapshot:
    """마이그레이션된 blockdiagram 실제 convert_pdf 호출 시퀀스 (소스에서 직접)."""
    return _run_migrated(_BD_SCRIPT, "extract_pdf_blockdiagram", total_pages)


def _golden_migrated_eda(total_pages: int) -> PipelineSnapshot:
    """마이그레이션된 eda_pyojun 실제 convert_pdf 호출 시퀀스 (소스에서 직접)."""
    return _run_migrated(_EDA_SCRIPT, "extract_eda_pyojun", total_pages)


# ──────────────────────────────────────────────────────────────────────────────
# blockdiagram — git-원본 vs 통합본 골든 비교
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenBlockdiagram(unittest.TestCase):
    """git-원본 process_pdf 시퀀스 == 마이그레이션된 convert_pdf 시퀀스."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_bd(total_pages)
        migr = _golden_migrated_bd(total_pages)
        orig.assert_equals(migr, label=f"blockdiagram {total_pages}p")

    def test_golden_20p(self):
        """20페이지: 단일 청크 시퀀스 동일."""
        self._compare(20)

    def test_golden_21p(self):
        """21페이지: 경계값 — (1,20)+(21,21) 동일."""
        self._compare(21)

    def test_golden_50p(self):
        """50페이지: 3청크 시퀀스 동일."""
        self._compare(50)

    def test_chunk_ranges_20p(self):
        snap = _golden_orig_bd(20)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 20))

    def test_chunk_ranges_21p(self):
        snap = _golden_orig_bd(21)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 21))

    def test_chunk_ranges_50p(self):
        snap = _golden_orig_bd(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_prompt_no_placeholders(self):
        """치환 후 리터럴 {start}/{end} 없어야 함."""
        snap = _golden_migrated_bd(21)
        for prompt, _, _ in snap.calls:
            self.assertNotIn("{start}", prompt)
            self.assertNotIn("{end}", prompt)

    def test_prompt_verbatim_content(self):
        """핵심 문자열이 실제 호출 프롬프트에 포함 (byte-동일 확인)."""
        snap = _golden_migrated_bd(20)
        self.assertIn("블록도 분석", snap.calls[0][0])
        self.assertIn("GFM pipe format", snap.calls[0][0])

    def test_separator_multi_chunk(self):
        snap = _golden_migrated_bd(21)
        self.assertEqual(snap.separator, "\n\n---\n\n")

    def test_separator_single_chunk(self):
        snap = _golden_migrated_bd(20)
        self.assertIsNone(snap.separator)


# ──────────────────────────────────────────────────────────────────────────────
# eda_pyojun — git-원본 vs 통합본 골든 비교 (≤25 경계 필수)
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenEdaPyojun(unittest.TestCase):
    """git-원본 ≤25 단일/26 분할 경계 == 마이그레이션된 single_chunk_max=25."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_eda(total_pages)
        migr = _golden_migrated_eda(total_pages)
        orig.assert_equals(migr, label=f"eda_pyojun {total_pages}p")

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
        snap = _golden_orig_eda(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_orig_26p_split(self):
        """원본: 26 → (1,20)+(21,26)."""
        snap = _golden_orig_eda(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_migrated_25p_single_chunk(self):
        """통합본: ≤25 → 단일 청크 (1,25)."""
        snap = _golden_migrated_eda(25)
        self.assertEqual(snap.chunk_count, 1)
        self.assertEqual(snap.calls[0][1:], (1, 25))

    def test_migrated_26p_split(self):
        """통합본: 26 → (1,20)+(21,26)."""
        snap = _golden_migrated_eda(26)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 26))

    def test_prompt_verbatim_content(self):
        snap = _golden_migrated_eda(25)
        self.assertIn("OCR", snap.calls[0][0])
        self.assertIn("GFM", snap.calls[0][0])

    def test_separator_25p_none(self):
        snap = _golden_migrated_eda(25)
        self.assertIsNone(snap.separator)

    def test_separator_26p_present(self):
        snap = _golden_migrated_eda(26)
        self.assertEqual(snap.separator, "\n\n---\n\n")


# ──────────────────────────────────────────────────────────────────────────────
# 프롬프트 byte-동일 — 원본 git 상수 vs 워킹트리 상수 직접 비교
# ──────────────────────────────────────────────────────────────────────────────

@unittest.skipIf(_BD_ORIG_SRC is None or _EDA_ORIG_SRC is None, _R11_ORIG_SKIP)
class TestPromptByteIdentical(unittest.TestCase):
    """마이그레이션 후 워킹트리 PROMPT_TEMPLATE 이 git-원본과 byte-동일해야 한다."""

    def test_blockdiagram_prompt_identical(self):
        self.assertEqual(
            _BD_WT_PROMPT, _BD_ORIG_PROMPT,
            "blockdiagram PROMPT_TEMPLATE 이 git-원본과 다름 (프롬프트 드리프트)"
        )

    def test_eda_pyojun_prompt_identical(self):
        self.assertEqual(
            _EDA_WT_PROMPT, _EDA_ORIG_PROMPT,
            "eda_pyojun PROMPT_TEMPLATE 이 git-원본과 다름 (프롬프트 드리프트)"
        )


# ──────────────────────────────────────────────────────────────────────────────
# import 크래시 없음 + convert_pdf 사용 확인
# ──────────────────────────────────────────────────────────────────────────────

class TestMigratedScriptHealth(unittest.TestCase):
    """통합본 스크립트 기본 건강 검사."""

    def _load(self, script: Path):
        spec = _ilu.spec_from_file_location(f"_health_{script.stem}", script)
        mod  = _ilu.module_from_spec(spec)
        with patch.object(Path, "mkdir", return_value=None):
            spec.loader.exec_module(mod)
        return mod

    def test_blockdiagram_import_no_crash(self):
        mod = self._load(_BD_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "OUTPUT_DIR"))
        self.assertTrue(hasattr(mod, "main"))

    def test_eda_pyojun_import_no_crash(self):
        mod = self._load(_EDA_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "PDF_PATH"))
        self.assertTrue(hasattr(mod, "OUTPUT_DIR"))
        self.assertTrue(hasattr(mod, "main"))

    def test_blockdiagram_uses_convert_pdf_not_process_pdf(self):
        src = _BD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src, "convert_pdf 미사용")
        self.assertNotIn("def process_pdf", src, "원본 process_pdf 잔존")
        self.assertNotIn("def extract_chunk", src, "원본 extract_chunk 잔존")

    def test_eda_pyojun_uses_convert_pdf_not_process_pdf(self):
        src = _EDA_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src, "convert_pdf 미사용")
        self.assertNotIn("def process_pdf", src, "원본 process_pdf 잔존")
        self.assertNotIn("def extract_chunk", src, "원본 extract_chunk 잔존")


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
