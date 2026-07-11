"""test_truncation_repetition.py — intra-line degenerate repetition 회귀 가드.

_has_degenerate_repetition() 은 원래 라인간(inter-line) 반복만 감지했다(표 헤더 라인이
그대로 여러 번 출력되는 경우). 실제 잘림 사고(LN08LPU_Design_Manual testpages)에서는
단일 라인(37,252자) *내부*에서 구절("teaching evaluation" 류)이 275회 이상 반복되는
패턴이 감지되지 않아 truncation 자동복구(Fix 1, _maybe_recover_truncation)가 발동하지
않았다. 본 테스트는 그 회귀를 막는다.

실행:
    .venv/bin/python -m pytest tests/test_truncation_repetition.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fmdw import ollama_extractor as ox  # noqa: E402
from fmdw.ollama_extractor import _TRUNCATION_MARKER  # noqa: E402

# 실사고 재현 픽스처는 라이브 산출물(output/pdf_md/*.md)이 아니라 보존된 잘림 원본
# (.bad_20260703 백업)을 가리킨다. 라이브 산출물은 파이프라인을 다시 돌릴 때마다
# 내용이 바뀌는(이번 잘림 자동복구 수정으로 실제로 재변환되어 TRUNCATED 마커가 0건이
# 됨) 불안정 픽스처라 회귀 테스트 근거로 쓸 수 없다. .bad_20260703 는 잘림 사고
# 당시(2026-07-03) 상태로 고정 보존된 사본이라 TRUNCATED 마커 2건 + degenerate 라인
# (360, 371)이 항상 동일하게 유지된다.
_DESIGN_MANUAL_MD = _ROOT / "output" / "pdf_md" / (
    "LN08LPU_Design_Manual_A00-V0.9.2.0_testpages.md.bad_20260703"
)
_HSPICE_MD = _ROOT / "output" / "pdf_md" / (
    "LN08LPU_HSPICE_ModelGuide_A00-V0.9.2.1_testpages.md"
)


# ─────────────────────────────────────────────────────────────────────────────
# 1) 실제 사고 재현(regression) — intra-line 반복 감지
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.skipif(
    not _DESIGN_MANUAL_MD.exists(),
    reason=f"실제 잘림 샘플 파일 없음: {_DESIGN_MANUAL_MD}",
)
def test_real_truncated_chunk_detected_as_degenerate():
    """실제 잘린 chunk(직전 "---" 구분자 ~ 첫 TRUNCATED 마커)는 True 여야 한다.

    운영 코드(_maybe_recover_truncation)는 파일 전체가 아니라 단일 추출 chunk 단위로
    호출된다. 이 파일에서 chunk 경계는 "---" 구분자이므로, 문서 맨 앞부터의 prefix
    전체(다른 페이지의 반복 캡션 등을 포함해 기존 inter-line 로직이 우연히 True 를
    반환할 수 있음)가 아니라, 실제 잘린 chunk 하나만 슬라이스해 검증한다.
    """
    content = _DESIGN_MANUAL_MD.read_text(encoding="utf-8")
    idx = content.index(_TRUNCATION_MARKER)
    sep = "\n---\n"
    sep_idx = content.rfind(sep, 0, idx)
    assert sep_idx != -1, "청크 구분자(---)를 찾지 못함(파일 포맷 변경 여부 확인)"
    chunk = content[sep_idx + len(sep): idx]
    assert len(chunk) > 20000, "샘플 chunk 크기가 예상보다 작음(파일 변경 여부 확인)"
    assert ox._has_degenerate_repetition(chunk) is True


def test_synthetic_intraline_repetition_detected():
    """단일 라인 내부에 구절이 275회 반복되면 True (실제 사고의 최소 재현)."""
    line = "teaching evaluation teaching)) " * 275
    assert ox._has_degenerate_repetition(line) is True


def test_korean_intraline_repetition_detected():
    """한글 degenerate 반복(적대적 테스트 발견) — MIN_LEN 1000 하향 후 True.

    영문과 동일 반복 횟수라도 한글은 문자 밀도가 높아(120회 반복 ≈1,680자) 구
    임계값(2000자)에서는 감지되지 않았다(false negative). 실사용 corpus 가 한글
    문서 위주이므로 이 사각지대는 실전 위험이 크다.
    """
    line = "평가 교육 평가)) 교육 " * 120
    assert len(line) < 2000, "이 테스트는 구(2000자) 임계 미만 구간을 검증해야 함"
    assert len(line) >= 1000, "새(1000자) 임계는 넘어야 True 가 기대됨"
    assert ox._has_degenerate_repetition(line) is True


def test_korean_intraline_repetition_below_new_floor_still_false():
    """같은 한글 구절이라도 새 임계(1000자) 미만이면 여전히 False (플로어 가드 유지)."""
    line = "평가 교육 평가)) 교육 " * 60
    assert len(line) < 1000, "이 테스트는 새(1000자) 임계 미만 구간을 검증해야 함"
    assert ox._has_degenerate_repetition(line) is False


# ─────────────────────────────────────────────────────────────────────────────
# 2) 기존 inter-line 동작 보존
# ─────────────────────────────────────────────────────────────────────────────
def test_interline_repeat_at_threshold_still_true():
    text = "\n".join(["| header | col |"] * 5)
    assert ox._has_degenerate_repetition(text) is True


def test_interline_repeat_below_threshold_still_false():
    text = "\n".join(["| header | col |"] * 3)
    assert ox._has_degenerate_repetition(text) is False


# ─────────────────────────────────────────────────────────────────────────────
# 3) false-positive 가드 — 정상 dense 표는 절대 True 가 되면 안 됨
# ─────────────────────────────────────────────────────────────────────────────
def test_long_varied_table_row_not_flagged():
    """다른 값이 들어있는 300셀 표 행(3000자+, 공백 없음)은 False."""
    row = "".join(f"<td>V{i:04d}</td>" for i in range(300))
    assert len(row) > 3000
    assert ox._has_degenerate_repetition(row) is False


def test_long_identical_td_cells_without_whitespace_not_flagged():
    """동일 셀(<td>1</td>)이 다량 반복돼도 공백 없는 한 단일 토큰이라 False.

    실제 파일(LN08LPU_Design_Manual)의 정상(비-잘림) 표에도 이런 패턴이 존재한다
    (예: line 356) — intra-line 검출이 이를 오탐하면 안 된다.
    """
    row = "<td>1</td>" * 2000
    assert len(row) > 3000
    assert ox._has_degenerate_repetition(row) is False


@pytest.mark.skipif(
    not _HSPICE_MD.exists(),
    reason=f"HSPICE 비교 샘플 파일 없음: {_HSPICE_MD}",
)
def test_hspice_full_nontruncated_file_never_flagged():
    """TRUNCATED 마커가 없는 정상 문서의 각 라인에서 intra-line 오탐이 없어야 한다.

    주의: 전체 파일을 한 문자열로 이어붙여 검사하면 서로 다른 도면에 대해 동일한
    캡션 헤딩("### p2 — Figure 4")이 정확히 5회(기존 inter-line 임계값) 반복되는
    합법적 패턴 때문에 기존(본 수정 이전부터 존재하던) inter-line 로직이 True 를
    반환한다. 이 함수는 원래 단일 추출 chunk(page 구간)에 적용되는 것이지 다중
    페이지가 이어붙여진 최종 문서 전체에 적용되는 것이 아니므로, 여기서는 실제
    운영 단위인 '라인 단위' 오탐 여부만 검증한다(본 이슈의 intra-line 스코프).
    """
    content = _HSPICE_MD.read_text(encoding="utf-8")
    assert _TRUNCATION_MARKER not in content
    for i, line in enumerate(content.splitlines(), start=1):
        assert ox._has_degenerate_repetition(line) is False, (
            f"false positive at HSPICE line {i}"
        )


@pytest.mark.skipif(
    not _DESIGN_MANUAL_MD.exists(),
    reason=f"실제 잘림 샘플 파일 없음: {_DESIGN_MANUAL_MD}",
)
def test_design_manual_only_known_bad_lines_flagged():
    """Design Manual testpages 파일에서 degenerate 로 걸리는 라인은 알려진 잘림
    라인(360, 371 — 두 TRUNCATED 마커 직전 라인)뿐이어야 한다(오탐 스윕)."""
    lines = _DESIGN_MANUAL_MD.read_text(encoding="utf-8").splitlines()
    known_bad = {360, 371}
    flagged = {
        i for i, line in enumerate(lines, start=1)
        if ox._has_degenerate_repetition(line)
    }
    assert flagged == known_bad, f"unexpected flagged lines: {flagged - known_bad}"
