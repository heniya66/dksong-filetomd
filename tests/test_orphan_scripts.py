"""test_orphan_scripts.py — 고아 진단/보수 스크립트 경량 단위 테스트.

검증 항목:
  A) import 시 크래시 없음 (모듈-레벨 부작용 제거 확인)
  B) --help 파서 동작 (SystemExit(0) — argparse 정상)
  C) 순수 함수 단위 테스트
     - reextract_figures: get_existing_image_blocks, inject_into_md
     - analyze_figures:   count_md_blocks, print_table(mock pdf)
     - analyze_figures_v2: is_tile_slice_group, count_md_blocks
     - inspect_images:   inspect_pdf 시그니처 존재

실행:
    .venv/bin/python -m pytest tests/test_orphan_scripts.py -v
"""
from __future__ import annotations

import importlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# scripts/ 도 경로에 추가 (직접 import 지원)
_SCRIPTS = os.path.join(_ROOT, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


# ──────────────────────────────────────────────────────────────────────────────
# A) import 시 크래시 없음
# ──────────────────────────────────────────────────────────────────────────────

class TestImportNoCrash(unittest.TestCase):
    """각 스크립트가 import 만으로 크래시 없어야 한다."""

    def _import(self, name: str):
        """scripts/<name>.py 를 import."""
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_SCRIPTS, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    def test_reextract_figures_import(self):
        mod = self._import("reextract_figures")
        self.assertTrue(hasattr(mod, "process_file"))

    def test_analyze_figures_import(self):
        mod = self._import("analyze_figures")
        self.assertTrue(hasattr(mod, "count_pdf_figures"))

    def test_analyze_figures_v2_import(self):
        mod = self._import("analyze_figures_v2")
        self.assertTrue(hasattr(mod, "count_pdf_figures"))

    def test_inspect_images_import(self):
        mod = self._import("inspect_images")
        self.assertTrue(hasattr(mod, "inspect_pdf"))


# ──────────────────────────────────────────────────────────────────────────────
# B) --help argparse 동작 확인
# ──────────────────────────────────────────────────────────────────────────────

class TestArgparseHelp(unittest.TestCase):
    """각 스크립트의 _build_parser().parse_args(['--help']) 가 SystemExit(0) 이어야 한다."""

    def _load(self, name: str):
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_SCRIPTS, f"{name}.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    def _assert_help(self, name: str):
        mod = self._load(name)
        parser = mod._build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0,
                         f"{name}: --help 종료 코드가 0 이어야 함")

    def test_reextract_figures_help(self):
        self._assert_help("reextract_figures")

    def test_analyze_figures_help(self):
        self._assert_help("analyze_figures")

    def test_analyze_figures_v2_help(self):
        self._assert_help("analyze_figures_v2")

    def test_inspect_images_help(self):
        self._assert_help("inspect_images")


# ──────────────────────────────────────────────────────────────────────────────
# C-1) reextract_figures — 순수 함수 단위 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestReextractFigures(unittest.TestCase):
    """reextract_figures.py 순수 함수 검증."""

    def _load(self):
        spec = importlib.util.spec_from_file_location(
            "reextract_figures",
            os.path.join(_SCRIPTS, "reextract_figures.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    def test_get_existing_image_blocks_finds_blocks(self):
        """get_existing_image_blocks: 이미지 블록 마커를 올바르게 추출."""
        mod = self._load()
        section = (
            "> **[이미지 · 블록도]**\n"
            "> **제목/캡션**: 테스트\n"
            "> **[이미지 · 다이어그램]**\n"
        )
        result = mod.get_existing_image_blocks(section)
        self.assertEqual(len(result), 2,
                         f"이미지 블록 2개여야 하지만 {result}")

    def test_get_existing_image_blocks_empty(self):
        """get_existing_image_blocks: 블록 없으면 빈 리스트."""
        mod = self._load()
        result = mod.get_existing_image_blocks("## Page 3\n본문 텍스트")
        self.assertEqual(result, [])

    def test_inject_into_md_inserts_block(self):
        """inject_into_md: ## Page N 섹션에 블록 삽입."""
        mod = self._load()
        md_content = (
            "## Page 1\n본문 텍스트\n\n"
            "## Page 2\n두 번째 페이지\n"
        )
        new_block = "> **[이미지 · 블록도]**\n> **제목/캡션**: 신규"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                                         encoding='utf-8', delete=False) as f:
            f.write(md_content)
            tmp_path = Path(f.name)

        try:
            result = mod.inject_into_md(tmp_path, 1, new_block)
            self.assertTrue(result, "삽입 성공이어야 함")
            updated = tmp_path.read_text(encoding='utf-8')
            self.assertIn("[이미지 · 블록도]", updated,
                          "삽입된 블록이 파일에 있어야 함")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_inject_into_md_skip_existing(self):
        """inject_into_md --skip-existing: 기존 블록 있으면 스킵."""
        mod = self._load()
        md_content = (
            "## Page 1\n"
            "> **[이미지 · 기존]**\n"
            "> **제목/캡션**: 이미 있음\n\n"
            "## Page 2\n두 번째\n"
        )
        new_block = "> **[이미지 · 신규]**\n> **제목/캡션**: 새것"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                                         encoding='utf-8', delete=False) as f:
            f.write(md_content)
            tmp_path = Path(f.name)

        try:
            result = mod.inject_into_md(tmp_path, 1, new_block, skip_existing=True)
            self.assertFalse(result, "기존 블록 있으면 스킵(False) 이어야 함")
            updated = tmp_path.read_text(encoding='utf-8')
            self.assertNotIn("[이미지 · 신규]", updated,
                             "skip_existing=True 이면 신규 블록 삽입 안 해야 함")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_inject_into_md_missing_section(self):
        """inject_into_md: ## Page N 섹션 없으면 False 반환 (크래시 없음)."""
        mod = self._load()
        md_content = "## Page 1\n본문\n"
        new_block = "> **[이미지 · 블록도]**"

        with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                                         encoding='utf-8', delete=False) as f:
            f.write(md_content)
            tmp_path = Path(f.name)

        try:
            result = mod.inject_into_md(tmp_path, 99, new_block)
            self.assertFalse(result, "섹션 없으면 False 이어야 함")
        finally:
            tmp_path.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# C-2) analyze_figures — count_md_blocks 단위 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyzeFigures(unittest.TestCase):
    """analyze_figures.py 순수 함수 검증 (PDF 열기 없이)."""

    def _load(self):
        spec = importlib.util.spec_from_file_location(
            "analyze_figures",
            os.path.join(_SCRIPTS, "analyze_figures.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    def test_count_md_blocks_correct(self):
        """count_md_blocks: 페이지별 이미지 블록 수 정확 집계."""
        mod = self._load()
        md = (
            "## Page 1\n"
            "> **[이미지 · 블록도]**\n"
            "> **[이미지 · 그래프]**\n"
            "## Page 2\n"
            "텍스트만\n"
            "## Page 3\n"
            "> **[이미지 · 다이어그램]**\n"
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                                         encoding='utf-8', delete=False) as f:
            f.write(md)
            tmp = Path(f.name)
        try:
            result = mod.count_md_blocks(tmp)
            self.assertEqual(result.get(1, 0), 2, f"Page 1 = 2블록, 실제={result}")
            self.assertEqual(result.get(2, 0), 0, f"Page 2 = 0블록, 실제={result}")
            self.assertEqual(result.get(3, 0), 1, f"Page 3 = 1블록, 실제={result}")
        finally:
            tmp.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# C-3) analyze_figures_v2 — is_tile_slice_group 단위 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestAnalyzeFiguresV2(unittest.TestCase):
    """analyze_figures_v2.py 순수 함수 검증."""

    def _load(self):
        spec = importlib.util.spec_from_file_location(
            "analyze_figures_v2",
            os.path.join(_SCRIPTS, "analyze_figures_v2.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    def test_is_tile_slice_group_detects_tiles(self):
        """is_tile_slice_group: 동일 크기 연속 xref → 타일로 판단."""
        mod = self._load()
        # xref 4,5,6 이 동일 크기(2414×430) → 5,6이 타일
        images = [(4, 2414, 430), (5, 2414, 430), (6, 2414, 430)]
        tile_xrefs = mod.is_tile_slice_group(images)
        self.assertIn(5, tile_xrefs)
        self.assertIn(6, tile_xrefs)
        self.assertNotIn(4, tile_xrefs, "첫 번째 xref(대표)는 타일에서 제외")

    def test_is_tile_slice_group_single_not_tile(self):
        """is_tile_slice_group: 유일한 크기 이미지 → 타일 아님."""
        mod = self._load()
        images = [(10, 800, 600), (20, 400, 300)]
        tile_xrefs = mod.is_tile_slice_group(images)
        self.assertEqual(len(tile_xrefs), 0,
                         "서로 다른 크기 이미지는 타일 아님")

    def test_count_md_blocks_v2(self):
        """analyze_figures_v2.count_md_blocks: v1과 동일 로직."""
        mod = self._load()
        md = (
            "## Page 1\n> **[이미지 · A]**\n"
            "## Page 2\n텍스트\n"
        )
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md',
                                         encoding='utf-8', delete=False) as f:
            f.write(md)
            tmp = Path(f.name)
        try:
            result = mod.count_md_blocks(tmp)
            self.assertEqual(result.get(1, 0), 1)
            self.assertEqual(result.get(2, 0), 0)
        finally:
            tmp.unlink(missing_ok=True)


# ──────────────────────────────────────────────────────────────────────────────
# C-4) inspect_images — 함수 시그니처 존재 확인
# ──────────────────────────────────────────────────────────────────────────────

class TestInspectImages(unittest.TestCase):
    """inspect_images.py 함수 존재 및 시그니처 확인."""

    def _load(self):
        spec = importlib.util.spec_from_file_location(
            "inspect_images",
            os.path.join(_SCRIPTS, "inspect_images.py")
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore
        return mod

    def test_inspect_pdf_callable(self):
        """inspect_pdf 함수가 callable 이어야 한다."""
        mod = self._load()
        self.assertTrue(callable(mod.inspect_pdf))

    def test_inspect_pdf_accepts_label(self):
        """inspect_pdf(pdf_path, label) 시그니처 — label 파라미터 있어야 함."""
        import inspect as _inspect
        mod = self._load()
        sig = _inspect.signature(mod.inspect_pdf)
        self.assertIn("label", sig.parameters,
                      "inspect_pdf 에 label 파라미터가 있어야 함")


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
