"""test_ensemble_mode.py — 앙상블(--ensemble) 모드 단위 테스트 (2026-06-25).

실제 네트워크/Ollama 호출 없이 검증한다:
  - config knob: 미설정→비활성/기본, EXTRACT_ENSEMBLE=1→활성, 모델 리스트 콤마 파싱.
  - _ensemble_vision: ENSEMBLE_MODELS 각 모델 N회 호출 → MERGER 1회 병합 호출
    (순서/모델/이미지 빈배열 검증). monkeypatch 로 _ollama_vision 을 가짜로 대체.
  - graceful: 모델 1개뿐 → 병합 skip(단일 반환). 1개 빈출력 → 나머지로 병합.
    전부 빈출력/실패 → ExtractError.
  - 병합 프롬프트 구성: 두 본문 + 통합본/상호보완 지시 포함.
  - 회귀: extract_pdf_pages 가 ENSEMBLE_ENABLED=False 일 때 _ollama_vision 직행,
    role!='structure' 이면 앙상블 우회.

실행:
    .venv/bin/python -m pytest tests/test_ensemble_mode.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

# 워크스페이스 루트를 sys.path 에 추가(fmdw.* 패키지 import 보장).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import config as cfg  # noqa: E402
from fmdw import ollama_extractor as ox  # noqa: E402

# 주의: ExtractError 는 모듈 속성(ox.ExtractError)으로만 참조한다.
# test_config_sst.py 가 importlib.reload(ox) 로 모듈을 재로드하면 ExtractError 클래스가
# 새 객체로 교체되는데, 상단에서 `from ... import ExtractError` 로 박제하면 stale 참조가 되어
# `except ExtractError` / `assertRaises(ExtractError)` 가 클래스 불일치로 깨진다(테스트 순서
# 의존). 항상 ox.ExtractError 를 쓰면 reload 후에도 현재 모듈의 클래스와 일치한다.


# ──────────────────────────────────────────────────────────────────────────────
# config knob 동작 (env 우선순위 + 파싱)
# ──────────────────────────────────────────────────────────────────────────────
class TestEnsembleKnobs(unittest.TestCase):
    def setUp(self):
        # 테스트 격리: 관련 env 를 모두 비운 상태에서 시작.
        self._saved = {
            k: os.environ.pop(k, None)
            for k in ("EXTRACT_ENSEMBLE", "FMDW_ENSEMBLE_MODELS", "FMDW_ENSEMBLE_MERGER")
        }

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_default_disabled(self):
        """미설정 → 비활성(False)."""
        self.assertFalse(cfg.knob_ensemble_enabled())

    def test_default_models_and_merger(self):
        """미설정 → 코드기본 모델 리스트 + merger."""
        self.assertEqual(cfg.knob_ensemble_models(), ["qwen3-vl:32b", "gemma4:31b"])
        self.assertEqual(cfg.knob_ensemble_merger(), "qwen3-vl:32b")

    def test_env_enables(self):
        """EXTRACT_ENSEMBLE=1/true/on → 활성, 0/false → 비활성."""
        for truthy in ("1", "true", "on", "yes", "TRUE"):
            os.environ["EXTRACT_ENSEMBLE"] = truthy
            self.assertTrue(cfg.knob_ensemble_enabled(), f"{truthy!r} should enable")
        for falsy in ("0", "false", "off", "no", ""):
            os.environ["EXTRACT_ENSEMBLE"] = falsy
            self.assertFalse(cfg.knob_ensemble_enabled(), f"{falsy!r} should disable")

    def test_env_model_list_parsing(self):
        """FMDW_ENSEMBLE_MODELS 콤마 파싱 — strip + 빈값 제거."""
        os.environ["FMDW_ENSEMBLE_MODELS"] = " modelA , modelB ,, ,modelC "
        self.assertEqual(cfg.knob_ensemble_models(), ["modelA", "modelB", "modelC"])

    def test_env_model_list_empty_falls_back(self):
        """env 가 빈/콤마뿐이면 코드기본값 폴백."""
        os.environ["FMDW_ENSEMBLE_MODELS"] = " , , "
        self.assertEqual(cfg.knob_ensemble_models(), ["qwen3-vl:32b", "gemma4:31b"])

    def test_env_merger_override(self):
        os.environ["FMDW_ENSEMBLE_MERGER"] = "  custom-merger  "
        self.assertEqual(cfg.knob_ensemble_merger(), "custom-merger")

    def test_env_merger_empty_falls_back(self):
        os.environ["FMDW_ENSEMBLE_MERGER"] = "   "
        self.assertEqual(cfg.knob_ensemble_merger(), "qwen3-vl:32b")


# ──────────────────────────────────────────────────────────────────────────────
# _build_merge_prompt 구성
# ──────────────────────────────────────────────────────────────────────────────
class TestMergePrompt(unittest.TestCase):
    def test_prompt_contains_both_outputs_and_instructions(self):
        outputs = [("modelA", "본문 A 내용 X1 16MHz"), ("modelB", "본문 B 내용 VDDA")]
        p = ox._build_merge_prompt(outputs)
        # 두 모델 결과가 모두 포함
        self.assertIn("modelA", p)
        self.assertIn("modelB", p)
        self.assertIn("X1 16MHz", p)
        self.assertIn("VDDA", p)
        # 통합본 / 상호보완 지시 포함
        self.assertIn("통합본", p)
        self.assertIn("보완", p)
        # 결과 개수 언급
        self.assertIn("2개", p)

    def test_prompt_handles_three_models(self):
        outputs = [("m1", "t1"), ("m2", "t2"), ("m3", "t3")]
        p = ox._build_merge_prompt(outputs)
        for tok in ("m1", "m2", "m3", "t1", "t2", "t3", "3개"):
            self.assertIn(tok, p)


# ──────────────────────────────────────────────────────────────────────────────
# _ensemble_vision 호출 흐름 (monkeypatch _ollama_vision)
# ──────────────────────────────────────────────────────────────────────────────
class TestEnsembleVision(unittest.TestCase):
    def test_two_models_then_merge(self):
        """2모델 추출 → merger 병합. 호출 순서/모델/이미지 검증."""
        calls = []

        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            calls.append({"model": model, "n_images": len(images_b64),
                          "prompt_head": prompt[:20]})
            # 각 모델은 자기 이름이 든 본문을 반환, merger 는 통합본 반환.
            if model == "MERGER":
                return "MERGED통합본"
            return f"output-from-{model}"

        with patch.object(ox, "ENSEMBLE_MODELS", ["M1", "M2"]), \
             patch.object(ox, "ENSEMBLE_MERGER", "MERGER"), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            result = ox._ensemble_vision("PROMPT_BODY", ["imgA", "imgB"],
                                         image_mime="image/png", role="structure")

        self.assertEqual(result, "MERGED통합본")
        # 정확히 3회 호출: M1, M2(이미지 2장), MERGER(이미지 0장)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["model"], "M1")
        self.assertEqual(calls[1]["model"], "M2")
        self.assertEqual(calls[2]["model"], "MERGER")
        # vision 호출은 이미지를 받고, 병합 호출은 이미지 0장
        self.assertEqual(calls[0]["n_images"], 2)
        self.assertEqual(calls[1]["n_images"], 2)
        self.assertEqual(calls[2]["n_images"], 0)
        # 병합 호출 프롬프트는 원본 본문 프롬프트가 아니라 merge 프롬프트
        self.assertNotEqual(calls[2]["prompt_head"], calls[0]["prompt_head"])

    def test_single_model_skips_merge(self):
        """모델 1개 → 병합 skip, 그 결과 그대로 반환(merger 미호출)."""
        calls = []

        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            calls.append(model)
            return f"solo-{model}"

        with patch.object(ox, "ENSEMBLE_MODELS", ["ONLY"]), \
             patch.object(ox, "ENSEMBLE_MERGER", "MERGER"), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            result = ox._ensemble_vision("P", ["img"], role="structure")

        self.assertEqual(result, "solo-ONLY")
        self.assertEqual(calls, ["ONLY"])  # MERGER 호출 안 됨

    def test_one_empty_output_merges_remaining_two(self):
        """3모델 중 1개 빈출력 → 나머지 2개로 병합."""
        calls = []

        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            calls.append(model)
            if model == "EMPTY":
                return "   "  # 공백(빈출력) → 제외 대상
            if model == "MERGER":
                return "MERGED"
            return f"out-{model}"

        with patch.object(ox, "ENSEMBLE_MODELS", ["A", "EMPTY", "B"]), \
             patch.object(ox, "ENSEMBLE_MERGER", "MERGER"), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            result = ox._ensemble_vision("P", ["img"], role="structure")

        self.assertEqual(result, "MERGED")
        # A, EMPTY, B 모두 시도되고, EMPTY 제외 후 2개 → MERGER 병합
        self.assertEqual(calls, ["A", "EMPTY", "B", "MERGER"])

    def test_one_failure_merges_remaining(self):
        """1개 모델 ExtractError → 제외하고 나머지로 병합."""
        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            if model == "BROKEN":
                raise ox.ExtractError("simulated failure")
            if model == "MERGER":
                return "MERGED"
            return f"out-{model}"

        with patch.object(ox, "ENSEMBLE_MODELS", ["GOOD1", "BROKEN", "GOOD2"]), \
             patch.object(ox, "ENSEMBLE_MERGER", "MERGER"), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            result = ox._ensemble_vision("P", ["img"], role="structure")
        self.assertEqual(result, "MERGED")

    def test_all_empty_raises(self):
        """전 모델 빈출력 → ExtractError."""
        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            return ""  # 모두 빈출력

        with patch.object(ox, "ENSEMBLE_MODELS", ["A", "B"]), \
             patch.object(ox, "ENSEMBLE_MERGER", "MERGER"), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            with self.assertRaises(ox.ExtractError):
                ox._ensemble_vision("P", ["img"], role="structure")

    def test_empty_model_list_falls_back_to_single(self):
        """ENSEMBLE_MODELS 빈 리스트 → 단일 _ollama_vision 폴백(회귀 안전)."""
        calls = []

        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            calls.append(model)
            return "single-fallback"

        with patch.object(ox, "ENSEMBLE_MODELS", []), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            result = ox._ensemble_vision("P", ["img"], role="structure")
        self.assertEqual(result, "single-fallback")
        # 단일 폴백: model=None(역할기반) 1회만
        self.assertEqual(len(calls), 1)

    def test_merger_called_with_merge_max_tokens(self):
        """병합 호출은 ENSEMBLE_MERGE_MAX_TOKENS 로 호출된다."""
        seen = {}

        def fake_vision(prompt, images_b64, image_mime="image/png",
                        model=None, temperature=0.2, max_tokens=None, role=None):
            if model == "MERGER":
                seen["max_tokens"] = max_tokens
                return "MERGED"
            return f"out-{model}"

        with patch.object(ox, "ENSEMBLE_MODELS", ["A", "B"]), \
             patch.object(ox, "ENSEMBLE_MERGER", "MERGER"), \
             patch.object(ox, "_ollama_vision", side_effect=fake_vision):
            ox._ensemble_vision("P", ["img"], role="structure")
        self.assertEqual(seen.get("max_tokens"), ox.ENSEMBLE_MERGE_MAX_TOKENS)


# ──────────────────────────────────────────────────────────────────────────────
# extract_pdf_pages 통합점 — 앙상블 게이트 (회귀 0 검증)
# ──────────────────────────────────────────────────────────────────────────────
class TestExtractPdfPagesGate(unittest.TestCase):
    def _patch_render(self):
        """render_pdf_pages_to_base64 를 가짜(이미지 1장)로 대체하는 컨텍스트."""
        return patch.object(ox, "render_pdf_pages_to_base64",
                            return_value=["FAKE_IMG_B64"])

    def test_disabled_uses_single_vision(self):
        """ENSEMBLE_ENABLED=False → _ollama_vision 직행, _ensemble_vision 미호출."""
        with self._patch_render(), \
             patch.object(ox, "ENSEMBLE_ENABLED", False), \
             patch.object(ox, "_ollama_vision", return_value="SINGLE") as m_single, \
             patch.object(ox, "_ensemble_vision", return_value="ENS") as m_ens:
            out = ox.extract_pdf_pages("P", "/fake.pdf", 1, 1, role="structure")
        self.assertEqual(out, "SINGLE")
        m_single.assert_called_once()
        m_ens.assert_not_called()

    def test_enabled_structure_uses_ensemble(self):
        """ENSEMBLE_ENABLED=True + role='structure' → _ensemble_vision 사용."""
        with self._patch_render(), \
             patch.object(ox, "ENSEMBLE_ENABLED", True), \
             patch.object(ox, "_ollama_vision", return_value="SINGLE") as m_single, \
             patch.object(ox, "_ensemble_vision", return_value="ENS") as m_ens:
            out = ox.extract_pdf_pages("P", "/fake.pdf", 1, 1, role="structure")
        self.assertEqual(out, "ENS")
        m_ens.assert_called_once()
        m_single.assert_not_called()

    def test_enabled_nonstructure_role_skips_ensemble(self):
        """ENSEMBLE_ENABLED=True 라도 role!='structure' → 단일 경로(앙상블 우회)."""
        with self._patch_render(), \
             patch.object(ox, "ENSEMBLE_ENABLED", True), \
             patch.object(ox, "_ollama_vision", return_value="SINGLE") as m_single, \
             patch.object(ox, "_ensemble_vision", return_value="ENS") as m_ens:
            out = ox.extract_pdf_pages("P", "/fake.pdf", 1, 1, role="caption")
        self.assertEqual(out, "SINGLE")
        m_single.assert_called_once()
        m_ens.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
