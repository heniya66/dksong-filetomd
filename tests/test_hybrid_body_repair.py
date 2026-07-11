"""test_hybrid_body_repair.py — 본문 하이브리드 전사(FMDW_BODY_HYBRID) 커버리지 폴백 로직 검증.

실제 네트워크/모델 호출 없이 ox.extract_pdf_pages 를 mock 하여 검증한다.
2026-07-04 갱신(Advisor QA Warning 수정 반영): _hybrid_extract_range 가 더 이상
chunk 범위 prompt 를 받지 않고 페이지마다 _build_transcription_prompt(page, page) 로
자기 자신의 절대 페이지 번호 프롬프트를 새로 만든다(<!-- page N --> 마커 정확도).
실행: .venv/bin/python -m pytest tests/test_hybrid_body_repair.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path.home() / "workspace" / "filetomd"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import extract_all_via_pdf as eap  # noqa: E402


def test_fallback_triggered_when_glm_skips_page_with_real_text():
    """pdf_text_len=3000, glm 출력 "" → qwen 폴백 호출됨."""
    calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        calls.append(model)
        # R1(2026-07-09): 기본 폴백이 glm-ocr 로 바뀌어 primary==fallback 일 수 있음 —
        # 모델명 대신 호출 순서(1차=primary, 2차=fallback)로 분기해야 계약이 유지된다.
        if len(calls) == 1:
            return ""
        return "REPAIRED CONTENT " * 50

    with patch.object(eap, "_pdf_page_text_len", return_value=3000), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 1, "prompt")

    assert calls == [eap.FMDW_BODY_PRIMARY_MODEL, eap.FMDW_BODY_FALLBACK_MODEL], \
        f"fallback(qwen) must be invoked after empty glm output, got {calls}"
    assert used == "qwen"
    assert md.startswith("REPAIRED")


def test_no_fallback_when_glm_covers_full_text():
    """pdf_text_len=3000, glm 출력 충분히 긺 → 폴백 미호출."""
    calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        calls.append(model)
        return "X" * 3000  # 100% coverage

    with patch.object(eap, "_pdf_page_text_len", return_value=3000), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 6, "prompt")

    assert calls == [eap.FMDW_BODY_PRIMARY_MODEL], \
        f"fallback must NOT be invoked when glm coverage is full, got {calls}"
    assert used == "glm"


def test_no_fallback_on_image_only_page_pdf_text_len_zero():
    """pdf_text_len=0(이미지 위주 페이지) + glm "" → MIN_TEXT 가드로 폴백 미호출."""
    calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        calls.append(model)
        return ""

    with patch.object(eap, "_pdf_page_text_len", return_value=0), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 11, "prompt")

    assert calls == [eap.FMDW_BODY_PRIMARY_MODEL], \
        f"fallback must NOT trigger for genuinely image-only page (pdf_text_len=0), got {calls}"
    assert used == "glm"
    assert md == ""


def test_glm_succeeds_but_qwen_fallback_fails_keeps_glm_result():
    """[QA 추가 1] glm 이 성공(다만 커버리지 미달로 폴백 트리거)했는데 qwen 폴백이
    예외를 던지면, 페이지 결과는 빈 값이 아니라 원본 glm 결과를 그대로 유지한다
    (실패 사유만 라벨에 남고 콘텐츠 유실 없음)."""
    calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        calls.append(model)
        # R1(2026-07-09): primary==fallback 가능 → 호출 순서로 분기(모델명 분기 금지).
        if len(calls) == 1:
            return "short glm output"  # 짧아서 커버리지 미달 → 폴백 트리거
        raise RuntimeError("qwen gateway timeout")

    with patch.object(eap, "_pdf_page_text_len", return_value=3000), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 4, "prompt")

    assert calls == [eap.FMDW_BODY_PRIMARY_MODEL, eap.FMDW_BODY_FALLBACK_MODEL]
    assert md == "short glm output", "qwen 실패 시 glm 결과를 그대로 보존해야 함(빈 값 아님)"
    assert used == "glm(fallback-failed)"


def test_best_of_keeps_glm_when_qwen_is_shorter():
    """[QA best-of 1, 2026-07-04 실사고] 실측: 밀집 TOC 페이지에서 qwen 이 비결정적으로
    glm(1411자) 보다 훨씬 짧은 56자만 반환한 사례. qwen 이 더 짧으면 절대 채택하지
    않고 glm 베이스라인을 유지해야 한다(폴백이 내용을 오히려 악화시키면 안 됨)."""
    glm_text = "G" * 1411
    qwen_text = "Q" * 56
    _calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        _calls.append(model)
        # R1(2026-07-09): primary==fallback 가능 → 호출 순서로 분기.
        if len(_calls) == 1:
            return glm_text  # 커버리지 미달(1411 < 0.30×3000)로 폴백은 트리거됨
        return qwen_text     # 하지만 glm 보다 짧음 → 채택 금지

    with patch.object(eap, "_pdf_page_text_len", return_value=5903), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 4, "prompt")

    assert md == glm_text, "qwen 이 더 짧으면 glm 베이스라인을 그대로 유지해야 함"
    assert used == "glm(qwen-shorter)", f"라벨이 glm 유지 사유를 명시해야 함, got {used!r}"


def test_best_of_adopts_qwen_when_meaningfully_longer():
    """[QA best-of 2] 같은 페이지가 다른 run 에서는 qwen 이 6136자(glm 1411자보다
    훨씬 김)를 반환한 사례 — 이번엔 qwen 이 실제로 더 완전하므로 채택해야 한다."""
    glm_text = "G" * 1411
    qwen_text = "Q" * 6136
    _calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        _calls.append(model)
        # R1(2026-07-09): primary==fallback 가능 → 호출 순서로 분기.
        if len(_calls) == 1:
            return glm_text
        return qwen_text

    with patch.object(eap, "_pdf_page_text_len", return_value=5903), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 4, "prompt")

    assert md == qwen_text, "qwen 이 glm 보다 실제로 더 길면 채택해야 함"
    assert used == "qwen"


def test_best_of_adopts_qwen_when_glm_empty():
    """[QA best-of 3] glm 이 완전히 빈 값(스킵)이고 qwen 이 정상 텍스트를 반환하면
    (glm_len=0 이므로 qwen 이 항상 더 길다) qwen 을 채택한다."""
    qwen_text = "REPAIRED " * 100
    _calls = []

    def fake_extract(prompt, pdf_path, start, end, model=None):
        _calls.append(model)
        # R1(2026-07-09): primary==fallback 가능 → 호출 순서로 분기.
        if len(_calls) == 1:
            return ""
        return qwen_text

    with patch.object(eap, "_pdf_page_text_len", return_value=5903), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        md, used = eap._hybrid_transcribe_page("/fake.pdf", 4, "prompt")

    assert md == qwen_text
    assert used == "qwen"


def test_glm_empty_and_qwen_fallback_fails_emits_missing_marker():
    """[QA 추가 2] glm 빈 출력 + qwen 폴백도 실패(None 반환)하면 그 페이지는 완전
    실패로 처리된다(_hybrid_transcribe_page 단위 확인). 범위 안에 다른 정상 페이지가
    있으면 _hybrid_extract_range 는 실패 페이지 자리에 인라인
    <!-- MISSING page N --> 마커를 남겨 무음(silent) 유실을 방지한다(내용이 그냥
    사라지지 않고 감사 가능하게 남음)."""
    def fake_extract(prompt, pdf_path, start, end, model=None):
        if start == 5:
            # p5: glm 빈 출력 + qwen 도 실패(예외 없이 빈 값 반환하는 경우까지 커버)
            return "" if model == eap.FMDW_BODY_PRIMARY_MODEL else None
        return "X" * 3000  # p6: 정상(커버리지 충분, kept-glm)

    with patch.object(eap, "_pdf_page_text_len", return_value=3000), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        # 단위 확인: p5 단독 호출은 (None, "failed").
        md5, used5 = eap._hybrid_transcribe_page("/fake.pdf", 5, "prompt")
        assert md5 is None
        assert used5 == "failed"

        # 범위 확인: p5(실패)+p6(정상) 혼합 시 p5 는 MISSING 마커, p6 는 내용 보존.
        result = eap._hybrid_extract_range("/fake.pdf", 5, 6)

    assert "<!-- MISSING page 5: extraction failed (hybrid) -->" in result, (
        "완전 실패 페이지는 침묵 유실이 아니라 감사 가능한 MISSING 마커로 남아야 함"
    )
    assert "X" * 3000 in result, "다른 정상 페이지(p6) 내용은 그대로 보존돼야 함"


def test_oversized_page_calls_neither_model():
    """오버사이즈 표 페이지는 placeholder 로 처리되어 primary/fallback 모두 호출 안 됨."""
    with patch.object(eap.ox, "extract_pdf_pages") as mo, \
         patch.object(eap, "_pdf_page_text_len") as mt:
        result = eap._oversized_placeholder(13)

    mo.assert_not_called()
    mt.assert_not_called()
    assert "13" in result
    assert "초대형 표" in result


def test_build_transcription_prompt_uses_given_range():
    """_build_transcription_prompt 는 인자로 준 [start,end] 범위 문구를 그대로 반영한다."""
    single = eap._build_transcription_prompt(4, 4)
    assert "pages 4 to 4" in single

    chunk = eap._build_transcription_prompt(1, 5)
    assert "pages 1 to 5" in chunk


def test_hybrid_extract_range_single_page_delegates():
    """단일 페이지 범위(start==end)는 자신의 프롬프트(_build_transcription_prompt(1,1))로
    _hybrid_transcribe_page 를 호출하고 그 결과를 그대로 반환한다."""
    with patch.object(eap, "_build_transcription_prompt",
                       return_value="PAGE1-PROMPT") as mbp, \
         patch.object(eap, "_hybrid_transcribe_page",
                       return_value=("PAGE1 MD", "glm")) as mp:
        result = eap._hybrid_extract_range("/fake.pdf", 1, 1)

    mbp.assert_called_once_with(1, 1)
    mp.assert_called_once_with("/fake.pdf", 1, "PAGE1-PROMPT")
    assert result == "PAGE1 MD"


def test_hybrid_extract_range_partial_failure_marks_missing():
    """멀티페이지 범위에서 한 페이지 실패 시 MISSING 마커 삽입 + 나머지는 결합.
    각 페이지 호출은 반드시 자기 자신의 프롬프트(_build_transcription_prompt(page,page))
    를 받아야 한다(청크 range 프롬프트 재사용 금지 — 마커 정확도 회귀 가드)."""
    def fake_build_prompt(start, end):
        assert start == end, "하이브리드 페이지별 프롬프트는 항상 start==end 여야 함"
        return f"PROMPT-{start}"

    def fake_page(pdf_path, page, prompt):
        assert prompt == f"PROMPT-{page}", (
            f"page {page} 호출은 자기 자신의 프롬프트를 받아야 함, got {prompt}"
        )
        if page == 2:
            return None, "failed"
        return (f"PAGE{page}", "glm")

    with patch.object(eap, "_build_transcription_prompt", side_effect=fake_build_prompt), \
         patch.object(eap, "_hybrid_transcribe_page", side_effect=fake_page):
        result = eap._hybrid_extract_range("/fake.pdf", 1, 3)

    assert "PAGE1" in result and "PAGE3" in result
    assert "MISSING page 2" in result


def test_hybrid_extract_range_multipage_prompt_matches_own_page_number():
    """[핵심 회귀 가드] EXTRACT_CHUNK_SIZE=5 같은 멀티페이지 청크에서, 각 페이지 호출의
    프롬프트가 청크 range("1 to 5")가 아니라 그 페이지 자신의 번호("N to N")를
    반영해야 <!-- page N --> 마커가 절대 페이지 번호와 어긋나지 않는다."""
    seen_prompts = {}

    def fake_extract(prompt, pdf_path, start, end, model=None):
        assert start == end, "하이브리드는 페이지당 이미지 1장만 렌더해야 함"
        seen_prompts[start] = prompt
        return "X" * 3000  # 커버리지 충분 → kept-glm(폴백 없이 로직 단순화)

    with patch.object(eap, "_pdf_page_text_len", return_value=3000), \
         patch.object(eap.ox, "extract_pdf_pages", side_effect=fake_extract):
        eap._hybrid_extract_range("/fake.pdf", 1, 5)

    assert set(seen_prompts.keys()) == {1, 2, 3, 4, 5}
    for page, prompt in seen_prompts.items():
        assert f"pages {page} to {page}" in prompt, (
            f"page {page} 프롬프트는 자기 자신의 번호를 반영해야 함: {prompt[:200]!r}"
        )
    # 구버전 버그 재현 방지: 청크 range 문구("1 to 5")가 그대로 남아있으면 안 된다.
    for page, prompt in seen_prompts.items():
        assert "pages 1 to 5" not in prompt


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
