"""test_migration_phase3.py — Phase 3 마이그레이션 골든 비교 테스트.

대상(고위험 3개):
  - extract_ava1            : **항상 단일 청크**(페이지 수 무관 — 원본 보존 핵심).
  - extract_sim_platform    : 20p 단위 분할 + renumber_images post_process 훅.
  - extract_gemini_multimodal: 원본은 임시 _chunk_NNN.md 병합. convert_pdf 메모리
                               병합으로 대체하되 최종 {stem}.md byte 동일 보존.

비순환(non-vacuous) 골든 원칙 (고정 fixture 방식 — Phase 1/2 와 동일):
  골든(원본 기준) = Phase 3 통합 직전 HEAD(PRE_INTEGRATION_REF_PHASE3) 원본을
                    1회 박제한 JSON fixture (tests/golden/migration_phase3/*.json).
  검증 대상      = 마이그레이션된 워킹트리 스크립트가 호출하는 convert_pdf 파라미터
                    (record_migrated_script 가 통합본 진입점을 실제 실행해 캡처).
  두 독립 소스를 비교하므로 순환이 없다.
  ⚠️ 절대 통합본으로 fixture 를 만들지 않는다(vacuous 금지). fixture 의 source_ref 는
     Phase 3 통합 직전 HEAD SHA 로 하드코딩되어, 통합 커밋 후에도 기준이 고정된다.

진입점:
  - ava1, sim_platform : 하드코딩 PDF_PATH → main() 진입.
  - gemini_multimodal  : main() 이 input/pdf/*.pdf 글롭(비결정적)이므로 단위 함수
                         process_pdf(pdf_path) 진입(SCRIPT_ENTRY 로 지정).

검증 항목 (스크립트별):
  ava1          : 10/26/50페이지 — **모두 단일 1청크**(always-single 보존 필수).
  sim_platform  : 20(단일)/21(분할)/50페이지 — 20p 경계 + renumber post_process.
  gemini        : 20(단일)/21(분할)/50페이지 — 임시파일→메모리 병합 동치.
  공통          : 프롬프트 byte-동일, import 크래시 없음, convert_pdf 사용 확인,
                  separator + output_path 대조, sleep(없음/15/10) 보존.

실행:
    .venv/bin/python -m pytest tests/test_migration_phase3.py -v
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
from tests._golden_fixtures import (
    PRE_INTEGRATION_REF_PHASE3, SCRIPT_ARGV, SCRIPT_ENTRY, load_golden,
)

# ── 대상 스크립트 경로 ──────────────────────────────────────────────────────────
_AVA1_SCRIPT = Path(_ROOT) / "extract_ava1.py"
_SIM_SCRIPT = Path(_ROOT) / "extract_sim_platform.py"
_GEMINI_SCRIPT = Path(_ROOT) / "extract_gemini_multimodal.py"


# ──────────────────────────────────────────────────────────────────────────────
# git 원본 로드 헬퍼 (프롬프트 byte-동일 비교용 — Phase 3 통합 직전 HEAD SHA 기준)
# ──────────────────────────────────────────────────────────────────────────────

def _git_show(rel_path: str) -> str:
    """git show <PRE_INTEGRATION_REF_PHASE3>:<rel_path> 로 통합 직전 원본 반환.

    ⚠️ HEAD 가 아니라 Phase 3 통합 직전 SHA 를 본다. 통합 커밋 후 HEAD==워킹트리가
    되어 HEAD 기준 byte-동일 비교가 vacuous 가 되는 것을 막는다.
    """
    r = subprocess.run(
        ["git", "show", f"{PRE_INTEGRATION_REF_PHASE3}:{rel_path}"],
        capture_output=True, text=True, cwd=_ROOT, check=True,
    )
    return r.stdout


def _load_const_from_src(src: str, attr: str, tag: str):
    """소스 문자열을 임시 파일로 저장 후 상수 추출 (mkdir 차단)."""
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


def _gemini_orig_prompt_for(start: int, end: int) -> str:
    """통합 직전 gemini 원본의 인라인 f-string 프롬프트를 재구성(byte 기준값).

    원본은 PROMPT_TEMPLATE 상수가 아니라 extract_chunk 안의 f-string 이라
    상수 추출이 불가하다. 따라서 통합본 fixture(원본 박제)의 실제 호출 프롬프트와
    통합본 워킹트리 프롬프트를 직접 대조한다(아래 test 에서 fixture 사용).
    """
    return (
        f"Extract the full content of pages {start} to {end} from this PDF. "
        "Output in high-quality Markdown, preserving all text structure and tables (GFM pipe format). "
        "CRITICAL: For any images, diagrams, charts, or graphs, perform a highly detailed visual analysis. "
        "Describe the overall layout, visual elements, data trends, X/Y axis values, relationships, and core concepts. "
        "Write the image descriptions within Markdown blockquotes (>). "
        "IMPORTANT: All extracted text and image descriptions MUST be written in fluent Korean. "
        "Ensure all information is transcribed accurately."
    )


def _load_const_from_file(script: Path, attr: str):
    """워킹트리 스크립트에서 상수 추출 (mkdir 차단)."""
    spec = _ilu.spec_from_file_location(f"_wt3_{script.stem}", script)
    mod = _ilu.module_from_spec(spec)
    with patch.object(Path, "mkdir", return_value=None):
        spec.loader.exec_module(mod)
    return getattr(mod, attr)


# ── 통합 직전 원본 소스 (프롬프트 byte-동일 비교 + sleep 리터럴 검증용) ───────────
_AVA1_ORIG_SRC = _git_show("extract_ava1.py")
_SIM_ORIG_SRC = _git_show("extract_sim_platform.py")
_GEMINI_ORIG_SRC = _git_show("extract_gemini_multimodal.py")

# 원본 PROMPT_TEMPLATE 상수 (ava1/sim_platform 만 — gemini 는 f-string 이라 없음)
_AVA1_ORIG_PROMPT = _load_const_from_src(_AVA1_ORIG_SRC, "PROMPT_TEMPLATE", "_ava1_orig")
_SIM_ORIG_PROMPT = _load_const_from_src(_SIM_ORIG_SRC, "PROMPT_TEMPLATE", "_sim_orig")

# 워킹트리(마이그레이션된) 상수 — 프롬프트 byte-동일 비교용.
_AVA1_WT_PROMPT = _load_const_from_file(_AVA1_SCRIPT, "PROMPT_TEMPLATE")
_SIM_WT_PROMPT = _load_const_from_file(_SIM_SCRIPT, "PROMPT_TEMPLATE")
_SIM_WT_OUTDIR = _load_const_from_file(_SIM_SCRIPT, "OUTPUT_DIR")
_GEMINI_WT_PROMPT = _load_const_from_file(_GEMINI_SCRIPT, "PROMPT_TEMPLATE")


# ──────────────────────────────────────────────────────────────────────────────
# 골든 캡처 헬퍼 — 원본(phase3 fixture 로드)
# ──────────────────────────────────────────────────────────────────────────────

def _golden_orig_ava1(total_pages: int) -> PipelineSnapshot:
    """통합 직전 ava1 골든 — 고정 fixture 로드 (항상 단일 청크)."""
    return load_golden("migration_phase3", "extract_ava1", total_pages)


def _golden_orig_sim(total_pages: int) -> PipelineSnapshot:
    """통합 직전 sim_platform 골든 — 고정 fixture 로드 (20p 분할)."""
    return load_golden("migration_phase3", "extract_sim_platform", total_pages)


def _golden_orig_gemini(total_pages: int) -> PipelineSnapshot:
    """통합 직전 gemini_multimodal 골든 — 고정 fixture 로드 (임시파일 병합)."""
    return load_golden("migration_phase3", "extract_gemini_multimodal", total_pages)


# ──────────────────────────────────────────────────────────────────────────────
# 골든 캡처 헬퍼 — 마이그레이션본 (통합본 진입점 실제 실행 → 실제 convert_pdf 인자)
# ──────────────────────────────────────────────────────────────────────────────

def _run_migrated(script: Path, stem: str, total_pages: int) -> PipelineSnapshot:
    """통합본 스크립트 진입점을 실제 실행해 convert_pdf 호출 인자를 녹화.

    파라미터를 하드코딩하지 않고 소스에서 직접 읽으므로(프롬프트/청크정책/출력경로/
    rate_limit/post_process), 스크립트가 원본과 어긋나면 골든 비교가 실패한다(teeth).
    진입점은 SCRIPT_ENTRY 로 결정(gemini=process_pdf, 나머지=main).
    """
    argv = SCRIPT_ARGV.get(stem)
    entry = SCRIPT_ENTRY.get(stem, {"entry_fn": "main", "entry_args": ()})
    saved = sys.argv
    try:
        if argv is not None:
            sys.argv = list(argv)
        return record_migrated_script(
            script, entry["entry_fn"], entry["entry_args"],
            total_pages=total_pages,
        )
    finally:
        sys.argv = saved


def _golden_migrated_ava1(total_pages: int) -> PipelineSnapshot:
    return _run_migrated(_AVA1_SCRIPT, "extract_ava1", total_pages)


def _golden_migrated_sim(total_pages: int) -> PipelineSnapshot:
    return _run_migrated(_SIM_SCRIPT, "extract_sim_platform", total_pages)


def _golden_migrated_gemini(total_pages: int) -> PipelineSnapshot:
    return _run_migrated(_GEMINI_SCRIPT, "extract_gemini_multimodal", total_pages)


# ──────────────────────────────────────────────────────────────────────────────
# ava1 — 항상 단일 청크 보존 (페이지 수 무관)
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenAva1(unittest.TestCase):
    """원본 always-single == 통합본 single_chunk_max=10**9."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_ava1(total_pages)
        migr = _golden_migrated_ava1(total_pages)
        orig.assert_equals(migr, label=f"ava1 {total_pages}p")

    def test_golden_10p(self):
        self._compare(10)

    def test_golden_26p_still_single(self):
        """26페이지여도 단일 청크(원본 always-single 보존)."""
        self._compare(26)

    def test_golden_50p_still_single(self):
        """50페이지여도 단일 청크(표준 20p 분할로 통일하지 않음)."""
        self._compare(50)

    def test_orig_always_single_chunk(self):
        for tp in (10, 26, 50):
            snap = _golden_orig_ava1(tp)
            self.assertEqual(snap.chunk_count, 1, f"{tp}p 원본이 단일청크 아님")
            self.assertEqual(snap.calls[0][1:], (1, tp))

    def test_migrated_always_single_chunk(self):
        for tp in (10, 26, 50):
            snap = _golden_migrated_ava1(tp)
            self.assertEqual(snap.chunk_count, 1, f"{tp}p 통합본이 단일청크 아님")
            self.assertEqual(snap.calls[0][1:], (1, tp))

    def test_separator_none_single(self):
        # 단일 청크라 항상 separator None.
        for tp in (10, 26, 50):
            self.assertIsNone(_golden_migrated_ava1(tp).separator)

    def test_output_path_ava1_md(self):
        snap = _golden_migrated_ava1(26)
        self.assertEqual(snap.output_path.name, "AVA_1.md")

    def test_prompt_verbatim_content(self):
        snap = _golden_migrated_ava1(10)
        self.assertIn("OCR", snap.calls[0][0])
        self.assertIn("GFM pipe", snap.calls[0][0])
        self.assertNotIn("{start}", snap.calls[0][0])


# ──────────────────────────────────────────────────────────────────────────────
# sim_platform — 20p 분할 + renumber post_process
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenSimPlatform(unittest.TestCase):
    """원본 20p 분할 시퀀스 == 통합본 chunk_size=20."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_sim(total_pages)
        migr = _golden_migrated_sim(total_pages)
        orig.assert_equals(migr, label=f"sim_platform {total_pages}p")

    def test_golden_20p_single(self):
        self._compare(20)

    def test_golden_21p_split(self):
        self._compare(21)

    def test_golden_50p(self):
        self._compare(50)

    def test_orig_chunk_ranges_50p(self):
        snap = _golden_orig_sim(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 40))
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_migrated_chunk_ranges_50p(self):
        snap = _golden_migrated_sim(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_migrated_21p_split(self):
        snap = _golden_migrated_sim(21)
        self.assertEqual(snap.chunk_count, 2)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 21))

    def test_output_path_sim_md(self):
        snap = _golden_migrated_sim(20)
        self.assertEqual(snap.output_path.name, "Sim_Platform.md")

    def test_separator_20p_none(self):
        self.assertIsNone(_golden_migrated_sim(20).separator)

    def test_separator_21p_present(self):
        self.assertEqual(_golden_migrated_sim(21).separator, "\n\n---\n\n")

    def test_post_process_renumber_preserved(self):
        """통합본이 renumber_images 를 post_process 로 전달하는지 소스 확인."""
        src = _SIM_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("def renumber_images", src, "renumber_images 미보존")
        self.assertIn("post_process=", src, "post_process 인자 미사용")

    def test_renumber_merged_equiv_per_chunk(self):
        """병합 후 1회 재부여 == 청크별 running-offset 재부여 (byte 동치)."""
        import re

        def renumber(text, offset):
            count = 0
            def repl(m):
                nonlocal count, offset
                count += 1
                return m.group(0).replace(m.group(1), str(offset + count), 1)
            return re.sub(r'\[이미지 (\d+)\s*·', repl, text), offset + count

        sep = "\n\n---\n\n"
        chunks = [
            "x\n> **[이미지 1 · 블록도]**\n> **[이미지 2 · 그래프]**\n",
            "> **[이미지 1 · 사진]**\n> **[이미지 2 · 표]**\n",
            "no img\n",
            "> **[이미지 1 · 회로도]**\n",
        ]
        off = 0
        per_chunk = []
        for c in chunks:
            r, off = renumber(c, off)
            per_chunk.append(r)
        orig_final = sep.join(per_chunk)
        merged_final, _ = renumber(sep.join(chunks), 0)
        self.assertEqual(orig_final, merged_final)


# ──────────────────────────────────────────────────────────────────────────────
# gemini_multimodal — 임시파일 병합 → 메모리 병합 동치
# ──────────────────────────────────────────────────────────────────────────────

class TestGoldenGeminiMultimodal(unittest.TestCase):
    """원본 임시파일 병합 시퀀스 == 통합본 convert_pdf 메모리 병합."""

    def _compare(self, total_pages: int):
        orig = _golden_orig_gemini(total_pages)
        migr = _golden_migrated_gemini(total_pages)
        orig.assert_equals(migr, label=f"gemini {total_pages}p")

    def test_golden_20p_single(self):
        self._compare(20)

    def test_golden_21p_split(self):
        self._compare(21)

    def test_golden_50p(self):
        self._compare(50)

    def test_orig_chunk_ranges_50p(self):
        snap = _golden_orig_gemini(50)
        self.assertEqual(snap.chunk_count, 3)
        self.assertEqual(snap.calls[0][1:], (1, 20))
        self.assertEqual(snap.calls[1][1:], (21, 40))
        self.assertEqual(snap.calls[2][1:], (41, 50))

    def test_orig_output_is_final_md_not_chunk(self):
        """원본 골든 출력 경로가 최종 {stem}.md (임시 _chunk_ 아님)."""
        snap = _golden_orig_gemini(50)
        self.assertTrue(snap.output_path.name.endswith("gemini.md"))
        self.assertNotIn("_chunk_", snap.output_path.name)

    def test_migrated_output_is_final_md(self):
        snap = _golden_migrated_gemini(50)
        self.assertTrue(snap.output_path.name.endswith("gemini.md"))
        self.assertNotIn("_chunk_", snap.output_path.name)

    def test_separator_21p_present(self):
        self.assertEqual(_golden_migrated_gemini(21).separator, "\n\n---\n\n")

    def test_separator_20p_none(self):
        self.assertIsNone(_golden_migrated_gemini(20).separator)

    def test_prompt_english_verbatim(self):
        snap = _golden_migrated_gemini(20)
        self.assertIn("fluent Korean", snap.calls[0][0])
        self.assertIn("GFM pipe format", snap.calls[0][0])
        self.assertNotIn("{start}", snap.calls[0][0])

    def test_prompt_matches_orig_fstring(self):
        """통합본 호출 프롬프트가 원본 f-string(byte 재구성)과 동일."""
        snap = _golden_migrated_gemini(50)
        self.assertEqual(snap.calls[0][0], _gemini_orig_prompt_for(1, 20))
        self.assertEqual(snap.calls[1][0], _gemini_orig_prompt_for(21, 40))


# ──────────────────────────────────────────────────────────────────────────────
# 프롬프트 byte-동일 — 원본 git 상수 vs 워킹트리 상수 직접 비교
# ──────────────────────────────────────────────────────────────────────────────

class TestPromptByteIdentical(unittest.TestCase):
    """마이그레이션 후 워킹트리 PROMPT_TEMPLATE 이 통합 직전 원본과 byte-동일."""

    def test_ava1_prompt_identical(self):
        self.assertEqual(_AVA1_WT_PROMPT, _AVA1_ORIG_PROMPT,
                         "ava1 PROMPT_TEMPLATE 드리프트")

    def test_sim_platform_prompt_identical(self):
        self.assertEqual(_SIM_WT_PROMPT, _SIM_ORIG_PROMPT,
                         "sim_platform PROMPT_TEMPLATE 드리프트")

    def test_gemini_prompt_identical_to_orig_fstring(self):
        """gemini 통합본 PROMPT_TEMPLATE.format == 원본 f-string (byte 동일)."""
        self.assertEqual(
            _GEMINI_WT_PROMPT.format(start=1, end=20),
            _gemini_orig_prompt_for(1, 20),
            "gemini 프롬프트 드리프트",
        )


# ──────────────────────────────────────────────────────────────────────────────
# sleep(rate_limit) 보존 — 원본 sleep 리터럴 == 통합본 rate_limit_s
# ──────────────────────────────────────────────────────────────────────────────

class TestRateLimitPreserved(unittest.TestCase):
    """원본 sleep(ava1 없음/sim 15/gemini 10) == 통합본 rate_limit_s."""

    def test_ava1_orig_no_sleep(self):
        # ava1 원본은 단일 청크라 time.sleep 호출이 없다.
        self.assertNotIn("time.sleep", _AVA1_ORIG_SRC)

    def test_sim_orig_sleep_15(self):
        self.assertIn("time.sleep(15)", _SIM_ORIG_SRC)

    def test_gemini_orig_sleep_10(self):
        self.assertIn("time.sleep(10)", _GEMINI_ORIG_SRC)

    def test_sim_migrated_rate_limit_15(self):
        self.assertIn("rate_limit_s=15", _SIM_SCRIPT.read_text(encoding="utf-8"))

    def test_gemini_migrated_rate_limit_10(self):
        self.assertIn("rate_limit_s=10", _GEMINI_SCRIPT.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────────────
# import 크래시 없음 + convert_pdf 사용 + 특이점 보존
# ──────────────────────────────────────────────────────────────────────────────

class TestMigratedScriptHealth(unittest.TestCase):
    def _load(self, script: Path):
        spec = _ilu.spec_from_file_location(f"_health3_{script.stem}", script)
        mod = _ilu.module_from_spec(spec)
        with patch.object(Path, "mkdir", return_value=None):
            spec.loader.exec_module(mod)
        return mod

    def test_ava1_import_no_crash(self):
        mod = self._load(_AVA1_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "PDF_PATH"))
        self.assertTrue(hasattr(mod, "OUTPUT_PATH"))
        self.assertTrue(hasattr(mod, "main"))

    def test_sim_import_no_crash(self):
        mod = self._load(_SIM_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "renumber_images"))
        self.assertTrue(hasattr(mod, "main"))

    def test_gemini_import_no_crash(self):
        mod = self._load(_GEMINI_SCRIPT)
        self.assertTrue(hasattr(mod, "PROMPT_TEMPLATE"))
        self.assertTrue(hasattr(mod, "process_pdf"))
        self.assertTrue(hasattr(mod, "main"))

    def test_ava1_uses_convert_pdf(self):
        src = _AVA1_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src)
        self.assertNotIn("def extract_chunk", src)
        self.assertIn('on_failure="abort"', src)
        self.assertIn("single_chunk_max=10**9", src)  # 항상 단일 청크 보존

    def test_sim_uses_convert_pdf(self):
        src = _SIM_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src)
        self.assertNotIn("def extract_chunk", src)
        self.assertIn('on_failure="abort"', src)
        self.assertIn("post_process=", src)

    def test_gemini_uses_convert_pdf(self):
        src = _GEMINI_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("convert_pdf", src)
        self.assertNotIn("def extract_chunk", src)
        # 임시파일 scaffolding 코드 제거 확인(원본 토큰 chunk_filename/chunk_paths).
        # (docstring 의 `_chunk_NNN.md` 설명·single_chunk_max 와 구분하기 위해
        #  실제 scaffolding 식별 토큰으로 검사.)
        self.assertNotIn("chunk_filename", src, "임시 청크파일 생성 코드 잔존")
        self.assertNotIn("chunk_paths", src, "임시 청크파일 병합 코드 잔존")
        self.assertIn('on_failure="abort"', src)

    def test_ava1_output_path_literal(self):
        mod = self._load(_AVA1_SCRIPT)
        self.assertEqual(mod.OUTPUT_PATH.name, "AVA_1.md")

    def test_sim_output_path_literal(self):
        mod = self._load(_SIM_SCRIPT)
        self.assertEqual(mod.OUTPUT_PATH.name, "Sim_Platform.md")


# ──────────────────────────────────────────────────────────────────────────────
# 출력경로/separator teeth 자가검증 (변형 시 실패)
# ──────────────────────────────────────────────────────────────────────────────

class TestTeeth(unittest.TestCase):
    def test_teeth_output_path_mutation(self):
        """sim output_path 가 달라지면 assert_equals 실패."""
        orig = _golden_orig_sim(20)
        with tempfile.TemporaryDirectory() as tmp:
            fake_pdf = Path(tmp) / "Sim_Platform.pdf"
            bad_out = _SIM_WT_OUTDIR / "WRONG.md"
            migr_bad = record_convert_pdf(
                fake_pdf, _SIM_WT_PROMPT, output_path=bad_out,
                total_pages=20, chunk_size=20, single_chunk_max=None,
                rate_limit_s=15.0,
            )
        with self.assertRaises(AssertionError):
            orig.assert_equals(migr_bad, label="mutation output_path")

    def test_teeth_separator_mutation(self):
        orig = _golden_orig_gemini(21)
        migr = _golden_migrated_gemini(21)
        migr.separator = "\n\nXXX\n\n"
        with self.assertRaises(AssertionError):
            orig.assert_equals(migr, label="mutation separator")

    def test_teeth_chunk_policy_mutation_ava1(self):
        """ava1 이 단일청크가 아니게 되면(예: 20p 분할) 골든 실패."""
        orig = _golden_orig_ava1(50)  # 단일 (1,50)
        with tempfile.TemporaryDirectory() as tmp:
            fake_pdf = Path(tmp) / "AVA_1.pdf"
            # 의도적 변이: single_chunk_max 제거 → 20p 분할로 3청크
            migr_bad = record_convert_pdf(
                fake_pdf, _AVA1_WT_PROMPT,
                output_path=Path("/Users/heni/workspace/filestomdwgem/output/pdf_md/AVA_1.md"),
                total_pages=50, chunk_size=20, single_chunk_max=None,
                rate_limit_s=0.0,
            )
        with self.assertRaises(AssertionError):
            orig.assert_equals(migr_bad, label="mutation ava1 chunk policy")


if __name__ == "__main__":
    unittest.main(verbosity=2)
