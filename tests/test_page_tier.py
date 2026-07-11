#!/usr/bin/env python3
"""test_page_tier.py — lib.page_tier 단위 테스트(claude/추출/실제 PDF 없음).

벡터 신호(_count_vector_segments)는 monkeypatch 로 가짜 segment 수를 주입한다(fitz
PDF 불요). MD 휴리스틱(designator/pin/keyword)은 실제 MD 문자열로 검증한다.

테스트 대상:
  (a) 고밀도 회로도(벡터 1500 + designator 30) → dense
  (b) 저밀도 회로도(designator 8)             → light
  (c) 텍스트 페이지(designator 0, 벡터 50)     → text
  (d) 래스터(벡터 0 + designator 20)           → light (MD 휴리스틱만, dense 불가)
  (e) 임계값 경계
  (f) 비용 가드: 앙상블 페이지 상한 초과 강등 로직(_decide_tier 와 분리해 단위 검증)

실행: python tests/test_page_tier.py    (PASS/FAIL 요약, 실패 시 exit 1)
"""
from __future__ import annotations

import os
import sys

# 워크스페이스 루트 import 경로(lib.* 패키지 import 보장).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import page_tier as pt  # noqa: E402


# ── 테스트 하네스 ────────────────────────────────────────────────────────────
_RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(cond), detail))


class _PatchVector:
    """_count_vector_segments 를 고정 (count, error) 반환으로 monkeypatch."""

    def __init__(self, count: int, error=None):
        self._count = count
        self._error = error
        self._orig = None

    def __enter__(self):
        self._orig = pt._count_vector_segments
        pt._count_vector_segments = lambda pdf_path, page: (self._count, self._error)
        return self

    def __exit__(self, *exc):
        pt._count_vector_segments = self._orig
        return False


# ── MD 픽스처 ────────────────────────────────────────────────────────────────
def _md_with_designators(n: int) -> str:
    """designator n개를 갖는 MD 생성(R/C/U 순환 — net_crosscheck 패턴 매칭)."""
    prefixes = ["R", "C", "U", "L", "J", "D", "Q"]
    lines = ["### Figure 1: schematic"]
    for i in range(1, n + 1):
        p = prefixes[i % len(prefixes)]
        lines.append(f"| {p}{i} | 10K |")
    return "\n".join(lines) + "\n"


_MD_TEXT_ONLY = (
    "# Introduction\n\n"
    "This document describes the overall product architecture and goals.\n"
    "There are no electronic components or circuits on this page.\n"
    "The methodology follows a phased rollout plan.\n"
)

_MD_PIN_NET = (
    "### Figure 2: connector\n"
    "U24 pin M8 -> DDR_DQ48\n"
    "U24 pin A1 -> VSS\n"
)


# ── 분류 테스트 ──────────────────────────────────────────────────────────────
def test_a_dense_circuit():
    """(a) 벡터 1500 + designator 30 → dense."""
    with _PatchVector(1500):
        res = pt.classify_page("/fake.pdf", 1, _md_with_designators(30))
    _check("a: dense circuit (vec1500+desig30) -> dense",
           res.tier == "dense", f"got {res.tier}, sig={res.signals}")
    _check("a: signals record vector_lines",
           res.signals.get("vector_lines") == 1500, str(res.signals))
    _check("a: signals record designators>=15",
           res.signals.get("designators") >= 15, str(res.signals.get("designators")))


def test_b_light_circuit():
    """(b) 저밀도 회로도(designator 8, 벡터 적음) → light."""
    with _PatchVector(120):  # < LIGHT_LINES(200), 하지만 designator 8 >= LIGHT_DESIG(5)
        res = pt.classify_page("/fake.pdf", 2, _md_with_designators(8))
    _check("b: low-density circuit (desig8) -> light",
           res.tier == "light", f"got {res.tier}, sig={res.signals}")


def test_c_text_page():
    """(c) 텍스트 페이지(designator 0, 벡터 50, schematic 키워드 없음) → text."""
    with _PatchVector(50):  # < LIGHT_LINES
        res = pt.classify_page("/fake.pdf", 3, _MD_TEXT_ONLY)
    _check("c: text page (desig0, vec50, no kw) -> text",
           res.tier == "text", f"got {res.tier}, sig={res.signals}")
    _check("c: designators==0", res.signals.get("designators") == 0,
           str(res.signals.get("designators")))


def test_d_raster_md_heuristic():
    """(d) 래스터(벡터 0 + designator 20) → light (MD 휴리스틱, dense 불가)."""
    with _PatchVector(0, error="no vector lines (raster?)"):
        res = pt.classify_page("/fake.pdf", 4, _md_with_designators(20))
    _check("d: raster (vec0+desig20) -> light (not dense)",
           res.tier == "light", f"got {res.tier}, sig={res.signals}")
    _check("d: vector_error recorded",
           res.signals.get("vector_error") is not None, str(res.signals))


def test_pin_net_triggers_light():
    """PIN->NET 행만 있어도(designator 적어도) light."""
    with _PatchVector(0):
        res = pt.classify_page("/fake.pdf", 5, _MD_PIN_NET)
    _check("pin->net row -> light",
           res.tier == "light", f"got {res.tier}, sig={res.signals}")
    _check("pin_rows>=1", res.signals.get("pin_rows", 0) >= 1,
           str(res.signals.get("pin_rows")))


def test_unicode_arrow_pin_row():
    """유니코드 화살표(→) PIN 행도 pin_rows 로 카운트 → light."""
    md = "### Figure 3\nVDD → U1 pin 5\n"
    with _PatchVector(0):
        res = pt.classify_page("/fake.pdf", 6, md)
    _check("unicode arrow counted as pin_row",
           res.signals.get("pin_rows", 0) >= 1, str(res.signals))


def test_keyword_only_light():
    """schematic 키워드만 있어도(designator/pin/벡터 없음) light."""
    md = "# Net List\nThe GND and VDD rails connect here.\n"
    with _PatchVector(0):
        res = pt.classify_page("/fake.pdf", 7, md)
    _check("schematic keyword -> light",
           res.tier == "light", f"got {res.tier}, kw={res.signals.get('matched_keywords')}")
    _check("matched_keywords non-empty",
           len(res.signals.get("matched_keywords", [])) > 0, str(res.signals))


# ── 임계값 경계 (e) ──────────────────────────────────────────────────────────
def test_e_dense_boundary_exact():
    """(e) 정확히 임계값(lines==800 & desig==15) → dense (>= 비교)."""
    with _PatchVector(pt.VTIER_DENSE_LINES):
        res = pt.classify_page("/fake.pdf", 8, _md_with_designators(pt.VTIER_DENSE_DESIG))
    _check("e: exact dense threshold -> dense",
           res.tier == "dense", f"got {res.tier}, sig={res.signals}")


def test_e_dense_just_below_lines():
    """(e) 벡터 799(<800), desig 30 → dense 아님(line 부족) → light(desig 충족)."""
    with _PatchVector(pt.VTIER_DENSE_LINES - 1):
        res = pt.classify_page("/fake.pdf", 9, _md_with_designators(30))
    _check("e: lines just below dense -> light",
           res.tier == "light", f"got {res.tier}, sig={res.signals}")


def test_e_dense_just_below_desig():
    """(e) 벡터 1500, desig 14(<15) → dense 아님 → light."""
    with _PatchVector(1500):
        res = pt.classify_page("/fake.pdf", 10, _md_with_designators(pt.VTIER_DENSE_DESIG - 1))
    _check("e: desig just below dense -> light",
           res.tier == "light", f"got {res.tier}, sig={res.signals}")


def test_e_light_boundary_desig():
    """(e) designator==LIGHT_DESIG(5) 경계 → light, ==4 → text(키워드 없는 MD)."""
    # designator 5개지만 schematic 키워드 회피를 위해 값만 나열.
    md5 = "\n".join(f"R{i}: 10K" for i in range(1, 6)) + "\n"
    md4 = "\n".join(f"R{i}: 10K" for i in range(1, 5)) + "\n"
    with _PatchVector(0):
        r5 = pt.classify_page("/fake.pdf", 11, md5)
        r4 = pt.classify_page("/fake.pdf", 11, md4)
    _check("e: desig==5 -> light", r5.tier == "light",
           f"got {r5.tier}, desig={r5.signals.get('designators')}")
    _check("e: desig==4 (no kw) -> text", r4.tier == "text",
           f"got {r4.tier}, desig={r4.signals.get('designators')} "
           f"kw={r4.signals.get('matched_keywords')}")


def test_e_light_lines_boundary():
    """(e) 벡터 200(==LIGHT_LINES) + designator 0 + 키워드 없음 → light(line 신호)."""
    with _PatchVector(pt.VTIER_LIGHT_LINES):
        res = pt.classify_page("/fake.pdf", 12, "plain text no parts no keywords here\n")
    _check("e: vec==200 -> light", res.tier == "light",
           f"got {res.tier}, sig={res.signals}")


# ── _decide_tier 순수함수 직접 검증 ──────────────────────────────────────────
def test_decide_tier_pure():
    """_decide_tier 순수 비교(벡터/MD 신호 조합). 반환값: (tier, dense_reason) 튜플."""
    tier, reason = pt._decide_tier(800, 15, 0, [])
    _check("decide: dense", tier == "dense", f"tier={tier}")
    _check("decide: dense reason==vec+desig", reason == "vec+desig", f"reason={reason}")
    tier2, _ = pt._decide_tier(300, 0, 0, [])
    _check("decide: line-only light", tier2 == "light", f"tier={tier2}")
    tier3, _ = pt._decide_tier(0, 0, 1, [])
    _check("decide: pin-only light", tier3 == "light", f"tier={tier3}")
    tier4, _ = pt._decide_tier(0, 0, 0, ["net"])
    _check("decide: kw-only light", tier4 == "light", f"tier={tier4}")
    tier5, _ = pt._decide_tier(50, 0, 0, [])
    _check("decide: text", tier5 == "text", f"tier={tier5}")


# ── strong_vector OR 분기 신규 테스트 ──────────────────────────────────────
def test_strong_vec_3000_desig0_dense():
    """vec=3000, desig=0 → dense(strong) + dense_reason=='strong_vector'."""
    with _PatchVector(3000):
        res = pt.classify_page("/fake.pdf", 1, "")
    _check("strong: vec3000 desig0 -> dense",
           res.tier == "dense", f"got {res.tier}")
    _check("strong: dense_reason==strong_vector",
           res.signals.get("dense_reason") == "strong_vector",
           f"dense_reason={res.signals.get('dense_reason')}")


def test_strong_vec_boundary_2500_desig0_dense():
    """vec=2500(경계 포함), desig=0 → dense(strong)."""
    with _PatchVector(pt.VTIER_DENSE_LINES_STRONG):
        res = pt.classify_page("/fake.pdf", 2, "")
    _check("strong: vec==STRONG desig0 -> dense (boundary inclusive)",
           res.tier == "dense", f"got {res.tier}")
    _check("strong: dense_reason==strong_vector at boundary",
           res.signals.get("dense_reason") == "strong_vector",
           f"dense_reason={res.signals.get('dense_reason')}")


def test_strong_vec_2499_desig0_light():
    """vec=2499(strong 미달), desig=0 → light 아님(desig/pin 없음) → text 또는 light.
    desig=0, pin=0, keyword 없음이면 text, vec>=LIGHT_LINES(200)이면 light."""
    with _PatchVector(pt.VTIER_DENSE_LINES_STRONG - 1):
        res = pt.classify_page("/fake.pdf", 3, "")
    # 2499 >= LIGHT_LINES(200) 이므로 light(벡터 신호).
    _check("strong: vec2499 desig0 -> light (not dense, not text)",
           res.tier == "light", f"got {res.tier}")
    _check("strong: no dense_reason when not dense",
           "dense_reason" not in res.signals,
           f"signals keys={list(res.signals.keys())}")


def test_strong_vec_1000_desig20_dense_vec_desig():
    """vec=1000, desig=20 → dense(기존 AND 분기) + dense_reason=='vec+desig'."""
    with _PatchVector(1000):
        res = pt.classify_page("/fake.pdf", 4, _md_with_designators(20))
    _check("strong: vec1000 desig20 -> dense (AND branch)",
           res.tier == "dense", f"got {res.tier}")
    _check("strong: dense_reason==vec+desig (AND branch)",
           res.signals.get("dense_reason") == "vec+desig",
           f"dense_reason={res.signals.get('dense_reason')}")


def test_strong_vec_1000_desig5_light():
    """vec=1000, desig=5 → dense 미달(desig<15) + light(desig>=LIGHT_DESIG)."""
    with _PatchVector(1000):
        res = pt.classify_page("/fake.pdf", 5, _md_with_designators(5))
    _check("strong: vec1000 desig5 -> light",
           res.tier == "light", f"got {res.tier}")
    _check("strong: no dense_reason when light",
           "dense_reason" not in res.signals,
           f"signals keys={list(res.signals.keys())}")


def test_strong_vec0_desig30_light_not_dense():
    """벡터 실패(vec=0, error), desig=30 → light(MD 휴리스틱만, strong 미발동).

    기존 '벡터 실패 시 최대 light' 정책 보존 확인.
    vec=0 < VTIER_DENSE_LINES_STRONG(2500) 이므로 strong 분기 발동 안 함.
    """
    with _PatchVector(0, error="raster/no vector"):
        res = pt.classify_page("/fake.pdf", 6, _md_with_designators(30))
    _check("strong: vec0(fail) desig30 -> light (NOT dense, strong 미발동)",
           res.tier == "light", f"got {res.tier}")
    _check("strong: vector_error recorded on vec0 path",
           res.signals.get("vector_error") is not None,
           str(res.signals.get("vector_error")))
    _check("strong: no dense_reason when vec0",
           "dense_reason" not in res.signals,
           f"signals keys={list(res.signals.keys())}")


# ── (f) 비용 가드 + W1/W2/W4: 실경로(real process_pdf_auto) 검증 ──────────────
#
# W3 수정: 기존 _budget_demote 복제본(테스트 재구현)을 제거하고, 실제
# extract_all_via_pdf.process_pdf_auto 를 무거운 의존(ox 추출/vqa 호출/net_tracer)을
# **monkeypatch** 하여 직접 구동한다. 실제 _AUTO_ENSEMBLE_USED 누적 카운터·강등 분기·
# W1 try/except degrade·W2 qa_called sleep 게이트·W4 라벨 분기를 *실경로*로 검증한다.
# 이로써 실코드를 >=→> 로 바꾸거나 카운터를 누수시키면 테스트가 실제로 실패한다.


class _FakePageTier:
    """ptier.classify_page 대체 반환 — 고정 tier/signals."""

    def __init__(self, tier: str):
        self.tier = tier
        self.signals = {"vector_lines": 999, "designators": 30, "pin_rows": 2,
                        "matched_keywords": ["net"], "vector_error": None}
        self.page = 0


class _FakeQAResult:
    """vqa.review / vqa_ensemble.review_ensemble 대체 반환(QAResult 형태)."""

    def __init__(self, markdown: str, corrected: bool = True, note: str = "fake"):
        self.markdown = markdown
        self.corrected = corrected
        self.backend = "fake"
        self.note = note
        self.page_pngs = []


class _FakeCrosscheck:
    """netcheck.crosscheck 대체 반환(CrosscheckResult 형태). applied 제어."""

    def __init__(self, markdown: str, applied: bool, summary=None):
        self.markdown = markdown
        self.applied = applied
        self.summary = summary or {}


class _EapHarness:
    """extract_all_via_pdf 의 무거운 의존을 monkeypatch 하는 컨텍스트.

    classify_tier 로 모든 페이지의 tier 를 고정한다(기본 dense). 옵션:
      vqa_enabled       : vqa.is_enabled() 반환값(False 면 vqa-disabled 경로).
      ensemble_raises   : review_ensemble 가 던질 예외(W1 — None 이면 정상).
      review_raises     : review(single) 가 던질 예외(W1).
      netcheck_applied  : netcheck.crosscheck 의 applied(기본 False = no-op).
    sleep 호출 횟수를 self.sleep_calls 로 계측한다(W2).
    """

    def __init__(self, eap, tier="dense", vqa_enabled=True,
                 ensemble_raises=None, review_raises=None,
                 netcheck_applied=False):
        self.eap = eap
        self.tier = tier
        self.vqa_enabled = vqa_enabled
        self.ensemble_raises = ensemble_raises
        self.review_raises = review_raises
        self.netcheck_applied = netcheck_applied
        self.sleep_calls = 0
        self.ensemble_calls = 0
        self.review_calls = 0
        self._orig = {}

    def __enter__(self):
        eap = self.eap

        def fake_extract(prompt, pdf_path, start, end):
            return f"### Figure 1\n| U24 | 10K |\nU24 pin M8 -> NET_A\n(page {start})\n"

        def fake_classify(pdf_path, page, primary_md):
            return _FakePageTier(self.tier)

        def fake_ensemble(primary_md, pdf_path, s, e, n=3, dpi=None):
            self.ensemble_calls += 1
            if self.ensemble_raises is not None:
                raise self.ensemble_raises
            return _FakeQAResult(primary_md + " [ens]", corrected=True)

        def fake_review(primary_md, pdf_path, s, e, dpi=None):
            self.review_calls += 1
            if self.review_raises is not None:
                raise self.review_raises
            return _FakeQAResult(primary_md + " [single]", corrected=True)

        def fake_crosscheck(md, pdf_path, page, timeout=120.0):
            return _FakeCrosscheck(md, applied=self.netcheck_applied,
                                   summary={"vector_confirmed": 1})

        def fake_sleep(secs):
            self.sleep_calls += 1

        # 저장 후 교체.
        self._orig["ox.extract_pdf_pages"] = eap.ox.extract_pdf_pages
        self._orig["ptier.classify_page"] = eap.ptier.classify_page
        self._orig["vqa.is_enabled"] = eap.vqa.is_enabled
        self._orig["vqa.backend_label"] = eap.vqa.backend_label
        self._orig["vqa.review"] = eap.vqa.review
        self._orig["vqa_ensemble.review_ensemble"] = eap.vqa_ensemble.review_ensemble
        self._orig["netcheck.crosscheck"] = eap.netcheck.crosscheck
        self._orig["time.sleep"] = eap.time.sleep

        eap.ox.extract_pdf_pages = fake_extract
        eap.ptier.classify_page = fake_classify
        eap.vqa.is_enabled = lambda: self.vqa_enabled
        eap.vqa.backend_label = lambda: "fake_backend"
        eap.vqa.review = fake_review
        eap.vqa_ensemble.review_ensemble = fake_ensemble
        eap.netcheck.crosscheck = fake_crosscheck
        eap.time.sleep = fake_sleep
        return self

    def __exit__(self, *exc):
        eap = self.eap
        eap.ox.extract_pdf_pages = self._orig["ox.extract_pdf_pages"]
        eap.ptier.classify_page = self._orig["ptier.classify_page"]
        eap.vqa.is_enabled = self._orig["vqa.is_enabled"]
        eap.vqa.backend_label = self._orig["vqa.backend_label"]
        eap.vqa.review = self._orig["vqa.review"]
        eap.vqa_ensemble.review_ensemble = self._orig["vqa_ensemble.review_ensemble"]
        eap.netcheck.crosscheck = self._orig["netcheck.crosscheck"]
        eap.time.sleep = self._orig["time.sleep"]
        return False


def _import_eap():
    """extract_all_via_pdf 모듈 import(실패 시 None + 체크 기록)."""
    try:
        import extract_all_via_pdf as eap  # noqa: F401
        return eap
    except Exception as e:  # noqa: BLE001
        _check("eap: extract_all_via_pdf import EXCEPTION", False, repr(e))
        return None


def test_f_budget_demotion_real_path():
    """(f) 실경로: cap+5 dense 페이지 구동 → ensemble 정확히 cap 회, 나머지 budget 강등.

    실제 process_pdf_auto 를 dense 페이지로 구동해 _AUTO_ENSEMBLE_USED 누적·상한 강등을
    *실코드*로 검증한다(W3 — >=→> off-by-one 변형이나 카운터 누수가 실제로 잡히게).
    """
    eap = _import_eap()
    if eap is None:
        return
    cap = eap.VISION_QA_MAX_ENSEMBLE_PAGES  # 기본 10.
    n_pages = cap + 5
    with _EapHarness(eap, tier="dense", vqa_enabled=True,
                     netcheck_applied=False) as h:
        page_texts, failed = eap.process_pdf_auto("/fake.pdf", n_pages)

    _check("f-real: all pages produced output (no abort)",
           len(page_texts) == n_pages and not failed,
           f"texts={len(page_texts)} failed={failed}")
    # ensemble 은 정확히 cap 회만 호출(나머지는 light 로 강등).
    _check("f-real: ensemble called exactly cap times",
           h.ensemble_calls == cap, f"ensemble_calls={h.ensemble_calls} cap={cap}")
    # 강등된 dense 페이지는 single review 로 처리(=cap+5 - cap = 5회).
    _check("f-real: demoted pages use single review",
           h.review_calls == n_pages - cap,
           f"review_calls={h.review_calls} expect={n_pages - cap}")
    # 카운터는 cap 에서 멈춘다(누수 없음).
    _check("f-real: _AUTO_ENSEMBLE_USED == cap",
           eap._AUTO_ENSEMBLE_USED == cap,
           f"used={eap._AUTO_ENSEMBLE_USED} cap={cap}")


def test_f_budget_counter_resets_across_pdfs():
    """(f) 멀티 PDF 2회 구동 후 카운터 누적 안 됨(각 PDF 진입 시 0 리셋)."""
    eap = _import_eap()
    if eap is None:
        return
    cap = eap.VISION_QA_MAX_ENSEMBLE_PAGES
    with _EapHarness(eap, tier="dense", vqa_enabled=True) as h1:
        eap.process_pdf_auto("/fake1.pdf", 3)
        used_after_1 = eap._AUTO_ENSEMBLE_USED
    with _EapHarness(eap, tier="dense", vqa_enabled=True) as h2:
        eap.process_pdf_auto("/fake2.pdf", 3)
        used_after_2 = eap._AUTO_ENSEMBLE_USED
    # 각 PDF 는 3 dense 페이지만 — cap(10) 이하이므로 누적 없이 각 3.
    _check("f-real: PDF1 used==3", used_after_1 == 3, f"used1={used_after_1}")
    _check("f-real: PDF2 used==3 (reset, not 6)", used_after_2 == 3,
           f"used2={used_after_2} (누적되면 6)")


def test_w1_dense_qa_exception_degrades_and_batch_continues():
    """(W1) dense QA(ensemble)가 예외를 던져도 그 페이지는 primary_md 로 degrade되고
    배치는 중단되지 않으며 다른 페이지도 생존한다(PDF 전체 abort 방지)."""
    eap = _import_eap()
    if eap is None:
        return
    boom = RuntimeError("simulated ensemble blowup")
    with _EapHarness(eap, tier="dense", vqa_enabled=True,
                     ensemble_raises=boom) as h:
        page_texts, failed = eap.process_pdf_auto("/fake.pdf", 4)
    # 예외에도 4페이지 전부 산출(abort 없음).
    _check("W1: dense QA exception → batch NOT aborted (all pages survive)",
           len(page_texts) == 4 and not failed,
           f"texts={len(page_texts)} failed={failed}")
    # degrade 산출물은 primary_md(=fake_extract 출력) 그대로여야 한다([ens] 미부착).
    _check("W1: degraded page keeps primary_md (no [ens] suffix)",
           all("[ens]" not in t for t in page_texts),
           f"texts={[t[:40] for t in page_texts]}")
    # ensemble 은 실제 호출되었으나(예외 발생) → review(single)로 폴백하지 않음.
    _check("W1: ensemble was actually attempted", h.ensemble_calls == 4,
           f"ensemble_calls={h.ensemble_calls}")


def test_w1_light_qa_exception_degrades():
    """(W1) light QA(single review)가 예외를 던져도 페이지 degrade + 배치 계속."""
    eap = _import_eap()
    if eap is None:
        return
    boom = ValueError("simulated single-review blowup")
    with _EapHarness(eap, tier="light", vqa_enabled=True,
                     review_raises=boom) as h:
        page_texts, failed = eap.process_pdf_auto("/fake.pdf", 3)
    _check("W1: light QA exception → batch NOT aborted",
           len(page_texts) == 3 and not failed,
           f"texts={len(page_texts)} failed={failed}")
    _check("W1: light degraded page keeps primary_md (no [single] suffix)",
           all("[single]" not in t for t in page_texts),
           f"texts={[t[:40] for t in page_texts]}")


def test_w1_strength_records_qa_failed():
    """(W1) QA 예외 시 strength 에 'qa-failed' 사유가 기록된다(가시성)."""
    eap = _import_eap()
    if eap is None:
        return
    boom = RuntimeError("boom")
    with _EapHarness(eap, tier="dense", vqa_enabled=True, ensemble_raises=boom):
        md, record = eap.extract_page_auto("/fake.pdf", 1)
    _check("W1: strength contains qa-failed",
           "qa-failed" in record.get("strength", ""),
           f"strength={record.get('strength')}")
    _check("W1: degraded record qa_called False on exception",
           record.get("qa_called") is False,
           f"qa_called={record.get('qa_called')}")


def test_w2_vqa_disabled_no_sleep():
    """(W2) VISION_QA 미설정(vqa-disabled) + AUTO=1: dense tier여도 sleep 미발생.

    기존 버그: record.tier in (dense,light) 게이트라 vqa-disabled 페이지마다 10초
    sleep(40p=6분 낭비). 수정 후 qa_called=False 이므로 sleep 0회여야 한다."""
    eap = _import_eap()
    if eap is None:
        return
    with _EapHarness(eap, tier="dense", vqa_enabled=False) as h:
        page_texts, failed = eap.process_pdf_auto("/fake.pdf", 5)
    _check("W2: vqa-disabled produces all pages", len(page_texts) == 5 and not failed,
           f"texts={len(page_texts)} failed={failed}")
    _check("W2: NO sleep when vqa disabled (was 5× before fix)",
           h.sleep_calls == 0, f"sleep_calls={h.sleep_calls}")
    # strength 는 vqa-disabled, qa_called False.
    md, record = (None, None)
    with _EapHarness(eap, tier="dense", vqa_enabled=False):
        md, record = eap.extract_page_auto("/fake.pdf", 1)
    _check("W2: strength==vqa-disabled", record.get("strength") == "vqa-disabled",
           f"strength={record.get('strength')}")
    _check("W2: qa_called False when disabled", record.get("qa_called") is False,
           f"qa_called={record.get('qa_called')}")


def test_w2_enabled_dense_sleeps_per_qa_page():
    """(W2) vqa 활성 dense 페이지는 QA 호출이 실제 발생 → 페이지당 sleep 1회."""
    eap = _import_eap()
    if eap is None:
        return
    with _EapHarness(eap, tier="dense", vqa_enabled=True) as h:
        eap.process_pdf_auto("/fake.pdf", 3)
    _check("W2: enabled dense → sleep per QA page (3)",
           h.sleep_calls == 3, f"sleep_calls={h.sleep_calls}")


def test_w2_text_tier_no_sleep():
    """(W2) text tier 페이지는 QA 호출 없음 → sleep 0(qa_called=False)."""
    eap = _import_eap()
    if eap is None:
        return
    with _EapHarness(eap, tier="text", vqa_enabled=True) as h:
        eap.process_pdf_auto("/fake.pdf", 4)
    _check("W2: text tier → no sleep", h.sleep_calls == 0,
           f"sleep_calls={h.sleep_calls}")


def test_w4_netcheck_noop_label():
    """(W4) netcheck degrade(applied=False) 시 strength 라벨에 '+netcheck(noop)' 부착."""
    eap = _import_eap()
    if eap is None:
        return
    # applied=False → noop 라벨.
    with _EapHarness(eap, tier="dense", vqa_enabled=True, netcheck_applied=False):
        md, rec_noop = eap.extract_page_auto("/fake.pdf", 1)
    _check("W4: netcheck no-op → '+netcheck(noop))' label",
           rec_noop.get("strength") == "dense(ensemble+netcheck(noop))",
           f"strength={rec_noop.get('strength')}")
    # applied=True → 기존 '+netcheck)' 라벨 유지(noop 미부착).
    with _EapHarness(eap, tier="dense", vqa_enabled=True, netcheck_applied=True):
        md, rec_ok = eap.extract_page_auto("/fake.pdf", 1)
    _check("W4: netcheck applied → '+netcheck)' label (no noop)",
           rec_ok.get("strength") == "dense(ensemble+netcheck)",
           f"strength={rec_ok.get('strength')}")
    # light 도 동일 — applied=False noop.
    with _EapHarness(eap, tier="light", vqa_enabled=True, netcheck_applied=False):
        md, rec_l = eap.extract_page_auto("/fake.pdf", 1)
    _check("W4: light netcheck no-op label",
           rec_l.get("strength") == "light(single+netcheck(noop))",
           f"strength={rec_l.get('strength')}")


def test_eap_importable_and_auto_fns_present():
    """(통합) extract_all_via_pdf import 가능 + auto 함수/가드 상수 존재 정합."""
    eap = _import_eap()
    if eap is None:
        return
    _check("eap: auto fns present (extract_page_auto/process_pdf_auto)",
           hasattr(eap, "extract_page_auto") and hasattr(eap, "process_pdf_auto"))
    _check("eap: VISION_QA_MAX_ENSEMBLE_PAGES present",
           hasattr(eap, "VISION_QA_MAX_ENSEMBLE_PAGES"))


# ── 벡터 카운터 graceful degrade (실파일 없음) ──────────────────────────────
def test_vector_missing_file_graceful():
    """존재하지 않는 PDF → (0, error) graceful, 분류는 MD 만으로."""
    # 실제 _count_vector_segments 호출(monkeypatch 안 함) — 없는 파일.
    count, err = pt._count_vector_segments("/nonexistent_xyz.pdf", 1)
    _check("vec: missing file -> (0, error)", count == 0 and err is not None,
           f"count={count} err={err}")


def main() -> int:
    tests = [
        test_a_dense_circuit,
        test_b_light_circuit,
        test_c_text_page,
        test_d_raster_md_heuristic,
        test_pin_net_triggers_light,
        test_unicode_arrow_pin_row,
        test_keyword_only_light,
        test_e_dense_boundary_exact,
        test_e_dense_just_below_lines,
        test_e_dense_just_below_desig,
        test_e_light_boundary_desig,
        test_e_light_lines_boundary,
        test_decide_tier_pure,
        # strong_vector OR 분기 신규 테스트.
        test_strong_vec_3000_desig0_dense,
        test_strong_vec_boundary_2500_desig0_dense,
        test_strong_vec_2499_desig0_light,
        test_strong_vec_1000_desig20_dense_vec_desig,
        test_strong_vec_1000_desig5_light,
        test_strong_vec0_desig30_light_not_dense,
        # (f) + W1/W2/W4 실경로(real process_pdf_auto / extract_page_auto) 검증.
        test_f_budget_demotion_real_path,
        test_f_budget_counter_resets_across_pdfs,
        test_w1_dense_qa_exception_degrades_and_batch_continues,
        test_w1_light_qa_exception_degrades,
        test_w1_strength_records_qa_failed,
        test_w2_vqa_disabled_no_sleep,
        test_w2_enabled_dense_sleeps_per_qa_page,
        test_w2_text_tier_no_sleep,
        test_w4_netcheck_noop_label,
        test_eap_importable_and_auto_fns_present,
        test_vector_missing_file_graceful,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            _check(f"{t.__name__}: EXCEPTION", False, repr(e))

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print("=" * 64)
    for name, ok, detail in _RESULTS:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f"\n        detail: {detail[:300]}"
        print(line)
    print("=" * 64)
    print(f"{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
