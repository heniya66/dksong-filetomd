"""Phase2 fixes unit tests (recall-gap for wide/dense tables + prompt-leak).

Covers:
  Fix1 detect_dense_vector_tables / detect_oversized_tables (real PDF pages)
  Fix2 ellipsis truncation runaway detection
  Fix3 borderless 2-tier group-header flattening
  Fix4 prompt-leak line signatures

Run: .venv/bin/python -m pytest test_phase2_fixes.py -q
"""
import os
import pytest

import extract_all_via_pdf as E
from fmdw import figure_extractor as FX

DM = "input/pdf/DM_p1274-1284.pdf"
HS = "input/pdf/HS_p0249-0278.pdf"


# ── Fix1: dense vector table detector ────────────────────────────────────────
@pytest.mark.skipif(not os.path.exists(DM), reason="DM pdf absent")
def test_fix1_detects_dm_biasing_matrix():
    import fitz
    doc = fitz.open(DM)
    try:
        # p3/p4 (idx 2,3) = biasing matrix, ~25-26 aligned cols
        for idx in (2, 3):
            boxes = FX.detect_dense_vector_tables(doc[idx])
            assert len(boxes) == 1, f"idx{idx}: expected 1 dense-vector box, got {boxes}"
            b = boxes[0]
            assert b[2] > b[0] and b[3] > b[1]
    finally:
        doc.close()


@pytest.mark.skipif(not os.path.exists(DM), reason="DM pdf absent")
def test_fix1_no_falsepos_on_normal_dm_pages():
    import fitz
    doc = fitz.open(DM)
    try:
        # idx 0 (title/prose), 4 (small), 10 (2 rows) must NOT be flagged
        for idx in (0, 1, 4, 5, 6, 10):
            assert FX.detect_dense_vector_tables(doc[idx]) == [], f"false pos idx{idx}"
    finally:
        doc.close()


@pytest.mark.skipif(not os.path.exists(HS), reason="HS pdf absent")
def test_fix1_no_falsepos_on_hs_12col_tables():
    import fitz
    doc = fitz.open(HS)
    try:
        # HS 12-col well-formed tables (idx15, idx29) transcribe fine → must NOT image
        for idx in (0, 1, 2, 15, 20, 29):
            assert FX.detect_dense_vector_tables(doc[idx]) == [], f"false pos HS idx{idx}"
    finally:
        doc.close()


@pytest.mark.skipif(not os.path.exists(DM), reason="DM pdf absent")
def test_fix1_threshold_env_gate():
    import fitz
    doc = fitz.open(DM)
    try:
        os.environ["FMDW_DENSE_VECTOR_COLS"] = "40"  # above DM's ~26 → suppressed
        assert FX.detect_dense_vector_tables(doc[2]) == []
        os.environ.pop("FMDW_DENSE_VECTOR_COLS")
        os.environ["FMDW_OVERSIZED_VECTOR"] = "0"     # master off
        assert FX.detect_dense_vector_tables(doc[2]) == []
        os.environ.pop("FMDW_OVERSIZED_VECTOR")
        assert len(FX.detect_dense_vector_tables(doc[2])) == 1  # restored default
    finally:
        doc.close()


@pytest.mark.skipif(not os.path.exists(DM), reason="DM pdf absent")
def test_fix1_combined_ssot_includes_vector():
    import fitz
    doc = fitz.open(DM)
    try:
        assert len(FX.detect_oversized_tables(doc[2])) >= 1
    finally:
        doc.close()


# ── Fix2: ellipsis truncation runaway ────────────────────────────────────────
def test_fix2_ellipsis_runaway_line():
    line = "| :--- | :--- | :--- | :--- | :--- | ... ... ... ... ... ... ... |"
    assert E._is_runaway_line(line) is True


def test_fix2_ellipsis_unicode():
    assert E._is_runaway_line("| a | … … … … |") is True


def test_fix2_prose_single_ellipsis_preserved():
    # ordinary prose with a single ellipsis and NO pipe → not runaway
    assert E._is_runaway_line("The result is unknown... we continue.") is False


def test_fix2_normal_table_row_preserved():
    assert E._is_runaway_line("| 20 | 2 | 0.359 | 0.331 | 27.9 |") is False


def test_fix2_gfm_separator_preserved():
    assert E._is_runaway_line("| :--- | :--- | :--- |") is False


def test_fix2_clean_runaway_removes_ellipsis():
    md = ("| L | NFIN | Vt |\n"
          "| :--- | :--- | ... ... ... ... |\n"
          "| 20 | 2 | 0.3 |\n")
    out = E._clean_runaway(md)
    assert "... ..." not in out
    assert "| 20 | 2 | 0.3 |" in out


def test_fix2_chunk_shows_truncation_on_ellipsis():
    md = "text\n| a | b | ... ... ... ... |\nmore"
    assert E._chunk_shows_truncation(md) is True


# ── Fix3: borderless 2-tier group-header flatten ─────────────────────────────
def test_fix3_flattens_group_header():
    md = (
        "| Metallization Option |  |  |\n"
        "| :--- | :--- | :--- |\n"
        "|  | 9 | 15 |\n"
        "| 0.1 | 0.2 | 0.3 |\n"
        "| 0.4 | 0.5 | 0.6 |\n"
    )
    out = E._flatten_borderless_group_headers(md)
    assert "Metallization Option 9" in out
    assert "Metallization Option 15" in out
    # sub-header row absorbed → the '| | 9 | 15 |' line gone
    assert "|  | 9 | 15 |" not in out
    # data preserved
    assert "| 0.1 | 0.2 | 0.3 |" in out


def test_fix3_noop_on_normal_single_header():
    md = (
        "| L | NFIN | Vtlin |\n"
        "| :--- | :--- | :--- |\n"
        "| [nm] | [#] | [V] |\n"
        "| 20 | 2 | 0.359 |\n"
    )
    assert E._flatten_borderless_group_headers(md) == md  # byte-identical


def test_fix3_noop_on_units_row_table():
    # header full, first data row = units (non-empty leader) → no group header
    md = (
        "| Param | Min | Typ | Max |\n"
        "| :--- | :--- | :--- | :--- |\n"
        "| Vth | 1 | 2 | 3 |\n"
    )
    assert E._flatten_borderless_group_headers(md) == md


def test_fix3_noop_single_empty_not_group():
    # only ONE spanned empty → below >=2 guard → no-op
    md = (
        "| Name |  | Value |\n"
        "| :--- | :--- | :--- |\n"
        "|  | note | 5 |\n"
        "| a | b | 6 |\n"
    )
    assert E._flatten_borderless_group_headers(md) == md


def test_fix3_env_gate_off():
    md = (
        "| Grp |  |  |\n| :--- | :--- | :--- |\n|  | a | b |\n| 1 | 2 | 3 |\n"
    )
    os.environ["FMDW_BORDERLESS_GROUPHDR"] = "0"
    try:
        assert E._flatten_borderless_group_headers(md) == md
    finally:
        os.environ.pop("FMDW_BORDERLESS_GROUPHDR")


# ── Fix4: prompt-leak line signatures ────────────────────────────────────────
def test_fix4_removes_output_only_leak():
    md = ("Some real body text.\n\n"
          "Output ONLY the Markdown content (no surrounding code fence).\n\n"
          "More real text.\n")
    out = E._strip_prompt_leak(md)
    assert "Output ONLY the Markdown content" not in out
    assert "Some real body text." in out
    assert "More real text." in out


def test_fix4_signatures_present():
    assert "output only the markdown content" in E._PROMPT_LEAK_LINE_SIGNATURES
    assert "no surrounding code fence" in E._PROMPT_LEAK_LINE_SIGNATURES


def test_fix4_preserves_table_rows():
    md = "| output only the markdown content | x |\n| :--- | :--- |\n"
    # lines starting with '|' are skipped by the A3 filter → preserved
    assert E._strip_prompt_leak(md) == md


# ── Fix3 Minor#2 보강 픽스처 (실코퍼스 발동 0건 → 방어적, 합성 대표패턴으로 보강) ────
# 코퍼스(output/pdf_md 87 + 백업 87) 전수 스캔에서 _flatten_borderless_group_headers 가
# 실제로 발동하는 샘플은 0건이었다(유일 loose 후보 HS Table 5-4 = OCR 붕괴표, 정당 미발동).
# 따라서 Fix3 회귀는 아래 합성 픽스처(단일/다중 그룹 + 멱등 + 무발동 가드)로만 커버한다.
def test_fix3_flattens_multi_group_header():
    # 그룹 라벨 2개(Metallization Option / Minimum Dimension)가 각각 전방채움되어
    # '{그룹} {서브}' 완전수식 단일헤더로 병합되는지(대표 실무 패턴) 검증.
    md = (
        "| Metallization Option |  |  | Minimum Dimension |  |\n"
        "| :--- | :--- | :--- | :--- | :--- |\n"
        "|  | 9 | 15 | Line | Space |\n"
        "| 0.1 | 0.2 | 0.3 | 0.4 | 0.5 |\n"
        "| 0.6 | 0.7 | 0.8 | 0.9 | 1.0 |\n"
    )
    out = E._flatten_borderless_group_headers(md)
    hdr = out.splitlines()[0]
    assert hdr == (
        "| Metallization Option | Metallization Option 9 | "
        "Metallization Option 15 | Minimum Dimension Line | "
        "Minimum Dimension Space |"
    )
    # 서브헤더 행 흡수 + 데이터 보존
    assert "|  | 9 | 15 | Line | Space |" not in out
    assert "| 0.1 | 0.2 | 0.3 | 0.4 | 0.5 |" in out
    assert "| 0.6 | 0.7 | 0.8 | 0.9 | 1.0 |" in out
    # 멱등: 평탄화 결과를 재적용해도 불변
    assert E._flatten_borderless_group_headers(out) == out


def test_fix3_idempotent_on_single_group():
    md = (
        "| Metallization Option |  |  |\n"
        "| :--- | :--- | :--- |\n"
        "|  | 9 | 15 |\n"
        "| 0.1 | 0.2 | 0.3 |\n"
    )
    once = E._flatten_borderless_group_headers(md)
    assert "Metallization Option 9" in once
    assert "Metallization Option 15" in once
    assert E._flatten_borderless_group_headers(once) == once  # 멱등
