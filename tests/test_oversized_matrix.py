"""test_oversized_matrix.py — 오버사이즈 행렬표(raster) 자동 검출 회귀 가드.

배경(2026-07-03 실측): LN08LPU_Design_Manual testpages 의 "Table 21: Design Truth
Table"(p11~13, ~50열×~60행)은 벡터 격자선이 전혀 없는 순수 raster 이미지로 페이지에
삽입돼 있다(선택 가능한 텍스트 0). 이 실측이 원래 truncation 사고의 근본 원인이었다
(vision 모델이 이 거대 raster 표 이미지를 OCR 텍스트로 전사하려다 응답이 조용히
잘림). detect_complex_tables(사선/벡터 격자 경로)는 이 표를 검출할 수 없어(격자선
자체가 없음), 완전히 독립적인 raster px밀도/페이지 점유율 기반 탐지 경로
(detect_oversized_matrix_tables)를 신설했다.

수용 기준(2026-07-03 조정, coordinator 승인):
    - Design Manual testpages: p11·p12·p13 (Table 21 raster 3페이지) 검출.
    - p10(Metallization Stack, 실제 벡터/텍스트 표) 및 그 외 전 페이지(1~9,14~25)는 미검출.
    - HSPICE_ModelGuide testpages: 전 페이지 미검출.

실행:
    .venv/bin/python -m pytest tests/test_oversized_matrix.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fmdw import figure_extractor as fx  # noqa: E402

_DESIGN_PDF = _ROOT / "input" / "pdf" / "test_pages" / (
    "LN08LPU_Design_Manual_A00-V0.9.2.0_testpages.pdf"
)
_HSPICE_PDF = _ROOT / "input" / "pdf" / "test_pages" / (
    "LN08LPU_HSPICE_ModelGuide_A00-V0.9.2.1_testpages.pdf"
)

_needs_design_pdf = pytest.mark.skipif(
    not _DESIGN_PDF.exists(),
    reason=f"테스트 fixture PDF 부재: {_DESIGN_PDF} — skip.",
)
_needs_hspice_pdf = pytest.mark.skipif(
    not _HSPICE_PDF.exists(),
    reason=f"테스트 fixture PDF 부재: {_HSPICE_PDF} — skip.",
)


# ──────────────────────────────────────────────────────────────────────────────
# 공용 fixture(module scope) — 문서 전체 스캔(watermark xref 집계)은 비용이 있어
# PDF 당 1회만 계산해 여러 테스트가 재사용한다.
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def design_doc():
    import fitz

    doc = fitz.open(str(_DESIGN_PDF))
    yield doc
    doc.close()


@pytest.fixture(scope="module")
def design_wm(design_doc):
    return fx._watermark_xrefs(design_doc)


@pytest.fixture(scope="module")
def hspice_doc():
    import fitz

    doc = fitz.open(str(_HSPICE_PDF))
    yield doc
    doc.close()


@pytest.fixture(scope="module")
def hspice_wm(hspice_doc):
    return fx._watermark_xrefs(hspice_doc)


# ──────────────────────────────────────────────────────────────────────────────
# 1) 게이트 함수 — 기본 OFF, env 로 ON
# ──────────────────────────────────────────────────────────────────────────────

def test_oversized_gate_default_off(monkeypatch):
    monkeypatch.delenv("EXTRACT_OVERSIZED_MATRIX_TABLES", raising=False)
    assert fx._is_oversized_matrix_enabled() is False


def test_oversized_gate_on(monkeypatch):
    monkeypatch.setenv("EXTRACT_OVERSIZED_MATRIX_TABLES", "1")
    assert fx._is_oversized_matrix_enabled() is True


def test_describe_complex_gate_default_off(monkeypatch):
    monkeypatch.delenv("EXTRACT_DESCRIBE_COMPLEX_TABLES", raising=False)
    assert fx._is_describe_complex_tables_enabled() is False


def test_describe_complex_gate_on(monkeypatch):
    monkeypatch.setenv("EXTRACT_DESCRIBE_COMPLEX_TABLES", "1")
    assert fx._is_describe_complex_tables_enabled() is True


def test_describe_complex_max_tokens_default():
    assert fx._describe_complex_max_tokens() == 16384


# ──────────────────────────────────────────────────────────────────────────────
# 2) 실측 픽스처 스윕 — 정확히 11·12·13 만 검출(그 외 0)
# ──────────────────────────────────────────────────────────────────────────────

@_needs_design_pdf
def test_design_manual_sweep_flags_only_11_12_13(design_doc, design_wm, monkeypatch):
    """기본 임계값(밀도 3.5, 점유율 0.30)으로 전 페이지 스윕 시 11·12·13만 검출."""
    monkeypatch.delenv("EXTRACT_OVERSIZED_IMG_MIN_DENSITY", raising=False)
    monkeypatch.delenv("EXTRACT_OVERSIZED_IMG_MIN_COVER", raising=False)
    flagged = []
    for pidx in range(design_doc.page_count):
        if fx.detect_oversized_matrix_tables(design_doc[pidx], exclude_xrefs=design_wm):
            flagged.append(pidx + 1)
    assert flagged == [11, 12, 13], f"기대: [11, 12, 13], 실제: {flagged}"


@_needs_design_pdf
def test_page10_metallization_stack_not_flagged(design_doc, design_wm):
    """p10(Metallization Stack)은 실제 벡터/텍스트 표라 두 탐지 경로 모두 미검출."""
    page10 = design_doc[9]  # 0-indexed
    assert fx.detect_oversized_matrix_tables(page10, exclude_xrefs=design_wm) == []
    # 사선 벡터 경로(독립 탐지기)도 p10 은 사선이 없어 미검출 — 두 경로가 서로
    # 얽혀있지 않음을 함께 확인.
    assert fx.detect_complex_tables(page10) == []


@_needs_hspice_pdf
def test_hspice_sweep_flags_none(hspice_doc, hspice_wm):
    flagged = [
        pidx + 1
        for pidx in range(hspice_doc.page_count)
        if fx.detect_oversized_matrix_tables(hspice_doc[pidx], exclude_xrefs=hspice_wm)
    ]
    assert flagged == []


# ──────────────────────────────────────────────────────────────────────────────
# 3) 임계값 노브 — override 시 실제로 판정이 바뀜을 증명
# ──────────────────────────────────────────────────────────────────────────────

@_needs_design_pdf
def test_threshold_override_changes_detection(design_doc, design_wm, monkeypatch):
    """기본 임계값에서는 p1 미검출이지만, 완화하면 검출됨 → 노브가 실제로 동작."""
    monkeypatch.delenv("EXTRACT_OVERSIZED_IMG_MIN_DENSITY", raising=False)
    monkeypatch.delenv("EXTRACT_OVERSIZED_IMG_MIN_COVER", raising=False)
    page1 = design_doc[0]
    assert fx.detect_oversized_matrix_tables(page1, exclude_xrefs=design_wm) == []

    monkeypatch.setenv("EXTRACT_OVERSIZED_IMG_MIN_DENSITY", "0.1")
    monkeypatch.setenv("EXTRACT_OVERSIZED_IMG_MIN_COVER", "0.01")
    assert fx.detect_oversized_matrix_tables(page1, exclude_xrefs=design_wm) != []


# ──────────────────────────────────────────────────────────────────────────────
# 4) extract_figures 통합 — 게이트 OFF/ON 이 실제 사이드카 항목에 반영되는지.
#    LLM 네트워크 호출 없이(EXTRACT_FIGURE_DETECT=deterministic) 검증.
# ──────────────────────────────────────────────────────────────────────────────

@_needs_design_pdf
def test_gate_off_extract_figures_zero_oversized_items(tmp_path, monkeypatch):
    monkeypatch.delenv("EXTRACT_OVERSIZED_MATRIX_TABLES", raising=False)
    monkeypatch.setenv("EXTRACT_FIGURE_DETECT", "deterministic")  # LLM 미호출(네트워크 0)
    figs = fx.extract_figures(_DESIGN_PDF, tmp_path, dpi=72)
    om_items = [f for f in figs if f.get("kind") == "oversized_matrix"]
    assert om_items == []


@_needs_design_pdf
def test_gate_on_extract_figures_flags_11_12_13_as_complex_table(tmp_path, monkeypatch):
    monkeypatch.setenv("EXTRACT_OVERSIZED_MATRIX_TABLES", "1")
    monkeypatch.setenv("EXTRACT_FIGURE_DETECT", "deterministic")  # LLM 미호출(네트워크 0)
    figs = fx.extract_figures(_DESIGN_PDF, tmp_path, dpi=72)
    om_items = [f for f in figs if f.get("kind") == "oversized_matrix"]
    om_pages = sorted({f["page"] for f in om_items})
    assert om_pages == [11, 12, 13]
    for item in om_items:
        assert item["type"] == "complex_table"
        assert item["source"] == "raster_density"
        png_path = tmp_path / item["image_path"]
        assert png_path.exists(), f"크롭 PNG 없음: {png_path}"
    # 사이드카 파일에도 동일 항목이 반영됐는지.
    sidecar = tmp_path / f"{_DESIGN_PDF.stem}_figures.json"
    assert sidecar.exists()


# ──────────────────────────────────────────────────────────────────────────────
# 5) describe-complex-tables — 전역 EXTRACT_FIGURE_DESCRIBE OFF 여도 complex_table
#    은 독립 게이트로 설명이 호출되는지(mock, 네트워크 0).
# ──────────────────────────────────────────────────────────────────────────────

def _dummy_png(tmp_path: Path) -> Path:
    p = tmp_path / "dummy.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return p


def test_describe_complex_tables_independent_of_global_describe(tmp_path, monkeypatch):
    calls: list[tuple[str, int | None]] = []

    def _fake_describe(image_b64: str, *, caption: str = "", max_tokens=None) -> str:
        calls.append((caption, max_tokens))
        return "합성 설명"

    monkeypatch.setattr(fx, "describe_figure_llm", _fake_describe)
    monkeypatch.delenv("EXTRACT_FIGURE_DESCRIBE", raising=False)  # 전역 OFF
    monkeypatch.setenv("EXTRACT_DESCRIBE_COMPLEX_TABLES", "1")    # 복잡표 전용 ON

    png = _dummy_png(tmp_path)
    desc = fx._maybe_describe_figure(png, caption="표 캡션", item_type="complex_table")
    assert desc == "합성 설명"
    assert calls == [("표 캡션", 16384)], "complex_table 은 16384 max_tokens 로 호출돼야 함"


def test_describe_complex_tables_does_not_affect_plain_figure(tmp_path, monkeypatch):
    """전역 describe OFF + complex-table describe ON 상태에서도 일반 figure(item_type
    기본값)는 설명이 생성되지 않아야 한다(게이트가 complex_table 에만 배타적으로 적용)."""
    calls: list[str] = []
    monkeypatch.setattr(
        fx, "describe_figure_llm",
        lambda *a, **kw: calls.append("called") or "합성 설명",
    )
    monkeypatch.delenv("EXTRACT_FIGURE_DESCRIBE", raising=False)
    monkeypatch.setenv("EXTRACT_DESCRIBE_COMPLEX_TABLES", "1")

    png = _dummy_png(tmp_path)
    desc = fx._maybe_describe_figure(png, caption="일반 도형", item_type="figure")
    assert desc == ""
    assert calls == []


def test_describe_complex_tables_off_by_default(tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        fx, "describe_figure_llm",
        lambda *a, **kw: calls.append("called") or "합성 설명",
    )
    monkeypatch.delenv("EXTRACT_FIGURE_DESCRIBE", raising=False)
    monkeypatch.delenv("EXTRACT_DESCRIBE_COMPLEX_TABLES", raising=False)

    png = _dummy_png(tmp_path)
    desc = fx._maybe_describe_figure(png, item_type="complex_table")
    assert desc == ""
    assert calls == []
