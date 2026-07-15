"""test_config_sst.py — lib/config.py SSoT(Single Source of Truth) 로더 단위 테스트.

검증 항목:
  A) 동작 보존: env 전부 unset 시 알려진 코드기본값과 1:1 일치
  B) 우선순위: env > yaml > default
  C) 이름 매핑: OLLAMA_CLOUD_MAX_TOKENS → max_tokens 등
  D) 이원성/중첩 fallback: EXTRACT_RENDER_DPI / VISION_QA_DPI
  E) degrade: config.yaml 없음/파싱 실패 시 전부 default
  F) bool any-nonempty 의미 보존: '0' → truthy
  G) formats hwp 포함 확인
  H) post 처리: rstrip('/'), strip().lower()

실행:
    .venv/bin/python -m pytest tests/test_config_sst.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _fresh_config(raw_override: dict | None = None, env_override: dict | None = None):
    """lib.config 를 격리된 상태로 재생성해 반환.

    raw_override: _raw 를 강제 교체 (yaml 로드 내용 제어).
    env_override : os.environ 을 patch 할 dict (None 인 항목은 삭제).
    """
    import importlib
    import fmdw.config as cfg
    importlib.reload(cfg)  # 매 테스트마다 _raw 초기화 (yaml 재로드)
    if raw_override is not None:
        cfg._raw = raw_override
    return cfg


# ──────────────────────────────────────────────────────────────────────────────
# A) 동작 보존 — env 전부 unset 시 알려진 코드기본값과 일치
# ──────────────────────────────────────────────────────────────────────────────

class TestDefaultValues(unittest.TestCase):
    """env 전부 unset, yaml 없음 상태에서 코드기본값 확인."""

    def _cfg_no_env_no_yaml(self):
        """yaml=빈dict, 관련 env 전부 unset."""
        env_keys = [
            "EXTRACT_PROVIDER", "OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL",
            "OLLAMA_VISION_MODEL", "GEMINI_VISION_MODEL",
            "OLLAMA_CLOUD_MAX_TOKENS", "OLLAMA_CLOUD_TIMEOUT",
            "EXTRACT_RENDER_DPI", "VISION_QA_DPI",
            "EXTRACT_CHUNK_SIZE",
            "OLLAMA_MAX_RETRIES", "OLLAMA_RETRY_BASE_DELAY",
            "OLLAMA_RETRY_MAX_DELAY", "OLLAMA_RETRY_AFTER_CAP",
        ]
        clean_env = {k: v for k, v in os.environ.items() if k not in env_keys}
        with patch.dict(os.environ, clean_env, clear=True):
            import importlib
            import fmdw.config as cfg
            importlib.reload(cfg)
            cfg._raw = {}  # yaml 없는 것처럼
            return cfg

    def test_render_dpi_default_150(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_render_dpi(), 150)

    def test_vision_qa_dpi_default_220(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_vision_qa_dpi(), 220)

    def test_chunk_size_default_5(self):
        # 기본=5: silent truncation 방지(무결성). chunk가 크면 후반 페이지 조용히 유실.
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_chunk_size(), 5)

    def test_max_tokens_default_8192(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_max_tokens(), 8192)

    def test_provider_default_ollama_cloud(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_extract_provider(), "ollama_cloud")

    def test_vision_model_default(self):
        # R11(2026-07-15): 기대값 갱신 — 구 'gemini-3-flash-preview' 는 하드 로컬
        # 전환(2026-06-30) + FIX D-R2(2026-07-09, 코드 기본 qwen3-vl:8b 교체) 이전
        # 사양. 검증 의도(env/yaml 없는 '코드 기본값' 고정) 는 그대로 유지.
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_vision_model(), "qwen3-vl:8b-instruct-q8_0")

    def test_base_url_default(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_ollama_base_url(), "http://localhost:11434/v1")

    def test_timeout_default_600(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_timeout(), 600)

    def test_max_retries_default_4(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertEqual(cfg.knob_max_retries(), 4)

    def test_retry_base_delay_default_1(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertAlmostEqual(cfg.knob_retry_base_delay(), 1.0)

    def test_retry_max_delay_default_60(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertAlmostEqual(cfg.knob_retry_max_delay(), 60.0)

    def test_retry_after_cap_default_120(self):
        cfg = self._cfg_no_env_no_yaml()
        self.assertAlmostEqual(cfg.knob_retry_after_cap(), 120.0)


# ──────────────────────────────────────────────────────────────────────────────
# B) 우선순위: env > yaml > default
# ──────────────────────────────────────────────────────────────────────────────

class TestPriority(unittest.TestCase):
    """env > yaml > default 우선순위 검증."""

    def test_env_beats_yaml_and_default(self):
        """env 설정 시 yaml/default 무시."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {"options": {"chunk_size": 99}}  # yaml = 99
        with patch.dict(os.environ, {"EXTRACT_CHUNK_SIZE": "5"}):  # env = 5
            self.assertEqual(cfg.knob_chunk_size(), 5)

    def test_yaml_beats_default_when_env_absent(self):
        """env 없고 yaml 있으면 yaml 사용."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {"options": {"chunk_size": 50}}
        clean = {k: v for k, v in os.environ.items() if k != "EXTRACT_CHUNK_SIZE"}
        with patch.dict(os.environ, clean, clear=True):
            self.assertEqual(cfg.knob_chunk_size(), 50)

    def test_default_when_both_absent(self):
        """env, yaml 모두 없으면 default."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        clean = {k: v for k, v in os.environ.items() if k != "EXTRACT_CHUNK_SIZE"}
        with patch.dict(os.environ, clean, clear=True):
            self.assertEqual(cfg.knob_chunk_size(), 5)


# ──────────────────────────────────────────────────────────────────────────────
# C) 이름 매핑: 환경변수 이름 → knob
# ──────────────────────────────────────────────────────────────────────────────

class TestNameMapping(unittest.TestCase):
    """환경변수 이름이 knob 에 올바르게 매핑되는지 확인."""

    def test_ollama_cloud_max_tokens_maps_to_max_tokens(self):
        """OLLAMA_CLOUD_MAX_TOKENS → knob_max_tokens."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        with patch.dict(os.environ, {"OLLAMA_CLOUD_MAX_TOKENS": "4096"}):
            self.assertEqual(cfg.knob_max_tokens(), 4096)

    def test_ollama_cloud_timeout_maps_to_timeout(self):
        """OLLAMA_CLOUD_TIMEOUT → knob_timeout."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        with patch.dict(os.environ, {"OLLAMA_CLOUD_TIMEOUT": "300"}):
            self.assertEqual(cfg.knob_timeout(), 300)

    def test_extract_provider_maps_to_provider(self):
        """EXTRACT_PROVIDER → knob_extract_provider (strip+lower 보존)."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        with patch.dict(os.environ, {"EXTRACT_PROVIDER": "  GEMINI  "}):
            self.assertEqual(cfg.knob_extract_provider(), "gemini")


# ──────────────────────────────────────────────────────────────────────────────
# D) 이원성/중첩 fallback
# ──────────────────────────────────────────────────────────────────────────────

class TestDualEnvFallback(unittest.TestCase):
    """2-var fallback 체인 및 중첩 fallback 확인."""

    def _clean(self, *keys):
        return {k: v for k, v in os.environ.items() if k not in keys}

    def test_ollama_base_url_primary_env(self):
        """OLLAMA_BASE_URL 설정 시 사용."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        with patch.dict(os.environ,
                        self._clean("OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL"),
                        clear=True):
            os.environ["OLLAMA_BASE_URL"] = "http://primary:1234/v1"
            result = cfg.knob_ollama_base_url()
        self.assertEqual(result, "http://primary:1234/v1")

    def test_ollama_base_url_fallback_env(self):
        """OLLAMA_BASE_URL 없고 OLLAMA_CLOUD_BASE_URL 있으면 fallback 사용."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        env = self._clean("OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL")
        env["OLLAMA_CLOUD_BASE_URL"] = "http://fallback:5678/v1"
        with patch.dict(os.environ, env, clear=True):
            result = cfg.knob_ollama_base_url()
        self.assertEqual(result, "http://fallback:5678/v1")

    def test_base_url_trailing_slash_stripped(self):
        """base_url 후행 슬래시 제거 (post 처리)."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        env = self._clean("OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL")
        env["OLLAMA_BASE_URL"] = "http://host:1234/v1/"
        with patch.dict(os.environ, env, clear=True):
            result = cfg.knob_ollama_base_url()
        self.assertFalse(result.endswith("/"),
                         f"후행 슬래시가 제거되어야 함: {result!r}")

    def test_extract_render_dpi_affects_both_when_vision_qa_dpi_absent(self):
        """EXTRACT_RENDER_DPI=999, VISION_QA_DPI 없음 → vision_qa_dpi=999."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        env = self._clean("EXTRACT_RENDER_DPI", "VISION_QA_DPI")
        env["EXTRACT_RENDER_DPI"] = "999"
        with patch.dict(os.environ, env, clear=True):
            render = cfg.knob_render_dpi()
            vqa = cfg.knob_vision_qa_dpi()
        self.assertEqual(render, 999)
        self.assertEqual(vqa, 999)

    def test_vision_qa_dpi_overrides_extract_render_dpi(self):
        """VISION_QA_DPI=111, EXTRACT_RENDER_DPI=999 → vision만 111."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        env = self._clean("EXTRACT_RENDER_DPI", "VISION_QA_DPI")
        env["EXTRACT_RENDER_DPI"] = "999"
        env["VISION_QA_DPI"] = "111"
        with patch.dict(os.environ, env, clear=True):
            render = cfg.knob_render_dpi()
            vqa = cfg.knob_vision_qa_dpi()
        self.assertEqual(render, 999, "EXTRACT_RENDER_DPI=999 이므로 render=999")
        self.assertEqual(vqa, 111, "VISION_QA_DPI=111 이 우선")


# ──────────────────────────────────────────────────────────────────────────────
# E) degrade: config.yaml 없음/파싱 실패 시 전부 default
# ──────────────────────────────────────────────────────────────────────────────

class TestDegrade(unittest.TestCase):
    """yaml _raw=빈dict 상태에서 env 없으면 전부 default 반환."""

    def _bare_cfg(self):
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        return cfg

    def _clean_env(self, cfg):
        knob_envs = [
            "EXTRACT_PROVIDER", "OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL",
            "OLLAMA_VISION_MODEL", "GEMINI_VISION_MODEL",
            "OLLAMA_CLOUD_MAX_TOKENS", "OLLAMA_CLOUD_TIMEOUT",
            "EXTRACT_RENDER_DPI", "VISION_QA_DPI", "EXTRACT_CHUNK_SIZE",
            "OLLAMA_MAX_RETRIES", "OLLAMA_RETRY_BASE_DELAY",
            "OLLAMA_RETRY_MAX_DELAY", "OLLAMA_RETRY_AFTER_CAP",
        ]
        return {k: v for k, v in os.environ.items() if k not in knob_envs}

    def test_all_defaults_when_no_yaml_no_env(self):
        """yaml 없고 env 없으면 전부 코드기본값."""
        cfg = self._bare_cfg()
        with patch.dict(os.environ, self._clean_env(cfg), clear=True):
            self.assertEqual(cfg.knob_max_tokens(), 8192)
            self.assertEqual(cfg.knob_render_dpi(), 150)
            self.assertEqual(cfg.knob_vision_qa_dpi(), 220)
            self.assertEqual(cfg.knob_chunk_size(), 5)
            self.assertEqual(cfg.knob_max_retries(), 4)
            self.assertEqual(cfg.knob_extract_provider(), "ollama_cloud")


# ──────────────────────────────────────────────────────────────────────────────
# F) bool any-nonempty 의미 보존
# ──────────────────────────────────────────────────────────────────────────────

class TestBoolNonempty(unittest.TestCase):
    """'0', 'false', 'no' 등 비어있지 않은 문자열은 truthy."""

    def test_zero_string_is_truthy(self):
        import fmdw.config as cfg
        self.assertTrue(cfg.bool_nonempty("0"), "'0' 은 truthy 여야 함")

    def test_false_string_is_truthy(self):
        import fmdw.config as cfg
        self.assertTrue(cfg.bool_nonempty("false"), "'false' 는 truthy 여야 함")

    def test_empty_string_is_falsy(self):
        import fmdw.config as cfg
        self.assertFalse(cfg.bool_nonempty(""), "빈 문자열은 falsy 여야 함")

    def test_nonempty_via_get_bool_nonempty(self):
        """get_bool_nonempty 헬퍼가 env '0' → True 반환."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        cfg._raw = {}
        with patch.dict(os.environ, {"SOME_FLAG": "0"}):
            result = cfg.get_bool_nonempty("options.some_flag", False,
                                           env_var="SOME_FLAG")
        self.assertTrue(result)


# ──────────────────────────────────────────────────────────────────────────────
# G) config.yaml 로드 시 formats 에 hwp 포함
# ──────────────────────────────────────────────────────────────────────────────

class TestConfigYamlFormats(unittest.TestCase):
    """실제 config.yaml 이 로드될 때 hwp 포맷 항목이 존재해야 한다."""

    def test_hwp_format_present_in_yaml(self):
        """config.yaml formats.hwp 존재 (silent 회귀 방지)."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)  # 실제 파일 로드
        formats = cfg._raw.get("formats", {})
        self.assertIn(
            "hwp", formats,
            "config.yaml formats 섹션에 hwp 가 없으면 HWP 처리 설정이 누락됨"
        )
        hwp = formats["hwp"]
        self.assertIn("input_dir", hwp)
        self.assertIn("output_dir", hwp)

    def test_vision_qa_dpi_in_options(self):
        """config.yaml options.vision_qa_dpi 존재 (silent 회귀 방지)."""
        import importlib
        import fmdw.config as cfg
        importlib.reload(cfg)
        options = cfg._raw.get("options", {})
        self.assertIn(
            "vision_qa_dpi", options,
            "config.yaml options.vision_qa_dpi 가 없으면 vision-QA DPI 가 150 으로 떨어짐"
        )
        self.assertEqual(options["vision_qa_dpi"], 220)


# ──────────────────────────────────────────────────────────────────────────────
# H) 모듈 상수가 config 경유로 올바른 값을 스냅샷하는지 확인
#    (ollama_extractor / vision_qa / extract_all_via_pdf)
# ──────────────────────────────────────────────────────────────────────────────

class TestModuleConstantsViaConfig(unittest.TestCase):
    """Stage 1 연결 모듈의 상수가 config 기본값과 일치하는지 확인."""

    def _clean_env(self, *keys):
        return {k: v for k, v in os.environ.items() if k not in keys}

    def test_ollama_extractor_constants_match_defaults(self):
        """lib.ollama_extractor 상수가 config 기본값(8192/600/150/4…)과 일치."""
        env_keys = [
            "OLLAMA_CLOUD_MAX_TOKENS", "OLLAMA_CLOUD_TIMEOUT", "EXTRACT_RENDER_DPI",
            "OLLAMA_MAX_RETRIES", "OLLAMA_RETRY_BASE_DELAY",
            "OLLAMA_RETRY_MAX_DELAY", "OLLAMA_RETRY_AFTER_CAP",
            "EXTRACT_PROVIDER", "OLLAMA_VISION_MODEL", "GEMINI_VISION_MODEL",
            "OLLAMA_BASE_URL", "OLLAMA_CLOUD_BASE_URL",
        ]
        with patch.dict(os.environ, self._clean_env(*env_keys), clear=True):
            import importlib
            import fmdw.config as cfg
            import fmdw.ollama_extractor as ox
            importlib.reload(cfg)
            importlib.reload(ox)
            self.assertEqual(ox.OLLAMA_MAX_TOKENS, 8192)
            self.assertEqual(ox.OLLAMA_TIMEOUT, 600)
            self.assertEqual(ox.RENDER_DPI, 150)
            self.assertEqual(ox.OLLAMA_MAX_RETRIES, 4)
            self.assertAlmostEqual(ox.OLLAMA_RETRY_BASE_DELAY, 1.0)
            self.assertAlmostEqual(ox.OLLAMA_RETRY_MAX_DELAY, 60.0)
            self.assertAlmostEqual(ox.OLLAMA_RETRY_AFTER_CAP, 120.0)
            self.assertEqual(ox.EXTRACT_PROVIDER, "ollama_cloud")
            # R11(2026-07-15): 기대값 갱신 — 코드 기본 vision 모델은 FIX D-R2
            # (2026-07-09) 이후 qwen3-vl:8b-instruct-q8_0 (하드 로컬 표준).
            self.assertEqual(ox.OLLAMA_VISION_MODEL, "qwen3-vl:8b-instruct-q8_0")
            self.assertEqual(ox.OLLAMA_BASE_URL, "http://localhost:11434/v1")

    def test_vision_qa_dpi_constant(self):
        """lib.vision_qa.VISION_QA_DPI 기본값 220."""
        env_keys = ["VISION_QA_DPI", "EXTRACT_RENDER_DPI"]
        with patch.dict(os.environ, self._clean_env(*env_keys), clear=True):
            import importlib
            import fmdw.config as cfg
            import fmdw.vision_qa as vqa
            importlib.reload(cfg)
            importlib.reload(vqa)
            self.assertEqual(vqa.VISION_QA_DPI, 220)

    def test_extract_all_chunk_size(self):
        """extract_all_via_pdf.CHUNK_SIZE 기본값 5 (silent truncation 방지)."""
        env_keys = ["EXTRACT_CHUNK_SIZE"]
        with patch.dict(os.environ, self._clean_env(*env_keys), clear=True):
            import importlib
            import fmdw.config as cfg
            import extract_all_via_pdf as eap
            importlib.reload(cfg)
            importlib.reload(eap)
            self.assertEqual(eap.CHUNK_SIZE, 5)


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
