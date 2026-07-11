"""앙상블 vision QA 단위 테스트 — claude 호출 없이 mock 교정본으로 검증.

실행:
    python -m pytest tests/test_vision_qa_ensemble.py -v
    또는 stdlib만:  python tests/test_vision_qa_ensemble.py

claude vision verifier는 호출하지 않는다(빠른 결정적 단위 테스트). 대신
mock 교정본 MD를 직접 _parse_verdicts / _majority_vote / _build_merged_md 에
넣어 다수결 규칙과 형식 계약 보존을 확인한다. review_ensemble 통합 테스트는
vision_qa.review 를 monkeypatch 하여 claude 없이 mock run을 주입한다.
"""

from __future__ import annotations

import os
import sys

# 워크스페이스 루트를 path에 추가(lib 패키지 import).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import vision_qa_ensemble as ens  # noqa: E402
from fmdw import vision_qa as vqa  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# _parse_verdicts
# ──────────────────────────────────────────────────────────────────────────────

def test_parse_verdicts_value_unreadable_unverified_confirmed():
    md = (
        "### Figure 1: DDR2 decoupling\n"
        "| Designator | Value | Net |\n"
        "|---|---|---|\n"
        "| C71 | 22uF/16V/2012 | SOC_DDR_VTO to GND |\n"
        "| C81 | `[unreadable]` | VDD to GND |\n"
        "| R12 | 10K | `[unverified]` pull-up |\n"
        "| U24 | | reset supervisor |\n"
    )
    v = ens._parse_verdicts(md)
    # _parse_verdicts는 이제 _LineVerdict(verdict + raw_value)를 반환(C-5).
    assert v["C71"].verdict == ens._normalize_value("22uF"), v
    # C-5: 원형 토큰(대소문자) 보존 확인.
    assert v["C71"].raw_value == "22uF", v
    assert v["C81"].verdict == ens.V_UNREADABLE, v
    # R12: 값(10K) 있으나 [unverified] 플래그도 있음 → unverified로 강등.
    assert v["R12"].verdict == ens.V_UNVERIFIED, v
    # U24: 값/플래그 없음 → confirmed.
    assert v["U24"].verdict == ens.V_CONFIRMED, v
    print("PASS test_parse_verdicts_value_unreadable_unverified_confirmed")


def test_parse_verdicts_bga_ball_and_no_partial_match():
    md = (
        "| U24.M8 | DDR_DQ0 |\n"
        "| CON2 | header |\n"        # CON2는 designator 정규식에 안 잡혀야(라벨)
        "- DDR_DQ48 routed to U7\n"  # U7만 designator, DDR_DQ48은 net
    )
    v = ens._parse_verdicts(md)
    assert "U24.M8" in v, v
    assert "U7" in v, v
    # CON2는 designator로 잡지 않는다(C\d+ 패턴이 'CON2'의 일부를 먹지 않게).
    assert "C2" not in v, v
    print("PASS test_parse_verdicts_bga_ball_and_no_partial_match")


# ──────────────────────────────────────────────────────────────────────────────
# _majority_vote — 핵심 케이스
# ──────────────────────────────────────────────────────────────────────────────

def test_majority_c71_two_unreadable():
    # (a) C71: [22uF, [unreadable], [unreadable]] → [unreadable] 채택.
    votes = [ens._normalize_value("22uF"), ens.V_UNREADABLE, ens.V_UNREADABLE]
    assert ens._majority_vote(votes) == ens.V_UNREADABLE
    print("PASS test_majority_c71_two_unreadable")


def test_majority_value_conflict_2to1():
    # (b) 값 충돌 [22uF, 220uF, 22uF] → 22uF (2/3 다수).
    a = ens._normalize_value("22uF")
    b = ens._normalize_value("220uF")
    votes = [a, b, a]
    assert ens._majority_vote(votes) == a
    print("PASS test_majority_value_conflict_2to1")


def test_majority_three_way_conflict():
    # (c) 3원 충돌 [22uF, 220uF, 2.2uF] → [unverified].
    votes = [
        ens._normalize_value("22uF"),
        ens._normalize_value("220uF"),
        ens._normalize_value("2.2uF"),
    ]
    assert ens._majority_vote(votes) == ens.V_UNVERIFIED
    print("PASS test_majority_three_way_conflict")


def test_majority_two_confirmed():
    votes = [ens.V_CONFIRMED, ens.V_CONFIRMED, ens.V_UNVERIFIED]
    assert ens._majority_vote(votes) == ens.V_CONFIRMED
    print("PASS test_majority_two_confirmed")


def test_majority_two_same_value_confirmed():
    # 단일 값 1표 + confirmed 2표 → confirmed 2표 다수이므로 confirmed 채택.
    # (값은 1표뿐 — 2+ 동의 미충족 → 값 채택 안 됨. confirmed 2+ 조건에 해당.)
    votes = [ens._normalize_value("22uF"), ens.V_CONFIRMED, ens.V_CONFIRMED]
    assert ens._majority_vote(votes) == ens.V_CONFIRMED
    # 값1 + unverified1 + unreadable1 → 어느 판정도 2+ 동의 없음 → unverified.
    votes2 = [ens._normalize_value("22uF"), ens.V_UNVERIFIED, ens.V_UNREADABLE]
    assert ens._majority_vote(votes2) == ens.V_UNVERIFIED
    print("PASS test_majority_two_same_value_confirmed")


def test_majority_absent_excluded():
    # absent run은 투표 제외 — 나머지 2 unreadable → unreadable.
    votes = [ens.V_ABSENT, ens.V_UNREADABLE, ens.V_UNREADABLE]
    assert ens._majority_vote(votes) == ens.V_UNREADABLE
    print("PASS test_majority_absent_excluded")


# ──────────────────────────────────────────────────────────────────────────────
# 통합 MD — 형식 계약 보존 + 중복 제거(C81/C82)
# ──────────────────────────────────────────────────────────────────────────────

def _mk_run(c71_val: str, dup_c81: bool) -> str:
    """테스트용 mock 교정본 MD 생성."""
    lines = [
        "### Figure 1: DDR2 decoupling bank",
        "**Type**: circuit schematic",
        "",
        "| Designator | Value | Net |",
        "|---|---|---|",
        f"| C71 | {c71_val} | SOC_DDR_VTO to GND |",
        "| C81 | 0.1uF | VDD to GND |",
    ]
    if dup_c81:
        lines.append("| C81 | 0.1uF | VDD to GND |")  # 중복 나열
    lines.append("| C82 | 0.1uF | VDD to GND |")
    return "\n".join(lines)


def test_merged_md_preserves_figure_and_dedupes():
    # (d) C81 중복: 다수 run이 중복 나열 → 통합본에서 1회만.
    # C71: 2 run [unreadable], 1 run 값 → [unreadable] 채택.
    run1 = _mk_run("`[unreadable]`", dup_c81=True)
    run2 = _mk_run("`[unreadable]`", dup_c81=True)
    run3 = _mk_run("22uF", dup_c81=False)
    run_mds = [run1, run2, run3]
    vmaps = [ens._parse_verdicts(m) for m in run_mds]
    final, _, raw_map = ens._tally_votes(vmaps)
    merged = ens._build_merged_md(run_mds, vmaps, final, raw_map)

    # 형식 계약: ### Figure 보존.
    assert "### Figure 1" in merged, merged
    assert "**Type**" in merged, merged
    # C81 중복 항목 라인 1회만.
    c81_item_lines = [
        ln for ln in merged.splitlines()
        if ens._is_item_line(ln) and ens._scan_line_designators(ln) == ["C81"]
    ]
    assert len(c81_item_lines) == 1, c81_item_lines
    # C71 다수결: [unreadable].
    assert final["C71"] == ens.V_UNREADABLE, final
    # 통합본 C71 라인에 [unreadable] 표기, 22uF 같은 잘못된 값 단정 없음.
    c71_line = next(
        ln for ln in merged.splitlines()
        if ens._scan_line_designators(ln) == ["C71"]
    )
    assert "[unreadable]" in c71_line.lower(), c71_line
    print("PASS test_merged_md_preserves_figure_and_dedupes")


def test_merged_md_base_line_preserved_when_unmatched():
    # 다수결에 없는 라인/텍스트는 베이스 그대로.
    run1 = "### Figure 1\n| C71 | 22uF | net |\nSome prose with no designator here.\n"
    run2 = "### Figure 1\n| C71 | 22uF | net |\nDifferent prose line.\n"
    run_mds = [run1, run2]
    vmaps = [ens._parse_verdicts(m) for m in run_mds]
    final, _, raw_map = ens._tally_votes(vmaps)
    merged = ens._build_merged_md(run_mds, vmaps, final, raw_map)
    assert "### Figure 1" in merged
    # 베이스 prose 라인 보존(어느 run이 base든 그 prose가 그대로).
    assert ("Some prose" in merged) or ("Different prose" in merged), merged
    # C71 2 run 동일 값 → 값 유지.
    assert final["C71"] == ens._normalize_value("22uF"), final
    print("PASS test_merged_md_base_line_preserved_when_unmatched")


def test_value_replaced_when_majority_value():
    # 베이스 라인의 잘못된 값이 다수결 값으로 치환되는지(단일 designator 라인).
    # C-5: raw_value(원형)로 치환되어야 — 정규화 강등 금지.
    base = "| C71 | 220uF | net |"
    line = ens._apply_verdict_to_line(
        base, "C71", ens._normalize_value("22uF"),
        single_designator=True, raw_value="22uF",
    )
    # 원형 대문자 'uF' 보존(정규화 'uf'로 강등되지 않음).
    assert "22uF" in line and "220uF" not in line, line
    print("PASS test_value_replaced_when_majority_value")


# ──────────────────────────────────────────────────────────────────────────────
# Advisor QA 지적 7개 실패모드 회귀 가드 (C-1 ~ C-5)
# ──────────────────────────────────────────────────────────────────────────────

def test_c1_multi_designator_row_no_garble():
    """C-1: 다중 designator 표 행 값 치환 → 행 구조 보존(셀 붕괴 금지).

    재현: `| C81 | C82 | 0.1uF |` (C81→0.22uf, C82→0.47uf 판정). 단일 designator
    아님 → 값 치환 금지. 행 파이프 구조 유지, 각 셀 베이스 그대로.
    """
    line = "| C81 | C82 | 0.1uF |"
    # 두 designator 각각 다른 구체값으로 판정해도(=다중 라인) 치환 안 됨.
    out = ens._apply_verdict_to_line(
        line, "C81", ens._normalize_value("0.22uF"),
        single_designator=False, raw_value="0.22uF",
    )
    out = ens._apply_verdict_to_line(
        out, "C82", ens._normalize_value("0.47uF"),
        single_designator=False, raw_value="0.47uF",
    )
    # 행 구조 보존(파이프 4개) + 어떤 값도 잘못된 셀로 치환되지 않음.
    assert out.count("|") == 4, out
    assert "C81" in out and "C82" in out and "0.1uF" in out, out
    # garbling 패턴(`C82 0.47uf` 같은 셀 결합) 부재.
    assert "0.22uf" not in out.lower() and "0.47uf" not in out.lower(), out

    # unverified 플래그는 라인 단위로만 부착 가능(값 치환 없이 구조 유지).
    flagged = ens._apply_verdict_to_line(
        line, "C81", ens.V_UNVERIFIED, single_designator=False)
    flagged = ens._apply_verdict_to_line(
        flagged, "C82", ens.V_UNVERIFIED, single_designator=False)
    assert flagged.count("|") == 4, flagged
    print("PASS test_c1_multi_designator_row_no_garble")


def test_c1_range_line_no_value_substitution():
    """C-1/C-6: 범위 라인(R91-R112)은 _RANGE_RE로 감지되어 값 치환 제외."""
    base_md = (
        "### Figure 1\n"
        "| Designator | Value |\n"
        "|---|---|\n"
        "| R91-R112 | 10K |\n"
    )
    runs = [base_md, base_md]
    vmaps = [ens._parse_verdicts(m) for m in runs]
    final, _, raw = ens._tally_votes(vmaps)
    merged = ens._build_merged_md(runs, vmaps, final, raw)
    row = next(ln for ln in merged.splitlines() if "R91" in ln)
    # 범위 라인 구조 보존(셀 붕괴/치환 없음).
    assert "R91-R112" in row, row
    assert row.count("|") == 3, row
    assert ens._is_range_line("| R91-R112 | 10K |") is True
    print("PASS test_c1_range_line_no_value_substitution")


def test_c2_ic_pinmap_rows_preserved():
    """C-2: IC 핀맵 `U24|pin1->NET_A`, `U24|pin2->NET_B` 두 행 모두 보존.

    designator만 같고 셀 내용이 다른 행은 dedupe 대상 아님(per-pin PIN->NET 보존).
    완전 동일 행만 1회로 축약.
    """
    md = (
        "### Figure 1\n"
        "**Type**: pinmap\n"
        "| Ref | Pin | Net |\n"
        "|---|---|---|\n"
        "| U24 | pin1 | NET_A |\n"
        "| U24 | pin2 | NET_B |\n"
    )
    runs = [md, md]
    vmaps = [ens._parse_verdicts(m) for m in runs]
    final, _, raw = ens._tally_votes(vmaps)
    merged = ens._build_merged_md(runs, vmaps, final, raw)
    u24_rows = [
        ln for ln in merged.splitlines()
        if ens._is_item_line(ln) and ens._scan_line_designators(ln) == ["U24"]
    ]
    # 두 핀 행 모두 보존(셀 내용 다름 → dedupe 금지).
    assert len(u24_rows) == 2, u24_rows
    assert any("pin1" in r and "NET_A" in r for r in u24_rows), u24_rows
    assert any("pin2" in r and "NET_B" in r for r in u24_rows), u24_rows

    # 반대로: 완전 동일 행(C81 두 번)은 1회로 dedupe.
    dup = ["| C81 | 0.1uF | GND |", "| C81 | 0.1uF | GND |"]
    deduped = ens._dedupe_designator_lines(dup)
    assert len([l for l in deduped if "C81" in l]) == 1, deduped
    print("PASS test_c2_ic_pinmap_rows_preserved")


def test_c3_comment_four_digit_not_replaced():
    """C-3: 주석 4자리(`2012`) 포함 행 → 값 셀만 치환, 주석 불변.

    재현: `| C71 (see 2012 note) | 16V |` (final 3.3v). 컬럼 인식 없이 첫 토큰을
    잡으면 주석 `2012`를 오교정 → 값 셀(`16V`)만 치환되어야 한다.
    """
    line = "| C71 (see 2012 note) | 16V | SOC to GND |"
    out = ens._apply_verdict_to_line(
        line, "C71", ens._normalize_value("3.3V"),
        single_designator=True, raw_value="3.3V",
    )
    # 주석 보존, 값 셀만 치환.
    assert "(see 2012 note)" in out, out
    assert "16V" not in out, out
    assert "| 3.3V |" in out, out
    # 분류 단계도 값 셀(16V)을 잡아야(주석 2012 아님).
    lv = ens._classify_line_for(line, "C71")
    assert lv.raw_value == "16V", lv
    print("PASS test_c3_comment_four_digit_not_replaced")


def test_c4_resistor_kilo_conflict_detected():
    """C-4: run간 `10K`↔`22K` 저항 충돌 → [unverified] 감지(미감지 회귀 가드).

    이전: `10K`/`22K`가 값으로 추출 안 돼 둘 다 confirmed → 충돌 미감지. 수정 후
    bare kilo 추출되어 다수결에서 충돌 시 [unverified].
    """
    def mkr(val):
        return f"### Figure 1\n| Designator | Value |\n|---|---|\n| R12 | {val} |\n"

    # 값 추출 자체 확인(net 오탐 없이).
    assert ens._extract_values("| R12 | 10K | pull-up |") == ["10K"]
    assert ens._extract_values("| R12 | 22K | pull-up |") == ["22K"]
    # net 이름 오탐 방지: CLK10/10KB 등은 값 아님.
    assert ens._extract_values("| U1 | CLK10 | clock |") == []
    assert ens._extract_values("| U1 | 10KB | mem |") == []

    # 10K vs 22K 충돌(다수 없음) → unverified.
    runs = [mkr("10K"), mkr("22K")]
    vmaps = [ens._parse_verdicts(m) for m in runs]
    final, _, _ = ens._tally_votes(vmaps)
    assert final["R12"] == ens.V_UNVERIFIED, final
    print("PASS test_c4_resistor_kilo_conflict_detected")


def test_c5_raw_value_casing_preserved_through_merge():
    """C-5: `220uF` 원형 대소문자 보존(정규화 강등 `22uf`/`220uf` 방지).

    다수결 채택 값의 raw_value를 끝까지 운반해 출력해야 한다.
    """
    def mk(v):
        return f"### Figure 1\n| Designator | Value |\n|---|---|\n| C71 | {v} |\n"

    runs = [mk("220uF"), mk("220uF")]
    vmaps = [ens._parse_verdicts(m) for m in runs]
    final, _, raw = ens._tally_votes(vmaps)
    # 내부 비교용 정규화는 소문자지만 raw_map은 원형 보존.
    assert final["C71"] == ens._normalize_value("220uF"), final
    assert raw.get("C71") == "220uF", raw
    merged = ens._build_merged_md(runs, vmaps, final, raw)
    c71 = next(ln for ln in merged.splitlines() if "C71" in ln)
    # 원형 대문자 'uF' 보존(소문자 강등 아님).
    assert "220uF" in c71, c71

    # 베이스 값이 틀려 치환될 때도 원형으로 치환.
    base = "| C71 | 470uF | net |"
    out = ens._apply_verdict_to_line(
        base, "C71", ens._normalize_value("220uF"),
        single_designator=True, raw_value="220uF",
    )
    assert "220uF" in out and "470uF" not in out, out
    print("PASS test_c5_raw_value_casing_preserved_through_merge")


# ──────────────────────────────────────────────────────────────────────────────
# review_ensemble 통합 — vision_qa.review monkeypatch (claude 호출 없음)
# ──────────────────────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, md, corrected=True, note="mock"):
        self.markdown = md
        self.corrected = corrected
        self.backend = "mock"
        self.note = note


def test_review_ensemble_majority_via_monkeypatch(monkeypatch):
    primary = _mk_run("22uF", dup_c81=False)  # 1차(틀린 값 가정)
    runs = [
        _mk_run("`[unreadable]`", dup_c81=True),
        _mk_run("`[unreadable]`", dup_c81=True),
        _mk_run("22uF", dup_c81=False),
    ]
    seq = iter(runs)

    # H-6: 렌더는 1회만(render_qa_pngs), verifier는 N회(review_with_pngs).
    def fake_render(pdf_path, start, end, dpi=None):
        return ([__import__("pathlib").Path("/tmp/fake_p1.png")], "page 1", 220)

    def fake_review_with_pngs(primary_md, png_paths, page_label, use_dpi=None):
        return _FakeResult(next(seq))

    monkeypatch.setattr(ens._vqa, "is_enabled", lambda: True)
    monkeypatch.setattr(ens._vqa, "render_qa_pngs", fake_render)
    monkeypatch.setattr(ens._vqa, "review_with_pngs", fake_review_with_pngs)
    monkeypatch.setattr(ens._vqa, "cleanup_pngs", lambda paths: None)

    res = ens.review_ensemble(primary, "dummy.pdf", 1, 1, n=3)
    assert res.corrected is True, res.note
    assert "ensemble n=3" in res.note, res.note
    assert "### Figure 1" in res.markdown
    # C71 다수결 [unreadable] 반영.
    c71_line = next(
        ln for ln in res.markdown.splitlines()
        if ens._scan_line_designators(ln) == ["C71"]
    )
    assert "[unreadable]" in c71_line.lower(), c71_line
    print("PASS test_review_ensemble_majority_via_monkeypatch")


def test_review_ensemble_degrade_when_under_two_runs(monkeypatch):
    primary = _mk_run("22uF", dup_c81=False)
    # 1개만 성공, 나머지 degrade(corrected=False).
    results = [
        _FakeResult(_mk_run("`[unreadable]`", dup_c81=False), corrected=True),
        _FakeResult(primary, corrected=False, note="degrade"),
        _FakeResult(primary, corrected=False, note="degrade"),
    ]
    seq = iter(results)

    def fake_render(pdf_path, start, end, dpi=None):
        return ([__import__("pathlib").Path("/tmp/fake_p1.png")], "page 1", 220)

    def fake_review_with_pngs(primary_md, png_paths, page_label, use_dpi=None):
        return next(seq)

    monkeypatch.setattr(ens._vqa, "is_enabled", lambda: True)
    monkeypatch.setattr(ens._vqa, "render_qa_pngs", fake_render)
    monkeypatch.setattr(ens._vqa, "review_with_pngs", fake_review_with_pngs)
    monkeypatch.setattr(ens._vqa, "cleanup_pngs", lambda paths: None)

    res = ens.review_ensemble(primary, "dummy.pdf", 1, 1, n=3)
    assert res.corrected is False, res.note
    assert res.markdown == primary, "degrade 시 1차 MD 그대로여야"
    assert "< 2" in res.note, res.note
    print("PASS test_review_ensemble_degrade_when_under_two_runs")


def test_review_ensemble_renders_png_once_for_n_runs(monkeypatch):
    """H-6 회귀 가드: 앙상블이 페이지 PNG를 **1회만** 렌더하고 N회 verifier 재사용.

    검증:
      (1) render_qa_pngs 호출 횟수 == 1 (이전: N회 = review() 내부 N렌더).
      (2) review_with_pngs 호출 횟수 == n, 매 호출이 **동일 PNG 경로 객체**를 받음.
      (3) cleanup_pngs 가 렌더된 그 경로로 정확히 1회 호출(누수 없음).
    """
    from pathlib import Path as _P

    primary = _mk_run("22uF", dup_c81=False)
    runs = [
        _mk_run("`[unreadable]`", dup_c81=True),
        _mk_run("`[unreadable]`", dup_c81=True),
        _mk_run("22uF", dup_c81=False),
    ]
    seq = iter(runs)

    sentinel_paths = [_P("/tmp/fake_p1.png")]
    counters = {"render": 0, "review": 0, "cleanup": 0}
    seen_png_args = []
    cleaned_with = []

    def fake_render(pdf_path, start, end, dpi=None):
        counters["render"] += 1
        return (sentinel_paths, "page 1", 220)

    def fake_review_with_pngs(primary_md, png_paths, page_label, use_dpi=None):
        counters["review"] += 1
        seen_png_args.append(png_paths)
        return _FakeResult(next(seq))

    def fake_cleanup(paths):
        counters["cleanup"] += 1
        cleaned_with.append(paths)

    monkeypatch.setattr(ens._vqa, "is_enabled", lambda: True)
    monkeypatch.setattr(ens._vqa, "render_qa_pngs", fake_render)
    monkeypatch.setattr(ens._vqa, "review_with_pngs", fake_review_with_pngs)
    monkeypatch.setattr(ens._vqa, "cleanup_pngs", fake_cleanup)

    res = ens.review_ensemble(primary, "dummy.pdf", 1, 1, n=3)

    # (1) 렌더 정확히 1회(N→1).
    assert counters["render"] == 1, counters
    # (2) verifier 는 n회, 매번 동일 PNG 객체 재사용.
    assert counters["review"] == 3, counters
    assert all(p is sentinel_paths for p in seen_png_args), seen_png_args
    # (3) 정리 정확히 1회 + 렌더된 경로로.
    assert counters["cleanup"] == 1, counters
    assert cleaned_with == [sentinel_paths], cleaned_with
    # 동작 보존: 다수결 결과는 동일(C71 [unreadable]).
    assert res.corrected is True, res.note
    c71_line = next(
        ln for ln in res.markdown.splitlines()
        if ens._scan_line_designators(ln) == ["C71"]
    )
    assert "[unreadable]" in c71_line.lower(), c71_line
    print("PASS test_review_ensemble_renders_png_once_for_n_runs")


def test_review_ensemble_result_identical_old_vs_new_render(monkeypatch):
    """H-6 동작 보존 입증: 렌더 1회(신규) 결과 == 렌더 N회(기존) 결과.

    같은 verifier 교정본 시퀀스가 주어졌을 때, '페이지를 N번 재렌더하던 기존 구조'와
    '1번 렌더 후 재사용하는 신규 구조'의 **다수결 통합 MD가 byte-동일**해야 한다.
    (렌더는 결정적이므로 N렌더 == 1렌더 — 본 테스트는 통합 경로 동일성을 고정한다.)
    """
    from pathlib import Path as _P

    primary = _mk_run("22uF", dup_c81=False)
    verifier_outputs = [
        _mk_run("`[unreadable]`", dup_c81=True),
        _mk_run("`[unreadable]`", dup_c81=True),
        _mk_run("22uF", dup_c81=False),
    ]

    # --- 신규 경로(렌더 1회 + review_with_pngs N회) ---
    seq_new = iter(verifier_outputs)
    monkeypatch.setattr(ens._vqa, "is_enabled", lambda: True)
    monkeypatch.setattr(
        ens._vqa, "render_qa_pngs",
        lambda pdf, s, e, dpi=None: ([_P("/tmp/p1.png")], "page 1", 220),
    )
    monkeypatch.setattr(
        ens._vqa, "review_with_pngs",
        lambda md, pngs, label, use_dpi=None: _FakeResult(next(seq_new)),
    )
    monkeypatch.setattr(ens._vqa, "cleanup_pngs", lambda paths: None)
    res_new = ens.review_ensemble(primary, "dummy.pdf", 1, 1, n=3)

    # --- 기준(동일 verifier 출력으로 _merge_runs 직접 호출 = 통합 로직 참조값) ---
    ref = ens._merge_runs(primary, list(verifier_outputs), failed=0, n=3,
                          backend=ens._vqa.backend_label())

    assert res_new.markdown == ref.markdown, "신규 렌더 경로 통합 MD != 참조 통합 MD"
    print("PASS test_review_ensemble_result_identical_old_vs_new_render")


def test_review_ensemble_disabled_noop(monkeypatch):
    monkeypatch.setattr(ens._vqa, "is_enabled", lambda: False)
    res = ens.review_ensemble("primary md", "dummy.pdf", 1, 1, n=3)
    assert res.corrected is False
    assert res.markdown == "primary md"
    print("PASS test_review_ensemble_disabled_noop")


# ──────────────────────────────────────────────────────────────────────────────
# stdlib runner (pytest 없이도 실행 가능)
# ──────────────────────────────────────────────────────────────────────────────

def _run_all():
    """pytest 미설치 환경용 간이 러너 — monkeypatch는 수동 구현."""
    class _MP:
        def __init__(self):
            self._undo = []

        def setattr(self, obj, name, val):
            old = getattr(obj, name)
            self._undo.append((obj, name, old))
            setattr(obj, name, val)

        def undo(self):
            for obj, name, old in reversed(self._undo):
                setattr(obj, name, old)

    plain = [
        test_parse_verdicts_value_unreadable_unverified_confirmed,
        test_parse_verdicts_bga_ball_and_no_partial_match,
        test_majority_c71_two_unreadable,
        test_majority_value_conflict_2to1,
        test_majority_three_way_conflict,
        test_majority_two_confirmed,
        test_majority_two_same_value_confirmed,
        test_majority_absent_excluded,
        test_merged_md_preserves_figure_and_dedupes,
        test_merged_md_base_line_preserved_when_unmatched,
        test_value_replaced_when_majority_value,
        test_c1_multi_designator_row_no_garble,
        test_c1_range_line_no_value_substitution,
        test_c2_ic_pinmap_rows_preserved,
        test_c3_comment_four_digit_not_replaced,
        test_c4_resistor_kilo_conflict_detected,
        test_c5_raw_value_casing_preserved_through_merge,
    ]
    mp_tests = [
        test_review_ensemble_majority_via_monkeypatch,
        test_review_ensemble_degrade_when_under_two_runs,
        test_review_ensemble_disabled_noop,
        test_review_ensemble_renders_png_once_for_n_runs,
        test_review_ensemble_result_identical_old_vs_new_render,
    ]
    failed = 0
    for t in plain:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    for t in mp_tests:
        mp = _MP()
        try:
            t(mp)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        finally:
            mp.undo()
    total = len(plain) + len(mp_tests)
    print(f"\n{total - failed}/{total} passed")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
