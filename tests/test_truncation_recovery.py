"""test_truncation_recovery.py — 잘림 자동복구 페이지 단위 재추출 회귀 가드 (Fix 4, 2026-07-03).

실사고: `_maybe_recover_truncation` 이 조밀한 다중 페이지 chunk 를 단일 vision 호출로
재추출하다 OLLAMA_TIMEOUT(기본 600s)을 초과해 항상 실패하는 구조적 결함이 있었다
(실측: qwen3-vl:32b ~4분/페이지 × 3페이지 chunk 단일 호출 → 500|10m0s 타임아웃 2회,
재시도 5회 소진 후 원본(잘린) 텍스트가 그대로 유지됨). 이를 페이지 단위 재추출 +
순서대로 이어붙이기로 교체했다. 실제 네트워크/모델 호출 없이 `_ollama_vision` 을
mock 하여 검증한다.

실행:
    .venv/bin/python -m pytest tests/test_truncation_recovery.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fmdw import ollama_extractor as ox  # noqa: E402

# 주의: ExtractError 를 `from fmdw.ollama_extractor import ExtractError` 로 최상단에서
# 직접 바인딩하지 않는다. 다른 테스트 파일(test_config_sst.py 등)이 importlib.reload(ox)
# 를 수행하면 ollama_extractor 모듈 내부의 `except ExtractError` 는 reload 후의 새
# 클래스 객체를 참조하게 되지만, 최상단에서 직접 import 한 이름은 reload 이전 클래스에
# 고정된 채로 남아 isinstance 불일치(캐치 실패)가 발생한다(실측: 전체 스위트 실행 시
# 알파벳순으로 먼저 도는 test_config_sst.py 의 reload 뒤에 본 파일이 실행되며 재현됨).
# 항상 `ox.ExtractError` 로 호출 시점에 동적으로 참조해 최신 클래스와 일치시킨다.


def _degenerate_truncated_text() -> str:
    """트리거 조건(H-4 잘림 마커 AND degenerate 반복 둘 다 참)을 만족하는 최소 원본 텍스트."""
    return "\n".join(["| header | col |"] * 5) + "\n" + ox._TRUNCATION_MARKER


# ─────────────────────────────────────────────────────────────────────────────
# 1) 페이지 단위 재추출 성공 — 페이지 순서대로 "\n\n" 이어붙여 원본 교체
# ─────────────────────────────────────────────────────────────────────────────
def test_per_page_fallback_concatenates_in_order():
    original = _degenerate_truncated_text()
    images = ["img16", "img17", "img18"]
    page_results = ["## Page16 clean", "## Page17 clean", "## Page18 clean"]

    with patch.object(ox, "_ollama_vision", side_effect=page_results) as mo:
        result = ox._maybe_recover_truncation(
            original, "prompt", images, "structure", 16, 18,
        )

    assert result == "\n\n".join(page_results)
    assert mo.call_count == 3
    for call_args, expected_img in zip(mo.call_args_list, images):
        args, kwargs = call_args
        assert args[1] == [expected_img], "각 호출은 해당 페이지 이미지 1장만 전달해야 함"
        assert kwargs.get("model") == ox.FMDW_TRUNCATION_FALLBACK_MODEL
        assert kwargs.get("timeout_s") == ox.FMDW_TRUNCATION_FALLBACK_TIMEOUT
        assert kwargs.get("max_tokens") == ox.FMDW_TRUNCATION_FALLBACK_MAX_TOKENS


# ─────────────────────────────────────────────────────────────────────────────
# 1b) 페이지 단위 폴백 max_tokens 플럼스루 — env override 값도 그대로 전달돼야 함
# ─────────────────────────────────────────────────────────────────────────────
def test_per_page_fallback_uses_overridden_max_tokens():
    original = _degenerate_truncated_text()
    images = ["img16"]

    with patch.object(ox, "FMDW_TRUNCATION_FALLBACK_MAX_TOKENS", 12345), \
         patch.object(ox, "_ollama_vision", return_value="## Page16 clean") as mo:
        result = ox._maybe_recover_truncation(
            original, "prompt", images, "structure", 16, 16,
        )

    assert result == "## Page16 clean"
    _, kwargs = mo.call_args
    assert kwargs.get("max_tokens") == 12345


# ─────────────────────────────────────────────────────────────────────────────
# 2) 페이지 하나 실패(ExtractError) → 즉시 포기, 원본 유지(짜깁기 금지)
# ─────────────────────────────────────────────────────────────────────────────
def test_per_page_fallback_one_page_extract_error_keeps_original():
    original = _degenerate_truncated_text()
    images = ["img16", "img17", "img18"]

    with patch.object(
        ox, "_ollama_vision",
        side_effect=["## Page16 clean", ox.ExtractError("boom"), "## Page18 clean"],
    ) as mo:
        result = ox._maybe_recover_truncation(
            original, "prompt", images, "structure", 16, 18,
        )

    assert result == original
    # page 17(2번째 호출)에서 실패 → 즉시 중단, page 18 은 호출되지 않아야 함.
    assert mo.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# 3) 이어붙인 결과도 잘림 마커+반복 → 원본 유지
# ─────────────────────────────────────────────────────────────────────────────
def test_per_page_fallback_concat_still_degenerate_keeps_original():
    original = _degenerate_truncated_text()
    images = ["img16", "img17", "img18"]
    # 폴백 모델도 페이지마다 반복+잘림을 낸 상황을 시뮬레이션(최악 케이스).
    bad_page = "| header | col |\n| header | col |\n" + ox._TRUNCATION_MARKER

    with patch.object(ox, "_ollama_vision", side_effect=[bad_page] * 3) as mo:
        result = ox._maybe_recover_truncation(
            original, "prompt", images, "structure", 16, 18,
        )

    assert result == original
    assert mo.call_count == 3


# ─────────────────────────────────────────────────────────────────────────────
# 4) images_b64 개수 != 페이지 수 → 레거시 chunk 단위 단일 호출로 폴백
# ─────────────────────────────────────────────────────────────────────────────
def test_images_page_count_mismatch_falls_back_to_whole_chunk():
    original = _degenerate_truncated_text()
    images = ["img16", "img17"]  # 2장이지만 페이지 범위는 16~18(3페이지) → 불일치

    with patch.object(ox, "_ollama_vision", return_value="## Whole chunk clean") as mo:
        result = ox._maybe_recover_truncation(
            original, "prompt", images, "structure", 16, 18,
        )

    assert result == "## Whole chunk clean"
    # 레거시 경로는 chunk 전체(주어진 이미지 전부)를 단일 호출로 보낸다.
    assert mo.call_count == 1
    args, kwargs = mo.call_args
    assert args[1] == images
    assert kwargs.get("model") == ox.FMDW_TRUNCATION_FALLBACK_MODEL
    assert kwargs.get("timeout_s") == ox.FMDW_TRUNCATION_FALLBACK_TIMEOUT
    assert kwargs.get("max_tokens") == ox.FMDW_TRUNCATION_FALLBACK_MAX_TOKENS


# ─────────────────────────────────────────────────────────────────────────────
# 5) timeout_s 플럼스루 — _ollama_vision(..., timeout_s=N) 이 eff_timeout 을 override
# ─────────────────────────────────────────────────────────────────────────────
def _success_body(content: str = "## OK") -> dict:
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def test_ollama_vision_timeout_s_overrides_eff_timeout():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json = MagicMock(return_value=_success_body())

    with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
        mock_client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        result = ox._ollama_vision("prompt", [], timeout_s=123)

    assert result == "## OK"
    _, kwargs = MockClient.call_args
    assert kwargs.get("timeout") == 123, "timeout_s 가 eff_timeout 을 override 해야 함"


def test_ollama_vision_timeout_s_none_keeps_default_behavior():
    """timeout_s 미지정(None) 시 기존 OLLAMA_TIMEOUT 그대로 사용 — 회귀 없음(하위호환)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.json = MagicMock(return_value=_success_body())

    with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
        mock_client = MagicMock()
        MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
        MockClient.return_value.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        ox._ollama_vision("prompt", [])

    _, kwargs = MockClient.call_args
    assert kwargs.get("timeout") == ox.OLLAMA_TIMEOUT
