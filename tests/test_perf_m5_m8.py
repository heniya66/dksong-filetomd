"""test_perf_m5_m8.py — QA_REVIEW 2026-06-04 Medium 성능 4건(M-5~M-8) 회귀 가드.

대상: extract_all_via_pdf.py + lib/net_crosscheck.py + lib/page_tier.py +
      lib/ollama_extractor.py + scripts/run_net_tracer.py.

검증 원칙 = **동작(출력) 보존**:
  - M-5(페이지 재오픈): 공유 fitz 핸들을 1회만 열어 render/classify 에 전달하되,
    핸들 미제공(None) 시 기존 내부 open 경로로 graceful degrade — 분류 결과 동일.
  - M-6(무조건 sleep): _RateLimiter 토큰버킷 + 마지막 호출 뒤 sleep 생략. base_delay=0
    이면 sleep 0. 출력 MD 는 sleep 변경과 무관(불변).
  - M-7(net_tracer 재실행): run_net_tracer_range 가 범위를 1회 서브프로세스로 호출해
    페이지별 JSON 맵을 반환. crosscheck_with_tracer == crosscheck(동일 tracer) 결과.
    apply_netcheck 가 페이지마다 서브프로세스를 재실행하지 않고 1회만 호출.
  - M-8(실패 시 전체 재추출): extract_chunk_with_page_fallback 가 청크 실패 시 페이지
    단위로 재추출(제한 재시도)해 살릴 수 있는 페이지를 살리고, 실패 페이지만 MISSING.
    성공 청크는 입력 그대로(byte-동일).

실행:
    .venv/bin/python -m pytest tests/test_perf_m5_m8.py -v
"""
from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import extract_all_via_pdf as eap  # noqa: E402
from fmdw import net_crosscheck as nc  # noqa: E402
from fmdw import page_tier as pt  # noqa: E402
from fmdw import ollama_extractor as ox  # noqa: E402


def _load_runner():
    runner_path = os.path.join(_ROOT, "scripts", "run_net_tracer.py")
    spec = importlib.util.spec_from_file_location("run_net_tracer", runner_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    return mod


# ──────────────────────────────────────────────────────────────────────────────
# M-5: 공유 fitz 핸들 재사용 (페이지 재오픈 제거)
# ──────────────────────────────────────────────────────────────────────────────

class _FakeDoc:
    """fitz.Document 흉내 — page_count + load_page/__getitem__ 만. close 카운트."""

    def __init__(self, page_count: int):
        self.page_count = page_count
        self.closed = 0

    def load_page(self, idx):
        return MagicMock(get_drawings=MagicMock(return_value=[]))

    def __getitem__(self, idx):  # render 경로용
        pm = MagicMock()
        pm.tobytes = MagicMock(return_value=b"\x89PNG")
        page = MagicMock()
        page.get_pixmap = MagicMock(return_value=pm)
        return page

    def close(self):
        self.closed += 1


class TestM5SharedDocHandle(unittest.TestCase):
    """공유 핸들이 주어지면 재오픈/재close 하지 않고 재사용한다."""

    def test_count_vector_segments_reuses_doc_no_close(self):
        """doc 제공 시 fitz.open 미호출 + doc.close 미호출(호출자 소유)."""
        doc = _FakeDoc(page_count=3)
        with patch("fitz.open", side_effect=AssertionError("must not reopen")):
            count, err = pt._count_vector_segments("/fake.pdf", 2, doc=doc)
        self.assertEqual(count, 0)        # get_drawings=[] → 0 segment
        self.assertIsNone(err)
        self.assertEqual(doc.closed, 0)   # 외부 핸들 — 닫지 않음

    def test_count_vector_segments_owns_doc_closes(self):
        """doc 미제공 시 내부 open + close(기존 동작)."""
        doc = _FakeDoc(page_count=2)
        with patch("fitz.open", return_value=doc) as mopen:
            count, err = pt._count_vector_segments("/fake.pdf", 1)
        mopen.assert_called_once()
        self.assertEqual(doc.closed, 1)   # 내부 핸들 — 닫음

    def test_classify_page_with_doc_matches_without(self):
        """공유 핸들 유무에 따라 분류 결과 동일(동작 보존)."""
        md = "### Figure 1: schematic\n| R1 | 10K |\n| C2 | net VDD |\n"
        doc = _FakeDoc(page_count=1)
        # with doc: open 금지(재사용).
        with patch("fitz.open", side_effect=AssertionError("must not reopen")):
            res_with = pt.classify_page("/fake.pdf", 1, md, doc=doc)
        # without doc: 내부 open(같은 FakeDoc 반환).
        doc2 = _FakeDoc(page_count=1)
        with patch("fitz.open", return_value=doc2):
            res_without = pt.classify_page("/fake.pdf", 1, md)
        self.assertEqual(res_with.tier, res_without.tier)
        self.assertEqual(res_with.signals["designators"],
                         res_without.signals["designators"])

    def test_render_pdf_pages_reuses_doc(self):
        """ox.render_pdf_pages_to_base64 가 doc 재사용 시 open/close 안 함."""
        doc = _FakeDoc(page_count=2)
        with patch("fitz.open", side_effect=AssertionError("must not reopen")):
            imgs = ox.render_pdf_pages_to_base64("/fake.pdf", 1, 2, doc=doc)
        self.assertEqual(len(imgs), 2)
        self.assertEqual(doc.closed, 0)


# ──────────────────────────────────────────────────────────────────────────────
# M-6: _RateLimiter — 토큰버킷 + 마지막 호출 뒤 sleep 생략 + 설정화
# ──────────────────────────────────────────────────────────────────────────────

class TestM6RateLimiter(unittest.TestCase):
    def test_base_delay_zero_no_sleep(self):
        """base_delay=0 → wait_before_next 가 sleep 하지 않음(0 반환)."""
        lim = eap._RateLimiter(base_delay=0.0)
        lim.mark_call_end()
        self.assertEqual(lim.wait_before_next(), 0.0)

    def test_base_delay_sleeps_remaining_only(self):
        """경과시간만큼 차감하고 잔여만 sleep(토큰버킷)."""
        slept = []
        lim = eap._RateLimiter(base_delay=10.0)
        # monotonic 을 제어: mark_call_end 시각 t=100, wait 시각 t=103 → 잔여 7.
        times = iter([100.0, 103.0])
        with patch("time.monotonic", side_effect=lambda: next(times)), \
             patch("time.sleep", side_effect=lambda s: slept.append(s)):
            lim.mark_call_end()
            waited = lim.wait_before_next()
        self.assertAlmostEqual(waited, 7.0, places=3)
        self.assertEqual(len(slept), 1)

    def test_adaptive_429_extra(self):
        """note_rate_limited 가 다음 1회 대기에 extra 가산(적응형)."""
        slept = []
        lim = eap._RateLimiter(base_delay=0.0)
        with patch("time.sleep", side_effect=lambda s: slept.append(s)):
            lim.note_rate_limited(5.0)
            waited = lim.wait_before_next()
            # 적응형은 1회성 — 다음엔 0.
            waited2 = lim.wait_before_next()
        self.assertAlmostEqual(waited, 5.0)
        self.assertEqual(waited2, 0.0)


class TestM6AutoPathSleep(unittest.TestCase):
    """AUTO 경로: qa_called 페이지 N개 → sleep N-1회(마지막 뒤 생략)."""

    def _run_auto(self, qa_flags, base_delay):
        """qa_flags: 페이지별 record['qa_called'] 불리언 리스트. 반환: sleep 횟수."""
        slept = []
        n = len(qa_flags)

        def fake_page_auto(pdf, page):  # doc=None 경로(2-인자)
            qa = qa_flags[page - 1]
            return f"PAGE{page}", {
                "page": page, "tier": "light", "strength": "light",
                "signals": {}, "qa_called": qa,
            }

        with patch.object(eap, "extract_page_auto", side_effect=fake_page_auto), \
             patch.object(eap, "VISION_QA_RATE_DELAY", base_delay), \
             patch("fitz.open", side_effect=RuntimeError("force doc=None")), \
             patch("time.sleep", side_effect=lambda s: slept.append(s)), \
             patch("time.monotonic", side_effect=lambda: 0.0):
            page_texts, failed = eap.process_pdf_auto("/fake.pdf", n)
        return slept, page_texts, failed

    def test_three_qa_pages_two_sleeps(self):
        slept, texts, failed = self._run_auto([True, True, True], base_delay=5.0)
        self.assertEqual(len(slept), 2, f"3 QA 페이지 → sleep 2회: {slept}")
        self.assertEqual(failed, [])
        # 출력 보존: 페이지 MD 순서대로.
        self.assertEqual(texts, ["PAGE1", "PAGE2", "PAGE3"])

    def test_no_sleep_when_no_qa_called(self):
        """qa_called=False(vqa-disabled/text) 페이지는 sleep 0회."""
        slept, _, _ = self._run_auto([False, False, False], base_delay=10.0)
        self.assertEqual(len(slept), 0, f"QA 미호출 → sleep 0: {slept}")

    def test_base_delay_zero_no_sleep(self):
        slept, _, _ = self._run_auto([True, True], base_delay=0.0)
        self.assertEqual(len(slept), 0)

    def test_last_qa_page_no_trailing_sleep(self):
        """마지막 페이지만 QA → sleep 0(끝 호출 뒤 생략)."""
        slept, _, _ = self._run_auto([False, False, True], base_delay=8.0)
        self.assertEqual(len(slept), 0, f"마지막 QA 뒤 sleep 없어야: {slept}")


class TestM6ChunkPathSleep(unittest.TestCase):
    """청크 경로: N청크 성공 시 sleep N-1회(마지막 뒤 생략), base_delay 설정 반영."""

    def _run_chunks(self, total_pages, chunk_size, base_delay):
        slept = []
        with patch.object(eap.ox, "count_pdf_pages", return_value=total_pages), \
             patch.object(eap, "extract_chunk_with_page_fallback",
                          side_effect=lambda p, s, e, i: ("text", [])), \
             patch.object(eap, "CHUNK_SIZE", chunk_size), \
             patch.object(eap, "VISION_QA_AUTO", 0), \
             patch.object(eap, "VISION_QA_RATE_DELAY", base_delay), \
             patch("time.sleep", side_effect=lambda s: slept.append(s)), \
             patch("time.monotonic", side_effect=lambda: 0.0):
            # process_file 의 chunk 루프만 검증하기 위해 저장 단계는 무해(tmp).
            import tempfile
            from pathlib import Path
            with tempfile.TemporaryDirectory() as tmp:
                orig = os.getcwd()
                os.chdir(tmp)
                try:
                    fake_pdf = Path(tmp) / "doc.pdf"
                    fake_pdf.write_bytes(b"%PDF-1.4 fake")
                    eap.process_file(fake_pdf, "pdf_md")
                finally:
                    os.chdir(orig)
        return slept

    def test_single_chunk_no_sleep(self):
        slept = self._run_chunks(20, 20, base_delay=10.0)
        self.assertEqual(len(slept), 0, f"단일 청크 뒤 sleep 없어야 함: {slept}")

    def test_three_chunks_two_sleeps(self):
        slept = self._run_chunks(50, 20, base_delay=7.0)
        self.assertEqual(len(slept), 2, f"3청크 → sleep 2회: {slept}")
        for s in slept:
            self.assertAlmostEqual(s, 7.0, places=3)

    def test_base_delay_zero_no_sleep_even_multi_chunk(self):
        slept = self._run_chunks(50, 20, base_delay=0.0)
        self.assertEqual(len(slept), 0, f"base_delay=0 → sleep 0: {slept}")


# ──────────────────────────────────────────────────────────────────────────────
# M-7: net_tracer 범위 1회 호출 + crosscheck_with_tracer 동등성
# ──────────────────────────────────────────────────────────────────────────────

_MOCK_TRACER_OK = {
    "ok": True,
    "page": 11,
    "nets": [
        {"name": "DDR_DQ48", "connections": [{"ref": "U24", "pin": "M8"}]},
    ],
    "no_connects": [],
    "junctions": [],
    "stats": {"lines": 500, "texts": 80, "total_nets": 1, "named_nets": 1},
}


class TestM7CrosscheckWithTracer(unittest.TestCase):
    def test_with_tracer_equals_crosscheck(self):
        """crosscheck_with_tracer(md, T) == crosscheck(md,...) (동일 tracer T)."""
        md = "| U24 pin M8 -> DDR_DQ48 |\n"
        # crosscheck 는 _run_net_tracer 를 호출 → 같은 tracer 로 monkeypatch.
        orig = nc._run_net_tracer
        nc._run_net_tracer = lambda pdf, page, timeout=120.0: _MOCK_TRACER_OK  # type: ignore
        try:
            via_crosscheck = nc.crosscheck(md, "/fake.pdf", 11)
        finally:
            nc._run_net_tracer = orig  # type: ignore
        via_tracer = nc.crosscheck_with_tracer(md, _MOCK_TRACER_OK)
        self.assertEqual(via_crosscheck.markdown, via_tracer.markdown)
        self.assertEqual(via_crosscheck.applied, via_tracer.applied)
        self.assertEqual(via_crosscheck.summary["vector_confirmed"],
                         via_tracer.summary["vector_confirmed"])

    def test_with_tracer_degrade_on_not_ok(self):
        """tracer.ok=False → MD 그대로(applied=False)."""
        md = "| U99 -> NET |\n"
        res = nc.crosscheck_with_tracer(md, {"ok": False, "reason": "raster"})
        self.assertEqual(res.markdown, md)
        self.assertFalse(res.applied)


class TestM7RunNetTracerRange(unittest.TestCase):
    """run_net_tracer_range — 서브프로세스 1회 호출 + 페이지별 JSON 맵."""

    def test_range_single_subprocess_call(self):
        """범위 호출 시 subprocess.run 이 1회만 실행(페이지 수만큼 재실행 아님)."""
        # 러너 stdout: 페이지당 JSON 1줄.
        lines = "\n".join(
            f'{{"ok": true, "page": {p}, "nets": [], "no_connects": [], '
            f'"junctions": [], "stats": {{}}}}'
            for p in (3, 4, 5)
        )
        fake_proc = MagicMock(stdout=lines, returncode=0)
        with patch.object(nc, "_runner_python", return_value="python"), \
             patch.object(nc.os.path, "isfile", return_value=True), \
             patch("subprocess.run", return_value=fake_proc) as mrun:
            result = nc.run_net_tracer_range("/fake.pdf", 3, 5)
        self.assertEqual(mrun.call_count, 1, "범위는 서브프로세스 1회만 호출")
        # 호출 인자에 start/end 가 전달됐는지(범위 모드).
        argv = mrun.call_args[0][0]
        self.assertIn("3", argv)
        self.assertIn("5", argv)
        # 페이지별 맵.
        self.assertEqual(set(result.keys()), {3, 4, 5})
        for p in (3, 4, 5):
            self.assertTrue(result[p]["ok"])
            self.assertEqual(result[p]["page"], p)

    def test_range_missing_page_degraded(self):
        """러너가 일부 페이지 줄을 안 내면 그 페이지는 degrade dict 로 채워짐."""
        only_p3 = '{"ok": true, "page": 3, "nets": [], "stats": {}}'
        fake_proc = MagicMock(stdout=only_p3, returncode=0)
        with patch.object(nc, "_runner_python", return_value="python"), \
             patch.object(nc.os.path, "isfile", return_value=True), \
             patch("subprocess.run", return_value=fake_proc):
            result = nc.run_net_tracer_range("/fake.pdf", 3, 5)
        self.assertTrue(result[3]["ok"])
        self.assertFalse(result[4]["ok"])
        self.assertFalse(result[5]["ok"])

    def test_range_subprocess_failure_all_degraded(self):
        """전체 호출 실패 → 모든 페이지 degrade(누락 페이지 없음)."""
        with patch.object(nc, "_runner_python", return_value="python"), \
             patch.object(nc.os.path, "isfile", return_value=True), \
             patch("subprocess.run", side_effect=OSError("boom")):
            result = nc.run_net_tracer_range("/fake.pdf", 1, 3)
        self.assertEqual(set(result.keys()), {1, 2, 3})
        for p in (1, 2, 3):
            self.assertFalse(result[p]["ok"])


class TestM7ApplyNetcheckSingleSubprocess(unittest.TestCase):
    """apply_netcheck(청크) 가 페이지마다 서브프로세스 재실행하지 않고 range 1회."""

    def test_apply_netcheck_calls_range_once(self):
        md = "| U24 pin M8 -> DDR_DQ48 |\n"
        called = {"range": 0, "single": 0}

        def fake_range(pdf, s, e, timeout=120.0):
            called["range"] += 1
            return {p: _MOCK_TRACER_OK for p in range(s, e + 1)}

        def fake_single(pdf, page, timeout=120.0):
            called["single"] += 1
            return _MOCK_TRACER_OK

        with patch.object(eap, "VISION_QA_NETCHECK", 1), \
             patch.object(nc, "run_net_tracer_range", side_effect=fake_range), \
             patch.object(nc, "_run_net_tracer", side_effect=fake_single):
            out = eap.apply_netcheck(md, "/fake.pdf", 1, 5)
        self.assertEqual(called["range"], 1, "range 1회 호출")
        self.assertEqual(called["single"], 0, "페이지별 단일 서브프로세스 재실행 없음")
        self.assertIsInstance(out, str)


class TestM7RunnerArgParse(unittest.TestCase):
    """run_net_tracer.py 의 argv 파싱 — 단일/범위 모두 + 잘못된 argv 비크래시."""

    def setUp(self):
        self.mod = _load_runner()

    def test_single_page_argv(self):
        parsed, reason = self.mod._parse_args(["run.py", "/a.pdf", "7"])
        self.assertIsNone(reason)
        self.assertEqual(parsed, ("/a.pdf", 7, 7))

    def test_range_argv(self):
        parsed, reason = self.mod._parse_args(["run.py", "/a.pdf", "3", "9"])
        self.assertIsNone(reason)
        self.assertEqual(parsed, ("/a.pdf", 3, 9))

    def test_bad_argv_returns_reason(self):
        parsed, reason = self.mod._parse_args(["run.py"])
        self.assertIsNone(parsed)
        self.assertIsInstance(reason, str)

    def test_range_end_lt_start_rejected(self):
        parsed, reason = self.mod._parse_args(["run.py", "/a.pdf", "9", "3"])
        self.assertIsNone(parsed)
        self.assertIn("end", reason.lower())


# ──────────────────────────────────────────────────────────────────────────────
# M-8: 청크 실패 시 페이지 단위 폴백
# ──────────────────────────────────────────────────────────────────────────────

class TestM8PageFallback(unittest.TestCase):
    """extract_chunk_with_page_fallback — 동작 보존 + 페이지 단위 resume."""

    def test_chunk_success_byte_identical(self):
        """청크가 첫 시도에 성공 → 입력 그대로(폴백 미발동, byte-동일)."""
        with patch.object(eap, "extract_chunk", return_value="CHUNK-OK") as mc:
            text, failed = eap.extract_chunk_with_page_fallback(
                "/fake.pdf", 1, 20, 1)
        self.assertEqual(text, "CHUNK-OK")
        self.assertEqual(failed, [])
        self.assertEqual(mc.call_count, 1, "성공 시 폴백 없이 1회 호출")

    def test_partial_recovery_salvages_pages(self):
        """청크 실패 → 페이지 폴백으로 일부 복구, 실패 페이지만 MISSING."""
        # 청크(1-3) 실패 → 페이지별: p1 성공, p2 실패, p3 성공.
        def fake_extract(pdf, s, e, idx):
            if s == 1 and e == 3:
                return None              # 청크 전체 실패
            if s == e == 2:
                return None              # 페이지 2 실패(재시도도 실패)
            return f"PAGE{s}"            # 나머지 페이지 성공

        with patch.object(eap, "extract_chunk", side_effect=fake_extract), \
             patch.object(eap, "EXTRACT_PAGE_RETRIES", 0):
            text, failed = eap.extract_chunk_with_page_fallback(
                "/fake.pdf", 1, 3, 1)
        self.assertEqual(failed, [2])
        self.assertIn("PAGE1", text)
        self.assertIn("PAGE3", text)
        self.assertIn("<!-- MISSING page 2", text)
        # 청크 결합 구분자 보존.
        self.assertEqual(text.count("\n\n---\n\n"), 2)

    def test_all_pages_fail_returns_none(self):
        """폴백으로도 한 페이지도 못 살리면 (None, 전체범위)."""
        with patch.object(eap, "extract_chunk", return_value=None), \
             patch.object(eap, "EXTRACT_PAGE_RETRIES", 0):
            text, failed = eap.extract_chunk_with_page_fallback(
                "/fake.pdf", 4, 6, 2)
        self.assertIsNone(text)
        self.assertEqual(failed, [4, 5, 6])

    def test_single_page_chunk_bounded_retry(self):
        """단일 페이지 청크 실패 → 제한 재시도 후 복구."""
        calls = {"n": 0}

        def fake_extract(pdf, s, e, idx):
            calls["n"] += 1
            # 첫 시도(청크=페이지) 실패, 재시도 1회차 성공.
            return None if calls["n"] == 1 else "RECOVERED"

        with patch.object(eap, "extract_chunk", side_effect=fake_extract), \
             patch.object(eap, "EXTRACT_PAGE_RETRIES", 1):
            text, failed = eap.extract_chunk_with_page_fallback(
                "/fake.pdf", 5, 5, 3)
        self.assertEqual(text, "RECOVERED")
        self.assertEqual(failed, [])

    def test_retry_limit_respected(self):
        """제한 재시도 횟수를 초과하지 않는다(무한 루프 방지)."""
        calls = {"n": 0}

        def fake_extract(pdf, s, e, idx):
            calls["n"] += 1
            return None  # 항상 실패

        with patch.object(eap, "extract_chunk", side_effect=fake_extract), \
             patch.object(eap, "EXTRACT_PAGE_RETRIES", 2):
            text, failed = eap.extract_chunk_with_page_fallback(
                "/fake.pdf", 9, 9, 4)
        self.assertIsNone(text)
        self.assertEqual(failed, [9])
        # 1(청크) + 3(페이지 시도 = retries+1) = 4 (단일 페이지 청크는 청크==페이지지만
        # 구현상 청크 1회 + 페이지 (retries+1)회).
        self.assertEqual(calls["n"], 1 + (2 + 1))


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
