"""R12(2026-07-15) 페이지 경계 sub-row 체인 병합 테스트.

대상: extract_all_via_pdf._xpage_subrow_migrate (3도구 공유 SSoT)
      + _xpage_registry_pass 통합 동작(실물 regress7 산출물 기반 결정론 검증).

배경: R7 F-SUBROW 가드 (b)는 페이지 상단 이월 파편(부모가 이전 페이지)을 오병합
방지 목적으로 보존 → continued 표 최상단에 anchor 빈 값행이 독립 잔존(사용자
실물 신고). R12 는 원장이 지문으로 확정한 체인 안에서만 그 파편을 이전 페이지
마지막 논리 행에 병합한다. FMDW_SUBROW_MERGE·FMDW_XPAGE_CONT 둘 다 ON 일 때만.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import extract_all_via_pdf as x  # noqa: E402

HDR = ["Mask Level", "Description", "On Wafer", "Aligns to", "Line", "Space"]
PREV_ROWS = [
    HDR,
    ["RX", "Active cut.", "Blocked", "FN", "0.042", "0.084"],
    ["TP", "1st TiN close.", "Blocked", "FN", "0.064", "0.042"],
]
FRAG1 = ["", "", "", "", "0.064 (run length ≡ 0)", "0.064 (run length ≡ 0)"]
FRAG2 = ["", "", "", "", "0.140 (SRAM)", "0.176 (SRAM)"]
REAL1 = ["NW", "N-well iip.", "Open", "FN", "0.192", "0.192"]


def test_migrate_happy_path_two_fragments():
    """선두 연속 파편 2행 → prev 마지막 행(TP)에 <br> 병합, cont 에서 제거."""
    pr, cr, n, pb, cb = x._xpage_subrow_migrate(PREV_ROWS, [FRAG1, FRAG2, REAL1])
    assert n == 2
    assert cr == [REAL1], "파편만 제거·실행 보존"
    tp = pr[-1]
    assert tp[0] == "TP"
    assert tp[4] == "0.064<br>0.064 (run length ≡ 0)<br>0.140 (SRAM)"
    assert tp[5] == "0.042<br>0.064 (run length ≡ 0)<br>0.176 (SRAM)"
    # 원본 불변(사본 반환)
    assert PREV_ROWS[-1][4] == "0.064" and FRAG1[4].startswith("0.064 (run")


def test_migrate_guard_parent_anchor_empty_preserved():
    """가드①: prev 마지막 행 anchor 공란 → 보존(오병합 방지 — 진짜 새 표의
    우연한 빈 anchor 첫 행 케이스)."""
    prev = [HDR, ["", "", "", "", "0.1", "0.2"]]
    pr, cr, n, _pb, _cb = x._xpage_subrow_migrate(prev, [FRAG1, REAL1])
    assert n == 0
    assert cr == [FRAG1, REAL1] and pr == prev


def test_migrate_only_leading_run_not_middle():
    """중간 파편은 R7 within-page 소관 — 선두 연속 run 만 이동."""
    pr, cr, n, _pb, _cb = x._xpage_subrow_migrate(
        PREV_ROWS, [REAL1, FRAG1, ["PC", "Gate.", "Open", "FN", "0.02", "0.04"]])
    assert n == 0
    assert len(cr) == 3 and pr[-1][4] == "0.064"


def test_migrate_env_off_noop(monkeypatch):
    """FMDW_SUBROW_MERGE=0 또는 FMDW_XPAGE_CONT=0 → 무변경(기존 동작 복원)."""
    monkeypatch.setenv("FMDW_SUBROW_MERGE", "0")
    pr, cr, n, _pb, _cb = x._xpage_subrow_migrate(PREV_ROWS, [FRAG1, REAL1])
    assert n == 0 and cr == [FRAG1, REAL1]
    monkeypatch.setenv("FMDW_SUBROW_MERGE", "1")
    monkeypatch.setenv("FMDW_XPAGE_CONT", "0")
    pr, cr, n, _pb, _cb = x._xpage_subrow_migrate(PREV_ROWS, [FRAG1, REAL1])
    assert n == 0 and cr == [FRAG1, REAL1]


def test_migrate_full_consumption():
    """연속분이 파편만으로 구성 → 전부 이동, cont 는 빈 리스트(블록 소진 신호)."""
    pr, cr, n, _pb, _cb = x._xpage_subrow_migrate(PREV_ROWS, [FRAG1, FRAG2])
    assert n == 2 and cr == []
    assert "0.176 (SRAM)" in pr[-1][5]


def test_migrate_bbox_merged_columns_nulled():
    """병합된 열의 prev bbox 는 None(페이지 경계 union 은 위치 증거 무효 —
    cross_verify 자동 교체 차단), cont bbox 는 파편 수만큼 제거."""
    pbb = [[(0, 0, 1, 1)] * 6 for _ in PREV_ROWS]
    cbb = [[(0, 0, 2, 2)] * 6 for _ in [FRAG1, REAL1]]
    pr, cr, n, pb, cb = x._xpage_subrow_migrate(
        PREV_ROWS, [FRAG1, REAL1], pbb, cbb)
    assert n == 1
    assert pb[-1][4] is None and pb[-1][5] is None, "병합 열 bbox 무효화"
    assert pb[-1][0] == (0, 0, 1, 1), "비병합 열 bbox 보존"
    assert len(cb) == 1


# ── 통합(결정론): 실물 regress7 산출물 + 원본 PDF 로 원장 패스 직접 실행 ────────
PDF_0047 = REPO / "input/pdf/DM_p0018-0047.pdf"
MD_R7 = REPO / "output/pdf_md_regress7_260714/DM_p0018-0047.md"
FRAG_LINE = "0.064 (run length ≡ 0)"


@pytest.mark.skipif(not (PDF_0047.exists() and MD_R7.exists()),
                    reason="regress7 픽스처 없음")
def test_registry_pass_migrates_real_0047(monkeypatch):
    """실물(구 R7 형식) MD 에 원장 패스 재적용 → p3 선두 파편 2행이 p2 TP 행으로
    이동(결정론 — LLM 0). OFF 면 파편 보존(기존 동작 복원)."""
    md = MD_R7.read_text(encoding="utf-8")

    def page_seg(text, pg):
        import re
        m = re.split(r"<!-- page (\d+) -->", text)
        # m = [pre, '1', seg1, '2', seg2, ...]
        for i in range(1, len(m), 2):
            if int(m[i]) == pg:
                return m[i + 1]
        return ""

    def first_table_leading_fragments(seg):
        """세그먼트 첫 GFM 표의 '선두 파편 데이터 행' 수(구조 판정 — 문자열 검색은
        p3 의 다른 정상 행(TN 등)에도 같은 값 문자열이 있어 오탐)."""
        lines = seg.split("\n")
        rows = []
        in_tbl = False
        for l in lines:
            if l.lstrip().startswith("|"):
                in_tbl = True
                if not x._XPAGE_SEP_RE.match(l):
                    rows.append(x._cc_cells(l))
            elif in_tbl:
                break
        data = rows[1:]   # 헤더 제외
        n = 0
        while n < len(data) and x._subrow_is_fragment(data[n]):
            n += 1
        return n

    # 전제(픽스처 형상): p3 첫 표 선두에 이월 파편 2행, TP 행은 p2
    assert first_table_leading_fragments(page_seg(md, 3)) == 2
    assert "| TP |" in page_seg(md, 2)

    # ON(기본): 파편이 p2 TP 행으로 이동
    out_on = x._xpage_registry_pass(md, str(PDF_0047))
    p2, p3 = page_seg(out_on, 2), page_seg(out_on, 3)
    assert first_table_leading_fragments(p3) == 0, "p3 선두 파편 제거"
    tp_line = next(l for l in p2.split("\n") if l.startswith("| TP |"))
    assert FRAG_LINE in tp_line and "0.140 (SRAM)" in tp_line, \
        "TP 행에 꼬리 값 병합(<br>)"
    assert "0.176 (SRAM)" in tp_line
    # 이동 = 삭제 아님: 파편 값 총량 보존(문서 전체 등장 횟수 불변)
    assert out_on.count("0.140 (SRAM)") == md.count("0.140 (SRAM)")

    # OFF: 파편 보존(= R7 기존 동작. 다른 원장 단계는 이미 적용된 md 라 멱등)
    monkeypatch.setenv("FMDW_SUBROW_MERGE", "0")
    out_off = x._xpage_registry_pass(md, str(PDF_0047))
    assert first_table_leading_fragments(page_seg(out_off, 3)) == 2, \
        "OFF = 이월 파편 보존(기존 동작)"
    tp_off = next(l for l in page_seg(out_off, 2).split("\n")
                  if l.startswith("| TP |"))
    assert "0.140 (SRAM)" not in tp_off
