"""test_resume_cache.py — lib/resume_cache.py + convert_pdf resume 경로 단위 테스트.

대용량 PDF resume(중단 후 이어받기) 캐시 검증. 실제 PDF / 네트워크 없이
ox.extract_pdf_pages / ox.count_pdf_pages / ox.provider_label 을 mock 한다.

테스트 항목:
  A) ResumeCache 저수준 단위
     - store/load 라운드트립, sha 무결성, 손상 manifest graceful, 키 검증, cleanup
  B) compute_cache_key — PDF 지문/청크정책/프롬프트/provider 변경 시 키 변동
  C) convert_pdf resume 통합
     (a) 캐시 없을 때 전체 추출
     (b) 일부 청크 캐시 존재 → 해당 청크 skip + 나머지만 추출
     (c) PDF 변경(mtime/size) → 캐시 무효화(전부 재추출)
     (d) resume=False → 캐시 디렉토리 미생성 + 기존 동작 동일
  D) 전체 성공 시 캐시 정리 / 부분 실패 시 캐시 보존

실행:
    .venv/bin/python -m pytest tests/test_resume_cache.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import time as _time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import pdf_pipeline as pp
from fmdw import resume_cache as rc


# ──────────────────────────────────────────────────────────────────────────────
# 공통 mock 헬퍼 — extract 호출을 카운트하며 청크별 텍스트 반환
# ──────────────────────────────────────────────────────────────────────────────

def _mk_extract(responses=None):
    """ox.extract_pdf_pages mock. (start,end) 별 호출 기록.

    responses: 청크 순서대로 반환값. None 이면 'X_{start}_{end}' 자동 생성.
               Exception 인스턴스면 raise.
    """
    calls = []
    idx = [0]

    def fake_extract(prompt, pdf_path, start, end):
        calls.append((start, end))
        i = idx[0]
        idx[0] += 1
        if responses is not None and i < len(responses):
            r = responses[i]
            if isinstance(r, Exception):
                raise r
            return r
        return f"X_{start}_{end}"

    return MagicMock(side_effect=fake_extract), calls


def _make_pdf(tmp: Path, name="doc.pdf", content=b"%PDF-1.4 fake") -> Path:
    p = tmp / name
    p.write_bytes(content)
    return p


# ──────────────────────────────────────────────────────────────────────────────
# A) ResumeCache 저수준 단위
# ──────────────────────────────────────────────────────────────────────────────

class TestResumeCacheLowLevel(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.pdf = _make_pdf(self.tmp)
        self.out = self.tmp / "out" / "doc.md"

    def tearDown(self):
        self._td.cleanup()

    def _open(self, key="k0", chunk_size=20, scm=None, prompt="p", prov="prov"):
        return rc.ResumeCache.open(
            self.pdf, self.out, key=key,
            chunk_size=chunk_size, single_chunk_max=scm,
            prompt_template=prompt, provider_label=prov,
        )

    def test_store_load_roundtrip(self):
        c = self._open()
        self.assertIsNone(c.load(0))
        c.store(0, 1, 20, "hello chunk")
        # 같은 캐시 인스턴스 load
        self.assertEqual(c.load(0), "hello chunk")
        # 새 인스턴스(디스크 manifest 재로드) 도 동일
        c2 = self._open()
        self.assertEqual(c2.load(0), "hello chunk")
        self.assertEqual(c2.cached_indices(), {0})

    def test_sha_integrity_mismatch_is_miss(self):
        c = self._open()
        c.store(0, 1, 20, "original")
        # chunk_0.md 파일 내용을 손상 → sha 불일치 → load 미스(None).
        chunk_file = c.cache_dir / "chunk_0.md"
        chunk_file.write_text("TAMPERED", encoding="utf-8")
        c2 = self._open()
        self.assertIsNone(c2.load(0), "sha 불일치 시 미스여야 함")

    def test_missing_chunk_file_is_miss(self):
        c = self._open()
        c.store(0, 1, 20, "data")
        (c.cache_dir / "chunk_0.md").unlink()
        c2 = self._open()
        self.assertIsNone(c2.load(0))

    def test_corrupt_manifest_graceful(self):
        c = self._open()
        c.store(0, 1, 20, "data")
        (c.cache_dir / "manifest.json").write_text("{not json", encoding="utf-8")
        # 손상 manifest → 빈 캐시로 degrade(예외 없음).
        c2 = self._open()
        self.assertEqual(c2.cached_indices(), set())
        self.assertIsNone(c2.load(0))

    def test_key_mismatch_ignores_old_cache(self):
        c = self._open(key="keyA")
        c.store(0, 1, 20, "data")
        # 다른 키로 열면 같은 stem 이라도 디렉토리 해시가 달라 옛 캐시 무시.
        c2 = self._open(key="keyB")
        self.assertEqual(c2.cached_indices(), set())

    def test_boundary_mismatch_is_miss(self):
        """저장 청크 경계와 다른 start/end 로 load 시 미스 (belt-and-suspenders).

        캐시 키가 이미 동일 경계를 보장하지만, 방어적으로 경계 불일치를 미스 처리해
        엉뚱한 위치 병합으로 인한 출력 오염을 원천 차단한다.
        """
        c = self._open()
        c.store(0, 1, 20, "data")
        # 동일 경계 → 히트
        self.assertEqual(c.load(0, 1, 20), "data")
        # end 불일치 → 미스
        self.assertIsNone(c.load(0, 1, 25), "end 불일치 시 미스여야 함")
        # start 불일치 → 미스
        self.assertIsNone(c.load(0, 5, 20), "start 불일치 시 미스여야 함")
        # 경계 미지정(None) → 검증 생략, 히트(역호환)
        self.assertEqual(c.load(0), "data")

    def test_cleanup_removes_dir(self):
        c = self._open()
        c.store(0, 1, 20, "data")
        self.assertTrue(c.cache_dir.is_dir())
        c.cleanup()
        self.assertFalse(c.cache_dir.exists())

    def test_store_graceful_when_write_fails(self):
        c = self._open()
        # write_text 가 OSError 를 던져도 store 가 예외를 전파하지 않아야 함.
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            c.store(0, 1, 20, "data")  # 예외 없이 반환
        # 기록되지 않았으므로 load 미스.
        self.assertIsNone(c.load(0))


# ──────────────────────────────────────────────────────────────────────────────
# B) compute_cache_key — 무효화 키 민감도
# ──────────────────────────────────────────────────────────────────────────────

class TestCacheKey(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.pdf = _make_pdf(self.tmp)

    def tearDown(self):
        self._td.cleanup()

    def _key(self, **over):
        kw = dict(chunk_size=20, single_chunk_max=None,
                  prompt_template="p", provider_label="prov")
        kw.update(over)
        return rc.compute_cache_key(self.pdf, **kw)

    def test_stable_for_same_inputs(self):
        self.assertEqual(self._key(), self._key())

    def test_changes_with_chunk_size(self):
        self.assertNotEqual(self._key(chunk_size=20), self._key(chunk_size=30))

    def test_changes_with_single_chunk_max(self):
        self.assertNotEqual(self._key(single_chunk_max=None),
                            self._key(single_chunk_max=10**9))

    def test_changes_with_prompt(self):
        self.assertNotEqual(self._key(prompt_template="A"),
                            self._key(prompt_template="B"))

    def test_changes_with_provider(self):
        self.assertNotEqual(self._key(provider_label="ollama"),
                            self._key(provider_label="gemini"))

    def test_changes_when_pdf_content_changes(self):
        k1 = self._key()
        # 내용 변경 → size/mtime 변동.
        _time.sleep(0.01)
        self.pdf.write_bytes(b"%PDF-1.4 fake DIFFERENT CONTENT longer")
        k2 = self._key()
        self.assertNotEqual(k1, k2, "PDF 변경 시 키가 달라져야 함")


# ──────────────────────────────────────────────────────────────────────────────
# C) convert_pdf resume 통합
# ──────────────────────────────────────────────────────────────────────────────

class TestConvertPdfResume(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.pdf = _make_pdf(self.tmp)
        self.out = self.tmp / "out" / "doc.md"

    def tearDown(self):
        self._td.cleanup()

    def _run(self, total_pages, extract_mock, *, resume, chunk_size=20,
             single_chunk_max=None, keep_cache=False, on_failure="abort"):
        with patch.object(pp.ox, "count_pdf_pages", MagicMock(return_value=total_pages)), \
             patch.object(pp.ox, "extract_pdf_pages", extract_mock), \
             patch.object(pp.ox, "provider_label", MagicMock(return_value="prov")), \
             patch.object(pp, "time"), \
             patch.object(pp._cfg, "knob_resume_keep_cache", MagicMock(return_value=keep_cache)):
            return pp.convert_pdf(
                self.pdf, "pages {start}~{end}",
                output_path=self.out,
                chunk_size=chunk_size,
                single_chunk_max=single_chunk_max,
                rate_limit_s=0.0,
                resume=resume,
                on_failure=on_failure,
            )

    # ── (a) 캐시 없을 때 전체 추출 ───────────────────────────────────────────
    def test_a_no_cache_extracts_all(self):
        ex, calls = _mk_extract()
        result = self._run(50, ex, resume=True, keep_cache=True)
        self.assertIsNotNone(result)
        # 3청크(1-20,21-40,41-50) 모두 추출.
        self.assertEqual(calls, [(1, 20), (21, 40), (41, 50)])

    # ── (b) 일부 청크 캐시 존재 → skip + 나머지만 추출 ──────────────────────
    def test_b_partial_cache_skips_cached(self):
        # 1차: 첫 2청크만 성공, 3번째 실패(partial) → 캐시에 0,1 보존.
        ex1, calls1 = _mk_extract(["A1", "A2", RuntimeError("boom")])
        r1 = self._run(50, ex1, resume=True, on_failure="partial", keep_cache=True)
        self.assertIsNotNone(r1)  # .partial.md
        self.assertEqual(calls1, [(1, 20), (21, 40), (41, 50)])

        # 2차: resume → 캐시된 청크 0,1 은 skip, 3번째만 재추출.
        ex2, calls2 = _mk_extract(["A3_recovered"])
        r2 = self._run(50, ex2, resume=True, on_failure="abort", keep_cache=True)
        self.assertIsNotNone(r2)
        self.assertEqual(self.out.suffix, ".md")
        self.assertTrue(self.out.exists())
        # 2차에는 마지막 청크(41-50)만 추출됐어야 함.
        self.assertEqual(calls2, [(41, 50)],
                         f"캐시 청크 skip 안 됨: {calls2}")
        # 병합 본문: 캐시된 A1/A2 + 새 A3 가 순서대로.
        content = self.out.read_text(encoding="utf-8")
        self.assertIn("A1", content)
        self.assertIn("A2", content)
        self.assertIn("A3_recovered", content)
        # 순서 보존
        self.assertLess(content.index("A1"), content.index("A2"))
        self.assertLess(content.index("A2"), content.index("A3_recovered"))

    def test_b_all_cached_no_extraction(self):
        # 1차 전체 성공 + keep → 캐시 보존.
        ex1, _ = _mk_extract()
        self._run(50, ex1, resume=True, keep_cache=True)
        # 2차: 전부 캐시 → extract 0회.
        ex2, calls2 = _mk_extract()
        r2 = self._run(50, ex2, resume=True, keep_cache=True)
        self.assertIsNotNone(r2)
        self.assertEqual(calls2, [], f"전부 캐시인데 추출 발생: {calls2}")

    # ── (c) PDF 변경 → 캐시 무효화 ──────────────────────────────────────────
    def test_c_pdf_change_invalidates_cache(self):
        ex1, _ = _mk_extract()
        self._run(50, ex1, resume=True, keep_cache=True)
        # PDF 내용/시각 변경 → 키 변동 → 옛 캐시 무시 → 전부 재추출.
        _time.sleep(0.01)
        self.pdf.write_bytes(b"%PDF-1.4 CHANGED a lot of new bytes here")
        ex2, calls2 = _mk_extract()
        self._run(50, ex2, resume=True, keep_cache=True)
        self.assertEqual(calls2, [(1, 20), (21, 40), (41, 50)],
                         "PDF 변경 후에도 전부 재추출돼야 함")

    def test_c_chunk_policy_change_invalidates(self):
        ex1, _ = _mk_extract()
        self._run(50, ex1, resume=True, chunk_size=20, keep_cache=True)
        # chunk_size 변경 → 키 변동 → 재추출.
        ex2, calls2 = _mk_extract()
        self._run(50, ex2, resume=True, chunk_size=25, keep_cache=True)
        self.assertEqual(calls2, [(1, 25), (26, 50)],
                         "청크정책 변경 후 새 경계로 전부 재추출돼야 함")

    # ── (d) resume=False → 기존 동작 동일, 캐시 미생성 ──────────────────────
    def test_d_resume_off_no_cache_dir(self):
        ex, calls = _mk_extract()
        result = self._run(50, ex, resume=False)
        self.assertIsNotNone(result)
        self.assertEqual(calls, [(1, 20), (21, 40), (41, 50)])
        # .resume_cache 디렉토리가 생기면 안 됨.
        cache_root = self.out.parent / ".resume_cache"
        self.assertFalse(cache_root.exists(),
                         "resume=False 인데 캐시 디렉토리 생성됨")

    def test_d_resume_off_ignores_existing_cache(self):
        # resume=True 로 캐시 적재(keep).
        ex1, _ = _mk_extract(["KEEP1", "KEEP2", "KEEP3"])
        self._run(50, ex1, resume=True, keep_cache=True)
        # resume=False → 캐시 무시하고 전부 새로 추출.
        ex2, calls2 = _mk_extract(["NEW1", "NEW2", "NEW3"])
        self._run(50, ex2, resume=False)
        self.assertEqual(calls2, [(1, 20), (21, 40), (41, 50)])
        content = self.out.read_text(encoding="utf-8")
        self.assertIn("NEW1", content)
        self.assertNotIn("KEEP1", content,
                         "resume=False 인데 캐시 내용이 출력에 섞임")


# ──────────────────────────────────────────────────────────────────────────────
# D) 성공 시 정리 / 부분 실패 시 보존
# ──────────────────────────────────────────────────────────────────────────────

class TestCacheLifecycle(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        self.pdf = _make_pdf(self.tmp)
        self.out = self.tmp / "out" / "doc.md"

    def tearDown(self):
        self._td.cleanup()

    def _run(self, total_pages, extract_mock, *, keep_cache, on_failure="abort"):
        with patch.object(pp.ox, "count_pdf_pages", MagicMock(return_value=total_pages)), \
             patch.object(pp.ox, "extract_pdf_pages", extract_mock), \
             patch.object(pp.ox, "provider_label", MagicMock(return_value="prov")), \
             patch.object(pp, "time"), \
             patch.object(pp._cfg, "knob_resume_keep_cache", MagicMock(return_value=keep_cache)):
            return pp.convert_pdf(
                self.pdf, "pages {start}~{end}",
                output_path=self.out,
                chunk_size=20, rate_limit_s=0.0,
                resume=True, on_failure=on_failure,
            )

    def test_success_cleans_cache_by_default(self):
        ex, _ = _mk_extract()
        self._run(50, ex, keep_cache=False)
        cache_root = self.out.parent / ".resume_cache"
        # 성공 → 캐시 정리(루트는 남을 수 있으나 PDF 캐시 디렉토리는 비어야 함).
        leftover = list(cache_root.glob("doc__*")) if cache_root.exists() else []
        self.assertEqual(leftover, [], f"성공 후 캐시 잔존: {leftover}")

    def test_success_keeps_cache_when_flag(self):
        ex, _ = _mk_extract()
        self._run(50, ex, keep_cache=True)
        cache_root = self.out.parent / ".resume_cache"
        leftover = list(cache_root.glob("doc__*"))
        self.assertTrue(leftover, "EXTRACT_RESUME_KEEP=on 인데 캐시가 정리됨")

    def test_partial_failure_preserves_cache(self):
        # 마지막 청크 실패(partial) → 캐시 보존(다음 실행이 이어받도록).
        ex, _ = _mk_extract(["P1", "P2", RuntimeError("fail")])
        self._run(50, ex, keep_cache=False, on_failure="partial")
        cache_root = self.out.parent / ".resume_cache"
        leftover = list(cache_root.glob("doc__*"))
        self.assertTrue(leftover, "부분 실패 시 캐시는 보존돼야 함(resume 핵심)")
        # 성공 청크 0,1 이 캐시에 있어야 함.
        manifest = leftover[0] / "manifest.json"
        self.assertTrue(manifest.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
