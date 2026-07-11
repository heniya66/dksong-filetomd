"""test_ollama_extractor_integrity.py — H-3/H-4/H-5 데이터 무결성 회귀 테스트.

httpx 클라이언트와 extract_all_via_pdf.process_file 을 mock 하여
실제 네트워크/Ollama 호출 없이 검증한다.

테스트 항목:
  H-3 재시도/백오프:
    1) 429 → 재시도 후 성공
    2) Retry-After 헤더 존중
    3) 5xx 반복 → ExtractError
  H-4 truncation 감지:
    4) finish_reason='length' → truncated 전파 또는 잘림 마커
    5) 빈 응답 → ExtractError
  H-5 부분본 고착 방지:
    6) 실패 청크 있을 때 .partial.md 생성 또는 누락 마커 포함
    7) .partial.md 존재 시 재실행이 스킵하지 않고 재처리

실행:
    python -m pytest tests/test_ollama_extractor_integrity.py -v
    또는: python tests/test_ollama_extractor_integrity.py
"""
from __future__ import annotations

import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# 워크스페이스 루트를 sys.path 에 추가(lib.* 패키지 import 보장).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fmdw import ollama_extractor as ox  # noqa: E402
from fmdw.ollama_extractor import ExtractError  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼: httpx 응답 mock 생성
# ──────────────────────────────────────────────────────────────────────────────

def _make_response(status_code: int, json_body=None, headers=None, text_body: str = "") -> MagicMock:
    """httpx.Response mock 객체 생성."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.text = text_body
    if json_body is not None:
        resp.json = MagicMock(return_value=json_body)
    return resp


def _success_response(content: str = "## Page Content\n\nExtracted text.", finish_reason: str = "stop") -> MagicMock:
    """정상 추출 성공 응답."""
    body = {
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": finish_reason,
            }
        ]
    }
    return _make_response(200, json_body=body)


def _error_response(status_code: int, retry_after: str | None = None) -> MagicMock:
    """HTTP 오류 응답."""
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return _make_response(status_code, text_body=f"error {status_code}", headers=headers)


# ──────────────────────────────────────────────────────────────────────────────
# H-3-1: 429 → 재시도 후 성공
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryOn429(unittest.TestCase):
    """H-3: 429 응답 후 재시도하여 성공해야 한다."""

    @patch("fmdw.ollama_extractor.time")
    def test_retry_after_429_succeeds(self, mock_time):
        """429 한 번 → 재시도 → 성공."""
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = [
                _error_response(429),
                _success_response("## Result"),
            ]
            result = ox._ollama_vision("test prompt", [])
        self.assertEqual(result, "## Result")
        # 재시도 전 sleep 이 호출되었는지 확인 (백오프)
        mock_time.sleep.assert_called()

    @patch("fmdw.ollama_extractor.time")
    def test_retry_after_500_succeeds(self, mock_time):
        """500 한 번 → 재시도 → 성공."""
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = [
                _error_response(500),
                _success_response("## Result"),
            ]
            result = ox._ollama_vision("test prompt", [])
        self.assertEqual(result, "## Result")


# ──────────────────────────────────────────────────────────────────────────────
# H-3-2: Retry-After 헤더 존중
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryAfterHeader(unittest.TestCase):
    """H-3: Retry-After 헤더 값만큼 대기해야 한다."""

    @patch("fmdw.ollama_extractor.time")
    def test_retry_after_header_respected(self, mock_time):
        """Retry-After: 5 → sleep(5) 호출 확인."""
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = [
                _error_response(429, retry_after="5"),
                _success_response("## Done"),
            ]
            ox._ollama_vision("prompt", [])
        # sleep 호출 인자 중 5 이상인 것이 있어야 함 (Retry-After 5초 존중)
        sleep_args = [args[0] for args, _ in mock_time.sleep.call_args_list]
        self.assertTrue(
            any(v >= 5 for v in sleep_args),
            f"Retry-After=5 헤더를 존중해야 하지만 sleep 인자가 {sleep_args}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# H-3-3: 5xx 반복 → ExtractError
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryExhausted(unittest.TestCase):
    """H-3: 최대 재시도 소진 후 ExtractError 를 올려야 한다."""

    @patch("fmdw.ollama_extractor.time")
    def test_5xx_repeated_raises_extract_error(self, mock_time):
        """503 을 OLLAMA_MAX_RETRIES+1 회 반환하면 ExtractError."""
        import sys
        # reload 후 sys.modules 에 교체된 최신 모듈 참조 사용 (교차 오염 방지)
        _ox = sys.modules.get("fmdw.ollama_extractor", ox)
        max_retries = _ox.OLLAMA_MAX_RETRIES
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            # 재시도 횟수(max_retries) + 최초 시도(1) = max_retries+1 회 오류
            mock_client.post.side_effect = [
                _error_response(503)
            ] * (max_retries + 1)
            with self.assertRaises(_ox.ExtractError):
                _ox._ollama_vision("prompt", [])

    @patch("fmdw.ollama_extractor.time")
    def test_http_error_raises_extract_error(self, mock_time):
        """httpx.HTTPError → 재시도 후 ExtractError."""
        import httpx, sys
        _ox = sys.modules.get("fmdw.ollama_extractor", ox)
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("connection refused")
            with self.assertRaises(_ox.ExtractError):
                _ox._ollama_vision("prompt", [])


# ──────────────────────────────────────────────────────────────────────────────
# H-4-1: finish_reason='length' → truncated 전파/마커
# ──────────────────────────────────────────────────────────────────────────────

class TestTruncationDetection(unittest.TestCase):
    """H-4: finish_reason='length' 시 truncated 신호를 전파해야 한다."""

    def test_finish_reason_length_inserts_marker(self):
        """finish_reason='length' → 반환값에 TRUNCATED 마커 포함 또는 경고 로그."""
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _success_response(
                content="## Partial content that got cut",
                finish_reason="length",
            )
            result = ox._ollama_vision("prompt", [])
        # 반환값에 TRUNCATED 마커가 삽입되어야 함
        self.assertIn("TRUNCATED", result.upper(),
                      "finish_reason=length 시 결과에 TRUNCATED 마커가 있어야 함")

    def test_finish_reason_stop_no_marker(self):
        """finish_reason='stop' 시 TRUNCATED 마커 없어야 함."""
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _success_response(
                content="## Complete content",
                finish_reason="stop",
            )
            result = ox._ollama_vision("prompt", [])
        self.assertNotIn("TRUNCATED", result.upper(),
                         "finish_reason=stop 시 TRUNCATED 마커가 없어야 함")


# ──────────────────────────────────────────────────────────────────────────────
# H-4-2: 빈 응답 → ExtractError
# ──────────────────────────────────────────────────────────────────────────────

class TestEmptyResponse(unittest.TestCase):
    """H-4: 빈 응답은 ExtractError 를 올려야 한다."""

    def test_empty_content_raises_extract_error(self):
        """content 가 빈 문자열 → ExtractError."""
        import sys
        _ox = sys.modules.get("fmdw.ollama_extractor", ox)
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _success_response(content="   ", finish_reason="stop")
            with self.assertRaises(_ox.ExtractError):
                _ox._ollama_vision("prompt", [])

    def test_no_choices_raises_extract_error(self):
        """choices 없음 → ExtractError."""
        import sys
        _ox = sys.modules.get("fmdw.ollama_extractor", ox)
        with patch("fmdw.ollama_extractor.httpx.Client") as MockClient:
            mock_client = MagicMock()
            MockClient.return_value.__enter__ = MagicMock(return_value=mock_client)
            MockClient.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _make_response(200, json_body={"choices": []})
            with self.assertRaises(_ox.ExtractError):
                _ox._ollama_vision("prompt", [])


# ──────────────────────────────────────────────────────────────────────────────
# H-5-1: 실패 청크 시 .partial.md 생성 또는 누락 마커
# ──────────────────────────────────────────────────────────────────────────────

class TestPartialOutputOnFailure(unittest.TestCase):
    """H-5: 일부 청크 실패 시 부분본이 완성본으로 위장되면 안 된다."""

    def _run_process_file_with_chunks(self, chunk_results: list, tmp_dir: Path):
        """extract_all_via_pdf.process_file 을 stub 으로 실행.

        chunk_results: 청크별 반환값 (None 은 실패).
        """
        # extract_all_via_pdf 는 모듈 수준 환경변수를 읽으므로 필요 설정
        with patch.dict(os.environ, {"VISION_QA_AUTO": "0"}):
            import extract_all_via_pdf as eap
            # extract_chunk 는 같은 모듈 내 함수이므로 모듈 속성으로 patch
            call_idx = [0]
            def mock_extract_chunk(pdf_path, start, end, chunk_num):
                idx = call_idx[0]
                call_idx[0] += 1
                if idx < len(chunk_results):
                    return chunk_results[idx]
                return None

            fake_pdf = tmp_dir / "test_doc.pdf"
            fake_pdf.touch()

            orig_cwd = os.getcwd()
            try:
                os.chdir(tmp_dir)
                # count_pdf_pages: 2 페이지 → CHUNK_SIZE(20) 기본값이면 청크 1개.
                # 청크 2개를 만들려면 total_pages를 CHUNK_SIZE+1 이상으로 설정.
                # 하지만 chunk_results 길이에 맞게 total_pages 를 조정한다.
                n_chunks = len(chunk_results)
                chunk_size = eap.CHUNK_SIZE  # 기본 20
                total_pages = chunk_size * n_chunks  # n_chunks 개 청크가 나오도록

                with patch.object(eap.ox, "count_pdf_pages", return_value=total_pages), \
                     patch.object(eap, "extract_chunk", side_effect=mock_extract_chunk), \
                     patch.object(eap, "time") as mock_time:
                    eap.process_file(fake_pdf, "pdf_md")
            finally:
                os.chdir(orig_cwd)

    def test_failed_chunk_produces_partial_or_marker(self):
        """일부 청크 실패 → .partial.md 또는 누락 마커가 있어야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            # 청크 1 성공, 청크 2 실패
            self._run_process_file_with_chunks(
                chunk_results=["## Chunk 1 content", None],
                tmp_dir=tmp_dir,
            )
            output_dir = tmp_dir / "output" / "pdf_md"
            md_file = output_dir / "test_doc.md"
            partial_file = output_dir / "test_doc.partial.md"

            has_partial = partial_file.exists()
            has_marker_in_md = (
                md_file.exists()
                and "MISSING" in md_file.read_text(encoding="utf-8").upper()
            )
            self.assertTrue(
                has_partial or has_marker_in_md,
                f"실패 청크 있을 때 .partial.md 또는 MISSING 마커가 필요합니다. "
                f"partial={partial_file.exists()}, md={md_file.exists()}"
            )

    def test_all_chunks_succeed_produces_clean_md(self):
        """모든 청크 성공 → .md 파일 생성, .partial.md 없어야 함."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            self._run_process_file_with_chunks(
                chunk_results=["## Chunk 1", "## Chunk 2"],
                tmp_dir=tmp_dir,
            )
            output_dir = tmp_dir / "output" / "pdf_md"
            md_file = output_dir / "test_doc.md"
            partial_file = output_dir / "test_doc.partial.md"
            self.assertTrue(md_file.exists(), ".md 파일이 생성되어야 함")
            self.assertFalse(partial_file.exists(), "성공 시 .partial.md 없어야 함")


# ──────────────────────────────────────────────────────────────────────────────
# H-5-2: .partial.md 존재 시 재실행이 스킵하지 않음
# ──────────────────────────────────────────────────────────────────────────────

class TestSkipGuardWithPartial(unittest.TestCase):
    """H-5: .partial.md 가 있으면 재실행 시 스킵하지 말아야 한다."""

    def test_partial_md_not_skipped_on_rerun(self):
        """이미 .partial.md 만 있을 때 process_file 이 재처리한다."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            orig_cwd = os.getcwd()
            try:
                os.chdir(tmp_dir)
                with patch.dict(os.environ, {"VISION_QA_AUTO": "0"}):
                    import extract_all_via_pdf as eap

                    output_dir = tmp_dir / "output" / "pdf_md"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    # .partial.md 만 있고 .md 없음
                    partial_file = output_dir / "test_skip.partial.md"
                    partial_file.write_text("## partial", encoding="utf-8")

                    extract_chunk_called = [False]

                    def mock_extract_chunk(pdf_path, start, end, chunk_num):
                        extract_chunk_called[0] = True
                        return "## Re-extracted content"

                    fake_pdf = tmp_dir / "test_skip.pdf"
                    fake_pdf.touch()

                    with patch.object(eap.ox, "count_pdf_pages", return_value=1), \
                         patch("extract_all_via_pdf.extract_chunk",
                               side_effect=mock_extract_chunk):
                        eap.process_file(fake_pdf, "pdf_md")

                self.assertTrue(
                    extract_chunk_called[0],
                    ".partial.md 존재 시 재실행이 스킵하지 않고 extract_chunk 를 호출해야 함"
                )
            finally:
                os.chdir(orig_cwd)

    def test_missing_marker_in_md_not_skipped(self):
        """기존 .md 에 MISSING 마커 있으면 재실행 시 스킵하지 말아야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            orig_cwd = os.getcwd()
            try:
                os.chdir(tmp_dir)
                with patch.dict(os.environ, {"VISION_QA_AUTO": "0"}):
                    import extract_all_via_pdf as eap

                    output_dir = tmp_dir / "output" / "pdf_md"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    md_file = output_dir / "test_marker.md"
                    # MISSING 마커가 있는 불완전한 .md
                    md_file.write_text(
                        "## Chunk 1\n\n<!-- MISSING pages 2-2: extraction failed -->\n",
                        encoding="utf-8",
                    )

                    extract_chunk_called = [False]

                    def mock_extract_chunk(pdf_path, start, end, chunk_num):
                        extract_chunk_called[0] = True
                        return "## Re-extracted"

                    fake_pdf = tmp_dir / "test_marker.pdf"
                    fake_pdf.touch()

                    with patch.object(eap.ox, "count_pdf_pages", return_value=1), \
                         patch("extract_all_via_pdf.extract_chunk",
                               side_effect=mock_extract_chunk):
                        eap.process_file(fake_pdf, "pdf_md")

                self.assertTrue(
                    extract_chunk_called[0],
                    "MISSING 마커 있는 .md 는 재실행 시 스킵하지 않아야 함"
                )
            finally:
                os.chdir(orig_cwd)


# ──────────────────────────────────────────────────────────────────────────────
# H-4 + H-5 연계: truncated 신호도 "불완전"으로 취급
# ──────────────────────────────────────────────────────────────────────────────

class TestTruncatedSignalIsIncomplete(unittest.TestCase):
    """H-4+H-5: TRUNCATED 마커가 있는 .md 는 재실행 시 스킵하지 않아야 한다."""

    def test_truncated_marker_not_skipped(self):
        """TRUNCATED 마커 포함 .md → 재실행 시 재처리."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            orig_cwd = os.getcwd()
            try:
                os.chdir(tmp_dir)
                with patch.dict(os.environ, {"VISION_QA_AUTO": "0"}):
                    import extract_all_via_pdf as eap

                    output_dir = tmp_dir / "output" / "pdf_md"
                    output_dir.mkdir(parents=True, exist_ok=True)
                    md_file = output_dir / "test_trunc.md"
                    md_file.write_text(
                        "## Content\n<!-- TRUNCATED: finish_reason=length -->\n",
                        encoding="utf-8",
                    )

                    extract_chunk_called = [False]

                    def mock_extract_chunk(pdf_path, start, end, chunk_num):
                        extract_chunk_called[0] = True
                        return "## Complete content"

                    fake_pdf = tmp_dir / "test_trunc.pdf"
                    fake_pdf.touch()

                    with patch.object(eap.ox, "count_pdf_pages", return_value=1), \
                         patch("extract_all_via_pdf.extract_chunk",
                               side_effect=mock_extract_chunk):
                        eap.process_file(fake_pdf, "pdf_md")

                self.assertTrue(
                    extract_chunk_called[0],
                    "TRUNCATED 마커 있는 .md 는 재실행 시 스킵하지 않아야 함"
                )
            finally:
                os.chdir(orig_cwd)


# ──────────────────────────────────────────────────────────────────────────────
# 추가 견고성 권고 #1: Retry-After 과도 대기 방지 (cap 클램프)
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryAfterCap(unittest.TestCase):
    """Retry-After 헤더 값이 OLLAMA_RETRY_AFTER_CAP 을 넘으면 cap 으로 클램프해야 한다."""

    def test_huge_retry_after_clamped_to_cap(self):
        """Retry-After: 99999 → cap(기본 120) 으로 클램프."""
        cap = ox.OLLAMA_RETRY_AFTER_CAP
        delay = ox._backoff_delay(0, retry_after="99999")
        self.assertLessEqual(
            delay, cap,
            f"Retry-After=99999 는 cap({cap}s) 이하로 클램프되어야 함, 실제={delay}"
        )

    def test_normal_retry_after_not_clamped(self):
        """Retry-After 가 cap 이내면 그 값이 반영되어야 한다."""
        cap = ox.OLLAMA_RETRY_AFTER_CAP
        small_value = min(10.0, cap - 1)  # cap 보다 작은 값
        delay = ox._backoff_delay(0, retry_after=str(small_value))
        # 계산 백오프(attempt=0: base*1 + jitter ≈ 1~2s)보다 small_value 가 크므로
        # delay >= small_value 이어야 함
        self.assertGreaterEqual(
            delay, small_value,
            f"Retry-After={small_value} 는 반영되어야 함, 실제={delay}"
        )

    def test_retry_after_cap_constant_exists(self):
        """OLLAMA_RETRY_AFTER_CAP 상수가 모듈에 존재해야 한다."""
        self.assertTrue(
            hasattr(ox, "OLLAMA_RETRY_AFTER_CAP"),
            "fmdw.ollama_extractor 에 OLLAMA_RETRY_AFTER_CAP 상수가 없음"
        )
        self.assertGreater(ox.OLLAMA_RETRY_AFTER_CAP, 0)


# ──────────────────────────────────────────────────────────────────────────────
# 추가 견고성 권고 #2: Retry-After HTTP-date 형식 파싱
# ──────────────────────────────────────────────────────────────────────────────

class TestRetryAfterHttpDate(unittest.TestCase):
    """Retry-After 헤더가 RFC 7231 HTTP-date 형식일 때 delta(초)로 변환해야 한다."""

    def test_http_date_future_parsed_as_delta(self):
        """미래 HTTP-date → 양수 delta(초) 반영."""
        import datetime
        # 현재보다 30초 후 HTTP-date 생성
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=30)
        # RFC 7231 형식: "Thu, 05 Jun 2026 12:00:00 GMT"
        http_date = future.strftime("%a, %d %b %Y %H:%M:%S GMT")
        delay = ox._backoff_delay(0, retry_after=http_date)
        cap = ox.OLLAMA_RETRY_AFTER_CAP
        # delta ≈ 30초, cap 이내면 30초 근방이어야 함
        self.assertGreaterEqual(delay, 25,
            f"HTTP-date 30초 후 → delay >= 25s 여야 하지만 {delay}")
        self.assertLessEqual(delay, cap,
            f"cap({cap}s) 을 넘으면 안 됨: {delay}")

    def test_http_date_past_yields_zero_or_backoff(self):
        """과거 HTTP-date → 음수 delta → 0 처리(계산 백오프로 fallback)."""
        import datetime
        past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=60)
        http_date = past.strftime("%a, %d %b %Y %H:%M:%S GMT")
        delay = ox._backoff_delay(0, retry_after=http_date)
        # 음수 delta → 0 처리 → 계산 백오프만 남아야 함 (cap 이내이어야 함)
        self.assertGreaterEqual(delay, 0, "음수 delta 는 0 이상이어야 함")
        self.assertLessEqual(delay, ox.OLLAMA_RETRY_AFTER_CAP)

    def test_invalid_date_falls_back_to_backoff(self):
        """파싱 불가 문자열 → 예외 없이 계산 백오프로 fallback."""
        # 파싱 실패해도 ExtractError/예외 없이 float 반환이어야 함
        try:
            delay = ox._backoff_delay(0, retry_after="not-a-date-or-number")
        except Exception as exc:
            self.fail(f"파싱 실패 시 예외 없이 fallback 해야 하지만 {type(exc).__name__}: {exc}")
        self.assertGreaterEqual(delay, 0)


# ──────────────────────────────────────────────────────────────────────────────
# 추가 견고성 권고 #3: AUTO 경로 실패 페이지 인라인 MISSING 마커 대칭화
# ──────────────────────────────────────────────────────────────────────────────

class TestAutoPathMissingMarker(unittest.TestCase):
    """AUTO 경로에서 실패한 페이지 위치에 MISSING 마커가 삽입되어야 한다."""

    def _run_process_file_auto(self, page_results: list, tmp_dir: Path):
        """VISION_QA_AUTO=1 로 process_file 을 실행.

        page_results: 페이지별 (md_text|None, record) 튜플 리스트.
        """
        orig_cwd = os.getcwd()
        try:
            os.chdir(tmp_dir)
            with patch.dict(os.environ, {"VISION_QA_AUTO": "1"}):
                import importlib
                import extract_all_via_pdf as eap
                # VISION_QA_AUTO 가 모듈 로드 시 읽히므로 속성 직접 설정
                eap.VISION_QA_AUTO = 1

                call_idx = [0]

                def mock_extract_page_auto(pdf_path, page):
                    idx = call_idx[0]
                    call_idx[0] += 1
                    if idx < len(page_results):
                        md, record = page_results[idx]
                    else:
                        md, record = None, {"tier": "text", "strength": "text",
                                            "page": page, "signals": {}}
                    return md, record

                fake_pdf = tmp_dir / "test_auto.pdf"
                fake_pdf.touch()

                with patch.object(eap.ox, "count_pdf_pages",
                                  return_value=len(page_results)), \
                     patch.object(eap, "extract_page_auto",
                                  side_effect=mock_extract_page_auto), \
                     patch.object(eap, "time"):
                    eap.process_file(fake_pdf, "pdf_md")
        finally:
            os.chdir(orig_cwd)

    def _make_record(self, page: int) -> dict:
        return {"tier": "text", "strength": "text", "page": page,
                "signals": {}, "qa_called": False}

    def test_failed_page_inserts_missing_marker(self):
        """AUTO 경로에서 페이지 실패 → 해당 위치에 MISSING 마커 삽입."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            page_results = [
                ("## Page 1 content", self._make_record(1)),
                (None, self._make_record(2)),          # 페이지 2 실패
                ("## Page 3 content", self._make_record(3)),
            ]
            self._run_process_file_auto(page_results, tmp_dir)

            output_dir = tmp_dir / "output" / "pdf_md"
            partial_file = output_dir / "test_auto.partial.md"
            # 실패가 있으므로 .partial.md 로 저장되어야 함
            self.assertTrue(
                partial_file.exists(),
                ".partial.md 가 생성되어야 함"
            )
            content = partial_file.read_text(encoding="utf-8")
            self.assertIn(
                "MISSING",
                content.upper(),
                "실패 페이지 위치에 MISSING 마커가 있어야 함"
            )

    def test_all_pages_succeed_no_marker(self):
        """AUTO 경로 전 페이지 성공 → .md 생성, MISSING 마커 없음."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            page_results = [
                ("## Page 1 content", self._make_record(1)),
                ("## Page 2 content", self._make_record(2)),
            ]
            self._run_process_file_auto(page_results, tmp_dir)

            output_dir = tmp_dir / "output" / "pdf_md"
            md_file = output_dir / "test_auto.md"
            partial_file = output_dir / "test_auto.partial.md"
            self.assertTrue(md_file.exists(), ".md 파일이 생성되어야 함")
            self.assertFalse(partial_file.exists(), "성공 시 .partial.md 없어야 함")
            content = md_file.read_text(encoding="utf-8")
            self.assertNotIn("MISSING", content.upper(),
                             "전 페이지 성공 시 MISSING 마커 없어야 함")


# ──────────────────────────────────────────────────────────────────────────────
# 개선 1: provider 런타임 override (함수 인자 우선, None=env 기본값)
# ──────────────────────────────────────────────────────────────────────────────

class TestResolveProvider(unittest.TestCase):
    """_resolve_provider — 함수 인자 우선, None 이면 EXTRACT_PROVIDER 기본값."""

    def test_none_uses_module_default(self):
        """provider=None → 모듈 기본값 EXTRACT_PROVIDER 사용 (기존 동작 보존)."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"):
            self.assertEqual(ox._resolve_provider(None), "ollama_cloud")
        with patch.object(ox, "EXTRACT_PROVIDER", "gemini"):
            self.assertEqual(ox._resolve_provider(None), "gemini")

    def test_explicit_arg_overrides_module_default(self):
        """명시 인자가 모듈 기본값을 override (이번 호출 한정)."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"):
            self.assertEqual(ox._resolve_provider("gemini"), "gemini")
        with patch.object(ox, "EXTRACT_PROVIDER", "gemini"):
            self.assertEqual(ox._resolve_provider("ollama_cloud"), "ollama_cloud")

    def test_arg_normalized_strip_lower(self):
        """인자도 EXTRACT_PROVIDER 정규화 규칙(strip+lower)과 일치."""
        self.assertEqual(ox._resolve_provider("  GEMINI  "), "gemini")
        self.assertEqual(ox._resolve_provider("Ollama_Cloud"), "ollama_cloud")


class TestProviderOverrideRouting(unittest.TestCase):
    """공개 추출 함수가 provider 인자로 호출 단위 경로를 전환하는지 검증."""

    def test_extract_pdf_pages_override_to_gemini(self):
        """모듈 기본 ollama_cloud 라도 provider='gemini' 면 gemini 경로 호출."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"), \
             patch.object(ox, "_gemini_pdf_pages", return_value="GEMINI_MD") as mg, \
             patch.object(ox, "_ollama_vision") as mo:
            result = ox.extract_pdf_pages("p", Path("/x.pdf"), 1, 1, provider="gemini")
            self.assertEqual(result, "GEMINI_MD")
            mg.assert_called_once()
            mo.assert_not_called()

    def test_extract_pdf_pages_override_to_ollama(self):
        """모듈 기본 gemini 라도 provider='ollama_cloud' 면 ollama 경로 호출."""
        with patch.object(ox, "EXTRACT_PROVIDER", "gemini"), \
             patch.object(ox, "_gemini_pdf_pages") as mg, \
             patch.object(ox, "render_pdf_pages_to_base64", return_value=["b64"]), \
             patch.object(ox, "_ollama_vision", return_value="OLLAMA_MD") as mo:
            result = ox.extract_pdf_pages("p", Path("/x.pdf"), 1, 1, provider="ollama_cloud")
            self.assertEqual(result, "OLLAMA_MD")
            mo.assert_called_once()
            mg.assert_not_called()

    def test_extract_pdf_pages_default_preserved(self):
        """provider 미지정 → 모듈 기본값(ollama_cloud) 경로 (기존 동작 100% 보존)."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"), \
             patch.object(ox, "_gemini_pdf_pages") as mg, \
             patch.object(ox, "render_pdf_pages_to_base64", return_value=["b64"]), \
             patch.object(ox, "_ollama_vision", return_value="OLLAMA_MD") as mo:
            result = ox.extract_pdf_pages("p", Path("/x.pdf"), 1, 1)
            self.assertEqual(result, "OLLAMA_MD")
            mo.assert_called_once()
            mg.assert_not_called()

    def test_extract_image_override_to_gemini(self):
        """extract_image provider override 가 gemini 경로로 라우팅."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"), \
             patch.object(ox, "_gemini_image", return_value="GEMINI_IMG") as mg, \
             patch.object(ox, "_ollama_vision") as mo:
            result = ox.extract_image("p", Path("/x.png"), provider="gemini")
            self.assertEqual(result, "GEMINI_IMG")
            mg.assert_called_once()
            mo.assert_not_called()

    def test_extract_image_default_preserved(self):
        """extract_image 미지정 → ollama 경로 (기존 동작 보존)."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"), \
             patch.object(ox, "_gemini_image") as mg, \
             patch.object(ox, "render_image_to_base64", return_value=("b64", "image/png")), \
             patch.object(ox, "_ollama_vision", return_value="OLLAMA_IMG") as mo:
            result = ox.extract_image("p", Path("/x.png"))
            self.assertEqual(result, "OLLAMA_IMG")
            mo.assert_called_once()
            mg.assert_not_called()

    def test_extract_text_prompt_override_to_ollama(self):
        """모듈 기본 gemini 라도 provider='ollama_cloud' 면 ollama 텍스트 경로."""
        with patch.object(ox, "EXTRACT_PROVIDER", "gemini"), \
             patch.object(ox, "_gemini_model") as mgm, \
             patch.object(ox, "_ollama_vision", return_value="OLLAMA_TXT") as mo:
            result = ox.extract_text_prompt("p", provider="ollama_cloud")
            self.assertEqual(result, "OLLAMA_TXT")
            mo.assert_called_once()
            mgm.assert_not_called()

    def test_extract_pdf_single_page_passes_provider(self):
        """extract_pdf_single_page 가 provider 를 extract_pdf_pages 로 전달."""
        with patch.object(ox, "extract_pdf_pages", return_value="OK") as mpp:
            ox.extract_pdf_single_page("p", Path("/x.pdf"), 3, provider="gemini")
            mpp.assert_called_once_with("p", Path("/x.pdf"), 3, 3, provider="gemini")

    def test_provider_label_override(self):
        """provider_label 이 override 인자에 따라 라벨 전환."""
        with patch.object(ox, "EXTRACT_PROVIDER", "ollama_cloud"):
            self.assertIn("gemini", ox.provider_label("gemini"))
            self.assertIn("ollama_cloud", ox.provider_label("ollama_cloud"))
            # None → 모듈 기본값
            self.assertIn("ollama_cloud", ox.provider_label())


# ──────────────────────────────────────────────────────────────────────────────
# 개선 2: _gemini_model 공통 헬퍼 (중복 제거 — 동작 불변 검증)
# ──────────────────────────────────────────────────────────────────────────────

class TestGeminiModelHelper(unittest.TestCase):
    """_gemini_model — configure+GenerativeModel 중복을 묶은 헬퍼. 동작 불변."""

    def _patched_genai(self):
        """google.generativeai 를 흉내내는 mock 모듈 + GenerativeModel."""
        fake_genai = MagicMock()
        fake_model = MagicMock()
        fake_genai.GenerativeModel.return_value = fake_model
        return fake_genai, fake_model

    def test_missing_api_key_raises(self):
        """GEMINI_API_KEY 미설정 시 ExtractError (기존 각 함수 동작과 동일).

        주의: test_config_sst 가 importlib.reload(ox) 를 수행할 수 있어, 모듈 reload 후
        ox.ExtractError 가 파일 상단 import 된 ExtractError 와 다른 클래스 객체가 될 수
        있다. 전체 스위트 실행 순서 의존성을 피하려 ox.ExtractError(현재 모듈 속성)를
        대상으로 단언한다.
        """
        fake_genai, _ = self._patched_genai()
        with patch.dict("sys.modules", {"google.generativeai": fake_genai}), \
             patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GEMINI_API_KEY", None)
            with self.assertRaises(ox.ExtractError):
                ox._gemini_model()

    def test_configures_with_key_and_default_model(self):
        """키 존재 시 configure(api_key=...) + 기본 모델명으로 생성."""
        fake_genai, fake_model = self._patched_genai()
        with patch.dict("sys.modules", {"google.generativeai": fake_genai}), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "dummy-key"}):
            genai, gmodel = ox._gemini_model()
            self.assertIs(genai, fake_genai)
            self.assertIs(gmodel, fake_model)
            fake_genai.configure.assert_called_once_with(api_key="dummy-key")
            fake_genai.GenerativeModel.assert_called_once_with(
                model_name=ox.GEMINI_VISION_MODEL
            )

    def test_explicit_model_name_used(self):
        """명시 model_name 이 GenerativeModel 로 전달."""
        fake_genai, _ = self._patched_genai()
        with patch.dict("sys.modules", {"google.generativeai": fake_genai}), \
             patch.dict(os.environ, {"GEMINI_API_KEY": "dummy-key"}):
            ox._gemini_model("custom-model-x")
            fake_genai.GenerativeModel.assert_called_once_with(model_name="custom-model-x")

    def test_gemini_pdf_pages_uses_helper(self):
        """_gemini_pdf_pages 리팩토링 후에도 upload/generate/delete 흐름 동일."""
        fake_genai, fake_model = self._patched_genai()
        fake_doc = MagicMock()
        fake_doc.name = "files/abc"
        fake_genai.upload_file.return_value = fake_doc
        fake_resp = MagicMock()
        fake_resp.text = "GEMINI PDF MD"
        fake_model.generate_content.return_value = fake_resp
        with patch.object(ox, "_gemini_model", return_value=(fake_genai, fake_model)):
            result = ox._gemini_pdf_pages("p", Path("/x.pdf"))
            self.assertEqual(result, "GEMINI PDF MD")
            fake_genai.upload_file.assert_called_once()
            fake_model.generate_content.assert_called_once()
            fake_genai.delete_file.assert_called_once_with("files/abc")

    def test_gemini_image_uses_helper(self):
        """_gemini_image 리팩토링 후에도 upload/generate/delete 흐름 동일."""
        fake_genai, fake_model = self._patched_genai()
        fake_img = MagicMock()
        fake_img.name = "files/img1"
        fake_genai.upload_file.return_value = fake_img
        fake_resp = MagicMock()
        fake_resp.text = "GEMINI IMG MD"
        fake_model.generate_content.return_value = fake_resp
        with patch.object(ox, "_gemini_model", return_value=(fake_genai, fake_model)):
            result = ox._gemini_image("p", Path("/x.png"))
            self.assertEqual(result, "GEMINI IMG MD")
            fake_genai.upload_file.assert_called_once()
            fake_genai.delete_file.assert_called_once_with("files/img1")

    def test_extract_text_prompt_gemini_uses_helper(self):
        """extract_text_prompt gemini 경로가 _gemini_model 헬퍼 경유 (중복 제거)."""
        fake_genai, fake_model = self._patched_genai()
        fake_resp = MagicMock()
        fake_resp.text = "GEMINI TEXT MD"
        fake_model.generate_content.return_value = fake_resp
        with patch.object(ox, "EXTRACT_PROVIDER", "gemini"), \
             patch.object(ox, "_gemini_model", return_value=(fake_genai, fake_model)) as mh:
            result = ox.extract_text_prompt("prompt-x")
            self.assertEqual(result, "GEMINI TEXT MD")
            mh.assert_called_once()
            fake_model.generate_content.assert_called_once_with("prompt-x")


# ──────────────────────────────────────────────────────────────────────────────
# stdlib 하네스 (pytest 없을 때 직접 실행)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
