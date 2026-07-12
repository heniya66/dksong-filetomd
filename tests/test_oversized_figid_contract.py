"""test_oversized_figid_contract.py — 오버사이즈 표 figure_id/kind ↔ codesign-rag 계약 가드.

GPU/모델 추론/네트워크 0. 합성 PDF(fitz) 로 크롭 경로만 검증하고, figure_id·kind 가
codesign-rag ``src/rag/figure_linker.py`` 의 매칭 계약을 만족하는지 정규식/집합으로 직접
대조한다. codesign-rag 는 원격에 없을 수 있어 계약값을 리터럴 미러로 박제한다(아래).

실행:
    .venv/bin/python -m pytest tests/test_oversized_figid_contract.py -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fmdw import figure_extractor as fx  # noqa: E402
from fmdw import oversized_table as ot  # noqa: E402

# ── codesign-rag figure_linker 계약 미러(읽기 전용, 2026-07-12 확인) ──────────────
#   src/rag/figure_linker.py L74 / L66
_FIG_ID_SUFFIX_RE = re.compile(r"__p\d+_(?:fig|table)(?:_[a-z]+)*\d+$")
_TABLE_KINDS = frozenset({"oversized_table", "diagonal_table"})


# ── 1) 기본(계약 준수) figure_id/kind ─────────────────────────────────────────────

def test_default_figure_id_matches_codesign_contract(monkeypatch):
    monkeypatch.delenv("FMDW_OVERSIZED_FIGID_LEGACY", raising=False)
    fid = fx._oversized_table_figure_id("LN08LPU_Design_Manual", 91, 1)
    assert fid == "LN08LPU_Design_Manual__p91_table_oversized1"
    # codesign-rag 접미사 정규식에 매치되어야 doc_stem 이 올바로 도출된다.
    assert _FIG_ID_SUFFIX_RE.search(fid), f"{fid} 가 _FIG_ID_SUFFIX_RE 에 매치되지 않음"
    doc_stem = _FIG_ID_SUFFIX_RE.sub("", fid)
    assert doc_stem == "LN08LPU_Design_Manual"


def test_default_kind_in_codesign_table_kinds(monkeypatch):
    monkeypatch.delenv("FMDW_OVERSIZED_FIGID_LEGACY", raising=False)
    assert fx._oversized_table_kind() == "oversized_table"
    assert fx._oversized_table_kind() in _TABLE_KINDS


def test_old_formats_do_not_match_contract():
    # 회귀 방지: 옛 형식은 계약에 안 걸린다는 사실을 박제(수정 근거).
    assert not _FIG_ID_SUFFIX_RE.search("STEM__p91_omtbl1")
    assert not _FIG_ID_SUFFIX_RE.search("STEM__p11_ctable1")
    assert "oversized_matrix" not in _TABLE_KINDS


# ── 2) 레거시 게이트 폴백 ─────────────────────────────────────────────────────────

def test_legacy_gate_restores_old_format(monkeypatch):
    monkeypatch.setenv("FMDW_OVERSIZED_FIGID_LEGACY", "1")
    assert fx._oversized_table_figure_id("STEM", 91, 2) == "STEM__p91_omtbl2"
    assert fx._oversized_table_kind() == "oversized_matrix"


# ── 3) 합성 PDF 크롭 경로(모델/GPU 없음) ───────────────────────────────────────────

def _make_synthetic_pdf(path: Path) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    # 페이지 대부분을 채우는 사각형(초대형 표 근사). 크롭 대상이 존재하도록.
    page.draw_rect(fitz.Rect(40, 80, 560, 720), color=(0, 0, 0), fill=(0.9, 0.9, 0.9))
    page.insert_text((60, 100), "Table 21: Design Truth Table")
    doc.save(str(path))
    doc.close()


@pytest.mark.skipif("fitz" not in sys.modules and __import__("importlib").util.find_spec("fitz") is None,
                    reason="PyMuPDF(fitz) 부재 — skip.")
def test_crop_table_region_default_contract(tmp_path, monkeypatch):
    monkeypatch.delenv("FMDW_OVERSIZED_FIGID_LEGACY", raising=False)
    pdf = tmp_path / "SyntheticDoc.pdf"
    _make_synthetic_pdf(pdf)
    out_dir = tmp_path / "out"

    entry = ot.crop_table_region(
        pdf, page_no=1, out_dir=out_dir, dpi=72, table_index=1,
        caption="Table 21: Design Truth Table",
    )
    # 계약 필드.
    assert entry["figure_id"] == "SyntheticDoc__p01_table_oversized1"
    assert _FIG_ID_SUFFIX_RE.search(entry["figure_id"])
    assert entry["kind"] == "oversized_table"
    assert entry["kind"] in _TABLE_KINDS
    assert entry["type"] == "complex_table"
    assert entry["source"] == "complex_table_crop"
    assert "text_transcription" in entry  # 항상 존재(계약 §3).
    # 크롭 PNG 실제 생성.
    png = out_dir / entry["image_path"]
    assert png.exists() and png.stat().st_size > 0

    # 사이드카 upsert 멱등성 + 타 figure_id 보존.
    sidecar = out_dir / f"{pdf.stem}_figures.json"
    other = {"figure_id": "SyntheticDoc__p01_fig1", "type": "figure"}
    ot.upsert_sidecar_entry(sidecar, other)
    ot.upsert_sidecar_entry(sidecar, entry)
    ot.upsert_sidecar_entry(sidecar, entry)  # 재실행 멱등.
    import json as _json
    items = _json.loads(sidecar.read_text(encoding="utf-8"))
    ids = [i["figure_id"] for i in items]
    assert ids.count("SyntheticDoc__p01_table_oversized1") == 1  # 중복 없음.
    assert "SyntheticDoc__p01_fig1" in ids  # 타 항목 보존.


@pytest.mark.skipif(__import__("importlib").util.find_spec("fitz") is None,
                    reason="PyMuPDF(fitz) 부재 — skip.")
def test_crop_table_region_legacy_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("FMDW_OVERSIZED_FIGID_LEGACY", "1")
    pdf = tmp_path / "LegacyDoc.pdf"
    _make_synthetic_pdf(pdf)
    entry = ot.crop_table_region(pdf, page_no=1, out_dir=tmp_path / "o", dpi=72)
    assert entry["figure_id"] == "LegacyDoc__p01_omtbl1"
    assert entry["kind"] == "oversized_matrix"


def test_page_range_guard(tmp_path):
    import importlib
    if importlib.util.find_spec("fitz") is None:
        pytest.skip("fitz 부재")
    pdf = tmp_path / "Doc.pdf"
    _make_synthetic_pdf(pdf)
    with pytest.raises(ValueError):
        ot.crop_table_region(pdf, page_no=99, out_dir=tmp_path / "o")


# ── 4) 순수 함수(모델/PDF 없음) ────────────────────────────────────────────────────

def test_replace_polluted_table_in_md_exact_one(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("intro\n\n<table><tr><td>x</td></tr></table>\n\ntail\n", encoding="utf-8")
    ot.replace_polluted_table_in_md(
        md, "<table><tr><td>x</td></tr></table>",
        image_path="figures/d__p01_table_oversized1.png",
        figure_id="d__p01_table_oversized1",
    )
    body = md.read_text(encoding="utf-8")
    assert "![d__p01_table_oversized1](figures/d__p01_table_oversized1.png)" in body
    assert "<table>" not in body
    assert "tail" in body


def test_replace_polluted_table_ambiguous_raises(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("MARK\n\nMARK\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ot.replace_polluted_table_in_md(md, "MARK", image_path="x.png", figure_id="x")


def test_inject_description_block_idempotent(tmp_path):
    md = tmp_path / "d.md"
    md.write_text("![x](figures/x.png)\n\nnext\n", encoding="utf-8")
    assert ot.inject_description_block(md, "figures/x.png", "설명 내용") is True
    assert ot.inject_description_block(md, "figures/x.png", "설명 내용") is False  # 멱등.
    assert ot.inject_description_block(md, "figures/x.png", "   ") is False  # 빈 설명.
    assert md.read_text(encoding="utf-8").count(ot.DESCRIPTION_BLOCK_PREFIX) == 1


def test_find_missing_descriptions(tmp_path):
    import json as _json
    sc = tmp_path / "s_figures.json"
    sc.write_text(_json.dumps([
        {"figure_id": "a", "kind": "oversized_table", "description": ""},
        {"figure_id": "b", "kind": "oversized_matrix", "description": ""},  # 구 형식도 회수.
        {"figure_id": "c", "kind": "oversized_table", "description": "채움"},
        {"figure_id": "d", "kind": "figure", "description": ""},  # 대상 아님.
    ]), encoding="utf-8")
    missing = ot.find_missing_descriptions(sc)
    ids = {m["figure_id"] for m in missing}
    assert ids == {"a", "b"}
