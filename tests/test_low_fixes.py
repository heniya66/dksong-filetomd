"""test_low_fixes.py — QA 리뷰 Low 항목 수정 회귀 테스트 (L-5/L-7/L-8/L-13).

대상 수정:
  L-5  net_crosscheck summary-key 상수(SSoT) — producer↔consumer 키 오타 침묵 방지.
  L-7  hwp_to_pdf 임시 디렉터리 누수 — html_file 미발견 early-return 포함 전 경로 정리.
  L-8  Gemini fallback .text ValueError 가드 — ExtractError 로 변환(안전차단/멀티파트).
  L-13 vision_qa._sanity_gate designator 보존율 가드 — 구조 훼손(부품 대거 증발) degrade.

네트워크/서브프로세스/claude/gemini 실호출 없음 — 전부 mock/단위.

실행:
    python -m pytest tests/test_low_fixes.py -v
    또는: python tests/test_low_fixes.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# 워크스페이스 루트를 sys.path 에 추가(lib.* 패키지 import 보장).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import net_crosscheck as nc  # noqa: E402
from fmdw import ollama_extractor as ox  # noqa: E402
from fmdw import vision_qa as vqa  # noqa: E402


def _extract_error():
    """현재 lib.ollama_extractor 모듈의 ExtractError 클래스(reload-안전).

    test_config_sst.py 가 importlib.reload(lib.ollama_extractor) 를 호출하면 모듈 내
    ExtractError 가 **새 클래스 객체**로 교체된다. import 시점에 바인딩한 ExtractError
    심볼을 그대로 쓰면 reload 후 isinstance/except 불일치로 거짓 실패가 난다(테스트 간
    오염). 따라서 호출 시점에 ox.ExtractError 를 재조회해 항상 현행 클래스를 쓴다.
    """
    return ox.ExtractError


# ──────────────────────────────────────────────────────────────────────────────
# L-5: net_crosscheck summary-key 상수가 실제 produce 되는 summary 와 정합
# ──────────────────────────────────────────────────────────────────────────────
class TestL5SummaryKeyConstants(unittest.TestCase):
    """summary dict 의 키를 상수로 고정 — producer 가 그 키로 실제 값을 채우는지 검증."""

    def test_constants_exist(self):
        # consumer(extract_all_via_pdf)가 import 하는 상수가 존재해야 한다.
        for name in (
            "SK_VECTOR_CONFIRMED", "SK_SPURIOUS_FLAGGED",
            "SK_VECTOR_ONLY_NETS", "SK_APPLIED", "SK_REASON",
        ):
            self.assertTrue(hasattr(nc, name), f"net_crosscheck.{name} 부재")

    def test_constant_values_match_legacy_literals(self):
        # 외부 manifest 스키마/하위호환을 위해 상수 '값'이 기존 리터럴과 동일해야 한다.
        self.assertEqual(nc.SK_VECTOR_CONFIRMED, "vector_confirmed")
        self.assertEqual(nc.SK_SPURIOUS_FLAGGED, "spurious_flagged")
        self.assertEqual(nc.SK_VECTOR_ONLY_NETS, "vector_only_nets")
        self.assertEqual(nc.SK_APPLIED, "applied")
        self.assertEqual(nc.SK_REASON, "reason")

    def test_applied_summary_uses_constant_keys(self):
        # 실제 적용 경로: 상수 키로 값이 채워지는지(오타 시 KeyError/누락 검출).
        tracer = {
            "ok": True,
            "page": 1,
            "nets": [
                {"name": "DDR_DQ48", "connections": [{"ref": "U24", "pin": "M8"}]},
            ],
            "no_connects": [],
            "stats": {},
        }
        vision_md = "U24 pin M8 -> DDR_DQ48 [unverified]"
        res = nc.crosscheck_with_tracer(vision_md, tracer)
        self.assertTrue(res.applied)
        # 상수 키로 접근 가능해야 한다(.get 아님 — 키 존재 단언).
        self.assertIn(nc.SK_VECTOR_CONFIRMED, res.summary)
        self.assertIn(nc.SK_SPURIOUS_FLAGGED, res.summary)
        self.assertIn(nc.SK_VECTOR_ONLY_NETS, res.summary)
        self.assertTrue(res.summary[nc.SK_APPLIED])
        self.assertEqual(res.summary[nc.SK_REASON], "ok")

    def test_degraded_summary_uses_constant_keys(self):
        # degrade(net_tracer not ok) 경로도 동일 키 계약.
        res = nc.crosscheck_with_tracer("R1 100nF", {"ok": False, "reason": "raster"})
        self.assertFalse(res.applied)
        self.assertEqual(res.summary[nc.SK_VECTOR_CONFIRMED], 0)
        self.assertEqual(res.summary[nc.SK_SPURIOUS_FLAGGED], 0)
        self.assertEqual(res.summary[nc.SK_VECTOR_ONLY_NETS], 0)
        self.assertFalse(res.summary[nc.SK_APPLIED])
        self.assertEqual(res.summary[nc.SK_REASON], "raster")


# ──────────────────────────────────────────────────────────────────────────────
# L-7: hwp_to_pdf 임시 디렉터리 누수 방지 (early-return 포함 전 경로 정리)
# ──────────────────────────────────────────────────────────────────────────────
class TestL7HwpTempCleanup(unittest.TestCase):
    """html_file 미발견 early-return / 예외 / 성공 모든 경로에서 temp_dir 정리 보장."""

    def _import_eavp(self):
        # extract_all_via_pdf 는 import 부작용(상수 스냅샷)만 있고 안전하다.
        import extract_all_via_pdf as eavp  # noqa: E402
        return eavp

    def test_temp_dir_removed_when_no_html_output(self):
        """과거 누수 지점: html_file 미발견 시 temp_dir 가 남던 버그 재발 방지."""
        eavp = self._import_eavp()
        removed = []

        # subprocess.run 은 성공으로(파일 미생성), glob 은 빈 결과로 → html_file=None 분기.
        with patch.object(eavp, "subprocess") as msub, \
             patch.object(eavp, "shutil") as mshutil, \
             patch.object(eavp.Path, "mkdir", lambda *a, **k: None), \
             patch.object(eavp.Path, "glob", lambda self, pat: iter(())), \
             patch.object(eavp.Path, "exists", lambda self: True):
            msub.run = MagicMock()
            mshutil.rmtree = MagicMock(side_effect=lambda p: removed.append(str(p)))
            result = eavp.hwp_to_pdf(Path("input/hwp/sample.hwp"))

        self.assertIsNone(result)  # HTML 없음 → None
        # finally 가 정확히 1회 temp_dir 정리해야 한다(누수 0).
        self.assertEqual(len(removed), 1, f"temp_dir 정리 누락/중복: {removed}")
        self.assertIn("temp_hwp_sample", removed[0])

    def test_temp_dir_removed_on_exception(self):
        """변환 중 예외가 나도 finally 가 temp_dir 를 정리한다."""
        eavp = self._import_eavp()
        removed = []

        with patch.object(eavp, "subprocess") as msub, \
             patch.object(eavp, "shutil") as mshutil, \
             patch.object(eavp.Path, "mkdir", lambda *a, **k: None), \
             patch.object(eavp.Path, "exists", lambda self: True):
            msub.run = MagicMock(side_effect=RuntimeError("hwp5html boom"))
            mshutil.rmtree = MagicMock(side_effect=lambda p: removed.append(str(p)))
            result = eavp.hwp_to_pdf(Path("input/hwp/broken.hwp"))

        self.assertIsNone(result)
        self.assertEqual(len(removed), 1, f"예외 경로 temp_dir 정리 누락/중복: {removed}")

    def test_cleanup_failure_does_not_raise(self):
        """temp_dir 정리 자체가 실패해도 hwp_to_pdf 가 예외를 던지지 않는다(방어)."""
        eavp = self._import_eavp()

        with patch.object(eavp, "subprocess") as msub, \
             patch.object(eavp, "shutil") as mshutil, \
             patch.object(eavp.Path, "mkdir", lambda *a, **k: None), \
             patch.object(eavp.Path, "glob", lambda self, pat: iter(())), \
             patch.object(eavp.Path, "exists", lambda self: True):
            msub.run = MagicMock()
            mshutil.rmtree = MagicMock(side_effect=OSError("permission denied"))
            # 정리 실패가 전파되면 안 됨(메시지만 출력하고 None 반환).
            result = eavp.hwp_to_pdf(Path("input/hwp/sample.hwp"))
        self.assertIsNone(result)


# ──────────────────────────────────────────────────────────────────────────────
# L-8: Gemini fallback .text ValueError → ExtractError 가드
# ──────────────────────────────────────────────────────────────────────────────
class TestL8GeminiTextGuard(unittest.TestCase):
    """response.text 가 ValueError(안전차단/멀티파트)일 때 ExtractError 로 변환.

    주의: MagicMock 의 `type(m).text = property(...)` 패턴은 공유 mock 클래스를 오염시켜
    뒤따르는 테스트의 모든 mock 속성 접근을 깨뜨린다(테스트 격리 위반). 따라서 여기서는
    경량 fake 응답 객체를 직접 정의해 google.generativeai response 의 `.text` accessor
    동작(정상/ValueError/빈 응답)을 재현한다 — 전역 오염 없음.
    """

    class _FakeResp:
        """google.generativeai GenerateContentResponse 의 .text 동작 재현 fake."""

        def __init__(self, text_value=None, raises=None, feedback=None,
                     feedback_raises=False):
            self._text_value = text_value
            self._raises = raises
            self._feedback = feedback
            self._feedback_raises = feedback_raises

        @property
        def text(self):
            if self._raises is not None:
                raise self._raises
            return self._text_value

        @property
        def prompt_feedback(self):
            if self._feedback_raises:
                raise RuntimeError("no feedback")
            return self._feedback

    def test_value_error_becomes_extract_error(self):
        # .text 접근 시 ValueError — google.generativeai 의 안전차단 동작 모사.
        resp = self._FakeResp(raises=ValueError("blocked"),
                              feedback="block_reason: SAFETY")
        with self.assertRaises(_extract_error()) as ctx:
            ox._gemini_response_text(resp)
        # 진단에 사유 메타 포함(키/원문 노출 아님).
        self.assertIn("안전차단", str(ctx.exception))

    def test_empty_text_becomes_extract_error(self):
        resp = self._FakeResp(text_value="   ")  # 공백만 → 빈 응답으로 취급
        with self.assertRaises(_extract_error()):
            ox._gemini_response_text(resp)

    def test_valid_text_passthrough(self):
        resp = self._FakeResp(text_value="## Page 1\n정상 추출")
        self.assertEqual(ox._gemini_response_text(resp), "## Page 1\n정상 추출")

    def test_feedback_access_failure_is_tolerated(self):
        # prompt_feedback 접근 자체가 실패해도 ExtractError 로 깔끔히 변환.
        resp = self._FakeResp(raises=ValueError("blocked"), feedback_raises=True)
        with self.assertRaises(_extract_error()):
            ox._gemini_response_text(resp)


# ──────────────────────────────────────────────────────────────────────────────
# L-13: vision_qa._sanity_gate designator 보존율 가드
# ──────────────────────────────────────────────────────────────────────────────
class TestL13DesignatorRetentionGate(unittest.TestCase):
    """1차 designator 가 verifier 출력에서 대거 증발하면 degrade(1차 MD 유지)."""

    def _primary_with_designators(self, n: int) -> str:
        # R1..Rn designator 가 줄마다 박힌 1차 MD 생성(>= MIN_COUNT 보장).
        return "\n".join(f"R{i} 10k -> NET{i}" for i in range(1, n + 1))

    def test_massive_designator_loss_degrades(self):
        primary = self._primary_with_designators(12)  # 12 designators
        # verifier 가 길이는 비슷하나 designator 를 거의 다 날린 출력(요약/중간 truncate).
        out = ("필러 텍스트 라인 " * 30) + "\nR1 10k -> NET1"  # designator 1개만 잔존
        result = vqa._sanity_gate(out, primary)
        self.assertEqual(result, primary, "designator 대거 증발 시 1차 MD 로 degrade 해야 함")

    def test_high_retention_passes(self):
        primary = self._primary_with_designators(12)
        # designator 대부분 보존 + 추가 정정(정상 verifier).
        out = primary + "\n(검증 완료)"
        result = vqa._sanity_gate(out, primary)
        self.assertEqual(result, out, "designator 보존율 높으면 verifier 출력 채택")

    def test_small_designator_sample_not_overpoliced(self):
        # designator 가 MIN_COUNT 미만이면 보존율 가드 비활성(과민반응 방지).
        primary = "R1 10k -> NET1\nC2 100nF -> NET2"  # 2개(<8)
        out = "정상 정정 텍스트 (designator 없음)"
        # 길이/Figure/preamble 가드만 적용 — 길이가 1차의 50% 이상이면 통과.
        result = vqa._sanity_gate(out, primary)
        self.assertEqual(result, out, "표본 적을 때 designator 가드가 작동하면 안 됨")

    def test_length_guard_still_precedes(self):
        # 보존율 가드 추가가 기존 길이 가드를 깨지 않는지(짧은 출력은 길이 가드로 degrade).
        primary = self._primary_with_designators(12)
        out = "x"  # 매우 짧음 → 길이 가드(1)에서 degrade
        result = vqa._sanity_gate(out, primary)
        self.assertEqual(result, primary)

    # ── Info(L-13 false-degrade) 보완: narrowing 정상 교정 비-degrade vs 진짜 훼손 degrade ──

    def test_intentional_narrowing_correction_not_degraded(self):
        """QA rule 1/2/3 의 정상 교정(태그 부착하며 designator 대량 축소)은 degrade 안 됨.

        verifier 가 환각/과대일반화 designator 대부분에 narrowing 태그를 붙이며 줄였다면,
        그 designator 들은 보존율 분모에서 제외되어야 한다(정상 교정 보존). 길이는 ≥50% 유지.
        """
        primary = self._primary_with_designators(12)  # R1..R12
        # verifier: R1만 검증 유지, R2~R12 는 의도적으로 narrowing 태그 부착(환각/range 축소).
        narrowed_lines = "\n".join(
            f"R{i} [unverified] (image 미확인 — 환각 가능)" for i in range(2, 13)
        )
        out = "R1 10k -> NET1\n" + narrowed_lines + "\n(검증 완료 — 충분한 본문 길이 확보)"
        # 길이 가드 통과 보장(1차의 50% 이상).
        self.assertGreaterEqual(len(out), len(primary) * 0.5)
        result = vqa._sanity_gate(out, primary)
        self.assertEqual(
            result, out,
            "narrowing 태그로 의도적으로 줄인 정상 교정은 보존율 가드에 걸리면 안 됨",
        )

    def test_true_structural_loss_still_degrades_with_teeth(self):
        """태그 없이 흔적도 없이 designator 가 대거 증발한 진짜 구조 훼손은 여전히 degrade.

        narrowing 신호가 전혀 없으므로 분모 축소가 일어나지 않고, 보존율 가드가 작동한다.
        """
        primary = self._primary_with_designators(12)  # R1..R12
        # 길이는 비슷하나 designator 1개만 남고 narrowing 태그 0 → 무흔적 증발.
        out = ("일반 서술 라인 " * 40) + "\nR1 10k -> NET1"
        self.assertGreaterEqual(len(out), len(primary) * 0.5)  # 길이 가드 통과 보장
        result = vqa._sanity_gate(out, primary)
        self.assertEqual(
            result, primary,
            "태그 없는 무흔적 대량 증발(진짜 훼손)은 여전히 degrade 해야 함(teeth 유지)",
        )

    def test_partial_narrowing_does_not_mask_real_loss(self):
        """일부만 narrowing 으로 설명되고 나머지가 무흔적 증발이면 여전히 degrade.

        narrowing 제외 후 남은 expected 표본이 충분(>=MIN_COUNT)하고 그 보존율이 낮으면
        가드가 작동한다 — narrowing 한두 개로 진짜 훼손을 가리지 못하게.
        """
        primary = self._primary_with_designators(14)  # R1..R14
        # R2 하나만 narrowing 태그, 나머지 R3..R14 는 태그 없이 증발, R1만 유지.
        out = "R1 10k -> NET1\nR2 [unverified range]\n" + ("필러 " * 50)
        self.assertGreaterEqual(len(out), len(primary) * 0.5)
        result = vqa._sanity_gate(out, primary)
        # expected = 14 - 1(narrowed R2) = 13(>=8), retained=1(R1) → 7.7% < 50% → degrade.
        self.assertEqual(
            result, primary,
            "narrowing 일부로 진짜 대량 증발을 가리면 안 됨(teeth 유지)",
        )

    def test_narrowing_helper_extracts_tagged_designators(self):
        """_narrowed_designators 가 태그 라인의 designator 만 정확히 뽑는지(단위)."""
        out = (
            "R1 10k -> NET1\n"           # 태그 없음 → 제외
            "U7 [unverified]\n"          # 태그 → 포함
            "C8-C60 [unverified range]\n"  # 태그 → C8/C60 포함
            "D2 100mA\n"                 # 태그 없음 → 제외
            "X1 [unreadable]\n"          # 태그 → 포함
        )
        narrowed = vqa._narrowed_designators(out)
        self.assertIn("U7", narrowed)
        self.assertIn("C8", narrowed)
        self.assertIn("C60", narrowed)
        self.assertIn("X1", narrowed)
        self.assertNotIn("R1", narrowed)
        self.assertNotIn("D2", narrowed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
